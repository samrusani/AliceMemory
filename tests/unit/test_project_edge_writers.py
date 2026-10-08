"""Every writer of a project edge stores the normalized project id as the edge target."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "alicebot_api"
HELPER = "project_edge_target"
#: The only modules that spell the edge type. The SQLite schema lists it as an allowed value.
EDGE_TYPE_FILES = {"routers/vnext_memories.py", "sqlite_schema.py"}


def _constant(node: ast.expr | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _is_helper_call(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == HELPER


def _project_edge_writers() -> list[tuple[str, int, bool]]:
    """Return (file, line, uses the helper) for every edge payload that targets a project."""

    found: list[tuple[str, int, bool]] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            from_helper = {
                target.id
                for statement in ast.walk(function)
                if isinstance(statement, ast.Assign) and _is_helper_call(statement.value)
                for target in statement.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(function):
                if not isinstance(node, ast.Dict):
                    continue
                fields = {_constant(key): value for key, value in zip(node.keys, node.values)}
                if "belongs_to_project" != _constant(fields.get("edge_type")) and "project" != _constant(
                    fields.get("to_type")
                ):
                    continue
                target = fields.get("to_id")
                uses_helper = _is_helper_call(target) or (isinstance(target, ast.Name) and target.id in from_helper)
                entry = (path.relative_to(PACKAGE).as_posix(), node.lineno, uses_helper)
                if entry not in found:
                    found.append(entry)
    return found


def test_the_scan_finds_the_memory_and_the_source_writer() -> None:
    writers = _project_edge_writers()
    assert [(name, uses_helper) for name, _line, uses_helper in writers] == [
        ("routers/vnext_memories.py", True),
        ("routers/vnext_memories.py", True),
    ]


def test_no_project_edge_stores_a_caller_spelling() -> None:
    raw = [(name, line) for name, line, uses_helper in _project_edge_writers() if not uses_helper]
    assert raw == [], "a project edge must store project_edge_target(...) as its to_id"


def test_no_other_module_names_the_project_edge_type() -> None:
    names = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "belongs_to_project" in path.read_text(encoding="utf-8")
    }
    # A new writer has to be added to this list on purpose, and routed through the helper.
    assert names == EDGE_TYPE_FILES
