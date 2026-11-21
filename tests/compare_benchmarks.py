# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare current and v1.5.3 benchmark JSON outputs."""

from argparse import ArgumentParser
import json
from pathlib import Path


def main():
    parser = ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("current")
    parser.add_argument("--memory-reduction", type=float, default=0.20)
    parser.add_argument("--max-time-regression", type=float, default=0.10)
    args = parser.parse_args()
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    failures = []
    if baseline["signature"] != current["signature"]:
        failures.append("scene signatures differ")
    baseline_peak = baseline["peak_delta"]
    current_peak = current["peak_delta"]
    if baseline_peak <= 0:
        failures.append("baseline peak delta is not measurable")
    else:
        reduction = 1.0 - current_peak / baseline_peak
        print(f"Peak-memory reduction: {reduction:.1%}")
        if reduction < args.memory_reduction:
            failures.append(
                f"peak-memory reduction {reduction:.1%} is below "
                f"{args.memory_reduction:.1%}"
            )
    baseline_time = baseline["elapsed_seconds"]
    current_time = current["elapsed_seconds"]
    regression = current_time / baseline_time - 1.0
    print(f"Wall-time change: {regression:+.1%}")
    if regression > args.max_time_regression:
        failures.append(
            f"wall-time regression {regression:.1%} exceeds "
            f"{args.max_time_regression:.1%}"
        )
    for failure in failures:
        print(f"FAIL: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
