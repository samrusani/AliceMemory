"""Helpers the Codex hook tests share. Nothing here imports a host module.

``commit_fact`` puts one known fact in a vault, so a session brief has a line a
test can look for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

_USER_ID = "00000000-0000-0000-0000-000000000001"
_ENV_NAMES = (
    "ALICE_EMBEDDINGS_BASE_URL",
    "ALICE_EMBEDDINGS_MODEL",
    "ALICE_EMBEDDINGS_API_KEY",
    "ALICE_AGENT_API_KEY",
)


def commit_fact(vault: Path, monkeypatch: pytest.MonkeyPatch, title: str, text: str) -> None:
    """Commit ``text`` as a durable fact in ``vault``, through the same door an agent uses."""

    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp_tools import MCPRuntimeContext

    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    database = resolve_db_path(data_dir=str(vault), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=_USER_ID)
    payload = call_mcp_tool(
        context,
        name="alice_memory_commit",
        arguments={
            "title": title,
            "canonical_text": text,
            "memory_type": "decision",
            "domain": "personal",
            "sensitivity": "private",
            "confidence": 0.96,
            "rationale": "User said: remember this",
        },
    )
    assert payload["status"] == "committed", payload
