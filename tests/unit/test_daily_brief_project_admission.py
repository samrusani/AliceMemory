"""The daily brief files a loop only under a project its caller may read, on any store.

The brief discovers open loops in the sources it reads and files each under the one project its source names in its scope
(or, for a source with none, the one project the request names). The project is looked up and judged with the caller's
own limits, the caller being the identity the request records and the owner when it records none. A source whose one
project is hidden, not stored or not a project id is filed under none (the project column holds a stored, readable project
and nothing else, and the scope stays in the loop's metadata); a source that names two projects or none is filed under none; a store that keeps no projects (the local SQLite vault) files under the id a scope gives, as it always did.

The PostgreSQL counterpart, through the route, the generation tool and the scheduler, is
``tests/integration/test_daily_brief_project_admission_postgres.py``.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): take the project of the source
without judging it; judge the project as the owner; look a project up on a store that keeps none.
"""
from __future__ import annotations

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from tests.unit.test_vnext_brain import InMemoryVNextBrainStore

PROJECT = "11111111-1111-4111-8111-111111111111"
GHOST = "22222222-2222-4222-8222-222222222222"
TRUSTED = AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent", auth="agent_api_key")
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_api_key")


def _bound(project_id: str, profile: str = "project_scoped_agent") -> AgentIdentity:
    return AgentIdentity(
        agent_id="alpha", permission_profile=profile, auth="agent_api_key",
        project_scope=(project_id,), project_scope_locked=True,
    )


class StoreWithProjects(InMemoryVNextBrainStore):
    def __init__(self) -> None:
        super().__init__()
        self.projects: dict[str, dict[str, object]] = {}

    def get_project(self, project_id: str) -> dict[str, object] | None:
        return self.projects.get(project_id)


def _store(*, sensitivity: str = "private", domain: str = "project", scopes=(PROJECT,)) -> StoreWithProjects:
    store = StoreWithProjects()
    store.projects[PROJECT] = {
        "id": PROJECT, "name": "Kiln Plans", "slug": "kiln-plans", "status": "active", "domain": domain,
        "sensitivity": sensitivity, "current_state": "s",
    }
    store.sources.append(
        {
            "id": "source-1",
            "source_type": "manual_text",
            "title": "Kiln notes",
            "content_hash": "sha256:abc",
            "captured_at": "2026-05-10T08:00:00Z",
            "domain": "project",
            "sensitivity": "private",
            "metadata_json": {"project_scope": list(scopes), "raw_text": "Notes.\nTODO: renew the kiln permit"},
        }
    )
    return store


def _brief(store, identity: AgentIdentity | None, *, projects: tuple[str, ...] = ()) -> dict:
    request = BrainArtifactRequest(
        agent_identity=None if identity is None else identity.to_record(),
        projects=projects,
        generated_for="2026-05-10",
        sensitivity_allowed=("public", "internal", "private", "confidential", "highly_sensitive", "unknown"),
    )
    return dict(VNextBrainService(store).generate_daily_brief(request))


@pytest.mark.parametrize("sensitivity", ["confidential", "highly_sensitive", "regulated"])
def test_a_brief_files_its_loop_under_no_project_for_a_project_the_caller_may_not_read(sensitivity):
    store = _store(sensitivity=sensitivity)
    artifact = _brief(store, TRUSTED)
    assert artifact["artifact_type"] == "daily_brief"
    assert [loop["project_id"] for loop in store.open_loops] == [None]


@pytest.mark.parametrize("identity", [None, ADMIN], ids=["owner", "unbound admin key"])
def test_the_owner_and_an_unbound_admin_key_file_a_loop_under_a_confidential_project(identity):
    store = _store(sensitivity="highly_sensitive")
    _brief(store, identity)
    assert [loop["project_id"] for loop in store.open_loops] == [PROJECT]


def test_a_brief_files_a_loop_under_a_project_the_caller_may_read():
    store = _store(sensitivity="private")
    _brief(store, TRUSTED)
    assert [loop["project_id"] for loop in store.open_loops] == [PROJECT]


@pytest.mark.parametrize(
    ("sensitivity", "domain"),
    [("confidential", "project"), ("highly_sensitive", "project"), ("regulated", "project"), ("public", "health"), ("public", "legal")],
)
def test_a_bound_admin_key_files_a_loop_under_its_own_project(sensitivity, domain):
    store = _store(sensitivity=sensitivity, domain=domain)
    _brief(store, _bound(PROJECT, "admin_agent"), projects=(PROJECT,))
    assert [loop["project_id"] for loop in store.open_loops] == [PROJECT]


def test_a_bound_project_key_files_its_loop_under_no_project_when_the_project_is_confidential():
    store = _store(sensitivity="confidential")
    _brief(store, _bound(PROJECT), projects=(PROJECT,))
    assert [loop["project_id"] for loop in store.open_loops] == [None]


@pytest.mark.parametrize("scope", [GHOST, "Kiln Plans", "not-a-uuid"], ids=["not stored", "free-form", "malformed"])
@pytest.mark.parametrize("identity", [None, ADMIN, TRUSTED], ids=["owner", "unbound admin key", "trusted key"])
def test_a_scope_that_names_no_stored_project_is_filed_under_none_for_every_caller(scope, identity):
    """The in-memory store accepts any id, so a free-form name is looked up and found missing, which is the answer a
    uuid that is not stored gets."""

    store = _store(scopes=(scope,))
    _brief(store, identity)
    assert [loop["project_id"] for loop in store.open_loops] == [None]


def test_a_hidden_project_in_one_source_does_not_hide_the_loop_of_another():
    store = _store(sensitivity="confidential")
    other = "33333333-3333-4333-8333-333333333333"
    store.projects[other] = {**store.projects[PROJECT], "id": other, "name": "Shown", "sensitivity": "private"}
    store.sources.append(
        {**store.sources[0], "id": "source-2", "metadata_json": {"project_scope": [other], "raw_text": "Notes.\nTODO: book the venue"}}
    )
    _brief(store, TRUSTED)
    assert sorted(str(loop["project_id"]) for loop in store.open_loops) == sorted([other, "None"])


def test_a_source_that_names_two_projects_or_none_is_filed_under_none():
    store = _store(sensitivity="confidential", scopes=(PROJECT, GHOST))
    _brief(store, TRUSTED)
    assert [loop["project_id"] for loop in store.open_loops] == [None]
    store = _store(scopes=())
    _brief(store, TRUSTED)
    assert [loop["project_id"] for loop in store.open_loops] == [None]


def test_a_store_that_keeps_no_projects_files_under_the_uuid_a_scope_gives():
    """The local SQLite vault has no project rows and no ``get_project``: nothing is looked up, and a scope that is a uuid
    is filed under, a free-form name under none, as before."""

    store = InMemoryVNextBrainStore()
    store.sources.extend(_store(scopes=(GHOST,)).sources)
    store.sources.append({**store.sources[0], "id": "source-2", "metadata_json": {"project_scope": ["Kiln Plans"], "raw_text": "Notes.\nTODO: pay the kiln invoice"}})
    _brief(store, TRUSTED)
    assert sorted(str(loop["project_id"]) for loop in store.open_loops) == sorted([GHOST, "None"])
