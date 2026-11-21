# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate a deterministic large COLLADA fixture outside the repository."""

from argparse import ArgumentParser
from pathlib import Path


def main():
    parser = ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--triangles", type=int, default=100_000)
    parser.add_argument("--instances", type=int, default=16)
    args = parser.parse_args()
    if args.triangles <= 0 or args.instances <= 0:
        parser.error("--triangles and --instances must be positive")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    vertex_count = args.triangles * 3
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n'
            '  <asset><unit meter="1"/><up_axis>Z_UP</up_axis></asset>\n'
            '  <library_geometries><geometry id="benchmark-geometry"><mesh>\n'
            '    <source id="positions">\n'
            f'      <float_array id="positions-array" count="{vertex_count * 3}">'
        )
        for triangle in range(args.triangles):
            x = triangle % 1000
            y = triangle // 1000
            stream.write(
                f"{x} {y} 0 {x + 0.9} {y} 0 {x} {y + 0.9} 0 "
            )
        stream.write(
            "</float_array>\n"
            "      <technique_common>"
            f'<accessor source="#positions-array" count="{vertex_count}" stride="3">'
            '<param name="X"/><param name="Y"/><param name="Z"/>'
            "</accessor></technique_common>\n"
            "    </source>\n"
            '    <vertices id="vertices"><input semantic="POSITION" source="#positions"/></vertices>\n'
            f'    <triangles count="{args.triangles}"><input semantic="VERTEX" source="#vertices"/><p>'
        )
        for vertex in range(vertex_count):
            stream.write(f"{vertex} ")
        stream.write(
            "</p></triangles>\n"
            "  </mesh></geometry></library_geometries>\n"
            '  <library_visual_scenes><visual_scene id="Scene">\n'
        )
        for index in range(args.instances):
            stream.write(
                f'    <node id="instance-{index}"><translate>{index * 2} 0 0</translate>'
                '<instance_geometry url="#benchmark-geometry"/></node>\n'
            )
        stream.write(
            "  </visual_scene></library_visual_scenes>\n"
            '  <scene><instance_visual_scene url="#Scene"/></scene>\n'
            "</COLLADA>\n"
        )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
