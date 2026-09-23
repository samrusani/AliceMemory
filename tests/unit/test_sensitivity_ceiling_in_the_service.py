"""One sensitivity ceiling, enforced in the commit service.

A keyed agent below a target's sensitivity used to forget, expire, undo,
confirm, or edit that target. Recall hid the row and explain refused, but
the policy engine returned ``allowed_with_filtering`` and the write path
stopped only on ``blocked``. These tests pin the service block on every
route, the commit-time refusal, who may resolve a pending write, and a
second confirm of a committed row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

USER_ID = "00000000-0000-0000-0000-000000000001"
REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "Ceiling secret zeta-914 the keyed agent must not store or change."
DO_NOT_RELABEL = (
    "This was not saved. Do not retry with a lower sensitivity label. "
    "Tell the user. The owner can raise this agent's clearance or store the memory themselves."
)
RESOLVER_REASON = "only_the_author_an_admin_key_or_the_owner_may_confirm_or_reject"


def _context(tmp_path: Path):
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _call(context, name: str, **arguments) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name=name, arguments=arguments)


def _store_read(context, reader):
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        return reader(SQLiteVNextStore(conn, USER_ID))


def _mint_key(context, monkeypatch: pytest.MonkeyPatch, **key_fields) -> None:
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
    from alicebot_api.vnext_agent_keys import create_agent_key

    _record, raw_key = _store_read(context, lambda store: create_agent_key(store, user_id=USER_ID, **key_fields))
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw_key)


def _row(context, memory_id: str) -> dict:
    return _store_read(context, lambda store: store.get_memory(memory_id))


def _stored_blob(context) -> str:
    def read(store) -> str:
        chunks: list[str] = []
        for table in ("memories", "memory_revisions", "event_log"):
            rows = store.conn.execute(f"SELECT * FROM {table}").fetchall()
            chunks.append(json.dumps(rows, default=str))
        return "\n".join(chunks)

    return _store_read(context, read)


def _memory_count(context) -> int:
    def read(store) -> int:
        row = store.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"])

    return _store_read(context, read)


def _revision_count(context) -> int:
    def read(store) -> int:
        row = store.conn.execute("SELECT COUNT(*) AS n FROM memory_revisions").fetchone()
        return int(row["n"])

    return _store_read(context, read)


def _blocked_for(context, *, target_type: str, target_id: str) -> list[dict]:
    events = _store_read(
        context,
        lambda store: store.list_events(target_type=target_type, target_id=target_id),
    )
    return [event for event in events if event["event_type"] == "agent.policy_blocked"]


def _assert_blocked_names_target(context, *, target_type: str, target_id: str) -> None:
    blocked = _blocked_for(context, target_type=target_type, target_id=target_id)
    assert blocked, "expected a policy block event on the target"
    for event in blocked:
        assert event["target_type"] == target_type
        assert event["target_id"] == target_id
        reasons = event["payload_json"]["policy_decision"]["reasons"]
        assert "sensitivity_above_agent_ceiling" in reasons


def _commit_active_confidential(context) -> str:
    """Owner stores a confidential fact, then confirms it, and returns the id."""

    pending = _call(
        context,
        "alice_memory_commit",
        title="Ceiling target",
        canonical_text=SECRET,
        sensitivity="confidential",
        confidence=0.95,
    )
    assert pending["status"] == "confirmation_required", pending
    confirmed = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
    )
    assert confirmed["status"] == "committed", confirmed
    return str(pending["memory"]["id"])


def _raise_sensitivity(context, memory_id: str, sensitivity: str) -> None:
    def write(store) -> None:
        store.conn.execute("UPDATE memories SET sensitivity = ? WHERE id = ?", (sensitivity, memory_id))

    _store_read(context, write)


def _keyed_confidential_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    context = _context(tmp_path)
    memory_id = _commit_active_confidential(context)
    before = dict(_row(context, memory_id))
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    return context, memory_id, before


def _assert_memory_unchanged(context, memory_id: str, before: dict) -> None:
    after = _row(context, memory_id)
    assert after["status"] == before["status"]
    assert after["canonical_text"] == SECRET
    assert after["sensitivity"] == "confidential"
    assert after.get("valid_to") == before.get("valid_to")
    _assert_blocked_names_target(context, target_type="memory", target_id=memory_id)


@pytest.mark.parametrize("action", ["forget", "expire", "undo"])
def test_manage_mutation_refuses_above_the_caller_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context, memory_id, before = _keyed_confidential_target(tmp_path, monkeypatch)
    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(context, "alice_memory_manage", action=action, memory_id=memory_id, reason="should not land")
    _assert_memory_unchanged(context, memory_id, before)


@pytest.mark.parametrize(
    "handler_name",
    ["_handle_alice_vnext_forget_memory", "_handle_alice_vnext_undo_memory"],
)
def test_legacy_mutation_refuses_above_the_caller_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler_name: str
) -> None:
    """The registry hides legacy tools whenever a key is set. The handlers
    those tools dispatch to still go through the service, so this calls
    them directly with the key configured."""

    import alicebot_api.mcp.memories as memories
    from alicebot_api.mcp_tools import MCPToolError

    context, memory_id, before = _keyed_confidential_target(tmp_path, monkeypatch)
    handler = getattr(memories, handler_name)
    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        handler(context, {"memory_id": memory_id, "reason": "should not land"})
    _assert_memory_unchanged(context, memory_id, before)


def _http_confidential_target(monkeypatch: pytest.MonkeyPatch):
    from uuid import uuid4

    from alicebot_api.vnext_agent_keys import create_agent_key
    from tests.unit.test_vnext_main import FakeVNextStore, _install_fake_vnext_store, _seed_active_memory

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text=SECRET)
    store.get_memory(memory_id)["sensitivity"] = "confidential"
    _record, raw_key = create_agent_key(
        store,
        user_id=user_id,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )
    return store, user_id, memory_id, f"Bearer {raw_key}"


def _assert_http_blocked(store, memory_id: str, response) -> None:
    assert response.status_code == 403, response.body
    body = json.loads(response.body)
    assert "sensitivity_above_agent_ceiling" in body["policy_decision"]["reasons"]
    memory = store.get_memory(memory_id)
    assert memory["status"] == "active"
    assert memory["canonical_text"] == SECRET
    assert memory.get("valid_to") is None
    blocked = [
        event
        for event in store.events
        if event.get("event_type") == "agent.policy_blocked" and event.get("target_id") == memory_id
    ]
    assert blocked
    assert all(event.get("target_type") == "memory" for event in blocked)


def test_http_forget_refuses_above_the_caller_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api.routers import vnext_memories as vnext_memories_router

    store, user_id, memory_id, authorization = _http_confidential_target(monkeypatch)
    response = vnext_memories_router.forget_vnext_memory(
        vnext_memories_router.VNextMemoryForgetRequest(user_id=user_id, memory_id=memory_id, reason="no"),
        authorization=authorization,
    )
    _assert_http_blocked(store, memory_id, response)


def test_http_expire_refuses_above_the_caller_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api.routers import vnext_memories as vnext_memories_router

    store, user_id, memory_id, authorization = _http_confidential_target(monkeypatch)
    response = vnext_memories_router.expire_vnext_memory(
        vnext_memories_router.VNextMemoryExpireRequest(user_id=user_id, memory_id=memory_id, reason="no"),
        authorization=authorization,
    )
    _assert_http_blocked(store, memory_id, response)


def test_http_undo_refuses_above_the_caller_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api.routers import vnext_memories as vnext_memories_router

    store, user_id, memory_id, authorization = _http_confidential_target(monkeypatch)
    response = vnext_memories_router.undo_vnext_memory(
        vnext_memories_router.VNextMemoryUndoRequest(user_id=user_id, memory_id=memory_id, reason="no"),
        authorization=authorization,
    )
    _assert_http_blocked(store, memory_id, response)


@pytest.mark.parametrize("action", ["close", "reopen", "snooze", "edit"])
def test_open_loop_mutations_refuse_above_the_caller_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    status = "resolved" if action == "reopen" else "open"

    def create(store):
        fields = {
            "title": "Highly sensitive follow-up",
            "status": status,
            "domain": "personal",
            "sensitivity": "highly_sensitive",
        }
        if status == "resolved":
            fields["resolved_at"] = "2026-09-01T00:00:00Z"
        return store.create_open_loop(fields)

    loop = _store_read(context, create)
    loop_id = str(loop["id"])
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    arguments = {"action": action, "loop_id": loop_id}
    if action == "snooze":
        arguments["due_at"] = "2026-12-01T00:00:00Z"
    if action == "edit":
        arguments["title"] = "Renamed above the ceiling"
    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(context, "alice_open_loops", **arguments)
    after = _store_read(context, lambda store: store.get_open_loop(loop_id))
    assert after["status"] == status
    assert after["title"] == "Highly sensitive follow-up"
    assert after["sensitivity"] == "highly_sensitive"
    _assert_blocked_names_target(context, target_type="open_loop", target_id=loop_id)


def test_commit_route_refuses_confirm_above_the_caller_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller is the author, so the only refusal is the ceiling."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    pending = _call(
        context,
        "alice_memory_commit",
        title="Held inside the ceiling",
        canonical_text=SECRET,
        sensitivity="private",
        confidence=0.7,
    )
    memory_id = str(pending["memory"]["id"])
    _raise_sensitivity(context, memory_id, "confidential")
    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
        )
    assert _row(context, memory_id)["status"] == "needs_review"
    assert _row(context, memory_id)["canonical_text"] == SECRET
    assert _row(context, memory_id)["sensitivity"] == "confidential"
    _assert_blocked_names_target(context, target_type="memory", target_id=memory_id)


def test_keyed_trusted_agent_commit_above_ceiling_is_rejected_with_no_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    refused = _call(
        context,
        "alice_memory_commit",
        title="Do not store",
        canonical_text=SECRET,
        sensitivity="confidential",
        confidence=0.95,
    )
    assert refused["status"] == "rejected", refused
    assert refused["reason"] == "sensitivity_above_agent_ceiling"
    assert DO_NOT_RELABEL in refused["receipt"]
    assert _memory_count(context) == 0
    assert _revision_count(context) == 0
    assert SECRET not in _stored_blob(context)


def test_owner_and_admin_can_still_commit_above_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _context(tmp_path / "owner")
    owner_pending = _call(
        owner,
        "alice_memory_commit",
        title="Owner salary",
        canonical_text="The owner stored a confidential salary note.",
        sensitivity="confidential",
        confidence=0.95,
    )
    assert owner_pending["status"] == "confirmation_required", owner_pending
    assert _row(owner, str(owner_pending["memory"]["id"]))["status"] == "needs_review"

    admin = _context(tmp_path / "admin")
    _mint_key(admin, monkeypatch, agent_id="operator", permission_profile="admin_agent")
    admin_pending = _call(
        admin,
        "alice_memory_commit",
        title="Admin salary",
        canonical_text="An admin key stored a confidential salary note.",
        sensitivity="confidential",
        confidence=0.95,
    )
    assert admin_pending["status"] == "confirmation_required", admin_pending
    assert _row(admin, str(admin_pending["memory"]["id"]))["canonical_text"].startswith("An admin key stored")


def test_author_admin_and_owner_can_confirm_and_a_different_key_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPToolError

    author = _context(tmp_path / "author")
    _mint_key(author, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    pending = _call(
        author,
        "alice_memory_commit",
        title="Within ceiling",
        canonical_text="Hermes noted a private planning preference.",
        sensitivity="private",
        confidence=0.7,
    )
    confirmed = _call(
        author,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
    )
    assert confirmed["status"] == "committed", confirmed
    assert _row(author, str(pending["memory"]["id"]))["status"] == "active"

    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    owned = _context(tmp_path / "owned")
    held = _call(
        owned,
        "alice_memory_commit",
        title="Held by hermes",
        canonical_text="Hermes asked to keep a private release note.",
        sensitivity="private",
        confidence=0.7,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )
    by_owner = _call(
        owned,
        "alice_memory_commit",
        confirmation_id=held["confirmation_id"],
        confirmation_action="confirm",
    )
    assert by_owner["status"] == "committed", by_owner

    admin = _context(tmp_path / "admin")
    held_for_admin = _call(
        admin,
        "alice_memory_commit",
        title="Held for admin",
        canonical_text="Hermes asked to keep another private release note.",
        sensitivity="private",
        confidence=0.7,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )
    _mint_key(admin, monkeypatch, agent_id="operator", permission_profile="admin_agent")
    by_admin = _call(
        admin,
        "alice_memory_commit",
        confirmation_id=held_for_admin["confirmation_id"],
        confirmation_action="confirm",
    )
    assert by_admin["status"] == "committed", by_admin

    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    stranger_db = _context(tmp_path / "stranger")
    held_for_stranger = _call(
        stranger_db,
        "alice_memory_commit",
        title="Not yours",
        canonical_text="Hermes asked to keep a private note a stranger must not resolve.",
        sensitivity="private",
        confidence=0.7,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )
    memory_id = str(held_for_stranger["memory"]["id"])
    _mint_key(stranger_db, monkeypatch, agent_id="other", permission_profile="trusted_local_agent")
    for action in ("confirm", "reject"):
        with pytest.raises(MCPToolError, match=RESOLVER_REASON):
            _call(
                stranger_db,
                "alice_memory_commit",
                confirmation_id=held_for_stranger["confirmation_id"],
                confirmation_action=action,
            )
        assert _row(stranger_db, memory_id)["status"] == "needs_review"
        assert _row(stranger_db, memory_id)["canonical_text"].startswith("Hermes asked to keep a private note")


def test_second_confirm_on_the_commit_route_refuses_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    pending = _call(
        context,
        "alice_memory_commit",
        title="Confirm once",
        canonical_text="Hermes noted a private preference that is confirmed once.",
        sensitivity="private",
        confidence=0.7,
    )
    memory_id = str(pending["memory"]["id"])
    first = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
    )
    assert first["status"] == "committed", first
    before = _row(context, memory_id)
    revisions_before = _store_read(context, lambda store: store.list_revisions(memory_id))
    events_before = _store_read(context, lambda store: store.list_events(target_type="memory", target_id=memory_id))

    with pytest.raises(MCPToolError, match="confirmation is not pending"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
        )

    after = _row(context, memory_id)
    assert after["status"] == before["status"]
    assert after["canonical_text"] == before["canonical_text"]
    assert after.get("last_confirmed_at") == before.get("last_confirmed_at")
    assert _store_read(context, lambda store: store.list_revisions(memory_id)) == revisions_before
    assert _store_read(context, lambda store: store.list_events(target_type="memory", target_id=memory_id)) == events_before


def _credential_assignment() -> str:
    """A fake credential in the detector's assignment shape, built at runtime.

    The source must not contain the assembled value. The scanner reads the
    file as text and also decodes base64.
    """

    name = "api" + "_key"
    value = "zz" + "99" + "qq"
    return f"The staging {name}={value} is tucked in the note."


def _events(context) -> list[dict]:
    return _store_read(context, lambda store: store.list_events())


@pytest.mark.parametrize("action", ["close", "reopen", "snooze", "edit"])
def test_http_open_loop_review_refuses_above_the_caller_ceiling(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from alicebot_api.routers import vnext_projects as vnext_projects_router

    store, user_id, _memory_id, authorization = _http_confidential_target(monkeypatch)
    status = "resolved" if action == "reopen" else "open"
    fields = {
        "title": "Highly sensitive follow-up",
        "status": status,
        "domain": "personal",
        "sensitivity": "highly_sensitive",
    }
    if status == "resolved":
        fields["resolved_at"] = "2026-09-01T00:00:00Z"
    loop = store.create_open_loop(fields)
    loop_id = str(loop["id"])
    request_fields = {"user_id": user_id, "action": action}
    if action == "snooze":
        request_fields["due_at"] = "2026-12-01T00:00:00Z"
    if action == "edit":
        request_fields["title"] = "Renamed above the ceiling"
    response = vnext_projects_router.review_vnext_open_loop(
        loop_id,
        vnext_projects_router.VNextOpenLoopReviewRequest(**request_fields),
        authorization=authorization,
    )
    assert response.status_code == 403, response.body
    body = json.loads(response.body)
    assert "sensitivity_above_agent_ceiling" in body["policy_decision"]["reasons"]
    after = store.get_open_loop(loop_id)
    assert after["status"] == status
    assert after["title"] == "Highly sensitive follow-up"
    assert after["sensitivity"] == "highly_sensitive"
    blocked = [
        event
        for event in store.events
        if event.get("event_type") == "agent.policy_blocked" and event.get("target_id") == loop_id
    ]
    assert blocked
    assert all(event.get("target_type") == "open_loop" for event in blocked)


def test_hermes_confidential_secret_keeps_the_credential_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret above the ceiling stays a credential refusal.

    The receipt must not tell the owner to store it themselves.
    """

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    text = _credential_assignment()
    refused = _call(
        context,
        "alice_memory_commit",
        title="Do not store",
        canonical_text=text,
        sensitivity="confidential",
        confidence=0.95,
    )
    assert refused["status"] == "rejected", refused
    assert refused["reason"] == "unsafe_secret_storage"
    assert "unsafe_secret_storage" in refused["reasons"]
    assert refused["receipt"] == "rejected."
    assert "store the memory themselves" not in refused["receipt"]
    assert _memory_count(context) == 0
    assert text not in _stored_blob(context)


def test_policy_blocked_confidential_commit_keeps_agent_policy_blocked(tmp_path: Path) -> None:
    """A caller the policy engine already stopped keeps that reason.

    Confidential sensitivity must not replace it with the ceiling receipt.
    """

    context = _context(tmp_path)
    refused = _call(
        context,
        "alice_memory_commit",
        title="Do not store",
        canonical_text=SECRET,
        sensitivity="confidential",
        confidence=0.95,
        agent_id="hermes",
        permission_profile="read_only_agent",
    )
    assert refused["status"] == "rejected", refused
    assert refused["reason"] == "agent_policy_blocked"
    assert "agent_policy_blocked" in refused["reasons"]
    assert refused["receipt"] == "rejected."
    assert "store the memory themselves" not in refused["receipt"]
    assert _memory_count(context) == 0


def test_ceiling_refusal_writes_agent_memory_commit_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    refused = _call(
        context,
        "alice_memory_commit",
        title="Do not store",
        canonical_text=SECRET,
        sensitivity="confidential",
        confidence=0.95,
    )
    assert refused["status"] == "rejected", refused
    rejected = [event for event in _events(context) if event["event_type"] == "agent.memory_commit_rejected"]
    assert rejected, "expected agent.memory_commit_rejected"
    assert rejected[0]["payload_json"]["reason"] == "sensitivity_above_agent_ceiling"
    assert _memory_count(context) == 0


def test_ceiling_refusal_writes_filtered_policy_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base decision is filtering, so the audit is policy_filtered.

    A blocked decision would write agent.policy_blocked instead.
    """

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    refused = _call(
        context,
        "alice_memory_commit",
        title="Do not store",
        canonical_text=SECRET,
        sensitivity="confidential",
        confidence=0.95,
    )
    assert refused["reason"] == "sensitivity_above_agent_ceiling"
    types = {event["event_type"] for event in _events(context)}
    assert "policy.decision" in types
    assert "agent.policy_filtered" in types
    assert "agent.policy_blocked" not in types


def test_keyless_declared_admin_skips_the_commit_ceiling(tmp_path: Path) -> None:
    """A keyless call that declares admin_agent is keyless owner mode.

    It is not held to the commit ceiling. A keyless server does not verify
    the declared profile.
    """

    context = _context(tmp_path)
    pending = _call(
        context,
        "alice_memory_commit",
        title="Declared admin",
        canonical_text="A keyless declared admin stored a confidential note.",
        sensitivity="confidential",
        confidence=0.95,
        agent_id="operator",
        permission_profile="admin_agent",
    )
    assert pending["status"] == "confirmation_required", pending
    assert _row(context, str(pending["memory"]["id"]))["status"] == "needs_review"


def test_keyless_declared_admin_cannot_resolve_another_agents_pending_write(tmp_path: Path) -> None:
    """Declaring admin_agent without a key is not an admin resolver."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    held = _call(
        context,
        "alice_memory_commit",
        title="Held by hermes",
        canonical_text="Hermes asked to keep a private note a declared admin must not resolve.",
        sensitivity="private",
        confidence=0.7,
        agent_id="hermes",
        permission_profile="trusted_local_agent",
    )
    memory_id = str(held["memory"]["id"])
    with pytest.raises(MCPToolError, match=RESOLVER_REASON):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=held["confirmation_id"],
            confirmation_action="confirm",
            agent_id="operator",
            permission_profile="admin_agent",
        )
    assert _row(context, memory_id)["status"] == "needs_review"


def test_unauthorized_confirm_of_a_finished_row_is_an_author_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorization runs before the pending check.

    A stranger confirming a finished row hears the author reason and the
    refusal is audited. The error does not say the row is not pending.
    """

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPToolError

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    pending = _call(
        context,
        "alice_memory_commit",
        title="Confirm once",
        canonical_text="Hermes noted a private preference that is confirmed once.",
        sensitivity="private",
        confidence=0.7,
    )
    memory_id = str(pending["memory"]["id"])
    first = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
    )
    assert first["status"] == "committed", first
    monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    _mint_key(context, monkeypatch, agent_id="other", permission_profile="trusted_local_agent")
    with pytest.raises(MCPToolError, match=RESOLVER_REASON) as raised:
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
        )
    assert "confirmation is not pending" not in str(raised.value)
    assert _row(context, memory_id)["status"] == "active"
    blocked = _blocked_for(context, target_type="memory", target_id=memory_id)
    assert blocked
    assert RESOLVER_REASON in blocked[-1]["payload_json"]["policy_decision"]["reasons"]


def test_authorized_reject_keeps_the_credential_placeholder(tmp_path: Path) -> None:
    """An authorized reject stores S4.4's placeholder, not the credential text.

    The author passes authorization and the ceiling. The credential check
    still replaces the rationale and says so.
    """

    from alicebot_api.credential_floor import RATIONALE_WITHHELD_PLACEHOLDER
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_control import AgentIdentity
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, memory_commit_request_from_payload

    context = _context(tmp_path)
    author = AgentIdentity(
        agent_id="hermes",
        agent_type="personal_assistant",
        permission_profile="trusted_local_agent",
    )
    credential = _credential_assignment()
    rationale = f"stop, {credential}"

    def reject(store):
        service = VNextMemoryCommitService(store)
        pending = service.commit(
            identity=author,
            request=memory_commit_request_from_payload(
                {
                    "title": "Pending note",
                    "canonical_text": "Deploys stay on Tuesdays.",
                    "domain": "personal",
                    "sensitivity": "private",
                    "confidence": 0.7,
                },
                user_id=USER_ID,
            ),
        )
        assert pending["status"] == "confirmation_required", pending
        rejected = service.confirm(
            identity=author,
            confirmation_id=str(pending["confirmation_id"]),
            action="reject",
            rationale=rationale,
        )
        tables: dict[str, str] = {}
        for table in ("memories", "memory_revisions", "event_log"):
            rows = store.conn.execute(f"SELECT * FROM {table}").fetchall()
            tables[table] = json.dumps(rows, default=str)
        return rejected, tables

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        rejected, tables = reject(SQLiteVNextStore(conn, USER_ID))

    assert rejected.get("status") == "rejected"
    assert rejected.get("rationale_withheld") is True
    assert RATIONALE_WITHHELD_PLACEHOLDER in tables["memory_revisions"]
    for table, blob in tables.items():
        assert credential not in blob, table
        assert rationale not in blob, table


def test_agentic_memory_commit_smoke_keeps_private_health_and_refuses_confidential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    from contextlib import contextmanager
    from uuid import UUID

    import alicebot_api.cli.smokes as smokes
    from alicebot_api.cli.models import CLIContext
    from alicebot_api.config import Settings
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    context = _context(tmp_path)

    @contextmanager
    def sqlite_store(_ctx):
        with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
            yield SQLiteVNextStore(conn, USER_ID)

    monkeypatch.setattr(smokes, "_vnext_store_context", sqlite_store)
    ctx = CLIContext(
        settings=Settings(database_url=context.database_url),
        database_url=context.database_url,
        user_id=UUID(USER_ID),
    )
    try:
        output = smokes._run_vnext_smoke_agentic_memory_commit(ctx, argparse.Namespace())
    except RuntimeError as exc:
        pytest.fail(str(exc))
    payload = json.loads(output)
    assert payload["status"] == "passed"
    assert payload["gates"]["sensitive_memory_requires_confirmation"] is True
    assert payload["gates"]["inline_confirmation_commits"] is True
    assert payload["gates"].get("confidential_hermes_commit_refused") is True


def test_refusal_wording_is_in_the_tool_and_both_skill_packs() -> None:
    from alicebot_api.mcp.registry import list_mcp_tools

    commit = next(tool for tool in list_mcp_tools() if tool["name"] == "alice_memory_commit")
    assert DO_NOT_RELABEL in commit["description"]
    for relative in (
        "agent-skills/hermes/alice-memory/SKILL.md",
        "agent-skills/openclaw/alice-project-memory/SKILL.md",
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "This was not saved." in text
        assert "Do not retry with a lower sensitivity label." in text
        assert "Tell the user." in text
        assert "The owner can raise this agent's clearance or store the memory themselves." in text
        assert "On a keyless install that limit is not protection" in text
