"""SQLite memory read and retrieval store seam."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import cast

import numpy as np

from alicebot_api.source_search_limits import literal_match_operand
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    EMBEDDING_VECTOR_DIMENSIONS,
    memory_embedding_signature_is_current,
    pad_embedding_vector,
)
from alicebot_api.vnext_project_scope import (
    GLOBAL_PROJECT_MARKER,
    normalize_project_scope,
    project_scope_identity,
)
from alicebot_api.vnext_stores.retrieval_common import _search_patterns
from alicebot_api.vnext_stores.sqlite.columns import MEMORY_COLUMNS
from alicebot_api.vnext_stores.sqlite.primitives import _iso_or_none
from alicebot_api.vnext_stores.sqlite.query_predicates import (
    _fts_match_any_expression,
    _fts_match_expression,
    CTE_MATERIALIZED_HINT,
    _project_view_partition_sql,
    _stated_exclusion,
    _sqlite_ascii_literal_contains_sql,
)
from alicebot_api.vnext_stores.sqlite.vector_scan import cached_vector_ranked

VNextRow = dict[str, object]

# Statuses admitted by ordinary memory reads. Maintenance-demoted stale,
# superseded, rejected, deleted, and every unknown status remain excluded
# unless a method explicitly selects a different lifecycle state.
_MEMORY_SEARCHABLE_STATUSES_SQL = "('active', 'accepted')"

# Vectorized scan tuning for search_memories_vector. Candidate rows stream in
# chunks this size so the float32 matrix stays bounded (~48 MiB per chunk).
_VECTOR_SCAN_CHUNK_ROWS = 8192

#: Bytes in one stored embedding blob (float32 payload at storage width).
_EMBEDDING_BLOB_BYTES = EMBEDDING_VECTOR_DIMENSIONS * 4

# The vectorized scan ranks candidates with a float64 dot product while the
# reported distance is recomputed per row with the exact float32 BLAS math the
# store has always used (``_embedding_blob_distance``). BLAS sdot's worst-case
# float32 accumulation error over 1536 terms is bounded by ~9.2e-5 relative to
# ||x||*||y|| (n*u with u=2^-24), so any candidate whose approximate distance
# exceeds the k-th verified exact distance by more than this epsilon provably
# cannot belong to the exact top-k. Measured divergence on adversarial
# cancellation-heavy vectors is ~1e-7; 1e-3 keeps a >10x margin on the proof
# bound.
_VECTOR_DISTANCE_REFINE_EPSILON = 1e-3

# Rows whose float32 norm sits near the subnormal range lose the error bound
# above (squares underflow), so they are scored with the exact per-row math
# during the scan instead of the vectorized approximation. Real embeddings
# have O(1) norms; this path exists for adversarial or corrupt blobs.
_VECTOR_TINY_NORM_THRESHOLD = 1e-30


def _embedding_blob_distance(blob: bytes, query_array: np.ndarray, query_norm: float) -> float:
    """Exact cosine distance for one stored embedding blob.

    This is byte-for-byte the scoring math the per-row implementation of
    ``search_memories_vector`` always used: float32 parse, pad/truncate to the
    storage width, float32 BLAS norm and dot, distance 1.0 for zero norms.
    The vectorized scan ranks with an approximation but every returned row's
    ``vector_distance`` comes from this function, so results are bit-identical
    to the historical algorithm.
    """
    vector: np.ndarray = np.frombuffer(blob, dtype=np.float32)
    if vector.size != EMBEDDING_VECTOR_DIMENSIONS:
        resized: np.ndarray = np.zeros(EMBEDDING_VECTOR_DIMENSIONS, dtype=np.float32)
        resized[: min(vector.size, EMBEDDING_VECTOR_DIMENSIONS)] = vector[:EMBEDDING_VECTOR_DIMENSIONS]
        vector = resized
    vector_norm = float(np.linalg.norm(vector))
    if query_norm == 0.0 or vector_norm == 0.0:
        return 1.0
    similarity = float(np.dot(query_array, vector)) / (query_norm * vector_norm)
    return 1.0 - similarity


def get_memory_by_key(
    self,
    *,
    memory_key: str,
    agent_profile_id: str = "assistant_default",
    include_deleted: bool = False,
) -> VNextRow | None:
    """The memory at ``memory_key``, or ``None``.

    A soft-deleted (archived) row is not returned unless ``include_deleted`` is true. It still holds its key:
    the unique index on ``(user, profile, memory_key)`` counts every row, so a caller that reads the key to
    avoid a collision on insert passes ``include_deleted=True`` to see every row the index sees.
    """

    deleted_clause = "" if include_deleted else "AND deleted_at IS NULL"
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND agent_profile_id = ?
                  AND memory_key = ?
                  {deleted_clause}
                LIMIT 1
                """,
        (self.user_id, agent_profile_id, memory_key),
    )


def get_memory(self, memory_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (str(memory_id), self.user_id),
    )


def get_memories_by_ids(self, memory_ids: Sequence[str]) -> list[VNextRow]:
    ids = list(dict.fromkeys(str(memory_id) for memory_id in memory_ids if memory_id))
    if not ids:
        return []
    placeholders = self._placeholders(ids)
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND id IN ({placeholders})
                """,
        (self.user_id, *ids),
    )


def list_memories_referencing_source(self, *, source_id: str, limit: int = 500) -> list[VNextRow]:
    """Bound memories related to one source, including provenance links."""

    if limit < 1:
        raise ValueError("limit must be positive")
    qualified_columns = ", ".join(f"m.{column}" for column in MEMORY_COLUMNS)
    return self._fetch_all(
        f"""
                SELECT {qualified_columns}
                FROM memories AS m
                WHERE m.user_id = ?
                  AND m.deleted_at IS NULL
                  AND (
                    EXISTS (
                      SELECT 1 FROM json_each(m.source_event_ids) AS source_event
                      WHERE CAST(source_event.value AS TEXT) = ?
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM provenance_links AS p
                      WHERE p.user_id = m.user_id
                        AND p.target_type = 'memory'
                        AND p.target_id = m.id
                        AND p.source_id = ?
                    )
                    OR EXISTS (
                      SELECT 1 FROM json_tree(m.metadata_json) AS ref
                      WHERE ref.key IN (
                        'source_id', 'source_ids', 'source_ref', 'source_refs',
                        'source_references', 'selected_source_ids'
                      )
                        AND CAST(ref.value AS TEXT) IN (?, ?)
                    )
                  )
                ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC
                LIMIT ?
                """,
        (self.user_id, source_id, source_id, source_id, f"source:{source_id}", limit),
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
    applies, this user's rows and ``deleted_at IS NULL``. Nothing here reads a
    memory the one-source method would not have returned.

    The one-source method parses the JSON of every memory row on each call, so
    asking once per packed source costs one table scan per source. This reads
    the memories table once: each of the three reference kinds yields
    ``(source id, memory id)`` pairs filtered to the requested ids, and a
    window function applies the cap per source. ``limit_per_source`` has no
    default because the callers disagree about the cap.
    """

    if limit_per_source < 1:
        raise ValueError("limit must be positive")
    ids = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
    grouped: dict[str, list[VNextRow]] = {source_id: [] for source_id in ids}
    if not ids:
        return grouped
    qualified_columns = ", ".join(f"m.{column}" for column in MEMORY_COLUMNS)
    rows = self._fetch_all(
        f"""
                WITH wanted(source_id) AS (
                  SELECT CAST(value AS TEXT) FROM json_each(?)
                ),
                hits(ref_source_id, memory_id) AS (
                  SELECT CAST(source_event.value AS TEXT), m.id
                  FROM memories AS m, json_each(m.source_event_ids) AS source_event
                  WHERE m.user_id = ?
                    AND m.deleted_at IS NULL
                    AND CAST(source_event.value AS TEXT) IN (SELECT source_id FROM wanted)
                  UNION
                  SELECT p.source_id, p.target_id
                  FROM provenance_links AS p
                  WHERE p.user_id = ?
                    AND p.target_type = 'memory'
                    AND p.source_id IN (SELECT source_id FROM wanted)
                  UNION
                  SELECT CAST(ref.value AS TEXT), m.id
                  FROM memories AS m, json_tree(m.metadata_json) AS ref
                  WHERE m.user_id = ?
                    AND m.deleted_at IS NULL
                    AND ref.key IN (
                      'source_id', 'source_ids', 'source_ref', 'source_refs',
                      'source_references', 'selected_source_ids'
                    )
                    AND CAST(ref.value AS TEXT) IN (SELECT source_id FROM wanted)
                  UNION
                  SELECT substr(CAST(ref.value AS TEXT), 8), m.id
                  FROM memories AS m, json_tree(m.metadata_json) AS ref
                  WHERE m.user_id = ?
                    AND m.deleted_at IS NULL
                    AND ref.key IN (
                      'source_id', 'source_ids', 'source_ref', 'source_refs',
                      'source_references', 'selected_source_ids'
                    )
                    AND substr(CAST(ref.value AS TEXT), 1, 7) = 'source:'
                    AND substr(CAST(ref.value AS TEXT), 8) IN (SELECT source_id FROM wanted)
                ),
                ranked(ref_source_id, memory_id, rank_in_source) AS (
                  SELECT h.ref_source_id, m.id,
                         ROW_NUMBER() OVER (
                           PARTITION BY h.ref_source_id
                           ORDER BY m.updated_at DESC, m.created_at DESC, m.id DESC
                         )
                  FROM hits AS h
                  JOIN memories AS m
                    ON m.id = h.memory_id
                   AND m.user_id = ?
                   AND m.deleted_at IS NULL
                )
                SELECT r.ref_source_id AS ref_source_id, {qualified_columns}
                FROM ranked AS r
                JOIN memories AS m ON m.id = r.memory_id
                WHERE r.rank_in_source <= ?
                ORDER BY r.ref_source_id, r.rank_in_source
                """,
        (
            json.dumps(ids),
            self.user_id,
            self.user_id,
            self.user_id,
            self.user_id,
            self.user_id,
            limit_per_source,
        ),
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
    """Return pending derived candidates whose reviewed input is member_id.

    ``get_memory_for_update`` acquires SQLite's database writer lock on
    lifecycle paths before this query runs, so the returned candidates
    stay stable for the enclosing transaction.
    """
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories AS candidate
                WHERE candidate.user_id = ?
                  AND candidate.deleted_at IS NULL
                  AND candidate.status IN ('candidate', 'needs_review')
                  AND (? IS NULL OR candidate.id <> ?)
                  AND EXISTS (
                    SELECT 1
                    FROM json_each(
                      CASE
                        WHEN json_valid(candidate.metadata_json)
                        THEN candidate.metadata_json
                        ELSE '{{}}'
                      END,
                      '$.consolidation.member_snapshots'
                    ) AS snapshot
                    WHERE CAST(json_extract(snapshot.value, '$.id') AS TEXT) = ?
                  )
                ORDER BY candidate.id
                """,
        (self.user_id, exclude_memory_id, exclude_memory_id, str(member_id)),
    )


def get_memory_by_commit_digest(self, commit_digest: str) -> VNextRow | None:
    """Indexed idempotency lookup; without this the commit service falls
    back to a Python full-table scan (measured: 18ms -> 222ms at 10k
    memories in the scale benchmark)."""
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE commit_digest = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (str(commit_digest), self.user_id),
    )


def latest_agentic_commit_memory(self, *, agent_id: str | None = None) -> VNextRow | None:
    """Fast lookup for the id-less undo path ("undo my last commit").

    Without this the commit service duck-type falls back to a
    full-table Python scan. Mirrors the Postgres jsonb path checks via
    json_extract.
    """
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status = 'active'
                  AND json_extract(metadata_json, '$.agentic_memory.kind') = 'agentic_memory_commit'
                  AND (
                    ? IS NULL
                    OR json_extract(metadata_json, '$.agentic_memory.agent_id') = ?
                    OR json_extract(metadata_json, '$.agentic_memory.agent_identity.agent_id') = ?
                  )
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
        (self.user_id, agent_id, agent_id, agent_id),
    )


def get_memory_by_confirmation_id(self, confirmation_id: str) -> VNextRow | None:
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE confirmation_id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
        (str(confirmation_id), self.user_id),
    )


def _memory_list_clauses(
    self,
    *,
    status: str | None,
    statuses: Sequence[str] | None,
    memory_types: Sequence[str] | None,
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
    created_at_start: datetime | None,
    created_at_end: datetime | None,
    query: str | None,
    include_expired: bool,
) -> tuple[str, list[object], str, list[object]] | None:
    """The filters of ``list_memories`` in two pieces, or ``None`` when they admit nothing.

    ``head`` runs from the user through the sensitivity ceiling and ``tail`` holds
    the time window, the literal query and the expiry test. The project fence goes
    between them, so ``list_memories`` and the single-scan partition read build one
    set of filters and cannot drift apart.
    """

    status_sql = ""
    params: list[object] = [self.user_id]
    if status is not None:
        status_sql = " AND status = ?"
        params.append(status)
    statuses_sql = ""
    if statuses is not None:
        normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
        if not normalized_statuses:
            return None
        statuses_sql = f" AND status IN ({self._placeholders(normalized_statuses)})"
        params.extend(normalized_statuses)
    memory_types_sql = ""
    if memory_types is not None:
        normalized_memory_types = list(dict.fromkeys(str(value) for value in memory_types if str(value)))
        if not normalized_memory_types:
            return None
        memory_types_sql = f" AND memory_type IN ({self._placeholders(normalized_memory_types)})"
        params.extend(normalized_memory_types)
    domains_sql = ""
    if domains:
        domain_placeholders = ", ".join("?" for _domain in domains)
        domains_sql = f" AND (domain IN ({domain_placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_sql = ""
    if sensitivity_allowed is not None:
        if not sensitivity_allowed:
            return None
        sensitivity_placeholders = ", ".join("?" for _sensitivity in sensitivity_allowed)
        sensitivity_sql = f" AND COALESCE(sensitivity, 'unknown') IN ({sensitivity_placeholders})"
        params.extend(sensitivity_allowed)
    head_sql = (
        f"WHERE user_id = ?{status_sql}{statuses_sql}{memory_types_sql}"
        f"\n                  AND deleted_at IS NULL"
        f"\n                  {domains_sql}"
        f"\n                  {sensitivity_sql}"
    )
    tail_params: list[object] = []
    created_at_sql = ""
    if created_at_start is not None:
        created_at_sql += " AND julianday(created_at) >= julianday(?)"
        tail_params.append(_iso_or_none(created_at_start))
    if created_at_end is not None:
        created_at_sql += " AND julianday(created_at) <= julianday(?)"
        tail_params.append(_iso_or_none(created_at_end))
    query_sql = ""
    if query is not None:
        normalized_query = str(query).strip()
        if normalized_query:
            query_sql = (
                f" AND ({_sqlite_ascii_literal_contains_sql("COALESCE(title, '')")}"
                f" OR {_sqlite_ascii_literal_contains_sql("COALESCE(canonical_text, '')")}"
                f" OR {_sqlite_ascii_literal_contains_sql("COALESCE(summary, '')")})"
            )
            escaped_query = literal_match_operand(normalized_query)
            tail_params.extend((escaped_query, escaped_query, escaped_query))
    expiry_sql, expiry_params = self._expiry_clause(include_expired)
    tail_params.extend(expiry_params)
    tail_sql = f"{created_at_sql}\n                  {query_sql}\n                  {expiry_sql}"
    return head_sql, params, tail_sql, tail_params


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
    exclude_global_domains: Sequence[str] | None = None,
) -> list[VNextRow]:
    """List memories. ``include_expired=False`` leaves out a memory whose ``valid_to`` has passed.

    The default keeps the management reads (review, confirm, unexpire, export)
    seeing an expired memory. A read an agent or a brief shows to a person passes
    ``False``, which applies recall's own test (``_expiry_clause``) before ``LIMIT``.

    ``projects`` may hold the reserved global marker (spec 6.1). With it,
    ``exclude_global_domains`` leaves out global memories in those domains before
    ``LIMIT``, so a held-back row never uses up a place. It has no effect on a
    request that does not hold the marker. With the marker it must be stated, an
    empty tuple when nothing is left out: ``None`` raises.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    clauses = _memory_list_clauses(
        self,
        status=status,
        statuses=statuses,
        memory_types=memory_types,
        domains=domains,
        sensitivity_allowed=sensitivity_allowed,
        created_at_start=created_at_start,
        created_at_end=created_at_end,
        query=query,
        include_expired=include_expired,
    )
    if clauses is None:
        return []
    head_sql, head_params, tail_sql, tail_params = clauses
    project_sql, project_params = self._project_clause(
        tuple(normalize_project_scope(projects or ())),
        global_excluded_domains=_stated_exclusion(exclude_global_domains),
    )
    params = [*head_params, *project_params, *tail_params]
    order_sql = (
        "ORDER BY created_at DESC, id DESC"
        if order_by_created_at
        else "ORDER BY updated_at DESC, created_at DESC, id DESC"
    )
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                {head_sql}
                  {project_sql}
                  {tail_sql}
                {order_sql}
                {limit_sql}
                """,
        tuple(params),
    )


def list_memories_view_partitions(
    self,
    *,
    project_ids: Sequence[str],
    exclude_global_domains: Sequence[str],
    per_partition_limit: int,
    # The domain filter and the sensitivity ceiling have no defaults: a caller that
    # forgets one would read every domain or every sensitivity. ``None`` is written
    # at the call site when a filter is meant to be absent.
    domains: list[str] | None,
    sensitivity_allowed: list[str] | None,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    memory_types: Sequence[str] | None = None,
    created_at_start: datetime | None = None,
    created_at_end: datetime | None = None,
    query: str | None = None,
    order_by_created_at: bool = False,
    include_expired: bool = True,
) -> tuple[list[VNextRow], list[VNextRow]]:
    """The project's memories and the global memories in one scan (spec 6.2, 12).

    Returns ``(project_rows, global_rows)``, each newest first and at most
    ``per_partition_limit`` long, under the same filters as ``list_memories``.
    ``project_ids`` are Alice project ids. A memory is global when its scope holds
    no Alice project id, and global memories in ``exclude_global_domains`` are left
    out before the limit. It adds no column, no index and no table.

    One pass over the table labels every row once (1 for the project, 0 for global,
    NULL for a row outside the view) into a narrow common table expression: the row
    id, the ordering columns and the label. The expression is read twice, once per
    label, each read a top-N by the list's own order, and the winning ids are joined
    back to the table for their full rows. Nothing wide is sorted. SQLite does not
    share a common table expression between two references by itself, so the
    expression carries ``AS MATERIALIZED`` (``CTE_MATERIALIZED_HINT``) and the label,
    a Python call per row that holds an Alice id, runs once per row: a test counts
    the calls. Two ordinary queries scan the table twice, and a window function over
    the labelled rows measured slower than both (spec 12).
    """

    if per_partition_limit < 1:
        raise ValueError("limit must be positive")
    clauses = _memory_list_clauses(
        self,
        status=status,
        statuses=statuses,
        memory_types=memory_types,
        domains=domains,
        sensitivity_allowed=sensitivity_allowed,
        created_at_start=created_at_start,
        created_at_end=created_at_end,
        query=query,
        include_expired=include_expired,
    )
    if clauses is None:
        return [], []
    head_sql, head_params, tail_sql, tail_params = clauses
    partition_sql, partition_params = _project_view_partition_sql(
        placeholders=self._placeholders,
        projects=(*project_ids, GLOBAL_PROJECT_MARKER),
        scope_expression="alice_project_scope_identity(metadata_json, project_id)",
        text_expressions=("metadata_json", "project_id"),
        domain_expression="domain",
        global_excluded_domains=tuple(sorted(exclude_global_domains)),
    )
    ordering = ("created_at",) if order_by_created_at else ("updated_at", "created_at")
    order_columns = ", ".join(ordering)
    order_sql = ", ".join(f"{column} DESC" for column in ordering) + ", id DESC"
    joined_order_sql = ", ".join(f"m.{column} DESC" for column in ordering) + ", m.id DESC"
    rows = self._fetch_all(
        f"""
                WITH alice_labelled AS{CTE_MATERIALIZED_HINT} (
                  SELECT id, {order_columns}, {partition_sql} AS alice_partition
                  FROM memories
                  {head_sql}
                    {tail_sql}
                )
                SELECT {", ".join(f"m.{column}" for column in MEMORY_COLUMNS)},
                       picked.alice_partition AS alice_partition
                FROM (
                  SELECT id, alice_partition FROM (
                    SELECT id, alice_partition FROM alice_labelled
                    WHERE alice_partition = 1
                    ORDER BY {order_sql}
                    LIMIT ?
                  )
                  UNION ALL
                  SELECT id, alice_partition FROM (
                    SELECT id, alice_partition FROM alice_labelled
                    WHERE alice_partition = 0
                    ORDER BY {order_sql}
                    LIMIT ?
                  )
                ) AS picked
                JOIN memories AS m ON m.id = picked.id
                ORDER BY picked.alice_partition DESC, {joined_order_sql}
                """,
        (*partition_params, *head_params, *tail_params, per_partition_limit, per_partition_limit),
    )
    project_rows: list[VNextRow] = []
    global_rows: list[VNextRow] = []
    for row in rows:
        partition = row.pop("alice_partition")
        (project_rows if partition == 1 else global_rows).append(row)
    return project_rows, global_rows


def list_memories_by_statuses(
    self,
    *,
    statuses: Sequence[str],
    domains: list[str] | None = None,
    sensitivity_allowed: list[str] | None = None,
    limit: int = 30,
) -> list[VNextRow]:
    """Bounded multi-status memory query for workspace/review surfaces."""
    if limit < 1:
        raise ValueError("limit must be positive")
    normalized_statuses = list(dict.fromkeys(str(value) for value in statuses if str(value)))
    if not normalized_statuses:
        return []
    if sensitivity_allowed is not None and not sensitivity_allowed:
        return []
    status_placeholders = self._placeholders(normalized_statuses)
    params: list[object] = [self.user_id, *normalized_statuses]
    domains_sql = ""
    if domains:
        domain_placeholders = self._placeholders(domains)
        domains_sql = f" AND (domain IN ({domain_placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_sql = ""
    if sensitivity_allowed is not None:
        sensitivity_placeholders = self._placeholders(sensitivity_allowed)
        sensitivity_sql = f" AND COALESCE(sensitivity, 'unknown') IN ({sensitivity_placeholders})"
        params.extend(sensitivity_allowed)
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN ({status_placeholders})
                  {domains_sql}
                  {sensitivity_sql}
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
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
    params: list[object] = [self.user_id]
    domains_sql = ""
    if domains:
        placeholders = self._placeholders(domains)
        domains_sql = f" AND (domain IN ({placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_sql = ""
    if sensitivity_allowed is not None:
        placeholders = self._placeholders(sensitivity_allowed)
        sensitivity_sql = f" AND COALESCE(sensitivity, 'unknown') IN ({placeholders})"
        params.extend(sensitivity_allowed)
    rows = self._fetch_all(
        f"""
                SELECT status, COUNT(*) AS count
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  {domains_sql}
                  {sensitivity_sql}
                GROUP BY status
                ORDER BY status
                """,
        tuple(params),
    )
    return {str(row["status"]): int(cast(int, row["count"])) for row in rows}


def list_recent_agentic_commits(self, *, limit: int = 20) -> list[VNextRow]:
    if limit < 1:
        raise ValueError("limit must be positive")
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND json_extract(metadata_json, '$.agentic_memory.kind') = 'agentic_memory_commit'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
        (self.user_id, limit),
    )


def list_pending_inline_confirmations(self, *, limit: int = 20) -> list[VNextRow]:
    if limit < 1:
        raise ValueError("limit must be positive")
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status = 'needs_review'
                  AND confirmation_status = 'unconfirmed'
                  AND json_extract(metadata_json, '$.agentic_memory.kind') = 'agentic_memory_commit'
                  AND json_extract(metadata_json, '$.agentic_memory.confirmation.status') = 'pending'
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
        (self.user_id, limit),
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
    normalized_scope = project_scope_identity(project_scope)
    return self._fetch_optional_one(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN ('candidate', 'active', 'accepted', 'needs_review', 'private_only')
                  AND lower(canonical_text) = lower(?)
                  AND domain = ?
                  AND sensitivity = ?
                  AND alice_project_scope_identity(metadata_json, project_id) = ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
        (
            self.user_id,
            normalized_text,
            str(domain),
            str(sensitivity),
            json.dumps(normalized_scope, ensure_ascii=False, separators=(",", ":")),
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
    if limit < 1:
        raise ValueError("limit must be positive")
    memory_types = list(dict.fromkeys(str(value) for value in review_memory_types if str(value)))
    placeholders = self._placeholders(memory_types)
    project_sql, project_params = self._project_clause(tuple(normalize_project_scope(projects or ())))
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status = 'active'
                  {project_sql}
                  AND (
                    (valid_to IS NOT NULL AND julianday(valid_to) < julianday(?))
                    OR (
                      memory_type IN ({placeholders})
                      AND julianday(COALESCE(last_confirmed_at, last_seen_at, created_at)) < julianday(?)
                    )
                  )
                ORDER BY
                  CASE WHEN valid_to IS NOT NULL AND julianday(valid_to) < julianday(?) THEN 0 ELSE 1 END,
                  julianday(COALESCE(valid_to, last_confirmed_at, last_seen_at, created_at)) ASC,
                  id ASC
                LIMIT ?
                """,
        (
            self.user_id,
            *project_params,
            reference_time.isoformat(),
            *memory_types,
            confirmation_before.isoformat(),
            reference_time.isoformat(),
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
    params: list[object] = [self.user_id]
    status_sql = ""
    if status is not None:
        status_sql = " AND status = ?"
        params.append(status)
    domains_sql = ""
    if domains:
        placeholders = ", ".join("?" for _domain in domains)
        domains_sql = f" AND (domain IN ({placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_sql = ""
    if sensitivity_allowed is not None:
        if not sensitivity_allowed:
            return 0
        placeholders = ", ".join("?" for _value in sensitivity_allowed)
        sensitivity_sql = f" AND COALESCE(sensitivity, 'unknown') IN ({placeholders})"
        params.extend(sensitivity_allowed)
    project_sql, project_params = self._project_clause(tuple(normalize_project_scope(projects or ())))
    params.extend(project_params)
    expiry_sql, expiry_params = self._expiry_clause(include_expired)
    params.extend(expiry_params)
    row = self._fetch_one(
        "count memories",
        f"""
                SELECT COUNT(*) AS count
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  {status_sql}
                  {domains_sql}
                  {sensitivity_sql}
                  {project_sql}
                  {expiry_sql}
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
    params: list[object] = [self.user_id, excluded_candidate_kind]
    domains_sql = ""
    if domains:
        placeholders = ", ".join("?" for _domain in domains)
        domains_sql = f" AND (domain IN ({placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_placeholders = ", ".join("?" for _value in sensitivity_allowed)
    params.extend(sensitivity_allowed)
    project_sql, project_params = self._project_clause(tuple(normalize_project_scope(projects or ())))
    params.extend(project_params)
    # A roll-up groups memories that recall can return, so an expired one is
    # left out here, before LIMIT, the way recall leaves it out.
    expiry_sql, expiry_params = self._expiry_clause(False)
    params.extend(expiry_params)
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND COALESCE(json_extract(metadata_json, '$.candidate_kind'), '') <> ?
                  {domains_sql}
                  AND COALESCE(sensitivity, 'unknown') IN ({sensitivity_placeholders})
                  {project_sql}
                  {expiry_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
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
    params: list[object] = [self.user_id, excluded_candidate_kind]
    domains_sql = ""
    if domains:
        placeholders = ", ".join("?" for _domain in domains)
        domains_sql = f" AND (domain IN ({placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_placeholders = ", ".join("?" for _value in sensitivity_allowed)
    params.extend(sensitivity_allowed)
    project_sql, project_params = self._project_clause(tuple(normalize_project_scope(projects or ())))
    params.extend(project_params)
    expiry_sql, expiry_params = self._expiry_clause(False)
    params.extend(expiry_params)
    row = self._fetch_one(
        "count rollup input memories",
        f"""
                SELECT COUNT(*) AS count
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                  AND COALESCE(json_extract(metadata_json, '$.candidate_kind'), '') <> ?
                  {domains_sql}
                  AND COALESCE(sensitivity, 'unknown') IN ({sensitivity_placeholders})
                  {project_sql}
                  {expiry_sql}
                """,
        tuple(params),
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
    digest_placeholders = ", ".join("?" for _digest in unique_digests)
    params: list[object] = [self.user_id, candidate_kind, *unique_digests]
    domains_sql = ""
    if domains:
        placeholders = ", ".join("?" for _domain in domains)
        domains_sql = f" AND (domain IN ({placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_placeholders = ", ".join("?" for _value in sensitivity_allowed)
    params.extend(sensitivity_allowed)
    project_sql, project_params = self._project_clause(tuple(normalize_project_scope(projects or ())))
    params.extend(project_params)
    params.append(bounded_limit)
    return self._fetch_all(
        f"""
                WITH ranked_rollups AS (
                  SELECT {", ".join(MEMORY_COLUMNS)},
                         ROW_NUMBER() OVER (
                           PARTITION BY json_extract(metadata_json, '$.rollup_digest')
                           ORDER BY updated_at DESC, created_at DESC, id DESC
                         ) AS rollup_rank
                  FROM memories
                  WHERE user_id = ?
                    AND deleted_at IS NULL
                    AND status = 'candidate'
                    AND json_extract(metadata_json, '$.candidate_kind') = ?
                    AND json_extract(metadata_json, '$.rollup_digest') IN ({digest_placeholders})
                    {domains_sql}
                    AND COALESCE(sensitivity, 'unknown') IN ({sensitivity_placeholders})
                    {project_sql}
                )
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM ranked_rollups
                WHERE rollup_rank = 1
                ORDER BY json_extract(metadata_json, '$.rollup_digest'), updated_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
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
    key_placeholders = ", ".join("?" for _key in unique_keys)
    params: list[object] = [self.user_id, candidate_kind, *unique_keys]
    domains_sql = ""
    if domains:
        placeholders = ", ".join("?" for _domain in domains)
        domains_sql = f" AND (domain IN ({placeholders}) OR domain = 'unknown')"
        params.extend(domains)
    sensitivity_placeholders = ", ".join("?" for _value in sensitivity_allowed)
    params.extend(sensitivity_allowed)
    project_sql, project_params = self._project_clause(tuple(normalize_project_scope(projects or ())))
    params.extend(project_params)
    # An expired card is not the accepted card for its topic. The test sits
    # inside the ranking query, so an older card that is still open is ranked
    # and returned when the newest one has expired.
    expiry_sql, expiry_params = self._expiry_clause(False)
    params.extend(expiry_params)
    params.append(bounded_limit)
    return self._fetch_all(
        f"""
                WITH ranked_rollups AS (
                  SELECT {", ".join(MEMORY_COLUMNS)},
                         ROW_NUMBER() OVER (
                           PARTITION BY json_extract(metadata_json, '$.rollup_key')
                           ORDER BY
                             CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                             updated_at DESC,
                             created_at DESC,
                             id DESC
                         ) AS rollup_rank
                  FROM memories
                  WHERE user_id = ?
                    AND deleted_at IS NULL
                    AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}
                    AND json_extract(metadata_json, '$.candidate_kind') = ?
                    AND json_extract(metadata_json, '$.rollup_key') IN ({key_placeholders})
                    {domains_sql}
                    AND COALESCE(sensitivity, 'unknown') IN ({sensitivity_placeholders})
                    {project_sql}
                    {expiry_sql}
                )
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM ranked_rollups
                WHERE rollup_rank = 1
                ORDER BY json_extract(metadata_json, '$.rollup_key'), updated_at DESC, id DESC
                LIMIT ?
                """,
        tuple(params),
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
    patterns = [pattern.casefold() for pattern in _search_patterns(query)]
    exact_pattern = patterns[0]
    domain_sql, domain_params = self._domain_clause(domains)
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed)
    type_sql, type_params = self._memory_type_clause(memory_types)
    project_sql, project_params = self._project_clause(projects)
    created_by_sql, created_by_params = self._created_by_clause(created_by_agent_ids)
    run_sql, run_params = self._run_clause(run_id)
    expiry_sql, expiry_params = self._expiry_clause(include_expired)
    count = len(patterns)
    match_columns = ("memory_key", "title", "canonical_text", "summary", "value")
    match_sql = " OR ".join(self._like_any(column, count) for column in match_columns)
    params: list[object] = [self.user_id]
    params.extend(domain_params)
    params.extend(sensitivity_params)
    params.extend(type_params)
    params.extend(project_params)
    params.extend(created_by_params)
    params.extend(run_params)
    params.extend(expiry_params)
    for _column in match_columns:
        params.extend(patterns)
    params.append(exact_pattern)
    params.append(exact_pattern)
    params.extend(patterns)
    params.extend(patterns)
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}{domain_sql}{sensitivity_sql}{type_sql}{project_sql}{created_by_sql}{run_sql}{expiry_sql}
                  AND ({match_sql})
                ORDER BY
                  CASE
                    WHEN LOWER(COALESCE(canonical_text, '')) LIKE ? THEN 0
                    WHEN LOWER(COALESCE(title, '')) LIKE ? THEN 1
                    WHEN {self._like_any("canonical_text", count)} THEN 2
                    WHEN {self._like_any("title", count)} THEN 3
                    ELSE 4
                  END,
                  updated_at DESC,
                  created_at DESC,
                  id DESC
                LIMIT ?
                """,
        tuple(params),
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
    # Strict pass ANDs every non-stopword term (websearch parity);
    # match_any (the retrieval OR-fallback) ORs them instead so a
    # natural-language question still reaches keyword-findable memories
    # when the AND pass returns nothing.
    match_expression = _fts_match_any_expression(query) if match_any else _fts_match_expression(query)
    if match_expression is None:
        return []
    domain_sql, domain_params = self._domain_clause(domains, prefix="m.")
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed, prefix="m.")
    type_sql, type_params = self._memory_type_clause(memory_types, prefix="m.")
    project_sql, project_params = self._project_clause(projects, prefix="m.")
    created_by_sql, created_by_params = self._created_by_clause(created_by_agent_ids, prefix="m.")
    run_sql, run_params = self._run_clause(run_id, prefix="m.")
    expiry_sql, expiry_params = self._expiry_clause(include_expired, prefix="m.")
    scope_sql, scope_params = self._retrieval_scope_clause(
        scope_thread_id=scope_thread_id,
        scope_task_id=scope_task_id,
        scope_people=scope_people,
        scope_person_memory_ids=scope_person_memory_ids,
        scope_window_start=scope_window_start,
        scope_window_end=scope_window_end,
        prefix="m.",
    )
    prefixed_columns = ", ".join(f"m.{column}" for column in MEMORY_COLUMNS)
    params: list[object] = [match_expression, self.user_id]
    params.extend(domain_params)
    params.extend(sensitivity_params)
    params.extend(type_params)
    params.extend(project_params)
    params.extend(created_by_params)
    params.extend(run_params)
    params.extend(expiry_params)
    params.extend(scope_params)
    params.append(limit)
    try:
        # Column weights follow the Postgres search_tsv setweights:
        # title 1.0 (A), canonical_text 0.4 (B), summary 0.2 (C),
        # memory_key 0.4, and derived fact_keys 0.1 (D) -- fact keys
        # make rows findable without outranking direct text matches.
        return self._fetch_all(
            f"""
                    SELECT {prefixed_columns},
                      -bm25(memories_fts, 1.0, 0.4, 0.2, 0.4, 0.1) AS fts_score
                    FROM memories_fts
                    JOIN memories m ON m.rowid = memories_fts.rowid
                    WHERE memories_fts MATCH ?
                      AND m.user_id = ?
                      AND m.deleted_at IS NULL
                      AND m.status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}{domain_sql}{sensitivity_sql}{type_sql}{project_sql}{created_by_sql}{run_sql}{expiry_sql}{scope_sql}
                    ORDER BY fts_score DESC, m.updated_at DESC, m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
            tuple(params),
        )
    except sqlite3.OperationalError as exc:  # pragma: no cover - sanitizer backstop
        if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
            return []
        raise


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
    if not query_vector:
        raise ContinuityStoreInvariantError("embedding vectors must not be empty")
    padded = pad_embedding_vector(query_vector)
    query_array: np.ndarray = np.asarray(padded, dtype=np.float32)
    query_norm = float(np.linalg.norm(query_array))
    domain_sql, domain_params = self._domain_clause(domains)
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed)
    type_sql, type_params = self._memory_type_clause(memory_types)
    project_sql, project_params = self._project_clause(projects)
    created_by_sql, created_by_params = self._created_by_clause(created_by_agent_ids)
    run_sql, run_params = self._run_clause(run_id)
    expiry_sql, expiry_params = self._expiry_clause(include_expired)
    scope_sql, scope_params = self._retrieval_scope_clause(
        scope_thread_id=scope_thread_id,
        scope_task_id=scope_task_id,
        scope_people=scope_people,
        scope_person_memory_ids=scope_person_memory_ids,
        scope_window_start=scope_window_start,
        scope_window_end=scope_window_end,
    )
    signature_sql = ""
    signature_params: list[object] = []
    if embedding_provider is not None or embedding_model is not None:
        if not embedding_provider or not embedding_model:
            raise ContinuityStoreInvariantError("embedding_provider and embedding_model must be supplied together")
        signature_sql = " AND json_extract(metadata_json, ?) = ? AND json_extract(metadata_json, ?) = ?"
        signature_params.extend(
            (
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.provider",
                embedding_provider,
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.model",
                embedding_model,
            )
        )
        if embedding_endpoint is not None:
            # Only compare vectors from the same endpoint fingerprint, so
            # distinct coordinate spaces sharing provider/model labels are
            # never pooled.
            signature_sql += " AND json_extract(metadata_json, ?) = ?"
            signature_params.extend(
                (
                    f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.endpoint",
                    embedding_endpoint,
                )
            )
        if embedding_signature_version is not None:
            signature_sql += " AND json_extract(metadata_json, ?) = ?"
            signature_params.extend(
                (
                    f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.version",
                    embedding_signature_version,
                )
            )
    base_params: list[object] = [self.user_id]
    base_params.extend(domain_params)
    base_params.extend(sensitivity_params)
    base_params.extend(type_params)
    base_params.extend(project_params)
    base_params.extend(created_by_params)
    base_params.extend(run_params)
    base_params.extend(expiry_params)
    base_params.extend(scope_params)
    params: list[object] = [*base_params, *signature_params]
    # Full predicate set shared by BOTH phases. No transaction spans the scan
    # and the hydrate (autocommit; per-statement snapshots), so a concurrent
    # writer can commit between them; re-applying every scan predicate --
    # including ``embedding IS NOT NULL`` -- in the hydrate SELECT means a row
    # mutated in that window (soft-deleted, lifecycle-demoted, embedding
    # cleared, re-scoped) simply hydrates to no row and is skipped, exactly as
    # if the single-SELECT implementation had never seen it.
    base_predicate_sql = f"""user_id = ?
                  AND deleted_at IS NULL
                  AND embedding IS NOT NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}{domain_sql}{sensitivity_sql}{type_sql}{project_sql}{created_by_sql}{run_sql}{expiry_sql}{scope_sql}"""
    predicate_sql = f"{base_predicate_sql}{signature_sql}"
    # Step 1a -- resident vector cache (Stage 2). When enabled and within
    # its byte cap, the ranking comes from the process-local float32 matrix
    # validated against the one-row embedding_stamp token; the candidate SQL
    # runs ids-only with the scan's FULL predicate set -- including the
    # signature json_extract clauses -- so every predicate reads the live
    # row on every query (a metadata_json signature rewrite can never go
    # stale in the cache; only vectors are resident).
    # ``None`` means the cache must not serve (disabled, :memory:, over-cap,
    # stamp unavailable) and the stateless scan below runs unchanged. Either
    # way, Step 2 recomputes every returned distance from the hydrated
    # row's own blob, so results are bit-identical across both paths.
    maybe_ranked = cached_vector_ranked(
        self,
        predicate_sql=predicate_sql,
        predicate_params=tuple(params),
        query_array=query_array,
        query_norm=query_norm,
        tiny_norm_threshold=_VECTOR_TINY_NORM_THRESHOLD,
    )
    if maybe_ranked is not None:
        ranked: list[tuple[float, str, str]] = maybe_ranked
    else:
        # Step 1b -- stateless vectorized scan. Only (id, updated_at,
        # embedding) stream out of SQLite, as raw tuples (no dict/JSON decode
        # per row); the full rows are hydrated later for just the ranked head.
        cursor = self._execute(
            f"""
                SELECT id, updated_at, embedding
                FROM memories
                WHERE {predicate_sql}
                """,
            tuple(params),
        )
        cursor.row_factory = None  # bypass any installed dict row factory
        query64: np.ndarray = query_array.astype(np.float64)
        # (approximate_distance, updated_at_key, id) per candidate row.
        ranked = []
        while True:
            chunk = cursor.fetchmany(_VECTOR_SCAN_CHUNK_ROWS)
            if not chunk:
                break
            conforming: list[tuple[object, object, bytes]] = []
            for memory_id, updated_at, blob in chunk:
                if isinstance(blob, bytes) and len(blob) == _EMBEDDING_BLOB_BYTES:
                    conforming.append((memory_id, updated_at, blob))
                else:
                    # Non-storage-width blobs take the exact pad/truncate path.
                    distance = _embedding_blob_distance(cast(bytes, blob), query_array, query_norm)
                    ranked.append((distance, str(updated_at or ""), str(memory_id or "")))
            if not conforming:
                continue
            if query_norm == 0.0:
                ranked.extend(
                    (1.0, str(updated_at or ""), str(memory_id or "")) for memory_id, updated_at, _blob in conforming
                )
                continue
            matrix: np.ndarray = np.frombuffer(
                b"".join(blob for _memory_id, _updated_at, blob in conforming),
                dtype=np.float32,
            ).reshape(-1, EMBEDDING_VECTOR_DIMENSIONS)
            norms: np.ndarray = np.linalg.norm(matrix, axis=1)
            dots: np.ndarray = matrix.astype(np.float64) @ query64
            norms64: np.ndarray = norms.astype(np.float64)
            for position, (memory_id, updated_at, blob) in enumerate(conforming):
                if norms[position] < _VECTOR_TINY_NORM_THRESHOLD:
                    # Zero or near-subnormal norms: the vectorized error bound
                    # does not hold, so score with the exact per-row math.
                    distance = _embedding_blob_distance(blob, query_array, query_norm)
                else:
                    distance = 1.0 - float(dots[position]) / (query_norm * float(norms64[position]))
                ranked.append((distance, str(updated_at or ""), str(memory_id or "")))
        ranked.sort()
    # Step 2 -- hydrate full rows for the ranked head in batches, verify the
    # embedding signature when signature filtering is active (stale rows are
    # skipped without consuming limit slots; the walk refills from further
    # down the ranking), and recompute the exact per-row distance for every
    # hydrated candidate. The walk stops once ``limit`` verified rows are
    # collected and the next candidate's approximate distance provably cannot
    # beat the current k-th exact distance (epsilon-guarded refinement).
    # Hydration batch: max(4*limit, 32) ids per SELECT, capped under the most
    # conservative SQLITE_MAX_VARIABLE_NUMBER (999) for legacy builds.
    batch_size = min(max(4 * limit, 32), 900)
    collected: list[tuple[float, str, str, VNextRow]] = []
    index = 0
    while index < len(ranked):
        if limit > 0 and len(collected) >= limit:
            collected.sort(key=lambda item: (item[0], item[1], item[2]))
            cutoff = collected[limit - 1][0]
            if ranked[index][0] > cutoff + _VECTOR_DISTANCE_REFINE_EPSILON:
                break
        batch = ranked[index : index + batch_size]
        index += len(batch)
        batch_ids = [entry[2] for entry in batch]
        # The hydrate re-applies ``predicate_sql`` (the scan's full WHERE
        # set), so ``embedding`` here is guaranteed non-NULL and the row still
        # satisfies every lifecycle/scope/signature gate at hydrate time.
        hydrated = self._fetch_all(
            f"""
                    SELECT {", ".join(MEMORY_COLUMNS)}, embedding
                    FROM memories
                    WHERE {predicate_sql}
                      AND id IN ({self._placeholders(batch_ids)})
                    """,
            (*params, *batch_ids),
        )
        rows_by_id = {str(row.get("id")): row for row in hydrated}
        for _approx, _updated_key, candidate_id in batch:
            row = rows_by_id.get(candidate_id)
            if row is None:
                # Row vanished or was mutated out of scope between the scan
                # and this hydrate: skip WITHOUT consuming a limit slot; the
                # walk refills from further down the ranking.
                continue
            if signature_sql and not memory_embedding_signature_is_current(row):
                continue
            blob = cast(bytes, row.pop("embedding"))
            distance = _embedding_blob_distance(blob, query_array, query_norm)
            row["vector_distance"] = distance
            collected.append(
                (
                    distance,
                    str(row.get("updated_at") or ""),
                    str(row.get("id") or ""),
                    row,
                )
            )
    collected.sort(key=lambda item: (item[0], item[1], item[2]))
    return [row for _distance, _updated_key, _row_id, row in collected[:limit]]


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
    discipline as the sibling search methods (user scoping,
    deleted/status gates, domain/sensitivity/scope filters); note the
    default expiry gate still hides rows whose ``valid_to`` has
    passed — pass ``include_expired=True`` to recall facts that were
    only true historically.
    """
    start_iso = _iso_or_none(window_start)
    end_iso = _iso_or_none(window_end)
    if window_center is None:
        window_center = window_start + (window_end - window_start) / 2
    center_iso = _iso_or_none(window_center)
    domain_sql, domain_params = self._domain_clause(domains)
    sensitivity_sql, sensitivity_params = self._sensitivity_clause(sensitivity_allowed)
    type_sql, type_params = self._memory_type_clause(memory_types)
    project_sql, project_params = self._project_clause(projects)
    created_by_sql, created_by_params = self._created_by_clause(created_by_agent_ids)
    run_sql, run_params = self._run_clause(run_id)
    expiry_sql, expiry_params = self._expiry_clause(include_expired)
    # julianday() parses the store's ISO-8601 TEXT timestamps and
    # returns NULL for anything unparseable, so malformed rows drop
    # out of the window instead of raising.
    event_time_sql = "julianday(COALESCE(valid_from, first_seen_at, created_at))"
    params: list[object] = [self.user_id]
    params.extend(domain_params)
    params.extend(sensitivity_params)
    params.extend(type_params)
    params.extend(project_params)
    params.extend(created_by_params)
    params.extend(run_params)
    params.extend(expiry_params)
    params.extend([start_iso, end_iso, end_iso, start_iso, center_iso])
    params.append(limit)
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)}
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN {_MEMORY_SEARCHABLE_STATUSES_SQL}{domain_sql}{sensitivity_sql}{type_sql}{project_sql}{created_by_sql}{run_sql}{expiry_sql}
                  AND (
                    ({event_time_sql} >= julianday(?) AND {event_time_sql} < julianday(?))
                    OR (
                      valid_from IS NOT NULL
                      AND valid_to IS NOT NULL
                      AND julianday(valid_from) < julianday(?)
                      AND julianday(valid_to) > julianday(?)
                    )
                  )
                ORDER BY
                  ABS({event_time_sql} - julianday(?)) ASC,
                  updated_at DESC,
                  created_at DESC,
                  id DESC
                LIMIT ?
                """,
        tuple(params),
    )


for _memory_method in (
    get_memory_by_key,
    get_memory,
    get_memories_by_ids,
    list_memories_referencing_source,
    list_memories_referencing_sources,
    list_pending_derived_candidates_for_member,
    get_memory_by_commit_digest,
    latest_agentic_commit_memory,
    get_memory_by_confirmation_id,
    list_memories,
    list_memories_view_partitions,
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
):
    _memory_method.__module__ = "alicebot_api.sqlite_store"
    _memory_method.__qualname__ = f"SQLiteVNextStore.{_memory_method.__name__}"
del _memory_method
