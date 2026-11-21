# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender-independent COLLADA transform evaluation."""

import math

import numpy as np

from .errors import ColladaError
from .model import Asset, Transform


def identity_matrix():
    return np.eye(4, dtype=np.float64)


def _normalize(vector, context):
    length = float(np.linalg.norm(vector))
    if length <= 1e-12:
        raise ColladaError("INVALID_TRANSFORM", f"Zero-length {context} vector")
    return vector / length


def translation_matrix(values):
    result = identity_matrix()
    result[:3, 3] = np.asarray(values[:3], dtype=np.float64)
    return result


def scale_matrix(values):
    result = identity_matrix()
    result[0, 0], result[1, 1], result[2, 2] = values[:3]
    return result


def rotation_matrix(values):
    if len(values) != 4:
        raise ColladaError("INVALID_ROTATE", "Rotate transform requires four values")
    axis = _normalize(np.asarray(values[:3], dtype=np.float64), "rotation axis")
    angle = math.radians(float(values[3]))
    x, y, z = axis
    c = math.cos(angle)
    s = math.sin(angle)
    one_minus_c = 1.0 - c
    result = identity_matrix()
    result[:3, :3] = (
        (c + x * x * one_minus_c, x * y * one_minus_c - z * s, x * z * one_minus_c + y * s),
        (y * x * one_minus_c + z * s, c + y * y * one_minus_c, y * z * one_minus_c - x * s),
        (z * x * one_minus_c - y * s, z * y * one_minus_c + x * s, c + z * z * one_minus_c),
    )
    return result


def look_at_matrix(values):
    if len(values) != 9:
        raise ColladaError("INVALID_LOOKAT", "Look-at transform requires nine values")
    eye = np.asarray(values[:3], dtype=np.float64)
    target = np.asarray(values[3:6], dtype=np.float64)
    up = _normalize(np.asarray(values[6:9], dtype=np.float64), "look-at up")
    z_axis = _normalize(eye - target, "look-at direction")
    x_axis = _normalize(np.cross(up, z_axis), "look-at right")
    y_axis = np.cross(z_axis, x_axis)
    result = identity_matrix()
    result[:3, 0] = x_axis
    result[:3, 1] = y_axis
    result[:3, 2] = z_axis
    result[:3, 3] = eye
    return result


def transform_matrix(transform):
    values = transform.values
    if transform.kind == "matrix":
        if len(values) != 16:
            raise ColladaError("INVALID_MATRIX", "Matrix transform requires 16 values")
        return np.asarray(values, dtype=np.float64).reshape(4, 4)
    if transform.kind == "translate":
        if len(values) != 3:
            raise ColladaError("INVALID_TRANSLATE", "Translate transform requires three values")
        return translation_matrix(values)
    if transform.kind == "rotate":
        return rotation_matrix(values)
    if transform.kind == "scale":
        if len(values) != 3:
            raise ColladaError("INVALID_SCALE", "Scale transform requires three values")
        return scale_matrix(values)
    if transform.kind == "lookat":
        return look_at_matrix(values)
    if transform.kind == "skew":
        raise ColladaError("UNSUPPORTED_SKEW", "Skew transforms are not supported")
    raise ColladaError(
        "UNSUPPORTED_TRANSFORM",
        f"Unsupported transform kind {transform.kind!r}",
    )


def compose_transforms(transforms):
    result = identity_matrix()
    for transform in transforms:
        result = result @ transform_matrix(transform)
    return result


def _axis_vector(axis):
    sign = -1.0 if axis.startswith("-") else 1.0
    name = axis[-1:].upper()
    vectors = {
        "X": np.asarray((1.0, 0.0, 0.0)),
        "Y": np.asarray((0.0, 1.0, 0.0)),
        "Z": np.asarray((0.0, 0.0, 1.0)),
    }
    if name not in vectors:
        raise ColladaError("INVALID_AXIS", f"Unsupported axis {axis!r}")
    return sign * vectors[name]


def _rotation_between(source, target):
    source = _normalize(np.asarray(source, dtype=np.float64), "source axis")
    target = _normalize(np.asarray(target, dtype=np.float64), "target axis")
    dot = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if dot >= 1.0 - 1e-12:
        return identity_matrix()
    if dot <= -1.0 + 1e-12:
        fallback = np.asarray((1.0, 0.0, 0.0))
        if abs(float(np.dot(source, fallback))) > 0.9:
            fallback = np.asarray((0.0, 1.0, 0.0))
        axis = _normalize(np.cross(source, fallback), "axis conversion")
        return rotation_matrix((*axis, 180.0))
    axis = _normalize(np.cross(source, target), "axis conversion")
    angle = math.degrees(math.acos(dot))
    return rotation_matrix((*axis, angle))


def document_correction_matrix(asset, global_scale=1.0, forward_axis="-Y"):
    """Return document-to-Blender axis and linear-unit conversion."""
    if not isinstance(asset, Asset):
        raise TypeError("asset must be an Asset")
    if global_scale <= 0.0:
        raise ColladaError("INVALID_GLOBAL_SCALE", "Global scale must be positive")
    unit_scale = asset.unit_meter * global_scale
    if unit_scale <= 0.0 or not math.isfinite(unit_scale):
        raise ColladaError("INVALID_UNIT", "COLLADA unit meter must be positive and finite")

    if asset.up_axis == "Z_UP":
        up = identity_matrix()
    elif asset.up_axis == "X_UP":
        up = rotation_matrix((0.0, 1.0, 0.0, -90.0))
    elif asset.up_axis == "Y_UP":
        up = rotation_matrix((1.0, 0.0, 0.0, 90.0))
    else:
        raise ColladaError("INVALID_UP_AXIS", f"Unsupported up axis {asset.up_axis!r}")

    selected_forward = _axis_vector(forward_axis)
    selected_up = np.asarray((0.0, 0.0, 1.0))
    if abs(float(np.dot(selected_forward, selected_up))) > 1.0 - 1e-8:
        selected_up = np.asarray((0.0, 1.0, 0.0))
    selected_right = _normalize(
        np.cross(selected_up, selected_forward),
        "source right axis",
    )
    selected_up = _normalize(
        np.cross(selected_forward, selected_right),
        "source up axis",
    )
    target_forward = np.asarray((0.0, -1.0, 0.0))
    target_up = np.asarray((0.0, 0.0, 1.0))
    target_right = _normalize(
        np.cross(target_up, target_forward),
        "target right axis",
    )
    source_basis = np.column_stack(
        (selected_right, selected_forward, selected_up)
    )
    target_basis = np.column_stack((target_right, target_forward, target_up))
    forward = identity_matrix()
    forward[:3, :3] = target_basis @ source_basis.T
    scale = np.diag((unit_scale, unit_scale, unit_scale, 1.0))
    return scale @ forward @ up


def scale_translation(matrix, factor):
    result = np.asarray(matrix, dtype=np.float64).copy()
    result[:3, 3] *= factor
    return result
