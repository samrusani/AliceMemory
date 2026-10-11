"""Resume and the grounding probe stay within a budget of main when many newer rows are hidden from the caller, on PostgreSQL.

Unreleased (on main, not in v0.20.0). The measurement of ``tests/performance/test_resume_hidden_rows_budget.py`` on a real
PostgreSQL store: 10,000 hidden rows newer than twelve readable decisions, each with an event, in two shapes, against the
paired main checkout. The tables are analyzed before any sample, as a vault that has been used would have been: without
statistics the planner joins ``event_log`` to ``memories`` with a nested loop that takes seconds on main as well.
"""

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.test_label_round3_read_budget_postgres import analyze_budget_tables
from tests.performance.test_resume_hidden_rows_budget import SHAPES, paired_resume_budgets, seed_hidden_rows


@pytest.mark.parametrize("shape", SHAPES)
def test_postgres_resume_stays_within_the_budget_of_main_with_many_hidden_rows(label_harness, shape):  # noqa: F811
    h = label_harness
    with h.store() as store:
        seed_hidden_rows(store, postgres=True, shape=shape)
    key = h.key("read_only_agent")
    analyze_budget_tables(h.urls["admin"])
    paired_resume_budgets("postgres", h.urls["app"], h.user_id, key, shape=shape)
