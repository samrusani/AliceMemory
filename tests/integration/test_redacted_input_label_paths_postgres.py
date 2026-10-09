"""On PostgreSQL every path that decides a derived row's label agrees about a row built from a redacted row.

The random graphs of ``tests/unit/test_redacted_input_label_paths.py`` are stored in real tables, one user per seed, with a
redacted row at every depth. The bulk kernel reads the tables as the doctor does, the guard reads them through the store as a
restricted request does (counts, admission, and the exact label of each derived row), and the SQL prefilter runs as the
full-text stage runs it. An oracle that states the rule in plain recursion judges all of them.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api import vnext_label_guard as guards
from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_derived_labels import SENSITIVITY_RANK, identifier, is_derived
from alicebot_api.vnext_label_repair import classify_stored_labels, load_postgres_label_tables
from alicebot_api.vnext_label_sql import hidden_memory_input_sql
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.unit.redacted_graph_support import CEILINGS, REGULATED, Graph, random_graph
from tests.unit.test_redacted_input_label_paths import first_memory_input_is_redacted

SEEDS = range(12)


def _store_graph(urls, user, graph: Graph) -> None:
    with user_connection(urls["app"], user) as conn, without_insert_floor():
        ContinuityStore(conn).create_user(user, f"paths-{user}@example.invalid", "Synthetic paths")
        store = PostgresVNextStore(conn)
        for node in graph.nodes:
            row = node.row
            if node.kind == "source":
                store.create_source({"id": str(row["id"]), "source_type": "note", "title": "s", "content_hash": str(uuid4()),
                                     "domain": "project", "sensitivity": row["sensitivity"], "metadata_json": row["metadata_json"]})
            elif node.kind == "memory":
                store.create_memory({"id": str(row["id"]), "memory_key": str(row["id"]), "canonical_text": "text", "status": "active",
                                     "domain": "project", "sensitivity": row["sensitivity"], "metadata_json": row["metadata_json"],
                                     "value": row.get("value") or {}})
            else:
                store.create_artifact({"id": str(row["id"]), "artifact_type": row["artifact_type"], "title": "report",
                                       "content_markdown": "text", "domain": "project", "sensitivity": row["sensitivity"],
                                       "metadata_json": row["metadata_json"]})


@pytest.mark.parametrize("ceiling", CEILINGS, ids=lambda value: value[-1])
def test_postgres_tables_store_readers_and_sql_prefilter_agree_with_the_oracle(label_harness, ceiling):
    urls = label_harness.urls
    seen = {"unverified": 0, "rejected": 0, "hidden": 0}
    for seed in SEEDS:
        user = uuid4()
        graph = random_graph(seed, id_base=0xA0000 + seed * 1000)
        _store_graph(urls, user, graph)
        contained, ranks = graph.contained(), graph.ranks()
        hidden = graph.hidden_from(ceiling)
        with user_connection(urls["app"], user) as conn:
            store = PostgresVNextStore(conn)
            # The bulk kernel over the tables, as the doctor reads them.
            below, unverified = classify_stored_labels(load_postgres_label_tables(conn))
            expected_unverified = {
                str(node.row["id"]) for node in graph.nodes if contained[node.key] and is_derived(node.kind, node.row)
            }
            assert {identifier(item) for ids in unverified.values() for item in ids} == {identifier(i) for i in expected_unverified}, seed
            seen["unverified"] += len(expected_unverified)
            # The SQL partition: never a row the kernel reads, always a row whose first listed memory is redacted.
            sql = hidden_memory_input_sql(ceiling, sqlite=False)
            with conn.cursor() as cur:
                cur.execute(f"SELECT m.id::text AS id FROM memories m WHERE NOT ({sql})")  # nosec B608 - closed kernel constants
                rejected = {identifier(row["id"]) for row in cur.fetchall()}
            hidden_memories = {identifier(key[1]) for key in hidden if key[0] == "memory"}
            assert rejected <= hidden_memories, (seed, ceiling, rejected - hidden_memories)
            sure = first_memory_input_is_redacted(graph)
            assert sure <= rejected, (seed, ceiling, sure - rejected)
            seen["rejected"] += len(rejected)
            seen["hidden"] += len(hidden)
            # The guard over the store, for each kind and both bulk paths.
            for kind in ("memory", "artifact"):
                wanted = {identifier(node.row["id"]) for node in graph.nodes if node.kind == kind and node.key not in hidden}
                for domains in ((), ("project",)):
                    with guards.label_read_scope(store):
                        guard = guards.LabelGuard.for_filters(store, domains, ceiling)
                        counts = guard.readable_status_counts(kind)
                        assert sum(counts.values()) == len(wanted), (seed, ceiling, kind, domains, counts)
                    with guards.label_read_scope(store):
                        guard = guards.LabelGuard.for_filters(store, domains, ceiling)
                        rows = [row for batch in store.iter_label_rows(kind) for row in batch]
                        admitted = {identifier(row["id"]) for row in guard.admit_rows(kind, rows)}
                        assert admitted == wanted, (seed, ceiling, kind, domains)
            # The exact label of each derived row, in one shared request and on its own.
            derived = [node for node in graph.nodes if is_derived(node.kind, node.row)]
            with guards.label_read_scope(store):
                guard = guards.LabelGuard(store, active=True, sensitivity_allowed=CEILINGS[-1])
                for node in derived:
                    row = store.read_label_rows(node.kind, [identifier(node.row["id"])])[0]
                    effective = guard.effective_row(node.kind, row)
                    expected = (contained[node.key], REGULATED if contained[node.key] else ranks[node.key])
                    assert (bool(effective["unverified"]), SENSITIVITY_RANK[effective["sensitivity"]]) == expected, (seed, node.kind)
    # The graphs gave every path something to decide, so agreement above is not agreement about nothing.
    assert seen["unverified"] > 30 and seen["rejected"] > 5 and seen["hidden"] > 30, seen
