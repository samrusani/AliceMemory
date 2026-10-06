"""Malformed restored metadata passes 0096 under a restricted table owner."""
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4
import json

from alembic import command
import psycopg
from psycopg import sql

from alicebot_api.db import user_connection, close_connection_pools
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore

TABLES = ("sources", "memories", "open_loops", "generated_artifacts", "beliefs", "event_log", "projects")


def test_0096_malformed_marker_is_unverified_and_not_repaired(migrated_database_urls):
    urls = migrated_database_urls
    command.downgrade(make_alembic_config(urls["admin"]), "20261004_0095")
    user = uuid4()
    with without_insert_floor(), user_connection(urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"malformed-{user}@example.invalid", "Synthetic")
        store = PostgresVNextStore(conn)
        memory = store.create_memory({"memory_key": "malformed", "canonical_text": "Synthetic malformed marker",
            "domain": "project", "sensitivity": "public", "status": "active", "metadata_json": {"candidate_kind": ["memory_rollup"]}})
        artifact = store.create_artifact({"artifact_type": "research_brief", "title": "Synthetic malformed marker",
            "content_markdown": "Synthetic malformed marker", "domain": "project", "sensitivity": "public", "metadata_json": {"workflow": {"broken": 1}}})
    role, password = "alice_handoff_owner_" + uuid4().hex[:12], uuid4().hex
    parsed = urlsplit(urls["admin"])
    owner_url = urlunsplit((parsed.scheme, f"{quote(role)}:{quote(password)}@" + parsed.netloc.rsplit("@", 1)[-1], parsed.path, parsed.query, parsed.fragment))
    with psycopg.connect(urls["admin"], autocommit=True) as conn:
        original = conn.execute("SELECT current_user").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role), sql.Literal(password)))
        for table in TABLES:
            conn.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(sql.Identifier(table), sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public, app TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT, UPDATE, INSERT, DELETE ON alembic_version TO {}").format(sql.Identifier(role)))
    try:
        with psycopg.connect(owner_url) as conn:
            assert conn.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone() == (False, False)
        command.upgrade(make_alembic_config(owner_url), "head")
        with psycopg.connect(owner_url) as conn:
            assert all(row[0] for row in conn.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname=ANY(%s)", (list(TABLES),)))
        with user_connection(urls["app"], user) as conn:
            from alicebot_api.vnext_label_repair import classify_stored_labels
            store = PostgresVNextStore(conn)
            mem, art = store.get_memory(str(memory["id"])), store.get_artifact(str(artifact["id"]))
            assert mem["sensitivity"] == art["sensitivity"] == "public"
            below, unverified = classify_stored_labels({"memories": [mem], "generated_artifacts": [art]})
            assert not below
            assert set(unverified["malformed_marker"]) == {str(memory["id"]), str(artifact["id"])}
            assert conn.execute("SELECT count(*) AS n FROM event_log WHERE event_type LIKE '%%.labels_raised'").fetchone()["n"] == 0
    finally:
        close_connection_pools()
        with psycopg.connect(urls["admin"], autocommit=True) as conn:
            for table in TABLES:
                conn.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(sql.Identifier(table), sql.Identifier(original)))
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
