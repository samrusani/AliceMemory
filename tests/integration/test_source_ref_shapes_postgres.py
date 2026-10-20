"""PostgreSQL: every door that stores ``source_refs`` gives one answer to a shape a producer never writes.

Producers write ``source:<id>``, ``memory:<id>``, a bare id or a label such as a URL. A client can send an object, a number,
a boolean, null, a blank string, a nested array, or the text of a JSON object or array (an object in disguise). These tests
drive the mounted application and the command line against PostgreSQL and count the rows before and after:

* ``POST /v0/vnext/connectors/agent_output/sync`` and ``alicebot vnext connectors ingest agent_output FILE`` reach
  ``sync_items`` and never ``ingest_agent_output``; they stored every one of these shapes in ``metadata_json.source_refs`` and
  again in ``metadata_json.raw_payload.source_refs`` before the check moved into the item normalizer;
* the three typed routes (commit, proposal, agent output ingest) answer HTTP 422 for an object and for JSON text, and HTTP 400
  for a blank entry, and the proposal route's 400 names the rule that was broken;
* the agent-output MCP tool refuses a blank entry and JSON text before it writes anything.

Mutations, each one alone: the shape check taken out of ``_normalize_agent_output_item`` (the sync and the command line tests
fail); ``SourceRefText`` replaced by ``str`` in a request model (the 422 test fails for that route); the ``detail`` of the proposal
route put back to its old text (the 400 test fails).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPArgumentError, MCPRuntimeContext
from tests.integration.test_cli_integration import REPO_ROOT, build_cli_env
from tests.integration.test_json_bounds_postgres import AGENT, _call, _post, _stored, app  # noqa: F401  (``app`` is a fixture)

A_QUOTE = {"memory_id": str(uuid4()), "quote": "words of a memory"}
REFUSED = (
    pytest.param(A_QUOTE, id="object"),
    pytest.param(3, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param(None, id="null"),
    pytest.param("", id="blank"),
    pytest.param("   ", id="spaces"),
    pytest.param(["nested"], id="nested array"),
    pytest.param(json.dumps(A_QUOTE), id="json object text"),
    pytest.param(json.dumps([A_QUOTE]), id="json array text"),
)
LABELS = ["source:abc", "memory:def", "kiln-notes", "https://example.test/kiln", "[draft] notes"]


def _item(refs: object, name: str = "one") -> dict[str, object]:
    return {"agent_id": "hermes", "agent_type": "personal_assistant", "title": f"Output {name}",
            "content": f"An output about the cobalt door, {name}.", "external_id": f"out-{name}-{uuid4()}", "source_refs": refs}


def _stored_refs(app) -> list[tuple[object, object]]:
    with psycopg.connect(app.admin_url) as conn:
        rows = conn.execute("SELECT metadata_json FROM sources").fetchall()
    return [(row[0].get("source_refs"), (row[0].get("raw_payload") or {}).get("source_refs")) for row in rows]


@pytest.mark.parametrize("bad", REFUSED)
def test_the_sync_route_refuses_every_shape_the_other_doors_refuse(app, bad: object) -> None:
    before = _stored(app)
    status, answer = _post("/v0/vnext/connectors/agent_output/sync", {"user_id": app.user, "items": [_item(["source:abc", bad])]})
    assert status == 400, answer
    assert (answer["status"], answer["imported_count"], answer["failed_count"]) == ("failed", 0, 1)
    assert _stored(app) == before
    assert _stored_refs(app) == []

    status, answer = _post("/v0/vnext/connectors/agent_output/sync", {"user_id": app.user, "items": [_item([bad], "bad"), _item(LABELS, "good")]})
    assert status == 207, answer
    assert (answer["status"], answer["imported_count"], answer["failed_count"]) == ("partial", 1, 1)
    assert _stored(app)[2] == before[2] + 1
    assert _stored_refs(app) == [(LABELS, LABELS)]


@pytest.mark.parametrize("bad", REFUSED)
def test_the_command_line_ingest_refuses_every_shape_the_other_doors_refuse(app, tmp_path, bad: object) -> None:
    refused_file = tmp_path / "refused.json"
    refused_file.write_text(json.dumps({"items": [_item(["source:abc", bad])]}), encoding="utf-8")
    good_file = tmp_path / "good.json"
    good_file.write_text(json.dumps({"items": [_item(LABELS, "good")]}), encoding="utf-8")
    env = build_cli_env(database_url=app.url, user_id=app.user_id)
    command = [sys.executable, "-m", "alicebot_api", "vnext", "connectors", "ingest", "agent_output"]

    before = _stored(app)
    refused = subprocess.run([*command, str(refused_file)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
    assert refused.returncode != 0, refused.stdout
    answer = json.loads(refused.stdout)
    assert (answer["status"], answer["imported_count"], answer["failed_count"]) == ("failed", 0, 1)
    assert _stored(app) == before
    assert _stored_refs(app) == []

    stored = subprocess.run([*command, str(good_file)], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)
    assert stored.returncode == 0, stored.stderr
    assert _stored_refs(app) == [(LABELS, LABELS)]


def _commit(app, refs: list[object]) -> dict[str, Any]:
    return {"user_id": app.user, "agent": AGENT, "title": "t", "canonical_text": "A fact about the cobalt door.", "source_refs": refs}


def _proposal(app, refs: list[object]) -> dict[str, Any]:
    return {"user_id": app.user, "agent": AGENT, "title": "t", "canonical_text": "A proposal about the cobalt door.", "source_refs": refs}


def _ingest(app, refs: list[object]) -> dict[str, Any]:
    return {"user_id": app.user, "agent_id": "hermes", "agent_type": "personal_assistant", "title": "Output",
            "content": "An output about the cobalt door.", "project_scope": ["Alice"], "source_refs": refs}


ROUTES = (
    pytest.param("/v0/vnext/memories/commit", _commit, id="commit"),
    pytest.param("/v0/vnext/memory-proposals", _proposal, id="proposal"),
    pytest.param("/v0/vnext/agents/ingest-output", _ingest, id="agent output ingest"),
)


def _everything(app) -> tuple[int, ...]:
    with psycopg.connect(app.admin_url) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("memories", "memory_revisions", "event_log", "sources", "source_chunks", "provenance_links")
        )


@pytest.mark.parametrize("path,body", ROUTES)
@pytest.mark.parametrize("bad", (A_QUOTE, json.dumps(A_QUOTE), json.dumps([A_QUOTE]), "[]", "  {}  "), ids=("object", "json object text", "json array text", "empty array text", "padded empty object text"))
def test_the_typed_routes_answer_json_text_as_they_answer_an_object(app, path: str, body, bad: object) -> None:
    before = _everything(app)
    status, answer = _post(path, body(app, ["source:abc", bad]))
    assert status == 422, answer
    assert [error["loc"] for error in answer["detail"]] == [["body", "source_refs", 1]]
    assert _everything(app) == before


@pytest.mark.parametrize("path,body", ROUTES)
@pytest.mark.parametrize("blank", ("", "   ", "\n\t"))
def test_the_typed_routes_answer_a_blank_entry_with_a_400_and_write_nothing(app, path: str, body, blank: str) -> None:
    before = _everything(app)
    status, answer = _post(path, body(app, ["source:abc", blank]))
    assert status == 400, answer
    assert _everything(app) == before


def test_the_proposal_route_names_the_rule_a_blank_entry_broke(app) -> None:
    status, answer = _post("/v0/vnext/memory-proposals", _proposal(app, [" "]))
    assert status == 400, answer
    assert answer == {
        "detail": "vNext memory proposal source_refs entries must be strings: source:<id>, memory:<id>, a bare id, or a label, "
        "and not blank or the text of a JSON object or array"}
    assert "nest" not in answer["detail"] and "finite" not in answer["detail"]


@pytest.mark.parametrize("path,body", ROUTES)
def test_the_typed_routes_take_plain_labels(app, path: str, body) -> None:
    status, answer = _post(path, body(app, LABELS))
    assert status == 201, answer


@pytest.mark.parametrize("refs", ([""], ["source:abc", "  "], [json.dumps(A_QUOTE)], ["[1]"]), ids=repr)
def test_the_agent_output_tool_refuses_a_bad_entry_before_it_writes(app, monkeypatch, refs: list[str]) -> None:
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    context = MCPRuntimeContext(database_url=app.url, user_id=app.user_id)
    arguments: dict[str, object] = {
        "agent_id": "hermes", "agent_type": "personal_assistant", "permission_profile": "trusted_local_agent",
        "project_scope": ["Alice"], "title": "Output", "content": "An output about the cobalt door.", "domain": "project",
        "sensitivity": "private",
    }
    before = _everything(app)
    with pytest.raises(MCPArgumentError, match="source_refs entries must be strings"):
        call_mcp_tool(context, name="alice_vnext_ingest_agent_output", arguments={**arguments, "source_refs": refs})
    assert _everything(app) == before
    stored = call_mcp_tool(context, name="alice_vnext_ingest_agent_output", arguments={**arguments, "source_refs": LABELS})
    assert stored["status"] == "imported"
    assert _stored_refs(app) == [(LABELS, LABELS)]


def _cli(app, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = build_cli_env(database_url=app.url, user_id=app.user_id)
    return subprocess.run([sys.executable, "-m", "alicebot_api", *arguments], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False)


CLI_REFUSED = (
    pytest.param(json.dumps(A_QUOTE), id="json object text"),
    pytest.param("[1]", id="json array text"),
    pytest.param("  ", id="blank"),
)


@pytest.mark.parametrize("bad", CLI_REFUSED)
def test_the_command_line_commit_refuses_what_the_other_doors_refuse(app, bad: str) -> None:
    """``alicebot vnext memories commit --source-ref`` takes a string, and JSON text is one."""

    before = _everything(app)
    command = ["vnext", "memories", "commit", "--title", "Kiln", "--text", "The kiln is fired on Mondays."]
    refused = _cli(app, *command, "--source-ref", "https://example.test/kiln", "--source-ref", bad)
    assert refused.returncode == 1, refused.stdout
    assert json.loads(refused.stderr)["error"]["code"] == "invalid_request"
    assert _everything(app) == before
    stored = _cli(app, *command, "--source-ref", "https://example.test/kiln", "--source-ref", "[draft] notes")
    assert stored.returncode == 0, stored.stderr
    assert _everything(app) != before


@pytest.mark.parametrize("bad", CLI_REFUSED)
def test_the_command_line_agent_output_ingest_refuses_what_the_other_doors_refuse(app, bad: str) -> None:
    before = _everything(app)
    command = ["vnext", "agents", "ingest-output", "--agent-id", "hermes", "--permission-profile", "trusted_local_agent",
               "--project-scope", "Alice", "--title", "Output"]
    refused = _cli(app, *command, "--source-ref", "https://example.test/kiln", "--source-ref", bad, "An output about the cobalt door.")
    assert refused.returncode == 1, refused.stdout
    assert json.loads(refused.stderr)["error"]["code"] == "invalid_request"
    assert _everything(app) == before, "no source row, no artifact, no event"
    stored = _cli(app, *command, "--source-ref", "https://example.test/kiln", "--source-ref", "[draft] notes", "An output about the cobalt door.")
    assert stored.returncode == 0, stored.stderr
    assert _stored_refs(app) == [(["https://example.test/kiln", "[draft] notes"], ["https://example.test/kiln", "[draft] notes"])]
