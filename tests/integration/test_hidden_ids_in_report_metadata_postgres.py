"""Where a readable row lists the id of a row the same key cannot read, and what stands next to the id.

Every producer runs through the mounted application with an admin key over a vault of readable rows. Then rows are made
unreadable after the reports exist, the only way a readable report can name an unreadable row (a report is stored at
least as strict as its inputs, so an input made confidential makes the report confidential too):

* the weekly candidate, which the report names in ``candidate_memory_ids`` and which is not one of its inputs, is made
  confidential;
* a source a report was made from is archived;
* a memory a report was made from is forgotten (explain and review still show its history, so it is not hidden, and it
  is the control for that);
* a memory a report was made from is redacted, and another is archived. A deleted memory is read by no door but redact,
  and a report keeps listing it.

A trusted key, a read-only key, a project-scoped key and a project-bound admin key then read through every door each can
reach that returns stored metadata. The tests pin these things:

1. The hidden ids appear only under the metadata fields that list ids. A new field, or a new door that returns the id as
   the row's own identity, fails here and has to be added to the disclosure.
2. Each id in those fields is a bare UUID, or a ``source:`` or ``memory:`` reference to one. There is no slug, path or
   name in an id.
3. A row that is hidden by its label is not returned as a row by any door: its id never stands as the ``id`` of an object.
4. The disclosure's own claim: the key can see an id, and the door for that id answers it as it answers an id that does not exist,
   and a call on the id of a deleted memory leaves the key's telemetry as a call on a missing id does.
5. A report that depends on a row stops being readable when the row is made confidential, so the cases above are the only ones.
6. The ids are not the only thing a report keeps. It keeps the words and values it was made with, so the text of a source
   that was archived and of a memory that was redacted afterwards stays in the reports that quoted it. The security note
   says so, and this file pins what is kept so that the note and the behaviour change together.
"""
from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.hidden_ids_postgres_support import ALL_DOORS, Env, changes, moved, snapshot

UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
BARE_ID = re.compile(r"^(source:|memory:|belief:|open_loop:|artifact:)?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PRODUCERS = (
    "daily", "weekly", "connections", "contradictions", "open_loop_review", "project_update", "consolidation", "staleness",
)
# The last key of a path where a hidden id may stand. Each is a field a producer writes to list the rows it used or made,
# the rendered report, or the event that recorded the run.
LISTING_KEYS = frozenset(
    {
        "candidate_memory_ids", "candidate_open_loop_ids", "candidate_edge_ids", "candidate_memory_id", "sources",
        "memories", "artifacts", "open_loops", "beliefs", "source_ids", "memory_ids", "open_loop_ids", "artifact_ids",
        "belief_ids", "source_refs", "stale_marked_memory_ids", "provenance", "source_item", "connected_item",
        "belief_memory_id", "content_markdown", "member_ids", "cluster_member_ids",
    }
)
# Paths (by their tail) where the id of a deleted memory also stands, next to what the report copied from it: the roll-up
# card lists its instances with the text, label and amounts of each memory it rolled up, and a consolidation candidate
# lists its members with a status, an update time and a digest. The last segment alone (``id``, ``memory_id``) is too
# common to allow on its own.
LISTING_PATHS = (".consolidation.member_snapshots.id", ".rollup.instances.memory_id")


def _paths(value, path=""):
    """Every (path, id, text) where a string holds a UUID. A list adds no segment, so the last segment is the field."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child, path)
    elif isinstance(value, str):
        for found in UUID_TEXT.findall(value):
            yield path, found.lower(), value


def _generate(h, producer, alpha, key):
    options = {
        "generated_for": "2026-10-05", "source_limit": 50, "memory_limit": 50, "artifact_limit": 50,
        "open_loop_limit": 50, "reference_time": "2026-10-05T12:00:00Z", "max_items": 50,
        "discover_open_loops": True, "create_candidate_memories": True,
    }
    payload = {"scope": {"projects": [alpha]}, "options": options}
    if producer in {"daily", "weekly", "connections", "contradictions"}:
        route = {"daily": "daily-brief", "weekly": "weekly-synthesis"}.get(producer, producer)
        path = "/v0/vnext/artifacts/generate/" + route
    elif producer == "project_update":
        path = "/v0/vnext/projects/update-candidates"
        payload["scope"]["project_id"] = alpha
    else:
        workflow = {"consolidation": "memory_consolidation", "staleness": "staleness_sweep"}.get(producer, producer)
        path = f"/v0/vnext/scheduler/workflows/{workflow}/run-now"
    status, body, _ = h.request("POST", path, payload=payload, key=key)
    assert status == 201, (producer, status, body)
    return body.get("artifact", body)


class World:
    def __init__(self, h) -> None:
        self.h = h
        self.alpha = str(uuid4())
        with h.store() as store:
            store.create_project({"id": self.alpha, "name": "Atlas", "slug": "atlas", "domain": "project", "sensitivity": "public"})
            self.sources = [
                store.create_source(
                    {
                        "source_type": "manual_text", "title": f"SOURCE{i} Atlas", "content_hash": str(uuid4()),
                        "captured_at": "2026-10-05T09:00:00Z", "domain": "project", "sensitivity": "public",
                        "metadata_json": {
                            "project_scope": [self.alpha],
                            "raw_text": f"Atlas does not prefer launch games. SOURCE{i} text.\nTODO: Atlas launch review SOURCE{i}.",
                        },
                    }
                )
                for i in range(2)
            ]
            self.memories = []
            for i, game in enumerate(("Hollow Knight", "Stardew Valley", "Celeste", "Hades", "Tunic")):
                text = f"Atlas played {game} for {25 + i * 30} hours. MEMORY{i}"
                self.memories.append(
                    store.create_memory(
                        {
                            "memory_key": f"alpha.game.{i}", "memory_type": "episode", "title": text, "canonical_text": text,
                            "summary": text, "value": {"text": text}, "status": "active", "domain": "project",
                            "sensitivity": "public", "created_at": "2026-10-05T09:00:00Z",
                            "metadata_json": {"project_scope": [self.alpha], "session_date": f"2026-10-0{i + 1}"},
                        }
                    )
                )
            backing = store.create_memory(
                {
                    "memory_key": "alpha.belief", "memory_type": "belief", "title": "BELIEF title",
                    "canonical_text": "Atlas prefers launch games. BELIEF text", "status": "active", "domain": "project",
                    "sensitivity": "public", "metadata_json": {"project_scope": [self.alpha]},
                }
            )
            store.create_belief({"memory_id": str(backing["id"]), "claim": backing["canonical_text"], "confidence": 0.9})
            for i in range(2):
                store.create_open_loop(
                    {
                        "title": f"LOOP{i} Atlas", "description": f"loop text {i}", "status": "open", "domain": "project",
                        "sensitivity": "public", "due_at": "2026-10-04T12:00:00Z", "opened_at": "2026-10-05T09:00:00Z",
                        "metadata_json": {"project_scope": [self.alpha]},
                    }
                )
            store.conn.execute(
                "UPDATE memories SET created_at='2026-10-05T09:00:00Z', updated_at='2026-10-05T09:00:00Z', "
                "first_seen_at='2026-10-05T09:00:00Z', last_seen_at='2026-10-05T09:00:00Z'"
            )
            store.conn.execute("UPDATE open_loops SET created_at='2026-10-05T09:00:00Z'")
            # Two memories whose validity has ended give the staleness sweep something to mark.
            for i in range(2):
                store.create_memory(
                    {
                        "memory_key": f"alpha.old.{i}", "memory_type": "episode", "title": f"OLD{i} Atlas note",
                        "canonical_text": f"Atlas old note {i}", "status": "active", "domain": "project",
                        "sensitivity": "public", "valid_to": "2026-10-04T12:00:00Z",
                        "metadata_json": {"project_scope": [self.alpha]},
                    }
                )
        self.admin = h.key("admin_agent")
        self.reports = {producer: _generate(h, producer, self.alpha, self.admin) for producer in PRODUCERS}
        with h.store() as store:
            weekly = [row for row in store.list_memories(status="candidate", limit=100) if row["memory_key"].startswith("weekly_synthesis")]
        assert weekly, "the weekly run made no candidate memory"
        self.candidate = weekly[0]
        # The owner edits the candidate and makes it confidential. The report that named it is not one of its dependants.
        self.candidate_token = f"CANDIDATE-ONLY-{uuid4().hex}"
        status, body, _ = h.request(
            "POST", f"/v0/vnext/memories/{self.candidate['id']}/review", key=self.admin,
            payload={"action": "edit", "sensitivity": "confidential", "title": self.candidate_token, "canonical_text": self.candidate_token},
        )
        assert status == 200, body
        with h.store() as store:
            store.delete_source(source_id=str(self.sources[1]["id"]), actor_type="user")
        status, body, _ = h.request(
            "POST", "/v0/vnext/memories/forget", payload={"memory_id": str(self.memories[2]["id"]), "reason": "r"}, key=self.admin
        )
        assert status == 200, body
        # One memory is redacted and one archived after the reports were made. A deleted memory is read by no door but
        # redact, and every report that used it keeps listing it.
        self.redacted = str(self.memories[3]["id"])
        self.archived = str(self.memories[4]["id"])
        status, body, _ = h.request(
            "POST", "/v0/vnext/memories/redact", payload={"memory_id": self.redacted, "reason": "r"}, key=self.admin
        )
        assert status == 200, body
        with h.store() as store:
            store.update_memory(memory_id=self.archived, patch={"status": "archived"}, actor_type="system")
        self.hidden = {
            str(self.candidate["id"]): "candidate made confidential",
            str(self.sources[1]["id"]): "source archived",
            self.redacted: "memory redacted",
            self.archived: "memory archived",
        }
        self.forgotten = str(self.memories[2]["id"])

    def hidden_from(self, key_name: str) -> dict[str, str]:
        """The hidden ids a key of this kind may not read. An admin key reads a confidential candidate, so it is not hidden from it."""
        return {
            row_id: reason
            for row_id, reason in self.hidden.items()
            if not (key_name == "admin_bound" and row_id == str(self.candidate["id"]))
        }


@pytest.fixture
def world(label_harness):
    return World(label_harness)


def _sweep(h, world, monkeypatch):
    """Every response of every door that returns stored metadata, for a key of each kind that can reach it.

    The artifact get and trace doors check the artifact against the key's own limits, so a read-only key, a project-scoped
    key and a project-bound admin key read a report within them. The artifact list, the source trace, the workspace and the
    project dashboard are operator routes, which only an unbound trusted key reaches.
    """
    trusted = h.key("trusted_local_agent")
    keys = {
        "trusted": trusted,
        "read_only": h.key("read_only_agent"),
        "project_scoped": h.key("project_scoped_agent", project=world.alpha),
        "admin_bound": h.key("admin_agent", project=world.alpha),
    }
    responses: list[tuple[str, str, int, object]] = []

    def get(key_name, label, path, key):
        status, body, _ = h.request("GET", path, key=key)
        responses.append((key_name, label, status, body))
        return status

    get("trusted", "artifact list", "/v0/vnext/artifacts", trusted)
    for key_name, key in keys.items():
        for producer, report in world.reports.items():
            get(key_name, f"artifact get:{producer}", f"/v0/vnext/artifacts/{report['id']}", key)
            get(key_name, f"artifact trace:{producer}", f"/v0/vnext/traces/artifacts/{report['id']}", key)
    for source in world.sources:
        get("trusted", "source trace", f"/v0/vnext/traces/sources/{source['id']}", trusted)
    for label, path in (
        ("workspace", "workspace"), ("context tree", "context-tree"), ("dogfooding", "dogfooding"),
        ("project dashboard", f"projects/{world.alpha}/dashboard"),
    ):
        assert get("trusted", label, f"/v0/vnext/{path}", trusted) == 200
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id FROM memories ORDER BY created_at, id")
        memory_ids = [str(row["id"]) for row in cur.fetchall()]
    for key_name, key in keys.items():
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        calls: list[tuple[str, str, dict[str, object]]] = [
            ("alice_recall", "list", {"query": "Atlas launch", "limit": 50}),
            ("alice_resume", "list", {}),
            ("alice_context_pack", "list", {"query": "Atlas launch games"}),
            ("alice_recent_decisions", "list", {}),
            ("alice_open_loops", "list", {"action": "list"}),
            ("alice_memory_review", "list", {"status": "all", "limit": 100}),
        ]
        # The detail modes return one memory whole, with the metadata a producer wrote on it: the project update, the
        # weekly candidate, the consolidation candidates and every other memory. A tool that is refused is skipped.
        for memory_id in memory_ids:
            calls.append(("alice_explain", "detail", {"memory_id": memory_id}))
            calls.append(("alice_memory_review", "detail", {"review_item_id": memory_id}))
        for tool, mode, arguments in calls:
            try:
                result = call_mcp_tool(context, name=tool, arguments=arguments)
            except MCPToolError:
                continue
            responses.append((key_name, f"{tool}:{mode}", 200, result))
    return responses


def test_a_hidden_id_stands_only_under_the_fields_that_list_ids(world, label_harness, monkeypatch):
    h = label_harness
    keys_by_reason: dict[str, set[str]] = {}
    doors_by_key: dict[str, set[str]] = {}
    own_ids: list[tuple[str, str]] = []
    for key_name, label, _status, body in _sweep(h, world, monkeypatch):
        door = label.split(":")[0]
        hidden = world.hidden_from(key_name)
        for path, row_id, text in _paths(body):
            if row_id not in hidden:
                continue
            reason, last = hidden[row_id], path.rsplit(".", 1)[-1]
            keys_by_reason.setdefault(reason, set()).add(last)
            doors_by_key.setdefault(key_name, set()).add(door)
            if last not in LISTING_KEYS and not path.endswith(LISTING_PATHS):
                own_ids.append((label, path))
            elif last != "content_markdown":
                assert BARE_ID.match(text), (label, path, text)
    assert not own_ids, own_ids[:10]
    # Every kind of hidden row really does appear, so the checks above ran on something.
    assert set(keys_by_reason) == {
        "candidate made confidential", "source archived", "memory redacted", "memory archived",
    }, keys_by_reason
    assert "candidate_memory_ids" in keys_by_reason["candidate made confidential"]
    assert {"source_ids", "source_refs"} <= keys_by_reason["source archived"]
    for reason in ("memory redacted", "memory archived"):
        assert {"memories", "memory_ids", "member_ids", "source_refs"} <= keys_by_reason[reason], (reason, keys_by_reason[reason])
    # These are the doors that return a stored report or memory with its metadata. The detail modes of explain and review
    # return the lists of a derived memory (a project update lists the sources it was made from), so the id of an
    # archived source stands there too. The list modes of the other tools return none.
    detail_tools = {"alice_explain", "alice_memory_review"}
    assert doors_by_key["trusted"] == {
        "artifact list", "artifact get", "artifact trace", "source trace", "workspace", "project dashboard", *detail_tools,
    }, doors_by_key
    for key_name in ("read_only", "project_scoped", "admin_bound"):
        assert doors_by_key[key_name] == {"artifact get", "artifact trace", *detail_tools}, (key_name, doors_by_key)


def test_a_report_stops_being_readable_when_an_input_is_made_confidential(world, label_harness):
    """The two cases above are the only ones: a report that depends on a row is as strict as the row."""
    h = label_harness
    trusted = h.key("trusted_local_agent")
    daily = str(world.reports["daily"]["id"])
    before = h.request("GET", f"/v0/vnext/artifacts/{daily}", key=trusted)
    assert before[0] == 200
    moved = h.request(
        "POST", f"/v0/vnext/sources/{world.sources[0]['id']}/review", key=world.admin,
        payload={"action": "update", "sensitivity": "confidential"},
    )
    assert moved[0] == 200, moved
    for tail, absent in (
        (f"artifacts/{daily}", f"artifacts/{uuid4()}"),
        (f"traces/artifacts/{daily}", f"traces/artifacts/{uuid4()}"),
    ):
        status, body, _ = h.request("GET", f"/v0/vnext/{tail}", key=trusted)
        # The report is above the key's ceiling now, so the key is answered as for a report that does not exist.
        assert (status, body) == h.request("GET", f"/v0/vnext/{absent}", key=trusted)[:2], (tail, status)
        assert status == 404
        assert str(world.sources[0]["id"]) not in json.dumps(body)
    listing = h.request("GET", "/v0/vnext/artifacts", key=trusted)[1]
    assert daily not in json.dumps(listing)


def test_a_row_hidden_by_its_label_is_never_returned_as_a_row(world, label_harness, monkeypatch):
    """The candidate is named in a report's metadata and is not returned itself, nor is its text."""
    h = label_harness
    candidate = str(world.candidate["id"])
    for key_name, label, _status, body in _sweep(h, world, monkeypatch):
        if candidate not in world.hidden_from(key_name):
            continue  # a project-bound admin key reads a confidential memory
        text = json.dumps(body, default=str)
        assert world.candidate_token not in text, (key_name, label)
        for path, row_id, _value in _paths(body):
            if row_id == candidate:
                assert not path.endswith((".id", ".memory_id", ".target_id")), (key_name, label, path)


def test_the_door_for_a_hidden_id_answers_as_a_missing_row(world, label_harness, monkeypatch):
    """A key reads an id in a report, names it, and is answered as if no such row existed, for every kind of hidden row."""
    h = label_harness
    trusted = h.key("trusted_local_agent")
    candidate = str(world.candidate["id"])
    absent = h.request("GET", f"/v0/vnext/memories/{uuid4()}/audit", key=trusted)
    refused = h.request("GET", f"/v0/vnext/memories/{candidate}/audit", key=trusted)
    assert refused[:2] == absent[:2], (refused, absent)
    assert refused[0] == 404
    assert world.candidate_token not in json.dumps(refused[1])
    # The same for a weekly candidate that was made confidential and a key that names it to every door that takes a memory.
    for method, path, payload in (
        ("POST", f"/v0/vnext/memories/{candidate}/review", {"action": "accept"}),
        ("POST", "/v0/vnext/memories/forget", {"memory_id": candidate, "reason": "r"}),
    ):
        got = h.request(method, path, payload=payload, key=trusted)
        gone = h.request(method, path.replace(candidate, str(uuid4())), payload={**payload, **({"memory_id": str(uuid4())} if "memory_id" in payload else {})}, key=trusted)
        assert got[:2] == gone[:2], (path, got, gone)
    archived = h.request("GET", f"/v0/vnext/traces/sources/{world.sources[1]['id']}", key=trusted)
    missing = h.request("GET", f"/v0/vnext/traces/sources/{uuid4()}", key=trusted)
    assert archived[:2] == missing[:2] == (404, {"detail": "vNext source was not found"})


def test_the_graph_neighborhood_returns_the_titles_an_edge_joins_for_any_id(world, label_harness):
    """A known limit that predates this set, pinned with the connection finder's own edges.

    The neighborhood route is an operator route that applies no label fence to the id it is given. Its edges carry an
    explanation the connection finder made from the rows the edge joins, so after a joined memory is made confidential and
    renamed, the route still returns its old title, and the title of a source that was archived since. Every other door
    answers that memory as a missing one. The security note names the limit, so this test fails when the route is fenced
    and the note has to change with it.
    """
    h = label_harness
    trusted = h.key("trusted_local_agent")
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT to_id FROM graph_edges WHERE to_type = 'memory' ORDER BY created_at, id LIMIT 1")
        memory_id = str(cur.fetchone()["to_id"])
    renamed = f"NEWTITLE-{uuid4().hex}"
    status, body, _ = h.request(
        "POST", f"/v0/vnext/memories/{memory_id}/review", key=world.admin,
        payload={"action": "edit", "sensitivity": "confidential", "title": renamed, "canonical_text": renamed},
    )
    assert status == 200, body
    absent = h.request("GET", f"/v0/vnext/memories/{uuid4()}/audit", key=trusted)
    assert h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=trusted)[:2] == absent[:2]
    status, body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{memory_id}", key=trusted)
    text = json.dumps(body)
    assert status == 200 and body["edge_count"] >= 1
    assert "BELIEF title" in text  # the title the memory had when the edge was made
    assert "SOURCE1 Atlas" in text  # the title of the archived source the edge joins
    assert renamed not in text  # the new title is not in the old explanation


def test_every_producer_names_its_inputs_by_bare_id_and_a_reader_sees_them_unchanged(world, label_harness):
    """What a producer writes beside an id is the report's own text; the ids themselves are opaque."""
    h = label_harness
    trusted = h.key("trusted_local_agent")
    seen: dict[str, set[str]] = {}
    for producer, report in world.reports.items():
        status, body, _ = h.request("GET", f"/v0/vnext/artifacts/{report['id']}", key=trusted)
        assert status == 200
        for path, _row_id, text in _paths(body["metadata_json"]):
            last = path.rsplit(".", 1)[-1]
            if last in {"trace_id", "scheduler_run_id", "agent_run_id", "project_id", "project_scope", "project_floor", "effective_project_scope", "requested_project_scope", "id"}:
                continue
            seen.setdefault(producer, set()).add(last)
            if last in LISTING_KEYS and last != "content_markdown":
                assert BARE_ID.match(text), (producer, path, text)
    assert set(seen) == set(PRODUCERS), set(PRODUCERS) - set(seen)


MEMORY_DOORS = (
    "GET memory audit", "tool explain memory_id", "tool explain continuity_object_id", "tool review item",
    "tool review object", "memory review accept", "memory review edit", "memory review reject", "memory review private",
    "memory review assign", "memory review promote", "memory correct", "memory expire", "memory forget", "memory redact",
    "memory undo", "memory unexpire", "accept consolidation", "tool correct approve", "tool correct reject",
    "tool correct supersede", "tool manage forget", "tool manage expire", "tool manage unexpire", "tool manage redact",
    "tool manage accept", "tool manage undo",
)


@pytest.mark.parametrize("profile", ["trusted", "read_only", "project_scoped", "trusted_bound", "memory_proposal"])
def test_the_id_of_a_deleted_memory_read_from_a_report_is_a_missing_id_to_every_memory_door(
    world, label_harness, monkeypatch, tmp_path, profile
):
    """A key reads a redacted and an archived memory's id in a report it may read, and names it to every door.

    Each door must answer it as it answers an id that was never stored, add and remove the rows that call adds and removes
    (the event log and the agent records included), and leave the key's telemetry where a call on a missing id leaves it.
    A redact of a deleted memory used to record its refusal and so moved the telemetry.
    """
    h = label_harness
    permission, bound = {
        "trusted": ("trusted_local_agent", False), "read_only": ("read_only_agent", False),
        "project_scoped": ("project_scoped_agent", True), "trusted_bound": ("trusted_local_agent", True),
        "memory_proposal": ("memory_proposal_agent", False),
    }[profile]
    key = h.key(permission, project=world.alpha if bound else None)
    observer = h.key("trusted_local_agent")
    env = Env(h, monkeypatch, h.urls["app"], str(tmp_path))
    doors = [door for door in ALL_DOORS if door.name in MEMORY_DOORS]
    assert {door.name for door in doors} == set(MEMORY_DOORS)
    # The ids come out of a report this key reads, when its limits admit the report.
    update = h.request("GET", f"/v0/vnext/artifacts/{world.reports['project_update']['id']}", key=key)
    if update[0] == 200:
        assert {world.redacted, world.archived} <= set(update[1]["metadata_json"]["derived_from"]["memories"]), profile
    else:
        assert profile in {"memory_proposal"}, (profile, update[0])  # a profile the artifact door does not admit
        update = h.request("GET", f"/v0/vnext/artifacts/{world.reports['project_update']['id']}", key=observer)
        assert {world.redacted, world.archived} <= set(update[1]["metadata_json"]["derived_from"]["memories"])

    def telemetry():
        status, body, _ = h.request("GET", "/v0/vnext/agents/policy-telemetry", key=observer)
        assert status == 200, body
        return body["summary"]

    # Some doors are refused for the whole profile before they look at the id (the operator gate), and that is recorded
    # for a missing id as for any other, so the comparison is between two rounds with the same number of calls per door.
    missing = {door.name: door.call(env, key, str(uuid4())) for door in doors}  # the key's first calls
    telemetry()
    t0 = telemetry()
    missing_changes = {}
    for door in doors:
        for _ in range(2):
            before = snapshot(h)
            assert door.call(env, key, str(uuid4())) == missing[door.name]
            missing_changes[door.name] = changes(before, snapshot(h))
    t1 = telemetry()
    failures = []
    for reason, row_id in (("redacted", world.redacted), ("archived", world.archived)):
        for door in doors:
            before = snapshot(h)
            got = door.call(env, key, row_id)
            if got != missing[door.name]:
                failures.append(f"{profile} / {door.name} / {reason}: {got} differs from a missing id: {missing[door.name]}")
            if changes(before, snapshot(h)) != missing_changes[door.name]:
                failures.append(f"{profile} / {door.name} / {reason}: changed {list(changes(before, snapshot(h)))}")
            for token in ("MEMORY3", "MEMORY4"):
                if token in got.body:
                    failures.append(f"{profile} / {door.name} / {reason}: answered with {token}")
    assert not failures, "\n".join(failures[:20])
    t2 = telemetry()
    assert moved(t1, t2) == moved(t0, t1), (profile, moved(t1, t2), moved(t0, t1))


def test_a_report_keeps_the_words_of_a_row_that_was_archived_or_redacted_after_it_was_made(world, label_harness, monkeypatch):
    """What stays beside the ids, pinned so that the security note and the behaviour change together.

    A report is written once. An archived source and a redacted or archived memory leave the reads of every door, but the
    reports and cards that copied from them keep what they copied, and a key that may read the report reads it. The
    security note and the known limitations page say so. When a report stops keeping these, this test fails and the pages
    change with it.
    """
    h = label_harness
    trusted = h.key("trusted_local_agent")

    def report(name):
        status, body, _ = h.request("GET", f"/v0/vnext/artifacts/{world.reports[name]['id']}", key=trusted)
        assert status == 200, (name, status)
        return body

    # Both sources are archived now (the world archived one, this test archives the other, so the choice a producer makes
    # between two equal sources does not matter), the memory is scrubbed, and no door shows any of them.
    with h.store() as store:
        store.delete_source(source_id=str(world.sources[0]["id"]), actor_type="user")
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT canonical_text FROM memories WHERE id = %s", (world.redacted,))
        assert cur.fetchone()["canonical_text"] == "[REDACTED]"
    for source in world.sources:
        assert h.request("GET", f"/v0/vnext/traces/sources/{source['id']}", key=trusted)[0] == 404
    # Reports keep the lines they printed. The project update prints every memory and source it used, so it still prints the
    # redacted and the archived memory's text and both sources' titles, and so does any other report that printed an id.
    update = report("project_update")
    assert "MEMORY3" in update["content_markdown"] and "MEMORY4" in update["content_markdown"]
    assert "SOURCE0 Atlas" in update["content_markdown"] and "SOURCE1 Atlas" in update["content_markdown"]
    printing = [name for name in world.reports if f"memory:{world.redacted}" in report(name).get("content_markdown", "")]
    assert "project_update" in printing
    assert all("MEMORY3" in report(name)["content_markdown"] for name in printing), printing
    assert "SOURCE0 Atlas" in report("daily")["content_markdown"] and "SOURCE1 Atlas" in report("daily")["content_markdown"]
    # The quoted fields of three producers keep the claim, the title, the shared terms and the quote of a source.
    assert " text." in update["metadata_json"]["suggested_current_state"] and "SOURCE" in update["metadata_json"]["suggested_current_state"]
    assert any("SOURCE" in item["explanation"] and "Atlas" in item["explanation"] for item in report("connections")["metadata_json"]["connections"])
    assert any("SOURCE" in item["quote_new"] and " text." in item["quote_new"] for item in report("contradictions")["metadata_json"]["contradictions"])
    # The roll-up card keeps the text and label of every memory it rolled up, and a consolidation candidate keeps a digest
    # of each member as it was. Both are memories a key reads through the detail modes of explain and review.
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM memories WHERE memory_key LIKE 'vnext.rollup.%' ORDER BY created_at")
        rollups = [row["id"] for row in cur.fetchall()]
        cur.execute("SELECT id::text AS id FROM memories WHERE metadata_json ? 'consolidation' ORDER BY created_at")
        candidates = [row["id"] for row in cur.fetchall()]
    assert rollups and candidates
    monkeypatch.setenv("ALICE_AGENT_API_KEY", trusted)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    card = call_mcp_tool(context, name="alice_explain", arguments={"memory_id": rollups[0]})
    instances = card["memory"]["value"]["rollup"]["instances"]
    assert {"id": world.redacted} and any(
        instance["memory_id"] == world.redacted and "MEMORY3" in instance["text"] for instance in instances
    ), "the card no longer keeps the text of the redacted memory"
    detail = call_mcp_tool(context, name="alice_memory_review", arguments={"review_item_id": candidates[0]})
    snapshots = {item["id"]: item for item in detail["review"]["memory"]["metadata_json"]["consolidation"]["member_snapshots"]}
    assert re.fullmatch(r"[0-9a-f]{16}", snapshots[world.redacted]["content_digest"])
    assert snapshots[world.redacted]["status"] == "active"  # the digest and status are those of the member as it was
