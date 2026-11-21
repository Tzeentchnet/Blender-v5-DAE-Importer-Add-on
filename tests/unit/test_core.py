# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

import numpy as np

from simple_collada_importer.collada import ColladaError, parse_collada
from simple_collada_importer.collada.accessors import parse_source
from simple_collada_importer.collada.animation import (
    parse_channel_target,
    resolve_animation_channels,
    sample_channel,
    union_key_times,
    unwrap_degrees,
)
from simple_collada_importer.collada.geometry import (
    decode_geometry,
    triangulate_polygon,
    triangulate_polygon_with_holes,
)
from simple_collada_importer.collada.model import SampledChannel
from simple_collada_importer.collada.transforms import (
    compose_transforms,
    document_correction_matrix,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


class AccessorTests(unittest.TestCase):
    def test_accessor_offset_count_stride_and_params(self):
        source = ET.fromstring(
            """
            <source id="values">
              <float_array id="array" count="10">99 1 2 3 7 4 5 6 8 100</float_array>
              <technique_common>
                <accessor source="#array" offset="1" count="2" stride="4">
                  <param name="X"/><param name="Y"/><param name="Z"/>
                </accessor>
              </technique_common>
            </source>
            """
        )
        decoded = parse_source(source)
        self.assertEqual(decoded.params, ("X", "Y", "Z"))
        np.testing.assert_array_equal(
            decoded.data,
            np.asarray(((1, 2, 3, 7), (4, 5, 6, 8))),
        )

    def test_accessor_bounds_are_explicit(self):
        source = ET.fromstring(
            """
            <source id="bad">
              <float_array id="array" count="3">1 2 3</float_array>
              <technique_common><accessor source="#array" count="2" stride="3"/></technique_common>
            </source>
            """
        )
        with self.assertRaisesRegex(ColladaError, "ACCESSOR_OUT_OF_BOUNDS"):
            parse_source(source)


class ParserTests(unittest.TestCase):
    def test_collada_15_namespace(self):
        document = parse_collada(FIXTURES / "collada_15.dae")
        self.assertEqual(document.version, "1.5.0")
        self.assertEqual(
            document.namespace,
            "http://www.collada.org/2008/03/COLLADASchema",
        )

    def test_active_scene_units_instances_and_bindings(self):
        document = parse_collada(FIXTURES / "scene_semantics.dae")
        self.assertEqual(document.active_visual_scene, "SelectedScene")
        self.assertEqual(document.scene.id, "SelectedScene")
        self.assertAlmostEqual(document.asset.unit_meter, 0.01)
        self.assertEqual(document.asset.up_axis, "X_UP")
        instance = document.library_nodes["shared-node"].instances[0]
        self.assertEqual(instance.materials[0].target, "checker-material")
        self.assertEqual(instance.materials[0].vertex_inputs[0].input_set, 1)
        effect = document.effects["checker-effect"]
        self.assertEqual(effect.model, "phong")
        self.assertTrue(effect.double_sided)
        self.assertEqual(effect.channels["diffuse"].texture.texcoord, "UVSET1")

    def test_instance_node_cycle_is_rejected(self):
        with self.assertRaisesRegex(ColladaError, "INSTANCE_NODE_CYCLE"):
            parse_collada(FIXTURES / "malformed" / "instance_cycle.dae")

    def test_skin_morph_camera_and_light_libraries(self):
        skin_document = parse_collada(FIXTURES / "skin_animation.dae")
        skin = skin_document.controllers["skin-controller"].skin
        self.assertEqual(skin.joint_names, ("joint-sid",))
        self.assertEqual(len(skin.vertex_weights), 3)
        document = parse_collada(FIXTURES / "morph_camera_light.dae")
        self.assertEqual(document.controllers["morph-normalized"].morph.method, "NORMALIZED")
        self.assertEqual(document.controllers["morph-relative"].morph.method, "RELATIVE")
        self.assertEqual(set(document.cameras), {"perspective-camera", "orthographic-camera"})
        self.assertEqual(
            {light.kind for light in document.lights.values()},
            {"ambient", "directional", "point", "spot"},
        )

    def test_material_models_and_filename_extensions(self):
        document = parse_collada(FIXTURES / "materials.dae")
        self.assertEqual(
            {effect.model for effect in document.effects.values()},
            {"constant", "lambert", "blinn", "phong"},
        )
        phong = document.effects["phong-effect"]
        self.assertTrue({"diffuse", "normal", "ao", "roughness", "metallic"} <= set(phong.channels))
        self.assertTrue(phong.double_sided)
        self.assertEqual(phong.channels["diffuse"].texture.offset, (0.1, 0.2))
        self.assertEqual(phong.channels["diffuse"].texture.scale, (2.0, 3.0))


class GeometryTests(unittest.TestCase):
    def test_concave_polygon_ear_clipping(self):
        points = np.asarray(
            ((0, 0, 0), (2, 0, 0), (2, 2, 0), (1, 1, 0), (0, 2, 0)),
            dtype=np.float64,
        )
        triangles = triangulate_polygon(points)
        self.assertEqual(triangles.shape, (3, 3))
        self.assertEqual(set(triangles.ravel()), set(range(5)))

    def test_multi_uv_color_geometry(self):
        document = parse_collada(FIXTURES / "scene_semantics.dae")
        decoded = decode_geometry(document.geometries["triangle-geometry"])
        self.assertEqual(decoded.triangles.shape, (1, 3))
        self.assertIn(("TEXCOORD", 0), decoded.corner_attributes)
        self.assertIn(("TEXCOORD", 1), decoded.corner_attributes)
        self.assertIn(("COLOR", 0), decoded.corner_attributes)

    def test_polygon_hole_triangulation(self):
        outer = np.asarray(
            ((0, 0, 0), (4, 0, 0), (4, 4, 0), (0, 4, 0)),
            dtype=np.float64,
        )
        hole = np.asarray(
            ((1, 1, 0), (1, 3, 0), (3, 3, 0), (3, 1, 0)),
            dtype=np.float64,
        )
        triangles, corner_map = triangulate_polygon_with_holes(outer, (hole,))
        merged = np.asarray(
            [(outer, hole)[loop][corner] for loop, corner in corner_map]
        )
        area = 0.0
        for triangle in triangles:
            a, b, c = merged[triangle, :2]
            edge_b = b - a
            edge_c = c - a
            area += abs(edge_b[0] * edge_c[1] - edge_b[1] * edge_c[0]) * 0.5
        self.assertAlmostEqual(area, 12.0)

    def test_all_roadmap_primitive_types(self):
        document = parse_collada(FIXTURES / "geometry_primitives.dae")
        decoded = decode_geometry(document.geometries["all-primitives"])
        self.assertGreaterEqual(len(decoded.triangles), 13)
        self.assertEqual(len(decoded.edges), 4)

    def test_negative_index_is_rejected(self):
        document = parse_collada(FIXTURES / "malformed" / "negative_index.dae")
        with self.assertRaisesRegex(ColladaError, "NEGATIVE_INDEX"):
            decode_geometry(document.geometries["bad"])


class TransformTests(unittest.TestCase):
    def test_ordered_transforms_and_document_scale(self):
        document = parse_collada(FIXTURES / "scene_semantics.dae")
        root = document.scene.nodes[0]
        local = compose_transforms(root.transforms)
        np.testing.assert_allclose(local[:3, 3], (1, 2, 3))
        self.assertAlmostEqual(local[0, 0], 2.0)
        correction = document_correction_matrix(document.asset)
        self.assertAlmostEqual(abs(np.linalg.det(correction[:3, :3])), 1e-6)
        for forward in ("-Y", "Y", "-X", "X", "-Z", "Z"):
            matrix = document_correction_matrix(
                document.asset,
                global_scale=1.0,
                forward_axis=forward,
            )
            self.assertAlmostEqual(
                abs(np.linalg.det(matrix[:3, :3])),
                document.asset.unit_meter**3,
            )


class AnimationTests(unittest.TestCase):
    def test_step_and_linear_sampling(self):
        document = parse_collada(FIXTURES / "animation.dae")
        channels = resolve_animation_channels(document.animations["move-animation"])
        self.assertEqual(len(channels), 1)
        np.testing.assert_allclose(sample_channel(channels[0], 0.5), (0.0,))
        np.testing.assert_allclose(sample_channel(channels[0], 1.5), (15.0,))
        np.testing.assert_allclose(union_key_times(channels), (0.0, 1.0, 2.0))
        target = parse_channel_target(channels[0].target)
        self.assertEqual(target.owner, "animation-root")
        self.assertEqual(target.sid_path, ("animated-sid", "location"))
        self.assertEqual(target.sid, "location")
        self.assertEqual(target.member, "X")

    def test_bezier_tangents_and_angle_unwrap(self):
        channel = SampledChannel(
            target="node/value.X",
            times=np.asarray((0.0, 1.0)),
            values=np.asarray(((0.0,), (1.0,))),
            interpolations=("BEZIER", "BEZIER"),
            in_tangents=np.asarray(((0.0, 0.0), (2.0 / 3.0, 1.0))),
            out_tangents=np.asarray(((1.0 / 3.0, 0.0), (1.0, 1.0))),
        )
        self.assertAlmostEqual(float(sample_channel(channel, 0.5)[0]), 0.5)
        np.testing.assert_allclose(
            unwrap_degrees((350.0, 10.0, 20.0)),
            (350.0, 370.0, 380.0),
        )


if __name__ == "__main__":
    unittest.main()
