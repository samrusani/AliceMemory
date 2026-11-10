"""Compare existing MCP reads with main on identical synthetic SQLite vaults."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

from pytest import MonkeyPatch
from tests.unit.test_lessons_read import Vault

from benchmark_lessons_read import ROOT, launch


def normalize(value):
    if isinstance(value, dict):
        return {
            key: normalize(item)
            for key, item in value.items()
            if key not in {"generated_at", "context_pack_id", "trace_id"}
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def main():
    with tempfile.TemporaryDirectory(prefix="lesson-parity-") as folder:
        base = Path(folder)
        main_source = base / "main"
        main_source.mkdir()
        archive = base / "main.tar"
        with archive.open("wb") as output:
            subprocess.run(["git", "archive", "origin/main"], cwd=ROOT, stdout=output, check=True)
        with tarfile.open(archive) as source:
            source.extractall(main_source, filter="data")
        comparisons = 0
        for profile in ("owner", "admin_agent", "project_scoped_agent", "unbound_admin_agent"):
            patch = MonkeyPatch()
            try:
                patch.setenv("ALICE_PROJECT_DIR", folder)
                vault = Vault(base / f"{profile}.db")
                if profile == "owner":
                    token, identity = None, {}
                    vault.commit(patch, None)
                else:
                    publisher = vault.key("publisher", "admin_agent", "alpha")
                    bound = None if profile == "unbound_admin_agent" else "alpha"
                    reader_profile = "admin_agent" if profile == "unbound_admin_agent" else profile
                    token = vault.key("reader", reader_profile, bound)
                    identity = {"agent_id": "reader"}
                    vault.commit(patch, publisher, "publisher")
                calls = (
                    ("alice_recall", {"query": "rebasing feature branch", "include_sources": False}),
                    ("alice_resume", {}),
                    ("alice_context_pack", {"query": "rebasing feature branch"}),
                )
                for number, (tool, args) in enumerate(calls):
                    answers = []
                    for label, source_path in (("main", main_source), ("head", ROOT)):
                        duplicate = base / f"{profile}-{number}-{label}.db"
                        with sqlite3.connect(vault.path) as source, sqlite3.connect(duplicate) as destination:
                            source.backup(destination)
                        env = dict(
                            os.environ,
                            PYTHONPATH=str(source_path / "apps/api/src"),
                            ALICE_MCP_FULL_TOOLS="1",
                            ALICE_PROJECT_DIR=folder,
                        )
                        for name in (
                            "ALICE_AGENT_API_KEY",
                            "ALICE_EMBEDDINGS_BASE_URL",
                            "ALICE_EMBEDDINGS_MODEL",
                            "ALICE_EMBEDDINGS_API_KEY",
                        ):
                            env.pop(name, None)
                        if token:
                            env["ALICE_AGENT_API_KEY"] = token
                        process, client = launch(duplicate, env, vault.user)
                        try:
                            answers.append(normalize(client.call_tool(tool, {**args, **identity})))
                        finally:
                            assert process.stdin is not None
                            process.stdin.close()
                            process.wait(timeout=20)
                    if answers[0] != answers[1]:
                        print(json.dumps({"profile": profile, "tool": tool, "main": answers[0], "head": answers[1]}))
                        raise SystemExit(1)
                    print(f"{profile} {tool}: equal", flush=True)
                    comparisons += 1
            finally:
                patch.undo()
        print(f"{comparisons} comparisons passed; generated_at, context_pack_id and trace_id excluded")


if __name__ == "__main__":
    main()
