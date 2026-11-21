# SPDX-License-Identifier: GPL-3.0-or-later
"""COLLADA animation channel resolution and sampling."""

from dataclasses import dataclass
import re

import numpy as np

from .errors import ColladaError, DiagnosticSink
from .model import Animation, SampledChannel, Transform


_MATRIX_MEMBER = re.compile(r"^(?P<sid>[^.(]+)\((?P<row>\d)\)\((?P<column>\d)\)$")
_ARRAY_MEMBER = re.compile(r"^(?P<sid>[^.(]+)\((?P<index>\d+)\)$")


@dataclass(frozen=True)
class ChannelTarget:
    owner: str
    sid_path: tuple[str, ...]
    member: str = ""
    matrix_row: int | None = None
    matrix_column: int | None = None
    array_index: int | None = None

    @property
    def sid(self):
        return self.sid_path[-1] if self.sid_path else ""


def parse_channel_target(value):
    if "/" not in value:
        raise ColladaError(
            "INVALID_ANIMATION_TARGET",
            f"Animation target {value!r} has no owner/path separator",
        )
    owner, path = value.split("/", 1)
    if not owner or not path:
        raise ColladaError(
            "INVALID_ANIMATION_TARGET",
            f"Animation target {value!r} is incomplete",
        )
    parts = path.split("/")
    terminal = parts[-1]
    matrix_match = _MATRIX_MEMBER.match(terminal)
    if matrix_match:
        parts[-1] = matrix_match.group("sid")
        return ChannelTarget(
            owner=owner,
            sid_path=tuple(parts),
            matrix_row=int(matrix_match.group("row")),
            matrix_column=int(matrix_match.group("column")),
        )
    array_match = _ARRAY_MEMBER.match(terminal)
    if array_match:
        parts[-1] = array_match.group("sid")
        return ChannelTarget(
            owner=owner,
            sid_path=tuple(parts),
            array_index=int(array_match.group("index")),
        )
    if "." in terminal:
        sid, member = terminal.rsplit(".", 1)
        parts[-1] = sid
        return ChannelTarget(owner, tuple(parts), member=member.upper())
    return ChannelTarget(owner, tuple(parts))


def iter_animations(animations):
    for animation in animations:
        yield animation
        yield from iter_animations(animation.children)


def _source_rows(source):
    return np.asarray(source.data)


def resolve_animation_channels(animation, sink=None, with_animation_ids=False):
    """Resolve one animation tree into validated sampler/channel objects."""
    sink = sink or DiagnosticSink()
    resolved = []
    inherited_sources = {}
    inherited_samplers = {}

    def walk(item, parent_sources, parent_samplers):
        sources = dict(parent_sources)
        sources.update(item.sources)
        samplers = dict(parent_samplers)
        samplers.update(item.samplers)
        for channel in item.channels:
            sampler = samplers.get(channel.source)
            context = f"animation {item.id or item.name or '<unnamed>'}"
            if sampler is None:
                sink.error(
                    "UNRESOLVED_ANIMATION_SAMPLER",
                    f"Channel references missing sampler {channel.source!r}",
                    context,
                )
            required = {"INPUT", "OUTPUT"}
            missing = required - sampler.inputs.keys()
            if missing:
                sink.error(
                    "INCOMPLETE_ANIMATION_SAMPLER",
                    f"Sampler {sampler.id!r} lacks {', '.join(sorted(missing))}",
                    context,
                )
            sampler_sources = {}
            for semantic, source_id in sampler.inputs.items():
                source = sources.get(source_id)
                if source is None:
                    sink.error(
                        "UNRESOLVED_ANIMATION_SOURCE",
                        f"Sampler input {semantic} references missing source {source_id!r}",
                        context,
                    )
                sampler_sources[semantic] = source
            input_rows = _source_rows(sampler_sources["INPUT"])
            output_rows = _source_rows(sampler_sources["OUTPUT"])
            if input_rows.shape[1] < 1:
                sink.error(
                    "INVALID_ANIMATION_INPUT",
                    "Animation INPUT source has no time component",
                    context,
                )
            times = np.asarray(input_rows[:, 0], dtype=np.float64)
            values = np.asarray(output_rows, dtype=np.float64)
            if len(times) != len(values):
                sink.error(
                    "ANIMATION_KEY_COUNT_MISMATCH",
                    f"INPUT has {len(times)} keys but OUTPUT has {len(values)}",
                    context,
                )
            if len(times) == 0:
                sink.error("EMPTY_ANIMATION", "Animation channel has no keys", context)
            if np.any(~np.isfinite(times)) or np.any(np.diff(times) < 0.0):
                sink.error(
                    "INVALID_ANIMATION_TIME",
                    "Animation key times must be finite and nondecreasing",
                    context,
                )

            interpolation_source = sampler_sources.get("INTERPOLATION")
            if interpolation_source is None:
                interpolations = ("LINEAR",) * len(times)
            else:
                interpolation_rows = _source_rows(interpolation_source)
                interpolations = tuple(
                    str(row[0]).upper() for row in interpolation_rows
                )
                if len(interpolations) != len(times):
                    sink.error(
                        "ANIMATION_KEY_COUNT_MISMATCH",
                        "INTERPOLATION key count differs from INPUT",
                        context,
                    )
                unsupported = set(interpolations) - {"STEP", "LINEAR", "BEZIER"}
                if unsupported:
                    sink.error(
                        "UNSUPPORTED_INTERPOLATION",
                        f"Unsupported interpolation {sorted(unsupported)[0]!r}",
                        context,
                    )

            in_tangents = sampler_sources.get("IN_TANGENT")
            out_tangents = sampler_sources.get("OUT_TANGENT")
            in_values = (
                np.asarray(_source_rows(in_tangents), dtype=np.float64)
                if in_tangents is not None
                else None
            )
            out_values = (
                np.asarray(_source_rows(out_tangents), dtype=np.float64)
                if out_tangents is not None
                else None
            )
            if "BEZIER" in interpolations and (
                in_values is None or out_values is None
            ):
                sink.error(
                    "MISSING_BEZIER_TANGENT",
                    "BEZIER interpolation requires IN_TANGENT and OUT_TANGENT",
                    context,
                )
            if in_values is not None and len(in_values) != len(times):
                sink.error(
                    "ANIMATION_KEY_COUNT_MISMATCH",
                    "IN_TANGENT key count differs from INPUT",
                    context,
                )
            if out_values is not None and len(out_values) != len(times):
                sink.error(
                    "ANIMATION_KEY_COUNT_MISMATCH",
                    "OUT_TANGENT key count differs from INPUT",
                    context,
                )
            parsed_target = parse_channel_target(channel.target)
            if parsed_target.member == "ANGLE" and values.shape[1] == 1:
                values = values.copy()
                values[:, 0] = unwrap_degrees(values[:, 0])
            sampled_channel = SampledChannel(
                    target=channel.target,
                    times=times,
                    values=values,
                    interpolations=interpolations,
                    in_tangents=in_values,
                    out_tangents=out_values,
            )
            resolved.append(
                (item.id, sampled_channel)
                if with_animation_ids
                else sampled_channel
            )
        for child in item.children:
            walk(child, sources, samplers)

    walk(animation, inherited_sources, inherited_samplers)
    return resolved


def _bezier_coordinate(control, parameter):
    inverse = 1.0 - parameter
    return (
        inverse**3 * control[0]
        + 3.0 * inverse**2 * parameter * control[1]
        + 3.0 * inverse * parameter**2 * control[2]
        + parameter**3 * control[3]
    )


def _solve_bezier_parameter(time, controls):
    low = 0.0
    high = 1.0
    parameter = 0.5
    for _ in range(30):
        parameter = (low + high) * 0.5
        value = _bezier_coordinate(controls, parameter)
        if abs(value - time) <= 1e-9:
            break
        if value < time:
            low = parameter
        else:
            high = parameter
    return parameter


def _tangent_components(row, width, fallback_time, fallback_values):
    row = np.asarray(row, dtype=np.float64).ravel()
    if row.size == width + 1:
        return np.full(width, row[0]), row[1:]
    if row.size == width * 2:
        return row[0::2], row[1::2]
    return np.full(width, fallback_time), np.asarray(fallback_values)


def sample_channel(channel, time):
    """Evaluate a resolved channel at a source-time value."""
    times = channel.times
    if time <= times[0]:
        return channel.values[0].copy()
    if time >= times[-1]:
        return channel.values[-1].copy()
    index = int(np.searchsorted(times, time, side="right") - 1)
    next_index = index + 1
    start_time = float(times[index])
    end_time = float(times[next_index])
    start = channel.values[index]
    end = channel.values[next_index]
    interpolation = channel.interpolations[index]
    if interpolation == "STEP" or end_time <= start_time:
        return start.copy()
    factor = (time - start_time) / (end_time - start_time)
    if interpolation == "LINEAR":
        return start + (end - start) * factor
    if interpolation != "BEZIER":
        raise ColladaError(
            "UNSUPPORTED_INTERPOLATION",
            f"Unsupported interpolation {interpolation!r}",
            channel.target,
        )

    width = int(np.asarray(start).size)
    out_times, out_values = _tangent_components(
        channel.out_tangents[index],
        width,
        start_time + (end_time - start_time) / 3.0,
        start + (end - start) / 3.0,
    )
    in_times, in_values = _tangent_components(
        channel.in_tangents[next_index],
        width,
        end_time - (end_time - start_time) / 3.0,
        end - (end - start) / 3.0,
    )
    result = np.empty(width, dtype=np.float64)
    for component in range(width):
        time_controls = (
            start_time,
            float(out_times[component]),
            float(in_times[component]),
            end_time,
        )
        parameter = _solve_bezier_parameter(time, time_controls)
        value_controls = (
            float(np.ravel(start)[component]),
            float(out_values[component]),
            float(in_values[component]),
            float(np.ravel(end)[component]),
        )
        result[component] = _bezier_coordinate(value_controls, parameter)
    return result.reshape(np.asarray(start).shape)


def union_key_times(channels, start=None, end=None):
    arrays = [channel.times for channel in channels if len(channel.times)]
    if not arrays:
        return np.empty(0, dtype=np.float64)
    times = np.unique(np.concatenate(arrays))
    if start is not None:
        times = times[times >= start]
    if end is not None:
        times = times[times <= end]
    boundaries = []
    if start is not None:
        boundaries.append(float(start))
    if end is not None:
        boundaries.append(float(end))
    if boundaries:
        times = np.unique(np.concatenate((times, boundaries)))
    return times


def unwrap_degrees(values):
    values = np.asarray(values, dtype=np.float64).copy()
    if len(values) < 2:
        return values
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        while delta > 180.0:
            values[index] -= 360.0
            delta -= 360.0
        while delta < -180.0:
            values[index] += 360.0
            delta += 360.0
    return values


def update_transform(transforms, target, sampled_value):
    """Apply a sampled target value to an ordered transform tuple."""
    sampled = np.asarray(sampled_value, dtype=np.float64).ravel()
    transform_index = next(
        (index for index, item in enumerate(transforms) if item.sid == target.sid),
        None,
    )
    if transform_index is None:
        raise ColladaError(
            "UNRESOLVED_ANIMATION_TARGET",
            f"No transform with sid {target.sid!r}",
            target.owner,
        )
    transform = transforms[transform_index]
    values = list(transform.values)
    member_map = {
        "X": 0,
        "Y": 1,
        "Z": 2,
        "ANGLE": 3,
    }
    if target.matrix_row is not None:
        if transform.kind != "matrix":
            raise ColladaError(
                "INVALID_ANIMATION_TARGET",
                "Matrix member target does not reference a matrix transform",
                target.owner,
            )
        values[target.matrix_row * 4 + target.matrix_column] = float(sampled[0])
    elif target.array_index is not None:
        if target.array_index >= len(values):
            raise ColladaError(
                "INVALID_ANIMATION_TARGET",
                f"Target index {target.array_index} exceeds transform width",
                target.owner,
            )
        values[target.array_index] = float(sampled[0])
    elif target.member:
        component = member_map.get(target.member)
        if component is None or component >= len(values):
            raise ColladaError(
                "INVALID_ANIMATION_TARGET",
                f"Unsupported target member {target.member!r}",
                target.owner,
            )
        values[component] = float(sampled[0])
    else:
        if len(sampled) != len(values):
            raise ColladaError(
                "ANIMATION_VALUE_WIDTH_MISMATCH",
                f"Target expects {len(values)} values but sampler produced {len(sampled)}",
                target.owner,
            )
        values = sampled.tolist()
    updated = list(transforms)
    updated[transform_index] = Transform(transform.kind, tuple(values), transform.sid)
    return tuple(updated)
