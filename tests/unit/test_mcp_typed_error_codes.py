"""Typed MCP error codes (ride-along P9 of the search-quality release).

Unreleased (on main, not in v0.20.0). v0.20.0 answered ``tool_request_failed`` for a policy refusal, a missing id,
a state that forbids the call and a rejected argument alike, so an agent could not tell "not allowed" from "does not
exist" from "broken". Main adds three codes (``not_permitted``, ``not_found``, ``precondition_failed``) and sends
``invalid_request`` for argument errors. The design is wiki 05, "Widen the code enum. Leave every message exactly as
it is": the codes are a closed set Alice authors, a code is chosen only from a typed exception class, and the message
the client sees is the same fixed sentence as before, so nothing a caller or a store put into an exception can reach
the wire. ``tests/unit/test_mcp_error_contracts.py`` (the sentinel test) is untouched and still passes.

Each test names the mutation that must fail it. A mutation is made in a scratch edit of the source file, the test is
seen to fail, and the file is restored by copying the saved file back.
"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from alicebot_api import mcp_server
from alicebot_api.continuity_brief import ContinuityBriefValidationError
from alicebot_api.continuity_capture import ContinuityCaptureValidationError
from alicebot_api.continuity_contradictions import (
    ContinuityContradictionNotFoundError,
    ContinuityContradictionValidationError,
)
from alicebot_api.continuity_evidence import ContinuityEvidenceNotFoundError
from alicebot_api.continuity_recall import ContinuityRecallValidationError, RetrievalTraceNotFoundError
from alicebot_api.continuity_resumption import ContinuityResumptionValidationError
from alicebot_api.continuity_review import (
    ContinuityReviewNotFoundError,
    ContinuityReviewStateError,
    ContinuityReviewValidationError,
    apply_continuity_correction,
)
from alicebot_api.contracts import ContinuityCorrectionInput
from alicebot_api.mcp import evidence_artifacts, registry
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.mcp.types import (
    MCP_CODED_ERROR_CODES,
    MCPArgumentError,
    MCPCodedToolError,
    MCPInvalidRequestError,
    MCPNotPermittedError,
    MCPPreconditionFailedError,
    MCPReferenceNotFoundError,
    MCPRuntimeContext,
    MCPToolError,
)
from alicebot_api.memory_mutations import MemoryMutationValidationError
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.task_briefing import TaskBriefNotFoundError, TaskBriefValidationError
from alicebot_api.temporal_state import TemporalStateNotFoundError, TemporalStateValidationError
from alicebot_api.vnext_agent_control import AgentIdentity, AgentPolicyBlockedError, PolicyDecision
from alicebot_api.vnext_agent_keys import AgentKeyAuthenticationError
from alicebot_api.vnext_lifecycle import LifecycleTransitionError
from alicebot_api.vnext_memory_commit import (
    IdempotencyKeyConflictError,
    MemoryNotFoundError,
    MemoryStateError,
    VNextMemoryCommitValidationError,
)
from alicebot_api.vnext_source_fence import SourceReadFence

_SENTINEL = "PRIVATE-TYPED-CODE-SENTINEL"
_FIXED_MESSAGE = "The tool request could not be processed"
_USER_ID = "00000000-0000-0000-0000-000000000001"
_ROOT = Path(__file__).resolve().parents[2]
_MCP_DIR = _ROOT / "apps" / "api" / "src" / "alicebot_api" / "mcp"

_TRUSTED = {
    "agent_id": "builder-typed-codes",
    "agent_type": "coding_agent",
    "permission_profile": "trusted_local_agent",
}


def _request(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _server(context: MCPRuntimeContext) -> mcp_server.MCPServer:
    return mcp_server.MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())


def _wire(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> dict[str, object]:
    """One ``tools/call`` through the real server, read the way a client reads it."""

    response = _server(context)._handle_request(_request(name, arguments))
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list) and len(content) == 1
    text = content[0]["text"]
    assert _SENTINEL not in text
    payload = json.loads(text)
    assert isinstance(payload, dict)
    payload["_is_error"] = result["isError"]
    return payload


def _code(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> str:
    """The error code of a failed call; fails when the call succeeded or the message is not the fixed sentence."""

    payload = _wire(context, name, arguments)
    assert payload["_is_error"] is True, payload
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["message"] == _FIXED_MESSAGE, error
    return str(error["code"])


@pytest.fixture
def sqlite_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID))


@pytest.fixture
def postgres_url_context(monkeypatch: pytest.MonkeyPatch) -> MCPRuntimeContext:
    """A context that never connects: every test that uses it patches the handler or raises first."""

    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return MCPRuntimeContext(database_url="postgresql://localhost/alicebot", user_id=UUID(_USER_ID))


def _commit(context: MCPRuntimeContext, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "title": "Typed code fixture",
        "canonical_text": "The billing service deploys on Thursdays.",
        "domain": "project",
        "sensitivity": "internal",
        "confidence": 0.95,
        **overrides,
    }
    return registry.call_mcp_tool(context, name="alice_memory_commit", arguments=arguments)


# --- The classes and the closed set -------------------------------------------------------------------------------


_CODED_CLASSES = {
    MCPArgumentError: "invalid_request",
    MCPNotPermittedError: "not_permitted",
    MCPReferenceNotFoundError: "not_found",
    MCPPreconditionFailedError: "precondition_failed",
}


def test_every_coded_error_is_an_mcp_tool_error_and_none_is_the_public_message_error() -> None:
    """Code that catches ``MCPToolError`` keeps catching them, and none can send a message of its own.

    ``MCPInvalidRequestError`` is the one class whose message is not fixed. A coded error that subclassed it would
    send its exception text.

    Mutations: make ``MCPNotPermittedError`` a plain ``ValueError``; make ``MCPArgumentError`` a subclass of
    ``MCPInvalidRequestError``. Each fails this test.
    """

    for cls, code in _CODED_CLASSES.items():
        assert issubclass(cls, MCPCodedToolError)
        assert issubclass(cls, MCPToolError)
        assert not issubclass(cls, MCPInvalidRequestError)
        assert cls.code == code


def test_the_closed_set_is_four_codes_and_the_old_codes_are_unchanged() -> None:
    """The set the server may send for a coded error is exactly four, and every code clients already read stays.

    ``invalid_request`` is reused for argument errors, so the set holds it; ``tool_request_failed`` is the fallback
    and is not in the set.

    Mutations: add a fifth code to ``MCP_CODED_ERROR_CODES``; put ``tool_request_failed`` in it; rename any of the
    five old constants in ``mcp_server.py``. Each fails this test.
    """

    assert MCP_CODED_ERROR_CODES == frozenset({"invalid_request", "not_permitted", "not_found", "precondition_failed"})
    assert MCPCodedToolError.code == "tool_request_failed"
    assert mcp_server._TOOL_NOT_FOUND_CODE == "tool_not_found"
    assert mcp_server._TOOL_INVALID_REQUEST_CODE == "invalid_request"
    assert mcp_server._TOOL_REQUEST_FAILED_CODE == "tool_request_failed"
    assert mcp_server._TOOL_EXECUTION_FAILED_CODE == "tool_execution_failed"
    assert mcp_server._MCP_STARTUP_FAILED_CODE == "mcp_startup_failed"
    assert mcp_server._TOOL_REQUEST_FAILED_MESSAGE == _FIXED_MESSAGE


# --- The server: a code, the same fixed message, never the exception text ---------------------------------------


@pytest.mark.parametrize(("exception_class", "code"), tuple(_CODED_CLASSES.items()))
def test_the_server_answers_each_coded_error_with_its_code_and_the_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url_context: MCPRuntimeContext,
    exception_class: type[MCPCodedToolError],
    code: str,
) -> None:
    """The wire body is the code and the one fixed sentence; the exception text, a planted sentinel, never leaves.

    Mutations: send ``str(exc)`` as the message in the ``MCPCodedToolError`` clause of ``mcp_server.py`` (every
    parameter fails); make that clause never match (every parameter fails); change one ``code = ...`` line of a class
    in ``mcp/types.py`` to another code (the parameter of that class fails).
    """

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise exception_class(_SENTINEL)

    monkeypatch.setattr(mcp_server, "call_mcp_tool", fail)

    payload = _wire(postgres_url_context, "alice_recall", {})

    assert payload["_is_error"] is True
    assert payload["error"] == {"code": code, "message": _FIXED_MESSAGE}


def test_a_code_outside_the_closed_set_answers_tool_request_failed(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url_context: MCPRuntimeContext,
) -> None:
    """A subclass cannot put a new string on the wire, and the bare base answers the generic code.

    The set is closed by the server, not by convention: a class whose ``code`` is not listed (here the sentinel
    itself) is sent as ``tool_request_failed``.

    Mutation: send ``exc.code`` without the ``in MCP_CODED_ERROR_CODES`` check in ``mcp_server.py``. The rogue
    parameter then answers the sentinel and fails.
    """

    class Rogue(MCPCodedToolError):
        code = _SENTINEL

    for exception in (Rogue(_SENTINEL), MCPCodedToolError(_SENTINEL)):

        def fail(*_args: object, _exception: Exception = exception, **_kwargs: object) -> dict[str, object]:
            raise _exception

        monkeypatch.setattr(mcp_server, "call_mcp_tool", fail)
        payload = _wire(postgres_url_context, "alice_recall", {})
        assert payload["error"] == {"code": "tool_request_failed", "message": _FIXED_MESSAGE}


def test_the_reason_of_a_coded_error_stays_in_the_server_log(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url_context: MCPRuntimeContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator still gets the exception text and traceback; the client gets neither.

    Mutation: drop ``exc_info=True`` from the ``logger.warning`` call of the coded clause in ``mcp_server.py``.
    """

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise MCPNotPermittedError(_SENTINEL)

    monkeypatch.setattr(mcp_server, "call_mcp_tool", fail)
    with caplog.at_level(logging.WARNING, logger=mcp_server.logger.name):
        payload = _wire(postgres_url_context, "alice_recall", {})

    assert payload["error"] == {"code": "not_permitted", "message": _FIXED_MESSAGE}
    assert _SENTINEL in caplog.text


# --- The dispatcher: a code comes from the class of the exception, never from its text -------------------------


def _check_violation() -> Exception:
    return CheckViolation("memories_memory_type_check")


def _real_integrity_error(kind: str) -> sqlite3.IntegrityError:
    """A real error raised by SQLite, so it carries ``sqlite_errorcode`` the way a vault's errors do."""

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), "
            "kind TEXT CHECK (kind IN ('a')), label TEXT UNIQUE)"
        )
        conn.execute("INSERT INTO child (id, kind, label) VALUES (1, 'a', 'x')")
        statements = {
            "check": "INSERT INTO child (id, kind) VALUES (2, 'zzz')",
            "foreign_key": "INSERT INTO child (id, parent_id) VALUES (3, 999)",
            "unique": "INSERT INTO child (id, label) VALUES (4, 'x')",
        }
        with pytest.raises(sqlite3.IntegrityError) as caught:
            conn.execute(statements[kind])
        return caught.value
    finally:
        conn.close()


_DECISION = PolicyDecision(
    decision="blocked",
    action="memory.correct",
    permission_profile="trusted_local_agent",
    reasons=("human_or_admin_review_required",),
)

# (label, a factory of the exception a handler raises, the code the wire must carry)
_DISPATCH_TABLE: tuple[tuple[str, Callable[[], Exception], str], ...] = (
    ("policy blocked", lambda: AgentPolicyBlockedError(_DECISION), "not_permitted"),
    ("key refused", lambda: AgentKeyAuthenticationError(_SENTINEL), "not_permitted"),
    ("capture validation", lambda: ContinuityCaptureValidationError(_SENTINEL), "invalid_request"),
    ("recall validation", lambda: ContinuityRecallValidationError(_SENTINEL), "invalid_request"),
    ("brief validation", lambda: ContinuityBriefValidationError(_SENTINEL), "invalid_request"),
    ("resumption validation", lambda: ContinuityResumptionValidationError(_SENTINEL), "invalid_request"),
    ("review validation", lambda: ContinuityReviewValidationError(_SENTINEL), "invalid_request"),
    ("contradiction validation", lambda: ContinuityContradictionValidationError(_SENTINEL), "invalid_request"),
    ("task brief validation", lambda: TaskBriefValidationError(_SENTINEL), "invalid_request"),
    ("temporal validation", lambda: TemporalStateValidationError(_SENTINEL), "invalid_request"),
    ("postgres check violation", lambda: _check_violation(), "invalid_request"),
    ("review not found", lambda: ContinuityReviewNotFoundError(_SENTINEL), "not_found"),
    ("contradiction not found", lambda: ContinuityContradictionNotFoundError(_SENTINEL), "not_found"),
    ("trace not found", lambda: RetrievalTraceNotFoundError(_SENTINEL), "not_found"),
    ("evidence not found", lambda: ContinuityEvidenceNotFoundError(_SENTINEL), "not_found"),
    ("task brief not found", lambda: TaskBriefNotFoundError(_SENTINEL), "not_found"),
    ("memory not found", lambda: MemoryNotFoundError(_SENTINEL), "not_found"),
    # A LookupError, not a ValueError, so the argument clause never saw it. Raised by the temporal functions behind
    # alice_explain (entity_id), alice_state_at and alice_timeline.
    ("temporal state not found", lambda: TemporalStateNotFoundError(_SENTINEL), "not_found"),
    ("memory state", lambda: MemoryStateError(_SENTINEL), "precondition_failed"),
    ("review state", lambda: ContinuityReviewStateError(_SENTINEL), "precondition_failed"),
    ("lifecycle transition", lambda: LifecycleTransitionError(_SENTINEL), "precondition_failed"),
    ("sqlite check", lambda: _real_integrity_error("check"), "invalid_request"),
    ("sqlite foreign key", lambda: _real_integrity_error("foreign_key"), "precondition_failed"),
    # The PostgreSQL twin of the row above, from the driver's own class (SQLSTATE 23503), so one write that names a
    # row the vault does not hold answers the same code on both backends.
    ("postgres foreign key violation", lambda: ForeignKeyViolation("provenance_links_source_fkey"), "precondition_failed"),
    # One class for two meanings, a rejected argument and a missing candidate: not typed, so not coded.
    ("memory mutation validation", lambda: MemoryMutationValidationError(_SENTINEL), "tool_request_failed"),
    ("commit validation, plain", lambda: VNextMemoryCommitValidationError(_SENTINEL), "tool_request_failed"),
    ("idempotency conflict", lambda: IdempotencyKeyConflictError(_SENTINEL), "tool_request_failed"),
    ("sqlite unique", lambda: _real_integrity_error("unique"), "tool_request_failed"),
    ("bare tool error", lambda: MCPToolError(_SENTINEL), "tool_request_failed"),
    ("bare value error", lambda: ValueError(_SENTINEL), "tool_request_failed"),
    ("bare type error", lambda: TypeError(_SENTINEL), "tool_request_failed"),
    # A permission error that is not a policy or key refusal is a file or socket failure: broken, not refused.
    ("plain permission error", lambda: PermissionError(_SENTINEL), "tool_execution_failed"),
    ("runtime error", lambda: RuntimeError(_SENTINEL), "tool_execution_failed"),
)


@pytest.mark.parametrize(("label", "make_exception", "code"), _DISPATCH_TABLE, ids=[row[0] for row in _DISPATCH_TABLE])
def test_the_dispatcher_picks_the_code_from_the_class_of_the_exception(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url_context: MCPRuntimeContext,
    label: str,
    make_exception: Callable[[], Exception],
    code: str,
) -> None:
    """Each class of exception a handler can raise answers its own code through the real dispatcher and server.

    Every exception carries the sentinel as its text, so a code that came out of the text would still not show the
    sentinel on the wire; the next test is the one that proves the text never decides.

    Mutations, each one alone, in ``mcp/registry.py``: drop one class from the tuple of its ``except`` clause (its
    row fails; ``TemporalStateNotFoundError`` is the ``temporal state not found`` row); delete the
    ``except ForeignKeyViolation`` clause (the ``postgres foreign key violation`` row answers
    ``tool_execution_failed``); move ``MemoryNotFoundError`` into the ``MCPPreconditionFailedError`` clause; move the
    ``MCPPreconditionFailedError`` clause below the argument clause (the ``review state`` row fails, because
    ``ContinuityReviewStateError`` is a ``ContinuityReviewValidationError``); make ``CheckViolation``
    or a SQLite constraint answer a neighbour class; delete the ``except MCPToolError: raise`` clause, after which the
    last ``ValueError`` clause flattens every coded error a handler raised; widen the policy clause from
    ``AgentPolicyBlockedError`` and ``AgentKeyAuthenticationError`` to ``PermissionError`` (the plain permission
    error row fails). Each fails its own rows.
    """

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise make_exception()

    monkeypatch.setitem(registry._TOOL_HANDLERS, "alice_recall", fail)

    payload = _wire(postgres_url_context, "alice_recall", {"query": "x"})

    assert payload["_is_error"] is True
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == code, label
    assert error["message"] == ("The tool could not be executed" if code == "tool_execution_failed" else _FIXED_MESSAGE)


_TEXT_CASES: tuple[tuple[str, Callable[[], Exception], str], ...] = (
    # The text says "not found", "not permitted" or "required"; the class says nothing, so the answer is generic.
    ("bare tool error that says not found", lambda: MCPToolError("memory was not found"), "tool_request_failed"),
    (
        "commit error that says memory not found",
        lambda: VNextMemoryCommitValidationError("memory was not found"),
        "tool_request_failed",
    ),
    (
        "commit error that says not pending",
        lambda: VNextMemoryCommitValidationError("confirmation is not pending"),
        "tool_request_failed",
    ),
    (
        "value error that says policy blocked",
        lambda: ValueError("agent policy blocked: human_or_admin_review_required"),
        "tool_request_failed",
    ),
    (
        "value error that says required properties",
        lambda: ValueError("tool 'alice_recall' is missing required properties: query"),
        "tool_request_failed",
    ),
    (
        "sqlite error that says foreign key, no code",
        lambda: sqlite3.IntegrityError("FOREIGN KEY constraint failed"),
        "tool_request_failed",
    ),
    (
        "sqlite error that says check, no code",
        lambda: sqlite3.IntegrityError("CHECK constraint failed: memories.memory_type"),
        "tool_request_failed",
    ),
    (
        "permission error that says policy blocked",
        lambda: PermissionError("agent policy blocked this action"),
        "tool_execution_failed",
    ),
    # The opposite: a coded class whose text says nothing of its kind still answers its code.
    ("memory not found class, no words", lambda: MemoryNotFoundError("no words about it"), "not_found"),
    ("memory state class, no words", lambda: MemoryStateError("no words about it"), "precondition_failed"),
)


@pytest.mark.parametrize(("label", "make_exception", "code"), _TEXT_CASES, ids=[row[0] for row in _TEXT_CASES])
def test_the_text_of_an_exception_never_decides_the_code(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url_context: MCPRuntimeContext,
    label: str,
    make_exception: Callable[[], Exception],
    code: str,
) -> None:
    """A message that looks like a missing id or a refusal gets no code unless its class is a typed one.

    The hand-made ``sqlite3.IntegrityError`` carries the words of a foreign key failure and no ``sqlite_errorcode``,
    so it stays generic; a real SQLite error carries the code (see the dispatcher table).

    Mutations, each one alone, in ``mcp/registry.py``: in the last ``ValueError`` clause, answer ``not_found`` when
    ``"not found" in str(exc)`` or ``not_permitted`` when ``"policy" in str(exc)``; decide the SQLite check or foreign
    key branch with ``"CHECK constraint" in str(exc)`` or ``"FOREIGN KEY" in str(exc)`` as well as the error code.
    Each fails a row here.
    """

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise make_exception()

    monkeypatch.setitem(registry._TOOL_HANDLERS, "alice_recall", fail)

    error = _wire(postgres_url_context, "alice_recall", {"query": "x"})["error"]

    assert isinstance(error, dict)
    assert error["code"] == code, label


def test_the_hermes_bridge_demo_sends_the_same_codes_as_the_stdio_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``scripts/run_hermes_mcp_smoke.py`` keeps its own copy of the wire mapping and follows the server's codes.

    Each coded class answers its code with the fixed sentence and never the text; a bare ``MCPToolError`` and a
    subclass whose code is outside the closed set answer ``tool_request_failed``. The sentinel test of
    ``test_mcp_error_contracts.py`` is untouched and covers the bare case.

    Mutations: delete the ``except MCPCodedToolError`` clause of the script (the four coded rows fail with
    ``tool_request_failed``); drop the ``in MCP_CODED_ERROR_CODES`` check (the outside-the-set row fails).
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_hermes_mcp_smoke_typed_codes", _ROOT / "scripts" / "run_hermes_mcp_smoke.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register, _shutdown, registry_ = module._build_local_mcp_compat_runtime()
    monkeypatch.setattr(module, "list_mcp_tools", lambda: [{"name": "alice_recall"}])
    register(
        {
            "alice_core": {
                "env": {"DATABASE_URL": "postgresql://localhost/alicebot", "ALICEBOT_AUTH_USER_ID": _USER_ID},
                "tools": {"include": ["alice_recall"]},
            }
        }
    )

    class _OutsideTheSet(MCPCodedToolError):
        code = "not_a_code_the_server_sends"

    cases = (
        (MCPArgumentError(_SENTINEL), "invalid_request"),
        (MCPNotPermittedError(_SENTINEL), "not_permitted"),
        (MCPReferenceNotFoundError(_SENTINEL), "not_found"),
        (MCPPreconditionFailedError(_SENTINEL), "precondition_failed"),
        (_OutsideTheSet(_SENTINEL), "tool_request_failed"),
        (MCPToolError(_SENTINEL), "tool_request_failed"),
    )
    for exception, code in cases:

        def fail(*_args: object, _exception: Exception = exception, **_kwargs: object) -> dict[str, object]:
            raise _exception

        monkeypatch.setattr(module, "call_mcp_tool", fail)
        raw = registry_.dispatch("mcp_alice_core_alice_recall", {})
        assert _SENTINEL not in raw
        assert json.loads(raw) == {"error": {"code": code, "message": _FIXED_MESSAGE}}, type(exception).__name__


def test_an_unknown_tool_still_answers_tool_not_found(postgres_url_context: MCPRuntimeContext) -> None:
    """The code of a missing tool is not touched: the message and the code are the ones clients read today.

    Mutation: let ``MCPToolNotFoundError`` fall into the coded clause by making it an ``MCPCodedToolError``.
    """

    payload = _wire(postgres_url_context, "alice_not_a_tool", {})

    assert payload["error"] == {"code": "tool_not_found", "message": "The requested tool is not available"}


# --- A real vault: the case the dogfood hit, then each kind of refusal ------------------------------------------


def _store_read(context: MCPRuntimeContext, read: Callable[[SQLiteVNextStore], object]) -> object:
    with sqlite_user_connection(_sqlite_path_from_url(context.database_url), context.user_id) as conn:
        return read(SQLiteVNextStore(conn, context.user_id))


def _policy_events(context: MCPRuntimeContext) -> list[dict[str, object]]:
    events = _store_read(context, lambda store: store.list_events())
    return [dict(event) for event in events if event.get("event_type") == "agent.policy_blocked"]  # type: ignore[attr-defined]


def test_correct_by_a_trusted_local_agent_answers_not_permitted_and_keeps_the_reason_in_the_events(
    sqlite_context: MCPRuntimeContext,
) -> None:
    """The dogfood case: ``alice_memory_correct`` by an agent is refused with ``human_or_admin_review_required``.

    v0.20.0 answered ``tool_request_failed``. Now the wire says ``not_permitted`` and still no reason; the reason is
    in the policy events, and the note is unchanged. The owner (no agent identity) can approve the same note, so the
    refusal belongs to the identity and not to the note.

    Mutations: raise ``MCPToolError`` in place of ``MCPNotPermittedError`` in ``_raise_mcp_policy_blocked``
    (``mcp/policy.py``), which fails the code assertion; delete the ``append_policy_events(...)`` call of
    ``_policy_checked`` in the same file, which fails the events assertion.
    """

    pending = _commit(sqlite_context, confidence=0.7)
    assert pending["status"] == "confirmation_required"
    memory_id = str(pending["memory"]["id"])  # type: ignore[index]
    arguments: dict[str, object] = {"review_item_id": memory_id, "action": "approve"}

    assert _code(sqlite_context, "alice_memory_correct", {**arguments, **_TRUSTED}) == "not_permitted"

    events = _policy_events(sqlite_context)
    assert events, "the refusal must be recorded as an event"
    assert any("human_or_admin_review_required" in json.dumps(event, default=str) for event in events)
    row = _store_read(sqlite_context, lambda store: store.get_memory(memory_id))
    assert row is not None and row["status"] == "needs_review"  # type: ignore[index]

    owner = _wire(sqlite_context, "alice_memory_correct", arguments)
    assert owner["_is_error"] is False


def test_other_policy_and_key_refusals_answer_not_permitted(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only agent's forget, a key the server does not know, and a raw-content read in production.

    Mutations, each one alone: in ``mcp/policy.py``, raise ``MCPToolError`` for ``AgentKeyAuthenticationError`` in
    ``_agent_identity_from_arguments`` (the key rows fail), or ``MCPToolError`` in place of
    ``MCPArgumentError`` for ``AgentIdentityValidationError`` in the keyed branch (the malformed identity row
    fails); in ``mcp/evidence_artifacts.py``, raise ``MCPToolError`` for the ``include_raw_content`` refusal in
    ``_handle_alice_explain`` (the last assertion fails); in ``_raise_mcp_policy_blocked``, raise ``MCPToolError``
    (the read-only forget fails).
    """

    read_only = {
        "agent_id": "reader-typed-codes",
        "agent_type": "personal_assistant",
        "permission_profile": "read_only_agent",
    }
    memory_id = str(_commit(sqlite_context)["memory"]["id"])  # type: ignore[index]
    assert (
        _code(sqlite_context, "alice_memory_manage", {"action": "forget", "memory_id": memory_id, **read_only})
        == "not_permitted"
    )
    # A first commit by a read-only agent is an in-band rejection, not an error; only a blocked replay raises.
    assert (
        _wire(sqlite_context, "alice_memory_commit", {"title": "t", "canonical_text": "x", **read_only})["_is_error"]
        is False
    )

    monkeypatch.setenv("ALICE_AGENT_API_KEY", "alice_sk_a_key_the_vault_does_not_know")
    assert _code(sqlite_context, "alice_recall", {"query": "billing"}) == "not_permitted"
    # A malformed identity is an argument even when a key is set: it is read before the key is checked.
    malformed = {"query": "billing", "agent_id": "someone", "permission_profile": "zzz"}
    assert _code(sqlite_context, "alice_recall", malformed) == "invalid_request"
    assert _code(sqlite_context, "alice_memory_commit", {"title": "t", "canonical_text": "x"}) == "not_permitted"
    monkeypatch.delenv("ALICE_AGENT_API_KEY")

    monkeypatch.setattr(evidence_artifacts, "get_settings", lambda: SimpleNamespace(app_env="production"))
    postgres_context = MCPRuntimeContext(database_url="postgresql://localhost/alicebot", user_id=UUID(_USER_ID))
    assert (
        _code(postgres_context, "alice_explain", {"continuity_object_id": str(uuid4()), "include_raw_content": True})
        == "not_permitted"
    )


def test_a_confirmation_by_the_wrong_agent_and_a_missing_one_answer_differently(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the default three tools, finishing a pending write: unknown id, not the author, and already answered.

    Mutations: in ``vnext_memory_commit.py`` raise the plain ``VNextMemoryCommitValidationError`` in place of
    ``MemoryNotFoundError`` for ``"confirmation was not found"`` (the first assertion fails) or in place of
    ``MemoryStateError`` for ``"confirmation is not pending"`` (the last fails); in ``mcp/policy.py`` raise
    ``MCPToolError`` in ``_raise_mcp_policy_blocked`` (the middle one fails).
    """

    monkeypatch.delenv("ALICE_MCP_FULL_TOOLS", raising=False)
    author = {
        "agent_id": "author-typed-codes",
        "agent_type": "coding_agent",
        "permission_profile": "trusted_local_agent",
    }
    pending = _commit(sqlite_context, confidence=0.7, **author)
    confirmation_id = str(pending["memory"]["metadata_json"]["agentic_memory"]["confirmation"]["confirmation_id"])  # type: ignore[index]

    unknown = {"confirmation_id": "confirm-" + str(uuid4()), "confirmation_action": "confirm", **author}
    assert _code(sqlite_context, "alice_memory_commit", unknown) == "not_found"

    other = {"agent_id": "other-typed-codes", "agent_type": "coding_agent", "permission_profile": "trusted_local_agent"}
    wrong_agent = {"confirmation_id": confirmation_id, "confirmation_action": "confirm", **other}
    assert _code(sqlite_context, "alice_memory_commit", wrong_agent) == "not_permitted"

    right_agent = {"confirmation_id": confirmation_id, "confirmation_action": "confirm", **author}
    assert _wire(sqlite_context, "alice_memory_commit", right_agent)["_is_error"] is False
    assert _code(sqlite_context, "alice_memory_commit", right_agent) == "precondition_failed"


def test_missing_ids_answer_not_found(sqlite_context: MCPRuntimeContext) -> None:
    """A memory, a review item, an open loop and a pending confirmation that do not exist.

    Mutations, each one alone: in ``mcp/review.py`` raise ``MCPArgumentError`` in place of
    ``MCPReferenceNotFoundError`` for ``memory {memory_id} was not found`` in ``_vnext_memory_correct`` (the
    ``correct`` row fails) or in ``_vnext_memory_review`` (the ``review`` row); in ``mcp/retrieval.py`` do the same
    for ``open loop ... was not found`` (the ``open loop`` row). The commit service's own lookups have their own
    test below.
    """

    missing = str(uuid4())
    cases = (
        ("alice_memory_correct", {"review_item_id": missing, "action": "approve"}),
        ("alice_memory_review", {"review_item_id": missing}),
        ("alice_memory_manage", {"action": "forget", "memory_id": missing}),
        ("alice_memory_manage", {"action": "undo", "memory_id": missing}),
        ("alice_explain", {"memory_id": missing}),
        ("alice_open_loops", {"action": "close", "loop_id": missing}),
    )
    for name, arguments in cases:
        assert _code(sqlite_context, name, arguments) == "not_found", (name, arguments)


def test_each_service_lookup_of_a_missing_memory_is_not_found(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every action of ``alice_memory_manage``, the audit and the confirmation lookup answer ``not_found``.

    The actions are read from ``_MEMORY_MANAGE_ACTIONS``, so an eighth action fails the first assertion until it has
    a row here.

    Mutations, each one alone, in ``vnext_memory_commit.py``: raise the plain
    ``VNextMemoryCommitValidationError`` in place of ``MemoryNotFoundError`` at one site (``confirm``, ``undo``,
    the superseding memory of ``undo``, ``correct`` (reached through the legacy tool), ``forget``,
    ``accept_consolidation``, ``expire``, ``unexpire``, ``audit``). The row of that action fails. In
    ``mcp/memories.py`` raise the plain ``VNextMemoryCommitValidationError`` in place of ``MemoryNotFoundError`` at
    the missing-memory check of ``redact_memory_flow`` (the ``redact`` row fails). The two sites that read a row
    again after taking its lock (``confirm`` and ``undo``) are reached by the last two assertions, which make the
    locked read answer nothing.
    """

    missing = str(uuid4())
    memory_id = str(_commit(sqlite_context)["memory"]["id"])  # type: ignore[index]
    pending = _commit(sqlite_context, confidence=0.7, canonical_text="A pending fact about billing.")
    confirmation_id = str(pending["memory"]["metadata_json"]["agentic_memory"]["confirmation"]["confirmation_id"])  # type: ignore[index]
    cases = (
        ("alice_memory_manage", {"action": "confirm", "confirmation_id": "confirm-" + missing}),
        ("alice_memory_manage", {"action": "undo", "memory_id": missing}),
        ("alice_memory_manage", {"action": "undo", "memory_id": memory_id, "superseded_by": missing}),
        ("alice_memory_manage", {"action": "forget", "memory_id": missing}),
        ("alice_memory_manage", {"action": "expire", "memory_id": missing, "reason": "no longer true"}),
        ("alice_memory_manage", {"action": "unexpire", "memory_id": missing, "reason": "still true"}),
        ("alice_memory_manage", {"action": "accept_consolidation", "memory_id": missing, "reason": "looks right"}),
        ("alice_memory_manage", {"action": "redact", "memory_id": missing, "reason": "the owner asked for removal"}),
        ("alice_explain", {"memory_id": missing}),
        ("alice_memory_commit", {"confirmation_id": "confirm-" + missing, "confirmation_action": "confirm"}),
    )
    from alicebot_api.mcp.memories import _MEMORY_MANAGE_ACTIONS

    covered = {str(arguments["action"]) for name, arguments in cases if name == "alice_memory_manage"}
    assert covered == set(_MEMORY_MANAGE_ACTIONS), sorted(set(_MEMORY_MANAGE_ACTIONS) ^ covered)
    for name, arguments in cases:
        assert _code(sqlite_context, name, arguments) == "not_found", (name, arguments)

    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    legacy_correct = {"memory_id": missing, "canonical_text": "A corrected fact."}
    assert _code(sqlite_context, "alice_vnext_correct_memory", legacy_correct) == "not_found"

    monkeypatch.setattr(SQLiteVNextStore, "get_memory_for_update", lambda _self, _memory_id: None, raising=False)
    assert _code(sqlite_context, "alice_memory_manage", {"action": "undo", "memory_id": memory_id}) == "not_found"
    finish = {"confirmation_id": confirmation_id, "confirmation_action": "confirm"}
    assert _code(sqlite_context, "alice_memory_commit", finish) == "not_found"


def test_a_confirmation_answered_twice_is_a_state_whichever_answer_came_first(
    sqlite_context: MCPRuntimeContext,
) -> None:
    """Confirm after reject, reject after confirm and confirm after confirm are ``precondition_failed``.

    Rejecting twice is a replay and succeeds. The orders reach two different checks in
    ``VNextMemoryCommitService.confirm``.

    Mutations: raise the plain ``VNextMemoryCommitValidationError`` in place of ``MemoryStateError`` at the first
    ``confirmation is not pending`` (before the policy check; the confirm rows fail) or at the second (after the
    replay lookup; the reject-after-confirm row fails).
    """

    def finish(confirmation_id: str, action: str) -> dict[str, object]:
        return {"confirmation_id": confirmation_id, "confirmation_action": action}

    def pending_id(text: str) -> str:
        row = _commit(sqlite_context, confidence=0.7, canonical_text=text)
        return str(row["memory"]["metadata_json"]["agentic_memory"]["confirmation"]["confirmation_id"])  # type: ignore[index]

    rejected_first = pending_id("A fact that is rejected first.")
    assert _wire(sqlite_context, "alice_memory_commit", finish(rejected_first, "reject"))["_is_error"] is False
    assert _code(sqlite_context, "alice_memory_commit", finish(rejected_first, "confirm")) == "precondition_failed"
    assert _wire(sqlite_context, "alice_memory_commit", finish(rejected_first, "reject"))["_is_error"] is False

    confirmed_first = pending_id("A fact that is confirmed first.")
    assert _wire(sqlite_context, "alice_memory_commit", finish(confirmed_first, "confirm"))["_is_error"] is False
    assert _code(sqlite_context, "alice_memory_commit", finish(confirmed_first, "reject")) == "precondition_failed"
    assert _code(sqlite_context, "alice_memory_commit", finish(confirmed_first, "confirm")) == "precondition_failed"


def test_a_pending_project_update_cannot_be_forgotten_and_says_so_as_a_state(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit service keeps a pending coupled project update on its own review path, as a state refusal.

    Mutation: raise the plain ``VNextMemoryCommitValidationError`` in place of ``MemoryStateError`` in
    ``_require_project_update_decision_path`` (``vnext_memory_commit.py``).
    """

    from alicebot_api import vnext_memory_commit

    memory_id = str(_commit(sqlite_context)["memory"]["id"])  # type: ignore[index]
    monkeypatch.setattr(vnext_memory_commit, "is_pending_project_update_memory", lambda _memory: True)

    code = _code(sqlite_context, "alice_memory_manage", {"action": "forget", "memory_id": memory_id})

    assert code == "precondition_failed"


def test_a_pending_project_update_cannot_be_redacted_and_says_so_as_a_state(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``redact`` refuses a pending coupled project update, and an open artifact of one, as a state refusal.

    ``redact_memory_flow`` has its own two checks, apart from the commit service's, so each needs its own row.

    Mutations, each one alone, in ``mcp/memories.py``: raise the plain ``VNextMemoryCommitValidationError`` in place
    of ``MemoryStateError`` at the ``is_pending_project_update_memory`` check (the first assertion fails) or at the
    check of the artifacts that are neither accepted nor rejected (the second fails).
    """

    from alicebot_api.mcp import memories as mcp_memories

    memory_id = str(_commit(sqlite_context)["memory"]["id"])  # type: ignore[index]
    redact = {"action": "redact", "memory_id": memory_id, "reason": "the owner asked for removal"}

    with monkeypatch.context() as patch:
        patch.setattr(mcp_memories, "is_pending_project_update_memory", lambda _memory: True)
        assert _code(sqlite_context, "alice_memory_manage", redact) == "precondition_failed"

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteVNextStore,
            "lock_project_update_artifacts_for_redaction",
            lambda _self, _memory_id: [{"id": str(uuid4()), "status": "pending"}],
            raising=False,
        )
        assert _code(sqlite_context, "alice_memory_manage", redact) == "precondition_failed"

    # Past both state checks, redact is for a human or an admin agent: a trusted agent is refused by policy.
    assert _code(sqlite_context, "alice_memory_manage", {**redact, **_TRUSTED}) == "not_permitted"


def test_each_status_refusal_of_a_continuity_correction_is_a_state_error_and_a_bad_edit_is_not() -> None:
    """A correction the object's status forbids is a state error; a correction with nothing to change is an argument.

    ``ContinuityReviewValidationError`` was one class for both, so the first answer an agent got for a retired object
    was ``invalid_request``, "fix the call and retry", for a call that cannot succeed. The class of each raise is
    checked exactly, so the base class does not pass for the subclass.

    Mutations, each one alone, in ``continuity_review.py``: change one of the five ``raise
    ContinuityReviewStateError`` (confirm, edit, delete, mark_stale, supersede) back to
    ``ContinuityReviewValidationError`` (its row fails); change the ``edit requires at least one of`` raise to
    ``ContinuityReviewStateError`` (the last assertion fails).
    """

    from tests.unit.test_continuity_review import ContinuityReviewStoreStub

    user_id = UUID("11111111-1111-4111-8111-111111111111")

    def raised(status: str, action: str, **fields: object) -> BaseException:
        store = ContinuityReviewStoreStub()
        row = store.add_object(title="Decision: Retired", status=status)
        with pytest.raises(ContinuityReviewValidationError) as caught:
            apply_continuity_correction(
                store,  # type: ignore[arg-type]
                user_id=user_id,
                continuity_object_id=row["id"],
                request=ContinuityCorrectionInput(action=action, **fields),  # type: ignore[arg-type]
            )
        return caught.value

    for action in ("confirm", "edit", "delete", "supersede"):
        assert type(raised("deleted", action, title="x")) is ContinuityReviewStateError, action
    assert type(raised("stale", "mark_stale")) is ContinuityReviewStateError

    assert type(raised("active", "edit")) is ContinuityReviewValidationError


def test_a_review_item_the_filters_hide_answers_the_same_as_one_that_is_not_there(
    sqlite_context: MCPRuntimeContext,
) -> None:
    """A memory outside the caller's review filters is ``not_found``, not ``not_permitted``.

    A refusal would tell the caller the row exists. The filters are the caller's own, so the answer is the one a
    missing row gets.

    Mutation: raise ``MCPNotPermittedError`` for ``memory review item is outside the effective review filters`` in
    ``_vnext_memory_review`` (``mcp/review.py``).
    """

    memory_id = str(_commit(sqlite_context, domain="project")["memory"]["id"])  # type: ignore[index]

    code = _code(sqlite_context, "alice_memory_review", {"review_item_id": memory_id, "domains": ["professional"]})

    assert code == "not_found"


def test_a_key_bound_explain_stays_uniform_and_a_keyless_one_gets_the_code(
    sqlite_context: MCPRuntimeContext,
) -> None:
    """``alice_explain`` for a key-bound agent answers one generic code for a missing and a refused target.

    A key-bound caller sits across a trust boundary, and since the v0.10.4 audit remediation ``alice_explain`` has
    given it one answer for a missing and an unreadable target, because that tool expands related rows. A keyless
    caller is local owner tooling and gets the code. The other tools that take an id do not hide the difference; the
    next test pins that, on purpose.

    Mutations: in ``_handle_alice_vnext_memory_audit`` (``mcp/evidence_artifacts.py``), delete the key-bound branch
    before ``raise validation_error`` (the first assertion fails with ``not_found``); in
    ``_raise_explain_authorization_error`` raise ``MCPNotPermittedError`` in the key-bound branch (the
    ``type(...) is MCPToolError`` assertion fails).
    """

    missing = str(uuid4())
    key_bound = AgentIdentity(
        agent_id="keyed-typed-codes", permission_profile="trusted_local_agent", auth="agent_api_key"
    )
    context = MCPRuntimeContext(
        database_url=sqlite_context.database_url,
        user_id=sqlite_context.user_id,
        agent_identity=key_bound,
        agent_identity_resolved=True,
    )

    assert _code(context, "alice_explain", {"memory_id": missing}) == "tool_request_failed"
    assert _code(sqlite_context, "alice_explain", {"memory_id": missing, **_TRUSTED}) == "not_found"

    decision = PolicyDecision(decision="blocked", action="memory.explain", permission_profile="x", reasons=("r",))
    error = evidence_artifacts._ExplainAuthorizationError(decision)
    with pytest.raises(MCPToolError) as keyed:
        evidence_artifacts._raise_explain_authorization_error(key_bound, error)
    assert type(keyed.value) is MCPToolError
    with pytest.raises(MCPNotPermittedError):
        evidence_artifacts._raise_explain_authorization_error(
            AgentIdentity(agent_id="keyless-typed-codes", permission_profile="trusted_local_agent"), error
        )


def test_a_key_bound_agent_cannot_tell_a_refused_id_from_a_missing_one(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real key bound to one project, an id in another project and a random id answer the same ``not_found`` on
    review, correct and manage, as they do on ``alice_explain``, which answers ``tool_request_failed`` for both.

    An id that a row of another project, or a row above the key's ceiling, is named by may be listed in the metadata of a
    row the key can read, so the answer to it must say no more than the answer to a random id. This replaces the second
    review of PR 528, which let a key tell ``not_permitted`` (a refused id) from ``not_found`` (a missing one) on these
    tools. ``not_permitted`` stays for a row the key may read and the policy still refuses, which
    ``test_mcp_refusal_is_independent_of_state.py`` pins.

    Mutations, each one alone: in ``VNextMemoryCommitService._record_write_decision`` (``vnext_memory_commit.py``) raise
    ``AgentPolicyBlockedError`` in place of ``RefusedAsMissingError`` (the ``forget`` and ``unexpire`` outside rows fail
    with ``not_permitted``); in ``_vnext_memory_review`` (``mcp/review.py``) set ``hide_the_row`` to ``False`` (the
    ``review`` row fails); in ``_vnext_memory_correct`` set ``hide_the_row`` to ``False`` after the first policy check
    (the ``correct`` row fails); in ``_handle_alice_vnext_memory_audit`` delete the key-bound branch (the explain rows
    fail with ``not_found``).
    """

    from alicebot_api.mcp.runtime import _vnext_store_context
    from alicebot_api.vnext_agent_keys import create_agent_key

    other_project_id = str(_commit(sqlite_context, project_scope=["other-project"])["memory"]["id"])  # type: ignore[index]
    inside = _commit(sqlite_context, canonical_text="A fact inside the key's project.", project_scope=["alicebot"])
    inside_id = str(inside["memory"]["id"])  # type: ignore[index]
    missing = str(uuid4())
    seen: dict[str, dict[str, str]] = {}
    for profile in ("project_scoped_agent", "trusted_local_agent", "read_only_agent", "admin_agent"):
        with _vnext_store_context(sqlite_context) as store:
            _record, raw_key = create_agent_key(
                store,  # type: ignore[arg-type]
                user_id=UUID(_USER_ID),
                agent_id=f"keyed-{profile}",
                permission_profile=profile,
                project_scope="alicebot",
            )
        monkeypatch.setenv("ALICE_AGENT_API_KEY", raw_key)
        # The control: the same key reads an id inside its own project, so the refusals below are about the project.
        assert _wire(sqlite_context, "alice_memory_review", {"review_item_id": inside_id})["_is_error"] is False
        codes: dict[str, str] = {}
        for label, name, arguments in (
            ("review", "alice_memory_review", {"review_item_id": "{id}"}),
            ("correct", "alice_memory_correct", {"review_item_id": "{id}", "action": "approve"}),
            ("forget", "alice_memory_manage", {"action": "forget", "memory_id": "{id}"}),
            ("unexpire", "alice_memory_manage", {"action": "unexpire", "memory_id": "{id}", "reason": "still true"}),
            ("explain", "alice_explain", {"memory_id": "{id}"}),
        ):
            for kind, target in (("outside", other_project_id), ("missing", missing)):
                filled = {key: target if value == "{id}" else value for key, value in arguments.items()}
                codes[f"{label} {kind}"] = _code(sqlite_context, name, filled)
        seen[profile] = codes
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)

    expected = {
        **{f"{label} outside": "not_found" for label in ("review", "correct", "forget", "unexpire")},
        **{f"{label} missing": "not_found" for label in ("review", "correct", "forget", "unexpire")},
        "explain outside": "tool_request_failed",
        "explain missing": "tool_request_failed",
    }
    assert seen == {
        "project_scoped_agent": expected,
        "trusted_local_agent": expected,
        "read_only_agent": expected,
        "admin_agent": expected,
    }


def test_a_key_bound_explain_of_a_missing_continuity_target_stays_uniform(
    postgres_url_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two Postgres explain paths (entity and continuity object) keep one generic answer for a key-bound caller.

    The stores are replaced: the authorization step passes and the explain function finds nothing. A key-bound
    caller gets ``tool_request_failed``; a keyless one gets ``not_found``.

    Mutations: in ``_handle_alice_explain`` (``mcp/evidence_artifacts.py``), delete the key-bound check in the
    ``except LookupError`` of the entity path, or of the continuity-object path. The matching row fails.
    """

    from contextlib import contextmanager

    @contextmanager
    def fake_store(_context: object):
        yield object()

    def not_found(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ContinuityEvidenceNotFoundError(_SENTINEL)

    monkeypatch.setattr(evidence_artifacts, "_store_context", fake_store)
    monkeypatch.setattr(evidence_artifacts, "get_temporal_explain", not_found)
    monkeypatch.setattr(evidence_artifacts, "build_continuity_explain", not_found)
    monkeypatch.setattr(evidence_artifacts, "_authorize_entity_explain_target", lambda *_a, **_k: None)
    monkeypatch.setattr(evidence_artifacts, "_authorize_continuity_explain_target", lambda *_a, **_k: None)
    key_bound = AgentIdentity(
        agent_id="keyed-typed-codes", permission_profile="trusted_local_agent", auth="agent_api_key"
    )
    keyed = MCPRuntimeContext(
        database_url=postgres_url_context.database_url,
        user_id=postgres_url_context.user_id,
        agent_identity=key_bound,
        agent_identity_resolved=True,
    )

    for arguments in ({"entity_id": str(uuid4())}, {"continuity_object_id": str(uuid4())}):
        assert _code(keyed, "alice_explain", arguments) == "tool_request_failed", arguments
        assert _code(postgres_url_context, "alice_explain", {**arguments, **_TRUSTED}) == "not_found", arguments


def test_a_missing_entity_is_not_found_through_the_real_temporal_functions(
    postgres_url_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``get_temporal_explain``, ``get_temporal_state_at`` and ``get_temporal_timeline`` raise
    ``TemporalStateNotFoundError`` for an entity the store does not hold, or one created after the instant asked for.

    The store is a stub with the one method the loader calls, so the real function raises the real class. A keyless
    caller gets ``not_found`` for every one of them, with a declared identity too. A key-bound ``alice_explain`` caller
    stays on the one opaque answer by decision. The test above replaces the explain function with one that raises a
    different class, so it could not see this.

    Mutation: delete ``TemporalStateNotFoundError`` from the ``MCPReferenceNotFoundError`` tuple in
    ``mcp/registry.py`` (every keyless row answers ``tool_execution_failed``); in ``_handle_alice_explain``
    (``mcp/evidence_artifacts.py``) delete the key-bound check in the ``except LookupError`` of the entity path (the
    key-bound row answers ``not_found``).
    """

    from contextlib import contextmanager
    from datetime import UTC, datetime

    from alicebot_api.mcp import retrieval

    class _EntityStore:
        """The one store method ``temporal_state._load_entity`` calls."""

        def __init__(self, entity: dict[str, object] | None) -> None:
            self.entity = entity

        def get_entity_optional(self, _entity_id: UUID) -> dict[str, object] | None:
            return self.entity

    def serve(entity: dict[str, object] | None) -> None:
        @contextmanager
        def fake_store(_context: object):
            yield _EntityStore(entity)

        monkeypatch.setattr(evidence_artifacts, "_store_context", fake_store)
        monkeypatch.setattr(retrieval, "_store_context", fake_store)

    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    monkeypatch.setattr(evidence_artifacts, "_authorize_entity_explain_target", lambda *_a, **_k: None)
    entity_id = str(uuid4())
    later = {"id": entity_id, "created_at": datetime(2030, 1, 1, tzinfo=UTC)}
    key_bound = AgentIdentity(
        agent_id="keyed-typed-codes", permission_profile="trusted_local_agent", auth="agent_api_key"
    )
    keyed = MCPRuntimeContext(
        database_url=postgres_url_context.database_url,
        user_id=postgres_url_context.user_id,
        agent_identity=key_bound,
        agent_identity_resolved=True,
    )
    long_ago = "2020-01-01T00:00:00Z"

    serve(None)
    for tool, arguments in (
        ("alice_explain", {"entity_id": entity_id}),
        ("alice_state_at", {"entity_id": entity_id}),
        ("alice_timeline", {"entity_id": entity_id}),
    ):
        assert _code(postgres_url_context, tool, arguments) == "not_found", tool
    # Only alice_explain takes an identity; the declared one changes nothing.
    assert _code(postgres_url_context, "alice_explain", {"entity_id": entity_id, **_TRUSTED}) == "not_found"
    assert _code(keyed, "alice_explain", {"entity_id": entity_id}) == "tool_request_failed"

    serve(later)
    for tool in ("alice_explain", "alice_state_at"):
        arguments = {"entity_id": entity_id, "at": long_ago}
        assert _code(postgres_url_context, tool, arguments) == "not_found", tool
    assert _code(keyed, "alice_explain", {"entity_id": entity_id, "at": long_ago}) == "tool_request_failed"


def test_a_state_that_forbids_the_call_answers_precondition_failed(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second forget, an entity explain on SQLite, and a tool the SQLite backend does not serve.

    Mutations, each one alone: in ``vnext_memory_commit.py`` raise the plain base class in ``_require_transition``
    (the second ``forget`` fails); in ``mcp/evidence_artifacts.py`` raise ``MCPArgumentError`` for the SQLite
    entity branch of ``_handle_alice_explain`` (the entity row fails); in ``mcp/runtime.py`` raise ``MCPToolError``
    in ``_store_context`` (the legacy rows fail).
    """

    memory_id = str(_commit(sqlite_context)["memory"]["id"])  # type: ignore[index]
    forget = {"action": "forget", "memory_id": memory_id}
    assert _wire(sqlite_context, "alice_memory_manage", forget)["_is_error"] is False
    assert _code(sqlite_context, "alice_memory_manage", forget) == "precondition_failed"

    assert _code(sqlite_context, "alice_explain", {"entity_id": str(uuid4())}) == "precondition_failed"

    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    assert _code(sqlite_context, "alice_vnext_scheduler_status", {}) == "precondition_failed"
    assert _code(sqlite_context, "alice_capture_candidates", {}) == "precondition_failed"


def test_an_explain_with_no_target_is_an_argument_error_on_every_backend(sqlite_context: MCPRuntimeContext) -> None:
    """``alice_explain`` with none of the three ids is ``invalid_request``, not a backend refusal.

    Mutation: delete the ``if not provided`` branch of the SQLite check in ``_handle_alice_explain``
    (``mcp/evidence_artifacts.py``); the call then answers ``precondition_failed``.
    """

    assert _code(sqlite_context, "alice_explain", {}) == "invalid_request"
    assert _code(sqlite_context, "alice_explain", {"memory_id": "a", "entity_id": str(uuid4())}) == "invalid_request"


def test_a_rejected_argument_answers_invalid_request_at_each_layer(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema check, the parsers and the handlers' own checks all answer ``invalid_request``.

    Mutations, each one alone: in ``mcp/registry.py`` raise ``MCPToolError`` in the ``missing required properties``
    branch (the first row), in ``fail()`` (the type and range rows) or in the ``additional properties`` branch; in
    ``mcp/arguments.py`` raise ``MCPToolError`` in ``_parse_required_text`` (the ``alice_vnext_propose_memory``
    row is the one that reaches it; the second to last row reaches ``_parse_optional_text``); in
    ``mcp/memories.py`` raise ``MCPToolError`` for the mixed confirmation fields; in ``mcp/retrieval.py`` for the
    open-loop action.
    """

    cases = (
        ("alice_recall", {}),  # missing required property
        ("alice_recall", {"query": "x", "zzz": 1}),  # additional property
        ("alice_recall", {"query": "x", "limit": 0}),  # out of range
        ("alice_recall", {"query": 7}),  # wrong type
        ("alice_recall", {"query": "x", "project": 7}),  # wrong type in a parsed field
        ("alice_memory_manage", {"action": "nonsense", "memory_id": "x"}),
        ("alice_open_loops", {"action": "nonsense"}),
        ("alice_memory_review", {"status": "nonsense"}),
        ("alice_explain", {"memory_id": "a", "entity_id": str(uuid4())}),
        ("alice_memory_commit", {"confirmation_id": "c", "confirmation_action": "confirm", "title": "no"}),
        ("alice_memory_commit", {"confirmation_id": "c", "confirmation_action": "maybe"}),
        ("alice_memory_commit", {"confirmation_action": "confirm"}),
    )
    for name, arguments in cases:
        assert _code(sqlite_context, name, arguments) == "invalid_request", (name, arguments)

    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    legacy_cases = (
        ("alice_vnext_propose_memory", {"canonical_text": "x"}),  # no agent identity
        ("alice_vnext_propose_memory", {"canonical_text": "x", "agent_id": "a", "permission_profile": "zzz"}),
        ("alice_vnext_scheduler_run_due", {"limit": "x"}),
        ("alice_commit_captures", {"mode": "nonsense"}),
        ("alice_memory_mutations_commit", {"candidate_ids": "x"}),
    )
    for name, arguments in legacy_cases:
        assert _code(sqlite_context, name, arguments) == "invalid_request", (name, arguments)


# --- Each handler's own checks, called directly so the schema check cannot answer first -------------------------


def _handler_cases() -> list[tuple[str, str, str, dict[str, object], type[MCPCodedToolError]]]:
    """(label, handler path, context kind, arguments, the class it must raise)."""

    missing = str(uuid4())
    return [
        (
            "propose needs an identity",
            "memories._handle_alice_vnext_propose_memory",
            "sqlite",
            {"canonical_text": "x"},
            MCPArgumentError,
        ),
        ("manage action", "memories._handle_alice_memory_manage", "sqlite", {"action": "zzz"}, MCPArgumentError),
        (
            "finish: mixed fields",
            "memories._finish_pending_commit",
            "sqlite",
            {"confirmation_id": "c", "confirmation_action": "confirm", "title": "t"},
            MCPArgumentError,
        ),
        (
            "finish: no id",
            "memories._finish_pending_commit",
            "sqlite",
            {"confirmation_action": "confirm"},
            MCPArgumentError,
        ),
        (
            "finish: bad action",
            "memories._finish_pending_commit",
            "sqlite",
            {"confirmation_id": "c", "confirmation_action": "zzz"},
            MCPArgumentError,
        ),
        ("compare mode", "retrieval._handle_alice_task_brief_compare", "sqlite", {}, MCPArgumentError),
        ("open loop action", "retrieval._handle_alice_open_loops", "sqlite", {"action": "zzz"}, MCPArgumentError),
        (
            "open loop missing",
            "retrieval._handle_alice_open_loops",
            "sqlite",
            {"action": "close", "loop_id": missing},
            MCPReferenceNotFoundError,
        ),
        (
            "ingest needs an identity",
            "capture_automation._handle_alice_vnext_ingest_agent_output",
            "sqlite",
            {},
            MCPArgumentError,
        ),
        (
            "workflow type",
            "capture_automation._handle_alice_vnext_generate_artifact",
            "sqlite",
            {"workflow_type": "zzz"},
            MCPArgumentError,
        ),
        (
            "captures mode",
            "capture_mutations._handle_alice_commit_captures",
            "sqlite",
            {"mode": "zzz"},
            MCPArgumentError,
        ),
        (
            "captures list",
            "capture_mutations._handle_alice_commit_captures",
            "sqlite",
            {"candidates": "x"},
            MCPArgumentError,
        ),
        (
            "captures item",
            "capture_mutations._handle_alice_commit_captures",
            "sqlite",
            {"candidates": ["x"]},
            MCPArgumentError,
        ),
        (
            "mutations mode",
            "capture_mutations._handle_alice_memory_mutations_generate",
            "sqlite",
            {"mode": "zzz"},
            MCPArgumentError,
        ),
        (
            "mutation ids list",
            "capture_mutations._handle_alice_memory_mutations_commit",
            "sqlite",
            {"candidate_ids": "x"},
            MCPArgumentError,
        ),
        (
            "mutation ids item",
            "capture_mutations._handle_alice_memory_mutations_commit",
            "sqlite",
            {"candidate_ids": [7]},
            MCPArgumentError,
        ),
        (
            "mutation ids uuid",
            "capture_mutations._handle_alice_memory_mutations_commit",
            "sqlite",
            {"candidate_ids": ["nope"]},
            MCPArgumentError,
        ),
        (
            "scheduler status",
            "scheduler._handle_alice_vnext_scheduler_status",
            "sqlite",
            {},
            MCPPreconditionFailedError,
        ),
        (
            "scheduler run now",
            "scheduler._handle_alice_vnext_scheduler_run_now",
            "sqlite",
            {},
            MCPPreconditionFailedError,
        ),
        (
            "scheduler run due",
            "scheduler._handle_alice_vnext_scheduler_run_due",
            "sqlite",
            {},
            MCPPreconditionFailedError,
        ),
        ("scheduler pause", "scheduler._handle_alice_vnext_scheduler_pause", "sqlite", {}, MCPPreconditionFailedError),
        (
            "scheduler resume",
            "scheduler._handle_alice_vnext_scheduler_resume",
            "sqlite",
            {},
            MCPPreconditionFailedError,
        ),
        (
            "scheduler limit",
            "scheduler._handle_alice_vnext_scheduler_run_due",
            "postgres",
            {"limit": "x"},
            MCPArgumentError,
        ),
        ("review status type", "review._vnext_memory_review", "sqlite", {"status": 7}, MCPArgumentError),
        ("review status value", "review._vnext_memory_review", "sqlite", {"status": "zzz"}, MCPArgumentError),
        (
            "correct: unsupported action",
            "review._vnext_memory_correct",
            "sqlite",
            {"review_item_id": missing, "action": "mark_stale"},
            MCPArgumentError,
        ),
        (
            "explain two ids",
            "evidence_artifacts._handle_alice_explain",
            "sqlite",
            {"memory_id": "a", "entity_id": missing},
            MCPArgumentError,
        ),
        ("explain no id on postgres", "evidence_artifacts._handle_alice_explain", "postgres", {}, MCPArgumentError),
        (
            "explain entity on sqlite",
            "evidence_artifacts._handle_alice_explain",
            "sqlite",
            {"entity_id": missing},
            MCPPreconditionFailedError,
        ),
        (
            "artifact inspect raw content",
            "evidence_artifacts._handle_alice_artifact_inspect",
            "postgres",
            {"include_raw_content": True},
            MCPNotPermittedError,
        ),
        (
            "explain raw content",
            "evidence_artifacts._handle_alice_explain",
            "postgres",
            {"continuity_object_id": missing, "include_raw_content": True},
            MCPNotPermittedError,
        ),
    ]


@pytest.mark.parametrize(
    ("label", "handler_path", "context_kind", "arguments", "expected"),
    _handler_cases(),
    ids=[row[0] for row in _handler_cases()],
)
def test_each_handler_check_raises_the_class_of_its_kind(
    monkeypatch: pytest.MonkeyPatch,
    sqlite_context: MCPRuntimeContext,
    postgres_url_context: MCPRuntimeContext,
    label: str,
    handler_path: str,
    context_kind: str,
    arguments: dict[str, object],
    expected: type[MCPCodedToolError],
) -> None:
    """A check inside a handler names the kind of its refusal; the exact class is compared.

    The Postgres context is a URL nothing connects to: every row raises before a store is opened. The settings are
    patched to a production environment for the two raw-content rows.

    Mutations: in the handler of a row, change the raised class to a neighbour (for example ``MCPArgumentError`` to
    ``MCPToolError`` in ``_handle_alice_memory_manage``, ``MCPPreconditionFailedError`` to ``MCPArgumentError`` in a
    scheduler handler, ``MCPNotPermittedError`` to ``MCPToolError`` for ``include_raw_content``). The row of that
    handler fails, and no other row does.
    """

    module_name, function_name = handler_path.split(".")
    module = __import__(f"alicebot_api.mcp.{module_name}", fromlist=[function_name])
    handler = getattr(module, function_name)
    monkeypatch.setattr(evidence_artifacts, "get_settings", lambda: SimpleNamespace(app_env="production"))
    context = sqlite_context if context_kind == "sqlite" else postgres_url_context

    with pytest.raises(MCPToolError) as caught:
        handler(context, arguments)

    assert type(caught.value) is expected, label


def test_the_postgres_only_store_context_refuses_a_sqlite_vault_as_a_state(sqlite_context: MCPRuntimeContext) -> None:
    """A tool that needs the Postgres backend, called on a SQLite vault, is ``precondition_failed``.

    Mutation: raise ``MCPToolError`` in place of ``MCPPreconditionFailedError`` in ``_store_context``
    (``mcp/runtime.py``).
    """

    from alicebot_api.mcp.runtime import _store_context

    with pytest.raises(MCPToolError) as caught:
        with _store_context(sqlite_context):
            pass

    assert type(caught.value) is MCPPreconditionFailedError


def test_a_missing_artifact_is_not_found_before_any_policy_check() -> None:
    """The artifact lookup answers ``not_found`` before it asks a policy question about a row that is not there.

    Mutation: raise ``MCPToolError`` in place of ``MCPReferenceNotFoundError`` in ``_authorize_vnext_artifact_target``
    (``mcp/evidence_artifacts.py``).
    """

    class _NoArtifacts:
        def get_artifact(self, _artifact_id: str) -> None:
            return None

        get_artifact_for_update = get_artifact

    for for_update in (False, True):
        with pytest.raises(MCPToolError) as caught:
            evidence_artifacts._authorize_vnext_artifact_target(
                _NoArtifacts(),  # type: ignore[arg-type]
                identity=None,
                artifact_id=str(uuid4()),
                action="artifact.read",
                for_update=for_update,
            )
        assert type(caught.value) is MCPReferenceNotFoundError


# --- The review tool: its own checks and the provenance it validates -------------------------------------------


class _ProvenanceStore:
    """The two reads ``_validated_review_provenance`` makes, with one source that holds one chunk."""

    def __init__(self, *, sources: dict[str, dict[str, object]], chunks: dict[str, list[dict[str, object]]]) -> None:
        self._sources = sources
        self._chunks = chunks

    def get_source(self, source_id: str) -> dict[str, object] | None:
        return self._sources.get(source_id)

    def list_source_chunks(self, source_id: str) -> list[dict[str, object]]:
        return self._chunks.get(source_id, [])


def _provenance_cases() -> list[tuple[str, dict[str, object], type[MCPCodedToolError], bool]]:
    source_id = str(uuid4())
    chunk_id = str(uuid4())
    other_chunk = str(uuid4())
    return [
        ("no source id", {}, MCPArgumentError, True),
        ("source id not a uuid", {"source_id": "nope"}, MCPArgumentError, True),
        ("source not found", {"source_id": str(uuid4())}, MCPReferenceNotFoundError, True),
        ("chunk id wrong type", {"source_id": source_id, "source_chunk_id": 7}, MCPArgumentError, True),
        ("chunk id not a uuid", {"source_id": source_id, "source_chunk_id": "nope"}, MCPArgumentError, True),
        (
            "chunk of another source",
            {"source_id": source_id, "source_chunk_id": other_chunk},
            MCPReferenceNotFoundError,
            True,
        ),
        (
            "store cannot list chunks",
            {"source_id": source_id, "source_chunk_id": chunk_id},
            MCPPreconditionFailedError,
            False,
        ),
        ("role", {"source_id": source_id, "evidence_role": "zzz"}, MCPArgumentError, True),
        ("confidence type", {"source_id": source_id, "confidence": "high"}, MCPArgumentError, True),
        ("confidence range", {"source_id": source_id, "confidence": 2}, MCPArgumentError, True),
        ("quote type", {"source_id": source_id, "quote": 7}, MCPArgumentError, True),
    ]


@pytest.mark.parametrize(
    ("label", "provenance", "expected", "store_lists_chunks"),
    _provenance_cases(),
    ids=[row[0] for row in _provenance_cases()],
)
def test_each_provenance_refusal_names_its_kind(
    label: str,
    provenance: dict[str, object],
    expected: type[MCPCodedToolError],
    store_lists_chunks: bool,
) -> None:
    """A bad source reference is an argument, a source or chunk that is not there is ``not_found``, and a store that
    cannot check chunks is a state.

    The source and chunk ids are drawn once per case list, so the stub store below recognises the ones the rows use.

    Mutations: change one raised class in ``_validated_review_provenance`` (``mcp/review.py``), for example the
    ``was not found in the current user scope`` raise to ``MCPArgumentError``, or the ``cannot validate provenance
    source chunks`` raise to ``MCPToolError``. The row of that site fails.
    """

    from alicebot_api.mcp.review import _validated_review_provenance

    source_id = str(provenance.get("source_id", ""))
    known = {} if provenance.get("source_id") is None or label == "source not found" else {source_id: {"id": source_id}}
    chunks = {source_id: [{"id": str(uuid4())}]}
    store: object = _ProvenanceStore(sources=known, chunks=chunks)
    if not store_lists_chunks:
        store = SimpleNamespace(get_source=store.get_source)  # type: ignore[attr-defined]

    with pytest.raises(MCPToolError) as caught:
        _validated_review_provenance(
            store, provenance, fallback_confidence=None, source_fence=SourceReadFence.unfenced()
        )

    assert type(caught.value) is expected, label


def test_an_oversized_correction_field_is_an_argument_error() -> None:
    """A correction field over the serialized-size limit is rejected as an argument.

    Mutation: raise ``MCPToolError`` in ``_refuse_oversized_correction`` (``mcp/review.py``).
    """

    from alicebot_api.mcp.review import _refuse_oversized_correction
    from alicebot_api.write_bounds import MAX_CORRECTION_FIELD_CHARS

    with pytest.raises(MCPToolError) as caught:
        _refuse_oversized_correction(body="x" * (MAX_CORRECTION_FIELD_CHARS + 1))

    assert type(caught.value) is MCPArgumentError


def test_the_review_tool_names_the_kind_of_each_refusal_on_a_real_vault(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``alice_memory_correct`` as the owner: an unsupported action, no edit fields, no replacement, a retired row,
    and a pending consolidation candidate that cannot be edited.

    Mutations, each one alone, in ``_vnext_memory_correct`` (``mcp/review.py``): raise ``MCPToolError`` for the
    ``is not supported by canonical vNext review`` check; for ``edit-and-approve requires at least one of``; for
    ``supersede-existing requires``; for the ``cannot be reviewed from status`` transition error; for ``pending
    consolidation candidates cannot be edit-and-approved``. The row of that check fails.
    """

    memory_id = str(_commit(sqlite_context, confidence=0.7)["memory"]["id"])  # type: ignore[index]
    base: dict[str, object] = {"review_item_id": memory_id}

    assert _code(sqlite_context, "alice_memory_correct", {**base, "action": "zzz"}) == "invalid_request"
    assert _code(sqlite_context, "alice_memory_correct", {**base, "action": "edit-and-approve"}) == "invalid_request"
    assert _code(sqlite_context, "alice_memory_correct", {**base, "action": "supersede-existing"}) == "invalid_request"

    def mark_consolidation(store: SQLiteVNextStore) -> object:
        return store.update_memory(memory_id=memory_id, patch={"metadata_json": {"consolidation": {}}})

    _store_read(sqlite_context, mark_consolidation)
    assert (
        _code(sqlite_context, "alice_memory_correct", {**base, "action": "edit-and-approve", "title": "New title"})
        == "precondition_failed"
    )

    retired = str(_commit(sqlite_context, canonical_text="A second fact, to be retired.")["memory"]["id"])  # type: ignore[index]
    assert (
        _wire(sqlite_context, "alice_memory_manage", {"action": "forget", "memory_id": retired})["_is_error"] is False
    )
    assert (
        _code(sqlite_context, "alice_memory_correct", {"review_item_id": retired, "action": "approve"})
        == "precondition_failed"
    )


def test_a_review_that_loses_a_race_to_a_consolidation_candidate_is_a_state_error(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the row becomes a consolidation candidate between the first read and the lock, the answer is a state.

    The first check sees an ordinary row and the second, after the lock, sees a candidate.

    Mutation: raise ``MCPToolError`` in place of ``MCPPreconditionFailedError`` for ``memory became a pending
    consolidation candidate during review`` in ``_vnext_memory_correct`` (``mcp/review.py``).
    """

    from alicebot_api.mcp import review

    memory_id = str(_commit(sqlite_context, confidence=0.7)["memory"]["id"])  # type: ignore[index]
    answers = iter((False, True))
    monkeypatch.setattr(review, "is_pending_consolidation_candidate", lambda _memory: next(answers))

    code = _code(sqlite_context, "alice_memory_correct", {"review_item_id": memory_id, "action": "approve"})

    assert code == "precondition_failed"


def test_a_memory_deleted_between_the_two_reads_of_a_review_is_not_found(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row exists at the first read and is gone at the locked read: ``not_found``, not a refusal.

    Mutation: raise ``MCPToolError`` in place of ``MCPReferenceNotFoundError`` for the second ``memory {memory_id}
    was not found`` in ``_vnext_memory_correct`` (``mcp/review.py``, the one after ``get_memory_for_update``).
    """

    memory_id = str(_commit(sqlite_context, confidence=0.7)["memory"]["id"])  # type: ignore[index]
    monkeypatch.setattr(SQLiteVNextStore, "get_memory_for_update", lambda _self, _memory_id: None, raising=False)

    code = _code(sqlite_context, "alice_memory_correct", {"review_item_id": memory_id, "action": "approve"})

    assert code == "not_found"


def test_a_pending_project_update_is_a_state_error_on_the_review_tool(
    sqlite_context: MCPRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A coupled project update still awaiting its own review cannot be approved through ``alice_memory_correct``.

    Mutation: raise ``MCPToolError`` in place of ``MCPPreconditionFailedError`` for
    ``PENDING_PROJECT_UPDATE_MEMORY_MUTATION_MESSAGE`` in ``_vnext_memory_correct`` (``mcp/review.py``). The same
    refusal inside ``VNextMemoryCommitService`` is the ``MemoryStateError`` row of the dispatcher table.
    """

    from alicebot_api.mcp import review

    memory_id = str(_commit(sqlite_context, confidence=0.7)["memory"]["id"])  # type: ignore[index]
    monkeypatch.setattr(review, "is_pending_project_update_memory", lambda _memory: True)

    code = _code(sqlite_context, "alice_memory_correct", {"review_item_id": memory_id, "action": "approve"})

    assert code == "precondition_failed"


# --- The schema check raises the argument class at every branch ------------------------------------------------


_SCHEMA_CASES: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
    # Top-level required properties belong to each handler's parser; the check here enforces nested ones.
    (
        "required_nested",
        {"type": "object", "properties": {"o": {"type": "object", "required": ["a"]}}},
        {"o": {}},
    ),
    ("min_properties", {"type": "object", "minProperties": 1}, {}),
    ("max_properties", {"type": "object", "maxProperties": 1}, {"a": 1, "b": 2}),
    (
        "additional",
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
        {"z": 1},
    ),
    ("type", {"type": "object", "properties": {"a": {"type": "string"}}}, {"a": 7}),
    ("enum", {"type": "object", "properties": {"a": {"enum": ["x"]}}}, {"a": "y"}),
    ("pattern", {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x$"}}}, {"a": "y"}),
    ("max_length", {"type": "object", "properties": {"a": {"type": "string", "maxLength": 1}}}, {"a": "yy"}),
    ("minimum", {"type": "object", "properties": {"a": {"type": "integer", "minimum": 1}}}, {"a": 0}),
    ("maximum", {"type": "object", "properties": {"a": {"type": "integer", "maximum": 1}}}, {"a": 2}),
    ("min_items", {"type": "object", "properties": {"a": {"type": "array", "minItems": 1}}}, {"a": []}),
    ("max_items", {"type": "object", "properties": {"a": {"type": "array", "maxItems": 1}}}, {"a": [1, 2]}),
    ("uuid_value", {"type": "object", "properties": {"a": {"type": "string", "format": "uuid"}}}, {"a": "nope"}),
    (
        "uuid_form",
        {"type": "object", "properties": {"a": {"type": "string", "format": "uuid"}}},
        {"a": "{" + str(uuid4()) + "}"},
    ),
    (
        "date_time_value",
        {"type": "object", "properties": {"a": {"type": "string", "format": "date-time"}}},
        {"a": "nope"},
    ),
    (
        "date_time_zone",
        {"type": "object", "properties": {"a": {"type": "string", "format": "date-time"}}},
        {"a": "2026-01-01T00:00:00"},
    ),
    (
        "date_time_calendar",
        {"type": "object", "properties": {"a": {"type": "string", "format": "date-time"}}},
        {"a": "2026-13-45T00:00:00Z"},
    ),
    ("date_value", {"type": "object", "properties": {"a": {"type": "string", "format": "date"}}}, {"a": "nope"}),
    (
        "date_calendar",
        {"type": "object", "properties": {"a": {"type": "string", "format": "date"}}},
        {"a": "2026-13-45"},
    ),
    ("date_form", {"type": "object", "properties": {"a": {"type": "string", "format": "date"}}}, {"a": "20260101"}),
    ("format", {"type": "object", "properties": {"a": {"type": "string", "format": "zzz"}}}, {"a": "x"}),
)


@pytest.mark.parametrize(("label", "schema", "arguments"), _SCHEMA_CASES, ids=[row[0] for row in _SCHEMA_CASES])
def test_each_branch_of_the_schema_check_raises_the_argument_class(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    schema: dict[str, object],
    arguments: dict[str, object],
) -> None:
    """The check that runs before every handler rejects with ``MCPArgumentError`` whatever it found wrong.

    A throwaway tool definition carries one schema per branch, so the branch is reached on its own.

    Mutations, each one alone, in ``mcp/registry.py``: change the ``raise MCPArgumentError`` of one direct branch
    (``missing required properties`` of a nested object, ``requires at least``, ``does not accept additional properties``, the UUID,
    date-time and full-date parse failures) back to ``raise MCPToolError``; change ``fail()`` to raise
    ``MCPToolError``, which fails every row that goes through it. The check is ``type(error) is MCPArgumentError``.
    """

    monkeypatch.setitem(registry._TOOL_DEFINITIONS_BY_NAME, "alice_typed_code_probe", {"inputSchema": schema})

    with pytest.raises(MCPToolError) as caught:
        registry._validate_mcp_arguments_against_advertised_schema("alice_typed_code_probe", arguments)

    assert type(caught.value) is MCPArgumentError, label


# --- Every parser of an argument raises the argument class -----------------------------------------------------


def _argument_parser_cases() -> list[tuple[str, Callable[[], object]]]:
    from alicebot_api.mcp import arguments as a

    return [
        ("normalize", lambda: a._normalize_arguments(["not", "an", "object"])),
        ("optional_text", lambda: a._parse_optional_text({"k": 7}, "k")),
        ("required_text", lambda: a._parse_required_text({}, "k")),
        ("required_text_empty", lambda: a._parse_required_text({"k": "  "}, "k")),
        ("required_document_text", lambda: a._parse_required_document_text({}, "k")),
        ("required_document_text_empty", lambda: a._parse_required_document_text({"k": " \n "}, "k")),
        ("optional_uuid_type", lambda: a._parse_optional_uuid({"k": 7}, "k")),
        ("optional_uuid_value", lambda: a._parse_optional_uuid({"k": "nope"}, "k")),
        ("required_uuid", lambda: a._parse_required_uuid({}, "k")),
        ("optional_datetime_type", lambda: a._parse_optional_datetime({"k": 7}, "k")),
        ("optional_datetime_value", lambda: a._parse_optional_datetime({"k": "nope"}, "k")),
        ("int_bool", lambda: a._parse_int({"k": True}, key="k", default=1, minimum=0, maximum=10)),
        ("int_text", lambda: a._parse_int({"k": "x"}, key="k", default=1, minimum=0, maximum=10)),
        ("int_blank", lambda: a._parse_int({"k": " "}, key="k", default=1, minimum=0, maximum=10)),
        ("int_other_type", lambda: a._parse_int({"k": [1]}, key="k", default=1, minimum=0, maximum=10)),
        ("int_range", lambda: a._parse_int({"k": 99}, key="k", default=1, minimum=0, maximum=10)),
        ("optional_json_object", lambda: a._parse_optional_json_object({"k": []}, "k")),
        ("string_list", lambda: a._parse_string_list({"k": 7}, "k")),
        ("string_list_item", lambda: a._parse_string_list({"k": [7]}, "k")),
        ("memory_types", lambda: a._parse_memory_types({"memory_types": ["zzz"]})),
        ("optional_float", lambda: a._parse_optional_float({"k": "x"}, "k")),
        ("optional_float_bool", lambda: a._parse_optional_float({"k": True}, "k")),
        ("optional_float_other_type", lambda: a._parse_optional_float({"k": [1]}, "k")),
        ("bool", lambda: a._parse_bool({"k": "maybe"}, key="k")),
        (
            "filter_window",
            lambda: a._retrieval_filter_kwargs({"since": "2026-01-02T00:00:00Z", "until": "2026-01-01T00:00:00Z"}),
        ),
        ("context_depth", lambda: a._parse_context_pack_tuning({"context_depth": "zzz"})),
        ("context_strategy", lambda: a._parse_context_pack_tuning({"budget_strategy": "zzz"})),
        ("model_mode", lambda: a._parse_model_generation_kwargs({"generation_mode": "zzz"})),
        ("model_route", lambda: a._parse_model_generation_kwargs({"model_route_mode": "zzz"})),
        ("model_temperature", lambda: a._parse_model_generation_kwargs({"model_temperature": 9})),
        ("review_status", lambda: a._parse_review_status({"status": "zzz"}, default="stale")),
        ("review_status_type", lambda: a._parse_review_status({"status": 7}, default="stale")),
        (
            "review_item_mismatch",
            lambda: a._parse_review_item_id(
                {"review_item_id": str(uuid4()), "continuity_object_id": str(uuid4())}, required=True
            ),
        ),
        ("review_item_required", lambda: a._parse_review_item_id({}, required=True)),
        ("review_action", lambda: a._resolve_review_apply_action("zzz", allow_legacy=False)),
        ("brief_type_empty", lambda: a._parse_continuity_brief_request({"brief_type": ""})),
        ("brief_type_value", lambda: a._parse_continuity_brief_request({"brief_type": "zzz"})),
        ("task_brief_mode_missing", lambda: a._parse_task_brief_request({})),
        ("task_brief_mode_empty", lambda: a._parse_task_brief_request({"mode": " "})),
    ]


@pytest.mark.parametrize(("label", "call"), _argument_parser_cases(), ids=[row[0] for row in _argument_parser_cases()])
def test_each_argument_parser_raises_the_argument_class(label: str, call: Callable[[], object]) -> None:
    """A bad value in any parser raises ``MCPArgumentError``, which the server sends as ``invalid_request``.

    Mutation: change the ``raise MCPArgumentError`` of one parser in ``mcp/arguments.py`` back to ``raise
    MCPToolError``. The rows of that parser fail. The check is ``type(error) is MCPArgumentError`` on purpose: a
    subclass would still satisfy ``pytest.raises(MCPArgumentError)`` and hide a change of code.
    """

    with pytest.raises(MCPToolError) as caught:
        call()
    assert type(caught.value) is MCPArgumentError, label


# --- The classification of every other raise site is closed ----------------------------------------------------

# A ``raise`` of one of these classes answers ``tool_request_failed``: the dispatcher leaves each of them generic on
# purpose (see the comment on the last ``except`` clause of ``call_mcp_tool``). A new one is a decision, so each
# raise that may exist is listed here with the reason, keyed by file, function, class and the text of the raised
# expression, with the number of times it appears. A raise anywhere else, a second copy of a listed one, or a listed
# one that is gone fails. Everything else in ``mcp/`` raises a class that names its kind.
_GENERIC_CLASSES = frozenset(
    {
        "MCPToolError",
        "VNextMemoryCommitValidationError",
        "IdempotencyKeyConflictError",
        "MemoryMutationValidationError",
        "ValueError",
        "TypeError",
    }
)
_BARE_RAISE_ALLOWLIST: dict[tuple[str, str, str, str], tuple[int, str]] = {
    (
        "registry.py",
        "call_mcp_tool",
        "MCPToolError",
        "MCPToolError(str(exc))",
    ): (2, "the dispatcher's own fallbacks: an other SQLite constraint, and any ValueError or TypeError"),
    (
        "memories.py",
        "_handle_alice_vnext_commit_memory",
        "MCPToolError",
        "MCPToolError(str(conflict))",
    ): (1, "an idempotency key bound to a different request: a conflict, not one of the four kinds"),
    (
        "memories.py",
        "redact_memory_flow",
        "VNextMemoryCommitValidationError",
        "VNextMemoryCommitValidationError('reason is required to redact a memory')",
    ): (
        1,
        "a flow shared with the HTTP and CLI doors; the MCP handler parses reason as required text first, so this "
        "is not reachable over MCP",
    ),
    (
        "evidence_artifacts.py",
        "_handle_alice_explain",
        "MCPToolError",
        "MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)",
    ): (2, "key-bound explain answers one uniform refusal for a missing and an unreadable target"),
    (
        "evidence_artifacts.py",
        "_raise_explain_authorization_error",
        "MCPToolError",
        "MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)",
    ): (2, "the same uniform refusal for a key-bound caller, and an undecided one"),
    (
        "evidence_artifacts.py",
        "_handle_alice_vnext_memory_audit",
        "MCPToolError",
        "MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)",
    ): (1, "the same uniform refusal for a key-bound caller"),
    (
        "runtime.py",
        "_sqlite_path_from_url",
        "MCPToolError",
        "MCPToolError('sqlite database URL must include a database file path')",
    ): (1, "a malformed database URL is operator configuration, not a state the agent can change"),
    (
        "runtime.py",
        "_sqlite_path_from_url",
        "MCPToolError",
        "MCPToolError('sqlite database URLs must reference a local file path')",
    ): (1, "a malformed database URL is operator configuration, not a state the agent can change"),
    (
        "runtime.py",
        "_sqlite_path_from_url",
        "MCPToolError",
        "MCPToolError(f\"expected a sqlite:/// database URL, got '{database_url}'\")",
    ): (1, "a malformed database URL is operator configuration, not a state the agent can change"),
    (
        "types.py",
        "_json_value",
        "MCPToolError",
        "MCPToolError(f'MCP result contains unsupported JSON value {type(normalized).__name__}')",
    ): (1, "an internal result holds a value that is not JSON"),
    (
        "types.py",
        "_json_object",
        "MCPToolError",
        "MCPToolError('MCP result must be a JSON object')",
    ): (1, "an internal result is not a JSON object"),
}
_INTERNAL_FALLTHROUGH_SUFFIX = "did not complete"


def _generic_raises() -> list[tuple[str, str, int, str, str, str | None]]:
    """Every ``raise`` of a generic class in ``mcp/``: file, function, line, class, expression, message literal."""

    found: list[tuple[str, str, int, str, str, str | None]] = []
    for path in sorted(_MCP_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            if not (isinstance(func, ast.Name) and func.id in _GENERIC_CLASSES):
                continue
            enclosing = node
            while enclosing in parents and not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = parents[enclosing]
            name = enclosing.name if isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
            first = node.exc.args[0] if node.exc.args else None
            literal = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            found.append((path.name, name, node.lineno, func.id, ast.unparse(node.exc), literal))
    return found


def _unlisted_generic_raises() -> tuple[list[str], dict[tuple[str, str, str, str], int]]:
    counts: dict[tuple[str, str, str, str], int] = {}
    unlisted: list[str] = []
    for filename, function, lineno, class_name, expression, literal in _generic_raises():
        if class_name == "MCPToolError" and literal is not None and literal.endswith(_INTERNAL_FALLTHROUGH_SUFFIX):
            continue
        key = (filename, function, class_name, expression)
        counts[key] = counts.get(key, 0) + 1
        if key not in _BARE_RAISE_ALLOWLIST or counts[key] > _BARE_RAISE_ALLOWLIST[key][0]:
            unlisted.append(f"{filename}:{lineno} in {function}: {expression}")
    return unlisted, counts


def test_a_generic_raise_is_made_only_where_the_allowlist_says_why() -> None:
    """A handler that rejects a request names the kind of the refusal; a generic raise is the exception.

    Allowed: the ``... did not complete`` fall-through of each handler (an internal failure with nothing for the
    caller to change) and the raises in the allowlist, each with its reason. A new ``raise MCPToolError("x is
    required")``, or a raise of the commit service's base class for a missing memory, fails here and the message says
    which class to raise instead. The allowlist is keyed by the text of the raised expression and a count, so a dead
    copy of a listed raise inside a listed function fails too.

    Mutations, each one alone: change the ``raise MCPArgumentError`` of ``_parse_required_text`` in
    ``mcp/arguments.py`` back to ``raise MCPToolError``; add ``raise MCPToolError("memory 1 was not found")`` to any
    handler, including a listed one such as ``_handle_alice_explain``; raise ``VNextMemoryCommitValidationError(
    "memory was not found")`` for the missing-memory check of ``redact_memory_flow`` in ``mcp/memories.py`` (the way
    the redact miss of the first review got through); add a second ``raise MCPToolError(str(exc))`` to
    ``call_mcp_tool``. Each fails.
    """

    unlisted, _counts = _unlisted_generic_raises()

    assert unlisted == [], (
        "a generic raise answers tool_request_failed. Raise MCPArgumentError (a rejected argument), "
        "MCPNotPermittedError (a policy or profile refusal), MCPReferenceNotFoundError or MemoryNotFoundError (an "
        "id that does not exist), MCPPreconditionFailedError or MemoryStateError (a state that forbids the call), "
        "or add the raise to _BARE_RAISE_ALLOWLIST with a reason: " + ", ".join(unlisted)
    )


def test_every_allowlisted_raise_still_exists_the_listed_number_of_times() -> None:
    """The allowlist names only raises that exist, as many as it says, so it cannot rot into a list of nothing.

    Mutations, each one alone: remove the only raise of an allowlisted function (for example the ``_json_value``
    raise in ``mcp/types.py``) without removing its allowlist row; remove one of the two ``raise
    MCPToolError(_EXPLAIN_UNAVAILABLE_MESSAGE)`` of ``_handle_alice_explain``. Each fails.
    """

    _unlisted, counts = _unlisted_generic_raises()

    assert counts == {key: count for key, (count, _reason) in _BARE_RAISE_ALLOWLIST.items()}
