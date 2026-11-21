# COLLADA/DAE export pipeline

The exporter writes a deterministic COLLADA 1.4.1 scene subset designed for
reliable re-import and interchange. Export runs on Blender's main thread
because it reads live Blender data.

## Pipeline summary

```text
Scene or selected objects
        |
        v
dependency and hierarchy collection
        |
        v
stable COLLADA IDs and library records
        |
        +--> Meshes --> geometry, UVs, colors, materials
        +--> Shape keys --> morph controllers
        +--> Armatures --> joints and skin controllers
        +--> Cameras and lights
        |
        v
visual-scene hierarchy and instances
        |
        v
temporary XML file --> atomic destination replacement
```

## Object selection

By default, supported objects from the complete scene are exported. Selected
Objects mode starts with the current selection and adds required object
parents and armatures referenced by selected skinned meshes.

Supported Blender object types are:

- Mesh;
- Empty;
- Armature;
- Camera;
- Light.

Unsupported object types are omitted. Disabling Export Armatures omits
Armature objects and skin controllers; meshes are still exported.

## IDs and hierarchy

All COLLADA IDs are sanitized and made unique before XML is written. Blender
object parenting becomes nested visual-scene nodes with local matrices.
Top-level object matrices receive the requested global scale and forward-axis
conversion.

Armature bones become `JOINT` nodes using rest-pose local matrices. Objects
with native Blender bone parenting are emitted below the corresponding JOINT,
preserving attachment hierarchy on re-import when Attach Empties to Bones is
enabled.

## Geometry

The exporter reads the stored Mesh datablock. It does not apply arbitrary
modifiers. This preserves source topology for vertex groups and shape keys;
Armature modifiers are represented as COLLADA skin controllers instead.

Each mesh exports:

- local vertex positions;
- loop/corner normals;
- triangulated faces grouped by material slot;
- loose edges as `<lines>`;
- every UV layer as a numbered `TEXCOORD` set;
- every point- or corner-domain color attribute as a numbered `COLOR` set.

Index streams remain separate for vertices, normals, UVs, and colors. Meshes
without faces or loose edges fail explicitly rather than producing an
unimportable success-shaped file.

## Materials and images

Principled BSDF values are translated to a `profile_COMMON` Phong effect:

- base color or a linked image texture;
- emission;
- specular level;
- roughness-derived shininess;
- metallic-derived reflectivity;
- alpha/transparency;
- index of refraction;
- a linked normal image through the FCOLLADA bump convention.

Texture references use `bind_vertex_input` and the first exported UV set.
Paths are relative to the destination DAE when possible. Cross-drive paths use
local `file:` URIs. Image bytes are not copied or embedded.

## Shape keys and morph controllers

Every non-Basis shape key becomes a geometry target with the same topology as
the base mesh. Relative shape keys export displacement coordinates with a
`RELATIVE` morph controller; absolute shape keys export target coordinates
with a `NORMALIZED` controller. Current shape-key values become initial morph
weights.

When a mesh is both morphed and skinned, the skin controller references the
morph controller, producing a geometry-to-morph-to-skin chain.

## Armatures and skinning

When Export Armatures is enabled:

1. Armature rest bones become a JOINT hierarchy.
2. Bone names are written as controller joint identifiers.
3. Inverse rest matrices become `INV_BIND_MATRIX` values.
4. Matching Blender vertex groups become arbitrary per-vertex influences.
5. Root JOINT nodes are referenced by the mesh's controller instance.

Only vertex groups whose names match exported bones are treated as skin
weights. Other vertex groups remain Blender-only data.

## Cameras and lights

Perspective and orthographic cameras export projection, clipping, and aspect
values. Sun, point, spot, and area lights map to COLLADA directional, point,
or spot lights with color and approximate intensity. Spot cone and blend
settings become falloff values.

## File writing and errors

The complete XML document is built in memory. It is written to a temporary
file in the destination directory and replaces the requested path only after
serialization succeeds. A failed export therefore leaves any existing target
file unchanged.

The public `export_dae()` facade returns an object count and an explicit error
message. The Blender operator reports the error and returns `CANCELLED`
instead of claiming success.

## Current boundaries

The exporter does not currently write:

- Blender Actions or COLLADA animation clips;
- arbitrary evaluated modifier results;
- ambient World lighting;
- physics, kinematics, formulas, or B-rep/NURBS data;
- effects outside `profile_COMMON`;
- embedded image data or ZAE archives.

These limits are deliberately narrower than import support. See
[Add-on architecture](architecture.md), [Import pipeline](import-pipeline.md),
and [Testing](testing.md) for related details.
