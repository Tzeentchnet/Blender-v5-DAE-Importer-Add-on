# SPDX-License-Identifier: GPL-3.0-or-later
"""Public import facade for COLLADA parsing and Blender composition."""

import logging
import os

import bmesh
import bpy
import numpy as np

from .blender_builder import (
    BlenderSceneBuilder,
    ImportOptions,
    ImportTransaction,
)
from .collada import ColladaError, parse_collada
from .collada.errors import ColladaCancelled


LOGGER = logging.getLogger(__name__)


def split_object_by_material(context, obj):
    """Split a mesh into material objects while preserving attached data."""
    if obj is None or obj.type != "MESH" or len(obj.data.polygons) == 0:
        return [obj] if obj is not None else []
    material_indices = np.empty(len(obj.data.polygons), dtype=np.int32)
    obj.data.polygons.foreach_get("material_index", material_indices)
    if len(np.unique(material_indices)) < 2:
        return [obj]

    base_name = obj.name
    before = set(bpy.data.objects)
    try:
        with context.temp_override(
            active_object=obj,
            selected_objects=[obj],
            selected_editable_objects=[obj],
            object=obj,
            edit_object=obj,
        ):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.separate(type="MATERIAL")
            bpy.ops.object.mode_set(mode="OBJECT")
    except (RuntimeError, TypeError) as exc:
        LOGGER.error("Could not split %s by material: %s", base_name, exc)
        if obj.mode != "OBJECT":
            with context.temp_override(active_object=obj, object=obj):
                bpy.ops.object.mode_set(mode="OBJECT")
        return [obj]

    pieces = [obj, *(item for item in bpy.data.objects if item not in before)]
    for piece in pieces:
        materials = [material for material in piece.data.materials if material]
        if materials:
            name = f"{base_name}__{materials[0].name}"
            piece.name = name
            piece.data.name = name
    return pieces


def _recalculate_outside(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    if mesh.polygons:
        mesh.polygons.foreach_set(
            "use_smooth",
            np.ones(len(mesh.polygons), dtype=bool),
        )
    mesh.update()


def import_dae(
    filepath,
    context,
    import_rig=True,
    global_scale=1.0,
    forward_axis="-Y",
    split_by_material=True,
    use_default_material=False,
    recalculate_normals=False,
    preserve_vertex_order=False,
    target_collection=None,
    wm=None,
    cancel=None,
    warning_callback=None,
):
    """Import one DAE file and retain the legacy tuple return contract.

    Returns ``(imported_count, last_armature_or_None, error_message_or_None)``.
    """
    if not os.path.isfile(filepath):
        return 0, None, f"File not found: {filepath}"

    try:
        document = parse_collada(filepath, cancel=cancel)
    except (ColladaError, ColladaCancelled, OSError) as exc:
        return 0, None, str(exc)
    return build_collada_document(
        document=document,
        filepath=filepath,
        context=context,
        import_rig=import_rig,
        global_scale=global_scale,
        forward_axis=forward_axis,
        split_by_material=split_by_material,
        use_default_material=use_default_material,
        recalculate_normals=recalculate_normals,
        preserve_vertex_order=preserve_vertex_order,
        target_collection=target_collection,
        wm=wm,
        cancel=cancel,
        warning_callback=warning_callback,
    )


def build_collada_document(
    document,
    filepath,
    context,
    import_rig=True,
    global_scale=1.0,
    forward_axis="-Y",
    split_by_material=True,
    use_default_material=False,
    recalculate_normals=False,
    preserve_vertex_order=False,
    target_collection=None,
    wm=None,
    cancel=None,
    warning_callback=None,
):
    """Compose an already parsed document on Blender's main thread."""

    if target_collection is not None:
        collection = target_collection
    elif context.view_layer.active_layer_collection is not None:
        collection = context.view_layer.active_layer_collection.collection
    else:
        collection = context.scene.collection

    def progress(value):
        if wm is not None:
            wm.progress_update(value)

    options = ImportOptions(
        import_rig=import_rig,
        global_scale=global_scale,
        forward_axis=forward_axis,
        split_by_material=split_by_material,
        use_default_material=use_default_material,
        recalculate_normals=recalculate_normals,
        preserve_vertex_order=preserve_vertex_order,
    )
    transaction = ImportTransaction()
    try:
        builder = BlenderSceneBuilder(
            document=document,
            filepath=filepath,
            context=context,
            collection=collection,
            options=options,
            cancel=cancel,
            progress=progress,
        )
        result = builder.build()
        if warning_callback is not None:
            for warning in result.warnings:
                warning_callback(warning)
        else:
            for warning in result.warnings:
                LOGGER.warning("%s", warning)

        imported_objects = [
            obj
            for obj in result.objects
            if obj.type not in {"EMPTY", "ARMATURE"}
        ]
        if split_by_material:
            split_objects = []
            for obj in imported_objects:
                if obj.type == "MESH" and obj.data.users > 1:
                    obj.data = obj.data.copy()
                split_objects.extend(split_object_by_material(context, obj))
            imported_objects = split_objects

        transaction.commit()
        armature = result.armatures[-1] if result.armatures else None
        return len(imported_objects), armature, None
    except (ColladaError, ColladaCancelled) as exc:
        transaction.rollback()
        LOGGER.error("COLLADA import failed for %s: %s", filepath, exc)
        return 0, None, str(exc)
    except (RuntimeError, ValueError) as exc:
        transaction.rollback()
        LOGGER.exception("Blender composition failed for %s", filepath)
        return 0, None, str(exc)
    except Exception as exc:
        transaction.rollback()
        LOGGER.exception("Unexpected COLLADA import failure for %s", filepath)
        return 0, None, f"Unexpected import failure: {exc}"
