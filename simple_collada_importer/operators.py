# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators for the Simple COLLADA extension."""

import os
from concurrent.futures import ThreadPoolExecutor
import threading

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    StringProperty,
)
from bpy.types import Operator, OperatorFileListElement
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .collada import parse_collada
from .collada.errors import ColladaCancelled, ColladaError
from .exporter import export_dae
from .importer import build_collada_document, import_dae
from .preferences import get_prefs


class IMPORT_OT_simple_collada_full(Operator, ImportHelper):
    """Import a COLLADA (.dae) mesh with full features"""

    bl_idname = "import_scene.simple_collada_full"
    bl_label = "Import Simple COLLADA (.dae)"
    bl_options = {"REGISTER", "UNDO", "PRESET"}
    filename_ext = ".dae"

    filter_glob: StringProperty(default="*.dae", options={"HIDDEN"})

    # Multi-file + drag-and-drop support
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})
    files: CollectionProperty(
        type=OperatorFileListElement,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    import_rig: BoolProperty(
        name="Import Rig",
        description="Import armature and skin weights if present in the DAE file",
        default=True,
    )

    split_by_material: BoolProperty(
        name="Split by Material",
        description=(
            "Split each imported geometry into one object per material so each "
            "piece appears separately in the Outliner"
        ),
        default=False,
    )

    use_default_material: BoolProperty(
        name="Use Blender Default Material",
        description=(
            "Ignore the DAE's diffuse/specular colors and textures and assign "
            "each material Blender's stock Principled BSDF defaults. Useful "
            "when a DAE has no usable materials or textures, to avoid "
            "splotchy chrome-like viewport shading"
        ),
        default=False,
    )

    recalculate_normals: BoolProperty(
        name="Recalculate Normals (Outside)",
        description=(
            "Discard any per-corner normals from the DAE and recompute "
            "consistent outward-facing normals after import. Fixes DAE files "
            "with inconsistent face winding that show up as dark / flipped "
            "patches on the model"
        ),
        default=True,
    )

    preserve_vertex_order: BoolProperty(
        name="Preserve Vertex Order",
        description=(
            "Keep source position indices and unreferenced vertices unchanged "
            "for shape-transfer and round-trip workflows"
        ),
        default=False,
    )

    parent_empties_to_bones: BoolProperty(
        name="Attach Empties to Bones",
        description=(
            "Convert non-joint nodes directly below COLLADA joints to native "
            "Blender bone parenting"
        ),
        default=False,
    )

    use_bind_pose_armature: BoolProperty(
        name="Construct Armature from Bind Pose",
        description=(
            "Build bone orientation and display lengths from inverse bind "
            "matrices instead of scene joint transforms"
        ),
        default=False,
    )

    global_scale: FloatProperty(
        name="Scale",
        description="Uniform scale applied to imported geometry and transforms",
        default=1.0,
        min=1e-6,
        soft_min=0.001,
        soft_max=1000.0,
    )

    forward_axis: EnumProperty(
        name="Forward",
        description="Which DAE axis (after up-axis correction) maps to Blender's forward (-Y)",
        items=(
            ("-Y", "-Y Forward", ""),
            ("Y", "Y Forward", ""),
            ("-X", "-X Forward", ""),
            ("X", "X Forward", ""),
            ("-Z", "-Z Forward", ""),
            ("Z", "Z Forward", ""),
        ),
        default="-Y",
    )

    def invoke(self, context, event):
        # Seed defaults from add-on preferences so the user's chosen defaults
        # appear pre-selected in both the file dialog and drag-and-drop path.
        prefs = get_prefs(context)
        if prefs is not None:
            if not self.properties.is_property_set("import_rig"):
                self.import_rig = prefs.default_import_rig
            if not self.properties.is_property_set("split_by_material"):
                self.split_by_material = prefs.default_split_by_material
            if not self.properties.is_property_set("use_default_material"):
                self.use_default_material = prefs.default_use_default_material
            if not self.properties.is_property_set("recalculate_normals"):
                self.recalculate_normals = prefs.default_recalculate_normals
            if not self.properties.is_property_set("preserve_vertex_order"):
                self.preserve_vertex_order = prefs.default_preserve_vertex_order
            if not self.properties.is_property_set("parent_empties_to_bones"):
                self.parent_empties_to_bones = (
                    prefs.default_parent_empties_to_bones
                )
            if not self.properties.is_property_set("use_bind_pose_armature"):
                self.use_bind_pose_armature = (
                    prefs.default_use_bind_pose_armature
                )
            if not self.properties.is_property_set("global_scale"):
                self.global_scale = prefs.default_global_scale
            if not self.properties.is_property_set("forward_axis"):
                self.forward_axis = prefs.default_forward_axis

        # Drag-and-drop populates `files`/`directory` directly; skip the dialog.
        if self.files:
            return self.execute(context)
        return super().invoke(context, event)

    def execute(self, context):
        paths = self._selected_paths()
        if not paths:
            self.report({"ERROR"}, "No file given")
            return {"CANCELLED"}
        if bpy.app.background or context.window is None:
            return self._execute_synchronously(context, paths)
        return self._begin_modal_import(context, paths)

    def _selected_paths(self):
        if self.files:
            return [
                os.path.join(self.directory, f.name)
                for f in self.files
                if f.name.lower().endswith(".dae")
            ]
        return [self.filepath] if self.filepath else []

    def _execute_synchronously(self, context, paths):

        total_imported = 0
        last_arm = None
        errors = []
        warnings = []

        # When importing more than one file, isolate each file's objects in
        # its own collection so the Outliner shows them as distinct groups.
        per_file_collections = len(paths) > 1
        scene_collection = context.scene.collection

        wm = context.window_manager
        wm.progress_begin(0, max(len(paths), 1))
        try:
            for i, path in enumerate(paths):
                wm.progress_update(i)
                target_coll = None
                if per_file_collections:
                    coll_name = os.path.splitext(os.path.basename(path))[0]
                    target_coll = bpy.data.collections.new(coll_name)
                    scene_collection.children.link(target_coll)
                count, arm_obj, err = import_dae(
                    path,
                    context,
                    import_rig=self.import_rig,
                    global_scale=self.global_scale,
                    forward_axis=self.forward_axis,
                    split_by_material=self.split_by_material,
                    use_default_material=self.use_default_material,
                    recalculate_normals=self.recalculate_normals,
                    preserve_vertex_order=self.preserve_vertex_order,
                    parent_empties_to_bones=self.parent_empties_to_bones,
                    use_bind_pose_armature=self.use_bind_pose_armature,
                    target_collection=target_coll,
                    wm=wm if len(paths) == 1 else None,
                    warning_callback=warnings.append,
                )
                if err:
                    errors.append(f"{os.path.basename(path)}: {err}")
                total_imported += count
                if arm_obj is not None:
                    last_arm = arm_obj
                # Drop empty per-file collection if nothing was imported.
                if (
                    target_coll is not None
                    and not target_coll.objects
                    and not target_coll.children
                ):
                    scene_collection.children.unlink(target_coll)
                    bpy.data.collections.remove(target_coll)
                wm.progress_update(i + 1)
        finally:
            wm.progress_end()

        if total_imported == 0:
            msg = "; ".join(errors) if errors else "No objects imported"
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        rig_msg = f" + armature ({last_arm.name})" if last_arm else ""
        file_msg = f" from {len(paths)} file(s)" if len(paths) > 1 else ""
        self.report(
            {"INFO"},
            f"Imported {total_imported} object(s){rig_msg}{file_msg}.",
        )
        if errors:
            self.report({"WARNING"}, " | ".join(errors))
        if warnings:
            preview = " | ".join(warnings[:3])
            if len(warnings) > 3:
                preview += f" | ... and {len(warnings) - 3} more warning(s)"
            self.report({"WARNING"}, preview)
        return {"FINISHED"}

    def _begin_modal_import(self, context, paths):
        self._modal_paths = paths
        self._modal_index = 0
        self._modal_total_imported = 0
        self._modal_last_armature = None
        self._modal_errors = []
        self._modal_warnings = []
        self._modal_cancelled = False
        self._cancel_event = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="collada-parser",
        )
        self._future = None
        wm = context.window_manager
        wm.progress_begin(0, max(len(paths), 1))
        self._timer = wm.event_timer_add(0.1, window=context.window)
        wm.modal_handler_add(self)
        self._start_next_parse()
        return {"RUNNING_MODAL"}

    def _start_next_parse(self):
        path = self._modal_paths[self._modal_index]
        self._future = self._executor.submit(
            parse_collada,
            path,
            self._cancel_event.is_set,
        )

    def modal(self, context, event):
        if event.type == "ESC":
            self._modal_cancelled = True
            self._cancel_event.set()
            if self._future is not None:
                self._future.cancel()
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if self._future is None or not self._future.done():
            return {"RUNNING_MODAL"}
        if self._modal_cancelled:
            return self._finish_modal(context, cancelled=True)

        path = self._modal_paths[self._modal_index]
        try:
            document = self._future.result()
        except (ColladaError, ColladaCancelled, OSError) as exc:
            self._modal_errors.append(f"{os.path.basename(path)}: {exc}")
            document = None
        except Exception as exc:
            self._modal_errors.append(
                f"{os.path.basename(path)}: Unexpected parse failure: {exc}"
            )
            document = None

        if document is not None:
            target_collection = self._new_file_collection(context, path)
            count, armature, error = build_collada_document(
                document=document,
                filepath=path,
                context=context,
                import_rig=self.import_rig,
                global_scale=self.global_scale,
                forward_axis=self.forward_axis,
                split_by_material=self.split_by_material,
                use_default_material=self.use_default_material,
                recalculate_normals=self.recalculate_normals,
                preserve_vertex_order=self.preserve_vertex_order,
                parent_empties_to_bones=self.parent_empties_to_bones,
                use_bind_pose_armature=self.use_bind_pose_armature,
                target_collection=target_collection,
                cancel=self._cancel_event.is_set,
                warning_callback=self._modal_warnings.append,
            )
            if error:
                self._modal_errors.append(f"{os.path.basename(path)}: {error}")
            self._modal_total_imported += count
            if armature is not None:
                self._modal_last_armature = armature
            self._remove_empty_file_collection(context, target_collection)

        self._modal_index += 1
        context.window_manager.progress_update(self._modal_index)
        if self._modal_index >= len(self._modal_paths):
            return self._finish_modal(context, cancelled=False)
        self._start_next_parse()
        return {"RUNNING_MODAL"}

    def _new_file_collection(self, context, path):
        if len(self._modal_paths) <= 1:
            return None
        name = os.path.splitext(os.path.basename(path))[0]
        collection = bpy.data.collections.new(name)
        context.scene.collection.children.link(collection)
        return collection

    @staticmethod
    def _remove_empty_file_collection(context, collection):
        if collection is None or collection.objects or collection.children:
            return
        if collection.name in context.scene.collection.children:
            context.scene.collection.children.unlink(collection)
        bpy.data.collections.remove(collection)

    def _finish_modal(self, context, cancelled):
        wm = context.window_manager
        if getattr(self, "_timer", None) is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        self._cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._future = None
        if cancelled:
            self.report(
                {"WARNING"},
                f"Import cancelled after {self._modal_index} completed file(s)",
            )
            return {"CANCELLED"}
        if self._modal_total_imported == 0:
            message = (
                "; ".join(self._modal_errors)
                if self._modal_errors
                else "No objects imported"
            )
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        rig_message = (
            f" + armature ({self._modal_last_armature.name})"
            if self._modal_last_armature
            else ""
        )
        file_message = (
            f" from {len(self._modal_paths)} file(s)"
            if len(self._modal_paths) > 1
            else ""
        )
        self.report(
            {"INFO"},
            f"Imported {self._modal_total_imported} object(s)"
            f"{rig_message}{file_message}.",
        )
        messages = [*self._modal_errors, *self._modal_warnings]
        if messages:
            preview = " | ".join(messages[:3])
            if len(messages) > 3:
                preview += f" | ... and {len(messages) - 3} more warning(s)"
            self.report({"WARNING"}, preview)
        return {"FINISHED"}

    def cancel(self, context):
        if hasattr(self, "_cancel_event"):
            self._modal_cancelled = True
            self._cancel_event.set()

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "import_rig")
        layout.prop(self, "split_by_material")
        layout.prop(self, "use_default_material")
        layout.prop(self, "recalculate_normals")
        layout.prop(self, "preserve_vertex_order")
        layout.prop(self, "parent_empties_to_bones")
        layout.prop(self, "use_bind_pose_armature")
        layout.prop(self, "global_scale")
        layout.prop(self, "forward_axis")


class EXPORT_OT_simple_collada(Operator, ExportHelper):
    """Export supported Blender scene data to COLLADA 1.4.1."""

    bl_idname = "export_scene.simple_collada"
    bl_label = "Export Simple COLLADA (.dae)"
    bl_options = {"REGISTER", "PRESET"}
    filename_ext = ".dae"

    filter_glob: StringProperty(default="*.dae", options={"HIDDEN"})

    selected_only: BoolProperty(
        name="Selected Objects",
        description=(
            "Export selected supported objects plus required parents and armatures"
        ),
        default=False,
    )

    export_rig: BoolProperty(
        name="Export Armatures",
        description="Export armatures, joints, skin controllers, and vertex weights",
        default=True,
    )

    export_textures: BoolProperty(
        name="Reference Textures",
        description="Write local image paths referenced by exported materials",
        default=True,
    )

    global_scale: FloatProperty(
        name="Scale",
        description="Uniform scale applied to exported root transforms",
        default=1.0,
        min=1e-6,
        soft_min=0.001,
        soft_max=1000.0,
    )

    forward_axis: EnumProperty(
        name="Forward",
        description="Axis represented as forward in the exported DAE",
        items=(
            ("-Y", "-Y Forward", ""),
            ("Y", "Y Forward", ""),
            ("-X", "-X Forward", ""),
            ("X", "X Forward", ""),
            ("-Z", "-Z Forward", ""),
            ("Z", "Z Forward", ""),
        ),
        default="-Y",
    )

    def execute(self, context):
        count, error = export_dae(
            self.filepath,
            context,
            selected_only=self.selected_only,
            export_rig=self.export_rig,
            export_textures=self.export_textures,
            global_scale=self.global_scale,
            forward_axis=self.forward_axis,
        )
        if error is not None:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Exported {count} object(s) to {os.path.basename(self.filepath)}",
        )
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "selected_only")
        layout.prop(self, "export_rig")
        layout.prop(self, "export_textures")
        layout.prop(self, "global_scale")
        layout.prop(self, "forward_axis")


class OBJECT_OT_assign_textures_by_name(Operator):
    """Assign textures based on material names matching image file names"""

    bl_idname = "object.assign_textures_by_name"
    bl_label = "Assign Textures by Name"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(
        name="Texture Folder",
        description="Folder containing texture images",
        subtype="DIR_PATH",
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        folder = bpy.path.abspath(self.directory)
        if not os.path.isdir(folder):
            self.report({"ERROR"}, f"Not a directory: {folder}")
            return {"CANCELLED"}

        exts = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds"}
        images = {}
        for f in os.listdir(folder):
            name, ext = os.path.splitext(f)
            if ext.lower() in exts:
                full = os.path.join(folder, f)
                try:
                    img = bpy.data.images.load(full, check_existing=True)
                    images[name] = img
                except Exception:
                    pass

        assigned = 0
        for obj in context.selected_objects:
            if not hasattr(obj.data, "materials"):
                continue
            for mat in obj.data.materials:
                if not mat or str(mat.name).strip() not in images:
                    continue
                img = images[str(mat.name).strip()]
                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                while nodes:
                    nodes.remove(nodes[0])
                out_n = nodes.new("ShaderNodeOutputMaterial")
                out_n.location = (300, 0)
                bsdf_n = nodes.new("ShaderNodeBsdfPrincipled")
                bsdf_n.location = (0, 0)
                img_n = nodes.new("ShaderNodeTexImage")
                img_n.location = (-300, 0)
                img_n.image = img
                links.new(img_n.outputs["Color"], bsdf_n.inputs["Base Color"])
                links.new(bsdf_n.outputs["BSDF"], out_n.inputs["Surface"])
                assigned += 1

        self.report({"INFO"}, f"Assigned textures to {assigned} materials.")
        return {"FINISHED"}
