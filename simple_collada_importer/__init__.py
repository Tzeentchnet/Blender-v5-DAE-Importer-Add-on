# SPDX-License-Identifier: GPL-3.0-or-later
"""Simple COLLADA (.dae) Importer — Blender 5 extension.

Blender imports are intentionally lazy so the pure COLLADA parser can be
unit-tested with a normal Python interpreter.
"""


_registered_classes = ()


def menu_func_import(self, context):
    self.layout.operator(
        "import_scene.simple_collada_full",
        text="Simple COLLADA (.dae)",
    )


def menu_func_assign_textures(self, context):
    self.layout.operator(
        "object.assign_textures_by_name",
        text="Assign Textures by Name",
    )


def register():
    global _registered_classes
    import bpy

    from .file_handler import IO_FH_simple_collada
    from .operators import (
        IMPORT_OT_simple_collada_full,
        OBJECT_OT_assign_textures_by_name,
    )
    from .preferences import SimpleColladaPreferences

    _registered_classes = (
        SimpleColladaPreferences,
        IMPORT_OT_simple_collada_full,
        OBJECT_OT_assign_textures_by_name,
        IO_FH_simple_collada,
    )
    for cls in _registered_classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_object.append(menu_func_assign_textures)


def unregister():
    global _registered_classes
    import bpy

    bpy.types.VIEW3D_MT_object.remove(menu_func_assign_textures)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(_registered_classes):
        bpy.utils.unregister_class(cls)
    _registered_classes = ()
