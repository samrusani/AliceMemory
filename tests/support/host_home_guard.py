"""Keep unit tests out of the real home folders of agent hosts.

Agent hosts keep their settings under the process home: ``.claude``,
``.claude.json``, ``.codex``, ``.hermes``, ``.cursor``, ``.config/opencode``,
``.openclaw`` and ``.alice``. A unit test must never open, stat, list or write
anything there. It points ``HOME`` at ``tmp_path`` or passes ``--home``.

``HostHomeGuard`` is armed for the length of each unit test by the autouse
fixture in ``tests/unit/conftest.py``. While it is armed:

* An audit hook watches ``open``, ``os.listdir``, ``os.scandir`` and the other
  path-taking events. Python 3.12 raises no audit event for ``os.stat``,
  ``os.lstat`` or ``os.access``, so those three are wrapped for as long as any
  guard is armed. ``os.path.exists``, ``Path.exists``, ``Path.is_dir`` and
  ``shutil.which`` all go through ``os.stat``.
* A watched path is refused before the system call runs, with a
  ``PermissionError``, and the violation is recorded. Code that catches
  ``OSError`` cannot hide it: the fixture fails the test from the record.

The real home is read once, when the guard is built, so a test that later
patches ``HOME`` does not move what is watched. Two spellings of each home are
watched (the path as given and its resolved path) and the passwd home is
watched too, so an unset ``HOME`` does not open a gap.

What it does not do: it does not follow symlinks, it does not watch child
processes (a subprocess is not covered), and it does not watch host folders
named by an environment variable such as ``CODEX_HOME``. One narrow allowance:
a stat or access check of a file that sits directly in a ``PATH`` directory
that is itself inside a watched folder is let through, because
``shutil.which`` probes every ``PATH`` entry. Nothing in that folder can be
opened or listed.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

WATCHED_NAMES: tuple[str, ...] = (
    ".claude",
    ".claude.json",
    ".codex",
    ".hermes",
    ".cursor",
    ".config/opencode",
    ".openclaw",
    ".alice",
)

# Files, not folders: ``.claude.json.lock`` and ``.claude.json.backup`` belong
# to the same file, so a sibling that starts with ``<name>.`` is watched too.
_FILE_NAMES = frozenset({".claude.json"})

_ONE_PATH_EVENTS = frozenset(
    {
        "open",
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.fwalk",
        "os.mkdir",
        "os.remove",
        "os.rmdir",
        "os.chmod",
        "os.chown",
        "os.chdir",
        "os.utime",
        "os.truncate",
        "glob.glob",
        "pathlib.Path.glob",
        "pathlib.Path.rglob",
        "sqlite3.connect",
        "shutil.rmtree",
    }
)
_TWO_PATH_EVENTS = frozenset(
    {
        "os.rename",
        "os.link",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
    }
)
_STAT_NAMES = ("stat", "lstat", "access")
_STAT_EVENTS = frozenset(f"os.{name}" for name in _STAT_NAMES)


class HostHomeAccessError(PermissionError):
    """A test reached for a host folder under the real home."""


def _as_text(raw: object) -> str | None:
    """The path as text, or None for a file descriptor, None or a non-path."""

    if isinstance(raw, str):
        return raw or None
    try:
        if isinstance(raw, bytes):
            return os.fsdecode(raw) or None
        if isinstance(raw, os.PathLike):
            return os.fsdecode(os.fspath(raw)) or None
    except (TypeError, ValueError):
        return None
    return None


def process_homes() -> tuple[str, ...]:
    """The homes of this process, as seen right now: ``HOME`` and the passwd home."""

    homes: list[str] = []
    try:
        homes.append(str(Path.home()))
    except (RuntimeError, KeyError):
        pass
    try:
        import pwd

        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        pass
    return tuple(home for home in homes if home)


class HostHomeGuard:
    """Refuses and records access to host folders under the given homes."""

    def __init__(
        self,
        homes: Iterable[str | os.PathLike[str]],
        *,
        names: Iterable[str] = WATCHED_NAMES,
        path_env: str | None = None,
    ) -> None:
        variants: list[str] = []
        for home in homes:
            text = os.fspath(home)
            for variant in (os.path.abspath(text), os.path.realpath(text)):
                if variant not in variants:
                    variants.append(variant)
        self.homes: tuple[str, ...] = tuple(variants)
        roots: list[tuple[str, bool]] = []
        for variant in variants:
            for name in names:
                root = os.path.join(variant, *name.split("/"))
                roots.append((root, name in _FILE_NAMES))
        self._roots = tuple(roots)
        self._prefixes = tuple(variant.rstrip(os.sep) + os.sep for variant in variants)
        self._allowed: list[str] = []
        self._lookup_dirs: frozenset[str] = frozenset(
            directory
            for directory in (
                os.path.normpath(entry)
                for entry in (path_env or "").split(os.pathsep)
                if entry and os.path.isabs(entry)
            )
            if self._watched(directory)
        )
        self._violations: list[str] = []
        # Totals for the whole process, for the summary line a test run prints.
        self.checks = 0
        self.refused = 0

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(self._violations)

    def allow(self, path: str | os.PathLike[str]) -> None:
        """Skip everything under ``path`` (the test temp folder)."""

        text = os.path.normpath(os.path.abspath(os.fspath(path)))
        if text not in self._allowed:
            self._allowed.append(text)

    def blocks(self, raw: object, *, stat_only: bool = False) -> str | None:
        """The normalized path when it is watched and not allowed, else None.

        Pure string work: nothing here touches the file system except
        ``os.getcwd`` for a relative path.
        """

        text = _as_text(raw)
        if text is None:
            return None
        if not text.startswith(os.sep):
            try:
                text = os.path.join(os.getcwd(), text)
            except OSError:
                return None
        text = os.path.normpath(text)
        if not text.startswith(self._prefixes):
            return None
        if not self._watched(text):
            return None
        for allowed in self._allowed:
            if text == allowed or text.startswith(allowed + os.sep):
                return None
        if stat_only and os.path.dirname(text) in self._lookup_dirs:
            return None
        return text

    def _watched(self, text: str) -> bool:
        for root, is_file in self._roots:
            if text == root or text.startswith(root + os.sep):
                return True
            if is_file and text.startswith(root + "."):
                return True
        return False

    def check(self, event: str, *paths: object) -> None:
        """Raise ``HostHomeAccessError`` when any of ``paths`` is watched."""

        self.checks += 1
        stat_only = event in _STAT_EVENTS
        for raw in paths:
            hit = self.blocks(raw, stat_only=stat_only)
            if hit is not None:
                self.refused += 1
                self._violations.append(f"{event} {hit}")
                raise HostHomeAccessError(
                    errno.EACCES,
                    "a unit test reached into a real agent host folder; point HOME at tmp_path or pass --home",
                    hit,
                )

    @contextlib.contextmanager
    def armed(self) -> Iterator[HostHomeGuard]:
        """Watch for the length of the block. Violations start empty."""

        global _ARMED
        self._violations.clear()
        _install_hook()
        _install_stat_wrappers()
        _ARMED = _ARMED + (self,)
        try:
            yield self
        finally:
            _ARMED = tuple(guard for guard in _ARMED if guard is not self)
            _remove_stat_wrappers()

    @property
    def is_armed(self) -> bool:
        return any(guard is self for guard in _ARMED)


_ARMED: tuple[HostHomeGuard, ...] = ()
_HOOK_INSTALLED = False
_WRAPPED: dict[str, tuple[Callable[..., object], Callable[..., object]]] = {}
_WRAP_DEPTH = 0


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not _ARMED:
        return
    if event in _ONE_PATH_EVENTS:
        paths = args[:1]
    elif event in _TWO_PATH_EVENTS:
        paths = args[:2]
    else:
        return
    for guard in _ARMED:
        guard.check(event, *paths)


def _install_hook() -> None:
    """Audit hooks cannot be removed, so this one stays and goes quiet when disarmed."""

    global _HOOK_INSTALLED
    if not _HOOK_INSTALLED:
        sys.addaudithook(_audit)
        _HOOK_INSTALLED = True


def _supports_sets() -> tuple[set[Callable[..., object]], ...]:
    return (
        os.supports_dir_fd,
        os.supports_effective_ids,
        os.supports_fd,
        os.supports_follow_symlinks,
    )


def _make_wrapper(name: str, real: Callable[..., object]) -> Callable[..., object]:
    event = f"os.{name}"

    @functools.wraps(real)
    def guarded(*args: object, **kwargs: object) -> object:
        if _ARMED and kwargs.get("dir_fd") is None:
            path = args[0] if args else kwargs.get("path")
            for guard in _ARMED:
                guard.check(event, path)
        return real(*args, **kwargs)

    return guarded


def _install_stat_wrappers() -> None:
    """Wrap os.stat, os.lstat and os.access while any guard is armed.

    The wrappers join the ``os.supports_*`` sets the originals are in, so a
    module that checks ``os.stat in os.supports_follow_symlinks`` still
    gets the answer it would get without the guard.
    """

    global _WRAP_DEPTH
    _WRAP_DEPTH += 1
    if _WRAP_DEPTH > 1:
        return
    for name in _STAT_NAMES:
        real = getattr(os, name)
        wrapper = _make_wrapper(name, real)
        setattr(os, name, wrapper)
        for supports in _supports_sets():
            if real in supports:
                supports.add(wrapper)
        _WRAPPED[name] = (real, wrapper)


def _remove_stat_wrappers() -> None:
    global _WRAP_DEPTH
    _WRAP_DEPTH -= 1
    if _WRAP_DEPTH > 0:
        return
    for name, (real, wrapper) in list(_WRAPPED.items()):
        if getattr(os, name) is wrapper:
            setattr(os, name, real)
        for supports in _supports_sets():
            supports.discard(wrapper)
    _WRAPPED.clear()


def process_guard() -> HostHomeGuard:
    """A guard on the homes this process has right now. Build it before tests patch HOME."""

    return HostHomeGuard(process_homes(), path_env=os.environ.get("PATH"))
