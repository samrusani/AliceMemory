"""SQLite query predicates and deterministic project-scope helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import cast

from alicebot_api.vnext_project_scope import (
    GLOBAL_PROJECT_MARKER,
    is_alice_project_id,
    project_scope_identity,
    resolve_project_scope,
    resolve_source_metadata_project_scope,
)
from alicebot_api.vnext_stores.retrieval_common import (
    FTS_QUERY_STOPWORDS as _FTS_QUERY_STOPWORDS,
    fts_fallback_tokens,
)
from alicebot_api.vnext_stores.sqlite.primitives import _iso_or_none, _utc_now_iso

def _project_scope_value_sqlite(value: object) -> str | None:
    identity = project_scope_identity(value)
    return identity[0] if len(identity) == 1 else None


def _project_scope_identity_json_sqlite(
    metadata_json: object,
    direct_project_id: object,
) -> str:
    if isinstance(metadata_json, Mapping):
        metadata = dict(metadata_json)
    elif isinstance(metadata_json, str):
        try:
            decoded = json.loads(metadata_json)
        except (TypeError, ValueError):
            decoded = {}
        metadata = decoded if isinstance(decoded, dict) else {}
    else:
        metadata = {}
    identity = resolve_project_scope(
        {
            "metadata_json": metadata,
            "project_id": direct_project_id,
        }
    ).identity
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _source_project_scope_identity_json_sqlite(metadata_json: object) -> str:
    """Resolve the complete persisted-source scope envelope for SQLite."""

    if isinstance(metadata_json, Mapping):
        metadata = dict(metadata_json)
    elif isinstance(metadata_json, str):
        try:
            decoded = json.loads(metadata_json)
        except (TypeError, ValueError):
            decoded = {}
        metadata = decoded if isinstance(decoded, dict) else {}
    else:
        metadata = {}
    identity = resolve_source_metadata_project_scope(metadata).identity
    return json.dumps(identity, ensure_ascii=False, separators=(",", ":"))


def _ensure_project_scope_identity_sqlite(conn: sqlite3.Connection) -> None:
    """Install the deterministic project identity functions per connection."""

    cursor = conn.execute(
        """
        SELECT count(*)
        FROM pragma_function_list
        WHERE name IN (
          'alice_project_scope_value',
          'alice_project_scope_identity',
          'alice_source_project_scope_identity'
        )
        """
    )
    try:
        row = cursor.fetchone()
        if row is not None:
            count = next(iter(row.values())) if isinstance(row, Mapping) else row[0]
            if int(count) == 3:
                return
    finally:
        cursor.close()
    conn.create_function(
        "alice_project_scope_value",
        1,
        _project_scope_value_sqlite,
        deterministic=True,
    )
    conn.create_function(
        "alice_project_scope_identity",
        2,
        _project_scope_identity_json_sqlite,
        deterministic=True,
    )
    conn.create_function(
        "alice_source_project_scope_identity",
        1,
        _source_project_scope_identity_json_sqlite,
        deterministic=True,
    )


#: ``AS MATERIALIZED`` for the labelled common table expression of the single-scan
#: partition reads (``list_memories_view_partitions``, ``list_open_loops_view_partitions``).
#: The expression is read twice, once per label. SQLite does not share a common table
#: expression between two references on its own: measured on 3.49.1, a partition read over
#: 30 memories called the identity function 68 times (twice per row, and eight more), and
#: with the hint exactly 30 times (once per row). The hint needs SQLite 3.35 (2021). An
#: older SQLite reads the same rows through the same SQL without it, twice as slowly.
CTE_MATERIALIZED_HINT = " MATERIALIZED" if sqlite3.sqlite_version_info >= (3, 35, 0) else ""


# ---------------------------------------------------------------------------
# The project view in SQL (spec 6.1)
#
# A request tuple may hold the reserved marker ``~global``. A row is in the view
# when its identity set (the JSON array the identity functions return) contains a
# requested id, or, when the marker is requested, contains no Alice project id.
# The test is one aggregate over ``json_each`` of one identity-function call, so
# a row costs one Python call, not two. In front of it sits a native prefilter
# made of ``LIKE`` and ``instr`` over the raw text, which decides most rows
# without calling Python and never rules out a row the exact test would admit.
# ---------------------------------------------------------------------------

#: ``GLOB`` pattern of an Alice project id. Identity values are already folded to
#: lowercase, so a case-sensitive ``GLOB`` is exact.
_ALICE_ID_GLOB = "prj_" + "[0-9a-f]" * 16


def _sql_has_alice_id(value_expression: str) -> str:
    return f"(length({value_expression}) = 20 AND {value_expression} GLOB '{_ALICE_ID_GLOB}')"


def _split_view_request(projects: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    """The requested identifiers without the marker, and whether the marker was asked for."""

    identity = project_scope_identity(projects)
    wants_global = GLOBAL_PROJECT_MARKER in identity
    return tuple(value for value in identity if value != GLOBAL_PROJECT_MARKER), wants_global


def _no_alice_id_in_text(text_expressions: tuple[str, ...]) -> str:
    """Native proof that a row holds no Alice project id.

    No ``prj_`` anywhere in the raw text of any column the identity reads (``LIKE``
    folds ASCII case, as the identity does) and no ``\\u`` escape that could spell
    one. A row that passes holds no Alice id, whatever the exact test would say.
    """

    parts: list[str] = []
    for expression in text_expressions:
        parts.append(f"COALESCE({expression}, '') NOT LIKE '%prj\\_%' ESCAPE '\\'")
        parts.append(f"instr(COALESCE({expression}, ''), '\\u') = 0")
    return "(" + " AND ".join(parts) + ")"


def _maybe_holds_requested_id(text_expressions: tuple[str, ...], ids: tuple[str, ...]) -> str:
    """Native necessary condition for a row to hold one of ``ids`` (all Alice ids).

    The id appears in the raw text of some column the identity reads, in any ASCII
    case, or a ``\\u`` escape could spell it. Placeholders for ``ids`` are
    repeated for each text expression, in order.
    """

    alternatives: list[str] = []
    for expression in text_expressions:
        for _identifier in ids:
            alternatives.append(f"COALESCE({expression}, '') LIKE '%' || ? || '%' ESCAPE '\\'")
        alternatives.append(f"instr(COALESCE({expression}, ''), '\\u') > 0")
    return "(" + " OR ".join(alternatives) + ")"


def _ids_are_alice_ids(ids: tuple[str, ...]) -> bool:
    return bool(ids) and all(is_alice_project_id(identifier) for identifier in ids)


def _view_membership_sql(
    *,
    placeholders: Callable[[list[str]], str],
    scope_expression: str,
    ids: tuple[str, ...],
    wants_global: bool,
    domain_expression: str | None,
    global_excluded_domains: tuple[str, ...],
    text_expressions: tuple[str, ...],
    partition: bool,
) -> tuple[str, list[object]]:
    """The exact view test as one aggregate over one identity-function call.

    With ``partition=False`` the SQL is a predicate (true when the row is in the
    view). With ``partition=True`` it is a value: 1 for a row of a requested
    project, 0 for a global row, NULL for a row outside the view. A row of a
    requested project is never global, so the value is unambiguous.
    """

    params: list[object] = []
    excluded = tuple(global_excluded_domains) if wants_global else ()
    if excluded and domain_expression is None:
        raise ValueError("a view that leaves out global domains needs the domain expression of the table")

    def excluded_sql() -> str:
        params.extend(excluded)
        return f"{domain_expression} IN ({placeholders(list(excluded))})"

    when_hit = ""
    hit_value = "1"
    if ids:
        params.extend(ids)
        when_hit = (
            f"WHEN COALESCE(MAX(CAST(scoped_project.value AS TEXT) IN ({placeholders(list(ids))})), 0) = 1 "
            f"THEN {hit_value} "
        )
    when_global = ""
    if wants_global:
        if partition:
            if excluded:
                inner = f"CASE WHEN {excluded_sql()} THEN NULL ELSE 0 END"
            else:
                inner = "0"
        elif excluded:
            inner = f"CASE WHEN {excluded_sql()} THEN 0 ELSE 1 END"
        else:
            inner = "1"
        when_global = (
            f"WHEN COALESCE(MAX({_sql_has_alice_id('CAST(scoped_project.value AS TEXT)')}), 0) = 0 "
            f"THEN {inner} "
        )
    fallback = "NULL" if partition else "0"
    sql = (
        "(SELECT CASE "
        + when_hit
        + when_global
        + f"ELSE {fallback} END "
        + f"FROM json_each({scope_expression}) AS scoped_project)"
    )
    return sql, params


_EXCLUSION_NOT_STATED_MESSAGE = (
    "a read that asks for global rows must state which global domains it leaves out "
    "(pass an empty tuple to leave none out)"
)


def _stated_exclusion(exclude_global_domains: Sequence[str] | None) -> tuple[str, ...] | None:
    """The exclusion a reader was given, in a stable order, or ``None`` when it was not stated.

    The readers that can take a request tuple holding the marker default the
    exclusion to ``None``, not to an empty tuple, so that "leave nothing out" has to be
    written at the call site. A reader that is handed the marker and no statement
    raises (``_project_view_sql``) instead of quietly holding nothing back.
    """

    return None if exclude_global_domains is None else tuple(sorted(exclude_global_domains))


def _project_view_sql(
    *,
    placeholders: Callable[[list[str]], str],
    projects: tuple[str, ...],
    scope_expression: str,
    text_expressions: tuple[str, ...],
    domain_expression: str | None,
    global_excluded_domains: tuple[str, ...] | None,
) -> tuple[str, list[object]]:
    """The ``AND ...`` clause for a request tuple, or ``("", [])`` when it fences nothing.

    A tuple with no marker and any name that is not an Alice id keeps the clause
    v0.20.0 built, character for character, so a caller that never meets a view
    runs the SQL it always ran. A tuple of Alice ids alone gets the native
    prefilter in front of the same test. A tuple with the marker gets the view
    test, with a native fast path for a row that provably holds no Alice id.
    """

    ids, wants_global = _split_view_request(projects)
    if not ids and not wants_global:
        return "", []
    if wants_global and global_excluded_domains is None:
        raise ValueError(_EXCLUSION_NOT_STATED_MESSAGE)
    if not wants_global and not _ids_are_alice_ids(ids):
        values: list[object] = list(ids)
        return (
            " AND EXISTS (SELECT 1 FROM json_each("
            f"{scope_expression}"
            ") AS scoped_project WHERE CAST(scoped_project.value AS TEXT) "
            f"IN ({placeholders(list(ids))}))"
        ), values
    params: list[object] = []
    excluded = tuple(global_excluded_domains or ()) if wants_global else ()
    if wants_global:
        # Native fast path: a row that holds no Alice id is global, and is in the
        # view unless its domain is held back.
        fast = _no_alice_id_in_text(text_expressions)
        if excluded:
            if domain_expression is None:
                raise ValueError("a view that leaves out global domains needs the domain expression of the table")
            params.extend(excluded)
            fast = f"({fast} AND {domain_expression} NOT IN ({placeholders(list(excluded))}))"
        exact, exact_params = _view_membership_sql(
            placeholders=placeholders,
            scope_expression=scope_expression,
            ids=ids,
            wants_global=True,
            domain_expression=domain_expression,
            global_excluded_domains=excluded,
            text_expressions=text_expressions,
            partition=False,
        )
        params.extend(exact_params)
        return f" AND ({fast} OR {exact} = 1)", params
    # Alice ids only: prefilter, then the exact test.
    for _expression in text_expressions:
        params.extend(_escape_like_literal(identifier) for identifier in ids)
    prefilter = _maybe_holds_requested_id(text_expressions, ids)
    exact, exact_params = _view_membership_sql(
        placeholders=placeholders,
        scope_expression=scope_expression,
        ids=ids,
        wants_global=False,
        domain_expression=None,
        global_excluded_domains=(),
        text_expressions=text_expressions,
        partition=False,
    )
    params.extend(exact_params)
    return f" AND ({prefilter} AND {exact} = 1)", params


def _project_view_partition_sql(
    *,
    placeholders: Callable[[list[str]], str],
    projects: tuple[str, ...],
    scope_expression: str,
    text_expressions: tuple[str, ...],
    domain_expression: str,
    global_excluded_domains: tuple[str, ...],
) -> tuple[str, list[object]]:
    """A value per row for the single-scan fill: 1 project, 0 global, NULL outside the view.

    ``projects`` is the request tuple of the project view: Alice ids and the
    marker. The native fast path labels a row that provably holds no Alice id as
    global without calling Python. Every other row goes through the exact test.
    """

    ids, wants_global = _split_view_request(projects)
    if not wants_global or not _ids_are_alice_ids(ids):
        raise ValueError("a partition needs Alice project ids and the global marker")
    excluded = tuple(global_excluded_domains)
    params: list[object] = []
    fast = _no_alice_id_in_text(text_expressions)
    if excluded:
        params.extend(excluded)
        fast = f"({fast} AND {domain_expression} NOT IN ({placeholders(list(excluded))}))"
    exact, exact_params = _view_membership_sql(
        placeholders=placeholders,
        scope_expression=scope_expression,
        ids=ids,
        wants_global=True,
        domain_expression=domain_expression,
        global_excluded_domains=excluded,
        text_expressions=text_expressions,
        partition=True,
    )
    params.extend(exact_params)
    return f"CASE WHEN {fast} THEN 0 ELSE {exact} END", params


def _escape_like_literal(value: str) -> str:
    """Escape one literal substring for SQL LIKE with backslash ESCAPE."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sqlite_ascii_literal_contains_sql(value_expression: str) -> str:
    """Build SQLite's ASCII-folded, literal substring predicate."""

    return f"lower({value_expression}) LIKE '%' || lower(?) || '%' ESCAPE '\\'"


# SQLite FTS5 has no built-in English stopword filtering. Keep the
# strict MATCH builder and OR-fallback tied to retrieval_common so
# both backends agree on content-bearing tokens and safe syntax.

def _fts_match_expression(query: str) -> str | None:
    """Translate a websearch-style query into a safe FTS5 MATCH expression.

    Quoted phrases are preserved as FTS5 phrase queries; bare terms are
    AND-ed after English stopwords are dropped, mirroring what
    ``websearch_to_tsquery('english', ...)`` does on the Postgres path (a
    query made only of stopwords matches nothing there too). Every token is
    individually double-quoted so FTS5 syntax metacharacters
    (``: * ^ ( ) - NEAR AND OR NOT``) cannot produce a parse error or
    operator injection.
    """
    normalized = " ".join(str(query).split())
    if not normalized:
        return None
    parts: list[str] = []
    for phrase in re.findall(r'"([^"]*)"', normalized):
        words = re.findall(r"\w+", phrase)
        if words:
            parts.append('"' + " ".join(words) + '"')
    remainder = re.sub(r'"[^"]*"', " ", normalized)
    for term in re.findall(r"\w+", remainder):
        if term.casefold() in _FTS_QUERY_STOPWORDS:
            continue
        parts.append(f'"{term}"')
    if not parts:
        return None
    return " AND ".join(parts)


def _fts_match_any_expression(query: str) -> str | None:
    """OR-of-terms FTS5 MATCH expression for the ``match_any`` fallback pass.

    Same sanitization discipline as the strict path: only ``\\w+`` tokens
    survive and each is individually double-quoted, so FTS5 metacharacters
    cannot produce a parse error or operator injection; stopwords are
    dropped so bare question words do not match everything.
    """
    tokens = fts_fallback_tokens(query)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


# These helpers remain descriptor-compatible with their original class
# positions; the facade grafts them directly without wrappers.

@staticmethod  # type: ignore[misc]  # intentionally graft the descriptor into the facade
def _placeholders(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


def _domain_clause(self, domains: list[str] | None, *, prefix: str = "") -> tuple[str, list[object]]:
    if domains is None:
        return "", []
    clause = f" AND ({prefix}domain IN ({self._placeholders(domains)}) OR {prefix}domain = 'unknown')"
    return clause, list(domains)


def _sensitivity_clause(
    self,
    sensitivity_allowed: list[str] | None,
    *,
    prefix: str = "",
) -> tuple[str, list[object]]:
    if sensitivity_allowed is None:
        return "", []
    clause = f" AND {prefix}sensitivity IN ({self._placeholders(sensitivity_allowed)})"
    return clause, list(sensitivity_allowed)


def _memory_type_clause(
    self,
    memory_types: tuple[str, ...],
    *,
    prefix: str = "",
) -> tuple[str, list[object]]:
    if not memory_types:
        return "", []
    values = list(memory_types)
    clause = f" AND {prefix}memory_type IN ({self._placeholders(values)})"
    return clause, list(values)


def _project_clause(
    self,
    projects: tuple[str, ...],
    *,
    prefix: str = "",
    global_excluded_domains: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """The project fence of a memory read, for explicit names and for a project view.

    ``projects`` may hold the reserved marker (spec 6.1), which asks for memories
    whose scope holds no Alice project id. ``global_excluded_domains`` leaves out
    global memories in those domains and has no effect without the marker. With the
    marker it must be stated, an empty tuple when nothing is left out: ``None``
    raises, so a reader that forgot the choice cannot hold nothing back by default.
    """

    return _project_view_sql(
        placeholders=self._placeholders,
        projects=projects,
        scope_expression=f"alice_project_scope_identity({prefix}metadata_json, {prefix}project_id)",
        text_expressions=(f"{prefix}metadata_json", f"{prefix}project_id"),
        domain_expression=f"{prefix}domain",
        global_excluded_domains=global_excluded_domains,
    )


def _created_by_clause(
    self,
    created_by_agent_ids: tuple[str, ...],
    *,
    prefix: str = "",
) -> tuple[str, list[object]]:
    if not created_by_agent_ids:
        return "", []
    values = list(created_by_agent_ids)
    clause = f" AND {prefix}created_by_agent_id IN ({self._placeholders(values)})"
    return clause, list(values)


@staticmethod  # type: ignore[misc]  # intentionally graft the descriptor into the facade
def _run_clause(run_id: str | None, *, prefix: str = "") -> tuple[str, list[object]]:
    if run_id is None:
        return "", []
    return f" AND {prefix}run_id = ?", [run_id]


@staticmethod  # type: ignore[misc]  # intentionally graft the descriptor into the facade
def _expiry_clause(include_expired: bool, *, prefix: str = "") -> tuple[str, list[object]]:
    """Exclude memories whose validity window has closed (valid_to < now)."""
    if include_expired:
        return "", []
    clause = f" AND ({prefix}valid_to IS NULL OR {prefix}valid_to >= ?)"
    return clause, [_utc_now_iso()]


def _retrieval_scope_clause(
    self,
    *,
    scope_thread_id: str | None = None,
    scope_task_id: str | None = None,
    scope_people: tuple[str, ...] = (),
    scope_person_memory_ids: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
    prefix: str = "",
) -> tuple[str, list[object]]:
    """Complete people/time predicate applied before ranked LIMIT."""
    clauses: list[str] = []
    params: list[object] = []
    if scope_thread_id is not None:
        clauses.append(f" AND LOWER(TRIM(CAST(json_extract({prefix}metadata_json, '$.thread_id') AS TEXT))) = ?")
        params.append(scope_thread_id.casefold())
    if scope_task_id is not None:
        clauses.append(f" AND LOWER(TRIM(CAST(json_extract({prefix}metadata_json, '$.task_id') AS TEXT))) = ?")
        params.append(scope_task_id.casefold())
    if scope_people:
        people = list(scope_people)
        person_ids = list(scope_person_memory_ids)
        alternatives: list[str] = []
        if person_ids:
            alternatives.append(f"{prefix}id IN ({self._placeholders(person_ids)})")
            params.extend(person_ids)
        json_values = ", ".join(
            f"json_extract({prefix}metadata_json, '$.{key}')"
            for key in ("person_id", "person_ids", "person", "people", "people_ids")
        )
        alternatives.append(
            "EXISTS (SELECT 1 FROM json_tree(json_array("
            + json_values
            + ")) AS scoped_person "
            + "WHERE scoped_person.type = 'text' "
            + f"AND LOWER(TRIM(CAST(scoped_person.value AS TEXT))) IN ({self._placeholders(people)}))"
        )
        params.extend(people)
        clauses.append(" AND (" + " OR ".join(alternatives) + ")")
    if scope_window_start is not None or scope_window_end is not None:
        event_time = (
            f"COALESCE(julianday({prefix}valid_from), julianday({prefix}last_seen_at), "
            f"julianday({prefix}updated_at), julianday({prefix}first_seen_at), "
            f"julianday({prefix}created_at))"
        )
    if scope_window_start is not None:
        clauses.append(f" AND {event_time} >= julianday(?)")
        params.append(_iso_or_none(scope_window_start))
    if scope_window_end is not None:
        clauses.append(f" AND {event_time} <= julianday(?)")
        params.append(_iso_or_none(scope_window_end))
    return "".join(clauses), params


def _metadata_scope_clause(
    self,
    *,
    metadata_expression: str,
    scope_projects: tuple[str, ...] = (),
    scope_people: tuple[str, ...] = (),
    direct_project_expression: str | None = None,
    direct_person_expression: str | None = None,
    persisted_source_envelope: bool = False,
    event_time_expression: str,
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
    domain_expression: str | None = None,
    global_excluded_domains: tuple[str, ...] | None = None,
) -> tuple[str, list[object]]:
    """Project/people/time predicate for source and open-loop reads.

    ``scope_projects`` may hold the reserved marker (spec 6.1). With the marker,
    ``global_excluded_domains`` leaves out global rows whose ``domain_expression``
    is in the set, and it must be stated (an empty tuple leaves none out): ``None``
    raises.
    """

    clauses: list[str] = []
    params: list[object] = []

    def _metadata_values(keys: tuple[str, ...], values: tuple[str, ...]) -> str:
        json_values = ", ".join(f"json_extract({metadata_expression}, '$.{key}')" for key in keys)
        params.extend(values)
        return (
            "EXISTS (SELECT 1 FROM json_tree(json_array("
            + json_values
            + ")) AS scoped_value WHERE scoped_value.type = 'text' "
            + "AND LOWER(TRIM(CAST(scoped_value.value AS TEXT))) "
            + f"IN ({self._placeholders(list(values))}))"
        )

    if project_scope_identity(scope_projects):
        if persisted_source_envelope:
            project_scope_expression = f"alice_source_project_scope_identity({metadata_expression})"
            text_expressions: tuple[str, ...] = (metadata_expression,)
        else:
            direct_project = direct_project_expression or "NULL"
            project_scope_expression = f"alice_project_scope_identity({metadata_expression}, {direct_project})"
            text_expressions = (
                (metadata_expression, direct_project_expression)
                if direct_project_expression
                else (metadata_expression,)
            )
        project_sql, project_params = _project_view_sql(
            placeholders=self._placeholders,
            projects=scope_projects,
            scope_expression=project_scope_expression,
            text_expressions=text_expressions,
            domain_expression=domain_expression,
            global_excluded_domains=global_excluded_domains,
        )
        clauses.append(project_sql)
        params.extend(project_params)
    if scope_people:
        alternatives = [
            _metadata_values(
                ("person_id", "person_ids", "person", "people", "people_ids"),
                scope_people,
            )
        ]
        if direct_person_expression is not None:
            insertion_index = len(params) - len(scope_people)
            alternatives.insert(
                0,
                f"LOWER(TRIM(CAST({direct_person_expression} AS TEXT))) "
                f"IN ({self._placeholders(list(scope_people))})",
            )
            params[insertion_index:insertion_index] = list(scope_people)
        clauses.append(" AND (" + " OR ".join(alternatives) + ")")
    if scope_window_start is not None:
        clauses.append(f" AND {event_time_expression} >= julianday(?)")
        params.append(_iso_or_none(scope_window_start))
    if scope_window_end is not None:
        clauses.append(f" AND {event_time_expression} <= julianday(?)")
        params.append(_iso_or_none(scope_window_end))
    return "".join(clauses), params


@staticmethod  # type: ignore[misc]  # intentionally graft the descriptor into the facade
def _like_any(column: str, pattern_count: int) -> str:
    predicate = f"LOWER(COALESCE({column}, '')) LIKE ?"
    return "(" + " OR ".join([predicate] * pattern_count) + ")"


for _query_helper in (
    _project_scope_value_sqlite,
    _project_scope_identity_json_sqlite,
    _source_project_scope_identity_json_sqlite,
    _ensure_project_scope_identity_sqlite,
    _escape_like_literal,
    _sqlite_ascii_literal_contains_sql,
    _fts_match_expression,
    _fts_match_any_expression,
):
    _query_helper.__module__ = "alicebot_api.sqlite_store"
    _query_helper.__qualname__ = _query_helper.__name__
del _query_helper


for _store_helper in (
    _domain_clause,
    _sensitivity_clause,
    _memory_type_clause,
    _project_clause,
    _created_by_clause,
    _retrieval_scope_clause,
    _metadata_scope_clause,
):
    _store_helper.__module__ = "alicebot_api.sqlite_store"
    _store_helper.__qualname__ = f"SQLiteVNextStore.{_store_helper.__name__}"
del _store_helper

for _store_descriptor in (_placeholders, _run_clause, _expiry_clause, _like_any):
    _store_descriptor.__module__ = "alicebot_api.sqlite_store"
    _store_descriptor.__qualname__ = f"SQLiteVNextStore.{_store_descriptor.__name__}"
    _store_descriptor.__func__.__module__ = "alicebot_api.sqlite_store"  # type: ignore[union-attr]
    _store_descriptor.__func__.__qualname__ = (  # type: ignore[union-attr]
        f"SQLiteVNextStore.{_store_descriptor.__name__}"
    )
del _store_descriptor
