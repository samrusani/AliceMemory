"""The pure derived-label kernel. No store and no product caller."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api import vnext_brain as brain
from alicebot_api import vnext_consolidation as consolidation
from alicebot_api.vnext_agent_control import RESTRICTED_DOMAINS, AgentIdentity, evaluate_agent_policy
from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError, _ID_KEYS
from alicebot_api.vnext_derived_labels import (
    SENSITIVITY_RANK,
    V2_ID_KEYS,
    dependencies_of,
    generation_domain,
    group_scope,
    is_derived,
    labels_raised_payload,
    row_class,
    settle_labels,
)
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER, project_floor_within, project_scopes_overlap

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16
USER = "user-1"
SOURCE_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _row(kind: str, row_id: str, **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "kind": kind,
        "id": row_id,
        "user_id": USER,
        "domain": "unknown",
        "sensitivity": "unknown",
        "metadata_json": {},
    }
    row.update(fields)
    return row


def _meta(row: dict[str, object], **fields: object) -> dict[str, object]:
    metadata = dict(row["metadata_json"]) if isinstance(row["metadata_json"], dict) else {}
    metadata.update(fields)
    row["metadata_json"] = metadata
    return row


def _brief(row_id: str, *, sources: list[str] | None = None, memories: list[str] | None = None,
           loops: list[str] | None = None, artifacts: list[str] | None = None, scope: list[str] | None = None,
           domain: str = "unknown", sensitivity: str = "public") -> dict[str, object]:
    sources = sources or []
    memories = memories or []
    loops = loops or []
    artifacts = artifacts or []
    summary = {
        "source_ids": sources,
        "memory_ids": memories,
        "open_loop_ids": loops,
        "artifact_ids": artifacts,
        "counts": {
            "sources": len(sources),
            "memories": len(memories),
            "open_loops": len(loops),
            "artifacts": len(artifacts),
        },
    }
    return _row(
        "artifact",
        row_id,
        artifact_type="daily_brief",
        domain=domain,
        sensitivity=sensitivity,
        metadata_json={
            "workflow": "daily_brief",
            "input_summary": summary,
            "project_scope": list(scope or []),
            "source_refs": [f"source:{item}" for item in sources],
        },
    )


def _source(row_id: str, *, domain: str = "unknown", sensitivity: str = "public", scope: list[str] | None = None,
            scrubbed: bool = False) -> dict[str, object]:
    metadata: dict[str, object] = {}
    if scope is not None:
        metadata["project_scope"] = list(scope)
    if scrubbed:
        metadata["scrubbed"] = True
    return _row("source", row_id, domain=domain, sensitivity=sensitivity, metadata_json=metadata)


def _memory(row_id: str, **fields: object) -> dict[str, object]:
    row = _row("memory", row_id, memory_key=row_id, canonical_text=row_id)
    row.update(fields)
    return row


def test_a_chain_settles_in_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain of any length is labelled from the leaf inward, each row once."""

    calls: list[str] = []
    real = __import__("alicebot_api.vnext_derived_labels", fromlist=["_apply_dependencies"])._apply_dependencies

    def wrapped(*args: object, **kwargs: object) -> object:
        current = args[0]
        calls.append(str(getattr(current, "stored_id")))
        return real(*args, **kwargs)

    monkeypatch.setattr("alicebot_api.vnext_derived_labels._apply_dependencies", wrapped)
    for size in (2, 5, 40):
        calls.clear()
        source = _source("s", domain="health", sensitivity="confidential", scope=[ALPHA])
        rows = [source]
        leaf = _brief("n0", sources=["s"], scope=[ALPHA])
        rows.append(leaf)
        previous = "n0"
        for index in range(1, size):
            rows.append(_brief(f"n{index}", artifacts=[previous], scope=[ALPHA]))
            previous = f"n{index}"
        settled = settle_labels(rows)
        derived_ids = [row.stored_id for row in settled.derived_rows()]
        assert calls == derived_ids
        for row in settled.derived_rows():
            assert row.domain == "health"
            assert row.sensitivity == "confidential"
            assert row.unverified is False


def test_a_diamond_settles_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    module = __import__("alicebot_api.vnext_derived_labels", fromlist=["_apply_dependencies"])
    real = module._apply_dependencies

    def wrapped(*args: object, **kwargs: object) -> object:
        calls.append(str(getattr(args[0], "stored_id")))
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "_apply_dependencies", wrapped)
    rows = [
        _source("s", domain="legal", sensitivity="private", scope=[ALPHA]),
        _brief("a", sources=["s"], scope=[ALPHA]),
        _brief("b", sources=["s"], scope=[ALPHA]),
        _brief("c", artifacts=["a", "b"], scope=[ALPHA]),
    ]
    settled = settle_labels(rows)
    assert calls.count("c") == 1
    assert settled.by_stored("artifact", "c").domain == "legal"


def test_a_cycle_that_does_not_settle_is_refused() -> None:
    ring = []
    names = ["r0", "r1", "r2"]
    for index, name in enumerate(names):
        ring.append(
            _brief(
                name,
                artifacts=[names[(index + 1) % 3]],
                domain="health" if index % 2 == 0 else "legal",
            )
        )
    with pytest.raises(DerivedDomainRepairError, match="did not settle"):
        settle_labels(ring)


def test_unverified_is_contagious_through_every_level() -> None:
    rows = [
        _brief("b", artifacts=["missing-artifact"]),
        _brief("a", artifacts=["b"]),
        _brief("top", artifacts=["a"]),
    ]
    settled = settle_labels(rows)
    assert settled.by_stored("artifact", "b").reason == "missing_dependency"
    assert settled.by_stored("artifact", "a").reason == "dependency_unverified"
    assert settled.by_stored("artifact", "top").reason == "dependency_unverified"


def test_every_unverified_case_is_unverified() -> None:
    belief_id = "b1"
    memory_id = "m1"
    cases = {
        "missing id": (
            [_brief("r", memories=["does-not-exist"])],
            "r",
            "missing_dependency",
        ),
        "kind with no table": (
            [_brief("r", artifacts=["ghost"])],
            "r",
            "missing_table",
        ),
        "derived marker with no record": (
            [_row("artifact", "r", artifact_type="daily_brief", metadata_json={"workflow": "daily_brief"})],
            "r",
            "no_record",
        ),
        "counts disagree": (
            [
                _row(
                    "artifact",
                    "r",
                    artifact_type="daily_brief",
                    metadata_json={
                        "workflow": "daily_brief",
                        "derived_from": {
                            "v": 1,
                            "sources": ["s"],
                            "memories": [],
                            "open_loops": [],
                            "artifacts": [],
                            "beliefs": [],
                            "counts": {"sources": 2, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
                        },
                    },
                )
            ],
            "r",
            "counts_disagree",
        ),
        "malformed list": (
            [
                _row(
                    "artifact",
                    "r",
                    artifact_type="daily_brief",
                    metadata_json={"workflow": "daily_brief", "derived_from": {"sources": [1]}},
                )
            ],
            "r",
            "malformed",
        ),
        "malformed floor": (
            [_meta(_brief("r"), **{"project_floor": "alpha"})],
            "r",
            "malformed_floor",
        ),
        "legacy counts": (
            [
                _row(
                    "artifact",
                    "r",
                    artifact_type="daily_brief",
                    metadata_json={
                        "workflow": "daily_brief",
                        "input_summary": {
                            "source_ids": ["s"],
                            "memory_ids": [],
                            "open_loop_ids": [],
                            "artifact_ids": [],
                            "counts": {"sources": 3, "memories": 0, "open_loops": 0, "artifacts": 0},
                        },
                    },
                )
            ],
            "r",
            "legacy_counts",
        ),
        "bound exceeded": (
            [
                _brief("c", artifacts=["b"]),
                _brief("b", artifacts=["a"]),
                _brief("a", sources=["s"]),
                _source("s", domain="health"),
            ],
            "c",
            "bound_exceeded",
        ),
        "dependency unverified": (
            [_brief("child", memories=["gone"]), _brief("parent", artifacts=["child"])],
            "parent",
            "dependency_unverified",
        ),
    }
    for name, (rows, row_id, reason) in cases.items():
        kwargs = {"unavailable_kinds": ["artifact"]} if name == "kind with no table" else {}
        if name == "bound exceeded":
            kwargs = {"max_hops": 1}
        settled = settle_labels(rows, **kwargs)
        got = settled.by_stored("artifact", row_id)
        assert got.unverified is True, name
        assert got.reason == reason, name

    empty = _brief("empty")
    assert settle_labels([empty]).by_stored("artifact", "empty").unverified is False

    redacted = _memory("red", metadata_json={"redacted": True, "source_id": "s", "workflow": "daily_brief"})
    assert is_derived("memory", redacted) is False
    assert settle_labels([redacted]).by_stored("memory", "red").unverified is False

    project = _row("project", "p", metadata_json={}, current_state="notes")
    assert is_derived("project", project) is False
    assert row_class("project", project) == "original"

    scrubbed = _source("s", domain="health", sensitivity="confidential", scope=[ALPHA], scrubbed=True)
    copy = _memory(
        "copy",
        domain="unknown",
        sensitivity="public",
        metadata_json={"source_id": "s", "project_scope": [ALPHA]},
    )
    kept = settle_labels([scrubbed, copy]).by_stored("memory", "copy")
    assert kept.unverified is False
    assert kept.project_scope == (ALPHA,)
    assert kept.project_floor == ()
    assert kept.domain == "health"
    assert kept.sensitivity == "confidential"

    repeated = _row(
        "artifact",
        "rep",
        artifact_type="connection_report",
        metadata_json={
            "workflow": "connection_finder",
            "source_ids": ["s", "s"],
            "memory_ids": [],
            "input_counts": {"sources": 1, "memories": 0},
        },
    )
    assert settle_labels([_source("s"), repeated]).by_stored("artifact", "rep").reason != "legacy_counts"

    belief = _row("belief", belief_id, memory_id=memory_id)
    backing = _memory(memory_id, domain="health", sensitivity="private")
    report = _row(
        "artifact",
        "belief-report",
        artifact_type="contradiction_report",
        metadata_json={
            "workflow": "contradiction_finder",
            "source_ids": [],
            "memory_ids": [],
            "belief_ids": [belief_id],
            "input_counts": {"sources": 0, "memories": 0, "beliefs": 1},
        },
    )
    followed = settle_labels([belief, backing, report]).by_stored("artifact", "belief-report")
    assert followed.unverified is False
    assert followed.domain == "health"
    assert followed.sensitivity == "private"


def test_sensitivity_is_raise_only_and_unknown_never_swaps_with_internal() -> None:
    source = _source("s", domain="unknown", sensitivity="confidential")
    raised = _memory("m", sensitivity="public", metadata_json={"source_id": "s"})
    assert settle_labels([source, raised]).by_stored("memory", "m").sensitivity == "confidential"

    high = _source("s2", domain="unknown", sensitivity="public")
    kept = _memory("m2", sensitivity="confidential", metadata_json={"source_id": "s2"})
    assert settle_labels([high, kept]).by_stored("memory", "m2").sensitivity == "confidential"

    internal = _source("s3", sensitivity="internal")
    unknown = _memory("m3", sensitivity="unknown", metadata_json={"source_id": "s3"})
    assert settle_labels([internal, unknown]).by_stored("memory", "m3").sensitivity == "unknown"

    unknown_source = _source("s4", sensitivity="unknown")
    internal_copy = _memory("m4", sensitivity="internal", metadata_json={"source_id": "s4"})
    assert settle_labels([unknown_source, internal_copy]).by_stored("memory", "m4").sensitivity == "internal"


def test_domain_follows_derived_domain_and_never_becomes_unrestricted() -> None:
    rows = [
        _source("h1", domain="health"),
        _source("h2", domain="health"),
        _source("l", domain="legal"),
        _brief("r", sources=["h1", "h2", "l"]),
    ]
    assert settle_labels(rows).by_stored("artifact", "r").domain == "health"
    tie = [
        _source("h", domain="health"),
        _source("l2", domain="legal"),
        _brief("tie", sources=["h", "l2"]),
    ]
    assert settle_labels(tie).by_stored("artifact", "tie").domain == "health"
    kept = [
        _source("open", domain="project"),
        _brief("kept", sources=["open"], domain="health"),
    ]
    assert settle_labels(kept).by_stored("artifact", "kept").domain == "health"
    moved = [
        _source("fin", domain="financial"),
        _source("fin2", domain="financial"),
        _brief("moved", sources=["fin", "fin2"], domain="health"),
    ]
    assert settle_labels(moved).by_stored("artifact", "moved").domain == "financial"
    assert generation_domain("health", ["financial"]) == "health"
    assert generation_domain("unknown", ["financial", "financial", "legal"]) == "financial"


def test_floor_is_the_union_and_never_shrinks() -> None:
    rows = [
        _source("a", scope=[ALPHA]),
        _source("b", scope=[BETA]),
        _meta(_brief("r", sources=["a", "b"], scope=[ALPHA]), **{"project_floor": [ALPHA]}),
    ]
    floor = settle_labels(rows).by_stored("artifact", "r").project_floor
    assert set(floor) == {ALPHA, BETA}

    shrink = [
        _source("only", scope=[ALPHA]),
        _meta(_brief("kept", sources=["only"], scope=[ALPHA]), **{"project_floor": [ALPHA, BETA]}),
    ]
    kept = settle_labels(shrink).by_stored("artifact", "kept").project_floor
    assert set(kept) == {ALPHA, BETA}


def test_scope_rules_per_row_class() -> None:
    global_memory = _memory("g", metadata_json={})
    alpha_source = _source("a", scope=[ALPHA], domain="health")
    report = _brief("report", sources=["a"], memories=["g"], scope=[ALPHA])
    stored = settle_labels([global_memory, alpha_source, report]).by_stored("artifact", "report")
    assert stored.project_scope == ()
    assert stored.project_floor == (ALPHA,)

    both = _brief("both", sources=["a"], scope=[ALPHA, BETA])
    beta = _source("beta", scope=[BETA])
    # a second source so the report is not global
    multi = settle_labels([alpha_source, beta, _brief("multi", sources=["a", "beta"], scope=[ALPHA, BETA])]).by_stored(
        "artifact", "multi"
    )
    assert multi.project_scope == (ALPHA, BETA)

    card = _memory(
        "card",
        metadata_json={
            "candidate_kind": "memory_rollup",
            "project_scope": [ALPHA, BETA],
            "consolidation": {"cluster_member_ids": ["m1", "m2"]},
        },
    )
    members = [
        _memory("m1", metadata_json={"project_scope": [ALPHA, BETA]}),
        _memory("m2", metadata_json={"project_scope": [ALPHA, BETA]}),
    ]
    aggregate = settle_labels([*members, card]).by_stored("memory", "card")
    assert aggregate.row_class == "aggregate"
    assert aggregate.project_scope == ()
    assert set(aggregate.project_floor) == {ALPHA, BETA}

    single = _memory(
        "one",
        metadata_json={
            "candidate_kind": "memory_rollup",
            "project_scope": [ALPHA],
            "project_floor": [ALPHA],
            "consolidation": {"cluster_member_ids": ["only"]},
        },
    )
    only = _memory("only", metadata_json={"project_scope": [ALPHA]})
    kept = settle_labels([only, single]).by_stored("memory", "one")
    assert kept.project_scope == (ALPHA,)

    copy = _memory("copy", metadata_json={"source_id": "a", "project_scope": [ALPHA, BETA]})
    narrowed = settle_labels([alpha_source, copy]).by_stored("memory", "copy")
    assert narrowed.project_scope == (ALPHA,)

    global_source = _source("glob")
    emptied = _memory("emptied", metadata_json={"source_id": "glob", "project_scope": [ALPHA]})
    assert settle_labels([global_source, emptied]).by_stored("memory", "emptied").project_scope == ()

    promoted = _memory(
        "promoted",
        metadata_json={"source_artifact_id": "report", "project_scope": [ALPHA]},
    )
    promoted_row = settle_labels([global_memory, alpha_source, report, promoted]).by_stored("memory", "promoted")
    assert promoted_row.project_scope == ()
    assert ALPHA in promoted_row.project_floor

    assert both["id"] == "both"


def test_a_forged_marker_in_value_makes_no_row_derived() -> None:
    forged = _memory("forged", value={"kind": "promoted_artifact", "artifact_id": "secret", "source_id": "s"})
    assert is_derived("memory", forged) is False
    assert dependencies_of("memory", forged) == frozenset()
    settled = settle_labels([forged, _source("s", domain="health", sensitivity="sacred")])
    row = settled.by_stored("memory", "forged")
    assert row.derived is False
    assert row.domain == "unknown"
    assert row.sensitivity == "unknown"


def test_a_dependency_is_found_in_every_recorded_field_of_every_derived_kind() -> None:
    source = "11111111-1111-1111-1111-111111111111"
    memory = "22222222-2222-2222-2222-222222222222"
    loop = "33333333-3333-3333-3333-333333333333"
    artifact = "44444444-4444-4444-4444-444444444444"
    samples = [
        _brief("daily", sources=[source], memories=[memory], loops=[loop], artifacts=[artifact]),
        _row(
            "artifact",
            "connection",
            artifact_type="connection_report",
            metadata_json={
                "workflow": "connection_finder",
                "source_ids": [source],
                "memory_ids": [memory],
                "source_refs": [f"source:{source}"],
                "input_counts": {"sources": 1, "memories": 1},
            },
        ),
        _row(
            "artifact",
            "contradiction",
            artifact_type="contradiction_report",
            metadata_json={
                "workflow": "contradiction_finder",
                "source_ids": [source],
                "memory_ids": [memory],
                "belief_ids": ["belief-1"],
                "source_refs": [f"memory:{memory}"],
                "input_counts": {"sources": 1, "memories": 1, "beliefs": 1},
            },
        ),
        _row(
            "artifact",
            "consolidation",
            artifact_type="memory_consolidation",
            metadata_json={
                "workflow": "memory_consolidation",
                "consolidation": {"cluster_member_ids": [memory], "member_snapshots": [{"id": memory}]},
                "source_refs": [f"source:{source}"],
            },
        ),
        _row(
            "artifact",
            "project",
            artifact_type="project_update",
            metadata_json={"workflow": "project_auto_update", "source_ids": [source], "memory_ids": [memory]},
        ),
        _row(
            "artifact",
            "stale",
            metadata_json={"workflow": "staleness_sweep", "stale_marked_memory_ids": [memory]},
        ),
        _row(
            "artifact",
            "loops",
            metadata_json={"workflow": "open_loop_review", "open_loop_ids": [loop], "source_refs": [f"source:{source}"]},
        ),
        _row(
            "artifact",
            "agent",
            metadata_json={"connector_name": "agent_output", "source_id": source, "source_refs": [f"source:{source}"]},
        ),
        _memory(
            "weekly",
            metadata_json={"discovered_by": "vnext_weekly_synthesis", "input_summary": {"memory_ids": [memory], "source_ids": [], "open_loop_ids": [], "artifact_ids": [], "counts": {"memories": 1, "sources": 0, "open_loops": 0, "artifacts": 0}}},
        ),
        _memory(
            "rollup",
            metadata_json={"candidate_kind": "memory_rollup", "consolidation": {"cluster_member_ids": [memory]}},
            value={"rollup": {"member_ids": [memory]}},
        ),
        _memory("promoted", metadata_json={"source_artifact_id": artifact}, value={"artifact_id": artifact}),
        _memory("extracted", metadata_json={"source_id": source, "extraction_rule": "sentence"}),
        _row("open_loop", "found", metadata_json={"discovered_by": "vnext_daily_brief", "source_id": source}, source_id=source),
        _row(
            "project",
            "state",
            current_state="line",
            metadata_json={"derived_from": {"memories": [memory], "artifacts": [artifact], "sources": [], "open_loops": [], "beliefs": [], "counts": {"memories": 1, "artifacts": 1, "sources": 0, "open_loops": 0, "beliefs": 0}}},
        ),
    ]
    expected = {
        "daily": {("source", source), ("memory", memory), ("open_loop", loop), ("artifact", artifact)},
        "connection": {("source", source), ("memory", memory)},
        "contradiction": {("source", source), ("memory", memory), ("belief", "belief-1")},
        "consolidation": {("memory", memory), ("source", source)},
        "project": {("source", source), ("memory", memory)},
        "stale": {("memory", memory)},
        "loops": {("open_loop", loop), ("source", source)},
        "agent": {("source", source)},
        "weekly": {("memory", memory)},
        "rollup": {("memory", memory)},
        "promoted": {("artifact", artifact)},
        "extracted": {("source", source)},
        "found": {("source", source)},
        "state": {("memory", memory), ("artifact", artifact)},
    }
    for sample in samples:
        assert is_derived(sample["kind"], sample) is True
        assert set(dependencies_of(sample["kind"], sample)) == expected[str(sample["id"])]


def test_a_ref_is_read_in_every_spelling_and_a_belief_resolves_through_its_memory() -> None:
    canonical = str(UUID(SOURCE_UUID))
    spellings = [
        SOURCE_UUID.upper(),
        SOURCE_UUID.replace("-", ""),
        "{" + SOURCE_UUID + "}",
        "urn:uuid:" + SOURCE_UUID,
        "source:" + SOURCE_UUID.upper(),
    ]
    for spelling in spellings:
        row = _memory("m", metadata_json={"source_id": spelling})
        assert dependencies_of("memory", row) == frozenset({("source", canonical)})
    refs = _brief("r", sources=[])
    _meta(refs, **{"source_refs": ["source:" + SOURCE_UUID.upper(), "memory:" + SOURCE_UUID]})
    found = dependencies_of("artifact", refs)
    assert ("source", canonical) in found
    assert ("memory", canonical) in found


def test_dependency_keys_cover_every_producer() -> None:
    """Every id key a producer writes as a dict key is classified.

    A new key fails this test until it is either read as a dependency or named
    in ``_NOT_A_DEPENDENCY``. The v2 keys are a subset of the keys v3 reads.
    """

    assert set(_ID_KEYS) <= V2_ID_KEYS | {"source_refs"}
    root = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "alicebot_api"
    producers = [
        "vnext_brain.py",
        "vnext_connections.py",
        "vnext_contradictions.py",
        "vnext_consolidation.py",
        "vnext_rollups.py",
        "vnext_projects.py",
        "vnext_scheduler.py",
        "vnext_queue.py",
        "vnext_connectors.py",
        "vnext_capture.py",
    ]
    found: set[str] = set()
    for name in producers:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    text = key.value
                    if text.endswith("_id") or text.endswith("_ids") or text in {"source_refs", "source_ref"}:
                        found.add(text)
    unknown = sorted(found - _DEPENDENCY_KEYS - _NOT_A_DEPENDENCY)
    assert unknown == []


_DEPENDENCY_KEYS = frozenset(
    {
        "artifact_id",
        "artifact_ids",
        "belief_ids",
        "cluster_member_ids",
        "cluster_membership",
        "member_ids",
        "memory_id",
        "memory_ids",
        "open_loop_ids",
        "source_artifact_id",
        "source_id",
        "source_ids",
        "source_refs",
        "stale_marked_memory_ids",
        "candidate_memory_ids",
    }
)
_NOT_A_DEPENDENCY = frozenset(
    {
        "actor_id",
        "agent_id",
        "agent_run_id",
        "allowed_chat_ids",
        "belief_id",
        "belief_memory_id",
        "candidate_edge_ids",
        "candidate_memory_id",
        "candidate_open_loop_ids",
        "chat_id",
        "connector_id",
        "conversation_id",
        "created_by_agent_id",
        "external_chat_id",
        "external_id",
        "failed_external_ids",
        "from_id",
        "last_failed_external_ids",
        "last_run_id",
        "last_source_ids",
        "message_id",
        "output_artifact_id",
        "person_id",
        "project_id",
        "project_ids",
        "promoted_memory_id",
        "provider_message_id",
        "provider_update_id",
        "review_artifact_id",
        "revises_memory_id",
        "rollup_candidate_ids",
        "run_id",
        "scheduler_run_id",
        "sender_id",
        "source_chunk_id",
        "source_event_ids",
        "source_ref",
        "survivor_memory_id",
        "target_id",
        "task_id",
        "to_id",
        "trace_id",
        "workflow_id",
    }
)


def test_the_seven_sensitivity_rank_tables_equal_the_kernel_table() -> None:
    root = Path(__file__).resolve().parents[2] / "apps" / "api" / "src" / "alicebot_api"
    files = [
        "vnext_brain.py",
        "vnext_consolidation.py",
        "vnext_connections.py",
        "vnext_contradictions.py",
        "vnext_rollups.py",
        "vnext_scheduler.py",
        "vnext_projects.py",
    ]
    found = 0
    for name in files:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
            if "highly_sensitive" not in keys or "regulated" not in keys:
                continue
            table = ast.literal_eval(node)
            assert table == SENSITIVITY_RANK
            found += 1
    assert found == 7
    assert brain.SENSITIVITY_RANK == SENSITIVITY_RANK
    assert consolidation.SENSITIVITY_RANK == SENSITIVITY_RANK


def test_labels_raised_events_carry_no_text_or_ids() -> None:
    payload = labels_raised_payload(
        cause="input_relabelled",
        previous={"domain": "unknown", "sensitivity": "public", "project_scope": [ALPHA], "project_floor": []},
        new={"domain": "health", "sensitivity": "confidential", "project_scope": [ALPHA], "project_floor": [BETA]},
    )
    assert set(payload) == {"cause", "previous", "new"}
    for side in (payload["previous"], payload["new"]):
        assert set(side) == {"domain", "sensitivity", "project_scope", "project_floor"}
    blob = str(payload)
    assert "canonical" not in blob
    assert "title" not in blob


def test_project_floor_blocks_a_locked_key_and_does_not_change_an_empty_floor() -> None:
    identity = AgentIdentity(
        agent_id="reader",
        permission_profile="read_only_agent",
        project_scope=(ALPHA,),
        project_scope_locked=True,
        auth="api_key",
    )
    blocked = evaluate_agent_policy(
        identity=identity,
        action="artifact.get",
        domains=("unknown",),
        sensitivity_allowed=("public",),
        project_scope=(ALPHA,),
        project_floor=(BETA,),
        require_explicit_project_scope=True,
    )
    assert blocked.decision == "blocked"
    assert "project_floor_binding_violation" in blocked.reasons
    allowed = evaluate_agent_policy(
        identity=identity,
        action="artifact.get",
        domains=("unknown",),
        sensitivity_allowed=("public",),
        project_scope=(ALPHA,),
        project_floor=(ALPHA,),
        require_explicit_project_scope=True,
    )
    assert allowed.decision == "allowed"
    assert project_floor_within((), (ALPHA,)) is True
    untouched = evaluate_agent_policy(
        identity=identity,
        action="artifact.get",
        domains=("unknown",),
        sensitivity_allowed=("public",),
        project_scope=(ALPHA,),
        require_explicit_project_scope=True,
    )
    assert untouched.decision == "allowed"


def test_a_global_row_with_a_floor_is_hidden_from_the_other_project() -> None:
    assert project_scopes_overlap((), (GLOBAL_PROJECT_MARKER, ALPHA), floor=(BETA,)) is False
    assert project_scopes_overlap((), (GLOBAL_PROJECT_MARKER, BETA), floor=(BETA,)) is True
    assert project_scopes_overlap((), (GLOBAL_PROJECT_MARKER, ALPHA), floor=("Alice",)) is True
    assert project_scopes_overlap((), (GLOBAL_PROJECT_MARKER,)) is True
    assert project_scopes_overlap((ALPHA,), (ALPHA,)) is True
    original = _memory("plain", metadata_json={"project_scope": [ALPHA]})
    derived = _memory(
        "derived",
        metadata_json={"source_id": "s", "project_scope": [ALPHA], "project_floor": [BETA]},
    )
    assert group_scope(original, kind="memory") == group_scope(
        _memory("same", metadata_json={"project_scope": [ALPHA]}), kind="memory"
    )
    assert BETA.lower() in group_scope(derived, kind="memory") or "prj_" + "b" * 16 in group_scope(derived, kind="memory")


def test_v2_id_keys_are_the_previous_set() -> None:
    assert V2_ID_KEYS == frozenset(_ID_KEYS)


def test_rank_table_matches_the_spec_order() -> None:
    assert SENSITIVITY_RANK["public"] < SENSITIVITY_RANK["unknown"] == SENSITIVITY_RANK["internal"]
    assert SENSITIVITY_RANK["sacred"] == SENSITIVITY_RANK["regulated"]
    assert "health" in RESTRICTED_DOMAINS
