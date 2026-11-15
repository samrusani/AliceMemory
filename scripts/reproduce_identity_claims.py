"""Print synthetic keyed claim results on SQLite MCP, PostgreSQL MCP, or HTTP."""
import argparse
import json
import logging
import os
import tempfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api import mcp_server
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key


class Vault:
    def __init__(self, path):
        self.path = path
        self.user = uuid4()
        bootstrap_database(path, user_id=self.user, user_email="fixture@example.invalid")

    def key(self, name, profile, project=None):
        with sqlite_user_connection(self.path, self.user) as conn:
            _, raw = create_agent_key(SQLiteVNextStore(conn, self.user), user_id=self.user,
                                      agent_id=name, permission_profile=profile, project_scope=project)
        return raw


def wire(context, name, arguments):
    server = mcp_server.MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    result = server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def cases():
    return (
        ("id-flat", "bound", "read", {"agent_id": "different-a"}),
        ("id-nested", "bound", "read", {"agent_identity": {"agent_id": "different-b"}}),
        ("flat-both-empty", "bound", "read", {"agent_id": "different-c", "permission_profile": "admin_agent", "agent_identity": {}}),
        ("flat-id-nested-own", "bound", "read", {"agent_id": "different-d", "agent_identity": {"agent_id": "bound"}}),
        ("flat-high-nested-own", "bound", "read", {"permission_profile": "admin_agent", "agent_identity": {"agent_id": "bound"}}),
        ("unknown-alone", "bound", "read", {"permission_profile": "unknown-one"}),
        ("unknown-own", "bound", "read", {"agent_id": "bound", "permission_profile": "unknown-two"}),
        ("unknown-other", "bound", "read", {"agent_id": "different-e", "permission_profile": "unknown-three"}),
        ("type-other", "bound", "read", {"agent_id": "different-f", "agent_type": "unknown-type-one"}),
        ("type-own", "bound", "read", {"agent_id": "bound", "agent_type": "unknown-type-two"}),
        ("type-alone", "bound", "read", {"agent_type": "unknown-type-three"}),
        ("scope-flat", "bound", "read", {"project_scope": ["beta-one"]}),
        ("scope-empty", "bound", "read", {"project_scope": ["beta-two"], "agent_identity": {}}),
        ("same-id", "bound", "read", {"agent_id": "bound", "agent_identity": {"agent_id": "bound"}}),
        ("empty", "bound", "read", {"agent_identity": {}}),
        ("write-id-high-empty", "bound", "write", {"agent_id": "different-g", "permission_profile": "admin_agent", "agent_identity": {}}),
        ("write-unknown-other", "bound", "write", {"agent_id": "different-h", "permission_profile": "unknown-four"}),
        ("write-scope-empty", "bound", "write", {"project_scope": ["beta-three"], "agent_identity": {}}),
        ("admin-lower", "admin", "write", {"permission_profile": "read_only_agent"}),
        ("admin-lower-empty", "admin", "write", {"permission_profile": "read_only_agent", "agent_identity": {}}),
        ("admin-lower-nested", "admin", "write", {"agent_identity": {"permission_profile": "read_only_agent"}}),
        ("admin-conflict", "admin", "write", {"agent_identity": {"permission_profile": "read_only_agent"}, "permission_profile": "admin_agent"}),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--door", choices=("sqlite-mcp", "postgres-mcp", "http"), required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
        os.environ["ALICE_PROJECT_DIR"] = directory
        if args.door == "sqlite-mcp":
            vault = Vault(Path(directory) / "vault.db")
            keys = {"bound": vault.key("bound", "project_scoped_agent", "alpha"), "admin": vault.key("admin", "admin_agent")}
            context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

            def events():
                with sqlite_user_connection(vault.path, vault.user) as conn:
                    return len([e for e in SQLiteVNextStore(conn, vault.user).list_events() if e["event_type"] == "agent.key_escalation_rejected"])
        else:
            from alicebot_api.db import user_connection
            from alicebot_api.store import ContinuityStore
            from alicebot_api.vnext_store import PostgresVNextStore
            from alicebot_api import main as main_module
            from alicebot_api.config import Settings
            from alicebot_api.routers import vnext_memories, vnext_retrieval
            from tests.integration.derived_labels_postgres_support import invoke

            user = uuid4()
            url = os.environ["DATABASE_URL"]
            with user_connection(url, user) as conn:
                ContinuityStore(conn).create_user(user, f"fixture-{user}", "Fixture")
                store = PostgresVNextStore(conn)
                _, bound = create_agent_key(store, user_id=user, agent_id="bound", permission_profile="project_scoped_agent", project_scope="alpha")
                _, admin = create_agent_key(store, user_id=user, agent_id="admin", permission_profile="admin_agent")
            keys = {"bound": bound, "admin": admin}
            context = MCPRuntimeContext(database_url=url, user_id=user)
            settings = Settings(database_url=url, auth_user_id=str(user))
            main_module.get_settings = vnext_memories.get_settings = vnext_retrieval.get_settings = lambda: settings

            def events():
                with user_connection(url, user) as conn:
                    return len([e for e in PostgresVNextStore(conn).list_events() if e["event_type"] == "agent.key_escalation_rejected"])

        for label, key, action, claim in cases():
            before = events()
            fields = {"query": "example"} if action == "read" else {"title": "Example", "canonical_text": "An ordinary project note.", "domain": "project", "sensitivity": "internal"}
            if args.door == "http":
                path = "/v0/vnext/context-packs" if action == "read" else "/v0/vnext/memories/commit"
                status, result, _ = invoke("POST", path, user_id=user, key=keys[key], payload={"user_id": str(user), **fields, **claim})
                answer = str(status) + ("/" + result["status"] if "status" in result else "")
            else:
                os.environ["ALICE_AGENT_API_KEY"] = keys[key]
                name = "alice_recall" if action == "read" else "alice_memory_commit"
                failed, result = wire(context, name, {**fields, **claim})
                answer = result.get("error", {}).get("code") if failed else result.get("status", "answered")
            print(label, answer, events() - before, flush=True)

        os.environ.pop("ALICE_AGENT_API_KEY", None)
        fields = {"query": "example", "agent_id": "fixture", "permission_profile": "missing-profile"}
        if args.door == "sqlite-mcp":
            keyless = Vault(Path(directory) / "keyless.db")
            context = MCPRuntimeContext(database_url=sqlite_url_for_path(keyless.path), user_id=keyless.user)
        else:
            user = uuid4()
            with user_connection(url, user) as conn:
                ContinuityStore(conn).create_user(user, f"fixture-{user}", "Fixture")
            context = MCPRuntimeContext(database_url=url, user_id=user)
            settings = Settings(database_url=url, auth_user_id=str(user))
        if args.door == "http":
            status, _, _ = invoke("POST", "/v0/vnext/context-packs", user_id=user, payload={"user_id": str(user), **fields})
            answer = str(status)
        else:
            failed, result = wire(context, "alice_recall", fields)
            answer = result.get("error", {}).get("code") if failed else "answered"
        print("keyless", answer, 0, flush=True)


if __name__ == "__main__":
    main()
