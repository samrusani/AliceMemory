"""Authenticated producer runs exclude each unreadable input before printing it."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.routers import vnext_projects, vnext_retrieval, vnext_review
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_consolidation import MemoryConsolidationRequest, VNextConsolidationService, _clustering_options
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_queue import VNextQueueService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_memory_mutations_api import invoke_request

PRODUCERS = ("daily", "weekly", "connections", "contradictions", "open_loop_review", "project_update",
             "consolidation", "staleness")


def wire_database(monkeypatch, app_url):
    for module in (main_module, vnext_projects, vnext_retrieval, vnext_review):
        monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=app_url))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")


def seed_grid(app_url):
    user_id, alpha, beta = uuid4(), str(uuid4()), str(uuid4())
    rows = []
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"producer-{user_id}@example.invalid", "Producer")
        store = PostgresVNextStore(conn)
        store.create_project({"id": alpha, "name": "Atlas", "slug": "atlas", "domain": "project", "sensitivity": "public"})
        store.create_project({"id": beta, "name": "Beta", "slug": "beta", "domain": "project", "sensitivity": "public"})
        for label, scope in (("alpha", [alpha]), ("beta", [beta]), ("shared", [alpha, beta]), ("global", [])):
            marker = f"SENTINEL_{label.upper()}"
            source = store.create_source({"source_type": "manual_text", "title": marker + " source Atlas",
                "content_hash": str(uuid4()), "captured_at": "2026-10-05T09:00:00Z", "domain": "project", "sensitivity": "public",
                "metadata_json": {"project_scope": scope, "raw_text": f"Atlas does not prefer launch games. {marker} source text.\nTODO: Atlas launch review {marker}."}})
            rows.append((label, "sources", source))
            for index, game in enumerate(("Hollow Knight", "Stardew Valley", "Celeste")):
                text = f"Atlas played {game} for {25 + index * 30} hours. {marker} memory {index}"
                memory = store.create_memory({"memory_key": f"{label}.game.{index}", "memory_type": "episode", "title": text,
                    "canonical_text": text, "summary": text, "value": {"text": text}, "status": "active", "domain": "project",
                    "sensitivity": "public", "created_at": "2026-10-05T09:00:00Z",
                    "metadata_json": {"project_scope": scope, "session_date": f"2026-10-0{index + 1}"}})
                rows.append((label, "memories", memory))
            backing = store.create_memory({"memory_key": f"{label}.belief", "memory_type": "belief", "title": marker + " belief title",
                "canonical_text": f"Atlas prefers launch games. {marker} belief text", "status": "active", "domain": "project",
                "sensitivity": "public", "metadata_json": {"project_scope": scope}})
            belief = store.create_belief({"memory_id": str(backing["id"]), "claim": backing["canonical_text"], "confidence": .9})
            rows.extend(((label, "memories", backing), (label, "beliefs", belief)))
            loop = store.create_open_loop({"title": marker + " loop Atlas", "description": marker + " loop text",
                "status": "open", "domain": "project", "sensitivity": "public", "due_at": "2026-10-04T12:00:00Z",
                "opened_at": "2026-10-05T09:00:00Z",
                "metadata_json": {"project_scope": scope}})
            rows.append((label, "open_loops", loop))
            artifact = store.create_artifact({"artifact_type": "research_brief", "title": marker + " artifact Atlas",
                "content_markdown": marker + " artifact text Atlas launch games", "domain": "project", "sensitivity": "public",
                "metadata_json": {"project_scope": scope}})
            rows.append((label, "artifacts", artifact))
            prior = store.create_artifact({"artifact_type": "daily_brief", "title": marker + " prior Atlas",
                "content_markdown": marker + " prior report text Atlas launch games", "domain": "project", "sensitivity": "public",
                "metadata_json": with_derived_from({"workflow": "daily_brief", "project_scope": scope}, {"sources": [source]})})
            rows.append((label, "artifacts", prior))
            promoted = VNextQueueService(store).review_artifact(artifact_id=str(prior["id"]), action="promote", actor_type="user")
            rows.append((label, "memories", store.get_memory(promoted["promoted_memory_id"])))
        # Fix the read windows independently of the machine clock.
        conn.execute("UPDATE memories SET created_at='2026-10-05T09:00:00Z', updated_at='2026-10-05T09:00:00Z', first_seen_at='2026-10-05T09:00:00Z', last_seen_at='2026-10-05T09:00:00Z'")
        conn.execute("UPDATE generated_artifacts SET created_at='2026-10-05T09:00:00Z'")
        conn.execute("UPDATE open_loops SET created_at='2026-10-05T09:00:00Z'")
        _, alpha_key = create_agent_key(store, user_id=user_id, agent_id="alpha", permission_profile="admin_agent", project_scope=alpha)
        _, beta_key = create_agent_key(store, user_id=user_id, agent_id="beta", permission_profile="admin_agent", project_scope=beta)
        _, unbound = create_agent_key(store, user_id=user_id, agent_id="unbound", permission_profile="admin_agent")
    return user_id, alpha, beta, rows, alpha_key, beta_key, unbound


def generate(producer, user_id, alpha, key=None, *, project_scope=True):
    options = {"generated_for": "2026-10-05", "source_limit": 50, "memory_limit": 50, "artifact_limit": 50,
               "open_loop_limit": 50, "reference_time": "2026-10-05T12:00:00Z", "max_items": 50,
               "discover_open_loops": True, "create_candidate_memories": True}
    payload = {"user_id": str(user_id), "scope": {"projects": [alpha]} if project_scope else {}, "options": options}
    if producer in {"daily", "weekly", "connections", "contradictions"}:
        route = {"daily": "daily-brief", "weekly": "weekly-synthesis"}.get(producer, producer)
        path = "/v0/vnext/artifacts/generate/" + route
    elif producer == "project_update":
        path = "/v0/vnext/projects/update-candidates"
        payload["scope"]["project_id"] = alpha
    else:
        workflow = {"consolidation": "memory_consolidation", "staleness": "staleness_sweep"}.get(producer, producer)
        path = f"/v0/vnext/scheduler/workflows/{workflow}/run-now"
    status, body = invoke_request("POST", path, payload=payload, headers={"authorization": f"Bearer {key}"} if key else {})
    assert status == 201, (producer, status, body)
    return body.get("artifact", body), body


def assert_canonical_printed_inputs(artifact, rows):
    metadata = artifact["metadata_json"]
    record = metadata["derived_from"]
    assert record["v"] == 1
    for kind in ("sources", "memories", "open_loops", "artifacts", "beliefs"):
        assert record["counts"][kind] == len(record[kind])
    printed = json.dumps(artifact, default=str)
    any_printed = False
    for _label, kind, row in rows:
        if str(row["id"]) in printed:
            belief_alias = kind == "memories" and any(
                str(belief["memory_id"]) == str(row["id"]) and str(belief["id"]) in record["beliefs"]
                for _label, belief_kind, belief in rows if belief_kind == "beliefs"
            )
            assert str(row["id"]) in record[kind] or belief_alias, (artifact["artifact_type"], kind, row["id"])
            any_printed = True
    assert any_printed, (artifact["artifact_type"], "fixture printed no input")


@pytest.mark.parametrize("producer", PRODUCERS)
def test_a_bound_key_generates_from_admitted_inputs_only(migrated_database_urls, monkeypatch, producer):
    app_url = migrated_database_urls["app"]
    wire_database(monkeypatch, app_url)
    user_id, alpha, _beta, rows, key, _beta_key, unbound = seed_grid(app_url)
    if producer == "staleness":
        with user_connection(app_url, user_id) as conn:
            conn.execute("UPDATE memories SET valid_to='2026-10-04T12:00:00Z'")
    artifact, body = generate(producer, user_id, alpha, key)
    assert_canonical_printed_inputs(artifact, rows)
    expected_kinds = {
        "daily": ("sources", "memories", "open_loops", "artifacts"),
        "weekly": ("sources", "memories", "open_loops", "artifacts"),
        "connections": ("sources", "memories"),
        "contradictions": ("sources", "beliefs"),
        "open_loop_review": ("open_loops",),
        "project_update": ("sources", "memories"),
        "consolidation": ("memories", "artifacts"),
        "staleness": ("memories",),
    }
    record = artifact["metadata_json"]["derived_from"]
    for kind in expected_kinds[producer]:
        assert any(str(row["id"]) in record[kind] for label, row_kind, row in rows
                   if label == "alpha" and row_kind == kind), (producer, "missing positive input kind", kind, record)
    artifact_id = str(artifact["id"])
    surfaces = [body]
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        surfaces.extend((store.get_artifact(artifact_id), store.list_events(target_type="artifact", target_id=artifact_id)))
        if producer == "staleness":
            for label, kind, row in rows:
                if label == "shared" and kind == "memories":
                    assert store.get_memory(str(row["id"]))["status"] == "active"
    for path in (f"/v0/vnext/artifacts/{artifact_id}", f"/v0/vnext/traces/artifacts/{artifact_id}"):
        status, surface = invoke_request("GET", path, query_params={"user_id": str(user_id)}, headers={"authorization": f"Bearer {key}"})
        assert status == 200, (producer, status, surface)
        surfaces.append(surface)
    # Project updates have a coupled candidate lifecycle, exercised by their review adapter.
    review_path = (f"/v0/vnext/projects/update-candidates/{artifact_id}/review" if producer == "project_update"
                   else f"/v0/vnext/artifacts/{artifact_id}/review")
    status, promoted = invoke_request("POST", review_path, payload={"user_id": str(user_id), "action": "accept" if producer == "project_update" else "promote"}, headers={"authorization": f"Bearer {unbound}"})
    assert status == 200, (producer, status, promoted)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        memory_id = (artifact["metadata_json"]["candidate_memory_id"] if producer == "project_update"
                     else promoted["promoted_memory_id"])
        surfaces.append(store.get_memory(memory_id))
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    recalled = call_mcp_tool(context, name="alice_recall", arguments={"query": "Atlas", "limit": 50})
    def copies(value):
        if isinstance(value, dict):
            if str(value.get("id")) == str(memory_id):
                return [value]
            return [row for child in value.values() for row in copies(child)]
        return [row for child in value for row in copies(child)] if isinstance(value, list) else []
    recalled_copies = copies(recalled)
    assert recalled_copies, (producer, "promoted copy was not recalled", recalled)
    surfaces.extend(recalled_copies)
    surfaces.append(call_mcp_tool(context, name="alice_explain", arguments={"memory_id": memory_id}))
    text = json.dumps(surfaces, default=str)
    assert any(str(row["id"]) in text for label, _kind, row in rows if label == "alpha")
    for label, _kind, row in rows:
        if label != "alpha":
            assert str(row["id"]) not in text, (producer, label, row["id"])
            for field in ("title", "canonical_text", "claim", "description"):
                if row.get(field):
                    assert str(row[field]) not in text, (producer, label, field)
            assert f"SENTINEL_{label.upper()}" not in text


@pytest.mark.parametrize("producer", PRODUCERS)
def test_input_selection_uses_effective_labels_including_the_owner_default_ceiling(migrated_database_urls, monkeypatch, producer):
    app_url = migrated_database_urls["app"]
    wire_database(monkeypatch, app_url)
    user_id, alpha, _beta, rows, _key, _beta_key, _unbound = seed_grid(app_url)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = next(row for label, kind, row in rows if label == "alpha" and kind == "sources")
        copy = store.create_memory({"memory_key": "stale.source.copy", "canonical_text": "Atlas played Hollow Knight for 25 hours. STALE_SOURCE_COPY Atlas secret",
            "title": "STALE_SOURCE_COPY", "status": "active", "domain": "project", "sensitivity": "public",
            "metadata_json": {"source_id": str(source["id"]), "project_scope": [alpha]}})
        promoted = next(row for label, kind, row in rows if label == "alpha" and kind == "memories"
                        and row["metadata_json"].get("source_artifact_id"))
        prior_id = promoted["metadata_json"]["source_artifact_id"]
        derived_loop = store.create_open_loop({"title": "STALE_SOURCE_LOOP Atlas", "description": "STALE_SOURCE_LOOP secret",
            "source_id": str(source["id"]), "status": "open", "domain": "project", "sensitivity": "public",
            "opened_at": "2026-10-05T09:00:00Z",
            "due_at": "2026-10-04T12:00:00Z", "metadata_json": {"project_scope": [alpha],
                "discovered_by": "vnext_daily_brief", "source_id": str(source["id"])}})
        conn.execute("UPDATE sources SET sensitivity='confidential' WHERE id=%s", (source["id"],))
        conn.execute("UPDATE memories SET created_at='2026-10-05T09:00:00Z', updated_at='2026-10-05T09:00:00Z', first_seen_at='2026-10-05T09:00:00Z', last_seen_at='2026-10-05T09:00:00Z' WHERE id=%s", (copy["id"],))
        if producer == "staleness":
            conn.execute("UPDATE memories SET valid_to='2026-10-04T12:00:00Z'")
        assert store.get_memory(str(copy["id"]))["sensitivity"] == "public"
        assert store.get_memory(str(promoted["id"]))["sensitivity"] == "public"
        _, trusted = create_agent_key(store, user_id=user_id, agent_id="trusted-alpha", permission_profile="trusted_local_agent", project_scope=alpha)
        owner = VNextBrainService(store).generate_daily_brief(BrainArtifactRequest(generated_for="2026-10-05",
            source_limit=50, memory_limit=50, artifact_limit=50, open_loop_limit=50, discover_open_loops=False,
            create_candidate_memories=False))
    bound, _ = generate(producer, user_id, alpha, trusted)
    for report in (owner, bound):
        text = json.dumps(report, default=str)
        assert str(copy["id"]) not in text
        assert copy["canonical_text"] not in text
        assert str(promoted["id"]) not in text
        assert str(prior_id) not in text
        assert str(source["id"]) not in text
        assert str(derived_loop["id"]) not in text
        assert "STALE_SOURCE_LOOP" not in text
        assert "SENTINEL_ALPHA prior report text" not in text
        assert any(str(row["id"]) in text for label, kind, row in rows if label == "alpha" and
                   (kind == "open_loops" or (kind == "memories" and row["memory_type"] == "episode"))), (producer, report)


def test_the_owner_keeps_the_cross_project_brief_and_bound_keys_cannot_read_it(migrated_database_urls, monkeypatch):
    app_url = migrated_database_urls["app"]
    wire_database(monkeypatch, app_url)
    user_id, alpha, beta, rows, alpha_key, beta_key, unbound = seed_grid(app_url)
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        owner = VNextBrainService(store).generate_daily_brief(BrainArtifactRequest(generated_for="2026-10-05",
            source_limit=50, memory_limit=50, artifact_limit=50, open_loop_limit=50, discover_open_loops=False,
            create_candidate_memories=False))
    text = json.dumps(owner, default=str)
    assert all(f"SENTINEL_{label.upper()}" in text for label in ("alpha", "beta", "shared", "global"))
    assert owner["metadata_json"]["project_scope"] == []
    assert set(owner["metadata_json"]["project_floor"]) == {alpha, beta}
    artifact_id = str(owner["id"])
    for key in (alpha_key, beta_key, unbound):
        for path in (f"/v0/vnext/artifacts/{artifact_id}", f"/v0/vnext/traces/artifacts/{artifact_id}"):
            status, body = invoke_request("GET", path, query_params={"user_id": str(user_id)}, headers={"authorization": f"Bearer {key}"})
            assert status == (200 if key == unbound else 403), (status, body)
    status, promoted = invoke_request("POST", f"/v0/vnext/artifacts/{artifact_id}/review",
                                     payload={"user_id": str(user_id), "action": "promote"}, headers={"authorization": f"Bearer {unbound}"})
    assert status == 200, promoted
    memory_id = promoted["promoted_memory_id"]
    with user_connection(app_url, user_id) as conn:
        copy = PostgresVNextStore(conn).get_memory(memory_id)
        assert copy["metadata_json"]["project_scope"] == []
        assert set(copy["metadata_json"]["project_floor"]) == {alpha, beta}
    for key in (alpha_key, beta_key, unbound):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        recalled = call_mcp_tool(MCPRuntimeContext(database_url=app_url, user_id=user_id), name="alice_recall",
                                 arguments={"query": "Atlas", "limit": 50})
        assert (memory_id in json.dumps(recalled, default=str)) == (key == unbound)


def test_consolidation_admits_effective_memory_labels_before_the_embedding_provider(migrated_database_urls, monkeypatch):
    app_url = migrated_database_urls["app"]
    user_id, alpha, _beta, rows, _alpha_key, _beta_key, _unbound = seed_grid(app_url)
    printed = []

    class RecordingProvider:
        def embed_batch(self, texts):
            printed.extend(texts)
            return [[1.0, 0.0, 0.0] for _ in texts]

    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        source = next(row for label, kind, row in rows if label == "alpha" and kind == "sources")
        store.create_memory({"memory_key": "provider.stale.copy", "canonical_text": "PROVIDER_SECRET Atlas played Hollow Knight for 25 hours",
            "title": "PROVIDER_SECRET", "status": "active", "domain": "project", "sensitivity": "public",
            "metadata_json": {"source_id": str(source["id"]), "project_scope": [alpha]}})
        conn.execute("UPDATE sources SET sensitivity='confidential' WHERE id=%s", (source["id"],))
        # Synthetic vectors are local. Presence is forced for all selected rows so selection reaches the provider.
        monkeypatch.setattr(store, "list_memory_ids_with_embeddings", lambda ids: set(ids))
        service = VNextConsolidationService(store, embedding_provider=RecordingProvider())
        service._cluster_memories(domains=None, sensitivity=["public", "internal", "private", "unknown"],
            projects=(alpha,), all_of=(alpha,), options=_clustering_options(MemoryConsolidationRequest()))
    text = json.dumps(printed)
    assert printed
    assert "SENTINEL_ALPHA memory" in text
    assert "PROVIDER_SECRET" not in text
    assert "SENTINEL_ALPHA prior report text" not in text
