"""The v0.23.1 release notes carry the upgrade steps, the wording about the agent key routes and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog, a page of the documents or a file of the tree at the
v0.23.1 tag. These tests pin the parts a later edit could quietly weaken: the release identity and its published state,
the checksums record, the six version sites at the tag and the marketplace pin, the dated changelog heading, the security
wording, the steps an upgrading user must act on, the sentences of the agent guide, the Codex page and the Hermes page
that the notes repeat (and the text of the guide at the v0.23.0 tag that the notes say was wrong), the numbers that drift
(the tool counts, the PostgreSQL revision) and the claim that the package holds one changed docstring and nothing else.
Two claims are run: the docstring helper gives the same values in v0.23.0 and v0.23.1, and a SQLite vault written by one
version opens in the other.

What the release held is read from the v0.23.1 tag, with ``git show`` or with ``git archive`` into a temporary folder
whose code runs in a subprocess, and never from the working tree. The tree moves on with every later change to main (a
changed module, an entry under Unreleased, a reworded page, a new shard), and a published claim about the release must
not fail because of one. The tree is read only for what must stay true after the release: the notes and their published
state, the checksums file, the control documents, the install lines, the marketplace pin and the dated heading of the
changelog. Each comparison with v0.23.0 reads that tag the same way. Each test names the mutation that must fail it, and
for a test that reads the tag the mutations include reading the v0.23.0 tag where it reads the v0.23.1 tag and reading
the tree where it reads the tag.

The notes are published. The change that followed publication moved their state to ``published`` and ``recorded``, added
the checksums file, moved the marketplace file to the v0.23.1 tag and its commit, named v0.23.1 as the latest published
release in the control documents and removed the release candidate line, as the change after v0.23.0 did, and added one
dated 2026-11-01 update beside the notes' own marketplace step. The tagged lines of the notes and of the changelog
section are held by ``test_published_release_notes_stay_immutable.py``. The release check's pre-tag mode refuses the tree
now, and one test below pins the reasons.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
from io import BytesIO
from pathlib import Path

import pytest
import yaml

import scripts.release_check as release_check
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
from tests.unit.test_published_release_notes_stay_immutable import _tagged
from tests.unit.test_v0210_release_notes import _tagged_listing

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.23.1-release-notes.md"
GUIDE_PATH = "docs/alpha/agent-integration.md"
HEADING = "## v0.23.1 \u2014 2026-10-31"
# The notes are about 6,200 characters with the dated update, and 7,000 leaves room for a correction and no more.
NOTES_CEILING = 7_000
USER_ID = "00000000-0000-0000-0000-000000000001"

# The v0.23.1 tag, the commit it points at (which the marketplace file pins now), the v0.23.0 tag that the copy of the
# marketplace file inside the v0.23.1 tag still pins, its commit, and the digests of the two files published for v0.23.1.
RELEASE_TAG = "v0.23.1"
TAG_COMMIT = "2443e7cb6d8f89b4e625cec7adcd7b4a8dcd7276"
PRIOR_TAG = "v0.23.0"
PRIOR_TAG_COMMIT = "116d21775c931e69478f691ab0e3fd1b1e68015d"
CHECKSUMS_PATH = "docs/release/v0.23.1-checksums.txt"
WHEEL_SHA256 = "0e90e2dce4066a5803ffed40f8476f8993187abcaecee6a1a244a2f4cd25347e"
SDIST_SHA256 = "626db6ab4befce9c4cbe2a287be48fd996465e31cea95d50ffb921a5367b09c2"

CANDIDATE_LINE = "`v0.23.1` is the current release candidate. It is not published."
LATEST_DOCS = (
    "ARCHITECTURE.md",
    "PRODUCT_BRIEF.md",
    "ROADMAP.md",
    "RELEASING.md",
    "docs/vnext/README.md",
)

SECTIONS = [
    "What to do after upgrading",
    "The agent key section of the guide",
    "Other changes",
    "Behaviour changes an integrator will notice",
    "Security",
    "What this release does not claim",
    "Install",
]

GUIDE_HEADING = "### Agent keys on the SQLite vault (`alice-memory`)"


def _text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _flat(path: str) -> str:
    return " ".join(_text(path).split())


def _section(text: str, heading: str) -> str:
    """The body of a ``## `` section of ``text``, whitespace collapsed."""

    match = re.search(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return " ".join(match.group("body").split())


def _guide_section(text: str) -> str:
    """The agent key section of the guide, whitespace collapsed: from its heading to the next ``## `` heading."""

    start = text.index(GUIDE_HEADING)
    end = text.index("\n## ", start)
    return " ".join(text[start:end].split())


def _tagged_flat(path: str) -> str:
    """``path`` as the v0.23.1 tag held it, whitespace collapsed."""

    return " ".join(_tagged(RELEASE_TAG, path).split())


def _changelog_section() -> str:
    """The v0.23.1 section of the changelog as the tag held it: a dated paragraph added later is not part of it."""

    changelog = _tagged(RELEASE_TAG, "CHANGELOG.md")
    return changelog.split(HEADING)[1].split("\n## v0.23.0")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


# ---------------------------------------------------------------------------
# The v0.23.0 and v0.23.1 tags, exported into temporary folders. The tests that compare code run it in a subprocess.
# ---------------------------------------------------------------------------


def _export(tag: str, tmp_path_factory: pytest.TempPathFactory, name: str, *paths: str) -> Path:
    """``paths`` of ``tag``, made with ``git archive`` into a new temporary folder.

    A checkout without tags skips the tests that use it, and under ``CI`` they fail instead, the rule of
    ``test_published_release_notes_stay_immutable.py``.
    """

    _tagged(tag, "pyproject.toml")  # skips, or fails under CI, when the tag cannot be read
    done = subprocess.run(["git", "archive", tag, *paths], cwd=REPO_ROOT, capture_output=True, check=True)
    target = tmp_path_factory.mktemp(name)
    with tarfile.open(fileobj=BytesIO(done.stdout)) as archive:
        archive.extractall(target, filter="data")
    return target


@pytest.fixture(scope="module")
def v0230_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``apps/api/src`` and ``apps/api/alembic`` of the v0.23.0 tag."""

    return _export(PRIOR_TAG, tmp_path_factory, "v0230", "apps/api/src", "apps/api/alembic")


@pytest.fixture(scope="module")
def v0231_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The package source, the migrations, ``workers``, ``pyproject.toml`` and ``MANIFEST.in`` of the v0.23.1 tag."""

    return _export(
        RELEASE_TAG, tmp_path_factory, "v0231", "apps/api/src", "apps/api/alembic", "workers", "pyproject.toml", "MANIFEST.in"
    )


def _env_for(source_root: Path, key: str | None = None) -> dict[str, str]:
    """The environment of a child process that imports ``alicebot_api`` from ``source_root``, with no ``ALICE_`` setting."""

    env = {name: value for name, value in os.environ.items() if not name.startswith("ALICE_")}
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(source_root)
    if key is not None:
        env[AGENT_API_KEY_ENV] = key
    return env


def _python(source_root: Path, code: str, *args: str, key: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args], env=_env_for(source_root, key), capture_output=True, text=True, timeout=180
    )


_COMMAND = "import sys; from alicebot_api.onramp import main; sys.exit(main(sys.argv[1:]))"


def _alice(source_root: Path, *args: str, key: str | None = None) -> subprocess.CompletedProcess[str]:
    """``alice-memory`` with the arguments, run from the code under ``source_root``."""

    return _python(source_root, _COMMAND, *args, key=key)


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (AGENT_API_KEY_ENV, "ALICE_MCP_FULL_TOOLS", "ALICE_MCP_LEGACY_TOOLS", "ALICE_LEGACY_SURFACES"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Identity, state, size and the six version sites
# ---------------------------------------------------------------------------


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title; move the state comment off the second line; set either status back to
    pending; add a second state line; delete ``docs/release/v0.23.1-checksums.txt``.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.23.1 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.23.1",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / CHECKSUMS_PATH).is_file()


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutations, each one alone: change one hex digit of either digest; swap the two file names; add a third line;
    write another version into the header or into the verify commands; delete a header line.

    The digests are the ones PyPI serves for 0.23.1 and the GitHub Release lists for its assets, read on 2026-11-01
    (wheel 1,995,599 bytes, source distribution 1,751,542 bytes). The header and the layout are the v0.23.0 file's with
    the version replaced, so the two files stay in one format. Nothing offline compares the digests with PyPI:
    ``release_check.py --tag v0.23.1 --verify-pypi-artifacts`` does, with the two files downloaded.
    """

    old = _text("docs/release/v0.23.0-checksums.txt").splitlines()
    new = _text(CHECKSUMS_PATH).splitlines()
    assert len(old) == len(new) == 19
    assert [line.replace("0.23.0", "0.23.1") for line in old[:17]] == new[:17]
    assert new[0] == "# Alice v0.23.1: published artifact SHA-256 digests"
    assert new[17:] == [
        f"{WHEEL_SHA256}  alice_memory-0.23.1-py3-none-any.whl",
        f"{SDIST_SHA256}  alice_memory-0.23.1.tar.gz",
    ]
    assert _text(CHECKSUMS_PATH).endswith(".tar.gz\n")


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,000 characters into the notes; delete a section; swap two sections."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def _tag_commit(tag: str) -> str:
    """The commit ``tag`` points at, with the shallow-clone rule of ``_tagged``."""

    _tagged(tag, "pyproject.toml")  # skips, or fails under CI, when the tag cannot be read
    done = subprocess.run(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_every_version_site_names_0231_at_the_tag_and_the_marketplace_pins_the_tag() -> None:
    """Mutations, each one alone: read the v0.23.0 tag where the six sites or the plugin pins are read; read the working
    tree where the sites are read, after a change that bumps the version (a tree that has moved on to the next release);
    set the marketplace ``ref`` back to v0.23.0; change one hex digit of the marketplace ``sha``; change the install
    command in the notes; change the commit that the copy of the marketplace file inside the v0.23.1 tag is expected to
    pin, or the commit of either tag.

    The six sites are the ones ``scripts/release_check.py`` requires, and they are read from the tag, so the change that
    prepares the next release can move them in the tree without failing a claim about this one. The post-publication
    change moved the marketplace file to the v0.23.1 tag and the commit that tag points at, so the tree names the latest
    published release. The v0.23.1 tag itself still pins v0.23.0, and the plugin page says so.
    """

    assert _tag_commit(RELEASE_TAG) == TAG_COMMIT
    pyproject = tomllib.loads(_tagged(RELEASE_TAG, "pyproject.toml"))["project"]["version"]
    assert pyproject == "0.23.1"
    assert json.loads(_tagged(RELEASE_TAG, "apps/web/package.json"))["version"] == pyproject
    assert json.loads(_tagged(RELEASE_TAG, "packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_tagged(RELEASE_TAG, "plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _tagged(RELEASE_TAG, name), name
        assert "alice-memory==0.23.0" not in _tagged(RELEASE_TAG, name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == RELEASE_TAG
    assert marketplace["sha"] == TAG_COMMIT
    # The copy of the marketplace file inside the tag still pins the release before it.
    inside = json.loads(_tagged(RELEASE_TAG, ".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert (inside["ref"], inside["sha"]) == (PRIOR_TAG, PRIOR_TAG_COMMIT)
    assert _tag_commit(PRIOR_TAG) == PRIOR_TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.23.1 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def _blocks(path: str) -> list[str]:
    """Paragraphs of a file with quote markers and indentation removed, each joined onto one line."""

    blocks: list[list[str]] = [[]]
    for line in _text(path).splitlines():
        stripped = re.sub(r"^[\s>]+", "", line)
        if stripped:
            blocks[-1].append(stripped)
        else:
            blocks.append([])
    return [" ".join(block) for block in blocks if block]


def test_the_notes_say_beside_their_marketplace_step_that_main_now_pins_v0231() -> None:
    """Mutations, each one alone: delete the 2026-11-01 update; name v0.23.0 as what ``main`` pins in it; drop the
    sentence about the copy inside the tag; move it away from step 4 of the upgrade steps; glue it to the end of another
    paragraph; add a second dated update.

    Step 4 of the upgrade steps says the marketplace file on ``main`` pins the v0.23.0 tag commit until a change after
    the release moves it. That sentence stays as tagged, and the update after it says the change has been made, as the
    update beside step 8 of the v0.23.0 notes did.
    """

    update = (
        "**Update (2026-11-01):** v0.23.1 is published, and the marketplace file on `main` now pins the v0.23.1 tag "
        "commit, so the marketplace install runs v0.23.1. The copy inside the v0.23.1 tag still pins v0.23.0, so add "
        "the marketplace from the repository, not from a checkout of the tag."
    )
    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == update]
    assert len(positions) == 1, positions
    index = positions[0]
    assert blocks[index - 1].endswith(
        "4. **Claude Code plugin.** The marketplace file on `main` pins the v0.23.0 tag commit until a change after "
        "this release moves it, so a marketplace install runs v0.23.0 code until then."
    )
    assert blocks[index + 1] == "## The agent key section of the guide"
    assert _text(NOTES_PATH).count("Update (2026-11-01)") == 1
    assert sum(block.startswith("**Update (") for block in blocks) == 1


def test_the_control_documents_name_v0231_as_the_latest_published_release_and_v0230_as_the_prior_one() -> None:
    """Mutations, each one alone: put the candidate line back into one control document or into the README; name
    v0.23.0 as the latest published release in one of them; name v0.22.0 as the immediately prior release; set a
    checksums or release notes pointer back to v0.23.0; put ``Targets`` back in the heading of the current state; point
    the release notes link of the README or the vNext README at another release; set the lead of ``What `v0.23.0`
    Shipped`` back to the latest published release; drop the v0.23.1 tag from the tag history of the current state or
    set its pin sentence back to v0.23.0.

    The candidate line of a later release names that release and is that release's test to pin, so only the v0.23.1
    candidate line is refused here.
    """

    for name in LATEST_DOCS:
        text = _text(name)
        assert CANDIDATE_LINE not in text, name
        assert "`v0.23.1` is the latest published release" in text, name
        assert "`v0.23.0` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.23.1` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.23.0` is the immediately prior published release."
    ) in readme
    assert CANDIDATE_LINE not in readme
    assert "- [v0.23.1 release notes](docs/release/v0.23.1-release-notes.md)" in _text("README.md")
    assert "[latest published release notes](../release/v0.23.1-release-notes.md)" in _flat("docs/vnext/README.md")
    architecture = _flat("ARCHITECTURE.md")
    assert "`docs/release/v0.23.1-checksums.txt`. - **Prior published:** `v0.23.0` is the immediately prior published release" in architecture
    assert "artifact digests are in `docs/release/v0.23.0-checksums.txt`." in architecture
    brief = _flat("PRODUCT_BRIEF.md")
    assert "`v0.23.0` is the immediately prior published release" in brief
    assert "exact digests in `docs/release/v0.23.1-checksums.txt`." in brief
    roadmap = _flat("ROADMAP.md")
    assert (
        "`docs/release/v0.23.1-release-notes.md`, with artifact digests in `docs/release/v0.23.1-checksums.txt`. "
        "`v0.23.0` is the immediately prior published release."
    ) in roadmap
    state = _text("CURRENT_STATE.md")
    assert "What `v0.23.1` Targets" not in state
    assert (
        "\n## What `v0.23.1` Shipped\n\n`v0.23.1` is the latest published release and remains the install, checksum,\n"
        "and baseline reference.\n\nIt is a patch release on `v0.23.0` and shipped two changes from `main`."
    ) in state
    shipped = " ".join(state[state.index("## What `v0.23.1` Shipped") : state.index("## What `v0.23.0` Shipped")].split())
    assert "It changes no behaviour and adds no tool, command, setting or migration: PostgreSQL stays at revision `20261011_0097`." in shipped
    assert state.index("## What `v0.23.1` Shipped") < state.index("## What `v0.23.0` Shipped")
    assert "\n## What `v0.23.0` Shipped\n\n`v0.23.0` is the immediately prior published release.\n" in state
    assert "\n## What `v0.22.0` Shipped\n\n`v0.22.0` is an earlier published release.\n" in state
    assert "\n## What `v0.21.1` Shipped\n\n`v0.21.1` is an earlier published release.\n" in state
    assert "\n## What `v0.21.0` Shipped\n\n`v0.21.0` is an earlier published release.\n" in state
    assert "- `v0.23.1` is the latest published release." in state
    assert "`v0.23.0` is the immediately prior\n  published release." in state
    assert "`docs/release/v0.23.1-release-notes.md`\n- `docs/release/v0.23.1-checksums.txt`\n" in state
    assert "`v0.23.0` is the immediately prior published release; its records are" in state
    history = " ".join(state.split())
    assert (
        "The `v0.19.2` tag has the file too, still pinned to the `v0.19.0` tag commit, the `v0.20.0` tag has it still "
        "pinned to the `v0.19.2` tag commit, the `v0.20.2` tag has it still pinned to the `v0.20.0` tag commit, the "
        "`v0.21.0` tag has it still pinned to the `v0.20.2` tag commit, the `v0.21.1` tag has it still pinned to the "
        "`v0.21.0` tag commit, the `v0.22.0` tag has it still pinned to the `v0.21.1` tag commit, the `v0.23.0` tag "
        "has it still pinned to the `v0.22.0` tag commit, and the `v0.23.1` tag has it still pinned to the `v0.23.0` "
        "tag commit."
    ) in history
    assert "pins the plugin to the `v0.23.1` tag commit, so the plugin installs from that marketplace." in history
    assert "- [v0.23.1 release notes](https://github.com/samrusani/AliceMemory/blob/main/" + NOTES_PATH + ")" in state


def test_the_install_and_checkout_lines_name_v0231_as_the_latest_release() -> None:
    """Mutations, each one alone: set the ``--tag`` of the headless install guide, the latest release tag it names, the
    checkout line of the quickstart or the baseline named by the reference paths page back to v0.23.0.

    The README's checkout line is pinned in ``test_readme_facts_live_in_the_docs.py``. The lines pinned here name the
    release a reader installs or checks out, so they move with each release. A comparison with v0.23.0 elsewhere in the
    docs is history and stays.
    """

    guide = _flat("docs/alpha/headless-ubuntu-install.md")
    assert "use the latest published release tag (`v0.23.1`) for an immutable deployment." in guide
    assert "with the latest published release tag (`v0.23.1`): `--tag v0.23.1`." in guide
    assert "v0.23.0" not in guide
    quickstart = _flat("docs/alpha/quickstart.md")
    assert "To run v0.23.1, run `git checkout v0.23.1` before `make setup`." in quickstart
    assert "git checkout v0.23.0" not in quickstart
    assert "on top of the latest published `v0.23.1` baseline." in _flat("docs/integrations/reference-paths.md")


def test_the_changelog_at_the_tag_has_the_dated_heading_and_an_empty_unreleased_section_right_before_it() -> None:
    """Mutations, each one alone: read the v0.23.0 tag where this test reads the v0.23.1 tag; read the working tree where
    it reads the tag (a tree that has an entry under ``## Unreleased``, as the tree after a later change does, fails it).

    ``release_check.py --require-finalized-release-docs`` asked at the tag for the dated heading and an empty Unreleased
    section, and the tag held both. The Unreleased section may hold entries once main has moved on, so the tree is not
    read here: the next test reads it for the heading.
    """

    changelog = _tagged(RELEASE_TAG, "CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1] == "Unreleased\n"
    assert sections[2].startswith("v0.23.1 \u2014 2026-10-31\n")
    assert sections[3].startswith("v0.23.0 \u2014 2026-10-30\n")
    assert sections[4].startswith("v0.22.0 \u2014 2026-10-26\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[2]
    assert "Unreleased (on" not in sections[3]


def test_the_changelog_keeps_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date; put another section between the Unreleased section
    and v0.23.1; put the marker for the unreleased state back into the v0.23.1 section.

    The Unreleased section may hold entries once main has moved on, so this test does not ask for it to be empty. It asks
    that the v0.23.1, v0.23.0 and v0.22.0 sections follow it in that order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[2].startswith("v0.23.1 \u2014 2026-10-31\n")
    assert sections[3].startswith("v0.23.0 \u2014 2026-10-30\n")
    assert sections[4].startswith("v0.22.0 \u2014 2026-10-26\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[2]
    assert "Unreleased (on" not in sections[3]


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest() -> None:
    """Mutation: put the marker for the unreleased state of v0.23.0 back into any document, docstring, test or case of
    the mutation manifest.

    The pattern is written as a regular expression so that this file does not hold the marker itself. The published
    v0.18.0 notes carry two dated corrections that say ``not in v0.19.0``; they are another marker and immutable.
    """

    pattern = re.compile(r"Unreleased\s*\(\s*on\s+main\s*,\s*not\s+in\s+v0\.23\.0")
    paths = [
        REPO_ROOT / name
        for name in ("README.md", "SECURITY.md", "CURRENT_STATE.md", "CHANGELOG.md", "ROADMAP.md", "RELEASING.md")
    ]
    for root in ("docs", "plugins", "agent-skills", "eval"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.md"))
    for root in ("apps/api/src", "tests", "scripts"):
        paths.extend(path for path in (REPO_ROOT / root).rglob("*.py"))
    paths.append(REPO_ROOT / "scripts/derived_label_mutations.json")
    hits = [str(path.relative_to(REPO_ROOT)) for path in paths if pattern.search(path.read_text(encoding="utf-8"))]
    assert hits == []


def test_the_notes_use_no_dash_and_none_of_the_words_the_repository_keeps_out() -> None:
    """Mutations, each one alone: write a dash into the notes; write a hyphen with a space on each side; write a word
    the repository keeps out of its text (assembled in this test from parts, so that this file holds none of them);
    use the review or the verb of the disclosure sentence anywhere else; delete the sentence that says how the release
    was checked, or only its ``internal adversarial review`` clause; delete the disclosure sentence; write an advisory
    identifier; delete the sentence that says no advisory is planned; write a local path or an email address; use the
    word ``isolation``.
    """

    text = _text(NOTES_PATH)
    assert "\u2014" not in text
    assert "\u2013" not in text
    assert re.search(r"\S - \S", text) is None
    flat = " ".join(text.split())
    lowered = flat.lower()
    kept_out = ("tow" + "er", "wi" + "ki", "rul" + "ing", "security " + "team", "re" + "viewer")
    for word in kept_out:
        assert re.search(rf"\b{word}", lowered) is None, word
    # The two sentences of the security wording are the only place the review and the verb appear.
    review = "automated security scanning and internal adversarial review, findings triaged and fixed"
    assert flat.count("This release was checked with " + review + ".") == 1
    assert lowered.count("re" + "view") == 1
    verb = "au" + "dited"
    assert flat.count(f"No one outside the project has {verb} the code.") == 1
    assert lowered.count("au" + "dit") == 1
    for word in ("isolation", "boundary", "tenant", "sandbox", "vulnerability", "independently", "third-party", "penetration"):
        assert word not in lowered, word
    assert "ghsa-" not in lowered and "cve-" not in lowered
    assert flat.count("No security advisory is planned for this release.") == 1
    # The scratch folder is written in two parts: the hygiene test refuses that path in any test file.
    for fragment in ("/Users/", "/home/", "/private/", "/tm" + "p/", "@"):
        assert fragment not in text, fragment
    assert "Security" in SECTIONS


def test_the_lead_paragraph_says_who_should_take_the_release_what_changed_and_that_there_is_no_migration() -> None:
    """Mutations, each one alone: delete the sentence that names Codex and Hermes; delete the sentence that lets everyone
    else stay on v0.23.0; delete the sentence that says there is no migration; unbold the paragraph; say behaviour
    changes; name another table or key for Codex, another table for Hermes, or another file or change for the
    docstring in the bullets under the paragraph.
    """

    text = _text(NOTES_PATH)
    lead = text.split("\n\n")[1]
    assert lead.startswith("**Take this release if you connect Codex or Hermes to a SQLite vault that holds agent")
    assert lead.endswith('See "What to do after upgrading".**')
    flat = " ".join(lead.split())
    assert "Once the vault holds a key, a call that does not carry `ALICE_AGENT_API_KEY` is refused" in flat
    assert "the v0.23.0 agent guide gave Codex no route for the key and told Hermes users to export it in the shell" in flat
    assert "The guide now names a route for each host." in flat
    assert "Nothing behaves differently:" in flat
    assert "the code is v0.23.0's apart from one docstring and the version number" in flat
    assert "there is no new tool, command or setting, and there is no migration" in flat
    assert "Everyone else can stay on v0.23.0." in flat
    bullets = [" ".join(item.split()) for item in text.split("\n\n")[2].split("\n- ")]
    assert len(bullets) == 3
    assert bullets[0].endswith(" The guide now names the alice table's `env_vars` or `env`.")
    assert bullets[1].endswith(" The guide now names the `env` map of `mcp_servers.alice`, which the Hermes page describes.")
    assert bullets[2] == (
        "The docstring of `_own_env` in `host_install.py` no longer says install writes a reference to the agent key."
    )


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command, the revision, the route or the
    check a step names; say that the install re-run prints the Hermes key.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 5))
    for needle in (
        "`uv tool upgrade alice-memory`",
        "`pip install -U alice-memory`",
        "`alice-memory --version`",
        "and restart your hosts. Nothing else is required.",
        "**No migration.**",
        "A vault that v0.23.0 wrote opens in v0.23.1, and a vault that v0.23.1 has opened opens in v0.23.0.",
        "PostgreSQL stays at revision `20261011_0097`.",
        "**Codex or Hermes on a vault that holds a key.** Skip this step if the vault holds no key.",
        "check that the host's MCP server gets `ALICE_AGENT_API_KEY`.",
        "For Codex, name it in the alice table's `env_vars` (`env_vars = [\"ALICE_AGENT_API_KEY\"]`) to forward the shell's value, "
        "or set it in the same table's `env`.",
        "For Hermes, put it in the `env` map of `mcp_servers.alice`.",
        "A re-run of `alice-memory install` keeps these entries, and for Hermes never prints the key.",
        "a call that did not receive the key is refused, and a call that did is answered.",
        "The guide's text on the session-start hook is unchanged: the hook is a separate process and reads the key from its own environment.",
        "The marketplace file on `main` pins the v0.23.0 tag commit until a change after this release moves it",
        "so a marketplace install runs v0.23.0 code until then",
    ):
        assert needle in steps, needle
    assert steps.index("**No migration.**") < steps.index("**Codex or Hermes on a vault that holds a key.**")
    assert steps.index("**Codex or Hermes on a vault that holds a key.**") < steps.index("**Claude Code plugin.**")


# ---------------------------------------------------------------------------
# The guide and the host pages the notes repeat, and the guide at the v0.23.0 tag
# ---------------------------------------------------------------------------


def test_the_guide_gives_each_host_the_route_the_notes_name() -> None:
    """Mutations, each one alone: read the v0.23.0 tag where the guide is read (its section has no Codex route); read the
    working tree there, after a change that rewords the section; reword one of the sentences of the guide that the test
    looks for; take the ``env_vars`` route out of the Codex bullet of the notes' section on the guide; say in the notes
    that the Hermes route is the shell environment.

    The notes repeat these sentences. The guide as the v0.23.1 tag held it is the source, so a note that drifts from it
    fails here, and a later change of the guide in the tree does not.
    """

    guide = _guide_section(_tagged(RELEASE_TAG, GUIDE_PATH))
    for needle in (
        "set `ALICE_AGENT_API_KEY=<the key>` in the environment the host's MCP server process starts with. How the key gets there depends on the host:",
        "Claude Code and the plugin: the `env` map of the host's MCP entry, or the launch environment of your user session.",
        "Codex passes a stdio server only `HOME`, `PATH`, `LANG` and a few other names, so a key exported in the shell that "
        "starts Codex does not reach Alice by itself.",
        "Name it in the alice table's `env_vars` (`env_vars = [\"ALICE_AGENT_API_KEY\"]`) to forward the shell's value, or set it "
        "in the same table's `env` (see [Codex](../integrations/codex.md)). A re-run of install keeps both.",
        "Hermes: put the key in the `env` map of `mcp_servers.alice`, the route the [Hermes](../integrations/hermes.md) page shows. "
        "A re-run of install keeps it and never prints it.",
        "This repository does not show that Hermes passes your shell's environment on to the server, so do not rely on an "
        "exported variable.",
        "A key written into a host's config file is stored there as plain text that your OS user can read, which is the same "
        "reach keys already leave open.",
        "None of these routes has been run against a real host in this repository.",
        "To check one, make a call from the host: once the vault holds a key, a call that did not receive it is refused, and a "
        "call that did is answered.",
        "keys tell Alice which agent is asking and limit what Alice returns to that agent. They do not stop a process that "
        "runs as your OS user from reading the vault file.",
    ):
        assert needle in guide, needle
    notes = _flat(NOTES_PATH)
    keys_section = _section(_text(NOTES_PATH), "The agent key section of the guide")
    for needle in (
        "**Claude Code and the plugin:** the `env` map of the host's MCP entry, or the launch environment of your user session. "
        "This is as in v0.23.0.",
        "**Codex:** `env_vars = [\"ALICE_AGENT_API_KEY\"]` in the alice table forwards the shell's value, and the same table's "
        "`env` sets it. A re-run of install keeps both.",
        "**Hermes:** the `env` map of `mcp_servers.alice`, which the Hermes page describes. A re-run of install keeps it and "
        "never prints it.",
        "a key written into a host's config file is stored there as plain text that your OS user can read, which is the same "
        "reach keys already leave open, and that none of these routes has been run against a real host in this repository.",
    ):
        assert needle in keys_section, needle
    assert "Keys tell Alice which agent is asking and limit what Alice returns to that agent. They do not stop a process that runs as your OS user from reading the vault file." in notes
    assert "Codex passes a stdio server only `HOME`, `PATH`, `LANG` and a few other names" in notes


def test_the_guide_at_the_v0230_tag_gave_codex_no_route_and_told_hermes_users_to_export_the_key() -> None:
    """Mutations, each one alone: read the v0.23.1 tag where the old section is read (it names a Codex route); read the
    v0.23.0 tag where the new section is read (it still tells Hermes users to export the key); read the working tree
    where the new section is read, after a change that rewords it; say in the notes that the v0.23.0 guide named a way
    to give Codex the key; change the notes' account of what the v0.23.0 guide told Hermes users.

    The two tags are the sources of what v0.23.0 said and what v0.23.1 says, and they cannot change. The Claude Code
    route, which the notes call unchanged, and the session-start hook bullet, which they call unchanged, are compared
    between the two tags as well.
    """

    old = _tagged(PRIOR_TAG, GUIDE_PATH)
    old_section = _guide_section(old)
    assert "codex" not in old_section.lower()
    assert (
        "set the variable in the environment the host launches with (Hermes does not inherit your shell's environment and its "
        "config's env map is not the place for a secret), so export `ALICE_AGENT_API_KEY` in the launchd session, the terminal, "
        "or the wrapper that starts the host."
    ) in old_section
    new_section = _guide_section(_tagged(RELEASE_TAG, GUIDE_PATH))
    assert "Hermes does not inherit your shell's environment" not in new_section
    assert "export `ALICE_AGENT_API_KEY` in the launchd session" not in new_section
    assert "the `env` map of the host's MCP entry, or the launch environment of your user session" in old_section
    assert "the `env` map of the host's MCP entry, or the launch environment of your user session" in new_section
    hook = (
        "the session-start hook (`alice-memory-session-start`, which the host's hooks and the plugin run) is a separate process "
        "from the MCP server: an MCP server env map does not reach it."
    )
    assert hook in old_section and hook in new_section
    tail = old_section[old_section.index(hook) : old_section.index("The owner's own agent hosts need a key too")]
    assert tail in new_section
    notes = _flat(NOTES_PATH)
    assert "The v0.23.0 guide named no way to give the Codex MCP server the key." in notes
    assert (
        "The v0.23.0 guide told Hermes users to export the key in the launchd session, the terminal or the wrapper that starts "
        "the host, and said the config's `env` map is not the place for a secret."
    ) in notes


def test_the_codex_and_hermes_pages_say_what_the_notes_take_from_them() -> None:
    """Mutations, each one alone: read the working tree where the two pages are read, after a change that deletes the
    sentence of the Codex page about the names Codex passes to a stdio server, its ``env_vars`` line from the list a
    re-run keeps, its line about documented ``ALICE_`` env values, the sentence of the Hermes page that a re-run keeps
    ``ALICE_AGENT_API_KEY`` on ``mcp_servers.alice``, or the one that says the key is not printed; reword one of the
    sentences the test looks for.

    The guide points at both pages, and the notes say that a re-run of install keeps the entries. The pages are read as
    the v0.23.1 tag held them. Both are the same in v0.23.0, so reading that tag instead cannot fail this test.
    """

    codex = _tagged_flat("docs/integrations/codex.md")
    assert "Codex passes a stdio server only `HOME`, `PATH`, `LANG`, and a few other names." in codex
    assert "- `env_vars`: an array of strings, on one line or several" in _tagged(RELEASE_TAG, "docs/integrations/codex.md")
    assert "documented `ALICE_*` env values, and `ALICE_MEMORY_DATA_DIR`, copied byte for byte" in codex
    assert "A re-run keeps these keys when they already sit on Alice's table and Codex would load them:" in codex
    hermes = _tagged_flat("docs/integrations/hermes.md")
    assert "`alice-memory install --host hermes` keeps a documented Alice env value on `mcp_servers.alice`" in hermes
    assert "`ALICE_MCP_FULL_TOOLS`, `ALICE_MCP_LEGACY_TOOLS`, `ALICE_AGENT_API_KEY`," in hermes
    assert "so `ALICE_AGENT_API_KEY` and `ALICE_EMBEDDINGS_API_KEY` are not printed." in hermes


# ---------------------------------------------------------------------------
# What changed in the package, and what did not
# ---------------------------------------------------------------------------


def _py_files(root: Path) -> dict[str, bytes]:
    """Every ``.py`` file under ``root`` (the tracked kind: no caches, no installed metadata), by path relative to it."""

    found = {}
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if "__pycache__" in parts or any(part.endswith(".egg-info") for part in parts):
            continue
        found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _without_docstring(source: bytes, function: str) -> str:
    """The syntax tree of ``source`` as text, with the docstring of ``function`` taken out."""

    tree = ast.parse(source)
    seen = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            seen += 1
            assert ast.get_docstring(node) is not None
            node.body = node.body[1:]
    assert seen == 1
    return ast.dump(tree)


def test_the_docstring_of_own_env_is_the_only_change_under_the_package_source(v0230_tree: Path, v0231_tree: Path) -> None:
    """Mutations, each one alone: read the v0.23.0 export where the v0.23.1 source is read (no file differs, so the list
    of differing files is empty); read the working tree there (a tree that changes ``chunk_vector_cache.py``, as the
    tree after a later change does, puts a second file in the list); reword the notes' sentence that the file is the only
    one that differs from v0.23.0 and that its code is the same.

    The notes say that ``host_install.py`` is the only file under ``apps/api/src`` that differs from v0.23.0, that its
    code is the same, and that the docstring said install writes the agent key's environment reference. The v0.23.0 and
    v0.23.1 tags are exported and compared file by file, and the two versions of ``host_install.py`` are compared with
    the docstring of ``_own_env`` taken out. Both sides are tags, which cannot change, so a later change to the package
    source on main does not fail this test.
    """

    old_source = _py_files(v0230_tree / "apps" / "api" / "src")
    new_source = _py_files(v0231_tree / "apps" / "api" / "src")
    assert sorted(old_source) == sorted(new_source)
    differing = [name for name in old_source if old_source[name] != new_source[name]]
    assert differing == ["alicebot_api/host_install.py"]
    # Booleans, not a comparison of two large syntax trees: pytest explains a failed comparison of long strings with a diff
    # that takes minutes on a whole module.
    same_code = _without_docstring(old_source["alicebot_api/host_install.py"], "_own_env") == _without_docstring(
        new_source["alicebot_api/host_install.py"], "_own_env"
    )
    assert same_code, "host_install.py differs from v0.23.0 in more than the docstring of _own_env"
    old_alembic = _py_files(v0230_tree / "apps" / "api" / "alembic")
    new_alembic = _py_files(v0231_tree / "apps" / "api" / "alembic")
    assert sorted(old_alembic) == sorted(new_alembic)
    assert [name for name in old_alembic if old_alembic[name] != new_alembic[name]] == []

    def docstring(source: bytes) -> str:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef) and node.name == "_own_env":
                return " ".join((ast.get_docstring(node) or "").split())
        raise AssertionError("_own_env not found")

    assert "and the agent key's environment reference (a ${VARNAME} the host resolves, never a raw key)" in docstring(
        old_source["alicebot_api/host_install.py"]
    )
    assert docstring(new_source["alicebot_api/host_install.py"]) == (
        "The env value install writes for ``payload``: ALICE_MEMORY_DATA_DIR = its --data-dir. Install writes no agent key and no "
        "reference to one."
    )
    other = _section(_text(NOTES_PATH), "Other changes")
    assert "said that install writes the agent key's environment reference" in other
    assert "It is the only file under `apps/api/src` that differs from v0.23.0, and its code is the same." in other


_OWN_ENV_CODE = (
    "import importlib, json\n"
    "module = importlib.import_module('alicebot_api.' + 'host_install')\n"
    "cases = [{'args': ['mcp', '--data-dir', '/x/y']}, {'args': ['mcp']}, {'args': [1]}, {}, "
    "{'args': ['--from', 'alice-memory==1', 'alice-memory', 'mcp', '--data-dir', '/d']}]\n"
    "print(json.dumps([module._own_env(case) for case in cases]))\n"
)


def test_install_writes_the_same_environment_values_as_v0230_and_never_a_key(v0230_tree: Path, v0231_tree: Path) -> None:
    """Mutations, each one alone: run the helper of the working tree where the v0.23.1 export is run, after a change
    that makes ``_own_env`` of ``host_install.py`` add a reference to ``ALICE_AGENT_API_KEY`` or any other value; say in
    the notes that install writes the key's reference.

    The notes say that install writes the value of ``ALICE_MEMORY_DATA_DIR`` and no key and no reference to one, as in
    v0.23.0. The helper that decides the values is run on five payloads by the v0.23.0 export and by the v0.23.1 export,
    so a later change to it on main does not fail this test. The module is loaded with ``importlib`` so that this file
    does not import a host module, which the real-host workflow lists by path.
    """

    old = _python(v0230_tree / "apps" / "api" / "src", _OWN_ENV_CODE)
    new = _python(v0231_tree / "apps" / "api" / "src", _OWN_ENV_CODE)
    assert old.returncode == 0 and new.returncode == 0, (old.stderr, new.stderr)
    assert json.loads(old.stdout) == json.loads(new.stdout)
    assert json.loads(new.stdout)[0] == {"ALICE_MEMORY_DATA_DIR": "/x/y"}
    assert AGENT_API_KEY_ENV not in new.stdout and "${" not in new.stdout
    assert "Install writes the value of `ALICE_MEMORY_DATA_DIR` and no key and no reference to one, as in v0.23.0" in _flat(NOTES_PATH)


def _packages_in(where: Path) -> list[str]:
    """The packages ``setuptools.find_packages(where=...)`` finds: each directory under ``where`` that holds an
    ``__init__.py``, with one in every directory between it and ``where``. Written out because the environment the
    tests run in does not install setuptools."""

    found = []
    for init in sorted(where.rglob("__init__.py")):
        parts = init.parent.relative_to(where).parts
        if parts and all((where.joinpath(*parts[: depth + 1]) / "__init__.py").is_file() for depth in range(len(parts))):
            found.append(".".join(parts))
    return found


def test_the_package_holds_none_of_the_bench_script_the_workflows_or_the_tests(v0231_tree: Path) -> None:
    """Mutations, each one alone: read the working tree where the v0.23.1 export is read, after a change that adds
    ``scripts`` to the package list of ``pyproject.toml``; say in the notes that the other changes are inside the
    package.

    The notes say the unit test shards, the bench script and the publish limits are outside the PyPI package. The wheel
    is the packages under ``apps/api/src`` and ``workers``, and the source distribution adds the files ``MANIFEST.in``
    names; this test pins what decides it, as the v0.23.1 tag held it. The v0.23.0 tag holds the same, so reading it
    instead cannot fail this test; ``test_every_version_site_names_0231_at_the_tag_and_the_marketplace_pins_the_tag``
    pins that the export is the v0.23.1 tag.
    """

    setuptools_config = tomllib.loads((v0231_tree / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    assert setuptools_config["packages"]["find"]["where"] == ["apps/api/src", "workers"]
    assert "package-data" not in setuptools_config and "data-files" not in setuptools_config
    packages = [pkg for where in ("apps/api/src", "workers") for pkg in _packages_in(v0231_tree / where)]
    assert packages and not [pkg for pkg in packages if pkg.split(".")[0] in {"scripts", "tests", "apps", ".github"}]
    assert (v0231_tree / "MANIFEST.in").read_text(encoding="utf-8").split("\n") == [
        "include apps/api/alembic.ini",
        "include eval/fixtures/public_eval_suites.json",
        "recursive-include apps/api/alembic *.py",
        "",
    ]
    assert "The other changes are outside the PyPI package." in _flat(NOTES_PATH)


def _publish_limits(publish: str, rehearsal: str) -> tuple[object, list[object], list[str]]:
    publish_job = yaml.safe_load(publish)["jobs"]["build-and-verify"]
    rehearsal_jobs = yaml.safe_load(rehearsal)["jobs"]
    report = [step for job in rehearsal_jobs.values() for step in job["steps"] if "JOB_TIMEOUT_MINUTES" in (step.get("env") or {})]
    return (
        publish_job["timeout-minutes"],
        [job["timeout-minutes"] for job in rehearsal_jobs.values()],
        [str(step["env"]["JOB_TIMEOUT_MINUTES"]) for step in report],
    )


def _shards(workflow: str) -> list[dict[str, object]]:
    return yaml.safe_load(workflow)["jobs"]["python-unit-shards"]["strategy"]["matrix"]["include"]


def test_the_unit_tests_run_as_six_shards_and_the_publish_workflows_allow_200_minutes_up_from_180() -> None:
    """Mutations, each one alone: read the v0.23.0 tag where the publish workflows are read as the v0.23.1 tag held them
    (they hold 180); read the working tree where the shard matrix is read, after a change that renumbers or adds a shard;
    read the v0.23.1 tag where the v0.23.0 tag is read (it holds six shards, not five); reword the notes' sentence about
    the shards.

    The notes say that the unit tests run as six CI shard jobs instead of five, and that the first job of the publish
    workflow and the job of the rehearsal have a ``timeout-minutes`` of 200, up from 180. The v0.23.1 tag holds six
    shards and 200, and the v0.23.0 tag holds five shards and 180. Both are read from the tags, so a later change to the
    shards on main does not fail this test.
    """

    publish, rehearsal, report = _publish_limits(
        _tagged(RELEASE_TAG, ".github/workflows/publish-pypi.yml"),
        _tagged(RELEASE_TAG, ".github/workflows/publish-recheck-rehearsal.yml"),
    )
    assert (publish, rehearsal, report) == (200, [200], ["200"])
    old_publish, old_rehearsal, old_report = _publish_limits(
        _tagged(PRIOR_TAG, ".github/workflows/publish-pypi.yml"), _tagged(PRIOR_TAG, ".github/workflows/publish-recheck-rehearsal.yml")
    )
    assert (old_publish, old_rehearsal, old_report) == (180, [180], ["180"])
    shards = _shards(_tagged(RELEASE_TAG, ".github/workflows/tests.yml"))
    assert [entry["shard"] for entry in shards] == [1, 2, 3, 4, 5, 6]
    assert len(_shards(_tagged(PRIOR_TAG, ".github/workflows/tests.yml"))) == 5
    gates = json.loads(_tagged(RELEASE_TAG, "gates.json"))["ci_time"]
    assert gates["shard_count"] == 6 and "shard N of 6" in gates["job"]
    assert "The limit is 200 minutes (six unit shards of 20 minutes plus the 80 minute margin)" in _tagged_flat("RELEASING.md")
    other = _section(_text(NOTES_PATH), "Other changes")
    assert "The unit tests run as six CI shard jobs instead of five." in other
    assert (
        "The first job of `publish-pypi.yml` and the job of `publish-recheck-rehearsal.yml` have a `timeout-minutes` of 200, up from 180."
    ) in other


def test_the_bench_script_mints_its_keys_in_process_where_v0230_started_an_interpreter_per_key() -> None:
    """Mutations, each one alone: read the v0.23.0 tag where the new script is read (it starts a subprocess with
    ``-m alicebot_api.onramp`` and does not call ``onramp_main``); read the v0.23.1 tag where the old script is read;
    read the working tree where the new script is read, after a change that stops it calling ``onramp_main``; say in the
    notes that the bench starts one interpreter per key.

    The two tags are the sources of the old and the new behaviour, and they cannot change.
    """

    new = _tagged(RELEASE_TAG, "scripts/showcase_bench.py")
    old = _tagged(PRIOR_TAG, "scripts/showcase_bench.py")
    assert 'from alicebot_api.onramp import main as onramp_main' in new
    assert "exit_code = onramp_main(argv)" in new
    assert '"keys", "create"' in new
    assert '"-m", "alicebot_api.onramp"' not in new
    assert '"-m", "alicebot_api.onramp", "keys", "create"' in old
    assert "subprocess.run(" in old
    assert "`scripts/showcase_bench.py` mints its keys through the entry point of `alice-memory keys create` in process." in _flat(NOTES_PATH)


# ---------------------------------------------------------------------------
# The numbers that drift
# ---------------------------------------------------------------------------


_TOOL_COUNTS_CODE = (
    "import json, os\n"
    "from alicebot_api.mcp.registry import list_mcp_tools\n"
    "counts = {}\n"
    "for label, flags in (\n"
    "    ('default', {}),\n"
    "    ('full', {'ALICE_MCP_FULL_TOOLS': '1'}),\n"
    "    ('legacy', {'ALICE_MCP_FULL_TOOLS': '1', 'ALICE_MCP_LEGACY_TOOLS': '1'}),\n"
    "    ('all', {'ALICE_MCP_FULL_TOOLS': '1', 'ALICE_MCP_LEGACY_TOOLS': '1', 'ALICE_LEGACY_SURFACES': '1'}),\n"
    "):\n"
    "    for name, value in flags.items():\n"
    "        os.environ[name] = value\n"
    "    counts[label] = len(list_mcp_tools())\n"
    "    for name in flags:\n"
    "        os.environ.pop(name, None)\n"
    "print(json.dumps(counts))\n"
)


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(v0231_tree: Path) -> None:
    """The tool counts and the PostgreSQL revision are read from the code and the files of the v0.23.1 tag, so a note that
    drifts from them fails here, and a later change on main does not.

    Mutations, each one alone: change a count in the test; rename the revision in the notes; reword the sentence of the
    notes that says the tool counts are those of v0.23.0; read the working tree where the export is read, after a change
    that adds a migration after 20261011_0097 (the revision, and the file list compared with the v0.23.0 tag, fail). The
    two tags hold the same tools and the same migrations, so reading the v0.23.0 tag instead cannot fail this test.
    """

    done = _python(v0231_tree / "apps" / "api" / "src", _TOOL_COUNTS_CODE)
    assert done.returncode == 0, done.stderr
    # The counts of v0.23.0: this release adds no tool.
    assert json.loads(done.stdout) == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert "there is no new tool, command or setting, and there is no migration" in notes
    assert "None. No tool, command, setting or route is added or changed, and the tool counts are those of v0.23.0." in notes

    versions = v0231_tree / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    tagged = _tagged_listing(PRIOR_TAG, "apps/api/alembic/versions")
    assert sorted(name for name in tagged if name.endswith(".py")) == sorted(path.name for path in versions.glob("*.py"))
    assert sorted(name for name in tagged if name.startswith("2") and name.endswith(".py"))[-1] == head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes


# ---------------------------------------------------------------------------
# A SQLite vault opens in both versions
# ---------------------------------------------------------------------------

_BOOTSTRAP_CODE = (
    "import sys\n"
    "from alicebot_api.onramp import bootstrap_database, resolve_db_path\n"
    "bootstrap_database(resolve_db_path(data_dir=sys.argv[1], db=None), user_id='" + USER_ID + "', user_email='local@alice')\n"
)

_RECALL_CODE = (
    "import json, sys\n"
    "from alicebot_api.mcp.registry import call_mcp_tool\n"
    "from alicebot_api.mcp_tools import MCPRuntimeContext\n"
    "from alicebot_api.onramp import resolve_db_path, sqlite_url_for_path\n"
    "database = resolve_db_path(data_dir=sys.argv[1], db=None)\n"
    "context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id='" + USER_ID + "')\n"
    "call_mcp_tool(context, name='alice_recall', arguments={'query': 'kiln firing'})\n"
    "print(json.dumps({'answered': True}))\n"
)


def _keys(source_root: Path, data_dir: Path) -> list[str]:
    """The agent ids ``keys list --json`` shows on the vault, from the code under ``source_root``."""

    done = _alice(source_root, "keys", "list", "--json", "--data-dir", str(data_dir))
    assert done.returncode == 0, done.stderr
    parsed = json.loads(done.stdout)
    assert parsed["count"] == len(parsed["items"])
    return sorted(item["agent_id"] for item in parsed["items"])


def _mint(source_root: Path, data_dir: Path, agent_id: str) -> str:
    done = _alice(source_root, "keys", "create", "--agent-id", agent_id, "--profile", "admin_agent", "--data-dir", str(data_dir))
    assert done.returncode == 0, done.stderr
    raw = done.stdout.splitlines()[0].strip()
    assert raw.startswith("alice_")
    return raw


def _answers(source_root: Path, data_dir: Path, key: str | None) -> bool:
    return _python(source_root, _RECALL_CODE, str(data_dir), key=key).returncode == 0


def test_a_vault_written_by_one_version_opens_in_the_other_in_both_directions(
    tmp_path: Path, v0230_tree: Path, v0231_tree: Path
) -> None:
    """Mutations, each one alone: run the source of the working tree where the v0.23.1 export is run, after a change that
    adds a column ``scope_note TEXT NOT NULL`` to the ``agent_api_keys`` table in ``sqlite_schema.py`` (a vault the tree
    writes then refuses a key from v0.23.0); say in the notes that a vault does not open in the other version.

    A vault that v0.23.0 wrote is opened by the v0.23.1 export, which lists its key, mints another and answers a call
    that carries a key. A vault that the v0.23.1 export wrote is opened by the v0.23.0 export the same way. A call with no
    key is refused by both once the vault holds one. Both sides are tags, so a later change on main does not fail this
    test.
    """

    old_src = v0230_tree / "apps" / "api" / "src"
    new_src = v0231_tree / "apps" / "api" / "src"
    # A vault that v0.23.0 wrote, opened by this tree.
    written_by_old = tmp_path / "written-by-old"
    assert _python(old_src, _BOOTSTRAP_CODE, str(written_by_old)).returncode == 0
    old_key = _mint(old_src, written_by_old, "host-one")
    assert _keys(new_src, written_by_old) == ["host-one"]
    assert _answers(new_src, written_by_old, old_key)
    assert not _answers(new_src, written_by_old, None)
    new_key = _mint(new_src, written_by_old, "host-two")
    assert _keys(old_src, written_by_old) == ["host-one", "host-two"]
    assert _answers(old_src, written_by_old, new_key)
    # A vault that this tree wrote, opened by v0.23.0.
    written_by_new = tmp_path / "written-by-new"
    assert _python(new_src, _BOOTSTRAP_CODE, str(written_by_new)).returncode == 0
    first = _mint(new_src, written_by_new, "host-one")
    assert _keys(old_src, written_by_new) == ["host-one"]
    assert _answers(old_src, written_by_new, first)
    assert not _answers(old_src, written_by_new, None)
    second = _mint(old_src, written_by_new, "host-two")
    assert _keys(new_src, written_by_new) == ["host-one", "host-two"]
    assert _answers(new_src, written_by_new, second)
    assert "A vault that v0.23.0 wrote opens in v0.23.1, and a vault that v0.23.1 has opened opens in v0.23.0." in _flat(NOTES_PATH)


# ---------------------------------------------------------------------------
# The changelog, the limits and the release check
# ---------------------------------------------------------------------------


def test_the_headline_changes_of_the_notes_each_have_a_changelog_entry() -> None:
    """Mutations, each one alone: read the v0.23.0 tag where the section is read (it holds other entries); read the working
    tree there, after a change that puts an entry in the section; reword the notes' sentence about the shards.

    The section is the one the v0.23.1 tag held. A dated paragraph added to the changelog later is not part of it.
    """

    starts = (
        "The agent guide's section on agent keys for the SQLite vault (`docs/alpha/agent-integration.md`) now says how each host's MCP server gets `ALICE_AGENT_API_KEY`.",
        "Internal change, no behaviour change: the unit tests run as six parallel shard jobs instead of five,",
    )
    entries = _entries()
    assert len(entries) == 2
    for start in starts:
        assert len([entry for entry in entries if entry.startswith(start)]) == 1, start
    guide = [entry for entry in entries if entry.startswith(starts[0])][0]
    for needle in (
        "The v0.23.0 guide told Hermes users to export the key in the shell or the launch environment, and gave Codex no route.",
        "Codex passes a stdio server only `HOME`, `PATH`, `LANG` and a few other names (as the Codex page says)",
        "so a key exported in the shell that starts Codex does not reach Alice and every call from it is refused once the vault holds a key.",
        "The guide now names the alice table's `env_vars` (`env_vars = [\"ALICE_AGENT_API_KEY\"]`) or `env` for Codex, "
        "and the `env` map of `mcp_servers.alice` for Hermes, which the Hermes page already showed; install keeps each of them on a re-run.",
        "None of these routes has been run against a real host in this repository.",
    ):
        assert needle in guide, needle
    assert guide.endswith("No behaviour changes, and no migration is required.")
    internal = [entry for entry in entries if entry.startswith(starts[1])][0]
    for needle in (
        "`scripts/showcase_bench.py`) mints each team key by calling the entry point of `alice-memory keys create` in process",
        "now have a `timeout-minutes` of 200, up from 180",
    ):
        assert needle in internal, needle
    assert internal.endswith("No migration is required.")
    notes = _flat(NOTES_PATH)
    assert "The unit tests run as six CI shard jobs instead of five." in notes


def test_the_notes_name_the_limits_they_say_matter() -> None:
    """Mutations, each one alone: delete a clause of the last section; say that Hermes passes the shell's environment on;
    say that a key in a config file is protected from other processes of the same user; say the release changes what Alice
    does.
    """

    section = _section(_text(NOTES_PATH), "What this release does not claim")
    for needle in (
        "It does not claim that any of the routes works on a real host: the guide says none has been run against one in this repository.",
        "It does not claim that Hermes passes your shell's environment on to the server, only that this repository does not show it, so the guide tells you not to rely on an exported variable.",
        "It does not claim that a key in a config file is protected from other processes that run as your OS user.",
        "It does not claim any change in what Alice does: only a docstring differs in the package.",
    ):
        assert needle in section, needle
    security = _section(_text(NOTES_PATH), "Security")
    assert "The change to the guide moves where a key is kept for two hosts: in the host's config file, as plain text that your OS user can read." in security


def test_the_release_check_accepts_the_notes_and_the_changelog_before_the_tag(tmp_path: Path) -> None:
    """Mutations, each one alone: read the changelog from the v0.23.0 tag (it has no v0.23.1 heading); read the notes from
    the working tree (they say ``published``); read the changelog from the working tree, after a change that puts an
    entry under ``## Unreleased``, as the tree after a later change has. Either tree read is refused by the pre-tag rule.

    This runs the release check's own pre-tag rule on the files the v0.23.1 tag held: the changelog, the notes and the
    absence of the checksums file. The tag cannot change, so a later change on main does not fail this test, and a
    change that the check would have refused at the tag cannot have been missed.
    """

    (tmp_path / "docs" / "release").mkdir(parents=True)
    (tmp_path / "CHANGELOG.md").write_text(_tagged(RELEASE_TAG, "CHANGELOG.md"), encoding="utf-8")
    (tmp_path / NOTES_PATH).write_text(_tagged(RELEASE_TAG, NOTES_PATH), encoding="utf-8")
    assert not (tmp_path / CHECKSUMS_PATH).exists()
    done = subprocess.run(
        ["git", "cat-file", "-e", f"{RELEASE_TAG}:{CHECKSUMS_PATH}"], cwd=REPO_ROOT, capture_output=True, check=False
    )
    assert done.returncode != 0, "the tag holds a checksums file"
    assert release_check.validate_release_document_state(tmp_path, version="0.23.1", require_finalized=True) == []


def test_the_release_check_accepts_the_tree_and_its_pre_tag_mode_refuses_the_published_notes() -> None:
    """Mutations, each one alone: set a status of the notes back to pending; delete the checksums file; delete the dated
    heading; set one version site back to 0.23.0.

    ``release_check.py`` passes on the tree. Its ``--require-finalized-release-docs`` mode is the pre-tag rule and
    refuses a published tree on purpose, for the same three reasons as after v0.23.0: both statuses must be pending
    before the tag, and the checksums file must not exist on the pre-publication tag. Once main has an Unreleased
    CHANGELOG entry, the pre-tag mode also says the section must be empty for a tag. That is true of every later commit
    and is the only other issue allowed here.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    assert metadata.version == "0.23.1"
    assert release_check.validate_release_document_state(REPO_ROOT, version=metadata.version, require_finalized=False) == []
    published = [
        "release notes alice-release-state publication_status must be pending before tag",
        "release notes alice-release-state checksums_status must be pending before tag",
        "docs/release/v0.23.1-checksums.txt must not exist on the pre-publication tag",
    ]
    issues = release_check.validate_release_document_state(REPO_ROOT, version=metadata.version, require_finalized=True)
    assert [issue for issue in issues if issue in published] == published
    assert [issue for issue in issues if issue not in published] in (
        [],
        ["CHANGELOG.md Unreleased section must be empty for the release tag"],
    )
