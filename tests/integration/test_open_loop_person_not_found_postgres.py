"""An open-loop extraction names its person by id, and a person no stored row answers to must not break the door.

``POST /v0/vnext/open-loops/extract`` took ``scope.person_id`` unparsed: a well-formed id of no
person failed the INSERT with a foreign-key error, a value that is not an id failed the digest
lookup's ``::uuid`` cast, and the id of a real person spelled in another accepted spelling made a
second loop for the same source and candidate, because the automation digest was computed from the
raw string. The service now resolves the person before it reads any source: a value that is not an
id and an id of no stored person raise one not-found refusal for every door, and a real person in
any accepted spelling is used in canonical form, so the digest matches the one the canonical
spelling makes and no duplicate loop is stored.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPReferenceNotFoundError, MCPRuntimeContext
from alicebot_api.vnext_capture import SourceCaptureInput, VNextCaptureService
from alicebot_api.vnext_store import PostgresVNextStore
try:
    from alicebot_api.vnext_projects import VNextOpenLoopPersonNotFoundError
except ImportError:  # a build without the refusal shows its answers in the failures below
    class VNextOpenLoopPersonNotFoundError(Exception):
        pass
from tests.integration.test_vnext_live_workspace_api import seed_user

UNKNOWN_ID = "00000000-0000-0000-0000-0000000000e1"
MALFORMED_VALUES = ("no-such-person", "123")

def _call(method: str, path: str, *, body: bytes = b"") -> tuple[int, Any]:
    messages: list[dict[str, object]] = []
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": urlencode({}).encode(), "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": "",
    }
    anyio.run(main_module.app, scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw)


def _post(path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    return _call("POST", path, body=json.dumps(payload).encode())


def _stored_counts(admin_url: str) -> tuple[int, int]:
    with psycopg.connect(admin_url) as conn:
        loops = conn.execute("SELECT count(*) FROM open_loops").fetchone()[0]
        people = conn.execute("SELECT count(*) FROM people").fetchone()[0]
        conn.commit()
    return int(loops), int(people)


def _seed_candidate_source(database_url: str) -> tuple[str, str]:
    """One captured source whose text names a task, so the extractor has a candidate."""

    user_id = seed_user(database_url, email=f"person-extract-{uuid4().hex[:8]}@example.com")
    with user_connection(database_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        captured = VNextCaptureService(store).capture_source(SourceCaptureInput(
            source_type="note", title="Cobalt follow up",
            raw_text="task: order a new cobalt door hinge",
            connector_name="manual", external_id="person-extract-" + uuid4().hex,
        ))
        source_id = str(captured.source_id)
    return str(user_id), source_id


def _extract(app, user_id: str, person_id: str | None) -> tuple[int, Any]:
    payload: dict[str, Any] = {"user_id": user_id, "scope": {"person_id": person_id}}
    return _post("/v0/vnext/open-loops/extract", payload)


@pytest.fixture
def app(migrated_database_urls, monkeypatch):
    url = migrated_database_urls["app"]
    for name, module in list(sys.modules.items()):
        if name.startswith("alicebot_api") and module is not None and hasattr(module, "get_settings"):
            monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url=url))
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")

    class App:
        pass

    handle = App()
    handle.url = url
    handle.admin_url = migrated_database_urls["admin"]
    handle.user_id, handle.source_id = _seed_candidate_source(url)
    return handle


def test_an_unknown_person_id_answers_not_found_and_writes_nothing(app) -> None:
    before = _stored_counts(app.admin_url)
    status, answer = _extract(app, app.user_id, UNKNOWN_ID)
    assert status == 404, answer
    assert answer == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert _stored_counts(app.admin_url) == before


@pytest.mark.parametrize("value", MALFORMED_VALUES)
def test_a_malformed_person_id_answers_not_found_and_writes_nothing(app, value) -> None:
    before = _stored_counts(app.admin_url)
    status, answer = _extract(app, app.user_id, value)
    assert status == 404, answer
    assert answer == {"detail": {"code": "not_found", "message": "The requested resource was not found"}}
    assert _stored_counts(app.admin_url) == before


@pytest.mark.parametrize("spelling", ("canonical", "upper", "urn:uuid"))
def test_a_real_person_in_any_accepted_spelling_makes_no_duplicate_loop(app, spelling) -> None:
    person_id = str(uuid4())
    with user_connection(app.url, UUID(app.user_id)) as conn:
        store = PostgresVNextStore(conn)
        stored = store.create_person({"name": "Cobalt Owner"})
        stored_person_id = str(stored["id"])

    def extract(person_value: str) -> tuple[int, Any]:
        return _extract(app, app.user_id, person_value)

    # The canonical spelling first: one loop for the source's candidate.
    status, first = extract(stored_person_id)
    assert status == 201, first
    assert first["created_count"] == 1, first
    first_loop = first["open_loops"][0]
    assert str(first_loop.get("person_id")) == stored_person_id

    # The same source and candidate in another accepted spelling answers the
    # loop that is already stored under the digest, and makes no second one.
    value = {"canonical": stored_person_id, "upper": stored_person_id.upper(),
             "urn:uuid": "urn:uuid:" + stored_person_id}[spelling]
    status, again = extract(value)
    assert status == 201, again
    assert again["created_count"] == 1, again
    assert str(again["open_loops"][0]["id"]) == str(first_loop["id"])
    assert _stored_counts(app.admin_url)[0] == 1


def test_the_legacy_mcp_tool_answers_not_found_for_an_unknown_and_a_malformed_person(app) -> None:
    for value in (UNKNOWN_ID, "no-such-person", "123"):
        with pytest.raises(MCPReferenceNotFoundError):
            call_mcp_tool(
                MCPRuntimeContext(database_url=app.url, user_id=UUID(app.user_id)),
                name="alice_open_loop_extract",
                arguments={"person_id": value},
            )


def test_the_legacy_mcp_tool_answers_a_malformed_project_id_as_an_unknown_one(app) -> None:
    """The extract tool's project_id answers a malformed value as an unknown one.

    A project id on this door is judged against the stored projects and the
    caller's binding: a project that is not stored files no loop and answers
    without a server error, and a value that is not an id at all is the same
    missing-project answer, never a database error.
    """

    before = _stored_counts(app.admin_url)
    context = MCPRuntimeContext(database_url=app.url, user_id=UUID(app.user_id))
    for value in ("no-such-project", "123", "not even close", str(uuid4())):
        unknown = call_mcp_tool(context, name="alice_open_loop_extract", arguments={"project_id": str(uuid4())})
        malformed = call_mcp_tool(context, name="alice_open_loop_extract", arguments={"project_id": value})
        assert malformed == unknown, (value, malformed, unknown)
        assert malformed.get("created_count") == 0, malformed
    assert _stored_counts(app.admin_url) == before


def test_the_legacy_mcp_tool_makes_no_duplicate_loop_for_another_spelling(app) -> None:
    with user_connection(app.url, UUID(app.user_id)) as conn:
        store = PostgresVNextStore(conn)
        stored_person_id = str(store.create_person({"name": "Cobalt Owner Two"})["id"])
    context = MCPRuntimeContext(database_url=app.url, user_id=UUID(app.user_id))
    first = call_mcp_tool(context, name="alice_open_loop_extract", arguments={"person_id": stored_person_id})
    assert first.get("created_count") == 1, first
    again = call_mcp_tool(context, name="alice_open_loop_extract", arguments={"person_id": stored_person_id.upper()})
    assert again.get("created_count") == 1, again
    assert _stored_counts(app.admin_url)[0] == 1


def test_the_cli_door_answers_not_found_for_an_unknown_and_a_malformed_person(app, capsys) -> None:
    """The CLI runner maps the refusal to its not-found answer and exit code 1."""

    import alicebot_api.cli.runner as cli_runner
    from alicebot_api.cli.automation import _run_vnext_open_loops_extract
    from alicebot_api.cli.models import CLIContext

    context = CLIContext(settings=Settings(database_url=app.url), database_url=app.url, user_id=UUID(app.user_id))
    for value in (UNKNOWN_ID, "no-such-person", "123"):
        with pytest.raises(VNextOpenLoopPersonNotFoundError):
            _run_vnext_open_loops_extract(
                context, argparse.Namespace(domain=[], sensitivity_allowed=None, project_id=None,
                                            person_id=value, max_items=8, generation_mode="deterministic",
                                            model_route_mode=None, model_provider=None, model=None,
                                            model_temperature=0.2, allow_cloud_private=False)
            )
    exit_code = cli_runner.main(["--database-url", app.url, "--user-id", app.user_id,
                                 "vnext", "open-loops", "extract", "--person-id", UNKNOWN_ID])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert '"not_found"' in captured.err or '"not_found"' in captured.out


def test_the_cli_door_makes_no_duplicate_loop_for_another_spelling(app) -> None:
    from alicebot_api.cli.automation import _run_vnext_open_loops_extract
    from alicebot_api.cli.models import CLIContext

    with user_connection(app.url, UUID(app.user_id)) as conn:
        store = PostgresVNextStore(conn)
        stored_person_id = str(store.create_person({"name": "Cobalt Owner Three"})["id"])
    context = CLIContext(settings=Settings(database_url=app.url), database_url=app.url, user_id=UUID(app.user_id))
    args = argparse.Namespace(domain=[], sensitivity_allowed=None, project_id=None, person_id=stored_person_id,
                              max_items=8, generation_mode="deterministic", model_route_mode=None,
                              model_provider=None, model=None, model_temperature=0.2, allow_cloud_private=False)
    first = json.loads(_run_vnext_open_loops_extract(context, args))
    assert first["created_count"] == 1, first
    args.person_id = stored_person_id.upper()
    again = json.loads(_run_vnext_open_loops_extract(context, args))
    assert again["created_count"] == 1, again
    assert _stored_counts(app.admin_url)[0] == 1
