"""A write or a savepoint rollback in a request drops that request's weekly-parent index.

One request reads the reports that name each weekly-synthesis candidate once and judges every candidate
from that index. A write to a report, or a rollback of a savepoint that created one, makes the index
wrong for the rest of the request, so the next read in the same scope must load it again and judge the
candidate as a read in a fresh scope does. Each test here compares the two.

Everything goes through the production store and a guard with a ``public`` ceiling. SQLite cannot store
the report, so ``tests/unit/test_weekly_parent_index_invalidation.py`` covers the same drops with a store
that offers the reports.
"""

from __future__ import annotations

import json
from uuid import uuid4

from alembic import command

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_closure import weekly_parent_query_count
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import database_urls  # noqa: F401

CEILING = ("public", "internal", "private", "unknown")
WEEKLY_MEMORIES = 40


class _RollBack(Exception):
    pass


def _summary(*source_ids: str) -> dict[str, object]:
    return {
        "source_ids": list(source_ids),
        "memory_ids": [],
        "open_loop_ids": [],
        "artifact_ids": [],
        "counts": {"sources": len(source_ids), "memories": 0, "open_loops": 0, "artifacts": 0},
    }


def _source(store: PostgresVNextStore, title: str, sensitivity: str) -> str:
    row = store.create_source(
        {
            "source_type": "note",
            "title": title,
            "content_hash": "sha256:" + uuid4().hex,
            "domain": "project",
            "sensitivity": sensitivity,
            "metadata_json": {"raw_text": title},
        }
    )
    return str(row["id"])


def _weekly_memory(store: PostgresVNextStore, token: str) -> str:
    with without_insert_floor():
        row = store.create_memory(
            {
                "memory_type": "artifact_summary",
                "memory_key": "weekly_synthesis." + uuid4().hex,
                "value": {"insight": f"The weekly pattern names {token}."},
                "status": "active",
                "confidence": 0.6,
                "canonical_text": f"The weekly pattern names {token}.",
                "summary": f"The weekly pattern names {token}.",
                "title": "Weekly candidate",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {
                    "candidate": True,
                    "discovered_by": "vnext_weekly_synthesis",
                    "generated_by": "system",
                    "project_scope": [],
                    "workflow_digest": uuid4().hex,
                },
            },
            actor_type="system",
        )
    return str(row["id"])


def _report(source_id: str, memory_id: str) -> dict[str, object]:
    return {
        "artifact_type": "weekly_synthesis",
        "title": "Weekly Synthesis",
        "content_markdown": "Weekly report.",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "public",
        "generated_by": "vnext_weekly_synthesis",
        "metadata_json": {
            "workflow": "weekly_synthesis",
            "generated_by": "system",
            "project_scope": [],
            "source_refs": [f"source:{source_id}"],
            "input_summary": _summary(source_id),
            "candidate_memory_ids": [memory_id],
            "workflow_digest": uuid4().hex,
        },
    }


def _judged(store: PostgresVNextStore, memory_id: str) -> tuple[str, bool]:
    """The sensitivity and the unverified flag a guard with a public ceiling gives the memory right now."""

    guard = LabelGuard.for_filters(store, (), CEILING, ())
    row = guard.effective_row("memory", store.get_memory(memory_id))
    return str(row["sensitivity"]), bool(row["unverified"])


def _judged_in_a_fresh_scope(store: PostgresVNextStore, memory_id: str) -> tuple[str, bool]:
    with label_read_scope(store):
        return _judged(store, memory_id)


def test_a_report_whose_inputs_gain_a_confidential_source_is_read_again_in_the_same_scope(database_urls) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    with user_connection(database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = PostgresVNextStore(conn)
        public_id = _source(store, "Public note", "public")
        secret_id = _source(store, "Confidential note", "confidential")
        memory_id = _weekly_memory(store, "indexchangedtoken")
        with without_insert_floor():
            report = store.create_artifact(_report(public_id, memory_id), actor_type="system")
        with label_read_scope(store):
            assert _judged(store, memory_id) == ("public", False)
            assert weekly_parent_query_count() == 1
            updated = store.update_artifact_status(
                artifact_id=str(report["id"]),
                status="needs_review",
                expected_status="needs_review",
                metadata_json={
                    "source_refs": [f"source:{public_id}", f"source:{secret_id}"],
                    "input_summary": _summary(public_id, secret_id),
                },
                actor_type="system",
            )
            assert updated is not None
            in_the_same_scope = _judged(store, memory_id)
        in_a_fresh_scope = _judged_in_a_fresh_scope(store, memory_id)
    assert in_a_fresh_scope == ("confidential", False)
    assert in_the_same_scope == in_a_fresh_scope


def test_a_report_created_in_a_rolled_back_savepoint_is_not_a_parent_in_the_same_scope(database_urls) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    with user_connection(database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = PostgresVNextStore(conn)
        public_id = _source(store, "Public note", "public")
        memory_id = _weekly_memory(store, "indexrolledbacktoken")
        with label_read_scope(store):
            try:
                with store.savepoint():
                    with without_insert_floor():
                        store.create_artifact(_report(public_id, memory_id), actor_type="system")
                    inside = _judged(store, memory_id)
                    raise _RollBack
            except _RollBack:
                pass
            in_the_same_scope = _judged(store, memory_id)
        in_a_fresh_scope = _judged_in_a_fresh_scope(store, memory_id)
    assert inside == ("public", False)
    assert in_a_fresh_scope[1] is True
    assert in_the_same_scope == in_a_fresh_scope


def test_a_report_stored_by_a_write_that_read_the_reports_first_is_a_parent_in_the_same_scope(database_urls) -> None:
    """The insert floor of a report that cites a weekly memory reads the reports before the new one exists."""

    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    with user_connection(database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = PostgresVNextStore(conn)
        public_id = _source(store, "Public note", "public")
        cited_id = _weekly_memory(store, "indexcitedtoken")
        named_id = _weekly_memory(store, "indexnamedtoken")
        with without_insert_floor():
            store.create_artifact(_report(public_id, cited_id), actor_type="system")
        later = _report(public_id, named_id)
        later["metadata_json"]["input_summary"] = {  # type: ignore[index]
            **_summary(public_id),
            "memory_ids": [cited_id],
            "counts": {"sources": 1, "memories": 1, "open_loops": 0, "artifacts": 0},
        }
        with label_read_scope(store):
            assert weekly_parent_query_count() == 0
            store.create_artifact(later, actor_type="system")
            # The floor of this insert judged the cited weekly memory, so it read the reports once, before the insert.
            assert weekly_parent_query_count() == 1
            in_the_same_scope = _judged(store, named_id)
            assert weekly_parent_query_count() == 2
        in_a_fresh_scope = _judged_in_a_fresh_scope(store, named_id)
    assert in_a_fresh_scope == ("public", False)
    assert in_the_same_scope == in_a_fresh_scope


def _plant_weekly_memories(conn, user_id) -> list[str]:
    ids = []
    values = []
    for index in range(WEEKLY_MEMORIES):
        row_id = str(uuid4())
        ids.append(row_id)
        meta = json.dumps(
            {
                "candidate": True,
                "discovered_by": "vnext_weekly_synthesis",
                "project_scope": [],
                "workflow_digest": uuid4().hex,
                "observation_index": index,
            }
        )
        values.append((row_id, str(user_id), "budget." + row_id, "weekly filler " + str(index), meta))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity) "
            "VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,'{}'::jsonb,'{}'::jsonb,'active','project','public')",
            values,
        )
    return ids


def test_the_parent_query_runs_once_a_request_and_once_more_after_each_write(database_urls) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    with user_connection(database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = PostgresVNextStore(conn)
        public_id = _source(store, "Public note", "public")
        named_id = _weekly_memory(store, "indexcounttoken")
        with without_insert_floor():
            report = store.create_artifact(_report(public_id, named_id), actor_type="system")
        ids = _plant_weekly_memories(conn, user_id)
        with label_read_scope(store):
            assert weekly_parent_query_count() == 0
            for memory_id in ids:
                _judged(store, memory_id)
            assert weekly_parent_query_count() == 1
            # Reading again, with no write between, runs no query.
            for memory_id in ids:
                _judged(store, memory_id)
            _judged(store, named_id)
            assert weekly_parent_query_count() == 1
            # A write, then reads: exactly one more query, however many memories the reads judge.
            store.update_artifact_status(
                artifact_id=str(report["id"]),
                status="needs_review",
                expected_status="needs_review",
                metadata_json={"note": "reviewed"},
                actor_type="system",
            )
            assert weekly_parent_query_count() == 1
            for memory_id in ids:
                _judged(store, memory_id)
            _judged(store, named_id)
            assert weekly_parent_query_count() == 2
            # A rolled back savepoint is a write for this purpose, and so is a second write.
            try:
                with store.savepoint():
                    raise _RollBack
            except _RollBack:
                pass
            for memory_id in ids:
                _judged(store, memory_id)
            assert weekly_parent_query_count() == 3
