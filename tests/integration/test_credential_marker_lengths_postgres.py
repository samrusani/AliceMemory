"""PostgreSQL: ``alice_capture`` takes text with short product codes and still refuses a real-shaped key (2026-10-22).

``tests/unit/test_credential_marker_lengths.py`` holds the rule and the SQLite doors. This file runs the same
capture on PostgreSQL, where the commit gate is the same function and the store is a different one. Before the fix a
code such as ``SK-2291`` made the commit gate read a credential, so the whole capture was refused; a real-shaped key is
refused now as it was then. Every key here is synthetic: a repeated or sequential run built from parts at run time.
"""

from __future__ import annotations

import itertools
import string
from uuid import UUID

import pytest

from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import MCPRuntimeContext, MCPToolError, call_mcp_tool

from tests.integration.test_memory_mutations_api import seed_user

_MIXED = "".join(
    f"{upper}{lower}{digit}"
    for upper, lower, digit in zip(string.ascii_uppercase, string.ascii_lowercase, itertools.cycle(string.digits))
)

NOTE = "\n".join(
    (
        "Fact: SK-2291 is the current seal kit.",
        "Fact: order SK-2192 from stores.",
        "Parts: SK-2291, HD-40213, PO-55102, TB-12, NCR-0042",
        "Note: sk-2291 is current and SK-229 is retired.",
        "Fact: set npm_config_cache to the shared folder.",
        "Fact: SK-20260301 shipped to the east depot.",
        "Order history: SK-2025-0618 and SK-2291-REV were backordered.",
    )
)
REAL_KEYS = {
    "ghp_": "gh" + "p_" + _MIXED[:36],
    "npm_": "np" + "m_" + _MIXED[:36],
    "sk-": "sk" + "-" + _MIXED[:48],
    "glpat-": "glp" + "at-" + _MIXED[:20],
}


def _source_counts(database_url: str, user_id: UUID) -> tuple[int, int]:
    with user_connection(database_url, user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM sources")
            sources = int(cur.fetchone()["n"])
            cur.execute("SELECT count(*) AS n FROM source_chunks")
            chunks = int(cur.fetchone()["n"])
    return sources, chunks


def _stored_text(database_url: str, user_id: UUID) -> str:
    with user_connection(database_url, user_id) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(string_agg(text, E'\\n'), '') AS t FROM source_chunks")
            chunks = str(cur.fetchone()["t"])
            cur.execute("SELECT coalesce(string_agg(title, E'\\n'), '') AS t FROM sources")
            titles = str(cur.fetchone()["t"])
    return chunks + "\n" + titles


def test_alice_capture_accepts_text_with_short_codes_on_postgres(migrated_database_urls) -> None:
    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email="marker-lengths-accept@example.com")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    result = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={"raw_text": NOTE, "title": "Seal kit SK-2291", "domain": "project", "sensitivity": "internal"},
    )
    assert result["status"] == "imported", result
    sources, chunks = _source_counts(app_url, user_id)
    assert sources == 1 and chunks >= 1
    stored = _stored_text(app_url, user_id)
    assert "Seal kit SK-2291" in stored
    for line in NOTE.splitlines():
        assert line in stored, line
    assert "[withheld: credential material]" not in stored


@pytest.mark.parametrize("family", sorted(REAL_KEYS))
def test_alice_capture_still_refuses_a_real_shaped_key_on_postgres(migrated_database_urls, family: str) -> None:
    app_url = migrated_database_urls["app"]
    key = REAL_KEYS[family]
    user_id = seed_user(app_url, email=f"marker-lengths-refuse-{sorted(REAL_KEYS).index(family)}@example.com")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    clean = call_mcp_tool(
        context,
        name="alice_capture",
        arguments={
            "raw_text": "SK-2291 is the current seal kit.",
            "title": "Seal kit",
            "domain": "project",
            "sensitivity": "internal",
        },
    )
    assert clean["status"] == "imported"
    before = _source_counts(app_url, user_id)
    with pytest.raises(MCPToolError, match="credential material"):
        call_mcp_tool(
            context,
            name="alice_capture",
            arguments={
                "raw_text": f"SK-2291 is the current seal kit. Use deploy key {key}",
                "title": "Seal kit",
                "domain": "project",
                "sensitivity": "internal",
            },
        )
    assert _source_counts(app_url, user_id) == before
    assert key not in _stored_text(app_url, user_id)
