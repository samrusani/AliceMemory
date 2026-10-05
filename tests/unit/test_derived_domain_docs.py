"""The pages that describe how derived rows keep their restricted input domains agree with the code.

The known limitations page is a short list, so it holds one bullet and a link. The explanation is on the two pages
that hold full explanations: ``mcp-tools.md`` (which label a derived row keeps, what the stored-row repair follows and
leaves alone, and what readers see change) and ``backup-and-restore.md`` (when the repair runs on a restore and on a
PostgreSQL upgrade, and what it can and cannot repair). A name, a revision, a table or a number that the code owns is read
from the code, so a change that leaves a page behind fails here.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from alicebot_api import vnext_derived_domain_backfill as repair

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"
LIMITATIONS = ROOT / "docs/alpha/known-limitations.md"
MCP_TOOLS = ROOT / "docs/alpha/mcp-tools.md"
BACKUP = ROOT / "docs/alpha/backup-and-restore.md"
MIGRATION = ROOT / "apps/api/alembic/versions/20261004_0095_derived_restricted_domains.py"
HEADING = "Derived row domains"
ANCHOR = "mcp-tools.md#derived-row-domains"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _paragraph(path: Path, start: str) -> str:
    """The one paragraph (or list bullet) of a page that starts with ``start``, whitespace collapsed."""

    blocks = [_flat(block) for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))]
    found = [item for block in blocks for item in re.split(r" (?=- )", block) if item.startswith(start)]
    assert len(found) == 1, (path.name, start, len(found))
    return found[0]


def _section(path: Path, heading: str) -> str:
    """The text under ``## heading`` up to the next second-level heading, whitespace collapsed."""

    parts = re.split(r"(?m)^## ", path.read_text(encoding="utf-8"))
    found = [part for part in parts if part.startswith(heading + "\n")]
    assert len(found) == 1, (path.name, heading, len(found))
    return _flat(found[0].partition("\n")[2])


def _migration():
    spec = importlib.util.spec_from_file_location("derived_domain_migration_0095", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_limitations_page_has_one_short_bullet_that_links_to_the_explanation() -> None:
    """The page states the label rule, the two limits that remain and the link, and nothing longer.

    Mutations, each one alone: delete ``not `unknown```, ``may be readable through one of them``, ``is not
    repaired`` or the link from the bullet; start the bullet without the marker; point the link at
    ``#derived-rows``; add a ``## Derived`` heading with a paragraph to the page.
    """

    bullet = _paragraph(LIMITATIONS, "- " + MARK + " a summary of restricted inputs")
    assert "keeps their most frequent restricted label, not `unknown`" in bullet
    assert "one that combines project scopes may be readable through one of them" in bullet
    assert "a stored one whose inputs no longer resolve is not repaired" in bullet
    assert bullet.endswith(f"See [{HEADING}]({ANCHOR})")
    assert "\n## Derived" not in LIMITATIONS.read_text(encoding="utf-8")


def test_the_mcp_tools_section_states_the_label_rule_the_repair_and_the_changes_readers_see() -> None:
    """Each paragraph carries the marker, and the facts that the page's one-line statement rests on are all here.

    The number of rows an unsettled-repair error names is read from ``_UNSETTLED_ROWS_SHOWN``.

    Mutations, each one alone: delete ``with alphabetical ties``, ``An explicit request domain cannot override it``,
    ``within the same user``, ``they never become unrestricted``, ``instead of publishing intermediate labels``,
    ``or restore an earlier backup``, ``rows without resolvable recorded inputs are left alone``, ``text is never
    used to guess an input``, the link to the backup guide, ``unknown` previously matched every domain filter``,
    ``project scope is unchanged`` or ``covers only copies with recorded inputs that still resolve``; change ``up to
    five`` to ``up to ten``; remove the marker from one paragraph; rename the heading.
    """

    section = _section(MCP_TOOLS, HEADING)
    paragraphs = re.split(r" (?=Unreleased \(on main, not in v0\.20\.0\): )", section)
    assert len(paragraphs) == 5 and all(item.startswith(MARK) for item in paragraphs), len(paragraphs)
    label, stored, where, readers, scope = paragraphs

    assert "keeps the most frequent restricted input label, with alphabetical ties" in label
    assert "An explicit request domain cannot override it" in label
    assert "With no restricted inputs, each producer retains its prior selection" in label
    for producer in (
        "briefs",
        "weekly synthesis and its candidates",
        "roll-ups",
        "consolidation",
        "connection and contradiction reports",
        "staleness reports",
        "open-loop reviews",
        "project updates",
    ):
        assert producer in label, producer

    shown = {5: "five"}[repair._UNSETTLED_ROWS_SHOWN]
    assert "resolves recorded input IDs within the same user" in stored
    assert "`value.kind`, `value.artifact_id` or `metadata_json.source_artifact_id`" in stored
    assert "they never become unrestricted" in stored
    assert (
        "aborts the upgrade or restore instead of publishing intermediate labels, with an error that names up to "
        f"{shown} of the rows that kept changing and says to remove their circular input references or restore an "
        "earlier backup"
    ) in stored
    assert "Redacted rows and rows without resolvable recorded inputs are left alone; text is never used to guess an input" in stored

    assert "SQLite upgrades a vault" in where and "`alice-memory import` stages a restore" in where
    assert f"PostgreSQL migration `{_migration().revision}`" in where
    assert "[Backup and restore](backup-and-restore.md)" in where

    assert "`unknown` previously matched every domain filter" in readers
    assert "New weekly candidate memories also store `input_summary`" in readers
    assert "does not add missing historical input summaries or repair historical sensitivity values" in readers

    assert "project scope is unchanged" in scope
    assert "a reader matching one scope can potentially read a summary of other scopes" in scope
    assert "the repair covers only copies with recorded inputs that still resolve" in scope


def test_the_backup_guide_states_when_the_repair_runs_and_what_it_cannot_repair() -> None:
    """The restore paragraph and the PostgreSQL paragraph agree with the migration and with the import.

    The revision, the tables whose FORCE setting the migration turns off and the role are read from the migration, and
    the code a refused import prints is the one the import test checks.

    Mutations, each one alone: in the restore paragraph, delete ``in the complete staged copy``, ``whether the
    destination is new or was already upgraded``, ``one audit event``, ``still refuses any other field that
    differs``, ``cannot be repaired from that reference``, ``stop the import with `restore_failed` before
    publication`` or the link; in the PostgreSQL paragraph, delete ``Run it before serving requests``, ``is not
    repaired again``, one table name, ``NOSUPERUSER NOBYPASSRLS``, ``rolls the relabels, their audit events and the
    FORCE change back together`` or ``The downgrade keeps the repaired labels``; change the revision in the
    paragraph; in the migration, drop one table from ``_RELAX_RLS``.
    """

    restore = _paragraph(BACKUP, MARK + " before it publishes the restored database")
    assert "import also repairs the labels of derived memories in the complete staged copy" in restore
    assert "whether the destination is new or was already upgraded" in restore
    assert "takes the most frequent restricted label among them" in restore
    assert "each memory the repair changes gets one audit event with its old and new domain" in restore
    assert "Repeating `--mode skip` accepts a memory whose label that repair changed, and still refuses any other field that differs" in restore
    assert "A vault that is upgraded without a restore gets the same repair once, the next time it opens" in restore
    assert "A portable backup carries no generated artifacts and the SQLite schema has no table for them" in restore
    assert "cannot be repaired from that reference and keeps its label" in restore
    assert "stop the import with `restore_failed` before publication, and nothing is written" in restore
    assert f"[{HEADING}]({ANCHOR})" in restore

    migration = _migration()
    postgres = _paragraph(BACKUP, MARK + f" migration `{migration.revision}`")
    assert "Run it before serving requests" in postgres
    assert "A database restored at an older revision gets it from the release upgrade" in postgres
    assert "one restored at or past it is not repaired again, so that backup must already hold the repaired labels" in postgres
    assert "The documented table owner, `alicebot_admin`, is `NOSUPERUSER NOBYPASSRLS`" in postgres
    tables = [re.fullmatch(r"ALTER TABLE (\w+) NO FORCE ROW LEVEL SECURITY", item).group(1) for item in migration._RELAX_RLS]
    assert len(tables) == 6
    for table in tables:
        assert f"`{table}`" in postgres, table
    assert "inside its own transaction and turns it back on before it commits" in postgres
    assert "rolls the relabels, their audit events and the FORCE change back together" in postgres
    assert "including derived rows in a cycle whose labels do not settle within a bounded number of changes" in postgres
    assert "The downgrade keeps the repaired labels" in postgres
