# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import math
import sys
import unittest

import bpy


REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

import simple_collada_importer
from simple_collada_importer.importer import import_dae


FIXTURES = REPOSITORY / "tests" / "fixtures"


def action_fcurves(action):
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    return [
        fcurve
        for layer in action.layers
        for strip in layer.strips
        for channelbag in strip.channelbags
        for fcurve in channelbag.fcurves
    ]


def clear_imported_data():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection_name in (
        "meshes",
        "armatures",
        "materials",
        "cameras",
        "lights",
        "actions",
    ):
        collection = getattr(bpy.data, collection_name)
        for data_block in list(collection):
            if data_block.users == 0:
                collection.remove(data_block)


class RegistrationTests(unittest.TestCase):
    def test_extension_registration(self):
        simple_collada_importer.register()
        try:
            self.assertTrue(
                hasattr(bpy.ops.import_scene, "simple_collada_full")
            )
            self.assertTrue(hasattr(bpy.types, "IO_FH_simple_collada"))
            clear_imported_data()
            result = bpy.ops.import_scene.simple_collada_full(
                "EXEC_DEFAULT",
                directory=str(FIXTURES) + "/",
                files=[
                    {"name": "scene_semantics.dae"},
                    {"name": "geometry_primitives.dae"},
                ],
                split_by_material=False,
            )
            self.assertEqual(result, {"FINISHED"})
            self.assertIn("scene_semantics", bpy.data.collections)
            self.assertIn("geometry_primitives", bpy.data.collections)
        finally:
            simple_collada_importer.unregister()


class ImportTests(unittest.TestCase):
    def setUp(self):
        clear_imported_data()

    def test_scene_geometry_material_and_attributes(self):
        warnings = []
        count, armature, error = import_dae(
            str(FIXTURES / "scene_semantics.dae"),
            bpy.context,
            split_by_material=False,
            recalculate_normals=False,
            preserve_vertex_order=True,
            warning_callback=warnings.append,
        )
        self.assertIsNone(error)
        self.assertEqual(count, 2)
        self.assertIsNone(armature)
        meshes = [obj for obj in bpy.data.objects if obj.type == "MESH"]
        self.assertEqual(len(meshes), 2)
        self.assertIs(meshes[0].data, meshes[1].data)
        mesh = meshes[0].data
        self.assertEqual(len(mesh.polygons), 1)
        self.assertEqual(
            {layer.name for layer in mesh.uv_layers},
            {"UVMap", "UVMap_1"},
        )
        self.assertIn("Col", mesh.color_attributes)
        self.assertIn("collada_source_index", mesh.attributes)
        self.assertEqual(meshes[0].material_slots[0].material["collada_effect_model"], "phong")
        root = bpy.data.objects["Root"]
        self.assertAlmostEqual(abs(root.matrix_world.to_3x3().determinant()), 8e-6)
        self.assertTrue(any("MISSING_TEXTURE" in warning for warning in warnings))

    def test_surface_and_line_primitives(self):
        count, _armature, error = import_dae(
            str(FIXTURES / "geometry_primitives.dae"),
            bpy.context,
            split_by_material=False,
        )
        self.assertIsNone(error)
        self.assertEqual(count, 1)
        mesh = next(obj.data for obj in bpy.data.objects if obj.type == "MESH")
        self.assertGreaterEqual(len(mesh.polygons), 5)
        self.assertGreaterEqual(len(mesh.edges), 4)

    def test_animation_creates_separate_action(self):
        _count, _armature, error = import_dae(
            str(FIXTURES / "animation.dae"),
            bpy.context,
            split_by_material=False,
        )
        self.assertIsNone(error)
        self.assertEqual(len(bpy.data.actions), 2)
        action = bpy.data.actions["Move__animated-node"]
        self.assertEqual(action["collada_clip_id"], "move-clip")
        animated = bpy.data.objects["animated-node"]
        self.assertIs(animated.animation_data.action, action)
        location_x = next(
            curve
            for curve in action_fcurves(action)
            if curve.data_path == "location" and curve.array_index == 0
        )
        self.assertAlmostEqual(location_x.evaluate(12.0), 0.0)
        self.assertAlmostEqual(location_x.evaluate(36.0), 15.0)

    def test_malformed_import_rolls_back(self):
        before = set(bpy.data.objects)
        count, _armature, error = import_dae(
            str(FIXTURES / "malformed" / "negative_index.dae"),
            bpy.context,
        )
        self.assertEqual(count, 0)
        self.assertIn("NEGATIVE_INDEX", error)
        self.assertEqual(set(bpy.data.objects), before)

    def test_cancellation_before_parse_leaves_no_data(self):
        before = set(bpy.data.objects)
        count, _armature, error = import_dae(
            str(FIXTURES / "scene_semantics.dae"),
            bpy.context,
            cancel=lambda: True,
        )
        self.assertEqual(count, 0)
        self.assertIn("cancelled", error.lower())
        self.assertEqual(set(bpy.data.objects), before)

    def test_cancellation_during_build_rolls_back(self):
        data_names = (
            "objects",
            "meshes",
            "armatures",
            "materials",
            "cameras",
            "lights",
            "actions",
            "collections",
            "images",
            "worlds",
        )
        before = {
            name: set(getattr(bpy.data, name))
            for name in data_names
        }
        world_before = bpy.context.scene.world
        background_before = None
        if world_before is not None and world_before.node_tree is not None:
            background = next(
                (
                    node
                    for node in world_before.node_tree.nodes
                    if node.type == "BACKGROUND"
                ),
                None,
            )
            if background is not None:
                background_before = tuple(
                    background.inputs["Color"].default_value
                )

        count, _armature, error = import_dae(
            str(FIXTURES / "morph_camera_light.dae"),
            bpy.context,
            split_by_material=False,
            cancel=lambda: "ambient-node" in bpy.data.objects,
        )

        self.assertEqual(count, 0)
        self.assertIn("cancelled", error.lower())
        for name in data_names:
            self.assertEqual(set(getattr(bpy.data, name)), before[name])
        self.assertIs(bpy.context.scene.world, world_before)
        if background_before is not None:
            background = next(
                node
                for node in world_before.node_tree.nodes
                if node.type == "BACKGROUND"
            )
            self.assertEqual(
                tuple(background.inputs["Color"].default_value),
                background_before,
            )

    def test_skin_armature_and_weights(self):
        count, armature, error = import_dae(
            str(FIXTURES / "skin_animation.dae"),
            bpy.context,
            split_by_material=False,
        )
        self.assertIsNone(error)
        self.assertEqual(count, 1)
        self.assertIsNotNone(armature)
        self.assertEqual(len(armature.data.bones), 1)
        mesh_object = next(obj for obj in bpy.data.objects if obj.type == "MESH")
        self.assertEqual(len(mesh_object.vertex_groups), 1)
        self.assertEqual(mesh_object.modifiers[0].type, "ARMATURE")
        self.assertEqual(
            [len(vertex.groups) for vertex in mesh_object.data.vertices],
            [1, 1, 1],
        )
        self.assertIsNotNone(armature.animation_data)
        self.assertEqual(
            armature.animation_data.action["collada_clip_id"],
            "joint-clip",
        )
        self.assertTrue(action_fcurves(armature.animation_data.action))

    def test_morph_cameras_lights_and_morph_action(self):
        count, _armature, error = import_dae(
            str(FIXTURES / "morph_camera_light.dae"),
            bpy.context,
            split_by_material=False,
        )
        self.assertIsNone(error)
        self.assertEqual(count, 7)
        morph_objects = [
            obj for obj in bpy.data.objects
            if obj.type == "MESH" and obj.data.shape_keys is not None
        ]
        self.assertEqual(len(morph_objects), 2)
        normalized = next(
            obj for obj in morph_objects
            if obj.parent and obj.parent.name.startswith("normalized-node")
        )
        self.assertAlmostEqual(
            normalized.data.shape_keys.key_blocks[1].value,
            1.0,
        )
        relative = next(obj for obj in morph_objects if obj is not normalized)
        self.assertAlmostEqual(relative.data.shape_keys.key_blocks[1].value, 0.5)
        self.assertAlmostEqual(
            relative.data.shape_keys.key_blocks[1].data[2].co.z,
            1.0,
        )
        self.assertEqual({camera.type for camera in bpy.data.cameras}, {"PERSP", "ORTHO"})
        self.assertEqual({light.type for light in bpy.data.lights}, {"SUN", "POINT", "SPOT"})
        perspective = bpy.data.cameras["Perspective"]
        orthographic = bpy.data.cameras["Orthographic"]
        self.assertAlmostEqual(perspective.angle_y, math.radians(60.0))
        self.assertAlmostEqual(perspective.clip_start, 0.1)
        self.assertAlmostEqual(perspective.clip_end, 100.0)
        self.assertAlmostEqual(orthographic.ortho_scale, 2.0)
        self.assertAlmostEqual(bpy.data.lights["Spot"].spot_size, math.radians(60.0))
        self.assertIsNotNone(bpy.context.scene.world)

    def test_material_models_uv_mapping_and_color_spaces(self):
        count, _armature, error = import_dae(
            str(FIXTURES / "materials.dae"),
            bpy.context,
            split_by_material=False,
        )
        self.assertIsNone(error)
        self.assertEqual(count, 1)
        materials = {
            material["collada_effect_model"]: material
            for material in bpy.data.materials
            if "collada_effect_model" in material
        }
        self.assertEqual(set(materials), {"constant", "lambert", "blinn", "phong"})
        constant_bsdf = next(
            node
            for node in materials["constant"].node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        lambert_bsdf = next(
            node
            for node in materials["lambert"].node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertAlmostEqual(constant_bsdf.inputs["Alpha"].default_value, 0.25)
        self.assertAlmostEqual(lambert_bsdf.inputs["Alpha"].default_value, 0.75)
        phong = materials["phong"]
        uv_nodes = [node for node in phong.node_tree.nodes if node.type == "UVMAP"]
        self.assertTrue(uv_nodes)
        self.assertTrue(all(node.uv_map == "UVMap_1" for node in uv_nodes))
        self.assertTrue(any(node.type == "MAPPING" for node in phong.node_tree.nodes))
        self.assertTrue(any(node.type == "NORMAL_MAP" for node in phong.node_tree.nodes))
        color_spaces = {
            image.name: image.colorspace_settings.name for image in bpy.data.images
        }
        self.assertIn("sRGB", set(color_spaces.values()))
        self.assertIn("Non-Color", set(color_spaces.values()))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
