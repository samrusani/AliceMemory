"""Approve, accept, propose and reject each meet the credential floor, at the store.

Why this file exists (S4.4 round 2, owner rulings C2, C6, R1 and R2,
addendum F1 and F2, 2026-09-23). The round 1 branch (86e35f1) put the credential
check on every door that writes text, but not on the doors that only change
a row's status, nor on the three that propose one. Reproduced on that
branch: a candidate row holding a GitHub token (written before the floor
existed, or by a background writer that never checks) became active through
alice_memory_correct approve, consolidation accept and inline confirm; the
three memory.propose doors stored a token in the title, text, rationale or
source_refs with no check, which under an auto-promote persona is an active
row; the promotion floor ran its own credential rule, which disagreed with
the write floor both ways; and confirm(reject) refused a rationale carrying
a token, so the reject never completed, while a caller's canonical_text on
reject went into memory_revisions.text_after unchecked; expire, forget,
undo and the quarantine sweep stored a credential reason in the row, the
revision and the event, and unexpire reactivated a row without reading it.

How it escaped. The floor's tests asserted the doors that write text. An
approve writes only a status, so nothing asserted what a status change makes
searchable. Every test here asserts at the store: the row that must stay a
candidate, or the token that must be absent from memories, memory_revisions
and event_log. The Postgres doors (HTTP review accept, promote and reject,
project-update accept, HTTP propose, legacy continuity confirm and delete,
and the /v0/memories/admit routes) are in
tests/integration/test_credential_floor_every_door_api.py.
"""

from __future__ import annotations

import argparse
import ast
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from alicebot_api import vnext_promotion_policy as policy
from alicebot_api.cli import memories as cli_memories
from alicebot_api.continuity_review import ContinuityReviewValidationError, apply_continuity_correction
from alicebot_api.contracts import ContinuityCorrectionInput
from alicebot_api.credential_floor import (
    RATIONALE_WITHHELD_PLACEHOLDER,
    SEARCHABLE_STATUSES,
    TEXT_WITHHELD_PLACEHOLDER,
    CredentialActivationRefused,
)
from alicebot_api.mcp import memories as mcp_memories
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, call_mcp_tool
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_memory_propose import MemoryProposalRefused
from alicebot_api.vnext_promotion_policy import (
    PromotionCandidate,
    PromotionSettings,
    evaluate_promotion,
    hard_floor_hits,
    promotion_candidate_for_proposal,
)
from alicebot_api.vnext_retrieval import MEMORY_SEARCHABLE_STATUSES

from tests.unit.fixtures_throwaway_keys import PRIVATE_KEY_NAMES, PUBLIC_KEY_NAMES, throwaway_key


# Built rather than written out so the source carries no scanner-shaped token.
PAT = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
USER_ID = "00000000-0000-4000-8000-0000000000ab"
REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "apps/api/src/alicebot_api"


@pytest.fixture(autouse=True)
def _no_embedding_or_promotion_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_AGENT_API_KEY",
        policy.PROMOTION_PERSONA_ENV,
        policy.PROMOTION_FILTERS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _store() -> SQLiteVNextStore:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "activation@example.com")
    return SQLiteVNextStore(conn, user_id)


def _values(row: object) -> tuple[object, ...]:
    # A bare connection yields tuples; the product connection yields mappings.
    return tuple(row.values()) if isinstance(row, dict) else tuple(row)  # type: ignore[arg-type]


def _column(conn: object, statement: str, *params: object) -> list[object]:
    return [_values(row)[0] for row in conn.execute(statement, params)]  # type: ignore[attr-defined]


def _tables_holding(conn: object, needle: str) -> list[str]:
    """Which of memories, memory_revisions and event_log carry the needle anywhere."""

    hits = []
    for table in ("memories", "memory_revisions", "event_log"):
        rows = [_values(row) for row in conn.execute(f"SELECT * FROM {table}")]  # type: ignore[attr-defined]
        if any(needle in str(row) for row in rows):
            hits.append(table)
    return hits


def _count(conn: object, table: str) -> int:
    return int(_column(conn, f"SELECT count(*) FROM {table}")[0])  # type: ignore[arg-type]


def _status(conn: object, memory_id: str) -> str:
    return str(_column(conn, "SELECT status FROM memories WHERE id = ?", memory_id)[0])


def _seed_candidate(store: SQLiteVNextStore, text: str, **extra: object) -> str:
    """A candidate row as a pre-floor vault or an unchecked background writer
    holds one. Creating a candidate is not an activation, so the store allows it."""

    row = store.create_memory(
        {
            "memory_key": f"legacy.seed.{uuid4().hex[:8]}",
            "status": "candidate",
            "title": "deploy token",
            "canonical_text": text,
            "domain": "unknown",
            "sensitivity": "unknown",
            **extra,
        }
    )
    return str(row["id"])


def _sqlite_context(tmp_path: Path) -> MCPRuntimeContext:
    db_path = tmp_path / "memory.db"
    if not db_path.exists():
        bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(db_path), user_id=USER_ID)  # type: ignore[arg-type]


@contextmanager
def _file_store(tmp_path: Path):
    with sqlite_user_connection(tmp_path / "memory.db", USER_ID) as conn:
        yield SQLiteVNextStore(conn, USER_ID)


# ---------------------------------------------------------------------------
# C2(i): the store-level activation check.
# ---------------------------------------------------------------------------


def test_the_searchable_statuses_are_the_ones_retrieval_reads() -> None:
    assert SEARCHABLE_STATUSES == frozenset(MEMORY_SEARCHABLE_STATUSES)


@pytest.mark.parametrize("status", sorted(SEARCHABLE_STATUSES))
def test_the_store_refuses_to_create_a_searchable_row_carrying_a_credential(status: str) -> None:
    store = _store()
    with pytest.raises(CredentialActivationRefused, match="cannot become active"):
        store.create_memory(
            {"memory_key": "direct.write", "status": status, "title": "deploy", "canonical_text": f"use {PAT}"}
        )
    assert _count(store.conn, "memories") == 0
    assert _tables_holding(store.conn, PAT) == []


def test_the_store_reads_a_row_moving_into_active_as_it_will_be_stored() -> None:
    store = _store()
    stored = _seed_candidate(store, f"use {PAT} for deploys")
    with pytest.raises(CredentialActivationRefused):
        store.update_memory(memory_id=stored, patch={"status": "active"})
    assert _status(store.conn, stored) == "candidate"

    # The patch's text over the stored text: a clean candidate activated
    # with a token in its new title is refused too.
    clean = _seed_candidate(store, "Deploys go out on Tuesdays.")
    with pytest.raises(CredentialActivationRefused):
        store.update_memory(memory_id=clean, patch={"status": "active", "title": f"token {PAT}"})
    assert _status(store.conn, clean) == "candidate"
    assert _count(store.conn, "memory_revisions") == 0


def test_the_store_check_leaves_clean_activations_and_retirements_alone() -> None:
    # Guards the guard: the check is not a blanket refusal.
    store = _store()
    clean = _seed_candidate(store, "Deploys go out on Tuesdays.")
    store.update_memory(memory_id=clean, patch={"status": "active"})
    assert _status(store.conn, clean) == "active"
    # A retirement of a row that carries a credential always goes through.
    dirty = _seed_candidate(store, f"use {PAT} for deploys")
    store.update_memory(memory_id=dirty, patch={"status": "rejected"})
    assert _status(store.conn, dirty) == "rejected"


# ---------------------------------------------------------------------------
# C2(i): every approve door on SQLite. The Postgres doors are in the
# integration twin.
# ---------------------------------------------------------------------------


def test_alice_memory_correct_approve_refuses_a_credential_candidate(tmp_path: Path) -> None:
    context = _sqlite_context(tmp_path)
    with _file_store(tmp_path) as store:
        dirty = _seed_candidate(store, f"use {PAT} for deploys")
        clean = _seed_candidate(store, "Deploys go out on Tuesdays.")
    with pytest.raises(MCPToolError, match="cannot become active"):
        call_mcp_tool(context, name="alice_memory_correct", arguments={"review_item_id": dirty, "action": "approve"})
    # Guards the guard: the same door approves a clean candidate.
    approved = call_mcp_tool(
        context, name="alice_memory_correct", arguments={"review_item_id": clean, "action": "approve"}
    )
    assert approved["memory"]["status"] == "active"
    with _file_store(tmp_path) as store:
        assert _status(store.conn, dirty) == "candidate"
        assert _tables_holding(store.conn, PAT) == ["memories"]
        assert _column(store.conn, "SELECT memory_id FROM memory_revisions") == [clean]


def test_inline_confirm_refuses_to_activate_a_credential_row() -> None:
    store = _store()
    service = VNextMemoryCommitService(store)
    pending = service.commit(
        identity=None,
        request=memory_commit_request_from_payload(
            {"title": "deploy day", "canonical_text": "Deploys may move to Wednesdays.", "confidence": 0.8},
            user_id=store.user_id,
        ),
    )
    memory_id = str(pending["memory"]["id"])  # type: ignore[index]
    # The row as a pre-floor vault holds it.
    store.conn.execute("UPDATE memories SET canonical_text = ? WHERE id = ?", (f"use {PAT} for deploys", memory_id))
    revisions = _count(store.conn, "memory_revisions")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        service.confirm(identity=None, confirmation_id=str(pending["confirmation_id"]), action="confirm")
    assert _status(store.conn, memory_id) == "needs_review"
    assert _count(store.conn, "memory_revisions") == revisions


def _consolidation_candidate(store: SQLiteVNextStore, text: str) -> str:
    return _seed_candidate(
        store,
        text,
        metadata_json={"consolidation": {"proposal_kind": "merge", "cluster_member_ids": [], "proposed_supersede": []}},
    )


def test_consolidation_accept_refuses_a_credential_candidate() -> None:
    store = _store()
    service = VNextMemoryCommitService(store)
    dirty = _consolidation_candidate(store, f"Merged: use {PAT} for deploys")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        service.accept_consolidation_candidate(dirty, reason="looks right")
    assert _status(store.conn, dirty) == "candidate"
    assert _count(store.conn, "memory_revisions") == 0
    # The reason is persisted with the row, so it is read with it.
    clean = _consolidation_candidate(store, "Merged: deploys go out on Tuesdays.")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        service.accept_consolidation_candidate(clean, reason=f"approved, token {PAT}")
    assert _status(store.conn, clean) == "candidate"
    # Guards the guard.
    assert service.accept_consolidation_candidate(clean, reason="looks right")["status"] == "accepted"
    assert _status(store.conn, clean) == "active"


class _ContinuityObject:
    """One stored continuity object; records every write, then stops the call."""

    def __init__(self, *, body: dict[str, object]) -> None:
        self.object_id = uuid4()
        self.row: dict[str, object] = {
            "id": self.object_id,
            "user_id": uuid4(),
            "capture_event_id": uuid4(),
            "object_type": "Decision",
            "status": "active",
            "is_preserved": True,
            "is_searchable": True,
            "is_promotable": True,
            "title": "Decision: ship on Tuesdays",
            "body": body,
            "provenance": {"source_kind": "continuity_capture_event"},
            "confidence": 0.9,
            "last_confirmed_at": None,
            "supersedes_object_id": None,
            "superseded_by_object_id": None,
        }
        self.writes: list[tuple[str, dict[str, object]]] = []

    def get_continuity_object_optional(self, object_id: object) -> dict[str, object] | None:
        return dict(self.row) if object_id == self.object_id else None

    def __getattr__(self, name: str) -> object:
        def record(*_args: object, **kwargs: object) -> object:
            self.writes.append((name, kwargs))
            raise LookupError(f"store.{name} reached")

        return record


def _correct(store: _ContinuityObject, request: ContinuityCorrectionInput) -> object:
    return apply_continuity_correction(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        continuity_object_id=store.object_id,
        request=request,
    )


def test_legacy_continuity_confirm_refuses_a_credential_object() -> None:
    dirty = _ContinuityObject(body={"decision_text": f"rotate to {PAT}"})
    with pytest.raises(ContinuityReviewValidationError, match="cannot become active"):
        _correct(dirty, ContinuityCorrectionInput(action="confirm"))
    assert dirty.writes == []
    clean = _ContinuityObject(body={"decision_text": "ship on Tuesdays"})
    with pytest.raises(ContinuityReviewValidationError, match="cannot become active"):
        _correct(clean, ContinuityCorrectionInput(action="confirm", reason=f"confirmed, token {PAT}"))
    assert clean.writes == []
    # Guards the guard: a clean confirm reaches its first write.
    with pytest.raises(LookupError):
        _correct(clean, ContinuityCorrectionInput(action="confirm", reason="still true"))
    assert [name for name, _kwargs in clean.writes] == ["create_continuity_correction_event"]


# ---------------------------------------------------------------------------
# C2(ii) and addendum F1: one propose function, gated before anything is
# written, on the MCP and CLI doors (HTTP is in the integration twin).
# ---------------------------------------------------------------------------


_PROPOSAL_FIELDS = {
    "title": {"title": f"token {PAT}"},
    "canonical_text": {"canonical_text": f"use {PAT} for deploys"},
    "rationale": {"rationale": f"because {PAT} is the deploy token"},
    "source_refs": {"source_refs": [f"note:{PAT}"]},
}


def _mcp_propose(monkeypatch: pytest.MonkeyPatch, store: SQLiteVNextStore, **arguments: object) -> dict[str, object]:
    @contextmanager
    def fake_store_context(_context: object):
        yield store

    monkeypatch.setattr(mcp_memories, "_vnext_store_context", fake_store_context)
    payload: dict[str, object] = {
        "agent_id": "hermes",
        "canonical_text": "The team deploys on Thursdays.",
        "title": "Deploy cadence",
        "domain": "professional",
        "sensitivity": "internal",
        **arguments,
    }
    return mcp_memories._handle_alice_vnext_propose_memory(
        MCPRuntimeContext(database_url="postgresql://localhost/alicebot", user_id=uuid4()),  # type: ignore[arg-type]
        payload,
    )


@pytest.mark.parametrize("field", sorted(_PROPOSAL_FIELDS))
def test_the_mcp_propose_door_refuses_before_anything_is_written(monkeypatch, field: str) -> None:
    store = _store()
    with pytest.raises(MemoryProposalRefused, match="credential material"):
        _mcp_propose(monkeypatch, store, **_PROPOSAL_FIELDS[field])
    # Nothing at all: no memory, no revision, and not even the policy audit.
    assert _count(store.conn, "memories") == 0
    assert _count(store.conn, "memory_revisions") == 0
    assert _count(store.conn, "event_log") == 0


def test_the_mcp_propose_door_persists_rationale_and_refs_it_used_to_drop(monkeypatch) -> None:
    # Guards the guard, and pins the one row shape all three doors now write.
    store = _store()
    payload = _mcp_propose(monkeypatch, store, rationale="said in standup", source_refs=["meeting:standup"])
    proposal = payload["proposal"]
    assert proposal["status"] == "candidate"  # type: ignore[index]
    assert proposal["value"]["rationale"] == "said in standup"  # type: ignore[index]
    assert proposal["metadata_json"]["source_refs"] == ["meeting:standup"]  # type: ignore[index]
    assert _count(store.conn, "memory_revisions") == 1


def _cli_args(**overrides: object) -> argparse.Namespace:
    payload: dict[str, object] = {
        "agent_id": "hermes",
        "agent_type": "personal_assistant",
        "permission_profile": "trusted_local_agent",
        "agent_run_id": None,
        "task_id": None,
        "project_scope": [],
        "sensitivity_allowed": None,
        "domain": "professional",
        "sensitivity": "internal",
        "memory_type": "semantic",
        "proposal_type": "candidate_memory",
        "title": "Deploy cadence",
        "canonical_text": "The team deploys on Thursdays.",
        "confidence": 0.6,
        "rationale": None,
        "source_ref": [],
        "contradiction_ref": [],
    }
    payload.update(overrides)
    return argparse.Namespace(**payload)


def _cli_propose(monkeypatch: pytest.MonkeyPatch, store: SQLiteVNextStore, args: argparse.Namespace) -> str:
    @contextmanager
    def fake_store_context(_ctx: object):
        yield store

    monkeypatch.setattr(cli_memories, "_vnext_store_context", fake_store_context)
    return cli_memories._run_vnext_agent_propose_memory(object(), args)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": f"token {PAT}"},
        {"canonical_text": f"use {PAT} for deploys"},
        {"rationale": f"because {PAT} is the deploy token"},
        {"source_ref": [f"note:{PAT}"]},
    ],
    ids=["title", "canonical_text", "rationale", "source_refs"],
)
def test_the_cli_propose_door_refuses_before_anything_is_written(monkeypatch, overrides: dict[str, object]) -> None:
    store = _store()
    with pytest.raises(MemoryProposalRefused, match="credential material"):
        _cli_propose(monkeypatch, store, _cli_args(**overrides))
    assert _count(store.conn, "memories") == 0
    assert _count(store.conn, "event_log") == 0
    # Guards the guard.
    assert json.loads(_cli_propose(monkeypatch, store, _cli_args()))["proposal"]["status"] == "candidate"
    assert _count(store.conn, "memories") == 1


# Realistic material, built at run time so no scanner-shaped token sits in
# the source. These are the addendum F1 cases the old promotion rule missed.
STRIPE_LIVE = "sk_" + "live_" + "51Hq8wLkT2mN9pQ4rS7vX3yZ"
ALICE_KEY = "alice" + "_sk_" + "Qm7pX2rT9vK4nW8sL3zB6cD1fG5h"
HF_TOKEN = "hf" + "_" + "Qm7pX2rT9vK4nW8sL3zB6cD1fG5hJ0kLa"
AUTHENTICATED_PERSONAL = {
    "settings": PromotionSettings(persona="personal"),
    "permission_profile": "trusted_local_agent",
    "writer_trust": "authenticated_agent",
}


@pytest.mark.parametrize(
    "candidate",
    [
        PromotionCandidate(title="billing", canonical_text=f"The Stripe key is {STRIPE_LIVE}"),
        PromotionCandidate(title="agent key", canonical_text=f"hermes uses {ALICE_KEY}"),
        PromotionCandidate(title="model hub", canonical_text=f"pull with {HF_TOKEN}"),
        # A split the old rule could not reassemble: the product's own key
        # across title and body (addendum F5 cross-seam set).
        PromotionCandidate(title=f"agent key {ALICE_KEY[:14]}", canonical_text=f"{ALICE_KEY[14:]} for hermes"),
        # Fields the old rule never read.
        PromotionCandidate(title="deploy", canonical_text="Deploys go out on Tuesdays.", rationale=f"use {PAT}"),
        PromotionCandidate(title="deploy", canonical_text="Deploys go out on Tuesdays.", project_scope=(PAT,)),
    ],
    ids=["stripe-live", "alice_sk_", "hf_", "alice_sk_ split", "rationale", "project_scope"],
)
def test_f1_the_promotion_floor_holds_back_what_the_write_floor_refuses(candidate: PromotionCandidate) -> None:
    assert "credential_material" in hard_floor_hits(candidate)
    decision = evaluate_promotion(candidate=candidate, **AUTHENTICATED_PERSONAL)  # type: ignore[arg-type]
    assert decision.tier == "hard_floor"
    assert decision.auto_promote is False


def test_f1_a_clean_twin_still_auto_promotes() -> None:
    # Guards the guard: the same writer and persona promote a clean note.
    decision = evaluate_promotion(
        candidate=PromotionCandidate(
            title="deploy",
            canonical_text="Deploys go out on Tuesdays.",
            domain="professional",
            sensitivity="internal",
            source_type="trusted_agent",
        ),
        **AUTHENTICATED_PERSONAL,  # type: ignore[arg-type]
    )
    assert decision.tier == "auto_promote"


def test_f1_the_label_split_residual_is_not_held_back_and_that_is_documented() -> None:
    """The residual addendum F5 accepts, pinned so a change to it is deliberate.

    A title label over a bare value passes both floors: the defence against a
    deliberate split is identity and trust, not the floor. See "What the
    credential check does not catch" in docs/memory/promotion-personas.md.
    """

    candidate = PromotionCandidate(title="Prod DB password", canonical_text="Kd9xoYWu83nq")
    assert "credential_material" not in hard_floor_hits(candidate)


def test_f1_the_proposal_candidate_carries_rationale_and_project_scope() -> None:
    candidate = promotion_candidate_for_proposal(canonical_text="x", rationale="why", project_scope=["alpha"])
    assert (candidate.rationale, candidate.project_scope) == ("why", ("alpha",))


# ---------------------------------------------------------------------------
# Addendum F2: SSH public keys pass every door and both floors; private keys
# pasted beside them do not. Both directions pinned.
# ---------------------------------------------------------------------------


def _fido_public_keys() -> list[str]:
    """FIDO public keys built from their wire format (ssh-keygen needs a
    hardware token to make one, so none is in the fixtures)."""

    import base64
    import struct

    def ssh_string(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value

    ed = ssh_string(b"sk-ssh-ed25519@openssh.com") + ssh_string(bytes(range(32))) + ssh_string(b"ssh:")
    ec = (
        ssh_string(b"sk-ecdsa-sha2-nistp256@openssh.com")
        + ssh_string(b"nistp256")
        + ssh_string(b"\x04" + bytes(range(64)))
        + ssh_string(b"ssh:")
    )
    return [
        "sk-ssh-ed25519@openssh.com " + base64.b64encode(ed).decode() + " yubikey@laptop",
        "sk-ecdsa-sha2-nistp256@openssh.com " + base64.b64encode(ec).decode() + " yubikey@laptop",
    ]


_PUBLIC_KEYS = [throwaway_key(name).strip() for name in PUBLIC_KEY_NAMES] + _fido_public_keys()


@pytest.mark.parametrize("key", _PUBLIC_KEYS, ids=[*PUBLIC_KEY_NAMES, "fido-ed25519", "fido-ecdsa"])
def test_f2_an_ssh_public_key_commits_and_passes_the_promotion_floor(key: str) -> None:
    store = _store()
    result = VNextMemoryCommitService(store).commit(
        identity=None,
        request=memory_commit_request_from_payload(
            {"title": "Build box deploy key", "canonical_text": f"Add to authorized_keys on web-1:\n{key}"},
            user_id=store.user_id,
        ),
    )
    assert result["status"] == "committed", result.get("reasons")
    assert "credential_material" not in hard_floor_hits(
        PromotionCandidate(title="Build box deploy key", canonical_text=key)
    )


@pytest.mark.parametrize("name", [name for name in PRIVATE_KEY_NAMES if name.startswith("openssh_")])
def test_f2_a_private_key_pasted_beside_its_public_half_is_still_refused(name: str) -> None:
    pasted = throwaway_key(name + ".pub") + "\n" + throwaway_key(name)
    store = _store()
    result = VNextMemoryCommitService(store).commit(
        identity=None,
        request=memory_commit_request_from_payload(
            {"title": "Build box keys", "canonical_text": pasted}, user_id=store.user_id
        ),
    )
    assert result["status"] == "rejected"
    assert _count(store.conn, "memories") == 0
    assert "credential_material" in hard_floor_hits(PromotionCandidate(title="keys", canonical_text=pasted))


# ---------------------------------------------------------------------------
# C6: a reject always completes, and never stores what it withholds.
# ---------------------------------------------------------------------------


def _pending(store: SQLiteVNextStore) -> dict[str, object]:
    return VNextMemoryCommitService(store).commit(
        identity=None,
        request=memory_commit_request_from_payload(
            {"title": "deploy day", "canonical_text": "Deploys may move to Wednesdays.", "confidence": 0.8},
            user_id=store.user_id,
        ),
    )


def test_confirm_reject_completes_and_withholds_a_credential_rationale() -> None:
    store = _store()
    pending = _pending(store)
    rejected = VNextMemoryCommitService(store).confirm(
        identity=None,
        confirmation_id=str(pending["confirmation_id"]),
        action="reject",
        rationale=f"it leaked {PAT}",
    )
    assert (rejected["status"], rejected["rationale_withheld"], rejected["text_withheld"]) == ("rejected", True, False)
    assert _tables_holding(store.conn, PAT) == []
    reasons = _column(store.conn, "SELECT reason FROM memory_revisions")
    assert RATIONALE_WITHHELD_PLACEHOLDER in reasons
    assert RATIONALE_WITHHELD_PLACEHOLDER in str(_column(store.conn, "SELECT metadata_json FROM memories"))


def test_confirm_reject_withholds_a_credential_canonical_text_from_text_after() -> None:
    store = _store()
    pending = _pending(store)
    rejected = VNextMemoryCommitService(store).confirm(
        identity=None,
        confirmation_id=str(pending["confirmation_id"]),
        action="reject",
        canonical_text=f"use {PAT} for deploys",
        rationale="wrong day",
    )
    assert (rejected["rationale_withheld"], rejected["text_withheld"]) == (False, True)
    assert _tables_holding(store.conn, PAT) == []
    text_after = _column(store.conn, "SELECT text_after FROM memory_revisions")
    assert text_after[-1] == TEXT_WITHHELD_PLACEHOLDER


def test_a_clean_reject_keeps_its_rationale_verbatim() -> None:
    # Guards the guard: withholding is not blanket replacement.
    store = _store()
    pending = _pending(store)
    rejected = VNextMemoryCommitService(store).confirm(
        identity=None, confirmation_id=str(pending["confirmation_id"]), action="reject", rationale="wrong day"
    )
    assert (rejected["rationale_withheld"], rejected["text_withheld"]) == (False, False)
    assert "wrong day" in _column(store.conn, "SELECT reason FROM memory_revisions")


def test_alice_memory_correct_reject_completes_and_withholds_the_reason(tmp_path: Path) -> None:
    context = _sqlite_context(tmp_path)
    with _file_store(tmp_path) as store:
        candidate = _seed_candidate(store, "Deploys may move to Wednesdays.")
    rejected = call_mcp_tool(
        context,
        name="alice_memory_correct",
        arguments={"review_item_id": candidate, "action": "reject", "reason": f"it leaked {PAT}"},
    )
    assert rejected["memory"]["status"] == "rejected"
    assert rejected["rationale_withheld"] is True
    with _file_store(tmp_path) as store:
        assert _tables_holding(store.conn, PAT) == []
        assert RATIONALE_WITHHELD_PLACEHOLDER in _column(store.conn, "SELECT reason FROM memory_revisions")


@pytest.mark.parametrize("action", ["delete", "mark_stale"])
def test_continuity_retirement_withholds_a_credential_reason(action: str) -> None:
    store = _ContinuityObject(body={"decision_text": "ship on Tuesdays"})
    with pytest.raises(LookupError):
        _correct(store, ContinuityCorrectionInput(action=action, reason=f"it leaked {PAT}", title=f"x {PAT}"))
    name, kwargs = store.writes[0]
    assert name == "create_continuity_correction_event"
    assert kwargs["reason"] == RATIONALE_WITHHELD_PLACEHOLDER
    assert kwargs["payload"]["title"] == TEXT_WITHHELD_PLACEHOLDER  # type: ignore[index]
    assert PAT not in str(kwargs)


# ---------------------------------------------------------------------------
# The enumerated activation sites (ruling C2 fallback, and R1's admit call).
# ---------------------------------------------------------------------------

# Every function that writes a row into a searchable status, and the check it
# must call. The store-level check covers every vNext row, but these are the
# doors whose refusal must arrive in the door's own error contract, plus the
# legacy stores the vNext check does not reach.
ACTIVATION_SITES: dict[tuple[str, str], str] = {
    ("vnext_stores/sqlite/memory_lifecycle.py", "create_memory"): "refuse_created_credential_activation",
    ("vnext_stores/sqlite/memory_lifecycle.py", "update_memory"): "refuse_updated_credential_activation",
    ("vnext_stores/postgres/memory_lifecycle.py", "create_memory"): "refuse_created_credential_activation",
    ("vnext_stores/postgres/memory_lifecycle.py", "update_memory"): "refuse_updated_credential_activation",
    ("vnext_memory_commit.py", "confirm"): "refuse_credential_activation",
    ("vnext_memory_commit.py", "correct"): "refuse_credential_activation",
    ("vnext_memory_commit.py", "accept_consolidation_candidate"): "refuse_credential_activation",
    ("routers/vnext_memories.py", "review_vnext_memory"): "refuse_credential_activation",
    ("mcp/review.py", "_vnext_memory_correct"): "refuse_credential_activation",
    ("continuity_review.py", "apply_continuity_correction"): "refuse_credential_activation",
    ("vnext_projects.py", "review_project_update"): "refuse_credential_activation",
    ("vnext_memory_propose.py", "propose_memory"): "refuse_proposal_credentials",
    ("vnext_queue.py", "_promote_artifact"): "refuse_credential_material",
    # Owner ruling R1: the four legacy admit routes, and the open-loop title
    # they write, meet the floor at the top of this one function.
    ("memory.py", "admit_memory_candidate"): "refuse_credential_material",
    # S4.4 round 3 (P2 item 8): still_blocked moves an open loop to active.
    ("continuity_open_loops.py", "apply_continuity_open_loop_review_action"): "refuse_credential_activation",
}
# Found by the scan below, and checked by their store only: CLI smokes and
# eval seeds write fixed demo text into a throwaway vault.
STORE_CHECKED_ONLY = {
    ("cli/smokes.py", "_seed_local_runtime_smoke_inputs"),
    ("cli/smokes.py", "_run_vnext_smoke_operator_console"),
    ("vnext_evals.py", "_seed_direct_memory"),
}
_WRITERS = {
    "create_memory",
    "update_memory",
    "upsert_memory_by_key",
    "create_continuity_object",
    "update_continuity_object_optional",
}


def _searchable_value(value: ast.AST | None) -> bool:
    if isinstance(value, ast.Constant):
        return value.value in SEARCHABLE_STATUSES
    if isinstance(value, ast.IfExp):
        return _searchable_value(value.body) or _searchable_value(value.orelse)
    return False


def _sets_searchable_status(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            if any(
                isinstance(key, ast.Constant) and key.value == "status" and _searchable_value(value)
                for key, value in zip(node.keys, node.values)
            ):
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "status"
                    and _searchable_value(node.value)
                ):
                    return True
                if isinstance(target, ast.Name) and target.id == "next_status" and _searchable_value(node.value):
                    return True
        elif isinstance(node, ast.keyword) and node.arg == "status" and _searchable_value(node.value):
            return True
    return False


def _calls(func: ast.AST, names: set[str]) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if callee in names:
                return True
    return False


def _functions(relative: str) -> dict[str, ast.AST]:
    tree = ast.parse((PACKAGE / relative).read_text(encoding="utf-8"))
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_enumerated_activation_site_calls_its_check() -> None:
    missing = [site for site, check in ACTIVATION_SITES.items() if not _calls(_functions(site[0])[site[1]], {check})]
    assert missing == []


def test_every_function_that_activates_a_row_is_enumerated() -> None:
    found: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        for name, func in _functions(relative).items():
            if _calls(func, _WRITERS) and _sets_searchable_status(func):
                found.add((relative, name))
    # Guards the guard: the scan sees the doors it is meant to see.
    assert ("memory.py", "admit_memory_candidate") in found
    assert ("vnext_memory_commit.py", "correct") in found
    assert found - set(ACTIVATION_SITES) - STORE_CHECKED_ONLY == set()


# ---------------------------------------------------------------------------
# Owner ruling R2: lifecycle reasons. Retirements (expire, forget, undo and
# the quarantine sweep) always complete and withhold a credential reason;
# activations (unexpire, accept_consolidation) refuse. The token must be
# absent from memories, memory_revisions, event_log and the audit envelope
# alice_explain returns.
# ---------------------------------------------------------------------------


def _committed(store: SQLiteVNextStore, text: str = "Deploys go out on Tuesdays.") -> str:
    result = VNextMemoryCommitService(store).commit(
        identity=None,
        request=memory_commit_request_from_payload({"title": "deploy", "canonical_text": text}, user_id=store.user_id),
    )
    return str(result["memory"]["id"])  # type: ignore[index]


def _nowhere(store: SQLiteVNextStore, memory_id: str, needle: str) -> None:
    assert _tables_holding(store.conn, needle) == []
    assert needle not in str(VNextMemoryCommitService(store).audit(memory_id=memory_id))


@pytest.mark.parametrize("action", ["expire", "forget", "undo"])
def test_r2_a_retirement_completes_and_withholds_a_credential_reason(action: str) -> None:
    store = _store()
    memory_id = _committed(store)
    service = VNextMemoryCommitService(store)
    reason = f"rotated after {PAT} leaked"
    if action == "expire":
        result = service.expire(memory_id, reason=reason)
        assert result["status"] == "expired"
    elif action == "forget":
        result = service.forget(identity=None, memory_id=memory_id, reason=reason)
        assert result["status"] == "forgotten"
    else:
        result = service.undo(identity=None, memory_id=memory_id, reason=reason)
        assert result["status"] == "undone"
    assert result["rationale_withheld"] is True
    _nowhere(store, memory_id, PAT)
    assert RATIONALE_WITHHELD_PLACEHOLDER in _column(store.conn, "SELECT reason FROM memory_revisions")


def test_r2_a_clean_retirement_reason_is_kept_verbatim() -> None:
    # Guards the guard.
    store = _store()
    memory_id = _committed(store)
    result = VNextMemoryCommitService(store).forget(identity=None, memory_id=memory_id, reason="superseded by v2")
    assert result["rationale_withheld"] is False
    assert "superseded by v2" in _column(store.conn, "SELECT reason FROM memory_revisions")


def test_r2_the_quarantine_sweep_withholds_once_before_every_store() -> None:
    store = _store()
    memory_id = _committed(store)
    # The sweep finds rows through the memory.auto_promoted event.
    store.append_event(
        {
            "event_type": "memory.auto_promoted",
            "actor_type": "agent",
            "actor_id": "hermes",
            "target_type": "memory",
            "target_id": memory_id,
            "payload": {},
        }
    )
    envelope = VNextMemoryCommitService(store).quarantine_by_agent_key(
        identity=None, agent_id="hermes", reason=f"key {PAT} was compromised"
    )
    assert envelope["expired"] == [{"memory_id": memory_id, "status": "expired"}]
    assert envelope["rationale_withheld"] is True
    _nowhere(store, memory_id, PAT)
    sweeps = [
        _values(row)
        for row in store.conn.execute("SELECT payload_json FROM event_log WHERE event_type = 'memory.quarantine_sweep'")
    ]
    assert len(sweeps) == 1 and RATIONALE_WITHHELD_PLACEHOLDER in str(sweeps[0])


def test_r2_unexpire_is_an_activation_and_refuses() -> None:
    store = _store()
    memory_id = _committed(store)
    service = VNextMemoryCommitService(store)
    service.expire(memory_id, reason="window closed")
    revisions = _count(store.conn, "memory_revisions")
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        service.unexpire(memory_id, reason=f"reopened, token {PAT}")
    # The row text, as a pre-floor vault holds it, is read too.
    store.conn.execute("UPDATE memories SET canonical_text = ? WHERE id = ?", (f"use {PAT}", memory_id))
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        service.unexpire(memory_id, reason="reopened")
    assert _count(store.conn, "memory_revisions") == revisions
    assert _column(store.conn, "SELECT valid_to FROM memories WHERE id = ?", memory_id)[0] is not None


def test_r2_carried_forward_history_is_withheld_when_the_row_is_rewritten() -> None:
    store = _store()
    memory_id = _committed(store)
    service = VNextMemoryCommitService(store)
    service.expire(memory_id, reason="first window")
    service.unexpire(memory_id, reason="reopened")
    # A validity history entry as a pre-floor vault holds it.
    row = store.get_memory(memory_id)
    assert row is not None
    metadata = dict(row["metadata_json"])  # type: ignore[arg-type]
    validity = dict(metadata["validity"])  # type: ignore[arg-type]
    validity["history"] = [*validity["history"], {"op": "expired", "reason": f"old note {PAT}"}]  # type: ignore[misc]
    store.conn.execute(
        "UPDATE memories SET metadata_json = ? WHERE id = ?",
        (json.dumps({**metadata, "validity": validity}), memory_id),
    )
    result = service.expire(memory_id, reason="second window")
    assert result["rationale_withheld"] is True
    assert PAT not in str(_column(store.conn, "SELECT metadata_json FROM memories WHERE id = ?", memory_id))


# Every method of the commit service that takes a reason or rationale, and
# the helper that must decide what happens to it.
_REASON_INTAKE = {
    "undo": "withhold_credential_text",
    "forget": "withhold_credential_text",
    "expire": "withhold_credential_text",
    "quarantine_by_agent_key": "withhold_credential_text",
    "confirm": "withhold_credential_text",
    "correct": "refuse_credential_activation",
    "unexpire": "refuse_credential_activation",
    "accept_consolidation_candidate": "refuse_credential_activation",
    "_transition_memory": "_withheld_history",
}
# Internal helpers that never see unchecked caller text: their reason is a
# fixed string built in this module, or (_append_revision on commit) the
# request rationale the commit gate has already read. memory_commit_receipt
# reads a reason code to choose a receipt line. It does not store the text.
_FIXED_REASON_HELPERS = {
    "_invalidate_pending_derived_candidates",
    "_refresh_last_confirmed",
    "_append_revision",
    "memory_commit_receipt",
}


def test_r2_every_reason_intake_in_the_commit_service_goes_through_the_helper() -> None:
    functions = _functions("vnext_memory_commit.py")
    takes_reason = {
        name
        for name, func in functions.items()
        if any(arg.arg in {"reason", "rationale"} for arg in func.args.args + func.args.kwonlyargs)  # type: ignore[attr-defined]
    }
    assert takes_reason - _FIXED_REASON_HELPERS == set(_REASON_INTAKE)
    assert [name for name, check in _REASON_INTAKE.items() if not _calls(functions[name], {check})] == []


# ---------------------------------------------------------------------------
# S4.4 round 3, P2 item 11: the reason stored with an approval is read with
# the row. No test pinned it on inline confirm, alice_memory_correct approve
# or correct(), and dropping the reason from each of those checks survived.
# ---------------------------------------------------------------------------


def test_r3_inline_confirm_reads_its_rationale() -> None:
    store = _store()
    service = VNextMemoryCommitService(store)
    pending = service.commit(
        identity=None,
        request=memory_commit_request_from_payload(
            {"title": "deploy day", "canonical_text": "Deploys may move to Wednesdays.", "confidence": 0.8},
            user_id=store.user_id,
        ),
    )
    memory_id = str(pending["memory"]["id"])  # type: ignore[index]
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        service.confirm(
            identity=None,
            confirmation_id=str(pending["confirmation_id"]),
            action="confirm",
            rationale=f"approved, token {PAT}",
        )
    assert _status(store.conn, memory_id) == "needs_review"
    assert _tables_holding(store.conn, PAT) == []


def test_r3_alice_memory_correct_approve_reads_its_reason(tmp_path: Path) -> None:
    context = _sqlite_context(tmp_path)
    with _file_store(tmp_path) as store:
        clean = _seed_candidate(store, "Deploys go out on Tuesdays.")
    with pytest.raises(MCPToolError, match="cannot become active"):
        call_mcp_tool(
            context,
            name="alice_memory_correct",
            arguments={"review_item_id": clean, "action": "approve", "reason": f"approved, token {PAT}"},
        )
    with _file_store(tmp_path) as store:
        assert _status(store.conn, clean) == "candidate"
        assert _tables_holding(store.conn, PAT) == []


def test_r3_correct_reads_its_reason() -> None:
    store = _store()
    memory_id = _committed(store)
    with pytest.raises(VNextMemoryCommitValidationError, match="cannot become active"):
        VNextMemoryCommitService(store).correct(
            identity=None,
            memory_id=memory_id,
            canonical_text="Deploys go out on Thursdays.",
            reason=f"rotated, token {PAT}",
        )
    assert _column(store.conn, "SELECT canonical_text FROM memories WHERE id = ?", memory_id) == [
        "Deploys go out on Tuesdays."
    ]
    assert _tables_holding(store.conn, PAT) == []


# ---------------------------------------------------------------------------
# S4.4 round 3, P2 item 8: the open-loop review-action door.
# ---------------------------------------------------------------------------


class _OpenLoop(_ContinuityObject):
    def __init__(self, *, body: dict[str, object]) -> None:
        super().__init__(body=body)
        self.row["object_type"] = "Blocker"
        self.row["status"] = "stale"


def _review_action(store: _OpenLoop, action: str, note: str | None = None) -> object:
    from alicebot_api.continuity_open_loops import apply_continuity_open_loop_review_action
    from alicebot_api.contracts import ContinuityOpenLoopReviewActionInput

    return apply_continuity_open_loop_review_action(
        store,  # type: ignore[arg-type]
        user_id=uuid4(),
        continuity_object_id=store.object_id,
        request=ContinuityOpenLoopReviewActionInput(action=action, note=note),  # type: ignore[arg-type]
    )


def test_r3_still_blocked_is_an_activation_and_refuses_a_credential_object() -> None:
    from alicebot_api.continuity_open_loops import ContinuityOpenLoopValidationError

    dirty = _OpenLoop(body={"blocking_reason": f"waiting on {PAT} rotation"})
    with pytest.raises(ContinuityOpenLoopValidationError, match="cannot become active"):
        _review_action(dirty, "still_blocked")
    assert dirty.writes == []
    # Guards the guard: a clean object reaches its first write.
    clean = _OpenLoop(body={"blocking_reason": "waiting on the vendor"})
    with pytest.raises(LookupError):
        _review_action(clean, "still_blocked")
    assert [name for name, _kwargs in clean.writes] == ["create_continuity_correction_event"]


@pytest.mark.parametrize("action", ["deferred", "still_blocked", "done"])
def test_r3_a_review_action_note_is_withheld(action: str) -> None:
    store = _OpenLoop(body={"blocking_reason": "waiting on the vendor"})
    with pytest.raises(LookupError):
        _review_action(store, action, note=f"moved, token {PAT}")
    name, kwargs = store.writes[0]
    assert name == "create_continuity_correction_event"
    assert kwargs["reason"] == RATIONALE_WITHHELD_PLACEHOLDER
    assert kwargs["payload"]["note"] == RATIONALE_WITHHELD_PLACEHOLDER  # type: ignore[index]
    assert PAT not in str(kwargs)
