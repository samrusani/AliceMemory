from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
QUICKSTART_SCRIPT = REPO_ROOT / "docs" / "examples" / "mcp_quickstart.py"


def test_mcp_quickstart_runs_against_packaged_sqlite_server() -> None:
    """Mutation: remove alice_lessons from the quickstart default order."""
    completed = subprocess.run(
        [sys.executable, str(QUICKSTART_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert "MCP QUICKSTART OK" in completed.stdout
    assert "tools: 4 (default surface verified)" in completed.stdout
    assert "results contain the canary phrase" in completed.stdout


def test_mcp_quickstart_is_stdlib_only_and_offline() -> None:
    source = QUICKSTART_SCRIPT.read_text(encoding="utf-8")

    # No third-party imports, no HTTP, no Postgres: the quickstart must
    # stay runnable with a bare interpreter plus the installed package.
    assert "import requests" not in source
    assert "import httpx" not in source
    assert "import mcp" not in source
    assert "psycopg" not in source
    assert "DATABASE_URL" not in source
    assert "alicebot_api.onramp" in source
