"""A scoped adapter's overlap prefix does not establish locked completeness."""
from datetime import UTC, datetime, timedelta

from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from tests.unit.test_vnext_brain import InMemoryVNextBrainStore


def test_locked_brief_refills_a_crowded_adapter_prefix():
    class Store(InMemoryVNextBrainStore):
        def search_sources(self, *, query, domains=None, sensitivity_allowed=None, limit=8,
                           scope_projects=(), scope_window_start=None, scope_window_end=None):
            return self.sources[:limit]

        def list_open_loops(self, *, status="open", domains=None, sensitivity_allowed=None, limit=8,
                            scope_projects=(), scope_window_start=None, scope_window_end=None):
            return self.open_loops[:limit]

    store = Store()
    rows = [{"id": str(index), "domain": "project", "sensitivity": "public", "created_at": "2026-10-05T09:00:00Z",
             "captured_at": "2026-10-05T09:00:00Z", "metadata_json": {"project_scope": ["alpha", "beta"] if index < 30 else ["alpha"]}}
            for index in range(33)]
    store.sources = rows
    store.open_loops = rows
    start = datetime(2026, 10, 5, tzinfo=UTC)
    selected = VNextBrainService(store)._load_inputs(BrainArtifactRequest(
        agent_identity={"project_scope_locked": True, "project_scope": ["alpha"]}, projects=("alpha",),
        source_limit=5, open_loop_limit=5, generated_for="2026-10-05"), window_start=start, window_end=start + timedelta(days=1))
    assert [row["id"] for row in selected[0]] == ["30", "31", "32"]
    assert [row["id"] for row in selected[2]] == ["30", "31", "32"]
