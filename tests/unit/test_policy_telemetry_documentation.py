"""The operator access sentence stays beside the policy telemetry route."""

from pathlib import Path


def test_policy_telemetry_operator_access_is_documented() -> None:
    """The route's access rule must stay on the page.

    Mutation: delete the policy-telemetry access sentence from local-runtime.md.
    """
    page = (Path(__file__).resolve().parents[2] / "docs/vnext/local-runtime.md").read_text()
    assert (
        "`GET /v0/vnext/agents/policy-telemetry`: the owner on an install without active keys "
        "and an unbound trusted or admin key may call it; every other key is refused, "
        "and a call without a key is refused while an active key exists "
        "([operator access](../alpha/mcp-tools.md#saved-quotes))."
    ) in page


def test_memory_audit_operator_access_is_documented() -> None:
    """Mutation: delete the HTTP audit operator access sentence from memory-operations-protocol.md."""
    page = (Path(__file__).resolve().parents[2] / "docs/memory-operations-protocol.md").read_text()
    assert (
        "The HTTP audit is an operator route: the owner on an install without active keys "
        "and an unbound trusted or admin key may call it; every other key is refused, "
        "and a call without a key is refused while an active key exists "
        "([operator access](alpha/mcp-tools.md#saved-quotes))."
    ) in page


def test_connector_status_operator_access_is_documented() -> None:
    """Mutation: delete the connector status access sentence from mcp-tools.md."""
    page = (Path(__file__).resolve().parents[2] / "docs/alpha/mcp-tools.md").read_text()
    assert (
        "The connector status route is an operator route: the owner on an install without active keys "
        "and an unbound trusted or admin key may call it; every other key is refused, "
        "and a call without a key is refused while an active key exists "
        "([operator access](#saved-quotes))."
    ) in page


def test_context_tree_operator_access_is_documented() -> None:
    """Mutation: delete the context-tree access sentence from mcp-tools.md."""
    page = (Path(__file__).resolve().parents[2] / "docs/alpha/mcp-tools.md").read_text()
    assert (
        "The context-tree route is an operator route: the owner on an install without active keys "
        "and an unbound trusted or admin key may call it; every other key is refused, "
        "and a call without a key is refused while an active key exists "
        "([operator access](#saved-quotes))."
    ) in page
