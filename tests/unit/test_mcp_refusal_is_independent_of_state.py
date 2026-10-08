"""A caller the policy refuses hears the same refusal whatever state the target is in.

Unreleased (on main, not in v0.20.0). PR 528 gave the MCP tools typed error codes. A state error is
``precondition_failed`` and a refusal is ``not_permitted``. Several lifecycle verbs checked the state of the target
before they asked the policy, so a caller bound to one project learned from the code that a memory in another project
was a pending project update, an answered confirmation, or (on redact) a forgotten row. Before 528 every refusal
was ``tool_request_failed``, which hid the order without removing it.

The rule these tests pin: authorization (project scope, permission profile, sensitivity ceiling, who may resolve a
pending write) is decided before any state-specific error on every tool that takes an id. A caller the policy refuses
for a row it can read hears ``not_permitted`` whatever the lifecycle state of the row, and a row the API treats as gone
(forgotten or redacted) is ``not_found`` for it, the same as an id the vault never held, on every verb. A caller that
may not read the row at all (it is in another project, above the caller's ceiling or in a domain the profile is held
back from) hears ``not_found`` in every state, the same as for an id that does not exist, and the call writes what a
call on a missing id writes, which is nothing: the policy events and the agent record are events a key reads back in its
own telemetry. That replaces the earlier ruling that such a caller may learn from ``not_permitted`` that an id it holds
exists outside its scope. A refusal for a row the caller can read is audited as before, and so is a refused ``redact``
of an archived or redacted row, which is read on purpose. ``alice_explain`` stays one opaque ``tool_request_failed`` for
a key-bound caller.

Each test names the mutation that must fail it. A mutation is made in a scratch edit of the source file, the test is
seen to fail, and the file is restored by copying the saved file back.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import mcp_server
from alicebot_api.mcp import registry
from alicebot_api.mcp.memories import redact_memory_flow
from alicebot_api.mcp.runtime import _sqlite_path_from_url, _vnext_store_context
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import (
    MemoryNotFoundError,
    MemoryStateError,
    VNextMemoryCommitService,
)

_USER_ID = "00000000-0000-0000-0000-000000000001"
_KEY_ENV = "ALICE_AGENT_API_KEY"
_FIXED_MESSAGE = "The tool request could not be processed"
_OWN_PROJECT = "alicebot"
_OTHER_PROJECT = "other-project"
# Every profile that can hold a key. A profile can refuse a verb on its own account (a read-only agent refuses every
# write, a trusted agent refuses redact), so the answer is read for each one: it must not vary with the state either.
_PROFILES = ("project_scoped_agent", "trusted_local_agent", "read_only_agent", "admin_agent")


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[MCPRuntimeContext]:
    monkeypatch.delenv(_KEY_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    # The server logs every refusal with its traceback. A matrix of several hundred refused calls would spend most of
    # its time formatting them, and no assertion here reads the log.
    logging.disable(logging.CRITICAL)
    try:
        yield MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID))
    finally:
        logging.disable(logging.NOTSET)


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
        metadata.update(with_derived_from({}, {}))
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

    The row is put in each lifecycle state the code distinguishes. The caller may not read a row of another project, so
    the answer is ``not_found`` in every state, for every profile, the answer for an id the vault never held: no verb
    tells a row of another project, a deleted row or a missing one apart. Two kinds of cell stay ``not_permitted``: the
    undo of the caller's own row by a profile that cannot write, and a confirmation, which is named by its token.
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
        if call.needs_confirmation and state != "missing":
            # A confirmation is named by its token and not by the id of a row, so its refusal stays a refusal.
            return call.refused
        return call.gone

    wrong = {key: answer for key, answer in seen.items() if answer != expected(*key)}
    assert wrong == {}, sorted(wrong.items())


# A fresh row per cell makes this the slowest test here, so it takes the states its assertions read and a few more.
_CONTROL_STATES = (
    "active",
    "pending confirmation",
    "confirmation confirmed",
    "superseded",
    "consolidation candidate",
    "pending project update",
    "archived",
    "redacted",
)


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
            for state in _CONTROL_STATES:
                build = _ALL_STATES[state]
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

    A confidential row in the key's own project is above the ceiling, so the key may not read it and every write verb
    answers ``not_found``, as review does. It must not turn into a state answer when the row is a pending project update
    or any other state. The refusal is recorded all the same.

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

    assert set(answers.values()) == {"not_found"}, answers
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
    # The rows are in another project, so the key may not read them in any state: each one is a row that does not exist.
    wrong = {key: outcome for key, outcome in outcomes.items() if outcome != "not found"}
    assert wrong == {}, sorted(wrong.items())


def test_a_refused_call_on_a_pending_project_update_is_audited_like_any_other_refusal(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal of a pending project update writes the same ``agent.policy_blocked`` row as an ordinary one, and a
    call the state refuses for an allowed caller writes no policy row at all.

    The caller here is a trusted agent bound to the rows' own project, so it may read them and its profile refuses
    ``redact``. A caller bound to another project may not read the rows and is told they do not exist, which writes
    nothing: the policy events and the agent record are events a key can read back, and a missing id writes none.

    Mutations, each one alone, in ``refuse_unauthorized_write`` (``vnext_memory_commit.py``): raise the blocked
    decision without calling ``_record_write_decision`` (the two ``agent.policy_blocked`` counts fail, no row); call
    ``_record_write_decision`` for an allowed decision too (the last assertion fails, a policy row is written before the
    state refuses the call); delete the ``_answer_as_missing_if_unreadable`` call there (the outsider's rows get a refusal
    row).
    """

    target = _VISIBLE_STATES["pending project update"](context, _OTHER_PROJECT, "internal")
    ordinary = _VISIBLE_STATES["active"](context, _OTHER_PROJECT, "internal")
    key = _mint_key(context, profile="trusted_local_agent", project=_OTHER_PROJECT, agent_id="audited-in-scope")
    monkeypatch.setenv(_KEY_ENV, key)
    redact = {"action": "redact", "reason": "no"}
    assert _answer(context, "alice_memory_manage", {**redact, "memory_id": target.memory_id}) == "not_permitted"
    assert _answer(context, "alice_memory_manage", {**redact, "memory_id": ordinary.memory_id}) == "not_permitted"
    monkeypatch.delenv(_KEY_ENV)

    def blocked_rows(memory_id: str) -> list[object]:
        events = _store_do(context, lambda store: store.list_events(target_type="memory", target_id=memory_id))
        return [event for event in events if event["event_type"] == "agent.policy_blocked"]  # type: ignore[attr-defined]

    assert len(blocked_rows(target.memory_id)) == 1
    assert len(blocked_rows(ordinary.memory_id)) == 1

    # A caller bound to another project may not read the rows: they do not exist for it, and the call writes nothing.
    outsider = _mint_key(context, profile="admin_agent", project=_OWN_PROJECT, agent_id="audited-outsider")
    monkeypatch.setenv(_KEY_ENV, outsider)
    for row in (target, ordinary):
        assert _answer(context, "alice_memory_manage", {"action": "forget", "memory_id": row.memory_id}) == "not_found"
    monkeypatch.delenv(_KEY_ENV)
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


# --- A refusal is audited whatever the row's state, and says what it was asked ----------------------------------------


def _blocked_events(ctx: MCPRuntimeContext, memory_id: str) -> list[dict[str, object]]:
    events = _store_do(ctx, lambda store: store.list_events(target_type="memory", target_id=memory_id))
    return [dict(event) for event in events if event["event_type"] == "agent.policy_blocked"]  # type: ignore[attr-defined]


def _blocked_decision(event: Mapping[str, object]) -> Mapping[str, object]:
    payload = event["payload_json"]
    assert isinstance(payload, Mapping)
    decision = payload["policy_decision"]
    assert isinstance(decision, Mapping)
    return decision


def test_a_refused_redact_of_an_archived_or_redacted_row_is_audited_and_answered_not_found(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused ``redact`` leaves one ``agent.policy_blocked`` row for every state of the row that the key may know of.

    A live row of another project is not the key's to read, so it is answered as a row that does not exist and writes
    nothing. A live row the key may read and its profile refuses, and an archived or redacted row whoever asks, leave the
    row.

    Redact reads an archived or redacted row on purpose, and a refused caller is answered ``not_found`` for it. The
    answer is raised after the refusal is recorded, and the whole call used to roll back with it: v0.20.0 recorded the
    refusal of a live and of an archived row (answered ``not_permitted``) and the first version of the follow-up wrote
    no row for an archived one. A refused replay of a redacted row wrote none in v0.20.0 either, and now writes one, so
    each of the three states here has exactly one row naming the caller, the action and the reason, whatever the
    answer. Two refusals are read: a key bound to another project (scope) and a trusted agent bound to the row's own
    project (permission profile, which refuses redact).

    Mutations, each one alone, in ``mcp/memories.py``: in ``redact_memory_flow`` raise ``MemoryNotFoundError`` in place
    of ``RefusedOnDeletedMemoryError`` (the archived and redacted cells of both callers have no audit row); in
    ``_handle_alice_vnext_redact_memory`` set ``hide_the_row`` false always (the deleted cells answer ``not_permitted``)
    or true always (the live cell answers ``not_found``).
    """

    targets = {
        "live": _VISIBLE_STATES["active"],
        "archived": _DELETED_STATES["archived"],
        "redacted": _DELETED_STATES["redacted"],
    }
    callers = (
        # (label, profile, key bound to, project of the rows, reason the policy gives)
        ("scope", "admin_agent", _OWN_PROJECT, _OTHER_PROJECT, "project_scope_binding_violation"),
        ("profile", "trusted_local_agent", _OTHER_PROJECT, _OTHER_PROJECT, "human_or_admin_review_required"),
    )
    cells: dict[tuple[str, str], tuple[str, list[dict[str, object]], str]] = {}
    for label, profile, bound_to, rows_in, _reason in callers:
        rows = {state: build(context, rows_in, "internal") for state, build in targets.items()}
        key = _mint_key(context, profile=profile, project=bound_to, agent_id=f"audited-{label}")
        monkeypatch.setenv(_KEY_ENV, key)
        for state, row in rows.items():
            answer = _answer(
                context, "alice_memory_manage", {"action": "redact", "memory_id": row.memory_id, "reason": "no"}
            )
            cells[(label, state)] = (answer, _blocked_events(context, row.memory_id), row.memory_id)
        monkeypatch.delenv(_KEY_ENV)

    for label, _profile, _bound_to, _rows_in, reason in callers:
        for state in targets:
            answer, events, _memory_id = cells[(label, state)]
            # The key bound to another project may not read the row at all; the trusted agent reads it and is refused
            # by its profile, which a live row tells it.
            assert answer == ("not_permitted" if state == "live" and label == "profile" else "not_found"), (
                label, state, answer,
            )
            if label == "scope" and state == "live":
                # A live row of another project is a row that does not exist for this key, which writes nothing. An
                # archived or redacted row is audited whoever asks, because redact reads it on purpose.
                assert events == [], (label, state, events)
                continue
            assert len(events) == 1, (label, state, events)
            assert events[0]["actor_id"] == f"audited-{label}", (label, state)
            decision = _blocked_decision(events[0])
            assert decision["action"] == "memory.redact", (label, state)
            assert decision["decision"] == "blocked", (label, state)
            assert reason in decision["reasons"], (label, state, decision["reasons"])  # type: ignore[operator]

    # The control: a caller the policy allows redacts the deleted row and is never audited as refused.
    admin = _mint_key(context, profile="admin_agent", project=_OTHER_PROJECT, agent_id="audited-in-scope")
    archived = _DELETED_STATES["archived"](context, _OTHER_PROJECT, "internal")
    monkeypatch.setenv(_KEY_ENV, admin)
    assert _answer(
        context, "alice_memory_manage", {"action": "redact", "memory_id": archived.memory_id, "reason": "ok"}
    ) == "ok"
    monkeypatch.delenv(_KEY_ENV)
    assert _blocked_events(context, archived.memory_id) == []


def test_the_http_redact_route_audits_a_refusal_and_answers_404_for_a_deleted_row(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route twin of the MCP test, on the SQLite store through the real route function and a real key.

    The route's connection is the SQLite one, which commits on a clean exit and rolls back on an exception, as the
    Postgres one does. A refused redact answers 403 for a live row and 404 for an archived or a redacted row, the
    answer for an id the vault never held, and each of the three leaves its ``agent.policy_blocked`` row. A plain
    not-found error would answer the same 404 and roll that row back, so the rows are read after the call. A key bound
    to another project may not read the live row either, so it is told 404 for it; a trusted agent bound to the row's own
    project reads it and is refused by its profile with 403.

    Mutations, each one alone: in ``routers/vnext_memories.py`` delete the ``except RefusedOnDeletedMemoryError``
    clause of ``redact_vnext_memory`` (the archived and redacted rows answer 403); in ``redact_memory_flow``
    (``mcp/memories.py``) raise ``MemoryNotFoundError`` in place of ``RefusedOnDeletedMemoryError`` (those rows answer 404
    and have no audit row); in the route, answer 404 for every refusal (the live row answers 404).
    """

    from contextlib import contextmanager

    from alicebot_api.config import Settings
    from alicebot_api.routers import vnext_memories as router

    path = _sqlite_path_from_url(context.database_url)

    @contextmanager
    def connection(_database_url: str, user_id: UUID) -> Iterator[object]:
        with sqlite_user_connection(path, user_id) as conn:
            yield conn

    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url="postgresql://stand-in"))
    monkeypatch.setattr(router, "user_connection", connection)
    monkeypatch.setattr(router, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, _USER_ID))

    rows = {
        "live": _VISIBLE_STATES["active"](context, _OTHER_PROJECT, "internal"),
        "archived": _DELETED_STATES["archived"](context, _OTHER_PROJECT, "internal"),
        "redacted": _DELETED_STATES["redacted"](context, _OTHER_PROJECT, "internal"),
    }
    key = _mint_key(context, profile="admin_agent", project=_OWN_PROJECT, agent_id="http-scoped-admin")
    trusted = _mint_key(context, profile="trusted_local_agent", project=_OTHER_PROJECT, agent_id="http-trusted-in-scope")
    missing = str(uuid4())

    def post(memory_id: str, bearer: str) -> int:
        response = router.redact_vnext_memory(
            router.VNextMemoryRedactRequest(user_id=UUID(_USER_ID), memory_id=UUID(memory_id), reason="no"),
            authorization=f"Bearer {bearer}",
        )
        return response.status_code

    statuses = {state: post(row.memory_id, key) for state, row in rows.items()}
    statuses["missing"] = post(missing, key)

    # A key bound to another project may not read any of the rows, so it is told 404 for each, as for an id the vault
    # never held. A trusted agent bound to the rows' project reads the live row and is refused by its profile (403).
    assert statuses == {"live": 404, "archived": 404, "redacted": 404, "missing": 404}
    # The live row is not the key's to read and writes nothing; the deleted rows are audited, because redact reads them.
    assert {state: len(_blocked_events(context, row.memory_id)) for state, row in rows.items()} == {
        "live": 0,
        "archived": 1,
        "redacted": 1,
    }
    assert _blocked_events(context, missing) == []
    in_scope = {state: post(row.memory_id, trusted) for state, row in rows.items()}
    assert in_scope == {"live": 403, "archived": 404, "redacted": 404}
    assert {state: len(_blocked_events(context, row.memory_id)) for state, row in rows.items()} == {
        "live": 1,
        "archived": 2,
        "redacted": 2,
    }


# --- The permission profile is a refusal dimension too --------------------------------------------------------------

_WRITE_VERBS = (
    "manage forget",
    "manage undo",
    _REPLACEMENT_CALL,
    "manage redact",
    "manage expire",
    "manage unexpire",
    "manage accept_consolidation",
)
# What each profile refuses on its own account, for a row of the key's own project (scope does not refuse it). A
# read-only agent and a proposal agent refuse every write; a trusted and a project-scoped agent refuse the two verbs only
# a human or an admin may run; an admin key refuses none.
_PROFILE_REFUSES: dict[str, tuple[str, ...]] = {
    "read_only_agent": _WRITE_VERBS,
    "memory_proposal_agent": _WRITE_VERBS,
    "trusted_local_agent": ("manage redact", "manage accept_consolidation"),
    "project_scoped_agent": ("manage redact", "manage accept_consolidation"),
    "admin_agent": (),
}


def test_a_caller_the_permission_profile_refuses_hears_the_same_answer_in_every_state(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third refusal dimension, read inside the key's own project so that scope refuses nothing.

    Every profile, bound to the project the rows are in, calls each write verb on rows in six states. A profile that
    refuses a verb hears ``not_permitted`` for a row it can see in every state (a pending project update included, the
    state that used to answer ``precondition_failed`` first), and ``not_found`` for an archived or redacted row. The
    control is the other half: a profile the policy allows hears the state, so a pending project update is
    ``precondition_failed`` to a trusted agent that forgets or undoes it and to an admin key that redacts it. Without
    the control a pre-check that refused everything would pass.

    Mutations, each one alone, in ``vnext_memory_commit.py`` and ``mcp/memories.py``: change the action of the
    ``refuse_unauthorized_write`` call in ``redact_memory_flow`` from ``memory.redact`` to ``memory.forget`` (the trusted
    and project-scoped agents hear ``precondition_failed`` for the pending project update); change the action of the
    call in ``forget`` or in ``undo`` to ``memory.redact`` (the trusted agent hears ``not_permitted`` where the control
    says ``precondition_failed``); change the action of the call for the successor in ``undo`` to ``memory.redact`` (the
    replacement cell of the trusted agent fails); change the action of the ``forget`` or ``undo`` call to ``memory.recall``
    (the read-only agent hears ``precondition_failed`` for the pending project update).
    """

    anchor = _active(context, _OWN_PROJECT, "internal").memory_id
    states = ("active", "pending project update", "superseded", "consolidation candidate", "archived", "redacted")
    targets = {state: _ALL_STATES[state](context, _OWN_PROJECT, "internal") for state in states}
    keys = {profile: _mint_key(context, profile=profile, project=_OWN_PROJECT) for profile in _PROFILE_REFUSES}

    seen: dict[tuple[str, str, str], str] = {}
    for profile, raw_key in keys.items():
        monkeypatch.setenv(_KEY_ENV, raw_key)
        for call_name in _WRITE_VERBS:
            call = _CALLS[call_name]
            refused = call_name in _PROFILE_REFUSES[profile]
            for state in states:
                if refused:
                    seen[(profile, call_name, state)] = _answer(
                        context, call.tool, call.arguments(targets[state], anchor)
                    )
                elif state == "pending project update" and call_name in {
                    "manage forget",
                    "manage undo",
                    _REPLACEMENT_CALL,
                    "manage redact",
                }:
                    # The control. The row stays a pending project update and the call changes nothing.
                    seen[(profile, call_name, state)] = _answer(
                        context, call.tool, call.arguments(targets[state], anchor)
                    )
    monkeypatch.delenv(_KEY_ENV)

    expected: dict[tuple[str, str, str], str] = {}
    for profile in _PROFILE_REFUSES:
        for call_name in _WRITE_VERBS:
            for state in states:
                if call_name in _PROFILE_REFUSES[profile]:
                    # The replacement call is refused on the anchor, which the profile reads first, in every state.
                    deleted = state in _DELETED_STATES and call_name != _REPLACEMENT_CALL
                    expected[(profile, call_name, state)] = "not_found" if deleted else "not_permitted"
                elif state == "pending project update" and call_name in {
                    "manage forget",
                    "manage undo",
                    _REPLACEMENT_CALL,
                    "manage redact",
                }:
                    expected[(profile, call_name, state)] = "precondition_failed"
    assert set(seen) == set(expected)
    assert len(seen) > 100, len(seen)
    wrong = {key: answer for key, answer in seen.items() if answer != expected[key]}
    assert wrong == {}, sorted(wrong.items())


def test_a_reject_stays_allowed_above_the_ceiling_and_a_confirm_does_not(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``allow_above_ceiling`` flag of the confirm pre-check, read by the author of a row above its own ceiling.

    The author of a pending write is a trusted agent whose ceiling stops at ``private``. The row is then raised to
    ``confidential`` (the ceiling was lowered, or the row relabelled), so only the author gets as far as the ceiling.
    A confirm stores the text and is refused by the ceiling, so it is ``not_permitted`` for a pending row and for an
    answered one. A reject stores nothing and stays allowed above the ceiling: the pending row is rejected, and the
    answered one answers the state (``precondition_failed``), because the policy let the reject through. The answered
    row is the cell that separates the flag from the later real check, which refuses a confirm of a pending row anyway.

    Mutations, each one alone, on the ``refuse_unauthorized_write`` call in ``confirm`` (``vnext_memory_commit.py``):
    change ``allow_above_ceiling=normalized_action == "reject"`` to ``True`` (the answered row's confirm answers
    ``precondition_failed``), to ``False`` (the pending row's reject answers ``not_permitted``), or to
    ``normalized_action == "confirm"`` (both of those).
    """

    key = _mint_key(context, profile="trusted_local_agent", project=_OWN_PROJECT, agent_id="ceiling-author")
    monkeypatch.setenv(_KEY_ENV, key)

    def author_row(*, answered: bool) -> _Target:
        row = _commit(context, _OWN_PROJECT, "private", 0.7)
        assert row["status"] == "confirmation_required", row
        target = _Target(str(row["memory"]["id"]), str(row["confirmation_id"]))  # type: ignore[index]
        if answered:
            done = _owner(
                context, "alice_memory_commit", confirmation_id=target.confirmation_id, confirmation_action="confirm"
            )
            assert done["status"] == "committed", done
        _store_do(
            context,
            lambda store: store.conn.execute(  # type: ignore[attr-defined]
                "UPDATE memories SET sensitivity = ? WHERE id = ?", ("confidential", target.memory_id)
            ),
        )
        return target

    rows = {"pending": author_row(answered=False), "answered": author_row(answered=True)}

    def decide(state: str, verb: str) -> str:
        return _answer(
            context,
            "alice_memory_commit",
            {"confirmation_id": rows[state].confirmation_id, "confirmation_action": verb},
        )

    # A confirm first, so the pending row is still pending when the reject reaches it.
    answers = {(state, "confirm"): decide(state, "confirm") for state in rows}
    answers[("pending", "manage confirm")] = _answer(
        context,
        "alice_memory_manage",
        {"action": "confirm", "confirmation_id": rows["pending"].confirmation_id},
    )
    answers.update({(state, "reject"): decide(state, "reject") for state in rows})
    monkeypatch.delenv(_KEY_ENV)

    assert answers == {
        ("pending", "confirm"): "not_permitted",
        ("answered", "confirm"): "not_permitted",
        ("pending", "manage confirm"): "not_permitted",
        ("pending", "reject"): "ok",
        ("answered", "reject"): "precondition_failed",
    }


# --- The audit row names the action that was asked ------------------------------------------------------------------


def test_each_refusal_audit_row_names_the_action_that_was_asked(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row of a refused call says which action it refused, at every pre-check, so a wrong action string is seen.

    The answer does not separate every wrong string (``memory.forget`` and ``memory.expire`` are the same rule to the
    policy), but the ``agent.policy_blocked`` row carries the action the pre-check evaluated. Two callers aim the verbs
    at rows of one project.

    A read-only key bound to that project may read the rows and its profile refuses every write, so each of forget,
    undo, redact and correct (through the service; an MCP caller cannot reach it with a key, the HTTP route does) leaves
    one row on its own target that names the verb. An admin key bound to another project may not read the rows: the
    verbs that name a row by id are answered as a missing row and leave no row, while a confirmation is named by its
    token and keeps its refusal, so confirm over the manage tool and over the commit tool (a confirm and a reject) each
    leave one row that names ``memory.confirm``.

    Mutations, each one alone: change the action string of the ``refuse_unauthorized_write`` call in ``forget``,
    in ``undo``, in ``correct`` or in ``confirm`` (``vnext_memory_commit.py``), or in ``redact_memory_flow``
    (``mcp/memories.py``), to any other write action such as ``memory.expire``; the row of that call names the wrong
    action and one assertion fails.
    """

    reader_rows = {name: _active(context, _OTHER_PROJECT, "internal") for name in ("forget", "undo", "redact", "correct")}
    reader = _mint_key(context, profile="read_only_agent", project=_OTHER_PROJECT, agent_id="audit-reader")
    monkeypatch.setenv(_KEY_ENV, reader)
    reader_answers = {
        "forget": _answer(context, "alice_memory_manage", {"action": "forget", "memory_id": reader_rows["forget"].memory_id}),
        "undo": _answer(context, "alice_memory_manage", {"action": "undo", "memory_id": reader_rows["undo"].memory_id}),
        "redact": _answer(
            context, "alice_memory_manage", {"action": "redact", "memory_id": reader_rows["redact"].memory_id, "reason": "no"}
        ),
    }
    monkeypatch.delenv(_KEY_ENV)
    assert set(reader_answers.values()) == {"not_permitted"}, reader_answers

    def refuse_correct(store: SQLiteVNextStore) -> str:
        identity = AgentIdentity(
            agent_id="audit-reader",
            agent_type="coding_agent",
            permission_profile="read_only_agent",
            project_scope=(_OTHER_PROJECT,),
            auth="agent_api_key",
            project_scope_locked=True,
        )
        try:
            VNextMemoryCommitService(store).correct(
                identity=identity, memory_id=reader_rows["correct"].memory_id, canonical_text="A corrected fact."
            )
        except AgentPolicyBlockedError:
            return "refused"
        return "ok"

    assert _store_do(context, refuse_correct) == "refused"
    asked = {"forget": "memory.forget", "undo": "memory.undo", "redact": "memory.redact", "correct": "memory.correct"}
    named = {}
    for name, row in reader_rows.items():
        events = _blocked_events(context, row.memory_id)
        assert len(events) == 1, (name, events)
        named[name] = _blocked_decision(events[0])["action"]
    assert named == asked

    # The outsider: rows of another project, which it may not read.
    rows = {
        "forget": _active(context, _OTHER_PROJECT, "internal"),
        "undo": _active(context, _OTHER_PROJECT, "internal"),
        "redact": _active(context, _OTHER_PROJECT, "internal"),
        "manage confirm": _pending(context, _OTHER_PROJECT, "internal"),
        "commit confirm": _pending(context, _OTHER_PROJECT, "internal"),
        "commit reject": _pending(context, _OTHER_PROJECT, "internal"),
    }
    calls: dict[str, tuple[str, dict[str, object]]] = {
        "forget": ("alice_memory_manage", {"action": "forget", "memory_id": rows["forget"].memory_id}),
        "undo": ("alice_memory_manage", {"action": "undo", "memory_id": rows["undo"].memory_id}),
        "redact": ("alice_memory_manage", {"action": "redact", "memory_id": rows["redact"].memory_id, "reason": "no"}),
        "manage confirm": (
            "alice_memory_manage",
            {"action": "confirm", "confirmation_id": rows["manage confirm"].confirmation_id},
        ),
        "commit confirm": (
            "alice_memory_commit",
            {"confirmation_id": rows["commit confirm"].confirmation_id, "confirmation_action": "confirm"},
        ),
        "commit reject": (
            "alice_memory_commit",
            {"confirmation_id": rows["commit reject"].confirmation_id, "confirmation_action": "reject"},
        ),
    }
    outsider = _mint_key(context, profile="admin_agent", project=_OWN_PROJECT, agent_id="audit-outsider")
    monkeypatch.setenv(_KEY_ENV, outsider)
    answers = {name: _answer(context, tool, arguments) for name, (tool, arguments) in calls.items()}
    monkeypatch.delenv(_KEY_ENV)
    assert answers == {
        "forget": "not_found",
        "undo": "not_found",
        "redact": "not_found",
        "manage confirm": "not_permitted",
        "commit confirm": "not_permitted",
        "commit reject": "not_permitted",
    }, answers
    for name, row in rows.items():
        events = _blocked_events(context, row.memory_id)
        if name.endswith("confirm") or name == "commit reject":
            assert [_blocked_decision(event)["action"] for event in events] == ["memory.confirm"], (name, events)
        else:
            assert events == [], (name, events)


# --- The replay branch is a guard of its own ------------------------------------------------------------------------


def test_a_replay_of_a_redaction_is_refused_by_its_own_policy_check_if_the_pre_check_is_gone(
    context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact-replay branch of ``redact_memory_flow`` evaluates the policy itself, and this proves it can stand alone.

    The pre-check refuses every caller this branch refuses, so with both in place no test can tell the branch is
    there. It is kept on purpose, as a second guard: a replay is the one path that writes nothing and answers with the
    row's redaction receipt, so its authorization should not depend on a call made earlier in the function. Here the
    pre-check is replaced by a function that does nothing, and a key bound to another project that replays the
    redaction of an already redacted row is still refused, with no write at all.

    Mutation, in ``redact_memory_flow`` (``mcp/memories.py``): delete the ``if decision.decision == "blocked"`` raise of
    the replay branch (the replay succeeds for the outsider).
    """

    redacted = _DELETED_STATES["redacted"](context, _OTHER_PROJECT, "internal")
    monkeypatch.setattr(VNextMemoryCommitService, "refuse_unauthorized_write", lambda self, **kwargs: None)
    outsider = AgentIdentity(
        agent_id="replay-outsider",
        agent_type="coding_agent",
        permission_profile="admin_agent",
        project_scope=(_OWN_PROJECT,),
        auth="agent_api_key",
        project_scope_locked=True,
    )

    def replay(store: SQLiteVNextStore) -> tuple[str, int, int]:
        before = len(store.list_events(target_type="memory", target_id=redacted.memory_id))
        try:
            redact_memory_flow(store, memory_id=redacted.memory_id, reason="replay", identity=outsider)
        except AgentPolicyBlockedError:
            outcome = "refused"
        else:
            outcome = "replayed"
        after = len(store.list_events(target_type="memory", target_id=redacted.memory_id))
        return outcome, before, after

    outcome, before, after = _store_do(context, replay)  # type: ignore[misc]
    assert outcome == "refused"
    assert after == before
