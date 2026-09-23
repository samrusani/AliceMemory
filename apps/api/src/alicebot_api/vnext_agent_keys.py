from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import secrets
from typing import Mapping, Protocol

from alicebot_api.vnext_agent_control import (
    PERMISSION_PROFILES,
    AgentIdentity,
)
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_project_scope import (
    normalize_project_identifier,
    normalize_project_scope,
    project_scope_identity,
)
from alicebot_api.vnext_repositories import JsonObject

# The key shape lives in a leaf module so the credential floor can import it
# without a cycle through the promotion policy. Re-exported here.
from alicebot_api.agent_key_format import AGENT_KEY_PREFIX as AGENT_KEY_PREFIX
from alicebot_api.agent_key_format import AGENT_KEY_PREFIX_LENGTH as AGENT_KEY_PREFIX_LENGTH

AGENT_KEY_AUTH = "agent_api_key"
UNAUTHENTICATED_LOCAL_AUTH = "unauthenticated_local"

# Privilege ordering used to reject payloads that claim a higher profile than
# the key grants. Downgrades (claiming a lower profile) are always allowed.
PROFILE_PRIVILEGE_ORDER = (
    "read_only_agent",
    "memory_proposal_agent",
    "project_scoped_agent",
    "trusted_local_agent",
    "admin_agent",
)
_PROFILE_RANKS = {profile: rank for rank, profile in enumerate(PROFILE_PRIVILEGE_ORDER)}


class AgentKeyValidationError(ValueError):
    """Raised when an agent API key request is malformed."""


class AgentKeyAuthenticationError(PermissionError):
    """Raised when agent API key authentication or authorization fails."""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentKeyStore(Protocol):
    def append_event(self, event: JsonObject) -> JsonObject: ...

    def create_agent_api_key(self, key: JsonObject) -> JsonObject: ...

    def get_agent_api_key_by_hash(self, key_hash: str) -> JsonObject | None: ...

    def list_agent_api_keys(self, *, limit: int = 50) -> list[JsonObject]: ...

    def revoke_agent_api_key(self, *, key_id: str) -> JsonObject | None: ...

    def touch_agent_api_key(self, *, key_id: str) -> JsonObject: ...

    def count_active_agent_api_keys(self) -> int: ...


def mint_agent_key() -> str:
    """Mint a new raw agent API key. Only its sha256 hash is ever stored."""

    return f"{AGENT_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_agent_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def agent_key_prefix(raw_key: str) -> str:
    return raw_key[:AGENT_KEY_PREFIX_LENGTH]


def agent_key_from_authorization(authorization: object) -> str | None:
    """Extract a raw agent API key from an Authorization header value.

    Only ``Bearer alice_sk_...`` is treated as an agent API key. Other
    authorization values are ignored so unrelated session tokens and bare
    secrets never reach the key verifier.
    """

    if not isinstance(authorization, str):
        return None
    value = authorization.strip()
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.casefold() == "bearer" and token.startswith(AGENT_KEY_PREFIX):
        return token
    return None


def create_agent_key(
    store: AgentKeyStore,
    *,
    user_id: object,
    agent_id: str,
    permission_profile: str,
    label: str | None = None,
    project_scope: str | None = None,
) -> tuple[JsonObject, str]:
    """Create an agent API key. Returns (record, raw_key).

    The raw key is returned exactly once and never persisted; the store keeps
    only its sha256 hash plus a short prefix for identification.

    ``project_scope`` optionally binds the key to one project: identities
    resolved from a bound key carry that project scope from the key record
    (the payload may narrow to a subset, never widen) and write actions
    outside the bound scope are blocked by policy.
    """

    normalized_agent_id = " ".join(str(agent_id).split()).strip()
    if not normalized_agent_id:
        raise AgentKeyValidationError("agent_id is required")
    if permission_profile not in PERMISSION_PROFILES:
        raise AgentKeyValidationError(
            f"permission_profile must be one of {', '.join(PERMISSION_PROFILES)}"
        )
    normalized_label = " ".join(str(label).split()).strip() if label is not None else None
    normalized_project_values = normalize_project_scope(project_scope) if project_scope is not None else ()
    if project_scope is not None and not normalized_project_values:
        raise AgentKeyValidationError("project_scope must not be blank")
    normalized_project_scope = next(iter(normalized_project_values), None)
    raw_key = mint_agent_key()
    record = store.create_agent_api_key(
        {
            "user_id": str(user_id),
            "agent_id": normalized_agent_id,
            "permission_profile": permission_profile,
            "project_scope": normalized_project_scope or None,
            "key_hash": hash_agent_key(raw_key),
            "key_prefix": agent_key_prefix(raw_key),
            "label": normalized_label or None,
        }
    )
    return record, raw_key


def verify_agent_key(store: AgentKeyStore, raw_key: str) -> JsonObject | None:
    """Verify a raw agent API key. Returns the key record or None."""

    if not isinstance(raw_key, str) or not raw_key.startswith(AGENT_KEY_PREFIX):
        return None
    candidate_hash = hash_agent_key(raw_key)
    record = store.get_agent_api_key_by_hash(candidate_hash)
    if record is None:
        return None
    if not hmac.compare_digest(str(record.get("key_hash") or ""), candidate_hash):
        return None
    if record.get("revoked_at") is not None:
        return None
    return store.touch_agent_api_key(key_id=str(record["id"]))


def resolve_agent_identity(
    store: AgentKeyStore,
    *,
    user_id: object,
    raw_key: str | None,
    payload: Mapping[str, object],
) -> AgentIdentity | None:
    """Resolve an authenticated agent identity for a request payload.

    With a valid key, agent_id and permission_profile come from the key
    record; the payload may claim a lower profile (downgrade) but claiming a
    different agent_id or a higher profile is rejected. When the key record
    carries a ``project_scope`` binding, the effective identity's project
    scope is the key's: the payload may narrow it to a subset but never
    widen it (violations are rejected with 403 and audited), and the
    identity is marked ``project_scope_locked`` so policy evaluation blocks
    out-of-scope writes. Without a key, the payload identity is honored only
    while the user has no active keys at all and is marked
    ``auth: "unauthenticated_local"``.
    """

    claimed = AgentIdentity.from_payload(payload)

    if raw_key is None:
        if claimed is None:
            return None
        if int(store.count_active_agent_api_keys()) > 0:
            raise AgentKeyAuthenticationError(
                "agent API keys are configured for this user; agent calls must "
                "authenticate with 'Authorization: Bearer alice_sk_...' "
                "(create a key with 'alicebot agent keys create')",
                status_code=401,
            )
        return replace(claimed, auth=UNAUTHENTICATED_LOCAL_AUTH)

    record = verify_agent_key(store, raw_key)
    if record is None:
        raise AgentKeyAuthenticationError(
            "agent API key is invalid or has been revoked", status_code=401
        )
    if str(record.get("user_id")) != str(user_id):
        raise AgentKeyAuthenticationError(
            "agent API key does not belong to this user", status_code=401
        )

    key_agent_id = str(record["agent_id"])
    key_profile = str(record["permission_profile"])
    source = _identity_source(payload)
    claimed_agent_id = _claimed_text(source.get("agent_id"))
    claimed_profile = _claimed_text(source.get("permission_profile"))

    if claimed_agent_id is not None and claimed_agent_id != key_agent_id:
        _append_key_rejection_event(
            store,
            record=record,
            reason="agent_id_mismatch",
            claimed={"claimed_agent_id": claimed_agent_id},
        )
        raise AgentKeyAuthenticationError(
            f"agent API key was issued to agent '{key_agent_id}' but the payload "
            f"claims agent '{claimed_agent_id}'",
            status_code=403,
        )

    effective_profile = key_profile
    if claimed_profile is not None:
        claimed_rank = _PROFILE_RANKS.get(claimed_profile)
        if claimed_rank is None or claimed_rank > _PROFILE_RANKS[key_profile]:
            _append_key_rejection_event(
                store,
                record=record,
                reason="permission_profile_escalation",
                claimed={"claimed_permission_profile": claimed_profile},
            )
            raise AgentKeyAuthenticationError(
                f"agent API key grants permission_profile '{key_profile}'; the "
                f"payload claims '{claimed_profile}', which is higher. Remove the "
                "claim or request a new key with the required profile.",
                status_code=403,
            )
        effective_profile = claimed_profile

    if claimed is None:
        claimed = AgentIdentity.from_payload(
            {"agent_id": key_agent_id, "permission_profile": key_profile}
        ) or AgentIdentity(agent_id=key_agent_id, permission_profile=key_profile)

    raw_key_project_scope = record.get("project_scope")
    key_project_scope = (
        normalize_project_identifier(raw_key_project_scope)
        if isinstance(raw_key_project_scope, str)
        else None
    )
    if raw_key_project_scope is not None and not key_project_scope:
        _append_key_rejection_event(
            store,
            record=record,
            reason="invalid_project_scope_binding",
            claimed={},
        )
        raise AgentKeyAuthenticationError(
            "agent API key has an invalid blank project scope binding",
            status_code=403,
        )
    # Read the scope claim from the payload source directly (like the
    # profile claim): key-only calls without an agent_id still must not be
    # able to widen the binding.
    claimed_scope = _claimed_project_scope(source) or claimed.project_scope
    effective_project_scope = claimed.project_scope or claimed_scope
    project_scope_locked = False
    if key_project_scope is not None:
        bound_scope = (key_project_scope,)
        bound_identity = set(project_scope_identity(bound_scope))
        widened = tuple(
            value
            for value in claimed_scope
            if not set(project_scope_identity((value,))).issubset(bound_identity)
        )
        if widened:
            _append_key_rejection_event(
                store,
                record=record,
                reason="project_scope_escalation",
                claimed={
                    "claimed_project_scope": list(claimed_scope),
                    "granted_project_scope": key_project_scope,
                },
            )
            raise AgentKeyAuthenticationError(
                f"agent API key is bound to project scope '{key_project_scope}'; the "
                f"payload claims project(s) {', '.join(repr(value) for value in widened)} "
                "outside that scope. Narrow the claim or request a key bound to those "
                "projects.",
                status_code=403,
            )
        # A subset claim narrows; an empty claim inherits the key binding.
        effective_project_scope = claimed_scope or bound_scope
        project_scope_locked = True

    return replace(
        claimed,
        agent_id=key_agent_id,
        permission_profile=effective_profile,
        project_scope=effective_project_scope,
        project_scope_locked=project_scope_locked,
        auth=AGENT_KEY_AUTH,
    )


def resolve_protected_agent_identity(
    store: AgentKeyStore,
    *,
    user_id: object,
    raw_key: str | None,
    payload: Mapping[str, object],
) -> AgentIdentity | None:
    """Authenticate a protected vNext request before endpoint dispatch.

    Fresh local installs retain keyless human/local compatibility while no
    active keys exist. Once a user provisions a key, every protected request
    must present a valid Bearer key, even when its payload omits agent claims.
    """

    if raw_key is None and int(store.count_active_agent_api_keys()) > 0:
        raise AgentKeyAuthenticationError(
            "agent API keys are configured for this user; protected vNext requests must "
            "authenticate with 'Authorization: Bearer alice_sk_...'",
            status_code=401,
        )
    return resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload=payload,
    )


def _identity_source(payload: Mapping[str, object]) -> Mapping[str, object]:
    nested = payload.get("agent_identity")
    if isinstance(nested, Mapping):
        return nested
    nested = payload.get("agent")
    if isinstance(nested, Mapping):
        return nested
    return payload


def _claimed_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _claimed_project_scope(source: Mapping[str, object]) -> tuple[str, ...]:
    value = source.get("project_scope")
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise AgentKeyAuthenticationError(
            "project_scope claim must be a list of non-blank strings",
            status_code=403,
        )
    values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AgentKeyAuthenticationError(
                "project_scope claim must be a list of non-blank strings",
                status_code=403,
            )
        normalized = normalize_project_identifier(item)
        if not normalized:
            raise AgentKeyAuthenticationError(
                "project_scope claim must be a list of non-blank strings",
                status_code=403,
            )
        values.append(normalized)
    return normalize_project_scope(values)


def _append_key_rejection_event(
    store: AgentKeyStore,
    *,
    record: Mapping[str, object],
    reason: str,
    claimed: JsonObject,
) -> None:
    append_event(
        store,
        event_type="agent.key_escalation_rejected",
        actor_type="agent",
        actor_id=str(record["agent_id"]),
        target_type="agent_api_key",
        target_id=str(record["id"]),
        payload={
            "reason": reason,
            "key_prefix": str(record.get("key_prefix") or ""),
            "granted_permission_profile": str(record["permission_profile"]),
            **claimed,
        },
    )


__all__ = [
    "AGENT_KEY_AUTH",
    "AGENT_KEY_PREFIX",
    "AGENT_KEY_PREFIX_LENGTH",
    "PROFILE_PRIVILEGE_ORDER",
    "UNAUTHENTICATED_LOCAL_AUTH",
    "AgentKeyAuthenticationError",
    "AgentKeyStore",
    "AgentKeyValidationError",
    "agent_key_from_authorization",
    "agent_key_prefix",
    "create_agent_key",
    "hash_agent_key",
    "mint_agent_key",
    "resolve_agent_identity",
    "resolve_protected_agent_identity",
    "verify_agent_key",
]
