from __future__ import annotations

import ast
import hashlib
import inspect
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import alicebot_api.sqlite_store as sqlite_store
import alicebot_api.vnext_store as postgres_store
from alicebot_api.vnext_embeddings import memory_embedding_content_sha256
from alicebot_api.vnext_stores.postgres import columns as postgres_columns
from alicebot_api.vnext_stores.postgres import embedding_cas as postgres_embedding_cas
from alicebot_api.vnext_stores.sqlite import columns as sqlite_columns
from alicebot_api.vnext_stores.sqlite import embedding_cas as sqlite_embedding_cas


REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_store.py"
SQLITE_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/sqlite_store.py"
POSTGRES_CARRIER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/embedding_cas.py"
SQLITE_CARRIER_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/embedding_cas.py"
POSTGRES_COLUMNS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/columns.py"
SQLITE_COLUMNS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/columns.py"

METHOD_NAMES = (
    "update_memory_embedding",
    "clear_memory_embedding",
    "list_memories_missing_embeddings",
)
EXPECTED_METHOD_AST_SHA256 = {
    "postgres": {
        "update_memory_embedding": "0cd0f0ef6f7bcaa6328b6f586a711a10b011c77af3e49d65b96c27b77a657cd9",
        "clear_memory_embedding": "4e9fe6955f3246b51998c6b547f48a659f947f8a8150e6c86d4e61a0cf46df6c",
        "list_memories_missing_embeddings": "ed175e14c8506a435f522f7791a0cb800413bf7ca12b55d835a6faab21b0d000",
    },
    # SQLite update/clear re-minted for the Phase 4 Stage 2 resident vector
    # cache (reviewed carrier change): both methods point-read whether a
    # non-NULL embedding exists and bump the embedding_stamp token when a
    # live vector is overwritten or cleared -- and now take the writer lock
    # (BEGIN IMMEDIATE, unless already in a transaction) BEFORE that read,
    # so the bump decision is atomic with the write (no TOCTOU window
    # against a concurrent embed-on-write).
    #
    # Both backends' update and list methods were re-minted again for the
    # embedding input cap (reviewed carrier change): update takes an optional
    # ``truncated_to_chars`` and writes it into the signature only when the
    # text was cut; list takes an optional ``embedding_input_cap`` and then also
    # lists a row whose stored cut label is not the label a vector made now
    # would carry. With neither argument given the generated SQL is byte for
    # byte what it was, which ``test_embedding_cas_generated_sql_is_byte_identical``
    # still pins.
    "sqlite": {
        "update_memory_embedding": "1f4517352a0f7d6a9147f326bc96a6c1d61effa3f106add89546cd981ddd05fc",
        "clear_memory_embedding": "51b583b250883911f0c5a068fec7ec4565f719c2bffafb1ed1c6b3dc980fa36c",
        "list_memories_missing_embeddings": "58aa82bc1ecbc4809b40d70befc2841d2692243a5df3f6da9b036e4b91527aa2",
    },
}
EXPECTED_SUPPORT_AST_SHA256 = {
    "postgres_columns": ("MEMORY_COLUMNS", "867724f6d135f733bf8091838f86432e54567002554ff7577580e6b005795016"),
    "postgres_strip_codepoints": (
        "_PYTHON_312_STRIP_CODEPOINTS",
        "fcefbffd9366db87733e863c6f6f726dd8a626cf8e5f426f708504ae6f11cab2",
    ),
    "postgres_strip_sql": (
        "_PYTHON_312_STRIP_CHARS_SQL",
        "9ab844dbb08f868d4aa530d9ad463ff4cf7ca8a4d1e2a92e3b2716051d48e420",
    ),
    "postgres_strip_function": (
        "_python_312_strip_sql",
        "442c39160e9c39bb9b053810eeddaada96b06b717626035506f6c66e8ff346f0",
    ),
    "postgres_digest_sql": (
        "_MEMORY_EMBEDDING_CONTENT_SHA256_SQL",
        "ffeb57914fb425ef61b4a6d64f1b865357beb0566f37fb237c9f22bff913d1d0",
    ),
    "postgres_vector": (
        "_vector_literal",
        "ae3ceb92a7583015a26695c40f64131cfd3731928b636ebbc97e28ec41bdfb0a",
    ),
    "postgres_text_chars_sql": (
        "_MEMORY_EMBEDDING_TEXT_CHARS_SQL",
        "09dbf72a100f9306eecdd21982fda8ee60a2b5045bde1aef3d0cb195aca1d74f",
    ),
    "sqlite_columns": ("MEMORY_COLUMNS", "262cceffd732759a4f0b8d0d9809e7387b3d2b99f9ac7fe781f5749894d44679"),
    "sqlite_digest_udf": (
        "_embedding_content_sha256_sqlite",
        "f05089935ff785de057bcace8c4cdc2efde4622c987eb9a1c166def6da00317a",
    ),
    "sqlite_register_udf": (
        "_ensure_embedding_content_sha256_sqlite",
        "4c9e94f8ffb659534bb7e1174e19d9a477fb0dd1e493466139a87d21b5866ce7",
    ),
    "sqlite_input_cut_udf": (
        "_embedding_input_cut_sqlite",
        "0c0405578793905bb01daa4e3232e49914e9c56f616f19452cba3cbf750aeb18",
    ),
    "sqlite_register_input_cut_udf": (
        "_ensure_embedding_input_cut_sqlite",
        "9693ce8e49a924e6d6624fac3e867a31e27e869830e0350b4578e8ff4acae547",
    ),
    "sqlite_missing_clause": (
        "_missing_embeddings_clause",
        "17f4bb82aa50dd6f9556514f5a7b4107804d384784309b550c0db2cfc24544b8",
    ),
    "sqlite_count_missing": (
        "count_memories_missing_embeddings",
        "dd490ddf8594606dac77d9944306e5947a09c20cf3f07addcf1314f481fb1a1e",
    ),
}
EXPECTED_SIGNATURES = {
    "update_memory_embedding": (
        "(self, *, memory_id: 'str', vector: 'list[float]', provider: 'str | None' = None, "
        "model: 'str | None' = None, endpoint: 'str | None' = None, content_sha256: 'str | None' = None, "
        "signature_version: 'int' = 1, truncated_to_chars: 'int | None' = None) -> 'VNextRow | None'"
    ),
    "clear_memory_embedding": "(self, *, memory_id: 'str') -> 'VNextRow | None'",
    "list_memories_missing_embeddings": (
        "(self, *, limit: 'int' = 100, after_id: 'str | None' = None, "
        "embedding_provider: 'str | None' = None, embedding_model: 'str | None' = None, "
        "embedding_endpoint: 'str | None' = None, embedding_signature_version: 'int | None' = None, "
        "embedding_input_cap: 'int | None' = None) -> 'list[VNextRow]'"
    ),
}
EXPECTED_QUERY_SHA256 = {
    "postgres_unsigned_update": ("dcbf4bc29a7702e9c17d864f65e1c1f36d641927d3f31aa4ec80825646c030ef",),
    "postgres_signed_update": ("1351db18168f7e23454736129e26a7c01039ca1bfdfcac235e3c666cf60d91db",),
    "postgres_clear": ("a5a6952a93bd77b3bdf311fe2682b411263d18a2822a9617c6fb7524555123ca",),
    "postgres_unsigned_missing": ("13a290c0fb56e0abb6b4feb15fbd96642ba60478b3d7e8e666ef904135002f31",),
    "postgres_signed_missing": ("d7af83168e7337a5798f1f9f201c5eb0863121b32d2f769a440dd1b099b01226",),
    # SQLite update/clear sequences start with BEGIN IMMEDIATE (the capture
    # connection is autocommit-shaped) followed by the Stage 2
    # embedding-presence point-read (the vector-cache invalidation gate),
    # now inside the writer transaction. The capture store reports no live
    # embedding, so no stamp bump appears in these sequences.
    "sqlite_unsigned_update": (
        "930a7770399087898ae6ac96ce5375048117486e06b21da4523d2c3c75113c32",
        "4ef4eca2da3716e062d072149d5b9dd1a84de1d50557e7e534890c1d93934844",
        "f9c780945f863a6735df31f7552b1588b938499ff427a97ec86ca168990c7631",
        "4c02258b8fe75dc0cf54d352a81badde39d952bc69eae56cd12edb1505165ff4",
    ),
    "sqlite_signed_update": (
        "930a7770399087898ae6ac96ce5375048117486e06b21da4523d2c3c75113c32",
        "4ef4eca2da3716e062d072149d5b9dd1a84de1d50557e7e534890c1d93934844",
        "c037557387b90f9e0becd053c1e5b620579596e187e4d84865ebf1c32d09340a",
        "4c02258b8fe75dc0cf54d352a81badde39d952bc69eae56cd12edb1505165ff4",
    ),
    "sqlite_clear": (
        "930a7770399087898ae6ac96ce5375048117486e06b21da4523d2c3c75113c32",
        "4ef4eca2da3716e062d072149d5b9dd1a84de1d50557e7e534890c1d93934844",
        "7049f5693c64baa495f701ff8492f8c3dac4f6eb2cce1fcb7ae745141c04951a",
        "4c02258b8fe75dc0cf54d352a81badde39d952bc69eae56cd12edb1505165ff4",
    ),
    "sqlite_unsigned_missing": ("e8149c28b861289b5899d7c8fce9efea98b2e809fed5ee209ee4490cb3712cae",),
    "sqlite_signed_missing": ("79eb432dd9482731107d8f4f62231755dc2ecb21db0a17fa3f6de3979def3a7c",),
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def _top_level_named_nodes(tree: ast.Module) -> dict[str, list[ast.AST]]:
    result: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        name: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
        if name is not None:
            result.setdefault(name, []).append(node)
    return result


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _class_method_names(node: ast.ClassDef) -> list[str]:
    return [child.name for child in node.body if isinstance(child, ast.FunctionDef)]


def _query_hashes(queries: list[tuple[str, object]]) -> tuple[str, ...]:
    return tuple(hashlib.sha256(query.encode()).hexdigest() for query, _params in queries)


class _PostgresCursor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, object]] = []

    def __enter__(self) -> _PostgresCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.queries.append((query, params))

    def fetchone(self) -> dict[str, object]:
        return {"id": "00000000-0000-0000-0000-000000000001"}

    def fetchall(self) -> list[dict[str, object]]:
        return []


class _PostgresConnection:
    def __init__(self) -> None:
        self.cursor_instance = _PostgresCursor()

    def cursor(self) -> _PostgresCursor:
        return self.cursor_instance


class _SQLiteResult:
    rowcount = 1


class _SQLiteCaptureConnection:
    """Autocommit-shaped connection stub: update/clear must BEGIN IMMEDIATE."""

    def __init__(self, queries: list[tuple[str, object]]) -> None:
        self.in_transaction = False
        self._queries = queries

    def execute(self, query: str, params: object = ()) -> _SQLiteResult:
        self._queries.append((query, params))
        self.in_transaction = True
        return _SQLiteResult()


class _SQLiteCapture:
    def __init__(self) -> None:
        self.user_id = "user"
        self.queries: list[tuple[str, object]] = []
        self.conn = _SQLiteCaptureConnection(self.queries)

    def _execute(self, query: str, params: object = ()) -> _SQLiteResult:
        self.queries.append((query, params))
        return _SQLiteResult()

    def _fetch_optional_one(self, query: str, params: object = ()) -> dict[str, object]:
        self.queries.append((query, params))
        return {"id": "memory"}

    def _fetch_all(self, query: str, params: object = ()) -> list[dict[str, object]]:
        self.queries.append((query, params))
        return []


def test_embedding_cas_methods_are_moved_once_and_grafted_without_wrappers() -> None:
    facade_specs = (
        (POSTGRES_FACADE_PATH, "PostgresVNextStore"),
        (SQLITE_FACADE_PATH, "SQLiteVNextStore"),
    )
    for path, class_name in facade_specs:
        class_node = _class_node(_tree(path), class_name)
        assert set(_class_method_names(class_node)).isdisjoint(METHOD_NAMES)
        assignments = [
            node.targets[0].id
            for node in class_node.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in METHOD_NAMES
        ]
        assert assignments == list(METHOD_NAMES)

    for backend, path in (("postgres", POSTGRES_CARRIER_PATH), ("sqlite", SQLITE_CARRIER_PATH)):
        definitions = _top_level_named_nodes(_tree(path))
        for method_name in METHOD_NAMES:
            assert len(definitions.get(method_name, [])) == 1
            node = definitions[method_name][0]
            assert isinstance(node, ast.FunctionDef)
            assert _digest(node) == EXPECTED_METHOD_AST_SHA256[backend][method_name]

    runtime_specs = (
        (postgres_store.PostgresVNextStore, postgres_embedding_cas, "alicebot_api.vnext_store"),
        (sqlite_store.SQLiteVNextStore, sqlite_embedding_cas, "alicebot_api.sqlite_store"),
    )
    for facade_class, carrier, module_name in runtime_specs:
        assert facade_class.__bases__ == (object,)
        assert facade_class.__mro__ == (facade_class, object)
        keys = list(facade_class.__dict__)
        assert keys[keys.index("search_memories_by_time") + 1 : keys.index("lock_graph_mutation")] == list(METHOD_NAMES)
        for method_name in METHOD_NAMES:
            method = getattr(facade_class, method_name)
            assert method is getattr(carrier, method_name)
            assert str(inspect.signature(method)) == EXPECTED_SIGNATURES[method_name]
            assert method.__module__ == module_name
            assert method.__qualname__ == f"{facade_class.__name__}.{method_name}"


def test_embedding_cas_support_nodes_and_old_module_reexports_are_exact() -> None:
    trees = {
        "postgres_columns": _tree(POSTGRES_COLUMNS_PATH),
        "postgres_strip_codepoints": _tree(POSTGRES_CARRIER_PATH),
        "postgres_strip_sql": _tree(POSTGRES_CARRIER_PATH),
        "postgres_strip_function": _tree(POSTGRES_CARRIER_PATH),
        "postgres_digest_sql": _tree(POSTGRES_CARRIER_PATH),
        "postgres_text_chars_sql": _tree(POSTGRES_CARRIER_PATH),
        "postgres_vector": _tree(POSTGRES_CARRIER_PATH),
        "sqlite_columns": _tree(SQLITE_COLUMNS_PATH),
        "sqlite_digest_udf": _tree(SQLITE_CARRIER_PATH),
        "sqlite_register_udf": _tree(SQLITE_CARRIER_PATH),
        "sqlite_input_cut_udf": _tree(SQLITE_CARRIER_PATH),
        "sqlite_register_input_cut_udf": _tree(SQLITE_CARRIER_PATH),
        "sqlite_missing_clause": _tree(SQLITE_CARRIER_PATH),
        "sqlite_count_missing": _tree(SQLITE_CARRIER_PATH),
    }
    for key, (name, expected_digest) in EXPECTED_SUPPORT_AST_SHA256.items():
        nodes = _top_level_named_nodes(trees[key]).get(name, [])
        assert len(nodes) == 1
        assert _digest(nodes[0]) == expected_digest

    assert postgres_store.MEMORY_COLUMNS is postgres_columns.MEMORY_COLUMNS
    assert sqlite_store.MEMORY_COLUMNS is sqlite_columns.MEMORY_COLUMNS
    assert postgres_store._PYTHON_312_STRIP_CODEPOINTS is postgres_embedding_cas._PYTHON_312_STRIP_CODEPOINTS
    assert postgres_store._PYTHON_312_STRIP_CHARS_SQL is postgres_embedding_cas._PYTHON_312_STRIP_CHARS_SQL
    assert postgres_store._MEMORY_EMBEDDING_CONTENT_SHA256_SQL is (
        postgres_embedding_cas._MEMORY_EMBEDDING_CONTENT_SHA256_SQL
    )
    assert postgres_store._python_312_strip_sql is postgres_embedding_cas._python_312_strip_sql
    assert postgres_store._vector_literal is postgres_embedding_cas._vector_literal
    assert sqlite_store._embedding_content_sha256_sqlite is sqlite_embedding_cas._embedding_content_sha256_sqlite
    assert sqlite_store._ensure_embedding_content_sha256_sqlite is (
        sqlite_embedding_cas._ensure_embedding_content_sha256_sqlite
    )

    assert type(postgres_store.MEMORY_COLUMNS) is str
    assert len(postgres_store.MEMORY_COLUMNS) == 1254
    assert hashlib.sha256(postgres_store.MEMORY_COLUMNS.encode()).hexdigest() == (
        "fde43f0166a95d3b2363274bb26cd3ad047c45de7b9fc50b23fe8d021f7c8a89"
    )
    assert type(sqlite_store.MEMORY_COLUMNS) is tuple
    assert len(sqlite_store.MEMORY_COLUMNS) == 39
    assert hashlib.sha256(repr(sqlite_store.MEMORY_COLUMNS).encode()).hexdigest() == (
        "a3aea617f10cc52cc01dc974e0ae7bacc10384a417617729146300f1c472ee54"
    )
    assert hashlib.sha256(repr(postgres_store._PYTHON_312_STRIP_CODEPOINTS).encode()).hexdigest() == (
        "e8d7321f271ab5d78db165cf718df15c1db3cd42532a5ea8d8c83fa6d4b119cf"
    )
    assert hashlib.sha256(postgres_store._PYTHON_312_STRIP_CHARS_SQL.encode()).hexdigest() == (
        "be69a09ba91f126449ada249270b85b942c7e9a2b0e54dcabc74ff3c86d9c4c5"
    )
    assert hashlib.sha256(postgres_store._MEMORY_EMBEDDING_CONTENT_SHA256_SQL.encode()).hexdigest() == (
        "7f5ef7bc3d1c489800a39c9a21b824990a6d92b65cef1de90bab248b11eb03ba"
    )
    assert hashlib.sha256(postgres_embedding_cas._MEMORY_EMBEDDING_TEXT_CHARS_SQL.encode()).hexdigest() == (
        "c2d8e56d80af6485667434446914d6776d3d6da91b4e50f8c1c3212910995f3a"
    )
    assert postgres_store.__all__ == ["PostgresVNextStore", "VNextRow"]
    assert sqlite_store.__all__ == [
        "REDACTION_MARKER",
        "SQLiteVNextStore",
        "ensure_sqlite_user",
        "sqlite_user_connection",
    ]


def test_embedding_cas_modules_do_not_import_the_facades_and_import_in_either_order() -> None:
    for path in (POSTGRES_CARRIER_PATH, SQLITE_CARRIER_PATH, POSTGRES_COLUMNS_PATH, SQLITE_COLUMNS_PATH):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
            elif isinstance(node, ast.Import):
                assert all(
                    alias.name not in {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
                    for alias in node.names
                )

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for statement in (
        "import alicebot_api.vnext_store; import alicebot_api.sqlite_store",
        "import alicebot_api.sqlite_store; import alicebot_api.vnext_store",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", statement],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_embedding_cas_generated_sql_is_byte_identical() -> None:
    memory_id = "00000000-0000-0000-0000-000000000001"

    def postgres_queries(call: Any) -> tuple[str, ...]:
        connection = _PostgresConnection()
        call(postgres_store.PostgresVNextStore(connection))
        return _query_hashes(connection.cursor_instance.queries)

    observed_postgres = {
        "postgres_unsigned_update": postgres_queries(
            lambda store: store.update_memory_embedding(memory_id=memory_id, vector=[1.0])
        ),
        "postgres_signed_update": postgres_queries(
            lambda store: store.update_memory_embedding(
                memory_id=memory_id,
                vector=[1.0],
                provider="provider",
                model="model",
                endpoint="endpoint",
                content_sha256="digest",
                signature_version=2,
            )
        ),
        "postgres_clear": postgres_queries(lambda store: store.clear_memory_embedding(memory_id=memory_id)),
        "postgres_unsigned_missing": postgres_queries(lambda store: store.list_memories_missing_embeddings()),
        "postgres_signed_missing": postgres_queries(
            lambda store: store.list_memories_missing_embeddings(
                embedding_provider="provider",
                embedding_model="model",
                embedding_endpoint="endpoint",
                embedding_signature_version=2,
            )
        ),
    }

    def sqlite_queries(call: Any) -> tuple[str, ...]:
        capture = _SQLiteCapture()
        call(capture)
        return _query_hashes(capture.queries)

    observed_sqlite = {
        "sqlite_unsigned_update": sqlite_queries(
            lambda store: sqlite_store.SQLiteVNextStore.update_memory_embedding(
                store, memory_id="memory", vector=[1.0]
            )
        ),
        "sqlite_signed_update": sqlite_queries(
            lambda store: sqlite_store.SQLiteVNextStore.update_memory_embedding(
                store,
                memory_id="memory",
                vector=[1.0],
                provider="provider",
                model="model",
                endpoint="endpoint",
                content_sha256="digest",
                signature_version=2,
            )
        ),
        "sqlite_clear": sqlite_queries(
            lambda store: sqlite_store.SQLiteVNextStore.clear_memory_embedding(store, memory_id="memory")
        ),
        "sqlite_unsigned_missing": sqlite_queries(
            lambda store: sqlite_store.SQLiteVNextStore.list_memories_missing_embeddings(store)
        ),
        "sqlite_signed_missing": sqlite_queries(
            lambda store: sqlite_store.SQLiteVNextStore.list_memories_missing_embeddings(
                store,
                embedding_provider="provider",
                embedding_model="model",
                embedding_endpoint="endpoint",
                embedding_signature_version=2,
            )
        ),
    }
    assert observed_postgres | observed_sqlite == EXPECTED_QUERY_SHA256


def test_sqlite_embedding_digest_udf_preserves_nbsp_and_control_strip_parity() -> None:
    title = "\u00a0Title\u00a0"
    canonical_text = "\x1cCanonical\x1f"
    summary = "\u202fSummary\u205f"
    expected = memory_embedding_content_sha256(
        {"title": title, "canonical_text": canonical_text, "summary": summary}
    )
    assert sqlite_embedding_cas._embedding_content_sha256_sqlite(title, canonical_text, summary) == expected

    conn = sqlite3.connect(":memory:")
    try:
        sqlite_store.SQLiteVNextStore(conn, "user")
        sqlite_store.SQLiteVNextStore(conn, "user")
        row = conn.execute(
            "SELECT alice_embedding_content_sha256(?, ?, ?)",
            (title, canonical_text, summary),
        ).fetchone()
        registered = conn.execute(
            "SELECT count(*) FROM pragma_function_list "
            "WHERE name = 'alice_embedding_content_sha256' AND narg = 3"
        ).fetchone()
    finally:
        conn.close()
    assert row == (expected,)
    assert registered == (1,)
