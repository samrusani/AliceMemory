"""An interrupted command must not leave a plaintext copy of the vault in the temp folder.

``alice-memory`` copies the vault into a private folder under the temp directory for
``export``, ``sources list``, the ``sources delete`` and ``sources prune`` previews and
``import-markdown --dry-run``. A normal exit or an exception removes that copy. A SIGKILL
or a power loss skips the cleanup, so the copy stayed until the system cleaned the temp
directory. Each copy now names its owner process in a marker file, and the next command
that makes a copy first removes the copies of processes that are gone.

The first block kills a real child process inside each of the two helpers, then runs a
command and looks at what is left. The rest pins what the sweep must leave alone and how
it fails. Every test names the edit that makes it fail.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api import onramp as onramp_module
from alicebot_api import snapshot_sweep
from alicebot_api.onramp import main as cli
from alicebot_api.snapshot_sweep import (
    EXPORT_SNAPSHOT_PREFIX,
    IMPORT_PREVIEW_PREFIX,
    OWNER_MARKER_NAME,
    parse_linux_stat,
    private_snapshot_directory,
    sweep_orphaned_snapshots,
)
from alicebot_api.vault_sleep import sleep_proposals_path
from tests.unit.test_importer_per_file_savepoint import USER_ID, _folder, _import, _read, _vault

PREFIX_BY_HELPER = {"export": EXPORT_SNAPSHOT_PREFIX, "preview": IMPORT_PREVIEW_PREFIX}
ROOT = Path(__file__).resolve().parents[2]

_CHILD = """
import argparse, os, signal, sys
from pathlib import Path
from uuid import UUID
from alicebot_api import onramp

helper, database, user, action = sys.argv[1:5]
if helper == "export":
    manager = onramp._prepared_export_connection(Path(database), UUID(user))
else:
    arguments = argparse.Namespace(dry_run=True, db=None, user_id=UUID(user), user_email="local@alice")
    manager = onramp._markdown_import_database(Path(database), arguments)
with manager:
    print("ready", flush=True)
    if action == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    sys.stdin.readline()
"""


@pytest.fixture
def scratch_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp directory of this test's own, used by this process and by its children."""
    directory = tmp_path / "temp"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    monkeypatch.setenv("TMPDIR", str(directory))
    return directory


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    database = _vault(tmp_path)
    _import(database, _folder(tmp_path, note="The kestrelplum lantern hangs in the loft."))
    return database


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def dead_pid() -> int:
    """The PID of a process that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


def _spawn(
    helper: str, database: Path, action: str, children: list[subprocess.Popen[str]]
) -> subprocess.Popen[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(entry for entry in sys.path if entry)
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, helper, str(database), USER_ID, action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    children.append(child)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready", "the child never entered the helper"
    return child


def _only_snapshot(helper: str) -> Path:
    found = [
        entry for entry in Path(tempfile.gettempdir()).iterdir() if entry.name.startswith(PREFIX_BY_HELPER[helper])
    ]
    assert len(found) == 1, found
    return found[0]


def _kill_inside(helper: str, database: Path, children: list[subprocess.Popen[str]]) -> Path:
    """Run a child that is SIGKILLed inside the helper. Return the folder it left."""
    child = _spawn(helper, database, "kill", children)
    child.wait(timeout=30)
    assert child.returncode == -9
    return _only_snapshot(helper)


def _release(child: subprocess.Popen[str]) -> None:
    assert child.stdin is not None
    child.stdin.write("\n")
    child.stdin.flush()
    child.wait(timeout=30)


def _commands(database: Path, tmp_path: Path) -> dict[str, list[str]]:
    source_id = str(_read(database, "SELECT id FROM sources")[0][0])
    base = ["--db", str(database), "--user-id", USER_ID]
    return {
        "export": ["export", *base, "--out", str(tmp_path / "out" / "backup.jsonl")],
        "sources-list": ["sources", "list", *base],
        "sources-delete-preview": ["sources", "delete", source_id, *base],
        "sources-prune-preview": ["sources", "prune", "--superseded", *base],
        "import-markdown-dry-run": ["import-markdown", "--from", str(tmp_path / "notes"), "--dry-run", *base],
    }


COMMAND_NAMES = ("export", "sources-list", "sources-delete-preview", "sources-prune-preview", "import-markdown-dry-run")


@pytest.mark.parametrize("command_name", COMMAND_NAMES)
@pytest.mark.parametrize("helper", ["export", "preview"])
def test_a_snapshot_left_by_a_killed_process_is_removed_by_the_next_command(
    helper: str,
    command_name: str,
    vault: Path,
    scratch_tmp: Path,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutations, each alone: delete the ``sweep_orphaned_snapshots()`` call in
    ``private_snapshot_directory``; delete the ``_write_owner_marker(directory)`` call there
    (a folder with no marker is never swept)."""
    orphan = _kill_inside(helper, vault, children)
    assert any(orphan.glob("snapshot-*.db")) or (orphan / "memory.db").exists(), "the orphan holds a vault copy"

    cli(_commands(vault, tmp_path)[command_name])
    capsys.readouterr()

    assert not orphan.exists(), f"{orphan.name} survived {command_name}"


@pytest.mark.parametrize("helper", ["export", "preview"])
def test_a_running_process_keeps_its_snapshot_and_removes_it_itself_on_a_normal_exit(
    helper: str,
    vault: Path,
    scratch_tmp: Path,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: make ``_owner_is_running`` return ``False``."""
    child = _spawn(helper, vault, "hold", children)
    snapshot = _only_snapshot(helper)
    held = sorted(entry.name for entry in snapshot.iterdir())

    cli(_commands(vault, tmp_path)["sources-list"])
    capsys.readouterr()

    assert snapshot.is_dir() and sorted(entry.name for entry in snapshot.iterdir()) == held
    _release(child)
    assert child.returncode == 0
    assert not snapshot.exists(), "a normal exit still removes its own snapshot"


@pytest.mark.parametrize("helper", ["export", "preview"])
def test_a_snapshot_whose_pid_now_belongs_to_a_process_that_started_at_another_time_is_removed(
    helper: str,
    vault: Path,
    scratch_tmp: Path,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live process holds the PID, but the marker names a different start time, which is what
    a reused PID looks like. Mutation: make ``_owner_is_running`` return ``True`` whenever the
    PID exists, without comparing start times."""
    child = _spawn(helper, vault, "hold", children)
    snapshot = _only_snapshot(helper)
    marker = snapshot / OWNER_MARKER_NAME
    record = json.loads(marker.read_text())
    assert record["pid"] == child.pid
    assert isinstance(record["start"], str), "this platform can read a process start time"
    record["start"] = record["start"] + " (an earlier process)"
    marker.write_text(json.dumps(record))

    cli(_commands(vault, tmp_path)["sources-list"])
    capsys.readouterr()

    assert not snapshot.exists()
    _release(child)


# --- the sweep on hand-made folders -------------------------------------------------------


def _marker_bytes(pid: object, *, version: object = 1, start: object = None, **extra: object) -> bytes:
    return json.dumps({"version": version, "pid": pid, "start": start, **extra}).encode()


def _populate(directory: Path) -> None:
    (directory / "snapshot-abc.db").write_bytes(b"plaintext copy of the vault")
    (directory / "family-1").mkdir()
    (directory / "family-1" / "snapshot-abc.db-wal").write_bytes(b"plaintext copy of the wal")


def _orphan(root: Path, name: str, pid: int, *, start: object = None, mode: int = 0o700) -> Path:
    directory = root / name
    directory.mkdir(mode=0o700)
    _populate(directory)
    (directory / OWNER_MARKER_NAME).write_bytes(_marker_bytes(pid, start=start))
    os.chmod(directory, mode)
    return directory


def _state(path: Path) -> object:
    """Everything observable about a path, following nothing: kind, mode, contents, link target."""
    if not os.path.lexists(path):
        return None
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return ("link", os.readlink(path), _state(Path(os.path.realpath(path))))
    if stat.S_ISDIR(info.st_mode):
        entries = sorted(os.listdir(path))
        return ("dir", stat.S_IMODE(info.st_mode), [(name, _state(path / name)) for name in entries])
    return ("file", stat.S_IMODE(info.st_mode), path.read_bytes())


def test_an_orphan_is_removed_whole_with_everything_in_it(scratch_tmp: Path, dead_pid: int) -> None:
    """Mutation: make ``_empty_directory`` remove every entry with ``os.unlink``, so the sub-folder
    stays and ``os.rmdir`` meets a folder that is not empty."""
    first = _orphan(scratch_tmp, EXPORT_SNAPSHOT_PREFIX + "aaaa", dead_pid)
    second = _orphan(scratch_tmp, IMPORT_PREVIEW_PREFIX + "bbbb", dead_pid)
    assert sweep_orphaned_snapshots(scratch_tmp) == 2
    assert not first.exists() and not second.exists()


def _no_marker(root: Path, pid: int) -> list[Path]:
    directory = root / (EXPORT_SNAPSHOT_PREFIX + "nomarker")
    directory.mkdir(mode=0o700)
    _populate(directory)
    return [directory]


def _no_marker_preview(root: Path, pid: int) -> list[Path]:
    directory = root / (IMPORT_PREVIEW_PREFIX + "nomarker")
    directory.mkdir(mode=0o700)
    _populate(directory)
    return [directory]


def _group_readable(root: Path, pid: int) -> list[Path]:
    return [_orphan(root, EXPORT_SNAPSHOT_PREFIX + "mode750", pid, mode=0o750)]


def _world_readable(root: Path, pid: int) -> list[Path]:
    return [_orphan(root, IMPORT_PREVIEW_PREFIX + "mode755", pid, mode=0o755)]


def _group_writable(root: Path, pid: int) -> list[Path]:
    return [_orphan(root, EXPORT_SNAPSHOT_PREFIX + "mode770", pid, mode=0o770)]


def _symlink_to_a_folder(root: Path, pid: int) -> list[Path]:
    target = root / "precious"
    target.mkdir(mode=0o700)
    _populate(target)
    (target / OWNER_MARKER_NAME).write_bytes(_marker_bytes(pid))
    link = root / (EXPORT_SNAPSHOT_PREFIX + "link")
    link.symlink_to(target, target_is_directory=True)
    return [link, target]


def _symlink_to_a_folder_outside_the_temp_dir(root: Path, pid: int) -> list[Path]:
    target = root.parent / "elsewhere"
    target.mkdir(mode=0o700)
    _populate(target)
    (target / OWNER_MARKER_NAME).write_bytes(_marker_bytes(pid))
    link = root / (IMPORT_PREVIEW_PREFIX + "link")
    link.symlink_to(target, target_is_directory=True)
    return [link, target]


def _regular_file(root: Path, pid: int) -> list[Path]:
    path = root / (EXPORT_SNAPSHOT_PREFIX + "file")
    path.write_text("not a folder")
    return [path]


def _marker_that_is_a_symlink(root: Path, pid: int) -> list[Path]:
    outside = root.parent / "real-marker-elsewhere"
    outside.mkdir(mode=0o700)
    (outside / OWNER_MARKER_NAME).write_bytes(_marker_bytes(pid))
    directory = root / (EXPORT_SNAPSHOT_PREFIX + "linkedmarker")
    directory.mkdir(mode=0o700)
    _populate(directory)
    (directory / OWNER_MARKER_NAME).symlink_to(outside / OWNER_MARKER_NAME)
    return [directory, outside]


def _marker_that_is_a_folder(root: Path, pid: int) -> list[Path]:
    directory = root / (IMPORT_PREVIEW_PREFIX + "foldermarker")
    directory.mkdir(mode=0o700)
    _populate(directory)
    (directory / OWNER_MARKER_NAME).mkdir()
    return [directory]


def _other_names(root: Path, pid: int) -> list[Path]:
    return [
        _orphan(root, "something-else-aaaa", pid),
        _orphan(root, "tmp" + EXPORT_SNAPSHOT_PREFIX, pid),
        _orphan(root, EXPORT_SNAPSHOT_PREFIX.upper() + "x", pid),
    ]


def _other_snapshot_kinds(root: Path, pid: int) -> list[Path]:
    """The import-file copy and the source replica have their own prefixes and are not swept."""
    return [
        _orphan(root, "alice-memory-import-snapshot-aaaa", pid),
        _orphan(root, "alice-memory-source-replica-aaaa", pid),
    ]


def _unrelated_temp_file(root: Path, pid: int) -> list[Path]:
    path = root / "notes.txt"
    path.write_text("my notes")
    return [path]


LEAVE_ALONE: dict[str, Callable[[Path, int], list[Path]]] = {
    "no marker, export prefix": _no_marker,
    "no marker, preview prefix": _no_marker_preview,
    "mode 0750": _group_readable,
    "mode 0755": _world_readable,
    "mode 0770": _group_writable,
    "symlink to a folder next to it": _symlink_to_a_folder,
    "symlink to a folder outside the temp dir": _symlink_to_a_folder_outside_the_temp_dir,
    "a regular file with the prefix": _regular_file,
    "marker that is a symlink": _marker_that_is_a_symlink,
    "marker that is a folder": _marker_that_is_a_folder,
    "other names": _other_names,
    "other snapshot kinds": _other_snapshot_kinds,
    "an unrelated temp file": _unrelated_temp_file,
}


@pytest.mark.parametrize("case", list(LEAVE_ALONE))
def test_the_sweep_leaves_alone_everything_that_is_not_a_proven_orphan(
    case: str, scratch_tmp: Path, dead_pid: int
) -> None:
    """Every case with a marker has one that names a dead process, so only the named guard keeps
    the folder. Mutations, each alone: ``mode`` drops the ``S_IMODE`` test in ``_sweep_entry``;
    ``other names`` and ``other snapshot kinds`` drop the prefix test in ``_sweep_root``;
    ``symlink`` and ``marker that is a symlink`` set ``_FILE_FLAGS = 0``; ``no marker`` makes
    ``_read_owner_marker`` return a marker for a process that is not running when the file is
    missing. A marker that is a folder, and a regular file with the prefix, are also kept by an
    exception that the sweep catches (``read`` on a folder, ``O_DIRECTORY`` on a file), so no
    single edit to a guard changes them; they pin the outcome."""
    protected = LEAVE_ALONE[case](scratch_tmp, dead_pid)
    before = [_state(path) for path in protected]
    assert all(state is not None for state in before)

    assert sweep_orphaned_snapshots(scratch_tmp) == 0

    assert [_state(path) for path in protected] == before


def test_a_folder_owned_by_another_user_is_left_alone(scratch_tmp: Path, dead_pid: int) -> None:
    """The user id is a parameter, so this runs without a second account. Mutation: delete the
    ``info.st_uid != uid`` test in ``_sweep_entry``."""
    folder = _orphan(scratch_tmp, EXPORT_SNAPSHOT_PREFIX + "theirs", dead_pid)
    before = _state(folder)

    assert sweep_orphaned_snapshots(scratch_tmp, uid=os.geteuid() + 1) == 0
    assert _state(folder) == before

    assert sweep_orphaned_snapshots(scratch_tmp) == 1, "the same folder is an orphan for its owner"
    assert not folder.exists()


MARKERS_THAT_DO_NOT_COUNT: dict[str, Callable[[int], bytes]] = {
    "not json": lambda pid: b"this is not json",
    "empty": lambda pid: b"",
    "a list": lambda pid: b"[1, 2, 3]",
    "version 2": lambda pid: _marker_bytes(pid, version=2),
    "no version": lambda pid: json.dumps({"pid": pid, "start": None}).encode(),
    "start of the wrong type": lambda pid: _marker_bytes(pid, start=5),
    "start that is too long": lambda pid: _marker_bytes(pid, start="x" * 300),
    "start that is empty": lambda pid: _marker_bytes(pid, start=""),
    "pid that is a group": lambda pid: _marker_bytes(-pid),
    "pid that is text": lambda pid: _marker_bytes(str(pid)),
    "a marker over the size limit": lambda pid: _marker_bytes(pid) + b" " * 5000,
}


@pytest.mark.parametrize("case", list(MARKERS_THAT_DO_NOT_COUNT))
def test_a_marker_that_is_not_valid_does_not_make_a_folder_an_orphan(
    case: str, scratch_tmp: Path, dead_pid: int
) -> None:
    """Each marker is valid except for one thing, and names a dead process (for the group case, a
    group that does not exist), so a sweep that accepted it would remove the folder. Mutations,
    each alone, in ``_read_owner_marker``: drop the version test; drop the ``start`` type test;
    drop the ``start`` length test; drop the ``pid <= 0`` test; drop the size test."""
    folder = scratch_tmp / (EXPORT_SNAPSHOT_PREFIX + "badmarker")
    folder.mkdir(mode=0o700)
    _populate(folder)
    (folder / OWNER_MARKER_NAME).write_bytes(MARKERS_THAT_DO_NOT_COUNT[case](dead_pid))
    before = _state(folder)

    assert sweep_orphaned_snapshots(scratch_tmp) == 0

    assert _state(folder) == before


def test_a_pid_that_is_alive_with_no_recorded_start_time_is_kept(scratch_tmp: Path) -> None:
    """When a marker has no start time, the PID alone decides, and a live PID keeps the folder.
    Mutation: make ``_owner_is_running`` return ``False`` when ``recorded_start is None``."""
    folder = _orphan(scratch_tmp, EXPORT_SNAPSHOT_PREFIX + "unknownstart", os.getpid(), start=None)
    before = _state(folder)
    assert sweep_orphaned_snapshots(scratch_tmp) == 0
    assert _state(folder) == before


def test_the_start_time_the_helper_records_for_this_process_matches_what_the_sweep_reads(
    scratch_tmp: Path,
) -> None:
    """The marker the product writes for this process, checked by the sweep in this process. The
    folder is kept. Mutation: make ``_process_details`` return a different token on each call."""
    with private_snapshot_directory(EXPORT_SNAPSHOT_PREFIX) as folder:
        record = json.loads((folder / OWNER_MARKER_NAME).read_text())
        assert sweep_orphaned_snapshots(scratch_tmp) == 0
        assert folder.is_dir()
    assert record["pid"] == os.getpid()
    assert not folder.exists()


def test_a_zombie_owner_does_not_keep_its_folder(scratch_tmp: Path) -> None:
    """A process that exited but has not been reaped still has a PID. Its folder is an orphan.
    Mutation: empty ``_DEAD_PROCESS_STATES``."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 15
        details: tuple[str | None, str | None] = (None, None)
        while time.monotonic() < deadline:
            details = snapshot_sweep._process_details(child.pid)
            if details[0] == "Z":
                break
            time.sleep(0.05)
        assert details[0] == "Z", f"the exited child never showed as a zombie: {details}"
        folder = _orphan(scratch_tmp, EXPORT_SNAPSHOT_PREFIX + "zombie", child.pid, start=details[1])
        assert sweep_orphaned_snapshots(scratch_tmp) == 1
        assert not folder.exists()
    finally:
        child.wait(timeout=30)


def test_a_failed_removal_keeps_the_marker_and_does_not_stop_the_other_folders(
    scratch_tmp: Path, dead_pid: int
) -> None:
    """One folder holds something that cannot be removed. The others still go, and the stuck
    folder keeps its marker so the next sweep finds it again. Mutations, each alone: move the
    ``os.unlink(OWNER_MARKER_NAME, ...)`` line in ``_empty_directory`` to the top of the
    function; delete the ``try``/``except`` around ``_sweep_entry`` in ``_sweep_root``."""
    if os.geteuid() == 0:
        pytest.skip("root can remove a read-only folder's contents")
    stuck = _orphan(scratch_tmp, EXPORT_SNAPSHOT_PREFIX + "stuck", dead_pid)
    (stuck / "family-1").chmod(0o500)
    others = [_orphan(scratch_tmp, IMPORT_PREVIEW_PREFIX + f"other{index}", dead_pid) for index in range(3)]
    try:
        assert sweep_orphaned_snapshots(scratch_tmp) == 3
        assert all(not folder.exists() for folder in others)
        assert (stuck / OWNER_MARKER_NAME).is_file(), "the marker goes last"
    finally:
        (stuck / "family-1").chmod(0o700)
    assert sweep_orphaned_snapshots(scratch_tmp) == 1
    assert not stuck.exists()


def test_a_folder_swapped_for_a_link_after_it_was_checked_is_not_followed(
    scratch_tmp: Path, dead_pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the checks and the removal, the name is replaced by a link to a folder that holds a
    canary. Removal works on the folder that was checked, not on the name. Mutation: replace
    ``_empty_directory(directory_fd)`` with a loop over ``os.path.join(tempfile.gettempdir(), name)``."""
    orphan = _orphan(scratch_tmp, EXPORT_SNAPSHOT_PREFIX + "swapped", dead_pid)
    canary = scratch_tmp / "canary"
    canary.mkdir(mode=0o700)
    _populate(canary)
    (canary / OWNER_MARKER_NAME).write_bytes(_marker_bytes(dead_pid))
    before = _state(canary)
    real_check = snapshot_sweep._owner_is_running

    def swap_then_report(pid: int, start: str | None) -> bool:
        os.rename(orphan, scratch_tmp / "moved-away")
        orphan.symlink_to(canary, target_is_directory=True)
        return real_check(pid, start)

    monkeypatch.setattr(snapshot_sweep, "_owner_is_running", swap_then_report)

    sweep_orphaned_snapshots(scratch_tmp)

    assert _state(canary) == before
    assert orphan.is_symlink(), "the link in its place is left"


def test_a_sweep_that_cannot_run_does_not_fail_the_command(
    vault: Path,
    scratch_tmp: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation: delete the outer ``try``/``except Exception`` in ``sweep_orphaned_snapshots``."""

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("the sweep broke")

    monkeypatch.setattr(snapshot_sweep, "_sweep_root", explode)

    assert cli(_commands(vault, tmp_path)["sources-list"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"]
    assert sweep_orphaned_snapshots(scratch_tmp / "does-not-exist") == 0


def test_one_real_command_removes_both_kinds_of_orphan_and_nothing_else(
    vault: Path,
    scratch_tmp: Path,
    tmp_path: Path,
    children: list[subprocess.Popen[str]],
    dead_pid: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A real orphan of each kind, and a decoy for every guard, then one real command. Only the two
    orphans go. Mutation: widen the sweep to every folder under the temp directory by dropping the
    prefix test in ``_sweep_root``."""
    export_orphan = _kill_inside("export", vault, children)
    preview_orphan = _kill_inside("preview", vault, children)
    protected: list[Path] = []
    for build in LEAVE_ALONE.values():
        protected.extend(build(scratch_tmp, dead_pid))
    before = [_state(path) for path in protected]

    cli(_commands(vault, tmp_path)["sources-list"])
    capsys.readouterr()

    assert not export_orphan.exists() and not preview_orphan.exists()
    assert [_state(path) for path in protected] == before


# --- the folder the helper makes ------------------------------------------------------------


def test_the_snapshot_folder_is_private_and_names_its_owner(scratch_tmp: Path) -> None:
    """Mutations, each alone: ``os.chmod(directory, 0o755)`` in ``private_snapshot_directory``; open
    the marker with mode 0644 in ``_write_owner_marker``."""
    previous = os.umask(0o022)
    try:
        with private_snapshot_directory(IMPORT_PREVIEW_PREFIX) as folder:
            assert folder.parent == scratch_tmp and folder.name.startswith(IMPORT_PREVIEW_PREFIX)
            assert stat.S_IMODE(folder.stat().st_mode) == 0o700
            marker = folder / OWNER_MARKER_NAME
            assert stat.S_IMODE(marker.stat().st_mode) == 0o600
            record = json.loads(marker.read_text())
            assert record["version"] == 1 and record["pid"] == os.getpid()
            assert record["start"] is None or isinstance(record["start"], str)
    finally:
        os.umask(previous)
    assert not folder.exists()


def test_a_prefix_the_sweep_does_not_know_is_refused(scratch_tmp: Path) -> None:
    """Every kind of snapshot folder must be one the sweep can find. Mutation: delete the
    ``if prefix not in SNAPSHOT_PREFIXES`` check."""
    with pytest.raises(ValueError):
        with private_snapshot_directory("alice-something-new-"):
            pass
    assert list(scratch_tmp.iterdir()) == []


def test_a_failed_marker_write_does_not_fail_the_snapshot(
    scratch_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: delete the ``try``/``except OSError`` around ``_write_owner_marker``."""

    def refuse(_directory: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(snapshot_sweep, "_write_owner_marker", refuse)
    with private_snapshot_directory(EXPORT_SNAPSHOT_PREFIX) as folder:
        assert folder.is_dir()
    assert not folder.exists()


def _wider_than_owner_only(folder: Path) -> list[tuple[str, int]]:
    """Entries under the folder that are not 0600 (files) or 0700 (folders), with their modes."""
    wide = []
    for current, names, files in os.walk(folder):
        for name in names:
            mode = stat.S_IMODE(os.lstat(Path(current, name)).st_mode)
            if mode != 0o700:
                wide.append((str(Path(current, name).relative_to(folder)), mode))
        for name in files:
            mode = stat.S_IMODE(os.lstat(Path(current, name)).st_mode)
            if mode != 0o600:
                wide.append((str(Path(current, name).relative_to(folder)), mode))
    return wide


def test_the_import_preview_copies_are_owner_only_from_the_moment_they_exist(
    vault: Path, scratch_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview copies the vault and ``sleep_proposals.jsonl``. A plain copy is made with the
    process umask, usually 022, so 0644. The check runs just before ``bootstrap_database``, which
    would otherwise tighten the database file afterwards. Mutations, each alone: copy the sidecar
    with ``shutil.copyfile`` in ``_markdown_import_database``; delete the line there that creates
    ``memory.db`` with ``os.open(..., 0o600)``."""
    sidecar = sleep_proposals_path(vault)
    sidecar.write_text(json.dumps({"user_id": USER_ID, "excerpt": "kestrelplum"}) + "\n")
    arguments = argparse.Namespace(dry_run=True, db=None, user_id=UUID(USER_ID), user_email="local@alice")
    real_bootstrap = onramp_module.bootstrap_database
    seen: list[list[tuple[str, int]]] = []

    def look_then_bootstrap(db_path: Path, **kwargs: object) -> None:
        seen.append(_wider_than_owner_only(db_path.parent))
        real_bootstrap(db_path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(onramp_module, "bootstrap_database", look_then_bootstrap)
    previous = os.umask(0o022)
    try:
        with onramp_module._markdown_import_database(vault, arguments) as selected:
            assert sleep_proposals_path(selected).read_bytes() == sidecar.read_bytes()
            assert (selected.parent / "memory.db").stat().st_size > 0
            assert _wider_than_owner_only(selected.parent) == []
    finally:
        os.umask(previous)
    assert seen == [[]]


def test_the_export_snapshot_is_owner_only_all_the_way_down(
    vault: Path, scratch_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The export snapshot was already owner-only, and this keeps it so. Mutation: put
    ``os.chmod(snapshot_path, 0o644)`` after ``_secure_sqlite_files(snapshot_path)`` in
    ``_prepared_export_connection``."""
    previous = os.umask(0o000)
    try:
        with onramp_module._prepared_export_connection(vault, UUID(USER_ID)):
            folders = [entry for entry in scratch_tmp.iterdir() if entry.name.startswith(EXPORT_SNAPSHOT_PREFIX)]
            assert len(folders) == 1 and any(folders[0].glob("snapshot-*.db"))
            assert _wider_than_owner_only(folders[0]) == []
    finally:
        os.umask(previous)


# --- reading a process on Linux --------------------------------------------------------------


def _linux_stat_line(command: str, *, state: str = "S", start: str = "987654") -> str:
    after_the_command = [state] + ["1"] * 18 + [start] + ["0"] * 30
    return f"4242 ({command}) " + " ".join(after_the_command) + "\n"


@pytest.mark.parametrize("command", ["python3", "tmux: server", "a (b) c", "evil) S 1 2 3 (x", ""])
def test_the_linux_stat_parser_counts_fields_from_the_last_parenthesis(command: str) -> None:
    """A command name can hold spaces and parentheses. Mutation: ``text.find(")")`` for ``text.rfind(")")``."""
    assert parse_linux_stat(_linux_stat_line(command, state="Z", start="31337")) == ("Z", "31337")


@pytest.mark.parametrize(
    "text", ["", "no parenthesis here", "1 (x) S 1 2", "1 (x) SS " + "1 " * 30, "1 (x) S " + "a " * 30]
)
def test_the_linux_stat_parser_refuses_text_that_is_not_a_stat_line(text: str) -> None:
    assert parse_linux_stat(text) is None


def test_the_docs_say_an_interrupted_command_can_leave_a_snapshot_and_the_next_one_removes_it() -> None:
    """Mutation: delete the sentence from ``docs/alpha/backup-and-restore.md``."""
    text = (ROOT / "docs/alpha/backup-and-restore.md").read_text(encoding="utf-8")
    sentence = next((line for line in text.split("\n\n") if "interrupted" in line and "snapshot" in line), "")
    assert sentence.startswith("Unreleased (on main, not in v0.20.0):"), sentence
    assert "removes" in sentence and "next" in sentence
