"""The lesson block through the HTTP commit route on PostgreSQL.

``POST /v0/vnext/memories/commit`` and ``alice_memory_commit`` share the ONE
commit service. The SQLite MCP answers live in
``tests/unit/test_lesson_block_commit.py``; this file pins the HTTP answers:
the owner publishes at once, a bound key waits, and a refused shape writes
nothing.
"""

from __future__ import annotations

from typing import Any

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user

LESSON = {
    "applies_when": "editing CHANGELOG.md",
    "what_happened": "A check failed on the pull request.",
    "do_instead": "Repin the mutation manifest before committing.",
    "paths": ["CHANGELOG.md"],
}


def _agent(profile: str, *, agent_id: str, scope: list[str] | None = None) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "agent_type": "coding_agent",
        "permission_profile": profile,
        "project_scope": scope or [],
    }


def _route_patches(monkeypatch, app_url: str) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=app_url))
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: Settings(database_url=app_url))
    monkeypatch.setattr(vnext_retrieval_router, "get_settings", lambda: Settings(database_url=app_url))


def test_the_owner_publishes_a_lesson_at_once_over_http(migrated_database_urls, monkeypatch) -> None:
    """Guard: the owner carve-out in the commit service's publisher rule.

    Mutation: remove the ``identity is None`` arm; the owner's lesson waits for review over
    HTTP too and the ``committed`` assertion fails, while the SQLite test still passes.
    """

    app_url = migrated_database_urls["app"]
    _route_patches(monkeypatch, app_url)
    user_id = seed_user(app_url, email="lesson-owner-http@example.com")
    status, payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": str(user_id), "lesson": LESSON},
    )
    assert status == 201
    assert payload["status"] == "committed"
    assert payload["memory"]["memory_type"] == "procedure"
    assert payload["memory"]["value"]["lesson"]["do_instead"] == LESSON["do_instead"]
    assert payload["memory"]["canonical_text"] == (
        "Applies when: editing CHANGELOG.md Do instead: Repin the mutation manifest before "
        "committing. What happened: A check failed on the pull request."
    )


def test_a_bound_key_s_lesson_waits_over_http(migrated_database_urls, monkeypatch) -> None:
    """Guard: the lesson publisher rule on the HTTP door.

    Mutation: remove the ``lesson_requires_approval`` block; the lesson commits and this fails.
    """

    app_url = migrated_database_urls["app"]
    _route_patches(monkeypatch, app_url)
    user_id = seed_user(app_url, email="lesson-bound-http@example.com")
    from alicebot_api.db import user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key
    from alicebot_api.vnext_store import PostgresVNextStore

    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="writer",
            permission_profile="project_scoped_agent",
            project_scope="alpha",
        )
    status, payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": str(user_id), "agent": _agent("project_scoped_agent", agent_id="writer", scope=["alpha"]), "lesson": LESSON},
        authorization=f"Bearer {raw_key}",
    )
    assert status == 201
    assert payload["status"] == "review_required"
    assert payload["policy_decision"]["reason"] == "lesson_requires_approval"


def test_a_refused_lesson_shape_names_the_field_over_http(migrated_database_urls, monkeypatch) -> None:
    """Guard: the shape refusals answer a validation error naming the field and write nothing.

    Mutation: accept canonical_text with a lesson; the refusal disappears and this fails.
    """

    app_url = migrated_database_urls["app"]
    _route_patches(monkeypatch, app_url)
    user_id = seed_user(app_url, email="lesson-shape-http@example.com")
    status, payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={"user_id": str(user_id), "lesson": LESSON, "canonical_text": "Free text"},
    )
    assert status in (400, 422)
    assert "canonical_text" in str(payload)
    status, payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": str(user_id),
            "lesson": {**LESSON, "applies_when": "a" * 301},
        },
    )
    assert status in (400, 422)
    assert "applies_when" in str(payload)
