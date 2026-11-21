# SPDX-License-Identifier: GPL-3.0-or-later
"""Intermediate representation used between COLLADA parsing and Blender."""

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import Diagnostic


@dataclass
class Asset:
    unit_meter: float = 1.0
    unit_name: str = "meter"
    up_axis: str = "Y_UP"


@dataclass
class Source:
    id: str
    data: np.ndarray
    params: tuple[str, ...]
    stride: int
    offset: int = 0
    count: int = 0


@dataclass(frozen=True)
class Input:
    semantic: str
    source: str
    offset: int = 0
    set: int | None = None


@dataclass
class Polygon:
    outer: np.ndarray
    holes: tuple[np.ndarray, ...] = ()


@dataclass
class Primitive:
    kind: str
    count: int
    material: str | None
    inputs: tuple[Input, ...]
    streams: tuple[np.ndarray, ...] = ()
    vcount: np.ndarray | None = None
    polygons: tuple[Polygon, ...] = ()


@dataclass
class Geometry:
    id: str
    name: str
    sources: dict[str, Source] = field(default_factory=dict)
    vertices: dict[str, dict[tuple[str, int | None], str]] = field(
        default_factory=dict
    )
    primitives: list[Primitive] = field(default_factory=list)


@dataclass
class Texture:
    image_id: str
    texcoord: str = ""
    sampler_sid: str = ""
    wrap_s: str = "WRAP"
    wrap_t: str = "WRAP"
    mirror_s: bool = False
    mirror_t: bool = False
    offset: tuple[float, float] = (0.0, 0.0)
    scale: tuple[float, float] = (1.0, 1.0)
    rotation: float = 0.0


@dataclass
class MaterialValue:
    color: tuple[float, float, float, float] | None = None
    scalar: float | None = None
    texture: Texture | None = None


@dataclass
class Effect:
    id: str
    model: str = "lambert"
    channels: dict[str, MaterialValue] = field(default_factory=dict)
    opaque: str = "A_ONE"
    double_sided: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Material:
    id: str
    name: str
    effect: str


@dataclass(frozen=True)
class VertexInputBinding:
    semantic: str
    input_semantic: str
    input_set: int = 0


@dataclass
class InstanceMaterial:
    symbol: str
    target: str
    vertex_inputs: tuple[VertexInputBinding, ...] = ()


@dataclass
class Instance:
    kind: str
    url: str
    name: str = ""
    skeletons: tuple[str, ...] = ()
    materials: tuple[InstanceMaterial, ...] = ()


@dataclass(frozen=True)
class Transform:
    kind: str
    values: tuple[float, ...]
    sid: str = ""


@dataclass
class Node:
    id: str
    sid: str
    name: str
    type: str
    transforms: tuple[Transform, ...] = ()
    instances: tuple[Instance, ...] = ()
    children: tuple["Node", ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisualScene:
    id: str
    name: str
    nodes: tuple[Node, ...]


@dataclass
class SkinController:
    source: str
    bind_shape_matrix: np.ndarray
    joint_names: tuple[str, ...]
    inverse_bind_matrices: np.ndarray
    vertex_weights: tuple[tuple[tuple[int, float], ...], ...]


@dataclass
class MorphController:
    source: str
    method: str
    targets: tuple[str, ...]
    weights: np.ndarray


@dataclass
class Controller:
    id: str
    name: str
    skin: SkinController | None = None
    morph: MorphController | None = None


@dataclass
class AnimationSampler:
    id: str
    inputs: dict[str, str]


@dataclass(frozen=True)
class AnimationChannel:
    source: str
    target: str


@dataclass
class Animation:
    id: str
    name: str
    sources: dict[str, Source] = field(default_factory=dict)
    samplers: dict[str, AnimationSampler] = field(default_factory=dict)
    channels: tuple[AnimationChannel, ...] = ()
    children: tuple["Animation", ...] = ()


@dataclass
class AnimationClip:
    id: str
    name: str
    start: float | None
    end: float | None
    animations: tuple[str, ...]


@dataclass
class Camera:
    id: str
    name: str
    projection: str
    values: dict[str, float] = field(default_factory=dict)


@dataclass
class Light:
    id: str
    name: str
    kind: str
    color: tuple[float, float, float]
    values: dict[str, float] = field(default_factory=dict)


@dataclass
class ColladaDocument:
    namespace: str
    version: str
    asset: Asset = field(default_factory=Asset)
    images: dict[str, str] = field(default_factory=dict)
    effects: dict[str, Effect] = field(default_factory=dict)
    materials: dict[str, Material] = field(default_factory=dict)
    geometries: dict[str, Geometry] = field(default_factory=dict)
    controllers: dict[str, Controller] = field(default_factory=dict)
    library_nodes: dict[str, Node] = field(default_factory=dict)
    visual_scenes: dict[str, VisualScene] = field(default_factory=dict)
    active_visual_scene: str = ""
    animations: dict[str, Animation] = field(default_factory=dict)
    animation_clips: dict[str, AnimationClip] = field(default_factory=dict)
    cameras: dict[str, Camera] = field(default_factory=dict)
    lights: dict[str, Light] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def scene(self):
        return self.visual_scenes.get(self.active_visual_scene)


@dataclass
class DecodedGeometry:
    id: str
    name: str
    vertices: np.ndarray
    triangles: np.ndarray
    edges: np.ndarray
    face_materials: tuple[str | None, ...]
    corner_attributes: dict[tuple[str, int | None], np.ndarray]
    source_vertex_indices: np.ndarray


@dataclass
class SampledChannel:
    target: str
    times: np.ndarray
    values: np.ndarray
    interpolations: tuple[str, ...]
    in_tangents: np.ndarray | None = None
    out_tangents: np.ndarray | None = None
