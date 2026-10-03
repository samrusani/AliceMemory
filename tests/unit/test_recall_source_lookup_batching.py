"""Recall and the pack read the memories that reference their sources once.

v0.19.0 added a label for a packed excerpt whose derived memory was later
corrected. It asked the store once per packed source, and each ask parses the
JSON of every memory row, so latency grew with sources packed times memories
stored. The label now reads one batched lookup per request.

Each test names the mutation that must fail it.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.source_ranking import SourceRanking
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_retrieval import (
    VNextRetrievalRequest,
    VNextRetrievalService,
    annotate_derived_memory_corrections,
)
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_source_fence import SourceReadFence

USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
LOOKUP_SIGNATURE = "json_tree(m.metadata_json"


# --------------------------------------------------------------------------
# SQLite store: the batched lookup returns what the one-source lookup returns
# --------------------------------------------------------------------------


def _database(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _source(store: SQLiteVNextStore, name: str) -> str:
    row = store.create_source(
        {
            "source_type": "note",
            "title": name,
            "content_hash": f"hash-{name}",
            "captured_at": "2026-08-01T08:00:00Z",
            "domain": "project",
            "sensitivity": "public",
            "metadata_json": {},
        }
    )
    return str(row["id"])


def _memory(
    store: SQLiteVNextStore,
    key: str,
    *,
    source_event_ids: list[str] | None = None,
    metadata: dict[str, object] | None = None,
    status: str = "active",
) -> str:
    row = store.create_memory(
        {
            "memory_key": key,
            "memory_type": "decision",
            "title": key,
            "canonical_text": f"Text of {key}.",
            "status": status,
            "domain": "project",
            "sensitivity": "public",
            "source_event_ids": source_event_ids or [],
            "metadata_json": metadata or {},
            "value": {"text": key},
        }
    )
    return str(row["id"])


def _link(store: SQLiteVNextStore, memory_id: str, source_id: str, role: str = "supports") -> None:
    store.create_provenance_link(
        {
            "target_type": "memory",
            "target_id": memory_id,
            "source_id": source_id,
            "evidence_role": role,
            "confidence": 0.9,
        }
    )


def _stamp(connection: sqlite3.Connection, memory_id: str, *, updated_at: str, created_at: str) -> None:
    connection.execute(
        "UPDATE memories SET updated_at = ?, created_at = ? WHERE id = ?",
        (updated_at, created_at, memory_id),
    )


def _seed_every_reference_kind(connection: sqlite3.Connection) -> dict[str, str]:
    """Sources A to D and memories that reference them in every way the store reads."""

    store = SQLiteVNextStore(connection, USER_ID)
    a, b, c, d = (_source(store, name) for name in ("a", "b", "c", "d"))
    ids: dict[str, str] = {"a": a, "b": b, "c": c, "d": d}

    ids["by_link"] = _memory(store, "by-link")
    _link(store, ids["by_link"], a)
    ids["by_event"] = _memory(store, "by-event", source_event_ids=[a])
    ids["by_meta_id"] = _memory(store, "by-meta-id", metadata={"source_id": a})
    ids["by_meta_ref"] = _memory(store, "by-meta-ref", metadata={"source_ref": f"source:{a}"})
    ids["by_meta_ref_bare"] = _memory(store, "by-meta-ref-bare", metadata={"source_ref": a})
    ids["by_meta_nested"] = _memory(store, "by-meta-nested", metadata={"extra": {"source_id": a}})
    ids["by_meta_refs_scalar"] = _memory(store, "by-meta-refs-scalar", metadata={"source_refs": f"source:{a}"})
    ids["by_meta_selected"] = _memory(store, "by-meta-selected", metadata={"selected_source_ids": a})
    # The SQLite lookup reads a scalar under these keys; a list is not matched.
    # The batched lookup must keep that, not "fix" it.
    ids["by_meta_list"] = _memory(store, "by-meta-list", metadata={"source_ids": [a]})
    ids["two_sources"] = _memory(store, "two-sources")
    _link(store, ids["two_sources"], a)
    _link(store, ids["two_sources"], b)
    ids["every_way"] = _memory(
        store,
        "every-way",
        source_event_ids=[a],
        metadata={"source_id": a, "source_ref": f"source:{a}"},
    )
    _link(store, ids["every_way"], a)
    ids["deleted"] = _memory(store, "deleted")
    _link(store, ids["deleted"], a)
    connection.execute("UPDATE memories SET deleted_at = '2026-08-02T00:00:00Z' WHERE id = ?", (ids["deleted"],))
    ids["superseded"] = _memory(store, "superseded", status="superseded")
    _link(store, ids["superseded"], a, "quoted_from")
    ids["unrelated"] = _memory(store, "unrelated")

    ensure_sqlite_user(connection, OTHER_USER_ID, "other@alice", "Other")
    other = SQLiteVNextStore(connection, OTHER_USER_ID)
    ids["other_user"] = _memory(other, "other-user", metadata={"source_id": a}, source_event_ids=[a])
    # This user's own link, naming a memory that belongs to the other user. Only
    # the user test on the memories join keeps it out.
    ids["other_user_linked"] = _memory(other, "other-user-linked")
    _link(store, ids["other_user_linked"], a)

    # Seven references to C with tied timestamps, so the cap and the tie-break both matter.
    for index in range(7):
        memory_id = _memory(store, f"capped-{index}")
        _link(store, memory_id, c)
        ids[f"capped_{index}"] = memory_id
        _stamp(
            connection,
            memory_id,
            updated_at="2026-08-03T00:00:00Z",
            created_at="2026-08-03T00:00:00Z" if index % 2 else "2026-08-03T00:00:01Z",
        )
    return ids


def test_batched_lookup_returns_what_the_one_source_lookup_returns(tmp_path: Path) -> None:
    """Whole rows, same order, same cap, for every reference kind, every source.

    Mutations, each of which must fail this test: replace ``AND m.deleted_at IS
    NULL`` in the ``ranked`` join with a true condition (the deleted memory comes
    back); do the same to ``AND m.user_id = ?`` in that join (the other user's
    memory, named by this user's link, comes back); make the ``substr(...) =
    'source:'`` test false (the ``source:`` forms disappear); change the cap from
    ``<=`` to ``<``; change the window's ``m.updated_at DESC`` to ``ASC`` or its
    ``m.id DESC`` to ``ASC``.
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        ids = _seed_every_reference_kind(connection)
        store = SQLiteVNextStore(connection, USER_ID)
        asked = [ids["a"], ids["b"], ids["c"], ids["d"], "no-such-source"]
        for cap in (50, 3):
            batched = store.list_memories_referencing_sources(asked, limit_per_source=cap)
            assert list(batched) == asked
            for source_id in asked:
                assert batched[source_id] == store.list_memories_referencing_source(
                    source_id=source_id, limit=cap
                ), (source_id, cap)

        wide = store.list_memories_referencing_sources(asked, limit_per_source=50)
        found_for_a = {row["id"] for row in wide[ids["a"]]}
        # The control: the fixture really exercises every kind the store reads.
        assert found_for_a == {
            ids["by_link"],
            ids["by_event"],
            ids["by_meta_id"],
            ids["by_meta_ref"],
            ids["by_meta_ref_bare"],
            ids["by_meta_nested"],
            ids["by_meta_refs_scalar"],
            ids["by_meta_selected"],
            ids["two_sources"],
            ids["every_way"],
            ids["superseded"],
        }
        assert ids["by_meta_list"] not in found_for_a
        assert ids["deleted"] not in found_for_a
        assert ids["other_user"] not in found_for_a
        assert ids["other_user_linked"] not in found_for_a
        assert [row["id"] for row in wide[ids["b"]]] == [ids["two_sources"]]
        assert wide[ids["d"]] == [] and wide["no-such-source"] == []
        capped = store.list_memories_referencing_sources([ids["c"]], limit_per_source=3)[ids["c"]]
        assert len(capped) == 3


def test_batched_lookup_edges(tmp_path: Path) -> None:
    """Empty input, duplicates, odd ids and a bad cap.

    Mutations: hand the statement the ids as something other than a JSON array
    (``json_each`` raises); drop the ``if not ids`` return (an empty ask runs the
    statement); check ``limit_per_source`` after the empty-ask return (a bad cap
    with no ids stops raising).
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        a = _source(store, "a")
        memory_id = _memory(store, "m")
        _link(store, memory_id, a)
        statements: list[str] = []
        connection.set_trace_callback(statements.append)

        assert store.list_memories_referencing_sources([], limit_per_source=5) == {}
        assert store.list_memories_referencing_sources(["", None], limit_per_source=5) == {}  # type: ignore[list-item]
        assert statements == [], "an empty ask must not touch the database"

        result = store.list_memories_referencing_sources([a, a, "x'y\"z", a], limit_per_source=5)
        assert list(result) == [a, "x'y\"z"]
        assert [row["id"] for row in result[a]] == [memory_id]
        assert result["x'y\"z"] == []

        for bad in (0, -1):
            with pytest.raises(ValueError):
                store.list_memories_referencing_sources([], limit_per_source=bad)
            with pytest.raises(ValueError):
                store.list_memories_referencing_sources([a], limit_per_source=bad)


def test_batched_lookup_ignores_provenance_links_to_other_target_types(tmp_path: Path) -> None:
    """A provenance link to an entity or open loop is not a reference to a memory.

    The one-source lookup reads only links whose ``target_type`` is ``memory``.
    A link to another kind of object can carry an id that is also a memory's id;
    neither lookup may return that memory for the link's source.

    Mutation: drop ``p.target_type = 'memory'`` from the provenance branch of the
    batched SQLite statement (the memory comes back for source ``a``).
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        a = _source(store, "a")
        b = _source(store, "b")
        linked = _memory(store, "linked")
        _link(store, linked, a)
        unrelated = _memory(store, "unrelated")
        for target_type in ("entity", "open_loop", "source"):
            store.create_provenance_link(
                {
                    "target_type": target_type,
                    "target_id": unrelated,
                    "source_id": a,
                    "evidence_role": "supports",
                    "confidence": 0.9,
                }
            )

        one = [row["id"] for row in store.list_memories_referencing_source(source_id=a, limit=50)]
        batched = store.list_memories_referencing_sources([a, b], limit_per_source=50)

        assert one == [linked]
        assert [row["id"] for row in batched[a]] == one
        assert batched[b] == []


def test_batched_lookup_is_one_statement_however_many_sources(tmp_path: Path) -> None:
    """One SELECT for 40 sources, not 40.

    Mutation: make ``list_memories_referencing_sources`` loop and call
    ``list_memories_referencing_source`` per id.
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        source_ids = [_source(store, f"s{index}") for index in range(40)]
        for index in range(0, 40, 3):
            memory_id = _memory(store, f"m{index}")
            _link(store, memory_id, source_ids[index])
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        result = store.list_memories_referencing_sources(source_ids, limit_per_source=50)
        assert len(statements) == 1, statements
        assert sum(1 for rows in result.values() if rows) == len(range(0, 40, 3))


def test_the_store_methods_state_their_cap() -> None:
    """``limit_per_source`` has no default: the callers disagree about the cap.

    Mutation: give ``limit_per_source`` a default, on either store.
    """

    for store_class in (SQLiteVNextStore, PostgresVNextStore):
        parameter = inspect.signature(store_class.list_memories_referencing_sources).parameters["limit_per_source"]
        assert parameter.default is inspect.Parameter.empty, store_class.__name__
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, store_class.__name__


# --------------------------------------------------------------------------
# Postgres store: the statement is the one-source query, once per id
# --------------------------------------------------------------------------


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        if params is not None:
            assert query.count("%s") == len(params)
        self.executed.append((query, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


# The ten tests the one-source Postgres query ORs together. The batched
# statement must carry each one, with the id and the "source:" form where the
# one-source query has them.
_ONE_SOURCE_PREDICATES = (
    "m.source_event_ids ? %s",
    "p.target_id = m.id::text",
    "p.source_id = %s::uuid",
    "m.metadata_json ->> 'source_id' = %s",
    "m.metadata_json ->> 'source_ref' IN (%s, %s)",
    "m.metadata_json -> 'source_ids' ? %s",
    "m.metadata_json -> 'source_refs' ? %s",
    "m.metadata_json -> 'source_references' ? %s",
    "m.metadata_json -> 'selected_source_ids' ? %s",
)
_BATCHED_PREDICATES = (
    "m.source_event_ids ? w.source_id",
    "p.target_id = m.id::text",
    "p.source_id = w.source_id::uuid",
    "m.metadata_json ->> 'source_id' = w.source_id",
    "m.metadata_json ->> 'source_ref' IN (w.source_id, w.source_ref)",
    "m.metadata_json -> 'source_ids' ? w.source_id",
    "m.metadata_json -> 'source_refs' ? w.source_id",
    "m.metadata_json -> 'source_refs' ? w.source_ref",
    "m.metadata_json -> 'source_references' ? w.source_id",
    "m.metadata_json -> 'source_references' ? w.source_ref",
    "m.metadata_json -> 'selected_source_ids' ? w.source_id",
)


def test_postgres_statement_carries_every_predicate_of_the_one_source_query() -> None:
    """The batched query keeps the one-source query's controls and per-source cap.

    A unit test of the statement text, because this environment has no
    Postgres; the integration test next to it runs the statement against one in CI.

    Mutations: drop ``m.deleted_at IS NULL``; drop any one OR branch; move
    ``LIMIT`` out of the ``LATERAL`` subquery (the cap would be over all sources);
    drop the ``ORDER BY`` inside it (the cap would keep arbitrary rows).
    """

    cursor = _Cursor([])
    store = PostgresVNextStore(_Connection(cursor))  # type: ignore[arg-type]
    store.list_memories_referencing_sources(["s-1"], limit_per_source=7)
    query, params = cursor.executed[0]

    one_source = _Cursor([])
    PostgresVNextStore(_Connection(one_source)).list_memories_referencing_source(source_id="s-1", limit=7)  # type: ignore[arg-type]
    one_query = one_source.executed[0][0]
    for fragment in _ONE_SOURCE_PREDICATES:
        assert fragment in one_query, f"the one-source query changed: {fragment}"
    for fragment in _BATCHED_PREDICATES:
        assert fragment in query, fragment

    assert "m.deleted_at IS NULL" in query
    assert "CROSS JOIN LATERAL" in query
    assert query.index("CROSS JOIN LATERAL") < query.index("ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC")
    assert query.index("ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC") < query.index("LIMIT %s")
    assert query.index("LIMIT %s") < query.index(") AS hit")
    assert params == (["s-1"], ["source:s-1"], 7)


def test_postgres_groups_rows_by_source_and_keeps_every_asked_key() -> None:
    """Rows come back under their source in the order asked; unreferenced sources get [].

    Mutations: leave ``ref_source_id`` in each row; build the result from the rows
    only (an unreferenced source loses its key); skip the dedupe.
    """

    rows = [
        {"ref_source_id": "s-2", "id": "m-1"},
        {"ref_source_id": "s-2", "id": "m-2"},
        {"ref_source_id": "s-1", "id": "m-3"},
    ]
    cursor = _Cursor(rows)
    store = PostgresVNextStore(_Connection(cursor))  # type: ignore[arg-type]
    result = store.list_memories_referencing_sources(["s-1", "s-2", "s-3", "s-1"], limit_per_source=5)
    assert list(result) == ["s-1", "s-2", "s-3"]
    assert result == {"s-1": [{"id": "m-3"}], "s-2": [{"id": "m-1"}, {"id": "m-2"}], "s-3": []}
    assert cursor.executed[0][1] == (["s-1", "s-2", "s-3"], ["source:s-1", "source:s-2", "source:s-3"], 5)


def test_postgres_empty_ask_and_bad_cap_never_reach_the_database() -> None:
    """Mutation: run the statement for an empty id list, or check the cap after it."""

    cursor = _Cursor([])
    store = PostgresVNextStore(_Connection(cursor))  # type: ignore[arg-type]
    assert store.list_memories_referencing_sources([], limit_per_source=5) == {}
    with pytest.raises(ValueError):
        store.list_memories_referencing_sources(["s-1"], limit_per_source=0)
    assert cursor.executed == []


# --------------------------------------------------------------------------
# The correction label: one lookup per pack, and the same labels
# --------------------------------------------------------------------------


class _RecordingStore:
    """A store double with every lookup the label reads, recording each call."""

    def __init__(self, rows_by_source: dict[str, list[dict[str, object]]], links: list[dict[str, object]]) -> None:
        self.rows_by_source = rows_by_source
        self.links = links
        self.batched_calls: list[tuple[list[str], int]] = []
        self.link_calls: list[list[str]] = []
        self.get_memory_calls: list[str] = []
        self.successors: dict[str, dict[str, object]] = {}

    def list_memories_referencing_sources(self, source_ids, *, limit_per_source):
        self.batched_calls.append((list(source_ids), limit_per_source))
        return {sid: list(self.rows_by_source.get(sid, ())) for sid in source_ids}

    def list_provenance_links_for_targets(self, *, target_type, target_ids):
        self.link_calls.append(list(target_ids))
        wanted = set(target_ids)
        return [link for link in self.links if link["target_id"] in wanted]

    def get_memory(self, memory_id):
        self.get_memory_calls.append(memory_id)
        return self.successors.get(memory_id)


def _stale_memory(memory_id: str, successor: str) -> dict[str, object]:
    return {
        "id": memory_id,
        "status": "superseded",
        "superseded_by": successor,
        "canonical_text": "New text",
        "updated_at": "2026-02-01T00:00:00Z",
    }


def _quote_link(memory_id: str, source_id: str) -> dict[str, object]:
    return {
        "target_id": memory_id,
        "source_id": source_id,
        "evidence_role": "quoted_from",
        "quote": "An older sentence",
    }


def _packed(source_id: str) -> dict[str, object]:
    return {"id": source_id, "excerpt": "An older sentence", "captured_at": "2026-01-01T00:00:00Z"}


def test_one_pack_reads_the_store_once_for_every_source() -> None:
    """One memory lookup at the label's cap of 50, one link lookup, for five sources.

    Mutations: call the store once per source in
    ``annotate_derived_memory_corrections``; read the links inside the per-source
    loop; change the cap passed to the lookup from 50.
    """

    ids = [f"s{index}" for index in range(5)]
    rows = {sid: [_stale_memory(f"m-{sid}", f"n-{sid}")] for sid in ids}
    links = [_quote_link(f"m-{sid}", sid) for sid in ids]
    store = _RecordingStore(rows, links)
    store.successors = {f"n-{sid}": {"id": f"n-{sid}", "status": "active"} for sid in ids}
    sources = [_packed(sid) for sid in ids]

    annotate_derived_memory_corrections(store, sources, memory_visible=lambda _row: True)

    assert store.batched_calls == [(ids, 50)]
    assert len(store.link_calls) == 1
    assert [source["current_memory_id"] for source in sources] == [f"n-{sid}" for sid in ids]


def test_a_very_large_pack_splits_the_link_ids_rather_than_exceed_the_variable_limit() -> None:
    """1,200 memory ids make three link lookups of at most 500, and lose no label.

    Mutation: pass every id in one call, or drop a batch's results.
    """

    ids = [f"s{index}" for index in range(24)]
    rows = {sid: [_stale_memory(f"m-{sid}-{n}", f"n-{sid}-{n}") for n in range(50)] for sid in ids}
    links = [_quote_link(f"m-{sid}-0", sid) for sid in ids]
    store = _RecordingStore(rows, links)
    store.successors = {f"n-{sid}-0": {"id": f"n-{sid}-0", "status": "active"} for sid in ids}
    sources = [_packed(sid) for sid in ids]

    annotate_derived_memory_corrections(store, sources, memory_visible=lambda _row: True)

    assert [len(call) for call in store.link_calls] == [500, 500, 200]
    assert all(source.get("derived_memory_corrected") is True for source in sources)


def test_the_batch_applies_the_visibility_fence_to_every_source() -> None:
    """One source's successor is hidden, another's is not: only the hidden id is withheld.

    Mutation: pass ``lambda _row: True`` as ``memory_visible`` in
    ``annotate_derived_memory_corrections`` or in ``_packable_sources``, or label
    from the first source's answer for all of them.
    """

    store = _RecordingStore(
        {"s1": [_stale_memory("m1", "n1")], "s2": [_stale_memory("m2", "n2")]},
        [_quote_link("m1", "s1"), _quote_link("m2", "s2")],
    )
    store.successors = {"n1": {"id": "n1", "status": "active"}, "n2": {"id": "n2", "status": "active"}}
    first, second = _packed("s1"), _packed("s2")
    annotate_derived_memory_corrections(
        store, [first, second], memory_visible=lambda row: row.get("id") != "n2"
    )
    assert first["derived_memory_corrected"] is True and first["current_memory_id"] == "n1"
    assert second["derived_memory_corrected"] is True and "current_memory_id" not in second


def test_a_store_without_the_batched_lookup_still_gets_the_same_labels() -> None:
    """Minimal stores keep working: the one-source method is called once per source, cap 50.

    Mutation: drop the fallback (the labels vanish), or call it without ``limit=50``.
    """

    calls: list[tuple[str, int]] = []

    class OneAtATime(_RecordingStore):
        list_memories_referencing_sources = None  # type: ignore[assignment]

        def list_memories_referencing_source(self, *, source_id, limit=500):
            calls.append((source_id, limit))
            return list(self.rows_by_source.get(source_id, ()))

    store = OneAtATime(
        {"s1": [_stale_memory("m1", "n1")], "s2": [_stale_memory("m2", "n2")]},
        [_quote_link("m1", "s1"), _quote_link("m2", "s2")],
    )
    store.successors = {"n1": {"id": "n1", "status": "active"}, "n2": {"id": "n2", "status": "active"}}
    sources = [_packed("s1"), _packed("s2")]
    annotate_derived_memory_corrections(store, sources, memory_visible=lambda _row: True)
    assert calls == [("s1", 50), ("s2", 50)]
    assert [source["current_memory_id"] for source in sources] == ["n1", "n2"]


def test_the_batched_label_functions_have_no_default_fence() -> None:
    """A defaulted fence is "no fence" for the next caller that forgets it.

    Mutation: give ``memory_visible`` a default on either function.
    """

    for func in (annotate_derived_memory_corrections, VNextRetrievalService._packable_sources):
        parameter = inspect.signature(func).parameters["memory_visible"]
        assert parameter.default is inspect.Parameter.empty, func.__qualname__
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, func.__qualname__


# --------------------------------------------------------------------------
# End to end on a real SQLite vault: same output, one lookup
# --------------------------------------------------------------------------

QUERY = "kettle shelf"


def _context(tmp_path: Path, monkeypatch) -> MCPRuntimeContext:
    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    database = _database(tmp_path)
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _store_do(context, fn):
    from alicebot_api.mcp_tools import _sqlite_path_from_url

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        return fn(SQLiteVNextStore(conn, USER_ID), conn)


def _call(context, name, arguments, *, key=None, full=False):
    from alicebot_api.mcp.registry import call_mcp_tool

    if key is None:
        os.environ.pop(AGENT_API_KEY_ENV, None)
    else:
        os.environ[AGENT_API_KEY_ENV] = key
    if full:
        os.environ[MCP_FULL_TOOLS_ENV] = "1"
    else:
        os.environ.pop(MCP_FULL_TOOLS_ENV, None)
    try:
        return call_mcp_tool(context, name=name, arguments=arguments)
    finally:
        os.environ.pop(AGENT_API_KEY_ENV, None)
        os.environ.pop(MCP_FULL_TOOLS_ENV, None)


def _mint(context, *, profile: str, project: str) -> str:
    _record, raw = _store_do(
        context,
        lambda s, _c: create_agent_key(
            s, user_id=USER_ID, agent_id=f"{profile}-{project}", permission_profile=profile, project_scope=project
        ),
    )
    return raw


def _capture(context, writer: str, text: str) -> tuple[str, str]:
    captured = _call(
        context,
        "alice_capture",
        {"raw_text": text, "title": text[:30], "domain": "project", "sensitivity": "public"},
        key=writer,
        full=True,
    )
    review = _call(context, "alice_memory_review", {"status": "all", "limit": 100}, full=True)
    memory_id = next(str(i["id"]) for i in review["items"] if text in json.dumps(i, default=str))
    return str(captured["source_id"]), memory_id


def _supersede(context, memory_id: str, text: str) -> str:
    done = _call(
        context,
        "alice_memory_correct",
        {
            "action": "supersede-existing",
            "review_item_id": memory_id,
            "replacement_title": "Kettle",
            "replacement_body": {"text": text},
            "reason": "moved",
        },
        full=True,
    )
    return str(done["replacement_object"]["id"])


def _set(context, memory_id: str, **patch) -> None:
    _store_do(context, lambda s, _c: s.update_memory(memory_id=memory_id, patch=patch, actor_type="user"))


def _seed_mixed_vault(context, monkeypatch) -> dict[str, str]:
    """Six excerpts that need six different answers from the label and its fence."""

    writer = _mint(context, profile="project_scoped_agent", project="acme")
    ids: dict[str, str] = {}
    ids["current_source"], _current = _capture(context, writer, "The kettle is stored on the zeroth shelf.")

    ids["visible_source"], visible_memory = _capture(context, writer, "The kettle is stored on the third shelf.")
    ids["visible_replacement"] = _supersede(context, visible_memory, "The kettle is stored on the first shelf.")

    ids["hidden_source"], hidden_memory = _capture(context, writer, "The kettle is stored on the fourth shelf.")
    ids["hidden_replacement"] = _supersede(context, hidden_memory, "The kettle is stored on the sixth shelf.")
    _set(context, ids["hidden_replacement"], sensitivity="confidential")

    ids["edited_source"], edited_memory = _capture(context, writer, "The kettle is stored on the fifth shelf.")
    _call(
        context,
        "alice_memory_correct",
        {
            "action": "edit-and-approve",
            "review_item_id": edited_memory,
            "body": {"text": "The kettle is stored on the seventh shelf."},
            "reason": "moved",
        },
        full=True,
    )

    ids["forgotten_source"], forgotten_memory = _capture(context, writer, "The kettle is stored on the second shelf.")
    _supersede(context, forgotten_memory, "The kettle is stored on the eighth shelf.")
    _store_do(
        context,
        lambda _s, conn: conn.execute(
            "UPDATE memories SET deleted_at = '2026-08-02T00:00:00Z' WHERE id = ?", (forgotten_memory,)
        ),
    )

    ids["beta_source"], beta_memory = _capture(context, writer, "The kettle is stored on the ninth shelf.")
    _supersede(context, beta_memory, "The kettle is stored on the tenth shelf.")

    def move(store, _conn):
        row = store.get_source(ids["beta_source"])
        metadata = dict(row.get("metadata_json") or {})
        metadata["project_id"] = "beta"
        metadata["project_scope"] = ["beta"]
        store.update_source(source_id=ids["beta_source"], patch={"metadata_json": metadata}, actor_type="user")

    _store_do(context, move)
    ids["acme_reader"] = _mint(context, profile="read_only_agent", project="acme")
    ids["beta_reader"] = _mint(context, profile="read_only_agent", project="beta")
    return ids


def _labels(payload: dict) -> dict[str, dict[str, object]]:
    return {
        str(row["id"]): {key: row[key] for key in ("derived_memory_corrected", "current_memory_id") if key in row}
        for row in payload.get("sources") or []
    }


def test_labels_and_outputs_are_identical_to_the_per_source_path(tmp_path: Path, monkeypatch) -> None:
    """Hidden, superseded, other-project, above-ceiling and deleted, through both tools.

    The per-source path is the batched method switched off on the store class,
    which sends the label to ``list_memories_referencing_source`` once per source.
    The two runs must return the same packed sources and the same recall results,
    and the labels must differ across the fixture, so a run that labelled
    nothing, or everything alike, would not pass.

    Mutations: stop passing ``memory_visible`` through the batch (the hidden id
    comes back); label only the first source of the batch; change the cap the
    batch asks for so a source loses a memory; drop ``deleted_at IS NULL`` from the
    batched SQL (the forgotten memory labels its source).
    """

    context = _context(tmp_path, monkeypatch)
    ids = _seed_mixed_vault(context, monkeypatch)

    def run(key):
        recall = _call(context, "alice_recall", {"query": QUERY, "limit": 20}, key=key)
        pack = _call(context, "alice_context_pack", {"query": QUERY}, key=key, full=True)
        return recall, pack

    seen_labels: list[tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]] = []
    for key in (None, ids["acme_reader"], ids["beta_reader"]):
        batched_recall, batched_pack = run(key)
        with monkeypatch.context() as patch:
            patch.setattr(SQLiteVNextStore, "list_memories_referencing_sources", None)
            single_recall, single_pack = run(key)
        for batched, single in ((batched_recall, single_recall), (batched_pack, single_pack)):
            assert batched["sources"], "no sources were packed; the comparison would be vacuous"
            assert json.dumps(batched["sources"], sort_keys=True, default=str) == json.dumps(
                single["sources"], sort_keys=True, default=str
            )
        assert json.dumps(batched_recall.get("results"), sort_keys=True, default=str) == json.dumps(
            single_recall.get("results"), sort_keys=True, default=str
        )
        seen_labels.append((_labels(batched_recall), _labels(batched_pack)))

    for tool_index in (0, 1):
        owner, acme, beta = (labels[tool_index] for labels in seen_labels)
        # Owner: current source unlabelled; visible successor named; hidden successor
        # flagged without an id when the ceiling hides it, named when it does not
        # (the owner's ceiling includes confidential); a forgotten memory labels
        # nothing.
        assert owner[ids["current_source"]] == {}
        assert owner[ids["visible_source"]]["current_memory_id"] == ids["visible_replacement"]
        assert owner[ids["hidden_source"]]["derived_memory_corrected"] is True
        assert owner[ids["forgotten_source"]] == {}
        # The acme reader's ceiling hides the confidential successor.
        assert acme[ids["hidden_source"]]["derived_memory_corrected"] is True
        assert "current_memory_id" not in acme[ids["hidden_source"]]
        assert acme[ids["visible_source"]]["current_memory_id"] == ids["visible_replacement"]
        assert acme[ids["edited_source"]]["derived_memory_corrected"] is True
        # The beta reader cannot see the acme memories: flag kept, id withheld.
        assert ids["visible_source"] not in beta
        assert beta[ids["beta_source"]]["derived_memory_corrected"] is True
        assert "current_memory_id" not in beta[ids["beta_source"]]


def _counting_service(connection: sqlite3.Connection, statements: list[str]) -> VNextRetrievalService:
    connection.set_trace_callback(statements.append)
    return VNextRetrievalService(SQLiteVNextStore(connection, USER_ID))


def _lookups(statements: list[str]) -> int:
    return sum(1 for statement in statements if LOOKUP_SIGNATURE in statement)


def test_recall_and_pack_run_one_memory_lookup_for_all_packed_sources(tmp_path: Path, monkeypatch) -> None:
    """Statements, not method calls: one lookup for ten sources on each path.

    ``search_source_excerpts`` is recall's source path and ``compile_context_pack``
    the pack's own. The control asserts the lookup ran at all, so a renamed SQL
    shape cannot turn this into a test that counts nothing.

    Mutations: restore the per-source loop in
    ``annotate_derived_memory_corrections``, or make the store's batched method
    loop over ``list_memories_referencing_source``.
    """

    context = _context(tmp_path, monkeypatch)
    writer = _mint(context, profile="project_scoped_agent", project="acme")
    for index in range(10):
        _source_id, memory_id = _capture(context, writer, f"The kettle is stored on shelf number {index}.")
        if index % 2:
            _supersede(context, memory_id, f"The kettle is stored on shelf number {index + 100}.")

    from alicebot_api.mcp_tools import _sqlite_path_from_url

    database = _sqlite_path_from_url(context.database_url)
    with sqlite_user_connection(database, USER_ID) as connection:
        statements: list[str] = []
        service = _counting_service(connection, statements)
        excerpts, _stage = service.search_source_excerpts(
            query="kettle shelf",
            domains=[],
            sensitivity_allowed=["public", "internal", "private", "confidential", "unknown"],
            limit=10,
            scope=None,
            ranking=SourceRanking.document(),
        )
        assert len(excerpts) >= 8, "too few sources packed for the count to mean anything"
        assert _lookups(statements) == 1, _lookups(statements)

        statements.clear()
        pack = service.compile_context_pack(VNextRetrievalRequest(query="kettle shelf"), source_fence=SourceReadFence.unfenced())
        assert len(pack["sources"]) >= 8
        assert _lookups(statements) == 1, _lookups(statements)


def test_the_lookup_count_does_not_grow_with_the_sources_packed(tmp_path: Path, monkeypatch) -> None:
    """Two sources and ten sources cost the same number of lookups.

    Mutation: restore the per-source loop (the ten-source run counts ten).
    """

    context = _context(tmp_path, monkeypatch)
    writer = _mint(context, profile="project_scoped_agent", project="acme")
    for index in range(10):
        _capture(context, writer, f"The kettle is stored on shelf number {index}.")

    from alicebot_api.mcp_tools import _sqlite_path_from_url

    database = _sqlite_path_from_url(context.database_url)
    counts: list[int] = []
    with sqlite_user_connection(database, USER_ID) as connection:
        statements: list[str] = []
        service = _counting_service(connection, statements)
        for limit in (2, 10):
            statements.clear()
            excerpts, _stage = service.search_source_excerpts(
                query="kettle shelf",
                domains=[],
                sensitivity_allowed=["public", "internal", "private", "confidential", "unknown"],
                limit=limit,
                scope=None,
                ranking=SourceRanking.document(),
            )
            assert len(excerpts) == limit
            counts.append(_lookups(statements))
    assert counts == [1, 1]
