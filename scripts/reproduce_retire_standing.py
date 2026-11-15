"""Print the lifecycle profile matrix on SQLite or PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from alicebot_api.mcp_server import MCPServer
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key

PROFILES = ("read_only_agent", "memory_proposal_agent", "project_scoped_agent", "trusted_local_agent", "admin_agent")
LESSON = {"applies_when": "reviewing a change", "what_happened": "A change was hidden.", "do_instead": "Inspect both sides."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="An isolated PostgreSQL database with migrations applied")
    parser.add_argument("--http", action="store_true", help="Use the PostgreSQL HTTP routes for retirement")
    parser.add_argument("--skip-lessons", action="store_true", help="For a release before lessons existed")
    args = parser.parse_args()
    if args.http and not args.database_url:
        parser.error("HTTP routes require PostgreSQL")
    logging.disable(logging.CRITICAL)
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    with tempfile.TemporaryDirectory(prefix="retire-probe-", dir=Path.cwd()) as directory:
        os.environ["ALICE_PROJECT_DIR"] = directory
        user = uuid4()
        if args.database_url:
            from alicebot_api.db import user_connection
            from alicebot_api.store import ContinuityStore
            from alicebot_api.vnext_store import PostgresVNextStore

            url = args.database_url
            with user_connection(url, user) as conn:
                ContinuityStore(conn).create_user(user, f"{user}@example.invalid", "Fixture")
            connect = lambda: user_connection(url, user)  # noqa: E731
            make_store = lambda conn: PostgresVNextStore(conn)  # noqa: E731
        else:
            path = Path(directory) / "vault.db"
            bootstrap_database(path, user_id=user, user_email=f"{user}@example.invalid")
            url = sqlite_url_for_path(path)
            connect = lambda: sqlite_user_connection(path, user)  # noqa: E731
            make_store = lambda conn: SQLiteVNextStore(conn, user)  # noqa: E731
        keys = {}
        with connect() as conn:
            store = make_store(conn)
            for profile in (*PROFILES, "author"):
                _, keys[profile] = create_agent_key(
                    store, user_id=user, agent_id=profile,
                    permission_profile="project_scoped_agent" if profile == "author" else profile,
                    project_scope=None if profile == "admin_agent" else "alpha",
                )
        context = MCPRuntimeContext(database_url=url, user_id=user)

        def tool(profile, name, arguments):
            os.environ["ALICE_AGENT_API_KEY"] = keys[profile]
            server = MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
            response = server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                               "params": {"name": name, "arguments": arguments}})["result"]
            return response["isError"], json.loads(response["content"][0]["text"])

        def http(profile, action, memory_id):
            from fastapi import FastAPI
            from alicebot_api.config import Settings
            from alicebot_api.routers import vnext_memories

            vnext_memories.get_settings = lambda: Settings(database_url=url)
            app = FastAPI()
            app.include_router(vnext_memories.memory_router)

            async def request():
                messages = []
                sent = False
                body = json.dumps({"user_id": str(user), "memory_id": memory_id, "reason": "A reason"}).encode()

                async def receive():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    await asyncio.Event().wait()

                async def send(message):
                    messages.append(message)

                route = "/v0/vnext/memories/" + action
                scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                         "scheme": "http", "path": route, "raw_path": route.encode(), "root_path": "", "query_string": b"",
                         "headers": [(b"authorization", ("Bearer " + keys[profile]).encode()), (b"content-type", b"application/json")],
                         "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
                await app(scope, receive, send)
                return next(item["status"] for item in messages if item["type"] == "http.response.start")

            return asyncio.run(request())

        kinds = ("active", "pending") if args.skip_lessons else ("active", "pending", "proposal", "lesson", "expired")
        print("target action", *PROFILES)
        for kind in kinds:
            for action in (("unexpire",) if kind == "expired" else ("forget", "expire", "undo")):
                outcomes = []
                for profile in PROFILES:
                    writer = "admin_agent" if kind in {"active", "lesson", "expired"} else "author" if kind == "pending" else "memory_proposal_agent"
                    fields = {"lesson": LESSON} if kind in {"proposal", "lesson", "expired"} else {
                        "title": "A procedure", "canonical_text": "An ordinary procedure.", "confidence": 0.7 if kind == "pending" else 0.95,
                    }
                    failed, created = tool(writer, "alice_memory_commit", {"domain": "project", "sensitivity": "internal", "project_scope": ["alpha"], **fields})
                    if failed:
                        raise RuntimeError(created)
                    mid = str(created["memory"]["id"])
                    if action == "unexpire":
                        failed, result = tool("admin_agent", "alice_memory_manage", {"action": "expire", "memory_id": mid, "reason": "A reason"})
                        if failed:
                            raise RuntimeError(result)
                    if args.http:
                        status = http(profile, action, mid)
                        outcomes.append(str(status))
                    else:
                        failed, result = tool(profile, "alice_memory_manage", {"action": action, "memory_id": mid, "reason": "A reason"})
                        outcomes.append(result["error"]["code"] if failed else "done")
                print(kind, action, *outcomes, flush=True)


if __name__ == "__main__":
    main()
