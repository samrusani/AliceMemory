"""A project update candidate is judged on its caller's limits: the project it is about, and the artifact it returns.

Two answers disclosed what the artifact get hides.

The route took the project named by the request (or the one a bound key is bound to) and generated from it without
asking whether the caller may read the project. A key with a ceiling below confidential, bound to a confidential project
that no readable input names, was answered HTTP 201 with the project's name and the generated text, while a project that
does not exist was answered HTTP 400. The artifact it made was itself above the key's ceiling, so ``GET
/v0/vnext/artifacts/{id}`` answered HTTP 404 for it.

The route also returned the artifact that a digest replay found, and the row that the insert returns when it meets the
unique digest, as they stood. Such an artifact carries the labels it has now, which can be above the caller's after the
stored artifact was raised, and the key was answered HTTP 201 with it while the artifact get answered HTTP 404.

Now the project is judged first, before anything is read from its inputs or written, and a project the caller may not read
is answered exactly as a project that does not exist. Every artifact that comes back, new, found again or returned by the
upsert, is judged by the check the artifact get applies (``outside_caller_limits``) before the event is recorded. An
artifact the caller may not read is not returned and nothing about it is echoed. The owner and an unbound admin key are
answered as before. Each case runs through the mounted application (HTTP) and through the tool handler (MCP), which hand
the service the same resolved identity, and the two answer alike.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): admit every project; admit the
artifact a digest replay finds without judging it; admit the artifact that is made or upserted without judging it; leave
the identity of the caller out of the service call in the route; leave it out in the tool; read a derived project at its stored
label.
"""
from __future__ import annotations

import json
import re
import time
from typing import NamedTuple
from uuid import uuid4

import pytest

from alicebot_api.mcp.projects import _handle_alice_project_update_candidate
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import resolve_agent_identity
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock
from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP
from alicebot_api.vnext_projects import VNextProjectValidationError
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

PATH = "/v0/vnext/projects/update-candidates"
NAME = "Quarterly Kiln Plans"
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Answer(NamedTuple):
    ok: bool
    detail: object  # the artifact when ok, otherwise what the refusal said with every id masked


def _masked(text: str) -> str:
    return UUID_TEXT.sub("<id>", text)


def _http(harness, key, project_id) -> Answer:
    options = {} if project_id is None else {"project_id": str(project_id)}
    status, payload, _headers = harness.request("POST", PATH, payload={"scope": {}, "options": options}, key=key)
    if status == 201:
        return Answer(True, payload)
    return Answer(False, (status, _masked(json.dumps(payload, sort_keys=True))))


def _mcp(harness, key, project_id) -> Answer:
    """The handler of ``alice_project_update_candidate`` with the identity a server resolved from the same key."""

    identity = None
    if key is not None:
        with harness.store() as store:
            identity = resolve_agent_identity(store, user_id=harness.user_id, raw_key=key, payload={})
    context = MCPRuntimeContext(
        database_url=harness.urls["app"], user_id=harness.user_id, agent_identity=identity, agent_identity_resolved=True
    )
    arguments = {} if project_id is None else {"project_id": str(project_id)}
    try:
        return Answer(True, _handle_alice_project_update_candidate(context, arguments))
    except VNextProjectValidationError as exc:
        return Answer(False, ("refused", _masked(str(exc))))


@pytest.fixture(params=["http", "mcp"])
def ask(request):
    """Ask for a candidate through one door: ``ask(harness, key, project_id=None) -> Answer``."""

    door = {"http": _http, "mcp": _mcp}[request.param]

    def asked(harness, key, project_id=None) -> Answer:
        return door(harness, key, project_id)

    asked.door = request.param
    return asked


def _project(harness, *, sensitivity: str = "public", domain: str = "project", name: str = NAME) -> dict:
    with harness.store() as store:
        return store.create_project(
            {
                "name": name,
                "slug": f"kiln-{uuid4().hex[:10]}",
                "description": "d",
                "current_state": "s",
                "domain": domain,
                "sensitivity": sensitivity,
            }
        )


def _get(harness, key, artifact_id) -> int:
    status, _payload, _headers = harness.request("GET", f"/v0/vnext/artifacts/{artifact_id}", key=key)
    return status


def _relabel_artifact(harness, artifact_id, *, sensitivity=None, domain=None, empty_scope=False) -> None:
    """What the owner could do to the stored row: raise its label, and optionally empty the scope its digest lookup reads.

    Raising the stored label is the transition. A public owner operation cannot produce it for a pending candidate while
    its digest stays the same (``test_no_public_owner_route_hides_a_pending_candidate_under_its_digest``), so the row is
    written directly, under the label lock a writer holds.
    """

    with harness.store() as store:
        acquire_exclusive_label_lock(store)
        with store.conn.cursor() as cur:
            if sensitivity is not None:
                cur.execute("UPDATE generated_artifacts SET sensitivity = %s WHERE id = %s", (sensitivity, artifact_id))
                assert cur.rowcount == 1
            if domain is not None:
                cur.execute("UPDATE generated_artifacts SET domain = %s WHERE id = %s", (domain, artifact_id))
                assert cur.rowcount == 1
            if empty_scope:
                cur.execute(
                    "UPDATE generated_artifacts SET metadata_json = jsonb_set(metadata_json, '{project_scope}', '[]'::jsonb)"
                    " WHERE id = %s",
                    (artifact_id,),
                )
                assert cur.rowcount == 1


def _raise_project_by_its_inputs(harness, project) -> None:
    """The project row stays public and records a confidential source as an input, so only its settled label is raised."""

    secret = harness.source(scope=(str(project["id"]),), text="Raised notes.", sensitivity="confidential")
    with harness.store() as store:
        acquire_exclusive_label_lock(store)
        # Written past the store's own propagation, so the stored label stays public.
        with store.conn.cursor() as cur:
            cur.execute(
                "UPDATE projects SET metadata_json = %s::jsonb WHERE id = %s",
                (json.dumps(with_derived_from({}, {"sources": [secret]})), str(project["id"])),
            )
            assert cur.rowcount == 1
        assert store.get_project(str(project["id"]))["sensitivity"] == "public"


def _created_events(harness) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute(
            "SELECT target_id, payload_json FROM event_log WHERE event_type = 'project.update_candidate_created' "
            "ORDER BY occurred_at, id"
        )
        return [dict(row) for row in cur.fetchall()]


def _artifact_ids(harness) -> list[str]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id FROM generated_artifacts WHERE artifact_type = 'project_update' ORDER BY created_at, id")
        return [str(row["id"]) for row in cur.fetchall()]


def _key(harness, profile: str, *, project=None):
    return harness.key(profile, project=None if project is None else str(project["id"]))


def _asked_project(project: dict, bound: bool):
    """A key bound to the project is not told which project: both doors take it from the binding."""

    return None if bound else project["id"]


def _missing_project_answer(harness, ask, profile: str, *, bound: bool) -> Answer:
    """The answer for a project that does not exist, asked by a key of the same kind through the same door."""

    ghost = uuid4()
    if bound:
        return ask(harness, harness.key(profile, project=str(ghost)))
    return ask(harness, harness.key(profile), ghost)


# -- the project -----------------------------------------------------------------------------------------------------

# (profile, the key is bound to the project, sensitivity, domain)
HIDDEN_PROJECTS = [
    pytest.param("project_scoped_agent", True, "confidential", "project", id="bound project key, confidential project"),
    pytest.param("project_scoped_agent", True, "highly_sensitive", "project", id="bound project key, highly sensitive"),
    pytest.param("project_scoped_agent", True, "public", "health", id="bound project key, health domain"),
    pytest.param("project_scoped_agent", True, "public", "legal", id="bound project key, legal domain"),
    pytest.param("trusted_local_agent", True, "confidential", "project", id="bound trusted key, confidential project"),
    pytest.param("trusted_local_agent", False, "confidential", "project", id="unbound trusted key, confidential project"),
    pytest.param("trusted_local_agent", False, "regulated", "project", id="unbound trusted key, regulated project"),
]


@pytest.mark.parametrize(("profile", "bound", "sensitivity", "domain"), HIDDEN_PROJECTS)
def test_a_project_the_key_may_not_read_answers_as_a_project_that_does_not_exist(
    label_harness, ask, profile, bound, sensitivity, domain
):
    project = _project(label_harness, sensitivity=sensitivity, domain=domain)
    key = _key(label_harness, profile, project=project if bound else None)
    missing = _missing_project_answer(label_harness, ask, profile, bound=bound)
    assert missing.ok is False
    before = label_harness.snapshot()

    answer = ask(label_harness, key, _asked_project(project, bound))

    assert answer == missing, answer
    assert NAME not in json.dumps(answer.detail, default=str) and str(project["id"]) not in json.dumps(answer.detail)
    # Nothing was generated and nothing was recorded: not an artifact, a candidate memory, an event or a policy row.
    assert label_harness.snapshot() == before
    assert _artifact_ids(label_harness) == []


def test_a_key_bound_to_another_project_is_answered_as_a_missing_project(label_harness, ask):
    mine = _project(label_harness, name="Mine")
    theirs = _project(label_harness, name="Theirs")
    for profile in ("project_scoped_agent", "trusted_local_agent"):
        key = _key(label_harness, profile, project=mine)
        # The same key, asking for a project that does not exist.
        missing = ask(label_harness, key, uuid4())
        assert missing.ok is False
        before = label_harness.snapshot()
        answer = ask(label_harness, key, theirs["id"])
        assert answer.ok is False and answer.detail == missing.detail, answer
        assert "Theirs" not in json.dumps(answer.detail)
        assert label_harness.snapshot() == before
    assert _artifact_ids(label_harness) == []


@pytest.mark.parametrize(
    ("profile", "bound", "sensitivity", "domain"),
    [
        pytest.param("project_scoped_agent", True, "public", "project", id="bound project key, public project"),
        pytest.param("project_scoped_agent", True, "private", "project", id="bound project key, private project"),
        pytest.param("project_scoped_agent", True, "internal", "professional", id="bound project key, professional domain"),
        pytest.param("trusted_local_agent", True, "private", "project", id="bound trusted key, private project"),
        pytest.param("trusted_local_agent", False, "private", "project", id="unbound trusted key, private project"),
        pytest.param("trusted_local_agent", False, "private", "health", id="unbound trusted key, health domain"),
    ],
)
def test_a_key_still_gets_a_candidate_for_a_project_it_may_read_and_is_shown_it_again(
    label_harness, ask, profile, bound, sensitivity, domain
):
    project = _project(label_harness, sensitivity=sensitivity, domain=domain)
    key = _key(label_harness, profile, project=project if bound else None)
    asked = _asked_project(project, bound)

    first = ask(label_harness, key, asked)
    assert first.ok, first
    assert first.detail["artifact_type"] == "project_update" and NAME in first.detail["content_markdown"]
    assert _get(label_harness, key, first.detail["id"]) == 200
    assert len(_created_events(label_harness)) == 1

    again = ask(label_harness, key, asked)
    assert again.ok and again.detail["id"] == first.detail["id"], again
    assert _artifact_ids(label_harness) == [first.detail["id"]]
    # The replay records no second creation.
    assert len(_created_events(label_harness)) == 1


def test_a_replayed_artifact_is_the_row_the_artifact_get_returns(label_harness):
    project = _project(label_harness)
    key = _key(label_harness, "project_scoped_agent", project=project)
    first = _http(label_harness, key, None)
    again = _http(label_harness, key, None)
    status, stored, _headers = label_harness.request("GET", f"/v0/vnext/artifacts/{first.detail['id']}", key=key)
    assert status == 200 and again.ok and again.detail == stored


def test_a_hidden_project_does_not_hide_the_project_beside_it(label_harness, ask):
    hidden = _project(label_harness, sensitivity="confidential", name="Hidden Kiln")
    shown = _project(label_harness, sensitivity="public", name="Shown Kiln")
    key = label_harness.key("trusted_local_agent")
    assert ask(label_harness, key, hidden["id"]).ok is False
    answer = ask(label_harness, key, shown["id"])
    assert answer.ok and "Shown Kiln" in answer.detail["content_markdown"]


def test_a_key_that_names_no_project_is_given_the_first_project_it_may_read(label_harness, ask):
    """With no project named, the service lists projects. A project above the ceiling is not the one it picks."""

    _project(label_harness, sensitivity="confidential", name="Hidden Kiln")
    key = label_harness.key("trusted_local_agent")
    assert ask(label_harness, key).ok is False
    shown = _project(label_harness, sensitivity="public", name="Shown Kiln")
    answer = ask(label_harness, key)
    assert answer.ok, answer
    assert answer.detail["metadata_json"]["project_id"] == str(shown["id"])
    assert "Hidden Kiln" not in json.dumps(answer.detail, default=str)


def test_a_bound_key_is_given_its_project_when_it_names_none(label_harness, ask):
    """Two projects are stored and the key is bound to the older one. Both doors take the project from the binding."""

    mine = _project(label_harness, name="Mine Kiln")
    _project(label_harness, name="Newer Kiln")
    key = _key(label_harness, "project_scoped_agent", project=mine)
    answer = ask(label_harness, key)
    assert answer.ok, answer
    assert answer.detail["metadata_json"]["project_id"] == str(mine["id"]) and "Mine Kiln" in answer.detail["content_markdown"]


# -- the artifact that comes back --------------------------------------------------------------------------------------

# (profile, the key is bound to the project, what the owner changes on the stored artifact, whether the stored scope is
# emptied too). Without that, the scoped digest lookup finds the artifact and the call is a replay. With it the lookup
# misses, the insert meets the unique digest, and the upsert returns the stored row.
RAISES = [
    pytest.param("project_scoped_agent", True, {"sensitivity": "confidential"}, False, id="bound, replay, confidential"),
    pytest.param("project_scoped_agent", True, {"sensitivity": "confidential"}, True, id="bound, upsert, confidential"),
    pytest.param("project_scoped_agent", True, {"sensitivity": "highly_sensitive"}, False, id="bound, replay, highly sensitive"),
    pytest.param("project_scoped_agent", True, {"domain": "health"}, False, id="bound, replay, health domain"),
    pytest.param("project_scoped_agent", True, {"domain": "health"}, True, id="bound, upsert, health domain"),
    pytest.param("trusted_local_agent", False, {"sensitivity": "confidential"}, False, id="unbound trusted, replay, confidential"),
    pytest.param("trusted_local_agent", False, {"sensitivity": "confidential"}, True, id="unbound trusted, upsert, confidential"),
    pytest.param("trusted_local_agent", False, {"sensitivity": "highly_sensitive"}, False, id="unbound trusted, replay, highly sensitive"),
]


@pytest.mark.parametrize(("profile", "bound", "raised", "empty_scope"), RAISES)
def test_an_artifact_the_key_may_no_longer_read_is_not_returned_by_a_replay(
    label_harness, ask, profile, bound, raised, empty_scope
):
    project = _project(label_harness)
    key = _key(label_harness, profile, project=project if bound else None)
    asked = _asked_project(project, bound)
    first = ask(label_harness, key, asked)
    assert first.ok, first
    artifact_id = first.detail["id"]
    _relabel_artifact(label_harness, artifact_id, empty_scope=empty_scope, **raised)
    # The artifact is one the key may not read: its own get answers it as a missing row.
    assert _get(label_harness, key, artifact_id) == 404
    missing = _missing_project_answer(label_harness, ask, profile, bound=bound)
    before = label_harness.snapshot()
    events_before = _created_events(label_harness)

    answer = ask(label_harness, key, asked)

    assert answer.ok is False and answer.detail == missing.detail, answer
    text = json.dumps(answer.detail, default=str)
    assert artifact_id not in text and NAME not in text and "Project Update Candidate" not in text
    # No artifact, candidate memory or event was written, and the creation event of the first call is the only one.
    assert label_harness.snapshot() == before
    assert _created_events(label_harness) == events_before
    assert _artifact_ids(label_harness) == [artifact_id]


@pytest.mark.parametrize("empty_scope", [False, True], ids=["replay", "upsert"])
def test_a_hidden_artifact_does_not_change_the_answer_for_another_project(label_harness, ask, empty_scope):
    project = _project(label_harness, name="First Kiln")
    other = _project(label_harness, name="Second Kiln")
    key = label_harness.key("trusted_local_agent")
    first = ask(label_harness, key, project["id"])
    assert first.ok
    _relabel_artifact(label_harness, first.detail["id"], sensitivity="confidential", empty_scope=empty_scope)
    assert ask(label_harness, key, project["id"]).ok is False
    second = ask(label_harness, key, other["id"])
    assert second.ok and "Second Kiln" in second.detail["content_markdown"] and second.detail["id"] != first.detail["id"]


@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
@pytest.mark.parametrize("empty_scope", [False, True], ids=["replay", "upsert"])
def test_the_owner_and_an_unbound_admin_key_are_shown_the_confidential_project_and_the_replay(
    label_harness, ask, admin, empty_scope
):
    """A request with no key is the owner's only while the vault has no key, so the owner case mints none."""

    project = _project(label_harness, sensitivity="confidential")
    key = label_harness.key("admin_agent") if admin else None

    first = ask(label_harness, key, project["id"])
    assert first.ok, first
    assert NAME in first.detail["content_markdown"] and first.detail["sensitivity"] == "confidential"

    _relabel_artifact(label_harness, first.detail["id"], sensitivity="highly_sensitive", empty_scope=empty_scope)
    again = ask(label_harness, key, project["id"])
    assert again.ok, again
    assert again.detail["id"] == first.detail["id"] and again.detail["sensitivity"] == "highly_sensitive"
    assert _artifact_ids(label_harness) == [first.detail["id"]]


# -- the answer agrees with the artifact get -------------------------------------------------------------------------

# (stored sensitivity, stored domain) the owner leaves on the artifact the key made.
LABELS = [
    ("public", "project"),
    ("internal", "project"),
    ("private", "project"),
    ("confidential", "project"),
    ("highly_sensitive", "project"),
    ("private", "health"),
    ("private", "financial"),
    ("private", "professional"),
]


@pytest.mark.parametrize(
    ("profile", "bound"),
    [("project_scoped_agent", True), ("trusted_local_agent", True), ("trusted_local_agent", False)],
    ids=["bound project key", "bound trusted key", "unbound trusted key"],
)
def test_a_stored_artifact_is_returned_when_the_artifact_get_would_return_it(label_harness, ask, profile, bound):
    """For each label the artifact can carry, the replay returns the artifact exactly when its get answers 200."""

    project = _project(label_harness)
    key = _key(label_harness, profile, project=project if bound else None)
    asked = _asked_project(project, bound)
    first = ask(label_harness, key, asked)
    assert first.ok, first
    seen = set()
    for sensitivity, domain in LABELS:
        _relabel_artifact(label_harness, first.detail["id"], sensitivity=sensitivity, domain=domain)
        readable = _get(label_harness, key, first.detail["id"]) == 200
        answer = ask(label_harness, key, asked)
        assert (answer.ok and answer.detail["id"] == first.detail["id"]) is readable, (sensitivity, domain, answer)
        seen.add(readable)
    assert seen == {True, False}


# -- a project made of inputs ----------------------------------------------------------------------------------------


def test_a_bound_key_gets_a_candidate_again_after_the_owner_accepted_one(label_harness, ask):
    """Accepting a candidate writes its state onto the project and makes the project a derived row (its inputs are the
    sources, the memories and the artifact). The key bound to it still reads it, so the next candidate is made."""

    project = _project(label_harness, name="Accepted Kiln")
    key = _key(label_harness, "project_scoped_agent", project=project)
    admin = label_harness.key("admin_agent")
    first = ask(label_harness, key)
    assert first.ok, first
    status, reviewed, _headers = label_harness.request(
        "POST", f"{PATH}/{first.detail['id']}/review", payload={"action": "accept"}, key=admin
    )
    assert status == 200 and reviewed["status"] == "accepted", reviewed
    with label_harness.store() as store:
        assert "derived_from" in store.get_project(str(project["id"]))["metadata_json"]
    second = ask(label_harness, key)
    assert second.ok, second
    assert second.detail["metadata_json"]["project_id"] == str(project["id"])
    # The state of the project is part of the digest, so the next candidate is a new one.
    assert second.detail["id"] != first.detail["id"]


def test_a_rejected_candidate_is_still_the_one_a_replay_returns(label_harness, ask):
    project = _project(label_harness, name="Rejected Kiln")
    key = _key(label_harness, "project_scoped_agent", project=project)
    first = ask(label_harness, key)
    status, reviewed, _headers = label_harness.request(
        "POST", f"{PATH}/{first.detail['id']}/review", payload={"action": "reject"}, key=label_harness.key("admin_agent")
    )
    assert status == 200 and reviewed["status"] == "rejected", reviewed
    again = ask(label_harness, key)
    assert again.ok and again.detail["id"] == first.detail["id"] and _get(label_harness, key, first.detail["id"]) == 200


def test_a_project_raised_by_its_inputs_is_read_at_the_label_it_has_now(label_harness, ask):
    """The project row is stored public. Once it records a confidential source as an input (an accepted update does)
    its effective label is confidential, and a key whose ceiling is below that does not read it."""

    project = _project(label_harness, name="Raised Kiln")
    key = _key(label_harness, "project_scoped_agent", project=project)
    admin = label_harness.key("admin_agent")
    _raise_project_by_its_inputs(label_harness, project)
    missing = _missing_project_answer(label_harness, ask, "project_scoped_agent", bound=True)
    before = label_harness.snapshot()
    assert ask(label_harness, key) == missing
    assert label_harness.snapshot() == before
    # An unbound admin key reads the same project.
    answer = ask(label_harness, admin, project["id"])
    assert answer.ok and "Raised Kiln" in answer.detail["content_markdown"]


# -- a key bound to one project with every sensitivity, and the agreement with the artifact get -----------------------

# The project kinds a key may or may not read. "raised" is stored public and settled confidential.
PROJECT_KINDS = {
    "public": {},
    "private": {"sensitivity": "private"},
    "confidential": {"sensitivity": "confidential"},
    "highly sensitive": {"sensitivity": "highly_sensitive"},
    "regulated": {"sensitivity": "regulated"},
    "health domain": {"domain": "health"},
    "legal domain": {"domain": "legal"},
    "raised by an input": {"raised": True},
}


def _project_of_kind(harness, kind: str) -> dict:
    labels = dict(PROJECT_KINDS[kind])
    raised = labels.pop("raised", False)
    project = _project(harness, **labels)
    if raised:
        _raise_project_by_its_inputs(harness, project)
    return project


@pytest.mark.parametrize("kind", [kind for kind in PROJECT_KINDS if kind not in {"public", "private"}])
def test_a_bound_admin_key_is_given_a_candidate_for_its_own_project(label_harness, ask, kind):
    """An admin key bound to one project is allowed every sensitivity and every domain inside that project. A project
    above the default selection (confidential, highly sensitive, regulated, a restricted domain, or raised by an input)
    is its own project all the same, and the artifact get shows it the artifact made for it."""

    project = _project_of_kind(label_harness, kind)
    made = ask(label_harness, label_harness.key("admin_agent"), project["id"])
    assert made.ok, made
    key = _key(label_harness, "admin_agent", project=project)
    assert _get(label_harness, key, made.detail["id"]) == 200, "control: the bound admin key reads the artifact"

    answer = ask(label_harness, key)

    assert answer.ok, answer
    assert answer.detail["metadata_json"]["project_id"] == str(project["id"]) and NAME in answer.detail["content_markdown"]
    assert _get(label_harness, key, answer.detail["id"]) == 200
    again = ask(label_harness, key)
    assert again.ok and again.detail["id"] == answer.detail["id"]


def test_a_bound_admin_key_is_still_refused_another_project(label_harness, ask):
    mine = _project(label_harness, name="Mine Kiln", sensitivity="confidential")
    theirs = _project(label_harness, name="Theirs Kiln", sensitivity="confidential")
    key = _key(label_harness, "admin_agent", project=mine)
    missing = ask(label_harness, key, uuid4())
    answer = ask(label_harness, key, theirs["id"])
    assert answer.ok is False and answer.detail == missing.detail, answer


AGREEMENT_PROFILES = [
    pytest.param("project_scoped_agent", True, id="bound project key"),
    pytest.param("trusted_local_agent", True, id="bound trusted key"),
    pytest.param("trusted_local_agent", False, id="unbound trusted key"),
    pytest.param("admin_agent", True, id="bound admin key"),
    pytest.param("admin_agent", False, id="unbound admin key"),
]


@pytest.mark.parametrize(("profile", "bound"), AGREEMENT_PROFILES)
@pytest.mark.parametrize("kind", list(PROJECT_KINDS))
def test_the_update_door_and_the_artifact_get_agree_for_every_profile(label_harness, ask, profile, bound, kind):
    """An unbound admin key makes the artifact of the project. A key is given a candidate for the project exactly when
    the artifact get shows it that artifact. The read-only and proposal profiles may not generate at all, so the door
    refuses them before it reads the project; they are not part of this comparison."""

    project = _project_of_kind(label_harness, kind)
    made = ask(label_harness, label_harness.key("admin_agent"), project["id"])
    assert made.ok, made
    key = _key(label_harness, profile, project=project if bound else None)
    readable = _get(label_harness, key, made.detail["id"]) == 200
    answer = ask(label_harness, key, _asked_project(project, bound))
    if kind == "raised by an input":
        # The artifact made for a project raised by an input carries the project's stored label, so the get shows it
        # to a key that the project itself is closed to. The door judges the project, the stricter of the two.
        assert answer.ok is profile.startswith("admin"), (profile, bound, answer)
    else:
        assert answer.ok is readable, (kind, profile, bound, answer)


# -- an id that is not a uuid, and a key bound by slug or name ---------------------------------------------------------

def _plain(answer: Answer, *echoed: str) -> tuple:
    """The answer with the words the caller sent masked, for the door that echoes the project it was asked for."""

    detail = answer.detail
    if isinstance(detail, tuple) and detail and detail[0] == "refused":
        text = detail[1]
        for word in echoed:
            text = text.replace(word, "<id>")
        detail = ("refused", text)
    return answer.ok, detail


NOT_A_UUID = [
    pytest.param("Kiln Plans", id="a free-form name"),
    pytest.param("not-a-uuid-1234", id="a malformed id"),
]


@pytest.mark.parametrize("named", NOT_A_UUID)
@pytest.mark.parametrize(
    ("profile", "bound"),
    [
        pytest.param(None, False, id="owner"),
        pytest.param("admin_agent", False, id="unbound admin key"),
        pytest.param("trusted_local_agent", False, id="unbound trusted key"),
    ],
)
def test_a_project_id_that_is_not_a_uuid_is_answered_as_a_project_that_is_not_stored(label_harness, ask, profile, bound, named):
    """PostgreSQL casts the id it is given to a uuid, so a name sent to it raised a database error (a server error).
    A name is read as an id that names no stored project, which is the answer a uuid that is not stored gets."""

    _project(label_harness)
    key = None if profile is None else label_harness.key(profile)
    missing = ask(label_harness, key, uuid4())
    assert missing.ok is False
    before = label_harness.snapshot()

    answer = ask(label_harness, key, named)

    assert _plain(answer, named) == _plain(missing), answer
    assert label_harness.snapshot() == before
    assert _artifact_ids(label_harness) == []


@pytest.mark.parametrize("form", ["slug", "name"])
@pytest.mark.parametrize("profile", ["trusted_local_agent", "project_scoped_agent", "admin_agent"])
def test_a_key_bound_by_slug_or_name_is_answered_as_a_missing_project_and_the_artifact_get_agrees(
    label_harness, ask, profile, form
):
    """A project update is scoped to the uuid of its project, which a binding by slug or name does not name, so the
    artifact get shows such a key none of them. The door answers it as it answers a project that is not stored: no
    database error from the slug or the name, and the answer agrees with the get."""

    project = _project(label_harness)
    made = ask(label_harness, label_harness.key("admin_agent"), project["id"])
    assert made.ok, made
    binding = {"slug": project["slug"], "name": project["name"]}[form]
    key = label_harness.key(profile, project=binding)
    # A key bound by a slug that names no project is the reference answer.
    nowhere = f"no-such-{uuid4().hex[:8]}"
    missing = ask(label_harness, label_harness.key(profile, project=nowhere))
    assert missing.ok is False
    assert _get(label_harness, key, made.detail["id"]) == 404
    before = label_harness.snapshot()

    answer = ask(label_harness, key)

    assert _plain(answer, binding) == _plain(missing, nowhere), answer
    named = ask(label_harness, key, project["id"])
    assert named.ok is False
    assert label_harness.snapshot() == before
    assert len(_artifact_ids(label_harness)) == 1


# -- the owner's routes ----------------------------------------------------------------------------------------------


def test_no_public_owner_route_hides_a_pending_candidate_under_its_digest(label_harness):
    """The digest of a candidate holds the labels of the project, of the sources and of the memories it was made from. A
    route that reclassifies one of those changes the digest, so the next call makes a new candidate and does not replay
    the old one. The candidate memory is the one input the digest does not hold, and no public route changes it while the
    candidate is pending: the memory review route and the redact route refuse it."""

    project = _project(label_harness)
    key = _key(label_harness, "project_scoped_agent", project=project)
    admin = label_harness.key("admin_agent")
    source = None
    with label_harness.store() as store:
        source = store.create_source(
            {
                "source_type": "note",
                "title": f"{NAME} notes",
                "content_hash": str(uuid4()),
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"project_scope": [str(project["id"])], "raw_text": f"{NAME} decision: order the kiln shelves."},
            }
        )
    first = _http(label_harness, key, None)
    assert first.ok and first.detail["metadata_json"]["source_ids"] == [str(source["id"])], first
    artifact_id = first.detail["id"]
    candidate_id = first.detail["metadata_json"]["candidate_memory_id"]

    # The candidate memory: a relabel and a redaction are refused while the candidate is pending.
    status, _body, _headers = label_harness.request(
        "POST", f"/v0/vnext/memories/{candidate_id}/review", payload={"action": "edit", "sensitivity": "confidential"}, key=admin
    )
    assert status == 409
    status, _body, _headers = label_harness.request(
        "POST", "/v0/vnext/memories/redact", payload={"memory_id": candidate_id, "reason": "owner redaction"}, key=admin
    )
    assert status in (400, 409)
    assert _get(label_harness, key, artifact_id) == 200
    again = _http(label_harness, key, None)
    assert again.ok and again.detail["id"] == artifact_id

    # The source: reclassifying it hides the old candidate, which was made from it, and changes the digest of the next one.
    status, body, _headers = label_harness.request(
        "POST", f"/v0/vnext/sources/{source['id']}/review", payload={"action": "update", "sensitivity": "confidential"}, key=admin
    )
    assert status == 200, body
    assert _get(label_harness, key, artifact_id) == 404
    fresh = _http(label_harness, key, None)
    assert fresh.ok and fresh.detail["id"] != artifact_id, fresh
    assert fresh.detail["metadata_json"]["source_ids"] == []
    assert _get(label_harness, key, fresh.detail["id"]) == 200
    assert str(source["id"]) not in json.dumps(fresh.detail, default=str)


# -- the tool ----------------------------------------------------------------------------------------------------------


def test_the_legacy_tool_runs_for_the_owner_and_is_refused_whenever_a_key_is_configured(label_harness, monkeypatch):
    """The tool advertises no identity and is refused when ``ALICE_AGENT_API_KEY`` is set, so through the server it only
    ever runs for the owner. The handler in the cases above is what a resolved identity would reach."""

    project = _project(label_harness, sensitivity="confidential")
    context = MCPRuntimeContext(database_url=label_harness.urls["app"], user_id=label_harness.user_id)
    first = call_mcp_tool(context, name="alice_project_update_candidate", arguments={"project_id": str(project["id"])})
    assert first["artifact_type"] == "project_update" and NAME in first["content_markdown"]
    _relabel_artifact(label_harness, first["id"], sensitivity="highly_sensitive")
    again = call_mcp_tool(context, name="alice_project_update_candidate", arguments={"project_id": str(project["id"])})
    assert again["id"] == first["id"]

    monkeypatch.setenv("ALICE_AGENT_API_KEY", label_harness.key("admin_agent"))
    with pytest.raises(Exception) as refused:
        call_mcp_tool(context, name="alice_project_update_candidate", arguments={"project_id": str(project["id"])})
    assert "legacy MCP surface" in str(refused.value)


# -- the scheduler's scan ----------------------------------------------------------------------------------------------

RUN_NOW = "/v0/vnext/scheduler/workflows/project_update_scan/run-now"
FALLBACK_TITLE = "Project Update Scan"


def _run_now(harness, key) -> tuple[int, dict]:
    status, body, _headers = harness.request("POST", RUN_NOW, payload={"scope": {}, "options": {}}, key=key)
    return status, body


def _stored_titles(harness) -> list[str]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT title FROM generated_artifacts ORDER BY created_at, id")
        return [row["title"] for row in cur.fetchall()]


def _candidate_memories(harness) -> int:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM memories WHERE memory_type = 'project_state'")
        return cur.fetchone()["n"]


@pytest.mark.parametrize(
    ("profile", "bound"),
    [
        pytest.param("trusted_local_agent", False, id="unbound trusted key"),
        pytest.param("trusted_local_agent", True, id="bound trusted key"),
        pytest.param("project_scoped_agent", True, id="bound project key"),
        pytest.param("admin_agent", True, id="bound admin key"),
        pytest.param("admin_agent", False, id="unbound admin key"),
        pytest.param(None, False, id="owner"),
    ],
)
def test_the_scan_of_a_key_with_limits_returns_the_candidate_of_a_project_it_may_read(label_harness, profile, bound):
    """The scheduler stages its writes and publishes them with the run. The candidate made for a readable project is
    published, the run answers it, and a second run finds it again by its digest."""

    project = _project(label_harness, sensitivity="private")
    key = None if profile is None else _key(label_harness, profile, project=project if bound else None)

    status, first = _run_now(label_harness, key)

    assert status == 201, first
    assert first["artifact"]["title"].startswith("Project Update Candidate"), first["artifact"]["content_markdown"][:90]
    assert NAME in first["artifact"]["content_markdown"]
    assert _stored_titles(label_harness) == [first["artifact"]["title"]]
    assert _candidate_memories(label_harness) == 1
    if key is not None:
        assert _get(label_harness, key, first["artifact"]["id"]) == 200

    status, again = _run_now(label_harness, key)
    assert status == 201 and again["artifact"]["id"] == first["artifact"]["id"], again["artifact"]["title"]
    assert _stored_titles(label_harness) == [first["artifact"]["title"]]
    assert _candidate_memories(label_harness) == 1


@pytest.mark.parametrize("kind", ["confidential", "raised by an input"])
@pytest.mark.parametrize(
    ("profile", "bound"),
    [
        pytest.param("trusted_local_agent", False, id="unbound trusted key"),
        pytest.param("project_scoped_agent", True, id="bound project key"),
    ],
)
def test_the_scan_of_a_key_with_limits_leaves_no_candidate_behind_for_a_project_it_may_not_read(
    label_harness, profile, bound, kind
):
    """A project the key may not read is not scanned: the run answers that no project matched, and nothing it staged for
    the project is published. The project raised by an input passes the scheduler's own test on its stored label and is
    refused by the service, which has written nothing at that point."""

    project = _project_of_kind(label_harness, kind)
    key = _key(label_harness, profile, project=project if bound else None)

    status, body = _run_now(label_harness, key)

    assert status == 201, body
    assert body["artifact"]["title"].startswith(FALLBACK_TITLE)
    assert "No active project matched" in body["artifact"]["content_markdown"]
    assert NAME not in json.dumps(body, default=str)
    if not bound:
        # A bound key is told its own binding in the policy decision of the run, which names the project.
        assert str(project["id"]) not in json.dumps(body, default=str)
    assert [title.startswith(FALLBACK_TITLE) for title in _stored_titles(label_harness)] == [True]
    assert _candidate_memories(label_harness) == 0


def test_a_newer_project_raised_by_a_confidential_input_does_not_hide_an_older_one(label_harness, ask):
    """No project id. The newer project is stored public and unreadable because of a confidential input.

    The unbound trusted key is given the older project. The owner and an unbound admin key are still given the newer
    one. A key bound to the older project is given that project. A bound admin key is given the newer one, which is its
    own. A trusted key bound to the newer one is answered as a key bound to a project that does not exist.
    """

    older = _project(label_harness, name="Older Kiln")
    newer = _project(label_harness, name="Newer Kiln")
    _raise_project_by_its_inputs(label_harness, newer)
    # A request with no key is the owner only while the vault has no key.
    owner = ask(label_harness, None)
    assert owner.ok and owner.detail["metadata_json"]["project_id"] == str(newer["id"]), owner
    assert "Newer Kiln" in owner.detail["content_markdown"]
    admin = ask(label_harness, label_harness.key("admin_agent"))
    assert admin.ok and admin.detail["metadata_json"]["project_id"] == str(newer["id"]), admin

    trusted = label_harness.key("trusted_local_agent")
    answer = ask(label_harness, trusted)
    assert answer.ok, answer
    assert answer.detail["metadata_json"]["project_id"] == str(older["id"])
    assert "Older Kiln" in answer.detail["content_markdown"]
    assert "Newer Kiln" not in json.dumps(answer.detail, default=str)

    bound = _key(label_harness, "trusted_local_agent", project=older)
    bound_answer = ask(label_harness, bound)
    assert bound_answer.ok and bound_answer.detail["metadata_json"]["project_id"] == str(older["id"])

    bound_admin = _key(label_harness, "admin_agent", project=newer)
    admin_answer = ask(label_harness, bound_admin)
    assert admin_answer.ok and admin_answer.detail["metadata_json"]["project_id"] == str(newer["id"])
    assert "Newer Kiln" in admin_answer.detail["content_markdown"]

    bound_hidden = _key(label_harness, "trusted_local_agent", project=newer)
    missing = _missing_project_answer(label_harness, ask, "trusted_local_agent", bound=True)
    assert ask(label_harness, bound_hidden) == missing


def test_only_hidden_projects_answer_as_a_vault_with_no_project(label_harness, ask):
    trusted = label_harness.key("trusted_local_agent")
    empty = ask(label_harness, trusted)
    newer = _project(label_harness, name="Newer Kiln")
    _raise_project_by_its_inputs(label_harness, newer)
    assert ask(label_harness, trusted) == empty


def test_the_scan_stops_after_128_hidden_newer_projects(label_harness, ask):
    trusted = label_harness.key("trusted_local_agent")
    empty = ask(label_harness, trusted)
    older = _project(label_harness, name="Older Kiln")
    for index in range(100):
        hidden = _project(label_harness, name=f"Hidden {index}")
        _raise_project_by_its_inputs(label_harness, hidden)
    found = ask(label_harness, trusted)
    assert found.ok and found.detail["metadata_json"]["project_id"] == str(older["id"]), found
    for index in range(100, READABLE_SCAN_CAP):
        hidden = _project(label_harness, name=f"Hidden {index}")
        _raise_project_by_its_inputs(label_harness, hidden)
    started = time.perf_counter()
    capped = ask(label_harness, trusted)
    elapsed = time.perf_counter() - started
    assert capped == empty, capped
    assert elapsed < 2.0, elapsed


def test_the_scheduler_scan_uses_the_older_project_a_key_may_read(label_harness):
    older = _project(label_harness, name="Older Kiln")
    newer = _project(label_harness, name="Newer Kiln")
    _raise_project_by_its_inputs(label_harness, newer)
    # A request with no key is the owner only while the vault has no key.
    status, owner = _run_now(label_harness, None)
    assert status == 201 and "Newer Kiln" in owner["artifact"]["content_markdown"], owner

    status, admin = _run_now(label_harness, label_harness.key("admin_agent"))
    assert status == 201 and "Newer Kiln" in admin["artifact"]["content_markdown"], admin

    trusted = label_harness.key("trusted_local_agent")
    status, body = _run_now(label_harness, trusted)
    assert status == 201, body
    assert "Older Kiln" in body["artifact"]["content_markdown"]
    assert "Newer Kiln" not in json.dumps(body, default=str)
    assert not body["artifact"]["title"].startswith(FALLBACK_TITLE)

    status, bound = _run_now(label_harness, _key(label_harness, "trusted_local_agent", project=older))
    assert status == 201 and "Older Kiln" in bound["artifact"]["content_markdown"]
    assert "Newer Kiln" not in json.dumps(bound, default=str)

    status, bound_admin = _run_now(label_harness, _key(label_harness, "admin_agent", project=newer))
    assert status == 201 and "Newer Kiln" in bound_admin["artifact"]["content_markdown"]

    status, hidden_only = _run_now(label_harness, _key(label_harness, "project_scoped_agent", project=newer))
    assert status == 201, hidden_only
    assert hidden_only["artifact"]["title"].startswith(FALLBACK_TITLE)
    assert "No active project matched" in hidden_only["artifact"]["content_markdown"]
    assert "Older Kiln" not in hidden_only["artifact"]["content_markdown"]
