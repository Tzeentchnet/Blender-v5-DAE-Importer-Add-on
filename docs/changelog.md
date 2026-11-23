# Changelog

## 2.1.1

- Removed the background parsing thread from the import operator. Imports now
  run synchronously on Blender's main thread because `threading` and `queue`
  can crash Blender. Escape no longer cancels an interactive import.

## 2.1.0

- Added COLLADA 1.4.1 export for scene hierarchy, triangles, loose edges,
  UV/color layers, Principled materials and texture references, cameras,
  lights, armatures, skin weights, and shape-key morph controllers.
- Added selected-object export, armature and texture toggles, global scale, and
  every forward-axis choice.
- Added default-off import preferences for native bone parenting of attached
  Empties and bind-pose-based armature construction.
- Added Blender 5.2/5.3 export/import round-trip coverage.

## 2.0.1

- Built release archives with Blender's extension command-line tooling so
  Python caches and development-only files are excluded.

## 2.0.0

- Added a Blender-independent COLLADA intermediate representation, structured
  diagnostics, strict accessors/reference resolution, and staged XML parsing.
- Added active-scene selection, units, node instances/hierarchy, look-at
  transforms, shared meshes, all roadmap primitive types, concave
  triangulation, multiple UV/color sets, and per-texture UV binding.
- Added complete classic-effect translation, animation clips/Actions, bone
  animation, morph shape keys, cameras, and lights.
- Added synthetic pure-Python/headless-Blender tests, opt-in corpus smoke tests,
  transactional rollback, Escape cancellation, and reproducible performance
  benchmarks.

## 1.5.3

Correctness and material fixes included in this release:

- **Shared-offset primitive inputs** — VERTEX / NORMAL / UV inputs that share the
  same COLLADA offset index are no longer overwritten; co-offset attributes
  import correctly.
- **Joint id / name / sid resolution** — skin `Name_array` entries resolve
  against node `id`, `name`, and `sid`; bones and vertex groups use the same
  final names so the Armature modifier can bind.
- **Armature axis / scale correction** — armatures receive the same up-axis,
  forward-axis, and scale correction matrix as mesh instances.
- **Specular map wiring** — specular textures link into Principled BSDF
  Specular IOR Level (or Roughness for roughness-named maps).
- **Diffuse alpha behavior** — diffuse textures no longer force `CLIP` or
  connect their alpha output; separate transparent/alpha maps still drive
  transparency.
- **Import progress** — single-file progress advances by processed scene
  instances and is clamped so material splits cannot exceed complete.
- **AO mix node** — prefers modern `ShaderNodeMix` (RGBA) with a MixRGB fallback.

## 1.5.2

- Scene material bindings, `instance_controller` import, normal/alpha maps,
  `file://` texture paths, and accessor offset/count support.
