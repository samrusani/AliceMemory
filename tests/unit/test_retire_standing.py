"""Standing for lifecycle changes to waiting writes and lessons."""

import asyncio
import json

import pytest
from fastapi import FastAPI

from alicebot_api.config import Settings
from alicebot_api.routers import vnext_memories

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import LESSON, Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    return Vault(tmp_path / "vault.db")


def _call(vault, monkeypatch, token, tool, arguments):
    monkeypatch.setenv("ALICE_AGENT_API_KEY", token)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    return wire(context, tool, arguments)


def _commit(vault, monkeypatch, token, **fields):
    failed, result = _call(vault, monkeypatch, token, "alice_memory_commit", {
        "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], **fields,
    })
    assert not failed, result
    return result



def _http(vault, monkeypatch, token, action, memory_id):
    monkeypatch.setattr(vnext_memories, "get_settings", lambda: Settings(database_url=sqlite_url_for_path(vault.path)))
    monkeypatch.setattr(vnext_memories, "user_connection", lambda _url, user: sqlite_user_connection(vault.path, user))
    monkeypatch.setattr(vnext_memories, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, vault.user))
    app = FastAPI()
    app.include_router(vnext_memories.memory_router)

    async def request():
        messages = []
        sent = False
        body = json.dumps({"user_id": str(vault.user), "memory_id": memory_id, "reason": "Retire it"}).encode()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        path = "/v0/vnext/memories/" + action
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
                 "headers": [(b"authorization", ("Bearer " + token).encode()), (b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
        await app(scope, receive, send)
        return next(item["status"] for item in messages if item["type"] == "http.response.start")

    return asyncio.run(request())


def _matrix(vault, monkeypatch, lesson):
    profiles = ("read_only_agent", "memory_proposal_agent", "project_scoped_agent", "trusted_local_agent", "admin_agent")
    keys = {profile: vault.key("matrix_" + profile, profile, None if profile == "admin_agent" else "alpha")
            for profile in profiles}
    author = vault.key("matrix_author", "project_scoped_agent", "alpha")
    actions = ("forget", "expire", "undo", "unexpire") if lesson else ("forget", "expire", "undo")
    for action in actions:
        for profile in profiles:
            kinds = ("lesson", "waiting") if lesson and action != "unexpire" else ("lesson",) if lesson else ("active", "pending", "proposal")
            for kind in kinds:
                if lesson:
                    writer = keys["memory_proposal_agent"] if kind == "waiting" else keys["admin_agent"]
                    created = _commit(vault, monkeypatch, writer, lesson=LESSON)
                else:
                    writer = author if kind == "pending" else keys["memory_proposal_agent"] if kind == "proposal" else keys["admin_agent"]
                    created = _commit(vault, monkeypatch, writer, title="A procedure", canonical_text="An ordinary procedure.",
                                      confidence=0.7 if kind == "pending" else 0.95)
                mid = str(created["memory"]["id"])
                if action == "unexpire":
                    failed, answer = _call(vault, monkeypatch, keys["admin_agent"], "alice_memory_manage", {
                        "action": "expire", "memory_id": mid, "reason": "Retire it",
                    })
                    assert not failed, answer
                allowed = profile == "admin_agent" or (kind == "active" and profile in profiles[2:4])
                status = _http(vault, monkeypatch, keys[profile], action, mid)
                assert status == (200 if allowed else 403)
                # HTTP succeeded on allowed cases, so use a fresh row for MCP.
                if allowed:
                    if lesson:
                        created = _commit(vault, monkeypatch, writer, lesson=LESSON)
                    else:
                        created = _commit(vault, monkeypatch, writer, title="A procedure", canonical_text="An ordinary procedure.",
                                          confidence=0.7 if kind == "pending" else 0.95)
                    mid = str(created["memory"]["id"])
                    if action == "unexpire":
                        failed, answer = _call(vault, monkeypatch, keys["admin_agent"], "alice_memory_manage", {
                            "action": "expire", "memory_id": mid, "reason": "Retire it",
                        })
                        assert not failed, answer
                failed, answer = _call(vault, monkeypatch, keys[profile], "alice_memory_manage", {
                    "action": action, "memory_id": mid, "reason": "Retire it",
                })
                assert not failed if allowed else failed and answer["error"]["code"] == "not_permitted"


def _owner(vault, monkeypatch, lesson):
    owner_vault = Vault(vault.path.parent / ("owner-lesson.db" if lesson else "owner-pending.db"))
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(owner_vault.path), user_id=owner_vault.user)
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    actions = ("forget", "expire", "undo", "unexpire") if lesson else ("forget", "expire", "undo")
    for action in actions:
        kinds = (("lesson",) if action == "unexpire" else ("lesson", "waiting_lesson")) if lesson else ("pending", "proposal")
        for kind in kinds:
            if lesson:
                fields = {"lesson": LESSON}
                if kind == "waiting_lesson":
                    fields.update(agent_id="proposal", permission_profile="memory_proposal_agent")
            else:
                fields = {"title": "A procedure", "canonical_text": "An ordinary procedure.", "confidence": 0.7,
                          "agent_id": "author", "permission_profile": "project_scoped_agent"}
                if kind == "proposal":
                    fields.update(agent_id="proposal", permission_profile="memory_proposal_agent")
            failed, created = wire(context, "alice_memory_commit", {
                "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], **fields,
            })
            assert not failed, created
            mid = str(created["memory"]["id"])
            if action == "unexpire":
                failed, result = wire(context, "alice_memory_manage", {"action": "expire", "memory_id": mid, "reason": "Retire it"})
                assert not failed, result
            failed, result = wire(context, "alice_memory_manage", {"action": action, "memory_id": mid, "reason": "Retire it"})
            assert not failed, result


def test_pending_retirement_requires_the_existing_decision_maker(vault, monkeypatch):
    """Guard: remove the pending retirement decision in _write_policy_decision."""
    _matrix(vault, monkeypatch, False)
    _owner(vault, monkeypatch, False)
    author = vault.key("author", "project_scoped_agent", "alpha")
    stranger = vault.key("stranger", "project_scoped_agent", "alpha")
    proposal = vault.key("proposal", "memory_proposal_agent", "alpha")
    for action in ("forget", "expire", "undo"):
        pending = _commit(vault, monkeypatch, author, title="A pending write", canonical_text="An ordinary procedure.", confidence=0.7)
        assert pending["status"] == "confirmation_required"
        mid = str(pending["memory"]["id"])
        with sqlite_user_connection(vault.path, vault.user) as conn:
            store = SQLiteVNextStore(conn, vault.user)
            row_before = store.get_memory(mid)
            revisions_before = store.list_revisions(mid)
            events_before = store.list_events(target_type="memory", target_id=mid)
            queue_before = store.list_pending_inline_confirmations(limit=200)
        failed, answer = _call(vault, monkeypatch, stranger, "alice_memory_manage", {
            "action": action, "memory_id": mid, "reason": "Retire it",
        })
        assert failed and answer["error"]["code"] == "not_permitted"
        with sqlite_user_connection(vault.path, vault.user) as conn:
            store = SQLiteVNextStore(conn, vault.user)
            assert store.get_memory(mid) == row_before
            assert store.list_revisions(mid) == revisions_before
            events_after = store.list_events(target_type="memory", target_id=mid)
            assert store.list_pending_inline_confirmations(limit=200) == queue_before
        assert {event["event_type"] for event in events_after} - {event["event_type"] for event in events_before} == {
            "policy.decision", "agent.policy_blocked",
        }
        failed, result = _call(vault, monkeypatch, author, "alice_memory_manage", {
            "action": "confirm", "memory_id": pending["memory"]["id"], "confirmation_id": pending["confirmation_id"],
        })
        assert not failed and result["memory"]["status"] == "active"
        proposed = _commit(vault, monkeypatch, proposal, title="A proposed write", canonical_text="An ordinary procedure.")
        assert proposed["status"] == "review_required"
        failed, answer = _call(vault, monkeypatch, stranger, "alice_memory_manage", {
            "action": action, "memory_id": proposed["memory"]["id"], "reason": "Retire it",
        })
        assert failed and answer["error"]["code"] == "not_permitted"
        owned = _commit(vault, monkeypatch, author, title="Another pending write", canonical_text="An ordinary procedure.", confidence=0.7)
        failed, result = _call(vault, monkeypatch, author, "alice_memory_manage", {
            "action": action, "memory_id": owned["memory"]["id"], "reason": "Retire it",
        })
        assert not failed, result


def test_lesson_retirement_requires_the_existing_publisher(vault, monkeypatch):
    """Guard: remove the lesson retirement decision in _write_policy_decision."""
    _matrix(vault, monkeypatch, True)
    _owner(vault, monkeypatch, True)
    admin = vault.key("publisher", "admin_agent")
    stranger = vault.key("stranger", "project_scoped_agent", "alpha")
    for action in ("forget", "expire", "undo", "unexpire"):
        lesson = _commit(vault, monkeypatch, admin, lesson=LESSON)
        mid = lesson["memory"]["id"]
        if action == "unexpire":
            failed, result = _call(vault, monkeypatch, admin, "alice_memory_manage", {
                "action": "expire", "memory_id": mid, "reason": "Retire it",
            })
            assert not failed, result
        failed, answer = _call(vault, monkeypatch, stranger, "alice_memory_manage", {
            "action": action, "memory_id": mid, "reason": "Retire it",
        })
        assert failed and answer["error"]["code"] == "not_permitted"

    for action in ("forget", "expire", "undo", "unexpire"):
        term = "lessonretire" + action + "example"
        lesson = _commit(vault, monkeypatch, admin, lesson={**LESSON, "applies_when": term})
        mid = str(lesson["memory"]["id"])
        if action == "unexpire":
            failed, result = _call(vault, monkeypatch, admin, "alice_memory_manage", {
                "action": "expire", "memory_id": mid, "reason": "Retire it",
            })
            assert not failed, result
        failed, result = _call(vault, monkeypatch, stranger, "alice_memory_manage", {
            "action": action, "memory_id": mid, "reason": "Retire it",
        })
        assert failed and result["error"]["code"] == "not_permitted"
        failed, result = _call(vault, monkeypatch, stranger, "alice_lessons", {"task": term})
        assert not failed and (mid in {str(row["id"]) for row in result["lessons"]}) == (action != "unexpire")
        failed, result = _call(vault, monkeypatch, admin, "alice_memory_manage", {
            "action": action, "memory_id": mid, "reason": "Retire it",
        })
        assert not failed, result
        failed, result = _call(vault, monkeypatch, stranger, "alice_lessons", {"task": term})
        assert not failed and (mid in {str(row["id"]) for row in result["lessons"]}) == (action == "unexpire")

    for action in ("redact", "accept_consolidation"):
        ordinary = _commit(vault, monkeypatch, admin, title="An ordinary row", canonical_text="An ordinary procedure.")
        failed, answer = _call(vault, monkeypatch, stranger, "alice_memory_manage", {
            "action": action, "memory_id": ordinary["memory"]["id"], "reason": "A reason",
        })
        assert failed and answer["error"]["code"] == "not_permitted"

    from uuid import uuid4

    hidden_key = vault.key("outside", "project_scoped_agent", "beta")
    bound_admin = vault.key("bound_admin", "admin_agent", "alpha")
    for action in ("forget", "expire", "undo", "unexpire"):
        lesson = _commit(vault, monkeypatch, admin, lesson=LESSON)
        mid = str(lesson["memory"]["id"])
        if action == "unexpire":
            failed, result = _call(vault, monkeypatch, admin, "alice_memory_manage", {
                "action": "expire", "memory_id": mid, "reason": "Retire it",
            })
            assert not failed, result
        hidden = _call(vault, monkeypatch, hidden_key, "alice_memory_manage", {
            "action": action, "memory_id": mid, "reason": "Retire it",
        })
        absent = _call(vault, monkeypatch, hidden_key, "alice_memory_manage", {
            "action": action, "memory_id": str(uuid4()), "reason": "Retire it",
        })
        assert hidden == absent and hidden[0] and hidden[1]["error"]["code"] == "not_found"
        failed, result = _call(vault, monkeypatch, bound_admin, "alice_memory_manage", {
            "action": action, "memory_id": mid, "reason": "Retire it",
        })
        assert not failed, result


def test_undo_does_not_apply_retirement_standing_to_the_successor(vault, monkeypatch):
    """Guard: remove retirement_target=False from the successor's preliminary check."""
    admin = vault.key("publisher", "admin_agent")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    lesson = _commit(vault, monkeypatch, admin, lesson=LESSON)
    ordinary = _commit(vault, monkeypatch, writer, title="A procedure", canonical_text="An ordinary procedure.")
    failed, result = _call(vault, monkeypatch, writer, "alice_memory_manage", {
        "action": "undo", "memory_id": ordinary["memory"]["id"], "superseded_by": lesson["memory"]["id"],
    })
    assert not failed and result["memory"]["status"] == "superseded", result
