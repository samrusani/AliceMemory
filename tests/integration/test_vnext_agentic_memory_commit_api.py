from __future__ import annotations

import json
from typing import Any

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, call_mcp_tool
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user


def _agent(profile: str = "trusted_local_agent", *, agent_id: str = "hermes") -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "agent_type": "personal_assistant",
        "permission_profile": profile,
        "project_scope": ["Alice"],
    }


def test_agentic_memory_commit_correct_undo_and_audit_api(migrated_database_urls, monkeypatch) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="agentic-memory-api@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        vnext_memories_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        vnext_retrieval_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    user_id_text = str(user_id)

    commit_status, commit_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "agent": _agent(),
            "intent": "explicit_remember",
            "title": "Agentic memory API preference",
            "canonical_text": "Agentic memory API tests should use the official commit endpoint.",
            "memory_type": "semantic",
            "domain": "professional",
            "sensitivity": "internal",
            "confidence": 0.96,
            "source_type": "direct_user_instruction",
            "idempotency_key": "agentic-memory-api-commit",
        },
    )
    assert commit_status == 201
    assert commit_payload["status"] == "committed"
    memory_id = commit_payload["memory"]["id"]
    assert commit_payload["memory"]["status"] == "active"

    recent_status, recent_payload = invoke_request(
        "GET",
        "/v0/vnext/memories/recent-commits",
        query_params={"user_id": user_id_text, "limit": "5"},
    )
    assert recent_status == 200
    assert any(item["id"] == memory_id for item in recent_payload["recent_commits"])

    correct_status, correct_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/correct",
        payload={
            "user_id": user_id_text,
            "agent": _agent(),
            "memory_id": memory_id,
            "canonical_text": "Agentic memory API tests use official commit, correct, undo, and audit endpoints.",
            "reason": "Exercise correction revision flow.",
        },
    )
    assert correct_status == 200
    assert "correct, undo" in correct_payload["memory"]["canonical_text"]

    context_status, context_payload = invoke_request(
        "POST",
        "/v0/vnext/context-packs",
        payload={
            "user_id": user_id_text,
            "query": "official commit, correct",
            "scope": {"domains": ["professional"]},
            "options": {"sensitivity_allowed": ["public", "internal", "private", "unknown"]},
        },
    )
    assert context_status == 201
    assert any(item["id"] == memory_id for item in context_payload["relevant_memories"])

    undo_status, undo_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/undo",
        payload={"user_id": user_id_text, "agent": _agent(), "memory_id": memory_id, "reason": "Exercise undo flow."},
    )
    assert undo_status == 200
    assert undo_payload["status"] == "undone"
    assert undo_payload["memory"]["status"] == "superseded"

    after_undo_status, after_undo_payload = invoke_request(
        "POST",
        "/v0/vnext/context-packs",
        payload={
            "user_id": user_id_text,
            "query": "official commit, correct",
            "scope": {"domains": ["professional"]},
            "options": {"sensitivity_allowed": ["public", "internal", "private", "unknown"]},
        },
    )
    assert after_undo_status == 201
    assert all(item["id"] != memory_id for item in after_undo_payload["relevant_memories"])

    audit_status, audit_payload = invoke_request(
        "GET",
        f"/v0/vnext/memories/{memory_id}/audit",
        query_params={"user_id": user_id_text},
    )
    assert audit_status == 200
    assert audit_payload["memory"]["id"] == memory_id
    assert any(revision["revision_type"] == "corrected" for revision in audit_payload["revisions"])
    assert any(event["event_type"] == "agent.memory_undone" for event in audit_payload["events"])


def test_agentic_memory_commit_confirmation_review_and_rejection_api(migrated_database_urls, monkeypatch) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="agentic-memory-policy-api@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        vnext_memories_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        vnext_retrieval_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    user_id_text = str(user_id)

    confirmation_status, confirmation_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "agent": _agent(),
            "title": "Sensitive memory",
            "canonical_text": "Sensitive health preference needs confirmation before becoming context.",
            "domain": "health",
            "sensitivity": "private",
            "confidence": 0.93,
        },
    )
    assert confirmation_status == 201
    assert confirmation_payload["status"] == "confirmation_required"
    assert confirmation_payload["memory"]["status"] == "needs_review"
    confirmation_id = confirmation_payload["confirmation_id"]

    confirmed_status, confirmed_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/confirm",
        payload={
            "user_id": user_id_text,
            "agent": _agent(),
            "confirmation_id": confirmation_id,
            "action": "confirm",
        },
    )
    assert confirmed_status == 200
    assert confirmed_payload["status"] == "committed"
    assert confirmed_payload["memory"]["status"] == "active"

    review_status, review_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "agent": _agent(),
            "title": "External memory",
            "canonical_text": "External browser clips should stay in review.",
            "domain": "professional",
            "sensitivity": "internal",
            "source_type": "browser_clip",
            "confidence": 0.91,
        },
    )
    assert review_status == 201
    assert review_payload["status"] == "review_required"
    assert review_payload["memory"]["status"] == "candidate"

    rejected_status, rejected_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "agent": _agent("read_only_agent", agent_id="readonly"),
            "title": "Blocked write",
            "canonical_text": "Read-only agents cannot write memory.",
            "domain": "professional",
            "sensitivity": "internal",
        },
    )
    assert rejected_status == 200
    assert rejected_payload["status"] == "rejected"
    assert "read_only_agent_cannot_write" in rejected_payload["reasons"]


def test_unknown_domain_agentic_memory_selected_by_keyword_context_pack(migrated_database_urls, monkeypatch) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="agentic-memory-unknown-domain@example.com")
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(database_url=migrated_database_urls["app"]))
    monkeypatch.setattr(
        vnext_memories_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        vnext_retrieval_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    user_id_text = str(user_id)

    commit_status, commit_payload = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "agent": _agent(),
            "intent": "explicit_remember",
            "title": "Agent-first vNext preference",
            "canonical_text": (
                "Alice should be agent-first, with /vnext as an audit and correction cockpit "
                "rather than a required manual review dashboard."
            ),
            "memory_type": "semantic",
            "domain": "unknown",
            "sensitivity": "unknown",
            "confidence": 0.95,
            "source_type": "direct_user_instruction",
            "idempotency_key": "agentic-memory-unknown-domain-keyword",
        },
    )
    assert commit_status == 201
    memory_id = commit_payload["memory"]["id"]

    context_status, context_payload = invoke_request(
        "POST",
        "/v0/vnext/context-packs",
        payload={
            "user_id": user_id_text,
            "query": "agent-first /vnext audit correction cockpit",
            "scope": {"domains": ["professional", "project", "personal"]},
            "options": {
                "max_items": 20,
                "sensitivity_allowed": ["public", "internal", "private", "unknown"],
            },
        },
    )

    assert context_status == 201
    assert any(item["id"] == memory_id for item in context_payload["relevant_memories"])
    assert "no_relevant_memories_selected" not in context_payload["warnings"]


def test_blocked_idempotent_replay_returns_403_and_keeps_policy_rows(
    migrated_database_urls, monkeypatch
) -> None:
    """A blocked replay of a stored commit is 403, and the policy row stays.

    A new commit that policy rejects returns 200 with status rejected.
    Replaying an existing idempotency key runs the stored memory through
    the policy check and raises AgentPolicyBlockedError. Returning that
    from inside the connection keeps the policy events. Mutation: catch
    the error outside user_connection. The status can still be 403 and
    the blocked row is gone.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="blocked-replay@example.com")
    settings = Settings(database_url=app_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: settings)
    user_id_text = str(user_id)
    payload = {
        "user_id": user_id_text,
        "intent": "explicit_remember",
        "title": "Replay fence",
        "canonical_text": "A later read-only replay of this note must not be a server error.",
        "memory_type": "semantic",
        "domain": "professional",
        "sensitivity": "internal",
        "confidence": 0.96,
        "source_type": "direct_user_instruction",
        "idempotency_key": "blocked-replay-commit",
    }

    first_status, first_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent()},
    )
    assert first_status == 201, first_body
    memory_id = first_body["memory"]["id"]

    replay_status, replay_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent("read_only_agent", agent_id="readonly")},
    )
    assert replay_status == 403, replay_body
    assert "read_only_agent_cannot_write" in replay_body["policy_decision"]["reasons"]

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        events = store.list_events(target_type="memory", target_id=memory_id)
    blocked = [event for event in events if event.get("event_type") == "agent.policy_blocked"]
    assert blocked, "blocked replay did not keep agent.policy_blocked"


def test_blocked_mcp_replay_on_postgres_keeps_policy_rows(migrated_database_urls, monkeypatch) -> None:
    """T1's same-content replay on Postgres keeps the policy rows."""

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="mcp-blocked-replay@example.com")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    text = "A later read-only replay of this note must keep its policy rows."
    key = "mcp-blocked-replay-postgres"
    trusted = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": "Replay fence",
            "canonical_text": text,
            "domain": "professional",
            "sensitivity": "internal",
            "confidence": 0.96,
            "idempotency_key": key,
            "agent_id": "writer",
            "agent_type": "personal_assistant",
            "permission_profile": "trusted_local_agent",
        },
    )
    assert trusted["status"] == "committed"
    memory_id = trusted["memory"]["id"]
    try:
        call_mcp_tool(
            context,
            name="alice_memory_commit",
            arguments={
                "title": "Replay fence",
                "canonical_text": text,
                "domain": "professional",
                "sensitivity": "internal",
                "confidence": 0.96,
                "idempotency_key": key,
                "agent_id": "readonly",
                "agent_type": "personal_assistant",
                "permission_profile": "read_only_agent",
            },
        )
    except Exception as exc:
        assert type(exc) is MCPToolError
    else:
        raise AssertionError("expected MCPToolError")
    with user_connection(app_url, user_id) as conn:
        events = PostgresVNextStore(conn).list_events(target_type="memory", target_id=memory_id)
    rows = sorted(str(event.get("event_type")) for event in events if event.get("actor_id") == "readonly")
    assert rows == ["agent.policy_blocked", "policy.decision"]


def _bind_settings(monkeypatch, database_url: str) -> None:
    settings = Settings(database_url=database_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_memories_router, "get_settings", lambda: settings)


def test_blocked_replay_with_foreign_labels_matches_the_writer_400(
    migrated_database_urls, monkeypatch
) -> None:
    """A guessed key does not learn the stored row's labels."""

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="label-replay@example.com")
    _bind_settings(monkeypatch, app_url)
    user_id_text = str(user_id)
    key = "foreign-label-replay"
    seeded, seeded_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "intent": "explicit_remember",
            "title": "Private note",
            "canonical_text": "The stored note stays in its original project.",
            "memory_type": "semantic",
            "domain": "health",
            "sensitivity": "highly_sensitive",
            "confidence": 0.96,
            "source_type": "direct_user_instruction",
            "idempotency_key": key,
            "project_scope": ["ProjectB"],
        },
    )
    assert seeded == 201, seeded_body
    memory_id = seeded_body["memory"]["id"]
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        _project_record, project_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="project-a",
            permission_profile="project_scoped_agent",
            project_scope="ProjectA",
        )
        _admin_record, admin_key = create_agent_key(
            store,
            user_id=user_id,
            agent_id="admin",
            permission_profile="admin_agent",
        )
    different = {
        "user_id": user_id_text,
        "intent": "explicit_remember",
        "title": "Private note",
        "canonical_text": "This replay is a different memory request.",
        "memory_type": "semantic",
        "domain": "health",
        "sensitivity": "highly_sensitive",
        "confidence": 0.96,
        "source_type": "direct_user_instruction",
        "idempotency_key": key,
        "project_scope": ["ProjectB"],
    }
    blocked = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            **different,
            "agent": {
                "agent_id": "project-a",
                "agent_type": "personal_assistant",
                "permission_profile": "project_scoped_agent",
                "project_scope": ["ProjectA"],
            },
        },
        authorization=f"Bearer {project_key}",
    )
    admin = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**different, "agent": _agent("admin_agent", agent_id="admin")},
        authorization=f"Bearer {admin_key}",
    )
    assert blocked == admin
    assert blocked[0] == 400
    rendered = json.dumps(blocked[1])
    assert "ProjectB" not in rendered
    assert "health" not in rendered
    assert "highly_sensitive" not in rendered
    with user_connection(app_url, user_id) as conn:
        events = PostgresVNextStore(conn).list_events(target_type="memory", target_id=memory_id)
    blocked_rows = [event for event in events if event.get("event_type") == "agent.policy_blocked"]
    assert len(blocked_rows) == 1


def test_same_content_replay_after_project_move_is_400_without_the_new_label(
    migrated_database_urls, monkeypatch
) -> None:
    """Moving the row does not put the new project in a blocked replay body."""

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="moved-replay@example.com")
    _bind_settings(monkeypatch, app_url)
    user_id_text = str(user_id)
    key = "moved-project-replay"
    text = "The original note was filed under ProjectB."
    seeded, seeded_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "intent": "explicit_remember",
            "title": "Project note",
            "canonical_text": text,
            "memory_type": "semantic",
            "domain": "professional",
            "sensitivity": "internal",
            "confidence": 0.96,
            "source_type": "direct_user_instruction",
            "idempotency_key": key,
            "project_scope": ["ProjectB"],
        },
    )
    assert seeded == 201, seeded_body
    memory_id = seeded_body["memory"]["id"]
    moved, moved_body = invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={
            "user_id": user_id_text,
            "action": "assign_project",
            "project_id": "ProjectC",
        },
    )
    assert moved == 200, moved_body
    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="readonly",
            permission_profile="read_only_agent",
        )
    status, body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={
            "user_id": user_id_text,
            "agent": _agent("read_only_agent", agent_id="readonly"),
            "intent": "explicit_remember",
            "title": "Project note",
            "canonical_text": text,
            "memory_type": "semantic",
            "domain": "professional",
            "sensitivity": "internal",
            "confidence": 0.96,
            "source_type": "direct_user_instruction",
            "idempotency_key": key,
            "project_scope": ["ProjectB"],
        },
        authorization=f"Bearer {raw_key}",
    )
    assert status == 400, body
    assert "ProjectC" not in json.dumps(body)
    with user_connection(app_url, user_id) as conn:
        events = PostgresVNextStore(conn).list_events(target_type="memory", target_id=memory_id)
    blocked_rows = [event for event in events if event.get("event_type") == "agent.policy_blocked"]
    assert len(blocked_rows) == 1


_POLICY_EVENT_TYPES = frozenset({"policy.decision", "agent.policy_blocked"})


def _policy_event_count(app_url: str, user_id: object) -> int:
    with user_connection(app_url, user_id) as conn:
        events = PostgresVNextStore(conn).list_events()
    return sum(event.get("event_type") in _POLICY_EVENT_TYPES for event in events)


def _commit_payload(user_id_text: str, key: str, text: str) -> dict[str, Any]:
    return {
        "user_id": user_id_text,
        "intent": "explicit_remember",
        "title": "Labeled note",
        "canonical_text": text,
        "memory_type": "semantic",
        "domain": "professional",
        "sensitivity": "internal",
        "confidence": 0.96,
        "source_type": "direct_user_instruction",
        "idempotency_key": key,
        "project_scope": ["ProjectB"],
    }


def test_exact_replay_after_private_mark_is_400_without_the_new_label(
    migrated_database_urls, monkeypatch
) -> None:
    """Marking the row private is not an exact replay for a read-only key.

    The stored sensitivity is what the comparison reads. Mutation: drop
    the sensitivity comparison. The status is 403 and the body contains
    private.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="private-replay@example.com")
    _bind_settings(monkeypatch, app_url)
    user_id_text = str(user_id)
    text = "The original note was internal before the owner marked it private."
    payload = _commit_payload(user_id_text, "private-label-replay", text)
    seeded, seeded_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent()},
    )
    assert seeded == 201, seeded_body
    memory_id = seeded_body["memory"]["id"]
    marked, marked_body = invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={"user_id": user_id_text, "action": "private"},
    )
    assert marked == 200, marked_body
    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="readonly",
            permission_profile="read_only_agent",
        )
    status, body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent("read_only_agent", agent_id="readonly")},
        authorization=f"Bearer {raw_key}",
    )
    assert status == 400, body
    assert "private" not in json.dumps(body)


def test_exact_replay_after_domain_edit_is_400_without_the_new_label(
    migrated_database_urls, monkeypatch
) -> None:
    """Editing the domain to health is not an exact replay for a read-only key.

    The stored domain is what the comparison reads. Mutation: drop the
    domain comparison. The status is 403 and the body contains health.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="health-replay@example.com")
    _bind_settings(monkeypatch, app_url)
    user_id_text = str(user_id)
    text = "The original note was professional before the owner moved its domain."
    payload = _commit_payload(user_id_text, "health-label-replay", text)
    seeded, seeded_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent()},
    )
    assert seeded == 201, seeded_body
    memory_id = seeded_body["memory"]["id"]
    edited, edited_body = invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={"user_id": user_id_text, "action": "edit", "domain": "health"},
    )
    assert edited == 200, edited_body
    with user_connection(app_url, user_id) as conn:
        _record, raw_key = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="readonly",
            permission_profile="read_only_agent",
        )
    status, body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent("read_only_agent", agent_id="readonly")},
        authorization=f"Bearer {raw_key}",
    )
    assert status == 400, body
    assert "health" not in json.dumps(body)


def test_insert_race_conflict_rolls_back_policy_rows(migrated_database_urls, monkeypatch) -> None:
    """A conflict after the first idempotent lookup misses leaves the transaction.

    The caller is allowed to write the request, then the insert loses the
    digest race and the stored row is above the caller's ceiling. The
    conflict becomes a plain validation error, so the policy rows from
    that attempt roll back. Mutation: drop the conversion. The status can
    still be 400 and a new policy row is kept.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="race-replay@example.com")
    _bind_settings(monkeypatch, app_url)
    user_id_text = str(user_id)
    text = "The original note was internal before the owner raised its sensitivity."
    payload = _commit_payload(user_id_text, "insert-race-replay", text)
    seeded, seeded_body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent()},
    )
    assert seeded == 201, seeded_body
    memory_id = seeded_body["memory"]["id"]
    edited, edited_body = invoke_request(
        "POST",
        f"/v0/vnext/memories/{memory_id}/review",
        payload={"user_id": user_id_text, "action": "edit", "sensitivity": "highly_sensitive"},
    )
    assert edited == 200, edited_body
    before = _policy_event_count(app_url, user_id)
    original = VNextMemoryCommitService._idempotent_memory
    calls = {"n": 0}

    def miss_once(self: VNextMemoryCommitService, idempotency_key: str | None):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original(self, idempotency_key)

    monkeypatch.setattr(VNextMemoryCommitService, "_idempotent_memory", miss_once)
    status, body = invoke_request(
        "POST",
        "/v0/vnext/memories/commit",
        payload={**payload, "agent": _agent()},
    )
    assert calls["n"] >= 2
    assert status == 400, body
    assert _policy_event_count(app_url, user_id) == before
