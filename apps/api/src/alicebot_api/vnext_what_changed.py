"""The what-changed read: recorded decision chains a caller may see.

The topic is free text. Starts come from the same full-text search recall uses,
at most ``READABLE_SCAN_CAP`` of them. ``superseded_by`` is followed for at most
64 hops, the bound ``supersession_would_cycle`` already uses, and only a step
the caller may read counts as a hop. A step the caller may not read is left
out. The answer has no count and no gap marker.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from alicebot_api.recall_framing import memory_writer, quote_session_brief_text, writer_attribution
from alicebot_api.source_search_limits import require_source_search_query
from alicebot_api.vnext_label_guard import label_read_scope, outside_caller_limits
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP
from alicebot_api.vnext_source_fence import cited_memory_refs, cited_source_ids

NOTICE = "This covers the recorded decisions the caller may read. It is not a company-wide statement."
NOT_FOUND = "No recorded decision history was found"
SEARCH = "alice_recall"
HOP_CAP = 64
_UNRESOLVED_CONTRADICTION = frozenset({"candidate", "needs_review"})
_LIFECYCLE_STATES = frozenset({"forgotten", "undone", "expired"})

_DECISION_SQL = """
SELECT id, title, canonical_text, summary, domain, sensitivity, status,
       confirmation_status, valid_from, valid_to, superseded_by, supersedes,
       metadata_json, created_by_agent_id, project_id, created_at, memory_type,
       deleted_at
FROM memories
WHERE user_id = ? AND id = ? AND memory_type = 'decision'
"""

_PREDECESSOR_SQL = """
SELECT id FROM memories
WHERE user_id = ? AND superseded_by = ? AND memory_type = 'decision' AND deleted_at IS NULL
"""

_SOURCE_SQL = """
SELECT id, title, author, domain, sensitivity, metadata_json, captured_at,
       source_created_at, deleted_at
FROM sources
WHERE user_id = ? AND id IN ({marks})
"""

_EDGE_SQL = """
SELECT from_type, from_id, to_type, to_id, metadata_json, valid_to
FROM graph_edges
WHERE user_id = ? AND edge_type = 'contradicts'
"""

_BELIEF_SQL = """
SELECT id, memory_id FROM beliefs
WHERE user_id = ? AND id IN ({marks})
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

    lock = getattr(store, "lock_label_writes", None)
    if callable(lock):
        lock()
    with label_read_scope(store):
        return _assemble(store, user_id=user_id, identity=identity, topic=topic, today=today)


def _assemble(
    store: Any,
    *,
    user_id: object,
    identity: Any,
    topic: str,
    today: str | None,
) -> dict[str, object]:
    today_text = today or datetime.now(UTC).date().isoformat()
    judged = _search_starts(store, identity, topic)
    by_id: dict[str, Mapping[str, object]] = {}
    predecessors: dict[str, list[str]] = defaultdict(list)
    claimed: set[str] = set()
    built: list[tuple[str, list[str]]] = []
    for row in judged:
        # Full-text search returns uuid columns as UUID objects. The walk
        # reads superseded_by and supersedes as text, the same as _fetch.
        row = _plain_row(row)
        by_id[str(row["id"])] = row
        if not _step_is_readable(store, identity, "memory", row):
            continue
        start = str(row["id"])
        if start in claimed:
            continue
        member_ids = _walk(store, user_id, start, by_id, predecessors, identity)
        for member_id in member_ids:
            claimed.add(member_id)
        built.append((start, member_ids))

    memories = [by_id[member] for _start, members in built for member in members if member in by_id]
    sources = _sources_for(store, user_id, memories)
    cited_memories = _cited_memories(store, memories)
    edges = _contradictions(store, user_id)
    chains = [
        _chain(
            store,
            identity,
            start,
            member_ids,
            by_id,
            predecessors,
            sources,
            cited_memories,
            edges,
            today_text,
        )
        for start, member_ids in built
    ]
    answer: dict[str, object] = {"notice": NOTICE, "topic": topic, "chains": chains}
    if not chains:
        answer["message"] = NOT_FOUND
        answer["search"] = SEARCH
    return answer


def _search_starts(store: Any, identity: Any, topic: str) -> list[Mapping[str, object]]:
    """The decision memories recall's full-text search would rank first.

    Hidden rows are included, so they can use up the 128 places before a
    readable start. An empty topic matches nothing. A topic recall cannot
    search is refused by the same limit recall uses.
    """

    text = topic.strip()
    if not text:
        return []
    check = getattr(store, "check_source_search_query", None)
    if callable(check):
        check(text)
    else:
        require_source_search_query(text)
    search = getattr(store, "search_memories_fts", None)
    if not callable(search):
        search = store.search_memories
    options = {
        "query": text,
        "memory_types": ("decision",),
        "limit": READABLE_SCAN_CAP,
        "include_expired": True,
    }
    rows = [row for row in search(**options) if isinstance(row, Mapping)]
    if any(_step_is_readable(store, identity, "memory", row) for row in rows):
        return rows
    try:
        relaxed = search(**options, match_any=True)
    except TypeError:
        return rows
    return [row for row in relaxed if isinstance(row, Mapping)]


def _chain(
    store: Any,
    identity: Any,
    start: str,
    member_ids: Sequence[str],
    by_id: Mapping[str, Mapping[str, object]],
    predecessors: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, object]],
    cited_memories: Mapping[str, Mapping[str, object]],
    edges: Sequence[tuple[str, str, bool]],
    today_text: str,
) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    rows_for_steps: dict[str, Mapping[str, object]] = {}
    for member_id in member_ids:
        row = by_id.get(member_id)
        if row is None:
            continue
        rows_for_steps[member_id] = row
        named = _text(row.get("supersedes"))
        priors = [prior for prior in predecessors.get(member_id, ()) if prior != named]
        steps.append(
            {
                "id": member_id,
                "kind": "memory",
                "chain": start,
                "date": _decision_day(row),
                "change": str(row.get("canonical_text") or ""),
                "reason": _reason(store, identity, row, sources, cited_memories),
                "replaced": named,
                "disagrees_with": None,
                "sources": _citations(store, identity, row, sources, cited_memories),
                "author": _text(row.get("created_by_agent_id")) or "owner",
                "review_state": _review_state(row),
                "_created_at": str(row.get("created_at") or ""),
                "_future": _is_future(row, today_text),
                "_settled": _in_force(row, today_text),
                "_successor": _text(row.get("superseded_by")),
                "_priors": priors,
            }
        )
    steps.extend(_source_steps(store, identity, start, steps, sources))
    visible = {str(step["id"]) for step in steps}
    for step in steps:
        step["replaced"] = _readable_replaced(step, visible, by_id)
        step.pop("_priors", None)
    unresolved_pairs = _mark_disagreements(steps, edges, visible)
    steps.sort(key=lambda step: (str(step["date"]), str(step["_created_at"]), str(step["id"])))
    settled_ids = {str(step["id"]) for step in steps if step.get("_settled")}
    settled = [
        step
        for step in steps
        if step.get("_settled") and step.get("_successor") not in settled_ids
    ]
    newest = settled[-1] if settled else None
    disputed: list[str] = []
    current: dict[str, object] | None = newest
    if newest is not None and str(newest["id"]) in unresolved_pairs:
        current = None
        disputed = sorted(unresolved_pairs[str(newest["id"])] | {str(newest["id"])})
    upcoming = [str(step["id"]) for step in steps if step.get("_future")]
    for step in steps:
        _present_step(store, step, rows_for_steps.get(str(step["id"])))
        step.pop("_created_at", None)
        step.pop("_future", None)
        step.pop("_settled", None)
        step.pop("_successor", None)
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
    store: Any,
    user_id: object,
    start: str,
    by_id: dict[str, Mapping[str, object]],
    predecessors: dict[str, list[str]],
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
        _remember_predecessors(store, user_id, current, predecessors)
        for neighbor in _neighbors(row, predecessors):
            if neighbor in seen or hops >= HOP_CAP:
                continue
            seen.add(neighbor)
            neighbor_row = _load_decision(store, user_id, neighbor, by_id)
            if neighbor_row is None or not _step_is_readable(store, identity, "memory", neighbor_row):
                continue
            hops += 1
            found.append(neighbor)
            frontier.append(neighbor)
    return found


def _remember_predecessors(
    store: Any,
    user_id: object,
    memory_id: str,
    predecessors: dict[str, list[str]],
) -> None:
    if memory_id in predecessors:
        return
    found: list[str] = []
    for row in _fetch(store, _PREDECESSOR_SQL, (str(user_id), memory_id)):
        prior = _text(row.get("id")) or str(row.get("id") or "")
        if prior and prior not in found:
            found.append(prior)
    predecessors[memory_id] = found


def _load_decision(
    store: Any,
    user_id: object,
    memory_id: str,
    by_id: dict[str, Mapping[str, object]],
) -> Mapping[str, object] | None:
    cached = by_id.get(memory_id)
    if cached is not None:
        return cached
    rows = _fetch(store, _DECISION_SQL, (str(user_id), memory_id))
    if not rows:
        return None
    row = rows[0]
    if row.get("deleted_at"):
        return None
    by_id[str(row["id"])] = row
    return row


def _neighbors(row: Mapping[str, object], predecessors: Mapping[str, Sequence[str]]) -> list[str]:
    found: list[str] = []
    for key in ("superseded_by", "supersedes"):
        value = _text(row.get(key))
        if value and value not in found:
            found.append(value)
    for prior in predecessors.get(str(row["id"]), ()):
        if prior not in found:
            found.append(prior)
    return found


def _reason(
    store: Any,
    identity: Any,
    row: Mapping[str, object],
    sources: Mapping[str, Mapping[str, object]],
    cited_memories: Mapping[str, Mapping[str, object]],
) -> dict[str, object] | None:
    correction = _correction_reason(row)
    if correction is not None:
        return {"text": correction, "source_id": None}
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
    for entry in _ref_entries(row):
        quote = _quote_text(entry)
        if quote is None or not _entry_is_readable(store, identity, entry, sources, cited_memories):
            continue
        source_id = _shown_source_id(entry, sources, store, identity)
        if source_id is None and cited_source_ids(entry).named:
            continue
        return {"text": quote, "source_id": source_id}
    return None


def _citations(
    store: Any,
    identity: Any,
    row: Mapping[str, object],
    sources: Mapping[str, Mapping[str, object]],
    cited_memories: Mapping[str, Mapping[str, object]],
) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in _ref_entries(row):
        if not _entry_is_readable(store, identity, entry, sources, cited_memories):
            continue
        for source_id in sorted(cited_source_ids(entry).named):
            if source_id in seen:
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
        for entry in _ref_entries(row):
            for source_id in cited_source_ids(entry).every:
                if source_id not in wanted:
                    wanted.append(source_id)
    loaded = _load_sources(store, user_id, wanted)
    extra = [
        replacement
        for row in loaded.values()
        if (replacement := _text(_meta(row).get("superseded_by"))) and _is_uuid(replacement) and replacement not in loaded
    ]
    if extra:
        loaded.update(_load_sources(store, user_id, extra))
    return loaded


def _cited_memories(store: Any, memories: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    wanted: list[str] = []
    for row in memories:
        for entry in _ref_entries(row):
            for memory_id in cited_memory_refs(entry).every:
                if memory_id not in wanted:
                    wanted.append(memory_id)
    found: dict[str, Mapping[str, object]] = {}
    get_memory = getattr(store, "get_memory", None)
    if not callable(get_memory):
        return found
    for memory_id in wanted:
        row = get_memory(memory_id)
        if isinstance(row, Mapping):
            found[str(row.get("id") or memory_id)] = row
    return found


def _ref_entries(row: Mapping[str, object]) -> list[object]:
    found: list[object] = []
    for container in (_meta(row), _agentic(row)):
        for key in ("source_refs", "provenance", "replacement_provenance"):
            value = container.get(key)
            if isinstance(value, list):
                found.extend(value)
            elif isinstance(value, Mapping):
                found.append(value)
    return found


def _quote_text(entry: object) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    quote = entry.get("quote")
    if isinstance(quote, str) and quote.strip():
        return quote.strip()
    return None


def _entry_is_readable(
    store: Any,
    identity: Any,
    entry: object,
    sources: Mapping[str, Mapping[str, object]],
    cited_memories: Mapping[str, Mapping[str, object]],
) -> bool:
    """A saved quote is shown only when every input the entry names is readable.

    Named ids that are not stored are refused. An incidental id is refused
    only when it names a stored row the caller may not read. This is the
    saved-quote reader's rule, including a ``source_ids`` list with no single
    ``source_id``.
    """

    cited_sources = cited_source_ids(entry)
    for source_id in cited_sources.named:
        source = sources.get(source_id)
        if source is None or not _step_is_readable(store, identity, "source", source):
            return False
    for source_id in cited_sources.incidental:
        source = sources.get(source_id)
        if source is not None and not _step_is_readable(store, identity, "source", source):
            return False
    cited = cited_memory_refs(entry)
    for memory_id in cited.named:
        memory = cited_memories.get(memory_id)
        if memory is None or not _step_is_readable(store, identity, "memory", memory):
            return False
    for memory_id in cited.incidental:
        memory = cited_memories.get(memory_id)
        if memory is not None and not _step_is_readable(store, identity, "memory", memory):
            return False
    return True


def _shown_source_id(
    entry: object,
    sources: Mapping[str, Mapping[str, object]],
    store: Any,
    identity: Any,
) -> str | None:
    named = sorted(cited_source_ids(entry).named)
    for source_id in named:
        source = sources.get(source_id)
        if source is not None and _step_is_readable(store, identity, "source", source):
            return source_id
    return None


def _correction_reason(row: Mapping[str, object]) -> str | None:
    metadata = _meta(row)
    direct = metadata.get("correction_reason")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    corrections = _agentic(row).get("corrections")
    if isinstance(corrections, list):
        for entry in reversed(corrections):
            if isinstance(entry, Mapping):
                reason = entry.get("reason")
                if isinstance(reason, str) and reason.strip():
                    return reason.strip()
    return None


def _contradictions(store: Any, user_id: object) -> list[tuple[str, str, bool]]:
    rows = _fetch(store, _EDGE_SQL, (str(user_id),))
    beliefs = _belief_memories(store, user_id, rows)
    found: list[tuple[str, str, bool]] = []
    for row in rows:
        if row.get("valid_to"):
            continue
        left = _edge_end(row, "from", beliefs)
        right = _edge_end(row, "to", beliefs)
        if not left or not right or left == right:
            continue
        status = str(_meta(row).get("status") or _meta(row).get("review_state") or "")
        found.append((left, right, status in _UNRESOLVED_CONTRADICTION))
    return found


def _belief_memories(store: Any, user_id: object, edges: Sequence[Mapping[str, object]]) -> dict[str, str]:
    wanted = [
        str(row.get("to_id"))
        for row in edges
        if str(row.get("to_type") or "") == "belief" and row.get("to_id")
    ]
    if not wanted:
        return {}
    marks = ", ".join("?" for _ in wanted)
    try:
        loaded = _fetch(store, _BELIEF_SQL.format(marks=marks), (str(user_id), *wanted))
    except Exception:
        return {}
    found: dict[str, str] = {}
    for row in loaded:
        belief_id = str(row.get("id") or "")
        memory_id = _text(row.get("memory_id")) or str(row.get("memory_id") or "")
        if belief_id and memory_id:
            found[belief_id] = memory_id
    return found


def _edge_end(row: Mapping[str, object], side: str, beliefs: Mapping[str, str]) -> str | None:
    kind = str(row.get(f"{side}_type") or "")
    end = _text(row.get(f"{side}_id")) or str(row.get(f"{side}_id") or "")
    if not end:
        return None
    if kind == "belief":
        return beliefs.get(end)
    if kind in {"memory", "source"}:
        return end
    return None


def _status_is_settled(row: Mapping[str, object]) -> bool:
    """A stored decision is settled only while its status is active."""

    status = str(row.get("status") or "")
    if status == "needs_review":
        return False
    return status == "active"


def _is_future(row: Mapping[str, object], today_text: str) -> bool:
    day = _day(row.get("valid_from"))
    return day is not None and day > today_text


def _in_force(row: Mapping[str, object], today_text: str) -> bool:
    """Settled means active and still inside its validity window.

    A ``needs_review`` step is never settled. A forgotten, undone or expired
    step is never settled, because its status is no longer active. A step
    past ``valid_to`` is never settled, including one whose status is still
    active. A step whose ``valid_from`` is after today is upcoming. A step
    that a later settled step replaced is dropped when ``current`` is chosen,
    whatever its dates.
    """

    if not _status_is_settled(row):
        return False
    if _is_future(row, today_text):
        return False
    valid_to = _day(row.get("valid_to"))
    return valid_to is None or valid_to >= today_text


def _review_state(row: Mapping[str, object]) -> str:
    lifecycle = _text(_agentic(row).get("lifecycle_status"))
    if lifecycle in _LIFECYCLE_STATES:
        return lifecycle
    validity = _meta(row).get("validity")
    if isinstance(validity, Mapping):
        recorded = _text(validity.get("state"))
        if recorded in _LIFECYCLE_STATES:
            return recorded
    status = str(row.get("status") or "")
    if status:
        return status
    return str(row.get("confirmation_status") or "") or "unknown"


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


def _readable_replaced(
    step: Mapping[str, object],
    visible: set[str],
    by_id: Mapping[str, Mapping[str, object]],
) -> str | None:
    """The predecessor this caller can see, chosen without the hidden ones.

    The recorded ``supersedes`` id wins when it is visible. Otherwise the
    earliest readable predecessor wins, by date, then created_at, then id.
    A hidden predecessor does not change that choice.
    """

    named = step.get("replaced")
    if isinstance(named, str) and named in visible:
        return named
    priors = step.get("_priors")
    if not isinstance(priors, list):
        return None
    readable = [prior for prior in priors if isinstance(prior, str) and prior in visible]

    def _order(prior: str) -> tuple[str, str, str]:
        row = by_id.get(prior) or {}
        return (_decision_day(row), str(row.get("created_at") or ""), prior)

    if not readable:
        return None
    return min(readable, key=_order)


def _present_step(store: Any, step: dict[str, object], row: Mapping[str, object] | None) -> None:
    """Quote stored text and name the writer, the way recall does."""

    change = step.get("change")
    if isinstance(change, str) and change.strip():
        step["change"] = quote_session_brief_text(change)
    reason = step.get("reason")
    if isinstance(reason, dict):
        text = reason.get("text")
        if isinstance(text, str) and text.strip():
            reason["text"] = quote_session_brief_text(text)
    if step.get("kind") == "memory" and isinstance(row, Mapping):
        step["writer"] = memory_writer(store, row)
        return
    author = step.get("author")
    step["writer"] = writer_attribution(
        {"created_by_agent_id": author} if isinstance(author, str) and author not in {"owner", "importer"} else None
    )


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
    kept: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if row.get("deleted_at") and not _text(_meta(row).get("superseded_by")):
            continue
        kept[str(row["id"])] = row
    return kept


def _plain_row(row: Mapping[str, object]) -> dict[str, object]:
    """Copy one store row and turn UUID objects into text.

    PostgreSQL returns ``id``, ``superseded_by`` and ``supersedes`` as UUID
    objects. The walk and the chain only follow those links when they are
    text. SQLite already returns text, and this copy leaves that text as it is.
    """

    return {
        str(key): str(value) if type(value).__name__ == "UUID" else value
        for key, value in row.items()
    }


def _fetch(store: Any, query: str, params: tuple[object, ...]) -> list[Mapping[str, object]]:
    # Every placeholder in these reads is a uuid column. Postgres will not
    # compare uuid to text, and it returns those columns as UUID objects.
    if type(store).__name__ == "PostgresVNextStore":
        query = query.replace("?", "%s::uuid")
    return [_plain_row(row) for row in store._fetch_all(query, params) if isinstance(row, Mapping)]
