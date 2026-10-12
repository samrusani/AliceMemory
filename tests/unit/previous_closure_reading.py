"""The reading of ``source_refs`` and of the other id keys that the derived-label closure had at ``79b6382b``, before this change.

This is the closure's reading before the saved-quote reader's parser joined it, kept word for word as an oracle. A test
(``test_legacy_citation_differential.py``) runs it on the same records as the closure and fails when the closure finds fewer
ids than this reading did: the closure may find more (the reader's shapes, the incidental ids), never less.

Only the functions that read ids are here. The helpers they call (``identifier``, ``_metadata``, the key tables) are the
closure's own, unchanged since then.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from uuid import UUID

from alicebot_api.vnext_derived_labels import (
    _ID_KIND,
    _SKIP_RECURSE,
    _DERIVED_FROM_KIND,
    _CANONICAL_SOURCE_UUID,
    _READ_METADATA,
    _object,
    _uuid_strings,
    _nonempty_str,
    canon_kind,
    identifier,
    is_derived,
    _malformed_marker,
    _metadata,
)
from alicebot_api.vnext_source_fence import cited_source_ids

# Main read these four prefixes in a typed ref.
_REF_KINDS = {"source": "source", "memory": "memory", "open_loop": "open_loop", "artifact": "artifact"}


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
    # An exact canonical UUID contains one source and no surrounding text.
    # Every other shape still runs the complete saved-quote parser, including
    # encoded/split references and text naming more than one source.
    if type(value) is str and _CANONICAL_SOURCE_UUID.fullmatch(value):
        return "", {value.lower()}
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
    cache = _READ_METADATA.get()
    key = ("source-token", value)
    if cache is not None and key in cache:
        return cache[key]
    result = _parse_source_token(value)
    if cache is not None:
        cache[key] = result
    return result


def _parse_source_token(value: str) -> str | None:
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
                    source_ids.update(item for item in _strings(child) if item not in source_ids and _plain_token(item))
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


def previous_dependencies(kind: object, row: Mapping[str, object]) -> set[tuple[str, str]]:
    """What ``dependencies_of`` returned before this change for ``row``: the ids it read, with the kind it gave them."""

    name = canon_kind(kind)
    if not is_derived(name, row):
        return set()
    meta = _metadata(row)
    found: set[tuple[str, str]] = set()
    _collect_metadata_ids(meta, found)
    if "derived_from" in meta:
        found |= _derived_from_deps(meta.get("derived_from"))[1]
    found |= _value_dependencies(row)
    if name == "open_loop" and _nonempty_str(row.get("source_id")):
        found.add(("source", identifier(row.get("source_id"))))
    return found
