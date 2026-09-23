"""Test helpers that pin where install looks for a launcher. No binary is run.

install's launcher search reads PATH, this interpreter's scripts dir, the
directory of sys.executable, the pip --user scripts dir and sys.prefix.
Each differs between this Mac, CI and a contributor's machine, so tests
that care pin every one of them to temp dirs.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

import pytest

from alicebot_api import host_launcher

SH_ARGV_STUB = '#!/bin/sh\nprintf \'%s\\0\' "$0" "$@" > "$ARGV_OUT"\n'


def executable(path: Path, content: str = "") -> Path:
    """A file with mode 0755, standing in for an installed program."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_scripts(directory: Path, *, session_start: bool = True, stub: str = "") -> Path:
    """alice-memory (and alice-memory-session-start) in ``directory``."""

    executable(directory / "alice-memory", stub)
    if session_start:
        executable(directory / "alice-memory-session-start", stub)
    return directory


def pin_launcher_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    uvx: str | None,
    interpreter: Path | None = None,
    python_bin: Path | None = None,
    user: Path | None = None,
    on_path: Mapping[str, str | None] | None = None,
    prefix: str | None = None,
) -> None:
    """Pin every input of host_launcher.find_launcher.

    ``uvx`` is what ``shutil.which("uvx")`` returns. The scripts dirs
    default to one empty dir. ``on_path`` gives ``shutil.which`` answers
    for the script names; unlisted script names are not on PATH. Every
    other name goes to the real lookup.
    """

    empty = tmp_path / "no-scripts-here"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(host_launcher, "_interpreter_scripts_dir", lambda: interpreter or empty)
    monkeypatch.setattr(host_launcher, "_python_bin_dir", lambda: python_bin or empty)
    monkeypatch.setattr(host_launcher, "_user_scripts_dir", lambda: user or empty)
    monkeypatch.setattr(
        host_launcher, "_running_prefix", lambda: prefix or str(tmp_path / "python-prefix")
    )
    answers: dict[str, str | None] = {
        "uvx": uvx,
        "alice-memory": None,
        "alice-memory-session-start": None,
        **(on_path or {}),
    }
    real_which = shutil.which

    def which(name: str, *args: object, **kwargs: object) -> str | None:
        if name in answers:
            return answers[name]
        return real_which(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "which", which)
