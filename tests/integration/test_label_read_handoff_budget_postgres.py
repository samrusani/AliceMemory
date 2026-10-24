"""Native 5,000-row read budgets, compared with a supplied main checkout."""
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys

import pytest
import psycopg

from tests.integration.derived_labels_postgres_support import label_harness
from tests.performance.read_budget_gate import (
    PAIRED_SAMPLES,
    append_evidence,
    merge_base_checkout,
    paired_over_merge_base,
    paired_summary,
    prelabels_action,
    prelabels_checkout,
)
from tests.performance.test_label_read_handoff_budget import seed_copies
from tests.performance.test_label_round3_read_budget import absolute_limit_seconds


def open_probe(repo, backend, location, user):
    script = Path(__file__).resolve().parents[1] / "performance/read_budget_probe.py"
    return subprocess.Popen(
        [sys.executable, str(script), str(repo), backend, str(location), str(user)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _read_sample(process):
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return json.loads(line)


def close_probe(process):
    if process.poll() is None:
        process.stdin.write("stop\n")
        process.stdin.flush()
    process.communicate(timeout=30)


def interleaved_medians(base_process, head_process, actions, samples=PAIRED_SAMPLES):
    """Wall and CPU time of ``samples`` interleaved calls, after one unmeasured call each, as ``paired_summary`` gives them."""

    measured = {"merge_base": {}, "head": {}}
    for action in actions:
        for process in (base_process, head_process):
            process.stdin.write(action + "\n")
            process.stdin.flush()
            _read_sample(process)
        arrays = [{"wall": [], "cpu": []} for _ in range(2)]
        for sample in range(samples):
            for index in (0, 1) if sample % 2 == 0 else (1, 0):
                process = (base_process, head_process)[index]
                process.stdin.write(action + "\n")
                process.stdin.flush()
                result = _read_sample(process)
                arrays[index]["wall"].append(result["wall"])
                arrays[index]["cpu"].append(result["cpu"])
        for index, revision in enumerate(("merge_base", "head")):
            measured[revision][action] = paired_summary(arrays[index]["wall"], arrays[index]["cpu"])
    return measured


def solo_medians(process, actions, samples=5):
    measured = {}
    for action in actions:
        process.stdin.write(action + "\n")
        process.stdin.flush()
        _read_sample(process)
        taken = []
        for _ in range(samples):
            process.stdin.write(action + "\n")
            process.stdin.flush()
            taken.append(_read_sample(process)["wall"])
        measured[action] = {"median": statistics.median(taken), "samples": taken}
    return measured


def prelabels_medians(process, actions, head, samples=5):
    report = {"gated": False, "actions": {}}
    for action in actions:
        process.stdin.write(action + "\n")
        process.stdin.flush()
        _read_sample(process)
        taken = []
        for _ in range(samples):
            process.stdin.write(action + "\n")
            process.stdin.flush()
            taken.append(_read_sample(process)["wall"])
        report["actions"][action] = prelabels_action(head[action]["median"], statistics.median(taken))
    return report


@pytest.mark.parametrize("hidden", [False, True])
def test_postgres_five_thousand_native_read_budgets(label_harness, hidden):
    h = label_harness
    with h.store() as store:
        source, _ = seed_copies(store, postgres=True)
        if not hidden:
            store.conn.execute("UPDATE sources SET sensitivity='public' WHERE id=%s", (source["id"],))
    with psycopg.connect(h.urls["admin"], autocommit=True) as conn:
        conn.execute("ANALYZE memories")
        conn.execute("ANALYZE event_log")
        conn.execute("ANALYZE sources")
    repo = Path(__file__).resolve().parents[2]
    # Pack and recall are the merge-base gate. The screens are timed only when the absolute
    # gate is on, which is the maximum-workload smoke job. The required job does not spend its session on them.
    actions = ("pack", "recall")
    if os.environ.get("ALICE_READ_ABSOLUTE_GATE") == "1":
        actions = ("pack", "recall", "workspace", "dogfooding")
    merge_base = os.environ.get("ALICE_READ_MERGE_BASE_CHECKOUT", "").strip()
    if merge_base:
        processes = [open_probe(checkout, "postgres", h.urls["app"], h.user_id) for checkout in (merge_base_checkout(), repo)]
        try:
            measured = interleaved_medians(processes[0], processes[1], actions)
        finally:
            for process in processes:
                close_probe(process)
        head = measured["head"]
        report = None
        prelabels = prelabels_checkout()
        if prelabels:
            process = open_probe(prelabels, "postgres", h.urls["app"], h.user_id)
            try:
                report = prelabels_medians(process, actions, head)
            finally:
                close_probe(process)
        row = {"store": "postgres", "hidden": hidden, "gate": "merge_base", "merge_base": measured["merge_base"], "head": head}
        if report is not None:
            row["prelabels_report"] = report
        print(json.dumps(row), flush=True)
        append_evidence(row)
        for action in ("pack", "recall"):
            over = paired_over_merge_base(head[action], measured["merge_base"][action])
            assert not over, (action, over, row)
    else:
        process = open_probe(repo, "postgres", h.urls["app"], h.user_id)
        try:
            head = solo_medians(process, actions)
        finally:
            close_probe(process)
        print("PostgreSQL hidden=" + str(hidden) + " head=" + json.dumps(head))
        for action in ("pack", "recall"):
            assert head[action]["median"] <= .15, (action, head)
    # The screen median limit is absolute_limit_seconds(), and only when the absolute gate is on.
    if os.environ.get("ALICE_READ_ABSOLUTE_GATE") == "1":
        limit = absolute_limit_seconds()
        for action in ("workspace", "dogfooding"):
            assert head[action]["median"] <= limit, (action, head[action])
