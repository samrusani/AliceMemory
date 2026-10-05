"""The PostgreSQL group consumers have the same controls as SQLite."""

from uuid import uuid4

import pytest

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_derived_labels import group_scope
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService
from alicebot_api.vnext_rollups import VNextRollupService
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.conftest import lock_label_fixture
from tests.unit.test_group_scope_sqlite import ALPHA, seed_members


@pytest.mark.parametrize("accept", (False, True))
def test_a_second_scoped_rollup_run_over_the_same_group_finds_its_card_and_inserts_nothing(migrated_database_urls, accept):
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
            assert VNextMemoryCommitService(store).accept_consolidation_candidate(first.candidate_ids[0], reason="Reviewed synthetic group")["status"] == "accepted"
        before = conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"]
        second = service.propose_rollups(projects=(ALPHA,))
        assert second.proposals == []
        assert conn.execute("SELECT count(*) AS n FROM memories").fetchone()["n"] == before
        assert any(group["state"] == ("already_covered_by_accepted" if accept else "existing_candidate") for group in second.groups), second
