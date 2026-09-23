"""When uv is installed and uvx is not on PATH, install writes an absolute uvx.

uv exports UV to child processes as the path of the uv binary. On Linux that
path is the symlink target, so Homebrew's uv reports
``<prefix>/Cellar/uv/<version>/bin/uv``. That directory is removed on
upgrade. Install writes ``<prefix>/bin/uvx`` when that file is executable,
and otherwise the name uvx. A path inside a uv cache is not written.
Scripts outside a uv cache still win. A relative UV value is ignored.

No binary is run. The uv and uvx files are empty 0755 stand-ins.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api import host_launcher
from alicebot_api.host_install import host_file_map
from alicebot_api.host_launcher import (
    UVX_MISSING_WARNING_PREFIX,
    UVX_OFF_PATH_WARNING,
    UV_TEMP_ENV_WARNING,
)
from alicebot_api.onramp import main as onramp_main
from tests.unit.launcher_helpers import executable, make_scripts, pin_launcher_search


def _temp_prefix(tmp_path: Path) -> str:
    return str(tmp_path / "run" / "uv" / "archive-v0" / "P")


def _pair(directory: Path) -> tuple[Path, Path]:
    return executable(directory / "uv"), executable(directory / "uvx")


def _install(capsys, home: Path, vault: Path) -> tuple[int, str]:
    code = onramp_main(
        ["install", "--home", str(home), "--host", "claude-code", "--data-dir", str(vault)]
    )
    return code, capsys.readouterr().out


def _written(home: Path) -> tuple[str, str, str]:
    files = host_file_map(home.resolve())["claude-code"]
    doc = json.loads(files["mcp"].read_text(encoding="utf-8"))
    command = doc["mcpServers"]["alice"]["command"]
    hook_doc = json.loads(files["hooks"].read_text(encoding="utf-8"))
    hook = hook_doc["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    return command, hook, files["mcp"].read_text(encoding="utf-8") + files["hooks"].read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["uv-env", "uv-on-path", "uvx-env"])
def test_install_writes_an_absolute_uvx_when_uvx_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, source: str
) -> None:
    """The host entry and the SessionStart hook run that absolute uvx.

    The prefix is inside a uv cache, which is how ``uvx alice-memory install``
    runs, so the bare name used to be written. Mutation: make
    ``absolute_uvx`` return None. This test fails.
    """

    uv_bin, uvx_bin = _pair(tmp_path / "tools" / "bin")
    pointed = uvx_bin if source == "uvx-env" else uv_bin
    pin_launcher_search(
        monkeypatch,
        tmp_path,
        uvx=None,
        prefix=_temp_prefix(tmp_path),
        uv=str(uv_bin) if source == "uv-on-path" else None,
        uv_env=str(pointed) if source != "uv-on-path" else None,
    )
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out = _install(capsys, home, vault)
    assert code == 0, out
    command, hook, _text = _written(home)
    assert command == str(uvx_bin)
    assert hook == f"{uvx_bin} --from alice-memory alice-memory-session-start --data-dir {vault.resolve()}"
    assert UV_TEMP_ENV_WARNING not in out
    assert UVX_OFF_PATH_WARNING not in out
    assert f"launcher: {uvx_bin} alice-memory mcp\n" in out


def test_a_versioned_cellar_bin_maps_to_the_stable_prefix_bin() -> None:
    """The layout check itself, including a Windows uv.exe path.

    ``/opt/homebrew/bin/uv`` is already stable, so it is not rewritten.
    """

    assert (
        host_launcher._cellar_stable_uvx("/opt/homebrew/Cellar/uv/0.11.6/bin/uv")
        == "/opt/homebrew/bin/uvx"
    )
    assert (
        host_launcher._cellar_stable_uvx("/opt/homebrew/Cellar/uv/0.11.6/bin/uvx")
        == "/opt/homebrew/bin/uvx"
    )
    assert host_launcher._cellar_stable_uvx("/opt/homebrew/bin/uv") is None
    assert (
        host_launcher._cellar_stable_uvx(r"C:\prefix\Cellar\uv\0.11.6\bin\uv.exe")
        == r"C:\prefix\bin\uvx.exe"
    )


def test_install_writes_the_stable_bin_instead_of_a_cellar_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """UV is the Cellar uv. The files name ``<prefix>/bin/uvx``.

    The stable path is a symlink to the Cellar uvx. Install writes the
    symlink path. Mutation: in ``_uvx_beside``, return the uvx beside the
    Cellar path instead of ``<prefix>/bin/uvx``. This test fails.
    """

    root = tmp_path / "opt" / "homebrew"
    _uv_bin, cellar_uvx = _pair(root / "Cellar" / "uv" / "0.11.6" / "bin")
    stable = root / "bin" / "uvx"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(cellar_uvx)
    pin_launcher_search(
        monkeypatch,
        tmp_path,
        uvx=None,
        prefix=_temp_prefix(tmp_path),
        uv_env=str(root / "Cellar" / "uv" / "0.11.6" / "bin" / "uv"),
    )
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out = _install(capsys, home, vault)
    assert code == 0, out
    command, hook, text = _written(home)
    assert command == str(stable)
    assert hook.startswith(f"{stable} --from alice-memory ")
    assert "Cellar" not in text
    assert "Cellar" not in out


def test_a_cellar_uvx_is_not_written_when_the_stable_bin_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """No ``<prefix>/bin/uvx``: the name uvx is written, and the Cellar path is not.

    Mutation: in ``_uvx_beside``, return the uvx beside the Cellar path
    instead of ``<prefix>/bin/uvx``. This test fails.
    """

    root = tmp_path / "opt" / "homebrew"
    uv_bin, _cellar_uvx = _pair(root / "Cellar" / "uv" / "0.11.6" / "bin")
    pin_launcher_search(
        monkeypatch,
        tmp_path,
        uvx=None,
        prefix=_temp_prefix(tmp_path),
        uv_env=str(uv_bin),
    )
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out = _install(capsys, home, vault)
    assert code == 0, out
    command, hook, text = _written(home)
    assert command == "uvx"
    assert hook == f"uvx --from alice-memory alice-memory-session-start --data-dir {vault.resolve()}"
    assert "Cellar" not in text
    assert "Cellar" not in out
    assert out.startswith(UV_TEMP_ENV_WARNING)


def test_a_uvx_inside_a_uv_cache_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """UV points at a uv inside a uv cache. The entry runs uvx by name.

    Mutation: drop the ``_cached`` check in ``_writable_uvx``. This test fails.
    """

    uv_bin, cached_uvx = _pair(tmp_path / "cache" / "uv" / "archive-v0" / "Id" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, uv_env=str(uv_bin))
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out = _install(capsys, home, vault)
    assert code == 0, out
    command, _hook, text = _written(home)
    assert command == "uvx"
    assert str(cached_uvx) not in text
    assert "archive-v0" not in text
    assert out.startswith(UVX_OFF_PATH_WARNING)


def test_scripts_outside_a_cache_still_win_over_an_absolute_uvx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A pip install's scripts are the launcher when uvx is not on PATH.

    Mutation: offer the absolute uvx before the script search in
    ``find_launcher``. This test fails.
    """

    bin_dir = make_scripts(tmp_path / "venv" / "bin")
    uv_bin, uvx_bin = _pair(tmp_path / "tools" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=bin_dir, uv_env=str(uv_bin))
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out = _install(capsys, home, vault)
    assert code == 0, out
    command, hook, text = _written(home)
    script = str(bin_dir / "alice-memory")
    assert command == script
    assert hook == f"{bin_dir / 'alice-memory-session-start'} --data-dir {vault.resolve()}"
    assert str(uvx_bin) not in text
    assert str(uvx_bin) not in out


def test_a_relative_uv_value_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``UV=bin/uv`` is not a path to write, even when that file exists here.

    Mutation: drop the absolute-path check in ``_writable_uvx``. This test fails.
    """

    executable(tmp_path / "bin" / "uv")
    executable(tmp_path / "bin" / "uvx")
    monkeypatch.chdir(tmp_path)
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, uv_env="bin/uv")
    home, vault = tmp_path / "home", tmp_path / "vault"
    code, out = _install(capsys, home, vault)
    assert code == 0, out
    command, _hook, text = _written(home)
    assert command == "uvx"
    assert "bin/uvx" not in text
    assert out.startswith(UVX_MISSING_WARNING_PREFIX)
