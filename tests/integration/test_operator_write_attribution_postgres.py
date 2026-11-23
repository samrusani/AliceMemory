"""Mounted PostgreSQL operator writes and artifact export boundaries."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from alicebot_api import vnext_queue
from alicebot_api.routers import vnext_projects
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_derived_labels import with_derived_from
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401


@pytest.fixture(params=["trusted_local_agent", "admin_agent", "owner", "declared"])
def caller(label_harness, request):
    profile = request.param
    if profile == "owner":
        return None, {}, ("user", None)
    if profile == "declared":
        return None, {"agent_id": "fixture-operator", "permission_profile": "admin_agent"}, ("agent", "fixture-operator")
    with label_harness.store() as store:
        _, key = create_agent_key(store, user_id=label_harness.user_id, agent_id="fixture-operator",
                                  permission_profile=profile)
    return key, {}, ("agent", "fixture-operator")


def _events(harness):
    with harness.store() as store:
        return {row["id"]: row for row in store.list_events(limit=100)}


def _new_events(harness, before):
    return [row for row_id, row in _events(harness).items() if row_id not in before]


def _expect_events(harness, before, kinds, actor):
    events = _new_events(harness, before)
    assert Counter(event["event_type"] for event in events) == Counter(kinds)
    assert {(event["actor_type"], event["actor_id"]) for event in events} == {actor}
    return events


def _expect_trace_matches_stored_events(trace, events):
    stored = {str(event["id"]): event for event in events}
    shown = {event["id"]: event for event in trace["events"] if event["id"] in stored}
    assert {event_id for event_id, event in stored.items() if event["target_type"] == "source"} <= shown.keys()
    for event_id, event in shown.items():
        for field in ("actor_type", "actor_id", "integrity_hash"):
            assert event[field] == stored[event_id][field], (event["event_type"], field)


@pytest.mark.parametrize(("write", "kinds"), [
    ("project", ("project.created",)),
    ("charter", ("brain_charter.upserted",)),
    ("review", ("source.updated", "source.reviewed")),
    ("update", ("source.updated", "source.updated_from_workspace")),
    ("domain", ("source.updated", "source.updated_from_workspace")),
    ("assign_project", ("source.updated", "graph_edge.created", "source.assigned_project")),
    ("archive", ("source.deleted", "source.archived")),
])
def test_operator_write_events_name_the_caller(label_harness, caller, write, kinds):
    """Guard: each write preserves its event kinds and records the resolved caller."""
    h = label_harness
    key, claims, actor = caller
    method, path, fields = "POST", "/v0/vnext/projects", {"name": "Operator fixture"}
    if write == "charter":
        method, path, fields = "PUT", "/v0/vnext/settings/brain-charter", {"content_markdown": "Keep the record."}
    if write in {"review", "update", "domain", "assign_project", "archive"}:
        source = h.source()
        path = f"/v0/vnext/sources/{source['id']}/review"
        fields = {"action": "update" if write == "domain" else write}
        if write == "update":
            fields["title"] = "Reviewed fixture"
        if write == "domain":
            fields["domain"] = "professional"
        if write == "assign_project":
            with h.store() as store:
                project = store.create_project({"name": "Operator fixture", "slug": "operator-fixture"})
            fields.update(project_id=str(project["id"]), confirm_label_hide=True)
    before = _events(h)
    status, reply, _ = h.request(method, path, key=key, payload={**claims, **fields})
    assert status == (201 if write == "project" else 200), reply
    if write == "project":
        assert set(reply) == {"project"} and reply["project"]["name"] == fields["name"]
    elif write == "charter":
        assert set(reply) == {"brain_charter"} and reply["brain_charter"]["content_markdown"] == fields["content_markdown"]
    else:
        assert set(reply) == {"source", "archived", "trace"}
        assert reply["source"]["id"] == str(source["id"])
        assert reply["archived"] is (write == "archive")
    events = _expect_events(h, before, kinds, actor)
    if "trace" in reply:
        _expect_trace_matches_stored_events(reply["trace"], events)
    if write == "assign_project":
        edge_id = next(event["target_id"] for event in events if event["event_type"] == "graph_edge.created")
        with h.store() as store:
            assert store.get_edge(edge_id)["created_by"] == actor[0]


def test_source_review_propagation_events_keep_the_caller(label_harness):
    """Guard: a source review attributes its derived label writes to the same agent."""
    h = label_harness
    source = h.source()
    h.memory(source=source)
    with h.store() as store:
        _, key = create_agent_key(store, user_id=h.user_id, agent_id="fixture-operator", permission_profile="admin_agent")
    before = _events(h)
    status, body, _ = h.request("POST", f"/v0/vnext/sources/{source['id']}/review", key=key,
                                payload={"action": "update", "sensitivity": "confidential"})
    assert status == 200, body
    events = _expect_events(h, before, (
        "agent.identity_upserted", "policy.decision", "source.updated", "memory.labels_raised", "source.updated_from_workspace",
    ), ("agent", "fixture-operator"))
    _expect_trace_matches_stored_events(body["trace"], events)


def test_operator_activity_holds_new_source_events_to_the_general_feed(label_harness, monkeypatch):
    """Guard: revised attribution preserves admission of the events shown to an operator."""
    h = label_harness
    writer, reader = h.key("admin_agent"), h.key("trusted_local_agent")
    marker = "Confidential synthetic source for the activity test"
    source = h.source(sensitivity="confidential", text=marker)
    source_id = str(source["id"])
    before = _events(h)
    status, project, _ = h.request("POST", "/v0/vnext/projects", key=writer, payload={"name": "Activity fixture"})
    assert status == 201, project
    status, charter, _ = h.request("PUT", "/v0/vnext/settings/brain-charter", key=writer,
                                   payload={"content_markdown": "Store routine activity."})
    assert status == 200, charter
    for action, fields in (
        ("review", {}), ("update", {"title": marker}),
        ("assign_project", {"project_id": str(project["project"]["id"])}), ("archive", {}),
    ):
        status, body, _ = h.request("POST", f"/v0/vnext/sources/{source_id}/review", key=writer,
                                    payload={"action": action, "review_note": marker, **fields})
        assert status == 200, body
    events = _new_events(h, before)
    written_ids = {str(event["id"]) for event in events}
    hidden_ids = {source_id, *(str(event["id"]) for event in events if event["target_type"] in {"source", "graph_edge"})}
    assert len(hidden_ids) > 1
    status, workspace, _ = h.request("GET", "/v0/vnext/workspace", key=reader)
    assert status == 200, workspace
    summarized = set()
    summarize = vnext_projects.summarize_agent_policy_telemetry

    def observe(*, agent_events, **kwargs):
        summarized.update(str(event["id"]) for event in agent_events)
        return summarize(agent_events=agent_events, **kwargs)

    monkeypatch.setattr(vnext_projects, "summarize_agent_policy_telemetry", observe)
    status, telemetry, _ = h.request("GET", "/v0/vnext/agents/policy-telemetry", key=reader)
    assert status == 200, telemetry
    general = {event["id"] for event in workspace["recent_events"]}
    activity = workspace["agent_activity"]["recent_events"]
    assert {event["id"] for event in activity if event["id"] in written_ids} <= general
    assert summarized & written_ids <= general and summarized.isdisjoint(hidden_ids)
    assert written_ids & general  # The fixture includes readable writes as controls.
    for reply in (workspace, telemetry):
        rendered = json.dumps(reply)
        assert marker not in rendered
        assert all(identifier not in rendered for identifier in hidden_ids)


@pytest.mark.parametrize("caller", ["admin_agent", "owner", "declared"], indirect=True)
def test_memory_assignment_edge_names_the_caller(label_harness, caller):
    """Guard: memory assignment uses the caller's actor type on the edge and identity on its event."""
    h = label_harness
    key, claims, actor = caller
    memory = h.memory()
    with h.store() as store:
        project = store.create_project({"name": "Assignment fixture", "slug": "assignment-fixture"})
    before = _events(h)
    status, body, _ = h.request("POST", f"/v0/vnext/memories/{memory['id']}/review", key=key,
                                payload={**claims, "action": "assign_project", "project_id": str(project["id"])})
    assert status == 200, body
    edge_events = [event for event in _new_events(h, before) if event["event_type"] == "graph_edge.created"]
    assert len(edge_events) == 1
    event = edge_events[0]
    assert (event["actor_type"], event["actor_id"]) == actor
    with h.store() as store:
        assert store.get_edge(event["target_id"])["created_by"] == actor[0]


@pytest.fixture
def export(label_harness, tmp_path, monkeypatch):
    h = label_harness
    source = h.source(sensitivity="private")
    with h.store() as store:
        row = store.create_artifact({"artifact_type": "daily_brief", "title": "Export fixture",
                                     "content_markdown": "# A synthetic report\n", "domain": "project",
                                     "sensitivity": "private",
                                     "metadata_json": with_derived_from({}, {"sources": [source]})})
    root = tmp_path / "exports"
    monkeypatch.setattr(vnext_queue, "DEFAULT_VNEXT_ARTIFACT_EXPORT_ROOT", root)
    return str(row["id"]), row["content_markdown"], root


@pytest.mark.parametrize("kind", ["relative", "absolute", "linked-root"])
def test_keyed_export_writes_inside_the_resolved_root(label_harness, export, caller, kind, monkeypatch):
    """Guard: write inside the resolved export root and attribute the export event."""
    h = label_harness
    key, claims, actor = caller
    artifact_id, content, root = export
    expected_root = root
    if kind == "linked-root":
        root.mkdir()
        link = root.with_name("linked-exports")
        link.symlink_to(root, target_is_directory=True)
        monkeypatch.setattr(vnext_queue, "DEFAULT_VNEXT_ARTIFACT_EXPORT_ROOT", link)
        destination = str(link / "nested")
    else:
        destination = "nested" if kind == "relative" else str(root / "nested")
        if kind == "relative" and actor[0] == "user":
            monkeypatch.chdir(root.parent)
            expected_root = root.parent
    before = _events(h)
    status, body, _ = h.request("POST", f"/v0/vnext/artifacts/{artifact_id}/export", key=key,
                                payload={**claims, "output_dir": destination})
    assert status == 200, body
    filename = f"artifact-{hashlib.sha256(artifact_id.encode()).hexdigest()[:24]}.md"
    expected_path = expected_root.resolve() / "nested" / filename
    assert body == {"artifact_id": artifact_id, "output_path": str(expected_path)}
    assert expected_path.read_text() == content
    assert list(expected_path.parent.iterdir()) == [expected_path]
    kinds = ("artifact.exported", "agent.identity_upserted", "policy.decision") if actor[0] == "agent" else ("artifact.exported",)
    _expect_events(h, before, kinds, actor if actor[0] == "agent" else ("system", None))


@pytest.mark.parametrize("kind", ["absolute", "relative", "linked-folder", "linked-file", "directory-instead-of-file"])
def test_keyed_export_refuses_outside_the_root_without_writes(label_harness, export, tmp_path, kind):
    """Guard: refuse an out-of-root destination before changing the filesystem or recording an export."""
    h = label_harness
    key = h.key("admin_agent")
    artifact_id, _, root = export
    outside = tmp_path / "outside"
    if kind == "absolute":
        destination = str(outside)
    elif kind == "relative":
        destination = "../outside"
    elif kind == "linked-folder":
        root.mkdir()
        (root / "linked").symlink_to(outside, target_is_directory=True)
        destination = "linked"
    else:
        root.mkdir()
        filename = f"artifact-{hashlib.sha256(artifact_id.encode()).hexdigest()[:24]}.md"
        (root / filename).symlink_to(outside if kind == "linked-file" else root)
        destination = "."
    before_paths = set(tmp_path.rglob("*"))
    before_events = _events(h)
    status, body, _ = h.request("POST", f"/v0/vnext/artifacts/{artifact_id}/export", key=key,
                                payload={"output_dir": destination})
    assert status == 400 and body == {"detail": "vNext artifact export request is invalid"}
    assert set(tmp_path.rglob("*")) == before_paths
    assert not outside.exists()
    assert _new_events(h, before_events) == []


def test_owner_cli_export_keeps_the_chosen_folder(label_harness, export, tmp_path):
    h = label_harness
    artifact_id, content, root = export
    destination = tmp_path / "owner-choice"
    env = {**os.environ, "DATABASE_URL": h.urls["app"]}
    before = _events(h)
    result = subprocess.run([sys.executable, "-m", "alicebot_api", "--user-id", str(h.user_id),
                             "vnext", "artifacts", "export", artifact_id, "--output-dir", str(destination)],
                            env=env, capture_output=True, text=True, check=True)
    output_path = Path(json.loads(result.stdout)["output_path"])
    assert output_path.parent == destination.resolve() and output_path.read_text() == content
    assert not root.exists()
    _expect_events(h, before, ("artifact.exported",), ("system", None))
