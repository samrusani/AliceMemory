"""The limitations page and the protocol state the observed behaviour."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_idempotency_limitation_and_advice_are_documented():
    """Mutation: delete the idempotency bullet from known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert (
        "- idempotency keys are shared across the vault, and a caller who knows one can test a guess of a hidden write "
        "([remember](../memory-operations-protocol.md#remember)); use idempotency keys that cannot be guessed"
    ) in page
    protocol = (ROOT / "docs/memory-operations-protocol.md").read_text()
    assert "The key names a write across the whole vault, not one agent or project." in protocol


def test_expired_waiting_confirmation_limitation_is_documented():
    """Mutation: delete the expiring-confirmation bullet from known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert (
        "- expiring a write awaiting its own confirmation leaves that confirmation usable; "
        "accepting it makes the row active with an expiry in the past "
        "([confirm](../memory-operations-protocol.md#confirm-and-reject-rules))"
    ) in page
    protocol = " ".join((ROOT / "docs/memory-operations-protocol.md").read_text().split())
    assert "recall and the memory list in the context pack leave it out" in protocol
    assert "the pack's recent changes can still name its events" in protocol


def test_keyless_history_limitation_is_documented():
    """Mutation: remove the From v0.24.0 opener from the keyless_history bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- rows written before the first key keep their standing, and revoking the last key restores keyless briefs even for a presented revoked key. From v0.24.0, lessons written before the first key keep their standing too ([authentication](agent-integration.md#authentication))' in page


def test_retirement_standing_limitation_is_documented():
    """Mutation: remove the From v0.24.0 opener from the retirement_standing bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- From v0.24.0, retirement of a write waiting for review is limited to its decision makers, while its writer can retire a row waiting for their own reply ([standing](../memory-operations-protocol.md#standing-to-retire-a-memory))' in page


def test_agent_type_defaults_limitation_is_documented():
    """Mutation: delete the agent_type_defaults bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- the two built-in agent ids record different types through MCP and through a nested HTTP identity with no type ([identity](agent-integration.md#agent-identity-fields))' in page


def test_thin_audit_limitation_is_documented():
    """Mutation: delete the thin_audit bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- some audit records omit actors or keep the original writer, a hidden change leaves no event, and a stored agent row shows its last claimed profile ([audit](../memory-operations-protocol.md#audit-guarantees))' in page


def test_waiting_lessons_limitation_is_documented():
    """Mutation: remove the From v0.24.0 opener from the waiting_lessons bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- From v0.24.0, every key of a project can read its waiting and rejected lessons through the full review and explain tools; explain returns the decision reason ([lessons](shared-lessons.md))' in page


def test_lesson_writer_ids_limitation_is_documented():
    """Mutation: remove the From v0.24.0 opener from the lesson_writer_ids bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert "- From v0.24.0, a lesson's reader sees the writer and publishing agent ids ([lessons](shared-lessons.md)); choose neutral agent ids" in page


def test_lesson_cost_limitation_is_documented():
    """Mutation: remove the From v0.24.0 opener from the lesson_cost bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- From v0.24.0, `alice_lessons` reads a projection of every lesson the caller may read, so its cost grows with that number ([lessons](shared-lessons.md))' in page


def test_scoped_session_limitation_is_documented():
    """Mutation: delete the scoped_session bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert "- with project scoping on and a project detected in the default view, a key's brief and session hook return no brief ([projects](projects.md#the-project-brief))" in page


def test_unreadable_link_limitation_is_documented():
    """Mutation: delete the unreadable_link bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- `alice_explain` fails on a readable row linked to a source the caller cannot read ([cited sources](mcp-tools.md#cited-sources))' in page


def test_cli_policy_code_limitation_is_documented():
    """Mutation: delete the cli_policy_code bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- outside capture commands, a CLI policy refusal is reported as `filesystem_operation_failed` ([CLI](../integrations/cli.md#determinism-contract))' in page


def test_connector_routes_limitation_is_documented():
    """Mutation: delete the connector_routes bullet in known-limitations.md."""
    page = (ROOT / "docs/alpha/known-limitations.md").read_text()
    assert '- the dedicated Telegram and local-folder sync routes are shadowed and their request bodies get HTTP 422 ([connectors](mcp-tools.md#saved-quotes))' in page
