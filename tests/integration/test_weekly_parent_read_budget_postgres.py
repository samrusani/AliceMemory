"""A vault of 5,000 weekly memories and 10,000 artifacts stays inside the pre-parent-lookup gate.

The comparison checkout is ``7c77c778``, the commit before the per-memory parent query. The head
passes when each measured time is at most 1.25 times that checkout plus 0.05 seconds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from alembic import command

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.conftest import database_urls  # noqa: F401
from tests.performance.read_budget_gate import over_merge_base

BASE = "7c77c77842ba8a9a85034f9c5a7d252b5048dbe4"
TOKEN = "weeklynesttoken"
WEEKLY_MEMORIES = 5000
ARTIFACTS = 10000
SAMPLES = 3
ACTIONS = ("workspace", "recall", "pack", "count")


def _base_checkout(dest: Path) -> Path:
    repo = Path(__file__).resolve().parents[2]
    override = os.environ.get("ALICE_WEEKLY_PARENT_BASE", "").strip()
    if override:
        dest = Path(override)
    if dest.exists():
        rev = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
        if rev == BASE:
            return dest
        subprocess.check_call(["git", "-C", str(repo), "worktree", "remove", "--force", str(dest)])
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), BASE])
    rev = subprocess.check_output(["git", "-C", str(dest), "rev-parse", "HEAD"], text=True).strip()
    assert rev == BASE
    return dest


def _seed(app_url: str, user_id) -> str:
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(user_id, f"owner-{user_id.hex[:8]}@example.invalid", "Owner")
        values = []
        for index in range(WEEKLY_MEMORIES):
            row_id = str(uuid4())
            meta = json.dumps(
                {
                    "candidate": True,
                    "discovered_by": "vnext_weekly_synthesis",
                    "project_scope": [],
                    "workflow_digest": uuid4().hex,
                    "observation_index": index,
                }
            )
            text = f"The weekly pattern names {TOKEN} {index}."
            values.append((row_id, str(user_id), "budget." + row_id, text, meta))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO memories(id,user_id,memory_key,canonical_text,metadata_json,value,source_event_ids,"
                "status,domain,sensitivity) VALUES(%s::uuid,%s::uuid,%s,%s,%s::jsonb,'{}'::jsonb,'{}'::jsonb,"
                "'active','project','public')",
                values,
            )
            cur.execute(
                """
                INSERT INTO generated_artifacts (
                    id, user_id, artifact_type, title, content_markdown, status, domain, sensitivity,
                    generated_by, metadata_json
                )
                SELECT gen_random_uuid(), %s, 'draft', 'filler', 'x', 'needs_review', 'project', 'public',
                       'system', '{}'::jsonb
                FROM generate_series(1, %s)
                """,
                (user_id, ARTIFACTS),
            )
        from alicebot_api.vnext_store import PostgresVNextStore

        _record, raw = create_agent_key(
            PostgresVNextStore(conn),
            user_id=user_id,
            agent_id="trusted-" + uuid4().hex[:8],
            permission_profile="trusted_local_agent",
        )
    return raw


def _open(checkout: Path, app_url: str, user_id, key: str) -> subprocess.Popen:
    script = Path(__file__).resolve().parents[1] / "performance" / "weekly_parent_budget_probe.py"
    env = {**os.environ, "ALICE_WEEKLY_BUDGET_QUERY": TOKEN, "DATABASE_URL": app_url}
    return subprocess.Popen(
        [sys.executable, str(script), str(checkout), app_url, str(user_id), key],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _ask(process: subprocess.Popen, action: str) -> dict:
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(action + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def test_weekly_parent_reads_stay_within_the_pre_lookup_gate(database_urls, tmp_path) -> None:
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    user_id = uuid4()
    key = _seed(database_urls["app"], user_id)
    repo = Path(__file__).resolve().parents[2]
    base = _base_checkout(tmp_path / "base")
    head = repo
    processes = [_open(checkout, database_urls["app"], user_id, key) for checkout in (base, head)]
    try:
        for action in ACTIONS:
            for process in processes:
                _ask(process, action)
        walls = {action: {"base": [], "head": []} for action in ACTIONS}
        queries = []
        for sample in range(SAMPLES):
            order = (0, 1) if sample % 2 == 0 else (1, 0)
            for action in ACTIONS:
                for index in order:
                    measured = _ask(processes[index], action)
                    side = "base" if index == 0 else "head"
                    walls[action][side].append(measured["wall"])
                    if action == "count" and side == "head":
                        queries.append(measured["queries"])
        print(json.dumps({"base": str(base), "samples": SAMPLES, "walls": walls, "queries": queries}), flush=True)
        assert queries == [1, 1, 1]
        for action in ACTIONS:
            assert not over_merge_base(min(walls[action]["head"]), min(walls[action]["base"])), (
                action,
                walls[action],
            )
    finally:
        for process in processes:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write("stop\n")
                process.stdin.flush()
            process.communicate(timeout=30)
        subprocess.call(["git", "-C", str(repo), "worktree", "remove", "--force", str(base)])
