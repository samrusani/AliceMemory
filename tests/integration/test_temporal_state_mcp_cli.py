from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID, uuid4

import psycopg

from alicebot_api.contracts import MemoryCandidateInput
from alicebot_api.db import user_connection
from alicebot_api.memory import admit_memory_candidate
from alicebot_api.store import ContinuityStore


REPO_ROOT = Path(__file__).resolve().parents[2]


def build_runtime_env(*, database_url: str, user_id: UUID) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["ALICEBOT_AUTH_USER_ID"] = str(user_id)
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


class MCPClient:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self._next_id = 1

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
    client = MCPClient(process)
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


def _call_tool(client: MCPClient, *, name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = client.request("tools/call", params={"name": name, "arguments": arguments})
    assert "error" not in response
    assert response["result"]["isError"] is False
    return json.loads(response["result"]["content"][0]["text"])


def _set_temporal_timestamps(
    admin_database_url: str,
    *,
    entity_id: UUID,
    edge_id: UUID,
    entity_created_at: datetime,
    edge_created_at: datetime,
) -> None:
    with psycopg.connect(admin_database_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE entities SET created_at = %s WHERE id = %s", (entity_created_at, entity_id))
            cur.execute(
                "UPDATE entity_edges SET created_at = %s, valid_from = %s WHERE id = %s",
                (edge_created_at, edge_created_at, edge_id),
            )


def seed_temporal_entity_graph(database_url: str, admin_database_url: str) -> dict[str, object]:
    user_id = uuid4()
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        store.create_user(user_id, "temporal-test@example.invalid", "Temporal Test")
        thread = store.create_thread("Temporal state thread")
        session = store.create_session(thread["id"], status="active")
        first_event = store.append_event(thread["id"], session["id"], "message.user", {"text": "AliceBot v1"})["id"]
        second_event = store.append_event(thread["id"], session["id"], "message.user", {"text": "AliceBot v2"})["id"]
        add_result = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.project.current",
                value={"name": "AliceBot v1"},
                source_event_ids=(first_event,),
                confidence=0.91,
                confirmation_status="confirmed",
                trust_class="human_curated",
                trust_reason="initial capture",
            ),
        )
    time.sleep(0.1)
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        update_result = admit_memory_candidate(
            store,
            user_id=user_id,
            candidate=MemoryCandidateInput(
                memory_key="user.project.current",
                value={"name": "AliceBot v2"},
                source_event_ids=(second_event,),
                confidence=0.98,
                trust_reason="confirmed by owner",
            ),
        )
    with user_connection(database_url, user_id) as conn:
        store = ContinuityStore(conn)
        entity = store.create_entity(
            entity_type="project",
            name="AliceBot",
            source_memory_ids=[add_result.memory["id"]],
        )
        person = store.create_entity(
            entity_type="person",
            name="Alex",
            source_memory_ids=[add_result.memory["id"]],
        )
        edge = store.create_entity_edge(
            from_entity_id=person["id"],
            to_entity_id=entity["id"],
            relationship_type="works_on",
            valid_from=datetime.fromisoformat("2026-03-12T09:30:00+00:00"),
            valid_to=None,
            source_memory_ids=[add_result.memory["id"]],
        )

    add_at = datetime.fromisoformat(add_result.revision["created_at"])
    update_at = datetime.fromisoformat(update_result.revision["created_at"])
    midpoint = add_at + (update_at - add_at) / 2
    _set_temporal_timestamps(
        admin_database_url,
        entity_id=entity["id"],
        edge_id=edge["id"],
        entity_created_at=add_at - timedelta(seconds=1),
        edge_created_at=midpoint,
    )
    return {
        "user_id": user_id,
        "entity_id": str(entity["id"]),
        "historical_at": midpoint.isoformat(),
    }


def test_cli_temporal_commands_support_historical_queries(migrated_database_urls) -> None:
    seeded = seed_temporal_entity_graph(migrated_database_urls["app"], migrated_database_urls["admin"])
    env = build_runtime_env(database_url=migrated_database_urls["app"], user_id=seeded["user_id"])

    help_result = run_cli(["--help"], env=env)
    assert help_result.returncode == 0
    assert "state-at" in help_result.stdout
    assert "timeline" in help_result.stdout

    state_result = run_cli(
        ["state-at", seeded["entity_id"], "--at", seeded["historical_at"]],
        env=env,
    )
    assert state_result.returncode == 0
    assert '"name":"AliceBot v1"' in state_result.stdout

    timeline_result = run_cli(["timeline", seeded["entity_id"], "--limit", "10"], env=env)
    assert timeline_result.returncode == 0
    assert "[fact_add]" in timeline_result.stdout
    assert "[fact_update]" in timeline_result.stdout

    explain_result = run_cli(
        ["explain", "--entity-id", seeded["entity_id"], "--at", seeded["historical_at"]],
        env=env,
    )
    assert explain_result.returncode == 0
    assert "supersession_chain=" in explain_result.stdout
    assert "trust=" in explain_result.stdout


def test_mcp_temporal_tools_return_historical_state_and_explainability(migrated_database_urls) -> None:
    seeded = seed_temporal_entity_graph(migrated_database_urls["app"], migrated_database_urls["admin"])
    client = start_mcp_client(database_url=migrated_database_urls["app"], user_id=seeded["user_id"])
    try:
        tools_list = client.request("tools/list")
        tool_names = [tool["name"] for tool in tools_list["result"]["tools"]]
        assert "alice_state_at" in tool_names
        assert "alice_timeline" in tool_names
        assert "alice_explain" in tool_names

        state_payload = _call_tool(
            client,
            name="alice_state_at",
            arguments={
                "entity_id": seeded["entity_id"],
                "at": seeded["historical_at"],
            },
        )
        assert state_payload["state_at"]["facts"][0]["value"] == {"name": "AliceBot v1"}

        timeline_payload = _call_tool(
            client,
            name="alice_timeline",
            arguments={
                "entity_id": seeded["entity_id"],
                "limit": 10,
            },
        )
        assert [event["event_type"] for event in timeline_payload["timeline"]["events"]] == [
            "entity_created",
            "fact_add",
            "edge_recorded",
            "fact_update",
        ]

        explain_payload = _call_tool(
            client,
            name="alice_explain",
            arguments={
                "entity_id": seeded["entity_id"],
                "at": seeded["historical_at"],
            },
        )
        assert explain_payload["explain"]["facts"][0]["trust"]["trust_class"] == "human_curated"
        assert [item["sequence_no"] for item in explain_payload["explain"]["facts"][0]["supersession_chain"]] == [1, 2]
    finally:
        client.close()
