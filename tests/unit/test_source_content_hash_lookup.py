"""The content-hash dedupe lookup uses the content-hash index and keeps its order.

Guard: the unary plus on the ORDER BY terms of ``get_sources_by_content_hash``
takes ``sources_user_captured_idx`` out of the planner's options, so the lookup
searches ``sources_user_content_hash_idx`` instead of scanning the captured
index of a growing vault on every capture. Removing the unary plus (or dropping
the ORDER BY) lets the planner walk the captured index again and makes every
capture slower as the vault grows; the plan test below fails on that removal.
The order test pins ``captured_at DESC, id DESC`` across ties, so the rewrite
cannot silently change which row a caller sees first.
"""

from __future__ import annotations

import inspect
import sqlite3
from contextlib import closing
from pathlib import Path

from alicebot_api.onramp import bootstrap_database, resolve_db_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

USER_ID = "00000000-0000-0000-0000-000000000001"

_HASH = "sha256:tie-order-check"

_ROWS = (
    ("old", "2026-01-01T00:00:00.000Z"),
    ("mid", "2026-01-02T00:00:00.000Z"),
    ("tie_b", "2026-01-03T00:00:00.000Z"),
    ("tie_a", "2026-01-03T00:00:00.000Z"),
)


def _database(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path / "data"), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def test_plan_uses_the_content_hash_index(tmp_path: Path) -> None:
    """Guard: the unary-plus ORDER BY (its removal reverts to a captured-index scan)."""
    database = _database(tmp_path)
    source = inspect.getsource(SQLiteVNextStore.get_sources_by_content_hash)
    assert "ORDER BY +captured_at DESC, +id DESC" in source
    with closing(sqlite3.connect(database)) as reader:
        indexes = {
            row[0]
            for row in reader.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'sources'")
        }
        assert "sources_user_content_hash_idx" in indexes
        plan = " | ".join(
            row[3]
            for row in reader.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM sources "
                "WHERE content_hash = 'x' AND user_id = 'u' AND deleted_at IS NULL "
                "ORDER BY +captured_at DESC, +id DESC"
            )
        )
    assert "sources_user_content_hash_idx" in plan
    assert "sources_user_captured_idx" not in plan


def test_ties_keep_captured_at_desc_then_id_desc(tmp_path: Path) -> None:
    """Guard: the same rows in the same order as the un-rewritten query."""
    database = _database(tmp_path)
    ids: list[str] = []
    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for title, captured_at in _ROWS:
            row = store.create_source(
                {
                    "source_type": "manual_text",
                    "title": title,
                    "content_hash": _HASH,
                    "captured_at": captured_at,
                    "dedupe_key": f"tie:{title}",
                    "metadata_json": {},
                }
            )
            ids.append(str(row["id"]))
        rows = store.get_sources_by_content_hash(_HASH)
    # SQLite compares id as text, so the tie order is id DESC.
    id_to_title = dict(zip(ids, [title for title, _ in _ROWS]))
    first, second = (id_to_title[i] for i in sorted([ids[2], ids[3]], reverse=True))
    assert [str(row["title"]) for row in rows] == [first, second, "mid", "old"]
    assert all(str(row["content_hash"]) == _HASH for row in rows)
