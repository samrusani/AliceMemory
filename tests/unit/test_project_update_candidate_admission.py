"""The project update service judges its caller on any store, and the local SQLite store has no project path at all.

The service is shared by the HTTP route and the legacy MCP tool (PostgreSQL), the command line and the scheduler. A door
hands it the identity it resolved. The service reads the project first and answers a project the caller may not read
exactly as a project that does not exist, and it judges every artifact that comes back (a new one, one found again by its
digest and the row an upsert returns) through the check the artifact get applies, before the creation event is recorded.
A call that passes no identity is judged as the identity its request records, and as the owner when it records none.

These tests run the service over the in-memory project store of ``test_vnext_projects.py``. The PostgreSQL counterpart,
through the mounted application and the tool handler, is
``tests/integration/test_project_update_candidate_admission_postgres.py``.

SQLite: ``SQLiteVNextStore`` has no ``get_project``, ``list_projects`` or ``create_project`` and the SQLite schema has no
projects table, so no project update candidate can be made on a SQLite vault. The legacy tool answers an error there
(``test_a_sqlite_vault_has_no_project_path``) and nothing is returned. When SQLite gains projects, that test fails first
and the service tests here already cover the admission the SQLite store would use.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): admit every project; admit the
artifact that is made, found or upserted without judging it; give the service a caller of its own that is always the owner.
"""
from __future__ import annotations

import inspect
import json
from argparse import Namespace
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from alicebot_api.cli import automation as cli_automation
from alicebot_api.cli.models import CLIContext
from alicebot_api.routers._vnext_automation import VNextProjectAutomationRequest, _vnext_project_automation_request
from alicebot_api.vnext_agent_control import AgentIdentity, evaluate_agent_policy
from alicebot_api.vnext_label_guard import outside_caller_limits
from alicebot_api.vnext_projects import ProjectAutomationRequest, VNextProjectService, VNextProjectValidationError
from tests.unit.test_vnext_projects import InMemoryVNextProjectStore, _seed_store

USER = UUID("44444444-4444-4444-8444-444444444444")
NAME = "Alice vNext"
PROJECT = "project-1"
TRUSTED = AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent", auth="agent_api_key")
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_api_key")


def _bound(project_id: str, profile: str = "project_scoped_agent") -> AgentIdentity:
    return AgentIdentity(
        agent_id="alpha-writer", permission_profile=profile, auth="agent_api_key",
        project_scope=(project_id,), project_scope_locked=True,
    )


def _store(*, sensitivity: str = "private", domain: str = "project") -> InMemoryVNextProjectStore:
    store = _seed_store()
    store.projects[PROJECT].update({"sensitivity": sensitivity, "domain": domain})
    return store


def _request(identity: AgentIdentity | None, project_id: str | None) -> ProjectAutomationRequest:
    """What the route builds: the request cut down to the policy decision of the caller, with the caller's record."""

    decision = evaluate_agent_policy(identity=identity, action="artifact.generate") if identity is not None else None
    assert decision is None or decision.decision != "blocked", decision
    options = {} if project_id is None else {"project_id": project_id}
    return _vnext_project_automation_request(
        VNextProjectAutomationRequest(user_id=USER, scope={}, options=options), identity=identity, decision=decision
    )


def _generate(store, identity, project_id=None, *, pass_identity: bool = True) -> dict:
    service = VNextProjectService(store)
    request = _request(identity, project_id)
    return dict(
        service.generate_project_update_candidate(request, identity=identity)
        if pass_identity
        else service.generate_project_update_candidate(request)
    )


def _refusal(store, identity, project_id=None, **kwargs) -> str:
    with pytest.raises(VNextProjectValidationError) as refused:
        _generate(store, identity, project_id, **kwargs)
    return str(refused.value)


def _written(store: InMemoryVNextProjectStore) -> tuple[int, int, int]:
    return len(store.artifacts), len(store.memories), len(store.events)


def test_the_service_takes_the_identity_of_its_caller_and_defaults_to_the_record_of_the_request():
    parameter = inspect.signature(VNextProjectService.generate_project_update_candidate).parameters["identity"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is None


HIDDEN = [
    pytest.param("bound", "confidential", "project", id="bound project key, confidential"),
    pytest.param("bound", "highly_sensitive", "project", id="bound project key, highly sensitive"),
    pytest.param("bound", "public", "health", id="bound project key, health domain"),
    pytest.param("trusted", "confidential", "project", id="unbound trusted key, confidential"),
    pytest.param("trusted", "regulated", "project", id="unbound trusted key, regulated"),
]


@pytest.mark.parametrize(("kind", "sensitivity", "domain"), HIDDEN)
@pytest.mark.parametrize("pass_identity", [True, False], ids=["identity passed", "identity from the request record"])
def test_a_project_the_caller_may_not_read_is_answered_as_a_project_that_does_not_exist(
    kind, sensitivity, domain, pass_identity
):
    store = _store(sensitivity=sensitivity, domain=domain)
    identity = _bound(PROJECT) if kind == "bound" else TRUSTED
    asked = None if kind == "bound" else PROJECT
    ghost_identity = _bound("project-ghost") if kind == "bound" else TRUSTED
    missing = _refusal(store, ghost_identity, None if kind == "bound" else "project-ghost")
    before = _written(store)

    refused = _refusal(store, identity, asked, pass_identity=pass_identity)

    assert refused.replace(PROJECT, "<id>") == missing.replace("project-ghost", "<id>"), refused
    assert NAME not in refused
    assert _written(store) == before


def test_a_caller_bound_to_another_project_is_answered_as_a_missing_project():
    store = _store(sensitivity="public")
    store.projects["project-2"] = {**store.projects[PROJECT], "id": "project-2", "name": "Other", "slug": "other"}
    before = _written(store)
    for profile in ("project_scoped_agent", "trusted_local_agent"):
        identity = _bound("project-2", profile)
        # The request builder of the route refuses a project outside the binding (a ValueError, HTTP 400). A door that
        # builds the request itself, as the tool does, gets the same refusal from the service.
        request = ProjectAutomationRequest(
            agent_identity=identity.to_record(), project_id=PROJECT, sensitivity_allowed=("public", "private")
        )
        with pytest.raises(VNextProjectValidationError) as refused:
            VNextProjectService(store).generate_project_update_candidate(request, identity=identity)
        assert NAME not in str(refused.value)
    assert _written(store) == before


@pytest.mark.parametrize("identity", [None, ADMIN], ids=["owner", "unbound admin key"])
def test_the_owner_and_an_unbound_admin_key_are_shown_the_confidential_project(identity):
    store = _store(sensitivity="confidential")
    artifact = _generate(store, identity, PROJECT)
    assert NAME in artifact["content_markdown"] and artifact["sensitivity"] == "confidential"


@pytest.mark.parametrize("kind", ["bound", "trusted"])
def test_a_caller_still_gets_a_candidate_for_a_project_it_may_read_and_is_shown_it_again(kind):
    store = _store(sensitivity="private")
    identity = _bound(PROJECT) if kind == "bound" else TRUSTED
    asked = None if kind == "bound" else PROJECT
    first = _generate(store, identity, asked)
    assert first["artifact_type"] == "project_update" and NAME in first["content_markdown"]
    assert not outside_caller_limits(store, identity, "artifact", store.artifacts[str(first["id"])])
    created = [event for event in store.events if event["event_type"] == "project.update_candidate_created"]
    assert [event["target_id"] for event in created] == [first["id"]]
    again = _generate(store, identity, asked)
    assert again["id"] == first["id"] and len(store.artifacts) == 1


@pytest.mark.parametrize("kind", ["bound", "trusted"])
def test_an_artifact_found_again_above_the_ceiling_is_not_returned_and_no_event_records_it(kind):
    store = _store(sensitivity="private")
    identity = _bound(PROJECT) if kind == "bound" else TRUSTED
    asked = None if kind == "bound" else PROJECT
    first = _generate(store, identity, asked)
    store.artifacts[str(first["id"])]["sensitivity"] = "confidential"
    before = _written(store)

    refused = _refusal(store, identity, asked)

    assert NAME not in refused and str(first["id"]) not in refused and "Project Update Candidate" not in refused
    assert _written(store) == before


def test_the_owner_and_an_unbound_admin_key_are_shown_the_artifact_found_again():
    store = _store(sensitivity="private")
    first = _generate(store, None, PROJECT)
    store.artifacts[str(first["id"])]["sensitivity"] = "highly_sensitive"
    assert _generate(store, None, PROJECT)["id"] == first["id"]
    first_admin = _generate(store, ADMIN, PROJECT)
    store.artifacts[str(first_admin["id"])]["sensitivity"] = "highly_sensitive"
    assert _generate(store, ADMIN, PROJECT)["id"] == first_admin["id"]


def test_a_row_the_scoped_lookup_misses_and_the_upsert_would_return_is_judged_before_anything_is_written():
    """The scoped lookup misses, the insert would meet the unique digest, and the upsert would return the stored row. That
    row is judged first, so a refusal leaves no candidate memory and no artifact written (or staged) behind."""

    store = _store(sensitivity="private")
    first = _generate(store, TRUSTED, PROJECT)
    stored = store.artifacts[str(first["id"])]
    stored["sensitivity"] = "confidential"
    real_find = store.find_artifact_by_workflow_digest
    store.find_artifact_by_workflow_digest = lambda **kwargs: None if kwargs.get("scope_projects") else real_find(**kwargs)  # type: ignore[method-assign]
    upserted: list[str] = []

    def upsert(artifact, **_kwargs):
        upserted.append(str(stored["id"]))
        return stored

    store.upsert_artifact_by_workflow_digest = upsert  # type: ignore[attr-defined]
    before = _written(store)

    refused = _refusal(store, TRUSTED, PROJECT)

    assert upserted == [], "the row is refused before the write that would return it"
    assert NAME not in refused and str(stored["id"]) not in refused
    assert _written(store) == before
    # The unbound admin key is shown that row, which comes back through the upsert.
    assert _generate(store, ADMIN, PROJECT)["id"] == str(stored["id"])
    assert upserted == [str(stored["id"])]


def test_a_row_the_upsert_returns_that_no_lookup_found_is_judged_on_its_own_labels():
    store = _store(sensitivity="private")
    first = _generate(store, TRUSTED, PROJECT)
    stored = store.artifacts[str(first["id"])]
    stored["sensitivity"] = "confidential"
    store.find_artifact_by_workflow_digest = lambda **_kwargs: None  # type: ignore[method-assign]
    store.upsert_artifact_by_workflow_digest = lambda artifact, **_kwargs: stored  # type: ignore[attr-defined]
    created = [event for event in store.events if event["event_type"] == "project.update_candidate_created"]

    refused = _refusal(store, TRUSTED, PROJECT)

    assert NAME not in refused and str(stored["id"]) not in refused
    assert [event for event in store.events if event["event_type"] == "project.update_candidate_created"] == created
    assert _generate(store, ADMIN, PROJECT)["id"] == str(stored["id"])


def test_a_new_artifact_is_judged_on_its_own_labels_when_the_store_has_not_written_it_yet():
    """The scheduler stages its writes and publishes them with the run, so the artifact it was just handed back, and the
    candidate memory it was made from, are not in the database to be read. A readable candidate is returned all the same."""

    store = _store(sensitivity="private")
    store.create_artifact = lambda artifact, **_kwargs: {**artifact, "id": "staged-artifact"}  # type: ignore[method-assign]
    store.create_memory = lambda memory, **_kwargs: {**memory, "id": "staged-memory"}  # type: ignore[method-assign]
    for identity in (TRUSTED, _bound(PROJECT), _bound(PROJECT, "trusted_local_agent")):
        artifact = _generate(store, identity, None if identity.project_scope_locked else PROJECT)
        assert artifact["id"] == "staged-artifact" and NAME in artifact["content_markdown"]
    assert store.artifacts == {} and store.memories.keys() == {"memory-existing"}


@pytest.mark.parametrize(
    ("sensitivity", "domain"),
    [("confidential", "project"), ("highly_sensitive", "project"), ("regulated", "project"), ("public", "health"), ("public", "legal")],
)
def test_a_bound_admin_key_is_given_a_candidate_for_its_own_project(sensitivity, domain):
    store = _store(sensitivity=sensitivity, domain=domain)
    admin = _bound(PROJECT, "admin_agent")
    artifact = _generate(store, admin, None)
    assert NAME in artifact["content_markdown"] and artifact["metadata_json"]["project_id"] == PROJECT
    assert _generate(store, admin, None)["id"] == artifact["id"]
    # Another project is outside the binding.
    store.projects["project-2"] = {**store.projects[PROJECT], "id": "project-2", "name": "Other", "slug": "other"}
    request = ProjectAutomationRequest(agent_identity=admin.to_record(), project_id="project-2", sensitivity_allowed=("public",))
    with pytest.raises(VNextProjectValidationError):
        VNextProjectService(store).generate_project_update_candidate(request, identity=admin)


def test_a_new_artifact_above_the_ceiling_is_not_returned_and_no_event_records_it():
    store = _store(sensitivity="private")
    original = store.create_artifact

    def raised(artifact, **kwargs):
        return {**original(artifact, **kwargs), "sensitivity": "confidential"}

    store.create_artifact = raised  # type: ignore[method-assign]
    events = len(store.events)
    refused = _refusal(store, TRUSTED, PROJECT)
    assert NAME not in refused and "Project Update Candidate" not in refused
    assert len(store.events) == events
    assert _generate(store, ADMIN, PROJECT)["sensitivity"] == "confidential"


def test_a_call_with_no_identity_is_judged_as_the_identity_its_request_records():
    """The scheduler hands the service a request that records the identity of its caller and passes no identity."""

    store = _store(sensitivity="confidential")
    request = ProjectAutomationRequest(
        agent_identity=TRUSTED.to_record(), project_id=PROJECT, sensitivity_allowed=("public", "private")
    )
    with pytest.raises(VNextProjectValidationError):
        VNextProjectService(store).generate_project_update_candidate(request)
    owner = ProjectAutomationRequest(agent_identity=None, project_id=PROJECT)
    assert NAME in VNextProjectService(store).generate_project_update_candidate(owner)["content_markdown"]


def test_a_project_the_listing_picks_is_judged_too():
    """With no project named the service lists the projects; one the caller may not read is not the one it picks.

    The request here asks for every sensitivity. A request filter is a selection and never a grant, so the listing
    returns the confidential project and the service refuses it on the limits of the caller."""

    wide = ("public", "internal", "private", "confidential", "highly_sensitive", "unknown")
    store = _store(sensitivity="confidential")
    request = ProjectAutomationRequest(agent_identity=TRUSTED.to_record(), sensitivity_allowed=wide)
    with pytest.raises(VNextProjectValidationError) as refused:
        VNextProjectService(store).generate_project_update_candidate(request, identity=TRUSTED)
    assert "no active project" in str(refused.value) and NAME not in str(refused.value)
    assert _written(store) == _written(_store(sensitivity="confidential"))
    shown = VNextProjectService(store).generate_project_update_candidate(
        ProjectAutomationRequest(agent_identity=None, sensitivity_allowed=wide)
    )
    assert NAME in shown["content_markdown"]
    # With a readable project beside it the listing still finds a project to make the candidate for.
    store.projects["project-2"] = {
        **store.projects[PROJECT], "id": "project-2", "name": "Open Kiln", "slug": "open-kiln", "sensitivity": "public",
    }
    store.projects[PROJECT]["status"] = "archived"
    request = ProjectAutomationRequest(agent_identity=TRUSTED.to_record(), sensitivity_allowed=wide)
    picked = VNextProjectService(store).generate_project_update_candidate(request, identity=TRUSTED)
    assert "Open Kiln" in picked["content_markdown"]


def _ordered(store):
    """Newest ``updated_at`` first, and record each limit the scan asks for."""

    store.list_limits = []
    real = store.list_projects

    def list_projects(**kwargs):
        limit = kwargs.get("limit", 8)
        store.list_limits.append(limit)
        rows = real(**{**kwargs, "limit": 10_000})
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return rows[:limit]

    store.list_projects = list_projects  # type: ignore[method-assign]
    return store


def _beside(store, project_id, *, name, updated_at, sensitivity="public", domain="project"):
    store.projects[project_id] = {
        **store.projects[PROJECT],
        "id": project_id,
        "name": name,
        "slug": project_id,
        "sensitivity": sensitivity,
        "domain": domain,
        "status": "active",
        "updated_at": updated_at,
    }


WIDE = ("public", "internal", "private", "unknown", "confidential", "highly_sensitive", "regulated")


def test_a_newer_project_the_caller_may_not_read_does_not_hide_an_older_one():
    """The request asks for every sensitivity. That is a selection, not a grant: the newest project is confidential."""

    store = _ordered(_store(sensitivity="public"))
    store.projects[PROJECT]["updated_at"] = "2026-01-01T00:00:00Z"
    store.projects[PROJECT]["name"] = "Older Kiln"
    _beside(store, "project-new", name="Newer Kiln", updated_at="2026-02-01T00:00:00Z", sensitivity="confidential")
    request = ProjectAutomationRequest(agent_identity=TRUSTED.to_record(), sensitivity_allowed=WIDE)
    picked = VNextProjectService(store).generate_project_update_candidate(request, identity=TRUSTED)
    assert "Older Kiln" in picked["content_markdown"] and "Newer Kiln" not in picked["content_markdown"]
    assert store.list_limits == [1, 128]


def test_the_owner_and_an_unbound_admin_still_get_the_newest_project():
    store = _ordered(_store(sensitivity="confidential"))
    store.projects[PROJECT]["updated_at"] = "2026-02-01T00:00:00Z"
    store.projects[PROJECT]["name"] = "Newer Kiln"
    _beside(store, "project-old", name="Older Kiln", updated_at="2026-01-01T00:00:00Z", sensitivity="public")
    for identity in (None, ADMIN):
        store.list_limits.clear()
        request = ProjectAutomationRequest(
            agent_identity=None if identity is None else identity.to_record(), sensitivity_allowed=WIDE
        )
        picked = VNextProjectService(store).generate_project_update_candidate(request, identity=identity)
        assert "Newer Kiln" in picked["content_markdown"]
        assert store.list_limits == [1]


def test_a_bound_key_is_given_the_project_it_may_read_when_a_newer_one_is_hidden():
    store = _ordered(_store(sensitivity="public"))
    store.projects[PROJECT]["updated_at"] = "2026-01-01T00:00:00Z"
    store.projects[PROJECT]["name"] = "Older Kiln"
    _beside(store, "project-new", name="Newer Kiln", updated_at="2026-02-01T00:00:00Z", sensitivity="confidential")
    identity = _bound(PROJECT, "trusted_local_agent")
    request = ProjectAutomationRequest(agent_identity=identity.to_record(), sensitivity_allowed=WIDE)
    picked = VNextProjectService(store).generate_project_update_candidate(request, identity=identity)
    assert "Older Kiln" in picked["content_markdown"] and "Newer Kiln" not in picked["content_markdown"]


def test_a_bound_admin_key_still_gets_its_own_newer_project():
    store = _ordered(_store(sensitivity="confidential"))
    store.projects[PROJECT]["updated_at"] = "2026-02-01T00:00:00Z"
    _beside(store, "project-old", name="Older Kiln", updated_at="2026-01-01T00:00:00Z", sensitivity="public")
    identity = _bound(PROJECT, "admin_agent")
    request = ProjectAutomationRequest(agent_identity=identity.to_record(), sensitivity_allowed=("public", "confidential"))
    picked = VNextProjectService(store).generate_project_update_candidate(request, identity=identity)
    assert NAME in picked["content_markdown"] and "Older Kiln" not in picked["content_markdown"]


def test_only_hidden_projects_answer_as_a_vault_with_no_project():
    empty = _ordered(_store(sensitivity="public"))
    empty.projects.clear()
    missing = _refusal(empty, TRUSTED, None)
    hidden = _ordered(_store(sensitivity="confidential"))
    hidden.projects[PROJECT]["updated_at"] = "2026-02-01T00:00:00Z"
    request = ProjectAutomationRequest(agent_identity=TRUSTED.to_record(), sensitivity_allowed=WIDE)
    with pytest.raises(VNextProjectValidationError) as refused:
        VNextProjectService(hidden).generate_project_update_candidate(request, identity=TRUSTED)
    assert str(refused.value) == missing
    assert _written(hidden) == _written(_store(sensitivity="confidential"))


def test_the_scan_stops_after_128_hidden_projects():
    """129 hidden rows would make a caller slow on purpose. The 129th is not judged, so the older project is not found."""

    from alicebot_api.vnext_label_sql import READABLE_SCAN_CAP

    store = _ordered(_store(sensitivity="public"))
    store.projects[PROJECT]["updated_at"] = "2026-01-01T00:00:00Z"
    store.projects[PROJECT]["name"] = "Older Kiln"
    for index in range(READABLE_SCAN_CAP):
        _beside(
            store, f"hidden-{index}", name=f"Hidden {index}", updated_at=f"2026-03-01T00:{index // 60:02d}:{index % 60:02d}Z",
            sensitivity="confidential",
        )
    request = ProjectAutomationRequest(agent_identity=TRUSTED.to_record(), sensitivity_allowed=WIDE)
    with pytest.raises(VNextProjectValidationError) as refused:
        VNextProjectService(store).generate_project_update_candidate(request, identity=TRUSTED)
    assert "Older Kiln" not in str(refused.value) and "no active project" in str(refused.value)
    assert store.list_limits == [1, READABLE_SCAN_CAP]
    # One fewer hidden row and the older project is inside the cap.
    del store.projects[f"hidden-{READABLE_SCAN_CAP - 1}"]
    store.list_limits.clear()
    picked = VNextProjectService(store).generate_project_update_candidate(request, identity=TRUSTED)
    assert "Older Kiln" in picked["content_markdown"]
    assert max(store.list_limits) == READABLE_SCAN_CAP


def test_the_scheduler_scan_skips_a_newer_project_the_caller_may_not_read():
    from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService

    store = _ordered(_store(sensitivity="public"))
    store.projects[PROJECT]["updated_at"] = "2026-01-01T00:00:00Z"
    store.projects[PROJECT]["name"] = "Older Kiln"
    _beside(store, "project-new", name="Newer Kiln", updated_at="2026-02-01T00:00:00Z", sensitivity="confidential")
    request = SchedulerRunRequest(
        workflow_type="project_update_scan",
        agent_identity=TRUSTED,
        sensitivity_allowed=WIDE,
    )
    made = VNextSchedulerService(store)._generate_project_update_scan_artifact(request, metadata={})
    assert "Older Kiln" in made["content_markdown"] and "Newer Kiln" not in made["content_markdown"]
    named = SchedulerRunRequest(
        workflow_type="project_update_scan",
        projects=("project-new", PROJECT),
        agent_identity=TRUSTED,
        sensitivity_allowed=WIDE,
    )
    made = VNextSchedulerService(store)._generate_project_update_scan_artifact(named, metadata={})
    assert "Older Kiln" in made["content_markdown"] and "Newer Kiln" not in made["content_markdown"]


def test_the_identity_a_door_passes_decides_whatever_the_request_records():
    """The request here records no caller, so it would run for the owner; the door passed the identity it resolved."""

    store = _store(sensitivity="confidential")
    request = ProjectAutomationRequest(agent_identity=None, project_id=PROJECT, sensitivity_allowed=("public", "private"))
    with pytest.raises(VNextProjectValidationError):
        VNextProjectService(store).generate_project_update_candidate(request, identity=TRUSTED)
    assert NAME in VNextProjectService(store).generate_project_update_candidate(request, identity=ADMIN)["content_markdown"]


def test_the_command_runs_for_the_owner(monkeypatch):
    store = _store(sensitivity="confidential")

    @contextmanager
    def store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_automation, "_vnext_store_context", store_context)
    monkeypatch.setattr(cli_automation, "_vnext_sensitivity_allowed", lambda _args: ("public", "internal", "private", "unknown"))
    args = Namespace(
        domain=[], project_id=PROJECT, person_id=None, max_items=8, generation_mode="deterministic",
        model_route_mode=None, model_provider=None, model=None, model_temperature=0.2, allow_cloud_private=False,
    )
    printed = json.loads(cli_automation._run_vnext_project_update_candidate(CLIContext(settings=None, database_url="x", user_id=USER), args))
    assert printed["artifact_type"] == "project_update" and NAME in printed["content_markdown"]


def test_a_sqlite_vault_has_no_project_path(tmp_path, monkeypatch):
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    path = tmp_path / "project-update.sqlite3"
    bootstrap_database(path, user_id=str(USER), user_email="synthetic@example.invalid")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        assert not any(hasattr(store, name) for name in ("get_project", "list_projects", "create_project"))
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    assert "projects" not in tables
    with pytest.raises(AttributeError, match="list_projects"):
        call_mcp_tool(
            MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=USER),
            name="alice_project_update_candidate",
            arguments={},
        )
