"""Open-loop extraction returns only the loops its caller may read now, on the local SQLite store.

The service is shared by the HTTP route (PostgreSQL only), the legacy MCP tool and the command line. The route hands it
the guard of the caller. The tool and the command take no agent identity and the tool is refused whenever a key is
configured, so they hand it the owner's guard. A loop found again by its digest, or returned by the upsert after a
digest conflict, is judged on its effective labels like a new loop, and a loop the caller may not read is left out of
the list and out of the count in the extraction event. The PostgreSQL counterpart, through the mounted application, is
``tests/integration/test_open_loop_extraction_admission_postgres.py``.

Mutations, each one alone (replayed by ``scripts/verify_derived_label_mutations.py``): replace the admission in
``VNextProjectService.extract_open_loops`` with the unfiltered list; count the loops before the admission in the
``open_loop.extraction_completed`` event; give ``guard`` a default in the signature.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import MCPToolNotFoundError, call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_label_guard import LabelGuard, clamp_request_filters, guard_for_caller
from alicebot_api.vnext_projects import (
    ProjectAutomationRequest,
    VNextProjectService,
    _open_loop_candidates,
    _open_loop_digest,
)

USER = UUID("22222222-2222-4222-8222-222222222222")
SENTINEL = "PRIVATE-EDIT-SENTINEL-extract-replay"
TRUSTED = AgentIdentity(agent_id="trusted", permission_profile="trusted_local_agent", auth="agent_api_key")
ADMIN = AgentIdentity(agent_id="admin", permission_profile="admin_agent", auth="agent_api_key")
READ_ONLY = AgentIdentity(agent_id="reader", permission_profile="read_only_agent", auth="agent_api_key")


def _bound(project_id: str) -> AgentIdentity:
    return AgentIdentity(
        agent_id="alpha-reader", permission_profile="project_scoped_agent", auth="agent_api_key",
        project_scope=(project_id,), project_scope_locked=True,
    )


@pytest.fixture
def vault(tmp_path, monkeypatch):
    path = tmp_path / "extract.sqlite3"
    bootstrap_database(path, user_id=str(USER), user_email="synthetic@example.invalid")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)

    class Vault:
        def __init__(self):
            self.path = path

        def store(self):
            return sqlite_user_connection(path, USER)

        def context(self):
            return MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=USER)

    return Vault()


def _source(store, *, todo: str, sensitivity: str, scope=()) -> dict:
    return store.create_source(
        {
            "source_type": "note",
            "title": f"Planning notes {uuid4()}",
            "content_hash": str(uuid4()),
            "domain": "project",
            "sensitivity": sensitivity,
            "metadata_json": {"project_scope": list(scope), "raw_text": f"Planning notes.\nTODO: {todo}"},
        }
    )


def _extract(vault, identity, request: ProjectAutomationRequest, *, action: str = "http.operator.access") -> list[dict]:
    """What the route does: clamp the request to the caller, hand the service the guard of the caller."""

    with vault.store() as conn:
        store = SQLiteVNextStore(conn, USER)
        domains, sensitivity = clamp_request_filters(
            identity, domains=request.domains, sensitivity_allowed=request.sensitivity_allowed, action=action
        )
        guard = guard_for_caller(store, identity, action=action)
        loops = VNextProjectService(store).extract_open_loops(
            replace(request, domains=domains, sensitivity_allowed=sensitivity), guard=guard
        )
        return [dict(loop) for loop in loops]


def _everything() -> ProjectAutomationRequest:
    return ProjectAutomationRequest(agent_identity=None, sensitivity_allowed=tuple(ALL_SENSITIVITY), max_items=50)


def _events(vault) -> list[dict]:
    """The extraction events, oldest first. Events written in the same instant are ordered by id."""

    with vault.store() as conn:
        store = SQLiteVNextStore(conn, USER)
        found = [
            event for event in store.list_events(limit=500) if event.get("event_type") == "open_loop.extraction_completed"
        ]
    return list(reversed(found))


def _counts_since(vault, seen: list[dict]) -> list[int]:
    """The ``created_count`` of the events written after ``seen`` was read, in any order."""

    known = {str(event["id"]) for event in seen}
    return sorted(event["payload_json"]["created_count"] for event in _events(vault) if str(event["id"]) not in known)


def _lowered_confidential_loop(vault) -> dict:
    """The finding: a loop made from a confidential source, then written into by the owner, then its source lowered."""

    todo = f"follow up with the auditors {uuid4()}"
    with vault.store() as conn:
        source = _source(SQLiteVNextStore(conn, USER), todo=todo, sensitivity="confidential")
    made = _extract(vault, ADMIN, _everything())
    assert [loop["sensitivity"] for loop in made] == ["confidential"]
    with vault.store() as conn:
        store = SQLiteVNextStore(conn, USER)
        store.update_open_loop(loop_id=str(made[0]["id"]), patch={"description": SENTINEL}, actor_type="user")
        store.update_source(source_id=str(source["id"]), patch={"sensitivity": "public"}, actor_type="user")
        stored = store.get_open_loop(str(made[0]["id"]))
        assert stored["sensitivity"] == "confidential" and stored["description"] == SENTINEL
        assert store.get_source(str(source["id"]))["sensitivity"] == "public"
    return {"loop": made[0], "source": source, "todo": todo}


def test_the_service_requires_the_guard_of_its_caller():
    parameter = inspect.signature(VNextProjectService.extract_open_loops).parameters["guard"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_a_loop_found_again_above_the_ceiling_is_left_out_of_the_answer_and_the_event_count(vault):
    world = _lowered_confidential_loop(vault)
    seen = _events(vault)
    for identity in (TRUSTED, TRUSTED):
        assert _extract(vault, identity, _everything()) == []
    assert _counts_since(vault, seen) == [0, 0]
    with vault.store() as conn:
        # Finding it again wrote no loop.
        assert len(SQLiteVNextStore(conn, USER).list_open_loops(status=None, limit=20)) == 1
    assert str(world["loop"]["id"]) not in json.dumps(_events(vault), default=str)


@pytest.mark.parametrize("identity", [ADMIN, None], ids=["unbound admin key", "owner"])
def test_the_owner_and_an_unbound_admin_key_are_still_shown_the_existing_loop(vault, identity):
    world = _lowered_confidential_loop(vault)
    seen = _events(vault)
    shown = _extract(vault, identity, _everything())
    assert [str(loop["id"]) for loop in shown] == [str(world["loop"]["id"])]
    assert shown[0]["description"] == SENTINEL and shown[0]["sensitivity"] == "confidential"
    assert _counts_since(vault, seen) == [1]


def test_a_key_with_a_ceiling_still_extracts_a_new_loop_it_may_read_and_is_shown_it_again(vault):
    todo = f"order the kiln parts {uuid4()}"
    with vault.store() as conn:
        _source(SQLiteVNextStore(conn, USER), todo=todo, sensitivity="public")
    first = _extract(vault, TRUSTED, _everything())
    assert [loop["title"] for loop in first] == [todo]
    again = _extract(vault, TRUSTED, _everything())
    assert [str(loop["id"]) for loop in again] == [str(first[0]["id"])]
    assert _counts_since(vault, []) == [1, 1]


def test_a_hidden_loop_does_not_hide_the_loops_beside_it(vault):
    world = _lowered_confidential_loop(vault)
    todo = f"book the venue {uuid4()}"
    with vault.store() as conn:
        _source(SQLiteVNextStore(conn, USER), todo=todo, sensitivity="public")
    shown = _extract(vault, TRUSTED, _everything())
    assert [loop["title"] for loop in shown] == [todo]
    assert str(world["loop"]["id"]) not in json.dumps(shown, default=str) and SENTINEL not in json.dumps(shown, default=str)


def test_a_loop_the_upsert_returns_after_a_digest_conflict_is_admitted_like_any_other(vault, monkeypatch):
    """The lookup by digest filters on the project column, and a loop whose project column was emptied is missed by it.
    The insert then meets the unique digest, and the upsert returns the stored loop, here one above the ceiling."""

    project = str(uuid4())
    todo = f"renew the kiln permit {uuid4()}"
    with vault.store() as conn:
        store = SQLiteVNextStore(conn, USER)
        source = _source(store, todo=todo, sensitivity="public", scope=(project,))
        digest = _open_loop_digest(_open_loop_candidates(source)[0], project_id=project, person_id=None)
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
    original = SQLiteVNextStore.upsert_open_loop_by_automation_digest

    def spy(self, loop, **kwargs):
        row = original(self, loop, **kwargs)
        returned.append(str(row["id"]))
        return row

    monkeypatch.setattr(SQLiteVNextStore, "upsert_open_loop_by_automation_digest", spy)
    request = replace(_everything(), project_id=project)
    assert _extract(vault, TRUSTED, request) == []
    assert returned == [str(stored["id"])], "the loop must come back through the upsert, not the lookup"
    returned.clear()
    shown = _extract(vault, ADMIN, request)
    assert returned == [str(stored["id"])]
    assert [str(loop["id"]) for loop in shown] == [str(stored["id"])] and shown[0]["description"] == SENTINEL


def test_a_loop_whose_domain_the_caller_may_not_read_is_left_out_of_a_key_that_reads_the_source(vault):
    todo = f"renew the policy {uuid4()}"
    with vault.store() as conn:
        _source(SQLiteVNextStore(conn, USER), todo=todo, sensitivity="public")
    request = ProjectAutomationRequest(agent_identity=None, sensitivity_allowed=("public",), max_items=50)
    made = _extract(vault, None, request)
    assert [loop["title"] for loop in made] == [todo]
    assert [str(loop["id"]) for loop in _extract(vault, READ_ONLY, request, action="open_loop.lookup")] == [
        str(made[0]["id"])
    ]
    with vault.store() as conn:
        SQLiteVNextStore(conn, USER).update_open_loop(
            loop_id=str(made[0]["id"]), patch={"domain": "health", "description": SENTINEL}, actor_type="user"
        )
    # The source is still readable by this key, the loop made from it is now in a domain the key may not read.
    assert _extract(vault, READ_ONLY, request, action="open_loop.lookup") == []
    assert [str(loop["id"]) for loop in _extract(vault, None, request)] == [str(made[0]["id"])]


def test_a_loop_in_another_project_is_left_out_of_a_key_bound_to_the_first(vault):
    alpha, beta = str(uuid4()), str(uuid4())
    todo = f"file the alpha report {uuid4()}"
    with vault.store() as conn:
        store = SQLiteVNextStore(conn, USER)
        source = _source(store, todo=todo, sensitivity="public", scope=(alpha,))
        digest = _open_loop_digest(_open_loop_candidates(source)[0], project_id=alpha, person_id=None)
        other = store.create_open_loop(
            {
                "title": todo,
                "description": SENTINEL,
                "domain": "project",
                "sensitivity": "public",
                "project_id": beta,
                "metadata_json": {"project_scope": [beta], "automation_digest": digest, "idempotency_digest": digest},
            }
        )
    request = ProjectAutomationRequest(agent_identity=None, project_id=alpha, sensitivity_allowed=("public",), max_items=50)
    identity = _bound(alpha)
    # Nothing in the request, the source or the loop's sensitivity is above this key: only the project is.
    assert _extract(vault, identity, request, action="open_loop.lookup") == []
    assert [str(loop["id"]) for loop in _extract(vault, None, request)] == [str(other["id"])]
    own_todo = f"book the alpha review {uuid4()}"
    with vault.store() as conn:
        _source(SQLiteVNextStore(conn, USER), todo=own_todo, sensitivity="public", scope=(alpha,))
    shown = _extract(vault, identity, request, action="open_loop.lookup")
    assert [loop["title"] for loop in shown] == [own_todo]
    assert str(other["id"]) not in json.dumps(shown, default=str) and SENTINEL not in json.dumps(shown, default=str)


# -- the doors that take no identity --------------------------------------------------------------------------------


def test_the_legacy_tool_returns_the_owner_every_loop_and_is_refused_to_a_configured_key(vault, monkeypatch):
    world = _lowered_confidential_loop(vault)
    arguments = {"sensitivity_allowed": list(ALL_SENSITIVITY), "max_items": 50}
    payload = call_mcp_tool(vault.context(), name="alice_open_loop_extract", arguments=arguments)
    assert [str(loop["id"]) for loop in payload["open_loops"]] == [str(world["loop"]["id"])]
    assert payload["created_count"] == 1 and payload["open_loops"][0]["description"] == SENTINEL
    # With an agent key configured the tool is not served at all, so no key reads a loop through it.
    monkeypatch.setenv("ALICE_AGENT_API_KEY", "placeholder")
    with pytest.raises(MCPToolNotFoundError):
        call_mcp_tool(vault.context(), name="alice_open_loop_extract", arguments=arguments)
