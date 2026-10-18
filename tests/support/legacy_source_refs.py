"""Plant object ``source_refs`` the way a v0.20.2 vault stored them.

The commit doors on main refuse an object entry. A read-protection test still
needs the stored shape, so it commits the string ids the link writer would
have linked, then this module writes the original entries into the row, its
revisions and its events. That is the store path, not a commit door.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb

from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_source_fence import source_uuids_in_ref


def needs_legacy_plant(refs: Sequence[object]) -> bool:
    return any(not isinstance(ref, str) for ref in refs)


def link_strings(refs: Sequence[object]) -> list[str]:
    """Strings the current commit door can store, and the first source id of each other entry.

    The link writer links the first source id of a ref. Passing that id as a string keeps the
    same link, and the plant step puts the original entry back.
    """

    strings: list[str] = []
    for ref in refs:
        if isinstance(ref, str):
            if ref.strip():
                strings.append(ref)
            continue
        named = source_uuids_in_ref(ref)
        if named:
            strings.append(named[0])
    return strings


def _replace_standin(node: object, standin: list[str], original: list[object]) -> object:
    if isinstance(node, dict):
        replaced: dict[str, object] = {}
        for key, value in node.items():
            if key == "source_refs" and value == standin:
                replaced[key] = original
            else:
                replaced[key] = _replace_standin(value, standin, original)
        return replaced
    if isinstance(node, list):
        return [_replace_standin(item, standin, original) for item in node]
    return node


def _load(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value) if value else None
    return value


def _dump(value: object) -> str:
    return json.dumps(value, default=str)


def plant_legacy_source_refs(store: Any, memory_id: str, refs: Sequence[object], standin: Sequence[str]) -> None:
    """Overwrite the commit's string stand-in with ``refs`` on this connection."""

    original = list(refs)
    standin_list = list(standin)
    conn = store.conn
    if isinstance(conn, sqlite3.Connection):
        _plant_sqlite(conn, memory_id, original, standin_list)
    else:
        _plant_postgres(conn, memory_id, original, standin_list)


def plant_legacy_source_metadata(store: Any, source_id: str, refs: Sequence[object], standin: Sequence[str]) -> None:
    """Overwrite the string stand-in in a source row an ingest stored."""

    original = list(refs)
    standin_list = list(standin)
    conn = store.conn
    if isinstance(conn, sqlite3.Connection):
        row = conn.execute("SELECT metadata_json FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise AssertionError(f"source {source_id} was not stored")
        metadata = _replace_standin(_load(_field(row, "metadata_json", 0)), standin_list, original)
        conn.execute("UPDATE sources SET metadata_json = ? WHERE id = ?", (_dump(metadata), source_id))
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT metadata_json FROM sources WHERE id = %s", (source_id,))
            row = cur.fetchone()
            if row is None:
                raise AssertionError(f"source {source_id} was not stored")
            metadata = _replace_standin(_load(row["metadata_json"]), standin_list, original)
            cur.execute(
                "UPDATE sources SET metadata_json = %s WHERE id = %s",
                (_jsonb(metadata), source_id),
            )


def _field(row: object, name: str, index: int) -> object:
    if isinstance(row, sqlite3.Row):
        return row[name]
    if isinstance(row, dict):
        return row[name]
    return row[index]  # type: ignore[index]


def _plant_sqlite(conn: sqlite3.Connection, memory_id: str, original: list[object], standin: list[str]) -> None:
    row = conn.execute("SELECT metadata_json, value FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if row is None:
        raise AssertionError(f"memory {memory_id} was not stored")
    metadata = _replace_standin(_load(_field(row, "metadata_json", 0)), standin, original)
    value = _replace_standin(_load(_field(row, "value", 1)), standin, original)
    conn.execute(
        "UPDATE memories SET metadata_json = ?, value = ? WHERE id = ?",
        (_dump(metadata), _dump(value), memory_id),
    )
    # Revisions and events are append-only. A commit writes the string stand-in there. The quote the
    # reader judges lives on the memory row, which is what a restored backup's current row holds.


def _jsonb(value: object) -> Jsonb:
    return Jsonb(json_safe(value))


def _plant_postgres(conn: Any, memory_id: str, original: list[object], standin: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT metadata_json, value FROM memories WHERE id = %s", (memory_id,))
        row = cur.fetchone()
        if row is None:
            raise AssertionError(f"memory {memory_id} was not stored")
        cur.execute(
            "UPDATE memories SET metadata_json = %s, value = %s WHERE id = %s",
            (
                _jsonb(_replace_standin(_load(row["metadata_json"]), standin, original)),
                _jsonb(_replace_standin(_load(row["value"]), standin, original)),
                memory_id,
            ),
        )
