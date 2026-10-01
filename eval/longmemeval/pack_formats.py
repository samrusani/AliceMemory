"""Structured (JSON) rendering of Alice's LongMemEval context pack.

Research adoption: the LongMemEval authors' own reading-format ablation found
JSON the best of four formats under oracle retrieval (up to +10 absolute for
gpt-4o). Alice is at ~95% evidence coverage — their exact regime — so the
harness can render the SAME pack content as a compact JSON document instead
of the prose block.

Honesty boundary: this module shapes PACK CONTENT/FORMAT only — the history
slot of the official reading template receives the document; the template's
instruction text is untouched (sha-pinned in tests). Every field is
question-agnostic retrieval output (claims, session labels/dates, provenance
roles, validity annotations).

Correction (2026-10-01). An earlier version of this docstring denied that
any benchmark label reaches the document. That was false. LongMemEval names every
evidence session ``answer_...`` and no filler session so, and the harness
before 1.1 put that raw session id in each excerpt record (and in the prose
excerpt header), so the reader could see which sessions held the evidence.
The adapter now hands this module a session LABEL, a keyed hash from
:mod:`longmemeval.session_labels`, unless the run asks for raw labels to
reproduce an old run. This module formats whatever label it is given; the
``session_id`` key in the excerpt record holds that label.

Format contract (``PACK_FORMAT_JSON``)::

    {
      "memories": [
        {"id", "claim", "date", "date_iso", "provenance_role",
         "validity"?, "currency"?}, ...
      ],
      "session_excerpts": [
        {"session_id", "date", "date_iso", "excerpt_index", "excerpt"}, ...
      ],
      "notes": [ ... ],           # only when grounding notes exist
      "derived_timeline": [ ... ] # only when the pack carries precomputed
                                  # date arithmetic (see append_derived_timeline)
    }

* ``date`` is the session date exactly as prose shows it: memory records
  carry the prose fact line's ISO-normalized form (``2023-05-28 (Sun)
  20:27`` — weekday and time kept), excerpt records the raw LongMemEval
  ``2023/05/28 (Sun) 20:27`` shape of the prose excerpt header;
  ``date_iso`` is the normalized ``YYYY-MM-DD`` prefix either way, omitted
  when not parseable.
* ``validity`` / ``currency`` are pass-throughs of the same pack-item
  annotations the prose fact lines render; absent annotations are omitted
  cleanly, so the schema is stable whether or not sibling branches that
  produce them have landed.
* Serialization is compact (no pretty-print bloat) with ``ensure_ascii=False``
  and stable key order, so identical inputs render byte-identical documents.
"""

from __future__ import annotations

import json
import re
from typing import Mapping

PACK_FORMAT_PROSE = "prose"
PACK_FORMAT_JSON = "json"
PACK_FORMATS = (PACK_FORMAT_PROSE, PACK_FORMAT_JSON)
DEFAULT_PACK_FORMAT = PACK_FORMAT_PROSE

# Optional per-memory pack annotations rendered as fields when present
# (sibling branches populate them; omitted cleanly when absent).
_MEMORY_PASSTHROUGH_KEYS = ("validity", "currency")

_SLASH_DATE_PREFIX = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")
_ISO_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def compact_json(value: object) -> str:
    """Deterministic compact serialization (the whole document and its parts)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def iso_date_prefix(raw: object) -> str | None:
    """``YYYY-MM-DD`` prefix of a session-date string, or ``None``.

    Handles LongMemEval's ``2023/05/28 (Sun) 20:27`` shape and ISO-prefixed
    timestamps; anything else (e.g. ``"undated"``) yields ``None`` so the
    ``date_iso`` field is omitted rather than fabricated.
    """
    text = str(raw or "").strip()
    slash = _SLASH_DATE_PREFIX.match(text)
    if slash:
        year, month, day = slash.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if _ISO_DATE_PREFIX.match(text):
        return text[:10]
    return None


def memory_record(memory: Mapping[str, object], *, claim: str, date: str) -> dict[str, object]:
    """One ``memories[]`` record from a pack memory item.

    ``claim`` and ``date`` are the exact strings the prose fact line renders
    (text and session date), so the two formats carry identical content.
    """
    record: dict[str, object] = {}
    memory_id = str(memory.get("id") or "").strip()
    if memory_id:
        record["id"] = memory_id
    record["claim"] = claim
    record["date"] = date
    date_iso = iso_date_prefix(date)
    if date_iso is not None:
        record["date_iso"] = date_iso
    metadata = memory.get("metadata_json")
    if isinstance(metadata, Mapping):
        provenance_role = str(metadata.get("provenance_role") or "").strip()
        if provenance_role:
            record["provenance_role"] = provenance_role
    for key in _MEMORY_PASSTHROUGH_KEYS:
        value = memory.get(key)
        if isinstance(value, Mapping) and value:
            record[key] = dict(value)
        elif value not in (None, "", {}, []) and not isinstance(value, Mapping):
            record[key] = value
    return record


def excerpt_record(*, session_id: str, date: str, excerpt_index: int, excerpt: str) -> dict[str, object]:
    """One ``session_excerpts[]`` record (``excerpt_index`` is 1-based, like prose).

    ``session_id`` is the session label the adapter supplies (a keyed hash
    unless the run uses raw labels), never the dataset's id.
    """
    record: dict[str, object] = {"session_id": session_id, "date": date}
    date_iso = iso_date_prefix(date)
    if date_iso is not None:
        record["date_iso"] = date_iso
    record["excerpt_index"] = excerpt_index
    record["excerpt"] = excerpt
    return record


def document_envelope(
    memory_records: list[dict[str, object]],
    grounding_notes: list[str],
) -> tuple[str, str]:
    """``(prefix, suffix)`` of the document around the ``session_excerpts`` array.

    The caller streams excerpt records into the array under its character
    budget: ``len(prefix) + len(suffix)`` is the fixed cost, each record then
    costs ``len(compact_json(record)) + 1`` (a comma-conservative bound), so
    the assembled document never exceeds the budget the caller enforces.
    """
    prefix = '{"memories":' + compact_json(memory_records) + ',"session_excerpts":['
    suffix = "]"
    if grounding_notes:
        suffix += ',"notes":' + compact_json(grounding_notes)
    suffix += "}"
    return prefix, suffix


def assemble_document(prefix: str, excerpt_jsons: list[str], suffix: str) -> str:
    return prefix + ",".join(excerpt_jsons) + suffix


def append_derived_timeline(suffix: str, derived_lines: list[str]) -> str:
    """Suffix with the pack-level precomputed date arithmetic appended.

    The "[derived]" lines the prose format renders under its derived-values
    section land here as a top-level ``derived_timeline`` array (after
    ``notes`` when present). Empty input returns the suffix unchanged, so
    packs without derived values keep the byte-identical document shape.
    """
    if not derived_lines:
        return suffix
    return suffix[:-1] + ',"derived_timeline":' + compact_json(list(derived_lines)) + "}"


__all__ = [
    "DEFAULT_PACK_FORMAT",
    "PACK_FORMATS",
    "PACK_FORMAT_JSON",
    "PACK_FORMAT_PROSE",
    "append_derived_timeline",
    "assemble_document",
    "compact_json",
    "document_envelope",
    "excerpt_record",
    "iso_date_prefix",
    "memory_record",
]
