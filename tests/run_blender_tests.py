# SPDX-License-Identifier: GPL-3.0-or-later
"""Launch the integration suite in one or more Blender executables."""

from argparse import ArgumentParser
from pathlib import Path
import os
import subprocess
import sys


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "--blender",
        action="append",
        dest="blenders",
        help="Path to blender executable; may be supplied more than once",
    )
    parser.add_argument(
        "--corpus-root",
        help="Optional local DAE corpus path passed as COLLADA_SMOKE_ROOT",
    )
    parser.add_argument(
        "--full-corpus",
        action="store_true",
        help="Import every DAE under --corpus-root instead of a deterministic sample",
    )
    args = parser.parse_args()
    blenders = args.blenders or [os.environ.get("BLENDER_EXE", "blender")]
    repository = Path(__file__).resolve().parents[1]
    test_script = repository / "tests" / "blender" / "run_tests.py"
    environment = os.environ.copy()
    if args.corpus_root:
        environment["COLLADA_SMOKE_ROOT"] = args.corpus_root

    failed = []
    for executable in blenders:
        command = [
            executable,
            "--background",
            "--factory-startup",
            "--python",
            str(test_script),
        ]
        completed = subprocess.run(
            command,
            cwd=repository,
            env=environment,
            check=False,
        )
        if completed.returncode:
            failed.append((executable, completed.returncode))
            continue
        if args.corpus_root:
            corpus_script = repository / "tests" / "blender" / "corpus_smoke.py"
            corpus_command = [
                executable,
                "--background",
                "--factory-startup",
                "--python",
                str(corpus_script),
                "--",
                "--root",
                args.corpus_root,
            ]
            if args.full_corpus:
                corpus_command.append("--full")
            corpus_completed = subprocess.run(
                corpus_command,
                cwd=repository,
                env=environment,
                check=False,
            )
            if corpus_completed.returncode:
                failed.append((f"{executable} (corpus)", corpus_completed.returncode))
    if failed:
        for executable, returncode in failed:
            print(f"{executable}: failed with exit code {returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
