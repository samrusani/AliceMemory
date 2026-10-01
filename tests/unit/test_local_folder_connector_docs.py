"""The docs say what main does in the local-folder connector and keep v0.19.2's behaviour as the comparison.

v0.19.2 is released, so its notes and the sentences that describe it stay as they
are. What main changed is marked ``Unreleased (on main, not in v0.19.2):`` where a
document describes the latest release, and sits under the changelog's Unreleased
heading. The hard link a planted file can use to reach content elsewhere is a
documented residual in every place the fix is described.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNRELEASED = "Unreleased (on main, not in v0.19.2):"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return _flat((ROOT / name).read_text(encoding="utf-8"))


def _unreleased_changelog() -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.19.2")]


def _changelog_entry(start: str) -> str:
    entries = [item for item in _unreleased_changelog().split("\n- ")[1:] if item.startswith(start)]
    assert len(entries) == 1, start
    return _flat(entries[0])


def test_the_changelog_entry_for_the_connector_sits_under_unreleased_and_states_v0192() -> None:
    """One Unreleased entry for the connector, with the v0.19.2 behaviour beside the change.

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


def test_the_threat_model_and_limitations_mark_the_connector_fix_unreleased_and_keep_the_hard_link() -> None:
    """Each place that describes the latest release marks the fix and keeps the residual.

    Mutations, each one alone: drop the Unreleased marker from one document; delete
    the hard link sentence from one document; say the fix is in v0.19.2.
    """

    threat_model = _read("docs/security/threat-model.md")
    limitations = _read("docs/alpha/known-limitations.md")
    privacy = _read("docs/vnext/security-privacy.md")
    for site, text in (("threat-model", threat_model), ("known-limitations", limitations), ("security-privacy", privacy)):
        assert "hard link planted inside the watched folder to a file elsewhere" in text, site
    assert f"{UNRELEASED} DB-010, the local-folder scan reading a file swapped for a link" in threat_model
    assert f"{UNRELEASED} the local-folder connector opens the watched folder" in threat_model
    assert "v0.19.2 checked containment and then read by path" in threat_model
    assert (
        "The local-folder connector reads a hard link planted in the watched folder too, because a hard link is the "
        "file itself."
    ) in threat_model
    assert "the local-folder scan reads each matching file whole with no size limit" in limitations
    assert f"{UNRELEASED} the scan opens the watched folder, each directory below it" in limitations
    assert f"{UNRELEASED} each local folder file is read through a descriptor" in privacy
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


def test_the_threat_model_and_limitations_mark_the_bounds_unreleased_and_keep_v0192() -> None:
    """Each document keeps the v0.19.2 sentence and marks the bounds as main only.

    Mutations, each one alone: drop the DB-011 sentence from the threat model;
    drop the Unreleased marker before the bounds in the limitations; delete the
    sentence that says v0.19.2 ended the sync on one such file; drop the
    resource exhaustion row's marker.
    """

    threat_model = _read("docs/security/threat-model.md")
    limitations = _read("docs/alpha/known-limitations.md")
    assert (
        "DB-011, the local-folder scan with no size or count bound. The scan now reads at most 2 MiB of a file, stops "
        "at 10,000 files or 64 MiB of text in all, lists at most 100,000 directory entries"
    ) in threat_model
    assert "v0.19.2 ended the whole sync with an error on one such file." in threat_model
    assert (
        f"Existing size/shape checks and local deployment limits. {UNRELEASED} the local-folder scan reads at most "
        "2 MiB of a file and stops at 10,000 files or 64 MiB."
    ) in threat_model
    assert "and one file that is not UTF-8 text or cannot be read ends the whole sync with an error." in limitations
    assert f"{UNRELEASED} the scan opens the watched folder" in limitations
    assert "It reads at most 2 MiB of a file, stops at 10,000 files or 64 MiB in all" in limitations
