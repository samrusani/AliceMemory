"""A SQLite request does not keep an id map that predates a row created in it.

The store lists every stored id of a kind once for a request, under its canonical spelling, so that a row
that cites an id in one spelling finds the row stored in another. A create reads that map while its insert
floor runs, before the new row exists. If the map outlives the create, a row stored under a spelling that
an earlier row already cites is missing from it, and the earlier row is judged without it for the rest of
the request: a confidential memory reads as public in the same scope and as confidential in a fresh one.

No product code passes an ``id`` into ``create_memory`` today. The store keeps whatever id a caller gives,
and the label code reads any spelling, so this is the case the map exists for.

Mutations: leave the id map out of what ``takes_label_lock`` drops after a write.
"""

from __future__ import annotations

import contextvars
from uuid import uuid4

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_writes import without_insert_floor

USER = "11111111-1111-4111-8111-111111111111"
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
