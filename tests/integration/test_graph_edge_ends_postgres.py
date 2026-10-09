"""A key with limits reads a graph edge only when it may read every row the edge names, whatever kind that row is.

An edge has no label of its own and keeps the explanation it was made with, which holds the titles and shared terms of the
rows at its two ends. The ends are judged by an oracle written from the stored rows with plain rules and none of the kernel:
a row that is missing, above the key's ceiling, redacted, or built from a redacted row hides the edge. The exception is a
project end. A project is named by the id of its row or by any identifier the caller typed, so an end that is a name, or an
id that finds no project row, names no row and hides nothing, as an entity end hides nothing. A project row that exists and
is not readable (above the ceiling, or built from a redacted memory) still hides the edge.

Every kind of end is checked in both positions: as the end an edge points to, and as the end an edge starts from. The
neighborhood and the edge review give the same verdict, a refused review changes nothing, and the unbound admin key still
reads every edge.

Mutations that these tests must fail (the manifest replays each one): the guard stops reading the source, artifact, open
loop or project end of an edge; a named project end hides the edge; a review is not refused before it changes the edge.
"""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.redaction_containment_support import World, contained_ids, snapshot

# What the unbound trusted key may read. It is written out here and not taken from the code under test.
CEILING = ("public", "internal", "private", "unknown")
TABLES = {
    "memory": "memories",
    "source": "sources",
    "open_loop": "open_loops",
    "artifact": "generated_artifacts",
    "project": "projects",
}
NAMED = "atlas-project"
SLUG = "secret-slug"


def _stored(h, kind: str, row_id: str):
    """The labels of a stored row, or None when there is no such row. A text that is not an id names no row."""
    try:
        UUID(row_id)
    except ValueError:
        return None
    with h.store() as store, store.conn.cursor() as cur:
        cur.execute(f"SELECT sensitivity, metadata_json FROM {TABLES[kind]} WHERE id = %s::uuid", (row_id,))  # closed table names
        return cur.fetchone()


def _hides(h, kind: str, row_id: str, contained: set[str]) -> bool:
    """Whether this end hides an edge from the trusted key, by the rules in the module docstring."""
    if kind == "entity":
        return False
    row = _stored(h, kind, row_id)
    if row is None:
        return kind != "project"
    metadata = row["metadata_json"] or {}
    return row["sensitivity"] not in CEILING or metadata.get("redacted") is True or row_id in contained


class Ends:
    """Rows of every kind that carry a label, each one clear, above the ceiling, missing, or built from a redacted memory."""

    def __init__(self, h, world: World) -> None:
        self.h = h
        self.world = world
        with h.store() as store:
            self.base_source = world.sources[1]
            self.base_memory = world.memories[0]
            confidential = {"domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": [world.alpha]}}
            memory = store.create_memory(
                {"memory_key": "ends.confidential", "memory_type": "episode", "title": "CONFMEMORY title",
                 "canonical_text": "CONFMEMORY text", "status": "active", **confidential}
            )
            source = store.create_source(
                {"source_type": "manual_text", "title": "CONFSOURCE title", "content_hash": str(uuid4()),
                 "captured_at": "2026-10-05T09:00:00Z", **confidential}
            )
            loop = store.create_open_loop(
                {"title": "CONFLOOP title", "description": "CONFLOOP text", "status": "open", "opened_at": "2026-10-05T09:00:00Z",
                 **confidential}
            )
            artifact = store.create_artifact(
                {"artifact_type": "daily_brief", "title": "CONFARTIFACT title", "content_markdown": "CONFARTIFACT text", **confidential}
            )
            clear_loop = store.create_open_loop(
                {"title": "CLEARLOOP title", "description": "CLEARLOOP text", "status": "open", "domain": "project",
                 "sensitivity": "public", "opened_at": "2026-10-05T09:00:00Z", "metadata_json": {"project_scope": [world.alpha]}}
            )
            clear_project = str(uuid4())
            store.create_project({"id": clear_project, "name": "Clear", "slug": "clear", "domain": "project", "sensitivity": "public"})
            confidential_project = str(uuid4())
            store.create_project(
                {"id": confidential_project, "name": "Secret", "slug": SLUG, "domain": "project", "sensitivity": "confidential"}
            )
            contained_project = str(uuid4())
            store.create_project(
                {"id": contained_project, "name": "Built", "slug": "built", "domain": "project", "sensitivity": "public",
                 "metadata_json": {"derived_from": {"memories": [world.redacted]}}}
            )
        # (name, kind, id): each is the other end of two edges, one pointing to it and one starting from it.
        self.ends = [
            ("clear memory", "memory", str(world.memories[1]["id"])),
            ("redacted memory", "memory", world.redacted),
            ("confidential memory", "memory", str(memory["id"])),
            ("clear source", "source", str(world.sources[0]["id"])),
            ("confidential source", "source", str(source["id"])),
            ("missing source", "source", str(uuid4())),
            ("clear open loop", "open_loop", str(clear_loop["id"])),
            ("confidential open loop", "open_loop", str(loop["id"])),
            ("clear artifact", "artifact", str(world.reports["open_loop_review"]["id"])),
            ("artifact built from the redacted memory", "artifact", str(world.reports["daily"]["id"])),
            ("confidential artifact", "artifact", str(artifact["id"])),
            ("clear project", "project", clear_project),
            ("confidential project", "project", confidential_project),
            ("project built from the redacted memory", "project", contained_project),
            ("named project", "project", NAMED),
            ("named project that is a slug of a confidential row", "project", SLUG),
            ("project id that finds no row", "project", str(uuid4())),
            ("entity", "entity", str(uuid4())),
        ]
        self.edges: list[dict] = []
        with h.store() as store:
            for number, (name, kind, row_id) in enumerate(self.ends):
                for position in ("to", "from"):
                    base = {"type": "source", "id": str(self.base_source["id"])} if position == "to" else {"type": "memory", "id": str(self.base_memory["id"])}
                    other = {"type": kind, "id": row_id}
                    first, second = (base, other) if position == "to" else (other, base)
                    marker = f"EDGETEXT{number}{position}"
                    edge = store.create_edge(
                        {"from_type": first["type"], "from_id": first["id"], "to_type": second["type"], "to_id": second["id"],
                         "edge_type": "mentions", "confidence": 0.5, "explanation": f"{name} says {marker}.",
                         "created_by": "test", "metadata_json": {"status": "candidate"}}
                    )
                    self.edges.append(
                        {"id": str(edge["id"]), "name": name, "kind": kind, "end": row_id, "position": position, "marker": marker,
                         "ids": (first["id"], second["id"])}
                    )
        contained = contained_ids(h)
        for edge in self.edges:
            edge["hidden"] = _hides(h, edge["kind"], edge["end"], contained)

    def targets(self) -> list[str]:
        return [str(self.base_source["id"]), str(self.base_memory["id"]), *(row_id for _name, _kind, row_id in self.ends)]


@pytest.fixture
def ends(label_harness):
    return Ends(label_harness, World(label_harness))


def _shown(body) -> set[str]:
    return {edge["id"] for edge in body["from_edges"] + body["to_edges"]}


def test_the_oracle_marks_each_kind_of_end_both_ways(ends):
    """The cases are not all one verdict: the test below would pass for a guard that hides everything or nothing."""
    verdicts = {(edge["kind"], edge["hidden"]) for edge in ends.edges}
    for kind in ("memory", "source", "open_loop", "artifact", "project"):
        assert (kind, True) in verdicts and (kind, False) in verdicts, kind
    assert ("entity", False) in verdicts
    assert {edge["name"] for edge in ends.edges if edge["kind"] == "project" and not edge["hidden"]} >= {
        "clear project", "named project", "named project that is a slug of a confidential row", "project id that finds no row"
    }


def test_a_key_reads_an_edge_only_when_it_may_read_the_row_at_each_end_whatever_its_kind_and_position(label_harness, ends):
    h = label_harness
    keys = ends.world.keys()
    mine = {edge["id"] for edge in ends.edges}
    expected = {edge["id"] for edge in ends.edges if not edge["hidden"]}
    assert expected and expected < mine
    secrets = [edge["marker"] for edge in ends.edges if edge["hidden"]]
    seen_by_trusted: set[str] = set()
    for target in ends.targets():
        touching = {edge["id"] for edge in ends.edges if target in edge["ids"]}
        status, admin_body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{target}", key=keys["admin"])
        # The two base rows also hold the edges the producers made. Only the edges made here are judged by this oracle.
        assert status == 200 and _shown(admin_body) & mine == touching, target
        status, body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{target}", key=keys["trusted"])
        assert status == 200
        assert _shown(body) & mine == touching & expected, (target, _shown(body) ^ (touching & expected))
        assert body["edge_count"] == len(body["from_edges"]) + len(body["to_edges"])
        assert not any(secret in json.dumps(body) for secret in secrets), target
        seen_by_trusted |= _shown(body)
    assert seen_by_trusted & mine == expected
    # The named project's own neighborhood lists its edges, and so does the neighborhood of an id that finds no row.
    for name in ("named project", "project id that finds no row"):
        end = next(edge["end"] for edge in ends.edges if edge["name"] == name)
        status, body, _ = h.request("GET", f"/v0/vnext/graph/neighborhood/{end}", key=keys["trusted"])
        assert status == 200 and body["edge_count"] == 2, name


def test_the_review_of_an_edge_gives_the_verdict_of_the_neighborhood_and_a_refused_review_changes_nothing(label_harness, ends):
    h = label_harness
    keys = ends.world.keys()
    hidden = [edge for edge in ends.edges if edge["hidden"]]
    clear = [edge for edge in ends.edges if not edge["hidden"]]
    assert hidden and clear
    missing = "/v0/vnext/graph/edges/00000000-0000-4000-8000-000000000003/review"
    secrets = [edge["marker"] for edge in hidden]
    before = snapshot(h)
    for edge in hidden:
        for action in ("review", "accept", "reject"):
            status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{edge['id']}/review", payload={"action": action}, key=keys["trusted"])
            assert status == 404, (edge["name"], edge["position"], action, status)
            assert body == h.request("POST", missing, payload={"action": action}, key=keys["trusted"])[1]
            assert not any(secret in json.dumps(body) for secret in secrets)
    assert snapshot(h) == before, "a refused review changed an edge or recorded an event"
    for edge in clear:
        status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{edge['id']}/review", payload={"action": "review"}, key=keys["trusted"])
        assert status == 200 and body["metadata_json"]["status"] == "reviewed", (edge["name"], edge["position"], body)
    # The unbound admin key is not limited: it reviews a hidden edge and reads the explanation that edge was made with.
    for edge in hidden:
        status, body, _ = h.request("POST", f"/v0/vnext/graph/edges/{edge['id']}/review", payload={"action": "accept"}, key=keys["admin"])
        assert status == 200 and edge["marker"] in json.dumps(body), (edge["name"], edge["position"])


def test_a_review_checks_its_action_first_and_answers_a_malformed_id_as_a_missing_row(label_harness):
    """The two review routes for a key with limits: the action is checked before the row is looked up, and an id that is not an
    id answers as a row that is not there does, instead of reaching the database as a value it cannot read."""
    h = label_harness
    trusted = h.key("trusted_local_agent")
    with h.store() as store:
        public = store.create_memory(
            {"memory_key": "review.public", "memory_type": "episode", "title": "PUBLIC title", "canonical_text": "PUBLIC text",
             "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": []}}
        )
        secret = store.create_memory(
            {"memory_key": "review.secret", "memory_type": "episode", "title": "SECRET title", "canonical_text": "SECRET text",
             "status": "active", "domain": "project", "sensitivity": "confidential", "metadata_json": {"project_scope": []}}
        )
        hidden_belief = str(store.create_belief({"memory_id": str(secret["id"]), "claim": "SECRET claim", "confidence": 0.9})["id"])
        clear_belief = str(store.create_belief({"memory_id": str(public["id"]), "claim": "PUBLIC claim", "confidence": 0.9})["id"])
        edges = [
            str(store.create_edge(
                {"from_type": "memory", "from_id": str(first["id"]), "to_type": "memory", "to_id": str(second["id"]),
                 "edge_type": "mentions", "confidence": 0.5, "explanation": f"{name} edge", "created_by": "test",
                 "metadata_json": {"status": "candidate"}}
            )["id"])
            for name, first, second in (("clear", public, public), ("hidden", public, secret))
        ]
    clear_edge, hidden_edge = edges
    missing_edge = "00000000-0000-4000-8000-000000000004"
    missing_belief = "00000000-0000-4000-8000-000000000005"
    before = snapshot(h)

    def review(kind, row_id, action):
        route = "graph/edges" if kind == "edge" else "beliefs"
        status, body, _ = h.request("POST", f"/v0/vnext/{route}/{row_id}/review", payload={"action": action}, key=trusted)
        return status, body

    for kind, ids, good in (("edge", (clear_edge, hidden_edge, missing_edge, "not-an-id"), "review"),
                            ("belief", (clear_belief, hidden_belief, missing_belief, "not-an-id"), "reinforce")):
        refused = review(kind, "not-an-id", good)
        assert refused[0] == 404 and refused == review(kind, missing_edge if kind == "edge" else missing_belief, good), kind
        # An action that does not exist is a bad request whatever the row is, and is answered the same for every row.
        invalid = {review(kind, row_id, "no-such-action")[0] for row_id in ids}
        assert invalid == {400}, (kind, invalid)
        assert len({json.dumps(review(kind, row_id, "no-such-action")[1]) for row_id in ids}) == 1, kind
    assert snapshot(h) == before, "a refused call changed a belief, an edge or the events about them"
