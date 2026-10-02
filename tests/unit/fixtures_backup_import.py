"""Shared builders for the backup import tests.

``origin_export`` makes an export of a vault that holds one of every record
type, parsed into records a test can edit and sign again. ``write_legacy`` turns
those records into the headerless file an early release wrote.
"""

from __future__ import annotations

import json
from pathlib import Path

from alicebot_api import onramp as onramp_module
from alicebot_api.onramp import bootstrap_database
from tests.unit.test_import_key_claim import _Export
from tests.unit.test_sqlite_onramp import USER_ID as SEED_USER_ID
from tests.unit.test_sqlite_onramp import _seed_full_graph


def origin_export(tmp_path: Path) -> _Export:
    """An export holding one of every record type, with a real memory graph behind it."""
    database = tmp_path / "origin" / "memory.db"
    database.parent.mkdir(parents=True)
    bootstrap_database(database, user_id=SEED_USER_ID, user_email="local@alice")
    _seed_full_graph(database)
    out = tmp_path / "origin.jsonl"
    assert onramp_module.main(["export", "--db", str(database), "--user-id", str(SEED_USER_ID), "--out", str(out)]) == 0
    return _Export(out)


def write_legacy(export: _Export, path: Path) -> Path:
    """The records as a headerless file: no header, no footer, no declared schema.

    Early releases wrote this. Import accepts it, with no integrity check, and
    a record may leave out a column the schema had not got yet.
    """
    lines = [
        json.dumps({"record_type": item["record_type"], "record": item["record"]}, sort_keys=True, separators=(",", ":"))
        for item in export.body
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
