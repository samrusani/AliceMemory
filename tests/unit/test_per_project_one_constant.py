"""Per-project memory S2: one constant, and the release-note words (spec 6.4, test 77).

The brief's exclusion reads ``vnext_memory_commit.SENSITIVE_DOMAINS``, the constant that commit confirmation and the
widening gate of the later slices also read, so changing the set later changes all of them together. Four copies of
the set already exist elsewhere in the package, a test keeps three of them equal, and this slice adds none. The
CHANGELOG entry holds the owner's release-note sentence word for word, and the sentence about detection after it.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import alicebot_api.vnext_memory_commit as memory_commit
from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import sensitive_global_exclusion
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.per_project_s2_support import add_memory, context_for, db_path_for
from tests.unit.per_project_view_support import USER_ID, compile_view_brief, project_view

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "apps" / "api" / "src" / "alicebot_api"
FIVE = frozenset({"family", "health", "spiritual", "legal", "financial"})

RELEASE_NOTE_SENTENCE = (
    "Project briefs no longer automatically include global family, health, spiritual, legal or financial material."
)
DETECTION_SENTENCE = (
    "This applies when Alice finds a project for the folder. When no project is found, when detection fails, "
    "or when project scoping is off, the brief searches all memory and still includes them."
)

#: The four set literals of exactly these five names that existed before this slice, by file and the name each is
#: assigned to. ``_VALID_DOMAINS`` in ``vnext_connectors.py`` holds 13 names, contains all five and is not a copy.
EXISTING_SITES = {
    ("vnext_agent_control.py", "RESTRICTED_DOMAINS"),
    ("vnext_memory_commit.py", "SENSITIVE_DOMAINS"),
    ("vnext_model_intelligence.py", "RESTRICTED_DOMAINS"),
    ("vnext_promotion_policy.py", "PROMOTION_RESTRICTED_DOMAINS"),
}


def _literal_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.Tuple, ast.List)):
                continue
            if len(node.elts) != 5 or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
                continue
            if {e.value for e in node.elts} != FIVE:  # type: ignore[attr-defined]
                continue
            owner: ast.AST = node
            name = "<expression>"
            while owner in parents:
                owner = parents[owner]
                if isinstance(owner, (ast.Assign, ast.AnnAssign)):
                    target = owner.targets[0] if isinstance(owner, ast.Assign) else owner.target
                    name = ast.unparse(target)
                    break
            found.add((path.relative_to(SOURCE_ROOT).as_posix(), name))
    return found


def test_no_new_copy_of_the_five_names_was_added() -> None:
    """Mutation: write the five names into ``session_briefing.py`` (or any new module) as a literal.

    A scan of the package for a set, tuple or list literal of exactly these five strings, in any order, finds the four
    sites that exist and nothing else.
    """

    assert _literal_sites() == EXISTING_SITES


def test_the_brief_reads_the_constant_on_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: copy the five names into ``session_briefing.py``, or read the constant once at import.

    A test that adds a domain to the constant changes the brief: a global ``personal`` fact is shown today and held
    back after the change. The exclusion helper returns exactly the constant for the project view and nothing for any
    other view.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="fact.personal", text="A global personal fact qzpersonalnote", domain="personal")
        add_memory(store, key="fact.plain", text="A global plain fact qzplainnote")
    assert sensitive_global_exclusion(project_view()) == FIVE
    assert "qzpersonalnote" in compile_view_brief(data_dir, project_view())
    monkeypatch.setattr(memory_commit, "SENSITIVE_DOMAINS", set(memory_commit.SENSITIVE_DOMAINS) | {"personal"})
    assert sensitive_global_exclusion(project_view()) == FIVE | {"personal"}
    changed = compile_view_brief(data_dir, project_view())
    assert "qzpersonalnote" not in changed
    assert "qzplainnote" in changed
    for view in (ProjectView.unscoped(), ProjectView.unscoped("failed"), project_view(choice="all"), project_view(choice="global")):
        assert sensitive_global_exclusion(view) == frozenset()


def _unreleased_section() -> str:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## Unreleased")
    end = text.index("\n## v", start)
    return text[start:end]


def test_the_changelog_carries_the_release_note_sentence_word_for_word_and_the_detection_sentence_after_it() -> None:
    """Mutation: reword the sentence, or move the detection sentence away from it.

    The first sentence is the owner's, as ruled. The sentence about detection follows it directly, because the rule is
    keyed to a detected project and the first sentence alone would say more than the rule does.
    """

    unreleased = _unreleased_section()
    assert unreleased.count(RELEASE_NOTE_SENTENCE) == 1
    index = unreleased.index(RELEASE_NOTE_SENTENCE)
    following = unreleased[index + len(RELEASE_NOTE_SENTENCE) :]
    assert following.startswith(" " + DETECTION_SENTENCE)


def test_the_docs_carry_the_same_two_sentences_together() -> None:
    """Mutation: drop the detection sentence from the docs page, or reword the first one."""

    page = (REPO_ROOT / "docs" / "alpha" / "projects.md").read_text(encoding="utf-8")
    assert (RELEASE_NOTE_SENTENCE + " " + DETECTION_SENTENCE) in page
