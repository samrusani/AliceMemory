"""Record planted decisions through the commit and correct tools.

The search benchmark does not call this module. The demo that shows recorded
decisions does. Each write goes through ``alice_memory_commit`` or
``alice_memory_correct`` with that team's key. A profile that may not correct
is refused by the tool, and the leadership key records the replacement.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import scripts.alice_bench as bench
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPNotPermittedError, MCPReferenceNotFoundError, MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key

REASON = "the June price was too high for the renewal"
PRODUCT_AGENT = "showcase-product"


def record_planted_prices(db_path: Path) -> tuple[str, str]:
    """Store the June price and the November price through the tools.

    Returns the two memory ids, older then newer. Sales records the June
    price, the 640 ledger units the company's notes record on 2 June 2025.
    Each team then calls ``alice_memory_correct``. Leadership, which may
    correct, records the replacement, the 520 cut of 18 November 2025. The
    other teams are refused.
    """

    user_id = UUID(bench.DEFAULT_USER_ID)
    keys = _team_keys(db_path, user_id)
    older = _commit(
        db_path,
        user_id,
        keys["showcase-sales"],
        agent_id="showcase-sales",
        permission_profile="trusted_local_agent",
        title="Keel sensor kit price",
        text="the list price was raised to 640 ledger units",
        domain="financial",
        sensitivity="private",
    )
    for agent_id, profile in (
        ("showcase-sales", "trusted_local_agent"),
        ("showcase-support", "memory_proposal_agent"),
        (PRODUCT_AGENT, "project_scoped_agent"),
    ):
        try:
            _correct(
                db_path,
                user_id,
                keys[agent_id],
                agent_id=agent_id,
                permission_profile=profile,
                memory_id=older,
                text="the list price was cut to 520 ledger units",
            )
        except (MCPNotPermittedError, MCPReferenceNotFoundError):
            continue
        else:
            raise RuntimeError(f"{agent_id} was allowed to correct")
    newer = _correct(
        db_path,
        user_id,
        keys["showcase-leadership"],
        agent_id="showcase-leadership",
        permission_profile="admin_agent",
        memory_id=older,
        text="the list price was cut to 520 ledger units",
    )
    _commit(
        db_path,
        user_id,
        keys["showcase-support"],
        agent_id="showcase-support",
        permission_profile="memory_proposal_agent",
        title="support hours",
        text="support hours stay on weekdays",
        domain="project",
        sensitivity="public",
    )
    _commit(
        db_path,
        user_id,
        keys[PRODUCT_AGENT],
        agent_id=PRODUCT_AGENT,
        permission_profile="project_scoped_agent",
        title="product scope",
        text="the product scope stays the bound project",
        domain="project",
        sensitivity="public",
    )
    _commit(
        db_path,
        user_id,
        keys["showcase-leadership"],
        agent_id="showcase-leadership",
        permission_profile="admin_agent",
        title="leadership review",
        text="leadership reviews the recorded prices",
        domain="project",
        sensitivity="public",
    )
    return older, newer


def _team_keys(db_path: Path, user_id: UUID) -> dict[str, str]:
    from scripts.showcase_demo import prepare_demo_product

    prepare_demo_product(db_path)
    keys: dict[str, str] = {}
    with sqlite_user_connection(db_path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        scope = conn.execute(
            "SELECT project_scope FROM agent_api_keys WHERE user_id = ? AND agent_id = ?",
            (str(user_id), PRODUCT_AGENT),
        ).fetchone()
        if scope is None or not scope["project_scope"]:
            raise RuntimeError("the product key has no project")
        project_scope = str(scope["project_scope"])
        conn.execute(
            "UPDATE agent_api_keys SET revoked_at = ? WHERE user_id = ? AND agent_id = ? AND revoked_at IS NULL",
            ("2026-10-14T00:00:00Z", str(user_id), PRODUCT_AGENT),
        )
        _record, raw = create_agent_key(
            store,
            user_id=user_id,
            agent_id=PRODUCT_AGENT,
            permission_profile="project_scoped_agent",
            label="product",
            project_scope=project_scope,
        )
        keys[PRODUCT_AGENT] = raw
        for agent_id, profile in (
            ("showcase-sales", "trusted_local_agent"),
            ("showcase-support", "memory_proposal_agent"),
            ("showcase-leadership", "admin_agent"),
        ):
            _record, raw = create_agent_key(
                store,
                user_id=user_id,
                agent_id=agent_id,
                permission_profile=profile,
                label=agent_id,
            )
            keys[agent_id] = raw
    return keys


def _commit(
    db_path: Path,
    user_id: UUID,
    raw_key: str,
    *,
    agent_id: str,
    permission_profile: str,
    title: str,
    text: str,
    domain: str,
    sensitivity: str,
) -> str:
    arguments: dict[str, object] = {
        "agent_id": agent_id,
        "permission_profile": permission_profile,
        "title": title,
        "canonical_text": text,
        "memory_type": "decision",
        "domain": domain,
        "sensitivity": sensitivity,
        "confidence": 0.95,
        "rationale": REASON,
    }
    result = _call(db_path, user_id, raw_key, "alice_memory_commit", arguments)
    if result.get("status") == "confirmation_required":
        result = _call(
            db_path,
            user_id,
            raw_key,
            "alice_memory_commit",
            {
                "agent_id": agent_id,
                "permission_profile": permission_profile,
                "confirmation_id": result["confirmation_id"],
                "confirmation_action": "confirm",
                "rationale": REASON,
            },
        )
    memory = result["memory"]
    assert isinstance(memory, dict)
    return str(memory["id"])


def _correct(
    db_path: Path,
    user_id: UUID,
    raw_key: str,
    *,
    agent_id: str,
    permission_profile: str,
    memory_id: str,
    text: str,
) -> str:
    result = _call(
        db_path,
        user_id,
        raw_key,
        "alice_memory_correct",
        {
            "agent_id": agent_id,
            "permission_profile": permission_profile,
            "review_item_id": memory_id,
            "action": "supersede-existing",
            "reason": REASON,
            "replacement_body": {"decision_text": text},
            "replacement_title": "Keel sensor kit price",
        },
    )
    replacement = result.get("replacement_object") or result.get("memory")
    assert isinstance(replacement, dict)
    return str(replacement["id"])


def _call(
    db_path: Path,
    user_id: UUID,
    raw_key: str,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    previous_key = os.environ.get("ALICE_AGENT_API_KEY")
    previous_full = os.environ.get("ALICE_MCP_FULL_TOOLS")
    os.environ["ALICE_AGENT_API_KEY"] = raw_key
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    try:
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(db_path), user_id=user_id)
        result = call_mcp_tool(context, name=name, arguments=arguments)
    finally:
        if previous_key is None:
            os.environ.pop("ALICE_AGENT_API_KEY", None)
        else:
            os.environ["ALICE_AGENT_API_KEY"] = previous_key
        if previous_full is None:
            os.environ.pop("ALICE_MCP_FULL_TOOLS", None)
        else:
            os.environ["ALICE_MCP_FULL_TOOLS"] = previous_full
    return result
