#!/usr/bin/env python3
"""Combine the coverage data of the unit-test shards and fail closed when a shard left none.

Each unit-test shard writes one coverage data file named ``shard-<n>.coverage``. The
combine step refuses to run unless every expected file is present, readable and
measured at least one source file, because a shard that lost its data would only
lower the combined number and could hide behind a threshold that still passes.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3

from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException

# One name per shard, shared with the workflow that writes the files.
DATA_FILE_TEMPLATE = "shard-{number}.coverage"


def shard_data_files(data_dir: Path, shards: int) -> list[Path]:
    """Return the data file every shard must have written, in shard order."""
    return [data_dir / DATA_FILE_TEMPLATE.format(number=number) for number in range(1, shards + 1)]


def shard_problems(data_files: list[Path]) -> list[str]:
    """Name every shard data file that is missing, empty, unreadable or measured nothing."""
    problems: list[str] = []
    for path in data_files:
        if not path.is_file():
            problems.append(f"missing coverage data: {path}")
            continue
        if path.stat().st_size == 0:
            problems.append(f"empty coverage data: {path}")
            continue
        try:
            data = CoverageData(basename=str(path))
            data.read()
            measured = data.measured_files()
        except (CoverageException, sqlite3.Error, OSError) as exc:
            problems.append(f"unreadable coverage data: {path} ({exc})")
            continue
        if not measured:
            problems.append(f"coverage data measured no source files: {path}")
    return problems


def combine_shards(data_files: list[Path], output: Path) -> None:
    """Merge the shard data into ``output``, replacing anything already stored there."""
    coverage = Coverage(data_file=str(output))
    coverage.combine([str(path) for path in data_files], strict=True, keep=True)
    coverage.save()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--output", type=Path, default=Path(".coverage"))
    args = parser.parse_args(argv)
    if args.shards < 1:
        parser.error("--shards must be at least 1")

    data_files = shard_data_files(args.data_dir, args.shards)
    problems = shard_problems(data_files)
    if problems:
        print("Python shard coverage combine: FAIL")
        for problem in problems:
            print(f" - {problem}")
        return 1
    try:
        combine_shards(data_files, args.output)
    except (CoverageException, sqlite3.Error, OSError) as exc:
        print(f"Python shard coverage combine: FAIL\n - could not combine: {exc}")
        return 1
    print(f"Python shard coverage combine: PASS ({len(data_files)} shards into {args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
