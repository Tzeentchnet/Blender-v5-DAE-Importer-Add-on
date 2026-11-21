# Improvements over upstream

| Area | Upstream | This extension |
| --- | --- | --- |
| Add-on format | `bl_info` legacy | Blender 5 extension (`blender_manifest.toml`) |
| File entry point | File → Import only | File → Import, File → Export, and drag-and-drop |
| COLLADA export | None | Tested COLLADA 1.4.1 hierarchy, mesh, material, camera/light, skin, and morph export |
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
