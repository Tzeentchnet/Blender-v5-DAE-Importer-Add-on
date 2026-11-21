# SPDX-License-Identifier: GPL-3.0-or-later
"""COLLADA 1.4.1 exporter for Blender scenes."""

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET

import bpy
from mathutils import Matrix, Vector

from .collada.model import Asset
from .collada.transforms import document_correction_matrix


LOGGER = logging.getLogger(__name__)
COLLADA_NAMESPACE = "http://www.collada.org/2005/11/COLLADASchema"
SUPPORTED_OBJECT_TYPES = {"MESH", "EMPTY", "ARMATURE", "CAMERA", "LIGHT"}


@dataclass(frozen=True)
class ExportOptions:
    selected_only: bool = False
    export_rig: bool = True
    export_textures: bool = True
    global_scale: float = 1.0
    forward_axis: str = "-Y"


class _IdRegistry:
    def __init__(self):
        self._ids = {}
        self._used = set()

    def get(self, key, preferred):
        identifier = self._ids.get(key)
        if identifier is not None:
            return identifier
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(preferred or "item"))
        if not base or not re.match(r"[A-Za-z_]", base):
            base = f"id_{base}"
        identifier = base
        suffix = 1
        while identifier in self._used:
            suffix += 1
            identifier = f"{base}_{suffix}"
        self._ids[key] = identifier
        self._used.add(identifier)
        return identifier


def _element(parent, tag, attributes=None, text=None):
    element = ET.SubElement(parent, tag, attributes or {})
    if text is not None:
        element.text = text
    return element


def _floats(values):
    return " ".join(f"{float(value):.9g}" for value in values)


def _integers(values):
    return " ".join(str(int(value)) for value in values)


def _matrix_text(matrix):
    return _floats(value for row in matrix for value in row)


def _socket(node, *names):
    for name in names:
        socket = node.inputs.get(name)
        if socket is not None:
            return socket
    return None


def _linked_image(socket):
    if socket is None or not socket.is_linked:
        return None
    pending = [link.from_node for link in socket.links]
    visited = set()
    while pending:
        node = pending.pop()
        pointer = node.as_pointer()
        if pointer in visited:
            continue
        visited.add(pointer)
        if node.type == "TEX_IMAGE" and node.image is not None:
            return node.image
        for input_socket in node.inputs:
            pending.extend(link.from_node for link in input_socket.links)
    return None


class ColladaExporter:
    def __init__(self, filepath, context, options):
        self.filepath = os.path.abspath(filepath)
        self.context = context
        self.options = options
        self.ids = _IdRegistry()
        self.objects = []
        self.object_set = set()
        self.node_ids = {}
        self.geometry_ids = {}
        self.material_ids = {}
        self.effect_ids = {}
        self.image_ids = {}
        self.camera_ids = {}
        self.light_ids = {}
        self.morph_ids = {}
        self.skin_ids = {}
        self.morph_target_ids = {}
        self.joint_ids = {}
        self.joint_sids = {}
        self.material_symbols = {}
        self.armatures = {}
        self.normal_children = {}
        self.bone_children = {}
        self.root = None
        self.library_images = None
        self.library_effects = None
        self.library_materials = None
        self.library_geometries = None
        self.library_controllers = None
        self.library_cameras = None
        self.library_lights = None
        self.visual_scene = None
        self.written_images = set()
        self.written_materials = set()

    def export(self):
        if self.options.global_scale <= 0.0:
            raise ValueError("Export scale must be positive")
        self.context.view_layer.update()
        self.objects = self._collect_objects()
        if not self.objects:
            raise ValueError("No supported objects to export")
        self.object_set = set(self.objects)
        self._prepare_ids()
        self._prepare_hierarchy()
        self._create_document()
        self._write_libraries()
        self._write_visual_scene()
        self._prune_empty_libraries()
        self._write_atomic()
        return len(self.objects)

    def _collect_objects(self):
        source = (
            list(self.context.selected_objects)
            if self.options.selected_only
            else list(self.context.scene.objects)
        )
        included = {
            obj
            for obj in source
            if obj.type in SUPPORTED_OBJECT_TYPES
            and (self.options.export_rig or obj.type != "ARMATURE")
        }
        if self.options.selected_only:
            pending = list(included)
            while pending:
                obj = pending.pop()
                parent = obj.parent
                if (
                    parent is not None
                    and parent.type in SUPPORTED_OBJECT_TYPES
                    and (self.options.export_rig or parent.type != "ARMATURE")
                    and parent not in included
                ):
                    included.add(parent)
                    pending.append(parent)
                if obj.type == "MESH" and self.options.export_rig:
                    armature = self._mesh_armature(obj)
                    if armature is not None and armature not in included:
                        included.add(armature)
                        pending.append(armature)
        order = {obj: index for index, obj in enumerate(self.context.scene.objects)}
        return sorted(included, key=lambda obj: order.get(obj, len(order)))

    def _prepare_ids(self):
        for obj in self.objects:
            self.node_ids[obj] = self.ids.get(
                ("node", obj.as_pointer()),
                f"node_{obj.name}",
            )
            if obj.type == "MESH":
                self.geometry_ids[obj] = self.ids.get(
                    ("geometry", obj.as_pointer()),
                    f"{obj.name}_geometry",
                )
                shape_keys = getattr(obj.data, "shape_keys", None)
                key_blocks = (
                    list(shape_keys.key_blocks)[1:]
                    if shape_keys is not None and shape_keys.key_blocks
                    else []
                )
                for key in key_blocks:
                    self.morph_target_ids[(obj, key.name)] = self.ids.get(
                        ("morph_geometry", obj.as_pointer(), key.name),
                        f"{obj.name}_{key.name}_geometry",
                    )
                if key_blocks:
                    self.morph_ids[obj] = self.ids.get(
                        ("morph", obj.as_pointer()),
                        f"{obj.name}_morph",
                    )
                armature = self._mesh_armature(obj)
                if self.options.export_rig and armature is not None:
                    self.armatures[obj] = armature
                    self.skin_ids[obj] = self.ids.get(
                        ("skin", obj.as_pointer()),
                        f"{obj.name}_skin",
                    )
                for index, slot in enumerate(obj.material_slots):
                    material = slot.material
                    symbol = self.ids.get(
                        ("material_symbol", obj.as_pointer(), index),
                        f"{obj.name}_material_{index}",
                    )
                    self.material_symbols[(obj, index)] = symbol
                    if material is not None:
                        self._prepare_material_ids(material)
            elif obj.type == "CAMERA":
                self.camera_ids[obj] = self.ids.get(
                    ("camera", obj.data.as_pointer()),
                    f"{obj.data.name}_camera",
                )
            elif obj.type == "LIGHT":
                self.light_ids[obj] = self.ids.get(
                    ("light", obj.data.as_pointer()),
                    f"{obj.data.name}_light",
                )
            elif obj.type == "ARMATURE":
                for bone in obj.data.bones:
                    key = (obj, bone.name)
                    self.joint_ids[key] = self.ids.get(
                        ("joint", obj.as_pointer(), bone.name),
                        f"{obj.name}_{bone.name}_joint",
                    )
                    self.joint_sids[key] = self.ids.get(
                        ("joint_sid", obj.as_pointer(), bone.name),
                        f"{obj.name}_{bone.name}",
                    )

    def _prepare_material_ids(self, material):
        if material in self.material_ids:
            return
        pointer = material.as_pointer()
        self.material_ids[material] = self.ids.get(
            ("material", pointer),
            f"{material.name}_material",
        )
        self.effect_ids[material] = self.ids.get(
            ("effect", pointer),
            f"{material.name}_effect",
        )

    def _prepare_hierarchy(self):
        self.normal_children = {obj: [] for obj in self.objects}
        for obj in self.objects:
            parent = obj.parent
            if parent not in self.object_set:
                continue
            if obj.parent_type == "BONE" and parent.type == "ARMATURE":
                self.bone_children.setdefault(
                    (parent, obj.parent_bone),
                    [],
                ).append(obj)
            else:
                self.normal_children[parent].append(obj)

    def _create_document(self):
        self.root = ET.Element(
            "COLLADA",
            {
                "xmlns": COLLADA_NAMESPACE,
                "version": "1.4.1",
            },
        )
        asset = _element(self.root, "asset")
        contributor = _element(asset, "contributor")
        _element(
            contributor,
            "authoring_tool",
            text="Simple COLLADA Importer/Exporter for Blender 5",
        )
        _element(
            asset,
            "created",
            text="1970-01-01T00:00:00Z",
        )
        _element(
            asset,
            "modified",
            text="1970-01-01T00:00:00Z",
        )
        _element(asset, "unit", {"name": "meter", "meter": "1"})
        _element(asset, "up_axis", text="Z_UP")
        self.library_images = _element(self.root, "library_images")
        self.library_effects = _element(self.root, "library_effects")
        self.library_materials = _element(self.root, "library_materials")
        self.library_geometries = _element(self.root, "library_geometries")
        self.library_controllers = _element(self.root, "library_controllers")
        self.library_cameras = _element(self.root, "library_cameras")
        self.library_lights = _element(self.root, "library_lights")
        visual_scenes = _element(self.root, "library_visual_scenes")
        self.visual_scene = _element(
            visual_scenes,
            "visual_scene",
            {"id": "Scene", "name": self.context.scene.name},
        )

    def _write_libraries(self):
        for obj in self.objects:
            if obj.type == "MESH":
                for slot in obj.material_slots:
                    if slot.material is not None:
                        self._write_material(slot.material)
                self._write_mesh_geometries(obj)
                if obj in self.morph_ids:
                    self._write_morph_controller(obj)
                if obj in self.skin_ids:
                    self._write_skin_controller(obj, self.armatures[obj])
            elif obj.type == "CAMERA":
                self._write_camera(obj)
            elif obj.type == "LIGHT":
                self._write_light(obj)

    def _write_float_source(self, parent, source_id, rows, params):
        source = _element(parent, "source", {"id": source_id})
        width = len(params)
        flat = [component for row in rows for component in row[:width]]
        array_id = f"{source_id}_array"
        _element(
            source,
            "float_array",
            {"id": array_id, "count": str(len(flat))},
            _floats(flat),
        )
        common = _element(source, "technique_common")
        accessor = _element(
            common,
            "accessor",
            {
                "source": f"#{array_id}",
                "count": str(len(rows)),
                "stride": str(width),
            },
        )
        for name, value_type in params:
            _element(accessor, "param", {"name": name, "type": value_type})
        return source_id

    def _write_name_source(self, parent, source_id, values, param, array_tag):
        source = _element(parent, "source", {"id": source_id})
        array_id = f"{source_id}_array"
        _element(
            source,
            array_tag,
            {"id": array_id, "count": str(len(values))},
            " ".join(values),
        )
        common = _element(source, "technique_common")
        accessor = _element(
            common,
            "accessor",
            {
                "source": f"#{array_id}",
                "count": str(len(values)),
                "stride": "1",
            },
        )
        value_type = "IDREF" if array_tag == "IDREF_array" else "Name"
        _element(accessor, "param", {"name": param, "type": value_type})

    def _write_matrix_source(self, parent, source_id, matrices):
        source = _element(parent, "source", {"id": source_id})
        array_id = f"{source_id}_array"
        flat = [
            value
            for matrix in matrices
            for row in matrix
            for value in row
        ]
        _element(
            source,
            "float_array",
            {"id": array_id, "count": str(len(flat))},
            _floats(flat),
        )
        common = _element(source, "technique_common")
        accessor = _element(
            common,
            "accessor",
            {
                "source": f"#{array_id}",
                "count": str(len(matrices)),
                "stride": "16",
            },
        )
        _element(
            accessor,
            "param",
            {"name": "TRANSFORM", "type": "float4x4"},
        )

    def _write_mesh_geometries(self, obj):
        mesh = obj.data
        positions = [vertex.co[:] for vertex in mesh.vertices]
        self._write_geometry(obj, self.geometry_ids[obj], positions)
        shape_keys = getattr(mesh, "shape_keys", None)
        if shape_keys is None or not shape_keys.key_blocks:
            return
        basis = shape_keys.key_blocks[0]
        for key in list(shape_keys.key_blocks)[1:]:
            if len(key.data) != len(mesh.vertices):
                raise ValueError(
                    f"Shape key {key.name!r} on {obj.name!r} has a different vertex count"
                )
            if shape_keys.use_relative:
                if key.relative_key.as_pointer() != basis.as_pointer():
                    raise ValueError(
                        f"Shape key {key.name!r} on {obj.name!r} is relative "
                        "to a non-Basis key, which COLLADA morph export cannot "
                        "represent"
                    )
                positions = [
                    (point.co - basis.data[index].co)[:]
                    for index, point in enumerate(key.data)
                ]
            else:
                positions = [point.co[:] for point in key.data]
            self._write_geometry(
                obj,
                self.morph_target_ids[(obj, key.name)],
                positions,
                name=key.name,
            )

    def _write_geometry(self, obj, geometry_id, positions, name=None):
        mesh = obj.data
        mesh.calc_loop_triangles()
        geometry = _element(
            self.library_geometries,
            "geometry",
            {"id": geometry_id, "name": name or obj.name},
        )
        mesh_element = _element(geometry, "mesh")
        position_source = f"{geometry_id}_positions"
        self._write_float_source(
            mesh_element,
            position_source,
            positions,
            (("X", "float"), ("Y", "float"), ("Z", "float")),
        )
        corner_normals = [normal.vector[:] for normal in mesh.corner_normals]
        if len(corner_normals) != len(mesh.loops):
            corner_normals = [
                mesh.polygons[loop.polygon_index].normal[:]
                for loop in mesh.loops
            ]
        normal_source = f"{geometry_id}_normals"
        self._write_float_source(
            mesh_element,
            normal_source,
            corner_normals,
            (("X", "float"), ("Y", "float"), ("Z", "float")),
        )
        uv_sources = []
        for index, layer in enumerate(mesh.uv_layers):
            source_id = f"{geometry_id}_uv_{index}"
            self._write_float_source(
                mesh_element,
                source_id,
                [item.uv[:] for item in layer.data],
                (("S", "float"), ("T", "float")),
            )
            uv_sources.append((index, source_id))
        color_sources = []
        for index, attribute in enumerate(mesh.color_attributes):
            source_id = f"{geometry_id}_color_{index}"
            values = []
            for loop in mesh.loops:
                data_index = (
                    loop.index
                    if attribute.domain == "CORNER"
                    else loop.vertex_index
                )
                values.append(attribute.data[data_index].color[:])
            self._write_float_source(
                mesh_element,
                source_id,
                values,
                (
                    ("R", "float"),
                    ("G", "float"),
                    ("B", "float"),
                    ("A", "float"),
                ),
            )
            color_sources.append((index, source_id))
        vertices_id = f"{geometry_id}_vertices"
        vertices = _element(mesh_element, "vertices", {"id": vertices_id})
        _element(
            vertices,
            "input",
            {"semantic": "POSITION", "source": f"#{position_source}"},
        )
        triangles_by_material = {}
        for triangle in mesh.loop_triangles:
            material_index = mesh.polygons[triangle.polygon_index].material_index
            triangles_by_material.setdefault(material_index, []).append(triangle)
        for material_index, triangles in triangles_by_material.items():
            attributes = {"count": str(len(triangles))}
            if (obj, material_index) in self.material_symbols:
                attributes["material"] = self.material_symbols[
                    (obj, material_index)
                ]
            triangle_element = _element(mesh_element, "triangles", attributes)
            inputs = [
                ("VERTEX", vertices_id, None),
                ("NORMAL", normal_source, None),
                *(
                    ("TEXCOORD", source_id, set_index)
                    for set_index, source_id in uv_sources
                ),
                *(
                    ("COLOR", source_id, set_index)
                    for set_index, source_id in color_sources
                ),
            ]
            for offset, (semantic, source_id, set_index) in enumerate(inputs):
                input_attributes = {
                    "semantic": semantic,
                    "source": f"#{source_id}",
                    "offset": str(offset),
                }
                if set_index is not None:
                    input_attributes["set"] = str(set_index)
                _element(triangle_element, "input", input_attributes)
            indices = []
            for triangle in triangles:
                for loop_index in triangle.loops:
                    indices.extend(
                        (
                            mesh.loops[loop_index].vertex_index,
                            loop_index,
                            *(loop_index for _item in uv_sources),
                            *(loop_index for _item in color_sources),
                        )
                    )
            _element(triangle_element, "p", text=_integers(indices))
        used_edge_indices = {
            mesh.loops[loop_index].edge_index
            for polygon in mesh.polygons
            for loop_index in polygon.loop_indices
        }
        loose_edges = [
            edge for edge in mesh.edges if edge.index not in used_edge_indices
        ]
        if not triangles_by_material and not loose_edges:
            raise ValueError(
                f"Mesh {obj.name!r} has no faces or loose edges to export"
            )
        if loose_edges:
            lines = _element(
                mesh_element,
                "lines",
                {"count": str(len(loose_edges))},
            )
            _element(
                lines,
                "input",
                {
                    "semantic": "VERTEX",
                    "source": f"#{vertices_id}",
                    "offset": "0",
                },
            )
            _element(
                lines,
                "p",
                text=_integers(
                    vertex
                    for edge in loose_edges
                    for vertex in edge.vertices
                ),
            )

    def _write_material(self, material):
        if material in self.written_materials:
            return
        self.written_materials.add(material)
        material_id = self.material_ids[material]
        effect_id = self.effect_ids[material]
        effect = _element(
            self.library_effects,
            "effect",
            {"id": effect_id},
        )
        profile = _element(effect, "profile_COMMON")
        bsdf = None
        if material.use_nodes and material.node_tree is not None:
            bsdf = next(
                (
                    node
                    for node in material.node_tree.nodes
                    if node.type == "BSDF_PRINCIPLED"
                ),
                None,
            )
        diffuse_socket = _socket(bsdf, "Base Color") if bsdf else None
        diffuse_image = (
            _linked_image(diffuse_socket)
            if self.options.export_textures
            else None
        )
        diffuse_sampler = self._write_image_sampler(
            profile,
            material,
            "diffuse",
            diffuse_image,
        )
        normal_image = (
            _linked_image(_socket(bsdf, "Normal"))
            if bsdf is not None and self.options.export_textures
            else None
        )
        normal_sampler = self._write_image_sampler(
            profile,
            material,
            "normal",
            normal_image,
        )
        technique = _element(profile, "technique", {"sid": "common"})
        shader = _element(technique, "phong")
        emission_socket = (
            _socket(bsdf, "Emission Color", "Emission") if bsdf else None
        )
        emission = _element(shader, "emission")
        emission_color = (
            tuple(emission_socket.default_value)
            if emission_socket is not None
            else (0.0, 0.0, 0.0, 1.0)
        )
        _element(emission, "color", text=_floats(emission_color[:4]))
        ambient = _element(shader, "ambient")
        _element(ambient, "color", text="0 0 0 1")
        diffuse = _element(shader, "diffuse")
        if diffuse_sampler is not None:
            _element(
                diffuse,
                "texture",
                {"texture": diffuse_sampler, "texcoord": "CHANNEL0"},
            )
        else:
            color = (
                tuple(diffuse_socket.default_value)
                if diffuse_socket is not None
                else tuple(material.diffuse_color)
            )
            _element(diffuse, "color", text=_floats(color[:4]))
        specular = _element(shader, "specular")
        specular_level = _socket(
            bsdf,
            "Specular IOR Level",
            "Specular",
        ) if bsdf else None
        specular_value = (
            float(specular_level.default_value)
            if specular_level is not None
            else 0.5
        )
        _element(
            specular,
            "color",
            text=_floats((specular_value,) * 3 + (1.0,)),
        )
        roughness_socket = _socket(bsdf, "Roughness") if bsdf else None
        roughness = (
            float(roughness_socket.default_value)
            if roughness_socket is not None
            else float(material.roughness)
        )
        shininess = _element(shader, "shininess")
        _element(
            shininess,
            "float",
            text=f"{max(0.0, (1.0 - roughness) ** 2 * 128.0):.9g}",
        )
        reflective = _element(shader, "reflective")
        _element(reflective, "color", text="1 1 1 1")
        reflectivity = _element(shader, "reflectivity")
        metallic_socket = _socket(bsdf, "Metallic") if bsdf else None
        metallic = (
            float(metallic_socket.default_value)
            if metallic_socket is not None
            else float(material.metallic)
        )
        _element(reflectivity, "float", text=f"{metallic:.9g}")
        alpha_socket = _socket(bsdf, "Alpha") if bsdf else None
        alpha = (
            float(alpha_socket.default_value)
            if alpha_socket is not None
            else float(material.diffuse_color[3])
        )
        transparent = _element(shader, "transparent", {"opaque": "A_ONE"})
        _element(transparent, "color", text="1 1 1 1")
        transparency = _element(shader, "transparency")
        _element(transparency, "float", text=f"{alpha:.9g}")
        ior_socket = _socket(bsdf, "IOR") if bsdf else None
        ior = float(ior_socket.default_value) if ior_socket is not None else 1.45
        index_of_refraction = _element(shader, "index_of_refraction")
        _element(index_of_refraction, "float", text=f"{ior:.9g}")
        if normal_sampler is not None:
            extra = _element(effect, "extra")
            technique_extra = _element(
                extra,
                "technique",
                {"profile": "FCOLLADA"},
            )
            bump = _element(technique_extra, "bump")
            _element(
                bump,
                "texture",
                {"texture": normal_sampler, "texcoord": "CHANNEL0"},
            )
        material_element = _element(
            self.library_materials,
            "material",
            {"id": material_id, "name": material.name},
        )
        _element(
            material_element,
            "instance_effect",
            {"url": f"#{effect_id}"},
        )

    def _write_image_sampler(self, profile, material, channel, image):
        if image is None:
            return None
        raw_path = bpy.path.abspath(image.filepath)
        if not raw_path or not os.path.isfile(raw_path):
            return None
        image_id = self.image_ids.get(image)
        if image_id is None:
            image_id = self.ids.get(
                ("image", image.as_pointer()),
                f"{image.name}_image",
            )
            self.image_ids[image] = image_id
        if image not in self.written_images:
            self.written_images.add(image)
            try:
                path = os.path.relpath(raw_path, os.path.dirname(self.filepath))
            except ValueError:
                path = Path(raw_path).resolve().as_uri()
            image_element = _element(
                self.library_images,
                "image",
                {"id": image_id, "name": image.name},
            )
            _element(
                image_element,
                "init_from",
                text=path.replace("\\", "/"),
            )
        base = self.ids.get(
            ("sampler", material.as_pointer(), channel),
            f"{material.name}_{channel}",
        )
        surface_sid = f"{base}_surface"
        sampler_sid = f"{base}_sampler"
        newparam = _element(profile, "newparam", {"sid": surface_sid})
        surface = _element(newparam, "surface", {"type": "2D"})
        _element(surface, "init_from", text=image_id)
        newparam = _element(profile, "newparam", {"sid": sampler_sid})
        sampler = _element(newparam, "sampler2D")
        _element(sampler, "source", text=surface_sid)
        return sampler_sid

    def _write_morph_controller(self, obj):
        shape_keys = obj.data.shape_keys
        keys = list(shape_keys.key_blocks)[1:]
        controller_id = self.morph_ids[obj]
        controller = _element(
            self.library_controllers,
            "controller",
            {"id": controller_id, "name": f"{obj.name} Morph"},
        )
        method = "RELATIVE" if shape_keys.use_relative else "NORMALIZED"
        morph = _element(
            controller,
            "morph",
            {"source": f"#{self.geometry_ids[obj]}", "method": method},
        )
        target_source = f"{controller_id}_targets"
        target_ids = [self.morph_target_ids[(obj, key.name)] for key in keys]
        self._write_name_source(
            morph,
            target_source,
            target_ids,
            "MORPH_TARGET",
            "IDREF_array",
        )
        weight_source = f"{controller_id}_weights"
        self._write_float_source(
            morph,
            weight_source,
            [(float(key.value),) for key in keys],
            (("MORPH_WEIGHT", "float"),),
        )
        targets = _element(morph, "targets")
        _element(
            targets,
            "input",
            {"semantic": "MORPH_TARGET", "source": f"#{target_source}"},
        )
        _element(
            targets,
            "input",
            {"semantic": "MORPH_WEIGHT", "source": f"#{weight_source}"},
        )

    def _mesh_armature(self, obj):
        return next(
            (
                modifier.object
                for modifier in obj.modifiers
                if modifier.type == "ARMATURE"
                and modifier.object is not None
                and modifier.object.type == "ARMATURE"
            ),
            None,
        )

    def _write_skin_controller(self, obj, armature):
        controller_id = self.skin_ids[obj]
        controller = _element(
            self.library_controllers,
            "controller",
            {"id": controller_id, "name": f"{obj.name} Skin"},
        )
        source_id = self.morph_ids.get(obj, self.geometry_ids[obj])
        skin = _element(
            controller,
            "skin",
            {"source": f"#{source_id}"},
        )
        _element(
            skin,
            "bind_shape_matrix",
            text=_matrix_text(Matrix.Identity(4)),
        )
        bones = list(armature.data.bones)
        joint_source = f"{controller_id}_joints"
        joint_names = [self.joint_sids[(armature, bone.name)] for bone in bones]
        self._write_name_source(
            skin,
            joint_source,
            joint_names,
            "JOINT",
            "Name_array",
        )
        bind_source = f"{controller_id}_bind_poses"
        inverse_binds = []
        for bone in bones:
            try:
                inverse_binds.append(bone.matrix_local.inverted())
            except ValueError as exc:
                raise ValueError(
                    f"Bone {bone.name!r} has a singular bind matrix"
                ) from exc
        self._write_matrix_source(skin, bind_source, inverse_binds)
        weight_rows = []
        vertex_influences = []
        group_to_bone = {
            group.index: index
            for index, bone in enumerate(bones)
            for group in (obj.vertex_groups.get(bone.name),)
            if group is not None
        }
        for vertex in obj.data.vertices:
            influences = []
            for membership in vertex.groups:
                bone_index = group_to_bone.get(membership.group)
                if bone_index is None or membership.weight <= 0.0:
                    continue
                weight_index = len(weight_rows)
                weight_rows.append((float(membership.weight),))
                influences.append((bone_index, weight_index))
            vertex_influences.append(influences)
        if not weight_rows:
            weight_rows.append((0.0,))
        weight_source = f"{controller_id}_weights"
        self._write_float_source(
            skin,
            weight_source,
            weight_rows,
            (("WEIGHT", "float"),),
        )
        joints = _element(skin, "joints")
        _element(
            joints,
            "input",
            {"semantic": "JOINT", "source": f"#{joint_source}"},
        )
        _element(
            joints,
            "input",
            {"semantic": "INV_BIND_MATRIX", "source": f"#{bind_source}"},
        )
        vertex_weights = _element(
            skin,
            "vertex_weights",
            {"count": str(len(vertex_influences))},
        )
        _element(
            vertex_weights,
            "input",
            {
                "semantic": "JOINT",
                "source": f"#{joint_source}",
                "offset": "0",
            },
        )
        _element(
            vertex_weights,
            "input",
            {
                "semantic": "WEIGHT",
                "source": f"#{weight_source}",
                "offset": "1",
            },
        )
        _element(
            vertex_weights,
            "vcount",
            text=_integers(len(items) for items in vertex_influences),
        )
        _element(
            vertex_weights,
            "v",
            text=_integers(
                value
                for influences in vertex_influences
                for pair in influences
                for value in pair
            ),
        )

    def _write_camera(self, obj):
        camera = obj.data
        camera_id = self.camera_ids[obj]
        camera_element = _element(
            self.library_cameras,
            "camera",
            {"id": camera_id, "name": camera.name},
        )
        optics = _element(camera_element, "optics")
        technique = _element(optics, "technique_common")
        if camera.type == "ORTHO":
            projection = _element(technique, "orthographic")
            _element(
                projection,
                "ymag",
                text=f"{float(camera.ortho_scale):.9g}",
            )
        else:
            projection = _element(technique, "perspective")
            _element(
                projection,
                "yfov",
                text=f"{math.degrees(float(camera.angle_y)):.9g}",
            )
            resolution = self.context.scene.render
            aspect = (
                float(resolution.resolution_x)
                * float(resolution.pixel_aspect_x)
                / max(
                    float(resolution.resolution_y)
                    * float(resolution.pixel_aspect_y),
                    1e-9,
                )
            )
            _element(projection, "aspect_ratio", text=f"{aspect:.9g}")
        _element(projection, "znear", text=f"{float(camera.clip_start):.9g}")
        _element(projection, "zfar", text=f"{float(camera.clip_end):.9g}")

    def _write_light(self, obj):
        light = obj.data
        kind_map = {
            "SUN": "directional",
            "POINT": "point",
            "SPOT": "spot",
            "AREA": "point",
        }
        kind = kind_map.get(light.type, "point")
        light_id = self.light_ids[obj]
        light_element = _element(
            self.library_lights,
            "light",
            {"id": light_id, "name": light.name},
        )
        technique = _element(
            _element(light_element, "technique_common"),
            kind,
        )
        _element(
            technique,
            "color",
            text=_floats(tuple(light.color)),
        )
        if kind in {"point", "spot"}:
            _element(technique, "constant_attenuation", text="1")
            _element(technique, "linear_attenuation", text="0")
            _element(technique, "quadratic_attenuation", text="0")
        if kind == "spot":
            _element(
                technique,
                "falloff_angle",
                text=f"{math.degrees(float(light.spot_size)) * 0.5:.9g}",
            )
            blend = min(max(float(light.spot_blend), 0.0), 0.999999)
            exponent = blend / max(1.0 - blend, 1e-6)
            _element(
                technique,
                "falloff_exponent",
                text=f"{exponent:.9g}",
            )
        extra = _element(light_element, "extra")
        blender_technique = _element(
            extra,
            "technique",
            {"profile": "BLENDER"},
        )
        _element(
            blender_technique,
            "intensity",
            text=f"{max(float(light.energy), 0.0) / 1000.0:.9g}",
        )

    def _export_matrix(self):
        correction = Matrix(
            document_correction_matrix(
                Asset(unit_meter=1.0, up_axis="Z_UP"),
                global_scale=1.0,
                forward_axis=self.options.forward_axis,
            ).tolist()
        )
        return Matrix.Scale(self.options.global_scale, 4) @ correction.inverted()

    def _write_visual_scene(self):
        roots = [
            obj
            for obj in self.objects
            if obj.parent not in self.object_set
        ]
        export_matrix = self._export_matrix()
        for obj in roots:
            self._write_object_node(
                obj,
                self.visual_scene,
                export_matrix @ obj.matrix_world,
            )
        scene = _element(self.root, "scene")
        _element(scene, "instance_visual_scene", {"url": "#Scene"})

    def _write_object_node(self, obj, parent_element, local_matrix):
        node = _element(
            parent_element,
            "node",
            {
                "id": self.node_ids[obj],
                "name": obj.name,
                "type": "NODE",
            },
        )
        _element(
            node,
            "matrix",
            {"sid": "transform"},
            _matrix_text(local_matrix),
        )
        if obj.type == "MESH":
            self._write_mesh_instance(node, obj)
        elif obj.type == "CAMERA":
            _element(
                node,
                "instance_camera",
                {"url": f"#{self.camera_ids[obj]}"},
            )
        elif obj.type == "LIGHT":
            _element(
                node,
                "instance_light",
                {"url": f"#{self.light_ids[obj]}"},
            )
        elif obj.type == "ARMATURE" and self.options.export_rig:
            for bone in obj.data.bones:
                if bone.parent is None:
                    self._write_joint_node(node, obj, bone)
        for child in self.normal_children.get(obj, ()):
            try:
                matrix = obj.matrix_world.inverted() @ child.matrix_world
            except ValueError as exc:
                raise ValueError(
                    f"Object {obj.name!r} has a singular world matrix"
                ) from exc
            self._write_object_node(child, node, matrix)

    def _write_joint_node(self, parent_element, armature, bone):
        key = (armature, bone.name)
        node = _element(
            parent_element,
            "node",
            {
                "id": self.joint_ids[key],
                "sid": self.joint_sids[key],
                "name": bone.name,
                "type": "JOINT",
            },
        )
        matrix = (
            bone.parent.matrix_local.inverted() @ bone.matrix_local
            if bone.parent is not None
            else bone.matrix_local
        )
        _element(
            node,
            "matrix",
            {"sid": "transform"},
            _matrix_text(matrix),
        )
        for child_bone in bone.children:
            self._write_joint_node(node, armature, child_bone)
        for child in self.bone_children.get((armature, bone.name), ()):
            bone_world = armature.matrix_world @ bone.matrix_local
            try:
                matrix = bone_world.inverted() @ child.matrix_world
            except ValueError as exc:
                raise ValueError(
                    f"Bone {bone.name!r} has a singular world matrix"
                ) from exc
            self._write_object_node(child, node, matrix)

    def _write_mesh_instance(self, node, obj):
        controller_id = self.skin_ids.get(obj) or self.morph_ids.get(obj)
        if controller_id is not None:
            instance = _element(
                node,
                "instance_controller",
                {"url": f"#{controller_id}"},
            )
            armature = self.armatures.get(obj)
            if armature is not None:
                for bone in armature.data.bones:
                    if bone.parent is None:
                        _element(
                            instance,
                            "skeleton",
                            text=f"#{self.joint_ids[(armature, bone.name)]}",
                        )
        else:
            instance = _element(
                node,
                "instance_geometry",
                {"url": f"#{self.geometry_ids[obj]}"},
            )
        bindings = [
            (index, slot.material)
            for index, slot in enumerate(obj.material_slots)
            if slot.material is not None
        ]
        if not bindings:
            return
        technique = _element(
            _element(instance, "bind_material"),
            "technique_common",
        )
        for index, material in bindings:
            instance_material = _element(
                technique,
                "instance_material",
                {
                    "symbol": self.material_symbols[(obj, index)],
                    "target": f"#{self.material_ids[material]}",
                },
            )
            for set_index, _layer in enumerate(obj.data.uv_layers):
                _element(
                    instance_material,
                    "bind_vertex_input",
                    {
                        "semantic": f"CHANNEL{set_index}",
                        "input_semantic": "TEXCOORD",
                        "input_set": str(set_index),
                    },
                )

    def _prune_empty_libraries(self):
        for library in (
            self.library_images,
            self.library_effects,
            self.library_materials,
            self.library_geometries,
            self.library_controllers,
            self.library_cameras,
            self.library_lights,
        ):
            if len(library) == 0:
                self.root.remove(library)

    def _write_atomic(self):
        directory = os.path.dirname(self.filepath)
        os.makedirs(directory, exist_ok=True)
        ET.indent(self.root, space="  ")
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".collada-export-",
            suffix=".tmp",
            dir=directory,
        )
        os.close(descriptor)
        try:
            ET.ElementTree(self.root).write(
                temporary_path,
                encoding="utf-8",
                xml_declaration=True,
            )
            os.replace(temporary_path, self.filepath)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


def export_dae(
    filepath,
    context,
    selected_only=False,
    export_rig=True,
    export_textures=True,
    global_scale=1.0,
    forward_axis="-Y",
):
    """Export supported Blender scene data to COLLADA 1.4.1.

    Returns ``(exported_object_count, error_message_or_None)``.
    """
    options = ExportOptions(
        selected_only=selected_only,
        export_rig=export_rig,
        export_textures=export_textures,
        global_scale=global_scale,
        forward_axis=forward_axis,
    )
    try:
        count = ColladaExporter(filepath, context, options).export()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        LOGGER.exception("COLLADA export failed for %s", filepath)
        return 0, str(exc)
    return count, None
