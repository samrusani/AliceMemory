"""Other spellings of a weekly candidate id keep the same verdict as labels check.

The lookup used to require the exact id in a flat array. Upper case, a compact id, ``urn:uuid:``,
a scalar ``candidate_memory_ids`` and a nested list then left ``labels check`` quiet and default
recall empty. The read now uses the classifier's own normalisation.
"""
from __future__ import annotations

import json
from uuid import uuid4

from alembic import command

from alicebot_api.cli.labels import _run_vnext_labels_check
from alicebot_api.cli.models import CLIContext
from alicebot_api.config import Settings, get_settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_closure import weekly_parent_query_count
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import database_urls  # noqa: F401


def _summary(source_id: str) -> dict[str, object]:
    return {
        "source_ids": [source_id],
        "memory_ids": [],
        "open_loop_ids": [],
        "artifact_ids": [],
        "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0},
    }


def test_every_candidate_id_spelling_agrees_with_labels_check(database_urls) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    app_url = database_urls["app"]
    settings = Settings(database_url=app_url)
    get_settings.cache_clear()
    spellings = ("uppercase", "compact", "urn", "braces", "scalar", "nested")
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = PostgresVNextStore(conn)
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Public note",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"raw_text": "A public source."},
            }
        )
        source_id = str(source["id"])
        planted: dict[str, str] = {}
        with without_insert_floor():
            for name in spellings:
                token = "spell" + name + "token"
                memory = store.create_memory(
                    {
                        "memory_type": "artifact_summary",
                        "memory_key": "weekly_synthesis." + uuid4().hex,
                        "value": {"insight": token},
                        "status": "active",
                        "confidence": 0.6,
                        "canonical_text": f"The weekly pattern names {token}.",
                        "summary": token,
                        "title": "Weekly candidate",
                        "domain": "project",
                        "sensitivity": "public",
                        "metadata_json": {
                            "candidate": True,
                            "discovered_by": "vnext_weekly_synthesis",
                            "project_scope": [],
                            "workflow_digest": name,
                        },
                    },
                    actor_type="system",
                )
                memory_id = str(memory["id"])
                planted[name] = memory_id
                if name == "uppercase":
                    named: object = [memory_id.upper()]
                elif name == "compact":
                    named = [memory_id.replace("-", "")]
                elif name == "urn":
                    named = ["urn:uuid:" + memory_id]
                elif name == "braces":
                    named = ["{" + memory_id + "}"]
                elif name == "scalar":
                    named = memory_id
                else:
                    named = [[memory_id]]
                store.create_artifact(
                    {
                        "artifact_type": "weekly_synthesis",
                        "title": "Weekly Synthesis",
                        "content_markdown": "Weekly report.",
                        "status": "needs_review",
                        "domain": "project",
                        "sensitivity": "public",
                        "generated_by": "vnext_weekly_synthesis",
                        "metadata_json": {
                            "workflow": "weekly_synthesis",
                            "project_scope": [],
                            "source_refs": [f"source:{source_id}"],
                            "input_summary": _summary(source_id),
                            "candidate_memory_ids": named,
                            "workflow_digest": name,
                        },
                    },
                    actor_type="system",
                )
    ctx = CLIContext(settings=settings, database_url=app_url, user_id=user_id)
    assert _run_vnext_labels_check(ctx, None) == "below_inputs 0"
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    for name, memory_id in planted.items():
        token = "spell" + name + "token"
        found = call_mcp_tool(context, name="alice_recall", arguments={"query": token, "limit": 5})
        assert [row["id"] for row in found["results"]] == [memory_id], name


def test_a_request_with_many_weekly_memories_runs_one_parent_query(database_urls) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    app_url = database_urls["app"]
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        values = []
        for index in range(40):
            row_id = str(uuid4())
            meta = json.dumps(
                {
                    "candidate": True,
                    "discovered_by": "vnext_weekly_synthesis",
                    "project_scope": [],
                    "workflow_digest": uuid4().hex,
                    "observation_index": index,
                }
            )
            values.append((row_id, str(user_id), "budget." + row_id, "weekly filler " + str(index), meta))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,status,domain,sensitivity) "
                "VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,'{}'::jsonb,'{}'::jsonb,'active','project','public')",
                values,
            )
            cur.execute(
                """
                INSERT INTO generated_artifacts (
                    id, user_id, artifact_type, title, content_markdown, status, domain, sensitivity,
                    generated_by, metadata_json
                )
                SELECT gen_random_uuid(), %s, 'draft', 'filler', 'x', 'needs_review', 'project', 'public',
                       'system', jsonb_build_object('candidate_memory_ids', jsonb_build_array(gen_random_uuid()::text))
                FROM generate_series(1, 200)
                """,
                (user_id,),
            )
        store = PostgresVNextStore(conn)
        with label_read_scope(store):
            LabelGuard.for_filters(
                store, (), ("public", "internal", "private", "unknown"), ()
            ).readable_status_counts("memory")
            assert weekly_parent_query_count() == 1
