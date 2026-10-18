#!/usr/bin/env python3
"""Install one built artifact outside the checkout and smoke public entrypoints."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
    timeout_seconds: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout_seconds,
    )


def _parse_json_lines(output: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def smoke_artifact(artifact: Path, *, expected_version: str) -> None:
    artifact = artifact.resolve()
    with tempfile.TemporaryDirectory(prefix="alice-artifact-smoke-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        venv_dir = temp_dir / "venv"
        virtualenv_env = os.environ.copy()
        virtualenv_env["VIRTUALENV_NO_PERIODIC_UPDATE"] = "1"
        subprocess.run(
            [sys.executable, "-m", "virtualenv", "--no-download", str(venv_dir)],
            cwd=temp_dir,
            env=virtualenv_env,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        bin_dir = venv_dir / "bin"
        python = bin_dir / "python"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"

        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(artifact),
            ],
            cwd=temp_dir,
            env=env,
            timeout_seconds=300,
        )

        for entrypoint in (
            "alice",
            "alicebot",
            "alice-memory",
            "alice-memory-session-start",
            "alicebot-mcp",
        ):
            _run([str(bin_dir / entrypoint), "--help"], cwd=temp_dir, env=env)
        for entrypoint, prefix in (
            ("alice", "alicebot"),
            ("alicebot", "alicebot"),
            ("alice-memory", "alice-memory"),
        ):
            completed = _run([str(bin_dir / entrypoint), "--version"], cwd=temp_dir, env=env)
            assert completed.stdout.strip() == f"{prefix} {expected_version}", completed.stdout

        probe = _run(
            [
                str(python),
                "-c",
                "\n".join(
                    (
                        "import json",
                        "import sys",
                        "from importlib.metadata import version",
                        "from pathlib import Path",
                        "from alembic.script import ScriptDirectory",
                        "import alicebot_api",
                        "import alicebot_api.cli as cli_module",
                        "import alicebot_api.cli.parser as cli_parser",
                        "import alicebot_api.cli.runner as cli_runner",
                        "import alicebot_api.mcp.registry as mcp_registry",
                        "from alicebot_api.main import app",
                        "from alicebot_api.migrations import make_alembic_config",
                        "from alicebot_api.public_evals import _load_fixture_catalog",
                        "cfg = make_alembic_config('postgresql://u:p@localhost/db')",
                        "script_dir = ScriptDirectory.from_config(cfg)",
                        "catalog = _load_fixture_catalog()",
                        "print(json.dumps({",
                        "  'version': version('alice-memory'),",
                        "  'api_version': app.version,",
                        "  'package_inside_venv': Path(alicebot_api.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'cli_package_inside_venv': Path(cli_module.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'cli_parser_inside_venv': Path(cli_parser.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'cli_runner_inside_venv': Path(cli_runner.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'cli_build_parser_inside_venv': Path(cli_module.build_parser.__code__.co_filename).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'cli_main_inside_venv': Path(cli_module.main.__code__.co_filename).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'cli_public_aliases_match': [cli_module.build_parser is cli_parser.build_parser, cli_module.main is cli_runner.main],",
                        "  'mcp_registry_inside_venv': Path(mcp_registry.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'mcp_handler_inside_venv': Path(mcp_registry._TOOL_HANDLERS['alice_recall'].__code__.co_filename).resolve().is_relative_to(Path(sys.prefix).resolve()),",
                        "  'mcp_registry_counts': [len(mcp_registry._CORE_TOOL_DEFINITIONS), len(mcp_registry._LEGACY_TOOL_DEFINITIONS), len(mcp_registry._TOOL_HANDLERS)],",
                        "  'alembic_head': script_dir.get_current_head(),",
                        "  'alembic_path_exists': Path(cfg.get_main_option('script_location')).is_dir(),",
                        "  'public_eval_schema': catalog.get('schema_version'),",
                        "}))",
                    )
                ),
            ],
            cwd=temp_dir,
            env=env,
        )
        payload = json.loads(probe.stdout)
        assert payload["version"] == expected_version
        assert payload["api_version"] == expected_version
        assert payload["package_inside_venv"] is True
        assert payload["cli_package_inside_venv"] is True
        assert payload["cli_parser_inside_venv"] is True
        assert payload["cli_runner_inside_venv"] is True
        assert payload["cli_build_parser_inside_venv"] is True
        assert payload["cli_main_inside_venv"] is True
        assert payload["cli_public_aliases_match"] == [True, True]
        assert payload["mcp_registry_inside_venv"] is True
        assert payload["mcp_handler_inside_venv"] is True
        assert payload["mcp_registry_counts"] == [12, 65, 77]
        assert payload["alembic_head"]
        assert payload["alembic_path_exists"] is True
        assert payload["public_eval_schema"] == "public_eval_fixture_v1"

        base_request_payloads = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        )
        sqlite_request_payloads = (
            *base_request_payloads,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "alice_memory_commit",
                        "arguments": {
                            "agent_id": "artifact-smoke",
                            "agent_type": "coding_agent",
                            "permission_profile": "trusted_local_agent",
                            "title": "Installed artifact smoke",
                            "canonical_text": "Installed Alice artifacts preserve a real SQLite memory flow.",
                            "domain": "professional",
                            "sensitivity": "internal",
                            "confidence": 0.96,
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "alice_recall",
                        "arguments": {"query": "installed artifact SQLite memory flow"},
                    },
                }
            ),
        )
        requests = "\n".join((*base_request_payloads, ""))
        sqlite_requests = "\n".join((*sqlite_request_payloads, ""))
        mcp = _run(
            [str(bin_dir / "alicebot-mcp"), "--database-url", "sqlite:///:memory:"],
            cwd=temp_dir,
            env=env,
            input_text=requests,
        )
        mcp_responses = _parse_json_lines(mcp.stdout)
        assert mcp_responses[0]["result"]["serverInfo"]["version"] == expected_version
        assert [tool["name"] for tool in mcp_responses[1]["result"]["tools"]] == [
            "alice_memory_commit",
            "alice_recall",
            "alice_resume",
        ]

        sqlite_mcp = _run(
            [str(bin_dir / "alice-memory"), "mcp", "--db", str(temp_dir / "memory.db")],
            cwd=temp_dir,
            env=env,
            input_text=sqlite_requests,
        )
        sqlite_responses = _parse_json_lines(sqlite_mcp.stdout)
        assert sqlite_responses[0]["result"]["serverInfo"]["version"] == expected_version
        assert [tool["name"] for tool in sqlite_responses[1]["result"]["tools"]] == [
            "alice_memory_commit",
            "alice_recall",
            "alice_resume",
        ]
        commit_payload = json.loads(sqlite_responses[2]["result"]["content"][0]["text"])
        recall_payload = json.loads(sqlite_responses[3]["result"]["content"][0]["text"])
        assert commit_payload["status"] == "committed"
        memory_id = commit_payload["memory"]["id"]
        assert any(result["id"] == memory_id for result in recall_payload["results"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="+")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    for artifact in args.artifacts:
        smoke_artifact(artifact, expected_version=args.expected_version)
        print(f"Artifact smoke: PASS ({artifact.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
