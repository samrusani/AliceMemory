from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import anyio

import alicebot_api.main as main_module
from alicebot_api.routers import continuity as continuity_router
from alicebot_api.config import Settings
from alicebot_api.contracts import MemoryCandidateInput
from alicebot_api.db import user_connection
from alicebot_api.memory import admit_memory_candidate
from alicebot_api.store import ContinuityStore


def invoke_request(
    method: str,
    path: str,
    *,
    query_params: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, object]] = []
    request_received = False

    async def receive() -> dict[str, object]:
        nonlocal request_received
        if request_received:
            return {"type": "http.disconnect"}
        request_received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    query_string = urlencode(query_params or {}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    anyio.run(main_module.app, scope, receive, send)

    start_message = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start_message["status"], json.loads(body)


def seed_trusted_fact_memories(database_url: str) -> dict[str, str]:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "trusted-facts@example.invalid", "Trusted Facts")
        thread = store.create_thread("Trusted fact promotions")
        session = store.create_session(thread["id"], status="active")
        coffee_event = store.append_event(
            thread["id"], session["id"], "message.user", {"text": "Prefer coffee"}
        )["id"]
        tea_event = store.append_event(
            thread["id"], session["id"], "message.user", {"text": "Prefer tea"}
        )["id"]
        generated_event = store.append_event(
            thread["id"], session["id"], "message.user", {"text": "Model guessed mate"}
        )["id"]

        admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.coffee",
                value={"drink": "coffee"},
                source_event_ids=(coffee_event,),
                memory_type="preference",
                trust_class="human_curated",
                promotion_eligibility="promotable",
                evidence_count=2,
                independent_source_count=2,
                trust_reason="owner confirmed",
            ),
        )
        admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.tea",
                value={"drink": "tea"},
                source_event_ids=(tea_event,),
                memory_type="preference",
                trust_class="deterministic",
                promotion_eligibility="promotable",
                evidence_count=1,
                independent_source_count=1,
                trust_reason="direct deterministic capture",
            ),
        )
        admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.preference.generated",
                value={"drink": "mate"},
                source_event_ids=(generated_event,),
                memory_type="preference",
                trust_class="llm_single_source",
                promotion_eligibility="promotable",
                evidence_count=1,
                independent_source_count=1,
                extracted_by_model="gpt-5.4-mini",
                trust_reason="single-source model extraction",
            ),
        )

    return {
        "user_id": str(user_id),
        "coffee_event": str(coffee_event),
        "tea_event": str(tea_event),
        "generated_event": str(generated_event),
    }


def test_pattern_and_playbook_endpoints_only_promote_trusted_facts(
    migrated_database_urls,
    monkeypatch,
) -> None:
    seeded = seed_trusted_fact_memories(migrated_database_urls["app"])
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )
    monkeypatch.setattr(
        continuity_router,
        "get_settings",
        lambda: Settings(database_url=migrated_database_urls["app"]),
    )

    list_status, list_payload = invoke_request(
        "GET",
        "/v0/patterns",
        query_params={"user_id": seeded["user_id"], "limit": "10"},
    )

    assert list_status == 200
    assert list_payload["summary"]["total_count"] == 1
    pattern = list_payload["items"][0]
    assert pattern["fact_count"] == 2
    assert len(pattern["evidence_chain"]) == 2
    assert seeded["generated_event"] not in [
        source_event_id
        for evidence in pattern["evidence_chain"]
        for source_event_id in evidence["source_event_ids"]
    ]

    explain_status, explain_payload = invoke_request(
        "GET",
        f"/v0/patterns/{pattern['id']}",
        query_params={"user_id": seeded["user_id"]},
    )
    assert explain_status == 200
    assert explain_payload["pattern"]["source_fact_ids"] == pattern["source_fact_ids"]
    assert explain_payload["pattern"]["evidence_chain"][0]["revision_action"] == "ADD"

    playbook_list_status, playbook_list_payload = invoke_request(
        "GET",
        "/v0/playbooks",
        query_params={"user_id": seeded["user_id"], "limit": "10"},
    )
    assert playbook_list_status == 200
    assert playbook_list_payload["summary"]["total_count"] == 1
    playbook = playbook_list_payload["items"][0]
    assert len(playbook["steps"]) == 2
    assert "opaque" in playbook["explanation"]

    playbook_explain_status, playbook_explain_payload = invoke_request(
        "GET",
        f"/v0/playbooks/{playbook['id']}",
        query_params={"user_id": seeded["user_id"]},
    )
    assert playbook_explain_status == 200
    assert playbook_explain_payload["playbook"]["source_fact_ids"] == pattern["source_fact_ids"]
