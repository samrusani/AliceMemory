"""Alice adapter: ingest LongMemEval haystacks and answer via real retrieval.

Per question the harness creates an isolated SQLite store (one file, no
services), then:

1. **Ingest** — every haystack session is rendered as speaker-tagged turns
   (one paragraph per turn, so ``vnext_capture.chunk_text`` chunks on turn
   boundaries) and captured through the real ``VNextCaptureService`` write
   path: sources, chunks, provenance, event log and candidate memories.
   Candidate memories are then promoted to ``active`` with ``update_memory``
   (the store's review-accept patch), because Alice's search stages only see
   active/accepted memories. When ``ALICE_EMBEDDINGS_BASE_URL`` and
   ``ALICE_EMBEDDINGS_MODEL`` are set, each promoted memory is embedded via
   the real provider at that moment, as a reviewer's acceptance does in the
   product. Capture itself embeds nothing, because recall cannot return a
   candidate and its text is not sent to the endpoint.
2. **Retrieve** — ``VNextRetrievalService.compile_context_pack`` runs with
   the benchmark question as the query (hybrid FTS5 + vector KNN + RRF, or
   FTS-only when no embedding provider is configured), and the pack is
   rendered into a compact context block: retrieved memories first, then
   chunk excerpts of the retrieved source sessions under a character
   budget — each source is guaranteed its best chunk (by query-term
   overlap) before the leftover budget goes to the next-best chunks
   globally, and the selected excerpts render oldest-session-first.
   When a session's best-matching *line* would be cut by the head-biased
   chunk boundary (late chess moves, item 7 of a numbered list), the
   source's guaranteed excerpt becomes a query-anchored window centered
   on that line instead — same cost cap, so packing stays round-robin.

The answer prompt is the official LongMemEval reading template with the
history slot filled by Alice's context block instead of the full haystack.
The context block renders as prose (default, byte-stable) or as a compact
structured JSON document (``pack_format="json"``; see
:mod:`longmemeval.pack_formats`) — same selected content either way.

Four run-level choices decide what a score measures, and each is recorded in
the run fingerprint and on every checkpoint row (harness 1.1):

* **Session labels** (:mod:`longmemeval.session_labels`). The dataset names
  every evidence session ``answer_...`` and no filler session so, so the raw
  id must never reach the store or the reader. New runs write a keyed-hash
  label everywhere a session id used to go; ``raw`` reproduces old runs.
* **Excerpt source** (``store_chunks`` | ``pack_excerpts``, see below).
* **Promotion** (``all_candidates`` | ``sources_only``). The default
  force-accepts every extracted candidate memory, as every earlier run did.
  ``sources_only`` leaves candidates unpromoted, which is what a real import
  gives a user: sources, not accepted memories.
* **Surface** (``context_pack`` | ``recall``). The default compiles a context
  pack. ``recall`` calls the shipped ``alice_recall`` MCP tool, the question
  answering tool a default install exposes, and hands the reader the text the
  tool returns.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Callable, Iterator
from urllib.parse import quote
from uuid import UUID

from alicebot_api.mcp.types import _RECALL_MAX_LIMIT
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext, call_mcp_tool
from alicebot_api.recall_framing import serialize_mcp_tool_result
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService
# Currency chains: renders the pack's per-memory currency annotation
# ("[SUPERSEDED as of <date>]"/"[CURRENT as of <date>]") on fact lines;
# empty suffix for memories without the annotation (see the marked block
# in _render_context_block).
from alicebot_api.vnext_currency import currency_label_suffix
from alicebot_api.vnext_embeddings import attach_memory_embeddings
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_retrieval import (
    VECTOR_STAGE_ENABLED,
    VNextRetrievalRequest,
    VNextRetrievalService,
    query_terms,
)
from alicebot_api.vnext_fact_keys import attach_memory_fact_keys
from alicebot_api.vnext_rollups import RollupOptions, VNextRollupService
from alicebot_api.vnext_temporal_query import parse_event_datetime

from longmemeval.dataset import LongMemEvalQuestion, SessionTurn
from longmemeval.session_labels import (
    DEFAULT_SESSION_LABEL_MODE,
    SessionLabeler,
    session_labeler_for_question,
    validate_session_label_mode,
)
from longmemeval.pack_formats import (
    DEFAULT_PACK_FORMAT,
    PACK_FORMATS,
    PACK_FORMAT_JSON,
    append_derived_timeline,
    assemble_document,
    compact_json,
    document_envelope,
    excerpt_record,
    memory_record,
)
from alicebot_api.vnext_source_fence import SourceReadFence


LME_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
LME_USER_EMAIL = "longmemeval@alice.local"
SOURCE_TYPE = "chat_session"
SOURCE_DOMAIN = "unknown"  # never domain-filtered by the retrieval policy
SOURCE_SENSITIVITY = "internal"

CONTEXT_CHAR_BUDGET_ENV = "ALICE_LME_CONTEXT_CHAR_BUDGET"
MAX_ITEMS_ENV = "ALICE_LME_MAX_ITEMS"
# Where excerpt text comes from. This decides whether a run measures the
# product or measures the harness.
#
#   "store_chunks"  reads every chunk of every retrieved source straight out of
#                   the store, then runs the harness's own budgeted two-pass
#                   selection over them. Privileged: no MCP tool ever offered
#                   this. Every published Alice benchmark number, 81.2%
#                   included, was produced this way.
#   "pack_excerpts" uses only the excerpt the context pack itself returns, which
#                   is exactly what an agent receives. One windowed excerpt per
#                   source instead of unlimited chunks.
#
# The default stays "store_chunks" so existing numbers remain comparable and are
# not silently restated. Switching to "pack_excerpts" measures a different and
# more honest thing, and the two must never be quoted as the same metric.
EXCERPT_SOURCE_ENV = "ALICE_LME_EXCERPT_SOURCE"
EXCERPT_SOURCE_STORE_CHUNKS = "store_chunks"
EXCERPT_SOURCE_PACK_EXCERPTS = "pack_excerpts"
DEFAULT_EXCERPT_SOURCE = EXCERPT_SOURCE_STORE_CHUNKS
EXCERPT_SOURCES = (EXCERPT_SOURCE_STORE_CHUNKS, EXCERPT_SOURCE_PACK_EXCERPTS)

# What happens to the candidate memories capture extracts. A real import gives a
# user sources, not accepted memories: capture leaves memories as candidates,
# and the retrieval memory stages only see active/accepted rows.
#
#   "all_candidates"  every candidate is force-accepted straight after capture
#                     (``_promote_candidate_memories``). What every published
#                     LongMemEval number did, so it stays the default and old
#                     runs stay reproducible. It models a store whose owner
#                     reviewed and accepted every extracted memory.
#   "sources_only"    nothing is promoted. The store holds sources, chunks and
#                     unreviewed candidates, exactly as after an import, and
#                     retrieval can only return source excerpts.
PROMOTION_MODE_ALL_CANDIDATES = "all_candidates"
PROMOTION_MODE_SOURCES_ONLY = "sources_only"
PROMOTION_MODES = (PROMOTION_MODE_ALL_CANDIDATES, PROMOTION_MODE_SOURCES_ONLY)
DEFAULT_PROMOTION_MODE = PROMOTION_MODE_ALL_CANDIDATES

# Which retrieval call produces the reader's context.
#
#   "context_pack"  ``compile_context_pack`` (the ``alice_context_pack`` tool,
#                   which a default install does not expose), rendered by the
#                   harness. What every published number used.
#   "recall"        the shipped ``alice_recall`` MCP tool, called through the
#                   same ``call_mcp_tool`` entry point the MCP server uses, with
#                   the tool's own default limit and fences. The reader gets
#                   the text the server would hand a model, no harness
#                   rendering, no budget, no reference time (the tool has none).
SURFACE_CONTEXT_PACK = "context_pack"
SURFACE_RECALL = "recall"
SURFACES = (SURFACE_CONTEXT_PACK, SURFACE_RECALL)
DEFAULT_SURFACE = SURFACE_CONTEXT_PACK
# Recorded as the pack format of a recall run: the reader sees the tool result.
RECALL_RESULT_FORMAT = "recall_result"

DEFAULT_CONTEXT_CHAR_BUDGET = 12_000
DEFAULT_MAX_ITEMS = 8
# The recall tool refuses a limit above this. The recall surface passes the run's
# max_items as the limit, so the runner checks it before any store is built.
RECALL_MAX_LIMIT = _RECALL_MAX_LIMIT

EMPTY_CONTEXT_PLACEHOLDER = "(no relevant chat history was retrieved)"

# -- temporal precompute (derived date values) --------------------------------
# Section header + hard char cap for the pack's "[derived]" date-arithmetic
# lines (pack["derived_values"], computed by the retrieval service from the
# selected items' event dates and the request's reference_time). The section
# is rendered UNCHARGED against the excerpt char budget: reserving for it
# would evict excerpt content on nearly every question (measured packs run
# at ~100% of budget), turning a presentation feature into a retrieval
# regression. Growth is therefore additive and hard-bounded: the service
# caps the block at DERIVED_TIMELINE_MAX_LINES lines and this cap bounds the
# rendered section's characters (whole lines only, never truncated mid-line).
DERIVED_SECTION_HEADER = "### Derived date arithmetic (precomputed):"
DERIVED_SECTION_MAX_CHARS = 1_600

# Review-accept reason recorded on every roll-up acceptance the harness
# performs (--accept-rollups); it lands verbatim in the acceptance metadata,
# the promotion revision, and the agent.memory_consolidation_accepted event,
# so accepted-by-the-harness cards are always distinguishable offline.
ROLLUP_ACCEPTANCE_REASON = (
    "LongMemEval harness --accept-rollups: review-accepted consolidation roll-up proposal "
    "(models the product's human review workflow)."
)

# Official LongMemEval reading templates (src/generation/run_generation.py in
# xiaowu0162/LongMemEval), verbatim; the history slot receives Alice's
# retrieved context block instead of the full haystack.
ANSWER_PROMPT_TEMPLATE = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history.\n\n\n"
    "History Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer:"
)
ANSWER_PROMPT_TEMPLATE_COT = (
    "I will give you several history chats between you and a user. "
    "Please answer the question based on the relevant chat history. "
    "Answer the question step by step: first extract all the relevant information, "
    "and then reason over the information to get the answer.\n\n\n"
    "History Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer (step by step):"
)
ANSWER_MAX_TOKENS = 500
ANSWER_MAX_TOKENS_COT = 800

_WORD_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class RollupAcceptanceStats:
    """One post-ingest consolidation roll-up pass + review acceptance.

    Produced only when the disclosed ``--accept-rollups`` step ran, and then
    serialized into the checkpoint row's ``ingest.rollups`` block, so every
    scored run carries per-store visibility of how many cards were proposed
    and accepted (and which memory rows they are).
    """

    groupable_memory_count: int
    proposal_count: int
    accepted_count: int
    accepted_memory_ids: tuple[str, ...]
    skipped: tuple[str, ...]
    rollup_seconds: float

    def to_record(self) -> dict[str, object]:
        return {
            "groupable_memory_count": self.groupable_memory_count,
            "proposal_count": self.proposal_count,
            "accepted_count": self.accepted_count,
            "accepted_memory_ids": list(self.accepted_memory_ids),
            "skipped": list(self.skipped),
            "rollup_seconds": round(self.rollup_seconds, 3),
        }


@dataclass(frozen=True, slots=True)
class IngestStats:
    session_count: int
    source_count: int
    duplicate_count: int
    chunk_count: int
    candidate_memory_count: int
    promoted_memory_count: int
    ingest_seconds: float
    # None whenever the --accept-rollups step did not run, and then the
    # record below is byte-identical to the pre-roll-up schema (no new key),
    # so off-flag checkpoint rows do not drift.
    rollups: RollupAcceptanceStats | None = None

    def to_record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "session_count": self.session_count,
            "source_count": self.source_count,
            "duplicate_count": self.duplicate_count,
            "chunk_count": self.chunk_count,
            "candidate_memory_count": self.candidate_memory_count,
            "promoted_memory_count": self.promoted_memory_count,
            "ingest_seconds": round(self.ingest_seconds, 3),
        }
        if self.rollups is not None:
            record["rollups"] = self.rollups.to_record()
        return record


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    context_block: str
    context_chars: int
    approx_context_tokens: int
    memory_count: int
    source_count: int
    excerpt_count: int
    vector_stage: str
    vector_enabled: bool
    warnings: tuple[str, ...]
    retrieval_seconds: float
    # Pack provenance, persisted in checkpoint rows so any later paired flip
    # is attributable offline: which sessions the source stage retrieved,
    # which memory rows the pack selected, and a digest of the exact
    # rendered context block (ids + hash only -- never the context text).
    # (session labels, not raw ids, unless the run used raw labels)
    source_session_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    context_sha256: str = ""
    # How the context block was rendered ("prose" | "json"). The default
    # keeps off-flag checkpoint rows byte-identical to the pre-JSON schema
    # (no new key), mirroring the IngestStats.rollups precedent; a JSON run
    # always discloses itself here AND in the config fingerprint.
    pack_format: str = DEFAULT_PACK_FORMAT

    def to_record(self) -> dict[str, object]:
        record = {
            "context_chars": self.context_chars,
            "approx_context_tokens": self.approx_context_tokens,
            "memory_count": self.memory_count,
            "source_count": self.source_count,
            "excerpt_count": self.excerpt_count,
            "vector_stage": self.vector_stage,
            "vector_enabled": self.vector_enabled,
            "warnings": list(self.warnings),
            "retrieval_seconds": round(self.retrieval_seconds, 3),
            "provenance": {
                "source_session_ids": list(self.source_session_ids),
                "memory_ids": list(self.memory_ids),
                "context_sha256": self.context_sha256,
            },
        }
        if self.pack_format != DEFAULT_PACK_FORMAT:
            record["pack_format"] = self.pack_format
        return record


def collapse_intra_turn_blank_lines(content: str) -> str:
    """Keep each turn a single chunking paragraph without reflowing lines."""
    return re.sub(r"\n\s*\n+", "\n", content.replace("\r\n", "\n").replace("\r", "\n")).strip()


def render_session_text(session_id: str, date: str, turns: tuple[SessionTurn, ...]) -> str:
    """Speaker-tagged session text; one paragraph per turn for chunking.

    ``session_id`` is whatever label the caller supplies and lands in the first
    paragraph, which sits inside chunk 0 of the stored source. The adapter
    passes the session label (a keyed hash unless the raw-label flag is set),
    never the dataset's raw id; see :mod:`longmemeval.session_labels`.
    """
    paragraphs = [f"Chat session {session_id} on {date}."]
    for turn in turns:
        content = collapse_intra_turn_blank_lines(turn.content)
        if content == "":
            continue
        paragraphs.append(f"[{turn.role.upper()}]: {content}")
    return "\n\n".join(paragraphs)


def build_answer_prompt(*, context_block: str, question: str, question_date: str, cot: bool = False) -> str:
    history = context_block if context_block.strip() else EMPTY_CONTEXT_PLACEHOLDER
    template = ANSWER_PROMPT_TEMPLATE_COT if cot else ANSWER_PROMPT_TEMPLATE
    return template.format(history, question_date, question)


def context_char_budget_from_env() -> int:
    raw = os.environ.get(CONTEXT_CHAR_BUDGET_ENV, "").strip()
    return int(raw) if raw else DEFAULT_CONTEXT_CHAR_BUDGET


def max_items_from_env() -> int:
    raw = os.environ.get(MAX_ITEMS_ENV, "").strip()
    return int(raw) if raw else DEFAULT_MAX_ITEMS


def excerpt_source_from_env() -> str:
    raw = os.environ.get(EXCERPT_SOURCE_ENV, "").strip() or DEFAULT_EXCERPT_SOURCE
    if raw not in EXCERPT_SOURCES:
        # Fail loudly. A typo silently falling back to the privileged reader
        # would report a product-path number that was never measured.
        raise ValueError(
            f"{EXCERPT_SOURCE_ENV}={raw!r} is not one of "
            f"{EXCERPT_SOURCE_STORE_CHUNKS!r} / {EXCERPT_SOURCE_PACK_EXCERPTS!r}"
        )
    return raw


def resolve_excerpt_source(requested: str | None = None, *, surface: str = DEFAULT_SURFACE) -> str:
    """The excerpt source one run uses, decided once for the whole run.

    ``requested`` (the ``--excerpt-source`` flag) wins over the environment
    variable, which wins over the default. The recall surface has exactly one
    coherent value, ``pack_excerpts``: the tool returns its own excerpts and
    nothing in the harness can read chunks around it. It is chosen when nothing
    was requested, and asking for ``store_chunks`` with it fails loudly instead
    of recording a reader that was never used.
    """
    validate_surface(surface)
    explicit = requested if requested is not None else (os.environ.get(EXCERPT_SOURCE_ENV, "").strip() or None)
    if explicit is None:
        return EXCERPT_SOURCE_PACK_EXCERPTS if surface == SURFACE_RECALL else DEFAULT_EXCERPT_SOURCE
    if explicit not in EXCERPT_SOURCES:
        raise ValueError(
            f"excerpt source {explicit!r} is not one of "
            f"{EXCERPT_SOURCE_STORE_CHUNKS!r} / {EXCERPT_SOURCE_PACK_EXCERPTS!r}"
        )
    if surface == SURFACE_RECALL and explicit != EXCERPT_SOURCE_PACK_EXCERPTS:
        raise ValueError(
            f"the recall surface returns the tool's own excerpts; excerpt source {explicit!r} "
            "would read chunks the tool never returns"
        )
    return explicit


def validate_surface(surface: str) -> str:
    if surface not in SURFACES:
        raise ValueError(f"surface {surface!r} is not one of {SURFACES}")
    return surface


def validate_promotion_mode(promotion_mode: str) -> str:
    if promotion_mode not in PROMOTION_MODES:
        raise ValueError(f"promotion mode {promotion_mode!r} is not one of {PROMOTION_MODES}")
    return promotion_mode


def recall_surface_blocker() -> str | None:
    """Why the recall surface cannot run in this environment, or ``None``.

    The surface calls the shipped tool through ``call_mcp_tool``. With
    ``ALICE_AGENT_API_KEY`` set the tool authenticates the caller against the
    store's issued keys, which a throwaway benchmark store does not have, and
    every question would fail. Without it the tool runs as the local operator,
    which is the default-install behaviour being measured.
    """
    if (os.environ.get(AGENT_API_KEY_ENV) or "").strip():
        # The variable is named with a literal on purpose: formatting the
        # imported constant into a message that reaches stderr is read by static
        # analysis as logging a credential, though only the name is printed.
        # A test pins the literal to the constant.
        return (
            "ALICE_AGENT_API_KEY is set; the recall surface runs the tool as the local operator "
            "(the default install) and would try to authenticate against keys the benchmark store lacks"
        )
    return None


def _chunk_overlap_score(chunk_text: str, terms: frozenset[str]) -> int:
    if not terms:
        return 0
    tokens = set(_WORD_PATTERN.findall(chunk_text.casefold()))
    return len(terms & tokens)


def _iso_date(value: object) -> str | None:
    """``YYYY-MM-DD`` prefix of an ISO timestamp string, or None."""
    text = str(value or "")
    return text[:10] if len(text) >= 10 else None


# Connector-style session-date prefix ("2023/05/30 (Tue) 02:01"); the tail
# (day-of-week, clock time) is kept verbatim by _iso_fact_date.
_FACT_DATE_PREFIX_PATTERN = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(.*)$", re.DOTALL)


def _iso_fact_date(raw_date: str) -> str:
    """ISO-8601 normalization of a fact line's session-date prefix.

    ``2023/05/30 (Tue) 02:01`` -> ``2023-05-30 (Tue) 02:01``: the leading
    calendar date becomes machine-readable ISO-8601 (dashes, zero-padded)
    while the day-of-week and clock time are kept byte-for-byte. Strings
    that do not lead with a valid calendar date ("undated") pass through
    unchanged, so nothing is ever invented.
    """
    match = _FACT_DATE_PREFIX_PATTERN.match(raw_date)
    if match is None:
        return raw_date
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        normalized = date(year, month, day)
    except ValueError:
        return raw_date
    return f"{normalized.isoformat()}{match.group(4)}"


def _validity_suffix(memory: dict[str, object]) -> str:
    """Compact suffix rendering a pack item's ``validity`` annotation.

    Factual metadata only, quoted from the retrieval pack (validity window
    dates, supersession state, correction timestamps) -- never instruction
    text, so the official reading templates stay untouched. Memories
    without the annotation render exactly as before (empty suffix).
    """
    validity = memory.get("validity")
    if not isinstance(validity, dict):
        return ""
    parts: list[str] = []
    valid_from = _iso_date(validity.get("valid_from"))
    valid_to = _iso_date(validity.get("valid_to"))
    if valid_from and valid_to:
        parts.append(f"valid {valid_from} → {valid_to}")
    elif valid_to:
        parts.append(f"valid until {valid_to}")
    elif valid_from:
        parts.append(f"valid from {valid_from}")
    corrected_at = _iso_date(validity.get("corrected_at"))
    if validity.get("superseded"):
        parts.append("superseded by a newer entry")
    elif validity.get("supersedes_memory_id"):
        updated_on = corrected_at or _iso_date(memory.get("created_at"))
        parts.append(
            f"updated {updated_on}; supersedes an earlier value"
            if updated_on
            else "supersedes an earlier value"
        )
    elif corrected_at:
        parts.append(f"corrected {corrected_at}")
    if not parts:
        return ""
    return f" [{'; '.join(parts)}]"


def _prose_entry_length(entry: tuple[int, int, int, str, str, str]) -> int:
    """Rendered cost of one prose excerpt entry (header + text + separators)."""
    _neg_score, _source_rank, chunk_index, session_id, date, chunk_text = entry
    header = f"[Session {session_id} | {date} | excerpt {chunk_index + 1}]"
    return len(header) + len(chunk_text) + 3


def _json_excerpt_record(entry: tuple[int, int, int, str, str, str]) -> dict[str, object]:
    """One ``session_excerpts[]`` record from a selection entry."""
    _neg_score, _source_rank, chunk_index, session_id, date, chunk_text = entry
    return excerpt_record(session_id=session_id, date=date, excerpt_index=chunk_index + 1, excerpt=chunk_text)


def _json_entry_cost(entry: tuple[int, int, int, str, str, str]) -> int:
    """Serialized cost of one JSON excerpt entry (record + one separator comma)."""
    return len(compact_json(_json_excerpt_record(entry))) + 1


def _derived_pack_lines(pack: dict[str, object]) -> list[str]:
    """Bounded "[derived]" date-arithmetic lines quoted from the pack.

    Shared by both pack formats so they carry identical derived content:
    prose renders them under ``DERIVED_SECTION_HEADER``, JSON as the
    top-level ``derived_timeline`` array. Whole lines only, capped at
    ``DERIVED_SECTION_MAX_CHARS`` (see the constant's rationale above);
    packs without ``derived_values`` (no reference_time, no dated items)
    yield ``[]`` and render byte-identically to the pre-feature output.
    """
    derived = pack.get("derived_values") if isinstance(pack.get("derived_values"), dict) else None
    if derived is None:
        return []
    raw_lines = derived.get("lines")
    derived_lines: list[str] = []
    derived_chars = 0
    for raw_line in raw_lines if isinstance(raw_lines, list) else []:
        line = str(raw_line)
        if derived_chars + len(line) + 1 > DERIVED_SECTION_MAX_CHARS:
            break
        derived_lines.append(line)
        derived_chars += len(line) + 1
    return derived_lines


# -- query-anchored excerpt windows ------------------------------------------
#
# The head-biased failure mode: a source's best chunk (whole-chunk term
# overlap) contains the query's best-matching line near its tail, and the
# answer sits just past the chunk boundary ("28. Kg3" after "27. Kg2 Bd5+"),
# or an enumerated list is cut mid-run before the asked-about item. All
# triggers below are query-surface (query-term matches) or content-shape
# (line-initial/inline numbering density) — no benchmark labels.

_ANCHOR_MIN_SCORE = 3  # weighted line score required before anchoring may replace the head path
_ANCHOR_EXTENSION_MAX_CHARS = 1_600  # cap on the keep-the-enumeration-intact continuation
_ENUMERATED_LINE_HEAD_PATTERN = re.compile(r"^\s*\d{1,3}[.)]\s")
_INLINE_ENUMERATION_PATTERN = re.compile(r"(?:^|\s)\d{1,3}\.(?=\s|$)")


def _line_anchor_score(line: str, terms: frozenset[str]) -> int:
    """Weighted query-term overlap for one line; distinctive terms count double.

    Terms carrying a digit (move numbers, list ordinals, notation like
    ``kg2``) or five-plus characters are worth 2, short stopwordy terms 1,
    so "27. Kg2 Bd5+" outranks a chatty line matching "the ... move".
    """
    if not terms:
        return 0
    matched = terms & set(_WORD_PATTERN.findall(line.casefold()))
    return sum(2 if len(term) >= 5 or any(ch.isdigit() for ch in term) else 1 for term in matched)


def _has_enumeration_signal(line: str) -> bool:
    """Content-shape check: line-initial numbering or dense inline numbering.

    Catches numbered list items ("7. Transcriptionist") and wrapped move
    records ("bxc3 exd4 9. cxd4 Bb4 10. Rb1 a5 ...") without reading any
    benchmark metadata.
    """
    if _ENUMERATED_LINE_HEAD_PATTERN.match(line):
        return True
    return len(_INLINE_ENUMERATION_PATTERN.findall(line)) >= 2


def _trim_line_around_matches(line: str, terms: frozenset[str], *, max_chars: int) -> str:
    """Char window of an overlong line centered on its query-term matches."""
    spans = [match.span() for match in _WORD_PATTERN.finditer(line.casefold()) if match.group(0) in terms]
    if not spans:
        return line[:max_chars]
    center = (spans[0][0] + spans[-1][1]) // 2
    start = max(0, min(center - max_chars // 2, len(line) - max_chars))
    return line[start : start + max_chars]


@dataclass(frozen=True, slots=True)
class _AnchoredExcerpt:
    chunk_index: int  # chunk containing the anchor line (used for the excerpt header)
    text: str  # window text; costs no more than the baseline chunk it replaces
    extension_text: str  # downward enumeration continuation ("" if none); spent from the second-pass pool
    upward_extension_text: str  # upward enumeration continuation ("" if none); same pool
    covered_chunk_indexes: frozenset[int]  # chunks the window overlaps (kept out of pass 2)
    extension_chunk_indexes: frozenset[int]  # chunks the downward extension overlaps (excluded only if applied)
    upward_extension_chunk_indexes: frozenset[int]  # chunks the upward extension overlaps (excluded only if applied)


def _query_anchored_excerpt(
    ordered_chunks: list[tuple[int, str]],
    terms: frozenset[str],
    *,
    baseline_chunk_index: int,
    baseline_text: str,
) -> _AnchoredExcerpt | None:
    """Query-anchored window replacing a source's head-biased best chunk.

    Returns ``None`` whenever anchoring is not clearly warranted, in which
    case the caller keeps today's byte-identical best-chunk excerpt:
    single-chunk sources (the whole session is already visible), no line
    with a weighted score of at least ``_ANCHOR_MIN_SCORE``, or no
    enumeration shape near the matched line. Ordinary prose never anchors —
    not even when the best-matching line sits outside the baseline chunk —
    because a prose anchor move displaces the head-biased best chunk that
    usually carries the surrounding answer context; only enumerated runs
    (move records, numbered lists) have the cut-mid-run failure mode this
    window exists to fix.

    The window is centered on the best-matching line over the session's
    concatenated chunk stream (ties break to the earliest line), expands to
    whichever side is lighter (ties extend downward, where answers to
    "after X" questions live), and never exceeds the baseline entry's cost,
    so pass-1 packing admits exactly the same sources as before. When the
    window's lower edge cuts an enumerated run, the continuation through
    the run (capped) is returned separately for the second-pass pool; when
    the window's upper edge cuts an enumerated run, the lines above it are
    returned the same way (list items 1..k-1 when the window starts at k).
    """
    if len(ordered_chunks) < 2 or not terms:
        return None
    lines: list[tuple[int, str]] = []
    for position, (chunk_index, text) in enumerate(ordered_chunks):
        if position:
            lines.append((chunk_index, ""))  # paragraph seam between adjacent chunks
        for line in text.split("\n"):
            lines.append((chunk_index, line))
    best_index = -1
    best_score = 0
    for index, (_chunk_index, line) in enumerate(lines):
        score = _line_anchor_score(line, terms)
        if score > best_score:
            best_index, best_score = index, score
    if best_score < _ANCHOR_MIN_SCORE:
        return None
    anchor_chunk = lines[best_index][0]
    enumerated_shape = any(
        _has_enumeration_signal(lines[index][1])
        for index in range(max(0, best_index - 3), min(len(lines), best_index + 4))
    )
    if not enumerated_shape:
        return None
    # Cost parity with the baseline entry: same header except the excerpt
    # ordinal, so cap the window text to keep entry_length from growing.
    cap = len(baseline_text) + len(str(baseline_chunk_index + 1)) - len(str(anchor_chunk + 1))
    if cap <= 0:
        return None
    anchor_line = lines[best_index][1]
    low = high = best_index
    if len(anchor_line) > cap:
        window_text = _trim_line_around_matches(anchor_line, terms, max_chars=cap)
    else:
        length = len(anchor_line)
        above = below = 0
        while True:
            up_cost = len(lines[low - 1][1]) + 1 if low > 0 else None
            down_cost = len(lines[high + 1][1]) + 1 if high + 1 < len(lines) else None
            can_up = up_cost is not None and length + up_cost <= cap
            can_down = down_cost is not None and length + down_cost <= cap
            if can_down and (not can_up or below <= above):
                high += 1
                below += down_cost or 0
                length += down_cost or 0
            elif can_up:
                low -= 1
                above += up_cost or 0
                length += up_cost or 0
            else:
                break
        while low < best_index and lines[low][1] == "":
            low += 1
        while high > best_index and lines[high][1] == "":
            high -= 1
        window_text = "\n".join(line for _chunk_index, line in lines[low : high + 1])
    extension_lines: list[tuple[int, str]] = []
    if _has_enumeration_signal(lines[high][1]):
        extension_length = 0
        for chunk_index, line in lines[high + 1 :]:
            if not _has_enumeration_signal(line):
                break
            if extension_length + len(line) + 1 > _ANCHOR_EXTENSION_MAX_CHARS:
                break
            extension_lines.append((chunk_index, line))
            extension_length += len(line) + 1
    extension_text = "\n".join(line for _chunk_index, line in extension_lines)
    # Symmetric upward continuation: the window's TOP edge cutting an
    # enumerated run loses items 1..k-1 ("3. Tomato ..." without steps 1-2),
    # so walk upward while the shape holds, under the same cap, also spent
    # from the second-pass pool (never displacing pass-1 guarantees).
    upward_lines: list[tuple[int, str]] = []
    if low > 0 and _has_enumeration_signal(lines[low][1]):
        upward_length = 0
        for chunk_index, line in lines[low - 1 :: -1]:
            if not _has_enumeration_signal(line):
                break
            if upward_length + len(line) + 1 > _ANCHOR_EXTENSION_MAX_CHARS:
                break
            upward_lines.append((chunk_index, line))
            upward_length += len(line) + 1
    upward_lines.reverse()
    upward_extension_text = "\n".join(line for _chunk_index, line in upward_lines)
    if window_text == baseline_text and extension_text == "" and upward_extension_text == "":
        return None  # anchoring would change nothing; keep the old path
    return _AnchoredExcerpt(
        chunk_index=anchor_chunk,
        text=window_text,
        extension_text=extension_text,
        upward_extension_text=upward_extension_text,
        covered_chunk_indexes=frozenset(chunk_index for chunk_index, _line in lines[low : high + 1]),
        extension_chunk_indexes=frozenset(chunk_index for chunk_index, _line in extension_lines),
        upward_extension_chunk_indexes=frozenset(chunk_index for chunk_index, _line in upward_lines),
    )


class QuestionRun:
    """One LongMemEval question against one isolated Alice store."""

    def __init__(
        self,
        question: LongMemEvalQuestion,
        store: SQLiteVNextStore,
        *,
        excerpt_source: str | None = None,
        session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
        promotion_mode: str = DEFAULT_PROMOTION_MODE,
        surface: str = DEFAULT_SURFACE,
        db_path: str | Path | None = None,
    ) -> None:
        self.question = question
        self.store = store
        self.surface = validate_surface(surface)
        self.promotion_mode = validate_promotion_mode(promotion_mode)
        # Resolved once per run, not per call, so a single run cannot mix the
        # two readers and report a number that describes neither.
        self.excerpt_source = (
            excerpt_source
            if excerpt_source is not None
            else resolve_excerpt_source(surface=self.surface)
        )
        if self.excerpt_source not in EXCERPT_SOURCES:
            raise ValueError(f"excerpt source {self.excerpt_source!r} is not one of {EXCERPT_SOURCES}")
        if self.surface == SURFACE_RECALL and self.excerpt_source != EXCERPT_SOURCE_PACK_EXCERPTS:
            raise ValueError("the recall surface returns the tool's own excerpts; use excerpt source pack_excerpts")
        # The raw ids live here and nowhere else in memory; every write of a
        # session id into the store and every render of one for the reader goes
        # through this labeler (see session_labels.py). Built eagerly so a
        # label collision fails before anything is ingested.
        self.session_label_mode = validate_session_label_mode(session_label_mode)
        self._labels: SessionLabeler = session_labeler_for_question(question, mode=session_label_mode)
        # File the per-question store lives in; only the recall surface needs
        # it, because the shipped tool opens its own connection to the store.
        self.db_path = Path(db_path) if db_path is not None else None
        self._source_sessions: dict[str, tuple[str, str]] = {}  # source_id -> (session label, date)

    @property
    def session_labeler(self) -> SessionLabeler:
        """The label mapping, for the sidecar file and offline tools only."""
        return self._labels

    # -- ingest ------------------------------------------------------------

    def ingest(self, *, accept_rollups: bool = False, reuse_store: bool = False) -> IngestStats:
        if accept_rollups and self.promotion_mode == PROMOTION_MODE_SOURCES_ONLY:
            # Roll-ups group accepted memories; with none promoted the step would
            # run on nothing and still be recorded as a roll-up run.
            raise ValueError("accept_rollups needs promoted memories and cannot be combined with sources_only")
        started = time.monotonic()
        if reuse_store:
            # Marker-verified reuse (runner --reuse-stores): the sessions are
            # already captured and promoted in this store, so session capture
            # is skipped entirely. Promotion is a no-op on a promoted store
            # and roll-up acceptance is idempotent, so both still run below —
            # keeping the --accept-rollups fingerprint truthful on reuse.
            rollup_stats = (
                self._consolidate_and_accept_rollups()
                if accept_rollups
                else None
            )
            return IngestStats(
                session_count=len(self.question.haystack_session_ids),
                source_count=0,
                duplicate_count=0,
                chunk_count=0,
                candidate_memory_count=0,
                promoted_memory_count=0,
                ingest_seconds=time.monotonic() - started,
                rollups=rollup_stats,
            )
        capture = VNextCaptureService(self.store, actor_type="system")
        source_count = 0
        duplicate_count = 0
        chunk_count = 0
        candidate_count = 0
        session_count = 0
        for session_id, date, turns in self.question.sessions_with_metadata():
            session_count += 1
            # The raw dataset id stops here. Each of the four store entry
            # points below takes the label; the leak-guard test mutates them
            # one at a time (see test_session_label_leak_guard.py).
            label = self._labels.label_for(session_id)
            text = render_session_text(label, date, turns)
            result = capture.capture_source(
                SourceCaptureInput(
                    source_type=SOURCE_TYPE,
                    title=f"Chat session {label} on {date}",
                    raw_text=text,
                    connector_name="longmemeval",
                    external_id=f"{self.question.question_id}/{label}",
                    domain=SOURCE_DOMAIN,
                    sensitivity=SOURCE_SENSITIVITY,
                    metadata_json={
                        "benchmark": "longmemeval",
                        "question_id": self.question.question_id,
                        "session_id": label,
                        "session_date": date,
                    },
                )
            )
            if result.duplicate:
                duplicate_count += 1
            else:
                source_count += 1
                chunk_count += result.chunk_count
                candidate_count += result.candidate_memory_count
            if result.source_id is not None and result.source_id not in self._source_sessions:
                self._source_sessions[result.source_id] = (label, date)
        if self.promotion_mode == PROMOTION_MODE_SOURCES_ONLY:
            # Nothing is accepted: the store keeps what an import leaves behind.
            promoted = 0
        else:
            promoted = self._promote_candidate_memories(stamp_session_dates=accept_rollups)
        rollups = self._consolidate_and_accept_rollups() if accept_rollups else None
        return IngestStats(
            session_count=session_count,
            source_count=source_count,
            duplicate_count=duplicate_count,
            chunk_count=chunk_count,
            candidate_memory_count=candidate_count,
            promoted_memory_count=promoted,
            ingest_seconds=time.monotonic() - started,
            rollups=rollups,
        )

    def _session_date_for_memory(self, memory: dict[str, object]) -> str | None:
        metadata = memory.get("metadata_json")
        source_id = str(metadata.get("source_id") or "") if isinstance(metadata, dict) else ""
        if not source_id:
            return None
        session_id, date = self._session_label(source_id)
        return date if session_id and date != "undated" else None

    def _promote_candidate_memories(self, *, stamp_session_dates: bool = False) -> int:
        """Accept capture candidates so Alice's search stages can see them.

        Mirrors the store-level review-accept patch (``status: active``) used
        by the product's review flow; without it, ``search_memories*`` only
        matches pre-existing active/accepted rows and the memory stages would
        run empty for every question.

        ``stamp_session_dates`` (only set by the disclosed --accept-rollups
        step) additionally records each memory's originating session date in
        ``metadata_json.session_date`` — the per-instance date hook the
        roll-up pass documents (``vnext_rollups._member_date``). In the
        product, memories are created in real time so their ``created_at``
        IS the content date; benchmark replay compresses years of sessions
        into one ingest moment, so without the stamp every roll-up instance
        would carry today's date instead of its session's. The stamp is
        provenance the ingest already wrote on the source row's metadata;
        no benchmark label is involved. Off flag, the promotion patch is
        byte-identical to the pre-roll-up harness (``{"status": "active"}``).

        Only called under the ``all_candidates`` promotion mode (the default,
        which reproduces every published run). Under ``sources_only`` it is not
        called: a real import leaves candidates unpromoted, so the memory
        stages see nothing and only source excerpts can be retrieved.
        """
        promoted = 0
        promoted_rows: list[dict[str, object]] = []
        for memory in self.store.list_memories(status="candidate"):
            patch: dict[str, object] = {"status": "active"}
            if stamp_session_dates:
                session_date = self._session_date_for_memory(memory)
                metadata = memory.get("metadata_json")
                if session_date is not None and isinstance(metadata, dict):
                    patch["metadata_json"] = {**metadata, "session_date": session_date}
            updated = self.store.update_memory(
                memory_id=str(memory["id"]),
                patch=patch,
                actor_type="system",
            )
            # Promotion is also the derived-retrieval-key moment (mirrors
            # the product review-accept path); deterministic tier only so
            # keyless ingest never makes a model call.
            attach_memory_fact_keys(self.store, updated, use_env_provider=False)
            promoted_rows.append(updated)
            promoted += 1
        # And the embedding moment: capture no longer embeds a candidate, so the
        # vectors the memory stages search are made here, for the now active
        # rows, in batches as capture made them. A no-op when no embedding
        # provider is configured.
        attach_memory_embeddings(self.store, promoted_rows, actor_type="system")
        return promoted

    def _consolidate_and_accept_rollups(self) -> RollupAcceptanceStats:
        """Run the consolidation roll-up pass, then review-accept every card.

        This models the product's intended human workflow (the user accepts
        the review proposals the consolidation run produced), the same way
        the harness already mirrors review-accept promotion for candidate
        memories in ``_promote_candidate_memories``. Two real product code
        paths, no store-level shortcuts:

        * Proposal — the roll-up portion of the scheduled consolidation
          workflow. ``VNextConsolidationService.generate_memory_consolidation``
          invokes ``VNextRollupService.propose_rollups`` (see
          apps/api/src/alicebot_api/vnext_consolidation.py); the harness
          calls that same service entry point with the scheduled workflow's
          argument shape: deterministic generation (no model call), default
          ``RollupOptions``, default sensitivity scope, candidate creation
          on. The full ``generate_memory_consolidation`` wrapper cannot run
          against the per-question SQLite on-ramp store because it also
          writes the consolidation report artifact (``create_artifact``), a
          surface ``SQLiteVNextStore`` does not implement; the roll-up pass
          itself is store-complete. The near-duplicate exclusion the
          wrapper derives from embedding-based clustering is empty here for
          the same reason keyless replay is FTS-only: ``_cluster_memories``
          skips with ``no_embedding_provider_configured``, so the scheduled
          pass would also pass no clusters; the roll-up pass's own pairwise
          Jaccard duplicate-group guard still applies.
        * Acceptance — every proposed card goes through
          ``VNextMemoryCommitService.accept_consolidation_candidate``, the
          exact service call the review console's
          ``POST /v0/vnext/memories/accept-consolidation`` endpoint
          (apps/api/src/alicebot_api/main.py), the ``alicebot vnext
          memories accept-consolidation`` CLI (cli.py), and the MCP review
          tool (mcp_tools.py) all delegate to, with ``identity=None`` — the
          human-reviewer shape. Acceptance therefore does everything the
          product does: policy events, promotion to ``active``,
          best-effort embedding, entity linking, the ``promoted`` revision,
          and the ``agent.memory_consolidation_accepted`` event.

        Members are never superseded (first-time roll-up proposals carry an
        empty ``proposed_supersede``), so every individual memory stays
        active and recallable exactly as before; the cards are additive.
        """
        started = time.monotonic()
        outcome = VNextRollupService(self.store).propose_rollups(
            domains=None,
            sensitivity_allowed=["public", "internal", "private", "unknown"],
            options=RollupOptions(),
            create_candidate_memories=True,
            generated_by="system",
            trace_id=None,
            generation_mode="deterministic",
            exclude_member_id_sets=[],
        )
        commit = VNextMemoryCommitService(self.store)  # duck-typed store, like the CLI on-ramp
        accepted_ids: list[str] = []
        for candidate_id in outcome.candidate_ids:
            payload = commit.accept_consolidation_candidate(
                candidate_id,
                reason=ROLLUP_ACCEPTANCE_REASON,
                identity=None,
            )
            if payload.get("status") == "accepted":
                accepted_ids.append(candidate_id)
        return RollupAcceptanceStats(
            groupable_memory_count=outcome.groupable_count,
            proposal_count=len(outcome.proposals),
            accepted_count=len(accepted_ids),
            accepted_memory_ids=tuple(accepted_ids),
            skipped=tuple(str(reason) for reason in outcome.skipped),
            rollup_seconds=time.monotonic() - started,
        )

    # -- retrieval ----------------------------------------------------------

    def retrieve(
        self,
        *,
        max_items: int | None = None,
        context_char_budget: int | None = None,
        pack_format: str = DEFAULT_PACK_FORMAT,
    ) -> RetrievalOutcome:
        if pack_format not in PACK_FORMATS:
            raise ValueError(f"pack_format must be one of {PACK_FORMATS}, got {pack_format!r}")
        resolved_max_items = max_items if max_items is not None else max_items_from_env()
        if self.surface == SURFACE_RECALL:
            if pack_format != DEFAULT_PACK_FORMAT:
                raise ValueError("pack_format applies to the context pack surface; recall hands the reader the tool result")
            return self._retrieve_via_recall(max_items=resolved_max_items)
        budget = context_char_budget if context_char_budget is not None else context_char_budget_from_env()
        service = VNextRetrievalService(self.store)
        request = VNextRetrievalRequest(
            query=self.question.question,
            max_items=resolved_max_items,
            include_sources=True,
            actor_type="system",
            # The question's own date is official benchmark input (the
            # reading template already presents it to the answering model);
            # passing it as the anchor clock lets relative phrases like
            # "two weeks ago" resolve against the conversation's timeline
            # instead of the wall clock.
            reference_time=parse_event_datetime(self.question.question_date),
        )
        started = time.monotonic()
        pack = service.compile_context_pack(request, source_fence=SourceReadFence.unfenced())
        retrieval_seconds = time.monotonic() - started
        if pack_format == PACK_FORMAT_JSON:
            context_block, excerpt_count = self._render_context_json(pack, budget=budget)
        else:
            context_block, excerpt_count = self._render_context_block(pack, budget=budget)
        memories = pack.get("relevant_memories") or []
        sources = pack.get("sources") or []
        trace = pack.get("trace") if isinstance(pack.get("trace"), dict) else {}
        vector_stage = str(trace.get("vector_stage", "unknown"))
        warnings = pack.get("warnings") or []
        source_session_ids = tuple(
            self._session_label(str(source.get("id")))[0]
            for source in sources
            if isinstance(source, dict) and source.get("id")
        )
        memory_ids = tuple(
            str(memory.get("id"))
            for memory in memories
            if isinstance(memory, dict) and memory.get("id")
        )
        return RetrievalOutcome(
            context_block=context_block,
            context_chars=len(context_block),
            approx_context_tokens=len(context_block) // 4,
            memory_count=len(memories),
            source_count=len(sources),
            excerpt_count=excerpt_count,
            vector_stage=vector_stage,
            vector_enabled=vector_stage == VECTOR_STAGE_ENABLED,
            warnings=tuple(str(warning) for warning in warnings),
            retrieval_seconds=retrieval_seconds,
            source_session_ids=source_session_ids,
            memory_ids=memory_ids,
            context_sha256=hashlib.sha256(context_block.encode("utf-8")).hexdigest(),
            pack_format=pack_format,
        )

    def _retrieve_via_recall(self, *, max_items: int) -> RetrievalOutcome:
        """Retrieve through the shipped ``alice_recall`` tool, as a default install does.

        The call goes through ``call_mcp_tool``, the entry point the MCP server
        uses: argument validation, the policy preflight, then the handler's own
        sequence (memory FTS, vector and graph stages fused, source excerpt
        search under the same fence, one provenance hop, the current-version
        preference). The harness adds nothing to that sequence and takes
        nothing from it:

        * ``limit`` is the run's ``max_items``. Its default, 8, is the tool's
          default (a test pins that they stay equal); an operator who sets
          ``--max-items`` is choosing a non-default call, and the fingerprint
          records it.
        * No ``reference_time``, no coverage gate, no instance diversity pass:
          the tool has none, and a default agent gets none.
        * No harness budget. The reader is handed
          ``serialize_mcp_tool_result(result)``, the exact text the MCP server
          puts in the model's tool message.
        * ``debug`` is switched on only to read ``vector_stage`` for the run's
          statistics. The tool adds exactly one key for it (``retrieval``),
          which is removed again before the text is built, so the reader sees
          what a call without ``debug`` returns (a test compares the two).
        """
        if self.db_path is None:
            raise ValueError("the recall surface needs the store's file path; open the run with question_run()")
        blocker = recall_surface_blocker()
        if blocker is not None:
            raise ValueError(blocker)
        # The tool opens its own connection to the store file, so what this run
        # ingested has to be committed before the call. Nothing writes after it.
        self.store.conn.commit()
        context = MCPRuntimeContext(
            database_url="sqlite://" + quote(str(self.db_path.resolve())),
            user_id=LME_USER_ID,
        )
        started = time.monotonic()
        payload = call_mcp_tool(
            context,
            name="alice_recall",
            arguments={"query": self.question.question, "limit": max_items, "debug": True},
        )
        retrieval_seconds = time.monotonic() - started
        retrieval = payload.pop("retrieval", None)
        vector_stage = str(retrieval.get("vector_stage", "unknown")) if isinstance(retrieval, dict) else "unknown"
        results = payload.get("results") or []
        sources = payload.get("sources") or []
        context_block = serialize_mcp_tool_result(payload)
        return RetrievalOutcome(
            context_block=context_block,
            context_chars=len(context_block),
            approx_context_tokens=len(context_block) // 4,
            memory_count=len(results),
            source_count=len(sources),
            excerpt_count=sum(
                1 for source in sources if isinstance(source, dict) and str(source.get("excerpt") or "").strip()
            ),
            vector_stage=vector_stage,
            vector_enabled=vector_stage == VECTOR_STAGE_ENABLED,
            warnings=(),
            retrieval_seconds=retrieval_seconds,
            source_session_ids=tuple(
                self._session_label(str(source["id"]))[0]
                for source in sources
                if isinstance(source, dict) and source.get("id")
            ),
            memory_ids=tuple(
                str(result["id"]) for result in results if isinstance(result, dict) and result.get("id")
            ),
            context_sha256=hashlib.sha256(context_block.encode("utf-8")).hexdigest(),
            pack_format=RECALL_RESULT_FORMAT,
        )

    def _session_label(self, source_id: str) -> tuple[str, str]:
        """``(session label, session date)`` for a stored source.

        The label is what the store holds in ``metadata_json.session_id``: a
        keyed-hash label for a run that ingested anonymised, the dataset id for
        a raw-label run. Everything the reader sees about a session (the
        prose excerpt header, the JSON excerpt record) and everything a
        checkpoint row records (``source_session_ids``) comes through here.
        Under anonymised labels a value that is not a label of this question
        raises instead of being shown (see ``SessionLabeler.require_reader_safe``).
        """
        if source_id in self._source_sessions:
            return self._source_sessions[source_id]
        source = self.store.get_source(source_id)
        if source is not None and isinstance(source.get("metadata_json"), dict):
            metadata = source["metadata_json"]
            session_id = self._labels.require_reader_safe(str(metadata.get("session_id") or source_id))
            date = str(metadata.get("session_date") or "undated")
            self._source_sessions[source_id] = (session_id, date)
            return session_id, date
        return source_id, "undated"

    def _render_context_block(self, pack: dict[str, object], *, budget: int) -> tuple[str, int]:
        """Compact prompt block: memory facts, session excerpts, grounding notes.

        Fact lines append the pack's per-memory ``validity`` annotation when
        present (see ``_validity_suffix``): validity windows, supersession
        state, and correction dates, rendered as bracketed factual metadata.

        Excerpt packing is two-pass so one wordy session cannot crowd out
        the rest: pass 1 guarantees every retrieved source its single best
        excerpt (in source rank order), pass 2 spends the remaining budget
        on the next-best chunks globally by lexical-overlap score. A
        source's guaranteed excerpt is its best chunk, unless a query
        anchor fires (see ``_query_anchored_excerpt``): then it is a window
        centered on the best-matching line at the same cost, and any
        enumeration continuation is charged to the pass-2 pool. The
        selected excerpts render in session-timestamp order (oldest
        first), each prefixed with its session id and date.
        """
        lines: list[str] = []
        used = 0

        memories = pack.get("relevant_memories") or []
        fact_lines: list[str] = []
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            text = str(memory.get("canonical_text") or memory.get("summary") or memory.get("title") or "").strip()
            if text == "":
                continue
            metadata = memory.get("metadata_json") if isinstance(memory.get("metadata_json"), dict) else {}
            source_id = str(metadata.get("source_id") or "")
            _session_id, session_date = self._session_label(source_id) if source_id else ("", "undated")
            # Fact-line date prefix: ISO-8601 date, day-of-week kept
            # (machine-readable time; see _iso_fact_date).
            # ---- currency chains begin: chain members carry a factual
            # "[SUPERSEDED as of <date>]"/"[CURRENT as of <date>]" suffix
            # quoted from the pack annotation; the pack already renders a
            # chain as one contiguous oldest-first block with the CURRENT
            # entry last. Non-members get "" — lines byte-identical. ------
            fact_lines.append(
                f"- [{_iso_fact_date(session_date)}] {text}{_validity_suffix(memory)}{currency_label_suffix(memory)}"
            )
            # ---- currency chains end --------------------------------------
        if fact_lines:
            lines.append("### Facts Alice remembers (with session dates):")
            lines.extend(fact_lines)
            used = sum(len(line) + 1 for line in lines)

        # Grounding note (pack-level retrieval statistic from
        # vnext_grounding): factual line(s) about salient query entities
        # with zero corpus support. Rendered with the other
        # retrieval-derived content below; the cost is reserved up front
        # so the note always fits inside the budget.
        grounding = pack.get("grounding") if isinstance(pack.get("grounding"), dict) else None
        grounding_lines: list[str] = []
        if grounding:
            for name in grounding.get("unsupported_entities") or []:
                grounding_lines.append(f'Note: no stored memories mention "{name}".')
        if grounding_lines:
            used += sum(len(line) + 1 for line in grounding_lines) + 1

        selected = self._select_excerpts(pack, budget=budget, used=used, entry_cost=_prose_entry_length)
        excerpt_count = len(selected)
        excerpt_lines: list[str] = []
        for _neg_score, _source_rank, chunk_index, session_id, date, chunk_text in selected:
            excerpt_lines.append(f"[Session {session_id} | {date} | excerpt {chunk_index + 1}]")
            excerpt_lines.append(chunk_text)
            excerpt_lines.append("")
        if excerpt_lines:
            if lines:
                lines.append("")
            lines.append("### Retrieved chat history excerpts:")
            lines.extend(excerpt_lines)

        # ---- temporal precompute (derived date values) begin ----------------
        # Factual "[derived]" lines quoted from the retrieval pack — the
        # date arithmetic the service precomputed from the selected items'
        # timestamps (deltas to the reference date, chronological ordinals,
        # span). Content, not instructions: the official reading templates
        # stay byte-frozen. Rendered after the excerpts they summarize;
        # additive and hard-bounded (see DERIVED_SECTION_MAX_CHARS above);
        # absent packs (no reference_time, no dated items) render
        # byte-identically to the pre-feature block.
        derived_lines = _derived_pack_lines(pack)
        if derived_lines:
            if lines:
                lines.append("")
            lines.append(DERIVED_SECTION_HEADER)
            lines.extend(derived_lines)
        # ---- temporal precompute (derived date values) end ------------------

        if grounding_lines:
            # Retrieval statistic, not an instruction: it belongs with the
            # retrieval output, after the excerpts it summarizes.
            if lines:
                lines.append("")
            lines.extend(grounding_lines)

        return "\n".join(lines).strip(), excerpt_count

    def _source_chunks_for(
        self, source: dict[str, object], *, source_id: str
    ) -> list[dict[str, object]]:
        """The chunks this run is allowed to read for one retrieved source.

        The single place the harness's privilege is exercised, so it can be
        removed by configuration rather than by editing selection logic.

        Under ``pack_excerpts`` the harness sees exactly what an agent sees: the
        one windowed excerpt the pack returned. Under ``store_chunks`` it reads
        the whole document out of the store, which no MCP tool exposes. Keeping
        both behind one call means a run cannot half-switch.
        """

        if self.excerpt_source == EXCERPT_SOURCE_PACK_EXCERPTS:
            excerpt = source.get("excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip():
                return []
            return [{"text": excerpt, "chunk_index": 0}]
        return list(self.store.list_source_chunks(source_id))

    def _select_excerpts(
        self,
        pack: dict[str, object],
        *,
        budget: int,
        used: int,
        entry_cost: Callable[[tuple[int, int, int, str, str, str]], int],
    ) -> list[tuple[int, int, int, str, str, str]]:
        """Shared two-pass excerpt selection under a rendered-length budget.

        Format-agnostic: ``entry_cost`` prices one excerpt entry in the
        caller's serialization (prose header+text or compact-JSON record),
        and ``used`` is the budget the caller already spent on its
        memories/notes/envelope. Everything else — per-source pass-1
        guarantees, query anchoring, enumeration extensions charged as a
        cost DELTA of the grown entry, global pass-2 by score, and the
        oldest-session-first final order — is identical across formats, so
        format stays the only variable between prose and JSON packs.
        """
        terms = frozenset(query_terms(self.question.question))
        # One entry per chunk: (-score, source_rank, chunk_index, session_id, date, text).
        chunks_by_source: list[list[tuple[int, int, int, str, str, str]]] = []
        ordered_chunks_by_source: list[list[tuple[int, str]]] = []  # (chunk_index, text) in session order
        sources = pack.get("sources") or []
        for source_rank, source in enumerate(sources):
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("id"))
            session_id, date = self._session_label(source_id)
            source_chunks: list[tuple[int, int, int, str, str, str]] = []
            ordered_chunks: list[tuple[int, str]] = []
            for chunk in self._source_chunks_for(source, source_id=source_id):
                chunk_text = str(chunk.get("text") or "")
                if chunk_text.strip() == "":
                    continue
                score = _chunk_overlap_score(chunk_text, terms)
                chunk_index = int(chunk.get("chunk_index") or 0)
                source_chunks.append((-score, source_rank, chunk_index, session_id, date, chunk_text))
                ordered_chunks.append((chunk_index, chunk_text))
            source_chunks.sort(key=lambda item: (item[0], item[2]))  # best score first, then position
            ordered_chunks.sort(key=lambda item: item[0])
            if source_chunks:
                chunks_by_source.append(source_chunks)
                ordered_chunks_by_source.append(ordered_chunks)

        # Pass 1: every retrieved source gets its single best excerpt (source
        # rank order), so one wordy session cannot crowd out the rest. A
        # query-anchored window is capped at the baseline chunk's cost, so
        # admission decisions are identical either way; its enumeration
        # extension waits for the leftover pool below.
        selected: list[tuple[int, int, int, str, str, str]] = []
        # Per source: (chunk entries not yet shown, chunk indexes covered by an anchored window).
        leftovers: list[tuple[list[tuple[int, int, int, str, str, str]], frozenset[int]]] = []
        # (selected position, leftovers position, extension text, extension chunk indexes, joins above window).
        pending_extensions: list[tuple[int, int, str, frozenset[int], bool]] = []
        for source_chunks, ordered_chunks in zip(chunks_by_source, ordered_chunks_by_source):
            best = source_chunks[0]
            anchored = _query_anchored_excerpt(
                ordered_chunks, terms, baseline_chunk_index=best[2], baseline_text=best[5]
            )
            entry = best
            if anchored is not None:
                entry = (best[0], best[1], anchored.chunk_index, best[3], best[4], anchored.text)
            cost = entry_cost(entry)
            if used + cost <= budget:
                selected.append(entry)
                used += cost
                if anchored is None:
                    leftovers.append((source_chunks[1:], frozenset()))
                else:
                    leftovers.append((source_chunks, anchored.covered_chunk_indexes))
                    if anchored.extension_text:
                        pending_extensions.append(
                            (
                                len(selected) - 1,
                                len(leftovers) - 1,
                                anchored.extension_text,
                                anchored.extension_chunk_indexes,
                                False,
                            )
                        )
                    if anchored.upward_extension_text:
                        pending_extensions.append(
                            (
                                len(selected) - 1,
                                len(leftovers) - 1,
                                anchored.upward_extension_text,
                                anchored.upward_extension_chunk_indexes,
                                True,
                            )
                        )
            else:
                leftovers.append((source_chunks, frozenset()))
        # Enumeration extensions spend the leftover pool first (source rank
        # order): a kept-intact list never evicts another source's
        # guaranteed excerpt, only competes with pass-2 chunks.
        for selected_position, leftover_position, extension_text, extension_chunk_indexes, joins_above in pending_extensions:
            current = selected[selected_position]
            neg_score, source_rank, chunk_index, session_id, date, window_text = current
            grown = (
                neg_score,
                source_rank,
                chunk_index,
                session_id,
                date,
                extension_text + "\n" + window_text if joins_above else window_text + "\n" + extension_text,
            )
            # Joined to the window with one newline; charged as the entry's
            # cost DELTA so serializations with escaping stay budget-exact
            # (prose: exactly len(extension_text) + 1, as before).
            extra = entry_cost(grown) - entry_cost(current)
            if used + extra > budget:
                continue
            selected[selected_position] = grown
            used += extra
            entries, covered = leftovers[leftover_position]
            leftovers[leftover_position] = (entries, covered | extension_chunk_indexes)
        # Pass 2: spend what is left on the next-best chunks globally,
        # skipping chunks an anchored window (or applied extension) already shows.
        remaining = [entry for entries, covered in leftovers for entry in entries if entry[2] not in covered]
        remaining.sort(key=lambda item: item[:3])
        for entry in remaining:
            cost = entry_cost(entry)
            if used + cost > budget:
                continue
            selected.append(entry)
            used += cost

        # Render oldest-first so the excerpts read chronologically.
        selected.sort(key=lambda item: (item[4], item[1], item[2]))
        return selected

    def _render_context_json(self, pack: dict[str, object], *, budget: int) -> tuple[str, int]:
        """Compact-JSON context document (``--pack-format json``).

        Same content pipeline as :meth:`_render_context_block` — identical
        memory extraction (text fallback chain, session-date lookup),
        identical grounding notes, and the shared :meth:`_select_excerpts`
        two-pass packing — serialized as the structured document described
        in :mod:`longmemeval.pack_formats`. The budget applies to the
        SERIALIZED length: the envelope and memory/notes sections are
        charged up front and each excerpt record is priced by its compact
        serialization, so ``len(document) <= budget`` whenever the fixed
        sections fit (the same regime prose operates under). An entirely
        empty pack renders ``""`` so the empty-context placeholder path is
        shared with prose.
        """
        memories = pack.get("relevant_memories") or []
        memory_records: list[dict[str, object]] = []
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            text = str(memory.get("canonical_text") or memory.get("summary") or memory.get("title") or "").strip()
            if text == "":
                continue
            metadata = memory.get("metadata_json") if isinstance(memory.get("metadata_json"), dict) else {}
            source_id = str(metadata.get("source_id") or "")
            _session_id, session_date = self._session_label(source_id) if source_id else ("", "undated")
            # Same ISO-normalized date string the prose fact line renders
            # (machine-readable time; see _iso_fact_date), so the two
            # formats carry byte-identical memory dates. The record's
            # date_iso field is derived from it just the same.
            memory_records.append(memory_record(memory, claim=text, date=_iso_fact_date(session_date)))

        grounding = pack.get("grounding") if isinstance(pack.get("grounding"), dict) else None
        grounding_notes: list[str] = []
        if grounding:
            for name in grounding.get("unsupported_entities") or []:
                grounding_notes.append(f'Note: no stored memories mention "{name}".')

        prefix, suffix = document_envelope(memory_records, grounding_notes)
        used = len(prefix) + len(suffix)
        selected = self._select_excerpts(pack, budget=budget, used=used, entry_cost=_json_entry_cost)
        # ---- temporal precompute (derived date values) begin ----------------
        # The same bounded "[derived]" lines prose renders (see
        # _derived_pack_lines), as a top-level "derived_timeline" array.
        # Appended AFTER excerpt selection priced the underived envelope, so
        # the block is uncharged against the excerpt budget exactly like
        # prose (charging would evict excerpt content; see
        # DERIVED_SECTION_MAX_CHARS above) and excerpt admission is
        # byte-identical with or without it.
        derived_lines = _derived_pack_lines(pack)
        suffix = append_derived_timeline(suffix, derived_lines)
        # ---- temporal precompute (derived date values) end ------------------
        if not memory_records and not selected and not grounding_notes and not derived_lines:
            return "", 0
        excerpt_jsons = [compact_json(_json_excerpt_record(entry)) for entry in selected]
        return assemble_document(prefix, excerpt_jsons, suffix), len(selected)


@contextmanager
def question_run(
    question: LongMemEvalQuestion,
    db_path: str | Path,
    *,
    excerpt_source: str | None = None,
    session_label_mode: str = DEFAULT_SESSION_LABEL_MODE,
    promotion_mode: str = DEFAULT_PROMOTION_MODE,
    surface: str = DEFAULT_SURFACE,
) -> Iterator[QuestionRun]:
    """Open an isolated per-question store and yield a :class:`QuestionRun`.

    Uses ``sqlite_user_connection`` (schema bootstrap + one transaction that
    commits on clean exit), exactly like the product's SQLite on-ramp. The
    run-level choices (see the module docstring) are fixed here for the whole
    question.
    """
    with sqlite_user_connection(db_path, LME_USER_ID) as conn:
        ensure_sqlite_user(conn, LME_USER_ID, LME_USER_EMAIL, "LongMemEval Harness")
        store = SQLiteVNextStore(conn, LME_USER_ID)
        yield QuestionRun(
            question,
            store,
            excerpt_source=excerpt_source,
            session_label_mode=session_label_mode,
            promotion_mode=promotion_mode,
            surface=surface,
            db_path=db_path,
        )


__all__ = [
    "ANSWER_MAX_TOKENS",
    "ANSWER_MAX_TOKENS_COT",
    "ANSWER_PROMPT_TEMPLATE",
    "ANSWER_PROMPT_TEMPLATE_COT",
    "CONTEXT_CHAR_BUDGET_ENV",
    "DEFAULT_CONTEXT_CHAR_BUDGET",
    "DEFAULT_EXCERPT_SOURCE",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_PROMOTION_MODE",
    "DEFAULT_SURFACE",
    "DERIVED_SECTION_HEADER",
    "DERIVED_SECTION_MAX_CHARS",
    "EMPTY_CONTEXT_PLACEHOLDER",
    "EXCERPT_SOURCES",
    "EXCERPT_SOURCE_ENV",
    "EXCERPT_SOURCE_PACK_EXCERPTS",
    "EXCERPT_SOURCE_STORE_CHUNKS",
    "IngestStats",
    "LME_USER_EMAIL",
    "LME_USER_ID",
    "MAX_ITEMS_ENV",
    "PROMOTION_MODES",
    "PROMOTION_MODE_ALL_CANDIDATES",
    "PROMOTION_MODE_SOURCES_ONLY",
    "QuestionRun",
    "RECALL_MAX_LIMIT",
    "RECALL_RESULT_FORMAT",
    "ROLLUP_ACCEPTANCE_REASON",
    "RetrievalOutcome",
    "RollupAcceptanceStats",
    "SURFACES",
    "SURFACE_CONTEXT_PACK",
    "SURFACE_RECALL",
    "build_answer_prompt",
    "collapse_intra_turn_blank_lines",
    "context_char_budget_from_env",
    "excerpt_source_from_env",
    "max_items_from_env",
    "question_run",
    "recall_surface_blocker",
    "render_session_text",
    "resolve_excerpt_source",
    "validate_promotion_mode",
    "validate_surface",
]
