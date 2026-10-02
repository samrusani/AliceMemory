"""Live PostgreSQL: the reads that leave out an expired memory do it before ``LIMIT``.

The unit tests pin the Postgres statements as text. Only a live database proves they parse,
that the shared test (``POSTGRES_UNEXPIRED_SQL`` and its ``m.`` alias form for the join) means
what recall's SQL means, and that it is applied before the limit, so an expired memory does not
use up a place that an open one should take.

Three active memories are made, the newest with a ``valid_to`` a second in the past and a
``valid_from`` before it. Each read is asked for one row.

Mutations that must fail this test, each one alone: drop ``{expiry_sql}`` from ``list_memories`` or
``count_memories``; drop ``AND {POSTGRES_UNEXPIRED_SQL}`` from the roll-up input list, the roll-up
input count or the accepted-card lookup; drop the ``m.`` alias test from
``list_resume_memory_events``; flip the comparison in ``postgres_unexpired_sql`` to ``<``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore

SENSITIVITY = ["public", "internal", "private", "unknown"]
CARD_KIND = "rollup_card"


def _memory(
    store: PostgresVNextStore,
    label: str,
    *,
    valid_to: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    row = store.create_memory(
        {
            "memory_key": f"expiry.{label}.{uuid4()}",
            "memory_type": "decision",
            "title": f"{label} memory",
            "canonical_text": f"Text of {label}.",
            "status": "active",
            "domain": "project",
            "sensitivity": "private",
            "value": {"text": label},
            "valid_from": datetime.now(UTC) - timedelta(days=2),
            "valid_to": valid_to,
            "metadata_json": metadata or {},
        }
    )
    return str(row["id"])


def test_the_postgres_reads_leave_out_an_expired_memory_before_the_limit(migrated_database_urls) -> None:
    """The list, the count, the resume events and the roll-up input list and count skip an expired memory, before ``LIMIT``.

    Three active memories are made and the newest is expired. Asked for one row, each read must return the
    newest open memory, and each count must be 2 (the management default, which keeps ``include_expired=True``,
    still sees all 3).

    Mutations, each one alone: drop ``{expiry_sql}`` from ``list_memories`` (the one-row list returns the expired
    memory); drop ``{expiry_sql}`` from ``count_memories`` (the count is 3); drop ``AND {POSTGRES_UNEXPIRED_SQL}``
    from ``list_rollup_input_memories`` (the one-row list returns the expired memory) or from
    ``count_rollup_input_memories`` (the count is 3); drop the ``m.`` alias test from ``list_resume_memory_events``
    in ``vnext_store.py`` (the one-row event list names the expired memory); flip the comparison in
    ``postgres_unexpired_sql`` to ``<`` (the memory with a future ``valid_to`` is left out and the expired one is
    kept).
    """

    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    gone_at = datetime.now(UTC) - timedelta(seconds=1)
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"expiry-{user_id}@example.invalid", "Expiry")
        store = PostgresVNextStore(conn)
        _memory(store, "old")
        middle = _memory(store, "middle", valid_to=datetime.now(UTC) + timedelta(days=3650))
        gone = _memory(store, "gone", valid_to=gone_at)

        # the management default still lists and counts the expired memory
        newest = store.list_memories(status="active", order_by_created_at=True, limit=1)
        assert [str(row["id"]) for row in newest] == [gone]
        assert store.count_memories(status="active") == 3

        newest_open = store.list_memories(status="active", order_by_created_at=True, limit=1, include_expired=False)
        assert [str(row["id"]) for row in newest_open] == [middle]
        assert store.count_memories(status="active", include_expired=False) == 2

        events = store.list_resume_memory_events(statuses=("active",), limit=1)
        assert [str(event["target_id"]) for event in events] == [middle]
        every_event = store.list_resume_memory_events(statuses=("active",), limit=20)
        assert gone not in {str(event["target_id"]) for event in every_event}

        arguments = {
            "domains": None,
            "sensitivity_allowed": SENSITIVITY,
            "excluded_candidate_kind": CARD_KIND,
        }
        assert [str(row["id"]) for row in store.list_rollup_input_memories(limit=1, **arguments)] == [middle]
        assert store.count_rollup_input_memories(**arguments) == 2


def test_the_postgres_accepted_card_lookup_picks_the_open_card_when_the_newest_has_expired(
    migrated_database_urls,
) -> None:
    """``list_accepted_rollup_cards`` ranks only unexpired cards, so an older open card is returned when the newest has expired.

    Two cards share a roll-up key and the newer one is expired, so the older one is the accepted card. When the
    older card is expired too, there is no accepted card.

    Mutations, each one alone: drop ``AND {POSTGRES_UNEXPIRED_SQL}`` from the ``DISTINCT ON`` query of
    ``list_accepted_rollup_cards`` (the lookup returns the expired newer card, then the expired older one);
    flip the comparison in ``postgres_unexpired_sql`` to ``<`` (the expired newer card is returned).
    """

    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    key = f"topic:games:{uuid4()}"
    metadata = {"candidate_kind": CARD_KIND, "rollup_key": key}
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"cards-{user_id}@example.invalid", "Cards")
        store = PostgresVNextStore(conn)
        older = _memory(store, "older-card", metadata=metadata)
        _memory(store, "newer-card", valid_to=datetime.now(UTC) - timedelta(seconds=1), metadata=metadata)

        def accepted() -> list[str]:
            rows = store.list_accepted_rollup_cards(
                rollup_keys=(key,),
                domains=None,
                sensitivity_allowed=SENSITIVITY,
                candidate_kind=CARD_KIND,
                limit=5,
            )
            return [str(row["id"]) for row in rows]

        assert accepted() == [older]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET valid_to = clock_timestamp() - interval '1 second' WHERE id = %s::uuid",
                (older,),
            )
        assert accepted() == []
