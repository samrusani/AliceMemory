"""Narrow, bounded ancestry reads shared by label writes and read guards."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from typing import Any

from alicebot_api.vnext_derived_labels import (
    _metadata,
    _strings,
    canon_kind,
    dependencies_of,
    identifier,
    is_derived,
    soft_refs,
    weekly_candidate_memory,
)

# One request shares the parent index. The index itself is held by the request's label state, in ``cache`` under
# ``"index"``, so whatever empties that state (``invalidate_read_labels``) empties the index with it. It is absent until
# the first weekly memory asks. This variable names the store the request belongs to and counts the lookups.
_WEEKLY_PARENTS: ContextVar[dict[str, Any] | None] = ContextVar("weekly_parent_index", default=None)


def bind_weekly_parent_cache(store: Any, cache: dict[str, Any]) -> Token:
    """Start the parent index for one read. A nested read of the same store keeps the outer index.

    ``cache`` is the dict the request's label state clears when it is invalidated. It has no default: an index
    that the invalidation does not reach would serve a report that a write or a rollback has already changed.
    """

    return _WEEKLY_PARENTS.set({"store": store, "cache": cache, "queries": 0})


def reset_weekly_parent_cache(token: Token) -> None:
    _WEEKLY_PARENTS.reset(token)


def drop_weekly_parent_index(store: Any) -> None:
    """Forget the index this store's request has loaded, so its next lookup reads the reports again.

    A write calls this once it has stored its row: the insert floor reads the reports while the write runs,
    and an index loaded then does not hold the row the write goes on to store.
    """

    current = _WEEKLY_PARENTS.get()
    if current is not None and current["store"] is store:
        current["cache"].clear()


def weekly_parent_query_count() -> int:
    """How many parent lookups this request has run. Zero outside a read scope."""

    current = _WEEKLY_PARENTS.get()
    if current is None:
        return 0
    return int(current["queries"])


def load_weekly_parent_artifacts(store: Any, memory_id: str) -> list[dict[str, object]]:
    """Reports that name ``memory_id`` in ``candidate_memory_ids``, or none.

    One request loads every candidate list once and matches it with the same normalisation the
    stored-row classifier uses. A later weekly memory in that request does not run another query.
    A store may offer ``read_artifacts_naming_candidates`` (called with ``None`` for every list).
    Otherwise a PostgreSQL connection is scanned and a SQLite connection returns nothing: the local
    schema has no generated-artifact table.
    """

    if not memory_id:
        return []
    return [dict(row) for row in _parent_index(store).get(identifier(memory_id), ())]


def _parent_index(store: Any) -> dict[str, list[dict[str, object]]]:
    current = _WEEKLY_PARENTS.get()
    if current is not None and current["store"] is store:
        cached = current["cache"].get("index")
        if cached is not None:
            return cached
    index = _load_parent_index(store)
    if current is not None and current["store"] is store:
        current["cache"]["index"] = index
    return index


def _load_parent_index(store: Any) -> dict[str, list[dict[str, object]]]:
    current = _WEEKLY_PARENTS.get()
    if current is not None and current["store"] is store:
        current["queries"] += 1
    reader = getattr(store, "read_artifacts_naming_candidates", None)
    if callable(reader):
        rows = reader(None)
    else:
        rows = _postgres_candidate_artifacts(store)
    return _index_candidate_rows(rows)


def _postgres_candidate_artifacts(store: Any) -> list[dict[str, object]]:
    conn = getattr(store, "conn", None)
    if conn is None or "psycopg" not in type(conn).__module__:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, user_id, domain, sensitivity, metadata_json, artifact_type
            FROM generated_artifacts
            WHERE metadata_json ? 'candidate_memory_ids'
            """
        )
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def _index_candidate_rows(rows: object) -> dict[str, list[dict[str, object]]]:
    """Map each candidate id, in the classifier's spelling, to the reports that name it."""

    index: dict[str, list[dict[str, object]]] = {}
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        return index
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        copied = dict(row)
        for candidate in _strings(_metadata(copied).get("candidate_memory_ids")):
            if candidate:
                index.setdefault(candidate, []).append(copied)
    return index


def collect_label_rows(
    store: Any,
    roots: Sequence[Mapping[str, object]],
    *,
    max_nodes: int,
    max_hops: int | None = None,
    cache: dict[tuple[str, str], list[dict[str, object]]] | None = None,
    user_id: str | None = None,
) -> tuple[list[dict[str, object]], bool]:
    """Return reachable stored rows and whether the independent walk budget ran out.

    Cache entries only avoid reads; every origin still traverses its ancestry.
    Stored IDs remain intact while query and graph identities use the canonical parser.
    """
    loaded = cache if cache is not None else {}
    pending = deque((canon_kind(row.get("kind")), dict(row), 0) for row in roots)
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    requested: set[tuple[str, str]] = set()
    exceeded = False
    reader = getattr(store, "read_label_rows", None)
    while pending:
        kind, row, depth = pending.popleft()
        stored_key = (kind, str(row.get("user_id") or ""), str(row.get("id") or ""))
        if stored_key in seen:
            continue
        if len(seen) >= max_nodes or (max_hops is not None and depth > max_hops):
            exceeded = True
            continue
        seen.add(stored_key)
        # Tag and alias-raise only this walk's copy. Cached raw rows keep
        # their tenant and stored label across origins in the same request.
        row = dict(row)
        row["kind"] = kind
        if user_id is not None:
            row["user_id"] = user_id
        rows.append(row)
        # The ids a derived row may cite are read with the rest: one that names no stored row finds nothing here, and one that
        # does is a node, which the settlement takes for an input (resolve or ignore).
        refs = [*dependencies_of(kind, row), *soft_refs(kind, row)] if is_derived(kind, row) else []
        if kind == "belief" and row.get("memory_id"):
            refs.append(("memory", identifier(row["memory_id"])))
        # A weekly candidate's inputs live on the report that names it. The forward walk never
        # reaches that report, so load it here and keep walking from its own inputs.
        if weekly_candidate_memory(kind, row):
            parents = load_weekly_parent_artifacts(store, str(row.get("id") or ""))
        else:
            parents = []
        for found in parents:
            copied = dict(found)
            art_id = identifier(copied.get("id"))
            if not art_id:
                continue
            art_key = ("artifact", art_id)
            if art_key in requested:
                continue
            requested.add(art_key)
            bucket = loaded.setdefault(art_key, [])
            if not any(identifier(item.get("id")) == art_id for item in bucket):
                bucket.append(copied)
            pending.append(("artifact", copied, depth + 1))
        grouped: dict[str, list[str]] = {}
        for ref_kind, ref_id in refs:
            name, canonical_id = canon_kind(ref_kind), identifier(ref_id)
            key = (name, canonical_id)
            if key in requested:
                continue
            requested.add(key)
            if key in loaded:
                pending.extend((name, dict(found), depth + 1) for found in loaded[key])
            else:
                grouped.setdefault(name, []).append(canonical_id)
        if not callable(reader):
            continue
        for name, ids in grouped.items():
            for item in ids:
                loaded[(name, item)] = []
            for found in reader(name, ids):
                if not isinstance(found, Mapping):
                    continue
                key = (name, identifier(found.get("id")))
                if key not in loaded or key[1] not in ids:
                    continue
                copied = dict(found)
                loaded[key].append(copied)
                pending.append((name, copied, depth + 1))
    # UUID aliases may coexist in SQLite. Never let iteration order choose a public twin.
    twins: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for row in rows:
        twins.setdefault((str(row["kind"]), str(row.get("user_id") or ""), identifier(row.get("id"))), []).append(row)
    for aliases in twins.values():
        if len(aliases) > 1:
            for row in aliases:
                row["sensitivity"] = "regulated"
    return rows, exceeded
