"""Open-loop extraction answers with the loops its caller may read now: new loops, and loops found again, alike.

A loop already stored under the digest of a candidate is not made again, it is returned. Until now the route returned
that stored row as it stood, so a loop made by an admin key from a confidential source stayed confidential, with
whatever its owner later wrote into it, after the owner lowered the source to public. The unbound trusted key then got
HTTP 404 from ``POST /v0/vnext/open-loops/{id}/review`` and HTTP 201 from ``POST /v0/vnext/open-loops/extract`` with the
loop and its text in the body. The source references were scrubbed from the row, the row itself was not judged.

Every loop the service returns is now admitted through the guard of the caller, on its effective labels, before the
references are scrubbed and before the events and the answer count it. A loop the caller may not read is left out, as a
missing row is, and nothing about it is echoed. The owner and an unbound admin key are shown the loop they were.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): replace the admission in
``VNextProjectService.extract_open_loops`` with the unfiltered list; count the loops before the admission in the
``open_loop.extraction_completed`` event; make the route hand the service a guard that admits everything.
"""
from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_label_guard import clamp_request_filters, guard_for_caller
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock
from alicebot_api.vnext_projects import (
    ProjectAutomationRequest,
    VNextProjectService,
    _open_loop_candidates,
    _open_loop_digest,
)
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

PATH = "/v0/vnext/open-loops/extract"
SENTINEL = "PRIVATE-EDIT-SENTINEL-extract-replay"
EVERYTHING = {"scope": {}, "options": {"sensitivity_allowed": list(ALL_SENSITIVITY), "max_items": 50}}


def _source(harness, *, todo: str, sensitivity: str, domain: str = "project", scope=()):
    with harness.store() as store:
        row = store.create_source(
            {
                "source_type": "note",
                "title": f"Planning notes {uuid4()}",
                "content_hash": str(uuid4()),
                "domain": domain,
                "sensitivity": sensitivity,
                "metadata_json": {"project_scope": list(scope), "raw_text": f"Planning notes.\nTODO: {todo}"},
            }
        )
        store.create_source_chunk({"source_id": str(row["id"]), "chunk_index": 0, "text": f"TODO: {todo}"})
    return row


def _extract(harness, key, body=None):
    status, payload, _headers = harness.request("POST", PATH, payload=EVERYTHING if body is None else body, key=key)
    return status, payload


def _loop_rows(harness) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id, title, description, domain, sensitivity, project_id FROM open_loops ORDER BY created_at, id")
        return [dict(row) for row in cur.fetchall()]


def _extraction_events(harness) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT actor_type, payload_json FROM event_log WHERE event_type = 'open_loop.extraction_completed' "
            "ORDER BY occurred_at, id"
        )
        return [dict(row) for row in cur.fetchall()]


def _lowered_confidential_loop(harness, *, keyed: bool = True) -> dict:
    """The case: an admin key makes a loop from a confidential source, the owner writes into the loop and lowers only
    the source to public. Returns the keys, the ids and the loop. ``keyed=False`` is a vault with no key at all, where
    the owner makes the loop: a request with no key is the owner's only while the vault has no key."""

    todo = f"follow up with the auditors {uuid4()}"
    source = _source(harness, todo=todo, sensitivity="confidential")
    admin = harness.key("admin_agent") if keyed else None
    trusted = harness.key("trusted_local_agent") if keyed else None
    status, made = _extract(harness, admin)
    assert status == 201 and [loop["sensitivity"] for loop in made["open_loops"]] == ["confidential"], made
    loop = made["open_loops"][0]
    with harness.store() as store:
        acquire_exclusive_label_lock(store)
        store.update_open_loop(loop_id=loop["id"], patch={"description": SENTINEL}, actor_type="user")
        store.update_source(source_id=str(source["id"]), patch={"sensitivity": "public"}, actor_type="user")
        stored = store.get_open_loop(loop["id"])
        assert stored["sensitivity"] == "confidential" and stored["description"] == SENTINEL
        assert store.get_source(str(source["id"]))["sensitivity"] == "public"
    return {"admin": admin, "trusted": trusted, "loop": loop, "source": source, "todo": todo}


# -- the case --------------------------------------------------------------------------------------------------------


def test_a_loop_found_again_above_the_ceiling_is_left_out_of_the_answer_and_the_count(label_harness):
    world = _lowered_confidential_loop(label_harness)
    loop_id = world["loop"]["id"]
    rows_before = _loop_rows(label_harness)

    # The loop is one the key may not read: the review door answers it as a missing row.
    status, review, _ = label_harness.request(
        "POST", f"/v0/vnext/open-loops/{loop_id}/review", payload={"action": "edit", "title": "x"}, key=world["trusted"]
    )
    assert status == 404, review

    for body in (EVERYTHING, {}):
        status, payload = _extract(label_harness, world["trusted"], body)
        text = json.dumps(payload)
        assert status == 201, payload
        assert payload == {"open_loops": [], "created_count": 0}, payload
        assert SENTINEL not in text and loop_id not in text

    # Extracting again wrote no loop, and the stored loop is as the owner left it.
    assert _loop_rows(label_harness) == rows_before


def test_the_extraction_event_does_not_count_a_loop_the_caller_may_not_read(label_harness):
    world = _lowered_confidential_loop(label_harness)
    events_before = len(_extraction_events(label_harness))
    status, payload = _extract(label_harness, world["trusted"])
    assert status == 201 and payload["created_count"] == 0
    events = _extraction_events(label_harness)
    assert len(events) == events_before + 1
    # A count of the events, read from the workspace feed, must not tell the key that a loop it may not read exists.
    assert events[-1]["payload_json"]["created_count"] == 0
    status, payload = _extract(label_harness, world["admin"])
    assert status == 201 and payload["created_count"] == 1
    assert _extraction_events(label_harness)[-1]["payload_json"]["created_count"] == 1


@pytest.mark.parametrize("keyed", [True, False], ids=["unbound admin key", "owner"])
def test_the_owner_and_an_unbound_admin_key_are_still_shown_the_existing_loop(label_harness, keyed):
    world = _lowered_confidential_loop(label_harness, keyed=keyed)
    loop_id = world["loop"]["id"]
    status, payload = _extract(label_harness, world["admin"])
    assert status == 201, payload
    assert [loop["id"] for loop in payload["open_loops"]] == [loop_id]
    assert payload["open_loops"][0]["description"] == SENTINEL
    assert payload["open_loops"][0]["sensitivity"] == "confidential"
    assert payload["created_count"] == 1


def test_a_key_with_a_ceiling_still_extracts_a_new_loop_it_may_read_and_is_shown_it_again(label_harness):
    todo = f"order the kiln parts {uuid4()}"
    _source(label_harness, todo=todo, sensitivity="public")
    trusted = label_harness.key("trusted_local_agent")
    status, first = _extract(label_harness, trusted)
    assert status == 201, first
    assert [loop["title"] for loop in first["open_loops"]] == [todo] and first["created_count"] == 1
    status, again = _extract(label_harness, trusted)
    assert status == 201
    assert [loop["id"] for loop in again["open_loops"]] == [first["open_loops"][0]["id"]]
    assert again["created_count"] == 1
    assert len(_loop_rows(label_harness)) == 1


def test_a_hidden_loop_does_not_hide_the_loops_beside_it(label_harness):
    world = _lowered_confidential_loop(label_harness)
    todo = f"book the venue {uuid4()}"
    _source(label_harness, todo=todo, sensitivity="public")
    status, payload = _extract(label_harness, world["trusted"])
    assert status == 201
    assert [loop["title"] for loop in payload["open_loops"]] == [todo]
    assert payload["created_count"] == 1
    assert world["loop"]["id"] not in json.dumps(payload) and SENTINEL not in json.dumps(payload)


# -- the digest conflict path ---------------------------------------------------------------------------------------


def test_a_loop_the_upsert_returns_after_a_digest_conflict_is_admitted_like_any_other(label_harness, monkeypatch):
    """The lookup by digest filters on the project column, and a loop whose project column was emptied is missed by it.
    The insert then meets the unique digest, and the upsert returns the stored loop, here one above the ceiling."""

    with label_harness.store() as store:
        project = store.create_project(
            {"name": "Kiln", "slug": f"kiln-{uuid4().hex[:8]}", "description": "d", "current_state": "s",
             "domain": "project", "sensitivity": "public"}
        )
    project_id = str(project["id"])
    todo = f"renew the kiln permit {uuid4()}"
    source = _source(label_harness, todo=todo, sensitivity="public", scope=(project_id,))
    candidate = _open_loop_candidates(source)[0]
    digest = _open_loop_digest(candidate, project_id=project_id, person_id=None)
    with label_harness.store() as store:
        stored = store.create_open_loop(
            {
                "title": todo,
                "description": SENTINEL,
                "domain": "project",
                "sensitivity": "confidential",
                "source_id": str(source["id"]),
                "metadata_json": {"automation_digest": digest, "idempotency_digest": digest},
            }
        )
    assert stored["project_id"] is None

    returned: list[str] = []
    original = PostgresVNextStore.upsert_open_loop_by_automation_digest

    def spy(self, loop, **kwargs):
        row = original(self, loop, **kwargs)
        returned.append(str(row["id"]))
        return row

    monkeypatch.setattr(PostgresVNextStore, "upsert_open_loop_by_automation_digest", spy)
    body = {"scope": {"project_id": project_id}, "options": {"sensitivity_allowed": list(ALL_SENSITIVITY), "max_items": 50}}
    trusted, admin = label_harness.key("trusted_local_agent"), label_harness.key("admin_agent")

    status, payload = _extract(label_harness, trusted, body)
    assert status == 201, payload
    assert returned == [str(stored["id"])], "the loop must come back through the upsert, not the lookup"
    assert payload == {"open_loops": [], "created_count": 0}
    assert SENTINEL not in json.dumps(payload)

    returned.clear()
    status, payload = _extract(label_harness, admin, body)
    assert status == 201 and returned == [str(stored["id"])]
    assert [loop["id"] for loop in payload["open_loops"]] == [str(stored["id"])]
    assert payload["open_loops"][0]["description"] == SENTINEL and payload["created_count"] == 1


# -- the other limits of a caller: its project binding and its domains --------------------------------------------


def _service_extract(harness, identity, request: ProjectAutomationRequest) -> list[dict]:
    """What the route does, without the central gate: clamp the request, hand the service the guard of the caller.

    The central gate refuses a project-bound or read-only key before the handler (``http.operator.access``), so the
    limits of those callers are asked as the open-loop list asks them, with ``open_loop.lookup``. The service holds
    whatever guard it is given.
    """

    action = "open_loop.lookup"
    with harness.store() as store:
        domains, sensitivity = clamp_request_filters(
            identity, domains=request.domains, sensitivity_allowed=request.sensitivity_allowed, action=action
        )
        guard = guard_for_caller(store, identity, action=action)
        loops = VNextProjectService(store).extract_open_loops(
            replace(request, domains=domains, sensitivity_allowed=sensitivity), guard=guard
        )
        return [{**loop, "id": str(loop["id"])} for loop in loops]


def _bound(project_id: str, profile: str = "project_scoped_agent") -> AgentIdentity:
    return AgentIdentity(
        agent_id="alpha-reader", permission_profile=profile, auth="agent_api_key",
        project_scope=(project_id,), project_scope_locked=True,
    )


def test_a_loop_in_another_project_is_left_out_of_a_key_bound_to_the_first(label_harness):
    """The digest belongs to the user, and the lookup filters on the project column. A loop whose columns name another
    project than the request does is missed by the lookup and returned by the upsert after the digest conflict."""

    with label_harness.store() as store:
        alpha, beta = (
            store.create_project(
                {"name": name, "slug": f"{name.lower()}-{uuid4().hex[:8]}", "description": "d", "current_state": "s",
                 "domain": "project", "sensitivity": "public"}
            )
            for name in ("Alpha", "Beta")
        )
    alpha_id, beta_id = str(alpha["id"]), str(beta["id"])
    todo = f"file the alpha report {uuid4()}"
    source = _source(label_harness, todo=todo, sensitivity="public", scope=(alpha_id,))
    request = ProjectAutomationRequest(
        agent_identity=None, project_id=alpha_id, sensitivity_allowed=("public",), max_items=50
    )
    digest = _open_loop_digest(_open_loop_candidates(source)[0], project_id=alpha_id, person_id=None)
    with label_harness.store() as store:
        other = store.create_open_loop(
            {
                "title": todo,
                "description": SENTINEL,
                "domain": "project",
                "sensitivity": "public",
                "project_id": beta_id,
                "metadata_json": {
                    "project_scope": [beta_id], "automation_digest": digest, "idempotency_digest": digest,
                },
            }
        )
    identity = _bound(alpha_id)
    # Nothing in the request, the source or the loop's sensitivity is above this key: only the project is.
    assert _service_extract(label_harness, identity, request) == []
    assert [loop["id"] for loop in _service_extract(label_harness, None, request)] == [str(other["id"])]
    # A loop of its own project is still made for the bound key and returned to it.
    own_todo = f"book the alpha review {uuid4()}"
    _source(label_harness, todo=own_todo, sensitivity="public", scope=(alpha_id,))
    shown = _service_extract(label_harness, identity, request)
    assert [loop["title"] for loop in shown] == [own_todo]
    assert str(other["id"]) not in json.dumps(shown, default=str) and SENTINEL not in json.dumps(shown, default=str)
    assert [loop["id"] for loop in _service_extract(label_harness, identity, request)] == [shown[0]["id"]]


def test_a_loop_whose_domain_the_caller_may_not_read_is_left_out_of_a_key_that_reads_the_source(label_harness):
    todo = f"renew the policy {uuid4()}"
    _source(label_harness, todo=todo, sensitivity="public")
    request = ProjectAutomationRequest(agent_identity=None, sensitivity_allowed=("public",), max_items=50)
    identity = AgentIdentity(agent_id="reader", permission_profile="read_only_agent", auth="agent_api_key")
    made = _service_extract(label_harness, None, request)
    assert [loop["title"] for loop in made] == [todo]
    assert [loop["id"] for loop in _service_extract(label_harness, identity, request)] == [made[0]["id"]]
    with label_harness.store() as store:
        acquire_exclusive_label_lock(store)
        store.update_open_loop(loop_id=made[0]["id"], patch={"domain": "health", "description": SENTINEL}, actor_type="user")
        assert store.get_open_loop(made[0]["id"])["domain"] == "health"
    # The source is still readable by this key, the loop made from it is now in a domain the key may not read.
    assert _service_extract(label_harness, identity, request) == []
    assert [loop["id"] for loop in _service_extract(label_harness, None, request)] == [made[0]["id"]]
