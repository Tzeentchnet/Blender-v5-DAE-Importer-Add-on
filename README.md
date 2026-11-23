# Simple COLLADA (.dae) Importer/Exporter — Blender 5 Extension

A Blender 5 **extension** that restores broad `.dae` (COLLADA) scene import and
export after the official integration was removed in Blender 5. It imports
scene hierarchies, geometry, classic materials, armatures, animation, morphs,
cameras, and lights through a tested NumPy-accelerated pipeline, and exports a
round-trip-oriented COLLADA 1.4.1 scene subset.

> **Upstream project:** <https://github.com/RebeccaNod1/Blender-v5-DAE-Importer-Add-on>

## Features

- Supports both **COLLADA 1.4.1** and **COLLADA 1.5.0** schemas — namespace is
  detected at parse time so files from any compliant exporter load correctly.
- Selects the active `<visual_scene>`, expands `<instance_node>` safely, retains
  object parenting, and shares mesh data across repeated unmodified instances.
- Applies COLLADA units, X/Y/Z up-axis conversion, configurable forward axis,
  and global scale exactly once across scene data.
- Imports `<triangles>`, `<polylist>`, concave `<polygons>`, `<tristrips>`,
  `<trifans>`, `<lines>`, and `<linestrips>`. Line primitives become mesh edges.
- Preserves every declared UV and vertex-color set and honors
  `<bind_vertex_input>` texture-to-UV mappings.
- Builds Principled materials from Constant, Lambert, Blinn, and Phong effects,
  including diffuse, emission, specular, shininess, reflectivity, IOR,
  transparency/opaque modes, normal or bump data, double-sided settings,
  texture transforms, and correct color spaces.
- Keeps AO, roughness, metallic, and filename inference as fallbacks when
  explicit COLLADA bindings are unavailable.
- Imports skin controllers with bind-shape and inverse-bind matrices, multiple
  skeleton scopes, non-deforming hierarchy bones, and arbitrary influence
  counts.
- Imports nested object and pose-bone animation with STEP, LINEAR, and BEZIER
  sampling. COLLADA clips remain separate Blender Actions.
- Imports normalized and relative morph controllers as shape keys, including
  initial and animated morph weights.
- Imports perspective/orthographic cameras and ambient, directional, point, and
  spot lights.
- Exports COLLADA 1.4.1 hierarchy, triangles, loose edges, every UV/color
  layer, Principled materials and local texture references, armatures, skin
  weights, relative/normalized shape keys, cameras, and lights.
- Uses staged `iterparse`, strict accessor/index validation, NumPy bulk paths,
  cooperative cancellation hooks for scripted imports, and file-atomic
  rollback.
- Provides optional **Preserve Vertex Order** source-index metadata.
- Shows status-bar progress for single and batched files.
- **Drag-and-drop** `.dae` files from your OS file manager into the 3D viewport.
- "Assign Textures by Name" helper operator (Object menu) for batch texture wiring.

## Installation

### Blender 5.0+ (recommended — extensions panel)

1. Download `simple_collada_importer-2.1.0.zip` (or build it from this repo —
   see _Building_ below).
2. In Blender open **Edit → Preferences → Get Extensions → Install from Disk…**
3. Select the `.zip` file.
4. The extension is enabled automatically. You will find it under the **Add-ons**
   tab as **Simple COLLADA (.dae) Importer/Exporter**.

## Usage

### Import

**File → Import → Simple COLLADA (.dae)** — opens a file dialog. The sidebar
(and F9 redo panel) exposes:

- **Import Rig** — skip armature/skin import for geometry-only loads.
- **Split by Material** — split each imported geometry into one object per
  material, so each piece appears separately in the Outliner. Off by default.
- **Use Blender Default Material** — ignore the DAE's diffuse / specular
  colors and texture references and assign each material Blender's stock
  Principled BSDF defaults instead. Useful when a DAE has no usable materials
  or its textures cannot be located, to avoid splotchy chrome-like viewport
  shading. Off by default.
- **Recalculate Normals (Outside)** — discard any per-corner normals from the
  DAE and recompute consistent outward-facing normals after import (same as
  Mesh → Normals → Recalculate Outside in edit mode). All polygons are also
  marked smooth. Fixes DAE files with inconsistent face winding that show up
  as dark / flipped patches on the model. On by default.
- **Preserve Vertex Order** — retain source position indices and write a
  `collada_source_index` point attribute for shape-transfer and round-trip
  workflows. Off by default.
- **Attach Empties to Bones** — convert non-joint nodes directly below a
  COLLADA joint to native Blender bone parenting. Off by default.
- **Construct Armature from Bind Pose** — use inverse-bind matrices and
  child-derived display lengths when creating bones instead of scene joint
  transforms and fixed display lengths. Off by default.
- **Scale** — uniform scale applied to imported geometry and transforms
  (default `1.0`).
- **Forward** — which DAE axis (after up-axis correction) maps to Blender's
  forward (`-Y`). Default `-Y` keeps existing behavior.

The defaults for every option above are also configurable in
**Edit → Preferences → Add-ons → Simple COLLADA (.dae) Importer/Exporter**, so your
preferred values are pre-selected in both the file dialog and drag-and-drop
imports.

### Export

**File → Export → Simple COLLADA (.dae)** writes a COLLADA 1.4.1 file. The
export dialog exposes:

- **Selected Objects** — export the selection plus required parents and
  armatures instead of the complete scene.
- **Export Armatures** — include joint hierarchies, skin controllers, and
  vertex weights.
- **Reference Textures** — include local image paths used by exported
  Principled materials.
- **Scale** — apply a uniform scale to exported root transforms.
- **Forward** — choose the forward-axis basis written to the DAE.

### Drag and drop

Drag one or more `.dae` files from your OS file manager (Explorer, Finder,
Nautilus, …) onto the **3D Viewport** or **Outliner**. The files are imported
immediately using the defaults set in the add-on preferences. Use the **F9
redo panel** to adjust any option after the drop.

### Assign Textures by Name

After import, in the **Object menu** select **Assign Textures by Name**, then
pick a folder. Materials whose name matches an image file (e.g. material
`brick_wall` and image `brick_wall.png`) get a fresh Principled BSDF + image
texture.

## Developer documentation

- [Add-on architecture](docs/architecture.md)
- [COLLADA/DAE import pipeline](docs/import-pipeline.md)
- [COLLADA/DAE export pipeline](docs/export-pipeline.md)

## Improvements over upstream

See [Improvements over upstream](docs/improvements-over-upstream.md) for a
detailed comparison with the original importer.

## Changelog

See the [Changelog](docs/changelog.md) for release details.

## Testing

See [Testing](docs/testing.md) for the pure-Python, headless Blender, optional
asset-corpus, and performance benchmark workflows.

## Building from source

The extension is a plain folder + `blender_manifest.toml`. Use Blender's
extension builder so development files and Python caches are excluded from the
package:

```powershell
blender --command extension build `
  --source-dir simple_collada_importer `
  --output-dir dist
```

Validate the resulting archive with Blender:

```powershell
blender --command extension validate dist/simple_collada_importer-2.1.0.zip
```

## Notes

- Tested on Blender 5.0+. Skin weights require the mesh and armature to be
  exported together in the same `.dae` file.
- Joint identifiers from the skin controller are matched to scene joints by
  `id`, `name`, or `sid`. Bone and vertex-group names prefer the joint's
  display `name` so the Armature modifier can bind even when exporters use
  different identifier spaces.
- Armatures inherit the same up-axis / scale / forward correction applied to
  mesh instances. If a known-good Z_UP rig looks wrong after upgrade, please
  report it with a sample file.
- Specular maps connect to Principled **Specular IOR Level** by default. Maps
  whose filenames contain roughness hints (`_roughness`, `_rgh`) connect to
  **Roughness** instead. This is a simple COLLADA→Principled mapping, not a
  full PBR conversion.
- Diffuse texture alpha remains disconnected by default. Separate
  transparent/alpha maps set blend to BLEND; solid color materials with
  alpha < 1 also use BLEND.
- Network URIs are never fetched. Local image/file URIs are resolved relative
  to the DAE and unresolved references are reported.
- Export uses the stored Mesh datablock rather than applying arbitrary
  modifiers. Armature modifiers and shape keys are serialized as COLLADA skin
  and morph controllers.
- Animation/Action export, B-rep/NURBS surfaces, physics,
  kinematics/formulas, non-`profile_COMMON` effects, lossless round-trip
  guarantees, and ZAE archives remain out of scope.

## Credits

This extension builds directly on prior community work:

- **[RebeccaNod1](https://github.com/RebeccaNod1)** — the immediate code base this
  extension is built on
- **[TwistedFlog](https://github.com/TwistedFlog)** — exporter, bone attachment,
  and bind-pose workflow inspiration
- **[MilesExilium](https://github.com/MilesExilium)** — extended materials, armature, skin
  weights, polylist support
- **[ekztal](https://github.com/ekztal)** — original Blender 5 add-on
- Original concept by **/u/varyingopinions** on Reddit

Huge thanks to the authors above for the core import/export ideas. On top of that
foundation, this fork repackages everything as a proper Blender 5 extension,
adds drag-and-drop and multi-file import, a status-bar progress indicator,
configurable scale and forward axis, a correctness fix for skin-weight
assignment, and NumPy-accelerated parsing and mesh upload — see _Improvements
over upstream_ below for the full list.

## License

GPL-3.0-or-later — matches Blender's add-on licensing requirements. See
[`LICENSE`](LICENSE) for the full text.
