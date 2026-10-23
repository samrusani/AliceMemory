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
    DEFAULT_MAX_SENSITIVITY,
    DEFAULT_MAX_VECTORS,
    MIN_READABLE_CHARS,
    SOURCE_EMBEDDINGS_ENV,
    SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
    SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
    SOURCE_EMBEDDINGS_REMOTE_ENV,
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
        SOURCE_EMBEDDINGS_REMOTE_ENV,
        SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
        SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
        CACHE_MAX_MB_ENV,
        "ALICE_SEARCH_QUALITY=vectors",
        "reindex-embeddings --sources",
        "--sources --drop",
    ):
        assert name in section, name
    assert f"Default `{DEFAULT_MAX_SENSITIVITY}`" in section
    assert f"Default {DEFAULT_MAX_VECTORS:,}" in section
    assert f"Default {DEFAULT_CACHE_MAX_MB}" in section
    assert f"fewer than {MIN_READABLE_CHARS} readable characters" in section
    assert QUERY_EMBEDDING_TIMEOUT_SECONDS == 2.0 and "two second timeout" in section
    assert f"{int(BREAKER_SECONDS)} seconds" in section
    assert DISCOVERY_HEAD_DIVISOR == 3 and "A third of the head" in section
    # The rules are applied again before each request, and the window that is left is stated.
    assert "just before each request of up to 128 texts" in section
    assert "can still reach the endpoint" in section and "do not relabel or scrub" in section
    # Deleting a vector is logical deletion, and a caller with limits is told no more than "unavailable".
    assert "logical deletion" in section and "`VACUUM`" in section and "`retained_data`" in section
    assert "`vector search unavailable`" in section


def test_the_security_page_lists_what_leaves_the_machine_and_when() -> None:
    """Mutation: delete the section, the remote opt-in sentence, or the sentence about the query."""

    page = _flat("SECURITY.md")
    section = page[page.index("## What Text Leaves the Machine") : page.index("## Browser Clipper Credentials")]
    for needle in (
        "Nothing from a source is sent unless `ALICE_SOURCE_EMBEDDINGS=on`",
        "`ALICE_SOURCE_EMBEDDINGS_REMOTE=on`",
        "a source labelled `unknown` is held at `private`",
        "a chunk the credential check flags",
        "just before each request of up to 128 texts",
        "can still reach the endpoint",
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
    assert "`ALICE_SOURCE_EMBEDDINGS`" in mcp and "ALICE_SOURCE_EMBEDDINGS_REMOTE=on" in mcp
    assert "`ALICE_SEARCH_QUALITY=vectors`" in mcp
    assert "checked again just before each request" in mcp and "can still reach the" in mcp
    assert "ALICE_SOURCE_EMBEDDINGS=on" in _flat("docs/alpha/agent-integration.md")
