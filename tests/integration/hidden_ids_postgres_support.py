"""A stored vault with rows hidden for every reason, and every door that takes a row id.

The tests that use this ask one question: what does a key get when it names the id of a row it may not read? The answer
must not be more than the answer for an id that does not exist, and a write must leave every stored table as it was.

Each row is hidden for one reason. A reason hides the row from some profiles and not from others, and ``HIDDEN_FOR``
says which. A forgotten memory is not hidden: explain and review still show its history to a caller who may read its
labels, so it is a control and not a hidden row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from uuid import UUID, uuid4

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.vnext_label_writes import without_insert_floor

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
EVERYONE = frozenset(PROFILES)
ABOVE_THE_SENSITIVITY_CEILING = frozenset({"trusted", "read_only", "memory_proposal", "trusted_bound", "alpha_only"})
# Reason a row is hidden -> the profiles that may not read it.
HIDDEN_FOR = {
    "confidential": ABOVE_THE_SENSITIVITY_CEILING,
    "private": frozenset({"read_only"}),
    "health": frozenset({"read_only", "memory_proposal", "alpha_only"}),
    "beta": frozenset({"trusted_bound", "alpha_only", "admin_bound"}),
    "unverified": EVERYONE - {"admin"},
    "archived": EVERYONE,
    "forgotten": frozenset(),
}
KINDS = ("source", "memory", "loop", "artifact")
# A source is always an original, so it is never unverified. Only a source can be archived and a memory forgotten.
KINDS_FOR = {"archived": ("source",), "forgotten": ("memory",), "unverified": ("memory", "loop", "artifact")}
# reason -> (sensitivity, domain, project)
REASONS = {
    "visible": ("public", "project", "alpha"),
    "confidential": ("confidential", "project", "alpha"),
    "private": ("private", "project", "alpha"),
    "health": ("public", "health", "alpha"),
    "beta": ("public", "project", "beta"),
    "unverified": ("public", "project", "alpha"),
    "archived": ("public", "project", "alpha"),
    "forgotten": ("public", "project", "alpha"),
}
TABLES = (
    "sources", "source_chunks", "memories", "memory_revisions", "open_loops", "generated_artifacts", "projects", "beliefs",
    "graph_edges", "provenance_links", "artifact_quality_ratings", "task_queue",
)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_HASH = re.compile(r"\b[0-9a-f]{64}\b")


def normalize(value: object) -> str:
    """The answer with every generated id, instant and digest replaced, so two answers compare by what they say."""
    text = json.dumps(value, sort_keys=True, default=str)
    return _HASH.sub("<HASH>", _TIME.sub("<TIME>", _UUID.sub("<ID>", text)))


@dataclass
class Answer:
    status: object
    body: str

    def __repr__(self) -> str:
        return f"{self.status} {self.body[:600]}"

    @property
    def refused(self) -> bool:
        """A refusal is an HTTP 400, 403, 404 or 422, or a tool error. Anything else did the work or answered."""
        return str(self.status) in {"400", "403", "404", "422"} or str(self.status).startswith("tool-error")


@dataclass
class Vault:
    """Rows of every kind, each hidden for one reason, plus the readable rows around them."""

    h: object
    alpha: str
    beta: str
    ids: dict[tuple[str, str], str] = field(default_factory=dict)
    secrets: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def hidden_ids(self, profile: str) -> dict[tuple[str, str], str]:
        return {key: value for key, value in self.ids.items() if profile in HIDDEN_FOR.get(key[1], frozenset())}

    def readable_ids(self, profile: str) -> dict[tuple[str, str], str]:
        return {key: value for key, value in self.ids.items() if profile not in HIDDEN_FOR.get(key[1], frozenset())}


def build_vault(h) -> Vault:
    alpha, beta = str(uuid4()), str(uuid4())
    with h.store() as store:
        for identifier, name in ((alpha, "alpha"), (beta, "beta")):
            store.create_project({"id": identifier, "name": f"project-{name}-{identifier[:8]}", "slug": f"p-{identifier}"})
    vault = Vault(h, alpha, beta)
    for reason, (sensitivity, domain, project) in REASONS.items():
        scope = [alpha if project == "alpha" else beta]
        tag = reason.upper()
        with h.store() as store:
            for kind in KINDS_FOR.get(reason, KINDS):
                words = [f"TITLE-{kind}-{tag}", f"TEXT-{kind}-{tag}"]
                if kind == "source":
                    row = store.create_source(
                        {
                            "source_type": "note", "title": words[0], "content_hash": str(uuid4()), "domain": domain,
                            "sensitivity": sensitivity,
                            "metadata_json": {"project_scope": scope, "raw_text": f"RAW-{words[1]}"},
                        }
                    )
                    store.create_source_chunk({"source_id": str(row["id"]), "chunk_index": 0, "text": f"CHUNK-{words[1]}"})
                    words += [f"RAW-{words[1]}", f"CHUNK-{words[1]}"]
                elif kind == "memory":
                    metadata: dict[str, object] = {"project_scope": scope}
                    if reason == "unverified":
                        metadata["source_id"] = str(uuid4())
                    with without_insert_floor():
                        row = store.create_memory(
                            {
                                "memory_key": str(uuid4()), "title": words[0], "canonical_text": words[1],
                                "summary": f"SUMMARY-{words[1]}", "status": "active", "domain": domain,
                                "sensitivity": sensitivity, "metadata_json": metadata,
                            }
                        )
                    words.append(f"SUMMARY-{words[1]}")
                elif kind == "loop":
                    metadata = {"project_scope": scope}
                    if reason == "unverified":
                        metadata.update({"discovered_by": "vnext_daily_brief", "source_id": str(uuid4())})
                    with without_insert_floor():
                        row = store.create_open_loop(
                            {
                                "title": words[0], "description": words[1], "domain": domain, "sensitivity": sensitivity,
                                "metadata_json": metadata,
                            }
                        )
                else:
                    metadata = {"project_scope": scope}
                    if reason != "unverified":
                        metadata["derived_from"] = {
                            "v": 1, "sources": [], "memories": [], "open_loops": [], "artifacts": [], "beliefs": [],
                            "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                        }
                    with without_insert_floor():
                        row = store.create_artifact(
                            {
                                "artifact_type": "daily_brief", "title": words[0], "content_markdown": words[1],
                                "domain": domain, "sensitivity": sensitivity, "metadata_json": metadata,
                            }
                        )
                vault.ids[(kind, reason)] = str(row["id"])
                vault.secrets[(kind, reason)] = words
    # A belief behind each of two memories, and a candidate edge from the visible source to each of them.
    with h.store() as store:
        for reason in ("visible", "confidential"):
            belief = store.create_belief(
                {"memory_id": vault.ids[("memory", reason)], "claim": f"CLAIM-{reason}", "confidence": 0.9}
            )
            vault.ids[("belief", reason)] = str(belief["id"])
            vault.secrets[("belief", reason)] = [f"CLAIM-{reason}"]
            edge = store.create_edge(
                {
                    "from_type": "source", "from_id": vault.ids[("source", "visible")], "to_type": "memory",
                    "to_id": vault.ids[("memory", reason)], "edge_type": "similar_to", "confidence": 0.9,
                    "explanation": f"EDGE-{reason}", "created_by": "vnext_connection_finder",
                    "metadata_json": {"status": "candidate", "candidate": True},
                }
            )
            vault.ids[("edge", reason)] = str(edge["id"])
    # An archived source is read by nobody. A forgotten memory stays readable by its history.
    with h.store() as store:
        store.delete_source(source_id=vault.ids[("source", "archived")], actor_type="user")
    status, body, _ = h.request(
        "POST", "/v0/vnext/memories/forget", payload={"memory_id": vault.ids[("memory", "forgotten")], "reason": "synthetic"}
    )
    assert status == 200, body
    return vault


def snapshot(h) -> dict[str, list[str]]:
    """Every table that holds a row a door can change, as text, so two snapshots compare."""
    result = {}
    with h.store() as store, store.conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SELECT row_to_json(x) FROM {table} x")  # closed table names
            result[table] = sorted(json.dumps(row["row_to_json"], sort_keys=True, default=str) for row in cur.fetchall())
    return result


# --- the doors -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Door:
    name: str
    write: bool
    call: object  # (env, key, row_id) -> Answer


class Env:
    """Calls the mounted application over HTTP and the tool registry in process, with one key."""

    def __init__(self, h, monkeypatch, database_url: str, export_dir: str = "") -> None:
        self.h = h
        self.monkeypatch = monkeypatch
        self.database_url = database_url
        self.export_dir = export_dir

    def http(self, method: str, path: str, key, payload=None) -> Answer:
        try:
            status, body, _headers = self.h.request(method, path, payload=payload, key=key)
        except Exception as exc:  # an unhandled route error reaches the caller as a server error
            return Answer(f"raised:{type(exc).__name__}", normalize(str(exc)))
        return Answer(status, normalize(body))

    def tool(self, key, name: str, arguments: dict[str, object]) -> Answer:
        self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if key:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        context = MCPRuntimeContext(database_url=self.database_url, user_id=UUID(str(self.h.user_id)))
        try:
            result = call_mcp_tool(context, name=name, arguments=arguments)
        except MCPToolError as exc:
            return Answer(f"tool-error:{type(exc).__name__}", normalize(str(exc)))
        return Answer("tool-ok", normalize(result))


def http_door(method: str, template: str, payload=None):
    def call(env: Env, key, row_id: str) -> Answer:
        return env.http(method, template.format(id=row_id), key, payload(row_id) if callable(payload) else payload)

    return call


def tool_door(name: str, arguments):
    def call(env: Env, key, row_id: str) -> Answer:
        return env.tool(key, name, arguments(row_id) if callable(arguments) else arguments)

    return call


READ_DOORS = (
    Door("GET source", False, http_door("GET", "/v0/vnext/sources/{id}")),
    Door("GET artifact", False, http_door("GET", "/v0/vnext/artifacts/{id}")),
    Door("GET source trace", False, http_door("GET", "/v0/vnext/traces/sources/{id}")),
    Door("GET artifact trace", False, http_door("GET", "/v0/vnext/traces/artifacts/{id}")),
    Door("GET memory audit", False, http_door("GET", "/v0/vnext/memories/{id}/audit")),
    Door("GET belief state", False, http_door("GET", "/v0/vnext/beliefs/{id}/state")),
    Door("GET project dashboard", False, http_door("GET", "/v0/vnext/projects/{id}/dashboard")),
    Door("tool explain memory_id", False, tool_door("alice_explain", lambda i: {"memory_id": i})),
    Door("tool explain continuity_object_id", False, tool_door("alice_explain", lambda i: {"continuity_object_id": i})),
    Door("tool explain entity_id", False, tool_door("alice_explain", lambda i: {"entity_id": i})),
    Door("tool review item", False, tool_door("alice_memory_review", lambda i: {"review_item_id": i})),
    Door("tool review object", False, tool_door("alice_memory_review", lambda i: {"continuity_object_id": i})),
    Door("tool recall by id", False, tool_door("alice_recall", lambda i: {"query": i})),
    Door("tool context pack by id", False, tool_door("alice_context_pack", lambda i: {"query": i})),
    Door("POST context pack by id", False, http_door("POST", "/v0/vnext/context-packs", lambda i: {"query": i})),
    Door("tool resume by id", False, tool_door("alice_resume", lambda i: {"query": i})),
    Door("tool recent decisions by id", False, tool_door("alice_recent_decisions", lambda i: {"query": i})),
    Door("tool open loops list", False, tool_door("alice_open_loops", lambda i: {"action": "list", "loop_id": i})),
)

WRITE_DOORS = (
    Door("review source", True, http_door("POST", "/v0/vnext/sources/{id}/review", {"action": "review", "review_note": "n"})),
    Door("update source", True, http_door("POST", "/v0/vnext/sources/{id}/review", {"action": "update", "title": "RENAMED"})),
    Door(
        "assign source", True,
        http_door(
            "POST", "/v0/vnext/sources/{id}/review",
            lambda i: {"action": "assign_project", "project_id": str(uuid4()), "confirm_label_hide": True},
        ),
    ),
    Door("archive source", True, http_door("POST", "/v0/vnext/sources/{id}/review", {"action": "archive"})),
    Door("delete source", True, http_door("DELETE", "/v0/vnext/sources/{id}")),
    Door("regenerate source", True, http_door("POST", "/v0/vnext/sources/{id}/regenerate", {})),
    Door("memory review accept", True, http_door("POST", "/v0/vnext/memories/{id}/review", {"action": "accept"})),
    Door("memory review edit", True, http_door("POST", "/v0/vnext/memories/{id}/review", {"action": "edit", "title": "RENAMED"})),
    Door("memory review reject", True, http_door("POST", "/v0/vnext/memories/{id}/review", {"action": "reject"})),
    Door("memory review private", True, http_door("POST", "/v0/vnext/memories/{id}/review", {"action": "private"})),
    Door(
        "memory review assign", True,
        http_door("POST", "/v0/vnext/memories/{id}/review", lambda i: {"action": "assign_project", "project_id": str(uuid4())}),
    ),
    Door("memory review promote", True, http_door("POST", "/v0/vnext/memories/{id}/review", {"action": "promote"})),
    Door(
        "memory correct", True,
        http_door("POST", "/v0/vnext/memories/correct", lambda i: {"memory_id": i, "canonical_text": "REPLACED"}),
    ),
    Door("memory expire", True, http_door("POST", "/v0/vnext/memories/expire", lambda i: {"memory_id": i, "reason": "r"})),
    Door("memory forget", True, http_door("POST", "/v0/vnext/memories/forget", lambda i: {"memory_id": i, "reason": "r"})),
    Door("memory redact", True, http_door("POST", "/v0/vnext/memories/redact", lambda i: {"memory_id": i, "reason": "r"})),
    Door("memory undo", True, http_door("POST", "/v0/vnext/memories/undo", lambda i: {"memory_id": i, "reason": "r"})),
    Door("memory unexpire", True, http_door("POST", "/v0/vnext/memories/unexpire", lambda i: {"memory_id": i, "reason": "r"})),
    Door(
        "accept consolidation", True,
        http_door("POST", "/v0/vnext/memories/accept-consolidation", lambda i: {"memory_id": i, "reason": "r"}),
    ),
    Door("loop review close", True, http_door("POST", "/v0/vnext/open-loops/{id}/review", {"action": "close"})),
    Door("loop review edit", True, http_door("POST", "/v0/vnext/open-loops/{id}/review", {"action": "edit", "title": "RENAMED"})),
    Door("artifact review promote", True, http_door("POST", "/v0/vnext/artifacts/{id}/review", {"action": "promote"})),
    Door("artifact review reject", True, http_door("POST", "/v0/vnext/artifacts/{id}/review", {"action": "reject"})),
    Door("artifact feedback", True, http_door("POST", "/v0/vnext/artifacts/{id}/insight-feedback", {"useful_insight": "yes"})),
    Door("artifact rating", True, http_door("POST", "/v0/vnext/artifacts/{id}/quality-ratings", {"usefulness": 3})),
    Door(
        "artifact export", True,
        lambda env, key, row_id: env.http("POST", f"/v0/vnext/artifacts/{row_id}/export", key, {"output_dir": env.export_dir}),
    ),
    Door("belief review", True, http_door("POST", "/v0/vnext/beliefs/{id}/review", {"action": "retire"})),
    Door(
        "belief review superseded_by", True,
        lambda env, key, row_id: env.http(
            "POST", f"/v0/vnext/beliefs/{uuid4()}/review", key, {"action": "supersede", "superseded_by": row_id}
        ),
    ),
    Door("edge review", True, http_door("POST", "/v0/vnext/graph/edges/{id}/review", {"action": "reject"})),
    Door(
        "project update review", True,
        http_door("POST", "/v0/vnext/projects/update-candidates/{id}/review", {"action": "accept"}),
    ),
    Door("tool correct approve", True, tool_door("alice_memory_correct", lambda i: {"review_item_id": i, "action": "approve"})),
    Door("tool correct reject", True, tool_door("alice_memory_correct", lambda i: {"continuity_object_id": i, "action": "reject"})),
    Door(
        "tool correct supersede", True,
        tool_door(
            "alice_memory_correct",
            lambda i: {"review_item_id": i, "action": "supersede-existing", "replacement_body": {"text": "R"}},
        ),
    ),
    Door("tool manage forget", True, tool_door("alice_memory_manage", lambda i: {"memory_id": i, "action": "forget"})),
    Door("tool manage expire", True, tool_door("alice_memory_manage", lambda i: {"memory_id": i, "action": "expire", "reason": "r"})),
    Door("tool manage unexpire", True, tool_door("alice_memory_manage", lambda i: {"memory_id": i, "action": "unexpire", "reason": "r"})),
    Door("tool manage redact", True, tool_door("alice_memory_manage", lambda i: {"memory_id": i, "action": "redact", "reason": "r"})),
    Door(
        "tool manage accept", True,
        tool_door("alice_memory_manage", lambda i: {"memory_id": i, "action": "accept_consolidation", "reason": "r"}),
    ),
    Door("tool manage undo", True, tool_door("alice_memory_manage", lambda i: {"memory_id": i, "action": "undo"})),
    Door("tool open loop close", True, tool_door("alice_open_loops", lambda i: {"action": "close", "loop_id": i})),
    Door("tool open loop edit", True, tool_door("alice_open_loops", lambda i: {"action": "edit", "loop_id": i, "title": "RENAMED"})),
)

# Doors that take an id as a reference to attach, not as the row to act on.
REFERENCE_DOORS = (
    Door("create loop over memory", True, http_door("POST", "/v0/vnext/open-loops", lambda i: {"title": "LOOP", "memory_id": i})),
    Door("create loop over source", True, http_door("POST", "/v0/vnext/open-loops", lambda i: {"title": "LOOP", "source_id": i})),
    Door(
        "proposal citing source", True,
        http_door("POST", "/v0/vnext/memory-proposals", lambda i: {"title": "P", "canonical_text": "P", "source_refs": [f"source:{i}"]}),
    ),
    Door(
        "proposal citing memory", True,
        http_door("POST", "/v0/vnext/memory-proposals", lambda i: {"title": "P", "canonical_text": "P", "source_refs": [f"memory:{i}"]}),
    ),
    Door(
        "ingest citing source", True,
        http_door(
            "POST", "/v0/vnext/agents/ingest-output",
            lambda i: {"agent_id": "a", "title": "T", "content": "C", "source_refs": [f"source:{i}"]},
        ),
    ),
    Door(
        "tool commit citing source", True,
        tool_door("alice_memory_commit", lambda i: {"title": "C", "canonical_text": "C", "source_refs": [f"source:{i}"]}),
    ),
    Door(
        "tool commit citing memory", True,
        tool_door("alice_memory_commit", lambda i: {"title": "C", "canonical_text": "C", "source_refs": [f"memory:{i}"]}),
    ),
    Door(
        "tool supersede by id", True,
        tool_door("alice_memory_manage", lambda i: {"memory_id": str(uuid4()), "action": "expire", "reason": "r", "superseded_by": i}),
    ),
)

ALL_DOORS = READ_DOORS + WRITE_DOORS + REFERENCE_DOORS

# Doors that name the row to act on and refuse a row above the caller's limits with the policy refusal (HTTP 403, or the
# tool's not-permitted error) where a missing row gets a not-found answer. That refusal confirms that the row exists and
# can repeat its labels. It is the known exception the security note names. This list is its boundary: a door that is
# not on it must answer a hidden id exactly as it answers a missing one, and a door on it must still refuse.
POLICY_REFUSAL_DOORS = frozenset(
    {
        "GET artifact", "GET artifact trace", "GET memory audit",
        "memory review accept", "memory review edit", "memory review reject", "memory review private",
        "memory review assign", "memory review promote", "memory correct", "memory expire", "memory forget",
        "memory redact", "memory undo", "memory unexpire", "accept consolidation",
        "loop review close", "loop review edit",
        "artifact review promote", "artifact review reject", "artifact feedback", "artifact rating", "artifact export",
        "project update review",
        "tool review item", "tool review object", "tool correct approve", "tool correct reject",
        "tool correct supersede", "tool manage forget", "tool manage expire", "tool manage unexpire",
        "tool manage redact", "tool manage accept", "tool manage undo", "tool open loop close", "tool open loop edit",
    }
)
