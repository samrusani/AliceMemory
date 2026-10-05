#!/usr/bin/env python3
"""Execute the Phase 5 backup, restore, upgrade, and monitoring drills.

The report is deliberately content-free: it records checks, counts, hashes,
and revisions, but never database URLs, credentials, filesystem paths, memory
text, or subprocess output.  Raw drill databases and dumps live only in a
private temporary directory and are removed when the command exits.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from uuid import UUID, uuid4


def _as_int(value: object) -> int:
    """Narrow a database cell to int.

    A psycopg row cell is typed `object`, so `int(cell)` does not type-check.
    Narrowed explicitly rather than cast, so a column that is not integer-like
    fails loudly here instead of somewhere further downstream. Duplicated from
    the seed script on purpose: that script runs as a subprocess under a
    different PYTHONPATH so the two cannot share an import.
    """

    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        return int(value)
    raise TypeError(f"expected an integer-like database value, got {type(value).__name__}")


ROOT_DIR = Path(__file__).resolve().parents[1]
CURRENT_SOURCE_DIR = ROOT_DIR / "apps" / "api" / "src"
SEED_SCRIPT = ROOT_DIR / "scripts" / "_phase5_ops_seed.py"
ALEMBIC_INI = ROOT_DIR / "apps" / "api" / "alembic.ini"
BASELINE_TAG = "v0.12.0"
BASELINE_COMMIT = "692c28ae60072b1eac4a437676b3ecf68e8bc026"
BASELINE_POSTGRES_HEAD = "20260716_0092"
MIGRATION_0093 = "20260721_0093"
MIGRATION_0094 = "20260721_0094"
POSTGRES_MAJOR = 16
USER_ID = UUID("00000000-0000-0000-0000-000000005001")
MEMORY_ID = "00000000-0000-0000-0000-000000005101"
ARTIFACT_ID = "00000000-0000-0000-0000-000000005201"
NEWER_RATING_ID = "00000000-0000-0000-0000-000000005302"
REVIEWER_ID = "phase5-ops-reviewer"
SEED_QUERY = "cobalt recovery beacon"
REPORT_VERSION = "phase5_ops_evidence.v1"
_SAFE_CODE = re.compile(r"^[a-z0-9_.:-]+$")
_CREDENTIAL_URL = re.compile(r"(?:postgres(?:ql)?|redis)://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_PUBLIC_SCHEMA_ACL_TOC = re.compile(r"^\s*[0-9]+;\s+[0-9]+\s+[0-9]+\s+ACL\s+-\s+SCHEMA\s+public(?:\s|$)")
_DIAGNOSTIC_LIMIT = 16 * 1024


class EvidenceError(RuntimeError):
    """A stable failure whose code is safe to include in the public report."""

    def __init__(self, code: str, *additional_codes: str):
        codes = tuple(dict.fromkeys((code, *additional_codes)))
        if any(_SAFE_CODE.fullmatch(item) is None for item in codes):
            raise ValueError("evidence failure codes must be stable identifiers")
        super().__init__(",".join(codes))
        self.code = codes[0]
        self.codes = codes


def _emit_subprocess_stderr(
    code: str,
    stderr: str | bytes | None,
    *,
    env: Mapping[str, str] | None,
) -> None:
    if not stderr:
        return
    if isinstance(stderr, bytes):
        diagnostic = stderr.decode("utf-8", errors="replace")
    else:
        diagnostic = stderr
    sensitive_values = {
        value
        for key, value in (env or {}).items()
        if value
        and (
            key
            in {
                "PGPASSWORD",
                "DATABASE_ADMIN_URL",
                "DATABASE_BACKUP_URL",
                "DATABASE_LIFECYCLE_URL",
                "DATABASE_URL",
            }
            or key.lower().endswith(("password", "_secret"))
        )
    }
    sensitive_values.update(
        {
            SEED_QUERY,
            "phase5-ops@example.invalid",
        }
    )
    for sensitive in sorted(sensitive_values, key=len, reverse=True):
        diagnostic = diagnostic.replace(sensitive, "<redacted>")
    diagnostic = _CREDENTIAL_URL.sub("<redacted-database-url>", diagnostic)
    diagnostic = diagnostic.strip()
    if not diagnostic:
        return
    if len(diagnostic) > _DIAGNOSTIC_LIMIT:
        half = (_DIAGNOSTIC_LIMIT - len("\n...[stderr truncated]...\n")) // 2
        diagnostic = diagnostic[:half] + "\n...[stderr truncated]...\n" + diagnostic[-half:]
    print(f"phase5_ops_evidence[{code}] subprocess stderr:", file=sys.stderr)
    print(diagnostic, file=sys.stderr)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    code: str,
    env: Mapping[str, str] | None = None,
    cwd: Path = ROOT_DIR,
    stdout_file: Path | None = None,
    stdin_file: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    input_stream = None
    try:
        input_stream = stdin_file.open("rb") if stdin_file is not None else None
        if stdout_file is not None:
            with stdout_file.open("wb") as output:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=dict(env) if env is not None else None,
                    stdin=input_stream if input_stream is not None else subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=timeout,
                )
            if completed.returncode != 0:
                _emit_subprocess_stderr(code, completed.stderr, env=env)
                raise EvidenceError(code)
            return subprocess.CompletedProcess(command, completed.returncode, "", "")
        completed_text = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=input_stream if input_stream is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _emit_subprocess_stderr(code, exc.stderr, env=env)
        raise EvidenceError(code) from exc
    except OSError as exc:
        raise EvidenceError(code) from exc
    finally:
        if input_stream is not None:
            input_stream.close()
    if completed_text.returncode != 0:
        _emit_subprocess_stderr(code, completed_text.stderr, env=env)
        raise EvidenceError(code)
    return completed_text


def _require_tool(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise EvidenceError(f"missing_prerequisite:{name.replace('-', '_')}")
    return resolved


def _git_bytes(repo_root: Path, arguments: list[str], *, code: str) -> bytes:
    try:
        completed = subprocess.run(
            [_require_tool("git"), *arguments],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError(code) from exc
    if completed.returncode != 0:
        raise EvidenceError(code)
    return completed.stdout


def _snapshot_paths(repo_root: Path) -> tuple[list[bytes], bytes]:
    index = _git_bytes(
        repo_root,
        ["ls-files", "--stage", "-z"],
        code="carrier_index_unavailable",
    )
    if any(entry.startswith(b"160000 ") for entry in index.split(b"\0") if entry):
        raise EvidenceError("carrier_snapshot_gitlink_unsupported")
    current = _git_bytes(
        repo_root,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"],
        code="carrier_paths_unavailable",
    )
    head = _git_bytes(
        repo_root,
        ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
        code="carrier_head_paths_unavailable",
    )
    paths = sorted({path for path in (*current.split(b"\0"), *head.split(b"\0")) if path})
    return paths, index


def _snapshot_entry(root_fd: int, relative_path: bytes) -> tuple[bytes, int, bytes]:
    parts = relative_path.split(b"/")
    if not parts or any(part in {b"", b".", b".."} for part in parts):
        raise EvidenceError("carrier_path_invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise EvidenceError("carrier_snapshot_nofollow_unavailable")
    parent_fd = os.dup(root_fd)
    try:
        try:
            for component in parts[:-1]:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = child_fd
            file_name = parts[-1]
            info = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return b"missing", 0, b""
            raise EvidenceError("carrier_entry_stat_failed") from exc

        mode = info.st_mode & 0o177777
        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(file_name, dir_fd=parent_fd)
            except OSError as exc:
                raise EvidenceError("carrier_symlink_read_failed") from exc
            target_bytes = target if isinstance(target, bytes) else os.fsencode(target)
            return b"symlink", mode, target_bytes
        if stat.S_ISREG(info.st_mode):
            try:
                file_fd = os.open(file_name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
            except OSError as exc:
                raise EvidenceError("carrier_file_open_failed") from exc
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise EvidenceError("carrier_entry_changed_during_snapshot")
                content = hashlib.sha256()
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    content.update(chunk)
                after = os.fstat(file_fd)
            finally:
                os.close(file_fd)
            stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
                raise EvidenceError("carrier_entry_changed_during_snapshot")
            return b"regular", mode, content.digest()
        if stat.S_ISDIR(info.st_mode):
            return b"directory", mode, b""
        if stat.S_ISFIFO(info.st_mode):
            return b"fifo", mode, b""
        if stat.S_ISSOCK(info.st_mode):
            return b"socket", mode, b""
        if stat.S_ISCHR(info.st_mode):
            return b"character_device", mode, b""
        if stat.S_ISBLK(info.st_mode):
            return b"block_device", mode, b""
        return b"unknown", mode, b""
    finally:
        os.close(parent_fd)


def _digest_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _carrier_snapshot_once(repo_root: Path) -> str:
    paths, index = _snapshot_paths(repo_root)
    digest = hashlib.sha256(b"alice-carrier-snapshot-v1\0")
    _digest_field(digest, index)
    root_fd = os.open(repo_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for relative_path in paths:
            entry_type, mode, payload = _snapshot_entry(root_fd, relative_path)
            _digest_field(digest, relative_path)
            _digest_field(digest, entry_type)
            _digest_field(digest, f"{mode:o}".encode("ascii"))
            _digest_field(digest, payload)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def repository_carrier_identity(repo_root: Path = ROOT_DIR) -> dict[str, str]:
    repo_root = repo_root.resolve(strict=True)
    source_head_commit = _run(
        [_require_tool("git"), "rev-parse", "HEAD"],
        code="source_head_commit_unavailable",
        cwd=repo_root,
    ).stdout.strip()
    source_head_tree = _run(
        [_require_tool("git"), "rev-parse", "HEAD^{tree}"],
        code="source_head_tree_unavailable",
        cwd=repo_root,
    ).stdout.strip()
    before_status = _git_bytes(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"],
        code="carrier_status_unavailable",
    )
    first_snapshot = _carrier_snapshot_once(repo_root)
    second_snapshot = _carrier_snapshot_once(repo_root)
    after_status = _git_bytes(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"],
        code="carrier_status_unavailable",
    )
    after_head = _run(
        [_require_tool("git"), "rev-parse", "HEAD"],
        code="source_head_commit_unavailable",
        cwd=repo_root,
    ).stdout.strip()
    after_tree = _run(
        [_require_tool("git"), "rev-parse", "HEAD^{tree}"],
        code="source_head_tree_unavailable",
        cwd=repo_root,
    ).stdout.strip()
    if (
        first_snapshot != second_snapshot
        or before_status != after_status
        or source_head_commit != after_head
        or source_head_tree != after_tree
    ):
        raise EvidenceError("carrier_changed_during_snapshot")
    return {
        "source_head_commit": source_head_commit,
        "source_head_tree": source_head_tree,
        "carrier_state": "dirty" if before_status else "clean",
        "carrier_snapshot_sha256": first_snapshot,
    }


def _source_env(source_dir: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_dir)
    env.pop("ALICE_EMBEDDINGS_API_KEY", None)
    if extra:
        env.update(extra)
    return env


def _seed_sqlite(db_path: Path, *, source_dir: Path, label: str) -> None:
    env = _source_env(source_dir)
    env.pop("DATABASE_URL", None)
    env.pop("DATABASE_ADMIN_URL", None)
    _run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "--backend",
            "sqlite",
            "--db",
            str(db_path),
            "--label",
            label,
        ],
        code="sqlite_seed_failed",
        env=env,
    )


def _parse_portable_footer(path: Path) -> dict[str, object]:
    footer: dict[str, object] | None = None
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvidenceError("portable_json_invalid") from exc
            if isinstance(record, dict) and record.get("record_type") == "export_footer":
                data = record.get("record")
                if isinstance(data, dict):
                    footer = data
    if footer is None:
        raise EvidenceError("portable_footer_missing")
    digest = footer.get("sha256")
    counts = footer.get("record_counts")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise EvidenceError("portable_footer_digest_invalid")
    if not isinstance(counts, dict):
        raise EvidenceError("portable_footer_counts_invalid")
    return footer


def _sqlite_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in ("users", "memories", "event_log")
    }


def _verify_sqlite_store(
    db_path: Path,
    *,
    expect_embedding: bool,
    expect_stamp: bool = True,
) -> dict[str, object]:
    from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
    from alicebot_api.vnext_embeddings import memory_embedding_signature_is_current

    raw = sqlite3.connect(db_path)
    try:
        integrity = [str(row[0]) for row in raw.execute("PRAGMA integrity_check").fetchall()]
        foreign_keys = raw.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != ["ok"] or foreign_keys:
            raise EvidenceError("sqlite_integrity_failed")
        embedding_present = bool(
            raw.execute(
                "SELECT embedding IS NOT NULL FROM memories WHERE id = ?",
                (MEMORY_ID,),
            ).fetchone()[0]
        )
        if embedding_present is not expect_embedding:
            raise EvidenceError("sqlite_embedding_presence_mismatch")
        if expect_stamp:
            stamp_rows = raw.execute("SELECT id, token FROM embedding_stamp ORDER BY id").fetchall()
            if len(stamp_rows) != 1 or stamp_rows[0][0] != 1 or not str(stamp_rows[0][1]).strip():
                raise EvidenceError("sqlite_embedding_stamp_invalid")
        counts = _sqlite_counts(raw)
    finally:
        raw.close()

    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        memory = store.get_memory(MEMORY_ID)
        recalled = store.search_memories_fts(
            query=SEED_QUERY,
            sensitivity_allowed=["internal"],
            limit=5,
        )
    if memory is None or not memory_embedding_signature_is_current(memory):
        raise EvidenceError("sqlite_embedding_signature_invalid")
    if MEMORY_ID not in {str(item.get("id")) for item in recalled}:
        raise EvidenceError("sqlite_recall_failed")
    return {
        "counts": counts,
        "embedding_present": embedding_present,
        "embedding_signature_current": True,
        "integrity": "ok",
        "recall": "matched",
    }


def _checkpoint_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) != 0:
            raise EvidenceError("sqlite_checkpoint_busy")
        if [str(item[0]) for item in conn.execute("PRAGMA integrity_check").fetchall()] != ["ok"]:
            raise EvidenceError("sqlite_integrity_failed")
    finally:
        conn.close()


def _destroy_sqlite_family(db_path: Path) -> None:
    for candidate in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
    ):
        candidate.unlink(missing_ok=True)


def _sqlite_physical_drill(work_dir: Path) -> tuple[dict[str, object], Path]:
    source = work_dir / "sqlite-current.db"
    backup = work_dir / "sqlite-physical.backup"
    _seed_sqlite(source, source_dir=CURRENT_SOURCE_DIR, label="current")
    before = _verify_sqlite_store(source, expect_embedding=True)
    _checkpoint_sqlite(source)
    shutil.copy2(source, backup)
    os.chmod(backup, 0o600)
    if stat.S_IMODE(backup.stat().st_mode) != 0o600:
        raise EvidenceError("sqlite_backup_permissions_invalid")
    backup_sha256 = _sha256_file(backup)
    _destroy_sqlite_family(source)
    if source.exists():
        raise EvidenceError("sqlite_destroy_failed")
    shutil.copy2(backup, source)
    if stat.S_IMODE(source.stat().st_mode) != 0o600:
        raise EvidenceError("sqlite_restore_permissions_invalid")
    after = _verify_sqlite_store(source, expect_embedding=True)
    if before["counts"] != after["counts"]:
        raise EvidenceError("sqlite_physical_count_mismatch")
    return (
        {
            "status": "passed",
            "backup_sha256": backup_sha256,
            "counts": after["counts"],
            "checkpoint": "truncate_complete",
            "destroy_restore": "proved",
            "integrity": after["integrity"],
            "recall": after["recall"],
            "embedding_signature": "current",
        },
        source,
    )


def _sqlite_portable_drill(work_dir: Path, source: Path) -> dict[str, object]:
    portable = work_dir / "portable.jsonl"
    imported = work_dir / "sqlite-portable-import.db"
    reexport = work_dir / "portable-reexport.jsonl"
    env = _source_env(CURRENT_SOURCE_DIR)
    env.pop("DATABASE_URL", None)
    env.pop("DATABASE_ADMIN_URL", None)
    base = [sys.executable, "-m", "alicebot_api.onramp"]
    _run(
        [*base, "export", "--db", str(source), "--user-id", str(USER_ID), "--out", str(portable)],
        code="portable_export_failed",
        env=env,
    )
    first = _parse_portable_footer(portable)
    _run(
        [*base, "import", "--db", str(imported), "--user-id", str(USER_ID), "--in", str(portable)],
        code="portable_import_failed",
        env=env,
    )
    restored = _verify_sqlite_store(imported, expect_embedding=False)
    _run(
        [*base, "export", "--db", str(imported), "--user-id", str(USER_ID), "--out", str(reexport)],
        code="portable_reexport_failed",
        env=env,
    )
    second = _parse_portable_footer(reexport)
    if first.get("sha256") != second.get("sha256"):
        raise EvidenceError("portable_digest_mismatch")
    if first.get("record_counts") != second.get("record_counts"):
        raise EvidenceError("portable_count_mismatch")
    return {
        "status": "passed",
        "content_sha256": first["sha256"],
        "record_counts": first["record_counts"],
        "fidelity": "canonical_digest_and_counts_match",
        "fts_recall": restored["recall"],
        "embeddings": "omitted_by_contract",
        "reindex_command": "alice-memory reindex-embeddings",
    }


def _extract_baseline(work_dir: Path) -> Path:
    git = _require_tool("git")
    resolved = _run(
        [git, "rev-parse", f"{BASELINE_TAG}^{{commit}}"],
        code="baseline_tag_unavailable",
    ).stdout.strip()
    if resolved != BASELINE_COMMIT:
        raise EvidenceError("baseline_tag_revision_mismatch")
    archive = work_dir / "v0.12.0.tar"
    _run(
        [git, "archive", "--format=tar", BASELINE_TAG],
        code="baseline_archive_failed",
        stdout_file=archive,
    )
    extracted = work_dir / "v0.12.0"
    extracted.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, mode="r:") as bundle:
            bundle.extractall(extracted, filter="data")
    except (OSError, tarfile.TarError) as exc:
        raise EvidenceError("baseline_archive_extract_failed") from exc
    if not (extracted / "apps" / "api" / "src" / "alicebot_api" / "onramp.py").is_file():
        raise EvidenceError("baseline_archive_incomplete")
    return extracted


def _sqlite_upgrade_drill(work_dir: Path, baseline: Path) -> dict[str, object]:
    from alicebot_api.onramp import bootstrap_database

    db_path = work_dir / "sqlite-v0.12-upgrade.db"
    old_source = baseline / "apps" / "api" / "src"
    _seed_sqlite(db_path, source_dir=old_source, label="v0.12.0")
    raw = sqlite3.connect(db_path)
    try:
        old_tables = {
            str(row[0]) for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "embedding_stamp" in old_tables:
            raise EvidenceError("baseline_sqlite_stamp_unexpected")
        if raw.execute("SELECT count(*) FROM memories WHERE id = ?", (MEMORY_ID,)).fetchone()[0] != 1:
            raise EvidenceError("baseline_sqlite_seed_missing")
    finally:
        raw.close()

    bootstrap_database(db_path, user_id=USER_ID, user_email="phase5-ops@example.invalid")
    first = _verify_sqlite_store(db_path, expect_embedding=True)
    conn = sqlite3.connect(db_path)
    try:
        first_token = str(conn.execute("SELECT token FROM embedding_stamp WHERE id = 1").fetchone()[0])
    finally:
        conn.close()
    bootstrap_database(db_path, user_id=USER_ID, user_email="phase5-ops@example.invalid")
    conn = sqlite3.connect(db_path)
    try:
        second_token = str(conn.execute("SELECT token FROM embedding_stamp WHERE id = 1").fetchone()[0])
    finally:
        conn.close()
    if first_token != second_token:
        raise EvidenceError("sqlite_embedding_stamp_not_idempotent")
    return {
        "status": "passed",
        "baseline_tag": BASELINE_TAG,
        "baseline_commit": BASELINE_COMMIT,
        "source_method": "git_archive_no_checkout",
        "data_preserved": first["counts"],
        "recall": first["recall"],
        "embedding_signature": "current",
        "embedding_stamp": "one_nonempty_stable_row",
    }


def _database_url_for(root_url: str, database_name: str) -> str:
    parsed = urlsplit(root_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise EvidenceError("postgres_url_invalid")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise EvidenceError("postgres_url_query_invalid") from exc
    socket_hosts = query.get("host", [])
    if parsed.hostname is None and (len(socket_hosts) != 1 or not socket_hosts[0].startswith("/")):
        raise EvidenceError("postgres_url_invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, ""))


def _required_role_from_url(database_url: str, *, expected: str, code: str) -> str:
    role = unquote(urlsplit(database_url).username or "")
    if role != expected:
        raise EvidenceError(code)
    return role


def _libpq_env(database_url: str) -> dict[str, str]:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise EvidenceError("postgres_url_invalid")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise EvidenceError("postgres_url_query_invalid") from exc
    socket_hosts = query.get("host", [])
    if parsed.hostname is None:
        if len(socket_hosts) != 1 or not socket_hosts[0].startswith("/"):
            raise EvidenceError("postgres_url_invalid")
        host = socket_hosts[0]
    else:
        host = parsed.hostname
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": host,
            "PGPORT": str(parsed.port or 5432),
            "PGUSER": unquote(parsed.username or ""),
            "PGDATABASE": unquote(parsed.path.removeprefix("/")),
        }
    )
    password = unquote(parsed.password or "")
    if password:
        env["PGPASSWORD"] = password
    for query_name, environment_name in (
        ("sslmode", "PGSSLMODE"),
        ("sslrootcert", "PGSSLROOTCERT"),
    ):
        values = query.get(query_name)
        if values is None:
            continue
        if (
            len(values) != 1
            or not values[0]
            or values[0] != values[0].strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in values[0])
        ):
            raise EvidenceError(f"postgres_{query_name}_invalid")
        env[environment_name] = values[0]
    return env


def _postgres_client_major(executable: str, program: str) -> int:
    completed = _run(
        [executable, "--version"],
        code=f"postgres_{program}_version_unavailable",
    )
    match = re.fullmatch(
        rf"{re.escape(program)} \(PostgreSQL\) ([1-9][0-9]*)(?:\.[0-9]+)*(?:\s+.*)?",
        completed.stdout.strip(),
    )
    if match is None:
        raise EvidenceError(f"postgres_{program}_version_invalid")
    return int(match.group(1))


def _postgres_server_major(root_url: str) -> int:
    import psycopg

    try:
        with psycopg.connect(root_url) as conn:
            row = conn.execute("SHOW server_version_num").fetchone()
    except Exception as exc:
        raise EvidenceError("postgres_server_version_unavailable") from exc
    raw_version: object | None
    if isinstance(row, Mapping):
        raw_version = row.get("server_version_num")
    elif isinstance(row, (tuple, list)) and row:
        raw_version = row[0]
    else:
        raw_version = None
    value = str(raw_version) if isinstance(raw_version, (str, int)) else ""
    if re.fullmatch(r"[0-9]{5,6}", value) is None:
        raise EvidenceError("postgres_server_version_invalid")
    return int(value) // 10000


def _validate_postgres_role_posture(
    database_url: str,
    *,
    expected_role: str,
    expected_createdb: bool,
    expected_bypassrls: bool,
    code: str,
) -> None:
    import psycopg

    try:
        with psycopg.connect(database_url) as conn:
            row = conn.execute(
                """
                SELECT
                  current_user::text AS role_name,
                  rolsuper,
                  rolcreatedb,
                  rolcreaterole,
                  rolbypassrls
                FROM pg_roles
                WHERE rolname = current_user
                """
            ).fetchone()
    except Exception as exc:
        raise EvidenceError(code) from exc
    if isinstance(row, Mapping):
        values = (
            row.get("role_name"),
            row.get("rolsuper"),
            row.get("rolcreatedb"),
            row.get("rolcreaterole"),
            row.get("rolbypassrls"),
        )
    elif isinstance(row, (tuple, list)) and len(row) == 5:
        values = tuple(row)
    else:
        raise EvidenceError(code)
    expected = (
        expected_role,
        False,
        expected_createdb,
        False,
        expected_bypassrls,
    )
    if values != expected:
        raise EvidenceError(code)


def _validate_postgres_toolchain(
    *,
    root_admin_url: str,
    pg_dump: str,
    pg_restore: str,
) -> None:
    client_majors = (
        _postgres_client_major(pg_dump, "pg_dump"),
        _postgres_client_major(pg_restore, "pg_restore"),
    )
    if client_majors != (POSTGRES_MAJOR, POSTGRES_MAJOR):
        raise EvidenceError("postgres_client_major_mismatch")
    if _postgres_server_major(root_admin_url) != POSTGRES_MAJOR:
        raise EvidenceError("postgres_server_major_mismatch")


def _create_database(
    lifecycle_url: str,
    database_name: str,
    *,
    admin_role: str,
    app_role: str,
    backup_role: str,
) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(lifecycle_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        conn.execute(
            sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(database_name),
                sql.Identifier(admin_role),
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                sql.Identifier(database_name),
                sql.Identifier(app_role),
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database_name),
                sql.Identifier(backup_role),
            )
        )
    target_lifecycle_url = _database_url_for(lifecycle_url, database_name)
    with psycopg.connect(target_lifecycle_url) as conn:
        conn.execute(
            sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {} WITH GRANT OPTION").format(sql.Identifier(admin_role))
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(
                sql.Identifier(app_role),
                sql.Identifier(backup_role),
            )
        )


def _drop_database(lifecycle_url: str, database_name: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(lifecycle_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))


def _grant_backup_access(admin_url: str, *, admin_role: str, backup_role: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_url) as conn:
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(backup_role)))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(backup_role)))
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(backup_role))
        )
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(
                sql.Identifier(admin_role),
                sql.Identifier(backup_role),
            )
        )
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON SEQUENCES TO {}").format(
                sql.Identifier(admin_role),
                sql.Identifier(backup_role),
            )
        )


def _write_restore_list(archive_listing: str, path: Path) -> None:
    retained: list[str] = []
    excluded = 0
    for line in archive_listing.splitlines(keepends=True):
        if _PUBLIC_SCHEMA_ACL_TOC.match(line):
            excluded += 1
            continue
        retained.append(line)
    if excluded != 1:
        raise EvidenceError("postgres_public_schema_acl_toc_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.writelines(retained)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _verify_restored_role_access(
    admin_url: str,
    *,
    app_role: str,
    backup_role: str,
) -> None:
    import psycopg

    with psycopg.connect(admin_url) as conn:
        row = conn.execute(
            """
            SELECT
              EXISTS (
                SELECT 1
                FROM pg_namespace AS app_namespace
                CROSS JOIN LATERAL aclexplode(app_namespace.nspacl) AS app_acl
                JOIN pg_roles AS app_grantee
                  ON app_grantee.oid = app_acl.grantee
                WHERE app_namespace.nspname = 'public'
                  AND app_grantee.rolname = %s
                  AND app_acl.privilege_type = 'USAGE'
              ),
              has_table_privilege(%s, 'public.memories', 'SELECT'),
              EXISTS (
                SELECT 1
                FROM pg_namespace AS backup_namespace
                CROSS JOIN LATERAL aclexplode(backup_namespace.nspacl)
                  AS backup_acl
                JOIN pg_roles AS backup_grantee
                  ON backup_grantee.oid = backup_acl.grantee
                WHERE backup_namespace.nspname = 'public'
                  AND backup_grantee.rolname = %s
                  AND backup_acl.privilege_type = 'USAGE'
              ),
              COALESCE(
                bool_and(has_table_privilege(%s, relation.oid, 'SELECT')),
                false
              )
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind IN ('r', 'p')
            """,
            (app_role, app_role, backup_role, backup_role),
        ).fetchone()
    if row != (True, True, True, True):
        raise EvidenceError("postgres_restored_role_privileges_invalid")


def _dynamic_alembic_head() -> str:
    from alembic.script import ScriptDirectory
    from alicebot_api.migrations import make_alembic_config

    scripts = ScriptDirectory.from_config(make_alembic_config())
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise EvidenceError("alembic_multiple_heads")
    try:
        migration_0093 = scripts.get_revision(MIGRATION_0093)
        migration_0094 = scripts.get_revision(MIGRATION_0094)
    except Exception as exc:
        raise EvidenceError("required_migration_missing") from exc
    if migration_0093 is None or migration_0094 is None:
        raise EvidenceError("required_migration_missing")
    return heads[0]


def _migrate_postgres(database_url: str, revision: str = "head") -> None:
    from alembic import command
    from alicebot_api.migrations import make_alembic_config

    command.upgrade(make_alembic_config(database_url), revision)


def _postgres_counts(conn: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in (
        "users",
        "memories",
        "event_log",
        "generated_artifacts",
        "artifact_quality_ratings",
    ):
        row = conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()
        if row is None or "count" not in row:
            raise EvidenceError("postgres_count_row_invalid")
        counts[table] = int(row["count"])
    return counts


def _verify_postgres_store(admin_url: str, app_url: str, *, expected_head: str) -> dict[str, object]:
    from alicebot_api.db import direct_user_connection
    from alicebot_api.vnext_embeddings import memory_embedding_signature_is_current
    from alicebot_api.vnext_store import PostgresVNextStore

    with direct_user_connection(admin_url, USER_ID) as conn:
        revision_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if revision_row is None:
            raise EvidenceError("postgres_alembic_version_missing")
        revision = str(revision_row["version_num"])
        if revision != expected_head:
            raise EvidenceError("postgres_alembic_head_mismatch")
        capabilities_row = conn.execute("SELECT to_regclass('public.browser_clip_capabilities')").fetchone()
        if capabilities_row is None or capabilities_row["to_regclass"] is None:
            raise EvidenceError("postgres_migration_0094_table_missing")
        vector_row = conn.execute(
            "SELECT embedding_vector IS NOT NULL AS present FROM memories WHERE id = %s::uuid",
            (MEMORY_ID,),
        ).fetchone()
        if vector_row is None:
            raise EvidenceError("postgres_seed_memory_missing")
        vector_present = bool(vector_row["present"])
        counts = _postgres_counts(conn)
    if not vector_present:
        raise EvidenceError("postgres_embedding_missing")
    with direct_user_connection(app_url, USER_ID) as conn:
        store = PostgresVNextStore(conn)
        memory = store.get_memory(MEMORY_ID)
        recalled = store.search_memories_fts(
            query=SEED_QUERY,
            sensitivity_allowed=["internal"],
            limit=5,
        )
    if memory is None or not memory_embedding_signature_is_current(memory):
        raise EvidenceError("postgres_embedding_signature_invalid")
    if MEMORY_ID not in {str(item.get("id")) for item in recalled}:
        raise EvidenceError("postgres_recall_failed")
    return {
        "counts": counts,
        "alembic_head": revision,
        "recall": "matched",
        "embedding_signature": "current",
    }


def _verify_migration_0093(admin_url: str) -> None:
    from psycopg import errors
    from alicebot_api.db import direct_user_connection

    with direct_user_connection(admin_url, USER_ID) as conn:
        rows = conn.execute(
            """
            SELECT id::text, usefulness
            FROM artifact_quality_ratings
            WHERE artifact_id = %s::uuid AND reviewer_id = %s
            """,
            (ARTIFACT_ID, REVIEWER_ID),
        ).fetchall()
        normalized_rows = [(str(row["id"]), _as_int(row["usefulness"])) for row in rows]
        if normalized_rows != [(NEWER_RATING_ID, 5)]:
            raise EvidenceError("migration_0093_survivor_mismatch")
        try:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO artifact_quality_ratings (
                      user_id, artifact_id, reviewer_id, usefulness, verbosity
                    ) VALUES (%s, %s, %s, 1, 'right_sized')
                    """,
                    (USER_ID, ARTIFACT_ID, REVIEWER_ID),
                )
        except errors.UniqueViolation:
            pass
        else:
            raise EvidenceError("migration_0093_unique_not_enforced")


def _verify_derived_labels(admin_url: str) -> None:
    """The migrations must leave no derived row below its inputs or unverified."""

    from alicebot_api.db import direct_user_connection
    from alicebot_api.vnext_label_repair import classify_stored_labels

    with direct_user_connection(admin_url, USER_ID) as conn:
        from alicebot_api.vnext_label_repair import INPUT_SELECTS_V3

        tables: dict[str, list[dict[str, object]]] = {}
        for table, statement in INPUT_SELECTS_V3.items():
            cursor = conn.execute(statement)
            names = [column[0] for column in cursor.description]
            tables[table] = [
                row if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()
            ]
        below, unverified = classify_stored_labels(tables)
    if below or any(unverified.values()):
        raise EvidenceError("derived_labels_not_repaired")


def _seed_postgres_baseline(
    *,
    baseline: Path,
    admin_url: str,
    app_url: str,
) -> None:
    env = _source_env(
        baseline / "apps" / "api" / "src",
        {"DATABASE_ADMIN_URL": admin_url, "DATABASE_URL": app_url},
    )
    _run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "--backend",
            "postgres",
            "--label",
            "v0.12.0",
            "--migrate-to-head",
            "--seed-migration-0093-fixture",
        ],
        code="postgres_baseline_seed_failed",
        env=env,
        timeout=600,
    )


def _postgres_drill(
    work_dir: Path,
    *,
    baseline: Path,
    root_lifecycle_url: str,
    root_admin_url: str,
    root_app_url: str,
    root_backup_url: str,
) -> dict[str, object]:
    import psycopg

    pg_dump = _require_tool("pg_dump")
    pg_restore = _require_tool("pg_restore")
    admin_role = _required_role_from_url(
        root_admin_url,
        expected="alicebot_admin",
        code="postgres_admin_role_must_be_alicebot_admin",
    )
    app_role = _required_role_from_url(
        root_app_url,
        expected="alicebot_app",
        code="postgres_app_role_must_be_alicebot_app",
    )
    backup_role = _required_role_from_url(
        root_backup_url,
        expected="alicebot_backup",
        code="postgres_backup_role_must_be_alicebot_backup",
    )
    lifecycle_role = _required_role_from_url(
        root_lifecycle_url,
        expected="alicebot_drill",
        code="postgres_lifecycle_role_must_be_alicebot_drill",
    )
    if len({lifecycle_role, admin_role, app_role, backup_role}) != 4:
        raise EvidenceError("postgres_operational_roles_not_distinct")
    for database_url, expected_role, expected_createdb, expected_bypassrls, code in (
        (
            root_lifecycle_url,
            lifecycle_role,
            True,
            False,
            "postgres_lifecycle_role_posture_invalid",
        ),
        (
            root_admin_url,
            admin_role,
            False,
            False,
            "postgres_admin_role_posture_invalid",
        ),
        (
            root_app_url,
            app_role,
            False,
            False,
            "postgres_app_role_posture_invalid",
        ),
        (
            root_backup_url,
            backup_role,
            False,
            True,
            "postgres_backup_role_posture_invalid",
        ),
    ):
        _validate_postgres_role_posture(
            database_url,
            expected_role=expected_role,
            expected_createdb=expected_createdb,
            expected_bypassrls=expected_bypassrls,
            code=code,
        )
    _validate_postgres_toolchain(
        root_admin_url=root_admin_url,
        pg_dump=pg_dump,
        pg_restore=pg_restore,
    )
    suffix = uuid4().hex[:10]
    database_name = f"alice_phase5_ops_{suffix}"
    admin_url = _database_url_for(root_admin_url, database_name)
    app_url = _database_url_for(root_app_url, database_name)
    backup_url = _database_url_for(root_backup_url, database_name)
    dump_path = work_dir / "postgres.dump"
    restore_list_path = work_dir / "postgres.restore.list"
    result: dict[str, object] | None = None
    primary_error: Exception | None = None
    try:
        _create_database(
            root_lifecycle_url,
            database_name,
            admin_role=admin_role,
            app_role=app_role,
            backup_role=backup_role,
        )
        _seed_postgres_baseline(baseline=baseline, admin_url=admin_url, app_url=app_url)
        with psycopg.connect(admin_url) as conn:
            old_head_row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            if old_head_row is None:
                raise EvidenceError("postgres_baseline_version_missing")
            old_head = str(old_head_row[0])
        if old_head != BASELINE_POSTGRES_HEAD:
            raise EvidenceError("postgres_baseline_head_mismatch")

        current_head = _dynamic_alembic_head()
        _migrate_postgres(admin_url)
        _verify_migration_0093(admin_url)
        before = _verify_postgres_store(admin_url, app_url, expected_head=current_head)
        _grant_backup_access(
            admin_url,
            admin_role=admin_role,
            backup_role=backup_role,
        )

        _run(
            [pg_dump, "--format=custom", "--no-comments"],
            code="postgres_dump_failed",
            env=_libpq_env(backup_url),
            stdout_file=dump_path,
            timeout=600,
        )
        archive_listing = _run(
            [pg_restore, "--list"],
            code="postgres_dump_archive_invalid",
            env=_libpq_env(backup_url),
            stdin_file=dump_path,
        ).stdout
        if re.search(r"(?m)^[0-9]+;\s+[0-9]+\s+[0-9]+\s+COMMENT\b", archive_listing):
            raise EvidenceError("postgres_dump_comments_present")
        _write_restore_list(archive_listing, restore_list_path)
        dump_sha256 = _sha256_file(dump_path)

        _drop_database(root_lifecycle_url, database_name)
        _create_database(
            root_lifecycle_url,
            database_name,
            admin_role=admin_role,
            app_role=app_role,
            backup_role=backup_role,
        )
        _run(
            [
                pg_restore,
                "--exit-on-error",
                "--no-owner",
                "--no-comments",
                f"--use-list={restore_list_path}",
                f"--dbname={database_name}",
            ],
            code="postgres_restore_failed",
            env=_libpq_env(admin_url),
            stdin_file=dump_path,
            timeout=600,
        )
        _verify_restored_role_access(
            admin_url,
            app_role=app_role,
            backup_role=backup_role,
        )
        after = _verify_postgres_store(admin_url, app_url, expected_head=current_head)
        _verify_migration_0093(admin_url)
        _verify_derived_labels(admin_url)
        if before["counts"] != after["counts"]:
            raise EvidenceError("postgres_restore_count_mismatch")
        result = {
            "status": "passed",
            "backup_sha256": dump_sha256,
            "counts": after["counts"],
            "destroy_restore": "proved_on_disposable_database",
            "recall": after["recall"],
            "embedding_signature": after["embedding_signature"],
            "baseline_head": old_head,
            "current_head": current_head,
            "migration_0093": "newest_survived_and_unique_enforced",
            "migration_0094": "table_present",
        }
    except Exception as exc:
        primary_error = exc

    cleanup_error: Exception | None = None
    try:
        # The name is random and DROP is idempotent, so always clean up.  This
        # also covers CREATE succeeding before its subsequent GRANT fails.
        _drop_database(root_lifecycle_url, database_name)
    except Exception as exc:
        cleanup_error = exc

    if cleanup_error is not None:
        if primary_error is None:
            raise EvidenceError("postgres_cleanup_failed") from cleanup_error
        primary_codes = (
            primary_error.codes
            if isinstance(primary_error, EvidenceError)
            else (f"unexpected:{type(primary_error).__name__.lower()}",)
        )
        raise EvidenceError(*(primary_codes + ("postgres_cleanup_failed",))) from cleanup_error
    if primary_error is not None:
        raise primary_error
    if result is None:  # pragma: no cover - defensive exhaustiveness
        raise EvidenceError("postgres_result_missing")
    return result


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def classify_scheduler_snapshot(snapshot: Mapping[str, object], *, now: datetime) -> dict[str, object]:
    """Classify the documented scheduler status fields without leaking values."""

    if snapshot.get("configured") is not True:
        return {"state": "disabled", "reason_codes": []}
    stuck: list[str] = []
    degraded: list[str] = []
    reported_running = snapshot.get("reported_running") is True
    running = snapshot.get("running") is True
    if reported_running and snapshot.get("ownership_verified") is not True:
        stuck.append("ownership_unverified")
    interval = snapshot.get("interval_seconds")
    interval_seconds = float(interval) if isinstance(interval, (int, float)) and interval > 0 else 60.0
    heartbeat = _parse_timestamp(snapshot.get("last_heartbeat_at"))
    if running and (heartbeat is None or now - heartbeat > timedelta(seconds=max(60.0, interval_seconds * 3))):
        stuck.append("heartbeat_stale")
    if isinstance(snapshot.get("last_error_code"), str) and str(snapshot["last_error_code"]).strip():
        degraded.append("last_error")
    expired = snapshot.get("expired_claim_count", 0)
    if isinstance(expired, int) and not isinstance(expired, bool) and expired > 0:
        degraded.append("expired_claims")
    reasons = sorted(set((*stuck, *degraded)))
    state = "stuck" if stuck else "degraded" if degraded else "healthy" if running else "stopped"
    return {"state": state, "reason_codes": reasons}


def _monitoring_drill() -> dict[str, object]:
    from alicebot_api.main import build_healthcheck_payload

    class SettingsStub:
        app_env = "phase5-evidence"
        redis_url = "redis://operator:do-not-report@127.0.0.1:6379/0"

    healthy = build_healthcheck_payload(SettingsStub(), True)  # type: ignore[arg-type]
    degraded = build_healthcheck_payload(SettingsStub(), False)  # type: ignore[arg-type]
    if healthy["status"] != "ok" or degraded["status"] != "degraded":
        raise EvidenceError("health_contract_invalid")
    if healthy["services"]["redis"]["status"] != "not_checked":
        raise EvidenceError("health_redis_contract_invalid")
    if healthy["services"]["object_storage"]["status"] != "not_checked":
        raise EvidenceError("health_object_storage_contract_invalid")
    now = datetime.now(UTC)
    stuck = classify_scheduler_snapshot(
        {
            "configured": True,
            "reported_running": True,
            "running": True,
            "ownership_verified": False,
            "last_heartbeat_at": (now - timedelta(minutes=10)).isoformat(),
            "interval_seconds": 30,
            "last_error_code": "claim_failed",
            "expired_claim_count": 1,
        },
        now=now,
    )
    expected_reasons = [
        "expired_claims",
        "heartbeat_stale",
        "last_error",
        "ownership_unverified",
    ]
    if stuck["state"] != "stuck" or stuck["reason_codes"] != expected_reasons:
        raise EvidenceError("scheduler_stuck_contract_invalid")
    return {
        "status": "passed",
        "healthz": {
            "database": "checked",
            "redis": "not_checked",
            "object_storage": "not_checked",
            "failure_status": "degraded",
        },
        "scheduler": {
            "stuck_detection": "proved",
            "fields": [
                "running",
                "ownership_verified",
                "last_heartbeat_at",
                "interval_seconds",
                "last_error_code",
                "expired_claim_count",
            ],
        },
    }


def _assert_report_safe(value: object) -> None:
    forbidden_literals = (
        SEED_QUERY,
        "phase5-ops@example.invalid",
        "do-not-report",
        str(ROOT_DIR),
    )

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                lowered = str(key).lower()
                if any(name in lowered for name in ("password", "secret", "database_url", "admin_url")):
                    raise EvidenceError("report_sensitive_key")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            if _CREDENTIAL_URL.search(item) or any(literal in item for literal in forbidden_literals):
                raise EvidenceError("report_sensitive_value")

    visit(value)


def _write_report(path: Path, report: dict[str, object]) -> None:
    _assert_report_safe(report)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(report, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute Phase 5 operations evidence with sanitized JSON output.")
    parser.add_argument("--backend", choices=("sqlite", "postgres", "all"), default="all")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="Parent for private ephemeral drill files; raw stores are removed on exit.",
    )
    parser.add_argument("--database-admin-url", default=os.getenv("DATABASE_ADMIN_URL"))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--database-backup-url", default=os.getenv("DATABASE_BACKUP_URL"))
    parser.add_argument(
        "--database-lifecycle-url",
        default=os.getenv("DATABASE_LIFECYCLE_URL"),
    )
    parser.add_argument("--output", default=None, help="Optional durable sanitized JSON report path.")
    return parser


def _scope_proof_gaps(backend: str) -> list[str]:
    if backend == "sqlite":
        return ["postgres_not_requested"]
    if backend == "postgres":
        return ["sqlite_not_requested"]
    return []


def run_evidence(args: argparse.Namespace) -> dict[str, object]:
    requested = args.backend
    if requested in {"postgres", "all"} and any(
        not value
        for value in (
            args.database_lifecycle_url,
            args.database_admin_url,
            args.database_url,
            args.database_backup_url,
        )
    ):
        raise EvidenceError("missing_prerequisite:postgres_urls")
    parent = Path(args.work_dir).expanduser() if args.work_dir else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    checks: dict[str, object] = {}
    baseline: Path | None = None
    with tempfile.TemporaryDirectory(prefix="alice-phase5-ops-", dir=parent) as raw_dir:
        work_dir = Path(raw_dir)
        os.chmod(work_dir, 0o700)
        if requested in {"sqlite", "all", "postgres"}:
            baseline = _extract_baseline(work_dir)
        if requested in {"sqlite", "all"}:
            physical, restored_source = _sqlite_physical_drill(work_dir)
            checks["sqlite_physical_backup_restore"] = physical
            checks["portable_export_import"] = _sqlite_portable_drill(work_dir, restored_source)
            assert baseline is not None
            checks["sqlite_v0_12_upgrade"] = _sqlite_upgrade_drill(work_dir, baseline)
        if requested in {"postgres", "all"}:
            assert baseline is not None
            checks["postgres_backup_restore_upgrade"] = _postgres_drill(
                work_dir,
                baseline=baseline,
                root_lifecycle_url=args.database_lifecycle_url,
                root_admin_url=args.database_admin_url,
                root_app_url=args.database_url,
                root_backup_url=args.database_backup_url,
            )
        checks["health_and_monitoring"] = _monitoring_drill()

    carrier_identity = repository_carrier_identity()
    report: dict[str, object] = {
        "artifact_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "backend_scope": requested,
        "repository": {
            **carrier_identity,
            "baseline_tag": BASELINE_TAG,
            "baseline_commit": BASELINE_COMMIT,
        },
        "checks": checks,
        "proof_gaps": _scope_proof_gaps(requested),
    }
    _assert_report_safe(report)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = run_evidence(args)
        exit_code = 0
    except EvidenceError as exc:
        report = {
            "artifact_version": REPORT_VERSION,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": "failed",
            "backend_scope": args.backend,
            "checks": {},
            "failure_codes": list(exc.codes),
            "proof_gaps": _scope_proof_gaps(args.backend),
        }
        exit_code = 1
    except Exception as exc:  # pragma: no cover - fail-closed process boundary
        report = {
            "artifact_version": REPORT_VERSION,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "status": "failed",
            "backend_scope": args.backend,
            "checks": {},
            "failure_codes": [f"unexpected:{type(exc).__name__.lower()}"],
            "proof_gaps": _scope_proof_gaps(args.backend),
        }
        exit_code = 1
    _assert_report_safe(report)
    if args.output:
        _write_report(Path(args.output).expanduser(), report)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
