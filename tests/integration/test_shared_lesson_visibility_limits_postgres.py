"""Full-surface MCP review and explain expose a project's waiting lesson rows."""

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.vnext_agent_control import PERMISSION_PROFILES
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.support.lesson_visibility_contract import check_lesson_publisher_ids, check_lesson_visibility
from tests.unit.test_identity_claim_codes import wire


def test_postgres_read_only_project_key_reads_waiting_and_rejected_lessons(label_harness, monkeypatch):
    """The limitations page changes if review and explain no longer return these rows.

    Mutation: make review's ``all`` status select only ``active`` rows.
    """
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    writer = h.key("project_scoped_agent", project="alpha")
    reviewer = h.key("admin_agent", project="alpha")
    readers = [h.key(profile, project="alpha") for profile in PERMISSION_PROFILES]
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    check_lesson_visibility(call, writer, reviewer, readers)


def test_postgres_lesson_reader_sees_writer_and_reviewer_ids(label_harness, monkeypatch):
    """The limitations page changes if lesson publication stops returning the reviewer's id.

    Mutation: replace the publishing revision's agent actor id with ``owner``.
    """
    h = label_harness
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    with h.store() as store:
        writer = create_agent_key(store, user_id=h.user_id, agent_id="writer", permission_profile="project_scoped_agent", project_scope="alpha")[1]
        reviewer = create_agent_key(store, user_id=h.user_id, agent_id="reviewer", permission_profile="admin_agent", project_scope="alpha")[1]
        reader = create_agent_key(store, user_id=h.user_id, agent_id="reader", permission_profile="read_only_agent", project_scope="alpha")[1]
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    check_lesson_publisher_ids(call, writer, reviewer, reader)
