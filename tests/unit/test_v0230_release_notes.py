"""The v0.23.0 release notes carry the upgrade steps, the wording about keys and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog. These tests pin the parts a later edit could quietly
weaken: the release identity and its published state, the checksums record, the six version sites and the marketplace
pin, the dated changelog heading, the security wording, the exact sentence about what a key does and does not do, the steps an
upgrading user must act on, the numbers that drift (the tool counts, the PostgreSQL revision, the thirteen migration
files) and the claims about what changes. Those claims are run, not read: the three keys commands, the keyless
refusal, the brief and the session-start hook, the commands the owner runs without a key, what the export keeps, the
difference between a key and the keyless owner, the ten note passages of a recall, and the spellings of a memory id on
the SQLite vault. Each comparison with v0.22.0 runs the code of the v0.22.0 tag, which is exported with ``git archive``
into a temporary folder and run in a subprocess. Each test names the mutation that must fail it.

The PostgreSQL claims (the doors that answered a server error, the open-loop extraction, the context pack and the owner
migrations) are run by the integration tests named in ``test_the_postgresql_claims_have_integration_tests_that_run_in_ci``
against a local PostgreSQL 16, on this tree and on the tag. They are not run here.

The notes are published. The change that followed publication moved their state to ``published`` and ``recorded``,
added the checksums file, moved the marketplace file to the v0.23.0 tag and its commit, named v0.23.0 as the latest
published release in the control documents and removed the release candidate line, as the change after v0.22.0 did, and
added one dated 2026-10-30 update beside the notes' own marketplace step. The tagged lines of the notes and of the
changelog section are held by ``test_published_release_notes_stay_immutable.py``. The release check's pre-tag mode
refuses the tree now, and one test below pins the three reasons.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

import pytest
import tomllib

import scripts.release_check as release_check
from alicebot_api.mcp.registry import call_mcp_tool, list_mcp_tools
from alicebot_api.mcp.types import _RECALL_DEFAULT_LIMIT, _RECALL_DEFAULT_NOTE_PASSAGE_BUDGET, MCPNotPermittedError
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, main as alice_memory_main, resolve_db_path, sqlite_url_for_path
from tests.unit.test_published_release_notes_stay_immutable import _tagged

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.23.0-release-notes.md"
HEADING = "## v0.23.0 \u2014 2026-10-30"
# The notes are about 14,600 characters, and 17,500 leaves room for a correction and no more.
NOTES_CEILING = 17_500
USER_ID = "00000000-0000-0000-0000-000000000001"

# The v0.23.0 tag, the commit it points at (which the marketplace file pins now), the commit of the v0.22.0 tag that the
# copy of the marketplace file inside the v0.23.0 tag still pins, and the digests of the two files published for v0.23.0.
RELEASE_TAG = "v0.23.0"
CHECKSUMS_PATH = "docs/release/v0.23.0-checksums.txt"
TAG_COMMIT = "116d21775c931e69478f691ab0e3fd1b1e68015d"
PRIOR_TAG = "v0.22.0"
PRIOR_TAG_COMMIT = "a6a5f49eebb7377f40b388f2ffe07290b05b85fb"
WHEEL_SHA256 = "ad644f225da839d4fa3d20104feeb959c2fb6c8af1a16591f7f59338b7c5b847"
SDIST_SHA256 = "118ce1f8389d095bec76f4e0cdfa5c4966e1b26e95c99f292582581724271821"

CANDIDATE_LINE = "`v0.23.0` is the current release candidate. It is not published."
LATEST_DOCS = (
    "ARCHITECTURE.md",
    "PRODUCT_BRIEF.md",
    "ROADMAP.md",
    "RELEASING.md",
    "docs/vnext/README.md",
)

SECTIONS = [
    "What to do after upgrading",
    "Agent keys on the SQLite vault",
    "Server errors that are now answers",
    "Migrations for the self-hosted database owner",
    "Recall and import on SQLite",
    "Other changes in the package",
    "Behaviour changes an integrator will notice",
    "Security",
    "What this release does not claim",
    "Install",
]

# The sentence the owner fixed for every statement about keys.
KEYS_SENTENCE = (
    "keys tell Alice which agent is asking and limit what Alice returns to it; they do not stop a process that runs as "
    "your OS user from reading the vault file."
)

# The thirteen migration files that changed since v0.22.0, by revision.
CHANGED_MIGRATIONS = (
    "20260510_0067",
    "20260511_0070",
    "20260704_0072",
    "20260704_0076",
    "20260704_0077",
    "20260711_0083",
    "20260712_0084",
    "20260713_0085",
    "20260713_0086",
    "20260713_0088",
    "20260714_0090",
    "20260715_0091",
    "20260716_0092",
)

# The line that opens every brief the vault prints.
BRIEF_MARK = "Stored notes from Alice memory"


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
    return changelog.split(HEADING)[1].split("\n## v0.22.0")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


# ---------------------------------------------------------------------------
# The v0.22.0 tag, exported into a temporary folder. The tests that compare with it run its code in a subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def v0220_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``apps/api/src`` and ``apps/api/alembic`` of the v0.22.0 tag, made with ``git archive``.

    A checkout without tags skips the tests that use it, and under ``CI`` they fail instead, the rule of
    ``test_published_release_notes_stay_immutable.py``.
    """

    _tagged("v0.22.0", "pyproject.toml")  # skips, or fails under CI, when the tag cannot be read
    done = subprocess.run(
        ["git", "archive", "v0.22.0", "apps/api/src", "apps/api/alembic"], cwd=REPO_ROOT, capture_output=True, check=True
    )
    target = tmp_path_factory.mktemp("v0220")
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

# Mints a key with the code of the tree it runs from, as the only way to put one into a v0.22.0 vault, then asks the
# vault the same question a keyless MCP host asks.
_OLD_KEYED_VAULT_CODE = (
    "import json, sys\n"
    "from alicebot_api.mcp.registry import call_mcp_tool\n"
    "from alicebot_api.mcp_tools import MCPRuntimeContext\n"
    "from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path\n"
    "from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection\n"
    "from alicebot_api.vnext_agent_keys import create_agent_key\n"
    "user = '00000000-0000-0000-0000-000000000001'\n"
    "database = resolve_db_path(data_dir=sys.argv[1], db=None)\n"
    "bootstrap_database(database, user_id=user, user_email='local@alice')\n"
    "with sqlite_user_connection(database, user) as connection:\n"
    "    create_agent_key(SQLiteVNextStore(connection, user), user_id=user, agent_id='host', permission_profile='admin_agent')\n"
    "context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=user)\n"
    "answer = call_mcp_tool(context, name='alice_recall', arguments={'query': 'kiln firing'})\n"
    "print(json.dumps({'answered': True, 'keys': sorted(answer)}))\n"
)


def _env_for(source_root: Path) -> dict[str, str]:
    """The environment of a child process that imports ``alicebot_api`` from ``source_root``, with no ``ALICE_`` setting."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(source_root)
    return env


def _old(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``alice-memory`` with the arguments, run from the code of the v0.22.0 tag. Nothing of ``ALICE_`` is passed on."""

    code = "import sys; from alicebot_api.onramp import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run(
        [sys.executable, "-c", code, *args], env=_env_for(tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )


def _old_python(tree: Path, code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args], env=_env_for(tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )


def _new(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    capsys.readouterr()  # what an earlier call of the command printed is not this call's answer
    code = alice_memory_main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _hook(data_dir: Path, *, key: str | None = None) -> subprocess.CompletedProcess[str]:
    """``alice-memory-session-start`` with ``{}`` on stdin, bounded by a timeout, from this tree."""

    env = {name: value for name, value in os.environ.items() if not name.startswith("ALICE_")}
    env["PYTHONPATH"] = f"{REPO_ROOT}/apps/api/src:{REPO_ROOT}"
    if key is not None:
        env[AGENT_API_KEY_ENV] = key
    # Run as a module, which is how a host's hook command starts it. The test does not import the module, so the real-host
    # workflow does not need to list this file among the paths that start its heavy job.
    return subprocess.run(
        [sys.executable, "-m", "alicebot_api.session_start_hook", "--data-dir", str(data_dir)],
        env=env, input="{}\n", capture_output=True, text=True, timeout=120,
    )


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (AGENT_API_KEY_ENV, "ALICE_MCP_FULL_TOOLS", "ALICE_MCP_LEGACY_TOOLS", "ALICE_LEGACY_SURFACES", "ALICE_SEARCH_QUALITY"):
        monkeypatch.delenv(name, raising=False)


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


def _vault(tmp_path: Path, *, import_notes: bool = True) -> tuple[Path, Path]:
    """A SQLite vault in a temporary data directory. Returns (data directory, database file)."""

    data_dir = tmp_path / "vault"
    database = resolve_db_path(data_dir=str(data_dir), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    if import_notes:
        code = alice_memory_main(["import-markdown", "--from", str(_notes_folder(tmp_path)), "--data-dir", str(data_dir)])
        assert code == 0
    return data_dir, database


def _mint(capsys: pytest.CaptureFixture[str], data_dir: Path, agent_id: str = "host", profile: str = "admin_agent", *extra: str) -> str:
    capsys.readouterr()
    code, out, err = _new(capsys, "keys", "create", "--agent-id", agent_id, "--profile", profile, "--data-dir", str(data_dir), *extra)
    assert code == 0, err
    return out.splitlines()[0].strip()


def _context(database: Path) -> MCPRuntimeContext:
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _call(context: MCPRuntimeContext, name: str, arguments: dict, *, key: str | None, monkeypatch: pytest.MonkeyPatch):
    if key is None:
        monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(AGENT_API_KEY_ENV, key)
    try:
        return call_mcp_tool(context, name=name, arguments=arguments)
    finally:
        monkeypatch.delenv(AGENT_API_KEY_ENV, raising=False)


# ---------------------------------------------------------------------------
# Identity, state, size and the six version sites
# ---------------------------------------------------------------------------


def test_the_notes_are_published_with_the_exact_title_state_and_a_checksums_record() -> None:
    """Mutations, each one alone: change the title, move the state comment, set either status back to pending,
    add a second state line, delete the checksums file.
    """

    lines = _text(NOTES_PATH).splitlines()
    assert lines[0] == "# Alice v0.23.0 Release Notes"
    assert lines[1] == (
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"0.23.0",'
        '"publication_status":"published","checksums_status":"recorded"} -->'
    )
    assert sum("alice-release-state" in line for line in lines) == 1
    assert (REPO_ROOT / CHECKSUMS_PATH).is_file()


def test_the_checksums_file_records_the_two_published_digests_in_the_format_of_the_last_release() -> None:
    """Mutations, each one alone: change one hex digit of either digest; swap the two file names; add a third
    line; write another version into the header or into the verify commands; delete a header line.

    The digests are the ones PyPI serves for 0.23.0 and the GitHub Release lists for its assets, read on
    2026-10-30 (wheel 1,995,637 bytes, source distribution 1,751,701 bytes). The header and the layout are the v0.22.0
    file's with the version replaced, so the two files stay in one format. Nothing offline compares the digests with
    PyPI: ``release_check.py --tag v0.23.0 --verify-pypi-artifacts`` does, with the two files downloaded.
    """

    old = _text("docs/release/v0.22.0-checksums.txt").splitlines()
    new = _text(CHECKSUMS_PATH).splitlines()
    assert len(old) == len(new) == 19
    assert [line.replace("0.22.0", "0.23.0") for line in old[:17]] == new[:17]
    assert new[0] == "# Alice v0.23.0: published artifact SHA-256 digests"
    assert new[17:] == [
        f"{WHEEL_SHA256}  alice_memory-0.23.0-py3-none-any.whl",
        f"{SDIST_SHA256}  alice_memory-0.23.0.tar.gz",
    ]
    assert _text(CHECKSUMS_PATH).endswith(".tar.gz\n")


def test_the_notes_stay_under_the_size_ceiling_and_keep_their_sections_in_order() -> None:
    """Mutations, each one alone: paste 3,500 characters into the notes; delete or reorder a section."""

    text = _text(NOTES_PATH)
    assert len(text) < NOTES_CEILING
    headings = re.findall(r"(?m)^## (.+?)\s*$", text)
    assert headings == SECTIONS


def test_the_marketplace_pins_the_published_release_and_the_notes_carry_its_install_lines() -> None:
    """Mutations, each one alone: set the marketplace ``ref`` back to v0.22.0; change one hex digit of the marketplace
    ``sha``; change the install command in the notes; change the copy of the marketplace file inside the v0.23.0 tag.

    The post-publication change moved the marketplace file to the v0.23.0 tag and the commit that tag points at, so it
    names the latest published release. The v0.23.0 tag itself still pins v0.22.0, and the plugin page says so. The tree
    has moved on to the version sites of the next release, which ``test_v0231_release_notes.py`` pins, so this test no
    longer reads them.
    """

    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == RELEASE_TAG
    assert marketplace["sha"] == TAG_COMMIT
    notes = _flat(NOTES_PATH)
    assert "pip install alice-memory==0.23.0 && alice-memory install" in notes
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


def test_the_notes_say_beside_their_marketplace_step_that_main_now_pins_v0230() -> None:
    """Mutations, each one alone: delete the 2026-10-30 update; name v0.22.0 as what ``main`` pins in it; drop the
    sentence about the copy inside the tag; move it away from step 8 of the upgrade steps; glue it to the end of
    another paragraph; add a second dated update.

    Step 8 of the upgrade steps says the marketplace file on ``main`` pins the v0.22.0 tag commit until a change after
    the release moves it. That sentence stays as tagged, and the update after it says the change has been made, as the
    update beside step 7 of the v0.22.0 notes did.
    """

    update = (
        "**Update (2026-10-30):** v0.23.0 is published, and the marketplace file on `main` now pins the v0.23.0 tag "
        "commit, so the marketplace install runs v0.23.0. The copy inside the v0.23.0 tag still pins v0.22.0, so add "
        "the marketplace from the repository, not from a checkout of the tag."
    )
    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == update]
    assert len(positions) == 1, positions
    index = positions[0]
    assert blocks[index - 1].endswith(
        "8. **Claude Code plugin.** The marketplace file on `main` pins the v0.22.0 tag commit until a change after "
        "this release moves it, so a marketplace install runs v0.22.0 code until then."
    )
    assert blocks[index + 1] == "## Agent keys on the SQLite vault"
    assert _text(NOTES_PATH).count("Update (2026-10-30)") == 1
    assert sum(block.startswith("**Update (") for block in blocks) == 1


def test_the_control_documents_name_v0230_as_the_latest_published_release_and_v0220_as_the_prior_one() -> None:
    """Mutations, each one alone: put the candidate line back into one control document or into the README; name
    v0.22.0 as the latest published release in one of them; name v0.21.1 as the immediately prior release; set a
    checksums or release notes pointer back to v0.22.0; put ``Targets`` back in the heading of the current state; point
    the release notes link of the README or the vNext README at another release; set the lead of ``What `v0.22.0`
    Shipped`` back to the latest published release; drop the v0.23.0 tag from the tag history of the current state or
    set its pin sentence back to v0.22.0.

    The candidate line of a later release names that release and is that release's test to pin, so only the v0.23.0
    candidate line is refused here.
    """

    for name in LATEST_DOCS:
        text = _text(name)
        assert CANDIDATE_LINE not in text, name
        assert "`v0.23.0` is the latest published release" in text, name
        assert "`v0.22.0` is the latest published release" not in text, name
    readme = _flat("README.md")
    assert (
        "`v0.23.0` is the latest published release and remains the install, checksum, and release-note baseline. "
        "`v0.22.0` is the immediately prior published release."
    ) in readme
    assert CANDIDATE_LINE not in readme
    assert "- [v0.23.0 release notes](docs/release/v0.23.0-release-notes.md)" in _text("README.md")
    assert "[latest published release notes](../release/v0.23.0-release-notes.md)" in _flat("docs/vnext/README.md")
    architecture = _flat("ARCHITECTURE.md")
    assert "`docs/release/v0.23.0-checksums.txt`. - **Prior published:** `v0.22.0` is the immediately prior published release" in architecture
    assert "artifact digests are in `docs/release/v0.22.0-checksums.txt`." in architecture
    brief = _flat("PRODUCT_BRIEF.md")
    assert "`v0.22.0` is the immediately prior published release" in brief
    assert "exact digests in `docs/release/v0.23.0-checksums.txt`." in brief
    roadmap = _flat("ROADMAP.md")
    assert (
        "`docs/release/v0.23.0-release-notes.md`, with artifact digests in `docs/release/v0.23.0-checksums.txt`. "
        "`v0.22.0` is the immediately prior published release."
    ) in roadmap
    state = _text("CURRENT_STATE.md")
    assert "What `v0.23.0` Targets" not in state
    assert (
        "\n## What `v0.23.0` Shipped\n\n`v0.23.0` is the latest published release and remains the install, checksum,\n"
        "and baseline reference.\n\nIt shipped the fixes and additions on `main` after `v0.22.0`."
    ) in state
    assert state.index("## What `v0.23.0` Shipped") < state.index("## What `v0.22.0` Shipped")
    assert "\n## What `v0.22.0` Shipped\n\n`v0.22.0` is the immediately prior published release.\n" in state
    assert "\n## What `v0.21.1` Shipped\n\n`v0.21.1` is an earlier published release.\n" in state
    assert "\n## What `v0.21.0` Shipped\n\n`v0.21.0` is an earlier published release.\n" in state
    assert "- `v0.23.0` is the latest published release." in state
    assert "`v0.22.0` is the immediately prior\n  published release." in state
    assert "`docs/release/v0.23.0-release-notes.md`\n- `docs/release/v0.23.0-checksums.txt`\n" in state
    assert "`v0.22.0` is the immediately prior published release; its records are" in state
    history = " ".join(state.split())
    assert (
        "The `v0.19.2` tag has the file too, still pinned to the `v0.19.0` tag commit, the `v0.20.0` tag has it still "
        "pinned to the `v0.19.2` tag commit, the `v0.20.2` tag has it still pinned to the `v0.20.0` tag commit, the "
        "`v0.21.0` tag has it still pinned to the `v0.20.2` tag commit, the `v0.21.1` tag has it still pinned to the "
        "`v0.21.0` tag commit, the `v0.22.0` tag has it still pinned to the `v0.21.1` tag commit, and the `v0.23.0` tag "
        "has it still pinned to the `v0.22.0` tag commit."
    ) in history
    assert "pins the plugin to the `v0.23.0` tag commit, so the plugin installs from that marketplace." in history
    assert "- [v0.23.0 release notes](https://github.com/samrusani/AliceMemory/blob/main/" + NOTES_PATH + ")" in state


def test_the_install_and_checkout_lines_name_v0230_as_the_latest_release() -> None:
    """Mutations, each one alone: set the ``--tag`` of the headless install guide, the latest release tag it names, the
    checkout line of the quickstart or the baseline named by the reference paths page back to v0.22.0.

    The README's checkout line is pinned in ``test_readme_facts_live_in_the_docs.py``. The lines pinned here name the
    release a reader installs or checks out, so they move with each release. A comparison with v0.22.0 elsewhere in the
    docs is history and stays.
    """

    guide = _flat("docs/alpha/headless-ubuntu-install.md")
    assert "use the latest published release tag (`v0.23.0`) for an immutable deployment." in guide
    assert "with the latest published release tag (`v0.23.0`): `--tag v0.23.0`." in guide
    assert "v0.22.0" not in guide
    quickstart = _flat("docs/alpha/quickstart.md")
    assert "To run v0.23.0, run `git checkout v0.23.0` before `make setup`." in quickstart
    assert "git checkout v0.22.0" not in quickstart
    assert "on top of the latest published `v0.23.0` baseline." in _flat("docs/integrations/reference-paths.md")


def test_the_changelog_has_the_dated_heading_right_after_the_unreleased_section() -> None:
    """Mutations, each one alone: drop the heading; change its date; put another section between v0.23.0 and v0.22.0;
    put the marker for the unreleased state back into the v0.23.0 section.

    The Unreleased section is empty on the release commit. It may hold entries once main has moved on, so this test does
    not ask for it to be empty. It asks that the v0.23.1, v0.23.0, v0.22.0, v0.21.1 and v0.21.0 sections follow it in
    that order, one heading each.
    """

    changelog = _text("CHANGELOG.md")
    sections = changelog.split("\n## ")
    assert sections[1].startswith("Unreleased\n")
    assert sections[2].startswith("v0.23.1 \u2014 2026-10-31\n")
    assert sections[3].startswith("v0.23.0 \u2014 2026-10-30\n")
    assert sections[4].startswith("v0.22.0 \u2014 2026-10-26\n")
    assert sections[5].startswith("v0.21.1 \u2014 2026-10-23\n")
    assert sections[6].startswith("v0.21.0 \u2014 2026-10-22\n")
    assert changelog.count(HEADING) == 1
    assert "Unreleased (on" not in sections[3]
    assert "Unreleased (on" not in sections[4]


def test_no_marker_for_the_unreleased_state_is_left_in_the_documents_the_code_or_the_manifest() -> None:
    """Mutation: put the marker for the unreleased state of v0.22.0 back into any document, docstring, test or case of
    the mutation manifest, with or without the word ``Unreleased``.

    The pattern is written as a regular expression so that this file does not hold the marker itself. It also reads
    the form without the word ``Unreleased``, which three documents carried in the release before. The published
    v0.18.0 notes carry two dated corrections that say ``not in v0.19.0``, and the v0.20.0 notes one that says
    ``not in v0.20.0``; they are other markers and immutable.
    """

    pattern = re.compile(r"(?:Unreleased\s*\(\s*)?on\s+main\s*,\s*not\s+in\s+v0\.22\.0", flags=re.IGNORECASE)
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


# ---------------------------------------------------------------------------
# Wording and the steps
# ---------------------------------------------------------------------------


def test_the_notes_use_no_dash_and_only_the_allowed_security_wording() -> None:
    """Mutations, each one alone: write a dash into the notes; use the verb of the disclosure sentence elsewhere;
    write independently, third-party or penetration; name an external review tool; write an advisory id or the
    pending identifier of the last release; delete the review sentence, or only its ``internal adversarial review``
    clause; delete the sentence that says no advisory is planned.
    """

    text = _text(NOTES_PATH)
    assert "\u2014" not in text
    assert "\u2013" not in text
    flat = " ".join(text.split())
    verb = "au" + "dited"
    disclosure = f"No one outside the project has {verb} the code."
    assert flat.count(disclosure) == 1
    assert flat.count(verb) == flat.count(disclosure)
    review = "automated security scanning and internal adversarial review, findings triaged and fixed"
    assert flat.count(review) == 1
    assert "This release was checked with " + review + "." in flat
    lowered = flat.lower()
    for forbidden in ("independently", "third-party", "third party", "penetration", "pentest", "external review"):
        assert forbidden not in lowered, forbidden
    # No advisory is planned for this release: the sentence says so once, and no identifier or word for one is written.
    assert flat.count("no advisory is planned for this release") == 1
    assert lowered.count("advisory") == 1
    assert "identifier" not in lowered
    assert "ghsa-" not in lowered and "cve-" not in lowered
    for banned in ("wi" + "ki", "to" + "wer", "review" + "er", "security" + " team", "rul" + "ing"):
        assert banned not in lowered, banned
    assert "Security" in SECTIONS


def test_the_notes_word_keys_as_asking_and_limiting_and_never_as_isolation_or_a_fix() -> None:
    """Mutations, each one alone: delete or reword the sentence about keys; write isolation, a boundary, tenant,
    sandbox or vulnerability anywhere in the notes; call the keyless refusal a fix.

    The sentence is the owner's, word for word. v0.22.0 had no command to mint a key on the SQLite vault, so the
    refusal is described as a change of behaviour and no advisory is planned (see the test of the wording above).
    """

    flat = _flat(NOTES_PATH)
    keys = _section(_text(NOTES_PATH), "Agent keys on the SQLite vault")
    assert flat.count(KEYS_SENTENCE) == 1
    assert KEYS_SENTENCE in keys
    lowered = flat.lower()
    for word in ("isolat", "boundary", "tenant", "sandbox", "vulnerab"):
        assert word not in lowered, word
    security = _section(_text(NOTES_PATH), "Security")
    assert "It is a change of behaviour, and no advisory is planned for this release." in security
    assert "v0.22.0 had no command that mints a key on the SQLite vault" in security
    assert re.search(r"\bfix(es|ed)?\b", keys.lower()) is None
    assert "v0.22.0 had no command to mint an agent key on the SQLite vault: `alicebot agent keys create` needs PostgreSQL" in keys


def test_the_upgrade_steps_name_every_action_an_existing_user_takes_in_order() -> None:
    """Mutations, each one alone: delete one numbered step; swap two; drop the command, the variable, the revision or
    the rule a step names.
    """

    steps = _section(_text(NOTES_PATH), "What to do after upgrading")
    numbers = [int(number) for number in re.findall(r"(?:^| )(\d{1,2})\. \*\*", steps)]
    assert numbers == list(range(1, 9))
    for needle in (
        "`uv tool upgrade alice-memory`",
        "`pip install -U alice-memory`",
        "`alice-memory --version`",
        "**A SQLite vault with no keys needs nothing else.**",
        "It behaves as in v0.22.0",
        "**Once you mint a key, every agent host needs one.**",
        "`alice-memory keys create --agent-id <host> --profile <profile>` prints the raw key once",
        "an MCP call that carries no `ALICE_AGENT_API_KEY` is refused, reads included, and the legacy tools are refused too",
        "in the launch environment of the host for the session-start hook",
        "the `env` map of an MCP server does not reach it",
        "the hook and `alice-memory brief` print nothing and exit 0",
        "mint an unbound `admin_agent` key for each",
        "**The commands you run yourself keep running as the owner and take no key**",
        "`import`, `export`, `doctor`, `sleep`, `sleep-proposals`, `import-markdown`, `import-chatgpt`, `labels`, "
        "`sources`, `project` and `keys`",
        "**Mint keys again after an import.**",
        "The JSONL export drops keys",
        "A copy of the vault file taken after a checkpoint keeps its keys",
        "Revoking the last active key makes the vault keyless again",
        "A revoked key is refused on its next call",
        "**PostgreSQL stack: no step.**",
        "the revision stays `20261011_0097`",
        "run it again with this version",
        "The marketplace file on `main` pins the v0.22.0 tag commit until a change after this release moves it",
    ):
        assert needle in steps, needle
    assert (
        steps.index("**A SQLite vault with no keys needs nothing else.**")
        < steps.index("**Once you mint a key, every agent host needs one.**")
        < steps.index("**The commands you run yourself keep running as the owner and take no key**")
        < steps.index("**Mint keys again after an import.**")
        < steps.index("**To return to a vault with no keys,**")
        < steps.index("**PostgreSQL stack: no step.**")
        < steps.index("**Claude Code plugin.**")
    )


def test_the_notes_name_the_limits_they_say_matter() -> None:
    """Mutations, each one alone: delete a clause of the last section; say the vectors answer better; say keys keep an
    agent away from your data; say the archived-source difference does not exist.
    """

    section = _section(_text(NOTES_PATH), "What this release does not claim")
    for needle in (
        "It does not claim that keys keep an agent away from your data.",
        "It does not claim that an unbound `admin_agent` key reads everything the keyless owner reads",
        "the evidence of an archived source is one exception, set out above",
        "It does not claim that the vectors answer better.",
        "neither of two model families answered measurably better",
        "the gemma4 family lost 5 of 46 exact-term questions with vectors",
        "It does not claim that the import speed holds on another machine or another folder.",
        "It does not claim that the thirteen migrations repair a database that already ran their old bodies as the owner "
        "and wrote nothing",
    ):
        assert needle in section, needle
    keys = _section(_text(NOTES_PATH), "Agent keys on the SQLite vault")
    assert (
        "An unbound `admin_agent` key does not get the evidence of a source that was archived, which the keyless owner gets."
        in keys
    )
    assert "`alice_explain` is refused to the key" in keys


def test_the_published_comparison_page_says_what_the_notes_take_from_it() -> None:
    """Mutations, each one alone: delete the results page of the passage vectors comparison; delete one of the two
    sentences of its ``Both families`` section that the notes summarise; change the notes' link.

    The notes of v0.22.0 said that no measurement of the vectors was published. The pages of the comparison were added
    to ``main`` after that release, so this release states what they found instead of repeating the old sentence.
    """

    page = _flat("docs/benchmarks/showcase/phase1-passage-vectors-results.md")
    both = page.split("## Both families")[1].split("## Departures")[0]
    assert "passage vectors make Alice's search find a fact stated in other words more often" in both
    assert "neither family answers measurably better with vectors" in both
    assert "gemma4 lost 5 of 46 exact-term questions with vectors" in both
    notes = _flat(NOTES_PATH)
    assert "([results](../benchmarks/showcase/phase1-passage-vectors-results.md))" in notes
    assert "No measurement of them is published" not in notes


# ---------------------------------------------------------------------------
# The numbers that drift
# ---------------------------------------------------------------------------


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool counts, the recall limits and the PostgreSQL revision are read from the code and the files, so a change
    that leaves the notes behind fails here.

    Mutations, each one alone: add a tool to the default surface (the notes say none is added); add a migration after
    20261011_0097; change the default note passage budget, the default recall limit, or a number of the import speed
    in the notes.
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
    # The counts of v0.22.0: this release adds no tool.
    assert counts == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert (
        "No tool is added or removed: the default surface lists 3 tools, the full surface 12, and the legacy flags bring "
        "it to 74 and 77."
    ) in notes

    versions = REPO_ROOT / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "the revision stays `20261011_0097`" in notes
    assert "the head is `20261011_0097`, as in v0.22.0" in notes

    assert (_RECALL_DEFAULT_LIMIT, _RECALL_DEFAULT_NOTE_PASSAGE_BUDGET) == (8, 10)
    recall = _section(_text(NOTES_PATH), "Recall and import on SQLite")
    assert "returns ten note passages by default in passage and vectors mode, where it returned eight" in recall
    assert "Memory results stay at eight" in recall
    entry = [item for item in _entries() if item.startswith("The MCP tool `alice_recall` returns ten note passages")][0]
    for number in ("13,104 bytes", "10,617"):
        assert number in entry and number in recall, number
    speed = [item for item in _entries() if item.startswith("`alice-memory import-markdown` on a SQLite vault runs")][0]
    for number in ("about 213 s", "about 321 s", "13,400 Markdown files", "149 MB", "104,797 chunks", "about 535 MB", "about 480 MB"):
        assert number in speed, number
    for number in ("about 213 s", "about 321 s", "13,400 files", "149 MB", "104,797", "535 MB to 480 MB"):
        assert number in recall, number
    # No hardware, machine or new measurement is added to the notes.
    for word in ("Apple", "M3", "128 GB", "SSD", "Python 3.12", "SQLite 3.49"):
        assert word not in notes, word


def test_the_thirteen_changed_migrations_are_the_ones_the_notes_name_and_nothing_else_moved(v0220_tree: Path) -> None:
    """Mutations, each one alone: add a file to ``apps/api/alembic``; change a migration the notes do not name; take
    the ``NO FORCE ROW LEVEL SECURITY`` statement out of a migration the notes name; rename a revision in the notes.

    The tag and the tree are compared byte for byte. Thirteen files differ, and each one is a revision the notes name,
    which gained the statement that relaxes forced row-level security for its data statements (none of them had it).
    """

    def files(root: Path) -> dict[str, bytes]:
        base = root / "apps" / "api" / "alembic"
        return {
            str(path.relative_to(base)): path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }

    old, new = files(v0220_tree), files(REPO_ROOT)
    assert set(old) == set(new), sorted(set(old) ^ set(new))
    changed = sorted(name for name in new if new[name] != old[name])
    assert [Path(name).name[:13] for name in changed] == list(CHANGED_MIGRATIONS)
    for name in changed:
        assert b"NO FORCE ROW LEVEL SECURITY" not in old[name], name
        assert b"NO FORCE ROW LEVEL SECURITY" in new[name], name
    notes = _flat(NOTES_PATH)
    for revision in CHANGED_MIGRATIONS:
        assert f"`{revision}`" in notes, revision
    assert "Thirteen migration files changed" in notes
    assert "No file was added" in notes


def test_the_settings_and_flags_the_notes_list_are_all_that_this_release_adds(v0220_tree: Path) -> None:
    """Mutations, each one alone: add a setting named ``ALICE_`` anywhere under ``apps/api/src``; add a flag to any
    ``alice-memory`` command; delete the ``keys`` command or one of its sub-commands.

    The names are read from every Python file under ``apps/api/src`` at the v0.22.0 tag and now. The flags are read from
    the parser of every ``alice-memory`` command and sub-command, built by the v0.22.0 export and by this tree.
    """

    def names(root: Path) -> set[str]:
        found: set[str] = set()
        for path in (root / "apps" / "api" / "src").rglob("*.py"):
            found |= set(re.findall(r"\b(ALICE_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8", errors="replace")))
        return found

    assert names(REPO_ROOT) == names(v0220_tree)
    behaviour = _section(_text(NOTES_PATH), "Behaviour changes an integrator will notice")
    assert "No setting is added." in behaviour
    assert "`alice-memory` gains the `keys` command with the sub-commands `create`, `list` and `revoke`. No other flag is added." in behaviour

    old = subprocess.run(
        [sys.executable, "-c", _FLAGS_CODE], env=_env_for(v0220_tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )
    new = subprocess.run(
        [sys.executable, "-c", _FLAGS_CODE], env=_env_for(REPO_ROOT / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )
    assert old.returncode == 0 and new.returncode == 0, (old.stderr, new.stderr)
    flags_old, flags_new = json.loads(old.stdout), json.loads(new.stdout)
    assert set(flags_old) <= set(flags_new) and len(flags_new) > 15
    assert all(set(flags_old[command]) <= set(flags_new[command]) for command in flags_old)
    changed = {command: set(flags_new[command]) - set(flags_old.get(command, [])) for command in flags_new if set(flags_new[command]) != set(flags_old.get(command, []))}
    assert set(changed) == {"keys", "keys create", "keys list", "keys revoke"}
    common = {"--data-dir", "--db", "--user-id", "--user-email", "--help"}
    assert changed["keys"] == {"--help"}
    assert changed["keys create"] == common | {"--agent-id", "--profile", "--label", "--project", "--json"}
    assert changed["keys list"] == common | {"--limit", "--json"}
    assert changed["keys revoke"] == common | {"--json"}
    assert "--agent-key-env" not in json.dumps(flags_new)


_OWN_ENV_CODE = (
    "import importlib, json\n"
    "module = importlib.import_module('alicebot_api.' + 'host_install')\n"
    "cases = [{'args': ['mcp', '--data-dir', '/x/y']}, {'args': ['mcp']}, {'args': [1]}, {}, "
    "{'args': ['--from', 'alice-memory==1', 'alice-memory', 'mcp', '--data-dir', '/d']}]\n"
    "print(json.dumps([module._own_env(case) for case in cases]))\n"
)


def test_install_writes_the_same_environment_values_as_v0220_and_never_a_key(v0220_tree: Path) -> None:
    """Mutations, each one alone: make ``_own_env`` of ``host_install.py`` add ``ALICE_AGENT_API_KEY``, a reference to
    it or any other value; add a flag to ``alice-memory install`` (the flags test above fails too).

    The notes say that ``alice-memory install`` takes the same flags and writes the same environment values. The helper
    that decides the values is run on five payloads by the v0.22.0 export and by this tree. The module is loaded with
    ``importlib`` so that this file does not import a host module, which the real-host workflow lists by path.
    """

    old = _old_python(v0220_tree, _OWN_ENV_CODE)
    new = subprocess.run(
        [sys.executable, "-c", _OWN_ENV_CODE], env=_env_for(REPO_ROOT / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )
    assert old.returncode == 0 and new.returncode == 0, (old.stderr, new.stderr)
    assert json.loads(old.stdout) == json.loads(new.stdout)
    assert json.loads(new.stdout)[0] == {"ALICE_MEMORY_DATA_DIR": "/x/y"}
    assert AGENT_API_KEY_ENV not in new.stdout
    assert "`alice-memory install` takes the same flags and writes the same environment values." in _flat(NOTES_PATH)


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


def test_the_package_holds_none_of_the_bench_scripts_the_corpus_generator_or_the_console() -> None:
    """Mutation: add ``scripts`` or ``apps/web`` to the package list of ``pyproject.toml``, or a line for either to
    ``MANIFEST.in``.

    The wheel is the packages under ``apps/api/src`` and ``workers``, and the source distribution adds the files
    ``MANIFEST.in`` names. A wheel (392 files) and a source distribution (400 files and 22 folders) of the tree were
    built to check this and hold no file from ``scripts/`` or ``apps/web``; this test pins what decides it.
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
    for script in ("alice_bench.py", "showcase_bench.py", "generate_markdown_perf_corpus.py"):
        assert (REPO_ROOT / "scripts" / script).is_file(), script
    other = _section(_text(NOTES_PATH), "Other changes in the package")
    assert "A wheel and a source distribution built from the tree hold no file from `scripts/` or `apps/web`" in other
    assert "the wheel has 392 files, the source distribution 400 files and 22 folders" in other


# ---------------------------------------------------------------------------
# Agent keys on the SQLite vault, run
# ---------------------------------------------------------------------------


def test_keys_create_prints_the_raw_key_once_and_list_shows_neither_it_nor_its_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], v0220_tree: Path
) -> None:
    """Mutations, each one alone: print the key a second time; add the raw key or ``key_hash`` to the list record; drop
    the line that says the key is not shown again; let ``--project`` bind nothing.

    v0.22.0 has no ``keys`` command: its parser refuses it. The guard is ``_agent_key_public_record`` and the single
    ``print(raw_key)`` of ``_run_keys_create``.
    """

    import hashlib

    data_dir, _database = _vault(tmp_path, import_notes=False)
    code, out, err = _new(capsys, "keys", "create", "--agent-id", "claude-code", "--profile", "admin_agent", "--label", "Claude Code host", "--data-dir", str(data_dir))
    assert code == 0, err
    lines = out.splitlines()
    raw = lines[0]
    assert raw.startswith("alice_") and out.count(raw) == 1
    assert lines[1] == "Store this key now; it is not shown again, and only its sha256 hash is stored."
    assert lines[2] == "Agent: claude-code  profile: admin_agent  label: Claude Code host"
    code, bound, err = _new(capsys, "keys", "create", "--agent-id", "openclaw", "--profile", "project_scoped_agent", "--project", "acme", "--data-dir", str(data_dir))
    assert code == 0 and "project: acme" in bound, err
    digest = hashlib.sha256(raw.encode()).hexdigest()
    code, listing, _ = _new(capsys, "keys", "list", "--data-dir", str(data_dir))
    assert code == 0 and listing.count("active") == 2
    assert raw not in listing and digest not in listing and raw[:12] in listing
    assert "project=acme" in listing and "label=Claude Code host" in listing
    code, records, _ = _new(capsys, "keys", "list", "--json", "--data-dir", str(data_dir))
    parsed = json.loads(records)
    assert code == 0 and parsed["count"] == 2
    assert raw not in records and digest not in records and "key_hash" not in records
    assert sorted(parsed["items"][0]) == [
        "agent_id", "created_at", "id", "key_prefix", "label", "last_used_at", "permission_profile", "project_scope", "revoked", "revoked_at",
    ]
    with contextlib.closing(sqlite3.connect(_database_file(data_dir))) as connection:
        stored = connection.execute("SELECT key_hash, key_prefix FROM agent_api_keys WHERE agent_id = 'claude-code'").fetchone()
    assert stored[0] == digest and stored[1] == raw[: len(stored[1])] and raw not in stored
    old = _old(v0220_tree, "keys", "list", "--data-dir", str(tmp_path / "old"))
    assert old.returncode == 2 and not (tmp_path / "old").exists()


def _database_file(data_dir: Path) -> Path:
    return resolve_db_path(data_dir=str(data_dir), db=None)


def test_keys_create_refuses_a_blank_project_the_reserved_name_and_a_blank_agent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Mutations, each one alone: let ``--project`` take ``~global``; let it take a blank value; let ``--agent-id`` be
    blank; let ``--profile`` take a value that is not one of the five profiles.

    The command calls the shared ``create_agent_key`` of the PostgreSQL command, so the refusals are its refusals.
    """

    data_dir, _ = _vault(tmp_path, import_notes=False)
    for extra, message in (
        (["--agent-id", "x", "--profile", "admin_agent", "--project", "~global"], "the project name ~global is reserved by Alice"),
        (["--agent-id", "x", "--profile", "admin_agent", "--project", "  "], "project_scope must not be blank"),
        (["--agent-id", "  ", "--profile", "admin_agent"], "agent_id is required"),
    ):
        code, out, err = _new(capsys, "keys", "create", *extra, "--data-dir", str(data_dir))
        assert code == 2 and out == "" and message in err, (extra, err)
    code, out, err = _new(capsys, "keys", "list", "--data-dir", str(data_dir))
    assert out.strip() == "No agent keys on this vault."
    code, out, err = _new(capsys, "keys", "create", "--agent-id", "x", "--profile", "nonsense", "--data-dir", str(data_dir))
    assert code == 2 and out == ""


def test_a_vault_with_an_active_key_refuses_a_keyless_call_and_v0220_answered_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, v0220_tree: Path
) -> None:
    """Mutations, each one alone: stop counting the active keys in ``_agent_identity_from_arguments``; count revoked keys
    too; let a legacy tool through; let an unreadable key table through; refuse a call that carries a good key.

    On v0.22.0 the same keyless call is answered on a vault that holds a key (the key is minted with that tree's own
    ``create_agent_key``, because it had no command for it).
    """

    data_dir, database = _vault(tmp_path)
    context = _context(database)
    query = {"query": "kiln firing schedule"}
    assert _call(context, "alice_recall", query, key=None, monkeypatch=monkeypatch)["source_count"] >= 1
    key = _mint(capsys, data_dir)
    for name, arguments in (("alice_recall", query), ("alice_resume", {})):
        with pytest.raises(MCPNotPermittedError):
            _call(context, name, arguments, key=None, monkeypatch=monkeypatch)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    # Two legacy tools that resolve no agent identity of their own, so only the refusal in call_mcp_tool stops them.
    for legacy, arguments in (
        ("alice_open_loop_extract", {"person_name": "someone"}),
        ("alice_graph_neighborhood", {"target_id": "00000000-0000-0000-0000-0000000000aa"}),
    ):
        with pytest.raises(MCPNotPermittedError):
            _call(context, legacy, arguments, key=None, monkeypatch=monkeypatch)
    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS")
    monkeypatch.delenv("ALICE_MCP_LEGACY_TOOLS")
    assert _call(context, "alice_recall", query, key=key, monkeypatch=monkeypatch)["source_count"] >= 1
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", query, key="alice_sk_not_a_real_key_000000000000000000", monkeypatch=monkeypatch)
    # A second key, then a revoked one: only active keys count, and a revoked key is refused on its next call.
    second = _mint(capsys, data_dir, "other", "read_only_agent")
    code, listing, _ = _new(capsys, "keys", "list", "--json", "--data-dir", str(data_dir))
    prefix = [item["key_prefix"] for item in json.loads(listing)["items"] if item["agent_id"] == "other"][0]
    code, revoked, _ = _new(capsys, "keys", "revoke", prefix, "--data-dir", str(data_dir))
    assert code == 0 and revoked.startswith(f"Revoked {prefix} (other)")
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", query, key=second, monkeypatch=monkeypatch)
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", query, key=None, monkeypatch=monkeypatch)
    # Revoking the last active key makes the vault keyless again.
    code, listing, _ = _new(capsys, "keys", "list", "--json", "--data-dir", str(data_dir))
    last = [item["key_prefix"] for item in json.loads(listing)["items"] if item["agent_id"] == "host"][0]
    assert _new(capsys, "keys", "revoke", last, "--data-dir", str(data_dir))[0] == 0
    assert _call(context, "alice_recall", query, key=None, monkeypatch=monkeypatch)["source_count"] >= 1
    with pytest.raises(MCPNotPermittedError):
        _call(context, "alice_recall", query, key=key, monkeypatch=monkeypatch)
    # v0.22.0 on a vault that holds an active key: the keyless call is answered.
    old_dir = tmp_path / "old-vault"
    answered = _old_python(v0220_tree, _OLD_KEYED_VAULT_CODE, str(old_dir))
    assert answered.returncode == 0, answered.stderr
    assert json.loads(answered.stdout.splitlines()[-1])["answered"] is True


def test_an_unreadable_key_table_refuses_the_call_instead_of_answering_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: let the call through when the count of active keys raises.

    The notes say that if the count of keys cannot be read, the call is refused. The column the count reads is renamed
    in a copy of the vault, which makes the query fail.
    """

    data_dir, database = _vault(tmp_path)
    _mint(capsys, data_dir)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE agent_api_keys RENAME COLUMN revoked_at TO revoked_x")
        connection.commit()
    with pytest.raises(MCPNotPermittedError):
        _call(_context(database), "alice_recall", {"query": "kiln firing schedule"}, key=None, monkeypatch=monkeypatch)
    assert "If the count of keys cannot be read, the call is refused." in _flat(NOTES_PATH)


def test_the_brief_and_the_hook_print_nothing_without_the_key_and_v0220_printed_the_brief(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, v0220_tree: Path
) -> None:
    """Mutations, each one alone: let ``session_brief_identity`` return the owner's stance when the vault has an active
    key and the environment has none; let the hook skip the rule; print the brief for a key that does not resolve.

    The hook is run as a subprocess with ``{}`` on stdin and a timeout, from this tree. The same vault is run from the
    v0.22.0 tree with a key in it, and prints the brief.
    """

    data_dir, _database = _vault(tmp_path)
    code, keyless_brief, _ = _new(capsys, "brief", "--data-dir", str(data_dir))
    assert code == 0 and BRIEF_MARK in keyless_brief
    keyless_hook = _hook(data_dir)
    assert keyless_hook.returncode == 0 and BRIEF_MARK in keyless_hook.stdout
    key = _mint(capsys, data_dir)
    code, out, err = _new(capsys, "brief", "--data-dir", str(data_dir))
    assert (code, out, err) == (0, "", "")
    hook = _hook(data_dir)
    assert (hook.returncode, hook.stdout) == (0, "")
    bad = _hook(data_dir, key="alice_sk_not_a_real_key_000000000000000000")
    assert (bad.returncode, bad.stdout) == (0, "")
    monkeypatch.setenv(AGENT_API_KEY_ENV, key)
    code, keyed_brief, _ = _new(capsys, "brief", "--data-dir", str(data_dir))
    monkeypatch.setenv(AGENT_API_KEY_ENV, "alice_sk_not_a_real_key_000000000000000000")
    _, bad_brief, _ = _new(capsys, "brief", "--data-dir", str(data_dir))
    monkeypatch.delenv(AGENT_API_KEY_ENV)
    assert code == 0 and BRIEF_MARK in keyed_brief and bad_brief == ""
    keyed_hook = _hook(data_dir, key=key)
    assert keyed_hook.returncode == 0 and BRIEF_MARK in keyed_hook.stdout
    # v0.22.0 on a vault that holds a key prints the brief without one.
    old_dir = tmp_path / "old-vault"
    minted = _old_python(v0220_tree, _OLD_KEYED_VAULT_CODE, str(old_dir))
    assert minted.returncode == 0, minted.stderr
    assert _old(v0220_tree, "import-markdown", "--from", str(tmp_path / "notes"), "--data-dir", str(old_dir)).returncode == 0
    old_brief = _old(v0220_tree, "brief", "--data-dir", str(old_dir))
    assert old_brief.returncode == 0 and BRIEF_MARK in old_brief.stdout


def test_the_commands_you_run_yourself_take_no_key_on_a_vault_that_holds_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutations, each one alone: make ``doctor``, ``export``, ``import``, ``sleep``, ``sleep-proposals``,
    ``import-markdown`` or ``import-chatgpt`` read ``ALICE_AGENT_API_KEY`` or refuse without it.

    The notes list these commands in upgrade step 4. Each runs on a vault with an active key and no key in the
    environment, and exits 0.
    """

    data_dir, _database = _vault(tmp_path)
    _mint(capsys, data_dir)
    capsys.readouterr()
    chatgpt = tmp_path / "chatgpt.json"
    chatgpt.write_text(
        json.dumps([{"id": "c1", "title": "Kiln talk", "create_time": 1760000000, "mapping": {
            "a": {"id": "a", "parent": None, "children": ["b"], "message": {"id": "a", "author": {"role": "user"}, "create_time": 1760000001, "content": {"content_type": "text", "parts": ["When does the kiln firing move to Tuesday this month?"]}}},
            "b": {"id": "b", "parent": "a", "children": [], "message": {"id": "b", "author": {"role": "assistant"}, "create_time": 1760000002, "content": {"content_type": "text", "parts": ["The kiln firing moves to Tuesday because the glaze supplier delivers on Monday."]}}},
        }}]),
        encoding="utf-8",
    )
    export_file = tmp_path / "export.jsonl"
    commands = [
        ["doctor", "--data-dir", str(data_dir)],
        ["export", "--out", str(export_file), "--data-dir", str(data_dir)],
        ["sleep", "--data-dir", str(data_dir)],
        ["sleep-proposals", "--data-dir", str(data_dir)],
        ["import-markdown", "--from", str(tmp_path / "notes"), "--data-dir", str(data_dir)],
        ["import-chatgpt", "--from", str(chatgpt), "--data-dir", str(data_dir)],
        ["keys", "list", "--data-dir", str(data_dir)],
    ]
    for args in commands:
        code, out, err = _new(capsys, *args)
        assert code == 0, (args[0], err)
    code, out, err = _new(capsys, "import", "--in", str(export_file), "--data-dir", str(tmp_path / "restored"))
    assert code == 0, err


def test_the_export_drops_keys_and_a_file_copy_after_a_checkpoint_keeps_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutations, each one alone: write a key record, a key hash or a raw key into the export; make the import create a
    key; make the file copy lose the key table.

    The export keeps the event that records a key's creation (prefix, agent id, profile), which makes no key.
    """

    import hashlib
    import shutil

    data_dir, database = _vault(tmp_path)
    key = _mint(capsys, data_dir)
    export_file = tmp_path / "export.jsonl"
    assert _new(capsys, "export", "--out", str(export_file), "--data-dir", str(data_dir))[0] == 0
    exported = export_file.read_text(encoding="utf-8")
    assert key not in exported and hashlib.sha256(key.encode()).hexdigest() not in exported
    record_types = {json.loads(line).get("record_type") for line in exported.splitlines()}
    assert "agent_api_key" not in record_types and "key_hash" not in exported
    created = [json.loads(line)["record"] for line in exported.splitlines() if '"agent.key_created"' in line]
    assert len(created) == 1 and created[0]["payload_json"]["key_prefix"] == key[: len(created[0]["payload_json"]["key_prefix"])]
    restored = tmp_path / "restored"
    assert _new(capsys, "import", "--in", str(export_file), "--data-dir", str(restored))[0] == 0
    code, listing, _ = _new(capsys, "keys", "list", "--data-dir", str(restored))
    assert listing.strip() == "No agent keys on this vault."
    restored_context = _context(_database_file(restored))
    assert _call(restored_context, "alice_recall", {"query": "kiln firing schedule"}, key=None, monkeypatch=monkeypatch)["source_count"] >= 1
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    shutil.copy2(database, copy_dir / database.name)
    code, listing, _ = _new(capsys, "keys", "list", "--data-dir", str(copy_dir))
    assert code == 0 and listing.count("active") == 1
    with pytest.raises(MCPNotPermittedError):
        _call(_context(_database_file(copy_dir)), "alice_recall", {"query": "kiln firing schedule"}, key=None, monkeypatch=monkeypatch)


def test_an_unbound_admin_key_does_not_get_the_evidence_of_an_archived_source_that_the_keyless_owner_gets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutations, each one alone: let the saved-quote reader return an archived source to a key; delete the sentence
    about the difference from the notes.

    The memory cites a source, the source is archived with ``sources delete``, and the same three reads run keyless
    (before any key exists) and as an unbound ``admin_agent`` key. The notes name this as the one known difference.
    """

    data_dir, database = _vault(tmp_path, import_notes=False)
    folder = tmp_path / "larkspur"
    folder.mkdir()
    (folder / "larkspur.md").write_text("# Larkspur account\n\nThe Larkspur contract renews in March and the account owner is Dana.\n", encoding="utf-8")
    assert _new(capsys, "import-markdown", "--from", str(folder), "--data-dir", str(data_dir))[0] == 0
    code, listing, _ = _new(capsys, "sources", "list", "--data-dir", str(data_dir))
    source_id = json.loads(listing)["sources"][0]["id"]
    context = _context(database)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    identity = {"agent_id": "probe", "agent_type": "coding_agent", "permission_profile": "admin_agent"}
    committed = _call(context, "alice_memory_commit", {
        **identity, "title": "Larkspur", "canonical_text": "The Larkspur contract renews in March.", "domain": "project",
        "sensitivity": "private", "source_refs": [f"source:{source_id}"], "idempotency_key": "known-difference",
    }, key=None, monkeypatch=monkeypatch)
    memory_id = committed["memory"]["id"]
    pack = {"query": "Larkspur contract renews"}
    explain = {"memory_id": memory_id}
    review = {"continuity_object_id": memory_id}
    assert _new(capsys, "sources", "delete", source_id, "--yes", "--data-dir", str(data_dir))[0] == 0
    owner = {
        "explain": json.dumps(_call(context, "alice_explain", explain, key=None, monkeypatch=monkeypatch)),
        "review": json.dumps(_call(context, "alice_memory_review", review, key=None, monkeypatch=monkeypatch)),
        "pack": json.dumps(_call(context, "alice_context_pack", pack, key=None, monkeypatch=monkeypatch)),
    }
    assert all(source_id in text for text in owner.values())
    key = _mint(capsys, data_dir, "owner-host", "admin_agent")
    with pytest.raises(Exception, match="unavailable"):
        _call(context, "alice_explain", explain, key=key, monkeypatch=monkeypatch)
    assert source_id not in json.dumps(_call(context, "alice_memory_review", review, key=key, monkeypatch=monkeypatch))
    assert source_id not in json.dumps(_call(context, "alice_context_pack", pack, key=key, monkeypatch=monkeypatch))
    notes = _section(_text(NOTES_PATH), "Agent keys on the SQLite vault")
    assert "`alice_explain` is refused to the key" in notes and "do not carry the source's id" in notes


# ---------------------------------------------------------------------------
# Recall, ids and the changelog
# ---------------------------------------------------------------------------


def _fifteen_passages(tmp_path: Path) -> Path:
    folder = tmp_path / "lanterns"
    folder.mkdir()
    for index in range(1, 15):
        (folder / f"lantern{index:02d}.md").write_text(
            f"# Lantern {index}\n\nParagraph {index} of the lantern plan records the tok{index:02d}lantern detail for the archive.\n",
            encoding="utf-8",
        )
    (folder / "brass.md").write_text("# Brass\n\nParagraph 1 of the brass plan records the tok01brass detail for the workshop.\n", encoding="utf-8")
    return folder


_OLD_RECALL_CODE = (
    "import json, sys\n"
    "from alicebot_api.mcp.registry import call_mcp_tool\n"
    "from alicebot_api.mcp_tools import MCPRuntimeContext\n"
    "from alicebot_api.onramp import main, resolve_db_path, sqlite_url_for_path\n"
    "user = '00000000-0000-0000-0000-000000000001'\n"
    "assert main(['import-markdown', '--from', sys.argv[2], '--data-dir', sys.argv[1]]) == 0\n"
    "context = MCPRuntimeContext(database_url=sqlite_url_for_path(resolve_db_path(data_dir=sys.argv[1], db=None)), user_id=user)\n"
    "query = {'query': 'lantern plan archive detail'}\n"
    "print(json.dumps({'default': call_mcp_tool(context, name='alice_recall', arguments=query)['source_count'],\n"
    "                  'limit8': call_mcp_tool(context, name='alice_recall', arguments={**query, 'limit': 8})['source_count']}))\n"
)


def test_recall_returns_ten_note_passages_by_default_and_v0220_returned_eight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, v0220_tree: Path
) -> None:
    """Mutations, each one alone: set ``_RECALL_DEFAULT_NOTE_PASSAGE_BUDGET`` back to 8; ignore an explicit limit; apply
    the budget in document mode.

    A vault of fifteen one-passage notes is asked the same question on this tree and on v0.22.0.
    """

    folder = _fifteen_passages(tmp_path)
    assert alice_memory_main(["import-markdown", "--from", str(folder), "--data-dir", str(tmp_path / "vault")]) == 0
    context = _context(_database_file(tmp_path / "vault"))
    query = {"query": "lantern plan archive detail"}
    assert _call(context, "alice_recall", query, key=None, monkeypatch=monkeypatch)["source_count"] == 10
    assert _call(context, "alice_recall", {**query, "limit": 8}, key=None, monkeypatch=monkeypatch)["source_count"] == 8
    assert _call(context, "alice_recall", {**query, "limit": 5}, key=None, monkeypatch=monkeypatch)["source_count"] == 5
    monkeypatch.setenv("ALICE_SEARCH_QUALITY", "off")
    assert _call(context, "alice_recall", query, key=None, monkeypatch=monkeypatch)["source_count"] == 8
    old = _old_python(v0220_tree, _OLD_RECALL_CODE, str(tmp_path / "old-vault"), str(folder))
    assert old.returncode == 0, old.stderr
    assert json.loads(old.stdout.splitlines()[-1]) == {"default": 8, "limit8": 8}


_OLD_EXPLAIN_CODE = (
    "import json, os, sys\n"
    "os.environ['ALICE_MCP_FULL_TOOLS'] = '1'\n"
    "from alicebot_api.mcp.registry import call_mcp_tool\n"
    "from alicebot_api.mcp_tools import MCPRuntimeContext\n"
    "from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path\n"
    "user = '00000000-0000-0000-0000-000000000001'\n"
    "database = resolve_db_path(data_dir=sys.argv[1], db=None)\n"
    "bootstrap_database(database, user_id=user, user_email='local@alice')\n"
    "context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=user)\n"
    "memory = call_mcp_tool(context, name='alice_memory_commit', arguments={'agent_id': 'probe', 'agent_type': 'coding_agent',\n"
    "    'permission_profile': 'admin_agent', 'title': 'Larkspur', 'canonical_text': 'The Larkspur contract renews in March.',\n"
    "    'domain': 'project', 'sensitivity': 'private', 'idempotency_key': 'spellings'})['memory']['id']\n"
    "found = {}\n"
    "for label, value in (('canonical', memory), ('upper', memory.upper()), ('urn', 'urn:uuid:' + memory), ('braces', '{' + memory + '}'),\n"
    "                     ('plain', memory.replace('-', '')), ('not an id', 'no-such-id')):\n"
    "    try:\n"
    "        call_mcp_tool(context, name='alice_explain', arguments={'memory_id': value})\n"
    "        found[label] = 'found'\n"
    "    except Exception as exc:\n"
    "        found[label] = type(exc).__name__\n"
    "print(json.dumps(found))\n"
)


def test_a_memory_id_in_another_spelling_is_found_on_the_sqlite_vault_and_v0220_did_not_find_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, v0220_tree: Path
) -> None:
    """Mutations, each one alone: take the shared id reader out of ``alice_explain``; let a value that is not an id
    answer anything but not found.

    The notes say that ``alice_explain`` finds a memory by an id in upper case, in braces, without hyphens or as
    ``urn:uuid:`` on the SQLite vault, where v0.22.0 answered ``not_found``.
    """

    _data_dir, database = _vault(tmp_path, import_notes=False)
    context = _context(database)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    identity = {"agent_id": "probe", "agent_type": "coding_agent", "permission_profile": "admin_agent"}
    memory = _call(context, "alice_memory_commit", {
        **identity, "title": "Larkspur", "canonical_text": "The Larkspur contract renews in March.", "domain": "project",
        "sensitivity": "private", "idempotency_key": "spellings",
    }, key=None, monkeypatch=monkeypatch)["memory"]["id"]
    for value in (memory, memory.upper(), "urn:uuid:" + memory, "{" + memory + "}", memory.replace("-", "")):
        assert _call(context, "alice_explain", {"memory_id": value}, key=None, monkeypatch=monkeypatch), value
    with pytest.raises(Exception, match="not found|not_found|unavailable|could not"):
        _call(context, "alice_explain", {"memory_id": "no-such-id"}, key=None, monkeypatch=monkeypatch)
    old = _old_python(v0220_tree, _OLD_EXPLAIN_CODE, str(tmp_path / "old-vault"))
    assert old.returncode == 0, old.stderr
    found = json.loads(old.stdout.splitlines()[-1])
    assert found["canonical"] == "found"
    assert all(found[label] != "found" for label in ("upper", "urn", "braces", "plain", "not an id")), found


def test_the_postgresql_claims_have_integration_tests_that_run_in_ci() -> None:
    """Mutations, each one alone: delete one of the eight integration files; name one of them in no CI job; drop the
    sentence of the notes that says which doors answer 404.

    These tests run against PostgreSQL and are not run by the unit suite. They were run on this tree (179 passed) and
    on the v0.22.0 code, where the doors failed in the ways the changelog names (108 of the 161 tests of seven files
    failed, and the eighth file, which counts the migrations that relax forced row-level security, failed on collection
    with 5 of 13). ``test_ci_integration_job_split.py`` fails when an integration file is in no job or in two.
    """

    files = (
        "test_malformed_id_doors_postgres.py",
        "test_open_loop_person_not_found_postgres.py",
        "test_source_id_spelling_context_pack_postgres.py",
        "test_migrations_0067_strict_owner.py",
        "test_migrations_0070_0092_strict_owner.py",
        "test_migrations_0088_strict_owner.py",
        "test_migrations_residual_backfills_strict_owner.py",
        "test_migrations_forced_flags_restore.py",
    )
    workflow = _text(".github/workflows/tests.yml")
    for name in files:
        assert (REPO_ROOT / "tests" / "integration" / name).is_file(), name
    assert "tests/integration" in workflow
    errors = _section(_text(NOTES_PATH), "Server errors that are now answers")
    assert "All four answer HTTP 404 now, the answer of a well-formed id that does not exist." in errors
    assert "It answers HTTP 404 `not_found` and writes nothing." in errors
    assert "They return the pack now." in errors


def test_the_headline_changes_of_the_notes_each_have_a_changelog_entry() -> None:
    """Mutations, each one alone: delete the changelog entry of a change the notes name, or reword its first words;
    put an entry back under ``Unreleased``; delete the sentence of the keys entry that says what v0.22.0 had.
    """

    starts = (
        "The SQLite vault gets agent keys and a keyless refusal.",
        "The MCP tool `alice_recall` returns ten note passages by default",
        "A vNext door that reads a row id no longer answers a server error",
        "`POST /v0/vnext/open-loops/extract` no longer answers a server error",
        "`POST /v0/vnext/context-packs` and the MCP tool `alice_context_pack` no longer answer a server error",
        "The Alembic migrations `20260510_0067`",
        "`alice-memory import-markdown` on a SQLite vault runs the same import",
    )
    entries = _entries()
    assert len(entries) == len(starts) == 7
    for start in starts:
        assert len([entry for entry in entries if entry.startswith(start)]) == 1, start
    for entry in entries:
        assert "migration is required" in entry.lower() or "operator action is required" in entry.lower(), entry[:60]
        assert "Unreleased" not in entry[:40]
    keys = entries[0]
    assert "v0.22.0 and earlier had no way to mint an agent key on the SQLite vault" in keys
    assert "a keyless MCP call read with the local owner's standing even when the vault held keys" in keys
    assert "they do not stop a process that runs as your OS user from reading the vault file" in keys
    assert "The JSONL export still drops keys, so keys are minted again after an import from an export; a file copy after a checkpoint keeps them." in keys
    assert "au" + "dit" not in keys.lower()
    # The malformed id entry names a legacy tool whose name holds the word; only text outside code spans counts.
    assert "au" + "dit" not in re.sub(r"`[^`]*`", "", entries[2]).lower()
    migrations = entries[5]
    for revision in CHANGED_MIGRATIONS:
        assert f"`{revision}`" in migrations, revision
    assert "No operator action is required." in migrations
    notes = _flat(NOTES_PATH)
    for name in ("`alice_explain`", "`alice_open_loops`", "`alice_belief_review`", "`alice_open_loop_extract`", "`alice_context_pack`"):
        assert name in notes, name


def test_the_release_check_accepts_the_tree_and_its_pre_tag_mode_refuses_the_published_notes() -> None:
    """Mutations, each one alone: set a status of the notes back to pending; delete the checksums file; delete the
    dated heading.

    ``release_check.py`` passes on the tree. Its ``--require-finalized-release-docs`` mode is the pre-tag rule and
    refuses a published tree on purpose, for the same three reasons as after v0.22.0: both statuses must be pending
    before the tag, and the checksums file must not exist on the pre-publication tag. The package version of the tree is
    the next release's (``test_v0231_release_notes.py`` pins it), so the rule is run for the version of these notes.
    """

    metadata, issues = release_check.validate_metadata(REPO_ROOT)
    assert issues == []
    version = "0.23.0"
    assert metadata.version != version
    assert release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=False) == []
    published = [
        "release notes alice-release-state publication_status must be pending before tag",
        "release notes alice-release-state checksums_status must be pending before tag",
        "docs/release/v0.23.0-checksums.txt must not exist on the pre-publication tag",
    ]
    issues = release_check.validate_release_document_state(REPO_ROOT, version=version, require_finalized=True)
    assert [issue for issue in issues if issue in published] == published
    # Once main has an Unreleased CHANGELOG entry, the pre-tag mode also says the section must be empty for a tag.
    # That is true of every later commit and is the only other issue allowed here.
    assert [issue for issue in issues if issue not in published] in (
        [],
        ["CHANGELOG.md Unreleased section must be empty for the release tag"],
    )
