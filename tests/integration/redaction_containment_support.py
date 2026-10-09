"""A vault whose reports were made from a memory that is redacted afterwards, and every door a key reads them through.

Every producer runs through the mounted application with an admin key. A memory that holds a sentinel string is one of
their inputs. Then the memory is redacted through the redact route. The reports and cards made from it keep what they
copied, so the tests ask which key can still read it.

The sentinel appears only in the text of the redacted memory. A body that holds it, at any depth, has leaked the memory.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from uuid import uuid4

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError, MCPToolNotFoundError

# profile name -> (permission profile, bound to the alpha project)
PROFILES = {
    "admin": ("admin_agent", False),
    "trusted": ("trusted_local_agent", False),
    "read_only": ("read_only_agent", False),
    "memory_proposal": ("memory_proposal_agent", False),
    "trusted_bound": ("trusted_local_agent", True),
    "alpha_only": ("project_scoped_agent", True),
    "admin_bound": ("admin_agent", True),
}
RESTRICTED = tuple(name for name in PROFILES if name != "admin")
PRODUCERS = (
    "daily", "weekly", "connections", "contradictions", "open_loop_review", "project_update", "consolidation", "staleness",
)
OPTIONS = {
    "generated_for": "2026-10-05", "source_limit": 50, "memory_limit": 50, "artifact_limit": 50,
    "open_loop_limit": 50, "reference_time": "2026-10-05T12:00:00Z", "max_items": 50,
    "discover_open_loops": True, "create_candidate_memories": True,
}
GAMES = ("Hollow Knight", "Stardew Valley", "Celeste", "Hades", "Tunic")
# The memory that gets redacted. The others stay readable.
REDACTED_INDEX = 3
# Queries that reach the redacted text through its other words. A query never holds the sentinel, because a door echoes it.
QUERIES = ("Atlas played hours", "Atlas launch games", "MEMORY3 Atlas", "Atlas contradicts the belief")


@dataclass
class Response:
    key_name: str
    door: str
    status: object
    body: object

    @property
    def text(self) -> str:
        return json.dumps(self.body, default=str, sort_keys=True)


def generate(
    h, producer: str, alpha: str, key: str, *, expect: int = 201, options: dict | None = None, projects: bool = True
) -> dict:
    """Run one producer through its normal route and return the report (or the memory run record)."""
    payload = {"scope": {"projects": [alpha] if projects else []}, "options": {**OPTIONS, **(options or {})}}
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
    assert status == expect, (producer, status, body)
    return body.get("artifact", body)


class World:
    """Sources, memories, a belief and loops in one project, then every producer, then one memory redacted."""

    def __init__(self, h, *, redact: bool = True, accept_update: bool = True) -> None:
        self.h = h
        self.alpha = str(uuid4())
        self.sentinel = f"ZQXSENTINEL{uuid4().hex[:10]}"
        with h.store() as store:
            store.create_project(
                {"id": self.alpha, "name": "Atlas", "slug": "atlas", "domain": "project", "sensitivity": "public"}
            )
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
            for i, game in enumerate(GAMES):
                label = self.sentinel if i == REDACTED_INDEX else game
                text = f"Atlas played {label} for {25 + i * 30} hours. MEMORY{i}"
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
            contra_text = f"Atlas does not prefer launch games. {self.sentinel} contradicts the belief."
            self.contra = store.create_memory(
                {
                    "memory_key": "alpha.contra", "memory_type": "episode", "title": contra_text,
                    "canonical_text": contra_text, "summary": contra_text, "value": {"text": contra_text},
                    "status": "active", "domain": "project", "sensitivity": "public", "created_at": "2026-10-05T09:00:00Z",
                    "metadata_json": {"project_scope": [self.alpha]},
                }
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
        self.reports = {producer: generate(h, producer, self.alpha, self.admin) for producer in PRODUCERS}
        self.redacted = str(self.memories[REDACTED_INDEX]["id"])
        self.redacted_ids = [self.redacted, str(self.contra["id"])]
        self.accepted_update = None
        if accept_update:
            # The update is edited before it is accepted, and the state it stores copies the memory that is redacted later.
            state = f"Atlas state: {self.memories[REDACTED_INDEX]['canonical_text']}"
            status, body, _ = h.request(
                "POST", f"/v0/vnext/projects/update-candidates/{self.reports['project_update']['id']}/review",
                payload={"action": "edit", "edited_current_state": state}, key=self.admin,
            )
            assert status == 200, body
            self.accepted_update = body
        self.chain = self._build_chain()
        if redact:
            self.redact()

    def _build_chain(self) -> dict[str, str]:
        """Rows that never recorded the redacted memory and only read a report that did: reports of reports.

        The daily brief is promoted to a memory (the copy keeps the report's text), and two reports are made from the report
        and from each other. Each is stored while the brief is healthy, so only the read-time check can restrict them later.
        """
        daily = self.reports["daily"]
        status, body, _ = self.h.request(
            "POST", f"/v0/vnext/artifacts/{daily['id']}/review", payload={"action": "promote"}, key=self.admin
        )
        assert status == 200, body
        chain = {"promoted": str(body["promoted_memory_id"])}
        with self.h.store() as store:
            second = store.create_artifact(
                {
                    "artifact_type": "weekly_synthesis", "title": "Chain two", "content_markdown": f"Chain two reads the brief. {daily['content_markdown']}",
                    "domain": "project", "sensitivity": "public",
                    "metadata_json": with_derived_from(
                        {"workflow": "weekly_synthesis", "project_scope": [self.alpha]}, {"artifacts": [daily]}
                    ),
                }
            )
            third = store.create_artifact(
                {
                    "artifact_type": "weekly_synthesis", "title": "Chain three", "content_markdown": "Chain three reads chain two.",
                    "domain": "project", "sensitivity": "public",
                    "metadata_json": with_derived_from(
                        {"workflow": "weekly_synthesis", "project_scope": [self.alpha]}, {"artifacts": [second]}
                    ),
                }
            )
        chain["two"], chain["three"] = str(second["id"]), str(third["id"])
        return chain

    def redact(self) -> None:
        for memory_id in self.redacted_ids:
            status, body, _ = self.h.request(
                "POST", "/v0/vnext/memories/redact", payload={"memory_id": memory_id, "reason": "r"}, key=self.admin
            )
            assert status == 200, body

    def keys(self) -> dict[str, str]:
        return {
            name: self.h.key(permission, project=self.alpha if bound else None)
            for name, (permission, bound) in PROFILES.items()
        }

    def stored_text(self) -> dict[str, str]:
        """Every stored report, card and project, as text, for the keyless reader of the database."""
        found: dict[str, str] = {}
        with self.h.store() as store, store.conn.cursor() as cur:
            for table in ("generated_artifacts", "memories", "open_loops", "projects"):
                cur.execute(f"SELECT id::text AS id, row_to_json(t)::text AS body FROM {table} t")  # closed table names
                for row in cur.fetchall():
                    if self.sentinel in row["body"]:
                        found[f"{table}:{row['id']}"] = row["body"]
        return found


def _get(h, responses, key_name, label, path, key):
    status, body, _ = h.request("GET", path, key=key)
    responses.append(Response(key_name, label, status, body))
    return status, body


def _post(h, responses, key_name, label, path, key, payload):
    status, body, _ = h.request("POST", path, key=key, payload=payload)
    responses.append(Response(key_name, label, status, body))
    return status, body


def sweep(h, world: World, monkeypatch, keys: dict[str, str], *, only: tuple[str, ...] | None = None) -> list[Response]:
    """Every response of every door that returns a report or a memory, for each key given."""
    responses: list[Response] = []
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id FROM memories ORDER BY created_at, id")
        memory_ids = [row["id"] for row in cur.fetchall()]
        cur.execute("SELECT id::text AS id FROM generated_artifacts ORDER BY created_at, id")
        artifact_ids = [row["id"] for row in cur.fetchall()]
        cur.execute("SELECT id::text AS id FROM beliefs ORDER BY id")
        belief_ids = [row["id"] for row in cur.fetchall()]
    for key_name, key in keys.items():
        if only is not None and key_name not in only:
            continue
        for path, label in (
            ("/v0/vnext/artifacts", "artifact list"),
            ("/v0/vnext/workspace", "workspace"),
            ("/v0/vnext/context-tree", "context tree"),
            ("/v0/vnext/dogfooding", "dogfooding"),
            ("/v0/vnext/projects", "project list"),
            (f"/v0/vnext/projects/{world.alpha}/dashboard", "project dashboard"),
            ("/v0/vnext/memories/recent-commits", "recent commits"),
            ("/v0/vnext/scheduler/runs", "scheduler runs"),
            ("/v0/vnext/scheduler/status", "scheduler status"),
            ("/v0/vnext/scheduler/failures", "scheduler failures"),
            ("/v0/vnext/quality-evals", "quality evals"),
            ("/v0/vnext/agents/policy-telemetry", "policy telemetry"),
            ("/v0/vnext/doctor", "doctor"),
        ):
            _get(h, responses, key_name, label, path, key)
        for artifact_id in artifact_ids:
            _get(h, responses, key_name, "artifact get", f"/v0/vnext/artifacts/{artifact_id}", key)
            _get(h, responses, key_name, "artifact trace", f"/v0/vnext/traces/artifacts/{artifact_id}", key)
        for source in world.sources:
            _get(h, responses, key_name, "source trace", f"/v0/vnext/traces/sources/{source['id']}", key)
            _get(h, responses, key_name, "source get", f"/v0/vnext/sources/{source['id']}", key)
        for memory_id in memory_ids:
            _get(h, responses, key_name, "memory audit", f"/v0/vnext/memories/{memory_id}/audit", key)
            _get(h, responses, key_name, "graph neighborhood", f"/v0/vnext/graph/neighborhood/{memory_id}", key)
        for belief_id in belief_ids:
            _get(h, responses, key_name, "belief state", f"/v0/vnext/beliefs/{belief_id}/state", key)
        for query in QUERIES:
            _post(h, responses, key_name, "context pack", "/v0/vnext/context-packs", key, {"query": query})
    for key_name, key in keys.items():
        if only is not None and key_name not in only:
            continue
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
        calls: list[tuple[str, str, dict[str, object]]] = []
        for query in QUERIES:
            calls.append(("alice_recall", "recall", {"query": query, "limit": 50}))
            calls.append(("alice_context_pack", "context pack", {"query": query}))
            calls.append(("alice_vnext_context_pack", "context pack", {"query": query}))
        calls += [
            ("alice_resume", "resume", {}),
            ("alice_recent_decisions", "recent decisions", {}),
            ("alice_vnext_recent_decisions", "recent decisions", {}),
            ("alice_vnext_recent_changes", "recent changes", {}),
            ("alice_open_loops", "open loops", {"action": "list"}),
            ("alice_vnext_open_loops", "open loops", {}),
            ("alice_memory_review", "review list", {"status": "all", "limit": 100}),
            ("alice_vnext_review_items", "review list", {"status": "all", "limit": 100}),
            ("alice_vnext_context_tree", "context tree", {"include_events": True}),
            ("alice_vnext_project_dashboard", "project dashboard", {"project_id": world.alpha}),
            ("alice_project_dashboard", "project dashboard", {"project_id": world.alpha}),
            ("alice_vnext_scheduler_status", "scheduler status", {}),
        ]
        for memory_id in memory_ids:
            calls.append(("alice_explain", "explain", {"memory_id": memory_id}))
            calls.append(("alice_memory_review", "review detail", {"review_item_id": memory_id}))
            calls.append(("alice_vnext_memory_audit", "memory audit", {"memory_id": memory_id}))
        for artifact_id in artifact_ids:
            calls.append(("alice_vnext_artifact_get", "artifact get", {"artifact_id": artifact_id}))
            calls.append(("alice_artifact_inspect", "artifact inspect", {"artifact_id": artifact_id}))
        for belief_id in belief_ids:
            calls.append(("alice_belief_state", "belief state", {"belief_id": belief_id}))
        for tool, label, arguments in calls:
            try:
                result = call_mcp_tool(context, name=tool, arguments=arguments)
            except (MCPToolError, MCPToolNotFoundError) as exc:
                responses.append(Response(key_name, f"{tool}:{label}", "tool-error", str(exc)))
                continue
            responses.append(Response(key_name, f"{tool}:{label}", "tool-ok", result))
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    return responses


def leaks(responses: list[Response], needle: str) -> list[tuple[str, str, object]]:
    return [(item.key_name, item.door, item.status) for item in responses if needle in item.text]


# --- ground truth from the stored rows, written without the label kernel ---------------------------------------------


def _ids(*values) -> set[str]:
    found: set[str] = set()
    for value in values:
        if isinstance(value, str):
            found.add(value.lower())
        elif isinstance(value, list):
            for item in value:
                found |= _ids(item)
    return found


def recorded_inputs(metadata: dict, value: dict) -> set[str]:
    """The ids a stored row names as the rows it was made from, read from the lists the producers write."""
    derived = metadata.get("derived_from") if isinstance(metadata.get("derived_from"), dict) else {}
    found = _ids(*(derived.get(name) for name in ("sources", "memories", "open_loops", "artifacts", "beliefs")))
    consolidation = metadata.get("consolidation") if isinstance(metadata.get("consolidation"), dict) else {}
    rollup = value.get("rollup") if isinstance(value.get("rollup"), dict) else {}
    return found | _ids(
        metadata.get("source_artifact_id"), consolidation.get("cluster_member_ids"), rollup.get("member_ids"), value.get("artifact_id")
    )


@dataclass
class Stored:
    kind: str
    id: str
    redacted: bool
    inputs: set[str]
    status: str
    deleted: bool = False


def stored_rows(h) -> dict[str, Stored]:
    rows: dict[str, Stored] = {}
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute("SELECT id::text AS id, metadata_json, value, status, deleted_at IS NOT NULL AS deleted FROM memories")
        for row in cur.fetchall():
            meta = row["metadata_json"] or {}
            rows[row["id"]] = Stored("memory", row["id"], meta.get("redacted") is True, recorded_inputs(meta, row["value"] or {}), row["status"], row["deleted"])
        cur.execute("SELECT id::text AS id, metadata_json, status FROM generated_artifacts")
        for row in cur.fetchall():
            meta = row["metadata_json"] or {}
            rows[row["id"]] = Stored("artifact", row["id"], meta.get("redacted") is True, recorded_inputs(meta, {}), row["status"])
        cur.execute("SELECT id::text AS id, metadata_json FROM projects")
        for row in cur.fetchall():
            meta = row["metadata_json"] or {}
            rows[row["id"]] = Stored("project", row["id"], False, recorded_inputs(meta, {}), "")
    return rows


def contained_ids(h) -> set[str]:
    """Rows that are not redacted and recorded a redacted row, or a contained row, as an input."""
    rows = stored_rows(h)
    memo: dict[str, bool] = {}

    def walk(row_id: str) -> bool:
        if row_id in memo:
            return memo[row_id]
        row = rows.get(row_id)
        if row is None or row.redacted:
            memo[row_id] = False
            return False
        memo[row_id] = False  # a cycle adds nothing
        memo[row_id] = any((rows.get(item) is not None and rows[item].redacted) or walk(item) for item in row.inputs if item in rows)
        return memo[row_id]

    return {row_id for row_id in rows if walk(row_id)}


def own_ids(body) -> set[str]:
    """Every value that stands as the ``id`` of an object anywhere in a body."""
    found: set[str] = set()
    if isinstance(body, dict):
        for key, child in body.items():
            if key == "id" and isinstance(child, str):
                found.add(child.lower())
            found |= own_ids(child)
    elif isinstance(body, list):
        for child in body:
            found |= own_ids(child)
    return found


def owner_artifact(h, artifact_id: str) -> tuple[int, object]:
    """The artifact route as the owner calls it: with no key. Once keys exist the middleware refuses a keyless request, so the
    handler runs in process, as the owner's own commands run it."""
    from uuid import UUID

    from alicebot_api.routers import vnext_review

    result = vnext_review.get_vnext_artifact(UUID(str(artifact_id)), h.user_id, None)
    return result.status_code, json.loads(result.body)


@contextmanager
def keyless_owner(h):
    """Revoke every agent key for the block, so a keyless request is the owner's, and put them back after."""
    with h.store() as store:
        store.conn.execute("UPDATE agent_api_keys SET revoked_at = now() WHERE revoked_at IS NULL")
    try:
        yield
    finally:
        with h.store() as store:
            store.conn.execute("UPDATE agent_api_keys SET revoked_at = NULL WHERE revoked_at IS NOT NULL")
