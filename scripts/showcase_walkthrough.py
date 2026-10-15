"""Scripted showcase walkthrough. It does not call a model.

    python scripts/showcase_walkthrough.py

The command writes the 1,000-note company, records the price and the account
through alice_memory_commit and alice_memory_correct, then prints what the
tools return. Answers are stored notes, not a generated reply. A graded bench
file named answers.jsonl in the run directory is printed as the summary table.
When that file is absent the table is empty, and the script does not invent a
score.

Setup is scripted. The script does not extract decisions from notes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
from scripts.showcase_decisions import REASON, record_planted_prices

HR_PAY = "128400"
DRAFT_PRICE = "910 ledger"
CONTRACT = "QF-C-2041"
ACCOUNT_TITLE = "Larkspur Metals account"
ACCOUNT_TEXT = "Larkspur Metals keeps service contract QF-C-2041"
PRICE_TOPIC = "Keel sensor kit price"
SALES_RECALL = "Which service contract did Larkspur Metals sign for the Keel sensor kit?"
SUPPORT_RECALL = "What identifier does the Larkspur Metals service agreement carry?"
HR_QUESTION = "What is Mara Quen's pay?"
DRAFT_QUESTION = "What draft price is written for the Keel sensor kit?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("showcase-demo-run"))
    parser.add_argument("--size", type=int, default=1000, choices=showcase.SIZES)
    args = parser.parse_args(argv)
    text = setup_and_run(args.run_dir, size=args.size)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def setup_and_run(run_dir: Path, *, size: int) -> str:
    """Build the company, record the scripted decisions, and return the transcript."""

    showcase.build_company(run_dir, size=size, embeddings=False, extractor="fake")
    database = bench.vault_path(run_dir / bench.VAULT_DIRNAME)
    record_planted_prices(database)
    keys = _read_keys(database)
    _commit_account(database, keys["sales"])
    return meeting_text(database, keys, run_dir)


def meeting_text(database: Path, keys: dict[str, str], run_dir: Path) -> str:
    """The four parts of the meeting, from the tools, with no model call."""

    sales_contract = _tool(database, keys["sales"], "alice_recall", {"query": SALES_RECALL})
    support_contract = _tool(database, keys["support"], "alice_recall", {"query": SUPPORT_RECALL})
    price = _tool(database, keys["sales"], "alice_what_changed", {"topic": PRICE_TOPIC})
    account = _tool(database, keys["sales"], "alice_what_changed", {"topic": ACCOUNT_TITLE})
    hr = _tool(database, keys["support"], "alice_recall", {"query": HR_QUESTION})
    draft = _tool(database, keys["support"], "alice_recall", {"query": DRAFT_QUESTION})
    hidden = json.dumps({"hr": hr, "draft": draft})
    if HR_PAY in hidden or DRAFT_PRICE in hidden:
        raise RuntimeError("the support answer revealed a hidden fact")
    lines = [
        "Setup is scripted. Decisions were recorded with alice_memory_commit and alice_memory_correct.",
        "No model was called.",
        "",
        "1. Recall",
        f"Sales asked: {SALES_RECALL}",
        _short(sales_contract),
        f"Support asked: {SUPPORT_RECALL}",
        _short(support_contract),
        "",
        "2. What changed and why",
        f"Account: {ACCOUNT_TITLE}",
        _changed(account),
        f"Price: {PRICE_TOPIC}",
        _changed(price),
        "",
        "3. Support is refused the HR note and the draft price",
        "The support answer names neither the HR pay nor the draft price.",
        "",
        "4. Benchmark summary",
        _summary(run_dir),
    ]
    return "\n".join(lines) + "\n"


def _read_keys(database: Path) -> dict[str, str]:
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_agent_keys import create_agent_key

    user_id = UUID(bench.DEFAULT_USER_ID)
    keys: dict[str, str] = {}
    with sqlite_user_connection(database, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        for team, profile in showcase.TEAM_PROFILES.items():
            _record, raw = create_agent_key(
                store,
                user_id=user_id,
                agent_id=f"showcase-walk-{team}",
                permission_profile=profile,
                label=f"walk-{team}",
            )
            keys[team] = raw
    return keys


def _commit_account(database: Path, sales_key: str) -> None:
    arguments: dict[str, object] = {
        "agent_id": "showcase-walk-sales",
        "permission_profile": "trusted_local_agent",
        "title": ACCOUNT_TITLE,
        "canonical_text": ACCOUNT_TEXT,
        "memory_type": "decision",
        "domain": "professional",
        "sensitivity": "internal",
        "confidence": 0.95,
        "rationale": REASON,
    }
    result = _tool(database, sales_key, "alice_memory_commit", arguments)
    if result.get("status") == "confirmation_required":
        _tool(
            database,
            sales_key,
            "alice_memory_commit",
            {
                "agent_id": "showcase-walk-sales",
                "permission_profile": "trusted_local_agent",
                "confirmation_id": result["confirmation_id"],
                "confirmation_action": "confirm",
                "rationale": REASON,
            },
        )


def _tool(database: Path, raw_key: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path

    previous_key = os.environ.get("ALICE_AGENT_API_KEY")
    previous_full = os.environ.get("ALICE_MCP_FULL_TOOLS")
    os.environ["ALICE_AGENT_API_KEY"] = raw_key
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    try:
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(bench.DEFAULT_USER_ID))
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


def _short(payload: dict[str, object]) -> str:
    text = json.dumps(payload)
    if CONTRACT in text:
        return f"The stored notes include {CONTRACT}."
    return "The stored notes do not include the contract identifier."


def _changed(payload: dict[str, object]) -> str:
    notice = str(payload.get("notice") or "")
    message = payload.get("message")
    chains = payload.get("chains")
    lines = [notice]
    if isinstance(message, str) and message:
        lines.append(message)
    if isinstance(chains, list):
        for chain in chains:
            if not isinstance(chain, dict):
                continue
            current = chain.get("current")
            if isinstance(current, dict):
                lines.append(f"Current: {current.get('change')}")
                reason = current.get("reason")
                if isinstance(reason, dict) and reason.get("text"):
                    lines.append(f"Reason: {reason.get('text')}")
            history = chain.get("history")
            if isinstance(history, list):
                for step in history:
                    if isinstance(step, dict):
                        lines.append(f"History: {step.get('date')} {step.get('change')}")
            for label in ("upcoming", "disputed"):
                values = chain.get(label)
                if isinstance(values, list) and values:
                    lines.append(f"{label}: {', '.join(str(item) for item in values)}")
    return "\n".join(lines)


def _summary(run_dir: Path) -> str:
    path = run_dir / "answers.jsonl"
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    report = showcase.render_report(rows)
    if rows:
        return report.rstrip()
    return report + "No graded answers are recorded. The table stays empty until a batch is graded."


if __name__ == "__main__":
    raise SystemExit(main())
