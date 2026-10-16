"""Every product block that can roll back says what happens to the request's label state.

A read in a request caches the labels it judges. When a write inside a block is rolled back, a label cached
after that write describes a row that is gone, and the rest of the request would read a label that is too
permissive. ``store.savepoint()`` empties the cached labels when it rolls back. A block that opens its own
transaction or savepoint on the connection, or rolls the connection back, has to do the same, or say why it
does not need to.

This test finds every such block in the product code and requires one of two things in the function that holds it:
a reference to what empties the label state (``drop_label_state_on_rollback``, ``label_savepoint_rolled_back`` or
``invalidate_read_labels``), or a comment that starts with ``# label state:`` and gives the reason it needs
nothing. A new block therefore cannot be added without that decision being written down.

The behaviour of the blocks that do empty the state is tested in
``tests/integration/test_weekly_parent_index_invalidation_postgres.py`` and
``tests/unit/test_weekly_parent_index_invalidation.py``.

Mutations: leave ``drop_label_state_on_rollback`` out of ``capture_label_inputs`` or of ``_embedding_write_savepoint``;
delete one of the ``# label state:`` comments.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "apps/api/src/alicebot_api"
MARKER = "# label state:"
EMPTIES_THE_STATE = frozenset({"drop_label_state_on_rollback", "label_savepoint_rolled_back", "invalidate_read_labels"})
ROLLS_BACK_BY_SQL = re.compile(r"\s*(SAVEPOINT|ROLLBACK)\b", re.IGNORECASE)


def _leading_text(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)
    return ""


def _opens_or_ends_a_rollback(call: ast.Call) -> bool:
    """A call that opens a transaction or savepoint, or rolls one back: ``x.transaction()``, ``x.rollback()``, or a
    statement that starts with SAVEPOINT or ROLLBACK."""

    if isinstance(call.func, ast.Attribute) and call.func.attr in {"transaction", "rollback"}:
        return True
    return bool(call.args) and bool(ROLLS_BACK_BY_SQL.match(_leading_text(call.args[0])))


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _innermost(functions: list[ast.FunctionDef | ast.AsyncFunctionDef], line: int):
    holding = [item for item in functions if item.lineno <= line <= (item.end_lineno or item.lineno)]
    return max(holding, key=lambda item: item.lineno, default=None)


def _names_in(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def rollback_sites() -> list[tuple[str, int, str, bool]]:
    """(file, line, function, accounted for) for every block of the product code that can roll back."""

    found: list[tuple[str, int, str, bool]] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if "/migrations/" in relative:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        functions = _functions(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _opens_or_ends_a_rollback(node):
                continue
            holder = _innermost(functions, node.lineno)
            if holder is None:
                span, name, scope = range(node.lineno, node.lineno + 1), "<module>", tree
            else:
                span = range(holder.lineno, (holder.end_lineno or holder.lineno) + 1)
                name, scope = holder.name, holder
            commented = any(MARKER in lines[number - 1] for number in span)
            referenced = bool(_names_in(scope) & EMPTIES_THE_STATE)
            found.append((relative, node.lineno, name, commented or referenced))
    return found


def test_the_scan_finds_the_blocks_it_is_meant_to_find() -> None:
    sites = {(file.rsplit("/", 1)[-1], function) for file, _line, function, _ok in rollback_sites()}
    assert ("vnext_label_writes.py", "capture_label_inputs") in sites
    assert ("vnext_embeddings.py", "_embedding_write_savepoint") in sites
    assert ("vnext_store.py", "savepoint") in sites
    assert ("sqlite_store.py", "savepoint") in sites
    assert ("providers.py", "_persist_discovered_provider_capability") in sites


def test_every_block_that_can_roll_back_says_what_happens_to_the_label_state() -> None:
    unaccounted = [f"{file}:{line} in {function}" for file, line, function, ok in rollback_sites() if not ok]
    assert unaccounted == [], (
        "These blocks can roll back and neither empty the request's label state nor say why they need not. "
        "Wrap the block in drop_label_state_on_rollback, or put a '# label state: <reason>' comment in the function:\n"
        + "\n".join(unaccounted)
    )
