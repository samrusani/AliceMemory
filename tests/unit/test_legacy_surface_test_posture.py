from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_integration_runners_enable_legacy_surfaces_without_changing_unit_posture() -> None:
    workflow = _read(".github/workflows/tests.yml")
    integration_job = workflow.split("  python-integration:", 1)[1].split(
        "\n  web:", 1
    )[0]
    unit_job = workflow.split("  python-unit:", 1)[1].split(
        "\n  python-quality:", 1
    )[0]
    make_test_python = _read("Makefile").split("test-python:", 1)[1].split(
        "\n\ntest-web:", 1
    )[0]

    assert (
        "run: ALICE_LEGACY_SURFACES=1 ./.venv/bin/python -m pytest "
        "tests/integration -q -p no:cacheprovider --durations=20 --timeout=180"
    ) in integration_job
    assert (
        "ALICE_LEGACY_SURFACES=1 $(PYTHON) -m pytest tests/integration -q"
    ) in make_test_python

    assert "ALICE_LEGACY_SURFACES" not in unit_job
    unit_command = next(
        line
        for line in make_test_python.splitlines()
        if "-m pytest tests/unit" in line
    )
    assert "ALICE_LEGACY_SURFACES" not in unit_command


def test_postgres_matrix_has_a_required_flag_off_default_surface_row() -> None:
    workflow = _read(".github/workflows/tests.yml")
    integration_job = workflow.split("  python-integration:", 1)[1].split(
        "\n  web:", 1
    )[0]

    assert "name: ${{ matrix.integration_check }}" in integration_job
    assert (
        'integration_check: ["Integration tests (Postgres + pgvector, role separation)", '
        '"Default surface integration smoke (Postgres)"]'
    ) in integration_job
    assert (
        "if: matrix.integration_check == "
        "'Integration tests (Postgres + pgvector, role separation)'"
    ) in integration_job
    assert (
        "if: matrix.integration_check == "
        "'Default surface integration smoke (Postgres)'"
    ) in integration_job
    assert "CREATE ROLE alicebot_app LOGIN PASSWORD 'ci'" in integration_job
    assert "DATABASE_URL: postgresql://alicebot_app:ci@localhost:5432/alicebot" in integration_job
    assert "DATABASE_ADMIN_URL: postgresql://alicebot_admin:ci@localhost:5432/alicebot" in integration_job
    assert (
        "unset ALICE_LEGACY_SURFACES ALICE_MCP_LEGACY_TOOLS ALICE_AGENT_API_KEY"
    ) in integration_job
    assert (
        "./.venv/bin/python -m pytest \\\n"
        "            tests/integration/test_default_surface_integration.py \\\n"
        "            tests/integration/test_openai_agents_sdk_tool.py \\\n"
        "            -q -p no:cacheprovider --require-executed-tests"
    ) in integration_job
    assert "ALICE_LEGACY_SURFACES:" not in integration_job.split("    steps:", 1)[0]


def test_default_surface_smoke_fails_when_every_selected_test_is_skipped() -> None:
    env = os.environ.copy()
    env["ALICE_LEGACY_SURFACES"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_default_surface_integration.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--require-executed-tests",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == int(5), completed.stdout + completed.stderr
    assert "1 skipped" in completed.stdout
