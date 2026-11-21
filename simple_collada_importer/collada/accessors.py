# SPDX-License-Identifier: GPL-3.0-or-later
"""COLLADA source/accessor decoding."""

import numpy as np

from .errors import DiagnosticSink
from .model import Source


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def strip_fragment(value):
    value = (value or "").strip()
    return value[1:] if value.startswith("#") else value


def _parse_array(array_elem, context, sink):
    kind = local_name(array_elem.tag)
    text = array_elem.text or ""
    if kind == "float_array":
        values = np.fromstring(text, dtype=np.float64, sep=" ")
    elif kind == "int_array":
        values = np.fromstring(text, dtype=np.int64, sep=" ")
    elif kind == "bool_array":
        tokens = text.split()
        invalid = [token for token in tokens if token.lower() not in {"true", "false", "1", "0"}]
        if invalid:
            sink.error(
                "INVALID_BOOL_ARRAY",
                f"Invalid boolean token {invalid[0]!r}",
                context,
            )
        values = np.asarray(
            [token.lower() in {"true", "1"} for token in tokens],
            dtype=np.bool_,
        )
    elif kind in {"Name_array", "IDREF_array"}:
        values = np.asarray(text.split(), dtype=object)
    else:
        sink.error(
            "UNSUPPORTED_SOURCE_ARRAY",
            f"Unsupported source array element <{kind}>",
            context,
        )

    declared = array_elem.attrib.get("count")
    if declared is not None:
        try:
            declared_count = int(declared)
        except ValueError:
            sink.error("INVALID_ARRAY_COUNT", f"Invalid count {declared!r}", context)
        if declared_count != values.size:
            sink.warn(
                "ARRAY_COUNT_MISMATCH",
                f"Declared {declared_count} values but found {values.size}",
                context,
            )
    return values


def parse_source(source_elem, namespace="", sink=None):
    """Decode one COLLADA ``<source>`` and its accessor strictly."""
    sink = sink or DiagnosticSink()
    source_id = source_elem.attrib.get("id", "")
    context = f"source {source_id or '<unnamed>'}"
    if not source_id:
        sink.error("MISSING_SOURCE_ID", "Source has no id", context)

    array_elem = next(
        (
            child
            for child in source_elem
            if local_name(child.tag)
            in {"float_array", "int_array", "bool_array", "Name_array", "IDREF_array"}
        ),
        None,
    )
    if array_elem is None:
        sink.error("MISSING_SOURCE_ARRAY", "Source has no supported array", context)
    flat = _parse_array(array_elem, context, sink)

    accessor = None
    for child in source_elem:
        if local_name(child.tag) != "technique_common":
            continue
        accessor = next(
            (item for item in child if local_name(item.tag) == "accessor"),
            None,
        )
        break

    if accessor is None:
        stride = 1
        offset = 0
        count = int(flat.size)
        params = ("VALUE",)
    else:
        accessor_source = strip_fragment(accessor.attrib.get("source"))
        array_id = array_elem.attrib.get("id", "")
        if accessor_source and array_id and accessor_source != array_id:
            sink.error(
                "ACCESSOR_SOURCE_MISMATCH",
                f"Accessor references {accessor_source!r}, expected {array_id!r}",
                context,
            )
        try:
            offset = int(accessor.attrib.get("offset", "0"))
            stride = int(accessor.attrib.get("stride", "1"))
            count = int(
                accessor.attrib.get(
                    "count",
                    str(max((flat.size - offset) // max(stride, 1), 0)),
                )
            )
        except ValueError as exc:
            sink.error("INVALID_ACCESSOR", f"Invalid numeric accessor attribute: {exc}", context)
        if offset < 0 or stride <= 0 or count < 0:
            sink.error(
                "INVALID_ACCESSOR",
                "Accessor offset/count must be non-negative and stride must be positive",
                context,
            )
        params = tuple(
            param.attrib.get("name", "")
            for param in accessor
            if local_name(param.tag) == "param"
        )
        if len(params) > stride:
            sink.error(
                "INVALID_ACCESSOR_PARAMS",
                f"Accessor has {len(params)} params with stride {stride}",
                context,
            )
        if not params:
            params = tuple(f"VALUE{i}" for i in range(stride))

    required = offset + count * stride
    if required > flat.size:
        sink.error(
            "ACCESSOR_OUT_OF_BOUNDS",
            f"Accessor needs {required} array values but only {flat.size} exist",
            context,
        )

    if count == 0:
        data = np.empty((0, stride), dtype=flat.dtype)
    else:
        data = flat[offset:required].reshape(count, stride)

    return Source(
        id=source_id,
        data=data,
        params=params,
        stride=stride,
        offset=offset,
        count=count,
    )


def component_indices(source, names, default=()):
    """Return source-column indices for named accessor parameters."""
    normalized = {name.upper(): index for index, name in enumerate(source.params) if name}
    found = tuple(normalized[name.upper()] for name in names if name.upper() in normalized)
    return found or tuple(default)


def source_components(source, names, width=None):
    """Extract named columns, falling back to leading columns when unnamed."""
    indices = component_indices(source, names)
    if not indices:
        wanted = width if width is not None else min(len(names), source.stride)
        indices = tuple(range(wanted))
    result = source.data[:, indices]
    if result.ndim == 1:
        result = result.reshape(-1, 1)
    if width is not None and result.shape[1] < width:
        raise ValueError(
            f"Source {source.id!r} exposes {result.shape[1]} components; {width} required"
        )
    return result[:, :width] if width is not None else result
