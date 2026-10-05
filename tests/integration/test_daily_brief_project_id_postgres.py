"""A daily brief must not write a free-form project name into open_loops.project_id.

On Postgres that column is uuid. v0.20.0 wrote the single project string, so a
source scoped ``Alice`` with a TODO line aborted the brief with
``invalid input syntax for type uuid``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_store import PostgresVNextStore


def _source(store: PostgresVNextStore, *, projects: list[str], title: str, line: str) -> None:
    store.create_source(
        {
            "source_type": "manual_text",
            "title": title,
            "content_hash": f"sha256:{uuid4().hex}",
            "captured_at": datetime.now(UTC),
            "source_created_at": datetime.now(UTC),
            "domain": "project",
            "sensitivity": "internal",
            "metadata_json": {
                "raw_text": line,
                "project_scope": projects,
            },
        },
        actor_type="user",
    )


def test_daily_brief_keeps_a_free_form_project_out_of_the_uuid_column(migrated_database_urls) -> None:
    """A name stays in metadata. A single existing project uuid is stored. Two names leave the column empty.

    Mutation: in ``_stored_open_loop_project_id``, return ``project_scope[0]`` whenever the scope has one
    entry. This test then fails while inserting the ``Alice`` loop.
    """

    user = uuid4()
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"brief-{uuid4().hex}@example.invalid", "Brief")
        store = PostgresVNextStore(conn)
        project = store.create_project(
            {
                "name": "Tracked",
                "slug": f"tracked-{uuid4().hex[:8]}",
                "domain": "project",
                "sensitivity": "internal",
            },
            actor_type="user",
        )
        project_id = str(project["id"])
        _source(store, projects=["Alice"], title="Named project", line="TODO: publish the named note")
        _source(store, projects=[project_id], title="Uuid project", line="TODO: publish the uuid note")
        _source(store, projects=["Alice", "Bob"], title="Two projects", line="TODO: publish the pair")
        VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(sensitivity_allowed=ALL_SENSITIVITY, discover_open_loops=True)
        )
        loops = store.list_open_loops(
            status="open",
            sensitivity_allowed=list(ALL_SENSITIVITY),
            limit=20,
        )
    by_scope = {tuple(row["metadata_json"]["project_scope"]): str(row.get("project_id") or "") for row in loops}
    assert by_scope[("Alice",)] == ""
    assert by_scope[(project_id,)] == project_id
    assert by_scope[("Alice", "Bob")] == ""
