"""Owner migration and application repair acceptance with real restricted readers."""

from __future__ import annotations

from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import quote, urlsplit, urlunsplit

from alembic import command, op
import psycopg
from psycopg import sql
import pytest

import alicebot_api.main as main_module
from alicebot_api import vnext_label_repair as repair
from alicebot_api.cli import labels
from alicebot_api.config import Settings
from alicebot_api.db import close_connection_pools, user_connection, user_read_snapshot_connection
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.migrations import make_alembic_config
from alicebot_api.routers import vnext_retrieval as retrieval_router
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_doctor import VNextDoctorService
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_label_guard import LabelGuard
from alicebot_api.vnext_label_writes import without_insert_floor
from alicebot_api.vnext_store import PostgresVNextStore
from tests.integration.test_vnext_omitted_domains_api import invoke_request
from tests.integration.conftest import _create_role_separated_database, _drop_database, _role_urls

TABLES = ("sources", "memories", "open_loops", "generated_artifacts", "beliefs", "event_log", "projects")
SENTINEL = "Violet private migration sentinel"


@pytest.fixture
def database_urls(monkeypatch):
    """Run migration acceptance with an owner that cannot bypass forced RLS.

    CI uses a superuser administrator for historical migrations. These tests
    deliberately use a separate restricted owner for the current acceptance.
    """
    admin_root, _app, lifecycle_root, *_ = _role_urls("unused")
    role_name = None
    with psycopg.connect(admin_root) as conn:
        posture = conn.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
    if posture != (False, False):
        role_name = "alicebot_repair_owner_" + uuid4().hex[:12]
        password = uuid4().hex
        with psycopg.connect(lifecycle_root, autocommit=True) as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS").format(
                    sql.Identifier(role_name), sql.Literal(password)
                )
            )
        parsed = urlsplit(admin_root)
        server = parsed.netloc.rsplit("@", 1)[-1]
        strict_url = urlunsplit(
            (parsed.scheme, f"{quote(role_name)}:{quote(password)}@{server}", parsed.path, parsed.query, parsed.fragment)
        )
        # Preserve the original bootstrap actor when no explicit lifecycle URL is set.
        monkeypatch.setenv("DATABASE_LIFECYCLE_URL", lifecycle_root)
        monkeypatch.setenv("DATABASE_ADMIN_URL", strict_url)
    name = "alicebot_repair_" + uuid4().hex[:12]
    try:
        urls = _create_role_separated_database(name)
        # CI preloads vector in its root database rather than template1.
        with psycopg.connect(_role_urls(name)[-1], autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        yield urls
    finally:
        close_connection_pools()
        _drop_database(name)
        if role_name is not None:
            with psycopg.connect(lifecycle_root, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


@pytest.fixture
def migrated_database_urls(database_urls):
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    return database_urls


@contextmanager
def owner_bracket(url):
    with psycopg.connect(url) as conn:
        for table in TABLES:
            conn.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        yield conn
        for table in TABLES:
            conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def assert_roles_and_force(urls):
    for url in (urls["admin"], urls["app"]):
        with psycopg.connect(url) as conn:
            assert conn.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False, False)
    with psycopg.connect(urls["admin"]) as conn:
        assert all(
            row[0]
            for row in conn.execute("SELECT relforcerowsecurity FROM pg_class WHERE relname=ANY(%s)", (list(TABLES),))
        )


def seed_stale(urls, user=None):
    user = user or uuid4()
    with without_insert_floor(), user_connection(urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, f"fixture-{user}@example.invalid", "Synthetic fixture")
        store = PostgresVNextStore(conn)
        project = store.create_project({"slug": "alpha", "name": "Alpha", "domain": "project", "sensitivity": "public"})
        source = store.create_source(
            {
                "source_type": "note",
                "title": "Synthetic private input",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "health",
                "sensitivity": "confidential",
                "metadata_json": {},
            }
        )
        copies = []
        for i in range(3):
            copies.append(
                store.create_memory(
                    {
                        "memory_key": f"derived.{i}",
                        "canonical_text": SENTINEL,
                        "domain": "unknown",
                        "sensitivity": "public",
                        "status": "active",
                        "project_id": str(project["id"]),
                        "metadata_json": {"source_id": str(source["id"]), "project_scope": [str(project["id"])]},
                    }
                )
            )
        loop = store.create_open_loop(
            {
                "title": SENTINEL,
                "domain": "unknown",
                "sensitivity": "public",
                "source_id": str(source["id"]),
                "project_id": str(project["id"]),
                "metadata_json": {
                    "discovered_by": "vnext_daily_brief",
                    "source_id": str(source["id"]),
                    "project_scope": [str(project["id"])],
                },
            },
            actor_type="user",
        )
        belief = store.create_belief(
            {"memory_id": str(copies[0]["id"]), "claim": "Synthetic belief", "status": "active", "confidence": 0.8},
            actor_type="user",
        )
        reports = []
        for key, metadata in (
            ("source", {"input_summary": {"source_ids": [str(source["id"])]}}),
            ("memory", {"input_summary": {"memory_ids": [str(copies[0]["id"])]}}),
            ("loop", {"input_summary": {"open_loop_ids": [str(loop["id"])]}}),
            ("belief", {"belief_ids": [str(belief["id"])]}),
        ):
            reports.append(
                store.create_artifact(
                    {
                        "artifact_type": "contradiction_report" if key == "belief" else "daily_brief",
                        "title": f"Synthetic {key} report",
                        "content_markdown": SENTINEL,
                        "status": "needs_review",
                        "domain": "unknown",
                        "sensitivity": "public",
                        "metadata_json": {**metadata, "project_scope": [str(project["id"])]},
                    }
                )
            )
        # Raw state matches a pre-floor accepted project update while keeping its real recorded parents.
        conn.execute(
            "UPDATE projects SET metadata_json=%s::jsonb WHERE id=%s",
            (json.dumps(with_derived_from({}, {"memories": [copies[0]], "artifacts": [reports[0]]})), project["id"]),
        )
    return user, {
        "memories": [str(r["id"]) for r in copies],
        "open_loops": [str(loop["id"])],
        "generated_artifacts": [str(r["id"]) for r in reports],
        "projects": [str(project["id"])],
    }


def stored(urls, user, targets):
    with user_connection(urls["app"], user) as conn:
        rows = {
            table: conn.execute(
                f"SELECT domain, sensitivity, metadata_json{', project_id' if table in {'memories', 'open_loops'} else ''} FROM {table} WHERE id=ANY(%s::uuid[]) ORDER BY id",
                (ids,),
            ).fetchall()
            for table, ids in targets.items()
        }
        events = conn.execute(
            "SELECT target_id, payload_json FROM event_log WHERE event_type LIKE '%%.labels_raised' ORDER BY id"
        ).fetchall()
    return rows, events


def assert_repaired(urls, user, targets):
    rows, events = stored(urls, user, targets)
    for table, values in rows.items():
        assert len(values) == len(targets[table])
        for row in values:
            assert (row["domain"], row["sensitivity"]) == ("health", "confidential"), (table, row)
            assert row["metadata_json"]["project_scope"] == [], (table, row)
            assert row["metadata_json"]["project_floor"] == [], (table, row)
            if "project_id" in row:
                assert row["project_id"] is None
    assert len(events) == sum(len(ids) for ids in targets.values())
    assert SENTINEL not in json.dumps(events, default=str)
    return rows, events


def restricted_reads(urls, user, targets, monkeypatch, *, guard_off=False):
    with user_connection(urls["app"], user) as conn:
        _, raw = create_agent_key(
            PostgresVNextStore(conn), user_id=user, agent_id="migration-reader", permission_profile="read_only_agent"
        )
    with monkeypatch.context() as patch:
        for module in (main_module, retrieval_router):
            patch.setattr(module, "get_settings", lambda: Settings(database_url=urls["app"]))
        patch.setenv("ALICE_AGENT_API_KEY", raw)
        patch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        patch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        if guard_off:
            patch.setattr(LabelGuard, "effective_row", lambda self, kind, row: row)
        status, pack = invoke_request(
            "POST",
            "/v0/vnext/context-packs",
            authorization=f"Bearer {raw}",
            payload={"user_id": str(user), "query": "Violet", "scope": {}, "options": {"include_sources": True}},
        )
        assert status == 201, pack
        assert SENTINEL not in json.dumps(pack), (guard_off, pack)
        context = MCPRuntimeContext(database_url=urls["app"], user_id=user)
        result = call_mcp_tool(context, name="alice_recall", arguments={"query": "Violet"})
        assert SENTINEL not in json.dumps(result, default=str)
        for row_id in targets["memories"]:
            with pytest.raises(MCPToolError, match="requested explanation is unavailable"):
                call_mcp_tool(context, name="alice_explain", arguments={"memory_id": row_id})


def cli_context(urls, user):
    return SimpleNamespace(database_url=urls["app"], user_id=user)


@pytest.mark.parametrize("revision", ("20260721_0094", "20261004_0095"))
def test_upgrade_repairs_all_labels_as_nobypassrls_owner(database_urls, monkeypatch, revision):
    command.upgrade(make_alembic_config(database_urls["admin"]), revision)
    user, targets = seed_stale(database_urls)
    command.upgrade(make_alembic_config(database_urls["admin"]), "head")
    assert_roles_and_force(database_urls)
    assert_repaired(database_urls, user, targets)
    restricted_reads(database_urls, user, targets, monkeypatch, guard_off=True)
    restricted_reads(database_urls, user, targets, monkeypatch)


def test_head_restored_rows_are_guarded_then_fixed_by_owner_command(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    user, targets = seed_stale(urls)
    assert stored(urls, user, targets)[0]["memories"][0]["sensitivity"] == "public"
    restricted_reads(urls, user, targets, monkeypatch)
    assert labels._run_vnext_labels_repair(cli_context(urls, user), None).startswith("labels repair updated ")
    assert_repaired(urls, user, targets)
    restricted_reads(urls, user, targets, monkeypatch, guard_off=True)
    restricted_reads(urls, user, targets, monkeypatch)
    assert labels._run_vnext_labels_check(cli_context(urls, user), None) == "below_inputs 0"


def test_labels_check_is_one_read_only_repeatable_snapshot(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    user, targets = seed_stale(urls)
    original = labels._postgres_tables

    def inspect_snapshot(conn):
        assert conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
        assert conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        before = conn.execute("SELECT count(*) AS count FROM memories").fetchone()["count"]
        with user_connection(urls["app"], user) as other:
            other.execute("UPDATE memories SET sensitivity='regulated' WHERE id=%s", (targets["memories"][0],))
        rows = original(conn)
        assert len(rows["memories"]) == before
        assert all(row["sensitivity"] == "public" for row in rows["memories"])
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with conn.transaction():
                conn.execute("UPDATE memories SET sensitivity='public'")
        return rows

    monkeypatch.setattr(labels, "_postgres_tables", inspect_snapshot)
    with pytest.raises(SystemExit) as caught:
        labels._run_vnext_labels_check(cli_context(urls, user), None)
    assert caught.value.code == 1
    with user_read_snapshot_connection(urls["app"], user) as conn:
        assert conn.execute("SELECT app.current_user_id() AS id").fetchone()["id"] == user
    with psycopg.connect(urls["app"]) as conn:
        assert conn.execute("SELECT app.current_user_id()").fetchone()[0] is None


def test_repair_compare_and_set_failure_after_writes_rolls_everything_back(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    user, targets = seed_stale(urls)
    before = stored(urls, user, targets)
    original = labels.require_changed
    writes = []

    def fail_third(count, table, row_id):
        if row_id != "planned rows":
            writes.append((count, table, row_id))
            if len(writes) == 3:
                count = 0
        return original(count, table, row_id)

    with monkeypatch.context() as patch:
        patch.setattr(labels, "require_changed", fail_third)
        with pytest.raises(SystemExit) as caught:
            labels._run_vnext_labels_repair(cli_context(urls, user), None)
    assert caught.value.code == 2 and len(writes) == 3
    assert stored(urls, user, targets) == before
    labels._run_vnext_labels_repair(cli_context(urls, user), None)
    assert_repaired(urls, user, targets)
    restricted_reads(urls, user, targets, monkeypatch, guard_off=True)
    restricted_reads(urls, user, targets, monkeypatch)


def test_doctor_counts_real_postgres_rows_and_failure_stays_warning(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    user, targets = seed_stale(urls)
    with user_connection(urls["app"], user) as conn:
        store = PostgresVNextStore(conn)
        below, unverified = repair.label_gap_counts(store)
        assert below == sum(map(len, targets.values())) and unverified == 0

        def broken_loader(conn):
            conn.execute("SELECT * FROM synthetic_missing_label_table")

        monkeypatch.setattr(repair, "load_postgres_label_tables", broken_loader)
        with pytest.raises(repair.LabelCheckUnavailable):
            repair.label_gap_counts(store)
        assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
        result = VNextDoctorService(store).run(ci=True)
        check = next(row for row in result["checks"] if row["name"] == "derived_labels")
        assert check["severity"] == "warning" and check["status"] == "fail"
        assert "unavailable" in check["message"] and "0 below" not in check["message"]
        assert conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_interrupted_migration_rolls_back_and_restored_backup_repeats(database_urls, monkeypatch):
    urls = database_urls
    config = make_alembic_config(urls["admin"])
    command.upgrade(config, "20261004_0095")
    user, targets = seed_stale(urls)
    before = stored(urls, user, targets)
    backup_name = "alicebot_backup_" + uuid4().hex[:12]
    restored_name = "alicebot_restored_" + uuid4().hex[:12]
    close_connection_pools()
    _create_role_separated_database(backup_name, template=urlsplit(urls["admin"]).path.lstrip("/"))
    try:
        original = repair.plan_label_repairs

        def interrupted_plan(tables):
            # Alembic iterates this sequence and really writes its first three rows
            # before the iterator raises; labels, events and FORCE stay atomic.
            changes = original(tables)
            for index, change in enumerate(changes):
                if index == 3:
                    raise RuntimeError("injected after three migration writes")
                yield change

        with monkeypatch.context() as patch:
            patch.setattr(repair, "plan_label_repairs", interrupted_plan)
            with pytest.raises(RuntimeError, match="three migration writes"):
                command.upgrade(config, "head")
        assert stored(urls, user, targets) == before
        assert_roles_and_force(urls)
        with psycopg.connect(urls["admin"]) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261004_0095"
        command.upgrade(config, "head")
        assert_repaired(urls, user, targets)
        restricted_reads(urls, user, targets, monkeypatch, guard_off=True)
        restricted_reads(urls, user, targets, monkeypatch)
        close_connection_pools()
        restored_urls = _create_role_separated_database(restored_name, template=backup_name)
        try:
            assert stored(restored_urls, user, targets) == before
            command.upgrade(make_alembic_config(restored_urls["admin"]), "head")
            assert_repaired(restored_urls, user, targets)
            assert_roles_and_force(restored_urls)
            restricted_reads(restored_urls, user, targets, monkeypatch, guard_off=True)
            restricted_reads(restored_urls, user, targets, monkeypatch)
        finally:
            close_connection_pools()
            _drop_database(restored_name)
    finally:
        _drop_database(backup_name)


@pytest.mark.parametrize("table", TABLES)
def test_each_force_bracket_guard_is_behaviorally_required(database_urls, monkeypatch, table):
    urls = database_urls
    command.upgrade(make_alembic_config(urls["admin"]), "20261004_0095")
    user, targets = seed_stale(urls)
    original = op.execute

    def omit_one(statement, *args, **kwargs):
        if str(statement) == f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY":
            return None
        return original(statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(op, "execute", omit_one)
        try:
            command.upgrade(make_alembic_config(urls["admin"]), "head")
        except Exception as exc:
            assert table == "event_log", (table, exc)
        else:
            with pytest.raises(AssertionError):
                assert_repaired(urls, user, targets)
    # Return every intentional mutant to a safe settled state and a real reader.
    command.downgrade(make_alembic_config(urls["admin"]), "20261004_0095")
    command.upgrade(make_alembic_config(urls["admin"]), "head")
    assert_repaired(urls, user, targets)
    assert_roles_and_force(urls)
    restricted_reads(urls, user, targets, monkeypatch)


def test_downgrade_to_0095_and_upgrade_is_idempotent(database_urls, monkeypatch):
    urls = database_urls
    config = make_alembic_config(urls["admin"])
    command.upgrade(config, "20261004_0095")
    user, targets = seed_stale(urls)
    command.upgrade(config, "head")
    before = assert_repaired(urls, user, targets)
    command.downgrade(config, "20261004_0095")
    command.upgrade(config, "head")
    assert stored(urls, user, targets) == before
    restricted_reads(urls, user, targets, monkeypatch)


def test_labels_repair_cannot_overwrite_a_relabel(migrated_database_urls, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from alicebot_api.vnext_label_writes import acquire_exclusive_label_lock, held_label_locks

    urls = migrated_database_urls
    user, targets = seed_stale(urls)
    planned, release, relabel_started = threading.Event(), threading.Event(), threading.Event()
    original = labels._postgres_tables

    def pause_snapshot(conn):
        assert held_label_locks(PostgresVNextStore(conn)) == (True, True, True)
        rows = original(conn)
        planned.set()
        assert release.wait(10)
        return rows

    monkeypatch.setattr(labels, "_postgres_tables", pause_snapshot)

    def raise_source():
        with user_connection(urls["app"], user) as conn:
            store = PostgresVNextStore(conn)
            source_id = str(conn.execute("SELECT id FROM sources").fetchone()["id"])
            relabel_started.set()
            store.lock_graph_mutation()
            acquire_exclusive_label_lock(store)
            store.update_source(source_id=source_id, patch={"sensitivity": "regulated"}, actor_type="user")

    with ThreadPoolExecutor(max_workers=2) as executor:
        repaired = executor.submit(labels._run_vnext_labels_repair, cli_context(urls, user), None)
        assert planned.wait(10)
        relabelled = executor.submit(raise_source)
        assert relabel_started.wait(10)
        try:
            # The second transaction has really reached its lock request.
            import time

            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                with psycopg.connect(urls["admin"]) as conn:
                    waiting = bool(
                        conn.execute(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                        ).fetchone()[0]
                    )
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting and not relabelled.done()
        finally:
            release.set()
        repaired.result(timeout=10)
        relabelled.result(timeout=10)
    rows, _events = stored(urls, user, targets)
    assert all(row["sensitivity"] == "regulated" for values in rows.values() for row in values), [
        (table, row["metadata_json"], row["sensitivity"])
        for table, values in rows.items()
        for row in values
        if row["sensitivity"] != "regulated"
    ]
    restricted_reads(urls, user, targets, monkeypatch, guard_off=True)
    restricted_reads(urls, user, targets, monkeypatch)


def test_repair_takes_ordered_row_locks_and_preserves_nonlabel_fields(migrated_database_urls, monkeypatch):
    urls = migrated_database_urls
    user, targets = seed_stale(urls)
    with user_connection(urls["app"], user) as conn:
        before = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    original = psycopg.Cursor.execute
    locks = []

    def record(cursor, query, *args, **kwargs):
        text = query.as_string(cursor.connection) if hasattr(query, "as_string") else str(query)
        normalized = " ".join(text.split())
        if normalized.startswith("SELECT id FROM ") and "ORDER BY id FOR UPDATE" in normalized:
            locks.append(normalized.split()[3])
        return original(cursor, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", record)
    labels._run_vnext_labels_repair(cli_context(urls, user), None)
    assert locks == ["generated_artifacts", "projects", "open_loops", "memories"]
    with user_connection(urls["app"], user) as conn:
        after = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    for previous, new in zip(before, after, strict=True):
        assert {
            key: value
            for key, value in previous.items()
            if key not in {"domain", "sensitivity", "metadata_json", "project_id"}
        } == {
            key: value
            for key, value in new.items()
            if key not in {"domain", "sensitivity", "metadata_json", "project_id"}
        }
        assert new["metadata_json"]["source_id"] == previous["metadata_json"]["source_id"]
    assert_repaired(urls, user, targets)
    restricted_reads(urls, user, targets, monkeypatch, guard_off=True)
    restricted_reads(urls, user, targets, monkeypatch)
