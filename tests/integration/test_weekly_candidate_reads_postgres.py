"""An approved weekly candidate v0.20.0 wrote stays in the owner's default reads after the upgrade.

The vault is migrated to v0.20.0's head (``20260721_0094``), the memory and the weekly report are
written the way that version wrote them, the owner approves the memory, and the vault is upgraded
to head, through ``20261011_0097``. ``labels check`` stays quiet. A key whose ceiling is below a
confidential input of the report still cannot read that candidate.

SQLite cannot store the report, so the same planted row is covered in
``tests/unit/test_weekly_candidate_parent_read.py``.
"""

from __future__ import annotations

import json
import statistics
import time
from uuid import UUID, uuid4

from alembic import command

from alicebot_api.cli.labels import _run_vnext_labels_check, _run_vnext_labels_repair
from alicebot_api.cli.models import CLIContext
from alicebot_api.config import Settings, get_settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_label_closure import load_weekly_parent_artifacts
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import database_urls  # noqa: F401

TOKEN = "postgresweeklytoken"
SECRET_TOKEN = "postgressecrettoken"
CONTROL = "postgrescontroltoken"
EVERY = ["public", "internal", "private", "confidential", "highly_sensitive", "sacred", "regulated", "unknown"]


def _store(conn) -> PostgresVNextStore:
    return PostgresVNextStore(conn)


def _summary(source_id: str) -> dict[str, object]:
    return {
        "source_ids": [source_id],
        "memory_ids": [],
        "open_loop_ids": [],
        "artifact_ids": [],
        "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0},
    }


def _candidate(token: str) -> dict[str, object]:
    return {
        "memory_type": "artifact_summary",
        "memory_key": "weekly_synthesis." + uuid4().hex,
        "value": {"insight": f"The weekly pattern names {token}."},
        "status": "candidate",
        "confidence": 0.6,
        "canonical_text": f"The weekly pattern names {token}.",
        "summary": f"The weekly pattern names {token}.",
        "title": "Weekly candidate",
        "domain": "project",
        "sensitivity": "public",
        "metadata_json": {
            "candidate": True,
            "discovered_by": "vnext_weekly_synthesis",
            "generated_by": "system",
            "project_scope": [],
            "workflow_digest": uuid4().hex,
        },
    }


def _report(source_id: str, candidate_id: str) -> dict[str, object]:
    return {
        "artifact_type": "weekly_synthesis",
        "title": "Weekly Synthesis",
        "content_markdown": "Weekly report.",
        "status": "needs_review",
        "domain": "project",
        "sensitivity": "public",
        "generated_by": "vnext_weekly_synthesis",
        "metadata_json": {
            "workflow": "weekly_synthesis",
            "generated_by": "system",
            "project_scope": [],
            "source_refs": [f"source:{source_id}"],
            "input_summary": _summary(source_id),
            "candidate_memory_ids": [candidate_id],
            "workflow_digest": uuid4().hex,
        },
    }


def _recall(app_url: str, user_id, query: str, *, identity=None, sensitivity=None) -> dict:
    context = MCPRuntimeContext(
        database_url=app_url,
        user_id=user_id,
        agent_identity=identity,
        agent_identity_resolved=identity is not None,
    )
    arguments: dict[str, object] = {"query": query, "limit": 10}
    if sensitivity is not None:
        arguments["sensitivity_allowed"] = sensitivity
    return call_mcp_tool(context, name="alice_recall", arguments=arguments)


def test_an_approved_v020_weekly_candidate_survives_the_upgrade_on_postgres(database_urls, monkeypatch) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "20260721_0094")
    user_id = uuid4()
    with user_connection(database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        store = _store(conn)
        public_source = store.create_source(
            {
                "source_type": "note",
                "title": "Public note",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"raw_text": "A public source."},
            }
        )
        secret_source = store.create_source(
            {
                "source_type": "note",
                "title": "Confidential note",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "project",
                "sensitivity": "confidential",
                "metadata_json": {"raw_text": "A confidential source."},
            }
        )
        control = store.create_memory(
            {
                "memory_key": "control." + uuid4().hex[:8],
                "memory_type": "semantic",
                "canonical_text": f"The control note mentions {CONTROL}.",
                "title": "Control",
                "summary": CONTROL,
                "status": "active",
                "domain": "project",
                "sensitivity": "public",
                "value": {"text": CONTROL},
                "metadata_json": {},
            },
            actor_type="user",
        )
        with without_insert_floor():
            weekly = store.create_memory(_candidate(TOKEN), actor_type="system")
            hidden = store.create_memory(_candidate(SECRET_TOKEN), actor_type="system")
            store.create_artifact(_report(str(public_source["id"]), str(weekly["id"])), actor_type="system")
            store.create_artifact(_report(str(secret_source["id"]), str(hidden["id"])), actor_type="system")
    settings = Settings(database_url=database_urls["app"])
    get_settings.cache_clear()
    import alicebot_api.routers.vnext_memories as review_router

    monkeypatch.setattr(review_router, "get_settings", lambda: settings)
    for memory_id in (weekly["id"], hidden["id"]):
        approved = review_router.review_vnext_memory(
            UUID(str(memory_id)),
            review_router.VNextMemoryReviewRequest(user_id=user_id, action="accept"),
            authorization=None,
        )
        assert approved.status_code == 200
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")

    with user_connection(database_urls["app"], user_id) as conn:
        weekly_row = conn.execute(
            "SELECT status, sensitivity, metadata_json FROM memories WHERE id = %s",
            (str(weekly["id"]),),
        ).fetchone()
        hidden_row = conn.execute(
            "SELECT status, sensitivity, metadata_json FROM memories WHERE id = %s",
            (str(hidden["id"]),),
        ).fetchone()
    weekly_meta = weekly_row["metadata_json"]
    hidden_meta = hidden_row["metadata_json"]
    assert weekly_row["status"] == "active"
    assert weekly_row["sensitivity"] == "public"
    assert "input_summary" not in weekly_meta
    assert "derived_from" not in weekly_meta
    assert hidden_row["sensitivity"] == "confidential"

    owner = _recall(database_urls["app"], user_id, TOKEN)
    assert owner["count"] == 1
    assert owner["results"][0]["id"] == str(weekly["id"])
    pack = call_mcp_tool(
        MCPRuntimeContext(database_url=database_urls["app"], user_id=user_id),
        name="alice_context_pack",
        arguments={"query": TOKEN},
    )
    assert str(weekly["id"]) in json.dumps(pack)
    resume = call_mcp_tool(
        MCPRuntimeContext(database_url=database_urls["app"], user_id=user_id),
        name="alice_resume",
        arguments={},
    )
    assert str(weekly["id"]) in json.dumps(resume)
    assert _recall(database_urls["app"], user_id, CONTROL)["count"] == 1
    assert [row["id"] for row in _recall(database_urls["app"], user_id, SECRET_TOKEN)["results"]] == []
    seen = _recall(database_urls["app"], user_id, SECRET_TOKEN, sensitivity=EVERY)
    assert [row["id"] for row in seen["results"]] == [str(hidden["id"])]

    ctx = CLIContext(settings=settings, database_url=database_urls["app"], user_id=user_id)
    assert _run_vnext_labels_check(ctx, None) == "below_inputs 0"
    assert _run_vnext_labels_repair(ctx, None) == "labels repair updated 0"
    assert _recall(database_urls["app"], user_id, TOKEN)["count"] == 1

    with user_connection(database_urls["app"], user_id) as conn:
        store = _store(conn)
        _record, raw = create_agent_key(
            store,
            user_id=user_id,
            agent_id="trusted-" + uuid4().hex[:8],
            permission_profile="trusted_local_agent",
        )
        trusted = resolve_agent_identity(store, user_id=user_id, raw_key=raw, payload={})
        _bound_record, bound_raw = create_agent_key(
            store,
            user_id=user_id,
            agent_id="trusted-bound-" + uuid4().hex[:8],
            permission_profile="trusted_local_agent",
            project_scope=str(uuid4()),
        )
        bound = resolve_agent_identity(store, user_id=user_id, raw_key=bound_raw, payload={})
        hidden_loaded = store.get_memory(str(hidden["id"]))
        with label_read_scope(store):
            effective = LabelGuard.for_filters(store, (), ("public", "internal", "private", "unknown"), ()).effective_row(
                "memory", hidden_loaded
            )
    assert effective["sensitivity"] == "confidential"
    assert effective["unverified"] is False
    assert _recall(database_urls["app"], user_id, SECRET_TOKEN, identity=trusted)["count"] == 0
    assert _recall(database_urls["app"], user_id, TOKEN, identity=trusted)["count"] == 1
    # A bound key whose project is not this memory's project is a different door. The ceiling is what
    # refuses the confidential candidate; the public candidate is global, so this key does not receive it.
    assert bound.project_scope_locked
    assert _recall(database_urls["app"], user_id, SECRET_TOKEN, identity=bound)["count"] == 0

    with user_connection(database_urls["app"], user_id) as conn:
        conn.execute(
            """
            INSERT INTO generated_artifacts (
                id, user_id, artifact_type, title, content_markdown, status, domain, sensitivity,
                generated_by, metadata_json
            )
            SELECT gen_random_uuid(), %s, 'draft', 'filler', 'x', 'needs_review', 'project', 'public',
                   'system', '{}'::jsonb
            FROM generate_series(1, 10000)
            """,
            (user_id,),
        )
        store = _store(conn)
        named = []
        missing = []
        for _ in range(40):
            started = time.perf_counter()
            found = load_weekly_parent_artifacts(store, str(weekly["id"]))
            named.append(time.perf_counter() - started)
            started = time.perf_counter()
            absent = load_weekly_parent_artifacts(store, str(uuid4()))
            missing.append(time.perf_counter() - started)
        assert any(str(row["id"]) for row in found)
        assert absent == []
    named_median = statistics.median(named)
    missing_median = statistics.median(missing)
    assert named_median < 0.5
    assert missing_median < 0.5
    # Recorded for the pull request. A memory no report names still pays one scan of the artifact table.
    print(f"weekly parent lookup median named={named_median:.4f}s missing={missing_median:.4f}s over 10000 artifacts")
