"""The what-changed read: recorded decision chains a caller may see.

The topic is free text. Decision memories that share its words are ranked, at most
``READABLE_SCAN_CAP`` of them are judged, and each readable start is one chain.
``superseded_by`` is followed for at most 64 hops, the bound
``supersession_would_cycle`` already uses. A step the caller may not read is left
out. The answer has no count and no gap marker.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from alicebot_api.vnext_label_guard import outside_caller_limits
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP

NOTICE = "This covers the recorded decisions the caller may read. It is not a company-wide statement."
NOT_FOUND = "No recorded decision history was found"
SEARCH = "alice_recall"
HOP_CAP = 64
_WORD = re.compile(r"[a-z0-9]+")
_RESOLVED_CONTRADICTION = frozenset({"reinforce", "challenge", "supersede", "retire"})

_MEMORY_SQL = """
SELECT id, title, canonical_text, summary, domain, sensitivity, status,
       confirmation_status, valid_from, superseded_by, supersedes,
       metadata_json, created_by_agent_id, project_id, created_at, memory_type
FROM memories
WHERE user_id = ? AND memory_type = 'decision' AND deleted_at IS NULL
"""

_SOURCE_SQL = """
SELECT id, title, author, domain, sensitivity, metadata_json, captured_at,
       source_created_at
FROM sources
WHERE user_id = ? AND deleted_at IS NULL AND id IN ({marks})
"""

_EDGE_SQL = """
SELECT from_id, to_id, metadata_json
FROM graph_edges
WHERE user_id = ? AND edge_type = 'contradicts'
"""


def _step_is_readable(store: Any, identity: Any, kind: str, row: Mapping[str, object]) -> bool:
    """Whether this caller may read this stored step."""

    return not outside_caller_limits(store, identity, kind, row)


def what_changed(
    store: Any,
    *,
    user_id: object,
    identity: Any,
    topic: str,
    today: str | None = None,
) -> dict[str, object]:
    """The chains of recorded decisions on ``topic`` that ``identity`` may read.

    ``today`` is an ISO date. The default is the current UTC date.
    """

    today_text = today or datetime.now(UTC).date().isoformat()
    memories = _load_memories(store, user_id)
    by_id = {str(row["id"]): row for row in memories}
    predecessors: dict[str, list[str]] = defaultdict(list)
    for row in memories:
        nxt = _text(row.get("superseded_by"))
        if nxt:
            predecessors[nxt].append(str(row["id"]))

    judged = _rank(topic, memories)[:READABLE_SCAN_CAP]
    claimed: set[str] = set()
    built: list[tuple[str, list[str]]] = []
    for row in judged:
        if not _step_is_readable(store, identity, "memory", row):
            continue
        start = str(row["id"])
        if start in claimed:
            continue
        member_ids = _walk(start, by_id, predecessors, store, identity)
        for member_id in member_ids:
            claimed.add(member_id)
        built.append((start, member_ids))

    sources = _sources_for(store, user_id, [by_id[member] for _start, members in built for member in members if member in by_id])
    edges = _contradictions(store, user_id)
    chains = [
        _chain(store, identity, start, member_ids, by_id, predecessors, sources, edges, today_text)
        for start, member_ids in built
    ]
    answer: dict[str, object] = {"notice": NOTICE, "topic": topic, "chains": chains}
    if not chains:
        answer["message"] = NOT_FOUND
        answer["search"] = SEARCH
    return answer


def _chain(
    store: Any,
    identity: Any,
    start: str,
    member_ids: Sequence[str],
    by_id: Mapping[str, Mapping[str, object]],
    predecessors: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, object]],
    edges: Sequence[tuple[str, str, bool]],
    today_text: str,
) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    for member_id in member_ids:
        row = by_id.get(member_id)
        if row is None:
            continue
        replaced = _text(row.get("supersedes"))
        if replaced is None:
            prior = predecessors.get(member_id) or ()
            replaced = prior[0] if prior else None
        steps.append(
            {
                "id": member_id,
                "kind": "memory",
                "chain": start,
                "date": _decision_day(row),
                "change": str(row.get("canonical_text") or ""),
                "reason": _reason(store, identity, row, sources),
                "replaced": replaced,
                "disagrees_with": None,
                "sources": _citations(store, identity, row, sources),
                "author": _text(row.get("created_by_agent_id")) or "owner",
                "review_state": _review_state(row),
                "_created_at": str(row.get("created_at") or ""),
                "_future": _is_future(row, today_text),
                "_settled": _accepted(row) and not _is_future(row, today_text),
            }
        )
    steps.extend(_source_steps(store, identity, start, steps, sources))
    visible = {str(step["id"]) for step in steps}
    for step in steps:
        if step["replaced"] not in visible:
            step["replaced"] = None
    unresolved_pairs = _mark_disagreements(steps, edges, visible)
    steps.sort(key=lambda step: (str(step["date"]), str(step["_created_at"]), str(step["id"])))
    settled = [step for step in steps if step.get("_settled")]
    newest = settled[-1] if settled else None
    disputed: list[str] = []
    current: dict[str, object] | None = newest
    if newest is not None and str(newest["id"]) in unresolved_pairs:
        current = None
        disputed = sorted(unresolved_pairs[str(newest["id"])] | {str(newest["id"])})
    upcoming = [str(step["id"]) for step in steps if step.get("_future")]
    for step in steps:
        step.pop("_created_at", None)
        step.pop("_future", None)
        step.pop("_settled", None)
    return {
        "start": start,
        "current": current,
        "upcoming": upcoming,
        "disputed": disputed,
        "history": steps,
    }


def _source_steps(
    store: Any,
    identity: Any,
    start: str,
    memory_steps: Sequence[Mapping[str, object]],
    sources: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    cited: list[str] = []
    for step in memory_steps:
        raw_sources = step.get("sources")
        if not isinstance(raw_sources, list):
            continue
        for source in raw_sources:
            if isinstance(source, Mapping):
                source_id = str(source.get("source_id") or "")
                if source_id and source_id not in cited:
                    cited.append(source_id)
    chosen: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for source_id in cited:
        row = sources.get(source_id)
        if row is None or not _step_is_readable(store, identity, "source", row):
            continue
        replacement = _text(_meta(row).get("superseded_by"))
        if not replacement:
            continue
        if source_id not in seen:
            chosen.append(row)
            seen.add(source_id)
        successor = sources.get(replacement)
        if (
            successor is not None
            and replacement not in seen
            and _step_is_readable(store, identity, "source", successor)
        ):
            chosen.append(successor)
            seen.add(replacement)
    forward = {
        str(row["id"]): _text(_meta(row).get("superseded_by"))
        for row in chosen
    }
    built: list[dict[str, object]] = []
    for row in chosen:
        source_id = str(row["id"])
        older = next((left for left, right in forward.items() if right == source_id), None)
        day = _source_day(row)
        built.append(
            {
                "id": source_id,
                "kind": "source",
                "chain": start,
                "date": day,
                "change": str(row.get("title") or ""),
                "reason": None,
                "replaced": older,
                "disagrees_with": None,
                "sources": [{"source_id": source_id, "title": str(row.get("title") or ""), "date": day}],
                "author": _text(row.get("author")) or "importer",
                "review_state": "recorded",
                "_created_at": str(row.get("captured_at") or row.get("created_at") or ""),
                "_future": False,
                "_settled": False,
            }
        )
    return built


def _mark_disagreements(
    steps: list[dict[str, object]],
    edges: Sequence[tuple[str, str, bool]],
    visible: set[str],
) -> dict[str, set[str]]:
    """Set ``disagrees_with`` and return unresolved partners keyed by step id."""

    by_id = {str(step["id"]): step for step in steps}
    unresolved: dict[str, set[str]] = defaultdict(set)
    for left, right, open_review in edges:
        if left not in visible or right not in visible:
            continue
        if by_id[left]["disagrees_with"] is None:
            by_id[left]["disagrees_with"] = right
        if by_id[right]["disagrees_with"] is None:
            by_id[right]["disagrees_with"] = left
        if open_review:
            unresolved[left].add(right)
            unresolved[right].add(left)
    return unresolved


def _walk(
    start: str,
    by_id: Mapping[str, Mapping[str, object]],
    predecessors: Mapping[str, Sequence[str]],
    store: Any,
    identity: Any,
) -> list[str]:
    found = [start]
    seen = {start}
    hops = 0
    frontier = [start]
    while frontier and hops < HOP_CAP:
        current = frontier.pop(0)
        row = by_id.get(current)
        if row is None:
            continue
        for neighbor in _neighbors(row, predecessors):
            if neighbor in seen or hops >= HOP_CAP:
                continue
            hops += 1
            seen.add(neighbor)
            neighbor_row = by_id.get(neighbor)
            if neighbor_row is None or not _step_is_readable(store, identity, "memory", neighbor_row):
                continue
            found.append(neighbor)
            frontier.append(neighbor)
    return found


def _neighbors(row: Mapping[str, object], predecessors: Mapping[str, Sequence[str]]) -> list[str]:
    found: list[str] = []
    for key in ("superseded_by", "supersedes"):
        value = _text(row.get(key))
        if value:
            found.append(value)
    found.extend(predecessors.get(str(row["id"]), ()))
    return found


def _rank(topic: str, memories: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    words = {word for word in _WORD.findall(topic.lower()) if len(word) >= 3}
    if not words:
        return []
    scored: list[tuple[int, Mapping[str, object]]] = []
    for row in memories:
        hay = " ".join(str(row.get(key) or "") for key in ("title", "canonical_text", "summary")).lower()
        hay_words = {word for word in _WORD.findall(hay) if len(word) >= 3}
        score = sum(1 for word in words if word in hay_words)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: str(item[1]["id"]))
    scored.sort(key=lambda item: str(item[1].get("created_at") or ""), reverse=True)
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _score, row in scored]


def _reason(
    store: Any,
    identity: Any,
    row: Mapping[str, object],
    sources: Mapping[str, Mapping[str, object]],
) -> dict[str, object] | None:
    agentic = _agentic(row)
    rationale = agentic.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        return {"text": rationale.strip(), "source_id": None}
    confirmation = agentic.get("confirmation_rationale")
    if not isinstance(confirmation, str) or not confirmation.strip():
        nested = agentic.get("confirmation")
        if isinstance(nested, Mapping):
            nested_reason = nested.get("rationale") or nested.get("confirmation_rationale")
            confirmation = nested_reason if isinstance(nested_reason, str) else None
    if isinstance(confirmation, str) and confirmation.strip():
        return {"text": confirmation.strip(), "source_id": None}
    for source_id, quote in _refs(row):
        if not source_id or not quote:
            continue
        source = sources.get(source_id)
        if source is None or not _step_is_readable(store, identity, "source", source):
            continue
        return {"text": quote, "source_id": source_id}
    return None


def _citations(
    store: Any,
    identity: Any,
    row: Mapping[str, object],
    sources: Mapping[str, Mapping[str, object]],
) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_id, _quote in _refs(row):
        if not source_id or source_id in seen:
            continue
        source = sources.get(source_id)
        if source is None or not _step_is_readable(store, identity, "source", source):
            continue
        seen.add(source_id)
        found.append({"source_id": source_id, "title": str(source.get("title") or ""), "date": _source_day(source)})
    return found


def _sources_for(store: Any, user_id: object, memories: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    wanted: list[str] = []
    for row in memories:
        for source_id, _quote in _refs(row):
            if source_id and source_id not in wanted:
                wanted.append(source_id)
    loaded = _load_sources(store, user_id, wanted)
    extra = [
        replacement
        for row in loaded.values()
        if (replacement := _text(_meta(row).get("superseded_by"))) and replacement not in loaded
    ]
    if extra:
        loaded.update(_load_sources(store, user_id, extra))
    return loaded


def _refs(row: Mapping[str, object]) -> list[tuple[str | None, str | None]]:
    found: list[tuple[str | None, str | None]] = []
    for container in (_meta(row), _agentic(row)):
        for key in ("source_refs", "provenance"):
            value = container.get(key)
            if isinstance(value, list):
                found.extend(_ref_parts(entry) for entry in value)
    return found


def _ref_parts(entry: object) -> tuple[str | None, str | None]:
    if isinstance(entry, str):
        return _source_id(entry), None
    if isinstance(entry, Mapping):
        quote = entry.get("quote")
        return _source_id(entry.get("source_id") or entry.get("id") or entry.get("ref")), (
            quote.strip() if isinstance(quote, str) and quote.strip() else None
        )
    return None, None


def _source_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower().startswith("source:"):
        text = text.split(":", 1)[1].strip()
    return text or None


def _contradictions(store: Any, user_id: object) -> list[tuple[str, str, bool]]:
    found: list[tuple[str, str, bool]] = []
    for row in _fetch(store, _EDGE_SQL, (str(user_id),)):
        left = _text(row.get("from_id"))
        right = _text(row.get("to_id"))
        if not left or not right:
            continue
        status = str(_meta(row).get("status") or _meta(row).get("review_state") or "")
        found.append((left, right, status not in _RESOLVED_CONTRADICTION))
    return found


def _accepted(row: Mapping[str, object]) -> bool:
    status = str(row.get("status") or "")
    if status == "needs_review":
        return False
    return status == "active" or str(row.get("confirmation_status") or "") == "confirmed"


def _is_future(row: Mapping[str, object], today_text: str) -> bool:
    day = _day(row.get("valid_from"))
    return day is not None and day > today_text


def _review_state(row: Mapping[str, object]) -> str:
    status = str(row.get("status") or "")
    confirmation = str(row.get("confirmation_status") or "")
    if status == "needs_review":
        return "needs_review"
    if confirmation == "confirmed" and status != "active":
        return "confirmed"
    return status or confirmation or "unknown"


def _decision_day(row: Mapping[str, object]) -> str:
    return _day(row.get("valid_from")) or _day(row.get("created_at")) or ""


def _source_day(row: Mapping[str, object]) -> str:
    return _day(row.get("source_created_at")) or _day(row.get("captured_at")) or _day(row.get("created_at")) or ""


def _day(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value)
    return text[:10] if len(text) >= 10 else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _meta(row: Mapping[str, object]) -> dict[str, object]:
    raw = row.get("metadata_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _agentic(row: Mapping[str, object]) -> Mapping[str, object]:
    agentic = _meta(row).get("agentic_memory")
    return agentic if isinstance(agentic, Mapping) else {}


def _load_memories(store: Any, user_id: object) -> list[Mapping[str, object]]:
    return _fetch(store, _MEMORY_SQL, (str(user_id),))


def _is_uuid(value: str) -> bool:
    from uuid import UUID

    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def _load_sources(store: Any, user_id: object, source_ids: Sequence[str]) -> dict[str, Mapping[str, object]]:
    source_ids = [source_id for source_id in source_ids if _is_uuid(source_id)]
    if not source_ids:
        return {}
    marks = ", ".join("?" for _ in source_ids)
    rows = _fetch(store, _SOURCE_SQL.format(marks=marks), (str(user_id), *source_ids))
    return {str(row["id"]): row for row in rows}


def _fetch(store: Any, query: str, params: tuple[object, ...]) -> list[Mapping[str, object]]:
    # Every placeholder in these reads is a uuid column. Postgres will not
    # compare uuid to text, and it returns those columns as UUID objects.
    if type(store).__name__ == "PostgresVNextStore":
        query = query.replace("?", "%s::uuid")
    rows: list[Mapping[str, object]] = []
    for row in store._fetch_all(query, params):
        if isinstance(row, Mapping):
            plain: dict[str, object] = {
                str(key): str(value) if type(value).__name__ == "UUID" else value for key, value in row.items()
            }
            rows.append(plain)
    return rows
