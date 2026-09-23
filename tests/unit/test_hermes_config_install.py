"""alice-memory install --host hermes: never change the meaning of config.yaml.

Why this exists. Until 2026-09-22, install read ``~/.hermes/config.yaml``
with a small hand parser, then wrote the whole file back from that parse.
The parser did not know YAML: an inline comment became part of the value
(``model: gpt-4o  # default model`` came back as the string
``"gpt-4o  # default model"``), ``- name: web`` list items became the
strings ``"name: web"``, ``yes`` / ``no`` became the strings ``"yes"`` /
``"no"``, ``\\t`` and ``\\u00e9`` escapes came back as literal backslashes,
and every comment, header included, was dropped. No backup was taken and the
exit code was 0. The real Hermes binary then read the new values: ``hermes
config get model`` printed ``gpt-4o  # default model``.

How it escaped. The writer tests read the written file back with the same
hand parser that wrote it, so a wrong reading and a wrong writing agreed.
Hermes reads its config with PyYAML (``yaml.safe_load`` semantics), and
nothing in the suite did.

Install now inserts the ``mcp_servers.alice`` lines into the file text and
leaves every other byte alone, backs the file up first, and refuses, leaving
the file untouched and printing the snippet, when the file uses YAML it
cannot place lines into safely. These tests judge every outcome with
PyYAML, the loader Hermes uses, and, when ALICE_TEST_REAL_HOSTS=1 and
``hermes`` is on PATH, with the real binary.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import random
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import scripts.fuzz_hermes_config_writer as fuzz
from alicebot_api import host_install
from alicebot_api.host_install import host_file_map, mcp_server_payload
from alicebot_api.onramp import _ERROR_CONTRACTS, main as onramp_main

pytestmark = pytest.mark.usefixtures("uvx_on_path")
BS = chr(92)  # one backslash, spelled so no escape processing can touch it
E_ACUTE = chr(0xE9)
BACKUP_GLOB = "config.yaml.alice-backup-*"
REAL_HOSTS_ENV = "ALICE_TEST_REAL_HOSTS"
INSTALL_REFUSED = {
    "error": {
        "code": "install_refused",
        "message": _ERROR_CONTRACTS["install_refused"],
    }
}
INSTALL_FAILED = {
    "error": {
        "code": "install_failed",
        "message": _ERROR_CONTRACTS["install_failed"],
    }
}

# The hand-written config from the 2026-09-03 audit, one line per corruption.
CORRUPTION_FIXTURE = (
    "# Hermes config, written by hand.\n"
    "# Keep this header.\n"
    "\n"
    "model: gpt-4o  # default model\n"
    "toolsets:\n"
    "  - name: web\n"
    "  - name: terminal\n"
    "display:\n"
    "  compact: yes\n"
    "  streaming: no\n"
    f'  greeting: "tab{BS}there {BS}u00e9"\n'
    "  prompt: 'single # not a comment'\n"
    "mcp_servers:\n"
    "  other:\n"
    "    command: node\n"
    "    args:\n"
    "      - other.js\n"
    "\n"
    "# Trailing comment.\n"
)


def _install(home: Path, vault: Path, capsys, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(
        ["install", "--home", str(home), "--data-dir", str(vault), "--host", "hermes", *extra]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _config_path(home: Path) -> Path:
    return host_file_map(home.resolve())["hermes"]["mcp"]


def _seed(home: Path, content: str | bytes) -> Path:
    path = _config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_bytes(content.encode("utf-8"))
    return path


def _expected_alice(vault: Path) -> dict:
    return mcp_server_payload(str(vault.resolve()), with_env=True)


def _with_alice(before: object, vault: Path) -> dict:
    """``before`` plus mcp_servers.alice, and nothing else changed."""

    expected = copy.deepcopy(before) if isinstance(before, dict) else {}
    servers = expected.get("mcp_servers")
    servers = dict(servers) if isinstance(servers, dict) else {}
    servers["alice"] = _expected_alice(vault)
    expected["mcp_servers"] = servers
    return expected


def _single_insertion(before: str, after: str) -> str:
    """Return the inserted text when ``after`` is ``before`` plus one contiguous insert."""

    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(before) - prefix
        and suffix < len(after) - prefix
        and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]
    ):
        suffix += 1
    assert prefix + suffix == len(before), "original bytes were changed, not only added to"
    return after[prefix : len(after) - suffix]


def _backups(config: Path, data_dir: Path | None = None) -> list[Path]:
    """Backups of config.yaml: in <data dir>/backups/host-configs, and never beside it.

    Since review round 4 (tower decision) every backup goes to one 0700
    directory under the data dir. Without ``data_dir`` this lists what
    sits next to the file, which must stay empty.
    """

    if data_dir is None:
        return sorted(config.parent.glob(BACKUP_GLOB))
    return sorted((data_dir / "backups" / "host-configs").glob(f"hermes-{BACKUP_GLOB}"))


def _error_records(stderr: str) -> list[object]:
    return [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]


def test_fixture_exercises_every_corruption_class() -> None:
    """Guards the guard: the fixture really contains each construct that broke.

    If the fixture drifted (say the ``yes`` became ``true``), the tests below
    would pass without exercising that corruption. Mutation: replace any of
    the five constructs in CORRUPTION_FIXTURE. This test fails.
    """

    loaded = yaml.safe_load(CORRUPTION_FIXTURE)
    # The raw spellings matter, not only the parsed values: ``true`` parses
    # like ``yes`` but the old writer only broke ``yes``.
    for raw in (
        "model: gpt-4o  # default model\n",
        "  - name: web\n",
        "  compact: yes\n",
        "  streaming: no\n",
        f"{BS}t",
        f"{BS}u00e9",
    ):
        assert raw in CORRUPTION_FIXTURE, raw
    assert CORRUPTION_FIXTURE.startswith("# Hermes config, written by hand.\n")
    assert loaded["model"] == "gpt-4o"
    assert loaded["toolsets"] == [{"name": "web"}, {"name": "terminal"}]
    assert loaded["display"]["compact"] is True
    assert loaded["display"]["streaming"] is False
    assert loaded["display"]["greeting"] == f"tab\there {E_ACUTE}"
    assert loaded["display"]["prompt"] == "single # not a comment"
    assert loaded["mcp_servers"] == {"other": {"command": "node", "args": ["other.js"]}}


def test_install_keeps_every_existing_value_and_only_adds_alice(
    tmp_path: Path, capsys
) -> None:
    """After install, PyYAML reads the original mapping plus alice, byte for byte.

    Every original byte, comments included, is still there in order; the only
    change is one inserted block. Mutation: write the file back from a parse
    (the old writer), or drop the comment lines. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    config = _seed(home, CORRUPTION_FIXTURE)

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    after_text = config.read_bytes().decode("utf-8")

    assert yaml.safe_load(after_text) == _with_alice(yaml.safe_load(CORRUPTION_FIXTURE), vault)
    inserted = _single_insertion(CORRUPTION_FIXTURE, after_text)
    assert yaml.safe_load("mcp_servers:\n" + inserted) == {
        "mcp_servers": {"alice": _expected_alice(vault)}
    }
    assert after_text.startswith("# Hermes config, written by hand.\n# Keep this header.\n")
    assert after_text.endswith("\n# Trailing comment.\n")
    assert "action: written" in out


def test_backup_is_byte_identical_private_and_named_in_the_receipt(
    tmp_path: Path, capsys
) -> None:
    """A timestamped backup of the original is written before the edit.

    Mutation: skip the backup, or write it from the edited text. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    original = CORRUPTION_FIXTURE.encode("utf-8")
    config = _seed(home, original)

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert _backups(config) == []
    backups = _backups(config, vault)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(backups[0].parent.parent.stat().st_mode) == 0o700
    assert f"backup: {backups[0]}" in out
    assert config.read_bytes() != original


def test_second_run_changes_nothing_and_takes_no_second_backup(
    tmp_path: Path, capsys
) -> None:
    """Re-running install is a no-op: same bytes, one alice, no new backup.

    Mutation: always insert a new alice block, or always rewrite and back up.
    This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    config = _seed(home, CORRUPTION_FIXTURE)
    assert _install(home, vault, capsys)[0] == 0
    first = config.read_bytes()

    code, out, err = _install(home, vault, capsys)
    assert code == 0, err
    assert config.read_bytes() == first
    assert len(_backups(config, vault)) == 1
    assert "action: unchanged" in out
    assert first.decode("utf-8").count("\n  alice:\n") == 1


def test_existing_alice_block_is_replaced_in_place(
    tmp_path: Path, capsys
) -> None:
    """An alice block v0.16.0 wrote for another vault is replaced, not duplicated.

    Lines outside alice's block keep their bytes, including the server after
    it. Mutation: append a second alice, or rewrite the siblings. This test
    fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    head = "# servers\nmcp_servers:\n  other:\n    command: node\n"
    old_alice = (
        "  alice:\n"
        "    command: uvx\n"
        "    args:\n"
        "      - alice-memory\n"
        "      - mcp\n"
        '      - "--data-dir"\n'
        "      - /old/vault\n"
        "    env:\n"
        "      ALICE_MEMORY_DATA_DIR: /old/vault\n"
    )
    tail = "  zeta:  # after alice\n    command: zeta-server\nmodel: gpt-4o  # kept\n"
    config = _seed(home, head + old_alice + tail)
    assert yaml.safe_load(head + old_alice + tail)["mcp_servers"]["alice"]["args"][-1] == "/old/vault"

    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    after_text = config.read_text(encoding="utf-8")
    assert after_text.startswith(head)
    assert after_text.endswith(tail)
    loaded = yaml.safe_load(after_text)
    assert loaded == _with_alice(yaml.safe_load(head + old_alice + tail), vault)
    assert list(loaded["mcp_servers"]) == ["other", "alice", "zeta"]
    assert after_text.count("alice:") == 1


@pytest.mark.parametrize(
    ("label", "original"),
    [
        ("no mcp_servers, final newline", "model: gpt-4o  # kept\n"),
        ("no mcp_servers, no final newline", "model: gpt-4o  # kept"),
        ("comments only", "# nothing configured yet\n# model: gpt-4o\n"),
        ("empty mcp_servers", "mcp_servers:  # none yet\nmodel: gpt-4o\n"),
        ("empty flow mcp_servers", "mcp_servers: {}  # none yet\nmodel: gpt-4o\n"),
        ("null mcp_servers", "mcp_servers: ~\nmodel: gpt-4o\n"),
        ("crlf", "model: gpt-4o\r\nmcp_servers:\r\n  other:\r\n    command: node\r\n"),
        (
            "byte order mark before mcp_servers",
            "\ufeffmcp_servers:\n  other: {command: node}\nmodel: gpt-4o\n",
        ),
        ("space before the colon", "mcp_servers :\n  other: {command: node}\n"),
        ("quoted key", "'mcp_servers':\n  other: {command: node}\n"),
        (
            "keep-chomped block scalar last in mcp_servers",
            "mcp_servers:\n  other:\n    notes: |+\n      keep these blank lines\n\n\n# next\nmodel: gpt-4o\n",
        ),
        (
            "block scalar at end of file",
            "prompt: >-\n  folded text\n  mcp_servers: inside the scalar\n",
        ),
        ("four-space indentation", "mcp_servers:\n    other:\n        command: node\n"),
        ("compact child list", "mcp_servers:\n  other:\n    args:\n    - a.js\ntoolsets:\n- web\n"),
        ("compact list at the child indent", "mcp_servers:\n  other:\n  - a.js\nmodel: gpt-4o\n"),
        ("document start marker", "---\nmodel: gpt-4o\n"),
        (
            "alias inside old alice to an anchor outside it",
            (
                "tool: &outside uvx\nmcp_servers:\n  alice:\n    command: *outside\n"
                "    args: [alice-memory, mcp]\n"
            ),
        ),
        (
            "tabs inside a quoted value and in block scalar text",
            'greeting: "a\tb"\nnotes: |\n  a\tb\nmodel: x\n',
        ),
    ],
)
def test_supported_shapes_keep_their_meaning(
    tmp_path: Path, capsys, label: str, original: str
) -> None:
    """Each shape the writer claims to handle ends as the original plus alice.

    Mutation: drop the CRLF handling, the BOM handling, the block-scalar
    placement rule, the ``{}`` / ``~`` rewrite, or the final-newline fix.
    One of these cases fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    config = _seed(home, original)
    before = yaml.safe_load(original)

    code, out, err = _install(home, vault, capsys)
    assert code == 0, (label, out, err)
    after_text = config.read_bytes().decode("utf-8")
    assert yaml.safe_load(after_text) == _with_alice(before, vault), (label, after_text)
    if "\r\n" in original:
        assert after_text.count("\r\n") == after_text.count("\n")
    if original.startswith("\ufeff"):
        assert after_text.startswith("\ufeff")
    for line in original.splitlines():
        if line.lstrip().startswith("#"):
            assert line in after_text, (label, line)


_REFUSALS = [
    (
        "flow list across lines",
        "mcp_servers:\n  other:\n    command: node\n    args: [\n      other.js\n    ]\n",
        "a flow value continues on the next line",
        True,
    ),
    (
        "key-looking text inside a multi-line quoted string",
        'note: "starts here\nmcp_servers: not a key"\n',
        "a quoted value continues on the next line",
        True,
    ),
    (
        "anchor on mcp_servers used elsewhere",
        "mcp_servers: &servers\n  other:\n    command: node\nbackup_servers: *servers\n",
        "mcp_servers has an anchor, tag, alias or inline value",
        True,
    ),
    (
        "merge key at the top level",
        "defaults: &d\n  mcp_servers:\n    other:\n      command: node\n<<: *d\n",
        "a merge key (<<)",
        True,
    ),
    (
        "mcp_servers twice",
        "mcp_servers:\n  a: {command: x}\nmcp_servers:\n  b: {command: y}\n",
        "mcp_servers appears more than once",
        True,
    ),
    ("mcp_servers is a list", "mcp_servers:\n  - other\n", "mcp_servers is not a mapping", True),
    (
        "mcp_servers is a non-empty flow mapping",
        "mcp_servers: {other: {command: node}}\n",
        "mcp_servers has an anchor, tag, alias or inline value",
        True,
    ),
    ("top level is a list", "- a\n- b\n", "the top level is not a mapping", True),
    ("top-level keys indented", "  model: gpt-4o\n  other: 1\n", "the top level is not a mapping", True),
    ("explicit key", "? mcp_servers\n: {}\n", "an explicit '?' key", True),
    (
        "escaped quoted key",
        f'"mcp{BS}x5fservers":\n  other: {{command: node}}\n',
        "a key this writer does not read",
        True,
    ),
    (
        "file ends inside a block scalar with no final newline",
        "notes: |\n  keep this",
        "the file ends inside a block scalar",
        True,
    ),
    ("mixed line endings", "model: gpt-4o\r\nother: 1\n", "mixed line endings", True),
    ("tab indentation", "mcp_servers:\n\tother: 1\n", "a line is indented with a tab", False),
    ("second document", "model: gpt-4o\n---\nother: 1\n", "more than one YAML document", False),
    ("directive", "%YAML 1.1\n---\nmodel: gpt-4o\n", "a YAML directive", True),
    (
        "old alice block defines an anchor used elsewhere",
        "mcp_servers:\n  alice: &a\n    command: old\nbackup: *a\n",
        "mcp_servers.alice defines an anchor",
        True,
    ),
    (
        "flow anchor in old alice command, aliased outside",
        "mcp_servers:\n  alice: {command: &cmd uvx, args: [x]}\n  foo:\n    command: *cmd\n",
        "mcp_servers.alice defines an anchor",
        True,
    ),
    (
        "flow anchor in old alice args, aliased outside",
        (
            "mcp_servers:\n  alice:\n    command: uvx\n    args: [&a alice-memory, mcp]\n"
            "  foo:\n    command: node\n    args: [*a]\n"
        ),
        "mcp_servers.alice defines an anchor",
        True,
    ),
    (
        "block scalar header on its own line, no final newline",
        "model: gpt-4o\nnotes:\n  |\n  keep this",
        "a block scalar header on its own line",
        True,
    ),
    (
        "keep-chomped header on its own line in the last server",
        "mcp_servers:\n  other:\n    notes:\n      |+\n      text\n\n\n",
        "a block scalar header on its own line",
        True,
    ),
    (
        "tab after a mapping colon",
        "mcp_servers:\n  foo:\tbar\n",
        "a tab outside a quoted value or comment",
        False,
    ),
    (
        "tab inside a flow value",
        "mcp_servers:\n  other: {command: node,\targs: [a]}\n",
        "a tab outside a quoted value or comment",
        False,
    ),
    (
        "tab before a comment",
        "model: gpt-4o\t# default\n",
        "a tab outside a quoted value or comment",
        False,
    ),
]


@pytest.mark.parametrize(
    ("label", "original", "reason", "valid_yaml"),
    _REFUSALS,
    ids=[case[0] for case in _REFUSALS],
)
def test_unsafe_files_are_refused_untouched_with_the_snippet(
    tmp_path: Path, capsys, label: str, original: str, reason: str, valid_yaml: bool
) -> None:
    """A file the writer cannot edit safely is left byte-identical, exit 1.

    The receipt names the reason and prints the snippet to paste. No backup
    is written because nothing was modified. Mutation: fall back to the old
    parse-and-dump writer, or write anyway. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    config = _seed(home, original)
    if valid_yaml:
        # Guards the guard: these files are valid YAML that Hermes can load,
        # so the refusal is about safety, not about a broken file.
        yaml.safe_load(original)
    else:
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(original)

    code, out, err = _install(home, vault, capsys)
    assert code == 1, (label, out)
    assert config.read_bytes() == original.encode("utf-8")
    assert _backups(config) == []
    assert _error_records(err) == [INSTALL_REFUSED]
    assert "action: refused" in out
    assert reason in out, out
    snippet = out.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    assert yaml.safe_load(snippet) == {"mcp_servers": {"alice": _expected_alice(vault)}}
    assert "was not changed" in out


def test_non_utf8_config_is_refused(tmp_path: Path, capsys) -> None:
    """Bytes that are not UTF-8 are refused, byte-identical.

    Mutation: decode with errors="replace". This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    latin1 = b"model: caf\xe9\n"
    config = _seed(home, latin1)
    code, out, _err = _install(home, vault, capsys)
    assert code == 1
    assert "not UTF-8" in out
    assert config.read_bytes() == latin1


def test_symlinked_config_is_written_through_and_the_link_kept(tmp_path: Path, capsys) -> None:
    """A dotfiles symlink stays a symlink; the target is edited and backed up.

    Review round 3 finding 15, 2026-09-23: Hermes refused symlinks and the
    JSON hosts replaced them with regular files. Since round 4 the backup
    goes to the data dir, never next to the target, which may be in a
    dotfiles repo. Mutation: write to the link path instead of its target,
    or back up next to the target. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    target = tmp_path / "dotfiles" / "hermes.yaml"
    target.parent.mkdir()
    target.write_text("model: gpt-4o\n", encoding="utf-8")
    config = _config_path(home)
    config.parent.mkdir(parents=True)
    config.symlink_to(target)

    code, out, err = _install(home, vault, capsys)
    assert code == 0, (out, err)
    assert config.is_symlink()
    assert config.resolve() == target.resolve()
    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["mcp_servers"]["alice"] == _expected_alice(vault)
    assert f"target: {target.resolve()}" in out
    assert sorted(path.name for path in target.parent.iterdir()) == ["hermes.yaml"]
    backups = sorted(
        (vault / "backups" / "host-configs").glob(f"hermes-hermes.yaml{host_install.HERMES_BACKUP_MARKER}*")
    )
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "model: gpt-4o\n"
    assert sorted(path.name for path in config.parent.iterdir()) == ["config.yaml"]


def test_dangling_symlinked_config_is_refused(tmp_path: Path, capsys) -> None:
    """A link to nothing refuses the host; the link stays as it was.

    Mutation: create the target, or replace the link. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    config = _config_path(home)
    config.parent.mkdir(parents=True)
    missing = tmp_path / "dotfiles" / "gone.yaml"
    config.symlink_to(missing)

    code, out, err = _install(home, vault, capsys)
    assert code == 1
    assert _error_records(err) == [INSTALL_REFUSED]
    assert "symbolic link whose target is missing or loops" in out
    assert config.is_symlink() and not missing.exists()


def test_backup_that_fails_partway_leaves_no_backup_and_no_edit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A backup write that dies halfway leaves nothing under a backup name.

    The disk "fills" after half the bytes. No file may appear under the
    backup name, no temp file may be left behind, config.yaml must keep its
    bytes, and install must exit 1 with install_failed. Review finding 4 of
    2026-09-22: the first backup writer opened the final name directly, so
    a failed write left a truncated 0600 file that looked like a backup.
    Mutation: write the backup straight to its final name. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    original = CORRUPTION_FIXTURE.encode("utf-8")
    config = _seed(home, original)
    real_fdopen = os.fdopen

    class _HalfWriter:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self) -> _HalfWriter:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            self._handle.close()
            return False

        def write(self, data: bytes) -> int:
            self._handle.write(data[: len(data) // 2])
            self._handle.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def flush(self) -> None:
            self._handle.flush()

        def fileno(self) -> int:
            return self._handle.fileno()

    def half_writing_fdopen(descriptor: int, *args: object, **kwargs: object) -> _HalfWriter:
        return _HalfWriter(real_fdopen(descriptor, *args, **kwargs))

    monkeypatch.setattr(host_install.os, "fdopen", half_writing_fdopen)
    code, _out, err = _install(home, vault, capsys)
    monkeypatch.undo()

    assert code == 1
    assert _error_records(err) == [INSTALL_FAILED]
    assert config.read_bytes() == original
    assert sorted(path.name for path in config.parent.iterdir()) == ["config.yaml"]


def test_dry_run_writes_nothing_and_prints_only_the_alice_block(
    tmp_path: Path, capsys
) -> None:
    """--dry-run plans the edit without a write or a backup.

    The snippet is the alice block, not the whole file, so a dry run does not
    echo keys or secrets from config.yaml. Mutation: write during dry-run,
    or print the full file. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    secret_line = "  OPENAI_API_KEY: sk-not-real-but-private\n"
    original = CORRUPTION_FIXTURE + "env:\n" + secret_line
    config = _seed(home, original)

    code, out, err = _install(home, vault, capsys, "--dry-run")
    assert code == 0, err
    assert config.read_bytes() == original.encode("utf-8")
    assert _backups(config) == []
    assert "action: dry-run" in out
    assert "sk-not-real-but-private" not in out
    snippet = out.split("snippet:\n", 1)[1]
    # ALICE_MEMORY_DATA_DIR is install's own value, the --data-dir shown in
    # args, so it is printed; values from the user's file are not (round 4 P7).
    assert yaml.safe_load(snippet) == {"mcp_servers": {"alice": _expected_alice(vault)}}


def test_awkward_data_dir_round_trips_through_pyyaml(
    tmp_path: Path, capsys
) -> None:
    """A vault path with quotes, a backslash, '#', ': ' and non-ASCII reads back exactly.

    Every value in the alice block is double-quoted and escaped. Mutation:
    stop escaping the backslash or the double quote. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / f'we"ird {BS} dir # x: y {E_ACUTE}'
    config = _seed(home, "model: gpt-4o\n")

    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err
    loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert loaded["mcp_servers"]["alice"] == _expected_alice(vault)
    assert loaded["mcp_servers"]["alice"]["args"][-1] == str(vault.resolve())


def test_a_hermes_refusal_still_writes_the_other_hosts(
    tmp_path: Path, capsys
) -> None:
    """Hermes first and refused, Cursor second: Cursor is written, both receipts print.

    Mutation: stop at the first refusal. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    original = "mcp_servers:\n  other:\n    args: [\n      a.js\n    ]\n"
    config = _seed(home, original)

    code, out, err = _install(home, vault, capsys, "--host", "cursor")
    assert code == 1
    assert _error_records(err) == [INSTALL_REFUSED]
    assert config.read_bytes() == original.encode("utf-8")
    cursor = json.loads(host_file_map(home.resolve())["cursor"]["mcp"].read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["alice"]["command"] == "uvx"
    assert "host: hermes\n" in out and "action: refused" in out
    assert "host: cursor\n" in out and "action: written" in out


def _install_without_flag(home: Path, capsys, *extra: str) -> tuple[int, str, str]:
    code = onramp_main(["install", "--home", str(home), "--host", "hermes", *extra])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


OLD_ALICE_WITH_USER_KEYS = (
    "mcp_servers:\n"
    "  alice:\n"
    "    command: uvx\n"
    "    timeout: 30\n"
    "    args:\n"
    "      - alice-memory\n"
    "      - mcp\n"
    '      - "--data-dir"\n'
    "      - /old/vault\n"
    "    env:\n"
    "      ALICE_MEMORY_DATA_DIR: /old/vault\n"
    '      ALICE_MCP_FULL_TOOLS: "1"\n'
)


def test_old_alice_with_keys_install_never_wrote_is_refused_and_they_are_named(
    tmp_path: Path, capsys
) -> None:
    """An alice entry with user keys is not replaced; the keys are printed.

    Review decision Q1, 2026-09-22: replacing it would drop
    ALICE_MCP_FULL_TOOLS and timeout. The file keeps its bytes, and the
    snippet keeps the entry's own data dir so pasting it does not re-point
    the vault. Mutation: stop checking keys, or print the snippet on
    ~/.alice. This test fails.
    """

    home = tmp_path / "home"
    config = _seed(home, OLD_ALICE_WITH_USER_KEYS)

    code, out, err = _install_without_flag(home, capsys)
    assert code == 1
    assert _error_records(err) == [INSTALL_REFUSED]
    assert config.read_text(encoding="utf-8") == OLD_ALICE_WITH_USER_KEYS
    assert "mcp_servers.alice has keys install did not write" in out
    assert "extra_keys: timeout, env.ALICE_MCP_FULL_TOOLS" in out
    snippet = out.split("snippet:\n", 1)[1].split("\nextra_keys:", 1)[0]
    assert yaml.safe_load(snippet)["mcp_servers"]["alice"]["args"][-1] == "/old/vault"


@pytest.mark.parametrize(
    "old_alice",
    [
        (
            "  alice:\n"
            "    command: /opt/homebrew/bin/uvx\n"
            "    args: [alice-memory==0.16.0, mcp, --data-dir, /old/vault]\n"
        ),
        (
            "  alice:\n"
            "    command: uvx\n"
            "    args:\n"
            "      - alice-memory\n"
            "      - mcp\n"
            '      - "--data-dir"\n'
            "      - /old/vault\n"
            "    env:\n"
            "      ALICE_MEMORY_DATA_DIR: /old/vault\n"
        ),
        "  alice: {command: uvx, args: [alice-memory, mcp, --data-dir, /old/vault]}\n",
    ],
    ids=["absolute-uvx-and-pin", "v0.16.0-block", "flow"],
)
def test_hermes_rerun_without_flag_keeps_the_old_data_dir(
    tmp_path: Path, capsys, old_alice: str
) -> None:
    """No --data-dir: the old entry's data dir stays; an install-shaped one keeps
    its command and args.

    Mutation: default a missing flag to ~/.alice, or rebuild command and
    args from install's payload. This test fails.
    """

    home = tmp_path / "home"
    # A real dir, since the backup goes into <data dir>/backups (round 4).
    old_vault = str((tmp_path / "old-vault").resolve())
    original = ("model: gpt-4o\nmcp_servers:\n" + old_alice).replace("/old/vault", old_vault)
    before = yaml.safe_load(original)["mcp_servers"]["alice"]
    config = _seed(home, original)

    code, out, err = _install_without_flag(home, capsys)
    assert code == 0, (out, err)
    alice = yaml.safe_load(config.read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert alice["command"] == before["command"]
    assert alice["env"] == {"ALICE_MEMORY_DATA_DIR": old_vault}
    args = alice["args"]
    assert args[args.index("--data-dir") + 1] == old_vault
    assert args[: len(before["args"])][:2] == before["args"][:2]
    assert "data_dir:" not in out


def test_hermes_rerun_with_flag_moves_only_the_data_dir(tmp_path: Path, capsys) -> None:
    """--data-dir on an old install-shaped entry: pin and uvx path stay, dir moves.

    Mutation: drop the receipt line, or replace the pinned args. This test
    fails.
    """

    home = tmp_path / "home"
    new_vault = (tmp_path / "new").resolve()
    original = (
        "mcp_servers:\n  alice:\n    command: /opt/homebrew/bin/uvx\n"
        "    args: [alice-memory==0.16.0, mcp, --data-dir, /old/vault]\n"
    )
    config = _seed(home, original)

    code, out, err = _install(home, new_vault, capsys)
    assert code == 0, (out, err)
    alice = yaml.safe_load(config.read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert alice == {
        "command": "/opt/homebrew/bin/uvx",
        "args": ["alice-memory==0.16.0", "mcp", "--data-dir", str(new_vault)],
        "env": {"ALICE_MEMORY_DATA_DIR": str(new_vault)},
    }
    assert f"data_dir: /old/vault -> {new_vault}" in out


@pytest.mark.parametrize(
    ("label", "old_alice", "reason"),
    [
        (
            "block scalar inside alice",
            "  alice:\n    command: |\n      uvx\n",
            "the existing mcp_servers.alice uses YAML this writer does not read",
        ),
        (
            "list of mappings inside alice",
            "  alice:\n    args:\n      - name: x\n",
            "the existing mcp_servers.alice uses YAML this writer does not read",
        ),
        (
            "args behind an alias to a list",
            "  alice:\n    command: uvx\n    args: *listed\n",
            "the command or args of the existing mcp_servers.alice cannot be read",
        ),
    ],
)
def test_hermes_old_entry_it_cannot_read_is_refused(
    tmp_path: Path, capsys, label: str, old_alice: str, reason: str
) -> None:
    """An old alice entry the reader cannot decode is refused, not replaced.

    Mutation: treat an unreadable entry as empty. This test fails.
    """

    home = tmp_path / "home"
    original = "vaults: &vault /aliased/vault\nlisted: &listed [a, b]\nmcp_servers:\n" + old_alice
    yaml.safe_load(original)
    config = _seed(home, original)

    code, out, err = _install_without_flag(home, capsys)
    assert code == 1, (label, out)
    assert _error_records(err) == [INSTALL_REFUSED]
    assert config.read_text(encoding="utf-8") == original
    assert reason in out, out


def test_bare_data_dir_flag_at_the_end_of_args_is_left_alone(tmp_path: Path, capsys) -> None:
    """Old args ending in a bare --data-dir: the server would not start, so install keeps them.

    Found by the seeded fuzz pass on 2026-09-22, when the args became
    [..., "--data-dir", "--data-dir", dir]. Since review round 4 (S1) the
    server's parser reads the args: without the flag the entry stays byte
    for byte; with it, install refuses rather than guess. Mutation: rewrite
    args the parser rejects. This test fails.
    """

    home = tmp_path / "home"
    vault = (tmp_path / "vault").resolve()
    original = "mcp_servers:\n  alice:\n    command: uvx\n    args: [alice-memory, mcp, --data-dir]\n"
    config = _seed(home, original)
    code, out, err = _install_without_flag(home, capsys)
    assert code == 0, (out, err)
    assert config.read_text(encoding="utf-8") == original
    assert "alice-memory mcp would not start" in out
    code, out, err = _install(home, vault, capsys)
    assert code == 1
    assert config.read_text(encoding="utf-8") == original


def test_extra_keys_refusal_snippet_keeps_the_entrys_own_launcher(
    tmp_path: Path, capsys
) -> None:
    """Review round 3 finding 3: the snippet dropped an absolute uvx and a pin.

    For an install-shaped entry the snippet is the entry's own command and
    args with the data dir applied. Mutation: build the snippet from
    install's default payload. This test fails.
    """

    home = tmp_path / "home"
    uvx = tmp_path / "tools" / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.write_text("", encoding="utf-8")
    uvx.chmod(0o755)
    original = (
        "mcp_servers:\n  alice:\n"
        f"    command: {uvx}\n"
        "    args: [alice-memory==0.16.0, mcp, --data-dir, /old/vault]\n"
        "    timeout: 30\n"
    )
    config = _seed(home, original)
    code, out, err = _install_without_flag(home, capsys)
    assert code == 1
    assert config.read_text(encoding="utf-8") == original
    snippet = out.split("snippet:\n", 1)[1].split("\nextra_keys:", 1)[0]
    assert yaml.safe_load(snippet)["mcp_servers"]["alice"] == {
        "command": str(uvx),
        "args": ["alice-memory==0.16.0", "mcp", "--data-dir", "/old/vault"],
        "env": {"ALICE_MEMORY_DATA_DIR": "/old/vault"},
    }
    assert "carry over your timeout" in out


@pytest.mark.parametrize(
    ("label", "old_alice"),
    [
        ("python -m entry", "  alice:\n    command: python\n    args: [-m, alicebot_api.mcp_server]\n"),
        ("npx server named alice", "  alice:\n    command: npx\n    args: [other-server, mcp]\n"),
        (
            "uvx look-alike",
            "  alice:\n    command: uvx\n    args: [mcp-proxy, mcp, --name, alice-memory]\n",
        ),
    ],
)
def test_hermes_entry_install_did_not_write_is_refused(
    tmp_path: Path, capsys, label: str, old_alice: str
) -> None:
    """Review round 3 finding 4: the same shape check as the JSON hosts.

    Mutation: replace any alice entry whose keys look like install's. This
    test fails.
    """

    home = tmp_path / "home"
    original = "mcp_servers:\n" + old_alice
    config = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1, (label, out)
    assert _error_records(err) == [INSTALL_REFUSED]
    assert config.read_text(encoding="utf-8") == original
    assert "mcp_servers.alice exists and install did not write it" in out


def test_anchored_entry_snippet_uses_the_entrys_visible_data_dir(
    tmp_path: Path, capsys
) -> None:
    """Review round 3 finding 6: the snippet pointed at ~/.alice.

    The strict reader refuses the anchor, a lenient read still sees the
    data dir. Mutation: fall back to ~/.alice. This test fails.
    """

    home = tmp_path / "home"
    original = (
        "mcp_servers:\n  alice:\n    command: &c uvx\n"
        "    args: [alice-memory, mcp, --data-dir, /anchored/vault]\n"
        "other: *c\n"
    )
    _seed(home, original)
    code, out, err = _install_without_flag(home, capsys)
    assert code == 1
    snippet = out.split("snippet:\n", 1)[1].split("\nnext:", 1)[0]
    assert yaml.safe_load(snippet)["mcp_servers"]["alice"]["args"][-1] == "/anchored/vault"


@pytest.mark.parametrize(
    "original",
    [
        "mcp_servers:\n  alice:\n    command: |\n      uvx\n",
        "note:\ttabbed\nmcp_servers:\n  alice:\n    command: uvx\n",
    ],
    ids=["unreadable-entry", "file-refused-before-alice"],
)
def test_snippet_uses_a_placeholder_when_the_data_dir_is_not_visible(
    tmp_path: Path, capsys, original: str
) -> None:
    """Never a snippet on ~/.alice when an alice entry exists but cannot be read.

    Mutation: fall back to ~/.alice. This test fails.
    """

    home = tmp_path / "home"
    _seed(home, original)
    code, out, err = _install_without_flag(home, capsys)
    assert code == 1
    assert "<the data dir your existing alice entry uses>" in out
    assert "keep: replace the placeholder" in out
    assert str(home.resolve() / ".alice") not in out


def test_nested_list_item_is_refused_not_flattened(tmp_path: Path, capsys) -> None:
    """Review round 3 finding 7: ``- - x`` is a list inside a list, not "x".

    Mutation: read a multi-dash item as its text. This test fails.
    """

    home = tmp_path / "home"
    original = (
        "mcp_servers:\n  alice:\n    command: uvx\n    args:\n"
        "      - - alice-memory\n      - mcp\n"
    )
    assert yaml.safe_load(original)["mcp_servers"]["alice"]["args"][0] == ["alice-memory"]
    config = _seed(home, original)
    code, out, err = _install(home, tmp_path / "vault", capsys)
    assert code == 1
    assert config.read_text(encoding="utf-8") == original
    assert "uses YAML this writer does not read" in out


def test_hermes_env_only_entry_runs_on_the_default_dir(tmp_path: Path, capsys) -> None:
    """Review round 3 finding 1 on Hermes: env is not where the server looks.

    ``args: [alice-memory, mcp]`` with env on /custom runs on ~/.alice. The
    env install writes is set to that dir, with a note, and --data-dir is
    not invented. Mutation: take the data dir from the env. This test fails.
    """

    home = tmp_path / "home"
    original = (
        "mcp_servers:\n  alice:\n    command: uvx\n    args: [alice-memory, mcp]\n"
        "    env:\n      ALICE_MEMORY_DATA_DIR: /custom\n"
    )
    config = _seed(home, original)
    code, out, err = _install_without_flag(home, capsys)
    assert code == 0, (out, err)
    default = str(home.resolve() / ".alice")
    alice = yaml.safe_load(config.read_text(encoding="utf-8"))["mcp_servers"]["alice"]
    assert alice == {
        "command": "uvx",
        "args": ["alice-memory", "mcp"],
        "env": {"ALICE_MEMORY_DATA_DIR": default},
    }
    assert "env ALICE_MEMORY_DATA_DIR was <hidden>" in out and "/custom" not in out


def test_install_refused_error_contract_is_registered() -> None:
    """The refusal exit uses a static, documented error code.

    Mutation: drop install_refused from _ERROR_CONTRACTS. This test fails.
    """

    assert "could not edit it safely" in _ERROR_CONTRACTS["install_refused"]


# --- generated configs -------------------------------------------------------


def test_generated_configs_keep_their_meaning(tmp_path: Path, capsys) -> None:
    """Randomised configs built from supported YAML all end as original plus alice.

    Seeded, so a failure reproduces. Each case must also keep every original
    comment line. Mutation: break any placement rule in the writer (insert
    before a keep-chomped block's blank lines, ignore block scalar content,
    miss compact lists, treat a comment as the end of mcp_servers). This test
    fails.
    """

    rng = random.Random(20260922)
    kinds: dict[str, int] = {}
    for case in range(150):
        original, mcp_kind = fuzz.generate_config(rng)
        kinds[mcp_kind] = kinds.get(mcp_kind, 0) + 1
        before = yaml.safe_load(original)
        home = tmp_path / f"home{case}"
        vault = tmp_path / f"vault{case}"
        config = _seed(home, original)
        code, out, err = _install(home, vault, capsys)
        assert code == 0, (case, original, out, err)
        after_text = config.read_bytes().decode("utf-8")
        assert yaml.safe_load(after_text) == _with_alice(before, vault), (case, original, after_text)
        after_lines = after_text.splitlines()
        cursor = 0
        for line in original.splitlines():
            if not line.lstrip().startswith("#"):
                continue
            while cursor < len(after_lines) and after_lines[cursor] != line:
                cursor += 1
            assert cursor < len(after_lines), (case, "comment lost or moved", line)
            cursor += 1
    # Guards the guard: the generator really produced every mcp_servers shape.
    assert set(kinds) == {"absent", "children", "empty", "flow-empty", "null"}, kinds
    assert min(kinds.values()) >= 5, kinds


def test_seeded_fuzz_run_finds_no_meaning_change() -> None:
    """A short seeded pass of scripts/fuzz_hermes_config_writer.py.

    Every generated config is written with its meaning kept; every valid
    mutant is written with its meaning kept or refused. The anchor mutation
    puts an ``&anchor`` inside an old alice entry, often in a flow value,
    and an alias to it outside; replacing alice would leave that alias
    undefined, which is review finding 1 of 2026-09-22.

    Since round 3 (2026-09-23) the entry reader refuses an anchored entry on
    its own, so dropping the scanner's flow-anchor report no longer fails
    this pass; the two flow-anchor refusal cases in
    test_unsafe_files_are_refused_untouched_with_the_snippet still do.
    Mutation that this pass does catch: stop tracking block scalar text in
    the scanner.
    """

    counts = fuzz.run(range(5), configs_per_seed=100, mutants_per_config=6)
    assert counts.generated_written == 500
    anchored = counts.by_mutation["anchor-inside-alice"]
    aliased = counts.by_mutation["alias-inside-alice"]
    # Guards the guard: both anchor mutations really ran and really reached
    # the writer, so a pass is not a pass over nothing.
    assert anchored.get("refused", 0) >= 20 and "written" not in anchored, anchored
    assert aliased.get("written", 0) >= 20, aliased


# --- the real Hermes binary ----------------------------------------------------


def _hermes(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real binary with HERMES_HOME in the test's tmp dir.

    HOME is moved too, except on macOS, where a temp HOME can make the login
    Keychain prompt the user. HERMES_HOME alone decides where Hermes reads
    config.yaml.
    """

    hermes = shutil.which("hermes")
    assert hermes is not None
    env = dict(os.environ, HERMES_HOME=str(home / ".hermes"))
    if sys.platform != "darwin":
        env["HOME"] = str(home)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [hermes, *args],
        cwd=home,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.skipif(
    os.environ.get(REAL_HOSTS_ENV) != "1",
    reason=f"set {REAL_HOSTS_ENV}=1 to run the real hermes binary",
)
@pytest.mark.skipif(shutil.which("hermes") is None, reason="hermes binary not on PATH")
def test_real_hermes_loads_the_written_config(tmp_path: Path, capsys) -> None:
    """The real ``hermes`` reads the same values after install, and lists alice.

    Opt-in: runs only with ALICE_TEST_REAL_HOSTS=1 and ``hermes`` on PATH.
    HERMES_HOME points at this test's tmp dir, so the user's real ~/.hermes
    is never read or written (see _hermes). Before install, ``hermes config
    get`` must already print the fixture's values, which proves hermes read
    the temp file at all. The version is printed and put in every failure
    message. Mutation: the old parse-and-dump writer turns ``model`` into
    ``gpt-4o  # default model`` and ``compact`` into ``yes``. This test fails.
    """

    home = tmp_path / "home"
    vault = tmp_path / "vault"
    _seed(home, CORRUPTION_FIXTURE)
    version_proc = _hermes(home, "--version")
    version = (version_proc.stdout + version_proc.stderr).strip()
    print(f"hermes version: {version}")

    reads = {"model": "gpt-4o", "display.compact": "true", "display.streaming": "false"}
    for key, expected in reads.items():
        proc = _hermes(home, "config", "get", key)
        assert proc.returncode == 0, (version, proc.stderr)
        assert proc.stdout.strip() == expected, (version, key, proc.stdout)
    listed = _hermes(home, "mcp", "list")
    assert "other" in listed.stdout and "alice" not in listed.stdout, (version, listed.stdout)

    code, _out, err = _install(home, vault, capsys)
    assert code == 0, err

    for key, expected in reads.items():
        proc = _hermes(home, "config", "get", key)
        assert proc.returncode == 0, (version, proc.stderr)
        assert proc.stdout.strip() == expected, (version, key, proc.stdout)
    listed = _hermes(home, "mcp", "list")
    assert listed.returncode == 0, (version, listed.stderr)
    rows = {line.split()[0] for line in listed.stdout.splitlines() if line.strip()}
    assert {"other", "alice"} <= rows, (version, listed.stdout)
