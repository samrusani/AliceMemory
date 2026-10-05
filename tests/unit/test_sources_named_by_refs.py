"""The batched source read that report producers use to label a report over the sources it prints.

``sources_named_by_loops`` (the open-loop review) and ``sources_named_by_refs`` (the consolidation report) both go
through ``source_rows_by_ids``. These tests pin the read itself: one row for each source however many refs name it and
in whatever spelling, no store call when nothing is named, the bulk read when the store has it and the single read when
it has only that, and a bounded batch.
"""

from __future__ import annotations

from uuid import uuid4

from alicebot_api.vnext_open_loop_references import (
    source_rows_by_ids,
    sources_named_by_loops,
    sources_named_by_refs,
)


class BulkStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.reads: list[tuple[tuple[str, ...], bool]] = []

    def get_sources_by_ids(self, ids, *, include_deleted: bool = False):
        self.reads.append((tuple(ids), include_deleted))
        wanted = {str(value).lower() for value in ids}
        return [
            dict(row)
            for row in self.rows
            if str(row["id"]).lower() in wanted and (include_deleted or row.get("deleted_at") is None)
        ]


class SingleStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.reads: list[str] = []

    def get_source(self, source_id):
        self.reads.append(str(source_id))
        return next((dict(row) for row in self.rows if str(row["id"]).lower() == str(source_id).lower()), None)


def _row(**fields) -> dict:
    return {"id": str(uuid4()), "domain": "personal", "sensitivity": "internal", **fields}


def test_one_row_for_a_source_however_many_refs_name_it() -> None:
    row = _row(sensitivity="confidential")
    sid = row["id"]
    store = BulkStore([row, _row()])
    found = sources_named_by_refs(
        store, [f"source:{sid}", f"SOURCE:{sid.upper()}", sid, sid.replace("-", ""), f"alice://sources/{sid}"]
    )

    assert [item["id"] for item in found] == [sid]
    assert len(store.reads) == 1
    assert store.reads[0][0] == (sid,)


def test_an_archived_source_is_returned_with_its_deleted_marker() -> None:
    row = _row(deleted_at="2026-09-01T00:00:00Z")
    found = sources_named_by_refs(BulkStore([row]), [f"source:{row['id']}"])

    assert [item["id"] for item in found] == [row["id"]]
    assert found[0]["deleted_at"] == "2026-09-01T00:00:00Z"


def test_a_ref_that_names_a_memory_or_no_id_makes_no_store_call() -> None:
    store = BulkStore([_row()])

    assert sources_named_by_refs(store, [f"memory:{uuid4()}", "https://example.test/x", "plain words", 7, None]) == []
    assert sources_named_by_refs(store, []) == []
    assert sources_named_by_loops(store, [{"id": "loop-1"}, {"id": "loop-2", "source_id": None}]) == []
    assert source_rows_by_ids(store, []) == []
    assert store.reads == []


def test_an_id_that_names_no_row_adds_no_row() -> None:
    row = _row()
    found = sources_named_by_refs(BulkStore([row]), [f"source:{uuid4()}", f"source:{row['id']}"])

    assert [item["id"] for item in found] == [row["id"]]


def test_a_store_with_only_the_single_read_is_read_one_id_at_a_time() -> None:
    rows = [_row(), _row()]
    store = SingleStore(rows)
    found = sources_named_by_refs(store, [f"source:{row['id']}" for row in rows])

    assert sorted(item["id"] for item in found) == sorted(row["id"] for row in rows)
    assert sorted(store.reads) == sorted(row["id"] for row in rows)


def test_a_store_that_cannot_read_a_source_by_id_adds_no_row() -> None:
    assert sources_named_by_refs(object(), [f"source:{uuid4()}"]) == []


def test_the_loops_form_reads_the_source_id_column_of_each_loop_once() -> None:
    row = _row(sensitivity="private")
    store = BulkStore([row, _row()])
    found = sources_named_by_loops(
        store, [{"source_id": row["id"]}, {"source_id": row["id"].upper()}, {"source_id": None}, {}]
    )

    assert [item["id"] for item in found] == [row["id"]]
    assert len(store.reads) == 1


def test_a_long_list_is_read_in_bounded_batches() -> None:
    rows = [_row() for _ in range(501)]
    store = BulkStore(rows)
    found = source_rows_by_ids(store, [row["id"] for row in rows])

    assert len(found) == 501
    assert len(store.reads) == 2
    assert max(len(ids) for ids, _deleted in store.reads) <= 500
