"""Restricted-domain generation and migration on the role-separated database."""

from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

from alembic import command
import psycopg
import pytest
from psycopg import sql

from alicebot_api.db import user_connection
from alicebot_api.migrations import make_alembic_config
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_brain import BrainArtifactRequest, VNextBrainService
from alicebot_api.vnext_store import PostgresVNextStore


def test_postgres_derived_domain_upgrade_and_generation(database_urls):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260721_0094")
    user = uuid4()
    with user_connection(database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, "derived-fence@example.invalid", "Derived fence")
        store = PostgresVNextStore(conn)
        source = store.create_memory(
            {
                "memory_key": "health",
                "canonical_text": "Cedar observation",
                "domain": "health",
                "sensitivity": "public",
                "status": "active",
            }
        )
        store.create_memory(
            {
                "memory_key": "project",
                "canonical_text": "Project observation",
                "domain": "project",
                "sensitivity": "public",
                "status": "active",
            }
        )
        derived = store.create_memory(
            {
                "memory_key": "rollup",
                "canonical_text": "Derived text",
                "domain": "unknown",
                "status": "candidate",
                "metadata_json": {"consolidation": {"cluster_member_ids": [str(source["id"])]}},
            }
        )
        report = store.create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Earlier brief",
                "content_markdown": "Derived text",
                "status": "needs_review",
                "domain": "unknown",
                "sensitivity": "public",
                "metadata_json": {"input_summary": {"memory_ids": [str(derived["id"])]}},
            }
        )
    command.upgrade(config, "head")
    with user_connection(database_urls["app"], user) as conn:
        store = PostgresVNextStore(conn)
        repaired = store.get_memory(str(derived["id"]))
        assert repaired["domain"] == "health"
        assert {k: v for k, v in repaired.items() if k != "domain"} == {
            k: v for k, v in derived.items() if k != "domain"
        }
        assert store.get_artifact(str(report["id"]))["domain"] == "health"
        fresh = VNextBrainService(store).generate_daily_brief(
            BrainArtifactRequest(sensitivity_allowed=ALL_SENSITIVITY, discover_open_loops=False)
        )
        assert fresh["domain"] == "health"
    # The data-only downgrade retains safe labels, and repeating the upgrade is harmless.
    command.downgrade(config, "20260721_0094")
    command.upgrade(config, "head")
    with user_connection(database_urls["app"], user) as conn:
        assert PostgresVNextStore(conn).get_memory(str(derived["id"])) == repaired


@pytest.mark.parametrize("fail_after_write", (False, True))
def test_postgres_repair_as_documented_nobypassrls_owner(database_urls, monkeypatch, fail_after_write):
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260721_0094")
    user = uuid4()
    with user_connection(database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, "owner-repair@example.invalid", "Owner repair")
        store = PostgresVNextStore(conn)
        source = store.create_memory(
            {
                "memory_key": "health",
                "canonical_text": "Private observation",
                "domain": "health",
                "sensitivity": "public",
                "status": "active",
            }
        )
        derived = store.create_memory(
            {
                "memory_key": "derived",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "status": "candidate",
                "metadata_json": {"consolidation": {"cluster_member_ids": [str(source["id"])]}},
            }
        )
        artifact = store.create_artifact(
            {
                "artifact_type": "daily_brief",
                "title": "Earlier report",
                "content_markdown": "Private summary",
                "status": "needs_review",
                "domain": "unknown",
                "metadata_json": {"input_summary": {"memory_ids": [str(derived["id"])]}},
            }
        )
        promoted = store.create_memory(
            {
                "memory_key": "promoted",
                "canonical_text": "Private summary",
                "domain": "unknown",
                "status": "active",
                "value": {"kind": "promoted_artifact", "artifact_id": str(artifact["id"])},
                "metadata_json": {"source_artifact_id": str(artifact["id"])},
            }
        )
        # One input of each remaining kind the migration reads under FORCE RLS,
        # each with its own restricted label, and one derived report built only
        # from it. Reading a table the owner cannot see leaves its report
        # unknown, so a missing NO FORCE line on that table fails by name.
        legal_source = store.create_source(
            {
                "source_type": "note",
                "title": "Counsel note",
                "content_hash": "sha256:" + uuid4().hex,
                "domain": "legal",
                "sensitivity": "public",
            }
        )
        financial_loop = store.create_open_loop(
            {"title": "Settle the loan", "domain": "financial", "sensitivity": "public"}, actor_type="user"
        )
        family_memory = store.create_memory(
            {
                "memory_key": "family",
                "canonical_text": "Family observation",
                "domain": "family",
                "sensitivity": "public",
                "status": "active",
                "memory_type": "belief",
            }
        )
        family_belief = store.create_belief(
            {"memory_id": str(family_memory["id"]), "claim": "Family claim", "status": "active", "confidence": 0.8},
            actor_type="user",
        )
        from_source, from_open_loop, from_belief = (
            store.create_artifact(
                {
                    "artifact_type": artifact_type,
                    "title": title,
                    "content_markdown": "Derived text",
                    "status": "needs_review",
                    "domain": "unknown",
                    "metadata_json": metadata,
                }
            )
            for artifact_type, title, metadata in (
                ("daily_brief", "Brief of a source", {"input_summary": {"source_ids": [str(legal_source["id"])]}}),
                ("daily_brief", "Brief of a loop", {"input_summary": {"open_loop_ids": [str(financial_loop["id"])]}}),
                ("contradiction_report", "Report on a belief", {"belief_ids": [str(family_belief["id"])]}),
            )
        )
    role = "repair_owner_" + uuid4().hex
    tables = ("sources", "memories", "open_loops", "generated_artifacts", "beliefs", "event_log")
    parsed = urlsplit(database_urls["admin"])
    role_url = urlunsplit(
        parsed._replace(netloc=f"{role}:fixture-role-password@{parsed.hostname}:{parsed.port or 5432}")
    )
    with psycopg.connect(database_urls["admin"], autocommit=True) as admin:
        owner = admin.execute("SELECT current_user").fetchone()[0]
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(role), sql.Literal("fixture-role-password")
            )
        )
        try:
            admin.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("GRANT USAGE ON SCHEMA app TO {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("GRANT SELECT, UPDATE ON alembic_version TO {}").format(sql.Identifier(role)))
            for table in tables:
                admin.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(sql.Identifier(table), sql.Identifier(role)))
            with psycopg.connect(role_url) as restricted:
                assert restricted.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                ).fetchone() == (False, False)
                assert restricted.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
            if fail_after_write:
                from alicebot_api import vnext_derived_domain_backfill as repair

                def injected_failure(*args):
                    raise RuntimeError("injected audit failure")

                with monkeypatch.context() as patch:
                    patch.setattr(repair, "relabel_event", injected_failure)
                    with pytest.raises(RuntimeError, match="injected audit failure"):
                        command.upgrade(make_alembic_config(role_url), "head")
                assert admin.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20260721_0094"
            else:
                command.upgrade(make_alembic_config(role_url), "head")
            assert all(
                row[0]
                for row in admin.execute(
                    "SELECT relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)", (list(tables),)
                ).fetchall()
            ), "migration must restore FORCE RLS"
            with user_connection(database_urls["app"], user) as conn:
                store = PostgresVNextStore(conn)
                expected = "unknown" if fail_after_write else "health"
                assert store.get_memory(str(derived["id"]))["domain"] == expected, "migration must relabel derived memory"
                assert store.get_artifact(str(artifact["id"]))["domain"] == expected
                assert store.get_memory(str(promoted["id"]))["domain"] == expected
                for report, label, message in (
                    (from_source, "legal", "migration must relabel a report derived from a source"),
                    (from_open_loop, "financial", "migration must relabel a report derived from an open loop"),
                    (from_belief, "family", "migration must relabel a report derived from a belief"),
                ):
                    assert store.get_artifact(str(report["id"]))["domain"] == ("unknown" if fail_after_write else label), message
                rows = conn.execute(
                    "SELECT target_id FROM event_log WHERE event_type IN ('memory.domain_relabelled', 'artifact.domain_relabelled')"
                ).fetchall()
                assert len(rows) == (0 if fail_after_write else 6)
        finally:
            for table in tables:
                admin.execute(
                    sql.SQL("ALTER TABLE {} OWNER TO {}").format(sql.Identifier(table), sql.Identifier(owner))
                )
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


_SINGLE_TABLE_RELAX_GUARDS = {
    "relax_sources": ("sources", "migration must relabel a report derived from a source"),
    "relax_open_loops": ("open_loops", "migration must relabel a report derived from an open loop"),
    "relax_beliefs": ("beliefs", "migration must relabel a report derived from a belief"),
}


@pytest.mark.parametrize("guard", ("relax", "restore", "planner", *_SINGLE_TABLE_RELAX_GUARDS))
def test_postgres_repair_guard_mutations(database_urls, monkeypatch, guard):
    from alembic import op
    from alicebot_api import vnext_derived_domain_backfill as repair

    execute = op.execute
    if guard == "planner":
        monkeypatch.setattr(repair, "plan_relabels", lambda tables: [])
    else:

        def omit_guard(statement, *args, **kwargs):
            # Earlier migrations establish the precondition and must stay
            # intact. Only mutate 0095, while its predecessor is still stamped.
            if op.get_context().get_current_revision() != "20260721_0094":
                return execute(statement, *args, **kwargs)
            text = str(statement)
            if guard == "relax" and " NO FORCE " in text:
                return None
            if guard in _SINGLE_TABLE_RELAX_GUARDS and text == (
                f"ALTER TABLE {_SINGLE_TABLE_RELAX_GUARDS[guard][0]} NO FORCE ROW LEVEL SECURITY"
            ):
                return None
            if guard == "restore" and " FORCE " in text and " NO FORCE " not in text:
                return None
            return execute(statement, *args, **kwargs)

        monkeypatch.setattr(op, "execute", omit_guard)
    expected = {
        "restore": "migration must restore FORCE RLS",
        **{name: message for name, (_table, message) in _SINGLE_TABLE_RELAX_GUARDS.items()},
    }.get(guard, "migration must relabel derived memory")
    with pytest.raises(AssertionError, match=expected):
        test_postgres_repair_as_documented_nobypassrls_owner(database_urls, monkeypatch, False)


def test_promoted_artifact_uuid_alias_repaired(database_urls):
    from uuid import UUID
    from alicebot_api.vnext_artifact_review import dispatch_vnext_artifact_review
    config = make_alembic_config(database_urls["admin"])
    command.upgrade(config, "20260721_0094")
    user = uuid4()
    with user_connection(database_urls["app"], user) as conn:
        ContinuityStore(conn).create_user(user, "alias@example.invalid", "Alias fixture")
        store = PostgresVNextStore(conn)
        memory = store.create_memory({"memory_key": "health", "canonical_text": "Private observation",
            "domain": "health", "sensitivity": "public", "status": "active"})
        artifact = store.create_artifact({"artifact_type": "daily_brief", "title": "Fixture brief",
            "content_markdown": "Private observation", "status": "accepted", "domain": "unknown",
            "sensitivity": "public", "metadata_json": {"input_summary": {"memory_ids": [str(memory["id"])]}}})
        alias = UUID(str(artifact["id"])).hex.upper()
        result = dispatch_vnext_artifact_review(store, artifact_id=alias, action="promote", actor_type="user")
        promoted_id = result.artifact["promoted_memory_id"]
        assert store.get_memory(promoted_id)["value"]["artifact_id"] == alias
    command.upgrade(config, "head")
    with user_connection(database_urls["app"], user) as conn:
        store = PostgresVNextStore(conn)
        assert store.get_artifact(str(artifact["id"]))["domain"] == "health"
        assert store.get_memory(promoted_id)["domain"] == "health"
