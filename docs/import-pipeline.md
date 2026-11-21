# COLLADA/DAE import pipeline

This document follows one `.dae` file from user input to completed Blender
data. For module ownership and dependency boundaries, see
[Add-on architecture](architecture.md).

## Pipeline summary

```text
.dae path and import options
        |
        v
XML iterparse and library decoding
        |
        v
reference validation and active-scene selection
        |
        v
ColladaDocument
        |
        v
document coordinate correction
        |
        v
scene hierarchy and instances
        |
        +--> geometry --> Mesh attributes and materials
        +--> skin/morph controllers --> Armatures and shape keys
        +--> cameras and lights
        |
        v
animation clips --> Blender Actions
        |
        v
optional material split and transaction commit
```

## 1. Input selection and options

Imports can start from File > Import, drag-and-drop, a script, or a headless
test. The operator resolves one or more `.dae` paths and passes these options
to the import facade:

- import rigs;
- split meshes by material;
- create a default material when needed;
- recalculate normals;
- preserve source vertex order;
- global scale;
- source forward axis.

Interactive imports parse in a worker thread. Background imports use the same
pipeline synchronously.

## 2. Staged XML parsing

`parse_collada()` uses `xml.etree.ElementTree.iterparse` instead of loading the
entire document tree. It recognizes the document namespace and version, then
decodes children of top-level libraries as their end events arrive. Parsed XML
elements are cleared immediately.

The parser handles these libraries:

- images, effects, and materials;
- geometries and controllers;
- library nodes and visual scenes;
- animations and animation clips;
- cameras and lights.

It also reads `<asset>` metadata and the active
`<scene><instance_visual_scene>` reference.

COLLADA 1.4.1 and 1.5.0 are explicitly supported. Other versions generate a
warning so structurally compatible files can still be attempted.

## 3. Sources, accessors, and references

Source arrays are decoded according to their accessor:

- array type;
- accessor source;
- offset;
- count;
- stride;
- named parameters.

Numeric data is held in NumPy arrays. Primitive indices remain separate from
source values until geometry decoding.

Local URI references must use `#id`. Required unresolved references, duplicate
structural IDs, malformed accessors, and invalid required scene references are
fatal. Selected exporter compatibility issues, such as an incorrect declared
array count when accessor bounds remain usable, are retained as warnings.
External document references are not followed.

After all libraries are parsed, the parser validates cross-library references
and confirms that the requested visual scene exists. The result is a
`ColladaDocument`; no Blender data has been created yet.

## 4. Coordinate conversion

The scene builder calculates one correction matrix from:

1. the document's `X_UP`, `Y_UP`, or `Z_UP` declaration;
2. the document unit's meter value;
3. the user's global scale;
4. the selected forward-axis mapping to Blender's `-Y` forward direction.

The correction is applied at root scene nodes so child-local transforms and
object parenting remain meaningful. Animated root nodes reapply the same
correction when their keyed matrices are evaluated.

COLLADA transforms are composed in document order. Matrix, translation,
rotation, scale, and look-at transforms all feed the same matrix path.

## 5. Active scene and hierarchy

Only the visual scene referenced by the top-level `<scene>` element is built.
The builder first indexes its nodes for parent/world relationships and
animation target resolution.

Each COLLADA node becomes a Blender Empty. This preserves:

- node names and parent-child relationships;
- nodes that do not directly instantiate geometry;
- transform scopes used by cameras, lights, skeletons, and animation;
- COLLADA IDs, SIDs, and simple extras as custom properties.

Geometry, controller, camera, and light objects are parented below the
corresponding Empty with identity local transforms.

`instance_node` recursively expands nodes from `library_nodes`. Repeated
instances create separate object hierarchies while cached geometry can still
share Mesh data. Recursive instance chains are rejected with an
`INSTANCE_NODE_CYCLE` error.

## 6. Geometry decoding

Geometry is decoded when first instantiated and then cached by geometry ID.
The decoder expands `<vertices>` bindings and validates each primitive's
interleaved index tuples.

Supported primitives include:

- triangles;
- polylist;
- polygons, including holes;
- triangle strips and fans;
- lines and line strips.

Surface primitives become triangle index arrays. Concave polygons use
projected ear clipping, and polygon holes are bridged before triangulation.
Line primitives become Blender mesh edges. Negative indices, out-of-range
indices, inconsistent tuple widths, missing positions, and malformed counts
produce explicit diagnostics.

The decoded result contains:

- source positions;
- triangles and edges;
- one material symbol per face;
- normals, every UV set, and every color set as corner attributes;
- source position indices.

Mesh creation uses bulk Blender/NumPy transfer where practical. If Preserve
Vertex Order is enabled, a `collada_source_index` point attribute records the
source position index.

Repeated plain geometry instances share a cached Blender Mesh. Controller
state or later topology-changing operations receive a separate mesh when
necessary.

## 7. Materials and textures

An instance binds primitive material symbols to COLLADA materials. The material
points to a `profile_COMMON` effect using the constant, Lambert, Blinn, or
Phong model. The builder translates classic channels to a Principled BSDF
network, including:

- emission, ambient, diffuse, and specular values;
- shininess, reflectivity, transparency, and index of refraction;
- diffuse, alpha, normal/bump, AO, roughness, metallic, and specular textures;
- double-sided metadata;
- texture offset, scale, rotation, wrap, and mirror settings.

`bind_vertex_input` selects the correct imported UV set for each texture.
Images used in both color and data roles are copied when necessary so sRGB and
Non-Color settings do not conflict.

Only local paths and file URIs are resolved, relative to the DAE file when
appropriate. Network resources are never fetched. Missing textures generate
warnings rather than replacing the whole import with a success-shaped
fallback.

## 8. Skin and morph controllers

Controller chains are resolved until they reach their base geometry.

For a skin controller, the builder:

1. applies the bind-shape matrix to the mesh where required;
2. resolves joints against scene nodes using ID, SID, or name and the declared
   skeleton scopes;
3. creates an armature and edit-bone hierarchy;
4. applies inverse bind information;
5. creates vertex groups and assigns all declared influences;
6. adds an Armature modifier to the mesh object.

Influences are grouped by joint and assigned intentionally rather than summed
as accidental duplicate writes.

Two default-off compatibility options alter armature composition:

- **Attach Empties to Bones** converts non-joint nodes directly below a joint
  to native Blender bone parenting after all armatures are available.
- **Construct Armature from Bind Pose** uses inverse-bind matrices for bone
  orientation and derives display lengths from child spacing. Otherwise,
  scene-joint transforms and fixed display lengths are used.

For a morph controller, the basis geometry becomes the mesh Basis shape. Each
target geometry becomes a shape key. `NORMALIZED` and `RELATIVE` morph methods
are represented, and controller weights are available to the animation stage.

## 9. Cameras and lights

Perspective and orthographic cameras become Blender Camera data with projection
and clipping values mapped where COLLADA provides them.

Directional, point, and spot lights become Blender lights. Color, attenuation,
falloff, and relevant extras are mapped to native settings or custom
properties. Ambient lights contribute to the World background while retaining
their scene node and metadata.

## 10. Animation and clips

Nested animation libraries are flattened without losing the relationship
needed by animation clips. Samplers resolve INPUT, OUTPUT, INTERPOLATION, and
optional tangent sources.

Channel targets can address:

- node IDs and SID paths;
- whole transforms;
- vector members such as `.X`;
- matrix members;
- morph-weight array entries.

STEP, LINEAR, and BEZIER source interpolation is sampled by the pure animation
layer. Rotation angles are unwrapped before transform evaluation to avoid
unwanted full-turn discontinuities.

For each clip, sampled time in seconds is converted to Blender frames using
the scene frame rate. The builder creates separate Actions for:

- animated scene objects;
- pose bones, grouped by armature;
- morph shape-key weights.

If a file defines animation without clips, a synthetic default clip groups the
available channels. The first clip is activated; additional clips remain
available as separate Actions with their COLLADA clip metadata.

## 11. Finalization

After the scene has been built:

- optional normal recalculation is applied;
- optional material splitting creates one object per used material;
- shared mesh data is copied before any topology-changing split;
- diagnostics are reported to the operator;
- the per-file transaction is committed.

The public facade returns the imported data-object count, the last created
armature when present, and an error string when the file failed.

## Progress, cancellation, and failure behavior

Build progress is based on geometry, controller, camera, and light instances.
Multi-file progress advances once per completed file.

Escape during an interactive import sets a shared cancellation event. Parsing
checks it periodically while consuming XML; composition checks it between
nodes and instances. Cancellation is treated as a failed transaction for the
active file.

Any fatal parse, validation, composition, or cancellation error rolls back
data created for that file. Already committed files from an earlier multi-file
batch remain imported.

## Deliberate boundaries

The importer does not currently attempt B-rep/NURBS surfaces, physics,
kinematics/formulas, non-`profile_COMMON` effects, ZAE archives, or external
COLLADA document references. The separate exporter intentionally targets a
documented scene subset rather than lossless round-trip guarantees.

See the [Roadmap](roadmap.md) for the implemented acceptance criteria and
[Export pipeline](export-pipeline.md) for writing DAE files. See
[Testing](testing.md) for pure, headless Blender, corpus, and benchmark workflows.
