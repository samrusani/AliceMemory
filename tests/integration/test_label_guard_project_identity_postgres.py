"""Persisted original project aliases retain their scoped context-tree behavior."""
from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_context_tree import ContextTreeRequest, VNextContextTreeService
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_store import PostgresVNextStore

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


@pytest.mark.parametrize("field", ("id", "slug", "name"))
def test_persisted_original_project_matches_its_id_slug_or_name(migrated_database_urls, field: str) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"project-{user_id}@example.invalid", "Synthetic Project Reader")
        store = PostgresVNextStore(conn)
        project = store.create_project({"name": "Project Atlas", "slug": "launch-plan", "domain": "project", "sensitivity": "public"})
        assert "project_scope" not in project
        assert "project_scope" not in project["metadata_json"]
        scope = (str(project[field]).upper(),)
        payload = VNextContextTreeService(store).build_tree(ContextTreeRequest(projects=scope, include_events=False))
        assert payload["summary"]["projects"] == 1
        assert payload["roots"][0]["children"][0]["ref"] == f"project:{project['id']}"
        canonical = store.create_project({"name": "Canonical Project", "slug": ALPHA, "domain": "project", "sensitivity": "public",
                                          "metadata_json": {"project_scope": [BETA], "project_floor": [BETA]}})
        guard = LabelGuard.for_filters(store, None, ("public",), (ALPHA,))
        assert guard.admit_rows("project", [canonical]) == []
        guard = LabelGuard.for_filters(store, None, ("public",), (BETA,))
        assert guard.admit_rows("project", [canonical]) == [canonical]
