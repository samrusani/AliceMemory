"""The known limitations page is a short list of what is limited now, and every link on it lands.

The page used to retell the history of each limit (what v0.19.0 and v0.19.2 did, and what v0.20.0 fixed).
That history is on the dated records (the CHANGELOG and the release notes), so each current limit on the page is a
sentence or two and a link to the page that explains it. These checks keep the page from growing back into a
record and keep its links from rotting.

The tests that pin what the page says about one limit (the query bounds, the local-folder scan, the expiry
bullets and the rest) live next to the other pages that state the same limit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs/alpha/known-limitations.md"

# The page is about 15,060 characters and its longest bullet about 760. The old page was 30,000, with single bullets of
# 4,000 to 6,000, and the first short version was 16,300 with eight bullets of 630 to 1,090 characters and three to
# eight sentences each. The caps sit just above the page as it is, so a limit can be reworded and a new one added, and
# nothing grows back into a record without this test failing. A bullet is one or two sentences, and a closing
# "See ..." pointer to the page that explains it does not count as one.
# Recomputed 2026-10-08: current page length 16,300 plus 20 characters.
MAX_PAGE_CHARS = 16_320
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
    assert len(links) >= 15, "the page lost its links to the pages that explain each limit"
    for link in links:
        if re.match(r"^[a-z]+:", link):
            continue
        target, _, anchor = link.partition("#")
        path = (PAGE.parent / target).resolve() if target else PAGE
        assert path.is_file(), f"{link} does not name a file"
        if anchor:
            assert anchor in _anchors(path), f"{link}: no heading in {path.name} gives the anchor {anchor!r}"


def test_the_pages_the_limits_point_at_are_the_ones_that_explain_them() -> None:
    """The size bounds, the cited sources, the saved quotes, the error codes, the backup guide, the importers and the
    confirm and reject rules each have a link, so a limit is never stated without a way to the full explanation.

    Mutation: delete one of these links from the page.
    """

    text = _text()
    for target in (
        "mcp-tools.md#size-bounds",
        "mcp-tools.md#error-codes",
        "mcp-tools.md#cited-sources",
        "mcp-tools.md#saved-quotes",
        "mcp-tools.md#legacy-tool-surface",
        "backup-and-restore.md",
        "../integrations/importers.md",
        "../security/threat-model.md",
        "../memory-operations-protocol.md#confirm-and-reject-rules",
        "../release/v0.20.0-release-notes.md",
        "../release/v0.19.2-release-notes.md",
        "../../CHANGELOG.md",
    ):
        assert f"]({target})" in text, target


def test_the_page_is_short_and_no_bullet_is_a_record() -> None:
    """The page and each bullet stay under a size that a reader can scan, and a bullet is one or two sentences.

    Mutations, each one alone: put the old 4,000 character query bounds bullet, the old open loop fence paragraph or the
    1,090 character key-bound source bullet back; add two sentences to any bullet; add a 1,000 character paragraph of
    history to the page.
    """

    text = _text()
    assert len(text) <= MAX_PAGE_CHARS, len(text)
    bullets = _bullets(text)
    assert len(bullets) >= 40, "the list of limits was not read"
    for bullet in bullets:
        assert len(bullet) <= MAX_BULLET_CHARS, (len(bullet), bullet[:80])
        assert _sentences(bullet) <= MAX_BULLET_SENTENCES, (_sentences(bullet), bullet[:80])
    # The last bullet is not stretched over the paragraphs after the list: a bullet ends at the blank line.
    assert not any("What v0.19.0 and v0.19.2 limited" in bullet for bullet in bullets)


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
    """The one sentence that sends a reader of an earlier release to the records is on the page.

    Mutation: delete the sentence that starts ``What v0.19.0 and v0.19.2 limited``.
    """

    flat = " ".join(_text().split())
    assert "What v0.19.0 and v0.19.2 limited and v0.20.0 fixed or narrowed" in flat
    assert "[v0.19.2 release notes](../release/v0.19.2-release-notes.md)" in flat
    assert "[CHANGELOG](../../CHANGELOG.md)" in flat
