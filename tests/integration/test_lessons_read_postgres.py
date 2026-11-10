"""alice_lessons on PostgreSQL, through the MCP tool and real agent keys.

The SQLite cases are in ``tests/unit/test_lessons_read.py``. This file reaches
the PostgreSQL lesson list, the bulk revision read and the fenced search.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPArgumentError, MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_vnext_live_workspace_api import seed_user

TASK = "about to rebase my feature branch onto main"
LESSON = {
    "applies_when": "rebasing a feature branch onto main",
    "what_happened": "The rebase rewrote commits teammates had already fetched.",
    "do_instead": "Merge main into the feature branch instead of rebasing it.",
    "paths": ["src/*.py"],
}
CALL = {"task": TASK, "paths": ["src/feature.py"]}


def _call(monkeypatch: pytest.MonkeyPatch, app_url: str, user_id: UUID, key: str | None, name: str, arguments: dict) -> dict:
    if key is None:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    return json.loads(json.dumps(call_mcp_tool(context, name=name, arguments=arguments), default=str))


def _key(app_url: str, user_id: UUID, agent_id: str, profile: str, project: str | None = None) -> str:
    with user_connection(app_url, user_id) as conn:
        _record, raw = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id=agent_id,
            permission_profile=profile,
            project_scope=project,
        )
    return raw


def _events(app_url: str, user_id: UUID) -> list[dict]:
    with user_connection(app_url, user_id) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor_type, actor_id, payload_json
            FROM event_log
            WHERE event_type = 'agent.lessons_requested'
            ORDER BY occurred_at ASC, id ASC
            """
        )
        rows = cur.fetchall()
    found = []
    for row in rows:
        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        found.append({"actor_type": row["actor_type"], "actor_id": row["actor_id"], "payload": payload})
    return found


def test_the_owner_reads_a_published_lesson_on_postgres(migrated_database_urls, monkeypatch) -> None:
    """Guard: a commit revision whose new value holds a lesson publishes it, and the owner is ``by``.

    Mutation: skip ``_revision_holds_lesson``. A procedure whose stored value was given a lesson
    with no such revision is then returned and the injected-block test fails on both stores.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="lessons-owner@example.invalid")
    committed = _call(monkeypatch, app_url, user_id, None, "alice_memory_commit", {"lesson": LESSON})
    assert committed["status"] == "committed"
    answer = _call(monkeypatch, app_url, user_id, None, "alice_lessons", CALL)
    assert answer["count"] == 1
    lesson = answer["lessons"][0]
    assert lesson["id"] == committed["memory"]["id"]
    assert lesson["applies_when"] == LESSON["applies_when"]
    assert lesson["paths"] == LESSON["paths"]
    assert lesson["published"]["by"] == "owner"
    assert lesson["published"]["how"] == "committed"
    assert lesson["writer"]["id"] == "owner"
    events = _events(app_url, user_id)
    assert len(events) == 1
    assert events[0]["actor_type"] == "user"
    assert events[0]["actor_id"] is None
    assert events[0]["payload"] == {"returned": 1, "limit": 5, "paths_given": 1}
    with pytest.raises(MCPArgumentError, match="must not be empty"):
        _call(monkeypatch, app_url, user_id, None, "alice_lessons", {"task": "   "})
    assert len(_events(app_url, user_id)) == 1


def test_a_lesson_block_with_no_publishing_act_is_not_returned_on_postgres(migrated_database_urls, monkeypatch) -> None:
    """Guard: the same publishing check as the SQLite test, read through the PostgreSQL revision query.

    The plain procedure's value is updated in the store after the commit. The call returns the
    published lesson only.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="lessons-plain@example.invalid")
    published = _call(monkeypatch, app_url, user_id, None, "alice_memory_commit", {"lesson": LESSON})
    plain = _call(
        monkeypatch, app_url, user_id, None, "alice_memory_commit",
        {
            "title": "A plain procedure",
            "canonical_text": "Plain procedure text that does not mention the task.",
            "memory_type": "procedure",
        },
    )
    assert plain["status"] == "committed"
    plain_id = plain["memory"]["id"]
    with user_connection(app_url, user_id) as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM memories WHERE id = %s", (plain_id,))
        value = cur.fetchone()["value"]
        if isinstance(value, str):
            value = json.loads(value)
        value = dict(value)
        value["lesson"] = LESSON
        cur.execute("UPDATE memories SET value = %s::jsonb WHERE id = %s", (json.dumps(value), plain_id))
    answer = _call(monkeypatch, app_url, user_id, None, "alice_lessons", CALL)
    assert [item["id"] for item in answer["lessons"]] == [published["memory"]["id"]]


def test_a_bound_key_does_not_read_another_projects_lesson_on_postgres(migrated_database_urls, monkeypatch) -> None:
    """Guard: the lessons read passes the caller's project scope into the PostgreSQL lesson list and search.

    Mutation: pass an empty project scope into ``list_lesson_memories``. The beta key then sees the
    alpha lesson and this fails.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="lessons-bound@example.invalid")
    admin = _key(app_url, user_id, "publisher", "admin_agent")
    beta = _key(app_url, user_id, "beta", "project_scoped_agent", "beta")
    alpha = _key(app_url, user_id, "alpha", "project_scoped_agent", "alpha")
    committed = _call(
        monkeypatch, app_url, user_id, admin, "alice_memory_commit",
        {
            "lesson": LESSON,
            "agent_id": "publisher",
            "agent_type": "coding_agent",
            "domain": "project",
            "sensitivity": "internal",
            "project_scope": ["alpha"],
        },
    )
    assert committed["status"] == "committed"
    hidden = _call(
        monkeypatch, app_url, user_id, beta, "alice_lessons",
        {**CALL, "agent_id": "beta", "agent_type": "coding_agent"},
    )
    assert hidden["count"] == 0
    assert hidden["lessons"] == []
    seen = _call(
        monkeypatch, app_url, user_id, alpha, "alice_lessons",
        {**CALL, "agent_id": "alpha", "agent_type": "coding_agent"},
    )
    assert [item["id"] for item in seen["lessons"]] == [committed["memory"]["id"]]
    assert seen["lessons"][0]["published"]["by"] == "publisher"
    assert seen["lessons"][0]["published"]["how"] == "committed"
