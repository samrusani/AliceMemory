"""Restore, promotion and settled-label regressions from the security review."""

from __future__ import annotations

import json
import sqlite3

import pytest

from alicebot_api.onramp import bootstrap_database, main as onramp_main
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_derived_domain_backfill import plan_relabels
from tests.unit.per_project_s2_support import add_memory
from tests.unit.test_derived_domain_fence import USER


@pytest.mark.parametrize("existing_destination", (False, True))
def test_restore_repairs_derived_rows_before_publication(tmp_path, monkeypatch, existing_destination):
    from alicebot_api import sqlite_schema

    source = tmp_path / "old.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    backup = tmp_path / "backup.jsonl"
    # Build the old backup without the new repair, as a previous release did.
    with monkeypatch.context() as patch:
        patch.setattr(sqlite_schema, "_relabel_derived_domains", lambda conn: None)
        bootstrap_database(source, user_id=USER, user_email="local@alice")
        with sqlite_user_connection(source, USER) as conn:
            store = SQLiteVNextStore(conn, USER)
            health = add_memory(store, key="health", text="A restricted observation", domain="health")
            derived = store.create_memory(
                {
                    "memory_key": "derived",
                    "canonical_text": "A restricted summary",
                    "status": "active",
                    "domain": "unknown",
                    "sensitivity": "public",
                    "metadata_json": {"consolidation": {"cluster_member_ids": [health["id"]]}},
                }
            )
        assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    if existing_destination:
        bootstrap_database(destination, user_id=USER, user_email="local@alice")
    argv = ["import", "--in", str(backup), "--db", str(destination), "--user-id", USER]
    assert onramp_main(argv) == 0
    # Do not open through bootstrap here: publication itself must be safe.
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT domain FROM memories WHERE id = ?", (derived["id"],)).fetchone()[0] == "health"
    from uuid import UUID
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext
    from alicebot_api.onramp import sqlite_url_for_path
    from alicebot_api.vnext_agent_keys import create_agent_key

    with sqlite_user_connection(destination, USER) as conn:
        _, raw = create_agent_key(
            SQLiteVNextStore(conn, USER), user_id=USER, agent_id="restore-reader", permission_profile="read_only_agent"
        )
    monkeypatch.setenv("ALICE_AGENT_API_KEY", raw)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    from alicebot_api.mcp_tools import MCPToolError

    with pytest.raises(MCPToolError, match="requested explanation is unavailable"):
        call_mcp_tool(
            MCPRuntimeContext(database_url=sqlite_url_for_path(destination), user_id=UUID(USER)),
            name="alice_explain",
            arguments={"memory_id": str(derived["id"])},
        )
    assert onramp_main([*argv, "--mode", "skip"]) == 0
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT domain FROM memories WHERE id = ?", (derived["id"],)).fetchone()[0] == "health"
    # A logged label repair may reconcile only that label on a repeated import.
    with sqlite3.connect(destination) as conn:
        conn.execute("UPDATE memories SET canonical_text = ? WHERE id = ?", ("Changed local content", derived["id"]))
    assert onramp_main([*argv, "--mode", "skip"]) != 0
    with sqlite3.connect(destination) as conn:
        assert (
            conn.execute("SELECT canonical_text FROM memories WHERE id = ?", (derived["id"],)).fetchone()[0]
            == "Changed local content"
        )


@pytest.mark.parametrize("downstream", ("a", "z"))
def test_repair_uses_settled_input_labels_independently_of_id_order(downstream):
    def row(identifier, domain="unknown", **metadata):
        return {"id": identifier, "user_id": "u", "domain": domain, "metadata_json": metadata}

    tables = {
        "sources": [row("health", "health"), row("legal", "legal")],
        "generated_artifacts": [
            row(downstream, input_summary={"source_ids": ["health"], "artifact_ids": ["b", "c"]}),
            row("b", input_summary={"source_ids": ["legal"]}),
            row("c", input_summary={"source_ids": ["legal"]}),
        ],
    }
    updates = plan_relabels(tables)
    assert {item[2]: item[3] for item in updates}[downstream] == "legal"
    assert len(updates) == len({item[:3] for item in updates})


@pytest.mark.parametrize("reference", ("value", "metadata"))
def test_promoted_artifact_memory_follows_repaired_artifact(reference):
    memory = {"id": "promoted", "user_id": "u", "domain": "unknown", "value": {"kind": "promoted_artifact"}}
    if reference == "value":
        memory["value"]["artifact_id"] = "report"
    else:
        memory["metadata_json"] = {"source_artifact_id": "report"}
    tables = {
        "sources": [{"id": "source", "user_id": "u", "domain": "health"}],
        "generated_artifacts": [
            {
                "id": "report",
                "user_id": "u",
                "domain": "unknown",
                "metadata_json": {"input_summary": {"source_ids": ["source"]}},
            }
        ],
        "memories": [memory],
    }
    assert {item[2]: item[3] for item in plan_relabels(tables)} == {"report": "health", "promoted": "health"}


def test_repair_records_changed_rows_once(tmp_path):
    from alicebot_api.vnext_derived_domain_backfill import relabel_sqlite

    path = tmp_path / "audit.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        source = add_memory(store, key="health", text="Private observation", domain="health")
        derived = store.create_memory(
            {
                "memory_key": "derived",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "metadata_json": {"candidate_kind": "memory_rollup", "memory_ids": [source["id"]]},
            }
        )
        conn.execute("DELETE FROM alice_schema_state WHERE key LIKE 'derived_restricted_domains_%'")
        relabel_sqlite(conn)
        relabel_sqlite(conn)
        rows = conn.execute(
            "SELECT target_id, payload_json FROM event_log WHERE event_type = 'memory.domain_relabelled'"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert str(row["target_id"]) == str(derived["id"])
        assert json.loads(row["payload_json"])["domain"] == "health"


def test_staleness_report_keeps_title_sensitivity(monkeypatch):
    from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
    from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
    from tests.unit.test_vnext_scheduler import _staleness_store

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    store = _staleness_store()
    store.memories[0]["sensitivity"] = "highly_sensitive"
    store.memories[0]["title"] = "Private fixture title"
    result = VNextSchedulerService(store).run_now(
        SchedulerRunRequest(
            workflow_type="staleness_sweep",
            sensitivity_allowed=ALL_SENSITIVITY,
            generated_for="2026-07-04",
            options={"reference_time": "2026-07-04T03:30:00Z"},
        )
    )
    assert "Private fixture title" in result["artifact"]["content_markdown"]
    assert result["artifact"]["sensitivity"] == "highly_sensitive"


def test_nonsettling_cycles_are_bounded_and_not_published(monkeypatch):
    from alicebot_api import vnext_derived_domain_backfill as repair

    selector = repair.derived_domain
    calls = 0

    def counted_selector(rows, *, fallback):
        nonlocal calls
        calls += 1
        assert calls < 100, "repair exceeded the bounded change budget"
        return selector(rows, fallback=fallback)

    monkeypatch.setattr(repair, "derived_domain", counted_selector)
    # Each row copies the next. Revisiting this inconsistent odd cycle
    # oscillates unless the repair detects its bounded-work limit.
    tables = {
        "generated_artifacts": [
            {
                "id": key,
                "user_id": "u",
                "domain": domain,
                "metadata_json": {"input_summary": {"artifact_ids": [parent]}},
            }
            for key, parent, domain in (("a", "b", "health"), ("b", "c", "legal"), ("c", "a", "health"))
        ]
    }
    with pytest.raises(ValueError, match="did not settle"):
        repair.plan_relabels(tables)
    assert [row["domain"] for row in tables["generated_artifacts"]] == ["health", "legal", "health"]


def _bounded_selector(monkeypatch, limit=200):
    """Fail, rather than hang, if the repair ever stops bounding its own work."""
    from alicebot_api import vnext_derived_domain_backfill as repair

    selector = repair.derived_domain
    calls = 0

    def counted_selector(rows, *, fallback):
        nonlocal calls
        calls += 1
        assert calls < limit, "repair exceeded the bounded change budget"
        return selector(rows, fallback=fallback)

    monkeypatch.setattr(repair, "derived_domain", counted_selector)


def _odd_cycle(size):
    """Each row copies the next one and the labels alternate, so an odd cycle never settles."""
    keys = [chr(ord("a") + index) for index in range(size)]
    return {
        "generated_artifacts": [
            {
                "id": key,
                "user_id": "u",
                "domain": "health" if index % 2 == 0 else "legal",
                "metadata_json": {"input_summary": {"artifact_ids": [keys[(index + 1) % size]]}},
            }
            for index, key in enumerate(keys)
        ]
    }


def test_a_nonsettling_cycle_fails_with_a_message_that_names_the_rows_and_the_way_out(monkeypatch):
    from alicebot_api.vnext_derived_domain_backfill import DerivedDomainRepairError

    _bounded_selector(monkeypatch)
    with pytest.raises(ValueError) as caught:
        plan_relabels(_odd_cycle(3))
    assert isinstance(caught.value, DerivedDomainRepairError)
    message = str(caught.value)
    for expected in (
        "derived domain repair did not settle: derived rows record each other as inputs in a cycle",
        "(rows: generated_artifacts a, generated_artifacts b, generated_artifacts c)",
        "The repair stopped before it changed any row.",
        "Remove the circular input references from those rows, or restore a backup made before they were added",
    ):
        assert expected in message, expected
    # A long cycle names five rows and counts the rest, so the message stays one readable line.
    _bounded_selector(monkeypatch)
    with pytest.raises(DerivedDomainRepairError) as long_cycle:
        plan_relabels(_odd_cycle(9))
    named = long_cycle.value.args[0].partition("(rows: ")[2].partition(")")[0]
    assert named.count("generated_artifacts ") == 5 and named.endswith(" and 4 more"), named
    assert "\n" not in long_cycle.value.args[0]


def test_every_open_of_a_vault_holding_a_cycle_raises_the_clear_error_and_changes_nothing(tmp_path, monkeypatch):
    from alicebot_api.vnext_derived_domain_backfill import REPAIR_STATE_KEY, DerivedDomainRepairError

    path = tmp_path / "cycle.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        ids = sorted(add_memory(store, key=name, text=f"Row {name}")["id"] for name in "abc")
        # Each memory records the next as its consolidation input, and the labels alternate around the cycle.
        # The ids are sorted so the repair meets them in the order that oscillates.
        for index, (row_id, domain) in enumerate(zip(ids, ("health", "legal", "health"))):
            parent = ids[(index + 1) % 3]
            conn.execute(
                "UPDATE memories SET domain = ?, metadata_json = ? WHERE id = ?",
                (domain, json.dumps({"consolidation": {"cluster_member_ids": [parent]}}), row_id),
            )
        # A vault from before the repair has no stamp, so every open runs it again.
        conn.execute("DELETE FROM alice_schema_state WHERE key = ?", (REPAIR_STATE_KEY,))
    _bounded_selector(monkeypatch)
    for _ in range(2):
        with pytest.raises(DerivedDomainRepairError, match="did not settle") as caught:
            bootstrap_database(path, user_id=USER, user_email="local@alice")
        assert all(f"memories {row_id}" in str(caught.value) for row_id in ids)
    with pytest.raises(DerivedDomainRepairError, match="did not settle"):
        with sqlite_user_connection(path, USER):
            pass
    with sqlite3.connect(path) as raw:
        assert [row[0] for row in raw.execute("SELECT domain FROM memories ORDER BY id")] == ["health", "legal", "health"]
        assert raw.execute("SELECT count(*) FROM event_log WHERE event_type LIKE '%.domain_relabelled'").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM alice_schema_state WHERE key = ?", (REPAIR_STATE_KEY,)).fetchone()[0] == 0


def test_import_of_a_backup_holding_a_cycle_stops_before_publication(tmp_path, monkeypatch):
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    backup = tmp_path / "backup.jsonl"
    bootstrap_database(source, user_id=USER, user_email="local@alice")
    with sqlite_user_connection(source, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        ids = sorted(add_memory(store, key=name, text=f"Row {name}")["id"] for name in "abc")
        # The vault is stamped as repaired, so it opens and exports. Its rows still form the cycle that
        # oscillates, and only the restore repair meets it.
        for index, (row_id, domain) in enumerate(zip(ids, ("health", "legal", "health"))):
            conn.execute(
                "UPDATE memories SET domain = ?, metadata_json = ? WHERE id = ?",
                (domain, json.dumps({"consolidation": {"cluster_member_ids": [ids[(index + 1) % 3]]}}), row_id),
            )
    assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    _bounded_selector(monkeypatch)
    argv = ["import", "--in", str(backup), "--db", str(destination), "--user-id", USER]
    output = StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        assert onramp_main(argv) == 1
    assert "restore_failed" in output.getvalue()
    assert "no records were written" in output.getvalue()
    # Nothing is published and no staged file is left beside the destination.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["backup.jsonl", "source.sqlite3"]


def test_sqlite_repair_follows_available_artifact_and_leaves_missing_inputs(tmp_path):
    from alicebot_api.vnext_derived_domain_backfill import relabel_sqlite

    path = tmp_path / "promoted.sqlite3"
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        # The local product does not store artifacts. Imported references to
        # absent artifacts are not guessed from copied text.
        missing = store.create_memory(
            {
                "memory_key": "missing",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "value": {"kind": "promoted_artifact", "artifact_id": "absent"},
            }
        )
        relabel_sqlite(conn, restoring=True)
        assert store.get_memory(str(missing["id"]))["domain"] == "unknown"
        # Exercise the shared SQLite repair on a synthetic artifact-bearing schema.
        conn.execute("CREATE TABLE generated_artifacts (id TEXT, user_id TEXT, domain TEXT, metadata_json TEXT)")
        health = add_memory(store, key="input", text="Private observation", domain="health")
        conn.execute(
            "INSERT INTO generated_artifacts VALUES (?, ?, ?, ?)",
            ("report", USER, "unknown", json.dumps({"input_summary": {"memory_ids": [str(health["id"])]}})),
        )
        copied = store.create_memory(
            {
                "memory_key": "copy",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "value": {"kind": "promoted_artifact", "artifact_id": "report"},
            }
        )
        relabel_sqlite(conn, restoring=True)
        assert store.get_memory(str(copied["id"]))["domain"] == "health"
        assert conn.execute("SELECT domain FROM generated_artifacts").fetchone()["domain"] == "health"


@pytest.mark.parametrize("reference", ("value", "metadata"))
def test_promoted_artifact_uuid_aliases_resolve(reference):
    from uuid import UUID
    identifier = "aaaaaaaa-1234-5678-9000-000000000001"
    memory = {"id": "copy", "user_id": "u", "domain": "unknown",
              "value": {"kind": "promoted_artifact"}}
    if reference == "value":
        memory["value"]["artifact_id"] = UUID(identifier).hex.upper()
    else:
        memory["metadata_json"] = {"source_artifact_id": UUID(identifier).hex.upper()}
    tables = {"generated_artifacts": [{"id": identifier, "user_id": "u", "domain": "health"}],
              "memories": [memory]}
    assert plan_relabels(tables) == [("memories", "u", "copy", "health")]


def _chain(size):
    """Each row reads the next one and the labels alternate, with no cycle. The ids sort in reading order, which is the
    order in which a repair that revisits a row after each change to its input needs the most changes."""
    keys = [f"row{index:05d}" for index in range(size)]
    return {
        "generated_artifacts": [
            {
                "id": key,
                "user_id": "u",
                "domain": "health" if index % 2 == 0 else "legal",
                "metadata_json": {"input_summary": {"artifact_ids": keys[index + 1:index + 2]}},
            }
            for index, key in enumerate(keys)
        ]
    }


@pytest.mark.parametrize("size", (16, 3000))
def test_a_chain_without_a_cycle_settles_reading_each_row_once(monkeypatch, size):
    """An outside review of #554 found a chain of 16 derived rows with alternating labels refused as a cycle: the repair
    revisited each row after every change to its input and counted the changes against a bound for the whole graph,
    so a long chain ran out of changes. Each row is now labelled after the row it reads, once, so every row of a chain of
    any length takes the label at its end and the selector is called once for each row.

    Mutations: label the groups in reverse (``reversed(_input_groups(inputs))``: each row takes its input's label before
    that input is final); revisit rows outside the group (``& members`` dropped: a row is read again before its own
    group); go back to one bound for the whole graph with no groups (the chain of 16 is refused as before).
    """

    from alicebot_api import vnext_derived_domain_backfill as repair

    selector = repair.derived_domain
    calls = 0

    def counted_selector(rows, *, fallback):
        nonlocal calls
        calls += 1
        return selector(rows, fallback=fallback)

    monkeypatch.setattr(repair, "derived_domain", counted_selector)
    tables = _chain(size)
    final = "health" if (size - 1) % 2 == 0 else "legal"
    updates = repair.plan_relabels(tables)
    assert updates == [("generated_artifacts", "u", row["id"], final)
                       for row in tables["generated_artifacts"] if row["domain"] != final]
    assert calls == size


def test_a_cycle_that_settles_reads_its_rows_again():
    """Two reports record each other as inputs and the second also reads a health source. Both settle on health, which
    needs the first to be read again after the second changes.

    Mutation: never read a row again (``if dependant not in queued:`` made ``if False:``: the first stays unknown).
    """

    tables = {
        "sources": [{"id": "health", "user_id": "u", "domain": "health"}],
        "generated_artifacts": [
            {"id": "a", "user_id": "u", "domain": "unknown", "metadata_json": {"input_summary": {"artifact_ids": ["b"]}}},
            {"id": "b", "user_id": "u", "domain": "unknown",
             "metadata_json": {"input_summary": {"artifact_ids": ["a"], "source_ids": ["health"]}}},
        ],
    }
    assert {item[2]: item[3] for item in plan_relabels(tables)} == {"a": "health", "b": "health"}


def _seed_memory_chain(path, size=16):
    """A vault whose derived memories form the chain of the outside review: each records the next as its consolidation
    input, the labels alternate, and there is no cycle."""

    from uuid import UUID

    bootstrap_database(path, user_id=USER, user_email="local@alice")
    ids = [str(UUID(int=1000 + index)) for index in range(size)]
    with sqlite_user_connection(path, USER) as conn:
        store = SQLiteVNextStore(conn, USER)
        for index, memory_id in enumerate(ids):
            store.create_memory({
                "id": memory_id, "memory_key": f"chain.{index}", "memory_type": "semantic",
                "canonical_text": f"Derived observation {index}.", "status": "active",
                "domain": "health" if index % 2 == 0 else "legal",
                "metadata_json": {"consolidation": {"cluster_member_ids": ids[index + 1:index + 2]}},
            })
    return ids


def test_a_vault_holding_a_long_chain_opens_after_upgrade_and_settles(tmp_path):
    """The vault of the outside review, opened as a vault from before the repair: it opens, and every row of the chain
    takes the label at its end (legal), with one audit event for each row that changed."""

    from alicebot_api.vnext_derived_domain_backfill import REPAIR_STATE_KEY

    path = tmp_path / "chain.sqlite3"
    ids = _seed_memory_chain(path)
    with sqlite3.connect(path) as conn:
        # A vault from before the repair has no stamp, so opening it runs the repair.
        conn.execute("DELETE FROM alice_schema_state WHERE key = ?", (REPAIR_STATE_KEY,))
    bootstrap_database(path, user_id=USER, user_email="local@alice")
    with sqlite3.connect(path) as conn:
        assert {row[0] for row in conn.execute("SELECT domain FROM memories WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} == {"legal"}
        assert conn.execute("SELECT count(*) FROM event_log WHERE event_type = 'memory.domain_relabelled'").fetchone()[0] == 8


def test_a_backup_holding_a_long_chain_restores(tmp_path):
    """The backup of that vault restores into a fresh file, and the restored chain is settled."""

    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "restored.sqlite3"
    backup = tmp_path / "backup.jsonl"
    ids = _seed_memory_chain(source)
    assert onramp_main(["export", "--db", str(source), "--user-id", USER, "--out", str(backup)]) == 0
    assert onramp_main(["import", "--db", str(destination), "--user-id", USER, "--in", str(backup)]) == 0
    with sqlite3.connect(destination) as conn:
        assert {row[0] for row in conn.execute("SELECT domain FROM memories WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} == {"legal"}


def _report(name, reads, sources=()):
    refs = {"artifact_ids": list(reads)}
    if sources:
        refs["source_ids"] = list(sources)
    return {"id": name, "user_id": "u", "domain": "unknown", "metadata_json": {"input_summary": refs}}


def _keys(graph):
    return {("generated_artifacts", "u", name): {("generated_artifacts", "u", ref) for ref in reads}
            for name, reads in graph.items()}


def test_one_cycle_with_two_back_edges_is_one_group_and_settles():
    """r0 reads r1 and r2, r1 reads r2, and r2 reads r0 and a health source: one cycle with two edges back to r0. All three
    rows are one group and all three settle on health.

    Mutation: take the smaller of the two numbers out of the back-edge update (``lowest[key] = min(lowest[key],
    index[ref])`` made ``lowest[key] = index[ref]``: the cycle is split and no row is labelled).
    """

    from alicebot_api import vnext_derived_domain_backfill as repair

    graph = {"r0": ["r1", "r2"], "r1": ["r2"], "r2": ["r0"]}
    assert repair._input_groups(_keys(graph)) == [sorted(_keys(graph))]
    tables = {"sources": [{"id": "h", "user_id": "u", "domain": "health"}],
              "generated_artifacts": [_report("r0", ["r1", "r2"]), _report("r1", ["r2"]), _report("r2", ["r0"], ["h"])]}
    assert {item[2]: item[3] for item in repair.plan_relabels(tables)} == {"r0": "health", "r1": "health", "r2": "health"}


def test_rows_that_share_inputs_are_each_read_once(monkeypatch):
    """A ladder two rows wide and twelve deep, each row reading both rows below it, the bottom two reading a health
    source: no cycle, and every row is reached by many paths. Each row is one group and is read once.

    Mutation: count a row as new while it is off the stack (``if ref not in index:`` made ``if ref not in on_stack:``
    in ``_input_groups``: a row is walked again for every path to it, the groups repeat and the time grows with the
    number of paths).
    """

    from alicebot_api import vnext_derived_domain_backfill as repair

    depth = 12
    layers = [[f"L{level:02d}a", f"L{level:02d}b"] for level in range(depth)]
    graph = {name: (layers[level + 1] if level + 1 < depth else []) for level, layer in enumerate(layers) for name in layer}
    groups = repair._input_groups(_keys(graph))
    assert sorted(groups) == sorted([key] for key in _keys(graph))
    calls = 0
    selector = repair.derived_domain

    def counted(rows, *, fallback):
        nonlocal calls
        calls += 1
        return selector(rows, fallback=fallback)

    monkeypatch.setattr(repair, "derived_domain", counted)
    tables = {"sources": [{"id": "h", "user_id": "u", "domain": "health"}],
              "generated_artifacts": [_report(name, reads, [] if reads else ["h"]) for name, reads in graph.items()]}
    assert {item[3] for item in repair.plan_relabels(tables)} == {"health"} and calls == len(graph)


def _independent_groups(graph):
    """Strongly connected components by Kosaraju's two passes, written apart from the code under test."""

    order, seen = [], set()
    for root in sorted(graph):
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(sorted(graph[root])))]
        while stack:
            node, refs = stack[-1]
            for ref in refs:
                if ref in graph and ref not in seen:
                    seen.add(ref)
                    stack.append((ref, iter(sorted(graph[ref]))))
                    break
            else:
                stack.pop()
                order.append(node)
    readers = {node: set() for node in graph}
    for node, refs in graph.items():
        for ref in refs:
            if ref in graph:
                readers[ref].add(node)
    groups, placed = [], set()
    for root in reversed(order):
        if root in placed:
            continue
        group, todo = [], [root]
        placed.add(root)
        while todo:
            node = todo.pop()
            group.append(node)
            for reader in readers[node]:
                if reader not in placed:
                    placed.add(reader)
                    todo.append(reader)
        groups.append(sorted(group))
    return groups


def test_the_groups_match_an_independent_computation_on_random_graphs():
    """On 3,000 seeded random graphs of up to 12 rows (self loops, several cycles, inputs that are not derived rows), the
    groups are those of an independent computation, every row is in one group, and every group comes after each group
    it reads.

    Mutations: the two lowlink updates of ``_input_groups`` weakened or removed, and the visited check made
    ``if ref not in on_stack:``, each fail here as well as in the named tests.
    """

    import random

    from alicebot_api import vnext_derived_domain_backfill as repair

    rng = random.Random(560)
    for _ in range(3000):
        names = [f"n{index:02d}" for index in range(rng.randint(1, 12))]
        graph = {name: set(rng.sample(names + ["outside"], rng.randint(0, min(4, len(names) + 1)))) for name in names}
        keyed = {key: {ref for ref in refs if ref[2] != "outside"} | ({("sources", "u", "outside")} if "outside" in graph[key[2]] else set())
                 for key, refs in _keys(graph).items()}
        groups = repair._input_groups(keyed)
        assert sorted(sorted(key[2] for key in group) for group in groups) == sorted(_independent_groups(graph)), graph
        position = {key: index for index, group in enumerate(groups) for key in group}
        assert len(position) == len(graph)
        for key, refs in keyed.items():
            for ref in refs:
                if ref in position:
                    assert position[ref] <= position[key], graph


def test_a_cycle_is_refused_after_six_changes_for_each_of_its_rows_whatever_the_graph_around_it(monkeypatch):
    """A ring of three reports that read each other with alternating labels never settles: each read changes a label.
    Two hundred other reports in a chain come first. The chain is read once per row, and the ring is refused at its
    nineteenth read, one change past its budget of six for each of its three rows. The budget belongs to the cycle,
    not to the graph.

    Mutations: a budget for the whole graph (``len(component)`` made ``len(inputs)``: the ring is read about 1,400
    times); a smaller factor (``len(RESTRICTED_DOMAINS) + 1`` made ``2`` or ``len(RESTRICTED_DOMAINS)``); refuse at
    zero (``remaining_changes < 0`` made ``<= 0``).
    """

    from alicebot_api import vnext_derived_domain_backfill as repair

    calls = 0
    selector = repair.derived_domain

    def counted(rows, *, fallback):
        nonlocal calls
        calls += 1
        return selector(rows, fallback=fallback)

    monkeypatch.setattr(repair, "derived_domain", counted)
    chain = [f"c{index:04d}" for index in range(200)]
    ring = ["r0", "r1", "r2"]
    tables = {"generated_artifacts": [
        *[_report(name, chain[index + 1:index + 2]) for index, name in enumerate(chain)],
        *[{**_report(name, [ring[(index + 1) % 3]]), "domain": "health" if index % 2 == 0 else "legal"}
          for index, name in enumerate(ring)],
    ]}
    with pytest.raises(repair.DerivedDomainRepairError, match="did not settle"):
        repair.plan_relabels(tables)
    assert calls == len(chain) + 3 * (len(repair.RESTRICTED_DOMAINS) + 1) + 1
