"""The unit-test home guard refuses host folders under the real home.

The real home is never touched here. Each test points a guard at a fake home
inside tmp_path, so a broken guard reads a temporary file and nothing else.
Each test names the edit that makes it fail.
"""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.support.host_home_guard import (
    WATCHED_NAMES,
    HostHomeAccessError,
    HostHomeGuard,
)
from tests.unit import conftest as unit_conftest

FILES = {
    ".hermes/config.yaml": "model: x\n",
    ".claude.json": "{}\n",
    ".claude/settings.json": "{}\n",
    ".codex/config.toml": 'model = "x"\n',
    ".cursor/mcp.json": "{}\n",
    ".config/opencode/opencode.json": "{}\n",
    ".openclaw/openclaw.json": "{}\n",
    ".alice/vault.txt": "x\n",
    "projects/note.txt": "x\n",
}


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-real-home"
    for relative, text in FILES.items():
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return home


@pytest.fixture
def guard(fake_home: Path) -> HostHomeGuard:
    return HostHomeGuard([fake_home])


def _read(path: Path) -> object:
    return open(path, encoding="utf-8").read()


def _write(path: Path) -> object:
    return open(path.parent / "new.txt", "w", encoding="utf-8").close()


ACTIONS: dict[str, Callable[[Path], object]] = {
    "open-read": lambda home: _read(home / ".hermes" / "config.yaml"),
    "open-write": lambda home: _write(home / ".hermes" / "config.yaml"),
    "read_text": lambda home: (home / ".codex" / "config.toml").read_text(),
    "listdir": lambda home: os.listdir(home / ".openclaw"),
    "scandir": lambda home: list(os.scandir(home / ".cursor")),
    "iterdir": lambda home: list((home / ".claude").iterdir()),
    "walk": lambda home: list(os.walk(home / ".alice")),
    "glob": lambda home: glob.glob(str(home / ".config" / "opencode" / "*")),
    "Path.glob": lambda home: list((home / ".hermes").glob("*")),
    "os.stat": lambda home: os.stat(home / ".hermes" / "config.yaml"),
    "os.lstat": lambda home: os.lstat(home / ".claude.json"),
    "os.access": lambda home: os.access(home / ".codex", os.R_OK),
    "os.path.exists": lambda home: os.path.exists(home / ".openclaw"),
    "Path.exists": lambda home: (home / ".cursor" / "mcp.json").exists(),
    "Path.is_dir": lambda home: (home / ".alice").is_dir(),
    "Path.is_file": lambda home: (home / ".claude.json").is_file(),
    "mkdir": lambda home: os.mkdir(home / ".hermes" / "sub"),
    "makedirs": lambda home: os.makedirs(home / ".config" / "opencode" / "a" / "b"),
    "rename": lambda home: os.rename(home / "projects" / "note.txt", home / ".alice" / "n.txt"),
    "copy": lambda home: shutil.copy(home / ".hermes" / "config.yaml", home / "projects"),
    "unlink": lambda home: os.remove(home / ".claude" / "settings.json"),
    "resolve": lambda home: (home / ".codex" / "config.toml").resolve(),
    "bytes-path": lambda home: os.stat(os.fsencode(home / ".openclaw" / "openclaw.json")),
    "str-path": lambda home: open(str(home / ".alice" / "vault.txt")).close(),
}


@pytest.mark.parametrize("name", sorted(ACTIONS))
def test_guard_refuses_every_way_to_reach_a_watched_folder(name: str, guard: HostHomeGuard, fake_home: Path) -> None:
    """Open, read, write, list, stat, create and rename under a watched folder are refused.

    Some callers (``os.path.exists``, ``Path.resolve``) swallow the error, so
    the record is the contract. Mutation: drop that event from the audit
    hook, or stop wrapping os.stat, os.lstat or os.access. The matching case
    reaches the file, nothing is recorded, and this test fails.
    """

    with guard.armed():
        with contextlib.suppress(OSError):
            ACTIONS[name](fake_home)
    assert guard.violations, name
    assert not (fake_home / ".hermes" / "new.txt").exists()
    assert not (fake_home / ".hermes" / "sub").exists()
    assert not (fake_home / ".config" / "opencode" / "a").exists()
    assert not (fake_home / ".alice" / "n.txt").exists()
    assert (fake_home / "projects" / "note.txt").is_file()
    assert (fake_home / ".claude" / "settings.json").is_file()


@pytest.mark.parametrize(
    "name",
    ["open-read", "listdir", "os.stat", "Path.exists", "Path.is_dir", "mkdir"],
)
def test_the_error_reaches_a_caller_that_does_not_catch_it(name: str, guard: HostHomeGuard, fake_home: Path) -> None:
    """A caller that lets OSError through sees a PermissionError with the path.

    Mutation: return None instead of raising. These cases pass the access
    through or return a value, and this test fails.
    """

    with guard.armed():
        with pytest.raises(HostHomeAccessError) as caught:
            ACTIONS[name](fake_home)
    assert isinstance(caught.value, PermissionError)
    assert str(fake_home) in str(caught.value)


def test_the_guard_is_what_stops_the_read(guard: HostHomeGuard, fake_home: Path) -> None:
    """The same read works until the guard is armed, and works again after.

    Mutation: leave the audit hook out of the process (never call
    sys.addaudithook). The armed read succeeds and this test fails.
    """

    target = fake_home / ".hermes" / "config.yaml"
    assert target.read_text() == "model: x\n"
    assert not guard.is_armed
    with guard.armed():
        assert guard.is_armed
        with pytest.raises(HostHomeAccessError):
            target.read_text()
    assert not guard.is_armed
    assert target.read_text() == "model: x\n"
    assert guard.violations == (f"open {target}",)


def test_a_caught_error_still_leaves_a_record(guard: HostHomeGuard, fake_home: Path) -> None:
    """Code that swallows OSError cannot hide the access from the fixture.

    Mutation: refuse the access without recording it. The record is empty
    and this test fails.
    """

    with guard.armed():
        try:
            (fake_home / ".claude" / "settings.json").read_text()
        except OSError:
            pass
        try:
            os.listdir(fake_home / ".codex")
        except OSError:
            pass
    assert [line.split(" ", 1)[0] for line in guard.violations] == ["open", "os.listdir"]


@pytest.mark.parametrize("name", WATCHED_NAMES)
def test_every_watched_name_is_watched_in_both_spellings(name: str, tmp_path: Path) -> None:
    """Each of the eight names is watched, and so is a file or folder below it.

    Mutation: drop a name from WATCHED_NAMES. That name's case fails.
    """

    real = tmp_path / "real"
    link = tmp_path / "link"
    real.mkdir()
    link.symlink_to(real, target_is_directory=True)
    guard = HostHomeGuard([link])
    assert str(real) in guard.homes and str(link) in guard.homes
    for home in (real, link):
        base = home.joinpath(*name.split("/"))
        assert guard.blocks(base) == str(base)
        assert guard.blocks(base / "inner" / "file") == str(base / "inner" / "file")


def test_a_claude_json_sibling_is_watched_and_a_lookalike_name_is_not(guard: HostHomeGuard, fake_home: Path) -> None:
    """.claude.json.lock and .claude.json.backup are watched. .claudex is not.

    Mutation: match by plain prefix, or drop the sibling rule. A lookalike
    is refused, or the lock file is let through, and this test fails.
    """

    for name in (".claude.json.lock", ".claude.json.backup", ".claude.json"):
        assert guard.blocks(fake_home / name) is not None, name
    for name in (".claudex", ".hermes-old", ".codexes", ".config", ".config/other", "projects"):
        assert guard.blocks(fake_home / name) is None, name
        assert guard.blocks(fake_home / name / "inner") is None, name
    assert guard.blocks(fake_home) is None
    assert guard.blocks(fake_home.parent / "elsewhere" / ".hermes") is None


def test_unwatched_paths_under_the_home_still_work(guard: HostHomeGuard, fake_home: Path) -> None:
    """An armed guard leaves the rest of the home alone.

    Mutation: watch the whole home. This test fails.
    """

    with guard.armed():
        assert (fake_home / "projects" / "note.txt").read_text() == "x\n"
        assert os.listdir(fake_home / "projects") == ["note.txt"]
        assert (fake_home / "projects").is_dir()
        assert not (fake_home / "missing").exists()
    assert guard.violations == ()


def test_relative_dotdot_and_bytes_paths_are_resolved_before_matching(
    guard: HostHomeGuard, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path, a ``..`` detour and a bytes path reach the same rule.

    Mutation: match on the raw text without making it absolute and
    normalized. These three escape and this test fails.
    """

    monkeypatch.chdir(fake_home)
    with guard.armed():
        with pytest.raises(HostHomeAccessError):
            open(".hermes/config.yaml").close()
        with pytest.raises(HostHomeAccessError):
            os.listdir("./projects/../.codex")
        with pytest.raises(HostHomeAccessError):
            os.stat(os.fsencode(fake_home / "projects" / ".." / ".openclaw"))
    assert len(guard.violations) == 3


def test_the_temp_folder_is_skipped(tmp_path: Path) -> None:
    """A folder passed to allow() is skipped. Its siblings are not.

    Mutation: ignore allow(). The scratch case is refused and this test fails.
    """

    home = tmp_path / "home"
    (home / ".alice" / "scratch").mkdir(parents=True)
    (home / ".alice" / "scratch" / "f.txt").write_text("x")
    (home / ".alice" / "other").mkdir()
    guard = HostHomeGuard([home])
    guard.allow(home / ".alice" / "scratch")
    with guard.armed():
        assert (home / ".alice" / "scratch" / "f.txt").read_text() == "x"
        with pytest.raises(HostHomeAccessError):
            os.listdir(home / ".alice" / "other")
    assert len(guard.violations) == 1


def test_a_path_directory_inside_a_watched_folder_may_be_probed_but_not_opened(
    fake_home: Path,
) -> None:
    """shutil.which can stat a file in such a PATH directory. Nothing else can.

    Mutation: let every event through for a PATH directory, or drop the
    allowance. The open or list case passes, or the stat case is refused,
    and this test fails.
    """

    bin_dir = fake_home / ".claude" / "local"
    bin_dir.mkdir(parents=True)
    (bin_dir / "claude").write_text("x")
    (bin_dir / "claude").chmod(0o755)
    guard = HostHomeGuard([fake_home], path_env=os.pathsep.join(["/usr/bin", str(bin_dir)]))
    with guard.armed():
        assert shutil.which("claude", path=str(bin_dir)) == str(bin_dir / "claude")
        assert os.path.exists(bin_dir / "claude")
        with pytest.raises(HostHomeAccessError):
            (bin_dir / "claude").read_text()
        with pytest.raises(HostHomeAccessError):
            os.listdir(bin_dir)
        with pytest.raises(HostHomeAccessError):
            os.stat(fake_home / ".claude" / "settings.json")
        with pytest.raises(HostHomeAccessError):
            os.stat(bin_dir / "deeper" / "claude")
    assert [line.split(" ", 1)[0] for line in guard.violations] == [
        "open",
        "os.listdir",
        "os.stat",
        "os.stat",
    ]


LIFECYCLE_SCRIPT = """
import os, sys
from tests.support.host_home_guard import HostHomeGuard, _supports_sets

names = ("stat", "lstat", "access")
before = {name: getattr(os, name) for name in names}
sets_before = [set(members) for members in _supports_sets()]
guard = HostHomeGuard([sys.argv[1]])
with guard.armed():
    for name, original in before.items():
        assert getattr(os, name) is not original, name
        assert hasattr(getattr(os, name), "__wrapped__"), name
    assert os.stat in os.supports_follow_symlinks
    assert os.stat in os.supports_fd
    assert os.stat in os.supports_dir_fd
    assert os.access in os.supports_effective_ids
    with guard.armed():
        pass
    assert getattr(os, "stat") is not before["stat"]
for name, original in before.items():
    assert getattr(os, name) is original, name
assert [set(members) for members in _supports_sets()] == sets_before
print("lifecycle ok")
"""


def test_os_functions_are_wrapped_only_while_armed(tmp_path: Path) -> None:
    """os.stat, os.lstat and os.access come back, and the support sets keep their members.

    A fresh interpreter runs this: every unit test already runs under the
    process guard, so the original functions are not visible in this one.
    Mutation: leave the wrappers in place after the block, or leave the
    originals out of the os.supports_* sets. The child process fails.
    """

    repo_root = Path(__file__).resolve().parents[2]
    done = subprocess.run(
        [sys.executable, "-c", LIFECYCLE_SCRIPT, str(tmp_path / "home")],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "lifecycle ok"


def test_two_guards_can_be_armed_together(tmp_path: Path) -> None:
    """The wrappers stay until the last guard leaves, and each guard keeps its own record.

    Mutation: remove the wrappers when the first guard leaves. The second
    guard no longer sees os.stat and this test fails.
    """

    one = HostHomeGuard([tmp_path / "one"])
    two = HostHomeGuard([tmp_path / "two"])
    with one.armed():
        with two.armed():
            with pytest.raises(HostHomeAccessError):
                os.stat(tmp_path / "two" / ".hermes")
        with pytest.raises(HostHomeAccessError):
            os.stat(tmp_path / "one" / ".hermes")
        assert hasattr(os.stat, "__wrapped__")
    assert one.violations == (f"os.stat {tmp_path / 'one' / '.hermes'}",)
    assert two.violations == (f"os.stat {tmp_path / 'two' / '.hermes'}",)


def test_every_unit_test_runs_under_the_process_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The guard is armed here without this test asking, and it ignores a later HOME patch.

    Only strings are checked: the real home is never opened, listed or
    stat'ed here. Mutation: delete ``autouse=True`` on the fixture in
    tests/unit/conftest.py. The first assertion fails.
    """

    guard = unit_conftest.PROCESS_GUARD
    assert guard.is_armed
    assert hasattr(os.stat, "__wrapped__")
    assert guard.homes
    real_home = guard.homes[0]
    monkeypatch.setenv("HOME", str(tmp_path / "patched"))
    for name in WATCHED_NAMES:
        watched = os.path.join(real_home, *name.split("/"))
        assert guard.blocks(watched) == watched, name
        assert guard.blocks(str(tmp_path / "patched" / name)) is None, name
    assert guard.blocks(os.path.join(real_home, "projects")) is None
    patched = tmp_path / "patched" / ".hermes" / "config.yaml"
    patched.parent.mkdir(parents=True)
    patched.write_text("model: x\n", encoding="utf-8")
    assert patched.read_text(encoding="utf-8") == "model: x\n"
    assert guard.violations == ()


def test_the_fixture_hands_back_the_process_guard(host_home_guard: HostHomeGuard) -> None:
    """The autouse fixture can be asked for by name and is the process guard.

    Mutation: build a second guard in the fixture. This test fails.
    """

    assert host_home_guard is unit_conftest.PROCESS_GUARD
