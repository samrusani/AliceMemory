"""Relabel derived rows whose recorded inputs include a restricted domain.

Revision ID: 20261004_0095
Revises: 20260721_0094
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

from alicebot_api.vnext_derived_domain_backfill import INPUT_SELECTS, plan_relabels, relabel_event

revision = "20261004_0095"
down_revision = "20260721_0094"
branch_labels = None
depends_on = None

_RELAX_RLS = (
    "ALTER TABLE sources NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE open_loops NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE generated_artifacts NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE beliefs NO FORCE ROW LEVEL SECURITY",
    "ALTER TABLE event_log NO FORCE ROW LEVEL SECURITY",
)
_RESTORE_RLS = tuple(statement.replace(" NO FORCE ", " FORCE ") for statement in _RELAX_RLS)


def upgrade() -> None:
    connection = op.get_bind()
    # The documented table owner is NOSUPERUSER NOBYPASSRLS. As in 0093,
    # temporarily use its owner bypass inside Alembic's single transaction.
    # Concurrent sessions never see the relaxed FORCE setting; failure rolls
    # back the bracket, data changes and audit rows together.
    for statement in _RELAX_RLS:
        op.execute(statement)
    tables = {}
    for table, statement in INPUT_SELECTS.items():
        tables[table] = list(connection.execute(text(statement)).mappings())
    tables["beliefs"] = list(connection.execute(text("SELECT id, user_id, memory_id FROM beliefs")).mappings())
    previous = {
        (table, str(row["user_id"]), str(row["id"])): row.get("domain")
        for table, rows in tables.items()
        for row in rows
    }
    updates = {
        "memories": text(
            "UPDATE memories SET domain = :domain WHERE user_id = CAST(:user AS uuid) AND id = CAST(:id AS uuid)"
        ),
        "generated_artifacts": text(
            "UPDATE generated_artifacts SET domain = :domain WHERE user_id = CAST(:user AS uuid) AND id = CAST(:id AS uuid)"
        ),
    }
    for table, user, row_id, domain in plan_relabels(tables):
        connection.execute(updates[table], {"domain": domain, "user": user, "id": row_id})
        event = relabel_event(table, user, row_id, previous[table, user, row_id], domain)
        connection.execute(
            text("""
            INSERT INTO event_log (id, user_id, event_type, actor_type, target_type,
                target_id, occurred_at, payload_json, integrity_hash)
            VALUES (CAST(:id AS uuid), CAST(:user_id AS uuid), :event_type, :actor_type,
                :target_type, :target_id, CAST(:occurred_at AS timestamptz),
                CAST(:payload_json AS jsonb), :integrity_hash)
        """),
            {**event, "payload_json": json.dumps(event["payload_json"])},
        )
    for statement in _RESTORE_RLS:
        op.execute(statement)


def downgrade() -> None:
    # Reversing these labels would reopen the disclosure. Content/schema are
    # unchanged, so an older binary can read the repaired rows as they stand.
    pass
