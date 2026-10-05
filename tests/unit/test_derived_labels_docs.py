"""The pages that describe derived labels agree with the v3 repair and with migration 0096.

A revision, a table list and the SQLite state key are read from the code. The sentences are pinned by phrase.

Mutations, each one alone: delete ``derived_labels_v3`` from the repair module; drop ``projects`` from the 0096
FORCE bracket; delete ``apply the caller's sensitivity ceiling`` from the tool reference; delete ``rows the caller
may read``; put back ``apply no label``; delete ``label_floor_applied``; delete ``alicebot vnext labels check`` from
the disaster-recovery verify list; delete ``alice-memory labels check`` from the SQLite recovery steps; delete the
dated correction from the v0.20.0 notes; delete ``held at or above those of its inputs``; delete ``every project of
every row it was made from``; in the changelog, put back ``keeps such a row at its old label until a restore``,
``does not change sensitivity`` or ``historical sensitivity is not repaired``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alicebot_api.vnext_label_repair import REPAIR_STATE_KEY

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
MIGRATION = ROOT / "apps/api/alembic/versions/20261005_0096_derived_label_floor.py"


def _migration():
    spec = importlib.util.spec_from_file_location("derived_label_floor_0096_docs", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_the_pages_name_the_v3_repair_and_the_seven_table_migration() -> None:
    """Docs name the state key, the revision and every table the migration brackets, including projects."""

    migration = _migration()
    tables = [item.split()[2] for item in migration._RELAX_RLS]
    assert migration.revision == "20261005_0096"
    assert tables == [
        "sources",
        "memories",
        "open_loops",
        "generated_artifacts",
        "beliefs",
        "event_log",
        "projects",
    ]
    assert REPAIR_STATE_KEY == "derived_labels_v3"

    tools = _text("docs/alpha/mcp-tools.md")
    assert "promoted copies of reports" in tools
    assert "memories extracted from a source" in tools
    assert "the candidate open loops found in one" in tools
    assert "the state a project update copies onto a project" in tools
    assert "label_floor_applied" in tools
    assert "at most 3 s" in tools
    assert "apply the caller's sensitivity ceiling" in tools
    assert "rows the caller may read" in tools
    assert "apply no label" not in tools
    assert f"`{migration.revision}`" in tools
    assert REPAIR_STATE_KEY not in tools

    backup = _text("docs/alpha/backup-and-restore.md")
    assert f"`{migration.revision}`" in backup
    assert "`projects`" in backup
    assert "alice-memory labels check" in backup
    assert "alice-memory labels repair" in backup
    assert "raised again once" in backup

    recovery = _text("docs/runbooks/disaster-recovery.md")
    assert "alicebot vnext labels check" in recovery
    assert "alice-memory labels check" in recovery

    integration = _text("docs/alpha/agent-integration.md")
    assert "every project of every row it was made from" in integration
    assert "mcp-tools.md#derived-row-domains" in integration

    auth = _text("docs/security/auth-authorization.md")
    assert "held at or above those of its inputs" in auth
    assert auth.count(MARK) >= 1

    notes = _text("docs/release/v0.20.0-release-notes.md")
    assert "> **Correction (2026-10-05):**" in notes
    assert "listed under known limitations" in notes
    assert "raised once" in notes

    changelog = _text("CHANGELOG.md")
    assert "keeps such a row at its old label until a restore" not in changelog
    assert "does not change sensitivity" not in changelog
    assert "historical sensitivity is not repaired" not in changelog
    assert "Such a row is unverified." in changelog
    assert "the five operator screens apply the caller's sensitivity ceiling" in changelog
    assert "No migration is required." in changelog.split("## Unreleased", 1)[1].split("\n- ", 2)[1]
