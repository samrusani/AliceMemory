"""A pending `alice_memory_commit` must be finishable on the default three tools.

Wiki D8, audit 2026-09-03 finding 13. Written 2026-09-22.

`alice_memory_commit` returns `confirmation_required` for a write below 0.85
confidence, in a sensitive domain, or above `private`. The tool description told
the agent to finish with `alice_memory_manage`. The default registry refuses that
tool unless `ALICE_MCP_FULL_TOOLS=1`, and `alice-memory install` never sets it. The
row sat in `needs_review`, invisible to recall and resume, with nothing on the
default tools able to finish it. (Its 24 hour `expires_at` is read only by
`VNextMemoryCommitService.confirm`; nothing expires it in the background.) The
Hermes skill's own ambient example commits at 0.84.

Reproduced on the unmodified code through `call_mcp_tool`, default surface,
real SQLite store:

    advertised tools: ['alice_memory_commit', 'alice_recall', 'alice_resume']
    commit status: confirmation_required | reasons: ['medium_confidence_requires_confirmation']
    manage refused: MCPToolNotFoundError tool 'alice_memory_manage' is part of the
        full MCP surface and is currently disabled; set ALICE_MCP_FULL_TOOLS=1 ...
    recall result count: 0
    commit with confirmation_id refused: MCPToolError tool 'alice_memory_commit'
        does not accept additional properties: confirmation_id

How it escaped: `tests/conftest.py` turns `ALICE_MCP_FULL_TOOLS=1` on for every
test. Every confirm test ran on the eleven-tool surface, so none of them walked
the handshake an installed agent actually gets.

The fix, decided by the owner on 2026-09-04: finish the write on the same verb.
`alice_memory_commit` takes `confirmation_id` plus an explicit
`confirmation_action` of `confirm` or `reject`. It confirms through
`VNextMemoryCommitService.confirm`, the function `alice_memory_manage` calls.
A new route to an existing write is the defect class this repo keeps shipping,
so each control the old route applies is tested here through the new one:
identity, the policy check, the project fence (a key-bound scope; a keyless
server trusts the declared scope), and the audit trail. The route is also
stricter than manage in one place. Manage turns a row above the caller's
sensitivity ceiling into `allowed_with_filtering` and confirms it anyway
(finding 8). This route refuses that confirm. After review the same day it
allows a reject there, which stores nothing, lowers exposure and lets a keyed
agent clear its own above-ceiling write. The reject response still echoes the
pending row, including the text the writer sent, as manage and the HTTP
confirm route do. The reject still passes identity and the key-bound fence.

Every test here deletes `ALICE_MCP_FULL_TOOLS`, and the first one proves it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

USER_ID = "00000000-0000-0000-0000-000000000001"
REPO_ROOT = Path(__file__).resolve().parents[2]

PENDING_TITLE = "Billing deploy day"
PENDING_TEXT = "The team deploys the billing service on Thursdays."
QUERY = "billing service deploys Thursdays"


@pytest.fixture
def default_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    from alicebot_api.mcp_tools import (
        AGENT_API_KEY_ENV,
        LEGACY_SURFACES_ENV,
        MCP_FULL_TOOLS_ENV,
        MCP_LEGACY_TOOLS_ENV,
    )
    from alicebot_api.vnext_embeddings import (
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
    )

    for name in (
        MCP_FULL_TOOLS_ENV,
        MCP_LEGACY_TOOLS_ENV,
        LEGACY_SURFACES_ENV,
        AGENT_API_KEY_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _context(tmp_path: Path):
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _call(context, name: str, **arguments) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name=name, arguments=arguments)


def _commit_pending(context, **overrides) -> dict:
    arguments = {"title": PENDING_TITLE, "canonical_text": PENDING_TEXT, "confidence": 0.7, **overrides}
    payload = _call(context, "alice_memory_commit", **arguments)
    assert payload["status"] == "confirmation_required", payload
    assert payload["memory"]["status"] == "needs_review", payload
    return payload


def _recalled_ids(context, query: str = QUERY, **arguments) -> set[str]:
    payload = _call(context, "alice_recall", query=query, **arguments)
    return {str(row["id"]) for row in payload.get("results") or []}


def _store_read(context, reader):
    from alicebot_api.mcp_tools import _sqlite_path_from_url
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), USER_ID) as conn:
        return reader(SQLiteVNextStore(conn, USER_ID))


def _row(context, memory_id: str) -> dict:
    return _store_read(context, lambda store: store.get_memory(memory_id))


def _events(context, memory_id: str) -> list[dict]:
    return _store_read(context, lambda store: store.list_events(target_type="memory", target_id=memory_id))


def _revisions(context, memory_id: str) -> list[dict]:
    return _store_read(context, lambda store: store.list_revisions(memory_id))


def _mint_key(context, monkeypatch: pytest.MonkeyPatch, **key_fields) -> None:
    """Mint an agent key and make the MCP boundary resolve every call with it."""

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV
    from alicebot_api.vnext_agent_keys import create_agent_key

    _record, raw_key = _store_read(
        context, lambda store: create_agent_key(store, user_id=USER_ID, **key_fields)
    )
    monkeypatch.setenv(AGENT_API_KEY_ENV, raw_key)


def _blocked_reasons(context, memory_id: str) -> list[list[str]]:
    return [
        list(event["payload_json"]["policy_decision"]["reasons"])
        for event in _events(context, memory_id)
        if event["event_type"] == "agent.policy_blocked"
    ]


# --- the surface itself ----------------------------------------------------


def test_these_tests_run_on_the_default_three_tool_surface(tmp_path: Path, default_surface) -> None:
    """Guards the guard. If the conftest flag leaked back in, every test below
    would be exercising the eleven-tool surface and prove nothing about D8."""

    from alicebot_api.mcp.registry import list_mcp_tools
    from alicebot_api.mcp_tools import MCPToolNotFoundError

    context = _context(tmp_path)
    assert [tool["name"] for tool in list_mcp_tools()] == [
        "alice_memory_commit",
        "alice_recall",
        "alice_resume",
    ]
    pending = _commit_pending(context, agent_id="hermes")
    with pytest.raises(MCPToolNotFoundError, match="ALICE_MCP_FULL_TOOLS"):
        _call(
            context,
            "alice_memory_manage",
            action="confirm",
            confirmation_id=pending["confirmation_id"],
            agent_id="hermes",
        )


def test_the_advertised_schema_carries_the_confirmation_fields(default_surface) -> None:
    from alicebot_api.mcp.registry import list_mcp_tools

    commit = {tool["name"]: tool for tool in list_mcp_tools()}["alice_memory_commit"]
    properties = commit["inputSchema"]["properties"]
    assert properties["confirmation_action"]["enum"] == ["confirm", "reject"], (
        "edit is deliberately absent: an edited fact is a reject plus a fresh commit, "
        "which runs the full commit gate"
    )
    assert properties["confirmation_id"]["type"] == "string"
    assert "alice_memory_manage" not in commit["description"], (
        "the default tool must not point agents at a tool the default registry refuses"
    )
    assert "ask the user" in commit["description"].lower()


# --- acceptance ------------------------------------------------------------


def test_a_confirmed_pending_write_becomes_a_recallable_fact(tmp_path: Path, default_surface) -> None:
    """The acceptance case from the ticket, as Hermes would run it."""

    context = _context(tmp_path)
    pending = _commit_pending(context, agent_id="hermes")
    memory_id = str(pending["memory"]["id"])
    assert memory_id not in _recalled_ids(context, agent_id="hermes")

    confirmed = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
        agent_id="hermes",
    )

    assert confirmed["status"] == "committed", confirmed
    assert confirmed["receipt"] == "saved as a fact."
    assert confirmed["memory"]["id"] == memory_id
    assert _row(context, memory_id)["status"] == "active"
    assert memory_id in _recalled_ids(context, agent_id="hermes"), (
        "confirmed through alice_memory_commit, yet alice_recall still does not return it"
    )


def test_the_hermes_skill_ambient_example_can_be_finished(tmp_path: Path, default_surface) -> None:
    """The skill pack's own 0.84 example, read from the file an agent is given."""

    body = (REPO_ROOT / "agent-skills/hermes/alice-memory/SKILL.md").read_text(encoding="utf-8")
    examples = [json.loads(raw) for raw in re.findall(r"```json\n(.*?)```", body, re.S)]
    ambient = next(example for example in examples if example.get("confidence") == 0.84)
    finish = next(example for example in examples if "confirmation_id" in example)

    context = _context(tmp_path)
    pending = _call(context, "alice_memory_commit", **ambient)
    assert pending["status"] == "confirmation_required", pending

    confirmed = _call(
        context, "alice_memory_commit", **{**finish, "confirmation_id": pending["confirmation_id"]}
    )
    assert confirmed["status"] == "committed", confirmed
    assert str(pending["memory"]["id"]) in _recalled_ids(context, query="daily planning summaries")


def test_a_rejected_pending_write_never_becomes_searchable(tmp_path: Path, default_surface) -> None:
    context = _context(tmp_path)
    pending = _commit_pending(context, agent_id="hermes")
    memory_id = str(pending["memory"]["id"])

    rejected = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="reject",
        agent_id="hermes",
    )
    assert rejected["status"] == "rejected", rejected
    assert rejected["receipt"] == "rejected."
    assert _row(context, memory_id)["status"] == "rejected"

    # A committed control that matches the same query, so an empty result
    # cannot pass this test by accident.
    control = _call(
        context,
        "alice_memory_commit",
        title="Billing release day",
        canonical_text="The billing service deploys go out on Thursdays after review.",
        confidence=0.95,
        agent_id="hermes",
    )
    assert control["status"] == "committed", control
    recalled = _recalled_ids(context, agent_id="hermes")
    assert str(control["memory"]["id"]) in recalled
    assert memory_id not in recalled

    from alicebot_api.mcp_tools import MCPToolError

    with pytest.raises(MCPToolError, match="not pending"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
            agent_id="hermes",
        )
    assert _row(context, memory_id)["status"] == "rejected"


def test_an_expired_confirmation_resolves_to_rejected(tmp_path: Path, default_surface) -> None:
    """The 24 hour expiry is applied by VNextMemoryCommitService.confirm, so the
    new route inherits it rather than letting a stale pending row through. It
    is lazy: applied when a confirm or reject reaches the row, never in the
    background. The review approve path does not read it, so a reviewer can
    still approve the row after 24 hours; that path is outside this route."""

    context = _context(tmp_path)
    pending = _commit_pending(context, agent_id="hermes")
    memory_id = str(pending["memory"]["id"])

    def backdate(store) -> None:
        row = store.get_memory(memory_id)
        metadata = dict(row["metadata_json"])
        agentic = dict(metadata["agentic_memory"])
        confirmation = dict(agentic["confirmation"])
        confirmation["expires_at"] = "2020-01-01T00:00:00+00:00"
        agentic["confirmation"] = confirmation
        store.update_memory(memory_id=memory_id, patch={"metadata_json": {**metadata, "agentic_memory": agentic}})

    _store_read(context, backdate)
    result = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
        agent_id="hermes",
    )
    assert result["status"] == "rejected", result
    assert result["reason"] == "confirmation_expired"
    assert memory_id not in _recalled_ids(context, agent_id="hermes")


# --- the shape of a confirmation call --------------------------------------


def test_a_confirmation_must_say_confirm_or_reject(tmp_path: Path, default_surface) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context, agent_id="hermes")

    with pytest.raises(MCPToolError, match="confirmation_action"):
        _call(context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], agent_id="hermes")
    with pytest.raises(MCPToolError, match="confirmation_action"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="edit",
            agent_id="hermes",
        )
    with pytest.raises(MCPToolError, match="confirmation_id"):
        _call(context, "alice_memory_commit", confirmation_action="confirm", agent_id="hermes")
    assert _row(context, str(pending["memory"]["id"]))["status"] == "needs_review"


def test_a_confirmation_cannot_carry_a_new_write(tmp_path: Path, default_surface) -> None:
    """No edit on this route. confirm(action=edit) rewrites the stored text, and
    audit finding 3 says that path skips the credential check the commit gate
    applies. An edited fact is a reject plus a fresh commit instead."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context, agent_id="hermes")
    memory_id = str(pending["memory"]["id"])

    for extra in (
        {"canonical_text": "The team deploys the billing service on Fridays."},
        {"title": "Changed title"},
        {"sensitivity": "public"},
        {"confidence": 0.99},
    ):
        with pytest.raises(MCPToolError, match="reject it and commit"):
            _call(
                context,
                "alice_memory_commit",
                confirmation_id=pending["confirmation_id"],
                confirmation_action="confirm",
                agent_id="hermes",
                **extra,
            )
    row = _row(context, memory_id)
    assert row["status"] == "needs_review"
    assert row["canonical_text"] == PENDING_TEXT


def test_a_new_write_still_needs_title_and_text(tmp_path: Path, default_surface) -> None:
    """The schema no longer marks them required, because a confirmation call
    carries neither. The handler must still refuse a new write without them."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    with pytest.raises(MCPToolError, match="title is required"):
        _call(context, "alice_memory_commit", canonical_text=PENDING_TEXT, confidence=0.95)
    with pytest.raises(MCPToolError, match="canonical_text is required"):
        _call(context, "alice_memory_commit", title=PENDING_TITLE, confidence=0.95)


# --- the controls the manage route applies ---------------------------------


def test_the_commit_route_writes_the_same_audit_trail_as_manage(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same service call, same audit. Compared event for event and revision for
    revision against manage on the full surface."""

    from alicebot_api.mcp_tools import MCP_FULL_TOOLS_ENV

    def trail(context, memory_id: str) -> dict:
        events = _events(context, memory_id)
        confirm_decisions = [
            (
                event["payload_json"]["policy_decision"]["decision"],
                tuple(event["payload_json"]["policy_decision"]["reasons"]),
            )
            for event in events
            if event["event_type"] == "policy.decision"
            and event["payload_json"]["policy_decision"]["action"] == "memory.confirm"
        ]
        revisions = sorted(
            (
                str(revision["revision_type"]),
                str(revision["action"]),
                str(revision["reason"]),
                str(revision["actor_type"]),
                str(revision["actor_id"]),
            )
            for revision in _revisions(context, memory_id)
        )
        row = _row(context, memory_id)
        agentic = row["metadata_json"]["agentic_memory"]
        return {
            "event_types": Counter(
                (event["event_type"], event["actor_type"], event["actor_id"]) for event in events
            ),
            "confirm_decisions": confirm_decisions,
            "revisions": revisions,
            "row": (
                row["status"],
                row["confirmation_status"],
                row["canonical_text"],
                agentic["status"],
                agentic["lifecycle_status"],
                agentic["confirmation"]["status"],
            ),
        }

    via_commit = _context(tmp_path / "commit")
    pending = _commit_pending(via_commit, agent_id="hermes")
    _call(
        via_commit,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
        agent_id="hermes",
    )
    commit_trail = trail(via_commit, str(pending["memory"]["id"]))

    monkeypatch.setenv(MCP_FULL_TOOLS_ENV, "1")
    via_manage = _context(tmp_path / "manage")
    pending = _commit_pending(via_manage, agent_id="hermes")
    _call(
        via_manage,
        "alice_memory_manage",
        action="confirm",
        confirmation_id=pending["confirmation_id"],
        agent_id="hermes",
    )
    manage_trail = trail(via_manage, str(pending["memory"]["id"]))

    assert commit_trail["confirm_decisions"] == [("allowed", ())], commit_trail
    assert ("agent.memory_confirmed", "agent", "hermes") in commit_trail["event_types"]
    assert commit_trail == manage_trail


@pytest.mark.parametrize(
    ("claimed_identity", "reason"),
    (
        ({"agent_id": "stranger"}, "read_only_agent_cannot_write"),
        (
            {"agent_id": "proposer", "permission_profile": "memory_proposal_agent"},
            "memory_proposal_agent_cannot_mutate",
        ),
    ),
)
def test_an_agent_that_cannot_write_cannot_confirm(
    tmp_path: Path, default_surface, claimed_identity: dict, reason: str
) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context, agent_id="hermes")
    memory_id = str(pending["memory"]["id"])

    with pytest.raises(MCPToolError, match=reason):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
            **claimed_identity,
        )
    assert _row(context, memory_id)["status"] == "needs_review"
    assert any(reason in reasons for reasons in _blocked_reasons(context, memory_id)), (
        "the refusal left no agent.policy_blocked event on the pending row"
    )


def test_a_key_bound_agent_confirms_as_its_key_not_as_nobody(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ALICE_AGENT_API_KEY set, identity comes from the key. A read-only
    key that sends no identity fields at all must still be a read-only agent,
    not the identity-less local operator the service lets through."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context)
    memory_id = str(pending["memory"]["id"])
    _mint_key(context, monkeypatch, agent_id="reader", permission_profile="read_only_agent")

    with pytest.raises(MCPToolError, match="read_only_agent_cannot_write"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
        )
    assert _row(context, memory_id)["status"] == "needs_review"

    with pytest.raises(MCPToolError, match="issued to agent 'reader'"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
            agent_id="hermes",
        )
    assert _row(context, memory_id)["status"] == "needs_review"


def test_a_project_bound_key_cannot_confirm_another_projects_write(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context, domain="project", project_scope=["beta"])
    memory_id = str(pending["memory"]["id"])
    assert _row(context, memory_id)["project_scope"] == ["beta"]

    _mint_key(
        context,
        monkeypatch,
        agent_id="alpha-bot",
        permission_profile="trusted_local_agent",
        project_scope="alpha",
    )
    with pytest.raises(MCPToolError, match="project_scope_binding_violation"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
        )
    with pytest.raises(MCPToolError, match="project_scope_binding_violation"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="reject",
        )
    assert _row(context, memory_id)["status"] == "needs_review"

    # The same write from inside its project goes through, so the refusal
    # above is the fence and not something else about this row.
    _mint_key(
        context,
        monkeypatch,
        agent_id="beta-bot",
        permission_profile="trusted_local_agent",
        project_scope="beta",
    )
    confirmed = _call(
        context,
        "alice_memory_commit",
        confirmation_id=pending["confirmation_id"],
        confirmation_action="confirm",
    )
    assert confirmed["status"] == "committed", confirmed


def test_a_project_scoped_agent_cannot_confirm_a_restricted_domain(tmp_path: Path, default_surface) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(
        context,
        title="Clinic visit",
        canonical_text="The user has a clinic follow-up next month.",
        domain="health",
        sensitivity="private",
        confidence=0.95,
    )
    with pytest.raises(MCPToolError, match="all_requested_domains_restricted"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
            agent_id="openclaw",
            project_scope=["Alice"],
        )
    assert _row(context, str(pending["memory"]["id"]))["status"] == "needs_review"


# --- stricter than manage: the sensitivity ceiling -------------------------


SALARY = {
    "title": "Salary band",
    "canonical_text": "The user's salary band is confidential.",
    "sensitivity": "confidential",
    "confidence": 0.95,
}
SALARY_QUERY = "salary band confidential"


def _owner_recall_ids(context, monkeypatch: pytest.MonkeyPatch, query: str) -> set[str]:
    """Recall as the keyless owner, allowed to see confidential rows, so a
    confidential row that became searchable could not hide behind a filter."""

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    with monkeypatch.context() as scoped:
        scoped.delenv(AGENT_API_KEY_ENV, raising=False)
        return _recalled_ids(
            context,
            query=query,
            sensitivity_allowed=["public", "internal", "private", "confidential", "unknown"],
        )


def test_a_key_cannot_confirm_its_own_write_above_its_ceiling_but_can_reject_it(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review correction 1, 2026-09-22. Confirming past the ceiling is refused.
    Rejecting is allowed: it stores nothing and lowers exposure, and without
    it a keyed Hermes could not clear its own confidential pending write on
    this route at all. The reject response does echo the pending row back,
    as manage and the HTTP confirm route do; S4.5 is scheduled to make that
    echo author-only."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    pending = _commit_pending(context, **SALARY)
    memory_id = str(pending["memory"]["id"])
    assert _row(context, memory_id)["created_by_agent_id"] == "hermes", "the key's own write"

    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm")
    assert _row(context, memory_id)["status"] == "needs_review"
    blocked = _blocked_reasons(context, memory_id)
    assert blocked and all("sensitivity_above_agent_ceiling" in reasons for reasons in blocked), blocked

    rejected = _call(
        context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="reject"
    )
    assert rejected["status"] == "rejected", rejected
    assert rejected["receipt"] == "rejected."
    assert _row(context, memory_id)["status"] == "rejected"
    assert len(_blocked_reasons(context, memory_id)) == len(blocked), "the reject was refused somewhere"

    # A committed confidential control under the same query, so an empty owner
    # recall cannot pass this by accident.
    with monkeypatch.context() as scoped:
        from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

        scoped.delenv(AGENT_API_KEY_ENV, raising=False)
        control = _commit_pending(
            context, **{**SALARY, "canonical_text": "The salary band for the user is confidential and reviewed yearly."}
        )
        _call(context, "alice_memory_commit", confirmation_id=control["confirmation_id"], confirmation_action="confirm")
    recalled = _owner_recall_ids(context, monkeypatch, SALARY_QUERY)
    assert str(control["memory"]["id"]) in recalled
    assert memory_id not in recalled


def test_a_reject_past_the_ceiling_writes_the_services_own_audit(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reject goes through VNextMemoryCommitService.confirm, not a side
    door: its trail matches a direct service call with the same identity."""

    from alicebot_api.vnext_agent_control import AgentIdentity
    from alicebot_api.vnext_memory_commit import VNextMemoryCommitService

    def trail(context, memory_id: str) -> tuple:
        events = Counter(
            (event["event_type"], event["actor_type"], event["actor_id"]) for event in _events(context, memory_id)
        )
        decisions = sorted(
            (event["payload_json"]["policy_decision"]["decision"], tuple(event["payload_json"]["policy_decision"]["reasons"]))
            for event in _events(context, memory_id)
            if event["event_type"] == "policy.decision"
        )
        revisions = sorted(
            (str(r["revision_type"]), str(r["action"]), str(r["reason"]), str(r["actor_type"]), str(r["actor_id"]))
            for r in _revisions(context, memory_id)
        )
        return events, decisions, revisions, _row(context, memory_id)["status"]

    via_route = _context(tmp_path / "route")
    pending = _commit_pending(via_route, **SALARY)
    _mint_key(via_route, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    _call(via_route, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="reject")
    route_trail = trail(via_route, str(pending["memory"]["id"]))

    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV

    monkeypatch.delenv(AGENT_API_KEY_ENV)
    via_service = _context(tmp_path / "service")
    pending = _commit_pending(via_service, **SALARY)
    hermes = AgentIdentity(agent_id="hermes", permission_profile="trusted_local_agent", auth="agent_api_key")
    _store_read(
        via_service,
        lambda store: VNextMemoryCommitService(store).confirm(
            identity=hermes, confirmation_id=pending["confirmation_id"], action="reject"
        ),
    )
    service_trail = trail(via_service, str(pending["memory"]["id"]))

    assert ("agent.memory_confirmation_rejected", "agent", "hermes") in route_trail[0]
    assert route_trail[1] == [("allowed_with_filtering", ("restricted_sensitivity_filtered",))]
    assert route_trail == service_trail


@pytest.mark.parametrize(
    ("key_fields", "reason"),
    (
        ({"agent_id": "reader", "permission_profile": "read_only_agent"}, "read_only_agent_cannot_write"),
        (
            {"agent_id": "alpha-bot", "permission_profile": "trusted_local_agent", "project_scope": "alpha"},
            "project_scope_binding_violation",
        ),
    ),
)
def test_a_reject_past_the_ceiling_still_needs_identity_and_the_project_fence(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch, key_fields: dict, reason: str
) -> None:
    """Only the ceiling refusal is lifted for reject. The service's policy
    check still refuses an identity that could not write, and a key bound to
    another project."""

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context, **SALARY, domain="project", project_scope=["beta"])
    memory_id = str(pending["memory"]["id"])
    _mint_key(context, monkeypatch, **key_fields)

    with pytest.raises(MCPToolError, match=reason):
        _call(context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="reject")
    assert _row(context, memory_id)["status"] == "needs_review"


def test_a_ceiling_refusal_records_the_agent_like_a_service_refusal(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review correction 7. The service's own refusals upsert the refused
    agent into agent_identities before logging. The ceiling refusal must
    leave the same record, or an agent refused only here is invisible in the
    operator's agent list."""

    def agent_ids(context) -> set[str]:
        return {str(row["agent_id"]) for row in _store_read(context, lambda store: store.list_agent_identities(limit=50))}

    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(context, **SALARY)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    assert "hermes" not in agent_ids(context), "guard: nothing else may have recorded hermes yet"

    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm")
    assert "hermes" in agent_ids(context)

    # The service's own refusal, for comparison: it records the agent too.
    _mint_key(context, monkeypatch, agent_id="reader", permission_profile="read_only_agent")
    assert "reader" not in agent_ids(context)
    with pytest.raises(MCPToolError, match="read_only_agent_cannot_write"):
        _call(context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm")
    assert "reader" in agent_ids(context)


def test_the_ceiling_judges_the_row_as_read_under_the_lock(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review correction 7, time of check against time of use.

    The check first looks the row up by confirmation_id, then re-reads it
    under the row lock the service will hold. A write that raises the row's
    sensitivity between those two reads must be judged. SQLite cannot stage a
    real concurrent writer here, so the patch below makes that write happen
    inside the first get_memory_for_update call, just before the lock.
    """

    from alicebot_api.mcp_tools import MCPToolError
    from alicebot_api.sqlite_store import SQLiteVNextStore

    context = _context(tmp_path)
    pending = _commit_pending(context)
    memory_id = str(pending["memory"]["id"])
    assert _row(context, memory_id)["sensitivity"] == "unknown"
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")

    original = SQLiteVNextStore.get_memory_for_update
    raised: list[str] = []

    def a_write_lands_just_before_the_lock(self, target_id: str):
        if not raised:
            self.conn.execute("UPDATE memories SET sensitivity = 'confidential' WHERE id = ?", (target_id,))
            raised.append(target_id)
        return original(self, target_id)

    monkeypatch.setattr(SQLiteVNextStore, "get_memory_for_update", a_write_lands_just_before_the_lock)
    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm")
    assert raised == [memory_id], "guard: the simulated write never happened"
    assert _row(context, memory_id)["status"] == "needs_review"


def test_the_ceiling_takes_the_graph_lock_before_the_row_lock(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review correction 7, lock order.

    VNextMemoryCommitService.confirm takes the per-user graph lock and then
    the row lock. The ceiling check runs first and must take them in the same
    order, or on Postgres it would hold a row lock while waiting for the
    advisory lock other writers take first. SQLite's graph lock is a no-op, so
    this pins the order with a spy rather than a real deadlock.
    """

    from alicebot_api.sqlite_store import SQLiteVNextStore

    context = _context(tmp_path)
    pending = _commit_pending(context)
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")

    order: list[str] = []
    original_graph = SQLiteVNextStore.lock_graph_mutation
    original_row = SQLiteVNextStore.get_memory_for_update

    def graph_lock(self):
        order.append("graph")
        return original_graph(self)

    def row_lock(self, target_id: str):
        order.append("row")
        return original_row(self, target_id)

    monkeypatch.setattr(SQLiteVNextStore, "lock_graph_mutation", graph_lock)
    monkeypatch.setattr(SQLiteVNextStore, "get_memory_for_update", row_lock)
    confirmed = _call(
        context, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm"
    )

    assert confirmed["status"] == "committed", confirmed
    assert order.count("row") == 2, f"guard: expected the check's row lock and the service's, got {order}"
    assert order[0] == "graph", f"the check took a row lock before the graph lock: {order}"


def test_an_undeclared_caller_or_an_admin_key_can_confirm_above_private(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ceiling is the caller's, not a flat ban on sensitive writes."""

    def pending_confidential(context) -> dict:
        return _commit_pending(
            context,
            title="Salary band",
            canonical_text="The user's salary band is confidential.",
            sensitivity="confidential",
            confidence=0.95,
        )

    owner = _context(tmp_path / "owner")
    pending = pending_confidential(owner)
    confirmed = _call(
        owner, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm"
    )
    assert confirmed["status"] == "committed", confirmed

    admin = _context(tmp_path / "admin")
    pending = pending_confidential(admin)
    _mint_key(admin, monkeypatch, agent_id="operator", permission_profile="admin_agent")
    confirmed = _call(
        admin, "alice_memory_commit", confirmation_id=pending["confirmation_id"], confirmation_action="confirm"
    )
    assert confirmed["status"] == "committed", confirmed


def test_a_declared_trusted_agent_is_held_to_the_same_ceiling_without_a_key(
    tmp_path: Path, default_surface
) -> None:
    from alicebot_api.mcp_tools import MCPToolError

    context = _context(tmp_path)
    pending = _commit_pending(
        context,
        title="Salary band",
        canonical_text="The user's salary band is confidential.",
        sensitivity="confidential",
        confidence=0.95,
        agent_id="hermes",
    )
    with pytest.raises(MCPToolError, match="sensitivity_above_agent_ceiling"):
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=pending["confirmation_id"],
            confirmation_action="confirm",
            agent_id="hermes",
        )
    assert _row(context, str(pending["memory"]["id"]))["status"] == "needs_review"


def test_the_ceiling_is_not_already_enforced_by_the_shared_service(
    tmp_path: Path, default_surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the guard, and records audit finding 8 as it stands.

    The same key confirms the same kind of row through alice_memory_manage,
    which filters and writes. If this starts failing, manage has been fixed:
    good, and then the route's own check is redundant rather than wrong. Update
    this docstring and the D8 wiki entry; do not weaken the route.
    """

    from alicebot_api.mcp_tools import MCP_FULL_TOOLS_ENV

    context = _context(tmp_path)
    pending = _commit_pending(
        context,
        title="Salary band",
        canonical_text="The user's salary band is confidential.",
        sensitivity="confidential",
        confidence=0.95,
    )
    _mint_key(context, monkeypatch, agent_id="hermes", permission_profile="trusted_local_agent")
    monkeypatch.setenv(MCP_FULL_TOOLS_ENV, "1")
    via_manage = _call(context, "alice_memory_manage", action="confirm", confirmation_id=pending["confirmation_id"])
    assert via_manage["status"] == "committed", via_manage
