from __future__ import annotations

import ast
import hashlib
import inspect
import os
from pathlib import Path
import subprocess
import sys

import pytest

import alicebot_api.sqlite_store as sqlite_store
import alicebot_api.vnext_store as postgres_store
from alicebot_api.vnext_stores import retrieval_common
from alicebot_api.vnext_stores.postgres import memory_access as postgres_memory
from alicebot_api.vnext_stores.postgres import query_predicates as postgres_query
from alicebot_api.vnext_stores.sqlite import memory_access as sqlite_memory
from alicebot_api.vnext_stores.sqlite import query_predicates as sqlite_query


REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_store.py"
SQLITE_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/sqlite_store.py"

SOURCE_RECEIPTS = {
    "apps/api/src/alicebot_api/vnext_stores/retrieval_common.py": (
        "fa1a3a90511b5c61754ba29560e91b7b3058a48d47c143b09d8505d52025b8cc"
    ),
    "apps/api/src/alicebot_api/vnext_stores/postgres/query_predicates.py": (
        "f0ec9c7f13bc7bf93f5a3beaa86916a04e45200ef0296d6f9288eed3912be33d"
    ),
    "apps/api/src/alicebot_api/vnext_stores/postgres/memory_access.py": (
        "33029103fd78e2887793512ac8791e523f147fb22ecd1be2dcc962670742eafc"
    ),
    "apps/api/src/alicebot_api/vnext_stores/sqlite/query_predicates.py": (
        "aada597da76324ec05a118f95c2b26441b076771a0e53b8d45f08eefb656bbb4"
    ),
    # Re-minted for the Phase 4 Stage 2 resident vector cache (reviewed
    # carrier change; the receipt guards unreviewed drift): the vector scan
    # tries vector_scan.cached_vector_ranked before the stateless path, and
    # the cached candidate SQL now carries the scan's FULL predicate set --
    # including the signature json_extract clauses -- so no predicate is
    # ever captured into the resident data (metadata_json rewrites cannot
    # stale the cache). Re-minted again for ``list_memories``, which binds a query
    # through ``literal_match_operand`` and so refuses one past the LIKE operand
    # limit. The Postgres carrier is unchanged on purpose: it has no such limit.
    "apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py": (
        "b048578611ccfcb88519fa46ab47d52234fc68128ccceb0cd9bc4998dd9d8a44"
    ),
}

POSTGRES_METHODS = (
    "get_memory_by_key",
    "get_memory",
    "get_memories_by_ids",
    "list_memories_referencing_source",
    "list_memories_referencing_sources",
    "list_pending_derived_candidates_for_member",
    "list_memories",
    "list_memories_by_statuses",
    "count_memories_by_status",
    "list_recent_agentic_commits",
    "list_pending_inline_confirmations",
    "find_live_memory_by_canonical_text",
    "list_memories_for_staleness_sweep",
    "count_memories",
    "list_rollup_input_memories",
    "count_rollup_input_memories",
    "list_pending_rollup_candidates",
    "list_accepted_rollup_cards",
    "search_memories",
    "search_memories_fts",
    "search_memories_vector",
    "search_memories_by_time",
    "get_memory_by_commit_digest",
    "get_memory_by_confirmation_id",
    "latest_agentic_commit_memory",
)

SQLITE_METHODS = (
    "get_memory_by_key",
    "get_memory",
    "get_memories_by_ids",
    "list_memories_referencing_source",
    "list_memories_referencing_sources",
    "list_pending_derived_candidates_for_member",
    "get_memory_by_commit_digest",
    "latest_agentic_commit_memory",
    "get_memory_by_confirmation_id",
    "list_memories",
    "list_memories_by_statuses",
    "count_memories_by_status",
    "list_recent_agentic_commits",
    "list_pending_inline_confirmations",
    "find_live_memory_by_canonical_text",
    "list_memories_for_staleness_sweep",
    "count_memories",
    "list_rollup_input_memories",
    "count_rollup_input_memories",
    "list_pending_rollup_candidates",
    "list_accepted_rollup_cards",
    "search_memories",
    "search_memories_fts",
    "search_memories_vector",
    "search_memories_by_time",
)

SQLITE_QUERY_HELPERS = (
    "_placeholders",
    "_domain_clause",
    "_sensitivity_clause",
    "_memory_type_clause",
    "_project_clause",
    "_created_by_clause",
    "_run_clause",
    "_expiry_clause",
    "_retrieval_scope_clause",
    "_metadata_scope_clause",
    "_like_any",
)
SQLITE_STATIC_HELPERS = frozenset({"_placeholders", "_run_clause", "_expiry_clause", "_like_any"})

POSTGRES_QUERY_EXPORTS = (
    "_PROJECT_ASCII_WHITESPACE_PATTERN_SQL",
    "_ASCII_PROJECT_UPPER",
    "_ASCII_PROJECT_LOWER",
    "_escape_like_literal",
    "_postgres_ascii_literal_contains_sql",
    "_normalized_project_identifier_sql",
    "_project_identifier_identity_sql",
    "_jsonb_project_scope_values_sql",
    "_jsonb_project_scope_leaf_values_sql",
    "_jsonb_source_project_scope_values_sql",
    "_MEMORY_PROJECT_SCOPE_SQL",
    "_MEMORY_DIRECT_PEOPLE_SQL",
    "_MEMORY_SCOPE_EVENT_TIME_SQL",
    "_SCOPED_MEMORY_PROJECT_SQL",
    "_SCOPED_MEMORY_DIRECT_PEOPLE_SQL",
    "_SCOPED_MEMORY_EVENT_TIME_SQL",
    "_jsonb_scope_values_sql",
    "_SOURCE_SCOPE_PROJECT_SQL",
    "_SOURCE_SCOPE_PEOPLE_SQL",
    "_SOURCE_SCOPE_EVENT_TIME_SQL",
    "_ARTIFACT_SCOPE_PROJECT_SQL",
    "_OPEN_LOOP_SCOPE_PROJECT_SQL",
    "_OPEN_LOOP_SCOPE_PEOPLE_SQL",
    "_OPEN_LOOP_SCOPE_EVENT_TIME_SQL",
    "_tsquery_any_expression",
)

SQLITE_QUERY_EXPORTS = (
    "_project_scope_value_sqlite",
    "_project_scope_identity_json_sqlite",
    "_source_project_scope_identity_json_sqlite",
    "_ensure_project_scope_identity_sqlite",
    "_escape_like_literal",
    "_sqlite_ascii_literal_contains_sql",
    "_fts_match_expression",
    "_fts_match_any_expression",
)

EXPECTED_CLASS_ORDERS = {
    # Two paired browser-clip capability methods extend both façades. One more
    # paired method, ``list_memories_referencing_sources``, is the batched form
    # of ``list_memories_referencing_source``; both carrier receipts above were
    # re-minted for it (reviewed change, not drift).
    "PostgresVNextStore": (171, "526374782104a2a120a40fdb3596893975d4e1528d4c8130802cfc830161eb35"),
    # One SQLite-only method more, ``check_source_search_query``: the Postgres
    # source search has no expression-depth or LIKE-length limit to check.
    # Merge of #500 and #502 (2026-10-01): one more SQLite-only method,
    # ``check_literal_match_query``, beside the paired
    # ``list_memories_referencing_sources``. Re-minted for the merged facade
    # (reviewed change, not drift).
    "SQLiteVNextStore": (126, "08f33e48b0ada3aee40ab843f826bd0610a31ad495091bf6880bd2b68192084a"),
}


def _source_texts() -> dict[str, str]:
    return {path: (REPO_ROOT / path).read_text(encoding="utf-8") for path in SOURCE_RECEIPTS}


def _assert_source_receipts(texts: dict[str, str]) -> None:
    assert set(texts) == set(SOURCE_RECEIPTS)
    for path, expected in SOURCE_RECEIPTS.items():
        assert hashlib.sha256(texts[path].encode()).hexdigest() == expected, path


def _class_node(path: Path, name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _class_members(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(child.name)
        elif isinstance(child, ast.Assign):
            names.extend(target.id for target in child.targets if isinstance(target, ast.Name))
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            names.append(child.target.id)
    return names


def _assignment_map(node: ast.ClassDef) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in node.body:
        if (
            isinstance(child, ast.Assign)
            and len(child.targets) == 1
            and isinstance(child.targets[0], ast.Name)
            and isinstance(child.value, ast.Name)
        ):
            result[child.targets[0].id] = child.value.id
    return result


def _assert_no_facade_imports(texts: dict[str, str]) -> None:
    forbidden = {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
    for path, source in texts.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name not in forbidden for alias in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden, path
                if node.module == "alicebot_api":
                    assert all(alias.name not in {"vnext_store", "sqlite_store"} for alias in node.names), path


def test_memory_access_source_receipts_pin_sql_parameters_and_comments() -> None:
    texts = _source_texts()
    _assert_source_receipts(texts)
    _assert_no_facade_imports(texts)

    postgres = texts["apps/api/src/alicebot_api/vnext_stores/postgres/memory_access.py"]
    sqlite = texts["apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py"]
    sqlite_predicates = texts["apps/api/src/alicebot_api/vnext_stores/sqlite/query_predicates.py"]
    assert "SET LOCAL hnsw.iterative_scan = 'strict_order'" in postgres
    assert "same endpoint as the query" in postgres
    assert "embedding_signature_version" in postgres
    assert "except sqlite3.OperationalError" in sqlite
    assert sqlite.count("user_id = ?") >= 20
    assert "Only compare vectors from the same endpoint fingerprint" in sqlite
    assert "resolve_project_scope" in sqlite_predicates
    assert sqlite_predicates.count("create_function(") == 3


def test_memory_access_methods_are_direct_grafts_in_native_backend_order() -> None:
    specs = (
        (
            POSTGRES_FACADE_PATH,
            "PostgresVNextStore",
            POSTGRES_METHODS,
            postgres_store.PostgresVNextStore,
            postgres_memory,
            "alicebot_api.vnext_store",
        ),
        (
            SQLITE_FACADE_PATH,
            "SQLiteVNextStore",
            SQLITE_METHODS,
            sqlite_store.SQLiteVNextStore,
            sqlite_memory,
            "alicebot_api.sqlite_store",
        ),
    )
    for path, class_name, method_names, facade_class, carrier, module_name in specs:
        class_node = _class_node(path, class_name)
        class_defs = {child.name for child in class_node.body if isinstance(child, ast.FunctionDef)}
        assert class_defs.isdisjoint(method_names)
        assignments = _assignment_map(class_node)
        assert [name for name in _class_members(class_node) if name in method_names] == list(method_names)
        assert {name: assignments.get(name) for name in method_names} == {
            name: f"_memory_{name}" for name in method_names
        }
        expected_count, expected_digest = EXPECTED_CLASS_ORDERS[class_name]
        members = _class_members(class_node)
        assert len(members) == expected_count
        assert hashlib.sha256("\n".join(members).encode()).hexdigest() == expected_digest
        assert facade_class.__bases__ == (object,)
        assert facade_class.__mro__ == (facade_class, object)
        for name in method_names:
            method = getattr(facade_class, name)
            assert method is getattr(carrier, name)
            assert method.__module__ == module_name
            assert method.__qualname__ == f"{class_name}.{name}"

    for name in set(POSTGRES_METHODS) & set(SQLITE_METHODS):
        assert inspect.signature(getattr(postgres_store.PostgresVNextStore, name)) == inspect.signature(
            getattr(sqlite_store.SQLiteVNextStore, name)
        )


def test_sqlite_predicate_helpers_preserve_descriptor_identity_and_metadata() -> None:
    class_node = _class_node(SQLITE_FACADE_PATH, "SQLiteVNextStore")
    assignments = _assignment_map(class_node)
    assert {name: assignments.get(name) for name in SQLITE_QUERY_HELPERS} == {
        name: f"_query_{name.removeprefix('_')}" for name in SQLITE_QUERY_HELPERS
    }
    for name in SQLITE_QUERY_HELPERS:
        facade_descriptor = sqlite_store.SQLiteVNextStore.__dict__[name]
        carrier_descriptor = sqlite_query.__dict__[name]
        assert facade_descriptor is carrier_descriptor
        if name in SQLITE_STATIC_HELPERS:
            assert isinstance(carrier_descriptor, staticmethod)
            assert carrier_descriptor.__module__ == "alicebot_api.sqlite_store"
            assert carrier_descriptor.__qualname__ == f"SQLiteVNextStore.{name}"
            assert carrier_descriptor.__func__.__module__ == "alicebot_api.sqlite_store"
            assert carrier_descriptor.__func__.__qualname__ == f"SQLiteVNextStore.{name}"
        else:
            assert inspect.isfunction(carrier_descriptor)
            assert carrier_descriptor.__module__ == "alicebot_api.sqlite_store"
            assert carrier_descriptor.__qualname__ == f"SQLiteVNextStore.{name}"


def test_memory_access_support_reexports_remain_identity_compatible() -> None:
    assert postgres_store._search_patterns is retrieval_common._search_patterns
    assert sqlite_store._search_patterns is retrieval_common._search_patterns
    assert postgres_store.FTS_QUERY_STOPWORDS is retrieval_common.FTS_QUERY_STOPWORDS
    assert sqlite_store._FTS_QUERY_STOPWORDS is retrieval_common.FTS_QUERY_STOPWORDS
    assert postgres_store.fts_fallback_tokens is retrieval_common.fts_fallback_tokens
    assert sqlite_store.fts_fallback_tokens is retrieval_common.fts_fallback_tokens
    assert postgres_store._MEMORY_SEARCHABLE_STATUSES_SQL is postgres_memory._MEMORY_SEARCHABLE_STATUSES_SQL
    assert sqlite_store._MEMORY_SEARCHABLE_STATUSES_SQL is sqlite_memory._MEMORY_SEARCHABLE_STATUSES_SQL
    for name in POSTGRES_QUERY_EXPORTS:
        assert getattr(postgres_store, name) is getattr(postgres_query, name)
    for name in SQLITE_QUERY_EXPORTS:
        assert getattr(sqlite_store, name) is getattr(sqlite_query, name)


def test_memory_access_carriers_import_standalone_without_loading_facades() -> None:
    _assert_no_facade_imports(_source_texts())
    cases = (
        "alicebot_api.vnext_stores.retrieval_common",
        "alicebot_api.vnext_stores.postgres.query_predicates",
        "alicebot_api.vnext_stores.postgres.memory_access",
        "alicebot_api.vnext_stores.sqlite.query_predicates",
        "alicebot_api.vnext_stores.sqlite.memory_access",
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for module_name in cases:
        script = (
            f"import {module_name}; import sys; "
            "assert 'alicebot_api.vnext_store' not in sys.modules; "
            "assert 'alicebot_api.sqlite_store' not in sys.modules"
        )
        subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env, check=True)


def test_memory_access_split_enforces_file_size_caps() -> None:
    caps = {
        POSTGRES_FACADE_PATH: 6200,
        SQLITE_FACADE_PATH: 3500,
        REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/retrieval_common.py": 4000,
        REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/query_predicates.py": 4000,
        REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/memory_access.py": 4000,
        REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/query_predicates.py": 4000,
        REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py": 4000,
    }
    for path, cap in caps.items():
        assert len(path.read_text(encoding="utf-8").splitlines()) <= cap, path


@pytest.mark.parametrize(
    ("path", "old", "new"),
    (
        (
            "apps/api/src/alicebot_api/vnext_stores/postgres/memory_access.py",
            "SET LOCAL hnsw.iterative_scan = 'strict_order'",
            "SET LOCAL hnsw.iterative_scan = 'relaxed_order'",
        ),
        (
            "apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py",
            "except sqlite3.OperationalError",
            "except RuntimeError",
        ),
        (
            "apps/api/src/alicebot_api/vnext_stores/sqlite/query_predicates.py",
            "@staticmethod  # type: ignore[misc]  # intentionally graft the descriptor into the facade\ndef _like_any",
            "def _like_any",
        ),
        (
            "apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py",
            "Only compare vectors from the same endpoint fingerprint",
            "Compare vector candidates",
        ),
    ),
)
def test_memory_access_receipts_fail_on_adversarial_drift(path: str, old: str, new: str) -> None:
    texts = _source_texts()
    assert old in texts[path]
    texts[path] = texts[path].replace(old, new, 1)
    with pytest.raises(AssertionError):
        _assert_source_receipts(texts)


def test_memory_access_import_guard_rejects_package_alias_facade_imports() -> None:
    texts = _source_texts()
    path = "apps/api/src/alicebot_api/vnext_stores/retrieval_common.py"
    texts[path] = texts[path].replace(
        "import re",
        "import re\nfrom alicebot_api import vnext_store",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_no_facade_imports(texts)
