"""The budget fixtures keep the shapes that made a real vault slow.

Mutations: give the open loops one metadata key again, drop a field of the unique
metadata, leave a fixture without public originals, let the insert floor raise the
extra rows (so an unrepaired fixture has no gap), or stop deriving the loops.
"""
from __future__ import annotations

import json
import re
from uuid import UUID

import pytest

from alicebot_api.onramp import bootstrap_database
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_labels import dependencies_of, is_derived

from tests.performance import test_label_round3_read_budget as budgets

USER = budgets.USER
NOTE = re.compile(r"^(?:[a-z]+ ){8}[a-z]+ #[0-9a-f]+$")


def seeded_sqlite(tmp_path, name, **options):
    options = {"count": 400, "source_count": 24, **options}
    path = tmp_path / (name + ".db")
    bootstrap_database(path, user_id=USER, user_email="budget@example.invalid")
    with sqlite_user_connection(path, USER) as conn:
        budgets.seed_grid(SQLiteVNextStore(conn, USER), provision_keys=False, **options)
        rows = {table: [dict(row) for row in conn.execute("SELECT * FROM " + table)]
                for table in ("memories", "open_loops", "sources")}
    for table in ("memories", "open_loops"):
        for row in rows[table]:
            row["metadata_json"] = json.loads(row["metadata_json"])
    return rows


def assert_unique_metadata(metadata):
    assert isinstance(metadata["observation_index"], int)
    assert NOTE.match(metadata["capture_note"]), metadata["capture_note"]
    assert len(metadata["tags"]) == 3 and all(isinstance(tag, str) for tag in metadata["tags"])
    assert set(metadata["nested"]) == {"uuid", "weight"}
    assert UUID(metadata["nested"]["uuid"]) and isinstance(metadata["nested"]["weight"], float)


@pytest.mark.parametrize("case", budgets.CASES)
def test_every_fixture_holds_visible_originals_the_probe_query_matches(tmp_path, case):
    rows = seeded_sqlite(tmp_path, case)
    originals = [row for row in rows["memories"] if not is_derived("memory", row)]
    assert len(originals) == budgets.VISIBLE_ORIGINALS >= budgets.READ_LIMIT
    for row in originals:
        assert (row["sensitivity"], row["domain"], row["status"]) == ("public", "project", "active")
        assert "synthetic budget observation" in row["canonical_text"]
    assert len(rows["memories"]) == 400 + budgets.VISIBLE_ORIGINALS


def test_the_identical_copy_control_also_holds_originals(tmp_path):
    rows = seeded_sqlite(tmp_path, "identical", count=100, source_count=1, identical=True)
    originals = [row for row in rows["memories"] if not is_derived("memory", row)]
    assert len(originals) == budgets.VISIBLE_ORIGINALS
    assert len(rows["memories"]) == 100 + budgets.VISIBLE_ORIGINALS


def test_extra_rows_loops_are_derived_public_unscoped_and_unique(tmp_path):
    rows = seeded_sqlite(tmp_path, "extra", case="extra-rows")
    loops = rows["open_loops"]
    assert len(loops) == 400
    for loop in loops:
        metadata = loop["metadata_json"]
        assert (loop["sensitivity"], loop["domain"], loop["status"]) == ("public", "project", "open")
        assert metadata["project_scope"] == [] and metadata["project_floor"] == []
        assert is_derived("open_loop", loop) and ("source", loop["source_id"]) in dependencies_of("open_loop", loop)
        assert_unique_metadata(metadata)
    assert len({loop["metadata_json"]["capture_note"] for loop in loops}) == 400
    assert {loop["source_id"] for loop in loops} <= {row["id"] for row in rows["sources"]}
    # Stored below their inputs: the insert floor is not applied, so an unrepaired fixture has a gap.
    hidden = {row["id"] for row in rows["sources"] if row["sensitivity"] == "confidential"}
    assert any(loop["source_id"] in hidden for loop in loops)


def test_extra_rows_memories_carry_the_unique_metadata_and_other_cases_do_not(tmp_path):
    extra = seeded_sqlite(tmp_path, "extra", case="extra-rows")
    derived = [row for row in extra["memories"] if is_derived("memory", row)]
    assert len(derived) == 400
    for row in derived:
        assert_unique_metadata(row["metadata_json"])
    assert len({row["metadata_json"]["capture_note"] for row in derived}) == 400
    plain = seeded_sqlite(tmp_path, "plain", case="half-hidden")
    assert not plain["open_loops"]
    assert all("capture_note" not in row["metadata_json"] for row in plain["memories"])
