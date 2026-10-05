"""Live PostgreSQL parity for the pending project-update memory guard."""

from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import redact_memory_flow
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError
from alicebot_api.vnext_project_update_guard import PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import lock_label_fixture


@pytest.mark.parametrize("marker", ["workflow", "memory_key"])
@pytest.mark.parametrize("operation", ["correct", "forget", "undo", "redact"])
def test_postgres_pending_project_update_candidate_blocks_generic_memory_mutations(
    migrated_database_urls,
    marker: str,
    operation: str,
) -> None:
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"pending-project-guard-{user_id}@example.invalid",
            "Pending project guard",
        )
        store = PostgresVNextStore(conn)
        lock_label_fixture(store)
        metadata: dict[str, object] = {"candidate": True}
        memory_key = f"ordinary.pending.{uuid4().hex}"
        if marker == "workflow":
            metadata["workflow"] = "project_auto_update"
        else:
            memory_key = f"project_update.alice.{uuid4().hex}"
        memory = store.create_memory(
            {
                "memory_key": memory_key,
                "value": {"text": "Proposed project state."},
                "status": "candidate",
                "memory_type": "project_state",
                "title": "Pending project update",
                "canonical_text": "Proposed project state.",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": metadata,
            }
        )
        artifact = store.create_artifact(
            {
                "artifact_type": "project_update",
                "title": "Pending project update",
                "content_markdown": "# Pending project update",
                "status": "needs_review",
                "domain": "project",
                "sensitivity": "private",
                "metadata_json": {
                    "workflow": "project_auto_update",
                    "candidate_memory_id": str(memory["id"]),
                },
            }
        )
        state_before = (
            store.get_memory(str(memory["id"])),
            store.get_artifact(str(artifact["id"])),
            store.list_revisions(str(memory["id"])),
            store.list_events(),
        )

        with pytest.raises(
            VNextMemoryCommitValidationError,
            match=f"^{PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE}$",
        ):
            service = VNextMemoryCommitService(store)
            if operation == "correct":
                service.correct(
                    identity=None,
                    memory_id=str(memory["id"]),
                    canonical_text="Generic correction must not apply.",
                )
            elif operation == "forget":
                service.forget(identity=None, memory_id=str(memory["id"]), reason="Must not apply.")
            elif operation == "undo":
                service.undo(identity=None, memory_id=str(memory["id"]), reason="Must not apply.")
            else:
                redact_memory_flow(store, memory_id=str(memory["id"]), reason="Must not apply.")

        assert (
            store.get_memory(str(memory["id"])),
            store.get_artifact(str(artifact["id"])),
            store.list_revisions(str(memory["id"])),
            store.list_events(),
        ) == state_before
