"""alice-memory install warns when it cannot find a way to start Alice; it does not refuse.

Why this exists. Until 2026-09-22 the README said the packaged path needed
"Python 3.12+ and nothing else", while every host entry and hook that
install writes starts Alice with uvx. A user with Python and pip but no uv
got a clean exit 0 and hosts that could not start Alice. Nothing checked
for uvx, so nothing said so.

Since S4.6 (2026-09-23) install writes the installed alice-memory scripts
when uvx is missing; see test_install_without_uvx. The warning is left for
the case where neither uvx nor a pair of those scripts in one directory can
be found, and it names every directory it looked in. It is advice only:
the files are still right once uv is installed, so the exit code does not
change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alicebot_api.host_install import host_file_map
from alicebot_api.host_launcher import UVX_MISSING_WARNING_PREFIX
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import pin_launcher_search


def _install(capsys, home: Path, vault: Path) -> tuple[int, str]:
    code = onramp_main(["install", "--home", str(home), "--host", "cursor", "--data-dir", str(vault)])
    return code, capsys.readouterr().out


def test_nothing_to_start_alice_prints_a_warning_and_keeps_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Neither uvx nor the scripts: warning first, uvx entry written, exit 0.

    Mutation: drop the warning, or refuse the host. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    home = tmp_path / "home"
    code, out = _install(capsys, home, tmp_path / "vault")
    assert code == 0
    first = out.split("\n\n", 1)[0]
    assert first.startswith(UVX_MISSING_WARNING_PREFIX), out
    assert "looked in: " in first and "https://docs.astral.sh/uv/" in first
    assert "action: written" in out
    assert "launcher: uvx alice-memory mcp (cannot run here: uvx is not on PATH)" in out
    assert host_file_map(home.resolve())["cursor"]["mcp"].is_file()


def test_uvx_on_path_prints_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """uvx on PATH: no warning, and the receipt says uvx.

    Mutation: always print the warning. This test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    code, out = _install(capsys, tmp_path / "home", tmp_path / "vault")
    assert code == 0
    assert UVX_MISSING_WARNING_PREFIX not in out
    assert "launcher: uvx alice-memory mcp\n" in out
