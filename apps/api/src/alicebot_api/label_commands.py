"""Owner commands that check and repair stored derived labels on SQLite."""

from __future__ import annotations

import json
import sqlite3

from alicebot_api.sqlite_store import sqlite_user_connection
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError
from alicebot_api.vnext_label_repair import (
    REPAIR_STATE_KEY,
    classify_stored_labels,
    format_label_check,
    relabel_labels_sqlite,
)


def run_labels(args) -> int:
    from alicebot_api.onramp import _prepared_export_connection, resolve_db_path

    db = resolve_db_path(data_dir=args.data_dir, db=args.db)
    if not db.is_file():
        print(json.dumps({"error": {"code": "vault_not_found", "message": "No vault exists at the selected location"}}))
        return 1
    if args.labels_command == "check":
        try:
            with _prepared_export_connection(db, args.user_id) as conn:
                from alicebot_api.vnext_label_repair import _load_tables

                below, unverified = classify_stored_labels(_load_tables(conn))
                stamped = conn.execute(
                    "SELECT value FROM alice_schema_state WHERE key = ?",
                    (REPAIR_STATE_KEY,),
                ).fetchone()
                snapshot_events = {
                    str(row[0] if not isinstance(row, dict) else row["id"])
                    for row in conn.execute(
                        "SELECT id FROM event_log WHERE event_type IN "
                        "('memory.labels_raised', 'open_loop.labels_raised')"
                    )
                }
        except DerivedDomainRepairError as exc:
            print(f"labels check failed: {exc}")
            return 1
        print(format_label_check(below, unverified))
        if stamped is None:
            print("derived_labels_v3 is not stamped on this snapshot")
        live_events: set[str] = set()
        try:
            live = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                live_events = {
                    str(row[0])
                    for row in live.execute(
                        "SELECT id FROM event_log WHERE event_type IN "
                        "('memory.labels_raised', 'open_loop.labels_raised')"
                    )
                }
            finally:
                live.close()
        except sqlite3.Error:
            live_events = set()
        raised_on_next_open = sorted(snapshot_events - live_events)
        if raised_on_next_open:
            print(f"next open would raise {len(raised_on_next_open)}")
            for event_id in raised_on_next_open[:20]:
                print(f"  {event_id}")
        return 1 if below or unverified or raised_on_next_open else 0
    try:
        with sqlite_user_connection(db, args.user_id) as conn:
            changed = relabel_labels_sqlite(conn, explicit=True)
    except DerivedDomainRepairError as exc:
        print(f"labels repair failed: {exc}")
        return 2
    print(f"labels repair updated {changed}")
    return 0
