"""The pages and the changelog say that a failed item is named only to a key that may read it, and which versions had the old behaviour."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "From v0.20.1,"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _entry(prefix: str) -> str:
    entries = [line.removeprefix("- ") for line in _text("CHANGELOG.md").splitlines() if line.startswith("- ")]
    matches = [entry for entry in entries if entry.startswith(prefix[:1].upper() + prefix[1:])]
    assert len(matches) == 1, prefix
    return matches[0]


def test_the_changelog_has_one_entry_for_the_connector_failure_events() -> None:
    """Mutation: delete a sentence that names a version, an event or a field."""

    entry = _entry("the connector screens do not name a file whose import failed to a key that may not read it")
    assert "In v0.19.2 and in v0.20.0 an unbound `trusted_local_agent` key" in entry
    assert "on main until now" not in entry
    for name in ("`connector.item_failed`", "`connector.sync_failed`", "`connector.sync_completed`", "`external_id`", "`sync_cursor`", "`failed_external_ids`"):
        assert name in entry, name
    assert "`GET /v0/vnext/connectors/{connector_name}/status` (`recent_failures`)" in entry
    assert "is `null` otherwise, in its place in a list of ids" in entry
    assert "Checked and left as they are" in entry and "`connector.item_rejected`" in entry
    assert "The answer of a sync (`POST /v0/vnext/connectors/{connector_name}/sync` and the two sync routes that name one connector) follows the same rule" in entry
    assert "where the failed ids are files the caller never sent" in entry
    assert "a key that imports a public item at a path it already knows" in entry and "it can confirm that a failed import of a path it can name exists" in entry
    assert "the answer of a sync (the ids of the items of that call)" not in entry
    assert "took 0.96 s a call when each id scanned them all" in entry
    assert "\u2014" not in entry and "\u2013" not in entry


def test_the_older_entries_no_longer_list_the_failed_item_id_as_open() -> None:
    changelog = _text("CHANGELOG.md")
    assert "the failure list of the connector status names the id the importer gave to an item that failed" not in changelog
    note = _text("docs/release/derived-labels-security-note-draft.md")
    assert "the failure list of the connector status shows the id the importer gave" not in note
