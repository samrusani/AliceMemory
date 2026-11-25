"""Paired wall-clock samples through the MCP server."""

from collections.abc import Callable, Sequence
from contextlib import contextmanager, ExitStack
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
from time import perf_counter
from typing import Literal
from uuid import UUID

import psycopg

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.support.lesson_outcomes_contract import BLOCK, seed_record
from tests.unit.test_identity_claim_codes import wire


# published lessons, waiting lessons, tool, PostgreSQL slack, SQLite slack
CASES = {
    "commit-60": (60, 0, "alice_memory_commit", 40, 70),
    "commit-500": (500, 0, "alice_memory_commit", 90, 190),
    "review-60": (60, 10, "alice_memory_review", 250, 450),
    "review-500": (500, 10, "alice_memory_review", 700, 1000),
    "review-500-cap": (500, 30, "alice_memory_review", 750, 1100),
    "ordinary-commit": (500, 0, "alice_memory_commit", 3, 3),
    "empty-review": (500, 0, "alice_memory_review", 3, 3),
    "lessons": (500, 0, "alice_lessons", 3, 3),
}
ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _worker(checkout, vault, key, tool, arguments):
    selected_code = os.pathsep.join((str(checkout / "apps/api/src"), str(ROOT)))
    with subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, env={**os.environ, "PYTHONPATH": selected_code},
    ) as process:
        def tell(payload):
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def sample():
            tell(None)
            line = process.stdout.readline()
            assert line, "MCP timing worker stopped"
            failed, shape, elapsed = json.loads(line)
            assert not failed, shape
            return shape, elapsed

        tell([vault.context.database_url, str(vault.context.user_id), key, tool, arguments])
        try:
            yield sample
        finally:
            process.stdin.close()
            assert process.wait(timeout=45) == 0


def budget_decision(
    rounds: Sequence[tuple[float, float]], slack: float,
) -> Literal["pass", "fail", "measure again"]:
    lowest_base = min(base for base, _ in rounds)
    lowest_head = min(head for _, head in rounds)
    if lowest_head <= lowest_base + slack:
        return "pass"
    if len(rounds) == 3:
        return "fail"
    return "measure again"


def paired_budget(vault, case):
    published, waiting, tool, postgres_slack, sqlite_slack = CASES[case]
    admin = vault.key("publisher", None, "admin_agent")
    writer = vault.key("writer")
    now = datetime.now(UTC)
    with vault.store() as store:
        for _ in range(published):
            seed_record(vault, store, writer="publisher", status="active", decision_at=now,
                        action="agentic_memory_commit", kind="created")
        for _ in range(waiting):
            seed_record(vault, store, writer="writer")
        if vault.admin_url is None:
            store.conn.execute("ANALYZE")
    if vault.admin_url is not None:
        with psycopg.connect(vault.admin_url, autocommit=True) as connection:
            connection.execute("ANALYZE memories, memory_revisions, sources, event_log")

    if tool == "alice_memory_review":
        arguments = {"status": "pending_review", "limit": 40}
    elif tool == "alice_lessons":
        arguments = {"task": BLOCK["applies_when"], "paths": BLOCK["paths"], "limit": 3}
    elif case == "ordinary-commit":
        arguments = {"title": "Reading correction", "canonical_text": "Keep the corrected reading.", "sensitivity": "private"}
    else:
        arguments = {"lesson": BLOCK, "sensitivity": "private"}
    key = admin if tool == "alice_memory_review" else writer
    base = Path(os.environ.get("ALICE_READ_MERGE_BASE_CHECKOUT") or ROOT)
    samples = int(os.environ.get("ALICE_CLOSEST_LESSON_TIMING_SAMPLES", "7"))
    assert samples > 0
    db_type = "postgres" if vault.admin_url else "sqlite"
    limit = postgres_slack if vault.admin_url else sqlite_slack

    def measure_round() -> tuple[float, float]:
        base_times, head_times = [], []
        with ExitStack() as stack:
            before = stack.enter_context(_worker(base, vault, key, tool, arguments))
            after = stack.enter_context(_worker(ROOT, vault, key, tool, arguments))
            before()
            shape, _ = after()
            if tool == "alice_memory_review":
                assert shape.count(3) == min(waiting, 10)
                assert shape.count(None) == max(0, waiting - 10)
            elif case.startswith("commit-") or tool == "alice_lessons":
                assert shape == 3
            for index in range(samples):
                pair = ((before, base_times), (after, head_times))
                for measure, times in pair if index % 2 == 0 else reversed(pair):
                    _, elapsed = measure()
                    times.append(elapsed)
        return median(base_times), median(head_times)

    _require_paired_budget(measure_round, samples=samples, db_type=db_type, case=case, limit=limit)


def _require_paired_budget(
    measure_round: Callable[[], tuple[float, float]],
    *,
    samples: int,
    db_type: str,
    case: str,
    limit: int,
) -> None:
    rounds: list[tuple[float, float]] = []
    for _ in range(3):
        old_ms, new_ms = measure_round()
        rounds.append((old_ms, new_ms))
        print(
            f"CLOSEST_BUDGET {db_type} {case} median of {samples}: "
            f"base {old_ms:.3f} ms, head {new_ms:.3f} ms, slack {limit} ms",
            flush=True,
        )
        if budget_decision(rounds, limit) == "pass":
            return
    raise AssertionError((db_type, case, rounds, limit))


def main():
    database, uid, key, tool, arguments = json.loads(sys.stdin.readline())
    os.environ["ALICE_AGENT_API_KEY"] = key
    context = MCPRuntimeContext(database_url=database, user_id=UUID(uid))
    for _line in sys.stdin:
        started = perf_counter()
        failed, answer = wire(context, tool, arguments)
        elapsed = (perf_counter() - started) * 1000
        if failed:
            shape = answer
        elif tool == "alice_memory_review":
            shape = [len(item["similar"]) if item.get("similar") is not None else None for item in answer["items"]]
        elif tool == "alice_lessons":
            shape = len(answer["lessons"])
        else:
            shape = len(answer["similar"]) if answer.get("similar") is not None else None
        print(json.dumps([failed, shape, elapsed]), flush=True)


if __name__ == "__main__":
    main()
