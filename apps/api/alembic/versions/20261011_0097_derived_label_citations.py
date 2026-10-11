"""Raise stored derived labels again, reading every citation shape.

Revision ID: 20261011_0097
Revises: 20261005_0096

Revision 20261005_0096 raised a derived row to the labels of the rows it records as inputs. It read a citation only when it
was a plain ``memory:<id>`` or ``source:<id>`` string or sat under a few id keys. A report written before derived labels
existed keeps the references of its members as their writers sent them: JSON text such as ``{"memory_id": "<id>", "quote":
"..."}``, ``alice://memories/<id>``, ``MEMORY:<id>``, an id in braces, without hyphens, in capitals. The closure now reads a
record with the parser of the saved-quote reader (``vnext_citations``), so the same planner finds the inputs it missed, and
this revision runs it once more over every table for a database that already ran 0096 with the narrower reading. A database
that goes from 0094 straight to this revision has already been repaired by 0096, because 0096 calls the same planner, and
this pass finds nothing to change.

The change is idempotent: the plan holds only rows whose stored label is below their inputs. A row whose record cannot be
read (a saved reference that is JSON text which does not decode, with an id in it) is unverified and is not rewritten; the
owner and an unbound admin key read it, no other key does, until it is regenerated or deleted.
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

from alicebot_api.vnext_derived_domain_backfill import require_changed
from alicebot_api.vnext_derived_labels import labels_raised_payload
from alicebot_api.vnext_event_log import build_event_log_record
from alicebot_api.vnext_label_repair import (
    INPUT_SELECTS_V3,
    REPAIR_CAUSE,
    label_project_id,
    plan_label_repairs,
)

revision = "20261011_0097"
down_revision = "20261005_0096"
branch_labels = None
depends_on = None

# The same bracket as 0096: the documented table owner is NOSUPERUSER NOBYPASSRLS, so FORCE ROW LEVEL SECURITY is relaxed
# for this one transaction and restored before it ends. Failure rolls back the bracket, the labels and their audit rows.
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
            payload=labels_raised_payload(cause=REPAIR_CAUSE, previous=previous, new=new),
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
