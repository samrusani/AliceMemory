"""The HTTP answers of the two routes that used to fail with HTTP 500, on the in-memory store.

The database behaviour (the unique constraint, the claim statement) is tested on PostgreSQL in
``tests/integration/test_route_server_errors_postgres.py``. This file holds the wiring of the routes: which exception
becomes which answer, and which labels a caller's claim is held to.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_stores.postgres.project_slug import ProjectSlugConflictError
from tests.unit.test_vnext_main import FakeVNextStore, _install_fake_vnext_store, _invoke_vnext_request

CONFLICT = {"detail": "vNext project slug is already in use"}
IDLE = {"status": "idle", "task_id": None, "artifact_id": None, "error_code": None, "error_message": None}


def _caller(store: FakeVNextStore, user_id, profile: str | None) -> dict[str, str | None]:
    if profile is None:
        return {"authorization": None}
    _record, raw_key = create_agent_key(store, user_id=user_id, agent_id=f"{profile}-agent", permission_profile=profile)
    return {"authorization": f"Bearer {raw_key}"}


@pytest.mark.parametrize("profile", [None, "admin_agent", "trusted_local_agent"])
def test_a_project_slug_in_use_answers_409_in_the_same_words_to_every_caller(monkeypatch, profile) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    caller = _caller(store, user_id, profile)

    def slug_in_use(project, **_kwargs):
        raise ProjectSlugConflictError("a project with this slug already exists")

    store.create_project = slug_in_use  # type: ignore[method-assign]

    status, body = _invoke_vnext_request(
        "POST", "/v0/vnext/projects", payload={"user_id": str(user_id), "name": "Alice", "slug": "alice"}, **caller
    )

    assert (status, body) == (409, CONFLICT)


def test_a_free_slug_still_creates_the_project(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    store.create_project = lambda project, **_kwargs: {"id": "p-1", **project}  # type: ignore[method-assign]

    status, body = _invoke_vnext_request(
        "POST", "/v0/vnext/projects", payload={"user_id": str(uuid4()), "name": "Alice Beta"}
    )

    assert status == 201 and body["project"]["slug"] == "alice-beta"


def _task(store: FakeVNextStore, title: str, sensitivity: str, domain: str = "project") -> str:
    return str(
        store.create_task({"title": title, "task_type": "summarize", "instructions": title, "domain": domain, "sensitivity": sensitivity})["id"]
    )


def test_process_next_holds_a_trusted_key_to_the_tasks_it_may_read_and_leaves_the_rest_to_an_admin(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    hidden = _task(store, "older confidential", "confidential")
    shown = _task(store, "newer public", "public")
    trusted = _caller(store, user_id, "trusted_local_agent")
    admin = _caller(store, user_id, "admin_agent")
    payload = {"user_id": str(user_id)}

    status, body = _invoke_vnext_request("POST", "/v0/vnext/queue/process-next", payload=payload, **trusted)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == shown
    assert _invoke_vnext_request("POST", "/v0/vnext/queue/process-next", payload=payload, **trusted) == (200, IDLE)
    assert {task["id"]: task["status"] for task in store.tasks} == {hidden: "pending", shown: "completed"}

    status, body = _invoke_vnext_request("POST", "/v0/vnext/queue/process-next", payload=payload, **admin)
    assert status == 200 and body["status"] == "completed" and body["task_id"] == hidden


def test_process_next_gives_the_owner_the_oldest_task_whatever_its_label(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    hidden = _task(store, "older confidential", "confidential")
    shown = _task(store, "newer public", "public")
    payload = {"user_id": str(uuid4())}

    first = _invoke_vnext_request("POST", "/v0/vnext/queue/process-next", payload=payload)
    second = _invoke_vnext_request("POST", "/v0/vnext/queue/process-next", payload=payload)
    third = _invoke_vnext_request("POST", "/v0/vnext/queue/process-next", payload=payload)

    assert [body.get("task_id") for _status, body in (first, second, third)] == [hidden, shown, None]
    assert third == (200, IDLE)


def test_process_next_refuses_a_key_the_operator_gate_refuses(monkeypatch) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _task(store, "public", "public")
    read_only = _caller(store, user_id, "read_only_agent")

    status, _body = _invoke_vnext_request(
        "POST", "/v0/vnext/queue/process-next", payload={"user_id": str(user_id)}, **read_only
    )

    assert status == 403
    assert [task["status"] for task in store.tasks] == ["pending"]


@pytest.mark.parametrize("profile", ["trusted_local_agent", "admin_agent"])
def test_process_next_for_a_key_locked_to_a_project_that_reaches_the_handler_claims_nothing(monkeypatch, profile) -> None:
    from alicebot_api.routers import vnext_review

    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    task = _task(store, "public task", "public")
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="locked", permission_profile=profile, project_scope="alpha"
    )

    # The central gate refuses this key before the handler. This calls the handler alone, as a request that got past
    # the gate would: a queued task names no project, so a key locked to one claims nothing and is told the queue is idle.
    answer = vnext_review.process_next_vnext_queue_task(
        vnext_review.VNextQueueProcessNextRequest(user_id=user_id), authorization=f"Bearer {raw_key}"
    )

    assert answer.status_code == 200 and json.loads(answer.body) == IDLE
    assert [(row["id"], row["status"]) for row in store.tasks] == [(task, "pending")]


@pytest.mark.parametrize("profile", ["trusted_local_agent", "admin_agent"])
def test_process_next_refuses_a_key_locked_to_a_project_at_the_gate(monkeypatch, profile) -> None:
    store = FakeVNextStore(None)
    _install_fake_vnext_store(monkeypatch, store)
    user_id = uuid4()
    _task(store, "public task", "public")
    _record, raw_key = create_agent_key(
        store, user_id=user_id, agent_id="locked", permission_profile=profile, project_scope="alpha"
    )

    status, _body = _invoke_vnext_request(
        "POST", "/v0/vnext/queue/process-next", payload={"user_id": str(user_id)}, authorization=f"Bearer {raw_key}"
    )

    assert status == 403
    assert [row["status"] for row in store.tasks] == ["pending"]
