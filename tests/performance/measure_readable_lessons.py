"""Measure one lesson lookup at 100 and 5,000 readable rows on each store.

Run against a disposable PostgreSQL server with DATABASE_ADMIN_URL and
DATABASE_URL set to its migration and application roles. The script creates
and drops a temporary database. SQLite fixtures live in a temporary directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from uuid import UUID, uuid4

from alembic import command
from psycopg.types.json import Jsonb

from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.migrations import make_alembic_config
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import _create_role_separated_database, _drop_database
from tests.performance.lessons_brief_benchmark import seed
from tests.unit.test_lessons_read import LESSON


def measure(context: MCPRuntimeContext, key: str, backend: str, count: int) -> None:
    os.environ["ALICE_AGENT_API_KEY"] = key
    started = time.perf_counter()
    result = call_mcp_tool(context, name="alice_lessons", arguments={"task": "rebasing", "paths": ["src/a.py"]})
    elapsed = time.perf_counter() - started
    if not result["count"]:
        raise RuntimeError("the readable lesson fixture did not return a lesson")
    print(json.dumps({"backend": backend, "readable": count, "seconds": elapsed, "returned": result["count"]}))


def measure_sqlite(root: Path) -> None:
    for count in (100, 5_000):
        path = root / f"lessons-{count}.sqlite"
        user, key = seed(path, "published", rows_count=count * 4)
        with sqlite_user_connection(path, UUID(user)) as conn:
            actual = conn.execute(
                "select count(*) as n from memories where project_id = 'alpha' and memory_type = 'procedure'"
            ).fetchone()["n"]
        if actual != count:
            raise RuntimeError("unexpected SQLite lesson count")
        measure(MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=UUID(user)), key, "sqlite", count)


def measure_postgres() -> None:
    name = "alice_lesson_measure_" + uuid4().hex[:12]
    urls = _create_role_separated_database(name)
    try:
        command.upgrade(make_alembic_config(urls["admin"]), "head")
        user = uuid4()
        with user_connection(urls["app"], user) as conn:
            ContinuityStore(conn).create_user(user, f"{user}@example.invalid", "Synthetic measurement")
            store = PostgresVNextStore(conn)
            _, key = create_agent_key(
                store, user_id=user, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha",
            )
        lesson = {
            "text": "Applies when: rebasing a feature branch Do instead: Inspect both sides. What happened: A conflict hid a change.",
            "intent": "explicit_remember", "lesson": LESSON,
        }
        for added in (100, 4_900):
            with user_connection(urls["app"], user) as conn:
                conn.execute(
                    """WITH added AS (
                    INSERT INTO memories (user_id,memory_key,value,status,source_event_ids,memory_type,title,canonical_text,domain,sensitivity,metadata_json,project_id)
                    SELECT %s, 'bench-'||gen_random_uuid()::text, %s, 'active', '[]'::jsonb, 'procedure',
                    'synthetic lesson', %s, 'project', 'internal', '{"project_scope":["alpha"]}'::jsonb, 'alpha'
                    FROM generate_series(1,%s) RETURNING id,user_id,memory_key)
                    INSERT INTO memory_revisions (user_id,memory_id,sequence_no,action,memory_key,new_value,source_event_ids,candidate,revision_number,revision_type,text_after,actor_type)
                    SELECT user_id,id,1,'agentic_memory_commit',memory_key,%s,'[]'::jsonb,'{}'::jsonb,1,'created',%s,'user' FROM added""",
                    (user, Jsonb(lesson), lesson["text"], added, Jsonb(lesson), lesson["text"]),
                )
                count = conn.execute(
                    "select count(*) as n from memories where project_id = 'alpha' and memory_type = 'procedure'"
                ).fetchone()["n"]
            if count not in (100, 5_000):
                raise RuntimeError("unexpected PostgreSQL lesson count")
            measure(MCPRuntimeContext(database_url=urls["app"], user_id=user), key, "postgres", count)
    finally:
        _drop_database(name)


def main() -> None:
    for key in tuple(os.environ):
        if key.startswith(("ALICE_EMBEDDINGS_", "ALICE_SOURCE_EMBEDDINGS", "ALICE_PROJECT_SCOPING")):
            os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="lessons-lookup-") as directory:
        os.environ["ALICE_PROJECT_DIR"] = directory
        measure_sqlite(Path(directory))
        measure_postgres()


if __name__ == "__main__":
    main()
