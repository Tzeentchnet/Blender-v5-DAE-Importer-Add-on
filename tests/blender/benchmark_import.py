# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure one Blender import and emit a machine-readable scene signature."""

from argparse import ArgumentParser
import ctypes
import json
from pathlib import Path
import platform
import sys
import time

import bpy


def arguments():
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--addon-root", required=True)
    parser.add_argument("--output")
    return parser.parse_args(values)


def windows_memory():
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(Counters),
        ctypes.c_ulong,
    )
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(
        process,
        ctypes.byref(counters),
        counters.cb,
    ):
        raise OSError("GetProcessMemoryInfo failed")
    return counters.WorkingSetSize, counters.PeakWorkingSetSize


def process_memory():
    if sys.platform == "win32":
        return windows_memory()
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    return 0, peak


def scene_signature():
    bpy.context.view_layer.update()
    mesh_objects = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    bounds = []
    for obj in mesh_objects:
        for corner in obj.bound_box:
            bounds.append(obj.matrix_world @ __import__("mathutils").Vector(corner))
    if bounds:
        minimum = [round(min(point[index] for point in bounds), 6) for index in range(3)]
        maximum = [round(max(point[index] for point in bounds), 6) for index in range(3)]
    else:
        minimum = maximum = [0.0, 0.0, 0.0]
    return {
        "mesh_objects": len(mesh_objects),
        "object_vertices": sum(len(obj.data.vertices) for obj in mesh_objects),
        "object_polygons": sum(len(obj.data.polygons) for obj in mesh_objects),
        "object_edges": sum(len(obj.data.edges) for obj in mesh_objects),
        "materials": sum(len(obj.material_slots) for obj in mesh_objects),
        "armatures": sum(obj.type == "ARMATURE" for obj in bpy.data.objects),
        "bones": sum(
            len(obj.data.bones) for obj in bpy.data.objects if obj.type == "ARMATURE"
        ),
        "bounds_min": minimum,
        "bounds_max": maximum,
    }


def clear_factory_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for name in ("meshes", "materials", "cameras", "lights"):
        collection = getattr(bpy.data, name)
        for data_block in list(collection):
            if data_block.users == 0:
                collection.remove(data_block)


def main():
    args = arguments()
    addon_root = str(Path(args.addon_root).resolve())
    sys.path.insert(0, addon_root)
    from simple_collada_importer.importer import import_dae

    clear_factory_scene()
    working_before, peak_before = process_memory()
    started = time.perf_counter()
    count, armature, error = import_dae(
        str(Path(args.file).resolve()),
        bpy.context,
        import_rig=True,
        split_by_material=False,
        use_default_material=True,
        recalculate_normals=False,
    )
    elapsed = time.perf_counter() - started
    working_after, peak_after = process_memory()
    payload = {
        "blender": bpy.app.version_string,
        "python": platform.python_version(),
        "file": str(Path(args.file).resolve()),
        "elapsed_seconds": elapsed,
        "working_set_before": working_before,
        "working_set_after": working_after,
        "peak_before": peak_before,
        "peak_after": peak_after,
        "peak_delta": max(
            peak_after - peak_before,
            working_after - working_before,
            0,
        ),
        "imported_count": count,
        "armature": bool(armature),
        "error": error,
        "signature": scene_signature(),
    }
    encoded = json.dumps(payload, sort_keys=True)
    print(f"COLLADA_BENCHMARK={encoded}")
    if args.output:
        Path(args.output).write_text(encoded + "\n", encoding="utf-8")
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
