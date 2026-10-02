"""``alice_memory_commit`` takes at most 20,000 characters of memory text, on SQLite as on the Postgres HTTP routes.

The Postgres HTTP model of a commit has always had ``canonical_text`` with ``max_length=20_000``. The MCP tool, on
SQLite and on any store, had no limit: in v0.20.0 and before, a 2,000,000-character memory was stored whole and then
came back whole in every recall and context pack that named it. The request builder every door but the HTTP model
uses, ``memory_commit_request_from_payload``, now refuses a longer text with ``MemoryCommitTextTooLarge``, and the MCP
server answers it as ``invalid_request`` with the count and the limit and no text.

Every test names, in its docstring, the change to the code that must fail it. Nothing here reaches the network.
"""

from __future__ import annotations

import inspect
import json
from io import BytesIO
from pathlib import Path

import pytest

from alicebot_api import mcp_server, write_bounds
from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPInvalidRequestError
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.routers.vnext_memories import VNextMemoryCommitRequest
from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_memory_commit import (
    MAX_COMMIT_CANONICAL_TEXT_CHARS,
    MemoryCommitTextTooLarge,
    VNextMemoryCommitValidationError,
    memory_commit_request_from_payload,
)

USER = "00000000-0000-4000-8000-000000000001"
LIMIT = 20_000


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient embeddings endpoint, so a commit sends nothing anywhere."""

    for name in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class _Vault:
    """A SQLite vault on disk, reached through the MCP tool."""

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = resolve_db_path(data_dir=str(tmp_path), db=None)
        bootstrap_database(self.db_path, user_id=USER, user_email="local@alice")
        self.context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.db_path), user_id=USER)

    def commit(self, text: str, **extra: object) -> dict[str, object]:
        return call_mcp_tool(
            self.context,
            name="alice_memory_commit",
            arguments={"title": "A long memory", "canonical_text": text, "domain": "project", **extra},
        )

    def counts(self) -> tuple[int, int]:
        """(memory rows, event log rows)."""

        with sqlite_user_connection(self.db_path, USER) as connection:
            memories = connection.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
            events = connection.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"]
        return int(memories), int(events)

    def longest_text(self) -> int:
        with sqlite_user_connection(self.db_path, USER) as connection:
            row = connection.execute("SELECT COALESCE(MAX(LENGTH(canonical_text)), 0) AS n FROM memories").fetchone()
        return int(row["n"])


def _words(length: int) -> str:
    """Text of exactly ``length`` characters with single spaces only, so collapsing whitespace leaves it alone."""

    return ("alpha " * length)[: length - 1] + "x"


def test_the_limit_is_the_number_the_postgres_http_model_uses() -> None:
    """The constant is 20,000 and equals the ``max_length`` of the HTTP model's ``canonical_text``.

    The HTTP model keeps its own literal, so the two numbers could drift; this pins them together.

    Mutations, each one alone: change ``MAX_COMMIT_CANONICAL_TEXT_CHARS`` in ``write_bounds``, or change the
    ``max_length`` of ``canonical_text`` in ``VNextMemoryCommitRequest``.
    """

    assert write_bounds.MAX_COMMIT_CANONICAL_TEXT_CHARS == MAX_COMMIT_CANONICAL_TEXT_CHARS == LIMIT
    metadata = VNextMemoryCommitRequest.model_fields["canonical_text"].metadata
    assert [item.max_length for item in metadata if hasattr(item, "max_length")] == [LIMIT]


def test_a_text_of_exactly_the_limit_is_stored_and_one_more_is_refused_with_nothing_written(tmp_path: Path) -> None:
    """20,000 characters commit; 20,001 are refused as ``invalid_request`` and write no row and no event.

    The refusal comes before the store is opened, so the memory count and the event count stay as they were,
    and its message holds the count and the limit and not the text.

    Mutations, each one alone, in ``_commit_canonical_text``: compare with ``>=`` (the 20,000-character text is
    refused); compare with ``> LIMIT + 1`` or delete the check (the 20,001-character text is stored); build the
    error from the text (the message check fails).
    """

    vault = _Vault(tmp_path)
    fits = _words(LIMIT)
    assert len(fits) == LIMIT
    result = vault.commit(fits)
    assert result["status"] == "committed"
    assert vault.longest_text() == LIMIT
    before = vault.counts()

    too_long = fits + "z"
    assert len(too_long) == LIMIT + 1
    with pytest.raises(MCPInvalidRequestError) as refused:
        vault.commit(too_long)
    assert refused.value.public_message == (
        "canonical_text is 20001 characters; the limit is 20000. Shorten it, or commit it as separate memories."
    )
    assert "alpha beta" not in refused.value.public_message
    assert vault.counts() == before


def test_a_two_million_character_memory_is_refused_and_stored_nowhere(tmp_path: Path) -> None:
    """The case the known limitation names: 2,000,000 characters were stored whole, and are now refused.

    Mutation: delete the length check in ``_commit_canonical_text`` (the commit stores the 2,000,000 characters and
    the test fails on the missing refusal).
    """

    vault = _Vault(tmp_path)
    before = vault.counts()
    with pytest.raises(MCPInvalidRequestError) as refused:
        vault.commit("word " * 400_000)
    assert "1999999 characters; the limit is 20000" in refused.value.public_message
    assert vault.counts() == before
    assert vault.longest_text() == 0


def test_the_refusal_is_a_typed_commit_validation_error_that_carries_the_count_and_the_limit() -> None:
    """``MemoryCommitTextTooLarge`` is a ``VNextMemoryCommitValidationError`` with ``measured`` and ``limit``.

    The builder raises it for every caller, and a caller that already catches ``VNextMemoryCommitValidationError``
    (the CLI, the HTTP route) keeps catching it. The count is of the text as it is stored.

    Mutations, each one alone: make ``MemoryCommitTextTooLarge`` subclass ``ValueError`` only (the subclass check
    fails); drop ``measured`` or ``limit`` from it.
    """

    with pytest.raises(MemoryCommitTextTooLarge) as refused:
        memory_commit_request_from_payload({"title": "t", "canonical_text": "a" * (LIMIT + 5)}, user_id=USER)
    assert isinstance(refused.value, VNextMemoryCommitValidationError)
    assert (refused.value.measured, refused.value.limit) == (LIMIT + 5, LIMIT)
    assert refused.value.public_message == str(refused.value)
    assert "a" * 20 not in str(refused.value)
    request = memory_commit_request_from_payload({"title": "t", "canonical_text": "a" * LIMIT}, user_id=USER)
    assert len(request.canonical_text) == LIMIT


def test_the_limit_counts_the_text_as_it_is_stored_after_whitespace_is_collapsed() -> None:
    """Runs of whitespace count once: 30,000 characters that collapse to 3 are taken; the stored text is the collapsed one.

    The builder collapses whitespace before it measures, and so does the MCP handler before the builder, so what is
    measured is what a memory holds, and a text the HTTP model takes (at most 20,000 characters as sent) is never
    refused here.

    Mutation: measure the text before ``_normalized_text`` collapses it, in ``_commit_canonical_text`` (this
    30,000-character text is refused).
    """

    request = memory_commit_request_from_payload(
        {"title": "t", "canonical_text": "a" + " " * 29_998 + "b"}, user_id=USER
    )
    assert request.canonical_text == "a b"


def test_the_mcp_server_answers_the_refusal_as_invalid_request_with_the_message(tmp_path: Path) -> None:
    """Through the stdio server, the refusal is the tool error ``invalid_request`` with the count and the limit.

    Every other tool failure answers one static message. This one is safe to tell, because it holds a count and a
    limit and no text, so the server must send it as ``MCPInvalidRequestError`` does.

    Mutation: delete the ``except MemoryCommitTextTooLarge`` clause in the MCP registry (the client gets the static
    ``tool_request_failed`` message, and the message check fails).
    """

    vault = _Vault(tmp_path)
    server = mcp_server.MCPServer(context=vault.context, input_stream=BytesIO(), output_stream=BytesIO())
    response = server._handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "alice_memory_commit",
                "arguments": {"title": "t", "canonical_text": _words(LIMIT + 1), "domain": "project"},
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": {
            "code": "invalid_request",
            "message": "canonical_text is 20001 characters; the limit is 20000. Shorten it, or commit it as separate memories.",
        }
    }


def test_the_tool_schema_tells_the_agent_the_limit() -> None:
    """The ``canonical_text`` description of ``alice_memory_commit`` states the number from ``write_bounds``.

    Mutation: take the number out of the description, or hard-code a different one.
    """

    tool = next(item for item in _CORE_TOOL_DEFINITIONS if item["name"] == "alice_memory_commit")
    description = str(tool["inputSchema"]["properties"]["canonical_text"]["description"])  # type: ignore[index]
    assert f"At most {LIMIT:,} characters" in description


def test_the_builder_is_the_only_door_the_tool_and_the_http_route_use() -> None:
    """The MCP handler and the HTTP route both build their request with ``memory_commit_request_from_payload``.

    The limit lives in that function, so a door that built a ``MemoryCommitRequest`` by hand would skip it. This
    pins the two callers by name.

    Mutation: build the request in ``_handle_alice_vnext_commit_memory`` or ``commit_vnext_memory`` with
    ``MemoryCommitRequest(...)`` directly.
    """

    from alicebot_api.mcp import memories as mcp_memories
    from alicebot_api.routers import vnext_memories

    assert "memory_commit_request_from_payload(" in inspect.getsource(mcp_memories._handle_alice_vnext_commit_memory)
    assert "memory_commit_request_from_payload(" in inspect.getsource(vnext_memories.commit_vnext_memory)
