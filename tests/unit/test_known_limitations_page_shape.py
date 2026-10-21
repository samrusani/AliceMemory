"""The known limitations page is a short list of what is limited now, and every link on it lands.

The page describes the release it ships with, v0.21.0. It used to retell the history of each limit (what v0.19.0 and v0.19.2
did, what v0.20.0 fixed, and "From v0.20.2" narration). That history is on the dated records (the CHANGELOG and the
release notes), so each current limit on the page is a sentence or two and a link to the page that explains it, and a
bound that another page owns (the import file size, the query size, the export and import rules, the HTTP edge, the
local-folder scan, the outbound calls, SQLite source deletion) is one short sentence and a link. Behaviour that is new
in v0.21.0 is marked ``From v0.21.0``. These checks keep the page from growing back into a
record and keep its links from rotting.

The tests that pin what the page says about one limit (the query bounds, the local-folder scan, the expiry
bullets and the rest) live next to the other pages that state the same limit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/alpha/known-limitations.md"

# The page is 10,147 characters in 25 bullets, and its longest bullet is about 670. It was 19,297 characters in 57
# bullets before the version history and the bounds that other pages own left it. The old page before that was 30,000,
# with single bullets of 4,000 to 6,000. The caps sit just above the page as it is, so a limit can be reworded and a
# new one added, and nothing grows back into a record without this test failing. A bullet is one or two sentences, and
# a closing "See ..." pointer to the page that explains it does not count as one.
# Fit 2026-10-21 to the shortened page.
MAX_PAGE_CHARS = 10_270
MAX_BULLET_CHARS = 800
MAX_BULLET_SENTENCES = 2


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


def _bullets(text: str) -> list[str]:
    """Each bullet of the list, whitespace collapsed. A bullet ends at the next bullet or at a blank line, so the
    paragraphs that follow the last bullet are not counted as part of it."""

    found: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        if block.startswith("- "):
            found.extend(" ".join(chunk.split()) for chunk in re.split(r"\n(?=- )", block))
    return found


def _sentences(bullet: str) -> int:
    """The sentences of a bullet, not counting a closing pointer (``See ...``): code spans and link targets are
    removed first, so a full stop inside ``v0.20.0`` or ``mcp-tools.md`` does not end one."""

    plain = re.sub(r"`[^`]*`", "X", bullet)
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    sentences = re.split(r"[.!?]\s+(?=[A-Z])", plain.rstrip("."))
    return len([sentence for sentence in sentences if not sentence.startswith("See ")])


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""

    heading = heading.strip().lower()
    heading = re.sub(r"[^\w\- ]", "", heading)
    return heading.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            anchors.add(_slug(match.group(1)))
    return anchors


def test_every_relative_link_on_the_page_lands_on_a_file_and_a_heading() -> None:
    """Each ``[text](target)`` link that is not a web address names a file that exists and, with ``#anchor``, a heading in it.

    Mutations, each one alone: change ``mcp-tools.md#size-bounds`` to ``mcp-tools.md#size-limits``; point the
    CHANGELOG link at ``../CHANGELOG.md``; rename a heading that the page links to in the target page.
    """

    links = re.findall(r"\]\(([^)\s]+)\)", _text())
    assert len(links) >= 14, "the page lost its links to the pages that explain each limit"
    for link in links:
        if re.match(r"^[a-z]+:", link):
            continue
        target, _, anchor = link.partition("#")
        path = (PAGE.parent / target).resolve() if target else PAGE
        assert path.is_file(), f"{link} does not name a file"
        if anchor:
            assert anchor in _anchors(path), f"{link}: no heading in {path.name} gives the anchor {anchor!r}"


def test_the_pages_the_limits_point_at_are_the_ones_that_explain_them() -> None:
    """The size bounds, the cited sources, the redacted memories, the error codes, the legacy tool list, the backup guide,
    the importers (the file size limit and SQLite source deletion), the threat model, the security note and the
    confirm and reject rules each have a link, so a limit is never stated without a way to the full explanation.

    Mutation: delete one of these links from the page.
    """

    text = _text()
    for target in (
        "mcp-tools.md#size-bounds",
        "mcp-tools.md#error-codes",
        "mcp-tools.md#cited-sources",
        "mcp-tools.md#redacted-memories",
        "mcp-tools.md#legacy-tool-surface",
        "backup-and-restore.md",
        "../integrations/importers.md",
        "../integrations/importers.md#list-delete-and-prune-sqlite-sources",
        "../security/threat-model.md",
        "../memory-operations-protocol.md#confirm-and-reject-rules",
        "../release/derived-labels-security-note-draft.md",
        "../release/v0.21.0-release-notes.md",
        "../../CHANGELOG.md",
    ):
        assert f"]({target})" in text, target


def test_the_page_is_short_and_no_bullet_is_a_record() -> None:
    """The page and each bullet stay under a size that a reader can scan, and a bullet is one or two sentences.

    Mutations, each one alone: put the old 4,000 character query bounds bullet, the old open loop fence paragraph or the
    1,090 character key-bound source bullet back; add two sentences to any bullet; add a 1,000 character paragraph of
    history to the page.

    The floor on the number of bullets was 40 for the 57 bullet page. The page is 25 bullets now, because the scope
    items are one short paragraph and the bounds that other pages own are one sentence each, so the floor is 20: it only
    has to show that the list was read.
    """

    text = _text()
    assert len(text) <= MAX_PAGE_CHARS, len(text)
    bullets = _bullets(text)
    assert len(bullets) >= 20, "the list of limits was not read"
    for bullet in bullets:
        assert len(bullet) <= MAX_BULLET_CHARS, (len(bullet), bullet[:80])
        assert _sentences(bullet) <= MAX_BULLET_SENTENCES, (_sentences(bullet), bullet[:80])
    # The last bullet is not stretched over the paragraphs after the list: a bullet ends at the blank line.
    assert not any("See [Backup and restore]" in bullet for bullet in bullets)


def test_the_page_does_not_retell_a_limit_an_earlier_release_had() -> None:
    """A fixed limit is not listed as open, and no bullet opens with what an earlier release did.

    The v0.19.2 and v0.20.0 notes and the CHANGELOG hold that history, and the pins on it are on those records.

    Mutations, each one alone: put the heading ``Items from the internal security review of v0.19.0 that v0.19.2
    left open`` or ``Also open in v0.19.2, and fixed in v0.20.0`` back; start a bullet with ``in v0.19.2`` or
    ``in v0.19.0``.
    """

    text = _text()
    flat = " ".join(text.split())
    for retold in (
        "Items from the internal security review",
        "left open",
        "Also open in v0.19.2",
        "fixed in v0.20.0:",
    ):
        assert retold not in flat, retold
    for bullet in _bullets(text):
        assert not re.match(r"^- in v0\.(19\.0|19\.2) ", bullet, flags=re.IGNORECASE), bullet[:80]


def test_the_page_says_where_the_history_is() -> None:
    """The header says the page describes v0.21.0 and sends a reader of an earlier release to the records. The closing
    paragraph that named what v0.19.0, v0.19.2 and v0.20.0 limited is gone, because the header already points there.

    Mutations, each one alone: delete the sentence that starts ``What an earlier release did``, change the link to the
    release notes to the v0.20.0 notes, or put a paragraph that starts ``What v0.19.0 and v0.19.2 limited`` back.
    """

    flat = " ".join(_text().split())
    assert "This page lists what is limited now in v0.21.0." in flat
    assert (
        "What an earlier release did, and what a later release fixed, is in the [CHANGELOG](../../CHANGELOG.md) and the "
        "[release notes](../release/v0.21.0-release-notes.md)."
    ) in flat
    assert "What v0.19.0 and v0.19.2 limited" not in flat
    assert "What v0.20.0 listed as open" not in flat


def test_the_page_describes_v0210_and_marks_what_is_new_with_the_from_marker() -> None:
    """No sentence is narrated by release, and what is new in v0.21.0 carries the one marker.

    The page used to mark what v0.20.2 changed with ``From v0.20.2`` and to retell ``in v0.20.0 ...``. The page describes
    v0.21.0 now, so neither appears. The one marker is ``From v0.21.0``, and ``v0.21.0`` is named only as the release the
    page describes, never for work still to do: removing the words of a redacted memory from reports, hiding the words of
    archived rows and withholding ``capture_content_hash`` are planned with no release set, and each of the three says so.

    Mutations, each one alone: put ``From v0.20.2,`` or ``in v0.20.0 a derived summary`` back on the page; put
    the marker for the unreleased state of v0.20.2 back; name ``v0.21.0`` beside a task (``planned for v0.21.0``); change one
    ``planned, with no release set`` back to ``planned for v0.21.0``; delete the marker from one of the bullets.
    """

    flat = " ".join(_text().split())
    assert "From v0.20.2" not in flat
    assert not re.search(r"\bin v0\.(19\.\d|20\.0)\b", flat), re.findall(r"\bin v0\.(?:19\.\d|20\.0)\b", flat)
    assert "Unreleased (on main" not in flat
    rest = flat
    for described in (
        "v0.21.0-release-notes.md",
        "limited now in v0.21.0",
        "new in v0.21.0 is marked `From v0.21.0`",
        "Open in v0.21.0,",
        "From v0.21.0,",
        "from v0.21.0,",
    ):
        rest = rest.replace(described, "")
    assert "v0.21" not in rest and "0.22" not in flat
    assert not re.search(r"(?:planned|tracked|scheduled|due) for v0\.\d+\.\d+", flat)
    assert flat.count("planned, with no release set") == 3
    for sentence in (
        "withholding it is planned, with no release set",
        "hiding these words is planned, with no release set",
        "removing it is planned, with no release set",
    ):
        assert sentence in flat, sentence
    assert flat.lower().count("from v0.21.0") >= 5
