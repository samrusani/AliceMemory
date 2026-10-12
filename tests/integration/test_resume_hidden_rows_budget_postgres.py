"""Resume and the grounding probe stay within a budget of the baseline when many newer rows are hidden from the caller, on PostgreSQL.

Unreleased (on main, not in v0.20.0). The measurement of ``tests/performance/test_resume_hidden_rows_budget.py`` on a real
PostgreSQL store, against the merge base named by ``ALICE_READ_MERGE_BASE_CHECKOUT``: 10,000 hidden rows newer than twelve
readable decisions in two shapes, 100 hidden rows, and a vault of 200,000 sources with no hidden row, each with an event. The
tables are analyzed before any sample, as a vault that has been used would have been: without statistics the planner joins
``event_log`` to ``memories`` with a nested loop that takes seconds on the baseline as well.
"""

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.test_label_round3_read_budget_postgres import analyze_budget_tables
from tests.performance.test_resume_hidden_rows_budget import BULK_SOURCES, CASES, paired_resume_budgets, seed_hidden_rows


@pytest.mark.parametrize("case", list(CASES))
def test_postgres_resume_stays_within_the_budget_of_the_baseline_with_many_hidden_rows(label_harness, case):  # noqa: F811
    h = label_harness
    hidden, shape = CASES[case]
    with h.store() as store:
        seed_hidden_rows(store, postgres=True, hidden=hidden, shape=shape)
        if case == "sources":
            with store.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sources(id,user_id,source_type,title,content_hash,domain,sensitivity,metadata_json) "
                    "SELECT gen_random_uuid(), %s::uuid, 'note', 'bulk '||g, md5(g::text), "
                    "CASE WHEN g %% 10 = 0 THEN 'health' ELSE 'project' END, 'public', '{}'::jsonb FROM generate_series(1, %s) g",
                    (str(h.user_id), BULK_SOURCES),
                )
    key = h.key("read_only_agent")
    analyze_budget_tables(h.urls["admin"])
    paired_resume_budgets("postgres", h.urls["app"], h.user_id, key, case=case)
