"""Live PostgreSQL check of the batched memories-by-source lookup.

``list_memories_referencing_sources`` runs the one-source query once per id
inside a ``LATERAL`` subquery. The unit tests pin the statement text; only a
live database proves the statement parses and returns, for every id, the rows
the one-source method returns. The fixture also checks the two controls the
one-source query relies on: the user's row-level security and ``deleted_at``.

Mutations that must fail this test: drop ``m.deleted_at IS NULL`` from the
batched statement; drop any one OR branch from it; move ``LIMIT`` out of the
``LATERAL`` subquery; drop the ``ORDER BY`` inside it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore


def _source(store: PostgresVNextStore, name: str) -> str:
    row = store.create_source(
        {
            "source_type": "note",
            "title": name,
            "content_hash": f"sha256:{name}-{uuid4()}",
            "captured_at": "2026-08-01T08:00:00Z",
        }
    )
    return str(row["id"])


def _memory(
    store: PostgresVNextStore,
    key: str,
    *,
    source_event_ids: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    row = store.create_memory(
        {
            "memory_key": f"{key}.{uuid4()}",
            "memory_type": "decision",
            "title": key,
            "canonical_text": f"Text of {key}.",
            "status": "active",
            "domain": "project",
            "sensitivity": "public",
            "source_event_ids": source_event_ids or [],
            "metadata_json": metadata or {},
            "value": {"text": key},
        }
    )
    return str(row["id"])


def _link(store: PostgresVNextStore, memory_id: str, source_id: str) -> None:
    store.create_provenance_link(
        {
            "target_type": "memory",
            "target_id": memory_id,
            "source_id": source_id,
            "evidence_role": "supports",
            "confidence": 0.9,
        }
    )


def test_batched_lookup_matches_the_one_source_lookup_on_postgres(migrated_database_urls) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    other_user_id = uuid4()
    ids: dict[str, str] = {}

    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"batched-lookup-{user_id}@example.invalid", "Batched lookup")
        store = PostgresVNextStore(conn)
        a, b, c = (_source(store, name) for name in ("a", "b", "c"))
        ids.update({"a": a, "b": b, "c": c})

        ids["by_link"] = _memory(store, "by-link")
        _link(store, ids["by_link"], a)
        ids["by_event"] = _memory(store, "by-event", source_event_ids=[a])
        ids["by_meta_id"] = _memory(store, "by-meta-id", metadata={"source_id": a})
        ids["by_meta_ref"] = _memory(store, "by-meta-ref", metadata={"source_ref": f"source:{a}"})
        ids["by_meta_ids"] = _memory(store, "by-meta-ids", metadata={"source_ids": [a]})
        ids["by_meta_refs"] = _memory(store, "by-meta-refs", metadata={"source_refs": [f"source:{a}"]})
        ids["by_meta_references"] = _memory(store, "by-meta-references", metadata={"source_references": [a]})
        ids["by_meta_selected"] = _memory(store, "by-meta-selected", metadata={"selected_source_ids": [a]})
        ids["two_sources"] = _memory(store, "two-sources")
        _link(store, ids["two_sources"], a)
        _link(store, ids["two_sources"], b)
        ids["deleted"] = _memory(store, "deleted")
        _link(store, ids["deleted"], a)
        ids["unrelated"] = _memory(store, "unrelated")

        # Seven references to C with identical timestamps, so the per-source cap
        # and the id tie-break both decide which three come back.
        for index in range(7):
            memory_id = _memory(store, f"capped-{index}")
            _link(store, memory_id, c)
            ids[f"capped_{index}"] = memory_id
        tied_at = datetime(2026, 8, 3, tzinfo=UTC)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET deleted_at = clock_timestamp() WHERE id = %s::uuid",
                (ids["deleted"],),
            )
            for index in range(7):
                cur.execute(
                    "UPDATE memories SET created_at = %s, updated_at = %s WHERE id = %s::uuid",
                    (tied_at, tied_at, ids[f"capped_{index}"]),
                )

    with user_connection(app_url, other_user_id) as conn:
        ContinuityStore(conn).create_user(
            other_user_id, f"batched-lookup-other-{other_user_id}@example.invalid", "Other"
        )
        other = PostgresVNextStore(conn)
        ids["other_user"] = _memory(other, "other-user", metadata={"source_id": ids["a"]}, source_event_ids=[ids["a"]])

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        no_such_source = str(uuid4())
        asked = [ids["a"], ids["b"], ids["c"], no_such_source]
        for cap in (50, 3):
            batched = store.list_memories_referencing_sources(asked, limit_per_source=cap)
            assert list(batched) == asked
            for source_id in asked:
                assert batched[source_id] == store.list_memories_referencing_source(
                    source_id=source_id, limit=cap
                ), (source_id, cap)

        wide = store.list_memories_referencing_sources(asked, limit_per_source=50)
        # The control: the fixture really exercises every kind the query reads.
        assert {str(row["id"]) for row in wide[ids["a"]]} == {
            ids["by_link"],
            ids["by_event"],
            ids["by_meta_id"],
            ids["by_meta_ref"],
            ids["by_meta_ids"],
            ids["by_meta_refs"],
            ids["by_meta_references"],
            ids["by_meta_selected"],
            ids["two_sources"],
        }
        assert [str(row["id"]) for row in wide[ids["b"]]] == [ids["two_sources"]]
        assert wide[no_such_source] == []
        assert len(wide[ids["c"]]) == 7
        assert len(store.list_memories_referencing_sources([ids["c"]], limit_per_source=3)[ids["c"]]) == 3
        assert "ref_source_id" not in wide[ids["a"]][0]
