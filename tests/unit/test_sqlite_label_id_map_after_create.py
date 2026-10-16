"""A SQLite request does not keep an id map that predates a row created in it.

The store lists every stored id of a kind once for a request, under its canonical spelling, so that a row
that cites an id in one spelling finds the row stored in another. A create reads that map while its insert
floor runs, before the new row exists. If the map outlives the create, a row stored under a spelling that
an earlier row already cites is missing from it, and the earlier row is judged without it for the rest of
the request: a confidential memory reads as public in the same scope and as confidential in a fresh one.

No product code passes an ``id`` into ``create_memory`` today. The store keeps whatever id a caller gives,
and the label code reads any spelling, so this is the case the map exists for.

The map belongs to the store that opened the scope. A store of another user that reads label rows inside the scope has
its own rows, answered from its own ids; it neither reads the scope's map nor fills it, and a write through it leaves
the scope's map alone.

Mutations: leave the id map out of what ``takes_label_lock`` drops after a write; let a lookup through another store
use the scope's map (the store check of ``request_row_cache``); let a drop through another store empty the scope's map
(the store check of ``drop_request_row_cache``).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_labels import identifier
from alicebot_api.vnext_label_guard import SQLITE_LABEL_ID_MAP, LabelGuard, label_read_scope, request_row_cache
from alicebot_api.vnext_label_writes import without_insert_floor

USER = "11111111-1111-4111-8111-111111111111"
OTHER_USER = "22222222-2222-4222-8222-222222222222"
CEILING = ("public", "internal", "private", "unknown")


def _judged(store: SQLiteVNextStore, memory_id: str) -> tuple[str, bool]:
    guard = LabelGuard.for_filters(store, (), CEILING, ())
    row = guard.effective_row("memory", store.get_memory(memory_id))
    return str(row["sensitivity"]), bool(row["unverified"])


def _judged_in_a_fresh_scope(store: SQLiteVNextStore, memory_id: str) -> tuple[str, bool]:
    def run() -> tuple[str, bool]:
        with label_read_scope(store):
            return _judged(store, memory_id)

    return contextvars.Context().run(run)


def _source(store: SQLiteVNextStore, title: str, sensitivity: str) -> dict[str, object]:
    return store.create_source(
        {"source_type": "note", "title": title, "content_hash": title, "sensitivity": sensitivity, "domain": "project"}
    )


def test_a_memory_created_under_a_capital_id_is_found_by_a_row_that_cites_it_in_lower_case(tmp_path) -> None:
    path = tmp_path / "id-map.db"
    bootstrap_database(path, user_id=USER, user_email="synthetic@example.invalid")
    named = str(uuid4())
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        public = _source(store, "public", "public")
        confidential = _source(store, "confidential", "confidential")
        parent = store.create_memory(
            {"memory_key": "parent", "canonical_text": "parent", "status": "active", "domain": "project", "sensitivity": "public"}
        )
        # An earlier memory cites the id the later one will get, in lower case.
        with without_insert_floor():
            citing = store.create_memory(
                {
                    "memory_key": "citing",
                    "canonical_text": "citing",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "value": {"memory_id": named},
                    "metadata_json": {"source_id": public["id"]},
                }
            )
        citing_id = str(citing["id"])
        with label_read_scope(store):
            assert _judged(store, citing_id) == ("public", False)
            # The later memory reads a memory parent in its floor, so the memory id map is built before its row exists.
            created = store.create_memory(
                {
                    "id": named.upper(),
                    "memory_key": "named",
                    "canonical_text": "named",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {
                        "source_id": confidential["id"],
                        "consolidation": {"cluster_member_ids": [str(parent["id"])]},
                    },
                }
            )
            in_the_same_scope = _judged(store, citing_id)
        in_a_fresh_scope = _judged_in_a_fresh_scope(store, citing_id)
    assert created["id"] == named.upper()
    assert created["sensitivity"] == "confidential"
    assert in_a_fresh_scope == ("confidential", False)
    assert in_the_same_scope == in_a_fresh_scope


@contextmanager
def _two_stores(tmp_path) -> Iterator[tuple[SQLiteVNextStore, SQLiteVNextStore]]:
    """Two users, each with a vault of their own."""

    paths = [tmp_path / "first.db", tmp_path / "second.db"]
    for path, user in zip(paths, (USER, OTHER_USER), strict=True):
        bootstrap_database(path, user_id=user, user_email="synthetic@example.invalid")
    with sqlite_user_connection(paths[0], USER) as first_conn, sqlite_user_connection(paths[1], OTHER_USER) as second_conn:
        yield SQLiteVNextStore(first_conn, USER), SQLiteVNextStore(second_conn, OTHER_USER)


def _memory_stored_in_capitals(store: SQLiteVNextStore, key: str) -> tuple[str, str]:
    """A memory whose id is stored in capitals, and the same id in lower case, which is how a row cites it."""

    named = str(uuid4())
    store.create_memory(
        {"id": named.upper(), "memory_key": key, "canonical_text": key, "status": "active", "domain": "project",
         "sensitivity": "public"}
    )
    return named.upper(), named


def _ids(rows: list[dict[str, object]]) -> list[object]:
    return [row["id"] for row in rows]


def _listed(store: SQLiteVNextStore) -> dict:
    """What the scope has listed of ``store``'s stored ids so far, by kind."""

    cache = request_row_cache(store, SQLITE_LABEL_ID_MAP)
    assert cache is not None, "the store has no scope"
    return cache


def test_a_lookup_through_another_store_is_answered_from_that_stores_own_ids(tmp_path) -> None:
    with _two_stores(tmp_path) as (first, second):
        first_stored, first_cited = _memory_stored_in_capitals(first, "first")
        second_stored, second_cited = _memory_stored_in_capitals(second, "second")
        with label_read_scope(first):
            # The scope's own store lists its ids once, so its map is filled.
            assert _ids(first.read_label_rows("memory", [first_cited])) == [first_stored]
            assert set(_listed(first)["memory"]) == {identifier(first_stored)}
            # The other user's lookup cites its memory in lower case. Its row is stored in capitals, so it is found
            # only through that user's own ids, never through the scope's map.
            assert request_row_cache(second, SQLITE_LABEL_ID_MAP) is None
            assert _ids(second.read_label_rows("memory", [second_cited])) == [second_stored]
            assert _ids(first.read_label_rows("memory", [first_cited])) == [first_stored]


def test_a_lookup_through_another_store_does_not_fill_the_map_of_the_scope(tmp_path) -> None:
    with _two_stores(tmp_path) as (first, second):
        first_stored, first_cited = _memory_stored_in_capitals(first, "first")
        second_stored, second_cited = _memory_stored_in_capitals(second, "second")
        with label_read_scope(first):
            assert _ids(second.read_label_rows("memory", [second_cited])) == [second_stored]
            # Nothing of the scope's was listed yet. Its first lookup lists its own ids, not the other user's.
            assert "memory" not in _listed(first)
            assert _ids(first.read_label_rows("memory", [first_cited])) == [first_stored]
            assert set(_listed(first)["memory"]) == {identifier(first_stored)}


def test_a_write_through_another_store_leaves_the_map_of_the_scope_alone(tmp_path) -> None:
    with _two_stores(tmp_path) as (first, second):
        first_stored, first_cited = _memory_stored_in_capitals(first, "first")
        with label_read_scope(first):
            assert _ids(first.read_label_rows("memory", [first_cited])) == [first_stored]
            listed = _listed(first)
            assert "memory" in listed
            # A write through the other user's store drops what that store's own write read, and only that.
            _memory_stored_in_capitals(second, "second")
            assert _listed(first) is listed
            assert "memory" in listed
            # A write through the scope's own store drops its map, so the next lookup lists the ids again.
            _memory_stored_in_capitals(first, "first again")
            assert _listed(first) == {}
