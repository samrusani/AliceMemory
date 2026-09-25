from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from alicebot_api.continuity_objects import (
    create_continuity_object_record,
    get_continuity_object_for_capture_event,
    list_continuity_objects_for_capture_events,
)
from alicebot_api.contracts import (
    CONTINUITY_CAPTURE_ASSIST_AUTOSAVE_TYPES,
    CONTINUITY_CAPTURE_CANDIDATE_TYPES,
    CONTINUITY_CAPTURE_COMMIT_MODES,
    CONTINUITY_CAPTURE_EXPLICIT_SIGNALS,
    CONTINUITY_CAPTURE_LIST_ORDER,
    CONTINUITY_CAPTURE_REVIEW_REQUIRED_TYPES,
    DEFAULT_CONTINUITY_CAPTURE_LIMIT,
    MAX_CONTINUITY_CAPTURE_LIMIT,
    ContinuityCaptureCandidateRecord,
    ContinuityCaptureCandidateType,
    ContinuityCaptureCandidatesInput,
    ContinuityCaptureCandidatesResponse,
    ContinuityCaptureCandidatesSummary,
    ContinuityCaptureAdmissionPosture,
    ContinuityCaptureCommitDecision,
    ContinuityCaptureCommitInput,
    ContinuityCaptureCommitMode,
    ContinuityCaptureCommitRecord,
    ContinuityCaptureCommitResponse,
    ContinuityCaptureCommitSummary,
    ContinuityCaptureCreateInput,
    ContinuityCaptureCreateResponse,
    ContinuityCaptureDetailResponse,
    ContinuityCaptureEventRecord,
    ContinuityCaptureExplicitSignal,
    ContinuityCaptureInboxItem,
    ContinuityCaptureInboxResponse,
    ContinuityCaptureProposedAction,
    ContinuityObjectRecord,
    ContinuityObjectType,
    MemoryTrustClass,
)
from alicebot_api.store import ContinuityCaptureEventRow, ContinuityStore, JsonObject
from alicebot_api.write_bounds import MAX_CAPTURE_CANDIDATE_CHARS, MAX_CAPTURE_COMMIT_CANDIDATES, serialized_chars


class ContinuityCaptureValidationError(ValueError):
    """Raised when a continuity capture request is invalid."""


class ContinuityCaptureNotFoundError(LookupError):
    """Raised when a continuity capture event is not visible in scope."""


@dataclass(frozen=True, slots=True)
class DerivedObjectDecision:
    object_type: ContinuityObjectType
    normalized_text: str
    confidence: float
    admission_reason: str


@dataclass(frozen=True, slots=True)
class ExtractedCandidate:
    candidate_type: ContinuityCaptureCandidateType
    object_type: ContinuityObjectType | None
    normalized_text: str
    confidence: float
    trust_class: MemoryTrustClass
    evidence_snippet: str
    explicit: bool
    source_role: str
    admission_reason: str


_CANDIDATE_TYPES: tuple[ContinuityCaptureCandidateType, ...] = (
    "decision",
    "commitment",
    "waiting_for",
    "blocker",
    "preference",
    "correction",
    "note",
    "no_op",
)
_OBJECT_TYPES: tuple[ContinuityObjectType, ...] = (
    "Note",
    "MemoryFact",
    "Decision",
    "Commitment",
    "WaitingFor",
    "Blocker",
    "NextAction",
)

_EXPLICIT_SIGNAL_TO_OBJECT_TYPE: dict[ContinuityCaptureExplicitSignal, ContinuityObjectType] = {
    "remember_this": "MemoryFact",
    "task": "NextAction",
    "decision": "Decision",
    "commitment": "Commitment",
    "waiting_for": "WaitingFor",
    "blocker": "Blocker",
    "next_action": "NextAction",
    "note": "Note",
}

_HIGH_CONFIDENCE_PREFIXES: tuple[tuple[str, ContinuityObjectType, str], ...] = (
    ("decision:", "Decision", "high_confidence_prefix_decision"),
    ("task:", "NextAction", "high_confidence_prefix_task"),
    ("todo:", "NextAction", "high_confidence_prefix_todo"),
    ("next:", "NextAction", "high_confidence_prefix_next_action"),
    ("commitment:", "Commitment", "high_confidence_prefix_commitment"),
    ("waiting for:", "WaitingFor", "high_confidence_prefix_waiting_for"),
    ("blocker:", "Blocker", "high_confidence_prefix_blocker"),
    ("remember:", "MemoryFact", "high_confidence_prefix_remember"),
    ("fact:", "MemoryFact", "high_confidence_prefix_fact"),
    ("note:", "Note", "high_confidence_prefix_note"),
)

_CANDIDATE_PREFIX_RULES: tuple[
    tuple[str, ContinuityCaptureCandidateType, ContinuityObjectType | None, str], ...
] = (
    ("decision:", "decision", "Decision", "explicit_prefix_decision"),
    ("commitment:", "commitment", "Commitment", "explicit_prefix_commitment"),
    ("waiting for:", "waiting_for", "WaitingFor", "explicit_prefix_waiting_for"),
    ("waiting on:", "waiting_for", "WaitingFor", "explicit_prefix_waiting_for"),
    ("blocker:", "blocker", "Blocker", "explicit_prefix_blocker"),
    ("preference:", "preference", "MemoryFact", "explicit_prefix_preference"),
    ("correction:", "correction", "Note", "explicit_prefix_correction"),
    ("correct:", "correction", "Note", "explicit_prefix_correction"),
    ("note:", "note", "Note", "explicit_prefix_note"),
)

_PREFIX_ADMISSION_REASONS = frozenset(reason for _prefix, _candidate_type, _object_type, reason in _CANDIDATE_PREFIX_RULES)

_CANDIDATE_REGEX_RULES: tuple[
    tuple[
        re.Pattern[str],
        ContinuityCaptureCandidateType,
        ContinuityObjectType | None,
        str,
        float,
    ],
    ...,
] = (
    (
        re.compile(r"\b(i prefer|i like|i don't like|remember that i prefer)\b", re.IGNORECASE),
        "preference",
        "MemoryFact",
        "explicit_phrase_preference",
        0.9,
    ),
    (
        re.compile(r"\b(decision|we decided|i decided|decided that)\b", re.IGNORECASE),
        "decision",
        "Decision",
        "explicit_phrase_decision",
        0.9,
    ),
    (
        re.compile(r"\b(i will|we will|i'll|we'll|i need to|remind me to)\b", re.IGNORECASE),
        "commitment",
        "Commitment",
        "explicit_phrase_commitment",
        0.9,
    ),
    (
        re.compile(r"\b(waiting for|waiting on|awaiting)\b", re.IGNORECASE),
        "waiting_for",
        "WaitingFor",
        "explicit_phrase_waiting_for",
        0.86,
    ),
    (
        re.compile(r"\b(blocked|blocker|cannot proceed|can't proceed)\b", re.IGNORECASE),
        "blocker",
        "Blocker",
        "explicit_phrase_blocker",
        0.86,
    ),
    (
        re.compile(r"\b(correction|actually|instead|update)\b", re.IGNORECASE),
        "correction",
        "Note",
        "explicit_phrase_correction",
        0.9,
    ),
)

_ACK_ONLY_TURNS: set[str] = {
    "ok",
    "okay",
    "thanks",
    "thank you",
    "sounds good",
    "great",
    "got it",
    "understood",
}


def _normalize_content(raw_content: str) -> str:
    return re.sub(r"\s+", " ", raw_content).strip()


def _truncate(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."


def _title_for_object_type(object_type: str, normalized_text: str) -> str:
    if object_type == "Decision":
        prefix = "Decision"
    elif object_type == "Commitment":
        prefix = "Commitment"
    elif object_type == "WaitingFor":
        prefix = "Waiting For"
    elif object_type == "Blocker":
        prefix = "Blocker"
    elif object_type == "NextAction":
        prefix = "Next Action"
    elif object_type == "MemoryFact":
        prefix = "Memory Fact"
    else:
        prefix = "Note"
    return _truncate(f"{prefix}: {normalized_text}", max_length=280)


def _body_for_object_type(
    *,
    object_type: str,
    normalized_text: str,
    raw_content: str,
    explicit_signal: str | None,
) -> JsonObject:
    key_by_type = {
        "Note": "body",
        "MemoryFact": "fact_text",
        "Decision": "decision_text",
        "Commitment": "commitment_text",
        "WaitingFor": "waiting_for_text",
        "Blocker": "blocking_reason",
        "NextAction": "action_text",
    }
    value_key = key_by_type[object_type]
    payload: JsonObject = {
        value_key: normalized_text,
        "raw_content": raw_content,
    }
    payload["explicit_signal"] = explicit_signal
    return payload


def _resolve_explicit_signal_decision(
    *,
    explicit_signal: ContinuityCaptureExplicitSignal,
    normalized_text: str,
) -> DerivedObjectDecision:
    object_type = _EXPLICIT_SIGNAL_TO_OBJECT_TYPE[explicit_signal]
    return DerivedObjectDecision(
        object_type=object_type,
        normalized_text=normalized_text,
        confidence=1.0,
        admission_reason=f"explicit_signal_{explicit_signal}",
    )


def _resolve_high_confidence_decision(normalized_text: str) -> DerivedObjectDecision | None:
    lower = normalized_text.casefold()

    for prefix, object_type, reason in _HIGH_CONFIDENCE_PREFIXES:
        if not lower.startswith(prefix):
            continue

        stripped = _normalize_content(normalized_text[len(prefix) :])
        if not stripped:
            return None

        return DerivedObjectDecision(
            object_type=object_type,
            normalized_text=stripped,
            confidence=0.95,
            admission_reason=reason,
        )

    return None


def _serialize_capture_event(row: ContinuityCaptureEventRow) -> ContinuityCaptureEventRecord:
    explicit_signal = row["explicit_signal"]
    if explicit_signal is not None and explicit_signal not in _EXPLICIT_SIGNAL_TO_OBJECT_TYPE:
        raise ValueError(f"unsupported stored explicit signal: {explicit_signal}")
    admission_posture = row["admission_posture"]
    if admission_posture not in {"DERIVED", "TRIAGE"}:
        raise ValueError(f"unsupported stored admission posture: {admission_posture}")
    return {
        "id": str(row["id"]),
        "raw_content": row["raw_content"],
        "explicit_signal": cast(ContinuityCaptureExplicitSignal | None, explicit_signal),
        "admission_posture": cast(ContinuityCaptureAdmissionPosture, admission_posture),
        "admission_reason": row["admission_reason"],
        "created_at": row["created_at"].isoformat(),
    }


def _build_inbox_item(
    capture_event: ContinuityCaptureEventRecord,
    derived_object: ContinuityObjectRecord | None,
) -> ContinuityCaptureInboxItem:
    return {
        "capture_event": capture_event,
        "derived_object": derived_object,
    }


def _validate_capture_limit(limit: int) -> None:
    if limit < 1 or limit > MAX_CONTINUITY_CAPTURE_LIMIT:
        raise ContinuityCaptureValidationError(
            f"limit must be between 1 and {MAX_CONTINUITY_CAPTURE_LIMIT}"
        )


def _candidate_id(*, candidate_type: str, normalized_text: str, source_role: str) -> str:
    encoded = f"{candidate_type}|{normalized_text}|{source_role}".encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _user_prefix_autosave(
    *,
    explicit: bool,
    candidate_type: str,
    confidence: float,
    source_role: str,
    admission_reason: str,
) -> bool:
    """Auto-save only a user turn that used an explicit prefix rule.

    Regex hits set ``explicit`` too. Assistant turns do as well. Neither
    is an instruction from the user to save the line.
    """

    return (
        source_role == "user"
        and explicit
        and candidate_type in CONTINUITY_CAPTURE_ASSIST_AUTOSAVE_TYPES
        and admission_reason in _PREFIX_ADMISSION_REASONS
        and confidence >= 0.9
    )


def _derive_trust_class(*, explicit: bool, confidence: float) -> MemoryTrustClass:
    if explicit and confidence >= 0.9:
        return "deterministic"
    if confidence >= 0.85:
        return "llm_corroborated"
    return "llm_single_source"


def _build_candidate_record(candidate: ExtractedCandidate) -> ContinuityCaptureCandidateRecord:
    candidate_id = _candidate_id(
        candidate_type=candidate.candidate_type,
        normalized_text=candidate.normalized_text,
        source_role=candidate.source_role,
    )
    if candidate.candidate_type == "no_op":
        proposed_action: ContinuityCaptureProposedAction = "no_op"
    elif _user_prefix_autosave(
        explicit=candidate.explicit,
        candidate_type=candidate.candidate_type,
        confidence=candidate.confidence,
        source_role=candidate.source_role,
        admission_reason=candidate.admission_reason,
    ):
        proposed_action = "auto_save_candidate"
    else:
        proposed_action = "queue_for_review"

    return {
        "candidate_id": candidate_id,
        "candidate_type": candidate.candidate_type,
        "object_type": candidate.object_type,
        "normalized_text": candidate.normalized_text,
        "confidence": candidate.confidence,
        "trust_class": candidate.trust_class,
        "evidence_snippet": candidate.evidence_snippet,
        "explicit": candidate.explicit,
        "source_role": candidate.source_role,
        "admission_reason": candidate.admission_reason,
        "proposed_action": proposed_action,
    }


def _extract_from_role(*, text: str, source_role: str) -> ExtractedCandidate | None:
    normalized = _normalize_content(text)
    if normalized == "":
        return None

    lowered = normalized.casefold()
    for prefix, candidate_type, object_type, reason in _CANDIDATE_PREFIX_RULES:
        if not lowered.startswith(prefix):
            continue
        stripped = _normalize_content(normalized[len(prefix) :])
        if stripped == "":
            return None
        confidence = 0.98
        return ExtractedCandidate(
            candidate_type=candidate_type,
            object_type=object_type,
            normalized_text=stripped,
            confidence=confidence,
            trust_class=_derive_trust_class(explicit=True, confidence=confidence),
            evidence_snippet=_truncate(stripped, max_length=220),
            explicit=True,
            source_role=source_role,
            admission_reason=reason,
        )

    for pattern, candidate_type, object_type, reason, confidence in _CANDIDATE_REGEX_RULES:
        match = pattern.search(normalized)
        if match is None:
            continue
        return ExtractedCandidate(
            candidate_type=candidate_type,
            object_type=object_type,
            normalized_text=normalized,
            confidence=confidence,
            trust_class=_derive_trust_class(explicit=True, confidence=confidence),
            evidence_snippet=_truncate(match.group(0), max_length=220),
            explicit=True,
            source_role=source_role,
            admission_reason=reason,
        )

    return None


def _is_ack_only_turn(*, user_text: str, assistant_text: str) -> bool:
    normalized_user = _normalize_content(user_text).casefold()
    normalized_assistant = _normalize_content(assistant_text).casefold()
    if normalized_user == "" and normalized_assistant == "":
        return True
    if normalized_user in _ACK_ONLY_TURNS and normalized_assistant in _ACK_ONLY_TURNS:
        return True
    if normalized_user == "" and normalized_assistant in _ACK_ONLY_TURNS:
        return True
    if normalized_assistant == "" and normalized_user in _ACK_ONLY_TURNS:
        return True
    return False


def _no_op_candidate(*, user_text: str, assistant_text: str) -> ContinuityCaptureCandidateRecord:
    evidence = _normalize_content(" ".join(part for part in [user_text, assistant_text] if part))
    candidate = ExtractedCandidate(
        candidate_type="no_op",
        object_type=None,
        normalized_text="",
        confidence=1.0,
        trust_class="deterministic",
        evidence_snippet=_truncate(evidence, max_length=220),
        explicit=False,
        source_role="combined",
        admission_reason="turn_no_actionable_capture",
    )
    return _build_candidate_record(candidate)


def _object_type_for_candidate(
    candidate_type: ContinuityCaptureCandidateType,
    object_type: ContinuityObjectType | None,
) -> ContinuityObjectType:
    if object_type is not None:
        return object_type

    fallback_map: dict[ContinuityCaptureCandidateType, ContinuityObjectType] = {
        "decision": "Decision",
        "commitment": "Commitment",
        "waiting_for": "WaitingFor",
        "blocker": "Blocker",
        "preference": "MemoryFact",
        "correction": "Note",
        "note": "Note",
    }
    return fallback_map.get(candidate_type, "Note")


def _explicit_signal_for_candidate(
    candidate_type: ContinuityCaptureCandidateType,
) -> ContinuityCaptureExplicitSignal | None:
    signal_map: dict[ContinuityCaptureCandidateType, ContinuityCaptureExplicitSignal] = {
        "decision": "decision",
        "commitment": "commitment",
        "waiting_for": "waiting_for",
        "blocker": "blocker",
        "preference": "remember_this",
        "correction": "note",
        "note": "note",
    }
    return signal_map.get(candidate_type)


def _body_for_candidate(
    *,
    candidate: ContinuityCaptureCandidateRecord,
    object_type: ContinuityObjectType,
) -> JsonObject:
    normalized_text = candidate["normalized_text"]
    candidate_type = candidate["candidate_type"]

    if object_type == "Decision":
        key = "decision_text"
    elif object_type == "Commitment":
        key = "commitment_text"
    elif object_type == "WaitingFor":
        key = "waiting_for_text"
    elif object_type == "Blocker":
        key = "blocking_reason"
    elif object_type == "NextAction":
        key = "action_text"
    elif object_type == "MemoryFact":
        key = "fact_text"
    else:
        key = "body"

    payload: JsonObject = {
        key: normalized_text,
        "candidate_type": candidate_type,
        "explicit": candidate["explicit"],
        "evidence_snippet": candidate["evidence_snippet"],
    }
    if candidate_type == "correction":
        payload["correction_text"] = normalized_text
    if candidate_type == "preference":
        payload["preference_text"] = normalized_text
    return payload


def _normalize_commit_mode(mode: str) -> ContinuityCaptureCommitMode:
    normalized = mode.strip().lower()
    if normalized not in CONTINUITY_CAPTURE_COMMIT_MODES:
        allowed = ", ".join(CONTINUITY_CAPTURE_COMMIT_MODES)
        raise ContinuityCaptureValidationError(f"mode must be one of: {allowed}")
    return cast(ContinuityCaptureCommitMode, normalized)


def _normalize_candidate(payload: JsonObject) -> ContinuityCaptureCandidateRecord:
    candidate_type = str(payload.get("candidate_type", "")).strip().lower()
    if candidate_type not in _CANDIDATE_TYPES:
        allowed = ", ".join(CONTINUITY_CAPTURE_CANDIDATE_TYPES)
        raise ContinuityCaptureValidationError(f"candidate_type must be one of: {allowed}")

    raw_object_type = payload.get("object_type")
    if raw_object_type is None:
        object_type: ContinuityObjectType | None = None
    elif isinstance(raw_object_type, str) and raw_object_type.strip() != "":
        normalized_object_type = raw_object_type.strip()
        if normalized_object_type not in _OBJECT_TYPES:
            raise ContinuityCaptureValidationError("object_type is not supported")
        object_type = cast(ContinuityObjectType, normalized_object_type)
    else:
        raise ContinuityCaptureValidationError("object_type must be a string when provided")

    normalized_text = _normalize_content(str(payload.get("normalized_text", "")))
    if candidate_type != "no_op" and normalized_text == "":
        raise ContinuityCaptureValidationError("normalized_text must not be empty for non-no-op candidates")

    raw_confidence = payload.get("confidence", 0.0)
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (str, int, float)):
        raise ContinuityCaptureValidationError("confidence must be a number")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError) as exc:
        raise ContinuityCaptureValidationError("confidence must be a number") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise ContinuityCaptureValidationError("confidence must be between 0.0 and 1.0")

    raw_explicit = payload.get("explicit", False)
    if not isinstance(raw_explicit, bool):
        raise ContinuityCaptureValidationError("explicit must be a boolean")
    explicit = raw_explicit
    source_role = _normalize_content(str(payload.get("source_role", "combined"))) or "combined"
    admission_reason = _normalize_content(str(payload.get("admission_reason", "derived_candidate")))
    evidence_snippet = _normalize_content(str(payload.get("evidence_snippet", normalized_text)))

    candidate = ExtractedCandidate(
        candidate_type=cast(ContinuityCaptureCandidateType, candidate_type),
        object_type=object_type,
        normalized_text=normalized_text,
        confidence=confidence,
        trust_class=_derive_trust_class(explicit=explicit, confidence=confidence),
        evidence_snippet=evidence_snippet,
        explicit=explicit,
        source_role=source_role,
        admission_reason=admission_reason,
    )
    return _build_candidate_record(candidate)


def _resolve_commit_decision(
    *,
    candidate: ContinuityCaptureCandidateRecord,
    mode: ContinuityCaptureCommitMode,
) -> tuple[ContinuityCaptureCommitDecision, str, str]:
    candidate_type = candidate["candidate_type"]
    confidence = candidate["confidence"]
    explicit = candidate["explicit"]

    if candidate_type == "no_op":
        return "no_op", "no_actionable_candidate", "none"

    if mode == "manual":
        return "queued_for_review", "manual_mode_requires_review", "review_queue"

    if candidate_type in CONTINUITY_CAPTURE_REVIEW_REQUIRED_TYPES:
        return "queued_for_review", "type_requires_review", "review_queue"

    if mode in {"assist", "auto"}:
        if _user_prefix_autosave(
            explicit=explicit,
            candidate_type=candidate_type,
            confidence=confidence,
            source_role=candidate["source_role"],
            admission_reason=candidate["admission_reason"],
        ):
            return "auto_saved", "user_explicit_prefix_rule", "continuity_objects"
        gate = "assist_mode_review_gate" if mode == "assist" else "auto_mode_review_gate"
        return "queued_for_review", gate, "review_queue"

    return "queued_for_review", "unsupported_mode_review_fallback", "review_queue"


def capture_continuity_candidates(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityCaptureCandidatesInput,
) -> ContinuityCaptureCandidatesResponse:
    del store, user_id

    user_text = _normalize_content(request.user_content)
    assistant_text = _normalize_content(request.assistant_content)

    candidates: list[ContinuityCaptureCandidateRecord] = []
    seen_keys: set[tuple[str, str]] = set()

    for source_role, text in (("user", user_text), ("assistant", assistant_text)):
        extracted = _extract_from_role(text=text, source_role=source_role)
        if extracted is None:
            continue
        key = (extracted.candidate_type, extracted.normalized_text)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(_build_candidate_record(extracted))

    if not candidates:
        candidates = [_no_op_candidate(user_text=user_text, assistant_text=assistant_text)]
    elif _is_ack_only_turn(user_text=user_text, assistant_text=assistant_text):
        candidates = [_no_op_candidate(user_text=user_text, assistant_text=assistant_text)]

    summary: ContinuityCaptureCandidatesSummary = {
        "candidate_count": len(candidates),
        "explicit_count": sum(1 for candidate in candidates if candidate["explicit"]),
        "high_confidence_count": sum(1 for candidate in candidates if candidate["confidence"] >= 0.9),
        "no_op_count": sum(1 for candidate in candidates if candidate["candidate_type"] == "no_op"),
    }

    return {
        "candidates": candidates,
        "summary": summary,
    }


def commit_continuity_captures(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityCaptureCommitInput,
) -> ContinuityCaptureCommitResponse:
    mode = _normalize_commit_mode(request.mode)

    normalized_sync_fingerprint = _normalize_content(request.sync_fingerprint or "")
    commits: list[ContinuityCaptureCommitRecord] = []
    auto_saved_types: set[str] = set()
    review_queued_types: set[str] = set()

    auto_saved_count = 0
    review_queued_count = 0
    noop_count = 0
    duplicate_noop_count = 0

    # Bounded before anything is read or written (review finding 8).
    if len(request.candidates) > MAX_CAPTURE_COMMIT_CANDIDATES:
        raise ContinuityCaptureValidationError(
            f"candidates must hold at most {MAX_CAPTURE_COMMIT_CANDIDATES} entries"
        )
    for raw_candidate in request.candidates:
        if serialized_chars(raw_candidate) > MAX_CAPTURE_CANDIDATE_CHARS:
            raise ContinuityCaptureValidationError(
                f"each candidate must serialize to {MAX_CAPTURE_CANDIDATE_CHARS} characters or fewer"
            )
    normalized_candidates = [_normalize_candidate(candidate) for candidate in request.candidates]

    for candidate in normalized_candidates:
        sync_fingerprint = normalized_sync_fingerprint or f"candidate:{candidate['candidate_id']}"

        if candidate["candidate_type"] != "no_op":
            existing_row = store.get_continuity_object_by_commit_fingerprint_optional(
                sync_fingerprint=sync_fingerprint,
                candidate_id=candidate["candidate_id"],
            )
            if existing_row is not None:
                existing_object = get_continuity_object_for_capture_event(
                    store,
                    user_id=user_id,
                    capture_event_id=existing_row["capture_event_id"],
                )
                commits.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "candidate_type": candidate["candidate_type"],
                        "decision": "duplicate_noop",
                        "reason": "idempotent_sync_duplicate",
                        "persistence_target": "none",
                        "capture_event": None,
                        "continuity_object": existing_object,
                    }
                )
                duplicate_noop_count += 1
                continue

        decision, reason, persistence_target = _resolve_commit_decision(candidate=candidate, mode=mode)

        if decision == "no_op":
            commits.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_type": candidate["candidate_type"],
                    "decision": "no_op",
                    "reason": reason,
                    "persistence_target": "none",
                    "capture_event": None,
                    "continuity_object": None,
                }
            )
            noop_count += 1
            continue

        object_type = _object_type_for_candidate(candidate["candidate_type"], candidate["object_type"])
        explicit_signal = _explicit_signal_for_candidate(candidate["candidate_type"])
        admission_posture = "DERIVED" if decision == "auto_saved" else "TRIAGE"

        capture_event_row = store.create_continuity_capture_event(
            raw_content=candidate["normalized_text"],
            explicit_signal=explicit_signal,
            admission_posture=admission_posture,
            admission_reason=_truncate(candidate["admission_reason"], max_length=200),
        )
        serialized_capture = _serialize_capture_event(capture_event_row)

        provenance: JsonObject = {
            "capture_event_id": str(capture_event_row["id"]),
            "source_kind": "continuity_capture_candidate",
            "candidate_id": candidate["candidate_id"],
            "candidate_type": candidate["candidate_type"],
            "sync_fingerprint": sync_fingerprint,
            "commit_mode": mode,
            "commit_decision": decision,
            "admission_reason": candidate["admission_reason"],
            "explicit": candidate["explicit"],
            "trust_class": candidate["trust_class"],
            "evidence_snippet": candidate["evidence_snippet"],
        }

        continuity_object = create_continuity_object_record(
            store,
            user_id=user_id,
            capture_event_id=capture_event_row["id"],
            object_type=object_type,
            status="active" if decision == "auto_saved" else "stale",
            title=_title_for_object_type(object_type, candidate["normalized_text"]),
            body=_body_for_candidate(candidate=candidate, object_type=object_type),
            provenance=provenance,
            confidence=candidate["confidence"],
        )

        if decision == "auto_saved":
            auto_saved_count += 1
            auto_saved_types.add(candidate["candidate_type"])
        else:
            review_queued_count += 1
            review_queued_types.add(candidate["candidate_type"])

        commits.append(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_type": candidate["candidate_type"],
                "decision": decision,
                "reason": reason,
                "persistence_target": persistence_target,
                "capture_event": serialized_capture,
                "continuity_object": continuity_object,
            }
        )

    summary: ContinuityCaptureCommitSummary = {
        "mode": mode,
        "candidate_count": len(normalized_candidates),
        "auto_saved_count": auto_saved_count,
        "review_queued_count": review_queued_count,
        "noop_count": noop_count,
        "duplicate_noop_count": duplicate_noop_count,
        "auto_saved_types": sorted(auto_saved_types),
        "review_queued_types": sorted(review_queued_types),
    }

    return {
        "commits": commits,
        "summary": summary,
    }


def capture_continuity_input(
    store: ContinuityStore,
    *,
    user_id: UUID,
    request: ContinuityCaptureCreateInput,
) -> ContinuityCaptureCreateResponse:
    del user_id

    normalized_text = _normalize_content(request.raw_content)
    if not normalized_text:
        raise ContinuityCaptureValidationError("raw_content must not be empty")

    explicit_signal = request.explicit_signal
    if explicit_signal is not None and explicit_signal not in CONTINUITY_CAPTURE_EXPLICIT_SIGNALS:
        allowed = ", ".join(CONTINUITY_CAPTURE_EXPLICIT_SIGNALS)
        raise ContinuityCaptureValidationError(
            f"explicit_signal must be one of: {allowed}"
        )

    decision: DerivedObjectDecision | None = None
    admission_posture = "TRIAGE"
    admission_reason = "ambiguous_capture_requires_triage"

    if explicit_signal is not None:
        decision = _resolve_explicit_signal_decision(
            explicit_signal=explicit_signal,
            normalized_text=normalized_text,
        )
        admission_posture = "DERIVED"
        admission_reason = decision.admission_reason
    else:
        decision = _resolve_high_confidence_decision(normalized_text)
        if decision is not None:
            admission_posture = "DERIVED"
            admission_reason = decision.admission_reason

    capture_event_row = store.create_continuity_capture_event(
        raw_content=normalized_text,
        explicit_signal=explicit_signal,
        admission_posture=admission_posture,
        admission_reason=admission_reason,
    )

    serialized_capture = _serialize_capture_event(capture_event_row)
    derived_object = None

    if decision is not None:
        provenance: JsonObject = {
            "capture_event_id": str(capture_event_row["id"]),
            "source_kind": "continuity_capture_event",
            "admission_reason": decision.admission_reason,
            "explicit_signal": explicit_signal,
        }
        derived_object = create_continuity_object_record(
            store,
            user_id=UUID(str(capture_event_row["user_id"])),
            capture_event_id=capture_event_row["id"],
            object_type=decision.object_type,
            status="active",
            title=_title_for_object_type(decision.object_type, decision.normalized_text),
            body=_body_for_object_type(
                object_type=decision.object_type,
                normalized_text=decision.normalized_text,
                raw_content=normalized_text,
                explicit_signal=explicit_signal,
            ),
            provenance=provenance,
            confidence=decision.confidence,
        )

    return {
        "capture": _build_inbox_item(serialized_capture, derived_object),
    }


def list_continuity_capture_inbox(
    store: ContinuityStore,
    *,
    user_id: UUID,
    limit: int = DEFAULT_CONTINUITY_CAPTURE_LIMIT,
) -> ContinuityCaptureInboxResponse:
    _validate_capture_limit(limit)

    events = store.list_continuity_capture_events(limit=limit)
    event_ids = [row["id"] for row in events]
    object_map = list_continuity_objects_for_capture_events(
        store,
        user_id=user_id,
        capture_event_ids=event_ids,
    )

    items: list[ContinuityCaptureInboxItem] = []
    triage_count = 0

    for event in events:
        serialized_capture = _serialize_capture_event(event)
        if serialized_capture["admission_posture"] == "TRIAGE":
            triage_count += 1
        items.append(
            _build_inbox_item(
                serialized_capture,
                object_map.get(str(event["id"])),
            )
        )

    total_count = store.count_continuity_capture_events()
    derived_count = len(items) - triage_count

    return {
        "items": items,
        "summary": {
            "limit": limit,
            "returned_count": len(items),
            "total_count": total_count,
            "derived_count": derived_count,
            "triage_count": triage_count,
            "order": list(CONTINUITY_CAPTURE_LIST_ORDER),
        },
    }


def get_continuity_capture_detail(
    store: ContinuityStore,
    *,
    user_id: UUID,
    capture_event_id: UUID,
) -> ContinuityCaptureDetailResponse:
    del user_id

    event = store.get_continuity_capture_event_optional(capture_event_id)
    if event is None:
        raise ContinuityCaptureNotFoundError(
            f"continuity capture event {capture_event_id} was not found"
        )

    serialized_event = _serialize_capture_event(event)
    derived_object = get_continuity_object_for_capture_event(
        store,
        user_id=UUID(str(event["user_id"])),
        capture_event_id=capture_event_id,
    )

    return {
        "capture": _build_inbox_item(serialized_event, derived_object),
    }


__all__ = [
    "ContinuityCaptureNotFoundError",
    "ContinuityCaptureValidationError",
    "capture_continuity_candidates",
    "capture_continuity_input",
    "commit_continuity_captures",
    "get_continuity_capture_detail",
    "list_continuity_capture_inbox",
]
