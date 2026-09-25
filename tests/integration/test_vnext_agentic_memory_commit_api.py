from __future__ import annotations

from typing import Any

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.routers import vnext_memories as vnext_memories_router
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router
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
