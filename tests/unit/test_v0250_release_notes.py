"""Hold each v0.25.0 release claim in its section and check the release records."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts import release_check
from tests.unit.test_v0240_release_notes import _tag_python

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = Path(os.environ.get("BENCH_TEST_SOURCE", ROOT))
NOTES = "docs/release/v0.25.0-release-notes.md"
HEADING = "## v0.25.0 \u2014 2026-11-24"
BASE = "2ee56079d680cc6fc7802104288a1db8a86d3142"
PRIOR = "v0.24.1"
MARKER = "Unreleased (on " + "main, not in v0.24.1):"
ADVISORY_SENTENCE = "A security advisory for the correction change and the charter change is published with this release."


def _text(name: str, root: Path = ROOT) -> str:
    return (root / name).read_text(encoding="utf-8")


def _git_text(revision: str, name: str) -> str:
    return subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=REPOSITORY, text=True)


def _sections() -> dict[str, str]:
    lead, *parts = re.split(r"^## (.+)\n", _text(NOTES), flags=re.MULTILINE)
    return {"Lead": lead.split("\n\n", 1)[1], **dict(zip(parts[::2], parts[1::2], strict=True))}


# Mutation for each parameter: delete its sentence from the specified section.
CLAIMS = [
    pytest.param('Lead',
                 'An agent that shares a lesson now hears what became of it and sees the closest published lessons when it writes one.',
                 id='lesson-lead'),
    pytest.param('Lead',
                 'This is also a security release for installs that use agent keys on the PostgreSQL stack.',
                 id='security-lead'),
    pytest.param('What to do after upgrading',
                 'Upgrade the copy your hosts run with `uv tool upgrade alice-memory` or `pip install -U alice-memory`, check `alice-memory --version`, and restart the hosts.',
                 id='restart-hosts'),
    pytest.param('What to do after upgrading',
                 'No database migration is needed.',
                 id='no-migration'),
    pytest.param('What to do after upgrading',
                 "If your trusted key used to change the brain charter's promotion policy, lower its sensitivity, or correct a review candidate or a `private_only` memory, use the owner or an unbound `admin_agent` key.",
                 id='switch-review-key'),
    pytest.param('What to do after upgrading',
                 'A writer can still correct its own waiting confirmation within the confirmation window.',
                 id='waiting-writer-window'),
    pytest.param('What to do after upgrading',
                 "To change a stored source's sensitivity or a connector's default sensitivity, use the owner or an unbound `admin_agent` key.",
                 id='switch-label-key'),
    pytest.param('What to do after upgrading',
                 "Keep creates within your key's sensitivity ceiling, or use an authorized key with a ceiling that covers the requested label.",
                 id='keep-within-ceiling'),
    pytest.param('What to do after upgrading',
                 'If your integration reads `alice_lessons`, a lesson commit or a waiting review, handle the new `yours`, `lesson` and `similar` fields where they apply.',
                 id='read-new-fields'),
    pytest.param('What to do after upgrading',
                 'A key that is neither the writer nor a decider no longer sees a waiting lesson.',
                 id='waiting-visibility'),
    pytest.param('What to do after upgrading',
                 'If you run the web console from a checkout, run `pnpm install --frozen-lockfile` in `apps/web`.',
                 id='reinstall-web'),
    pytest.param('What to do after upgrading',
                 'The Claude Code marketplace pin moves to v0.25.0 in the post-publication change.',
                 id='marketplace'),
    pytest.param('Lessons',
                 'A keyed writer can use `alice_lessons` to see how many readable lessons are waiting and how many were published or closed without publication in the last seven days.',
                 id='writer-counts'),
    pytest.param('Lessons',
                 'It shows up to three recent unpublished decisions with their reasons when available.',
                 id='writer-reasons'),
    pytest.param('Lessons',
                 'v0.24.1 returned published matches without a writer outcome summary.',
                 id='prior-outcomes'),
    pytest.param('Lessons',
                 'An MCP lesson commit and its waiting review show up to three readable published lessons, with words, paths or vectors as the reason for each match.',
                 id='closest-matches'),
    pytest.param('Lessons',
                 "Review lists also show the waiting draft's fields and compute matches for the first ten waiting lessons.",
                 id='review-draft'),
    pytest.param('Lessons',
                 'v0.24.1 returned the commit receipt and review queue without similar lessons.',
                 id='prior-closest'),
    pytest.param('Lessons',
                 'Waiting, private-only and rejected lessons and their decision reasons are visible to their writers and authorized deciders.',
                 id='unpublished-standing'),
    pytest.param('Lessons',
                 'Other callers get the missing-item answer or the existing idempotency conflict.',
                 id='missing-or-conflict'),
    pytest.param('Lessons',
                 "v0.24.1 let other readers within the lesson's read limits see waiting or rejected lessons in review and audit.",
                 id='prior-visibility'),
    pytest.param('Lessons',
                 'Use `alice_memory_correct` to decide a waiting lesson.',
                 id='decide-lesson'),
    pytest.param('Lessons',
                 'See [shared lessons](../alpha/shared-lessons.md#review-and-retire).',
                 id='lesson-guide'),
    pytest.param('Keys and permissions',
                 "Changing the brain charter's promotion policy or lowering its sensitivity requires memory review standing.",
                 id='charter-standing'),
    pytest.param('Keys and permissions',
                 'An admitted caller without that standing keeps the stored promotion policy by omitting it or passing null.',
                 id='preserve-charter-policy'),
    pytest.param('Keys and permissions',
                 'A replacement promotion setting that its reader rejects is refused.',
                 id='invalid-promotion'),
    pytest.param('Keys and permissions',
                 'v0.24.1 let admitted callers replace promotion settings and lower sensitivity, including saving promotion settings the reader refused.',
                 id='prior-charter'),
    pytest.param('Keys and permissions',
                 'Correcting a review candidate, a `private_only` memory or a `needs_review` memory without a pending confirmation requires review standing.',
                 id='correction-standing'),
    pytest.param('Keys and permissions',
                 'A caller with review standing can correct a waiting confirmation at any time, and the writer can correct it within the confirmation window.',
                 id='waiting-correction'),
    pytest.param('Keys and permissions',
                 'A successful correction resolves the confirmation record, and missing, non-text, timezone-free or unreadable deadlines count as expired for confirming and correcting.',
                 id='correction-deadline'),
    pytest.param('Keys and permissions',
                 'v0.24.1 let corrections activate waiting rows without these standing checks and left the confirmation record pending.',
                 id='prior-correction'),
    pytest.param('Keys and permissions',
                 'Project creation, brain charter saves, source reviews and artifact exports record the acting agent.',
                 id='write-attribution'),
    pytest.param('Keys and permissions',
                 "Agent exports stay inside the server's artifact export root.",
                 id='export-root'),
    pytest.param('Keys and permissions',
                 'v0.24.1 recorded these writes as owner or system actions and accepted agent export destinations outside that root.',
                 id='prior-attribution'),
    pytest.param('Keys and permissions',
                 "HTTP memory proposals, open loops, queue tasks, projects and brain charter writes apply the caller's sensitivity ceiling.",
                 id='http-ceiling'),
    pytest.param('Keys and permissions',
                 'The proposal CLI and legacy proposal and queue tools apply the same rule.',
                 id='cli-mcp-ceiling'),
    pytest.param('Keys and permissions',
                 'v0.24.1 let admitted callers make these writes above their ceilings.',
                 id='prior-ceiling'),
    pytest.param('Keys and permissions',
                 "Changing a source's stored sensitivity or a connector's default sensitivity requires the owner or an unbound `admin_agent` key.",
                 id='label-standing'),
    pytest.param('Keys and permissions',
                 'A trusted operator key can still review a readable source or save connector settings at the existing sensitivity.',
                 id='preserve-operator-writes'),
    pytest.param('Keys and permissions',
                 'v0.24.1 let trusted operator keys make both sensitivity changes.',
                 id='prior-label'),
    pytest.param('Reads',
                 'PostgreSQL avoids repeated JIT compilation on scoped reads with a project-bound key and on `alice_resume` and `alice_context_pack` with an unbound `admin_agent` key.',
                 id='postgres-jit'),
    pytest.param('Reads',
                 'v0.24.1 turned JIT off on the workspace route and used the server default for those other requests.',
                 id='prior-jit'),
    pytest.param('The web console and its checks',
                 'The web console updates Next.js and `eslint-config-next` from 16.4.1 to 16.4.5.',
                 id='next-versions'),
    pytest.param('The web console and its checks',
                 'The full web dependency check drops the `picomatch` exception after the withdrawal of GHSA-fc5q-39xc-vjpf on 2026-11-18.',
                 id='withdrawal'),
    pytest.param('The web console and its checks',
                 'v0.24.1 listed that exception with an expiry of 2026-12-17.',
                 id='prior-exception'),
    pytest.param('The web console and its checks',
                 'The PyPI package does not include the web console.',
                 id='no-console-in-package'),
    pytest.param('Behaviour changes an integrator will notice',
                 '`alice_lessons` returns `yours` when the keyed writer has a readable waiting lesson or a decision in the last seven days.',
                 id='conditional-yours'),
    pytest.param('Behaviour changes an integrator will notice',
                 'The block includes `waiting`, `published_in_last_7_days` and `not_published_in_last_7_days`, with optional `not_published` entries containing `id`, `applies_when`, `reason` and `at`.',
                 id='outcome-fields'),
    pytest.param('Behaviour changes an integrator will notice',
                 "A successful MCP lesson commit and a waiting review item can carry `similar`, with each result's match described in `why`.",
                 id='similar-field'),
    pytest.param('Behaviour changes an integrator will notice',
                 "A waiting lesson's review list item also carries its `lesson` block.",
                 id='review-lesson-field'),
    pytest.param('Behaviour changes an integrator will notice',
                 'An empty `similar` list means no matches, while null means the matches were unavailable or were skipped for that list item.',
                 id='empty-or-null'),
    pytest.param('Behaviour changes an integrator will notice',
                 'A caller without standing to read an unpublished lesson receives the missing-item answer on review or explain, and a hidden idempotent replay retains the conflict answer.',
                 id='hidden-review-replay'),
    pytest.param('Security',
                 'Charter promotion changes and sensitivity reductions require review standing, which v0.24.1 did not require for admitted charter saves.',
                 id='charter-security'),
    pytest.param('Security',
                 "Review standing protects candidate and private-only corrections, and a writer's pending confirmation is limited by its deadline.",
                 id='correction-security'),
    pytest.param('Security',
                 'v0.24.1 did not apply these correction checks to waiting rows.',
                 id='prior-correction-security'),
    pytest.param('Security',
                 "Keyed operator writes identify the agent and keyed artifact exports stay under the server's export root.",
                 id='attribution-security'),
    pytest.param('Security',
                 'v0.24.1 could attribute these writes to the owner or system and allowed agent destinations outside the root.',
                 id='prior-attribution-security'),
    pytest.param('Security',
                 "The applicable create and save surfaces enforce the key's sensitivity ceiling, which v0.24.1 did not enforce on those writes.",
                 id='ceiling-security'),
    pytest.param('Security',
                 'The owner or an unbound admin key controls changes to stored source and connector default sensitivity, which v0.24.1 allowed trusted operator keys to change.',
                 id='label-security'),
    pytest.param('Security',
                 'Next.js 16.4.5 fixes seven advisories that apply to the web console in v0.24.1: GHSA-mfr8-8qww-v76h, GHSA-vc7v-qv4w-wrv8, GHSA-7qx7-33qx-rqx2, GHSA-f3mh-rxg3-f5g2, GHSA-pc58-hwjq-p385, GHSA-rhh4-g77r-r9qj and GHSA-vvgp-ww4w-46vf.',
                 id='next-advisory-ids'),
    pytest.param('What this release does not claim',
                 'A lesson written before the first key keeps its standing after keys are issued.',
                 id='pre-key-standing'),
    pytest.param('What this release does not claim',
                 'See the [known limitations](../alpha/known-limitations.md).',
                 id='limitations-link'),
    pytest.param('What this release does not claim',
                 'A reader sees the writer and publishing agent ids, so choose neutral ids.',
                 id='public-agent-ids'),
    pytest.param('What this release does not claim',
                 'See [shared lessons](../alpha/shared-lessons.md).',
                 id='ids-link'),
    pytest.param('What this release does not claim',
                 '`alice_lessons` reads a projection of readable published lessons, and its cost grows with that number.',
                 id='lookup-cost'),
    pytest.param('What this release does not claim',
                 'See the [lookup limits](../alpha/known-limitations.md).',
                 id='lookup-limit-link'),
    pytest.param('Install',
                 'pip install alice-memory==0.25.0 && alice-memory install',
                 id='pip-install'),
    pytest.param('Install',
                 'With uv, run `uvx alice-memory==0.25.0 install`.',
                 id='uv-install'),
]


@pytest.mark.parametrize(("section", "sentence"), CLAIMS)
def test_each_release_sentence_is_held(section: str, sentence: str) -> None:
    assert sentence in " ".join(_sections()[section].split())


def test_the_notes_follow_the_release_section_order() -> None:
    """Mutation: rename the Security heading."""
    assert list(_sections()) == [
        "Lead", "What to do after upgrading", "Lessons", "Keys and permissions", "Reads",
        "The web console and its checks", "Behaviour changes an integrator will notice", "Security",
        "What this release does not claim", "Install",
    ]


def test_the_security_section_uses_the_review_disclosure_and_advisory_text() -> None:
    """Mutation: change any one of the three opening sentences or the advisory sentence."""
    section = " ".join(_sections()["Security"].split())
    assert section.startswith(
        "This release was checked with automated security scanning and internal adversarial review. "
        "A second internal review is still being worked through: the fixes this release carries are named below, "
        "and some of its findings are not fixed yet. No one outside the project has audited the code."
    )
    assert section.endswith(ADVISORY_SENTENCE)


def test_candidate_versions_and_release_state_agree() -> None:
    """Mutation: restore the old version in apps/web/package.json."""
    metadata, issues = release_check.validate_metadata(ROOT)
    assert issues == []
    assert metadata.version == "0.25.0"
    assert release_check.validate_release_document_state(ROOT, version=metadata.version, require_finalized=True) == []


def test_the_candidate_keeps_the_published_marketplace_pin() -> None:
    """Mutation: change the marketplace ref from v0.24.1 to v0.25.0."""
    current = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    published_commit = subprocess.check_output(
        ["git", "rev-parse", f"{PRIOR}^{{commit}}"], cwd=REPOSITORY, text=True,
    ).strip()
    assert (current["ref"], current["sha"]) == (PRIOR, published_commit)


def test_the_changelog_moves_eleven_entries_and_preserves_history() -> None:
    """Mutation: remove the date from the candidate changelog heading."""
    old_entries, history = _git_text(BASE, "CHANGELOG.md").split("\n## Unreleased\n", 1)[1].split("\n## ", 1)
    expected = old_entries.replace(MARKER + " ", "").replace(" (`git diff --name-only HEAD^ HEAD`)", "")
    expected = re.sub(r"(?m)^- ([a-z])", lambda match: "- " + match[1].upper(), expected)
    current = _text("CHANGELOG.md")
    assert current.startswith(f"# Changelog\n\n## Unreleased\n\n{HEADING}\n")
    released, current_history = current.split(HEADING + "\n", 1)[1].split("\n## ", 1)
    assert released == expected
    assert len(re.findall(r"(?m)^- ", released)) == 11
    assert current_history == history



def test_the_released_marker_is_absent_from_documents_code_and_manifest() -> None:
    """Mutation: treat the released wording as the marker and scan the same files."""
    marker = MARKER.encode()
    if (ROOT / ".git").exists():
        names = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
        )
        files = (ROOT / os.fsdecode(name) for name in names.split(b"\0") if name)
    else:
        files = ROOT.rglob("*")
    found = [str(path.relative_to(ROOT)) for path in files if path.is_file() and marker in path.read_bytes()]
    assert found == []


@pytest.fixture(scope="module")
def previous_release(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("v0241-export")
    data = subprocess.check_output(["git", "archive", PRIOR], cwd=REPOSITORY)
    with tarfile.open(fileobj=BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")
    return destination


def test_database_schema_matches_the_previous_release(previous_release: Path, tmp_path: Path) -> None:
    """Mutation: read an empty SQLite schema in place of the new vault."""
    def migrations(tree: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in (tree / "apps/api/alembic/versions").glob("*.py")}

    assert migrations(previous_release) == migrations(ROOT)
    probe = """
from pathlib import Path
import json
import sqlite3
import sys
from alicebot_api.onramp import bootstrap_database
path = Path(sys.argv[1])
if sys.argv[2] == "bootstrap":
    bootstrap_database(path, user_id="00000000-0000-0000-0000-000000000001", user_email="fixture@example.invalid")
with sqlite3.connect(path) as conn:
    print(json.dumps(conn.execute("SELECT type, name, sql FROM main.sqlite_master ORDER BY type, name").fetchall()))
"""
    prior = _tag_python(previous_release, tmp_path, probe, str(tmp_path / "prior.db"), "bootstrap")
    candidate = _tag_python(ROOT, tmp_path, probe, str(tmp_path / "candidate.db"), "bootstrap")
    assert json.loads(prior) == json.loads(candidate)


def test_a_lesson_writer_gets_new_fields_on_the_same_workflow(previous_release: Path, tmp_path: Path) -> None:
    """Mutation: take the candidate's workflow answer from the previous release."""
    probe = """
import json
import sys
from pathlib import Path
import pytest
from tests.unit.test_lessons_read import LESSON, Vault

vault = Vault(Path(sys.argv[1]))
with pytest.MonkeyPatch.context() as env:
    env.setenv("ALICE_PROJECT_DIR", str(vault.path.parent))
    publisher = vault.key("publisher", "admin_agent", "alpha")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    published = vault.commit(env, publisher, "publisher", sensitivity="private")["memory"]["id"]
    waiting = vault.commit(env, writer, "writer", lesson={**LESSON, "what_happened": "A later conflict hid a change."},
                           sensitivity="private")
    review = vault.call(env, writer, "alice_memory_review", {"status": "pending_review"})["items"][0]
    lookup = vault.call(env, writer, "alice_lessons", {"task": LESSON["applies_when"], "paths": LESSON["paths"]})
    print(json.dumps({
        "found_published": published in [row["id"] for row in lookup["lessons"]],
        "commit_matches": [row["id"] for row in waiting.get("similar", [])] == [published],
        "review_matches": [row["id"] for row in review.get("similar", [])] == [published],
        "review_draft": "lesson" in review,
        "writer_outcomes": lookup.get("yours"),
    }))
"""
    prior = _tag_python(previous_release, tmp_path, probe, str(tmp_path / "prior.db"))
    candidate = _tag_python(ROOT, tmp_path, probe, str(tmp_path / "candidate.db"))
    assert json.loads(prior) == {
        "found_published": True, "commit_matches": False, "review_matches": False,
        "review_draft": False, "writer_outcomes": None,
    }
    assert json.loads(candidate) == {
        "found_published": True, "commit_matches": True, "review_matches": True,
        "review_draft": True, "writer_outcomes": {
            "waiting": 1, "published_in_last_7_days": 0, "not_published_in_last_7_days": 0,
        },
    }


def test_control_documents_match_the_candidate_and_published_baseline() -> None:
    """Mutation: remove the candidate statement from the README."""
    from scripts.check_control_doc_truth import run_control_doc_truth_check

    assert run_control_doc_truth_check(root_dir=ROOT) == []
