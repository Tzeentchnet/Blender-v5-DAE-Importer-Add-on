# Changelog

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
