"""Pin v0.24.1 facts to its tag and check the post-publication records."""

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
RELEASE = "v0.24.1"
TAG_COMMIT = "6718d7c006c2d64b4ca108a9a84b4eeff59b4709"
PRIOR_COMMIT = "b2f280e4740c02f803783e932a4711080dc39585"
CHECKSUMS = "docs/release/v0.24.1-checksums.txt"
ADVISORY = "[GHSA-345x-53v6-432p](https://github.com/samrusani/AliceMemory/security/advisories/GHSA-345x-53v6-432p)"
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
    pytest.param('Security', "The browser capability ceiling refuses a capture above the strictest eligible issuer's ceiling.", id='clip-security'),
    pytest.param('Security', 'The legacy peer check keeps mounted `/v0` routes local in development and test as well.', id='legacy-security'),
    pytest.param('Security', 'A security advisory for the `/v1` change is published with this release; it also names the PostgreSQL MCP change and the browser clip change.', id='advisory'),
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
def release_trees(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    trees = []
    for tag in (PRIOR, RELEASE):
        target = tmp_path_factory.mktemp(tag)
        export = subprocess.check_output(["git", "archive", tag], cwd=REPOSITORY)
        with tarfile.open(fileobj=BytesIO(export)) as archive:
            archive.extractall(target, filter="data")
        trees.append(target)
    return trees[0], trees[1]


@pytest.mark.parametrize(("section", "sentence"), CLAIMS)
def test_each_release_sentence_is_held(section: str, sentence: str) -> None:
    """Mutation for each parameter: delete that one sentence from its section."""
    notes = _text(NOTES)
    sections = notes.split("\n## ")
    body = sections[0] if section == "Lead" else next(
        part.split("\n", 1)[1] for part in sections[1:] if part.startswith(section + "\n")
    )
    assert sentence in " ".join(body.split())


def test_the_tag_has_the_six_version_sites_and_the_finalized_release_documents(release_trees: tuple[Path, Path]) -> None:
    """Mutation: validate the v0.24.0 export in place of the v0.24.1 export."""
    _, released = release_trees
    metadata, issues = release_check.validate_metadata(released)
    assert not issues and metadata.version == "0.24.1"
    assert release_check.validate_release_document_state(released, version=metadata.version, require_finalized=True) == []
    assert _text(NOTES, released).splitlines()[0] == "# Alice v0.24.1 Release Notes"


def test_the_notes_record_publication_and_checksums() -> None:
    """Mutation: set publication_status back to pending in the published notes."""
    lines = _text(NOTES).splitlines()
    assert lines[0] == "# Alice v0.24.1 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.24.1",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (ROOT / CHECKSUMS).is_file()


def test_the_checksums_file_records_both_published_artifacts() -> None:
    """Mutation: change one hex digit in the wheel record."""
    prior = _text("docs/release/v0.24.0-checksums.txt").splitlines(keepends=True)
    current = _text(CHECKSUMS).splitlines(keepends=True)
    assert len(prior) == len(current) == 19
    assert [line.replace("0.24.0", "0.24.1") for line in prior[:17]] == current[:17]
    assert current[17:] == [
        "2aff1cc7fa40387181de7e2c3f4e38aab00499bdabbae5ea247d3a3b81757123  alice_memory-0.24.1-py3-none-any.whl\n",
        "b064991767e735cd6c64ade99f5ae02aff8d70a26008a28d8bc1a400991013c5  alice_memory-0.24.1.tar.gz\n",
    ]


def test_the_changelog_moves_the_five_entries_and_preserves_published_sections() -> None:
    """Mutation: change the dated v0.24.1 heading in the working changelog."""
    old_unreleased, old_history = _git_text(BASE, "CHANGELOG.md").split("\n## Unreleased\n", 1)[1].split("\n## ", 1)
    tagged = _git_text(RELEASE, "CHANGELOG.md")
    assert f"\n## Unreleased\n\n{CHANGELOG_HEADING}\n" in tagged
    released, history = tagged.split(CHANGELOG_HEADING + "\n", 1)[1].split("\n## ", 1)
    old_entries = old_unreleased.replace(DRAFT_MARKERS[0] + " ", "")
    old_entries = old_entries.replace(" and the release-tag dispatch probe in this pull request", "", 1)
    expected = re.sub(r"(?m)^- ([a-z])", lambda match: "- " + match.group(1).upper(), old_entries)
    assert len(re.findall(r"(?m)^- ", expected)) == 5
    assert released == expected
    assert history == old_history
    assert _text("CHANGELOG.md").split(CHANGELOG_HEADING + "\n", 1)[1].split("\n## ", 1)[0] == released


def test_no_release_marker_remains_in_documents_code_or_manifest(release_trees: tuple[Path, Path]) -> None:
    """Mutation: treat the released opener in the tagged limitations as a draft marker."""
    _, released = release_trees
    paths = [path for path in released.glob("*.md") if path.is_file()]
    for folder in ("docs", "plugins", "agent-skills", "eval", "apps/api/src", "tests", "scripts"):
        paths.extend(path for path in (released / folder).rglob("*") if path.suffix in {".md", ".py", ".json"})
    paths.extend((released / "apps/web" / "app").rglob("*.ts*"))
    assert not [
        str(path.relative_to(released))
        for path in paths
        if any(marker in path.read_text(encoding="utf-8") for marker in DRAFT_MARKERS)
    ]


def _flat(path: str) -> str:
    return " ".join(_text(path).split())


def test_the_control_documents_name_v0241_as_latest_and_v0240_as_prior() -> None:
    """Mutation: restore the v0.24.1 candidate line in the README."""
    for name in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "README.md",
                 "RELEASING.md", "ROADMAP.md", "docs/vnext/README.md"):
        text = _flat(name)
        assert "`v0.24.1` is the current release candidate. It is not published." not in text, name
        assert "`v0.24.1` is the latest published release" in text, name
        assert "`v0.24.0` is the latest published release" not in text, name
    for name in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "README.md", "ROADMAP.md"):
        assert "`v0.24.0` is the immediately prior published release" in _flat(name), name
    for name in ("ARCHITECTURE.md", "CURRENT_STATE.md", "PRODUCT_BRIEF.md", "ROADMAP.md"):
        assert CHECKSUMS in _text(name), name
    assert "artifact digests are in `docs/release/v0.24.0-checksums.txt`." in _flat("ARCHITECTURE.md")
    assert f"- [v0.24.1 release notes]({NOTES})" in _text("README.md")
    assert "[latest published release notes](../release/v0.24.1-release-notes.md)" in _flat("docs/vnext/README.md")
    assert "[v0.24.1 release notes](release/v0.24.1-release-notes.md)" in _text("docs/overview.md")
    assert f"`{NOTES}`, with artifact digests in `{CHECKSUMS}`." in _flat("ROADMAP.md")
    state = _text("CURRENT_STATE.md")
    assert f"- `{NOTES}`\n- `{CHECKSUMS}`\n" in state
    assert "`v0.24.0` is the immediately prior published release; its records are" in state
    latest = "## What `v0.24.1` Shipped"
    previous = "## What `v0.24.0` Shipped"
    assert state.index(latest) < state.index(previous)
    assert f"[release notes]({NOTES})" in state.split(latest, 1)[1].split(previous, 1)[0]
    assert f"{previous}\n\n`v0.24.0` is the immediately prior published release." in state
    for earlier in ("v0.23.1", "v0.23.0", "v0.22.0", "v0.21.1", "v0.21.0"):
        assert f"## What `{earlier}` Shipped\n\n`{earlier}` is an earlier published release." in state
    history = " ".join(state.split())
    assert "pins the plugin to the `v0.24.1` tag commit, so the plugin installs from that marketplace." in history
    assert "`v0.24.1` tag has it still pinned to the `v0.24.0` tag commit." in history
    assert "`v0.24.0` tag has it still pinned to the `v0.23.1` tag commit," in history


def test_the_install_and_checkout_lines_name_v0241_as_the_latest_release() -> None:
    """Mutation: set the headless install guide's --tag back to v0.24.0."""
    guide = _flat("docs/alpha/headless-ubuntu-install.md")
    assert "use the latest published release tag (`v0.24.1`) for an immutable deployment." in guide
    assert "with the latest published release tag (`v0.24.1`): `--tag v0.24.1`." in guide
    assert "v0.24.0" not in guide
    for name in ("README.md", "docs/alpha/quickstart.md", "docs/overview.md"):
        page = _flat(name)
        assert "To run v0.24.1, run `git checkout v0.24.1` before `make setup`." in page, name
        assert "git checkout v0.24.0" not in page, name
    assert "on top of the latest published `v0.24.1` baseline." in _flat("docs/integrations/reference-paths.md")


def test_the_tag_keeps_the_prior_marketplace_pin(release_trees: tuple[Path, Path]) -> None:
    """Mutation: expect the v0.24.1 tag to contain the published pin."""
    _, released = release_trees
    at_tag = json.loads(_text(".claude-plugin/marketplace.json", released))["plugins"][0]["source"]
    assert (at_tag["ref"], at_tag["sha"]) == (PRIOR, PRIOR_COMMIT)
    for tag, expected in ((RELEASE, TAG_COMMIT), (PRIOR, PRIOR_COMMIT)):
        commit = subprocess.check_output(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=REPOSITORY, text=True)
        assert commit.strip() == expected


def test_the_marketplace_pins_the_release_tag_and_its_commit() -> None:
    """Mutation: put v0.24.0 back into the working marketplace reference."""
    current = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert (current["ref"], current["sha"]) == (RELEASE, TAG_COMMIT)


def test_the_upgrade_step_records_the_new_marketplace_pin() -> None:
    """Mutation: remove the 2026-11-18 marketplace update beside step 6."""
    step = (
        "6. **Claude Code plugin.** The marketplace on `main` pins v0.24.0 until the\n"
        "   change after publication moves it."
    )
    update = (
        "   > **Update (2026-11-18):** v0.24.1 is published, and the marketplace file on `main` now pins the v0.24.1 tag "
        "commit, so the marketplace install runs v0.24.1. The copy inside the v0.24.1 tag still pins v0.24.0. "
        "Add the marketplace from the repository."
    )
    notes = _text(NOTES)
    assert f"{step}\n\n{update}\n\n## The four changes" in notes
    assert notes.count("**Update (2026-11-18):** v0.24.1 is published") == 1


def test_the_security_section_names_the_published_advisory() -> None:
    """Mutation: remove the advisory identifier from the update in the v0.24.1 notes."""
    sentence = "The v0.24.0 notes said that no security advisory was planned; that sentence is corrected here."
    assert f"{sentence}\n\n> **Update (2026-11-18):** The advisory is {ADVISORY}.\n\n## What" in _text(NOTES)


def test_the_prior_security_section_records_the_advisory_and_upgrade() -> None:
    """Mutation: remove the v0.24.0 upgrade instruction from its advisory update."""
    sentence = "No security advisory is planned for this release."
    assert sentence in _git_text(PRIOR, PRIOR_NOTES)
    update = (
        "> **Update (2026-11-18):** An advisory covering behaviour in v0.24.0 was published "
        f"on 2026-11-18 with v0.24.1. See {ADVISORY}. Upgrade to v0.24.1."
    )
    assert f"{sentence}\n\n{update}\n\n## What" in _text(PRIOR_NOTES)


def test_the_three_limits_link_to_the_pages_that_state_them(release_trees: tuple[Path, Path]) -> None:
    """Mutation: substitute an incorrect expected browser ceiling in the tagged agent guide."""
    _, released = release_trees
    notes = _text(NOTES, released)
    for reference in re.findall(r"\]\((\.\.[^)]*)\)", notes):
        relative, _, anchor = reference.partition("#")
        target = released / Path(NOTES).parent / relative
        assert target.is_file(), (relative, anchor)
        if anchor:
            assert re.search(rf"(?m)^#+ {re.escape(anchor)}$", target.read_text(), re.IGNORECASE)
    assert "serve local callers without a key or label filtering" in _text("docs/alpha/known-limitations.md", released)
    deployment = " ".join(_text("docs/deployment/single-tenant-self-hosted.md", released).split())
    assert "remain loopback-only operator actions" in deployment
    assert "strictest sensitivity ceiling of any key allowed to issue one" in _text("docs/alpha/agent-integration.md", released)


def test_prior_mcp_docs_described_the_rule_for_both_backends(release_trees: tuple[Path, Path]) -> None:
    """Mutation: look for the old rule in the marketplace file instead of the prior MCP guide."""
    prior, released = release_trees
    path = "docs/alpha/mcp-tools.md"
    guide = " ".join(_text(path, prior).split())
    assert "`alicebot agent keys create` on Postgres or `alice-memory keys create` on the SQLite vault" in guide
    assert "every keyless call is refused, reads included" in guide
    assert _text(path, released) == _git_text(BASE, path)


def test_database_schema_is_unchanged(release_trees: tuple[Path, Path], tmp_path: Path) -> None:
    """Mutation: compare the released SQLite schema with an empty vault."""
    prior_tree, released_tree = release_trees
    migration_directory = Path("apps/api/alembic/versions")
    migrations = {
        path.name: path.read_bytes()
        for path in (prior_tree / migration_directory).glob("*.py")
    }
    assert migrations
    assert migrations == {
        path.name: path.read_bytes()
        for path in (released_tree / migration_directory).glob("*.py")
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
    released = _tag_python(released_tree, tmp_path, probe, str(tmp_path / "release.db"))
    assert json.loads(prior) == json.loads(released)
