"""alice-memory install warns when uvx is missing; it does not refuse.

Why this exists. Until 2026-09-22 the README said the packaged path needed
"Python 3.12+ and nothing else", while every host entry and hook that
install writes starts Alice with uvx. A user with Python and pip but no uv
got a clean exit 0 and hosts that could not start Alice. Nothing checked
for uvx, so nothing said so.

The warning is advice only: the files are still right once uv is
installed, so the exit code does not change.
"""

from __future__ import annotations

from pathlib import Path

from alicebot_api import host_install
from alicebot_api.host_install import UVX_MISSING_WARNING, host_file_map
from alicebot_api.onramp import main as onramp_main


def _install(capsys, home: Path, vault: Path) -> tuple[int, str]:
    code = onramp_main(["install", "--home", str(home), "--host", "cursor", "--data-dir", str(vault)])
    return code, capsys.readouterr().out


def test_missing_uvx_prints_a_warning_and_keeps_the_exit_code(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """No uvx on PATH: the warning comes first, the files are written, exit 0.

    Mutation: drop the warning, or refuse the host. This test fails.
    """

    monkeypatch.setattr(host_install, "_uvx_on_path", lambda: False)
    home = tmp_path / "home"
    code, out = _install(capsys, home, tmp_path / "vault")
    assert code == 0
    assert out.startswith(UVX_MISSING_WARNING + "\n\n"), out
    assert "https://docs.astral.sh/uv/" in UVX_MISSING_WARNING
    assert "action: written" in out
    assert host_file_map(home.resolve())["cursor"]["mcp"].is_file()


def test_uvx_on_path_prints_no_warning(tmp_path: Path, monkeypatch, capsys) -> None:
    """uvx on PATH: no warning.

    Mutation: always print the warning. This test fails.
    """

    monkeypatch.setattr(host_install, "_uvx_on_path", lambda: True)
    code, out = _install(capsys, tmp_path / "home", tmp_path / "vault")
    assert code == 0
    assert UVX_MISSING_WARNING not in out
