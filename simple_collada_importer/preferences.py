# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-on preferences: default values for the Simple COLLADA importer."""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty
from bpy.types import AddonPreferences


FORWARD_AXIS_ITEMS = (
    ("-Y", "-Y Forward", ""),
    ("Y", "Y Forward", ""),
    ("-X", "-X Forward", ""),
    ("X", "X Forward", ""),
    ("-Z", "-Z Forward", ""),
    ("Z", "Z Forward", ""),
)


class SimpleColladaPreferences(AddonPreferences):
    """Preferences shown in Edit -> Preferences -> Add-ons -> Simple COLLADA."""

    bl_idname = __package__

    default_import_rig: BoolProperty(
        name="Import Rig",
        description="Default state of the 'Import Rig' option in the import dialog",
        default=True,
    )

    default_split_by_material: BoolProperty(
        name="Split by Material",
        description=(
            "Default state of the 'Split by Material' option. When enabled, "
            "imported geometry is separated into one object per material so "
            "each piece is visible in the Outliner"
        ),
        default=False,
    )

    default_use_default_material: BoolProperty(
        name="Use Blender Default Material",
        description=(
            "Default state of the 'Use Blender Default Material' option. When "
            "enabled, imported materials use Blender's stock Principled BSDF "
            "defaults instead of the DAE's diffuse/specular colors and "
            "textures. Useful when a DAE has no usable materials or textures, "
            "to avoid splotchy chrome-like viewport shading"
        ),
        default=False,
    )

    default_recalculate_normals: BoolProperty(
        name="Recalculate Normals (Outside)",
        description=(
            "Default state of the 'Recalculate Normals' option. When enabled, "
            "the importer discards any per-corner normals from the DAE and "
            "recomputes consistent outward-facing normals after import. Fixes "
            "DAE files with inconsistent face winding that show up as dark / "
            "flipped patches on the model"
        ),
        default=True,
    )

    default_global_scale: FloatProperty(
        name="Scale",
        description="Default uniform scale applied on import",
        default=1.0,
        min=1e-6,
        soft_min=0.001,
        soft_max=1000.0,
    )

    default_forward_axis: EnumProperty(
        name="Forward",
        description="Default forward axis used by the importer",
        items=FORWARD_AXIS_ITEMS,
        default="-Y",
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Default import options:")
        col = layout.column(align=True)
        col.prop(self, "default_import_rig")
        col.prop(self, "default_split_by_material")
        col.prop(self, "default_use_default_material")
        col.prop(self, "default_recalculate_normals")
        col.prop(self, "default_global_scale")
        col.prop(self, "default_forward_axis")
        layout.label(
            text="These defaults seed the File > Import > Simple COLLADA "
                 "(.dae) dialog and drag-and-drop imports.",
        )


def get_prefs(context):
    """Return this add-on's AddonPreferences, or ``None`` if unavailable."""
    addon = context.preferences.addons.get(__package__)
    if addon is None:
        return None
    return addon.preferences
