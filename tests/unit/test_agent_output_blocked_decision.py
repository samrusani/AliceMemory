"""``VNextConnectorService.ingest_agent_output`` refuses a policy decision that is ``blocked``, before it writes anything.

The HTTP route, the MCP tool and the command line each ask the agent policy first and stop on a blocked decision. The
route once handed its blocked decision to the service, which recorded it in metadata and wrote a source and a
``needs_review`` artifact anyway. The service now holds the same line, so a caller that forgets the check gets the
refusal and no rows.

Mutations, each one alone: the call of ``_refuse_blocked_policy_decision`` taken out of ``ingest_agent_output`` (every
test of the refusal fails); the call moved below ``normalize_connector_item`` (the unreadable payload test fails); the
comparison with ``"blocked"`` changed to compare with another word (the refusal tests fail); the rebuilt decision made to
drop its reasons (the round trip test fails).
"""

from __future__ import annotations

import pytest

from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError, evaluate_agent_policy
from alicebot_api.vnext_connectors import VNextConnectorService, VNextConnectorValidationError
from tests.unit.test_vnext_connectors import InMemoryVNextConnectorStore

PAYLOAD = {
    "agent_id": "probe-agent",
    "agent_type": "coding_agent",
    "project_scope": ["Alpha"],
    "title": "Kiln schedule note",
    "content": "Decision: the kiln schedule moves to Tuesday.",
    "domain": "project",
    "sensitivity": "private",
}


def _blocked_record() -> dict[str, object]:
    """A blocked decision from the policy engine itself, not written by hand."""

    identity = AgentIdentity(agent_id="probe-agent", agent_type="coding_agent", permission_profile="read_only_agent")
    decision = evaluate_agent_policy(
        identity=identity,
        action="source.capture",
        domains=("project",),
        sensitivity_allowed=("private",),
        project_scope=("Alpha",),
    )
    assert decision.decision == "blocked"
    return decision.to_record()


def _written(store: InMemoryVNextConnectorStore) -> dict[str, int]:
    return {
        "events": len(store.events),
        "sources": len(store.sources),
        "chunks": len(store.chunks),
        "memories": len(store.memories),
        "artifacts": len(store.artifacts),
        "provenance_links": len(store.provenance_links),
    }


@pytest.mark.parametrize("propose_memory", [False, True], ids=["artifact only", "propose memory"])
def test_a_blocked_decision_is_refused_and_nothing_is_written(propose_memory: bool) -> None:
    store = InMemoryVNextConnectorStore()
    record = _blocked_record()

    with pytest.raises(AgentPolicyBlockedError) as refused:
        VNextConnectorService(store).ingest_agent_output({**PAYLOAD, "propose_memory": propose_memory}, policy_decision=record)

    assert _written(store) == {"events": 0, "sources": 0, "chunks": 0, "memories": 0, "artifacts": 0, "provenance_links": 0}
    # The refusal carries the decision it was given, field for field, so a route can answer it as the other routes do.
    assert refused.value.decision.to_record() == record
    assert "read_only_agent_cannot_write" in refused.value.decision.reasons


def test_a_blocked_decision_is_refused_before_the_payload_is_read() -> None:
    """The refusal comes first: a refused caller learns nothing about whether its payload would have been accepted."""

    store = InMemoryVNextConnectorStore()

    with pytest.raises(AgentPolicyBlockedError):
        VNextConnectorService(store).ingest_agent_output({}, policy_decision=_blocked_record())
    with pytest.raises(VNextConnectorValidationError):
        VNextConnectorService(store).ingest_agent_output({}, policy_decision={"decision": "allowed"})

    assert _written(store)["sources"] == 0


def test_a_record_that_says_only_blocked_is_still_refused() -> None:
    store = InMemoryVNextConnectorStore()

    with pytest.raises(AgentPolicyBlockedError) as refused:
        VNextConnectorService(store).ingest_agent_output(PAYLOAD, policy_decision={"decision": "blocked"})

    assert refused.value.decision.decision == "blocked"
    assert refused.value.decision.action == "source.capture"
    assert _written(store)["sources"] == 0


@pytest.mark.parametrize(
    "decision",
    [
        pytest.param(None, id="no decision"),
        pytest.param({"decision": "allowed", "action": "source.capture"}, id="allowed"),
        pytest.param({"decision": "allowed_with_filtering", "action": "source.capture"}, id="allowed with filtering"),
        pytest.param({"decision": "requires_review", "action": "source.capture"}, id="requires review"),
        pytest.param({}, id="a record with no decision"),
    ],
)
def test_a_decision_that_does_not_block_goes_on_and_writes_the_source_and_the_artifact(decision: dict[str, object] | None) -> None:
    store = InMemoryVNextConnectorStore()

    result = VNextConnectorService(store).ingest_agent_output({**PAYLOAD, "propose_memory": True}, policy_decision=decision)

    assert result.source_id and result.artifact_id and result.memory_id
    written = _written(store)
    assert (written["sources"], written["artifacts"]) == (1, 1)
    assert store.memories[-1]["id"] == result.memory_id
    assert store.artifacts[0]["status"] == "needs_review"
