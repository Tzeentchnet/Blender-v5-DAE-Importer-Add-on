# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose parsed COLLADA data into Blender data blocks."""

from dataclasses import dataclass, field
import logging
import math
import os
import urllib.parse

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

from .collada.animation import (
    ChannelTarget,
    parse_channel_target,
    resolve_animation_channels,
    sample_channel,
    union_key_times,
    update_transform,
)
from .collada.errors import ColladaCancelled, ColladaError
from .collada.geometry import decode_geometry
from .collada.transforms import (
    compose_transforms,
    document_correction_matrix,
)


LOGGER = logging.getLogger(__name__)


@dataclass
class ImportOptions:
    import_rig: bool = True
    global_scale: float = 1.0
    forward_axis: str = "-Y"
    split_by_material: bool = False
    use_default_material: bool = False
    recalculate_normals: bool = False
    preserve_vertex_order: bool = False


@dataclass
class BuildResult:
    objects: list = field(default_factory=list)
    armatures: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class ImportTransaction:
    """Remove only data blocks created by the active file on failure."""

    _DATA_NAMES = (
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

    def __init__(self):
        self._before = {
            name: set(getattr(bpy.data, name))
            for name in self._DATA_NAMES
        }
        self._scene = bpy.context.scene
        self._world_before = self._scene.world
        self._world_background_before = None
        if (
            self._world_before is not None
            and self._world_before.node_tree is not None
        ):
            background = next(
                (
                    node
                    for node in self._world_before.node_tree.nodes
                    if node.type == "BACKGROUND"
                ),
                None,
            )
            if background is not None:
                self._world_background_before = tuple(
                    background.inputs["Color"].default_value
                )
        self._committed = False

    def commit(self):
        self._committed = True

    def rollback(self):
        if self._committed:
            return
        for data_block in list(set(bpy.data.objects) - self._before["objects"]):
            bpy.data.objects.remove(data_block, do_unlink=True)
        for collection in list(
            set(bpy.data.collections) - self._before["collections"]
        ):
            bpy.data.collections.remove(collection)
        self._scene.world = self._world_before
        if (
            self._world_before is not None
            and self._world_background_before is not None
        ):
            background = next(
                (
                    node
                    for node in self._world_before.node_tree.nodes
                    if node.type == "BACKGROUND"
                ),
                None,
            )
            if background is not None:
                background.inputs["Color"].default_value = (
                    self._world_background_before
                )
        for name in (
            "meshes",
            "armatures",
            "materials",
            "cameras",
            "lights",
            "actions",
            "images",
            "worlds",
        ):
            data_collection = getattr(bpy.data, name)
            for data_block in list(set(data_collection) - self._before[name]):
                if getattr(data_block, "users", 0) == 0:
                    data_collection.remove(data_block)


def _to_matrix(values):
    return Matrix(np.asarray(values, dtype=np.float64).tolist())


def _safe_name(value, fallback):
    stripped = str(value or "").strip()
    return stripped or fallback


def _set_alpha_mode(material):
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "BLEND"


class BlenderSceneBuilder:
    def __init__(
        self,
        document,
        filepath,
        context,
        collection,
        options,
        cancel=None,
        progress=None,
    ):
        self.document = document
        self.filepath = filepath
        self.context = context
        self.collection = collection
        self.options = options
        self.cancel = cancel
        self.progress = progress
        self.result = BuildResult(
            warnings=[str(item) for item in document.diagnostics]
        )
        self.correction = _to_matrix(
            document_correction_matrix(
                document.asset,
                options.global_scale,
                options.forward_axis,
            )
        )
        self._decoded_cache = {}
        self._mesh_cache = {}
        self._material_cache = {}
        self._image_cache = {}
        self._node_objects = {}
        self._node_records = {}
        self._armature_by_controller_instance = {}
        self._joint_maps = {}
        self._bone_targets = {}
        self._morph_keys = {}
        self._instance_count = 0
        self._instance_total = max(self._count_instances(), 1)
        self._dae_directory = os.path.dirname(bpy.path.abspath(filepath))

    def build(self):
        scene = self.document.scene
        if scene is None:
            raise ColladaError(
                "UNRESOLVED_ACTIVE_SCENE",
                f"Active scene {self.document.active_visual_scene!r} is unavailable",
            )
        for node in scene.nodes:
            self._index_node(node, None, Matrix.Identity(4))
        for node in scene.nodes:
            self._build_node(
                node,
                parent_object=None,
                is_root=True,
                instance_stack=(),
            )
        self._build_animations()
        return self.result

    def _check_cancel(self):
        if self.cancel is not None and self.cancel():
            raise ColladaCancelled(f"Import cancelled: {self.filepath}")

    def _advance(self):
        self._instance_count += 1
        if self.progress is not None:
            self.progress(min(self._instance_count / self._instance_total, 1.0))

    def _count_instances(self):
        count = 0

        def walk(node, stack):
            nonlocal count
            count += sum(
                instance.kind in {"geometry", "controller", "camera", "light"}
                for instance in node.instances
            )
            for instance in node.instances:
                if instance.kind == "node" and instance.url not in stack:
                    referenced = self.document.library_nodes.get(instance.url)
                    if referenced is not None:
                        walk(referenced, (*stack, instance.url))
            for child in node.children:
                walk(child, stack)

        scene = self.document.scene
        if scene is not None:
            for node in scene.nodes:
                walk(node, ())
        return count

    def _index_node(self, node, parent, parent_world):
        local = _to_matrix(compose_transforms(node.transforms))
        world = parent_world @ local
        self._node_records.setdefault(node.id, []).append(
            {
                "node": node,
                "parent": parent,
                "local": local,
                "world": world,
            }
        )
        for child in node.children:
            self._index_node(child, node, world)

    def _new_empty(self, node, parent_object, is_root):
        obj = bpy.data.objects.new(_safe_name(node.name, node.id), None)
        obj.empty_display_type = "PLAIN_AXES"
        self.collection.objects.link(obj)
        local = _to_matrix(compose_transforms(node.transforms))
        if parent_object is None:
            obj.matrix_world = self.correction @ local
        else:
            obj.parent = parent_object
            obj.matrix_local = local
        obj["collada_id"] = node.id
        if node.sid:
            obj["collada_sid"] = node.sid
        for key, value in node.extras.items():
            obj[f"collada:{key}"] = value
        self.result.objects.append(obj)
        self._node_objects.setdefault(node.id, []).append(obj)
        if node.sid:
            self._node_objects.setdefault(node.sid, []).append(obj)
        return obj

    def _build_node(self, node, parent_object, is_root, instance_stack):
        self._check_cancel()
        node_object = self._new_empty(node, parent_object, is_root)
        for instance in node.instances:
            self._check_cancel()
            if instance.kind == "geometry":
                if instance.url not in self.document.geometries:
                    self.result.warnings.append(
                        f"UNRESOLVED_INSTANCE: Missing geometry {instance.url!r} "
                        f"on node {node.id!r}"
                    )
                    self._advance()
                    continue
                obj = self._build_geometry_instance(instance)
                self._parent_instance_object(obj, node_object)
                self._advance()
            elif instance.kind == "controller":
                if instance.url not in self.document.controllers:
                    self.result.warnings.append(
                        f"UNRESOLVED_INSTANCE: Missing controller {instance.url!r} "
                        f"on node {node.id!r}"
                    )
                    self._advance()
                    continue
                objects = self._build_controller_instance(instance, node_object)
                for obj in objects:
                    if obj.parent is None:
                        self._parent_instance_object(obj, node_object)
                self._advance()
            elif instance.kind == "camera":
                if instance.url not in self.document.cameras:
                    self.result.warnings.append(
                        f"UNRESOLVED_INSTANCE: Missing camera {instance.url!r} "
                        f"on node {node.id!r}"
                    )
                    self._advance()
                    continue
                obj = self._build_camera(instance.url)
                self._parent_instance_object(obj, node_object)
                self._advance()
            elif instance.kind == "light":
                if instance.url not in self.document.lights:
                    self.result.warnings.append(
                        f"UNRESOLVED_INSTANCE: Missing light {instance.url!r} "
                        f"on node {node.id!r}"
                    )
                    self._advance()
                    continue
                obj = self._build_light(instance.url, node_object)
                if obj is not None:
                    self._parent_instance_object(obj, node_object)
                self._advance()
            elif instance.kind == "node":
                if instance.url not in self.document.library_nodes:
                    self.result.warnings.append(
                        f"UNRESOLVED_INSTANCE: Missing library node {instance.url!r} "
                        f"on node {node.id!r}"
                    )
                    continue
                if instance.url in instance_stack:
                    chain = " -> ".join((*instance_stack, instance.url))
                    raise ColladaError(
                        "INSTANCE_NODE_CYCLE",
                        f"Recursive instance_node chain: {chain}",
                    )
                referenced = self.document.library_nodes[instance.url]
                self._build_node(
                    referenced,
                    node_object,
                    is_root=False,
                    instance_stack=(*instance_stack, instance.url),
                )
        for child in node.children:
            self._build_node(
                child,
                node_object,
                is_root=False,
                instance_stack=instance_stack,
            )
        return node_object

    @staticmethod
    def _parent_instance_object(obj, node_object):
        obj.parent = node_object
        obj.matrix_local = Matrix.Identity(4)

    def _decoded_geometry(self, geometry_id):
        decoded = self._decoded_cache.get(geometry_id)
        if decoded is None:
            geometry = self.document.geometries[geometry_id]
            decoded = decode_geometry(
                geometry,
                preserve_vertex_order=self.options.preserve_vertex_order,
            )
            self._decoded_cache[geometry_id] = decoded
        return decoded

    def _build_geometry_instance(self, instance, mesh_key_suffix=(), bind_shape=None):
        decoded = self._decoded_geometry(instance.url)
        mesh_key = (instance.url, *mesh_key_suffix)
        mesh = self._mesh_cache.get(mesh_key)
        if mesh is None:
            mesh = self._create_mesh(decoded, bind_shape)
            self._mesh_cache[mesh_key] = mesh
        obj = bpy.data.objects.new(_safe_name(instance.name, decoded.name), mesh)
        self.collection.objects.link(obj)
        self.result.objects.append(obj)
        self._assign_instance_materials(obj, decoded, instance)
        return obj

    def _create_mesh(self, decoded, bind_shape=None):
        positions = np.asarray(decoded.vertices, dtype=np.float64)
        if bind_shape is not None and not np.allclose(bind_shape, np.eye(4)):
            homogeneous = np.concatenate(
                (positions, np.ones((len(positions), 1), dtype=np.float64)),
                axis=1,
            )
            positions = (homogeneous @ np.asarray(bind_shape).T)[:, :3]

        mesh = bpy.data.meshes.new(decoded.name)
        mesh.vertices.add(len(positions))
        mesh.vertices.foreach_set("co", positions.astype(np.float32).ravel())

        if len(decoded.edges):
            mesh.edges.add(len(decoded.edges))
            mesh.edges.foreach_set("vertices", decoded.edges.astype(np.int32).ravel())

        if len(decoded.triangles):
            face_count = len(decoded.triangles)
            loop_count = face_count * 3
            mesh.loops.add(loop_count)
            mesh.loops.foreach_set(
                "vertex_index",
                decoded.triangles.astype(np.int32).ravel(),
            )
            mesh.polygons.add(face_count)
            mesh.polygons.foreach_set(
                "loop_start",
                np.arange(0, loop_count, 3, dtype=np.int32),
            )
            try:
                mesh.polygons.foreach_set(
                    "loop_total",
                    np.full(face_count, 3, dtype=np.int32),
                )
            except (AttributeError, RuntimeError):
                pass
        mesh.update(calc_edges=True)
        mesh.validate(verbose=False)
        if self.options.preserve_vertex_order:
            source_indices = mesh.attributes.new(
                name="collada_source_index",
                type="INT",
                domain="POINT",
            )
            source_indices.data.foreach_set(
                "value",
                decoded.source_vertex_indices.astype(np.int32),
            )
            mesh["collada_vertex_order_preserved"] = True

        normal_values = None
        for (semantic, set_index), values in decoded.corner_attributes.items():
            if semantic == "TEXCOORD":
                name = "UVMap" if set_index in {None, 0} else f"UVMap_{set_index}"
                layer = mesh.uv_layers.new(name=name)
                layer.data.foreach_set(
                    "uv",
                    np.asarray(values[:, :2], dtype=np.float32).ravel(),
                )
            elif semantic == "COLOR":
                name = "Col" if set_index in {None, 0} else f"Col_{set_index}"
                layer = mesh.color_attributes.new(
                    name=name,
                    type="FLOAT_COLOR",
                    domain="CORNER",
                )
                layer.data.foreach_set(
                    "color",
                    np.asarray(values[:, :4], dtype=np.float32).ravel(),
                )
            elif semantic == "NORMAL" and set_index in {None, 0}:
                normal_values = values[:, :3]

        if len(decoded.triangles):
            if self.options.recalculate_normals:
                bm = bmesh.new()
                bm.from_mesh(mesh)
                bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
                bm.to_mesh(mesh)
                bm.free()
                mesh.polygons.foreach_set(
                    "use_smooth",
                    np.ones(len(mesh.polygons), dtype=bool),
                )
                mesh.update()
            elif normal_values is not None:
                mesh.polygons.foreach_set(
                    "use_smooth",
                    np.ones(len(mesh.polygons), dtype=bool),
                )
                mesh.normals_split_custom_set(normal_values.tolist())

        symbols = []
        for symbol in decoded.face_materials:
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        for symbol in symbols:
            placeholder = bpy.data.materials.new(
                f"__COLLADA_SLOT__{symbol or 'default'}"
            )
            mesh.materials.append(placeholder)
        if symbols:
            indices = np.asarray(
                [
                    symbols.index(symbol) if symbol in symbols else 0
                    for symbol in decoded.face_materials
                ],
                dtype=np.int32,
            )
            mesh.polygons.foreach_set("material_index", indices)
        mesh["collada_material_symbols"] = [str(item or "") for item in symbols]
        return mesh

    def _assign_instance_materials(self, obj, decoded, instance):
        symbols = list(obj.data.get("collada_material_symbols", []))
        binding_by_symbol = {
            material.symbol: material for material in instance.materials
        }
        for slot_index, symbol in enumerate(symbols):
            binding = binding_by_symbol.get(symbol)
            material_id = binding.target if binding is not None else symbol
            if not material_id or material_id not in self.document.materials:
                continue
            vertex_inputs = binding.vertex_inputs if binding is not None else ()
            material = self._build_material(material_id, vertex_inputs)
            mesh_material = obj.data.materials[slot_index]
            if (
                mesh_material is not None
                and mesh_material.name.startswith("__COLLADA_SLOT__")
            ):
                obj.data.materials[slot_index] = material
                if mesh_material.users == 0:
                    bpy.data.materials.remove(mesh_material)
            slot = obj.material_slots[slot_index]
            slot.link = "OBJECT"
            slot.material = material

    def _build_material(self, material_id, vertex_inputs):
        uv_signature = tuple(
            sorted(
                (
                    item.semantic,
                    item.input_semantic,
                    item.input_set,
                )
                for item in vertex_inputs
            )
        )
        key = (self.options.use_default_material, material_id, uv_signature)
        cached = self._material_cache.get(key)
        if cached is not None:
            return cached
        material_record = self.document.materials[material_id]
        effect = self.document.effects[material_record.effect]
        material = bpy.data.materials.new(
            _safe_name(material_record.name, material_id)
        )
        material.use_nodes = True
        if self.options.use_default_material:
            self._material_cache[key] = material
            return material
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (700, 0)
        principled = nodes.new("ShaderNodeBsdfPrincipled")
        principled.location = (350, 0)
        links.new(principled.outputs["BSDF"], output.inputs["Surface"])

        if effect.model in {"constant", "lambert"}:
            principled.inputs["Roughness"].default_value = 1.0
            self._set_principled_value(
                principled,
                ("Specular IOR Level", "Specular"),
                0.0,
            )

        shininess = effect.channels.get("shininess")
        if shininess and shininess.scalar is not None:
            roughness = max(
                0.0,
                min(1.0, 1.0 - math.sqrt(max(shininess.scalar, 0.0) / 128.0)),
            )
            principled.inputs["Roughness"].default_value = roughness

        ior = effect.channels.get("index_of_refraction")
        if ior and ior.scalar is not None and "IOR" in principled.inputs:
            principled.inputs["IOR"].default_value = max(1.0, ior.scalar)

        reflectivity = effect.channels.get("reflectivity")
        if reflectivity and reflectivity.scalar is not None:
            self._set_principled_value(
                principled,
                ("Specular IOR Level", "Specular"),
                max(0.0, min(1.0, reflectivity.scalar)),
            )

        uv_map = {
            item.semantic: (
                "UVMap"
                if item.input_set == 0
                else f"UVMap_{item.input_set}"
            )
            for item in vertex_inputs
            if item.input_semantic == "TEXCOORD"
        }
        node_y = 350
        diffuse = effect.channels.get("diffuse")
        diffuse_node = None
        if diffuse is not None:
            diffuse_node = self._connect_material_value(
                material,
                principled,
                "Base Color",
                diffuse,
                "sRGB",
                uv_map,
                nodes,
                links,
                (-450, node_y),
            )
            node_y -= 240
        else:
            principled.inputs["Base Color"].default_value = (0.5, 0.5, 0.5, 1.0)

        emission = effect.channels.get("emission")
        if emission is not None:
            emission_socket = (
                "Emission Color"
                if "Emission Color" in principled.inputs
                else "Emission"
            )
            self._connect_material_value(
                material,
                principled,
                emission_socket,
                emission,
                "sRGB",
                uv_map,
                nodes,
                links,
                (-450, node_y),
            )
            self._set_principled_value(
                principled,
                ("Emission Strength",),
                1.0,
            )
            node_y -= 240

        roughness_value = effect.channels.get("roughness")
        if roughness_value is not None:
            self._connect_material_value(
                material,
                principled,
                "Roughness",
                roughness_value,
                "Non-Color",
                uv_map,
                nodes,
                links,
                (-450, node_y),
            )
            node_y -= 240

        metallic_value = effect.channels.get("metallic")
        if metallic_value is not None:
            self._connect_material_value(
                material,
                principled,
                "Metallic",
                metallic_value,
                "Non-Color",
                uv_map,
                nodes,
                links,
                (-450, node_y),
            )
            node_y -= 240

        ao_value = effect.channels.get("ao")
        if (
            ao_value is not None
            and ao_value.texture is not None
            and diffuse_node is not None
        ):
            ao_node = self._texture_node(
                ao_value.texture,
                "Non-Color",
                uv_map,
                nodes,
                links,
                (-700, node_y),
            )
            if ao_node is not None:
                try:
                    mix = nodes.new("ShaderNodeMix")
                    mix.data_type = "RGBA"
                    mix.blend_type = "MULTIPLY"
                    factor = mix.inputs.get("Factor") or mix.inputs[0]
                    input_a = mix.inputs.get("A") or mix.inputs[6]
                    input_b = mix.inputs.get("B") or mix.inputs[7]
                    output_color = mix.outputs.get("Result") or mix.outputs[2]
                except RuntimeError:
                    mix = nodes.new("ShaderNodeMixRGB")
                    mix.blend_type = "MULTIPLY"
                    factor = mix.inputs[0]
                    input_a = mix.inputs[1]
                    input_b = mix.inputs[2]
                    output_color = mix.outputs["Color"]
                mix.location = (-50, node_y)
                factor.default_value = 1.0
                for link in list(links):
                    if link.to_socket == principled.inputs["Base Color"]:
                        links.remove(link)
                links.new(diffuse_node.outputs["Color"], input_a)
                links.new(ao_node.outputs["Color"], input_b)
                links.new(output_color, principled.inputs["Base Color"])

        specular = effect.channels.get("specular")
        if specular is not None:
            if specular.color is not None:
                intensity = sum(specular.color[:3]) / 3.0
                self._set_principled_value(
                    principled,
                    ("Specular IOR Level", "Specular"),
                    max(0.0, min(1.0, intensity)),
                )
            elif specular.texture is not None:
                socket = next(
                    (
                        name
                        for name in ("Specular IOR Level", "Specular")
                        if name in principled.inputs
                    ),
                    None,
                )
                if socket:
                    self._connect_material_value(
                        material,
                        principled,
                        socket,
                        specular,
                        "Non-Color",
                        uv_map,
                        nodes,
                        links,
                        (-450, node_y),
                    )
                    node_y -= 240

        normal_value = effect.channels.get("normal") or effect.channels.get("bump")
        if normal_value is not None and normal_value.texture is not None:
            image_node = self._texture_node(
                normal_value.texture,
                "Non-Color",
                uv_map,
                nodes,
                links,
                (-700, node_y),
            )
            if image_node is not None:
                if "normal" in effect.channels:
                    converter = nodes.new("ShaderNodeNormalMap")
                    converter.location = (-350, node_y)
                    links.new(image_node.outputs["Color"], converter.inputs["Color"])
                else:
                    converter = nodes.new("ShaderNodeBump")
                    converter.location = (-350, node_y)
                    links.new(image_node.outputs["Color"], converter.inputs["Height"])
                links.new(converter.outputs["Normal"], principled.inputs["Normal"])

        alpha = self._material_alpha(effect)
        if alpha < 1.0 - 1e-6:
            principled.inputs["Alpha"].default_value = alpha
            _set_alpha_mode(material)
        transparent = effect.channels.get("transparent")
        if transparent and transparent.texture:
            image_node = self._texture_node(
                transparent.texture,
                "Non-Color",
                uv_map,
                nodes,
                links,
                (-700, node_y - 240),
            )
            if image_node is not None:
                output_socket = (
                    image_node.outputs["Alpha"]
                    if effect.opaque.startswith("A_")
                    else image_node.outputs["Color"]
                )
                links.new(output_socket, principled.inputs["Alpha"])
                _set_alpha_mode(material)

        if hasattr(material, "use_backface_culling"):
            material.use_backface_culling = not effect.double_sided
        material["collada_effect_model"] = effect.model
        material["collada_opaque"] = effect.opaque
        for extra_key, extra_value in effect.extras.items():
            material[f"collada:{extra_key}"] = extra_value
        self._material_cache[key] = material
        return material

    @staticmethod
    def _set_principled_value(node, names, value):
        for name in names:
            if name in node.inputs:
                node.inputs[name].default_value = value
                return

    def _connect_material_value(
        self,
        material,
        principled,
        socket_name,
        value,
        color_space,
        uv_map,
        nodes,
        links,
        location,
    ):
        if socket_name not in principled.inputs:
            return None
        if value.color is not None:
            socket = principled.inputs[socket_name]
            if hasattr(socket, "default_value") and len(socket.default_value) == 4:
                socket.default_value = value.color
            elif hasattr(socket, "default_value"):
                socket.default_value = sum(value.color[:3]) / 3.0
        elif value.scalar is not None:
            principled.inputs[socket_name].default_value = value.scalar
        elif value.texture is not None:
            image_node = self._texture_node(
                value.texture,
                color_space,
                uv_map,
                nodes,
                links,
                location,
            )
            if image_node is not None:
                links.new(image_node.outputs["Color"], principled.inputs[socket_name])
                return image_node
        return None

    def _texture_node(
        self,
        texture,
        color_space,
        uv_map,
        nodes,
        links,
        location,
    ):
        image = self._load_image(texture.image_id, color_space)
        if image is None:
            return None
        image_node = nodes.new("ShaderNodeTexImage")
        image_node.image = image
        image_node.location = location
        image_node.label = texture.image_id
        wrap = (
            "MIRROR"
            if texture.mirror_s or texture.mirror_t
            else texture.wrap_s if texture.wrap_s == texture.wrap_t else "WRAP"
        )
        extension_map = {
            "WRAP": "REPEAT",
            "REPEAT": "REPEAT",
            "MIRROR": "MIRROR",
            "CLAMP": "EXTEND",
            "BORDER": "CLIP",
            "NONE": "CLIP",
        }
        image_node.extension = extension_map.get(wrap, "REPEAT")
        image_node["collada_wrap_s"] = texture.wrap_s
        image_node["collada_wrap_t"] = texture.wrap_t
        image_node["collada_mirror_s"] = texture.mirror_s
        image_node["collada_mirror_t"] = texture.mirror_t

        uv_name = uv_map.get(texture.texcoord)
        if uv_name is None:
            digits = "".join(character for character in texture.texcoord if character.isdigit())
            uv_name = "UVMap" if not digits or int(digits) == 0 else f"UVMap_{int(digits)}"
        uv_node = nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = uv_name
        uv_node.location = (location[0] - 450, location[1])
        vector_output = uv_node.outputs["UV"]
        if (
            texture.offset != (0.0, 0.0)
            or texture.scale != (1.0, 1.0)
            or texture.rotation != 0.0
        ):
            mapping = nodes.new("ShaderNodeMapping")
            mapping.location = (location[0] - 220, location[1])
            mapping.vector_type = "POINT"
            mapping.inputs["Location"].default_value[:2] = texture.offset
            mapping.inputs["Scale"].default_value[:2] = texture.scale
            mapping.inputs["Rotation"].default_value[2] = texture.rotation
            links.new(vector_output, mapping.inputs["Vector"])
            vector_output = mapping.outputs["Vector"]
        links.new(vector_output, image_node.inputs["Vector"])
        return image_node

    def _resolve_image_path(self, raw_path):
        raw_path = str(raw_path or "").strip()
        if not raw_path:
            return None
        parsed = urllib.parse.urlparse(urllib.parse.unquote(raw_path))
        if parsed.scheme and parsed.scheme.lower() not in {"file"}:
            self.result.warnings.append(
                f"UNSUPPORTED_IMAGE_URI: Refused non-file image URI {raw_path!r}"
            )
            return None
        candidates = []
        if parsed.scheme.lower() == "file":
            value = urllib.parse.unquote(parsed.path)
            if os.name == "nt" and value.startswith("/") and len(value) > 2 and value[2] == ":":
                value = value[1:]
            if parsed.netloc and parsed.netloc.lower() != "localhost":
                value = f"//{parsed.netloc}{value}"
            candidates.append(value)
        else:
            candidates.append(urllib.parse.unquote(raw_path))
        for candidate in list(candidates):
            if candidate.startswith("//") and not candidate.startswith("///"):
                candidates.append(os.path.join(self._dae_directory, candidate[2:]))
            if not os.path.isabs(candidate):
                candidates.append(os.path.join(self._dae_directory, candidate))
            basename = os.path.basename(candidate)
            if basename:
                candidates.append(os.path.join(self._dae_directory, basename))
        for candidate in candidates:
            normalized = os.path.normpath(
                os.path.expandvars(os.path.expanduser(candidate))
            )
            if os.path.isfile(normalized):
                return normalized
        return None

    def _load_image(self, image_id, color_space):
        key = (image_id, color_space)
        if key in self._image_cache:
            return self._image_cache[key]
        raw_path = self.document.images.get(image_id)
        resolved = self._resolve_image_path(raw_path)
        if resolved is None:
            self.result.warnings.append(
                f"MISSING_TEXTURE: Could not resolve image {image_id!r} ({raw_path!r})"
            )
            self._image_cache[key] = None
            return None
        images_before = set(bpy.data.images)
        try:
            image = bpy.data.images.load(resolved, check_existing=True)
        except RuntimeError as exc:
            self.result.warnings.append(
                f"TEXTURE_LOAD_FAILED: {resolved}: {exc}"
            )
            self._image_cache[key] = None
            return None
        try:
            current_color_space = image.colorspace_settings.name
        except TypeError:
            current_color_space = ""
        if (
            image in images_before
            and current_color_space != color_space
        ) or any(
            cached is image and cached_key[1] != color_space
            for cached_key, cached in self._image_cache.items()
            if cached is not None
        ):
            image = image.copy()
            image.name = f"{image.name} [{color_space}]"
        try:
            image.colorspace_settings.name = color_space
        except TypeError as exc:
            self.result.warnings.append(
                f"COLORSPACE_UNAVAILABLE: {color_space}: {exc}"
            )
        self._image_cache[key] = image
        return image

    @staticmethod
    def _material_alpha(effect):
        transparent = effect.channels.get("transparent")
        transparency = effect.channels.get("transparency")
        scalar = (
            transparency.scalar
            if transparency is not None and transparency.scalar is not None
            else 1.0
        )
        if transparent is None or transparent.color is None:
            return max(0.0, min(1.0, scalar))
        color = transparent.color
        if effect.opaque == "A_ZERO":
            alpha = 1.0 - color[3] * scalar
        elif effect.opaque == "RGB_ONE":
            alpha = (color[0] + color[1] + color[2]) / 3.0 * scalar
        elif effect.opaque == "RGB_ZERO":
            alpha = 1.0 - (color[0] + color[1] + color[2]) / 3.0 * scalar
        else:
            alpha = color[3] * scalar
        return max(0.0, min(1.0, alpha))

    def _controller_chain(self, controller_id):
        chain = []
        visited = set()
        current = controller_id
        while current in self.document.controllers:
            if current in visited:
                raise ColladaError(
                    "CONTROLLER_CYCLE",
                    f"Recursive controller chain at {current!r}",
                )
            visited.add(current)
            controller = self.document.controllers[current]
            chain.append(controller)
            current = (
                controller.skin.source
                if controller.skin is not None
                else controller.morph.source
            )
        if current not in self.document.geometries:
            raise ColladaError(
                "UNRESOLVED_CONTROLLER_SOURCE",
                f"Controller chain {controller_id!r} ends at missing geometry {current!r}",
            )
        return current, chain

    def _build_controller_instance(self, instance, node_object):
        geometry_id, chain = self._controller_chain(instance.url)
        skin_controller = next(
            (item for item in chain if item.skin is not None),
            None,
        )
        morph_controller = next(
            (item for item in chain if item.morph is not None),
            None,
        )
        synthetic_instance = type(instance)(
            kind="geometry",
            url=geometry_id,
            name=instance.name,
            skeletons=instance.skeletons,
            materials=instance.materials,
        )
        bind_shape = (
            skin_controller.skin.bind_shape_matrix
            if skin_controller is not None
            else None
        )
        suffix = (
            ("controller", instance.url)
            if skin_controller is not None or morph_controller is not None
            else ()
        )
        obj = self._build_geometry_instance(
            synthetic_instance,
            mesh_key_suffix=suffix,
            bind_shape=bind_shape,
        )
        objects = [obj]
        if morph_controller is not None:
            self._build_morph_keys(obj, geometry_id, morph_controller)
        if skin_controller is not None and self.options.import_rig:
            armature = self._build_armature(
                skin_controller,
                instance,
                node_object,
            )
            self._apply_skin(obj, skin_controller, armature)
            objects.append(armature)
        return objects

    def _joint_records(self, instance):
        allowed_roots = set(instance.skeletons)
        records = []

        def walk(node, parent_joint, parent_world, enabled):
            local = _to_matrix(compose_transforms(node.transforms))
            world = parent_world @ local
            identifiers = {node.id, node.sid, node.name}
            now_enabled = enabled or not allowed_roots or bool(allowed_roots & identifiers)
            next_parent = parent_joint
            if now_enabled and node.type == "JOINT":
                record = {
                    "node": node,
                    "parent": parent_joint,
                    "world": world,
                }
                records.append(record)
                next_parent = record
            for child in node.children:
                walk(child, next_parent, world, now_enabled)

        for root in self.document.scene.nodes:
            walk(root, None, Matrix.Identity(4), not allowed_roots)
        return records

    def _build_armature(self, controller, instance, node_object):
        skin = controller.skin
        records = self._joint_records(instance)
        by_identifier = {}
        for record in records:
            node = record["node"]
            for identifier in {node.id, node.sid, node.name} - {""}:
                by_identifier.setdefault(identifier, []).append(record)

        selected = []
        for joint_name in skin.joint_names:
            matches = by_identifier.get(joint_name, [])
            if len(matches) > 1:
                raise ColladaError(
                    "AMBIGUOUS_JOINT",
                    f"Joint {joint_name!r} matches multiple scene nodes",
                    controller.id,
                )
            if matches:
                selected.append(matches[0])
            else:
                selected.append(
                    {
                        "node": type(
                            "_SyntheticJoint",
                            (),
                            {
                                "id": joint_name,
                                "sid": joint_name,
                                "name": joint_name,
                            },
                        )(),
                        "parent": None,
                        "world": Matrix.Identity(4),
                    }
                )
                self.result.warnings.append(
                    f"UNRESOLVED_JOINT: Created fallback bone for {joint_name!r}"
                )

        required = set(id(record) for record in selected)
        for record in list(selected):
            parent = record.get("parent")
            while parent is not None:
                required.add(id(parent))
                parent = parent.get("parent")
        included = [record for record in records if id(record) in required]
        for record in selected:
            if id(record) not in {id(item) for item in included}:
                included.append(record)

        armature_data = bpy.data.armatures.new(f"{controller.name}_Armature")
        armature_object = bpy.data.objects.new(
            f"{controller.name}_Armature",
            armature_data,
        )
        self.collection.objects.link(armature_object)
        armature_object.parent = node_object
        armature_object.matrix_local = Matrix.Identity(4)
        self.result.objects.append(armature_object)
        self.result.armatures.append(armature_object)

        view_layer = self.context.view_layer
        previous_active = view_layer.objects.active
        previous_selected = list(self.context.selected_objects)
        for selected_object in previous_selected:
            selected_object.select_set(False)
        armature_object.select_set(True)
        view_layer.objects.active = armature_object
        bpy.ops.object.mode_set(mode="EDIT")
        bone_by_record = {}
        bone_name_by_record = {}
        raw_to_bone = {}
        try:
            inverse_bind_by_name = {
                name: Matrix(matrix.tolist())
                for name, matrix in zip(
                    skin.joint_names,
                    skin.inverse_bind_matrices,
                )
            }
            bind_world_by_name = {}
            for name, inverse_bind in inverse_bind_by_name.items():
                try:
                    bind_world_by_name[name] = inverse_bind.inverted()
                except ValueError:
                    raise ColladaError(
                        "SINGULAR_INVERSE_BIND",
                        f"Joint {name!r} has a singular inverse-bind matrix",
                        controller.id,
                    )
            for record in included:
                node = record["node"]
                raw_candidates = [node.id, node.sid, node.name]
                raw_name = next(
                    (
                        name
                        for name in skin.joint_names
                        if name in raw_candidates
                    ),
                    node.name or node.id or node.sid,
                )
                bone = armature_data.edit_bones.new(
                    _safe_name(node.name, raw_name or "Joint")
                )
                bone_by_record[id(record)] = bone
                bone_name_by_record[id(record)] = bone.name
                for candidate in raw_candidates:
                    if candidate:
                        raw_to_bone[candidate] = bone.name
                if raw_name:
                    raw_to_bone[raw_name] = bone.name
                matrix = bind_world_by_name.get(raw_name, record["world"])
                bone.matrix = matrix
                bone.tail = bone.head + matrix.to_3x3() @ Vector((0.0, 0.05, 0.0))
                bone.use_deform = raw_name in skin.joint_names
            for record in included:
                parent = record.get("parent")
                if parent is not None and id(parent) in bone_by_record:
                    bone_by_record[id(record)].parent = bone_by_record[id(parent)]
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
            armature_object.select_set(False)
            for selected_object in previous_selected:
                if selected_object.name in view_layer.objects:
                    selected_object.select_set(True)
            view_layer.objects.active = previous_active
        armature_object["collada_controller"] = controller.id
        armature_object["_collada_joint_map"] = str(raw_to_bone)
        self._joint_maps[armature_object.as_pointer()] = raw_to_bone
        for record in included:
            node = record["node"]
            target_record = {
                "armature": armature_object,
                "bone_name": bone_name_by_record[id(record)],
                "transforms": tuple(getattr(node, "transforms", ())),
            }
            for identifier in {
                getattr(node, "id", ""),
                getattr(node, "sid", ""),
                getattr(node, "name", ""),
            } - {""}:
                self._bone_targets.setdefault(identifier, []).append(target_record)
        return armature_object

    def _apply_skin(self, obj, controller, armature):
        skin = controller.skin
        name_map = self._joint_maps.get(armature.as_pointer(), {})
        groups = []
        for raw_name in skin.joint_names:
            final_name = name_map.get(raw_name, raw_name)
            groups.append(obj.vertex_groups.get(final_name) or obj.vertex_groups.new(name=final_name))
        for vertex_index, influences in enumerate(skin.vertex_weights):
            combined = {}
            for joint_index, weight in influences:
                if weight <= 0.0:
                    continue
                combined[joint_index] = combined.get(joint_index, 0.0) + weight
            for joint_index, weight in combined.items():
                groups[joint_index].add((vertex_index,), float(weight), "REPLACE")
        modifier = obj.modifiers.new(name="Armature", type="ARMATURE")
        modifier.object = armature
        modifier.use_vertex_groups = True
        obj.parent = armature

    def _build_morph_keys(self, obj, basis_geometry_id, controller):
        morph = controller.morph
        basis = self._decoded_geometry(basis_geometry_id)
        targets = []
        for target_id in morph.targets:
            target_geometry_id = target_id
            if target_id in self.document.controllers:
                target_geometry_id, _ = self._controller_chain(target_id)
            target = self._decoded_geometry(target_geometry_id)
            if len(target.vertices) != len(basis.vertices):
                raise ColladaError(
                    "MORPH_TOPOLOGY_MISMATCH",
                    f"Morph target {target_id!r} has {len(target.vertices)} vertices; "
                    f"basis has {len(basis.vertices)}",
                    controller.id,
                )
            if not np.array_equal(target.triangles, basis.triangles):
                raise ColladaError(
                    "MORPH_TOPOLOGY_MISMATCH",
                    f"Morph target {target_id!r} has different face topology",
                    controller.id,
                )
            targets.append((target_id, target))

        basis_key = obj.shape_key_add(name="Basis", from_mix=False)
        del basis_key
        key_blocks = []
        for index, (target_id, target) in enumerate(targets):
            name = self.document.geometries.get(target_id)
            key = obj.shape_key_add(
                name=_safe_name(
                    name.name if name is not None else target_id,
                    target_id,
                ),
                from_mix=False,
            )
            coordinates = np.asarray(target.vertices, dtype=np.float64)
            if morph.method == "RELATIVE":
                coordinates = basis.vertices + coordinates
            key.data.foreach_set("co", coordinates.astype(np.float32).ravel())
            key.value = float(morph.weights[index])
            key_blocks.append(key)
        self._morph_keys[controller.id] = (obj, key_blocks)

    def _build_camera(self, camera_id):
        record = self.document.cameras[camera_id]
        camera = bpy.data.cameras.new(_safe_name(record.name, camera_id))
        values = record.values
        if record.projection == "orthographic":
            camera.type = "ORTHO"
            xmag = values.get("xmag")
            ymag = values.get("ymag")
            magnitude = xmag if xmag is not None else ymag
            camera.ortho_scale = max(
                float(magnitude or 1.0) * self.document.asset.unit_meter,
                1e-6,
            )
        else:
            camera.type = "PERSP"
            xfov = values.get("xfov")
            yfov = values.get("yfov")
            aspect = values.get("aspect_ratio", 1.0)
            if yfov is None and xfov is not None:
                yfov = math.degrees(
                    2.0
                    * math.atan(
                        math.tan(math.radians(xfov) * 0.5) / max(aspect, 1e-9)
                    )
                )
            camera.angle_y = math.radians(float(yfov or 50.0))
        unit = self.document.asset.unit_meter * self.options.global_scale
        camera.clip_start = max(values.get("znear", 0.1) * unit, 1e-6)
        camera.clip_end = max(values.get("zfar", 1000.0) * unit, camera.clip_start)
        obj = bpy.data.objects.new(_safe_name(record.name, camera_id), camera)
        self.collection.objects.link(obj)
        self.result.objects.append(obj)
        return obj

    def _build_light(self, light_id, node_object):
        record = self.document.lights[light_id]
        if record.kind == "ambient":
            world = self.context.scene.world
            if world is None:
                world = bpy.data.worlds.new("World")
                self.context.scene.world = world
            world.use_nodes = True
            background = next(
                (node for node in world.node_tree.nodes if node.type == "BACKGROUND"),
                None,
            )
            if background is not None:
                current = background.inputs["Color"].default_value
                color = record.color
                background.inputs["Color"].default_value = (
                    min(1.0, current[0] + color[0]),
                    min(1.0, current[1] + color[1]),
                    min(1.0, current[2] + color[2]),
                    1.0,
                )
            node_object["collada_ambient_color"] = record.color
            return None
        type_map = {
            "directional": "SUN",
            "point": "POINT",
            "spot": "SPOT",
        }
        light = bpy.data.lights.new(
            _safe_name(record.name, light_id),
            type=type_map[record.kind],
        )
        light.color = record.color
        light.energy = max(record.values.get("intensity", 1.0), 0.0) * 1000.0
        if record.kind == "spot":
            light.spot_size = math.radians(record.values.get("falloff_angle", 45.0) * 2.0)
            exponent = max(record.values.get("falloff_exponent", 0.0), 0.0)
            light.spot_blend = min(1.0, exponent / (exponent + 1.0))
        for key in (
            "constant_attenuation",
            "linear_attenuation",
            "quadratic_attenuation",
            "falloff_angle",
            "falloff_exponent",
        ):
            if key in record.values:
                light[f"collada:{key}"] = record.values[key]
        obj = bpy.data.objects.new(_safe_name(record.name, light_id), light)
        self.collection.objects.link(obj)
        self.result.objects.append(obj)
        return obj

    def _build_animations(self):
        if not self.document.animations:
            return
        resolved_by_top = {}

        def descendant_ids(animation):
            result = [animation.id]
            for child in animation.children:
                result.extend(descendant_ids(child))
            return result

        for animation in self.document.animations.values():
            entries = resolve_animation_channels(
                animation,
                with_animation_ids=True,
            )
            direct = {}
            for owner_id, channel in entries:
                direct.setdefault(owner_id, []).append(channel)

            def populate(item):
                subtree = descendant_ids(item)
                resolved_by_top[item.id] = [
                    channel
                    for animation_id in subtree
                    for channel in direct.get(animation_id, ())
                ]
                for child in item.children:
                    populate(child)

            populate(animation)
        clips = list(self.document.animation_clips.values())
        if not clips:
            clips = [
                type(
                    "_DefaultClip",
                    (),
                    {
                        "id": "default",
                        "name": "Default",
                        "start": None,
                        "end": None,
                        "animations": tuple(self.document.animations),
                    },
                )()
            ]
        for clip_index, clip in enumerate(clips):
            channels = []
            for animation_id in clip.animations:
                if animation_id in resolved_by_top:
                    channels.extend(resolved_by_top[animation_id])
            by_owner = {}
            for channel in channels:
                target = self._resolve_channel_target(
                    parse_channel_target(channel.target)
                )
                by_owner.setdefault(target.owner, []).append((target, channel))
            bone_groups = {}
            for owner, target_channels in by_owner.items():
                if owner in self._morph_keys:
                    self._build_morph_action(
                        clip,
                        owner,
                        target_channels,
                        activate=clip_index == 0,
                    )
                    continue
                if owner in self._bone_targets:
                    for bone_record in self._bone_targets[owner]:
                        key = bone_record["armature"].as_pointer()
                        group = bone_groups.setdefault(
                            key,
                            {
                                "armature": bone_record["armature"],
                                "channels": [],
                            },
                        )
                        group["channels"].extend(
                            (
                                bone_record,
                                target,
                                channel,
                            )
                            for target, channel in target_channels
                        )
                    continue
                objects = self._node_objects.get(owner, ())
                for object_index, obj in enumerate(objects):
                    self._build_object_action(
                        clip,
                        obj,
                        target_channels,
                        activate=clip_index == 0 and object_index == 0,
                    )
            for group in bone_groups.values():
                self._build_bone_action(
                    clip,
                    group["armature"],
                    group["channels"],
                    activate=clip_index == 0,
                )

    def _resolve_channel_target(self, target):
        if len(target.sid_path) <= 1:
            return target
        records = self._node_records.get(target.owner, ())
        if len(records) != 1:
            raise ColladaError(
                "UNRESOLVED_ANIMATION_TARGET",
                f"Animation owner {target.owner!r} is missing or ambiguous",
            )
        node = records[0]["node"]
        for segment in target.sid_path[:-1]:
            matches = [
                child
                for child in node.children
                if segment in {child.id, child.sid, child.name}
            ]
            if len(matches) != 1:
                raise ColladaError(
                    "UNRESOLVED_ANIMATION_TARGET",
                    f"SID path segment {segment!r} is missing or ambiguous",
                    target.owner,
                )
            node = matches[0]
        return ChannelTarget(
            owner=node.id,
            sid_path=(target.sid_path[-1],),
            member=target.member,
            matrix_row=target.matrix_row,
            matrix_column=target.matrix_column,
            array_index=target.array_index,
        )

    def _new_action(self, owner, clip):
        action = bpy.data.actions.new(
            name=f"{_safe_name(clip.name, clip.id)}__{owner.name}"
        )
        action["collada_clip_id"] = clip.id
        if clip.start is not None:
            action["collada_clip_start"] = clip.start
        if clip.end is not None:
            action["collada_clip_end"] = clip.end
        self.result.actions.append(action)
        return action

    def _build_object_action(self, clip, obj, target_channels, activate):
        transforms = next(
            (
                record["node"].transforms
                for records in self._node_records.values()
                for record in records
                if record["node"].id == obj.get("collada_id")
            ),
            (),
        )
        if not transforms:
            return
        times = self._sample_times(
            [channel for _target, channel in target_channels],
            clip,
        )
        if len(times) == 0:
            return
        action = self._new_action(obj, clip)
        obj.animation_data_create()
        previous_action = obj.animation_data.action
        obj.animation_data.action = action
        obj.rotation_mode = "QUATERNION"
        previous_quaternion = None
        fps = self.context.scene.render.fps / self.context.scene.render.fps_base
        for time in times:
            current = transforms
            for target, channel in target_channels:
                current = update_transform(current, target, sample_channel(channel, time))
            matrix = _to_matrix(compose_transforms(current))
            if obj.parent is None:
                matrix = self.correction @ matrix
            location, rotation, scale = matrix.decompose()
            if previous_quaternion is not None:
                rotation.make_compatible(previous_quaternion)
            previous_quaternion = rotation.copy()
            frame = float(time) * fps
            obj.location = location
            obj.rotation_quaternion = rotation
            obj.scale = scale
            obj.keyframe_insert("location", frame=frame, group="Transform")
            obj.keyframe_insert("rotation_quaternion", frame=frame, group="Transform")
            obj.keyframe_insert("scale", frame=frame, group="Transform")
        self._set_action_interpolation(action, target_channels)
        if not activate:
            obj.animation_data.action = previous_action

    def _build_bone_action(self, clip, armature, channel_records, activate):
        action = self._new_action(armature, clip)
        armature.animation_data_create()
        previous_action = armature.animation_data.action
        armature.animation_data.action = action
        fps = self.context.scene.render.fps / self.context.scene.render.fps_base
        grouped = {}
        for bone_record, target, channel in channel_records:
            grouped.setdefault(bone_record["bone_name"], []).append(
                (bone_record, target, channel)
            )
        for bone_name, records in grouped.items():
            pose_bone = armature.pose.bones.get(bone_name)
            if pose_bone is None:
                raise ColladaError(
                    "UNRESOLVED_ANIMATION_TARGET",
                    f"Pose bone {bone_name!r} does not exist",
                    armature.name,
                )
            transforms = records[0][0]["transforms"]
            if not transforms:
                continue
            base_matrix = _to_matrix(compose_transforms(transforms))
            times = self._sample_times(
                [channel for _record, _target, channel in records],
                clip,
            )
            pose_bone.rotation_mode = "QUATERNION"
            previous_quaternion = None
            for time in times:
                current = transforms
                for _record, target, channel in records:
                    current = update_transform(
                        current,
                        target,
                        sample_channel(channel, time),
                    )
                animated_matrix = _to_matrix(compose_transforms(current))
                delta = base_matrix.inverted_safe() @ animated_matrix
                location, rotation, scale = delta.decompose()
                if previous_quaternion is not None:
                    rotation.make_compatible(previous_quaternion)
                previous_quaternion = rotation.copy()
                frame = float(time) * fps
                pose_bone.location = location
                pose_bone.rotation_quaternion = rotation
                pose_bone.scale = scale
                pose_bone.keyframe_insert("location", frame=frame, group=bone_name)
                pose_bone.keyframe_insert(
                    "rotation_quaternion",
                    frame=frame,
                    group=bone_name,
                )
                pose_bone.keyframe_insert("scale", frame=frame, group=bone_name)
        self._set_action_interpolation(
            action,
            [(target, channel) for _record, target, channel in channel_records],
        )
        if not activate:
            armature.animation_data.action = previous_action

    def _sample_times(self, channels, clip):
        times = union_key_times(channels, clip.start, clip.end)
        if len(times) == 0:
            return times
        if any("BEZIER" in channel.interpolations for channel in channels):
            fps = self.context.scene.render.fps / self.context.scene.render.fps_base
            start_frame = math.ceil(float(times[0]) * fps)
            end_frame = math.floor(float(times[-1]) * fps)
            if end_frame >= start_frame:
                frame_times = (
                    np.arange(start_frame, end_frame + 1, dtype=np.float64) / fps
                )
                times = np.unique(np.concatenate((times, frame_times)))
        return times

    def _build_morph_action(self, clip, controller_id, target_channels, activate):
        obj, key_blocks = self._morph_keys[controller_id]
        action = self._new_action(obj, clip)
        key_data = obj.data.shape_keys
        key_data.animation_data_create()
        previous_action = key_data.animation_data.action
        key_data.animation_data.action = action
        fps = self.context.scene.render.fps / self.context.scene.render.fps_base
        for target, channel in target_channels:
            index = target.array_index
            if index is None or index >= len(key_blocks):
                raise ColladaError(
                    "INVALID_MORPH_ANIMATION_TARGET",
                    f"Morph weight target index {index!r} is invalid",
                    controller_id,
                )
            for time in channel.times:
                key_blocks[index].value = float(sample_channel(channel, time)[0])
                key_blocks[index].keyframe_insert(
                    "value",
                    frame=float(time) * fps,
                    group="Morph",
                )
        self._set_action_interpolation(action, target_channels)
        if not activate:
            key_data.animation_data.action = previous_action

    def _set_action_interpolation(self, action, target_channels):
        fps = self.context.scene.render.fps / self.context.scene.render.fps_base
        if hasattr(action, "fcurves"):
            fcurves = list(action.fcurves)
        else:
            fcurves = [
                fcurve
                for layer in action.layers
                for strip in layer.strips
                for channelbag in strip.channelbags
                for fcurve in channelbag.fcurves
            ]
        for fcurve in fcurves:
            for point in fcurve.keyframe_points:
                source_time = point.co.x / fps
                modes = []
                for _target, channel in target_channels:
                    index = int(
                        np.clip(
                            np.searchsorted(
                                channel.times,
                                source_time,
                                side="right",
                            )
                            - 1,
                            0,
                            len(channel.interpolations) - 1,
                        )
                    )
                    modes.append(channel.interpolations[index])
                point.interpolation = (
                    "CONSTANT"
                    if modes and all(mode == "STEP" for mode in modes)
                    else "LINEAR"
                )
