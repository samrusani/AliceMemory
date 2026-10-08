"""Native reads meet the unchanged budget on the exact random grid."""
import psycopg
import pytest

from tests.integration.derived_labels_postgres_support import label_harness
from tests.performance.test_label_round3_read_budget import CASES, SOURCE_COUNTS, seed_grid, repair_fixture, paired_budgets

ANALYZED_TABLES = ("sources", "memories", "event_log", "generated_artifacts", "open_loops")


def analyze_budget_tables(admin_url):
    """Give the planner real statistics before any sample, as a vault that has been used would have.

    Without them the paired main checkout plans its pack read badly (five to seven seconds on the
    identical-copy fixture), and a gate measured against it cannot fail.
    """
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for table in ANALYZED_TABLES:
            conn.execute("ANALYZE " + table)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("source_count", SOURCE_COUNTS)
@pytest.mark.parametrize("repaired", (False, True))
def test_postgres_round3_random_read_budgets(label_harness, case, source_count, repaired):
    h = label_harness
    with h.store() as store:
        keys = seed_grid(store, postgres=True, case=case, source_count=source_count)
    if repaired:
        repair_fixture("postgres", h.urls["app"], h.user_id)
    analyze_budget_tables(h.urls["admin"])
    paired_budgets("postgres", h.urls["app"], h.user_id, keys, case=case, source_count=source_count, repaired=repaired)


def test_postgres_round3_identical_copy_control(label_harness):
    h = label_harness
    with h.store() as store:
        keys = seed_grid(store, postgres=True, source_count=1, identical=True)
    analyze_budget_tables(h.urls["admin"])
    paired_budgets("postgres", h.urls["app"], h.user_id, keys, case="identical-copies", source_count=1, repaired=False)
