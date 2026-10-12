"""The daily brief files a loop only under a project its caller may read, on every door that generates it.

The brief discovers open loops in the sources it reads and files each under the one project its source names in its scope,
or, for a source with none, under the one project the request names. It took that project as it stood: a key with a
ceiling below confidential that captured a source scoped to a confidential project (or named the project in the request)
was answered HTTP 201 with a loop filed under the hidden project, a project id that is not stored made the insert fail on
the foreign key, and the free-form name of a project was filed with no project at all.

The loop is now filed under a project only when the project is stored and the caller may read it, judged with the caller's
own profile ceiling, permitted domains and project binding (a bound admin key reads its own confidential project). The
project column holds that and nothing else. For a source whose one project is hidden, not stored, a free-form name or a
malformed id the column stays empty, as it always did for a name, the scope stays in the metadata of the loop, and the
brief is generated, so the four are answered alike and nothing raises. A source that names two projects or none is filed
under none, as before. The weekly synthesis files no open loop at all.

The caller is the identity the request records, which the route, the generation tools and the scheduler all set, so each
of the three doors is run. The owner and an unbound admin key are answered as before.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): file the loop under the project of
its source with no judgement; judge the project as the owner; look up a project on a store that keeps none.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api.mcp.synthesis import _handle_alice_generate_daily_brief, _handle_alice_generate_weekly_synthesis
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_keys import resolve_agent_identity
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)

BRIEF = "/v0/vnext/artifacts/generate/daily-brief"
WEEKLY = "/v0/vnext/artifacts/generate/weekly-synthesis"
SENSITIVITY = ["public", "internal", "private", "unknown"]


def _project(harness, *, sensitivity: str = "public", domain: str = "project", name: str = "Quarterly Kiln Plans") -> dict:
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


def _source(harness, project_scope: list[str], *, todo: str = "renew the kiln permit"):
    with harness.store() as store:
        row = store.create_source(
            {
                "source_type": "note",
                "title": f"Planning notes {uuid4()}",
                "content_hash": str(uuid4()),
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"project_scope": list(project_scope), "raw_text": f"Planning notes.\nTODO: {todo}"},
            }
        )
        store.create_source_chunk({"source_id": str(row["id"]), "chunk_index": 0, "text": f"TODO: {todo}"})
    return row


def _changed(before: dict, after: dict) -> set[str]:
    return {table for table in after if after[table] != before[table]}


def _loop_rows(harness) -> list[dict]:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT title, project_id FROM open_loops ORDER BY created_at, id")
        return [{"title": row["title"], "project_id": str(row["project_id"]) if row["project_id"] else None} for row in cur.fetchall()]


def _artifact_count(harness) -> int:
    with harness.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM generated_artifacts")
        return cur.fetchone()["n"]


# -- the three doors that generate a brief -----------------------------------------------------------------------------


def _http(path):
    def door(harness, key, project_scope):
        payload: dict = {"scope": {}, "options": {"sensitivity_allowed": SENSITIVITY, "discover_open_loops": True, "source_limit": 20}}
        if project_scope is not None:
            payload["project_scope"] = [project_scope]
        status, _body, _headers = harness.request("POST", path, payload=payload, key=key)
        return status

    return door


def _mcp(handler):
    def door(harness, key, project_scope):
        identity = None
        if key is not None:
            with harness.store() as store:
                identity = resolve_agent_identity(store, user_id=harness.user_id, raw_key=key, payload={})
        context = MCPRuntimeContext(
            database_url=harness.urls["app"], user_id=harness.user_id, agent_identity=identity, agent_identity_resolved=True
        )
        arguments: dict = {"sensitivity_allowed": SENSITIVITY, "discover_open_loops": True, "source_limit": 20}
        if project_scope is not None:
            arguments["project_scope"] = [project_scope]
        handler(context, arguments)
        return 201

    return door


def _scheduler(workflow):
    def door(harness, key, project_scope):
        scope: dict = {} if project_scope is None else {"projects": [project_scope]}
        options = {"discover_open_loops": True, "create_candidate_memories": False, "source_limit": 20}
        status, _body, _headers = harness.request(
            "POST", f"/v0/vnext/scheduler/workflows/{workflow}/run-now", payload={"scope": scope, "options": options}, key=key
        )
        return status

    return door


DAILY_DOORS = {
    "http": _http(BRIEF),
    "mcp": _mcp(_handle_alice_generate_daily_brief),
    "scheduler": _scheduler("daily_brief"),
}
WEEKLY_DOORS = {
    "http": _http(WEEKLY),
    "mcp": _mcp(_handle_alice_generate_weekly_synthesis),
    "scheduler": _scheduler("weekly_synthesis"),
}


@pytest.fixture(params=sorted(DAILY_DOORS))
def door(request):
    return DAILY_DOORS[request.param]


@pytest.fixture(params=["named in the request", "named in the source"])
def named_in(request):
    return request.param


def _scope_for(harness, which: str) -> str:
    return {
        "hidden": lambda: str(_project(harness, sensitivity="confidential")["id"]),
        "missing": lambda: str(uuid4()),
        "free-form": lambda: "Kiln Plans",
        "malformed": lambda: "not-a-uuid",
    }[which]()


def _run(harness, door, key, project_scope: str, named_in: str):
    """One brief over one source scoped to ``project_scope``, which the request names too or leaves to the source."""

    _source(harness, [project_scope])
    return door(harness, key, project_scope if named_in == "named in the request" else None)


UNFILED = ["hidden", "missing", "free-form", "malformed"]


@pytest.mark.parametrize("which", UNFILED)
def test_a_brief_files_its_loop_under_no_project_for_a_project_the_key_may_not_read_or_that_is_not_stored(
    label_harness, door, named_in, which
):
    key = label_harness.key("trusted_local_agent")
    scope = _scope_for(label_harness, which)

    status = _run(label_harness, door, key, scope, named_in)

    # The brief is generated and the loop is made from the source, under no project: the column is empty and the scope
    # stays in the metadata of the loop.
    assert status == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [None]
    assert _artifact_count(label_harness) >= 1


def test_a_hidden_and_a_missing_project_are_answered_alike(label_harness, door, named_in):
    key = label_harness.key("trusted_local_agent")
    answers, written = [], []
    for which in ("hidden", "missing"):
        scope = _scope_for(label_harness, which)
        _source(label_harness, [scope])
        before = label_harness.snapshot()
        answers.append(door(label_harness, key, scope if named_in == "named in the request" else None))
        written.append(_changed(before, label_harness.snapshot()))
    assert answers[0] == answers[1] == 201
    assert written[0] == written[1]
    # Each brief made its loop under no project (the second may read the first source again, so it can make more).
    assert {row["project_id"] for row in _loop_rows(label_harness)} == {None}


# (profile, the key is bound to the project, sensitivity, domain) of a project the key reads.
READABLE = [
    pytest.param("trusted_local_agent", False, "private", "project", id="unbound trusted key"),
    pytest.param("trusted_local_agent", True, "private", "project", id="bound trusted key"),
    pytest.param("admin_agent", True, "confidential", "project", id="bound admin key, confidential project"),
    pytest.param("admin_agent", True, "highly_sensitive", "project", id="bound admin key, highly sensitive project"),
    pytest.param("admin_agent", True, "public", "health", id="bound admin key, health domain"),
    pytest.param("admin_agent", False, "confidential", "project", id="unbound admin key"),
]


@pytest.mark.parametrize(("profile", "bound", "sensitivity", "domain"), READABLE)
def test_a_brief_files_a_loop_under_a_project_the_key_may_read(label_harness, door, named_in, profile, bound, sensitivity, domain):
    project = _project(label_harness, sensitivity=sensitivity, domain=domain)
    key = label_harness.key(profile, project=str(project["id"]) if bound else None)
    status = _run(label_harness, door, key, str(project["id"]), named_in)
    assert status == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(project["id"])]


@pytest.mark.parametrize("door_name", ["http", "mcp"])
def test_a_brief_files_a_loop_under_the_project_a_project_key_is_bound_to(label_harness, door_name, named_in):
    """The scheduler refuses a project-bound key a global workflow such as the daily brief, so it is not run here."""

    project = _project(label_harness, sensitivity="internal")
    key = label_harness.key("project_scoped_agent", project=str(project["id"]))
    status = _run(label_harness, DAILY_DOORS[door_name], key, str(project["id"]), named_in)
    assert status == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(project["id"])]


def test_the_scheduler_still_refuses_a_project_bound_key_the_daily_brief(label_harness):
    project = _project(label_harness, sensitivity="internal")
    key = label_harness.key("project_scoped_agent", project=str(project["id"]))
    assert _run(label_harness, DAILY_DOORS["scheduler"], key, str(project["id"]), "named in the source") == 403
    assert _loop_rows(label_harness) == []


@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_the_owner_and_an_unbound_admin_key_still_file_a_loop_under_a_confidential_project(label_harness, door, named_in, admin):
    """A request with no key is the owner's only while the vault has no key, so the owner case mints none."""

    project = _project(label_harness, sensitivity="highly_sensitive", domain="health")
    key = label_harness.key("admin_agent") if admin else None
    status = _run(label_harness, door, key, str(project["id"]), named_in)
    assert status == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [str(project["id"])]


@pytest.mark.parametrize("which", ["missing", "free-form", "malformed"])
@pytest.mark.parametrize("admin", [True, False], ids=["unbound admin key", "owner"])
def test_a_scope_that_names_no_stored_project_is_filed_under_none_and_raises_nothing_for_the_owner_too(
    label_harness, door, named_in, which, admin
):
    """A project id that is not stored used to fail the insert on the foreign key."""

    key = label_harness.key("admin_agent") if admin else None
    status = _run(label_harness, door, key, _scope_for(label_harness, which), named_in)
    assert status == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [None]


def test_a_hidden_project_in_one_source_does_not_hide_the_loop_of_another(label_harness, door):
    shown = _project(label_harness, sensitivity="private", name="Shown Kiln")
    hidden = _project(label_harness, sensitivity="confidential", name="Hidden Kiln")
    key = label_harness.key("trusted_local_agent")
    _source(label_harness, [str(hidden["id"])], todo="renew the hidden permit")
    _source(label_harness, [str(shown["id"])], todo="renew the shown permit")
    assert door(label_harness, key, None) == 201
    assert sorted(str(row["project_id"]) for row in _loop_rows(label_harness)) == sorted([str(shown["id"]), "None"])


def test_a_source_that_names_two_projects_is_still_filed_under_none(label_harness, door):
    hidden = _project(label_harness, sensitivity="confidential")
    shown = _project(label_harness, sensitivity="private")
    key = label_harness.key("trusted_local_agent")
    _source(label_harness, [str(hidden["id"]), str(shown["id"])])
    assert door(label_harness, key, None) == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [None]


def test_a_source_with_no_project_is_still_filed_under_none(label_harness, door):
    key = label_harness.key("trusted_local_agent")
    _source(label_harness, [])
    assert door(label_harness, key, None) == 201
    assert [row["project_id"] for row in _loop_rows(label_harness)] == [None]


# -- the weekly synthesis files no open loop ---------------------------------------------------------------------------


@pytest.mark.parametrize("door_name", sorted(WEEKLY_DOORS))
def test_the_weekly_synthesis_files_no_loop_under_any_project_and_answers_hidden_and_missing_alike(label_harness, door_name):
    weekly = WEEKLY_DOORS[door_name]
    key = label_harness.key("trusted_local_agent")
    answers, written = [], []
    for which in ("hidden", "missing"):
        scope = _scope_for(label_harness, which)
        _source(label_harness, [scope])
        before = label_harness.snapshot()
        answers.append(weekly(label_harness, key, scope))
        written.append(_changed(before, label_harness.snapshot()))
    shown = _project(label_harness, sensitivity="private")
    _source(label_harness, [str(shown["id"])])
    assert weekly(label_harness, key, str(shown["id"])) == 201
    assert answers[0] == answers[1] == 201
    assert written[0] == written[1]
    assert _loop_rows(label_harness) == []
