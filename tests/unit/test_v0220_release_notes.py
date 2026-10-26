"""The v0.22.0 release notes carry the upgrade steps, the security wording and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its pending state, the six version sites, the dated changelog heading and the empty
Unreleased section, the security wording and the pending advisory identifier, the steps an upgrading user must act on,
the numbers that drift with the code (the tool counts, the PostgreSQL revision, the limits and defaults of the vectors)
and the claims about what changes. Those claims are run, not read: the endpoint rules, the opt-in values and the
off-by-default import against the module and a loopback endpoint; the policy decision of each kind of request to the
agent output route; and the v0.21.1 side of each comparison against an export of the v0.21.1 tag, which is made with
``git archive`` into a temporary folder. Each test names the mutation that must fail it.

The notes are not published yet. Their state is ``pending`` and there is no checksums file, because the release check
requires that before the tag. The change that follows publication moves the state to ``published`` and ``recorded``,
adds the checksums file, moves the marketplace pin to the tag and fills in the advisory identifier, and it changes the
first test, the version-site test and the advisory test below to say so, as the change after v0.21.1 did.
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
from alicebot_api.mcp.registry import list_mcp_tools
from alicebot_api.mcp.retrieval import recall_source_ranking
from alicebot_api.onramp import main as alice_memory_main
from alicebot_api.routers._vnext_shared import _vnext_agent_identity
from alicebot_api.routers.vnext_memories import VNextAgentOutputIngestRequest
from alicebot_api.source_chunk_vector_search import BREAKER_SECONDS, QUERY_EMBEDDING_TIMEOUT_SECONDS
from alicebot_api.source_embedding_policy import (
    DEFAULT_MAX_VECTORS,
    MIN_READABLE_CHARS,
    PREFIX_RULES,
    endpoint_is_loopback,
    source_embedding_settings,
)
from alicebot_api.source_ranking import SourceRanking
from alicebot_api.vnext_agent_control import evaluate_agent_policy
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import (
    DEFAULT_CACHE_MAX_MB,
    DEFAULT_SCOPED_CACHE_MAX_MB,
    DEFAULT_SCOPED_COPIES,
)
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
HEADING = "## v0.22.0 \u2014 2026-10-26"
# The notes are about 14,500 characters, and 17,500 leaves room for a correction and no more.
NOTES_CEILING = 17_500

# The v0.21.1 tag and the commit it points at: the marketplace file still pins them until the change after publication.
TAG_COMMIT = "8d0bac271164ad494687ab599a96499ddfbcce66"

CANDIDATE_LINE = "`v0.22.0` is the current release candidate. It is not published."
CANDIDATE_DOCS = (
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
# The v0.21.1 tag, exported into a temporary folder. The tests that compare with it run its code in a subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def v0211_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``apps/api/src`` and ``apps/api/alembic`` of the v0.21.1 tag, made with ``git archive``.

    A checkout without tags skips the tests that use it, and under ``CI`` they fail instead, the rule of
    ``test_published_release_notes_stay_immutable.py``.
    """

    _tagged("v0.21.1", "pyproject.toml")  # skips, or fails under CI, when the tag cannot be read
    done = subprocess.run(
        ["git", "archive", "v0.21.1", "apps/api/src", "apps/api/alembic"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    target = tmp_path_factory.mktemp("v0211")
    with tarfile.open(fileobj=BytesIO(done.stdout)) as archive:
        archive.extractall(target, filter="data")
    return target


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


def test_the_notes_have_the_exact_title_and_the_pending_state_and_no_checksums_file() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status to published or
    recorded, add a second state line, add ``docs/release/v0.22.0-checksums.txt``.

    The release check requires both statuses to be pending and the checksums file to be absent before the tag.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.22.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.22.0",'
        '"publication_status":"pending","checksums_status":"pending"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert not (REPO_ROOT / "docs/release/v0.22.0-checksums.txt").exists()


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,500 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_every_version_site_names_0220_and_the_marketplace_still_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.21.1; move the marketplace ``ref``
    to v0.22.0; change one hex digit of the marketplace ``sha``; change the install command in the notes.

    The six sites are the ones ``scripts/release_check.py`` requires. The marketplace file moves to the v0.22.0 tag
    only after publication, in the change that records the digests, so here it still names the latest published
    release and the commit its tag points at.
    """

    pyproject = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
    assert pyproject == "0.22.0"
    assert json.loads(_text("apps/web/package.json"))["version"] == pyproject
    assert json.loads(_text("packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_text("plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _text(name), name
        assert "alice-memory==0.21.1" not in _text(name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.21.1"
    assert marketplace["sha"] == TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.22.0 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_the_control_documents_name_the_candidate_and_keep_v0211_as_the_latest_published_release() -> None:
    """Mutations, each one alone: delete the candidate line from one control document or from the README; name
    v0.22.0 as the latest published release; delete the ``What `v0.22.0` Targets`` section of the current state, or
    move it below the section of v0.21.1; point its release notes link at another release.

    The statements about the latest published release, the baseline and the checksums change after publication.
    """

    for name in CANDIDATE_DOCS:
        text = _text(name)
        assert text.endswith("\n\n" + CANDIDATE_LINE + "\n"), name
        assert "`v0.21.1` is the latest published release" in text, name
        assert "`v0.22.0` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.21.1` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.21.0` is the immediately prior published release. " + CANDIDATE_LINE
    ) in readme
    state = _text("CURRENT_STATE.md")
    assert "\n## What `v0.22.0` Targets\n\n" + CANDIDATE_LINE + "\n" in state
    assert state.index("## What `v0.22.0` Targets") < state.index("## What `v0.21.1` Shipped")
    assert "\n## What `v0.21.1` Shipped\n\n`v0.21.1` is the latest published release" in state
    assert "- `v0.21.1` is the latest published release." in state
    assert "- [v0.22.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/" + NOTES_PATH + ")" in state


def test_the_changelog_has_the_dated_heading_and_an_empty_unreleased_section_right_before_it() -> None:
    """Mutations, each one alone: drop the heading; change its date; put an entry under ``## Unreleased``; put another
    section between v0.22.0 and v0.21.1; put the marker for the unreleased state back into the v0.22.0 section.

    ``release_check.py --require-finalized-release-docs`` asks for the same heading and an empty Unreleased section.
    It is empty on the release commit and may hold entries once main has moved on, and the change after
    publication drops that demand from here.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1] == "Unreleased\n"
    assert sections[2].startswith("v0.22.0 \u2014 2026-10-26\n")
    assert sections[3].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[4].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[2]
    assert "Unreleased (on" not in sections[3]


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
    # The identifier of the advisory is not known yet. The change after publication replaces the word ``pending``.
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


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool counts, the PostgreSQL revision and the limits of the vectors are read from the code and the files,
    so a change that leaves the notes behind fails here.

    Mutations, each one alone: add a tool to the default surface (the notes say none is added); add a migration after
    20261011_0097; change the default vault cap, the default cache limit, the query timeout, the pause after a failure
    or the least number of readable characters.
    """

    for name in ("ALICE_AGENT_API_KEY", "ALICE_MCP_FULL_TOOLS", "ALICE_MCP_LEGACY_TOOLS", "ALICE_LEGACY_SURFACES"):
        monkeypatch.delenv(name, raising=False)
    counts: dict[str, int] = {}
    for label, flags in (
        ("default", {}),
        ("full", {"ALICE_MCP_FULL_TOOLS": "1"}),
        ("legacy", {"ALICE_MCP_FULL_TOOLS": "1", "ALICE_MCP_LEGACY_TOOLS": "1"}),
        ("all", {"ALICE_MCP_FULL_TOOLS": "1", "ALICE_MCP_LEGACY_TOOLS": "1", "ALICE_LEGACY_SURFACES": "1"}),
    ):
        for name, value in flags.items():
            monkeypatch.setenv(name, value)
        counts[label] = len(list_mcp_tools())
        for name in flags:
            monkeypatch.delenv(name, raising=False)
    # The counts of v0.21.1: this release adds no tool.
    assert counts == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert (
        "No tool is added or removed: the default surface lists 3 tools, the full surface 12, and the legacy flags bring "
        "it to 74 and 77."
    ) in notes

    versions = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "PostgreSQL stays at revision `20261011_0097`." in notes

    assert DEFAULT_MAX_VECTORS == 250_000
    assert DEFAULT_CACHE_MAX_MB == 512
    assert (DEFAULT_SCOPED_CACHE_MAX_MB, DEFAULT_SCOPED_COPIES) == (256, 4)
    assert QUERY_EMBEDDING_TIMEOUT_SECONDS == 2.0
    assert BREAKER_SECONDS == 30.0
    assert MIN_READABLE_CHARS == 32
    assert [(rule.family, rule.document, rule.query) for rule in PREFIX_RULES] == [
        ("nomic-embed-text", "search_document: ", "search_query: ")
    ]
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


def test_the_settings_and_flags_the_notes_list_are_all_that_this_release_adds(v0211_tree: Path) -> None:
    """Mutations, each one alone: add a setting named ``ALICE_`` anywhere under ``apps/api/src``; add a flag to
    ``alice-memory reindex-embeddings``; delete one setting from the list in the notes.

    The names are read from every Python file under ``apps/api/src`` at the v0.21.1 tag and now. The flags are read from
    the parser of every ``alice-memory`` command and sub-command, built by the v0.21.1 export and by this tree.
    """

    def names(root: Path) -> set[str]:
        found: set[str] = set()
        for path in (root / "apps" / "api" / "src").rglob("*.py"):
            found |= set(re.findall(r"\b(ALICE_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8", errors="replace")))
        return found

    assert names(REPO_ROOT) - names(v0211_tree) == set(NEW_SETTINGS)
    assert names(v0211_tree) - names(REPO_ROOT) == set()
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
        [sys.executable, "-c", _FLAGS_CODE], env=_env_for(REPO_ROOT / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
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


def test_the_package_holds_none_of_the_bench_scripts_the_walkthrough_or_the_console() -> None:
    """Mutation: add ``scripts`` or ``apps/web`` to the package list of ``pyproject.toml``, or a line for either to
    ``MANIFEST.in``.

    The wheel is the packages under ``apps/api/src`` and ``workers``, and the source distribution adds the files
    ``MANIFEST.in`` names. A wheel and a source distribution of the tree were built to check this and hold no file from
    ``scripts/`` or ``apps/web``; this test pins what decides it.
    """

    from setuptools import find_packages

    setuptools_config = tomllib.loads(_text("pyproject.toml"))["tool"]["setuptools"]
    assert setuptools_config["packages"]["find"]["where"] == ["apps/api/src", "workers"]
    assert "package-data" not in setuptools_config and "data-files" not in setuptools_config
    packages = [pkg for where in ("apps/api/src", "workers") for pkg in find_packages(where=str(REPO_ROOT / where))]
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
    """Mutations, each one alone: take the ``blocked`` test out of ``ingest_vnext_agent_output``; move it below the call
    of the service; take ``VNextCaptureValidationError`` out of the route's ``except``; take the refusal out of
    ``VNextConnectorService.ingest_agent_output``.

    The PostgreSQL tests of the route count the rows of each refused request
    (``tests/integration/test_ingest_output_policy_postgres.py``) and ran against both trees when the notes were written;
    this test pins the shape of the code and the contrast with the tag.
    """

    route = "apps/api/src/alicebot_api/routers/vnext_memories.py"
    guard, ingest, caught = _guard_and_handlers(_route_function(_text(route)))
    assert guard is not None and ingest is not None and guard < ingest
    assert "VNextCaptureValidationError" in caught
    old_guard, old_ingest, old_caught = _guard_and_handlers(_route_function(_tagged("v0.21.1", route)))
    assert old_guard is None and old_ingest is not None
    assert "VNextCaptureValidationError" not in old_caught
    connectors = "apps/api/src/alicebot_api/vnext_connectors.py"
    assert "_refuse_blocked_policy_decision(policy_decision)" in _text(connectors)
    assert "_refuse_blocked_policy_decision" not in _tagged("v0.21.1", connectors)
    mcp = _text("apps/api/src/alicebot_api/mcp/capture_automation.py")
    assert 'if decision.decision == "blocked":' in mcp and "_raise_mcp_policy_blocked(blocked_decision)" in mcp
    assert 'if decision.decision == "blocked":' in _tagged("v0.21.1", "apps/api/src/alicebot_api/mcp/capture_automation.py")
    cli = _tagged("v0.21.1", "apps/api/src/alicebot_api/cli/capture.py")
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


def test_no_migration_the_alembic_folder_is_the_one_of_v0211(v0211_tree: Path) -> None:
    """Mutation: add a file to ``apps/api/alembic``, or change one.

    The PostgreSQL revision stays ``20261011_0097`` because the folder is the one of the tag, byte for byte.
    """

    def files(root: Path) -> dict[str, bytes]:
        base = root / "apps" / "api" / "alembic"
        return {str(path.relative_to(base)): path.read_bytes() for path in sorted(base.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}

    assert files(REPO_ROOT) == files(v0211_tree)
    assert files(REPO_ROOT)


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


def test_the_release_check_accepts_the_notes_and_the_changelog_before_the_tag() -> None:
    """Mutations, each one alone: set a status of the notes to published; add the checksums file; put an entry under
    ``## Unreleased``; delete the dated heading; set one version site back to 0.21.1.

    This runs the release check's own pre-tag rule on the real tree, so a change that the check would refuse at the tag
    fails on the pull request instead.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    assert metadata.version == "0.22.0"
    assert release_check.validate_release_document_state(REPO_ROOT, version=metadata.version, require_finalized=True) == []
