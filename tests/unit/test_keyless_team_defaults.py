"""Keyless identities and lessons through the SQLite tool boundary."""

from uuid import uuid4

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.support.keyless_team_contract import (
    FACT, LESSON, PROJECT, TRUSTED, check_implicit_local_profile, check_lesson_standing,
    check_marker_does_not_restore, check_proposal_identity_contract, mcp_door, profile_claims,
)
from tests.unit.test_lessons_read import Vault


@pytest.fixture
def local_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    for name in ("ALICE_AGENT_API_KEY", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    vault = Vault(tmp_path / "vault.db")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    return vault, context, mcp_door(context)


@pytest.mark.parametrize("shape", ["flat", "agent_identity"])
@pytest.mark.parametrize("blank", ["omitted", None, "", "   "])
def test_mcp_default_uses_the_count_and_records_the_declared_writer(local_tools, shape, blank):
    """Guard edit: use the original read-only fallback for an ordinary name."""
    check_implicit_local_profile(local_tools[2], *profile_claims(shape, blank, agent_type="coding_agent"))


@pytest.mark.parametrize("claims", [TRUSTED, {**PROJECT, "project_scope": ["alpha"]}])
def test_mcp_keyless_writers_publish_and_retire(local_tools, claims):
    """Guard edit: omit keyless publishing standing from the shared predicate."""
    check_lesson_standing(local_tools[2], claims)


def test_the_legacy_proposal_keeps_its_identity_and_shape_requirements(local_tools):
    """Guard edit: apply the read-only fallback at the legacy proposal boundary."""
    check_proposal_identity_contract(local_tools[2], missing="invalid_request", bad_lesson="invalid_request")


def test_only_counted_identity_holds_keyless_standing(local_tools):
    """Guard edit: stop passing the successful count to the identity constructor."""
    from alicebot_api.mcp.policy import _agent_identity_from_arguments
    from alicebot_api.vnext_memory_commit import caller_may_publish_lesson

    _, context, _ = local_tools
    identity = _agent_identity_from_arguments(context, TRUSTED)
    assert identity is not None and caller_may_publish_lesson(identity)
    check_marker_does_not_restore(identity)


def test_stored_keyless_lesson_mark_uses_publishing_standing(local_tools):
    """Guard edit: stop writing the stored keyless publishing mark."""
    from alicebot_api.vnext_agent_control import AgentIdentity
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload

    vault, _, call = local_tools
    error, published = call("commit", {"lesson": LESSON, **TRUSTED})
    assert error is None and published["status"] == "committed"
    mark = "keyless_lesson_publishing_standing"
    assert published["memory"]["metadata_json"]["agentic_memory"][mark] is True
    with sqlite_user_connection(vault.path, vault.user) as conn:
        result = VNextMemoryCommitService(SQLiteVNextStore(conn, vault.user)).commit(
            identity=AgentIdentity(**TRUSTED),
            request=memory_commit_request_from_payload({"lesson": LESSON}, user_id=vault.user),
        )
    assert result["status"] == "review_required"
    assert mark not in result["memory"]["metadata_json"]["agentic_memory"]


def test_keyless_publication_stays_visible_across_key_creation_and_revocation(local_tools, monkeypatch):
    """Guard edit: omit the write's recorded keyless publishing standing from lookup."""
    vault, _, call = local_tools
    args = {"lesson": LESSON, **TRUSTED}
    _, published = call("commit", args)
    assert published["status"] == "committed"
    memory_id = published["memory"]["id"]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        record, key = create_agent_key(
            SQLiteVNextStore(conn, vault.user), user_id=vault.user, **TRUSTED,
        )
    assert call("lessons", {"task": "amber fixture"})[0] == "not_permitted"
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    _, lookup = call("lessons", {"task": "amber fixture"})
    [entry] = lookup["lessons"]
    assert entry["id"] == memory_id
    assert entry["writer"] == {"id": TRUSTED["agent_id"], "established": "declared_on_keyless_install"}
    _, waiting = call("commit", {**args, "idempotency_key": "waiting-amber"})
    assert waiting["status"] == "review_required"
    assert waiting["policy_decision"]["reason"] == "lesson_requires_approval"
    with sqlite_user_connection(vault.path, vault.user) as conn:
        SQLiteVNextStore(conn, vault.user).revoke_agent_api_key(key_id=str(record["id"]))
    monkeypatch.delenv("ALICE_AGENT_API_KEY")
    _, replay = call("commit", {**args, "idempotency_key": "waiting-amber"})
    assert replay["status"] == waiting["status"]
    assert replay["memory"]["id"] == waiting["memory"]["id"]
    _, after = call("commit", args)
    assert after["status"] == "committed"


def test_keyless_publisher_sees_other_writers_waiting_lessons(local_tools):
    """Guard edit: omit keyless publishing standing from the shared predicate."""
    from alicebot_api.mcp.policy import _agent_identity_from_arguments
    from alicebot_api.vnext_label_guard import guard_for_caller

    vault, context, call = local_tools
    _, queued = call("commit", {
        "lesson": LESSON, "sensitivity": "internal", "project_scope": ["alpha"], "domain": "project",
        "agent_id": "proposal-writer", "permission_profile": "memory_proposal_agent",
    })
    assert queued["status"] == "review_required"
    memory_id = queued["memory"]["id"]
    for claims, visible in (
        ({}, True), (TRUSTED, True), ({**PROJECT, "project_scope": ["alpha"]}, True),
        ({"agent_id": "reader", "permission_profile": "memory_proposal_agent"}, False),
        ({"agent_id": "reader", "permission_profile": "read_only_agent"}, False),
    ):
        error, review = call("review", {**claims, "status": "pending_review"})
        assert error is None, review
        assert {str(item["id"]) for item in review["items"]} == ({memory_id} if visible else set())
        identity = _agent_identity_from_arguments(context, claims)
        with sqlite_user_connection(vault.path, vault.user) as conn:
            guard = guard_for_caller(SQLiteVNextStore(conn, vault.user), identity, action="review_items.lookup")
            assert guard.readable_status_counts("memory").get("candidate", 0) == int(visible)
        error, lookup = call("lessons", {**claims, "task": "amber fixture"})
        assert error is None and "yours" not in lookup and lookup["count"] == 0, lookup


def test_sqlite_key_count_distinguishes_no_count_from_zero(tmp_path):
    from alicebot_api.mcp.policy import _vault_has_active_key

    path = tmp_path / "vault.db"
    assert _vault_has_active_key(str(path)) is None
    assert not path.exists()
    path.touch()
    assert _vault_has_active_key(str(path)) is None
    Vault(path)  # Bootstrap the key table with no rows.
    assert _vault_has_active_key(str(path)) is False


@pytest.fixture(params=["missing-file", "missing-key-table"])
def uncounted_sqlite_call(tmp_path, monkeypatch, request):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    for name in ("ALICE_AGENT_API_KEY", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "uncounted.db"
    if request.param == "missing-key-table":
        path.touch()
    return mcp_door(MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=uuid4()))


def test_first_mcp_fact_without_a_key_count_keeps_the_read_only_default(uncounted_sqlite_call):
    error, receipt = uncounted_sqlite_call("commit", {
        **FACT, "agent_id": TRUSTED["agent_id"], "domain": "project", "sensitivity": "internal",
    })
    assert error is None and receipt["status"] == "rejected", receipt
    assert "read_only_agent_cannot_write" in receipt["policy_decision"]["reasons"]


def test_first_mcp_lesson_without_a_key_count_waits(uncounted_sqlite_call):
    error, receipt = uncounted_sqlite_call("commit", {
        **TRUSTED, "lesson": LESSON, "domain": "project", "sensitivity": "internal",
    })
    assert error is None and receipt["status"] == "review_required", receipt
    assert receipt["policy_decision"]["reason"] == "lesson_requires_approval"


def test_first_mcp_call_without_a_key_count_refuses_a_null_profile(uncounted_sqlite_call):
    error, receipt = uncounted_sqlite_call("commit", {
        **FACT, "agent_id": TRUSTED["agent_id"], "permission_profile": None,
        "domain": "project", "sensitivity": "internal",
    })
    assert error == "invalid_request", receipt
