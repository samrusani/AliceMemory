"""Live PostgreSQL checks of the read-side rule for open loop references.

Unreleased (on main, not in v0.20.0). The unit tests run the rule over a real SQLite vault and over stubs of the
Postgres store. Three things only a live database shows:

* the two bulk lookups the rule makes (``get_sources_by_ids`` and ``get_memories_by_ids``, each ``id = ANY(%s::uuid[])``)
  parse and return the rows of the loops' own columns and of the ids found inside ``metadata_json``, which the driver
  hands back as ``UUID`` objects and as ``jsonb``, and, with ``include_deleted``, the soft-deleted, archived and
  redacted rows too, so that an id of a deleted row is withheld under any key (the last test of the file);
* the HTTP review route and the HTTP context pack, which are Postgres only, withhold what the key bound to a project may
  not read and give the ``admin_agent`` key the reference it may read;
* the scheduler's ``open_loop_review`` report (Postgres only) copies into its text and its ``source_refs`` only the
  sources the identity it ran as may read.

Each test names the mutation that must fail it. A mutation is made in a scratch edit of the source file, the test is
seen to fail, and the file is restored by copying the saved file back.
"""

from __future__ import annotations

import json
from io import BytesIO
from uuid import UUID, uuid4

import alicebot_api.main as main_module
from alicebot_api import mcp_server
from alicebot_api.config import Settings
from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.routers import vnext_projects as vnext_projects_router
from alicebot_api.routers import vnext_retrieval as vnext_retrieval_router
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_open_loop_references import withhold_unreadable_references
from alicebot_api.vnext_scheduler import SchedulerRunRequest
from alicebot_api.vnext_scheduler_runtime import run_now_durable
from alicebot_api.vnext_source_fence import SourceReadFence
from alicebot_api.vnext_store import PostgresVNextStore

from tests.integration.test_vnext_live_workspace_api import invoke_request, seed_user

_PROJECT = "alicebot"
_OTHER_PROJECT = "other-project"


def _source(store: PostgresVNextStore, name: str, *, sensitivity: str = "internal", project: str | None = _PROJECT) -> str:
    row = store.create_source(
        {
            "source_type": "note",
            "title": f"Note {name}",
            "content_hash": f"sha256:{name}-{uuid4()}",
            "captured_at": "2026-08-01T08:00:00Z",
            "domain": "project",
            "sensitivity": sensitivity,
            "metadata_json": {"project_scope": [project]} if project is not None else {},
        }
    )
    return str(row["id"])


def _memory(store: PostgresVNextStore, name: str, *, sensitivity: str = "internal", project: str = _PROJECT) -> str:
    text = f"A fact {name} held for the open loop test {uuid4().hex}."
    row = store.create_memory(
        {
            "memory_key": f"memory.{uuid4()}",
            "value": {"text": text},
            "status": "active",
            "memory_type": "semantic",
            "title": f"Fact {name}",
            "canonical_text": text,
            "summary": "A fact.",
            "domain": "project",
            "sensitivity": sensitivity,
            "confirmation_status": "confirmed",
            "project_scope": [project],
            "project_id": project,
        }
    )
    return str(row["id"])


def _loop(
    store: PostgresVNextStore,
    name: str,
    *,
    source_id: str | None = None,
    memory_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    row = store.create_open_loop(
        {
            "title": f"Kiln followup {name}",
            "domain": "project",
            "sensitivity": "internal",
            "source_id": source_id,
            "memory_id": memory_id,
            "metadata_json": {"project_scope": [_PROJECT], **(metadata or {})},
        },
        actor_type="user",
    )
    return str(row["id"])


class _World:
    """Sources, memories and loops of a project, and a key of each kind bound to it."""

    def __init__(self, app_url: str, user_id: UUID) -> None:
        self.app_url = app_url
        self.user_id = user_id
        with user_connection(app_url, user_id) as conn:
            store = PostgresVNextStore(conn)
            self.own = _source(store, "own")
            self.confidential = _source(store, "confidential", sensitivity="confidential")
            self.other = _source(store, "other", project=_OTHER_PROJECT)
            self.shared = _source(store, "global", project=None)
            self.own_memory = _memory(store, "own")
            self.confidential_memory = _memory(store, "confidential", sensitivity="confidential")
            self.other_memory = _memory(store, "other", project=_OTHER_PROJECT)
            self.loops = {
                "own": _loop(store, "own", source_id=self.own, memory_id=self.own_memory),
                "confidential": _loop(
                    store, "confidential", source_id=self.confidential, memory_id=self.confidential_memory
                ),
                "other": _loop(store, "other", source_id=self.other, memory_id=self.other_memory),
                "global": _loop(store, "global", source_id=self.shared),
                "metadata": _loop(
                    store,
                    "metadata",
                    metadata={"source_id": self.confidential, "source_refs": [self.other, self.own], "label": "x"},
                ),
            }
            self.keys = {}
            for who, profile in (("admin", "admin_agent"), ("scoped", "project_scoped_agent")):
                _record, raw = create_agent_key(
                    store, user_id=user_id, agent_id=f"loop-{who}", permission_profile=profile, project_scope=_PROJECT
                )
                self.keys[who] = raw

    def expected_columns(self, who: str) -> dict[str, tuple[str | None, str | None]]:
        """What each loop must show ``who``: the project scoped key reads the own rows only, the admin key reads the
        confidential ones too, and nobody bound to a project reads the other project's or a global row."""

        admin = who == "admin"
        return {
            "own": (self.own, self.own_memory),
            "confidential": (self.confidential, self.confidential_memory) if admin else (None, None),
            "other": (None, None),
            "global": (None, None),
            "metadata": (None, None),
        }

    def expected_metadata(self, who: str) -> dict[str, object]:
        expected: dict[str, object] = {"project_scope": [_PROJECT], "source_refs": [self.own], "label": "x"}
        if who == "admin":
            expected["source_id"] = self.confidential
        return expected

    def check_item(self, who: str, item: dict[str, object], *, where: str) -> None:
        name = next(name for name, loop_id in self.loops.items() if loop_id == str(item["id"]))
        source, memory = self.expected_columns(who)[name]
        got_source = None if item["source_id"] is None else str(item["source_id"])
        got_memory = None if item["memory_id"] is None else str(item["memory_id"])
        assert (got_source, got_memory) == (source, memory), (where, who, name)
        if name == "metadata":
            assert item["metadata_json"] == self.expected_metadata(who), (where, who)

    def check_no_leak(self, who: str, payload: object, *, where: str) -> None:
        text = json.dumps(payload, default=str)
        refused = {self.other, self.shared, self.other_memory}
        if who != "admin":
            refused |= {self.confidential, self.confidential_memory}
        assert not [value for value in refused if value in text], (where, who)
        assert self.own in text, (where, who, "the own source is missing: the scan would be vacuous")


def _wire(context: MCPRuntimeContext, name: str, arguments: dict[str, object]) -> tuple[bool, dict[str, object]]:
    server = mcp_server.MCPServer(context=context, input_stream=BytesIO(), output_stream=BytesIO())
    response = server._handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    assert response is not None
    result = response["result"]
    return bool(result["isError"]), json.loads(result["content"][0]["text"])


def _bind_http(monkeypatch, app_url: str) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_url=app_url)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_projects_router, "get_settings", lambda: settings)
    monkeypatch.setattr(vnext_retrieval_router, "get_settings", lambda: settings)


def test_the_function_reads_the_real_store_rows_and_withholds_what_the_fence_refuses(migrated_database_urls) -> None:  # type: ignore[no-untyped-def]
    """The rule called directly on rows of the real Postgres store, with the fence of each reader. The columns come back
    as ``UUID`` objects and the metadata as ``jsonb``, the lookups are the real ``ANY(uuid[])`` statements, and an id
    the database does not hold (an id inside ``metadata_json`` under a reference key) is withheld like a refused one.

    Mutations: drop the ``str(...)`` around ``row.get("id")`` in ``_rows_by_id`` or around the column value in
    ``_canonical_id`` (the own reference is then withheld for every reader), or make ``fence.admits`` return True (the
    project scoped reader gets the confidential and the other project's id).
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email=f"open-loop-references-store-{uuid4().hex[:8]}@example.com")
    world = _World(app_url, user_id)
    unknown = str(uuid4())
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)
        loops = [dict(store.get_open_loop(loop_id)) for loop_id in world.loops.values()]  # type: ignore[arg-type]
        loops[-1]["metadata_json"] = {**loops[-1]["metadata_json"], "selected_source_ids": [unknown, world.own]}  # type: ignore[dict-item]
        for who, profile in (("scoped", "project_scoped_agent"), ("admin", "admin_agent")):
            identity = AgentIdentity(
                agent_id=f"loop-{who}",
                permission_profile=profile,
                project_scope=(_PROJECT,),
                auth="agent_api_key",
                project_scope_locked=True,
            )
            out = withhold_unreadable_references(store, loops, fence=SourceReadFence.for_identity(identity))
            by_id = {str(row["id"]): row for row in out}
            for name, loop_id in world.loops.items():
                row = by_id[loop_id]
                source, memory = world.expected_columns(who)[name]
                assert (None if row["source_id"] is None else str(row["source_id"])) == source, (who, name)
                assert (None if row["memory_id"] is None else str(row["memory_id"])) == memory, (who, name)
            assert by_id[world.loops["metadata"]]["metadata_json"] == {
                **world.expected_metadata(who),
                "selected_source_ids": [world.own],
            }
        owner = withhold_unreadable_references(store, loops, fence=SourceReadFence.unfenced())
        by_id = {str(row["id"]): row for row in owner}
        assert str(by_id[world.loops["other"]]["source_id"]) == world.other
        assert str(by_id[world.loops["global"]]["source_id"]) == world.shared


def test_the_mcp_list_and_the_http_routes_withhold_the_reference_on_postgres(
    migrated_database_urls, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """``alice_open_loops`` (list and ``edit``), ``POST /v0/vnext/open-loops/{id}/review`` and
    ``POST /v0/vnext/context-packs`` over the real store with real keys bound to a project. The project scoped key is
    shown the own-project references and ``null`` for the rest, the ``admin_agent`` key is also shown the confidential
    source and memory, and no refused id is anywhere in what either of them reads.

    Mutations: drop the ``withhold_unreadable_references`` call in ``_handle_alice_vnext_open_loops``, the
    ``withhold_unreadable_references_from_loop`` call in ``_handle_alice_open_loops`` or in ``review_vnext_open_loop``,
    or the ``withhold_unreadable_references_from_pack`` call in ``create_vnext_context_pack``.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email=f"open-loop-references-routes-{uuid4().hex[:8]}@example.com")
    world = _World(app_url, user_id)
    _bind_http(monkeypatch, app_url)
    monkeypatch.setenv(MCP_FULL_TOOLS_ENV, "1")
    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)

    for who in ("scoped", "admin"):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", world.keys[who])
        is_error, listed = _wire(context, "alice_open_loops", {"status": "all", "limit": 100})
        assert is_error is False, listed
        items = listed["items"]
        assert sorted(str(item["id"]) for item in items) == sorted(world.loops.values()), who  # type: ignore[index, union-attr]
        for item in items:  # type: ignore[attr-defined]
            world.check_item(who, item, where="list")
        world.check_no_leak(who, items, where="list")

        for name in ("own", "confidential", "other", "metadata"):
            is_error, updated = _wire(
                context,
                "alice_open_loops",
                {"action": "edit", "loop_id": world.loops[name], "description": f"Looked at by {who}."},
            )
            assert is_error is False, (who, name, updated)
            world.check_item(who, updated["open_loop"], where="update")  # type: ignore[arg-type, index]
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)

    for who in ("scoped", "admin"):
        authorization = f"Bearer {world.keys[who]}"
        for name in ("own", "confidential", "other", "metadata"):
            status, body = invoke_request(
                "POST",
                f"/v0/vnext/open-loops/{world.loops[name]}/review",
                payload={
                    "user_id": str(user_id),
                    "agent_id": f"loop-{who}",
                    "action": "edit",
                    "description": f"Looked at over HTTP by {who}.",
                },
                authorization=authorization,
            )
            assert status == 200, (who, name, body)
            world.check_item(who, body, where="review route")
        status, pack = invoke_request(
            "POST",
            "/v0/vnext/context-packs",
            payload={"user_id": str(user_id), "agent_id": f"loop-{who}", "query": "followup"},
            authorization=authorization,
        )
        assert status == 201, pack
        assert pack["open_loops"], who
        for item in pack["open_loops"]:
            world.check_item(who, item, where="pack")
        world.check_no_leak(who, {key: value for key, value in pack.items() if key != "recent_changes"}, where="pack")


def test_the_scheduler_open_loop_report_copies_only_the_sources_its_identity_may_read_on_postgres(
    migrated_database_urls,
) -> None:  # type: ignore[no-untyped-def]
    """The ``open_loop_review`` workflow on the real store. A run as a ``project_scoped_agent`` bound to the project
    leaves the confidential source out of the report text and out of the artifact's ``source_refs`` and keeps the own
    one, a run as the ``admin_agent`` keeps both, and a run with no identity (the owner's) keeps both. The loops are
    all in the report.

    Mutation: drop the ``withhold_unreadable_references`` call in ``_generate_open_loop_review_artifact``, or give it
    ``SourceReadFence.unfenced()``.
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email=f"open-loop-references-scheduler-{uuid4().hex[:8]}@example.com")
    world = _World(app_url, user_id)

    def run(identity: AgentIdentity | None) -> dict[str, object]:
        result = run_now_durable(
            database_url=app_url,
            user_id=user_id,
            request=SchedulerRunRequest(
                workflow_type="open_loop_review",
                domains=("project",),
                projects=(_PROJECT,),
                generated_for="2026-08-01",
                triggered_by="agent" if identity is not None else "user",
                agent_identity=identity,
            ),
        )
        assert result["run"]["status"] == "succeeded", result  # type: ignore[index]
        return result["artifact"]  # type: ignore[return-value]

    def identity(who: str, profile: str) -> AgentIdentity:
        return AgentIdentity(
            agent_id=f"loop-{who}",
            permission_profile=profile,
            project_scope=(_PROJECT,),
            auth="agent_api_key",
            project_scope_locked=True,
        )

    for who, caller, sees_confidential in (
        ("scoped", identity("scoped", "project_scoped_agent"), False),
        ("admin", identity("admin", "admin_agent"), True),
        ("owner", None, True),
    ):
        artifact = run(caller)
        text = json.dumps(artifact, default=str)
        assert world.own in text, who
        assert (world.confidential in text) is sees_confidential, who
        # A key bound to a project is shown no source of another project and no global one. The owner is shown both.
        assert (world.other in text and world.shared in text) is (caller is None), who
        assert (world.other in text or world.shared in text) is (caller is None), who
        assert sorted(artifact["metadata_json"]["open_loop_ids"]) == sorted(world.loops.values()), who  # type: ignore[index]
        refs = sorted(artifact["metadata_json"]["source_refs"])  # type: ignore[index]
        assert f"source:{world.own}" in refs, who
        assert (f"source:{world.confidential}" in refs) is sees_confidential, who


def test_a_deleted_row_is_refused_and_an_id_is_withheld_in_every_spelling_on_postgres(
    migrated_database_urls,
) -> None:  # type: ignore[no-untyped-def]
    """The two reads with ``include_deleted`` on the real store, and the two cases an outside review reproduced.

    ``get_sources_by_ids`` and ``get_memories_by_ids`` leave a soft-deleted row out by default and return it, with
    ``deleted_at`` set, when asked: a deleted source, an archived memory and a redacted memory. The rule then, for a
    ``project_scoped_agent`` key bound to the project: (1) a confidential source's id written without hyphens inside a URL
    and a sentence is withheld like the hyphenated one; (2) a loop that links a confidential source in its column and
    repeats the id under ``evidence.quote_from`` shows it neither before nor after the source is deleted; (3) the ids of a
    deleted source, an archived memory and a redacted memory under keys that name no reference, which nothing in the
    response links, are withheld; (4) a readable source's id under the same keys is shown as stored.

    Mutations: make either bulk read ignore ``include_deleted`` and always filter ``deleted_at IS NULL`` (the first
    assertions fail, and so does case 3), drop the ``include_deleted`` argument from the call in ``_rows_by_id`` (case 3
    fails), or scan text for the hyphenated spelling only (case 1 fails).
    """

    app_url = migrated_database_urls["app"]
    user_id = seed_user(app_url, email=f"open-loop-references-deleted-{uuid4().hex[:8]}@example.com")
    world = _World(app_url, user_id)
    scoped = AgentIdentity(
        agent_id="loop-scoped",
        permission_profile="project_scoped_agent",
        project_scope=(_PROJECT,),
        auth="agent_api_key",
        project_scope_locked=True,
    )
    fence = SourceReadFence.for_identity(scoped)
    cut = "(id withheld)"
    with user_connection(app_url, user_id) as conn:
        store = PostgresVNextStore(conn)

        def read(loop_id: str) -> dict[str, object]:
            return withhold_unreadable_references(store, [dict(store.get_open_loop(loop_id))], fence=fence)[0]  # type: ignore[arg-type]

        gone = _source(store, "gone")
        archived_memory = _memory(store, "archived")
        redacted_memory = _memory(store, "redacted")
        store.delete_source(source_id=gone)
        store.update_memory(memory_id=archived_memory, patch={"status": "archived"})
        store.redact_memory_content(memory_id=redacted_memory)

        # The reads: live rows by default, the deleted ones when asked.
        assert [str(row["id"]) for row in store.get_sources_by_ids([gone, world.own])] == [world.own]
        found = {str(row["id"]): row for row in store.get_sources_by_ids([gone, world.own], include_deleted=True)}
        assert set(found) == {gone, world.own}
        assert found[gone]["deleted_at"] is not None and found[world.own]["deleted_at"] is None
        asked = [archived_memory, redacted_memory, world.own_memory]
        assert [str(row["id"]) for row in store.get_memories_by_ids(asked)] == [world.own_memory]
        found_memories = {str(row["id"]): row for row in store.get_memories_by_ids(asked, include_deleted=True)}
        assert set(found_memories) == set(asked)
        assert found_memories[archived_memory]["deleted_at"] is not None
        assert found_memories[redacted_memory]["deleted_at"] is not None
        assert found_memories[world.own_memory]["deleted_at"] is None

        # Case 1: the compact spelling inside a URL and a sentence.
        confidential = _source(store, "compact-confidential", sensitivity="confidential")
        compact = UUID(confidential).hex
        first = read(
            _loop(
                store,
                "compact",
                source_id=confidential,
                metadata={
                    "source_refs": [f"https://example.test/source/{confidential}", f"https://example.test/source/{compact}"],
                    "note": f"derived from {compact} on import",
                },
            )
        )
        assert first["source_id"] is None
        assert first["metadata_json"] == {
            "project_scope": [_PROJECT],
            "source_refs": [f"https://example.test/source/{cut}", f"https://example.test/source/{cut}"],
            "note": f"derived from {cut} on import",
        }

        # Case 2: the id repeated under another key stays withheld after the source is deleted.
        probe = _source(store, "probe-confidential", sensitivity="confidential")
        probe_loop = _loop(
            store,
            "delete-probe",
            source_id=probe,
            metadata={"source_id": probe, "evidence": {"quote_from": probe}},
        )
        before = read(probe_loop)
        store.delete_source(source_id=probe)
        after = read(probe_loop)
        for row in (before, after):
            assert row["source_id"] is None
            assert probe not in json.dumps(row, default=str) and UUID(probe).hex not in json.dumps(row, default=str)
            assert row["metadata_json"] == {"project_scope": [_PROJECT], "evidence": {}}

        # Case 3 and 4: deleted rows nothing links, and a readable one.
        unlinked = read(
            _loop(
                store,
                "unlinked",
                metadata={
                    "evidence": {
                        "quote_from": gone,
                        "note": f"see {UUID(gone).hex}",
                        "readable": world.own,
                        "readable_text": f"own {UUID(world.own).hex}",
                    },
                    "related": [archived_memory, UUID(redacted_memory).hex.upper()],
                },
            )
        )
        assert unlinked["metadata_json"] == {
            "project_scope": [_PROJECT],
            "evidence": {
                "note": f"see {cut}",
                "readable": world.own,
                "readable_text": f"own {UUID(world.own).hex}",
            },
            "related": [],
        }
