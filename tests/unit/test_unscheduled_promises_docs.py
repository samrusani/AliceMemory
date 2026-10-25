"""Three pieces of work are planned with no release set, and no current page names v0.21.0 for them.

Until 2026-10-21 the docs promised v0.21.0 for three things: removing the words of a redacted memory from reports, hiding the
words of archived rows in reports, and withholding ``capture_content_hash``. None of them is scheduled. The security note
draft, the tool reference, the memory operations protocol, ``CURRENT_STATE.md`` and the known limitations page say
"planned, with no release set". The v0.20.2 release notes are published, so they keep the sentences they were published with
and carry a dated correction (2026-10-21) beside each of the four statements, in the form the earlier corrections use. The
v0.20.2 section of the changelog is published too and is not edited: the v0.21.0 section has one entry that says the three
items are planned with no release set and that the docs were corrected on 2026-10-21.

Other work that the pages tracked for v0.21.0 (the ids, the refill of the vector stage and of the list pages, the configured
folder paths in the connector status, the working copy a killed restore leaves) is not part of this change, and the
sentences about it still name v0.21.0.

Mutations, each one alone: put ``planned for v0.21.0`` back for one of the three in the security note, the tool reference,
the protocol or ``CURRENT_STATE.md``; delete a correction from the v0.20.2 notes; move a correction away from the statement it
follows; delete the date from the v0.21.0 entry; delete one of the three items from it; name v0.21.0 in a bullet of
the known limitations page as work still to do.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTES = "docs/release/v0.20.2-release-notes.md"
UNSCHEDULED = "planned, with no release set"

# The pages that are not published records. The three promises are reworded on each of them that makes one.
CURRENT_PAGES = (
    "docs/release/derived-labels-security-note-draft.md",
    "docs/alpha/mcp-tools.md",
    "docs/memory-operations-protocol.md",
    "CURRENT_STATE.md",
    "docs/alpha/known-limitations.md",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(path: str) -> str:
    return _flat((ROOT / path).read_text(encoding="utf-8"))


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text)


def test_the_current_pages_say_planned_with_no_release_set_for_the_three() -> None:
    """Each page that makes one of the three promises states it as planned with no release set.

    Mutation: put ``is planned for v0.21.0`` or ``is tracked for v0.21.0`` back in one of these sentences.
    """

    note = _read(CURRENT_PAGES[0])
    assert "That is containment and not removal, and removing the words is planned, with no release set." in note
    assert (
        "The words of archived rows are a known exception, not a way of meeting the requirement to filter hidden ids, and "
        "withholding them for a caller with limits is planned, with no release set."
    ) in note
    assert "Full removal of redacted text from reports is planned, with no release set." in note

    tools = _read(CURRENT_PAGES[1])
    assert "withholding the other words is planned, with no release set." in tools
    assert "Full removal of redacted text from reports is planned, with no release set." in tools

    assert "full removal is planned, with no release set." in _read(CURRENT_PAGES[2])
    assert "removal of redacted text from reports is planned, with no release set," in _read(CURRENT_PAGES[3])

    page = _read(CURRENT_PAGES[4])
    assert page.count(UNSCHEDULED) == 3
    for sentence in (
        "withholding it is planned, with no release set",
        "hiding these words is planned, with no release set",
        "removing it is planned, with no release set",
    ):
        assert sentence in page, sentence


def test_no_current_sentence_names_v0210_for_the_three() -> None:
    """A sentence that promises v0.21.0 on a current page (``for v0.21.0``) is about other work, never about the three.

    A sentence may say that something begins ``from v0.21.0``. That describes the release, and the release does none of the
    three. Mutation: put ``planned for v0.21.0`` back beside the redacted text, the archived words or the hash.
    """

    about_the_three = (
        "redacted text",
        "words of",
        "the words",
        "these words",
        "capture_content_hash",
        "removing the words",
        "hiding them",
        "withholding them",
        "removal of redacted",
    )
    for path in CURRENT_PAGES:
        for sentence in _sentences(_read(path)):
            if "for v0.21.0" in sentence or "for `v0.21.0`" in sentence:
                lowered = sentence.lower()
                assert not any(marker in lowered for marker in about_the_three), (path, sentence[:200])


def test_the_published_notes_keep_each_statement_and_carry_a_dated_correction_beside_it() -> None:
    """The four statements of the v0.20.2 notes still say v0.21.0, as published, and a dated correction follows each.

    The immutability of the published lines is held by ``test_published_release_notes_stay_immutable.py``; this test holds that
    each statement has its correction as the paragraph that follows its bullet, with the date and the same words.
    """

    notes = (ROOT / NOTES).read_text(encoding="utf-8")
    pairs = (
        (
            "reports is planned for v0.21.0.\n",
            "> **Correction (2026-10-21):** Full removal of redacted text from reports is no longer scheduled for v0.21.0. "
            "It is planned, with no release set.",
        ),
        (
            "regulated. A request that names every sensitivity lists them.\n\n",
            "> **Correction (2026-10-21):** Removing the redacted text from the reports is no longer scheduled for v0.21.0. "
            "It is planned, with no release set.",
        ),
        (
            "the digest of a consolidation member. Hiding them is tracked for v0.21.0.\n\n",
            "> **Correction (2026-10-21):** Hiding the words of archived rows from the reports is no longer scheduled for "
            "v0.21.0. It is planned, with no release set.",
        ),
        (
            "Withholding these is tracked\n  for v0.21.0.\n\n",
            "> **Correction (2026-10-21):** Withholding `capture_content_hash` is no longer scheduled for v0.21.0. It is "
            "planned, with no release set.",
        ),
    )
    assert notes.count("**Correction (2026-10-21):**") == len(pairs)
    for statement, correction in pairs:
        assert notes.count(statement) == 1, statement
        after = notes[notes.index(statement) + len(statement) :].lstrip("\n")
        assert after.startswith(correction + "\n"), (statement, after[:160])
    # The statements are the published ones: v0.21.0 is still named where the notes name it.
    assert "removing it is planned for v0.21.0" in _flat(notes)
    assert "Hiding them is tracked for v0.21.0" in _flat(notes)
    assert "Withholding these is tracked for v0.21.0" in _flat(notes)


def test_the_v0210_changelog_entries_name_the_three_the_date_and_the_shorter_page() -> None:
    """One entry says the three are planned with no release set and when the docs were corrected; one says the page is shorter.

    The v0.20.2 section of the changelog is published and keeps the sentences that named v0.21.0.
    """

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = changelog[changelog.index("## v0.21.0") : changelog.index("## v0.20.2")]
    entries = [_flat(line.removeprefix("- ")) for line in released.splitlines() if line.startswith("- ")]

    promise = [entry for entry in entries if entry.startswith("Removing the words of a redacted memory from reports")]
    assert len(promise) == 1
    entry = promise[0]
    assert (
        "Removing the words of a redacted memory from reports, hiding the words of archived rows in reports, and withholding "
        "`capture_content_hash` are planned, with no release set."
    ) in entry
    assert "They are no longer scheduled for v0.21.0." in entry
    assert "were corrected on 2026-10-21" in entry
    assert "the v0.20.2 release notes, which gained a dated correction beside each of the four statements" in entry
    assert "The v0.20.2 section of this changelog is unchanged and still says v0.21.0 where it was published that way." in entry

    shorter = [entry for entry in entries if entry.startswith("The known limitations page is shorter.")]
    assert len(shorter) == 1
    entry = shorter[0]
    assert "It describes v0.21.0" in entry
    assert "no longer retells what earlier releases did. That history stays in this changelog and in the release notes." in entry
    assert "a ChatGPT export takes about 6 to 9 times its size in memory (`docs/integrations/importers.md`)" in entry
    assert "a backup omits agent identities as well as users and API keys (`docs/alpha/backup-and-restore.md`)" in entry
    assert (
        "a provider that redirects must be configured with its final URL (`docs/security/threat-model.md`)"
    ) in entry

    released = _flat(changelog[changelog.index("## v0.20.2") : changelog.index("## v0.20.0")])
    assert "Full removal of redacted text from reports is planned for v0.21.0." in released
    assert "Withholding those words for a caller with limits is tracked for v0.21.0 too." in released


def test_the_known_limitations_page_names_no_release_for_unscheduled_work() -> None:
    """The page is written for v0.21.0 and names no later release, because nothing it lists is scheduled for one.

    v0.21.0 appears only as the release the page describes: the lead-in, the release notes link, the heading of the open
    list and the ``From v0.21.0`` clauses. Mutation: write ``planned for v0.21.0``, ``tracked for v0.22.0`` or any other
    release name beside an item that has no release into the page.
    """

    page = _read("docs/alpha/known-limitations.md")
    rest = page
    for described in (
        "v0.21.0-release-notes.md",
        "limited now in v0.21.0",
        "new in v0.21.0 is marked `From v0.21.0`",
        "Open in v0.21.0,",
        "From v0.21.0,",
        "from v0.21.0,",
    ):
        rest = rest.replace(described, "")
    assert "v0.21" not in rest
    assert "0.22" not in page
    assert not re.search(r"(?:planned|tracked|scheduled|due) for v0\.\d+\.\d+", page)
