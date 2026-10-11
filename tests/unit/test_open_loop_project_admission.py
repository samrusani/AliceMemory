"""Open-loop extraction files loops only under a project its caller may read, and says so alike for a project not stored.

A loop is filed under the project the request names (``project_id``). The service loads that project and judges it with
``project_in_view``: the domain and sensitivity the project has now, raised to its inputs, and the project binding of the
guard. A project the guard does not admit is answered as a project that is not stored: no source is read for it, no loop
is made, and the completion event records no loop and no source. A store that keeps no projects (the local SQLite vault,
where a project is a free-form name in a scope) has no project to judge and the name is admitted as it always was.

The PostgreSQL counterpart, through the mounted application, is
``tests/integration/test_open_loop_project_admission_postgres.py``.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): leave the project out of the
extraction; let the extraction admit every named project; make a store without projects refuse every name.
"""
from __future__ import annotations

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_label_guard import LabelGuard, guard_for_caller
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService, project_readable
from tests.unit.test_vnext_projects import InMemoryVNextProjectStore, _seed_store

PROJECT = "project-1"
GHOST = "project-ghost"
ALL = ("public", "internal", "private", "confidential", "highly_sensitive", "unknown")
TRUSTED = AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent", auth="agent_api_key")
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_api_key")


def _store(*, sensitivity: str = "private", domain: str = "project") -> InMemoryVNextProjectStore:
    store = _seed_store()
    store.projects[PROJECT].update({"sensitivity": sensitivity, "domain": domain})
    # A source that names a project nobody stored, as any source may.
    ghost_source = dict(store.sources[0])
    ghost_source.update({"id": "source-ghost", "metadata_json": {**ghost_source["metadata_json"], "project_scope": [GHOST]}})
    store.sources.append(ghost_source)
    return store


def _request(project_id: str) -> ProjectAutomationRequest:
    return ProjectAutomationRequest(agent_identity=None, project_id=project_id, sensitivity_allowed=ALL, max_items=20)


def _extract(store, project_id: str, identity: AgentIdentity | None) -> list[dict]:
    guard = LabelGuard.unlimited(store) if identity is None else guard_for_caller(store, identity)
    return [dict(loop) for loop in VNextProjectService(store).extract_open_loops(_request(project_id), guard=guard)]


def _completion(store) -> dict:
    return [event for event in store.events if event["event_type"] == "open_loop.extraction_completed"][-1]["payload_json"]


@pytest.mark.parametrize("sensitivity", ["confidential", "highly_sensitive", "regulated"])
def test_a_project_the_caller_may_not_read_files_no_loop_and_is_answered_as_a_project_not_stored(sensitivity):
    store = _store(sensitivity=sensitivity)
    assert _extract(store, GHOST, TRUSTED) == []
    missing = _completion(store)
    assert _extract(store, PROJECT, TRUSTED) == []
    assert _completion(store) == missing == {"created_count": 0, "source_ids": []}
    assert store.open_loops == {}


@pytest.mark.parametrize("identity", [None, ADMIN], ids=["owner", "unbound admin key"])
def test_the_owner_and_an_unbound_admin_key_file_loops_under_a_confidential_project(identity):
    store = _store(sensitivity="highly_sensitive")
    loops = _extract(store, PROJECT, identity)
    assert loops and {loop["project_id"] for loop in loops} == {PROJECT}


def test_a_caller_files_loops_under_a_project_it_may_read():
    store = _store(sensitivity="private")
    loops = _extract(store, PROJECT, TRUSTED)
    assert loops and {loop["project_id"] for loop in loops} == {PROJECT}


def test_a_project_that_is_not_stored_gives_no_loop_for_every_caller():
    for identity in (None, ADMIN, TRUSTED):
        store = _store()
        assert _extract(store, GHOST, identity) == []
        assert _completion(store) == {"created_count": 0, "source_ids": []}


def _extract_from_sources(store, identity: AgentIdentity | None) -> list[dict]:
    """No project in the request: a loop is filed under the one project its source names in its scope."""

    guard = LabelGuard.unlimited(store) if identity is None else guard_for_caller(store, identity)
    request = ProjectAutomationRequest(agent_identity=None, sensitivity_allowed=ALL, max_items=20)
    return [dict(loop) for loop in VNextProjectService(store).extract_open_loops(request, guard=guard)]


def test_a_source_that_names_a_project_the_caller_may_not_read_or_that_is_not_stored_files_no_loop():
    """The store holds a source scoped to the project and one scoped to a project nobody stored. The caller did not name
    a project, so each loop is filed under the project its source names, judged like a named one."""

    store = _store(sensitivity="confidential")
    assert _extract_from_sources(store, TRUSTED) == []
    assert _completion(store)["created_count"] == 0 and store.open_loops == {}
    loops = _extract_from_sources(store, ADMIN)
    assert loops and {loop["project_id"] for loop in loops} == {PROJECT}


def test_a_source_that_names_a_readable_project_is_filed_under_it():
    store = _store(sensitivity="private")
    loops = _extract_from_sources(store, TRUSTED)
    assert loops and {loop["project_id"] for loop in loops} == {PROJECT}


def test_a_source_scope_that_names_a_project_nobody_stored_files_nothing_for_every_caller():
    for identity in (None, ADMIN, TRUSTED):
        store = _store(sensitivity="private")
        store.sources[:] = [row for row in store.sources if row["id"] == "source-ghost"]
        assert _extract_from_sources(store, identity) == []


def test_a_store_that_keeps_no_projects_files_under_the_name_a_source_scope_gives():
    class WithoutProjects(InMemoryVNextProjectStore):
        get_project = None  # type: ignore[assignment]

    store = WithoutProjects()
    store.sources.extend(_store().sources)
    loops = _extract_from_sources(store, TRUSTED)
    assert {loop["project_id"] for loop in loops} == {PROJECT, GHOST}


def test_a_store_that_keeps_no_projects_admits_the_name():
    """The local SQLite vault has no project rows. A scope names a project there, and extraction files under the name."""

    class WithoutProjects(InMemoryVNextProjectStore):
        get_project = None  # type: ignore[assignment]

    store = WithoutProjects()
    seeded = _store()
    store.sources.extend(seeded.sources)
    loops = _extract(store, GHOST, TRUSTED)
    assert loops and {loop["project_id"] for loop in loops} == {GHOST}


# -- the check the doors share ---------------------------------------------------------------------------------------


def _bound(project_id: str, profile: str = "project_scoped_agent") -> AgentIdentity:
    return AgentIdentity(
        agent_id="alpha", permission_profile=profile, auth="agent_api_key",
        project_scope=(project_id,), project_scope_locked=True,
    )


@pytest.mark.parametrize(
    ("caller", "sensitivity", "domain", "readable"),
    [
        pytest.param(None, "highly_sensitive", "health", True, id="owner reads every project"),
        pytest.param(ADMIN, "highly_sensitive", "health", True, id="unbound admin key reads every project"),
        pytest.param(TRUSTED, "private", "project", True, id="trusted key, private project"),
        pytest.param(TRUSTED, "private", "health", True, id="trusted key, health domain"),
        pytest.param(TRUSTED, "confidential", "project", False, id="trusted key, confidential project"),
        pytest.param(_bound(PROJECT), "private", "project", True, id="bound project key, its own project"),
        pytest.param(_bound(PROJECT), "confidential", "project", False, id="bound project key, confidential"),
        pytest.param(_bound(PROJECT), "public", "health", False, id="bound project key, health domain"),
        pytest.param(_bound(PROJECT), "public", "legal", False, id="bound project key, legal domain"),
        pytest.param(_bound("project-2"), "public", "project", False, id="bound to another project"),
        pytest.param(_bound("project-2", "trusted_local_agent"), "public", "project", False, id="bound trusted key, another project"),
        pytest.param(_bound(PROJECT, "admin_agent"), "confidential", "project", True, id="bound admin key, confidential"),
        pytest.param(_bound(PROJECT, "admin_agent"), "highly_sensitive", "project", True, id="bound admin key, highly sensitive"),
        pytest.param(_bound(PROJECT, "admin_agent"), "regulated", "project", True, id="bound admin key, regulated"),
        pytest.param(_bound(PROJECT, "admin_agent"), "public", "health", True, id="bound admin key, health domain"),
        pytest.param(_bound(PROJECT, "admin_agent"), "public", "legal", True, id="bound admin key, legal domain"),
        pytest.param(_bound("project-2", "admin_agent"), "public", "project", False, id="bound admin key, another project"),
    ],
)
def test_project_readable(caller, sensitivity, domain, readable):
    store = _store(sensitivity=sensitivity, domain=domain)
    assert project_readable(store, store.projects[PROJECT], caller) is readable
    # The extraction, which judges the project with the guard of an operator caller, answers the same for those callers.
    if caller is None or (caller.permission_profile in {"trusted_local_agent", "admin_agent"} and not caller.project_scope_locked):
        loops = _extract(store, PROJECT, caller)
        assert bool(loops) is readable
