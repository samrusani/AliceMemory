"""Published release notes only gain dated correction and update paragraphs, and the v0.19.2 update lines say v0.19.2.

``RELEASING.md`` treats a published release note as immutable. The one rewrite it
allows is the state comment on line 2, which moves from ``pending`` to
``published`` once the checksums are recorded. Anything else is a dated
correction or update paragraph, added and never edited in. On 2026-10-01 the notes of
v0.17.0, v0.18.0 and v0.19.0 and the v0.18.0 section of ``CHANGELOG.md`` gained
"is in v0.19.2" update lines under corrections that said a fix was still on main.

Three things are pinned here, by reading the tag with ``git show`` and not by
trusting the working file:

* every published file or section still holds every line it held at its tag,
  in order, with only the state comment allowed to change, so a published
  sentence cannot be reworded, moved or dropped;
* every line a published file or section gained since its tag belongs to a
  paragraph that opens with ``Correction`` or ``Update`` and a date, so a
  correction can be added and an undated sentence cannot. A paragraph is judged
  by its opening, as a reader sees it, so quote markers, bold markers and
  indentation do not count;
* each dated 2026-10-01 update paragraph is present, says what it said, and
  names v0.19.2, so one cannot be dropped or pointed at the wrong release.

The unit-test job checks out with full history so the tags exist. Where they do
not (a shallow clone), the history check skips, and under ``CI`` it fails
instead, so a workflow that stops fetching tags cannot turn the guard off.

Mutations that must fail this file, each alone:

* reword, move or delete any line that a published note had at its tag: the
  added-lines test for that file fails;
* insert a sentence that does not open with a dated ``Correction`` or ``Update``
  (alone on a line, after a paragraph, or in the v0.17.0, v0.18.0 or v0.19.0
  changelog section): the same test fails;
* change the state comment of a published note to anything but
  ``published`` / ``recorded``, or edit its other fields: the same test fails;
* write v0.19.1 in place of v0.19.2 in an update line, or delete an update
  line: the update-line test for that file fails.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = "CHANGELOG.md"
# Changelog sections of published releases, unchanged since their tags except for dated lines.
PUBLISHED_CHANGELOG_SECTIONS = ("v0.17.0", "v0.18.0", "v0.19.0")

PUBLISHED_NOTES = (
    ("v0.17.0", "docs/release/v0.17.0-release-notes.md"),
    ("v0.18.0", "docs/release/v0.18.0-release-notes.md"),
    ("v0.19.0", "docs/release/v0.19.0-release-notes.md"),
)

# The dated 2026-10-01 update paragraphs, in file order, as read after quote
# markers are stripped and lines are joined with single spaces.
UPDATES: dict[tuple[str, str | None], tuple[str, ...]] = {
    ("docs/release/v0.17.0-release-notes.md", None): (
        "Update, 2026-10-01. The import change described in the correction above is in v0.19.2.",
    ),
    ("docs/release/v0.18.0-release-notes.md", None): (
        "**Update (2026-10-01):** Plugin 0.5.2 is in v0.19.2. From v0.19.2,"
        " `POST /v1/memory/operations/commit` applies in `assist` and `auto` mode only a user turn"
        " that matched an explicit prefix, as the capture routes do.",
        "**Update (2026-10-01):** Plugin 0.5.2 is in v0.19.2.",
    ),
    ("docs/release/v0.19.0-release-notes.md", None): (
        "Update, 2026-10-01. Import lists such records and the doctor reads chunk text in v0.19.2.",
    ),
    (CHANGELOG, "v0.18.0"): (
        "**Update, added 2026-10-01.** Plugin 0.5.2 is in v0.19.2.",
        "**Update, added 2026-10-01.** From v0.19.2 that policy reads the role and applies only a user"
        " turn that matched an explicit prefix.",
    ),
}

_STATE_COMMENT = re.compile(r"<!-- alice-release-state: (?P<payload>\{.*\}) -->")
_UPDATE_PARAGRAPH = re.compile(r"^\W*Update\W+(?:added\W+)?2026-10-01\b")
# How a paragraph added to a published file opens: "Correction, 2026-09-30.",
# "**Correction (2026-09-30):**", "Update, 2026-10-01.", "**Update, added 2026-10-01.**".
_DATED_PARAGRAPH = re.compile(r"^\W*(?:Correction|Update)\W+(?:added\W+)?\d{4}-\d{2}-\d{2}\b")


# --- helpers ----------------------------------------------------------------


def match_lines(old: list[str], new: list[str]) -> tuple[set[int], tuple[int, str] | None]:
    """Match ``old`` into ``new`` left to right. Returns (positions used in ``new``, first line not kept).

    The second value is None when ``old`` is a subsequence of ``new``, which is
    when ``new`` only has lines added. A greedy match decides that exactly. The
    positions of ``new`` that are not used are the added lines.
    """

    used: set[int] = set()
    position = 0
    for index, line in enumerate(old):
        while position < len(new) and new[position] != line:
            position += 1
        if position == len(new):
            return used, (index, line)
        used.add(position)
        position += 1
    return used, None


def first_line_not_kept(old: list[str], new: list[str]) -> tuple[int, str] | None:
    """The first line of ``old`` that is missing from ``new`` once order is kept, else None."""

    return match_lines(old, new)[1]


def added_paragraphs(old: list[str], new: list[str]) -> list[str]:
    """The paragraphs made of lines ``new`` has and ``old`` lacks.

    Two runs of added lines with a kept line between them stay two paragraphs, so a
    dated paragraph cannot carry an undated one that sits after a kept line.
    """

    used, _lost = match_lines(old, new)
    lines: list[str] = []
    in_run = False
    for index, line in enumerate(new):
        if index in used:
            if in_run:
                lines.append("")
            in_run = False
        else:
            lines.append(line)
            in_run = True
    return paragraphs("\n".join(lines))


def undated_additions(old: list[str], new: list[str]) -> list[str]:
    """Added paragraphs that do not open with a dated ``Correction`` or ``Update``."""

    return [block for block in added_paragraphs(old, new) if not _DATED_PARAGRAPH.match(block)]


def state_problem(old_line: str, new_line: str) -> str | None:
    """Why the state comment moved in a way ``RELEASING.md`` does not allow, else None.

    It was ``pending``/``pending`` on the tag. It may stay that way or become
    ``published``/``recorded``. Nothing else in it may change.
    """

    old = _STATE_COMMENT.fullmatch(old_line)
    new = _STATE_COMMENT.fullmatch(new_line)
    if old is None or new is None:
        return f"line 2 is not a release state comment: {old_line!r} -> {new_line!r}"
    try:
        before = json.loads(old.group("payload"))
        after = json.loads(new.group("payload"))
    except json.JSONDecodeError as exc:
        return f"the state comment is not valid JSON: {exc}"
    statuses = ("publication_status", "checksums_status")
    if tuple(before.get(key) for key in statuses) != ("pending", "pending"):
        return f"the tag's state comment was not pending/pending: {old_line!r}"
    if {k: v for k, v in before.items() if k not in statuses} != {
        k: v for k, v in after.items() if k not in statuses
    }:
        return f"the state comment changed a field other than its two statuses: {new_line!r}"
    if tuple(after.get(key) for key in statuses) not in {
        ("pending", "pending"),
        ("published", "recorded"),
    }:
        return f"the state comment is not pending/pending or published/recorded: {new_line!r}"
    return None


def paragraphs(text: str) -> list[str]:
    """Blocks of text, quote markers and indentation removed, each joined onto one line."""

    blocks: list[list[str]] = [[]]
    for line in text.splitlines():
        stripped = re.sub(r"^[\s>]+", "", line)
        if stripped:
            blocks[-1].append(stripped)
        else:
            blocks.append([])
    return [" ".join(block) for block in blocks if block]


def update_paragraphs(text: str) -> list[str]:
    return [block for block in paragraphs(text) if _UPDATE_PARAGRAPH.match(block)]


def section(text: str, tag: str) -> str:
    """The ``## vX.Y.Z`` section of a changelog, heading included, up to the next ``## `` heading."""

    lines = text.splitlines()
    start = next(
        index for index, line in enumerate(lines) if re.match(rf"^## {re.escape(tag)} \u2014 ", line)
    )
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _working(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _tagged(tag: str, path: str) -> str:
    done = subprocess.run(
        ["git", "show", f"{tag}:{path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if done.returncode != 0:
        message = f"cannot read {path} at {tag}: {done.stderr.strip()}"
        if os.environ.get("CI"):
            pytest.fail(message + ". The checkout must include tags (fetch-depth: 0).")
        pytest.skip(message)
    return done.stdout


# --- the helpers, on synthetic text ------------------------------------------


def test_first_line_not_kept_accepts_added_lines_and_names_a_changed_or_dropped_one() -> None:
    """Mutation: make the match order-insensitive, or let it skip a changed line."""

    old = ["# Title", "", "alpha", "", "beta"]
    assert first_line_not_kept(old, old) is None
    assert first_line_not_kept(old, ["# Title", "", "alpha", "", "gamma", "", "beta", "tail"]) is None
    assert first_line_not_kept(old, ["# Title", "", "alpha", "", "beta", "", "delta"]) is None
    assert first_line_not_kept(old, ["# Title", "", "alpha, reworded", "", "beta"]) == (2, "alpha")
    assert first_line_not_kept(old, ["# Title", "", "", "beta"]) == (2, "alpha")
    assert first_line_not_kept(old, ["# Title", "", "beta", "", "alpha"]) is not None  # moved
    assert first_line_not_kept(old, ["# Title", "", "alpha", ""]) == (4, "beta")


def test_added_paragraphs_are_the_new_text_split_where_a_kept_line_or_blank_separates_it() -> None:
    """Mutation: drop the separator after a run of added lines. The undated tail joins a dated paragraph."""

    old = ["# Title", "", "alpha", "", "beta"]
    assert added_paragraphs(old, old) == []
    assert added_paragraphs(old, [*old, "", "> Update, 2026-10-01. A wrapped line", "> that goes on."]) == [
        "Update, 2026-10-01. A wrapped line that goes on."
    ]
    # Two paragraphs added together, split by a blank or a bare quote marker.
    assert added_paragraphs(old, [*old, "", "one", "", "two", ">", "> three"]) == ["one", "two", "three"]
    # Two runs with a kept line between them are two paragraphs, not one.
    assert added_paragraphs(
        ["alpha", "beta"], ["alpha", "Update, 2026-10-01. x", "beta", "Tail."]
    ) == ["Update, 2026-10-01. x", "Tail."]
    # A line glued to a kept paragraph, with no blank line, is still an added paragraph.
    assert added_paragraphs(["alpha", "", "beta"], ["alpha", "glued", "", "beta"]) == ["glued"]
    # A copy of a line the file already holds is an addition, whichever copy is matched.
    assert added_paragraphs(["alpha", "beta"], ["alpha", "beta", "alpha"]) == ["alpha"]
    assert added_paragraphs(["alpha", "beta"], ["alpha", "alpha", "beta"]) == ["alpha"]


@pytest.mark.parametrize(
    "opening",
    (
        "Correction, 2026-09-30. Something was wrong.",
        "Update, 2026-10-01. It is in v0.19.2.",
        "**Correction (2026-09-30):** Something was wrong.",
        "**Update (2026-10-01):** It is in v0.19.2.",
        "**Update, added 2026-10-01.** It is in v0.19.2.",
        "  **Correction, added 2026-09-30.** Indented.",
    ),
)
def test_a_dated_correction_or_update_paragraph_may_be_added(opening: str) -> None:
    old = ["# Title", "", "alpha"]
    assert undated_additions(old, [*old, "", opening]) == []
    assert undated_additions(old, [*old, "", "> " + opening.strip(), ">", "> Update, 2026-10-01. Two."]) == []


@pytest.mark.parametrize(
    "sentence",
    (
        "Extra undated sentence.",
        "This release is now also faster.",
        "Note, 2026-10-01. A date alone is not enough.",
        "Update without a date.",
        "Updated 2026-10-01. Wrong word.",
        "Corrections, 2026-09-30. Wrong word.",
        "Update, 2026-10-1. Not a date.",
        "A sentence that says Update, 2026-10-01. later on.",
        "<!-- a hidden note -->",
        "---",
    ),
)
def test_an_undated_or_misdated_added_paragraph_is_reported(sentence: str) -> None:
    """Mutation: let any added line pass, or accept a paragraph that does not open with the marker."""

    old = ["# Title", "", "alpha", "", "beta"]
    assert undated_additions(old, [*old[:3], "", sentence, *old[3:]]) == [sentence]  # between paragraphs
    assert undated_additions(old, [*old, "", sentence]) == [sentence]  # at the end
    assert undated_additions(old, [*old, sentence]) == [sentence]  # glued to the last line
    assert undated_additions(old, ["> " + sentence, *old]) == [sentence]  # quoted, at the top
    assert undated_additions(
        old, [*old, "", "> Update, 2026-10-01. Fine.", "", sentence]
    ) == [sentence]  # after a dated paragraph, split by a blank line


def _state(version: str, publication: str, checksums: str, **extra: str) -> str:
    payload = {
        "schema_version": "alice_release_document_state_v1",
        "version": version,
        "publication_status": publication,
        "checksums_status": checksums,
        **extra,
    }
    return f"<!-- alice-release-state: {json.dumps(payload, separators=(',', ':'))} -->"


def test_state_comment_may_only_move_from_pending_to_published_and_recorded() -> None:
    pending = _state("0.17.0", "pending", "pending")
    assert state_problem(pending, pending) is None
    assert state_problem(pending, _state("0.17.0", "published", "recorded")) is None
    for bad in (
        _state("0.17.0", "published", "pending"),
        _state("0.17.0", "pending", "recorded"),
        _state("0.17.0", "withdrawn", "recorded"),
        _state("0.17.1", "published", "recorded"),
        _state("0.17.0", "published", "recorded", note="added field"),
        "<!-- alice-release-state: {not json} -->",
        "not a state comment",
    ):
        assert state_problem(pending, bad) is not None, bad
    assert state_problem(_state("0.17.0", "published", "recorded"), pending) is not None


def test_update_paragraphs_are_found_through_quote_markers_and_line_wraps() -> None:
    text = (
        "> Correction, 2026-09-30. Something.\n"
        ">\n"
        "> Update, 2026-10-01. A wrapped line that is\n"
        "> in v0.19.2.\n"
        "\n"
        "> **Update (2026-10-01):** Bold form is in v0.19.2.\n"
        "\n"
        "  **Update, added 2026-10-01.** Indented form is in v0.19.2.\n"
        "\n"
        "Update 2026-09-30 is not a match.\n"
    )
    assert update_paragraphs(text) == [
        "Update, 2026-10-01. A wrapped line that is in v0.19.2.",
        "**Update (2026-10-01):** Bold form is in v0.19.2.",
        "**Update, added 2026-10-01.** Indented form is in v0.19.2.",
    ]


def test_section_stops_at_the_next_heading() -> None:
    text = "# Changelog\n\n## v2.0.0 \u2014 2026-01-02\n\n- b\n\n## v1.0.0 \u2014 2026-01-01\n\n- a\n"
    assert section(text, "v1.0.0") == "## v1.0.0 \u2014 2026-01-01\n\n- a"
    assert section(text, "v2.0.0") == "## v2.0.0 \u2014 2026-01-02\n\n- b\n"


def _stop_of(tag: str, path: str) -> type[BaseException]:
    """How ``_tagged`` ends when it cannot read the tag: failed or skipped."""

    try:
        _tagged(tag, path)
    except (pytest.fail.Exception, pytest.skip.Exception) as stop:
        return type(stop)
    raise AssertionError("_tagged read a tag that does not exist")


def test_a_missing_tag_skips_locally_and_fails_under_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: skip under CI too. A workflow that stops fetching tags would turn the guard off."""

    path = PUBLISHED_NOTES[0][1]
    monkeypatch.setenv("CI", "true")
    assert _stop_of("v0.0.0-no-such-tag", path) is pytest.fail.Exception
    monkeypatch.delenv("CI")
    assert _stop_of("v0.0.0-no-such-tag", path) is pytest.skip.Exception


# --- the real files ----------------------------------------------------------


def _check_only_dated_lines_were_added(label: str, tag: str, old: list[str], new: list[str]) -> None:
    lost = first_line_not_kept(old, new)
    if lost is not None:
        pytest.fail(f"{label} no longer holds a line it had at {tag}: {lost[1]!r}")
    undated = undated_additions(old, new)
    assert undated == [], (
        f"{label} gained text since {tag} that is not a dated Correction or Update paragraph: {undated}"
    )


@pytest.mark.parametrize(("tag", "path"), PUBLISHED_NOTES)
def test_a_published_release_note_only_gained_dated_paragraphs_since_its_tag(tag: str, path: str) -> None:
    """Mutation: reword, move or delete a published line, add an undated sentence, or edit the state comment."""

    old = _tagged(tag, path).splitlines()
    new = _working(path).splitlines()
    assert old[0] == new[0] == f"# Alice {tag} Release Notes"
    problem = state_problem(old[1], new[1])
    assert problem is None, f"{path}: {problem}"
    _check_only_dated_lines_were_added(path, tag, [old[0], *old[2:]], [new[0], *new[2:]])


@pytest.mark.parametrize("tag", PUBLISHED_CHANGELOG_SECTIONS)
def test_a_published_changelog_section_only_gained_dated_paragraphs_since_its_tag(tag: str) -> None:
    """Mutation: reword, move or delete a line of the section, or add an undated sentence to it."""

    old = section(_tagged(tag, CHANGELOG), tag).splitlines()
    new = section(_working(CHANGELOG), tag).splitlines()
    _check_only_dated_lines_were_added(f"the {tag} section of {CHANGELOG}", tag, old, new)


@pytest.mark.parametrize(("path", "heading"), sorted(UPDATES, key=lambda key: (key[0], key[1] or "")))
def test_the_dated_update_lines_name_v0192(path: str, heading: str | None) -> None:
    """Mutation: write another version in an update line, edit its wording, or delete it."""

    text = _working(path)
    if heading is not None:
        text = section(text, heading)
    found = update_paragraphs(text)
    assert found == list(UPDATES[(path, heading)])
    for paragraph in found:
        assert "v0.19.2" in paragraph, paragraph
