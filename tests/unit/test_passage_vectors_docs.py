"""The pages say what the opt-in sends, when, and what the vector mode needs and costs.

Each test names the edit that must fail it: delete the named sentence or variable from the page.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api.source_chunk_vector_search import (
    BREAKER_SECONDS,
    DISCOVERY_HEAD_DIVISOR,
    QUERY_EMBEDDING_TIMEOUT_SECONDS,
)
from alicebot_api.source_embedding_policy import (
    DEFAULT_MAX_VECTORS,
    MIN_READABLE_CHARS,
    SOURCE_EMBEDDINGS_ENV,
    SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
)
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import CACHE_MAX_MB_ENV, DEFAULT_CACHE_MAX_MB

ROOT = Path(__file__).resolve().parents[2]


def _flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def test_the_tool_reference_names_every_variable_default_and_rule() -> None:
    """Mutation: delete a variable, a default or the 32 character floor from the section."""

    page = _flat("docs/alpha/mcp-tools.md")
    section = page[page.index("### Embeddings of source chunks") : page.index("## Explicit memory commits")]
    for name in (
        SOURCE_EMBEDDINGS_ENV,
        SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
        CACHE_MAX_MB_ENV,
        "ALICE_SEARCH_QUALITY=vectors",
        "reindex-embeddings --sources",
        "--sources --drop",
    ):
        assert name in section, name
    # Source text goes only to an endpoint on this machine, and no sentence of the page describes a remote path.
    assert "Remote endpoints are not supported for source embeddings in this release" in section
    assert "`endpoint_not_on_this_machine`" in section and "source embeddings need an endpoint on this machine" in section
    assert "ALICE_SOURCE_EMBEDDINGS_REMOTE" not in section and "MAX_SENSITIVITY" not in section
    assert f"Default {DEFAULT_MAX_VECTORS:,}" in section
    assert f"Default {DEFAULT_CACHE_MAX_MB}" in section
    assert f"fewer than {MIN_READABLE_CHARS} readable characters" in section
    assert QUERY_EMBEDDING_TIMEOUT_SECONDS == 2.0 and "two second timeout" in section
    assert f"{int(BREAKER_SECONDS)} seconds" in section
    assert DISCOVERY_HEAD_DIVISOR == 3 and "A third of the head" in section
    # The rules are applied again before each request, and the window that is left is stated.
    assert "just before each request of up to 128 texts" in section
    assert "a change of label is not an egress event" in section
    assert "after that pre-send read has read the source" in section and "whether the request has begun or not" in section
    assert "before a byte or a TLS handshake is written" in section and "an IPv4-mapped form is refused" in section
    # Deleting a vector is logical deletion, and a caller with limits is told no more than "unavailable".
    assert "logical deletion" in section and "`VACUUM`" in section and "`retained_data`" in section
    assert "`vector search unavailable`" in section
    # A caller with limits is served from a copy of the rows it may read, and the pages say what that costs.
    assert "a copy of the vectors of the sources it may read and of no others" in section
    assert "at most four for a user by default" in section and "256 MB by default" in section
    assert "Copies are kept by count and never by size" in section and "512 + 4 × 256 = 1,536 MB of vector arrays" in section
    assert "It is not the most memory the process holds" in section and "are not counted" in section
    assert "most memory a process holds" not in section
    assert "ALICE_SOURCE_VECTOR_SCOPED_CACHE_MAX_MB" in section and "ALICE_SOURCE_VECTOR_SCOPED_COPIES" in section
    assert "A scoped copy over its cap reports the generic status" in section


def test_the_security_page_lists_what_leaves_the_machine_and_when() -> None:
    """Mutation: delete the section, the remote opt-in sentence, or the sentence about the query."""

    page = _flat("SECURITY.md")
    section = page[page.index("## What Text Leaves the Machine") : page.index("## Browser Clipper Credentials")]
    assert "ALICE_SOURCE_EMBEDDINGS_REMOTE" not in section and "MAX_SENSITIVITY" not in section
    for needle in (
        "Nothing from a source is sent unless `ALICE_SOURCE_EMBEDDINGS=on`",
        "Source text goes only to an endpoint on this machine",
        "Remote endpoints are not supported for source embeddings in this release",
        "a chunk the credential check flags",
        "just before each request of up to 128 texts",
        "after that pre-send read has read the source",
        "whether the request has begun or not",
        "Deleting is logical",
        "`VACUUM`",
        "with no database lock held",
        "no tool starts it",
        "each `alice_recall` sends its query",
        "deleting a source",
    ):
        assert needle in section, needle


def test_the_integration_page_and_the_agent_table_name_the_opt_in() -> None:
    """Mutation: delete the variable from either page."""

    mcp = _flat("docs/integrations/mcp.md")
    assert "`ALICE_SOURCE_EMBEDDINGS`" in mcp and "remote endpoints are not supported for source embeddings" in mcp
    assert "ALICE_SOURCE_EMBEDDINGS_REMOTE" not in mcp
    assert "`ALICE_SEARCH_QUALITY=vectors`" in mcp
    assert "checked again just before each request" in mcp and "pre-send read has read the source" in mcp
    assert "ALICE_SOURCE_VECTOR_SCOPED_CACHE_MAX_MB" in mcp and "ALICE_SOURCE_VECTOR_SCOPED_COPIES" in mcp
    assert "limit the vector" in mcp and "memory of the process is" in mcp
    assert "ALICE_SOURCE_EMBEDDINGS=on" in _flat("docs/alpha/agent-integration.md")
