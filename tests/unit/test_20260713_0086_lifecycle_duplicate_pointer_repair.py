from __future__ import annotations

import importlib


MODULE_NAME = "apps.api.alembic.versions.20260713_0086_lifecycle_duplicate_pointer_repair"


def load_migration_module():
    return importlib.import_module(MODULE_NAME)


def test_revision_follows_capture_dedupe_migration() -> None:
    module = load_migration_module()
    assert module.revision == "20260713_0086"
    assert module.down_revision == "20260713_0085"


def test_upgrade_repoints_two_hop_duplicate_groups_idempotently(monkeypatch) -> None:
    module = load_migration_module()
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", executed.append)

    module.upgrade()

    # Two repair statements inside the owner bypass bracket.
    assert len(executed) == 4
    assert executed[0] == "ALTER TABLE memories NO FORCE ROW LEVEL SECURITY"
    assert executed[-1] == "ALTER TABLE memories FORCE ROW LEVEL SECURITY"
    joined = "\n".join(executed)
    for column in ("commit_digest", "confirmation_id"):
        assert f"former_holder.{column} IS NULL" in joined
        assert f"canonical.{column} IS NOT NULL" in joined
    assert "former_holder.deleted_at IS NOT NULL" in joined
    assert "canonical.deleted_at IS NULL" in joined
    assert "former_holder.id::text = (sibling.metadata_json" in joined
    assert "canonical.id::text = (former_holder.metadata_json" in joined
    assert "to_jsonb(repair.canonical_id::text)" in joined
    assert "memory.id = repair.sibling_id" in joined


def test_downgrade_is_a_no_op(monkeypatch) -> None:
    module = load_migration_module()
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", executed.append)

    module.downgrade()

    assert executed == []
