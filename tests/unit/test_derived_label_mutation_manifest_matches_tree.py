"""Every case of the mutation manifest still points at the code it edits.

``scripts/verify_derived_label_mutations.py`` refuses a case whose file digest or line no longer matches, before any
test runs, so a pull request that edits a pinned file without refreshing the manifest leaves cases that cannot be
replayed while every other check stays green. This test reads the manifest against the tree and fails on the first
stale digest, wrong line or missing text.

Mutations: skip the digest comparison (a case with a stale digest passes); skip the line check (a case whose text
moved passes).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "scripts/derived_label_mutations.json"


def _cases() -> list[dict[str, object]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]


def _problems(cases: list[dict[str, object]], root: Path) -> list[str]:
    problems: list[str] = []
    for case in cases:
        for edit in case["edits"]:  # type: ignore[union-attr]
            path = root / edit["file"]
            if not path.is_file():
                problems.append(f"{case['name']}: {edit['file']} is missing")
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != edit["sha256"]:
                problems.append(f"{case['name']}: {edit['file']} digest is stale")
                continue
            lines = data.decode("utf-8").split("\n")
            first = str(edit["before"]).split("\n")[0]
            line = int(edit["line"])
            if not 1 <= line <= len(lines) or first not in lines[line - 1]:
                problems.append(f"{case['name']}: {edit['file']}:{line} does not hold the edited text")
    return problems


def test_every_manifest_case_matches_the_files_it_edits() -> None:
    cases = _cases()
    assert cases
    assert _problems(cases, ROOT) == []


def test_case_names_are_unique() -> None:
    names = [case["name"] for case in _cases()]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("line", 1)])
def test_a_stale_digest_or_line_is_reported(tmp_path: Path, field: str, value: object) -> None:
    target = tmp_path / "module.py"
    target.write_text("first = 1\nguarded = True\n", encoding="utf-8")
    edit = {"file": "module.py", "line": 2, "before": "guarded = True", "after": "guarded = False",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    assert _problems([{"name": "probe", "edits": [edit]}], tmp_path) == []
    edit[field] = value
    assert _problems([{"name": "probe", "edits": [edit]}], tmp_path) != []
