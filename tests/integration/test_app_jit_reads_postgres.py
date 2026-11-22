"""Measure the application JIT policy through real PostgreSQL calls."""

from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
from zipfile import ZipFile

import psycopg
from psycopg.types.json import Jsonb
import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_identity_claim_codes import wire

ROOT = Path(__file__).resolve().parents[2]
MAIN = "04aad19835a6519649879adfbeb971c20f49425c"
SAMPLES = 15
QUERY = "synthetic calibration"


def _base_checkout(tmp_path):
    if configured := os.getenv("ALICE_READ_MERGE_BASE_CHECKOUT"):
        revision = subprocess.check_output(["git", "-C", configured, "rev-parse", "HEAD"], text=True).strip()
        if revision == MAIN:
            return Path(configured)
    archive = tmp_path / "main.zip"
    repository = Path(os.environ.get("BENCH_TEST_SOURCE", ROOT))
    with archive.open("wb") as output:
        subprocess.run(["git", "archive", "--format=zip", MAIN], cwd=repository, stdout=output, check=True)
    checkout = tmp_path / "main"
    with ZipFile(archive) as source:
        source.extractall(checkout)
    return checkout


@contextmanager
def _probe(checkout, harness, keys):
    code = os.pathsep.join((str(checkout / "apps/api/src"), str(ROOT)))
    with subprocess.Popen(
        [sys.executable, str(ROOT / "tests/support/postgres_jit_probe.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": code},
    ) as process:
        process.stdin.write(json.dumps([harness.urls["app"], str(harness.user_id), keys]) + "\n")
        process.stdin.flush()
        try:
            def measure(tool, profile, arguments, mode=None):
                process.stdin.write(json.dumps([tool, profile, arguments, mode]) + "\n")
                process.stdin.flush()
                output = process.stdout.readline()
                assert output, f"{tool} probe ended before returning a result"
                sample = json.loads(output)
                assert sample["ok"], sample["error"]
                return sample

            yield measure
        finally:
            process.stdin.close()
            assert process.wait(timeout=15) == 0


def _compare(base, head, tool, profile, arguments, *, count=SAMPLES):
    base_samples, head_samples = [], []
    for index in range(count):
        pair = [(base, base_samples), (head, head_samples)]
        for probe, samples in pair if index % 2 == 0 else reversed(pair):
            samples.append(probe(tool, profile, arguments)["ms"])
    return median(base_samples), median(head_samples)


def _runtime(harness, capsys):
    with psycopg.connect(harness.urls["app"], autocommit=True) as conn:
        jit = conn.execute("SHOW jit").fetchone()[0]
        available = conn.execute("SELECT pg_jit_available()").fetchone()[0]
        cost = float(conn.execute("SHOW jit_above_cost").fetchone()[0])
        version = conn.execute("SHOW server_version").fetchone()[0]
    with capsys.disabled():
        print(f"JIT benchmark PostgreSQL {version}: plain jit={jit}, pg_jit_available={available}, "
              f"jit_above_cost={cost:g}", flush=True)
    if jit != "on" or not available:
        pytest.skip(f"timing requires plain jit=on and a provider (jit={jit}, available={available})")
    return cost


def _analyze(harness):
    with psycopg.connect(harness.urls["admin"], autocommit=True) as conn:
        conn.execute("ANALYZE memories, memory_revisions, sources, event_log, generated_artifacts, open_loops")


def _prepare(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    for env in ("ALICE_EMBEDDINGS_BASE_URL", "ALICE_EMBEDDINGS_MODEL", "ALICE_EMBEDDINGS_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    return {"bound": harness.key("project_scoped_agent", project="alpha"), "unbound": harness.key("admin_agent")}


def _seed_published_vault(harness, monkeypatch):
    key = harness.key("admin_agent", project="alpha")
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    for index in range(1_200):
        fields = {"sensitivity": "private", "domain": "project"}
        if index < 1_000:
            fields.update(memory_type="semantic", title=f"Synthetic calibration {index}",
                          canonical_text=f"Synthetic calibration {index} tracks observations in an isolated vault.")
        else:
            fields["lesson"] = {
                "applies_when": f"Checking synthetic calibration {index}",
                "what_happened": "The reading drifted.",
                "do_instead": "Reset the fixture and check it again.",
                "paths": ["fixtures/sensor.py"],
            }
        failed, result = wire(context, "alice_memory_commit", fields)
        assert not failed and result["status"] == "committed", result


def test_application_jit_read_bars_and_statement_counts(label_harness, monkeypatch, tmp_path, capsys):
    """Guard: enable JIT in the common user setup and the bound reads miss their bars."""
    h = label_harness
    threshold = _runtime(h, capsys)
    keys = _prepare(h, monkeypatch, tmp_path)
    _seed_published_vault(h, monkeypatch)
    _analyze(h)
    read_bars = {
        "alice_recall": 0.25, "alice_resume": 0.5, "alice_context_pack": 0.25, "alice_lessons": None,
    }
    failures = []
    with ExitStack() as stack:
        base = stack.enter_context(_probe(_base_checkout(tmp_path), h, keys))
        head = stack.enter_context(_probe(ROOT, h, keys))
        old_plans = base("alice_recall", "bound", {"query": QUERY}, "plan")["plans"]
        assert max(plan["cost"] for plan in old_plans) > threshold
        assert any(plan["jit"] for plan in old_plans)
        for tool, bound_ratio in read_bars.items():
            arguments = {"task": QUERY, "limit": 3} if tool == "alice_lessons" else {"query": QUERY}
            for profile in ("bound", "unbound"):
                old_count = base(tool, profile, arguments, "count")["statement_count"]
                new_count = head(tool, profile, arguments, "count")["statement_count"]
                assert old_count == new_count and old_count > 0, (tool, profile, old_count, new_count)
                before, after = _compare(base, head, tool, profile, arguments)
                with capsys.disabled():
                    print(f"Application JIT {tool} {profile}: main {before:.3f} ms, head {after:.3f} ms, "
                          f"statements {old_count}/{new_count}", flush=True)
                limit = before + 10 if profile == "unbound" else (before * bound_ratio if bound_ratio else None)
                if limit is not None and after > limit:
                    failures.append((tool, profile, before, after, limit))
    assert not failures, failures


def test_project_scoped_scheduler_sweep_avoids_jit(label_harness, monkeypatch, tmp_path, capsys):
    """Guard: enable JIT in the shared setup and the scheduler compiles its scoped statement."""
    h = label_harness
    threshold = _runtime(h, capsys)
    keys = _prepare(h, monkeypatch, tmp_path)
    with h.store() as store:
        store.conn.execute(
            """
            INSERT INTO memories (user_id, memory_key, value, status, source_event_ids,
                                  memory_type, canonical_text, domain, sensitivity, metadata_json)
            SELECT app.current_user_id(), 'sweep-fixture.' || n, '{}', 'active', '[]',
                   'project_state', 'Synthetic task state', 'project', 'internal', %s
            FROM generate_series(1, 1200) AS n
            """,
            (Jsonb({"project_scope": ["alpha"]}),),
        )
    _analyze(h)
    args = {"workflow_type": "staleness_sweep", "projects": ["alpha"],
            "domains": ["project"], "sensitivity_allowed": ["internal"]}
    with ExitStack() as stack:
        base = stack.enter_context(_probe(_base_checkout(tmp_path), h, keys))
        head = stack.enter_context(_probe(ROOT, h, keys))
        old = base("staleness_sweep", "bound", args, "plan")
        new = head("staleness_sweep", "bound", args, "plan")
        before, after = _compare(base, head, "staleness_sweep", "bound", args, count=9)
    with capsys.disabled():
        print(f"Scheduler JIT sweep: main {before:.3f} ms, head {after:.3f} ms", flush=True)
    assert old["sweep_jit"] == ["on"] and new["sweep_jit"] == ["off"]
    assert any(plan["scoped_sweep"] and plan["cost"] > threshold and plan["compiled_ms"] > 0 for plan in old["plans"])
    assert any(plan["scoped_sweep"] for plan in new["plans"])
    assert not any(plan["jit"] for plan in new["plans"])
