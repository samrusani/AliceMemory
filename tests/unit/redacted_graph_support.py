"""Random graphs of derived rows with redacted rows at every depth, and an oracle written without the kernel.

A redacted row has its text and its markers replaced, so it is an original. A derived row that recorded a redacted row as
an input kept the words it copied, so it is unverified, and so is every row built from it. The oracle below states that
from the way the graph was built, with the plain recursion of the sentence and none of the kernel's code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import random
from uuid import UUID

from alicebot_api.vnext_derived_labels import SENSITIVITY_RANK, with_derived_from

USER = "label-guard"
SENSITIVITIES = ("public", "internal", "private", "confidential")
# The ceilings of the restricted profiles are prefixes of the rank table, and so are these.
CEILINGS = (
    ("public",),
    ("public", "internal", "unknown"),
    ("public", "internal", "unknown", "private"),
    ("public", "internal", "unknown", "private", "confidential"),
)
REGULATED = SENSITIVITY_RANK["regulated"]


@dataclass
class Node:
    kind: str
    row: dict
    inputs: list["Node"] = field(default_factory=list)
    redacted: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, str(self.row["id"])


@dataclass
class Graph:
    nodes: list[Node]

    def rows(self, kind: str | None = None) -> list[dict]:
        return [node.row for node in self.nodes if kind is None or node.kind == kind]

    def contained(self) -> dict[tuple[str, str], bool]:
        """A row not redacted is contained when any input is redacted or contained."""
        memo: dict[tuple[str, str], bool] = {}

        def walk(node: Node) -> bool:
            if node.key in memo:
                return memo[node.key]
            if node.redacted:
                memo[node.key] = False
                return False
            result = any(item.redacted or walk(item) for item in node.inputs)
            memo[node.key] = result
            return result

        for node in self.nodes:
            walk(node)
        return memo

    def ranks(self) -> dict[tuple[str, str], int]:
        """The rank a reader must use: the top rank for a contained row, else the highest stored rank it reaches."""
        contained = self.contained()
        memo: dict[tuple[str, str], int] = {}

        def walk(node: Node) -> int:
            if node.key in memo:
                return memo[node.key]
            if contained[node.key]:
                memo[node.key] = REGULATED
                return REGULATED
            own = SENSITIVITY_RANK[str(node.row["sensitivity"])]
            memo[node.key] = max([own, *(walk(item) for item in node.inputs)])
            return memo[node.key]

        for node in self.nodes:
            walk(node)
        return memo

    def hidden_from(self, ceiling: tuple[str, ...]) -> set[tuple[str, str]]:
        """Rows no reader limited to this ceiling may read: those whose rank is above its highest rank."""
        top = max(SENSITIVITY_RANK[name] for name in ceiling)
        ranks = self.ranks()
        return {key for key, rank in ranks.items() if rank > top}


def _redacted_row(row: dict) -> dict:
    """What a redacted memory keeps: no text, no markers, its structural keys and the flag."""
    return {**row, "metadata_json": {"redacted": True, "redacted_at": "2026-10-09T00:00:00Z", "project_scope": []},
            "value": {"redacted": True}, "status": "archived"}


def random_graph(seed: int, *, redact_chance: float = 0.25, artifacts: bool = True, id_base: int = 0xA0000) -> Graph:
    rng = random.Random(seed)
    counter = iter(range(1, 10_000))
    nodes: list[Node] = []

    def new_id() -> UUID:
        return UUID(int=id_base + next(counter))

    def base(kind: str, sensitivity: str) -> dict:
        row = {"id": new_id(), "user_id": USER, "domain": "project", "sensitivity": sensitivity}
        if kind == "source":
            row["metadata_json"] = {"project_scope": [], "project_floor": []}
        else:
            row.update({"status": "active", "value": {}, "metadata_json": {"project_scope": [], "project_floor": []}})
        if kind == "artifact":
            row.pop("status", None)
            row.pop("value", None)
        return row

    def add(kind: str, row: dict, inputs: list[Node]) -> Node:
        node = Node(kind, row, inputs)
        nodes.append(node)
        return node

    for _ in range(rng.randint(3, 5)):
        add("source", base("source", rng.choice(SENSITIVITIES)), [])
    for _ in range(rng.randint(3, 5)):
        add("memory", base("memory", rng.choice(SENSITIVITIES)), [])
    # A redacted original: the text of a memory that was redacted before anything was built from it.
    for node in nodes:
        if node.kind == "memory" and rng.random() < redact_chance:
            node.row = _redacted_row(node.row)
            node.redacted = True

    def stamped(picks: list[Node]) -> dict:
        used = {"sources": [], "memories": [], "artifacts": []}
        for pick in picks:
            used[{"source": "sources", "memory": "memories", "artifact": "artifacts"}[pick.kind]].append(pick.row)
        return used

    def maybe_redact(node: Node) -> None:
        # A derived memory that is redacted later is an original from then on, whatever it recorded.
        if node.kind == "memory" and rng.random() < redact_chance / 2:
            node.row = _redacted_row(node.row)
            node.redacted = True
            node.inputs = []

    for _depth in range(rng.randint(2, 4)):
        for _ in range(rng.randint(3, 6)):
            pool = list(nodes)
            picks = rng.sample(pool, k=min(len(pool), rng.randint(1, 3)))
            kind = rng.choice(("memory", "memory", "artifact") if artifacts else ("memory",))
            row = base(kind, rng.choice(SENSITIVITIES))
            metadata = {"project_scope": [], "project_floor": []}
            if kind == "artifact":
                if rng.random() < 0.15:
                    # A weekly report names its candidate in a list that is not one of its inputs (the guard reads such a report whole).
                    used = stamped(picks)
                    counts = {name: len(used[name]) for name in used}
                    candidate_row = base("memory", rng.choice(SENSITIVITIES))
                    candidate_row["metadata_json"] = with_derived_from(
                        {"project_scope": [], "project_floor": [], "discovered_by": "vnext_weekly_synthesis"}, used
                    )
                    metadata.update({
                        "workflow": "weekly_synthesis",
                        "input_summary": {
                            "source_ids": [str(item["id"]) for item in used["sources"]],
                            "memory_ids": [str(item["id"]) for item in used["memories"]],
                            "artifact_ids": [str(item["id"]) for item in used["artifacts"]],
                            "open_loop_ids": [],
                            "counts": {"sources": counts["sources"], "memories": counts["memories"],
                                       "artifacts": counts["artifacts"], "open_loops": 0},
                        },
                        "candidate_memory_ids": [str(candidate_row["id"])],
                    })
                    row["artifact_type"] = "weekly_synthesis"
                    row["metadata_json"] = metadata
                    add("artifact", row, picks)
                    add("memory", candidate_row, picks)
                    continue
                metadata["workflow"] = rng.choice(("daily_brief", "weekly_synthesis", "connection_finder", "project_auto_update"))
                row["artifact_type"] = rng.choice(("daily_brief", "weekly_synthesis", "connection_report", "project_update"))
                row["metadata_json"] = with_derived_from(metadata, stamped(picks))
                node = add("artifact", row, picks)
                if rng.random() < redact_chance / 3:
                    # A project update redacted with its memory keeps its type, the flag and nothing it copied.
                    node.row = {**row, "metadata_json": {"redacted": True, "redacted_at": "2026-10-09T00:00:00Z",
                                                         "workflow": "project_auto_update", "project_scope": []}}
                    node.redacted = True
                    node.inputs = []
                continue
            flavor = rng.choice(("consolidation", "weekly", "update", "rollup", "promoted", "copy"))
            members = [pick for pick in picks if pick.kind == "memory"] or [rng.choice([n for n in nodes if n.kind == "memory"])]
            if flavor == "promoted":
                reports = [n for n in nodes if n.kind == "artifact" and not n.redacted]
                if not reports:
                    flavor = "weekly"
                else:
                    report = rng.choice(reports)
                    picks = [report]
                    metadata["source_artifact_id"] = str(report.row["id"])
                    row["value"] = {"kind": "promoted_artifact", "artifact_id": str(report.row["id"])}
            if flavor == "copy":
                origin = rng.choice([n for n in nodes if n.kind == "source"])
                picks = [origin]
                metadata["source_id"] = str(origin.row["id"])
            elif flavor == "consolidation":
                picks = members
                metadata["consolidation"] = {"cluster_member_ids": [str(item.row["id"]) for item in members]}
            elif flavor == "rollup":
                picks = members
                metadata["candidate_kind"] = "memory_rollup"
                row["value"] = {"rollup": {"member_ids": [str(item.row["id"]) for item in members]}}
            elif flavor == "weekly":
                metadata["discovered_by"] = "vnext_weekly_synthesis"
            elif flavor == "update":
                metadata["workflow"] = "project_auto_update"
            row["metadata_json"] = metadata if flavor == "copy" else with_derived_from(metadata, stamped(picks))
            maybe_redact(add("memory", row, picks))
    return Graph(nodes)
