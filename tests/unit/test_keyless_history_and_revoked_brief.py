"""Keyless history stays labelled and the last revocation restores keyless briefs."""

import json
import os
import subprocess
import sys

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


def test_keyless_lesson_and_revoked_key_session_briefs(tmp_path, monkeypatch):
    """The limitations page changes if the last revocation no longer makes briefs keyless.

    Mutation: make ``session_brief_identity`` return quiet when no active keys remain.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    vault = Vault(tmp_path / "memory.db")
    created = vault.commit(monkeypatch, None, "old-agent", permission_profile="admin_agent", project_scope=["alpha"])
    assert created["status"] == "committed"
    key = vault.key("reader", "read_only_agent", "alpha")
    lessons = vault.call(monkeypatch, key, "alice_lessons", {"task": "rebasing"})
    assert lessons["lessons"][0]["writer"] == {"id": "old-agent", "established": "declared_on_keyless_install"}
    with sqlite_user_connection(vault.path, vault.user) as connection:
        store = SQLiteVNextStore(connection, vault.user)
        record = store.list_agent_api_keys()[0]
        store.revoke_agent_api_key(key_id=str(record["id"]))
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    failed, answer = wire(context, "alice_lessons", {"task": "rebasing"})
    assert failed and answer["error"]["code"] == "not_permitted"
    env = {**os.environ, "ALICE_AGENT_API_KEY": key, "ALICE_PROJECT_DIR": str(tmp_path)}
    env.pop("ALICE_PROJECT_SCOPING", None)
    brief = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "brief", "--db", str(vault.path), "--user-id", str(vault.user)],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert brief.returncode == 0 and "Shared lessons: 1" in brief.stdout
    hook = subprocess.run(
        [sys.executable, "-m", "alicebot_api.session_start_hook", "--data-dir", str(tmp_path), "--user-id", str(vault.user)],
        input="{}", capture_output=True, text=True, env=env,
    )
    assert hook.returncode == 0 and "Shared lessons: 1" in json.loads(hook.stdout)["additional_context"]


def test_a_memory_written_before_any_key_keeps_a_declared_writer(tmp_path, monkeypatch):
    """The limitations page changes if a pre-key write becomes a verified-key write.

    Mutation: replace the declared-on-keyless writer label with verified-by-key.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    vault = Vault(tmp_path / "memory.db")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    failed, created = wire(context, "alice_memory_commit", {
        "agent_id": "old-agent", "permission_profile": "admin_agent", "project_scope": ["alpha"],
        "title": "Synthetic", "canonical_text": "An amber keyless memory", "domain": "project", "sensitivity": "internal",
    })
    assert not failed and created["status"] == "committed"
    key = vault.key("reader", "read_only_agent", "alpha")
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    failed, result = wire(context, "alice_recall", {"query": "amber keyless memory"})
    assert not failed
    [memory] = [item for item in result["results"] if item["id"] == created["memory"]["id"]]
    assert memory["writer"] == {"id": "old-agent", "established": "declared_on_keyless_install"}
