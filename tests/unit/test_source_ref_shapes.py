"""Write doors accept the string shapes producers store and refuse anything else.

Producers write ``source:<id>``, ``memory:<id>``, a bare id, or a label such as a URL. They do not write an object, a number,
a boolean, null, a nested array, a blank string, or the text of a JSON object or array (an object in disguise). The commit
service, the proposal, the agent-output ingest and the connector sync of ``agent_output`` refuse each of them, and so do the
MCP tools. The HTTP models are ``list[SourceRefText]``, so an object and JSON text are HTTP 422 and a blank entry is the 400
of the service. A backup that already holds an object restores, and the reader still sees it.

Mutations, each one alone: drop the string check in ``_object_tuple`` (``test_the_commit_service_accepts_every_producer_string_and_refuses_the_rest``
fails); drop the check in ``_agent_output_source_refs`` (the sync tests fail); leave ``is_json_text_ref`` out of
``is_producer_source_ref`` (the JSON text cases fail at every door); let ``_parse_source_refs`` skip a blank entry (the MCP
blank tests fail). They are in ``scripts/derived_label_mutations.json``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPArgumentError, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.routers.vnext_memories import (
    VNextAgentOutputIngestRequest,
    VNextMemoryCommitRequest,
    VNextMemoryProposalRequest,
)
from alicebot_api.vnext_connectors import VNextConnectorService, VNextConnectorValidationError
from alicebot_api.vnext_memory_commit import VNextMemoryCommitService, VNextMemoryCommitValidationError, _object_tuple
from alicebot_api.vnext_memory_propose import MemoryProposal, MemoryProposalInvalid
from tests.unit.fixtures_backup_import import origin_export
from tests.unit.test_sqlite_onramp import USER_ID as SEED_USER_ID


SHAPES = ("source:" + str(uuid4()), "memory:" + str(uuid4()), str(uuid4()), "https://example.test/kiln")


def test_the_commit_service_accepts_every_producer_string_and_refuses_the_rest() -> None:
    assert _object_tuple(list(SHAPES)) == SHAPES
    for bad in ({"source_id": str(uuid4()), "quote": "x"}, 3, True, None, ["nested"]):
        with pytest.raises(VNextMemoryCommitValidationError, match="source_refs entries must be strings"):
            _object_tuple([bad])


def test_the_proposal_and_the_ingest_refuse_an_object() -> None:
    with pytest.raises(MemoryProposalInvalid, match="source_refs entries must be strings"):
        MemoryProposal(proposal_type="candidate_memory", title="t", canonical_text="c", memory_type="semantic", source_refs=({"quote": "x"},))
    MemoryProposal(proposal_type="candidate_memory", title="t", canonical_text="c", memory_type="semantic", source_refs=SHAPES)

    class _Store:
        pass

    with pytest.raises(VNextConnectorValidationError, match="source_refs entries must be strings"):
        VNextConnectorService(_Store()).ingest_agent_output({"title": "t", "content": "c", "source_refs": [{"quote": "x"}]})


def test_the_http_models_are_422_for_an_object() -> None:
    from pydantic import ValidationError

    body = {"user_id": str(uuid4()), "title": "t", "canonical_text": "c", "source_refs": [{"quote": "x"}]}
    for model in (VNextMemoryCommitRequest, VNextMemoryProposalRequest):
        with pytest.raises(ValidationError):
            model.model_validate(body)
    ingest = {
        "user_id": str(uuid4()),
        "agent_id": "a",
        "title": "t",
        "content": "c",
        "source_refs": [{"quote": "x"}],
    }
    with pytest.raises(ValidationError):
        VNextAgentOutputIngestRequest.model_validate(ingest)
    accepted = {**body, "source_refs": list(SHAPES)}
    assert VNextMemoryCommitRequest.model_validate(accepted).source_refs == list(SHAPES)


@pytest.mark.parametrize(
    "backup",
    ("v0.20.0-object-source-ref.jsonl", "v0.20.2-object-source-ref.jsonl"),
)
def test_a_released_backup_that_holds_an_object_ref_restores(tmp_path, backup: str) -> None:
    """A backup exported by the tagged source of v0.20.0 or v0.20.2, with an object entry the release stored.

    The commit doors on main refuse that shape. Import still accepts it.
    """

    source = Path(__file__).resolve().parents[1] / "fixtures" / "legacy-object-ref-backups" / backup
    release = backup.split("-", 1)[0]
    header = json.loads(source.read_text().splitlines()[0])
    assert header["record"]["application_version"] == release.removeprefix("v")
    user_id = header["record"]["user_id"]
    database = tmp_path / "restored.db"
    bootstrap_database(database, user_id=user_id, user_email="local@alice")
    from alicebot_api import onramp as onramp_module

    assert onramp_module.main(["import", "--in", str(source), "--db", str(database), "--user-id", user_id]) == 0
    row = sqlite3.connect(database).execute("SELECT metadata_json FROM memories").fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert stored["agentic_memory"]["source_refs"][0]["quote"] == f"kept from the {release} vault"


def test_a_backup_that_holds_an_object_ref_restores(tmp_path) -> None:
    export = origin_export(tmp_path)
    memories = [item for item in export.body if item.get("record_type") == "memory"]
    assert memories, sorted({item["record_type"] for item in export.body})
    record = memories[0]["record"]
    metadata = record.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    metadata["source_refs"] = [{"memory_id": str(uuid4()), "quote": "kept from the old vault"}]
    record["metadata_json"] = metadata
    backup = export.write(tmp_path / "backup.jsonl")
    database = tmp_path / "restored.db"
    from alicebot_api import onramp as onramp_module

    bootstrap_database(database, user_id=SEED_USER_ID, user_email="local@alice")
    assert onramp_module.main(["import", "--in", str(backup), "--db", str(database), "--user-id", str(SEED_USER_ID)]) == 0
    row = sqlite3.connect(database).execute(
        "SELECT metadata_json FROM memories WHERE id = ?", (record["id"],)
    ).fetchone()
    assert row is not None
    stored = json.loads(row[0])
    assert stored["source_refs"][0]["quote"] == "kept from the old vault"


# -- The same refusal at every door ---------------------------------------------------------------------------------------
#
# A client can send an entry in shapes a producer never writes: an object, a number, a boolean, null, a blank string, a nested
# array, and the text of a JSON object or array (an object in disguise). Every door that stores ``source_refs`` gives one
# answer to all of them, and writes nothing.

A_QUOTE_OF_A_MEMORY = {"memory_id": str(uuid4()), "quote": "words of a memory"}
JSON_TEXTS = (
    json.dumps(A_QUOTE_OF_A_MEMORY),
    json.dumps([A_QUOTE_OF_A_MEMORY]),
    '  {"quote": "padded"}\n',
    "[]",
    "{}",
    '["source:' + str(uuid4()) + '"]',
)
REFUSED_ENTRIES = (
    pytest.param(A_QUOTE_OF_A_MEMORY, id="object"),
    pytest.param(3, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param(None, id="null"),
    pytest.param("", id="blank"),
    pytest.param("  \n\t ", id="whitespace"),
    pytest.param(["nested"], id="nested array"),
    *(pytest.param(text, id=f"json text {index}") for index, text in enumerate(JSON_TEXTS)),
)
# Strings that are not the text of a JSON object or array, whatever they start with.
LABELS = (
    *SHAPES,
    "[draft] kiln notes",
    "{curly} label",
    "https://example.test/kiln?filter={a}&list=[1]",
    '"a quoted word"',
    "123",
    "null",
    "true",
    '["unclosed',
    '{"unclosed": ',
    "[1, 2] and more words",
)


def test_a_string_is_json_text_when_the_reader_would_decode_it() -> None:
    """The write check asks the reader's own decoder, so a string refused at a door is a string the reader reads as JSON.

    Mutation: make ``is_json_text_ref`` test ``startswith("{")`` only (the array texts of ``JSON_TEXTS`` pass).
    """

    from alicebot_api.vnext_source_fence import _json_container, is_json_text_ref

    for text in JSON_TEXTS:
        assert is_json_text_ref(text) is True and _json_container(text) is not None
    for label in LABELS:
        assert is_json_text_ref(label) is False and _json_container(label) is None
    # A text nested past what the decoder reads is a label to the reader, so it is here, and it costs no recursion error.
    deep = "[" * 1_000_000 + "]" * 1_000_000
    assert _json_container(deep) is None and is_json_text_ref(deep) is False


@pytest.mark.parametrize("bad", REFUSED_ENTRIES)
def test_the_commit_and_proposal_services_refuse_every_shape(bad: object) -> None:
    with pytest.raises(VNextMemoryCommitValidationError, match="source_refs entries must be strings"):
        _object_tuple(["source:" + str(uuid4()), bad])
    with pytest.raises(MemoryProposalInvalid, match="source_refs entries must be strings"):
        MemoryProposal(proposal_type="candidate_memory", title="t", canonical_text="c", memory_type="semantic", source_refs=("a", bad))


def test_the_commit_and_proposal_services_take_plain_labels() -> None:
    assert _object_tuple(list(LABELS)) == LABELS
    MemoryProposal(proposal_type="candidate_memory", title="t", canonical_text="c", memory_type="semantic", source_refs=LABELS)


@pytest.mark.parametrize("bad", [pytest.param(text, id=f"json text {index}") for index, text in enumerate(JSON_TEXTS)])
def test_the_http_models_answer_json_text_as_they_answer_an_object(bad: str) -> None:
    """A model field is ``list[str]``, so an object is a validation error at ``source_refs.<index>`` (HTTP 422). JSON text
    is refused at the same place, so a client gets the same status for both.

    Mutation: type the three fields as plain ``list[str]`` again.
    """

    from pydantic import ValidationError

    typed = {"user_id": str(uuid4()), "title": "t", "canonical_text": "c"}
    bodies = (
        (VNextMemoryCommitRequest, typed),
        (VNextMemoryProposalRequest, typed),
        (VNextAgentOutputIngestRequest, {"user_id": typed["user_id"], "agent_id": "a", "title": "t", "content": "c"}),
    )
    for model, body in bodies:
        locations = []
        for ref in ({"quote": "x"}, bad):
            with pytest.raises(ValidationError) as caught:
                model.model_validate({**body, "source_refs": ["source:a", ref]})
            errors = caught.value.errors()
            assert [error["loc"] for error in errors] == [("source_refs", 1)], (model, errors)
            locations.append(errors[0]["loc"])
        assert locations[0] == locations[1]
        assert model.model_validate({**body, "source_refs": list(LABELS)}).source_refs == list(LABELS)


class _Vault:
    """A SQLite vault, with the counts of the tables a refused write must leave alone."""

    TABLES = ("sources", "source_chunks", "memories", "memory_revisions", "provenance_links")

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.user = uuid4()
        self.path = tmp_path / "shapes.sqlite3"
        bootstrap_database(self.path, user_id=str(self.user), user_email="synthetic@example.invalid")
        for name, value in (("ALICE_MCP_FULL_TOOLS", "1"), ("ALICE_MCP_LEGACY_TOOLS", "1"), ("ALICE_PROJECT_SCOPING", "off")):
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)

    def counts(self) -> tuple[int, ...]:
        with sqlite3.connect(self.path) as conn:
            return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in self.TABLES)

    def stored_refs(self) -> list[object]:
        """Every ``source_refs`` list of a source, in its metadata and in the payload kept beside it."""

        found: list[object] = []
        with sqlite3.connect(self.path) as conn:
            for (raw,) in conn.execute("SELECT metadata_json FROM sources"):
                metadata = json.loads(raw)
                found.append(metadata.get("source_refs"))
                found.append((metadata.get("raw_payload") or {}).get("source_refs"))
        return found

    def sync(self, items: list[dict[str, object]]):
        with sqlite_user_connection(self.path, self.user) as conn:
            return VNextConnectorService(SQLiteVNextStore(conn, self.user)).sync_items("agent_output", items)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Vault:
    return _Vault(tmp_path, monkeypatch)


def _output(refs: object, name: str = "one") -> dict[str, object]:
    return {"agent_id": "agent-1", "title": f"Output {name}", "content": f"An output about the kiln, {name}.",
            "external_id": f"out-{name}-{uuid4()}", "source_refs": refs}


@pytest.mark.parametrize("bad", REFUSED_ENTRIES)
def test_the_sync_of_agent_output_refuses_every_shape_the_other_doors_refuse(vault: _Vault, bad: object) -> None:
    """``POST /v0/vnext/connectors/agent_output/sync`` and ``alicebot vnext connectors ingest agent_output FILE`` reach
    ``sync_items`` and not ``ingest_agent_output``. An item whose ``source_refs`` hold a refused entry is a failed item: no
    source row is written, whatever the other entries are, and an item beside it is imported.

    Mutation: take the check out of ``_normalize_agent_output_item``.
    """

    before = vault.counts()
    refused = vault.sync([_output(["source:" + str(uuid4()), bad])])
    assert (refused.status, refused.imported_count, refused.failed_count) == ("failed", 0, 1)
    assert vault.counts() == before
    assert vault.stored_refs() == []

    mixed = vault.sync([_output([bad], "bad"), _output(list(SHAPES), "good")])
    assert (mixed.status, mixed.imported_count, mixed.failed_count) == ("partial", 1, 1)
    assert vault.counts()[0] == before[0] + 1
    assert vault.stored_refs() == [list(SHAPES), list(SHAPES)]


@pytest.mark.parametrize("refs", ({"quote": "x"}, "source:abc", 5, True, 0, "", False, {}))
def test_the_sync_of_agent_output_refuses_source_refs_that_is_not_a_list(vault: _Vault, refs: object) -> None:
    before = vault.counts()
    result = vault.sync([_output(refs)])
    assert (result.status, result.imported_count, result.failed_count) == ("failed", 0, 1)
    assert vault.counts() == before


def test_the_sync_of_agent_output_takes_strings_and_an_item_with_no_refs(vault: _Vault) -> None:
    result = vault.sync([_output(list(LABELS), "labels"), _output([], "empty"), {k: v for k, v in _output([], "none").items() if k != "source_refs"}])
    assert (result.status, result.imported_count, result.failed_count) == ("ok", 3, 0)
    assert sorted(map(repr, vault.stored_refs())) == sorted(map(repr, [list(LABELS), list(LABELS), [], [], [], None]))


@pytest.mark.parametrize("bad", REFUSED_ENTRIES)
def test_the_ingest_of_one_agent_output_refuses_the_same_shapes(vault: _Vault, bad: object) -> None:
    before = vault.counts()
    with sqlite_user_connection(vault.path, vault.user) as conn:
        with pytest.raises(VNextConnectorValidationError, match="source_refs entries must be strings"):
            VNextConnectorService(SQLiteVNextStore(conn, vault.user)).ingest_agent_output(_output([bad]))
    assert vault.counts() == before


class _McpVault(_Vault):
    def call(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=self.user)
        return call_mcp_tool(context, name=tool, arguments=arguments)  # type: ignore[return-value]


MCP_TOOLS = (
    pytest.param(
        "alice_memory_commit",
        {"title": "Kiln", "canonical_text": "The kiln is fired on Mondays.", "domain": "project", "sensitivity": "internal"},
        id="alice_memory_commit",
    ),
    pytest.param(
        "alice_vnext_commit_memory",
        {"title": "Kiln", "canonical_text": "The kiln is fired on Mondays.", "domain": "project", "sensitivity": "internal"},
        id="alice_vnext_commit_memory",
    ),
    pytest.param(
        "alice_vnext_propose_memory",
        {"agent_id": "agent-1", "permission_profile": "memory_proposal_agent", "title": "Kiln",
         "canonical_text": "The kiln is fired on Mondays.", "domain": "project", "sensitivity": "internal"},
        id="alice_vnext_propose_memory",
    ),
    pytest.param(
        "alice_vnext_ingest_agent_output",
        {"agent_id": "agent-1", "permission_profile": "trusted_local_agent", "title": "Kiln", "content": "The kiln is fired on Mondays.",
         "domain": "project", "sensitivity": "internal"},
        id="alice_vnext_ingest_agent_output",
    ),
)


# The agent-output tool needs a store with artifacts, which the SQLite vault has not (the PostgreSQL twin in
# tests/integration/test_source_ref_shapes_postgres.py stores through it). It refuses a bad entry before it touches a store.
MCP_STORING_TOOLS = MCP_TOOLS[:3]


@pytest.fixture
def mcp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _McpVault:
    return _McpVault(tmp_path, monkeypatch)


@pytest.mark.parametrize("refs", ([""], ["   "], ["source:a", ""], ["  \n\t"], [" ", "source:b"]), ids=repr)
@pytest.mark.parametrize("tool,arguments", MCP_TOOLS)
def test_a_blank_source_ref_is_refused_over_mcp_like_the_other_doors(mcp_vault: _McpVault, tool: str, arguments: dict[str, object], refs: object) -> None:
    """The tools dropped a blank entry and went on, while the HTTP routes and the CLI refuse it. Nothing is written now.

    Mutation: let ``_parse_source_refs`` skip a blank entry again.
    """

    before = mcp_vault.counts()
    with pytest.raises(MCPArgumentError, match="source_refs entries must be strings"):
        mcp_vault.call(tool, {**arguments, "source_refs": refs})
    assert mcp_vault.counts() == before


@pytest.mark.parametrize("text", JSON_TEXTS)
@pytest.mark.parametrize("tool,arguments", MCP_TOOLS)
def test_json_text_in_source_refs_is_refused_over_mcp(mcp_vault: _McpVault, tool: str, arguments: dict[str, object], text: str) -> None:
    """The MCP schema says a string, and JSON text is one. It is refused with the answer an object gets
    (``invalid_request``).

    Mutation: leave the ``is_json_text_ref`` test out of ``_parse_source_refs``.
    """

    before = mcp_vault.counts()
    with pytest.raises(MCPArgumentError):
        mcp_vault.call(tool, {**arguments, "source_refs": ["source:a", text]})
    with pytest.raises(MCPArgumentError):
        mcp_vault.call(tool, {**arguments, "source_refs": [{"quote": "an object"}]})
    assert mcp_vault.counts() == before


@pytest.mark.parametrize("tool,arguments", MCP_STORING_TOOLS)
def test_plain_source_refs_are_stored_over_mcp(mcp_vault: _McpVault, tool: str, arguments: dict[str, object]) -> None:
    before = mcp_vault.counts()
    mcp_vault.call(tool, {**arguments, "source_refs": ["https://example.test/kiln", "[draft] kiln notes", "{curly} label"]})
    assert mcp_vault.counts() != before
    mcp_vault.call(tool, {**arguments, "title": "Kiln two", "canonical_text": "The kiln is cleaned on Fridays.", "source_refs": ["https://example.test/a"]})


def test_the_legacy_plant_puts_the_entries_into_the_revisions_and_keeps_the_append_only_guard(vault: _Vault) -> None:
    """A v0.20.2 commit wrote the entries into its creation revision as well as into the memory row. The helper that stands in
    for that vault plants both, so a test of the reader's revision path (``SavedProvenanceReader._revision``) has an entry to
    withhold, and the guard that makes revisions append-only is back in place after it.

    Mutations: drop the call of ``_plant_sqlite_revisions`` (the revision assertion fails); leave out the statement that creates
    the trigger again (the guard assertion fails).
    """

    from alicebot_api.vnext_memory_commit import MemoryCommitRequest
    from tests.support.legacy_source_refs import REVISION_TRIGGER, link_strings, needs_legacy_plant, plant_legacy_source_refs

    refs: list[object] = [A_QUOTE_OF_A_MEMORY, json.dumps(A_QUOTE_OF_A_MEMORY), "https://example.test/kiln", ""]
    assert needs_legacy_plant(refs) and not needs_legacy_plant(["https://example.test/kiln", "[draft] notes"])
    # Each shape a door refuses is planted on its own, JSON text and a blank string among them.
    assert all(needs_legacy_plant([ref]) for ref in (A_QUOTE_OF_A_MEMORY, json.dumps([1]), "  ", 3, None, ["nested"]))
    sent = link_strings(refs)
    assert sent == ["https://example.test/kiln"]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        result = VNextMemoryCommitService(store).commit(
            identity=None,
            request=MemoryCommitRequest(
                user_id=str(vault.user), title="Kiln", canonical_text="The kiln is fired on Mondays.", memory_type="semantic",
                domain="project", sensitivity="internal", confidence=0.95, source_refs=tuple(sent),
            ),
        )
        memory_id = str(result["memory"]["id"])
        plant_legacy_source_refs(store, memory_id, refs, sent)
        revisions = conn.execute("SELECT new_value FROM memory_revisions WHERE memory_id = ?", (memory_id,)).fetchall()
        assert [json.loads(row["new_value"])["source_refs"] for row in revisions] == [refs]
        row = conn.execute("SELECT metadata_json, value FROM memories WHERE id = ?", (memory_id,)).fetchone()
        assert json.loads(row["metadata_json"])["agentic_memory"]["source_refs"] == refs
        assert json.loads(row["value"])["source_refs"] == refs
        guard = conn.execute("SELECT count(*) AS n FROM sqlite_master WHERE type = 'trigger' AND name = ?", (REVISION_TRIGGER,)).fetchone()
        assert guard["n"] == 1
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE memory_revisions SET reason = 'edited' WHERE memory_id = ?", (memory_id,))


@pytest.mark.parametrize("bad", (3, 1.5, True, None, ["nested"], [["deeper"]]), ids=repr)
def test_the_http_models_refuse_a_number_a_boolean_null_and_a_nested_array(bad: object) -> None:
    """Each is HTTP 422 at ``source_refs.<index>``, from the string type, on the three models.

    Mutation: type a field ``list[object]`` again (the entries pass).
    """

    from pydantic import ValidationError

    typed = {"user_id": str(uuid4()), "title": "t", "canonical_text": "c"}
    ingest = {"user_id": typed["user_id"], "agent_id": "a", "title": "t", "content": "c"}
    for model, body in ((VNextMemoryCommitRequest, typed), (VNextMemoryProposalRequest, typed), (VNextAgentOutputIngestRequest, ingest)):
        with pytest.raises(ValidationError) as caught:
            model.model_validate({**body, "source_refs": ["source:a", bad]})
        assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [(("source_refs", 1), "string_type")]


def test_a_backup_that_holds_json_text_in_source_refs_restores_and_the_reader_still_decodes_it(tmp_path) -> None:
    """The doors refuse JSON text now, and a vault that stored it earlier keeps working: the backup restores with the text as
    it was, and the reader still decodes it into the entry it holds (so its quote is withheld as the quote of an object is).

    Mutation: make ``_json_container`` return None for every text (the decode assertion fails, and so do the JSON text cases of
    ``tests/unit/test_saved_quote_json_refs.py``).
    """

    from alicebot_api.vnext_source_fence import _json_container

    export = origin_export(tmp_path)
    memories = [item for item in export.body if item.get("record_type") == "memory"]
    record = memories[0]["record"]
    metadata = record.get("metadata_json") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    cited = str(uuid4())
    text = json.dumps({"memory_id": cited, "quote": "kept from the old vault"})
    metadata["source_refs"] = [text]
    record["metadata_json"] = metadata
    backup = export.write(tmp_path / "json-text-backup.jsonl")
    database = tmp_path / "restored-json-text.db"
    from alicebot_api import onramp as onramp_module

    bootstrap_database(database, user_id=SEED_USER_ID, user_email="local@alice")
    assert onramp_module.main(["import", "--in", str(backup), "--db", str(database), "--user-id", str(SEED_USER_ID)]) == 0
    row = sqlite3.connect(database).execute("SELECT metadata_json FROM memories WHERE id = ?", (record["id"],)).fetchone()
    assert row is not None
    stored = json.loads(row[0])["source_refs"]
    assert stored == [text]
    assert _json_container(stored[0]) == {"memory_id": cited, "quote": "kept from the old vault"}
