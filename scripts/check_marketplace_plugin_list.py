#!/usr/bin/env python3
"""Check a `claude plugin list --json` file for the Alice plugin.

Used by the dispatch-only marketplace check in `real-host-ci.yml`. After a
marketplace is added and `alice-memory@alicememory` is installed into a fresh
HOME, the list must hold exactly one plugin, and that plugin must have the id
`alice-memory@alicememory`, be enabled, and report the expected version. The
version is compared as text, so `v0.19.0` does not match `0.19.0`.

Exit 0 when the list matches. Exit 1, with the reason on stderr, when it does
not or when the file cannot be read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_ID = "alice-memory@alicememory"


def plugin_rows(payload: Any) -> list[Any]:
    """The list of plugin rows in a `plugin list --json` payload.

    The command prints a bare list, or an object that holds the list under
    `plugins` or `items`. Anything else has no rows.
    """

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        rows = payload.get("plugins", payload.get("items", []))
        if isinstance(rows, list):
            return rows
    raise ValueError(f"plugin list was not a list: {payload}")


def list_matches(payload: Any, expected_version: str) -> bool:
    rows = plugin_rows(payload)
    matched = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("id") == PLUGIN_ID
        and row.get("enabled") is True
        and str(row.get("version")) == expected_version
    ]
    return len(rows) == 1 and len(matched) == 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not args[0]:
        print("usage: check_marketplace_plugin_list.py <expected-version> <plugins.json>", file=sys.stderr)
        return 1
    expected, path = args
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        matches = list_matches(payload, expected)
    except (OSError, ValueError) as exc:
        print(f"plugin list could not be checked: {exc}", file=sys.stderr)
        return 1
    if not matches:
        print(f"plugin list did not match {expected}: {payload}", file=sys.stderr)
        return 1
    print(f"plugin list matches {PLUGIN_ID} {expected}, enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
