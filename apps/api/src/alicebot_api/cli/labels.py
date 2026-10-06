"""Owner commands that check and repair stored derived labels on Postgres."""

from __future__ import annotations

from psycopg import sql

from alicebot_api.cli.shared import CLIContext, _vnext_store_context
from alicebot_api.db import user_read_snapshot_connection
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError, require_changed
from alicebot_api.vnext_label_repair import (
    classify_stored_labels,
    format_label_check,
    plan_label_repairs,
    load_postgres_label_tables,
    label_project_id,
)
from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock, lock_settled_label_rows


_postgres_tables = load_postgres_label_tables


def _run_vnext_labels_check(ctx: CLIContext, args: object) -> str:
    del args
    try:
        with user_read_snapshot_connection(ctx.database_url, ctx.user_id) as conn:
            below, unverified = classify_stored_labels(_postgres_tables(conn))
    except DerivedDomainRepairError as exc:
        print(f"labels check failed: {exc}")
        raise SystemExit(1) from exc
    text = format_label_check(below, unverified)
    if below or unverified:
        print(text)
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
            lock_settled_label_rows(
                store,
                [
                    {
                        "kind": {
                            "generated_artifacts": "artifact",
                            "projects": "project",
                            "open_loops": "open_loop",
                            "memories": "memory",
                        }[table],
                        "id": row_id,
                    }
                    for table, _user, row_id, *_ in changes
                ],
            )
            # The SQLite writer is not used here. Postgres repair applies the
            # same plan through label-only updates inside this transaction.
            applied = 0
            for table, user, row_id, previous, new, node in changes:
                import json

                from alicebot_api.vnext_derived_labels import labels_raised_payload
                from alicebot_api.vnext_event_log import build_event_log_record

                metadata = {"project_scope": list(new["project_scope"]), "project_floor": list(new["project_floor"])}
                project_sql = ", project_id = %s" if table in {"memories", "open_loops"} else ""
                project_guard = " AND project_id IS NOT DISTINCT FROM %s" if project_sql else ""
                params: list[object] = [new["domain"], new["sensitivity"], json.dumps(metadata)]
                if project_sql:
                    params.append(label_project_id(new["project_scope"]))
                params.extend(
                    [
                        user,
                        row_id,
                        previous["domain"],
                        previous["sensitivity"],
                        json.dumps(node.get("metadata_json") or {}),
                    ]
                )
                if project_sql:
                    params.append(node.get("project_id"))
                cursor = store.conn.execute(
                    sql.SQL(
                        "UPDATE {} SET domain = %s, sensitivity = %s, metadata_json = metadata_json || %s::jsonb "
                        "{} WHERE user_id = %s::uuid AND id = %s::uuid "
                        "AND domain = %s AND sensitivity = %s AND metadata_json = %s::jsonb{}"
                    ).format(sql.Identifier(table), sql.SQL(project_sql), sql.SQL(project_guard)),
                    tuple(params),
                )
                require_changed(cursor.rowcount, table, row_id)
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
        from alicebot_api.vnext_label_writes import label_error_response

        answer = label_error_response(exc)
        if answer is not None and answer[0] == 503:
            print(f"{answer[1]}; HTTP 503; Retry-After: 2")
            raise SystemExit(3) from exc
        raise
    return f"labels repair updated {applied}"


__all__ = ["_run_vnext_labels_check", "_run_vnext_labels_repair"]
