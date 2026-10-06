"""Pure labels for a derived row: domain, sensitivity, and project requirement.

Nothing in the product calls this module until a later change wires it in.
The same functions serve generation, a relabel, a read, and the stored-row
repair, so a rule lives here once.

A derived row is found by a marker the server wrote, never by reading text.
``value.kind`` alone does not make a row derived.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import UUID
from typing import Any, TypeVar, overload

from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS
from alicebot_api.vnext_derived_domain import derived_domain
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError, _input_groups
from alicebot_api.vnext_project_scope import (
    is_global_scope,
    normalize_project_scope,
    project_floor_shape,
    project_identifier_identity,
    project_scope_identity,
    resolve_project_scope,
    source_project_scope,
)
from alicebot_api.vnext_source_fence import cited_source_ids

# Public 1, internal 2, unknown 2, private 3, confidential 4,
# highly_sensitive 5, sacred 6, regulated 6. Raise only when the rank is
# strictly higher, so unknown and internal never swap.
SENSITIVITY_RANK: dict[str, int] = {
    "public": 1,
    "internal": 2,
    "unknown": 2,
    "private": 3,
    "confidential": 4,
    "highly_sensitive": 5,
    "sacred": 6,
    "regulated": 6,
}

HOP_BOUND = 32
NODE_BOUND = 5_000
PROPAGATION_BOUND = 100_000

DERIVED_WORKFLOWS = frozenset(
    {
        "daily_brief",
        "weekly_synthesis",
        "connection_finder",
        "contradiction_finder",
        "memory_consolidation",
        "project_auto_update",
        "staleness_sweep",
        "open_loop_review",
    }
)
DERIVED_ARTIFACT_TYPES = frozenset(
    {
        "daily_brief",
        "weekly_synthesis",
        "connection_report",
        "contradiction_report",
        "memory_consolidation",
        "open_loop_report",
    }
)
_LEGACY_SUMMARY_KINDS = frozenset({"daily_brief", "weekly_synthesis"})
_LEGACY_COUNT_KINDS = frozenset(
    {"connection_finder", "contradiction_finder", "connection_report", "contradiction_report"}
)

# Top-level marker and record keys. A write that omits one of these keeps the
# stored value. Label keys (project_scope, project_floor) are a separate set.
MARKER_KEYS = frozenset(
    {
        "consolidation",
        "candidate_kind",
        "discovered_by",
        "workflow",
        "source_artifact_id",
        "source_id",
        "input_summary",
        "derived_from",
        "source_ids",
        "memory_ids",
        "open_loop_ids",
        "artifact_ids",
        "belief_ids",
        "source_refs",
        "stale_marked_memory_ids",
        "connector_name",
        "input_counts",
        "candidate_memory_ids",
        "artifact_id",
        "cluster_member_ids",
        "cluster_membership",
        "member_ids",
        "member_snapshots",
    }
)

# Id keys the v2 planner already reads. The v3 set includes every one of them.
V2_ID_KEYS = frozenset(
    {
        "source_ids",
        "memory_ids",
        "open_loop_ids",
        "artifact_ids",
        "member_ids",
        "cluster_member_ids",
        "cluster_membership",
        "stale_marked_memory_ids",
        "belief_ids",
        "artifact_id",
        "source_artifact_id",
    }
)

_ID_KIND = {
    "source_ids": "source",
    "source_id": "source",
    "source_artifact_id": "artifact",
    "artifact_id": "artifact",
    "artifact_ids": "artifact",
    "memory_ids": "memory",
    "memory_id": "memory",
    "member_ids": "memory",
    "cluster_member_ids": "memory",
    "cluster_membership": "memory",
    "stale_marked_memory_ids": "memory",
    "open_loop_ids": "open_loop",
    "belief_ids": "belief",
}
_DERIVED_FROM_KIND = {
    "sources": "source",
    "memories": "memory",
    "open_loops": "open_loop",
    "artifacts": "artifact",
    "beliefs": "belief",
}
_REF_KINDS = {"source": "source", "memory": "memory", "open_loop": "open_loop", "artifact": "artifact"}
_SKIP_RECURSE = frozenset(
    {
        "quote",
        "content",
        "title",
        "description",
        "canonical_text",
        "summary",
        "content_markdown",
        "prompt",
        "text",
        "markdown",
        "body",
    }
)
_KIND_ALIASES = {
    "source": "source",
    "sources": "source",
    "memory": "memory",
    "memories": "memory",
    "open_loop": "open_loop",
    "open_loops": "open_loop",
    "artifact": "artifact",
    "generated_artifacts": "artifact",
    "project": "project",
    "projects": "project",
    "belief": "belief",
    "beliefs": "belief",
}

_EVENT_FIELDS = ("domain", "sensitivity", "project_scope", "project_floor")


class LabelPropagationTooLarge(ValueError):
    """A relabel would touch more derived rows than the propagation bound allows."""


def canon_kind(kind: object) -> str:
    """Return the short kind name (``memory``, not ``memories``)."""

    text = str(kind or "")
    if text not in _KIND_ALIASES:
        raise ValueError(f"unknown derived label kind: {text}")
    return _KIND_ALIASES[text]


def identifier(value: object) -> str:
    """Normalize any spelling ``uuid.UUID`` accepts. Anything else is kept as text."""

    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return str(value)


def _object(value: object) -> Mapping[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, Mapping) else {}


_READ_METADATA: ContextVar[dict | None] = ContextVar("label_read_metadata", default=None)


@contextmanager
def label_metadata_cache(cache: dict):
    """Reuse pure decoding only inside a guarded store snapshot.

    The caller owns invalidation on label writes and rollback. Strong raw
    references prevent object-id reuse; exiting the request drops every entry.
    """
    token = _READ_METADATA.set(cache)
    try:
        yield
    finally:
        _READ_METADATA.reset(token)


def _metadata(row: Mapping[str, object]) -> Mapping[str, object]:
    raw = row.get("metadata_json")
    cache = _READ_METADATA.get()
    cached = cache.get(id(raw)) if cache is not None else None
    if cached is not None and cached[0] is raw:
        return cached[1]
    normalized = _object(_uuid_strings(_object(raw)))
    if cache is not None:
        cache[id(raw)] = (raw, normalized)
    return normalized


def _uuid_strings(value: object) -> object:
    """Database UUID objects and JSON strings name the same recorded input."""
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _uuid_strings(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_uuid_strings(child) for child in value]
    return value


def _nonempty_str(value: object) -> bool:
    return isinstance(value, UUID) or isinstance(value, str) and bool(value.strip())


def _marker_in(value: object, choices: Iterable[str]) -> bool:
    return isinstance(value, str) and value in choices


def _malformed_marker(kind: str, meta: Mapping[str, object]) -> bool:
    key = "workflow" if kind == "artifact" else "candidate_kind" if kind == "memory" else None
    return key is not None and key in meta and not isinstance(meta[key], str)


def _scrubbed(row: Mapping[str, object]) -> bool:
    return _metadata(row).get("scrubbed") is True


def ordered_identifiers(values: Iterable[object]) -> tuple[str, ...]:
    """Stored spellings, first one kept, ordered by project identity."""

    chosen: dict[str, str] = {}
    for item in normalize_project_scope(list(values)):
        identity = project_identifier_identity(item)
        if identity and identity not in chosen:
            chosen[identity] = item
    return tuple(chosen[key] for key in sorted(chosen))


def is_derived(kind: object, row: Mapping[str, object]) -> bool:
    """True when ``row`` carries a server-written derived marker.

    A redacted row has no dependencies. A marker that lives only in ``value``
    does not count, so a forged ``value.kind`` does not make the row derived.
    """

    name = canon_kind(kind)
    if name in {"source", "belief"}:
        return False
    cache = _READ_METADATA.get()
    raw = row.get("metadata_json")
    key = ("derived", name, id(raw), repr(row.get("source_id")), repr(row.get("artifact_type")))
    cached = cache.get(key) if cache is not None else None
    if cached is not None and cached[0] is raw:
        return cached[1]
    result = _is_derived(name, row)
    if cache is not None:
        cache[key] = (raw, result)
    return result


def _is_derived(name: str, row: Mapping[str, object]) -> bool:
    meta = _metadata(row)
    if meta.get("redacted") is True:
        return False
    if name == "project":
        return "derived_from" in meta
    if name == "open_loop":
        if "derived_from" in meta:
            return True
        discovered = meta.get("discovered_by")
        if not _nonempty_str(discovered):
            return False
        return _nonempty_str(row.get("source_id")) or _nonempty_str(meta.get("source_id"))
    if name == "artifact":
        if "derived_from" in meta:
            return True
        if _malformed_marker(name, meta) or _marker_in(meta.get("workflow"), DERIVED_WORKFLOWS):
            return True
        if str(row.get("artifact_type") or "") in DERIVED_ARTIFACT_TYPES:
            return True
        return meta.get("connector_name") == "agent_output"
    if isinstance(meta.get("consolidation"), Mapping):
        return True
    if _malformed_marker(name, meta) or _marker_in(meta.get("candidate_kind"), {"memory_consolidation", "memory_rollup"}):
        return True
    if meta.get("discovered_by") == "vnext_weekly_synthesis":
        return True
    if meta.get("workflow") == "project_auto_update":
        return True
    if _nonempty_str(meta.get("source_artifact_id")):
        return True
    if _nonempty_str(meta.get("source_id")):
        return True
    return "derived_from" in meta


def row_class(kind: object, row: Mapping[str, object]) -> str:
    """``report``, ``aggregate``, ``copy``, ``project_state`` or ``original``."""

    if not is_derived(kind, row):
        return "original"
    name = canon_kind(kind)
    if name == "project":
        return "project_state"
    if name == "artifact":
        return "report"
    if name == "open_loop":
        return "copy"
    meta = _metadata(row)
    if (
        _nonempty_str(meta.get("source_artifact_id"))
        or meta.get("discovered_by") == "vnext_weekly_synthesis"
        or meta.get("workflow") == "project_auto_update"
        or isinstance(meta.get("consolidation"), Mapping)
        or _marker_in(meta.get("candidate_kind"), {"memory_consolidation", "memory_rollup"})
    ):
        return "aggregate"
    return "copy"


def infer_kind(row: Mapping[str, object]) -> str:
    """Best-effort kind for a row that did not name one."""

    if "artifact_type" in row or (
        "content_markdown" in row and "memory_key" not in row and "canonical_text" not in row
    ):
        return "artifact"
    if "current_state" in row:
        return "project"
    if "content_hash" in row and "canonical_text" not in row and "memory_key" not in row:
        return "source"
    if "memory_key" in row or "canonical_text" in row:
        return "memory"
    return "open_loop"


def _floor_of(row: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    """``project_floor`` from the row or from parsed metadata."""

    if "project_floor" in row:
        return project_floor_shape(row)
    return project_floor_shape({"metadata_json": _metadata(row)})


def group_scope(row: Mapping[str, object], *, kind: str | None = None) -> tuple[str, ...]:
    """Identity of the scope united with the floor.

    An original row has no floor, so this is the identity of its scope.
    """

    name = canon_kind(kind) if kind is not None else infer_kind(row)
    scope = stored_scope(name, row)
    if not is_derived(name, row):
        return project_scope_identity(scope)
    shape, floor = _floor_of(row)
    if shape != "list":
        floor = ()
    return project_scope_identity([*scope, *floor])


def stored_scope(kind: object, row: Mapping[str, object]) -> tuple[str, ...]:
    """The scope a row has stored, before the effective-scope rule."""

    name = canon_kind(kind)
    if name == "source":
        if _scrubbed(row):
            return ()
        return source_project_scope(row)
    if name == "project":
        return ()
    return resolve_project_scope(row).values


def carries_scope(kind: object, row: Mapping[str, object]) -> bool:
    """False for a scrubbed source and for a project row. Both carry no scope."""

    name = canon_kind(kind)
    if name == "project":
        return False
    if name == "source" and _scrubbed(row):
        return False
    return True


def _strings(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(identifier(value))
    elif isinstance(value, list):
        for item in value:
            found.extend(_strings(item))
    return found


def _as_string_list(value: object) -> tuple[str, list[str]] | None:
    """``(problem, ids)`` or None when ``value`` is not a list of strings.

    A repeated id is kept, so the counts check can see it and skip.
    """

    if value is None:
        return None
    if not isinstance(value, list):
        return ("malformed", [])
    if any(not isinstance(item, str) for item in value):
        return ("malformed", [])
    return ("", _strings(value))


def _source_ids_from(value: object) -> tuple[str, set[str]]:
    """Named source ids, including every spelling the saved-quote reader names."""

    if value is None:
        return "", set()
    named = {identifier(item) for item in cited_source_ids(value).named}
    problem = ""
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                token = _source_token(item)
                if token:
                    named.add(token)
            elif isinstance(item, Mapping):
                nested_problem, nested = _source_ids_from(item)
                problem = problem or nested_problem
                named |= nested
            else:
                return "malformed", set()
    elif isinstance(value, str):
        token = _source_token(value)
        if token:
            named.add(token)
        elif value.strip() and not named:
            # A sentence is not a source id. cited_source_ids already kept the
            # explicit ones. A whole token that is not a uuid still counts.
            pass
    elif isinstance(value, Mapping):
        for key, child in value.items():
            key_text = key.lower() if isinstance(key, str) else ""
            if key_text in _SKIP_RECURSE:
                continue
            child_problem, child_ids = _source_ids_from(child)
            problem = problem or child_problem
            named |= child_ids
    else:
        return "malformed", set()
    return problem, named


def _plain_token(value: str) -> bool:
    text = value.strip()
    if not text or any(character.isspace() for character in text):
        return False
    return ":" not in text and "/" not in text


def _source_token(value: str) -> str | None:
    text = value.strip()
    lowered = text.lower()
    for prefix in ("urn:uuid:", "source:", "uuid:"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            lowered = text.lower()
    if not text or any(character.isspace() for character in text):
        return None
    if ":" in text or "/" in text:
        return None
    try:
        return str(UUID(text))
    except (ValueError, AttributeError, TypeError):
        return None


def _typed_refs(value: object) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    nodes: list[object] = [value]
    while nodes:
        node = nodes.pop()
        if isinstance(node, str):
            kind, separator, row_id = node.partition(":")
            if separator and kind in _REF_KINDS and row_id and _plain_token(row_id):
                found.add((_REF_KINDS[kind], identifier(row_id)))
        elif isinstance(node, list):
            nodes.extend(node)
        elif isinstance(node, Mapping):
            nodes.extend(node.values())
    return found


def _add_ids(found: set[tuple[str, str]], kind: str, values: Iterable[str]) -> None:
    for item in values:
        if item:
            found.add((kind, item))


def _collect_metadata_ids(value: object, found: set[tuple[str, str]]) -> str:
    """Walk server-written metadata. Return ``malformed`` or ``""``."""

    problem = ""
    nodes: list[object] = [value]
    while nodes:
        node = nodes.pop()
        if isinstance(node, list):
            nodes.extend(node)
            continue
        if not isinstance(node, Mapping):
            continue
        for key, child in node.items():
            if not isinstance(key, str) or key in _SKIP_RECURSE:
                continue
            if key == "derived_from":
                continue
            if key in {"source_id", "source_ids"}:
                if isinstance(child, (str, list)):
                    child_problem, source_ids = _source_ids_from(child)
                    problem = problem or child_problem
                    source_ids.update(item for item in _strings(child) if _plain_token(item))
                    _add_ids(found, "source", source_ids)
                else:
                    problem = problem or "malformed"
                continue
            if key == "source_refs":
                child_problem, source_ids = _source_ids_from(child)
                problem = problem or child_problem
                _add_ids(found, "source", source_ids)
                found.update(ref for ref in _typed_refs(child) if ref[0] != "source" or ref[1])
                continue
            if key == "member_snapshots" and isinstance(child, list):
                for item in child:
                    if isinstance(item, Mapping) and _nonempty_str(item.get("id")):
                        found.add(("memory", identifier(item.get("id"))))
                    elif isinstance(item, str):
                        found.add(("memory", identifier(item)))
                continue
            if key == "cluster_membership" and isinstance(child, list) and any(isinstance(item, list) for item in child):
                # Consolidation records one list of member IDs per cluster.
                # Keep its established nested JSON shape, but reject mixed or
                # non-string membership rather than silently omitting inputs.
                if any(not isinstance(cluster, list) or any(not isinstance(item, str) for item in cluster) for cluster in child):
                    problem = problem or "malformed"
                else:
                    for cluster in child:
                        _add_ids(found, "memory", _strings(cluster))
                continue
            if key in _ID_KIND:
                parsed = _as_string_list(child if isinstance(child, list) else [child] if isinstance(child, str) else child)
                if parsed is None:
                    continue
                child_problem, ids = parsed
                if child_problem:
                    problem = problem or child_problem
                else:
                    _add_ids(found, _ID_KIND[key], ids)
                continue
            if isinstance(child, (Mapping, list)):
                nodes.append(child)
    return problem


def _derived_from_deps(record: object) -> tuple[str, set[tuple[str, str]]]:
    """Read the canonical record. A bad shape is ``malformed`` or ``counts_disagree``."""

    if not isinstance(record, Mapping):
        return "malformed", set()
    found: set[tuple[str, str]] = set()
    lists: dict[str, list[str]] = {}
    for key, kind in _DERIVED_FROM_KIND.items():
        if key not in record:
            lists[key] = []
            continue
        raw = record.get(key)
        if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
            return "malformed", set()
        ids = _strings(raw)
        lists[key] = ids
        _add_ids(found, kind, ids)
    counts = record.get("counts")
    if counts is None:
        if any(lists.values()):
            return "counts_disagree", found
        return "", found
    if not isinstance(counts, Mapping):
        return "malformed", found
    for key, ids in lists.items():
        if key not in counts:
            if ids:
                return "counts_disagree", found
            continue
        expected = counts.get(key)
        if not isinstance(expected, int) or isinstance(expected, bool):
            return "malformed", found
        if expected != len(ids):
            return "counts_disagree", found
    return "", found


def _legacy_count_problem(kind: str, row: Mapping[str, object], meta: Mapping[str, object]) -> str:
    """Disagreeing legacy counts on the four producers that write them."""

    workflow = str(meta.get("workflow") or "")
    artifact_type = str(row.get("artifact_type") or "")
    summary = meta.get("input_summary")
    if workflow in _LEGACY_SUMMARY_KINDS or artifact_type in _LEGACY_SUMMARY_KINDS:
        if isinstance(summary, Mapping):
            problem = _counts_against_lists(
                summary.get("counts"),
                {
                    "sources": summary.get("source_ids"),
                    "memories": summary.get("memory_ids"),
                    "open_loops": summary.get("open_loop_ids"),
                    "artifacts": summary.get("artifact_ids"),
                },
            )
            if problem:
                return "legacy_counts"
    if workflow in _LEGACY_COUNT_KINDS or artifact_type in _LEGACY_COUNT_KINDS:
        problem = _counts_against_lists(
            meta.get("input_counts"),
            {
                "sources": meta.get("source_ids"),
                "memories": meta.get("memory_ids"),
                "beliefs": meta.get("belief_ids"),
                "open_loops": meta.get("open_loop_ids"),
                "artifacts": meta.get("artifact_ids"),
            },
        )
        if problem:
            return "legacy_counts"
    return ""


def _counts_against_lists(counts: object, lists: Mapping[str, object]) -> str:
    if counts is None:
        return ""
    if not isinstance(counts, Mapping):
        return "malformed"
    present_lists: dict[str, list[object]] = {}
    for key, raw in lists.items():
        if raw is None:
            continue
        if not isinstance(raw, list):
            return "malformed"
        present_lists[key] = list(raw)
    if any(len(values) != len(set(map(str, values))) for values in present_lists.values()):
        return ""
    for key in lists:
        if key not in counts:
            continue
        expected = counts.get(key)
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            return "malformed"
        if expected != len(present_lists.get(key, [])):
            return "counts_disagree"
    return ""


def _record_present(meta: Mapping[str, object], row: Mapping[str, object]) -> bool:
    if "derived_from" in meta or "input_summary" in meta or "source_refs" in meta or "input_counts" in meta:
        return True
    for key in (
        "source_ids",
        "memory_ids",
        "open_loop_ids",
        "artifact_ids",
        "belief_ids",
        "stale_marked_memory_ids",
        "source_id",
        "source_artifact_id",
        "artifact_id",
    ):
        if key in meta:
            return True
    consolidation = meta.get("consolidation")
    if isinstance(consolidation, Mapping) and consolidation:
        return True
    if _nonempty_str(row.get("source_id")):
        return True
    return False


def dependencies_of(kind: object, row: Mapping[str, object]) -> frozenset[tuple[str, str]]:
    """Recorded inputs as ``(kind, normalized id)``.

    Belief ids are returned as ``belief`` and resolved by :func:`settle_labels`.
    A row that is not derived has no dependencies. The value fields are read
    only when a metadata marker is already present.
    """

    deps, _problem = dependency_record(kind, row)
    return frozenset(deps)


def dependency_record(kind: object, row: Mapping[str, object]) -> tuple[frozenset[tuple[str, str]], str]:
    """Dependencies and a structural problem, or ``""`` when the record is sound.

    An empty record is sound. A missing record on a derived row is ``no_record``.
    """

    # Closure walks and settlement revisit the same raw metadata within one
    # guarded snapshot. Cache parsing, never labels or admission. The top-level
    # fields used by this parser remain part of the key, including presence.
    cache = _READ_METADATA.get()
    raw = row.get("metadata_json")
    key = ("dependencies", canon_kind(kind), id(raw), *((field in row, repr(row.get(field)))
           for field in ("value", "source_id", "artifact_type", "project_floor")))
    cached = cache.get(key) if cache is not None else None
    if cached is not None and cached[0] is raw:
        return cached[1]
    result = _dependency_record(kind, row)
    if cache is not None:
        cache[key] = (raw, result)
    return result


def _dependency_record(kind: object, row: Mapping[str, object]) -> tuple[frozenset[tuple[str, str]], str]:
    if not is_derived(kind, row):
        return frozenset(), ""
    meta = _metadata(row)
    found: set[tuple[str, str]] = set()
    problem = "malformed_marker" if _malformed_marker(canon_kind(kind), meta) else ""
    problem = problem or _collect_metadata_ids(meta, found)
    if "derived_from" in meta:
        derived_problem, derived_deps = _derived_from_deps(meta.get("derived_from"))
        problem = problem or derived_problem
        found |= derived_deps
    if is_derived(kind, row):
        found |= _value_dependencies(row)
    if canon_kind(kind) == "open_loop" and _nonempty_str(row.get("source_id")):
        found.add(("source", identifier(row.get("source_id"))))
    floor_shape, _floor = _floor_of(row)
    if floor_shape == "malformed":
        problem = problem or "malformed_floor"
    if not problem:
        problem = _legacy_count_problem(canon_kind(kind), row, meta)
    if not problem and not _record_present(meta, row) and not found:
        # A weekly candidate may still be completed from its parent artifact
        # inside settle_labels. The structural pass says no_record until then.
        problem = "no_record"
    if problem:
        return frozenset(found), problem
    return frozenset(found), ""


def _value_dependencies(row: Mapping[str, object]) -> set[tuple[str, str]]:
    value = _object(_uuid_strings(_object(row.get("value"))))
    found: set[tuple[str, str]] = set()
    if _nonempty_str(value.get("source_id")):
        found.add(("source", identifier(value.get("source_id"))))
    if _nonempty_str(value.get("artifact_id")):
        found.add(("artifact", identifier(value.get("artifact_id"))))
    parsed = _as_string_list(value.get("cluster_member_ids")) if "cluster_member_ids" in value else None
    if parsed and not parsed[0]:
        _add_ids(found, "memory", parsed[1])
    rollup = value.get("rollup")
    if isinstance(rollup, Mapping):
        members = _as_string_list(rollup.get("member_ids")) if "member_ids" in rollup else None
        if members and not members[0]:
            _add_ids(found, "memory", members[1])
    return found


def _raised_sensitivity(current: str, inputs: Iterable[str]) -> str:
    """Highest rank. A lower rank never replaces the current label."""

    current_rank = SENSITIVITY_RANK.get(current, SENSITIVITY_RANK["unknown"])
    chosen = current
    chosen_rank = current_rank
    for value in inputs:
        rank = SENSITIVITY_RANK.get(str(value), SENSITIVITY_RANK["unknown"])
        if rank > chosen_rank:
            chosen = str(value)
            chosen_rank = rank
    return chosen


def union_floor(stored: Sequence[str], parts: Iterable[Sequence[str]]) -> tuple[str, ...]:
    """Stored floor united with every part. The stored projects stay."""

    combined = [*stored, *[item for part in parts for item in part]]
    return ordered_identifiers(combined)


def intersect_scope(stored: Sequence[str], parent: Sequence[str]) -> tuple[str, ...]:
    parent_ids = set(project_scope_identity(parent))
    return ordered_identifiers(item for item in stored if project_identifier_identity(item) in parent_ids)


def generation_domain(payload_domain: str, dep_domains: Iterable[str]) -> str:
    """Insert rule: a restricted payload domain is kept. Otherwise the derived domain."""

    if payload_domain in RESTRICTED_DOMAINS:
        return payload_domain
    return derived_domain(({"domain": item} for item in dep_domains), fallback=payload_domain)


def labels_raised_payload(*, cause: str, previous: Mapping[str, object], new: Mapping[str, object]) -> dict[str, object]:
    """One audit payload. Labels only: no text, titles or input ids."""

    def side(source: Mapping[str, object]) -> dict[str, object]:
        scope = source.get("project_scope", ())
        floor = source.get("project_floor", ())
        return {
            "domain": str(source.get("domain") or "unknown"),
            "sensitivity": str(source.get("sensitivity") or "unknown"),
            "project_scope": list(scope) if isinstance(scope, (list, tuple)) else [],
            "project_floor": list(floor) if isinstance(floor, (list, tuple)) else [],
        }

    return {"cause": cause, "previous": side(previous), "new": side(new)}


@dataclass(frozen=True, slots=True)
class SettledLabel:
    """Effective label of one stored row."""

    kind: str
    user_id: str
    stored_id: str
    normalized_id: str
    domain: str
    sensitivity: str
    project_scope: tuple[str, ...]
    project_floor: tuple[str, ...]
    stored_domain: str
    stored_sensitivity: str
    stored_scope: tuple[str, ...]
    stored_floor: tuple[str, ...]
    unverified: bool
    reason: str | None
    carries_scope: bool
    derived: bool
    row_class: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.user_id, self.normalized_id)


@dataclass(frozen=True, slots=True)
class SettleResult:
    """Settled labels, one entry per stored row, in input order."""

    rows: tuple[SettledLabel, ...]

    def by_stored(self, kind: str, stored_id: str, *, user_id: str | None = None) -> SettledLabel:
        name = canon_kind(kind)
        for row in self.rows:
            if row.kind == name and row.stored_id == str(stored_id) and (user_id is None or row.user_id == user_id):
                return row
        raise KeyError((name, stored_id))

    def derived_rows(self) -> tuple[SettledLabel, ...]:
        return tuple(row for row in self.rows if row.derived)


def _node_label(kind: str, row: Mapping[str, object]) -> SettledLabel:
    meta_floor_shape, floor = _floor_of(row)
    if meta_floor_shape != "list":
        floor = ()
    scope = stored_scope(kind, row)
    domain = str(row.get("domain") or "unknown")
    sensitivity = str(row.get("sensitivity") or "unknown")
    return SettledLabel(
        kind=kind,
        user_id=str(row.get("user_id") or ""),
        stored_id=str(row.get("id") or ""),
        normalized_id=identifier(row.get("id")),
        domain=domain,
        sensitivity=sensitivity,
        project_scope=scope,
        project_floor=floor,
        stored_domain=domain,
        stored_sensitivity=sensitivity,
        stored_scope=scope,
        stored_floor=floor,
        unverified=False,
        reason=None,
        carries_scope=carries_scope(kind, row),
        derived=is_derived(kind, row),
        row_class=row_class(kind, row),
    )


def dependency_syntax_key(kind: str, row: Mapping[str, object]) -> tuple:
    """Memoize parsing without allowing incidental scalar metadata to split it.

    Keep every marker, reference, count, scope alias and value dependency. Walk
    unknown containers as the dependency parser does; their scalar text cannot
    name an input. This is only a parsing key, never a settled label or grant.
    """
    relevant = MARKER_KEYS | _ID_KIND.keys() | {
        "redacted", "scrubbed", "project_scope", "project_floor", "project_id", "project", "projects",
        "scope_json", "metadata_json", "agent_identity", "agentic_memory", "consolidation",
    }

    def metadata_key(value: object) -> tuple:
        if isinstance(value, Mapping):
            parts: list[tuple[str, object]] = []
            for key, child in value.items():
                if not isinstance(key, str):
                    continue
                if key in relevant:
                    parts.append((key, repr(child)))
                elif isinstance(child, (Mapping, list)):
                    nested = metadata_key(child)
                    if nested:
                        parts.append((key, nested))
            return tuple(sorted(parts))
        if isinstance(value, list):
            return tuple(nested for child in value if isinstance(child, (Mapping, list))
                         and (nested := metadata_key(child)))
        return ()

    fields = ("user_id", "domain", "sensitivity", "value", "project_id", "project", "projects", "scope_json",
              "source_id", "artifact_type", "project_scope", "project_floor")
    return (canon_kind(kind), isinstance(row.get("metadata_json"), Mapping), metadata_key(_metadata(row)),
            *((field in row, repr(row.get(field))) for field in fields))


def dependency_label_signature(kind: str, row: Mapping[str, object]) -> tuple:
    """Fields that determine a root label, excluding identity and incidental text.

    A caller may share a verified settlement only when the root is outside its
    cached ancestry and no implicit per-candidate parent rule is involved.
    Structural errors are part of the signature, never normalized into validity.
    """
    name = canon_kind(kind)
    deps, problem = dependency_record(name, row)
    label = _node_label(name, row)
    return (name, deps, problem, label.row_class, label.stored_domain,
            label.stored_sensitivity, label.stored_scope, label.stored_floor,
            label.carries_scope, str(row.get("user_id") or ""))


def _copy_scope(stored: tuple[str, ...], parents: Sequence[SettledLabel]) -> tuple[str, ...]:
    live = [item for item in parents if item.carries_scope]
    if not parents:
        return stored
    if not live:
        # Every parent is a scrubbed source: keep the stored scope.
        return stored
    scope = stored
    for item in live:
        if len(project_scope_identity(item.project_scope)) == 0:
            return ()
        scope = intersect_scope(scope, item.project_scope)
    return scope


def _effective_scope(current: SettledLabel, deps: Sequence[SettledLabel], floor: tuple[str, ...]) -> tuple[str, ...]:
    if current.row_class == "project_state":
        return ()
    if current.row_class == "original" or not current.derived:
        return current.stored_scope
    informative = [item for item in deps if item.carries_scope]
    if not deps:
        return current.stored_scope
    any_empty = any(len(project_scope_identity(item.project_scope)) == 0 for item in informative)
    if current.row_class == "report":
        if any_empty:
            return ()  # a global input empties the report scope
        return current.stored_scope
    if current.row_class == "aggregate":
        stored = current.stored_scope
        single = len(project_scope_identity(stored)) == 1
        floor_inside = set(project_scope_identity(floor)).issubset(set(project_scope_identity(stored)))
        if single and not any_empty and floor_inside:
            return stored
        return ()  # aggregate rule otherwise leaves the scope empty
    parents = [item for item in deps if item.kind == "source"]
    return _copy_scope(current.stored_scope, parents)


def _apply_dependencies(
    current: SettledLabel,
    deps: Sequence[SettledLabel],
    *,
    domain_fallback: str | None = None,
    sensitivity_fallback: str | None = None,
    scope_fallback: tuple[str, ...] | None = None,
    floor_fallback: tuple[str, ...] | None = None,
) -> SettledLabel:
    """One row from the effective labels of its dependencies."""

    fallback_domain = current.domain if domain_fallback is None else domain_fallback
    fallback_sensitivity = current.sensitivity if sensitivity_fallback is None else sensitivity_fallback
    base = current
    if scope_fallback is not None or floor_fallback is not None:
        base = replace(
            current,
            stored_scope=current.stored_scope if scope_fallback is None else scope_fallback,
            stored_floor=current.stored_floor if floor_fallback is None else floor_fallback,
            domain=fallback_domain,
            sensitivity=fallback_sensitivity,
        )
    domain = derived_domain(({"domain": item.domain} for item in deps), fallback=fallback_domain)
    if domain not in RESTRICTED_DOMAINS and fallback_domain in RESTRICTED_DOMAINS:
        domain = fallback_domain
    sensitivity = _raised_sensitivity(fallback_sensitivity, (item.sensitivity for item in deps))
    informative = [item for item in deps if item.carries_scope]
    floor = union_floor(
        base.stored_floor,
        [*(item.project_scope for item in informative), *(item.project_floor for item in informative)],
    )
    if base.row_class == "project_state":
        floor = ()
    scope = _effective_scope(base, deps, floor)
    return replace(base, domain=str(domain), sensitivity=sensitivity, project_scope=scope, project_floor=floor)


def _changed(before: SettledLabel, after: SettledLabel) -> bool:
    return (
        before.domain != after.domain
        or before.sensitivity != after.sensitivity
        or project_scope_identity(before.project_scope) != project_scope_identity(after.project_scope)
        or project_scope_identity(before.project_floor) != project_scope_identity(after.project_floor)
    )


def _weekly_parent_deps(
    labels: Mapping[tuple[str, str, str], SettledLabel],
    own: dict[tuple[str, str, str], set[tuple[str, str, str]]],
    nodes: Sequence[tuple[SettledLabel, Mapping[str, object]]],
) -> None:
    """Old weekly candidates take the input lists of the artifact that names them."""

    artifacts = [
        (label, _metadata(row))
        for label, row in nodes
        if label.kind == "artifact" and isinstance(_metadata(row).get("input_summary"), Mapping)
    ]
    for label, meta in artifacts:
        for candidate in _strings(meta.get("candidate_memory_ids")):
            candidate_key = ("memory", label.user_id, candidate)
            candidate_label = labels.get(candidate_key)
            if candidate_label is None or candidate_label.row_class != "aggregate":
                continue
            if _metadata_discovered(nodes, candidate_key) != "vnext_weekly_synthesis":
                continue
            own.setdefault(candidate_key, set()).update(own.get(label.key, set()))


def _metadata_discovered(
    nodes: Sequence[tuple[SettledLabel, Mapping[str, object]]],
    key: tuple[str, str, str],
) -> object:
    for label, row in nodes:
        if label.key == key:
            return _metadata(row).get("discovered_by")
    return None


def settle_labels(
    nodes: Sequence[Mapping[str, object]],
    *,
    unavailable_kinds: Iterable[str] = (),
    on_cycle: str = "raise",
    max_hops: int | None = None,
    max_nodes: int | None = None,
) -> SettleResult:
    """Settle every derived row in ``nodes``.

    ``nodes`` are mappings with ``kind`` (or a table name) plus the row fields.
    Originals are fixed inputs. A cycle that does not settle raises
    ``DerivedDomainRepairError`` unless ``on_cycle`` is ``unverified``.
    ``max_hops`` and ``max_nodes`` bound a read. The repair leaves them unset.
    """

    if on_cycle not in {"raise", "unverified"}:
        raise ValueError("on_cycle must be raise or unverified")
    unavailable = {canon_kind(kind) for kind in unavailable_kinds}
    prepared: list[tuple[SettledLabel, Mapping[str, object]]] = []
    for node in nodes:
        kind = canon_kind(node.get("kind") if node.get("kind") is not None else infer_kind(node))
        row = node
        if "row" in node and isinstance(node.get("row"), Mapping):
            row = node["row"]  # type: ignore[assignment]
        prepared.append((_node_label(kind, row), row))

    labels: dict[tuple[str, str, str], SettledLabel] = {}
    order: list[SettledLabel] = []
    for label, _row in prepared:
        order.append(label)
        labels[label.key] = label

    own: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    problems: dict[tuple[str, str, str], str] = {}
    for label, row in prepared:
        if not label.derived:
            continue
        deps, problem = dependency_record(label.kind, row)
        if problem == "no_record" and label.row_class == "aggregate":
            # Filled from the parent artifact below when one names this row.
            problem = ""
            problems[label.key] = "no_record"
        elif problem:
            problems[label.key] = problem
        keyed = {(kind, label.user_id, row_id) for kind, row_id in deps}
        own[label.key] = keyed
    _weekly_parent_deps(labels, own, prepared)
    stored_ids: dict[tuple[str, str, str], str] = {}
    for label, _row in prepared:
        previous_id = stored_ids.setdefault(label.key, label.stored_id)
        if previous_id != label.stored_id:
            problems[label.key] = "ambiguous_identity"
    for key, reason in list(problems.items()):
        if reason == "no_record" and own.get(key):
            del problems[key]
        elif reason == "no_record" and not own.get(key):
            pass
        elif reason == "" and not own.get(key):
            pass

    def resolve_belief(ref: tuple[str, str, str]) -> tuple[str, str, str]:
        if ref[0] != "belief":
            return ref
        belief = labels.get(ref)
        if belief is None:
            return ref
        for label, row in prepared:
            if label.key == ref:
                memory_id = row.get("memory_id")
                if _nonempty_str(memory_id) or memory_id not in (None, ""):
                    return ("memory", ref[1], identifier(memory_id))
        return ref

    resolved: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for key, own_refs in own.items():
        resolved[key] = {resolve_belief(ref) for ref in own_refs}

    for key, resolved_refs in resolved.items():
        if key in problems and problems[key] not in {"", "no_record"}:
            continue
        for ref in resolved_refs:
            if ref[0] in unavailable:
                problems[key] = "missing_table"
                break
            if ref not in labels:
                problems[key] = "missing_dependency"
                break

    if max_hops is not None or max_nodes is not None:
        _mark_bounds(labels, resolved, problems, max_hops=max_hops, max_nodes=max_nodes)

    _spread_unverified(resolved, problems)

    inputs = {key: set(resolved.get(key, set())) for key, label in labels.items() if label.derived and key not in problems}
    for key in problems:
        inputs.pop(key, None)

    cycle_keys: set[tuple[str, str, str]] = set()
    if inputs:
        try:
            _iterate(labels, inputs, on_cycle=on_cycle, cycle_keys=cycle_keys, problems=problems)
        except DerivedDomainRepairError:
            if on_cycle != "raise":
                raise
            raise

    for key in cycle_keys:
        problems[key] = "cycle_unsettled"
    _spread_unverified(resolved, problems)

    published: list[SettledLabel] = []
    for label, row in prepared:
        current = labels[label.key]
        if not label.derived:
            published.append(label)
            continue
        if label.key in problems:
            published.append(
                replace(
                    label,
                    unverified=True,
                    reason=problems[label.key],
                    carries_scope=False,
                )
            )
            continue
        settled = labels[label.key]
        same_stored_row = (
            settled.stored_id == label.stored_id
            and settled.stored_domain == label.stored_domain
            and settled.stored_sensitivity == label.stored_sensitivity
            and settled.stored_scope == label.stored_scope
            and settled.stored_floor == label.stored_floor
        )
        if same_stored_row:
            published.append(replace(settled, unverified=False, reason=None))
            continue
        settled_deps = [labels[ref] for ref in sorted(resolved.get(label.key, set())) if ref in labels]
        recomputed = _apply_dependencies(
            settled,
            settled_deps,
            domain_fallback=label.stored_domain,
            sensitivity_fallback=label.stored_sensitivity,
            scope_fallback=label.stored_scope,
            floor_fallback=label.stored_floor,
        )
        published.append(
            replace(
                recomputed,
                stored_id=label.stored_id,
                stored_domain=label.stored_domain,
                stored_sensitivity=label.stored_sensitivity,
                stored_scope=label.stored_scope,
                stored_floor=label.stored_floor,
                unverified=False,
                reason=None,
            )
        )
    return SettleResult(rows=tuple(published))


def _spread_unverified(
    resolved: Mapping[tuple[str, str, str], set[tuple[str, str, str]]],
    problems: dict[tuple[str, str, str], str],
) -> None:
    """A row that reads an unverified row is unverified. The reason is contagious."""

    changed = True
    while changed:
        changed = False
        for key, deps in resolved.items():
            if key in problems:
                continue
            if any(ref in problems for ref in deps):
                problems[key] = "dependency_unverified"
                changed = True


def _mark_bounds(
    labels: Mapping[tuple[str, str, str], SettledLabel],
    resolved: Mapping[tuple[str, str, str], set[tuple[str, str, str]]],
    problems: dict[tuple[str, str, str], str],
    *,
    max_hops: int | None,
    max_nodes: int | None,
) -> None:
    """Mark a derived row unverified when the walk from that row passes a bound."""

    for origin, label in labels.items():
        if not label.derived or origin in problems:
            continue
        pending: deque[tuple[tuple[str, str, str], int]] = deque([(origin, 0)])
        seen: set[tuple[str, str, str]] = set()
        while pending:
            key, depth = pending.popleft()
            if key in seen:
                continue
            if max_nodes is not None and len(seen) >= max_nodes:
                problems.setdefault(origin, "bound_exceeded")
                break
            if max_hops is not None and depth > max_hops:
                problems.setdefault(origin, "bound_exceeded")
                break
            seen.add(key)
            for ref in resolved.get(key, set()):
                pending.append((ref, depth + 1))


def _iterate(
    labels: dict[tuple[str, str, str], SettledLabel],
    inputs: dict[tuple[str, str, str], set[tuple[str, str, str]]],
    *,
    on_cycle: str,
    cycle_keys: set[tuple[str, str, str]],
    problems: dict[tuple[str, str, str], str],
) -> None:
    dependants: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
    for key, refs in inputs.items():
        for ref in refs:
            dependants.setdefault(ref, set()).add(key)
    for component in _input_groups(inputs):
        members = set(component)
        pending: deque[tuple[str, str, str]] = deque(component)
        queued = set(component)
        remaining_changes = len(component) * (len(RESTRICTED_DOMAINS) + 1)
        changes: dict[tuple[str, str, str], int] = {}
        while pending:
            key = pending.popleft()
            queued.remove(key)
            current = labels[key]
            deps = [labels[ref] for ref in sorted(inputs[key]) if ref in labels]
            updated = _apply_dependencies(
                current,
                deps,
                domain_fallback=current.stored_domain,
                sensitivity_fallback=current.stored_sensitivity,
                scope_fallback=current.stored_scope,
                floor_fallback=current.stored_floor,
            )
            if not _changed(current, updated):
                continue
            remaining_changes -= 1
            changes[key] = changes.get(key, 0) + 1
            if remaining_changes < 0:
                if on_cycle == "unverified":
                    for member in members:
                        cycle_keys.add(member)
                        problems[member] = "cycle_unsettled"
                    return
                shown = ", ".join(f"{item[0]} {item[2]}" for item in sorted(changes)[:5])
                raise DerivedDomainRepairError(
                    "derived label repair did not settle: derived rows record each other as inputs in a cycle, "
                    f"so their labels kept changing (rows: {shown})."
                )
            labels[key] = updated
            for dependant in sorted(dependants.get(key, set()) & members):
                if dependant not in queued:
                    pending.append(dependant)
                    queued.add(dependant)


def locked_projects(agent_identity: object, requested: object) -> tuple[str, ...] | None:
    """The projects a locked key may read, or None when the caller is not locked."""

    if not isinstance(agent_identity, Mapping) or not agent_identity.get("project_scope_locked"):
        return None
    binding = agent_identity.get("project_scope") or ()
    requested_values = tuple(requested) if isinstance(requested, (list, tuple)) else ()
    if requested_values:
        return tuple(str(item) for item in requested_values)
    if isinstance(binding, (list, tuple)):
        return tuple(str(item) for item in binding)
    return ()


def input_admitted(kind: str, row: Mapping[str, object], projects: object) -> bool:
    """Exact-door project test: scope and floor are both inside ``projects``."""

    if canon_kind(kind) == "source":
        scope = source_project_scope(row)
    else:
        scope = resolve_project_scope(row).values
    shape, floor = project_floor_shape(row)
    if shape == "malformed":
        return False
    bound = set(project_scope_identity(projects))
    scope_ids = set(project_scope_identity(scope))
    if not scope_ids or not scope_ids <= bound:
        return False
    return set(project_scope_identity(floor)) <= bound


def stamp_derived_from(payload: dict[str, object], rows_by_kind: Mapping[str, object]) -> None:
    """Write the canonical dependency record onto ``payload['metadata_json']``."""

    metadata = payload.get("metadata_json")
    meta = dict(metadata) if isinstance(metadata, Mapping) else {}
    record: dict[str, object] = {"v": 1}
    counts: dict[str, int] = {}
    for key in ("sources", "memories", "open_loops", "artifacts", "beliefs"):
        raw_rows = rows_by_kind.get(key)
        rows = raw_rows if isinstance(raw_rows, (list, tuple)) else []
        ids = [str(row.get("id")) for row in rows if isinstance(row, Mapping) and row.get("id") is not None]
        record[key] = ids
        counts[key] = len(ids)
    record["counts"] = counts
    meta["derived_from"] = record
    payload["metadata_json"] = meta


def with_derived_from(metadata: Mapping[str, object], rows_by_kind: Mapping[str, object]) -> dict[str, object]:
    """A copy of ``metadata`` with ``derived_from`` for the rows a producer used."""

    payload: dict[str, object] = {"metadata_json": dict(metadata)}
    stamp_derived_from(payload, rows_by_kind)
    stamped = payload["metadata_json"]
    return dict(stamped) if isinstance(stamped, Mapping) else {}


_InputRow = TypeVar("_InputRow", bound=Mapping[str, object])


@overload
def admit_when_locked(kind: str, rows: Sequence[_InputRow], projects: tuple[str, ...] | None) -> list[_InputRow]: ...


@overload
def admit_when_locked(kind: str, rows: object, projects: tuple[str, ...] | None) -> list[Mapping[str, object]]: ...


def admit_when_locked(
    kind: str,
    rows: object,
    projects: tuple[str, ...] | None,
) -> list[Any]:
    """Keep every row when ``projects`` is None. Otherwise keep rows inside that binding."""

    items = list(rows) if isinstance(rows, (list, tuple)) else []
    if projects is None:
        return [row for row in items if isinstance(row, Mapping)]
    return [row for row in items if isinstance(row, Mapping) and input_admitted(kind, row, projects)]


def scope_is_global(scope: object) -> bool:
    """True when a scope holds no Alice project id."""

    return is_global_scope(scope)


__all__ = [
    "DERIVED_ARTIFACT_TYPES",
    "DERIVED_WORKFLOWS",
    "HOP_BOUND",
    "LabelPropagationTooLarge",
    "MARKER_KEYS",
    "NODE_BOUND",
    "PROPAGATION_BOUND",
    "SENSITIVITY_RANK",
    "SettleResult",
    "SettledLabel",
    "V2_ID_KEYS",
    "canon_kind",
    "carries_scope",
    "dependencies_of",
    "dependency_record",
    "generation_domain",
    "group_scope",
    "identifier",
    "infer_kind",
    "admit_when_locked",
    "input_admitted",
    "intersect_scope",
    "is_derived",
    "locked_projects",
    "with_derived_from",
    "labels_raised_payload",
    "ordered_identifiers",
    "row_class",
    "scope_is_global",
    "stamp_derived_from",
    "settle_labels",
    "stored_scope",
    "union_floor",
]
