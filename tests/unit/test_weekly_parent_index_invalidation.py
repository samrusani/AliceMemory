"""Whatever empties a request's label state empties its weekly-parent index too.

A request loads the reports that name each weekly candidate once and judges every candidate from that
index. A write, or a rollback of a savepoint, changes the reports, so the next read in the same scope
must load them again. These tests use a store that offers the reports (``read_artifacts_naming_candidates``)
because SQLite has no generated-artifact table; ``tests/integration/test_weekly_parent_index_invalidation_postgres.py``
does the same against the production store.

A lookup through a store other than the one the scope belongs to is that store's own: it neither reads nor fills the
scope's index, and it does not count as one of the scope's queries.

Mutations: leave the index out of ``_RequestLabels.clear``; empty the index on every lookup; leave out the drop
that ``takes_label_lock`` makes after a write; read the scope's index for a lookup through another store; store another
store's index in the scope; count another store's lookup as one of the scope's; empty the scope's index when another
store drops its own.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from uuid import uuid4

import pytest

from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_derived_labels import identifier
from alicebot_api.vnext_label_closure import (
    bind_weekly_parent_cache,
    drop_weekly_parent_index,
    load_weekly_parent_artifacts,
    weekly_parent_query_count,
)
from alicebot_api.vnext_embeddings import _embedding_write_savepoint
from alicebot_api.vnext_label_guard import LabelGuard, invalidate_read_labels, label_read_scope
from alicebot_api.vnext_label_writes import takes_label_lock

CEILING = ("public", "internal", "private", "unknown")
USER = "user-1"


def _row(kind: str, row_id: str, **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": kind,
        "id": row_id,
        "user_id": USER,
        "domain": "project",
        "sensitivity": "public",
        "metadata_json": {},
    }
    row.update(fields)
    return row


def _weekly(row_id: str) -> dict[str, object]:
    return _row(
        "memory",
        row_id,
        metadata_json={"candidate": True, "discovered_by": "vnext_weekly_synthesis", "project_scope": []},
    )


def _report(memory_id: str, *source_ids: str) -> dict[str, object]:
    return _row(
        "artifact",
        "report-" + memory_id,
        artifact_type="weekly_synthesis",
        metadata_json={
            "workflow": "weekly_synthesis",
            "candidate_memory_ids": [memory_id],
            "input_summary": {"source_ids": list(source_ids), "counts": {"sources": len(source_ids)}},
        },
    )


class Vault:
    """Rows in memory, with the reads the guard uses and a count of the report reads."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.parent_reads = 0

    def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
        wanted = {identifier(item) for item in ids}
        return [dict(row) for row in self.rows if row["kind"] == kind and identifier(row["id"]) in wanted]

    def read_artifacts_naming_candidates(self, memory_ids: list[str] | None) -> list[dict[str, object]]:
        del memory_ids
        self.parent_reads += 1
        return [
            dict(row)
            for row in self.rows
            if row["kind"] == "artifact" and "candidate_memory_ids" in dict(row["metadata_json"])  # type: ignore[arg-type]
        ]

    @takes_label_lock
    def write_report_inputs(self, memory_id: str, *source_ids: str, read_while_writing: bool = False) -> None:
        """A store write. ``read_while_writing`` stands for the insert floor, which reads the reports first."""

        if read_while_writing:
            load_weekly_parent_artifacts(self, memory_id)
        for row in self.rows:
            if row["id"] == "report-" + memory_id:
                row["metadata_json"] = _report(memory_id, *source_ids)["metadata_json"]  # type: ignore[index]

    @takes_label_lock
    def write_that_fails(self, memory_id: str) -> None:
        load_weekly_parent_artifacts(self, memory_id)
        raise RuntimeError("the write was refused")


def _vault(memory_id: str = "weekly") -> Vault:
    return Vault(
        [
            _row("source", "source-public"),
            _row("source", "source-secret", sensitivity="confidential"),
            _weekly(memory_id),
            _report(memory_id, "source-public"),
        ]
    )


def _judged(vault: Vault, memory_id: str = "weekly") -> tuple[str, bool]:
    row = next(item for item in vault.rows if item["id"] == memory_id)
    effective = LabelGuard.for_filters(vault, (), CEILING, ()).effective_row("memory", row)
    return str(effective["sensitivity"]), bool(effective["unverified"])


def test_invalidating_the_read_labels_drops_the_index() -> None:
    vault = _vault()
    with label_read_scope(vault):
        assert _judged(vault) == ("public", False)
        assert vault.parent_reads == 1
        vault.write_report_inputs("weekly", "source-public", "source-secret")
        invalidate_read_labels(vault)
        assert _judged(vault) == ("confidential", False)
        assert vault.parent_reads == 2
        assert weekly_parent_query_count() == 2


def test_without_an_invalidation_the_scope_keeps_one_index() -> None:
    vault = _vault()
    with label_read_scope(vault):
        for _ in range(5):
            assert _judged(vault) == ("public", False)
        assert vault.parent_reads == 1
        assert weekly_parent_query_count() == 1


def test_a_request_with_many_weekly_memories_runs_one_parent_query_and_one_more_after_an_invalidation() -> None:
    memory_ids = [str(uuid4()) for _ in range(40)]
    rows: list[dict[str, object]] = [_row("source", "source-public")]
    for memory_id in memory_ids:
        rows.extend([_weekly(memory_id), _report(memory_id, "source-public")])
    vault = Vault(rows)
    with label_read_scope(vault):
        for memory_id in memory_ids:
            assert _judged(vault, memory_id) == ("public", False)
        assert weekly_parent_query_count() == 1
        for memory_id in memory_ids:
            _judged(vault, memory_id)
        assert weekly_parent_query_count() == 1
        invalidate_read_labels(vault)
        assert weekly_parent_query_count() == 1
        for memory_id in memory_ids:
            assert _judged(vault, memory_id) == ("public", False)
        assert weekly_parent_query_count() == 2
    assert vault.parent_reads == 2


def test_the_count_survives_an_invalidation() -> None:
    """Invalidation empties the index and keeps the count, so a test or a probe can tell it ran."""

    vault = _vault()
    with label_read_scope(vault):
        _judged(vault)
        invalidate_read_labels(vault)
        invalidate_read_labels(vault)
        assert weekly_parent_query_count() == 1
        _judged(vault)
        assert weekly_parent_query_count() == 2


def test_a_write_drops_the_index_before_it_runs() -> None:
    vault = _vault()
    with label_read_scope(vault):
        assert _judged(vault) == ("public", False)
        vault.write_report_inputs("weekly", "source-public", "source-secret")
        assert _judged(vault) == ("confidential", False)
        assert vault.parent_reads == 2


def test_a_write_that_read_the_index_while_it_ran_leaves_no_index_behind() -> None:
    """The insert floor reads the reports before the row exists. What it loaded is not what is stored after."""

    vault = _vault()
    with label_read_scope(vault):
        vault.write_report_inputs("weekly", "source-public", "source-secret", read_while_writing=True)
        assert vault.parent_reads == 1
        assert _judged(vault) == ("confidential", False)
        assert vault.parent_reads == 2
        assert weekly_parent_query_count() == 2


def test_a_write_that_fails_after_reading_the_index_leaves_no_index_behind() -> None:
    vault = _vault()
    with label_read_scope(vault):
        with pytest.raises(RuntimeError):
            vault.write_that_fails("weekly")
        assert vault.parent_reads == 1
        assert _judged(vault) == ("public", False)
        assert vault.parent_reads == 2


def test_dropping_the_index_of_another_store_leaves_this_scope_alone() -> None:
    vault, other = _vault(), _vault("other-weekly")
    with label_read_scope(vault):
        assert _judged(vault) == ("public", False)
        invalidate_read_labels(other)
        drop_weekly_parent_index(other)
        assert _judged(vault) == ("public", False)
        assert vault.parent_reads == 1
        assert other.parent_reads == 0


def test_a_nested_scope_for_a_second_store_keeps_its_own_index() -> None:
    first, second = _vault(), _vault("second-weekly")
    with label_read_scope(first):
        assert _judged(first) == ("public", False)
        with label_read_scope(second):
            assert _judged(second, "second-weekly") == ("public", False)
            assert weekly_parent_query_count() == 1
            invalidate_read_labels(second)
            assert _judged(second, "second-weekly") == ("public", False)
            assert weekly_parent_query_count() == 2
        assert weekly_parent_query_count() == 1
        assert _judged(first) == ("public", False)
        assert first.parent_reads == 1


def test_a_second_scope_on_the_same_store_shares_the_outer_index() -> None:
    vault = _vault()
    with label_read_scope(vault):
        _judged(vault)
        with label_read_scope(vault):
            _judged(vault)
            assert weekly_parent_query_count() == 1
            invalidate_read_labels(vault)
        _judged(vault)
        assert weekly_parent_query_count() == 2


def test_the_index_cannot_be_bound_without_the_state_that_invalidation_clears() -> None:
    parameters = inspect.signature(bind_weekly_parent_cache).parameters
    assert parameters["cache"].default is inspect.Parameter.empty


def test_a_lookup_outside_a_scope_reads_the_reports_every_time() -> None:
    vault = _vault()
    assert load_weekly_parent_artifacts(vault, "weekly")
    assert load_weekly_parent_artifacts(vault, "weekly")
    assert vault.parent_reads == 2
    assert weekly_parent_query_count() == 0


@contextmanager
def _the_stores_savepoint(store):
    with store.savepoint():
        yield


@contextmanager
def _the_embedding_writes_savepoint(store):
    with _embedding_write_savepoint(store):
        yield


@pytest.mark.parametrize(
    "block",
    [_the_stores_savepoint, _the_embedding_writes_savepoint],
    ids=["store-savepoint", "embedding-write"],
)
def test_a_rolled_back_sqlite_savepoint_drops_the_index(tmp_path, block) -> None:
    user_id = uuid4()
    with sqlite_user_connection(tmp_path / "vault.db", user_id, repair_labels=False) as conn:
        ensure_sqlite_user(conn, user_id, "owner@example.invalid", "Owner")
        store = SQLiteVNextStore(conn, user_id)
        reports: list[dict[str, object]] = []
        reads = []

        def offered(memory_ids):
            del memory_ids
            reads.append(len(reports))
            return [dict(item) for item in reports]

        store.read_artifacts_naming_candidates = offered  # type: ignore[attr-defined]
        with label_read_scope(store):
            assert load_weekly_parent_artifacts(store, "weekly") == []
            assert load_weekly_parent_artifacts(store, "weekly") == []
            assert weekly_parent_query_count() == 1
            with pytest.raises(RuntimeError):
                with block(store):
                    reports.append(_report("weekly", "source-public"))
                    invalidate_read_labels(store)  # what the write that stored the report does first
                    assert load_weekly_parent_artifacts(store, "weekly") == [reports[0]]
                    raise RuntimeError("roll back")
            reports.clear()
            assert load_weekly_parent_artifacts(store, "weekly") == []
            assert weekly_parent_query_count() == 3
            assert reads == [0, 1, 0]


def _other_vault() -> Vault:
    """A second store whose weekly memory has a parent of its own, that cites a confidential source."""

    return Vault(
        [
            _row("source", "source-public"),
            _row("source", "source-secret", sensitivity="confidential"),
            _weekly("other-weekly"),
            _report("other-weekly", "source-secret"),
        ]
    )


def _ids(reports: list[dict[str, object]]) -> list[object]:
    return [report["id"] for report in reports]


def test_a_lookup_through_another_store_does_not_read_the_index_of_the_scope() -> None:
    mine, other = _vault(), _other_vault()
    with label_read_scope(mine):
        assert _judged(mine) == ("public", False)
        assert weekly_parent_query_count() == 1
        assert _ids(load_weekly_parent_artifacts(other, "other-weekly")) == ["report-other-weekly"]
        assert other.parent_reads == 1
        assert _judged(other, "other-weekly") == ("confidential", False)
        assert mine.parent_reads == 1
        assert weekly_parent_query_count() == 1


def test_a_lookup_through_another_store_does_not_fill_the_index_of_the_scope() -> None:
    mine, other = _vault(), _other_vault()
    with label_read_scope(mine):
        assert _ids(load_weekly_parent_artifacts(other, "other-weekly")) == ["report-other-weekly"]
        assert other.parent_reads == 1
        assert mine.parent_reads == 0
        # Nothing of this scope's was loaded yet, so the next lookup through its own store is its first.
        assert _ids(load_weekly_parent_artifacts(mine, "weekly")) == ["report-weekly"]
        assert mine.parent_reads == 1
        assert _judged(mine) == ("public", False)
        assert weekly_parent_query_count() == 1


def test_a_lookup_through_another_store_is_not_counted_as_a_query_of_the_scope() -> None:
    mine, other = _vault(), _other_vault()
    with label_read_scope(mine):
        for _ in range(3):
            load_weekly_parent_artifacts(other, "other-weekly")
        assert other.parent_reads == 3
        assert weekly_parent_query_count() == 0
        load_weekly_parent_artifacts(mine, "weekly")
        assert weekly_parent_query_count() == 1


def test_dropping_the_index_of_another_store_keeps_the_index_of_the_scope() -> None:
    """Read the scope's index back with a lookup, since a judged row caches its own label and hides a dropped index."""

    mine, other = _vault(), _other_vault()
    with label_read_scope(mine):
        assert _ids(load_weekly_parent_artifacts(mine, "weekly")) == ["report-weekly"]
        assert mine.parent_reads == 1
        drop_weekly_parent_index(other)
        assert _ids(load_weekly_parent_artifacts(mine, "weekly")) == ["report-weekly"]
        assert mine.parent_reads == 1
        # The scope's own drop still empties it.
        drop_weekly_parent_index(mine)
        assert _ids(load_weekly_parent_artifacts(mine, "weekly")) == ["report-weekly"]
        assert mine.parent_reads == 2
