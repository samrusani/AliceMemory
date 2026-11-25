"""Exercise the documented reranker inputs and repeated-commit outcomes."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
from threading import Thread
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.integration.test_mcp_server import _call_tool, start_mcp_client

USER = UUID("00000000-0000-0000-0000-000000000001")
QUERY = "orchardblossom"
TITLES = ("orchardblossom Stocktake", "orchardblossom Planning", "orchardblossom password=fixture-only")
MEMORY_TEXTS = tuple(f"orchardblossom memo calendar {i}" for i in range(3))
SOURCE_FIELDS = {
    "summary": "fixture_summary_marker",
    "raw_text": "fixture_document_marker",
    "passage": "fixture_passage_marker",
}
OTHER_TITLE = "orchardblossom Sibling project fixture"


@pytest.fixture
def providers_off(monkeypatch):
    for name in (
        "ALICE_AGENT_API_KEY", "ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY",
        "ALICE_RERANKER_BASE_URL", "ALICE_RERANKER_MODEL", "ALICE_RERANKER_API_KEY", "ALICE_SEARCH_QUALITY",
        "ALICEBOT_AUTH_USER_ID", "ALICEBOT_AUTH_BEARER_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALICE_SOURCE_EMBEDDINGS", "off")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")


@pytest.fixture
def sqlite_vault(tmp_path, monkeypatch, providers_off):
    path = tmp_path / "memory.db"
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    bootstrap_database(path, user_id=str(USER), user_email="fixture@example.invalid")

    @contextmanager
    def store():
        with sqlite_user_connection(path, USER) as conn:
            yield SQLiteVNextStore(conn, USER)

    return MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=USER), store


@contextmanager
def recorder():
    prompts = []
    drop = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt = data["messages"][-1]["content"]
            prompts.append(prompt)
            count = len(re.findall(r"(?m)^\[\d+\] ", prompt))
            if drop:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            body = json.dumps({"choices": [{"message": {"content": json.dumps(list(range(count, 0, -1)))}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", prompts, drop
        finally:
            server.shutdown()
            thread.join(timeout=3)


def seed_source(store, title, project):
    src = store.create_source({
        "title": title, "source_type": "note", "domain": "project", "sensitivity": "public",
        "content_hash": str(uuid4()), "summary": SOURCE_FIELDS["summary"],
        "metadata_json": {"project_scope": [project], "summary": SOURCE_FIELDS["summary"], "raw_text": SOURCE_FIELDS["raw_text"]},
    })
    assert src["metadata_json"]["summary"] == SOURCE_FIELDS["summary"]
    assert src["metadata_json"]["raw_text"] == SOURCE_FIELDS["raw_text"]
    store.create_source_chunk({
        "source_id": str(src["id"]), "chunk_index": 0, "text": f"{QUERY} {SOURCE_FIELDS['passage']}",
    })


def seed_pack(store):
    for title in TITLES:
        seed_source(store, title, "orchard")
    for i, text in enumerate(MEMORY_TEXTS):
        store.create_memory({
            "title": f"Memo {i}", "memory_key": str(uuid4()), "memory_type": "procedure",
            "canonical_text": text, "value": {"text": text}, "status": "active", "confidence": 1.0,
            "domain": "project", "sensitivity": "public", "project_id": "orchard",
            "metadata_json": {"project_scope": ["orchard"]},
        })


@pytest.mark.parametrize("door", ("sqlite-mcp", "postgres-http", "postgres-mcp"))
def test_context_pack_reranker_sends_readable_titles_and_memory_text(door, request, monkeypatch, providers_off):
    """Mutation: append the synthetic source summary to the recorded request."""
    if door == "sqlite-mcp":
        context, store = request.getfixturevalue("sqlite_vault")
    else:
        harness = request.getfixturevalue("label_harness")
        context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
        store = harness.store

    with store() as vault:
        seed_pack(vault)

    def read_pack(key):
        if door == "postgres-http":
            status, result, _headers = harness.request(
                "POST", "/v0/vnext/context-packs",
                payload={"query": QUERY, "options": {"include_sources": True, "context_depth": "medium"}}, key=key,
            )
            # A keyed PostgreSQL installation requires a key on this protected route.
            assert status == 201, (status, result.keys())
            return result
        if key:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        else:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        return call_mcp_tool(context, name="alice_context_pack", arguments={
            "query": QUERY, "include_sources": True, "context_depth": "medium", "debug": True,
        })

    with recorder() as (endpoint, prompts, drop):
        monkeypatch.setenv("ALICE_RERANKER_BASE_URL", endpoint)
        monkeypatch.setenv("ALICE_RERANKER_MODEL", "fixture")

        def assert_requests(role, key, *, sibling_visible):
            result = read_pack(key)
            assert len(prompts) == 2, (door, role, len(prompts), result.keys())
            merged = "\n".join(prompts)
            if any(marker in merged for marker in SOURCE_FIELDS.values()):
                pytest.fail(f"{door} delivered source content beyond a title", pytrace=False)
            if not sibling_visible and OTHER_TITLE in merged:
                pytest.fail(f"{door} delivered a title outside the caller's project", pytrace=False)
            assert all(QUERY in request for request in prompts)
            assert [sum(text in prompt for text in MEMORY_TEXTS) for prompt in prompts] == [3, 0]
            assert [sum(title in prompt for title in TITLES[:2]) for prompt in prompts] == [0, 2]
            assert "password=fixture-only" in prompts[1]
            assert (OTHER_TITLE in merged) == sibling_visible
            prompts.clear()

        assert_requests("owner", None, sibling_visible=False)
        with store() as vault:
            seed_source(vault, OTHER_TITLE, "sibling")
            keys = [
                ("unbound-admin", create_agent_key(vault, user_id=context.user_id, agent_id="publisher", permission_profile="admin_agent")[1]),
                ("project", create_agent_key(vault, user_id=context.user_id, agent_id="reader", permission_profile="project_scoped_agent", project_scope="orchard")[1]),
                ("bound-admin", create_agent_key(vault, user_id=context.user_id, agent_id="reviewer", permission_profile="admin_agent", project_scope="orchard")[1]),
            ]
        for role, key in keys:
            assert_requests(role, key, sibling_visible=role == "unbound-admin")
        drop.append(True)
        result = read_pack(keys[0][1])
        assert len(prompts) == 2
        rr = result["trace"]["stages"]["reranker"]
        assert rr["memories"]["status"] == rr["sources"]["status"] == "fail_open: provider_error"


def mem_args(label):
    return {
        "title": f"Orchard test {label}", "canonical_text": f"The selected test option is {label}.",
        "memory_type": "decision", "domain": "project", "sensitivity": "public", "confidence": 0.96,
        "source_type": "direct_user_instruction",
    }


@pytest.mark.parametrize("door", ("sqlite-mcp", "postgres-http", "postgres-mcp"))
def test_identical_memory_commits_need_a_key_to_replay(door, request, monkeypatch, providers_off):
    """Mutation: omit the idempotency key from the fixture's keyed pair."""
    if door == "sqlite-mcp":
        context, store = request.getfixturevalue("sqlite_vault")
    else:
        harness = request.getfixturevalue("label_harness")
        context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
        store = harness.store
    with store() as vault:
        _, auth_key = create_agent_key(vault, user_id=context.user_id, agent_id="author", permission_profile="admin_agent")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", auth_key)

    def run(args):
        if door == "postgres-http":
            status, result, _headers = harness.request("POST", "/v0/vnext/memories/commit", payload=args, key=auth_key)
            assert status in {200, 201}, (status, result.keys())
            return result
        return _call_tool(client, name="alice_memory_commit", arguments=args)

    client = start_mcp_client(database_url=context.database_url, user_id=context.user_id) if door.endswith("-mcp") else None
    try:
        for label, key in (("unkeyed pair", None), ("keyed pair", "fixture-repeat-key")):
            args = mem_args(label)
            if key:
                args["idempotency_key"] = key
            first, second = (run(args) for _ in range(2))
            assert first["status"] == second["status"] == "committed", (first.keys(), second.keys())
            with store() as vault:
                rows = [
                    row for row in vault.list_memories(status="active", limit=25)
                    if row["title"] == args["title"] and row["canonical_text"] == args["canonical_text"]
                ]
            same_id = first["memory"]["id"] == second["memory"]["id"]
            replay = second.get("idempotent_replay", False)
            assert len(rows) == (1 if key else 2)
            assert same_id == bool(key)
            assert replay == bool(key)
    finally:
        if client is not None:
            client.close()
