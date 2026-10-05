"""Propagation must finish with its original, over actual generated chains."""

from __future__ import annotations

import json

import psycopg
import pytest

from alicebot_api import vnext_label_writes
from alicebot_api.vnext_derived_domain_backfill import require_changed
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_capture import VNextCaptureService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService
from alicebot_api.vnext_queue import VNextQueueService
from tests.integration.derived_labels_postgres_support import assert_raised, label_harness, today


@pytest.mark.parametrize("failure_kind", ["database", "writer"])
def test_a_failed_propagation_rolls_the_original_back_with_it(label_harness, monkeypatch, failure_kind):
    h = label_harness
    source = h.source()
    copies = [h.memory(source=source) for _ in range(4)]
    h.memory(parents=tuple(copies))
    before = h.snapshot()
    original = vnext_label_writes.write_settled_label
    attempts = []

    def fail_third(store, **kwargs):
        attempts.append(kwargs["row_id"])
        if len(attempts) == 3:
            if failure_kind == "writer":
                require_changed(0, "memories", kwargs["row_id"])
            # A genuine database exception must abort the transaction after two writes/events.
            store.conn.execute("SELECT 1 / 0")
        return original(store, **kwargs)

    monkeypatch.setattr(vnext_label_writes, "write_settled_label", fail_third)
    try:
        status, body, _ = h.relabel("source", source["id"], domain="health", sensitivity="confidential")
    except psycopg.errors.DivisionByZero:
        # Baseline exposes the error; atomic rollback is still asserted, separately from mapping.
        status = None
    assert len(attempts) == 3
    assert h.snapshot() == before
    assert status == 409, "all database propagation failures must return the fixed whole-refusal answer"
    assert "nothing was changed" in body["detail"]


def _build_chain(h, *, source=None, project=None, label=("project", "public")):
    with h.store() as store:
        # Taking S first also verifies capture's real strict-order path when it reacquires S.
        store.lock_graph_mutation()
        if project is None:
            project = store.create_project(
                {"name": "SyntheticChain", "slug": "synthetic-chain", "domain": "project", "sensitivity": "public"}
            )
        if source is None:
            capture = VNextCaptureService(store, defer_embeddings=True).capture_text(
                "Fact: SyntheticChain acceptance is prepared.\nTODO: SyntheticChain followup",
                title="SyntheticChain acceptance",
                domain=label[0],
                sensitivity=label[1],
                project_scope=(str(project["id"]),),
            )
            assert capture.status == "imported", capture
            assert capture.candidate_memory_count == 2
            source = store.get_source(str(capture.source_id))
        copies = store.list_memories_referencing_source(source_id=str(source["id"]))
        assert len(copies) >= 2
        copies = [
            store.update_memory(memory_id=str(row["id"]), patch={"status": "accepted"}, actor_type="user")
            if row["status"] == "candidate"
            else row
            for row in copies
        ]
        brain = VNextBrainService(store)
        request = BrainArtifactRequest(
            generated_for=today(),
            projects=(str(project["id"]),),
            sensitivity_allowed=("public", "internal", "private", "confidential", "regulated", "unknown"),
        )
        daily = brain.generate_daily_brief(request)
        assert {str(row["id"]) for row in copies}.issubset(set(daily["metadata_json"]["derived_from"]["memories"]))
        weekly = brain.generate_weekly_synthesis(request)
        assert str(daily["id"]) in weekly["metadata_json"]["derived_from"]["artifacts"]
        promoted = VNextQueueService(store, defer_embeddings=True)._promote_artifact(
            artifact_id=str(weekly["id"]), actor_type="user", actor_id=str(h.user_id), trace_id=None, run_id=None
        )
        update = VNextProjectService(store, defer_embeddings=True).generate_project_update_candidate(
            ProjectAutomationRequest(project_id=str(project["id"]), sensitivity_allowed=request.sensitivity_allowed)
        )
        VNextProjectService(store, defer_embeddings=True).review_project_update(
            artifact_id=str(update["id"]), action="accept", actor_type="user"
        )
        rows = [
            *(("memory", str(row["id"])) for row in copies),
            ("artifact", str(daily["id"])),
            ("artifact", str(weekly["id"])),
            ("memory", str(promoted["promoted_memory_id"])),
            ("artifact", str(update["id"])),
            ("memory", str(update["metadata_json"]["candidate_memory_id"])),
            ("project", str(project["id"])),
        ]
        rows.extend(("memory", str(row_id)) for row_id in weekly["metadata_json"]["candidate_memory_ids"])
        loops = store.list_open_loops(status=None, sensitivity_allowed=list(request.sensitivity_allowed))
        loops = [row for row in loops if str(row.get("source_id")) == str(source["id"])]
        assert loops, "daily producer must create a candidate loop"
        rows.extend(("open_loop", str(row["id"])) for row in loops)
        assert store.get_project(str(project["id"]))["metadata_json"]["derived_from"]
        return source, project, rows


def _read_row(store, kind, row_id):
    return {
        "memory": store.get_memory,
        "artifact": store.get_artifact,
        "open_loop": store.get_open_loop,
        "project": store.get_project,
    }[kind](row_id)


def _assert_chain(h, rows, *, domain, sensitivity):
    with h.store() as store:
        for kind, row_id in rows:
            row = _read_row(store, kind, row_id)
            assert_raised(row, domain=domain, sensitivity=sensitivity)


def test_a_source_relabel_reaches_the_extracted_memories_the_loop_every_report_and_the_project_state(label_harness):
    h = label_harness
    source, project, rows = _build_chain(h)
    assert h.relabel("source", source["id"], domain="health")[0] == 200
    _assert_chain(h, rows, domain="health", sensitivity="public")
    assert h.relabel("source", source["id"], sensitivity="confidential")[0] == 200
    _assert_chain(h, rows, domain="health", sensitivity="confidential")
    # All original roles in the chain must be recreated at the raised floor.
    with h.store() as store:
        updated_source = store.get_source(str(source["id"]))
    _control_source, _control_project, control = _build_chain(h, source=updated_source, project=project)
    _assert_chain(h, control, domain="health", sensitivity="confidential")

    withheld = {row_id for _kind, row_id in rows}
    for profile in ("read_only_agent", "trusted_local_agent"):
        key = h.key(profile)
        for kind, row_id in rows:
            if kind == "artifact":
                exact = f"/v0/vnext/artifacts/{row_id}"
            elif kind == "memory":
                exact = f"/v0/vnext/memories/{row_id}/audit"
            elif kind == "open_loop":
                # The coupled review door performs the exact target admission before mutation.
                status, body, _ = h.request(
                    "POST", f"/v0/vnext/open-loops/{row_id}/review", payload={"action": "close"}, key=key
                )
                assert status == 403, (profile, kind, body)
                continue
            else:
                exact = f"/v0/vnext/projects/{row_id}/dashboard"
            status, body, _ = h.request("GET", exact, key=key)
            if kind == "project":
                # O12 operator screens redact the response rather than requiring an exact policy refusal.
                assert row_id not in json.dumps(body), (profile, kind, status, body)
            else:
                assert status == 403, (profile, kind, status, body)
        for path in ("/v0/vnext/artifacts", "/v0/vnext/projects", "/v0/vnext/workspace", "/v0/vnext/context-tree"):
            status, body, _ = h.request("GET", path, key=key)
            # Read-only keys cannot use operator routes. Their refusal must carry no row data.
            assert status == (403 if profile == "read_only_agent" else 200), (path, body)
            encoded = json.dumps(body)
            assert not withheld.intersection({row_id for row_id in withheld if row_id in encoded}), (
                profile,
                path,
                body,
            )
        status, body, _ = h.request(
            "POST",
            "/v0/vnext/context-packs",
            payload={"query": "SyntheticChain", "scope": {}, "options": {"include_sources": True}},
            key=key,
        )
        assert status == 201, body
        assert all(row_id not in json.dumps(body) for row_id in withheld)

    # A project-bound key loses the entire moved chain; provenance and a union floor survive.
    bound = h.key("admin_agent", project=str(project["id"]))
    beta = "prj_" + "b" * 16
    from alicebot_api.routers import vnext_memories as router
    from uuid import UUID

    result = router.review_vnext_source(
        UUID(str(source["id"])),
        router.VNextSourceReviewRequest(
            user_id=h.user_id, action="assign_project", project_id=beta, confirm_label_hide=True
        ),
    )
    move_status, move = result.status_code, json.loads(result.body)
    assert move_status == 200, move
    with h.store() as store:
        for kind, row_id in rows:
            row = _read_row(store, kind, row_id)
            assert_raised(row)
            assert beta in row["metadata_json"]["project_floor"]
            if kind == "artifact":
                assert h.request("GET", f"/v0/vnext/artifacts/{row_id}", key=bound)[0] == 403
