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

# Reviewed lock boundary: the pending-candidate row locker takes L first.
SOURCE_RECEIPTS = {
    "apps/api/src/alicebot_api/vnext_stores/retrieval_common.py": (
        "fa1a3a90511b5c61754ba29560e91b7b3058a48d47c143b09d8505d52025b8cc"
    ),
    # Re-minted for the roll-up group scope: a floor identity beside the scope
    # identity, used only by the two roll-up lookups and the artifact list.
    # Previous receipt f0ec9c7f...
    "apps/api/src/alicebot_api/vnext_stores/postgres/query_predicates.py": (
        "f15f1bf2b3da73758c925b63dffaec707f91f00a6e153e53f562fb188859236d"
    ),
    # Re-minted for ``get_memory_by_key(include_deleted=...)`` (reviewed change, not drift; see the SQLite
    # entry below). Previous Postgres receipt 49748ecd...
    # Re-minted once more for ``get_memories_by_ids(include_deleted=...)`` (open-loop ids in every spelling and after
    # deletion; reviewed change, not drift). Proof: the only difference from origin/main daa46ef5 is that one function,
    # which takes a keyword-only ``include_deleted`` (false by default, so every caller reads what it read before)
    # and drops the ``deleted_at IS NULL`` clause only when it is true. Previous Postgres receipt f642880f...
    # Re-minted so the two roll-up lookups overlap scope united with floor.
    # Every other statement still uses the scope expression. Previous receipt 46946cc0...
    "apps/api/src/alicebot_api/vnext_stores/postgres/memory_access.py": (
        "ad6a1a81f077fbdaf13ec414558d43a8b6ad085350aec416e8a5e856a227d7c0"
    ),
    # Re-minted for per-project memory S2 (2026-10-02): the project fence builders read the reserved global
    # marker and take the domains to leave out, and the single-scan partition SQL and the materialized-CTE hint
    # are new. The Postgres carrier is unchanged on purpose: the Postgres runtime resolves no project view.
    # Re-minted once more in the S2 review round (2026-10-02): a request that holds the marker and does not state which
    # global domains it leaves out raises (reviewed change, not drift).
    # Re-minted for alice_project_floor_identity, the fourth identity function,
    # used by the roll-up lookups. Previous receipt eab46f16...
    # Re-minted so a global view also requires every Alice id in the floor.
    # Previous receipt 8beae59c...
    "apps/api/src/alicebot_api/vnext_stores/sqlite/query_predicates.py": (
        "670fd096b461c72517f3791c1f6216778f26d68838307c11d07ffcf5e7b38e79"
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
    # Both carriers were re-minted again for the expiry test (reviewed change, not
    # drift): ``list_memories`` and ``count_memories`` take ``include_expired``,
    # and the roll-up input list and count and the accepted-card lookup leave out a
    # memory whose ``valid_to`` has passed, with recall's own test.
    # Both carriers were re-minted once more for ``get_memory_by_key``, which takes ``include_deleted`` (false
    # by default, so every caller reads what it read before): a soft-deleted row still holds its memory key in
    # the unique index, and the roll-up pass reads the key with ``include_deleted=True`` so a card archived
    # through ``update_memory`` is seen instead of raising on create (reviewed change, not drift). Previous
    # SQLite receipt 3bb85f64...
    # Re-minted again for per-project memory S2 (2026-10-02): ``list_memories`` builds its filters in
    # ``_memory_list_clauses`` and takes ``exclude_global_domains``, and ``list_memories_view_partitions`` is new
    # (reviewed change, not drift).
    # Re-minted once more in the S2 review round (2026-10-02): ``exclude_global_domains`` defaults to ``None`` and the
    # single-scan reader takes ``domains`` and ``sensitivity_allowed`` as required arguments (reviewed change, not drift).
    # Re-minted for the merge of main into the S2 branch (2026-10-02): the file now holds both reviewed changes above.
    # Previous receipts: 8a0bcba8... on the S2 branch and 3d2f1732... on main.
    # Re-minted for ``get_memories_by_ids(include_deleted=...)`` (open-loop ids in every spelling and after deletion;
    # reviewed change, not drift). Proof: the only difference from origin/main daa46ef5 is that one function, which
    # takes a keyword-only ``include_deleted`` (false by default) and drops the ``deleted_at IS NULL`` clause only when
    # it is true. Previous SQLite receipt 91636de9...
    # Re-minted so the two roll-up lookups overlap scope or floor. Previous receipt 64f21989...
    # Re-minted so the memory partition read passes the floor identity.
    # Previous receipt 1580dca3...
    "apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py": (
        "58e22342c6aaf4ed73aa78e5f2ee85af1d8deeb7cbad6a3abe06f78fb8f9a0ac"
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
    "list_memories_view_partitions",
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
    "_PROJECT_FLOOR_SQL",
    "_MEMORY_GROUP_SCOPE_SQL",
    "_jsonb_string_array_identity_sql",
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
    # Per-file importer savepoint (2026-10-02): one paired method more, ``savepoint``, appended last.
    # Previous receipt: (171, 526374782104a2a1...). Proof: the member list equals the list at origin/main
    # 040a2a10 with ``savepoint`` added at the end and nothing else moved (reviewed change, not drift).
    # Label lock: lock_label_writes and read_label_rows follow __init__. Dropping
    # those two names restores the previous receipt (172, 6f1a459f...).
    "PostgresVNextStore": (174, "095250b8a77d6c0a32d2783343916b947bcae8ae86e3a1693e47c2fb11802211"),
    # One SQLite-only method more, ``check_source_search_query``: the Postgres
    # source search has no expression-depth or LIKE-length limit to check.
    # Merge of #500 and #502 (2026-10-01): one more SQLite-only method,
    # ``check_literal_match_query``, beside the paired
    # ``list_memories_referencing_sources``. Re-minted for the merged facade
    # (reviewed change, not drift).
    # Per-project memory S2 (2026-10-02): two SQLite-only methods more, the single-scan partition reads
    # ``list_memories_view_partitions`` and ``list_open_loops_view_partitions``. The Postgres runtime resolves no
    # project view, so it has no pair. Re-minted for the facade (reviewed change, not drift).
    # Per-file importer savepoint (2026-10-02): the same paired method, ``savepoint``, appended last.
    # Previous receipt: (128, fae6bee37a2b06541...). Proof: the member list equals the list at origin/main
    # 040a2a10 with ``savepoint`` added at the end and nothing else moved (reviewed change, not drift).
    # Proof: the replacement branch gains only scrub_source, source_inventory and
    # prunable_sources here; every pre-existing class member keeps its order.
    # Label lock: lock_label_writes and read_label_rows follow __init__. Dropping
    # those two names restores the previous receipt (134, 13012720...).
    "SQLiteVNextStore": (136, "1d99dbaf69e4ea888ca7beb2389ac176650a2e573c067bf0686adc9e609132f5"),
}


#: Keyword arguments the SQLite readers take and the Postgres readers do not (per-project memory S2, 2026-10-02).
#: ``exclude_global_domains`` leaves out global rows in those domains before ``LIMIT`` when the request tuple holds
#: the reserved global marker. The Postgres runtime resolves no project view, so its readers have no such argument,
#: and every other parameter must still match.
SQLITE_ONLY_PARAMETERS = frozenset({"exclude_global_domains"})


def _without_sqlite_only_parameters(signature: inspect.Signature) -> inspect.Signature:
    return signature.replace(
        parameters=[value for key, value in signature.parameters.items() if key not in SQLITE_ONLY_PARAMETERS]
    )


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
    assert sqlite_predicates.count("create_function(") == 4


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
        assert _without_sqlite_only_parameters(
            inspect.signature(getattr(sqlite_store.SQLiteVNextStore, name))
        ) == inspect.signature(getattr(postgres_store.PostgresVNextStore, name))


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
