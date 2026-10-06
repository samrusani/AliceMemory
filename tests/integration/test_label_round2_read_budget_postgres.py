"""Varied and plain native reads use the same database at both revisions."""
import pytest
import psycopg

from tests.integration.derived_labels_postgres_support import label_harness
from tests.performance.test_label_round2_read_budget import seed_varied, assert_budgets


@pytest.mark.parametrize("case", ["varied", "repaired", "plain"])
def test_postgres_round2_read_budgets(label_harness, case):
    h = label_harness
    with h.store() as store:
        keys = seed_varied(store, postgres=True, repaired=case == "repaired", plain=case == "plain",
                           source_count=1000 if case == "plain" else 300)
    with psycopg.connect(h.urls["admin"], autocommit=True) as conn:
        for table in ("sources", "memories", "event_log"):
            conn.execute("ANALYZE " + table)
    assert_budgets("postgres", h.urls["app"], h.user_id, keys)
