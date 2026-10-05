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

    from alicebot_api.vnext_label_repair import REPAIR_STATE_KEY

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
    assert "Each total checks the complete counted set before pagination" in tools
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


def test_each_derived_label_change_has_one_complete_changelog_entry() -> None:
    """One entry covers each PR, including both stores and all list and producer input doors.

    Mutations: split the repair entry by store; split producer input filtering from the list-door entry;
    remove the migration role or its before-serving requirement; remove a no-migration ending.
    """

    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    starts = (
        "the derived-row pages",
        "PostgreSQL migration `20261005_0096`",
        "recall, context packs",
        "an exact read",
        "a locked key's consolidation",
        "a memory copied from another row",
        "the label of a derived row",
    )
    selected: dict[str, str] = {}
    for start in starts:
        matches = [entry for entry in entries if entry.startswith(f"{MARK} {start}")]
        assert len(matches) == 1, (start, len(matches))
        selected[start] = matches[0]
    assert not any(entry.startswith(f"{MARK} opening a SQLite vault") for entry in entries)
    assert not any(entry.startswith(f"{MARK} a daily brief, connection report") for entry in entries)
    repair = selected[starts[1]]
    assert "SQLite" in repair
    assert "NOSUPERUSER NOBYPASSRLS" in repair
    assert repair.endswith(
        "Migration `20261005_0096` must run before serving requests as the NOSUPERUSER NOBYPASSRLS table owner."
    )
    assert "producer input readers" in selected[starts[2]]
    for start, entry in selected.items():
        assert entry.startswith(MARK)
        assert "v0.20.0" in entry
        if start != starts[1]:
            assert entry.endswith("No migration is required."), start


def test_repair_docs_distinguish_open_restore_and_explicit_commands() -> None:
    """Repair after completion and failed reads have different operator outcomes from a gated open.

    Mutations: gate explicit repair by completion; make restore one-time; turn a failed explicit repair
    into partial success; remove the read-only snapshot contract; replace an unavailable doctor check
    with a zero count.
    """

    backup = " ".join(_text("docs/alpha/backup-and-restore.md").split())
    assert "Explicit repair checks every time, including after the open pass has stamped completion" in backup
    assert "under `BEGIN IMMEDIATE`; a failure rolls back the whole repair" in backup
    assert "The open pass remains a one-time upgrade" in backup
    assert "a restore always repairs its staged copy before publication" in backup
    assert "Check reads a private snapshot and does not upgrade the live vault" in backup
    assert "one `REPEATABLE READ READ ONLY` snapshot, set before the acting user's row-security identity" in backup
    assert "an update that changes no row rolls back the whole repair" in backup
    tools = _text("docs/alpha/mcp-tools.md")
    assert "Explicit repair checks rows even after the open pass has stamped completion" in tools
    assert "every restore repairs its staged copy again" in tools
    assert "A failed explicit repair or restore rolls back the whole change" in tools
    doctor = " ".join(_text("docs/alpha/doctor.md").split())
    assert f"{MARK} the doctors report" in doctor
    assert "`derived labels: unavailable; run labels check`" in doctor
    assert "A failed read is never reported as zero rows" in doctor


def test_source_move_docs_name_the_proved_recovery_and_failure_contract() -> None:
    """A source move names a tested recovery route, confirmation and whole-refusal causes.

    Mutations: remove confirmation, the recovery route, current provenance, the report regeneration
    step, a failure cause or the retry header. A source move must not suggest that old rows are loosened.
    """

    tools = _text("docs/alpha/mcp-tools.md")
    assert "`derived_rows_hidden_from_project_keys`" in tools
    assert "writes nothing when the count is nonzero until `confirm_label_hide` is true" in tools
    assert "On PostgreSQL, the keyless local owner or an unbound admin" in tools
    assert "`POST /v0/vnext/sources/{source_id}/regenerate` with `user_id`" in tools
    assert "from all stored chunks under the source's current labels, scope and provenance" in tools
    assert "Old rows and their provenance stay intact and strict" in tools
    assert "Rerun a report's normal generation route" in tools
    assert "HTTP 201 returns `memory_ids`, `open_loop_ids`, `memory_count` and `open_loop_count`" in tools
    assert "trusted and project-bound keys are refused" in tools
    assert "HTTP 503 and `Retry-After: 2`, with nothing changed" in tools
    assert "HTTP 409 with the cause" in tools
    for cause in ("propagation_bound", "row_changed", "dependency_cycle", "lock_order", "database_error"):
        assert f"`{cause}`" in tools
