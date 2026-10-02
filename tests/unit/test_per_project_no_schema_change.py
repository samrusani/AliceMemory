"""Per-project memory S2: no table, column, index or record type, and the reads write nothing (spec 2.8, 12, test 44's S2 half).

v0.21 adds no table, no column and no export record type, so a downgrade to v0.20.0 keeps working and a backup from one
release restores in the other. The single-scan fill of the brief meets its latency budget with no index (spec 12): a
generated column with an index is a schema change, and the downgrade contract wins over a latency figure. The digest
below was computed from the v0.20.0 tag's schema code and from this branch's, and both gave this value.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from alicebot_api import session_start_hook
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import main as onramp_main
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from tests.unit.per_project_s2_support import build_parity_vault, db_path_for, repo_with_remote

USER_ID = "00000000-0000-0000-0000-000000000001"

#: Computed from ``alicebot_api.sqlite_schema.bootstrap_sqlite_schema`` of the v0.20.0 tag on 2026-10-02: 27
#: tables, 34 indexes, every column of every table read with ``PRAGMA table_xinfo`` (so a generated or hidden column
#: counts). The SQL text of the shadow tables SQLite makes for the full-text indexes is left out because SQLite writes
#: it, and its words differ between versions.
EXPECTED_SCHEMA_DIGEST = "3cc59fb61a545395d2cff42cac4634fe3d9ff7fa23ad7272f6c78eb8e834489f"
EXPECTED_TABLES = 27
EXPECTED_INDEXES = 34
_SHADOW_TABLE = re.compile(r"_(data|idx|content|docsize|config)$")


def schema_digest(conn: sqlite3.Connection) -> tuple[int, int, str]:
    objects: list[list[object]] = []
    for kind, name, table, sql in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ):
        objects.append([kind, name, table, None if (kind == "table" and _SHADOW_TABLE.search(name)) else sql])
    tables = [str(entry[1]) for entry in objects if entry[0] == "table"]
    columns = {table: [list(row) for row in conn.execute(f"PRAGMA table_xinfo('{table}')")] for table in tables}
    text = json.dumps({"objects": objects, "columns": columns}, sort_keys=True, separators=(",", ":"))
    indexes = len([entry for entry in objects if entry[0] == "index"])
    return len(tables), indexes, hashlib.sha256(text.encode()).hexdigest()


def test_a_fresh_vault_has_the_v0200_schema() -> None:
    """Mutation: add a table, a column (a generated one included), an index or a trigger to the bootstrap.

    The tables, the indexes and every column read with ``table_xinfo`` equal the digest the v0.20.0 tag gave.
    A generated column that an index could use would show here, and ``table_info`` would not show it.
    """

    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    conn.commit()
    assert schema_digest(conn) == (EXPECTED_TABLES, EXPECTED_INDEXES, EXPECTED_SCHEMA_DIGEST)


def test_adding_a_generated_column_changes_the_digest() -> None:
    """Mutation: compute the digest with ``PRAGMA table_info``, which leaves a generated column out.

    The check has to be able to see the column an index would need, or it would pass the change it exists to stop.
    """

    conn = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(conn)
    conn.commit()
    before = schema_digest(conn)
    conn.execute(
        "ALTER TABLE memories ADD COLUMN project_key TEXT GENERATED ALWAYS AS (json_extract(metadata_json, '$.project_id')) VIRTUAL"
    )
    assert schema_digest(conn)[2] != before[2]
    plain = [row[1] for row in conn.execute("PRAGMA table_info('memories')")]
    hidden = [row[1] for row in conn.execute("PRAGMA table_xinfo('memories')")]
    assert "project_key" not in plain and "project_key" in hidden


def _vault_state(database: Path) -> tuple[tuple[int, int, str], dict[str, str]]:
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        rows = {}
        for table in sorted(tables):
            if table.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
                continue
            digest = hashlib.sha256()
            for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
                digest.update(repr(row).encode())
            rows[table] = digest.hexdigest()
        return schema_digest(conn), rows
    finally:
        conn.close()


def test_the_reads_of_this_slice_write_nothing_and_change_no_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation: write a row, a setting or an index from the brief, the hook, ``sleep-proposals`` or ``alice_resume``.

    With scoping on and the working folder in a repository, every read of this slice leaves every table byte for byte as
    it found it and the schema unchanged. The fill reads, the project view, the status lines and the exclusion store
    nothing: ids are derived each time and kept nowhere.
    """

    for name in ("ALICE_PROJECT_SCOPING", "ALICE_PROJECT_DIR", "ALICE_MEMORY_DATA_DIR", "ALICE_AGENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = build_parity_vault(data_dir)
    repo = repo_with_remote(tmp_path / "repo")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ALICE_PROJECT_SCOPING", "on")
    database = db_path_for(data_dir)
    # One warm-up of each read, so any first-run bootstrap write is not blamed on the read itself.
    onramp_main(["brief", "--data-dir", str(data_dir)])
    onramp_main(["sleep-proposals", "--data-dir", str(data_dir)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(repo)})))
    session_start_hook.main(["--format", "markdown", "--data-dir", str(data_dir)])
    call_mcp_tool(context, name="alice_resume", arguments={})
    capsys.readouterr()
    before = _vault_state(database)
    assert before[0] == (EXPECTED_TABLES, EXPECTED_INDEXES, EXPECTED_SCHEMA_DIGEST)
    for scope in ("project", "project_only", "global", "all"):
        assert onramp_main(["brief", "--data-dir", str(data_dir), "--scope", scope]) == 0
        assert onramp_main(["sleep-proposals", "--data-dir", str(data_dir), "--scope", scope]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(repo)})))
    assert session_start_hook.main(["--format", "json", "--data-dir", str(data_dir)]) == 0
    resume_context = MCPRuntimeContext(
        database_url=sqlite_url_for_path(database), user_id=USER_ID, project_dir=str(repo)
    )
    call_mcp_tool(resume_context, name="alice_resume", arguments={})
    capsys.readouterr()
    assert _vault_state(database) == before
