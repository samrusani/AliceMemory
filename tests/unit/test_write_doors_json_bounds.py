"""Every write door that stores caller JSON refuses what cannot be stored and read back, with the door's own refusal.

The memory commit door measured the serialized size of a ref and not its depth, so a ref nested about 950 levels deep was
stored, and ``NaN``, ``Infinity`` and ``1e999`` reached the database: SQLite answered ``IntegrityError`` on its
``json_valid`` check and PostgreSQL ended as a server error. The limit is 64 levels of arrays and objects and no number
that is not finite (``alicebot_api.json_bounds``). Each door answers as it answers other bad input:

* the memory commit service (HTTP 400, an MCP argument error, the CLI's validation error) for ``source_refs``;
* the proposal builder (HTTP 400 with the limit named, ``MemoryProposalInvalid`` for the MCP tool and the CLI);
* the connector normalizer (HTTP 400 on every connector route, the agent output ingest included) and the connector payload
  file reader;
* every MCP tool, for its arguments, after the schema has named a typed field;
* the MCP stdio reader, which answers an invalid request to a message nested past what the decoder takes and goes on;
* the CLI JSON options, with a ``ValueError`` that names the option;
* the HTTP body, where a number that is not finite is HTTP 422 on every route, and a request-model field that holds free
  JSON either uses the bounded types or names the service that holds it (``test_http_json_fields_are_bounded.py``).

Mutations, each one alone, each named by the test that fails (replayed by ``scripts/verify_derived_label_mutations.py``):

* ``_object_tuple`` stops asking ``refuse_unstorable_json``: ``test_the_commit_door_refuses_what_cannot_be_stored``.
* ``MemoryProposal.__post_init__`` is removed: ``test_the_proposal_builder_refuses_what_cannot_be_stored``.
* ``normalize_connector_item`` stops asking: ``test_the_connector_normalizer_refuses_what_cannot_be_stored``.
* ``load_connector_items_from_file`` goes back to ``json.loads``: ``test_a_connector_payload_file_is_held_to_the_limits``.
* ``call_mcp_tool`` stops asking: ``test_every_mcp_tool_holds_its_arguments_to_the_limits``.
* ``_parse_json_rpc_payload`` goes back to decoding every message: ``test_a_message_nested_past_the_decoder_does_not_stop_the_mcp_server``.
* ``_parse_optional_json_object`` goes back to ``json.loads``: ``test_the_cli_json_options_are_held_to_the_limits``.
* ``body_non_finite_location`` is skipped in ``reject_lone_surrogate_json_body``: ``test_a_body_with_a_number_that_is_not_finite_is_a_422_on_every_route``.
"""

from __future__ import annotations

import io
import json
from uuid import UUID

import pytest

from alicebot_api import json_bounds
from alicebot_api.json_bounds import MAX_STORED_JSON_DEPTH
from alicebot_api.mcp import registry as mcp_registry
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPArgumentError, MCPRuntimeContext
from alicebot_api.mcp_server import MCPServer
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_connectors import (
    VNextConnectorValidationError,
    load_connector_items_from_file,
    normalize_connector_item,
)
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    VNextMemoryCommitValidationError,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_memory_propose import MemoryProposal, MemoryProposalInvalid
from tests.unit.api_edge_harness import USER, Edge
from tests.unit.test_importer_per_file_savepoint import USER_ID, _vault

LIMIT = MAX_STORED_JSON_DEPTH


def _nested(depth: int, leaf: object = "x") -> object:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def _objects(depth: int) -> object:
    value: object = 1
    for _ in range(depth):
        value = {"k": value}
    return value


TOO_DEEP = (
    pytest.param(_nested(LIMIT + 1), id="one level over"),
    pytest.param(_nested(950), id="950 levels, what the commit door took"),
    pytest.param(_nested(100_000), id="100000 levels"),
    pytest.param(_objects(LIMIT + 1), id="objects, one level over"),
    pytest.param({"a": [{"b": _nested(LIMIT)}]}, id="deep branch inside shallow ones"),
)
NOT_FINITE = (
    pytest.param({"score": float("nan")}, id="NaN"),
    pytest.param({"score": float("inf")}, id="Infinity"),
    pytest.param([float("-inf")], id="-Infinity"),
    pytest.param({"a": {"b": [1, 2, json.loads("1e999")]}}, id="1e999"),
)
FINE = (
    pytest.param(_nested(LIMIT), id="exactly the limit"),
    pytest.param(_objects(LIMIT), id="objects at the limit"),
    pytest.param({"score": 0.5, "refs": ["source:x", {"id": 1}], "big": 10**30, "text": "NaN Infinity"}, id="ordinary"),
)


# The memory commit door.


def _commit_payload(ref: object) -> dict[str, object]:
    return {"title": "t", "canonical_text": "The cobalt door opens on Monday.", "source_refs": [ref]}


@pytest.mark.parametrize("ref", TOO_DEEP)
def test_the_commit_door_refuses_a_ref_that_nests_too_deep(ref: object) -> None:
    with pytest.raises(VNextMemoryCommitValidationError, match=r"each source_ref must nest at most 64 levels"):
        memory_commit_request_from_payload(_commit_payload(ref), user_id=USER_ID)


@pytest.mark.parametrize("ref", NOT_FINITE)
def test_the_commit_door_refuses_a_ref_with_a_number_that_is_not_finite(ref: object) -> None:
    with pytest.raises(VNextMemoryCommitValidationError, match=r"each source_ref must not hold NaN, Infinity"):
        memory_commit_request_from_payload(_commit_payload(ref), user_id=USER_ID)


def test_the_commit_door_refuses_what_cannot_be_stored(tmp_path) -> None:
    """The refusal comes before anything is written: no memory row, no revision, no event for the refused commit."""

    db = _vault(tmp_path)
    counts = "SELECT (SELECT count(*) FROM memories), (SELECT count(*) FROM memory_revisions), (SELECT count(*) FROM event_log)"
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        before = tuple(conn.execute(counts).fetchone())
        for ref in (_nested(950), {"score": float("nan")}, [json.loads("1e999")]):
            with pytest.raises(VNextMemoryCommitValidationError):
                request = memory_commit_request_from_payload(_commit_payload(ref), user_id=USER_ID)
                VNextMemoryCommitService(store).commit(identity=None, request=request)
        assert tuple(conn.execute(counts).fetchone()) == before


@pytest.mark.parametrize("ref", FINE)
def test_the_commit_door_stores_and_reads_back_what_is_inside_the_limits(tmp_path, monkeypatch, ref: object) -> None:
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    db = _vault(tmp_path)
    request = memory_commit_request_from_payload(_commit_payload(ref), user_id=USER_ID)
    with sqlite_user_connection(db, USER_ID) as conn:
        result = VNextMemoryCommitService(SQLiteVNextStore(conn, USER_ID)).commit(identity=None, request=request)
    memory_id = str(result["memory"]["id"])
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(db), user_id=UUID(USER_ID))
    for name, arguments in (
        ("alice_memory_review", {"review_item_id": memory_id}),
        ("alice_explain", {"memory_id": memory_id}),
        ("alice_recall", {"query": "cobalt door"}),
    ):
        assert call_mcp_tool(context, name=name, arguments=arguments), name


# The proposal builder, shared by the HTTP route, the MCP tool and the CLI.


def _proposal(refs: tuple[object, ...]) -> MemoryProposal:
    return MemoryProposal(
        proposal_type="candidate_memory", title="t", canonical_text="The cobalt door opens on Monday.",
        memory_type="semantic", source_refs=refs,
    )


@pytest.mark.parametrize("ref", [*TOO_DEEP, *NOT_FINITE])
def test_the_proposal_builder_refuses_what_cannot_be_stored(ref: object) -> None:
    with pytest.raises(MemoryProposalInvalid, match=r"^source_refs must (nest at most 64 levels|not hold NaN)"):
        _proposal((ref,))


@pytest.mark.parametrize("ref", FINE)
def test_the_proposal_builder_takes_what_is_inside_the_limits(ref: object) -> None:
    assert _proposal((ref,)).source_refs == (ref,)


# The connector paths: every sync, the agent output ingest, the Telegram and local folder sources.


@pytest.mark.parametrize("connector", ("agent_output", "pdf_document", "browser_clipper", "voice_transcription"))
@pytest.mark.parametrize("bad", [*TOO_DEEP, *NOT_FINITE])
def test_the_connector_normalizer_refuses_what_cannot_be_stored(connector: str, bad: object) -> None:
    payload = {"content": "An agent output.", "text": "text", "agent_id": "agent-1", "title": "Output", "url": "https://example.test/",
               "transcript": "words", "source_refs": [bad]}
    with pytest.raises(VNextConnectorValidationError, match=r"(item|each source_ref) must (nest at most 64 levels|not hold NaN)"):
        normalize_connector_item(connector, payload)


def test_the_connector_normalizer_takes_an_item_inside_the_limits() -> None:
    item = normalize_connector_item(
        "agent_output",
        {"content": "An agent output.", "agent_id": "agent-1", "title": "Output", "source_refs": [_nested(LIMIT)]},
    )
    assert item.raw_text == "An agent output."


def test_a_source_ref_of_an_agent_output_is_measured_as_the_commit_door_measures_it() -> None:
    """The commit door takes a ref nested 64 levels and refuses 65. The agent output door counts the same: the ref is the
    caller's value, and the two levels of the item and of its list are not charged to it. Any other field of the item is
    measured from the item."""

    base = {"content": "An agent output.", "agent_id": "agent-1", "title": "Output"}
    normalize_connector_item("agent_output", {**base, "source_refs": [_nested(LIMIT), "source:abc"]})
    with pytest.raises(VNextConnectorValidationError, match=r"each source_ref must nest at most 64 levels"):
        normalize_connector_item("agent_output", {**base, "source_refs": ["source:abc", _nested(LIMIT + 1)]})
    normalize_connector_item("agent_output", {**base, "extra": _nested(LIMIT - 1)})
    with pytest.raises(VNextConnectorValidationError, match=r"agent_output item must nest at most 64 levels"):
        normalize_connector_item("agent_output", {**base, "extra": _nested(LIMIT), "source_refs": []})


def test_a_connector_payload_file_is_held_to_the_limits(tmp_path) -> None:
    for name, text in (
        ("deep.json", json.dumps({"items": [{"content": "c", "agent_id": "a", "x": _nested(900)}]})),
        ("deeper.json", "[" * 200_000 + "]" * 200_000),
        ("nan.json", '[{"content": "c", "agent_id": "a", "score": NaN}]'),
        ("huge.json", '[{"content": "c", "agent_id": "a", "score": 1e999}]'),
    ):
        path = tmp_path / name
        path.write_text(text)
        with pytest.raises(VNextConnectorValidationError, match=r"payload file must (nest at most 64|not hold NaN)"):
            load_connector_items_from_file(path)
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"items": [{"content": "c", "agent_id": "a", "x": _nested(LIMIT - 3)}]}))
    assert len(load_connector_items_from_file(ok)) == 1


# The MCP boundary.


def _context(tmp_path) -> MCPRuntimeContext:
    return MCPRuntimeContext(database_url=sqlite_url_for_path(_vault(tmp_path)), user_id=UUID(USER_ID))


@pytest.fixture
def probe_tool(monkeypatch):
    """A tool whose schema leaves its argument free, as a tool that took an object would. It records what it was given."""

    seen: list[object] = []
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setitem(mcp_registry._TOOL_HANDLERS, "alice_probe_bounds", lambda context, arguments: seen.append(arguments) or {"ok": True})
    return seen


@pytest.mark.parametrize("bad", [*TOO_DEEP, *NOT_FINITE])
def test_every_mcp_tool_holds_its_arguments_to_the_limits(tmp_path, probe_tool, bad: object) -> None:
    with pytest.raises(MCPArgumentError, match=r"tool 'alice_probe_bounds' arguments must (nest at most 64 levels|not hold NaN)"):
        call_mcp_tool(_context(tmp_path), name="alice_probe_bounds", arguments={"free": bad})
    assert probe_tool == []


def test_a_tool_argument_inside_the_limits_reaches_the_handler(tmp_path, probe_tool) -> None:
    assert call_mcp_tool(_context(tmp_path), name="alice_probe_bounds", arguments={"free": _nested(LIMIT - 1)}) == {"ok": True}
    assert len(probe_tool) == 1


def test_a_typed_field_still_gets_the_schemas_message_for_a_number_that_is_not_finite(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    with pytest.raises(MCPArgumentError, match=r"confidence"):
        call_mcp_tool(_context(tmp_path), name="alice_memory_correct", arguments={
            "review_item_id": "00000000-0000-4000-8000-000000000001", "action": "edit-and-approve", "confidence": float("nan")})


def _serve(tmp_path, *lines: bytes) -> list[dict]:
    """The answers, in order. An answer to a message the server could not read is framed with a Content-Length header,
    the others are one line each, so the output is read as a run of JSON objects with the header lines skipped."""

    out = io.BytesIO()
    MCPServer(context=_context(tmp_path), input_stream=io.BytesIO(b"".join(line + b"\n" for line in lines)), output_stream=out).run()
    text = out.getvalue().decode()
    decoder = json.JSONDecoder()
    answers: list[dict] = []
    position = text.find("{")
    while position != -1:
        value, position = decoder.raw_decode(text, position)
        answers.append(value)
        position = text.find("{", position)
    return answers


@pytest.mark.parametrize("depth", (258, 1_000, 100_000))
def test_a_message_nested_past_the_decoder_does_not_stop_the_mcp_server(tmp_path, depth: int) -> None:
    """The reader raised ``RecursionError`` out of its loop and the server stopped; now the message is an invalid request,
    answered before it is decoded, and the next message is served."""

    deep = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":{"a":' + b"[" * depth + b"]" * depth + b"}}}"
    answers = _serve(tmp_path, deep, b'{"jsonrpc":"2.0","id":2,"method":"ping"}')
    assert answers[0]["error"]["code"] == -32600
    assert "x" not in json.dumps(answers[0]).replace("jsonrpc", "")
    assert answers[1]["id"] == 2 and "result" in answers[1]


def test_a_message_with_a_number_that_is_not_finite_is_refused_by_the_tool_door(tmp_path) -> None:
    line = b'{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"alice_recall","arguments":{"query":"x","limit":NaN}}}'
    answers = _serve(tmp_path, line)
    payload = json.loads(answers[0]["result"]["content"][0]["text"])
    assert payload["error"]["code"] == "invalid_request"


# The CLI JSON options.


@pytest.mark.parametrize(
    "text",
    ('{"a": NaN}', '{"a": [Infinity]}', '{"a": 1e999}', json.dumps(_objects(LIMIT + 1)), "{\"a\": " + "[" * 100_000 + "]" * 100_000 + "}"),
)
def test_the_cli_json_options_are_held_to_the_limits(text: str) -> None:
    from alicebot_api.cli.arguments import _parse_optional_json_object

    with pytest.raises(ValueError, match=r"^--provenance-json must (nest at most 64 levels|not hold NaN)"):
        _parse_optional_json_object(text, option_name="--provenance-json")


def test_the_cli_json_options_still_report_text_that_is_not_json_or_not_an_object() -> None:
    from alicebot_api.cli.arguments import _parse_optional_json_object

    with pytest.raises(ValueError, match="must be valid JSON"):
        _parse_optional_json_object("{nope", option_name="--body-json")
    with pytest.raises(ValueError, match="must be a JSON object"):
        _parse_optional_json_object("[1]", option_name="--body-json")
    assert _parse_optional_json_object(json.dumps(_objects(LIMIT - 1)), option_name="--body-json")  # inside the limit
    assert _parse_optional_json_object(None, option_name="--body-json") is None


# The HTTP body: a number that is not finite is refused on every route that takes a body.


@pytest.fixture()
def edge(monkeypatch: pytest.MonkeyPatch) -> Edge:
    return Edge(monkeypatch)


NOT_FINITE_BODIES = (
    pytest.param(b'{"user_id": "' + USER.encode() + b'", "source_refs": [{"score": NaN}]}', ("source_refs", 0, "score"), id="NaN"),
    pytest.param(b'{"user_id": "' + USER.encode() + b'", "source_refs": [Infinity]}', ("source_refs", 0), id="Infinity"),
    pytest.param(b'{"user_id": "' + USER.encode() + b'", "x": {"y": -Infinity}}', ("x", "y"), id="-Infinity"),
    pytest.param(b'{"user_id": "' + USER.encode() + b'", "source_refs": [1e999]}', ("source_refs", 0), id="1e999"),
    pytest.param(b'{"user_id": "' + USER.encode() + b'", "source_refs": [-1e400]}', ("source_refs", 0), id="-1e400"),
    pytest.param(b'{"user_id": "' + USER.encode() + b'", "source_refs": [1' + b"0" * 320 + b".5]}", ("source_refs", 0), id="300 digits"),
)


@pytest.mark.parametrize(
    "path",
    ("/v0/vnext/memories/commit", "/v0/vnext/memory-proposals", "/v0/vnext/agents/ingest-output", "/v0/continuity/captures/commit",
     "/v0/vnext/context-packs", "/v0/vnext/connectors/pdf_document/sync", "/v1/memory/operations/commit"),
)
@pytest.mark.parametrize(("body", "location"), NOT_FINITE_BODIES)
def test_a_body_with_a_number_that_is_not_finite_is_a_422_on_every_route(edge: Edge, path: str, body: bytes, location: tuple) -> None:
    edge.configure()
    outcome = edge.call("POST", path, body=body, content_type="application/json", query={"user_id": USER})
    assert outcome.status == 422, (path, outcome.status)
    answer = json.loads(outcome.body)
    assert answer == {"detail": [{"type": "finite_number", "loc": ["body", *location], "msg": "Input should be a finite number"}]}
    assert outcome.reached_handler is False


def test_a_body_that_only_says_nan_in_text_is_not_refused(edge: Edge) -> None:
    edge.configure()
    body = b'{"user_id": "' + USER.encode() + b'", "title": "NaN and Infinity and 1e999 are words here", "source_refs": ["1e999"]}'
    outcome = edge.call("POST", "/v0/vnext/memories/commit", body=body, content_type="application/json", query={"user_id": USER})
    assert not (outcome.status == 422 and b"finite_number" in outcome.body)


def test_a_field_that_holds_free_json_is_a_422_past_the_limit(edge: Edge) -> None:
    """A model field typed ``dict[str, object]`` that no service holds to the limits uses ``BoundedJsonObject``."""

    edge.configure()
    base = b'{"user_id": "' + USER.encode() + b'", "query": "cobalt", "options": '
    nested = b'{"k":' * 70 + b"1" + b"}" * 70
    over = edge.call("POST", "/v0/vnext/context-packs", body=base + nested + b"}", content_type="application/json")
    assert over.status == 422
    detail = json.loads(over.body)["detail"][0]
    assert detail["type"] == "json_too_deep" and detail["loc"][:2] == ["body", "options"], detail
    assert detail["msg"] == "Value must nest at most 64 levels of arrays and objects"
    assert set(detail) == {"type", "loc", "msg"} and '"k"' not in over.body.decode(), "the value is not sent back"
    inside = b'{"k":' * (LIMIT - 1) + b"1" + b"}" * (LIMIT - 1)
    ok = edge.call("POST", "/v0/vnext/context-packs", body=base + inside + b"}", content_type="application/json")
    assert not (ok.status == 422 and b"json_too_deep" in ok.body)


# The backup import: a record whose JSON holds a number that is not finite.


def _import_with_a_record(tmp_path, metadata_text: str) -> tuple[int, str, list]:
    """Import an export of an empty vault with one graph edge added whose ``metadata_json`` is the given JSON text.

    SQLite's own checks on that column accept ``NaN`` and ``Infinity``, so before the import read its lines with the
    finite-number rule the edge was restored with the word in its column, and a route that listed it answered with a
    server error (the response encoder refuses the word)."""

    import contextlib
    import copy
    import hashlib
    import sqlite3
    from uuid import uuid4

    from alicebot_api import onramp

    (tmp_path / "origin").mkdir()
    source = _vault(tmp_path / "origin")
    exported = tmp_path / "exported.jsonl"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        assert onramp.main(["export", "--db", str(source), "--user-id", USER_ID, "--out", str(exported)]) == 0
    lines = exported.read_text().splitlines()
    head, footer, records = lines[0], json.loads(lines[-1]), lines[1:-1]
    row = {"id": str(uuid4()), "user_id": USER_ID, "from_type": "document", "from_id": str(uuid4()), "to_type": "project", "to_id": "proj-1",
           "edge_type": "supports", "confidence": 0.5, "explanation": "e", "created_by": "x", "created_at": "2030-01-01T00:00:00Z",
           "observed_at": "2030-01-01T00:00:00Z", "valid_from": "2030-01-01T00:00:00Z", "valid_to": None, "metadata_json": "__META__"}
    records.append(onramp._export_line("graph_edge", row).replace('"__META__"', metadata_text))
    digest = hashlib.sha256()
    for record in records:
        digest.update((record + "\n").encode())
    body = copy.deepcopy(footer["record"])
    body.update(record_count=len(records), sha256=digest.hexdigest())
    body["record_counts"]["graph_edge"] = 1
    spliced = tmp_path / "spliced.jsonl"
    spliced.write_text("\n".join([head, *records, onramp._export_line(onramp._EXPORT_FOOTER_TYPE, body)]) + "\n")
    target = tmp_path / "restored.sqlite3"
    onramp.bootstrap_database(target, user_id=USER_ID, user_email="local@alice")
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = onramp.main(["import", "--in", str(spliced), "--db", str(target), "--user-id", USER_ID])
    with contextlib.closing(sqlite3.connect(target)) as conn:
        stored = conn.execute("SELECT metadata_json FROM graph_edges").fetchall()
    return code, err.getvalue(), stored


@pytest.mark.parametrize("metadata", ('{"score":NaN}', '{"score":[Infinity]}', '{"score":-Infinity}', '{"score":1e999}', '{"score":[0,1e400]}'))
def test_backup_import_refuses_a_record_with_a_number_that_is_not_finite(tmp_path, metadata: str) -> None:
    code, err, stored = _import_with_a_record(tmp_path, metadata)
    assert code == 1, err
    assert '"code":"import_validation_failed"' in err
    assert "line " in err and "a number is not finite" in err
    assert stored == [], "nothing is written for a refused file"


def test_backup_import_takes_a_record_with_ordinary_numbers(tmp_path) -> None:
    code, err, stored = _import_with_a_record(tmp_path, '{"big":10000000000,"note":"NaN Infinity 1e999","score":0.5,"small":1e-300}')
    assert code == 0, err
    assert len(stored) == 1 and json.loads(stored[0][0])["note"] == "NaN Infinity 1e999"
