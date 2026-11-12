from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_control import PERMISSION_PROFILES, evaluate_agent_policy
from alicebot_api.vnext_agent_keys import (
    AGENT_KEY_PREFIX,
    AGENT_KEY_PREFIX_LENGTH,
    AgentKeyAuthenticationError,
    AgentKeyValidationError,
    agent_key_from_authorization,
    create_agent_key,
    hash_agent_key,
    mint_agent_key,
    resolve_agent_identity,
    resolve_protected_agent_identity,
    verify_agent_key,
)


class FakeAgentKeyStore:
    def __init__(self) -> None:
        self.agent_api_keys: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def create_agent_api_key(self, key: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {
            **key,
            "id": str(uuid4()),
            "created_at": "now",
            "revoked_at": None,
            "last_used_at": None,
        }
        self.agent_api_keys.append(row)
        return row

    def get_agent_api_key_by_hash(self, key_hash: str) -> dict[str, object] | None:
        for row in self.agent_api_keys:
            if row.get("key_hash") == key_hash:
                return row
        return None

    def list_agent_api_keys(self, *, limit: int = 50) -> list[dict[str, object]]:
        return self.agent_api_keys[:limit]

    def revoke_agent_api_key(self, *, key_id: str, **_kwargs) -> dict[str, object] | None:
        for row in self.agent_api_keys:
            if row["id"] == key_id and row.get("revoked_at") is None:
                row["revoked_at"] = "now"
                return row
        return None

    def touch_agent_api_key(self, *, key_id: str) -> dict[str, object]:
        for row in self.agent_api_keys:
            if row["id"] == key_id:
                row["last_used_at"] = "now"
                return row
        raise AssertionError(key_id)

    def count_active_agent_api_keys(self) -> int:
        return len([row for row in self.agent_api_keys if row.get("revoked_at") is None])


def test_mint_agent_key_uses_prefix_and_is_unique() -> None:
    first = mint_agent_key()
    second = mint_agent_key()

    assert first.startswith(AGENT_KEY_PREFIX)
    assert second.startswith(AGENT_KEY_PREFIX)
    assert first != second
    assert len(first) > AGENT_KEY_PREFIX_LENGTH


def test_create_and_verify_agent_key_round_trip() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()

    record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
        label="Hermes local",
    )

    assert raw_key.startswith(AGENT_KEY_PREFIX)
    assert record["agent_id"] == "hermes"
    assert record["permission_profile"] == "trusted_local_agent"
    assert record["label"] == "Hermes local"
    assert record["key_hash"] == hash_agent_key(raw_key)
    assert record["key_prefix"] == raw_key[:AGENT_KEY_PREFIX_LENGTH]
    assert raw_key not in str(record["key_hash"])

    verified = verify_agent_key(store, raw_key)
    assert verified is not None
    assert verified["id"] == record["id"]
    assert verified["last_used_at"] == "now"


def test_create_agent_key_rejects_unknown_profile_and_blank_agent_id() -> None:
    store = FakeAgentKeyStore()

    with pytest.raises(AgentKeyValidationError):
        create_agent_key(store, user_id=uuid4(), agent_id="hermes", permission_profile="root_agent")
    with pytest.raises(AgentKeyValidationError):
        create_agent_key(store, user_id=uuid4(), agent_id="   ", permission_profile="read_only_agent")
    assert store.agent_api_keys == []


def test_verify_agent_key_rejects_revoked_unknown_and_malformed_keys() -> None:
    store = FakeAgentKeyStore()
    record, raw_key = create_agent_key(
        store, user_id=uuid4(), agent_id="hermes", permission_profile="trusted_local_agent"
    )

    assert verify_agent_key(store, "alice_sk_not-a-real-key") is None
    assert verify_agent_key(store, "not-even-prefixed") is None

    store.revoke_agent_api_key(key_id=str(record["id"]))
    assert verify_agent_key(store, raw_key) is None


def test_resolve_agent_identity_uses_key_record_over_payload_claims() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="trusted_local_agent"
    )

    identity = resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={"agent_id": "openclaw", "agent_run_id": "run-1"},
    )

    assert identity is not None
    assert identity.agent_id == "openclaw"
    assert identity.permission_profile == "trusted_local_agent"
    assert identity.agent_run_id == "run-1"
    assert identity.auth == "agent_api_key"
    assert identity.to_record()["auth"] == "agent_api_key"


def test_resolve_agent_identity_allows_profile_downgrade() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="trusted_local_agent"
    )

    identity = resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={"agent_id": "openclaw", "permission_profile": "read_only_agent"},
    )

    assert identity is not None
    assert identity.permission_profile == "read_only_agent"


@pytest.mark.parametrize("identity_namespace", [None, "agent", "agent_identity"])
def test_resolve_agent_identity_rejects_profile_escalation_and_audits(
    identity_namespace: str | None,
) -> None:
    """Mutation: replace the returned rejection notice with ``None``."""
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )
    claimed_identity = {
        "agent_id": "openclaw",
        "permission_profile": "admin_agent",
    }
    payload = (
        claimed_identity
        if identity_namespace is None
        else {identity_namespace: claimed_identity}
    )

    with pytest.raises(AgentKeyAuthenticationError) as exc_info:
        resolve_agent_identity(
            store,
            user_id=user_id,
            raw_key=raw_key,
            payload=payload,
        )

    assert exc_info.value.status_code == 403
    assert store.events == []
    payload = exc_info.value.key_rejection.payload
    assert payload["reason"] == "permission_profile_escalation"
    assert payload["granted_permission_profile"] == "project_scoped_agent"
    assert payload["claimed_permission_profile"] == "admin_agent"
    serialized = str(payload)
    assert raw_key not in serialized
    assert hash_agent_key(raw_key) not in serialized


def test_resolve_agent_identity_rejects_agent_id_mismatch() -> None:
    """Mutation: replace the returned rejection notice with ``None``."""
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )

    with pytest.raises(AgentKeyAuthenticationError) as exc_info:
        resolve_agent_identity(
            store,
            user_id=user_id,
            raw_key=raw_key,
            payload={"agent_id": "hermes"},
        )

    assert exc_info.value.status_code == 403
    assert store.events == []
    assert exc_info.value.key_rejection.payload["reason"] == "agent_id_mismatch"


def test_resolve_agent_identity_rejects_invalid_revoked_and_foreign_keys() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="openclaw", permission_profile="project_scoped_agent"
    )

    with pytest.raises(AgentKeyAuthenticationError) as invalid_info:
        resolve_agent_identity(
            store, user_id=user_id, raw_key="alice_sk_wrong", payload={"agent_id": "openclaw"}
        )
    assert invalid_info.value.status_code == 401

    with pytest.raises(AgentKeyAuthenticationError) as foreign_info:
        resolve_agent_identity(
            store, user_id=uuid4(), raw_key=raw_key, payload={"agent_id": "openclaw"}
        )
    assert foreign_info.value.status_code == 401

    store.revoke_agent_api_key(key_id=str(record["id"]))
    with pytest.raises(AgentKeyAuthenticationError) as revoked_info:
        resolve_agent_identity(
            store, user_id=user_id, raw_key=raw_key, payload={"agent_id": "openclaw"}
        )
    assert revoked_info.value.status_code == 401


def test_resolve_agent_identity_key_only_call_builds_identity_from_key() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="hermes", permission_profile="trusted_local_agent"
    )

    identity = resolve_agent_identity(store, user_id=user_id, raw_key=raw_key, payload={})

    assert identity is not None
    assert identity.agent_id == "hermes"
    assert identity.agent_type == "personal_assistant"
    assert identity.permission_profile == "trusted_local_agent"
    assert identity.auth == "agent_api_key"


def test_resolve_agent_identity_no_keys_falls_back_as_unauthenticated_local() -> None:
    store = FakeAgentKeyStore()

    identity = resolve_agent_identity(
        store,
        user_id=uuid4(),
        raw_key=None,
        payload={"agent_id": "hermes", "permission_profile": "trusted_local_agent"},
    )

    assert identity is not None
    assert identity.agent_id == "hermes"
    assert identity.permission_profile == "trusted_local_agent"
    assert identity.auth == "unauthenticated_local"
    assert identity.to_record()["auth"] == "unauthenticated_local"


def test_resolve_agent_identity_keyless_agent_calls_rejected_once_keys_exist() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    create_agent_key(
        store, user_id=user_id, agent_id="hermes", permission_profile="trusted_local_agent"
    )

    with pytest.raises(AgentKeyAuthenticationError) as exc_info:
        resolve_agent_identity(
            store,
            user_id=user_id,
            raw_key=None,
            payload={"agent_id": "hermes"},
        )

    assert exc_info.value.status_code == 401
    assert "Authorization: Bearer alice_sk_" in str(exc_info.value)


def test_resolve_agent_identity_non_agent_calls_stay_untouched() -> None:
    store = FakeAgentKeyStore()
    create_agent_key(
        store, user_id=uuid4(), agent_id="hermes", permission_profile="trusted_local_agent"
    )

    assert resolve_agent_identity(store, user_id=uuid4(), raw_key=None, payload={}) is None


def test_create_agent_key_normalizes_and_stores_project_scope_binding() -> None:
    store = FakeAgentKeyStore()

    bound, _raw = create_agent_key(
        store,
        user_id=uuid4(),
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope="  Alice   Bot  ",
    )
    unbound, _raw = create_agent_key(
        store,
        user_id=uuid4(),
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )

    assert bound["project_scope"] == "Alice Bot"
    assert unbound["project_scope"] is None


def test_resolve_agent_identity_binds_project_scope_from_the_key_record() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope="alicebot",
    )

    # Empty payload claim inherits the key binding.
    identity = resolve_agent_identity(
        store, user_id=user_id, raw_key=raw_key, payload={"agent_id": "openclaw"}
    )
    assert identity is not None
    assert identity.project_scope == ("alicebot",)
    assert identity.project_scope_locked is True
    assert identity.to_record()["project_scope_locked"] is True

    # A subset claim (here: the same single project) narrows, never widens.
    narrowed = resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={"agent_id": "openclaw", "project_scope": ["  AliceBot  "]},
    )
    assert narrowed is not None
    assert narrowed.project_scope == ("AliceBot",)
    assert narrowed.project_scope_locked is True


@pytest.mark.parametrize(
    ("bound_scope", "claimed_scope"),
    [
        ("İ", "i"),
        ("Straße", "STRASSE"),
        ("Σ", "σ"),
        ("Σ", "ς"),
        ("\u00a0Alice\u00a0", "\u00a0alice\u00a0"),
    ],
)
def test_key_binding_rejects_non_ascii_case_variants(
    bound_scope: str,
    claimed_scope: str,
) -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope=bound_scope,
    )

    with pytest.raises(AgentKeyAuthenticationError, match="outside that scope"):
        resolve_agent_identity(
            store,
            user_id=user_id,
            raw_key=raw_key,
            payload={"agent_id": "openclaw", "project_scope": [claimed_scope]},
        )


@pytest.mark.parametrize("bound_scope", ["İ", "Straße", "Σ", "σ", "ς", "\u00a0Alice\u00a0"])
def test_key_binding_allows_exact_non_ascii_self_match(bound_scope: str) -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope=bound_scope,
    )

    identity = resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={"agent_id": "openclaw", "project_scope": [bound_scope]},
    )

    assert identity is not None
    assert identity.project_scope == (bound_scope,)
    assert identity.project_scope_locked is True


def test_key_binding_rejects_blank_scope_instead_of_silently_unbinding() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()

    with pytest.raises(AgentKeyValidationError, match="must not be blank"):
        create_agent_key(
            store,
            user_id=user_id,
            agent_id="openclaw",
            permission_profile="project_scoped_agent",
            project_scope=" \t\n",
        )


def test_resolve_agent_identity_rejects_project_scope_widening_and_audits() -> None:
    """Mutation: replace the returned rejection notice with ``None``."""
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope="alicebot",
    )

    with pytest.raises(AgentKeyAuthenticationError) as exc_info:
        resolve_agent_identity(
            store,
            user_id=user_id,
            raw_key=raw_key,
            payload={"agent_id": "openclaw", "project_scope": ["alicebot", "other-project"]},
        )

    assert exc_info.value.status_code == 403
    assert store.events == []
    payload = exc_info.value.key_rejection.payload
    assert payload["reason"] == "project_scope_escalation"
    assert payload["granted_project_scope"] == "alicebot"
    assert payload["claimed_project_scope"] == ["alicebot", "other-project"]
    serialized = str(payload)
    assert raw_key not in serialized
    assert hash_agent_key(raw_key) not in serialized


def test_resolve_agent_identity_unbound_keys_keep_payload_project_scope() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
    )

    identity = resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={"agent_id": "openclaw", "project_scope": ["anything-goes"]},
    )

    assert identity is not None
    assert identity.project_scope == ("anything-goes",)
    assert identity.project_scope_locked is False


def test_policy_enforces_the_bound_project_scope_for_reads_and_writes() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
        project_scope="alicebot",
    )
    identity = resolve_agent_identity(
        store, user_id=user_id, raw_key=raw_key, payload={"agent_id": "openclaw"}
    )
    assert identity is not None

    blocked = evaluate_agent_policy(
        identity=identity,
        action="memory.commit",
        domains=("project",),
        project_scope=("other-project",),
    )
    assert blocked.decision == "blocked"
    assert "project_scope_binding_violation" in blocked.reasons

    allowed = evaluate_agent_policy(
        identity=identity,
        action="memory.commit",
        domains=("project",),
        project_scope=("alicebot",),
    )
    assert allowed.decision != "blocked"
    assert "project_scope_binding_violation" not in allowed.reasons

    inherited_read = evaluate_agent_policy(
        identity=identity,
        action="context_pack.request",
        domains=("project",),
    )
    assert inherited_read.decision != "blocked"
    assert inherited_read.effective_project_scope == ("alicebot",)

    out_of_scope_read = evaluate_agent_policy(
        identity=identity,
        action="context_pack.request",
        domains=("project",),
        project_scope=("other-project",),
    )
    assert out_of_scope_read.decision == "blocked"
    assert "project_scope_binding_violation" in out_of_scope_read.reasons
    assert out_of_scope_read.effective_project_scope == ()


def test_policy_binding_rule_only_applies_when_a_binding_exists() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="openclaw",
        permission_profile="project_scoped_agent",
    )
    identity = resolve_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={"agent_id": "openclaw", "project_scope": ["self-asserted"]},
    )
    assert identity is not None
    assert identity.project_scope_locked is False

    decision = evaluate_agent_policy(
        identity=identity,
        action="memory.commit",
        domains=("project",),
        project_scope=("completely-different",),
    )
    assert "project_scope_binding_violation" not in decision.reasons


def test_agent_key_from_authorization_parses_bearer_and_ignores_other_tokens() -> None:
    assert agent_key_from_authorization("Bearer alice_sk_abc123") == "alice_sk_abc123"
    assert agent_key_from_authorization("bearer  alice_sk_abc123 ") == "alice_sk_abc123"
    assert agent_key_from_authorization("alice_sk_abc123") is None
    assert agent_key_from_authorization("Bearer hosted-session-token") is None
    assert agent_key_from_authorization("Basic dXNlcjpwYXNz") is None
    assert agent_key_from_authorization("") is None
    assert agent_key_from_authorization(None) is None
    assert agent_key_from_authorization(object()) is None


def test_protected_request_requires_bearer_once_any_active_key_exists() -> None:
    store = FakeAgentKeyStore()
    user_id = uuid4()
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="reader",
        permission_profile="read_only_agent",
        project_scope="project-a",
    )

    with pytest.raises(AgentKeyAuthenticationError, match="protected vNext requests"):
        resolve_protected_agent_identity(store, user_id=user_id, raw_key=None, payload={})

    identity = resolve_protected_agent_identity(
        store,
        user_id=user_id,
        raw_key=raw_key,
        payload={},
    )
    assert identity is not None
    assert identity.agent_id == "reader"
    assert identity.project_scope == ("project-a",)


def test_profile_privilege_order_covers_all_profiles() -> None:
    from alicebot_api.vnext_agent_keys import PROFILE_PRIVILEGE_ORDER

    assert sorted(PROFILE_PRIVILEGE_ORDER) == sorted(PERMISSION_PROFILES)
