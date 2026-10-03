"""PostgreSQL regressions for authoritative canonical project scope."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from alicebot_api.db import user_connection
from alicebot_api.mcp_tools import MCPRuntimeContext, call_mcp_tool
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_store import PostgresVNextStore


def _ids(rows: list[dict[str, object]]) -> set[str]:
    return {str(row["id"]) for row in rows}


def test_canonical_project_scope_precedes_empty_and_stale_legacy_values(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    stale_project = str(uuid4())
    canonical_project = str(uuid4())
    marker = f"canonical scope precedence {uuid4().hex}"

    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"scope-precedence-{user_id}@example.invalid",
            "Scope precedence",
        )
        store = PostgresVNextStore(conn)
        store.create_project({"id": stale_project, "name": "Stale project", "slug": f"stale-{stale_project}"})
        store.create_project(
            {
                "id": canonical_project,
                "name": "Canonical project",
                "slug": f"canonical-{canonical_project}",
            }
        )
        empty_metadata = {
            "project_scope": [],
            "project_id": stale_project,
            "agentic_memory": {"project_scope": [stale_project]},
        }
        canonical_metadata = {
            "project_scope": [canonical_project],
            "project_id": stale_project,
            "agentic_memory": {"project_scope": [stale_project]},
        }

        with conn.cursor() as cur:
            memory_ids: list[str] = []
            for suffix, metadata in (("empty", empty_metadata), ("canonical", canonical_metadata)):
                cur.execute(
                    """
                    INSERT INTO memories (
                      user_id, memory_key, value, status, source_event_ids,
                      memory_type, title, canonical_text, domain, sensitivity,
                      project_id, metadata_json
                    ) VALUES (
                      %s, %s, '{}'::jsonb, 'active', '[]'::jsonb,
                      'semantic', %s, %s, 'project', 'internal',
                      %s::uuid, %s
                    )
                    RETURNING id
                    """,
                    (
                        user_id,
                        f"scope.precedence.memory.{suffix}.{uuid4().hex}",
                        marker,
                        f"{marker} {suffix}",
                        stale_project,
                        Jsonb(metadata),
                    ),
                )
                memory_ids.append(str(cur.fetchone()["id"]))

        source_ids = [
            str(
                store.create_source(
                    {
                        "source_type": "manual_text",
                        "title": marker,
                        "content_hash": f"sha256:{uuid4().hex}",
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": metadata,
                    }
                )["id"]
            )
            for metadata in (empty_metadata, canonical_metadata)
        ]
        artifact_ids = [
            str(
                store.create_artifact(
                    {
                        "artifact_type": "daily_brief",
                        "title": marker,
                        "content_markdown": marker,
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": metadata,
                    }
                )["id"]
            )
            for metadata in (empty_metadata, canonical_metadata)
        ]
        open_loop_ids = [
            str(
                store.create_open_loop(
                    {
                        "title": marker,
                        "project_id": stale_project,
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": metadata,
                    }
                )["id"]
            )
            for metadata in (empty_metadata, canonical_metadata)
        ]

        stale_results = {
            "memory": _ids(store.list_memories(projects=(stale_project,), limit=10)),
            "source": _ids(store.search_sources(query=marker, scope_projects=(stale_project,), limit=10)),
            "artifact": _ids(store.list_artifacts(scope_projects=(stale_project,), limit=10)),
            "open_loop": _ids(store.list_open_loops(scope_projects=(stale_project,), limit=10)),
        }
        canonical_results = {
            "memory": _ids(store.list_memories(projects=(canonical_project,), limit=10)),
            "source": _ids(store.search_sources(query=marker, scope_projects=(canonical_project,), limit=10)),
            "artifact": _ids(store.list_artifacts(scope_projects=(canonical_project,), limit=10)),
            "open_loop": _ids(store.list_open_loops(scope_projects=(canonical_project,), limit=10)),
        }

    resource_ids = {
        "memory": memory_ids,
        "source": source_ids,
        "artifact": artifact_ids,
        "open_loop": open_loop_ids,
    }
    for resource_type, (empty_id, canonical_id) in resource_ids.items():
        assert empty_id not in stale_results[resource_type]
        assert canonical_id not in stale_results[resource_type]
        assert empty_id not in canonical_results[resource_type]
        assert canonical_results[resource_type] == {canonical_id}


def test_persisted_source_envelope_scope_precedes_stale_alias_for_source_and_chunk_search(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    marker = f"persisted source envelope {uuid4().hex}"
    stale_project = "stale-project"
    real_project = "real-project"
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"source-envelope-{user_id}@example.invalid",
            "Source envelope scope",
        )
        store = PostgresVNextStore(conn)

        def create_source_with_chunk(
            suffix: str,
            metadata_json: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            source = store.create_source(
                {
                    "source_type": "manual_text",
                    "title": f"{marker} {suffix}",
                    "content_hash": f"sha256:{uuid4().hex}",
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": metadata_json,
                }
            )
            chunk = store.create_source_chunk(
                {
                    "source_id": source["id"],
                    "chunk_index": 0,
                    "text": f"{marker} {suffix}",
                }
            )
            return source, chunk

        empty_source, _empty_chunk = create_source_with_chunk(
            "empty",
            {
                "project_id": stale_project,
                "metadata_json": {"project_scope": []},
            },
        )
        real_source, real_chunk = create_source_with_chunk(
            "real",
            {
                "project_id": stale_project,
                "metadata_json": {"project_scope": [real_project]},
            },
        )
        scalar_source, scalar_chunk = create_source_with_chunk(
            "scalar parity",
            {
                "project_id": stale_project,
                "metadata_json": {
                    "project_scope": [
                        [" Alpha "],
                        7,
                        7.0,
                        1e3,
                        -0.0,
                        True,
                        False,
                        1.5,
                        {"leak": "wrong-project"},
                        None,
                        " ",
                    ]
                },
            },
        )

        def source_ids(project: str) -> set[str]:
            return _ids(
                store.search_sources(
                    query=marker,
                    scope_projects=(project,),
                    limit=20,
                )
            )

        def chunk_ids(project: str) -> set[str]:
            return _ids(
                store.search_source_chunks(
                    query=marker,
                    scope_projects=(project,),
                    limit=20,
                )
            )

        assert source_ids(stale_project) == set()
        assert chunk_ids(stale_project) == set()
        assert source_ids(real_project) == {str(real_source["id"])}
        assert chunk_ids(real_project) == {str(real_chunk["id"])}
        for scalar_identity in ("alpha", "7", "1000", "0", "TRUE", "false"):
            assert source_ids(scalar_identity) == {str(scalar_source["id"])}
            assert chunk_ids(scalar_identity) == {str(scalar_chunk["id"])}
        for rejected_identity in ("1.5", "wrong-project"):
            assert source_ids(rejected_identity) == set()
            assert chunk_ids(rejected_identity) == set()
        assert str(empty_source["id"]) not in source_ids(real_project)


def test_persisted_source_nested_scope_presence_blocks_stale_alias_for_source_and_chunks(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    marker = f"persisted nested source presence {uuid4().hex}"
    stale_project = "stale-project"
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"nested-source-presence-{user_id}@example.invalid",
            "Nested source presence",
        )
        store = PostgresVNextStore(conn)

        def create_source_with_chunk(
            suffix: str,
            metadata_json: dict[str, object],
        ) -> tuple[dict[str, object], dict[str, object]]:
            source = store.create_source(
                {
                    "source_type": "manual_text",
                    "title": f"{marker} {suffix}",
                    "content_hash": f"sha256:{uuid4().hex}",
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": metadata_json,
                }
            )
            chunk = store.create_source_chunk(
                {
                    "source_id": source["id"],
                    "chunk_index": 0,
                    "text": f"{marker} {suffix}",
                }
            )
            return source, chunk

        valid_source, valid_chunk = create_source_with_chunk(
            "valid",
            {
                "project_id": stale_project,
                "agentic_memory": {"project_scope": " Alpha "},
                "agent_identity": {"project_scope": [7, 1e1, True]},
            },
        )
        invalid_sources: set[str] = set()
        invalid_chunks: set[str] = set()
        for index, nested in enumerate(
            (
                {"agentic_memory": {"project_scope": ["\t\n"]}},
                {"agent_identity": {"project_scope": None}},
                {"agentic_memory": {"project_scope": {"leak": stale_project}}},
                {"agent_identity": {"project_scope": 1.5}},
            )
        ):
            source, chunk = create_source_with_chunk(
                f"invalid-{index}",
                {"project_id": stale_project, **nested},
            )
            invalid_sources.add(str(source["id"]))
            invalid_chunks.add(str(chunk["id"]))

        def source_ids(project: str) -> set[str]:
            return _ids(store.search_sources(query=marker, scope_projects=(project,), limit=20))

        def chunk_ids(project: str) -> set[str]:
            return _ids(store.search_source_chunks(query=marker, scope_projects=(project,), limit=20))

        for accepted in ("alpha", "7", "10", "TRUE"):
            assert source_ids(accepted) == {str(valid_source["id"])}
            assert chunk_ids(accepted) == {str(valid_chunk["id"])}
        assert source_ids(stale_project) == set()
        assert chunk_ids(stale_project) == set()
        assert invalid_sources.isdisjoint(source_ids("alpha"))
        assert invalid_chunks.isdisjoint(chunk_ids("alpha"))


def test_generic_postgres_scope_filters_canonicalize_finite_integral_json_numbers(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    marker = f"numeric project scope parity {uuid4().hex}"
    metadata = {"project_scope": [1, 1.0, 1e3, -0.0, 0, True, False, 1.5, None, {"leak": "wrong"}]}
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"scope-numeric-{user_id}@example.invalid",
            "Numeric scope parity",
        )
        store = PostgresVNextStore(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories (
                  user_id, memory_key, value, status, source_event_ids,
                  memory_type, canonical_text, domain, sensitivity, metadata_json
                ) VALUES (
                  %s, %s, '{}'::jsonb, 'active', '[]'::jsonb,
                  'semantic', %s, 'project', 'internal', %s
                )
                RETURNING id
                """,
                (user_id, f"scope.numeric.{uuid4().hex}", marker, Jsonb(metadata)),
            )
            memory_id = str(cur.fetchone()["id"])
        artifact_id = str(
            store.create_artifact(
                {
                    "artifact_type": "daily_brief",
                    "title": marker,
                    "content_markdown": marker,
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": metadata,
                }
            )["id"]
        )
        open_loop_id = str(
            store.create_open_loop(
                {
                    "title": marker,
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": metadata,
                }
            )["id"]
        )

        def scoped(project: str) -> dict[str, set[str]]:
            return {
                "memory": _ids(store.list_memories(projects=(project,), limit=20)),
                "artifact": _ids(store.list_artifacts(scope_projects=(project,), limit=20)),
                "open_loop": _ids(store.list_open_loops(scope_projects=(project,), limit=20)),
            }

        expected = {
            "memory": {memory_id},
            "artifact": {artifact_id},
            "open_loop": {open_loop_id},
        }
        for accepted in ("1", "1000", "0", "true", "FALSE"):
            assert scoped(accepted) == expected
        for rejected in ("1.5", "wrong"):
            assert scoped(rejected) == {"memory": set(), "artifact": set(), "open_loop": set()}


def test_generic_postgres_nested_canonical_scope_is_scalar_aware_and_presence_authoritative(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    marker = f"nested canonical project scope {uuid4().hex}"
    stale_project = "stale-project"
    valid_metadata = {
        "project_id": stale_project,
        "agentic_memory": {"project_scope": " Alpha "},
        "agent_identity": {"project_scope": [7, 1e1, True]},
    }
    invalid_metadata = (
        {"project_id": stale_project, "agentic_memory": {"project_scope": []}},
        {"project_id": stale_project, "agent_identity": {"project_scope": None}},
        {"project_id": stale_project, "agentic_memory": {"project_scope": {"leak": stale_project}}},
        {"project_id": stale_project, "agent_identity": {"project_scope": 1.5}},
    )
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"nested-scope-{user_id}@example.invalid",
            "Nested canonical scope",
        )
        store = PostgresVNextStore(conn)

        def raw_memory(metadata: dict[str, object], suffix: str) -> str:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO memories (
                      user_id, memory_key, value, status, source_event_ids,
                      memory_type, canonical_text, domain, sensitivity, metadata_json
                    ) VALUES (
                      %s, %s, '{}'::jsonb, 'active', '[]'::jsonb,
                      'semantic', %s, 'project', 'internal', %s
                    )
                    RETURNING id
                    """,
                    (user_id, f"scope.nested.{suffix}.{uuid4().hex}", marker, Jsonb(metadata)),
                )
                return str(cur.fetchone()["id"])

        memory_ids = [raw_memory(valid_metadata, "valid")]
        artifact_ids = [
            str(
                store.create_artifact(
                    {
                        "artifact_type": "daily_brief",
                        "title": marker,
                        "content_markdown": marker,
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": valid_metadata,
                    }
                )["id"]
            )
        ]
        loop_ids = [
            str(
                store.create_open_loop(
                    {
                        "title": marker,
                        "domain": "project",
                        "sensitivity": "internal",
                        "metadata_json": valid_metadata,
                    }
                )["id"]
            )
        ]
        for index, metadata in enumerate(invalid_metadata):
            memory_ids.append(raw_memory(metadata, f"invalid-{index}"))
            artifact_ids.append(
                str(
                    store.create_artifact(
                        {
                            "artifact_type": "daily_brief",
                            "title": marker,
                            "content_markdown": marker,
                            "domain": "project",
                            "sensitivity": "internal",
                            "metadata_json": metadata,
                        }
                    )["id"]
                )
            )
            loop_ids.append(
                str(
                    store.create_open_loop(
                        {
                            "title": marker,
                            "domain": "project",
                            "sensitivity": "internal",
                            "metadata_json": metadata,
                        }
                    )["id"]
                )
            )

        def scoped(project: str) -> dict[str, set[str]]:
            return {
                "memory": _ids(store.list_memories(projects=(project,), limit=20)),
                "artifact": _ids(store.list_artifacts(scope_projects=(project,), limit=20)),
                "open_loop": _ids(store.list_open_loops(scope_projects=(project,), limit=20)),
            }

        expected = {
            "memory": {memory_ids[0]},
            "artifact": {artifact_ids[0]},
            "open_loop": {loop_ids[0]},
        }
        for accepted in ("alpha", "7", "10", "TRUE"):
            assert scoped(accepted) == expected
        assert scoped(stale_project) == {"memory": set(), "artifact": set(), "open_loop": set()}


def test_postgres_resume_filters_and_event_joins_precede_limits(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    target_project = "project-a"
    foreign_project = "project-b"
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"resume-prelimit-{user_id}@example.invalid",
            "Resume pre-limit",
        )
        store = PostgresVNextStore(conn)

        def memory(title: str, project: str, *, memory_type: str = "decision", status: str = "active"):
            return store.create_memory(
                {
                    "memory_key": f"resume.pg.{uuid4().hex}",
                    "value": {"text": title},
                    "status": status,
                    "memory_type": memory_type,
                    "title": title,
                    "canonical_text": title,
                    "summary": title,
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": {"project_scope": [project]},
                }
            )

        decision = memory("Release decision target", target_project)
        loop = store.create_open_loop(
            {
                "title": "Release loop target",
                "opened_at": "2030-07-10T12:00:00Z",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": [target_project]},
            }
        )
        old_memory = memory("Old target memory", target_project, memory_type="semantic")
        old_loop = store.create_open_loop(
            {
                "title": "Old target loop",
                "opened_at": "2029-01-01T00:00:00Z",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": [target_project]},
            }
        )
        foreign_memory = memory("Foreign memory", foreign_project, memory_type="semantic")
        foreign_loop = store.create_open_loop(
            {
                "title": "Foreign loop",
                "opened_at": "2029-01-01T00:00:00Z",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": [foreign_project]},
            }
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE memories SET created_at = %s, updated_at = %s WHERE id = %s",
                (datetime(2030, 7, 10, 12, tzinfo=UTC), datetime(2030, 7, 10, 12, tzinfo=UTC), decision["id"]),
            )
            for old in (old_memory, foreign_memory):
                cur.execute(
                    "UPDATE memories SET created_at = %s, updated_at = %s WHERE id = %s",
                    (datetime(2029, 1, 1, tzinfo=UTC), datetime(2029, 1, 1, tzinfo=UTC), old["id"]),
                )
        for target_type, target_id, minute in (
            ("memory", old_memory["id"], 1),
            ("open_loop", old_loop["id"], 2),
        ):
            store.append_event(
                {
                    "id": str(uuid4()),
                    "event_type": f"{target_type}.updated",
                    "actor_type": "system",
                    "target_type": target_type,
                    "target_id": target_id,
                    "occurred_at": datetime(2030, 7, 10, 12, minute, tzinfo=UTC),
                    "payload_json": {},
                }
            )
        for index in range(60):
            for target_type, target_id in (
                ("memory", foreign_memory["id"]),
                ("open_loop", foreign_loop["id"]),
            ):
                store.append_event(
                    {
                        "id": str(uuid4()),
                        "event_type": f"{target_type}.updated",
                        "actor_type": "system",
                        "target_type": target_type,
                        "target_id": target_id,
                        "occurred_at": datetime(2030, 7, 10, 13, index, tzinfo=UTC),
                        "payload_json": {},
                    }
                )

    payload = call_mcp_tool(
        MCPRuntimeContext(database_url=app_url, user_id=user_id),
        name="alice_resume",
        arguments={
            "project": target_project,
            "since": "2030-07-10T00:00:00Z",
            "until": "2030-07-11T00:00:00Z",
            "max_open_loops": 1,
            "max_recent_changes": 2,
        },
    )
    assert payload["brief"]["last_decision"]["id"] == str(decision["id"])
    assert [row["id"] for row in payload["brief"]["open_loops"]] == [str(loop["id"])]
    assert {row["target_id"] for row in payload["brief"]["recent_changes"]} == {
        str(old_memory["id"]),
        str(old_loop["id"]),
    }


def test_postgres_resume_query_filters_loop_rows_and_events_before_limits(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    project = "project-a"
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"resume-query-{user_id}@example.invalid",
            "Resume query",
        )
        store = PostgresVNextStore(conn)

        def loop(title: str, opened_at: str) -> dict[str, object]:
            return store.create_open_loop(
                {
                    "title": title,
                    "opened_at": opened_at,
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": {"project_scope": [project]},
                }
            )

        target = loop("Release matching loop", "2030-07-10T12:00:00Z")
        payload_only = loop("Payload-only older loop", "2030-07-10T11:00:00Z")
        store.append_event(
            {
                "id": str(uuid4()),
                "event_type": "open_loop.updated",
                "actor_type": "system",
                "target_type": "open_loop",
                "target_id": target["id"],
                "occurred_at": datetime(2030, 7, 10, 12, 1, tzinfo=UTC),
                "payload_json": {"nested": {"value": "Release lives in a nested payload."}},
            }
        )
        store.append_event(
            {
                "id": str(uuid4()),
                "event_type": "open_loop.updated",
                "actor_type": "system",
                "target_type": "open_loop",
                "target_id": payload_only["id"],
                "occurred_at": datetime(2030, 7, 10, 12, 2, tzinfo=UTC),
                "payload_json": {"items": ["Array Release match"]},
            }
        )
        noise_ids: set[str] = set()
        for index in range(62):
            hour_offset, minute = divmod(index, 60)
            noise = loop(
                f"Unrelated newer loop {index}",
                f"2030-07-10T{13 + hour_offset:02d}:{minute:02d}:00Z",
            )
            noise_ids.add(str(noise["id"]))
            store.append_event(
                {
                    "id": str(uuid4()),
                    "event_type": "open_loop.updated",
                    "actor_type": "system",
                    "target_type": "open_loop",
                    "target_id": noise["id"],
                    "occurred_at": datetime(2030, 7, 10, 14 + hour_offset, minute, tzinfo=UTC),
                    "payload_json": {"release": "completely unrelated value"},
                }
            )

    arguments = {
        "query": "release",
        "since": "2030-07-10T00:00:00Z",
        "until": "2030-07-11T00:00:00Z",
        "max_open_loops": 1,
        "max_recent_changes": 2,
    }
    for requested_project in (project, None):
        call_arguments = dict(arguments)
        if requested_project is not None:
            call_arguments["project"] = requested_project
        payload = call_mcp_tool(
            MCPRuntimeContext(database_url=app_url, user_id=user_id),
            name="alice_resume",
            arguments=call_arguments,
        )
        assert [row["id"] for row in payload["brief"]["open_loops"]] == [str(target["id"])]
        assert payload["brief"]["next_action"]["id"] == str(target["id"])
        assert {row["target_id"] for row in payload["brief"]["recent_changes"]} == {
            str(target["id"]),
            str(payload_only["id"]),
        }
        assert noise_ids.isdisjoint(row["id"] for row in payload["brief"]["open_loops"])
        assert noise_ids.isdisjoint(row["target_id"] for row in payload["brief"]["recent_changes"])


def test_postgres_open_loop_queries_use_ascii_literal_leaf_semantics(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    project = "project-a"
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"ascii-loop-query-{user_id}@example.invalid",
            "ASCII loop query",
        )
        store = PostgresVNextStore(conn)
        sequence = 0

        def create_loop(
            title: str,
            *,
            description: str | None = None,
            metadata: dict[str, object] | None = None,
        ) -> dict[str, object]:
            nonlocal sequence
            sequence += 1
            return store.create_open_loop(
                {
                    "title": title,
                    "description": description,
                    "opened_at": f"2030-07-10T10:{sequence:02d}:00Z",
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": {"project_scope": [project], **(metadata or {})},
                }
            )

        rows = {
            "title": create_loop("Release title"),
            "description": create_loop("Unrelated loop", description="Release description"),
            "next_action": create_loop("Unrelated loop", metadata={"next_action": "Release next action"}),
            "agentic_next_action": create_loop(
                "Unrelated loop",
                metadata={"agentic_memory": {"next_action": "Release agentic next action"}},
            ),
            "root_integer_next_action": create_loop(
                "Unrelated loop",
                metadata={"next_action": 8675309},
            ),
            "agentic_object_next_action": create_loop(
                "Unrelated loop",
                metadata={"agentic_memory": {"next_action": {"text": "object row sentinel"}}},
            ),
            "agentic_array_next_action": create_loop(
                "Unrelated loop",
                metadata={"agentic_memory": {"next_action": ["array row sentinel"]}},
            ),
            "arende": create_loop("Ärende row"),
            "strasse": create_loop("Straße row"),
            "percent": create_loop("100% complete"),
            "underscore": create_loop("Unrelated loop", description="under_score marker"),
            "backslash": create_loop("Unrelated loop", metadata={"next_action": r"path\segment"}),
        }
        event_loop = create_loop("Payload-only loop")
        event_payloads = {
            "split-leaves": {"items": ["alpha", "beta"]},
            "nested-positive": {"nested": {"value": "alpha beta in one nested leaf"}},
            "array-positive": {"items": ["alpha beta in one array leaf"]},
            "key-and-non-string-negative": {
                "alpha beta": 123,
                "flag": True,
                "nothing": None,
            },
            "release-nested": {"nested": {"value": "Release nested leaf"}},
            "release-array": {"items": ["Release array leaf"]},
            "arende-nested": {"nested": {"value": "Ärende nested leaf"}},
            "arende-array": {"items": ["Ärende array leaf"]},
            "strasse-nested": {"nested": {"value": "Straße nested leaf"}},
            "strasse-array": {"items": ["Straße array leaf"]},
            "percent-nested": {"nested": {"value": "100% nested leaf"}},
            "percent-array": {"items": ["100% array leaf"]},
            "underscore-nested": {"nested": {"value": "under_score nested leaf"}},
            "underscore-array": {"items": ["under_score array leaf"]},
            "backslash-nested": {"nested": {"value": r"path\nested"}},
            "backslash-array": {"items": [r"path\array"]},
            "next-action-object-payload": {"next_action": {"text": "payload object next action sentinel"}},
            "next-action-array-payload": {"agentic_memory": {"next_action": ["payload array next action sentinel"]}},
        }
        event_ids = {key: str(uuid4()) for key in event_payloads}
        for index, (event_key, payload_json) in enumerate(event_payloads.items()):
            store.append_event(
                {
                    "id": event_ids[event_key],
                    "event_type": "open_loop.updated",
                    "actor_type": "system",
                    "target_type": "open_loop",
                    "target_id": event_loop["id"],
                    "occurred_at": datetime(2030, 7, 10, 12, index, tzinfo=UTC),
                    "payload_json": payload_json,
                }
            )
        row_event_targets = {
            "row-root-string-event": rows["next_action"]["id"],
            "row-nested-string-event": rows["agentic_next_action"]["id"],
            "row-root-integer-event": rows["root_integer_next_action"]["id"],
            "row-nested-object-event": rows["agentic_object_next_action"]["id"],
            "row-nested-array-event": rows["agentic_array_next_action"]["id"],
        }
        row_event_ids = {key: str(uuid4()) for key in row_event_targets}
        for index, (event_key, target_id) in enumerate(row_event_targets.items()):
            store.append_event(
                {
                    "id": row_event_ids[event_key],
                    "event_type": "open_loop.updated",
                    "actor_type": "system",
                    "target_type": "open_loop",
                    "target_id": target_id,
                    "occurred_at": datetime(2030, 7, 10, 13, index, tzinfo=UTC),
                    "payload_json": {"note": "unrelated payload"},
                }
            )

        row_expectations = {
            "release": {str(rows[key]["id"]) for key in ("title", "description", "next_action", "agentic_next_action")},
            "ärende": set(),
            "Ärende": {str(rows["arende"]["id"])},
            "STRASSE": set(),
            "Straße": {str(rows["strasse"]["id"])},
            "%": {str(rows["percent"]["id"])},
            "_": {str(rows["underscore"]["id"])},
            "\\": {str(rows["backslash"]["id"])},
            r"missing%_\path": set(),
            "8675309": set(),
            "object row sentinel": set(),
            "array row sentinel": set(),
        }
        event_expectations = {
            "alpha beta": {event_ids["nested-positive"], event_ids["array-positive"]},
            "release": {
                event_ids["release-nested"],
                event_ids["release-array"],
                row_event_ids["row-root-string-event"],
                row_event_ids["row-nested-string-event"],
            },
            "payload object next action sentinel": {event_ids["next-action-object-payload"]},
            "payload array next action sentinel": {event_ids["next-action-array-payload"]},
            "ärende": set(),
            "Ärende": {event_ids["arende-nested"], event_ids["arende-array"]},
            "STRASSE": set(),
            "Straße": {event_ids["strasse-nested"], event_ids["strasse-array"]},
            "%": {event_ids["percent-nested"], event_ids["percent-array"]},
            "_": {event_ids["underscore-nested"], event_ids["underscore-array"]},
            "\\": {event_ids["backslash-nested"], event_ids["backslash-array"]},
            r"missing%_\path": set(),
            "123": set(),
            "true": set(),
            "8675309": set(),
            "object row sentinel": set(),
            "array row sentinel": set(),
        }
        for scope_projects in (None, (project,)):
            for query, expected_ids in row_expectations.items():
                actual = store.list_open_loops(query=query, scope_projects=scope_projects, limit=50)
                assert {str(row["id"]) for row in actual} == expected_ids
            for query, expected_ids in event_expectations.items():
                actual = store.list_open_loop_events(
                    statuses=("open",),
                    scope_projects=scope_projects,
                    query=query,
                    occurred_at_start=datetime(2030, 7, 10, 12, tzinfo=UTC),
                    limit=50,
                    domains=None,
                    sensitivity_allowed=None,
                )
                assert {str(row["id"]) for row in actual} == expected_ids

            assert len(store.list_open_loops(query="   ", scope_projects=scope_projects, limit=50)) == len(rows) + 1
            assert len(
                store.list_open_loop_events(
                    statuses=("open",),
                    scope_projects=scope_projects,
                    query="   ",
                    occurred_at_start=datetime(2030, 7, 10, 12, tzinfo=UTC),
                    limit=50,
                    domains=None,
                    sensitivity_allowed=None,
                )
            ) == len(event_payloads) + len(row_event_targets)


def test_postgres_memory_queries_use_ascii_literal_semantics_across_store_and_public_mcp(
    migrated_database_urls,
) -> None:
    app_url = migrated_database_urls["app"]
    user_id = uuid4()
    with user_connection(app_url, user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"ascii-memory-query-{user_id}@example.invalid",
            "ASCII memory query",
        )
        store = PostgresVNextStore(conn)

        def create_memory(title: str, canonical_text: str, summary: str = "unrelated summary") -> dict[str, object]:
            return store.create_memory(
                {
                    "memory_key": f"ascii.memory.query.{uuid4().hex}",
                    "value": {"text": canonical_text},
                    "status": "active",
                    "memory_type": "decision",
                    "title": title,
                    "canonical_text": canonical_text,
                    "summary": summary,
                    "domain": "project",
                    "sensitivity": "internal",
                }
            )

        rows = {
            "title": create_memory("Release title", "unrelated canonical text"),
            "canonical": create_memory("Unrelated title", "Release canonical text"),
            "summary": create_memory("Unrelated title", "unrelated canonical text", "Release summary"),
            "arende": create_memory("Ärende row", "unrelated canonical text"),
            "strasse": create_memory("Straße row", "unrelated canonical text"),
            "literals": create_memory(r"100% under_score path\segment", "unrelated canonical text"),
        }

        expectations = {
            "release": {str(rows[key]["id"]) for key in ("title", "canonical", "summary")},
            "RELEASE": {str(rows[key]["id"]) for key in ("title", "canonical", "summary")},
            "ärende": set(),
            "Ärende": {str(rows["arende"]["id"])},
            "STRASSE": set(),
            "Straße": {str(rows["strasse"]["id"])},
            "%": {str(rows["literals"]["id"])},
            "_": {str(rows["literals"]["id"])},
            "\\": {str(rows["literals"]["id"])},
            r"missing%_\path": set(),
        }
        for query, expected_ids in expectations.items():
            memories = store.list_memories(query=query, order_by_created_at=True, limit=50)
            resume_events = store.list_resume_memory_events(
                statuses=("active",), query=query, limit=100, domains=None, sensitivity_allowed=None
            )
            assert {str(row["id"]) for row in memories} == expected_ids
            assert {str(event["target_id"]) for event in resume_events} == expected_ids

        assert len(store.list_memories(query="   ", limit=50)) == len(rows)
        assert {
            str(event["target_id"])
            for event in store.list_resume_memory_events(
                statuses=("active",), query="   ", limit=100, domains=None, sensitivity_allowed=None
            )
        } == {str(row["id"]) for row in rows.values()}

    context = MCPRuntimeContext(database_url=app_url, user_id=user_id)
    for query, expected_ids in {
        "release": expectations["release"],
        "ärende": set(),
        "Ärende": expectations["Ärende"],
        "%": expectations["%"],
        r"missing%_\path": set(),
    }.items():
        recent = call_mcp_tool(context, name="alice_recent_decisions", arguments={"query": query, "limit": 50})
        resume = call_mcp_tool(
            context,
            name="alice_resume",
            arguments={"query": query, "max_open_loops": 0, "max_recent_changes": 0},
        )
        assert {str(row["id"]) for row in recent["decisions"]} == expected_ids
        last_decision = resume["brief"]["last_decision"]
        if expected_ids:
            assert str(last_decision["id"]) in expected_ids
        else:
            assert last_decision is None


def test_project_scope_identity_is_case_order_whitespace_and_duplicate_insensitive(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    marker = f"project scope identity {uuid4().hex}"
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"scope-identity-{user_id}@example.invalid",
            "Scope identity",
        )
        store = PostgresVNextStore(conn)
        memory = store.create_memory(
            {
                "memory_key": f"scope.identity.{uuid4().hex}",
                "value": {"text": marker},
                "status": "active",
                "canonical_text": marker,
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": [" Beta Project ", "ALICE", "alice"]},
            }
        )
        source = store.create_source(
            {
                "source_type": "manual_text",
                "title": marker,
                "content_hash": f"sha256:{uuid4().hex}",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": ["ALICE", "beta project"]},
            }
        )

        assert _ids(store.list_memories(projects=(" alice ",), limit=10)) == {str(memory["id"])}
        assert _ids(
            store.search_sources(
                query=marker,
                scope_projects=("BETA   PROJECT",),
                limit=10,
            )
        ) == {str(source["id"])}
        exact = store.find_live_memory_by_canonical_text(
            marker,
            domain="project",
            sensitivity="internal",
            project_scope=("beta project", "Alice", "BETA PROJECT"),
        )
        assert exact is not None
        assert str(exact["id"]) == str(memory["id"])


def test_project_scope_identity_preserves_unicode_case_and_order_on_postgres(
    migrated_database_urls,
) -> None:
    user_id = uuid4()
    marker = f"unicode project scope identity {uuid4().hex}"
    with user_connection(migrated_database_urls["app"], user_id) as conn:
        ContinuityStore(conn).create_user(
            user_id,
            f"scope-unicode-{user_id}@example.invalid",
            "Unicode scope identity",
        )
        store = PostgresVNextStore(conn)

        def create_scoped_memory(scope: str, index: int) -> dict[str, object]:
            return store.create_memory(
                {
                    "memory_key": f"scope.unicode.{index}.{uuid4().hex}",
                    "value": {"text": marker},
                    "status": "active",
                    "canonical_text": f"{marker} {index}",
                    "domain": "project",
                    "sensitivity": "internal",
                    "metadata_json": {"project_scope": [scope]},
                }
            )

        scopes = (
            "İ",
            "i",
            "Straße",
            "STRASSE",
            "Σ",
            "σ",
            "ς",
            "\u00a0Alice\u00a0",
            "\u00a0alice\u00a0",
        )
        scoped = {scope: create_scoped_memory(scope, index) for index, scope in enumerate(scopes)}

        for scope, memory in scoped.items():
            assert _ids(store.list_memories(projects=(scope,), limit=20)) == {str(memory["id"])}
        assert _ids(store.list_memories(projects=("\t I \n",), limit=20)) == {str(scoped["i"]["id"])}
        assert _ids(store.list_memories(projects=("straße",), limit=20)) == set()

        mixed = store.create_memory(
            {
                "memory_key": f"scope.unicode.mixed.{uuid4().hex}",
                "value": {"text": marker},
                "status": "active",
                "canonical_text": f"{marker} mixed",
                "domain": "project",
                "sensitivity": "internal",
                "metadata_json": {"project_scope": ["é", "Z", "Ä", "a", "z", "İ", "i"]},
            }
        )
        exact = store.find_live_memory_by_canonical_text(
            f"{marker} mixed",
            domain="project",
            sensitivity="internal",
            project_scope=("İ", "i", "Ä", "z", "a", "é"),
        )

        assert exact is not None
        assert str(exact["id"]) == str(mixed["id"])
