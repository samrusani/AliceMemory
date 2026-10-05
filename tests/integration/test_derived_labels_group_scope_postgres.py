"""The PostgreSQL group consumers have the same controls as SQLite."""

from uuid import uuid4
import json

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_derived_labels import group_scope
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError
from alicebot_api.vnext_rollups import VNextRollupService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.unit.test_group_scope_sqlite import ALPHA, seed_members, seed_promoted_members
from tests.integration.conftest import lock_label_fixture


@pytest.mark.parametrize("accept", (False, True))
def test_a_second_scoped_rollup_run_over_the_same_group_finds_its_card_and_inserts_nothing(
    migrated_database_urls, accept
):
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"group-{user_id}@example.invalid", "Group")
        store = PostgresVNextStore(conn)
        lock_label_fixture(store)
        members = seed_members(store)
        service = VNextRollupService(store)
        first = service.propose_rollups(projects=(ALPHA,))
        assert len(first.candidate_ids) == 1
        candidate = store.get_memory(first.candidate_ids[0])
        assert candidate["metadata_json"]["project_scope"] == []
        assert group_scope(candidate) == group_scope(members[0])
        if accept:
            assert (
                VNextMemoryCommitService(store).accept_consolidation_candidate(
                    first.candidate_ids[0], reason="Reviewed synthetic group"
                )["status"]
                == "accepted"
            )
        before = conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"]
        second = service.propose_rollups(projects=(ALPHA,))
        assert second.proposals == []
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == before
        assert any(
            group["state"] == ("already_covered_by_accepted" if accept else "existing_candidate")
            for group in second.groups
        ), second


def test_a_cluster_of_promoted_copies_with_different_floors_is_refused_as_crossing_project_scopes(
    migrated_database_urls,
):
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"group-{user_id}@example.invalid", "Group")
        store = PostgresVNextStore(conn)
        lock_label_fixture(store)
        members = seed_promoted_members(store)
        first = VNextRollupService(store).propose_rollups()
        candidate_id = first.candidate_ids[0]
        member = members[0]
        metadata = dict(member["metadata_json"], project_scope=[], project_floor=[ALPHA])
        conn.execute("UPDATE memories SET metadata_json=%s::jsonb WHERE id=%s", (json.dumps(metadata), member["id"]))
        with pytest.raises(VNextMemoryCommitValidationError, match="crosses project scopes"):
            VNextMemoryCommitService(store).accept_consolidation_candidate(
                candidate_id, reason="Reviewed synthetic group"
            )


@pytest.mark.parametrize("accept", (False, True))
def test_existing_rollup_state_admits_effective_labels_for_pending_and_accepted_cards(migrated_database_urls, accept):
    user_id = uuid4()
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"existing-{user_id}@example.invalid", "Existing")
        store = PostgresVNextStore(conn)
        members = seed_members(store)
        service = VNextRollupService(store)
        proposal = service.propose_rollups(projects=(ALPHA,))
        candidate_id = proposal.candidate_ids[0]
        candidate = store.get_memory(candidate_id)
        metadata = candidate["metadata_json"]
        if accept:
            VNextMemoryCommitService(store).accept_consolidation_candidate(
                candidate_id, reason="Reviewed synthetic group"
            )
        arguments = {
            "rollup_digests": (metadata["rollup_digest"],),
            "rollup_keys": (metadata["rollup_key"],),
            "domains": None,
            "sensitivity_allowed": ["public", "internal"],
            "projects": (ALPHA,),
        }
        pending, accepted = service._existing_rollup_state(**arguments)
        assert bool(accepted if accept else pending)
        # Simulate a stale stored card whose input now has a higher effective label.
        conn.execute("UPDATE memories SET sensitivity='confidential' WHERE id=%s", (members[0]["id"],))
        pending, accepted = service._existing_rollup_state(**arguments)
        assert not pending
        assert not accepted
