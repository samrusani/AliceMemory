"""Standing to read lessons through the real SQLite MCP server."""

import sqlite3
from contextlib import closing, contextmanager

import pytest
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_label_guard import LabelGuard
from tests.support.lesson_visibility_contract import (
    LESSON_BLOCK,
    check_hidden_lesson_verbs,
    check_keyless_legacy_lesson_correction,
    check_keyless_lesson_visibility,
    check_lesson_pack,
    check_lesson_publisher_ids,
    check_lesson_replays,
    check_lesson_review_growth,
    check_lesson_visibility,
    check_saved_lesson_references,
    make_review_lessons,
    omit_key_use,
    put_each_lesson_in_the_pack_event_prefix,
    seed_timing_lessons,
)
from tests.unit.test_identity_claim_codes import wire
from tests.unit.test_lessons_read import Vault


@pytest.fixture
def lesson_review_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    writer = vault.key("writer", "memory_proposal_agent", "alpha")
    reviewer = vault.key("reviewer", "admin_agent", "alpha")
    admin = vault.key("admin", "admin_agent")
    peer = vault.key("peer", "memory_proposal_agent", "alpha")
    scoped = vault.key("scoped", "project_scoped_agent", "alpha")
    reader = vault.key("reader", "read_only_agent", "alpha")
    trusted = vault.key("trusted", "trusted_local_agent")
    outside = vault.key("outside", "memory_proposal_agent", "beta")
    outside_admin = vault.key("outside-admin", "admin_agent", "beta")

    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        if key is None:
            monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        else:
            monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    def snapshot(key):
        with closing(sqlite3.connect(vault.path)) as conn:
            conn.row_factory = sqlite3.Row
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
            rows = {}
            for (table,) in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                rows[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {quoted}")]
            return omit_key_use(rows, key)

    return call, snapshot, writer, reviewer, admin, peer, scoped, reader, trusted, outside, outside_admin, vault


def test_a_read_only_project_key_cannot_read_waiting_or_rejected_lessons(lesson_review_sqlite):
    """Guard edit: include active lessons in the review-only states."""
    call, snapshot, writer, reviewer, admin, peer, scoped, reader, trusted, outside, outside_admin, _vault = lesson_review_sqlite
    check_lesson_visibility(call, writer, reviewer, [
        (peer, True, False), (scoped, True, False), (reader, True, False), (trusted, True, False),
        (writer, True, True), (reviewer, True, True), (admin, True, True), (outside, False, False),
        (outside_admin, False, False),
    ], snapshot)


def test_sqlite_lesson_idempotency_conflicts_withhold_the_row(lesson_review_sqlite):
    """Guard edit: also refuse the writer and decider replay of a review-only lesson."""
    call, snapshot, writer, reviewer, admin, peer, scoped, _reader, trusted, outside, outside_admin, _vault = lesson_review_sqlite
    check_lesson_replays(call, writer, reviewer, [
        (peer, True, False), (scoped, True, False), (trusted, True, False), (writer, True, True),
        (reviewer, True, True), (admin, True, True), (outside, False, False), (outside_admin, False, False),
    ], snapshot)


def test_sqlite_lesson_mutations_use_the_missing_id_answer(lesson_review_sqlite):
    """Guard edit: also hide a review-only lesson from its writer and decider at a write verb."""
    call, snapshot, writer, reviewer, _admin, peer, scoped, reader, trusted, outside, outside_admin, _vault = lesson_review_sqlite
    check_hidden_lesson_verbs(call, writer, reviewer, (peer, scoped, reader, trusted, outside, outside_admin), snapshot)


@pytest.mark.parametrize("shape", ("typed", "incidental", "object"))
def test_sqlite_saved_lesson_references_follow_review_standing(lesson_review_sqlite, shape):
    """Guard edit: remove references to review-only lessons even for their writer."""
    call, _snapshot, *keys, vault = lesson_review_sqlite

    @contextmanager
    def store():
        with sqlite_user_connection(vault.path, vault.user) as conn:
            yield SQLiteVNextStore(conn, vault.user)

    @contextmanager
    def keyless_owner():
        with sqlite_user_connection(vault.path, vault.user) as conn:
            conn.execute("UPDATE agent_api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE revoked_at IS NULL")
        try:
            yield
        finally:
            with sqlite_user_connection(vault.path, vault.user) as conn:
                conn.execute("UPDATE agent_api_keys SET revoked_at = NULL WHERE revoked_at IS NOT NULL")

    check_saved_lesson_references(call, store, keys, shape, keyless_owner=keyless_owner)


def test_event_admission_refreshes_lessons_and_reuses_ordinary_memory_labels():
    """Guard edit: reload ordinary cached rows as well as lessons."""
    ordinary = {"id": "ordinary", "domain": "project", "sensitivity": "internal", "status": "active",
                "value": {"text": "Ordinary memory"}}
    edited = {**ordinary, "id": "edited"}
    rejected = {**edited, "status": "rejected", "value": {"lesson": LESSON_BLOCK}, "created_by_agent_id": "writer"}

    class Store:
        native_review_lesson_selection = True

        def __init__(self):
            self.reads = []

        def read_label_rows(self, kind, ids, *, lesson_blocks_only=False):
            assert kind == "memory"
            self.reads.append((ids, lesson_blocks_only))
            return [row for row in (ordinary, rejected)
                    if row["id"] in ids and (not lesson_blocks_only or row is rejected)]

    store = Store()
    guard = LabelGuard(store=store, active=True, sensitivity_allowed=("internal",), lesson_reader="peer")
    assert guard.admit_rows("memory", [ordinary, edited]) == [ordinary, edited]
    events = [{"event_type": "memory.updated", "target_type": "memory", "target_id": row["id"]}
              for row in (ordinary, edited)]
    assert guard.admit_events(events) == events[:1]
    assert store.reads == [(["ordinary", "edited"], True)]


def test_sqlite_keyless_lesson_review_uses_the_resolved_identity(tmp_path, monkeypatch):
    """Guard edit: include active lessons in the review-only states."""
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    vault = Vault(tmp_path / "vault.db")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    check_keyless_lesson_visibility(lambda claims, tool, args: wire(context, tool, {**args, **claims}))


def test_sqlite_legacy_correction_gives_the_missing_answer_for_unpublished_lessons(tmp_path, monkeypatch):
    """Guard edit: withhold even a decider's unpublished correction target."""
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    vault = Vault(tmp_path / "vault.db")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    check_keyless_legacy_lesson_correction(lambda claims, tool, args: wire(context, tool, {**args, **claims}))


def test_sqlite_context_pack_recent_changes_follow_review_standing(lesson_review_sqlite):
    """Guard edit: return no recent changes to callers with an identity."""
    call, _snapshot, writer, reviewer, admin, peer, scoped, reader, trusted, outside, outside_admin, vault = lesson_review_sqlite
    rows = make_review_lessons(call, writer, reviewer)
    with sqlite_user_connection(vault.path, vault.user) as conn:
        prefix = put_each_lesson_in_the_pack_event_prefix(SQLiteVNextStore(conn, vault.user), rows)
    for key, same_scope, may_read_review in (
        (peer, True, False), (scoped, True, False), (reader, True, False), (trusted, True, False),
        (writer, True, True), (reviewer, True, True), (admin, True, True), (outside, False, False),
        (outside_admin, False, False),
    ):
        check_lesson_pack(
            lambda: call(key, "alice_context_pack", {"query": "rebasing", "max_tokens": 8000, "debug": True}),
            rows, prefix, may_read_review=may_read_review, same_scope=same_scope,
        )


def test_sqlite_lesson_review_cost_over_200_and_2000_waiting_lessons(tmp_path, monkeypatch):
    """Guard edit: stop returning active lessons in the all-status review queue."""
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))

    def setup(count):
        vault = Vault(tmp_path / f"vault-{count}.db")
        reader = vault.key("timing-reader", "read_only_agent", "alpha")
        admin = vault.key("timing-admin", "admin_agent", "alpha")
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
        monkeypatch.setenv("ALICE_AGENT_API_KEY", admin)
        failed, published = wire(context, "alice_memory_commit", {"lesson": LESSON_BLOCK})
        assert not failed and published["status"] == "committed"
        with sqlite_user_connection(vault.path, vault.user) as conn:
            seed_timing_lessons(SQLiteVNextStore(conn, vault.user), count)

        def read():
            monkeypatch.setenv("ALICE_AGENT_API_KEY", reader)
            failed, result = wire(context, "alice_memory_review", {"status": "all", "limit": 1})
            assert not failed and result["count"] == 1, result
            assert result["items"][0]["id"] == published["memory"]["id"]

        return read

    check_lesson_review_growth(setup(200), setup(2000))


def test_lesson_reader_sees_writer_and_reviewer_ids(tmp_path, monkeypatch):
    """The limitations page changes if lesson publication stops returning the reviewer's id.

    Mutation: replace the publishing revision's agent actor id with ``owner``.
    """
    monkeypatch.setenv("ALICE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    vault = Vault(tmp_path / "vault.db")
    writer = vault.key("writer", "project_scoped_agent", "alpha")
    reviewer = vault.key("reviewer", "admin_agent", "alpha")
    reader = vault.key("reader", "read_only_agent", "alpha")
    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)

    def call(key, tool, arguments):
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
        return wire(context, tool, arguments)

    check_lesson_publisher_ids(call, writer, reviewer, reader)
