"""Private snapshot folders for ``alice-memory``, and the sweep that removes orphans.

``export``, ``sources list``, the ``sources delete`` and ``sources prune`` previews and
``import-markdown --dry-run`` copy the vault into a folder under the temp directory and
read the copy. ``import`` copies the file it is about to read into a folder there too.
A normal exit or an exception removes the folder. A SIGKILL or a power loss skips that
cleanup and leaves a plaintext copy of the vault, or of the import file, behind.

Each folder therefore carries a small marker file that names the process that made it
(its PID and its start time). Before it makes a new folder, every one of those commands
removes the folders of processes that are gone. The sweep is deliberately narrow. It
only touches a folder that is all of these:

* directly under the temp directory, with one of the name prefixes in ``SNAPSHOT_PREFIXES``;
* a real directory (never a symlink), owned by the current user, with mode 0700;
* holding a marker, a small file with valid contents; and
* named by a marker whose process is gone, or whose PID now belongs to a process that
  started at a different time.

Anything else is left alone, including a folder with no marker (another process may be
creating it right now). When the sweep cannot tell whether a process is running, it
keeps the folder. A failed sweep never fails the command that ran it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess  # nosec B404 # one fixed ps command, only to read a process start time
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

EXPORT_SNAPSHOT_PREFIX = "alice-memory-export-snapshot-"
IMPORT_PREVIEW_PREFIX = "alice-import-preview-"
IMPORT_SNAPSHOT_PREFIX = "alice-memory-import-snapshot-"
SNAPSHOT_PREFIXES = (EXPORT_SNAPSHOT_PREFIX, IMPORT_PREVIEW_PREFIX, IMPORT_SNAPSHOT_PREFIX)
OWNER_MARKER_NAME = ".alice-snapshot-owner"
OWNER_MARKER_VERSION = 1

_MARKER_MAX_BYTES = 4096
_START_TOKEN_MAX_CHARS = 256
_PS_COMMAND = "/bin/ps"
_PS_TIMEOUT_SECONDS = 5
# Linux state letters for a zombie and a dead process. Neither runs any more.
_DEAD_PROCESS_STATES = frozenset("ZXx")
_FILE_FLAGS = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def parse_linux_stat(text: str) -> tuple[str, str] | None:
    """Return (state letter, start time) from the text of ``/proc/<pid>/stat``.

    The command name is field 2, sits in parentheses, and may hold spaces and
    parentheses, so fields are counted from the last closing parenthesis. State is
    field 3 and start time is field 22. ``None`` means the text did not parse.
    """

    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 1 :].split()
    if len(fields) < 20 or len(fields[0]) != 1 or not fields[19].isdigit():
        return None
    return fields[0], fields[19]


def _linux_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""


def _process_details(pid: int) -> tuple[str | None, str | None]:
    """Return (state letter, start token) for a process, each ``None`` when unreadable.

    The token is the same string for the whole life of a process and differs for a
    process that later reuses the PID. It is built from the kernel start time (Linux,
    with the boot id) or from ``ps`` in UTC (macOS and the BSDs). Never raises.
    """

    if os.path.exists("/proc/self/stat"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, None
        parsed = parse_linux_stat(text)
        if parsed is None:
            return None, None
        return parsed[0], f"linux:{_linux_boot_id()}:{parsed[1]}"
    try:
        done = subprocess.run(  # nosec B603 # fixed argument list, no shell, pid is an int
            [_PS_COMMAND, "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": "/usr/bin:/bin", "TZ": "UTC", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if done.returncode != 0:
        return None, None
    parts = done.stdout.split(None, 1)
    if len(parts) != 2:
        return None, None
    return parts[0][:1], "ps:" + " ".join(parts[1].split())


def _owner_is_running(pid: int, recorded_start: str | None) -> bool:
    """Whether the process a marker names is still running. Unsure means yes."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # the PID exists and belongs to another user
    state, start = _process_details(pid)
    if state is not None and state in _DEAD_PROCESS_STATES:
        return False
    if recorded_start is None or start is None:
        return True
    return start == recorded_start


def _write_owner_marker(directory: Path) -> None:
    pid = os.getpid()
    _state, start = _process_details(pid)
    payload = json.dumps({"version": OWNER_MARKER_VERSION, "pid": pid, "start": start}).encode("ascii")
    descriptor = os.open(
        directory / OWNER_MARKER_NAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def private_snapshot_directory(prefix: str) -> Iterator[Path]:
    """Yield an owner-only temp folder that names its owner process, after a sweep.

    ``prefix`` must be one of ``SNAPSHOT_PREFIXES`` so that every kind of folder made
    here is one the sweep can find. The marker is written before anything else goes
    into the folder, so a folder that holds a vault copy has one unless it could not
    be written.
    """

    if prefix not in SNAPSHOT_PREFIXES:
        raise ValueError(f"unknown snapshot prefix: {prefix}")
    sweep_orphaned_snapshots()
    with tempfile.TemporaryDirectory(prefix=prefix) as raw_dir:
        directory = Path(raw_dir)
        os.chmod(directory, 0o700)
        try:
            _write_owner_marker(directory)
        except OSError:
            # Without a marker an interrupted run is left alone, as before. The
            # copy itself is still private, so do not fail the command for this.
            logger.debug("could not write the snapshot owner marker", exc_info=True)
        yield directory


def _read_owner_marker(directory_fd: int) -> tuple[int, str | None] | None:
    """Return (pid, start token) from a valid marker, else ``None``. Never follows a link."""

    try:
        marker_fd = os.open(
            OWNER_MARKER_NAME,
            os.O_RDONLY | os.O_NONBLOCK | _FILE_FLAGS,
            dir_fd=directory_fd,
        )
    except OSError:
        return None
    try:
        info = os.fstat(marker_fd)
        if info.st_size > _MARKER_MAX_BYTES:
            return None
        raw = os.read(marker_fd, _MARKER_MAX_BYTES + 1)
    finally:
        os.close(marker_fd)
    try:
        record = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != OWNER_MARKER_VERSION:
        return None
    pid = record.get("pid")
    start = record.get("start")
    if type(pid) is not int or pid <= 0:
        return None
    if start is not None and (not isinstance(start, str) or not 0 < len(start) <= _START_TOKEN_MAX_CHARS):
        return None
    return pid, start


def _empty_directory(directory_fd: int) -> None:
    """Remove what a snapshot folder holds, leaving the marker for last.

    If anything fails part way, the marker is still there, so the next sweep finds the
    folder and tries again. Everything is removed relative to the open folder, and no
    link is followed, so a swapped name cannot send the removal somewhere else.
    """

    for name in os.listdir(directory_fd):
        if name == OWNER_MARKER_NAME:
            continue
        if stat.S_ISDIR(os.lstat(name, dir_fd=directory_fd).st_mode):
            shutil.rmtree(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)
    os.unlink(OWNER_MARKER_NAME, dir_fd=directory_fd)


def _sweep_entry(root_fd: int, name: str, uid: int) -> bool:
    # O_NOFOLLOW and O_DIRECTORY: a symlink, a file or a FIFO fails to open here and is
    # never read or removed. The checks that follow are made on the opened folder, so a
    # name that changes after this call cannot get a different folder past them.
    directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _FILE_FLAGS, dir_fd=root_fd)
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
            return False
        marker = _read_owner_marker(directory_fd)
        if marker is None:
            return False
        pid, start = marker
        if _owner_is_running(pid, start):
            return False
        _empty_directory(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=root_fd)
    return True


def _sweep_root(root_fd: int, uid: int) -> int:
    removed = 0
    for name in os.listdir(root_fd):
        if not name.startswith(SNAPSHOT_PREFIXES):
            continue
        try:
            if _sweep_entry(root_fd, name, uid):
                removed += 1
        except Exception:  # noqa: BLE001 - one bad folder must not stop the others
            logger.debug("orphaned snapshot sweep skipped %s", name, exc_info=True)
    return removed


def sweep_orphaned_snapshots(temp_dir: Path | str | None = None, *, uid: int | None = None) -> int:
    """Remove snapshot folders that a process which is gone left in the temp directory.

    Returns how many folders were removed. Never raises: a sweep that cannot run, or
    cannot remove one folder, logs at debug level and carries on with the next.
    """

    try:
        if os.name != "posix" or os.open not in os.supports_dir_fd:
            return 0
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            return 0
        if not shutil.rmtree.avoids_symlink_attacks:
            return 0
        root = Path(tempfile.gettempdir() if temp_dir is None else temp_dir)
        owner = os.geteuid() if uid is None else uid
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        try:
            return _sweep_root(root_fd, owner)
        finally:
            os.close(root_fd)
    except Exception:  # noqa: BLE001 - a sweep must never fail the command
        logger.debug("orphaned snapshot sweep did not run", exc_info=True)
        return 0


__all__ = [
    "EXPORT_SNAPSHOT_PREFIX",
    "IMPORT_PREVIEW_PREFIX",
    "IMPORT_SNAPSHOT_PREFIX",
    "OWNER_MARKER_NAME",
    "OWNER_MARKER_VERSION",
    "SNAPSHOT_PREFIXES",
    "parse_linux_stat",
    "private_snapshot_directory",
    "sweep_orphaned_snapshots",
]
