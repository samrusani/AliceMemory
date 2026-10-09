"""Where a readable row lists the id of a row the same key cannot read, and what stands next to the id.

Every producer runs through the mounted application with an admin key over a vault of readable rows. Then rows are made
unreadable after the reports exist, the only way a readable report can name an unreadable row (a report is stored at
least as strict as its inputs, so an input made confidential makes the report confidential too):

* the weekly candidate, which the report names in ``candidate_memory_ids`` and which is not one of its inputs, is made
  confidential;
* a source a report was made from is archived;
* a memory a report was made from is forgotten (explain and review still show its history, so it is not hidden, and it
  is the control for that);
* two memories a report was made from are archived. A deleted memory is read by no door but redact, and a report keeps
  listing it. A memory a report was made from that is redacted afterwards is not in this list: a report that recorded it is
  read by the owner and an unbound admin key only, so no key below that reads the id in it (the last test redacts one of
  the archived memories to pin that).

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
6. A source captured as confidential after the reports exist is named nowhere a key below the confidential ceiling reads:
   not by the event feed (a chunk event names its source in the payload), not by the agent feed, and not by the event
   count, while the owner and an unbound admin key still see its events.
7. The ids are not the only thing a report keeps. It keeps the words and values it was made with, so the text of a source
   and of a memory that were archived afterwards stays in the reports that quoted it, and a key that may read the report
   reads it. The text of a memory that is redacted afterwards stays in the reports too, but they are then read by the owner
   and an unbound admin key only. The security note says so, and this file pins what is kept and who reads it so that the
   note and the behaviour change together.
"""
from __future__ import annotations

import json
import re
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.routers.workspaces import _vnext_workspace_payload
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
# The fields of an event that name the row it is about.
EVENT_TARGETS = ("target_id", "memory_id")


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


# Tables that hold the rows a capture makes. A row of any of them that a capture made must not be named to a key below the
# confidential ceiling (the entity tables are left out: an entity has no label and the capture makes none here).
CAPTURE_TABLES = (
    "sources", "source_chunks", "memories", "memory_revisions", "open_loops", "generated_artifacts", "graph_edges",
    "provenance_links", "beliefs",
)


def _row_ids(h) -> dict[str, set[str]]:
    with h.store() as store, store.conn.cursor() as cur:
        found = {}
        for table in CAPTURE_TABLES:
            cur.execute(f"SELECT id::text AS id FROM {table}")  # closed table names
            found[table] = {row["id"] for row in cur.fetchall()}
        cur.execute("SELECT id::text AS id FROM event_log")
        found["event_log"] = {row["id"] for row in cur.fetchall()}
    return found


def capture_confidential_source(h, alpha, key, *, statements=True, size=4) -> dict[str, object]:
    """Capture a confidential source through the API, as the admin key. Return every row and event the capture made.

    With ``statements`` the text holds a decision and a task, so the capture makes candidate memories and open loops too.
    Without them it makes a source and its chunks only, so the chunk events stand among the latest events of the log.
    """
    before = _row_ids(h)
    token = f"CONFIDENTIAL-CAPTURE-{uuid4().hex}"
    extra = "Decision: ship {t} {i}.\nTodo: review {t} {i}. " if statements else ""
    paragraphs = [f"Paragraph {i} {token}. " + extra.format(t=token, i=i) + "lorem ipsum dolor " * 120 for i in range(size)]
    status, body, _ = h.request(
        "POST", "/v0/vnext/sources", key=key,
        payload={"raw_text": "\n\n".join(paragraphs), "title": f"{token} title", "project_scope": [alpha], "domain": "project", "sensitivity": "confidential"},
    )
    assert status == 201, body
    after = _row_ids(h)
    made = {table: after[table] - before[table] for table in CAPTURE_TABLES}
    assert made["sources"] == {body["source_id"]} and len(made["source_chunks"]) >= size, made
    assert bool(made["memories"]) == statements, made
    return {
        "token": token, "source_id": body["source_id"], "chunk_ids": made["source_chunks"],
        "ids": set().union(*made.values()), "events": after["event_log"] - before["event_log"],
    }


def capture_public_source(h, alpha, key) -> dict[str, object]:
    """Capture a public source with statements through the API and accept the memories it made. Return their ids and hash."""
    token = f"KEPT-CAPTURE-{uuid4().hex}"
    text = "\n\n".join(f"Paragraph {i} {token}. Decision: ship {token} {i}.\nTodo: review {token} {i}." for i in range(2))
    status, body, _ = h.request(
        "POST", "/v0/vnext/sources", key=key,
        payload={"raw_text": text, "title": f"{token} title", "project_scope": [alpha], "domain": "project", "sensitivity": "public"},
    )
    assert status == 201, body
    source_id = body["source_id"]
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM memories WHERE metadata_json ->> 'source_id' = %s ORDER BY created_at, id", (source_id,))
        memory_ids = [row["id"] for row in cur.fetchall()]
        cur.execute("SELECT content_hash FROM sources WHERE id = %s", (source_id,))
        content_hash = cur.fetchone()["content_hash"]
    assert memory_ids and content_hash.startswith("sha256:")
    for memory_id in memory_ids:
        status, body, _ = h.request("POST", f"/v0/vnext/memories/{memory_id}/review", payload={"action": "accept"}, key=key)
        assert status == 200, body
    return {"token": token, "source_id": source_id, "memory_ids": memory_ids, "hash": content_hash}


def _strings(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, path)
    elif isinstance(value, str):
        yield path, value


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
        # Two memories are archived after the reports were made. A deleted memory is read by no door but redact, and every
        # report that used it keeps listing it. The first is redacted later by the last test, which pins that a report that
        # recorded a redacted memory is then read by the owner and an unbound admin key only.
        self.to_redact = str(self.memories[3]["id"])
        self.archived = str(self.memories[4]["id"])
        with h.store() as store:
            for memory_id in (self.to_redact, self.archived):
                store.update_memory(memory_id=memory_id, patch={"status": "archived"}, actor_type="system")
        # A third memory is made and redacted at once. No report used it, so no report lists it; it stands for a redacted id
        # that a key already holds, to be named to every door.
        with h.store() as store:
            extra = store.create_memory(
                {
                    "memory_key": "alpha.extra", "memory_type": "episode", "title": "EXTRA Atlas note",
                    "canonical_text": "EXTRA Atlas note", "status": "active", "domain": "project", "sensitivity": "public",
                    "metadata_json": {"project_scope": [self.alpha]},
                }
            )
        self.redacted = str(extra["id"])
        status, body, _ = h.request(
            "POST", "/v0/vnext/memories/redact", payload={"memory_id": self.redacted, "reason": "r"}, key=self.admin
        )
        assert status == 200, body
        self.hidden = {
            str(self.candidate["id"]): "candidate made confidential",
            str(self.sources[1]["id"]): "source archived",
            self.to_redact: "second memory archived",
            self.archived: "memory archived",
        }
        self.forgotten = str(self.memories[2]["id"])
        # A source captured as confidential now that the reports exist, so no report used it. Every row the capture made is
        # hidden from a key below the ceiling, and none of them may be named to it.
        self.capture = capture_confidential_source(h, self.alpha, self.admin)

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
    # The audit of a memory returns its metadata, its revisions and the changes its events recorded, so it is called for every
    # memory (the derived ones among them) by every kind of key. A key that is refused, or a memory it cannot read, gives a
    # status that is not 200 and no ids.
    for key_name, key in keys.items():
        for memory_id in memory_ids:
            get(key_name, "memory audit", f"/v0/vnext/memories/{memory_id}/audit", key)
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
    audit_paths: set[str] = set()
    own_ids: list[tuple[str, str]] = []
    # The rows of the confidential capture are named by no door to a key below the ceiling, as an id or in words. A
    # project-bound admin key is not below it.
    named: list[tuple[str, str, str]] = []
    for key_name, label, _status, body in _sweep(h, world, monkeypatch):
        door = label.split(":")[0]
        if key_name != "admin_bound":
            if world.capture["token"] in json.dumps(body, default=str):
                named.append((key_name, label, "the words of the source"))
            named.extend((key_name, label, path) for path, row_id, _text in _paths(body) if row_id in world.capture["ids"])
        hidden = world.hidden_from(key_name)
        for path, row_id, text in _paths(body):
            if row_id not in hidden:
                continue
            reason, last = hidden[row_id], path.rsplit(".", 1)[-1]
            if door == "workspace" and ".recent_events." in path and reason.endswith("memory archived") and last in EVENT_TARGETS:
                # The event feed judges an event by the label of the rows it names, and an archived memory keeps its label,
                # so the events about the memory name it as their target. Whether they stand in the latest events depends on
                # how many events the other calls of this sweep wrote, so the test does not count on them either way.
                continue
            keys_by_reason.setdefault(reason, set()).add(last)
            doors_by_key.setdefault(key_name, set()).add(door)
            if door == "memory audit":
                audit_paths.add(path)
            if last not in LISTING_KEYS and not path.endswith(LISTING_PATHS):
                own_ids.append((label, path))
            elif last != "content_markdown":
                assert BARE_ID.match(text), (label, path, text)
    assert not own_ids, own_ids[:10]
    assert not named, named[:10]
    # Every kind of hidden row really does appear, so the checks above ran on something.
    assert set(keys_by_reason) == {
        "candidate made confidential", "source archived", "memory archived", "second memory archived",
    }, keys_by_reason
    assert "candidate_memory_ids" in keys_by_reason["candidate made confidential"]
    assert {"source_ids", "source_refs"} <= keys_by_reason["source archived"]
    for reason in ("memory archived", "second memory archived"):
        assert {"memories", "memory_ids", "member_ids", "source_refs"} <= keys_by_reason[reason], (reason, keys_by_reason[reason])
    # The memory audit returns the lists of a derived memory in the memory, in its revisions and in the changes its events
    # recorded, and an archived member stands in each of them. The security note, the tool reference, the known
    # limitations and the changelog name the route; this fails when a field is added or goes.
    assert {
        ".memory.metadata_json.consolidation.cluster_member_ids", ".memory.metadata_json.derived_from.memories",
        ".memory.metadata_json.source_refs", ".memory.value.rollup.member_ids",
        ".revisions.previous_value.rollup.member_ids", ".revisions.new_value.rollup.member_ids",
        ".events.payload_json.changes.metadata_json.consolidation.cluster_member_ids",
        ".events.payload_json.changes.metadata_json.derived_from.memories", ".events.payload_json.changes.metadata_json.source_refs",
    } <= audit_paths, sorted(audit_paths)
    # These are the doors that return a stored report or memory with its metadata. The detail modes of explain and review
    # return the lists of a derived memory (a project update lists the sources it was made from), so the id of an
    # archived source stands there too, and so does the memory audit, an operator route, with the revisions and the event
    # changes of the memory. The list modes of the other tools return none.
    detail_tools = {"alice_explain", "alice_memory_review"}
    assert doors_by_key["trusted"] == {
        "artifact list", "artifact get", "artifact trace", "source trace", "workspace", "project dashboard", "memory audit",
        *detail_tools,
    }, doors_by_key
    for key_name in ("read_only", "project_scoped", "admin_bound"):
        assert doors_by_key[key_name] == {"artifact get", "artifact trace", *detail_tools}, (key_name, doors_by_key)


def _workspace(h, key):
    """The workspace as a key reads it over HTTP, or as the owner reads it (the owner has no key and no route)."""
    if key is None:
        with h.store() as store:
            return json.loads(json.dumps(_vnext_workspace_payload(store, identity=None), default=str))
    status, body, _ = h.request("GET", "/v0/vnext/workspace", key=key)
    assert status == 200, (status, body)
    return body


def _event_texts(h, event_ids):
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id, row_to_json(e)::text AS text FROM event_log e WHERE id::text = ANY(%s)", (sorted(event_ids),))
        return {row["id"]: row["text"] for row in cur.fetchall()}


def test_a_confidential_capture_is_named_by_no_event_in_the_feed_of_a_key_below_the_ceiling(world, label_harness):
    """The chunk events of a source name it in their payload, and the feed used to show them to any key.

    The capture here has no statements, so its chunk events stand among the latest events of the log, where the workspace
    feed reads. The owner and an unbound admin key are not limited and still read them.
    """
    h = label_harness
    trusted = h.key("trusted_local_agent")
    capture = capture_confidential_source(h, world.alpha, world.admin, statements=False, size=6)
    texts = _event_texts(h, capture["events"])
    assert any('"source_chunk.created"' in text for text in texts.values()), "the capture wrote no chunk event"

    body = _workspace(h, trusted)
    for name, feed in (("recent_events", body["recent_events"]), ("agent_activity", body["agent_activity"]["recent_events"])):
        text = json.dumps(feed, default=str)
        assert capture["token"] not in text, name
        assert not [row_id for row_id in capture["ids"] if row_id in text], name
    for name, key in (("admin", world.admin), ("owner", None)):
        chunk_events = [event for event in _workspace(h, key)["recent_events"] if event["event_type"] == "source_chunk.created"]
        assert chunk_events, name
        assert {event["payload_json"]["source_id"] for event in chunk_events} == {capture["source_id"]}, name


def test_a_confidential_capture_adds_to_a_count_only_the_events_that_name_none_of_its_rows(world, label_harness):
    """The count of a key below the ceiling grows by the capture's events that name no row of the capture, and no more."""
    h = label_harness
    trusted = h.key("trusted_local_agent")
    keys = {"trusted": trusted, "admin": world.admin, "owner": None}

    def count(key):
        return _workspace(h, key)["summary"]["event_count"]

    grown = {}
    for statements in (False, True):
        steps, last = {}, {}
        for name, key in keys.items():
            count(key)  # the first read of a key writes its identity and the default workflows
            counts = [count(key) for _ in range(3)]
            steps[name] = counts[1] - counts[0]
            assert counts[2] - counts[1] == steps[name], (name, counts)
            last[name] = counts[2]
        capture = capture_confidential_source(h, world.alpha, world.admin, statements=statements, size=6)
        texts = _event_texts(h, capture["events"])
        named = {event_id for event_id, text in texts.items() if any(row_id in text for row_id in capture["ids"])}
        grown[statements] = {}
        for name, key in keys.items():
            grown[statements][name] = count(key) - last[name] - steps[name]
        assert grown[statements]["trusted"] == len(texts) - len(named), (statements, grown[statements], len(texts), len(named))
        assert grown[statements]["admin"] == grown[statements]["owner"] == len(texts), (statements, grown[statements])
        assert len(named) > 5
    # With statements the capture makes memories, and their events name rows of the capture as well.
    assert grown[True]["admin"] > grown[False]["admin"]


def test_a_memory_keeps_the_source_it_was_made_from_after_the_source_is_archived(label_harness, monkeypatch):
    """The memory keeps `source_id`, `source_event_ids` and `capture_content_hash` (the SHA-256 of the source's whole text).

    A source is archived and no door reads it, but the memories captured from it stay readable and keep the id of the source,
    the ids of its chunks and the hash of its whole captured text. This pins which doors return them, so the security note,
    the tool reference, the known limitations and the changelog say the same. It uses a small vault of its own, so that the
    memories stand within the window the workspace lists.
    """
    h = label_harness
    alpha = str(uuid4())
    with h.store() as store:
        store.create_project({"id": alpha, "name": "Atlas", "slug": "atlas", "domain": "project", "sensitivity": "public"})
    admin = h.key("admin_agent")
    kept = capture_public_source(h, alpha, admin)
    status, body, _ = h.request("DELETE", f"/v0/vnext/sources/{kept['source_id']}", key=admin)
    assert status == 200, body
    assert h.request("GET", f"/v0/vnext/sources/{kept['source_id']}", key=h.key("trusted_local_agent"))[0] == 404
    keys = {
        "trusted": h.key("trusted_local_agent"),
        "read_only": h.key("read_only_agent"),
        "project_scoped": h.key("project_scoped_agent", project=alpha),
        "admin_bound": h.key("admin_agent", project=alpha),
    }
    found: dict[tuple[str, str], set[str]] = {}

    def note(key_name, door, body):
        for path, value in _strings(body):
            last = path if door == "workspace" else path.rsplit(".", 1)[-1]  # the workspace is pinned by the whole path
            if value == kept["hash"]:
                found.setdefault((key_name, door), set()).add("hash:" + last)
            elif kept["source_id"] in value:
                found.setdefault((key_name, door), set()).add("id:" + last)

    # The workspace lists the latest events, so it is read before the calls below write their own.
    note("trusted", "workspace", h.request("GET", "/v0/vnext/workspace", key=keys["trusted"])[1])
    explained = {}
    for key_name, key in keys.items():
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        for memory_id in kept["memory_ids"]:
            note(key_name, "memory audit", h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=key)[1])
            for tool, arguments in (("alice_memory_review", {"review_item_id": memory_id}), ("alice_explain", {"memory_id": memory_id})):
                try:
                    result = call_mcp_tool(context, name=tool, arguments=arguments)
                except MCPToolError as error:
                    explained[(key_name, tool)] = str(error)
                    continue
                note(key_name, tool, result)
        for tool, arguments in (
            ("alice_recall", {"query": kept["token"]}), ("alice_context_pack", {"query": kept["token"]}), ("alice_resume", {}),
            ("alice_open_loops", {"action": "list"}),
        ):
            try:
                note(key_name, tool, call_mcp_tool(context, name=tool, arguments=arguments))
            except MCPToolError:
                pass
    memory_fields = {"hash:capture_content_hash", "id:source_id", "id:source_event_ids"}
    # The memory audit and the workspace are operator routes and answer an unbound trusted key. The detail mode of
    # alice_memory_review answers every key whose limits admit the memory. alice_explain answers the memory of an archived
    # source as unavailable, so it returns none of these.
    assert memory_fields <= found[("trusted", "memory audit")], found
    assert memory_fields <= found[("trusted", "alice_memory_review")], found
    # The workspace returns them in its event feed: in the changes the memory's events copied from its metadata, and as
    # the target of the events of the source.
    assert {
        "hash:.recent_events.payload_json.changes.metadata_json.capture_content_hash",
        "id:.recent_events.payload_json.changes.metadata_json.source_id", "id:.recent_events.target_id",
    } <= found[("trusted", "workspace")], found
    for key_name in ("read_only", "project_scoped", "admin_bound"):
        assert memory_fields <= found[(key_name, "alice_memory_review")], (key_name, found)
    assert {key_name for key_name, tool in explained if tool == "alice_explain"} == set(keys), explained
    assert all("unavailable" in message for (_key, tool), message in explained.items() if tool == "alice_explain"), explained
    # No other door names the source: not the search tools, not the lists.
    assert {door for (_key, door) in found} == {"memory audit", "alice_memory_review", "workspace"}, found


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


def test_the_graph_neighborhood_lists_an_edge_only_when_the_key_may_read_the_rows_it_joins(world, label_harness):
    """The explanation of an edge is made from the rows it joins, so a key with limits is listed the edges whose ends it may read.

    The connection finder made the edges of this vault, and each explanation holds the titles of the rows it joins. After a
    joined memory is made confidential and renamed, the edge still holds the title the memory had, and the title of a source
    archived since. Every other door answers that memory as a missing one. The neighborhood of the memory, and of the other
    end of the edge, no longer lists the edge to the unbound trusted key; it is answered as an id with no edges is. The unbound
    admin key is not limited and still reads it. The security note names the rule, so this test fails when it changes.
    """
    h = label_harness
    trusted = h.key("trusted_local_agent")
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id, from_id::text AS from_id, to_id::text AS to_id FROM graph_edges WHERE to_type = 'memory' ORDER BY created_at, id LIMIT 1")
        edge = cur.fetchone()
    memory_id, edge_id = edge["to_id"], edge["id"]

    def neighborhood(target, key):
        status, body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{target}", key=key)
        assert status == 200, (target, status)
        return body

    def listed(body):
        return {item["id"] for item in (*body["from_edges"], *body["to_edges"])}

    # While the memory is readable the key is listed the edge, with the title the connection finder wrote.
    assert edge_id in listed(neighborhood(memory_id, trusted))
    assert "BELIEF title" in json.dumps(neighborhood(memory_id, trusted))
    renamed = f"NEWTITLE-{uuid4().hex}"
    status, body, _ = h.request(
        "POST", f"/v0/vnext/memories/{memory_id}/review", key=world.admin,
        payload={"action": "edit", "sensitivity": "confidential", "title": renamed, "canonical_text": renamed},
    )
    assert status == 200, body
    absent = h.request("GET", f"/v0/vnext/memories/{uuid4()}/audit", key=trusted)
    assert h.request("GET", f"/v0/vnext/memories/{memory_id}/audit", key=trusted)[:2] == absent[:2]
    for target in (memory_id, edge["from_id"]):
        body = neighborhood(target, trusted)
        assert edge_id not in listed(body), target
        assert renamed not in json.dumps(body), target
        assert body["edge_count"] == len(listed(body))
    memory_text = json.dumps(neighborhood(memory_id, trusted))
    assert "BELIEF title" not in memory_text and "SOURCE1 Atlas" not in memory_text
    # The memory has no edge the key may read, so its neighborhood is the neighborhood of an id that has none.
    gone = neighborhood(str(uuid4()), trusted)
    assert neighborhood(memory_id, trusted) == {**gone, "target_id": memory_id}
    # The unbound admin key is not limited. It lists the edge, with the title the memory had and not the new one.
    for target in (memory_id, edge["from_id"]):
        assert edge_id in listed(neighborhood(target, world.admin)), target
    admin_text = json.dumps(neighborhood(memory_id, world.admin))
    assert "BELIEF title" in admin_text and "SOURCE1 Atlas" in admin_text and renamed not in admin_text


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
    """A key reads the ids of archived memories in a report it may read, holds the id of a redacted one, and names them to every door.

    No report lists the redacted memory here: a report that recorded a redacted memory is read by the owner and an unbound
    admin key only, so a key below them learns such an id from somewhere else. The doors answer it as a missing id all the same.

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
        assert {world.to_redact, world.archived} <= set(update[1]["metadata_json"]["derived_from"]["memories"]), profile
    else:
        assert profile in {"memory_proposal"}, (profile, update[0])  # a profile the artifact door does not admit
        update = h.request("GET", f"/v0/vnext/artifacts/{world.reports['project_update']['id']}", key=observer)
        assert {world.to_redact, world.archived} <= set(update[1]["metadata_json"]["derived_from"]["memories"])

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
    deleted = (("redacted", world.redacted), ("archived", world.archived), ("second archived", world.to_redact))
    for door in doors:
        for _ in deleted:
            before = snapshot(h)
            assert door.call(env, key, str(uuid4())) == missing[door.name]
            missing_changes[door.name] = changes(before, snapshot(h))
    t1 = telemetry()
    failures = []
    for reason, row_id in deleted:
        for door in doors:
            before = snapshot(h)
            got = door.call(env, key, row_id)
            if got != missing[door.name]:
                failures.append(f"{profile} / {door.name} / {reason}: {got} differs from a missing id: {missing[door.name]}")
            if changes(before, snapshot(h)) != missing_changes[door.name]:
                failures.append(f"{profile} / {door.name} / {reason}: changed {list(changes(before, snapshot(h)))}")
            for token in ("MEMORY3", "MEMORY4", "EXTRA"):
                if token in got.body:
                    failures.append(f"{profile} / {door.name} / {reason}: answered with {token}")
    assert not failures, "\n".join(failures[:20])
    t2 = telemetry()
    assert moved(t1, t2) == moved(t0, t1), (profile, moved(t1, t2), moved(t0, t1))


def test_a_report_keeps_the_words_of_a_row_that_was_archived_or_redacted_after_it_was_made(world, label_harness, monkeypatch):
    """What stays beside the ids, and who reads it, pinned so that the security note and the behaviour change together.

    A report is written once. An archived source and an archived memory leave the reads of every door, but the reports and
    cards that copied from them keep what they copied, and a key that may read the report reads it, as before. A memory that
    is redacted after the report was made leaves its words in the report as well, but the report is then contained: the owner
    and an unbound admin key read it, and no other key does. That is containment and not removal. The security note and the
    known limitations page say so. When a report stops keeping these words, or a key below an unbound admin key reads the
    words of a redacted memory, this test fails and the pages change with it.
    """
    h = label_harness
    trusted = h.key("trusted_local_agent")

    def report(name, key=trusted):
        status, body, _ = h.request("GET", f"/v0/vnext/artifacts/{world.reports[name]['id']}", key=key)
        assert status == 200, (name, status)
        return body

    # Both sources are archived now (the world archived one, this test archives the other, so the choice a producer makes
    # between two equal sources does not matter), the two memories are archived, and no door shows any of them.
    with h.store() as store:
        store.delete_source(source_id=str(world.sources[0]["id"]), actor_type="user")
    for source in world.sources:
        assert h.request("GET", f"/v0/vnext/traces/sources/{source['id']}", key=trusted)[0] == 404
    # Reports keep the lines they printed, and the key that reads them reads the words of what was archived. The project
    # update prints every memory and source it used, so it still prints both memories' text and both sources' titles, and so
    # does any other report that printed an id.
    update = report("project_update")
    assert "MEMORY3" in update["content_markdown"] and "MEMORY4" in update["content_markdown"]
    assert "SOURCE0 Atlas" in update["content_markdown"] and "SOURCE1 Atlas" in update["content_markdown"]
    printing = [name for name in world.reports if f"memory:{world.to_redact}" in report(name).get("content_markdown", "")]
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

    def explain_card(key):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        return call_mcp_tool(context, name="alice_explain", arguments={"memory_id": rollups[0]})

    instances = explain_card(trusted)["memory"]["value"]["rollup"]["instances"]
    assert any(
        instance["memory_id"] == world.to_redact and "MEMORY3" in instance["text"] for instance in instances
    ), "the card no longer keeps the text of the archived memory"
    monkeypatch.setenv("ALICE_AGENT_API_KEY", trusted)
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    detail = call_mcp_tool(context, name="alice_memory_review", arguments={"review_item_id": candidates[0]})
    snapshots = {item["id"]: item for item in detail["review"]["memory"]["metadata_json"]["consolidation"]["member_snapshots"]}
    assert re.fullmatch(r"[0-9a-f]{16}", snapshots[world.to_redact]["content_digest"])
    assert snapshots[world.to_redact]["status"] == "active"  # the digest and status are those of the member as it was

    # A memory that is redacted now keeps its words in the same reports, and the reports that recorded it are contained:
    # the owner and an unbound admin key read them, and the key that read them a moment ago is answered as for a report that
    # does not exist. The rows are not rewritten. This is containment, not removal.
    status, body, _ = h.request(
        "POST", "/v0/vnext/memories/redact", payload={"memory_id": world.to_redact, "reason": "r"}, key=world.admin
    )
    assert status == 200, body
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT canonical_text FROM memories WHERE id = %s", (world.to_redact,))
        assert cur.fetchone()["canonical_text"] == "[REDACTED]"
    for name in printing:
        status, body, _ = h.request("GET", f"/v0/vnext/artifacts/{world.reports[name]['id']}", key=trusted)
        assert (status, body) == h.request("GET", f"/v0/vnext/artifacts/{uuid4()}", key=trusted)[:2], name
        assert "MEMORY3" in report(name, world.admin)["content_markdown"], name
    assert "MEMORY3" in report("project_update", world.admin)["content_markdown"]
    with pytest.raises(MCPToolError):
        explain_card(trusted)
    admin_instances = explain_card(world.admin)["memory"]["value"]["rollup"]["instances"]
    assert any(instance["memory_id"] == world.to_redact and "MEMORY3" in instance["text"] for instance in admin_instances)
    # The archived memory's words stand in the same rows and are contained with them: the rule follows the recorded input
    # that was redacted, and the report that kept both is no longer read below an unbound admin key.
    assert "MEMORY4" in report("project_update", world.admin)["content_markdown"]
