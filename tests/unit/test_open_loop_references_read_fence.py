"""A reader of an open loop is shown only the sources and memories its own read fence admits.

Unreleased (on main, not in v0.20.0). An open loop keeps the ``source_id`` and ``memory_id`` it was made with in columns
of its own, and its ``metadata_json`` can name sources and memories too. The readers of a loop filtered the loop by the
caller's domain, ceiling and project, then returned the row whole, so a key that could read a loop was handed the id of
a source or memory it could not read: a confidential source an ``admin_agent`` key had linked, or the id of another
project's source, a global source, a health source or a deleted one on a loop saved before the write fence of PR 534.
The id is not the text (``alice_memory_review`` by id, ``alice_explain`` and recall still answer nothing for it), but it
is an id and an existence signal.

Now every reader that returns a loop whole to a caller with an identity (the ``alice_open_loops`` list and its legacy
alias, the four update actions of ``alice_open_loops``, ``POST /v0/vnext/open-loops/{id}/review``,
``POST /v0/vnext/context-packs``, the create route and the scheduler's open-loop report) checks each reference against
the reader's own ``SourceReadFence`` and returns ``null`` for one it does not admit. Section 1 runs the surfaces over a
real SQLite vault with real agent keys, section 2 shows the owner and an authorized reader get every reference back,
section 3 pins the shape, section 4 calls the function directly, section 5 lists every reader of a loop and fails for
one it does not know, and section 6 covers the scheduler, the legacy tools and the words.

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the file was
restored by copying the saved copy back.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import mcp_server
from alicebot_api.config import Settings
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV, MCP_LEGACY_TOOLS_ENV
from alicebot_api.vnext_open_loop_references import (
    withhold_unreadable_references,
    withhold_unreadable_references_from_loop,
    withhold_unreadable_references_from_pack,
)
from alicebot_api.vnext_source_fence import SourceReadFence
from tests.unit.test_source_refs_read_fence import (  # noqa: F401  (``vault`` is a fixture)
    _KEY_ENV,
    _LOOP_AGENT_IDS,
    _ROOT,
    _SRC,
    _USER_ID,
    _bound_identity,
    _function_sites,
    _memories,
    _memory_row,
    _OwnerVault,
    _post_open_loop,
    _source,
    _Vault,
    vault,
)

# -- 1. every surface, every reader, over a real vault ----------------------------------------------------------

# What each key may read, by the kind names ``vault.sources`` and ``_memories`` use. A trusted key reads every domain,
# so it reads the health note; a project scoped key and a read only key may not; only the admin key is cleared for a
# confidential note. Nobody reads another project's note, a global one (a key bound to a project gets no global
# fill) or a deleted one.
_READABLE = {
    "alpha_trusted": {"own", "health"},
    "alpha_project": {"own"},
    "alpha_read_only": {"own"},
    "alpha_admin": {"own", "health", "confidential"},
}
_READERS = tuple(_READABLE)
# The keys that may update a loop (the read only key is refused, which a test below pins).
_UPDATERS = ("alpha_trusted", "alpha_project", "alpha_admin")
_KINDS = ("own", "health", "confidential", "beta", "global", "deleted")
_LABEL = "https://example.test/notes/kiln-schedule"


class _World:
    """One vault, eight loops, and what each reader must be shown of each.

    Three loops are made through the route as a key that may name the ids (own, health, confidential: the writer's
    fence at write time). The rest are written straight into the store, past the route, as a loop saved before the
    write fence was: another project's source and memory, global ones, deleted ones, an id that names no row, an id in
    an upper case spelling, and ids inside ``metadata_json``.
    """

    def __init__(self, vault: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
        self.vault = vault
        self.post = _post_open_loop(vault, monkeypatch)
        self.sources = dict(vault.sources)
        self.memories = _memories(vault)
        self.ghost = str(uuid4())
        self.ghost_memory = str(uuid4())
        self.trace_id = str(uuid4())
        self.loops: dict[str, str] = {}
        # name -> (source kind, source value as stored, memory kind, memory value as stored). A kind of None is a
        # reference no reader may be shown (it names no row).
        self.spec: dict[str, tuple[str | None, str | None, str | None, str | None]] = {}
        for name, writer in (("own", "alpha_trusted"), ("health", "alpha_trusted"), ("confidential", "alpha_admin")):
            status, body = self.post(writer, source_id=self.sources[name], memory_id=self.memories[name])
            assert status == 201, (name, body)
            self.loops[name] = str(body["open_loop"]["id"])  # type: ignore[index]
            self.spec[name] = (name, self.sources[name], name, self.memories[name])
        for kind in ("beta", "global", "deleted"):
            self._plant(f"old_{kind}", source_kind=kind, memory_kind=kind)
        self._plant("old_ghost", source_kind=None, source_value=self.ghost, memory_value=self.ghost_memory)
        self._plant("old_upper", source_kind="own", source_value=self.sources["own"].upper())
        self._plant("old_metadata", metadata=self.metadata())
        self.protected_ids = {
            *(self.sources[kind] for kind in _KINDS),
            *(self.memories[kind] for kind in _KINDS),
            self.ghost,
            self.ghost_memory,
        }

    def _plant(
        self,
        name: str,
        *,
        source_kind: str | None = None,
        source_value: str | None = None,
        memory_kind: str | None = None,
        memory_value: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        source = source_value or (self.sources[source_kind] if source_kind else None)
        memory = memory_value or (self.memories[memory_kind] if memory_kind else None)
        path = _sqlite_path_from_url(self.vault.context.database_url)
        with sqlite_user_connection(path, _USER_ID) as conn:
            row = SQLiteVNextStore(conn, _USER_ID).create_open_loop(
                {
                    "title": f"Kiln followup {name}",
                    "domain": "project",
                    "sensitivity": "internal",
                    "memory_id": memory if memory_value is None else None,
                    "metadata_json": metadata or {"project_scope": ["alpha"]},
                },
                actor_type="user",
            )
        # With foreign keys off, as an import or a restore leaves them: an id that names no row, or one in a spelling
        # the key does not match, can be stored in the column.
        if source is not None:
            self.vault.sql("UPDATE open_loops SET source_id = ? WHERE id = ?", (source, str(row["id"])))
        if memory_value is not None:
            self.vault.sql("UPDATE open_loops SET memory_id = ? WHERE id = ?", (memory, str(row["id"])))
        self.loops[name] = str(row["id"])
        self.spec[name] = (
            source_kind,
            source,
            memory_kind,
            memory,
        )

    def metadata(self) -> dict[str, object]:
        s, m = self.sources, self.memories
        return {
            "project_scope": ["alpha"],
            "source_id": s["confidential"],
            "source_refs": [s["beta"], f"source:{s['confidential']}", s["own"], _LABEL],
            "selected_source_ids": [s["global"], s["deleted"]],
            "memory_ids": [m["beta"], m["confidential"]],
            "evidence": {"quote_from": s["health"], "note": f"derived from {s['beta']} on import"},
            "run": {"trace_id": self.trace_id, "agent_run_id": "run-7"},
        }

    # -- what a reader must be shown ---------------------------------------------------------------------------

    def expected_columns(self, reader: str, name: str) -> tuple[str | None, str | None]:
        source_kind, source_value, memory_kind, memory_value = self.spec[name]
        readable = _READABLE[reader]
        return (
            source_value if source_kind in readable else None,
            memory_value if memory_kind in readable else None,
        )

    def expected_metadata(self, reader: str) -> dict[str, object]:
        """The metadata of ``old_metadata`` as ``reader`` must see it, worked out by hand from the rule: an id under a
        key that names a reference stays only if it names a row the reader may read; an id elsewhere goes only if it
        names a row the reader may not read; an id inside longer text is replaced; a label and an unrelated id stay."""

        s, m, readable = self.sources, self.memories, _READABLE[reader]
        refs = [value for value in (f"source:{s['confidential']}",) if "confidential" in readable]
        expected: dict[str, object] = {
            "project_scope": ["alpha"],
            "source_refs": [*refs, s["own"], _LABEL],
            "selected_source_ids": [],
            "memory_ids": [m["confidential"]] if "confidential" in readable else [],
            "evidence": {"note": "derived from (id withheld) on import"},
            "run": {"trace_id": self.trace_id, "agent_run_id": "run-7"},
        }
        if "confidential" in readable:
            expected["source_id"] = s["confidential"]
        if "health" in readable:
            expected["evidence"] = {"quote_from": s["health"], "note": "derived from (id withheld) on import"}  # type: ignore[assignment]
        return {key: expected[key] for key in sorted(expected)}

    def visible_ids(self, reader: str) -> set[str]:
        readable = _READABLE[reader]
        return {self.sources[kind] for kind in readable} | {self.memories[kind] for kind in readable}

    def check_payload(self, reader: str, payload: object, *, where: str) -> None:
        """No id the reader may not read is anywhere in ``payload``, and the ids it may read are."""

        text = json.dumps(payload)
        leaked = sorted(value for value in self.protected_ids - self.visible_ids(reader) if value in text)
        assert not leaked, (where, reader, leaked)
        assert self.sources["own"] in text, (where, reader, "the own source is missing: the scan would be vacuous")

    def check_item(self, reader: str, item: dict[str, object], *, where: str) -> None:
        name = next(name for name, loop_id in self.loops.items() if loop_id == item["id"])
        source, memory = self.expected_columns(reader, name)
        assert "source_id" in item and "memory_id" in item, (where, reader, name, "a withheld id keeps its key")
        assert (item["source_id"], item["memory_id"]) == (source, memory), (where, reader, name)
        if name == "old_metadata":
            assert item["metadata_json"] == self.expected_metadata(reader), (where, reader)

    # -- the surfaces ------------------------------------------------------------------------------------------

    def list_items(self, reader: str) -> list[dict[str, object]]:
        answer = self.vault.wire("alice_open_loops", {"status": "all", "limit": 100}, key=self.vault.keys[reader])
        assert answer["is_error"] is False, answer
        return answer["payload"]["items"]  # type: ignore[index, return-value]

    def update(self, reader: str, name: str) -> dict[str, object]:
        answer = self.vault.wire(
            "alice_open_loops",
            {"action": "edit", "loop_id": self.loops[name], "description": f"Reviewed by {reader}."},
            key=self.vault.keys[reader],
        )
        assert answer["is_error"] is False, (reader, name, answer)
        return answer["payload"]  # type: ignore[return-value]

    def review_route(self, reader: str, name: str) -> tuple[int, dict[str, object]]:
        from alicebot_api.routers import vnext_projects as router

        response = router.review_vnext_open_loop(
            self.loops[name],
            router.VNextOpenLoopReviewRequest(
                user_id=UUID(_USER_ID),
                action="edit",
                description=f"Reviewed over HTTP by {reader}.",
                agent_id=_LOOP_AGENT_IDS[reader],
            ),
            authorization=f"Bearer {self.vault.keys[reader]}",
        )
        return response.status_code, json.loads(response.body)

    def pack_route(self, who: str | None) -> tuple[int, dict[str, object]]:
        return _post_context_pack(self.vault, who)


def _bind_context_pack_route(vault: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /v0/vnext/context-packs`` with the real SQLite store behind it, bound the way ``_post_open_loop`` binds the
    open-loop routes (the route is Postgres only)."""

    from alicebot_api.routers import vnext_retrieval as router

    path = _sqlite_path_from_url(vault.context.database_url)

    @contextmanager
    def connection(_database_url: object, current_user_id: object):  # type: ignore[no-untyped-def]
        with sqlite_user_connection(path, str(current_user_id)) as conn:
            yield conn

    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(router, "user_connection", connection)
    monkeypatch.setattr(router, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, _USER_ID))


def _post_context_pack(vault: _Vault, who: str | None) -> tuple[int, dict[str, object]]:
    from alicebot_api.routers import vnext_retrieval as router

    response = router.create_vnext_context_pack(
        router.VNextContextPackRequest(
            user_id=UUID(_USER_ID),
            query="kiln followup loop",
            agent_id=_LOOP_AGENT_IDS[who] if who else None,
        ),
        authorization=f"Bearer {vault.keys[who]}" if who else None,
    )
    return response.status_code, json.loads(response.body)


@pytest.fixture
def world(vault: _Vault, monkeypatch: pytest.MonkeyPatch) -> _World:
    _bind_context_pack_route(vault, monkeypatch)
    return _World(vault, monkeypatch)


@pytest.mark.parametrize("reader", _READERS)
def test_the_open_loop_list_withholds_what_the_reader_may_not_read(world: _World, reader: str) -> None:
    """``alice_open_loops`` returns every loop the key may read, and each loop names only the sources and memories the
    key may read: ``null`` (the key kept) for the rest. The own-project loop is the control that keeps a blanket
    removal from passing, and the loop count is the control that keeps dropping the whole row from passing.

    On v0.20.0 and main every reader got every id. Mutations: drop the ``withhold_unreadable_references`` call in
    ``_handle_alice_vnext_open_loops``, or pass ``SourceReadFence.unfenced()`` there in place of the caller's fence (the
    reader then gets every live id), or make ``fence.admits`` return True (the reader gets every id), or return
    ``loops[:limit]`` raw next to the checked copy.
    """

    items = world.list_items(reader)
    assert sorted(item["id"] for item in items) == sorted(world.loops.values())
    for item in items:
        world.check_item(reader, item, where="list")
    world.check_payload(reader, items, where="list")


@pytest.mark.parametrize("reader", _UPDATERS)
@pytest.mark.parametrize("name", sorted(("own", "health", "confidential", "old_beta", "old_global", "old_deleted", "old_ghost", "old_upper", "old_metadata")))
def test_the_open_loop_update_actions_withhold_what_the_reader_may_not_read(world: _World, reader: str, name: str) -> None:
    """``alice_open_loops`` with ``edit`` (and ``close``, ``snooze``, ``reopen``, which share the branch) returns the
    updated row. A key that may update the loop may not thereby read the source and memory it points at.

    Mutations: return ``updated`` instead of the checked copy in ``_handle_alice_open_loops``, or build the fence there
    from ``SourceReadFence.unfenced()``.
    """

    payload = world.update(reader, name)
    assert payload["action"] == "edit"
    item = payload["open_loop"]
    assert isinstance(item, dict)
    world.check_item(reader, item, where="update")
    text = json.dumps(item)
    assert not [value for value in world.protected_ids - world.visible_ids(reader) if value in text]


@pytest.mark.parametrize("action", ["close", "snooze", "reopen"])
def test_the_other_update_actions_take_the_same_branch(world: _World, action: str) -> None:
    """The branch is shared, so close, snooze and reopen are held to the rule as edit is. Checked on the loop that
    holds the most references, as the project scoped key.

    Mutation: as for the edit test, which fails first, and the same one makes this fail.
    """

    arguments: dict[str, object] = {"action": action, "loop_id": world.loops["old_metadata"]}
    if action == "snooze":
        arguments["due_at"] = "2030-01-01T00:00:00Z"
    answer = world.vault.wire("alice_open_loops", arguments, key=world.vault.keys["alpha_project"])
    assert answer["is_error"] is False, answer
    world.check_item("alpha_project", answer["payload"]["open_loop"], where=action)  # type: ignore[index]


def test_a_read_only_key_cannot_update_a_loop_so_the_update_surfaces_are_not_a_way_around_the_list(
    world: _World,
) -> None:
    """The read only key is refused on both update surfaces, which is why the list is its only door to a loop. It is
    held to the same rule there.

    Mutation: none of its own; it fails if the profile ever gains ``open_loop.update``, so the update tests above must
    then include it.
    """

    refused = world.vault.wire(
        "alice_open_loops",
        {"action": "edit", "loop_id": world.loops["own"], "description": "x"},
        key=world.vault.keys["alpha_read_only"],
    )
    assert refused["is_error"] is True and refused["payload"]["error"]["code"] == "not_permitted"  # type: ignore[index]
    status, _body = world.review_route("alpha_read_only", "own")
    assert status == 403


@pytest.mark.parametrize("reader", _UPDATERS)
@pytest.mark.parametrize("name", sorted(("own", "health", "confidential", "old_beta", "old_global", "old_deleted", "old_ghost", "old_upper", "old_metadata")))
def test_the_http_review_route_withholds_what_the_reader_may_not_read(world: _World, reader: str, name: str) -> None:
    """``POST /v0/vnext/open-loops/{id}/review`` returns the updated row.

    Mutations: return ``updated`` instead of the checked copy in ``review_vnext_open_loop``, or build its fence from
    ``SourceReadFence.unfenced()``.
    """

    status, body = world.review_route(reader, name)
    assert status == 200, (reader, name, body)
    world.check_item(reader, body, where="review route")  # type: ignore[arg-type]


@pytest.mark.parametrize("reader", _READERS)
def test_the_http_context_pack_withholds_what_the_reader_may_not_read(world: _World, reader: str) -> None:
    """``POST /v0/vnext/context-packs`` returns the open loops it selects as whole rows. Every id of every refused kind
    is gone from the whole pack, not only from ``open_loops``, and each loop that is in the pack carries what the list
    would show the same reader.

    Mutations: drop the ``withhold_unreadable_references_from_pack`` call in ``create_vnext_context_pack`` (every
    reader gets every id), or hand it ``SourceReadFence.unfenced()``.
    """

    status, pack = world.pack_route(reader)
    assert status == 201, pack
    loops = pack["open_loops"]
    assert len(loops) >= 6, "the pack must carry most of the loops, or the scan below proves little"  # type: ignore[arg-type]
    for item in loops:  # type: ignore[attr-defined]
        world.check_item(reader, item, where="pack")
    # ``recent_changes`` lists the creation event of every memory in the request's scope, by id, and the pack applies no
    # domain fence when the call names no domains, so it names the vault's health memory to a project scoped key. That
    # is the event list's own behavior, not a loop reference, and is not changed here.
    world.check_payload(reader, {key: value for key, value in pack.items() if key != "recent_changes"}, where="pack")


def test_the_mcp_context_pack_and_resume_return_no_reference_at_all(world: _World) -> None:
    """The compact tools return a fixed list of fields per loop and never ``source_id``, ``memory_id`` or
    ``metadata_json``. They are clean by that list, not by the fence, so this pins the result on the vault and the
    classification test in section 5 pins the list. The scan looks for the ids the reader may not read, anywhere in
    the answer.

    Mutation: add ``source_id`` to ``_COMPACT_OPEN_LOOP_FIELDS`` in ``mcp/context.py`` (the compact pack then carries
    the raw id of a loop's source to a key that may not read it).
    """

    for reader in _READERS:
        key = world.vault.keys[reader]
        for name, arguments in (
            ("alice_context_pack", {"query": "kiln followup loop", "max_tokens": 4000}),
            ("alice_resume", {"query": "kiln followup"}),
        ):
            answer = world.vault.wire(name, arguments, key=key)
            assert answer["is_error"] is False, (name, reader, answer)
            # Without ``recent_changes``, which names memory creation events by id (see the pack route test).
            text = json.dumps({key: value for key, value in answer["payload"].items() if key != "recent_changes"})
            leaked = [value for value in world.protected_ids - world.visible_ids(reader) if value in text]
            assert not leaked, (name, reader, leaked)
            assert "kiln followup" in text.lower(), (name, reader, "the loops are in the answer")


def test_the_create_route_answers_with_the_same_rule(world: _World) -> None:
    """The loop the route stores goes back through the one output function. The ids were checked for this caller a
    moment earlier, so an admitted id is returned as it was stored, and the answer is the stored row.

    Mutation: remove the ``withhold_unreadable_references_from_loop`` call in ``create_vnext_open_loop`` and answer
    ``created`` (nothing fails here, which is why the AST test of section 5 names the call).
    """

    status, body = world.post("alpha_admin", source_id=world.sources["confidential"], memory_id=world.memories["confidential"])
    assert status == 201
    assert body["open_loop"]["source_id"] == world.sources["confidential"]  # type: ignore[index]
    assert body["open_loop"]["memory_id"] == world.memories["confidential"]  # type: ignore[index]


def test_a_keyless_caller_that_declares_a_profile_is_held_to_that_profile_on_the_legacy_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no agent key on the server the legacy tools are served and a call may declare an identity.
    ``alice_vnext_open_loops`` (the same handler as the core list) holds a declared ``project_scoped_agent`` to its own
    fence: the confidential and the health source are withheld, the own-project one is shown. A declared ``admin_agent``
    gets them, and a call that declares nothing is the owner's and gets them all. (The legacy pack tool takes no
    identity argument, so it is always the owner's.)

    Mutation: drop the ``withhold_unreadable_references`` call in ``_handle_alice_vnext_open_loops`` (the declared
    project scoped agent then gets the confidential and the health id), or give it ``SourceReadFence.unfenced()``.
    """

    database = resolve_db_path(data_dir=str(tmp_path / "legacy"), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    owner = _OwnerVault(MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID)))

    def capture(text: str, **extra: object) -> str:
        done = owner.wire(
            "alice_capture",
            {"raw_text": text, "title": text[:24], "project_scope": ["alpha"], **extra},
            key=None,
        )
        assert done["is_error"] is False, done
        return str(done["payload"]["source_id"])  # type: ignore[index]

    own = capture("Kiln note: the alpha kiln runs on Mondays.", domain="project", sensitivity="internal")
    health = capture("Health note: knee rehab twice a week.", domain="health", sensitivity="internal")
    confidential = capture("Quote note: the supplier quote was cedar-ledger-55.", domain="project", sensitivity="confidential")
    path = _sqlite_path_from_url(owner.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        for kind, source_id in (("own", own), ("health", health), ("confidential", confidential)):
            store.create_open_loop(
                {
                    "title": f"Kiln followup {kind}",
                    "domain": "project",
                    "sensitivity": "internal",
                    "source_id": source_id,
                    "metadata_json": {"project_scope": ["alpha"]},
                },
                actor_type="user",
            )

    def legacy(name: str, arguments: dict[str, object]) -> dict[str, object]:
        os.environ.pop(_KEY_ENV, None)
        os.environ[MCP_FULL_TOOLS_ENV] = "1"
        os.environ[MCP_LEGACY_TOOLS_ENV] = "1"
        try:
            server = mcp_server.MCPServer(context=owner.context, input_stream=BytesIO(), output_stream=BytesIO())
            response = server._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            )
        finally:
            os.environ.pop(MCP_FULL_TOOLS_ENV, None)
            os.environ.pop(MCP_LEGACY_TOOLS_ENV, None)
        assert response is not None
        assert response["result"]["isError"] is False, response
        return json.loads(response["result"]["content"][0]["text"])  # type: ignore[no-any-return]

    declared_project = {"agent_id": "legacy-project-agent", "permission_profile": "project_scoped_agent", "project_scope": ["alpha"]}
    declared_admin = {"agent_id": "legacy-admin-agent", "permission_profile": "admin_agent", "project_scope": ["alpha"]}
    text = {
        who: json.dumps(legacy("alice_vnext_open_loops", {"status": "all", **identity}))
        for who, identity in (("project", declared_project), ("admin", declared_admin), ("owner", {}))
    }
    assert own in text["project"]
    assert health not in text["project"] and confidential not in text["project"]
    assert own in text["admin"] and confidential in text["admin"] and health in text["admin"]
    assert own in text["owner"] and confidential in text["owner"] and health in text["owner"]


# -- 2. the owner and an authorized reader are not changed --------------------------------------------------------

_VOLATILE = ("source_id", "memory_id", "metadata_json")


def _raw_rows(vault: _Vault) -> dict[str, dict[str, object]]:
    """Every loop as the store holds it, by id, in the JSON form the tools answer in."""

    path = _sqlite_path_from_url(vault.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        rows = SQLiteVNextStore(conn, _USER_ID).list_open_loops(status=None, limit=100)
    return {str(row["id"]): json.loads(json.dumps(row, default=str)) for row in rows}


def test_an_admin_key_gets_every_reference_it_may_read_and_the_row_is_otherwise_the_stored_row(world: _World) -> None:
    """The authorized reader. The ``admin_agent`` key may read the own, health and confidential rows, so on those
    three loops the list returns the stored row exactly, every field, and on the loops that point at rows nobody
    here may read it returns the stored row with ``source_id`` and ``memory_id`` ``null`` and nothing else different.

    Mutations: make the fence refuse an ``admin_agent`` identity (the admin loses its confidential reference), or
    make ``withhold_unreadable_references`` clear every reference (the three loops differ from the stored rows).
    """

    raw = _raw_rows(world.vault)
    items = {str(item["id"]): item for item in world.list_items("alpha_admin")}
    assert set(items) == set(raw) == set(world.loops.values())
    for name in ("own", "health", "confidential"):
        assert items[world.loops[name]] == raw[world.loops[name]], name
    for name, loop_id in world.loops.items():
        got, stored = items[loop_id], raw[loop_id]
        for column in ("source_id", "memory_id"):
            assert got[column] in (stored[column], None), (name, column)
        if name != "old_metadata":
            assert got["metadata_json"] == stored["metadata_json"], name
        rest = {key: value for key, value in got.items() if key not in _VOLATILE}
        assert rest == {key: value for key, value in stored.items() if key not in _VOLATILE}, name
    assert items[world.loops["confidential"]]["source_id"] == world.sources["confidential"]
    assert items[world.loops["confidential"]]["memory_id"] == world.memories["confidential"]


@pytest.mark.parametrize("reader", ["alpha_trusted", "alpha_project", "alpha_read_only"])
def test_a_lower_reader_still_gets_every_loop_and_only_the_references_differ(world: _World, reader: str) -> None:
    """The loops are the reader's to read and they are all still returned, with every field the store holds. Only
    ``source_id``, ``memory_id`` and the references inside ``metadata_json`` differ from the stored row, so the fix
    withholds a reference and not the loop.

    Mutation: drop a loop whose reference is refused instead of clearing the reference (the count and the id set fail),
    or clear another field (``title``) together with the reference.
    """

    raw = _raw_rows(world.vault)
    items = {str(item["id"]): item for item in world.list_items(reader)}
    assert set(items) == set(raw)
    for loop_id, stored in raw.items():
        got = {key: value for key, value in items[loop_id].items() if key not in _VOLATILE}
        assert got == {key: value for key, value in stored.items() if key not in _VOLATILE}, loop_id


def _owner_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_OwnerVault, dict[str, str], dict[str, str]]:
    """A vault with no agent key (so a keyless call is the owner's), four sources and three memories the keys of the
    other vault could not all read, and one loop on each, made through the route as the owner."""

    database = resolve_db_path(data_dir=str(tmp_path / "owner"), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    owner = _OwnerVault(MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID)))
    other = owner.wire(
        "alice_capture",
        {"raw_text": "Beta kiln log: firing at cone-six-914.", "title": "Beta log", "domain": "project",
         "sensitivity": "internal", "project_scope": ["beta"]},
        key=None,
    )
    assert other["is_error"] is False, other
    sources = {
        "other_project": str(other["payload"]["source_id"]),  # type: ignore[index]
        "global": owner._capture("Global note: the studio closes for the holiday, bell-48.", None),
        "confidential": owner._capture("Quote note: the supplier quote was cedar-ledger-55.", None, sensitivity="confidential"),
        "health": owner._capture("Health note: knee rehab uses brine-wrap-33.", None, domain="health"),
    }
    memories = {kind: str(owner.commit(None, [])["payload"]["memory"]["id"]) for kind in ("global", "confidential", "health")}  # type: ignore[index]
    owner.sql("UPDATE memories SET sensitivity = 'confidential' WHERE id = ?", (memories["confidential"],))
    owner.sql("UPDATE memories SET domain = 'health' WHERE id = ?", (memories["health"],))
    _bind_context_pack_route(owner, monkeypatch)
    return owner, sources, memories


def test_the_owner_is_shown_every_live_reference_on_every_surface_exactly_as_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner is a call with no agent identity and is not fenced. Loops that point at another project's source, a
    global source, a confidential source, a health source and the memories of the last three come back from the list,
    the review route and the context pack with every id, and the list returns each stored row exactly (every field,
    ``metadata_json`` included).

    Mutations: build the fence in any of the three surfaces from a bound identity instead of
    ``SourceReadFence.for_identity(identity)`` (the owner then loses the other project's id), or make
    ``for_identity(None)`` return a fence that refuses what ``unfenced()`` admits.
    """

    owner, sources, memories = _owner_world(tmp_path, monkeypatch)
    post = _post_open_loop(owner, monkeypatch)
    loop_ids: dict[str, str] = {}
    for name, fields in (
        ("other_project", {"source_id": sources["other_project"]}),
        ("global", {"source_id": sources["global"], "memory_id": memories["global"]}),
        ("confidential", {"source_id": sources["confidential"], "memory_id": memories["confidential"]}),
        ("health", {"source_id": sources["health"], "memory_id": memories["health"]}),
    ):
        status, body = post(None, **fields)
        assert status == 201, (name, body)
        loop_ids[name] = str(body["open_loop"]["id"])  # type: ignore[index]
    raw = _raw_rows(owner)
    assert set(loop_ids.values()) <= set(raw)

    listed = owner.wire("alice_open_loops", {"status": "all", "limit": 100}, key=None)
    assert listed["is_error"] is False, listed
    items = {str(item["id"]): item for item in listed["payload"]["items"]}  # type: ignore[index]
    assert set(items) == set(raw)
    for loop_id in loop_ids.values():
        assert items[loop_id] == raw[loop_id]

    from alicebot_api.routers import vnext_projects as projects_router

    for name, loop_id in loop_ids.items():
        response = projects_router.review_vnext_open_loop(
            loop_id,
            projects_router.VNextOpenLoopReviewRequest(user_id=UUID(_USER_ID), action="edit", description=f"Looked at {name}."),
            authorization=None,
        )
        assert response.status_code == 200, name
        reviewed = json.loads(response.body)
        assert (reviewed["source_id"], reviewed["memory_id"]) == (raw[loop_id]["source_id"], raw[loop_id]["memory_id"]), name

    status, pack = _post_context_pack(owner, None)
    assert status == 201, pack
    for item in pack["open_loops"]:  # type: ignore[attr-defined]
        assert (item["source_id"], item["memory_id"]) == (raw[item["id"]]["source_id"], raw[item["id"]]["memory_id"])
    text = json.dumps(pack["open_loops"])
    for value in (*sources.values(), *memories.values()):
        assert value in text


def test_a_loop_that_points_at_a_deleted_source_shows_the_owner_no_id_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one place the owner's view changes, and it is the fence's own rule: ``SourceReadFence.unfenced()`` admits any
    live row and refuses a deleted one, so a loop that points at a source or memory the owner has since forgotten shows
    ``null`` there, as every key reader sees it. The live reference on the loop beside it is shown. (An id in
    ``metadata_json`` under a reference key that names no row, or a deleted one, goes the same way.)

    Mutation: make ``withhold_unreadable_references`` skip the fence when ``fence.identity is None`` (the owner then
    sees the deleted id again), or drop the ``deleted_at`` check of ``SourceReadFence._admits``.
    """

    owner, sources, _memories_unused = _owner_world(tmp_path, monkeypatch)
    forgotten = owner._capture("Forgotten note: the old kiln log said ember-chart-91.", None)
    path = _sqlite_path_from_url(owner.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        loop = store.create_open_loop(
            {"title": "Kiln followup forgotten", "domain": "project", "sensitivity": "internal", "source_id": forgotten,
             "metadata_json": {"source_refs": [forgotten, sources["global"]], "source_id": forgotten}},
            actor_type="user",
        )
    owner.sql("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (forgotten,))
    listed = owner.wire("alice_open_loops", {"status": "all", "limit": 100}, key=None)
    item = next(row for row in listed["payload"]["items"] if row["id"] == loop["id"])  # type: ignore[index]
    assert item["source_id"] is None
    assert item["metadata_json"] == {"source_refs": [sources["global"]]}


# -- 3. the shape: a protected, a deleted and a missing reference read alike -------------------------------------


def test_a_protected_a_deleted_and_a_missing_reference_read_alike(world: _World) -> None:
    """For the project scoped key the loops that point at another project's rows (``old_beta``), at deleted rows
    (``old_deleted``) and at ids that name no row (``old_ghost``) come back with the same two fields ``null`` and the
    key present, the same set of keys and the same metadata, so the answer does not say which of the three it was.

    Mutations: make the column rule return a different value for a refused row than for a missing one (``""`` for a
    refused row), or leave a deleted reference in place (``old_deleted`` then differs).
    """

    items = {str(item["id"]): item for item in world.list_items("alpha_project")}
    states = [items[world.loops[name]] for name in ("old_beta", "old_deleted", "old_ghost")]
    for item in states:
        assert (item["source_id"], item["memory_id"]) == (None, None)
    assert len({tuple(sorted(item)) for item in states}) == 1
    assert len({json.dumps(item["metadata_json"], sort_keys=True) for item in states}) == 1
    normalised = [
        json.dumps({key: value for key, value in item.items() if key not in ("id", "title", "created_at", "updated_at", "opened_at")}, sort_keys=True)
        for item in states
    ]
    assert len(set(normalised)) == 1, normalised


# -- 4. the function, by direct call ----------------------------------------------------------------------------


class _Rows:
    """A store with the two bulk lookups. It records every call, and returns deleted rows too if it holds them."""

    def __init__(self, sources: list[dict[str, object]] = (), memories: list[dict[str, object]] = ()) -> None:  # type: ignore[assignment]
        self.sources = {str(row["id"]).lower(): row for row in sources}
        self.memories = {str(row["id"]).lower(): row for row in memories}
        self.source_calls: list[list[str]] = []
        self.memory_calls: list[list[str]] = []

    def get_sources_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
        self.source_calls.append(list(ids))
        return [self.sources[i] for i in ids if i in self.sources]

    def get_memories_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
        self.memory_calls.append(list(ids))
        return [self.memories[i] for i in ids if i in self.memories]


_FENCE = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))


def _loop(**fields: object) -> dict[str, object]:
    return {"id": str(uuid4()), "title": "Kiln followup", "source_id": None, "memory_id": None, "metadata_json": {}, **fields}


def _own_source() -> dict[str, object]:
    return _source(str(uuid4()), scope=["alpha"])


def _own_memory() -> dict[str, object]:
    return _memory_row(str(uuid4()), scope=["alpha"])


def test_an_admitted_id_in_any_spelling_the_link_writer_reads_is_found_and_returned_as_stored() -> None:
    """Upper case, no hyphens, braces, ``urn:uuid:``, a ``source:`` prefix and padding all name the stored source, and
    ``memory:`` and the plain spellings name the memory. The lookup is made with the canonical id, and the value the
    caller gets back is the stored one, untouched.

    Mutations: drop the ``.strip()`` or the prefix handling of ``_canonical_id`` (the prefixed and padded spellings are
    then withheld), or return the canonical id instead of the stored value (the stored spelling comes back changed).
    """

    source, memory = _own_source(), _own_memory()
    source_id, memory_id = str(source["id"]), str(memory["id"])
    store = _Rows([source], [memory])
    spellings = [
        source_id.upper(),
        source_id.replace("-", ""),
        f"{{{source_id}}}",
        f"urn:uuid:{source_id}",
        f"source:{source_id}",
        f"  {source_id}  ",
    ]
    loops = [_loop(source_id=value) for value in spellings] + [
        _loop(memory_id=value) for value in (memory_id.upper(), memory_id.replace("-", ""), f"memory:{memory_id}")
    ]
    out = withhold_unreadable_references(store, loops, fence=_FENCE)
    assert [row["source_id"] for row in out[: len(spellings)]] == spellings
    assert [row["memory_id"] for row in out[len(spellings) :]] == [memory_id.upper(), memory_id.replace("-", ""), f"memory:{memory_id}"]
    assert store.source_calls == [[source_id]]
    assert store.memory_calls == [[memory_id]]


@pytest.mark.parametrize("value", ["not-an-id", "", "   ", "12345", "00000000-0000-0000-0000-00000000000g", 12345])
def test_a_value_that_is_no_id_in_any_spelling_is_withheld_like_a_missing_one(value: object) -> None:
    """A column holding something that is no id at all is a reference to nothing: ``null``, the key kept, and no store
    call is made for it.

    Mutation: let ``_canonical_id`` return the text when it does not parse (the value is then looked up, found
    nowhere and withheld, so the mutation to make is to skip the withholding for a value that did not parse).
    """

    store = _Rows()
    out = withhold_unreadable_references(store, [_loop(source_id=value, memory_id=value)], fence=_FENCE)
    assert out[0]["source_id"] is None and out[0]["memory_id"] is None
    assert "source_id" in out[0] and "memory_id" in out[0]
    assert store.source_calls == [] and store.memory_calls == []


def test_rows_and_columns_that_hold_uuid_objects_are_matched_as_the_postgres_store_returns_them() -> None:
    """The Postgres store returns ``id`` and the reference columns as ``UUID`` objects and SQLite as text. No Postgres
    runs here, so this stub stands in for the one thing the two stores do differently. The object is returned as it
    came.

    Mutation: drop the ``str(...)`` around ``row.get("id")`` in ``_rows_by_id`` or around the column value in
    ``_canonical_id`` (the own reference is then withheld).
    """

    source, memory = _own_source(), _own_memory()
    source["id"], memory["id"] = UUID(str(source["id"])), UUID(str(memory["id"]))
    loop = _loop(source_id=source["id"], memory_id=memory["id"])
    out = withhold_unreadable_references(_Rows([source], [memory]), [loop], fence=_FENCE)
    assert out[0]["source_id"] is source["id"] and out[0]["memory_id"] is memory["id"]


def test_every_refused_state_of_a_reference_gives_the_same_row() -> None:
    """A source or memory in another project, one the reader's domain or ceiling refuses, a deleted one that a store
    hands over anyway, and an id the store does not hold all answer with the same row: ``null`` with the key kept.

    Mutations: make ``admits`` or ``admits_memory`` return True, drop the ``deleted_at`` check of ``_admits``, or
    give a missing id a different value from a refused one.
    """

    other_project = _source(str(uuid4()), scope=["beta"])
    restricted = _source(str(uuid4()), scope=["alpha"], sensitivity="confidential")
    deleted = _source(str(uuid4()), scope=["alpha"], deleted_at="2026-01-01T00:00:00Z")
    missing = str(uuid4())
    other_memory = _memory_row(str(uuid4()), scope=["beta"])
    restricted_memory = _memory_row(str(uuid4()), scope=["alpha"], sensitivity="confidential")
    deleted_memory = _memory_row(str(uuid4()), scope=["alpha"], deleted_at="2026-01-01T00:00:00Z")
    store = _Rows([other_project, restricted, deleted], [other_memory, restricted_memory, deleted_memory])
    loops = [
        _loop(source_id=other_project["id"], memory_id=other_memory["id"]),
        _loop(source_id=restricted["id"], memory_id=restricted_memory["id"]),
        _loop(source_id=deleted["id"], memory_id=deleted_memory["id"]),
        _loop(source_id=missing, memory_id=str(uuid4())),
    ]
    out = withhold_unreadable_references(store, loops, fence=_FENCE)
    for row in out:
        assert (row["source_id"], row["memory_id"]) == (None, None)
        assert set(row) == set(loops[0])


def test_a_store_that_cannot_look_rows_up_withholds_every_reference() -> None:
    """A store with no bulk lookup and no single one admits nothing, so every reference is withheld and the rest of
    the metadata stays. A store with only the single-id reads is asked one id at a time and works.

    Mutation: treat a store without the lookups as admitting everything (the references are then returned).
    """

    source_id, memory_id = str(uuid4()), str(uuid4())
    loop = _loop(source_id=source_id, memory_id=memory_id, metadata_json={"source_id": source_id, "kept": "x"})
    out = withhold_unreadable_references(object(), [loop], fence=_FENCE)
    assert (out[0]["source_id"], out[0]["memory_id"]) == (None, None)
    assert out[0]["metadata_json"] == {"kept": "x"}

    class _Single:
        def __init__(self, source: dict[str, object], memory: dict[str, object]) -> None:
            self.source, self.memory = source, memory
            self.asked: list[str] = []

        def get_source(self, source_id: str) -> dict[str, object] | None:
            self.asked.append(f"source {source_id}")
            return self.source if source_id == self.source["id"] else None

        def get_memory(self, memory_id: str) -> dict[str, object] | None:
            self.asked.append(f"memory {memory_id}")
            return self.memory if memory_id == self.memory["id"] else None

    source, memory = _own_source(), _own_memory()
    single = _Single(source, memory)
    out = withhold_unreadable_references(
        single, [_loop(source_id=source["id"], memory_id=memory["id"])], fence=_FENCE
    )
    assert (out[0]["source_id"], out[0]["memory_id"]) == (source["id"], memory["id"])
    assert single.asked == [f"source {source['id']}", f"memory {memory['id']}"]


def test_ids_are_looked_up_in_batches_and_once_per_kind_however_many_loops_name_them() -> None:
    """1,200 distinct source ids are read in slices of at most 500 (a SQLite build from before 3.32 allows 999
    variables), the same for memories, and a source named by thirty loops is asked for once. A page of loops costs one
    read per kind, not one per loop.

    Mutations: look rows up one loop at a time, drop the slicing (one call of 1,200 ids), or skip the de-duplication
    (the thirty loops ask thirty times).
    """

    sources = [_own_source() for _ in range(1200)]
    memories = [_own_memory() for _ in range(1200)]
    store = _Rows(sources, memories)
    loops = [_loop(source_id=s["id"], memory_id=m["id"]) for s, m in zip(sources, memories)]
    out = withhold_unreadable_references(store, loops, fence=_FENCE)
    assert all(row["source_id"] is not None and row["memory_id"] is not None for row in out)
    assert [len(call) for call in store.source_calls] == [500, 500, 200]
    assert [len(call) for call in store.memory_calls] == [500, 500, 200]

    shared_source, shared_memory = _own_source(), _own_memory()
    store = _Rows([shared_source], [shared_memory])
    withhold_unreadable_references(
        store, [_loop(source_id=shared_source["id"], memory_id=shared_memory["id"]) for _ in range(30)], fence=_FENCE
    )
    assert store.source_calls == [[shared_source["id"]]] and store.memory_calls == [[shared_memory["id"]]]


def test_nothing_is_read_when_the_rows_name_nothing() -> None:
    """Loops with no reference and metadata with no id make no store call, so the usual loop costs nothing.

    Mutation: read the store before looking for ids.
    """

    store = _Rows()
    loops = [_loop(), _loop(metadata_json={"created_from": "vnext_workspace", "label": "plain text"})]
    out = withhold_unreadable_references(store, loops, fence=_FENCE)
    assert out == loops
    assert store.source_calls == [] and store.memory_calls == []
    assert withhold_unreadable_references(store, [], fence=_FENCE) == []


def test_the_rows_that_come_in_are_not_changed_and_a_row_with_nothing_to_withhold_is_equal_and_keeps_its_metadata() -> None:
    """The input is copied. A loop with nothing withheld comes back equal and holds the same ``metadata_json`` object,
    and a loop that loses a reference leaves the input's metadata as it was.

    Mutation: edit the input row in place, or rebuild the metadata of a row that lost nothing.
    """

    source = _own_source()
    hidden = _source(str(uuid4()), scope=["beta"])
    clean = _loop(source_id=source["id"], metadata_json={"source_id": source["id"], "run": {"n": 1}})
    dirty_metadata = {"source_refs": [hidden["id"], source["id"]]}
    dirty = _loop(source_id=hidden["id"], metadata_json=dirty_metadata)
    before = json.dumps([clean, dirty], sort_keys=True)
    out = withhold_unreadable_references(_Rows([source, hidden]), [clean, dirty], fence=_FENCE)
    assert json.dumps([clean, dirty], sort_keys=True) == before
    assert out[0] == clean and out[0]["metadata_json"] is clean["metadata_json"]
    assert out[1]["source_id"] is None and out[1]["metadata_json"] == {"source_refs": [source["id"]]}
    assert dirty["metadata_json"] is dirty_metadata


def test_a_key_the_row_does_not_have_is_not_added() -> None:
    """A compact row that never had ``source_id`` or ``memory_id`` does not gain them (their absence is a shape of
    its own), and a row with no ``metadata_json`` stays so.

    Mutation: set ``row["source_id"] = None`` unconditionally.
    """

    out = withhold_unreadable_references(_Rows(), [{"id": "x", "title": "t"}], fence=_FENCE)
    assert out == [{"id": "x", "title": "t"}]


def _meta_ids() -> dict[str, str]:
    return {name: str(uuid4()) for name in ("mine", "hidden", "unknown", "mem_mine", "mem_hidden")}


@pytest.mark.parametrize(
    ("label", "metadata", "expected"),
    [
        ("scalar source_id, refused", lambda i: {"source_id": i["hidden"]}, lambda i: {}),
        ("scalar source_id, admitted", lambda i: {"source_id": i["mine"]}, lambda i: {"source_id": i["mine"]}),
        (
            "list under source_refs",
            lambda i: {"source_refs": [i["hidden"], i["mine"], "a label"]},
            lambda i: {"source_refs": [i["mine"], "a label"]},
        ),
        ("an id that names no row under a reference key", lambda i: {"source_refs": [i["unknown"]]}, lambda i: {"source_refs": []}),
        (
            "dicts under a reference key",
            lambda i: {"source_refs": [{"id": i["hidden"]}, {"id": i["mine"], "quote": "q"}]},
            lambda i: {"source_refs": [{}, {"id": i["mine"], "quote": "q"}]},
        ),
        ("source: prefix", lambda i: {"selected_source_ids": f"source:{i['hidden']}"}, lambda i: {}),
        ("upper case", lambda i: {"source_ids": [i["hidden"].upper()]}, lambda i: {"source_ids": []}),
        ("no hyphens", lambda i: {"source_ref": i["hidden"].replace("-", "")}, lambda i: {}),
        ("memory key", lambda i: {"memory_ids": [i["mem_hidden"], i["mem_mine"]]}, lambda i: {"memory_ids": [i["mem_mine"]]}),
        ("memory id that names no row", lambda i: {"memory_id": i["unknown"]}, lambda i: {}),
        ("a refused id under an unknown key", lambda i: {"anything": i["hidden"]}, lambda i: {}),
        ("a refused memory id under an unknown key", lambda i: {"anything": [i["mem_hidden"]]}, lambda i: {"anything": []}),
        (
            "an unrelated id under an unknown key",
            lambda i: {"trace_id": i["unknown"], "run": {"id": i["mine"]}},
            lambda i: {"trace_id": i["unknown"], "run": {"id": i["mine"]}},
        ),
        (
            "an id inside longer text",
            lambda i: {"note": f"see {i['hidden']} and {i['mine']}"},
            lambda i: {"note": f"see (id withheld) and {i['mine']}"},
        ),
        ("an id as a key", lambda i: {i["hidden"]: "x", "kept": 1}, lambda i: {"kept": 1}),
        (
            "values that are no text",
            lambda i: {"source_id": 7, "source_refs": [None, True, 1.5], "n": {"deep": [1, 2]}},
            lambda i: {"source_id": 7, "source_refs": [None, True, 1.5], "n": {"deep": [1, 2]}},
        ),
        (
            "everything below a reference key is held to the strict rule",
            lambda i: {"source_refs": {"evidence": {"trace": i["unknown"], "ok": i["mine"]}}},
            lambda i: {"source_refs": {"evidence": {"ok": i["mine"]}}},
        ),
        (
            "a key that names a reference is matched without regard to case",
            lambda i: {"Source_Refs": [i["unknown"], i["mine"]]},
            lambda i: {"Source_Refs": [i["mine"]]},
        ),
    ],
)
def test_the_metadata_of_a_loop_loses_the_ids_the_rule_says_and_nothing_else(label: str, metadata, expected) -> None:  # type: ignore[no-untyped-def]
    """The rule, one case at a time. Under a key that names a reference (and below it) an id stays only if it names a
    row the reader may read, so a refused, a deleted and an unknown id read alike. Elsewhere an id goes only if it names
    a row the reader may not read, because an id under an unknown key may be a trace id. Text around an id stays.

    Mutations: apply the strict rule everywhere (the unrelated ids go), apply the generic rule everywhere (the unknown
    id under a reference key stays), skip the key scan (the id as a key stays), compare reference keys case sensitively
    (the ``Source_Refs`` row), or stop recursing into dicts or lists.
    """

    ids = _meta_ids()
    store = _Rows(
        [_source(ids["mine"], scope=["alpha"]), _source(ids["hidden"], scope=["beta"])],
        [_memory_row(ids["mem_mine"], scope=["alpha"]), _memory_row(ids["mem_hidden"], scope=["beta"])],
    )
    out = withhold_unreadable_references(store, [_loop(metadata_json=metadata(ids))], fence=_FENCE)
    assert out[0]["metadata_json"] == expected(ids), label


def test_metadata_nested_deeper_than_the_scan_reads_is_dropped_and_does_not_raise() -> None:
    """Text a store let through can nest deeper than the product writes. Recursing without a bound would raise
    ``RecursionError``, so the part below the limit is dropped and the rest is returned.

    Mutation: remove the depth check in ``_scrub`` (the call raises ``RecursionError`` on 3,000 levels).
    """

    deep: object = "bottom"
    for _ in range(3000):
        deep = {"k": deep}
    out = withhold_unreadable_references(_Rows(), [_loop(metadata_json={"kept": 1, "deep": deep})], fence=_FENCE)
    metadata = out[0]["metadata_json"]
    assert isinstance(metadata, dict) and metadata["kept"] == 1
    depth, node = 0, metadata["deep"]
    while isinstance(node, dict) and "k" in node:
        depth, node = depth + 1, node["k"]
    assert depth < 100 and "bottom" not in json.dumps(metadata)


def test_the_owners_fence_admits_a_live_row_of_any_project_and_refuses_a_deleted_one() -> None:
    """``SourceReadFence.unfenced()`` is the owner's fence. It admits another project's row and a global one, and
    refuses a deleted one, so the owner is shown every live reference and no reference to a forgotten row.

    Mutation: make ``unfenced()`` build a fence from an identity (the other project's row is then withheld).
    """

    live_other = _source(str(uuid4()), scope=["beta"])
    live_global = _source(str(uuid4()), scope=[])
    forgotten = _source(str(uuid4()), scope=["alpha"], deleted_at="2026-01-01T00:00:00Z")
    out = withhold_unreadable_references(
        _Rows([live_other, live_global, forgotten]),
        [_loop(source_id=row["id"]) for row in (live_other, live_global, forgotten)],
        fence=SourceReadFence.unfenced(),
    )
    assert [row["source_id"] for row in out] == [live_other["id"], live_global["id"], None]


def test_the_pack_form_checks_open_loops_only_and_hands_back_a_copy() -> None:
    """The pack form replaces ``open_loops`` with checked copies, leaves every other section as the same object, hands
    back a new dict, and returns a pack with no ``open_loops`` unchanged. An entry that is not a mapping cannot be
    checked and is dropped.

    Mutation: edit the pack in place, or leave a non-mapping entry in.
    """

    hidden = _source(str(uuid4()), scope=["beta"])
    pack = {"relevant_memories": [{"id": "m"}], "open_loops": [_loop(source_id=hidden["id"]), "stray"], "trace": {"a": 1}}
    out = withhold_unreadable_references_from_pack(_Rows([hidden]), pack, fence=_FENCE)
    assert out is not pack
    assert out["relevant_memories"] is pack["relevant_memories"] and out["trace"] is pack["trace"]
    assert len(out["open_loops"]) == 1 and out["open_loops"][0]["source_id"] is None  # type: ignore[arg-type, index]
    assert pack["open_loops"][0]["source_id"] == hidden["id"]  # type: ignore[index]
    bare = {"trace": {"a": 1}}
    assert withhold_unreadable_references_from_pack(_Rows(), bare, fence=_FENCE) == bare


def test_the_one_loop_form_returns_the_checked_row() -> None:
    """``withhold_unreadable_references_from_loop`` is the one-row form the update surfaces call.

    Mutation: return the input row.
    """

    hidden = _source(str(uuid4()), scope=["beta"])
    out = withhold_unreadable_references_from_loop(_Rows([hidden]), _loop(source_id=hidden["id"]), fence=_FENCE)
    assert out["source_id"] is None


def test_the_fence_is_a_required_keyword_only_argument_of_every_function_with_no_default() -> None:
    """Like the functions of ``vnext_source_fence``: a caller that forgets the fence fails to run, and does not read
    "no fence".

    Mutation: give ``fence`` a default (``SourceReadFence.unfenced()`` is the one that would hurt) or make it
    positional.
    """

    for function in (
        withhold_unreadable_references,
        withhold_unreadable_references_from_loop,
        withhold_unreadable_references_from_pack,
    ):
        parameter = inspect.signature(function).parameters["fence"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, function
        assert parameter.default is inspect.Parameter.empty, function
        with pytest.raises(TypeError):
            function(_Rows(), [] if function is withhold_unreadable_references else {})  # type: ignore[call-arg]


def test_the_reference_keys_include_every_key_the_reverse_lookup_of_a_source_reads() -> None:
    """``list_open_loops_referencing_source`` finds the loops that name a source under six metadata keys. A loop found
    by a source id must lose that id by the same words, so the keys the output rule treats as references are a
    superset of the keys the lookup reads. Read from the shared SQL text of the SQLite store
    (``OPEN_LOOP_SOURCE_REFERENCE_SQL``, which the lookup, the delete preview and the scrub all use).

    Mutation: remove a key from ``SOURCE_REFERENCE_KEYS``, or add a seventh to the SQL.
    """

    from alicebot_api.vnext_open_loop_references import SOURCE_REFERENCE_KEYS
    from alicebot_api.vnext_stores.sqlite.open_loop_source_reference import OPEN_LOOP_SOURCE_REFERENCE_SQL

    # The rule the lookup, the delete preview and the scrub share lives in one statement (the lookup reads it from there).
    text = OPEN_LOOP_SOURCE_REFERENCE_SQL
    keys_in_sql = {
        part.strip().strip("'")
        for part in text[text.index("ref.key IN (") + len("ref.key IN (") : text.index(")", text.index("ref.key IN ("))].split(",")
    }
    assert keys_in_sql and keys_in_sql <= SOURCE_REFERENCE_KEYS, keys_in_sql


# -- 5. every reader of a loop, classified ----------------------------------------------------------------------

# The calls that hand a reader an open loop row, or a pack or dashboard that holds whole rows. ``create_open_loop`` is
# classified by ``test_every_open_loop_create_site_is_classified``.
_LOOP_READ_CALLS = (
    "list_open_loops",
    "get_open_loop",
    "update_open_loop",
    "update_open_loop_status",
    "list_open_loops_referencing_source",
    "review_open_loop",
    "extract_open_loops",
    "project_dashboard",
    "compile_context_pack",
)

_FENCED = "fenced: checks every reference against the caller's fence before it returns the row"
_PRODUCER = "producer: hands the row to its caller, and every caller of it is in this table"
_NOT_RETURNED = "not returned: reads the row to decide something and returns no part of it"
_ALLOWLIST = "allowlist: returns a fixed list of fields that holds no reference (pinned by the allowlist test)"
_OWNER = "owner: no agent identity reaches this door, so the reader is the owner and is not fenced"
_SENSITIVITY = (
    "sensitivity ceiling: a caller identity hides a row above that caller's sensitivity, "
    "and the domain and project fences stay as they were"
)
_OPERATOR = "operator: a local command or test harness, not a door a key-bound caller reaches"
_SHARED = (
    "shared: one handler of it returns a fixed list of fields and the other takes no identity "
    "(pinned by the allowlist test and the schema test)"
)

# (call, file, enclosing function) -> how it is held. A new reader of a loop fails the test below until it is read and
# classified here, and a reader that is called fenced must contain the call.
_LOOP_READERS: dict[tuple[str, str, str], str] = {
    ("compile_context_pack", "cli/context.py", "_run_context_pack"): _OPERATOR,
    ("compile_context_pack", "cli/smokes.py", "_run_vnext_smoke_agent_integration_pack"): _OPERATOR,
    ("compile_context_pack", "cli/smokes.py", "_run_vnext_smoke_agentic_memory_commit"): _OPERATOR,
    ("compile_context_pack", "cli/smokes.py", "_run_vnext_smoke_capture_to_brief"): _OPERATOR,
    ("compile_context_pack", "cli/smokes.py", "_run_vnext_smoke_operator_console"): _OPERATOR,
    ("compile_context_pack", "mcp/context.py", "_vnext_context_pack_payload"): _SHARED,
    ("compile_context_pack", "routers/vnext_retrieval.py", "create_vnext_context_pack"): _FENCED,
    ("compile_context_pack", "vnext_evals.py", "_retrieve"): _OPERATOR,
    ("extract_open_loops", "cli/automation.py", "_run_vnext_open_loops_extract"): _OPERATOR,
    ("extract_open_loops", "mcp/projects.py", "_handle_alice_open_loop_extract"): _OWNER,
    ("extract_open_loops", "routers/vnext_projects.py", "extract_vnext_open_loops"): _OWNER,
    ("get_open_loop", "mcp/retrieval.py", "_handle_alice_open_loops"): _FENCED,
    ("get_open_loop", "mcp/retrieval.py", "_resume_event_honours_policy_fence"): _NOT_RETURNED,
    ("get_open_loop", "routers/vnext_projects.py", "review_vnext_open_loop"): _FENCED,
    ("get_open_loop", "session_briefing.py", "_event_target_honours_fence"): _NOT_RETURNED,
    ("get_open_loop", "session_briefing.py", "_merge_recent_change_targets"): _ALLOWLIST,
    ("get_open_loop", "vnext_projects.py", "review_open_loop"): _NOT_RETURNED,
    ("list_open_loops", "compiler.py", "compile_and_persist_trace"): _OWNER,
    ("list_open_loops", "compiler.py", "compile_resumption_brief"): _OWNER,
    ("list_open_loops", "explicit_commitments.py", "_find_active_open_loop_for_memory"): _OWNER,
    ("list_open_loops", "mcp/projects.py", "_handle_alice_vnext_open_loops"): _FENCED,
    ("list_open_loops", "mcp/retrieval.py", "_vnext_resume"): _ALLOWLIST,
    ("list_open_loops", "memory.py", "list_open_loop_records"): _OWNER,
    ("list_open_loops", "routers/workspaces.py", "_vnext_workspace_payload"): _FENCED,
    ("list_open_loops", "session_briefing.py", "compile_session_brief"): _ALLOWLIST,
    ("list_open_loops", "vnext_context_tree.py", "build_tree"): _ALLOWLIST,
    ("list_open_loops", "vnext_dogfooding.py", "dashboard"): _OPERATOR,
    ("list_open_loops", "vnext_projects.py", "project_dashboard"): _FENCED,
    ("list_open_loops", "vnext_scheduler.py", "_generate_open_loop_review_artifact"): _FENCED,
    ("list_open_loops_referencing_source", "routers/_vnext_shared.py", "_vnext_load_source_trace"): _FENCED,
    ("project_dashboard", "cli/automation.py", "_run_vnext_project_dashboard"): _OPERATOR,
    ("project_dashboard", "mcp/projects.py", "_handle_alice_project_dashboard"): _OWNER,
    ("project_dashboard", "routers/vnext_projects.py", "get_vnext_project_dashboard"): _SENSITIVITY,
    ("project_dashboard", "routers/workspaces.py", "_vnext_workspace_payload"): _FENCED,
    ("review_open_loop", "cli/automation.py", "_run_vnext_open_loop_review"): _OPERATOR,
    ("review_open_loop", "mcp/projects.py", "_handle_alice_open_loop_review"): _OWNER,
    ("review_open_loop", "mcp/retrieval.py", "_handle_alice_open_loops"): _FENCED,
    ("review_open_loop", "routers/vnext_projects.py", "review_vnext_open_loop"): _FENCED,
    ("update_open_loop", "vnext_projects.py", "review_open_loop"): _PRODUCER,
    ("update_open_loop_status", "vnext_projects.py", "review_open_loop"): _PRODUCER,
}

_WITHHOLD_CALLS = {
    "withhold_unreadable_references",
    "withhold_unreadable_references_from_loop",
    "withhold_unreadable_references_from_pack",
}
_IDENTITY_NAMES = {
    "identity",
    "agent_identity",
    "_agent_identity_from_arguments",
    "_vnext_authenticated_agent_identity",
    "_vnext_agent_identity",
}


def _function_node(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((_SRC / path).read_text(encoding="utf-8"))
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def _withhold_calls(function: ast.FunctionDef) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Name) and node.func.id in _WITHHOLD_CALLS)
             or (isinstance(node.func, ast.Attribute) and node.func.attr in _WITHHOLD_CALLS))
    ]


def test_every_reader_of_an_open_loop_is_classified_and_a_fenced_one_contains_the_check() -> None:
    """The read-side twin of ``test_every_open_loop_create_site_is_classified``: a loop keeps ids of its own and every
    function that returns a row of ``open_loops`` (or a pack or a dashboard that holds whole rows) is listed here with
    how it is held. A new reader fails until it is read and classified. A function classified as fenced must call
    one of the three ``withhold_unreadable_references`` functions with ``fence=``, and one classified as the owner's
    must not read an agent identity (a door that learns to read one has to be reclassified, which is the moment to
    fence it).

    Mutations: add ``store.list_open_loops(...)`` to a function not listed, rename a listed function, delete the
    ``withhold_unreadable_references`` call from any fenced function (``_handle_alice_vnext_open_loops``,
    ``_handle_alice_open_loops``, ``review_vnext_open_loop``, ``create_vnext_context_pack``
    or the scheduler's report), drop its
    ``fence=``, or make an owner door read ``identity``.
    """

    found = {
        (call, file, function)
        for call in _LOOP_READ_CALLS
        for file, function in _function_sites(call)
        if not file.startswith("vnext_stores/")
    }
    assert found == set(_LOOP_READERS), {"new": sorted(found - set(_LOOP_READERS)), "gone": sorted(set(_LOOP_READERS) - found)}
    for (call, file, function), disposition in sorted(_LOOP_READERS.items()):
        node = _function_node(file, function)
        if disposition == _FENCED:
            calls = _withhold_calls(node)
            assert calls, (file, function, "no withhold call")
            for withhold in calls:
                assert [keyword.arg for keyword in withhold.keywords] == ["fence"], (file, function)
        if disposition == _OWNER:
            used = {
                sub.id if isinstance(sub, ast.Name) else sub.attr
                for sub in ast.walk(node)
                if isinstance(sub, (ast.Name, ast.Attribute))
            } & _IDENTITY_NAMES
            assert not used, (file, function, used)


def test_the_fenced_readers_build_their_fence_from_the_callers_identity() -> None:
    """A fenced reader must hand the function the fence of the identity it resolved for the call, not the owner's. Read
    from the syntax tree: every ``fence=`` of a call in the fenced functions is ``SourceReadFence.for_identity(...)``
    or a name assigned from it, and none is ``SourceReadFence.unfenced()``.

    Mutation: write ``fence=SourceReadFence.unfenced()`` at any of the call sites.
    """

    for (_call, file, function), disposition in sorted(_LOOP_READERS.items()):
        if disposition != _FENCED:
            continue
        node = _function_node(file, function)
        for withhold in _withhold_calls(node):
            value = ast.unparse(withhold.keywords[0].value)
            assert "unfenced" not in value, (file, function, value)
            assert value.startswith("SourceReadFence.for_identity(") or value == "read_fence", (file, function, value)
            if value == "read_fence":
                assigned = [
                    ast.unparse(sub.value)
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "read_fence" for target in sub.targets)
                ]
                assert assigned and all(item.startswith("SourceReadFence.for_identity(") for item in assigned), (file, function)


def test_the_create_route_returns_the_row_through_the_output_function_after_the_event_ids_are_read() -> None:
    """``create_vnext_open_loop`` stores the row, answers with the checked copy and keeps the event's target id from the
    stored row, so the answer and the audit row cannot disagree about which loop it was.

    Mutation: answer ``created`` in place of the checked copy.
    """

    node = _function_node("routers/vnext_projects.py", "create_vnext_open_loop")
    calls = _withhold_calls(node)
    assert len(calls) == 1 and ast.unparse(calls[0].keywords[0].value) == "read_fence"
    answer = next(sub for sub in ast.walk(node) if isinstance(sub, ast.Call) and ast.unparse(sub.func) == "JSONResponse")
    assert "payload" in ast.unparse(answer) and "created" not in ast.unparse(answer)


def test_the_projections_of_a_loop_hold_no_reference_and_drop_them() -> None:
    """The readers classified as an allowlist return a fixed projection of each loop: the compact fields of the context
    pack tool, the compact row of ``alice_resume`` (and the recent-change rows it adds), the rendered line of the session
    brief and the node of the context tree. None of them can carry an id of a source or memory, so those readers are
    clean without the fence, and a field added to one is caught here. Each projection is called on a row full of
    references and must not return any of them.

    Mutations: add ``source_id``, ``memory_id`` or ``metadata_json`` to ``_COMPACT_OPEN_LOOP_FIELDS``, to the dict of
    ``_compact_vnext_open_loop`` or to the metadata keys of ``_row_node``, or make ``_loop_text`` return the whole row.
    """

    from alicebot_api.mcp.context import _COMPACT_OPEN_LOOP_FIELDS
    from alicebot_api.mcp.retrieval_shared import _compact_vnext_open_loop
    from alicebot_api.session_briefing import _loop_text, _render_brief
    from alicebot_api.vnext_context_tree import _row_node

    forbidden = {"source_id", "memory_id", "metadata_json", "source_refs", "person_id", "user_id"}
    assert not forbidden & set(_COMPACT_OPEN_LOOP_FIELDS)
    source_id, memory_id, person_id = str(uuid4()), str(uuid4()), str(uuid4())
    row = {
        "id": str(uuid4()), "title": "Kiln followup", "description": "Call the supplier.", "status": "open",
        "priority": "normal", "due_at": None, "opened_at": None, "domain": "project", "project_id": None,
        "source_id": source_id, "memory_id": memory_id, "metadata_json": {"source_id": source_id, "source_refs": [source_id]},
        "person_id": person_id, "user_id": _USER_ID,
    }
    seen = {
        "compact row": _compact_vnext_open_loop(row),
        "brief line": _loop_text(row),
        "brief": _render_brief(facts=[], open_loops=[row], sources=[], pack_view=None),
        "tree node": _row_node("open_loop", row, label_keys=("title", "description"), fallback="Open loop"),
    }
    for name, projection in seen.items():
        text = json.dumps(projection)
        for value in (source_id, memory_id, person_id):
            assert value not in text, (name, value)
    assert not forbidden & set(seen["compact row"])  # type: ignore[arg-type]
    assert "Kiln followup" in json.dumps(seen["brief"]), "the loop is in the brief, so the scan is not vacuous"


# -- 6. the scheduler's report, the legacy tools, and the words ---------------------------------------------


def test_the_scheduler_open_loop_report_copies_only_the_sources_its_identity_may_read() -> None:
    """The ``open_loop_review`` workflow writes each loop's source id into the text of its report and into the
    ``source_refs`` of the artifact it stores, and a later reader of the artifact is shown both. The run is held to the
    read fence of the identity it runs as: a ``trusted_local_agent`` run leaves out the confidential source and keeps
    the own-project one, an ``admin_agent`` run and a run with no identity (the owner's) keep both. The loop itself is
    in the report either way, with ``Source: not linked`` where the reference was withheld.

    The Postgres scheduler runs this same method over the same two lookups, so this stub is the unit-level check; the
    live one is in ``tests/integration/test_open_loop_references_postgres.py``.

    Mutations: drop the ``withhold_unreadable_references`` call in ``_generate_open_loop_review_artifact``, or give it
    ``SourceReadFence.unfenced()``.
    """

    from tests.unit.test_vnext_scheduler import InMemorySchedulerStore
    from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService

    own, confidential = str(uuid4()), str(uuid4())

    class _Store(InMemorySchedulerStore):
        def get_sources_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
            rows = {
                own: _source(own, scope=["alpha"], sensitivity="private"),
                confidential: _source(confidential, scope=["alpha"], sensitivity="confidential"),
            }
            return [rows[i] for i in ids if i in rows]

        def get_memories_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
            return []

        def read_label_rows(self, kind: str, ids: list[str]) -> list[dict[str, object]]:
            if kind == "source":
                return self.get_sources_by_ids(ids)
            if kind == "open_loop":
                return [dict(row) for row in self.open_loops if str(row.get("id")) in ids]
            return []

    def run(identity) -> dict[str, object]:  # type: ignore[no-untyped-def]
        store = _Store()
        store.open_loops = [
            {"id": f"loop-{kind}", "title": f"Kiln followup {kind}", "status": "open", "domain": "project",
             "sensitivity": "private", "project_id": "alpha", "source_id": source_id, "memory_id": None,
             "metadata_json": {"project_scope": ["alpha"]}}
            for kind, source_id in (("own", own), ("confidential", confidential))
        ]
        result = VNextSchedulerService(store).run_now(
            SchedulerRunRequest(
                workflow_type="open_loop_review", domains=("project",), projects=("alpha",), agent_identity=identity
            )
        )
        return result["artifact"]  # type: ignore[return-value]

    for identity, sees_confidential in (
        (_bound_identity("trusted_local_agent", "alpha"), False),
        (_bound_identity("admin_agent", "alpha"), True),
        (None, True),
    ):
        artifact = run(identity)
        text = json.dumps(artifact)
        assert artifact["metadata_json"]["open_loop_ids"] == ["loop-own", "loop-confidential"]  # type: ignore[index]
        assert own in text
        assert (confidential in text) is sees_confidential, identity
        refs = artifact["metadata_json"]["source_refs"]  # type: ignore[index]
        assert refs == ([f"source:{own}", f"source:{confidential}"] if sees_confidential else [f"source:{own}"])
        assert "Kiln followup confidential" in text
        assert ("- Source: not linked" in str(artifact["content_markdown"])) is (not sees_confidential)


def test_the_legacy_tools_that_return_whole_loops_take_no_identity_so_they_are_the_owners() -> None:
    """``alice_project_dashboard``, ``alice_vnext_context_pack``, ``alice_open_loop_extract`` and
    ``alice_open_loop_review`` return loops whole and advertise no identity argument, and they are served only with no
    agent key, so every call to them is the owner's and there is no reader to fence. That is what classifies them as
    the owner's in the table above. A tool that gains an identity property fails here, and has to be fenced.

    Mutation: add an ``agent_id`` property to the schema of any of the four.
    """

    from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS

    identity_fields = {"agent_id", "agent_identity", "agent_type", "agent_run_id", "task_id", "permission_profile"}
    tools = {str(tool["name"]): tool for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS)}
    for name in ("alice_project_dashboard", "alice_vnext_context_pack", "alice_open_loop_extract", "alice_open_loop_review"):
        properties = set(tools[name]["inputSchema"].get("properties", {}))  # type: ignore[attr-defined]
        assert not properties & identity_fields, (name, properties & identity_fields)
        assert name in {str(tool["name"]) for tool in _LEGACY_TOOL_DEFINITIONS}, name


def test_the_docs_say_what_the_readers_do_and_name_the_two_limits_that_remain() -> None:
    """The limitation of PR 534 named ``alice_open_loops`` beside the context pack's ``supporting_evidence`` and
    ``alice_memory_review`` by id. This change fixes the first and the saved-quotes change fixes the other two, so
    none of the three places that carried the sentence may still state it as a limit, and the open-loop fix is stated
    once where a user reads the tool contract and once in the release notes.

    Mutations, each one alone: put the old sentence back in any one of the CHANGELOG, ``docs/alpha/known-limitations.md``
    or ``docs/alpha/mcp-tools.md``, or delete the new paragraph of ``mcp-tools.md``; delete ``ask the reader's own
    fence again`` from the limitations page; change ``the value is `null` `` to ``the value is an empty string`` in
    ``mcp-tools.md``.
    """

    def squashed(path: str) -> str:
        return " ".join((_ROOT / path).read_text(encoding="utf-8").split())

    changelog = squashed("CHANGELOG.md")
    limitations = squashed("docs/alpha/known-limitations.md")
    tools = squashed("docs/alpha/mcp-tools.md")
    old_clauses = (
        "and, for an open loop, in `alice_open_loops`",
        "and `alice_open_loops` still returns a foreign id",
        "in `alice_memory_review` by id and in `alice_open_loops`",
        "`supporting_evidence` and in `alice_memory_review` by id",
    )
    for name, text in (("CHANGELOG.md", changelog), ("known-limitations.md", limitations), ("mcp-tools.md", tools)):
        for clause in old_clauses:
            assert clause not in text, (name, clause)
    assert changelog.count("- An open loop no longer shows its reader the id of a source or memory the reader may not read.") == 1
    assert (
        tools.count(
            "Unreleased (on main, not in v0.20.0): the readers of an open loop hold the loop's references to the "
            "reader's own fence"
        )
        == 1
    )
    # The limitations page states the change in one clause and links the sections that hold the detail. The clause is
    # pinned here; the rule that a reader gets `null` for a reference it may not read is pinned on ``mcp-tools.md``,
    # which holds it (it used to be pinned on the page as well).
    assert limitations.count("the readers of a saved quote and of an open loop ask the reader's own fence again") == 1
    assert tools.count("the key stays and the value is `null`, so the cases read alike") == 1
