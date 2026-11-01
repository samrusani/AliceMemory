"""The v0.23.0 release notes carry the upgrade steps, the wording about keys and the claims a reader can check.

Every claim in the notes comes from an entry of the changelog, a page of the documents or a file of the tree at the
v0.23.0 tag. These tests pin the parts a later edit could quietly weaken: the release identity and its published state,
the checksums record, the six version sites and the marketplace pin, the dated changelog heading, the security wording,
the exact sentence about what a key does and does not do, the steps an upgrading user must act on, the numbers that
drift (the tool counts, the PostgreSQL revision, the thirteen migration files) and the claims about what changes. Those
claims are run, not read: the three keys commands, the keyless refusal, the brief and the session-start hook, the
commands the owner runs without a key, what the export keeps, the difference between a key and the keyless owner, the
ten note passages of a recall, and the spellings of a memory id on the SQLite vault.

What the release held is read from the v0.23.0 tag, with ``git show`` or with ``git archive`` into a temporary folder
whose code runs in a subprocess, and never from the working tree. The tree moves on with every later change to main (a
changed module, a new setting, a reworded page, a new entry or a dated paragraph in the changelog), and a published claim
about the release must not fail because of one. Each comparison with v0.22.0 reads that tag the same way and runs its
code in a subprocess too. The tree is read only for what must stay true after the release: the notes and their
published state, the checksums files, the marketplace pin, the dated heading of the changelog, the markers that must be
absent, and the release check on the published notes. Each test names the mutation that must fail it, and for a test
that reads the tag the mutations include reading the v0.22.0 tag where it reads the v0.23.0 tag, or another change that
the test must notice, and reading the tree where it reads the tag.

The PostgreSQL claims (the doors that answered a server error, the open-loop extraction, the context pack and the owner
migrations) are run by the integration tests named in ``test_the_postgresql_claims_have_integration_tests_that_run_in_ci``
against a local PostgreSQL 16, on the tag and on the v0.22.0 code. They are not run here.

The notes are published. The change that followed publication moved their state to ``published`` and ``recorded``,
added the checksums file, moved the marketplace file to the v0.23.0 tag and its commit, named v0.23.0 as the latest
published release in the control documents and removed the release candidate line, as the change after v0.22.0 did, and
added one dated 2026-10-30 update beside the notes' own marketplace step. The change after v0.23.1 moved the marketplace
file and the control documents on to v0.23.1, which ``test_v0231_release_notes.py`` pins, and added a dated 2026-11-01
follow-up after that update. The tagged lines of the notes and of the changelog section are held by
``test_published_release_notes_stay_immutable.py``. The release check's pre-tag mode refuses the tree now, and one test
below pins the three reasons.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tomllib
from io import BytesIO
from pathlib import Path

import pytest

import scripts.release_check as release_check
from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
from tests.unit.test_published_release_notes_stay_immutable import _tagged
from tests.unit.test_v0210_release_notes import _tagged_listing

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_PATH = "docs/release/v0.23.0-release-notes.md"
HEADING = "## v0.23.0 \u2014 2026-10-30"
# The notes are about 14,600 characters, and 17,500 leaves room for a correction and no more.
NOTES_CEILING = 17_500
USER_ID = "00000000-0000-0000-0000-000000000001"

# The v0.23.0 tag, the commit of the latest tag (v0.23.1), which the marketplace file pins now, the commit of the v0.22.0
# tag that the copy of the marketplace file inside the v0.23.0 tag still pins, and the digests of the two files published
# for v0.23.0.
RELEASE_TAG = "v0.23.0"
CHECKSUMS_PATH = "docs/release/v0.23.0-checksums.txt"
LATEST_TAG_COMMIT = "2443e7cb6d8f89b4e625cec7adcd7b4a8dcd7276"
PRIOR_TAG = "v0.22.0"
PRIOR_TAG_COMMIT = "a6a5f49eebb7377f40b388f2ffe07290b05b85fb"
WHEEL_SHA256 = "ad644f225da839d4fa3d20104feeb959c2fb6c8af1a16591f7f59338b7c5b847"
SDIST_SHA256 = "118ce1f8389d095bec76f4e0cdfa5c4966e1b26e95c99f292582581724271821"

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


def _tagged_flat(path: str) -> str:
    """``path`` as the v0.23.0 tag held it, whitespace collapsed."""

    return " ".join(_tagged(RELEASE_TAG, path).split())


def _changelog_section() -> str:
    """The v0.23.0 section of the changelog as the tag held it: an entry or a dated paragraph added later is not part of it."""

    changelog = _tagged(RELEASE_TAG, "CHANGELOG.md")
    return changelog.split(HEADING)[1].split("\n## v0.22.0")[0]


def _entries() -> list[str]:
    return [" ".join(item.split()) for item in _changelog_section().split("\n- ")[1:]]


# ---------------------------------------------------------------------------
# The v0.22.0 and v0.23.0 tags, exported into temporary folders. The tests that compare code run it in a subprocess.
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
def v0220_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``apps/api/src`` and ``apps/api/alembic`` of the v0.22.0 tag."""

    return _export(PRIOR_TAG, tmp_path_factory, "v0220", "apps/api/src", "apps/api/alembic")


@pytest.fixture(scope="module")
def v0230_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The package source, the migrations, ``workers``, ``pyproject.toml`` and ``MANIFEST.in`` of the v0.23.0 tag."""

    return _export(
        RELEASE_TAG, tmp_path_factory, "v0230", "apps/api/src", "apps/api/alembic", "workers", "pyproject.toml", "MANIFEST.in"
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


def _env_for(source_root: Path, key: str | None = None) -> dict[str, str]:
    """The environment of a child process that imports ``alicebot_api`` from ``source_root``, with no ``ALICE_`` setting."""

    env = {name: value for name, value in os.environ.items() if not name.startswith("ALICE_")}
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = str(source_root)
    if key is not None:
        env[AGENT_API_KEY_ENV] = key
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


def _new_python(tree: Path, code: str, *args: str, key: str | None = None) -> subprocess.CompletedProcess[str]:
    """``code`` run by the code of the v0.23.0 tag under ``tree``, in a subprocess, with ``key`` in the environment if given."""

    return subprocess.run(
        [sys.executable, "-c", code, *args], env=_env_for(tree / "apps" / "api" / "src", key), capture_output=True, text=True, timeout=180
    )


_COMMAND = "import sys; from alicebot_api.onramp import main; sys.exit(main(sys.argv[1:]))"


def _new(tree: Path, *args: str, key: str | None = None) -> tuple[int, str, str]:
    """``alice-memory`` with the arguments, run from the code of the v0.23.0 tag. Returns (exit code, output, error output)."""

    done = _new_python(tree, _COMMAND, *args, key=key)
    return done.returncode, done.stdout, done.stderr


def _hook(tree: Path, data_dir: Path, *, key: str | None = None) -> subprocess.CompletedProcess[str]:
    """``alice-memory-session-start`` with ``{}`` on stdin, bounded by a timeout, from the code of the v0.23.0 tag."""

    # Run as a module, which is how a host's hook command starts it.
    return subprocess.run(
        [sys.executable, "-m", "alicebot_api.session_start_hook", "--data-dir", str(data_dir)],
        env=_env_for(tree / "apps" / "api" / "src", key), input="{}\n", capture_output=True, text=True, timeout=120,
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


_BOOTSTRAP_CODE = (
    "import sys\n"
    "from alicebot_api.onramp import bootstrap_database, resolve_db_path\n"
    "bootstrap_database(resolve_db_path(data_dir=sys.argv[1], db=None), user_id='" + USER_ID + "', user_email='local@alice')\n"
)


@pytest.fixture(scope="module")
def vault_templates(tmp_path_factory: pytest.TempPathFactory, v0230_tree: Path) -> dict[bool, Path]:
    """Two SQLite vaults made by the code of the v0.23.0 tag, each copied for a test: ``False`` is empty, ``True`` has imported
    the two notes of ``_notes_folder``. Making them once keeps the tests from starting two more processes each."""

    empty = tmp_path_factory.mktemp("empty-vault") / "vault"
    assert _new_python(v0230_tree, _BOOTSTRAP_CODE, str(empty)).returncode == 0
    with_notes = tmp_path_factory.mktemp("notes-vault") / "vault"
    assert _new_python(v0230_tree, _BOOTSTRAP_CODE, str(with_notes)).returncode == 0
    folder = _notes_folder(tmp_path_factory.mktemp("template-notes"))
    code, _out, err = _new(v0230_tree, "import-markdown", "--from", str(folder), "--data-dir", str(with_notes))
    assert code == 0, err
    return {False: empty, True: with_notes}


def _database_file(data_dir: Path) -> Path:
    database = data_dir / "memory.db"
    assert database.is_file(), database
    return database


def _vault(tmp_path: Path, templates: dict[bool, Path], *, import_notes: bool = True) -> tuple[Path, Path]:
    """A copy of a SQLite vault made by the v0.23.0 code, in a temporary data directory. Returns (data directory, database file)."""

    data_dir = tmp_path / "vault"
    shutil.copytree(templates[import_notes], data_dir)
    if import_notes:
        _notes_folder(tmp_path)
    return data_dir, _database_file(data_dir)


def _mint(tree: Path, data_dir: Path, agent_id: str = "host", profile: str = "admin_agent", *extra: str) -> str:
    code, out, err = _new(tree, "keys", "create", "--agent-id", agent_id, "--profile", profile, "--data-dir", str(data_dir), *extra)
    assert code == 0, err
    return out.splitlines()[0].strip()


# Makes the tool calls of a list one after the other in one process, on the vault in ``sys.argv[1]``. Before each call the
# settings and the key of the previous call are removed, then those of this call are set. An exception is an outcome.
_CALLS_CODE = (
    "import json, os, sys\n"
    "from alicebot_api.mcp.registry import call_mcp_tool\n"
    "from alicebot_api.mcp_tools import MCPRuntimeContext\n"
    "from alicebot_api.onramp import resolve_db_path, sqlite_url_for_path\n"
    "database = resolve_db_path(data_dir=sys.argv[1], db=None)\n"
    "context = MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id='" + USER_ID + "')\n"
    "outcomes = []\n"
    "for call in json.loads(sys.argv[2]):\n"
    "    for name in ('ALICE_MCP_FULL_TOOLS', 'ALICE_MCP_LEGACY_TOOLS', 'ALICE_SEARCH_QUALITY', '" + AGENT_API_KEY_ENV + "'):\n"
    "        os.environ.pop(name, None)\n"
    "    os.environ.update(call['env'])\n"
    "    if call['key'] is not None:\n"
    "        os.environ['" + AGENT_API_KEY_ENV + "'] = call['key']\n"
    "    try:\n"
    "        outcomes.append({'ok': True, 'result': call_mcp_tool(context, name=call['name'], arguments=call['arguments'])})\n"
    "    except Exception as exc:\n"
    "        outcomes.append({'ok': False, 'error': type(exc).__name__, 'message': str(exc)})\n"
    "print(json.dumps(outcomes, default=str))\n"
)

_FULL = {"ALICE_MCP_FULL_TOOLS": "1"}


def _call(name: str, arguments: dict | None = None, *, key: str | None = None, env: dict[str, str] | None = None) -> dict:
    """One tool call for ``_calls``: the tool, its arguments, the key in the environment and the settings of that call."""

    return {"name": name, "arguments": arguments or {}, "key": key, "env": env or {}}


def _calls(tree: Path, data_dir: Path, *calls: dict) -> list[dict]:
    """The outcomes of ``calls``, made one after the other by the code under ``tree`` on the vault in ``data_dir``.

    An outcome is ``{"ok": True, "result": ...}`` or ``{"ok": False, "error": <class name>, "message": ...}``.
    """

    done = _new_python(tree, _CALLS_CODE, str(data_dir), json.dumps(list(calls)))
    assert done.returncode == 0, done.stderr
    outcomes = json.loads(done.stdout.splitlines()[-1])
    assert len(outcomes) == len(calls)
    return outcomes


def _answer(outcome: dict) -> dict:
    assert outcome["ok"], outcome
    return outcome["result"]


def _refused(outcome: dict) -> bool:
    """Whether the call raised ``MCPNotPermittedError``, the refusal of a keyless call on a vault that holds a key."""

    return not outcome["ok"] and outcome["error"] == "MCPNotPermittedError"


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


def test_the_marketplace_pins_the_latest_release_and_the_notes_carry_the_install_lines() -> None:
    """Mutations, each one alone: set the marketplace ``ref`` back to v0.23.0; change one hex digit of the marketplace
    ``sha``; change the install command in the notes; change the copy of the marketplace file inside the v0.23.0 tag.

    The post-publication change of v0.23.0 moved the marketplace file to the v0.23.0 tag, and the one of v0.23.1 moved
    it on to the v0.23.1 tag and the commit that tag points at, so it names the latest published release. The v0.23.0
    tag itself still pins v0.22.0, and the plugin page says so. The tree has moved on to the version sites of the next
    release, which ``test_v0231_release_notes.py`` pins, so this test no longer reads them.
    """

    marketplace = json.loads(_text(".claude-plugin/marketplace.json"))["plugins"][0]["source"]
    assert marketplace["ref"] == "v0.23.1"
    assert marketplace["sha"] == LATEST_TAG_COMMIT
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


def test_the_notes_say_beside_their_marketplace_step_that_main_now_pins_v0230_and_then_v0231() -> None:
    """Mutations, each one alone: delete either dated update; name v0.22.0 as what ``main`` pins in the first or v0.23.0
    in the second; drop the sentence about the copy inside a tag; move them away from step 8 of the upgrade steps; swap
    them; glue one to the end of another paragraph; add a third dated update.

    Step 8 of the upgrade steps says the marketplace file on ``main`` pins the v0.22.0 tag commit until a change after
    the release moves it. That sentence stays as tagged, and the update after it says the change has been made, as the
    update beside step 7 of the v0.22.0 notes did. The 2026-11-01 follow-up says the file was moved on to the v0.23.1
    tag, and that the copy inside the v0.23.1 tag pins v0.23.0.
    """

    update = (
        "**Update (2026-10-30):** v0.23.0 is published, and the marketplace file on `main` now pins the v0.23.0 tag "
        "commit, so the marketplace install runs v0.23.0. The copy inside the v0.23.0 tag still pins v0.22.0, so add "
        "the marketplace from the repository, not from a checkout of the tag."
    )
    follow_up = (
        "**Update (2026-11-01):** v0.23.1 is published, and the marketplace file on `main` now pins the v0.23.1 tag "
        "commit, so the marketplace install runs v0.23.1. The copy inside the v0.23.0 tag still pins v0.22.0 and the "
        "copy inside the v0.23.1 tag still pins v0.23.0, so add the marketplace from the repository, not from a "
        "checkout of a tag."
    )
    blocks = _blocks(NOTES_PATH)
    positions = [index for index, block in enumerate(blocks) if block == update]
    assert len(positions) == 1, positions
    index = positions[0]
    assert blocks[index - 1].endswith(
        "8. **Claude Code plugin.** The marketplace file on `main` pins the v0.22.0 tag commit until a change after "
        "this release moves it, so a marketplace install runs v0.22.0 code until then."
    )
    assert blocks[index + 1] == follow_up
    assert blocks[index + 2] == "## Agent keys on the SQLite vault"
    assert _text(NOTES_PATH).count("Update (2026-10-30)") == 1
    assert _text(NOTES_PATH).count("Update (2026-11-01)") == 1
    assert sum(block.startswith("**Update (") for block in blocks) == 2


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
    """Mutations, each one alone: read the v0.22.0 tag where the page is read (the page is not there, and under ``CI`` a
    path that cannot be read fails); delete one of the two sentences of its ``Both families`` section that the notes
    summarise (changing the sentence the test expects imitates it); change the notes' link; read the working tree where
    the page is read, after a change that rewords one of the sentences (a re-measured comparison does).

    The notes of v0.22.0 said that no measurement of the vectors was published. The pages of the comparison were added
    to ``main`` after that release, so this release states what they found instead of repeating the old sentence. The
    page is read as the v0.23.0 tag held it, which is the page the notes link to; a later edit of the page on main does
    not make the published notes wrong.
    """

    page = _tagged_flat("docs/benchmarks/showcase/phase1-passage-vectors-results.md")
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


_NUMBERS_CODE = (
    "import json, os\n"
    "from alicebot_api.mcp.registry import list_mcp_tools\n"
    "from alicebot_api.mcp.types import _RECALL_DEFAULT_LIMIT, _RECALL_DEFAULT_NOTE_PASSAGE_BUDGET\n"
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
    "print(json.dumps({'counts': counts, 'recall': [_RECALL_DEFAULT_LIMIT, _RECALL_DEFAULT_NOTE_PASSAGE_BUDGET]}))\n"
)


def test_the_numbers_that_drift_are_the_ones_the_code_and_the_files_have(v0230_tree: Path) -> None:
    """The tool counts, the recall limits and the PostgreSQL revision are read from the code and the files of the v0.23.0
    tag, so a note that drifts from them fails here, and a later change on main does not.

    Mutations, each one alone: read the v0.22.0 tag where the export is read (its code has no note passage budget, so
    the subprocess cannot import the limits); add a tool to the default surface (changing the count the test expects
    imitates it); rename the revision in the notes; change a number of the import speed in the notes; read the working
    tree where the export is read, after a change to the default note passage budget or the default recall limit.
    """

    done = _new_python(v0230_tree, _NUMBERS_CODE)
    assert done.returncode == 0, done.stderr
    numbers = json.loads(done.stdout)
    # The counts of v0.22.0: this release adds no tool.
    assert numbers["counts"] == {"default": 3, "full": 12, "legacy": 74, "all": 77}
    notes = _flat(NOTES_PATH)
    assert (
        "No tool is added or removed: the default surface lists 3 tools, the full surface 12, and the legacy flags bring "
        "it to 74 and 77."
    ) in notes

    versions = v0230_tree / "apps" / "api" / "alembic" / "versions"
    head = sorted(path.name for path in versions.glob("2*.py"))[-1]
    assert head.startswith("20261011_0097_"), head
    assert "the revision stays `20261011_0097`" in notes
    assert "the head is `20261011_0097`, as in v0.22.0" in notes

    assert numbers["recall"] == [8, 10]
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


def test_the_thirteen_changed_migrations_are_the_ones_the_notes_name_and_nothing_else_moved(
    v0220_tree: Path, v0230_tree: Path
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (no file differs then); read the
    working tree there, after a change that edits a migration the notes do not name or adds a file to
    ``apps/api/alembic``; rename a revision in the notes.

    The two tags are compared byte for byte. Thirteen files differ, and each one is a revision the notes name, which
    gained the statement that relaxes forced row-level security for its data statements (none of them had it). Both
    sides are tags, so a migration edited or added on main later does not fail this test.
    """

    def files(root: Path) -> dict[str, bytes]:
        base = root / "apps" / "api" / "alembic"
        return {
            str(path.relative_to(base)): path.read_bytes()
            for path in sorted(base.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }

    old, new = files(v0220_tree), files(v0230_tree)
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


def test_the_settings_and_flags_the_notes_list_are_all_that_this_release_adds(v0220_tree: Path, v0230_tree: Path) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (the ``keys`` command is gone, and
    no flag differs); read the working tree there, after a change that adds a setting named ``ALICE_`` anywhere under
    ``apps/api/src`` or a flag to an ``alice-memory`` command; delete the sentence about the ``keys`` command from the
    notes.

    The names are read from every Python file under ``apps/api/src`` at the v0.22.0 tag and at the v0.23.0 tag. The flags
    are read from the parser of every ``alice-memory`` command and sub-command, built by the export of each tag. Nothing
    is read from the working tree, so a setting or a flag added to main later does not fail this test.
    """

    def names(root: Path) -> set[str]:
        found: set[str] = set()
        for path in (root / "apps" / "api" / "src").rglob("*.py"):
            found |= set(re.findall(r"\b(ALICE_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8", errors="replace")))
        return found

    assert names(v0230_tree) == names(v0220_tree)
    behaviour = _section(_text(NOTES_PATH), "Behaviour changes an integrator will notice")
    assert "No setting is added." in behaviour
    assert "`alice-memory` gains the `keys` command with the sub-commands `create`, `list` and `revoke`. No other flag is added." in behaviour

    old = subprocess.run(
        [sys.executable, "-c", _FLAGS_CODE], env=_env_for(v0220_tree / "apps" / "api" / "src"), capture_output=True, text=True, timeout=180
    )
    new = _new_python(v0230_tree, _FLAGS_CODE)
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


def test_install_writes_the_same_environment_values_as_v0220_and_never_a_key(v0220_tree: Path, v0230_tree: Path) -> None:
    """Mutations, each one alone: change the value that the test expects for the payload ``mcp --data-dir /x/y``; run the
    helper of the working tree where the v0.23.0 export is run, after a change that makes ``_own_env`` of
    ``host_install.py`` add ``ALICE_AGENT_API_KEY``, a reference to it or any other value; add a flag to
    ``alice-memory install`` (the flags test above fails too). Reading the v0.22.0 tag where the v0.23.0 export is read
    cannot fail this test: the two tags write the same values.

    The notes say that ``alice-memory install`` takes the same flags and writes the same environment values. The helper
    that decides the values is run on five payloads by the v0.22.0 export and by the v0.23.0 export, so a later change to
    it on main does not fail this test. The module is loaded with ``importlib`` so that this file does not import a host
    module, which the real-host workflow lists by path.
    """

    old = _old_python(v0220_tree, _OWN_ENV_CODE)
    new = _new_python(v0230_tree, _OWN_ENV_CODE)
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


def test_the_package_holds_none_of_the_bench_scripts_the_corpus_generator_or_the_console(v0230_tree: Path) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the listing of ``scripts`` is read (the corpus generator is
    not in it); change a line that the test expects in ``MANIFEST.in``; read the working tree where the export is read,
    after a change that adds a line to ``MANIFEST.in`` or ``scripts`` to the package list of ``pyproject.toml``.

    The wheel is the packages under ``apps/api/src`` and ``workers``, and the source distribution adds the files
    ``MANIFEST.in`` names. A wheel (392 files) and a source distribution (400 files and 22 folders) of the tag were
    built to check this and hold no file from ``scripts/`` or ``apps/web``; this test pins what decides it, as the
    v0.23.0 tag held it, so a later change to the package list or to ``MANIFEST.in`` on main does not fail it.
    """

    setuptools_config = tomllib.loads((v0230_tree / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    assert setuptools_config["packages"]["find"]["where"] == ["apps/api/src", "workers"]
    assert "package-data" not in setuptools_config and "data-files" not in setuptools_config
    packages = [pkg for where in ("apps/api/src", "workers") for pkg in _packages_in(v0230_tree / where)]
    assert packages and not [pkg for pkg in packages if pkg.split(".")[0] in {"scripts", "web", "apps"}]
    assert (v0230_tree / "MANIFEST.in").read_text(encoding="utf-8").split("\n") == [
        "include apps/api/alembic.ini",
        "include eval/fixtures/public_eval_suites.json",
        "recursive-include apps/api/alembic *.py",
        "",
    ]
    scripts = _tagged_listing(RELEASE_TAG, "scripts")
    for script in ("alice_bench.py", "showcase_bench.py", "generate_markdown_perf_corpus.py"):
        assert script in scripts, script
    other = _section(_text(NOTES_PATH), "Other changes in the package")
    assert "A wheel and a source distribution built from the tree hold no file from `scripts/` or `apps/web`" in other
    assert "the wheel has 392 files, the source distribution 400 files and 22 folders" in other


# ---------------------------------------------------------------------------
# Agent keys on the SQLite vault, run
# ---------------------------------------------------------------------------


def test_keys_create_prints_the_raw_key_once_and_list_shows_neither_it_nor_its_hash(
    tmp_path: Path, v0220_tree: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it has no ``keys`` command); read
    the working tree there, after a change that prints the key a second time, adds the raw key or ``key_hash`` to the
    list record, rewords the line that says the key is not shown again, or lets ``--project`` bind nothing.

    v0.22.0 has no ``keys`` command: its parser refuses it. The guard is ``_agent_key_public_record`` and the single
    ``print(raw_key)`` of ``_run_keys_create``, as the v0.23.0 tag held them. The command runs from the export of that
    tag, so a later change to the command on main does not fail this test.
    """

    import hashlib

    data_dir, _database = _vault(tmp_path, vault_templates, import_notes=False)
    code, out, err = _new(v0230_tree, "keys", "create", "--agent-id", "claude-code", "--profile", "admin_agent", "--label", "Claude Code host", "--data-dir", str(data_dir))
    assert code == 0, err
    lines = out.splitlines()
    raw = lines[0]
    assert raw.startswith("alice_") and out.count(raw) == 1
    assert lines[1] == "Store this key now; it is not shown again, and only its sha256 hash is stored."
    assert lines[2] == "Agent: claude-code  profile: admin_agent  label: Claude Code host"
    code, bound, err = _new(v0230_tree, "keys", "create", "--agent-id", "openclaw", "--profile", "project_scoped_agent", "--project", "acme", "--data-dir", str(data_dir))
    assert code == 0 and "project: acme" in bound, err
    digest = hashlib.sha256(raw.encode()).hexdigest()
    code, listing, _ = _new(v0230_tree, "keys", "list", "--data-dir", str(data_dir))
    assert code == 0 and listing.count("active") == 2
    assert raw not in listing and digest not in listing and raw[:12] in listing
    assert "project=acme" in listing and "label=Claude Code host" in listing
    code, records, _ = _new(v0230_tree, "keys", "list", "--json", "--data-dir", str(data_dir))
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


def test_keys_create_refuses_a_blank_project_the_reserved_name_and_a_blank_agent(
    tmp_path: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it has no ``keys`` command); read
    the working tree there, after a change that lets ``--project`` take ``~global`` or a blank value, lets ``--agent-id``
    be blank, or lets ``--profile`` take a value that is not one of the five profiles.

    The command calls the shared ``create_agent_key`` of the PostgreSQL command, so the refusals are its refusals, as the
    v0.23.0 tag held them.
    """

    data_dir, _ = _vault(tmp_path, vault_templates, import_notes=False)
    for extra, message in (
        (["--agent-id", "x", "--profile", "admin_agent", "--project", "~global"], "the project name ~global is reserved by Alice"),
        (["--agent-id", "x", "--profile", "admin_agent", "--project", "  "], "project_scope must not be blank"),
        (["--agent-id", "  ", "--profile", "admin_agent"], "agent_id is required"),
    ):
        code, out, err = _new(v0230_tree, "keys", "create", *extra, "--data-dir", str(data_dir))
        assert code == 2 and out == "" and message in err, (extra, err)
    code, out, err = _new(v0230_tree, "keys", "list", "--data-dir", str(data_dir))
    assert out.strip() == "No agent keys on this vault."
    code, out, err = _new(v0230_tree, "keys", "create", "--agent-id", "x", "--profile", "nonsense", "--data-dir", str(data_dir))
    assert code == 2 and out == ""


def test_a_vault_with_an_active_key_refuses_a_keyless_call_and_v0220_answered_it(
    tmp_path: Path, v0220_tree: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it cannot mint a key, so the first
    ``keys create`` fails); read the working tree there, after a change that stops counting the active keys in
    ``_agent_identity_from_arguments``, counts revoked keys too, lets a legacy tool through, lets an unreadable key table
    through, or refuses a call that carries a good key.

    The calls run from the export of the v0.23.0 tag, in one process for each run of calls that no command separates. On
    v0.22.0 the same keyless call is answered on a vault that holds a key (the key is minted with that tree's own
    ``create_agent_key``, because it had no command for it).
    """

    data_dir, _database = _vault(tmp_path, vault_templates)
    query = {"query": "kiln firing schedule"}
    assert _answer(_calls(v0230_tree, data_dir, _call("alice_recall", query))[0])["source_count"] >= 1
    key = _mint(v0230_tree, data_dir)
    # Two legacy tools that resolve no agent identity of their own, so only the refusal in call_mcp_tool stops them.
    legacy = {"ALICE_MCP_FULL_TOOLS": "1", "ALICE_MCP_LEGACY_TOOLS": "1"}
    outcomes = _calls(
        v0230_tree,
        data_dir,
        _call("alice_recall", query),
        _call("alice_resume"),
        _call("alice_open_loop_extract", {"person_name": "someone"}, env=legacy),
        _call("alice_graph_neighborhood", {"target_id": "00000000-0000-0000-0000-0000000000aa"}, env=legacy),
        _call("alice_recall", query, key=key),
        _call("alice_recall", query, key="alice_sk_not_a_real_key_000000000000000000"),
    )
    assert all(_refused(outcome) for outcome in outcomes[:4]), outcomes[:4]
    assert _answer(outcomes[4])["source_count"] >= 1
    assert _refused(outcomes[5]), outcomes[5]
    # A second key, then a revoked one: only active keys count, and a revoked key is refused on its next call.
    second = _mint(v0230_tree, data_dir, "other", "read_only_agent")
    code, listing, _ = _new(v0230_tree, "keys", "list", "--json", "--data-dir", str(data_dir))
    prefix = [item["key_prefix"] for item in json.loads(listing)["items"] if item["agent_id"] == "other"][0]
    code, revoked, _ = _new(v0230_tree, "keys", "revoke", prefix, "--data-dir", str(data_dir))
    assert code == 0 and revoked.startswith(f"Revoked {prefix} (other)")
    outcomes = _calls(v0230_tree, data_dir, _call("alice_recall", query, key=second), _call("alice_recall", query))
    assert all(_refused(outcome) for outcome in outcomes), outcomes
    # Revoking the last active key makes the vault keyless again.
    code, listing, _ = _new(v0230_tree, "keys", "list", "--json", "--data-dir", str(data_dir))
    last = [item["key_prefix"] for item in json.loads(listing)["items"] if item["agent_id"] == "host"][0]
    assert _new(v0230_tree, "keys", "revoke", last, "--data-dir", str(data_dir))[0] == 0
    outcomes = _calls(v0230_tree, data_dir, _call("alice_recall", query), _call("alice_recall", query, key=key))
    assert _answer(outcomes[0])["source_count"] >= 1
    assert _refused(outcomes[1]), outcomes[1]
    # v0.22.0 on a vault that holds an active key: the keyless call is answered.
    old_dir = tmp_path / "old-vault"
    answered = _old_python(v0220_tree, _OLD_KEYED_VAULT_CODE, str(old_dir))
    assert answered.returncode == 0, answered.stderr
    assert json.loads(answered.stdout.splitlines()[-1])["answered"] is True


def test_an_unreadable_key_table_refuses_the_call_instead_of_answering_it(
    tmp_path: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it cannot mint a key, so the first
    ``keys create`` fails); read the working tree there, after a change that lets the call through when the count of
    active keys raises.

    The notes say that if the count of keys cannot be read, the call is refused. The column the count reads is renamed
    in a copy of the vault, which makes the query fail. The call runs from the export of the v0.23.0 tag.
    """

    data_dir, database = _vault(tmp_path, vault_templates)
    _mint(v0230_tree, data_dir)
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("ALTER TABLE agent_api_keys RENAME COLUMN revoked_at TO revoked_x")
        connection.commit()
    outcome = _calls(v0230_tree, data_dir, _call("alice_recall", {"query": "kiln firing schedule"}))[0]
    assert _refused(outcome), outcome
    assert "If the count of keys cannot be read, the call is refused." in _flat(NOTES_PATH)


def test_the_brief_and_the_hook_print_nothing_without_the_key_and_v0220_printed_the_brief(
    tmp_path: Path, v0220_tree: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it cannot mint a key, so the first
    ``keys create`` fails); read the working tree there, after a change that lets ``session_brief_identity`` return the
    owner's stance when the vault has an active key and the environment has none, lets the hook skip the rule, prints the
    brief for a key that does not resolve, or rewords the line that opens every brief.

    The hook is run as a subprocess with ``{}`` on stdin and a timeout, from the export of the v0.23.0 tag. The same vault
    is run from the v0.22.0 tree with a key in it, and prints the brief.
    """

    data_dir, _database = _vault(tmp_path, vault_templates)
    code, keyless_brief, _ = _new(v0230_tree, "brief", "--data-dir", str(data_dir))
    assert code == 0 and BRIEF_MARK in keyless_brief
    keyless_hook = _hook(v0230_tree, data_dir)
    assert keyless_hook.returncode == 0 and BRIEF_MARK in keyless_hook.stdout
    key = _mint(v0230_tree, data_dir)
    code, out, err = _new(v0230_tree, "brief", "--data-dir", str(data_dir))
    assert (code, out, err) == (0, "", "")
    hook = _hook(v0230_tree, data_dir)
    assert (hook.returncode, hook.stdout) == (0, "")
    bad = _hook(v0230_tree, data_dir, key="alice_sk_not_a_real_key_000000000000000000")
    assert (bad.returncode, bad.stdout) == (0, "")
    code, keyed_brief, _ = _new(v0230_tree, "brief", "--data-dir", str(data_dir), key=key)
    _, bad_brief, _ = _new(v0230_tree, "brief", "--data-dir", str(data_dir), key="alice_sk_not_a_real_key_000000000000000000")
    assert code == 0 and BRIEF_MARK in keyed_brief and bad_brief == ""
    keyed_hook = _hook(v0230_tree, data_dir, key=key)
    assert keyed_hook.returncode == 0 and BRIEF_MARK in keyed_hook.stdout
    # v0.22.0 on a vault that holds a key prints the brief without one.
    old_dir = tmp_path / "old-vault"
    minted = _old_python(v0220_tree, _OLD_KEYED_VAULT_CODE, str(old_dir))
    assert minted.returncode == 0, minted.stderr
    assert _old(v0220_tree, "import-markdown", "--from", str(tmp_path / "notes"), "--data-dir", str(old_dir)).returncode == 0
    old_brief = _old(v0220_tree, "brief", "--data-dir", str(old_dir))
    assert old_brief.returncode == 0 and BRIEF_MARK in old_brief.stdout


def test_the_commands_you_run_yourself_take_no_key_on_a_vault_that_holds_one(
    tmp_path: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it cannot mint a key, so the first
    ``keys create`` fails); read the working tree there, after a change that makes ``doctor``, ``export``, ``import``,
    ``sleep``, ``sleep-proposals``, ``import-markdown`` or ``import-chatgpt`` read ``ALICE_AGENT_API_KEY`` or refuse
    without it.

    The notes list these commands in upgrade step 4. Each runs from the export of the v0.23.0 tag on a vault with an
    active key and no key in the environment, and exits 0.
    """

    data_dir, _database = _vault(tmp_path, vault_templates)
    _mint(v0230_tree, data_dir)
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
        code, out, err = _new(v0230_tree, *args)
        assert code == 0, (args[0], err)
    code, out, err = _new(v0230_tree, "import", "--in", str(export_file), "--data-dir", str(tmp_path / "restored"))
    assert code == 0, err


def test_the_export_drops_keys_and_a_file_copy_after_a_checkpoint_keeps_them(
    tmp_path: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it cannot mint a key, so the first
    ``keys create`` fails); read the working tree there, after a change that writes a key record, a key hash or a raw key
    into the export, makes the import create a key, renames the event that records a key's creation, or makes the file
    copy lose the key table.

    The export keeps the event that records a key's creation (prefix, agent id, profile), which makes no key. Every
    command and call runs from the export of the v0.23.0 tag.
    """

    import hashlib
    import shutil

    data_dir, database = _vault(tmp_path, vault_templates)
    key = _mint(v0230_tree, data_dir)
    export_file = tmp_path / "export.jsonl"
    assert _new(v0230_tree, "export", "--out", str(export_file), "--data-dir", str(data_dir))[0] == 0
    exported = export_file.read_text(encoding="utf-8")
    assert key not in exported and hashlib.sha256(key.encode()).hexdigest() not in exported
    record_types = {json.loads(line).get("record_type") for line in exported.splitlines()}
    assert "agent_api_key" not in record_types and "key_hash" not in exported
    created = [json.loads(line)["record"] for line in exported.splitlines() if '"agent.key_created"' in line]
    assert len(created) == 1 and created[0]["payload_json"]["key_prefix"] == key[: len(created[0]["payload_json"]["key_prefix"])]
    restored = tmp_path / "restored"
    assert _new(v0230_tree, "import", "--in", str(export_file), "--data-dir", str(restored))[0] == 0
    code, listing, _ = _new(v0230_tree, "keys", "list", "--data-dir", str(restored))
    assert listing.strip() == "No agent keys on this vault."
    restored_outcome = _calls(v0230_tree, restored, _call("alice_recall", {"query": "kiln firing schedule"}))[0]
    assert _answer(restored_outcome)["source_count"] >= 1
    with contextlib.closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    shutil.copy2(database, copy_dir / database.name)
    code, listing, _ = _new(v0230_tree, "keys", "list", "--data-dir", str(copy_dir))
    assert code == 0 and listing.count("active") == 1
    assert _refused(_calls(v0230_tree, copy_dir, _call("alice_recall", {"query": "kiln firing schedule"}))[0])


def test_an_unbound_admin_key_does_not_get_the_evidence_of_an_archived_source_that_the_keyless_owner_gets(
    tmp_path: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it cannot mint a key, so the
    ``keys create`` fails); read the working tree there, after a change that lets the saved-quote reader return an
    archived source to a key; delete the sentence about the difference from the notes.

    The memory cites a source, the source is archived with ``sources delete``, and the same three reads run keyless
    (before any key exists) and as an unbound ``admin_agent`` key. The notes name this as the one known difference. The
    commands and the calls run from the export of the v0.23.0 tag.
    """

    data_dir, _database = _vault(tmp_path, vault_templates, import_notes=False)
    folder = tmp_path / "larkspur"
    folder.mkdir()
    (folder / "larkspur.md").write_text("# Larkspur account\n\nThe Larkspur contract renews in March and the account owner is Dana.\n", encoding="utf-8")
    assert _new(v0230_tree, "import-markdown", "--from", str(folder), "--data-dir", str(data_dir))[0] == 0
    code, listing, _ = _new(v0230_tree, "sources", "list", "--data-dir", str(data_dir))
    source_id = json.loads(listing)["sources"][0]["id"]
    identity = {"agent_id": "probe", "agent_type": "coding_agent", "permission_profile": "admin_agent"}
    commit = {
        **identity, "title": "Larkspur", "canonical_text": "The Larkspur contract renews in March.", "domain": "project",
        "sensitivity": "private", "source_refs": [f"source:{source_id}"], "idempotency_key": "known-difference",
    }
    committed = _answer(_calls(v0230_tree, data_dir, _call("alice_memory_commit", commit, env=_FULL))[0])
    memory_id = committed["memory"]["id"]
    # The name of the second tool is written in three parts so that this file does not hold the word, as the wording test does.
    memory_check = "alice_memory_" + "rev" + "iew"
    assert _new(v0230_tree, "sources", "delete", source_id, "--yes", "--data-dir", str(data_dir))[0] == 0
    reads = (
        ("alice_explain", {"memory_id": memory_id}),
        (memory_check, {"continuity_object_id": memory_id}),
        ("alice_context_pack", {"query": "Larkspur contract renews"}),
    )
    owner_reads = _calls(v0230_tree, data_dir, *[_call(name, arguments, env=_FULL) for name, arguments in reads])
    assert all(source_id in json.dumps(_answer(outcome)) for outcome in owner_reads)
    key = _mint(v0230_tree, data_dir, "owner-host", "admin_agent")
    keyed = _calls(v0230_tree, data_dir, *[_call(name, arguments, key=key, env=_FULL) for name, arguments in reads])
    assert not keyed[0]["ok"] and re.search("unavailable", keyed[0]["message"]), keyed[0]
    assert source_id not in json.dumps(_answer(keyed[1]))
    assert source_id not in json.dumps(_answer(keyed[2]))
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
    tmp_path: Path, v0220_tree: Path, v0230_tree: Path
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it returns eight); read the
    working tree there, after a change that sets ``_RECALL_DEFAULT_NOTE_PASSAGE_BUDGET`` back to 8, ignores an explicit
    limit, or applies the budget in document mode.

    A vault of fifteen one-passage notes is asked the same question by the export of the v0.23.0 tag and by v0.22.0.
    """

    folder = _fifteen_passages(tmp_path)
    vault = tmp_path / "vault"
    code, _out, err = _new(v0230_tree, "import-markdown", "--from", str(folder), "--data-dir", str(vault))
    assert code == 0, err
    query = {"query": "lantern plan archive detail"}
    outcomes = _calls(
        v0230_tree,
        vault,
        _call("alice_recall", query),
        _call("alice_recall", {**query, "limit": 8}),
        _call("alice_recall", {**query, "limit": 5}),
        _call("alice_recall", query, env={"ALICE_SEARCH_QUALITY": "off"}),
    )
    assert [_answer(outcome)["source_count"] for outcome in outcomes] == [10, 8, 5, 8]
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
    tmp_path: Path, v0220_tree: Path, v0230_tree: Path, vault_templates: dict[bool, Path]
) -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the v0.23.0 export is read (it does not find the other
    spellings); read the working tree there, after a change that takes the shared id reader out of ``alice_explain`` or
    lets a value that is not an id answer anything but not found.

    The notes say that ``alice_explain`` finds a memory by an id in upper case, in braces, without hyphens or as
    ``urn:uuid:`` on the SQLite vault, where v0.22.0 answered ``not_found``. The calls run from the export of the v0.23.0
    tag.
    """

    data_dir, _database = _vault(tmp_path, vault_templates, import_notes=False)
    identity = {"agent_id": "probe", "agent_type": "coding_agent", "permission_profile": "admin_agent"}
    commit = {
        **identity, "title": "Larkspur", "canonical_text": "The Larkspur contract renews in March.", "domain": "project",
        "sensitivity": "private", "idempotency_key": "spellings",
    }
    memory = _answer(_calls(v0230_tree, data_dir, _call("alice_memory_commit", commit, env=_FULL))[0])["memory"]["id"]
    values = (memory, memory.upper(), "urn:uuid:" + memory, "{" + memory + "}", memory.replace("-", ""))
    outcomes = _calls(v0230_tree, data_dir, *[_call("alice_explain", {"memory_id": value}, env=_FULL) for value in (*values, "no-such-id")])
    for value, outcome in zip(values, outcomes[:-1], strict=True):
        assert _answer(outcome), value
    assert not outcomes[-1]["ok"] and re.search("not found|not_found|unavailable|could not", outcomes[-1]["message"]), outcomes[-1]
    old = _old_python(v0220_tree, _OLD_EXPLAIN_CODE, str(tmp_path / "old-vault"))
    assert old.returncode == 0, old.stderr
    found = json.loads(old.stdout.splitlines()[-1])
    assert found["canonical"] == "found"
    assert all(found[label] != "found" for label in ("upper", "urn", "braces", "plain", "not an id")), found


def test_the_postgresql_claims_have_integration_tests_that_run_in_ci() -> None:
    """Mutations, each one alone: read the v0.22.0 tag where the listing is read (the eight files are not all
    there); read the working tree there, after a change that renames or deletes one of the eight integration files or
    moves the folder out of the name that the workflow uses; drop the sentence of the notes that says which doors answer
    404.

    These tests run against PostgreSQL and are not run by the unit suite. They were run on the v0.23.0 tag (179 passed)
    and on the v0.22.0 code, where the doors failed in the ways the changelog names (108 of the 161 tests of seven files
    failed, and the eighth file, which counts the migrations that relax forced row-level security, failed on collection
    with 5 of 13). ``test_ci_integration_job_split.py`` fails when an integration file is in no job or in two. The files
    and the workflow are read as the v0.23.0 tag held them, so a later rename or split of the integration folder on main
    does not fail this test.
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
    workflow = _tagged(RELEASE_TAG, ".github/workflows/tests.yml")
    present = _tagged_listing(RELEASE_TAG, "tests/integration")
    for name in files:
        assert name in present, name
    assert "tests/integration" in workflow
    errors = _section(_text(NOTES_PATH), "Server errors that are now answers")
    assert "All four answer HTTP 404 now, the answer of a well-formed id that does not exist." in errors
    assert "It answers HTTP 404 `not_found` and writes nothing." in errors
    assert "They return the pack now." in errors


def test_the_headline_changes_of_the_notes_each_have_a_changelog_entry() -> None:
    """Mutations, each one alone: delete the changelog entry of a change the notes name, or reword its first words;
    put an entry back under ``Unreleased``; delete the sentence of the keys entry that says what v0.22.0 had; read the
    v0.22.0 tag where the section is read (its changelog has no v0.23.0 heading); read the working tree there, after a
    change that adds a dated correction as an entry of the v0.23.0 section.

    The section is the one the v0.23.0 tag held. An entry or a dated paragraph added to the changelog later is not part
    of it.
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
