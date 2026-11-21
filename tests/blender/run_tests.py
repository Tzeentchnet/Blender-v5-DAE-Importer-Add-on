# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import math
import sys
import tempfile
import unittest

import bpy
from mathutils import Vector


REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

import simple_collada_importer
from simple_collada_importer.collada import parse_collada
from simple_collada_importer.collada.geometry import decode_geometry
from simple_collada_importer.exporter import export_dae
from simple_collada_importer.importer import import_dae
from simple_collada_importer.operators import IMPORT_OT_simple_collada_full
from simple_collada_importer.preferences import SimpleColladaPreferences


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
            self.assertTrue(hasattr(bpy.ops.export_scene, "simple_collada"))
            self.assertTrue(hasattr(bpy.types, "IO_FH_simple_collada"))
            preferences_type = SimpleColladaPreferences
            import_type = IMPORT_OT_simple_collada_full
            self.assertFalse(
                preferences_type.__annotations__[
                    "default_parent_empties_to_bones"
                ].keywords["default"]
            )
            self.assertFalse(
                preferences_type.__annotations__[
                    "default_use_bind_pose_armature"
                ].keywords["default"]
            )
            self.assertFalse(
                import_type.__annotations__[
                    "parent_empties_to_bones"
                ].keywords["default"]
            )
            self.assertFalse(
                import_type.__annotations__[
                    "use_bind_pose_armature"
                ].keywords["default"]
            )
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
            with tempfile.TemporaryDirectory() as directory:
                result = bpy.ops.export_scene.simple_collada(
                    "EXEC_DEFAULT",
                    filepath=str(Path(directory) / "operator-export.dae"),
                )
                self.assertEqual(result, {"FINISHED"})
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


class ExportTests(unittest.TestCase):
    def setUp(self):
        clear_imported_data()
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        clear_imported_data()
        self.temporary_directory.cleanup()

    def _path(self, name):
        return str(Path(self.temporary_directory.name) / name)

    @staticmethod
    def _new_mesh_object(name):
        mesh = bpy.data.meshes.new(f"{name}Mesh")
        mesh.from_pydata(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            [],
            [(0, 1, 2)],
        )
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.scene.collection.objects.link(obj)
        return obj

    def test_geometry_material_camera_and_light_round_trip(self):
        root = bpy.data.objects.new("ExportRoot", None)
        bpy.context.scene.collection.objects.link(root)
        root.location = (2.0, 3.0, 4.0)
        mesh_object = self._new_mesh_object("ExportMesh")
        mesh_object.parent = root
        mesh_object.location = (1.0, 0.0, 0.0)
        uv_layer = mesh_object.data.uv_layers.new(name="UVMap")
        for index, value in enumerate(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))):
            uv_layer.data[index].uv = value
        color = mesh_object.data.color_attributes.new(
            name="Color",
            type="FLOAT_COLOR",
            domain="CORNER",
        )
        for item in color.data:
            item.color = (0.25, 0.5, 0.75, 1.0)
        material = bpy.data.materials.new("ExportMaterial")
        material.use_nodes = True
        bsdf = next(
            node
            for node in material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        bsdf.inputs["Base Color"].default_value = (0.2, 0.4, 0.6, 1.0)
        bsdf.inputs["Alpha"].default_value = 0.5
        image = bpy.data.images.load(
            str(FIXTURES / "textures" / "test_alb.ppm"),
            check_existing=True,
        )
        image_node = material.node_tree.nodes.new("ShaderNodeTexImage")
        image_node.image = image
        material.node_tree.links.new(
            image_node.outputs["Color"],
            bsdf.inputs["Base Color"],
        )
        mesh_object.data.materials.append(material)

        camera_data = bpy.data.cameras.new("ExportCamera")
        camera_object = bpy.data.objects.new("ExportCamera", camera_data)
        camera_object.parent = root
        bpy.context.scene.collection.objects.link(camera_object)
        light_data = bpy.data.lights.new("ExportLight", type="SPOT")
        light_data.energy = 250.0
        light_object = bpy.data.objects.new("ExportLight", light_data)
        light_object.parent = root
        bpy.context.scene.collection.objects.link(light_object)

        filepath = self._path("scene.dae")
        count, error = export_dae(filepath, bpy.context)
        self.assertIsNone(error)
        self.assertEqual(count, 4)

        document = parse_collada(filepath)
        self.assertEqual(len(document.geometries), 1)
        self.assertEqual(len(document.materials), 1)
        self.assertEqual(len(document.images), 1)
        self.assertEqual(len(document.cameras), 1)
        self.assertEqual(len(document.lights), 1)
        decoded = decode_geometry(next(iter(document.geometries.values())))
        self.assertEqual(len(decoded.triangles), 1)
        self.assertIn(("TEXCOORD", 0), decoded.corner_attributes)
        self.assertIn(("COLOR", 0), decoded.corner_attributes)

        clear_imported_data()
        imported, _armature, error = import_dae(
            filepath,
            bpy.context,
            split_by_material=False,
            recalculate_normals=False,
        )
        self.assertIsNone(error)
        self.assertEqual(imported, 3)
        self.assertEqual(
            {obj.type for obj in bpy.data.objects if obj.type != "EMPTY"},
            {"MESH", "CAMERA", "LIGHT"},
        )
        bpy.context.view_layer.update()
        imported_mesh = next(obj for obj in bpy.data.objects if obj.type == "MESH")
        self.assertLess(
            (imported_mesh.matrix_world.translation - Vector((3.0, 3.0, 4.0))).length,
            1e-5,
        )
        self.assertAlmostEqual(bpy.data.lights["ExportLight"].energy, 250.0)
        imported_material = bpy.data.materials["ExportMaterial"]
        imported_bsdf = next(
            node
            for node in imported_material.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        self.assertAlmostEqual(imported_bsdf.inputs["Alpha"].default_value, 0.5)

    def test_skin_morph_and_optional_bone_construction_round_trip(self):
        armature_data = bpy.data.armatures.new("ExportRig")
        armature = bpy.data.objects.new("ExportRig", armature_data)
        bpy.context.scene.collection.objects.link(armature)
        bpy.context.view_layer.objects.active = armature
        armature.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        root_bone = armature_data.edit_bones.new("Root")
        root_bone.head = (0.0, 0.0, 0.0)
        root_bone.tail = (0.0, 1.0, 0.0)
        child_bone = armature_data.edit_bones.new("Child")
        child_bone.head = (0.0, 1.0, 0.0)
        child_bone.tail = (0.0, 2.0, 0.0)
        child_bone.parent = root_bone
        bpy.ops.object.mode_set(mode="OBJECT")
        armature.select_set(False)

        attachment = bpy.data.objects.new("BoneSocket", None)
        bpy.context.scene.collection.objects.link(attachment)
        attachment.parent = armature
        attachment.parent_type = "BONE"
        attachment.parent_bone = "Root"
        attachment.location = (0.25, 0.5, 0.0)

        mesh_object = self._new_mesh_object("SkinnedMesh")
        mesh_object.shape_key_add(name="Basis")
        smile = mesh_object.shape_key_add(name="Smile")
        smile.data[2].co.z = 1.0
        smile.value = 0.5
        group = mesh_object.vertex_groups.new(name="Root")
        group.add([0, 1, 2], 1.0, "REPLACE")
        modifier = mesh_object.modifiers.new(name="Armature", type="ARMATURE")
        modifier.object = armature

        filepath = self._path("rigged.dae")
        count, error = export_dae(filepath, bpy.context)
        self.assertIsNone(error)
        self.assertEqual(count, 3)
        document = parse_collada(filepath)
        self.assertEqual(len(document.controllers), 2)
        self.assertTrue(
            any(controller.skin is not None for controller in document.controllers.values())
        )
        self.assertTrue(
            any(controller.morph is not None for controller in document.controllers.values())
        )

        clear_imported_data()
        imported, armature_default, error = import_dae(
            filepath,
            bpy.context,
            split_by_material=False,
            recalculate_normals=False,
        )
        self.assertIsNone(error)
        self.assertEqual(imported, 1)
        self.assertAlmostEqual(
            armature_default.data.bones["Root"].length,
            0.05,
            places=4,
        )
        socket = bpy.data.objects["BoneSocket"]
        self.assertEqual(socket.parent.type, "EMPTY")
        default_socket_world = socket.matrix_world.copy()

        clear_imported_data()
        imported, armature_bound, error = import_dae(
            filepath,
            bpy.context,
            split_by_material=False,
            recalculate_normals=False,
            parent_empties_to_bones=True,
            use_bind_pose_armature=True,
        )
        self.assertIsNone(error)
        self.assertEqual(imported, 1)
        self.assertAlmostEqual(
            armature_bound.data.bones["Root"].length,
            1.0,
            places=4,
        )
        socket = bpy.data.objects["BoneSocket"]
        self.assertIs(socket.parent, armature_bound)
        self.assertEqual(socket.parent_type, "BONE")
        self.assertEqual(socket.parent_bone, "Root")
        self.assertLess(
            (socket.matrix_world.translation - default_socket_world.translation).length,
            1e-5,
        )
        mesh = next(obj for obj in bpy.data.objects if obj.type == "MESH")
        self.assertAlmostEqual(
            mesh.data.shape_keys.key_blocks["Smile"].data[2].co.z,
            1.0,
        )

    def test_selected_line_export_supports_every_forward_axis(self):
        line_mesh = bpy.data.meshes.new("LineMesh")
        line_mesh.from_pydata(
            [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
            [(0, 1)],
            [],
        )
        line_object = bpy.data.objects.new("Line", line_mesh)
        bpy.context.scene.collection.objects.link(line_object)
        self._new_mesh_object("NotSelected")
        line_object.select_set(True)
        bpy.context.view_layer.objects.active = line_object

        for axis in ("-Y", "Y", "-X", "X", "-Z", "Z"):
            filepath = self._path(f"line-{axis.replace('-', 'negative')}.dae")
            count, error = export_dae(
                filepath,
                bpy.context,
                selected_only=True,
                forward_axis=axis,
            )
            self.assertIsNone(error)
            self.assertEqual(count, 1)
            document = parse_collada(filepath)
            self.assertEqual(len(document.geometries), 1)
            decoded = decode_geometry(next(iter(document.geometries.values())))
            self.assertEqual(len(decoded.edges), 1)
            self.assertEqual(len(decoded.triangles), 0)

    def test_export_failure_preserves_existing_destination(self):
        mesh = bpy.data.meshes.new("PointOnlyMesh")
        mesh.from_pydata([(0.0, 0.0, 0.0)], [], [])
        obj = bpy.data.objects.new("PointOnly", mesh)
        bpy.context.scene.collection.objects.link(obj)
        filepath = Path(self._path("existing.dae"))
        filepath.write_text("existing", encoding="utf-8")

        count, error = export_dae(str(filepath), bpy.context)

        self.assertEqual(count, 0)
        self.assertIn("no faces or loose edges", error)
        self.assertEqual(filepath.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
