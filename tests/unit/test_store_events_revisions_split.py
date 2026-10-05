from __future__ import annotations

import ast
from datetime import UTC, datetime
import hashlib
import inspect
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import alicebot_api.sqlite_store as sqlite_store
import alicebot_api.vnext_store as postgres_store
from alicebot_api.vnext_stores.postgres import columns as postgres_columns
from alicebot_api.vnext_stores.postgres import events_revisions as postgres_events
from alicebot_api.vnext_stores.postgres import primitives as postgres_primitives
from alicebot_api.vnext_stores.sqlite import columns as sqlite_columns
from alicebot_api.vnext_stores.sqlite import events_revisions as sqlite_events
from alicebot_api.vnext_stores.sqlite import primitives as sqlite_primitives


REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_store.py"
SQLITE_FACADE_PATH = REPO_ROOT / "apps/api/src/alicebot_api/sqlite_store.py"
POSTGRES_EVENTS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/events_revisions.py"
SQLITE_EVENTS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/events_revisions.py"
POSTGRES_COLUMNS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/columns.py"
SQLITE_COLUMNS_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/columns.py"
POSTGRES_PRIMITIVES_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/postgres/primitives.py"
SQLITE_PRIMITIVES_PATH = REPO_ROOT / "apps/api/src/alicebot_api/vnext_stores/sqlite/primitives.py"

METHOD_NAMES = (
    "_append_mutation_event",
    "append_event",
    "list_events",
    "list_events_for_source_trace",
    "list_project_update_events",
    "count_events",
    "append_revision",
    "list_revisions",
)
EXPECTED_METHOD_AST_SHA256 = {
    "postgres": {
        "_append_mutation_event": "294b2174082ac9f670e15cb6664c9968c2961d2c061a200c96c2bdecc55b87c9",
        "append_event": "e1624ec8cb156e52dfd8f821032689378ab49863ad0d4a2e68cf6176cfd8c89c",
        "list_events": "7659f8bcbf050155eca1d61f575854e3c54120944e46d0d7aa24bce8af75e8fe",
        "list_events_for_source_trace": "20f22e3b75c3612c02c4242bc973295b2535b01f95e5451a0a0bff1196f187c3",
        "list_project_update_events": "2bd457ee19535f203da5c31e557387f7717fefc27cbef2c546a62bbad74962d3",
        "count_events": "e740e4b09ecfda973ef6b84acd0ea808b26d118faf6984e4057d7e104e595fb5",
        "append_revision": "6070b96a01a50072e4e898bfb64ddbbf253be0478fe63205f760d0fe016a109a",
        "list_revisions": "10a485935b59bda1fcb33b36ba48b8d86376b9fd180bf9ebf6358b5dfbd24f55",
    },
    "sqlite": {
        "_append_mutation_event": "75922431a64c17ca369cd9b57e360a63b492e45d706af33d572b958ea287e27a",
        "append_event": "61927c2a8ed03ceff60c2b993ef1155cee01a5706e21ebee3e002a988820e052",
        "list_events": "1fe66068556bfe8263a31b4da9e0e69aff91d54c16bb041c381ff55b720af0e6",
        "list_events_for_source_trace": "64a122d098df8c8f62ef74654235f6bfd4d4322d908089c23e53564ef80f3eae",
        "list_project_update_events": "7e80021e9b6023c65c28f356ccda3242f202f446724628ec90aef87155c309ca",
        "count_events": "0387b6810557e231ab2dccacbf2f82da55984060ff986d43e1409b094f0ceb54",
        "append_revision": "1bce7dcc1a86bda28da10caf44d0e195e4cdbdabc518f12f0df9efb241ca598e",
        "list_revisions": "1aa77a7c16efe104eead4eed971ab8fb8fbaa686c81d9dbd94ee3b6bf3d136e6",
    },
}
EXPECTED_SOURCE_LINE_COUNTS = {
    "postgres": (24, 48, 50, 58, 26, 18, 90, 10),
    "sqlite": (24, 36, 38, 45, 26, 21, 72, 11),
}
EXPECTED_SIGNATURES = {
    "_append_mutation_event": (
        "(self, *, event_type: 'str', actor_type: 'str', target_type: 'str', target_id: 'object', "
        "payload: 'JsonObject', actor_id: 'str | None' = None, trace_id: 'str | None' = None, "
        "run_id: 'str | None' = None) -> 'VNextRow'"
    ),
    "append_event": "(self, event: 'JsonObject') -> 'VNextRow'",
    "list_events": (
        "(self, *, target_type: 'str | None' = None, target_id: 'str | None' = None, "
        "occurred_at_start: 'datetime | None' = None, occurred_at_end: 'datetime | None' = None, "
        "limit: 'int | None' = None) -> 'list[VNextRow]'"
    ),
    "list_events_for_source_trace": (
        "(self, *, source_id: 'str', memory_ids: 'Sequence[str]' = (), artifact_ids: 'Sequence[str]' = (), "
        "open_loop_ids: 'Sequence[str]' = (), limit: 'int' = 500) -> 'list[VNextRow]'"
    ),
    "list_project_update_events": (
        "(self, *, artifact_id: 'str', candidate_memory_id: 'str') -> 'list[VNextRow]'"
    ),
    "count_events": "(self, *, target_type: 'str | None' = None, target_id: 'str | None' = None) -> 'int'",
    "append_revision": "(self, revision: 'JsonObject', *, actor_type: 'str' = 'system') -> 'VNextRow'",
    "list_revisions": "(self, memory_id: 'str') -> 'list[VNextRow]'",
}
EXPECTED_METHOD_DOCS = {
    "postgres": {
        "_append_mutation_event": None,
        "append_event": None,
        "list_events": None,
        "list_events_for_source_trace": "Bound source-trace events with relationship predicates before LIMIT.",
        "list_project_update_events": (
            "Return every creation/decision event coupled to one project update.\n\n"
            "        Direct targets and every supported payload-only linkage are selected\n"
            "        in SQL so terminal replay is proportional to the coupled evidence,\n"
            "        rather than to the user's complete append-only event log.\n"
            "        "
        ),
        "count_events": "Count matching event rows without materializing the append-only log.",
        "append_revision": None,
        "list_revisions": None,
    },
    "sqlite": {
        "_append_mutation_event": None,
        "append_event": None,
        "list_events": None,
        "list_events_for_source_trace": "Bound source-trace events with predicates before LIMIT.",
        "list_project_update_events": "Return every creation/decision event coupled to one project update.",
        "count_events": "Count matching event rows without materializing the event log.",
        "append_revision": None,
        "list_revisions": None,
    },
}
EXPECTED_PRIMITIVE_METADATA = {
    "postgres": {
        "_json_object": ("(value: 'object | None') -> 'Jsonb'", None),
        "_json_list": ("(value: 'object | None') -> 'Jsonb'", None),
        "_json_safe": ("(value: 'object') -> 'object'", None),
    },
    "sqlite": {
        "_utc_now_iso": ("() -> 'str'", None),
        "_iso_or_none": (
            "(value: 'object | None') -> 'str | None'",
            "Normalize timestamps to ISO-8601 UTC TEXT with a trailing ``Z``.",
        ),
        "_iso_or_now": ("(value: 'object | None') -> 'str'", None),
        "_new_id": ("(value: 'object | None') -> 'str'", None),
        "_uuid_text": ("(value: 'object | None') -> 'str | None'", None),
        "_json_object_text": ("(value: 'object | None') -> 'str'", None),
        "_json_list_text": ("(value: 'object | None') -> 'str'", None),
    },
}
EXPECTED_CLASS_KEY_SHA256 = {
    # Reviewed recovery and reverse propagation add only the two Postgres
    # methods; all prior runtime class keys retain their order.
    # Source owner methods are additive; the existing member order is unchanged.
    # Re-minted for the paired browser-clip capability façade methods.
    # The sqlite hash is re-minted again for ``check_source_search_query``. It is
    # SQLite only on purpose: the Postgres source search has no expression-depth
    # or LIKE-length limit to check. Both are re-minted again for the paired
    # ``list_memories_referencing_sources`` method.
    # Re-minted for the per-file importer savepoint (2026-10-02): ``savepoint`` is appended last on both façades.
    # Previous receipt: 650e2e0ff088d67c... Proof: the class key list equals the list at origin/main 040a2a10
    # plus ``savepoint`` before ``__dict__``, with every other key in the same order.
    "postgres": "e4a921e06bc707a362fb9c2b3050d292b119cd0b92b346ce5997a679b3bd61f0",
    # Re-minted for the merge of #500 and #502: ``check_literal_match_query``.
    # Re-minted again for per-project memory S2 (2026-10-02): the two single-scan partition reads
    # ``list_memories_view_partitions`` and ``list_open_loops_view_partitions``, SQLite only on purpose
    # (the Postgres runtime resolves no project view).
    # Re-minted again for the per-file importer savepoint (2026-10-02), the same appended method.
    # Previous receipt: 365b7a01acf7a8b5... Proof: as for postgres, one added key and no other change.
    # Re-minted for the derived-label lock: ``lock_label_writes`` and ``read_label_rows`` on both facades.
    "sqlite": "3212a93f2011cecddb6b0002a3f9b3abe37819d3582b2a1173846871981a5ff9",
}
EXPECTED_SUPPORT_AST_SHA256 = {
    "postgres_columns": {
        "EVENT_LOG_COLUMNS": "6268d52ae832dc322834749bb838f2fb3184e2f9b6bdbd3b0e5a0418fcf1ac94",
        "REVISION_COLUMNS": "8fde7e72365924ab88a5e04baf9585e1864b6476f9ef8d762523c7cae91b5b19",
    },
    "sqlite_columns": {
        "EVENT_LOG_COLUMNS": "9cd28fd41d2f5f67541f175f22220ede3a18a969720f28487a418ccfb0b9c150",
        "REVISION_COLUMNS": "befaa23c4c7dc1a3be13e9e8edbc6f0c688eeb62a4df0a99202c9cbc682e4b80",
    },
    "postgres_events": {
        "_PROJECT_UPDATE_EVENT_TYPES_SQL": "2a448e007aaf71b05309be347cbc8635656feadde5ee4c156010a001d6ad344f",
        "_PROJECT_UPDATE_EVENT_LINKAGE_SQL": "0207b31c042d94f4d192ab68b2aa4642b7fdfa04e5e093e1571eccbaf953262b",
        "_PROJECT_UPDATE_EVENT_LOOKUP_SQL": "30bd31f81a691dd7ff4f8f0a7c9c940e59785dd2a46a605f7615eac863685ad7",
    },
    "sqlite_events": {
        "_PROJECT_UPDATE_EVENT_TYPES_SQL": "2a448e007aaf71b05309be347cbc8635656feadde5ee4c156010a001d6ad344f",
        "_PROJECT_UPDATE_EVENT_LINKAGE_SQL": "15bfcad3ad281b48c5ee825a3688c9abde96418d62d792848a1ca3ceecdd6de3",
        "_PROJECT_UPDATE_EVENT_LOOKUP_SQL": "58b6a1f37cbd62171224e7823a1f1f8bfb284509b27017c4ae1e28d99b8d749e",
    },
    "postgres_primitives": {
        "_json_object": "8c6c01160d66d8dac637f61309082ef81c6d23751e8ad80de24fb04f30f4ea8c",
        "_json_list": "43c68fb97fe573187144b4cff4d824da3f6b00e130463dca1461e7b79227ee65",
        "_json_safe": "86eb908be088d52e6ff087202647571036e3aacc1d5421ae808ed9f9e400c9e4",
    },
    "sqlite_primitives": {
        "_utc_now_iso": "eb8726526a323ae5fce20d18ca46ec94cb88d0494b13bac12df0db6f50a244cd",
        "_iso_or_none": "1d82fbff6e4cdbbd9ce418c83701246c8055abbec8fc72b740eaf3b824ed841e",
        "_iso_or_now": "643c52902b836d5fd8aa42f24de978e1cf332dbc0085612327bd574df18b41b7",
        "_new_id": "f8455483b1d91d9ac29e33ba782072d1ad67bc8f637639a28e590e8d9fa6dca2",
        "_uuid_text": "cebf3f745b79e7b153639dfaa2a19b8482f853b01931e6ec9406ff829ee72df2",
        "_json_object_text": "e22a122ef28f096ae53d9f5f8e3a690c3fb51d71600009c3df3a01912d0f260c",
        "_json_list_text": "304e45a34507323c60589e56672ebd5fc684284c98152f9f0005c7176581e615",
    },
}
EXPECTED_VALUE_SHA256 = {
    "postgres_event_columns": "3c38107f9432b403d4d0f034f42742750153d64760a3ea50cca4d548b0b7fe7b",
    "postgres_event_types": "541c71f5d3fe824899897e746f0a4dd52105de23e9f5c50f1012944496be2423",
    "postgres_event_linkage": "3fe70a2c8ebb03ee3e0ccc27f7ee32d90d1804da66254a384bc3219f24e993ed",
    "postgres_event_lookup": "3114005cb22d417810ec56f02467a4ed56cac621cfbb437285c7c180f59aa687",
    "postgres_revision_columns": "fa0a48be48d763f49e34c4425a27b67b9a7ee328823e8825e16a4f49b91887c6",
    "sqlite_event_columns": "b2187114012af3bfa4f2b92674258b640b4728288735e21e7a9c7f52736a1f0e",
    "sqlite_event_types": "541c71f5d3fe824899897e746f0a4dd52105de23e9f5c50f1012944496be2423",
    "sqlite_event_linkage": "276055fb03ba91b6e32f8d867acb256fb9c2e9b2c0f96efe6638ee52c2444d7f",
    "sqlite_event_lookup": "c16eea6d82f5e428d9be1b0513624fcf22ef4f5cc9c349ed87edeed8831dbd3b",
    "sqlite_revision_columns": "70092cc2397ac0a194e6102def5eb44e058d1c2cd5164f5a48ed024d109db5b7",
}
EXPECTED_QUERY_SHA256 = {
    "postgres_append": "399c6a8fd3993597045302edd83507402e5e2cd8d57ae723935561a21ba9f6c0",
    "postgres_list_unbounded": "a8cde88eefc0842a2092a394c7f4a8e961bab1c4d85a482acfacb72dbda354bd",
    "postgres_list_limited": "030b249b851ee2b2d7ac2f188107990ebadbebbdbca355317bc8cde6847a0a47",
    "postgres_list_filtered": "da124cc9bcd37849a8775c014d0953a01d096a68b159cec4d20050efb0b82de7",
    "postgres_source_trace": "49e8d593d8c4614b2b666f5a35b037e38b3427f15c8a8e4c397a2fb51f7a545e",
    "postgres_project_update": "3114005cb22d417810ec56f02467a4ed56cac621cfbb437285c7c180f59aa687",
    "postgres_count_unfiltered": "f909bce6a56802a53c7b1ed5b3f6a6be53336d10b3e7a357b679154a807ae1de",
    "postgres_count_filtered": "f909bce6a56802a53c7b1ed5b3f6a6be53336d10b3e7a357b679154a807ae1de",
    "postgres_append_revision": "3f8c505941a01a5485536b1c0739f0ebd2523b6e5cfaf0696d33e5a90795f211",
    "postgres_list_revisions": "8bcc185a54e329bfee9a956db2ac9cc4f8b7c875c34c434b5bcccbe38f1f5a58",
    "sqlite_append": "d6f2d446542cc3858e5cb7d4b904f7a28dbffed13b0082a72c8fc553637120f6",
    "sqlite_list_unbounded": "b9ed711e7afdb44c8dec661659bb15ba66ffc6d9178f9d0f3da03779d063a94d",
    "sqlite_list_limited": "4d0d5b160f2ac09a4ad9a14b522ac234f365dc528ed5d8c5202033ee81d22109",
    "sqlite_list_filtered": "797ca3e1e4bbce7012c5f516a0a11556a71566035303a0beed6f72caf00046fb",
    "sqlite_source_trace_empty": "1de7a56afc085fceff89712097fc1bf8c210ad0ed0cc73969890e7d0c620e762",
    "sqlite_source_trace_populated": "73ab63cbd21bda17da310aa9b0104ceadd911adca5028f11ff546ba1940d7439",
    "sqlite_project_update": "c16eea6d82f5e428d9be1b0513624fcf22ef4f5cc9c349ed87edeed8831dbd3b",
    "sqlite_count_unfiltered": "b87d9672dcec41fe8530331194fd967ab6bbfd1674ef1b9f025ff5c753f99a8c",
    "sqlite_count_filtered": "5ee5201e58ab55c976534f014d1d31d6487b4d57f85eb3590c577caea513bdeb",
    "sqlite_append_revision": "b1921869d9d0fb2996d0576d6cbe93d4cdf88e8e94ee80a948bf6d52eb3ae8bd",
    "sqlite_list_revisions": "b02e9e5f729363ab3a702b5583e6a25f67a570fbcb26f6fa470d220ac0930976",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _digest(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def _value_digest(value: object) -> str:
    encoded = value.encode() if isinstance(value, str) else repr(value).encode()
    return hashlib.sha256(encoded).hexdigest()


def _named_nodes(tree: ast.Module) -> dict[str, list[ast.AST]]:
    result: dict[str, list[ast.AST]] = {}
    for node in tree.body:
        name: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name is not None:
            result.setdefault(name, []).append(node)
    return result


def _class_node(path: Path, class_name: str) -> ast.ClassDef:
    matches = [node for node in _tree(path).body if isinstance(node, ast.ClassDef) and node.name == class_name]
    assert len(matches) == 1
    return matches[0]


def _class_binding_names(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for child in node.body:
        if isinstance(child, ast.FunctionDef):
            names.append(child.name)
        elif isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
            names.append(child.targets[0].id)
    return names


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def _plain_param(value: object) -> object:
    if type(value).__module__.startswith("psycopg.types.json") and hasattr(value, "obj"):
        return (type(value).__name__, _plain_param(value.obj))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain_param(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_param(item) for item in value)
    if isinstance(value, dict):
        return {key: _plain_param(item) for key, item in value.items()}
    return value


class _Capture:
    def __init__(self) -> None:
        self.user_id = "user"
        self.queries: list[tuple[str, object]] = []
        self.mutation_events: list[dict[str, object]] = []

    def _fetch_one(self, _operation: str, query: str, params: object) -> dict[str, object]:
        self.queries.append((query, params))
        return {"id": "revision", "memory_id": "memory", "count": 0}

    def _fetch_all(self, query: str, params: object = ()) -> list[dict[str, object]]:
        self.queries.append((query, params))
        return []

    def _execute(self, query: str, params: object = ()) -> None:
        self.queries.append((query, params))

    def _get_row(self, *_args: object) -> dict[str, object]:
        return {"id": "revision", "memory_id": "memory"}

    def _append_mutation_event(self, **event: object) -> None:
        self.mutation_events.append(event)

    def _placeholders(self, values: list[str]) -> str:
        return ", ".join("?" for _value in values)


def _capture_call(store_class: type[Any], method_name: str, **kwargs: object) -> _Capture:
    capture = _Capture()
    getattr(store_class, method_name)(capture, **kwargs)
    return capture


def test_event_revision_methods_are_moved_once_at_the_original_class_positions() -> None:
    postgres_class = _class_node(POSTGRES_FACADE_PATH, "PostgresVNextStore")
    sqlite_class = _class_node(SQLITE_FACADE_PATH, "SQLiteVNextStore")
    for class_node in (postgres_class, sqlite_class):
        assert {
            child.name
            for child in class_node.body
            if isinstance(child, ast.FunctionDef)
        }.isdisjoint(METHOD_NAMES)
        assignments = [
            child.targets[0].id
            for child in class_node.body
            if isinstance(child, ast.Assign)
            and len(child.targets) == 1
            and isinstance(child.targets[0], ast.Name)
            and child.targets[0].id in METHOD_NAMES
        ]
        assert assignments == list(METHOD_NAMES)

    postgres_names = _class_binding_names(postgres_class)
    sqlite_names = _class_binding_names(sqlite_class)
    assert postgres_names[postgres_names.index("_fetch_all") + 1 : postgres_names.index("list_resume_memory_events")] == [
        "_append_mutation_event",
        "append_event",
        "list_events",
        "list_events_for_source_trace",
    ]
    assert postgres_names[
        postgres_names.index("list_resume_memory_events") + 1 : postgres_names.index("list_memory_events")
    ] == ["list_project_update_events"]
    assert postgres_names[postgres_names.index("list_memory_events") + 1 : postgres_names.index("count_sources")] == [
        "count_events"
    ]
    assert postgres_names[postgres_names.index("update_memory") + 1 : postgres_names.index("_redaction_mode")] == [
        "append_revision",
        "list_revisions",
    ]
    assert sqlite_names[sqlite_names.index("_like_any") + 1 : sqlite_names.index("list_resume_memory_events")] == [
        "_append_mutation_event",
        "append_event",
        "list_events",
        "list_events_for_source_trace",
    ]
    assert sqlite_names[
        sqlite_names.index("list_resume_memory_events") + 1 : sqlite_names.index("list_memory_events")
    ] == ["list_project_update_events"]
    assert sqlite_names[sqlite_names.index("list_memory_events") + 1 : sqlite_names.index("create_source")] == [
        "count_events", "markdown_sources_by_path", "supersede_source",
        "scrub_source", "source_inventory", "prunable_sources"
    ]
    assert sqlite_names[
        sqlite_names.index("list_memories_missing_fact_keys") + 1 : sqlite_names.index("_redaction_mode")
    ] == ["append_revision", "list_revisions"]


def test_event_revision_carriers_preserve_ast_comments_and_readable_source() -> None:
    for backend, path in (("postgres", POSTGRES_EVENTS_PATH), ("sqlite", SQLITE_EVENTS_PATH)):
        source = path.read_text(encoding="utf-8")
        definitions = _named_nodes(ast.parse(source))
        observed_line_counts: list[int] = []
        for method_name in METHOD_NAMES:
            nodes = definitions.get(method_name, [])
            assert len(nodes) == 1
            node = nodes[0]
            assert isinstance(node, ast.FunctionDef)
            assert _digest(node) == EXPECTED_METHOD_AST_SHA256[backend][method_name]
            assert node.end_lineno is not None
            observed_line_counts.append(node.end_lineno - node.lineno + 1)
        assert tuple(observed_line_counts) == EXPECTED_SOURCE_LINE_COUNTS[backend]

    postgres_source = POSTGRES_EVENTS_PATH.read_text(encoding="utf-8")
    sqlite_source = SQLITE_EVENTS_PATH.read_text(encoding="utf-8")
    assert "# UNION (rather than one five-way OR or UNION ALL) gives each linkage" in postgres_source
    assert "# arm its own indexable scan while collapsing an event that carries" in postgres_source
    assert "# UNION makes every linkage arm independently indexable. It also" in sqlite_source
    assert "# Allocate both counters inside the INSERT statement. SQLite" in sqlite_source
    assert "# avoiding the former read-MAX / later-INSERT race across connections." in sqlite_source
    assert "# timespec pins the fractional part: isoformat() omits it entirely" in SQLITE_PRIMITIVES_PATH.read_text(
        encoding="utf-8"
    )


def test_event_revision_grafts_preserve_runtime_identity_signatures_and_class_shape() -> None:
    runtime_specs = (
        ("postgres", postgres_store.PostgresVNextStore, postgres_events, "alicebot_api.vnext_store"),
        ("sqlite", sqlite_store.SQLiteVNextStore, sqlite_events, "alicebot_api.sqlite_store"),
    )
    for backend, facade_class, carrier, module_name in runtime_specs:
        assert facade_class.__bases__ == (object,)
        assert facade_class.__mro__ == (facade_class, object)
        assert hashlib.sha256("\n".join(facade_class.__dict__).encode()).hexdigest() == EXPECTED_CLASS_KEY_SHA256[
            backend
        ]
        for method_name in METHOD_NAMES:
            method = facade_class.__dict__[method_name]
            assert method is getattr(carrier, method_name)
            assert str(inspect.signature(method)) == EXPECTED_SIGNATURES[method_name]
            assert method.__module__ == module_name
            assert method.__qualname__ == f"{facade_class.__name__}.{method_name}"
            assert method.__doc__ == EXPECTED_METHOD_DOCS[backend][method_name]


def test_event_revision_support_nodes_values_and_facade_reexports_are_exact() -> None:
    trees = {
        "postgres_columns": _tree(POSTGRES_COLUMNS_PATH),
        "sqlite_columns": _tree(SQLITE_COLUMNS_PATH),
        "postgres_events": _tree(POSTGRES_EVENTS_PATH),
        "sqlite_events": _tree(SQLITE_EVENTS_PATH),
        "postgres_primitives": _tree(POSTGRES_PRIMITIVES_PATH),
        "sqlite_primitives": _tree(SQLITE_PRIMITIVES_PATH),
    }
    for module_name, expected_nodes in EXPECTED_SUPPORT_AST_SHA256.items():
        definitions = _named_nodes(trees[module_name])
        for name, expected_digest in expected_nodes.items():
            assert len(definitions.get(name, [])) == 1
            assert _digest(definitions[name][0]) == expected_digest

    observed_values = {
        "postgres_event_columns": postgres_columns.EVENT_LOG_COLUMNS,
        "postgres_event_types": postgres_events._PROJECT_UPDATE_EVENT_TYPES_SQL,
        "postgres_event_linkage": postgres_events._PROJECT_UPDATE_EVENT_LINKAGE_SQL,
        "postgres_event_lookup": postgres_events._PROJECT_UPDATE_EVENT_LOOKUP_SQL,
        "postgres_revision_columns": postgres_columns.REVISION_COLUMNS,
        "sqlite_event_columns": sqlite_columns.EVENT_LOG_COLUMNS,
        "sqlite_event_types": sqlite_events._PROJECT_UPDATE_EVENT_TYPES_SQL,
        "sqlite_event_linkage": sqlite_events._PROJECT_UPDATE_EVENT_LINKAGE_SQL,
        "sqlite_event_lookup": sqlite_events._PROJECT_UPDATE_EVENT_LOOKUP_SQL,
        "sqlite_revision_columns": sqlite_columns.REVISION_COLUMNS,
    }
    assert {name: _value_digest(value) for name, value in observed_values.items()} == EXPECTED_VALUE_SHA256

    assert postgres_store.EVENT_LOG_COLUMNS is postgres_columns.EVENT_LOG_COLUMNS
    assert postgres_store.REVISION_COLUMNS is postgres_columns.REVISION_COLUMNS
    assert sqlite_store.EVENT_LOG_COLUMNS is sqlite_columns.EVENT_LOG_COLUMNS
    assert sqlite_store.REVISION_COLUMNS is sqlite_columns.REVISION_COLUMNS
    assert postgres_events.EVENT_LOG_COLUMNS is postgres_columns.EVENT_LOG_COLUMNS
    assert postgres_events.REVISION_COLUMNS is postgres_columns.REVISION_COLUMNS
    assert sqlite_events.EVENT_LOG_COLUMNS is sqlite_columns.EVENT_LOG_COLUMNS
    assert sqlite_events.REVISION_COLUMNS is sqlite_columns.REVISION_COLUMNS
    for name in (
        "_PROJECT_UPDATE_EVENT_TYPES_SQL",
        "_PROJECT_UPDATE_EVENT_LINKAGE_SQL",
        "_PROJECT_UPDATE_EVENT_LOOKUP_SQL",
    ):
        assert getattr(postgres_store, name) is getattr(postgres_events, name)
        assert getattr(sqlite_store, name) is getattr(sqlite_events, name)
    for name in ("_json_object", "_json_list", "_json_safe"):
        assert getattr(postgres_store, name) is getattr(postgres_primitives, name)
        assert getattr(postgres_events, name) is getattr(postgres_primitives, name)
        primitive = getattr(postgres_primitives, name)
        signature, doc = EXPECTED_PRIMITIVE_METADATA["postgres"][name]
        assert str(inspect.signature(primitive)) == signature
        assert primitive.__doc__ == doc
        assert primitive.__module__ == "alicebot_api.vnext_store"
        assert primitive.__qualname__ == name
    for name in (
        "_utc_now_iso",
        "_iso_or_none",
        "_iso_or_now",
        "_new_id",
        "_uuid_text",
        "_json_object_text",
        "_json_list_text",
    ):
        assert getattr(sqlite_store, name) is getattr(sqlite_primitives, name)
        assert getattr(sqlite_events, name) is getattr(sqlite_primitives, name)
        primitive = getattr(sqlite_primitives, name)
        signature, doc = EXPECTED_PRIMITIVE_METADATA["sqlite"][name]
        assert str(inspect.signature(primitive)) == signature
        assert primitive.__doc__ == doc
        assert primitive.__module__ == "alicebot_api.sqlite_store"
        assert primitive.__qualname__ == name
    assert postgres_store.__all__ == ["PostgresVNextStore", "VNextRow"]
    assert sqlite_store.__all__ == [
        "REDACTION_MARKER",
        "SQLiteVNextStore",
        "ensure_sqlite_user",
        "sqlite_user_connection",
    ]


def test_event_revision_modules_do_not_import_facades_and_import_in_either_order() -> None:
    for path in (
        POSTGRES_EVENTS_PATH,
        SQLITE_EVENTS_PATH,
        POSTGRES_COLUMNS_PATH,
        SQLITE_COLUMNS_PATH,
        POSTGRES_PRIMITIVES_PATH,
        SQLITE_PRIMITIVES_PATH,
    ):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
                if node.module == "alicebot_api":
                    assert all(alias.name not in {"vnext_store", "sqlite_store"} for alias in node.names)
            elif isinstance(node, ast.Import):
                assert all(
                    alias.name not in {"alicebot_api.vnext_store", "alicebot_api.sqlite_store"}
                    for alias in node.names
                )

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for statement in (
        "import alicebot_api.vnext_store; import alicebot_api.sqlite_store",
        "import alicebot_api.sqlite_store; import alicebot_api.vnext_store",
        (
            "import sys; "
            "import alicebot_api.vnext_stores.postgres.columns; "
            "import alicebot_api.vnext_stores.postgres.primitives; "
            "import alicebot_api.vnext_stores.postgres.events_revisions; "
            "assert 'alicebot_api.vnext_store' not in sys.modules; "
            "assert 'alicebot_api.sqlite_store' not in sys.modules"
        ),
        (
            "import sys; "
            "import alicebot_api.vnext_stores.sqlite.columns; "
            "import alicebot_api.vnext_stores.sqlite.primitives; "
            "import alicebot_api.vnext_stores.sqlite.events_revisions; "
            "assert 'alicebot_api.vnext_store' not in sys.modules; "
            "assert 'alicebot_api.sqlite_store' not in sys.modules"
        ),
    ):
        completed = subprocess.run(
            [sys.executable, "-c", statement],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_event_queries_and_parameters_remain_byte_identical() -> None:
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    calls: dict[str, _Capture] = {
        "postgres_append": _capture_call(
            postgres_store.PostgresVNextStore,
            "append_event",
            event={"id": "event", "event_type": "created", "actor_type": "system", "occurred_at": stamp},
        ),
        "postgres_list_unbounded": _capture_call(postgres_store.PostgresVNextStore, "list_events"),
        "postgres_list_limited": _capture_call(postgres_store.PostgresVNextStore, "list_events", limit=2),
        "postgres_list_filtered": _capture_call(
            postgres_store.PostgresVNextStore,
            "list_events",
            target_type="memory",
            target_id="m",
            occurred_at_start=stamp,
            occurred_at_end=stamp,
            limit=2,
        ),
        "postgres_source_trace": _capture_call(
            postgres_store.PostgresVNextStore,
            "list_events_for_source_trace",
            source_id="source",
            memory_ids=("m", "m", ""),
            artifact_ids=("a",),
            open_loop_ids=("o",),
            limit=7,
        ),
        "postgres_project_update": _capture_call(
            postgres_store.PostgresVNextStore,
            "list_project_update_events",
            artifact_id="a",
            candidate_memory_id="m",
        ),
        "postgres_count_unfiltered": _capture_call(postgres_store.PostgresVNextStore, "count_events"),
        "postgres_count_filtered": _capture_call(
            postgres_store.PostgresVNextStore, "count_events", target_type="memory", target_id="m"
        ),
        "postgres_append_revision": _capture_call(
            postgres_store.PostgresVNextStore,
            "append_revision",
            revision={"memory_id": "memory", "memory_key": "key"},
        ),
        "postgres_list_revisions": _capture_call(
            postgres_store.PostgresVNextStore, "list_revisions", memory_id="memory"
        ),
        "sqlite_append": _capture_call(
            sqlite_store.SQLiteVNextStore,
            "append_event",
            event={"id": "event", "event_type": "created", "actor_type": "system", "occurred_at": stamp},
        ),
        "sqlite_list_unbounded": _capture_call(sqlite_store.SQLiteVNextStore, "list_events"),
        "sqlite_list_limited": _capture_call(sqlite_store.SQLiteVNextStore, "list_events", limit=2),
        "sqlite_list_filtered": _capture_call(
            sqlite_store.SQLiteVNextStore,
            "list_events",
            target_type="memory",
            target_id="m",
            occurred_at_start=stamp,
            occurred_at_end=stamp,
            limit=2,
        ),
        "sqlite_source_trace_empty": _capture_call(
            sqlite_store.SQLiteVNextStore, "list_events_for_source_trace", source_id="source", limit=7
        ),
        "sqlite_source_trace_populated": _capture_call(
            sqlite_store.SQLiteVNextStore,
            "list_events_for_source_trace",
            source_id="source",
            memory_ids=("m", "m", ""),
            artifact_ids=("a",),
            open_loop_ids=("o",),
            limit=7,
        ),
        "sqlite_project_update": _capture_call(
            sqlite_store.SQLiteVNextStore,
            "list_project_update_events",
            artifact_id="a",
            candidate_memory_id="m",
        ),
        "sqlite_count_unfiltered": _capture_call(sqlite_store.SQLiteVNextStore, "count_events"),
        "sqlite_count_filtered": _capture_call(
            sqlite_store.SQLiteVNextStore, "count_events", target_type="memory", target_id="m"
        ),
        "sqlite_list_revisions": _capture_call(
            sqlite_store.SQLiteVNextStore, "list_revisions", memory_id="memory"
        ),
    }
    observed = {name: _query_digest(capture.queries[0][0]) for name, capture in calls.items()}
    assert observed == {name: digest for name, digest in EXPECTED_QUERY_SHA256.items() if name in calls}

    stamp_text = "2026-01-02T03:04:05+00:00"
    expected_params = {
        "postgres_append": (
            "event",
            "created",
            "system",
            None,
            None,
            None,
            stamp_text,
            ("Jsonb", {}),
            None,
            None,
            None,
        ),
        "postgres_list_unbounded": (),
        "postgres_list_limited": (2,),
        "postgres_list_filtered": ("memory", "memory", "m", "m", stamp_text, stamp_text, 2),
        "postgres_source_trace": (
            "source",
            ["m"],
            ["m"],
            ["a"],
            ["a"],
            ["o"],
            ["o"],
            "source",
            "source",
            "source:source",
            "source",
            "source",
            "source:source",
            "source",
            "source:source",
            "source",
            7,
        ),
        "postgres_project_update": ("a", "m", "a", "m", "m"),
        "postgres_count_unfiltered": (None, None, None, None),
        "postgres_count_filtered": ("memory", "memory", "m", "m"),
        "postgres_append_revision": (
            "memory",
            None,
            "memory",
            None,
            "UPDATE",
            "key",
            None,
            ("Jsonb", {}),
            ("Jsonb", []),
            ("Jsonb", {}),
            None,
            "edited",
            None,
            "",
            None,
            "system",
            None,
            ("Jsonb", {}),
        ),
        "postgres_list_revisions": ("memory",),
        "sqlite_append": (
            "event",
            "user",
            "created",
            "system",
            None,
            None,
            None,
            "2026-01-02T03:04:05Z",
            "{}",
            None,
            None,
            None,
        ),
        "sqlite_list_unbounded": ("user",),
        "sqlite_list_limited": ("user", 2),
        "sqlite_list_filtered": (
            "user",
            "memory",
            "m",
            "2026-01-02T03:04:05Z",
            "2026-01-02T03:04:05Z",
            2,
        ),
        "sqlite_source_trace_empty": ("user", "source", "source", "source:source", 7),
        "sqlite_source_trace_populated": (
            "user",
            "source",
            "m",
            "a",
            "o",
            "source",
            "source:source",
            7,
        ),
        "sqlite_project_update": (
            "user",
            "a",
            "user",
            "m",
            "user",
            "a",
            "user",
            "m",
            "user",
            "m",
        ),
        "sqlite_count_unfiltered": ("user",),
        "sqlite_count_filtered": ("user", "memory", "m"),
        "sqlite_list_revisions": ("memory", "user"),
    }
    assert {name: _plain_param(capture.queries[0][1]) for name, capture in calls.items()} == expected_params
    postgres_calls = {name: capture for name, capture in calls.items() if name.startswith("postgres_")}
    sqlite_calls = {name: capture for name, capture in calls.items() if name.startswith("sqlite_")}
    assert all("user" not in repr(_plain_param(capture.queries[0][1])) for capture in postgres_calls.values())
    assert all("user_id = %s" not in capture.queries[0][0] for capture in postgres_calls.values())
    assert "app.current_user_id()" in calls["postgres_append"].queries[0][0]
    assert "app.current_user_id()" in calls["postgres_project_update"].queries[0][0]
    assert "app.current_user_id()" in calls["postgres_append_revision"].queries[0][0]
    assert all("user_id" in capture.queries[0][0] for capture in sqlite_calls.values())
    assert all("user" in repr(_plain_param(capture.queries[0][1])) for capture in sqlite_calls.values())

    assert calls["postgres_source_trace"].queries[0][1] == (
        "source",
        ["m"],
        ["m"],
        ["a"],
        ["a"],
        ["o"],
        ["o"],
        "source",
        "source",
        "source:source",
        "source",
        "source",
        "source:source",
        "source",
        "source:source",
        "source",
        7,
    )
    assert calls["sqlite_source_trace_populated"].queries[0][1] == (
        "user",
        "source",
        "m",
        "a",
        "o",
        "source",
        "source:source",
        7,
    )
    assert calls["postgres_project_update"].queries[0][1] == ("a", "m", "a", "m", "m")
    assert calls["sqlite_project_update"].queries[0][1] == (
        "user",
        "a",
        "user",
        "m",
        "user",
        "a",
        "user",
        "m",
        "user",
        "m",
    )
    assert calls["postgres_append_revision"].mutation_events == [
        {
            "event_type": "memory_revision.created",
            "actor_type": "system",
            "target_type": "memory",
            "target_id": "memory",
            "payload": {"operation": "create_revision", "revision_id": "revision"},
        }
    ]


def test_sqlite_revision_query_clock_and_mutation_event_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_events, "_utc_now_iso", lambda: "2026-01-02T03:04:05.000000Z")
    capture = _capture_call(
        sqlite_store.SQLiteVNextStore,
        "append_revision",
        revision={"id": "revision", "memory_id": "memory", "memory_key": "key"},
    )
    query, params = capture.queries[0]
    assert _query_digest(query) == EXPECTED_QUERY_SHA256["sqlite_append_revision"]
    assert params == (
        "revision",
        "user",
        "memory",
        None,
        "UPDATE",
        "key",
        None,
        "{}",
        "[]",
        "{}",
        None,
        "edited",
        None,
        "",
        None,
        "system",
        None,
        "{}",
        "2026-01-02T03:04:05.000000Z",
        "memory",
        "user",
    )
    assert "user_id = ?" in query
    assert capture.mutation_events == [
        {
            "event_type": "memory_revision.created",
            "actor_type": "system",
            "target_type": "memory",
            "target_id": "memory",
            "payload": {"operation": "create_revision", "revision_id": "revision"},
        }
    ]


@pytest.mark.parametrize("store_class", [postgres_store.PostgresVNextStore, sqlite_store.SQLiteVNextStore])
def test_event_query_limits_still_fail_before_sql(store_class: type[Any]) -> None:
    capture = _Capture()
    with pytest.raises(ValueError, match="limit must be positive"):
        store_class.list_events(capture, limit=0)
    with pytest.raises(ValueError, match="limit must be positive"):
        store_class.list_events_for_source_trace(capture, source_id="source", limit=0)
    assert capture.queries == []
