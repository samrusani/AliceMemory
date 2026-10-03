"""Shared builders for the project identity tests.

``FakeFileSystem`` is an in-memory filesystem that follows the path rules of
either platform, so one runner covers the Windows and case-insensitive rules.
``make_repo`` and friends write real git layouts by hand, and ``run_git`` builds
the same layouts with the real ``git`` binary under a scrubbed environment (it
never reads the owner's git configuration).
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import subprocess
from pathlib import Path

from alicebot_api.project_identity import EntryKind


def config_text(url: str | None = None, *, name: str = "origin", extra: str = "") -> str:
    """A git config like the one ``git init`` writes, with an optional remote."""

    text = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
    if url is not None:
        text += f'[remote "{name}"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/{name}/*\n'
    return text + extra


def make_repo(root: Path, *, config: str | None = "") -> Path:
    """Write ``root/.git`` as a directory holding HEAD and a config.

    ``config=""`` writes the default config, a string writes that text, and
    ``None`` writes no config file at all.
    """

    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    if config is not None:
        (git_dir / "config").write_text(config or config_text())
    return root


def scrubbed_git_env(home: Path) -> dict[str, str]:
    """An environment under which git reads no configuration outside the repo."""

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture",
    }
    return env


def run_git(home: Path, cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=scrubbed_git_env(home),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class FakeFileSystem:
    """An in-memory filesystem that follows one platform's path rules.

    ``windows`` selects ``ntpath`` (drive letters, backslashes, case folded
    lookups). By default ``realpath`` returns the spelling that was stored, as
    Windows does; ``canonical_realpath=False`` returns the spelling it was given,
    so a test can show that a comparison folds case itself. ``reads`` records
    every ``read_capped`` call as a path and a limit, and ``probed`` records every
    ``entry_kind`` path.
    """

    def __init__(self, *, windows: bool = False, canonical_realpath: bool = True) -> None:
        self.windows = windows
        self.case_insensitive = windows
        self.canonical_realpath = canonical_realpath
        self._dirs: dict[str, str] = {}
        self._files: dict[str, tuple[str, bytes]] = {}
        self.reads: list[tuple[str, int]] = []
        self.probed: list[str] = []

    @property
    def _path(self):  # type: ignore[no-untyped-def]
        return ntpath if self.windows else posixpath

    def _normal(self, path: str) -> str:
        return self._path.normpath(path)

    def _key(self, path: str) -> str:
        normal = self._normal(path)
        return normal.lower() if self.case_insensitive else normal

    def add_dir(self, path: str) -> None:
        normal = self._normal(path)
        while True:
            self._dirs.setdefault(self._key(normal), normal)
            parent = self._path.dirname(normal)
            if parent == normal:
                return
            normal = parent

    def add_file(self, path: str, data: bytes | str) -> None:
        self.add_dir(self._path.dirname(self._normal(path)))
        payload = data.encode("utf-8") if isinstance(data, str) else data
        normal = self._normal(path)
        self._files[self._key(normal)] = (normal, payload)

    def realpath(self, path: str) -> str:
        normal = self._normal(path)
        if not self.canonical_realpath:
            return normal
        key = self._key(normal)
        if key in self._dirs:
            return self._dirs[key]
        if key in self._files:
            return self._files[key][0]
        return normal

    def entry_kind(self, path: str) -> EntryKind:
        self.probed.append(path)
        key = self._key(path)
        if key in self._dirs:
            return "dir"
        if key in self._files:
            return "file"
        return "missing"

    def read_capped(self, path: str, limit: int) -> bytes:
        self.reads.append((path, limit))
        key = self._key(path)
        if key not in self._files:
            raise OSError("not a file")
        return self._files[key][1][: limit + 1]
