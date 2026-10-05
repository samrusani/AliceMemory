"""Group scope for consolidation, roll-ups, and the lookups that find a card."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_agent_control import AgentIdentity, resource_project_scope
from alicebot_api.vnext_consolidation import _project_scope_key, _scoped_rows
from alicebot_api.vnext_derived_labels import group_scope
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError
from alicebot_api.vnext_project_scope import project_scope_identity
from alicebot_api.vnext_rollups import ROLLUP_CANDIDATE_KIND
from alicebot_api.vnext_rollups import _scoped_rows as rollup_scoped_rows
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_stores.postgres import memory_access as postgres_memory
from alicebot_api.routers.vnext_review import list_vnext_artifacts
from tests.unit.test_vnext_memory_commit import (
    TargetedLookupStore,
    _seed_consolidation_candidate,
    _seed_row,
)

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16


def _derived(scope: list[str], floor: list[str]) -> dict[str, object]:
    return {
        "id": "derived",
        "memory_key": "vnext.consolidation.example",
        "metadata_json": {
            "candidate_kind": "memory_consolidation",
            "project_scope": scope,
            "project_floor": floor,
        },
    }


def test_scoped_rows_overlap_the_group_scope() -> None:
    row = _derived([], [ALPHA, BETA])
    assert group_scope(row) == (ALPHA, BETA)
    kept = _scoped_rows([row], domains=None, sensitivity_allowed=["unknown"], projects=(ALPHA,))
    assert kept == [row]
    kept_rollups = rollup_scoped_rows(
        [row], domains=None, sensitivity_allowed=["unknown"], projects=(BETA,)
    )
    assert kept_rollups == [row]
    assert _project_scope_key(row) == (ALPHA, BETA)


def test_a_two_project_group_is_accepted_on_the_group_scope() -> None:
    store = TargetedLookupStore()
    members = [
        _seed_row(store, title=f"Member {index}", text="Shared fact.", metadata={"project_scope": [ALPHA, BETA]})
        for index in range(2)
    ]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )
    store.memories[candidate_id]["metadata_json"]["project_scope"] = []
    store.memories[candidate_id]["metadata_json"]["project_floor"] = [ALPHA, BETA]

    result = VNextMemoryCommitService(store).accept_consolidation_candidate(
        candidate_id, reason="The group scope matches."
    )

    assert result["status"] == "accepted"


def test_m51_acceptance_that_compares_scope_again_refuses_the_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def scope_only(row: dict[str, object], *, kind: str | None = None) -> tuple[str, ...]:
        del kind
        return project_scope_identity(resource_project_scope(row))

    monkeypatch.setattr("alicebot_api.vnext_memory_commit.group_scope", scope_only)
    store = TargetedLookupStore()
    members = [
        _seed_row(store, title=f"Wide {index}", text="Shared fact.", metadata={"project_scope": [ALPHA, BETA]})
        for index in range(2)
    ]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )
    store.memories[candidate_id]["metadata_json"]["project_scope"] = []
    store.memories[candidate_id]["metadata_json"]["project_floor"] = [ALPHA, BETA]

    with pytest.raises(VNextMemoryCommitValidationError, match="crosses project scopes"):
        VNextMemoryCommitService(store).accept_consolidation_candidate(candidate_id, reason="Scope only.")


def test_promoted_copies_with_different_floors_are_refused() -> None:
    store = TargetedLookupStore()
    members = [
        _seed_row(
            store,
            title=f"Copy {index}",
            text="Promoted text.",
            metadata={
                "source_artifact_id": f"artifact-{index}",
                "project_scope": [],
                "project_floor": [project],
            },
        )
        for index, project in enumerate((ALPHA, BETA))
    ]
    candidate_id = _seed_consolidation_candidate(
        store,
        member_ids=members,
        proposal_kind="merge",
        survivor_memory_id=None,
        proposed_supersede=list(members),
    )

    with pytest.raises(VNextMemoryCommitValidationError, match="crosses project scopes"):
        VNextMemoryCommitService(store).accept_consolidation_candidate(candidate_id, reason="Different floors.")


def test_rollup_lookups_match_a_floor_when_the_scope_is_empty() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    user_id = str(uuid4())
    ensure_sqlite_user(conn, user_id, "group-scope@example.com", "Group Scope")
    store = SQLiteVNextStore(conn, user_id)
    card = store.create_memory(
        {
            "memory_key": "vnext.rollup.floor-card",
            "value": {"text": "card"},
            "status": "candidate",
            "memory_type": "semantic",
            "title": "Floor card",
            "canonical_text": "Floor card",
            "summary": "Floor card",
            "domain": "personal",
            "sensitivity": "internal",
            "metadata_json": {
                "candidate_kind": ROLLUP_CANDIDATE_KIND,
                "rollup_digest": "digest-floor",
                "rollup_key": "topic:floor",
                "project_scope": [],
                "project_floor": [ALPHA, BETA],
            },
        }
    )
    pending = store.list_pending_rollup_candidates(
        rollup_digests=("digest-floor",),
        domains=["personal"],
        sensitivity_allowed=["internal"],
        candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=5,
        projects=(ALPHA,),
    )
    assert [str(row["id"]) for row in pending] == [str(card["id"])]
    missed = store.list_pending_rollup_candidates(
        rollup_digests=("digest-floor",),
        domains=["personal"],
        sensitivity_allowed=["internal"],
        candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=5,
        projects=("prj_" + "c" * 16,),
    )
    assert missed == []
    accepted = store.create_memory(
        {
            "memory_key": "vnext.rollup.floor-accepted",
            "value": {"text": "accepted"},
            "status": "active",
            "memory_type": "semantic",
            "title": "Accepted floor card",
            "canonical_text": "Accepted floor card",
            "summary": "Accepted floor card",
            "domain": "personal",
            "sensitivity": "internal",
            "metadata_json": {
                "candidate_kind": ROLLUP_CANDIDATE_KIND,
                "rollup_key": "topic:floor-accepted",
                "project_scope": [],
                "project_floor": [ALPHA, BETA],
            },
        }
    )
    cards = store.list_accepted_rollup_cards(
        rollup_keys=("topic:floor-accepted",),
        domains=["personal"],
        sensitivity_allowed=["internal"],
        candidate_kind=ROLLUP_CANDIDATE_KIND,
        limit=5,
        projects=(BETA,),
    )
    assert [str(row["id"]) for row in cards] == [str(accepted["id"])]
    conn.close()


def test_the_lookup_sql_names_the_group_scope() -> None:
    pending = inspect.getsource(postgres_memory.list_pending_rollup_candidates)
    accepted = inspect.getsource(postgres_memory.list_accepted_rollup_cards)
    assert "_MEMORY_GROUP_SCOPE_SQL" in pending
    assert "_MEMORY_GROUP_SCOPE_SQL" in accepted
    assert "_PROJECT_FLOOR_SQL" in inspect.getsource(PostgresVNextStore.list_artifacts)
    assert "project" in inspect.signature(list_vnext_artifacts).parameters


class _SweepStore:
    def __init__(self) -> None:
        self.memories = [
            {
                "id": "alpha",
                "status": "active",
                "memory_type": "semantic",
                "title": "Alpha only",
                "canonical_text": "Alpha only",
                "valid_to": datetime(2020, 1, 1, tzinfo=UTC),
                "metadata_json": {"project_scope": [ALPHA]},
                "sensitivity": "internal",
            },
            {
                "id": "shared",
                "status": "active",
                "memory_type": "semantic",
                "title": "Shared",
                "canonical_text": "Shared",
                "valid_to": datetime(2020, 1, 1, tzinfo=UTC),
                "metadata_json": {"project_scope": [ALPHA, BETA]},
                "sensitivity": "internal",
            },
        ]
        self.artifacts: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.revisions: list[dict[str, object]] = []

    def list_memories_for_staleness_sweep(
        self,
        *,
        reference_time: object = None,
        confirmation_before: object = None,
        review_memory_types: object = None,
        limit: int = 20,
        projects: object = None,
    ) -> list[dict[str, object]]:
        del reference_time, confirmation_before, review_memory_types, limit, projects
        return list(self.memories)

    def update_memory(self, *, memory_id: str, patch: dict[str, object], actor_type: str = "system") -> dict[str, object]:
        del actor_type
        for memory in self.memories:
            if memory["id"] == memory_id:
                memory.update(patch)
                return memory
        raise KeyError(memory_id)

    def append_revision(self, revision: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        del actor_type
        self.revisions.append(revision)
        return revision

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def create_artifact(self, artifact: dict[str, object], *, actor_type: str = "system") -> dict[str, object]:
        del actor_type
        row = {**artifact, "id": "artifact-sweep"}
        self.artifacts.append(row)
        return row


def test_a_locked_staleness_sweep_does_not_mark_a_shared_memory() -> None:
    store = _SweepStore()
    identity = AgentIdentity(
        agent_id="alpha-key",
        project_scope=(ALPHA,),
        project_scope_locked=True,
        permission_profile="trusted_local_agent",
    )
    VNextSchedulerService(store)._run_staleness_sweep(  # noqa: SLF001
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            projects=(ALPHA,),
            agent_identity=identity,
        ),
        metadata={"scheduler_run_id": "run-1", "trace_id": "trace-1"},
    )
    by_id = {memory["id"]: memory for memory in store.memories}
    assert by_id["alpha"]["status"] == "stale"
    assert by_id["shared"]["status"] == "active"
    derived = store.artifacts[0]["metadata_json"]["derived_from"]
    assert derived["memories"] == ["alpha"]
