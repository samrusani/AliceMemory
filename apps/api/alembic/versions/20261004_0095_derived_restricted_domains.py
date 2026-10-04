"""Relabel derived rows whose recorded inputs include a restricted domain.

Revision ID: 20261004_0095
Revises: 20260721_0094
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import text

from alicebot_api.vnext_derived_domain_backfill import INPUT_TABLES, plan_relabels

revision = '20261004_0095'
down_revision = '20260721_0094'
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    # The migration owner reads every user; each reference is still joined by
    # user_id as well as id by the planner. App-role RLS remains unchanged.
    tables = {}
    for table in INPUT_TABLES:
        columns = 'id, user_id, domain'
        if table in ('memories', 'generated_artifacts'):
            columns += ', metadata_json'
        if table == 'memories':
            columns += ', value'
        tables[table] = list(connection.execute(text(f'SELECT {columns} FROM {table}')).mappings())
    tables['beliefs'] = list(connection.execute(text('SELECT id, user_id, memory_id FROM beliefs')).mappings())
    for table, user, row_id, domain in plan_relabels(tables):
        connection.execute(text(f'UPDATE {table} SET domain = :domain WHERE user_id = CAST(:user AS uuid) AND id = CAST(:id AS uuid)'),
                           {'domain': domain, 'user': user, 'id': row_id})


def downgrade() -> None:
    # Reversing these labels would reopen the disclosure. Content/schema are
    # unchanged, so an older binary can read the repaired rows as they stand.
    pass
