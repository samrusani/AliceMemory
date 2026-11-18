"""Hold the candidate notes and the published baseline they describe."""

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
BASE = "e50ef44c85c024e8f153a14add8b2d741c2cee70"
PRIOR = "v0.24.0"
NOTES = "docs/release/v0.24.1-release-notes.md"
PRIOR_NOTES = "docs/release/v0.24.0-release-notes.md"
CHANGELOG_HEADING = "## v0.24.1 \u2014 2026-11-18"
DRAFT_MARKERS = ("Unreleased (on " + "main, not in v0.24.0):", "On main (" + "unreleased)")


# For each parameter, delete just the named sentence from its section.
CLAIMS = [
    pytest.param('Lead', 'This is a security release for installs that use agent keys on the PostgreSQL stack.', id='postgres-lead'),
    pytest.param('Lead', 'The SQLite vault behaves as before.', id='sqlite-lead'),
    pytest.param('What to do after upgrading', '**Upgrade the copy your hosts run** (`uv tool upgrade alice-memory` or `pip install -U alice-memory` in the environment they use), check it with `alice-memory --version`, and restart your hosts.', id='upgrade-and-restart'),
    pytest.param('What to do after upgrading', 'Once an active key exists, calls to `/v1` need an unbound `admin_agent` key.', id='operator-key'),
    pytest.param('What to do after upgrading', 'Other keys use `/v0/vnext` or MCP.', id='other-keys'),
    pytest.param('What to do after upgrading', "Set `ALICE_AGENT_API_KEY` in the server's environment once the vault holds an active key.", id='server-key'),
    pytest.param('What to do after upgrading', 'On an install with active keys, a capability capture at `confidential`, `highly_sensitive`, `sacred` or `regulated` is refused.', id='clip-labels'),
    pytest.param('What to do after upgrading', 'If appropriate for the clip, use `private` for capture and have the owner or an unbound admin key raise the label after capture.', id='raise-label'),
    pytest.param('What to do after upgrading', '**No database migration is needed.**', id='no-migration'),
    pytest.param('What to do after upgrading', 'The marketplace on `main` pins v0.24.0 until the change after publication moves it.', id='marketplace'),
    pytest.param('The four changes', 'On a PostgreSQL install with active keys, a valid unbound `admin_agent` key is required.', id='v1-policy'),
    pytest.param('The four changes', 'Other valid keys receive HTTP 403.', id='v1-refusal'),
    pytest.param('The four changes', 'v0.24.0 served any valid key on `/v1`.', id='v1-prior'),
    pytest.param('The four changes', 'A server without `ALICE_AGENT_API_KEY` refuses tool calls, including reads and legacy tools, while an active key exists.', id='mcp-policy'),
    pytest.param('The four changes', 'v0.24.0 served keyless calls while active keys existed, although the [MCP guide](../alpha/mcp-tools.md) said those calls were refused.', id='mcp-prior'),
    pytest.param('The four changes', 'On an install with active keys, a capability capture is held to the strictest sensitivity ceiling among eligible issuers.', id='clip-policy'),
    pytest.param('The four changes', 'A ceiling refusal leaves the capability available for another attempt before expiry.', id='clip-retry'),
    pytest.param('The four changes', 'v0.24.0 applied no sensitivity ceiling to a capture made with a capability, even where the issuing key had a lower ceiling.', id='clip-prior'),
    pytest.param('The four changes', 'Every mode checks that the peer is loopback.', id='legacy-policy'),
    pytest.param('The four changes', 'v0.24.0 checked the peer only outside development and test.', id='legacy-prior'),
    pytest.param('Behaviour changes an integrator will notice', 'On a keyed PostgreSQL install, a valid key that cannot use `/v1` receives HTTP 403, and a keyless MCP tool call that reaches the key check receives `not_permitted`.', id='v1-and-mcp-answers'),
    pytest.param('Behaviour changes an integrator will notice', 'A browser capture refused by the capability ceiling receives HTTP 403 with `sensitivity_above_agent_ceiling`.', id='clip-answer'),
    pytest.param('Behaviour changes an integrator will notice', 'A mounted legacy `/v0` route refuses a non-loopback peer with HTTP 403.', id='legacy-answer'),
    pytest.param('Security', 'This release was checked with automated security scanning and internal adversarial review, findings triaged and fixed.', id='security-review'),
    pytest.param('Security', 'No one outside the project has audited the code.', id='outside-audit'),
    pytest.param('Security', 'On keyed installs the `/v1` check reserves operator access for a valid unbound `admin_agent` key.', id='v1-security'),
    pytest.param('Security', 'The PostgreSQL MCP check closes keyless tool access while a key is active.', id='mcp-security'),
    pytest.param('Security', "The browser capability ceiling prevents a clip from exceeding the strictest eligible issuer's ceiling.", id='clip-security'),
    pytest.param('Security', 'The legacy peer check keeps mounted `/v0` routes local in development and test as well.', id='legacy-security'),
    pytest.param('Security', 'A security advisory for the /v1 change is published with this release; it also names the PostgreSQL MCP change and the browser clip change.', id='advisory'),
    pytest.param('Security', 'The v0.24.0 notes said that no security advisory was planned; that sentence is corrected here.', id='advisory-correction'),
    pytest.param('What this release does not claim', 'Mounted legacy `/v0` routes still serve local callers without a key or label filtering.', id='legacy-limit'),
    pytest.param('What this release does not claim', 'See [known limitations](../alpha/known-limitations.md).', id='legacy-link'),
    pytest.param('What this release does not claim', 'In the supported deployment, `/v1` remains a loopback surface for the operator.', id='operator-limit'),
    pytest.param('What this release does not claim', 'See the [deployment guide](../deployment/single-tenant-self-hosted.md).', id='operator-link'),
    pytest.param('What this release does not claim', 'The strictest eligible issuer ceiling applies to capability captures made on a keyed install even when an admin key issued the capability.', id='admin-clip-limit'),
    pytest.param('What this release does not claim', 'See [agent integration](../alpha/agent-integration.md#authentication).', id='admin-clip-link'),
    pytest.param('Install', 'After publication:', id='after-publication'),
    pytest.param('Install', 'pip install alice-memory==0.24.1 && alice-memory install', id='pip-install'),
    pytest.param('Install', 'With uv: `uvx alice-memory==0.24.1 install`.', id='uv-install'),
]


def _text(name: str, root: Path = ROOT) -> str:
    return (root / name).read_text(encoding="utf-8")


def _git_text(revision: str, name: str) -> str:
    return subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=REPOSITORY, text=True)


@pytest.fixture(scope="module")
def prior_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp(PRIOR)
    export = subprocess.check_output(["git", "archive", PRIOR], cwd=REPOSITORY)
    with tarfile.open(fileobj=BytesIO(export)) as archive:
        archive.extractall(target, filter="data")
    return target


@pytest.mark.parametrize(("section", "sentence"), CLAIMS)
def test_each_release_sentence_is_held(section: str, sentence: str) -> None:
    """Mutation for each parameter: delete that one sentence from its section."""
    notes = _text(NOTES)
    sections = notes.split("\n## ")
    body = sections[0] if section == "Lead" else next(
        part.split("\n", 1)[1] for part in sections[1:] if part.startswith(section + "\n")
    )
    assert sentence in " ".join(body.split())


def test_the_candidate_identity_and_prepublication_state() -> None:
    """Mutation: set the candidate's publication status to published."""
    metadata, issues = release_check.validate_metadata(ROOT)
    assert not issues and metadata.version == "0.24.1"
    assert release_check.validate_release_document_state(ROOT, version=metadata.version, require_finalized=True) == []
    assert _text(NOTES).splitlines()[0] == "# Alice v0.24.1 Release Notes"


def test_the_changelog_moves_the_five_entries_and_preserves_published_sections() -> None:
    """Mutation: change the dated v0.24.1 heading."""
    old_unreleased, old_history = _git_text(BASE, "CHANGELOG.md").split("\n## Unreleased\n", 1)[1].split("\n## ", 1)
    current = _text("CHANGELOG.md")
    assert f"\n## Unreleased\n\n{CHANGELOG_HEADING}\n" in current
    released, history = current.split(CHANGELOG_HEADING + "\n", 1)[1].split("\n## ", 1)
    old_entries = old_unreleased.replace(DRAFT_MARKERS[0] + " ", "")
    expected = re.sub(r"(?m)^- ([a-z])", lambda match: "- " + match.group(1).upper(), old_entries)
    assert len(re.findall(r"(?m)^- ", expected)) == 5
    assert released == expected
    assert history == old_history


def test_no_release_marker_remains_in_documents_code_or_manifest() -> None:
    """Mutation: restore the old opener in the vNext limitations."""
    paths = [path for path in ROOT.glob("*.md") if path.is_file()]
    for folder in ("docs", "plugins", "agent-skills", "eval", "apps/api/src", "tests", "scripts"):
        paths.extend(path for path in (ROOT / folder).rglob("*") if path.suffix in {".md", ".py", ".json"})
    paths.extend((ROOT / "apps/web" / "app").rglob("*.ts*"))
    assert not [
        str(path.relative_to(ROOT))
        for path in paths
        if any(marker in path.read_text(encoding="utf-8") for marker in DRAFT_MARKERS)
    ]


def test_candidate_control_documents_keep_the_published_baseline() -> None:
    """Mutation: drop the current candidate sentence from the README."""
    for path in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "README.md",
                 "RELEASING.md", "ROADMAP.md", "docs/vnext/README.md"):
        assert "`v0.24.1` is the current release candidate. It is not published." in _text(path), path
        assert "`v0.24.0` is the latest published release" in " ".join(_text(path).split()), path


def test_marketplace_and_released_notes_keep_their_published_records(prior_tree: Path) -> None:
    """Mutation: put v0.23.1 back into the working marketplace reference."""
    marketplace = ".claude-plugin/marketplace.json"
    current = json.loads(_text(marketplace))["plugins"][0]["source"]
    at_tag = json.loads(_text(marketplace, prior_tree))["plugins"][0]["source"]
    commit = subprocess.check_output(["git", "rev-parse", f"{PRIOR}^{{commit}}"], cwd=REPOSITORY, text=True).strip()
    assert (current["ref"], current["sha"]) == (PRIOR, commit)
    assert at_tag["ref"] == "v0.23.1"
    assert _text(PRIOR_NOTES) == _git_text(BASE, PRIOR_NOTES)
    assert "No security advisory is planned for this release." in _text(PRIOR_NOTES, prior_tree)


def test_the_three_limits_link_to_the_pages_that_state_them() -> None:
    """Mutation: remove the browser capability ceiling from the agent guide."""
    notes = _text(NOTES)
    for reference in re.findall(r"\]\((\.\.[^)]*)\)", notes):
        relative, _, anchor = reference.partition("#")
        target = ROOT / Path(NOTES).parent / relative
        assert target.is_file(), (relative, anchor)
        if anchor:
            assert re.search(rf"(?m)^#+ {re.escape(anchor)}$", target.read_text(), re.IGNORECASE)
    assert "serve local callers without a key or label filtering" in _text("docs/alpha/known-limitations.md")
    deployment = " ".join(_text("docs/deployment/single-tenant-self-hosted.md").split())
    assert "remain loopback-only operator actions" in deployment
    assert "strictest sensitivity ceiling of any key allowed to issue one" in _text("docs/alpha/agent-integration.md")


def test_prior_mcp_docs_described_the_rule_for_both_backends(prior_tree: Path) -> None:
    """Mutation: look for the old rule in the marketplace file instead of the prior MCP guide."""
    path = "docs/alpha/mcp-tools.md"
    guide = " ".join(_text(path, prior_tree).split())
    assert "`alicebot agent keys create` on Postgres or `alice-memory keys create` on the SQLite vault" in guide
    assert "every keyless call is refused, reads included" in guide
    assert _text(path) == _git_text(BASE, path)


def test_database_schema_is_unchanged(prior_tree: Path, tmp_path: Path) -> None:
    """Mutation: compare the candidate's SQLite schema with an empty vault."""
    migration_directory = Path("apps/api/alembic/versions")
    migrations = {
        path.name: path.read_bytes()
        for path in (prior_tree / migration_directory).glob("*.py")
    }
    assert migrations
    assert migrations == {
        path.name: path.read_bytes()
        for path in (ROOT / migration_directory).glob("*.py")
    }
    probe = """
from pathlib import Path
import json
import sqlite3
import sys
from alicebot_api.onramp import bootstrap_database
path = Path(sys.argv[1])
bootstrap_database(path, user_id="00000000-0000-0000-0000-000000000001", user_email="fixture@example.invalid")
with sqlite3.connect(path) as conn:
    print(json.dumps(conn.execute("SELECT type, name, sql FROM main.sqlite_master ORDER BY type, name").fetchall()))
"""
    prior = _tag_python(prior_tree, tmp_path, probe, str(tmp_path / "prior.db"))
    candidate = _tag_python(ROOT, tmp_path, probe, str(tmp_path / "candidate.db"))
    assert json.loads(prior) == json.loads(candidate)
