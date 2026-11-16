"""Full-surface review and explain expose a project's waiting lesson rows."""

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from tests.support.lesson_visibility_contract import check_lesson_publisher_ids, check_lesson_visibility
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


def test_a_read_only_project_key_reads_waiting_and_rejected_lessons(tmp_path, monkeypatch):
    """The limitations page changes if review and explain no longer return these rows.

    Mutation: make review's ``all`` status select only ``active`` rows.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    reviewer = vault.key("reviewer", "admin_agent", "alpha")
    readers = [vault.key("reader-" + profile, profile, "alpha") for profile in PERMISSION_PROFILES]
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    check_lesson_visibility(call, writer, reviewer, readers)


def test_lesson_reader_sees_writer_and_reviewer_ids(tmp_path, monkeypatch):
    """The limitations page changes if lesson publication stops returning the reviewer's id.

    Mutation: replace the publishing revision's agent actor id with ``owner``.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    reviewer = vault.key("reviewer", "admin_agent", "alpha")
    reader = vault.key("reader", "read_only_agent", "alpha")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    check_lesson_publisher_ids(call, writer, reviewer, reader)
