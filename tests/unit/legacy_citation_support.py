"""The citation shapes a report written before derived labels existed can hold, and the rows that hold them.

The consolidation report of v0.20.0 copies, as strings, the ``source_refs`` of the memories it clusters, and the weekly
candidate and the roll-up cards copy them too. A client wrote those strings (the commit route, the memory proposal and the
agent-output ingest keep a ref as sent), so a report holds whatever shape a client chose: a prefix in any case, an
``alice://`` URL, JSON text that quotes the memory, an id with braces or without hyphens. The shapes here are the ones the
saved-quote reader names (``cited_source_ids``, ``cited_memory_refs``), written out one by one so a test can state each.

Every builder returns the entry to put in a ``source_refs`` list. The row builders return the row as the version before
derived labels wrote it: a marker the server wrote, the refs as sent, and no ``derived_from``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import UUID

# How one id can be spelled. ``uuid.UUID`` takes every one of them, and so does the link writer.
SPELLINGS: dict[str, Callable[[str], str]] = {
    "canonical": lambda row_id: row_id,
    "capitals": lambda row_id: row_id.upper(),
    "no-hyphens": lambda row_id: row_id.replace("-", ""),
    "capitals-no-hyphens": lambda row_id: row_id.replace("-", "").upper(),
    "braces": lambda row_id: "{" + row_id + "}",
    "urn": lambda row_id: "urn:uuid:" + row_id,
    "padded": lambda row_id: "  " + row_id + "  ",
}


def memory_shapes(quote: str) -> dict[str, Callable[[str], object]]:
    """Each way a ref can name a memory, as a function of the (already spelled) id."""

    return {
        "prefix": lambda x: f"memory:{x}",
        "prefix-capitals": lambda x: f"MEMORY:{x}",
        "prefix-mixed-case": lambda x: f"MeMoRy:{x}",
        "prefix-twice": lambda x: f"memory: memory:{x}",
        "prefix-spaced": lambda x: f"  memory:  {x}",
        "prefix-fragment": lambda x: f"memory:{x}#chunk-1",
        "prefix-in-a-sentence": lambda x: f"copied from memory:{x} on Monday",
        "url": lambda x: f"alice://memories/{x}",
        "url-capitals": lambda x: f"ALICE://MEMORIES/{x}",
        "url-in-a-sentence": lambda x: f"see alice://memories/{x} for the words",
        "json-object": lambda x: json.dumps({"memory_id": x, "quote": quote}),
        "json-object-spaced": lambda x: "  \n" + json.dumps({"memory_id": x, "quote": quote}, indent=2) + "\n ",
        "json-list": lambda x: json.dumps([{"memory_id": x, "quote": quote}]),
        "json-ids": lambda x: json.dumps({"memory_ids": [x]}),
        "json-refs": lambda x: json.dumps({"memory_refs": [x], "quote": quote}),
        "json-selected": lambda x: json.dumps({"selected_memory_ids": [x]}),
        "json-memories": lambda x: json.dumps({"memories": [x]}),
        "json-prefixed": lambda x: json.dumps({"ref": f"memory:{x}", "quote": quote}),
        "json-in-json": lambda x: json.dumps({"entry": json.dumps({"memory_id": x, "quote": quote})}),
        "json-repeated-key": lambda x: '{"memory_id": "00000000-0000-0000-0000-00000000dead", "memory_id": "' + x + '"}',
        "object": lambda x: {"memory_id": x, "quote": quote},
        "object-ids": lambda x: {"memory_ids": [x], "quote": quote},
        "object-json-inside": lambda x: {"note": json.dumps({"memory_id": x, "quote": quote})},
    }


def source_shapes(quote: str) -> dict[str, Callable[[str], object]]:
    """Each way a ref can name a source, as a function of the (already spelled) id."""

    return {
        "bare": lambda x: x,
        "prefix": lambda x: f"source:{x}",
        "prefix-capitals": lambda x: f"SOURCE:{x}",
        "prefix-twice": lambda x: f"source: source:{x}",
        "prefix-fragment": lambda x: f"source:{x}#chunk-1",
        "url": lambda x: f"alice://sources/{x}",
        "url-capitals": lambda x: f"ALICE://SOURCES/{x}",
        "json-object": lambda x: json.dumps({"source_id": x, "quote": quote}),
        "json-list": lambda x: json.dumps([{"source_id": x, "quote": quote}]),
        "json-ids": lambda x: json.dumps({"source_ids": [x]}),
        "json-sources": lambda x: json.dumps({"sources": [x]}),
        "json-selected": lambda x: json.dumps({"selected_source_ids": [x]}),
        "json-prefixed": lambda x: json.dumps({"ref": f"source:{x}", "quote": quote}),
        "json-in-json": lambda x: json.dumps({"entry": json.dumps({"source_id": x, "quote": quote})}),
        "object": lambda x: {"source_id": x, "quote": quote},
        "object-ids": lambda x: {"source_ids": [x], "quote": quote},
    }


# Where a ref sits in the record of a row, as a function of the entries (a list of refs).
def _top(entries: list[object]) -> dict[str, object]:
    return {"source_refs": entries}


def _consolidation(entries: list[object]) -> dict[str, object]:
    # The consolidation report keeps the refs it copied at the top and again in each proposal.
    return {"consolidation": {"proposals": [{"source_refs": entries, "cluster_member_ids": []}]}}


METADATA_POSITIONS: dict[str, Callable[[list[object]], dict[str, object]]] = {
    "source_refs": _top,
    "proposal-source_refs": _consolidation,
    "agentic_memory": lambda entries: {"agentic_memory": {"source_refs": entries}},
    "provenance": lambda entries: {"provenance": entries},
}


def report(entries: list[object], *, position: str = "source_refs", workflow: str = "memory_consolidation") -> dict[str, object]:
    """An artifact row as the version before derived labels wrote a consolidation report that copied ``entries``."""

    metadata: dict[str, object] = {
        "workflow": workflow,
        "workflow_type": workflow,
        "generated_by": "scheduler",
        "review_status": "needs_review",
        "input_counts": {"clusters": 1, "proposals": 1, "active_memories": 3},
        "project_scope": [],
        "candidate_memory_ids": [],
        **METADATA_POSITIONS[position](entries),
    }
    return {
        "artifact_type": "memory_consolidation" if workflow == "memory_consolidation" else workflow,
        "title": "Memory Consolidation",
        "content_markdown": "No near-duplicate cluster needs review.",
        "status": "needs_review",
        "domain": "unknown",
        "sensitivity": "internal",
        "metadata_json": metadata,
    }


def candidate_memory(entries: list[object], *, position: str = "source_refs") -> dict[str, object]:
    """A consolidation candidate memory as it was written: the marker, the refs of its members, a stored label below them."""

    metadata: dict[str, object] = {
        "candidate_kind": "memory_consolidation",
        "review_required": True,
        "consolidation": {"proposal_kind": "dedup", "cluster_member_ids": []},
        "project_scope": [],
    }
    value: dict[str, object] = {"kind": "memory_consolidation_proposal", "text": "The kiln is fired on Mondays."}
    if position == "value":
        value["source_refs"] = entries
    else:
        metadata.update(METADATA_POSITIONS[position](entries))
    return {
        "memory_key": "legacy-candidate",
        "memory_type": "semantic",
        "canonical_text": "The kiln is fired on Mondays.",
        "status": "candidate",
        "domain": "unknown",
        "sensitivity": "internal",
        "metadata_json": metadata,
        "value": value,
    }


def open_loop(entries: list[object], *, source_id: str | None = None) -> dict[str, object]:
    """A loop a daily brief found: ``discovered_by``, the source it came from, and refs as sent."""

    metadata: dict[str, object] = {"discovered_by": "vnext_daily_brief", "project_scope": [], "source_refs": entries}
    if source_id is not None:
        metadata["source_id"] = source_id
    return {
        "title": "Check the kiln",
        "status": "open",
        "domain": "unknown",
        "sensitivity": "internal",
        "source_id": source_id,
        "metadata_json": metadata,
    }


def canonical(row_id: object) -> str:
    return str(UUID(str(row_id)))
