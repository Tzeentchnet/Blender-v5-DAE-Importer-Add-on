# Simple COLLADA (.dae) Importer — Blender 5 Extension

A lightweight Blender 5 **extension** that restores support for importing `.dae`
(COLLADA) files after the official importer was removed in Blender 5. It also
handles textures, polylist geometry, armatures, skin weights, and per-file
up-axis correction.

> **Upstream project:** <https://github.com/RebeccaNod1/Blender-v5-DAE-Importer-Add-on>

## Credits

This extension builds directly on prior community work:

- **[RebeccaNod1](https://github.com/RebeccaNod1)** — the immediate code base this
  extension is built on
- **[MilesExilium](https://github.com/MilesExilium)** — extended materials, armature, skin
  weights, polylist support
- **[ekztal](https://github.com/ekztal)** — original Blender 5 add-on
- Original concept by **/u/varyingopinions** on Reddit

Huge thanks to the authors above for the core importer logic. On top of that
foundation, this fork repackages everything as a proper Blender 5 extension,
adds drag-and-drop and multi-file import, a status-bar progress indicator,
configurable scale and forward axis, a correctness fix for skin-weight
assignment, and NumPy-accelerated parsing and mesh upload — see _Improvements
over upstream_ below for the full list.

## Features

- Supports both **COLLADA 1.4.1** and **COLLADA 1.5.0** schemas — namespace is
  detected at parse time so files from any compliant exporter load correctly.
- Imports COLLADA `.dae` geometry — `<triangles>` **and** `<polylist>` (auto fan-triangulated).
- Builds Principled BSDF materials with diffuse, normal, AO, and specular channels.
- Supports both **FCOLLADA** and **OpenCOLLADA3dsMax** normal-map conventions.
- Heuristic "bad diffuse" correction — substitutes the `_alb` variant when an
  exporter wires an AO/normal/spec map into the diffuse channel.
- Imports armatures from joint hierarchy + `INV_BIND_MATRIX` (toggleable).
- Imports skin weights as vertex groups + `Armature` modifier.
- Per-file `up_axis` correction (Y_UP / Z_UP / X_UP).
- **Configurable global scale** and **forward-axis** remap on the import operator.
- **Progress bar** in Blender's status bar — per-file and per-geometry updates so
  large or batched imports show responsive feedback.
- **Drag-and-drop** `.dae` files from your OS file manager into the 3D viewport.
- **NumPy-accelerated** parsing and bulk mesh data transfer for fast imports of
  large meshes.
- "Assign Textures by Name" helper operator (Object menu) for batch texture wiring.

## Installation

### Blender 5.0+ (recommended — extensions panel)

1. Download `simple_collada_importer-1.2.0.zip` (or build it from this repo —
   see _Building_ below).
2. In Blender open **Edit → Preferences → Get Extensions → Install from Disk…**
3. Select the `.zip` file.
4. The extension is enabled automatically. You will find it under the **Add-ons**
   tab as **Simple COLLADA (.dae) Importer**.

## Usage

### File menu

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
- **Scale** — uniform scale applied to imported geometry and transforms
  (default `1.0`).
- **Forward** — which DAE axis (after up-axis correction) maps to Blender's
  forward (`-Y`). Default `-Y` keeps existing behavior.

The defaults for every option above are also configurable in
**Edit → Preferences → Add-ons → Simple COLLADA (.dae) Importer**, so your
preferred values are pre-selected in both the file dialog and drag-and-drop
imports.

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

## Improvements over upstream

| Area | Upstream | This extension |
| --- | --- | --- |
| Add-on format | `bl_info` legacy | Blender 5 extension (`blender_manifest.toml`) |
| File entry point | File → Import only | File → Import **+** drag-and-drop |
| Multi-file import | One at a time | Many `.dae` files in a single drop |
| Float / int parsing | `[float(v) for v in text.split()]` | `numpy.fromstring(..., sep=" ")` |
| Mesh data transfer | `from_pydata` (Python loop) | `mesh.{vertices,loops,polygons}.foreach_set` |
| UV / color upload | per-loop Python loop | `foreach_set` from a flat NumPy buffer |
| Skin weight assign | per-pair `vgroup.add(..., 'ADD')` (sums duplicates by accident) | grouped per-bone `'REPLACE'` (intentional, vectorized) |
| Fan-triangulation | per-corner Python loop | Vectorized NumPy fan + degenerate-triangle filter |
| Source array reuse | re-parsed per primitive / per instance | Cached by `<source>` id across the whole import |
| Validation | None | `mesh.validate()` after build |

On meshes with hundreds of thousands of corners these changes typically cut
import time by an order of magnitude versus the upstream pure-Python path.

## Suggested future improvements

These are documented for contributors; they are **not** implemented yet.

- Use `xml.etree.ElementTree.iterparse` + `clear()` to reduce peak memory on
  very large `.dae` files.
- Optional preservation of vertex order for round-trip workflows.

## Building from source

The extension is a plain folder + `blender_manifest.toml`. To produce an
installable zip from a checkout:

```powershell
# PowerShell (Windows)
Compress-Archive -Path simple_collada_importer\* `
  -DestinationPath simple_collada_importer-1.2.0.zip -Force
```

```bash
# bash
cd simple_collada_importer && zip -r ../simple_collada_importer-1.2.0.zip . && cd ..
```

You can also use Blender's own validator:

```bash
blender --command extension validate simple_collada_importer-1.2.0.zip
```

## Notes

- Tested on Blender 5.0+. Skin weights require the mesh and armature to be
  exported together in the same `.dae` file.
- Normal maps on models with multiple UV channels may need to be connected
  manually in the shader editor.
- Schema coverage: COLLADA 1.4.1 and 1.5.0. Only `<triangles>` and `<polylist>`
  primitives are imported — `<polygons>`, `<lines>`, `<linestrips>`, `<tristrips>`,
  `<trifans>`, and 1.5-only B-rep / NURBS / kinematics / physics elements are
  ignored.

## License

GPL-3.0-or-later — matches Blender's add-on licensing requirements. See
[`LICENSE`](LICENSE) for the full text.
