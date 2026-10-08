"""Varied and plain native reads use the same database at both revisions."""
import pytest
import psycopg

from tests.integration.derived_labels_postgres_support import label_harness
from tests.performance.test_label_round2_read_budget import seed_varied, assert_budgets


@pytest.mark.parametrize("case", ["varied", "repaired", "plain", "many-hidden", "many-hidden-repaired", "one-hidden", "identical"])
def test_postgres_round2_read_budgets(label_harness, monkeypatch, case):
    monkeypatch.setenv("ALICE_READ_BUDGET_CASE", case)
    h = label_harness
    with h.store() as store:
        keys = seed_varied(store, postgres=True, repaired="repaired" in case, plain=case == "plain",
                           source_count=1000 if case == "plain" else 3000 if case.startswith("many-hidden") else 1 if case in {"one-hidden", "identical"} else 300,
                           mixed=case in {"varied", "repaired"}, all_hidden=case in {"many-hidden", "many-hidden-repaired", "one-hidden", "identical"},
                           unique_metadata=case != "identical")
    with psycopg.connect(h.urls["admin"], autocommit=True) as conn:
        for table in ("sources", "memories", "event_log"):
            conn.execute("ANALYZE " + table)
    assert_budgets("postgres", h.urls["app"], h.user_id, keys)
