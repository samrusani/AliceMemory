"""Narrow, bounded ancestry reads shared by label writes and read guards."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from alicebot_api.vnext_derived_labels import canon_kind, dependencies_of, identifier, is_derived


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
        row["kind"] = kind
        if user_id is not None:
            row["user_id"] = user_id
        rows.append(row)
        refs = list(dependencies_of(kind, row)) if is_derived(kind, row) else []
        if kind == "belief" and row.get("memory_id"):
            refs.append(("memory", identifier(row["memory_id"])))
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
