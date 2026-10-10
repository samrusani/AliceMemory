"""Totals and trace completeness use the full effectively readable population."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alicebot_api.routers import _vnext_shared, workspaces
from alicebot_api.mcp.retrieval import _resume_event_honours_policy_fence
from alicebot_api.session_briefing import _event_target_honours_fence
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_dogfooding import VNextDogfoodingService
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_context_tree import _tree_event_visible


class PopulationStore:
    def __init__(self):
        self.rows = {kind: [] for kind in ("source", "memory", "artifact", "project", "open_loop")}
        self.events = []

    def read_label_rows(self, kind, ids):
        return [row for row in self.rows[kind] if row["id"] in ids]

    def iter_label_rows(self, kind, *, batch_size=200):
        for start in range(0, len(self.rows[kind]), batch_size):
            yield self.rows[kind][start:start + batch_size]

    def iter_label_events(self, *, batch_size=200):
        for start in range(0, len(self.events), batch_size):
            yield self.events[start:start + batch_size]

    def iter_label_ratings(self):
        return iter(())

    def list_memories(self, *, status=None, limit=None, **kwargs):
        return self.rows["memory"][:limit]

    def list_memories_by_statuses(self, *, statuses, sensitivity_allowed, limit):
        return [row for row in self.rows["memory"] if row["status"] in statuses and row["sensitivity"] in sensitivity_allowed][:limit]

    def count_memories_by_status(self, **kwargs):
        return {"candidate": len(self.rows["memory"])}

    def list_events(self, **kwargs):
        return self.events[:kwargs.get("limit")]

    def __getattr__(self, name):
        kinds = {"sources": "source", "artifacts": "artifact", "projects": "project", "open_loops": "open_loop"}
        suffix = name.removeprefix("list_")
        if name.startswith("get_") and name.removeprefix("get_") in self.rows:
            kind = name.removeprefix("get_")
            return lambda identifier: next((row for row in self.rows[kind] if row["id"] == identifier), None)
        if suffix in kinds:
            def listed(**kwargs):
                rows = self.rows[kinds[suffix]]
                sensitivities = kwargs.get("sensitivity_allowed")
                if sensitivities:
                    rows = [row for row in rows if row["sensitivity"] in sensitivities]
                return rows[:kwargs.get("limit")]
            return listed
        if name.startswith("list_"):
            return lambda *args, **kwargs: []
        if name.startswith("count_"):
            return lambda **kwargs: len(self.rows[kinds[name.removeprefix("count_")]]) if name.removeprefix("count_") in kinds else 0
        if name == "get_brain_charter":
            return lambda: {}
        raise AttributeError(name)


def _row(identifier, sensitivity="public", **kwargs):
    return {"id": identifier, "domain": "project", "sensitivity": sensitivity, "status": "candidate", "metadata_json": {}, **kwargs}


def _quiet_services(monkeypatch):
    for name in ("VNextSchedulerService", "VNextConnectorService", "VNextDoctorService", "VNextProjectService", "VNextMemoryCommitService"):
        monkeypatch.setattr(workspaces, name, lambda store: SimpleNamespace(
            status=lambda: {}, connector_health_all=lambda **kwargs: [], run=lambda **kwargs: {},
            project_dashboard=lambda **kwargs: {}, recent_commits=lambda **kwargs: {"recent_commits": []},
            inline_confirmations=lambda **kwargs: []))
    monkeypatch.setattr(workspaces, "daemon_status", lambda: {})
    monkeypatch.setattr("alicebot_api.vnext_dogfooding.VNextConnectorService.connector_health_all", lambda self, **kwargs: [])


def test_workspace_counts_sql_hidden_and_beyond_display_page(monkeypatch):
    _quiet_services(monkeypatch)
    store = PopulationStore()
    for kind in store.rows:
        store.rows[kind] = [_row(f"{kind}-{index}") for index in range(35)]
        store.rows[kind] += [_row(f"{kind}-hidden-{index}", "confidential") for index in range(205)]
    store.rows["open_loop"] = [{**row, "status": "open"} for row in store.rows["open_loop"]]
    store.events = [{"id": str(index), "target_type": "memory", "target_id": row["id"], "event_type": "memory.labels_raised"} for index, row in enumerate(store.rows["memory"])]
    body = workspaces._vnext_workspace_payload(store, identity=AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent"))
    summary = body["summary"]
    for field in ("source_count", "artifact_count", "project_count", "open_loop_count", "event_count", "candidate_memory_count"):
        assert summary[field] == 35, (field, summary[field])
    assert summary["memory_status_counts"] == {"candidate": 35}
    assert summary["artifact_status_counts"] == {"candidate": 35}
    assert summary["open_loop_status_counts"] == {"open": 35}
    assert body["samples"]["sources"]["has_more"] is True
    assert "hidden" not in str(body)


def test_all_sql_prefiltered_rows_leave_zero_totals(monkeypatch):
    _quiet_services(monkeypatch)
    store = PopulationStore()
    for kind in store.rows:
        store.rows[kind] = [_row(f"{kind}-hidden", "confidential")]
    body = workspaces._vnext_workspace_payload(store, identity=AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent"))
    for field in ("source_count", "artifact_count", "project_count", "open_loop_count", "candidate_memory_count"):
        assert body["summary"][field] == 0
    assert all(not sample["has_more"] for sample in body["samples"].values())


def test_workspace_activity_and_nested_dashboard_share_the_guard(monkeypatch):
    _quiet_services(monkeypatch)
    store = PopulationStore()
    visible = _row("visible-memory")
    hidden = _row("hidden-memory", "confidential")
    store.rows["memory"] = [visible, hidden]
    store.events = [{"id": "visible-event", "target_type": "memory", "target_id": visible["id"], "event_type": "agent.policy_blocked"},
                    {"id": "hidden-event", "target_type": "memory", "target_id": hidden["id"], "event_type": "agent.policy_blocked"}]
    store.list_agent_events = lambda **kwargs: store.events
    store.list_recent_agentic_commits = lambda **kwargs: [visible, hidden]
    store.list_pending_inline_confirmations = lambda **kwargs: [visible, hidden]
    body = workspaces._vnext_workspace_payload(store, identity=AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent"))
    assert "hidden-memory" not in str(body)
    assert "hidden-event" not in str(body)
    activity = body["agent_activity"]
    assert [row["id"] for row in activity["recent_commits"]] == [visible["id"]]
    assert [row["id"] for row in activity["inline_confirmations"]] == [visible["id"]]
    assert [row["id"] for row in activity["policy_blocks"]] == ["visible-event"]
    assert body["dogfooding"]["sample_scope"]["memories"]["total_count"] == 1


def test_dogfooding_counts_hidden_rows_beyond_500(monkeypatch):
    _quiet_services(monkeypatch)
    store = PopulationStore()
    store.rows["memory"] = [_row(str(index)) for index in range(500)] + [_row("hidden", "confidential")]
    trusted = VNextDogfoodingService(store).dashboard(sensitivity_allowed=("public", "internal", "private", "unknown"))
    owner = VNextDogfoodingService(store).dashboard()
    assert trusted["memory_status_counts"] == {"candidate": 500}
    assert trusted["sample_scope"]["memories"]["total_count"] == 500
    assert owner["memory_status_counts"] == {"candidate": 501}


def test_count_rejects_a_store_without_complete_enumeration():
    with pytest.raises(TypeError, match="complete label enumeration"):
        LabelGuard.for_filters(object(), (), ("public",)).readable_status_counts("memory")


def test_derived_totals_use_effective_labels_and_keep_owner_control():
    store = PopulationStore()
    store.rows["source"] = [_row("11111111-1111-4111-8111-111111111111", "confidential")]
    store.rows["memory"] = [_row("22222222-2222-4222-8222-222222222222", metadata_json={"source_id": store.rows["source"][0]["id"]})]
    trusted = LabelGuard.for_filters(store, (), ("public", "internal", "private", "unknown"))
    owner = LabelGuard.for_filters(store, (), ALL_SENSITIVITY)
    assert trusted.readable_status_counts("memory") == {}
    assert owner.readable_status_counts("memory") == {"candidate": 1}


def test_trace_completeness_does_not_reveal_hidden_501st_row(monkeypatch):
    monkeypatch.setattr(_vnext_shared, "_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT", 2)
    store = PopulationStore()
    rows = [_row(str(index)) for index in range(2)] + [_row("hidden", "confidential")]
    fetches = []
    def fetch(limit):
        fetches.append(limit)
        return rows[:limit]
    trusted = AgentIdentity(agent_id="reader", permission_profile="trusted_local_agent", agent_type="unknown")
    admitted, complete = _vnext_shared._vnext_readable_trace_rows(store, "memory", fetch, trusted)
    assert [row["id"] for row in admitted] == ["0", "1"]
    assert complete is True
    assert fetches == [3, 6]
    owner, complete = _vnext_shared._vnext_readable_trace_rows(store, "memory", fetch, None)
    assert len(owner) == 2
    assert complete is False


def test_trace_event_completeness_uses_admitted_targets(monkeypatch):
    monkeypatch.setattr(_vnext_shared, "_VNEXT_SOURCE_TRACE_COLLECTION_LIMIT", 2)
    events = [{"id": str(index), "target_id": "visible"} for index in range(2)] + [{"id": "hidden-event", "target_id": "hidden"}]
    admitted, complete = _vnext_shared._vnext_readable_trace_rows(
        PopulationStore(), "event", lambda limit: events[:limit], None,
        admit=lambda rows: [row for row in rows if row["target_id"] == "visible"],
    )
    assert [row["id"] for row in admitted] == ["0", "1"]
    assert complete is True


@pytest.mark.parametrize("kind", ("source", "memory", "open_loop", "artifact", "project"))
def test_context_events_use_current_effective_target_of_every_kind(kind):
    store = PopulationStore()
    source_id = "11111111-1111-4111-8111-111111111111"
    row_id = source_id if kind == "source" else "22222222-2222-4222-8222-222222222222"
    store.rows["source"] = [_row(source_id, "confidential")]
    if kind != "source":
        refs = {"sources": [source_id], "memories": [], "open_loops": [], "artifacts": [], "beliefs": []}
        store.rows[kind] = [_row(row_id, metadata_json={"derived_from": {"v": 1, **refs, "counts": {name: len(ids) for name, ids in refs.items()}}})]
        if kind == "open_loop":
            store.rows[kind][0]["metadata_json"]["discovered_by"] = "vnext_daily_brief"
            store.rows[kind][0]["metadata_json"]["source_id"] = source_id
    event = {"target_type": kind, "target_id": row_id, "event_type": f"{kind}.labels_raised", "payload_json": {"cause": "repair_v3"}}
    trusted = ["public", "internal", "private", "unknown"]
    assert _tree_event_visible(store, event, None, trusted, (), caller_limited=True) is False
    assert _tree_event_visible(store, event, None, list(ALL_SENSITIVITY), (), caller_limited=True) is True
    store.rows[kind] = [_row(row_id)]
    assert _tree_event_visible(store, event, None, trusted, (), caller_limited=True) is True


def test_context_event_missing_and_unknown_label_targets_fail_closed():
    store = PopulationStore()
    sensitivities = ["public", "internal", "private", "unknown"]
    for kind in ("source", "memory", "open_loop", "artifact", "project", "not-a-label-kind"):
        event = {"target_type": kind, "target_id": "missing", "event_type": f"{kind}.labels_raised"}
        assert _tree_event_visible(store, event, None, sensitivities, (), caller_limited=True) is False
    assert _tree_event_visible(store, {"target_type": "connector", "event_type": "connector.heartbeat"}, None, sensitivities, (), caller_limited=True) is True


@pytest.mark.parametrize("kind", ("memory", "open_loop"))
@pytest.mark.parametrize("reader", (_resume_event_honours_policy_fence, _event_target_honours_fence))
def test_resume_and_session_events_use_current_target_labels(kind, reader):
    store = PopulationStore()
    source_id = "11111111-1111-4111-8111-111111111111"
    row_id = "22222222-2222-4222-8222-222222222222"
    store.rows["source"] = [_row(source_id, "confidential")]
    store.rows[kind] = [_row(row_id, metadata_json={"source_id": source_id})]
    if kind == "open_loop":
        store.rows[kind][0]["metadata_json"]["discovered_by"] = "vnext_daily_brief"
    event = {"target_type": kind, "target_id": row_id, "event_type": f"{kind}.labels_raised", "payload_json": {"cause": "repair_v3"}}
    arguments = {"effective_domains": ("project", "health"), "effective_sensitivity_allowed": ("public", "internal", "private", "unknown"), "effective_project_scope": (), "exclude_global_domains": frozenset()}
    assert reader(store, event, **arguments) is False
    arguments["effective_sensitivity_allowed"] = ALL_SENSITIVITY
    assert reader(store, event, **arguments) is True
