"""Retirement standing through PostgreSQL MCP and HTTP doors."""

import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi import FastAPI

from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.routers import vnext_memories
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_mcp_server import seed_user
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import LESSON

PROFILES = ("read_only_agent", "memory_proposal_agent", "project_scoped_agent", "trusted_local_agent", "admin_agent")


@pytest.fixture
def vault(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    user = seed_user(url, email=f"{uuid4()}@example.invalid")
    keys = {}
    with user_connection(url, user) as conn:
        store = PostgresVNextStore(conn)
        for profile in (*PROFILES, "author", "bound_admin", "hidden"):
            _, keys[profile] = create_agent_key(
                store, user_id=user, agent_id=profile,
                permission_profile=("project_scoped_agent" if profile in {"author", "hidden"} else "admin_agent" if profile == "bound_admin" else profile),
                project_scope=None if profile == "admin_agent" else "beta" if profile == "hidden" else "alpha",
            )
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setattr(vnext_memories, "get_settings", lambda: Settings(database_url=url))
    app = FastAPI()
    app.include_router(vnext_memories.memory_router)
    return user, keys, MCPRuntimeContext(database_url=url, user_id=user), app


def _tool(vault, monkeypatch, profile, tool, arguments):
    _, keys, context, _ = vault
    monkeypatch.setenv("ALICE_AGENT_API_KEY", keys[profile])
    return wire(context, tool, arguments)


def _http(vault, profile, action, memory_id):
    user, keys, _, app = vault

    async def request():
        messages = []
        sent = False
        body = json.dumps({"user_id": str(user), "memory_id": memory_id, "reason": "Retire it"}).encode()

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
                 "headers": [(b"authorization", ("Bearer " + keys[profile]).encode()), (b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
        await app(scope, receive, send)
        return (next(item["status"] for item in messages if item["type"] == "http.response.start"),
                json.loads(b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")))

    return asyncio.run(request())


def _commit(vault, monkeypatch, profile, fields):
    failed, result = _tool(vault, monkeypatch, profile, "alice_memory_commit", {
        "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], **fields,
    })
    assert not failed, result
    return result


def _exercise(vault, monkeypatch, kind, action, profile, door):
    if kind in {"active", "pending", "proposal"}:
        fields = {"title": "A procedure", "canonical_text": "An ordinary procedure.",
                  "confidence": 0.7 if kind == "pending" else 0.95}
        writer = "author" if kind == "pending" else "memory_proposal_agent" if kind == "proposal" else "admin_agent"
    else:
        fields = {"lesson": LESSON}
        writer = "memory_proposal_agent" if kind == "waiting_lesson" else "admin_agent"
    created = _commit(vault, monkeypatch, writer, fields)
    mid = str(created["memory"]["id"])
    if action == "unexpire":
        failed, result = _tool(vault, monkeypatch, "admin_agent", "alice_memory_manage", {
            "action": "expire", "memory_id": mid, "reason": "Retire it",
        })
        assert not failed, result
    if door == "http":
        return _http(vault, profile, action, mid)[0] == 200
    failed, result = _tool(vault, monkeypatch, profile, "alice_memory_manage", {
        "action": action, "memory_id": mid, "reason": "Retire it",
    })
    if failed:
        assert result["error"]["code"] == "not_permitted"
    return not failed



def _cli(vault, action, memory_id):
    user, keys, context, _ = vault
    arguments = [sys.executable, "-c", "from alicebot_api.cli import main; raise SystemExit(main())",
                 "--database-url", context.database_url, "--user-id", str(user), "vnext", "memories", action]
    arguments += ["--memory-id", memory_id] if action == "undo" else [memory_id]
    arguments += ["--agent-id", "project_scoped_agent", "--permission-profile", "project_scoped_agent",
                  "--project-scope", "alpha", "--reason", "Retire it"]
    env = os.environ.copy()
    env["ALICE_AGENT_API_KEY"] = keys["project_scoped_agent"]
    return subprocess.run(arguments, capture_output=True, text=True, env=env, check=False).returncode


def _cli_target(vault, monkeypatch, kind, action):
    fields = {"lesson": LESSON} if kind == "lesson" else {
        "title": "A procedure", "canonical_text": "An ordinary procedure.", "confidence": 0.7,
    }
    result = _commit(vault, monkeypatch, "admin_agent" if kind == "lesson" else "author", fields)
    mid = str(result["memory"]["id"])
    if action == "unexpire":
        failed, answer = _tool(vault, monkeypatch, "admin_agent", "alice_memory_manage", {
            "action": "expire", "memory_id": mid, "reason": "Retire it",
        })
        assert not failed, answer
    return _cli(vault, action, mid)


def _legacy(vault, monkeypatch, kind):
    _, _, original, _ = vault
    user = seed_user(original.database_url, email=f"{uuid4()}@example.invalid")
    context = MCPRuntimeContext(database_url=original.database_url, user_id=user)
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    failures = []
    for action in ("forget", "undo"):
        if kind == "lesson":
            fields = {"lesson": LESSON, "agent_id": "publisher", "permission_profile": "admin_agent"}
        else:
            fields = {"title": "A procedure", "canonical_text": "An ordinary procedure.", "confidence": 0.7,
                      "agent_id": "author", "permission_profile": "project_scoped_agent"}
        failed, created = wire(context, "alice_memory_commit", {
            "domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], **fields,
        })
        assert not failed, created
        failed, result = wire(context, "alice_vnext_" + action + "_memory", {
            "memory_id": str(created["memory"]["id"]), "reason": "Retire it", "agent_id": "stranger",
            "permission_profile": "project_scoped_agent", "project_scope": ["alpha"],
        })
        if not failed or result["error"]["code"] != "not_permitted":
            failures.append(("legacy", action, kind))
    return failures

def test_pending_retirement_uses_decision_standing_through_each_door(vault, monkeypatch):
    """Guard: remove the pending retirement decision in _write_policy_decision."""
    failures = []
    for door in ("mcp", "http"):
        for action in ("forget", "expire", "undo"):
            for profile in PROFILES:
                assert _exercise(vault, monkeypatch, "active", action, profile, door) == (profile in PROFILES[2:])
                for kind in ("pending", "proposal"):
                    if _exercise(vault, monkeypatch, kind, action, profile, door) != (profile == "admin_agent"):
                        failures.append((door, action, kind, profile))
            assert _exercise(vault, monkeypatch, "pending", action, "author", door)
            assert _exercise(vault, monkeypatch, "proposal", action, "bound_admin", door)
    for action in ("forget", "expire", "undo"):
        if _cli_target(vault, monkeypatch, "pending", action) == 0:
            failures.append(("cli", action, "pending"))
    failures.extend(_legacy(vault, monkeypatch, "pending"))
    assert not failures, "\n".join(map(str, failures))


def test_lesson_retirement_uses_publisher_standing_through_each_door(vault, monkeypatch):
    """Guard: remove the lesson retirement decision in _write_policy_decision."""
    failures = []
    for door in ("mcp", "http"):
        for action in ("forget", "expire", "undo", "unexpire"):
            for profile in PROFILES:
                kinds = ("lesson", "waiting_lesson") if action != "unexpire" else ("lesson",)
                for kind in kinds:
                    if _exercise(vault, monkeypatch, kind, action, profile, door) != (profile == "admin_agent"):
                        failures.append((door, action, kind, profile))

    for action in ("forget", "expire", "undo", "unexpire"):
        if _cli_target(vault, monkeypatch, "lesson", action) == 0:
            failures.append(("cli", action, "lesson"))
    failures.extend(_legacy(vault, monkeypatch, "lesson"))

    for door in ("mcp", "http"):
        for action in ("forget", "expire", "undo", "unexpire"):
            assert _exercise(vault, monkeypatch, "lesson", action, "bound_admin", door)
            created = _commit(vault, monkeypatch, "admin_agent", {"lesson": LESSON})
            mid = str(created["memory"]["id"])
            if action == "unexpire":
                assert _http(vault, "admin_agent", "expire", mid)[0] == 200
            missing = str(uuid4())
            if door == "http":
                assert _http(vault, "hidden", action, mid) == _http(vault, "hidden", action, missing)
            else:
                hidden = _tool(vault, monkeypatch, "hidden", "alice_memory_manage", {"action": action, "memory_id": mid, "reason": "Retire it"})
                absent = _tool(vault, monkeypatch, "hidden", "alice_memory_manage", {"action": action, "memory_id": missing, "reason": "Retire it"})
                assert hidden == absent and hidden[0] and hidden[1]["error"]["code"] == "not_found"

    assert not failures, "\n".join(map(str, failures))
