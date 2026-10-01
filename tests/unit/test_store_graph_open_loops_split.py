from __future__ import annotations

import ast
import hashlib
import inspect
import io
import os
from pathlib import Path
import subprocess
import sys
import tokenize

import pytest

import alicebot_api.sqlite_store as sqlite_store
import alicebot_api.vnext_store as postgres_store
from alicebot_api.vnext_stores.postgres import columns as postgres_columns
from alicebot_api.vnext_stores.postgres import graph_open_loops as postgres_graph
from alicebot_api.vnext_stores.postgres import primitives as postgres_primitives
from alicebot_api.vnext_stores.sqlite import columns as sqlite_columns
from alicebot_api.vnext_stores.sqlite import graph_open_loops as sqlite_graph
from alicebot_api.vnext_stores.sqlite import primitives as sqlite_primitives


REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_store.py"
SQLITE_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/sqlite_store.py"
POSTGRES_COLUMNS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/columns.py"
SQLITE_COLUMNS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/columns.py"
POSTGRES_CARRIER_PATH = (
    REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/graph_open_loops.py"
)
SQLITE_CARRIER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/graph_open_loops.py"

POSTGRES_METHODS = (
    "create_edge",
    "find_edge_by_idempotency_digest",
    "upsert_edge_by_idempotency_digest",
    "list_edges",
    "list_memory_entity_edges",
    "list_edges_as_of",
    "update_edge_status",
    "expire_edge",
    "create_entity",
    "get_entity",
    "get_entity_by_normalized_name",
    "find_entities_by_names",
    "list_entities",
    "update_entity",
    "record_entity_mention",
    "record_relationship_change",
    "list_relationship_events",
    "create_belief",
    "get_belief",
    "list_beliefs",
    "update_belief_status",
    "create_open_loop",
    "upsert_open_loop_by_automation_digest",
    "get_open_loop",
    "find_open_loop_by_automation_digest",
    "list_open_loops_referencing_source",
    "list_open_loops",
    "list_open_loop_events",
    "update_open_loop",
    "update_open_loop_status",
)
SQLITE_METHODS = (
    "create_graph_edge",
    "list_edges",
    "list_memory_entity_edges",
    "expire_edge",
    "list_edges_as_of",
    "create_entity",
    "get_entity",
    "get_entity_by_normalized_name",
    "find_entities_by_names",
    "list_entities",
    "update_entity",
    "record_entity_mention",
    "record_relationship_change",
    "list_relationship_events",
    "create_open_loop",
    "upsert_open_loop_by_automation_digest",
    "get_open_loop",
    "find_open_loop_by_automation_digest",
    "list_open_loops_referencing_source",
    "list_open_loops",
    "list_open_loop_events",
    "update_open_loop",
    "update_open_loop_status",
)
POSTGRES_COLUMN_NAMES = (
    "GRAPH_EDGE_COLUMNS",
    "ENTITY_COLUMNS",
    "ENTITY_RELATIONSHIP_EVENT_COLUMNS",
    "BELIEF_COLUMNS",
    "OPEN_LOOP_COLUMNS",
)
SQLITE_COLUMN_NAMES = (
    "GRAPH_EDGE_COLUMNS",
    "ENTITY_COLUMNS",
    "ENTITY_RELATIONSHIP_EVENT_COLUMNS",
    "OPEN_LOOP_COLUMNS",
)

SOURCE_RECEIPTS = {
    POSTGRES_CARRIER_PATH: "9e91fbb96705ccb61c8100c32f8fc7875b6e715d8355865923491c7fa937a102",
    SQLITE_CARRIER_PATH: "fb0569f51578e3c57f43cd6beb9dc88ab307242f6ef829180e7613b50000283f",
    POSTGRES_COLUMNS_PATH: "5b0d972a55abf8590ce14394a37fd71b9b88ba7ab3de82d61efc1bddfc022b71",
    SQLITE_COLUMNS_PATH: "be81b8628d0831d3d02b280b5455fb02333db5740ebef8d85d58024384ae6556",
}
EXPECTED_METHOD_AST_MANIFESTS = {
    POSTGRES_CARRIER_PATH: "9a354d1cfb9f134ec7fadb74dd1647b1502ecb2a5b83edb3b65c7123091111ca",
    SQLITE_CARRIER_PATH: "96a4c8d4ecbbe8dbd4ef4f3f3831a0cc81a049f90192f6bf9a634c3fb6b497be",
}
EXPECTED_METADATA_MANIFESTS = {
    POSTGRES_CARRIER_PATH: "801a455053962b25972ab783d36b03d0389df5c151cba545b05ee8d150f172b9",
    SQLITE_CARRIER_PATH: "adb5a1b9aeeea1cab3ef24f4fd3beffdfeb2ce450943dc8a0f220b192791f020",
}
EXPECTED_COMMENT_MANIFESTS = {
    POSTGRES_CARRIER_PATH: (9, "bb34d175e716f5a929fa1ee5e7e30ba0e0b25be285cda3556a0c709719316c4e"),
    SQLITE_CARRIER_PATH: (1, "8f448801a348111594f3d0f33c9e82756981958985c270d45a8716914892d71b"),
}
EXPECTED_CLASS_ORDERS = {
    # Two paired browser-clip capability methods extend both façades, and one
    # more paired method, ``list_memories_referencing_sources``.
    "PostgresVNextStore": (171, "526374782104a2a120a40fdb3596893975d4e1528d4c8130802cfc830161eb35"),
    # One SQLite-only method more, ``check_source_search_query``: the Postgres
    # source search has no expression-depth or LIKE-length limit to check.
    "SQLiteVNextStore": (125, "8597531ec4227a265bcfd8a21e033f618fb3c6842e513d03ade05e0cd97723e3"),
}
EXPECTED_COLUMN_AST = {
    POSTGRES_COLUMNS_PATH: {
        "GRAPH_EDGE_COLUMNS": "b0f146d327870472bcd646b291c2aa77336dc92049fdcc21c664669cf182110e",
        "ENTITY_COLUMNS": "42217e33d0e5b732cb53db2f1fe70ae0128ebd890e3633f89f1ccbbd8a79a1c5",
        "ENTITY_RELATIONSHIP_EVENT_COLUMNS": (
            "c333a0dacf8733a16fb86f3acc1bbf61bd25fa66ce96cc3bc25805c4a85d9203"
        ),
        "BELIEF_COLUMNS": "32bac57e9e38fead1af29b9324777978f3b03bfe20d5306695fae98079d82dc7",
        "OPEN_LOOP_COLUMNS": "651275ee48d37e13228bf339ac4f260a50333fc7b86197ab16573cc099f912bf",
    },
    SQLITE_COLUMNS_PATH: {
        "GRAPH_EDGE_COLUMNS": "587b88564c446c03420441371a180e11618ea6bf192e5e20d2ad5d426ce890f2",
        "ENTITY_COLUMNS": "fb248a2a3c594a3e3a85c38d4978e828d6877aed1eb8fdd65e31ee5f695a5b3e",
        "ENTITY_RELATIONSHIP_EVENT_COLUMNS": (
            "a66291abf36ff05d353efd34d45f8a740c29644c321060c55f1ae72c8d210829"
        ),
        "OPEN_LOOP_COLUMNS": "7e07fc8938294633fbc90a9664375dd80b7389a3740303bc5f3926da43ea4b6a",
    },
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


def _named_assignments(path: Path) -> dict[str, ast.Assign]:
    return {
        node.targets[0].id: node
        for node in _tree(path).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }


def _assert_source_receipts(texts: dict[Path, str]) -> None:
    assert set(texts) == set(SOURCE_RECEIPTS)
    for path, expected in SOURCE_RECEIPTS.items():
        assert _digest(texts[path]) == expected, path


def _assert_grafts(
    path: Path,
    class_name: str,
    names: tuple[str, ...],
    source: str | None = None,
) -> None:
    node = _class_node(path, class_name, source)
    assert {child.name for child in node.body if isinstance(child, ast.FunctionDef)}.isdisjoint(names)
    assignments = _assignment_map(node)
    assert [name for name in _class_members(node) if name in names] == list(names)
    assert {name: assignments.get(name) for name in names} == {
        name: f"_graph_{name}" for name in names
    }


def _metadata_manifest(module: object, path: Path) -> str:
    rows = []
    for node in _functions(path):
        method = getattr(module, node.name)
        rows.append(
            (
                node.name,
                str(inspect.signature(method)),
                method.__doc__,
                method.__module__,
                method.__qualname__,
            )
        )
    return _digest(repr(rows))


def test_graph_open_loop_sources_pin_all_code_sql_parameters_and_comments() -> None:
    texts = {path: path.read_text(encoding="utf-8") for path in SOURCE_RECEIPTS}
    _assert_source_receipts(texts)
    for path, expected in EXPECTED_METHOD_AST_MANIFESTS.items():
        functions = _functions(path)
        assert _digest("\n".join(ast.dump(node, include_attributes=False) for node in functions)) == expected
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(texts[path]).readline)
            if token.type == tokenize.COMMENT
        ]
        expected_count, expected_comments = EXPECTED_COMMENT_MANIFESTS[path]
        assert len(comments) == expected_count
        assert _digest("\n".join(comments)) == expected_comments


def test_graph_open_loop_methods_are_direct_native_order_grafts_with_exact_metadata() -> None:
    specs = (
        (
            POSTGRES_FACADE_PATH,
            "PostgresVNextStore",
            POSTGRES_METHODS,
            postgres_store.PostgresVNextStore,
            postgres_graph,
            "alicebot_api.vnext_store",
        ),
        (
            SQLITE_FACADE_PATH,
            "SQLiteVNextStore",
            SQLITE_METHODS,
            sqlite_store.SQLiteVNextStore,
            sqlite_graph,
            "alicebot_api.sqlite_store",
        ),
    )
    for path, class_name, names, facade_class, carrier, module_name in specs:
        _assert_grafts(path, class_name, names)
        members = _class_members(_class_node(path, class_name))
        expected_count, expected_digest = EXPECTED_CLASS_ORDERS[class_name]
        assert len(members) == expected_count
        assert _digest("\n".join(members)) == expected_digest
        assert facade_class.__bases__ == (object,)
        assert facade_class.__mro__ == (facade_class, object)
        for name in names:
            method = getattr(facade_class, name)
            assert method is getattr(carrier, name)
            assert method.__module__ == module_name
            assert method.__qualname__ == f"{class_name}.{name}"

    assert _metadata_manifest(postgres_graph, POSTGRES_CARRIER_PATH) == EXPECTED_METADATA_MANIFESTS[
        POSTGRES_CARRIER_PATH
    ]
    assert _metadata_manifest(sqlite_graph, SQLITE_CARRIER_PATH) == EXPECTED_METADATA_MANIFESTS[
        SQLITE_CARRIER_PATH
    ]


def test_graph_open_loop_columns_are_exact_single_source_reexports() -> None:
    specs = (
        (
            POSTGRES_COLUMNS_PATH,
            POSTGRES_COLUMN_NAMES,
            postgres_columns,
            postgres_store,
            postgres_graph,
        ),
        (
            SQLITE_COLUMNS_PATH,
            SQLITE_COLUMN_NAMES,
            sqlite_columns,
            sqlite_store,
            sqlite_graph,
        ),
    )
    for path, names, columns, facade, carrier in specs:
        nodes = _named_assignments(path)
        for name in names:
            assert _digest(ast.dump(nodes[name], include_attributes=False)) == EXPECTED_COLUMN_AST[path][name]
            assert getattr(facade, name) is getattr(columns, name)
            assert getattr(carrier, name) is getattr(columns, name)
        facade_assignments = _named_assignments(
            POSTGRES_FACADE_PATH if facade is postgres_store else SQLITE_FACADE_PATH
        )
        assert set(names).isdisjoint(facade_assignments)

    assert postgres_graph._sorted_field_names is postgres_primitives._sorted_field_names
    assert sqlite_graph._sorted_field_names is sqlite_primitives._sorted_field_names
    assert postgres_store._sorted_field_names is postgres_primitives._sorted_field_names
    assert sqlite_store._sorted_field_names is sqlite_primitives._sorted_field_names


def test_graph_open_loop_carriers_import_standalone_without_facade_cycles() -> None:
    for path in (POSTGRES_CARRIER_PATH, SQLITE_CARRIER_PATH):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
            elif isinstance(node, ast.Import):
                assert all(
                    alias.name not in {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
                    for alias in node.names
                )

    code = """
import sys
from alicebot_api.vnext_stores.postgres import graph_open_loops as postgres_graph
from alicebot_api.vnext_stores.sqlite import graph_open_loops as sqlite_graph
assert 'alicebot_api.vnext_store' not in sys.modules
assert 'alicebot_api.sqlite_store' not in sys.modules
assert postgres_graph.create_edge
assert sqlite_graph.create_graph_edge
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


def test_graph_open_loop_split_keeps_every_store_file_below_phase_cap() -> None:
    for path in (
        POSTGRES_FACADE_PATH,
        SQLITE_FACADE_PATH,
        POSTGRES_CARRIER_PATH,
        SQLITE_CARRIER_PATH,
    ):
        assert len(path.read_text(encoding="utf-8").splitlines()) < 4000, path


@pytest.mark.parametrize(
    ("path", "old", "new"),
    (
        (POSTGRES_CARRIER_PATH, "AND valid_to IS NULL", ""),
        (POSTGRES_CARRIER_PATH, "_sorted_field_names(entity)", "[]"),
        (POSTGRES_CARRIER_PATH, "app.current_user_id()", "NULL"),
        (SQLITE_CARRIER_PATH, "AND user_id = ?", ""),
        (SQLITE_CARRIER_PATH, "if cursor.rowcount == 0:", "if False:"),
        (SQLITE_CARRIER_PATH, "_sorted_field_names(entity)", "[]"),
        (POSTGRES_COLUMNS_PATH, "observed_at,", ""),
        (SQLITE_COLUMNS_PATH, '"observed_at",', ""),
    ),
)
def test_graph_open_loop_receipts_fail_on_weakened_sources(path: Path, old: str, new: str) -> None:
    texts = {candidate: candidate.read_text(encoding="utf-8") for candidate in SOURCE_RECEIPTS}
    assert old in texts[path]
    texts[path] = texts[path].replace(old, new, 1)
    with pytest.raises(AssertionError):
        _assert_source_receipts(texts)


def test_graph_open_loop_graft_guard_rejects_old_or_cross_wired_facades() -> None:
    postgres_source = POSTGRES_FACADE_PATH.read_text(encoding="utf-8")
    assert "    create_edge = _graph_create_edge" in postgres_source
    cross_wired = postgres_source.replace(
        "    create_edge = _graph_create_edge",
        "    create_edge = _graph_list_edges",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_grafts(
            POSTGRES_FACADE_PATH,
            "PostgresVNextStore",
            POSTGRES_METHODS,
            cross_wired,
        )

    sqlite_source = SQLITE_FACADE_PATH.read_text(encoding="utf-8")
    assert "    create_graph_edge = _graph_create_graph_edge" in sqlite_source
    old_shape = sqlite_source.replace(
        "    create_graph_edge = _graph_create_graph_edge",
        "    def create_graph_edge(self):\n        raise NotImplementedError",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_grafts(
            SQLITE_FACADE_PATH,
            "SQLiteVNextStore",
            SQLITE_METHODS,
            old_shape,
        )
