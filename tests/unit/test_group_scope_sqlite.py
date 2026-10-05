"""Group consumers keep global aggregate cards usable without widening reads."""

from __future__ import annotations

import sqlite3
import json
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_derived_labels import group_scope
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError
from alicebot_api.vnext_rollups import VNextRollupService

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def seed_members(store):
    members = []
    for index, (text, day) in enumerate((("I played Hollow Knight for 25 hours", "2023-06-02"),
                                        ("I played Stardew Valley for 85 hours", "2023-06-20"),
                                        ("I played Celeste for 10 hours", "2023-07-01"))):
        members.append(store.create_memory({"memory_key": f"group-{index}", "memory_type": "episode",
            "title": text, "canonical_text": text, "summary": text, "status": "active", "value": {"text": text},
            "domain": "personal", "sensitivity": "internal",
            "metadata_json": {"session_date": day, "project_scope": [ALPHA, BETA]}}))
    return members


@pytest.fixture
def sqlite_group_store():
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "group@example.invalid", "Group")
    yield SQLiteVNextStore(conn, user_id)
    conn.close()


def test_a_consolidation_candidate_over_a_two_project_group_is_accepted(sqlite_group_store):
    store = sqlite_group_store
    members = seed_members(store)
    first = VNextRollupService(store).propose_rollups(projects=(ALPHA,))
    assert len(first.candidate_ids) == 1
    candidate = store.get_memory(first.candidate_ids[0])
    assert candidate["metadata_json"]["project_scope"] == []
    assert group_scope(candidate) == group_scope(members[0])
    accepted = VNextMemoryCommitService(store).accept_consolidation_candidate(str(candidate["id"]), reason="Reviewed synthetic group")
    assert accepted["status"] == "accepted"


@pytest.mark.parametrize("accept", (False, True))
def test_a_second_scoped_rollup_run_over_the_same_group_finds_its_card_and_inserts_nothing(sqlite_group_store, accept):
    store = sqlite_group_store
    seed_members(store)
    service = VNextRollupService(store)
    first = service.propose_rollups(projects=(ALPHA,))
    assert len(first.candidate_ids) == 1
    if accept:
        VNextMemoryCommitService(store).accept_consolidation_candidate(first.candidate_ids[0], reason="Reviewed synthetic group")
    before = store.conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    second = service.propose_rollups(projects=(ALPHA,))
    assert second.proposals == []
    assert store.conn.execute("SELECT count(*) FROM memories").fetchone()[0] == before
    assert any(group["state"] == ("already_covered_by_accepted" if accept else "existing_candidate") for group in second.groups), second


def test_a_cluster_of_promoted_copies_with_different_floors_is_refused_as_crossing_project_scopes(sqlite_group_store):
    store = sqlite_group_store
    members = seed_members(store)
    first = VNextRollupService(store).propose_rollups(projects=(ALPHA,))
    candidate_id = first.candidate_ids[0]
    # Preserve the snapshot apart from the group labels so the scope check is reached.
    member = members[0]
    metadata = dict(member["metadata_json"])
    metadata["project_floor"] = [ALPHA]
    metadata["project_scope"] = []
    store.conn.execute("UPDATE memories SET metadata_json=? WHERE id=?", (json.dumps(metadata), member["id"]))
    candidate = store.get_memory(candidate_id)
    with pytest.raises(VNextMemoryCommitValidationError, match="crosses project scopes"):
        VNextMemoryCommitService(store).accept_consolidation_candidate(candidate_id, reason="Reviewed synthetic group")
