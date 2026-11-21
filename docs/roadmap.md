# COLLADA Importer Roadmap

Last reviewed: 2026-11-21

Status: implemented in extension version 2.0.0.

All phases below are covered by synthetic pure-Python and headless Blender
tests. The test commands are documented in the repository README. Optional
local compatibility corpora remain external to the repository so asset
redistribution rights are not assumed.

This roadmap describes the recommended order for expanding the Simple COLLADA
Importer beyond its current Blender 5 feature set. The goal is to improve
interoperability without losing the add-on's lightweight packaging, predictable
Blender integration, or NumPy-accelerated mesh path.

The current release already imports:

- COLLADA 1.4.1 and 1.5.0 namespaces
- triangle and polylist geometry
- scene transforms and geometry/controller instances
- normals, vertex colors, and one UV set
- classic materials and common texture channels
- skin controllers, armatures, inverse-bind matrices, and vertex weights
- per-file up-axis correction, configurable forward axis, and global scale

The work was implemented in the order below. The phase descriptions are
retained as the acceptance record for the importer architecture and tests.

## Implementation principles

1. Preserve existing import behavior unless a fixture demonstrates that it is
   incorrect.
2. Parse COLLADA data into testable intermediate structures before creating
   Blender data blocks.
3. Keep parsing and Blender composition separate so XML behavior can be tested
   without importing `bpy`.
4. Reject or report malformed indices and references explicitly. Do not clamp,
   silently discard, or invent success-shaped data.
5. Retain arbitrary skin influence counts by default. Compatibility limits,
   such as four influences per vertex, must be optional.
6. Keep filename-based material heuristics as fallbacks, not replacements for
   explicit COLLADA bindings.
7. Reuse permissively licensed algorithms only with their required notices.
   Prefer clean-room implementation when source-file licensing is unclear.

## Phase 0: Regression foundation (complete)

Build the test infrastructure before increasing schema coverage.

### Deliverables

- Add small synthetic `.dae` fixtures for:
  - Y_UP, Z_UP, and X_UP assets
  - non-default `<unit meter>` values
  - active visual-scene selection
  - nested and repeated node instances
  - concave polygons and malformed index streams
  - multiple UV sets and per-texture UV bindings
  - skin controllers with distinct `id`, `sid`, and `name` values
  - animation channels and clips
  - normalized and relative morph controllers
- Add pure-Python tests for XML parsing, reference resolution, accessors,
  triangulation, animation sampling, and controller decoding.
- Add headless Blender tests for generated meshes, materials, armatures,
  Actions, FCurves, shape keys, cameras, and lights.
- Test extension registration, import cancellation, error reporting, and
  multi-file imports.
- Add compatibility smoke tests using real files from several exporters when
  redistributable samples are available.

### Acceptance criteria

- Tests run from a documented command.
- Every supported feature has at least one positive fixture.
- Malformed-input fixtures verify explicit errors or warnings.
- Existing v1.5.3 behavior has a repeatable baseline.

## Phase 1: Document and scene semantics (complete)

Correct document-level behavior before adding more object types.

### Deliverables

- Apply `<asset><unit meter="...">` consistently to geometry, scene
  transforms, armatures, cameras, lights, and animation.
- Resolve the active scene through `<scene><instance_visual_scene>` instead of
  selecting the first `<visual_scene>`.
- Resolve and expand `<instance_node>` references, including cycle detection.
- Preserve node hierarchy in Blender collections or object parenting.
- Reuse Blender mesh data for repeated unmodified geometry instances.
- Resolve local URI references consistently and report unresolved references.
- Support ordered `matrix`, `translate`, `rotate`, and `scale` transforms
  through a shared transform representation.
- Add `lookat` support. Defer `skew` unless a real compatibility case requires
  it.

### Acceptance criteria

- Unit conversion is applied exactly once.
- Repeated instances share mesh data while retaining independent transforms and
  material bindings.
- Files with multiple visual scenes import only the selected scene.
- Recursive node references fail safely with a useful diagnostic.

## Phase 2: Geometry and attribute completeness (complete)

Expand mesh support after scene/reference handling is reliable.

### Deliverables

- Import `<polygons>`, `<tristrips>`, `<trifans>`, `<lines>`, and
  `<linestrips>` where Blender has a meaningful representation.
- Replace unconditional fan triangulation with a concave-safe triangulation
  path.
- Preserve all declared UV sets as Blender UV layers.
- Honor `<bind_vertex_input>` so each texture uses the intended UV set.
- Preserve vertex colors with their declared set and accessor layout.
- Parse accessors consistently across geometry and controllers, including
  `source`, `offset`, `count`, `stride`, and named parameters.
- Add optional vertex-order preservation for round-trip and shape-transfer
  workflows.
- Validate primitive counts, tuple widths, source bounds, and negative indices
  before mesh creation.

### Acceptance criteria

- Concave faces triangulate without self-intersections or missing corners.
- Multi-UV fixtures retain every layer and select the correct layer per
  texture.
- Invalid streams produce actionable diagnostics rather than partially
  corrupted meshes.

## Phase 3: Material fidelity (complete)

Translate COLLADA's classic shading models into predictable Blender Principled
materials.

### Deliverables

- Implement Constant, Lambert, Blinn, and Phong parameter handling.
- Map diffuse/base color, emission, specular color/intensity, shininess,
  reflectivity, IOR, transparency, and normal/bump inputs.
- Implement COLLADA `opaque` modes and `transparency` scalar behavior.
- Support double-sided material extras.
- Support texture UV-set selection, wrapping, mirroring, offset, rotation, and
  scale where exporters provide them.
- Preserve color-space intent: color textures use sRGB; data textures use
  Non-Color.
- Keep AO, roughness, metallic, and filename inference as documented
  compatibility extensions rather than COLLADA-standard behavior.
- Cache materials by a complete document-local signature or stable material
  identity.

### Acceptance criteria

- Explicit COLLADA values take precedence over filename heuristics.
- Alpha and transparency fixtures match expected Blender render behavior.
- Materials sharing one diffuse image but differing in other channels remain
  distinct.
- Every texture is connected to the UV layer selected by its binding.

## Phase 4: Animation and armature expansion (complete)

Animation is the largest feature milestone and should build on the tested scene
and transform model from earlier phases.

### Deliverables

- Parse nested `<animation>` elements, sources, samplers, channels, and
  `<animation_clip>` ranges.
- Resolve channel targets by node ID, SID path, matrix member, and vector
  component.
- Combine independently keyed matrix/TRS components on a shared time line.
- Support STEP, LINEAR, and BEZIER interpolation, including tangent data.
- Subsample or unwrap large axis-angle rotations where direct interpolation
  would take the wrong path.
- Create Blender Actions and FCurves for objects and pose bones.
- Preserve separate clips as separate Actions or NLA strips.
- Include animated non-deforming joints required by the skeleton hierarchy.
- Support multiple skeleton roots and controller-specific joint scopes.
- Verify bind shape, inverse bind, rest pose, parent transforms, and animated
  pose composition together.

### Acceptance criteria

- Sparse and independently keyed channels evaluate consistently.
- Object and bone animation match fixture values at keyed and interpolated
  frames.
- STEP remains constant, LINEAR remains linear, and BEZIER handles reproduce
  source tangents within tolerance.
- Multiple animation clips remain independently selectable.

## Phase 5: Morph controllers and shape keys (complete)

### Deliverables

- Parse normalized and relative morph controllers.
- Resolve morph targets through geometry and controller chains.
- Create Blender relative shape keys with stable names.
- Apply initial morph weights.
- Import animated morph weights as shape-key FCurves.
- Validate topology compatibility between the basis and every target.

### Acceptance criteria

- Normalized and relative fixtures produce the expected deformed mesh.
- Incompatible topology is reported without leaving partial shape-key data.
- Morph animation follows imported clip timing.

## Phase 6: Cameras, lights, and scene breadth (complete)

### Deliverables

- Import perspective and orthographic cameras.
- Reconstruct field of view, aspect ratio, clipping planes, and orthographic
  scale without applying conversion twice.
- Import ambient, directional, point, and spot lights.
- Map color, intensity, attenuation, falloff angle, and falloff exponent to the
  closest Blender representation.
- Preserve names, hierarchy, and transforms for non-mesh scene objects.
- Consider custom properties for unsupported metadata and `<extra>` data.

### Acceptance criteria

- Camera framing and clipping match reference scenes.
- Light type, placement, color, cone angle, and attenuation are preserved as
  closely as Blender permits.

## Phase 7: Scale and performance hardening (complete)

Optimize only after the intermediate representation and feature behavior are
covered by tests.

### Deliverables

- Replace whole-document parsing with staged `iterparse` processing where it
  reduces memory without breaking cross-library references.
- Clear processed XML elements after retaining the required indexed data.
- Keep NumPy source parsing and bulk Blender `foreach_set` uploads.
- Cache immutable source arrays, resolved references, and composed materials.
- Add import cancellation checks for long-running files.
- Add timing and peak-memory benchmarks for large meshes, many instances,
  controllers, and animation channels.

### Acceptance criteria

- Large-file peak memory is measurably lower than the v1.5.3 baseline.
- Performance changes preserve fixture output exactly.
- Cancellation leaves no partially linked collections or orphaned data blocks.

## Deferred scope

The following should remain out of scope until the phases above are complete
and real user files justify the maintenance cost:

- B-rep and NURBS surfaces
- physics scenes
- kinematics and formulas
- COLLADA FX profiles other than `profile_COMMON`
- export or round-trip guarantees
- ZAE packaging, unless requested by users with representative fixtures

## Reference implementations

No new full-stack COLLADA importer replaced the mature implementations during
2023-2026. The recommended approach is to combine the strongest parts of the
following projects.

### Assimp

- Repository: <https://github.com/assimp/assimp>
- COLLADA implementation:
  <https://github.com/assimp/assimp/tree/master/code/AssetLib/Collada>
- COLLADA tests:
  <https://github.com/assimp/assimp/blob/master/test/unit/utColladaImportExport.cpp>
- License: BSD-3-Clause-style
- Best references:
  - defensive input validation and regression cases
  - instance-node and material/UV binding resolution
  - skin and morph controller composition
  - animation target resolution and channel resampling
  - asset units, cameras, lights, and scene metadata

### Three.js ColladaLoader

- Repository: <https://github.com/mrdoob/three.js>
- Loader:
  <https://github.com/mrdoob/three.js/tree/dev/examples/jsm/loaders/collada>
- License: MIT
- Best references:
  - aggregation by animated node and SID
  - sparse-channel synthesis and union of key times
  - STEP, LINEAR, and BEZIER evaluation
  - matrix decomposition into translation, rotation, and scale tracks
  - readable camera, light, skin, and material composition

Do not copy Three.js's four-influence GPU limit as Blender's default. Blender
vertex groups can preserve all valid influences.

### Blender legacy COLLADA importer

- Source:
  <https://github.com/blender/blender/tree/v4.0.0/source/blender/io/collada>
- Removal commit:
  <https://github.com/blender/blender/commit/5ad6d42c8398dc56cac3f212043e2146a2e3119a>
- License: GPL-2.0-or-later
- Best references:
  - Blender Action and FCurve creation
  - bone/rest-pose conversion and armature linking
  - morph targets to relative shape keys
  - scene-unit handling
  - camera, light, image, and Principled-node integration

Do not restore the OpenCOLLADA dependency. Port only focused Blender-facing
algorithms and retain the original copyright and SPDX notices.

### pycollada

- Repository: <https://github.com/pycollada/pycollada>
- License: BSD-3-Clause-style
- Best references:
  - Python-native XML and resource loading
  - asset metadata and unit representation
  - geometry primitives and source accessors
  - scene graphs, node references, cameras, lights, and classic effects
  - skin and morph data structures

pycollada's animation model does not provide complete sampler/channel
evaluation. It should not be used as the animation implementation.

### dae-parser

- Repository: <https://github.com/digama0/dae-parser>
- License: MIT OR Apache-2.0; use the MIT option when adapting code
- Best references:
  - a specification-shaped COLLADA 1.4.1 model
  - required/default attributes and URI handling
  - animation, controller, scene-instance, and effect-profile coverage
  - typed schema and validation checklists

This project is a parser/writer rather than a composed scene importer. Use it
as a completeness checklist, not as a Blender integration design.

### BeamNG CDAE add-on

- Repository: <https://github.com/Grille/Blender_BeamNG_CDAE>
- License: MIT
- Best reference: modern Blender material-node construction, channel handling,
  UV selection, and Non-Color texture configuration

This is not a general XML DAE importer and should only inform the material
composition layer.

## Licensing guidance

This add-on is distributed under GPL-3.0-or-later.

- Assimp, Three.js, pycollada, dae-parser under its MIT option, and BeamNG CDAE
  use permissive licenses compatible with a GPL distribution. Preserve their
  required copyright, license, and disclaimer text when code is copied or
  translated.
- Blender's legacy importer is GPL-2.0-or-later and can be ported into this
  GPL-compatible add-on with its original attribution retained.
- OpenCOLLADA does not have one unambiguous root license covering every
  component. Copy only files with explicit compatible headers; otherwise use
  clean-room architectural ideas.
- Check the license of each external fixture independently before committing it.
  A repository's code license does not necessarily cover all sample assets.
