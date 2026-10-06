"""An empty cached answer is not permission to scan every source again."""
import json
from uuid import UUID

import pytest

from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_stores.sqlite import source_retirement
from tests.unit.test_source_scrub_loop_references import _command, _create_loop
from tests.unit.test_source_supersede import run_import
from tests.unit.test_importer_per_file_savepoint import USER_ID, _folder, _read, _vault


@pytest.mark.parametrize("loop_count", [0, 1])
@pytest.mark.parametrize("operation", ["prune", "supersede", "dry_run"])
def test_many_sources_empty_answers_use_one_loop_pass(tmp_path, monkeypatch, capsys, loop_count, operation):
    db = _vault(tmp_path)
    folder = _folder(tmp_path, **{f"note{i}": f"The older synthetic statement {i}." for i in range(12)})
    ids = run_import(db, folder).source_ids
    for i in range(12):
        (folder / f"note{i}.md").write_text(f"The newer synthetic statement {i}.")
    if operation == "prune":
        run_import(db, folder, supersede=True)
    with sqlite_user_connection(db, USER_ID) as conn:
        if loop_count:
            _create_loop(SQLiteVNextStore(conn, USER_ID), "only-loop", metadata={"source_id": ids[0]})
    calls = []
    original = source_retirement._user_open_loops

    def count(store):
        calls.append(store.user_id)
        return original(store)

    monkeypatch.setattr(source_retirement, "_user_open_loops", count)
    if operation == "prune":
        assert _command(db, "prune", "--superseded") == 2
        preview = json.loads(capsys.readouterr().out)["would_delete"]
        assert len(calls) == 1
        assert sum(row["open_loops"] for row in preview) == loop_count
        calls.clear()
        assert _command(db, "prune", "--superseded", "--yes") == 0
        receipt = json.loads(capsys.readouterr().out)["deleted"]
        assert len(calls) == 1
        assert sum(row["open_loops"] for row in receipt) == loop_count
    else:
        result = run_import(db, folder, supersede=True, dry_run=operation == "dry_run")
        assert len(result.superseded) == 12
        assert len(calls) == 1


def test_reverse_lookup_limit_order_and_canonical_source_id(tmp_path):
    db = _vault(tmp_path)
    sid = run_import(db, _folder(tmp_path, note="Synthetic source.")).source_ids[0]
    with sqlite_user_connection(db, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        old = _create_loop(store, "old", metadata={"source_id": sid})
        new = _create_loop(store, "new", metadata={"source_id": sid})
        conn.execute("UPDATE open_loops SET opened_at='2020-01-01T00:00:00Z', created_at='2020-01-01T00:00:00Z' WHERE id=?", (old,))
        conn.execute("UPDATE open_loops SET opened_at='2021-01-01T00:00:00Z', created_at='2021-01-01T00:00:00Z' WHERE id=?", (new,))
        assert [row["id"] for row in store.list_open_loops_referencing_source(source_id=UUID(sid).hex.upper(), limit=1)] == [new]
        with pytest.raises(ValueError):
            store.list_open_loops_referencing_source(source_id=sid, limit=0)


def test_plain_import_does_not_scan_loops(tmp_path, monkeypatch):
    db = _vault(tmp_path)
    def refuse(_store):
        raise AssertionError("plain import scanned open loops")
    monkeypatch.setattr(source_retirement, "_user_open_loops", refuse)
    assert run_import(db, _folder(tmp_path, note="Synthetic source.")).imported_count == 1
