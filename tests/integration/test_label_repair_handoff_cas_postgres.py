"""A writer from an older binary cannot be overwritten by a repair plan."""
from types import SimpleNamespace

import pytest

from alicebot_api.cli import labels
from alicebot_api.db import user_connection
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_derived_labels_migration_postgres import seed_stale


def test_repair_compare_and_set_rejects_a_raw_concurrent_raise(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    user, rows = seed_stale(urls)
    # Locate the first planned memory rather than depending on the fixture's return shape.
    original = labels.plan_label_repairs
    changed_id = []

    def race(tables):
        plan = original(tables)
        target = next(item for item in plan if item[0] == "memories")
        changed_id.append(target[2])
        with user_connection(urls["app"], user) as conn:
            conn.execute("UPDATE memories SET sensitivity='regulated' WHERE id=%s::uuid", (target[2],))
        return plan

    monkeypatch.setattr(labels, "plan_label_repairs", race)
    with pytest.raises(SystemExit) as refused:
        labels._run_vnext_labels_repair(SimpleNamespace(database_url=urls["app"], user_id=user), None)
    assert refused.value.code == 2
    with user_connection(urls["app"], user) as conn:
        assert PostgresVNextStore(conn).get_memory(changed_id[0])["sensitivity"] == "regulated"
        assert conn.execute("SELECT count(*) AS n FROM event_log WHERE event_type LIKE '%%.labels_raised'").fetchone()["n"] == 0
