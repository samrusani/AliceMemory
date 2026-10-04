"""The docs say what v0.20.0 does in the local-folder connector and keep v0.19.2's behaviour as the comparison.

v0.19.2 is released, so its notes and the sentences that describe it stay as they
are. What v0.20.0 changed is marked ``From v0.20.0,`` where a
document describes the latest release, and sits under the changelog's v0.20.0
heading. The hard link a planted file can use to reach content elsewhere is a
documented residual in every place the fix is described.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FROM_V0200 = "From v0.20.0,"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _v0200_changelog() -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return changelog[changelog.index("## v0.20.0 \u2014 2026-10-02") + len("## v0.20.0 \u2014 2026-10-02") : changelog.index("## v0.19.2")]


def _changelog_entry(start: str) -> str:
    entries = [item for item in _v0200_changelog().split("\n- ")[1:] if item.startswith(start)]
    assert len(entries) == 1, start
    return _flat(entries[0])


def test_the_changelog_entry_for_the_connector_sits_under_v0200_and_states_v0192() -> None:
    """One v0.20.0 entry for the connector, with the v0.19.2 behaviour beside the change.

    Mutations, each one alone: move the entry under the v0.19.2 heading; delete
    the sentence that says what v0.19.2 did; delete the hard link sentence; delete
    the sentence that says an ordinary file returns what it returned before.
    """

    entry = _changelog_entry("The local-folder connector (")
    assert "(`alicebot vnext connectors local-folder sync` and `watch`, Postgres stack only)" in entry
    assert (
        "In v0.19.2 the scan checked that a file was inside the watched folder and then read it by path, so a file or "
        "an ancestor directory replaced by a symlink between the two steps was read from outside the folder"
    ) in entry
    assert "each relative to the one before and with `O_NOFOLLOW`" in entry
    assert "is skipped on its own and counted in `refused_count`, and the rest of the folder still scans" in entry
    assert "A hard link planted inside the watched folder to a file elsewhere is still read, as in the importers." in entry
    assert "The text, size, times and line endings of an ordinary file are what v0.19.2 returned." in entry


def test_the_threat_model_and_limitations_mark_the_connector_fix_from_v0200_and_keep_the_hard_link() -> None:
    """The threat model and the security notes mark the fix, and the limitations page states what the scan does now.

    The known limitations page lists what is limited now, so it no longer retells what v0.19.2 did
    (a read by path after a containment check). That history is in the threat model, in the
    changelog entry pinned in ``test_the_changelog_entry_for_the_connector_sits_under_v0200_and_states_v0192``
    and in the v0.19.2 and v0.20.0 notes. All three documents keep the hard link residual.

    Mutations, each one alone: drop the ``From v0.20.0,`` marker from the threat model or the
    security notes; delete the hard link sentence from one of the three documents; say the fix is
    in v0.19.2; delete the sentence about opening each directory and the file one at a time from
    the limitations page.
    """

    threat_model = _read("docs/security/threat-model.md")
    limitations = _read("docs/alpha/known-limitations.md")
    privacy = _read("docs/vnext/security-privacy.md")
    for site, text in (("threat-model", threat_model), ("known-limitations", limitations), ("security-privacy", privacy)):
        assert "hard link planted inside the watched folder to a file elsewhere" in text, site
    assert "Fixed in v0.20.0 (added 2026-10-01). DB-010, the local-folder scan reading a file swapped for a link" in threat_model
    assert f"{FROM_V0200} the local-folder connector opens the watched folder" in threat_model
    assert "v0.19.2 checked containment and then read by path" in threat_model
    assert (
        "The local-folder connector reads a hard link planted in the watched folder too, because a hard link is the "
        "file itself."
    ) in threat_model
    assert "the local-folder scan opens the watched folder, each directory below it" in limitations
    assert "without following a link, so a file or directory swapped for a link is skipped and counted in `refused_count`" in limitations
    assert f"{FROM_V0200} each local folder file is read through a descriptor" in privacy
    assert "constraint to allowed local roots also holds for the read itself" in privacy


def test_the_changelog_states_the_bounds_and_what_v0192_did_without_them() -> None:
    """The four numbers, the v0.19.2 behaviour, and where the skips are shown.

    Mutations, each one alone: change any of 2 MiB, 10,000 files, 64 MiB or
    100,000 entries; delete the sentence about v0.19.2 having no limit and
    ending the sync on one bad file; delete the sentence that names the event and
    the health output; say the limits are settings.
    """

    entry = _changelog_entry("The local-folder connector (")
    assert (
        "In v0.19.2 the scan had no size or count limit: it listed and sorted the whole walk and read every matching "
        "file whole, and one file that was not UTF-8 text, or that the process could not read, ended the sync with "
        "an error (a four byte file of invalid text was enough)."
    ) in entry
    assert (
        "The scan now reads at most 2 MiB of one file, stops at 10,000 files or 64 MiB of text in all, and lists at "
        "most 100,000 directory entries before it sorts them."
    ) in entry
    assert "`truncated` is true when a limit stopped the scan before it had read everything that matched." in entry
    assert (
        "`refused_count` and `truncated` are written to the `connector.local_folder_scan` event and shown as "
        "`last_scan` in the health output of the connector."
    ) in entry
    assert "The limits are fixed in code and are not settings." in entry


def test_the_threat_model_and_limitations_mark_the_bounds_from_v0200_and_keep_v0192() -> None:
    """The threat model keeps the v0.19.2 sentence and marks the bounds from v0.20.0; the limitations page states the bounds.

    The limitations page lists what is limited now, so the v0.19.2 half (no size limit, one bad
    file ended the sync) is pinned on the dated records: the threat model here and the changelog entry
    in ``test_the_changelog_states_the_bounds_and_what_v0192_did_without_them``.

    Mutations, each one alone: drop the DB-011 sentence from the threat model; delete the
    sentence that says v0.19.2 ended the sync on one such file from the threat model; drop the
    resource exhaustion row's marker; change 2 MiB, 10,000 files or 64 MiB on the limitations page.
    """

    threat_model = _read("docs/security/threat-model.md")
    limitations = _read("docs/alpha/known-limitations.md")
    assert (
        "DB-011, the local-folder scan with no size or count bound. The scan now reads at most 2 MiB of a file, stops "
        "at 10,000 files or 64 MiB of text in all, lists at most 100,000 directory entries"
    ) in threat_model
    assert "v0.19.2 ended the whole sync with an error on one such file." in threat_model
    assert (
        f"Existing size/shape checks and local deployment limits. {FROM_V0200} the local-folder scan reads at most "
        "2 MiB of a file and stops at 10,000 files or 64 MiB."
    ) in threat_model
    assert "the local-folder scan opens the watched folder" in limitations
    assert "It reads at most 2 MiB of a file, stops at 10,000 files or 64 MiB in all" in limitations
    assert "and lists at most 100,000 directory entries, and sets `truncated` when a limit stopped it" in limitations


def test_the_cli_page_says_what_sync_and_watch_print_and_what_ignored_count_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI integration page, a page about the latest state of the commands, states the two numbers and ``ignored_count``.

    Both facts were pinned only on dated records (the v0.20.0 changelog entry and release notes), so a correction to the
    current docs would not have been caught. The page says that ``sync`` and ``watch`` print ``refused_count`` and
    ``truncated``, and what ``ignored_count`` counts. The printing is run by ``test_sync_prints_what_the_scan_refused_and_whether_it_stopped``
    and ``test_watch_prints_what_each_scan_refused_and_whether_it_stopped`` in ``test_local_folder_scan_gaps.py``; the
    count is run here against a real scan, so the page and the code cannot drift together.

    Mutations, each one alone: on the page, change ``print `refused_count` and `truncated` `` to ``print `refused_count` ``,
    ``so the files inside one are not counted`` to ``so the files inside one are counted``, or delete the sentence about
    ``refused_count``; in ``_walk_local_folder``, stop pruning ignored folders (``ignored_count`` then counts the
    note inside ``node_modules``).
    """

    page = _read("docs/integrations/cli.md")
    assert (
        "`alicebot vnext connectors local-folder sync` and `watch` print `refused_count` and `truncated` with the sync "
        "result."
    ) in page
    assert (
        "`refused_count` is the number of files the scan skipped on its own (a file over the size limit, one that is not "
        "UTF-8 text or cannot be read, or one swapped for a link), and `truncated` is true when a limit stopped the scan."
    ) in page
    assert (
        "`ignored_count` counts the files the scan listed and then ignored: the scan does not enter a folder named like a "
        "default ignore, such as `node_modules` or `.git`, so the files inside one are not counted."
    ) in page

    from alicebot_api import vnext_connectors as connectors

    monkeypatch.setenv(connectors.LOCAL_FOLDER_ROOTS_ENV, str(tmp_path))
    root = tmp_path / "watched"
    (root / "node_modules").mkdir(parents=True)
    (root / "keep.md").write_text("keep", encoding="utf-8")
    (root / "drop.skip.md").write_text("drop", encoding="utf-8")
    (root / "node_modules" / "dep.md").write_text("dep", encoding="utf-8")
    scan = connectors.scan_local_folder([root], ignore_patterns=["*.skip.md"])
    assert [item["relative_path"] for item in scan.items] == ["keep.md"]
    assert scan.ignored_count == 1, "the pattern drops one note; the note inside node_modules was never listed"
