"""Real PostgreSQL acceptance races for derived labels, always in strict mode."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Thread
import time
from types import SimpleNamespace

import pytest

from alicebot_api.cli.automation import _run_vnext_artifact_review
from alicebot_api.cli.models import CLIContext
from alicebot_api.config import Settings
from alicebot_api.mcp_tools import MCPRuntimeContext, call_mcp_tool
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_queue import VNextQueueService
from alicebot_api.vnext_scheduler import _StagedSchedulerStore, VNextSchedulerService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import (
    assert_raised,
    join_thread,
    label_harness,
    today,
)


def _thread(fn):
    failures = []

    def run():
        try:
            fn()
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread, failures


def test_a_report_built_from_stale_inputs_is_floored_at_insert(label_harness, monkeypatch):
    h = label_harness
    source = h.source()
    reader = h.key("trusted_local_agent")
    with h.store() as store:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        stale = deepcopy(
            VNextBrainService(store)._load_inputs(
                BrainArtifactRequest(agent_identity=None, generated_for=today()), window_start=start, window_end=start + timedelta(days=1)
            )
        )
    assert str(source["id"]) in [str(row["id"]) for row in stale[0]]
    assert h.relabel("source", source["id"], domain="health", sensitivity="confidential")[0] == 200
    monkeypatch.setattr(VNextBrainService, "_load_inputs", lambda *_args, **_kwargs: deepcopy(stale))
    status, body, _ = h.request(
        "POST",
        "/v0/vnext/artifacts/generate/daily-brief",
        payload={"options": {"generated_for": today(), "discover_open_loops": True}},
        key=reader,
    )
    assert status == 201, body
    assert "Synthetic acceptance" in body["content_markdown"]
    # The 201 is the read made at selection; the durable row has the current floor.
    with h.store() as store:
        assert_raised(store.get_artifact(str(body["id"])))
        loops = store.list_open_loops(status=None, sensitivity_allowed=["confidential", "regulated"], limit=50)
        assert loops
        for loop in loops:
            assert_raised(loop)
            assert "project_floor" in loop["metadata_json"]
    assert h.request("GET", f"/v0/vnext/artifacts/{body['id']}", key=reader)[0] == 403


def test_a_relabel_waits_for_an_open_generation_and_then_labels_its_report(label_harness):
    h = label_harness
    source = h.source()
    response = []
    with h.store() as store:
        report = VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(agent_identity=None, generated_for=today(), discover_open_loops=False)
        )
        thread, failures = _thread(
            lambda: response.append(h.relabel("source", source["id"], domain="health", sensitivity="confidential"))
        )
        try:
            time.sleep(0.5)
            assert thread.is_alive(), response
            rows = h.wait_exclusive()
            assert any(row["mode"] == "ShareLock" and row["granted"] for row in rows)
        finally:
            # Leaving the context commits the uncommitted report before joining A.
            pass
    join_thread(thread, failures)
    assert response[0][0] == 200, response
    with h.store() as store:
        assert_raised(store.get_artifact(str(report["id"])))


def test_a_relabel_behind_a_slow_provider_call_answers_retryable_and_changes_nothing(label_harness, monkeypatch):
    h = label_harness
    source = h.source()
    h.memory(source=source)
    before = h.snapshot()
    provider_entered, release = Event(), Event()
    generated = []

    def provider(self, **kwargs):
        provider_entered.set()
        assert release.wait(8), "provider release deadline exceeded"
        return SimpleNamespace(
            content_markdown=kwargs["deterministic_markdown"], prompt_hash="synthetic", model_info={}, metadata={}
        )

    monkeypatch.setattr(VNextBrainService, "_model_backed_artifact", provider)

    def generate():
        with h.store() as store:
            generated.append(
                VNextBrainService(store).generate_daily_brief(
                    BrainArtifactRequest(agent_identity=None, generated_for=today(), generation_mode="model_backed")
                )
            )

    thread, failures = _thread(generate)
    try:
        assert provider_entered.wait(2)
        assert any(row["mode"] == "ShareLock" and row["granted"] for row in h.label_locks())
        result = []
        started = time.monotonic()
        relabel_thread, relabel_failures = _thread(
            lambda: result.append(h.relabel("source", source["id"], domain="health", sensitivity="confidential"))
        )
        relabel_thread.join(4.0)
        assert not relabel_thread.is_alive(), "relabel exceeded 3 s bound plus 1 s"
        assert not relabel_failures, relabel_failures
        assert time.monotonic() - started < 4.0
        status, body, headers = result[0]
        assert status == 503, body
        assert headers[b"retry-after"] == b"2"
        assert "nothing was changed" in body["detail"]
        assert h.snapshot() == before
    finally:
        release.set()
        join_thread(thread, failures)
    assert h.relabel("source", source["id"], domain="health", sensitivity="confidential")[0] == 200
    with h.store() as store:
        assert_raised(store.get_artifact(str(generated[0]["id"])))
        loops = store.list_open_loops(
            status=None, sensitivity_allowed=["public", "private", "confidential", "regulated", "unknown"]
        )
        assert loops, "model-backed generation must write its candidate before the provider"
        for loop in loops:
            assert_raised(loop)


def test_a_scheduler_plan_staged_before_a_relabel_is_floored_at_publish(label_harness):
    h = label_harness
    source = h.source()
    with h.store() as store:
        staged = _StagedSchedulerStore(store)
        report = VNextBrainService(staged).generate_daily_brief(
            BrainArtifactRequest(agent_identity=None, generated_for=today(), discover_open_loops=False)
        )
        plan = staged.plan(report)
    assert h.relabel("source", source["id"], domain="health", sensitivity="confidential")[0] == 200
    with h.store() as store:
        published = plan.publish(store)
        assert_raised(published)
        assert any(row["mode"] == "ShareLock" and row["granted"] for row in h.label_locks())
    with h.store() as store:
        assert_raised(store.get_artifact(str(published["id"])))


def test_scheduler_direct_create_takes_the_shared_publish_lock(label_harness):
    h = label_harness
    source = h.source()
    with h.store() as store:
        staged = _StagedSchedulerStore(store)
        artifact = staged.create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Synthetic staged report",
                "content_markdown": "Synthetic staged report",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"workflow": "daily_brief", "source_refs": [str(source["id"])]},
            }
        )
        plan = staged.plan(artifact)
    assert h.relabel("source", source["id"], domain="health", sensitivity="confidential")[0] == 200
    with h.store() as store:
        assert_raised(plan.publish(store))
        assert any(row["mode"] == "ShareLock" and row["granted"] for row in h.label_locks())


def test_a_staged_staleness_mark_cannot_undo_a_relabel(label_harness):
    h = label_harness
    alpha, beta = "prj_" + "a" * 16, "prj_" + "b" * 16
    memory = h.memory(scope=(alpha,))
    with h.store() as store:
        staged = _StagedSchedulerStore(store)
        marked = VNextSchedulerService(staged)._mark_memory_stale(
            memory, reason="synthetic", note="Synthetic mark", metadata={}
        )
        plan = staged.plan(marked)
    assert h.relabel("memory", memory["id"], domain="health", sensitivity="confidential")[0] == 200
    assert (
        h.request(
            "POST",
            f"/v0/vnext/memories/{memory['id']}/review",
            payload={"action": "assign_project", "project_id": beta},
        )[0]
        == 200
    )
    with h.store() as store:
        before = store.get_memory(str(memory["id"]))
        plan.publish(store)
        after = store.get_memory(str(memory["id"]))
        assert_raised(after)
        assert after["metadata_json"]["project_scope"] == [beta]
        assert after["metadata_json"].get("project_floor") == before["metadata_json"].get("project_floor")
        assert "staleness" in after["metadata_json"]


def test_an_update_that_omits_a_marker_keeps_it(label_harness):
    h = label_harness
    original = h.memory()
    derived = h.memory(parents=(original,))
    with h.store() as store:
        before = store.get_memory(str(derived["id"]))
        after = store.update_memory(
            memory_id=str(derived["id"]), patch={"metadata_json": {"staleness": {"note": "synthetic"}}}
        )
        for marker in ("consolidation", "derived_from"):
            if marker in before["metadata_json"]:
                assert after["metadata_json"][marker] == before["metadata_json"][marker]
        assert "consolidation" in after["metadata_json"]


@pytest.mark.parametrize(
    "entrypoint", ["http", "mcp", "cli", "project_accept", "project_edit", "project_reject", "direct_promote"]
)
def test_every_entry_point_that_locks_a_row_and_a_relabel_never_deadlock(label_harness, monkeypatch, entrypoint):
    h = label_harness
    original_fetch = PostgresVNextStore._fetch_optional_one
    for round_no in range(25):
        source = h.source(text=f"TODO: Synthetic acceptance task {entrypoint} {round_no}")
        with h.store() as store:
            if entrypoint.startswith("project_"):
                store.lock_graph_mutation()
                store.lock_label_writes(exclusive=True)
                project = store.create_project(
                    {
                        "name": f"Synthetic acceptance {round_no}",
                        "slug": f"synthetic-{round_no}",
                        "domain": "project",
                        "sensitivity": "public",
                    }
                )
                # A real producer selects a source in the requested project.
                store.update_source(
                    source_id=str(source["id"]), patch={"metadata_json": {"project_scope": [str(project["id"])]}}
                )
                artifact = VNextProjectService(store).generate_project_update_candidate(
                    ProjectAutomationRequest(agent_identity=None, project_id=str(project["id"]))
                )
            else:
                artifact = VNextBrainService(store).generate_daily_brief(
                    BrainArtifactRequest(agent_identity=None, generated_for=today(), discover_open_loops=False)
                )
        row_locked, release = Event(), Event()

        def paused_fetch(self, *args, **kwargs):
            row = original_fetch(self, *args, **kwargs)
            if (
                row
                and str(row.get("id")) == str(artifact["id"])
                and "FOR UPDATE" in str(args).upper()
                and not row_locked.is_set()
            ):
                row_locked.set()
                assert release.wait(4), "review lock release deadline exceeded"
            return row

        monkeypatch.setattr(PostgresVNextStore, "_fetch_optional_one", paused_fetch)

        def review():
            artifact_id = str(artifact["id"])
            if entrypoint == "http":
                result = h.request("POST", f"/v0/vnext/artifacts/{artifact_id}/review", payload={"action": "promote"})
                assert result[0] == 200, result
            elif entrypoint == "mcp":
                call_mcp_tool(
                    MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id),
                    name="alice_vnext_artifact_review",
                    arguments={"artifact_id": artifact_id, "action": "promote"},
                )
            elif entrypoint == "cli":
                _run_vnext_artifact_review(
                    CLIContext(Settings(database_url=h.urls["app"]), h.urls["app"], h.user_id),
                    argparse.Namespace(artifact_id=artifact_id, action="promote"),
                )
            else:
                with h.store() as store:
                    if entrypoint.startswith("project_"):
                        VNextProjectService(store, defer_embeddings=True).review_project_update(
                            artifact_id=artifact_id,
                            action=entrypoint.split("_", 1)[1],
                            edited_current_state="Synthetic edited state" if entrypoint == "project_edit" else None,
                        )
                    else:
                        VNextQueueService(store, defer_embeddings=True)._promote_artifact(
                            artifact_id=artifact_id,
                            actor_type="user",
                            actor_id=str(h.user_id),
                            trace_id=None,
                            run_id=None,
                        )

        reviewer, review_failures = _thread(review)
        result = []
        relabeller = None
        try:
            assert row_locked.wait(2), (entrypoint, round_no, review_failures)
            relabeller, relabel_failures = _thread(
                lambda: result.append(h.relabel("source", source["id"], domain="health", sensitivity="confidential"))
            )
            h.wait_relabel()
        finally:
            release.set()
            join_thread(reviewer, review_failures)
            if relabeller is not None:
                join_thread(relabeller, relabel_failures)
            monkeypatch.setattr(PostgresVNextStore, "_fetch_optional_one", original_fetch)
        assert result[0][0] == 200, (entrypoint, round_no, result)
        with h.store() as store:
            assert_raised(store.get_artifact(str(artifact["id"])))
            from alicebot_api.vnext_label_writes import walk_dependants

            for row in walk_dependants(store, [str(source["id"])]):
                assert_raised(row)


def test_two_relabels_with_a_shared_dependant_do_not_deadlock(label_harness):
    h = label_harness
    first, second = h.memory(), h.memory()
    shared = h.memory(parents=(first, second))
    barrier = Barrier(2)
    results = []

    def change(memory):
        barrier.wait(2)
        results.append(h.relabel("memory", memory["id"], domain="health", sensitivity="confidential"))

    a, af = _thread(lambda: change(first))
    b, bf = _thread(lambda: change(second))
    join_thread(a, af)
    join_thread(b, bf)
    assert [row[0] for row in results] == [200, 200], results
    with h.store() as store:
        assert_raised(store.get_memory(str(shared["id"])))


@pytest.mark.parametrize("method", ["get_artifact_for_update", "get_memory_for_update", "get_project_for_update"])
def test_each_row_locker_holds_the_shared_label_lock(label_harness, method):
    h = label_harness
    memory = h.memory()
    with h.store() as store:
        project = store.create_project({"name": "Synthetic locker", "slug": "synthetic-locker"})
        artifact = VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(agent_identity=None, generated_for=today(), discover_open_loops=False)
        )
    row_id = {
        "get_artifact_for_update": artifact["id"],
        "get_memory_for_update": memory["id"],
        "get_project_for_update": project["id"],
    }[method]
    with h.store() as store:
        getattr(store, method)(str(row_id))
        assert any(row["mode"] == "ShareLock" and row["granted"] for row in h.label_locks()), method


@pytest.mark.parametrize(
    "method", ["create_memory", "create_open_loop", "create_artifact", "upsert_artifact_by_workflow_digest"]
)
def test_each_postgres_creator_floors_a_stale_source_copy(label_harness, method):
    from uuid import uuid4

    h = label_harness
    source = h.source()
    assert h.relabel("source", source["id"], domain="health", sensitivity="confidential")[0] == 200
    with h.store() as store:
        metadata = {"source_id": str(source["id"])}
        if method == "create_memory":
            row = store.create_memory(
                {
                    "memory_key": str(uuid4()),
                    "canonical_text": "Synthetic copy",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": metadata,
                }
            )
        elif method == "create_open_loop":
            row = store.create_open_loop(
                {
                    "title": "Synthetic copy",
                    "source_id": str(source["id"]),
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {**metadata, "discovered_by": "vnext_daily_brief"},
                }
            )
        else:
            payload = {
                "artifact_type": "daily_brief",
                "title": "Synthetic copy",
                "content_markdown": "Synthetic copy",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"workflow": "daily_brief", "source_refs": [str(source["id"])]},
            }
            row = (
                store.create_artifact(payload)
                if method == "create_artifact"
                else store.upsert_artifact_by_workflow_digest(payload, workflow="daily_brief", digest=str(uuid4()))
            )
        assert_raised(row)
