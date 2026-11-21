# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated primitive decoding and concave-safe triangulation."""

from dataclasses import dataclass

import numpy as np

from .accessors import source_components
from .errors import ColladaError, DiagnosticSink
from .model import DecodedGeometry, Geometry, Input


_SURFACE_KINDS = {"triangles", "polylist", "polygons", "tristrips", "trifans"}
_LINE_KINDS = {"lines", "linestrips"}


@dataclass(frozen=True)
class _Binding:
    semantic: str
    source: str
    offset: int
    set: int | None

    @property
    def key(self):
        return self.semantic, self.set


def _signed_area(points):
    x = points[:, 0]
    y = points[:, 1]
    return float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y) * 0.5)


def _project_polygon(points):
    if len(points) < 3:
        raise ColladaError("INVALID_POLYGON", "A polygon requires at least three corners")
    normal = np.zeros(3, dtype=np.float64)
    for index, current in enumerate(points):
        following = points[(index + 1) % len(points)]
        normal[0] += (current[1] - following[1]) * (current[2] + following[2])
        normal[1] += (current[2] - following[2]) * (current[0] + following[0])
        normal[2] += (current[0] - following[0]) * (current[1] + following[1])
    axis = int(np.argmax(np.abs(normal)))
    if abs(float(normal[axis])) <= 1e-12:
        raise ColladaError("DEGENERATE_POLYGON", "Polygon has no stable plane")
    return np.delete(np.asarray(points, dtype=np.float64), axis, axis=1)


def _cross_2d(a, b, c):
    return float((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))


def _point_in_triangle(point, a, b, c, epsilon=1e-12):
    ab = _cross_2d(a, b, point)
    bc = _cross_2d(b, c, point)
    ca = _cross_2d(c, a, point)
    return ab > epsilon and bc > epsilon and ca > epsilon


def triangulate_polygon(points):
    """Return triangle corner indices for one simple 3D polygon."""
    points_2d = _project_polygon(points)
    original_ccw = _signed_area(points_2d) > 0.0
    remaining = list(range(len(points_2d)))
    if not original_ccw:
        remaining.reverse()

    triangles = []
    guard = len(remaining) * len(remaining)
    while len(remaining) > 3 and guard > 0:
        guard -= 1
        ear_found = False
        for cursor, current in enumerate(remaining):
            previous = remaining[cursor - 1]
            following = remaining[(cursor + 1) % len(remaining)]
            a, b, c = points_2d[previous], points_2d[current], points_2d[following]
            cross = _cross_2d(a, b, c)
            if cross <= 1e-12:
                if abs(cross) <= 1e-12:
                    remaining.pop(cursor)
                    ear_found = True
                    break
                continue
            if any(
                _point_in_triangle(points_2d[index], a, b, c)
                for index in remaining
                if index not in {previous, current, following}
            ):
                continue
            triangle = (previous, current, following)
            triangles.append(triangle if original_ccw else tuple(reversed(triangle)))
            remaining.pop(cursor)
            ear_found = True
            break
        if not ear_found:
            raise ColladaError(
                "TRIANGULATION_FAILED",
                "Polygon is self-intersecting or numerically degenerate",
            )

    if len(remaining) == 3:
        triangle = tuple(remaining)
        triangles.append(triangle if original_ccw else tuple(reversed(triangle)))
    if not triangles:
        raise ColladaError("TRIANGULATION_FAILED", "Polygon produced no triangles")
    return np.asarray(triangles, dtype=np.int64)


def _orientation(a, b, c):
    value = _cross_2d(a, b, c)
    if abs(value) <= 1e-12:
        return 0
    return 1 if value > 0.0 else -1


def _segments_intersect(a, b, c, d):
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)
    return o1 != o2 and o3 != o4


def _bridge_holes(outer_points, hole_points):
    """Return a simple ring and flattened-corner map for polygon holes."""
    all_points = [np.asarray(outer_points, dtype=np.float64)]
    all_points.extend(np.asarray(points, dtype=np.float64) for points in hole_points)
    projected_all = [_project_polygon(points) for points in all_points]

    ring = [(0, index) for index in range(len(all_points[0]))]
    outer_projected = projected_all[0]
    if _signed_area(outer_projected) < 0.0:
        ring.reverse()

    for hole_number, hole in enumerate(projected_all[1:], start=1):
        hole_ring = [(hole_number, index) for index in range(len(hole))]
        if _signed_area(hole) > 0.0:
            hole_ring.reverse()
        hole_cursor = max(
            range(len(hole_ring)),
            key=lambda index: (
                hole[hole_ring[index][1], 0],
                -hole[hole_ring[index][1], 1],
            ),
        )
        hole_key = hole_ring[hole_cursor]
        hole_point = hole[hole_key[1]]

        candidates = []
        ring_points = [projected_all[loop][index] for loop, index in ring]
        for ring_cursor, ring_key in enumerate(ring):
            ring_point = ring_points[ring_cursor]
            blocked = False
            for edge_index, edge_a in enumerate(ring_points):
                edge_b = ring_points[(edge_index + 1) % len(ring_points)]
                if ring_cursor in {edge_index, (edge_index + 1) % len(ring_points)}:
                    continue
                if _segments_intersect(hole_point, ring_point, edge_a, edge_b):
                    blocked = True
                    break
            if not blocked:
                candidates.append(
                    (float(np.sum((ring_point - hole_point) ** 2)), ring_cursor)
                )
        if not candidates:
            raise ColladaError(
                "TRIANGULATION_FAILED",
                "Could not connect a polygon hole to its outer boundary",
            )
        _, outer_cursor = min(candidates)
        rotated_hole = hole_ring[hole_cursor:] + hole_ring[:hole_cursor]
        bridge_outer = ring[outer_cursor]
        ring = (
            ring[: outer_cursor + 1]
            + rotated_hole
            + [rotated_hole[0], bridge_outer]
            + ring[outer_cursor + 1 :]
        )

    merged_points = np.asarray(
        [all_points[loop][index] for loop, index in ring],
        dtype=np.float64,
    )
    return merged_points, ring


def triangulate_polygon_with_holes(outer_points, holes):
    if not holes:
        triangles = triangulate_polygon(outer_points)
        return triangles, [(0, index) for index in range(len(outer_points))]
    merged, corner_map = _bridge_holes(outer_points, holes)
    return triangulate_polygon(merged), corner_map


def _expanded_bindings(geometry, primitive, sink):
    bindings = []
    for item in primitive.inputs:
        if item.semantic == "VERTEX":
            vertex_inputs = geometry.vertices.get(item.source)
            if vertex_inputs is None:
                sink.error(
                    "UNRESOLVED_VERTICES",
                    f"Primitive references missing vertices {item.source!r}",
                    geometry.id,
                )
            for (semantic, set_index), source in vertex_inputs.items():
                bindings.append(_Binding(semantic, source, item.offset, set_index))
        else:
            bindings.append(
                _Binding(item.semantic, item.source, item.offset, item.set)
            )
    if not bindings:
        sink.error("MISSING_PRIMITIVE_INPUT", "Primitive has no inputs", geometry.id)
    return bindings


def _source_values(source, semantic):
    if semantic == "POSITION":
        try:
            return source_components(source, ("X", "Y", "Z"), width=3).astype(
                np.float64, copy=False
            )
        except ValueError as exc:
            raise ColladaError("INVALID_POSITION_SOURCE", str(exc), source.id) from exc
    if semantic == "NORMAL":
        try:
            return source_components(source, ("X", "Y", "Z"), width=3).astype(
                np.float64, copy=False
            )
        except ValueError as exc:
            raise ColladaError("INVALID_NORMAL_SOURCE", str(exc), source.id) from exc
    if semantic == "TEXCOORD":
        values = source_components(source, ("S", "T"), width=2)
        return values.astype(np.float64, copy=False)
    if semantic == "COLOR":
        values = source_components(source, ("R", "G", "B", "A"))
        if values.shape[1] < 3:
            raise ColladaError(
                "INVALID_COLOR_SOURCE",
                "Color source requires at least RGB components",
                source.id,
            )
        if values.shape[1] == 3:
            values = np.concatenate(
                (values, np.ones((len(values), 1), dtype=values.dtype)),
                axis=1,
            )
        return values[:, :4].astype(np.float64, copy=False)
    return np.asarray(source.data)


def _default_attribute(semantic, width):
    if semantic == "NORMAL":
        return np.asarray((0.0, 0.0, 1.0), dtype=np.float64)
    if semantic == "COLOR":
        return np.ones(width, dtype=np.float64)
    return np.zeros(width, dtype=np.float64)


def _validate_stream(stream, width, context, expected_corners=None):
    stream = np.asarray(stream, dtype=np.int64)
    if stream.size % width:
        raise ColladaError(
            "INVALID_INDEX_STREAM_WIDTH",
            f"Index stream has {stream.size} values for tuple width {width}",
            context,
        )
    tuples = stream.reshape(-1, width)
    if expected_corners is not None and len(tuples) != expected_corners:
        raise ColladaError(
            "PRIMITIVE_COUNT_MISMATCH",
            f"Expected {expected_corners} corners but found {len(tuples)}",
            context,
        )
    return tuples


def _primitive_corner_groups(primitive, width, context):
    """Yield (outer tuples, hole tuple arrays) for surface primitives."""
    if primitive.kind == "triangles":
        if len(primitive.streams) != 1:
            raise ColladaError("INVALID_PRIMITIVE", "Triangles require one <p>", context)
        tuples = _validate_stream(
            primitive.streams[0],
            width,
            context,
            expected_corners=primitive.count * 3,
        )
        for start in range(0, len(tuples), 3):
            yield tuples[start : start + 3], ()
        return

    if primitive.kind == "polylist":
        if len(primitive.streams) != 1 or primitive.vcount is None:
            raise ColladaError(
                "INVALID_PRIMITIVE",
                "Polylist requires one <p> and <vcount>",
                context,
            )
        counts = np.asarray(primitive.vcount, dtype=np.int64)
        if len(counts) != primitive.count or np.any(counts < 0):
            raise ColladaError(
                "PRIMITIVE_COUNT_MISMATCH",
                "Polylist vcount does not match its declared count",
                context,
            )
        tuples = _validate_stream(
            primitive.streams[0],
            width,
            context,
            expected_corners=int(counts.sum()),
        )
        cursor = 0
        for count in counts:
            next_cursor = cursor + int(count)
            yield tuples[cursor:next_cursor], ()
            cursor = next_cursor
        return

    if primitive.kind == "polygons":
        if len(primitive.polygons) != primitive.count:
            raise ColladaError(
                "PRIMITIVE_COUNT_MISMATCH",
                f"Declared {primitive.count} polygons but found {len(primitive.polygons)}",
                context,
            )
        for polygon in primitive.polygons:
            outer = _validate_stream(polygon.outer, width, context)
            holes = tuple(_validate_stream(hole, width, context) for hole in polygon.holes)
            yield outer, holes
        return

    if primitive.kind in {"tristrips", "trifans"}:
        if len(primitive.streams) != primitive.count:
            raise ColladaError(
                "PRIMITIVE_COUNT_MISMATCH",
                f"Declared {primitive.count} {primitive.kind} but found {len(primitive.streams)}",
                context,
            )
        for stream in primitive.streams:
            yield _validate_stream(stream, width, context), ()
        return

    raise ColladaError(
        "UNSUPPORTED_PRIMITIVE",
        f"Unsupported surface primitive {primitive.kind!r}",
        context,
    )


def _surface_triangles(kind, corner_count, points, holes=()):
    if corner_count < 3:
        raise ColladaError("INVALID_POLYGON", "Surface has fewer than three corners")
    if kind == "triangles":
        return np.asarray(((0, 1, 2),), dtype=np.int64), [(0, i) for i in range(3)]
    if kind == "tristrips":
        triangles = []
        for index in range(corner_count - 2):
            triangle = (
                (index, index + 1, index + 2)
                if index % 2 == 0
                else (index + 1, index, index + 2)
            )
            triangles.append(triangle)
        return np.asarray(triangles, dtype=np.int64), [
            (0, index) for index in range(corner_count)
        ]
    if kind == "trifans":
        triangles = [
            (0, index, index + 1) for index in range(1, corner_count - 1)
        ]
        return np.asarray(triangles, dtype=np.int64), [
            (0, index) for index in range(corner_count)
        ]
    return triangulate_polygon_with_holes(points, holes)


def decode_geometry(geometry, preserve_vertex_order=False, sink=None):
    """Decode and validate one geometry into Blender-ready arrays."""
    sink = sink or DiagnosticSink()
    if not geometry.primitives:
        sink.error("EMPTY_GEOMETRY", "Geometry has no supported primitives", geometry.id)

    primitive_bindings = [
        _expanded_bindings(geometry, primitive, sink)
        for primitive in geometry.primitives
    ]

    position_source_ids = []
    attribute_layout = {}
    for bindings in primitive_bindings:
        for binding in bindings:
            source = geometry.sources.get(binding.source)
            if source is None:
                sink.error(
                    "UNRESOLVED_SOURCE",
                    f"Input {binding.semantic} references missing source {binding.source!r}",
                    geometry.id,
                )
            if binding.semantic == "POSITION":
                if binding.source not in position_source_ids:
                    position_source_ids.append(binding.source)
            elif binding.semantic not in {"VERTEX"}:
                values = _source_values(source, binding.semantic)
                previous = attribute_layout.get(binding.key)
                if previous is not None and previous != values.shape[1]:
                    sink.error(
                        "ATTRIBUTE_WIDTH_MISMATCH",
                        f"Attribute {binding.key} has inconsistent widths",
                        geometry.id,
                    )
                attribute_layout[binding.key] = values.shape[1]

    if not position_source_ids:
        sink.error("MISSING_POSITION", "Geometry has no POSITION input", geometry.id)

    vertices_parts = []
    source_bases = {}
    source_vertex_indices = []
    for source_id in position_source_ids:
        values = _source_values(geometry.sources[source_id], "POSITION")
        source_bases[source_id] = sum(len(part) for part in vertices_parts)
        vertices_parts.append(values)
        source_vertex_indices.extend(range(len(values)))
    vertices = np.concatenate(vertices_parts, axis=0)

    triangles = []
    edges = []
    face_materials = []
    corner_attributes = {key: [] for key in attribute_layout}

    for primitive, bindings in zip(geometry.primitives, primitive_bindings):
        context = f"geometry {geometry.id}, {primitive.kind}"
        width = max(item.offset for item in primitive.inputs) + 1
        binding_by_key = {}
        position_binding = None
        binding_values = {}
        for binding in bindings:
            if binding.offset >= width:
                sink.error(
                    "INVALID_INPUT_OFFSET",
                    f"Input offset {binding.offset} exceeds tuple width {width}",
                    context,
                )
            source = geometry.sources[binding.source]
            values = _source_values(source, binding.semantic)
            binding_values[binding] = values
            if binding.semantic == "POSITION":
                if position_binding is not None and position_binding != binding:
                    sink.error(
                        "AMBIGUOUS_POSITION",
                        "Primitive resolves more than one POSITION input",
                        context,
                    )
                position_binding = binding
            else:
                binding_by_key.setdefault(binding.key, binding)
        if position_binding is None:
            sink.error("MISSING_POSITION", "Primitive has no POSITION input", context)

        def validate_indices(tuples):
            for binding in bindings:
                indices = tuples[:, binding.offset]
                if np.any(indices < 0):
                    sink.error(
                        "NEGATIVE_INDEX",
                        f"{binding.semantic} contains a negative index",
                        context,
                    )
                limit = len(binding_values[binding])
                if np.any(indices >= limit):
                    bad = int(indices[indices >= limit][0])
                    sink.error(
                        "INDEX_OUT_OF_BOUNDS",
                        f"{binding.semantic} index {bad} is outside source size {limit}",
                        context,
                    )

        def append_triangle(corner_tuples):
            validate_indices(corner_tuples)
            position_indices = corner_tuples[:, position_binding.offset]
            global_indices = (
                position_indices + source_bases[position_binding.source]
            ).astype(np.int64)
            if len(set(global_indices.tolist())) < 3:
                sink.warn(
                    "DEGENERATE_TRIANGLE",
                    "Skipped triangle with repeated position indices",
                    context,
                )
                return
            triangles.append(global_indices.tolist())
            face_materials.append(primitive.material)
            for key, width_value in attribute_layout.items():
                binding = binding_by_key.get(key)
                if binding is None:
                    value = _default_attribute(key[0], width_value)
                    corner_attributes[key].extend([value.copy() for _ in range(3)])
                    continue
                indices = corner_tuples[:, binding.offset]
                corner_attributes[key].extend(binding_values[binding][indices])

        if primitive.kind in _SURFACE_KINDS:
            for outer, hole_tuples in _primitive_corner_groups(
                primitive, width, context
            ):
                validate_indices(outer)
                for hole in hole_tuples:
                    validate_indices(hole)
                position_values = binding_values[position_binding]
                outer_points = position_values[outer[:, position_binding.offset]]
                hole_points = [
                    position_values[hole[:, position_binding.offset]]
                    for hole in hole_tuples
                ]
                local_triangles, corner_map = _surface_triangles(
                    primitive.kind,
                    len(outer),
                    outer_points,
                    holes=hole_points,
                )
                all_loops = (outer,) + tuple(hole_tuples)
                mapped_tuples = np.asarray(
                    [all_loops[loop][corner] for loop, corner in corner_map],
                    dtype=np.int64,
                )
                for local_triangle in local_triangles:
                    append_triangle(mapped_tuples[np.asarray(local_triangle)])
            continue

        if primitive.kind not in _LINE_KINDS:
            sink.error(
                "UNSUPPORTED_PRIMITIVE",
                f"Unsupported primitive {primitive.kind!r}",
                context,
            )
        if len(primitive.streams) != (1 if primitive.kind == "lines" else primitive.count):
            sink.error(
                "PRIMITIVE_COUNT_MISMATCH",
                f"Unexpected number of streams for {primitive.kind}",
                context,
            )
        line_streams = primitive.streams
        for stream in line_streams:
            expected = primitive.count * 2 if primitive.kind == "lines" else None
            tuples = _validate_stream(stream, width, context, expected)
            validate_indices(tuples)
            position_indices = (
                tuples[:, position_binding.offset]
                + source_bases[position_binding.source]
            ).astype(np.int64)
            if primitive.kind == "lines":
                pairs = position_indices.reshape(-1, 2)
            else:
                pairs = np.column_stack((position_indices[:-1], position_indices[1:]))
            for first, second in pairs:
                if first == second:
                    sink.warn(
                        "DEGENERATE_LINE",
                        "Skipped line with identical endpoints",
                        context,
                    )
                else:
                    edges.append((int(first), int(second)))

    triangle_array = (
        np.asarray(triangles, dtype=np.int32).reshape(-1, 3)
        if triangles
        else np.empty((0, 3), dtype=np.int32)
    )
    edge_array = (
        np.asarray(edges, dtype=np.int32).reshape(-1, 2)
        if edges
        else np.empty((0, 2), dtype=np.int32)
    )
    if len(triangle_array) == 0 and len(edge_array) == 0:
        sink.error("EMPTY_GEOMETRY", "Geometry produced no faces or edges", geometry.id)

    finalized_attributes = {}
    expected_corners = len(triangle_array) * 3
    for key, values in corner_attributes.items():
        array = np.asarray(values, dtype=np.float64)
        if len(array) != expected_corners:
            sink.error(
                "ATTRIBUTE_CORNER_MISMATCH",
                f"Attribute {key} has {len(array)} corners; expected {expected_corners}",
                geometry.id,
            )
        finalized_attributes[key] = array

    return DecodedGeometry(
        id=geometry.id,
        name=geometry.name,
        vertices=np.asarray(vertices, dtype=np.float64),
        triangles=triangle_array,
        edges=edge_array,
        face_materials=tuple(face_materials),
        corner_attributes=finalized_attributes,
        source_vertex_indices=np.asarray(source_vertex_indices, dtype=np.int64),
    )
