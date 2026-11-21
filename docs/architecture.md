# Add-on architecture

## Overview

The add-on is divided into two primary layers:

1. A Blender-independent COLLADA layer parses XML, validates references and
   indices, and produces a typed intermediate representation.
2. A Blender composition layer converts that representation into Blender data
   blocks on Blender's main thread.

```text
File menu / drag-and-drop / script
                 |
                 v
        Blender import operator
                 |
                 v
      COLLADA parser and validators
       (no bpy or Blender context)
                 |
                 v
          ColladaDocument
                 |
                 v
    Import transaction + scene builder
                 |
                 v
 Blender objects, data blocks, and Actions
```

This boundary keeps XML and COLLADA algorithms testable with a normal Python
interpreter. It also prevents worker threads from touching Blender's API.

## Design rules

- **Parsing is Blender-independent.** Modules under
  [`simple_collada_importer/collada`](../simple_collada_importer/collada) use
  Python, NumPy, and the standard library, but not `bpy`.
- **Blender data is created on the main thread.** Interactive imports may parse
  in a worker, but scene composition always runs in the operator's main-thread
  modal callback.
- **One file is one transaction.** If composition fails or is cancelled, data
  blocks created for that file are removed and pre-existing World state is
  restored.
- **References are IDs in the intermediate model.** Parsing does not retain
  live XML elements or Blender objects.
- **Recoverable compatibility problems are warnings.** Invalid structure,
  unsafe indices, broken required references, and unsupported external
  document references are structured fatal errors.
- **Coordinate conversion is centralized.** Unit, up-axis, user scale, and
  forward-axis conversion share one document correction matrix.

## Module map

| Module | Responsibility |
| --- | --- |
| [`__init__.py`](../simple_collada_importer/__init__.py) | Registers Blender classes and menus. Blender imports are lazy so importing the pure COLLADA package outside Blender remains possible. |
| [`file_handler.py`](../simple_collada_importer/file_handler.py) | Registers `.dae` drag-and-drop with Blender. |
| [`operators.py`](../simple_collada_importer/operators.py) | Owns the file dialog, import options, multi-file orchestration, progress reporting, worker-thread parsing, modal cancellation, and user-facing reports. |
| [`preferences.py`](../simple_collada_importer/preferences.py) | Stores default values for the import operator. |
| [`importer.py`](../simple_collada_importer/importer.py) | Public parse/build facade, legacy tuple result, optional material splitting, and transaction boundary. |
| [`blender_builder.py`](../simple_collada_importer/blender_builder.py) | Converts a `ColladaDocument` into Blender hierarchy, meshes, materials, armatures, shape keys, cameras, lights, and Actions. |
| [`collada/model.py`](../simple_collada_importer/collada/model.py) | Dataclasses for the Blender-independent document and decoded data. |
| [`collada/parser.py`](../simple_collada_importer/collada/parser.py) | Staged XML parsing for COLLADA libraries, scenes, controllers, animation, cameras, and lights. |
| [`collada/accessors.py`](../simple_collada_importer/collada/accessors.py) | Decodes source arrays and accessor offset/count/stride/parameter layouts. |
| [`collada/geometry.py`](../simple_collada_importer/collada/geometry.py) | Validates primitive tuples, triangulates surfaces, decodes lines, and creates Blender-ready NumPy arrays. |
| [`collada/transforms.py`](../simple_collada_importer/collada/transforms.py) | Composes ordered transforms and calculates document-to-Blender coordinate conversion. |
| [`collada/animation.py`](../simple_collada_importer/collada/animation.py) | Resolves animation samplers and targets, samples interpolation, and updates transform components. |
| [`collada/errors.py`](../simple_collada_importer/collada/errors.py) | Defines stable diagnostics, fatal COLLADA errors, and cancellation. |

## Intermediate representation

`ColladaDocument` is the hand-off between parsing and composition. It contains
the active-scene reference and dictionaries for:

- images, effects, and materials;
- geometries and controllers;
- library nodes and visual scenes;
- animations and animation clips;
- cameras and lights;
- document asset metadata and diagnostics.

Scene nodes retain ordered COLLADA transforms and typed instances. Geometry
sources retain their accessor layout and NumPy data. Controllers retain
resolved skin weights or morph targets without creating any Blender state.

Two smaller result models are created later:

- `DecodedGeometry` contains vertices, triangles, edges, per-face material
  symbols, corner attributes, and source vertex indices.
- `SampledChannel` contains animation times, values, interpolation modes, and
  optional tangents.

## Execution modes

### Interactive import

The Blender operator submits `parse_collada()` to a single worker thread.
Blender remains responsive while the XML is parsed. A timer-driven modal
handler receives the completed `ColladaDocument` and runs composition on the
main thread. Escape sets a cancellation event used by both stages.

### Headless or scripted import

When Blender runs in background mode, or no window is available, parsing and
composition run synchronously. This makes command-line behavior deterministic
and suitable for automated tests.

### Multi-file import

Files are processed independently. Each file receives its own collection when
more than one file is selected, and a failure in one file does not roll back a
previously completed file.

## Blender composition

`BlenderSceneBuilder` first indexes the active visual scene for hierarchy and
animation target lookup. It then recursively creates an Empty for every
COLLADA node and attaches instantiated data objects below that Empty.

The builder maintains per-file caches for:

- decoded geometry;
- Blender Mesh data;
- generated materials;
- loaded images and their color-space roles.

Repeated geometry instances can therefore share one Mesh. Operations that
change topology, such as splitting by material, copy shared data first.

The builder returns a `BuildResult` containing created objects, armatures,
Actions, and accumulated warnings. The facade commits the transaction only
after scene composition and requested post-processing complete.

## Diagnostics, cancellation, and rollback

Diagnostics use a stable code, message, severity, and optional context. The
parser accumulates recoverable warnings in the document. The operator displays
a bounded warning preview rather than flooding Blender's report area.

Cancellation is cooperative:

- the parser checks the cancellation callback during `iterparse`;
- the scene builder checks before nodes and instances;
- multi-file imports stop before starting the next file.

`ImportTransaction` snapshots the relevant Blender data-block collections
before composition. On failure it unlinks and removes newly created objects,
collections, meshes, armatures, materials, cameras, lights, Actions, images,
and Worlds. It also restores the scene's original World and background color.

## Extending the importer

For a new COLLADA feature:

1. Add or extend a dataclass in
   [`collada/model.py`](../simple_collada_importer/collada/model.py).
2. Parse and validate the XML in
   [`collada/parser.py`](../simple_collada_importer/collada/parser.py) or a
   focused pure helper.
3. Add pure decoding, transform, or sampling logic under
   [`collada`](../simple_collada_importer/collada).
4. Add the Blender mapping in
   [`blender_builder.py`](../simple_collada_importer/blender_builder.py).
5. Add a small redistributable fixture and pure tests; add a headless Blender
   assertion when Blender data is affected.

See [Testing](testing.md) for the available validation workflows and
[COLLADA/DAE import pipeline](import-pipeline.md) for the detailed runtime
sequence.
