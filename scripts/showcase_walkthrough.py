"""Scripted showcase walkthrough. It does not call a model.

    python scripts/showcase_walkthrough.py
    python scripts/showcase_walkthrough.py --grades grades.json

The command writes the 1,000-note company, records the price and the account
through alice_memory_commit and alice_memory_correct, then prints what the
tools return for each team's key. Answers are stored notes, not a generated
reply.

Setup is scripted. The script does not extract decisions from notes.

The last part of the transcript is a benchmark summary. Every number in it is
read from the grades file named by ``--grades`` (the file ``showcase-report
--grades`` reads). With no such file the summary shows no number. It then
points to the published result pages, whose titles it reads from those pages.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

import scripts.alice_bench as bench
import scripts.showcase_bench as showcase
import scripts.showcase_visibility as visibility
from scripts.showcase_decisions import record_planted_prices

_FACTS = {str(fact["id"]): fact for fact in visibility.POLICY}
HR_PAY = str(_FACTS["hr-pay"]["text"])
DRAFT_PRICE = str(_FACTS["draft-price"]["text"])
CONTRACT = "QF-C-2041"
ACCOUNT_TITLE = "Larkspur Metals account"
ACCOUNT_TEXT = f"Larkspur Metals keeps service contract {CONTRACT}"
ACCOUNT_REASON = "the renewal kept the February 2025 contract"
PRICE_TOPIC = "Keel sensor kit price"
SALES_RECALL = "Which service contract did Larkspur Metals sign for the Keel sensor kit?"
SUPPORT_RECALL = "What identifier does the Larkspur Metals service agreement carry?"
HR_QUESTION = "What is Mara Quen's compensation?"
DRAFT_QUESTION = "What draft list price is written for the Keel sensor kit?"
PRICE_UNITS = "ledger units"
PUBLISHED_PAGES = (
    "docs/benchmarks/showcase/phase0-baseline.md",
    "docs/benchmarks/showcase/phase1-v0210-results.md",
)
REPOSITORY = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, default=Path("showcase-demo-run"))
    parser.add_argument("--size", type=int, default=1000, choices=showcase.SIZES)
    parser.add_argument(
        "--grades",
        type=Path,
        default=None,
        help="a JSON list of graded answers, the file `showcase-report --grades` reads",
    )
    args = parser.parse_args(argv)
    text = setup_and_run(args.run_dir, size=args.size, grades=args.grades)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def setup_and_run(run_dir: Path, *, size: int, grades: Path | None = None) -> str:
    """Build the company, record the scripted decisions, and return the transcript."""

    showcase.build_company(run_dir, size=size, embeddings=False, extractor="fake")
    database = bench.vault_path(run_dir / bench.VAULT_DIRNAME)
    record_planted_prices(database)
    keys = _read_keys(database)
    _commit_account(database, keys["sales"])
    return meeting_text(database, keys, grades=grades)


def meeting_text(database: Path, keys: dict[str, str], *, grades: Path | None = None) -> str:
    """The four parts of the meeting, from the tools, with no model call.

    Part 3 is only shown when the facts it hides can be read by the teams the
    policy names. A hidden fact that no team can find would make the refusal
    mean nothing, so the control is checked before the line is printed.
    """

    sales_contract = _recall(database, keys, "sales", SALES_RECALL)
    support_contract = _recall(database, keys, "support", SUPPORT_RECALL)
    price = _tool(database, keys["sales"], "alice_what_changed", {"topic": PRICE_TOPIC})
    account = _tool(database, keys["sales"], "alice_what_changed", {"topic": ACCOUNT_TITLE})
    support_price = _tool(database, keys["support"], "alice_what_changed", {"topic": PRICE_TOPIC})
    hr = _recall(database, keys, "support", HR_QUESTION)
    draft = _recall(database, keys, "support", DRAFT_QUESTION)
    hr_control = _recall(database, keys, "leadership", HR_QUESTION)
    draft_control = _recall(database, keys, "sales", DRAFT_QUESTION)
    if HR_PAY not in json.dumps(hr_control) or DRAFT_PRICE not in json.dumps(draft_control):
        raise RuntimeError("a team the policy names could not read the hidden fact, so the refusal shows nothing")
    hidden = json.dumps({"hr": hr, "draft": draft})
    if HR_PAY in hidden or DRAFT_PRICE in hidden:
        raise RuntimeError("the support answer revealed a hidden fact")
    if PRICE_UNITS not in json.dumps(price) or PRICE_UNITS in json.dumps(support_price):
        raise RuntimeError("the recorded prices were not shown to sales only")
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
        f"Support asked: {HR_QUESTION}",
        "The support answer does not include the HR pay. Leadership, asking the same question, was given it.",
        f"Support asked: {DRAFT_QUESTION}",
        "The support answer does not include the draft price. Sales, asking the same question, was given it.",
        f"Support asked what changed: {PRICE_TOPIC}",
        "The support answer holds none of the recorded prices. Sales, asking the same question, was given them.",
        "",
        "4. Benchmark summary",
        summary_text(grades),
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
        "rationale": ACCOUNT_REASON,
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
                "rationale": ACCOUNT_REASON,
            },
        )


def _recall(database: Path, keys: Mapping[str, str], team: str, query: str) -> dict[str, object]:
    """``alice_recall`` through the team's key, asking for every sensitivity the profile may read.

    The harness asks the same way (``sensitivity_allowed`` from the team profile). The key still decides: a request
    can ask for less than its ceiling and not more. Without the list, recall leaves out confidential notes for every
    key, and a refusal could not be told from a search that found nothing.
    """

    from alicebot_api.vnext_agent_control import _profile_sensitivity

    allowed = list(_profile_sensitivity(showcase.TEAM_PROFILES[team]))
    return _tool(database, keys[team], "alice_recall", {"query": query, "sensitivity_allowed": allowed})


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


def summary_text(grades: Path | None) -> str:
    """The benchmark summary: counts read from the grades file, and a pointer to the published pages.

    No number is written here. A line of counts comes from one group of rows of the file, and the interval from those
    counts. With no file the summary has no count at all.
    """

    pointer = _published_pages()
    if grades is None:
        return (
            "No graded answers were given, so this run has no accuracy or leak figure.\n"
            "This walkthrough shows the stored tools at work on a scripted company. It does not measure anything.\n"
            + pointer
        )
    rows = _graded_rows(grades)
    if not rows:
        return "The grades file holds no graded answer, so this run has no accuracy or leak figure.\n" + pointer
    kinds = {showcase.question_kind(row) for row in rows}
    if len(kinds) > 1:
        raise bench.BenchError("the summary refuses to combine question kinds")
    kind = next(iter(kinds))
    if kind == "hidden":
        raise bench.BenchError("a held-out set is reported with showcase-report --heldout, not in the walkthrough")
    lines = [showcase.DEVELOPMENT_HEADER.rstrip() if kind == "development" else _DEMONSTRATION_HEADER]
    groups: dict[tuple[str, str, str, str], list[Mapping[str, object]]] = {}
    skipped = False
    for row in rows:
        if str(row["arm"]).startswith("mem0"):
            skipped = True
            continue
        key = (str(row.get("size", "")), str(row.get("model") or ""), str(row["arm"]), str(row["task"]))
        groups.setdefault(key, []).append(row)
    for (size, model, arm, task), items in sorted(groups.items()):
        correct = sum(1 for item in items if item.get("correct"))
        leaks = sum(1 for item in items if item.get("leak"))
        low, high = showcase.wilson_interval(correct, len(items))
        head = " ".join(part for part in (f"size {size}" if size else "", f"model {model}" if model else "") if part)
        line = (
            f"{head + ' ' if head else ''}arm {arm} task {task}: {correct}/{len(items)} correct, "
            f"interval {low:.2f} to {high:.2f}; leaks {leaks}/{len(items)}"
        )
        if arm == "alice":
            line = f"{line} ({showcase.ALICE_PRIVILEGE_NOTE})"
        lines.append(line)
    if skipped:
        lines.append("mem0 arms are not reported in Phase 0.")
    if any(key[2] == "alice" for key in groups):
        lines.append(showcase.HARNESS_PRIVILEGE_SECTION)
    lines.append(pointer.rstrip("\n"))
    return "\n".join(lines) + "\n"


_DEMONSTRATION_HEADER = "These answers are for the demonstration questions. They are not a published result."


def _graded_rows(path: Path) -> list[dict[str, object]]:
    """The rows of a grades file. A row without an arm, a task and a correct flag is refused."""

    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise bench.BenchError("the grades file is a JSON list of graded answers")
    rows: list[dict[str, object]] = []
    for row in loaded:
        if not isinstance(row, dict) or not {"arm", "task", "correct"} <= set(row):
            raise bench.BenchError("every graded answer names its arm and task and says whether it was correct")
        rows.append(row)
    return rows


def _published_pages() -> str:
    """The published result pages that exist in this checkout, each by the title on its first line."""

    lines: list[str] = []
    for relative in PUBLISHED_PAGES:
        page = REPOSITORY / relative
        if not page.is_file():
            continue
        first = page.read_text(encoding="utf-8").splitlines()[:1]
        title = first[0].lstrip("# ").strip() if first else ""
        lines.append(f"  {title} ({relative})" if title else f"  {relative}")
    if not lines:
        return ""
    return "Published results, with their method and where each run departed from the plan:\n" + "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
