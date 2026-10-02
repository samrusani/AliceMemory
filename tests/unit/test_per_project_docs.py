"""The per-project docs mark what only main does, and name the one input that differs with scoping off.

v0.20.0 is released, so what it did stays as it was. Text that describes the project brief, ``alice_resume``
following the project and the ``~global`` refusal is true of main and not of v0.20.0, so each paragraph that says it
carries ``Unreleased (on main, not in v0.20.0)`` until the release PR converts the markers. A marker nothing pins
can be deleted with every test still green, and then the page says v0.20.0 already does it.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0)"
ENTRY_START = "- Per-project memory can be tried on main, **off by default**."
DOCS = ("docs/alpha/projects.md", "docs/alpha/mcp-tools.md")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _unreleased() -> str:
    changelog = _read("CHANGELOG.md")
    return changelog[changelog.index("## Unreleased") : changelog.index("## v0.20.0")]


def _entry() -> str:
    entries = [item for item in _unreleased().split("\n- ") if ("- " + item).startswith(ENTRY_START)]
    assert len(entries) == 1
    return _flat(entries[0])


def test_the_projects_page_marks_its_opening_paragraph_and_the_sentence_that_covers_the_rest() -> None:
    """Mutation: delete the marker from the first paragraph of ``docs/alpha/projects.md``, or from the sentence
    that begins ``Every statement below``.

    The first paragraph says the brief and ``alice_resume`` follow the project, which v0.20.0 does not do. The
    sentence after it says the same of everything on the page about the commands, the switch and the brief.
    """

    text = _read("docs/alpha/projects.md")
    opening = text.split("\n\n")[1]
    assert opening.startswith(MARK + ": Alice can work out which git repository a folder belongs to"), opening[:120]
    assert (
        "Every statement below about the project commands, the switch and the project brief is " + MARK + "."
        in _flat(text)
    )


def test_the_resume_bullet_of_the_tools_page_marks_the_per_project_paragraph() -> None:
    """Mutation: delete the marker from the ``alice_resume`` bullet of ``docs/alpha/mcp-tools.md``, or add a second one
    to that bullet.

    The bullet describes v0.20.0 first, then, after the marker, what main does with scoping on. The marker sits once
    in that bullet. Other bullets of the page carry their own markers (the commit limit does) and their own tests, so
    the count is the bullet's, not the page's.
    """

    text = _read("docs/alpha/mcp-tools.md")
    start = text.index("- `alice_resume`")
    bullet = _flat(text[start : text.index("\n\n", start)])
    assert MARK + ": with per-project scoping on, on the SQLite server, a call that names no project reads" in bullet
    assert bullet.count(MARK) == 1


def test_the_docs_and_the_changelog_name_the_one_input_that_differs_with_scoping_off() -> None:
    """Mutation: delete the sentence that says ``~global`` is refused with scoping off from the changelog entry,
    from the opening paragraph of ``docs/alpha/projects.md``, from its ``What does not change`` list or from the
    ``alice_resume`` bullet.

    With scoping off every output is what v0.20.0 printed except for one input: ``~global`` is reserved now and
    every tool refuses it, where v0.20.0 read it as an ordinary project name. A claim of byte for byte parity
    that left this out would be false for a project literally named ``~global``.
    """

    entry = _entry()
    assert "with scoping off as well" in entry
    assert "with one exception: the refusal of `~global` as a project name applies with scoping off too" in entry
    assert "In v0.20.0 `~global` was an ordinary project name, so a project literally named `~global` worked" in entry
    projects = _flat(_read("docs/alpha/projects.md"))
    assert "with one exception: the name `~global` is reserved now and every tool refuses it as a project name even with scoping off" in projects
    assert "it is the one input whose answer differs from v0.20.0 while scoping is off" in projects
    tools = _flat(_read("docs/alpha/mcp-tools.md"))
    assert "except that `~global` is now a reserved name that every call refuses as a project" in tools


def test_the_changelog_entry_is_one_unreleased_bullet_with_the_held_back_sentences() -> None:
    """Mutation: move the entry under the v0.20.0 heading, add a second entry that starts the same way, or delete
    the three sentences about held-back global material.

    The brief and ``alice_resume`` leave out global notes in the five sensitive domains in a project, say when that
    holds and when it does not, and the entry states what v0.20.0 did for comparison.
    """

    assert _read("CHANGELOG.md").count(ENTRY_START) == 1
    assert ENTRY_START in _unreleased()
    entry = _entry()
    for needle in (
        "Project briefs no longer automatically include global family, health, spiritual, legal or financial material.",
        "This applies when Alice finds a project for the folder.",
        "When no project is found, when detection fails, or when project scoping is off, the brief searches all memory and still includes them.",
        "In v0.20.0 the hook discarded its stdin",
    ):
        assert needle in entry, needle


def test_the_cost_text_states_the_heavily_scoped_vault_as_well_as_the_light_one() -> None:
    """Mutation: delete the 85 percent rows of the table in ``docs/alpha/projects.md``, or the 85 percent
    figures from the changelog entry, or the sentence that leaves the 50,000-note decision open.

    A note that carries a project id costs one Python call in the single-scan read, so the cost of the project view
    depends on how many notes carry one. The budget was priced with 15 percent. Once scoping is on every new note
    carries an id, so the cost that matters is the 85 percent one, and a page that gave only the light figure would
    be easy to over-read. The figures come from ``scripts/measure_project_view.py`` (build with ``--scoped-share``).
    """

    page = _flat(_read("docs/alpha/projects.md"))
    for row in (
        "| 5,000 notes, 15 percent with an id | 68 ms | 81 ms | 13 ms | 100 ms |",
        "| 5,000 notes, 85 percent with an id | 71 ms | 126 ms | 55 ms | 100 ms |",
        "| 50,000 notes, 15 percent with an id | 577 ms | 744 ms | 167 ms | 150 ms |",
        "| 50,000 notes, 85 percent with an id | 616 ms | 1,145 ms | 530 ms | 150 ms |",
        "Whether to accept the 50,000-note figure, or to build a cheaper shape, is a decision for before scoping turns on by default",
    ):
        assert row in page, row
    entry = _entry()
    for needle in (
        "81 ms in a project against 68 ms unscoped, about 13 ms more",
        "744 ms against 577 ms, about 167 ms more, which is 17 ms over the 150 ms budget",
        "the same figures are 126 ms against 71 ms (55 ms more) and 1,145 ms against 616 ms (530 ms more, about 380 ms over the budget)",
    ):
        assert needle in entry, needle
    script = _read("scripts/measure_project_view.py")
    assert '"--scoped-share"' in script and "0.85" in script


def test_the_new_per_project_text_has_no_en_or_em_dashes() -> None:
    """Mutation: add an en or em dash to the entry, to the projects page or to the marked ``alice_resume`` paragraph."""

    tools = _flat(_read(DOCS[1]))
    marked_paragraph = tools[tools.index(MARK) : tools.index("See [Projects](projects.md).")]
    for text in (_entry(), _read(DOCS[0]), marked_paragraph):
        assert "\u2014" not in text
        assert "\u2013" not in text
