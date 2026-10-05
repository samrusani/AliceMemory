"""Each read stage holds back a stale public row and keeps a visible control."""
from __future__ import annotations
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import compile_session_brief
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_context_tree import ContextTreeRequest, VNextContextTreeService
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService, VNextProjectValidationError
from alicebot_api.vnext_retrieval import VNextRetrievalService, _ResolvedRetrievalScope, expand_provenance_once
from alicebot_api.vnext_temporal_query import TemporalAnchor
from alicebot_api.vnext_open_loop_references import withhold_unreadable_references
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.mcp import evidence_artifacts
from alicebot_api.routers import workspaces
from tests.unit.test_complete_readable_counts import PopulationStore, _quiet_services
from tests.unit.test_derived_labels_real_keys import ALPHA

SOURCE = str(UUID(int=100))
MEMORY = str(UUID(int=101))
LOOP = str(UUID(int=102))
ARTIFACT = str(UUID(int=103))
PROJECT = str(UUID(int=104))
ENTITY = str(UUID(int=105))
SECRET = "Door sentinel"
CEILING = ["public", "internal", "private", "unknown"]

class StageStore(PopulationStore):
    def __init__(self):
        super().__init__()
        derived = {"v": 1, "sources": [SOURCE], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [], "counts": {"sources": 1, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0}}
        self.rows["source"] = [{"id": SOURCE, "domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": [ALPHA]}}]
        for kind, identifier in (("memory", MEMORY), ("open_loop", LOOP), ("artifact", ARTIFACT), ("project", PROJECT)):
            self.rows[kind] = [{"id": identifier, "domain": "project", "sensitivity": "public", "status": "open" if kind == "open_loop" else "active",
                               "title": SECRET, "name": SECRET, "canonical_text": SECRET, "content_markdown": SECRET,
                               "memory_type": "semantic", "artifact_type": "daily_brief", "source_id": SOURCE,
                               "metadata_json": {"project_scope": [ALPHA], "source_id": SOURCE, "discovered_by": "vnext_daily_capture", "derived_from": derived}}]
        self.belief = {"id": str(UUID(int=106)), "memory_id": MEMORY, "claim": "The deployment pipeline is ready for production launch.", "status": "active"}
        self.events = [{"id": str(UUID(int=107)), "event_type": "memory.labels_raised", "target_type": "memory", "target_id": MEMORY, "occurred_at": datetime.now(UTC).isoformat()}]
    def search_memories_vector(self, **kwargs): return self.rows["memory"]
    def search_memories_by_time(self, **kwargs): return self.rows["memory"]
    def search_memories(self, **kwargs): return self.rows["memory"]
    def search_sources(self, **kwargs): return self.rows["source"]
    def search_sources_fts(self, **kwargs): return []
    def search_source_chunks_fts(self, **kwargs): return []
    def get_memories_by_ids(self, ids): return [row for row in self.rows["memory"] if row["id"] in ids]
    def list_memories_referencing_source(self, **kwargs): return self.rows["memory"]
    def list_beliefs(self, **kwargs): return [self.belief]
    def list_memory_events(self, **kwargs): return self.events
    def list_resume_memory_events(self, **kwargs): return []
    def list_open_loop_events(self, **kwargs): return []
    def find_entities_by_names(self, names): return [{"id": ENTITY, "name": "Alice", "entity_type": "person"}]
    def list_memory_entity_edges(self, **kwargs): return [{"from_type": "memory", "from_id": MEMORY, "to_type": "entity", "to_id": ENTITY, "edge_type": "mentions", "observed_at": datetime.now(UTC).isoformat()}]
    def append_event(self, event): return {"id": str(uuid4()), **event}
    def upsert_agent_identity(self, *args, **kwargs): return None
    def get_project(self, identifier): return {"id": identifier, "name": "Visible project", "domain": "project", "sensitivity": "public", "metadata_json": {}}
    def get_entity_optional(self, identifier): return {"id": identifier, "source_memory_ids": [MEMORY]}
    def list_entity_edges_for_entity(self, identifier): return []
    def list_edges(self, **kwargs): return [{"from_type": "memory", "from_id": MEMORY, "to_type": "entity", "to_id": ENTITY}]

def _scope(scoped=False):
    return _ResolvedRetrievalScope(projects=frozenset((ALPHA,)) if scoped else frozenset(), people=frozenset(), window_start=None, window_end=None, exclude_global_domains=frozenset())

STAGES = ("by_ids", "vector", "graph", "temporal", "provenance", "visibility", "contradictions", "scoped_contradictions", "recent_changes", "scoped_recent_changes", "session", "context_projects", "context_memories", "context_open_loops", "context_artifacts", "context_sources", "project_resolution", "dashboard_lists", "loop_memory_reference", "entity_explain", "entity_backing", "workspace_projects", "workspace_memories", "workspace_open_loops", "workspace_artifacts", "workspace_beliefs")

def _stage(stage, store):
    service = VNextRetrievalService(store, embedding_provider=SimpleNamespace(provider="synthetic", model="synthetic", base_url="http://synthetic.invalid"))
    kwargs = {"domains": ["project"], "sensitivity_allowed": CEILING, "limit": 10}
    if stage == "by_ids": return list(service._memories_by_ids([MEMORY], domains=kwargs["domains"], sensitivity_allowed=CEILING).values())
    if stage == "vector": return service._memory_vector_rows(query="Alice", query_vector=[0.1], query_embedding_status="enabled", **kwargs)[0]
    if stage == "graph": return service._memory_graph_rows(query="Alice", entity_read_fenced=True, **kwargs)[0]
    if stage == "temporal": return service._memory_temporal_rows(anchor=TemporalAnchor(datetime.now(UTC)-timedelta(days=1), datetime.now(UTC), "synthetic"), **kwargs)[0]
    if stage == "provenance": return expand_provenance_once(store, fts_memories=[], source_excerpts=[{"id": SOURCE}], already_selected_ids=set(), effective_domains=["project"], effective_sensitivity_allowed=CEILING, effective_project_scope=())
    if stage == "visibility": return [store.rows["memory"][0]] if service.memory_visibility(domains=["project"], sensitivity_allowed=CEILING, scope=None)(store.rows["memory"][0]) else []
    if "contradictions" in stage:
        new = {"id": str(UUID(int=108)), "memory_type": "semantic", "canonical_text": "The deployment pipeline is not ready for production launch."}
        return service._contradicting_evidence([new], requested=True, domains=["project"], sensitivity_allowed=CEILING, scope=_scope(stage.startswith("scoped_")), person_linked_memory_ids=frozenset())[0]
    if "recent_changes" in stage:
        return service._recent_changes(domains=["project"], sensitivity_allowed=CEILING, scope=_scope(stage.startswith("scoped_")), person_linked_memory_ids=frozenset())
    if stage == "session": return compile_session_brief(store, effective_domains=("project",), effective_sensitivity_allowed=tuple(CEILING), effective_project_scope=(), project_view=ProjectView.unscoped(), exclude_global_domains=frozenset(), query=None)
    if stage.startswith("context_"):
        tree = VNextContextTreeService(store).build_tree(ContextTreeRequest(domains=("project",), sensitivity_allowed=tuple(CEILING)))
        return next(root["children"] for root in tree["roots"] if root["id"] == "root:" + stage.removeprefix("context_"))
    if stage == "project_resolution":
        try: return [VNextProjectService(store)._resolve_project(ProjectAutomationRequest(domains=("project",), sensitivity_allowed=tuple(CEILING)))]
        except VNextProjectValidationError: return []
    if stage == "dashboard_lists":
        result = VNextProjectService(store).project_dashboard(project_id=ALPHA, identity=AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent"))
        return [*result["memories"], *result["open_loops"], *result["artifacts"]]
    if stage.startswith("workspace_"):
        field = stage.removeprefix("workspace_")
        if field == "memories":
            store.rows["memory"][0]["status"] = "candidate"
            field = "review_memories"
        return workspaces._vnext_workspace_payload(store)[field]
    trusted = AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent")
    if stage == "loop_memory_reference":
        shown = withhold_unreadable_references(store, [{"id": LOOP, "memory_id": MEMORY, "metadata_json": {}}], fence=SourceReadFence.for_identity(trusted))
        return [shown[0]["memory_id"]] if shown[0]["memory_id"] else []
    if stage == "entity_backing":
        return [store.rows["memory"][0]] if evidence_artifacts._entity_backing_is_fully_authorized(store, identity=trusted, entity_id=ENTITY) else []
    if stage == "entity_explain":
        try:
            evidence_artifacts._authorize_entity_explain_target(None, identity=trusted, entity_id=UUID(ENTITY))
        except evidence_artifacts._ExplainAuthorizationError:
            return []
        return store.rows["memory"]
    raise AssertionError(stage)

@pytest.mark.parametrize("stage", STAGES)
def test_each_stage_uses_current_parent_label(stage, monkeypatch):
    _quiet_services(monkeypatch)
    store = StageStore()
    @contextmanager
    def context(_ignored):
        yield store
    monkeypatch.setattr(evidence_artifacts, "_store_context", context)
    monkeypatch.setattr(evidence_artifacts, "_vnext_store_context", context)
    hidden = _stage(stage, store)
    assert SECRET not in str(hidden)
    assert SOURCE not in str(hidden) and MEMORY not in str(hidden) and LOOP not in str(hidden) and ARTIFACT not in str(hidden) and PROJECT not in str(hidden)
    store.rows["source"][0]["sensitivity"] = "public"
    visible = _stage(stage, store)
    assert visible, (stage, visible)
    if stage != "session":
        assert any(identifier in str(visible) for identifier in (SOURCE, MEMORY, LOOP, ARTIFACT, PROJECT, str(store.belief["id"]))), (stage, visible)
    else:
        assert SECRET in visible
