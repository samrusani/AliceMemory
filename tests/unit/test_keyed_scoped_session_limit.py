"""A detected project and a keyed caller leave the local session brief empty."""

import json
import os
import subprocess
import sys

import pytest

from alicebot_api.project_identity import detect_project
from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from tests.unit.per_project_s2_support import repo_with_remote
from tests.unit.test_lessons_read import Vault


@pytest.mark.parametrize("profile", PERMISSION_PROFILES)
@pytest.mark.parametrize("bound", [False, True])
def test_keyed_session_brief_and_hook_with_a_detected_project(tmp_path, monkeypatch, profile, bound):
    """The limitations page changes if the detected-project failure is removed.

    Mutation: skip the project-view and effective-scope equality check in session_briefing.py.
    """
    repo = repo_with_remote(tmp_path / "repo", "https://example.com/synthetic/alpha.git")
    project = detect_project(argument=str(repo)).context.ids[0]
    vault = Vault(tmp_path / "memory.db")
    admin = vault.key("admin", "admin_agent", project)
    reader = vault.key("reader", profile, project if bound else None)
    assert vault.commit(monkeypatch, admin, "admin")["status"] == "committed"
    env = {name: value for name, value in os.environ.items() if not name.startswith("ALICE")}
    env.update(ALICE_AGENT_API_KEY=reader, ALICE_PROJECT_DIR=str(repo), ALICE_PROJECT_SCOPING="on")
    brief = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(vault.path), "--user-id", str(vault.user)],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert brief.returncode == 1 and brief.stdout == ""
    hook = subprocess.run(
        [sys.executable, "-m", "alicebot_api.session_start_hook", "--data-dir", str(tmp_path), "--user-id", str(vault.user)],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert hook.returncode == 0 and json.loads(hook.stdout) == {}
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    assert vault.call(monkeypatch, reader, "alice_lessons", {"task": "rebasing"})["count"] == 1
    # Keep the project search within the fixture even when it is inside a checkout.
    no_project_dir = tmp_path.joinpath(*(["empty"] * 33))
    no_project_dir.mkdir(parents=True)
    env["ALICE_PROJECT_DIR"] = str(no_project_dir)
    no_project = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(vault.path), "--user-id", str(vault.user)],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert no_project.returncode == 0 and "Shared lessons: 1" in no_project.stdout
    env["ALICE_PROJECT_DIR"] = str(repo)
    env.pop("ALICE_PROJECT_SCOPING")
    brief = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(vault.path), "--user-id", str(vault.user)],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert brief.returncode == 0 and "Shared lessons: 1" in brief.stdout
