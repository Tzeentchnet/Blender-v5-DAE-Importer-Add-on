# Code Review & Implementation Notes — Simple COLLADA Importer v1.5.2

**Status:** Implemented in **1.5.3** (see [Changelog](../README.md#changelog-selected) in README)

**Original review scope:** Static review of the 1.5.2 tree

**Primary file:** `simple_collada_importer/importer.py`

**Version under review:** `1.5.2` → fixes shipped as `1.5.3`

### Implementation status (1.5.3)

| Issue | Severity | Status in 1.5.3 |
| --- | --- | --- |
| 1 Shared-offset primitive inputs | High | **Fixed** — multi-map `inputs_by_offset` |
| 2 Joint id / name / sid mismatch | High | **Fixed and Blender-tested** — controller/skeleton-scoped aliases map to Blender's final unique bone names |
| 3 Armature ignores axis/scale correction | Medium | **Fixed and Blender-tested** — Z_UP/Y_UP bind poses and deformation verified with `arm_obj.matrix_world = correction_mat` |
| 4 Specular texture never connected | Medium | **Fixed and Blender-tested** — links Specular IOR Level (or Roughness by filename); material reuse is scoped by COLLADA material ID |
| 5 Diffuse always forces CLIP | Low/Medium | **Fixed** — diffuse alpha stays disconnected; explicit alpha owns BLEND |
| 6 Progress can exceed 1.0 after splits | Low | **Fixed and Blender-tested** — progress uses processed scene instances, clamped |
| 7 AO uses legacy MixRGB | Low/Watch | **Fixed** — prefer `ShaderNodeMix` RGBA with MixRGB fallback |
| 8 Dead / misleading parameters | Low | **Fixed** — return `joint_name_map`; removed unused matrix-named plumbing |

The sections below are the original review notes and suggested changes used to drive the 1.5.3 work. Keep them for history and regression discussion.

---
## Summary

A full pass of the Blender 5 Simple COLLADA importer found that several historical gaps (instance_controller, bind_material, normal/alpha maps, file:// paths, accessor offset/count) are **already fixed** in 1.5.2. Remaining work is a smaller set of verified correctness and completeness bugs, plus a few low-risk cleanups.

Recommended approach: fix **High** geometry and skinning issues first, gate armature orientation on a rigged repro, then complete material wiring and light UX polish. Bump to **1.5.3** after accepted fixes.

---

## Already fixed in 1.5.2 (do not re-open)

| Area | Evidence |
| --- | --- |
| `<instance_controller>` import | `import_dae.walk_scene` handles both instance types |
| Scene material bindings | `parse_instance_material_bindings` used for geometry + controller |
| Normal map wiring | `_build_mat_nodes` always links Normal Map → BSDF |
| Separate alpha / transparent map | Alpha texture → BSDF Alpha + `BLEND` |
| Texture path `file://` / URL-decode | `_resolve_tex` with `urllib.parse` |
| Accessor stride / offset / count | `parse_source_float_array` |
| Split-by-material object counts | `_finish_imported_object` counts pieces |

### Explicitly not bugs

- `np.fromstring(..., sep=" ")` with an explicit separator remains valid.
- `bmesh.ops.recalc_face_normals` is intentional for Recalculate Normals.
- Single UV layer only is documented intentional scope.
- Unsupported primitives (`polygons`, tristrips, etc.) are intentional exclusions.

---

## Issues

### Issue 1 — Shared-offset primitive inputs overwrite each other

| Field | Value |
| --- | --- |
| **Severity** | High |
| **Type** | Correctness / data loss |
| **File** | `simple_collada_importer/importer.py` |
| **Function** | `build_mesh_from_geometry` |
| **Approx. lines** | 628–640 (build map); 643–700 (consume map) |

#### Problem

Primitive `<input>` elements are stored as:

```text
input_by_offset[off] = (sem, src, set_i)
```

COLLADA allows **multiple inputs to share the same offset index** (common pattern: `VERTEX` and `NORMAL` both at offset `0`). A single dict slot keeps only the last writer. Depending on XML order, normals (or other semantics) are dropped even though their index stream column is present.

#### Impact

- Missing custom/split normals when `recalculate_normals=False`.
- Possible wrong POSITION/VERTEX resolution if a later co-offset input overwrites VERTEX.
- Affects both `<triangles>` and `<polylist>`.

#### Suggested change

1. Replace the single-slot map with a multi-map:

```text
inputs_by_offset: dict[int, list[tuple[sem, src, set]]]
```

2. Append every input: `inputs_by_offset.setdefault(off, []).append((sem, src, set_i))`.
3. Keep `num_inputs = max_offset + 1` (column count is still by max offset).
4. Resolve semantics by **scanning all inputs at all offsets**:
   - Prefer `VERTEX` → `vertices_map` for positions; fall back to `POSITION`.
   - Take first/last explicit `NORMAL`, `COLOR`.
   - For `TEXCOORD`, keep current preference: first found, or prefer `set="0"`.
5. Remove the DEBUG print path once resolution is robust, or keep it only when no POSITION is found.

#### Suggested verification

- Synthetic DAE where VERTEX and NORMAL both use `offset="0"` and TEXCOORD uses `offset="1"`.
- Import with Recalculate Normals **off**; confirm custom normals exist and shading is not faceted incorrectly.
- Regression: known-good multi-offset files still import UVs/normals.

---

### Issue 2 — Joint id / name / sid mismatch breaks skinning

| Field | Value |
| --- | --- |
| **Severity** | High |
| **Type** | Correctness / skinning failure |
| **File** | `simple_collada_importer/importer.py` |
| **Functions** | `build_armature`, `parse_controllers`, skin-weight block in `build_mesh_from_geometry` |
| **Approx. lines** | 335–370 (joint bind keys); 358–370 (bone_info by id only); 1100–1120 (vgroups) |

#### Problem

Three different string spaces are mixed:

1. Controller `Name_array` joint strings key `joint_bind_world` and are used as vertex-group names.
2. Scene joints are collected only when `type="JOINT"` and keyed by node **`id`**.
3. Edit bones are created with node **`name`** (`info["name"]`).

Bones are only created when `bid in joint_bind_world`, so `Name_array` must equal node `id`. Vertex groups are named from `Name_array`, while bones use node `name`. The Armature modifier binds groups by **bone name**. Skinning therefore only works when:

```text
node.id == node.name == Name_array entry
```

Many exporters use distinct `id`, `name`, and/or `sid`.

#### Impact

- Missing bones, empty armatures, or bones without matching vertex groups.
- Mesh imports but does not deform correctly.
- Hard to notice if the user’s sample happens to use identical strings.

#### Suggested change

1. When walking joints, store full identity:

```text
bone_info[node_id] = {
  "id": node_id,
  "name": node_name,          # display name
  "sid": node_sid or "",
  "parent_id": parent_id,
}
```

2. Build a resolve map from any of `{id, sid, name}` → joint record (first write wins; log collisions).
3. When reading controller `Name_array`, resolve each entry through that map; store both:
   - resolved joint id (for hierarchy / bind matrix)
   - final bone/vgroup name (prefer node `name`, else `id`)
4. Create bones only for resolved joints that have bind matrices.
5. When assigning vertex groups, use **the same final bone names** as created bones (map joint index → bone name), never the raw unresolved `Name_array` string if it differs.
6. Log unresolved joint names once per controller (console is fine).

#### Suggested verification

- Skinned DAE where `Name_array` uses `sid` or `name` different from node `id`.
- Confirm bone count, matching vgroup names, Armature modifier, and pose deformation.
- Regression: DAE where id/name/Name_array already match still skins correctly.

---

### Issue 3 — Armature ignores up-axis / scale correction; dead matrix parameter

| Field | Value |
| --- | --- |
| **Severity** | Medium (evidence-gated) |
| **Type** | Correctness / transform consistency; API hygiene |
| **File** | `simple_collada_importer/importer.py` |
| **Functions** | `build_armature`, `import_dae`, `build_mesh_from_geometry` |
| **Approx. lines** | 288–426 (`correction_mat` unused; returns `joint_bsm`); 1265–1277 (assigns return to `armature_node_mat`); 567+ (param unused) |

#### Problem

- Mesh instances get `obj.matrix_world = correction_mat @ node_hierarchy`.
- `build_armature(..., correction_mat=...)` never uses `correction_mat`.
- Bones are placed from inverted `INV_BIND_MATRIX` translations in raw DAE space.
- Return value is `joint_bsm` (dict), stored as `armature_node_mat` and passed into mesh build but **never used**.

Risk: on Y_UP / X_UP files, mesh is rotated into Blender Z-up while armature bones remain in DAE space → bind pose misalignment. Some exporters may already bake compatible spaces, so this needs repro before changing known-good rigs.

#### Impact

- Possible mesh/armature offset or axis mismatch on non-Z_UP skinned assets.
- Misleading API (`armature_node_mat` is not a matrix).

#### Suggested change (after repro)

1. **Reproduce first** with at least one Y_UP skinned DAE and one Z_UP skinned DAE (real Downloads files preferred).
2. If misalignment confirmed, pick one consistent model:
   - **Option A (preferred if simple):** after bones are built, set `arm_obj.matrix_world = correction_mat` (same correction applied to mesh roots). Validate skinning still looks correct because mesh vertices are in bind space and parented to armature.
   - **Option B:** transform each bone head/tail by `correction_mat` in edit mode; leave armature object matrix identity.
3. Stop returning `joint_bsm` under a matrix-sounding name. Either:
   - return `(arm_obj, None)` / just `arm_obj`, or
   - return a clearly named structure if BSM is still needed elsewhere (mesh already reads BSM from `controllers`).
4. Remove unused `armature_node_mat` parameter from `build_mesh_from_geometry` once callers are updated.
5. Do **not** ship this change without re-testing known-good rigged files.

#### Suggested verification

- Y_UP + Z_UP skinned imports: mesh and armature align in bind pose.
- Posing a bone moves the expected mesh region.
- Known-good rigged Downloads files unchanged in orientation and scale.

---

### Issue 4 — Specular texture never connected to Principled BSDF

| Field | Value |
| --- | --- |
| **Severity** | Medium |
| **Type** | Incomplete feature / docs mismatch |
| **File** | `simple_collada_importer/importer.py` |
| **Function** | `_build_mat_nodes` inside `build_mesh_from_geometry` |
| **Approx. lines** | 1007–1015 |
| **Docs** | README claims specular channel support |

#### Problem

Specular (or specularLevel / filename-heuristic) maps are extracted and an Image Texture node is created with `label="specular"`, but **no link** is made to any Principled BSDF input. The node is orphaned.

#### Impact

- Specular maps have no visual effect.
- README overstates material support.

#### Suggested change

1. After creating the specular image node (Non-Color), link into Principled BSDF:
   - Prefer **`Specular IOR Level`** if present (Blender 4+/5 Principled).
   - Else fall back to **`Specular`** if present.
2. Optional refinement (only if filename heuristics already classified the map as roughness — existing `_roughness/_rgh` hints in extraction): connect to **Roughness** instead of Specular IOR Level.
3. Do not invent a full PBR conversion; keep the link simple and deterministic.
4. Update README only if behavior changes in a user-visible way (it should).

#### Suggested verification

- DAE with `<specular><texture>` or OpenCOLLADA specularLevel map.
- Node editor: specular image → Principled Specular IOR Level (or documented socket).
- Opaque/diffuse-only materials unchanged.

---

### Issue 5 — Diffuse textures always force CLIP blend mode

| Field | Value |
| --- | --- |
| **Severity** | Low / Medium |
| **Type** | Shading quality |
| **File** | `simple_collada_importer/importer.py` |
| **Function** | `_build_mat_nodes` |
| **Approx. lines** | 940–947 |

#### Problem

Whenever a diffuse texture loads successfully, the code always:

```text
links Alpha → BSDF Alpha
m.blend_method = "CLIP"
```

Fully opaque albedo maps (common) become alpha-clipped materials, which can cause edge artifacts, sorting issues, or unexpected transparency behavior in EEVEE/Cycles.

#### Impact

- Opaque textured materials unnecessarily treated as cutout.
- Separate alpha path already sets `BLEND` correctly; diffuse path is too aggressive.

#### Suggested change

1. Always link diffuse Color → Base Color.
2. Only link diffuse Alpha → BSDF Alpha when transparency is intended:
   - Prefer: if a separate `channels["alpha"]` exists, let that path own alpha (already `BLEND`).
   - Else: either leave blend OPAQUE by default, **or** only enable CLIP when the image packs meaningful alpha (harder without packing checks).
3. Practical recommendation for this importer:
   - Default diffuse textured materials to **OPAQUE**.
   - Set `CLIP` only if an explicit transparent/alpha channel exists and is monochrome cutout-like, or keep separate alpha path as the sole transparency driver.
   - Color-only transparent materials already set `BLEND` from `_diffuse_color` alpha — keep that.

#### Suggested verification

- Opaque PNG/JPG diffuse: material blend remains OPAQUE (or default).
- Diffuse with alpha cutout **and** explicit transparent map: still transparent as expected.
- Color alpha `< 1` without texture: still BLEND.

---

### Issue 6 — Single-file progress can exceed 1.0 after material splits

| Field | Value |
| --- | --- |
| **Severity** | Low |
| **Type** | UX |
| **File** | `simple_collada_importer/importer.py` |
| **Function** | `import_dae._finish_imported_object` |
| **Approx. lines** | 1288–1298 |

#### Problem

Progress is updated as `imported / geom_total`, where `imported` is the count of objects **after** `split_by_material`. One geometry can produce many objects, so the ratio can exceed 1. Multi-file imports pass `wm=None` into `import_dae` and manage progress in the operator, so they are fine.

#### Impact

- Status bar can complete early or behave oddly; import correctness unaffected.

#### Suggested change

Pick one:

- **A (minimal):** `wm.progress_update(min(imported / geom_total, 1.0))`
- **B (more accurate):** track `geometries_done` separately from object piece count; update progress by geometry completion, not piece count.

Recommendation: **A** unless you want accurate multi-geometry progress with splits.

#### Suggested verification

- Single file, many materials, Split by Material on: progress never reports > complete; import still succeeds.

---

### Issue 7 — AO mix uses legacy `ShaderNodeMixRGB` (watch item)

| Field | Value |
| --- | --- |
| **Severity** | Low / Watch |
| **Type** | Compatibility |
| **File** | `simple_collada_importer/importer.py` |
| **Function** | `_build_mat_nodes` |
| **Approx. lines** | 986–1005 |

#### Problem

AO multiplies diffuse via `nodes.new("ShaderNodeMixRGB")`. Blender 3.4+ introduced `ShaderNodeMix` as the modern mix node; MixRGB may still exist as a legacy type in Blender 5, but this is not guaranteed forever and socket names differ.

#### Impact

- AO path may fail or no-op if node type creation fails (currently no try/except around node creation).

#### Suggested change

1. Confirm in Blender 5.0+ that `ShaderNodeMixRGB` still constructs.
2. If it fails or is deprecated in practice, switch to:

```text
mix = nodes.new("ShaderNodeMix")
mix.data_type = "RGBA"
mix.blend_type = "MULTIPLY"
# Factor / A / B / Result sockets (RGBA mode)
```

3. Keep multiply factor at 1.0 and rewire Base Color through the mix result as today.

#### Suggested verification

- DAE with both diffuse + AO textures: Base Color is AO-multiplied diffuse; no console errors.

---

### Issue 8 — Dead / misleading parameters (cleanup after Issue 3)

| Field | Value |
| --- | --- |
| **Severity** | Low |
| **Type** | Maintainability |
| **File** | `simple_collada_importer/importer.py` |
| **Symbols** | `build_armature.correction_mat`, return `joint_bsm` as `armature_node_mat`, `build_mesh_from_geometry.armature_node_mat` |

#### Problem

Parameters and return values suggest transform plumbing that does not exist. Confuses future contributors and hides Issue 3.

#### Suggested change

- Fold into Issue 3 resolution.
- After transform behavior is decided, delete unused parameters or rename returns to match reality.
- No user-facing behavior change from cleanup alone.

---

## Recommended implementation order

| Phase | Issues | Notes |
| --- | --- | --- |
| 1 | Issue 1 | Geometry input multi-map; low regression risk if tests cover shared offset |
| 2 | Issue 2 | Joint resolve map; critical for skinned non-matching names |
| 3 | Issue 3 (+8) | Evidence-gated; do not ship without rigged Y_UP/Z_UP tests |
| 4 | Issues 4, 5, 6, (7) | Material completeness + soft UX; Issue 7 only if needed |
| 5 | Docs / version | README truth, manifest `1.5.2` → `1.5.3`, `build.ps1` zip, extension validate |

### Parallelism

- Phase 1 and Phase 4 can proceed in parallel once Issue 1’s input map shape is stable (Phase 4 does not depend on it).
- Phase 2 should land before heavy armature transform work (Phase 3) so skin tests are meaningful.
- Phase 3 blocks on repro samples.

---

## Files to touch

| Path | Changes |
| --- | --- |
| `simple_collada_importer/importer.py` | Issues 1–8 |
| `simple_collada_importer/operators.py` | Only if multi-file progress needs related tweaks (likely none) |
| `simple_collada_importer/blender_manifest.toml` | Patch version bump after fixes |
| `README.md` | Specular wiring, joint matching, any armature correction decision, blend-mode behavior |
| `build.ps1` | Rebuild installable zip |

No changes expected to `__init__.py`, `file_handler.py`, or `preferences.py` for these issues.

---

## Acceptance / regression checklist

1. Shared-offset VERTEX+NORMAL DAE imports with usable normals when recalculate is off.
2. Known-good geometry-only Downloads DAEs: object count, UVs, textures, scale, orientation unchanged.
3. Skinned DAE with id/name/sid mismatch: bones + matching vgroups + deformation.
4. If Phase 3 ships: Y_UP and Z_UP rigged DAEs align; known-good rigs still good.
5. Specular map linked into Principled BSDF.
6. Opaque diffuse materials not forced to CLIP.
7. Split-by-material single-file progress ≤ complete.
8. AO path still multiplies diffuse (or fixed via ShaderNodeMix).
9. `.\build.ps1` + `blender --command extension validate dist/simple_collada_importer-<ver>.zip`.

---

## Out of scope (this pass)

- Streaming/`iterparse` memory rewrite
- Multi-UV layer import
- Unsupported primitives (`polygons`, lines, tristrips, NURBS, physics)
- Replacing NumPy parsing for its own sake
- Full PBR conversion from Phong/Blinn
- Broad armature redesign without a failing repro

---

## Discussion questions for reviewers

1. **Issue 3:** Do we have a local Y_UP skinned sample that currently misaligns? If not, leave transform change as “investigate only” for 1.5.3.
2. **Issue 5:** Prefer always-OPAQUE diffuse textures, or attempt to detect image alpha?
3. **Issue 4:** Specular map → Specular IOR Level only, or also support roughness-by-filename?
4. Ship as one 1.5.3 PR, or split High (1–2) vs Medium/Low?

---

## Suggested commit / PR breakdown (if splitting)

1. **fix(geometry): preserve co-offset primitive inputs** — Issue 1
2. **fix(skin): resolve joint id/name/sid for bones and vgroups** — Issue 2
3. **fix(rig): apply axis correction to armature (if repro confirms)** — Issue 3 + 8
4. **fix(materials): wire specular; stop forcing CLIP on opaque diffuse** — Issues 4–5
5. **chore: clamp import progress; optional Mix node; docs + 1.5.3** — Issues 6–7 + docs
