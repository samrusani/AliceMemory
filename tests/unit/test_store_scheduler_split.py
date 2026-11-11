"""Receipts for the scheduler carrier moved out of the PostgreSQL facade.

The family moved the way the graph and open loop family moved in v0.12.0:
13 module-level functions in ``vnext_stores/postgres/scheduler.py``, taken
back by ``PostgresVNextStore`` under the same names, in the same class
order, with the ``__module__`` and ``__qualname__`` the other carriers set.
Every moved body equals its body at the base commit ``33406c14`` (proved
by the comparison command in the pull request). The receipts here pin the
carrier's source, its method AST manifest, its comments, the facade's
class member order, the graft shape, the standalone import, the line caps,
and the weakened-source and cross-wired-facade cases.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import os
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

import alicebot_api.vnext_store as postgres_store
from alicebot_api.vnext_stores.postgres import scheduler as postgres_scheduler

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_store.py"
POSTGRES_CARRIER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/scheduler.py"
BASE_COMMIT = "33406c14"

POSTGRES_METHODS = (
    "upsert_scheduler_workflow",
    "update_scheduler_workflow",
    "get_scheduler_workflow",
    "list_scheduler_workflows",
    "create_scheduler_run",
    "update_scheduler_run",
    "list_scheduler_runs",
    "claim_due_scheduler_workflow",
    "heartbeat_scheduler_claim",
    "lock_scheduler_claim_for_publish",
    "finalize_scheduler_claim",
    "reap_expired_scheduler_claims",
    "try_scheduler_workflow_lock",
)

SOURCE_RECEIPTS = {
    POSTGRES_CARRIER_PATH: "9b76d3e99aeee9d29bc699519279e06006d62162739307ace3b8461fa1537412",
}
EXPECTED_METHOD_AST_MANIFESTS = {
    POSTGRES_CARRIER_PATH: "5756a1cee3b603a1e8650835108f16a4e2b54c4dd1f43ea6b58cb09ec30b613a",
}
EXPECTED_COMMENT_MANIFESTS = {
    POSTGRES_CARRIER_PATH: (3, "d0143b7f79d5b33cb8836069202abbb4727bdedab3e5dd5dd7cc5f69a3a437c3"),
}
EXPECTED_METADATA_MANIFESTS = {
    POSTGRES_CARRIER_PATH: "f6983a000e51854669b7ac2a42e9aeebaae36fa44b5810767c2f3b3bcfde699d",
}
EXPECTED_CLASS_ORDERS = {
    # The graft assignments keep every member name and position the inline
    # methods held, so this receipt is the base commit's own order.
    "PostgresVNextStore": (186, "974e71a5c4c9748e51169bd5878c94ac354d404096b2f71d238cadbbed85cebe"),
}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _tree(path: Path, source: str | None = None) -> ast.Module:
    return ast.parse(source if source is not None else path.read_text(encoding="utf-8"))


def _class_node(path: Path, class_name: str, source: str | None = None) -> ast.ClassDef:
    matches = [
        node
        for node in _tree(path, source).body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert len(matches) == 1
    return matches[0]


def _class_members(node: ast.ClassDef) -> list[str]:
    members: list[str] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members.append(child.name)
        elif isinstance(child, ast.Assign):
            members.extend(target.id for target in child.targets if isinstance(target, ast.Name))
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            members.append(child.target.id)
    return members


def _assignment_map(node: ast.ClassDef) -> dict[str, str]:
    return {
        child.targets[0].id: child.value.id
        for child in node.body
        if isinstance(child, ast.Assign)
        and len(child.targets) == 1
        and isinstance(child.targets[0], ast.Name)
        and isinstance(child.value, ast.Name)
    }


def _functions(path: Path) -> list[ast.FunctionDef]:
    return [node for node in _tree(path).body if isinstance(node, ast.FunctionDef)]


def test_scheduler_carrier_source_receipt_and_manifests_hold() -> None:
    texts = {path: path.read_text(encoding="utf-8") for path in SOURCE_RECEIPTS}
    for path, expected in SOURCE_RECEIPTS.items():
        assert _digest(texts[path]) == expected, path
    for path, expected in EXPECTED_METHOD_AST_MANIFESTS.items():
        functions = _functions(path)
        assert len(functions) == len(POSTGRES_METHODS)
        assert _digest("\n".join(ast.dump(node, include_attributes=False) for node in functions)) == expected
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(texts[path]).readline)
            if token.type == tokenize.COMMENT
        ]
        expected_count, expected_comments = EXPECTED_COMMENT_MANIFESTS[path]
        assert len(comments) == expected_count
        assert _digest("\n".join(comments)) == expected_comments


def test_scheduler_methods_are_direct_native_order_grafts_with_exact_metadata() -> None:
    node = _class_node(POSTGRES_FACADE_PATH, "PostgresVNextStore")
    assert {child.name for child in node.body if isinstance(child, ast.FunctionDef)}.isdisjoint(POSTGRES_METHODS)
    assignments = _assignment_map(node)
    assert [name for name in _class_members(node) if name in POSTGRES_METHODS] == list(POSTGRES_METHODS)
    assert {name: assignments.get(name) for name in POSTGRES_METHODS} == {
        name: f"_scheduler_{name}" for name in POSTGRES_METHODS
    }
    members = _class_members(node)
    expected_count, expected_digest = EXPECTED_CLASS_ORDERS["PostgresVNextStore"]
    assert len(members) == expected_count
    assert _digest("\n".join(members)) == expected_digest
    facade_class = postgres_store.PostgresVNextStore
    assert facade_class.__bases__ == (object,)
    assert facade_class.__mro__ == (facade_class, object)
    for name in POSTGRES_METHODS:
        method = getattr(facade_class, name)
        assert method is getattr(postgres_scheduler, name)
        assert method.__module__ == "alicebot_api.vnext_store"
        assert method.__qualname__ == f"PostgresVNextStore.{name}"
    rows = []
    for node_function in _functions(POSTGRES_CARRIER_PATH):
        method = getattr(postgres_store.PostgresVNextStore, node_function.name)
        rows.append(
            (
                node_function.name,
                str(inspect.signature(method)),
                method.__doc__,
                method.__module__,
                method.__qualname__,
            )
        )
    assert _digest(repr(rows)) == EXPECTED_METADATA_MANIFESTS[POSTGRES_CARRIER_PATH]


def test_scheduler_carrier_imports_standalone_without_facade_cycles() -> None:
    for node in ast.walk(_tree(POSTGRES_CARRIER_PATH)):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "alicebot_api.vnext_store"
        elif isinstance(node, ast.Import):
            assert all(alias.name != "alicebot_api.vnext_store" for alias in node.names)

    code = """
import sys
from alicebot_api.vnext_stores.postgres import scheduler as postgres_scheduler
assert 'alicebot_api.vnext_store' not in sys.modules
assert postgres_scheduler.upsert_scheduler_workflow
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_split_keeps_every_store_file_below_phase_cap() -> None:
    for path in (POSTGRES_FACADE_PATH, POSTGRES_CARRIER_PATH):
        assert len(path.read_text(encoding="utf-8").splitlines()) < 4000, path


def test_scheduler_carrier_bodies_equal_the_base_commit() -> None:
    old_src = subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:apps/api/src/alicebot_api/vnext_store.py"],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    ).stdout
    new_functions = {node.name: node for node in _functions(POSTGRES_CARRIER_PATH)}
    base_class = _class_node(
        Path("vnext_store.py"), "PostgresVNextStore", old_src
    )
    base_functions = {
        node.name: node
        for node in base_class.body
        if isinstance(node, ast.FunctionDef) and node.name in POSTGRES_METHODS
    }
    assert set(base_functions) == set(POSTGRES_METHODS)
    for name in POSTGRES_METHODS:
        assert ast.dump(base_functions[name], include_attributes=False) == ast.dump(
            new_functions[name], include_attributes=False
        ), name


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("RETURNING {SCHEDULER_WORKFLOW_COLUMNS}", "RETURNING *"),
        ("app.current_user_id()", "NULL"),
        ("_json_object(metadata_json)", "metadata_json"),
    ),
)
def test_scheduler_receipts_fail_on_weakened_carriers(old: str, new: str) -> None:
    texts = {path: path.read_text(encoding="utf-8") for path in SOURCE_RECEIPTS}
    assert old in texts[POSTGRES_CARRIER_PATH]
    texts[POSTGRES_CARRIER_PATH] = texts[POSTGRES_CARRIER_PATH].replace(old, new, 1)
    with pytest.raises(AssertionError):
        for path, expected in SOURCE_RECEIPTS.items():
            assert _digest(texts[path]) == expected, path


def test_scheduler_graft_guard_rejects_old_or_cross_wired_facades() -> None:
    postgres_source = POSTGRES_FACADE_PATH.read_text(encoding="utf-8")
    assert "    upsert_scheduler_workflow = _scheduler_upsert_scheduler_workflow" in postgres_source
    cross_wired = postgres_source.replace(
        "    upsert_scheduler_workflow = _scheduler_upsert_scheduler_workflow",
        "    upsert_scheduler_workflow = _scheduler_list_scheduler_workflows",
        1,
    )
    with pytest.raises(AssertionError):
        node = _class_node(POSTGRES_FACADE_PATH, "PostgresVNextStore", cross_wired)
        assignments = _assignment_map(node)
        assert {name: assignments.get(name) for name in POSTGRES_METHODS} == {
            name: f"_scheduler_{name}" for name in POSTGRES_METHODS
        }
    old_shape = postgres_source.replace(
        "    upsert_scheduler_workflow = _scheduler_upsert_scheduler_workflow",
        "    def upsert_scheduler_workflow(self):\n        raise NotImplementedError",
        1,
    )
    with pytest.raises(AssertionError):
        node = _class_node(POSTGRES_FACADE_PATH, "PostgresVNextStore", old_shape)
        assert {child.name for child in node.body if isinstance(child, ast.FunctionDef)}.isdisjoint(
            POSTGRES_METHODS
        )
