"""Native reads meet the unchanged budget on the exact random grid."""
import psycopg
import pytest

from tests.integration.derived_labels_postgres_support import label_harness
from tests.performance.test_label_round3_read_budget import CASES, SOURCE_COUNTS, seed_grid, repair_fixture, paired_budgets


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("source_count", SOURCE_COUNTS)
@pytest.mark.parametrize("repaired", (False, True))
def test_postgres_round3_random_read_budgets(label_harness, case, source_count, repaired):
    h = label_harness
    with h.store() as store:
        keys = seed_grid(store, postgres=True, case=case, source_count=source_count)
    if repaired:
        repair_fixture("postgres", h.urls["app"], h.user_id)
    with psycopg.connect(h.urls["admin"], autocommit=True) as conn:
        for table in ("sources", "memories", "event_log", "generated_artifacts", "open_loops"):
            conn.execute("ANALYZE " + table)
    paired_budgets("postgres", h.urls["app"], h.user_id, keys, case=case, source_count=source_count, repaired=repaired)


def test_postgres_round3_identical_copy_control(label_harness):
    h = label_harness
    with h.store() as store:
        keys = seed_grid(store, postgres=True, source_count=1, identical=True)
    paired_budgets("postgres", h.urls["app"], h.user_id, keys, case="identical-copies", source_count=1, repaired=False)
