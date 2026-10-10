"""The product writes strings into ``source_refs``, never an object, so the list of field names a withheld entry keeps has no producer in it.

Unreleased (on main, not in v0.20.0). The reader of saved quotes keeps a field of an entry that names a memory the caller may not
read only when its name is one the product writes (``PRODUCT_REF_KEYS`` in ``vnext_source_fence.py``), because a writer can type
words as the name of a field. The list is the reader's own vocabularies. These tests are the check that no producer needs a name
of its own in it: they run the doors that write ``source_refs`` on a SQLite vault (the commit service, the MCP commit tool, a
commit held for confirmation and confirmed, a memory proposal, a capture, the review edit and the supersede), read every JSON
column of the vault, and require that each entry the producers stored is a string and that no field name outside the list is
stored. The producers that need PostgreSQL (the scheduler's reports, consolidation, the roll-ups, the project update scan) are run
by ``tests/integration/test_saved_quote_producer_ref_keys_postgres.py``.

The doors a client uses to send an object are the HTTP commit route, the memory proposal and the agent-output ingest (their request
models take any object); the MCP tools take an array of strings and refuse an object, and the CLI flag ``--source-ref`` takes a
string, which may be JSON text. A client's object is not a producer's, and the test reads a stored client object to show that the
scan would have seen a producer's.

Mutation: have the commit service write an object entry (``json_safe(list(request.source_refs))`` in ``_base_metadata`` replaced
with a list of ``{"source_id": ..., "label": ...}`` built from the strings): the producers' test fails on the object and on the name
``label``. It is in the manifest.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest

from alicebot_api.cli.parser import build_parser
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService
from alicebot_api.vnext_source_fence import PRODUCT_REF_KEYS, _json_container


class _Vault:
    """A SQLite vault with an admin key, and the doors that write ``source_refs`` called through the MCP registry."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.user = uuid4()
        self.path = tmp_path / "producers.sqlite3"
        bootstrap_database(self.path, user_id=str(self.user), user_email="synthetic@example.invalid")
        for name, value in (("ALICE_MCP_FULL_TOOLS", "1"), ("ALICE_MCP_LEGACY_TOOLS", "1"), ("ALICE_PROJECT_SCOPING", "off")):
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        self.monkeypatch = monkeypatch
        with sqlite_user_connection(self.path, self.user) as conn:
            store = SQLiteVNextStore(conn, self.user)
            self.admin = create_agent_key(
                store, user_id=self.user, agent_id="admin", permission_profile="admin_agent", project_scope=None
            )[1]

    def call(self, key: str | None, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if key is not None:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=UUID(str(self.user)))
        return call_mcp_tool(context, name=tool, arguments=arguments)  # type: ignore[return-value]

    def run_the_producers(self) -> None:
        captured = self.call(self.admin, "alice_capture", {
            "raw_text": "Remember: the alpha kiln is fired on Mondays.", "title": "Kiln note", "domain": "project",
            "sensitivity": "internal",
        })
        source_id = str(captured["source_id"])
        committed = self.call(self.admin, "alice_memory_commit", {
            "title": "Kiln schedule", "canonical_text": "The alpha kiln is fired on Mondays.", "domain": "project",
            "sensitivity": "internal", "confidence": 0.95, "source_refs": [f"source:{source_id}", "https://example.test/kiln"],
        })
        memory_id = str(committed["memory"]["id"])  # type: ignore[index]
        with sqlite_user_connection(self.path, self.user) as conn:
            service = VNextMemoryCommitService(SQLiteVNextStore(conn, self.user))
            # A commit that asks for a confirmation (middling confidence) and is confirmed: its event holds the refs it was sent with.
            asked = service.commit(identity=None, request=MemoryCommitRequest(
                user_id=str(self.user), title="Kiln fuel", canonical_text="The alpha kiln burns gas.", memory_type="semantic",
                domain="project", sensitivity="internal", confidence=0.6, source_refs=(f"source:{source_id}", f"memory:{memory_id}"),
            ))
            assert asked["status"] == "confirmation_required", asked
            service.confirm(identity=None, confirmation_id=asked["memory"]["confirmation_id"], action="confirm")
        proposed = self.call(None, "alice_vnext_propose_memory", {
            "agent_id": "kiln-agent", "canonical_text": "The alpha kiln needs a new thermocouple.",
            "source_refs": [f"source:{source_id}", f"memory:{memory_id}"], "permission_profile": "trusted_local_agent",
        })
        proposal_id = str(proposed["proposal"]["id"])  # type: ignore[index]
        self.call(self.admin, "alice_memory_correct", {
            "review_item_id": proposal_id, "action": "edit-and-approve", "reason": "checked", "title": "Thermocouple",
            "body": {"body": "The alpha kiln needs a new thermocouple."},
            "provenance": {"source_id": source_id, "quote": "fired on Mondays"},
        })
        self.call(self.admin, "alice_memory_correct", {
            "review_item_id": memory_id, "action": "supersede-existing", "reason": "newer", "replacement_title": "Kiln days",
            "replacement_body": {"body": "The alpha kiln is fired on Mondays and Thursdays."},
            "replacement_provenance": {"source_id": source_id, "quote": "fired on Mondays"},
        })


def _lists_under_source_refs(value: object) -> Iterator[list[object]]:
    """Every ``source_refs`` list inside a decoded JSON value, at any depth (a JSON text is decoded, as the reader decodes it)."""

    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            nested = _json_container(node)
            if nested is not None:
                pending.append(nested)
        elif isinstance(node, dict):
            for key, child in node.items():
                if key == "source_refs":
                    yield child if isinstance(child, list) else [child]
                pending.append(child)
        elif isinstance(node, list):
            pending.extend(node)


def _stored_entries(vault: _Vault) -> list[tuple[str, str, object]]:
    """``(table, column, entry)`` for every entry of every ``source_refs`` list stored in any JSON column of the vault."""

    found: list[tuple[str, str, object]] = []
    with sqlite_user_connection(vault.path, vault.user) as conn:
        tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):  # table names come from sqlite_master
                for column, cell in dict(row).items():
                    if not (isinstance(cell, str) and cell[:1] in "{["):
                        continue
                    try:
                        decoded = json.loads(cell)
                    except ValueError:
                        continue
                    for refs in _lists_under_source_refs(decoded):
                        found.extend((table, column, entry) for entry in refs)
    return found


def _names_in(entry: object) -> set[str]:
    """Every field name inside one entry, at any depth, JSON text included."""

    names: set[str] = set()
    pending = [entry]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            nested = _json_container(node)
            if nested is not None:
                pending.append(nested)
        elif isinstance(node, dict):
            names.update(str(key) for key in node)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return names


@pytest.fixture
def vault(tmp_path, monkeypatch) -> _Vault:
    return _Vault(tmp_path, monkeypatch)


def test_the_producers_store_strings_and_no_name_outside_the_list(vault: _Vault) -> None:
    """Capture, the MCP commit tool, the commit service (committed and confirmed), a memory proposal, the review edit and the
    supersede store their ``source_refs`` as strings, in the memory row, its value, its revisions and its events. No object is
    stored by a producer, so no field name is, and the list of names a withheld entry keeps needs no producer's name in it.

    Mutation: see the module docstring.
    """

    vault.run_the_producers()
    entries = _stored_entries(vault)
    where = {(table, column) for table, column, _entry in entries}
    assert {("memories", "metadata_json"), ("memories", "value"), ("memory_revisions", "new_value"), ("event_log", "payload_json")} <= where, where
    assert len(entries) >= 12, "the producers stored refs; a scan that finds none proves nothing"
    objects = [(table, column, entry) for table, column, entry in entries if not isinstance(entry, str)]
    assert objects == []
    assert set().union(*(_names_in(entry) for _t, _c, entry in entries)) - PRODUCT_REF_KEYS == set()


def test_the_scan_sees_the_names_of_an_object_a_client_stored(vault: _Vault) -> None:
    """A client sends an object through the HTTP commit route (the request model takes any object) and the commit service stores
    it as sent. The scan of the test above reads it: the field names a client chose are outside the list, so a producer that
    started to write an object with a name of its own would fail the test above in the same way.

    Mutation: make ``_stored_entries`` skip the ``event_log`` and ``memories`` tables (this test and the one above fail).
    """

    vault.run_the_producers()
    with sqlite_user_connection(vault.path, vault.user) as conn:
        VNextMemoryCommitService(SQLiteVNextStore(conn, vault.user)).commit(
            identity=None,
            request=MemoryCommitRequest(
                user_id=str(vault.user), title="Kiln photo", canonical_text="The kiln is photographed.", memory_type="semantic",
                domain="project", sensitivity="internal", confidence=0.95,
                source_refs=({"memory_id": str(uuid4()), "page": 3, "caption": "front view"}, json.dumps({"label": "side view"})),
            ),
        )
    names = set().union(*(_names_in(entry) for _t, _c, entry in _stored_entries(vault)))
    assert {"page", "caption", "label"} <= names - PRODUCT_REF_KEYS
    assert "memory_id" in names and "memory_id" in PRODUCT_REF_KEYS


def test_the_mcp_tools_and_the_cli_flag_take_strings_and_no_object() -> None:
    """The MCP tools that write ``source_refs`` take an array of strings and refuse an object, and the CLI flag ``--source-ref`` of
    the commit and the agent-output ingest gives strings (which may be JSON text, and the reader decodes JSON text), so the two
    doors never store an object either.

    Mutation: let ``_parse_string_list`` accept an object (the MCP assertion fails).
    """

    from alicebot_api.mcp.arguments import MCPArgumentError, _parse_string_list

    with pytest.raises(MCPArgumentError):
        _parse_string_list({"source_refs": [{"memory_id": str(uuid4()), "page": 3}]}, "source_refs")
    assert _parse_string_list({"source_refs": ["source:a", '{"page": 3}']}, "source_refs") == ("source:a", '{"page": 3}')
    parser = build_parser()
    commit = parser.parse_args(
        ["vnext", "memories", "commit", "--agent-id", "a", "--title", "t", "--text", "x", "--source-ref", '{"page": 3}',
         "--source-ref", "source:a"]
    )
    ingest = parser.parse_args(
        ["vnext", "agents", "ingest-output", "--agent-id", "a", "--title", "t", "--source-ref", '{"page": 3}', "inline", "content"]
    )
    assert commit.source_ref == ['{"page": 3}', "source:a"] and ingest.source_ref == ['{"page": 3}']


def test_a_tool_that_takes_an_object_for_a_ref_is_refused_over_mcp(vault: _Vault) -> None:
    """Over the registry, as a client calls it: ``alice_memory_commit`` with an object in ``source_refs`` answers a tool error and
    stores nothing.

    Mutation: none of its own; it fails with the one above.
    """

    with pytest.raises(MCPToolError):
        vault.call(vault.admin, "alice_memory_commit", {
            "title": "Kiln", "canonical_text": "An object ref.", "domain": "project", "sensitivity": "internal",
            "source_refs": [{"memory_id": str(uuid4()), "page": 3}],
        })
    assert _stored_entries(vault) == []
