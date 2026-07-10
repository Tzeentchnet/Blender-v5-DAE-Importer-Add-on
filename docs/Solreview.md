# 1.5.3 Implementation Audit

**Audit date:** 2026-07-09
**Reviewed:** `docs/code-review-1.5.2.md`, the working-tree changes to
`simple_collada_importer/importer.py`, the 1.5.3 README/manifest updates, and
the built extension package

**Runtime used:** Blender 5.1.1 on Windows
**Remediation status:** All actionable findings below were fixed and re-tested
on 2026-07-09. The findings are retained as the audit trail.

## Verdict

The initial audit found two skinning regressions, an unsafe material-reuse key,
inaccurate progress accounting, public wording that overstated the render
mode, and packaging that could include Python caches. Each actionable finding
has now been addressed. Focused Blender 5.1.1 tests verified duplicate bone
names, controller-scoped skeleton aliases, Z_UP/Y_UP bind transforms and pose
deformation, distinct material graphs, repeated-instance progress, extension
registration, package contents, and extension validation.

No known release-blocking issue remains from this audit. Real exporter files
are still recommended as final compatibility smoke tests because the automated
runtime probes use minimal synthetic COLLADA documents.

## Findings

### 1. Resolved (High): duplicate bone display names routed weights incorrectly

**Affected code:** `build_armature` and the skin-weight block in
`build_mesh_from_geometry`.

`joint_name_map` is populated with the requested node display name before
Blender creates the edit bone. Blender guarantees unique bone names by
suffixing duplicates. For two joints both named `Bone`, the implementation
records both controller names as `Bone`, while Blender creates `Bone` and
`Bone.001`. Both controller joint indices therefore resolve to the first
vertex group and the second bone receives no matching weights.

This was reproduced in Blender 5.1.1:

```text
BONES ['Bone', 'Bone.001']
MAP {'j1': 'Bone', 's1': 'Bone', 'Bone': 'Bone',
     'j2': 'Bone', 's2': 'Bone'}
```

This makes the review document's Issue 2 status (`vgroups match bones`) false
for valid files with repeated display names. Blender name truncation or other
automatic sanitization can cause the same class of mismatch.

**Suggested changes:**

1. Resolve each raw controller name to a stable scene joint ID first, not
   directly to the requested display name.
2. Create all edit bones and capture the actual `eb.name` returned by Blender.
3. Build the controller-joint-to-vertex-group map from
   `created[joint_id].name` after creation.
4. Detect and report ambiguous `id`/`sid`/`name` aliases instead of silently
   selecting the first one.
5. Add a regression fixture with two joint IDs that share one `name`.

**Resolution:** joint aliases now resolve to internal joint keys first. The
controller map is created only after edit-bone creation and records each
actual `eb.name`, including Blender-generated suffixes such as `.001`.
An end-to-end Blender import verified matching `Bone` / `Bone.001` groups and
pose deformation.

### 2. Resolved (High): global aliases collapsed separate skeletons

**Affected code:** `build_armature`.

`joint_bind_raw` is globally keyed by the raw `Name_array` string, and the
`resolve` table is globally first-write-wins across the visual scene. Separate
controllers/skeleton roots commonly use local `sid` or display-name values.
When those strings repeat, the later controller is discarded before a bone
can be built.

This was reproduced with two controllers and two root joints that both used
`sid="sharedSid"`:

```text
Armature 'Armature' created with 1 bones.
BONES ['RigOne']
MAP {'sharedSid': 'RigOne', 'root1': 'RigOne', 'RigOne': 'RigOne'}
```

This limitation existed in part before 1.5.3, but the new claim that joint
`id`/`name`/`sid` resolution is fixed is too broad. The global alias design
cannot correctly represent multiple skeleton namespaces.

**Suggested changes:**

1. Keep joint bindings and resolved names per controller rather than in one
   document-wide map; key at least by `(controller_id, joint_index)`.
2. Resolve each `instance_controller` against its `<skeleton>` root(s) when
   present, limiting `sid`/`name` lookup to that hierarchy.
3. Either build one armature per distinct skeleton or maintain an explicit
   controller-to-armature mapping.
4. Add a two-controller fixture with reused `sid` values.

**Resolution:** bind data and name maps are now controller-owned and keyed by
`(controller_id, skeleton_refs)`. Alias lookup is restricted to each
`instance_controller`'s declared skeleton hierarchy. A full two-controller
import with reused `sharedSid` values produced `RigOne` and `RigTwo`, and each
mesh received only its matching vertex group.

### 3. Resolved (Medium): material reuse ignored non-diffuse channels

**Affected code:** `_mat_diffuse_path` and material selection in
`build_mesh_from_geometry`.

The material cache/reuse decision compares only the diffuse texture path. Two
COLLADA materials that share an albedo but use different specular, roughness,
AO, alpha, or scalar/color settings are treated as the same Blender material.
The second material therefore keeps the first material's node graph.

This was reproduced in Blender 5.1.1 with two materials sharing
`common.png` but using `spec1.png` and `spec2.png`:

```text
SAME_MATERIAL True
SPEC_IMAGES [('diffuse', 'common.png'), ('specular', 'spec1.png')]
```

The second object should have used `spec2.png`. This behavior predates the
new specular link, but it means Issue 4 is only fixed for a newly created
material (or one whose diffuse path does not collide). It can also undermine
the separate-alpha behavior described by Issue 5.

**Suggested changes:**

1. Build a complete immutable material signature from all resolved texture
   paths plus relevant colors/scalars, or cache by COLLADA material ID within
   one import.
2. Store that signature as a custom property on generated Blender materials
   if cross-import reuse is desired.
3. Reuse a material only when the complete signature matches.
4. Add a fixture with a shared diffuse map and distinct specular/alpha maps.

**Resolution:** generated materials are cached only within one DAE import and
keyed by the document-unique COLLADA material ID plus the default-material
mode. A Blender runtime test confirmed two materials sharing one diffuse image
remain distinct and retain `spec1.png` / `spec2.png` respectively.

### 4. Resolved (Medium): armature correction required runtime verification

**Affected code:** `build_armature` (`arm_obj.matrix_world = correction_mat`).

The implementation chose Option A from the original review. A Blender runtime
check confirmed that, for a basic parented object, assigning the same corrected
world matrix to the armature and mesh yields an identity mesh local transform;
there is no obvious double correction in that simple case.

However, the repository contains no DAE fixtures or automated tests, and no
rigged Y_UP/Z_UP sample was available during this audit. Therefore bind-pose
alignment, deformation, non-identity controller-node transforms, bind-shape
matrices, and known-good rig regressions were not tested. The original review
explicitly said not to ship this change without those checks, while its new
status banner calls the issue fixed.

**Suggested changes:**

1. Change the review status to **Implemented; runtime verification required**
   until the acceptance tests exist.
2. Test at least one Y_UP and one Z_UP skinned DAE, including a pose/deformation
   check and a known-good regression file.
3. Include a controller instance under a non-identity scene-node transform and
   a non-identity bind-shape matrix.
4. Keep or revert the object-level correction based on those results; do not
   infer correctness from matrix inspection alone.

**Resolution:** end-to-end synthetic Z_UP and Y_UP controller imports verified
matching armature/mesh world matrices, unique vertex groups, and evaluated
mesh deformation after posing the child bone. Real exporter samples remain a
recommended compatibility smoke test, but the evidence gate is now satisfied
for the implemented transform model.

### 5. Resolved (Low): progress used a mismatched numerator and denominator

**Affected code:** `import_dae._finish_imported_object`.

`geom_total` counts unique `<geometry>` definitions, while `geometries_done`
increments for every successfully imported scene instance. If one geometry is
instanced twice, progress reaches 100% on the first instance and remains there
for the second. If the library contains unused geometries, progress may not
reach 100% through importer updates.

The original overflow bug is fixed because the value is clamped, but the
README phrase "per-geometry updates" and the code comment "completed source
geometries" are not accurate for instanced scenes.

**Suggested change:** pre-count importable `instance_geometry` and
`instance_controller` occurrences in the selected visual scene, then advance
one step per processed occurrence. Keep the clamp as a defensive guard.

**Resolution:** the importer now pre-counts importable scene instances and
advances once per processed instance. A repeated-geometry test reported
exactly `[0.5, 1.0]`.

### 6. Resolved (Low): OPAQUE wording did not match Blender 5.1's API

**Affected documentation:** README and the implementation-status table in the
original review.

The code correctly stops wiring every diffuse image's Alpha output and stops
forcing `CLIP`. With an unlinked alpha socket at 1.0, the material is visually
opaque. It does not, however, explicitly set an OPAQUE render mode. Blender
5.1.1 reported the following for a new material:

```text
blend_method = HASHED
surface_render_method = DITHERED
```

Even assigning legacy `blend_method = "OPAQUE"` retained that Blender 5.1
state. The behavioral change is reasonable, but "opaque diffuse stays
OPAQUE" is inaccurate API terminology.

`has_separate_alpha` is also assigned in `_build_mat_nodes` but never read.

**Suggested changes:**

1. Document this as "diffuse alpha is not connected unless an explicit
   transparent/alpha channel is present" rather than claiming an OPAQUE mode.
2. Remove the unused `has_separate_alpha` local.
3. Keep the explicit alpha path using Blender's supported transparency API.

**Resolution:** README wording now states the observable behavior (diffuse
alpha remains disconnected unless an explicit alpha channel drives it), and
the unused `has_separate_alpha` local was removed.

### 7. Resolved (Low): release zip included Python cache artifacts

**Affected code:** `build.ps1` packaging behavior.

`Compress-Archive -Path (Join-Path $pkgDir "*")` includes every package child.
After syntax compilation, the built 1.5.3 archive contained:

```text
__pycache__/__init__.cpython-313.pyc
__pycache__/file_handler.cpython-313.pyc
__pycache__/importer.cpython-313.pyc
__pycache__/operators.cpython-313.pyc
__pycache__/preferences.cpython-313.pyc
```

These files are ignored by Git but not by the build script. Blender's extension
validator still accepted the archive, so this is package hygiene rather than a
runtime failure.

**Suggested change:** stage only manifest/source/license files into a temporary
directory before compression, or explicitly exclude `__pycache__`, `*.pyc`,
and other generated artifacts. Add an archive-content check to the build.

**Resolution:** `build.ps1` now stages the package in a temporary directory,
removes `__pycache__`, `*.pyc`, `*.pyo`, and `.DS_Store`, and cleans the stage
in `finally`. The existing `.gitignore` already covers `__pycache__/` and
`*.pyc`, so no duplicate ignore rule was added. A build performed while cache
files were present produced zero cache entries and passed Blender validation.

## Per-Issue Accuracy Review

| Original issue | Audit result | Notes |
| --- | --- | --- |
| 1 Shared-offset inputs | **Verified fixed** | Blender 5.1 synthetic mesh preserved positions, UVs, and corner normals with VERTEX+NORMAL at offset 0. |
| 2 Joint id/name/sid | **Verified fixed** | Final Blender names and controller/skeleton-scoped aliases pass duplicate-name and two-controller imports. |
| 3 Armature correction | **Verified fixed synthetically** | Z_UP/Y_UP transforms and pose deformation pass; real exporter smoke tests remain advisable. |
| 4 Specular wiring | **Verified fixed** | Socket wiring and material-ID-scoped reuse preserve distinct specular graphs. |
| 5 Diffuse CLIP | **Verified fixed** | Diffuse alpha remains disconnected; documentation now describes behavior rather than an OPAQUE enum. |
| 6 Progress overflow | **Verified fixed** | Instance-based progress reports correct fractions for repeated geometry. |
| 7 AO Mix node | **Verified fixed on Blender 5.1** | `ShaderNodeMix` selected active RGBA A/B/Result sockets correctly. |
| 8 Dead/misleading plumbing | **Verified fixed** | Matrix-named plumbing and the unused `has_separate_alpha` local are gone. |

## Validation Performed

- `python -m py_compile` passed for all add-on Python files.
- VS Code diagnostics reported no errors in `importer.py`, README, or the
  original review document.
- `git diff --check` found no patch whitespace errors (only Windows line-ending
  warnings).
- `build.ps1` produced `dist/simple_collada_importer-1.5.3.zip`.
- Blender 5.1.1 extension validation accepted the zip and parsed its manifest.
- Blender 5.1.1 runtime probes covered:
  - shared-offset VERTEX/NORMAL plus TEXCOORD import;
  - modern Mix node RGBA socket selection;
  - transparency property behavior;
  - duplicate bone display names;
  - duplicate local skeleton aliases;
  - material reuse with distinct specular maps;
  - parent/world correction matrix composition.
- Post-remediation end-to-end Blender 5.1.1 checks additionally covered:
   - Z_UP and Y_UP skinned imports with duplicate display names;
   - matching final bone and vertex-group names;
   - evaluated mesh deformation after posing a child bone;
   - two controller instances reusing one local `sid` under separate roots;
   - repeated geometry instances reporting progress `[0.5, 1.0]`;
   - complete extension register/unregister lifecycle;
   - cache exclusion while cache files were present during the build.

## Remaining Test Gaps

- No real or synthetic full DAE fixture is stored in the repository.
- No end-to-end skin deformation test was run.
- No known-good Y_UP/Z_UP rigged DAE regression test was available.
- Blender 5.0 specifically was not run; runtime probes used Blender 5.1.1.
- Performance and very large mesh behavior were not retested.

## Completed Release Gate

1. Actual Blender bone names are captured after creation.
2. Multiple skeleton contexts are controller/skeleton scoped and tested.
3. Material reuse is DAE-local and keyed by COLLADA material identity.
4. Synthetic Z_UP/Y_UP bind and deformation acceptance tests pass.
5. Internal review status and public README wording match tested behavior.
6. A cache-free 1.5.3 zip builds and passes Blender extension validation.