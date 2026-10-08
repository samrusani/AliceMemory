"""Native 5,000-row read budgets, compared with a supplied main checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import psycopg

from tests.integration.derived_labels_postgres_support import label_harness
from tests.performance.test_label_read_handoff_budget import seed_copies


def probe(repo, backend, location, user):
    script = Path(__file__).resolve().parents[1] / "performance/read_budget_probe.py"
    completed = subprocess.run([sys.executable, str(script), str(repo), backend, str(location), str(user)],
                               capture_output=True, text=True, check=True, timeout=120)
    return json.loads(completed.stdout.strip().splitlines()[-1])


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
    head = probe(repo, "postgres", h.urls["app"], h.user_id)
    print("PostgreSQL hidden=" + str(hidden) + " head=" + json.dumps(head))
    assert head["workspace"]["median"] <= 1.0, head
    assert head["dogfooding"]["median"] <= 1.0, head
    main = os.environ.get("ALICE_READ_MAIN_CHECKOUT")
    if main:
        baseline = probe(main, "postgres", h.urls["app"], h.user_id)
        print("PostgreSQL main=" + json.dumps(baseline))
        for action in ("pack", "recall"):
            assert head[action]["median"] <= 2 * baseline[action]["median"] + .1, (action, head, baseline)
    else:
        for action in ("pack", "recall"):
            assert head[action]["median"] <= .15, (action, head)
