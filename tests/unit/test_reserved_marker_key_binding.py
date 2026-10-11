"""A key cannot be bound to the reserved project name, and a stored key that is bound to it is refused whole.

``~global`` is the name Alice puts in a request tuple to ask for the rows that belong to no project. Every project test
reads it that way, so a key whose binding is that name reads the rows of every free-form project (a project that is a
plain name and not an Alice project id) as "the rows of no project". In v0.20.0 the name was an ordinary one and such a
key read the rows stored under the name ``~global`` and nothing else.

These tests run on SQLite, through the MCP tools with real keys, and on the key functions directly. The same cases on
PostgreSQL and through every ``/v0/vnext`` route are in ``tests/integration/test_reserved_marker_binding_postgres.py``.
"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPInvalidRequestError, MCPNotPermittedError, MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import (
    AgentIdentity,
    AgentIdentityValidationError,
    evaluate_agent_policy,
)
from alicebot_api.vnext_agent_keys import (
    AgentKeyAuthenticationError,
    AgentKeyValidationError,
    agent_key_prefix,
    create_agent_key,
    hash_agent_key,
    mint_agent_key,
    resolve_agent_identity,
)
from alicebot_api.vnext_project_scope import (
    GLOBAL_PROJECT_MARKER,
    RESERVED_PROJECT_MARKERS,
    reserved_project_marker,
)
from tests.unit.test_vnext_agent_keys import FakeAgentKeyStore

# Every spelling the identity functions fold to the marker: case, padding and tabs.
MARKER_SPELLINGS = ("~global", "~GLOBAL", "~Global", "  ~global  ", "\t~global\n")
# Names that look like it and are ordinary free-form names.
NEAR_MISSES = ("~globalx", "x~global", "global", "~", "~ global", "~glоbal", "~global2", "")

ORCHARD, LIGHTHOUSE = "Orchard", "Lighthouse"
ALICE_PROJECT = "prj_" + "a" * 16
ORCHARD_TEXT = "Synthetic fact about the orchard project"
LIGHTHOUSE_TEXT = "Synthetic fact about the lighthouse project"
GLOBAL_TEXT = "Synthetic fact that belongs to no project"
ALICE_TEXT = "Synthetic fact about the registered project"


def test_the_reserved_set_is_the_global_marker_and_nothing_else() -> None:
    """Mutation: add a second name to the tuple without a test for it, or drop the marker from it."""

    assert RESERVED_PROJECT_MARKERS == (GLOBAL_PROJECT_MARKER,) == ("~global",)


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
def test_the_reserved_name_is_found_however_it_is_spelled(spelling: str) -> None:
    """Mutation: compare the raw text with the marker instead of the identity the project tests use."""

    assert reserved_project_marker(spelling) == "~global"
    assert reserved_project_marker([spelling]) == "~global"
    assert reserved_project_marker(["Orchard", spelling]) == "~global"
    assert reserved_project_marker((ALICE_PROJECT, spelling)) == "~global"


@pytest.mark.parametrize("name", NEAR_MISSES)
def test_an_ordinary_name_is_not_the_reserved_name(name: str) -> None:
    """Mutation: match the marker as a prefix, as a substring or after dropping the tilde."""

    assert reserved_project_marker(name) is None
    assert reserved_project_marker([name, "Orchard"]) is None


def test_no_project_scope_holds_no_reserved_name() -> None:
    assert reserved_project_marker(None) is None
    assert reserved_project_marker(()) is None
    assert reserved_project_marker([]) is None
    assert reserved_project_marker([ALICE_PROJECT, "Orchard"]) is None


# -- creation -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
def test_a_key_cannot_be_created_bound_to_the_reserved_name(spelling: str) -> None:
    """Mutation: remove the check from ``create_agent_key``.

    The error says the name is reserved and what to do, repeats nothing but that name, and no key is stored.
    """

    store = FakeAgentKeyStore()
    with pytest.raises(AgentKeyValidationError) as caught:
        create_agent_key(
            store, user_id=uuid4(), agent_id="agent", permission_profile="project_scoped_agent", project_scope=spelling
        )
    assert str(caught.value) == (
        "the project name ~global is reserved by Alice and cannot be a key's project scope; "
        "use the name or id of a project instead"
    )
    assert store.agent_api_keys == []


def test_a_key_is_refused_when_the_reserved_name_is_anywhere_in_the_binding_it_was_given() -> None:
    """Mutation: check only the part of the value that is stored (the first name)."""

    store = FakeAgentKeyStore()
    for value in (["Orchard", "~global"], ("~global", "Orchard"), [["Orchard"], ["~GLOBAL"]]):
        with pytest.raises(AgentKeyValidationError):
            create_agent_key(
                store, user_id=uuid4(), agent_id="agent", permission_profile="project_scoped_agent", project_scope=value  # type: ignore[arg-type]
            )
    assert store.agent_api_keys == []


@pytest.mark.parametrize("name", [ORCHARD, ALICE_PROJECT, "~globalx", "x~global", "~glоbal"])
def test_a_key_is_still_created_bound_to_an_ordinary_name(name: str) -> None:
    store = FakeAgentKeyStore()
    record, raw = create_agent_key(
        store, user_id=uuid4(), agent_id="agent", permission_profile="project_scoped_agent", project_scope=name
    )
    assert record["project_scope"] == name
    assert raw.startswith("alice_sk_")
    assert [row["project_scope"] for row in store.agent_api_keys] == [name]


def test_the_cli_refuses_to_create_a_key_bound_to_the_reserved_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation: remove the check from ``create_agent_key``; the command has no check of its own."""

    from alicebot_api import cli as cli_module
    from alicebot_api.config import Settings

    store = FakeAgentKeyStore()

    @contextmanager
    def fake_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"), database_url="postgresql://db", user_id=uuid4()
    )
    parser = cli_module.build_parser()
    args = parser.parse_args(
        ["agent", "keys", "create", "--agent-id", "openclaw", "--profile", "project_scoped_agent", "--project-scope", "~global"]
    )
    with pytest.raises(AgentKeyValidationError, match="reserved by Alice"):
        args.handler(ctx, args)
    assert store.agent_api_keys == []
    ordinary = parser.parse_args(
        ["agent", "keys", "create", "--agent-id", "openclaw", "--profile", "project_scoped_agent", "--project-scope", "alicebot"]
    )
    assert json.loads(ordinary.handler(ctx, ordinary))["key"]["project_scope"] == "alicebot"


def test_the_cli_logs_why_a_key_command_was_refused_at_warning_level(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Mutation: drop the decorator from a key command, or log the reason below warning level.

    The command answers every refusal with the same public ``invalid_request`` error, so the reason is written to the
    log at warning level, which a person running the command reads on stderr.
    """

    import logging

    from alicebot_api import cli as cli_module
    from alicebot_api.config import Settings

    store = FakeAgentKeyStore()

    @contextmanager
    def fake_store_context(_ctx):
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_store_context)
    ctx = cli_module.CLIContext(
        settings=Settings(database_url="postgresql://db"), database_url="postgresql://db", user_id=uuid4()
    )
    parser = cli_module.build_parser()
    create = parser.parse_args(
        ["agent", "keys", "create", "--agent-id", "openclaw", "--profile", "project_scoped_agent", "--project-scope", "~GLOBAL"]
    )
    revoke = parser.parse_args(["agent", "keys", "revoke", "alice_sk_nokey"])
    with caplog.at_level(logging.DEBUG, logger="alicebot_api.cli"):
        for args in (create, revoke):
            with pytest.raises(AgentKeyValidationError):
                args.handler(ctx, args)
    records = [item for item in caplog.records if item.name == "alicebot_api.cli"]
    assert [item.levelno for item in records] == [logging.WARNING, logging.WARNING]
    assert records[0].getMessage() == (
        "the project name ~global is reserved by Alice and cannot be a key's project scope; "
        "use the name or id of a project instead"
    )
    assert records[1].getMessage() == "no agent API key matches 'alice_sk_nokey'"
    assert create.handler.__name__ == "_run_agent_keys_create" and revoke.handler.__name__ == "_run_agent_keys_revoke"


def test_create_agent_key_is_the_only_writer_of_a_key_row() -> None:
    """Mutation: add a second caller of ``create_agent_api_key`` that stores a binding of its own.

    The refusal lives in ``create_agent_key``. A door that called the store method directly would write a binding
    the refusal never saw, so the only caller outside the stores themselves is that function.
    """

    source_root = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api"
    callers = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "create_agent_api_key":
                callers.append(path.relative_to(source_root).as_posix())
    assert callers == ["vnext_agent_keys.py"]


# -- a key stored with the reserved name ---------------------------------------------------------------------------


def stored_marker_key(store: FakeAgentKeyStore, *, profile: str = "project_scoped_agent", binding: str = "~global") -> str:
    """A key as an older version could store it: the binding written to the row without any check."""

    raw = mint_agent_key()
    store.create_agent_api_key(
        {
            "user_id": "u",
            "agent_id": "old-agent",
            "permission_profile": profile,
            "project_scope": binding,
            "key_hash": hash_agent_key(raw),
            "key_prefix": agent_key_prefix(raw),
            "label": None,
        }
    )
    return raw


@pytest.mark.parametrize("binding", MARKER_SPELLINGS)
@pytest.mark.parametrize("payload", [{}, {"agent_id": "old-agent"}, {"project_scope": ["Orchard"]}])
def test_a_stored_key_bound_to_the_reserved_name_is_refused_whole(binding: str, payload: dict) -> None:
    """Mutation: remove the stored-binding check in ``resolve_agent_identity``.

    The key is refused with status 403 before any claim is read. The function appends an event that names the key by
    its prefix and the reason, which a store that keeps its writes shows here. A request rolls its transaction back on
    the refusal, so no event is stored in a real flow (see ``test_a_refused_key_persists_no_rejection_event_in_a_real_store``).
    """

    store = FakeAgentKeyStore()
    raw = stored_marker_key(store, binding=binding)
    with pytest.raises(AgentKeyAuthenticationError) as caught:
        resolve_agent_identity(store, user_id="u", raw_key=raw, payload=payload)
    assert caught.value.status_code == 403
    assert str(caught.value) == (
        "agent API key is bound to the project name ~global, which Alice reserves for its own use, so the key is "
        "refused at every door. Revoke it with 'alicebot agent keys revoke' and create a new key bound to the name "
        "or id of a project."
    )
    [event] = store.events
    assert event["event_type"] == "agent.key_escalation_rejected"
    assert event["payload_json"]["reason"] == "reserved_project_scope_binding"
    assert event["payload_json"]["key_prefix"] == raw[:12]


def test_a_stored_key_bound_to_the_reserved_name_is_refused_whatever_its_profile() -> None:
    store = FakeAgentKeyStore()
    for profile in ("read_only_agent", "memory_proposal_agent", "project_scoped_agent", "trusted_local_agent", "admin_agent"):
        raw = stored_marker_key(store, profile=profile)
        with pytest.raises(AgentKeyAuthenticationError) as caught:
            resolve_agent_identity(store, user_id="u", raw_key=raw, payload={})
        assert caught.value.status_code == 403, profile


def test_a_stored_key_with_an_ordinary_binding_still_resolves() -> None:
    store = FakeAgentKeyStore()
    raw = stored_marker_key(store, binding="Orchard")
    identity = resolve_agent_identity(store, user_id="u", raw_key=raw, payload={})
    assert identity is not None
    assert identity.project_scope == ("Orchard",) and identity.project_scope_locked is True
    assert store.events == []


def test_the_reason_of_a_refused_stored_key_goes_to_the_server_log(caplog: pytest.LogCaptureFixture) -> None:
    """Mutation: log the refusal below warning level, or leave the id or the revoke command out of the line.

    Every surface answers this refusal with its standard text, which names no reason, so the server log is the one
    place that says why and which key to revoke.
    """

    import logging

    store = FakeAgentKeyStore()
    raw = stored_marker_key(store)
    with caplog.at_level(logging.DEBUG, logger="alicebot_api.vnext_agent_keys"):
        with pytest.raises(AgentKeyAuthenticationError):
            resolve_agent_identity(store, user_id="u", raw_key=raw, payload={})
    [record] = [item for item in caplog.records if item.name == "alicebot_api.vnext_agent_keys"]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    key_id = store.agent_api_keys[0]["id"]
    assert f"agent API key {key_id} (agent old-agent) is refused" in message
    assert "bound to the project name ~global" in message
    assert f"alicebot agent keys revoke {key_id}" in message
    # The id is logged and not the prefix of the secret, nor the secret.
    assert raw[:12] not in message and raw not in message


# -- a declared scope ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
def test_a_declared_project_scope_cannot_hold_the_reserved_name(spelling: str) -> None:
    """Mutation: remove the check from the declared-scope reader in ``vnext_agent_control``.

    This is the identity a keyless install honours from the payload, and the first reader of the claim of a keyed call.
    """

    for payload in (
        {"agent_id": "agent", "project_scope": [spelling]},
        {"agent_identity": {"agent_id": "agent", "project_scope": ["Orchard", spelling]}},
    ):
        with pytest.raises(AgentIdentityValidationError, match="reserved by Alice"):
            AgentIdentity.from_payload(payload)
    assert AgentIdentity.from_payload({"agent_id": "agent", "project_scope": ["Orchard"]}) is not None


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
def test_a_keyless_install_does_not_honour_a_declared_reserved_name(spelling: str) -> None:
    store = FakeAgentKeyStore()
    with pytest.raises(AgentIdentityValidationError):
        resolve_agent_identity(
            store,
            user_id="u",
            raw_key=None,
            payload={"agent_id": "agent", "permission_profile": "project_scoped_agent", "project_scope": [spelling]},
        )


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
@pytest.mark.parametrize("bound", [None, "Orchard", ALICE_PROJECT])
@pytest.mark.parametrize("with_agent_id", [True, False])
def test_a_key_cannot_claim_the_reserved_name_whether_it_is_bound_or_not(spelling: str, bound: str | None, with_agent_id: bool) -> None:
    """Mutation: remove the check from ``_claimed_project_scope`` (a claim with no agent id is read only there)."""

    store = FakeAgentKeyStore()
    _record, raw = create_agent_key(
        store, user_id="u", agent_id="agent", permission_profile="project_scoped_agent", project_scope=bound
    )
    payload: dict = {"project_scope": [spelling]}
    if with_agent_id:
        payload["agent_id"] = "agent"
    with pytest.raises((AgentKeyAuthenticationError, AgentIdentityValidationError)) as caught:
        resolve_agent_identity(store, user_id="u", raw_key=raw, payload=payload)
    assert "reserved by Alice" in str(caught.value)


def test_a_key_may_still_claim_a_project_inside_its_binding_or_any_project_when_it_is_unbound() -> None:
    store = FakeAgentKeyStore()
    _record, bound_raw = create_agent_key(
        store, user_id="u", agent_id="agent", permission_profile="project_scoped_agent", project_scope="Orchard"
    )
    identity = resolve_agent_identity(store, user_id="u", raw_key=bound_raw, payload={"project_scope": ["Orchard"]})
    assert identity is not None and identity.project_scope == ("Orchard",)
    _record, open_raw = create_agent_key(store, user_id="u", agent_id="agent2", permission_profile="trusted_local_agent")
    identity = resolve_agent_identity(
        store, user_id="u", raw_key=open_raw, payload={"agent_id": "agent2", "project_scope": ["Lighthouse"]}
    )
    assert identity is not None and identity.project_scope == ("Lighthouse",) and identity.project_scope_locked is False


# -- the policy engine --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("locked", [True, False])
@pytest.mark.parametrize("action", ["review_items.lookup", "context_pack.request", "memory.commit", "source.capture"])
def test_the_policy_engine_grants_nothing_to_an_identity_whose_scope_holds_the_reserved_name(locked: bool, action: str) -> None:
    """Mutation: remove the identity-scope check from ``evaluate_agent_policy``.

    An identity built anywhere else (the engine does not know where) with the name in its scope, bound or declared, is
    blocked with an empty effective scope, so no reader receives a scope that it would read as "the rows of no project".
    """

    identity = AgentIdentity(
        agent_id="agent",
        permission_profile="project_scoped_agent",
        project_scope=("~global",),
        project_scope_locked=locked,
        auth="agent_api_key",
    )
    decision = evaluate_agent_policy(identity=identity, action=action, project_scope=(ORCHARD,))
    assert decision.decision == "blocked"
    assert "reserved_project_name_in_identity_scope" in decision.reasons
    assert decision.effective_project_scope == ()
    decision = evaluate_agent_policy(identity=identity, action=action)
    assert decision.decision == "blocked" and decision.effective_project_scope == ()


def test_the_policy_engine_does_not_change_for_an_ordinary_scope() -> None:
    identity = AgentIdentity(
        agent_id="agent",
        permission_profile="project_scoped_agent",
        project_scope=(ORCHARD,),
        project_scope_locked=True,
        auth="agent_api_key",
    )
    decision = evaluate_agent_policy(identity=identity, action="review_items.lookup")
    assert decision.decision != "blocked" and decision.effective_project_scope == (ORCHARD,)
    assert "reserved_project_name_in_identity_scope" not in decision.reasons


# -- the tools, with real keys, on SQLite --------------------------------------------------------------------------


READ_TOOLS: tuple[tuple[str, dict], ...] = (
    ("alice_memory_review", {"status": "all"}),
    ("alice_memory_review", {"status": "active"}),
    ("alice_recall", {"query": "Synthetic fact"}),
    ("alice_context_pack", {"query": "Synthetic fact"}),
    ("alice_resume", {}),
    ("alice_recent_decisions", {}),
    ("alice_open_loops", {"action": "list"}),
)


class World:
    def __init__(self, path: Path, user_id: UUID, memory_ids: dict[str, str]) -> None:
        self.path = path
        self.user_id = user_id
        self.memory_ids = memory_ids

    def store(self):
        return sqlite_user_connection(self.path, self.user_id)

    def context(self) -> MCPRuntimeContext:
        return MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=self.user_id)

    def counts(self) -> dict[str, int]:
        with self.store() as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # closed table names
                for table in ("memories", "sources", "open_loops", "agent_api_keys")
            }


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    # A folder that is no git work tree, so scoping on finds no project and reads the vault as scoping off does.
    folder = tmp_path / "folder"
    folder.mkdir()
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(folder))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    user_id = uuid4()
    path = tmp_path / "vault" / "reads.sqlite3"
    bootstrap_database(path, user_id=user_id, user_email="synthetic@example.invalid")
    ids: dict[str, str] = {}
    with sqlite_user_connection(path, user_id) as conn:
        store = SQLiteVNextStore(conn, user_id)
        for name, scope, text in (
            ("orchard", [ORCHARD], ORCHARD_TEXT),
            ("lighthouse", [LIGHTHOUSE], LIGHTHOUSE_TEXT),
            ("global", [], GLOBAL_TEXT),
            ("alice", [ALICE_PROJECT], ALICE_TEXT),
        ):
            row = store.create_memory(
                {
                    "memory_key": f"synthetic.{name}",
                    "canonical_text": text,
                    "title": f"Fact {name}",
                    "memory_type": "decision",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"project_scope": scope},
                }
            )
            ids[name] = str(row["id"])
            store.create_open_loop(
                {"title": f"Loop {name}", "memory_id": ids[name], "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": scope}}
            )
            store.create_source(
                {
                    "source_type": "note",
                    "title": f"Source {name}",
                    "content_hash": str(uuid4()),
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"project_scope": scope, "raw_text": text},
                }
            )
    return World(path, user_id, ids)


def store_key(world: World, *, profile: str, binding: str | None) -> str:
    """A key written to the row with no check, as any version before this change could write it."""

    raw = mint_agent_key()
    with world.store() as conn:
        SQLiteVNextStore(conn, world.user_id).create_agent_api_key(
            {
                "user_id": str(world.user_id),
                "agent_id": f"{profile}-{binding}",
                "permission_profile": profile,
                "project_scope": binding,
                "key_hash": hash_agent_key(raw),
                "key_prefix": agent_key_prefix(raw),
                "label": None,
            }
        )
    return raw


def seen(world: World, key: str | None, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict) -> str:
    if key is None:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    return json.dumps(call_mcp_tool(world.context(), name=tool, arguments=arguments), default=str)


@pytest.mark.parametrize("scoping", ["off", "on"])
@pytest.mark.parametrize("profile", ["project_scoped_agent", "trusted_local_agent", "admin_agent", "read_only_agent"])
@pytest.mark.parametrize(("tool", "arguments"), READ_TOOLS, ids=[f"{tool}-{index}" for index, (tool, _a) in enumerate(READ_TOOLS)])
def test_a_key_stored_with_the_reserved_name_reads_nothing_through_any_tool(
    world: World, monkeypatch: pytest.MonkeyPatch, scoping: str, profile: str, tool: str, arguments: dict
) -> None:
    """Mutation: remove the stored-binding check in ``resolve_agent_identity``.

    On the base this key read every row of every free-form project through ``alice_memory_review`` and was refused
    the other tools only by an error of the store. It is refused at every tool now, with the same error, and the call
    writes no memory, source or open loop.
    """

    monkeypatch.setenv("ALICE_PROJECT_SCOPING", scoping)
    key = store_key(world, profile=profile, binding="~global")
    before = world.counts()
    with pytest.raises(MCPNotPermittedError) as caught:
        seen(world, key, monkeypatch, tool, arguments)
    assert "bound to the project name ~global" in str(caught.value)
    assert world.counts() == before


@pytest.mark.parametrize(
    "tool_arguments",
    [
        ("alice_explain", lambda ids: {"memory_id": ids["orchard"]}),
        ("alice_memory_commit", lambda ids: {"title": "Synthetic", "canonical_text": "Synthetic fact", "project_scope": ["Orchard"]}),
        ("alice_capture", lambda ids: {"raw_text": "Synthetic note", "project_scope": ["Orchard"]}),
    ],
    ids=["explain", "commit", "capture"],
)
def test_a_key_stored_with_the_reserved_name_is_refused_by_the_tools_that_write_or_explain(
    world: World, monkeypatch: pytest.MonkeyPatch, tool_arguments: tuple
) -> None:
    tool, build = tool_arguments
    key = store_key(world, profile="project_scoped_agent", binding="~global")
    before = world.counts()
    with pytest.raises(MCPNotPermittedError):
        seen(world, key, monkeypatch, tool, build(world.memory_ids))
    assert world.counts() == before


@pytest.mark.parametrize("scoping", ["off", "on"])
def test_a_key_bound_to_an_ordinary_project_reads_only_that_project(
    world: World, monkeypatch: pytest.MonkeyPatch, scoping: str
) -> None:
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", scoping)
    key = store_key(world, profile="project_scoped_agent", binding=ORCHARD)
    for tool, arguments in READ_TOOLS:
        text = seen(world, key, monkeypatch, tool, arguments)
        assert LIGHTHOUSE_TEXT not in text and GLOBAL_TEXT not in text and ALICE_TEXT not in text, tool
    review = seen(world, key, monkeypatch, "alice_memory_review", {"status": "all"})
    assert ORCHARD_TEXT in review or "Fact orchard" in review
    assert "Fact lighthouse" not in review and "Fact global" not in review and "Fact alice" not in review


@pytest.mark.parametrize("scoping", ["off", "on"])
@pytest.mark.parametrize("reader", ["owner", "unbound_admin"])
def test_the_owner_and_an_unbound_admin_key_still_read_every_project(
    world: World, monkeypatch: pytest.MonkeyPatch, scoping: str, reader: str
) -> None:
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", scoping)
    key = None if reader == "owner" else store_key(world, profile="admin_agent", binding=None)
    review = seen(world, key, monkeypatch, "alice_memory_review", {"status": "all"})
    for name in ("orchard", "lighthouse", "global", "alice"):
        assert f"Fact {name}" in review, (reader, name)


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
def test_a_caller_who_sends_the_reserved_name_as_a_scope_is_still_refused_at_the_tool_edge(
    world: World, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """The refusal at the edge of the tools is unchanged: the name is not accepted as a project, a scope or an identity's scope."""

    key = store_key(world, profile="trusted_local_agent", binding=None)
    before = world.counts()
    for arguments in (
        {"status": "all", "projects": [spelling]},
        {"status": "all", "agent_identity": {"agent_id": "agent", "project_scope": [spelling]}},
    ):
        with pytest.raises(MCPInvalidRequestError):
            seen(world, key, monkeypatch, "alice_memory_review", arguments)
    assert world.counts() == before


def test_a_declared_scope_with_the_reserved_name_on_a_keyless_vault_is_refused_at_the_tool_edge(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for spelling in MARKER_SPELLINGS:
        with pytest.raises(MCPInvalidRequestError):
            seen(
                world,
                None,
                monkeypatch,
                "alice_memory_review",
                {"status": "all", "agent_identity": {"agent_id": "agent", "permission_profile": "project_scoped_agent", "project_scope": [spelling]}},
            )


def test_a_refused_key_persists_no_rejection_event_in_a_real_store(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins today's behaviour: a refusal of a key leaves no ``agent.key_escalation_rejected`` event in the store.

    ``resolve_agent_identity`` appends the event inside the transaction of the request, and the refusal that follows rolls
    that transaction back, so no event is stored, for the older reasons (a claim wider than the binding) as for the
    reserved binding. ``FakeAgentKeyStore`` never rolls back, which is why the tests above see an event. Persisting
    refusals is a separate change that has to decide how a refused request keeps a write; when it lands, this test
    changes with it. Until then the reason of the reserved binding is in the server log (see the log test above).
    """

    marker = store_key(world, profile="project_scoped_agent", binding="~global")
    bound = store_key(world, profile="project_scoped_agent", binding=ORCHARD)

    def rejections() -> list[str]:
        with world.store() as conn:
            events = SQLiteVNextStore(conn, world.user_id).list_events()
        return [event["payload_json"].get("reason") for event in events if event["event_type"] == "agent.key_escalation_rejected"]

    with pytest.raises(MCPNotPermittedError):
        seen(world, marker, monkeypatch, "alice_memory_review", {"status": "all"})
    with pytest.raises(MCPNotPermittedError):
        seen(
            world,
            bound,
            monkeypatch,
            "alice_memory_review",
            {"status": "all", "agent_identity": {"project_scope": [LIGHTHOUSE]}},
        )
    assert rejections() == []


# -- the guard ------------------------------------------------------------------------------------------------------


def test_a_guard_made_for_a_key_bound_to_the_reserved_name_admits_no_row() -> None:
    """Mutation: remove the binding check from ``LabelGuard._admits_effective``.

    The guard of a locked caller carries the binding as ``all_of``. A binding that holds the reserved name is invalid,
    so no row is admitted, not a row of a free-form project, not a row of no project, and not a row stored under the
    literal name. The same tuple as a request tuple that Alice builds (no binding) keeps its meaning.
    """

    from alicebot_api.vnext_label_guard import LabelGuard

    def row(scope: list[str]) -> dict:
        return {"id": str(uuid4()), "sensitivity": "public", "domain": "project", "metadata_json": {"project_scope": scope}}

    rows = [row([ORCHARD]), row([]), row(["~global"]), row([ALICE_PROJECT])]
    store = object()
    for projects in ((), ("~global",), (ORCHARD,)):
        locked = LabelGuard.for_filters(store, None, ("public",), projects, all_of=("~global",))
        assert locked.admit_rows("memory", rows) == [], projects
    assert LabelGuard.for_filters(store, None, ("public",), (), all_of=("  ~GLOBAL ",)).admit_rows("memory", rows) == []
    view = LabelGuard.for_filters(store, None, ("public",), ("~global",)).admit_rows("memory", rows)
    assert [item["metadata_json"]["project_scope"] for item in view] == [[ORCHARD], [], ["~global"]]
    bound = LabelGuard.for_filters(store, None, ("public",), (ORCHARD,), all_of=(ORCHARD,)).admit_rows("memory", rows)
    assert [item["metadata_json"]["project_scope"] for item in bound] == [[ORCHARD]]
