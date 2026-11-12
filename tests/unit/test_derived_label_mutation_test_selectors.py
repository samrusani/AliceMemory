"""Each mutation case names test files and functions that exist, without collecting or running them."""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _selector_problems(cases: list[dict], root: Path) -> list[str]:
    problems: list[str] = []
    trees: dict[Path, ast.Module] = {}
    for case in cases:
        selectors = iter(case["pytest"])
        for selector in selectors:
            if selector == "-k":
                next(selectors)
                continue
            parts = selector.split("::")
            path = root / parts[0]
            if not path.is_file():
                problems.append(f"{case['name']}: missing test file {parts[0]}")
                continue
            if len(parts) == 1:
                continue
            if path not in trees:
                trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            body = trees[path].body
            for name in parts[1:-1]:
                found = next((node for node in body if isinstance(node, ast.ClassDef) and node.name == name), None)
                if found is None:
                    problems.append(f"{case['name']}: missing test class {selector}")
                    break
                body = found.body
            else:
                function = parts[-1].split("[", 1)[0]
                if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function for node in body):
                    problems.append(f"{case['name']}: missing test function {selector}")
    return problems


def test_every_manifest_case_names_existing_test_files_and_functions(tmp_path: Path) -> None:
    """Changing ``return problems`` in ``_selector_problems`` to ``return []`` fails this test."""
    folder = tmp_path / "tests"
    folder.mkdir()
    (folder / "test_sample.py").write_text(
        "def test_present(): pass\nclass TestGroup:\n    async def test_inside(self): pass\n", encoding="utf-8"
    )
    probes = [
        {"name": "missing-file", "pytest": ["tests/test_absent.py::test_present"]},
        {"name": "missing-function", "pytest": ["tests/test_sample.py::test_absent"]},
        {"name": "missing-class", "pytest": ["tests/test_sample.py::TestAbsent::test_inside"]},
        {"name": "present", "pytest": ["tests/test_sample.py::test_present[x]", "-k", "present"]},
        {"name": "present-class", "pytest": ["tests/test_sample.py::TestGroup::test_inside"]},
    ]
    assert [problem.split(":", 1)[0] for problem in _selector_problems(probes, tmp_path)] == [
        "missing-file", "missing-function", "missing-class"
    ]
    cases = json.loads((ROOT / "scripts/derived_label_mutations.json").read_text(encoding="utf-8"))["cases"]
    assert _selector_problems(cases, ROOT) == []
