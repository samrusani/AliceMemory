"""A vault with a hidden and a visible row of every kind, for the operator route sweep.

Every hidden row carries a sentinel in each text field an operator screen could print (title, text, name, state,
explanation, comment). The visible rows carry a different marker, so a route that answers with nothing at all is told
apart from a route that answers with only what the caller may read.

``owner=True`` builds the same vault with no agent key at all, so every call is the owner's. A vault with keys is read
by the admin key (no limits, the control) and by the trusted key (the ceiling: public, internal, private, unknown).

``redacted_family=True`` adds a public memory that is redacted through the route before the vault is returned, and the rows
that hold or point at its words: a derived commit, a derived loop, a derived report and a derived project state (copies that
redaction contains with the reports), and four commits that quote the memory, one with the ref ``{"memory_id": ..., "quote":
...}``, one with the two entries ``"memory:<id>"`` and ``{"quote": ...}``, one held for review, two that write the words of the
memory as the name of a field (in the entry that names it and in the entry beside it), and two that asked for an inline
confirmation and were confirmed, one by the owner and one by the admin key (the lifecycle whose event holds the refs the commit was
sent with; the admin key's event is an agent event, which the workspace lists in its agent activity). Three more rows keep a structure
their writer chose that quotes the memory: a source made by the agent-output ingest (its ``source_refs``, in the metadata and in the raw
payload), a queued task that was processed (its allowed sources and scope, and the artifact the worker made), and a quality rating of
a shown artifact (its metadata). The memory is not above any ceiling, so it is hidden from a key with limits only because it is
redacted. The commits that quote it are public and readable,
and carry shown text of their own: a restricted key may read the commit and must not read the quote. It is off by default so the
tests that count the rows of the vault keep their numbers; ``tests/integration/test_saved_quote_memory_refs_postgres.py`` sweeps
every route with it on.

The derived project is created after the scheduler runs. The scheduler's project update scan takes the newest active project, and
an update candidate generated for a project that is itself derived from a redacted memory records the memory and the sources as
its inputs and not the project, so its title names the project; with ``contained_project_scanned=True`` the project exists when the
scan runs, and ``test_the_update_candidate_of_a_contained_project_is_contained`` records that gap as a failing test.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import uuid4

from alicebot_api.vnext_derived_labels import with_derived_from
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
    redacted_family: bool = False
    contained_project_scanned: bool = False
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
        if self.redacted_family:
            self._redacted_memory()
        self._queue_task()
        self._charter()
        self._insight_feedback()
        self._connector_sync()
        self._scheduler_runs()
        if self.redacted_family and not self.contained_project_scanned:
            self._redacted_project()
        self._connector_sync_again()
        if self.redacted_family:
            # Last, so their events are among the newest the workspace lists. The agent's goes first: the owner's confirmation appends
            # no agent event, so the agent's is still the newest of the agent activity, and the owner's the newest of the recent events.
            self._confirmed_quote_by_an_agent()
            self._confirmed_quote()
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

    def _redacted_memory(self) -> None:
        """A public memory, the rows that copy or quote it, and then the redaction.

        The words are the sentinel of ``memory_redacted-text``. The derived commit and the derived loop record the memory as an
        input and carry the sentinel of their own, as the copies a producer makes do. Three commits that quote it go through
        the commit route as an agent sends them; their quote is the memory's text, and their own title and text are shown text.
        A fourth, confirmed inline, is made last (``_confirmed_quote``).
        """

        with self.harness.store() as store:
            memory = self._memory(store, "memory_redacted", hidden=True, sensitivity="public")
            self._redacted_input = memory
            row = store.create_memory(
                {
                    "memory_key": f"{self.tag}-commit_of_redacted",
                    "memory_type": "semantic",
                    "canonical_text": self._text("commit_of_redacted-text", hidden=True),
                    "title": self._text("commit_of_redacted-title", hidden=True),
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": with_derived_from(
                        {"agentic_memory": {"kind": "agentic_memory_commit", "agent_identity": {"agent_id": "sweep"}}},
                        {"memories": [memory]},
                    ),
                }
            )
            self.ids["commit_of_redacted"] = str(row["id"])
            self.remember_hidden("memories", row["id"])
            loop = store.create_open_loop(
                {
                    "title": self._text("loop_of_redacted-title", hidden=True),
                    "description": self._text("loop_of_redacted-description", hidden=True),
                    "status": "open",
                    "domain": "project",
                    "sensitivity": "public",
                    "memory_id": str(memory["id"]),
                    "metadata_json": with_derived_from({}, {"memories": [memory]}),
                }
            )
            self.ids["loop_of_redacted"] = str(loop["id"])
            self.remember_hidden("open_loops", loop["id"])
            artifact = store.create_artifact(
                {
                    "artifact_type": "daily_brief",
                    "title": self._text("artifact_of_redacted-title", hidden=True),
                    "content_markdown": self._text("artifact_of_redacted-body", hidden=True),
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": with_derived_from({"workflow": "daily_brief"}, {"memories": [memory]}),
                }
            )
            self.ids["artifact_of_redacted"] = str(artifact["id"])
            self.remember_hidden("generated_artifacts", artifact["id"])
            if self.contained_project_scanned:
                self._redacted_project_row(store, memory)
        words = self.text("memory_redacted-text")
        cited = self.ids["memory_redacted"]
        # The last commit is held for review (its confidence is low), so the workspace lists it among the review memories.
        for name, refs, confidence in (
            ("commit_quotes_redacted", [{"memory_id": cited, "quote": words}], 0.99),
            ("commit_quotes_redacted_typed", [f"memory:{cited}", {"quote": words}], 0.99),
            ("commit_pending_quotes_redacted", [{"memory_id": cited, "quote": words}], 0.3),
            # The words of the memory written as the name of a field, which a restricted reader must not be shown either.
            ("commit_keys_redacted", [{"memory_id": cited, words: None}], 0.99),
            ("commit_keys_redacted_typed", [f"memory:{cited}", {words: 1}], 0.99),
        ):
            status, body = self.admin_request(
                "POST",
                "/v0/vnext/memories/commit",
                {
                    "title": self._text(f"{name}-title", hidden=False),
                    "canonical_text": self._text(f"{name}-text", hidden=False),
                    "memory_type": "fact",
                    "domain": "project",
                    "sensitivity": "public",
                    "confidence": confidence,
                    "source_type": "agent",
                    "source_refs": refs,
                },
            )
            assert status in {200, 201}, (status, str(body)[:300])
            self.ids[name] = str(body["memory"]["id"])
        self._ingested_quote(cited, words)
        self._queued_quote(cited, words)
        self._rated_quote(cited, words)
        status, body = self.admin_request(
            "POST", "/v0/vnext/memories/redact", {"memory_id": cited, "reason": "sweep"}
        )
        assert status == 200, (status, str(body)[:300])

    def _ingested_quote(self, cited: str, words: str) -> None:
        """A source made by the agent-output ingest, whose ``source_refs`` quote the memory (and write its words as a field name).

        The ingest keeps the refs it was sent on the source it makes, twice: in ``metadata_json.source_refs`` and in
        ``metadata_json.raw_payload.source_refs``. Every route that returns a source returns them, so a restricted key must be shown
        the ids and the quote marker and none of the words once the memory is redacted.
        """

        status, body = self.admin_request(
            "POST",
            "/v0/vnext/agents/ingest-output",
            {
                "agent_id": self.agent_ids.get("admin_agent", "sweep-ingest"),
                "title": self._text("ingest_quotes_redacted-title", hidden=False),
                "content": self._text("ingest_quotes_redacted-text", hidden=False),
                "domain": "project",
                "sensitivity": "public",
                "source_refs": [{"memory_id": cited, "quote": words, words: True}, f"memory:{cited}", {"quote": words}],
            },
        )
        assert status in {200, 201}, (status, str(body)[:300])
        self.ids["ingest_quotes_redacted"] = str(body["source_id"])
        self.ids["ingest_quotes_redacted_artifact"] = str(body["artifact_id"])

    def _queued_quote(self, cited: str, words: str) -> None:
        """A queued task whose allowed sources quote the memory and whose scope writes its words as a field name, then processed.

        The worker prints both structures into the artifact it makes, and the workspace lists the task as stored. The artifact is
        derived from the memory the task names, so redacting the memory takes it out of every route of a key with limits; the task
        row is held to the reader. It is the only task queued when it is processed, so the worker claims it.
        """

        status, body = self.admin_request(
            "POST",
            "/v0/vnext/queue/tasks",
            {
                "title": self._text("task_quotes_redacted-title", hidden=False),
                "task_type": "summarize",
                "instructions": self._text("task_quotes_redacted-instructions", hidden=False),
                "domain": "project",
                "sensitivity": "public",
                "scope_json": {words: cited},
                "allowed_sources_json": [{"memory_id": cited, "quote": words, words: True}],
            },
        )
        assert status in {200, 201}, (status, str(body)[:300])
        task = body.get("task") or body
        self.ids["task_quotes_redacted"] = str(task["id"])
        status, body = self.admin_request("POST", "/v0/vnext/queue/process-next", {})
        assert status == 200 and body["status"] == "completed", (status, str(body)[:300])
        assert body["task_id"] == self.ids["task_quotes_redacted"], "the worker claimed the task of this vault"
        self.ids["task_quotes_redacted_artifact"] = str(body["artifact_id"])
        self.remember_hidden("generated_artifacts", body["artifact_id"])

    def _rated_quote(self, cited: str, words: str) -> None:
        """A quality rating of a shown artifact whose metadata quotes the memory and writes its words as a field name. The artifact
        is not derived from the memory, so redacting the memory does not contain it, and the rating is held to the reader."""

        status, body = self.admin_request(
            "POST",
            f"/v0/vnext/artifacts/{self.ids['artifact_shown']}/quality-ratings",
            {"verbosity": "right_sized", "metadata_json": {"memory_id": cited, "quote": words, words: 1}},
        )
        assert status in {200, 201}, (status, str(body)[:300])
        self.ids["rating_quotes_redacted"] = str(body["id"])

    def _redacted_project_row(self, store, memory) -> None:
        project = store.create_project(
            {
                "name": self._text("project_of_redacted-name", hidden=True),
                "slug": f"{self.tag}-project_of_redacted".lower(),
                "description": self._text("project_of_redacted-description", hidden=True),
                "current_state": self._text("project_of_redacted-state", hidden=True),
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": with_derived_from({}, {"memories": [memory]}),
            }
        )
        self.ids["project_of_redacted"] = str(project["id"])
        self.remember_hidden("projects", project["id"])

    def _redacted_project(self) -> None:
        """A project whose state was copied from the redacted memory, made after the scheduler ran (see the module docstring)."""

        with self.harness.store() as store:
            self._redacted_project_row(store, self._redacted_input)

    def _confirmed_quote(self) -> None:
        """A commit that quotes the redacted memory, asks for an inline confirmation and is confirmed by the owner.

        The confirmation appends a ``memory.updated`` event whose payload holds the refs and the excerpt the commit was sent with,
        and the feeds of events (the workspace, the source trace) admit that event by the commit it is about, which a restricted
        key may read. It is made after the redaction, so the quote it carries is the words of a memory that is already redacted,
        and it is the newest row of the vault, so its events are inside the window of the workspace's recent events.
        """

        from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService

        words = self.text("memory_redacted-text")
        # Two transactions, as the commit route and the confirm route are: a strict test refuses the graph lock after the label lock,
        # and the commit takes the label lock.
        with self.harness.store() as store:
            asked = VNextMemoryCommitService(store, defer_embeddings=True).commit(
                identity=None,
                request=MemoryCommitRequest(
                    user_id=str(self.harness.user_id),
                    title=self._text("commit_confirmed_quotes_redacted-title", hidden=False),
                    canonical_text=self._text("commit_confirmed_quotes_redacted-text", hidden=False),
                    memory_type="semantic",
                    domain="project",
                    sensitivity="public",
                    confidence=0.6,
                    source_refs=({"memory_id": self.ids["memory_redacted"], "quote": words, words: None},),
                    conversation_excerpt=words,
                ),
            )
        assert asked["status"] == "confirmation_required", str(asked)[:300]
        with self.harness.store() as store:
            VNextMemoryCommitService(store, defer_embeddings=True).confirm(
                identity=None, confirmation_id=asked["memory"]["confirmation_id"], action="confirm"
            )
        self.ids["commit_confirmed_quotes_redacted"] = str(asked["memory"]["id"])

    def _confirmed_quote_by_an_agent(self) -> None:
        """A commit that quotes the redacted memory, asks for an inline confirmation and is confirmed by the admin key.

        The confirmation appends the same ``memory.updated`` event as the owner's, with the actor an agent, and the workspace lists
        the events an agent caused in a feed of their own (``agent_activity.recent_events``, the newest fifty). That feed admits the
        event by the commit it is about, which a restricted key may read, and the payload holds the refs and the excerpt the commit
        was sent with. It is made just before the owner's confirmation, which appends no agent event, so its event is the newest of that
        feed; an owner vault has no agent, and the feed holds nothing of it.
        """

        words = self.text("memory_redacted-text")
        status, body = self.admin_request(
            "POST",
            "/v0/vnext/memories/commit",
            {
                "title": self._text("commit_agent_confirmed_quotes_redacted-title", hidden=False),
                "canonical_text": self._text("commit_agent_confirmed_quotes_redacted-text", hidden=False),
                "memory_type": "fact",
                "domain": "project",
                "sensitivity": "public",
                "confidence": 0.6,
                "source_type": "agent",
                "source_refs": [{"memory_id": self.ids["memory_redacted"], "quote": words, words: True}],
                "conversation_excerpt": words,
            },
        )
        assert status in {200, 201} and body["status"] == "confirmation_required", (status, str(body)[:300])
        status, confirmed = self.admin_request(
            "POST", "/v0/vnext/memories/confirm", {"confirmation_id": body["memory"]["confirmation_id"], "action": "confirm"}
        )
        assert status == 200, (status, str(confirmed)[:300])
        self.ids["commit_agent_confirmed_quotes_redacted"] = str(body["memory"]["id"])
        if self.owner:
            return
        with self.harness.store() as store:
            feed = store.list_agent_events(limit=50)
        assert any(
            event["event_type"] == "memory.updated" and str(event["target_id"]) == self.ids["commit_agent_confirmed_quotes_redacted"]
            for event in feed
        ), "the confirmation is an agent event inside the newest fifty, so the workspace lists it in its agent activity"

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
