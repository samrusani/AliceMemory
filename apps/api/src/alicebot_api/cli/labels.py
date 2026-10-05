"""Owner commands that check and repair stored derived labels on Postgres."""

from __future__ import annotations

from alicebot_api.cli.shared import CLIContext, _vnext_store_context
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError
from alicebot_api.vnext_label_repair import classify_stored_labels, format_label_check, plan_label_repairs
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock


def _postgres_tables(conn) -> dict[str, list[dict[str, object]]]:
    from alicebot_api.vnext_label_repair import INPUT_SELECTS_V3

    tables: dict[str, list[dict[str, object]]] = {}
    for table, statement in INPUT_SELECTS_V3.items():
        cursor = conn.execute(statement)
        names = [column[0] for column in cursor.description]
        tables[table] = [row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]
    return tables


def _run_vnext_labels_check(ctx: CLIContext, args: object) -> str:
    del args
    try:
        with _vnext_store_context(ctx) as store:
            conn = store.conn
            if not conn.in_transaction:
                conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            below, unverified = classify_stored_labels(_postgres_tables(conn))
    except DerivedDomainRepairError as exc:
        print(f"labels check failed: {exc}")
        raise SystemExit(1) from exc
    text = format_label_check(below, unverified)
    print(text)
    if below or unverified:
        raise SystemExit(1)
    return text


def _run_vnext_labels_repair(ctx: CLIContext, args: object) -> str:
    del args
    try:
        with _vnext_store_context(ctx) as store:
            if hasattr(store, "lock_graph_mutation"):
                store.lock_graph_mutation()
            acquire_exclusive_label_lock(store)
            changes = plan_label_repairs(_postgres_tables(store.conn))
            # The SQLite writer is not used here. Postgres repair applies the
            # same plan through label-only updates inside this transaction.
            applied = 0
            for table, user, row_id, previous, new, node in changes:
                import json

                from alicebot_api.vnext_derived_labels import labels_raised_payload
                from alicebot_api.vnext_event_log import build_event_log_record

                metadata = dict(node.get("metadata_json") or {})
                metadata["project_scope"] = list(new["project_scope"])
                metadata["project_floor"] = list(new["project_floor"])
                cursor = store.conn.execute(
                    f"UPDATE {table} SET domain = %s, sensitivity = %s, metadata_json = %s::jsonb "
                    "WHERE user_id = %s::uuid AND id = %s::uuid "
                    "AND domain = %s AND sensitivity = %s",
                    (
                        new["domain"],
                        new["sensitivity"],
                        json.dumps(metadata),
                        user,
                        row_id,
                        previous["domain"],
                        previous["sensitivity"],
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                target = {
                    "memories": "memory",
                    "open_loops": "open_loop",
                    "generated_artifacts": "artifact",
                    "projects": "project",
                }[table]
                event = build_event_log_record(
                    event_type=f"{target}.labels_raised",
                    actor_type="system",
                    target_type=target,
                    target_id=row_id,
                    payload=labels_raised_payload(cause="repair_v3", previous=previous, new=new),
                )
                store.conn.execute(
                    """
                    INSERT INTO event_log (
                        id, user_id, event_type, actor_type, target_type, target_id,
                        occurred_at, payload_json, integrity_hash
                    ) VALUES (
                        %s::uuid, %s::uuid, %s, %s, %s, %s, %s::timestamptz, %s::jsonb, %s
                    )
                    """,
                    (
                        event["id"],
                        user,
                        event["event_type"],
                        event["actor_type"],
                        event["target_type"],
                        row_id,
                        event["occurred_at"],
                        json.dumps(event["payload_json"]),
                        event["integrity_hash"],
                    ),
                )
                applied += 1
    except DerivedDomainRepairError as exc:
        print(f"labels repair failed: {exc}")
        raise SystemExit(2) from exc
    except Exception as exc:
        message = str(exc).lower()
        if "lock" in message and "timeout" in message:
            print("labels repair waited for the label lock and changed nothing")
            raise SystemExit(3) from exc
        raise
    return f"labels repair updated {applied}"


__all__ = ["_run_vnext_labels_check", "_run_vnext_labels_repair"]
