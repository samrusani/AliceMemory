"""A vault with a hidden and a visible row of every kind, for the operator route sweep.

Every hidden row carries a sentinel in each text field an operator screen could print (title, text, name, state,
explanation, comment). The visible rows carry a different marker, so a route that answers with nothing at all is told
apart from a route that answers with only what the caller may read.

``owner=True`` builds the same vault with no agent key at all, so every call is the owner's. A vault with keys is read
by the admin key (no limits, the control) and by the trusted key (the ceiling: public, internal, private, unknown).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import uuid4

from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_label_writes import without_insert_floor

HIDDEN = "SENTINEL-HIDDEN"
SHOWN = "MARKER-SHOWN"

_EMPTY_DERIVATION = {
    "v": 1,
    "sources": [],
    "memories": [],
    "open_loops": [],
    "artifacts": [],
    "beliefs": [],
    "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
}


@dataclass
class Vault:
    """Rows built through the store and the product routes, and the keys that read them."""

    harness: object
    tag: str
    owner: bool = False
    ids: dict[str, str] = field(default_factory=dict)
    keys: dict[str, str | None] = field(default_factory=dict)
    agent_ids: dict[str, str] = field(default_factory=dict)
    # table -> ids of the rows no caller below the ceiling may read, so a test can show a call changed none of them.
    hidden_ids: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _texts: dict[str, str] = field(default_factory=dict)

    #: Names a probe may send, and so the sweep never looks for in an answer.
    SENDABLE = ("dedupe_source-raw", "dedupe_memory-text", "dedupe_project-name")

    # -- naming ------------------------------------------------------------------------------------------------

    def hidden(self, name: str) -> str:
        return f"{HIDDEN}-{self.tag}-{name}"

    def shown(self, name: str) -> str:
        return f"{SHOWN}-{self.tag}-{name}"

    def text(self, name: str) -> str:
        """The sentinel text registered under ``name``."""

        return self._texts[name]

    def _text(self, name: str, *, hidden: bool) -> str:
        value = self.hidden(name) if hidden else self.shown(name)
        self._texts[name] = value
        return value

    def is_hidden(self, name: str) -> bool:
        return self._texts[name].startswith(HIDDEN)

    def remember_hidden(self, table: str, row_id: object) -> None:
        self.hidden_ids[table].append(str(row_id))

    # -- requests ----------------------------------------------------------------------------------------------

    def admin_request(self, method: str, path: str, payload: dict | None = None):
        """A call by the unbound admin key, or by the owner in an owner vault."""

        status, body, _headers = self.harness.request(method, path, payload=payload, key=self.keys.get("admin"))
        return status, body

    def hidden_rows(self) -> dict[tuple[str, str], object]:
        """The stored rows of the hidden set, as JSON, to compare before and after a call."""

        found: dict[tuple[str, str], object] = {}
        with self.harness.store() as store, store.conn.cursor() as cur:
            for table, ids in self.hidden_ids.items():
                if not ids:
                    continue
                cur.execute(
                    f"SELECT id, row_to_json(t) AS body FROM {table} t WHERE id = ANY(%s::uuid[])",  # closed table names
                    (ids,),
                )
                for row in cur.fetchall():
                    found[(table, str(row["id"]))] = row["body"]
        return found

    # -- building ----------------------------------------------------------------------------------------------

    def build(self) -> Vault:
        h = self.harness
        if not self.owner:
            self.keys["admin"] = h.key("admin_agent")
            self.keys["trusted"] = h.key("trusted_local_agent")
            with h.store() as store, store.conn.cursor() as cur:
                cur.execute("SELECT agent_id, permission_profile FROM agent_api_keys")
                self.agent_ids = {row["permission_profile"]: row["agent_id"] for row in cur.fetchall()}
        with h.store() as store:
            self._sources(store)
            self._memories(store)
            self._artifacts(store)
            self._loops_and_projects(store)
            self._graph(store)
        self._commits()
        self._dedupe_rows()
        self._queue_task()
        self._charter()
        self._insight_feedback()
        self._connector_sync()
        self._scheduler_runs()
        self._connector_sync_again()
        return self

    def _source(self, store, name: str, *, hidden: bool, sensitivity: str):
        title = self._text(f"{name}-title", hidden=hidden)
        raw = self._text(f"{name}-raw", hidden=hidden)
        chunk = self._text(f"{name}-chunk", hidden=hidden)
        todo = self._text(f"{name}-todo", hidden=hidden)
        row = store.create_source(
            {
                "source_type": "note",
                "title": title,
                "content_hash": str(uuid4()),
                "domain": "project",
                "sensitivity": sensitivity,
                "metadata_json": {"raw_text": f"{raw}\nTODO: {todo}"},
            }
        )
        store.create_source_chunk({"source_id": str(row["id"]), "chunk_index": 0, "text": f"{chunk}\nTODO: {todo}"})
        self.ids[name] = str(row["id"])
        if hidden:
            self.remember_hidden("sources", row["id"])
        return row

    def _sources(self, store) -> None:
        self._source(store, "source_hidden", hidden=True, sensitivity="confidential")
        self._source(store, "source_shown", hidden=False, sensitivity="public")
        # A source captured through a connector carries the connector's name, and the connector screens list it.
        for name, hidden, sensitivity in (("capture_hidden", True, "confidential"), ("capture_shown", False, "public")):
            row = self._source(store, name, hidden=hidden, sensitivity=sensitivity)
            store.conn.execute("UPDATE sources SET connector_name = %s WHERE id = %s", ("local_folder", row["id"]))

    def _memory(self, store, name: str, *, hidden: bool, sensitivity: str, metadata=None, status="active", floor=True):
        record = {
            "memory_key": f"{self.tag}-{name}",
            "canonical_text": self._text(f"{name}-text", hidden=hidden),
            "title": self._text(f"{name}-title", hidden=hidden),
            "status": status,
            "domain": "project",
            "sensitivity": sensitivity,
            "metadata_json": dict(metadata or {}),
        }
        if floor:
            row = store.create_memory(record)
        else:
            # Stored below the labels of its input, as a row is after a source is relabelled without the repair.
            with without_insert_floor():
                row = store.create_memory(record)
        self.ids[name] = str(row["id"])
        if hidden:
            self.remember_hidden("memories", row["id"])
        return row

    def _memories(self, store) -> None:
        commit = {"agentic_memory": {"kind": "agentic_memory_commit"}}
        self._memory(store, "memory_hidden", hidden=True, sensitivity="confidential")
        self._memory(store, "memory_shown", hidden=False, sensitivity="public")
        self._memory(
            store, "memory_derived", hidden=True, sensitivity="public",
            metadata={"source_id": self.ids["source_hidden"]}, floor=False,
        )
        self._memory(
            store, "commit_derived", hidden=True, sensitivity="public",
            metadata={**commit, "source_id": self.ids["source_hidden"]}, floor=False,
        )
        self._memory(
            store, "memory_cites_shown", hidden=True, sensitivity="confidential",
            metadata={"source_id": self.ids["source_shown"]},
        )
        self._memory(store, "candidate_hidden", hidden=True, sensitivity="confidential", status="candidate")
        self._memory(store, "candidate_shown", hidden=False, sensitivity="public", status="candidate")

    def _artifacts(self, store) -> None:
        derived = {
            **_EMPTY_DERIVATION,
            "sources": [self.ids["source_hidden"]],
            "counts": {**_EMPTY_DERIVATION["counts"], "sources": 1},
        }
        for name, hidden, sensitivity, metadata in (
            ("artifact_hidden", True, "confidential", {"derived_from": _EMPTY_DERIVATION}),
            ("artifact_shown", False, "public", {"derived_from": _EMPTY_DERIVATION}),
            ("artifact_derived", True, "public", {"derived_from": derived, "source_refs": [self.ids["source_hidden"]]}),
            (
                "artifact_cites_shown", True, "confidential",
                {"derived_from": _EMPTY_DERIVATION, "source_refs": [self.ids["source_shown"]]},
            ),
        ):
            with without_insert_floor():
                row = store.create_artifact(
                    {
                        "artifact_type": "daily_brief",
                        "title": self._text(f"{name}-title", hidden=hidden),
                        "content_markdown": self._text(f"{name}-body", hidden=hidden),
                        "domain": "project",
                        "sensitivity": sensitivity,
                        "metadata_json": metadata,
                    }
                )
            self.ids[name] = str(row["id"])
            if hidden:
                self.remember_hidden("generated_artifacts", row["id"])
            rating = store.create_artifact_quality_rating(
                {
                    "artifact_id": str(row["id"]),
                    "reviewer_id": "rater",
                    "verbosity": "right_sized",
                    "comments": self._text(f"{name}-rating", hidden=hidden),
                }
            )
            self.ids[f"{name}_rating"] = str(rating["id"])

    def _loops_and_projects(self, store) -> None:
        for name, hidden, sensitivity, metadata, floor in (
            ("loop_hidden", True, "confidential", {}, True),
            ("loop_shown", False, "public", {}, True),
            # Discovered from the confidential source and stored below its label.
            ("loop_derived", True, "public", {"discovered_by": "vnext_capture", "source_id": self.ids["source_hidden"]}, False),
            ("loop_cites_shown", True, "confidential", {"source_id": self.ids["source_shown"]}, True),
        ):
            record = {
                "title": self._text(f"{name}-title", hidden=hidden),
                "description": self._text(f"{name}-description", hidden=hidden),
                "domain": "project",
                "sensitivity": sensitivity,
                "metadata_json": metadata,
            }
            if "source_id" in metadata:
                record["source_id"] = metadata["source_id"]
            if floor:
                row = store.create_open_loop(record)
            else:
                with without_insert_floor():
                    row = store.create_open_loop(record)
            self.ids[name] = str(row["id"])
            if hidden:
                self.remember_hidden("open_loops", row["id"])
        for name, hidden, sensitivity in (("project_hidden", True, "confidential"), ("project_shown", False, "public")):
            row = store.create_project(
                {
                    "name": self._text(f"{name}-name", hidden=hidden),
                    "slug": f"{self.tag}-{name}".lower(),
                    "description": self._text(f"{name}-description", hidden=hidden),
                    "current_state": self._text(f"{name}-state", hidden=hidden),
                    "domain": "project",
                    "sensitivity": sensitivity,
                }
            )
            self.ids[name] = str(row["id"])
            if hidden:
                self.remember_hidden("projects", row["id"])
        for name, hidden, sensitivity in (("person_hidden", True, "confidential"), ("person_shown", False, "public")):
            row = store.create_person(
                {"name": self._text(f"{name}-name", hidden=hidden), "domain": "personal", "sensitivity": sensitivity}
            )
            self.ids[name] = str(row["id"])
            if hidden:
                self.remember_hidden("people", row["id"])

    def _graph(self, store) -> None:
        for name, hidden, memory in (("belief_hidden", True, "memory_hidden"), ("belief_shown", False, "memory_shown")):
            belief_id = str(uuid4())
            store.conn.execute(
                "INSERT INTO beliefs(id,user_id,memory_id,claim) VALUES (%s,%s,%s,%s)",
                (belief_id, self.harness.user_id, self.ids[memory], self._text(f"{name}-claim", hidden=hidden)),
            )
            self.ids[name] = belief_id
            if hidden:
                self.remember_hidden("beliefs", belief_id)
        for name, hidden, left, right in (
            ("edge_hidden", True, "memory_hidden", "memory_shown"),
            ("edge_shown", False, "memory_shown", "candidate_shown"),
        ):
            row = store.create_edge(
                {
                    "from_type": "memory",
                    "from_id": self.ids[left],
                    "to_type": "memory",
                    "to_id": self.ids[right],
                    "edge_type": "supports",
                    "explanation": self._text(f"{name}-explanation", hidden=hidden),
                }
            )
            self.ids[name] = str(row["id"])
            if hidden:
                self.remember_hidden("graph_edges", row["id"])
        for name, hidden in (("memory_hidden", True), ("memory_shown", False)):
            append_event(
                store,
                event_type="agent.policy_blocked",
                actor_type="agent",
                actor_id="sweep-reader",
                target_type="memory",
                target_id=self.ids[name],
                payload={"note": self._text(f"event-{name}", hidden=hidden)},
            )

    def _commits(self) -> None:
        """Commits through the product route: confidential and public, one of each confirmed afterwards."""

        for name, hidden, sensitivity, confirm in (
            ("commit_hidden", True, "confidential", False),
            ("commit_confirmed_hidden", True, "confidential", True),
            ("commit_shown", False, "public", False),
        ):
            status, body = self.admin_request(
                "POST",
                "/v0/vnext/memories/commit",
                {
                    "title": self._text(f"{name}-title", hidden=hidden),
                    "canonical_text": self._text(f"{name}-text", hidden=hidden),
                    "memory_type": "fact",
                    "domain": "project",
                    "sensitivity": sensitivity,
                    "confidence": 0.99,
                    "source_type": "agent",
                },
            )
            assert status in {200, 201}, (status, str(body)[:300])
            memory_id = str(body["memory"]["id"])
            self.ids[f"{name}_confirmation"] = str(body.get("confirmation_id") or "")
            if confirm:
                status, body = self.admin_request(
                    "POST",
                    "/v0/vnext/memories/confirm",
                    {"confirmation_id": body["confirmation_id"], "action": "confirm"},
                )
                assert status == 200, (status, str(body)[:300])
            self.ids[name] = memory_id
            if hidden:
                self.remember_hidden("memories", memory_id)

    def _dedupe_rows(self) -> None:
        """Hidden rows whose exact text a probe sends, to see whether a duplicate answer describes the stored row.

        These are the only sentinels a probe may put in a request. A response to the key that sent one is allowed to
        echo it, and so is a later read of the key's own copy, so the sweep does not look for them in any answer.
        """

        status, body = self.admin_request(
            "POST",
            "/v0/vnext/sources",
            {
                "raw_text": self._text("dedupe_source-raw", hidden=True),
                "title": self._text("dedupe_source-title", hidden=True),
                "domain": "project",
                "sensitivity": "confidential",
            },
        )
        assert status == 201, (status, str(body)[:300])
        self.ids["dedupe_source"] = str(body["source_id"])
        self.remember_hidden("sources", body["source_id"])
        status, body = self.admin_request(
            "POST",
            "/v0/vnext/memories/commit",
            {
                "title": self._text("dedupe_memory-title", hidden=True),
                "canonical_text": self._text("dedupe_memory-text", hidden=True),
                "memory_type": "fact",
                "domain": "project",
                "sensitivity": "confidential",
                "confidence": 0.99,
                "source_type": "agent",
            },
        )
        assert status in {200, 201}, (status, str(body)[:300])
        self.ids["dedupe_memory"] = str(body["memory"]["id"])
        self.remember_hidden("memories", body["memory"]["id"])
        with self.harness.store() as store:
            project = store.create_project(
                {
                    "name": self._text("dedupe_project-name", hidden=True),
                    "slug": f"{self.tag}-dedupe-project".lower(),
                    "description": self._text("dedupe_project-description", hidden=True),
                    "domain": "project",
                    "sensitivity": "confidential",
                }
            )
        self.ids["dedupe_project"] = str(project["id"])
        self.remember_hidden("projects", project["id"])

    def _queue_task(self) -> None:
        for name, hidden, sensitivity in (("task_hidden", True, "confidential"), ("task_shown", False, "public")):
            status, body = self.admin_request(
                "POST",
                "/v0/vnext/queue/tasks",
                {
                    "title": self._text(f"{name}-title", hidden=hidden),
                    "task_type": "summarize",
                    "instructions": self._text(f"{name}-instructions", hidden=hidden),
                    "domain": "project",
                    "sensitivity": sensitivity,
                },
            )
            assert status in {200, 201}, (status, str(body)[:300])
            task = body.get("task") or body
            self.ids[name] = str(task.get("id") or "")
            if hidden:
                self.remember_hidden("task_queue", self.ids[name])

    def _charter(self) -> None:
        status, body = self.admin_request(
            "PUT",
            "/v0/vnext/settings/brain-charter",
            {
                "content_markdown": self._text("charter-body", hidden=True),
                "sensitivity": "confidential",
                "owner_json": {"name": self._text("charter-owner", hidden=True)},
                "priorities_json": {"first": self._text("charter-priority", hidden=True)},
                "active_projects_json": [self._text("charter-project", hidden=True)],
                "autonomous_rules_json": [self._text("charter-rule", hidden=True)],
            },
        )
        assert status == 200, (status, str(body)[:300])
        self.remember_hidden("brain_charters", body["brain_charter"]["id"])

    def _insight_feedback(self) -> None:
        for name in ("artifact_hidden", "artifact_derived"):
            status, body = self.admin_request(
                "POST",
                f"/v0/vnext/artifacts/{self.ids[name]}/insight-feedback",
                {"useful_insight": "yes", "comments": self._text(f"{name}-feedback", hidden=True)},
            )
            assert status in {200, 201}, (status, str(body)[:300])

    def _connector_sync(self) -> None:
        """A confidential connector import, then one item that fails under a name of its own.

        The import is the connector's last captured item and sets its cursor: the path of the file is a title-like
        string and the source id is an id, both above the key's ceiling, so the health block of the connector screens
        must not show either to the trusted key. The failed item never became a row, so it has no label to judge. The
        connector status screen lists the id the importer gave it, and the security note names that as a known limit;
        the sweep does not look for it. It comes second and in a call of its own because a failed item in the same
        call would stop the cursor from moving.
        """

        status, body = self.admin_request(
            "POST",
            "/v0/vnext/connectors/local_folder/sync",
            {
                "default_sensitivity": "confidential",
                "items": [
                    {
                        "path": f"/vault/{self._text('synced-file', hidden=True)}.md",
                        "title": self._text("synced-title", hidden=True),
                        "text": self._text("synced-text", hidden=True),
                    },
                ],
            },
        )
        assert status in {200, 201, 207}, (status, str(body)[:300])
        self.ids["synced_source"] = str(body["source_ids"][0])
        self.remember_hidden("sources", self.ids["synced_source"])
        status, body = self.admin_request(
            "POST",
            "/v0/vnext/connectors/local_folder/sync",
            {"default_sensitivity": "confidential", "items": [{"path": "", "external_id": f"{self.tag}-failed-item-path"}]},
        )
        assert status in {200, 201, 207, 400}, (status, str(body)[:300])

    def _connector_sync_again(self) -> None:
        """The confidential file sent once more, after the scheduler runs, so the newest events are the connector's own.

        The workspace lists the 20 newest events a key may read. The scheduler runs above leave events a key may read
        (they name no hidden row), and they push the events of the first import out of that window. A sync that comes
        last puts its events (``connector.sync_started``, ``connector.state_updated`` and ``connector.sync_completed``,
        each holding the path of the confidential file as a cursor) inside it, so a screen that prints the events of the
        connector can be seen to print the path. The file is a duplicate, so the cursor and the last captured item stay.
        """

        status, body = self.admin_request(
            "POST",
            "/v0/vnext/connectors/local_folder/sync",
            {
                "default_sensitivity": "confidential",
                "items": [
                    {
                        "path": f"/vault/{self.text('synced-file')}.md",
                        "title": self.text("synced-title"),
                        "text": self.text("synced-text"),
                    },
                ],
            },
        )
        assert status in {200, 201, 207}, (status, str(body)[:300])

    def _scheduler_runs(self) -> None:
        """Runs by the admin key, so the scheduler history holds work done over the hidden rows."""

        for workflow in ("daily_brief", "weekly_synthesis", "open_loop_review", "memory_consolidation", "project_update_scan"):
            status, body = self.admin_request(
                "POST",
                f"/v0/vnext/scheduler/workflows/{workflow}/run-now",
                {"options": {"sensitivity_allowed": ["public", "internal", "private", "confidential", "unknown"]}},
            )
            assert status in {200, 201}, (workflow, status, str(body)[:300])


class StubVault:
    """Stands in for a vault where a probe table is built without a database, to read what each call declares."""

    tag = "stub"

    def __init__(self) -> None:
        self.ids = defaultdict(lambda: str(uuid4()))
        self.agent_ids = defaultdict(lambda: "agent")

    def text(self, name: str) -> str:
        return f"text:{name}"

    def hidden(self, name: str) -> str:
        return f"hidden:{name}"

    def shown(self, name: str) -> str:
        return f"shown:{name}"
