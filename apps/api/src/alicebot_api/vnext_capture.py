from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import md5, sha256
import json
import logging
import math
from pathlib import Path
import re
from typing import Mapping, Protocol, Sequence, cast

from alicebot_api.credential_floor import (
    CREDENTIAL_MATERIAL_REFUSED_MESSAGE,
    EXPANSION_REFUSED_MESSAGE,
    VERDICT_EXPANSION,
    private_key_armor_role,
)
from alicebot_api.importer_paths import (
    DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    DEFAULT_MAX_TEXT_FILE_BYTES,
    MIB,
    ImportFileTooLargeError,
    read_contained_source_text,
)
from alicebot_api.legacy_credential_check import commit_door_fields_verdict
from alicebot_api.memory_provenance import (
    ASSERTION_CLASS_USER_ASSERTED,
    PROVENANCE_ROLE_USER,
    classify_assertion,
    derive_speaker_role,
    order_by_provenance,
    provenance_promotion_rank,
)
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding, attach_memory_embeddings
from alicebot_api.vnext_entities import (
    ENTITY_EXTRACTION_SKIP_SENSITIVITIES,
    EntityLinkingService,
    store_supports_entity_linking,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_project_scope import (
    normalize_project_scope,
    project_scope_identity,
    resolve_project_scope,
    source_capture_identity_matches,
    source_project_scope,
)
from alicebot_api.vnext_repositories import JsonObject


DEFAULT_CHUNK_MAX_CHARS = 2_400
SUPPORTED_TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".text"})
SOURCE_IMPORT_ERROR_CODE = "source_import_failed"
SOURCE_IMPORT_ERROR_MESSAGE = "Source could not be imported"
CREDENTIAL_WITHHELD = "[withheld: credential material]"
ENTITY_EXTRACTION_ERROR_CODE = "entity_extraction_failed"
ENTITY_EXTRACTION_ERROR_MESSAGE = "Entity extraction failed"
# One ChatGPT conversation the importer could not turn into a transcript. The
# receipt names it by position, and the other conversations still import.
CHATGPT_CONVERSATION_UNREADABLE_CODE = "conversation_unreadable"
CHATGPT_CONVERSATION_UNREADABLE_MESSAGE = "Conversation could not be read"
logger = logging.getLogger(__name__)


def _unit_verdict(value: object) -> str | None:
    """The commit door's verdict for one import unit.

    The same check ``capture_source`` uses. A line, message, title, id, or
    name the floor misses and the commit gate flags is withheld here, so
    the rest of the file or conversation still imports.
    """

    return commit_door_fields_verdict(value)


class VNextCaptureValidationError(ValueError):
    """Raised when a vNext capture request cannot be normalized."""


class CaptureCredentialRefused(VNextCaptureValidationError):
    """A capture field carries credential material. Nothing was written."""


class ImportFileTooLargeRefused(VNextCaptureValidationError):
    """A selected import file is over the per-file size limit. Nothing was written.

    The file was refused before it was read. ``reason_code`` is what a command
    puts in its error record, so a person or an agent can tell this refusal from
    a bad path or a file that is not text.
    """

    reason_code = ImportFileTooLargeError.reason_code


class VNextCaptureStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def get_source_by_content_hash(self, content_hash: str) -> JsonObject | None: ...

    def create_source(self, source: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def create_source_chunk(self, chunk: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def create_memory(self, memory: JsonObject, *, actor_type: str = "system") -> JsonObject: ...

    def create_provenance_link(self, link: JsonObject, *, actor_type: str = "system") -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class CaptureCandidate:
    text: str
    memory_type: str
    source_chunk_id: str
    source_chunk_index: int
    confidence: float
    extraction_rule: str
    # Speaker provenance, derived only from a leading "[USER]:" /
    # "[ASSISTANT]:" transcript tag in the line itself (content shape, never
    # external labels). None for untagged content, which keeps the exact
    # pre-provenance candidate shape.
    provenance_role: str | None = None
    assertion_class: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureResult:
    status: str
    source_id: str | None
    content_hash: str
    chunk_count: int = 0
    candidate_memory_count: int = 0
    duplicate: bool = False
    errors: tuple[str, ...] = ()
    # Internal two-phase handoff. Deliberately omitted from ``to_record`` so
    # enabling deferred persistence does not change the public capture API.
    deferred_embedding_inputs: tuple[DeferredMemoryEmbedding, ...] = ()

    def _receipt_line(self) -> str:
        """One line a host can print. Import is a source, not a fact."""

        if self.status == "duplicate" and not self.errors:
            return "duplicate source"
        if self.status != "imported" or self.errors:
            return "capture failed"
        if self.chunk_count <= 0:
            return "saved as source"
        receipt = f"saved as source, {self.chunk_count} chunks searchable now"
        if self.candidate_memory_count > 0:
            receipt += f", {self.candidate_memory_count} candidates waiting in review"
        return receipt

    def to_record(self) -> JsonObject:
        return {
            "status": self.status,
            "source_id": self.source_id,
            "content_hash": self.content_hash,
            "chunk_count": self.chunk_count,
            "candidate_memory_count": self.candidate_memory_count,
            "duplicate": self.duplicate,
            "errors": list(self.errors),
            "receipt": self._receipt_line(),
            # Side by side, chunk_count and candidate_memory_count read as two
            # counts of the same stored thing. They are not: the chunks answer
            # a query on the next call, the candidates answer nothing until a
            # reviewer promotes them. Saying which is which is the difference
            # between an agent reporting "saved and findable" and an agent
            # sending its user to a review queue it does not need to visit.
            "retrieval": {
                "searchable_now": "source_material" if self.chunk_count else "nothing",
                "searchable_chunks": self.chunk_count,
                "awaiting_review": self.candidate_memory_count,
                "how": (
                    "alice_recall and alice_context_pack return this document's "
                    "matching passages now, as sources rather than as facts"
                )
                if self.chunk_count
                else "nothing was indexed from this capture",
            },
        }


@dataclass(frozen=True, slots=True)
class BatchImportResult:
    status: str
    imported_count: int
    duplicate_count: int
    failed_count: int
    source_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    error_code: str | None = None
    deferred_embedding_inputs: tuple[DeferredMemoryEmbedding, ...] = ()
    skipped_count: int = 0
    skipped_credentials: int = 0
    skipped_credential_items: tuple[str, ...] = ()

    def to_record(self) -> JsonObject:
        return {
            "status": self.status,
            "imported_count": self.imported_count,
            "duplicate_count": self.duplicate_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "skipped_credentials": self.skipped_credentials,
            "skipped_credential_items": list(self.skipped_credential_items),
            "source_ids": list(self.source_ids),
            "errors": list(self.errors),
            "error_code": self.error_code,
        }


def _batch_status(
    *,
    imported_count: int,
    duplicate_count: int,
    failed_count: int,
    skipped_count: int,
) -> str:
    """Batch outcome. A credential skip is not a failure."""

    if imported_count == 0 and duplicate_count == 0 and failed_count > 0:
        return "failed"
    if imported_count == 0 and duplicate_count > 0 and failed_count == 0:
        return "duplicate"
    if imported_count == 0 and duplicate_count == 0 and failed_count == 0 and skipped_count > 0:
        return "skipped"
    if failed_count:
        return "partial"
    return "ok"


@dataclass(frozen=True, slots=True)
class SourceCaptureInput:
    source_type: str
    raw_text: str
    title: str | None = None
    author: str | None = None
    uri: str | None = None
    raw_path: str | None = None
    connector_name: str | None = None
    external_id: str | None = None
    domain: str = "unknown"
    sensitivity: str = "unknown"
    # Effective project scope for the capture. Persisted onto the source and
    # every promoted candidate memory so the owning project's filtered recall
    # retrieves it and other projects are scoped out. ``None`` means omitted;
    # an empty tuple is an explicit unscoped declaration and is persisted.
    project_scope: tuple[str, ...] | None = None
    captured_at: str | None = None
    source_created_at: str | None = None
    source_modified_at: str | None = None
    metadata_json: JsonObject = field(default_factory=dict)
    # Imports pass False. A capture still proposes candidates unless the
    # caller says this source is material to read, not a fact to review.
    extract_candidates: bool = True


def normalize_text(raw_text: str) -> str:
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if normalized == "":
        raise VNextCaptureValidationError("source text must not be empty")
    return normalized


def content_hash_for_text(raw_text: str, project_scope: Sequence[str] = ()) -> str:
    normalized = normalize_text(raw_text)
    scope = project_scope_identity(project_scope)
    if scope:
        # Fold project scope into the content identity so the same text captured
        # under different project scopes is NOT deduped away: each scope keeps
        # its own source and scoped candidate (audit 2 P1 #2). Empty scope hashes
        # exactly as before, so global/unscoped captures stay byte-identical.
        normalized = normalized + "\x00project_scope:" + "\x00".join(scope)
    return "sha256:" + sha256(normalized.encode("utf-8")).hexdigest()


def raw_text_sha256(raw_text: str) -> str:
    """Digest the exact captured text, without folding identity scope into it."""
    return "sha256:" + sha256(raw_text.encode("utf-8")).hexdigest()


def capture_dedupe_key_for_text(
    raw_text: str,
    project_scope: Sequence[str] = (),
    *,
    domain: str | None = None,
    sensitivity: str | None = None,
) -> str:
    """Internal source identity, including any explicitly supplied classification.

    Calls that omit classification retain the pre-v0.10 digest so upgraded
    stores can still recognize legacy rows. New capture writes always supply
    both values: the same evidence may legitimately exist under a different
    domain or sensitivity without losing the newly intended classification.
    """
    normalized = normalize_text(raw_text)
    scope = project_scope_identity(project_scope)
    if scope:
        normalized += "\x1fproject_scope:" + "\x1f".join(scope)
    if domain is not None or sensitivity is not None:
        normalized += "\x1fdomain:" + str(domain or "unknown").strip().casefold()
        normalized += "\x1fsensitivity:" + str(sensitivity or "unknown").strip().casefold()
    return "capture-md5:" + md5(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()


def capture_dedupe_key_for_source(source: Mapping[str, object]) -> str | None:
    """Recompute a source identity from its current persisted envelope.

    ``raw_text`` is the only lossless input to the capture normalizer. Older
    rows which do not preserve it cannot safely retain a key after their scope
    or classification changes, so callers must release the key in that case.
    """

    raw_text = source_capture_raw_text(source)
    if raw_text is None or not raw_text.strip():
        return None
    return capture_dedupe_key_for_text(
        raw_text,
        source_project_scope(source),
        domain=str(source.get("domain") or "unknown"),
        sensitivity=str(source.get("sensitivity") or "unknown"),
    )


def capture_content_hash_for_source(source: Mapping[str, object]) -> str | None:
    """Recompute the public text/project identity from preserved evidence."""

    raw_text = source_capture_raw_text(source)
    if raw_text is None or not raw_text.strip():
        return None
    return content_hash_for_text(raw_text, source_project_scope(source))


def source_capture_raw_text(source: Mapping[str, object]) -> str | None:
    """Return preserved raw capture evidence without normalizing it."""

    metadata = source.get("metadata_json")
    raw_text = metadata.get("raw_text") if isinstance(metadata, Mapping) else None
    return raw_text if isinstance(raw_text, str) else None


def _truncate(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."


def _split_on_words(part: str, *, max_chars: int) -> list[str]:
    """Last resort for text with no usable line structure.

    Cuts at word boundaries, and at character boundaries for a single token
    longer than the whole budget.
    """

    words = part.split()
    if not words:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for word in words:
        if len(word) > max_chars:
            if current:
                chunks.append(" ".join(current))
                current = []
                current_length = 0
            chunks.extend(word[index : index + max_chars] for index in range(0, len(word), max_chars))
            continue

        projected_length = current_length + len(word) + (1 if current else 0)
        if current and projected_length > max_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_length = len(word)
            continue

        current.append(word)
        current_length = projected_length

    if current:
        chunks.append(" ".join(current))
    return chunks


def _split_large_part(part: str, *, max_chars: int) -> list[str]:
    """Split one oversized paragraph, preferring its own line structure.

    A paragraph only reaches here when it alone exceeds ``max_chars``. The
    original implementation went straight to ``part.split()``, which splits on
    every whitespace character and rejoins with single spaces, so it cut at an
    arbitrary word AND discarded the paragraph's internal newlines.

    That is wrong for the shape it actually meets: a numbered or bulleted list
    written one item per line with no blank line between items. Markdown treats
    that as a single paragraph, so a long list arrived here and came out as
    flattened word-count slices with individual items cut in half.

    Found 2026-08-17 in a real 226-document vault import: of 710 chunks, five
    carried no newline at all and two sat at the character ceiling, one of them
    ending mid-sentence inside item 22 of a quote list.

    Oversized lines are handled per line rather than disqualifying the whole
    paragraph. An earlier draft required *every* line to fit before packing by
    line, so one long item in a list flattened all the others with it. Review
    caught that; the mixed case is now covered by tests.
    """

    lines = part.split("\n")
    if len(lines) > 1:
        packed: list[str] = []
        current_lines: list[str] = []
        current_length = 0
        for line in lines:
            if len(line) > max_chars:
                # This one line cannot be kept whole. Flush what is buffered,
                # then word-split only the offending line.
                if current_lines:
                    packed.append("\n".join(current_lines))
                    current_lines = []
                    current_length = 0
                packed.extend(_split_on_words(line, max_chars=max_chars))
                continue

            projected = current_length + len(line) + (1 if current_lines else 0)
            if current_lines and projected > max_chars:
                packed.append("\n".join(current_lines))
                current_lines = [line]
                current_length = len(line)
                continue

            current_lines.append(line)
            current_length = projected

        if current_lines:
            packed.append("\n".join(current_lines))
        return [chunk for chunk in packed if chunk.strip()]

    return _split_on_words(part, max_chars=max_chars)


# A markdown heading or thematic break is an author-declared section boundary.
# Packing across one merges unrelated material into a single chunk, which is how
# a bulk note import produces candidate memories that span adjacent notes.
_SECTION_BOUNDARY = re.compile(r"^(?:#{1,6}\s|(?:-{3,}|\*{3,}|_{3,})\s*$)")


def _starts_new_section(paragraph: str) -> bool:
    first_line = paragraph.lstrip().split("\n", 1)[0].rstrip()
    return bool(_SECTION_BOUNDARY.match(first_line))


def chunk_text(raw_text: str, *, max_chars: int = DEFAULT_CHUNK_MAX_CHARS) -> list[str]:
    """Split text into retrieval chunks, never packing across a section boundary.

    Paragraphs are packed together up to ``max_chars``, which keeps prose chunks
    usefully large. Markdown headings and thematic breaks end the current chunk
    first, because they are the author saying "different subject".

    Without that rule a whole-vault note import collapses many short, unrelated
    notes into one chunk: an imported quotes library produced candidate memories
    spanning several adjacent quotes, and recalling any single quote by its exact
    wording then returned nothing. Reported from a real Obsidian import,
    2026-08-15.
    """

    if max_chars < 200:
        raise VNextCaptureValidationError("chunk max_chars must be at least 200")

    normalized = normalize_text(raw_text)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if current and _starts_new_section(paragraph):
            chunks.append(current)
            current = ""

        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_large_part(paragraph, max_chars=max_chars))
            continue

        separator = "\n\n" if current else ""
        projected = f"{current}{separator}{paragraph}"
        if current and len(projected) > max_chars:
            chunks.append(current)
            current = paragraph
            continue
        current = projected

    if current:
        chunks.append(current)
    return chunks or [normalized]


_PREFIX_RULES: tuple[tuple[str, str, float, str], ...] = (
    ("decision:", "decision", 0.86, "prefixed_decision"),
    ("preference:", "preference", 0.84, "prefixed_preference"),
    ("prefer:", "preference", 0.82, "prefixed_preference"),
    ("remember:", "semantic", 0.82, "prefixed_semantic"),
    ("fact:", "semantic", 0.82, "prefixed_semantic"),
    ("belief:", "belief", 0.78, "prefixed_belief"),
    ("question:", "question", 0.72, "prefixed_question"),
    ("answer:", "answer", 0.72, "prefixed_answer"),
    ("commitment:", "open_loop", 0.76, "prefixed_commitment"),
    ("todo:", "open_loop", 0.74, "prefixed_open_loop"),
    ("next action:", "open_loop", 0.74, "prefixed_open_loop"),
    ("procedure:", "procedure", 0.80, "prefixed_procedure"),
    ("playbook:", "procedure", 0.80, "prefixed_procedure"),
    ("happened:", "episode", 0.74, "prefixed_episode"),
    ("log:", "episode", 0.72, "prefixed_episode"),
)

# "How to ..." lines are playbook headings, not colon prefixes, so the whole
# line is kept as the candidate text. Lines ending in "?" stay with the
# existing question rule: "How to fix the printer?" is a question the user
# asked, not procedure content.
_HOW_TO_PREFIX = "how to "


def _strip_markdown_prefix(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^[-*]\s+", "", stripped)
    stripped = re.sub(r"^\d+\.\s+", "", stripped)
    stripped = re.sub(r"^#{1,6}\s+", "", stripped)
    return stripped.strip()


USER_ASSERTED_VALUE_CONFIDENCE = 0.72
USER_ASSERTED_VALUE_RULE = "user_asserted_value"


def _annotate_candidate_provenance(candidate: CaptureCandidate) -> CaptureCandidate:
    """Attach speaker provenance to a speaker-tagged candidate.

    Gated on a leading speaker tag in the candidate text: untagged content
    derives no role and is returned unchanged (byte-identical old path).
    Provenance biases promotion ORDER only (``order_candidates_for_promotion``);
    it never adjusts confidence -- pack ranking does not read confidence, so
    a confidence delta here would be config implying nonexistent behavior.
    """
    role = derive_speaker_role(candidate.text)
    if role is None:
        return candidate
    return replace(
        candidate,
        provenance_role=role,
        assertion_class=classify_assertion(candidate.text, role),
    )


def candidate_promotion_rank(candidate: CaptureCandidate) -> int:
    """Promotion-rank ordinal for a capture candidate (lower wins ties)."""
    return provenance_promotion_rank(
        provenance_role=candidate.provenance_role,
        assertion_class=candidate.assertion_class,
    )


def order_candidates_for_promotion(candidates: list[CaptureCandidate]) -> list[CaptureCandidate]:
    """Same-slot promotion bias: user-asserted values outrank assistant estimates.

    Stable ordering by provenance rank only — candidate lists without any
    speaker-tagged content come back in their original order unchanged.
    """
    return order_by_provenance(candidates, rank_of=candidate_promotion_rank)


def _candidate_from_line(line: str, *, source_chunk_id: str, source_chunk_index: int) -> CaptureCandidate | None:
    candidate = _base_candidate_from_line(
        line,
        source_chunk_id=source_chunk_id,
        source_chunk_index=source_chunk_index,
    )
    if candidate is not None:
        return _annotate_candidate_provenance(candidate)

    # New (gated) rule: a speaker-tagged USER line asserting a concrete
    # value in the first person ("[USER]: I paid $50 for the taxi") becomes
    # a semantic candidate even though it matches no legacy rule. Untagged
    # lines never reach this branch with a role, so the legacy behavior is
    # byte-identical for them.
    normalized = _strip_markdown_prefix(line)
    if not normalized:
        return None
    role = derive_speaker_role(normalized)
    if role != PROVENANCE_ROLE_USER:
        return None
    if classify_assertion(normalized, role) != ASSERTION_CLASS_USER_ASSERTED:
        return None
    return CaptureCandidate(
        text=normalized,
        memory_type="semantic",
        source_chunk_id=source_chunk_id,
        source_chunk_index=source_chunk_index,
        confidence=USER_ASSERTED_VALUE_CONFIDENCE,
        extraction_rule=USER_ASSERTED_VALUE_RULE,
        provenance_role=role,
        assertion_class=ASSERTION_CLASS_USER_ASSERTED,
    )


def _base_candidate_from_line(line: str, *, source_chunk_id: str, source_chunk_index: int) -> CaptureCandidate | None:
    normalized = _strip_markdown_prefix(line)
    if not normalized or normalized == "---":
        return None

    lowered = normalized.casefold()
    for prefix, memory_type, confidence, rule in _PREFIX_RULES:
        if not lowered.startswith(prefix):
            continue
        text = normalized[len(prefix) :].strip()
        if text:
            return CaptureCandidate(
                text=text,
                memory_type=memory_type,
                source_chunk_id=source_chunk_id,
                source_chunk_index=source_chunk_index,
                confidence=confidence,
                extraction_rule=rule,
            )

    if lowered.startswith(_HOW_TO_PREFIX) and len(normalized) > len(_HOW_TO_PREFIX) and not normalized.endswith("?"):
        return CaptureCandidate(
            text=normalized,
            memory_type="procedure",
            source_chunk_id=source_chunk_id,
            source_chunk_index=source_chunk_index,
            confidence=0.68,
            extraction_rule="how_to_procedure",
        )

    if normalized.endswith("?"):
        return CaptureCandidate(
            text=normalized,
            memory_type="question",
            source_chunk_id=source_chunk_id,
            source_chunk_index=source_chunk_index,
            confidence=0.62,
            extraction_rule="question_sentence",
        )

    if len(normalized) >= 24 and re.search(r"\b(is|are|was|were|will|should|needs?|must|prefers?)\b", lowered):
        return CaptureCandidate(
            text=normalized,
            memory_type="semantic",
            source_chunk_id=source_chunk_id,
            source_chunk_index=source_chunk_index,
            confidence=0.58,
            extraction_rule="claim_sentence",
        )

    return None


def extract_candidate_memories(chunks: list[JsonObject]) -> list[CaptureCandidate]:
    candidates: list[CaptureCandidate] = []
    seen: set[str] = set()
    for chunk in chunks:
        chunk_id = str(chunk["id"])
        chunk_index_value = chunk["chunk_index"]
        if not isinstance(chunk_index_value, int):
            raise VNextCaptureValidationError("source chunk index must be an integer")
        chunk_index = chunk_index_value
        text = str(chunk["text"])
        for line in text.splitlines():
            candidate = _candidate_from_line(
                line,
                source_chunk_id=chunk_id,
                source_chunk_index=chunk_index,
            )
            if candidate is None:
                continue
            dedupe_key = f"{candidate.memory_type}:{candidate.text.casefold()}"
            if dedupe_key in seen:
                continue
            candidates.append(candidate)
            seen.add(dedupe_key)
    return candidates


def _memory_key(
    *,
    content_hash: str,
    candidate: CaptureCandidate,
    domain: str | None = None,
    sensitivity: str | None = None,
) -> str:
    identity = f"{content_hash}|{candidate.source_chunk_index}|{candidate.text}"
    if domain is not None or sensitivity is not None:
        identity += f"|domain:{domain or 'unknown'}|sensitivity:{sensitivity or 'unknown'}"
    digest = sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"vnext.capture.{candidate.memory_type}.{digest}"


def _chatgpt_conversations(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    conversations = payload.get("conversations")
    if isinstance(conversations, list):
        return [item for item in conversations if isinstance(item, dict)]
    if isinstance(payload.get("mapping"), dict) or isinstance(payload.get("messages"), list):
        return [payload]
    return []


def _ordered_chatgpt_nodes(conversation: dict[str, object]) -> list[dict[str, object]]:
    """Return every mapping node in parent-before-child graph order.

    ChatGPT export mappings are keyed by opaque IDs whose lexical order has no
    conversational meaning. The current branch is visited first, while any
    alternate branches are retained afterward instead of silently discarded.
    """
    messages = conversation.get("messages")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]

    raw_mapping = conversation.get("mapping")
    if not isinstance(raw_mapping, dict):
        return []
    mapping = {str(node_id): node for node_id, node in raw_mapping.items() if isinstance(node, dict)}
    if not mapping:
        return []

    children_by_parent: dict[str, list[str]] = {node_id: [] for node_id in mapping}
    for node_id, node in mapping.items():
        parent = node.get("parent")
        if isinstance(parent, str) and parent in mapping:
            children_by_parent[parent].append(node_id)
    # Prefer the export's explicit child order where present, then append any
    # derived links that an incomplete export omitted from ``children``.
    for node_id, node in mapping.items():
        explicit = node.get("children")
        if not isinstance(explicit, list):
            continue
        ordered = [child for child in explicit if isinstance(child, str) and child in mapping]
        children_by_parent[node_id] = list(dict.fromkeys([*ordered, *children_by_parent[node_id]]))

    active_successor: dict[str, str] = {}
    current = conversation.get("current_node")
    if isinstance(current, str) and current in mapping:
        active_chain: list[str] = []
        seen: set[str] = set()
        while current in mapping and current not in seen:
            seen.add(current)
            active_chain.append(current)
            parent = mapping[current].get("parent")
            if not isinstance(parent, str):
                break
            current = parent
        active_chain.reverse()
        active_successor.update(zip(active_chain, active_chain[1:]))

    roots = [
        node_id
        for node_id, node in mapping.items()
        if not isinstance(node.get("parent"), str) or node.get("parent") not in mapping
    ]
    visited: set[str] = set()
    ordered_nodes: list[dict[str, object]] = []

    def children_in_visit_order(node_id: str) -> list[str]:
        preferred = active_successor.get(node_id)
        children = children_by_parent[node_id]
        if preferred in children:
            children = [preferred, *(child for child in children if child != preferred)]
        return children

    def visit(start: str) -> None:
        # Depth first, parent before child, each node once. This is a loop over
        # an explicit stack and not a recursive call per node: one long chain of
        # replies is one node deep per message, and a conversation of a few
        # thousand messages overran the interpreter's recursion limit and
        # failed the whole import. A node is marked visited when it is popped,
        # not when it is pushed, and its children are pushed last first so the
        # first child is popped next. That is the order the recursive walk
        # produced, including for a node reachable by two paths and for a
        # cycle.
        stack = [start]
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            ordered_nodes.append(mapping[node_id])
            stack.extend(reversed(children_in_visit_order(node_id)))

    for root in roots:
        visit(root)
    # Malformed/cyclic exports still retain every node once in source order.
    for node_id in mapping:
        visit(node_id)
    return ordered_nodes


def _chatgpt_timestamp(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        try:
            return datetime.fromtimestamp(numeric, tz=UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        stripped = value.strip()
        try:
            return _chatgpt_timestamp(float(stripped))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return None


def _chatgpt_message_parts(message: dict[str, object]) -> list[str]:
    content = message.get("content")
    values: list[object]
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            values = list(parts)
        else:
            values = [content.get("text") or content.get("content") or content.get("message")]
    elif content is not None:
        values = [content]
    else:
        values = [message.get("text") or message.get("message")]

    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        for line in value.replace("\r\n", "\n").replace("\r", "\n").splitlines() or [value]:
            text = " ".join(line.split()).strip()
            if text:
                normalized.append(text)
    return normalized


@dataclass(frozen=True, slots=True)
class _ChatGPTConversationTranscript:
    index: int
    external_id: str
    title: str
    raw_text: str
    message_count: int
    created_at: str | None
    modified_at: str | None


@dataclass(frozen=True, slots=True)
class _ChatGPTConversationRefused:
    """A conversation whose transcript could not be built.

    Only its position is kept. The reason is the same for every conversation
    that lands here, and what went wrong goes to the process log.
    """

    index: int


def _chatgpt_conversation_id(conversation: dict[str, object], *, index: int) -> str:
    for key in ("id", "conversation_id"):
        value = conversation.get(key)
        if value is not None and str(value).strip():
            return " ".join(str(value).split())
    return f"conversation-{index}"


def _printed_label(value: str | None) -> str:
    """Receipt text for a path or title. A flagged value prints as withheld."""

    if value is None or value == "":
        return "withheld"
    if _unit_verdict(value) is not None:
        return "withheld"
    return value


def _stored_label(value: str | None) -> str | None:
    if value is None:
        return None
    if _unit_verdict(value) is not None:
        return "withheld"
    return value


def _label_event_value(value: object) -> object:
    """Apply ``_stored_label`` to every string in an event payload value."""

    if isinstance(value, str):
        return _stored_label(value)
    if isinstance(value, dict):
        return {key: _label_event_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_label_event_value(item) for item in value]
    return value


def _line_phrase(start: int, end: int) -> str:
    if start == end:
        return f"line {start}"
    return f"lines {start} to {end}"


def _file_phrase(index: int, name: str) -> str:
    shown = "name withheld" if _unit_verdict(name) is not None else name
    return f"file {index} ({shown})"


def _conversation_phrase(conversation: str, message_index: int) -> str:
    return f"conversation {conversation} message {message_index}"


def _remember_item(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _refuse_flagged_import_folder(source: str | Path) -> None:
    """A token in the folder name refuses the import before any write.

    The error text does not include the path.
    """

    path = Path(source).expanduser()
    if path.is_dir() or not path.suffix:
        folder_name = path.name
    else:
        folder_name = path.parent.name
    if folder_name and _unit_verdict(folder_name) is not None:
        raise VNextCaptureValidationError("The import folder name is withheld")


def _public_markdown_import_error(exc: Exception) -> VNextCaptureValidationError:
    """Name a UTF-8 failure by file, and keep a flagged name out of the text."""

    message = str(exc)
    prefix = "import source file is not valid UTF-8 text: "
    if message.startswith(prefix):
        name = Path(message[len(prefix) :]).name
        safe = "withheld" if _unit_verdict(name) is not None else name
        return VNextCaptureValidationError(f"import source file is not valid UTF-8 text: {safe}")
    if _unit_verdict(message) is not None:
        return VNextCaptureValidationError("The import path is withheld")
    return VNextCaptureValidationError(message)


def _mib_phrase(size_bytes: int) -> str:
    return f"{size_bytes / MIB:.2f} MiB"


def _public_too_large_error(exc: ImportFileTooLargeError) -> ImportFileTooLargeRefused:
    """Name the file by its own name, never its path, and state both numbers.

    A file name the commit door flags is withheld, as it is for a file that is
    not valid UTF-8.
    """

    name = exc.file_path.name
    shown = "a file whose name is withheld" if _unit_verdict(name) is not None else name
    if exc.exact:
        size_phrase = f"is {exc.size_bytes} bytes ({_mib_phrase(exc.size_bytes)})"
    else:
        size_phrase = f"is more than {exc.limit_bytes} bytes ({_mib_phrase(exc.limit_bytes)})"
    return ImportFileTooLargeRefused(
        f"import source file is too large: {shown} {size_phrase}, "
        f"the limit is {exc.limit_bytes} bytes ({_mib_phrase(exc.limit_bytes)})"
    )


def read_named_text_file(file_path: Path, *, max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES) -> str:
    """Read the one text file an operator named, bounded, and without following a link at the open.

    ``capture_file`` and the ``--file`` options of the CLI read a file the caller
    named, not a folder. They read it the way the importers read theirs: one open
    with ``O_NOFOLLOW`` and ``O_NONBLOCK``, the size taken from that descriptor
    before any byte is read, a read that stops one byte past ``max_file_bytes``,
    and strict UTF-8 with universal newlines. A path the caller gave may itself be
    a link, and the caller resolves it first, as the importers resolve their
    source. What the open refuses is a final component that is a link by the time
    it is opened, a file that is not a regular file (a FIFO, a device), and a file
    over the limit.

    A file over the limit raises ``ImportFileTooLargeRefused``. Every other refusal
    is a ``VNextCaptureValidationError`` that names the file and never its text.
    """

    try:
        return read_contained_source_text(file_path, max_bytes=max_file_bytes, error_factory=ValueError)
    except ImportFileTooLargeError as exc:
        raise _public_too_large_error(exc) from exc
    except ValueError as exc:
        raise _public_markdown_import_error(exc) from exc


def _filter_markdown_units(raw_text: str, *, file_index: int, file_name: str) -> tuple[str, list[str]]:
    """Replace each flagged line, or one private-key block, before capture.

    Line numbers count from 1 on the first line of the file. Each receipt
    item names the file, then ``line N`` or ``lines N to M``, and never
    the matched text. A flagged file name is ``file K (name withheld)``.
    An unmatched BEGIN line is withheld through the end of the file, so
    the key body is not left for the capture backstop to store.
    """

    from alicebot_api.markdown_import import _armored_private_key_ranges

    file_label = _file_phrase(file_index, file_name)
    lines = raw_text.splitlines()
    ranges = _armored_private_key_ranges(lines)
    kept: list[str] = []
    skipped: list[str] = []
    index = 0
    while index < len(lines):
        line_no = index + 1
        span = ranges.get(line_no)
        if span is not None:
            start_no, end_no = span
            if line_no != start_no:
                index += 1
                continue
            block = "\n".join(lines[start_no - 1 : end_no])
            if _unit_verdict(block) is not None:
                kept.append(CREDENTIAL_WITHHELD)
                skipped.append(f"{file_label} {_line_phrase(start_no, end_no)}")
            else:
                kept.extend(lines[start_no - 1 : end_no])
            index = end_no
            continue
        line = lines[index]
        role = private_key_armor_role(line)
        if role is not None and role[0] == "begin":
            kept.append(CREDENTIAL_WITHHELD)
            skipped.append(f"{file_label} {_line_phrase(line_no, len(lines))}")
            break
        if line.strip() and _unit_verdict(line) is not None:
            kept.append(CREDENTIAL_WITHHELD)
            skipped.append(f"{file_label} {_line_phrase(line_no, line_no)}")
        else:
            kept.append(line)
        index += 1
    return "\n".join(kept), skipped


def _chatgpt_conversation_transcript(
    conversation: dict[str, object],
    *,
    index: int,
    credential_skips: list[str] | None = None,
) -> _ChatGPTConversationTranscript:
    title = " ".join(str(conversation.get("title") or f"Conversation {index}").split())
    if not title:
        title = f"Conversation {index}"
    external_id = _chatgpt_conversation_id(conversation, index=index)
    message_lines: list[str] = []
    message_timestamps: list[str] = []
    message_count = 0
    for node in _ordered_chatgpt_nodes(conversation):
        message_value = node.get("message") if isinstance(node.get("message"), dict) else node
        if not isinstance(message_value, dict):
            continue
        parts = _chatgpt_message_parts(message_value)
        if not parts:
            continue
        author = message_value.get("author")
        role_value = author.get("role") if isinstance(author, dict) else message_value.get("role")
        role = str(role_value or "unknown").strip().upper()
        role = re.sub(r"[^A-Z0-9_-]", "_", role) or "UNKNOWN"
        timestamp_value = message_value.get("create_time")
        if timestamp_value is None:
            timestamp_value = node.get("create_time")
        timestamp = _chatgpt_timestamp(timestamp_value)
        if timestamp is not None:
            message_timestamps.append(timestamp)
            message_lines.append(f"[AT]: {timestamp}")
        message_count += 1
        safe_parts: list[str] = []
        for part in parts:
            if _unit_verdict(part) is not None:
                safe_parts.append(CREDENTIAL_WITHHELD)
                if credential_skips is not None:
                    _remember_item(
                        credential_skips,
                        _conversation_phrase(_printed_label(external_id), message_count),
                    )
            else:
                safe_parts.append(part)
        joined = "\n".join(safe_parts)
        # A secret that only exists across parts of this message is withheld
        # here. The rest of the conversation stays in the transcript.
        if _unit_verdict(joined) is not None:
            safe_parts = [CREDENTIAL_WITHHELD]
            if credential_skips is not None:
                _remember_item(
                    credential_skips,
                    _conversation_phrase(_printed_label(external_id), message_count),
                )
        message_lines.extend(f"[{role}]: {part}" for part in safe_parts)

    created_at = _chatgpt_timestamp(conversation.get("create_time"))
    modified_at = _chatgpt_timestamp(conversation.get("update_time"))
    if created_at is None and message_timestamps:
        created_at = min(message_timestamps)
    if modified_at is None and message_timestamps:
        modified_at = max(message_timestamps)

    if _unit_verdict(title) is not None:
        if credential_skips is not None:
            _remember_item(
                credential_skips,
                f"conversation {_printed_label(external_id)} title",
            )
        title = "withheld"
    if _unit_verdict(external_id) is not None:
        external_id = "withheld"
    lines = [
        f"[CONVERSATION]: {external_id}",
        f"[TITLE]: {title}",
    ]
    if created_at is not None:
        lines.append(f"[CREATED_AT]: {created_at}")
    if modified_at is not None:
        lines.append(f"[UPDATED_AT]: {modified_at}")
    lines.extend(message_lines)
    return _ChatGPTConversationTranscript(
        index=index,
        external_id=external_id,
        title=title,
        raw_text="\n".join(lines),
        message_count=message_count,
        created_at=created_at,
        modified_at=modified_at,
    )


class VNextCaptureService:
    def __init__(
        self,
        store: VNextCaptureStore,
        *,
        chunk_max_chars: int = DEFAULT_CHUNK_MAX_CHARS,
        actor_type: str = "system",
        actor_id: str | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
        agent_identity: JsonObject | None = None,
        policy_decision: JsonObject | None = None,
        defer_embeddings: bool = False,
    ) -> None:
        self.store = store
        self.chunk_max_chars = chunk_max_chars
        self.actor_type = actor_type
        self.actor_id = actor_id
        self.trace_id = trace_id
        self.run_id = run_id
        self.agent_identity = agent_identity
        self.policy_decision = policy_decision
        self.defer_embeddings = defer_embeddings

    def _log_event(
        self,
        *,
        event_type: str,
        payload: JsonObject,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> JsonObject:
        return append_event(
            self.store,
            event_type=event_type,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            trace_id=self.trace_id,
            run_id=self.run_id,
            payload={
                **payload,
                "agent_identity": self.agent_identity,
                "policy_decision": self.policy_decision,
            },
            target_type=target_type,
            target_id=target_id,
        )

    def _log_failure(self, *, source_type: str, title: str | None, error: Exception, metadata: JsonObject) -> None:
        logger.error(
            "source import failed source_type=%s error_code=%s",
            source_type,
            SOURCE_IMPORT_ERROR_CODE,
            exc_info=(type(error), error, error.__traceback__),
        )
        self._log_event(
            event_type="source.import_failed",
            target_type="source",
            payload={
                "source_type": source_type,
                "title": _stored_label(title),
                "error_code": SOURCE_IMPORT_ERROR_CODE,
                "error_message": SOURCE_IMPORT_ERROR_MESSAGE,
                "metadata_json": cast(JsonObject, _label_event_value(metadata)),
            },
        )

    def capture_text(
        self,
        raw_text: str,
        *,
        title: str | None = None,
        domain: str = "unknown",
        sensitivity: str = "unknown",
        project_scope: tuple[str, ...] | None = None,
        metadata_json: JsonObject | None = None,
    ) -> CaptureResult:
        return self.capture_source(
            SourceCaptureInput(
                source_type="manual_text",
                title=title,
                raw_text=raw_text,
                domain=domain,
                sensitivity=sensitivity,
                project_scope=(tuple(project_scope) if project_scope is not None else None),
                metadata_json=metadata_json or {},
            )
        )

    def capture_file(
        self,
        path: str | Path,
        *,
        domain: str = "unknown",
        sensitivity: str = "unknown",
        metadata_json: JsonObject | None = None,
        max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES,
    ) -> CaptureResult:
        # The path is resolved once, as the importers resolve their source, and
        # the file is then opened once without following a link and read up to
        # ``max_file_bytes``. A file over the limit is refused before it is read.
        file_path = Path(path).expanduser().resolve()
        if file_path.suffix.casefold() not in SUPPORTED_TEXT_SUFFIXES:
            raise VNextCaptureValidationError(f"unsupported vNext text source type: {file_path.suffix}")
        raw_text = read_named_text_file(file_path, max_file_bytes=max_file_bytes)
        return self.capture_source(
            SourceCaptureInput(
                source_type="file",
                title=file_path.name,
                raw_text=raw_text,
                raw_path=str(file_path),
                connector_name="manual_file",
                external_id=str(file_path),
                domain=domain,
                sensitivity=sensitivity,
                metadata_json={
                    **(metadata_json or {}),
                    "filename": file_path.name,
                    "suffix": file_path.suffix.casefold(),
                },
            )
        )

    def capture_source(self, source_input: SourceCaptureInput) -> CaptureResult:
        # Before dedupe and before the try that logs a failure. A refusal
        # must not look up an existing row and must not persist the fields.
        # The commit door, not the floor alone: a low-entropy AKIA-shaped
        # key the floor treats as a placeholder is still refused.
        verdict = commit_door_fields_verdict(
            source_input.title,
            source_input.author,
            source_input.uri,
            source_input.raw_path,
            source_input.external_id,
            source_input.raw_text,
            source_input.metadata_json,
        )
        if verdict == VERDICT_EXPANSION:
            raise CaptureCredentialRefused(EXPANSION_REFUSED_MESSAGE)
        if verdict is not None:
            raise CaptureCredentialRefused(CREDENTIAL_MATERIAL_REFUSED_MESSAGE)
        try:
            normalized_text = normalize_text(source_input.raw_text)
            # Effective project scope, from the dedicated field with a
            # fallback to any scope already carried in metadata_json (the
            # connector path historically threads scope there). ``None``
            # preserves the legacy no-key shape; an explicit empty scope
            # persists ``project_scope: []`` so stale legacy aliases cannot
            # widen it later.
            project_scope_present = source_input.project_scope is not None or (
                "project_scope" in source_input.metadata_json
            )
            project_scope = normalize_project_scope(
                source_input.project_scope
                if source_input.project_scope is not None
                else source_input.metadata_json.get("project_scope")
            )
            project_scope_metadata: JsonObject = {"project_scope": list(project_scope)} if project_scope_present else {}
            # Public content identity stays text/project based. The internal
            # atomic dedupe identity additionally includes classification so an
            # exact recapture under a changed domain or sensitivity can retain
            # its own correctly classified source and candidate.
            content_hash = content_hash_for_text(normalized_text, project_scope)
            dedupe_key = capture_dedupe_key_for_text(
                normalized_text,
                project_scope,
                domain=source_input.domain,
                sensitivity=source_input.sensitivity,
            )
            duplicate = self._find_compatible_source(
                dedupe_key=dedupe_key,
                content_hash=content_hash,
                legacy_content_hash=content_hash_for_text(normalized_text),
                project_scope=project_scope,
                domain=source_input.domain,
                sensitivity=source_input.sensitivity,
            )
            if duplicate is not None:
                source_id = str(duplicate["id"])
                self._log_event(
                    event_type="source.duplicate_skipped",
                    target_type="source",
                    target_id=source_id,
                    payload={
                        "content_hash": content_hash,
                        "source_type": source_input.source_type,
                        "title": source_input.title,
                    },
                )
                return CaptureResult(
                    status="duplicate",
                    source_id=source_id,
                    content_hash=content_hash,
                    duplicate=True,
                )

            source_record: JsonObject = {
                "source_type": source_input.source_type,
                "title": source_input.title,
                "author": source_input.author,
                "uri": source_input.uri,
                "raw_path": source_input.raw_path,
                # ``content_hash`` remains the v0.9.4 public identity.  The
                # separately persisted dedupe key lets legacy pre-v0.9.4
                # scoped rows participate in atomic uniqueness after backfill.
                "content_hash": content_hash,
                "dedupe_key": dedupe_key,
                "captured_at": source_input.captured_at,
                "source_created_at": source_input.source_created_at,
                "source_modified_at": source_input.source_modified_at,
                "connector_name": source_input.connector_name,
                "external_id": source_input.external_id,
                "domain": source_input.domain,
                "sensitivity": source_input.sensitivity,
                "metadata_json": {
                    **source_input.metadata_json,
                    **project_scope_metadata,
                    "generated_by": self.actor_type,
                    "agent_identity": self.agent_identity,
                    "agent_id": self.actor_id if self.actor_type == "agent" else None,
                    "agent_run_id": self.run_id if self.actor_type == "agent" else None,
                    "trace_id": self.trace_id,
                    "policy_decision": self.policy_decision,
                    # Preserve and digest the exact evidence.  In particular,
                    # a scoped capture's raw digest must not equal its
                    # scope-folded identity merely because both use SHA-256.
                    "raw_text": source_input.raw_text,
                    "raw_text_sha256": raw_text_sha256(source_input.raw_text),
                },
            }
            get_or_create_source = getattr(self.store, "get_or_create_source", None)
            if callable(get_or_create_source):
                source, source_created = get_or_create_source(
                    source_record,
                    actor_type=self.actor_type,
                )
                if not source_created:
                    if not source_capture_identity_matches(
                        source,
                        content_hashes=(content_hash,),
                        project_scope=project_scope,
                        domain=source_input.domain,
                        sensitivity=source_input.sensitivity,
                    ):
                        raise ContinuityStoreInvariantError(
                            "atomic source dedupe winner does not match capture identity"
                        )
                    source_id = str(source["id"])
                    self._log_event(
                        event_type="source.duplicate_skipped",
                        target_type="source",
                        target_id=source_id,
                        payload={
                            "content_hash": content_hash,
                            "source_type": source_input.source_type,
                            "title": source_input.title,
                        },
                    )
                    return CaptureResult(
                        status="duplicate",
                        source_id=source_id,
                        content_hash=content_hash,
                        duplicate=True,
                    )
            else:
                source = self.store.create_source(source_record, actor_type=self.actor_type)
            source_id = str(source["id"])
            self._log_event(
                event_type="source.captured",
                target_type="source",
                target_id=source_id,
                payload={
                    "content_hash": content_hash,
                    "source_type": source_input.source_type,
                    "title": source_input.title,
                    "raw_preserved": True,
                },
            )

            chunk_rows: list[JsonObject] = []
            for chunk_index, chunk in enumerate(chunk_text(normalized_text, max_chars=self.chunk_max_chars)):
                chunk_row = self.store.create_source_chunk(
                    {
                        "source_id": source_id,
                        "chunk_index": chunk_index,
                        "text": chunk,
                        "token_count": len(chunk.split()),
                        "metadata_json": {"content_hash": content_hash},
                    },
                    actor_type=self.actor_type,
                )
                chunk_rows.append(chunk_row)

            self._log_event(
                event_type="source.chunked",
                target_type="source",
                target_id=source_id,
                payload={"content_hash": content_hash, "chunk_count": len(chunk_rows)},
            )

            extracted = (
                extract_candidate_memories(chunk_rows) if source_input.extract_candidates else []
            )
            candidates = self._drop_cross_batch_user_asserted_duplicates(
                extracted,
                project_scope=project_scope,
                domain=source_input.domain,
                sensitivity=source_input.sensitivity,
            )
            memory_rows: list[JsonObject] = []
            for candidate in candidates:
                # Speaker provenance is only stamped when a role was derived,
                # so provenance-free captures keep byte-identical metadata.
                provenance_metadata: JsonObject = (
                    {
                        "provenance_role": candidate.provenance_role,
                        "assertion_class": candidate.assertion_class,
                    }
                    if candidate.provenance_role is not None
                    else {}
                )
                memory = self.store.create_memory(
                    {
                        "memory_key": _memory_key(
                            content_hash=content_hash,
                            candidate=candidate,
                            domain=source_input.domain,
                            sensitivity=source_input.sensitivity,
                        ),
                        "value": {
                            "text": candidate.text,
                            "source_id": source_id,
                            "source_chunk_id": candidate.source_chunk_id,
                        },
                        "status": "candidate",
                        "source_event_ids": [source_id, candidate.source_chunk_id],
                        "memory_type": candidate.memory_type,
                        "confidence": candidate.confidence,
                        "title": _truncate(candidate.text, max_length=120),
                        "canonical_text": candidate.text,
                        "summary": _truncate(candidate.text, max_length=280),
                        "domain": source_input.domain,
                        "sensitivity": source_input.sensitivity,
                        "project_id": project_scope[0] if len(project_scope) == 1 else None,
                        "created_by_agent_id": self.actor_id if self.actor_type == "agent" else None,
                        "run_id": self.run_id if self.actor_type == "agent" else None,
                        "metadata_json": {
                            "source_id": source_id,
                            "source_chunk_id": candidate.source_chunk_id,
                            "source_chunk_index": candidate.source_chunk_index,
                            "extraction_rule": candidate.extraction_rule,
                            "capture_content_hash": content_hash,
                            **provenance_metadata,
                            **project_scope_metadata,
                            "generated_by": self.actor_type,
                            "agent_identity": self.agent_identity,
                            "agent_id": self.actor_id if self.actor_type == "agent" else None,
                            "agent_run_id": self.run_id if self.actor_type == "agent" else None,
                            "trace_id": self.trace_id,
                            "policy_decision": self.policy_decision,
                        },
                    },
                    actor_type=self.actor_type,
                )
                memory_rows.append(memory)
                self.store.create_provenance_link(
                    {
                        "target_type": "memory",
                        "target_id": str(memory["id"]),
                        "source_id": source_id,
                        "source_chunk_id": candidate.source_chunk_id,
                        "quote": candidate.text,
                        "evidence_role": "quoted_from",
                        "confidence": candidate.confidence,
                    },
                    actor_type=self.actor_type,
                )
                self._log_event(
                    event_type="memory.candidate_created",
                    target_type="memory",
                    target_id=str(memory["id"]),
                    payload={
                        "source_id": source_id,
                        "source_chunk_id": candidate.source_chunk_id,
                        "memory_type": candidate.memory_type,
                        "confidence": candidate.confidence,
                    },
                )

            deferred_embedding_inputs = tuple(DeferredMemoryEmbedding.from_memory(memory) for memory in memory_rows)
            if not self.defer_embeddings:
                attach_memory_embeddings(
                    self.store,
                    memory_rows,
                    actor_type=self.actor_type,
                    actor_id=self.actor_id,
                    trace_id=self.trace_id,
                )

            self._link_captured_entities(
                source=source,
                source_id=source_id,
                raw_text=normalized_text,
                sensitivity=source_input.sensitivity,
                memory_rows=memory_rows,
            )

            return CaptureResult(
                status="imported",
                source_id=source_id,
                content_hash=content_hash,
                chunk_count=len(chunk_rows),
                candidate_memory_count=len(candidates),
                deferred_embedding_inputs=(deferred_embedding_inputs if self.defer_embeddings else ()),
            )
        except Exception as exc:
            self._log_failure(
                source_type=source_input.source_type,
                title=source_input.title,
                error=exc,
                metadata=source_input.metadata_json,
            )
            raise

    def _find_compatible_source(
        self,
        *,
        dedupe_key: str,
        content_hash: str,
        legacy_content_hash: str,
        project_scope: tuple[str, ...],
        domain: str,
        sensitivity: str,
    ) -> JsonObject | None:
        """Find the same scoped and classified identity across legacy rows."""
        by_dedupe_key = getattr(self.store, "get_source_by_dedupe_key", None)
        if callable(by_dedupe_key):
            source = by_dedupe_key(dedupe_key)
            if source is not None:
                recomputed_key = capture_dedupe_key_for_source(source)
                if recomputed_key in (None, dedupe_key) and source_capture_identity_matches(
                    source,
                    content_hashes=(),
                    project_scope=project_scope,
                    domain=domain,
                    sensitivity=sensitivity,
                ):
                    return source

        many_by_hash = getattr(self.store, "get_sources_by_content_hash", None)
        for candidate_hash in dict.fromkeys((content_hash, legacy_content_hash)):
            if callable(many_by_hash):
                matches = many_by_hash(candidate_hash)
            else:
                match = self.store.get_source_by_content_hash(candidate_hash)
                matches = [] if match is None else [match]
            for source in matches:
                if source_capture_identity_matches(
                    source,
                    content_hashes=(content_hash, legacy_content_hash),
                    project_scope=project_scope,
                    domain=domain,
                    sensitivity=sensitivity,
                ):
                    return source
        return None

    def _drop_cross_batch_user_asserted_duplicates(
        self,
        candidates: list[CaptureCandidate],
        *,
        project_scope: tuple[str, ...],
        domain: str,
        sensitivity: str,
    ) -> list[CaptureCandidate]:
        """Dedupe user-asserted-value promotions against the whole store.

        ``extract_candidate_memories`` dedupes within one capture batch
        only, so a user restating the same value line in a later session
        used to mint a second memory with identical canonical text (proven
        cross-batch duplicate: one Omega-watch assertion captured twice
        from two sessions). Scoped to the ``user_asserted_value`` rule so
        every legacy rule keeps its batch-local behavior byte-identical.
        Current stores use an exact indexed lookup; legacy adapters retain the
        list fallback for compatibility.
        """
        if not any(candidate.extraction_rule == USER_ASSERTED_VALUE_RULE for candidate in candidates):
            return candidates
        find_live_memory = getattr(self.store, "find_live_memory_by_canonical_text", None)
        if callable(find_live_memory):
            return [
                candidate
                for candidate in candidates
                if candidate.extraction_rule != USER_ASSERTED_VALUE_RULE
                or find_live_memory(
                    candidate.text,
                    domain=domain,
                    sensitivity=sensitivity,
                    project_scope=project_scope,
                )
                is None
            ]
        list_memories = getattr(self.store, "list_memories", None)
        if not callable(list_memories):
            return candidates
        live_statuses = {"candidate", "active", "accepted", "needs_review", "private_only"}
        existing_texts = set()
        for row in list_memories():
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") not in live_statuses:
                continue
            if str(row.get("domain") or "unknown") != domain:
                continue
            if str(row.get("sensitivity") or "unknown") != sensitivity:
                continue
            row_scope = resolve_project_scope(row).identity
            if row_scope != project_scope_identity(project_scope):
                continue
            existing_texts.add(str(row.get("canonical_text") or "").casefold())
        existing_texts.discard("")
        return [
            candidate
            for candidate in candidates
            if candidate.extraction_rule != USER_ASSERTED_VALUE_RULE or candidate.text.casefold() not in existing_texts
        ]

    def _link_captured_entities(
        self,
        *,
        source: JsonObject,
        source_id: str,
        raw_text: str,
        sensitivity: str,
        memory_rows: list[JsonObject],
    ) -> None:
        """Best-effort deterministic entity linking after a successful capture.

        - Sensitivity gate: sources at or above 'private' skip extraction
          entirely, because entity rows leak content into ``entities.name``
          (a person/org name IS content).
        - Optional surface: stores without the entity substrate skip
          silently, mirroring how ``attach_memory_embedding`` degrades.
        - Failure isolation: extraction/link errors NEVER fail capture;
          they are logged as ``entity.extraction_failed`` and capture
          continues, mirroring embedding attach failures.
        """
        if str(sensitivity).casefold() in ENTITY_EXTRACTION_SKIP_SENSITIVITIES:
            return
        if not store_supports_entity_linking(self.store):
            return
        # Event time for entity observations and mention edges: the
        # source's own timestamp, falling back to ingestion time.
        observed_at = (
            source.get("source_created_at")
            or source.get("captured_at")
            or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        )
        try:
            linker = EntityLinkingService(
                self.store,
                actor_type=self.actor_type,
                actor_id=self.actor_id,
                trace_id=self.trace_id,
            )
            linker.link_entities_for_source(source_id=source_id, text=raw_text, observed_at=observed_at)
            for memory_row in memory_rows:
                text = str(memory_row.get("canonical_text") or "")
                if text.strip():
                    linker.link_entities_for_memory(
                        memory_id=str(memory_row["id"]),
                        text=text,
                        observed_at=observed_at,
                    )
        except Exception as exc:
            logger.exception(
                "entity extraction failed source_id=%s error_code=%s",
                source_id,
                ENTITY_EXTRACTION_ERROR_CODE,
            )
            self._log_event(
                event_type="entity.extraction_failed",
                target_type="source",
                target_id=source_id,
                payload={
                    "stage": "capture",
                    "error_code": ENTITY_EXTRACTION_ERROR_CODE,
                    "error_message": ENTITY_EXTRACTION_ERROR_MESSAGE,
                },
            )

    def import_markdown_folder(
        self,
        folder: str | Path,
        *,
        domain: str = "unknown",
        sensitivity: str = "unknown",
        max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES,
    ) -> BatchImportResult:
        # Same selection and one-shot read as the legacy markdown importer:
        # no symlink outside the root, and a single file is allowed.
        # The per-unit filter runs first. capture_source then refuses whatever
        # that filter could not isolate. A file over ``max_file_bytes`` refuses
        # the whole import before it is read, as a file that is not UTF-8 does.
        from alicebot_api.markdown_import import MarkdownImportValidationError, _snapshot_markdown_source

        _refuse_flagged_import_folder(folder)
        try:
            folder_path, snapshot = _snapshot_markdown_source(folder, max_file_bytes=max_file_bytes)
        except ImportFileTooLargeError as exc:
            raise _public_too_large_error(exc) from exc
        except MarkdownImportValidationError as exc:
            raise _public_markdown_import_error(exc) from exc

        source_ids: list[str] = []
        errors: list[str] = []
        duplicate_count = 0
        failed_count = 0
        skipped_count = 0
        credential_items: list[str] = []
        run_hashes: set[str] = set()
        deferred_embedding_inputs: list[DeferredMemoryEmbedding] = []

        for file_index, source_file in enumerate(snapshot, start=1):
            file_path = source_file.path
            raw_text, file_skips = _filter_markdown_units(
                source_file.text,
                file_index=file_index,
                file_name=file_path.name,
            )
            credential_items.extend(file_skips)
            try:
                content_hash = content_hash_for_text(raw_text)
                if content_hash in run_hashes:
                    duplicate_count += 1
                    self._log_event(
                        event_type="source.duplicate_skipped",
                        target_type="source",
                        payload={
                            "content_hash": content_hash,
                            "source_type": "markdown",
                            "duplicate_scope": "batch",
                        },
                    )
                    continue
                run_hashes.add(content_hash)

                result = self.capture_source(
                    SourceCaptureInput(
                        source_type="markdown",
                        title=file_path.stem,
                        raw_text=raw_text,
                        raw_path=str(file_path),
                        connector_name="markdown_folder",
                        external_id=source_file.relative_path,
                        domain=domain,
                        sensitivity=sensitivity,
                        extract_candidates=False,
                        metadata_json={
                            "folder": str(folder_path),
                            "relative_path": source_file.relative_path,
                        },
                    )
                )
                deferred_embedding_inputs.extend(result.deferred_embedding_inputs)
                if result.duplicate:
                    duplicate_count += 1
                    continue
                if result.source_id is not None:
                    source_ids.append(result.source_id)
            except CaptureCredentialRefused:
                # The file is skipped. The receipt names it without a token.
                skipped_count += 1
                _remember_item(credential_items, _file_phrase(file_index, file_path.name))
                continue
            except Exception as exc:
                failed_count += 1
                errors.append(SOURCE_IMPORT_ERROR_MESSAGE)
                self._log_failure(
                    source_type="markdown",
                    title=file_path.name,
                    error=exc,
                    metadata={"folder": str(folder_path), "title": file_path.name},
                )

        imported_count = len(source_ids)
        status = _batch_status(
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

        self._log_event(
            event_type="source.batch_import_completed",
            target_type="source",
            payload={
                "source_type": "markdown",
                "folder": _stored_label(str(folder_path)),
                "imported_count": imported_count,
                "duplicate_count": duplicate_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
            },
        )
        return BatchImportResult(
            status=status,
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            skipped_credentials=len(credential_items),
            skipped_credential_items=tuple(credential_items),
            source_ids=tuple(source_ids),
            errors=tuple(errors),
            error_code=SOURCE_IMPORT_ERROR_CODE if failed_count else None,
            deferred_embedding_inputs=tuple(deferred_embedding_inputs),
        )

    def import_chatgpt_export_file(
        self,
        path: str | Path,
        *,
        domain: str = "personal",
        sensitivity: str = "private",
        max_file_bytes: int = DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    ) -> BatchImportResult:
        # Same selection and one-shot read as the legacy ChatGPT importer.
        # A file with no conversations is refused. It is not stored. A file
        # over ``max_file_bytes`` is refused before it is read, and so is a file
        # that is not JSON. A single conversation that cannot be turned into a
        # transcript is counted as failed and named by its position, and the
        # others still import, as a capture that fails for one conversation does.
        from alicebot_api.chatgpt_import import ChatGPTImportValidationError, _snapshot_chatgpt_source
        from alicebot_api.importer_paths import ImportSourceFile

        _refuse_flagged_import_folder(path)
        try:
            export_path, snapshot = _snapshot_chatgpt_source(path, max_file_bytes=max_file_bytes)
        except ImportFileTooLargeError as exc:
            raise _public_too_large_error(exc) from exc
        except ChatGPTImportValidationError as exc:
            message = str(exc)
            if _unit_verdict(message) is not None:
                raise VNextCaptureValidationError("The import path is withheld") from exc
            raise VNextCaptureValidationError(message) from exc

        credential_items: list[str] = []
        parsed_files: list[
            tuple[ImportSourceFile, str, list[_ChatGPTConversationTranscript | _ChatGPTConversationRefused]]
        ] = []
        for source_file in snapshot:
            try:
                payload = json.loads(source_file.text)
            except json.JSONDecodeError as exc:
                raise VNextCaptureValidationError("ChatGPT export is not valid JSON") from exc
            except RecursionError as exc:
                # Arrays or objects nested past the decoder's depth. A real
                # export nests a few levels and spreads a conversation over a
                # mapping, so this is a file that is not an export.
                raise VNextCaptureValidationError("ChatGPT export is nested too deeply to read") from exc
            conversations = _chatgpt_conversations(payload)
            if not conversations:
                raise VNextCaptureValidationError("ChatGPT export has no conversations")
            transcripts: list[_ChatGPTConversationTranscript | _ChatGPTConversationRefused] = []
            for index, conversation in enumerate(conversations, start=1):
                conversation_skips: list[str] = []
                try:
                    built = _chatgpt_conversation_transcript(
                        conversation,
                        index=index,
                        credential_skips=conversation_skips,
                    )
                except MemoryError:
                    # Not a fact about this conversation. The process is out of
                    # memory, so the next conversation would fail the same way, and
                    # calling this one unreadable would hide that and import a
                    # partial export as if it were complete.
                    raise
                except Exception as exc:
                    # Nothing is written in this pass, so the refusal is only
                    # recorded here and counted with the writes below. The log gets
                    # one line with the position and the kind of error, and never a
                    # traceback or the error's own text, which can quote the export.
                    # The traceback is there at debug level for whoever asks for it.
                    logger.warning(
                        "ChatGPT conversation could not be read conversation_index=%d error_code=%s error_type=%s",
                        index,
                        CHATGPT_CONVERSATION_UNREADABLE_CODE,
                        type(exc).__name__,
                    )
                    logger.debug(
                        "ChatGPT conversation traceback conversation_index=%d",
                        index,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    transcripts.append(_ChatGPTConversationRefused(index=index))
                    continue
                # Skips from a conversation that failed half way are dropped
                # with it, so the receipt never names a unit of a conversation
                # it did not read.
                for skip in conversation_skips:
                    _remember_item(credential_items, skip)
                transcripts.append(built)
            export_sha256 = "sha256:" + sha256(source_file.text.encode("utf-8")).hexdigest()
            parsed_files.append((source_file, export_sha256, transcripts))

        captured_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        source_ids: list[str] = []
        errors: list[str] = []
        duplicate_count = 0
        failed_count = 0
        skipped_count = 0
        deferred_embedding_inputs: list[DeferredMemoryEmbedding] = []
        conversation_count = 0
        for file_index, (source_file, export_sha256, transcripts) in enumerate(parsed_files, start=1):
            conversation_count += len(transcripts)
            file_path = source_file.path
            for transcript in transcripts:
                if isinstance(transcript, _ChatGPTConversationRefused):
                    failed_count += 1
                    errors.append(
                        f"conversation {transcript.index} refused: {CHATGPT_CONVERSATION_UNREADABLE_CODE}"
                    )
                    self._log_event(
                        event_type="source.import_failed",
                        target_type="source",
                        payload={
                            "source_type": "chatgpt_export",
                            "title": None,
                            "error_code": CHATGPT_CONVERSATION_UNREADABLE_CODE,
                            "error_message": CHATGPT_CONVERSATION_UNREADABLE_MESSAGE,
                            "metadata_json": cast(
                                JsonObject,
                                _label_event_value(
                                    {"filename": file_path.name, "conversation_index": transcript.index}
                                ),
                            ),
                        },
                    )
                    continue
                try:
                    result = self.capture_source(
                        SourceCaptureInput(
                            source_type="chatgpt_export",
                            title=transcript.title,
                            raw_text=transcript.raw_text,
                            raw_path=str(file_path),
                            connector_name="chatgpt_export",
                            external_id=transcript.external_id,
                            domain=domain,
                            sensitivity=sensitivity,
                            captured_at=captured_at,
                            source_created_at=transcript.created_at,
                            source_modified_at=transcript.modified_at,
                            extract_candidates=False,
                            metadata_json={
                                "filename": file_path.name,
                                "export_sha256": export_sha256,
                                "transcript_format": "chatgpt_conversation_v1",
                                "export_conversation_count": len(transcripts),
                                "conversation_index": transcript.index,
                                "conversation_id": transcript.external_id,
                                "conversation_title": transcript.title,
                                "message_count": transcript.message_count,
                            },
                        )
                    )
                except CaptureCredentialRefused:
                    # A refused conversation is skipped, not failed.
                    skipped_count += 1
                    if _unit_verdict(file_path.name) is not None:
                        _remember_item(credential_items, _file_phrase(file_index, file_path.name))
                    else:
                        _remember_item(
                            credential_items,
                            _conversation_phrase(
                                _printed_label(transcript.external_id),
                                transcript.message_count or transcript.index,
                            ),
                        )
                    continue
                except Exception as exc:
                    failed_count += 1
                    errors.append(SOURCE_IMPORT_ERROR_MESSAGE)
                    logger.exception(
                        "ChatGPT conversation import failed conversation_index=%d error_code=%s",
                        transcript.index,
                        SOURCE_IMPORT_ERROR_CODE,
                    )
                    self._log_failure(
                        source_type="chatgpt_export",
                        title=transcript.title,
                        error=exc,
                        metadata={"filename": file_path.name, "title": transcript.title},
                    )
                    continue
                deferred_embedding_inputs.extend(result.deferred_embedding_inputs)
                if result.duplicate:
                    duplicate_count += 1
                elif result.source_id is not None:
                    source_ids.append(result.source_id)

        imported_count = len(source_ids)
        status = _batch_status(
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

        self._log_event(
            event_type="source.batch_import_completed",
            target_type="source",
            payload={
                "source_type": "chatgpt_export",
                "filename": _stored_label(export_path.name),
                "conversation_count": conversation_count,
                "imported_count": imported_count,
                "duplicate_count": duplicate_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
            },
        )
        return BatchImportResult(
            status=status,
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
            skipped_credentials=len(credential_items),
            skipped_credential_items=tuple(credential_items),
            source_ids=tuple(source_ids),
            errors=tuple(errors),
            error_code=SOURCE_IMPORT_ERROR_CODE if failed_count else None,
            deferred_embedding_inputs=tuple(deferred_embedding_inputs),
        )


__all__ = [
    "BatchImportResult",
    "CaptureCandidate",
    "CaptureCredentialRefused",
    "CaptureResult",
    "SourceCaptureInput",
    "USER_ASSERTED_VALUE_CONFIDENCE",
    "USER_ASSERTED_VALUE_RULE",
    "VNextCaptureService",
    "VNextCaptureStore",
    "VNextCaptureValidationError",
    "candidate_promotion_rank",
    "capture_content_hash_for_source",
    "capture_dedupe_key_for_source",
    "capture_dedupe_key_for_text",
    "chunk_text",
    "content_hash_for_text",
    "extract_candidate_memories",
    "normalize_text",
    "order_candidates_for_promotion",
    "source_capture_raw_text",
]
