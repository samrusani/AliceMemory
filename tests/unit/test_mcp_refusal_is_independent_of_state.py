"""A caller the policy refuses hears the same refusal whatever state the target is in.

Unreleased (on main, not in v0.20.0). PR 528 gave the MCP tools typed error codes. A state error is
``precondition_failed`` and a refusal is ``not_permitted``. Several lifecycle verbs checked the state of the target
before they asked the policy, so a caller bound to one project learned from the code that a memory in another project
was a pending project update, an answered confirmation, or (on redact) a forgotten row. Before 528 every refusal
was ``tool_request_failed``, which hid the order without removing it.

The rule these tests pin: authorization (project scope, permission profile, sensitivity ceiling, who may resolve a
pending write) is decided before any state-specific error on every tool that takes an id. A refused caller hears
``not_permitted`` whatever the lifecycle state of a row it can see, and a row the API treats as gone (forgotten or
redacted) is ``not_found`` for it, the same as an id the vault never held, on every verb. The ruling that a key-bound
caller may learn that an id it holds exists outside its scope (``not_permitted``) rather than not at all (``not_found``)
is unchanged, and ``alice_explain`` stays one opaque ``tool_request_failed`` for a key-bound caller.

Each test names the mutation that must fail it. A mutation is made in a scratch edit of the source file, the test is
seen to fail, and the file is restored by copying the saved file back.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import mcp_server
from alicebot_api.mcp import registry
from alicebot_api.mcp.runtime import _sqlite_path_from_url, _vnext_store_context
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import MemoryNotFoundError, MemoryStateError, VNextMemoryCommitService

_USER_ID = "00000000-0000-0000-0000-000000000001"
_KEY_ENV = "ALICE_AGENT_API_KEY"
_FIXED_MESSAGE = "The tool request could not be processed"
_OWN_PROJECT = "alicebot"
_OTHER_PROJECT = "other-project"
# Every profile that can hold a key. A profile can refuse a verb on its own account (a read-only agent refuses every
# write, a trusted agent refuses redact), so the answer is read for each one: it must not vary with the state either.
_PROFILES = ("project_scoped_agent", "trusted_local_agent", "read_only_agent", "admin_agent")


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    monkeypatch.delenv(_KEY_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID))


# --- Wire helpers -------------------------------------------------------------------------------------------------


def _wire(ctx: MCPRuntimeContext, name: str, arguments: Mapping[str, object]) -> tuple[bool, dict[str, object]]:
    """One ``tools/call`` through the real server: the error flag and the decoded body."""

    server = mcp_server.MCPServer(context=ctx, input_stream=BytesIO(), output_stream=BytesIO())
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": dict(arguments)}}
    response = server._handle_request(request)
    assert response is not None
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return bool(result["isError"]), payload


def _answer(ctx: MCPRuntimeContext, name: str, arguments: Mapping[str, object]) -> str:
    """``ok`` for a call that succeeded, else the error code (the message is always the one fixed sentence)."""

    is_error, payload = _wire(ctx, name, arguments)
    if not is_error:
        return "ok"
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["message"] == _FIXED_MESSAGE, error
    return str(error["code"])


def _owner(ctx: MCPRuntimeContext, name: str, **arguments: object) -> dict[str, object]:
    """A keyless call that must succeed (the owner builds the fixtures)."""

    return registry.call_mcp_tool(ctx, name=name, arguments=arguments)


def _store_do(ctx: MCPRuntimeContext, action: Callable[[SQLiteVNextStore], object]) -> object:
    with sqlite_user_connection(_sqlite_path_from_url(ctx.database_url), ctx.user_id) as conn:
        return action(SQLiteVNextStore(conn, ctx.user_id))


def _mint_key(ctx: MCPRuntimeContext, *, profile: str, project: str, agent_id: str | None = None) -> str:
    with _vnext_store_context(ctx) as store:
        _record, raw_key = create_agent_key(
            store,  # type: ignore[arg-type]
            user_id=UUID(_USER_ID),
            agent_id=agent_id or f"keyed-{profile}-{project}",
            permission_profile=profile,
            project_scope=project,
        )
    return raw_key


# --- Target states ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    memory_id: str
    confirmation_id: str | None = None


def _set_metadata(ctx: MCPRuntimeContext, memory_id: str, update: Callable[[dict], dict], **patch: object) -> None:
    def write(store: SQLiteVNextStore) -> None:
        current = store.get_memory(memory_id)
        assert current is not None
        metadata = update(dict(current["metadata_json"]))
        store.update_memory(memory_id=memory_id, patch={**patch, "metadata_json": metadata}, actor_type="system")

    _store_do(ctx, write)


def _commit(ctx: MCPRuntimeContext, project: str, sensitivity: str, confidence: float) -> dict[str, object]:
    text = f"State fixture {uuid4().hex} in {project}."
    return _owner(
        ctx,
        "alice_memory_commit",
        title=text[:60],
        canonical_text=text,
        domain="project",
        sensitivity=sensitivity,
        confidence=confidence,
        project_scope=[project],
    )


def _pending(ctx: MCPRuntimeContext, project: str, sensitivity: str) -> _Target:
    row = _commit(ctx, project, sensitivity, 0.7)
    assert row["status"] == "confirmation_required", row
    return _Target(str(row["memory"]["id"]), str(row["confirmation_id"]))  # type: ignore[index]


def _answered(ctx: MCPRuntimeContext, project: str, sensitivity: str, action: str) -> _Target:
    target = _pending(ctx, project, sensitivity)
    done = _owner(
        ctx, "alice_memory_commit", confirmation_id=target.confirmation_id, confirmation_action=action
    )
    assert done["status"] in {"committed", "rejected"}, done
    return target


def _active(ctx: MCPRuntimeContext, project: str, sensitivity: str) -> _Target:
    if sensitivity == "internal":
        row = _commit(ctx, project, sensitivity, 0.95)
        assert row["status"] == "committed", row
        return _Target(str(row["memory"]["id"]))  # type: ignore[index]
    # A level above private always asks the owner first, however sure the writer is.
    return _answered(ctx, project, sensitivity, "confirm")


def _after_manage(action: str, **extra: object) -> Callable[[MCPRuntimeContext, str, str], _Target]:
    def build(ctx: MCPRuntimeContext, project: str, sensitivity: str) -> _Target:
        target = _active(ctx, project, sensitivity)
        _owner(ctx, "alice_memory_manage", action=action, memory_id=target.memory_id, **extra)
        return target

    return build


def _patched(update: Callable[[dict], dict], **patch: object) -> Callable[[MCPRuntimeContext, str, str], _Target]:
    def build(ctx: MCPRuntimeContext, project: str, sensitivity: str) -> _Target:
        target = _active(ctx, project, sensitivity)
        _set_metadata(ctx, target.memory_id, update, **patch)
        return target

    return build


def _confirmation_expired(ctx: MCPRuntimeContext, project: str, sensitivity: str) -> _Target:
    target = _pending(ctx, project, sensitivity)

    def backdate(metadata: dict) -> dict:
        confirmation = metadata["agentic_memory"]["confirmation"]
        confirmation["expires_at"] = "2020-01-01T00:00:00Z"
        return metadata

    _set_metadata(ctx, target.memory_id, backdate)
    return target


def _as_project_update(candidate: object) -> Callable[[dict], dict]:
    def update(metadata: dict) -> dict:
        metadata["workflow"] = "project_auto_update"
        if candidate is not None:
            metadata["candidate"] = candidate
        return metadata

    return update


def _as_consolidation_candidate(metadata: dict) -> dict:
    metadata["consolidation"] = {"proposal_kind": "merge"}
    return metadata


# A state is a name and a way to reach it. The visible ones leave a row the API still reads (a forgotten row is
# ``superseded``, so it is one of them). ``archived`` and ``redacted`` leave a row with ``deleted_at`` set, which every
# read but redact's own treats as gone.
_VISIBLE_STATES: dict[str, Callable[[MCPRuntimeContext, str, str], _Target]] = {
    "active": _active,
    "pending confirmation": _pending,
    "confirmation confirmed": lambda ctx, project, sensitivity: _answered(ctx, project, sensitivity, "confirm"),
    "confirmation rejected": lambda ctx, project, sensitivity: _answered(ctx, project, sensitivity, "reject"),
    "confirmation expired": _confirmation_expired,
    "needs review": lambda ctx, project, sensitivity: _Target(str(_commit(ctx, project, sensitivity, 0.4)["memory"]["id"])),  # type: ignore[index]
    "superseded": _after_manage("undo"),
    "forgotten": _after_manage("forget"),
    "expired": _after_manage("expire", reason="no longer true"),
    "stale": _patched(lambda metadata: metadata, status="stale"),
    "consolidation candidate": _patched(_as_consolidation_candidate, status="candidate"),
    "pending project update": _patched(_as_project_update(None), status="needs_review"),
    "terminal project update": _patched(_as_project_update(False)),
}
_DELETED_STATES: dict[str, Callable[[MCPRuntimeContext, str, str], _Target]] = {
    "archived": _patched(lambda metadata: metadata, status="archived"),
    "redacted": _after_manage("redact", reason="the owner asked for removal"),
}
_ALL_STATES = {**_VISIBLE_STATES, **_DELETED_STATES}


# --- Calls: every tool and verb that takes an id --------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    tool: str
    arguments: Callable[[_Target, str], dict[str, object]]
    needs_confirmation: bool = False
    # What a refused caller is told for a row it can see, and for one that is gone.
    refused: str = "not_permitted"
    gone: str = "not_found"


def _manage(action: str, **extra: object) -> Callable[[_Target, str], dict[str, object]]:
    return lambda target, _anchor: {"action": action, "memory_id": target.memory_id, **extra}


_REPLACEMENT_CALL = "manage undo, target as the replacement"
_CALLS: dict[str, _Call] = {
    "review": _Call("alice_memory_review", lambda target, _anchor: {"review_item_id": target.memory_id}),
    "correct approve": _Call(
        "alice_memory_correct", lambda target, _anchor: {"review_item_id": target.memory_id, "action": "approve"}
    ),
    "manage forget": _Call("alice_memory_manage", _manage("forget")),
    "manage undo": _Call("alice_memory_manage", _manage("undo")),
    _REPLACEMENT_CALL: _Call(
        "alice_memory_manage",
        lambda target, anchor: {"action": "undo", "memory_id": anchor, "superseded_by": target.memory_id},
    ),
    "manage redact": _Call("alice_memory_manage", _manage("redact", reason="the owner asked for removal")),
    "manage expire": _Call("alice_memory_manage", _manage("expire", reason="no longer true")),
    "manage unexpire": _Call("alice_memory_manage", _manage("unexpire", reason="still true")),
    "manage accept_consolidation": _Call(
        "alice_memory_manage", _manage("accept_consolidation", reason="the merge is right")
    ),
    "manage confirm": _Call(
        "alice_memory_manage",
        lambda target, _anchor: {"action": "confirm", "confirmation_id": target.confirmation_id},
        needs_confirmation=True,
    ),
    "commit, confirm": _Call(
        "alice_memory_commit",
        lambda target, _anchor: {"confirmation_id": target.confirmation_id, "confirmation_action": "confirm"},
        needs_confirmation=True,
    ),
    "commit, reject": _Call(
        "alice_memory_commit",
        lambda target, _anchor: {"confirmation_id": target.confirmation_id, "confirmation_action": "reject"},
        needs_confirmation=True,
    ),
    # One uniform answer for a key-bound caller, on purpose: explain expands related rows.
    "explain": _Call(
        "alice_explain",
        lambda target, _anchor: {"memory_id": target.memory_id},
        refused="tool_request_failed",
        gone="tool_request_failed",
    ),
}
_STATES_WITH_A_CONFIRMATION = {
    "pending confirmation",
    "confirmation confirmed",
    "confirmation rejected",
    "confirmation expired",
}


def _applies(call: _Call, state: str) -> bool:
    return not call.needs_confirmation or state in _STATES_WITH_A_CONFIRMATION


# --- The matrix ----------------------------------------------------------------------------------------------------


def test_a_caller_the_project_scope_refuses_hears_the_same_answer_in_every_state(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every id-taking tool and verb, for a key bound to one project, aimed at a row of another project.

    The row is put in each lifecycle state the code distinguishes. For a row the caller can see, the answer is
    ``not_permitted`` in every state, for every profile. For an archived or a redacted row (``deleted_at`` is set) it is
    ``not_found``, the answer for an id the vault never held, on every verb, so no verb tells a deleted row from a
    missing one.
    ``alice_explain`` stays ``tool_request_failed`` in every cell.

    Mutations, each one alone, in ``vnext_memory_commit.py`` unless noted (a failing cell names the call, the profile
    and the state): delete the ``refuse_unauthorized_write`` call in ``forget`` (the pending project update cell of
    ``manage forget`` fails), in ``undo`` before ``_require_project_update_decision_path(memory)`` (``manage undo``),
    in ``undo`` before the same call for the successor (the replacement row), in ``confirm`` (the answered
    confirmation cells of ``manage confirm`` and ``commit`` fail); in ``mcp/memories.py`` delete the
    ``refuse_unauthorized_write`` call in ``redact_memory_flow`` (``manage redact`` fails in the pending project
    update state) or delete its ``deleted_at`` branch (``manage redact`` fails for ``archived`` and ``redacted``).
    """

    anchor_key = _mint_key(context, profile="admin_agent", project=_OWN_PROJECT, agent_id="anchor-builder")
    monkeypatch.setenv(_KEY_ENV, anchor_key)
    anchor = _active(context, _OWN_PROJECT, "internal").memory_id
    monkeypatch.delenv(_KEY_ENV)
    targets = {state: build(context, _OTHER_PROJECT, "internal") for state, build in _ALL_STATES.items()}
    missing = _Target(str(uuid4()), f"confirm-{uuid4()}")
    keys = {profile: _mint_key(context, profile=profile, project=_OWN_PROJECT) for profile in _PROFILES}

    seen: dict[tuple[str, str, str], str] = {}
    for profile, raw_key in keys.items():
        monkeypatch.setenv(_KEY_ENV, raw_key)
        for call_name, call in _CALLS.items():
            for state, target in targets.items():
                if _applies(call, state):
                    seen[(profile, call_name, state)] = _answer(context, call.tool, call.arguments(target, anchor))
            seen[(profile, call_name, "missing")] = _answer(context, call.tool, call.arguments(missing, anchor))
    monkeypatch.delenv(_KEY_ENV)

    assert {key[0] for key in seen} == set(_PROFILES)
    cells = len(seen)
    assert cells > 600, cells

    def expected(profile: str, call_name: str, state: str) -> str:
        call = _CALLS[call_name]
        if profile == "read_only_agent" and call_name == _REPLACEMENT_CALL:
            # The profile refuses the undo itself, on the caller's own row, before the replacement is read. It is
            # the same refusal in every state, which is the property under test.
            return call.refused
        return call.gone if state == "missing" or state in _DELETED_STATES else call.refused

    wrong = {key: answer for key, answer in seen.items() if answer != expected(*key)}
    assert wrong == {}, sorted(wrong.items())


def test_the_callers_the_policy_allows_still_hear_the_state_and_are_never_refused(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control of the matrix: the owner and a key bound to the target's own project are never ``not_permitted``.

    Each cell gets a fresh row, because a call that succeeds changes it. The state answers are still the state
    answers for these callers: a pending project update is ``precondition_failed`` to forget, undo or redact, an
    answered confirmation is ``precondition_failed`` to answer again, an active row is forgotten, and an archived or a redacted
    row can still be redacted (that is why redact reads deleted rows).

    Mutations, each one alone, in ``vnext_memory_commit.py``: make ``_write_policy_decision`` answer ``blocked`` for
    every call (the first assertion fails, the owner is refused); delete the ``_require_project_update_decision_path``
    call in ``forget`` (the pending project update cell of the owner and of the key fails with ``ok``).
    """

    admin_in_scope = _mint_key(context, profile="admin_agent", project=_OTHER_PROJECT, agent_id="admin-in-scope")
    anchor = _active(context, _OTHER_PROJECT, "internal").memory_id
    answers: dict[tuple[str, str, str], str] = {}
    for caller, raw_key in (("owner", None), ("key bound in scope", admin_in_scope)):
        for call_name, call in _CALLS.items():
            for state, build in _ALL_STATES.items():
                if not _applies(call, state):
                    continue
                monkeypatch.delenv(_KEY_ENV, raising=False)
                target = build(context, _OTHER_PROJECT, "internal")
                if raw_key is not None:
                    monkeypatch.setenv(_KEY_ENV, raw_key)
                answers[(caller, call_name, state)] = _answer(context, call.tool, call.arguments(target, anchor))
    monkeypatch.delenv(_KEY_ENV, raising=False)

    assert "not_permitted" not in answers.values()
    for caller in ("owner", "key bound in scope"):
        assert answers[(caller, "manage forget", "pending project update")] == "precondition_failed"
        assert answers[(caller, "manage undo", "pending project update")] == "precondition_failed"
        assert answers[(caller, "manage redact", "pending project update")] == "precondition_failed"
        assert answers[(caller, "manage forget", "active")] == "ok"
        assert answers[(caller, "manage redact", "archived")] == "ok"
        assert answers[(caller, "manage redact", "redacted")] == "ok"
        assert answers[(caller, "manage forget", "archived")] == "not_found"
        assert answers[(caller, "manage confirm", "confirmation confirmed")] == "precondition_failed"
        assert answers[(caller, "commit, reject", "confirmation confirmed")] == "precondition_failed"
        assert answers[(caller, "commit, confirm", "pending confirmation")] == "ok"


def test_a_caller_above_the_sensitivity_ceiling_hears_the_same_answer_in_every_state(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second refusal dimension: a trusted agent in the row's own project, below the row's sensitivity.

    A confidential row in the key's own project is refused by the ceiling on every write verb. It must not turn into
    a state answer when the row is a pending project update or any other state. Review hides a row above the ceiling,
    so it answers ``not_found`` in every state; that is another code, and the same in every cell.

    Mutations, each one alone, in ``vnext_memory_commit.py``: delete the ``refuse_unauthorized_write`` call in ``forget``
    or in ``undo`` (the pending project update cell of that verb fails with ``precondition_failed``); in
    ``_write_policy_decision`` skip ``_block_mutation_above_sensitivity_ceiling`` always (every cell of every write
    verb fails with ``ok`` or a state answer).
    """

    states = (
        "active",
        "superseded",
        "stale",
        "consolidation candidate",
        "pending project update",
        "terminal project update",
    )
    targets = {state: _VISIBLE_STATES[state](context, _OWN_PROJECT, "confidential") for state in states}
    key = _mint_key(context, profile="trusted_local_agent", project=_OWN_PROJECT, agent_id="trusted-below-ceiling")
    monkeypatch.setenv(_KEY_ENV, key)
    verbs = ("manage forget", "manage undo", "manage expire", "manage unexpire", "manage accept_consolidation")
    answers = {
        (verb, state): _answer(context, _CALLS[verb].tool, _CALLS[verb].arguments(target, ""))
        for verb in verbs
        for state, target in targets.items()
    }
    review = {
        state: _answer(context, "alice_memory_review", {"review_item_id": target.memory_id})
        for state, target in targets.items()
    }
    monkeypatch.delenv(_KEY_ENV)

    assert set(answers.values()) == {"not_permitted"}, answers
    assert set(review.values()) == {"not_found"}, review


# --- The service the HTTP routes share ------------------------------------------------------------------------------


def test_the_service_refuses_before_it_reads_the_state_for_every_verb_including_correct(
    context: MCPRuntimeContext,
) -> None:
    """``correct`` is not reachable by a key-bound caller over MCP (a legacy tool), but the HTTP route calls it.

    The commit service is the common path of the MCP tools and the HTTP routes, so a caller the policy refuses is
    refused in every state, and a deleted row is a missing one. This calls the service with a key-bound identity.

    Mutation: delete the ``refuse_unauthorized_write`` call in ``correct`` (the pending project update row fails
    with ``MemoryStateError``).
    """

    outsider = AgentIdentity(
        agent_id="service-outsider",
        agent_type="coding_agent",
        permission_profile="admin_agent",
        project_scope=(_OWN_PROJECT,),
        auth="agent_api_key",
        project_scope_locked=True,
    )
    targets = {state: build(context, _OTHER_PROJECT, "internal") for state, build in _ALL_STATES.items()}
    outcomes: dict[tuple[str, str], str] = {}

    def attempt(label: str, state: str, call: Callable[[VNextMemoryCommitService], object]) -> None:
        def run(store: SQLiteVNextStore) -> str:
            try:
                call(VNextMemoryCommitService(store))
            except AgentPolicyBlockedError:
                return "refused"
            except MemoryNotFoundError:
                return "not found"
            except MemoryStateError:
                return "state"
            return "ok"

        outcomes[(label, state)] = str(_store_do(context, run))

    for state, target in targets.items():
        attempt(
            "correct",
            state,
            lambda service, memory_id=target.memory_id: service.correct(
                identity=outsider, memory_id=memory_id, canonical_text="A corrected fact."
            ),
        )
        attempt(
            "forget",
            state,
            lambda service, memory_id=target.memory_id: service.forget(identity=outsider, memory_id=memory_id),
        )
        attempt(
            "undo",
            state,
            lambda service, memory_id=target.memory_id: service.undo(identity=outsider, memory_id=memory_id),
        )
    wrong = {
        key: outcome
        for key, outcome in outcomes.items()
        if outcome != ("not found" if key[1] in _DELETED_STATES else "refused")
    }
    assert wrong == {}, sorted(wrong.items())


def test_a_refused_call_on_a_pending_project_update_is_audited_like_any_other_refusal(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal of a pending project update writes the same ``agent.policy_blocked`` row as an ordinary one, and a
    call the state refuses for an allowed caller writes no policy row at all.

    Mutations, each one alone, in ``refuse_unauthorized_write`` (``vnext_memory_commit.py``): raise the blocked
    decision without calling ``_record_write_decision`` (the two ``agent.policy_blocked`` counts fail, no row); call
    ``_record_write_decision`` for an allowed decision too (the last assertion fails, a policy row is written before the
    state refuses the call).
    """

    target = _VISIBLE_STATES["pending project update"](context, _OTHER_PROJECT, "internal")
    ordinary = _VISIBLE_STATES["active"](context, _OTHER_PROJECT, "internal")
    key = _mint_key(context, profile="admin_agent", project=_OWN_PROJECT, agent_id="audited-outsider")
    monkeypatch.setenv(_KEY_ENV, key)
    assert _answer(context, "alice_memory_manage", {"action": "forget", "memory_id": target.memory_id}) == "not_permitted"
    assert _answer(context, "alice_memory_manage", {"action": "forget", "memory_id": ordinary.memory_id}) == "not_permitted"
    monkeypatch.delenv(_KEY_ENV)

    def blocked_rows(memory_id: str) -> list[object]:
        events = _store_do(context, lambda store: store.list_events(target_type="memory", target_id=memory_id))
        return [event for event in events if event["event_type"] == "agent.policy_blocked"]  # type: ignore[attr-defined]

    assert len(blocked_rows(target.memory_id)) == 1
    assert len(blocked_rows(ordinary.memory_id)) == 1

    # An allowed caller whose call the state refuses writes no policy row. The wire rolls the whole call back, which
    # would hide a row written before the refusal, so the service is called here and the connection is kept.
    insider = AgentIdentity(
        agent_id="audited-insider",
        agent_type="coding_agent",
        permission_profile="admin_agent",
        project_scope=(_OTHER_PROJECT,),
        auth="agent_api_key",
        project_scope_locked=True,
    )

    def state_refusal_writes(store: SQLiteVNextStore) -> tuple[list[str], list[str]]:
        before = [str(event["id"]) for event in store.list_events(target_type="memory", target_id=target.memory_id)]
        with pytest.raises(MemoryStateError):
            VNextMemoryCommitService(store).forget(identity=insider, memory_id=target.memory_id)
        after = [str(event["id"]) for event in store.list_events(target_type="memory", target_id=target.memory_id)]
        return before, after

    before, after = _store_do(context, state_refusal_writes)  # type: ignore[misc]
    assert after == before
