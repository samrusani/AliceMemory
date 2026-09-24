"""Sleep applies the commit door's credential check.

Each test names the edit that makes it fail. Credential values are built
at runtime. Sidecar bytes are read with Path.read_text.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import UUID

import pytest

from alicebot_api.credential_floor import credential_verdict
from alicebot_api.legacy_credential_check import commit_door_secret_verdict, commit_gate_refuses
from alicebot_api.onramp import bootstrap_database, resolve_db_path
from alicebot_api.vnext_capture import chunk_text
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_sleep import (
    SLEEP_EXCERPT_MAX,
    SleepError,
    load_sleep_proposals,
    run_local_vault_sleep,
    sleep_proposals_path,
)
from tests.unit.fixtures_v0160_export import V0160_PROSE_EXPORT_LINES

USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_USER_ID = "00000000-0000-0000-0000-000000000002"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _database(tmp_path: Path) -> Path:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def _create_source(store: SQLiteVNextStore, *, note: str, suffix: str, minute: int) -> dict[str, object]:
    source = store.create_source(
        {
            "source_type": "note",
            "title": f"Harbour note {suffix}",
            "content_hash": f"hash-sleep-cred-{suffix}",
            "captured_at": f"2026-08-01T08:{minute:02d}:00Z",
            "domain": "project",
            "sensitivity": "public",
            "metadata_json": {"project_scope": ["harbour"], "raw_text": note},
        }
    )
    store.create_source_chunk(
        {
            "source_id": source["id"],
            "chunk_index": 0,
            "text": note,
            "token_count": max(1, len(note.split())),
        }
    )
    return source


def _memory_count(database: Path) -> int:
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        return len(store.list_memories())


def _line_value(report: str, label: str) -> str:
    prefix = f"{label}: "
    for line in report.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing {label!r}")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _floor_token() -> str:
    return "ghp_" + "ab12cd34ef56"


def _legacy_assignment() -> str:
    name = "PASSWORD" + "_DB"
    value = "Ab" + "12" + "cd" + "EF"
    return f"{name}={value}"


def _aws_key() -> str:
    return "AK" + "IA" + "Ab12Cd34" + "Ef56Gh78"


def _ordinary_note() -> str:
    return "The harbour watch keeps channel 7 for the night shift."


def _short_excerpt(text: str) -> str:
    flattened = " ".join(text.split())
    if len(flattened) <= SLEEP_EXCERPT_MAX:
        return flattened
    return flattened[:SLEEP_EXCERPT_MAX].rstrip()


def test_the_commit_door_alias_is_the_legacy_function() -> None:
    """``legacy_commit_gate_refuses`` stays the import alias, not a second function."""

    from alicebot_api import vnext_memory_commit as door

    assert door.legacy_commit_gate_refuses is commit_gate_refuses
    assert door.commit_door_secret_verdict is commit_door_secret_verdict


def test_sleep_withholds_floor_and_legacy_hits_and_keeps_the_ordinary_note(tmp_path: Path) -> None:
    """Three sources: floor, legacy-only, and an ordinary note.

    Neither secret is in the sidecar. The ordinary note is the one proposed
    row. No memory row is created. Mutation: check only the floor. The
    legacy assignment is proposed and this test fails.
    """

    floor_note = f"Rotate the deploy token {_floor_token()} before Friday."
    legacy_note = f"we decided the billing store uses {_legacy_assignment()} for invoices."
    ordinary = _ordinary_note()
    assert credential_verdict(floor_note) is not None
    assert credential_verdict(legacy_note) is None
    assert commit_gate_refuses("", legacy_note) is True
    assert commit_door_secret_verdict("", ordinary) is None

    database = _database(tmp_path)
    before_memories = _memory_count(database)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        _create_source(store, note=floor_note, suffix="floor", minute=1)
        _create_source(store, note=legacy_note, suffix="legacy", minute=2)
        _create_source(store, note=ordinary, suffix="plain", minute=3)

    report = run_local_vault_sleep(database, user_id=USER_ID)
    sidecar = sleep_proposals_path(database)
    text = sidecar.read_text(encoding="utf-8")
    rows = load_sleep_proposals(sidecar)

    assert _floor_token() not in text
    assert _legacy_assignment() not in text
    assert _floor_token() not in report
    assert _legacy_assignment() not in report
    assert len(rows) == 1
    assert rows[0]["excerpt"] == ordinary
    assert rows[0]["status"] == "proposed"
    assert int(_line_value(report, "proposals written")) == 1
    assert int(_line_value(report, "sources withheld")) == 2
    assert int(_line_value(report, "existing rows removed")) == 0
    assert _memory_count(database) == before_memories
    assert _mode(sidecar) == 0o600


def test_an_aws_key_cut_inside_the_excerpt_is_withheld(tmp_path: Path) -> None:
    """AKIA plus 8 characters in the excerpt, the rest only in the chunk.

    The excerpt alone passes. The full chunk is refused, so the row is
    dropped. Mutation: check the excerpt and not the chunk. This test fails.
    """

    key = _aws_key()
    visible = key[:12]
    prefix = ("n" * 147) + " "
    note = prefix + key
    excerpt = _short_excerpt(note)
    assert excerpt.endswith(visible)
    assert key not in excerpt
    assert len(excerpt) == SLEEP_EXCERPT_MAX
    assert commit_door_secret_verdict("", excerpt) is None
    assert commit_door_secret_verdict("", note) is not None

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        _create_source(store, note=note, suffix="aws", minute=1)

    report = run_local_vault_sleep(database, user_id=USER_ID)
    sidecar = sleep_proposals_path(database)
    assert not sidecar.exists()
    assert key not in report
    assert visible not in report
    assert int(_line_value(report, "sources withheld")) == 1
    assert int(_line_value(report, "proposals written")) == 0


def test_existing_rows_for_both_users_lose_credential_excerpts(tmp_path: Path) -> None:
    """A seeded sidecar with no new sources drops both refused rows.

    The receipt counts only the caller's removal. Mutation: scrub only the
    caller's rows. The other user's value stays and this test fails.
    """

    caller_secret = _legacy_assignment()
    other_secret = _floor_token()
    caller_source = "11111111-1111-4111-8111-111111111111"
    other_source = "22222222-2222-4222-8222-222222222222"
    database = _database(tmp_path)
    sidecar = sleep_proposals_path(database)
    rows = [
        {
            "excerpt": f"we decided {caller_secret} stays in the note",
            "source_id": caller_source,
            "status": "proposed",
            "user_id": USER_ID,
        },
        {
            "excerpt": f"rotate {other_secret} on Friday",
            "source_id": other_source,
            "status": "proposed",
            "user_id": OTHER_USER_ID,
        },
    ]
    sidecar.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    assert commit_door_secret_verdict("", rows[0]["excerpt"]) is not None
    assert commit_door_secret_verdict("", rows[1]["excerpt"]) is not None

    report = run_local_vault_sleep(database, user_id=USER_ID)
    text = sidecar.read_text(encoding="utf-8")
    assert caller_secret not in text
    assert other_secret not in text
    assert caller_source not in report
    assert other_source not in report
    assert int(_line_value(report, "existing rows removed")) == 1
    assert int(_line_value(report, "proposals written")) == 0
    assert load_sleep_proposals(sidecar) == []


def test_replace_bytes_and_modes_exclude_refused_material(tmp_path: Path, monkeypatch) -> None:
    """The bytes at replace never hold a refused value. Modes are 0600.

    Covers a writing run, a stale 0644 tmp, and a no-op run on a 0644
    sidecar. Mutation: write the tmp with the process default mode, or
    skip the no-op chmod. This test fails.
    """

    secret = _legacy_assignment()
    ordinary = _ordinary_note()
    database = _database(tmp_path)
    sidecar = sleep_proposals_path(database)
    stale = sidecar.with_name(f"{sidecar.name}.tmp")
    stale.write_text(secret + "\n", encoding="utf-8")
    os.chmod(stale, 0o644)
    assert _mode(stale) == 0o644

    captured: list[bytes] = []
    real_replace = Path.replace

    def _spy(self: Path, target: Path) -> Path:
        if self.name.endswith(".tmp"):
            captured.append(self.read_bytes())
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _spy)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        _create_source(store, note=f"we decided {secret} for billing", suffix="secret", minute=1)
        _create_source(store, note=ordinary, suffix="plain", minute=2)

    run_local_vault_sleep(database, user_id=USER_ID)
    written = sidecar.read_text(encoding="utf-8")
    assert captured, "replace was not called"
    assert all(secret.encode("utf-8") not in blob for blob in captured)
    assert secret not in written
    assert ordinary in written
    assert not stale.exists()
    assert _mode(sidecar) == 0o600

    os.chmod(sidecar, 0o644)
    before = sidecar.read_text(encoding="utf-8")
    second = run_local_vault_sleep(database, user_id=USER_ID)
    assert sidecar.read_text(encoding="utf-8") == before
    assert _mode(sidecar) == 0o600
    assert int(_line_value(second, "proposals written")) == 0
    assert secret not in second


def test_a_clean_noop_leaves_the_sidecar_bytes_unchanged(tmp_path: Path) -> None:
    """No new row and no dropped row: the file bytes stay put.

    Mutation: rewrite the sidecar on every run. This test fails.
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        _create_source(store, note=_ordinary_note(), suffix="plain", minute=1)
    run_local_vault_sleep(database, user_id=USER_ID)
    sidecar = sleep_proposals_path(database)
    before = sidecar.read_bytes()
    run_local_vault_sleep(database, user_id=USER_ID)
    assert sidecar.read_bytes() == before


def test_sidecar_loads_and_runs_under_v0160_sleep(tmp_path: Path) -> None:
    """A sidecar this writer produced loads under v0.16.0's sleep.

    Mutation: add a fifth key to each row. v0.16.0 still loads extra keys,
    so this test also runs sleep and requires the old receipt. A row with
    no source_id makes that sleep raise.
    """

    database = _database(tmp_path)
    with sqlite_user_connection(database, USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        _create_source(store, note=_ordinary_note(), suffix="plain", minute=1)
    run_local_vault_sleep(database, user_id=USER_ID)
    sidecar = sleep_proposals_path(database)
    assert sidecar.read_text(encoding="utf-8")

    source = subprocess.check_output(
        ["git", "show", "v0.16.0:apps/api/src/alicebot_api/vault_sleep.py"],
        cwd=REPO_ROOT,
        text=True,
    )
    module_path = tmp_path / "v0160_vault_sleep.py"
    module_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("v0160_vault_sleep_rollback", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    loaded = module.load_sleep_proposals(sidecar)
    assert len(loaded) == 1
    assert set(loaded[0]) == {"excerpt", "source_id", "status", "user_id"}
    report = module.run_local_vault_sleep(database, user_id=UUID(USER_ID))
    assert "proposals written: 0" in report
    assert module.load_sleep_proposals(sidecar)[0]["excerpt"] == _ordinary_note()


def test_v0160_export_notes_and_imported_markdown_are_not_withheld() -> None:
    """v0.16.0 export notes and the first chunk of each tracked markdown file pass.

    Import splits a file with ``chunk_text``. Sleep checks that first chunk,
    not a later section. A refusal here is a source the writer would drop.
    """

    notes: list[str] = []
    for line in V0160_PROSE_EXPORT_LINES:
        record = json.loads(line)
        if record.get("record_type") != "memory":
            continue
        text = record["record"].get("canonical_text")
        if isinstance(text, str):
            notes.append(text)
    assert len(notes) == 5
    refused_notes = [text for text in notes if commit_door_secret_verdict("", text) is not None]
    assert refused_notes == []

    listed = subprocess.check_output(["git", "ls-files", "-z", "*.md"], cwd=REPO_ROOT)
    paths = [REPO_ROOT / item.decode("utf-8") for item in listed.split(b"\0") if item]
    assert len(paths) >= 259
    refused_docs: list[str] = []
    for path in paths:
        chunks = chunk_text(path.read_text(encoding="utf-8"))
        if not chunks:
            continue
        chunk = chunks[0]
        flattened = " ".join(chunk.split())
        excerpt = flattened[:SLEEP_EXCERPT_MAX].rstrip() if len(flattened) > SLEEP_EXCERPT_MAX else flattened
        if commit_door_secret_verdict("", excerpt) is not None or commit_door_secret_verdict("", chunk) is not None:
            refused_docs.append(str(path.relative_to(REPO_ROOT)))
    assert refused_docs == []


def test_a_row_without_source_id_still_makes_sleep_raise(tmp_path: Path) -> None:
    """The credential sweep does not accept a row that omits source_id."""

    database = _database(tmp_path)
    sidecar = sleep_proposals_path(database)
    sidecar.write_text(
        json.dumps({"excerpt": "harbour note", "status": "proposed", "user_id": USER_ID}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SleepError, match="sidecar is invalid"):
        run_local_vault_sleep(database, user_id=USER_ID)
