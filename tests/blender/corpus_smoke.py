# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in Blender smoke imports for a local, uncommitted DAE corpus."""

from argparse import ArgumentParser
from pathlib import Path
import sys
import time

import bpy


REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from simple_collada_importer.importer import import_dae


def arguments():
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--full", action="store_true")
    return parser.parse_args(values)


def contains_skin(path):
    marker = b"<skin"
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if marker in chunk:
                return True
    return False


def select_files(root, full):
    files = sorted(root.rglob("*.dae"), key=lambda path: (path.stat().st_size, str(path)))
    if full or len(files) <= 4:
        return files
    selected = {
        files[0],
        files[len(files) // 2],
        files[-1],
    }
    skinned = next((path for path in files if contains_skin(path)), None)
    if skinned is not None:
        selected.add(skinned)
    return sorted(selected, key=str)


def clear_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for name in ("meshes", "armatures", "materials", "cameras", "lights", "actions"):
        collection = getattr(bpy.data, name)
        for data_block in list(collection):
            if data_block.users == 0:
                collection.remove(data_block)


def main():
    args = arguments()
    root = Path(args.root)
    if not root.is_dir():
        print(f"Corpus root is not a directory: {root}", file=sys.stderr)
        return 2
    files = select_files(root, args.full)
    if not files:
        print(f"No .dae files found under {root}", file=sys.stderr)
        return 2
    failures = []
    for index, path in enumerate(files, 1):
        clear_scene()
        started = time.perf_counter()
        count, armature, error = import_dae(
            str(path),
            bpy.context,
            split_by_material=False,
            recalculate_normals=False,
        )
        duration = time.perf_counter() - started
        if error:
            failures.append((path, error))
            print(f"FAIL {index}/{len(files)} {path}: {error}")
            continue
        meshes = sum(obj.type == "MESH" for obj in bpy.data.objects)
        print(
            f"PASS {index}/{len(files)} {path} "
            f"objects={count} meshes={meshes} armature={bool(armature)} "
            f"seconds={duration:.3f}"
        )
    if failures:
        print(f"{len(failures)} of {len(files)} corpus files failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
