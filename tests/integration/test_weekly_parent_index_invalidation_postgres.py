"""A write or a rollback in a request drops that request's weekly-parent index and the labels cached with it.

One request reads the reports that name each weekly-synthesis candidate once and judges every candidate
from that index. A write to a report, or a rollback of a savepoint that created one, makes the index
wrong for the rest of the request, so the next read in the same scope must load it again and judge the
candidate as a read in a fresh scope does. Each test here compares the two.

A rollback that does not go through ``store.savepoint()`` is the same case. The product has blocks that open
their own transaction or savepoint (the capture loop, the optional embedding write), and a read inside one
caches labels that the block's writes produced. When the block rolls back, those labels describe rows that
no longer exist.

Everything goes through the production store and a guard with a ``public`` ceiling. SQLite cannot store
the report, so ``tests/unit/test_weekly_parent_index_invalidation.py`` covers the same drops with a store
that offers the reports.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from alembic import command

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_closure import load_weekly_parent_artifacts, weekly_parent_query_count
from alicebot_api.vnext_embeddings import _embedding_write_savepoint
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_writes import capture_label_inputs, without_insert_floor
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


@contextmanager
def _the_stores_savepoint(store: PostgresVNextStore, source_id: str):
    with store.savepoint():
        yield


@contextmanager
def _the_capture_loops_transaction(store: PostgresVNextStore, source_id: str):
    with capture_label_inputs(store, source_id):
        yield


@contextmanager
def _the_embedding_writes_transaction(store: PostgresVNextStore, source_id: str):
    with _embedding_write_savepoint(store):
        yield


@pytest.mark.parametrize(
    "block",
    [_the_stores_savepoint, _the_capture_loops_transaction, _the_embedding_writes_transaction],
    ids=["store-savepoint", "capture-loop", "embedding-write"],
)
def test_a_rollback_of_any_block_that_wrote_a_report_leaves_no_label_cached_in_the_same_scope(
    database_urls, block
) -> None:
    """A second weekly memory gains a parent inside the block and loses it again when the block rolls back."""

    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    with user_connection(database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = PostgresVNextStore(conn)
        public_id = _source(store, "Public note", "public")
        named_id = _weekly_memory(store, "rollbacknamedtoken")
        renamed_id = _weekly_memory(store, "rollbackrenamedtoken")
        with without_insert_floor():
            report = store.create_artifact(_report(public_id, named_id), actor_type="system")
        with label_read_scope(store):
            before = _judged(store, renamed_id)
            try:
                with block(store, public_id):
                    store.update_artifact_status(
                        artifact_id=str(report["id"]),
                        status="needs_review",
                        expected_status="needs_review",
                        metadata_json={"candidate_memory_ids": [renamed_id]},
                        actor_type="system",
                    )
                    inside = _judged(store, renamed_id)
                    raise _RollBack
            except _RollBack:
                pass
            in_the_same_scope = _judged(store, renamed_id)
        in_a_fresh_scope = _judged_in_a_fresh_scope(store, renamed_id)
    assert before[1] is True, "no report names this memory yet, so it has no parent"
    assert inside == ("public", False), "inside the block the renamed report names it"
    assert in_a_fresh_scope == before
    assert in_the_same_scope == in_a_fresh_scope


def test_a_lookup_for_another_user_inside_a_scope_is_answered_from_that_users_own_reports(database_urls) -> None:
    """The scope belongs to one store. Another store's lookup neither reads its index nor leaves its own in it."""

    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    first_user, second_user = uuid4(), uuid4()
    with user_connection(database_urls["app"], first_user) as first_conn:
        ContinuityStore(first_conn).create_user(first_user, f"owner-{first_user.hex[:8]}@example.invalid", "Owner")
        first = PostgresVNextStore(first_conn)
        first_source = _source(first, "First note", "public")
        first_memory = _weekly_memory(first, "firstusertoken")
        with without_insert_floor():
            first_report = first.create_artifact(_report(first_source, first_memory), actor_type="system")
        with user_connection(database_urls["app"], second_user) as second_conn:
            ContinuityStore(second_conn).create_user(second_user, f"owner-{second_user.hex[:8]}@example.invalid", "Owner")
            second = PostgresVNextStore(second_conn)
            second_source = _source(second, "Second note", "public")
            second_memory = _weekly_memory(second, "secondusertoken")
            with without_insert_floor():
                second_report = second.create_artifact(_report(second_source, second_memory), actor_type="system")
            with label_read_scope(first):
                # The second user's lookup comes first, so a leftover of it would be the first user's index.
                second_first = [row["id"] for row in load_weekly_parent_artifacts(second, second_memory)]
                assert weekly_parent_query_count() == 0
                first_own = [row["id"] for row in load_weekly_parent_artifacts(first, first_memory)]
                assert weekly_parent_query_count() == 1
                # The first user's index is loaded now, so a read of it would answer the second user's lookup.
                second_again = [row["id"] for row in load_weekly_parent_artifacts(second, second_memory)]
                assert weekly_parent_query_count() == 1
                first_again = [row["id"] for row in load_weekly_parent_artifacts(first, first_memory)]
                assert weekly_parent_query_count() == 1
    assert second_first == [second_report["id"]]
    assert second_again == [second_report["id"]]
    assert first_own == [first_report["id"]]
    assert first_again == [first_report["id"]]
