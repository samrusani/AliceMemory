"""Which sources a caller may read, as one SQL predicate that every source read shares.

``search_source_chunks`` (the passage stage), ``search_sources`` (the title and recency list) and the mask of
the vector stage (``readable_source_ids``) all ask the same question of a source row: is it live, is its domain
and its sensitivity among those the caller was given, and does it fall inside the project, people and time scope.
Each used to write that question out in its own statement. They call ``readable_source_clause`` now, so a
change to one of the rules is a change to all three, and ``tests/unit/test_source_readable_parity.py`` checks
the three against each other over a grid of labels and scopes.

A function of this module, not a method of the store class, so the shape of the class is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from alicebot_api.sqlite_schema import source_chunk_vectors_table_exists

if TYPE_CHECKING:
    from alicebot_api.sqlite_store import SQLiteVNextStore

#: The time a source is placed at for the scope window, as the expression over a source row with this prefix.
_EVENT_TIME = (
    "COALESCE(julianday({p}source_created_at), "
    "julianday(json_extract({p}metadata_json, '$.session_date')), "
    "julianday(json_extract({p}metadata_json, '$.event_date')), "
    "julianday(json_extract({p}metadata_json, '$.date')), "
    "julianday({p}captured_at))"
)


def readable_source_clause(
    store: "SQLiteVNextStore",
    *,
    prefix: str,
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
    scope_projects: tuple[str, ...] = (),
    scope_people: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
) -> tuple[str, list[object]]:
    """The conditions on a ``sources`` row that make it readable to a caller, and their parameters.

    ``prefix`` is the alias of the sources table in the statement with its dot (``"s."``), or empty when the
    statement reads ``sources`` unaliased. The text begins with `` AND `` so it can follow a ``WHERE`` that
    already holds the user predicate, and the parameters come in the order the placeholders appear: domains,
    sensitivities, then the scope. The user predicate stays with the caller.

    The rules: not deleted, replaced or scrubbed (``deleted_at`` is null); the domain is one of ``domains`` or
    ``unknown``; the sensitivity is one of ``sensitivity_allowed``; the project scope, the people and the time
    window hold (``global_excluded_domains`` is stated empty, as the source reads have always stated it: a
    source is held back in Python by the brief, not in these reads).
    """

    domain_sql, domain_params = store._domain_clause(domains, prefix=prefix)
    sensitivity_sql, sensitivity_params = store._sensitivity_clause(sensitivity_allowed, prefix=prefix)
    scope_sql, scope_params = store._metadata_scope_clause(
        metadata_expression=f"{prefix}metadata_json",
        persisted_source_envelope=True,
        scope_projects=scope_projects,
        scope_people=scope_people,
        event_time_expression=_EVENT_TIME.format(p=prefix),
        scope_window_start=scope_window_start,
        scope_window_end=scope_window_end,
        global_excluded_domains=(),
    )
    clause = f" AND {prefix}deleted_at IS NULL{domain_sql}{sensitivity_sql}{scope_sql}"
    return clause, [*domain_params, *sensitivity_params, *scope_params]


def readable_source_ids(
    store: "SQLiteVNextStore",
    *,
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
    scope_projects: tuple[str, ...] = (),
    scope_people: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
    with_vectors: bool = False,
) -> list[str]:
    """Ids of the sources the caller may read, in id order.

    With ``with_vectors`` only the sources that hold at least one chunk vector are listed (an index lookup per
    source), which is all the mask of the vector stage needs. Without it, every readable source is listed: the
    form the parity test compares with the two search reads.
    """

    if with_vectors and not source_chunk_vectors_table_exists(store.conn):
        return []
    clause, params = readable_source_clause(
        store,
        prefix="s.",
        domains=domains,
        sensitivity_allowed=sensitivity_allowed,
        scope_projects=scope_projects,
        scope_people=scope_people,
        scope_window_start=scope_window_start,
        scope_window_end=scope_window_end,
    )
    held = (
        " AND EXISTS (SELECT 1 FROM source_chunk_embeddings e WHERE e.user_id = s.user_id AND e.source_id = s.id)"
        if with_vectors
        else ""
    )
    rows = store._fetch_all(
        f"""
        SELECT s.id
        FROM sources s
        WHERE s.user_id = ?{clause}{held}
        ORDER BY s.id ASC
        """,  # nosec B608 - the clause is built from placeholders and constants
        (store.user_id, *params),
    )
    return [str(row["id"]) for row in rows]


__all__ = ["readable_source_clause", "readable_source_ids"]
