"""The faster reverse search preserves the previous superset and exact closure."""

from uuid import uuid4

from alicebot_api.db import user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_writes import _compact_id, _like_clause, _postgres_dependants, walk_dependants
from alicebot_api.vnext_store import PostgresVNextStore
from tests.unit.test_encoded_label_dependencies import encoded_object


def test_the_regex_candidate_search_matches_the_previous_scan_for_aliases_and_encoded_objects(migrated_database_urls):
    user = uuid4()
    roots = [str(uuid4()) for _ in range(3)]
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"reverse-{user}@example.invalid", "Reverse")
        store = PostgresVNextStore(conn)
        expected = set()
        for index, root in enumerate(roots):
            for variant, reference in enumerate(
                (root, root.upper(), root.replace("-", ""), "{" + root.upper() + "}", encoded_object("source_id", root))
            ):
                row = store.create_memory(
                    {
                        "memory_key": f"alias.{index}.{variant}",
                        "canonical_text": "Synthetic alias",
                        "domain": "project",
                        "sensitivity": "public",
                        "metadata_json": {"source_id": reference},
                    }
                )
                expected.add(str(row["id"]))
        decoy = store.create_memory(
            {
                "memory_key": "decoy",
                "canonical_text": "Synthetic decoy",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"note": roots[0]},
            }
        )
        unrelated = store.create_memory(
            {
                "memory_key": "unrelated",
                "canonical_text": "Synthetic unrelated",
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"note": encoded_object("source_id", str(uuid4()))},
            }
        )
        compacts = [_compact_id(root) for root in roots]
        text, _ = _like_clause("metadata_json::text", len(compacts), qmark=False)
        value, _ = _like_clause("value::text", len(compacts), qmark=False)
        previous = conn.execute(
            f"SELECT id::text AS id FROM memories WHERE ({text}) OR ({value}) "
            "OR strpos(metadata_json::text,chr(92))>0 OR strpos(value::text,chr(92))>0",
            [*(f"%{item}%" for item in compacts), *(f"%{item}%" for item in compacts)],
        ).fetchall()
        current = _postgres_dependants(store, "memories", "memory", compacts, with_value=True)
        assert {row["id"] for row in current} == {row["id"] for row in previous}
        assert {row["id"] for row in walk_dependants(store, roots)} == expected
        assert str(decoy["id"]) not in expected
        assert str(unrelated["id"]) not in expected


def test_multiple_frontier_batches_keep_memory_chains_and_both_direct_loop_columns(migrated_database_urls):
    user = uuid4()
    root = str(uuid4())
    with user_connection(migrated_database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"frontier-{user}@example.invalid", "Frontier")
        store = PostgresVNextStore(conn)
        store.lock_graph_mutation()
        store.create_source(
            {
                "id": root,
                "source_type": "note",
                "title": "Synthetic source",
                "content_hash": str(user),
                "domain": "project",
                "sensitivity": "public",
            }
        )
        roots = [str(uuid4()) for _ in range(220)]
        conn.execute(
            """INSERT INTO memories(id,user_id,memory_key,value,source_event_ids,canonical_text,
                                    status,domain,sensitivity,metadata_json)
            SELECT id,app.current_user_id(),'frontier.'||id,'{}'::jsonb,'[]'::jsonb,'Synthetic frontier',
                'active','project','public',
                jsonb_build_object('source_id',%s::text) FROM unnest(%s::uuid[]) id""",
            (root, roots),
        )
        descendants = []
        for index in (0, 199, 219):
            row = store.create_memory(
                {
                    "memory_key": f"descendant.{index}",
                    "canonical_text": "Synthetic descendant",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"derived_from": {"v": 1, "memories": [roots[index]], "counts": {"memories": 1}}},
                }
            )
            descendants.append(str(row["id"]))
        direct_source = store.create_open_loop(
            {
                "title": "Synthetic source loop",
                "source_id": root,
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"discovered_by": "synthetic"},
            }
        )
        direct_memory = store.create_open_loop(
            {
                "title": "Synthetic memory loop",
                "source_id": root,
                "memory_id": roots[-1],
                "domain": "project",
                "sensitivity": "public",
                "metadata_json": {"discovered_by": "synthetic"},
            }
        )
        expected = {*roots, *descendants, str(direct_source["id"]), str(direct_memory["id"])}
        assert {row["id"] for row in walk_dependants(store, [root])} == expected
        memory_candidates = _postgres_dependants(
            store, "open_loops", "open_loop", [_compact_id(roots[-1])], with_value=False
        )
        assert str(direct_memory["id"]) in {row["id"] for row in memory_candidates}
