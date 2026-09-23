from __future__ import annotations

from copy import deepcopy
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError
from alicebot_api.vnext_entities import ENTITY_MENTION_EDGE_TYPE, PERSON_ABOUT_EDGE_TYPE
from alicebot_api.vnext_memory_commit import (
    MEMORY_STATUSES,
    VALID_TO_UNBOUNDED_SENTINEL,
    MemoryCommitRequest,
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
    evaluate_memory_commit_policy,
)
from alicebot_api.vnext_memory_version import memory_version_snapshot
from alicebot_api.vnext_project_update_guard import (
    PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE,
    is_pending_project_update_memory,
)


@pytest.fixture(autouse=True)
def _clear_embedding_env(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)


def _identity(permission_profile: str, *, project_scope: tuple[str, ...] = ()) -> AgentIdentity:
    return AgentIdentity(
        agent_id="hermes" if permission_profile != "project_scoped_agent" else "openclaw",
        agent_type="personal_assistant",
        permission_profile=permission_profile,
        project_scope=project_scope,
    )


def _request(**overrides: object) -> MemoryCommitRequest:
    payload = {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "title": "Coffee preference",
        "canonical_text": "Sam prefers coffee before noon.",
        "domain": "personal",
        "sensitivity": "private",
        "confidence": 0.95,
    }
    payload.update(overrides)
    return MemoryCommitRequest(**payload)  # type: ignore[arg-type]


def test_trusted_explicit_direct_memory_auto_commits() -> None:
    decision = evaluate_memory_commit_policy(
        identity=_identity("trusted_local_agent"),
        request=_request(domain="professional", sensitivity="internal"),
    )

    assert decision.write_mode == "commit"
    assert decision.status == "committed"
    assert decision.requires_confirmation is False
    assert decision.requires_dashboard_review is False


def test_sensitive_memory_requires_inline_confirmation() -> None:
    # private is within a trusted_local_agent ceiling. confidential is above
    # it and is refused instead of held; that refusal is pinned separately.
    decision = evaluate_memory_commit_policy(
        identity=_identity("trusted_local_agent"),
        request=_request(domain="health", sensitivity="private"),
    )

    assert decision.write_mode == "confirm_inline"
    assert decision.status == "confirmation_required"
    assert "sensitive_memory_requires_confirmation" in decision.reasons


def test_external_source_requires_dashboard_review() -> None:
    decision = evaluate_memory_commit_policy(
        identity=_identity("trusted_local_agent"),
        request=_request(source_type="browser_clip"),
    )

    assert decision.write_mode == "propose_review"
    assert decision.status == "review_required"
    assert "external_source_requires_review" in decision.reasons


def test_direct_user_commit_without_identity_auto_commits() -> None:
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(domain="professional", sensitivity="internal"),
    )

    assert decision.write_mode == "commit"
    assert decision.status == "committed"
    assert decision.policy_decision.permission_profile == "user_or_system"


def test_direct_user_commit_keeps_non_identity_safeguards() -> None:
    secret = evaluate_memory_commit_policy(
        identity=None,
        request=_request(
            # Was "The api_key for staging is tucked in here." A bare mention of
            # api_key no longer rejects, so this now carries an actual assignment
            # to keep testing what the test is named for. The changed behaviour
            # is pinned separately in the mention tests below.
            canonical_text="The staging api_key=hunter2 is tucked in here.",
            domain="professional",
            sensitivity="internal",
        ),
    )
    assert secret.write_mode == "reject"
    assert "unsafe_secret_storage" in secret.reasons

    external = evaluate_memory_commit_policy(
        identity=None,
        request=_request(source_type="browser_clip", domain="professional", sensitivity="internal"),
    )
    assert external.write_mode == "propose_review"
    assert "external_source_requires_review" in external.reasons


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("title", "sk-live_abcd1234"),
        ("canonical_text", "The staging api_key=hunter2 is tucked in here."),
        ("conversation_excerpt", "here it is: ghp_aaaabbbbcccc"),
        ("rationale", "stored password=hunter2 for later"),
    ],
)
def test_secret_markers_are_rejected_in_every_text_field(field: str, payload: str) -> None:
    """A credential is a leak wherever it is pasted, not only in the body.

    Every one of these fields is stored and later replayed inside context packs,
    so guarding canonical_text alone moved the leak instead of closing it.
    """
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(**{field: payload}),
    )

    assert decision.write_mode == "reject", f"{field} accepted a credential"
    assert "unsafe_secret_storage" in decision.reasons


@pytest.mark.parametrize(
    "source_refs",
    [
        # Realistic material since 2026-09-23: the design round's detector
        # needs real key material after a prefix, so "xoxb-9999-secret-token"
        # (a word chain) is no longer a key. See the benign twin below.
        ("xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",),
        ({"source_id": "AKIAIOSFODNN7EXAMPLE"},),
        ({"note": {"deep": "ghp_aaaabbbbcccc"}},),
        ({"api_key": "Xq9mZt2L" + "xP9wKc4BVq7m"},),
        (["nested", ["deeper", "sk-live_abcd1234"]],),
    ],
)
def test_secret_markers_are_rejected_anywhere_inside_source_refs(source_refs: tuple[object, ...]) -> None:
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(source_refs=source_refs),
    )

    assert decision.write_mode == "reject"
    assert "unsafe_secret_storage" in decision.reasons


@pytest.mark.parametrize(
    "source_refs",
    [
        ("xoxb-9999-secret-token",),
        ({"api_key": "value-is-clean"},),
    ],
)
def test_word_chains_after_a_prefix_or_a_key_name_are_refused_at_commit_as_v0160_did(
    source_refs: tuple[object, ...],
) -> None:
    """BEHAVIOUR CHANGE, 2026-09-23, twice.

    S4.4 round 2 (design ruling): the credential floor reads neither as key
    material, since a Slack token is a long opaque run after "xoxb-" and a
    value made of short dictionary words is a label. That still holds for the
    floor, which every other door calls alone.

    S4.4 round 5 (ruling H1): the commit gate is one of the two doors
    v0.16.0 checked, and it now refuses whatever v0.16.0's check refused
    there unless one of four named carve-outs applies. v0.16.0 refused both
    (the xoxb- prefix pattern and the api_key assignment rule), and neither
    is a carve-out target, so both are refused here again.
    """

    from alicebot_api.credential_floor import carries_credential_material

    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(domain="professional", sensitivity="internal", source_refs=source_refs),
    )

    assert "unsafe_secret_storage" in decision.reasons
    # Guards the guard: the floor alone still reads the ref as clean, so the
    # refusal comes from v0.16.0's check.
    assert not carries_credential_material(list(source_refs))


@pytest.mark.parametrize(
    "text",
    [
        "We reviewed the api_key rotation policy doc.",
        "I bought a private keyboard for the office.",
        "The private key ceremony is scheduled for Tuesday.",
        "Her password= convention in the wiki is outdated.",
        "Access token lifetimes came up in the review.",
        "Rotate the api key every 90 days.",
        "monkey=abc123 was the test fixture name.",
    ],
)
def test_naming_a_credential_is_not_storing_one(text: str) -> None:
    """BEHAVIOUR CHANGE: a bare mention no longer rejects.

    Previously any text containing `api_key`, `private key`, `password=` or
    `access_token` was rejected outright, so ordinary notes discussing
    credential hygiene could not be stored at all. A credential is now
    recognised as a secret-shaped name assigned an actual value.

    The cost is stated rather than hidden: a sentence that says a key is
    present without showing one, such as "The api_key for staging is tucked in
    here.", now commits. That sentence is grammatically indistinguishable from
    the first case above, so no rule separates them without semantics.
    """
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(canonical_text=text, domain="professional", sensitivity="internal"),
    )

    assert decision.write_mode == "commit", f"ordinary prose rejected: {text}"
    assert "unsafe_secret_storage" not in decision.reasons


@pytest.mark.parametrize(
    "text",
    [
        # Synthetic fixtures, not real credentials. They have to keep the shape
        # of the real thing to exercise the guard, which is also the shape the
        # repository secret scanner looks for, so each is allowed explicitly
        # rather than by broadening the scanner's configuration.
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG",  # gitleaks:allow
        "X_API_TOKEN=abcdef123456",  # gitleaks:allow
        "AZURE_STORAGE_ACCOUNT_KEY=abc123",  # gitleaks:allow
        "PGPASSWORD=hunter2",  # gitleaks:allow
        # Realistic material since 2026-09-23: "sk-proj_QQQQQQQQ" is a
        # repeated-character placeholder, which the design round's detector
        # reads as filler, not a key.
        "OPENAI_API_KEY=sk-proj-" + "Zq8mP2vL9kXw4TbN7cRy1uJh6fDs3aGe",  # gitleaks:allow
        "export AWS_SECRET_ACCESS_KEY=wJalrXUtn9",  # gitleaks:allow
        '{"secret_key": "abcdef123456"}',  # gitleaks:allow
        "ALICE_AGENT_API_KEY=alice_sk_HGl9",  # gitleaks:allow
    ],
)
def test_prefixed_credential_assignments_are_rejected(text: str) -> None:
    """A vendor prefix used to defeat the guard entirely.

    The old key-name rule led with a word boundary, and an underscore is a word
    character, so `SECRET_KEY=` matched while `AWS_SECRET_ACCESS_KEY=` did not.
    A pasted .env line is the most ordinary route a live key takes into a note.
    """
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(canonical_text=text),
    )

    assert decision.write_mode == "reject", f"credential accepted: {text}"
    assert "unsafe_secret_storage" in decision.reasons


@pytest.mark.parametrize(
    "text",
    [
        "The task-list template is in docs/templates.",
        "Reviewed the risk-limit changes today.",
        "Akia is a colleague on the platform team.",
        "Filed under desk-level notes.",
        "Ask-list of questions for the retro.",
        "Check disk-usage before the next deploy.",
    ],
)
def test_ordinary_prose_is_not_read_as_a_credential(text: str) -> None:
    """Credential prefixes must start a token, not match inside a word.

    Matched as bare substrings, `sk-` fires inside "task-list", "risk-limit",
    "desk-level" and "disk-usage", and `akia` fires on the name "Akia".
    """
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(canonical_text=text, domain="professional", sensitivity="internal"),
    )

    assert decision.write_mode == "commit", f"ordinary prose rejected: {text}"
    assert "unsafe_secret_storage" not in decision.reasons


@pytest.mark.parametrize(
    "text",
    [
        "The key is sk-live_abcd1234 for staging.",
        "Token ghp_aaaabbbbcccc was rotated.",
        "Slack bot uses xoxb-" + "123456789012-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx now.",
        "Root id AKIAIOSFODNN7EXAMPLE must be revoked.",
        "Saved mypassword=hunter2 by mistake.",
    ],
)
def test_real_credentials_are_still_rejected_after_the_boundary_fix(text: str) -> None:
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(canonical_text=text),
    )

    assert decision.write_mode == "reject", f"credential accepted: {text}"
    assert "unsafe_secret_storage" in decision.reasons


def test_ordinary_source_refs_still_commit() -> None:
    decision = evaluate_memory_commit_policy(
        identity=None,
        request=_request(
            domain="professional",
            sensitivity="internal",
            source_refs=("doc-1", {"source_id": "doc-2"}, ["doc-3"]),
        ),
    )

    assert decision.write_mode == "commit"
    assert "unsafe_secret_storage" not in decision.reasons

    sensitive = evaluate_memory_commit_policy(
        identity=None,
        request=_request(domain="health", sensitivity="confidential"),
    )
    assert sensitive.write_mode == "confirm_inline"
    assert "sensitive_memory_requires_confirmation" in sensitive.reasons


def test_memory_commit_can_defer_embedding_provider_work_until_after_commit(
    monkeypatch,
) -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store, defer_embeddings=True)
    monkeypatch.setattr(
        "alicebot_api.vnext_memory_commit.attach_memory_embedding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider-backed embedding must be deferred")),
    )

    result = service.commit(
        identity=_identity("trusted_local_agent"),
        request=_request(domain="professional", sensitivity="internal"),
    )

    assert result["status"] == "committed"
    assert len(service.deferred_embedding_inputs) == 1
    deferred = service.deferred_embedding_inputs[0]
    assert deferred.memory_id == result["memory"]["id"]
    assert deferred.canonical_text == "Sam prefers coffee before noon."


def test_memory_correction_defers_reembedding_of_updated_text() -> None:
    store = TargetedLookupStore()
    initial = VNextMemoryCommitService(store).commit(
        identity=_identity("trusted_local_agent"),
        request=_request(domain="professional", sensitivity="internal"),
    )
    service = VNextMemoryCommitService(store, defer_embeddings=True)

    corrected = service.correct(
        identity=_identity("trusted_local_agent"),
        memory_id=str(initial["memory"]["id"]),
        canonical_text="Sam prefers tea before noon.",
    )

    assert corrected["status"] == "committed"
    assert len(service.deferred_embedding_inputs) == 1
    assert service.deferred_embedding_inputs[0].canonical_text == ("Sam prefers tea before noon.")


def test_read_only_agent_is_rejected() -> None:
    decision = evaluate_memory_commit_policy(
        identity=_identity("read_only_agent"),
        request=_request(domain="professional", sensitivity="internal"),
    )

    assert decision.write_mode == "reject"
    assert decision.status == "rejected"
    assert "read_only_agent_cannot_write" in decision.reasons


def test_read_only_agent_private_sensitivity_stays_policy_blocked() -> None:
    # A self-declared agent_id without a permission_profile defaults to
    # read_only_agent; private-sensitivity commits stay blocked on that path.
    decision = evaluate_memory_commit_policy(
        identity=_identity("read_only_agent"),
        request=_request(),
    )

    assert decision.write_mode == "reject"
    assert decision.status == "rejected"
    assert "agent_policy_blocked" in decision.reasons


def test_project_scoped_agent_can_commit_project_memory_in_scope() -> None:
    decision = evaluate_memory_commit_policy(
        identity=_identity("project_scoped_agent", project_scope=("Alice",)),
        request=_request(domain="project", sensitivity="private", project_scope=("Alice",)),
    )

    assert decision.write_mode == "commit"
    assert decision.status == "committed"


def test_project_scoped_agent_rejects_non_project_memory() -> None:
    decision = evaluate_memory_commit_policy(
        identity=_identity("project_scoped_agent", project_scope=("Alice",)),
        request=_request(domain="family", sensitivity="private", project_scope=("Alice",)),
    )

    assert decision.write_mode == "reject"
    assert "project_scoped_agent_domain_out_of_scope" in decision.reasons


def test_contradiction_requires_inline_confirmation() -> None:
    decision = evaluate_memory_commit_policy(
        identity=_identity("trusted_local_agent"),
        request=_request(domain="professional", sensitivity="internal", contradiction_refs=("memory-old",)),
    )

    assert decision.write_mode == "confirm_inline"
    assert "contradiction_requires_confirmation" in decision.reasons


class TargetedLookupStore:
    """Fake vNext store that fails loudly if the commit path falls back to full-table scans."""

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, object]] = {}
        self.events: list[dict[str, object]] = []
        self.revisions: list[dict[str, object]] = []
        self.agent_identities: dict[str, dict[str, object]] = {}
        self.lookup_calls: list[tuple[str, object]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def upsert_agent_identity(self, identity: dict[str, object], **_kwargs) -> dict[str, object]:
        self.agent_identities[str(identity["agent_id"])] = identity
        return identity

    def create_memory(self, memory: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**memory, "id": f"memory-{len(self.memories) + 1}"}
        self.memories[str(row["id"])] = row
        return row

    def update_memory(self, *, memory_id: str, patch: dict[str, object], **_kwargs) -> dict[str, object]:
        memory = self.memories[memory_id]
        memory.update(patch)
        return memory

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        return self.memories.get(memory_id)

    def list_memories(self, *, status: str | None = None) -> list[dict[str, object]]:
        raise AssertionError("commit path must use targeted lookups, not full-table scans")

    def get_memory_by_commit_digest(self, commit_digest: str) -> dict[str, object] | None:
        self.lookup_calls.append(("commit_digest", commit_digest))
        for memory in self.memories.values():
            if memory.get("commit_digest") == commit_digest:
                return memory
        return None

    def get_memory_by_confirmation_id(self, confirmation_id: str) -> dict[str, object] | None:
        self.lookup_calls.append(("confirmation_id", confirmation_id))
        for memory in self.memories.values():
            if memory.get("confirmation_id") == confirmation_id:
                return memory
        return None

    def latest_agentic_commit_memory(self, *, agent_id: str | None = None) -> dict[str, object] | None:
        self.lookup_calls.append(("latest_agentic_commit", agent_id))
        for memory in reversed(list(self.memories.values())):
            if memory.get("status") != "active":
                continue
            metadata = memory.get("metadata_json")
            agentic = metadata.get("agentic_memory", {}) if isinstance(metadata, dict) else {}
            if agentic.get("kind") != "agentic_memory_commit":
                continue
            identity = agentic.get("agent_identity")
            if agent_id is None or (isinstance(identity, dict) and identity.get("agent_id") == agent_id):
                return memory
        return None

    def append_revision(self, revision: dict[str, object], **_kwargs) -> dict[str, object]:
        row = {**revision, "id": f"revision-{len(self.revisions) + 1}"}
        self.revisions.append(row)
        return row

    def list_revisions(self, memory_id: str) -> list[dict[str, object]]:
        return [revision for revision in self.revisions if revision.get("memory_id") == memory_id]

    def list_events(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        rows = [
            event
            for event in self.events
            if (target_type is None or event.get("target_type") == target_type)
            and (target_id is None or event.get("target_id") == target_id)
        ]
        return rows[:limit] if limit is not None else rows

    def create_provenance_link(self, link: dict[str, object], **_kwargs) -> dict[str, object]:
        return {**link, "id": "provenance-1"}

    def list_provenance_links(self, *, target_type: str, target_id: str) -> list[dict[str, object]]:
        return []


def test_commit_idempotent_replay_uses_commit_digest_lookup() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    request = _request(
        domain="professional",
        sensitivity="internal",
        idempotency_key="retry-digest-1",
    )

    first = service.commit(identity=identity, request=request)
    second = service.commit(identity=identity, request=request)

    assert first["status"] == "committed"
    assert store.memories[str(first["memory"]["id"])]["commit_digest"] == "retry-digest-1"
    assert second["idempotent_replay"] is True
    assert second["memory"]["id"] == first["memory"]["id"]
    assert ("commit_digest", "retry-digest-1") in store.lookup_calls


def test_idempotency_key_reuse_with_different_content_is_rejected() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    first = _request(
        domain="professional",
        sensitivity="internal",
        idempotency_key="content-bound-retry",
    )
    service.commit(identity=identity, request=first)

    with pytest.raises(VNextMemoryCommitValidationError, match="different memory request"):
        service.commit(
            identity=identity,
            request=_request(
                domain="professional",
                sensitivity="internal",
                canonical_text="This is unrelated content.",
                idempotency_key="content-bound-retry",
            ),
        )


class _ConcurrentWinnerSQLiteStore(SQLiteVNextStore):
    """Hide the winner once so the service exercises its conflict replay."""

    def __init__(self, conn, user_id):
        super().__init__(conn, user_id)
        self.lookup_count = 0

    def get_memory_by_commit_digest(self, commit_digest: str):  # type: ignore[override]
        self.lookup_count += 1
        if self.lookup_count == 1:
            return None
        return super().get_memory_by_commit_digest(commit_digest)


def test_concurrent_idempotent_winner_replays_without_duplicate_side_effects() -> None:
    store = _live_sqlite_store()
    identity = _identity("trusted_local_agent")
    request = _request(
        domain="professional",
        sensitivity="internal",
        idempotency_key="concurrent-retry",
    )
    first = VNextMemoryCommitService(store).commit(identity=identity, request=request)
    revisions_before = len(store.list_revisions(str(first["memory"]["id"])))

    racing_store = _ConcurrentWinnerSQLiteStore(store.conn, store.user_id)
    replay = VNextMemoryCommitService(racing_store).commit(identity=identity, request=request)

    assert replay["idempotent_replay"] is True
    assert replay["memory"]["id"] == first["memory"]["id"]
    assert len(store.list_revisions(str(first["memory"]["id"]))) == revisions_before
    assert (
        store.conn.execute("SELECT COUNT(*) FROM memories WHERE commit_digest = 'concurrent-retry'").fetchone()[0] == 1
    )


def test_confirm_uses_confirmation_id_lookup_and_persisted_column() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )
    confirmation_id = pending["confirmation_id"]
    assert pending["status"] == "confirmation_required"
    assert store.memories[str(pending["memory"]["id"])]["confirmation_id"] == confirmation_id

    confirmed = service.confirm(identity=identity, confirmation_id=confirmation_id)

    assert confirmed["status"] == "committed"
    assert confirmed["memory"]["status"] == "active"
    assert ("confirmation_id", confirmation_id) in store.lookup_calls


def test_memory_status_vocabulary_includes_stale() -> None:
    assert "stale" in MEMORY_STATUSES
    # The base row statuses stay present; "stale" extends, not replaces.
    for status in ("candidate", "active", "rejected", "superseded", "archived", "needs_review"):
        assert status in MEMORY_STATUSES


def test_confirm_refreshes_last_confirmed_at_and_notes_it_in_revision() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )
    memory_id = str(pending["memory"]["id"])
    assert store.memories[memory_id].get("last_confirmed_at") is None

    confirmed = service.confirm(identity=identity, confirmation_id=pending["confirmation_id"])

    assert confirmed["status"] == "committed"
    assert store.memories[memory_id]["last_confirmed_at"] is not None
    confirm_revisions = [
        revision for revision in store.revisions if revision.get("action") == "agentic_memory_confirm_confirm"
    ]
    assert confirm_revisions[-1]["metadata_json"]["last_confirmed_at_refreshed"] is True


def test_repeated_confirm_of_a_committed_row_refuses_and_writes_nothing() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )
    confirmation_id = pending["confirmation_id"]
    memory_id = str(pending["memory"]["id"])

    first = service.confirm(identity=identity, confirmation_id=confirmation_id)
    assert first["status"] == "committed"
    frozen_memory = deepcopy(store.memories[memory_id])
    frozen_revisions = deepcopy(store.revisions)
    frozen_events = deepcopy(store.events)

    with pytest.raises(VNextMemoryCommitValidationError, match="confirmation is not pending"):
        service.confirm(identity=identity, confirmation_id=confirmation_id)

    assert store.memories[memory_id] == frozen_memory
    assert store.revisions == frozen_revisions
    assert store.events == frozen_events
    assert not any(revision.get("action") == "agentic_memory_reconfirm" for revision in store.revisions)


def test_confirm_reject_replay_is_idempotent_without_mutation() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )
    confirmation_id = pending["confirmation_id"]

    rejected = service.confirm(identity=identity, confirmation_id=confirmation_id, action="reject")
    revisions_after_reject = len(store.revisions)
    replay = service.confirm(identity=identity, confirmation_id=confirmation_id, action="reject")

    assert rejected["status"] == "rejected"
    assert replay["status"] == "rejected"
    assert replay["idempotent_replay"] is True
    assert len(store.revisions) == revisions_after_reject


def test_expired_confirmation_records_rejection_revision_and_event() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )
    memory_id = str(pending["memory"]["id"])
    confirmation_id = str(pending["confirmation_id"])
    agentic = store.memories[memory_id]["metadata_json"]["agentic_memory"]
    agentic["confirmation"]["expires_at"] = "2020-01-01T00:00:00Z"

    result = service.confirm(identity=identity, confirmation_id=confirmation_id)

    assert result["status"] == "rejected"
    assert result["write_mode"] == "confirm_inline"
    row = store.memories[memory_id]
    assert row["status"] == "rejected"
    assert row["metadata_json"]["agentic_memory"]["confirmation"]["status"] == "expired"
    expired_revisions = [
        revision for revision in store.revisions if revision.get("action") == "agentic_memory_confirmation_expired"
    ]
    assert len(expired_revisions) == 1
    assert expired_revisions[0]["revision_type"] == "rejected"
    assert expired_revisions[0]["metadata_json"]["confirmation_id"] == confirmation_id
    expired_events = [event for event in store.events if event.get("event_type") == "agent.memory_confirmation_expired"]
    assert len(expired_events) == 1
    assert expired_events[0]["payload_json"]["confirmation_id"] == confirmation_id


def test_correct_refreshes_last_confirmed_at() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal"),
    )
    memory_id = str(committed["memory"]["id"])
    committed_at = store.memories[memory_id]["last_confirmed_at"]

    corrected = service.correct(
        identity=identity,
        memory_id=memory_id,
        canonical_text="Sam prefers tea before noon.",
        reason="User corrected the beverage.",
    )

    assert corrected["status"] == "committed"
    assert store.memories[memory_id]["last_confirmed_at"] is not None
    assert store.memories[memory_id]["last_confirmed_at"] >= committed_at
    correction_revisions = [
        revision for revision in store.revisions if revision.get("action") == "agentic_memory_correct"
    ]
    assert correction_revisions[-1]["metadata_json"]["last_confirmed_at_refreshed"] is True


def test_committed_memory_persists_first_class_scope_columns() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = AgentIdentity(
        agent_id="openclaw",
        agent_type="coding_agent",
        agent_run_id="run-2026-07-04-001",
        permission_profile="project_scoped_agent",
        project_scope=("alicebot",),
    )

    committed = service.commit(
        identity=identity,
        request=_request(domain="project", sensitivity="internal", project_scope=("alicebot",)),
    )

    assert committed["status"] == "committed"
    row = store.memories[str(committed["memory"]["id"])]
    assert row["project_scope"] == ["alicebot"]
    assert row["project_id"] == "alicebot"
    assert row["created_by_agent_id"] == "openclaw"
    assert row["run_id"] == "run-2026-07-04-001"


def test_scope_columns_fall_back_to_identity_project_scope_when_request_has_none() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = AgentIdentity(
        agent_id="openclaw",
        agent_type="coding_agent",
        agent_run_id="run-7",
        permission_profile="project_scoped_agent",
        project_scope=("alicebot",),
    )

    committed = service.commit(
        identity=identity,
        request=_request(domain="project", sensitivity="internal"),
    )

    row = store.memories[str(committed["memory"]["id"])]
    assert row["project_scope"] == ["alicebot"]
    assert row["project_id"] == "alicebot"


def test_multi_project_scope_stays_metadata_only_and_project_id_is_null() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            project_scope=("alicebot", "hermes"),
        ),
    )

    row = store.memories[str(committed["memory"]["id"])]
    # Ambiguous scope: the hard column never guesses.
    assert row["project_scope"] == ["alicebot", "hermes"]
    assert row["project_id"] is None
    assert row["metadata_json"]["project_scope"] == ["alicebot", "hermes"]
    assert row["metadata_json"]["agentic_memory"]["project_scope"] == ["alicebot", "hermes"]
    assert row["created_by_agent_id"] == identity.agent_id


def test_confirmation_and_review_candidates_also_carry_scope_columns() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = AgentIdentity(
        agent_id="hermes",
        agent_type="personal_assistant",
        agent_run_id="run-9",
        permission_profile="trusted_local_agent",
        project_scope=("alicebot",),
    )

    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )
    assert pending["status"] == "confirmation_required"
    pending_row = store.memories[str(pending["memory"]["id"])]
    assert pending_row["project_id"] == "alicebot"
    assert pending_row["created_by_agent_id"] == "hermes"
    assert pending_row["run_id"] == "run-9"

    review = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            source_type="browser_clip",
        ),
    )
    assert review["status"] == "review_required"
    review_row = store.memories[str(review["memory"]["id"])]
    assert review_row["status"] == "candidate"
    assert review_row["project_id"] == "alicebot"
    assert review_row["created_by_agent_id"] == "hermes"
    assert review_row["run_id"] == "run-9"


def test_inline_confirmation_queue_contains_only_pending_actionable_rows() -> None:
    class ConfirmationQueueStore(TargetedLookupStore):
        def list_memories(self, *, status: str | None = None) -> list[dict[str, object]]:
            return [row for row in self.memories.values() if status is None or row.get("status") == status]

    store = ConfirmationQueueStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    pending = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", confidence=0.7),
    )

    assert [row["id"] for row in service.inline_confirmations()] == [pending["memory"]["id"]]

    service.confirm(
        identity=identity,
        confirmation_id=str(pending["confirmation_id"]),
        action="confirm",
    )

    assert service.inline_confirmations() == []


def test_undo_without_memory_id_uses_latest_agentic_commit_lookup() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal"),
    )
    undone = service.undo(identity=identity)

    assert undone["status"] == "undone"
    assert undone["memory"]["id"] == committed["memory"]["id"]
    assert undone["memory"]["status"] == "superseded"
    assert ("latest_agentic_commit", identity.agent_id) in store.lookup_calls


# -- entity linking on the commit path (live sqlite) ---------------------------


def _live_sqlite_store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "commit@example.com")
    return SQLiteVNextStore(conn, user_id)


def test_committed_memory_links_entities_with_memory_mention_edges() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            title="Standup preference",
            canonical_text="We met Jane Doe, who prefers async standups at Northwind Capital.",
        ),
    )

    assert committed["status"] == "committed"
    memory_id = str(committed["memory"]["id"])
    person = store.get_entity_by_normalized_name("person", "jane doe")
    org = store.get_entity_by_normalized_name("organization", "northwind capital")
    assert person is not None and org is not None
    edges = store.list_edges(from_id=memory_id)
    assert {(str(edge["to_id"]), str(edge["edge_type"])) for edge in edges} == {
        (str(person["id"]), ENTITY_MENTION_EDGE_TYPE),
        (str(org["id"]), ENTITY_MENTION_EDGE_TYPE),
    }
    assert all(edge["from_type"] == "memory" for edge in edges)
    assert all(edge["observed_at"] is not None for edge in edges)


def test_person_memory_creates_person_entity_and_about_edge() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            memory_type="person",
            title="Jane Doe — Northwind intro",
            canonical_text="GP at a seed fund; met about the continuity layer.",
        ),
    )

    assert committed["status"] == "committed"
    memory_id = str(committed["memory"]["id"])
    person = store.get_entity_by_normalized_name("person", "jane doe")
    assert person is not None
    assert person["entity_type"] == "person"
    about_edges = [
        edge for edge in store.list_edges(from_id=memory_id) if str(edge["edge_type"]) == PERSON_ABOUT_EDGE_TYPE
    ]
    assert len(about_edges) == 1
    assert str(about_edges[0]["to_id"]) == str(person["id"])
    assert about_edges[0]["metadata_json"]["relation"] == "about"


def test_person_memory_reuses_the_existing_person_entity() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    seeded = store.create_entity(
        {
            "entity_type": "person",
            "name": "Jane Doe",
            "first_observed_at": "2026-06-01T00:00:00Z",
            "last_observed_at": "2026-06-01T00:00:00Z",
            "mention_count": 3,
        }
    )

    service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            memory_type="person",
            title="Jane Doe",
            canonical_text="Now leading the continuity round.",
        ),
    )

    entities = store.list_entities(entity_type="person")
    assert [str(row["id"]) for row in entities] == [str(seeded["id"])]
    refreshed = store.get_entity(str(seeded["id"]))
    assert refreshed["mention_count"] == 4
    assert refreshed["first_observed_at"] == "2026-06-01T00:00:00Z"


def test_review_candidates_do_not_link_entities_until_accepted() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    review = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            source_type="browser_clip",
            title="Clipped bio",
            canonical_text="Zara Quill founded Quillworks Labs.",
        ),
    )

    assert review["status"] == "review_required"
    assert review["memory"]["status"] == "candidate"
    assert store.list_entities() == []
    assert store.list_edges(from_id=str(review["memory"]["id"])) == []


def test_inline_confirmation_links_entities_only_after_acceptance() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            confidence=0.7,
            title="Intro note",
            canonical_text="We met Ondrej Pavel, who leads the Prague office.",
        ),
    )
    assert pending["status"] == "confirmation_required"
    assert store.list_entities() == []

    confirmed = service.confirm(identity=identity, confirmation_id=pending["confirmation_id"])

    assert confirmed["status"] == "committed"
    person = store.get_entity_by_normalized_name("person", "ondrej pavel")
    assert person is not None
    edges = store.list_edges(from_id=str(confirmed["memory"]["id"]))
    assert [(str(edge["to_id"]), str(edge["edge_type"])) for edge in edges] == [
        (str(person["id"]), ENTITY_MENTION_EDGE_TYPE)
    ]


def test_rejected_inline_confirmation_never_links_entities() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            confidence=0.7,
            title="Intro note",
            canonical_text="We met Ondrej Pavel, who leads the Prague office.",
        ),
    )
    rejected = service.confirm(identity=identity, confirmation_id=pending["confirmation_id"], action="reject")

    assert rejected["status"] == "rejected"
    assert store.list_entities() == []


def test_correction_links_entities_introduced_by_the_new_text() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            title="Round lead",
            canonical_text="The continuity round has no lead yet.",
        ),
    )
    memory_id = str(committed["memory"]["id"])

    service.correct(
        identity=identity,
        memory_id=memory_id,
        canonical_text="We met Jane Doe, who leads the continuity round.",
        reason="Lead confirmed.",
    )

    person = store.get_entity_by_normalized_name("person", "jane doe")
    assert person is not None
    assert (str(person["id"]), ENTITY_MENTION_EDGE_TYPE) in {
        (str(edge["to_id"]), str(edge["edge_type"])) for edge in store.list_edges(from_id=memory_id)
    }


def test_correction_replaces_fact_keys_and_expires_obsolete_entity_edges() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            title="Bike-a-Thon result",
            canonical_text="Jane Doe said the Bike-a-Thon raised $5,000.",
        ),
    )
    memory_id = str(committed["memory"]["id"])
    old_person = store.get_entity_by_normalized_name("person", "jane doe")
    assert old_person is not None
    original_fact_keys = store.conn.execute("SELECT fact_keys FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
    assert "charity event fundraiser fundraising" in str(original_fact_keys)

    service.correct(
        identity=identity,
        memory_id=memory_id,
        canonical_text="We met Zara Quill, who confirmed rainy weather for the picnic.",
        reason="The original note was attached to the wrong event.",
    )

    corrected_fact_keys = store.conn.execute("SELECT fact_keys FROM memories WHERE id = ?", (memory_id,)).fetchone()[0]
    assert "charity" not in str(corrected_fact_keys)
    assert store.search_memories(query="charity event fundraiser fundraising", limit=10) == []
    new_person = store.get_entity_by_normalized_name("person", "zara quill")
    assert new_person is not None
    active_edges = store.list_edges(from_id=memory_id)
    assert (str(new_person["id"]), ENTITY_MENTION_EDGE_TYPE) in {
        (str(edge["to_id"]), str(edge["edge_type"])) for edge in active_edges
    }
    assert (str(old_person["id"]), ENTITY_MENTION_EDGE_TYPE) not in {
        (str(edge["to_id"]), str(edge["edge_type"])) for edge in active_edges
    }
    historical = store.list_edges_as_of("9999-01-01T00:00:00Z", limit=100)
    assert (str(old_person["id"]), ENTITY_MENTION_EDGE_TYPE) not in {
        (str(edge["to_id"]), str(edge["edge_type"])) for edge in historical
    }


def test_correction_cannot_resurrect_a_forgotten_memory() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    committed = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal"),
    )
    memory_id = str(committed["memory"]["id"])
    service.forget(identity=identity, memory_id=memory_id, reason="User asked to forget it.")

    with pytest.raises(VNextMemoryCommitValidationError, match="cannot correct a retired superseded memory"):
        service.correct(
            identity=identity,
            memory_id=memory_id,
            canonical_text="Attempted resurrection.",
        )
    assert store.get_memory(memory_id)["status"] == "superseded"


class _BrokenEntityLookupSQLiteStore(SQLiteVNextStore):
    def find_entities_by_names(self, normalized_names):  # type: ignore[override]
        raise RuntimeError("entity lookup exploded")


def test_entity_linking_failure_never_fails_the_commit() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "broken-commit@example.com")
    store = _BrokenEntityLookupSQLiteStore(conn, user_id)
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            title="Standup preference",
            canonical_text="Jane Doe prefers async standups.",
        ),
    )

    assert committed["status"] == "committed"
    failures = [event for event in store.list_events() if event.get("event_type") == "entity.extraction_failed"]
    assert len(failures) == 1
    assert failures[0]["payload_json"]["stage"] == "commit"
    assert failures[0]["payload_json"]["error_code"] == "entity_linking_failed"
    assert failures[0]["payload_json"]["error_message"] == "Memory entity linking failed"
    assert "entity lookup exploded" not in str(failures[0]["payload_json"])


def test_stores_without_the_entity_surface_commit_without_linking() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    committed = service.commit(
        identity=identity,
        request=_request(
            domain="professional",
            sensitivity="internal",
            canonical_text="Jane Doe prefers async standups.",
        ),
    )

    assert committed["status"] == "committed"
    assert not [event for event in store.events if event.get("event_type") == "entity.extraction_failed"]


# -- temporal slice: supersession pointers and the audit chain -----------------


def _commit_active(service: VNextMemoryCommitService, identity: AgentIdentity, *, title: str, text: str) -> str:
    result = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", title=title, canonical_text=text),
    )
    assert result["status"] == "committed"
    return str(result["memory"]["id"])


def test_undo_with_replacement_sets_pointer_columns_on_both_rows() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    old_id = _commit_active(service, identity, title="Coffee", text="Sam prefers coffee before noon.")
    new_id = _commit_active(service, identity, title="Tea", text="Sam switched to green tea before noon.")

    undone = service.undo(
        identity=identity,
        memory_id=old_id,
        reason="Replaced by the tea preference.",
        superseded_by_memory_id=new_id,
    )

    old_row = store.memories[old_id]
    new_row = store.memories[new_id]
    assert undone["status"] == "undone"
    assert undone["memory"]["status"] == "superseded"
    # Real pointer columns on both rows...
    assert old_row["superseded_by"] == new_id
    assert new_row["supersedes"] == old_id
    # ...plus the metadata_json copies for backward compatibility.
    assert old_row["metadata_json"]["superseded_by"] == new_id
    assert new_row["metadata_json"]["supersedes"] == old_id
    # The revision and the audit event carry the pointer too.
    superseded_revision = store.revisions[-1]
    assert superseded_revision["revision_type"] == "superseded"
    assert superseded_revision["metadata_json"]["superseded_by"] == new_id
    undone_events = [event for event in store.events if event.get("event_type") == "agent.memory_undone"]
    assert undone_events[-1]["payload_json"]["superseded_by"] == new_id


def test_undo_without_replacement_keeps_pointer_columns_null() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    memory_id = _commit_active(service, identity, title="Solo", text="A fact retired without replacement.")

    undone = service.undo(identity=identity, memory_id=memory_id, reason="Wrong fact")

    assert undone["memory"]["status"] == "superseded"
    assert store.memories[memory_id].get("superseded_by") is None
    assert "superseded_by" not in store.memories[memory_id]["metadata_json"]


def test_undo_rejects_missing_or_self_replacement() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    memory_id = _commit_active(service, identity, title="Target", text="A fact to undo.")

    with pytest.raises(VNextMemoryCommitValidationError, match="superseding memory was not found"):
        service.undo(identity=identity, memory_id=memory_id, superseded_by_memory_id="memory-missing")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot supersede itself"):
        service.undo(identity=identity, memory_id=memory_id, superseded_by_memory_id=memory_id)


def test_audit_supersession_chain_walks_both_directions_oldest_first() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    a_id = _commit_active(service, identity, title="v1", text="Version one of the fact.")
    b_id = _commit_active(service, identity, title="v2", text="Version two of the fact.")
    c_id = _commit_active(service, identity, title="v3", text="Version three of the fact.")
    service.undo(identity=identity, memory_id=a_id, superseded_by_memory_id=b_id)
    service.undo(identity=identity, memory_id=b_id, superseded_by_memory_id=c_id)

    audit = service.audit(memory_id=b_id)
    chain = audit["supersession_chain"]

    assert [(entry["id"], entry["relation"]) for entry in chain] == [
        (a_id, "predecessor"),
        (b_id, "self"),
        (c_id, "successor"),
    ]
    assert [entry["title"] for entry in chain] == ["v1", "v2", "v3"]
    assert [entry["status"] for entry in chain] == ["superseded", "superseded", "active"]
    assert set(chain[0]) == {"id", "title", "status", "created_at", "relation"}
    # The same chain is visible from either end.
    assert [entry["id"] for entry in service.audit(memory_id=a_id)["supersession_chain"]] == [a_id, b_id, c_id]
    assert [entry["id"] for entry in service.audit(memory_id=c_id)["supersession_chain"]] == [a_id, b_id, c_id]


def test_audit_authorizes_every_supersession_node_before_assembling_the_envelope() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    a_id = _commit_active(service, identity, title="v1", text="Version one.")
    b_id = _commit_active(service, identity, title="v2", text="Version two.")
    c_id = _commit_active(service, identity, title="v3", text="Version three.")
    service.undo(identity=identity, memory_id=a_id, superseded_by_memory_id=b_id)
    service.undo(identity=identity, memory_id=b_id, superseded_by_memory_id=c_id)
    authorized: list[str] = []

    def authorize(memory) -> None:
        memory_id = str(memory["id"])
        authorized.append(memory_id)
        if memory_id == c_id:
            raise RuntimeError("successor is outside the caller's scope")

    with pytest.raises(RuntimeError, match="outside the caller's scope"):
        service.audit(memory_id=b_id, authorize_memory=authorize)

    assert authorized == [b_id, a_id, c_id]


@pytest.mark.parametrize(
    ("direction", "mixed"),
    [
        ("supersedes", False),
        ("superseded_by", False),
        ("supersedes", True),
        ("superseded_by", True),
    ],
)
def test_audit_fails_closed_for_unresolved_supersession_pointer_without_partial_envelope(
    direction: str,
    mixed: bool,
) -> None:
    class AuditReadRecordingStore(TargetedLookupStore):
        def __init__(self) -> None:
            super().__init__()
            self.envelope_reads: list[str] = []

        def list_revisions(self, memory_id: str) -> list[dict[str, object]]:
            self.envelope_reads.append("revisions")
            return super().list_revisions(memory_id)

        def list_events(self, **kwargs) -> list[dict[str, object]]:
            self.envelope_reads.append("events")
            return super().list_events(**kwargs)

        def list_provenance_links(self, **kwargs) -> list[dict[str, object]]:
            self.envelope_reads.append("provenance")
            return super().list_provenance_links(**kwargs)

    store = AuditReadRecordingStore()
    root = store.create_memory({"memory_key": "audit.root", "value": {}, "status": "active", "title": "Root"})
    root_id = str(root["id"])
    if mixed:
        reachable = store.create_memory(
            {
                "memory_key": "audit.reachable",
                "value": {},
                "status": "superseded",
                "title": "Reachable",
            }
        )
        reachable_id = str(reachable["id"])
        root[direction] = reachable_id
        reachable[direction] = "memory-missing"
    else:
        root[direction] = "memory-missing"

    with pytest.raises(
        VNextMemoryCommitValidationError,
        match="supersession chain contains an unresolved pointer",
    ):
        VNextMemoryCommitService(store).audit(memory_id=root_id)

    assert store.envelope_reads == []


def test_audit_supersession_chain_reads_metadata_only_pointers_from_legacy_rows() -> None:
    """Rows written before the pointer columns existed recorded supersession
    in metadata_json only; the chain walker falls back to those keys."""
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    old = store.create_memory(
        {
            "memory_key": "legacy.old",
            "value": {},
            "status": "superseded",
            "title": "Legacy old",
            "metadata_json": {},
        }
    )
    new = store.create_memory(
        {
            "memory_key": "legacy.new",
            "value": {},
            "status": "active",
            "title": "Legacy new",
            "metadata_json": {"supersedes": str(old["id"])},
        }
    )
    old["metadata_json"] = {"superseded_by": str(new["id"])}

    chain = service.audit(memory_id=str(old["id"]))["supersession_chain"]

    assert [(entry["id"], entry["relation"]) for entry in chain] == [
        (str(old["id"]), "self"),
        (str(new["id"]), "successor"),
    ]


def test_audit_supersession_chain_is_cycle_safe_and_depth_bounded() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    ids: list[str] = []
    for index in range(15):
        row = store.create_memory(
            {"memory_key": f"chain.{index}", "value": {}, "status": "superseded", "title": f"v{index}"}
        )
        ids.append(str(row["id"]))
    for index in range(14):
        store.memories[ids[index]]["superseded_by"] = ids[index + 1]
        store.memories[ids[index + 1]]["supersedes"] = ids[index]

    # Depth is bounded to 10 hops per direction.
    from_start = service.audit(memory_id=ids[0])["supersession_chain"]
    assert len(from_start) == 11  # self + 10 successors
    assert from_start[0]["id"] == ids[0]

    # A pointer cycle terminates instead of looping.
    store.memories[ids[14]]["superseded_by"] = ids[0]
    cyclic = service.audit(memory_id=ids[12])["supersession_chain"]
    ids_in_chain = [entry["id"] for entry in cyclic]
    assert len(ids_in_chain) == len(set(ids_in_chain))
    assert [entry["relation"] for entry in cyclic].count("self") == 1


# -- consolidation acceptance ---------------------------------------------------


def _seed_row(
    store: TargetedLookupStore,
    *,
    title: str,
    text: str,
    status: str = "active",
    domain: str = "professional",
    sensitivity: str = "internal",
    metadata: dict | None = None,
) -> str:
    row = store.create_memory(
        {
            "memory_key": f"memory.{title.casefold().replace(' ', '.')}",
            "value": {"text": text},
            "status": status,
            "memory_type": "semantic",
            "title": title,
            "canonical_text": text,
            "summary": text[:80],
            "domain": domain,
            "sensitivity": sensitivity,
            "metadata_json": metadata or {},
        }
    )
    return str(row["id"])


@pytest.mark.parametrize("operation", ["correct", "forget", "undo"])
@pytest.mark.parametrize("marker", ["workflow", "memory_key"])
def test_generic_lifecycle_mutations_cannot_strand_pending_project_update_candidate(
    operation: str,
    marker: str,
) -> None:
    store = TargetedLookupStore()
    metadata: dict[str, object] = {"candidate": True}
    if marker == "workflow":
        metadata["workflow"] = "project_auto_update"
    memory_id = _seed_row(
        store,
        title="Pending project update",
        text="Proposed project state.",
        status="candidate",
        metadata=metadata,
    )
    if marker == "memory_key":
        store.memories[memory_id]["memory_key"] = "  project_update.alice.digest  "
    service = VNextMemoryCommitService(store)
    state_before = deepcopy((store.memories, store.revisions, store.events))

    with pytest.raises(
        VNextMemoryCommitValidationError,
        match=f"^{PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE}$",
    ):
        if operation == "correct":
            service.correct(
                identity=None,
                memory_id=memory_id,
                canonical_text="Generic correction must not apply.",
            )
        elif operation == "forget":
            service.forget(identity=None, memory_id=memory_id, reason="Generic forget must not apply.")
        else:
            service.undo(identity=None, memory_id=memory_id, reason="Generic undo must not apply.")

    assert (store.memories, store.revisions, store.events) == state_before


@pytest.mark.parametrize(
    ("memory", "expected"),
    [
        ({"memory_key": "ordinary.key", "metadata_json": {}}, False),
        ({"memory_key": "project_update.alice.digest", "metadata_json": {}}, True),
        ({"memory_key": "ordinary.key", "metadata_json": {"workflow": "project_auto_update"}}, True),
        (
            {
                "memory_key": "project_update.alice.digest",
                "metadata_json": {"candidate": False},
            },
            False,
        ),
    ],
)
def test_pending_project_update_guard_requires_exact_terminal_candidate_marker(
    memory: dict[str, object],
    expected: bool,
) -> None:
    assert is_pending_project_update_memory(memory) is expected


def test_terminal_project_update_memory_can_use_generic_correction() -> None:
    store = TargetedLookupStore()
    memory_id = _seed_row(
        store,
        title="Reviewed project update",
        text="Accepted project state.",
        metadata={"workflow": "project_auto_update", "candidate": False},
    )

    result = VNextMemoryCommitService(store).correct(
        identity=None,
        memory_id=memory_id,
        canonical_text="Later correction after project review.",
    )

    assert result["status"] == "committed"
    assert store.memories[memory_id]["canonical_text"] == "Later correction after project review."


def _seed_consolidation_candidate(
    store: TargetedLookupStore,
    *,
    member_ids: list[str],
    proposal_kind: str,
    survivor_memory_id: str | None,
    proposed_supersede: list[str],
    status: str = "candidate",
) -> str:
    return _seed_row(
        store,
        title=f"{proposal_kind} proposal",
        text="Sam prefers oat milk lattes every morning before standup.",
        status=status,
        metadata={
            "candidate_kind": "memory_consolidation",
            "consolidation_digest": "digest-1",
            "review_required": True,
            "consolidation": {
                "cluster_member_ids": member_ids,
                "member_snapshots": [memory_version_snapshot(store.memories[member_id]) for member_id in member_ids],
                "proposal_kind": proposal_kind,
                "survivor_memory_id": survivor_memory_id,
                "proposed_supersede": proposed_supersede,
                "reviewer_instructions": ["Review candidate memory."],
            },
        },
    )


def test_accept_merge_candidate_executes_the_proposed_supersessions() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    members = [_seed_row(store, title=f"Latte {index}", text=f"Latte fact {index}.") for index in range(3)]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )

    result = service.accept_consolidation_candidate(candidate_id, reason="Reviewed and accepted the merge.")

    assert result["status"] == "accepted"
    assert result["idempotent_replay"] is False
    assert result["superseded_member_ids"] == members
    # Promotion: active, freshness signals refreshed, review flag cleared.
    accepted = store.memories[candidate_id]
    assert accepted["status"] == "active"
    assert accepted["confirmation_status"] == "confirmed"
    assert accepted["last_confirmed_at"]
    assert accepted["last_reviewed_at"]
    assert accepted["metadata_json"]["review_required"] is False
    # Merge pointer semantics: supersedes stays NULL; merged_from carries members.
    assert accepted.get("supersedes") is None
    assert accepted["metadata_json"]["merged_from"] == members
    assert accepted["metadata_json"]["consolidation"]["accepted"]["superseded_member_ids"] == members
    # Every member is superseded by the accepted candidate, with pointer copies.
    for member_id in members:
        member = store.memories[member_id]
        assert member["status"] == "superseded"
        assert member["superseded_by"] == candidate_id
        assert member["metadata_json"]["superseded_by"] == candidate_id
    # Revisions: one superseded revision per member plus the promoted revision.
    member_revisions = [row for row in store.revisions if row["revision_type"] == "superseded"]
    assert {row["memory_id"] for row in member_revisions} == set(members)
    assert all(row["action"] == "agentic_memory_consolidation_supersede" for row in member_revisions)
    promoted = [row for row in store.revisions if row["revision_type"] == "promoted"]
    assert len(promoted) == 1 and promoted[0]["memory_id"] == candidate_id
    assert promoted[0]["metadata_json"]["last_confirmed_at_refreshed"] is True
    # Events: one per member supersession plus the acceptance event.
    superseded_events = [e for e in store.events if e.get("event_type") == "agent.memory_superseded"]
    assert {e["target_id"] for e in superseded_events} == set(members)
    accepted_events = [e for e in store.events if e.get("event_type") == "agent.memory_consolidation_accepted"]
    assert len(accepted_events) == 1
    assert accepted_events[0]["target_id"] == candidate_id
    assert accepted_events[0]["payload_json"]["superseded_member_ids"] == members


def test_accept_consolidation_locks_graph_before_candidate_and_member_rows() -> None:
    class OrderedLockStore(TargetedLookupStore):
        def __init__(self) -> None:
            super().__init__()
            self.lock_order: list[str] = []

        def lock_graph_mutation(self) -> None:
            self.lock_order.append("graph")

        def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
            self.lock_order.append(f"row:{memory_id}")
            return self.get_memory(memory_id)

    store = OrderedLockStore()
    member_id = _seed_row(store, title="Member", text="Member fact.")
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=[member_id],
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=[member_id],
    )

    VNextMemoryCommitService(store).accept_consolidation_candidate(
        candidate_id,
        reason="Reviewed and accepted.",
    )

    assert store.lock_order[0] == "graph"
    assert store.lock_order[1:] == [f"row:{candidate_id}", f"row:{member_id}"]


def test_accept_replay_is_a_noop_with_a_note() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    members = [_seed_row(store, title=f"Fact {index}", text=f"Fact {index}.") for index in range(2)]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )

    first = service.accept_consolidation_candidate(candidate_id, reason="Accept.")
    revisions_after_first = len(store.revisions)
    events_after_first = len(store.events)
    second = service.accept_consolidation_candidate(candidate_id, reason="Accept again.")

    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert "already accepted" in second["note"]
    assert second["superseded_member_ids"] == members
    assert second["skipped_members"] == first["skipped_members"]
    assert second["supersedes"] == first["supersedes"]
    assert second["policy_decision"]["decision"] == "allowed"
    assert len(store.revisions) == revisions_after_first
    assert len(store.events) == events_after_first


def test_accept_dedup_candidate_points_supersedes_at_the_survivor() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    survivor = _seed_row(store, title="Survivor", text="Sam prefers oat milk lattes every morning before standup.")
    dropped = [_seed_row(store, title=f"Duplicate {index}", text="Sam prefers oat milk lattes.") for index in range(2)]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=[survivor, *dropped],
        proposal_kind="dedup",
        survivor_memory_id=survivor,
        proposed_supersede=list(dropped),
    )

    result = service.accept_consolidation_candidate(candidate_id, reason="Dedup accepted.")

    # Dedup pointer semantics: the single-valued supersedes column records
    # the survivor the candidate's text descends from, while every original
    # member is retired so there is exactly one active representative.
    accepted = store.memories[candidate_id]
    assert result["supersedes"] == survivor
    assert accepted["supersedes"] == survivor
    assert accepted["metadata_json"]["supersedes"] == survivor
    assert "merged_from" not in accepted["metadata_json"]
    for member_id in [survivor, *dropped]:
        assert store.memories[member_id]["status"] == "superseded"
        assert store.memories[member_id]["superseded_by"] == candidate_id
    assert result["superseded_member_ids"] == [survivor, *dropped]


def test_accept_rejects_consolidation_candidate_that_crosses_project_scopes() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    member_a = _seed_row(
        store,
        title="Project A",
        text="Shared fact.",
        metadata={"project_scope": ["project-a"]},
    )
    member_b = _seed_row(
        store,
        title="Project B",
        text="Shared fact.",
        metadata={"project_scope": ["project-b"]},
    )
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=[member_a, member_b],
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=[member_a, member_b],
    )

    with pytest.raises(VNextMemoryCommitValidationError, match="crosses project scopes"):
        service.accept_consolidation_candidate(candidate_id, reason="Must not cross scopes.")

    assert store.memories[member_a]["status"] == "active"
    assert store.memories[member_b]["status"] == "active"
    assert store.memories[candidate_id]["status"] == "candidate"


def test_accept_rejects_candidate_when_member_changed_after_proposal() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    members = [_seed_row(store, title=f"Member {index}", text=f"Member fact {index}.") for index in range(2)]
    replacement = _seed_row(store, title="Manual replacement", text="Manually corrected fact.")
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )
    service.undo(identity=None, memory_id=members[0], superseded_by_memory_id=replacement)

    with pytest.raises(VNextMemoryCommitValidationError, match="candidate is stale"):
        service.accept_consolidation_candidate(candidate_id, reason="Accept with a stale member.")

    # No partial supersession occurred and the manual pointer was not overwritten.
    assert store.memories[members[0]]["superseded_by"] == replacement
    assert store.memories[members[1]]["status"] == "active"
    assert store.memories[candidate_id]["status"] == "candidate"


def test_accept_validates_every_snapshot_before_first_supersession() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    members = [_seed_row(store, title=f"Member {index}", text=f"Member fact {index}.") for index in range(3)]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )
    store.update_memory(
        memory_id=members[-1],
        patch={"canonical_text": "The final member changed after review was requested."},
    )

    with pytest.raises(VNextMemoryCommitValidationError, match="candidate is stale"):
        service.accept_consolidation_candidate(candidate_id, reason="Accept stale merge.")

    assert all(store.memories[member_id]["status"] == "active" for member_id in members)
    assert all(store.memories[member_id].get("superseded_by") is None for member_id in members)
    assert store.memories[candidate_id]["status"] == "candidate"
    assert not any(revision.get("revision_type") == "superseded" for revision in store.revisions)


def test_rollup_candidate_without_snapshots_fails_closed() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    member_id = _seed_row(store, title="Member", text="One reviewed roll-up input.")
    candidate_id = _seed_row(
        store,
        title="Legacy roll-up",
        text="A legacy roll-up candidate without version evidence.",
        status="candidate",
        metadata={
            "candidate_kind": "memory_rollup",
            "review_required": True,
            "consolidation": {
                "proposal_kind": "rollup",
                "cluster_member_ids": [member_id],
                "proposed_supersede": [],
            },
        },
    )

    with pytest.raises(VNextMemoryCommitValidationError, match="lacks member version snapshots"):
        service.accept_consolidation_candidate(candidate_id, reason="Accept unverifiable roll-up.")

    assert store.memories[member_id]["status"] == "active"
    assert store.memories[candidate_id]["status"] == "candidate"


def test_missing_final_member_stale_fails_before_any_supersession() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    members = [_seed_row(store, title=f"Member {index}", text=f"Member fact {index}.") for index in range(3)]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )
    del store.memories[members[-1]]

    with pytest.raises(VNextMemoryCommitValidationError, match="member .* is missing"):
        service.accept_consolidation_candidate(candidate_id, reason="Accept with missing member.")

    assert all(store.memories[member_id]["status"] == "active" for member_id in members[:-1])
    assert store.memories[candidate_id]["status"] == "candidate"
    assert not any(revision.get("revision_type") == "superseded" for revision in store.revisions)


def test_accept_relinks_entities_on_a_live_sqlite_store() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    member = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {"text": "Jane Doe leads Northwind Capital."},
            "status": "active",
            "memory_type": "semantic",
            "title": "Member fact",
            "canonical_text": "We met Jane Doe, who leads Northwind Capital.",
            "domain": "professional",
            "sensitivity": "internal",
        }
    )
    candidate = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {"text": "Jane Doe is leading Northwind Capital."},
            "status": "candidate",
            "memory_type": "semantic",
            "title": "Merge proposal",
            "canonical_text": "We met Jane Doe, who is leading Northwind Capital.",
            "domain": "professional",
            "sensitivity": "internal",
            "metadata_json": {
                "candidate_kind": "memory_consolidation",
                "review_required": True,
                "consolidation": {
                    "cluster_member_ids": [str(member["id"])],
                    "member_snapshots": [memory_version_snapshot(member)],
                    "proposal_kind": "merge",
                    "survivor_memory_id": None,
                    "proposed_supersede": [str(member["id"])],
                },
            },
        }
    )
    candidate_id = str(candidate["id"])
    # Candidates do not link entities until accepted.
    assert store.list_edges(from_id=candidate_id) == []

    result = service.accept_consolidation_candidate(candidate_id, reason="Reviewed on the dashboard.")

    assert result["status"] == "accepted"
    person = store.get_entity_by_normalized_name("person", "jane doe")
    org = store.get_entity_by_normalized_name("organization", "northwind capital")
    assert person is not None and org is not None
    edges = store.list_edges(from_id=candidate_id)
    assert {(str(edge["to_id"]), str(edge["edge_type"])) for edge in edges} == {
        (str(person["id"]), ENTITY_MENTION_EDGE_TYPE),
        (str(org["id"]), ENTITY_MENTION_EDGE_TYPE),
    }
    refreshed_member = store.get_memory(str(member["id"]))
    assert refreshed_member["status"] == "superseded"
    assert str(refreshed_member["superseded_by"]) == candidate_id


def test_accept_validates_candidate_shape_and_status() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    plain = _seed_row(store, title="Plain", text="Not a consolidation candidate.")
    wrong_status = _seed_consolidation_candidate(
        store,
        member_ids=[plain],
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=[plain],
        status="active",
    )

    with pytest.raises(VNextMemoryCommitValidationError, match="memory was not found"):
        service.accept_consolidation_candidate("memory-missing", reason="Accept.")
    with pytest.raises(VNextMemoryCommitValidationError, match="not a consolidation candidate"):
        service.accept_consolidation_candidate(plain, reason="Accept.")
    with pytest.raises(VNextMemoryCommitValidationError, match="candidate or needs_review"):
        service.accept_consolidation_candidate(wrong_status, reason="Accept.")
    # Nothing was mutated by the failed attempts.
    assert store.memories[plain]["status"] == "active"
    assert store.revisions == []


def test_accept_is_policy_blocked_for_non_admin_agent_identities() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    member = _seed_row(store, title="Member", text="A fact to merge.")
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=[member],
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=[member],
    )

    with pytest.raises(AgentPolicyBlockedError):
        service.accept_consolidation_candidate(
            candidate_id, reason="Agent accept.", identity=_identity("trusted_local_agent")
        )

    # Review acceptance is human-or-admin: nothing changed and the block is audited.
    assert store.memories[candidate_id]["status"] == "candidate"
    assert store.memories[member]["status"] == "active"
    assert any(event.get("event_type") == "agent.policy_blocked" for event in store.events)

    admin = AgentIdentity(agent_id="warden", permission_profile="admin_agent")
    accepted = service.accept_consolidation_candidate(candidate_id, reason="Admin accept.", identity=admin)
    assert accepted["status"] == "accepted"
    assert store.memories[member]["superseded_by"] == candidate_id


# -- expire / unexpire -----------------------------------------------------------


def test_expire_hides_the_memory_from_live_sqlite_retrieval_and_unexpire_restores() -> None:
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    memory_id = _commit_active(
        service,
        identity,
        title="Planning cadence",
        text="Quarterly planning cadence happens on Thursdays.",
    )

    assert any(str(row["id"]) == memory_id for row in store.search_memories(query="quarterly planning cadence"))

    expired = service.expire(memory_id, reason="Cadence changed after the offsite.")

    # Status stays active; the row is excluded purely by its validity window.
    row = store.get_memory(memory_id)
    assert expired["status"] == "expired"
    assert row["status"] == "active"
    assert row["valid_to"] is not None
    assert not any(str(hit["id"]) == memory_id for hit in store.search_memories(query="quarterly planning cadence"))
    assert any(
        str(hit["id"]) == memory_id
        for hit in store.search_memories(query="quarterly planning cadence", include_expired=True)
    )
    revisions = store.list_revisions(memory_id)
    expire_revisions = [r for r in revisions if r["action"] == "agentic_memory_expire"]
    assert len(expire_revisions) == 1
    assert expire_revisions[0]["revision_type"] == "edited"
    assert expire_revisions[0]["metadata_json"]["note"] == "expired"
    assert any(
        event["event_type"] == "agent.memory_expired"
        for event in store.list_events(target_type="memory", target_id=memory_id)
    )

    restored = service.unexpire(memory_id, reason="Cadence reinstated.")

    assert restored["status"] == "active"
    assert restored["idempotent_replay"] is False
    assert any(str(hit["id"]) == memory_id for hit in store.search_memories(query="quarterly planning cadence"))
    # SQLite's update_memory COALESCEs, so the clear lands as the documented
    # far-future sentinel and is recorded in metadata.
    row = store.get_memory(memory_id)
    assert str(row["valid_to"]) == VALID_TO_UNBOUNDED_SENTINEL
    assert row["metadata_json"]["validity"]["unbounded_sentinel"] == VALID_TO_UNBOUNDED_SENTINEL
    unexpire_revisions = [r for r in store.list_revisions(memory_id) if r["action"] == "agentic_memory_unexpire"]
    assert len(unexpire_revisions) == 1
    assert unexpire_revisions[0]["metadata_json"]["note"] == "unexpired"
    assert any(
        event["event_type"] == "agent.memory_unexpired"
        for event in store.list_events(target_type="memory", target_id=memory_id)
    )

    # A re-expire over the sentinel takes effect again.
    service.expire(memory_id, reason="Cancelled for good.")
    assert not any(str(hit["id"]) == memory_id for hit in store.search_memories(query="quarterly planning cadence"))


def test_expire_accepts_an_explicit_valid_to_and_stores_clear_null() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    memory_id = _seed_row(store, title="Sabbatical", text="Sam is on sabbatical.")

    expired = service.expire(memory_id, valid_to="2026-09-01T00:00:00Z", reason="Sabbatical ends September 1.")

    assert expired["valid_to"] == "2026-09-01T00:00:00Z"
    assert store.memories[memory_id]["valid_to"] == "2026-09-01T00:00:00Z"
    assert store.memories[memory_id]["status"] == "active"
    assert store.memories[memory_id]["metadata_json"]["validity"]["state"] == "expired"

    restored = service.unexpire(memory_id, reason="Sabbatical extended indefinitely.")

    # Dict-backed stores honor the NULL write directly: no sentinel needed.
    assert restored["idempotent_replay"] is False
    assert store.memories[memory_id]["valid_to"] is None
    assert store.memories[memory_id]["metadata_json"]["validity"]["state"] == "cleared"
    assert "unbounded_sentinel" not in store.memories[memory_id]["metadata_json"]["validity"]


def test_unexpire_replays_as_a_noop_when_nothing_is_expired() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    memory_id = _seed_row(store, title="Fresh", text="Never expired.")

    result = service.unexpire(memory_id, reason="Nothing to clear.")

    assert result["idempotent_replay"] is True
    assert "no validity end" in result["note"]
    assert store.revisions == []


def test_unexpire_noop_reports_the_returned_rows_actual_status() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    memory_id = _seed_row(store, title="Stale", text="No validity end.", status="stale")

    result = service.unexpire(memory_id, reason="Nothing to clear.")

    assert result["idempotent_replay"] is True
    assert result["status"] == "stale"
    assert result["memory"]["status"] == "stale"
    assert store.revisions == []


def test_expire_is_policy_blocked_for_an_out_of_scope_agent_identity() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    memory_id = _seed_row(store, title="Family fact", text="A family fact.", domain="family", sensitivity="private")

    with pytest.raises(AgentPolicyBlockedError) as blocked:
        service.expire(
            memory_id,
            reason="Out of scope expire.",
            identity=_identity("project_scoped_agent", project_scope=("Alice",)),
        )

    assert "all_requested_domains_restricted" in blocked.value.decision.reasons
    assert store.memories[memory_id].get("valid_to") is None
    assert store.memories[memory_id]["status"] == "active"
    assert any(event.get("event_type") == "agent.policy_blocked" for event in store.events)

    # Read-only profiles cannot write, even for an unrestricted domain:
    # expire mirrors the WRITE_ACTIONS block.
    unrestricted_id = _seed_row(store, title="Work fact", text="A professional fact.")
    with pytest.raises(AgentPolicyBlockedError) as read_only_blocked:
        service.expire(unrestricted_id, reason="Read-only expire.", identity=_identity("read_only_agent"))
    assert "read_only_agent_cannot_write" in read_only_blocked.value.decision.reasons
    assert store.memories[unrestricted_id].get("valid_to") is None

    # An in-scope trusted agent expires the same row fine, with policy audit.
    result = service.expire(memory_id, reason="Trusted expire.", identity=_identity("trusted_local_agent"))
    assert result["status"] == "expired"
    assert store.memories[memory_id]["valid_to"] is not None


def test_expire_and_unexpire_validation_failures_leave_no_writes() -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    superseded_id = _seed_row(store, title="Old", text="Superseded fact.", status="superseded")
    rejected_id = _seed_row(store, title="Rejected", text="Rejected fact.", status="rejected")
    active_id = _seed_row(store, title="Active", text="Active fact.")

    with pytest.raises(VNextMemoryCommitValidationError, match="memory was not found"):
        service.expire("memory-missing", reason="Expire.")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot expire a superseded memory"):
        service.expire(superseded_id, reason="Expire.")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot expire a rejected memory"):
        service.expire(rejected_id, reason="Expire.")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot unexpire a superseded memory"):
        service.unexpire(superseded_id, reason="Unexpire.")
    with pytest.raises(VNextMemoryCommitValidationError, match="valid_to must be an ISO-8601 timestamp"):
        service.expire(active_id, valid_to="not-a-timestamp", reason="Expire.")
    with pytest.raises(VNextMemoryCommitValidationError, match="reason must not be empty"):
        service.expire(active_id, reason="   ")
    assert store.revisions == []
    for row in store.memories.values():
        assert row.get("valid_to") is None


# -- lifecycle transition table: reproductions for audit P1 #1 / #2 ------------
#
# A single centrally enforced transition table (vnext_lifecycle) must reject
# impossible/reversible transitions on every backend. Each test below first
# reproduces the audit's exact scenario against the pre-fix code (where it
# fails), then locks in the corrected behavior.


def test_confirm_refuses_a_row_a_review_already_rejected() -> None:
    """Audit #1(a): confirmation_required -> rejected -> confirm must NOT reactivate.

    A dashboard/review rejection retires the row (status -> rejected) but the
    nested inline-confirmation flag is left ``pending`` (mcp/review.py).
    confirm() must verify the row's lifecycle status, not just the flag.
    """
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(domain="health", sensitivity="private", confidence=0.95),
    )
    assert pending["status"] == "confirmation_required"
    memory_id = str(pending["memory"]["id"])
    confirmation_id = pending["confirmation_id"]

    # Review rejection retires the row while leaving the nested flag pending.
    store.update_memory(memory_id=memory_id, patch={"status": "rejected"}, actor_type="user")

    with pytest.raises(VNextMemoryCommitValidationError):
        service.confirm(identity=identity, confirmation_id=confirmation_id)
    assert store.get_memory(memory_id)["status"] == "rejected"


def test_confirm_refuses_a_superseded_row_and_never_yields_two_active_memories() -> None:
    """Audit #1(b): confirmation_required -> superseded -> confirm must NOT reactivate.

    Otherwise the superseded row and its replacement are both active and
    contradictory.
    """
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    pending = service.commit(
        identity=identity,
        request=_request(domain="health", sensitivity="private", confidence=0.95),
    )
    assert pending["status"] == "confirmation_required"
    pending_id = str(pending["memory"]["id"])
    confirmation_id = pending["confirmation_id"]

    replacement_id = _commit_active(service, identity, title="Replacement", text="The confirmed replacement fact.")
    # Review supersede retires the pending row, leaving the nested flag pending.
    store.update_memory(
        memory_id=pending_id,
        patch={"status": "superseded", "superseded_by": replacement_id},
        actor_type="user",
    )

    with pytest.raises(VNextMemoryCommitValidationError):
        service.confirm(identity=identity, confirmation_id=confirmation_id)

    assert store.get_memory(pending_id)["status"] == "superseded"
    active_ids = {str(row["id"]) for row in store.list_memories(status="active")}
    assert pending_id not in active_ids
    assert replacement_id in active_ids


def test_correct_promoting_a_review_candidate_confirms_it_and_clears_review() -> None:
    """Audit #1: correct() must not leave a promoted row unconfirmed / review_required.

    A dashboard-review candidate corrected into ``active`` must also carry a
    consistent confirmed/reviewed state.
    """
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")

    review = service.commit(
        identity=identity,
        request=_request(domain="professional", sensitivity="internal", source_type="browser_clip"),
    )
    assert review["status"] == "review_required"
    memory_id = str(review["memory"]["id"])
    seeded = store.get_memory(memory_id)
    assert seeded["status"] == "candidate"
    assert seeded["confirmation_status"] == "unconfirmed"
    assert seeded["metadata_json"]["review_required"] is True

    corrected = service.correct(
        identity=identity,
        memory_id=memory_id,
        canonical_text="Corrected and reviewed fact.",
        reason="Reviewer rewrote the candidate.",
    )
    assert corrected["memory"]["status"] == "active"
    row = store.get_memory(memory_id)
    assert row["confirmation_status"] == "confirmed"
    assert row["metadata_json"].get("review_required") is False


def test_undo_cannot_supersede_back_to_an_ancestor() -> None:
    """Audit #1: supersession must not permit A -> B -> A cycles."""
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    a_id = _commit_active(service, identity, title="A", text="Version A of the fact.")
    b_id = _commit_active(service, identity, title="B", text="Version B of the fact.")

    service.undo(identity=identity, memory_id=a_id, superseded_by_memory_id=b_id)

    # Re-superseding B back to its own predecessor A would close an A<->B cycle.
    with pytest.raises(VNextMemoryCommitValidationError):
        service.undo(identity=identity, memory_id=b_id, superseded_by_memory_id=a_id)

    assert store.memories[b_id]["status"] == "active"
    assert store.memories[a_id]["status"] == "superseded"


def test_supersession_acquires_the_graph_mutation_lock() -> None:
    """Audit 2 P1 #1: the cycle guard must serialize graph mutation through the
    store's advisory lock before checking/writing the supersession edge, so two
    concurrent supersessions on disjoint pairs cannot together close a cycle."""

    class LockRecordingStore(TargetedLookupStore):
        def __init__(self) -> None:
            super().__init__()
            self.graph_locks = 0

        def lock_graph_mutation(self) -> None:
            self.graph_locks += 1

    store = LockRecordingStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    a_id = _commit_active(service, identity, title="A", text="Version A of the fact.")
    b_id = _commit_active(service, identity, title="B", text="Version B of the fact.")

    service.undo(identity=identity, memory_id=a_id, superseded_by_memory_id=b_id)

    assert store.graph_locks >= 1


def test_supersession_acquires_graph_lock_before_any_row_lock() -> None:
    class OrderedLockStore(TargetedLookupStore):
        def __init__(self) -> None:
            super().__init__()
            self.lock_order: list[str] = []

        def lock_graph_mutation(self) -> None:
            self.lock_order.append("graph")

        def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
            self.lock_order.append(f"row:{memory_id}")
            return self.get_memory(memory_id)

    store = OrderedLockStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    old_id = _commit_active(service, identity, title="Old", text="Old fact.")
    successor_id = _commit_active(service, identity, title="New", text="New fact.")

    service.undo(
        identity=identity,
        memory_id=old_id,
        superseded_by_memory_id=successor_id,
    )

    assert store.lock_order == ["graph", f"row:{old_id}", f"row:{successor_id}"]


@pytest.mark.parametrize("operation", ["correct", "forget", "expire", "unexpire"])
def test_member_lifecycle_mutations_lock_graph_before_the_row(operation: str) -> None:
    class OrderedLockStore(TargetedLookupStore):
        def __init__(self) -> None:
            super().__init__()
            self.lock_order: list[str] = []

        def lock_graph_mutation(self) -> None:
            self.lock_order.append("graph")

        def get_memory_for_update(self, memory_id: str) -> dict[str, object] | None:
            self.lock_order.append(f"row:{memory_id}")
            return self.get_memory(memory_id)

    store = OrderedLockStore()
    memory_id = _seed_row(store, title="Member", text="Member lifecycle fact.")
    if operation == "unexpire":
        store.memories[memory_id]["valid_to"] = "2020-01-01T00:00:00+00:00"
    service = VNextMemoryCommitService(store)

    if operation == "correct":
        service.correct(
            identity=None,
            memory_id=memory_id,
            canonical_text="Corrected member lifecycle fact.",
        )
    elif operation == "forget":
        service.forget(identity=None, memory_id=memory_id, reason="Forget it.")
    elif operation == "expire":
        service.expire(memory_id, reason="Expire it.")
    else:
        service.unexpire(memory_id, reason="Restore it.")

    assert store.lock_order == ["graph", f"row:{memory_id}"]


@pytest.mark.parametrize(
    "successor_status",
    ["candidate", "needs_review", "stale", "rejected", "superseded", "archived", "unknown"],
)
def test_undo_rejects_a_successor_that_cannot_be_a_live_chain_head(
    successor_status: str,
) -> None:
    store = TargetedLookupStore()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    old_id = _commit_active(service, identity, title="Old", text="Old fact.")
    successor_id = _seed_row(
        store,
        title="Invalid successor",
        text="Invalid replacement.",
        status=successor_status,
    )

    with pytest.raises(VNextMemoryCommitValidationError, match="accepted live successor"):
        service.undo(
            identity=identity,
            memory_id=old_id,
            superseded_by_memory_id=successor_id,
        )

    assert store.memories[old_id]["status"] == "active"
    assert store.memories[old_id].get("superseded_by") is None


def test_unexpire_restores_a_stale_expired_row_to_active_and_retrievable() -> None:
    """Audit #2: unexpire must not report ``active`` while the row stays stale.

    The staleness sweep marks long-expired rows ``stale`` (unretrievable).
    Clearing the validity window must bring the row back to a retrievable
    ``active`` state that matches the reported status.
    """
    store = _live_sqlite_store()
    service = VNextMemoryCommitService(store)
    identity = _identity("trusted_local_agent")
    memory_id = _commit_active(service, identity, title="Cadence", text="Weekly planning cadence on Mondays.")
    service.expire(memory_id, reason="Paused after the reorg.")
    # The staleness sweep marks the expired row stale; it is now unretrievable.
    store.update_memory(memory_id=memory_id, patch={"status": "stale"}, actor_type="scheduler")
    assert not any(
        str(hit["id"]) == memory_id for hit in store.search_memories(query="weekly planning cadence mondays")
    )

    restored = service.unexpire(memory_id, reason="Cadence reinstated.")

    assert restored["status"] == "active"
    row = store.get_memory(memory_id)
    assert row["status"] == "active"
    assert any(str(hit["id"]) == memory_id for hit in store.search_memories(query="weekly planning cadence mondays"))
