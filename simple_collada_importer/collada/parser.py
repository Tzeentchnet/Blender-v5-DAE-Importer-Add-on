# SPDX-License-Identifier: GPL-3.0-or-later
"""Staged COLLADA XML parser producing the Blender-independent IR."""

import math
import os
import xml.etree.ElementTree as ET

import numpy as np

from .accessors import local_name, parse_source, strip_fragment
from .errors import ColladaCancelled, ColladaError, DiagnosticSink
from .model import (
    Animation,
    AnimationChannel,
    AnimationClip,
    AnimationSampler,
    Asset,
    Camera,
    ColladaDocument,
    Controller,
    Effect,
    Geometry,
    Input,
    Instance,
    InstanceMaterial,
    Light,
    Material,
    MaterialValue,
    MorphController,
    Node,
    Polygon,
    Primitive,
    SkinController,
    Texture,
    Transform,
    VertexInputBinding,
    VisualScene,
)


_PRIMITIVE_TAGS = {
    "triangles",
    "polylist",
    "polygons",
    "tristrips",
    "trifans",
    "lines",
    "linestrips",
}
_TRANSFORM_TAGS = {"matrix", "translate", "rotate", "scale", "lookat", "skew"}
_SHADER_MODELS = {"constant", "lambert", "blinn", "phong"}
_MATERIAL_CHANNELS = {
    "ambient",
    "diffuse",
    "emission",
    "reflective",
    "reflectivity",
    "specular",
    "shininess",
    "transparent",
    "transparency",
    "index_of_refraction",
    "bump",
    "normal",
}


def _children(element, name):
    return [child for child in element if local_name(child.tag) == name]


def _first(element, name):
    return next((child for child in element if local_name(child.tag) == name), None)


def _descendants(element, name):
    return (item for item in element.iter() if local_name(item.tag) == name)


def _text_values(element, dtype=np.float64):
    if element is None or not element.text:
        return np.empty(0, dtype=dtype)
    return np.fromstring(element.text, dtype=dtype, sep=" ")


def _bool_text(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class _Parser:
    def __init__(self, namespace, version, sink=None):
        self.sink = sink or DiagnosticSink()
        self.document = ColladaDocument(namespace=namespace, version=version)
        self._node_serial = 0
        self._animation_serial = 0

    def parse_top_level(self, element):
        tag = local_name(element.tag)
        handlers = {
            "asset": self._parse_asset,
            "library_images": self._parse_images,
            "library_effects": self._parse_effects,
            "library_materials": self._parse_materials,
            "library_geometries": self._parse_geometries,
            "library_controllers": self._parse_controllers,
            "library_nodes": self._parse_library_nodes,
            "library_visual_scenes": self._parse_visual_scenes,
            "library_animations": self._parse_animations,
            "library_animation_clips": self._parse_animation_clips,
            "library_cameras": self._parse_cameras,
            "library_lights": self._parse_lights,
            "scene": self._parse_scene,
        }
        handler = handlers.get(tag)
        if handler is not None:
            handler(element)

    def finish(self):
        document = self.document
        if not document.active_visual_scene:
            self.sink.error(
                "MISSING_ACTIVE_SCENE",
                "Document has no <scene><instance_visual_scene> reference",
            )
        if document.active_visual_scene not in document.visual_scenes:
            self.sink.error(
                "UNRESOLVED_ACTIVE_SCENE",
                f"Active visual scene {document.active_visual_scene!r} does not exist",
            )
        self._validate_references()
        document.diagnostics.extend(self.sink.warnings)
        return document

    def _put(self, mapping, key, value, kind):
        if not key:
            self.sink.error("MISSING_ID", f"{kind} has no id")
        if key in mapping:
            self.sink.error("DUPLICATE_ID", f"Duplicate {kind} id {key!r}")
        mapping[key] = value

    def _local_ref(self, value, context):
        value = (value or "").strip()
        if not value:
            self.sink.error("MISSING_REFERENCE", "Required URI reference is empty", context)
        if not value.startswith("#"):
            self.sink.error(
                "EXTERNAL_DOCUMENT_REFERENCE",
                f"External document reference {value!r} is not supported",
                context,
            )
        return value[1:]

    def _parse_asset(self, element):
        unit = _first(element, "unit")
        meter = 1.0
        unit_name = "meter"
        if unit is not None:
            unit_name = unit.attrib.get("name", "meter")
            try:
                meter = float(unit.attrib.get("meter", "1"))
            except ValueError:
                self.sink.error("INVALID_UNIT", "Asset unit meter is not numeric")
        if meter <= 0.0 or not math.isfinite(meter):
            self.sink.error("INVALID_UNIT", "Asset unit meter must be positive and finite")
        up_element = _first(element, "up_axis")
        up_axis = (
            up_element.text.strip().upper()
            if up_element is not None and up_element.text
            else "Y_UP"
        )
        if up_axis not in {"X_UP", "Y_UP", "Z_UP"}:
            self.sink.error("INVALID_UP_AXIS", f"Unsupported up axis {up_axis!r}")
        self.document.asset = Asset(meter, unit_name, up_axis)

    def _parse_images(self, library):
        for element in _children(library, "image"):
            image_id = element.attrib.get("id", "")
            init_from = next(_descendants(element, "init_from"), None)
            path = ""
            if init_from is not None:
                if init_from.text and init_from.text.strip():
                    path = init_from.text.strip()
                else:
                    ref = next(_descendants(init_from, "ref"), None)
                    if ref is not None and ref.text:
                        path = ref.text.strip()
            if not path:
                self.sink.warn(
                    "EMPTY_IMAGE",
                    f"Image {image_id or '<unnamed>'!r} has no usable init_from",
                )
            if not image_id:
                self.sink.error("MISSING_ID", "image has no id")
            if image_id in self.document.images:
                previous = self.document.images[image_id]
                detail = (
                    "with the same path"
                    if previous == path
                    else f"with a different path; using the last value {path!r}"
                )
                self.sink.warn(
                    "DUPLICATE_IMAGE_ID",
                    f"Duplicate image id {image_id!r} {detail}",
                    image_id,
                )
            self.document.images[image_id] = path

    def _parse_effects(self, library):
        for element in _children(library, "effect"):
            effect = self._parse_effect(element)
            self._put(self.document.effects, effect.id, effect, "effect")

    def _parse_effect(self, element):
        effect_id = element.attrib.get("id", "")
        profile = _first(element, "profile_COMMON")
        if profile is None:
            self.sink.warn(
                "UNSUPPORTED_EFFECT_PROFILE",
                f"Effect {effect_id!r} has no profile_COMMON",
                effect_id,
            )
            return Effect(effect_id)

        surfaces = {}
        samplers = {}
        for newparam in _children(profile, "newparam"):
            sid = newparam.attrib.get("sid", "")
            surface = _first(newparam, "surface")
            sampler = _first(newparam, "sampler2D")
            if surface is not None:
                init_from = _first(surface, "init_from")
                if init_from is not None and init_from.text:
                    surfaces[sid] = init_from.text.strip()
            if sampler is not None:
                source = _first(sampler, "source")
                sampler_data = {
                    "source": source.text.strip() if source is not None and source.text else "",
                    "wrap_s": "WRAP",
                    "wrap_t": "WRAP",
                    "mirror_s": False,
                    "mirror_t": False,
                }
                for child in sampler:
                    name = local_name(child.tag)
                    value = child.text.strip() if child.text else ""
                    if name in {"wrap_s", "wrap_t"} and value:
                        sampler_data[name] = value.upper()
                    elif name in {"mirror_s", "mirror_t"}:
                        sampler_data[name] = _bool_text(value)
                samplers[sid] = sampler_data

        technique = _first(profile, "technique")
        shader = None
        model = "lambert"
        if technique is not None:
            for child in technique:
                if local_name(child.tag) in _SHADER_MODELS:
                    shader = child
                    model = local_name(child.tag)
                    break
        if shader is None:
            self.sink.warn(
                "MISSING_COMMON_SHADER",
                f"Effect {effect_id!r} has no supported profile_COMMON shader",
                effect_id,
            )
            return Effect(effect_id)

        channels = {}
        opaque = "A_ONE"
        for channel_element in shader:
            channel_name = local_name(channel_element.tag)
            if channel_name not in _MATERIAL_CHANNELS:
                continue
            value = self._parse_material_value(
                channel_element,
                surfaces,
                samplers,
                f"effect {effect_id}, channel {channel_name}",
            )
            if value is not None:
                channels[channel_name] = value
            if channel_name == "transparent":
                opaque = channel_element.attrib.get("opaque", "A_ONE").upper()

        for technique_element in _descendants(element, "technique"):
            for child in technique_element:
                channel_name = local_name(child.tag).lower()
                if channel_name not in {"bump", "normal"} or channel_name in channels:
                    continue
                value = self._parse_material_value(
                    child,
                    surfaces,
                    samplers,
                    f"effect {effect_id}, channel {channel_name}",
                )
                if value is not None:
                    channels[channel_name] = value

        texture_candidates = []
        for texture_element in _descendants(element, "texture"):
            wrapper = ET.Element("channel")
            wrapper.append(ET.fromstring(ET.tostring(texture_element, encoding="unicode")))
            value = self._parse_material_value(
                wrapper,
                surfaces,
                samplers,
                f"effect {effect_id}, inferred texture",
            )
            if value is None or value.texture is None:
                continue
            raw_path = self.document.images.get(value.texture.image_id, "")
            texture_candidates.append((os.path.basename(raw_path).lower(), value))
        inferred = {}
        for basename, value in texture_candidates:
            if any(token in basename for token in ("_nrm", "_normal", "_norm", "_nor", "_bump")):
                inferred.setdefault("normal", value)
            elif any(token in basename for token in ("_ao", "_ambient_occlusion", "_occlusion")):
                inferred.setdefault("ao", value)
            elif any(token in basename for token in ("_roughness", "_rgh", "roughness")):
                inferred.setdefault("roughness", value)
            elif any(token in basename for token in ("_metallic", "_metalness", "_metal")):
                inferred.setdefault("metallic", value)
            elif any(token in basename for token in ("_spm", "_spec", "_specular")):
                inferred.setdefault("specular", value)
            elif any(token in basename for token in ("_alb", "_albedo", "_diffuse", "_color", "_col", "_base")):
                inferred.setdefault("diffuse", value)
        diffuse = channels.get("diffuse")
        if (
            diffuse is not None
            and diffuse.texture is not None
            and "diffuse" in inferred
        ):
            diffuse_path = self.document.images.get(diffuse.texture.image_id, "").lower()
            if any(
                token in os.path.basename(diffuse_path)
                for token in ("_ao", "_nrm", "_normal", "_spm", "_spec", "_bump")
            ):
                channels["diffuse"] = inferred["diffuse"]
        for inferred_channel, value in inferred.items():
            channels.setdefault(inferred_channel, value)

        double_sided = any(
            _bool_text(item.text)
            for item in _descendants(element, "double_sided")
        )
        extras = self._simple_extras(element)
        return Effect(
            id=effect_id,
            model=model,
            channels=channels,
            opaque=opaque,
            double_sided=double_sided,
            extras=extras,
        )

    def _parse_material_value(self, element, surfaces, samplers, context):
        color = _first(element, "color")
        if color is not None:
            values = _text_values(color)
            if values.size == 0:
                self.sink.warn(
                    "EMPTY_MATERIAL_VALUE",
                    "Ignored an empty material color",
                    context,
                )
                return None
            if values.size == 1:
                self.sink.warn(
                    "NONSTANDARD_MATERIAL_COLOR",
                    "Expanded a one-component material color to RGB",
                    context,
                )
                values = np.repeat(values, 3)
            if values.size not in {3, 4}:
                self.sink.error(
                    "INVALID_MATERIAL_COLOR",
                    f"Color requires 3 or 4 values, found {values.size}",
                    context,
                )
            rgba = np.ones(4, dtype=np.float64)
            rgba[: values.size] = values
            return MaterialValue(color=tuple(float(item) for item in rgba))
        scalar = _first(element, "float")
        if scalar is not None:
            values = _text_values(scalar)
            if values.size != 1:
                self.sink.error(
                    "INVALID_MATERIAL_SCALAR",
                    "Material scalar requires exactly one value",
                    context,
                )
            return MaterialValue(scalar=float(values[0]))
        texture_element = _first(element, "texture")
        if texture_element is None:
            return None
        sampler_sid = texture_element.attrib.get("texture", "")
        sampler = samplers.get(sampler_sid, {})
        surface_sid = sampler.get("source", sampler_sid)
        image_id = surfaces.get(surface_sid, surface_sid)
        if not image_id:
            self.sink.error(
                "UNRESOLVED_TEXTURE",
                f"Texture sampler {sampler_sid!r} has no image source",
                context,
            )
        transform = {
            "offset": [0.0, 0.0],
            "scale": [1.0, 1.0],
            "rotation": 0.0,
        }
        scalar_aliases = {
            "offsetu": ("offset", 0),
            "offsetv": ("offset", 1),
            "repeatu": ("scale", 0),
            "repeatv": ("scale", 1),
            "rotateuv": ("rotation", None),
            "rotation": ("rotation", None),
        }
        for descendant in texture_element.iter():
            name = local_name(descendant.tag).lower()
            if name not in scalar_aliases or not descendant.text:
                continue
            try:
                value = float(descendant.text.strip())
            except ValueError:
                self.sink.warn(
                    "INVALID_TEXTURE_TRANSFORM",
                    f"Ignored non-numeric {name}",
                    context,
                )
                continue
            destination, index = scalar_aliases[name]
            if index is None:
                transform[destination] = value
            else:
                transform[destination][index] = value
        return MaterialValue(
            texture=Texture(
                image_id=image_id,
                texcoord=texture_element.attrib.get("texcoord", ""),
                sampler_sid=sampler_sid,
                wrap_s=sampler.get("wrap_s", "WRAP"),
                wrap_t=sampler.get("wrap_t", "WRAP"),
                mirror_s=bool(sampler.get("mirror_s", False)),
                mirror_t=bool(sampler.get("mirror_t", False)),
                offset=tuple(transform["offset"]),
                scale=tuple(transform["scale"]),
                rotation=float(transform["rotation"]),
            )
        )

    def _parse_materials(self, library):
        for element in _children(library, "material"):
            material_id = element.attrib.get("id", "")
            instance = _first(element, "instance_effect")
            effect = (
                self._local_ref(instance.attrib.get("url"), f"material {material_id}")
                if instance is not None
                else ""
            )
            if not effect:
                self.sink.error(
                    "MISSING_MATERIAL_EFFECT",
                    f"Material {material_id!r} has no instance_effect",
                )
            material = Material(
                id=material_id,
                name=element.attrib.get("name", material_id),
                effect=effect,
            )
            self._put(self.document.materials, material_id, material, "material")

    def _parse_geometries(self, library):
        for element in _children(library, "geometry"):
            geometry = self._parse_geometry(element)
            self._put(self.document.geometries, geometry.id, geometry, "geometry")

    def _parse_geometry(self, element):
        geometry_id = element.attrib.get("id", "")
        mesh = _first(element, "mesh")
        if mesh is None:
            self.sink.warn(
                "UNSUPPORTED_GEOMETRY",
                f"Geometry {geometry_id!r} is not a mesh",
                geometry_id,
            )
            return Geometry(geometry_id, element.attrib.get("name", geometry_id))
        sources = {}
        for source_element in _children(mesh, "source"):
            source = parse_source(source_element, self.document.namespace, self.sink)
            self._put(sources, source.id, source, "geometry source")
        vertices = {}
        for vertices_element in _children(mesh, "vertices"):
            vertices_id = vertices_element.attrib.get("id", "")
            mapping = {}
            for input_element in _children(vertices_element, "input"):
                semantic = input_element.attrib.get("semantic", "").upper()
                source = self._local_ref(
                    input_element.attrib.get("source"),
                    f"vertices {vertices_id}",
                )
                set_index = self._optional_int(
                    input_element.attrib.get("set"),
                    "vertex input set",
                    f"vertices {vertices_id}",
                )
                key = (semantic, set_index)
                if key in mapping:
                    self.sink.error(
                        "DUPLICATE_INPUT",
                        f"Duplicate vertices input {key}",
                        vertices_id,
                    )
                mapping[key] = source
            self._put(vertices, vertices_id, mapping, "vertices")

        primitives = []
        for primitive_element in mesh:
            kind = local_name(primitive_element.tag)
            if kind not in _PRIMITIVE_TAGS:
                continue
            primitives.append(self._parse_primitive(primitive_element, geometry_id))
        return Geometry(
            id=geometry_id,
            name=element.attrib.get("name", geometry_id),
            sources=sources,
            vertices=vertices,
            primitives=primitives,
        )

    def _parse_primitive(self, element, geometry_id):
        kind = local_name(element.tag)
        context = f"geometry {geometry_id}, {kind}"
        try:
            count = int(element.attrib.get("count", "0"))
        except ValueError:
            self.sink.error("INVALID_PRIMITIVE_COUNT", "Primitive count is not an integer", context)
        if count < 0:
            self.sink.error("INVALID_PRIMITIVE_COUNT", "Primitive count is negative", context)
        inputs = []
        for input_element in _children(element, "input"):
            try:
                offset = int(input_element.attrib.get("offset", "0"))
            except ValueError:
                self.sink.error("INVALID_INPUT_OFFSET", "Input offset is not an integer", context)
            if offset < 0:
                self.sink.error("INVALID_INPUT_OFFSET", "Input offset is negative", context)
            inputs.append(
                Input(
                    semantic=input_element.attrib.get("semantic", "").upper(),
                    source=self._local_ref(input_element.attrib.get("source"), context),
                    offset=offset,
                    set=self._optional_int(input_element.attrib.get("set"), "input set", context),
                )
            )
        material = element.attrib.get("material")
        vcount_element = _first(element, "vcount")
        vcount = (
            _text_values(vcount_element, dtype=np.int64)
            if vcount_element is not None
            else None
        )
        streams = tuple(
            _text_values(child, dtype=np.int64)
            for child in element
            if local_name(child.tag) == "p"
        )
        polygons = ()
        if kind == "polygons":
            parsed_polygons = [
                Polygon(stream)
                for stream in streams
            ]
            for ph in _children(element, "ph"):
                outer_element = _first(ph, "p")
                if outer_element is None:
                    self.sink.error("INVALID_POLYGON", "<ph> has no outer <p>", context)
                holes = tuple(
                    _text_values(hole, dtype=np.int64)
                    for hole in _children(ph, "h")
                )
                parsed_polygons.append(
                    Polygon(_text_values(outer_element, dtype=np.int64), holes)
                )
            polygons = tuple(parsed_polygons)
            streams = ()
        return Primitive(
            kind=kind,
            count=count,
            material=material,
            inputs=tuple(inputs),
            streams=streams,
            vcount=vcount,
            polygons=polygons,
        )

    def _parse_controllers(self, library):
        for element in _children(library, "controller"):
            controller_id = element.attrib.get("id", "")
            skin_element = _first(element, "skin")
            morph_element = _first(element, "morph")
            skin = self._parse_skin(skin_element, controller_id) if skin_element is not None else None
            morph = (
                self._parse_morph(morph_element, controller_id)
                if morph_element is not None
                else None
            )
            if skin is None and morph is None:
                self.sink.warn(
                    "EMPTY_CONTROLLER",
                    f"Controller {controller_id!r} has no supported skin or morph",
                    controller_id,
                )
            controller = Controller(
                id=controller_id,
                name=element.attrib.get("name", controller_id),
                skin=skin,
                morph=morph,
            )
            self._put(
                self.document.controllers,
                controller_id,
                controller,
                "controller",
            )

    def _parse_controller_sources(self, element):
        sources = {}
        for source_element in _children(element, "source"):
            source = parse_source(source_element, self.document.namespace, self.sink)
            self._put(sources, source.id, source, "controller source")
        return sources

    def _parse_skin(self, element, controller_id):
        context = f"controller {controller_id}, skin"
        source_ref = self._local_ref(element.attrib.get("source"), context)
        sources = self._parse_controller_sources(element)
        bind_element = _first(element, "bind_shape_matrix")
        if bind_element is None:
            bind_shape = np.eye(4, dtype=np.float64)
        else:
            values = _text_values(bind_element)
            if values.size != 16:
                self.sink.error(
                    "INVALID_BIND_SHAPE_MATRIX",
                    "bind_shape_matrix requires 16 values",
                    context,
                )
            bind_shape = values.reshape(4, 4)

        joints_element = _first(element, "joints")
        if joints_element is None:
            self.sink.error("MISSING_SKIN_JOINTS", "Skin has no joints element", context)
        joint_source_id = ""
        bind_source_id = ""
        for input_element in _children(joints_element, "input"):
            semantic = input_element.attrib.get("semantic", "").upper()
            reference = self._local_ref(input_element.attrib.get("source"), context)
            if semantic == "JOINT":
                joint_source_id = reference
            elif semantic == "INV_BIND_MATRIX":
                bind_source_id = reference
        joint_source = sources.get(joint_source_id)
        bind_source = sources.get(bind_source_id)
        if joint_source is None or bind_source is None:
            self.sink.error(
                "INCOMPLETE_SKIN_JOINTS",
                "Skin JOINT or INV_BIND_MATRIX source is unresolved",
                context,
            )
        joint_names = tuple(str(row[0]) for row in joint_source.data)
        if bind_source.data.shape[1] < 16 or len(bind_source.data) < len(joint_names):
            self.sink.error(
                "INVALID_INVERSE_BIND_SOURCE",
                "Inverse-bind source does not contain one 4x4 matrix per joint",
                context,
            )
        inverse_bind = np.asarray(
            bind_source.data[: len(joint_names), :16],
            dtype=np.float64,
        ).reshape(-1, 4, 4)

        weights_element = _first(element, "vertex_weights")
        if weights_element is None:
            self.sink.error("MISSING_VERTEX_WEIGHTS", "Skin has no vertex_weights", context)
        input_records = []
        max_offset = -1
        for input_element in _children(weights_element, "input"):
            try:
                offset = int(input_element.attrib.get("offset", "0"))
            except ValueError:
                self.sink.error("INVALID_INPUT_OFFSET", "Weight offset is not an integer", context)
            if offset < 0:
                self.sink.error("INVALID_INPUT_OFFSET", "Weight offset is negative", context)
            semantic = input_element.attrib.get("semantic", "").upper()
            reference = self._local_ref(input_element.attrib.get("source"), context)
            input_records.append((semantic, reference, offset))
            max_offset = max(max_offset, offset)
        tuple_width = max_offset + 1
        joint_input = next((item for item in input_records if item[0] == "JOINT"), None)
        weight_input = next((item for item in input_records if item[0] == "WEIGHT"), None)
        if joint_input is None or weight_input is None:
            self.sink.error(
                "INCOMPLETE_VERTEX_WEIGHTS",
                "vertex_weights requires JOINT and WEIGHT inputs",
                context,
            )
        weight_source = sources.get(weight_input[1])
        if weight_source is None:
            self.sink.error(
                "UNRESOLVED_SOURCE",
                f"Missing weight source {weight_input[1]!r}",
                context,
            )
        weight_values = np.asarray(weight_source.data[:, 0], dtype=np.float64)
        vcount = _text_values(_first(weights_element, "vcount"), dtype=np.int64)
        stream = _text_values(_first(weights_element, "v"), dtype=np.int64)
        try:
            declared_count = int(weights_element.attrib.get("count", str(len(vcount))))
        except ValueError:
            self.sink.error("INVALID_VERTEX_WEIGHT_COUNT", "Weight count is not an integer", context)
        if declared_count != len(vcount) or np.any(vcount < 0):
            self.sink.error(
                "VERTEX_WEIGHT_COUNT_MISMATCH",
                "vertex_weights count/vcount is inconsistent",
                context,
            )
        expected_values = int(vcount.sum()) * tuple_width
        if stream.size != expected_values:
            self.sink.error(
                "VERTEX_WEIGHT_STREAM_MISMATCH",
                f"Expected {expected_values} weight indices, found {stream.size}",
                context,
            )
        tuples = stream.reshape(-1, tuple_width)
        result = []
        cursor = 0
        for influence_count in vcount:
            influences = []
            for row in tuples[cursor : cursor + int(influence_count)]:
                joint_index = int(row[joint_input[2]])
                weight_index = int(row[weight_input[2]])
                if joint_index < 0 or joint_index >= len(joint_names):
                    self.sink.error(
                        "INDEX_OUT_OF_BOUNDS",
                        f"Joint index {joint_index} is outside {len(joint_names)} joints",
                        context,
                    )
                if weight_index < 0 or weight_index >= len(weight_values):
                    self.sink.error(
                        "INDEX_OUT_OF_BOUNDS",
                        f"Weight index {weight_index} is outside {len(weight_values)} weights",
                        context,
                    )
                influences.append((joint_index, float(weight_values[weight_index])))
            result.append(tuple(influences))
            cursor += int(influence_count)
        return SkinController(
            source=source_ref,
            bind_shape_matrix=bind_shape,
            joint_names=joint_names,
            inverse_bind_matrices=inverse_bind,
            vertex_weights=tuple(result),
        )

    def _parse_morph(self, element, controller_id):
        context = f"controller {controller_id}, morph"
        source_ref = self._local_ref(element.attrib.get("source"), context)
        method = element.attrib.get("method", "NORMALIZED").upper()
        if method not in {"NORMALIZED", "RELATIVE"}:
            self.sink.error("INVALID_MORPH_METHOD", f"Unsupported morph method {method!r}", context)
        sources = self._parse_controller_sources(element)
        targets_element = _first(element, "targets")
        if targets_element is None:
            self.sink.error("MISSING_MORPH_TARGETS", "Morph has no targets", context)
        target_source_id = ""
        weight_source_id = ""
        for input_element in _children(targets_element, "input"):
            semantic = input_element.attrib.get("semantic", "").upper()
            reference = self._local_ref(input_element.attrib.get("source"), context)
            if semantic == "MORPH_TARGET":
                target_source_id = reference
            elif semantic == "MORPH_WEIGHT":
                weight_source_id = reference
        target_source = sources.get(target_source_id)
        weight_source = sources.get(weight_source_id)
        if target_source is None or weight_source is None:
            self.sink.error(
                "INCOMPLETE_MORPH_TARGETS",
                "Morph target or weight source is unresolved",
                context,
            )
        targets = tuple(str(row[0]) for row in target_source.data)
        weights = np.asarray(weight_source.data[:, 0], dtype=np.float64)
        if len(targets) != len(weights):
            self.sink.error(
                "MORPH_TARGET_COUNT_MISMATCH",
                f"Morph has {len(targets)} targets but {len(weights)} weights",
                context,
            )
        return MorphController(source_ref, method, targets, weights)

    def _parse_library_nodes(self, library):
        for element in _children(library, "node"):
            node = self._parse_node(element)
            self._put(self.document.library_nodes, node.id, node, "library node")

    def _parse_visual_scenes(self, library):
        for element in _children(library, "visual_scene"):
            scene_id = element.attrib.get("id", "")
            nodes = tuple(self._parse_node(node) for node in _children(element, "node"))
            scene = VisualScene(
                id=scene_id,
                name=element.attrib.get("name", scene_id),
                nodes=nodes,
            )
            self._put(self.document.visual_scenes, scene_id, scene, "visual scene")

    def _parse_node(self, element):
        self._node_serial += 1
        node_id = (
            element.attrib.get("id")
            or element.attrib.get("sid")
            or element.attrib.get("name")
            or f"__node_{self._node_serial}"
        )
        transforms = []
        instances = []
        children = []
        for child in element:
            tag = local_name(child.tag)
            if tag in _TRANSFORM_TAGS:
                values = tuple(float(item) for item in _text_values(child))
                expected = {
                    "matrix": 16,
                    "translate": 3,
                    "rotate": 4,
                    "scale": 3,
                    "lookat": 9,
                    "skew": 7,
                }[tag]
                if len(values) != expected:
                    self.sink.error(
                        "INVALID_TRANSFORM",
                        f"{tag} requires {expected} values, found {len(values)}",
                        f"node {node_id}",
                    )
                transforms.append(Transform(tag, values, child.attrib.get("sid", "")))
            elif tag == "node":
                children.append(self._parse_node(child))
            elif tag.startswith("instance_"):
                instance = self._parse_instance(child, node_id)
                if instance is not None:
                    instances.append(instance)
        return Node(
            id=node_id,
            sid=element.attrib.get("sid", ""),
            name=element.attrib.get("name", node_id),
            type=element.attrib.get("type", "NODE").upper(),
            transforms=tuple(transforms),
            instances=tuple(instances),
            children=tuple(children),
            extras=self._simple_extras(element),
        )

    def _parse_instance(self, element, node_id):
        tag = local_name(element.tag)
        kind = tag.removeprefix("instance_")
        if kind not in {"geometry", "controller", "node", "camera", "light"}:
            return None
        context = f"node {node_id}, {tag}"
        url = self._local_ref(element.attrib.get("url"), context)
        skeletons = ()
        if kind == "controller":
            skeletons = tuple(
                self._local_ref(item.text, context)
                for item in _children(element, "skeleton")
                if item.text and item.text.strip()
            )
        materials = self._parse_instance_materials(element, context)
        return Instance(
            kind=kind,
            url=url,
            name=element.attrib.get("name", ""),
            skeletons=skeletons,
            materials=materials,
        )

    def _parse_instance_materials(self, element, context):
        bind_material = _first(element, "bind_material")
        if bind_material is None:
            return ()
        common = _first(bind_material, "technique_common")
        if common is None:
            return ()
        result = []
        for instance in _children(common, "instance_material"):
            symbol = instance.attrib.get("symbol", "")
            target = self._local_ref(instance.attrib.get("target"), context)
            bindings = []
            for binding in _children(instance, "bind_vertex_input"):
                input_set = self._optional_int(
                    binding.attrib.get("input_set", "0"),
                    "bind_vertex_input input_set",
                    context,
                )
                bindings.append(
                    VertexInputBinding(
                        semantic=binding.attrib.get("semantic", ""),
                        input_semantic=binding.attrib.get("input_semantic", "").upper(),
                        input_set=input_set if input_set is not None else 0,
                    )
                )
            result.append(InstanceMaterial(symbol, target, tuple(bindings)))
        return tuple(result)

    def _parse_animations(self, library):
        for element in _children(library, "animation"):
            animation = self._parse_animation(element)
            self._put(
                self.document.animations,
                animation.id,
                animation,
                "animation",
            )

    def _parse_animation(self, element):
        self._animation_serial += 1
        animation_id = element.attrib.get("id") or f"__animation_{self._animation_serial}"
        sources = {}
        for source_element in _children(element, "source"):
            source = parse_source(source_element, self.document.namespace, self.sink)
            self._put(sources, source.id, source, "animation source")
        samplers = {}
        for sampler_element in _children(element, "sampler"):
            sampler_id = sampler_element.attrib.get("id", "")
            inputs = {}
            for input_element in _children(sampler_element, "input"):
                semantic = input_element.attrib.get("semantic", "").upper()
                inputs[semantic] = self._local_ref(
                    input_element.attrib.get("source"),
                    f"animation sampler {sampler_id}",
                )
            self._put(
                samplers,
                sampler_id,
                AnimationSampler(sampler_id, inputs),
                "animation sampler",
            )
        channels = tuple(
            AnimationChannel(
                source=self._local_ref(
                    channel.attrib.get("source"),
                    f"animation {animation_id}",
                ),
                target=channel.attrib.get("target", ""),
            )
            for channel in _children(element, "channel")
        )
        children = tuple(
            self._parse_animation(child) for child in _children(element, "animation")
        )
        return Animation(
            id=animation_id,
            name=element.attrib.get("name", animation_id),
            sources=sources,
            samplers=samplers,
            channels=channels,
            children=children,
        )

    def _parse_animation_clips(self, library):
        for element in _children(library, "animation_clip"):
            clip_id = element.attrib.get("id", "")
            try:
                start = (
                    float(element.attrib["start"]) if "start" in element.attrib else None
                )
                end = float(element.attrib["end"]) if "end" in element.attrib else None
            except ValueError:
                self.sink.error(
                    "INVALID_ANIMATION_CLIP_RANGE",
                    "Animation clip start/end is not numeric",
                    clip_id,
                )
            animations = tuple(
                self._local_ref(instance.attrib.get("url"), f"animation clip {clip_id}")
                for instance in _children(element, "instance_animation")
            )
            clip = AnimationClip(
                id=clip_id,
                name=element.attrib.get("name", clip_id),
                start=start,
                end=end,
                animations=animations,
            )
            self._put(
                self.document.animation_clips,
                clip_id,
                clip,
                "animation clip",
            )

    def _parse_cameras(self, library):
        for element in _children(library, "camera"):
            camera_id = element.attrib.get("id", "")
            optics = _first(element, "optics")
            common = _first(optics, "technique_common") if optics is not None else None
            projection_element = None
            if common is not None:
                projection_element = next(
                    (
                        child
                        for child in common
                        if local_name(child.tag) in {"perspective", "orthographic"}
                    ),
                    None,
                )
            if projection_element is None:
                self.sink.error(
                    "MISSING_CAMERA_PROJECTION",
                    f"Camera {camera_id!r} has no supported projection",
                )
            values = self._float_children(projection_element, f"camera {camera_id}")
            camera = Camera(
                id=camera_id,
                name=element.attrib.get("name", camera_id),
                projection=local_name(projection_element.tag),
                values=values,
            )
            self._put(self.document.cameras, camera_id, camera, "camera")

    def _parse_lights(self, library):
        for element in _children(library, "light"):
            light_id = element.attrib.get("id", "")
            common = _first(element, "technique_common")
            kind_element = next(iter(common), None) if common is not None else None
            if kind_element is None or local_name(kind_element.tag) not in {
                "ambient",
                "directional",
                "point",
                "spot",
            }:
                self.sink.error(
                    "MISSING_LIGHT_TYPE",
                    f"Light {light_id!r} has no supported type",
                )
            color_element = _first(kind_element, "color")
            color_values = _text_values(color_element)
            if color_values.size != 3:
                self.sink.error(
                    "INVALID_LIGHT_COLOR",
                    "Light color requires three values",
                    light_id,
                )
            values = self._float_children(kind_element, f"light {light_id}")
            light = Light(
                id=light_id,
                name=element.attrib.get("name", light_id),
                kind=local_name(kind_element.tag),
                color=tuple(float(item) for item in color_values),
                values=values,
            )
            self._put(self.document.lights, light_id, light, "light")

    def _parse_scene(self, element):
        instance = _first(element, "instance_visual_scene")
        if instance is None:
            self.sink.error(
                "MISSING_ACTIVE_SCENE",
                "Scene has no instance_visual_scene",
            )
        self.document.active_visual_scene = self._local_ref(
            instance.attrib.get("url"),
            "scene",
        )

    def _float_children(self, element, context):
        values = {}
        for child in element:
            if local_name(child.tag) == "color" or not child.text:
                continue
            parsed = _text_values(child)
            if parsed.size != 1:
                continue
            values[local_name(child.tag)] = float(parsed[0])
        return values

    def _simple_extras(self, element):
        values = {}
        for extra in _children(element, "extra"):
            for leaf in extra.iter():
                if len(leaf) or not leaf.text or not leaf.text.strip():
                    continue
                profile = ""
                parent_technique = next(
                    (
                        item
                        for item in extra.iter()
                        if local_name(item.tag) == "technique" and leaf in list(item.iter())
                    ),
                    None,
                )
                if parent_technique is not None:
                    profile = parent_technique.attrib.get("profile", "")
                key = f"{profile}:{local_name(leaf.tag)}" if profile else local_name(leaf.tag)
                values[key] = leaf.text.strip()
        return values

    def _optional_int(self, value, name, context):
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            self.sink.error("INVALID_INTEGER", f"{name} is not an integer", context)

    def _validate_references(self):
        document = self.document
        for material in document.materials.values():
            if material.effect not in document.effects:
                self.sink.error(
                    "UNRESOLVED_EFFECT",
                    f"Material {material.id!r} references missing effect {material.effect!r}",
                )
        for effect in document.effects.values():
            for channel, value in effect.channels.items():
                if value.texture and value.texture.image_id not in document.images:
                    self.sink.warn(
                        "UNRESOLVED_IMAGE",
                        f"Effect {effect.id!r} channel {channel} references missing image "
                        f"{value.texture.image_id!r}",
                        effect.id,
                    )
        for geometry in document.geometries.values():
            for vertices_id, mapping in geometry.vertices.items():
                for source_id in mapping.values():
                    if source_id not in geometry.sources:
                        self.sink.error(
                            "UNRESOLVED_SOURCE",
                            f"Vertices {vertices_id!r} references missing source {source_id!r}",
                            geometry.id,
                        )
        for controller in document.controllers.values():
            source = controller.skin.source if controller.skin else controller.morph.source
            if source not in document.geometries and source not in document.controllers:
                self.sink.error(
                    "UNRESOLVED_CONTROLLER_SOURCE",
                    f"Controller {controller.id!r} references missing source {source!r}",
                )
            if controller.morph:
                for target in controller.morph.targets:
                    if target not in document.geometries and target not in document.controllers:
                        self.sink.error(
                            "UNRESOLVED_MORPH_TARGET",
                            f"Morph controller {controller.id!r} references missing target {target!r}",
                        )

        all_nodes = {}

        def register_node(node):
            all_nodes.setdefault(node.id, []).append(node)
            for child in node.children:
                register_node(child)

        for node in document.library_nodes.values():
            register_node(node)
        for scene in document.visual_scenes.values():
            for node in scene.nodes:
                register_node(node)

        targets = {
            "geometry": document.geometries,
            "controller": document.controllers,
            "node": document.library_nodes,
            "camera": document.cameras,
            "light": document.lights,
        }

        def validate_node(node, instance_stack=()):
            for instance in node.instances:
                mapping = targets[instance.kind]
                if instance.url not in mapping:
                    self.sink.warn(
                        "UNRESOLVED_INSTANCE",
                        f"Node {node.id!r} references missing {instance.kind} {instance.url!r}",
                    )
                    continue
                for binding in instance.materials:
                    if binding.target not in document.materials:
                        self.sink.error(
                            "UNRESOLVED_MATERIAL",
                            f"Material symbol {binding.symbol!r} references "
                            f"missing material {binding.target!r}",
                            node.id,
                        )
                if instance.kind == "node":
                    if instance.url in instance_stack:
                        chain = " -> ".join((*instance_stack, instance.url))
                        self.sink.error(
                            "INSTANCE_NODE_CYCLE",
                            f"Recursive instance_node chain: {chain}",
                            node.id,
                        )
                    validate_node(
                        document.library_nodes[instance.url],
                        (*instance_stack, instance.url),
                    )
            for child in node.children:
                validate_node(child, instance_stack)

        active = document.scene
        for node in active.nodes:
            validate_node(node)

        animation_ids = set(document.animations)

        def collect_animation_ids(animation):
            animation_ids.add(animation.id)
            for child in animation.children:
                collect_animation_ids(child)

        for animation in document.animations.values():
            collect_animation_ids(animation)
        for clip in document.animation_clips.values():
            for animation_id in clip.animations:
                if animation_id not in animation_ids:
                    self.sink.error(
                        "UNRESOLVED_ANIMATION",
                        f"Clip {clip.id!r} references missing animation {animation_id!r}",
                    )


def _namespace_from_tag(tag):
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _validate_root(root_tag, version, sink):
    if local_name(root_tag) != "COLLADA":
        sink.error("INVALID_ROOT", "XML root is not COLLADA")
    if version not in {"1.4.1", "1.5.0"}:
        sink.warn(
            "UNTESTED_COLLADA_VERSION",
            f"COLLADA version {version or '<unspecified>'!r} is not explicitly tested",
        )


def parse_collada(filepath, cancel=None):
    """Parse a file with top-level staged ``iterparse`` element clearing."""
    if not os.path.isfile(filepath):
        raise ColladaError("FILE_NOT_FOUND", f"File not found: {filepath}")
    if cancel is not None and cancel():
        raise ColladaCancelled(f"Import cancelled before parsing {filepath}")
    sink = DiagnosticSink()
    parser = None
    depth = 0
    event_count = 0
    tag_stack = []
    staged_libraries = {
        "library_images",
        "library_effects",
        "library_materials",
        "library_geometries",
        "library_controllers",
        "library_nodes",
        "library_visual_scenes",
        "library_animations",
        "library_animation_clips",
        "library_cameras",
        "library_lights",
    }
    try:
        for event, element in ET.iterparse(filepath, events=("start", "end")):
            event_count += 1
            if cancel is not None and event_count % 256 == 0 and cancel():
                raise ColladaCancelled(f"Import cancelled while parsing {filepath}")
            if event == "start":
                depth += 1
                tag_stack.append(local_name(element.tag))
                if depth == 1:
                    version = element.attrib.get("version", "")
                    _validate_root(element.tag, version, sink)
                    parser = _Parser(_namespace_from_tag(element.tag), version, sink)
                continue
            if (
                depth == 3
                and len(tag_stack) >= 2
                and tag_stack[-2] in staged_libraries
            ):
                wrapper = ET.Element(tag_stack[-2])
                wrapper.append(element)
                parser.parse_top_level(wrapper)
                element.clear()
            elif depth == 2:
                if tag_stack[-1] not in staged_libraries:
                    parser.parse_top_level(element)
                element.clear()
            tag_stack.pop()
            depth -= 1
    except ET.ParseError as exc:
        raise ColladaError("XML_PARSE_ERROR", str(exc), filepath) from exc
    if parser is None:
        raise ColladaError("EMPTY_DOCUMENT", "COLLADA document is empty", filepath)
    if cancel is not None and cancel():
        raise ColladaCancelled(f"Import cancelled after parsing {filepath}")
    return parser.finish()


def parse_collada_string(text):
    """Parse a small in-memory COLLADA document, primarily for unit tests."""
    sink = DiagnosticSink()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ColladaError("XML_PARSE_ERROR", str(exc)) from exc
    version = root.attrib.get("version", "")
    _validate_root(root.tag, version, sink)
    parser = _Parser(_namespace_from_tag(root.tag), version, sink)
    for child in root:
        parser.parse_top_level(child)
    return parser.finish()
