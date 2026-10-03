from __future__ import annotations

import shutil

import pytest

from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV


@pytest.fixture(autouse=True)
def enable_full_mcp_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep existing full-surface tests on the eleven core tools.

    Tests that assert the default handshake must delete this env.
    """
    monkeypatch.setenv(MCP_FULL_TOOLS_ENV, "1")


@pytest.fixture
def uvx_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``uvx`` look present on PATH, whatever this machine has.

    Since S4.6 (2026-09-23), install writes the absolute path of the
    installed alice-memory script when uvx is missing, so tests that assert
    uvx entries pin this. Every other name still goes to the real lookup,
    so a test can still find a real binary.
    """

    real_which = shutil.which

    def which(name: str, *args: object, **kwargs: object) -> str | None:
        if name == "uvx":
            return "/usr/local/bin/uvx"
        return real_which(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "which", which)
