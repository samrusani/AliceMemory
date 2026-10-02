"""The SQLite store reads the clock once for each write that sets two times together.

``memories`` has the check ``last_seen_at >= first_seen_at``. ``create_memory`` took ``first_seen_at`` and
``last_seen_at`` from two separate readings of the wall clock, and two back-to-back readings can go backwards
(57 times in 198 million pairs on one machine), so a write that landed on a step back failed on the check with
``IntegrityError``, in v0.20.0 and on main before this change. ``update_memory`` took ``updated_at`` and, for an
archive, ``deleted_at`` from two readings as well, which could leave a memory deleted before it was updated.

Every test names, in its docstring, the change to the code that must fail it. The clock is injected, nothing here
waits for it to step back, and nothing reaches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService
from alicebot_api.vnext_stores.sqlite import primitives
from tests.unit.test_expired_memories_everywhere import _memory_store

BASE = datetime(2026, 10, 2, 12, 0, 0, 500_000, tzinfo=UTC)


class _BackwardsClock(datetime):
    """``datetime`` whose ``now()`` is one microsecond earlier at every reading than at the one before."""

    readings = 0

    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        cls.readings += 1
        moment = BASE - timedelta(microseconds=cls.readings)
        return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)


@pytest.fixture
def backwards_clock(monkeypatch: pytest.MonkeyPatch) -> type[_BackwardsClock]:
    """The store's clock, going backwards at every reading."""

    _BackwardsClock.readings = 0
    monkeypatch.setattr(primitives, "datetime", _BackwardsClock)
    return _BackwardsClock


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_memory(**overrides: object) -> dict[str, object]:
    memory: dict[str, object] = {
        "memory_key": "clock.test",
        "value": {"text": "a memory"},
        "status": "active",
        "memory_type": "semantic",
        "title": "a memory",
        "canonical_text": "a memory",
        "domain": "project",
        "sensitivity": "private",
    }
    memory.update(overrides)
    return memory


def test_a_memory_written_while_the_clock_goes_backwards_has_one_time_for_all_four_columns(
    backwards_clock: type[_BackwardsClock],
) -> None:
    """``create_memory`` reads the clock once, so the seen times are equal and the check cannot fail.

    Every reading is earlier than the one before it. A write that read the clock twice for the two seen times
    stored ``last_seen_at`` earlier than ``first_seen_at`` and failed on ``memories_seen_range_check``. The four
    times of the row are equal, which is what one reading gives.

    Mutations, each one alone, in ``create_memory``: take ``last_seen_at`` from a new reading
    (``_iso_or_none(memory.get("last_seen_at")) or _utc_now_iso()``; the write raises ``IntegrityError`` on the
    check); take ``first_seen_at`` from a new reading (it differs from ``last_seen_at``); take ``created_at`` from a
    new reading (the four times differ). Both seen times from new readings is the code of v0.20.0, and it raises.
    """

    with _memory_store() as store:
        before = backwards_clock.readings
        row = store.create_memory(_new_memory())
        assert row["first_seen_at"] == row["last_seen_at"] == row["created_at"] == row["updated_at"]
        # the first reading the write made, not a later one
        assert row["first_seen_at"] == _stamp(BASE - timedelta(microseconds=before + 1))


def test_a_time_the_caller_gives_is_kept_and_only_the_missing_one_takes_the_reading(
    backwards_clock: type[_BackwardsClock],
) -> None:
    """A ``first_seen_at`` or ``last_seen_at`` in the memory is stored as given; a missing one is the one reading.

    A backup restore gives both times and must get them back. A caller that gives one gets the reading for the
    other. The given times are well before the injected clock.

    Mutations, each one alone, in ``create_memory``: store the reading whatever the caller gave for
    ``first_seen_at`` (the first assertion fails), or for ``last_seen_at`` (the second fails).
    """

    first, last = "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z"
    with _memory_store() as store:
        both = store.create_memory(_new_memory(memory_key="clock.both", first_seen_at=first, last_seen_at=last))
        assert (both["first_seen_at"], both["last_seen_at"]) == (first, last)
        before = backwards_clock.readings
        only_first = store.create_memory(_new_memory(memory_key="clock.first", first_seen_at=first))
        assert only_first["first_seen_at"] == first
        assert only_first["last_seen_at"] == _stamp(BASE - timedelta(microseconds=before + 1))
        assert only_first["last_seen_at"] == only_first["created_at"]


def test_a_memory_committed_through_the_commit_door_while_the_clock_goes_backwards_is_stored(
    backwards_clock: type[_BackwardsClock],
) -> None:
    """``alice_memory_commit`` stores a memory when the clock steps back during the write.

    The door writes the memory, its revision and its events, and each takes its own readings. The check is on the
    memory row only, so what must hold is that the memory is stored and its seen times are equal.

    Mutations, each one alone, in ``create_memory``: take ``last_seen_at`` from a new reading (the commit raises
    ``IntegrityError``), or ``first_seen_at`` (the two seen times differ).
    """

    identity = AgentIdentity(
        agent_id="hermes", agent_type="personal_assistant", permission_profile="trusted_local_agent", project_scope=()
    )
    with _memory_store() as store:
        request = MemoryCommitRequest(
            user_id=store.user_id,
            title="Coffee preference",
            canonical_text="Sam prefers coffee before noon.",
            domain="personal",
            sensitivity="private",
            confidence=0.95,
        )
        result = VNextMemoryCommitService(store).commit(identity=identity, request=request)
        assert result["status"] == "committed"
        memory = store.get_memory(str(result["memory"]["id"]))  # type: ignore[index]
        assert memory is not None
        assert memory["first_seen_at"] == memory["last_seen_at"]


def test_an_archive_takes_one_reading_for_updated_at_and_deleted_at(backwards_clock: type[_BackwardsClock]) -> None:
    """``update_memory`` sets ``updated_at`` and ``deleted_at`` of an archive from one reading.

    Under a clock that goes backwards, two readings left ``deleted_at`` earlier than ``updated_at``: the memory was
    deleted before the update that deleted it.

    Mutation: take ``deleted_at`` from a new reading in ``update_memory`` (``_utc_now_iso()`` in place of ``now``).
    """

    with _memory_store() as store:
        created = store.create_memory(_new_memory())
        # an archived row is hidden from ``get_memory``, so read the columns directly
        store.update_memory(memory_id=str(created["id"]), patch={"status": "archived"})
        updated_at, deleted_at = store.conn.execute(
            "SELECT updated_at, deleted_at FROM memories WHERE id = ?", (created["id"],)
        ).fetchone()
        assert deleted_at == updated_at
        assert updated_at < created["updated_at"]
