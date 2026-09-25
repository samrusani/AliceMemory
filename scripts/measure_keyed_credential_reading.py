"""Count credential refusals on repo text, and compare keyed reading.

The doors now pass provenance and the import value column as mappings.
This script reports how many tracked markdown lines the floor refuses,
and how a small set of synthetic mappings differs when read keyed versus
by value only. Credential values are built at runtime.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from alicebot_api.credential_floor import carries_credential_material, string_values


def _tracked_markdown() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    listed = subprocess.check_output(["git", "ls-files", "*.md"], cwd=root, text=True)
    return [root / line for line in listed.splitlines() if line]


def _refused_lines(paths: list[Path]) -> int:
    refused = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if carries_credential_material(line):
                refused += 1
    return refused


def _synthetic() -> list[dict[str, str]]:
    opaque = "Xq9mZt2L" + "xP9wKc4BVq7m"
    digest = "ab" * 32
    return [
        {"api_key": opaque},
        {"openclaw_dedupe_key": digest},
        {"rollup_key": "scope:" + digest + ":topic:games"},
        {"stripeKey": opaque},
        {"openaiKey": opaque},
    ]


def main() -> None:
    paths = _tracked_markdown()
    print(f"markdown files: {len(paths)}")
    print(f"refused lines: {_refused_lines(paths)}")
    print("mapping key | keyed | values_only")
    for mapping in _synthetic():
        key = next(iter(mapping))
        keyed = carries_credential_material(mapping)
        values_only = carries_credential_material(*string_values(mapping))
        print(f"{key} | {keyed} | {values_only}")


if __name__ == "__main__":
    main()
