"""PostgreSQL memory read and retrieval store seam."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    memory_embedding_signature_is_current,
)
from alicebot_api.vnext_project_scope import project_scope_identity
from alicebot_api.vnext_recall_visibility import POSTGRES_UNEXPIRED_SQL
from alicebot_api.vnext_stores.postgres.columns import MEMORY_COLUMNS
from alicebot_api.vnext_stores.postgres.embedding_cas import (
    _MEMORY_EMBEDDING_CONTENT_SHA256_SQL,
    _vector_literal,
)
from alicebot_api.vnext_stores.postgres.primitives import _json_list
from alicebot_api.vnext_stores.postgres.query_predicates import (
    _MEMORY_DIRECT_PEOPLE_SQL,
    _MEMORY_GROUP_SCOPE_SQL,
    _MEMORY_PROJECT_SCOPE_SQL,
    _MEMORY_SCOPE_EVENT_TIME_SQL,
    _escape_like_literal,
    _postgres_ascii_literal_contains_sql,
    _tsquery_any_expression,
)
from alicebot_api.vnext_stores.retrieval_common import _search_patterns

VNextRow = dict[str, object]

# Statuses admitted by ordinary memory reads. Maintenance-demoted stale,
# superseded, rejected, deleted, and every unknown status remain excluded
# unless a method explicitly selects a different lifecycle state.
_MEMORY_SEARCHABLE_STATUSES_SQL = "('active', 'accepted')"

def get_memory_by_key(
    self,
    *,
    memory_key: str,
    agent_profile_id: str = "assistant_default",
    include_deleted: bool = False,
) -> VNextRow | None:
    """The memory at ``memory_key``, or ``None``.

    A soft-deleted (archived) row is not returned unless ``include_deleted`` is true. It still holds its key:
    the unique constraint on ``(user, profile, memory_key)`` counts every row, so a caller that reads the key
    to avoid a collision on insert passes ``include_deleted=True`` to see every row the constraint sees.
    """

    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE agent_profile_id = %s
                  AND memory_key = %s
                  {deleted_clause}
                LIMIT 1
                """,
        (agent_profile_id, memory_key),
    )


def get_memory(self, memory_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                """,
        (memory_id,),
    )


def get_memories_by_ids(self, memory_ids: Sequence[str], *, include_deleted: bool = False) -> list[VNextRow]:
    """The memories at ``memory_ids``. A soft-deleted (archived or redacted) row is returned only with
    ``include_deleted``, which a reader that must tell a deleted memory from one that never existed passes."""

    ids = list(dict.fromkeys(str(memory_id) for memory_id in memory_ids if memory_id))
    if not ids:
        return []
    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE id = ANY(%s::uuid[])
                  {deleted_clause}
                """,
        (ids,),
    )


def list_memories_referencing_source(self, *, source_id: str, limit: int = 500) -> list[VNextRow]:
    """Bound memories related to one source, including provenance links."""

    if limit < 1:
        raise ValueError("limit must be positive")
    source_ref = f"source:{source_id}"
    qualified_columns = ", ".join(f"m.{column.strip()}" for column in MEMORY_COLUMNS.split(",") if column.strip())
    return self._fetch_all(
        f"""
                SELECT {qualified_columns}
                FROM memories AS m
                WHERE m.deleted_at IS NULL
                  AND (
                    m.source_event_ids ? %s
                    OR EXISTS (
                      SELECT 1
                      FROM provenance_links AS p
                      WHERE p.target_type = 'memory'
                        AND p.target_id = m.id::text
                        AND p.source_id = %s::uuid
                    )
                    OR m.metadata_json ->> 'source_id' = %s
                    OR m.metadata_json ->> 'source_ref' IN (%s, %s)
                    OR m.metadata_json -> 'source_ids' ? %s
                    OR m.metadata_json -> 'source_refs' ? %s
                    OR m.metadata_json -> 'source_refs' ? %s
                    OR m.metadata_json -> 'source_references' ? %s
                    OR m.metadata_json -> 'source_references' ? %s
                    OR m.metadata_json -> 'selected_source_ids' ? %s
                  )
                ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC
                LIMIT %s
                """,
        (
            source_id,
            source_id,
            source_id,
            source_id,
            source_ref,
            source_id,
            source_id,
            source_ref,
            source_id,
            source_ref,
            source_id,
            limit,
        ),
    )


def list_memories_referencing_sources(
    self,
    source_ids: Sequence[str],
    *,
    limit_per_source: int,
) -> dict[str, list[VNextRow]]:
    """``list_memories_referencing_source`` for several sources in one statement.

    Returns ``{source_id: rows}`` with one key per distinct source id asked
    for, in the order asked, and an empty list for a source nothing
    references. Each list is exactly what the one-source method returns for
    that source with ``limit=limit_per_source``: the same rows, the same
    order, the same cap. The controls are the same two the one-source method
    applies, the row-level security of the user's connection and
    ``deleted_at IS NULL``.

    The statement runs the one-source query once per requested id inside a
    ``LATERAL`` subquery, so the predicate, the ordering and the per-source
    ``LIMIT`` are the one-source query's own text. The saving is the round
    trip and the per-statement overhead of one query per packed source, not
    the scan: each id still evaluates the predicate over the memories table.
    ``limit_per_source`` has no default because the callers disagree about
    the cap.
    """

    if limit_per_source < 1:
        raise ValueError("limit must be positive")
    ids = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
    grouped: dict[str, list[VNextRow]] = {source_id: [] for source_id in ids}
    if not ids:
        return grouped
    refs = [f"source:{source_id}" for source_id in ids]
    qualified_columns = ", ".join(f"m.{column.strip()}" for column in MEMORY_COLUMNS.split(",") if column.strip())
    rows = self._fetch_all(
        f"""
                SELECT w.source_id AS ref_source_id, hit.*
                FROM unnest(%s::text[], %s::text[]) AS w(source_id, source_ref)
                CROSS JOIN LATERAL (
                  SELECT {qualified_columns}
                  FROM memories AS m
                  WHERE m.deleted_at IS NULL
                    AND (
                      m.source_event_ids ? w.source_id
                      OR EXISTS (
                        SELECT 1
                        FROM provenance_links AS p
                        WHERE p.target_type = 'memory'
                          AND p.target_id = m.id::text
                          AND p.source_id = w.source_id::uuid
                      )
                      OR m.metadata_json ->> 'source_id' = w.source_id
                      OR m.metadata_json ->> 'source_ref' IN (w.source_id, w.source_ref)
                      OR m.metadata_json -> 'source_ids' ? w.source_id
                      OR m.metadata_json -> 'source_refs' ? w.source_id
                      OR m.metadata_json -> 'source_refs' ? w.source_ref
                      OR m.metadata_json -> 'source_references' ? w.source_id
                      OR m.metadata_json -> 'source_references' ? w.source_ref
                      OR m.metadata_json -> 'selected_source_ids' ? w.source_id
                    )
                  ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC
                  LIMIT %s
                ) AS hit
                ORDER BY w.source_id, hit.updated_at DESC, hit.created_at DESC, hit.id DESC
                """,
        (ids, refs, limit_per_source),
    )
    for row in rows:
        grouped[str(row.pop("ref_source_id"))].append(row)
    return grouped


def list_pending_derived_candidates_for_member(
    self,
    *,
    member_id: str,
    exclude_memory_id: str | None = None,
) -> list[VNextRow]:
    """Lock pending consolidation/roll-up candidates derived from a member.

        New derived candidates persist their reviewed inputs under
        ``consolidation.member_snapshots``. Querying that bounded pending set
        lets a correction or retirement invalidate stale review work in the
        same transaction as the source-memory mutation.
        """
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories AS candidate
                WHERE candidate.deleted_at IS NULL
                  AND candidate.status IN ('candidate', 'needs_review')
                  AND (%s::uuid IS NULL OR candidate.id <> %s::uuid)
                  AND EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                      CASE
                        WHEN jsonb_typeof(
                          candidate.metadata_json #> '{{consolidation,member_snapshots}}'
                        ) = 'array'
                        THEN candidate.metadata_json #> '{{consolidation,member_snapshots}}'
                        ELSE '[]'::jsonb
                      END
                    ) AS member_snapshot(value)
                    WHERE member_snapshot.value ->> 'id' = %s
                  )
                ORDER BY candidate.id
                FOR UPDATE OF candidate
                """,
        (exclude_memory_id, exclude_memory_id, str(member_id)),
    )


def list_memories(
    self,
    *,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    memory_types: Sequence[str] | None = None,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    projects: Sequence[str] | None = None,
    created_at_start: datetime | None = None,
    created_at_end: datetime | None = None,
    query: str | None = None,
    order_by_created_at: bool = False,
    limit: int | None = None,
    include_expired: bool = True,
) -> list[VNextRow]:
    """List memories. ``include_expired=False`` leaves out a memory whose ``valid_to`` has passed.

    The default keeps the management reads (review, confirm, unexpire, export)
    seeing an expired memory. A read an agent or a brief shows to a person passes
    ``False``, which applies recall's own test (``POSTGRES_UNEXPIRED_SQL``) before
    ``LIMIT``.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    status_sql = ""
    params: list[object] = []
    if status is not None:
        status_sql = " AND status = %s"
        params.append(status)
    statuses_sql = ""
    if statuses is not None:
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return []
        statuses_sql = " AND status = ANY(%s::text[])"
        params.append(normalized_statuses)
    memory_types_sql = ""
    if memory_types is not None:
        normalized_memory_types = list(dict.fromkeys(str(value) for value in memory_types if str(value)))
        if not normalized_memory_types:
            return []
        memory_types_sql = " AND memory_type = ANY(%s::text[])"
        params.append(normalized_memory_types)
    domains_sql = ""
    if domains:
        domains_sql = " AND (domain = ANY(%s::text[]) OR domain = 'unknown')"
        params.append(domains)
    sensitivity_sql = ""
    if sensitivity_allowed is not None:
        if not sensitivity_allowed:
            return []
        sensitivity_sql = " AND COALESCE(sensitivity, 'unknown') = ANY(%s::text[])"
        params.append(sensitivity_allowed)
    projects_sql = ""
    project_list = list(project_scope_identity(projects or ())) or None
    if project_list is not None:
        projects_sql = f" AND ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[]"
        params.append(project_list)
    created_at_sql = ""
    if created_at_start is not None:
        created_at_sql += " AND created_at >= %s::timestamptz"
        params.append(created_at_start)
    if created_at_end is not None:
        created_at_sql += " AND created_at <= %s::timestamptz"
        params.append(created_at_end)
    query_sql = ""
    if query is not None:
        normalized_query = str(query).strip()
        if normalized_query:
            query_sql = (
                f" AND ({_postgres_ascii_literal_contains_sql("COALESCE(title, '')")}"
                f" OR {_postgres_ascii_literal_contains_sql("COALESCE(canonical_text, '')")}"
                f" OR {_postgres_ascii_literal_contains_sql("COALESCE(summary, '')")})"
            )
            escaped_query = _escape_like_literal(normalized_query)
            params.extend((escaped_query, escaped_query, escaped_query))
    expiry_sql = "" if include_expired else f" AND {POSTGRES_UNEXPIRED_SQL}"
    order_sql = (
        "ORDER BY created_at DESC, id DESC"
        if order_by_created_at
        else "ORDER BY updated_at DESC, created_at DESC, id DESC"
    )
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT %s"
        params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL{status_sql}{statuses_sql}{memory_types_sql}
                  {domains_sql}{sensitivity_sql}{projects_sql}{created_at_sql}{query_sql}{expiry_sql}
                {order_sql}
                {limit_sql}
                """,
        tuple(params),
    )


def list_memories_by_statuses(
    self,
    *,
    statuses: Sequence[str],
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    limit: int = 30,
) -> list[VNextRow]:
    """Return a bounded review/workspace slice for several statuses.

        Callers previously materialized the entire memory corpus and filtered
        it in Python.  Keep the status predicate, tenant RLS, ordering, and
        limit in PostgreSQL so dashboard latency and memory use stay bounded.
        """
    if limit < 1:
        raise ValueError("limit must be positive")
    normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
    if not normalized_statuses:
        return []
    if sensitivity_allowed is not None and not sensitivity_allowed:
        return []
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR COALESCE(sensitivity, 'unknown') = ANY(%s::text[]))
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (
            normalized_statuses,
            domains,
            domains,
            sensitivity_allowed,
            sensitivity_allowed,
            limit,
        ),
    )


def count_memories_by_status(
    self,
    *,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
) -> dict[str, int]:
    """Return exact status counts without loading memory rows."""
    if sensitivity_allowed is not None and not sensitivity_allowed:
        return {}
    rows = self._fetch_all(
        """
                SELECT status, COUNT(*)::bigint AS count
                FROM memories
                WHERE deleted_at IS NULL
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR COALESCE(sensitivity, 'unknown') = ANY(%s::text[]))
                GROUP BY status
                ORDER BY status
                """,
        (domains, domains, sensitivity_allowed, sensitivity_allowed),
    )
    return {str(row["status"]): int(cast(int, row["count"])) for row in rows}


def list_recent_agentic_commits(self, *, limit: int = 20) -> list[VNextRow]:
    """Bounded replacement for scanning all memories in ``recent_commits``."""
    if limit < 1:
        raise ValueError("limit must be positive")
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND metadata_json #>> '{{agentic_memory,kind}}' = 'agentic_memory_commit'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (limit,),
    )


def list_pending_inline_confirmations(self, *, limit: int = 20) -> list[VNextRow]:
    """Return only actionable inline confirmations, never resolved rows."""
    if limit < 1:
        raise ValueError("limit must be positive")
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status = 'needs_review'
                  AND confirmation_status = 'unconfirmed'
                  AND metadata_json #>> '{{agentic_memory,kind}}' = 'agentic_memory_commit'
                  AND metadata_json #>> '{{agentic_memory,confirmation,status}}' = 'pending'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (limit,),
    )


def find_live_memory_by_canonical_text(
    self,
    canonical_text: str,
    *,
    domain: str,
    sensitivity: str,
    project_scope: Sequence[str] = (),
) -> VNextRow | None:
    """Find one live, exactly-scoped duplicate without an O(N) scan."""
    normalized_text = str(canonical_text).strip()
    if not normalized_text:
        return None
    normalized_scope = list(project_scope_identity(project_scope))
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN ('candidate', 'active', 'accepted', 'needs_review', 'private_only')
                  AND md5(lower(canonical_text)) = md5(lower(%s))
                  AND lower(canonical_text) = lower(%s)
                  AND domain = %s
                  AND sensitivity = %s
                  AND {_MEMORY_PROJECT_SCOPE_SQL} = %s::jsonb
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
        (
            normalized_text,
            normalized_text,
            str(domain),
            str(sensitivity),
            _json_list(normalized_scope),
        ),
    )


def list_memories_for_staleness_sweep(
    self,
    *,
    reference_time: datetime,
    confirmation_before: datetime,
    review_memory_types: Sequence[str],
    limit: int,
    projects: Sequence[str] | None = None,
) -> list[VNextRow]:
    """Read only active rows that actually cross a staleness threshold."""
    if limit < 1:
        raise ValueError("limit must be positive")
    memory_types = list(dict.fromkeys(str(value) for value in review_memory_types if str(value)))
    project_list = list(project_scope_identity(projects or ())) or None
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status = 'active'
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                  AND (
                    (valid_to IS NOT NULL AND valid_to < %s::timestamptz)
                    OR (
                      memory_type = ANY(%s::text[])
                      AND COALESCE(last_confirmed_at, last_seen_at, created_at) < %s::timestamptz
                    )
                  )
                ORDER BY
                  CASE WHEN valid_to IS NOT NULL AND valid_to < %s::timestamptz THEN 0 ELSE 1 END,
                  COALESCE(valid_to, last_confirmed_at, last_seen_at, created_at) ASC,
                  id ASC
                LIMIT %s
                """,
        (
            project_list,
            project_list,
            reference_time,
            memory_types,
            confirmation_before,
            reference_time,
            limit,
        ),
    )


def count_memories(
    self,
    *,
    status: str | None = None,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    projects: Sequence[str] | None = None,
    include_expired: bool = True,
) -> int:
    """Count the exact in-scope memory corpus without materializing it.

    ``include_expired=False`` leaves out a memory whose ``valid_to`` has passed,
    as ``list_memories`` does, so a count and the list behind it agree.
    """
    status_sql = ""
    params: list[object] = []
    if status is not None:
        status_sql = " AND status = %s"
        params.append(status)
    domains_sql = ""
    if domains:
        domains_sql = " AND (domain = ANY(%s::text[]) OR domain = 'unknown')"
        params.append(domains)
    sensitivity_sql = ""
    if sensitivity_allowed is not None:
        if not sensitivity_allowed:
            return 0
        sensitivity_sql = " AND COALESCE(sensitivity, 'unknown') = ANY(%s::text[])"
        params.append(sensitivity_allowed)
    projects_sql = ""
    project_list = list(project_scope_identity(projects or ())) or None
    if project_list is not None:
        projects_sql = f" AND ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[]"
        params.append(project_list)
    expiry_sql = "" if include_expired else f" AND {POSTGRES_UNEXPIRED_SQL}"
    row = self._fetch_one(
        "count memories",
        f"""
                SELECT COUNT(*) AS count
                FROM memories
                WHERE deleted_at IS NULL{status_sql}{domains_sql}{sensitivity_sql}{projects_sql}{expiry_sql}
                """,
        tuple(params),
    )
    return cast(int, row["count"])


def list_rollup_input_memories(
    self,
    *,
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    excluded_candidate_kind: str,
    limit: int,
    projects: Sequence[str] | None = None,
) -> list[VNextRow]:
    """Return the newest bounded roll-up inputs, excluding cards in SQL."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not sensitivity_allowed:
        return []
    domain_filter = domains or None
    project_list = list(project_scope_identity(projects or ())) or None
    # A roll-up groups memories that recall can return, so an expired one is
    # left out here, before LIMIT, the way recall leaves it out.
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND {POSTGRES_UNEXPIRED_SQL}
                  AND COALESCE(metadata_json ->> 'candidate_kind', '') <> %s
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND COALESCE(sensitivity, 'unknown') = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
        (
            excluded_candidate_kind,
            domain_filter,
            domain_filter,
            sensitivity_allowed,
            project_list,
            project_list,
            limit,
        ),
    )


def count_rollup_input_memories(
    self,
    *,
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    excluded_candidate_kind: str,
    projects: Sequence[str] | None = None,
) -> int:
    """Return the authoritative total behind the bounded roll-up read."""
    if not sensitivity_allowed:
        return 0
    domain_filter = domains or None
    project_list = list(project_scope_identity(projects or ())) or None
    row = self._fetch_one(
        "count rollup input memories",
        f"""
                SELECT COUNT(*) AS count
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND {POSTGRES_UNEXPIRED_SQL}
                  AND COALESCE(metadata_json ->> 'candidate_kind', '') <> %s
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND COALESCE(sensitivity, 'unknown') = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                """,
        (
            excluded_candidate_kind,
            domain_filter,
            domain_filter,
            sensitivity_allowed,
            project_list,
            project_list,
        ),
    )
    return cast(int, row["count"])


def list_pending_rollup_candidates(
    self,
    *,
    rollup_digests: tuple[str, ...],
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    candidate_kind: str,
    limit: int,
    projects: Sequence[str] | None = None,
) -> list[VNextRow]:
    """Return at most one newest pending candidate per requested digest."""
    unique_digests = tuple(sorted(set(rollup_digests)))
    if not unique_digests or not sensitivity_allowed:
        return []
    if limit < 1:
        raise ValueError("limit must be positive")
    bounded_limit = min(limit, len(unique_digests))
    domain_filter = domains or None
    project_list = list(project_scope_identity(projects or ())) or None
    return self._fetch_all(
        f"""
                SELECT DISTINCT ON (metadata_json ->> 'rollup_digest') {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status = 'candidate'
                  AND metadata_json ->> 'candidate_kind' = %s
                  AND metadata_json ->> 'rollup_digest' = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND COALESCE(sensitivity, 'unknown') = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR ({_MEMORY_GROUP_SCOPE_SQL}) ?| %s::text[])
                ORDER BY metadata_json ->> 'rollup_digest', updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (
            candidate_kind,
            list(unique_digests),
            domain_filter,
            domain_filter,
            sensitivity_allowed,
            project_list,
            project_list,
            bounded_limit,
        ),
    )


def list_accepted_rollup_cards(
    self,
    *,
    rollup_keys: tuple[str, ...],
    domains: list[str] | None,
    sensitivity_allowed: list[str],
    candidate_kind: str,
    limit: int,
    projects: Sequence[str] | None = None,
) -> list[VNextRow]:
    """Return at most one active/accepted card per requested roll-up key."""
    unique_keys = tuple(sorted(set(rollup_keys)))
    if not unique_keys or not sensitivity_allowed:
        return []
    if limit < 1:
        raise ValueError("limit must be positive")
    bounded_limit = min(limit, len(unique_keys))
    domain_filter = domains or None
    project_list = list(project_scope_identity(projects or ())) or None
    return self._fetch_all(
        f"""
                SELECT DISTINCT ON (metadata_json ->> 'rollup_key') {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND {POSTGRES_UNEXPIRED_SQL}
                  AND metadata_json ->> 'candidate_kind' = %s
                  AND metadata_json ->> 'rollup_key' = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND COALESCE(sensitivity, 'unknown') = ANY(%s::text[])
                  AND (%s::text[] IS NULL OR ({_MEMORY_GROUP_SCOPE_SQL}) ?| %s::text[])
                ORDER BY
                  metadata_json ->> 'rollup_key',
                  CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                  updated_at DESC,
                  created_at DESC,
                  id DESC
                LIMIT %s
                """,
        (
            candidate_kind,
            list(unique_keys),
            domain_filter,
            domain_filter,
            sensitivity_allowed,
            project_list,
            project_list,
            bounded_limit,
        ),
    )


def search_memories(
    self,
    *,
    query: str,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    limit: int = 8,
    memory_types: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    created_by_agent_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    include_expired: bool = False,
) -> list[VNextRow]:
    patterns = _search_patterns(query)
    exact_pattern = patterns[0]
    memory_type_list = list(memory_types) or None
    project_list = list(project_scope_identity(projects)) or None
    created_by_list = list(created_by_agent_ids) or None
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR memory_type = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                  AND (%s::text[] IS NULL OR created_by_agent_id = ANY(%s::text[]))
                  AND (%s::text IS NULL OR run_id = %s)
                  AND (%s::boolean OR valid_to IS NULL OR valid_to >= clock_timestamp())
                  AND (
                    memory_key ILIKE ANY(%s::text[])
                    OR title ILIKE ANY(%s::text[])
                    OR canonical_text ILIKE ANY(%s::text[])
                    OR summary ILIKE ANY(%s::text[])
                    OR value::text ILIKE ANY(%s::text[])
                  )
                ORDER BY
                  CASE
                    WHEN canonical_text ILIKE %s THEN 0
                    WHEN title ILIKE %s THEN 1
                    WHEN canonical_text ILIKE ANY(%s::text[]) THEN 2
                    WHEN title ILIKE ANY(%s::text[]) THEN 3
                    ELSE 4
                  END,
                  updated_at DESC,
                  created_at DESC,
                  id DESC
                LIMIT %s
                """,
        (
            domains,
            domains,
            sensitivity_allowed,
            sensitivity_allowed,
            memory_type_list,
            memory_type_list,
            project_list,
            project_list,
            created_by_list,
            created_by_list,
            run_id,
            run_id,
            include_expired,
            patterns,
            patterns,
            patterns,
            patterns,
            patterns,
            exact_pattern,
            exact_pattern,
            patterns,
            patterns,
            limit,
        ),
    )


def search_memories_fts(
    self,
    *,
    query: str,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    limit: int = 50,
    memory_types: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    created_by_agent_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    include_expired: bool = False,
    match_any: bool = False,
    scope_thread_id: str | None = None,
    scope_task_id: str | None = None,
    scope_people: tuple[str, ...] = (),
    scope_person_memory_ids: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
) -> list[VNextRow]:
    memory_type_list = list(memory_types) or None
    project_list = list(project_scope_identity(projects)) or None
    created_by_list = list(created_by_agent_ids) or None
    scope_people_list = list(scope_people) or None
    scope_person_id_list = list(scope_person_memory_ids) or None
    # Strict pass: websearch_to_tsquery ANDs every non-stopword term.
    # match_any (the retrieval OR-fallback): a to_tsquery OR of the
    # sanitized lexemes, so a natural-language question still reaches
    # keyword-findable memories when the AND pass returns nothing.
    if match_any:
        tsquery_sql = "to_tsquery('english', %s)"
        tsquery_text = _tsquery_any_expression(query)
        if tsquery_text is None:
            return []
    else:
        tsquery_sql = "websearch_to_tsquery('english', %s)"
        tsquery_text = query
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS},
                  ts_rank(search_tsv, {tsquery_sql}) AS fts_score
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR memory_type = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                  AND (%s::text[] IS NULL OR created_by_agent_id = ANY(%s::text[]))
                  AND (%s::text IS NULL OR run_id = %s)
                  AND (%s::boolean OR valid_to IS NULL OR valid_to >= clock_timestamp())
                  AND (
                    %s::text IS NULL
                    OR lower(trim(metadata_json ->> 'thread_id')) = lower(trim(%s::text))
                  )
                  AND (
                    %s::text IS NULL
                    OR lower(trim(metadata_json ->> 'task_id')) = lower(trim(%s::text))
                  )
                  AND (
                    %s::text[] IS NULL
                    OR id::text = ANY(%s::text[])
                    OR {_MEMORY_DIRECT_PEOPLE_SQL}
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_MEMORY_SCOPE_EVENT_TIME_SQL} >= %s::timestamptz
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_MEMORY_SCOPE_EVENT_TIME_SQL} <= %s::timestamptz
                  )
                  AND search_tsv @@ {tsquery_sql}
                ORDER BY fts_score DESC, updated_at DESC, created_at DESC, id DESC
                LIMIT %s
                """,
        (
            tsquery_text,
            domains,
            domains,
            sensitivity_allowed,
            sensitivity_allowed,
            memory_type_list,
            memory_type_list,
            project_list,
            project_list,
            created_by_list,
            created_by_list,
            run_id,
            run_id,
            include_expired,
            scope_thread_id,
            scope_thread_id,
            scope_task_id,
            scope_task_id,
            scope_people_list,
            scope_person_id_list,
            scope_people_list,
            scope_window_start,
            scope_window_start,
            scope_window_end,
            scope_window_end,
            tsquery_text,
            limit,
        ),
    )


def search_memories_vector(
    self,
    *,
    query_vector: list[float],
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    limit: int = 50,
    memory_types: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    created_by_agent_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    include_expired: bool = False,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_endpoint: str | None = None,
    embedding_signature_version: int | None = None,
    scope_thread_id: str | None = None,
    scope_task_id: str | None = None,
    scope_people: tuple[str, ...] = (),
    scope_person_memory_ids: tuple[str, ...] = (),
    scope_window_start: datetime | None = None,
    scope_window_end: datetime | None = None,
) -> list[VNextRow]:
    vector_param = _vector_literal(query_vector)
    memory_type_list = list(memory_types) or None
    project_list = list(project_scope_identity(projects)) or None
    created_by_list = list(created_by_agent_ids) or None
    scope_people_list = list(scope_people) or None
    scope_person_id_list = list(scope_person_memory_ids) or None
    signature_sql = ""
    signature_params: list[object] = []
    if embedding_provider is not None or embedding_model is not None:
        if not embedding_provider or not embedding_model:
            raise ContinuityStoreInvariantError("embedding_provider and embedding_model must be supplied together")
        signature_sql = f"""
                  AND metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'provider' = %s
                  AND metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'model' = %s
            """
        signature_params.extend((embedding_provider, embedding_model))
        if embedding_endpoint is not None:
            # Vectors carry an endpoint fingerprint; only pool those from the
            # same endpoint as the query so distinct coordinate spaces that
            # share provider/model labels are never compared.
            signature_sql += f"""
                  AND metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'endpoint' = %s
                """
            signature_params.append(embedding_endpoint)
        if embedding_signature_version is not None:
            signature_sql += f"""
                  AND metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'version' = %s
                """
            signature_params.append(str(embedding_signature_version))
        # Content freshness belongs in the indexed candidate query, not
        # after its LIMIT.  Filtering stale signatures only in Python let
        # four stale nearest neighbors exhaust a limit=1 overfetch and
        # report zero vector candidates even when a current row followed.
        signature_sql += f"""
                  AND metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'content_sha256'
                    = ({_MEMORY_EMBEDDING_CONTENT_SHA256_SQL})
            """
    params: list[object] = [
        vector_param,
        domains,
        domains,
        sensitivity_allowed,
        sensitivity_allowed,
        memory_type_list,
        memory_type_list,
        project_list,
        project_list,
        created_by_list,
        created_by_list,
        run_id,
        run_id,
        scope_thread_id,
        scope_thread_id,
        scope_task_id,
        scope_task_id,
        scope_people_list,
        scope_person_id_list,
        scope_people_list,
        scope_window_start,
        scope_window_start,
        scope_window_end,
        scope_window_end,
    ]
    params.extend(signature_params)
    candidate_limit = max(limit, min(limit * 4, 1000)) if signature_sql else limit
    base_params = [*params, include_expired, vector_param]
    # Enable iterative HNSW scan so the lifecycle/scope/signature filters
    # applied alongside the approximate ORDER BY do not silently underfill
    # the result set (a plain filtered HNSW scan can return far fewer than
    # LIMIT valid rows). ``hnsw.iterative_scan`` is a dotted custom GUC, so
    # this is a harmless no-op on pgvector < 0.8 rather than an error, and
    # SET LOCAL scopes it to the current transaction.
    with self.conn.cursor() as cur:
        cur.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
    vector_query = f"""
                SELECT {MEMORY_COLUMNS},
                  (embedding_vector <=> %s::vector) AS vector_distance
                FROM memories
                WHERE deleted_at IS NULL
                  AND embedding_vector IS NOT NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR memory_type = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                  AND (%s::text[] IS NULL OR created_by_agent_id = ANY(%s::text[]))
                  AND (%s::text IS NULL OR run_id = %s)
                  AND (
                    %s::text IS NULL
                    OR lower(trim(metadata_json ->> 'thread_id')) = lower(trim(%s::text))
                  )
                  AND (
                    %s::text IS NULL
                    OR lower(trim(metadata_json ->> 'task_id')) = lower(trim(%s::text))
                  )
                  AND (
                    %s::text[] IS NULL
                    OR id::text = ANY(%s::text[])
                    OR {_MEMORY_DIRECT_PEOPLE_SQL}
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_MEMORY_SCOPE_EVENT_TIME_SQL} >= %s::timestamptz
                  )
                  AND (
                    %s::timestamptz IS NULL
                    OR {_MEMORY_SCOPE_EVENT_TIME_SQL} <= %s::timestamptz
                  )
                  {signature_sql}
                  AND (%s::boolean OR valid_to IS NULL OR valid_to >= clock_timestamp())
                ORDER BY embedding_vector <=> %s::vector
                LIMIT %s
                """
    while True:
        rows = self._fetch_all(vector_query, tuple((*base_params, candidate_limit)))
        if not signature_sql:
            return rows
        current_rows = [row for row in rows if memory_embedding_signature_is_current(row)]
        if len(current_rows) >= limit:
            return current_rows[:limit]
        # The SQL freshness predicate should make every row current. This
        # bounded deepening is a defensive compatibility path for older
        # pgcrypto/text-normalization behavior and test doubles: expand
        # until PostgreSQL reports exhaustion rather than treating a
        # fixed four-times overfetch as authoritative.
        if len(rows) < candidate_limit or candidate_limit >= 1000:
            return current_rows[:limit]
        candidate_limit = min(candidate_limit * 2, 1000)


def search_memories_by_time(
    self,
    *,
    window_start: datetime,
    window_end: datetime,
    window_center: datetime | None = None,
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    limit: int = 50,
    memory_types: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    created_by_agent_ids: tuple[str, ...] = (),
    run_id: str | None = None,
    include_expired: bool = False,
) -> list[VNextRow]:
    """Memories whose event window intersects ``[window_start, window_end)``.

        Event time is ``COALESCE(valid_from, first_seen_at, created_at)``:
        ``valid_from`` is the explicit event-validity start when a writer
        recorded one (the honest event signal); ``first_seen_at`` — when
        the fact was first observed — is the fallback for rows without
        one, and ``created_at`` (row write time) is the last resort for
        legacy rows. A row matches when that event time falls inside the
        window, or when a closed ``[valid_from, valid_to)`` validity
        interval overlaps it. Results order by proximity of the event
        time to ``window_center`` (default: the window midpoint; open
        "before X"/"since X" windows pass their closed edge), so the
        tightest temporal matches lead the RRF list. Same scoping
        discipline as the sibling search methods (deleted/status gates,
        domain/sensitivity/scope filters); note the default expiry gate
        still hides rows whose ``valid_to`` has passed — pass
        ``include_expired=True`` to recall facts that were only true
        historically.
        """
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=UTC)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=UTC)
    if window_center is None:
        window_center = window_start + (window_end - window_start) / 2
    elif window_center.tzinfo is None:
        window_center = window_center.replace(tzinfo=UTC)
    memory_type_list = list(memory_types) or None
    project_list = list(project_scope_identity(projects)) or None
    created_by_list = list(created_by_agent_ids) or None
    event_time_sql = "COALESCE(valid_from, first_seen_at, created_at)"
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND (%s::text[] IS NULL OR domain = ANY(%s::text[]) OR domain = 'unknown')
                  AND (%s::text[] IS NULL OR sensitivity = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR memory_type = ANY(%s::text[]))
                  AND (%s::text[] IS NULL OR ({_MEMORY_PROJECT_SCOPE_SQL}) ?| %s::text[])
                  AND (%s::text[] IS NULL OR created_by_agent_id = ANY(%s::text[]))
                  AND (%s::text IS NULL OR run_id = %s)
                  AND (%s::boolean OR valid_to IS NULL OR valid_to >= clock_timestamp())
                  AND (
                    ({event_time_sql} >= %s::timestamptz AND {event_time_sql} < %s::timestamptz)
                    OR (
                      valid_from IS NOT NULL
                      AND valid_to IS NOT NULL
                      AND valid_from < %s::timestamptz
                      AND valid_to > %s::timestamptz
                    )
                  )
                ORDER BY
                  ABS(EXTRACT(EPOCH FROM ({event_time_sql} - %s::timestamptz))) ASC,
                  updated_at DESC,
                  created_at DESC,
                  id DESC
                LIMIT %s
                """,
        (
            domains,
            domains,
            sensitivity_allowed,
            sensitivity_allowed,
            memory_type_list,
            memory_type_list,
            project_list,
            project_list,
            created_by_list,
            created_by_list,
            run_id,
            run_id,
            include_expired,
            window_start,
            window_end,
            window_end,
            window_start,
            window_center,
            limit,
        ),
    )


def get_memory_by_commit_digest(self, commit_digest: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE commit_digest = %s
                  AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (commit_digest,),
    )


def get_memory_by_confirmation_id(self, confirmation_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE confirmation_id = %s
                  AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (confirmation_id,),
    )


def latest_agentic_commit_memory(self, *, agent_id: str | None = None) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {MEMORY_COLUMNS}
                FROM memories
                WHERE deleted_at IS NULL
                  AND status = 'active'
                  AND metadata_json #>> '{{agentic_memory,kind}}' = 'agentic_memory_commit'
                  AND (
                    %s::text IS NULL
                    OR metadata_json #>> '{{agentic_memory,agent_id}}' = %s
                    OR metadata_json #>> '{{agentic_memory,agent_identity,agent_id}}' = %s
                  )
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
        (agent_id, agent_id, agent_id),
    )


for _memory_method in (
    get_memory_by_key,
    get_memory,
    get_memories_by_ids,
    list_memories_referencing_source,
    list_memories_referencing_sources,
    list_pending_derived_candidates_for_member,
    list_memories,
    list_memories_by_statuses,
    count_memories_by_status,
    list_recent_agentic_commits,
    list_pending_inline_confirmations,
    find_live_memory_by_canonical_text,
    list_memories_for_staleness_sweep,
    count_memories,
    list_rollup_input_memories,
    count_rollup_input_memories,
    list_pending_rollup_candidates,
    list_accepted_rollup_cards,
    search_memories,
    search_memories_fts,
    search_memories_vector,
    search_memories_by_time,
    get_memory_by_commit_digest,
    get_memory_by_confirmation_id,
    latest_agentic_commit_memory,
):
    _memory_method.__module__ = "alicebot_api.vnext_store"
    _memory_method.__qualname__ = f"PostgresVNextStore.{_memory_method.__name__}"
del _memory_method
