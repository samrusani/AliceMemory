"""A row the caller could read when a door first looked at it and cannot when the door looks again is a missing row.

A review reads the row, authorizes it, and then reads it again under a lock before it writes. The row can be moved to
another project or raised above the caller's ceiling in between. The second read is judged by the same rule as the first,
so the caller hears what it hears for an id that does not exist, and not a policy decision that repeats the new labels or
a state error that tells it what the row is.

Mutations, each one alone: in ``review_vnext_memory`` (``routers/vnext_memories.py``) delete ``or
outside_caller_limits(store, identity, "memory", preview)`` (the pending-candidate case answers 400, the state of the
row), or ``or outside_caller_limits(store, identity, "memory", existing)`` (the locked case answers 403, a policy
decision); in ``_vnext_memory_correct`` (``mcp/review.py``) delete ``or outside_caller_limits(store, identity, "memory",
memory)`` after the lock (the tool answers ``not_permitted``).
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.unit.test_vnext_main import FakeVNextStore, _install_fake_vnext_store, _seed_active_memory
from alicebot_api.routers import vnext_memories as vnext_memories_router

_USER_ID = "00000000-0000-0000-0000-000000000001"
_KEY_ENV = "ALICE_AGENT_API_KEY"
_OWN, _OTHER = "project-a", "project-b"


def _bound_admin(store: FakeVNextStore, user_id: UUID) -> str:
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="bound-admin", permission_profile="admin_agent", project_scope=_OWN
    )
    return f"Bearer {raw_key}"


def _review(user_id: UUID, memory_id: str, authorization: str, **fields: object):
    return vnext_memories_router.review_vnext_memory(
        UUID(memory_id),
        vnext_memories_router.VNextMemoryReviewRequest(user_id=user_id, **{"action": "edit", **fields}),
        authorization=authorization,
    )


def _moved(row: dict[str, object]) -> dict[str, object]:
    """The same row after it was moved to a project the bound admin key does not hold."""

    relabelled = deepcopy(row)
    relabelled["metadata_json"] = {**relabelled["metadata_json"], "project_scope": [_OTHER]}  # type: ignore[dict-item]
    if "project_scope" in relabelled:  # a stored row can carry its scope at the root, and the root is read first
        relabelled["project_scope"] = [_OTHER]
    return relabelled


def test_a_candidate_moved_to_another_project_before_the_preview_read_is_answered_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="A consolidation candidate of project A.")
    row = store.get_memory(memory_id)
    assert row is not None
    row["metadata_json"] = {
        "project_scope": [_OWN],
        "consolidation": {},  # a candidate that still awaits acceptance
        # The candidate is a derived row, which a locked key reads only when its recorded inputs are known.
        "derived_from": {
            "v": 1, "sources": [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [],
            "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
        },
    }
    authorization = _bound_admin(store, user_id)
    original, relabelled = deepcopy(row), _moved(row)
    reads = {"count": 0}

    def get_memory(requested: str) -> dict[str, object] | None:
        if requested != memory_id:
            return None
        reads["count"] += 1
        return original if reads["count"] == 1 else relabelled  # the second read is the preview

    monkeypatch.setattr(store, "get_memory", get_memory)
    response = _review(user_id, memory_id, authorization, title="A new title")
    # Edited, the candidate would be refused with a message about what it is. The key may not read it now, so it hears
    # what it hears for a memory that does not exist.
    assert (response.status_code, json.loads(response.body)) == (404, {"detail": "vNext memory was not found"})
    assert reads["count"] == 2


def test_a_memory_moved_to_another_project_before_the_locked_read_is_answered_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    memory_id = _seed_active_memory(store, text="A memory of project A.")
    row = store.get_memory(memory_id)
    assert row is not None
    row["metadata_json"] = {"project_scope": [_OWN]}
    authorization = _bound_admin(store, user_id)
    original, relabelled = deepcopy(row), _moved(row)

    monkeypatch.setattr(store, "get_memory", lambda requested: original if requested == memory_id else None)
    monkeypatch.setattr(store, "get_memory_for_update", lambda requested: relabelled if requested == memory_id else None)
    response = _review(user_id, memory_id, authorization, title="A new title")
    # The policy would refuse this edit with a decision that names the project the memory now belongs to.
    assert (response.status_code, json.loads(response.body)) == (404, {"detail": "vNext memory was not found"})
    assert row["title"] != "A new title"


def test_the_correct_tool_answers_a_memory_moved_before_its_locked_read_as_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_KEY_ENV, raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=_USER_ID)
    with sqlite_user_connection(database, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        memory = store.create_memory(
            {
                "memory_key": str(uuid4()), "title": "A memory of project A", "canonical_text": "Project A fact.",
                "status": "candidate", "domain": "project", "sensitivity": "internal",
                "metadata_json": {"project_scope": [_OWN]},
            }
        )
        _record, raw_key = create_agent_key(
            store, user_id=_USER_ID, agent_id="bound-admin", permission_profile="admin_agent", project_scope=_OWN
        )
    memory_id = str(memory["id"])
    monkeypatch.setenv(_KEY_ENV, raw_key)
    original_for_update = SQLiteVNextStore.get_memory_for_update

    def moved_for_update(self: SQLiteVNextStore, requested: str):
        row = original_for_update(self, requested)
        return _moved(dict(row)) if row is not None and requested == memory_id else row

    monkeypatch.setattr(SQLiteVNextStore, "get_memory_for_update", moved_for_update)
    with pytest.raises(Exception) as caught:  # noqa: PT011
        call_mcp_tool(context, name="alice_memory_correct", arguments={"review_item_id": memory_id, "action": "approve"})
    assert type(caught.value).__name__ == "MCPReferenceNotFoundError", caught.value
    assert str(caught.value) == f"memory {memory_id} was not found"
