"""The v0.22.0 release notes carry the upgrade steps, the security wording and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its published state, the checksums record, the six version sites and the marketplace
pin, the dated changelog heading, the security wording and the pending advisory identifier, the steps an upgrading user
must act on, the numbers that drift (the tool counts, the PostgreSQL revision, the settings and flags, and the limits
and defaults of the vectors) and the claims about what changes. Those claims are run, not read: the endpoint rules, the
opt-in values and the off-by-default import against the module and a loopback endpoint, and the policy decision of each
kind of request to the agent output route. Each test names the mutation that must fail it.

What the notes say about the release itself is read at the v0.22.0 tag, and every comparison with v0.21.1 reads both of
its sides from the tags. Each tag is exported with ``git archive`` into a temporary folder, and the code of the export
is run in a subprocess. The Alembic folder, the settings, the flags, the tool counts, the limits of the vectors and the
code of the agent output route are therefore not read from the working tree, so a later change to ``main`` (a migration
edited in place, a new setting, a new tool) does not make a published claim about what v0.22.0 shipped fail. The
behaviour tests further down (the endpoint rules, the opt-in values, the policy decisions, the import and the vault that
v0.21.1 opens) still run the module of the working tree, as ``test_credential_marker_lengths.py`` does, and a later
change to that behaviour has to update them.

The notes are published. The change that followed publication moved their state to ``published`` and ``recorded``, added
the checksums file, moved the marketplace file to the v0.22.0 tag and its commit, named v0.22.0 as the latest published
release in the control documents and removed the release candidate line, as the change after v0.21.1 did, and added one
dated 2026-10-27 update beside the notes' own marketplace step. The advisory identifier is still ``pending``: a later
dated update adds it. The tagged lines of the notes and of the changelog section are held by
``test_published_release_notes_stay_immutable.py``. The release check's pre-tag mode refuses the tree now, and one test
below pins the three reasons.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.request
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import tomllib

import scripts.release_check as release_check
from alicebot_api.mcp.retrieval import recall_source_ranking
from alicebot_api.onramp import main as alice_memory_main
from alicebot_api.routers._vnext_shared import _vnext_agent_identity
from alicebot_api.routers.vnext_memories import VNextAgentOutputIngestRequest
from alicebot_api.source_embedding_policy import endpoint_is_loopback, source_embedding_settings
from alicebot_api.source_ranking import SourceRanking
from alicebot_api.vnext_agent_control import evaluate_agent_policy
from tests.unit.passage_vector_support import (
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MODEL_ENV,
    SEARCH_QUALITY_ENV,
    SOURCE_EMBEDDINGS_ENV,
    ScriptedServer,
    quiet_environment,
)
from tests.unit.test_published_release_notes_stay_immutable import _tagged

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.22.0-release-notes.md"
RELEASE_TAG = "v0.22.0"
PRIOR_TAG = "v0.21.1"
HEADING = "## v0.22.0 \u2014 2026-10-26"
# The notes are about 14,500 characters, and 17,500 leaves room for a correction and no more.
NOTES_CEILING = 17_500

# The v0.22.0 tag, the commit it points at, and the digests of the two files published for it.
CHECKSUMS_PATH = "docs/release/v0.22.0-checksums.txt"
TAG_COMMIT = "a6a5f49eebb7377f40b388f2ffe07290b05b85fb"
PRIOR_TAG_COMMIT = "8d0bac271164ad494687ab599a96499ddfbcce66"
WHEEL_SHA256 = "803f466454de8973ed64db47de9e3c366943fc8833ed3ae0b40fbd9a48018fdb"
SDIST_SHA256 = "c2fe674d9d475ad04f6ed56df3ed4d55a1aef96209dbb1f8d86f07bf7634296a"

CANDIDATE_LINE = "`v0.22.0` is the current release candidate. It is not published."
LATEST_DOCS = (
    "ARCHITECTURE.md",
    "PRODUCT_BRIEF.md",
    "ROADMAP.md",
    "RELEASING.md",
    "docs/vnext/README.md",
)

SECTIONS = [
    "What to do after upgrading",
    "The agent output route",
    "Passage vectors, an opt-in",
    "Other changes in the package",
    "Behaviour changes an integrator will notice",
    "Security",
    "What this release does not claim",
    "Install",
]

NEW_SETTINGS = (
    "ALICE_SOURCE_EMBEDDINGS",
    "ALICE_SOURCE_EMBEDDINGS_MAX_VECTORS",
    "ALICE_SOURCE_VECTOR_CACHE_MAX_MB",
    "ALICE_SOURCE_VECTOR_SCOPED_CACHE_MAX_MB",
    "ALICE_SOURCE_VECTOR_SCOPED_COPIES",
)


def _text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _flat(path: str) -> str:
    return " ".join(_text(path).split())


def _section(text: str, heading: str) -> str:
    """The body of a ``## `` section of ``text``, whitespace collapsed."""

    match = re.search(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match is not None, heading
    return " ".join(match.group("body").split())


def _changelog_section() -> str:
    changelog = _text("CHANGELOG.md")
    return changelog.split(HEADING)[1].split("\n## v0.21.1")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


# ---------------------------------------------------------------------------
# The two tags, each exported into a temporary folder. The tests that compare them run their code in a subprocess.
# ---------------------------------------------------------------------------


def _export(tag: str, factory: pytest.TempPathFactory, name: str) -> Path:
    """``apps/api/src`` and ``apps/api/alembic`` of ``tag``, made with ``git archive``.

    A checkout without tags skips the tests that use it, and under ``CI`` they fail instead, the rule of
    ``test_published_release_notes_stay_immutable.py``.
    """

    _tagged(tag, "pyproject.toml")  # skips, or fails under CI, when the tag cannot be read
    done = subprocess.run(
        ["git", "archive", tag, "apps/api/src", "apps/api/alembic"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    target = factory.mktemp(name)
    with tarfile.open(fileobj=BytesIO(done.stdout)) as archive:
        archive.extractall(target, filter="data")
    return target


@pytest.fixture(scope="module")
def v0211_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The code and the Alembic folder of the tag before this release, v0.21.1."""

    return _export(PRIOR_TAG, tmp_path_factory, "v0211")


@pytest.fixture(scope="module")
def v0220_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The code and the Alembic folder of the tag of this release, v0.22.0."""

    return _export(RELEASE_TAG, tmp_path_factory, "v0220")


_NUMBERS_CODE = (
    "import json, os\n"
    "from alicebot_api.mcp.registry import list_mcp_tools\n"
    "from alicebot_api.source_chunk_vector_search import BREAKER_SECONDS, QUERY_EMBEDDING_TIMEOUT_SECONDS\n"
    "from alicebot_api.source_embedding_policy import DEFAULT_MAX_VECTORS, MIN_READABLE_CHARS, PREFIX_RULES\n"
    "from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import (\n"
    "    DEFAULT_CACHE_MAX_MB, DEFAULT_SCOPED_CACHE_MAX_MB, DEFAULT_SCOPED_COPIES)\n"
    "counts = {}\n"
    "for label, flags in (\n"
    "    ('default', {}),\n"
    "    ('full', {'ALICE_MCP_FULL_TOOLS': '1'}),\n"
    "    ('legacy', {'ALICE_MCP_FULL_TOOLS': '1', 'ALICE_MCP_LEGACY_TOOLS': '1'}),\n"
    "    ('all', {'ALICE_MCP_FULL_TOOLS': '1', 'ALICE_MCP_LEGACY_TOOLS': '1', 'ALICE_LEGACY_SURFACES': '1'}),\n"
    "):\n"
    "    os.environ.update(flags)\n"
    "    counts[label] = len(list_mcp_tools())\n"
    "    for name in flags:\n"
    "        del os.environ[name]\n"
    "print(json.dumps({\n"
    "    'counts': counts,\n"
    "    'max_vectors': DEFAULT_MAX_VECTORS,\n"
    "    'cache_mb': DEFAULT_CACHE_MAX_MB,\n"
    "    'scoped': [DEFAULT_SCOPED_CACHE_MAX_MB, DEFAULT_SCOPED_COPIES],\n"
    "    'query_timeout': QUERY_EMBEDDING_TIMEOUT_SECONDS,\n"
    "    'breaker': BREAKER_SECONDS,\n"
    "    'min_chars': MIN_READABLE_CHARS,\n"
    "    'prefixes': [[rule.family, rule.document, rule.query] for rule in PREFIX_RULES],\n"
    "}))\n"
)


_FLAGS_CODE = (
    "import argparse, json\n"
    "from alicebot_api.onramp import build_parser\n"
    "found = {}\n"
    "def walk(parser, path):\n"
    "    for action in parser._actions:\n"
    "        if isinstance(action, argparse._SubParsersAction):\n"
    "            for name, sub in action.choices.items():\n"
    "                walk(sub, path + [name])\n"
    "        else:\n"
    "            found.setdefault(' '.join(path), []).extend(o for o in action.option_strings if o.startswith('--'))\n"
    "walk(build_parser(), [])\n"
    "print(json.dumps({k: sorted(set(v)) for k, v in found.items()}))\n"
)


def _env_for(source_root: Path) -> dict[str, str]:
    """The environment of a child process that imports ``alicebot_api`` from ``source_root``, with no ``ALICE_`` setting."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(source_root)
    return env


def _old(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``alice-memory`` with the arguments, run from the code of the v0.21.1 tag. Nothing of ``ALICE_`` is passed on."""

    code = "import sys; from alicebot_api.onramp import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run(
        [sys.executable, "-c", code, *args], env=_env_for(tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )


def _new(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = alice_memory_main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _notes_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "kiln.md").write_text(
        "# Kiln schedule\n\nThe kiln firing schedule moves to Tuesday because the glaze supplier delivers on Monday.\n",
        encoding="utf-8",
    )
    (folder / "pricing.md").write_text(
        "# Pricing note\n\nThe Keel plan price was set after the March review of customer interviews and seat cost.\n",
        encoding="utf-8",
    )
    return folder


def _tables(database: Path) -> set[str]:
    import sqlite3

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Identity, state, size and the six version sites
# ---------------------------------------------------------------------------


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status back to pending,
    add a second state line, delete the checksums file.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.22.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.22.0",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / CHECKSUMS_PATH).is_file()


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutations, each one alone: change one hex digit of either digest; swap the two file names; add a third
    line; write another version into the header or into the verify commands; delete a header line.

    The digests are the ones PyPI serves for 0.22.0 and the GitHub Release lists for its assets, read on
    2026-10-27 (wheel 1,977,474 bytes, source distribution 1,736,968 bytes). The header and the layout are the v0.21.1
    file's with the version replaced, so the two files stay in one format. Nothing offline compares the digests with
    PyPI: ``release_check.py --tag v0.22.0 --verify-pypi-artifacts`` does, with the two files downloaded.
    """

    old = _text("docs/release/v0.21.1-checksums.txt").splitlines()
    new = _text(CHECKSUMS_PATH).splitlines()
    assert len(old) == len(new) == 19
    assert [line.replace("0.21.1", "0.22.0") for line in old[:17]] == new[:17]
    assert new[0] == "# Alice v0.22.0: published artifact SHA-256 digests"
    assert new[17:] == [
        f"{WHEEL_SHA256}  alice_memory-0.22.0-py3-none-any.whl",
        f"{SDIST_SHA256}  alice_memory-0.22.0.tar.gz",
    ]
    assert _text(CHECKSUMS_PATH).endswith(".tar.gz\n")


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,500 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_the_marketplace_pins_the_published_release_and_the_notes_carry_its_install_lines() -> None:
    """Mutations, each one alone: set the marketplace ``ref`` back to v0.21.1; change one hex digit of the marketplace
    ``sha``; change the install command in the notes.

    The post-publication change moved the marketplace file to the v0.22.0 tag and the commit that tag points at, so it
    names the latest published release. The v0.22.0 tag itself still pins v0.21.1, and the plugin page says so. The tree
    has moved on to the version sites of the next release, which ``test_v0230_release_notes.py`` pins, so this test no
    longer reads them.
    """

    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.22.0"
    assert marketplace["sha"] == TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.22.0 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes
    # The copy of the marketplace file inside the tag still pins the release before it.
    inside = json.loads(_tagged(RELEASE_TAG, ".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert (inside["ref"], inside["sha"]) == (PRIOR_TAG, PRIOR_TAG_COMMIT)


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


def test_the_notes_say_beside_their_marketplace_step_that_main_now_pins_v0220() -> None:
    """Mutations, each one alone: delete the 2026-10-27 update; name v0.21.1 as what ``main`` pins in it; drop the
    sentence about the copy inside the tag; move it away from step 7 of the upgrade steps; glue it to the end of
    another paragraph; add a second dated update.

    Step 7 of the upgrade steps says the marketplace file on ``main`` pins the v0.21.1 tag commit until a change after
    the release moves it. That sentence stays as tagged, and the update after it says the change has been made, as the
    update beside step 6 of the v0.21.1 notes did.
    """

    update = (
        "**Update (2026-10-27):** v0.22.0 is published, and the marketplace file on `main` now pins the v0.22.0 tag "
        "commit, so the marketplace install runs v0.22.0. The copy inside the v0.22.0 tag still pins v0.21.1, so add "
        "the marketplace from the repository, not from a checkout of the tag."
    )
    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == update]
    assert len(positions) == 1, positions
    index = positions[0]
    assert blocks[index - 1].endswith(
        "7. **Claude Code plugin.** The marketplace file on `main` pins the v0.21.1 tag commit until a change after "
        "this release moves it, so a marketplace install runs v0.21.1 code until then."
    )
    assert blocks[index + 1] == "## The agent output route"
    assert _text(NOTES_PATH).count("Update (2026-10-27)") == 1
    assert sum(block.startswith("**Update (") for block in blocks) == 1


def test_the_control_documents_name_v0220_as_the_latest_published_release_and_v0211_as_the_prior_one() -> None:
    """Mutations, each one alone: put the candidate line back into one control document or into the README; name
    v0.21.1 as the latest published release in one of them; name v0.21.0 as the immediately prior release; set a
    checksums or release notes pointer back to v0.21.1; put ``Targets`` back in the heading of the current state; point
    the release notes link of the README or the vNext README at another release; set the lead of ``What `v0.21.1`
    Shipped`` back to the latest published release; drop the v0.22.0 tag from the tag history of the current state or
    set its pin sentence back to v0.21.1.

    The candidate line of a later release names that release and is that release's test to pin, so only the v0.22.0
    candidate line is refused here.
    """

    for name in LATEST_DOCS:
        text = _text(name)
        assert CANDIDATE_LINE not in text, name
        assert "`v0.22.0` is the latest published release" in text, name
        assert "`v0.21.1` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.22.0` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.21.1` is the immediately prior published release."
    ) in readme
    assert CANDIDATE_LINE not in readme
    assert "- [v0.22.0 release notes](docs/release/v0.22.0-release-notes.md)" in _text("README.md")
    assert "[latest published release notes](../release/v0.22.0-release-notes.md)" in _flat("docs/vnext/README.md")
    architecture = _flat("ARCHITECTURE.md")
    assert "`docs/release/v0.22.0-checksums.txt`. - **Prior published:** `v0.21.1` is the immediately prior published release" in architecture
    assert "artifact digests are in `docs/release/v0.21.1-checksums.txt`." in architecture
    brief = _flat("PRODUCT_BRIEF.md")
    assert "`v0.21.1` is the immediately prior published release" in brief
    assert "exact digests in `docs/release/v0.22.0-checksums.txt`." in brief
    roadmap = _flat("ROADMAP.md")
    assert (
        "`docs/release/v0.22.0-release-notes.md`, with artifact digests in `docs/release/v0.22.0-checksums.txt`. "
        "`v0.21.1` is the immediately prior published release."
    ) in roadmap
    state = _text("CURRENT_STATE.md")
    assert "What `v0.22.0` Targets" not in state
    assert (
        "\n## What `v0.22.0` Shipped\n\n`v0.22.0` is the latest published release and remains the install, checksum,\n"
        "and baseline reference.\n\nIt shipped the fixes and additions on `main` after `v0.21.1`."
    ) in state
    assert state.index("## What `v0.22.0` Shipped") < state.index("## What `v0.21.1` Shipped")
    assert "\n## What `v0.21.1` Shipped\n\n`v0.21.1` is the immediately prior published release.\n" in state
    assert "\n## What `v0.21.0` Shipped\n\n`v0.21.0` is an earlier published release.\n" in state
    assert "- `v0.22.0` is the latest published release." in state
    assert "`v0.21.1` is the immediately prior\n  published release." in state
    assert "`docs/release/v0.22.0-release-notes.md`\n- `docs/release/v0.22.0-checksums.txt`\n" in state
    assert "`v0.21.1` is the immediately prior published release; its records are" in state
    history = " ".join(state.split())
    assert (
        "The `v0.19.2` tag has the file too, still pinned to the `v0.19.0` tag commit, the `v0.20.0` tag has it still "
        "pinned to the `v0.19.2` tag commit, the `v0.20.2` tag has it still pinned to the `v0.20.0` tag commit, the "
        "`v0.21.0` tag has it still pinned to the `v0.20.2` tag commit, the `v0.21.1` tag has it still pinned to the "
        "`v0.21.0` tag commit, and the `v0.22.0` tag has it still pinned to the `v0.21.1` tag commit."
    ) in history
    assert "pins the plugin to the `v0.22.0` tag commit, so the plugin installs from that marketplace." in history
    assert "- [v0.22.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/" + NOTES_PATH + ")" in state


def test_the_install_and_checkout_lines_name_v0220_as_the_latest_release() -> None:
    """Mutations, each one alone: set the ``--tag`` of the headless install guide, the latest release tag it names, the
    checkout line of the quickstart or the baseline named by the reference paths page back to v0.21.1.

    The README's checkout line is pinned in ``test_readme_facts_live_in_the_docs.py``. The lines pinned here name the
    release a reader installs or checks out, so they move with each release. A comparison with v0.21.1 elsewhere in the
    docs is history and stays.
    """

    guide = _flat("docs/alpha/headless-ubuntu-install.md")
    assert "use the latest published release tag (`v0.22.0`) for an immutable deployment." in guide
    assert "with the latest published release tag (`v0.22.0`): `--tag v0.22.0`." in guide
    assert "v0.21.1" not in guide
    quickstart = _flat("docs/alpha/quickstart.md")
    assert "To run v0.22.0, run `git checkout v0.22.0` before `make setup`." in quickstart
    assert "git checkout v0.21.1" not in quickstart
    assert "on top of the latest published `v0.22.0` baseline." in _flat("docs/integrations/reference-paths.md")


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date; put another section between v0.22.0 and v0.21.1;
    put the marker for the unreleased state back into the v0.22.0 section.

    The Unreleased section is empty on the release commit. It may hold entries once main has moved on, so this test does
    not ask for it to be empty. It asks that the v0.23.0, v0.22.0, v0.21.1 and v0.21.0 sections follow it in that
    order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[2].startswith("v0.23.0 \u2014 2026-10-30\n")
    assert sections[3].startswith("v0.22.0 \u2014 2026-10-26\n")
    assert sections[4].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[5].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[3]
    assert "Unreleased (on" not in sections[4]


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest() -> None:
    """Mutation: put the marker for the unreleased state of v0.21.1 back into any document, docstring, test or case of
    the mutation manifest, with or without the word ``Unreleased``.

    The pattern is written as a regular expression so that this file does not hold the marker itself. It also reads
    the form without the word ``Unreleased``, which three documents carried. The published v0.18.0 notes carry two dated
    corrections that say ``not in v0.19.0``, and the v0.20.0 notes one that says ``not in v0.20.0``; they are other
    markers and immutable.
    """

    pattern = re.compile(r"(?:Unreleased\s*\(\s*)?on\s+main\s*,\s*not\s+in\s+v0\.21\.1", flags=re.IGNORECASE)
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
    for name in (
        "docs/benchmarks/showcase/what-changed-and-why.md",
        "docs/benchmarks/showcase/harness.md",
        "docs/alpha/mcp-tools.md",
    ):
        assert "From v0.22.0, " in _flat(name), name


# ---------------------------------------------------------------------------
# Wording and the steps
# ---------------------------------------------------------------------------


def test_the_notes_use_no_dash_and_only_the_allowed_security_wording() -> None:
    """Mutations, each one alone: write a dash into the notes; say "audited" outside the disclosure sentence;
    write independently, third-party or penetration; name an external review tool; write an advisory id; delete the
    review sentence, or only its ``internal adversarial review`` clause; delete or reword the pending identifier;
    write the identifier as a number.
    """

    text = _text(NOTES_PATH)
    assert "\u2014" not in text
    assert "\u2013" not in text
    flat = " ".join(text.split())
    disclosure = "No one outside the project has audited the code."
    assert flat.count(disclosure) == 1
    assert flat.count("audited") == flat.count(disclosure)
    review = "automated security scanning and internal adversarial review, findings triaged and fixed"
    assert flat.count(review) == 1
    assert "This release was checked with " + review + "." in flat
    lowered = flat.lower()
    for forbidden in ("independently", "third-party", "third party", "penetration", "pentest", "external review"):
        assert forbidden not in lowered, forbidden
    # The identifier of the advisory is not assigned yet. A later dated update adds it; the tagged sentence stays.
    assert flat.count("Advisory identifier: pending.") == 1
    assert "ghsa-" not in lowered and "cve-" not in lowered
    for banned in ("wiki", "tower", "reviewer", "security team"):
        assert banned not in lowered, banned
    assert "Security" in SECTIONS


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command, the revision, the flag or the
    setting a step names.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 8))
    for needle in (
        "`uv tool upgrade alice-memory`",
        "`pip install -U alice-memory`",
        "`alice-memory --version`",
        "**No migration.**",
        "PostgreSQL stays at revision `20261011_0097`.",
        "A SQLite vault that v0.21.1 wrote opens in v0.22.0 and needs no step.",
        "gets no new table and still opens in v0.21.1",
        "**PostgreSQL stack: check what posts to the agent output route.**",
        "a `read_only_agent` key, a `memory_proposal_agent` key",
        "a call with no key that declares no `permission_profile` and names an `agent_id` other than `hermes` and "
        "`openclaw`",
        "declare a `permission_profile` that may write (`trusted_local_agent` or `admin_agent`)",
        "A call that declares `read_only_agent` is refused.",
        "**Rows v0.21.1 wrote for a blocked request stay.**",
        "`metadata_json.policy_decision`",
        "its artifact is `needs_review`",
        "**To try the vectors**",
        "`ALICE_SOURCE_EMBEDDINGS=on`",
        "`alice-memory reindex-embeddings --sources`",
        "`ALICE_SEARCH_QUALITY=vectors`",
        "`alice-memory reindex-embeddings --sources --drop` to delete every chunk vector and the table",
        "**To go back to v0.21.1 after you used the vectors**",
        "its `alice-memory export` fails on it until they are dropped",
        "The marketplace file on `main` pins the v0.21.1 tag commit until a change after this release moves it",
    ):
        assert needle in steps, needle
    assert (
        steps.index("**No migration.**")
        < steps.index("**PostgreSQL stack: check what posts to the agent output route.**")
        < steps.index("**To try the vectors**")
        < steps.index("**To go back to v0.21.1 after you used the vectors**")
        < steps.index("**Claude Code plugin.**")
    )


def test_the_notes_name_the_limits_they_say_matter() -> None:
    """Mutations, each one alone: delete a clause of the last section; say the vectors retrieve better; say remote
    endpoints work; say a deleted vector is gone from the disk.
    """

    section = _section(_text(NOTES_PATH), "What this release does not claim")
    for needle in (
        "It does not claim that the vectors retrieve better.",
        "No measurement of them is published",
        "It does not claim that remote embeddings endpoints work for source text: they are not supported.",
        "It does not claim that a deleted vector is gone from the disk",
        "until a checkpoint and `VACUUM`",
        "a scrub committed after the text of a chunk was chosen can still be followed by that text reaching the endpoint "
        "on this machine, and the vector that comes back is not stored",
        "It does not remove the rows that v0.21.1 wrote for blocked agent output requests.",
    ):
        assert needle in section, needle
    summary = " ".join(_text(NOTES_PATH).split("\n\n")[1].split())
    assert "no gain from them has been measured, so this release does not claim better retrieval" in summary
    vectors = _section(_text(NOTES_PATH), "Passage vectors, an opt-in")
    assert "Both are off by default" in vectors
    assert "nothing in this release shows that they retrieve better. No measurement of them is published." in vectors
    assert "retrieve better" not in vectors.replace("shows that they retrieve better", "")


# ---------------------------------------------------------------------------
# The numbers that drift
# ---------------------------------------------------------------------------


def test_the_numbers_in_the_notes_are_the_ones_the_release_tag_has(v0220_tree: Path) -> None:
    """The tool counts, the PostgreSQL revision and the limits of the vectors are read from the code and the files at the
    ``v0.22.0`` tag, because the notes are published and describe that tag; a later change to main does not make them
    wrong. The code of the tag runs in a subprocess that prints the counts and the limits.

    Mutations, each one alone: set ``RELEASE_TAG`` to ``v0.21.1`` (the tagged code has no ``vectors`` limits then, and the
    subprocess fails to import them); add a tool to the default surface, add a migration after 20261011_0097 or change the
    default vault cap, the default cache limit, the query timeout, the pause after a failure or the least number of
    readable characters in the code at the tag (the notes say none is added, and none changed); change a number in the
    notes. Changing any of these in the working tree does not fail this test: it reads the tag, which is the point.
    """

    run = subprocess.run(
        [sys.executable, "-c", _NUMBERS_CODE],
        env=_env_for(v0220_tree / "apps" / "api" / "src"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert run.returncode == 0, run.stderr
    numbers = json.loads(run.stdout)
    # The counts of v0.21.1: this release adds no tool.
    assert numbers["counts"] == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert (
        "No tool is added or removed: the default surface lists 3 tools, the full surface 12, and the legacy flags bring "
        "it to 74 and 77."
    ) in notes

    versions = v0220_tree / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes

    assert numbers["max_vectors"] == 250_000
    assert numbers["cache_mb"] == 512
    assert numbers["scoped"] == [256, 4]
    assert numbers["query_timeout"] == 2.0
    assert numbers["breaker"] == 30.0
    assert numbers["min_chars"] == 32
    assert numbers["prefixes"] == [["nomic-embed-text", "search_document: ", "search_query: "]]
    vectors = _section(_text(NOTES_PATH), "Passage vectors, an opt-in")
    for needle in (
        "A vault keeps at most 250,000 vectors by default (`ALICE_SOURCE_EMBEDDINGS_MAX_VECTORS`).",
        "The copy in memory is limited to 512 MB by default (`ALICE_SOURCE_VECTOR_CACHE_MAX_MB`)",
        "a two second timeout, and after a failure the stage pauses for 30 seconds",
        "a chunk with fewer than 32 readable characters",
        "the document prefix of the model (`search_document: ` for nomic-embed-text)",
        "about 3 KB of memory for each chunk vector of 768 numbers",
    ):
        assert needle in vectors, needle
    assert 768 * 4 == 3072


def test_the_settings_and_flags_the_notes_list_are_all_that_this_release_adds(v0211_tree: Path, v0220_tree: Path) -> None:
    """Mutations, each one alone: add a setting named ``ALICE_`` anywhere under ``apps/api/src`` at the v0.22.0 tag; add a
    flag to ``alice-memory reindex-embeddings`` at the tag; delete one setting from the list in the notes.

    The names are read from every Python file under ``apps/api/src`` at the v0.21.1 tag and at the v0.22.0 tag. The flags
    are read from the parser of every ``alice-memory`` command and sub-command, built by the export of each tag. Nothing
    is read from the working tree, so a setting or a flag added to main later does not fail this test.
    """

    def names(root: Path) -> set[str]:
        found: set[str] = set()
        for path in (root / "apps" / "api" / "src").rglob("*.py"):
            found |= set(re.findall(r"\b(ALICE_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8", errors="replace")))
        return found

    assert names(v0220_tree) - names(v0211_tree) == set(NEW_SETTINGS)
    assert names(v0211_tree) - names(v0220_tree) == set()
    behaviour = _section(_text(NOTES_PATH), "Behaviour changes an integrator will notice")
    for name in NEW_SETTINGS:
        assert f"`{name}`" in behaviour, name
    assert "There are five new settings" in behaviour
    assert "`ALICE_SEARCH_QUALITY` takes the value `vectors`" in behaviour
    assert "`alice-memory reindex-embeddings` takes `--sources` and `--drop`." in behaviour

    old = subprocess.run(
        [sys.executable, "-c", _FLAGS_CODE], env=_env_for(v0211_tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )
    new = subprocess.run(
        [sys.executable, "-c", _FLAGS_CODE], env=_env_for(v0220_tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )
    assert old.returncode == 0 and new.returncode == 0, (old.stderr, new.stderr)
    flags_old, flags_new = json.loads(old.stdout), json.loads(new.stdout)
    assert set(flags_old) <= set(flags_new) and len(flags_new) > 15
    added = {
        command: set(flags_new[command]) - set(flags_old.get(command, []))
        for command in flags_new
        if set(flags_new[command]) != set(flags_old.get(command, []))
    }
    assert all(set(flags_old[command]) <= set(flags_new[command]) for command in flags_old)
    assert added == {"reindex-embeddings": {"--sources", "--drop"}}


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


def test_the_package_holds_none_of_the_bench_scripts_the_walkthrough_or_the_console() -> None:
    """Mutation: add ``scripts`` or ``apps/web`` to the package list of ``pyproject.toml``, or a line for either to
    ``MANIFEST.in``.

    The wheel is the packages under ``apps/api/src`` and ``workers``, and the source distribution adds the files
    ``MANIFEST.in`` names. A wheel and a source distribution of the tree were built to check this and hold no file from
    ``scripts/`` or ``apps/web``; this test pins what decides it.
    """

    setuptools_config = tomllib.loads(_text("pyproject.toml"))["tool"]["setuptools"]
    assert setuptools_config["packages"]["find"]["where"] == ["apps/api/src", "workers"]
    assert "package-data" not in setuptools_config and "data-files" not in setuptools_config
    packages = [pkg for where in ("apps/api/src", "workers") for pkg in _packages_in(REPO_ROOT / where)]
    assert packages and not [pkg for pkg in packages if pkg.split(".")[0] in {"scripts", "web", "apps"}]
    assert _text("MANIFEST.in").split("\n") == [
        "include apps/api/alembic.ini",
        "include eval/fixtures/public_eval_suites.json",
        "recursive-include apps/api/alembic *.py",
        "",
    ]
    for script in ("alice_bench.py", "showcase_bench.py", "showcase_walkthrough.py"):
        assert (REPO_ROOT / "scripts" / script).is_file(), script
    other = _section(_text(NOTES_PATH), "Other changes in the package")
    assert "are not part of the PyPI package" in other
    assert "hold no file from `scripts/` or `apps/web`" in other


# ---------------------------------------------------------------------------
# The agent output route
# ---------------------------------------------------------------------------


def _decision(agent_id: str, **fields: object):
    request = VNextAgentOutputIngestRequest(
        user_id=uuid4(), agent_id=agent_id, title="Kiln schedule note", content="The kiln schedule moves to Tuesday.", **fields
    )
    identity = _vnext_agent_identity(request)
    return evaluate_agent_policy(
        identity=identity,
        action="source.capture",
        domains=(request.domain,),
        sensitivity_allowed=(request.sensitivity,),
        project_scope=tuple(request.project_scope),
        write_policy="proposal_only" if request.propose_memory else None,
    )


@pytest.mark.parametrize(
    ("agent_id", "fields", "blocked"),
    [
        pytest.param("probe-agent", {"permission_profile": "read_only_agent"}, True, id="read_only_agent"),
        pytest.param("probe-agent", {"permission_profile": "memory_proposal_agent"}, True, id="memory_proposal_agent"),
        pytest.param("kiln-bot", {}, True, id="no key, no profile, another agent id"),
        pytest.param("kiln-bot", {"permission_profile": "read_only_agent"}, True, id="declared read_only_agent"),
        pytest.param("hermes", {}, False, id="hermes"),
        pytest.param("openclaw", {}, False, id="openclaw"),
        pytest.param("kiln-bot", {"permission_profile": "trusted_local_agent"}, False, id="declared trusted_local_agent"),
        pytest.param("kiln-bot", {"permission_profile": "admin_agent"}, False, id="declared admin_agent"),
        pytest.param("probe-agent", {"permission_profile": "trusted_local_agent", "propose_memory": True}, False, id="trusted, proposes"),
    ],
)
def test_the_kinds_of_request_the_notes_say_are_refused_are_the_ones_the_policy_blocks(
    agent_id: str, fields: dict[str, object], blocked: bool
) -> None:
    """Mutations, each one alone: let an agent id that is not ``hermes`` or ``openclaw`` default to a profile that may
    write; let ``read_only_agent`` write; let ``hermes`` default to ``read_only_agent``.

    Step 3 of the upgrade steps lists these requests. The route builds its decision from the same two calls.
    """

    decision = _decision(agent_id, **fields)
    assert (decision.decision == "blocked") is blocked, decision


def test_a_key_whose_domains_or_sensitivity_exclude_the_request_is_blocked() -> None:
    """Mutation: let ``project_scoped_agent`` capture a ``health`` source; let ``memory_proposal_agent`` capture a
    ``confidential`` one.
    """

    domain = _decision("scoped-agent", permission_profile="project_scoped_agent", domain="health", project_scope=["Alpha"])
    assert domain.decision == "blocked" and "all_requested_domains_restricted" in domain.reasons
    sensitivity = _decision("proposal-agent", permission_profile="memory_proposal_agent", sensitivity="confidential")
    assert sensitivity.decision == "blocked" and "restricted_sensitivity_filtered" in sensitivity.reasons


def _route_function(source: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    return next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "ingest_vnext_agent_output"
    )


def _guard_and_handlers(function: ast.FunctionDef) -> tuple[int | None, int | None, set[str]]:
    """The line of a ``blocked`` test that returns, the line of the first call of ``ingest_agent_output``, and the names
    the route's ``except`` clauses catch."""

    guard = ingest = None
    caught: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.If) and "blocked" in ast.unparse(node.test) and any(
            isinstance(child, ast.Return) for child in node.body
        ):
            guard = node.lineno
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "ingest_agent_output":
            ingest = node.lineno if ingest is None else min(ingest, node.lineno)
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            caught |= {part.id for part in ast.walk(node.type) if isinstance(part, ast.Name)}
    return guard, ingest, caught


def test_the_route_stops_on_a_blocked_decision_before_the_service_writes_and_v0211_did_not() -> None:
    """Mutations, each one alone: take the ``blocked`` test out of ``ingest_vnext_agent_output`` at the v0.22.0 tag; move
    it below the call of the service; take ``VNextCaptureValidationError`` out of the route's ``except``; take the refusal
    out of ``VNextConnectorService.ingest_agent_output``.

    The PostgreSQL tests of the route count the rows of each refused request
    (``tests/integration/test_ingest_output_policy_postgres.py``) and ran against both trees when the notes were written;
    this test pins the shape of the code and the contrast between the two tags. Both sides are read from the tags, so a
    later change to the route on main does not fail it.
    """

    route = "apps/api/src/alicebot_api/routers/vnext_memories.py"
    guard, ingest, caught = _guard_and_handlers(_route_function(_tagged(RELEASE_TAG, route)))
    assert guard is not None and ingest is not None and guard < ingest
    assert "VNextCaptureValidationError" in caught
    old_guard, old_ingest, old_caught = _guard_and_handlers(_route_function(_tagged(PRIOR_TAG, route)))
    assert old_guard is None and old_ingest is not None
    assert "VNextCaptureValidationError" not in old_caught
    connectors = "apps/api/src/alicebot_api/vnext_connectors.py"
    assert "_refuse_blocked_policy_decision(policy_decision)" in _tagged(RELEASE_TAG, connectors)
    assert "_refuse_blocked_policy_decision" not in _tagged(PRIOR_TAG, connectors)
    mcp_path = "apps/api/src/alicebot_api/mcp/capture_automation.py"
    mcp = _tagged(RELEASE_TAG, mcp_path)
    assert 'if decision.decision == "blocked":' in mcp and "_raise_mcp_policy_blocked(blocked_decision)" in mcp
    assert 'if decision.decision == "blocked":' in _tagged(PRIOR_TAG, mcp_path)
    cli = _tagged(PRIOR_TAG, "apps/api/src/alicebot_api/cli/capture.py")
    assert "ensure_policy_allowed(decision)" in cli
    route_notes = _section(_text(NOTES_PATH), "The agent output route")
    for needle in (
        "HTTP 403, the detail `agent policy blocked this action` and the policy decision",
        "It writes no source, chunk, artifact, provenance link or memory for it.",
        "It still stores the policy events of the refusal and records the agent identity",
        "`vNext agent output ingest request is invalid`",
        "v0.21.1 answered HTTP 500.",
        "already refused a blocked decision and wrote nothing",
        "A SQLite vault is not affected",
    ):
        assert needle in route_notes, needle


# ---------------------------------------------------------------------------
# The vectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://LOCALHOST:11434/v1",
        "http://embed.localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://127.3.4.5:11434/v1",
        "http://[::1]:11434/v1",
        "https://localhost/v1",
    ],
)
def test_the_endpoints_on_this_machine_are_the_ones_the_notes_list(base_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations, each one alone: stop reading ``.localhost`` names; stop reading ``::1``; read a name that resolves to
    loopback as local.
    """

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    assert endpoint_is_loopback(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.1:11434/v1",
        "http://2130706433:11434/v1",
        "http://0x7f000001:11434/v1",
        "http://[::ffff:127.0.0.1]:11434/v1",
        "http://192.0.2.1:11434/v1",
        "http://embeddings.example.com/v1",
        "http://localhost.example.com/v1",
        "ftp://localhost/v1",
        "",
    ],
)
def test_every_other_endpoint_is_not_on_this_machine(base_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations, each one alone: read ``127.1`` or an IPv4-mapped form as loopback; read a name that merely starts with
    ``localhost`` as local.
    """

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    assert not endpoint_is_loopback(base_url)


def test_an_endpoint_behind_a_proxy_is_not_on_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: stop asking whether a proxy of the environment would carry the request."""

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:3128"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda _host: False)
    assert not endpoint_is_loopback("http://127.0.0.1:11434/v1")


@pytest.mark.parametrize("value", ["1", "true", "yes", "enabled", "off", ""])
def test_the_values_the_notes_say_leave_the_opt_in_off_leave_it_off(value: str) -> None:
    """Mutation: let ``1``, ``true`` or ``yes`` turn the opt-in on."""

    assert not source_embedding_settings({SOURCE_EMBEDDINGS_ENV: value}).enabled
    assert source_embedding_settings({SOURCE_EMBEDDINGS_ENV: "on"}).enabled
    assert not source_embedding_settings({}).enabled


def test_the_value_vectors_is_known_now_and_v0211_refused_it(v0211_tree: Path) -> None:
    """Mutations, each one alone: stop accepting ``vectors`` in ``SourceRanking.from_environment``; make unset rank
    as ``vectors``.

    The v0.21.1 side runs the code of the tag: ``from_environment`` raises for a value it does not know, and
    ``alice_recall`` answers ``tool_request_failed`` for that error.
    """

    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "vectors"}).mode == "vectors"
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "passage"}).mode == "passage"
    assert SourceRanking.from_environment({SEARCH_QUALITY_ENV: "off"}).mode == "document"
    assert SourceRanking.from_environment({}, unset=SourceRanking.passage()).mode == "passage"
    env = _env_for(v0211_tree / "apps" / "api" / "src")
    code = (
        "from alicebot_api.source_ranking import SourceRanking\n"
        "try:\n"
        "    SourceRanking.from_environment({'ALICE_SEARCH_QUALITY': 'vectors'})\n"
        "except ValueError as exc:\n"
        "    print('refused')\n"
        "print(SourceRanking.from_environment({'ALICE_SEARCH_QUALITY': 'passage'}).mode)\n"
    )
    old = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert old.stdout.split() == ["refused", "passage"], old.stderr


def test_vectors_ranks_as_passage_on_postgresql_and_the_other_values_rank_as_they_did(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutations, each one alone: let ``recall_source_ranking`` return ``vectors`` for the PostgreSQL store; change what
    unset, ``passage`` or ``off`` rank as on SQLite.
    """

    sqlite_store = type("SQLiteVNextStore", (), {})()
    postgres_store = type("PostgresVNextStore", (), {})()
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "vectors")
    assert recall_source_ranking(sqlite_store, None).mode == "vectors"
    assert recall_source_ranking(postgres_store, None).mode == "passage"
    monkeypatch.delenv(SEARCH_QUALITY_ENV)
    assert recall_source_ranking(sqlite_store, None).mode == "passage"
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "passage")
    assert recall_source_ranking(sqlite_store, None).mode == "passage"
    monkeypatch.setenv(SEARCH_QUALITY_ENV, "off")
    assert recall_source_ranking(sqlite_store, None).mode == "document"
    vectors = _section(_text(NOTES_PATH), "Passage vectors, an opt-in")
    assert "Without any of them, and on PostgreSQL, `alice_recall` answers as `passage` does" in vectors
    assert "Unset, `passage` and `off` rank as they did, and nothing reads the vectors unless `vectors` is set." in vectors


def test_nothing_is_sent_by_default_and_with_the_opt_in_the_chunks_go_to_this_machine_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutations, each one alone: send chunks when only the two endpoint variables are set; send them to an endpoint that
    is not on this machine; leave the document prefix off; leave the table out of ``--drop``; let ``--drop`` send text.

    This runs ``import-markdown``, ``doctor`` and ``reindex-embeddings`` against a loopback endpoint that records every
    text it receives. The model name carries no prefix rule, so the prefix is asserted with a ``nomic-embed-text`` model.
    """

    quiet_environment(monkeypatch, opt_in=False)
    monkeypatch.chdir(tmp_path)
    folder = _notes_folder(tmp_path)
    with ScriptedServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "nomic-embed-text:v1.5")

        # Off: the endpoint variables are set, the opt-in is not.
        off = tmp_path / "off"
        code, out, _err = _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(off))
        assert code == 0 and "chunk_embeddings" not in json.loads(out)
        assert server.texts == [] and "source_chunk_embeddings" not in _tables(off / "memory.db")
        code, out, _err = _new(capsys, "doctor", "--data-dir", str(off))
        lines = out.splitlines()
        assert lines[lines.index("source embeddings: off") :][:5] == [
            "source embeddings: off",
            "chunks with a current vector: 0",
            "chunks pending a vector: 0",
            "chunks with a stale vector: 0",
            "chunks skipped: not counted while source embeddings are off",
        ]
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "yes")
        _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "yes"))
        assert server.texts == []

        # On: the chunks go to the endpoint on this machine, behind the document prefix, after the commit.
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        vault = tmp_path / "on"
        code, out, _err = _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(vault))
        receipt = json.loads(out)
        assert code == 0 and receipt["chunk_embeddings"]["embedded"] == len(server.texts) > 0
        assert receipt["chunk_embeddings"]["pending"] == 0
        assert all(text.startswith("search_document: ") for text in server.texts)
        assert "source_chunk_embeddings" in _tables(vault / "memory.db")

        # Delete: nothing is sent, the table goes, and v0.21.1's view of the vault is as before.
        sent = len(server.texts)
        code, out, _err = _new(capsys, "reindex-embeddings", "--sources", "--drop", "--data-dir", str(vault))
        assert code == 0 and json.loads(out)["dropped"] == sent
        assert len(server.texts) == sent and "source_chunk_embeddings" not in _tables(vault / "memory.db")

        # An endpoint that is not on this machine: nothing is sent and the chunks stay pending.
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, "http://127.1:9/v1")
        remote = tmp_path / "remote"
        code, out, _err = _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(remote))
        counted = json.loads(out)["chunk_embeddings"]
        assert code == 0 and counted["embedded"] == 0 and counted["pending"] > 0
        assert counted["pending_reasons"] == {"endpoint_not_on_this_machine": counted["pending"]}
        assert len(server.texts) == sent


def test_a_chunk_that_is_a_list_of_links_or_too_short_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutations, each one alone: send a chunk of links only; send a chunk shorter than the least; send on a dry run."""

    quiet_environment(monkeypatch, opt_in=True)
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "edge"
    folder.mkdir()
    (folder / "tiny.md").write_text("# T\n\nok\n", encoding="utf-8")
    (folder / "links.md").write_text("# Links\n\n[one](http://example.org/a) [two](http://example.org/b)\n", encoding="utf-8")
    (folder / "kiln.md").write_text(
        "# Kiln schedule\n\nThe kiln firing schedule moves to Tuesday because the glaze supplier delivers on Monday.\n",
        encoding="utf-8",
    )
    with ScriptedServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "scripted-embed")
        code, out, _err = _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "v"))
        counted = json.loads(out)["chunk_embeddings"]
        assert code == 0 and counted["embedded"] == 1 == len(server.texts)
        assert counted["skipped"] == {"link_only": 1, "too_short": 1}
        before = len(server.texts)
        code, out, _err = _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "dry"), "--dry-run")
        assert code == 0 and json.loads(out)["chunk_embeddings"]["reason"] == "dry_run" and len(server.texts) == before


def test_v0211_opens_a_vault_that_holds_vectors_but_cannot_export_it_until_they_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], v0211_tree: Path
) -> None:
    """Mutations, each one alone: make the table of vectors part of the schema that every vault gets (the vault that never
    turned the opt-in on then holds it); let ``--drop`` leave the table behind; make the export of v0.21.1 skip the table.

    The v0.21.1 side runs the code of the tag on vaults that the current code wrote: the vault that never turned the
    opt-in on opens and exports, the vault that holds vectors opens and its export fails, and after ``--drop`` it exports.
    """

    quiet_environment(monkeypatch, opt_in=False)
    monkeypatch.chdir(tmp_path)
    folder = _notes_folder(tmp_path)
    plain = tmp_path / "plain"
    assert _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(plain))[0] == 0
    assert "source_chunk_embeddings" not in _tables(plain / "memory.db")
    assert _old(v0211_tree, "doctor", "--data-dir", str(plain)).returncode == 0
    assert _old(v0211_tree, "export", "--data-dir", str(plain), "--out", str(tmp_path / "plain.jsonl")).returncode == 0

    with ScriptedServer() as server:
        monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
        monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "scripted-embed")
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
        embedded = tmp_path / "embedded"
        assert _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(embedded))[0] == 0
        assert "source_chunk_embeddings" in _tables(embedded / "memory.db")
        assert _old(v0211_tree, "doctor", "--data-dir", str(embedded)).returncode == 0
        failed = _old(v0211_tree, "export", "--data-dir", str(embedded), "--out", str(tmp_path / "embedded.jsonl"))
        assert failed.returncode != 0 and "export_failed" in failed.stderr
        # This tree exports it, leaves the vectors out and says how many it left.
        code, _out, err = _new(capsys, "export", "--data-dir", str(embedded), "--out", str(tmp_path / "new.jsonl"))
        assert code == 0 and "this vault holds" in err and "source chunk vectors" in err
        # A vault written by v0.21.1 opens here.
        written = tmp_path / "written"
        assert _old(v0211_tree, "import-markdown", "--from", str(folder), "--data-dir", str(written)).returncode == 0
        assert _new(capsys, "doctor", "--data-dir", str(written))[0] == 0
        assert "source_chunk_embeddings" not in _tables(written / "memory.db")
        # Drop, and v0.21.1 exports it.
        assert _new(capsys, "reindex-embeddings", "--sources", "--drop", "--data-dir", str(embedded))[0] == 0
        assert _old(v0211_tree, "export", "--data-dir", str(embedded), "--out", str(tmp_path / "after.jsonl")).returncode == 0
    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    assert "v0.21.1 opens a vault that holds chunk vectors, but its `alice-memory export` fails on it until they are dropped" in steps


def test_no_migration_the_alembic_folder_at_v0220_is_the_one_at_v0211(v0211_tree: Path, v0220_tree: Path) -> None:
    """Mutations, each one alone: add a file to ``apps/api/alembic`` at the v0.22.0 tag, or change one.

    The PostgreSQL revision stays ``20261011_0097`` because the folder at the v0.22.0 tag is the one at the v0.21.1 tag,
    byte for byte. Both sides are read from the tags, so a migration edited on main later (to repair it for a stricter
    database owner, for example) does not fail this test, which says what v0.22.0 shipped.
    """

    def files(root: Path) -> dict[str, bytes]:
        base = root / "apps" / "api" / "alembic"
        return {str(path.relative_to(base)): path.read_bytes() for path in sorted(base.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}

    assert files(v0220_tree) == files(v0211_tree)
    assert files(v0220_tree)


# ---------------------------------------------------------------------------
# The changelog and the release check
# ---------------------------------------------------------------------------


def test_the_headline_changes_of_the_notes_each_have_a_changelog_entry() -> None:
    """Mutations, each one alone: delete the changelog entry of a change the notes name, or reword its first words;
    delete the sentence of the embeddings entry that says what reads the vectors; delete a name from the notes.
    """

    starts = (
        "`POST /v0/vnext/agents/ingest-output` refuses a request that its agent policy blocks,",
        "The continuity page has a topic box for what changed.",
        "The full web dependency ",
        "The web console's test tooling now pins `undici` to 8.12.2",
        "The web console's lint tooling now pins `zod` to 4.6.6",
        "The chunks of imported and captured notes can be embedded, as an opt-in",
        "`ALICE_SEARCH_QUALITY=vectors` runs passage mode with the vectors of the chunks",
        "The showcase bench harness (`scripts/alice_bench.py` and `scripts/showcase_bench.py`)",
    )
    entries = _entries()
    assert len(entries) == len(starts) == 8
    for start in starts:
        assert len([entry for entry in entries if entry.startswith(start)]) == 1, start
    for entry in entries:
        assert "migration is required" in entry.lower(), entry[:60]
    ingest = [entry for entry in entries if entry.startswith(starts[0])][0]
    assert "v0.21.1 and earlier wrote the source and the `needs_review` artifact for a blocked request" in ingest
    assert "answers HTTP 400 with `vNext agent output ingest request is invalid`" in ingest
    assert "v0.21.1 and earlier answered HTTP 500" in ingest
    embeddings = [entry for entry in entries if entry.startswith(starts[5])][0]
    assert "Nothing reads the vectors unless `ALICE_SEARCH_QUALITY=vectors` is set (the next entry)" in embeddings
    assert "Nothing reads the vectors yet" not in embeddings
    assert "PostgreSQL is unchanged." in embeddings
    vectors = [entry for entry in entries if entry.startswith(starts[6])][0]
    assert "or on PostgreSQL, `alice_recall` answers exactly as `passage` does" in vectors
    assert "a build without this change rejects the value `vectors`" in vectors
    notes = _flat(NOTES_PATH)
    for name in ("showcase_walkthrough.py", "alice_bench.py", "showcase_bench.py", "`undici`", "`zod`", "`braces`", "topic box"):
        assert name in notes, name


def test_the_release_check_accepts_the_tree_and_its_pre_tag_mode_refuses_the_published_notes() -> None:
    """Mutations, each one alone: set a status of the notes back to pending; delete the checksums file; delete the
    dated heading.

    ``release_check.py`` passes on the tree. Its ``--require-finalized-release-docs`` mode is the pre-tag rule and
    refuses a published tree on purpose, for the same three reasons as after v0.21.1: both statuses must be pending
    before the tag, and the checksums file must not exist on the pre-publication tag. The package version of the tree is
    the next release's (``test_v0230_release_notes.py`` pins it), so the rule is run for the version of these notes.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    version = "0.22.0"
    assert metadata.version != version
    assert release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=False) == []
    published = [
        "release notes alice-release-state publication_status must be pending before tag",
        "release notes alice-release-state checksums_status must be pending before tag",
        "docs/release/v0.22.0-checksums.txt must not exist on the pre-publication tag",
    ]
    issues = release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=True)
    assert [issue for issue in issues if issue in published] == published
    # Once main has an Unreleased CHANGELOG entry, the pre-tag mode also says the section must be empty for a tag.
    # That is true of every later commit and is the only other issue allowed here.
    assert [issue for issue in issues if issue not in published] in (
        [],
        ["CHANGELOG.md Unreleased section must be empty for the release tag"],
    )
