"""The v0.23.1 release notes carry the upgrade steps, the wording about the agent key routes and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog, a page of the documents or a file of the tree. These tests
pin the parts a later edit could quietly weaken: the release identity and its pending state, the six version sites, the
dated changelog heading and the empty Unreleased section, the security wording, the steps an upgrading user must act on,
the sentences of the agent guide, the Codex page and the Hermes page that the notes repeat (and the text of the guide at
the v0.23.0 tag that the notes say was wrong), the numbers that drift (the tool counts, the PostgreSQL revision) and the
claim that the package holds one changed docstring and nothing else. Two claims are run: the docstring helper gives the
same values in v0.23.0 and here, and a SQLite vault written by one version opens in the other. Each comparison with
v0.23.0 reads the tag, which a test exports with ``git archive`` into a temporary folder and runs in a subprocess. Each
test names the mutation that must fail it.

The notes are not published yet. Their state is ``pending`` and there is no checksums file, because the release check
requires that before the tag. The change that follows publication moves the state to ``published`` and ``recorded``,
adds the checksums file, moves the marketplace pin to the tag and changes the first test and the version-site test below
to say so, as the change after v0.23.0 did.
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
from alicebot_api.mcp.registry import list_mcp_tools
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
from tests.unit.test_published_release_notes_stay_immutable import _tagged
from tests.unit.test_v0210_release_notes import _tagged_listing

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.23.1-release-notes.md"
GUIDE_PATH = "docs/alpha/agent-integration.md"
HEADING = "## v0.23.1 \u2014 2026-10-31"
# The notes are about 5,700 characters, and 7,000 leaves room for a correction and no more.
NOTES_CEILING = 7_000
USER_ID = "00000000-0000-0000-0000-000000000001"

# The v0.23.0 tag and the commit it points at: the marketplace file still pins them until the change after publication.
RELEASE_TAG = "v0.23.0"
TAG_COMMIT = "116d21775c931e69478f691ab0e3fd1b1e68015d"

CANDIDATE_LINE = "`v0.23.1` is the current release candidate. It is not published."
CANDIDATE_DOCS = (
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


def _changelog_section() -> str:
    changelog = _text("CHANGELOG.md")
    return changelog.split(HEADING)[1].split("\n## v0.23.0")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


# ---------------------------------------------------------------------------
# The v0.23.0 tag, exported into a temporary folder. The tests that compare with it run its code in a subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def v0230_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``apps/api/src`` and ``apps/api/alembic`` of the v0.23.0 tag, made with ``git archive``.

    A checkout without tags skips the tests that use it, and under ``CI`` they fail instead, the rule of
    ``test_published_release_notes_stay_immutable.py``.
    """

    _tagged(RELEASE_TAG, "pyproject.toml")  # skips, or fails under CI, when the tag cannot be read
    done = subprocess.run(
        ["git", "archive", RELEASE_TAG, "apps/api/src", "apps/api/alembic"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    target = tmp_path_factory.mktemp("v0230")
    with tarfile.open(fileobj=BytesIO(done.stdout)) as archive:
        archive.extractall(target, filter="data")
    return target


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


def test_the_notes_have_the_exact_title_and_the_pending_state_and_no_checksums_file() -> None:
    """Mutations, each one alone: change the title; move the state comment off the second line; set either status to
    published or recorded; add a second state line; add ``docs/release/v0.23.1-checksums.txt``.

    The release check requires both statuses to be pending and the checksums file to be absent before the tag.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.23.1 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.23.1",'
        '"publication_status":"pending","checksums_status":"pending"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert not (REPO_ROOT / "docs/release/v0.23.1-checksums.txt").exists()


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,000 characters into the notes; delete a section; swap two sections."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_every_version_site_names_0231_and_the_marketplace_still_pins_the_published_release() -> None:
    """Mutations, each one alone: set one version site or one plugin pin back to 0.23.0; move the marketplace ``ref`` to
    v0.23.1; change one hex digit of the marketplace ``sha``; change the install command in the notes.

    The six sites are the ones ``scripts/release_check.py`` requires. The marketplace file moves to the v0.23.1 tag only
    after publication, in the change that records the digests, so here it still names the latest published release and
    the commit its tag points at.
    """

    pyproject = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
    assert pyproject == "0.23.1"
    assert json.loads(_text("apps/web/package.json"))["version"] == pyproject
    assert json.loads(_text("packaging/mcpb/manifest.json"))["version"] == pyproject
    assert json.loads(_text("plugins/alice-memory/.claude-plugin/plugin.json"))["version"] == pyproject
    for name in ("plugins/alice-memory/.mcp.json", "plugins/alice-memory/hooks/hooks.json"):
        assert f"alice-memory=={pyproject}" in _text(name), name
        assert "alice-memory==0.23.0" not in _text(name), name
    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == RELEASE_TAG
    assert marketplace["sha"] == TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.23.1 && alice-memory install" in notes
    assert "With uv: `uvx alice-memory install`." in notes


def test_the_control_documents_name_the_candidate_and_keep_v0230_as_the_latest_published_release() -> None:
    """Mutations, each one alone: delete the candidate line from one control document or from the README; name v0.23.1
    as the latest published release; delete the ``What `v0.23.1` Targets`` section of the current state, or move it below
    the section of v0.23.0; point its release notes link at another release; say in it that the release changes behaviour.

    The statements about the latest published release, the baseline and the checksums change after publication.
    """

    for name in CANDIDATE_DOCS:
        text = _text(name)
        assert text.endswith("\n\n" + CANDIDATE_LINE + "\n"), name
        assert "`v0.23.0` is the latest published release" in text, name
        assert "`v0.23.1` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.23.0` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.22.0` is the immediately prior published release. " + CANDIDATE_LINE
    ) in readme
    state = _text("CURRENT_STATE.md")
    assert "\n## What `v0.23.1` Targets\n\n" + CANDIDATE_LINE + "\n" in state
    assert state.index("## What `v0.23.1` Targets") < state.index("## What `v0.23.0` Shipped")
    assert "\n## What `v0.23.0` Shipped\n\n`v0.23.0` is the latest published release" in state
    assert "- `v0.23.0` is the latest published release." in state
    assert "- [v0.23.1 release notes](https://github.com/samrusani/AliceMemory/blob/main/" + NOTES_PATH + ")" in state
    targets = " ".join(state[state.index("## What `v0.23.1` Targets") : state.index("## What `v0.23.0` Shipped")].split())
    assert "It is a patch release on `v0.23.0` and takes two changes from `main`." in targets
    assert "It changes no behaviour and adds no tool, command, setting or migration: PostgreSQL stays at revision `20261011_0097`." in targets


def test_the_changelog_has_the_dated_heading_and_an_empty_unreleased_section_right_before_it() -> None:
    """Mutations, each one alone: drop the heading; change its date; put an entry under ``## Unreleased``; put another
    section between v0.23.1 and v0.23.0; put the marker for the unreleased state back into a v0.23.1 entry.

    ``release_check.py --require-finalized-release-docs`` asks for the same heading and an empty Unreleased section.
    It is empty on the release commit and may hold entries once main has moved on, and the change after publication
    drops that demand from here.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1] == "Unreleased\n"
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
    changes.
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
    """Mutations, each one alone: delete the Codex bullet of the guide; drop its ``env_vars`` line, its ``env`` clause
    or its sentence that a re-run keeps both; delete the Hermes bullet, or say that Hermes reads the shell's
    environment; delete the sentence that the key is plain text in a config file, or the sentence that no route has been
    run against a real host; delete the sentence about the check from the host; reword the sentence about what a key does.

    The notes repeat these sentences. The guide is the source, so a change of the guide that leaves the notes behind
    fails here.
    """

    guide = _guide_section(_text(GUIDE_PATH))
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
    """Mutations, each one alone: delete the Codex sentence from the notes' first bullet, or the Hermes bullet; say in the
    notes that the v0.23.0 guide named a Codex route; change the notes' account of what the v0.23.0 guide told Hermes users.

    The tag is the source of what v0.23.0 said, and it cannot change. The Claude Code route, which the notes call
    unchanged, and the session-start hook bullet, which they call unchanged, are compared with the tag as well.
    """

    old = _tagged(RELEASE_TAG, GUIDE_PATH)
    old_section = _guide_section(old)
    assert "codex" not in old_section.lower()
    assert (
        "set the variable in the environment the host launches with (Hermes does not inherit your shell's environment and its "
        "config's env map is not the place for a secret), so export `ALICE_AGENT_API_KEY` in the launchd session, the terminal, "
        "or the wrapper that starts the host."
    ) in old_section
    new_section = _guide_section(_text(GUIDE_PATH))
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
    """Mutations, each one alone: delete the sentence of the Codex page about the names Codex passes to a stdio server;
    delete its ``env_vars`` line from the list a re-run keeps, or its line about documented ``ALICE_`` env values; delete
    the sentence of the Hermes page that a re-run keeps ``ALICE_AGENT_API_KEY`` on ``mcp_servers.alice``, or the one that
    says the key is not printed.

    The guide points at both pages, and the notes say that a re-run of install keeps the entries.
    """

    codex = _flat("docs/integrations/codex.md")
    assert "Codex passes a stdio server only `HOME`, `PATH`, `LANG`, and a few other names." in codex
    assert "- `env_vars`: an array of strings, on one line or several" in _text("docs/integrations/codex.md")
    assert "documented `ALICE_*` env values, and `ALICE_MEMORY_DATA_DIR`, copied byte for byte" in codex
    assert "A re-run keeps these keys when they already sit on Alice's table and Codex would load them:" in codex
    hermes = _flat("docs/integrations/hermes.md")
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


def test_the_docstring_of_own_env_is_the_only_change_under_the_package_source(v0230_tree: Path) -> None:
    """Mutations, each one alone: change a line of code in ``host_install.py``; change a line of another module under
    ``apps/api/src``; add a file under ``apps/api/src`` or ``apps/api/alembic``; change a migration; put the old docstring
    back; say in the notes that install writes a reference to the key.

    The notes say that ``host_install.py`` is the only file under ``apps/api/src`` that differs from v0.23.0, that its
    code is the same, and that the docstring said install writes the agent key's environment reference. The v0.23.0 tag
    is exported and compared file by file, and the two trees of ``host_install.py`` are compared with the docstring of
    ``_own_env`` taken out.
    """

    old_source = _py_files(v0230_tree / "apps" / "api" / "src")
    new_source = _py_files(REPO_ROOT / "apps" / "api" / "src")
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
    new_alembic = _py_files(REPO_ROOT / "apps" / "api" / "alembic")
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


def test_install_writes_the_same_environment_values_as_v0230_and_never_a_key(v0230_tree: Path) -> None:
    """Mutations, each one alone: make ``_own_env`` of ``host_install.py`` add ``ALICE_AGENT_API_KEY``, a reference to it
    or any other value; say in the notes that install writes the key's reference.

    The notes say that install writes the value of ``ALICE_MEMORY_DATA_DIR`` and no key and no reference to one, as in
    v0.23.0. The helper that decides the values is run on five payloads by the v0.23.0 export and by this tree. The
    module is loaded with ``importlib`` so that this file does not import a host module, which the real-host workflow
    lists by path.
    """

    old = _python(v0230_tree / "apps" / "api" / "src", _OWN_ENV_CODE)
    new = _python(REPO_ROOT / "apps" / "api" / "src", _OWN_ENV_CODE)
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


def test_the_package_holds_none_of_the_bench_script_the_workflows_or_the_tests() -> None:
    """Mutation: add ``scripts`` or ``tests`` to the package list of ``pyproject.toml``, or a line for either to
    ``MANIFEST.in``.

    The notes say the unit test shards, the bench script and the publish limits are outside the PyPI package. The wheel
    is the packages under ``apps/api/src`` and ``workers``, and the source distribution adds the files ``MANIFEST.in``
    names; this test pins what decides it.
    """

    setuptools_config = tomllib.loads(_text("pyproject.toml"))["tool"]["setuptools"]
    assert setuptools_config["packages"]["find"]["where"] == ["apps/api/src", "workers"]
    assert "package-data" not in setuptools_config and "data-files" not in setuptools_config
    packages = [pkg for where in ("apps/api/src", "workers") for pkg in _packages_in(REPO_ROOT / where)]
    assert packages and not [pkg for pkg in packages if pkg.split(".")[0] in {"scripts", "tests", "apps", ".github"}]
    assert _text("MANIFEST.in").split("\n") == [
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
    """Mutations, each one alone: set ``timeout-minutes`` of ``build-and-verify`` in ``publish-pypi.yml`` back to 180; set
    the job of ``publish-recheck-rehearsal.yml`` or its ``JOB_TIMEOUT_MINUTES`` back to 180; remove a shard from the matrix
    of ``tests.yml`` or add a seventh; set ``shard_count`` in ``gates.json`` back to 5; delete the sentence of
    ``RELEASING.md`` that gives the limit; reword the notes' sentence about the shards or the limits.

    The notes say that the unit tests run as six CI shard jobs instead of five, and that the first job of the publish
    workflow and the job of the rehearsal have a ``timeout-minutes`` of 200, up from 180. The v0.23.0 tag holds five
    shards and 180.
    """

    publish, rehearsal, report = _publish_limits(
        _text(".github/workflows/publish-pypi.yml"), _text(".github/workflows/publish-recheck-rehearsal.yml")
    )
    assert (publish, rehearsal, report) == (200, [200], ["200"])
    old_publish, old_rehearsal, old_report = _publish_limits(
        _tagged(RELEASE_TAG, ".github/workflows/publish-pypi.yml"), _tagged(RELEASE_TAG, ".github/workflows/publish-recheck-rehearsal.yml")
    )
    assert (old_publish, old_rehearsal, old_report) == (180, [180], ["180"])
    shards = _shards(_text(".github/workflows/tests.yml"))
    assert [entry["shard"] for entry in shards] == [1, 2, 3, 4, 5, 6]
    assert len(_shards(_tagged(RELEASE_TAG, ".github/workflows/tests.yml"))) == 5
    gates = json.loads(_text("gates.json"))["ci_time"]
    assert gates["shard_count"] == 6 and "shard N of 6" in gates["job"]
    assert "The limit is 200 minutes (six unit shards of 20 minutes plus the 80 minute margin)" in _flat("RELEASING.md")
    other = _section(_text(NOTES_PATH), "Other changes")
    assert "The unit tests run as six CI shard jobs instead of five." in other
    assert (
        "The first job of `publish-pypi.yml` and the job of `publish-recheck-rehearsal.yml` have a `timeout-minutes` of 200, up from 180."
    ) in other


def test_the_bench_script_mints_its_keys_in_process_where_v0230_started_an_interpreter_per_key() -> None:
    """Mutations, each one alone: make ``_mint_via_command`` of ``scripts/showcase_bench.py`` start a subprocess with
    ``-m alicebot_api.onramp`` again; stop it calling the entry point ``onramp_main`` of ``alice-memory``; say in the
    notes that the bench starts one interpreter per key.

    The tag is the source of the old behaviour.
    """

    new = _text("scripts/showcase_bench.py")
    old = _tagged(RELEASE_TAG, "scripts/showcase_bench.py")
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


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool counts and the PostgreSQL revision are read from the code and the files, so a change that leaves the
    notes behind fails here.

    Mutations, each one alone: add a tool to the default surface (the notes say none is added); add a migration after
    20261011_0097; rename the revision in the notes; delete the sentence of the notes that says the tool counts are
    those of v0.23.0.
    """

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
    # The counts of v0.23.0: this release adds no tool.
    assert counts == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert "there is no new tool, command or setting, and there is no migration" in notes
    assert "None. No tool, command, setting or route is added or changed, and the tool counts are those of v0.23.0." in notes

    versions = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    tagged = _tagged_listing(RELEASE_TAG, "apps/api/alembic/versions")
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


def test_a_vault_written_by_one_version_opens_in_the_other_in_both_directions(tmp_path: Path, v0230_tree: Path) -> None:
    """Mutations, each one alone: add a column ``scope_note TEXT NOT NULL`` to the ``agent_api_keys`` table in
    ``sqlite_schema.py`` (a vault this tree writes then refuses a key from v0.23.0); rename the column ``key_prefix`` of the
    same table; make ``keys list`` read a field that v0.23.0 does not write; say in the notes that a vault does not open
    in the other version.

    A vault that v0.23.0 wrote is opened by this tree, which lists its key, mints another and answers a call that carries
    a key. A vault that this tree wrote is opened by the v0.23.0 export the same way. A call with no key is refused by
    both once the vault holds one.
    """

    old_src = v0230_tree / "apps" / "api" / "src"
    new_src = REPO_ROOT / "apps" / "api" / "src"
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
    """Mutations, each one alone: delete the changelog entry of a change the notes name, or reword its first words; delete
    the sentence of the guide entry that says Codex had no route; delete the sentence that no route has been run against a
    real host; put an entry before the two; change the limit in the second entry.
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


def test_the_release_check_accepts_the_notes_and_the_changelog_before_the_tag() -> None:
    """Mutations, each one alone: set a status of the notes to published; add the checksums file; put an entry under
    ``## Unreleased``; delete the dated heading; set one version site back to 0.23.0.

    This runs the release check's own pre-tag rule on the real tree, so a change that the check would refuse at the tag
    fails on the pull request instead.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    assert metadata.version == "0.23.1"
    assert release_check.validate_release_document_state(REPO_ROOT, version=metadata.version, require_finalized=True) == []
