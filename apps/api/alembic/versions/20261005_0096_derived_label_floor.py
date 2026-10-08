"""Raise stored derived labels to the labels of their inputs.

Revision ID: 20261005_0096
Revises: 20261004_0095
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

from alicebot_api.vnext_derived_domain_backfill import require_changed
from alicebot_api.vnext_derived_labels import labels_raised_payload
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_repair import INPUT_SELECTS_V3, plan_label_repairs, label_project_id

revision = "20261005_0096"
down_revision = "20261004_0095"
branch_labels = None
depends_on = None

_RELAX_RLS = (
    "ALTER TABLE sources NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE open_loops NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE generated_artifacts NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE beliefs NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE event_log NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE projects NO FORCE ROW LEVEL SECURITY",
)
_RESTORE_RLS = tuple(statement.replace(" NO FORCE ", " FORCE ") for statement in _RELAX_RLS)
_TARGET = {
    "memories": "memory",
    "open_loops": "open_loop",
    "generated_artifacts": "artifact",
    "projects": "project",
}


def upgrade() -> None:
    connection = op.get_bind()
    for statement in _RELAX_RLS:
        op.execute(statement)
    tables = {}
    for table, statement in INPUT_SELECTS_V3.items():
        tables[table] = list(connection.execute(text(statement)).mappings())
    for table, user, row_id, previous, new, node in plan_label_repairs(tables):
        metadata = {"project_scope": list(new["project_scope"]), "project_floor": list(new["project_floor"])}
        project_sql = ", project_id = :project_id" if table in {"memories", "open_loops"} else ""
        project_guard = " AND project_id IS NOT DISTINCT FROM :previous_project_id" if project_sql else ""
        require_changed(
            connection.execute(
                text(
                    f"UPDATE {table} SET domain = :domain, sensitivity = :sensitivity, "
                    f"metadata_json = metadata_json || CAST(:metadata AS jsonb){project_sql} "
                    "WHERE user_id = CAST(:user AS uuid) AND id = CAST(:id AS uuid) "
                    "AND domain = :previous_domain AND sensitivity = :previous_sensitivity "
                    f"AND metadata_json = CAST(:previous_metadata AS jsonb){project_guard}"
                ),
                {
                    "domain": new["domain"],
                    "sensitivity": new["sensitivity"],
                    "metadata": json.dumps(metadata),
                    "user": user,
                    "id": row_id,
                    "previous_domain": previous["domain"],
                    "previous_sensitivity": previous["sensitivity"],
                    "previous_metadata": json.dumps(node.get("metadata_json") or {}),
                    "project_id": label_project_id(new["project_scope"]),
                    "previous_project_id": node.get("project_id"),
                },
            ).rowcount,
            table,
            row_id,
        )
        target = _TARGET[table]
        event = build_event_log_record(
            event_type=f"{target}.labels_raised",
            actor_type="system",
            target_type=target,
            target_id=row_id,
            payload=labels_raised_payload(cause="repair_v3", previous=previous, new=new),
        )
        event["user_id"] = user
        connection.execute(
            text(
                """
                INSERT INTO event_log (
                    id, user_id, event_type, actor_type, target_type, target_id,
                    occurred_at, payload_json, integrity_hash
                ) VALUES (
                    CAST(:id AS uuid), CAST(:user_id AS uuid), :event_type, :actor_type,
                    :target_type, :target_id, CAST(:occurred_at AS timestamptz),
                    CAST(:payload_json AS jsonb), :integrity_hash
                )
                """
            ),
            {**event, "payload_json": json.dumps(event["payload_json"])},
        )
    for statement in _RESTORE_RLS:
        op.execute(statement)


def downgrade() -> None:
    # Raised labels stay. An older binary can read the repaired rows.
    pass
