from __future__ import annotations

import importlib


MODULE_NAME = "apps.api.alembic.versions.20260715_0091_source_identity_defensive_edges"


def test_upgrade_clears_only_live_whitespace_string_identities_and_adds_bounded_indexes(
    monkeypatch,
) -> None:
    module = importlib.import_module(MODULE_NAME)
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", executed.append)

    module.upgrade()

    assert module.revision == "20260715_0091"
    assert module.down_revision == "20260714_0090"
    # The clear runs inside the owner bypass bracket; the bracket is closed
    # before the additive event-linkage columns and their partial indexes.
    assert executed[0] == "ALTER TABLE sources NO FORCE ROW LEVEL SECURITY"
    assert "ALTER TABLE sources FORCE ROW LEVEL SECURITY" in executed
    assert executed.index("ALTER TABLE sources NO FORCE ROW LEVEL SECURITY") < executed.index(
        "ALTER TABLE sources FORCE ROW LEVEL SECURITY"
    )
    repair = next(s for s in executed if "UPDATE sources" in s)
    assert "UPDATE sources" in repair
    assert "SET dedupe_key = NULL" in repair
    assert "deleted_at IS NULL" in repair
    assert "jsonb_typeof(metadata_json -> 'raw_text') = 'string'" in repair
    assert "btrim(" in repair
    assert "[[:space:]]" not in repair
    assert all(f"chr({codepoint})" in repair for codepoint in module._PYTHON_312_STRIP_CODEPOINTS)

    columns = next(s for s in executed if "ADD COLUMN payload_artifact_id" in s)
    target, artifact, candidate_memory, memory = [
        s for s in executed if "CREATE INDEX IF NOT EXISTS event_log_project_update" in s
    ]
    event_types = (
        "project.update_candidate_created",
        "project.update_candidate_accepted",
        "project.update_candidate_rejected",
    )
    for column_name, payload_key in (
        ("payload_artifact_id", "artifact_id"),
        ("payload_candidate_memory_id", "candidate_memory_id"),
        ("payload_memory_id", "memory_id"),
    ):
        assert f"ADD COLUMN {column_name} text" in columns
        assert f"jsonb_typeof(payload_json -> '{payload_key}') = 'string'" in columns
        assert f"THEN payload_json ->> '{payload_key}'" in columns
        assert "GENERATED ALWAYS AS" in columns
        assert ") STORED" in columns
    assert "event_log_project_update_target_idx" in target
    assert "user_id" in target
    assert "target_type" in target
    assert "target_id" in target
    assert "event_type" in target
    assert "occurred_at DESC" in target
    assert "id DESC" in target
    assert "target_type IS NOT NULL" in target
    assert "target_id IS NOT NULL" in target
    for statement, index_name, column_name in (
        (artifact, "event_log_project_update_artifact_id_idx", "payload_artifact_id"),
        (
            candidate_memory,
            "event_log_project_update_candidate_memory_id_idx",
            "payload_candidate_memory_id",
        ),
        (memory, "event_log_project_update_memory_id_idx", "payload_memory_id"),
    ):
        assert index_name in statement
        assert "USING gin" not in statement
        assert "user_id" in statement
        assert "event_type" in statement
        assert column_name in statement
        assert "occurred_at DESC" in statement
        assert "id DESC" in statement
        assert f"{column_name} IS NOT NULL" in statement
    for statement in (target, artifact, candidate_memory, memory):
        assert "WHERE event_type IN" in statement
        assert all(f"'{event_type}'" in statement for event_type in event_types)


def test_whitespace_vocabulary_matches_0090_and_python_strip() -> None:
    module = importlib.import_module(MODULE_NAME)
    previous = importlib.import_module("apps.api.alembic.versions.20260714_0090_project_scope_identity")
    expected_codepoints = (
        *range(0x0009, 0x000E),
        *range(0x001C, 0x0021),
        0x0085,
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    )

    assert module._PYTHON_312_STRIP_CODEPOINTS == expected_codepoints
    assert module._PYTHON_312_STRIP_CODEPOINTS == previous._PYTHON_312_STRIP_CODEPOINTS
    assert all(chr(codepoint).strip() == "" for codepoint in expected_codepoints)


def test_downgrade_keeps_repair_and_drops_only_additive_indexes(monkeypatch) -> None:
    module = importlib.import_module(MODULE_NAME)
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", executed.append)

    module.downgrade()

    assert executed == [
        "DROP INDEX IF EXISTS event_log_project_update_memory_id_idx",
        "DROP INDEX IF EXISTS event_log_project_update_candidate_memory_id_idx",
        "DROP INDEX IF EXISTS event_log_project_update_artifact_id_idx",
        "DROP INDEX IF EXISTS event_log_project_update_target_idx",
        "ALTER TABLE event_log DROP COLUMN IF EXISTS payload_memory_id",
        "ALTER TABLE event_log DROP COLUMN IF EXISTS payload_candidate_memory_id",
        "ALTER TABLE event_log DROP COLUMN IF EXISTS payload_artifact_id",
    ]
    assert all("UPDATE sources" not in statement for statement in executed)
