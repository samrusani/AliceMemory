"""Every case of the mutation manifest still points at the code it edits.

``scripts/verify_derived_label_mutations.py`` refuses a case whose file digest, recorded line or exact ``before`` text no
longer matches, before any test runs, so a pull request that edits a pinned file without refreshing the manifest leaves
cases that cannot be replayed while every other check stays green. This test runs the checks of that script itself
(``check_edit``, loaded from the script, not a copy of it) on every case and fails on the first stale digest, wrong line
or missing text. A case whose ``before`` spans several lines is checked on every line, not only on the first.

Mutations: skip the digest comparison in ``check_edit`` (a case with a stale digest passes); skip the span check in
``check_edit`` (a case whose text moved, or whose later lines changed, passes).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "scripts/derived_label_mutations.json"
VERIFIER = ROOT / "scripts/verify_derived_label_mutations.py"


def _verifier():
    spec = importlib.util.spec_from_file_location("verify_derived_label_mutations", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cases() -> list[dict[str, object]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]


def _problems(cases: list[dict[str, object]], root: Path) -> list[str]:
    """Why each case could not be replayed against ``root``: the reasons the replay itself gives."""

    check_edit = _verifier().check_edit
    problems: list[str] = []
    for case in cases:
        for edit in case["edits"]:  # type: ignore[union-attr]
            try:
                check_edit(root, edit)
            except (ValueError, OSError, KeyError) as exc:
                problems.append(f"{case['name']}: {type(exc).__name__}: {exc}")
    return problems


def test_every_manifest_case_matches_the_files_it_edits() -> None:
    cases = _cases()
    assert cases
    assert _problems(cases, ROOT) == []


def test_case_names_are_unique() -> None:
    names = [case["name"] for case in _cases()]
    assert len(names) == len(set(names))


def _edit(target: Path, before: str, after: str, line: int) -> dict[str, object]:
    return {"file": target.name, "line": line, "before": before, "after": after,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("line", 1)])
def test_a_stale_digest_or_line_is_reported(tmp_path: Path, field: str, value: object) -> None:
    target = tmp_path / "module.py"
    target.write_text("first = 1\nguarded = True\n", encoding="utf-8")
    edit = _edit(target, "guarded = True", "guarded = False", 2)
    assert _problems([{"name": "probe", "edits": [edit]}], tmp_path) == []
    edit[field] = value
    assert _problems([{"name": "probe", "edits": [edit]}], tmp_path) != []


_MULTILINE_SOURCE = "first = 1\nguarded = (\n    True and\n    checked\n)\nlast = 2\n"
_MULTILINE_BEFORE = "guarded = (\n    True and\n    checked\n)"


def test_a_multiline_case_that_still_matches_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text(_MULTILINE_SOURCE, encoding="utf-8")
    edit = _edit(target, _MULTILINE_BEFORE, "guarded = False", 2)
    assert _problems([{"name": "probe", "edits": [edit]}], tmp_path) == []


def test_a_multiline_case_whose_later_lines_changed_is_reported_though_its_first_line_still_holds(tmp_path: Path) -> None:
    """The case was recorded when the guard read ``True and checked``; the guard now reads ``True or checked``. The
    first line of ``before`` is still on the recorded line, which is all the earlier check looked at, and the digest is
    fresh, so only the exact span tells that the replay would refuse the case."""

    target = tmp_path / "module.py"
    target.write_text(_MULTILINE_SOURCE.replace("True and", "True or"), encoding="utf-8")
    edit = _edit(target, _MULTILINE_BEFORE, "guarded = False", 2)
    first_line = _MULTILINE_BEFORE.split("\n")[0]
    assert first_line in target.read_text(encoding="utf-8").split("\n")[edit["line"] - 1]  # type: ignore[operator]
    problems = _problems([{"name": "probe", "edits": [edit]}], tmp_path)
    assert len(problems) == 1 and "span mismatch" in problems[0]


def test_a_case_whose_text_appears_twice_or_not_at_all_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("guarded = True\nguarded = True\n", encoding="utf-8")
    twice = _edit(target, "guarded = True", "guarded = False", 1)
    missing = _edit(target, "guarded = None", "guarded = False", 1)
    problems = _problems([{"name": "twice", "edits": [twice]}, {"name": "missing", "edits": [missing]}], tmp_path)
    assert [problem.split(":")[0] for problem in problems] == ["twice", "missing"]


def test_a_case_that_names_a_missing_file_or_a_path_outside_the_root_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (tmp_path / "outside.py").write_text("guarded = True\n", encoding="utf-8")
    outside = _edit(tmp_path / "outside.py", "guarded = True", "guarded = False", 1)
    outside["file"] = "../outside.py"
    absent = {"file": "absent.py", "line": 1, "before": "x", "after": "y", "sha256": "0" * 64}
    problems = _problems([{"name": "outside", "edits": [outside]}, {"name": "absent", "edits": [absent]}], root)
    assert [problem.split(":")[0] for problem in problems] == ["outside", "absent"]
