"""Every bracketed migration restores the forced row-level security settings it relaxes.

A migration that brackets its data statements with ``ALTER TABLE ... NO FORCE ROW LEVEL SECURITY``
must issue the matching ``FORCE ROW LEVEL SECURITY`` before its transaction commits: a table whose
forced setting stays off binds the table owner to no policy for statements the policies do not
cover. The strict-owner and superuser tests share the migration's code, so a removed restore
changes no row either path reads; this test reads the catalog directly. For each migration whose
upgrade text relaxes the forced setting, it upgrades a database to the revision before it, records
every public table's ``relrowsecurity`` and ``relforcerowsecurity``, upgrades to the revision, and
requires the two catalogs to be equal for every table that existed before.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest
from alembic import command

from alicebot_api.migrations import make_alembic_config
from tests.integration.test_migrations_0067_strict_owner import (  # noqa: F401  (fixtures)
    superuser_reference_urls,
)

VERSIONS_DIR = Path(__file__).resolve().parents[2] / "apps" / "api" / "alembic" / "versions"


def _bracketed_migrations() -> list[tuple[str, str, tuple[str, ...]]]:
    """(revision, down_revision, bracketed tables) of every migration whose upgrade relaxes the forced setting.

    The upgrade side of each file is found by attributing every
    ``ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`` line to the module-level
    tuple or function that contains it and leaving the downgrade side out.
    """

    found: list[tuple[str, str, tuple[str, ...]]] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        text = path.read_text()
        owner = "module"
        tables: set[str] = set()
        for line in text.splitlines():
            match = re.match(r"^(_\w+)\s*[:=]|^def (\w+)", line)
            if match:
                owner = match.group(1) or match.group(2) or owner
            if "downgrade" in owner:
                continue
            hit = re.search(r"ALTER TABLE (\w+) NO FORCE ROW LEVEL SECURITY", line)
            if hit:
                tables.add(hit.group(1))
        if not tables:
            continue
        revision = re.search(r'^revision = "([^"]+)"', text, re.MULTILINE)
        down_revision = re.search(r'^down_revision = "([^"]+)"', text, re.MULTILINE)
        assert revision and down_revision, path
        found.append((revision.group(1), down_revision.group(1), tuple(sorted(tables))))
    assert len(found) >= 13
    return found


def _forced_flags(admin_url: str) -> dict[str, tuple[bool, bool]]:
    with psycopg.connect(admin_url) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        ).fetchall()
    return {name: (bool(rks), bool(rfrs)) for name, rks, rfrs in rows}


@pytest.mark.parametrize("revision,down_revision,tables", _bracketed_migrations())
def test_a_bracketed_migration_restores_every_forced_flag(
    superuser_reference_urls, revision, down_revision, tables
) -> None:
    """The row-level security and forced flags of every bracketed table are identical before and after."""

    config = make_alembic_config(superuser_reference_urls["admin"])
    command.upgrade(config, down_revision)
    before = _forced_flags(superuser_reference_urls["admin"])
    command.upgrade(config, revision)
    after = _forced_flags(superuser_reference_urls["admin"])

    # A table the migration itself creates did not exist before; its flags
    # are judged by the migration's own statements, not by this comparison.
    changed = {
        table: (before[table], after[table])
        for table in tables
        if table in before and before[table] != after.get(table)
    }
    assert changed == {}, changed
