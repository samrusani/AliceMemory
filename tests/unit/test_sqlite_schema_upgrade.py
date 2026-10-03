from __future__ import annotations

import json
import sqlite3

import pytest

from alicebot_api import sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user
from alicebot_api.vnext_capture import VNextCaptureService, capture_dedupe_key_for_text


def _table_statement(name: str) -> str:
    marker = f"CREATE TABLE IF NOT EXISTS {name} ("
    return next(statement for statement in sqlite_schema._TABLE_STATEMENTS if marker in statement)


def _v0_7_memories_statement() -> str:
    """The v0.7 memory shape at the columns/constraint that later changed."""
    statement = _table_statement("memories")
    for line in (
        "      project_id TEXT NULL,\n",
        "      created_by_agent_id TEXT NULL,\n",
        "      run_id TEXT NULL,\n",
        "      superseded_by TEXT NULL,\n",
        "      supersedes TEXT NULL,\n",
        "      fact_keys TEXT NULL,\n",
    ):
        statement = statement.replace(line, "")
    return statement.replace(", 'stale'", "")


def test_v0_7_data_bearing_file_upgrades_status_check_and_preserves_children() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(_table_statement("users"))
    conn.execute(_table_statement("sources"))
    conn.execute(_v0_7_memories_statement())
    conn.execute(_table_statement("memory_revisions"))
    conn.execute(_table_statement("open_loops"))

    user_id = "00000000-0000-0000-0000-000000000201"
    memory_id = "00000000-0000-0000-0000-000000000202"
    revision_id = "00000000-0000-0000-0000-000000000203"
    loop_id = "00000000-0000-0000-0000-000000000204"
    conn.execute(
        "INSERT INTO users (id, email, display_name) VALUES (?, ?, ?)",
        (user_id, "sqlite-upgrade@example.com", "SQLite Upgrade"),
    )
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids,
          memory_type, canonical_text
        )
        VALUES (?, ?, 'upgrade.seed', '{"text":"before"}', 'active',
                '[]', 'semantic', 'Preserve this seeded memory')
        """,
        (memory_id, user_id),
    )
    conn.execute(
        """
        INSERT INTO memory_revisions (
          id, user_id, memory_id, sequence_no, action, memory_key,
          source_event_ids, candidate, revision_number, revision_type,
          text_after, metadata_json
        )
        VALUES (?, ?, ?, 1, 'ADD', 'upgrade.seed', '[]', '{}', 1,
                'created', 'Preserve this seeded memory', '{}')
        """,
        (revision_id, user_id, memory_id),
    )
    conn.execute(
        """
        INSERT INTO open_loops (id, user_id, memory_id, title, status)
        VALUES (?, ?, ?, 'Verify public release', 'open')
        """,
        (loop_id, user_id, memory_id),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="memories_status_check"):
        conn.execute("UPDATE memories SET status = 'stale' WHERE id = ?", (memory_id,))
    conn.rollback()

    sqlite_schema.bootstrap_sqlite_schema(conn)
    conn.execute("UPDATE memories SET status = 'stale' WHERE id = ?", (memory_id,))
    conn.commit()

    assert conn.execute("SELECT status, canonical_text FROM memories WHERE id = ?", (memory_id,)).fetchone() == (
        "stale",
        "Preserve this seeded memory",
    )
    assert conn.execute(
        "SELECT memory_id, text_after FROM memory_revisions WHERE id = ?", (revision_id,)
    ).fetchone() == (memory_id, "Preserve this seeded memory")
    assert conn.execute("SELECT memory_id, title FROM open_loops WHERE id = ?", (loop_id,)).fetchone() == (
        memory_id,
        "Verify public release",
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {
        "project_id",
        "created_by_agent_id",
        "run_id",
        "superseded_by",
        "supersedes",
        "fact_keys",
    }.issubset({row[1] for row in conn.execute("PRAGMA table_info(memories)")})

    # Opening the upgraded file again is idempotent and leaves the row intact.
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    sqlite_schema.bootstrap_sqlite_schema(conn)
    conn.set_trace_callback(None)
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not [statement for statement in traced if "ROW_NUMBER() OVER" in statement]


def test_bootstrap_canonicalizes_legacy_duplicate_retry_identifiers() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    conn.execute("DROP INDEX memories_user_commit_digest_unique_idx")
    conn.execute("DROP INDEX memories_user_confirmation_id_unique_idx")
    user_id = "00000000-0000-0000-0000-000000000211"
    first_id = "00000000-0000-0000-0000-000000000212"
    second_id = "00000000-0000-0000-0000-000000000213"
    conn.execute(
        "INSERT INTO users (id, email) VALUES (?, ?)",
        (user_id, "duplicate-retry@example.com"),
    )
    for memory_id, key, created_at in (
        (first_id, "retry.first", "2026-01-01T00:00:00Z"),
        (second_id, "retry.second", "2026-01-02T00:00:00Z"),
    ):
        conn.execute(
            """
            INSERT INTO memories (
              id, user_id, memory_key, value, status, source_event_ids,
              memory_type, canonical_text, commit_digest, confirmation_id,
              metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, '{"text":"same retry"}', 'active', '[]',
                    'semantic', 'Same retry', 'duplicate-digest',
                    'duplicate-confirmation',
                    '{"agentic_memory":{"idempotency_key":"duplicate-digest","confirmation":{"confirmation_id":"duplicate-confirmation"}}}',
                    ?, ?)
            """,
            (memory_id, user_id, key, created_at, created_at),
        )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    rows = conn.execute(
        """
        SELECT id, commit_digest, confirmation_id, metadata_json
        FROM memories
        ORDER BY created_at, id
        """
    ).fetchall()
    assert rows[0][0:3] == (first_id, "duplicate-digest", "duplicate-confirmation")
    assert rows[1][0:3] == (second_id, None, None)
    assert first_id in rows[1][3]
    with pytest.raises(sqlite3.IntegrityError, match="memories.user_id, memories.commit_digest"):
        conn.execute(
            """
            INSERT INTO memories (
              id, user_id, memory_key, value, status, source_event_ids,
              memory_type, canonical_text, commit_digest
            )
            VALUES ('00000000-0000-0000-0000-000000000214', ?,
                    'retry.third', '{}', 'active', '[]', 'semantic',
                    'Third retry', 'duplicate-digest')
            """,
            (user_id,),
        )


def test_bootstrap_keeps_duplicate_retry_identifiers_on_live_row_over_tombstone() -> None:
    """Audit P1 #3: dedup must not strand identifiers on a deleted row.

    A legacy file may hold an older archived/deleted row and a newer active
    row that share a retry (commit_digest) and confirmation (confirmation_id)
    identifier. Runtime lookups filter ``deleted_at IS NULL``, so the live row
    must retain the identifiers; the earliest-row rule strands them on the
    tombstone where nothing can read them while the unique index blocks reuse.
    """
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    conn.execute("DROP INDEX memories_user_commit_digest_unique_idx")
    conn.execute("DROP INDEX memories_user_confirmation_id_unique_idx")
    user_id = "00000000-0000-0000-0000-000000000241"
    tombstone_id = "00000000-0000-0000-0000-000000000242"
    live_id = "00000000-0000-0000-0000-000000000243"
    conn.execute(
        "INSERT INTO users (id, email) VALUES (?, ?)",
        (user_id, "tombstone-retry@example.com"),
    )
    for memory_id, key, created_at, status, deleted_at in (
        (tombstone_id, "retry.old", "2026-01-01T00:00:00Z", "archived", "2026-01-01T01:00:00Z"),
        (live_id, "retry.new", "2026-01-02T00:00:00Z", "active", None),
    ):
        conn.execute(
            """
            INSERT INTO memories (
              id, user_id, memory_key, value, status, source_event_ids,
              memory_type, canonical_text, commit_digest, confirmation_id,
              metadata_json, created_at, updated_at, deleted_at
            )
            VALUES (?, ?, ?, '{"text":"same retry"}', ?, '[]',
                    'semantic', 'Same retry', 'duplicate-digest',
                    'duplicate-confirmation',
                    '{"agentic_memory":{"idempotency_key":"duplicate-digest","confirmation":{"confirmation_id":"duplicate-confirmation"}}}',
                    ?, ?, ?)
            """,
            (memory_id, user_id, key, status, created_at, created_at, deleted_at),
        )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    # The live (non-deleted) row must keep the retry/confirmation identifiers.
    live_digest, live_confirmation = conn.execute(
        "SELECT commit_digest, confirmation_id FROM memories WHERE id = ?", (live_id,)
    ).fetchone()
    assert (live_digest, live_confirmation) == ("duplicate-digest", "duplicate-confirmation")
    # The tombstone relinquishes them and records the live canonical row id.
    tombstone_digest, tombstone_confirmation, tombstone_metadata = conn.execute(
        "SELECT commit_digest, confirmation_id, metadata_json FROM memories WHERE id = ?",
        (tombstone_id,),
    ).fetchone()
    assert (tombstone_digest, tombstone_confirmation) == (None, None)
    assert live_id in tombstone_metadata


def test_bootstrap_repairs_identifiers_left_on_tombstone_by_prior_bootstrap() -> None:
    """The corrective pass repairs a file already mis-deduplicated by v0.9.2.

    Simulates the state the shipped (buggy) dedup produced: the older deleted
    row keeps the identifiers while the newer live row was cleared and points
    back to the tombstone. Re-opening the file must move the identifiers to the
    live row without needing the non-unique legacy indexes to still be present.
    """
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000251"
    tombstone_id = "00000000-0000-0000-0000-000000000252"
    live_id = "00000000-0000-0000-0000-000000000253"
    conn.execute(
        "INSERT INTO users (id, email) VALUES (?, ?)",
        (user_id, "repair-retry@example.com"),
    )
    # Older archived row still holding the identifiers (the mis-assignment).
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids,
          memory_type, canonical_text, commit_digest, confirmation_id,
          metadata_json, created_at, updated_at, deleted_at
        )
        VALUES (?, ?, 'retry.old', '{"text":"same retry"}', 'archived', '[]',
                'semantic', 'Same retry', 'duplicate-digest',
                'duplicate-confirmation',
                '{"agentic_memory":{"idempotency_key":"duplicate-digest","confirmation":{"confirmation_id":"duplicate-confirmation"}}}',
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
        """,
        (tombstone_id, user_id),
    )
    # Newer live row that the buggy dedup cleared, pointing back to the tombstone.
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids,
          memory_type, canonical_text, commit_digest, confirmation_id,
          metadata_json, created_at, updated_at, deleted_at
        )
        VALUES (?, ?, 'retry.new', '{"text":"same retry"}', 'active', '[]',
                'semantic', 'Same retry', NULL, NULL,
                ?, '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z', NULL)
        """,
        (
            live_id,
            user_id,
            json.dumps(
                {
                    "agentic_memory": {"confirmation": {}},
                    "lifecycle_migration": {
                        "duplicate_commit_digest_canonical_memory_id": tombstone_id,
                        "duplicate_confirmation_id_canonical_memory_id": tombstone_id,
                    },
                }
            ),
        ),
    )
    conn.commit()

    def _assert_repaired() -> None:
        live_digest, live_confirmation, live_metadata = conn.execute(
            "SELECT commit_digest, confirmation_id, metadata_json FROM memories WHERE id = ?",
            (live_id,),
        ).fetchone()
        assert (live_digest, live_confirmation) == ("duplicate-digest", "duplicate-confirmation")
        # The live canonical row no longer points at a tombstone.
        live_meta = json.loads(live_metadata)
        assert "duplicate_commit_digest_canonical_memory_id" not in live_meta.get("lifecycle_migration", {})
        assert "duplicate_confirmation_id_canonical_memory_id" not in live_meta.get("lifecycle_migration", {})
        tombstone_digest, tombstone_confirmation = conn.execute(
            "SELECT commit_digest, confirmation_id FROM memories WHERE id = ?", (tombstone_id,)
        ).fetchone()
        assert (tombstone_digest, tombstone_confirmation) == (None, None)

    sqlite_schema.bootstrap_sqlite_schema(conn)
    _assert_repaired()
    # Re-opening the repaired file is a safe no-op.
    sqlite_schema.bootstrap_sqlite_schema(conn)
    _assert_repaired()


def test_bootstrap_promotes_and_reads_legacy_nested_multi_project_scope() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000231"
    memory_id = "00000000-0000-0000-0000-000000000232"
    ensure_sqlite_user(conn, user_id, "legacy-scope@example.com")
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids,
          memory_type, canonical_text, domain, sensitivity, metadata_json
        )
        VALUES (?, ?, 'legacy.nested.scope', '{}', 'active', '[]',
                'semantic', 'Legacy nested scope memory', 'project', 'internal',
                '{"agentic_memory":{"project_scope":[" alicebot ","hermes","alicebot"]}}')
        """,
        (memory_id, user_id),
    )
    conn.commit()

    legacy_store = SQLiteVNextStore(conn, user_id)
    legacy_row = legacy_store.get_memory(memory_id)
    assert legacy_row is not None
    assert legacy_row["project_scope"] == ["alicebot", "hermes"]
    assert [
        row["id"]
        for row in legacy_store.search_memories(
            query="legacy nested scope",
            projects=("hermes",),
        )
    ] == [memory_id]

    sqlite_schema.bootstrap_sqlite_schema(conn)

    raw_project_id, raw_metadata = conn.execute(
        "SELECT project_id, metadata_json FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    assert raw_project_id is None
    assert json.loads(raw_metadata)["project_scope"] == ["alicebot", "hermes"]

    store = SQLiteVNextStore(conn, user_id)
    upgraded = store.get_memory(memory_id)
    assert upgraded is not None
    assert upgraded["project_scope"] == ["alicebot", "hermes"]
    assert [row["id"] for row in store.search_memories(query="legacy nested scope", projects=("hermes",))] == [
        memory_id
    ]
    assert (
        store.search_memories(
            query="legacy nested scope",
            projects=("unrelated",),
        )
        == []
    )


def test_bootstrap_reopen_never_rewrites_an_explicit_empty_project_scope(tmp_path) -> None:
    db_path = tmp_path / "explicit-empty-scope.db"
    user_id = "00000000-0000-0000-0000-000000000261"
    memory_id = "00000000-0000-0000-0000-000000000262"
    metadata_text = '{"project_scope":[],"agentic_memory":{"project_scope":["stale-project"]}}'
    conn = sqlite3.connect(db_path)
    sqlite_schema.bootstrap_sqlite_schema(conn)
    ensure_sqlite_user(conn, user_id, "explicit-empty-scope@example.com")
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids,
          memory_type, canonical_text, domain, sensitivity, project_id,
          metadata_json
        )
        VALUES (?, ?, 'explicit.empty.scope', '{}', 'active', '[]',
                'semantic', 'Explicit empty scope', 'project', 'internal',
                'stale-project', ?)
        """,
        (memory_id, user_id, metadata_text),
    )
    conn.commit()
    conn.close()

    for _ in range(2):
        conn = sqlite3.connect(db_path)
        sqlite_schema.bootstrap_sqlite_schema(conn)
        conn.commit()
        conn.close()

    conn = sqlite3.connect(db_path)
    raw_project_id, raw_metadata = conn.execute(
        "SELECT project_id, metadata_json FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    assert raw_project_id == "stale-project"
    assert raw_metadata == metadata_text
    store = SQLiteVNextStore(conn, user_id)
    assert store.list_memories(projects=("stale-project",)) == []
    assert (
        store.find_live_memory_by_canonical_text(
            "Explicit empty scope",
            domain="project",
            sensitivity="internal",
            project_scope=(),
        )["id"]
        == memory_id
    )
    conn.close()


def test_bootstrap_backfills_legacy_source_dedupe_keys_per_project_scope() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000271"
    ensure_sqlite_user(conn, user_id, "legacy-source-scope@example.com")
    text = "Fact: The same evidence may belong to distinct projects."
    conn.execute("DROP INDEX sources_user_dedupe_key_unique_idx")
    for source_id, project in (
        ("00000000-0000-0000-0000-000000000272", "Alpha"),
        ("00000000-0000-0000-0000-000000000273", "Beta"),
    ):
        conn.execute(
            """
            INSERT INTO sources (
              id, user_id, source_type, content_hash, dedupe_key, metadata_json
            )
            VALUES (?, ?, 'manual_text', 'sha256:legacy-unscoped', NULL, ?)
            """,
            (
                source_id,
                user_id,
                json.dumps({"raw_text": text, "project_scope": [project]}),
            ),
        )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    rows = conn.execute("SELECT dedupe_key FROM sources ORDER BY id").fetchall()
    assert all(row[0].startswith("capture-md5:") for row in rows)
    assert rows[0][0] != rows[1][0]
    index = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sources_user_dedupe_key_unique_idx'").fetchone()[
        0
    ]
    assert "UNIQUE INDEX" in index
    assert "deleted_at IS NULL" in index


def test_bootstrap_repairs_precanonical_source_dedupe_identity_once() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000281"
    ensure_sqlite_user(conn, user_id, "source-identity-repair@example.com")
    raw_text = "\tFact: canonical scope identity repairs old keys.\n"
    for source_id, captured_at, scope, old_key in (
        (
            "00000000-0000-0000-0000-000000000282",
            "2026-01-01T00:00:00Z",
            [" Beta ", "ALICE", "alice"],
            "capture-md5:old-a",
        ),
        (
            "00000000-0000-0000-0000-000000000283",
            "2026-01-02T00:00:00Z",
            ["alice", "beta"],
            "capture-md5:old-b",
        ),
    ):
        conn.execute(
            """
            INSERT INTO sources (
              id, user_id, source_type, content_hash, dedupe_key, captured_at,
              domain, sensitivity, metadata_json
            ) VALUES (?, ?, 'manual_text', ?, ?, ?, 'project', 'private', ?)
            """,
            (
                source_id,
                user_id,
                f"sha256:{source_id}",
                old_key,
                captured_at,
                json.dumps({"raw_text": raw_text, "project_scope": scope}),
            ),
        )
    conn.execute(
        "DELETE FROM alice_schema_state WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    expected = capture_dedupe_key_for_text(
        raw_text,
        ("alice", "beta"),
        domain="project",
        sensitivity="private",
    )
    assert conn.execute("SELECT dedupe_key FROM sources ORDER BY captured_at, id").fetchall() == [(expected,), (None,)]
    assert conn.execute(
        "SELECT value FROM alice_schema_state WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    ).fetchone() == (sqlite_schema._SOURCE_DEDUPE_IDENTITY_VERSION,)

    # The versioned fast path does not rewrite already-repaired identities.
    sqlite_schema.bootstrap_sqlite_schema(conn)
    assert conn.execute("SELECT dedupe_key FROM sources ORDER BY captured_at, id").fetchall() == [(expected,), (None,)]


def test_bootstrap_v6_clears_live_whitespace_raw_text_without_reclassifying_other_sources() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000411"
    ensure_sqlite_user(conn, user_id, "source-whitespace-v6@example.com")
    whitespace_cases = {
        "ascii": " \t\r\n",
        "unit_separator_control": "\u001c\u001f",
        "nbsp": "\u00a0",
        "nel": "\u0085",
        "em_space": "\u2003",
    }
    expected_by_id: dict[str, str | None] = {}
    for index, (name, raw_text) in enumerate(whitespace_cases.items(), start=1):
        source_id = f"00000000-0000-0000-0006-{index:012d}"
        expected_by_id[source_id] = None
        conn.execute(
            """
            INSERT INTO sources (
              id, user_id, source_type, content_hash, dedupe_key, captured_at,
              domain, sensitivity, metadata_json
            ) VALUES (?, ?, 'manual_text', ?, ?, ?, 'project', 'private', ?)
            """,
            (
                source_id,
                user_id,
                f"sha256:whitespace-{name}",
                f"capture-md5:pre-v6-{name}",
                f"2026-04-{index:02d}T00:00:00Z",
                json.dumps({"raw_text": raw_text}, ensure_ascii=False),
            ),
        )

    nonempty_id = "00000000-0000-0000-0006-000000000010"
    nonempty_text = "\u00a0Fact: nonempty survives the defensive repair.\u2003"
    expected_by_id[nonempty_id] = capture_dedupe_key_for_text(
        nonempty_text,
        domain="project",
        sensitivity="private",
    )
    absent_id = "00000000-0000-0000-0006-000000000011"
    expected_by_id[absent_id] = "sha256:absent-raw-text"
    nonstring_id = "00000000-0000-0000-0006-000000000012"
    expected_by_id[nonstring_id] = "sha256:nonstring-raw-text"
    for source_id, content_hash, old_key, metadata in (
        (nonempty_id, "sha256:nonempty", "capture-md5:pre-v6-nonempty", {"raw_text": nonempty_text}),
        (absent_id, "sha256:absent-raw-text", "capture-md5:pre-v6-absent", {"source": "legacy"}),
        (
            nonstring_id,
            "sha256:nonstring-raw-text",
            "capture-md5:pre-v6-nonstring",
            {"raw_text": ["not", "text"]},
        ),
    ):
        conn.execute(
            """
            INSERT INTO sources (
              id, user_id, source_type, content_hash, dedupe_key, captured_at,
              domain, sensitivity, metadata_json
            ) VALUES (?, ?, 'manual_text', ?, ?,
                      '2026-04-10T00:00:00Z', 'project', 'private', ?)
            """,
            (source_id, user_id, content_hash, old_key, json.dumps(metadata, ensure_ascii=False)),
        )

    deleted_id = "00000000-0000-0000-0006-000000000013"
    conn.execute(
        """
        INSERT INTO sources (
          id, user_id, source_type, content_hash, dedupe_key, captured_at,
          domain, sensitivity, metadata_json, deleted_at
        ) VALUES (?, ?, 'manual_text', 'sha256:deleted-whitespace',
                  'capture-md5:deleted-whitespace', '2026-04-11T00:00:00Z',
                  'project', 'private', ?, '2026-04-12T00:00:00Z')
        """,
        (deleted_id, user_id, json.dumps({"raw_text": "\u00a0"}, ensure_ascii=False)),
    )
    conn.execute(
        "UPDATE alice_schema_state SET value = '5' WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    )
    conn.commit()

    def assert_repaired() -> None:
        rows = conn.execute(
            "SELECT id, dedupe_key FROM sources WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        assert dict(rows) == expected_by_id
        assert conn.execute(
            "SELECT dedupe_key FROM sources WHERE id = ?",
            (deleted_id,),
        ).fetchone() == ("capture-md5:deleted-whitespace",)
        assert conn.execute(
            "SELECT value FROM alice_schema_state WHERE key = ?",
            (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
        ).fetchone() == ("6",)

    sqlite_schema.bootstrap_sqlite_schema(conn)
    assert_repaired()
    sqlite_schema.bootstrap_sqlite_schema(conn)
    assert_repaired()


def test_bootstrap_installs_bounded_project_update_event_indexes() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'index' AND name LIKE 'event_log_project_update_%'
        ORDER BY name
        """
    ).fetchall()
    definitions = {name: sql for name, sql in rows}
    assert set(definitions) == {
        "event_log_project_update_artifact_id_idx",
        "event_log_project_update_candidate_memory_id_idx",
        "event_log_project_update_memory_id_idx",
        "event_log_project_update_target_idx",
    }
    event_types = (
        "project.update_candidate_created",
        "project.update_candidate_accepted",
        "project.update_candidate_rejected",
    )
    for definition in definitions.values():
        assert "WHERE event_type IN" in definition
        assert all(f"'{event_type}'" in definition for event_type in event_types)
        assert "user_id" in definition
        assert "event_type" in definition
        assert "occurred_at DESC" in definition
        assert "id DESC" in definition
    assert "target_type" in definitions["event_log_project_update_target_idx"]
    assert "target_id" in definitions["event_log_project_update_target_idx"]
    assert "target_type IS NOT NULL" in definitions["event_log_project_update_target_idx"]
    assert "target_id IS NOT NULL" in definitions["event_log_project_update_target_idx"]
    assert "json_extract(payload_json, '$.artifact_id')" in definitions[
        "event_log_project_update_artifact_id_idx"
    ]
    assert "json_extract(payload_json, '$.candidate_memory_id')" in definitions[
        "event_log_project_update_candidate_memory_id_idx"
    ]
    assert "json_extract(payload_json, '$.memory_id')" in definitions[
        "event_log_project_update_memory_id_idx"
    ]


def test_bootstrap_v5_dedupe_repair_preserves_unicode_scope_distinctions() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000311"
    ensure_sqlite_user(conn, user_id, "source-unicode-identity@example.com")
    raw_text = "Fact: conservative Unicode project identity is stable."
    scopes = ("ALICE", "alice", "İ", "i", "Straße", "STRASSE", "Σ", "σ", "ς")
    for index, scope in enumerate(scopes, start=1):
        conn.execute(
            """
            INSERT INTO sources (
              id, user_id, source_type, content_hash, dedupe_key, captured_at,
              domain, sensitivity, metadata_json
            ) VALUES (?, ?, 'manual_text', ?, ?, ?, 'project', 'private', ?)
            """,
            (
                f"00000000-0000-0000-0002-{index:012d}",
                user_id,
                f"sha256:unicode-{index}",
                f"capture-md5:old-unicode-{index}",
                f"2026-01-{index:02d}T00:00:00Z",
                json.dumps({"raw_text": raw_text, "project_scope": [scope]}, ensure_ascii=False),
            ),
        )
    conn.execute(
        "UPDATE alice_schema_state SET value = '2' WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    rows = conn.execute("SELECT metadata_json, dedupe_key FROM sources ORDER BY captured_at, id").fetchall()
    repaired = {json.loads(metadata_json)["project_scope"][0]: dedupe_key for metadata_json, dedupe_key in rows}
    assert repaired["ALICE"] == capture_dedupe_key_for_text(
        raw_text,
        ("alice",),
        domain="project",
        sensitivity="private",
    )
    assert repaired["alice"] is None
    for scope in ("İ", "i", "Straße", "STRASSE", "Σ", "σ", "ς"):
        assert repaired[scope] == capture_dedupe_key_for_text(
            raw_text,
            (scope,),
            domain="project",
            sensitivity="private",
        )
    assert len({repaired[scope] for scope in ("İ", "i", "Straße", "STRASSE", "Σ", "σ", "ς")}) == 7
    assert conn.execute(
        "SELECT value FROM alice_schema_state WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    ).fetchone() == (sqlite_schema._SOURCE_DEDUPE_IDENTITY_VERSION,)


def test_bootstrap_v5_canonicalizes_finite_integral_numeric_source_scope() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0002-000000000020"
    ensure_sqlite_user(conn, user_id, "source-numeric-identity@example.com")
    raw_text = "Fact: Integral JSON spellings share one project identity."
    cases = (
        ("00000000-0000-0000-0002-000000000021", "2026-01-01T00:00:00Z", [1]),
        ("00000000-0000-0000-0002-000000000022", "2026-01-02T00:00:00Z", [1.0]),
        ("00000000-0000-0000-0002-000000000023", "2026-01-03T00:00:00Z", [-0.0]),
        ("00000000-0000-0000-0002-000000000024", "2026-01-04T00:00:00Z", [0]),
        ("00000000-0000-0000-0002-000000000025", "2026-01-05T00:00:00Z", [True]),
    )
    for source_id, captured_at, scope in cases:
        conn.execute(
            """
            INSERT INTO sources (
              id, user_id, source_type, content_hash, dedupe_key, captured_at,
              domain, sensitivity, metadata_json
            ) VALUES (?, ?, 'manual_text', ?, ?, ?, 'project', 'private', ?)
            """,
            (
                source_id,
                user_id,
                f"sha256:{source_id}",
                f"capture-md5:old-{source_id}",
                captured_at,
                json.dumps({"raw_text": raw_text, "project_scope": scope}),
            ),
        )
    conn.execute(
        "UPDATE alice_schema_state SET value = '4' WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    repaired = conn.execute("SELECT id, dedupe_key FROM sources ORDER BY captured_at, id").fetchall()
    assert repaired[0][1] == capture_dedupe_key_for_text(
        raw_text,
        ("1",),
        domain="project",
        sensitivity="private",
    )
    assert repaired[1][1] is None
    assert repaired[2][1] == capture_dedupe_key_for_text(
        raw_text,
        ("0",),
        domain="project",
        sensitivity="private",
    )
    assert repaired[3][1] is None
    assert repaired[4][1] == capture_dedupe_key_for_text(
        raw_text,
        ("True",),
        domain="project",
        sensitivity="private",
    )


@pytest.mark.parametrize(
    ("case_name", "scope_metadata"),
    (
        (
            "canonical",
            {
                "project_scope": [" Legacy Project "],
                "agentic_memory": {"project_scope": ["stale-project"]},
            },
        ),
        (
            "agentic_project_scope",
            {
                "project_id": "stale-project",
                "agentic_memory": {"project_scope": [" Legacy Project "]},
            },
        ),
        (
            "agent_identity_project_scope",
            {
                "project_id": "stale-project",
                "agent_identity": {"project_scope": [" Legacy Project "]},
            },
        ),
        (
            "metadata_canonical",
            {"metadata_json": {"project_scope": [" Legacy Project "]}},
        ),
        (
            "scope_canonical",
            {"scope_json": {"project_scope": [" Legacy Project "]}},
        ),
        (
            "metadata_identity_project_scope",
            {"metadata_json": {"agent_identity": {"project_scope": [" Legacy Project "]}}},
        ),
        (
            "scope_agentic_project_scope",
            {"scope_json": {"agentic_memory": {"project_scope": [" Legacy Project "]}}},
        ),
        (
            "project_id",
            {"project_id": " Legacy Project "},
        ),
        (
            "project",
            {"project": " Legacy Project "},
        ),
        (
            "projects",
            {"projects": [" Legacy Project "]},
        ),
        (
            "agentic_project_id",
            {"agentic_memory": {"project_id": " Legacy Project "}},
        ),
        (
            "agentic_project",
            {"agentic_memory": {"project": " Legacy Project "}},
        ),
        (
            "agentic_projects",
            {"agentic_memory": {"projects": [" Legacy Project "]}},
        ),
        (
            "metadata_project_id",
            {"metadata_json": {"project_id": " Legacy Project "}},
        ),
        (
            "scope_project",
            {"scope_json": {"project": " Legacy Project "}},
        ),
        (
            "scope_agentic_projects",
            {"scope_json": {"agentic_memory": {"projects": [" Legacy Project "]}}},
        ),
    ),
)
def test_bootstrap_v4_resolves_legacy_source_scope_and_blocks_duplicate_recapture(
    case_name: str,
    scope_metadata: dict[str, object],
) -> None:
    """Every source-reachable resolver form repairs to the scoped identity."""

    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000321"
    source_id = "00000000-0000-0000-0000-000000000322"
    ensure_sqlite_user(conn, user_id, f"source-{case_name}@example.com")
    raw_text = f"Fact: {case_name} preserves its legacy source scope."
    metadata = {"raw_text": raw_text, **scope_metadata}
    conn.execute(
        """
        INSERT INTO sources (
          id, user_id, source_type, content_hash, dedupe_key, captured_at,
          domain, sensitivity, metadata_json
        ) VALUES (?, ?, 'manual_text', ?, 'capture-md5:pre-v4',
                  '2026-01-01T00:00:00Z', 'project', 'private', ?)
        """,
        (
            source_id,
            user_id,
            f"sha256:{case_name}",
            json.dumps(metadata),
        ),
    )
    conn.execute(
        "UPDATE alice_schema_state SET value = '3' WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    expected_scoped = capture_dedupe_key_for_text(
        raw_text,
        ("Legacy Project",),
        domain="project",
        sensitivity="private",
    )
    unscoped = capture_dedupe_key_for_text(
        raw_text,
        domain="project",
        sensitivity="private",
    )
    repaired_key = conn.execute(
        "SELECT dedupe_key FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()[0]
    assert repaired_key == expected_scoped
    assert repaired_key != unscoped

    result = VNextCaptureService(SQLiteVNextStore(conn, user_id)).capture_text(
        raw_text,
        project_scope=("Legacy Project",),
        domain="project",
        sensitivity="private",
    )
    assert result.duplicate is True
    assert result.source_id == source_id
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_bootstrap_v4_keeps_blank_nested_canonical_source_scope_authoritative() -> None:
    """Blank nested canonical scope must not resurrect a stale singular alias."""

    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000323"
    source_id = "00000000-0000-0000-0000-000000000324"
    ensure_sqlite_user(conn, user_id, "blank-nested-source-scope@example.com")
    raw_text = "Fact: blank nested canonical source scope remains authoritative."
    conn.execute(
        """
        INSERT INTO sources (
          id, user_id, source_type, content_hash, dedupe_key, captured_at,
          domain, sensitivity, metadata_json
        ) VALUES (?, ?, 'manual_text', ?, 'capture-md5:pre-v4',
                  '2026-01-01T00:00:00Z', 'project', 'private', ?)
        """,
        (
            source_id,
            user_id,
            "sha256:blank-nested-canonical-scope",
            json.dumps(
                {
                    "raw_text": raw_text,
                    "project_id": " Legacy Project ",
                    "agent_identity": {"project_scope": ["\t\n"]},
                }
            ),
        ),
    )
    conn.execute(
        "UPDATE alice_schema_state SET value = '3' WHERE key = ?",
        (sqlite_schema._SOURCE_DEDUPE_IDENTITY_STATE_KEY,),
    )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    unscoped = capture_dedupe_key_for_text(
        raw_text,
        domain="project",
        sensitivity="private",
    )
    stale_scoped = capture_dedupe_key_for_text(
        raw_text,
        ("Legacy Project",),
        domain="project",
        sensitivity="private",
    )
    repaired_key = conn.execute(
        "SELECT dedupe_key FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()[0]
    assert repaired_key == unscoped
    assert repaired_key != stale_scoped

    result = VNextCaptureService(SQLiteVNextStore(conn, user_id)).capture_text(
        raw_text,
        project_scope=("Legacy Project",),
        domain="project",
        sensitivity="private",
    )
    assert result.duplicate is False
    assert result.source_id != source_id
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2


def test_bootstrap_source_dedupe_fast_path_skips_complete_metadata_scan(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000291"
    ensure_sqlite_user(conn, user_id, "source-fast-path@example.com")
    conn.executemany(
        """
        INSERT INTO sources (
          id, user_id, source_type, content_hash, dedupe_key, metadata_json
        )
        VALUES (?, ?, 'manual_text', ?, ?, ?)
        """,
        [
            (
                f"00000000-0000-0000-0001-{index:012d}",
                user_id,
                f"sha256:source-{index}",
                f"capture-md5:source-{index}",
                json.dumps({"raw_text": f"Fact: source {index}"}),
            )
            for index in range(128)
        ],
    )
    conn.commit()

    decode_count = 0
    original_loads = sqlite_schema.json.loads

    def counted_loads(value):
        nonlocal decode_count
        decode_count += 1
        return original_loads(value)

    monkeypatch.setattr(sqlite_schema.json, "loads", counted_loads)
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    sqlite_schema.bootstrap_sqlite_schema(conn)
    conn.set_trace_callback(None)

    dedupe_probes = [
        statement
        for statement in traced
        if "from sources" in statement.casefold()
        and "dedupe_key is null" in statement.casefold()
        and "select id, user_id, content_hash" in statement.casefold()
    ]
    assert len(dedupe_probes) == 1
    assert decode_count == 0
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 128

    query_plan = conn.execute(
        """
        EXPLAIN QUERY PLAN
        SELECT id, user_id, content_hash, metadata_json, domain, sensitivity
        FROM sources
        WHERE deleted_at IS NULL AND dedupe_key IS NULL
        ORDER BY captured_at ASC, id ASC
        """
    ).fetchall()
    assert any("sources_missing_dedupe_key_idx" in str(row) for row in query_plan)


def test_bootstrap_backfills_historical_agent_and_run_attribution() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000281"
    memory_id = "00000000-0000-0000-0000-000000000282"
    ensure_sqlite_user(conn, user_id, "legacy-agent-attribution@example.com")
    conn.execute(
        """
        INSERT INTO memories (
          id, user_id, memory_key, value, status, source_event_ids,
          memory_type, canonical_text, metadata_json,
          created_by_agent_id, run_id
        )
        VALUES (?, ?, 'legacy.agent', '{}', 'candidate', '[]',
                'semantic', 'Legacy agent output', ?, NULL, NULL)
        """,
        (
            memory_id,
            user_id,
            json.dumps(
                {
                    "agentic_memory": {
                        "agent_identity": {
                            "agent_id": "hermes",
                            "agent_run_id": "run-historical",
                        }
                    }
                }
            ),
        ),
    )
    conn.commit()

    sqlite_schema.bootstrap_sqlite_schema(conn)

    assert conn.execute(
        "SELECT created_by_agent_id, run_id FROM memories WHERE id = ?",
        (memory_id,),
    ).fetchone() == ("hermes", "run-historical")


def test_content_update_transactionally_expires_only_derived_entity_edges() -> None:
    conn = sqlite3.connect(":memory:")
    sqlite_schema.bootstrap_sqlite_schema(conn)
    user_id = "00000000-0000-0000-0000-000000000221"
    ensure_sqlite_user(conn, user_id, "derived-edge@example.com")
    store = SQLiteVNextStore(conn, user_id)
    memory = store.create_memory(
        {
            "memory_key": "edge.seed",
            "value": {"text": "Jane Doe said hello."},
            "status": "active",
            "memory_type": "semantic",
            "canonical_text": "Jane Doe said hello.",
        }
    )
    mention = store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(memory["id"]),
            "to_type": "entity",
            "to_id": "00000000-0000-0000-0000-000000000222",
            "edge_type": "mentions",
        }
    )
    manual = store.create_graph_edge(
        {
            "from_type": "memory",
            "from_id": str(memory["id"]),
            "to_type": "memory",
            "to_id": "00000000-0000-0000-0000-000000000223",
            "edge_type": "supports",
        }
    )

    store.update_memory(
        memory_id=str(memory["id"]),
        patch={"canonical_text": "Zara Quill said hello."},
    )

    assert [str(edge["id"]) for edge in store.list_edges(from_id=str(memory["id"]))] == [str(manual["id"])]
    assert conn.execute(
        "SELECT valid_to IS NOT NULL FROM graph_edges WHERE id = ?", (str(mention["id"]),)
    ).fetchone() == (1,)
