"""Live PostgreSQL regression for content-stale vector backfill selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_embeddings import (
    memory_embedding_content_sha256,
    pad_embedding_vector,
)
from alicebot_api.vnext_store import PostgresVNextStore


def test_backfill_selects_vector_whose_content_signature_is_stale(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"embedding-backfill-{user_id}@example.invalid",
            "Embedding backfill",
        )
        store = PostgresVNextStore(conn)
        memory = store.create_memory(
            {
                "memory_key": f"embedding.backfill.{uuid4()}",
                "value": {"text": "Original fact"},
                "status": "active",
                "title": "Signed memory",
                "canonical_text": "Original fact",
                "summary": "A signed vector",
                "domain": "project",
                "sensitivity": "private",
            }
        )
        store.update_memory_embedding(
            memory_id=str(memory["id"]),
            vector=pad_embedding_vector([1.0, 0.0]),
            provider="openai_compatible",
            model="embed-v1",
            endpoint="host-a",
            content_sha256=memory_embedding_content_sha256(memory),
            signature_version=2,
        )
        assert (
            store.list_memories_missing_embeddings(
                statuses=("active", "accepted"),
                embedding_provider="openai_compatible",
                embedding_model="embed-v1",
                embedding_endpoint="host-a",
                embedding_signature_version=2,
            )
            == []
        )

        # Simulate an old snapshot/direct adapter bypassing lifecycle hooks.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET canonical_text = %s WHERE id = %s::uuid",
                ("Changed fact", str(memory["id"])),
            )

        stale = store.list_memories_missing_embeddings(
            statuses=("active", "accepted"),
            embedding_provider="openai_compatible",
            embedding_model="embed-v1",
            embedding_endpoint="host-a",
            embedding_signature_version=2,
        )

    assert [str(row["id"]) for row in stale] == [str(memory["id"])]
    assert stale[0]["embedding_present"] is True


@pytest.mark.parametrize(
    ("boundary", "title", "canonical_text", "summary"),
    [
        ("nbsp", "\u00a0Signed memory\u00a0", "\u00a0Whitespace fact\u00a0", "\u00a0Whitespace fact\u00a0"),
        ("u001c", "\u001cSigned memory\u001c", "\u001cWhitespace fact\u001c", "\u001cWhitespace fact\u001c"),
        (
            "mixed_blank",
            "\u00a0\u001c",
            "\u001cMixed whitespace fact\u00a0",
            "\u00a0Mixed whitespace fact\u001c",
        ),
    ],
)
def test_embedding_cas_matches_python_strip_for_unicode_boundaries(
    migrated_database_urls,
    boundary: str,
    title: str,
    canonical_text: str,
    summary: str,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"embedding-whitespace-{boundary}-{user_id}@example.invalid",
            f"Embedding whitespace {boundary}",
        )
        store = PostgresVNextStore(conn)
        memory = store.create_memory(
            {
                "memory_key": f"embedding.whitespace.{uuid4()}",
                "value": {"text": "Whitespace fact"},
                "status": "active",
                "title": title,
                "canonical_text": canonical_text,
                "summary": summary,
                "domain": "project",
                "sensitivity": "private",
            }
        )
        digest = memory_embedding_content_sha256(memory)
        updated = store.update_memory_embedding(
            memory_id=str(memory["id"]),
            vector=pad_embedding_vector([1.0, 0.0]),
            provider="openai_compatible",
            model="embed-v1",
            endpoint="host-a",
            content_sha256=digest,
            signature_version=2,
        )

        assert updated is not None
        assert (
            store.list_memories_missing_embeddings(
                statuses=("active", "accepted"),
                embedding_provider="openai_compatible",
                embedding_model="embed-v1",
                embedding_endpoint="host-a",
                embedding_signature_version=2,
            )
            == []
        )
        vector_rows = store.search_memories_vector(
            query_vector=pad_embedding_vector([1.0, 0.0]),
            embedding_provider="openai_compatible",
            embedding_model="embed-v1",
            embedding_endpoint="host-a",
            embedding_signature_version=2,
            limit=5,
        )
        assert str(memory["id"]) in {str(row["id"]) for row in vector_rows}


def test_backfill_lists_only_the_statuses_it_is_asked_for(migrated_database_urls) -> None:
    """The list names rows by status: a forgotten, rejected or candidate memory is not listed for embedding.

    One memory in each of nine statuses, none with a vector. Asked for the two
    statuses recall can return, the list holds the active and the accepted
    memory and no other. A superseded memory that holds a vector the signature
    calls stale (another model) is left out of that list as well, and is
    listed when the superseded status is asked for. In v0.19.2 the list had no
    status argument and held every row of the user.

    Mutation: drop ``AND status IN (...)`` from ``list_memories_missing_embeddings``
    in ``vnext_stores/postgres/embedding_cas.py``. The first assertion then
    lists nine rows. Or move the status test inside the ``embedding_vector IS
    NULL`` branch of the condition, and the stale superseded row is listed.
    """

    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    statuses = (
        "candidate",
        "active",
        "accepted",
        "rejected",
        "superseded",
        "archived",
        "needs_review",
        "private_only",
        "stale",
    )
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"embedding-statuses-{user_id}@example.invalid",
            "Embedding statuses",
        )
        store = PostgresVNextStore(conn)
        created = {}
        for status in statuses:
            created[status] = store.create_memory(
                {
                    "memory_key": f"embedding.status.{status}.{uuid4()}",
                    "value": {"text": f"A {status} fact"},
                    "status": status,
                    "title": f"{status} memory",
                    "canonical_text": f"A {status} fact",
                    "summary": f"A {status} summary",
                    "domain": "project",
                    "sensitivity": "private",
                }
            )
        ids = {status: str(memory["id"]) for status, memory in created.items()}

        live = store.list_memories_missing_embeddings(statuses=("active", "accepted"))
        assert sorted(str(row["id"]) for row in live) == sorted([ids["active"], ids["accepted"]])
        assert all(row["embedding_present"] is False for row in live)

        only_candidate = store.list_memories_missing_embeddings(statuses=("candidate",))
        assert [str(row["id"]) for row in only_candidate] == [ids["candidate"]]

        # a forgotten memory whose vector was made under another model is not listed for the new one
        updated = store.update_memory_embedding(
            memory_id=ids["superseded"],
            vector=pad_embedding_vector([1.0, 0.0]),
            provider="openai_compatible",
            model="embed-v1",
            endpoint="host-a",
            content_sha256=memory_embedding_content_sha256(created["superseded"]),
            signature_version=2,
        )
        assert updated is not None
        signature = {
            "embedding_provider": "openai_compatible",
            "embedding_model": "embed-v2",
            "embedding_endpoint": "host-a",
            "embedding_signature_version": 2,
        }
        live_under_new_model = store.list_memories_missing_embeddings(
            statuses=("active", "accepted"), **signature
        )
        assert sorted(str(row["id"]) for row in live_under_new_model) == sorted(
            [ids["active"], ids["accepted"]]
        )
        forgotten = store.list_memories_missing_embeddings(statuses=("superseded",), **signature)
        assert [str(row["id"]) for row in forgotten] == [ids["superseded"]]
        assert forgotten[0]["embedding_present"] is True


def test_backfill_leaves_out_a_memory_whose_valid_to_has_passed(migrated_database_urls) -> None:
    """The list leaves out an expired memory, and agrees with what vector recall returns.

    Four active memories with no vector: no ``valid_to``, one far in the future,
    one a day in the past, and one whose window closed a second ago. The list holds
    the first two. Once all four have a vector, vector search returns the same two
    and not the expired ones, so the listing's expiry test and recall's are one
    test, and a stale vector under a new model is listed for the same two rows
    only. In v0.19.2 the list held all four.

    Mutations: drop ``AND {POSTGRES_UNEXPIRED_SQL}`` from
    ``list_memories_missing_embeddings`` in ``vnext_stores/postgres/embedding_cas.py``
    (the first assertion lists four rows), or flip the comparison in
    ``POSTGRES_UNEXPIRED_SQL`` to ``<`` (the open-window rows drop out and the
    expired ones are listed).
    """

    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    now = datetime.now(UTC)
    windows = {
        "no_window": None,
        "far_future": now + timedelta(days=3650),
        "a_day_ago": now - timedelta(days=1),
        "a_second_ago": now - timedelta(seconds=1),
    }
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"embedding-expiry-{user_id}@example.invalid",
            "Embedding expiry",
        )
        store = PostgresVNextStore(conn)
        created = {}
        for label, valid_to in windows.items():
            created[label] = store.create_memory(
                {
                    "memory_key": f"embedding.expiry.{label}.{uuid4()}",
                    "value": {"text": f"A {label} fact"},
                    "status": "active",
                    "title": f"{label} memory",
                    "canonical_text": f"A {label} fact",
                    "summary": f"A {label} summary",
                    "domain": "project",
                    "sensitivity": "private",
                    "valid_to": valid_to,
                }
            )
        ids = {label: str(memory["id"]) for label, memory in created.items()}
        open_ids = sorted([ids["no_window"], ids["far_future"]])

        listed = store.list_memories_missing_embeddings(statuses=("active", "accepted"))
        assert sorted(str(row["id"]) for row in listed) == open_ids

        for label, memory in created.items():
            assert (
                store.update_memory_embedding(
                    memory_id=ids[label],
                    vector=pad_embedding_vector([1.0, 0.0]),
                    provider="openai_compatible",
                    model="embed-v1",
                    endpoint="host-a",
                    content_sha256=memory_embedding_content_sha256(memory),
                    signature_version=2,
                )
                is not None
            )
        recalled = store.search_memories_vector(
            query_vector=pad_embedding_vector([1.0, 0.0]),
            embedding_provider="openai_compatible",
            embedding_model="embed-v1",
            embedding_endpoint="host-a",
            embedding_signature_version=2,
            limit=10,
        )
        assert sorted(str(row["id"]) for row in recalled) == open_ids

        # under a new model every row holds a stale vector: only the open-window rows are listed
        signature = {
            "embedding_provider": "openai_compatible",
            "embedding_model": "embed-v2",
            "embedding_endpoint": "host-a",
            "embedding_signature_version": 2,
        }
        under_new_model = store.list_memories_missing_embeddings(statuses=("active", "accepted"), **signature)
        assert sorted(str(row["id"]) for row in under_new_model) == open_ids
