"""Which memories recall can return, in one place.

Recall, the context pack and the session brief read a memory only in the
statuses ``active`` and ``accepted``. Recall, the brief, ``alice_resume`` and
``alice_recent_decisions`` also skip a memory whose validity window has closed
(``valid_to`` earlier than now). The text of any other memory is never shown to
a reader, so it is not sent to the embeddings endpoint either, whether the door
is a write, reindex, consolidation or the roll-up pass: the endpoint is a third
party, and a vector made from a candidate, a forgotten memory or an expired one
cannot be recalled.

This module imports nothing from Alice, so the embedding door
(``vnext_embeddings``), the stores and the retrieval module can all read it
without an import cycle. It holds the status tuple, the Python form of the
expiry test, and the Postgres form of the same test. The SQLite form is
``_expiry_clause`` in ``vnext_stores/sqlite/query_predicates.py``, which recall
itself uses; a test pins every form to recall's own SQL.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TypeVar

_MemoryRow = TypeVar("_MemoryRow", bound=Mapping[str, object])

# The statuses recall, the context pack, the session brief and the doctor read.
# Mirrored in each store's ``_MEMORY_SEARCHABLE_STATUSES_SQL`` (a test pins the
# three equal).
MEMORY_SEARCHABLE_STATUSES = ("active", "accepted")


def postgres_unexpired_sql(prefix: str = "") -> str:
    """The Postgres test for "the validity window has not closed", for a column prefix.

    ``prefix`` is a table alias with its dot, for a statement that joins
    (``"m."``). Every Postgres statement that has to leave out an expired memory
    takes its text from here, so there is one comparison to change.
    """

    return f"({prefix}valid_to IS NULL OR {prefix}valid_to >= clock_timestamp())"


# The Postgres test for "the validity window has not closed". Recall writes the
# same text next to a flag that lifts it; reindex, the backfill, the resume
# reads, the roll-up reads and consolidation use it as it is. A test fails if
# recall's statements stop containing it.
POSTGRES_UNEXPIRED_SQL = postgres_unexpired_sql()


def parse_valid_to(value: object) -> datetime | None:
    """A ``valid_to`` value as an aware UTC datetime, or None when there is none.

    A row has no validity end when ``valid_to`` is empty. A value that is
    neither a datetime nor an ISO-8601 string is read as no end too: recall's
    SQL cannot order it either, and the stores write only the two forms.
    """

    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        # An offset that moves the moment past year 9999 is the far future, so
        # the row has not ended; one that moves it before year 1 has ended.
        return None if moment.year > 1 else datetime.min.replace(tzinfo=UTC)


def valid_to_has_passed(value: object, *, now: datetime | None = None) -> bool:
    """True when ``value`` is a validity end earlier than ``now`` (default: the present)."""

    moment = parse_valid_to(value)
    if moment is None:
        return False
    return moment < (now if now is not None else datetime.now(UTC))


def memory_window_is_open(memory: Mapping[str, object], *, now: datetime | None = None) -> bool:
    """True when the row's validity window has not closed (no ``valid_to``, or one that is not past).

    The Python form of ``_expiry_clause`` and ``POSTGRES_UNEXPIRED_SQL``. A caller
    that reads a row by id, or from a store that cannot apply the SQL test, uses
    this one, so a row is never judged by a second comparison.
    """

    return not valid_to_has_passed(memory.get("valid_to"), now=now)


def drop_expired_memories(memories: Sequence[_MemoryRow], *, now: datetime | None = None) -> list[_MemoryRow]:
    """The rows whose validity window has not closed, in their order."""

    moment = now if now is not None else datetime.now(UTC)
    return [memory for memory in memories if memory_window_is_open(memory, now=moment)]


def memory_is_recall_visible(memory: Mapping[str, object], *, now: datetime | None = None) -> bool:
    """True when recall can return this row: a searchable status and an open validity window.

    A row with no status is not visible: the caller did not say what it is, so
    its text is not sent anywhere.
    """

    status = memory.get("status")
    if not isinstance(status, str) or status not in MEMORY_SEARCHABLE_STATUSES:
        return False
    return memory_window_is_open(memory, now=now)


__all__ = [
    "MEMORY_SEARCHABLE_STATUSES",
    "POSTGRES_UNEXPIRED_SQL",
    "drop_expired_memories",
    "memory_is_recall_visible",
    "memory_window_is_open",
    "parse_valid_to",
    "postgres_unexpired_sql",
    "valid_to_has_passed",
]
