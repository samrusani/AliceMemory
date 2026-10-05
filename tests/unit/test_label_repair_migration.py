"""Migration 0096 relaxes row security on all seven label tables."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alicebot_api.cli.parser import build_parser as build_alicebot_parser
from alicebot_api.onramp import _KNOWN_COMMANDS
from alicebot_api.onramp import build_parser as build_sqlite_parser

ROOT = Path(__file__).resolve().parents[2]


def _migration():
    path = ROOT / "apps/api/alembic/versions/20261005_0096_derived_label_floor.py"
    spec = importlib.util.spec_from_file_location("migration_0096", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0096_brackets_every_label_table() -> None:
    migration = _migration()
    tables = tuple(statement.split()[2] for statement in migration._RELAX_RLS)
    assert migration.revision == "20261005_0096"
    assert migration.down_revision == "20261004_0095"
    assert tables == (
        "sources",
        "memories",
        "open_loops",
        "generated_artifacts",
        "beliefs",
        "event_log",
        "projects",
    )


def test_labels_commands_are_registered() -> None:
    assert "labels" in _KNOWN_COMMANDS
    sqlite = build_sqlite_parser().parse_args(["labels", "check", "--db", "vault.db"])
    assert sqlite.command == "labels"
    assert sqlite.labels_command == "check"
    postgres = build_alicebot_parser().parse_args(["vnext", "labels", "repair"])
    assert postgres.vnext_command == "labels"
    assert postgres.labels_command == "repair"
