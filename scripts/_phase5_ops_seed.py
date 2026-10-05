#!/usr/bin/env python3
"""Seed the small deterministic store used by the Phase 5 operations drill.

This helper is intentionally compatible with the v0.12.0 source tree.  The
orchestrator runs it with ``PYTHONPATH`` pointed at either an extracted
v0.12.0 archive or the current checkout, so the data is written by the runtime
being exercised instead of by an ad-hoc SQL fixture.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID


def _as_int(value: object) -> int:
    """Narrow a database cell to int.

    A psycopg row cell is typed `object`, so `int(cell)` does not type-check.
    This narrows explicitly rather than casting, so a column that is not
    integer-like fails loudly here instead of somewhere further downstream.
    """

    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        return int(value)
    raise TypeError(f"expected an integer-like database value, got {type(value).__name__}")


USER_ID = UUID("00000000-0000-0000-0000-000000005001")
MEMORY_ID = "00000000-0000-0000-0000-000000005101"
ARTIFACT_ID = "00000000-0000-0000-0000-000000005201"
CONFIDENTIAL_MEMORY_ID = "00000000-0000-0000-0000-000000005111"
DERIVED_BRIEF_ID = "00000000-0000-0000-0000-000000005112"
_EMPTY_DERIVED_FROM = {
    "v": 1,
    "sources": [],
    "memories": [],
    "open_loops": [],
    "artifacts": [],
    "beliefs": [],
    "counts": {"sources": 0, "memories": 0, "open_loops": 0, "artifacts": 0, "beliefs": 0},
}
OLDER_RATING_ID = "00000000-0000-0000-0000-000000005301"
NEWER_RATING_ID = "00000000-0000-0000-0000-000000005302"
REVIEWER_ID = "phase5-ops-reviewer"
SEED_QUERY = "cobalt recovery beacon"


def _memory_payload(label: str) -> dict[str, object]:
    text = f"The cobalt recovery beacon identifies the {label} operations evidence store."
    return {
        "id": MEMORY_ID,
        "memory_key": f"phase5.ops.{label}",
        "value": {"text": text},
        "status": "active",
        "memory_type": "decision",
        "confirmation_status": "confirmed",
        "trust_class": "human_curated",
        "title": "Cobalt recovery beacon",
        "canonical_text": text,
        "summary": "Deterministic recovery evidence anchor.",
        "domain": "project",
        "sensitivity": "internal",
        "metadata_json": {"evidence_fixture": "phase5_ops_v1"},
    }


def _signed_vector(store: Any, memory: dict[str, object]) -> None:
    from alicebot_api.vnext_embeddings import (  # imported from selected source tree
        EMBEDDING_SIGNATURE_VERSION,
        EMBEDDING_VECTOR_DIMENSIONS,
        memory_embedding_content_sha256,
    )

    vector = [1.0, *([0.0] * (EMBEDDING_VECTOR_DIMENSIONS - 1))]
    updated = store.update_memory_embedding(
        memory_id=str(memory["id"]),
        vector=vector,
        provider="phase5-ops",
        model="deterministic-v1",
        endpoint="phase5-ops-local",
        content_sha256=memory_embedding_content_sha256(memory),
        signature_version=EMBEDDING_SIGNATURE_VERSION,
    )
    if updated is None:
        raise RuntimeError("signed embedding fixture was not persisted")


def _seed_sqlite(db_path: Path, *, label: str) -> dict[str, object]:
    from alicebot_api.onramp import bootstrap_database
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection

    bootstrap_database(db_path, user_id=USER_ID, user_email="phase5-ops@example.invalid")
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        memory = store.create_memory(_memory_payload(label), actor_type="system")
        _signed_vector(store, memory)
        counts = {
            "users": int(conn.execute("SELECT count(*) AS count FROM users").fetchone()["count"]),
            "memories": int(conn.execute("SELECT count(*) AS count FROM memories").fetchone()["count"]),
            "event_log": int(conn.execute("SELECT count(*) AS count FROM event_log").fetchone()["count"]),
        }
    return {"backend": "sqlite", "counts": counts, "seeded": True}


def _seed_postgres(
    *,
    admin_database_url: str,
    app_database_url: str,
    label: str,
    migrate_to_head: bool,
    seed_migration_0093_fixture: bool,
) -> dict[str, object]:
    if migrate_to_head:
        from alembic import command
        from alicebot_api.migrations import make_alembic_config

        command.upgrade(make_alembic_config(admin_database_url), "head")

    from alicebot_api.db import direct_user_connection

    with direct_user_connection(admin_database_url, USER_ID) as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (USER_ID, "phase5-ops@example.invalid", "Phase 5 Ops"),
        )

    from alicebot_api.vnext_store import PostgresVNextStore

    with direct_user_connection(app_database_url, USER_ID) as conn:
        store = PostgresVNextStore(conn)
        memory = store.create_memory(_memory_payload(label), actor_type="system")
        _signed_vector(store, memory)
        if seed_migration_0093_fixture:
            artifact = store.create_artifact(
                {
                    "id": ARTIFACT_ID,
                    "artifact_type": "weekly_synthesis",
                    "title": "Phase 5 migration survivor fixture",
                    "content_markdown": "Deterministic migration fixture.",
                    "status": "draft",
                    "domain": "project",
                    "sensitivity": "internal",
                    "generated_by": "system",
                    "metadata_json": {
                        "evidence_fixture": "phase5_ops_v1",
                        "derived_from": _EMPTY_DERIVED_FROM,
                    },
                }
            )
            store.create_memory(
                {
                    "id": CONFIDENTIAL_MEMORY_ID,
                    "memory_key": "phase5.ops.confidential",
                    "value": {"text": "A confidential operations fact."},
                    "status": "active",
                    "memory_type": "decision",
                    "confirmation_status": "confirmed",
                    "trust_class": "human_curated",
                    "title": "Confidential operations fact",
                    "canonical_text": "A confidential operations fact.",
                    "summary": "Stored above the brief that names it.",
                    "domain": "project",
                    "sensitivity": "confidential",
                    "metadata_json": {"evidence_fixture": "phase5_ops_v1"},
                },
                actor_type="system",
            )
            store.create_artifact(
                {
                    "id": DERIVED_BRIEF_ID,
                    "artifact_type": "weekly_synthesis",
                    "title": "Brief over a confidential memory",
                    "content_markdown": "Names the confidential operations fact.",
                    "status": "draft",
                    "domain": "project",
                    "sensitivity": "public",
                    "generated_by": "system",
                    "metadata_json": {
                        "workflow": "weekly_synthesis",
                        "input_summary": {
                            "memory_ids": [CONFIDENTIAL_MEMORY_ID],
                            "source_ids": [],
                            "open_loop_ids": [],
                            "artifact_ids": [],
                            "counts": {"memories": 1, "sources": 0, "open_loops": 0, "artifacts": 0},
                        },
                        "derived_from": {
                            "v": 1,
                            "sources": [],
                            "memories": [CONFIDENTIAL_MEMORY_ID],
                            "open_loops": [],
                            "artifacts": [],
                            "beliefs": [],
                            "counts": {
                                "sources": 0,
                                "memories": 1,
                                "open_loops": 0,
                                "artifacts": 0,
                                "beliefs": 0,
                            },
                        },
                    },
                }
            )
            for rating_id, usefulness, created_at in (
                (OLDER_RATING_ID, 2, "2020-01-01T00:00:00Z"),
                (NEWER_RATING_ID, 5, "2021-01-01T00:00:00Z"),
            ):
                conn.execute(
                    """
                    INSERT INTO artifact_quality_ratings (
                      id,
                      user_id,
                      artifact_id,
                      reviewer_id,
                      usefulness,
                      accuracy,
                      verbosity,
                      created_at,
                      metadata_json
                    ) VALUES (
                      %s::uuid,
                      app.current_user_id(),
                      %s::uuid,
                      %s,
                      %s,
                      %s,
                      'right_sized',
                      %s::timestamptz,
                      '{"evidence_fixture":"phase5_ops_v1"}'::jsonb
                    )
                    """,
                    (
                        rating_id,
                        str(artifact["id"]),
                        REVIEWER_ID,
                        usefulness,
                        usefulness,
                        created_at,
                    ),
                )

    with direct_user_connection(admin_database_url, USER_ID) as conn:
        users_row = conn.execute("SELECT count(*) FROM users").fetchone()
        memories_row = conn.execute("SELECT count(*) FROM memories").fetchone()
        events_row = conn.execute("SELECT count(*) FROM event_log").fetchone()
        if users_row is None or memories_row is None or events_row is None:
            raise RuntimeError("PostgreSQL seed count query returned no row")
        counts = {
            "users": _as_int(users_row["count"]),
            "memories": _as_int(memories_row["count"]),
            "event_log": _as_int(events_row["count"]),
        }
    return {"backend": "postgres", "counts": counts, "seeded": True}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed deterministic Phase 5 operations evidence.")
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument("--label", default="current")
    parser.add_argument("--db")
    parser.add_argument("--database-admin-url", default=os.getenv("DATABASE_ADMIN_URL"))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--migrate-to-head", action="store_true")
    parser.add_argument("--seed-migration-0093-fixture", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.backend == "sqlite":
        if not args.db:
            raise SystemExit("--db is required for SQLite evidence")
        result = _seed_sqlite(Path(args.db), label=args.label)
    else:
        if not args.database_admin_url or not args.database_url:
            raise SystemExit("--database-admin-url and --database-url are required for PostgreSQL evidence")
        result = _seed_postgres(
            admin_database_url=args.database_admin_url,
            app_database_url=args.database_url,
            label=args.label,
            migrate_to_head=args.migrate_to_head,
            seed_migration_0093_fixture=args.seed_migration_0093_fixture,
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
