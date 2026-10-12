"""The tools, the command and the generation routes that answer with a report judge it on the caller's limits.

``test_scheduler_run_response_admission_postgres.py`` pins the run-now and run-due routes. The same report is also the
answer of ``POST /v0/vnext/artifacts/generate/*``, of the MCP tools that run a workflow, of ``alicebot vnext scheduler
run-now`` and ``run-due``, and (as an id) of ``POST /v0/vnext/queue/process-next``. Each of them used to return the report
it made, or the one it found again under the same digest, as stored, while ``GET /v0/vnext/artifacts/{id}`` answered the
same report with HTTP 404 for the same caller. They now ask the check the artifact read applies, and a report the caller
may not read is answered as a missing artifact is: HTTP 404, the MCP error ``not_found``, ``artifact`` null in a run, an
empty ``artifact_id`` in a queue answer. The owner and an unbound admin key are answered as before.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from uuid import uuid4

import pytest

from alicebot_api.cli import main as cli_main
from alicebot_api.mcp import synthesis
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPReferenceNotFoundError, MCPRuntimeContext
from alicebot_api.vnext_agent_keys import create_agent_key, resolve_agent_identity
from alicebot_api.vnext_queue import QueueTaskRequest, VNextQueueService
from tests.integration.derived_labels_postgres_support import label_harness, today  # noqa: F401  (fixtures)
from tests.integration.test_scheduler_run_response_admission_postgres import (
    HIDDEN,
    QUOTE,
    _cluster_provider,  # noqa: F401  (autouse fixture)
    _get_artifact,
    _owner_harness,
    _project,
    _quoting_cluster,
    _raise,
    _stored_artifacts,
    _traces,
    _vault,
)

GENERATE = {
    "daily_brief": "/v0/vnext/artifacts/generate/daily-brief",
    "weekly_synthesis": "/v0/vnext/artifacts/generate/weekly-synthesis",
    "connection_report": "/v0/vnext/artifacts/generate/connections",
    "contradiction_report": "/v0/vnext/artifacts/generate/contradictions",
}
HANDLER = {
    "daily_brief": synthesis._handle_alice_generate_daily_brief,
    "weekly_synthesis": synthesis._handle_alice_generate_weekly_synthesis,
    "connection_report": synthesis._handle_alice_generate_connections,
    "contradiction_report": synthesis._handle_alice_generate_contradictions,
}


def _generate(h, workflow, key, project):
    payload = {"scope": {"projects": [project]}, "options": {"generated_for": today()}}
    return h.request("POST", GENERATE[workflow], payload=payload, key=key)


def _context(h):
    return MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)


def _tool(h, monkeypatch, name, arguments, *, key=None):
    """One tool call in process. The generation tools are on the surface that is off whenever a key is configured, so the
    caller declares its profile in the call, as a local client does."""

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    return call_mcp_tool(_context(h), name=name, arguments=arguments)


def _declared(profile, project):
    return {"agent_id": "declared-agent", "permission_profile": profile, "project_scope": [project]}


def _arguments(project, **extra):
    return {"project_scope": [project], "generated_for": today(), **extra}


# -- POST /v0/vnext/artifacts/generate/* ------------------------------------------------------------------------------


@pytest.mark.parametrize("workflow", list(GENERATE))
def test_a_generation_route_answers_a_missing_artifact_for_a_report_the_caller_may_no_longer_read(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    key = h.key("trusted_local_agent", project=project)
    status, first, _ = _generate(h, workflow, key, project)
    assert status == 201 and first["id"], first
    assert _get_artifact(h, first["id"], key)[0] == 200
    _raise(h, "generated_artifacts", [first["id"]])
    assert _get_artifact(h, first["id"], key)[0] == 404

    status, again, _ = _generate(h, workflow, key, project)

    # The route answers exactly as the read of the same report does, and echoes nothing of it.
    assert (status, again) == (404, {"detail": "vNext artifact was not found"}), again
    assert first["id"] in {row["id"] for row in _stored_artifacts(h)}


@pytest.mark.parametrize("workflow", list(GENERATE))
def test_a_generation_route_still_answers_the_report_a_caller_with_limits_may_read(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    key = h.key("trusted_local_agent", project=project)
    status, body, _ = _generate(h, workflow, key, project)
    assert status == 201 and _get_artifact(h, body["id"], key)[0] == 200
    assert _traces(body, HIDDEN) == []
    status, again, _ = _generate(h, workflow, key, project)
    assert status == 201 and again["id"] == body["id"]


@pytest.mark.parametrize("keyed", [True, False], ids=["unbound admin key", "owner"])
@pytest.mark.parametrize("workflow", list(GENERATE))
def test_a_generation_route_answers_the_owner_and_an_unbound_admin_key_with_a_raised_report(
    label_harness, migrated_database_urls, workflow, keyed
):
    h = label_harness if keyed else _owner_harness(migrated_database_urls)
    key = h.key("admin_agent") if keyed else None
    project = _vault(h)
    status, first, _ = _generate(h, workflow, key, project)
    assert status == 201, first
    _raise(h, "generated_artifacts", [first["id"]])
    status, again, _ = _generate(h, workflow, key, project)
    assert status == 201 and again["id"] == first["id"], again
    assert again["sensitivity"] == "confidential"


# -- the MCP tools ----------------------------------------------------------------------------------------------------


def _resolved(h, profile, project):
    """The identity a core tool gets from the server, resolved from a real key bound to ``project``. The four
    ``alice_generate_*`` tools advertise no identity fields, so a call declares none and runs as the owner; a server that
    resolves a key hands them the identity as the registry does for a core tool."""

    _agent_id, raw = _keyed(h, profile, project=project)
    with h.store() as store:
        return resolve_agent_identity(store, user_id=h.user_id, raw_key=raw, payload={})


def _handler(h, workflow, arguments, identity):
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id, agent_identity=identity, agent_identity_resolved=True)
    return HANDLER[workflow](context, arguments)


@pytest.mark.parametrize("workflow", list(HANDLER))
def test_a_generation_tool_answers_not_found_for_a_report_the_caller_may_no_longer_read(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    identity = _resolved(h, "trusted_local_agent", project)
    arguments = _arguments(project)
    first = _handler(h, workflow, arguments, identity)
    assert first["id"]
    _raise(h, "generated_artifacts", [first["id"]])

    with pytest.raises(MCPReferenceNotFoundError) as caught:
        _handler(h, workflow, arguments, identity)

    assert _traces({"error": str(caught.value)}, first["id"], first["title"]) == []


@pytest.mark.parametrize("workflow", list(HANDLER))
def test_a_generation_tool_answers_the_report_to_a_caller_without_limits(label_harness, workflow):
    h = label_harness
    project = _vault(h)
    first = _handler(h, workflow, _arguments(project), None)
    _raise(h, "generated_artifacts", [first["id"]])
    again = _handler(h, workflow, _arguments(project), None)
    assert again["id"] == first["id"] and again["sensitivity"] == "confidential"
    admin = _handler(h, workflow, _arguments(project), _resolved(h, "admin_agent", None))
    assert admin["id"]


@pytest.mark.parametrize("workflow", ["daily_brief", "weekly_synthesis", "connection_report", "contradiction_report",
                                      "open_loop_review", "project_update_scan", "memory_consolidation"])
def test_the_generate_artifact_tool_answers_not_found_for_a_report_the_caller_may_no_longer_read(label_harness, monkeypatch, workflow):
    h = label_harness
    project = _vault(h)
    arguments = {**_arguments(project), **_declared("trusted_local_agent", project), "workflow_type": workflow}
    first = _tool(h, monkeypatch, "alice_vnext_generate_artifact", arguments)
    artifact = first.get("artifact") or first
    artifact_id = artifact["id"]
    _raise(h, "generated_artifacts", [artifact_id])

    if workflow in {"open_loop_review", "project_update_scan", "memory_consolidation"}:
        # These run as scheduler workflows, whose answer carries a run and a null report.
        again = _tool(h, monkeypatch, "alice_vnext_generate_artifact", arguments)
        assert again["artifact"] is None and again["run"]["artifact_id"] is None, again
        assert _traces(again, artifact_id) == []
    else:
        with pytest.raises(MCPReferenceNotFoundError):
            _tool(h, monkeypatch, "alice_vnext_generate_artifact", arguments)


def test_the_run_now_tool_leaves_out_the_cluster_that_quotes_a_confidential_memory(label_harness, monkeypatch):
    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    arguments = {"workflow_type": "memory_consolidation", **_arguments(project), **_declared("project_scoped_agent", project)}

    answer = _tool(h, monkeypatch, "alice_vnext_scheduler_run_now", arguments)

    [stored] = _stored_artifacts(h, "memory_consolidation")
    assert QUOTE in json.dumps(stored["metadata_json"], default=str)
    assert answer["artifact"] is None and answer["run"]["status"] == "succeeded" and answer["run"]["artifact_id"] is None
    assert _traces(answer, QUOTE, str(secret["id"]), stored["id"], stored["title"]) == []
    # The owner's tool call, which declares no profile, is answered with the report found again.
    owner = _tool(h, monkeypatch, "alice_vnext_scheduler_run_now", {"workflow_type": "memory_consolidation", **_arguments(project)})
    assert owner["artifact"] is not None and owner["run"]["artifact_id"] == owner["artifact"]["id"]


def test_the_run_now_tool_leaves_out_the_cluster_for_the_identity_of_a_key_bound_to_a_project(label_harness):
    """The server resolves the identity of a key bound to a project and hands it to the handler, as it does for a core tool."""

    from alicebot_api.mcp.scheduler import _handle_alice_vnext_scheduler_run_now

    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    identity = _resolved(h, "project_scoped_agent", project)
    assert identity.project_scope_locked
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id, agent_identity=identity, agent_identity_resolved=True)

    answer = _handle_alice_vnext_scheduler_run_now(context, {"workflow_type": "memory_consolidation", **_arguments(project)})

    [stored] = _stored_artifacts(h, "memory_consolidation")
    assert answer["artifact"] is None and answer["run"]["artifact_id"] is None, answer
    assert _traces(answer, QUOTE, str(secret["id"]), stored["id"], stored["title"]) == []


def test_the_generate_artifact_tool_leaves_out_the_cluster_that_quotes_a_confidential_memory(label_harness, monkeypatch):
    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    arguments = {"workflow_type": "memory_consolidation", **_arguments(project), **_declared("project_scoped_agent", project)}
    answer = _tool(h, monkeypatch, "alice_vnext_generate_artifact", arguments)
    assert answer["artifact"] is None and answer["run"]["artifact_id"] is None
    assert _traces(answer, QUOTE, str(secret["id"])) == []


def test_the_run_due_tool_leaves_out_a_report_the_caller_may_no_longer_read(label_harness, monkeypatch):
    from tests.integration.test_scheduler_run_response_admission_postgres import _make_due
    from alicebot_api.vnext_scheduler import VNextSchedulerService

    h = label_harness
    _vault(h)
    with h.store() as store:
        VNextSchedulerService(store).ensure_default_workflows()
    _make_due(h, "daily_brief")
    declared = {"agent_id": "declared-agent", "permission_profile": "trusted_local_agent"}
    first = _tool(h, monkeypatch, "alice_vnext_scheduler_run_due", {**declared})
    artifact_id = first["runs"][0]["artifact"]["id"]
    _raise(h, "generated_artifacts", [artifact_id])
    _make_due(h, "daily_brief")

    again = _tool(h, monkeypatch, "alice_vnext_scheduler_run_due", {**declared})

    [answered] = again["runs"]
    assert answered["artifact"] is None and answered["run"]["artifact_id"] is None
    assert _traces(again, artifact_id) == []


# -- alicebot vnext scheduler ----------------------------------------------------------------------------------------


def _cli(h, monkeypatch, argv, *, key=None):
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli_main(["--database-url", h.urls["app"], "--user-id", str(h.user_id), *argv])
    return code, out.getvalue(), err.getvalue()


def _keyed(h, profile, *, project=None):
    agent_id = f"cli-{uuid4().hex[:8]}"
    with h.store() as store:
        _record, raw = create_agent_key(
            store, user_id=h.user_id, agent_id=agent_id, permission_profile=profile, project_scope=project
        )
    return agent_id, raw


def test_the_scheduler_command_leaves_out_the_cluster_that_quotes_a_confidential_memory(label_harness, monkeypatch):
    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    agent_id, key = _keyed(h, "project_scoped_agent", project=project)
    argv = ["vnext", "scheduler", "run-now", "memory_consolidation", "--agent-id", agent_id,
            "--permission-profile", "project_scoped_agent", "--project-scope", project, "--generated-for", today()]

    code, out, err = _cli(h, monkeypatch, argv, key=key)

    assert code == 0, (out, err)
    answer = json.loads(out)
    [stored] = _stored_artifacts(h, "memory_consolidation")
    assert answer["artifact"] is None and answer["run"]["artifact_id"] is None, answer
    assert _traces(answer, QUOTE, str(secret["id"]), stored["id"], stored["title"]) == []


def test_the_scheduler_command_answers_the_owner_with_the_report(label_harness, migrated_database_urls, monkeypatch):
    h = _owner_harness(migrated_database_urls)
    project = _project(h)
    _quoting_cluster(h, project)
    argv = ["vnext", "scheduler", "run-now", "memory_consolidation", "--generated-for", today(),
            "--sensitivity-allowed", "public", "--sensitivity-allowed", "internal", "--sensitivity-allowed", "confidential"]
    code, out, err = _cli(h, monkeypatch, argv)
    assert code == 0, (out, err)
    answer = json.loads(out)
    assert answer["artifact"] is not None and answer["run"]["artifact_id"] == answer["artifact"]["id"]
    assert QUOTE in json.dumps(answer["artifact"], default=str)


# -- POST /v0/vnext/queue/process-next -------------------------------------------------------------------------------


def _task_naming(h, memory_id, *, sensitivity="public"):
    """The owner queues a task whose allowed sources name a memory, and labels the task itself public."""

    with h.store() as store:
        return VNextQueueService(store).enqueue_task(
            QueueTaskRequest(
                title="Summarize the kiln notes",
                task_type="summarize",
                instructions="Summarize the notes.",
                allowed_sources_json=[{"memory_id": memory_id, "quote": QUOTE}],
                domain="project",
                sensitivity=sensitivity,
            )
        )


def test_the_queue_worker_does_not_name_a_result_report_the_caller_may_not_read(label_harness):
    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    task = _task_naming(h, str(secret["id"]))
    trusted = h.key("trusted_local_agent")

    status, body, _ = h.request("POST", "/v0/vnext/queue/process-next", payload={}, key=trusted)

    assert status == 200 and body["status"] == "completed" and body["task_id"] == str(task["id"]), body
    [stored] = [row for row in _stored_artifacts(h) if row["artifact_type"] != "memory_consolidation"]
    assert _get_artifact(h, stored["id"], trusted)[0] == 404
    assert body["artifact_id"] is None, body
    assert _traces(body, stored["id"], QUOTE, str(secret["id"])) == []


def test_the_queue_worker_names_the_result_to_the_owner_and_an_unbound_admin_key(label_harness):
    h = label_harness
    project = _project(h)
    secret = _quoting_cluster(h, project)
    _task_naming(h, str(secret["id"]))
    admin = h.key("admin_agent")
    status, body, _ = h.request("POST", "/v0/vnext/queue/process-next", payload={}, key=admin)
    [stored] = [row for row in _stored_artifacts(h) if row["artifact_type"] != "memory_consolidation"]
    assert status == 200 and body["artifact_id"] == stored["id"], body


def test_the_queue_worker_names_a_result_report_the_caller_may_read(label_harness):
    h = label_harness
    project = _project(h)
    readable = _quoting_cluster(h, project)
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("UPDATE memories SET sensitivity = 'public' WHERE id = %s::uuid", (str(readable["id"]),))
    _task_naming(h, str(readable["id"]))
    trusted = h.key("trusted_local_agent")
    status, body, _ = h.request("POST", "/v0/vnext/queue/process-next", payload={}, key=trusted)
    assert status == 200 and body["status"] == "completed" and body["artifact_id"], body
    assert _get_artifact(h, body["artifact_id"], trusted)[0] == 200
