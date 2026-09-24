from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import UUID, uuid4

import psycopg

from alicebot_api.continuity_brief import compile_continuity_brief
from alicebot_api.continuity_recall import query_continuity_recall
from alicebot_api.continuity_resumption import compile_continuity_resumption_brief
from alicebot_api.contracts import (
    ContinuityBriefRequestInput,
    ContinuityRecallQueryInput,
    ContinuityResumptionBriefRequestInput,
)
from alicebot_api.db import user_connection
from alicebot_api.session_briefing import SESSION_BRIEF_FRAME
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore


REPO_ROOT = Path(__file__).resolve().parents[2]


def seed_user(database_url: str, *, email: str) -> UUID:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, email, email.split("@", 1)[0].title())
    return user_id


def set_continuity_timestamps(
    admin_database_url: str,
    *,
    continuity_object_id: UUID,
    created_at: datetime,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE continuity_objects SET created_at = %s, updated_at = %s WHERE id = %s",
                (created_at, created_at, continuity_object_id),
            )


def build_runtime_env(*, database_url: str, user_id: UUID) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["ALICEBOT_AUTH_USER_ID"] = str(user_id)
    for name in (
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
    ):
        env.pop(name, None)
    # These suites exercise the legacy long-tail tools as well as the core
    # nine, so enable the legacy MCP surface for the spawned server.
    env["ALICE_MCP_LEGACY_TOOLS"] = "1"
    pythonpath_entries = [str(REPO_ROOT / "apps" / "api" / "src"), str(REPO_ROOT / "workers")]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    return env


def run_cli(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alicebot_api", *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_mcp_message(stream, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    stream.write(encoded)
    stream.flush()


def _read_mcp_message(stream) -> dict[str, object]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            raise RuntimeError("MCP server closed stdout unexpectedly")
        if line in {b"\r\n", b"\n"}:
            break
        decoded = line.decode("utf-8").strip()
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    content_length = int(headers["content-length"])
    body = stream.read(content_length)
    return json.loads(body.decode("utf-8"))


@dataclass
class MCPClient:
    process: subprocess.Popen[bytes]
    _next_id: int = 1

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        _write_mcp_message(self.process.stdin, payload)
        response = _read_mcp_message(self.process.stdout)
        assert response.get("id") == request_id
        return response

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        _write_mcp_message(self.process.stdin, payload)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def start_mcp_client(*, database_url: str, user_id: UUID) -> MCPClient:
    env = build_runtime_env(database_url=database_url, user_id=user_id)
    process = subprocess.Popen(
        [sys.executable, "-m", "alicebot_api.mcp_server"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    client = MCPClient(process=process)
    initialize = client.request(
        "initialize",
        params={
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "pytest-mcp-client", "version": "1.0"},
            "capabilities": {},
        },
    )
    assert initialize["result"]["protocolVersion"] == "2024-11-05"
    client.notify("notifications/initialized", {})
    return client


def _call_tool(client: MCPClient, *, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    response = client.request("tools/call", params={"name": name, "arguments": arguments})
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def _call_tool_error(client: MCPClient, *, name: str, arguments: dict[str, object]) -> str:
    response = client.request("tools/call", params={"name": name, "arguments": arguments})
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    return str(result["content"][0]["text"])


def test_mcp_recall_and_resume_match_core_and_cli_behavior(migrated_database_urls) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="mcp-parity@example.com")
    thread_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        decision_capture = store.create_continuity_capture_event(
            raw_content="Decision: Keep release freeze",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        decision_object = store.create_continuity_object(
            capture_event_id=decision_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Keep release freeze",
            body={"decision_text": "Keep release freeze"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["mcp-parity-1"]},
            confidence=0.96,
        )

        next_action_capture = store.create_continuity_capture_event(
            raw_content="Next Action: Draft release memo",
            explicit_signal="next_action",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_next_action",
        )
        next_action_object = store.create_continuity_object(
            capture_event_id=next_action_capture["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Draft release memo",
            body={"action_text": "Draft release memo"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["mcp-parity-2"]},
            confidence=0.92,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=decision_object["id"],
        created_at=datetime(2026, 4, 2, 9, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_action_object["id"],
        created_at=datetime(2026, 4, 2, 9, 5, tzinfo=UTC),
    )

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        core_recall = query_continuity_recall(
            store,
            user_id=user_id,
            request=ContinuityRecallQueryInput(
                thread_id=thread_id,
                query="release",
                limit=20,
            ),
        )
        core_resume = compile_continuity_resumption_brief(
            store,
            user_id=user_id,
            request=ContinuityResumptionBriefRequestInput(
                thread_id=thread_id,
                max_recent_changes=5,
                max_open_loops=5,
            ),
        )

    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        mcp_recall = _call_tool(
            client,
            name="alice_recall",
            arguments={
                "thread_id": str(thread_id),
                "query": "release",
                "limit": 20,
            },
        )
        mcp_resume = _call_tool(
            client,
            name="alice_resume",
            arguments={
                "thread_id": str(thread_id),
                "max_recent_changes": 5,
                "max_open_loops": 5,
            },
        )
        mcp_recall_debug = _call_tool(
            client,
            name="alice_recall_debug",
            arguments={
                "thread_id": str(thread_id),
                "query": "release",
                "limit": 20,
            },
        )
        retrieval_run_id = mcp_recall_debug["debug"]["retrieval_run_id"]
        mcp_retrieval_trace = _call_tool(
            client,
            name="alice_retrieval_trace",
            arguments={"retrieval_run_id": retrieval_run_id},
        )
    finally:
        client.close()

    # Core alice_recall searches vNext memories now (none seeded in this test);
    # legacy continuity parity is asserted through alice_recall_debug below.
    # An empty result still carries framing. HTTP context packs do the same
    # on every pack, including an empty one, so the shape stays stable.
    assert mcp_recall == {
        "framing": SESSION_BRIEF_FRAME,
        "query": "release",
        "results": [],
        "count": 0,
    }
    # Core resume is canonical vNext. This fixture intentionally seeds only
    # the legacy continuity store, so the core view is empty and reports the
    # legacy-only thread filter instead of silently switching backends.
    assert mcp_resume["brief"]["mode"] == "vnext"
    assert mcp_resume["brief"]["last_decision"] is None
    assert mcp_resume["brief"]["next_action"] is None
    assert mcp_resume["brief"]["open_loops"] == []
    assert mcp_resume["brief"]["filters_ignored"] == ["thread_id"]
    assert mcp_recall_debug["items"] == core_recall["items"]
    assert mcp_recall_debug["debug"]["candidate_count"] >= 1
    assert mcp_retrieval_trace["retrieval_run"]["id"] == retrieval_run_id

    env = build_runtime_env(database_url=migrated_database_urls["app"], user_id=user_id)
    cli_recall = run_cli(
        ["recall", "--thread-id", str(thread_id), "--query", "release", "--limit", "20"],
        env=env,
    )
    assert cli_recall.returncode == 0
    assert core_recall["items"][0]["title"] in cli_recall.stdout
    assert core_recall["items"][0]["id"] in cli_recall.stdout

    cli_resume = run_cli(
        ["resume", "--thread-id", str(thread_id), "--max-recent-changes", "5", "--max-open-loops", "5"],
        env=env,
    )
    assert cli_resume.returncode == 0
    assert core_resume["brief"]["last_decision"]["item"]["title"] in cli_resume.stdout
    assert core_resume["brief"]["next_action"]["item"]["title"] in cli_resume.stdout


def test_mcp_review_provenance_is_validated_atomically_on_postgres(
    migrated_database_urls,
) -> None:
    user_id = seed_user(
        migrated_database_urls["app"], email="mcp-provenance-atomic@example.com"
    )
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "manual_text",
                "title": "Reviewed source",
                "content_hash": f"sha256:{uuid4().hex}",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"raw_text": "The reviewed release decision."},
            },
            actor_type="user",
        )
        chunk = store.create_source_chunk(
            {
                "source_id": str(source["id"]),
                "chunk_index": 0,
                "text": "The reviewed release decision.",
                "token_count": 5,
                "metadata_json": {},
            },
            actor_type="user",
        )
        unrelated_source = store.create_source(
            {
                "source_type": "manual_text",
                "title": "Unrelated source",
                "content_hash": f"sha256:{uuid4().hex}",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"raw_text": "Unrelated evidence."},
            },
            actor_type="user",
        )
        unrelated_chunk = store.create_source_chunk(
            {
                "source_id": str(unrelated_source["id"]),
                "chunk_index": 0,
                "text": "Unrelated evidence.",
                "token_count": 2,
                "metadata_json": {},
            },
            actor_type="user",
        )
        memory = store.create_memory(
            {
                "memory_key": f"mcp.review.{uuid4().hex}",
                "value": {"text": "Pending release decision."},
                "status": "needs_review",
                "memory_type": "decision",
                "confidence": 0.6,
                "title": "Pending release decision",
                "canonical_text": "Pending release decision.",
                "summary": "Pending release decision.",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"review_required": True},
            },
            actor_type="user",
        )

    other_user_id = seed_user(
        migrated_database_urls["app"], email="mcp-provenance-other-user@example.com"
    )
    with user_connection(migrated_database_urls["app"], other_user_id) as conn:
        out_of_scope_source = PostgresVNextStore(conn).create_source(
            {
                "source_type": "manual_text",
                "title": "Other user's source",
                "content_hash": f"sha256:{uuid4().hex}",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"raw_text": "Must not cross tenant scope."},
            },
            actor_type="user",
        )

    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        expected_tool_error = (
            '{"error":{"code":"tool_request_failed","message":"The tool request could not be processed"}}'
        )
        for invalid_confidence in (-0.1, 1.5):
            confidence_error = _call_tool_error(
                client,
                name="alice_memory_correct",
                arguments={
                    "review_item_id": str(memory["id"]),
                    "action": "edit-and-approve",
                    "confidence": invalid_confidence,
                },
            )
            assert confidence_error == expected_tool_error

        for invalid_confidence in (-0.1, 1.5):
            replacement_confidence_error = _call_tool_error(
                client,
                name="alice_memory_correct",
                arguments={
                    "review_item_id": str(memory["id"]),
                    "action": "supersede-existing",
                    "replacement_title": "Invalid replacement",
                    "replacement_confidence": invalid_confidence,
                },
            )
            assert replacement_confidence_error == expected_tool_error

        malformed_body_error = _call_tool_error(
            client,
            name="alice_memory_correct",
            arguments={
                "review_item_id": str(memory["id"]),
                "action": "edit-and-approve",
                "body": {"text": 7},
            },
        )
        assert malformed_body_error == expected_tool_error

        malformed_provenance_error = _call_tool_error(
            client,
            name="alice_memory_correct",
            arguments={
                "review_item_id": str(memory["id"]),
                "action": "edit-and-approve",
                "provenance": {
                    "source_id": str(source["id"]),
                    "evidence_role": "not-real",
                },
            },
        )
        assert malformed_provenance_error == expected_tool_error

        malformed_uuid_error = _call_tool_error(
            client,
            name="alice_memory_correct",
            arguments={
                "review_item_id": str(memory["id"]),
                "action": "edit-and-approve",
                "provenance": {"source_id": "not-a-uuid"},
            },
        )
        assert malformed_uuid_error == expected_tool_error

        error = _call_tool_error(
            client,
            name="alice_memory_correct",
            arguments={
                "review_item_id": str(memory["id"]),
                "action": "edit-and-approve",
                "provenance": {
                    "source_id": str(out_of_scope_source["id"]),
                    "evidence_role": "supports",
                },
            },
        )
        assert error == expected_tool_error

        mismatched_chunk_error = _call_tool_error(
            client,
            name="alice_memory_correct",
            arguments={
                "review_item_id": str(memory["id"]),
                "action": "edit-and-approve",
                "provenance": {
                    "source_id": str(source["id"]),
                    "source_chunk_id": str(unrelated_chunk["id"]),
                    "evidence_role": "supports",
                },
            },
        )
        assert mismatched_chunk_error == expected_tool_error

        with user_connection(migrated_database_urls["app"], user_id) as conn:
            store = PostgresVNextStore(conn)
            unchanged = store.get_memory(str(memory["id"]))
            assert unchanged is not None
            assert unchanged["status"] == "needs_review"
            assert unchanged["canonical_text"] == "Pending release decision."
            assert store.list_provenance_links(
                target_type="memory", target_id=str(memory["id"])
            ) == []
            assert store.list_revisions(str(memory["id"])) == []

        approved = _call_tool(
            client,
            name="alice_memory_correct",
            arguments={
                "review_item_id": str(memory["id"]),
                "action": "edit-and-approve",
                "body": {"text": "Approved release decision."},
                "provenance": {
                    "source_id": str(source["id"]),
                    "source_chunk_id": str(chunk["id"]),
                    "evidence_role": "quoted_from",
                    "confidence": 0.91,
                    "quote": "The reviewed release decision.",
                },
            },
        )
    finally:
        client.close()

    assert approved["memory"]["status"] == "active"
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        links = store.list_provenance_links(
            target_type="memory", target_id=str(memory["id"])
        )
        assert len(links) == 1
        assert str(links[0]["source_id"]) == str(source["id"])
        assert str(links[0]["source_chunk_id"]) == str(chunk["id"])
        assert links[0]["evidence_role"] == "quoted_from"
        assert float(links[0]["confidence"]) == 0.91


def test_mcp_recall_advertises_and_applies_all_distributed_scopes_on_postgres(
    migrated_database_urls,
) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="mcp-recall-scopes@example.com")
    thread_id = UUID("11111111-1111-4111-8111-111111111111")
    task_id = UUID("22222222-2222-4222-8222-222222222222")
    in_window = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    def create_scoped_memory(
        store: PostgresVNextStore,
        *,
        suffix: str,
        scoped_thread_id: UUID = thread_id,
        scoped_task_id: UUID = task_id,
        project: str = "Apollo",
        person: str = "Sam",
        valid_from: datetime = in_window,
    ) -> dict[str, object]:
        return store.create_memory(
            {
                "memory_key": f"mcp.scope.{suffix}.{uuid4().hex}",
                "value": {"text": f"Scope parity release marker {suffix}."},
                "status": "active",
                "memory_type": "decision",
                "confidence": 0.9,
                "title": f"Scope parity {suffix}",
                "canonical_text": f"Scope parity release marker {suffix}.",
                "summary": f"Scope parity release marker {suffix}.",
                "domain": "project",
                "sensitivity": "internal",
                "valid_from": valid_from,
                "metadata_json": {
                    "thread_id": str(scoped_thread_id),
                    "task_id": str(scoped_task_id),
                    "project_scope": [project],
                    "person": person,
                },
            },
            actor_type="user",
        )

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = PostgresVNextStore(conn)
        expected = create_scoped_memory(store, suffix="expected")
        create_scoped_memory(
            store,
            suffix="wrong-thread",
            scoped_thread_id=UUID("33333333-3333-4333-8333-333333333333"),
        )
        create_scoped_memory(
            store,
            suffix="wrong-task",
            scoped_task_id=UUID("44444444-4444-4444-8444-444444444444"),
        )
        create_scoped_memory(store, suffix="wrong-project", project="Zeus")
        create_scoped_memory(store, suffix="wrong-person", person="Alex")
        create_scoped_memory(
            store,
            suffix="outside-window",
            valid_from=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
        )

    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        listed = client.request("tools/list")["result"]["tools"]
        recall_tool = next(tool for tool in listed if tool["name"] == "alice_recall")
        recall_properties = recall_tool["inputSchema"]["properties"]
        assert {
            "thread_id",
            "task_id",
            "project",
            "person",
            "since",
            "until",
        } <= set(recall_properties)

        recalled = _call_tool(
            client,
            name="alice_recall",
            arguments={
                "query": "scope parity release marker",
                "thread_id": str(thread_id),
                "task_id": str(task_id),
                "project": "Apollo",
                "person": "Sam",
                "since": "2026-07-01T00:00:00Z",
                "until": "2026-07-31T23:59:59Z",
                "limit": 20,
            },
        )
    finally:
        client.close()

    assert recalled["count"] == 1
    assert [item["id"] for item in recalled["results"]] == [str(expected["id"])]


def test_mcp_one_call_brief_matches_core_and_cli_surface(migrated_database_urls) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="mcp-one-call@example.com")
    thread_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        fact_one = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Fact: Deploy owner is Platform",
                explicit_signal="remember_this",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_memory",
            )["id"],
            object_type="MemoryFact",
            status="active",
            title="Fact: Deploy owner is Platform",
            body={"fact_key": "deploy_owner", "fact_value": "Platform"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-parity-1"]},
            confidence=0.9,
        )
        fact_two = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Fact: Deploy owner is Infrastructure",
                explicit_signal="remember_this",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_memory",
            )["id"],
            object_type="MemoryFact",
            status="active",
            title="Fact: Deploy owner is Infrastructure",
            body={"fact_key": "deploy_owner", "fact_value": "Infrastructure"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-parity-2"]},
            confidence=0.86,
        )
        blocker = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Blocker: Await deploy approval",
                explicit_signal="blocker",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_blocker",
            )["id"],
            object_type="Blocker",
            status="active",
            title="Blocker: Await deploy approval",
            body={"blocker_text": "Await deploy approval"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-parity-3"]},
            confidence=0.91,
        )
        next_action = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Next Action: Send deploy plan",
                explicit_signal="next_action",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_next_action",
            )["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Send deploy plan",
            body={"action_text": "Send deploy plan"},
            provenance={"thread_id": str(thread_id), "source_event_ids": ["brief-parity-4"]},
            confidence=0.95,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=fact_one["id"],
        created_at=datetime(2026, 4, 3, 9, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=fact_two["id"],
        created_at=datetime(2026, 4, 3, 9, 5, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=blocker["id"],
        created_at=datetime(2026, 4, 3, 9, 10, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_action["id"],
        created_at=datetime(2026, 4, 3, 9, 15, tzinfo=UTC),
    )

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        core_brief = compile_continuity_brief(
            store,
            user_id=user_id,
            request=ContinuityBriefRequestInput(
                brief_type="coding_context",
                thread_id=thread_id,
                query="deploy",
                max_relevant_facts=4,
                max_recent_changes=4,
                max_open_loops=3,
                max_conflicts=3,
                max_timeline_highlights=4,
            ),
        )

    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        mcp_brief = _call_tool(
            client,
            name="alice_brief",
            arguments={
                "brief_type": "coding_context",
                "thread_id": str(thread_id),
                "query": "deploy",
                "max_relevant_facts": 4,
                "max_recent_changes": 4,
                "max_open_loops": 3,
                "max_conflicts": 3,
                "max_timeline_highlights": 4,
            },
        )
    finally:
        client.close()

    assert mcp_brief == core_brief

    env = build_runtime_env(database_url=migrated_database_urls["app"], user_id=user_id)
    cli_brief = run_cli(
        [
            "brief",
            "--brief-type",
            "coding_context",
            "--thread-id",
            str(thread_id),
            "--query",
            "deploy",
            "--max-relevant-facts",
            "4",
            "--max-recent-changes",
            "4",
            "--max-open-loops",
            "3",
            "--max-conflicts",
            "3",
            "--max-timeline-highlights",
            "4",
        ],
        env=env,
    )
    assert cli_brief.returncode == 0
    assert core_brief["brief"]["next_suggested_action"]["title"] in cli_brief.stdout
    assert "confidence=" in cli_brief.stdout
    assert "conflicts:" in cli_brief.stdout


def test_mcp_task_brief_compare_smoke_matches_cli_surface(migrated_database_urls) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="mcp-task-briefs@example.com")
    thread_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)
        decision = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Decision: Freeze release scope",
                explicit_signal="decision",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_decision",
            )["id"],
            object_type="Decision",
            status="active",
            title="Decision: Freeze release scope",
            body={"decision_text": "Freeze release scope"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )
        next_action = store.create_continuity_object(
            capture_event_id=store.create_continuity_capture_event(
                raw_content="Next Action: Send release checklist",
                explicit_signal="next_action",
                admission_posture="DERIVED",
                admission_reason="explicit_signal_next_action",
            )["id"],
            object_type="NextAction",
            status="active",
            title="Next Action: Send release checklist",
            body={"action_text": "Send release checklist"},
            provenance={"thread_id": str(thread_id)},
            confidence=1.0,
        )

    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=decision["id"],
        created_at=datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
    )
    set_continuity_timestamps(
        migrated_database_urls["admin"],
        continuity_object_id=next_action["id"],
        created_at=datetime(2026, 4, 14, 10, 5, tzinfo=UTC),
    )

    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        mcp_compare = _call_tool(
            client,
            name="alice_task_brief_compare",
            arguments={
                "mode": "worker_subtask",
                "compare_to_mode": "user_recall",
                "thread_id": str(thread_id),
            },
        )
        assert mcp_compare["comparison"]["smaller_mode"] == "worker_subtask"
    finally:
        client.close()

    cli_env = build_runtime_env(database_url=migrated_database_urls["app"], user_id=user_id)
    cli_compare = run_cli(
        [
            "task-briefs",
            "compare",
            "--mode",
            "worker_subtask",
            "--compare-to-mode",
            "user_recall",
            "--thread-id",
            str(thread_id),
        ],
        env=cli_env,
    )
    assert cli_compare.returncode == 0
    assert "smaller_mode: worker_subtask" in cli_compare.stdout


def test_mcp_contradiction_and_trust_tools_smoke(migrated_database_urls) -> None:
    user_id = seed_user(migrated_database_urls["app"], email="mcp-contradictions@example.com")
    thread_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")

    with user_connection(migrated_database_urls["app"], user_id) as conn:
        store = ContinuityStore(conn)

        first_capture = store.create_continuity_capture_event(
            raw_content="Decision: Release mode canary",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        first_object = store.create_continuity_object(
            capture_event_id=first_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Release mode canary",
            body={
                "fact_key": "release_mode",
                "fact_value": "canary",
                "decision_text": "Release mode canary",
            },
            provenance={"thread_id": str(thread_id)},
            confidence=0.94,
        )

        second_capture = store.create_continuity_capture_event(
            raw_content="Decision: Release mode beta",
            explicit_signal="decision",
            admission_posture="DERIVED",
            admission_reason="explicit_signal_decision",
        )
        store.create_continuity_object(
            capture_event_id=second_capture["id"],
            object_type="Decision",
            status="active",
            title="Decision: Release mode beta",
            body={
                "fact_key": "release_mode",
                "fact_value": "beta",
                "decision_text": "Release mode beta",
            },
            provenance={"thread_id": str(thread_id)},
            confidence=0.94,
        )

    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=user_id)
    try:
        detected = _call_tool(
            client,
            name="alice_contradictions_detect",
            arguments={"limit": 20},
        )
        assert detected["summary"]["open_case_count"] == 1
        contradiction_case_id = detected["items"][0]["id"]

        listed = _call_tool(
            client,
            name="alice_contradictions_list",
            arguments={"status": "open", "limit": 20},
        )
        assert listed["summary"]["returned_count"] == 1
        assert listed["items"][0]["id"] == contradiction_case_id

        detailed = _call_tool(
            client,
            name="alice_contradictions_list",
            arguments={"contradiction_case_id": contradiction_case_id},
        )
        assert detailed["contradiction_case"]["id"] == contradiction_case_id

        signals = _call_tool(
            client,
            name="alice_trust_signals",
            arguments={
                "continuity_object_id": str(first_object["id"]),
                "signal_state": "active",
                "limit": 20,
            },
        )
        assert signals["summary"]["returned_count"] == 1
        assert signals["items"][0]["signal_type"] == "contradiction"

        resolved = _call_tool(
            client,
            name="alice_contradictions_resolve",
            arguments={
                "contradiction_case_id": contradiction_case_id,
                "action": "confirm_primary",
                "note": "Primary record remains current.",
            },
        )
        assert resolved["contradiction_case"]["status"] == "resolved"

        active_after = _call_tool(
            client,
            name="alice_trust_signals",
            arguments={
                "continuity_object_id": str(first_object["id"]),
                "signal_state": "active",
                "limit": 20,
            },
        )
        assert active_after["items"] == []
    finally:
        client.close()
