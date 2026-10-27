from __future__ import annotations

import importlib


MODULE_NAME = "apps.api.alembic.versions.20260704_0072_semantic_retrieval_substrate"


def load_migration_module():
    return importlib.import_module(MODULE_NAME)


def test_revision_chain_extends_procedure_memory_type() -> None:
    module = load_migration_module()

    assert module.revision == "20260704_0072"
    assert module.down_revision == "20260621_0071"


def test_upgrade_adds_vector_fts_and_targeted_lookup_substrate(monkeypatch) -> None:
    module = load_migration_module()
    executed: list[str] = []

    monkeypatch.setattr(module.op, "execute", executed.append)

    module.upgrade()

    # The upgrade brackets the statements with the owner bypass for the
    # documented strict table owner; the bracket is restored before the
    # transaction commits.
    assert executed[0] == "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY"
    assert executed[-1] == "ALTER TABLE memories FORCE ROW LEVEL SECURITY"
    assert executed[1:-1] == list(module._UPGRADE_STATEMENTS)
    joined = "\n".join(executed)
    assert "ADD COLUMN embedding_vector vector(1536) NULL" in joined
    assert "USING hnsw (embedding_vector vector_cosine_ops)" in joined
    assert "ADD COLUMN search_tsv tsvector" in joined
    assert "GENERATED ALWAYS AS" in joined
    assert "STORED" in joined
    assert "coalesce(title, '')" in joined
    assert "coalesce(canonical_text, '')" in joined
    assert "coalesce(summary, '')" in joined
    assert "USING gin (search_tsv)" in joined
    assert "ADD COLUMN commit_digest text NULL" in joined
    assert "ADD COLUMN confirmation_id text NULL" in joined
    assert "WHERE commit_digest IS NOT NULL" in joined
    assert "WHERE confirmation_id IS NOT NULL" in joined
    assert "'agentic_memory_commit'" in joined


def test_upgrade_backfills_lookup_columns_from_agentic_metadata(monkeypatch) -> None:
    module = load_migration_module()
    executed: list[str] = []

    monkeypatch.setattr(module.op, "execute", executed.append)

    module.upgrade()

    joined = "\n".join(executed)
    assert "SET commit_digest = metadata_json #>> '{agentic_memory,idempotency_key}'" in joined
    assert (
        "SET confirmation_id = metadata_json #>> '{agentic_memory,confirmation,confirmation_id}'"
        in joined
    )


def test_downgrade_drops_everything_upgrade_added(monkeypatch) -> None:
    module = load_migration_module()
    executed: list[str] = []

    monkeypatch.setattr(module.op, "execute", executed.append)

    module.downgrade()

    assert executed == list(module._DOWNGRADE_STATEMENTS)
    joined = "\n".join(executed)
    assert "DROP COLUMN IF EXISTS embedding_vector" in joined
    assert "DROP COLUMN IF EXISTS search_tsv" in joined
    assert "DROP COLUMN IF EXISTS commit_digest" in joined
    assert "DROP COLUMN IF EXISTS confirmation_id" in joined
    assert "DROP INDEX IF EXISTS memories_embedding_vector_hnsw_idx" in joined
    assert "DROP INDEX IF EXISTS memories_search_tsv_gin_idx" in joined
    assert "DROP INDEX IF EXISTS memories_commit_digest_idx" in joined
    assert "DROP INDEX IF EXISTS memories_confirmation_id_idx" in joined
    assert "DROP INDEX IF EXISTS memories_agentic_commit_updated_idx" in joined
