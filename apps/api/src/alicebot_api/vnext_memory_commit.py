from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import logging
from typing import Callable, Mapping, cast
from uuid import UUID, uuid4

from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    AgentPolicyBlockedError,
    PolicyDecision,
    agent_metadata,
    append_policy_events,
    evaluate_agent_policy,
    resource_project_scope,
)

# Currency chains (stored currency): an approved supersession stamps the
# retired row's valid_to with the replacement's event time — see the marked
# block in _transition_memory.
from alicebot_api.vnext_currency import supersession_event_time
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding, attach_memory_embedding
from alicebot_api.vnext_entities import (
    ENTITY_MENTION_EDGE_TYPE,
    PERSON_ABOUT_EDGE_TYPE,
    EntityLinkingService,
    derive_person_name_from_title,
    store_supports_entity_linking,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_fact_keys import attach_memory_fact_keys
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_lifecycle import (
    CONFIRM_ACCEPT,
    CONFIRM_EXPIRE,
    CONFIRM_REJECT,
    CORRECT,
    EXPIRE,
    FORGET,
    LIVE_STATUSES as _LIFECYCLE_LIVE_STATUSES,
    RETIRED_STATUSES as _LIFECYCLE_RETIRED_STATUSES,
    SUPERSESSION_SUCCESSOR_STATUSES,
    SUPERSEDE_MEMBER,
    UNDO,
    UNEXPIRE,
    LifecycleTransitionError,
    resolve_transition,
    supersession_would_cycle,
)
from alicebot_api.vnext_memory_version import memory_matches_snapshot
from alicebot_api.write_bounds import (
    MAX_COMMIT_SOURCE_REF_CHARS as _MAX_COMMIT_SOURCE_REF_CHARS,
    MAX_COMMIT_SOURCE_REFS as _MAX_COMMIT_SOURCE_REFS,
)
from alicebot_api.credential_floor import (
    SECRET_PREFIX_PATTERNS as SECRET_PREFIX_PATTERNS,
    TEXT_WITHHELD_PLACEHOLDER,
    VERDICT_CREDENTIAL,
    VERDICT_EXPANSION,
    credential_verdict,
    refuse_credential_activation,
    withhold_credential_text,
)
from alicebot_api.legacy_credential_check import commit_gate_refuses as legacy_commit_gate_refuses
from alicebot_api.vnext_promotion_policy import (
    PromotionCandidate,
    PromotionDecision,
    PromotionSettings,
    PromotionSettingsValidationError,
    evaluate_promotion,
    resolve_promotion_settings,
    writer_trust_for,
)
from alicebot_api.vnext_project_update_guard import (
    PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE,
    is_pending_project_update_memory,
)
from alicebot_api.vnext_project_scope import project_scope_identity
from alicebot_api.vnext_repositories import EventStore, JsonObject
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_store import PostgresVNextStore, VNextRow


MEMORY_COMMIT_WRITE_MODES = ("commit", "confirm_inline", "propose_review", "reject")
MEMORY_COMMIT_STATUSES = ("committed", "confirmation_required", "review_required", "rejected")
AUTO_PROMOTED_EVENT = "memory.auto_promoted"
ENTITY_LINKING_ERROR_CODE = "entity_linking_failed"
ENTITY_LINKING_ERROR_MESSAGE = "Memory entity linking failed"
logger = logging.getLogger(__name__)
# vNext memory row status vocabulary. Mirrors sqlite_schema.MEMORY_STATUSES
# (owned by the schema workstream) plus "stale": the write-side marker the
# staleness sweep applies to expired or long-unconfirmed working-state
# memories. Postgres carries no CHECK on memories.status (migration
# 20260510_0067 built the status list but never attached a constraint), so
# "stale" needs no Postgres constraint change; the SQLite CHECK is extended
# separately in sqlite_schema.py.
MEMORY_STATUSES = (
    "candidate",
    "active",
    "accepted",
    "rejected",
    "superseded",
    "archived",
    "needs_review",
    "private_only",
    "stale",
)
# The lifecycle transition table partitions this same vocabulary into
# retired/live; keep the two in lockstep so a new status can never slip past
# the centrally-enforced transitions.
assert set(MEMORY_STATUSES) == (_LIFECYCLE_LIVE_STATUSES | _LIFECYCLE_RETIRED_STATUSES), (
    "MEMORY_STATUSES and vnext_lifecycle status partitions have diverged"
)
VNEXT_DOMAINS = (
    "professional",
    "personal",
    "family",
    "health",
    "spiritual",
    "financial",
    "legal",
    "learning",
    "relationship",
    "project",
    "agent_run",
    "system",
    "unknown",
)
VNEXT_SENSITIVITY_LEVELS = (
    "public",
    "internal",
    "private",
    "confidential",
    "highly_sensitive",
    "sacred",
    "regulated",
    "unknown",
)
VNEXT_MEMORY_TYPES = (
    "preference",
    "identity_fact",
    "relationship_fact",
    "project_fact",
    "decision",
    "commitment",
    "routine",
    "procedure",
    "constraint",
    "working_style",
    "episode",
    "semantic",
    "project_state",
    "belief",
    "thesis",
    "person",
    "relationship",
    "open_loop",
    "value",
    "pattern",
    "contradiction",
    "question",
    "answer",
    "artifact_summary",
    "agent_run",
    "system",
)
_MEMORY_TYPE_ALIASES = {
    "fact": "semantic",
    "note": "semantic",
    "quote": "semantic",
    "quote_collection": "semantic",
    "quote_memory": "semantic",
    "quote_note": "semantic",
    "quotes": "semantic",
    "quotation": "semantic",
    "quotation_note": "semantic",
    "quotations": "semantic",
    "saved_quote": "semantic",
}
_DOMAIN_ALIASES = {
    "work": "professional",
    "career": "professional",
    "finance": "financial",
    "money": "financial",
    "quote": "learning",
    "quote_collection": "learning",
    "quote_memory": "learning",
    "quote_note": "learning",
    "quotes": "learning",
    "quotation": "learning",
    "quotation_note": "learning",
    "quotations": "learning",
    "philosophy": "learning",
    "saved_quote": "learning",
    "wisdom": "learning",
    "general": "unknown",
}
_SENSITIVITY_ALIASES = {
    "sensitive": "confidential",
    "secret": "highly_sensitive",
}
TRUSTED_COMMIT_PROFILES = {"trusted_local_agent", "admin_agent"}
PROJECT_COMMIT_PROFILES = {"project_scoped_agent"}
REVIEW_ONLY_PROFILES = {"memory_proposal_agent"}
SENSITIVE_DOMAINS = {"family", "health", "spiritual", "legal", "financial"}
SENSITIVE_LEVELS = {"confidential", "highly_sensitive", "sacred", "regulated"}
DIRECT_TRUSTED_SOURCES = {"direct_user_instruction", "trusted_agent", "local_conversation"}
EXTERNAL_REVIEW_SOURCES = {
    "browser_clip",
    "browser_clipper",
    "csv",
    "docx",
    "email",
    "external",
    "generated_artifact",
    "gmail",
    "pdf",
    "research",
    "screenshot_ocr",
    "telegram",
    "telegram_forward",
    "voice_transcript",
    "web_page",
}
EXPLICIT_MEMORY_INTENTS = {
    "explicit_remember",
    "remember_this",
    "save_this",
    "add_to_memory",
    "commit_memory",
}
# Statuses a consolidation candidate may hold when it is accepted.
CONSOLIDATION_ACCEPTABLE_STATUSES = ("candidate", "needs_review")
DERIVED_CONSOLIDATION_CANDIDATE_KINDS = frozenset({"memory_consolidation", "memory_rollup"})
# Rows in these statuses are already retired; expiring or unexpiring them
# would corrupt the supersession/review audit trail. Retained for backward
# compatibility only -- the authoritative expire/unexpire preconditions now
# live in the central transition table (vnext_lifecycle.EXPIRE / UNEXPIRE,
# which also block the ``archived`` terminal status).
EXPIRE_BLOCKED_STATUSES = ("superseded", "rejected")
# update_memory in both live stores COALESCEs every column, so a NULL
# valid_to can never be written back through the patch surface. unexpire()
# therefore falls back to this far-future timestamp, which the read-path
# exclusion (valid_to IS NULL OR valid_to >= now) treats identically to
# NULL. Stores that grow a real clear_memory_valid_to seam make this
# sentinel unnecessary; until then it is recorded in metadata_json.validity.
VALID_TO_UNBOUNDED_SENTINEL = "9999-12-31T23:59:59Z"
# SECRET_PREFIX_PATTERNS moved to alicebot_api.credential_floor, the one
# credential check that commit, confirm and correct call. Re-exported above
# so the name still resolves here.

# source_refs are persisted on the row, replayed into context packs, and
# scanned by the credential floor, so an unbounded list was both a storage
# and a scan-time problem (the 2026-09-03 dump measured 33.9 s for a single
# element against the old quadratic assignment regex). The HTTP model and the
# MCP schema carry the same bounds; this is the one every caller of the
# service meets.
# Defined once, in write_bounds, so the HTTP model and the MCP schemas read
# the same numbers; re-exported here, where the one enforcer lives.
MAX_COMMIT_SOURCE_REFS = _MAX_COMMIT_SOURCE_REFS
MAX_COMMIT_SOURCE_REF_CHARS = _MAX_COMMIT_SOURCE_REF_CHARS


class VNextMemoryCommitValidationError(ValueError):
    """Raised when an agentic memory commit request is invalid."""


class _IdempotentReplaySignal(RuntimeError):
    def __init__(self, memory: VNextRow):
        super().__init__("concurrent idempotent memory replay")
        self.memory = memory


@dataclass(frozen=True, slots=True)
class MemoryCommitPolicyDecision:
    write_mode: str
    status: str
    reason: str
    reasons: tuple[str, ...]
    requires_confirmation: bool
    requires_dashboard_review: bool
    policy_decision: PolicyDecision
    # Present only when a persona has been configured; absent otherwise so an
    # unconfigured deployment writes byte-identical metadata.
    promotion: PromotionDecision | None = None
    # The write mode this request would have received without promotion.
    # Set only when promotion actually changed the outcome.
    promoted_from: str | None = None

    def to_record(self) -> JsonObject:
        record: JsonObject = {
            "write_mode": self.write_mode,
            "status": self.status,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "requires_confirmation": self.requires_confirmation,
            "requires_dashboard_review": self.requires_dashboard_review,
            "policy_decision": self.policy_decision.to_record(),
            "trace_id": self.policy_decision.trace_id,
        }
        if self.promotion is not None:
            record["promotion"] = self.promotion.to_record()
        if self.promoted_from is not None:
            record["promoted_from"] = self.promoted_from
        return record


@dataclass(frozen=True, slots=True)
class MemoryCommitRequest:
    user_id: str
    title: str
    canonical_text: str
    memory_type: str = "semantic"
    domain: str = "unknown"
    sensitivity: str = "unknown"
    confidence: float = 0.9
    intent: str = "explicit_remember"
    source_type: str = "direct_user_instruction"
    source_refs: tuple[object, ...] = ()
    conversation_excerpt: str | None = None
    rationale: str | None = None
    idempotency_key: str | None = None
    project_scope: tuple[str, ...] = ()
    contradiction_refs: tuple[str, ...] = ()
    trace_id: str | None = None


def _request_fingerprint(request: MemoryCommitRequest) -> str:
    """Content-bound digest for detecting unsafe idempotency-key reuse."""
    payload = {
        "user_id": request.user_id,
        "title": request.title,
        "canonical_text": request.canonical_text,
        "memory_type": request.memory_type,
        "domain": request.domain,
        "sensitivity": request.sensitivity,
        "confidence": request.confidence,
        "intent": request.intent,
        "source_type": request.source_type,
        "source_refs": json_safe(list(request.source_refs)),
        "conversation_excerpt": request.conversation_excerpt,
        "rationale": request.rationale,
        "project_scope": list(request.project_scope),
        "contradiction_refs": list(request.contradiction_refs),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat().replace("+00:00", "Z")


def _valid_to_iso(value: object | None) -> str:
    """Normalize an expiry timestamp to ISO-8601 UTC text; default now."""
    if value is None:
        return _utc_iso()
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VNextMemoryCommitValidationError("valid_to must be an ISO-8601 timestamp") from exc
        return _valid_to_iso(parsed)
    raise VNextMemoryCommitValidationError("valid_to must be an ISO-8601 timestamp")


def _is_unbounded_valid_to(value: object | None) -> bool:
    """True when valid_to is the far-future stand-in for 'no expiry'."""
    if isinstance(value, datetime):
        return value.year >= 9999
    return isinstance(value, str) and value.startswith("9999-")


def _earliest_valid_to(existing: object | None, replacement_event_time: object | None) -> str:
    """Close a retired row at the earliest already-reviewed boundary.

    Supersession cannot extend a validity window. This also replaces the
    far-future sentinel written by ``unexpire`` with the real replacement
    event time.
    """

    replacement_iso = _valid_to_iso(replacement_event_time)
    if existing is None:
        return replacement_iso
    try:
        existing_iso = _valid_to_iso(existing)
        existing_at = datetime.fromisoformat(existing_iso.replace("Z", "+00:00"))
        replacement_at = datetime.fromisoformat(replacement_iso.replace("Z", "+00:00"))
    except (TypeError, ValueError, VNextMemoryCommitValidationError):
        # A malformed legacy value must not make a superseded row immortal.
        return replacement_iso
    return existing_iso if existing_at <= replacement_at else replacement_iso


def _normalized_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise VNextMemoryCommitValidationError(f"{field_name} must be a string")
    normalized = " ".join(value.split()).strip()
    if normalized == "":
        raise VNextMemoryCommitValidationError(f"{field_name} must not be empty")
    return normalized


def _enum_token(value: str) -> str:
    return "_".join(value.casefold().replace("-", "_").split())


def _enum_value(
    value: object,
    *,
    field_name: str,
    allowed_values: tuple[str, ...],
    aliases: Mapping[str, str] | None = None,
) -> str:
    normalized = _normalized_text(value, field_name=field_name)
    token = _enum_token(normalized)
    canonical = aliases.get(token, token) if aliases is not None else token
    if canonical not in allowed_values:
        raise VNextMemoryCommitValidationError(f"{field_name} must be one of: {', '.join(allowed_values)}")
    return canonical


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VNextMemoryCommitValidationError("optional text fields must be strings")
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = " ".join(value.split()).strip()
        return (normalized,) if normalized else ()
    if not isinstance(value, (list, tuple)):
        raise VNextMemoryCommitValidationError("list fields must be arrays of strings")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise VNextMemoryCommitValidationError("list fields must be arrays of strings")
        normalized = " ".join(item.split()).strip()
        if normalized:
            output.append(normalized)
    return tuple(output)


def _object_tuple(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise VNextMemoryCommitValidationError("source_refs must be an array")
    if len(value) > MAX_COMMIT_SOURCE_REFS:
        raise VNextMemoryCommitValidationError(
            f"source_refs must hold at most {MAX_COMMIT_SOURCE_REFS} entries"
        )
    for ref in value:
        # A string ref is measured as sent (owner ruling R4): measuring its
        # JSON form counted every quote and newline twice, so a 2,100
        # character ref of quotes was refused. Any other ref is measured by
        # its serialized length.
        size = len(ref) if isinstance(ref, str) else len(json.dumps(json_safe(ref), ensure_ascii=False))
        if size > MAX_COMMIT_SOURCE_REF_CHARS:
            raise VNextMemoryCommitValidationError(
                f"each source_ref must be at most {MAX_COMMIT_SOURCE_REF_CHARS} characters"
            )
    return tuple(value)


def _request_contains_secret_marker(request: MemoryCommitRequest) -> str | None:
    """Scan every persisted caller field; the credential floor's verdict or None.

    A credential pasted into the title, excerpt, rationale, or source refs is
    stored and later returned inside context packs exactly like body text, so
    guarding one field only moves the leak rather than closing it.

    Until 2026-09-22 this scanned each field on its own, so a key split
    across the title and the body ("id AK" + "IAIOSFODNN7EXAMPLE ...")
    committed active while the promotion floor, which joins the fields,
    would have caught it. It now delegates to the credential floor, in the
    floor's reading order: title, body, free text, structured values, then
    the persisted identifiers (idempotency_key, trace_id and project_scope,
    read by value only). A key planted in an identifier is stored and
    replayed like any other field.

    Then v0.16.0's own check (S4.4 round 5, ruling H1). This is one of the
    two doors v0.16.0 checked, and the floor's name grammar reads only the
    last segment of a name, so PASSWORD_DB=, TOKEN_GITHUB=, API_KEY_RAW= and
    GITHUB_TOKEN_WRITE= with real values, which v0.16.0 refused here, were
    stored. The request is refused when the floor refuses OR when v0.16.0's
    check, re-implemented in linear time over the fields v0.16.0 read, finds
    something no carve-out excuses (see legacy_credential_check).
    """

    verdict = credential_verdict(
        request.title,
        request.canonical_text,
        request.conversation_excerpt,
        request.source_refs,
        request.rationale,
        request.idempotency_key,
        request.trace_id,
        list(request.project_scope),
    )
    if verdict is not None:
        return verdict
    if legacy_commit_gate_refuses(
        request.title,
        request.canonical_text,
        request.conversation_excerpt,
        request.rationale,
        request.source_refs,
    ):
        return VERDICT_CREDENTIAL
    return None


def _withheld_history(entries: list[object]) -> tuple[list[object], bool]:
    """Carried-forward history entries with any credential reason withheld.

    Owner ruling R2: when a writer rebuilds validity.history or
    lifecycle_history, the entries it carries forward pass through the same
    withhold helper as the new reason, so a reason stored before the floor
    existed does not ride along into the rewritten row.
    """

    carried: list[object] = []
    withheld_any = False
    for entry in entries:
        if isinstance(entry, Mapping) and isinstance(entry.get("reason"), str):
            reason, withheld = withhold_credential_text(str(entry["reason"]))
            if withheld:
                entry = {**entry, "reason": reason}
                withheld_any = True
        carried.append(entry)
    return carried, withheld_any


def _source_ref_values(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.strip():
            refs.append(value.strip())
    elif isinstance(value, Mapping):
        for key in ("source_id", "id", "ref", "source_ref"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)):
                refs.append(str(candidate))
        for nested_key in ("source_ids", "source_refs", "sources"):
            refs.extend(_source_ref_values(value.get(nested_key)))
    elif isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_source_ref_values(item))
    return refs


def _source_uuid(value: object) -> str | None:
    for ref in _source_ref_values(value):
        normalized = ref.removeprefix("source:")
        try:
            return str(UUID(normalized))
        except ValueError:
            continue
    return None


def _scope_columns(
    *,
    identity: AgentIdentity | None,
    request: MemoryCommitRequest,
) -> JsonObject:
    """First-class scope columns for a memory row written by the commit path.

    ``project_scope`` is the canonical overlap-aware representation;
    ``project_id`` remains a singular compatibility/index column.
    """
    scope = request.project_scope or (identity.project_scope if identity is not None else ())
    return {
        "project_scope": list(scope),
        "project_id": scope[0] if len(scope) == 1 else None,
        "created_by_agent_id": identity.agent_id if identity is not None else None,
        "run_id": identity.agent_run_id if identity is not None else None,
    }


def _memory_metadata(row: Mapping[str, object] | None) -> dict[str, object]:
    if row is None:
        return {}
    metadata = row.get("metadata_json")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def is_pending_consolidation_candidate(memory: Mapping[str, object]) -> bool:
    """Return whether a memory still awaits canonical consolidation acceptance."""

    metadata = _memory_metadata(memory)
    consolidation = metadata.get("consolidation")
    if not isinstance(consolidation, Mapping):
        candidate_kind = metadata.get("candidate_kind")
        return isinstance(candidate_kind, str) and candidate_kind in DERIVED_CONSOLIDATION_CANDIDATE_KINDS
    return not isinstance(consolidation.get("accepted"), Mapping)


def _require_project_update_decision_path(memory: Mapping[str, object]) -> None:
    """Keep pending coupled candidates on their atomic project-review path."""

    if is_pending_project_update_memory(memory):
        raise VNextMemoryCommitValidationError(PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE)


def _agentic_metadata(row: Mapping[str, object] | None) -> dict[str, object]:
    metadata = _memory_metadata(row)
    agentic = metadata.get("agentic_memory")
    return dict(agentic) if isinstance(agentic, Mapping) else {}


# Supersession chains are short in practice (one correction, occasionally
# a handful); the bound only guards against pathological pointer data.
_SUPERSESSION_CHAIN_MAX_DEPTH = 10


def _supersession_pointer(row: Mapping[str, object] | None, key: str) -> str | None:
    """Read a supersession pointer: real column first, metadata fallback.

    The metadata_json fallback covers rows written before migration
    20260704_0077 promoted the pointers to columns (the migration
    backfills, but a not-yet-migrated row costs nothing to tolerate).
    """
    if row is None:
        return None
    value = row.get(key)
    if value:
        return str(value)
    metadata = row.get("metadata_json")
    if isinstance(metadata, Mapping):
        fallback = metadata.get(key)
        if isinstance(fallback, str) and fallback:
            return fallback
    return None


def _append_policy_decision(
    store: PostgresVNextStore,
    *,
    identity: AgentIdentity | None,
    decision: PolicyDecision,
    target_type: str | None = None,
    target_id: str | None = None,
) -> None:
    append_policy_events(store, identity=identity, decision=decision, target_type=target_type, target_id=target_id)


def _brain_charter_row(store: object) -> Mapping[str, object] | None:
    """Best-effort read of the owner's Brain Charter row.

    Guarded rather than assumed: several adapters and tests pass store
    doubles that carry no charter seam, and a missing charter must resolve to
    "no persona configured" rather than raise.
    """

    getter = getattr(store, "get_brain_charter", None)
    if not callable(getter):
        return None
    try:
        loaded = getter()
    except Exception:  # pragma: no cover - defensive, store shapes vary
        # Warning rather than debug: an operator who configured a persona and
        # is silently not getting it needs a signal above the default level.
        logger.warning("brain charter lookup failed; promotion stays review-gated", exc_info=True)
        return None
    return loaded if isinstance(loaded, Mapping) else None


def load_promotion_settings(
    *,
    brain_charter: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> PromotionSettings | None:
    """Resolve the deployment's promotion persona, or None when unconfigured.

    Reads configuration only: an optional already-loaded Brain Charter row and
    the process environment. It issues no query of its own.

    Fails closed. An absent charter key, an unset environment variable, an
    empty escalation-filter value, or a malformed persona all resolve to the
    strictest posture available rather than a loosened one; a malformed
    persona resolves to None, meaning every write stays review-gated.
    """

    try:
        return resolve_promotion_settings(brain_charter=brain_charter, environ=environ)
    except PromotionSettingsValidationError:
        logger.warning("invalid memory promotion settings; falling back to review-gated writes")
        return None


def agent_api_keys_provisioned(store: object) -> bool:
    """Whether this user has at least one active agent API key.

    This is one half of the owner test, and on its own it proves nothing
    about a particular call. It is the codebase's own precondition for
    ``resolve_protected_agent_identity`` rejecting keyless callers, so an
    adapter that runs that check may combine it with this to conclude that an
    identity-less request is the authenticated human.

    An adapter that does NOT enforce keys must not use it. MCP resolves
    against a key only when ``ALICE_AGENT_API_KEY`` is set in its own
    environment, and the CLI has no enforcement path at all, so on those two
    surfaces an absent agent identity means nobody in particular. Treating
    "a key exists somewhere on this install" as evidence about this call is
    what let a caller reach owner standing by omitting one optional field.
    """

    counter = getattr(store, "count_active_agent_api_keys", None)
    if not callable(counter):
        return False
    try:
        return int(counter()) > 0
    except Exception:  # pragma: no cover - defensive, store shapes vary
        logger.warning("agent key count failed; promotion stays review-gated", exc_info=True)
        return False


def writer_trust_for_commit(*, identity: AgentIdentity | None, owner_verified: bool) -> str:
    """Classify the writer of a commit from transport facts only.

    ``owner_verified`` is asserted by the adapter that actually enforced a
    credential on this request. It is a parameter rather than something this
    function works out, because only the adapter knows whether its own
    surface rejects keyless callers.
    """

    return writer_trust_for(
        identity_auth=identity.auth if identity is not None else None,
        owner_verified=owner_verified,
    )


def promotion_candidate_for_request(
    *,
    identity: AgentIdentity | None,
    request: MemoryCommitRequest,
) -> PromotionCandidate:
    """Project a commit request onto the facts the promotion layer may read.

    ``written_by_agent`` is recorded for provenance only. Whether the write
    may skip review is decided by ``writer_trust_for_commit`` from how the
    identity was established, never from the payload or from the text.
    """

    return PromotionCandidate(
        canonical_text=request.canonical_text,
        title=request.title,
        memory_type=request.memory_type,
        domain=request.domain,
        sensitivity=request.sensitivity,
        intent=request.intent,
        source_type=request.source_type,
        source_refs=tuple(request.source_refs),
        contradiction_refs=tuple(request.contradiction_refs),
        conversation_excerpt=request.conversation_excerpt,
        rationale=request.rationale,
        project_scope=tuple(request.project_scope),
        written_by_agent=identity is not None,
    )


def evaluate_memory_commit_policy(
    *,
    identity: AgentIdentity | None,
    request: MemoryCommitRequest,
    policy_decision: PolicyDecision | None = None,
    promotion_settings: PromotionSettings | None = None,
    writer_trust: str = "unverified",
) -> MemoryCommitPolicyDecision:
    base_decision = policy_decision or evaluate_agent_policy(
        identity=identity,
        action="memory.commit",
        domains=(request.domain,),
        sensitivity_allowed=(request.sensitivity,),
        project_scope=request.project_scope,
    )
    reasons: list[str] = list(base_decision.reasons)
    mode = "commit"
    status = "committed"
    if identity is not None and identity.permission_profile in PROJECT_COMMIT_PROFILES and request.domain != "project":
        reasons.append("project_scoped_agent_domain_out_of_scope")

    if base_decision.decision == "blocked":
        reasons.append("agent_policy_blocked")
        mode = "reject"
        status = "rejected"
    elif (secret_verdict := _request_contains_secret_marker(request)) is not None:
        reasons.append(
            "unsafe_text_expansion" if secret_verdict == VERDICT_EXPANSION else "unsafe_secret_storage"
        )
        mode = "reject"
        status = "rejected"
    elif request.intent.casefold() not in EXPLICIT_MEMORY_INTENTS:
        reasons.append("explicit_memory_intent_required")
        mode = "propose_review"
        status = "review_required"
    elif request.source_type.casefold() in EXTERNAL_REVIEW_SOURCES:
        reasons.append("external_source_requires_review")
        mode = "propose_review"
        status = "review_required"
    elif len(request.source_refs) > 8:
        reasons.append("bulk_source_refs_require_review")
        mode = "propose_review"
        status = "review_required"
    elif identity is None:
        # Direct human/system operator path (evaluate_agent_policy already
        # returned a user_or_system decision): no agent permission-profile
        # gates apply, but the confidence/sensitivity checks below still run.
        pass
    elif identity.permission_profile in REVIEW_ONLY_PROFILES:
        reasons.append("memory_proposal_agent_review_only")
        mode = "propose_review"
        status = "review_required"
    elif identity.permission_profile in PROJECT_COMMIT_PROFILES:
        if request.domain != "project":
            reasons.append("project_scoped_agent_domain_out_of_scope")
            mode = "reject"
            status = "rejected"
        elif not (request.project_scope or identity.project_scope):
            reasons.append("project_scope_required")
            mode = "reject"
            status = "rejected"
        elif request.confidence < 0.5:
            reasons.append("low_confidence_requires_review")
            mode = "propose_review"
            status = "review_required"
    elif identity.permission_profile not in TRUSTED_COMMIT_PROFILES:
        reasons.append("trusted_or_project_scoped_agent_required")
        mode = "reject"
        status = "rejected"

    if mode == "commit":
        if request.confidence < 0.5:
            reasons.append("low_confidence_requires_review")
            mode = "propose_review"
            status = "review_required"
        elif request.confidence < 0.85:
            reasons.append("medium_confidence_requires_confirmation")
            mode = "confirm_inline"
            status = "confirmation_required"
        elif request.domain in SENSITIVE_DOMAINS or request.sensitivity in SENSITIVE_LEVELS:
            reasons.append("sensitive_memory_requires_confirmation")
            mode = "confirm_inline"
            status = "confirmation_required"
        elif request.contradiction_refs:
            reasons.append("contradiction_requires_confirmation")
            mode = "confirm_inline"
            status = "confirmation_required"
        elif request.source_type.casefold() not in DIRECT_TRUSTED_SOURCES:
            reasons.append("non_direct_source_requires_review")
            mode = "propose_review"
            status = "review_required"

    # Promotion runs strictly after the existing gate, on the gate's own
    # result. It can lift "this needs a human" to "write it"; it can never
    # lift a rejection, so every authorization, scope and secret-marker
    # rejection above still stands exactly as it did.
    promotion: PromotionDecision | None = None
    promoted_from: str | None = None
    if promotion_settings is not None:
        promotion = evaluate_promotion(
            settings=promotion_settings,
            candidate=promotion_candidate_for_request(identity=identity, request=request),
            permission_profile=base_decision.permission_profile,
            writer_trust=writer_trust,
        )
        if promotion.auto_promote and mode in {"propose_review", "confirm_inline"}:
            reasons.append("promotion_auto_promoted")
            promoted_from = mode
            mode = "commit"
            status = "committed"

    reason = reasons[-1] if reasons else "explicit_trusted_memory_commit"
    return MemoryCommitPolicyDecision(
        write_mode=mode,
        status=status,
        reason=reason,
        reasons=tuple(dict.fromkeys(reasons)),
        requires_confirmation=mode == "confirm_inline",
        requires_dashboard_review=mode == "propose_review",
        policy_decision=base_decision,
        promotion=promotion,
        promoted_from=promoted_from,
    )


COMMITTED_FACT_RECEIPT = "saved as a fact."


def memory_commit_receipt(status: str) -> str:
    """One line a host can print. Only a committed row is a fact."""

    if status == "committed":
        return COMMITTED_FACT_RECEIPT
    if status == "confirmation_required":
        return "needs confirmation."
    if status == "review_required":
        return "waiting in review."
    if status == "rejected":
        return "rejected."
    return "not recorded as a fact."


def _with_commit_receipt(payload: JsonObject) -> JsonObject:
    return {**payload, "receipt": memory_commit_receipt(str(payload.get("status") or ""))}


class VNextMemoryCommitService:
    def __init__(
        self,
        store: PostgresVNextStore,
        *,
        defer_embeddings: bool = False,
        promotion_settings: PromotionSettings | None = None,
        owner_verified: bool = False,
    ):
        self.store = store
        self._defer_embeddings = defer_embeddings
        # Only an adapter that enforced a credential on this request may set
        # this. The default is False, so a surface that never authenticates
        # anybody cannot promote an identity-less caller to owner standing by
        # doing nothing, which is how MCP and the CLI reached the top trust
        # level with no credential at all.
        self._owner_verified = owner_verified
        self._explicit_promotion_settings = promotion_settings
        # Resolution is deferred to first use. The service is constructed on
        # every memory API call, including undo, forget, expire, audit and
        # recent_commits, none of which consult a persona. Resolving eagerly
        # made a deployment that has configured nothing pay a charter read on
        # all of them; resolving here means only commit pays it, once.
        self._resolved_promotion_settings: PromotionSettings | None = None
        self._promotion_settings_resolved = False
        self._deferred_embedding_inputs: list[DeferredMemoryEmbedding] = []

    @property
    def promotion_settings(self) -> PromotionSettings | None:
        """Effective settings, resolved once and cached for this instance."""

        if self._explicit_promotion_settings is not None:
            return self._explicit_promotion_settings
        if not self._promotion_settings_resolved:
            self._resolved_promotion_settings = load_promotion_settings(
                brain_charter=_brain_charter_row(self.store)
            )
            self._promotion_settings_resolved = True
        return self._resolved_promotion_settings

    @property
    def deferred_embedding_inputs(self) -> tuple[DeferredMemoryEmbedding, ...]:
        """Immutable embedding snapshots collected for post-commit processing."""

        return tuple(self._deferred_embedding_inputs)

    def _attach_or_defer_memory_embedding(
        self,
        memory: Mapping[str, object],
        *,
        actor_type: str,
        actor_id: str | None,
        trace_id: str | None,
    ) -> None:
        if self._defer_embeddings:
            self._deferred_embedding_inputs.append(DeferredMemoryEmbedding.from_memory(memory))
            return
        attach_memory_embedding(
            self.store,
            memory,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=trace_id,
        )

    def _require_transition(self, operation: str, current_status: str) -> str:
        """Route a lifecycle mutation through the central transition table.

        Returns the resulting row status and raises the service's own
        validation error (so every interface surfaces a consistent type) when
        the transition is illegal from ``current_status``.
        """
        try:
            return resolve_transition(operation, current_status)
        except LifecycleTransitionError as exc:
            raise VNextMemoryCommitValidationError(str(exc)) from exc

    def lock_supersession_graph(self) -> None:
        """Acquire the transaction-scoped lifecycle lock before any row lock.

        PostgreSQL row locks and the per-user advisory lock must always be
        acquired in this order. The lock serializes supersession-graph writes
        plus consolidation candidate/member invalidation, preventing both
        row-to-graph and candidate-to-member/member-to-candidate inversions.
        PostgreSQL transaction advisory locks are re-entrant, so adapters may
        safely establish this boundary before delegating to a service method.
        """
        lock_graph_mutation = getattr(self.store, "lock_graph_mutation", None)
        if callable(lock_graph_mutation):
            lock_graph_mutation()

    def require_valid_supersession_successor(
        self,
        successor: Mapping[str, object],
        *,
        allow_pending_consolidation: bool = False,
    ) -> None:
        """Require a successor that can truthfully become the chain head.

        Normal replacements must already be accepted/live. A consolidation
        candidate is the sole exception: its candidate/needs_review status is
        permitted only inside the same atomic acceptance transaction that
        promotes it after retiring its reviewed members.
        """
        status = str(successor.get("status") or "")
        allowed = SUPERSESSION_SUCCESSOR_STATUSES
        if allow_pending_consolidation:
            allowed = allowed | frozenset(CONSOLIDATION_ACCEPTABLE_STATUSES)
        if status not in allowed:
            raise VNextMemoryCommitValidationError(
                f"superseding memory must be an accepted live successor; got status '{status or 'unknown'}'"
            )

    def _guard_supersession_acyclic(
        self,
        *,
        memory: VNextRow,
        successor: VNextRow,
        allow_pending_consolidation: bool = False,
    ) -> None:
        """Reject recording ``memory`` superseded-by ``successor`` when cyclic.

        Re-superseding a row back to one of its own predecessors would create
        an ``A -> B -> A`` supersession cycle and corrupt the audit chain.

        Concurrent supersessions on disjoint row pairs could each pass an
        unlocked cycle check and together close a cycle (e.g. ``A->B`` and
        ``C->D`` committing over pre-existing ``B->C`` and ``D->A``). A
        per-user, transaction-scoped advisory lock serializes graph mutation so
        the second supersession's check sees the first's committed edge. The
        public supersession entry points acquire that lock before any row lock
        and hold it through the edge write in the same transaction. The chain
        walk also fails closed when it cannot verify acyclicity within its hop
        bound (audit 2 P1 #1).
        """
        self.require_valid_supersession_successor(
            successor,
            allow_pending_consolidation=allow_pending_consolidation,
        )
        try:
            would_cycle = supersession_would_cycle(
                memory_id=str(memory["id"]),
                successor=successor,
                load_memory=self.store.get_memory,
                read_pointer=_supersession_pointer,
            )
        except LifecycleTransitionError as exc:
            raise VNextMemoryCommitValidationError(str(exc)) from exc
        if would_cycle:
            raise VNextMemoryCommitValidationError(
                "cannot supersede a memory with one of its own predecessors; that would create a supersession cycle"
            )

    def evaluate_policy(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
    ) -> MemoryCommitPolicyDecision:
        self._upsert_identity(identity)
        base_decision = evaluate_agent_policy(
            identity=identity,
            action="memory.commit",
            domains=(request.domain,),
            sensitivity_allowed=(request.sensitivity,),
            project_scope=request.project_scope,
        )
        _append_policy_decision(self.store, identity=identity, decision=base_decision)
        settings = self.promotion_settings
        return evaluate_memory_commit_policy(
            identity=identity,
            request=request,
            policy_decision=base_decision,
            promotion_settings=settings,
            # Only computed when a persona is configured, so an unconfigured
            # deployment issues no extra query on any path.
            writer_trust=(
                writer_trust_for_commit(identity=identity, owner_verified=self._owner_verified)
                if settings is not None
                else "unverified"
            ),
        )

    def commit(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
    ) -> JsonObject:
        existing = self._idempotent_memory(request.idempotency_key)
        if existing is not None:
            return _with_commit_receipt(self._idempotent_replay(memory=existing, request=request, identity=identity))

        decision = self.evaluate_policy(identity=identity, request=request)
        if decision.write_mode == "reject":
            self._append_decision_event(identity=identity, request=request, decision=decision, target_id=None)
            return _with_commit_receipt(
                {
                    "status": "rejected",
                    "write_mode": "reject",
                    "reason": decision.reason,
                    "reasons": list(decision.reasons),
                    "policy_decision": decision.to_record(),
                }
            )
        try:
            if decision.write_mode == "confirm_inline":
                return _with_commit_receipt(
                    self._create_confirmation(identity=identity, request=request, decision=decision)
                )
            if decision.write_mode == "propose_review":
                return _with_commit_receipt(
                    self._create_review_candidate(identity=identity, request=request, decision=decision)
                )
            return _with_commit_receipt(
                self._create_committed_memory(
                    identity=identity,
                    request=request,
                    decision=decision,
                    confirmed_inline=False,
                )
            )
        except _IdempotentReplaySignal as replay:
            return _with_commit_receipt(
                self._idempotent_replay(
                    memory=replay.memory,
                    request=request,
                    identity=identity,
                )
            )

    def confirm(
        self,
        *,
        identity: AgentIdentity | None,
        confirmation_id: str,
        action: str = "confirm",
        canonical_text: str | None = None,
        rationale: str | None = None,
    ) -> JsonObject:
        self.lock_supersession_graph()
        normalized_action = action.strip().casefold()
        if normalized_action not in {"confirm", "reject", "edit"}:
            raise VNextMemoryCommitValidationError("confirmation action must be confirm, reject, or edit")
        memory = self._memory_by_confirmation_id(confirmation_id)
        if memory is None:
            raise VNextMemoryCommitValidationError("confirmation was not found")
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        if callable(get_memory_for_update):
            locked = get_memory_for_update(str(memory["id"]))
            if locked is None:
                raise VNextMemoryCommitValidationError("confirmation was not found")
            memory = locked
        self._policy_checked_write(identity=identity, action="memory.confirm", memory=memory)
        metadata = _memory_metadata(memory)
        agentic = _agentic_metadata(memory)
        confirmation_value = agentic.get("confirmation")
        confirmation = dict(confirmation_value) if isinstance(confirmation_value, Mapping) else {}
        if confirmation.get("status") != "pending":
            replay = self._replay_confirmation(
                identity=identity,
                memory=memory,
                confirmation=confirmation,
                confirmation_id=confirmation_id,
                action=normalized_action,
            )
            if replay is not None:
                return replay
            raise VNextMemoryCommitValidationError("confirmation is not pending")

        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        expires_at_raw = confirmation.get("expires_at")
        if isinstance(expires_at_raw, str):
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
            if expires_at < _utc_now():
                # Expiration is a lifecycle decision, not a silent metadata
                # cleanup. Validate the row independently and preserve a full
                # revision/event trail just like explicit confirmation actions.
                self._require_transition(
                    CONFIRM_EXPIRE,
                    str(memory.get("status") or ""),
                )
                now = _utc_iso()
                confirmation["status"] = "expired"
                confirmation["expired_at"] = now
                agentic["confirmation"] = confirmation
                agentic["status"] = "rejected"
                agentic["lifecycle_status"] = "confirmation_expired"
                updated = self.store.update_memory(
                    memory_id=str(memory["id"]),
                    patch={
                        "status": "rejected",
                        "last_reviewed_at": now,
                        "metadata_json": {**metadata, "agentic_memory": agentic},
                    },
                    actor_type=actor_type,
                )
                expiry_reason = "Inline memory confirmation expired before it was acted on."
                self.store.append_revision(
                    {
                        "memory_id": str(updated["id"]),
                        "memory_key": str(updated["memory_key"]),
                        "previous_value": memory.get("value"),
                        "new_value": updated.get("value"),
                        "source_event_ids": updated.get("source_event_ids"),
                        "revision_type": "rejected",
                        "action": "agentic_memory_confirmation_expired",
                        "text_before": str(memory.get("canonical_text") or ""),
                        "text_after": str(updated.get("canonical_text") or ""),
                        "reason": expiry_reason,
                        "actor_type": actor_type,
                        "actor_id": actor_id,
                        "metadata_json": {
                            "confirmation_id": confirmation_id,
                            "action": "expire",
                            "expired_at": now,
                        },
                    },
                    actor_type=actor_type,
                )
                append_event(
                    self.store,
                    event_type="agent.memory_confirmation_expired",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    target_type="memory",
                    target_id=str(updated["id"]),
                    trace_id=str(agentic.get("trace_id") or "") or None,
                    payload={
                        "confirmation_id": confirmation_id,
                        "action": "expire",
                        "status": "rejected",
                        "expired_at": now,
                    },
                )
                return {
                    "status": "rejected",
                    "write_mode": "confirm_inline",
                    "confirmation_id": confirmation_id,
                    "reason": "confirmation_expired",
                    "memory": updated,
                }

        # The nested flag says "pending"; the row's lifecycle status must
        # independently agree that this row still awaits confirmation. A review
        # rejection/supersession that retired the row (while leaving the flag
        # pending) must not be confirmable back to active.
        self._require_transition(
            CONFIRM_REJECT if normalized_action == "reject" else CONFIRM_ACCEPT,
            str(memory.get("status") or ""),
        )

        previous_text = str(memory.get("canonical_text") or "")
        next_text = (
            _normalized_text(canonical_text, field_name="canonical_text")
            if canonical_text is not None
            else previous_text
        )
        now = _utc_iso()
        if normalized_action == "reject":
            confirmation["status"] = "rejected"
            next_status = "rejected"
            response_status = "rejected"
            event_type = "agent.memory_confirmation_rejected"
            revision_type = "rejected"
        else:
            confirmation["status"] = "confirmed"
            next_status = "active"
            response_status = "committed"
            event_type = "agent.memory_confirmed"
            revision_type = "corrected" if normalized_action == "edit" else "promoted"

        # The credential floor, before anything is written. Until 2026-09-22
        # confirm(action=edit), and confirm with canonical_text, rewrote the
        # row's text with no credential check, so a secret the commit gate
        # rejects became active here. An accept is an activation: the shared
        # activation check reads the title, text and summary the row will
        # carry once active, plus the rationale persisted with it (ruling C2).
        # A reject always completes (ruling C6): a rationale, or a
        # caller-supplied canonical_text, that carries credential material is
        # stored as a fixed placeholder and the response says so.
        rationale_withheld = False
        text_withheld = False
        if next_status == "active":
            title_after = next_text[:120] if normalized_action == "edit" else str(memory.get("title") or "")
            refuse_credential_activation(
                title_after, next_text, next_text[:280], rationale, error=VNextMemoryCommitValidationError
            )
        else:
            rationale, rationale_withheld = withhold_credential_text(rationale)
            if canonical_text is not None:
                withheld_text, text_withheld = withhold_credential_text(next_text, TEXT_WITHHELD_PLACEHOLDER)
                next_text = withheld_text or ""

        agentic["confirmation"] = confirmation
        agentic["status"] = response_status
        agentic["lifecycle_status"] = "inline_confirmed" if next_status == "active" else "confirmation_rejected"
        agentic["confirmed_at"] = now if next_status == "active" else None
        if rationale is not None:
            agentic["confirmation_rationale"] = rationale
        patch: JsonObject = {
            "status": next_status,
            "metadata_json": {**metadata, "agentic_memory": agentic},
            "last_reviewed_at": now,
        }
        if next_status == "active":
            current_value = memory.get("value")
            value_payload = dict(current_value) if isinstance(current_value, dict) else {}
            patch.update(
                {
                    "canonical_text": next_text,
                    "summary": next_text[:280],
                    "value": {**value_payload, "text": next_text},
                    "confirmation_status": "confirmed",
                    "last_confirmed_at": now,
                }
            )
            if normalized_action == "edit":
                patch["title"] = next_text[:120]
        updated = self.store.update_memory(memory_id=str(memory["id"]), patch=patch, actor_type=actor_type)
        if next_status == "active":
            self._refresh_memory_derived_state(
                memory=updated,
                identity=identity,
                trace_id=str(agentic.get("trace_id") or "") or None,
                stage="inline_confirmation_accepted",
                replace_entity_links=normalized_action == "edit",
            )
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": revision_type,
                "action": f"agentic_memory_confirm_{normalized_action}",
                "text_before": previous_text,
                "text_after": next_text,
                "reason": rationale or f"Inline memory confirmation {normalized_action}.",
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {
                    "confirmation_id": confirmation_id,
                    "action": normalized_action,
                    "last_confirmed_at_refreshed": next_status == "active",
                },
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(updated["id"]),
            trace_id=str(agentic.get("trace_id") or ""),
            payload={"confirmation_id": confirmation_id, "action": normalized_action, "status": response_status},
        )
        response: JsonObject = {
            "status": response_status,
            "write_mode": "confirm_inline",
            "confirmation_id": confirmation_id,
            "memory": updated,
        }
        if next_status != "active":
            response["rationale_withheld"] = rationale_withheld
            response["text_withheld"] = text_withheld
        return response

    def _replay_confirmation(
        self,
        *,
        identity: AgentIdentity | None,
        memory: VNextRow,
        confirmation: Mapping[str, object],
        confirmation_id: str,
        action: str,
    ) -> JsonObject | None:
        """Handle repeated confirmation calls idempotently.

        Re-confirming an already-confirmed memory does not create a second
        memory or flip lifecycle state; it refreshes ``last_confirmed_at``
        (the staleness sweep's freshness signal) and notes the refresh with a
        revision. Re-rejecting an already-rejected/expired confirmation is a
        no-op replay. Mismatched actions still raise in the caller.
        """
        status = confirmation.get("status")
        if status == "confirmed" and action == "confirm":
            refreshed = self._refresh_last_confirmed(
                identity=identity,
                memory=memory,
                action="agentic_memory_reconfirm",
                reason="Repeated inline confirmation replayed; last_confirmed_at refreshed.",
                metadata={"confirmation_id": confirmation_id, "idempotent_replay": True},
            )
            return {
                "status": "committed",
                "write_mode": "confirm_inline",
                "confirmation_id": confirmation_id,
                "memory": refreshed,
                "idempotent_replay": True,
            }
        if status in {"rejected", "expired"} and action == "reject":
            return {
                "status": "rejected",
                "write_mode": "confirm_inline",
                "confirmation_id": confirmation_id,
                "memory": memory,
                "idempotent_replay": True,
            }
        return None

    def _refresh_last_confirmed(
        self,
        *,
        identity: AgentIdentity | None,
        memory: VNextRow,
        action: str,
        reason: str,
        metadata: JsonObject | None = None,
    ) -> VNextRow:
        """Set ``last_confirmed_at`` to now on an accepted memory.

        Every confirm/accept path must bump ``last_confirmed_at`` because the
        staleness sweep reads it as the freshness signal for working-state
        memory types. The write is idempotent (repeating it only moves the
        timestamp forward) and is always noted with a revision.
        """
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        now = _utc_iso()
        updated = self.store.update_memory(
            memory_id=str(memory["id"]),
            patch={"last_confirmed_at": now, "last_reviewed_at": now},
            actor_type=actor_type,
        )
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": "edited",
                "action": action,
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": str(updated.get("canonical_text") or ""),
                "reason": reason,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {
                    **(metadata or {}),
                    "last_confirmed_at_refreshed": True,
                    "last_confirmed_at": now,
                },
            },
            actor_type=actor_type,
        )
        return updated

    def undo(
        self,
        *,
        identity: AgentIdentity | None,
        memory_id: str | None = None,
        reason: str | None = None,
        superseded_by_memory_id: str | None = None,
    ) -> JsonObject:
        """Retire a commit; optionally link the memory that replaces it.

        When ``superseded_by_memory_id`` names the replacement (the usual
        replace-then-undo correction pattern), both rows get real
        supersession pointer columns -- ``superseded_by`` on the retired
        row and ``supersedes`` on the replacement -- plus metadata_json
        copies for backward compatibility, so the supersession chain in
        audit/explain can answer "what did I believe before".
        """
        # Every lifecycle mutation shares the same graph -> row boundary,
        # including undo without an explicit successor because it invalidates
        # derived consolidation candidates.
        self.lock_supersession_graph()
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        memory = self.store.get_memory(memory_id) if memory_id else self._latest_agentic_commit(identity)
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        if callable(get_memory_for_update):
            locked = get_memory_for_update(str(memory["id"]))
            if locked is None:
                raise VNextMemoryCommitValidationError("memory was not found")
            memory = locked
        _require_project_update_decision_path(memory)
        self._policy_checked_write(identity=identity, action="memory.undo", memory=memory)
        successor: VNextRow | None = None
        if superseded_by_memory_id is not None:
            successor = (
                get_memory_for_update(superseded_by_memory_id)
                if callable(get_memory_for_update)
                else self.store.get_memory(superseded_by_memory_id)
            )
            if successor is None:
                raise VNextMemoryCommitValidationError("superseding memory was not found")
            if str(successor["id"]) == str(memory["id"]):
                raise VNextMemoryCommitValidationError("a memory cannot supersede itself")
            _require_project_update_decision_path(successor)
            self._policy_checked_write(identity=identity, action="memory.undo", memory=successor)
        # A retirement always completes (owner ruling R2): a reason carrying
        # credential material is stored as the fixed placeholder.
        reason_text, rationale_withheld = withhold_credential_text(reason or "Agentic memory commit undone.")
        result = self._transition_memory(
            identity=identity,
            memory=memory,
            operation=UNDO,
            lifecycle_status="undone",
            next_status="superseded",
            event_type="agent.memory_undone",
            revision_type="superseded",
            action="agentic_memory_undo",
            reason=reason_text or "Agentic memory commit undone.",
            superseded_by=successor,
        )
        result["rationale_withheld"] = rationale_withheld or bool(result.get("rationale_withheld"))
        return result

    def correct(
        self,
        *,
        identity: AgentIdentity | None,
        memory_id: str,
        canonical_text: str,
        reason: str | None = None,
    ) -> JsonObject:
        self.lock_supersession_graph()
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        memory = (
            get_memory_for_update(memory_id) if callable(get_memory_for_update) else self.store.get_memory(memory_id)
        )
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        _require_project_update_decision_path(memory)
        self._policy_checked_write(identity=identity, action="memory.correct", memory=memory)
        # Retirement is terminal: correcting a superseded/rejected/archived row
        # is rejected here through the central transition table (which also
        # keeps the message stable for callers that match on it).
        self._require_transition(CORRECT, str(memory.get("status") or ""))
        metadata = _memory_metadata(memory)
        if is_pending_consolidation_candidate(memory):
            raise VNextMemoryCommitValidationError(
                "consolidation candidates must be approved through accept_consolidation"
            )
        next_text = _normalized_text(canonical_text, field_name="canonical_text")
        # The credential floor, before anything is written, on the row as it
        # will be stored. Until 2026-09-22 correct() rewrote title and text
        # with no credential check, so a secret the commit gate rejects was
        # stored active and recalled. A correction leaves the row active, so
        # it is an activation: title and summary are derived from the new
        # text and dropped as copies; the reason is persisted in the
        # corrections history and the revision, so it is read with the row.
        refuse_credential_activation(
            next_text[:120], next_text, next_text[:280], reason, error=VNextMemoryCommitValidationError
        )
        self._invalidate_pending_derived_candidates(
            member_id=str(memory["id"]),
            identity=identity,
            reason="source memory was corrected after the derived candidate was proposed",
        )
        agentic = _agentic_metadata(memory)
        now = _utc_iso()
        correction = {"corrected_at": now, "reason": reason, "previous_text": memory.get("canonical_text")}
        corrections_value = agentic.get("corrections")
        history = list(corrections_value) if isinstance(corrections_value, list) else []
        history.append(correction)
        agentic["corrections"] = history
        agentic["lifecycle_status"] = "corrected"
        # A correction that promotes a candidate/needs_review row to active is
        # an explicit accept of the new text: the row must not stay unconfirmed
        # or review_required, or later gates see an inconsistent active state.
        agentic["requires_dashboard_review"] = False
        next_metadata: JsonObject = {**metadata, "review_required": False, "agentic_memory": agentic}
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        current_value = memory.get("value")
        value_payload = dict(current_value) if isinstance(current_value, dict) else {}
        updated = self.store.update_memory(
            memory_id=str(memory["id"]),
            patch={
                "status": "active",
                "confirmation_status": "confirmed",
                "title": next_text[:120],
                "canonical_text": next_text,
                "summary": next_text[:280],
                "value": {**value_payload, "text": next_text},
                "metadata_json": next_metadata,
                # A correction is an explicit accept of the new text, so it
                # refreshes the staleness sweep's freshness signal.
                "last_confirmed_at": now,
                "last_reviewed_at": now,
            },
            actor_type=actor_type,
        )
        self._refresh_memory_derived_state(
            memory=updated,
            identity=identity,
            trace_id=str(agentic.get("trace_id") or "") or None,
            stage="correction",
            replace_entity_links=True,
        )
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": "corrected",
                "action": "agentic_memory_correct",
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": next_text,
                "reason": reason or "Agentic memory correction.",
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {"lifecycle_status": "corrected", "last_confirmed_at_refreshed": True},
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type="agent.memory_corrected",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(updated["id"]),
            payload={"lifecycle_status": "corrected"},
        )
        return {"status": "committed", "write_mode": "commit", "memory": updated}

    def forget(
        self,
        *,
        identity: AgentIdentity | None,
        memory_id: str,
        reason: str | None = None,
    ) -> JsonObject:
        self.lock_supersession_graph()
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        memory = (
            get_memory_for_update(memory_id) if callable(get_memory_for_update) else self.store.get_memory(memory_id)
        )
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        _require_project_update_decision_path(memory)
        self._policy_checked_write(identity=identity, action="memory.forget", memory=memory)
        # A retirement always completes (owner ruling R2).
        reason_text, rationale_withheld = withhold_credential_text(reason or "Agentic memory forgotten.")
        result = self._transition_memory(
            identity=identity,
            memory=memory,
            operation=FORGET,
            lifecycle_status="forgotten",
            next_status="superseded",
            event_type="agent.memory_forgotten",
            revision_type="archived",
            action="agentic_memory_forget",
            reason=reason_text or "Agentic memory forgotten.",
        )
        result["rationale_withheld"] = rationale_withheld or bool(result.get("rationale_withheld"))
        return result

    def accept_consolidation_candidate(
        self,
        memory_id: str,
        *,
        reason: str,
        identity: AgentIdentity | None = None,
    ) -> JsonObject:
        """Accept a consolidation candidate and execute the proposed merge.

        Acceptance is the promotion decision the consolidation pipeline left
        to reviewers: the candidate is promoted to active (freshness signals
        refreshed, revision 'promoted') and every ``proposed_supersede``
        member is superseded by the accepted row through the existing
        supersession transition (real ``superseded_by`` pointer column,
        status flip, revision, event per member).

        Pointer semantics (``supersedes`` is a single-valued column):

        - ``dedup`` proposals copy the survivor's text verbatim. The accepted
          candidate becomes the one active representative, supersedes every
          original member, and points its single-valued ``supersedes`` column
          at ``survivor_memory_id`` as the canonical content lineage.
        - ``merge`` proposals synthesize new text from every member, so no
          single-member pointer is honest: ``supersedes`` stays NULL and the
          full member list is recorded in ``metadata_json.merged_from``.

        Acceptance is a review decision, so an agent identity is policy
        checked under ``memory.accept_consolidation`` (human reviewers pass ``identity=None``;
        only admin agents may accept). Rejection stays the existing dashboard
        review path -- nothing here handles it. Replaying an acceptance is a
        no-op with a note.
        """
        # Acceptance may retire several graph members. Serialize the graph
        # before locking the candidate or any member rows so every path uses
        # one deadlock-safe advisory-lock -> row-lock order.
        self.lock_supersession_graph()
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        memory = (
            get_memory_for_update(memory_id) if callable(get_memory_for_update) else self.store.get_memory(memory_id)
        )
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        decision = self._policy_checked_write(
            identity=identity,
            action="memory.accept_consolidation",
            memory=memory,
        )
        metadata = _memory_metadata(memory)
        consolidation_raw = metadata.get("consolidation")
        if not isinstance(consolidation_raw, Mapping):
            raise VNextMemoryCommitValidationError("memory is not a consolidation candidate")
        consolidation = dict(consolidation_raw)
        accepted_record = consolidation.get("accepted")
        if isinstance(accepted_record, Mapping):
            return {
                "status": "accepted",
                "memory": memory,
                "proposal_kind": consolidation.get("proposal_kind"),
                "superseded_member_ids": list(accepted_record.get("superseded_member_ids") or []),
                "skipped_members": list(accepted_record.get("skipped_members") or []),
                "supersedes": memory.get("supersedes") or metadata.get("supersedes"),
                "policy_decision": decision.to_record(),
                "idempotent_replay": True,
                "note": "consolidation candidate was already accepted; replay changed nothing",
            }
        status = str(memory.get("status") or "")
        invalidated_record = consolidation.get("invalidated")
        if status == "stale" and isinstance(invalidated_record, Mapping):
            raise VNextMemoryCommitValidationError("consolidation candidate is stale; regenerate it before acceptance")
        if status not in CONSOLIDATION_ACCEPTABLE_STATUSES:
            raise VNextMemoryCommitValidationError(
                "consolidation candidate must be in candidate or needs_review status"
            )
        reason_text = _normalized_text(reason, field_name="reason")
        # The activation check (ruling C2), before any member is superseded:
        # acceptance makes the candidate's text searchable, and the reason is
        # persisted with it in the metadata, the revision and the event.
        refuse_credential_activation(
            memory.get("title"),
            memory.get("canonical_text"),
            memory.get("summary"),
            reason_text,
            error=VNextMemoryCommitValidationError,
        )
        accepted_id = str(memory["id"])
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        proposal_kind = str(consolidation.get("proposal_kind") or "dedup")
        survivor_raw = consolidation.get("survivor_memory_id")
        survivor_id = str(survivor_raw) if survivor_raw else None
        member_ids = [str(value) for value in (consolidation.get("cluster_member_ids") or []) if value]
        proposed = [
            member_id
            for member_id in dict.fromkeys(
                str(value) for value in (consolidation.get("proposed_supersede") or []) if value
            )
            if member_id != accepted_id
        ]
        if proposal_kind == "dedup":
            # Legacy v0.10.2 proposals omitted the survivor from this list,
            # which promoted a duplicate active copy. Derive the coherent
            # reviewed transition from the authoritative member list.
            proposed = [member_id for member_id in dict.fromkeys(member_ids) if member_id != accepted_id]

        # Lock and validate the entire reviewed input set before the first
        # supersession. A correction, retirement, or content edit after the
        # proposal was generated invalidates the review decision; the operator
        # must regenerate rather than apply stale intent.
        snapshot_rows = consolidation.get("member_snapshots")
        candidate_kind = metadata.get("candidate_kind")
        strict_snapshots = isinstance(candidate_kind, str) and candidate_kind in DERIVED_CONSOLIDATION_CANDIDATE_KINDS
        snapshots = (
            {
                str(snapshot.get("id")): snapshot
                for snapshot in snapshot_rows
                if isinstance(snapshot, Mapping) and snapshot.get("id") is not None
            }
            if isinstance(snapshot_rows, list)
            else {}
        )
        if strict_snapshots and not snapshots:
            raise VNextMemoryCommitValidationError(
                "consolidation candidate lacks member version snapshots; regenerate it before acceptance"
            )
        dependency_ids = list(dict.fromkeys([*member_ids, *proposed]))
        if strict_snapshots and (
            not member_ids
            or not isinstance(snapshot_rows, list)
            or len(snapshot_rows) != len(dependency_ids)
            or set(snapshots) != set(dependency_ids)
        ):
            raise VNextMemoryCommitValidationError(
                "consolidation candidate has incomplete member version snapshots; regenerate it before acceptance"
            )
        locked_members: dict[str, VNextRow] = {}
        for member_id in dependency_ids:
            member = (
                get_memory_for_update(member_id)
                if callable(get_memory_for_update)
                else self.store.get_memory(member_id)
            )
            if member is None:
                if strict_snapshots:
                    raise VNextMemoryCommitValidationError(
                        f"consolidation candidate is stale: member {member_id} is missing"
                    )
                continue
            locked_members[member_id] = member
            if member_id in proposed:
                self._policy_checked_write(
                    identity=identity,
                    action="memory.accept_consolidation",
                    memory=member,
                )
            if strict_snapshots:
                snapshot = snapshots.get(member_id)
                if snapshot is None or not memory_matches_snapshot(member, snapshot):
                    raise VNextMemoryCommitValidationError(
                        f"consolidation candidate is stale: member {member_id} changed; regenerate it"
                    )

        if strict_snapshots and dependency_ids:
            member_scope_keys = {
                project_scope_identity(resource_project_scope(member)) for member in locked_members.values()
            }
            candidate_scope_key = project_scope_identity(resource_project_scope(memory))
            if len(member_scope_keys) != 1 or candidate_scope_key not in member_scope_keys:
                raise VNextMemoryCommitValidationError(
                    "consolidation candidate crosses project scopes; regenerate it before acceptance"
                )

        # Supersede the members before stamping the acceptance marker so a
        # crash mid-way replays safely: already-superseded members are
        # skipped, then promotion completes.
        superseded_member_ids: list[str] = []
        skipped_members: list[JsonObject] = []
        for member_id in proposed:
            member = locked_members.get(member_id)
            if member is None:
                skipped_members.append({"member_id": member_id, "state": "missing"})
                continue
            if str(member.get("status") or "") == "superseded" or _supersession_pointer(member, "superseded_by"):
                skipped_members.append({"member_id": member_id, "state": "already_superseded"})
                continue
            self._transition_memory(
                identity=identity,
                memory=member,
                operation=SUPERSEDE_MEMBER,
                lifecycle_status="superseded_by_consolidation",
                next_status="superseded",
                event_type="agent.memory_superseded",
                revision_type="superseded",
                action="agentic_memory_consolidation_supersede",
                reason=f"Superseded by accepted consolidation candidate {accepted_id}.",
                superseded_by=memory,
                # The accepted row's single-valued supersedes pointer is set
                # once below by the documented dedup/merge rule, not
                # clobbered per member here.
                set_successor_pointer=False,
                exclude_derived_candidate_id=accepted_id,
                allow_pending_consolidation_successor=True,
            )
            superseded_member_ids.append(member_id)

        now = _utc_iso()
        consolidation["accepted"] = {
            "accepted_at": now,
            "actor_type": actor_type,
            "accepted_by": actor_id,
            "reason": reason_text,
            "superseded_member_ids": superseded_member_ids,
            "skipped_members": skipped_members,
        }
        next_metadata: JsonObject = {**metadata, "consolidation": consolidation, "review_required": False}
        patch: JsonObject = {
            "status": "active",
            "confirmation_status": "confirmed",
            "last_confirmed_at": now,
            "last_reviewed_at": now,
            "metadata_json": next_metadata,
        }
        supersedes_pointer: str | None = None
        if proposal_kind == "dedup" and survivor_id and survivor_id != accepted_id:
            supersedes_pointer = survivor_id
            patch["supersedes"] = survivor_id
            # metadata_json copy mirrors the _transition_memory convention.
            next_metadata["supersedes"] = survivor_id
        else:
            next_metadata["merged_from"] = member_ids
        updated = self.store.update_memory(memory_id=accepted_id, patch=patch, actor_type=actor_type)
        self._refresh_memory_derived_state(
            memory=updated,
            identity=identity,
            trace_id=decision.trace_id,
            stage="consolidation_accepted",
            replace_entity_links=True,
        )
        self.store.append_revision(
            {
                "memory_id": accepted_id,
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": "promoted",
                "action": "agentic_memory_consolidation_accept",
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": str(updated.get("canonical_text") or ""),
                "reason": reason_text,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {
                    "proposal_kind": proposal_kind,
                    "consolidation_digest": metadata.get("consolidation_digest"),
                    "superseded_member_ids": superseded_member_ids,
                    "skipped_members": skipped_members,
                    "supersedes": supersedes_pointer,
                    "last_confirmed_at_refreshed": True,
                },
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type="agent.memory_consolidation_accepted",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=accepted_id,
            trace_id=decision.trace_id,
            payload={
                "proposal_kind": proposal_kind,
                "superseded_member_ids": superseded_member_ids,
                "skipped_members": skipped_members,
                "supersedes": supersedes_pointer,
                "reason": reason_text,
            },
        )
        return {
            "status": "accepted",
            "memory": updated,
            "proposal_kind": proposal_kind,
            "superseded_member_ids": superseded_member_ids,
            "skipped_members": skipped_members,
            "supersedes": supersedes_pointer,
            "policy_decision": decision.to_record(),
            "idempotent_replay": False,
        }

    def expire(
        self,
        memory_id: str,
        *,
        valid_to: object | None = None,
        reason: str,
        identity: AgentIdentity | None = None,
    ) -> JsonObject:
        """Close a memory's validity window without retiring the row.

        Sets ``valid_to`` (default now) so the read-path exclusion
        (``valid_to IS NULL OR valid_to >= now``) applies immediately. The
        row's status stays ``active``: expiry is temporal, not a lifecycle
        judgment -- the staleness sweep later marks long-expired rows stale.
        Audited with an 'edited' revision noting 'expired' plus an
        ``agent.memory_expired`` event, and policy checked like undo/forget
        when an agent identity is present.

        The row is locked (``SELECT ... FOR UPDATE`` on Postgres, the writer
        lock on SQLite) BEFORE policy evaluation and metadata derivation, so a
        concurrent correction/supersession cannot be overwritten by a stale
        snapshot.
        """
        self.lock_supersession_graph()
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        memory = (
            get_memory_for_update(memory_id) if callable(get_memory_for_update) else self.store.get_memory(memory_id)
        )
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        decision = self._policy_checked_write(identity=identity, action="memory.expire", memory=memory)
        self._require_transition(EXPIRE, str(memory.get("status") or ""))
        # A retirement always completes (owner ruling R2): the reason is
        # withheld once, here, before the metadata, the revision and the event.
        reason_text, rationale_withheld = withhold_credential_text(_normalized_text(reason, field_name="reason"))
        reason_text = reason_text or ""
        valid_to_iso = _valid_to_iso(valid_to)
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        now = _utc_iso()
        metadata = _memory_metadata(memory)
        validity_value = metadata.get("validity")
        validity = dict(validity_value) if isinstance(validity_value, Mapping) else {}
        validity_history_value = validity.get("history")
        history, history_withheld = _withheld_history(
            list(validity_history_value) if isinstance(validity_history_value, list) else []
        )
        history.append(
            {"op": "expired", "at": now, "valid_to": valid_to_iso, "reason": reason_text, "actor_id": actor_id}
        )
        validity.update({"state": "expired", "expired_at": now, "valid_to": valid_to_iso, "history": history})
        validity.pop("unbounded_sentinel", None)
        updated = self.store.update_memory(
            memory_id=str(memory["id"]),
            patch={
                "valid_to": valid_to_iso,
                "last_reviewed_at": now,
                "metadata_json": {**metadata, "validity": validity},
            },
            actor_type=actor_type,
        )
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": "edited",
                "action": "agentic_memory_expire",
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": str(updated.get("canonical_text") or ""),
                "reason": reason_text,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {"note": "expired", "valid_to": valid_to_iso},
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type="agent.memory_expired",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(updated["id"]),
            trace_id=decision.trace_id,
            payload={"valid_to": valid_to_iso, "reason": reason_text},
        )
        return {
            "status": "expired",
            "memory": updated,
            "valid_to": valid_to_iso,
            "policy_decision": decision.to_record(),
            "rationale_withheld": rationale_withheld or history_withheld,
        }

    def unexpire(
        self,
        memory_id: str,
        *,
        reason: str,
        identity: AgentIdentity | None = None,
    ) -> JsonObject:
        """Reopen an expired memory's validity window, with audit.

        Clears ``valid_to`` so the read-path exclusion stops hiding the row.
        Stores whose ``update_memory`` COALESCEs every column cannot write
        NULL back; for those the far-future ``VALID_TO_UNBOUNDED_SENTINEL``
        is written instead (read-path equivalent to NULL) and noted in
        ``metadata_json.validity.unbounded_sentinel``. A row that is not
        expired replays as a no-op with a note.

        The row is locked BEFORE policy evaluation and metadata derivation so a
        concurrent correction/supersession cannot be overwritten by a stale
        snapshot. A row the staleness sweep marked ``stale`` for an expired
        window is restored to a retrievable ``active`` state, so the reported
        status matches reality rather than leaving it stale and unretrievable.
        """
        self.lock_supersession_graph()
        get_memory_for_update = getattr(self.store, "get_memory_for_update", None)
        memory = (
            get_memory_for_update(memory_id) if callable(get_memory_for_update) else self.store.get_memory(memory_id)
        )
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        decision = self._policy_checked_write(
            identity=identity,
            action="memory.unexpire",
            memory=memory,
        )
        status = str(memory.get("status") or "")
        restored_status = self._require_transition(UNEXPIRE, status)
        reason_text = _normalized_text(reason, field_name="reason")
        current_valid_to = memory.get("valid_to")
        if not current_valid_to or _is_unbounded_valid_to(current_valid_to):
            return {
                "status": status,
                "memory": memory,
                "idempotent_replay": True,
                "policy_decision": decision.to_record(),
                "note": "memory has no validity end to clear; replay changed nothing",
            }
        # Clearing valid_to makes the row retrievable again whether or not
        # its status changes, so unexpire is an activation (owner ruling R2):
        # one call over the row text and the reason, before anything is
        # written.
        refuse_credential_activation(
            memory.get("title"),
            memory.get("canonical_text"),
            memory.get("summary"),
            reason_text,
            error=VNextMemoryCommitValidationError,
        )
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        now = _utc_iso()
        metadata = _memory_metadata(memory)
        validity_value = metadata.get("validity")
        validity = dict(validity_value) if isinstance(validity_value, Mapping) else {}
        validity_history_value = validity.get("history")
        history, history_withheld = _withheld_history(
            list(validity_history_value) if isinstance(validity_history_value, list) else []
        )
        history.append({"op": "unexpired", "at": now, "reason": reason_text, "actor_id": actor_id})
        validity.update({"state": "cleared", "unexpired_at": now, "valid_to": None, "history": history})
        validity.pop("unbounded_sentinel", None)
        unexpire_patch: JsonObject = {
            "valid_to": None,
            "last_reviewed_at": now,
            "metadata_json": {**metadata, "validity": validity},
        }
        # A row swept ``stale`` by an expired window is retrievable again once
        # the window is cleared, so restore it to ``active`` to match the
        # reported status instead of leaving it stale and unretrievable.
        if restored_status != status:
            unexpire_patch["status"] = restored_status
        updated = self.store.update_memory(
            memory_id=str(memory["id"]),
            patch=unexpire_patch,
            actor_type=actor_type,
        )
        if updated.get("valid_to") and not _is_unbounded_valid_to(updated.get("valid_to")):
            # COALESCE-style store: NULL cannot be written through the patch
            # surface, so fall back to the documented far-future sentinel.
            validity = {**validity, "unbounded_sentinel": VALID_TO_UNBOUNDED_SENTINEL}
            updated = self.store.update_memory(
                memory_id=str(memory["id"]),
                patch={
                    "valid_to": VALID_TO_UNBOUNDED_SENTINEL,
                    "metadata_json": {**metadata, "validity": validity},
                },
                actor_type=actor_type,
            )
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": "edited",
                "action": "agentic_memory_unexpire",
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": str(updated.get("canonical_text") or ""),
                "reason": reason_text,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {"note": "unexpired", "previous_valid_to": json_safe(current_valid_to)},
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type="agent.memory_unexpired",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(updated["id"]),
            trace_id=decision.trace_id,
            payload={"previous_valid_to": json_safe(current_valid_to), "reason": reason_text},
        )
        return {
            "status": str(updated.get("status") or restored_status),
            "memory": updated,
            "policy_decision": decision.to_record(),
            "idempotent_replay": False,
            "rationale_withheld": history_withheld,
        }

    def _policy_checked_write(
        self,
        *,
        identity: AgentIdentity | None,
        action: str,
        memory: Mapping[str, object],
    ) -> PolicyDecision:
        """Mirror the undo/forget policy treatment for service-layer writes.

        Upserts the identity, evaluates the policy scoped to the target
        memory's domain/sensitivity, appends the policy audit events, and
        raises ``AgentPolicyBlockedError`` when blocked. ``identity=None``
        (human/system callers) always evaluates as allowed.
        """
        self._upsert_identity(identity)
        # memory.expire / memory.unexpire / memory.accept_consolidation are
        # in the agent-control WRITE_ACTIONS vocabulary, so
        # evaluate_agent_policy carries the read-only write block itself.
        decision = evaluate_agent_policy(
            identity=identity,
            action=action,
            domains=(str(memory.get("domain") or "unknown"),),
            sensitivity_allowed=(str(memory.get("sensitivity") or "unknown"),),
            project_scope=resource_project_scope(memory),
            require_explicit_project_scope=True,
        )
        append_policy_events(
            self.store,
            identity=identity,
            decision=decision,
            target_type="memory",
            target_id=str(memory.get("id")) if memory.get("id") is not None else None,
        )
        if decision.decision == "blocked":
            raise AgentPolicyBlockedError(decision)
        return decision

    def authorize_memory_action(
        self,
        *,
        identity: AgentIdentity | None,
        action: str,
        memory: Mapping[str, object],
    ) -> PolicyDecision:
        """Authorize a persisted memory target for a cross-surface lifecycle adapter."""

        return self._policy_checked_write(identity=identity, action=action, memory=memory)

    def auto_promoted_by_agent(
        self,
        *,
        agent_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[JsonObject]:
        """Every memory a named agent auto-promoted in a window.

        Read off the ``memory.auto_promoted`` events the write path already
        emits, which is the only record that distinguishes a write promotion
        actually changed from one that would have committed anyway. Ordinary
        reviewed writes and human writes carry no such event and are
        therefore unreachable from here.
        """

        normalized_agent = " ".join(str(agent_id).split()).strip()
        if not normalized_agent:
            raise VNextMemoryCommitValidationError("agent_id is required")

        events = self.store.list_events(
            target_type="memory",
            occurred_at_start=since,
            occurred_at_end=until,
        )
        seen: set[str] = set()
        rows: list[JsonObject] = []
        for event in events:
            if str(event.get("event_type") or "") != AUTO_PROMOTED_EVENT:
                continue
            if str(event.get("actor_id") or "") != normalized_agent:
                continue
            target_id = event.get("target_id")
            if not target_id or str(target_id) in seen:
                continue
            seen.add(str(target_id))
            rows.append({"memory_id": str(target_id), "event": event})
        return rows

    def quarantine_by_agent_key(
        self,
        *,
        identity: AgentIdentity | None,
        agent_id: str,
        reason: str,
        since: datetime | None = None,
        until: datetime | None = None,
        dry_run: bool = False,
    ) -> JsonObject:
        """Expire everything one agent key auto-promoted in a window.

        The control a compromised key needs and did not have. Revoking a key
        stops the next write; it does nothing about the rows already written,
        whose blast radius was otherwise unbounded in both count and time,
        with no sweep and no way to find them except by hand.

        Four properties keep the sweep from becoming an attack of its own.

        It is operator-only. ``memory.quarantine`` is in
        ``HUMAN_OR_ADMIN_ACTIONS``, so the policy engine blocks every agent
        below ``admin_agent``, and it is in ``WRITE_ACTIONS``, so a read-only
        agent is blocked before that.

        It cannot reach anything it did not cause. The candidate set comes
        from ``memory.auto_promoted`` events attributed to that agent id, so
        a reviewed write, a human write, or another agent's write is not
        addressable by this call at all.

        It erases nothing. Rows are expired, not deleted or redacted: the
        row, its revisions, its provenance and its events all survive, and
        ``memory.unexpire`` reverses it row by row. The sweep records the
        exact set it acted on, so the reversal set is knowable afterwards.

        It can be inspected first. ``dry_run`` returns the same envelope
        without writing.
        """

        decision = evaluate_agent_policy(
            identity=identity,
            action="memory.quarantine",
            promotion_settings=None,
        )
        append_policy_events(self.store, identity=identity, decision=decision)
        if decision.decision == "blocked":
            raise AgentPolicyBlockedError(decision)

        reason_text = " ".join(str(reason).split()).strip()
        if not reason_text:
            raise VNextMemoryCommitValidationError("reason is required")
        # A retirement always completes (owner ruling R2). Withheld once here,
        # so the per-row expiries and the memory.quarantine_sweep event both
        # carry the placeholder.
        withheld_reason, rationale_withheld = withhold_credential_text(reason_text)
        reason_text = withheld_reason or reason_text

        normalized_agent = " ".join(str(agent_id).split()).strip()
        if identity is not None and normalized_agent != identity.agent_id:
            # An admin key may clean up after itself and nothing else. Letting
            # one agent sweep another's writes would make the control a way to
            # bury somebody else's memories, which is the attack an
            # operator-only action has to rule out. Sweeping an arbitrary key
            # is the human operator's to do.
            raise AgentPolicyBlockedError(
                replace(
                    decision,
                    decision="blocked",
                    reasons=tuple(
                        dict.fromkeys((*decision.reasons, "quarantine_limited_to_own_agent_id"))
                    ),
                )
            )

        candidates = self.auto_promoted_by_agent(agent_id=normalized_agent, since=since, until=until)
        sweep_id = f"quarantine-{uuid4()}"
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None

        expired: list[JsonObject] = []
        skipped: list[JsonObject] = []
        for candidate in candidates:
            memory_id = str(candidate["memory_id"])
            memory = self.store.get_memory(memory_id)
            if memory is None:
                skipped.append({"memory_id": memory_id, "reason": "memory_not_found"})
                continue
            if _is_unbounded_valid_to(memory.get("valid_to")) is False and memory.get("valid_to") is not None:
                skipped.append({"memory_id": memory_id, "reason": "already_expired"})
                continue
            if dry_run:
                expired.append({"memory_id": memory_id, "status": "would_expire"})
                continue
            try:
                self.expire(memory_id, reason=f"{reason_text} (sweep {sweep_id})", identity=identity)
            except (VNextMemoryCommitValidationError, LifecycleTransitionError) as exc:
                skipped.append({"memory_id": memory_id, "reason": str(exc)})
                continue
            expired.append({"memory_id": memory_id, "status": "expired"})

        envelope: JsonObject = {
            "sweep_id": sweep_id,
            "agent_id": normalized_agent,
            "since": _utc_iso(since) if since is not None else None,
            "until": _utc_iso(until) if until is not None else None,
            "dry_run": dry_run,
            "candidate_count": len(candidates),
            "expired": expired,
            "skipped": skipped,
            "reversible_via": "memory.unexpire",
            "policy_decision": decision.to_record(),
            "rationale_withheld": rationale_withheld,
        }
        if not dry_run:
            append_event(
                self.store,
                event_type="memory.quarantine_sweep",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="agent",
                    target_id=normalized_agent,
                trace_id=decision.trace_id,
                payload={
                    "sweep_id": sweep_id,
                    "reason": reason_text,
                    "since": envelope["since"],
                    "until": envelope["until"],
                    "expired_memory_ids": [row["memory_id"] for row in expired],
                    "skipped": skipped,
                    "reversible_via": "memory.unexpire",
                },
            )
        return envelope

    def recent_commits(self, *, limit: int = 20) -> JsonObject:
        rows = []
        for memory in self.store.list_memories(status=None):
            agentic = _agentic_metadata(memory)
            if agentic.get("kind") == "agentic_memory_commit":
                rows.append(memory)
            if len(rows) >= limit:
                break
        return {"recent_commits": rows, "count": len(rows)}

    def refresh_memory_derived_state(
        self,
        memory: Mapping[str, object],
        *,
        identity: AgentIdentity | None = None,
        trace_id: str | None = None,
        stage: str = "review_accepted",
    ) -> None:
        """Reconcile indexes after an accepted mutation from another surface.

        HTTP/MCP review adapters that still own their row transition call this
        seam after the accepted row is persisted. Keeping the derived-state
        lifecycle here prevents those adapters from forking embedding,
        fact-key, and entity-edge semantics.
        """
        self._refresh_memory_derived_state(
            memory=memory,
            identity=identity,
            trace_id=trace_id,
            stage=stage,
            # Bundled stores expire old derived edges transactionally in the
            # memory content-update trigger. Cross-surface callers reach this
            # hook after that update; they only need to link the new state.
            replace_entity_links=False,
        )

    def audit(
        self,
        *,
        memory_id: str,
        authorize_memory: Callable[[Mapping[str, object]], None] | None = None,
    ) -> JsonObject:
        """Return one memory's audit envelope after authorizing its full chain.

        ``alice_explain`` can be called by a project-bound agent.  The root
        memory is not the whole disclosure boundary: predecessor/successor
        nodes are expanded into the response as well.  Letting the adapter
        provide an authorizer keeps policy ownership at that boundary while
        ensuring every fetched chain node is checked before traversal follows
        any of its pointers or the wider audit envelope is assembled.
        """
        memory = self.store.get_memory(memory_id)
        if memory is None:
            raise VNextMemoryCommitValidationError("memory was not found")
        if authorize_memory is not None:
            authorize_memory(memory)
        return {
            "memory": memory,
            "supersession_chain": self._supersession_chain(
                memory,
                authorize_memory=authorize_memory,
            ),
            "revisions": self.store.list_revisions(memory_id),
            "events": self.store.list_events(target_type="memory", target_id=memory_id),
            "provenance_links": self.store.list_provenance_links(target_type="memory", target_id=memory_id),
        }

    def _supersession_chain(
        self,
        memory: VNextRow,
        *,
        authorize_memory: Callable[[Mapping[str, object]], None] | None = None,
    ) -> list[JsonObject]:
        """Full replacement history around ``memory``, oldest to newest.

        Walks the ``supersedes`` pointers backwards and the
        ``superseded_by`` pointers forwards, bounded to
        ``_SUPERSESSION_CHAIN_MAX_DEPTH`` hops per direction and cycle-safe
        (a pointer to an already-visited row ends the walk). A memory with
        no supersession history yields a single 'self' entry.
        """

        def _entry(row: VNextRow, relation: str) -> JsonObject:
            return {
                "id": str(row.get("id")),
                "title": row.get("title"),
                "status": row.get("status"),
                "created_at": json_safe(row.get("created_at")),
                "relation": relation,
            }

        seen: set[str] = {str(memory.get("id"))}

        def _walk(key: str) -> list[VNextRow]:
            rows: list[VNextRow] = []
            current: VNextRow = memory
            for _hop in range(_SUPERSESSION_CHAIN_MAX_DEPTH):
                pointer = _supersession_pointer(current, key)
                if pointer is None or pointer in seen:
                    break
                row = self.store.get_memory(pointer)
                if row is None:
                    raise VNextMemoryCommitValidationError("memory supersession chain contains an unresolved pointer")
                if authorize_memory is not None:
                    authorize_memory(row)
                seen.add(pointer)
                rows.append(row)
                current = row
            return rows

        predecessors = _walk("supersedes")
        successors = _walk("superseded_by")
        return [
            *(_entry(row, "predecessor") for row in reversed(predecessors)),
            _entry(memory, "self"),
            *(_entry(row, "successor") for row in successors),
        ]

    def inline_confirmations(self, *, limit: int = 20) -> list[VNextRow]:
        rows: list[VNextRow] = []
        list_pending = getattr(self.store, "list_pending_inline_confirmations", None)
        candidates = list_pending(limit=limit) if callable(list_pending) else self.store.list_memories(status=None)
        for memory in candidates:
            agentic = _agentic_metadata(memory)
            confirmation = agentic.get("confirmation")
            if (
                agentic.get("kind") == "agentic_memory_commit"
                and isinstance(confirmation, Mapping)
                and confirmation.get("status") == "pending"
                and memory.get("status") == "needs_review"
            ):
                rows.append(memory)
            if len(rows) >= limit:
                break
        return rows

    def _upsert_identity(self, identity: AgentIdentity | None) -> None:
        if identity is None:
            return
        self.store.upsert_agent_identity(
            {
                "agent_id": identity.agent_id,
                "agent_type": identity.agent_type,
                "permission_profile": identity.permission_profile,
                "project_scope_json": list(identity.project_scope),
                "metadata_json": {"last_agent_run_id": identity.agent_run_id, "last_task_id": identity.task_id},
            },
            actor_type="agent",
        )

    def _idempotent_memory(self, idempotency_key: str | None) -> VNextRow | None:
        if idempotency_key is None:
            return None
        get_memory_by_commit_digest = getattr(self.store, "get_memory_by_commit_digest", None)
        if callable(get_memory_by_commit_digest):
            return get_memory_by_commit_digest(idempotency_key)
        for memory in self.store.list_memories(status=None):
            if _agentic_metadata(memory).get("idempotency_key") == idempotency_key:
                return memory
        return None

    def _idempotent_replay(
        self,
        *,
        memory: VNextRow,
        request: MemoryCommitRequest,
        identity: AgentIdentity | None,
    ) -> JsonObject:
        self._policy_checked_write(identity=identity, action="memory.commit", memory=memory)
        self._assert_idempotent_request_matches(memory=memory, request=request)
        agentic = _agentic_metadata(memory)
        decision_record = agentic.get("policy_decision")
        return {
            "status": agentic.get("status") or "committed",
            "write_mode": agentic.get("write_mode") or "commit",
            "memory": memory,
            "idempotent_replay": True,
            "policy_decision": decision_record if isinstance(decision_record, dict) else {},
        }

    def _assert_idempotent_request_matches(
        self,
        *,
        memory: Mapping[str, object],
        request: MemoryCommitRequest,
    ) -> None:
        agentic = _agentic_metadata(memory)
        stored_fingerprint = agentic.get("request_fingerprint")
        if isinstance(stored_fingerprint, str) and stored_fingerprint:
            if stored_fingerprint != _request_fingerprint(request):
                raise VNextMemoryCommitValidationError(
                    "idempotency_key was already used for a different memory request"
                )
            return

        # Compatibility check for rows written before request fingerprints.
        # Prefer a conservative rejection over replaying unrelated content.
        legacy_checks = (
            (memory.get("title"), request.title),
            (memory.get("canonical_text"), request.canonical_text),
            (memory.get("memory_type"), request.memory_type),
            (memory.get("domain"), request.domain),
            (memory.get("sensitivity"), request.sensitivity),
            (agentic.get("intent"), request.intent),
            (agentic.get("source_type"), request.source_type),
            (agentic.get("project_scope"), list(request.project_scope)),
        )
        if any(json_safe(stored) != json_safe(expected) for stored, expected in legacy_checks):
            raise VNextMemoryCommitValidationError("idempotency_key was already used for a different memory request")

    def _create_memory_record(
        self,
        memory: JsonObject,
        *,
        actor_type: str,
        request: MemoryCommitRequest,
    ) -> VNextRow:
        try:
            return self.store.create_memory(memory, actor_type=actor_type)
        except ContinuityStoreInvariantError:
            # Both stores use INSERT .. ON CONFLICT DO NOTHING against the
            # unique commit-digest index. A concurrent winner therefore
            # leaves this transaction usable and becomes a verified replay.
            existing = self._idempotent_memory(request.idempotency_key)
            if existing is None:
                raise
            self._assert_idempotent_request_matches(memory=existing, request=request)
            raise _IdempotentReplaySignal(existing) from None

    def _base_metadata(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
        decision: MemoryCommitPolicyDecision,
        status: str,
        write_mode: str,
        lifecycle_status: str,
        confirmation: JsonObject | None = None,
    ) -> JsonObject:
        agentic: JsonObject = {
            "kind": "agentic_memory_commit",
            "status": status,
            "write_mode": write_mode,
            "lifecycle_status": lifecycle_status,
            "intent": request.intent,
            "source_type": request.source_type,
            "source_refs": json_safe(list(request.source_refs)),
            "conversation_excerpt": request.conversation_excerpt,
            "rationale": request.rationale,
            "idempotency_key": request.idempotency_key,
            "request_fingerprint": _request_fingerprint(request),
            "project_scope": list(request.project_scope),
            "contradiction_refs": list(request.contradiction_refs),
            "policy_decision": decision.to_record(),
            "trace_id": request.trace_id or decision.policy_decision.trace_id,
            "agent_identity": identity.to_record() if identity is not None else None,
        }
        if confirmation is not None:
            agentic["confirmation"] = confirmation
        return {
            "project_scope": list(request.project_scope),
            "agentic_memory": agentic,
            **agent_metadata(identity, decision.policy_decision),
        }

    def _memory_key(self, request: MemoryCommitRequest) -> str:
        suffix = request.idempotency_key or str(uuid4())
        return f"agentic_memory.{request.memory_type}.{suffix}"

    def _create_committed_memory(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
        decision: MemoryCommitPolicyDecision,
        confirmed_inline: bool,
    ) -> JsonObject:
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        metadata = self._base_metadata(
            identity=identity,
            request=request,
            decision=decision,
            status="committed",
            write_mode="confirm_inline" if confirmed_inline else "commit",
            lifecycle_status="inline_confirmed" if confirmed_inline else "auto_committed",
        )
        now = _utc_iso()
        memory = self._create_memory_record(
            {
                "memory_type": request.memory_type,
                "memory_key": self._memory_key(request),
                "value": {
                    "text": request.canonical_text,
                    "intent": request.intent,
                    "source_refs": json_safe(list(request.source_refs)),
                },
                "status": "active",
                "confirmation_status": "confirmed",
                "confidence": request.confidence,
                "title": request.title,
                "canonical_text": request.canonical_text,
                "summary": request.canonical_text[:280],
                "domain": request.domain,
                "sensitivity": request.sensitivity,
                "last_confirmed_at": now,
                "metadata_json": metadata,
                "commit_digest": request.idempotency_key,
                **_scope_columns(identity=identity, request=request),
            },
            actor_type=actor_type,
            request=request,
        )
        self._attach_or_defer_memory_embedding(
            memory,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=request.trace_id or decision.policy_decision.trace_id,
        )
        # Fact-key integration point (vnext_fact_keys): derived retrieval
        # keys ride the same best-effort post-create hook as embeddings.
        # use_env_provider=False keeps the commit path deterministic-tier
        # only -- commits never make a synchronous model call.
        attach_memory_fact_keys(self.store, memory, use_env_provider=False, actor_type=actor_type, actor_id=actor_id)
        self._append_revision(
            memory=memory,
            action="agentic_memory_confirmed" if confirmed_inline else "agentic_memory_commit",
            revision_type="promoted" if confirmed_inline else "created",
            reason=request.rationale or "Agentic memory committed from explicit user intent.",
            actor_type=actor_type,
            actor_id=actor_id,
        )
        self._create_provenance_links(memory=memory, request=request, actor_type=actor_type)
        self._link_memory_entities(
            memory=memory,
            identity=identity,
            trace_id=request.trace_id or decision.policy_decision.trace_id,
            stage="commit",
        )
        append_event(
            self.store,
            event_type="agent.memory_committed",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(memory["id"]),
            trace_id=request.trace_id or decision.policy_decision.trace_id,
            run_id=identity.agent_run_id if identity is not None else None,
            payload={
                "write_mode": "confirm_inline" if confirmed_inline else "commit",
                "policy_decision": decision.to_record(),
                "agent_identity": identity.to_record() if identity is not None else None,
            },
        )
        # One event per write that a human would otherwise have gated. This
        # is what makes the false-promotion rate countable after the fact
        # instead of being lost inside the general commit stream.
        if decision.promotion is not None and decision.promoted_from is not None:
            append_event(
                self.store,
                event_type="memory.auto_promoted",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="memory",
                target_id=str(memory["id"]),
                trace_id=request.trace_id or decision.policy_decision.trace_id,
                run_id=identity.agent_run_id if identity is not None else None,
                payload={
                    "promotion": decision.promotion.to_record(),
                    "promoted_from": decision.promoted_from,
                    "gated_status_without_promotion": "confirmation_required"
                    if decision.promoted_from == "confirm_inline"
                    else "review_required",
                    "reasons": list(decision.reasons),
                    "reversible_via": "memory.undo",
                },
            )
            # This is what separates ``team`` from ``personal``: the write
            # lands either way, but a team deployment also gets a review
            # entry it can work through after the fact. It carries
            # review_required False, so nothing is gated on it.
            if decision.promotion.review_is_advisory:
                append_event(
                    self.store,
                    event_type="review.item_created",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    target_type="memory",
                    target_id=str(memory["id"]),
                    trace_id=request.trace_id or decision.policy_decision.trace_id,
                    run_id=identity.agent_run_id if identity is not None else None,
                    payload={
                        "review_required": False,
                        "review_mode": "digest",
                        "proposal_type": "auto_promoted_memory",
                        "persona": decision.promotion.persona,
                        "promoted_from": decision.promoted_from,
                    },
                )
        return {
            "status": "committed",
            "write_mode": "confirm_inline" if confirmed_inline else "commit",
            "memory": memory,
            "policy_decision": decision.to_record(),
        }

    def _create_confirmation(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
        decision: MemoryCommitPolicyDecision,
    ) -> JsonObject:
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        now = _utc_now()
        confirmation_id = f"confirm-{uuid4()}"
        confirmation: JsonObject = {
            "confirmation_id": confirmation_id,
            "proposed_text": request.canonical_text,
            "domain": request.domain,
            "sensitivity": request.sensitivity,
            "policy_reason": decision.reason,
            "agent_id": identity.agent_id if identity is not None else None,
            "created_at": _utc_iso(now),
            "expires_at": _utc_iso(now + timedelta(hours=24)),
            "status": "pending",
        }
        metadata = self._base_metadata(
            identity=identity,
            request=request,
            decision=decision,
            status="confirmation_required",
            write_mode="confirm_inline",
            lifecycle_status="pending_inline_confirmation",
            confirmation=confirmation,
        )
        memory = self._create_memory_record(
            {
                "memory_type": request.memory_type,
                "memory_key": self._memory_key(request),
                "value": {
                    "text": request.canonical_text,
                    "intent": request.intent,
                    "source_refs": json_safe(list(request.source_refs)),
                },
                "status": "needs_review",
                "confirmation_status": "unconfirmed",
                "confidence": request.confidence,
                "title": request.title,
                "canonical_text": request.canonical_text,
                "summary": request.canonical_text[:280],
                "domain": request.domain,
                "sensitivity": request.sensitivity,
                "metadata_json": metadata,
                "commit_digest": request.idempotency_key,
                "confirmation_id": confirmation_id,
                **_scope_columns(identity=identity, request=request),
            },
            actor_type=actor_type,
            request=request,
        )
        self._attach_or_defer_memory_embedding(
            memory,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=request.trace_id or decision.policy_decision.trace_id,
        )
        self._append_revision(
            memory=memory,
            action="agentic_memory_confirmation_required",
            revision_type="created",
            reason=decision.reason,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        append_event(
            self.store,
            event_type="agent.memory_confirmation_required",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(memory["id"]),
            trace_id=request.trace_id or decision.policy_decision.trace_id,
            run_id=identity.agent_run_id if identity is not None else None,
            payload={"confirmation": confirmation, "policy_decision": decision.to_record()},
        )
        return {
            "status": "confirmation_required",
            "write_mode": "confirm_inline",
            "confirmation_id": confirmation_id,
            "confirmation": confirmation,
            "memory": memory,
            "policy_decision": decision.to_record(),
        }

    def _create_review_candidate(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
        decision: MemoryCommitPolicyDecision,
    ) -> JsonObject:
        # Deliberately NO entity linking here: review candidates await
        # dashboard review and must not seed the entity substrate until a
        # human accepts them. Inline confirmations link in confirm();
        # dashboard acceptance happens in the review flows outside this
        # module, which do not yet re-enter this service -- linking those
        # at acceptance time is a known gap until the review flow calls
        # back into an accept seam here.
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        metadata = self._base_metadata(
            identity=identity,
            request=request,
            decision=decision,
            status="review_required",
            write_mode="propose_review",
            lifecycle_status="pending_dashboard_review",
        )
        proposal_id = f"agentic-{uuid4()}"
        metadata["proposal_id"] = proposal_id
        metadata["proposal_type"] = "agentic_memory_commit"
        metadata["review_required"] = True
        memory = self._create_memory_record(
            {
                "memory_type": request.memory_type,
                "memory_key": self._memory_key(request),
                "value": {
                    "proposal_type": "agentic_memory_commit",
                    "text": request.canonical_text,
                    "source_refs": json_safe(list(request.source_refs)),
                    "rationale": request.rationale,
                },
                "status": "candidate",
                "confirmation_status": "unconfirmed",
                "confidence": request.confidence,
                "title": request.title,
                "canonical_text": request.canonical_text,
                "summary": request.canonical_text[:280],
                "domain": request.domain,
                "sensitivity": request.sensitivity,
                "metadata_json": metadata,
                "commit_digest": request.idempotency_key,
                **_scope_columns(identity=identity, request=request),
            },
            actor_type=actor_type,
            request=request,
        )
        self._attach_or_defer_memory_embedding(
            memory,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=request.trace_id or decision.policy_decision.trace_id,
        )
        self._append_revision(
            memory=memory,
            action="agentic_memory_review_required",
            revision_type="created",
            reason=decision.reason,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        append_event(
            self.store,
            event_type="agent.memory_review_required",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(memory["id"]),
            trace_id=request.trace_id or decision.policy_decision.trace_id,
            run_id=identity.agent_run_id if identity is not None else None,
            payload={"proposal_id": proposal_id, "policy_decision": decision.to_record()},
        )
        append_event(
            self.store,
            event_type="review.item_created",
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(memory["id"]),
            trace_id=request.trace_id or decision.policy_decision.trace_id,
            run_id=identity.agent_run_id if identity is not None else None,
            payload={"review_required": True, "proposal_type": "agentic_memory_commit"},
        )
        return {
            "status": "review_required",
            "write_mode": "propose_review",
            "proposal_id": proposal_id,
            "memory": memory,
            "policy_decision": decision.to_record(),
        }

    def _append_decision_event(
        self,
        *,
        identity: AgentIdentity | None,
        request: MemoryCommitRequest,
        decision: MemoryCommitPolicyDecision,
        target_id: str | None,
    ) -> None:
        append_event(
            self.store,
            event_type="agent.memory_commit_rejected",
            actor_type="agent" if identity is not None else "user",
            actor_id=identity.agent_id if identity is not None else None,
            target_type="memory",
            target_id=target_id,
            trace_id=request.trace_id or decision.policy_decision.trace_id,
            run_id=identity.agent_run_id if identity is not None else None,
            payload={"policy_decision": decision.to_record(), "reason": decision.reason},
        )

    def _append_revision(
        self,
        *,
        memory: Mapping[str, object],
        action: str,
        revision_type: str,
        reason: str,
        actor_type: str,
        actor_id: str | None,
    ) -> None:
        self.store.append_revision(
            {
                "memory_id": str(memory["id"]),
                "memory_key": str(memory["memory_key"]),
                "new_value": memory.get("value"),
                "source_event_ids": memory.get("source_event_ids"),
                "revision_type": revision_type,
                "action": action,
                "text_after": str(memory.get("canonical_text") or ""),
                "reason": reason,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": {"agentic_memory": True},
            },
            actor_type=actor_type,
        )

    def _link_memory_entities(
        self,
        *,
        memory: Mapping[str, object],
        identity: AgentIdentity | None,
        trace_id: str | None,
        stage: str,
    ) -> None:
        """Best-effort deterministic entity linking for ACCEPTED memories.

        Runs only on the accept paths (auto-commit, inline confirmation,
        correction), never on review candidates awaiting dashboard review
        -- those link when they are accepted. Unlike capture, no
        sensitivity gate applies here: an explicit commit is a direct
        user/agent instruction to store exactly this content, so linking
        it into the people/entity substrate honors that intent.

        ``person``-type memories additionally get a person entity for the
        memory's title-derived name plus a memory->person
        ``related_to_person`` edge carrying ``relation: "about"``, closing
        the audit's "person memory type not linked to the people/entity
        substrate" gap.

        Failure isolation mirrors ``attach_memory_embedding``: linking
        errors never fail the commit; they log ``entity.extraction_failed``
        and the commit proceeds. Stores without the entity substrate skip
        silently.
        """
        if not store_supports_entity_linking(self.store):
            return
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        memory_id = str(memory["id"])
        # Event time: when the commitment was accepted (the user just
        # asserted the fact), not some earlier source timestamp.
        observed_at = memory.get("last_confirmed_at") or memory.get("created_at") or _utc_iso()
        try:
            linker = EntityLinkingService(
                self.store,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
            )
            text = str(memory.get("canonical_text") or "")
            if text.strip():
                linker.link_entities_for_memory(memory_id=memory_id, text=text, observed_at=observed_at)
            if str(memory.get("memory_type") or "") == "person":
                person_name = derive_person_name_from_title(str(memory.get("title") or ""))
                if person_name is not None:
                    linker.link_memory_to_person(
                        memory_id=memory_id,
                        person_name=person_name,
                        observed_at=observed_at,
                    )
        except Exception as exc:
            logger.error(
                "memory entity linking failed memory_id=%s stage=%s error_code=%s",
                memory_id,
                stage,
                ENTITY_LINKING_ERROR_CODE,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            append_event(
                cast(EventStore, self.store),
                event_type="entity.extraction_failed",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="memory",
                target_id=memory_id,
                trace_id=trace_id,
                payload={
                    "stage": stage,
                    "error_code": ENTITY_LINKING_ERROR_CODE,
                    "error_message": ENTITY_LINKING_ERROR_MESSAGE,
                },
            )

    def _refresh_memory_derived_state(
        self,
        *,
        memory: Mapping[str, object],
        identity: AgentIdentity | None,
        trace_id: str | None,
        stage: str,
        replace_entity_links: bool,
    ) -> None:
        """Refresh every index derived from mutable memory content.

        Clear embeddings first so an unavailable provider degrades to no
        vector instead of silently retaining one for the previous text.
        Fact keys are deterministic on this write path and are always
        overwritten. Entity edges are temporal: content edits expire the
        old linker-owned edges before deriving the current set.
        """
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        memory_id = str(memory["id"])
        clear_embedding = getattr(self.store, "clear_memory_embedding", None)
        if callable(clear_embedding):
            clear_embedding(memory_id=memory_id)
        self._attach_or_defer_memory_embedding(
            memory,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=trace_id,
        )
        attach_memory_fact_keys(
            self.store,
            memory,
            use_env_provider=False,
            actor_type=actor_type,
            actor_id=actor_id,
            trace_id=trace_id,
        )
        if replace_entity_links:
            self._expire_memory_entity_links(
                memory_id=memory_id,
                actor_type=actor_type,
                actor_id=actor_id,
                trace_id=trace_id,
                stage=stage,
            )
        self._link_memory_entities(
            memory=memory,
            identity=identity,
            trace_id=trace_id,
            stage=stage,
        )

    def _expire_memory_entity_links(
        self,
        *,
        memory_id: str,
        actor_type: str,
        actor_id: str | None,
        trace_id: str | None,
        stage: str,
    ) -> None:
        list_edges = getattr(self.store, "list_edges", None)
        expire_edge = getattr(self.store, "expire_edge", None)
        if not callable(list_edges) or not callable(expire_edge):
            return
        try:
            for edge in list_edges(from_id=memory_id):
                if str(edge.get("from_type") or "") != "memory":
                    continue
                if str(edge.get("edge_type") or "") not in {
                    ENTITY_MENTION_EDGE_TYPE,
                    PERSON_ABOUT_EDGE_TYPE,
                }:
                    continue
                expire_edge(edge_id=str(edge["id"]), actor_type=actor_type)
        except Exception as exc:
            logger.error(
                "memory entity-link expiry failed memory_id=%s stage=%s error_code=%s",
                memory_id,
                stage,
                ENTITY_LINKING_ERROR_CODE,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            append_event(
                self.store,
                event_type="entity.extraction_failed",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="memory",
                target_id=memory_id,
                trace_id=trace_id,
                payload={
                    "stage": f"{stage}.replace_links",
                    "error_code": ENTITY_LINKING_ERROR_CODE,
                    "error_message": ENTITY_LINKING_ERROR_MESSAGE,
                },
            )
            raise ContinuityStoreInvariantError("content mutation could not expire obsolete entity links") from exc

    def _create_provenance_links(
        self, *, memory: Mapping[str, object], request: MemoryCommitRequest, actor_type: str
    ) -> None:
        seen: set[str] = set()
        for source_ref in request.source_refs:
            source_id = _source_uuid(source_ref)
            if source_id is None or source_id in seen:
                continue
            seen.add(source_id)
            self.store.create_provenance_link(
                {
                    "target_type": "memory",
                    "target_id": str(memory["id"]),
                    "source_id": source_id,
                    "quote": request.conversation_excerpt,
                    "evidence_role": "supports",
                    "confidence": request.confidence,
                },
                actor_type=actor_type,
            )

    def _memory_by_confirmation_id(self, confirmation_id: str) -> VNextRow | None:
        get_memory_by_confirmation_id = getattr(self.store, "get_memory_by_confirmation_id", None)
        if callable(get_memory_by_confirmation_id):
            return get_memory_by_confirmation_id(confirmation_id)
        for memory in self.store.list_memories(status=None):
            confirmation = _agentic_metadata(memory).get("confirmation")
            if isinstance(confirmation, Mapping) and confirmation.get("confirmation_id") == confirmation_id:
                return memory
        return None

    def _latest_agentic_commit(self, identity: AgentIdentity | None) -> VNextRow | None:
        latest_agentic_commit_memory = getattr(self.store, "latest_agentic_commit_memory", None)
        if callable(latest_agentic_commit_memory):
            return latest_agentic_commit_memory(agent_id=identity.agent_id if identity is not None else None)
        for memory in self.store.list_memories(status="active"):
            agentic = _agentic_metadata(memory)
            if agentic.get("kind") != "agentic_memory_commit":
                continue
            if identity is None or agentic.get("agent_id") == identity.agent_id:
                return memory
            nested_identity = agentic.get("agent_identity")
            if isinstance(nested_identity, Mapping) and nested_identity.get("agent_id") == identity.agent_id:
                return memory
        return None

    def _invalidate_pending_derived_candidates(
        self,
        *,
        member_id: str,
        identity: AgentIdentity | None,
        reason: str,
        exclude_memory_id: str | None = None,
    ) -> list[str]:
        """Make review work stale when one of its snapshotted inputs changes.

        Bundled stores provide a targeted, locking lookup over pending
        candidate snapshots. Keeping invalidation in the same transaction as
        the member mutation ensures operators never see a candidate that can
        later republish forgotten, corrected, redacted, or superseded text.
        Third-party stores without the optional lookup still fail closed at
        acceptance through the mandatory snapshot comparison.
        """
        list_pending = getattr(self.store, "list_pending_derived_candidates_for_member", None)
        if not callable(list_pending):
            return []
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        invalidated_at = _utc_iso()
        invalidated_ids: list[str] = []
        for candidate in list_pending(
            member_id=member_id,
            exclude_memory_id=exclude_memory_id,
        ):
            candidate_id = str(candidate["id"])
            metadata = _memory_metadata(candidate)
            consolidation_raw = metadata.get("consolidation")
            if not isinstance(consolidation_raw, Mapping):
                continue
            consolidation = dict(consolidation_raw)
            if isinstance(consolidation.get("accepted"), Mapping):
                continue
            invalidated: JsonObject = {
                "invalidated_at": invalidated_at,
                "member_id": member_id,
                "reason": reason,
            }
            consolidation["invalidated"] = invalidated
            updated = self.store.update_memory(
                memory_id=candidate_id,
                patch={
                    "status": "stale",
                    "promotion_eligibility": "not_promotable",
                    "last_reviewed_at": invalidated_at,
                    "metadata_json": {
                        **metadata,
                        "review_required": False,
                        "consolidation": consolidation,
                    },
                },
                actor_type=actor_type,
            )
            self.store.append_revision(
                {
                    "memory_id": candidate_id,
                    "memory_key": str(updated["memory_key"]),
                    "previous_value": candidate.get("value"),
                    "new_value": updated.get("value"),
                    "source_event_ids": updated.get("source_event_ids"),
                    "revision_type": "rejected",
                    "action": "memory_consolidation_candidate_invalidated",
                    "text_before": str(candidate.get("canonical_text") or ""),
                    "text_after": str(updated.get("canonical_text") or ""),
                    "reason": reason,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    "metadata_json": invalidated,
                },
                actor_type=actor_type,
            )
            append_event(
                self.store,
                event_type="agent.memory_consolidation_candidate_invalidated",
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="memory",
                target_id=candidate_id,
                payload=invalidated,
            )
            invalidated_ids.append(candidate_id)
        return invalidated_ids

    def _transition_memory(
        self,
        *,
        identity: AgentIdentity | None,
        memory: VNextRow,
        operation: str,
        lifecycle_status: str,
        next_status: str,
        event_type: str,
        revision_type: str,
        action: str,
        reason: str,
        superseded_by: VNextRow | None = None,
        set_successor_pointer: bool = True,
        exclude_derived_candidate_id: str | None = None,
        allow_pending_consolidation_successor: bool = False,
    ) -> JsonObject:
        # Central enforcement: reject undoing/forgetting/superseding a row that
        # is already retired, and reject re-superseding back to an ancestor.
        self._require_transition(operation, str(memory.get("status") or ""))
        if superseded_by is not None:
            self._guard_supersession_acyclic(
                memory=memory,
                successor=superseded_by,
                allow_pending_consolidation=allow_pending_consolidation_successor,
            )
        metadata = _memory_metadata(memory)
        agentic = _agentic_metadata(memory)
        lifecycle_history_value = agentic.get("lifecycle_history")
        history, history_withheld = _withheld_history(
            list(lifecycle_history_value) if isinstance(lifecycle_history_value, list) else []
        )
        history.append({"status": lifecycle_status, "at": _utc_iso(), "reason": reason})
        agentic["lifecycle_status"] = lifecycle_status
        agentic["lifecycle_history"] = history
        actor_type = "agent" if identity is not None else "user"
        actor_id = identity.agent_id if identity is not None else None
        self._invalidate_pending_derived_candidates(
            member_id=str(memory["id"]),
            identity=identity,
            reason=f"source memory transitioned to {next_status} after the derived candidate was proposed",
            exclude_memory_id=exclude_derived_candidate_id,
        )
        patch: JsonObject = {"status": next_status, "metadata_json": {**metadata, "agentic_memory": agentic}}
        successor_id: str | None = None
        if superseded_by is not None:
            # Supersession pointers are first-class columns; the
            # metadata_json copies stay for backward compatibility.
            successor_id = str(superseded_by["id"])
            patch["superseded_by"] = successor_id
            patch["metadata_json"] = {**metadata, "superseded_by": successor_id, "agentic_memory": agentic}
            # ---- currency chains (stored currency) begin -------------------
            # An approved supersession closes the retired row's validity
            # window at min(existing valid_to, replacement event time). It
            # must never extend a previously reviewed expiry, and an unexpire
            # sentinel must not keep a superseded row current.
            replacement_event_time = (
                supersession_event_time(
                    superseded_by,
                    source_lookup=getattr(self.store, "get_source", None),
                )
                or _utc_iso()
            )
            patch["valid_to"] = _earliest_valid_to(
                memory.get("valid_to"),
                replacement_event_time,
            )
            # ---- currency chains (stored currency) end ---------------------
        updated = self.store.update_memory(
            memory_id=str(memory["id"]),
            patch=patch,
            actor_type=actor_type,
        )
        # set_successor_pointer=False leaves the successor's single-valued
        # supersedes column to the caller (consolidation acceptance sets it
        # once by the dedup/merge rule instead of clobbering it per member).
        if superseded_by is not None and successor_id is not None and set_successor_pointer:
            successor_metadata = _memory_metadata(superseded_by)
            self.store.update_memory(
                memory_id=successor_id,
                patch={
                    "supersedes": str(memory["id"]),
                    "metadata_json": {**successor_metadata, "supersedes": str(memory["id"])},
                },
                actor_type=actor_type,
            )
        revision_metadata: JsonObject = {"lifecycle_status": lifecycle_status}
        event_payload: JsonObject = {"lifecycle_status": lifecycle_status, "reason": reason}
        if successor_id is not None:
            revision_metadata["superseded_by"] = successor_id
            event_payload["superseded_by"] = successor_id
        self.store.append_revision(
            {
                "memory_id": str(updated["id"]),
                "memory_key": str(updated["memory_key"]),
                "previous_value": memory.get("value"),
                "new_value": updated.get("value"),
                "source_event_ids": updated.get("source_event_ids"),
                "revision_type": revision_type,
                "action": action,
                "text_before": str(memory.get("canonical_text") or ""),
                "text_after": str(updated.get("canonical_text") or ""),
                "reason": reason,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "metadata_json": revision_metadata,
            },
            actor_type=actor_type,
        )
        append_event(
            self.store,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            target_type="memory",
            target_id=str(updated["id"]),
            payload=event_payload,
        )
        return {
            "status": lifecycle_status,
            "write_mode": "commit",
            "memory": updated,
            "rationale_withheld": history_withheld,
        }


def memory_commit_request_from_payload(payload: Mapping[str, object], *, user_id: object) -> MemoryCommitRequest:
    confidence = payload.get("confidence", 0.9)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise VNextMemoryCommitValidationError("confidence must be a number")
    if confidence < 0.0 or confidence > 1.0:
        raise VNextMemoryCommitValidationError("confidence must be between 0.0 and 1.0")
    return MemoryCommitRequest(
        user_id=str(user_id),
        title=_normalized_text(payload.get("title"), field_name="title"),
        canonical_text=_normalized_text(payload.get("canonical_text"), field_name="canonical_text"),
        memory_type=_enum_value(
            payload.get("memory_type", "semantic"),
            field_name="memory_type",
            allowed_values=VNEXT_MEMORY_TYPES,
            aliases=_MEMORY_TYPE_ALIASES,
        ),
        domain=_enum_value(
            payload.get("domain", "unknown"),
            field_name="domain",
            allowed_values=VNEXT_DOMAINS,
            aliases=_DOMAIN_ALIASES,
        ),
        sensitivity=_enum_value(
            payload.get("sensitivity", "unknown"),
            field_name="sensitivity",
            allowed_values=VNEXT_SENSITIVITY_LEVELS,
            aliases=_SENSITIVITY_ALIASES,
        ),
        confidence=float(confidence),
        intent=_normalized_text(payload.get("intent", "explicit_remember"), field_name="intent"),
        source_type=_normalized_text(payload.get("source_type", "direct_user_instruction"), field_name="source_type"),
        source_refs=_object_tuple(payload.get("source_refs")),
        conversation_excerpt=_optional_text(payload.get("conversation_excerpt")),
        rationale=_optional_text(payload.get("rationale")),
        idempotency_key=_optional_text(payload.get("idempotency_key")),
        project_scope=_string_tuple(payload.get("project_scope")),
        contradiction_refs=_string_tuple(payload.get("contradiction_refs")),
        trace_id=_optional_text(payload.get("trace_id")),
    )


__all__ = [
    "CONSOLIDATION_ACCEPTABLE_STATUSES",
    "EXPIRE_BLOCKED_STATUSES",
    "MEMORY_COMMIT_STATUSES",
    "MEMORY_COMMIT_WRITE_MODES",
    "MEMORY_STATUSES",
    "VALID_TO_UNBOUNDED_SENTINEL",
    "MemoryCommitPolicyDecision",
    "MemoryCommitRequest",
    "VNextMemoryCommitService",
    "VNextMemoryCommitValidationError",
    "VNEXT_DOMAINS",
    "VNEXT_MEMORY_TYPES",
    "VNEXT_SENSITIVITY_LEVELS",
    "evaluate_memory_commit_policy",
    "is_pending_consolidation_candidate",
    "AUTO_PROMOTED_EVENT",
    "load_promotion_settings",
    "memory_commit_request_from_payload",
    "promotion_candidate_for_request",
]
