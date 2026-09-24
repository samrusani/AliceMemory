"""``alice-memory``: the zero-infrastructure SQLite on-ramp for Alice.

Runs the stdio MCP server (the core tool surface) against a local SQLite
file instead of Postgres. No services, no migrations: the schema is
bootstrapped into ``~/.alice/memory.db`` (or ``--data-dir``/``--db``) on
startup and the default local user row is created.

Subcommands:
- ``mcp`` (default): serve MCP over stdio. stdout carries only the MCP
  protocol; human-facing notices go to stderr.
- ``export``: dump the memory graph as JSONL -- memories, sources, source
  chunks, entities and relationship changes, graph edges, memory revisions,
  provenance links, open loops, and the event log.
- ``import``: load an export JSONL file into a (new or existing) local
  database, preserving ids and timestamps so provenance references and
  the audit trail survive the round trip. ``--quarantine`` removes the
  credential from each named memory and from the records derived from it,
  and reports any other copies it finds.
- ``reindex-embeddings``: rebuild missing or provider/model-incompatible
  vectors in place after an import, upgrade, or embedding-model change.
- ``brief``: print a labelled session brief (committed facts and imported
  sources) as markdown on stdout. Host session-start hooks call this.
- ``doctor``: print a local SQLite vault census on stdout: sources,
  searchable chunks, committed facts, last brief token estimate,
  then candidates waiting. Not ``alicebot vnext doctor``.
- ``demo``: import a markdown folder into a SQLite vault, then print
  the import summary, doctor, session brief, and the one source
  snippet a new session will quote. Defaults to ``~/.alice-demo``,
  not ``~/.alice``.
- ``sleep``: write a capped sidecar of source commit proposals next
  to the db. Imported notes and committed facts are not rewritten.
  Search is unchanged. Accept is a later commit. Defaults to
  ``~/.alice``, like doctor.
- ``install``: write host MCP config (and optional SessionStart hooks)
  under ``--home``. Does not import a vault. Hermes is opt-in.
- ``--version``: print the package version.

Export/import round-trip contract ("you own the memory"):
- Every exported record keeps its original ``id`` and timestamps on
  import, so ``export -> import into a fresh file -> export`` produces
  equivalent records (event-log ordering aside).
- Versioned exports carry an explicit record schema plus per-type counts
  and a stable SHA-256 data-record footer. Export verifies a raw read-only
  source-family replica, copies it through SQLite's private backup API,
  upgrades only the private snapshot, and atomically replaces a file
  destination.
- Import writes rows via direct INSERT rather than the store's
  ``create_*`` methods: those methods re-stamp ``created_at``/``updated_at``
  and append fresh ``*.created`` mutation events, which would corrupt the
  imported audit trail. Historical events also use direct INSERT so their
  ids, occurred_at values, and integrity hashes remain byte-for-byte exact,
  except a memory named by ``--quarantine``. That memory is stored with
  status ``rejected``. Its text is replaced by ``[quarantined on import]``,
  its JSON columns become ``{"quarantined": true}``, its ``memory_key``
  becomes ``quarantined.<memory_id>``, and ``commit_digest`` is cleared.
  The integrity hash of each event that belongs to it is cleared because
  that hash is a SHA-256 of the event record, including the payload, so a
  reconstructed original payload could be checked against it. The event
  payload keeps ``memory_id`` and ``candidate_memory_id`` when they name a
  quarantined memory, so a later redact can still update the row.
  Append-only triggers on ``event_log``/``memory_revisions`` only block
  UPDATE/DELETE.
- Soft-deleted rows are omitted. Nullable references to omitted parents are
  cleared, and graph edges with omitted known endpoints are left behind, so
  the portable record set can be restored into a fresh database.
- Embedding vectors are NOT exported (they are provider-specific blobs);
  imported memories are searchable via FTS immediately and re-enter
  vector search once re-embedded (configure ``ALICE_EMBEDDINGS_*`` and run
  ``alice-memory reindex-embeddings``).
"""

from __future__ import annotations

import argparse
import hashlib
from contextlib import contextmanager, redirect_stderr
from io import StringIO
import json
import logging
import marshal
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import IO
from uuid import UUID

from alicebot_api import __version__
from alicebot_api.credential_floor import VERDICT_EXPANSION, credential_verdict, is_derived_copy, string_values
from alicebot_api.mcp_server import _DEFAULT_MCP_USER_ID, MCPServer
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.sqlite_schema import bootstrap_sqlite_schema
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.sqlite_store import (
    ENTITY_COLUMNS,
    ENTITY_RELATIONSHIP_EVENT_COLUMNS,
    EVENT_LOG_COLUMNS,
    GRAPH_EDGE_COLUMNS,
    MEMORY_COLUMNS,
    OPEN_LOOP_COLUMNS,
    PROVENANCE_COLUMNS,
    REVISION_COLUMNS,
    SOURCE_CHUNK_COLUMNS,
    SOURCE_COLUMNS,
    SQLiteVNextStore,
    _JSON_COLUMNS,
    ensure_sqlite_user,
    sqlite_user_connection,
)
from alicebot_api.vnext_json import json_safe
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_VERSION,
    MAX_EMBEDDINGS_BATCH_SIZE,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    endpoint_fingerprint,
    get_embedding_provider,
    memory_embedding_text,
    signed_memory_embedding_update,
)

DEFAULT_DATA_DIR = "~/.alice"
DEFAULT_DEMO_DATA_DIR = "~/.alice-demo"
DEFAULT_DB_FILENAME = "memory.db"
DEFAULT_USER_EMAIL = "local@alice"
_KNOWN_COMMANDS = (
    "mcp",
    "export",
    "import",
    "reindex-embeddings",
    "brief",
    "doctor",
    "demo",
    "sleep",
    "sleep-proposals",
    "install",
)

_EXPORT_FORMAT = "alice-memory-jsonl"
_EXPORT_FORMAT_VERSION = 2
_EXPORT_SCHEMA_VERSION = 1
_EXPORT_HEADER_TYPE = "export_header"
_EXPORT_FOOTER_TYPE = "export_footer"
_EXPORT_FETCH_SIZE = 512
_EXPORT_INTEGRITY_SCOPE = "canonical-data-record-lines"
_LEGACY_V2_INTEGRITY_SCOPE = "canonical-header-and-data-record-lines"
_SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")
_SQLITE_SNAPSHOT_SUFFIXES = ("", "-wal", "-journal")
_SQLITE_SNAPSHOT_ATTEMPTS = 3
_FILE_COPY_CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger(__name__)

_ERROR_CONTRACTS: dict[str, str] = {
    "invalid_request": "The command request is invalid",
    "export_source_not_found": "The export database does not exist",
    "export_path_conflict": "The export output conflicts with the database or a SQLite sidecar",
    "export_failed": "The export could not be completed",
    "import_source_not_found": "The import file does not exist",
    "import_path_conflict": "The import input conflicts with the database or a SQLite sidecar",
    "import_snapshot_failed": "The import file could not be read into a stable snapshot",
    "import_validation_failed": "The import file is invalid or incompatible",
    "import_quarantine_unknown": "A --quarantine memory id is not in the import file",
    "sqlite_db_path_required": (
        "alice-memory --db takes a SQLite file path. A Postgres URL is not a database file."
    ),
    "import_credential_material": (
        "Memories listed above carry credential material; no records were written. In the "
        "source vault, redact each listed memory, then export again. SQLite: alice_memory_manage "
        "with action=redact (needs ALICE_MCP_FULL_TOOLS=1). Postgres: alicebot vnext memories "
        "redact <memory_id> --reason <why>. Forget or correct is not enough: the old text stays "
        "in the row or its correction history. Do not edit the export by hand; that breaks its "
        "SHA-256 footer"
    ),
    "restore_failed": "The import was aborted before publication; no records were written",
    "restore_committed_hardening_failed": (
        "The restore committed, but database permissions were not hardened; do not retry blindly"
    ),
    "restore_committed_summary_failed": (
        "The restore committed, but summary output failed; do not retry blindly"
    ),
    "restore_committed_hardening_and_summary_failed": (
        "The restore committed, but permission hardening and summary output failed; do not retry blindly"
    ),
    "embedding_provider_not_configured": (
        "The embedding provider is not configured; set ALICE_EMBEDDINGS_BASE_URL and "
        "ALICE_EMBEDDINGS_MODEL, plus ALICE_EMBEDDINGS_API_KEY when required"
    ),
    "embedding_batch_size_invalid": "The embedding batch size is outside the supported range",
    "embedding_batch_failed": "An embedding batch failed",
    "alice_memory_failed": "The alice-memory command could not be completed",
    "demo_vault_invalid": (
        "The demo vault is missing, is not a directory, or has no quotable markdown"
    ),
    "demo_failed": "The demo could not complete after import",
    "sleep_failed": "The sleep pass could not complete",
    "proposals_failed": "The sleep proposal list could not be read",
    "doctor_failed": "The vault census could not be completed",
    "install_failed": "The host install could not complete",
    "install_refused": (
        "A host config was left unchanged because install could not edit it safely; "
        "add the printed snippet by hand"
    ),
}


def _emit_error(code: str) -> None:
    """Write one compact, stable error record without runtime details."""

    print(
        json.dumps(
            {"error": {"code": code, "message": _ERROR_CONTRACTS[code]}},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )

# ``MEMORY_COLUMNS`` intentionally omits provider-specific embeddings and
# derived fact keys from ordinary store reads. Embeddings remain excluded
# from portable backups, but fact keys are part of the user-owned retrieval
# state and must survive export/import.
_MEMORY_EXPORT_COLUMNS = (*MEMORY_COLUMNS, "fact_keys")

# The full export/import record surface, in FK-safe insert order: sources
# before their chunks, memories before revisions and open loops, events
# last. record_type values are singular, matching the original export
# format ("memory", "source", "open_loop", "event"); imports of old files
# lacking the newer types work unchanged.
_RECORD_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "source": ("sources", SOURCE_COLUMNS),
    "source_chunk": ("source_chunks", SOURCE_CHUNK_COLUMNS),
    "memory": ("memories", _MEMORY_EXPORT_COLUMNS),
    "entity": ("vnext_entities", ENTITY_COLUMNS),
    "entity_relationship_event": (
        "entity_relationship_events",
        ENTITY_RELATIONSHIP_EVENT_COLUMNS,
    ),
    "graph_edge": ("graph_edges", GRAPH_EDGE_COLUMNS),
    "memory_revision": ("memory_revisions", REVISION_COLUMNS),
    "provenance_link": ("provenance_links", PROVENANCE_COLUMNS),
    "open_loop": ("open_loops", OPEN_LOOP_COLUMNS),
    "event": ("event_log", EVENT_LOG_COLUMNS),
}

# Portable records intentionally omit only the provider-specific embedding
# blob. Unknown physical columns on one of these tables may contain state
# written by a newer Alice release, so an older exporter must fail closed
# instead of publishing a superficially complete but lossy backup.
_PORTABLE_TABLE_EXTRA_COLUMNS: dict[str, frozenset[str]] = {
    "memories": frozenset({"embedding"}),
}

_EMBEDDING_NOTE = (
    "imported without embeddings; configure ALICE_EMBEDDINGS_* and run "
    "'alice-memory reindex-embeddings' to restore vector search "
    "(FTS keyword recall works immediately)"
)

# One fixed string for every text field replaced by --quarantine. Recall,
# resume, and context packs do not return status ``rejected``, so this
# placeholder is not a way to import the credential it replaced.
_QUARANTINE_PLACEHOLDER = "[quarantined on import]"
_QUARANTINE_JSON_OBJECT: dict[str, bool] = {"quarantined": True}
_QUARANTINE_TEXT_FIELDS = ("title", "canonical_text", "summary", "trust_reason", "fact_keys")
_QUARANTINE_REVISION_TEXT_FIELDS = ("text_before", "text_after", "reason")
_QUARANTINE_REVISION_JSON_FIELDS = ("previous_value", "new_value", "candidate", "metadata_json")
_QUARANTINE_EVENT_LINK_KEYS = ("memory_id", "candidate_memory_id", "replacement_memory_id")
_QUARANTINE_EVENT_KEPT_KEYS = ("memory_id", "candidate_memory_id")
_SUCCESSOR_COPIED_FIELDS = frozenset({"rationale", "idempotency_key", "request_fingerprint"})
_QUARANTINE_COUNT_LABELS = (
    ("memory", "memory", "memories"),
    ("memory_revision", "revision", "revisions"),
    ("event", "event", "events"),
    ("provenance_link", "provenance quote", "provenance quotes"),
    ("open_loop", "open loop", "open loops"),
    ("graph_edge", "graph edge", "graph edges"),
    ("entity", "entity", "entities"),
    ("rollup_instance", "rollup instance", "rollup instances"),
    ("successor", "successor", "successors"),
)
_NO_QUARANTINE_REMOVAL_COMMAND = "no command removes this today"


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid UUID value: {value}") from exc


def _is_postgres_database_url(value: str) -> bool:
    """True for a ``postgres`` or ``postgresql`` URL, including driver suffixes."""
    scheme = value.strip().partition(":")[0].lower()
    return scheme == "postgres" or scheme.startswith("postgresql")


def resolve_db_path(*, data_dir: str, db: str | None) -> Path:
    """The database file path: ``--db`` wins, else ``<data-dir>/memory.db``.

    A Postgres URL is not a SQLite file. ``Path`` would otherwise treat it as
    a relative path and the command would create that file.
    """
    if db is not None:
        if _is_postgres_database_url(db):
            raise ValueError("alice-memory --db takes a SQLite file path, not a Postgres URL")
        return Path(db).expanduser().resolve()
    return Path(data_dir).expanduser().resolve() / DEFAULT_DB_FILENAME


def _refuse_postgres_db_argument(args: argparse.Namespace) -> bool:
    """Refuse ``--db`` when it is a Postgres URL. Nothing is written."""
    db = getattr(args, "db", None)
    if not isinstance(db, str) or not _is_postgres_database_url(db):
        return False
    _emit_error("sqlite_db_path_required")
    return True


def sqlite_url_for_path(path: Path) -> str:
    """A ``sqlite:///`` URL for an absolute database file path."""
    return path.resolve().as_uri().replace("file://", "sqlite://", 1)


def _paths_alias(first: Path, second: Path) -> bool:
    """True when paths resolve to, symlink to, or hard-link the same file."""
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        # The later open will report the actionable filesystem error. Never
        # turn an uncertain equality check into permission to overwrite the DB.
        return True
    return False


def _database_family_paths(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES)


def _path_aliases_database_family(candidate: Path, database: Path) -> bool:
    """Reject lexical and inode aliases to the DB and every SQLite sidecar."""
    def alias_key(path: Path) -> str:
        return unicodedata.normalize("NFC", os.fspath(path)).casefold()

    lexical_candidate = Path(os.path.abspath(os.fspath(candidate.expanduser())))
    # macOS normally uses a case-insensitive filesystem, but normcase() does
    # not fold case there. Conservatively fold the complete absolute path on
    # every platform: over-rejecting MEMORY.DB-wal on a case-sensitive volume
    # is preferable to letting a later WAL creation overwrite a backup.
    lexical_candidate_key = alias_key(lexical_candidate)
    try:
        resolved_candidate_key = alias_key(candidate.resolve(strict=False))
    except OSError:
        return True
    for member in _database_family_paths(database):
        lexical_member = Path(os.path.abspath(os.fspath(member)))
        try:
            resolved_member_key = alias_key(member.resolve(strict=False))
        except OSError:
            return True
        if (
            lexical_candidate_key == alias_key(lexical_member)
            or resolved_candidate_key == resolved_member_key
            or _paths_alias(candidate, member)
        ):
            return True
    return False


def _ensure_private_directory(path: Path) -> None:
    """Create a sensitive-data directory with owner-only permissions."""
    was_missing = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if was_missing:
        os.chmod(path, 0o700)


def _secure_sqlite_files(path: Path) -> None:
    """Make the database and any live SQLite sidecars owner-only."""
    for candidate in _database_family_paths(path):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    """Persist a same-directory atomic rename where the platform supports it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems do not support fsync on directories. The file has
        # already been fsynced; the atomic rename still preserves old-or-new.
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _best_effort_stderr(code: str) -> None:
    """Report a post-publication condition without masking committed state."""
    try:
        _emit_error(code)
    except (OSError, ValueError):
        # The operation is already committed and both output streams may be
        # closed. The distinct return code remains the machine-readable signal.
        pass


def _remove_sqlite_files(path: Path) -> None:
    """Best-effort cleanup for a temporary SQLite database and its sidecars."""
    for candidate in _database_family_paths(path):
        try:
            candidate.unlink()
        except OSError:
            pass


class _BackupError(Exception):
    """A safe, user-facing SQLite snapshot or restore error."""


_BASE_ALICE_TABLES = frozenset({"users", "memories", "sources", "event_log"})


@lru_cache(maxsize=1)
def _current_alice_table_names() -> frozenset[str]:
    """Return every table created by this Alice version, including FTS helpers."""
    probe = sqlite3.connect(":memory:")
    try:
        bootstrap_sqlite_schema(probe)
        return frozenset(_sqlite_table_names(probe))
    finally:
        probe.close()


def _read_only_sqlite_connection(path: Path) -> sqlite3.Connection:
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    wal_has_pages = wal_path.exists() and wal_path.stat().st_size > 0
    if wal_has_pages and not shm_path.exists():
        raise _BackupError(
            "database has an uncheckpointed WAL but no shared-memory index; "
            "open and cleanly close Alice before exporting"
        )
    # immutable=1 prevents a clean private snapshot/staged database from
    # growing fresh -wal/-shm files. A private copy with a live WAL needs
    # ordinary read-only WAL coordination through its SHM index.
    immutable = "" if wal_has_pages else "&immutable=1"
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro{immutable}",
        uri=True,
    )
    connection.execute("PRAGMA query_only=ON")
    return connection


@dataclass(frozen=True)
class _FileFingerprint:
    device: int
    inode: int
    size: int
    digest: str


def _fingerprint_file(path: Path) -> _FileFingerprint:
    """Read and fingerprint one file, rejecting changes during the read."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        while chunk := stream.read(_FILE_COPY_CHUNK_SIZE):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    stable_fields_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    stable_fields_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if stable_fields_before != stable_fields_after:
        raise _BackupError(f"SQLite source changed while reading {path.name}")
    return _FileFingerprint(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        digest=digest.hexdigest(),
    )


def _copy_file_with_fingerprint(source: Path, destination: Path) -> _FileFingerprint:
    """Copy one source file owner-only and fingerprint the copied bytes."""
    digest = hashlib.sha256()
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        os.chmod(destination, 0o600)
        before = os.fstat(input_stream.fileno())
        while chunk := input_stream.read(_FILE_COPY_CHUNK_SIZE):
            output_stream.write(chunk)
            digest.update(chunk)
        output_stream.flush()
        os.fsync(output_stream.fileno())
        after = os.fstat(input_stream.fileno())
    stable_fields_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    stable_fields_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if stable_fields_before != stable_fields_after:
        raise _BackupError(f"SQLite source changed while copying {source.name}")
    return _FileFingerprint(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        digest=digest.hexdigest(),
    )


def _snapshot_source_members(source_path: Path) -> tuple[Path, ...]:
    """Return the main DB plus WAL/rollback journal files that exist now."""
    return tuple(
        member
        for suffix in _SQLITE_SNAPSHOT_SUFFIXES
        if (member := Path(f"{source_path}{suffix}")).exists()
    )


def _copy_stable_sqlite_family(source_path: Path, replica_path: Path) -> None:
    """Take a verified byte-stable DB/WAL replica without opening SQLite.

    An ordinary read-only SQLite connection still writes volatile reader-lock
    bytes to a live ``-shm`` file. Instead, read the main DB and any WAL or
    rollback journal three times (fingerprint, copy, fingerprint) and accept
    the replica only if identity, size, and content stayed unchanged across
    the complete interval. SQLite reconstructs a private SHM index when the
    replica is opened. A busy writer gets bounded retries and then a clear
    failure instead of a torn backup or a source-family mutation.
    """
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise _BackupError(f"SQLite database file is not readable: {source_path}")
    last_error: Exception | None = None
    for _attempt in range(_SQLITE_SNAPSHOT_ATTEMPTS):
        attempt_dir = Path(
            tempfile.mkdtemp(prefix="family-", dir=replica_path.parent)
        )
        os.chmod(attempt_dir, 0o700)
        attempt_replica = attempt_dir / replica_path.name
        try:
            before_members = _snapshot_source_members(source_path)
            if not before_members or before_members[0] != source_path:
                raise _BackupError(f"SQLite database file disappeared: {source_path}")
            before = {member.name: _fingerprint_file(member) for member in before_members}
            copied: dict[str, _FileFingerprint] = {}
            for member in before_members:
                suffix = member.name.removeprefix(source_path.name)
                destination = Path(f"{attempt_replica}{suffix}")
                copied[member.name] = _copy_file_with_fingerprint(member, destination)
            after_members = _snapshot_source_members(source_path)
            if tuple(member.name for member in after_members) != tuple(
                member.name for member in before_members
            ):
                raise _BackupError("SQLite source family changed while taking the snapshot")
            after = {member.name: _fingerprint_file(member) for member in after_members}
            if before != copied or copied != after:
                raise _BackupError("SQLite source changed while taking the snapshot")
            # A prior interrupted move may have left only part of a replica.
            # Clear every destination member before publishing this complete,
            # verified attempt so a disappeared WAL/journal cannot survive.
            for suffix in _SQLITE_SNAPSHOT_SUFFIXES:
                try:
                    Path(f"{replica_path}{suffix}").unlink()
                except FileNotFoundError:
                    pass
            for suffix in _SQLITE_SNAPSHOT_SUFFIXES:
                copied_member = Path(f"{attempt_replica}{suffix}")
                if copied_member.exists():
                    copied_member.replace(Path(f"{replica_path}{suffix}"))
            return
        except (OSError, _BackupError) as exc:
            last_error = exc
        finally:
            _remove_sqlite_files(attempt_replica)
            try:
                attempt_dir.rmdir()
            except OSError:
                pass
    raise _BackupError(
        "SQLite database did not remain stable long enough to snapshot; "
        "quiesce writers and retry"
    ) from last_error


def _copy_sqlite_database(source_path: Path, destination_path: Path) -> None:
    """Copy one consistent SQLite snapshot without opening the source in SQLite."""
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    with tempfile.TemporaryDirectory(
        prefix="alice-memory-source-replica-", dir=destination_path.parent
    ) as raw_dir:
        replica_dir = Path(raw_dir)
        os.chmod(replica_dir, 0o700)
        replica_path = replica_dir / "source.db"
        try:
            _copy_stable_sqlite_family(source_path, replica_path)
            # This is a disposable private replica, so SQLite may safely
            # rebuild its SHM index or recover a copied rollback journal here.
            # query_only prevents application-level writes before backup.
            source = sqlite3.connect(replica_path)
            source.execute("PRAGMA query_only=ON")
            destination = sqlite3.connect(destination_path)
            source.backup(destination, pages=256, sleep=0.01)
        except sqlite3.Error as exc:
            raise _BackupError(f"could not read SQLite database snapshot: {exc}") from exc
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
    os.chmod(destination_path, 0o600)


def _sqlite_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _validate_alice_snapshot(
    conn: sqlite3.Connection,
    *,
    user_id: UUID | None = None,
    require_current_schema: bool = False,
) -> None:
    quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    if quick_check != ["ok"]:
        raise _BackupError(f"SQLite integrity check failed: {quick_check}")
    tables = _sqlite_table_names(conn)
    missing_base = sorted(_BASE_ALICE_TABLES - tables)
    if missing_base:
        raise _BackupError(
            "unsupported Alice SQLite schema; missing base tables: " + ", ".join(missing_base)
        )
    unknown_tables = sorted(
        table
        for table in tables - _current_alice_table_names()
        if not table.startswith("sqlite_stat")
    )
    if unknown_tables:
        raise _BackupError(
            "unsupported newer Alice SQLite schema; unknown application tables: "
            + ", ".join(unknown_tables)
        )
    if user_id is not None:
        user = conn.execute("SELECT 1 FROM users WHERE id = ?", (str(user_id),)).fetchone()
        if user is None:
            raise _BackupError(f"database does not contain user {user_id}")
    table_specs = {table: columns for table, columns in _RECORD_SPECS.values()}
    for table, portable_columns in table_specs.items():
        if table not in tables:
            continue
        actual_columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        allowed_columns = set(portable_columns) | set(
            _PORTABLE_TABLE_EXTRA_COLUMNS.get(table, frozenset())
        )
        unknown_columns = sorted(actual_columns - allowed_columns)
        if unknown_columns:
            raise _BackupError(
                f"unsupported newer Alice SQLite schema; {table} has unknown columns: "
                + ", ".join(unknown_columns)
            )
    if not require_current_schema:
        return
    for record_type, (table, required_columns) in _RECORD_SPECS.items():
        if table not in tables:
            raise _BackupError(
                f"snapshot upgrade did not produce required table {table} ({record_type})"
            )
        actual_columns = set(
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
        missing_columns = sorted(set(required_columns) - actual_columns)
        if missing_columns:
            raise _BackupError(
                f"snapshot upgrade left {table} without columns: {', '.join(missing_columns)}"
            )


@contextmanager
def _prepared_export_connection(
    source_path: Path, user_id: UUID
) -> Iterator[sqlite3.Connection]:
    """Yield a current-schema private copy while leaving the source untouched."""
    with tempfile.TemporaryDirectory(prefix="alice-memory-export-snapshot-") as raw_dir:
        snapshot_dir = Path(raw_dir)
        os.chmod(snapshot_dir, 0o700)
        fd, raw_snapshot = tempfile.mkstemp(
            prefix="snapshot-", suffix=".db", dir=snapshot_dir
        )
        os.close(fd)
        snapshot_path = Path(raw_snapshot)
        _copy_sqlite_database(source_path, snapshot_path)
        upgrade = sqlite3.connect(snapshot_path)
        try:
            _validate_alice_snapshot(upgrade, user_id=user_id)
            bootstrap_sqlite_schema(upgrade)
            upgrade.commit()
            _validate_alice_snapshot(
                upgrade,
                user_id=user_id,
                require_current_schema=True,
            )
        except (sqlite3.Error, ContinuityStoreInvariantError) as exc:
            raise _BackupError(f"could not upgrade private export snapshot: {exc}") from exc
        finally:
            upgrade.close()
        _secure_sqlite_files(snapshot_path)
        export_conn = _read_only_sqlite_connection(snapshot_path)
        try:
            yield export_conn
        finally:
            export_conn.close()


def _publish_staged_database(
    staged_path: Path,
    target_path: Path,
    *,
    pages: int = 256,
    progress: Callable[[int, int, int], None] | None = None,
) -> None:
    """Atomically publish staged pages through SQLite's backup transaction."""
    source = _read_only_sqlite_connection(staged_path)
    target = sqlite3.connect(target_path, timeout=5.0)
    try:
        target.execute("PRAGMA busy_timeout=5000")
        source.backup(target, pages=pages, progress=progress, sleep=0.05)
    finally:
        target.close()
        source.close()


def bootstrap_database(
    db_path: Path,
    *,
    user_id: UUID,
    user_email: str,
    secure_parent: bool = False,
) -> None:
    """Create the data dir, schema, and local user row (idempotent)."""
    parent_was_missing = not db_path.parent.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent_was_missing or secure_parent:
        os.chmod(db_path.parent, 0o700)
    conn = sqlite3.connect(str(db_path))
    try:
        bootstrap_sqlite_schema(conn)
        display_name = user_email.split("@", 1)[0].replace(".", " ").title() or None
        ensure_sqlite_user(conn, user_id, user_email, display_name)
        conn.commit()
    finally:
        conn.close()
    _secure_sqlite_files(db_path)


def _add_database_arguments(
    parser: argparse.ArgumentParser,
    *,
    data_dir_default: str | None = DEFAULT_DATA_DIR,
    data_dir_help: str | None = None,
) -> None:
    parser.add_argument(
        "--data-dir",
        default=data_dir_default,
        help=data_dir_help
        or f"Directory holding {DEFAULT_DB_FILENAME}. Defaults to {DEFAULT_DATA_DIR}.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Explicit SQLite database file path. Overrides --data-dir.",
    )
    parser.add_argument(
        "--user-id",
        type=_parse_uuid,
        default=UUID(_DEFAULT_MCP_USER_ID),
        help=f"Acting local user UUID. Defaults to {_DEFAULT_MCP_USER_ID}.",
    )
    parser.add_argument(
        "--user-email",
        default=DEFAULT_USER_EMAIL,
        help=f"Email recorded for the local user row. Defaults to {DEFAULT_USER_EMAIL}.",
    )


def _add_demo_database_arguments(parser: argparse.ArgumentParser) -> None:
    """Database flags for ``demo``. Default data dir is the demo vault, not live."""

    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DEMO_DATA_DIR,
        help=f"Directory holding {DEFAULT_DB_FILENAME}. Defaults to {DEFAULT_DEMO_DATA_DIR}.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Explicit SQLite database file path. Overrides --data-dir.",
    )
    parser.add_argument(
        "--user-id",
        type=_parse_uuid,
        default=UUID(_DEFAULT_MCP_USER_ID),
        help=f"Acting local user UUID. Defaults to {_DEFAULT_MCP_USER_ID}.",
    )
    parser.add_argument(
        "--user-email",
        default=DEFAULT_USER_EMAIL,
        help=f"Email recorded for the local user row. Defaults to {DEFAULT_USER_EMAIL}.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alice-memory",
        description=(
            "Alice's local-first memory on SQLite: serve the core MCP tools over "
            "stdio with zero infrastructure. Running with no subcommand starts 'mcp'."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"alice-memory {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Serve the Alice MCP tools over stdio against a local SQLite file.",
    )
    _add_database_arguments(mcp_parser)

    export_parser = subparsers.add_parser(
        "export",
        help=(
            "Export the memory graph as JSONL: memories, sources, source chunks, "
            "entities and relationship changes, graph edges, memory revisions, "
            "provenance links, open loops, and events."
        ),
        description=(
            "Export the portable memory graph as versioned JSONL. Use --out for "
            "backups. WARNING: never shell-redirect stdout to the SQLite database "
            "or its -wal, -shm, or -journal sidecars; the shell truncates the "
            "destination before alice-memory can validate it."
        ),
    )
    _add_database_arguments(export_parser)
    export_parser.add_argument(
        "--out",
        default=None,
        help=(
            "Private, atomic output file (recommended). Without --out, JSONL is "
            "written to stdout; never redirect stdout to the database or a sidecar."
        ),
    )

    import_parser = subparsers.add_parser(
        "import",
        help=(
            "Import an export JSONL file into a local SQLite database, "
            "creating the database if needed. Ids and timestamps are preserved."
        ),
    )
    _add_database_arguments(import_parser)
    import_parser.add_argument(
        "--in",
        dest="in_path",
        required=True,
        help="Input JSONL file produced by 'alice-memory export'.",
    )
    import_parser.add_argument(
        "--mode",
        choices=("skip", "fail"),
        default="skip",
        help=(
            "Collision handling when a record id already exists: 'skip' accepts "
            "only an identical existing row (default); 'fail' aborts on every "
            "collision. Different content with the same id always aborts, and "
            "existing rows are never overwritten."
        ),
    )
    import_parser.add_argument(
        "--quarantine",
        default=None,
        type=_quarantine_arg,
        help=(
            "Comma-separated memory ids to store as rejected. Removes the "
            "credential from each named memory and from the records derived "
            "from it, and reports any other copies it finds. After a successful "
            "import, credential_verdict scans every imported text column that "
            "was not replaced, and prints table, id, and column only. The "
            "SHA-256 footer is checked on the file as given before any "
            "replacement. An id that is not in the file is an error and "
            "nothing is written."
        ),
    )

    reindex_parser = subparsers.add_parser(
        "reindex-embeddings",
        help=(
            "Rebuild missing, unsigned, or provider/model-incompatible memory "
            "embeddings in the local SQLite database."
        ),
    )
    _add_database_arguments(reindex_parser)
    reindex_parser.add_argument(
        "--batch-size",
        type=int,
        default=MAX_EMBEDDINGS_BATCH_SIZE,
        help=(
            "Embedding request batch size. Must be between 1 and "
            f"{MAX_EMBEDDINGS_BATCH_SIZE}."
        ),
    )

    brief_parser = subparsers.add_parser(
        "brief",
        help="Print a labelled session brief from the local SQLite vault.",
    )
    _add_database_arguments(brief_parser)
    brief_parser.add_argument(
        "--query",
        default=None,
        help=(
            "Optional excerpt query. When omitted, the brief derives one from "
            "a recent committed fact, open loop, or imported source."
        ),
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help=(
            "Print a local SQLite vault census: sources, chunks, facts, "
            "candidates, then sleep proposals."
        ),
    )
    _add_database_arguments(doctor_parser)

    demo_parser = subparsers.add_parser(
        "demo",
        help=(
            "Import a markdown folder into a demo SQLite vault, then print "
            "doctor, the session brief, and the quote."
        ),
    )
    demo_parser.add_argument(
        "--vault",
        required=True,
        help="Folder of *.md files to import as sources. Required.",
    )
    _add_demo_database_arguments(demo_parser)

    sleep_parser = subparsers.add_parser(
        "sleep",
        help=(
            "Write a capped sidecar of source commit proposals, oldest "
            "sources first. A row stops counting toward the cap once its "
            "source has an active or accepted memory. The row stays in the "
            "sidecar. Does not rewrite notes or facts, and does not open "
            "the capture inbox."
        ),
    )
    _add_database_arguments(sleep_parser)

    proposals_parser = subparsers.add_parser(
        "sleep-proposals",
        help=(
            "List this user's sleep proposals oldest first by captured_at, "
            "then id, framed and JSON-quoted, with the alice_memory_commit "
            "arguments that accept each one. Applies the session brief fences "
            "and the commit door again. Writes nothing."
        ),
    )
    _add_database_arguments(proposals_parser)

    install_parser = subparsers.add_parser(
        "install",
        help=(
            "Write host MCP config for Claude Desktop, Claude Code, Cursor, "
            "and OpenClaw. Hermes is --host hermes. Does not import a vault."
        ),
    )
    # install needs to know whether --data-dir was passed: without it, each
    # host keeps the data dir its existing Alice entry already uses.
    _add_database_arguments(
        install_parser,
        data_dir_default=None,
        data_dir_help=(
            "Vault directory for the host entries. Without it, install keeps the "
            f"data dir an existing Alice entry uses, else {DEFAULT_DATA_DIR}."
        ),
    )
    install_parser.add_argument(
        "--host",
        action="append",
        choices=(
            "claude-desktop",
            "claude-code",
            "cursor",
            "openclaw",
            "hermes",
        ),
        dest="hosts",
        help=(
            "Host to configure. Repeatable. Default: claude-desktop, "
            "claude-code, cursor, openclaw. Hermes is opt-in."
        ),
    )
    install_parser.add_argument(
        "--home",
        default=None,
        help="Override Path.home() when resolving host config files.",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned paths and snippets. Write nothing.",
    )
    install_parser.add_argument(
        "--write-mcpb",
        default=None,
        help="Write a Claude Desktop .mcpb zip that launches uvx alice-memory mcp.",
    )
    return parser


def _normalized_argv(argv: list[str]) -> list[str]:
    """Insert the default ``mcp`` subcommand when none is given."""
    if argv and argv[0] in _KNOWN_COMMANDS:
        return argv
    if argv and argv[0] in {"-h", "--help", "--version"}:
        return argv
    return ["mcp", *argv]


def _run_mcp(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    database_url = sqlite_url_for_path(db_path)
    # stdout is the MCP protocol channel; the startup notice goes to stderr.
    print(
        f"alice-memory: serving MCP over stdio (db: {db_path}, user: {args.user_id})",
        file=sys.stderr,
        flush=True,
    )
    context = MCPRuntimeContext(database_url=database_url, user_id=args.user_id)
    server = MCPServer(
        context=context,
        input_stream=sys.stdin.buffer,
        output_stream=sys.stdout.buffer,
    )
    return server.run()


def _run_brief(args: argparse.Namespace) -> int:
    from alicebot_api.session_briefing import compile_local_session_brief

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    markdown = compile_local_session_brief(
        db_path,
        user_id=args.user_id,
        query=args.query,
    )
    print(markdown)
    return 0


def _run_doctor(args: argparse.Namespace) -> int:
    from alicebot_api.vault_doctor import compile_local_vault_doctor

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    from alicebot_api.vault_sleep import SleepError

    try:
        print(compile_local_vault_doctor(db_path, user_id=args.user_id))
    except SleepError:
        _emit_error("doctor_failed")
        return 1
    return 0


def _run_demo(args: argparse.Namespace) -> int:
    from alicebot_api.vault_demo import (
        DemoVaultError,
        run_local_vault_demo,
        validate_demo_vault,
    )
    from alicebot_api.vnext_capture import VNextCaptureValidationError

    try:
        vault = validate_demo_vault(args.vault)
    except DemoVaultError:
        _emit_error("demo_vault_invalid")
        return 1

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    try:
        print(
            run_local_vault_demo(
                db_path,
                user_id=args.user_id,
                vault=vault,
            )
        )
    except (DemoVaultError, VNextCaptureValidationError):
        _emit_error("demo_failed")
        return 1
    return 0


def _run_sleep_proposals(args: argparse.Namespace) -> int:
    from alicebot_api.vnext_agent_control import DEFAULT_AGENT_SENSITIVITY, evaluate_agent_policy
    from alicebot_api.vault_sleep import SleepError, compile_sleep_proposal_listing

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    decision = evaluate_agent_policy(
        identity=None,
        action="context_pack.request",
        domains=(),
        sensitivity_allowed=DEFAULT_AGENT_SENSITIVITY,
        project_scope=(),
    )
    try:
        print(
            compile_sleep_proposal_listing(
                db_path,
                user_id=args.user_id,
                effective_domains=decision.effective_domains,
                effective_sensitivity_allowed=decision.effective_sensitivity_allowed,
                effective_project_scope=decision.effective_project_scope,
            )
        )
    except SleepError:
        _emit_error("proposals_failed")
        return 1
    return 0


def _run_sleep(args: argparse.Namespace) -> int:
    from alicebot_api.vault_sleep import SleepError, run_local_vault_sleep

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    try:
        print(run_local_vault_sleep(db_path, user_id=args.user_id))
    except SleepError:
        _emit_error("sleep_failed")
        return 1
    return 0


def _run_install(args: argparse.Namespace) -> int:
    from alicebot_api.host_install import (
        InstallError,
        InstallFailed,
        InstallRefused,
        run_host_install,
    )

    try:
        print(
            run_host_install(
                home=args.home,
                data_dir=args.data_dir,
                hosts=args.hosts,
                dry_run=args.dry_run,
                write_mcpb=args.write_mcpb,
            )
        )
    except InstallFailed as failed:
        print(failed.output)
        _emit_error("install_failed")
        return 1
    except InstallRefused as refused:
        print(refused.output)
        _emit_error("install_refused")
        return 1
    except InstallError:
        _emit_error("install_failed")
        return 1
    return 0


# --- export ---------------------------------------------------------------------


def _export_line(record_type: str, row: object) -> str:
    payload = {"record_type": record_type, "record": json_safe(row)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _decoded_rows(
    conn: sqlite3.Connection, query: str, params: tuple[object, ...]
) -> Iterator[dict[str, object]]:
    """Stream dict rows with JSON TEXT decoded, bounded by a fetch batch."""
    cursor = conn.execute(query, params)
    columns = [description[0] for description in cursor.description]
    while True:
        batch = cursor.fetchmany(_EXPORT_FETCH_SIZE)
        if not batch:
            return
        for raw in batch:
            row = dict(raw) if isinstance(raw, dict) else dict(zip(columns, raw))
            for key, value in row.items():
                if key in _JSON_COLUMNS and isinstance(value, str):
                    row[key] = json.loads(value)
            yield row


def _export_rows(
    conn: sqlite3.Connection, user_id: UUID
) -> Iterator[tuple[str, object]]:
    """Yield ``(record_type, row)`` for the whole memory graph.

    Soft-deleted rows stay out. Dependent rows with mandatory parents are
    omitted; nullable references are cleared; polymorphic graph edges with
    omitted known endpoints are excluded. The resulting set is closed under
    every SQLite foreign key and can be restored into a fresh database.
    """
    uid = str(user_id)
    memory_select = []
    for column in _MEMORY_EXPORT_COLUMNS:
        if column in {"superseded_by", "supersedes"}:
            memory_select.append(
                f"""
                CASE
                  WHEN m.{column} IS NULL OR EXISTS (
                    SELECT 1 FROM memories referenced
                    WHERE referenced.id = m.{column}
                      AND referenced.user_id = m.user_id
                      AND referenced.deleted_at IS NULL
                  ) THEN m.{column}
                  ELSE NULL
                END AS {column}
                """.strip()
            )
        else:
            memory_select.append(f"m.{column}")
    relationship_select = [f"r.{column}" for column in ENTITY_RELATIONSHIP_EVENT_COLUMNS]
    relationship_select[ENTITY_RELATIONSHIP_EVENT_COLUMNS.index("source_id")] = """
        CASE WHEN r.source_id IS NULL OR EXISTS (
          SELECT 1 FROM sources relationship_source
          WHERE relationship_source.id = r.source_id
            AND relationship_source.user_id = r.user_id
            AND relationship_source.deleted_at IS NULL
        ) THEN r.source_id ELSE NULL END AS source_id
    """.strip()
    provenance_select = [f"p.{column}" for column in PROVENANCE_COLUMNS]
    provenance_select[PROVENANCE_COLUMNS.index("source_id")] = """
        CASE WHEN p.source_id IS NULL OR EXISTS (
          SELECT 1 FROM sources ps
          WHERE ps.id = p.source_id AND ps.user_id = p.user_id
            AND ps.deleted_at IS NULL
        ) THEN p.source_id ELSE NULL END AS source_id
    """.strip()
    provenance_select[PROVENANCE_COLUMNS.index("source_chunk_id")] = """
        CASE WHEN p.source_chunk_id IS NULL OR EXISTS (
          SELECT 1 FROM source_chunks pc
          JOIN sources pcs ON pcs.id = pc.source_id AND pcs.user_id = pc.user_id
          WHERE pc.id = p.source_chunk_id AND pc.user_id = p.user_id
            AND pcs.deleted_at IS NULL
        ) THEN p.source_chunk_id ELSE NULL END AS source_chunk_id
    """.strip()
    open_loop_select = [f"l.{column}" for column in OPEN_LOOP_COLUMNS]
    open_loop_select[OPEN_LOOP_COLUMNS.index("memory_id")] = """
        CASE WHEN l.memory_id IS NULL OR EXISTS (
          SELECT 1 FROM memories lm
          WHERE lm.id = l.memory_id AND lm.user_id = l.user_id
            AND lm.deleted_at IS NULL
        ) THEN l.memory_id ELSE NULL END AS memory_id
    """.strip()
    open_loop_select[OPEN_LOOP_COLUMNS.index("source_id")] = """
        CASE WHEN l.source_id IS NULL OR EXISTS (
          SELECT 1 FROM sources ls
          WHERE ls.id = l.source_id AND ls.user_id = l.user_id
            AND ls.deleted_at IS NULL
        ) THEN l.source_id ELSE NULL END AS source_id
    """.strip()
    # The store has no list_sources method (the core tools never need
    # one), so export reads the sources table directly with the same
    # user scoping and column order the store uses.
    yield from (
        ("source", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(SOURCE_COLUMNS)}
            FROM sources
            WHERE user_id = ? AND deleted_at IS NULL
            ORDER BY captured_at DESC, id DESC
            """,
            (uid,),
        )
    )
    yield from (
        ("source_chunk", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(f"c.{column}" for column in SOURCE_CHUNK_COLUMNS)}
            FROM source_chunks c
            JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
            WHERE c.user_id = ? AND s.deleted_at IS NULL
            ORDER BY c.source_id ASC, c.chunk_index ASC, c.id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("memory", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(memory_select)}
            FROM memories m
            WHERE m.user_id = ? AND m.deleted_at IS NULL
            ORDER BY m.created_at ASC, m.id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("entity", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(ENTITY_COLUMNS)}
            FROM vnext_entities
            WHERE user_id = ? AND deleted_at IS NULL
            ORDER BY created_at ASC, id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("entity_relationship_event", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(relationship_select)}
            FROM entity_relationship_events r
            JOIN vnext_entities e ON e.id = r.entity_id AND e.user_id = r.user_id
            WHERE r.user_id = ? AND e.deleted_at IS NULL
            ORDER BY r.changed_at ASC, r.id ASC
            """,
            (uid,),
        )
    )
    # All edges, including closed ones (valid_to set): the temporal
    # history is part of the graph. store.list_edges filters those out.
    yield from (
        ("graph_edge", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(f"g.{column}" for column in GRAPH_EDGE_COLUMNS)}
            FROM graph_edges g
            WHERE g.user_id = ?
              AND (
                g.from_type != 'memory' OR EXISTS (
                  SELECT 1 FROM memories m
                  WHERE m.id = g.from_id AND m.user_id = g.user_id AND m.deleted_at IS NULL
                )
              )
              AND (
                g.to_type != 'memory' OR EXISTS (
                  SELECT 1 FROM memories m
                  WHERE m.id = g.to_id AND m.user_id = g.user_id AND m.deleted_at IS NULL
                )
              )
              AND (
                g.from_type != 'entity' OR EXISTS (
                  SELECT 1 FROM vnext_entities e
                  WHERE e.id = g.from_id AND e.user_id = g.user_id AND e.deleted_at IS NULL
                )
              )
              AND (
                g.to_type != 'entity' OR EXISTS (
                  SELECT 1 FROM vnext_entities e
                  WHERE e.id = g.to_id AND e.user_id = g.user_id AND e.deleted_at IS NULL
                )
              )
              AND (
                g.from_type != 'source' OR EXISTS (
                  SELECT 1 FROM sources s
                  WHERE s.id = g.from_id AND s.user_id = g.user_id AND s.deleted_at IS NULL
                )
              )
              AND (
                g.to_type != 'source' OR EXISTS (
                  SELECT 1 FROM sources s
                  WHERE s.id = g.to_id AND s.user_id = g.user_id AND s.deleted_at IS NULL
                )
              )
              AND (
                g.from_type != 'source_chunk' OR EXISTS (
                  SELECT 1 FROM source_chunks c
                  JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
                  WHERE c.id = g.from_id AND c.user_id = g.user_id AND s.deleted_at IS NULL
                )
              )
              AND (
                g.to_type != 'source_chunk' OR EXISTS (
                  SELECT 1 FROM source_chunks c
                  JOIN sources s ON s.id = c.source_id AND s.user_id = c.user_id
                  WHERE c.id = g.to_id AND c.user_id = g.user_id AND s.deleted_at IS NULL
                )
              )
            ORDER BY g.created_at ASC, g.id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("memory_revision", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(f"r.{column}" for column in REVISION_COLUMNS)}
            FROM memory_revisions r
            JOIN memories m ON m.id = r.memory_id AND m.user_id = r.user_id
            WHERE r.user_id = ? AND m.deleted_at IS NULL
            ORDER BY r.memory_id ASC, r.sequence_no ASC, r.id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("provenance_link", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(provenance_select)}
            FROM provenance_links p
            WHERE p.user_id = ?
              AND (
                p.target_type != 'memory' OR EXISTS (
                  SELECT 1 FROM memories target_memory
                  WHERE target_memory.id = p.target_id
                    AND target_memory.user_id = p.user_id
                    AND target_memory.deleted_at IS NULL
                )
              )
              AND (
                p.target_type != 'source' OR EXISTS (
                  SELECT 1 FROM sources target_source
                  WHERE target_source.id = p.target_id
                    AND target_source.user_id = p.user_id
                    AND target_source.deleted_at IS NULL
                )
              )
              AND (
                p.target_type != 'entity' OR EXISTS (
                  SELECT 1 FROM vnext_entities target_entity
                  WHERE target_entity.id = p.target_id
                    AND target_entity.user_id = p.user_id
                    AND target_entity.deleted_at IS NULL
                )
              )
              AND (
                p.target_type != 'source_chunk' OR EXISTS (
                  SELECT 1 FROM source_chunks target_chunk
                  JOIN sources target_chunk_source
                    ON target_chunk_source.id = target_chunk.source_id
                   AND target_chunk_source.user_id = target_chunk.user_id
                  WHERE target_chunk.id = p.target_id
                    AND target_chunk.user_id = p.user_id
                    AND target_chunk_source.deleted_at IS NULL
                )
              )
            ORDER BY p.created_at ASC, p.id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("open_loop", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(open_loop_select)}
            FROM open_loops l
            WHERE l.user_id = ?
            ORDER BY l.created_at ASC, l.id ASC
            """,
            (uid,),
        )
    )
    yield from (
        ("event", row)
        for row in _decoded_rows(
            conn,
            f"""
            SELECT {", ".join(EVENT_LOG_COLUMNS)}
            FROM event_log
            WHERE user_id = ?
            ORDER BY occurred_at ASC, id ASC
            """,
            (uid,),
        )
    )


def _export_schema() -> dict[str, object]:
    record_types = {
        record_type: list(columns)
        for record_type, (_table, columns) in _RECORD_SPECS.items()
    }
    canonical = json.dumps(
        record_types,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "version": _EXPORT_SCHEMA_VERSION,
        "record_types": record_types,
        "fingerprint": hashlib.sha256(canonical).hexdigest(),
    }


def _write_export(
    stream: IO[str],
    *,
    db_path: Path,
    user_id: UUID,
    credential_findings: list["_CredentialFinding"] | None = None,
) -> int:
    """Write a versioned export from a read-only private SQLite snapshot.

    When ``credential_findings`` is given, every memory row is also run
    through the same credential check import applies, so the owner hears
    about a row import will refuse while the source vault still exists.
    """
    with _prepared_export_connection(db_path, user_id) as conn:
        header = {
            "format": _EXPORT_FORMAT,
            "format_version": _EXPORT_FORMAT_VERSION,
            "application_version": __version__,
            "exported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "user_id": str(user_id),
            "schema": _export_schema(),
            "integrity": {
                "algorithm": "sha256",
                "scope": _EXPORT_INTEGRITY_SCOPE,
            },
        }
        stream.write(_export_line(_EXPORT_HEADER_TYPE, header) + "\n")
        digest = hashlib.sha256()
        counts = {record_type: 0 for record_type in _RECORD_SPECS}
        written = 0
        for record_type, row in _export_rows(conn, user_id):
            line = _export_line(record_type, row) + "\n"
            if credential_findings is not None and record_type == "memory" and isinstance(row, Mapping):
                # The header is line 1, so this record lands on line written + 2.
                finding = _memory_record_credential_finding(row, line_no=written + 2)
                if finding is not None:
                    credential_findings.append(finding)
            stream.write(line)
            digest.update(line.encode("utf-8"))
            counts[record_type] += 1
            written += 1

        footer = {
            "format": _EXPORT_FORMAT,
            "format_version": _EXPORT_FORMAT_VERSION,
            "record_count": written,
            "record_counts": counts,
            "sha256": digest.hexdigest(),
        }
        stream.write(_export_line(_EXPORT_FOOTER_TYPE, footer) + "\n")
        return written


def _run_export(args: argparse.Namespace) -> int:
    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    if not db_path.exists():
        _emit_error("export_source_not_found")
        return 1
    if args.out is not None:
        requested_out_path = Path(args.out).expanduser()
        if _path_aliases_database_family(requested_out_path, db_path):
            _emit_error("export_path_conflict")
            return 1
        out_path = requested_out_path.resolve()
        temp_path: Path | None = None
        credential_findings: list[_CredentialFinding] = []
        try:
            _ensure_private_directory(out_path.parent)
            fd, raw_temp_path = tempfile.mkstemp(
                prefix=f".{out_path.name}.",
                suffix=".tmp",
                dir=out_path.parent,
                text=True,
            )
            temp_path = Path(raw_temp_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                os.fchmod(stream.fileno(), 0o600)
                written = _write_export(
                    stream,
                    db_path=db_path,
                    user_id=args.user_id,
                    credential_findings=credential_findings,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, out_path)
            temp_path = None
            _fsync_directory(out_path.parent)
        except (
            _BackupError,
            OSError,
            sqlite3.Error,
            ContinuityStoreInvariantError,
        ) as exc:
            logger.debug(
                "SQLite export failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            _emit_error("export_failed")
            return 1
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        try:
            print(
                f"alice-memory: exported {written} records to {out_path}",
                file=sys.stderr,
                flush=True,
            )
            if credential_findings:
                for line in _credential_finding_lines(credential_findings):
                    print(line, file=sys.stderr, flush=True)
                print(
                    "alice-memory: warning: alice-memory import will refuse this export. Redact "
                    "the memories listed above in this vault (alice_memory_manage action=redact, "
                    "with ALICE_MCP_FULL_TOOLS=1), then export again.",
                    file=sys.stderr,
                    flush=True,
                )
        except (OSError, ValueError):
            return 2
    else:
        stdout_findings: list[_CredentialFinding] = []
        try:
            _write_export(sys.stdout, db_path=db_path, user_id=args.user_id, credential_findings=stdout_findings)
            for line in _credential_finding_lines(stdout_findings):
                _stderr_line(line)
            if stdout_findings:
                _stderr_line(
                    "alice-memory: warning: alice-memory import will refuse this export. Redact "
                    "the memories listed above in this vault (alice_memory_manage action=redact, "
                    "with ALICE_MCP_FULL_TOOLS=1), then export again."
                )
        except (
            _BackupError,
            OSError,
            sqlite3.Error,
            ContinuityStoreInvariantError,
        ) as exc:
            logger.debug(
                "SQLite stdout export failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            _emit_error("export_failed")
            return 1
    return 0


# --- import ---------------------------------------------------------------------


class _ImportError(Exception):
    """A user-facing import failure; the message names the offending line."""


class _ImportCredentialError(_ImportError):
    """Memory records carry credential material. Reported under its own code,
    naming every offender's line and memory id, never the matched text."""

    def __init__(self, findings: Sequence["_CredentialFinding"]) -> None:
        super().__init__(f"{len(findings)} memory record(s) carry credential material")
        self.findings = tuple(findings)


@dataclass(frozen=True)
class _ValidatedImport:
    versioned: bool
    record_count: int
    record_counts: dict[str, int]
    content_sha256: str
    manifest_sha256: str
    spool_path: Path


def _create_import_spool(path: Path) -> tuple[Path, sqlite3.Connection]:
    """Create an owner-only, disk-backed spool beside the private snapshot."""
    spool_path = path.with_name("validated-import-spool.sqlite3")
    try:
        spool_path.unlink()
    except FileNotFoundError:
        pass
    spool: sqlite3.Connection | None = None
    try:
        spool = sqlite3.connect(spool_path)
        os.chmod(spool_path, 0o600)
        spool.execute("PRAGMA journal_mode=OFF")
        spool.execute("PRAGMA synchronous=OFF")
        spool.execute("PRAGMA temp_store=FILE")
        spool.execute("PRAGMA cache_size=-1024")
        spool.execute(
            """
            CREATE TABLE validated_records (
                record_type TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                line_no INTEGER NOT NULL,
                payload BLOB NOT NULL,
                PRIMARY KEY (record_type, ordinal)
            ) WITHOUT ROWID
            """
        )
    except (OSError, sqlite3.Error):
        if spool is not None:
            try:
                spool.close()
            except sqlite3.Error:
                pass
        _remove_sqlite_files(spool_path)
        raise
    assert spool is not None
    return spool_path, spool


def _iter_spooled_records(
    validated_import: _ValidatedImport,
    record_type: str,
) -> Iterator[tuple[int, dict[str, object]]]:
    """Stream validated records without decoding the source JSONL again."""
    spool = sqlite3.connect(
        f"{validated_import.spool_path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        cursor = spool.execute(
            """
            SELECT line_no, payload
            FROM validated_records
            WHERE record_type = ?
            ORDER BY ordinal
            """,
            (record_type,),
        )
        while rows := cursor.fetchmany(_EXPORT_FETCH_SIZE):
            for line_no, payload in rows:
                try:
                    record = marshal.loads(bytes(payload))
                except (EOFError, TypeError, ValueError) as exc:
                    raise _ImportError(
                        f"line {line_no}: validated import spool record is unreadable"
                    ) from exc
                if not isinstance(record, dict):
                    raise _ImportError(
                        f"line {line_no}: validated import spool record is malformed"
                    )
                yield int(line_no), record
    finally:
        spool.close()


def _decode_import_envelope(
    text: str, *, line_no: int
) -> tuple[str, dict[str, object]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _ImportError(f"line {line_no}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _ImportError(
            f"line {line_no}: expected a JSON object, got {type(payload).__name__}"
        )
    record_type = payload.get("record_type")
    record = payload.get("record")
    if not isinstance(record_type, str) or not isinstance(record, dict):
        raise _ImportError(
            f"line {line_no}: expected {{\"record_type\": str, \"record\": object}}"
        )
    return record_type, record


def _json_column(value: object) -> object:
    """A JSON column as the mapping or list it holds; text that is not JSON as text."""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


@dataclass(frozen=True)
class _CredentialFinding:
    line_no: int
    memory_id: str
    verdict: str
    fields: tuple[str, ...]


# Keys the product itself writes into a memory's metadata_json that the
# credential name rule would read as secret names. Enumerated from the vNext
# memory writers (a test walks them and fails on a new one), not guessed:
# a rollup card's rollup_key ("scope:<hex>:topic:<anchor>") blocked the
# restore of the product's own export (S4.4 round 3, P2 item 7). Their
# values are still read, by value.
SYSTEM_METADATA_KEYS = frozenset({"rollup_key"})


def _without_system_keys(value: object) -> object:
    """The mapping with each system key's value wrapped in a list, so the
    floor reads that value on its own and never as a keyed pair."""

    if isinstance(value, Mapping):
        return {
            key: [item] if key in SYSTEM_METADATA_KEYS and isinstance(item, str) else _without_system_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_system_keys(item) for item in value]
    return value


def _memory_record_credential_fields(record: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    """The fields of one memory record the floor reads, in reading order.

    Title and canonical text; the value column by value only (owner ruling
    C3: an importer's structural key over a digest has the same shape as a
    secret name over a key); metadata_json as a mapping, keyed, except the
    keys the product itself writes; then the identifiers memory_key and
    project_id. The summary is left out when it is a derived copy of the
    text (canonical_text[:N] or a "..." preview); a summary that says
    something else is read.
    """

    text = record.get("canonical_text")
    summary = record.get("summary")
    fields: list[tuple[str, object]] = [("title", record.get("title")), ("canonical_text", text)]
    if summary is not None and not is_derived_copy(summary, text):
        fields.append(("summary", summary))
    fields.extend(
        [
            ("value", string_values(_json_column(record.get("value")))),
            ("metadata_json", _without_system_keys(_json_column(record.get("metadata_json")))),
            ("memory_key", record.get("memory_key")),
            ("project_id", record.get("project_id")),
        ]
    )
    return tuple(fields)


def _memory_record_credential_finding(record: Mapping[str, object], *, line_no: int) -> _CredentialFinding | None:
    """The credential floor for one restored memory row, or None when it is clean.

    Until 2026-09-22 import restored a backup with no credential check, so a
    crafted export planted a secret as an active memory. Every memory record
    is checked whatever its status: a retired row's title and text can still
    be rendered through a supersession chain, and a correction's previous
    text stays in metadata_json. The finding names the fields, never the text.
    """

    fields = _memory_record_credential_fields(record)
    verdict = credential_verdict(*(value for _name, value in fields))
    if verdict is None:
        return None
    named = tuple(name for name, value in fields if value is not None and credential_verdict(value) is not None)
    return _CredentialFinding(
        line_no=line_no,
        memory_id=str(record.get("id")),
        verdict=verdict,
        fields=named or ("across fields",),
    )


def _credential_finding_lines(findings: Sequence[_CredentialFinding]) -> list[str]:
    lines = []
    for finding in findings:
        what = "expands too far under unicode normalisation" if finding.verdict == VERDICT_EXPANSION else (
            "carries credential material"
        )
        lines.append(
            f"alice-memory: line {finding.line_no}: memory {finding.memory_id} {what} "
            f"({', '.join(finding.fields)})"
        )
    return lines


_IMPORT_PROGRESS_EVERY = 10_000


def _stderr_line(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _validate_import_file(
    path: Path,
    *,
    progress: Callable[[str], None] | None = None,
    enforce_credential_floor: bool = True,
) -> _ValidatedImport:
    """Validate the complete file before opening or creating the target DB.

    Version 2 exports carry schema, counts, and a canonical SHA-256 footer;
    a missing footer therefore detects truncation. Legacy headerless JSONL
    remains accepted, but cannot offer an integrity guarantee it never had.

    ``enforce_credential_floor`` is false only for ``--quarantine``. That
    path still checks the footer on the file as given, then rewrites the
    named memory before deciding whether any memory row would still carry
    credential material. A normal import refuses here, before any write.
    """
    versioned: bool | None = None
    footer: dict[str, object] | None = None
    digest = hashlib.sha256()
    manifest_sha256 = ""
    counts = {record_type: 0 for record_type in _RECORD_SPECS}
    try:
        spool_path, spool = _create_import_spool(path)
    except (OSError, sqlite3.Error) as exc:
        raise _ImportError(f"could not create validated import spool: {exc}") from exc
    spool_complete = False
    record_count = 0
    credential_findings: list[_CredentialFinding] = []
    saw_nonblank = False
    export_user_id: str | None = None
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, start=1):
                text = line.strip()
                if not text:
                    if versioned:
                        raise _ImportError(
                            f"line {line_no}: blank lines are not allowed in a versioned export"
                        )
                    continue
                record_type, record = _decode_import_envelope(text, line_no=line_no)
                if not saw_nonblank:
                    saw_nonblank = True
                    versioned = record_type == _EXPORT_HEADER_TYPE
                    if versioned:
                        if record.get("format") != _EXPORT_FORMAT:
                            raise _ImportError(f"line {line_no}: unsupported export format")
                        if record.get("format_version") != _EXPORT_FORMAT_VERSION:
                            raise _ImportError(
                                f"line {line_no}: unsupported export format version "
                                f"{record.get('format_version')!r}"
                            )
                        try:
                            export_user_id = str(UUID(str(record.get("user_id") or "")))
                        except ValueError as exc:
                            raise _ImportError(
                                f"line {line_no}: export header has an invalid user_id"
                            ) from exc
                        if not isinstance(record.get("exported_at"), str) or not isinstance(
                            record.get("application_version"), str
                        ):
                            raise _ImportError(
                                f"line {line_no}: export header is missing version/timestamp metadata"
                            )
                        if record.get("schema") != _export_schema():
                            raise _ImportError(
                                f"line {line_no}: export schema is not supported by this Alice version"
                            )
                        integrity = record.get("integrity")
                        if not isinstance(integrity, dict) or integrity.get(
                            "algorithm"
                        ) != "sha256" or integrity.get("scope") not in {
                            _EXPORT_INTEGRITY_SCOPE,
                            _LEGACY_V2_INTEGRITY_SCOPE,
                        }:
                            raise _ImportError(
                                f"line {line_no}: unsupported export integrity declaration"
                            )
                        if integrity["scope"] == _LEGACY_V2_INTEGRITY_SCOPE:
                            digest.update(
                                (_export_line(_EXPORT_HEADER_TYPE, record) + "\n").encode(
                                    "utf-8"
                                )
                            )
                        manifest_sha256 = hashlib.sha256(
                            _export_line(_EXPORT_HEADER_TYPE, record).encode("utf-8")
                        ).hexdigest()
                        continue
                if record_type == _EXPORT_HEADER_TYPE:
                    raise _ImportError(f"line {line_no}: export header must be the first record")
                if record_type == _EXPORT_FOOTER_TYPE:
                    if not versioned:
                        raise _ImportError(
                            f"line {line_no}: export footer appeared without a versioned header"
                        )
                    if footer is not None:
                        raise _ImportError(f"line {line_no}: duplicate export footer")
                    footer = record
                    continue
                if footer is not None:
                    raise _ImportError(f"line {line_no}: data found after export footer")
                if record_type not in _RECORD_SPECS:
                    known = ", ".join(_RECORD_SPECS)
                    raise _ImportError(
                        f"line {line_no}: unknown record_type '{record_type}' (known: {known})"
                    )
                if not str(record.get("id") or "").strip():
                    raise _ImportError(
                        f"line {line_no}: {record_type} record is missing an 'id'"
                    )
                expected_columns = set(_RECORD_SPECS[record_type][1])
                actual_columns = set(record)
                if versioned:
                    if actual_columns != expected_columns:
                        missing = sorted(expected_columns - actual_columns)
                        unknown = sorted(actual_columns - expected_columns)
                        raise _ImportError(
                            f"line {line_no}: {record_type} fields do not match the "
                            f"declared schema (missing={missing}, unknown={unknown})"
                        )
                    if str(record.get("user_id")) != export_user_id:
                        raise _ImportError(
                            f"line {line_no}: {record_type} belongs to a different export user"
                        )
                else:
                    unknown_columns = sorted(actual_columns - expected_columns)
                    if unknown_columns:
                        raise _ImportError(
                            f"line {line_no}: legacy {record_type} has unknown fields that "
                            f"this Alice version cannot restore: {unknown_columns}"
                        )
                if record_type == "memory":
                    finding = _memory_record_credential_finding(record, line_no=line_no)
                    if finding is not None:
                        credential_findings.append(finding)
                if progress is not None and (record_count + 1) % _IMPORT_PROGRESS_EVERY == 0:
                    progress(f"alice-memory: validated {record_count + 1} records")
                counts[record_type] += 1
                spool.execute(
                    """
                    INSERT INTO validated_records (record_type, ordinal, line_no, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        record_type,
                        counts[record_type],
                        line_no,
                        sqlite3.Binary(marshal.dumps(record)),
                    ),
                )
                record_count += 1
                digest.update((_export_line(record_type, record) + "\n").encode("utf-8"))
        if not saw_nonblank:
            raise _ImportError("file is empty")
        if versioned:
            if footer is None:
                raise _ImportError("versioned export is truncated: integrity footer is missing")
            if footer.get("format") != _EXPORT_FORMAT or footer.get(
                "format_version"
            ) != _EXPORT_FORMAT_VERSION:
                raise _ImportError("export integrity footer has an unsupported format or version")
            if footer.get("record_count") != record_count:
                raise _ImportError("export integrity record count does not match its contents")
            if footer.get("record_counts") != counts:
                raise _ImportError("export integrity per-type counts do not match its contents")
            if footer.get("sha256") != digest.hexdigest():
                raise _ImportError("export integrity SHA-256 does not match its contents")
        if enforce_credential_floor and credential_findings:
            # Every offender in one pass, so a user fixes the vault once.
            raise _ImportCredentialError(credential_findings)
        spool.commit()
        spool.close()
        spool_complete = True
    except UnicodeDecodeError as exc:
        raise _ImportError(f"file is not valid UTF-8 JSONL: {exc}") from exc
    except sqlite3.Error as exc:
        raise _ImportError(f"could not write validated import spool: {exc}") from exc
    finally:
        if not spool_complete:
            try:
                spool.close()
            except sqlite3.Error:
                pass
            _remove_sqlite_files(spool_path)

    return _ValidatedImport(
        versioned=bool(versioned),
        record_count=record_count,
        record_counts=counts,
        content_sha256=digest.hexdigest(),
        manifest_sha256=manifest_sha256,
        spool_path=spool_path,
    )


def _quarantine_arg(value: str) -> tuple[str, ...]:
    """Parse ``--quarantine id[,id...]`` without reading the import file."""
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or any(not part for part in parts):
        raise argparse.ArgumentTypeError(
            "--quarantine needs one or more memory ids, separated by commas"
        )
    return parts


def _normalized_quarantine_ids(raw: object) -> tuple[str, ...]:
    """Dedupe ids, preserving the order the owner typed them."""
    if raw is None:
        return ()
    parts = raw if isinstance(raw, tuple) else _quarantine_arg(str(raw))
    ordered: list[str] = []
    for part in parts:
        if part not in ordered:
            ordered.append(part)
    return tuple(ordered)


@dataclass(frozen=True)
class _QuarantinePlan:
    """What one import must rewrite, and which shared copies it only reports."""

    ids: frozenset[str]
    exclusive_entity_ids: frozenset[str]
    successor_ids: frozenset[str]
    reports: tuple[tuple[str, str, str], ...]


_EMPTY_QUARANTINE_PLAN = _QuarantinePlan(frozenset(), frozenset(), frozenset(), ())


def _redact_text(value: object) -> object:
    if isinstance(value, str):
        return _QUARANTINE_PLACEHOLDER
    return value


def _quarantine_json_object(value: object) -> object:
    """Replace a present JSON value. NULL stays NULL."""
    if value is None:
        return None
    return dict(_QUARANTINE_JSON_OBJECT)


def _event_belongs_to_quarantine(record: dict[str, object], quarantine_ids: frozenset[str]) -> bool:
    """True when an event is about one of the named memories.

    Membership is the memory target, or a payload ``memory_id``,
    ``candidate_memory_id``, or ``replacement_memory_id`` equal to one of
    those ids. The check reads the file record, before the payload is replaced.
    """
    if str(record.get("target_type") or "") == "memory" and str(record.get("target_id") or "") in quarantine_ids:
        return True
    payload = record.get("payload_json")
    if isinstance(payload, dict):
        for key in _QUARANTINE_EVENT_LINK_KEYS:
            if str(payload.get(key) or "") in quarantine_ids:
                return True
    return False


def _edge_endpoints(record: dict[str, object]) -> tuple[tuple[str, str], tuple[str, str]]:
    return (
        (str(record.get("from_type") or ""), str(record.get("from_id") or "")),
        (str(record.get("to_type") or ""), str(record.get("to_id") or "")),
    )


def _edge_touches_quarantine(record: dict[str, object], quarantine_ids: frozenset[str]) -> bool:
    return any(kind == "memory" and endpoint in quarantine_ids for kind, endpoint in _edge_endpoints(record))


def _memory_entity_link(record: dict[str, object]) -> tuple[str, str] | None:
    entity_id = ""
    memory_id = ""
    for kind, endpoint in _edge_endpoints(record):
        if not endpoint:
            continue
        if kind == "entity":
            entity_id = endpoint
        elif kind == "memory":
            memory_id = endpoint
    if entity_id and memory_id:
        return entity_id, memory_id
    return None


def _metadata_pointer(record: dict[str, object], key: str) -> str:
    metadata = record.get("metadata_json")
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get(key)
    return value if isinstance(value, str) else ""


def _build_quarantine_plan(
    validated_import: _ValidatedImport,
    quarantine_ids: tuple[str, ...],
) -> _QuarantinePlan:
    """Classify derived rows before insert. Shared chunks and entities stay."""
    ids = frozenset(quarantine_ids)
    if not ids:
        return _EMPTY_QUARANTINE_PLAN
    memories = [record for _line_no, record in _iter_spooled_records(validated_import, "memory")]
    edges = [record for _line_no, record in _iter_spooled_records(validated_import, "graph_edge")]
    links = [record for _line_no, record in _iter_spooled_records(validated_import, "provenance_link")]

    successors: set[str] = set()
    for record in memories:
        memory_id = str(record.get("id") or "")
        supersedes_ids = {
            pointer
            for pointer in (
                str(record.get("supersedes") or ""),
                _metadata_pointer(record, "supersedes"),
            )
            if pointer
        }
        if memory_id and memory_id not in ids and supersedes_ids & ids:
            successors.add(memory_id)
        if memory_id in ids:
            for successor_id in (
                str(record.get("superseded_by") or ""),
                _metadata_pointer(record, "superseded_by"),
            ):
                if successor_id and successor_id not in ids:
                    successors.add(successor_id)

    linked: dict[str, set[str]] = {}
    for edge in edges:
        pair = _memory_entity_link(edge)
        if pair is None:
            continue
        entity_id, memory_id = pair
        linked.setdefault(entity_id, set()).add(memory_id)
    exclusive = frozenset(
        entity_id for entity_id, memory_ids in linked.items() if memory_ids and memory_ids <= ids
    )
    shared_entities = frozenset(
        entity_id for entity_id, memory_ids in linked.items() if memory_ids & ids and not memory_ids <= ids
    )

    chunk_targets: dict[str, set[str]] = {}
    for link in links:
        if str(link.get("target_type") or "") != "memory":
            continue
        chunk_id = str(link.get("source_chunk_id") or "")
        target_id = str(link.get("target_id") or "")
        if chunk_id and target_id:
            chunk_targets.setdefault(chunk_id, set()).add(target_id)
    shared_chunks = frozenset(
        chunk_id
        for chunk_id, targets in chunk_targets.items()
        if targets & ids and not targets <= ids
    )

    reports: list[tuple[str, str, str]] = []
    for chunk_id in sorted(shared_chunks):
        reports.append(("source_chunks", chunk_id, "text"))
    for entity_id in sorted(shared_entities):
        for column in ("aliases", "name", "normalized_name"):
            reports.append(("vnext_entities", entity_id, column))
    reports.sort()
    return _QuarantinePlan(
        ids=ids,
        exclusive_entity_ids=exclusive,
        successor_ids=frozenset(successors),
        reports=tuple(reports),
    )


def _redact_quarantined_memory(record: dict[str, object]) -> dict[str, object]:
    redacted = dict(record)
    memory_id = str(record.get("id") or "")
    # Rejected rows are outside recall, resume, and context packs.
    redacted["status"] = "rejected"
    redacted["memory_key"] = f"quarantined.{memory_id}"
    # Cleared, matching product redaction, so the old idempotency key can
    # create a fresh row through the normal checks.
    redacted["commit_digest"] = None
    for field in _QUARANTINE_TEXT_FIELDS:
        redacted[field] = _redact_text(redacted.get(field))
    if isinstance(redacted.get("extracted_by_model"), str):
        redacted["extracted_by_model"] = _QUARANTINE_PLACEHOLDER
    redacted["value"] = dict(_QUARANTINE_JSON_OBJECT)
    redacted["metadata_json"] = dict(_QUARANTINE_JSON_OBJECT)
    return redacted


def _redact_quarantined_revision(record: dict[str, object]) -> dict[str, object]:
    redacted = dict(record)
    redacted["memory_key"] = f"quarantined.{record.get('memory_id') or ''}"
    for field in _QUARANTINE_REVISION_TEXT_FIELDS:
        redacted[field] = _redact_text(redacted.get(field))
    for field in _QUARANTINE_REVISION_JSON_FIELDS:
        if field in redacted:
            redacted[field] = _quarantine_json_object(redacted.get(field))
    return redacted


def _redact_quarantined_event(
    record: dict[str, object],
    quarantine_ids: frozenset[str],
) -> dict[str, object]:
    redacted = dict(record)
    payload = redacted.get("payload_json")
    kept: dict[str, str] = {}
    if isinstance(payload, dict):
        for key in _QUARANTINE_EVENT_KEPT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value in quarantine_ids:
                kept[key] = value
    redacted["payload_json"] = {"quarantined": True, **kept}
    # integrity_hash is a SHA-256 of the event record, including the payload.
    # A reconstructed original payload could be checked against a kept hash.
    redacted["integrity_hash"] = None
    return redacted


def _replace_successor_fields(value: object) -> tuple[object, int]:
    """Replace copied successor fields. Leave every other key in place."""
    if isinstance(value, dict):
        replaced = 0
        rewritten: dict[str, object] = {}
        for key, item in value.items():
            if key in _SUCCESSOR_COPIED_FIELDS and isinstance(item, str):
                rewritten[key] = _QUARANTINE_PLACEHOLDER
                replaced += 1
            else:
                nested, nested_count = _replace_successor_fields(item)
                rewritten[key] = nested
                replaced += nested_count
        return rewritten, replaced
    if isinstance(value, list):
        replaced = 0
        items: list[object] = []
        for item in value:
            nested, nested_count = _replace_successor_fields(item)
            items.append(nested)
            replaced += nested_count
        return items, replaced
    return value, 0


def _replace_quarantined_rollup_instances(
    value: object,
    quarantine_ids: frozenset[str],
) -> tuple[object, int]:
    """Replace rollup instance entries that belong to a quarantined memory."""
    if not isinstance(value, dict):
        return value, 0
    rollup = value.get("rollup")
    if not isinstance(rollup, dict):
        return value, 0
    instances = rollup.get("instances")
    if not isinstance(instances, list):
        return value, 0
    replaced = 0
    rewritten_instances: list[object] = []
    for item in instances:
        memory_id = item.get("memory_id") if isinstance(item, dict) else None
        if isinstance(memory_id, str) and memory_id in quarantine_ids:
            rewritten_instances.append({"quarantined": True, "memory_id": memory_id})
            replaced += 1
        else:
            rewritten_instances.append(item)
    if replaced == 0:
        return value, 0
    rewritten = dict(value)
    rewritten_rollup = dict(rollup)
    rewritten_rollup["instances"] = rewritten_instances
    rewritten["rollup"] = rewritten_rollup
    return rewritten, replaced


def _apply_import_quarantine(
    record_type: str,
    record: dict[str, object],
    plan: _QuarantinePlan,
) -> tuple[dict[str, object], dict[str, int]]:
    """Return the row to insert and the receipt counts that row adds."""
    if not plan.ids:
        return record, {}
    if record_type == "memory":
        memory_id = str(record.get("id") or "")
        if memory_id in plan.ids:
            return _redact_quarantined_memory(record), {"memory": 1}
        rewritten = dict(record)
        counts: dict[str, int] = {}
        if memory_id in plan.successor_ids:
            metadata, replaced = _replace_successor_fields(rewritten.get("metadata_json"))
            if replaced:
                rewritten["metadata_json"] = metadata
                counts["successor"] = 1
        new_value, instance_count = _replace_quarantined_rollup_instances(rewritten.get("value"), plan.ids)
        if instance_count:
            rewritten["value"] = new_value
            counts["rollup_instance"] = instance_count
        return (rewritten if counts else record), counts
    if record_type == "memory_revision" and str(record.get("memory_id") or "") in plan.ids:
        return _redact_quarantined_revision(record), {"memory_revision": 1}
    if record_type == "event" and _event_belongs_to_quarantine(record, plan.ids):
        return _redact_quarantined_event(record, plan.ids), {"event": 1}
    if (
        record_type == "provenance_link"
        and str(record.get("target_type") or "") == "memory"
        and str(record.get("target_id") or "") in plan.ids
        and isinstance(record.get("quote"), str)
    ):
        redacted = dict(record)
        redacted["quote"] = _QUARANTINE_PLACEHOLDER
        return redacted, {"provenance_link": 1}
    if record_type == "open_loop" and str(record.get("memory_id") or "") in plan.ids:
        redacted = dict(record)
        for field in ("title", "description", "resolution_note"):
            if isinstance(redacted.get(field), str):
                redacted[field] = _QUARANTINE_PLACEHOLDER
        return redacted, {"open_loop": 1}
    if record_type == "graph_edge" and _edge_touches_quarantine(record, plan.ids):
        redacted = dict(record)
        if isinstance(redacted.get("explanation"), str):
            redacted["explanation"] = _QUARANTINE_PLACEHOLDER
        redacted["metadata_json"] = dict(_QUARANTINE_JSON_OBJECT)
        return redacted, {"graph_edge": 1}
    if record_type == "entity" and str(record.get("id") or "") in plan.exclusive_entity_ids:
        redacted = dict(record)
        redacted["name"] = _QUARANTINE_PLACEHOLDER
        redacted["normalized_name"] = f"quarantined.{record.get('id') or ''}"
        redacted["aliases"] = []
        return redacted, {"entity": 1}
    return record, {}


def _credential_findings_after_quarantine(
    validated_import: _ValidatedImport,
    plan: _QuarantinePlan,
) -> tuple[_CredentialFinding, ...]:
    """S4.4's memory check on each memory as quarantine will store it.

    A named memory, a rollup instance, and a successor's copied fields are
    rewritten first. A memory that still carries credential material after
    that rewrite is refused, and nothing is written. Shared chunks and
    shared entity names are not memories, so they are not refused here.
    """

    findings: list[_CredentialFinding] = []
    for line_no, record in _iter_spooled_records(validated_import, "memory"):
        rewritten, _added = _apply_import_quarantine("memory", record, plan)
        finding = _memory_record_credential_finding(rewritten, line_no=line_no)
        if finding is not None:
            findings.append(finding)
    return tuple(findings)


def _column_replaced_by_quarantine(value: object) -> bool:
    """True when quarantine replaced this column with the placeholder or the fixed object."""

    if value == _QUARANTINE_PLACEHOLDER:
        return True
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return False
    return isinstance(decoded, Mapping) and dict(decoded) == _QUARANTINE_JSON_OBJECT


def _quarantine_text_column(value: object) -> bool:
    """A stored text or JSON column the post-import scan should read."""

    if value is None or isinstance(value, (bool, int, float)):
        return False
    if _column_replaced_by_quarantine(value):
        return False
    return isinstance(value, (str, Mapping, list, tuple))


def _quarantine_credential_reports(
    validated_import: _ValidatedImport,
    plan: _QuarantinePlan,
) -> tuple[tuple[str, str, str], ...]:
    """Leftover text columns ``credential_verdict`` still flags, as table, id, column.

    Runs on the row quarantine will store. Columns replaced by the placeholder
    or ``{"quarantined": true}`` are not passed to the detector. The matched
    text is not returned. Shared source chunks and shared entity names are
    scanned here too; the receipt already lists those from the plan, so a hit
    on one of them is not repeated.
    """

    if not plan.ids:
        return ()
    already = set(plan.reports)
    found: list[tuple[str, str, str]] = []
    for record_type, (table, columns) in _RECORD_SPECS.items():
        for _line_no, record in _iter_spooled_records(validated_import, record_type):
            rewritten, _added = _apply_import_quarantine(record_type, record, plan)
            row_id = str(rewritten.get("id") or "")
            for column in columns:
                value = rewritten.get(column)
                if not _quarantine_text_column(value):
                    continue
                if credential_verdict(value) is None:
                    continue
                item = (table, row_id, column)
                if item in already:
                    continue
                already.add(item)
                found.append(item)
    found.sort()
    return tuple(found)


def _missing_quarantine_memory_ids(
    validated_import: _ValidatedImport,
    quarantine_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Ids the owner named that are not memory records in the validated file."""
    if not quarantine_ids:
        return ()
    wanted = frozenset(quarantine_ids)
    found: set[str] = set()
    for _line_no, record in _iter_spooled_records(validated_import, "memory"):
        memory_id = str(record.get("id") or "")
        if memory_id in wanted:
            found.add(memory_id)
    return tuple(memory_id for memory_id in quarantine_ids if memory_id not in found)


def _encode_column_value(column: str, value: object) -> object:
    """TEXT-encode JSON columns the way the store writes them; pass the rest."""
    if column in _JSON_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(json_safe(value), ensure_ascii=False, separators=(",", ":"))
    return value


def _normalized_import_values(
    store: SQLiteVNextStore,
    columns: tuple[str, ...],
    record: dict[str, object],
) -> tuple[object, ...]:
    return tuple(
        store.user_id
        if column == "user_id"
        else _encode_column_value(column, record.get(column))
        for column in columns
    )


def _collision_is_identical(
    existing: dict[str, object],
    columns: tuple[str, ...],
    expected: tuple[object, ...],
) -> bool:
    for column, expected_value in zip(columns, expected):
        existing_value = existing.get(column)
        if column in _JSON_COLUMNS:
            try:
                existing_json = (
                    json.loads(existing_value)
                    if isinstance(existing_value, str)
                    else existing_value
                )
                expected_json = (
                    json.loads(expected_value)
                    if isinstance(expected_value, str)
                    else expected_value
                )
            except json.JSONDecodeError:
                return False
            if json_safe(existing_json) != json_safe(expected_json):
                return False
        elif existing_value != expected_value:
            return False
    return True


def _import_records(
    conn: sqlite3.Connection,
    store: SQLiteVNextStore,
    validated_import: _ValidatedImport,
    *,
    mode: str,
    plan: _QuarantinePlan | None = None,
    quarantine_tally: dict[str, int] | None = None,
) -> dict[str, dict[str, int]]:
    """Insert parsed records in FK-safe order; returns per-type counts.

    Direct INSERT (not the store ``create_*`` methods) so ids and
    timestamps land exactly as exported and no fresh mutation events are
    appended. Event rows use the same direct path, preserving occurred_at
    and integrity_hash text exactly, except events quarantined with a
    memory: those payloads are replaced and their integrity hash is
    cleared, because it is a SHA-256 of the event record, including the
    payload. ``user_id`` is rebound to the importing user. ``skip`` accepts
    only field-for-field identical collisions; divergent content with the
    same id is never merged. A second import of the same file with the
    same ``--quarantine`` ids therefore skips the redacted rows. Raises
    ``_ImportError`` on the first collision in ``fail`` mode and on any
    constraint violation; the staged transaction rolls back on failure.
    Quarantine replacement happens after the file's SHA-256 check, on the
    row about to be inserted, and is what ``skip`` compares.
    """
    quarantine_plan = plan if plan is not None else _EMPTY_QUARANTINE_PLAN
    counts: dict[str, dict[str, int]] = {}
    for record_type, (table, columns) in _RECORD_SPECS.items():
        for line_no, record in _iter_spooled_records(validated_import, record_type):
            tally = counts.setdefault(record_type, {"imported": 0, "skipped": 0})
            record, added = _apply_import_quarantine(record_type, record, quarantine_plan)
            if quarantine_tally is not None:
                for key, amount in added.items():
                    quarantine_tally[key] = quarantine_tally.get(key, 0) + amount
            row_id = str(record["id"])
            existing = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?", (row_id,)
            ).fetchone()
            values = _normalized_import_values(store, columns, record)
            if existing is not None:
                if mode == "fail":
                    raise _ImportError(
                        f"line {line_no}: {record_type} id {row_id} already exists; "
                        "aborting (--mode fail). Rerun with --mode skip to keep "
                        "existing rows and import only new records."
                    )
                if not _collision_is_identical(dict(existing), columns, values):
                    raise _ImportError(
                        f"line {line_no}: {record_type} id {row_id} has the same id "
                        "but different content; refusing to combine incompatible backups"
                    )
                tally["skipped"] += 1
                continue
            try:
                conn.execute(
                    f"""
                    INSERT INTO {table} ({", ".join(columns)})
                    VALUES ({", ".join("?" for _ in columns)})
                    """,
                    values,
                )
            except (
                sqlite3.Error,
                ContinuityStoreInvariantError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                raise _ImportError(
                    f"line {line_no}: {record_type} {row_id} could not be imported: {exc}"
                ) from exc
            tally["imported"] += 1
    return counts


def _count_phrase(count: int, singular: str, plural: str) -> str:
    noun = singular if count == 1 else plural
    return f"{count} {noun}"


def _quarantine_removal_line(table: str, row_id: str) -> str:
    """The command that removes this leftover, or the fixed no-command line.

    Shared source chunks and shared entity names have no removal command.
    ``alicebot vnext memories redact`` keeps source and source-chunk text.
    """
    if table == "memories":
        return f"alicebot vnext memories redact {row_id}"
    return _NO_QUARANTINE_REMOVAL_COMMAND


def _print_import_summary(
    counts: dict[str, dict[str, int]],
    *,
    in_path: Path,
    db_path: Path,
    quarantine_ids: tuple[str, ...] = (),
    quarantine_counts: dict[str, int] | None = None,
    quarantine_reports: tuple[tuple[str, str, str], ...] = (),
) -> None:
    imported_total = sum(tally["imported"] for tally in counts.values())
    skipped_total = sum(tally["skipped"] for tally in counts.values())
    print(
        f"alice-memory: imported {imported_total} records from {in_path} "
        f"into {db_path} ({skipped_total} skipped)"
    )
    for record_type in _RECORD_SPECS:
        tally = counts.get(record_type)
        if tally is None:
            continue
        print(f"  {record_type}: {tally['imported']} imported, {tally['skipped']} skipped")
    if quarantine_ids:
        tallies = quarantine_counts or {}
        print(
            "quarantine: "
            + ", ".join(
                _count_phrase(tallies.get(key, 0), singular, plural)
                for key, singular, plural in _QUARANTINE_COUNT_LABELS
            )
        )
        print("quarantined memory ids: " + ", ".join(quarantine_ids))
        for table, row_id, column in quarantine_reports:
            print(f"quarantine report: {table} {row_id} {column}")
            print(_quarantine_removal_line(table, row_id))
    memories_imported = counts.get("memory", {}).get("imported", 0)
    if memories_imported:
        plural = "memory" if memories_imported == 1 else "memories"
        print(f"note: {memories_imported} {plural} {_EMBEDDING_NOTE}")


@contextmanager
def _immutable_import_copy(source_path: Path) -> Iterator[Path]:
    """Yield an owner-only snapshot read from one stable source handle."""
    with tempfile.TemporaryDirectory(prefix="alice-memory-import-snapshot-") as raw_dir:
        snapshot_dir = Path(raw_dir)
        os.chmod(snapshot_dir, 0o700)
        snapshot_path = snapshot_dir / "import.jsonl"
        with source_path.open("rb") as source, snapshot_path.open("xb") as destination:
            before = os.fstat(source.fileno())
            shutil.copyfileobj(source, destination, length=_FILE_COPY_CHUNK_SIZE)
            after = os.fstat(source.fileno())
            destination.flush()
            os.fsync(destination.fileno())
        stable_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if stable_before != stable_after:
            raise _ImportError("import file changed while it was being snapshotted")
        os.chmod(snapshot_path, 0o600)
        yield snapshot_path


def _run_import(args: argparse.Namespace) -> int:
    requested_in_path = Path(args.in_path).expanduser()
    if not requested_in_path.exists():
        _emit_error("import_source_not_found")
        return 1
    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    if _path_aliases_database_family(requested_in_path, db_path):
        _emit_error("import_path_conflict")
        return 1
    display_path = requested_in_path.resolve()
    try:
        with _immutable_import_copy(display_path) as in_path:
            return _run_import_snapshot(
                args,
                in_path=in_path,
                display_path=display_path,
                db_path=db_path,
            )
    except (_ImportError, OSError) as exc:
        logger.debug(
            "SQLite import snapshot failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_error("import_snapshot_failed")
        return 1


def _run_import_snapshot(
    args: argparse.Namespace,
    *,
    in_path: Path,
    display_path: Path,
    db_path: Path,
) -> int:
    # Quarantine rewrites the named memory after the footer check. The
    # credential floor still refuses any memory that would be stored with
    # credential material, judged on the rewritten row. Other imports
    # refuse during validation, before the spool is committed.
    quarantine_ids = _normalized_quarantine_ids(getattr(args, "quarantine", None))
    try:
        _stderr_line(f"alice-memory: validating {display_path}")
        validated_import = _validate_import_file(
            in_path,
            progress=_stderr_line,
            enforce_credential_floor=not quarantine_ids,
        )
    except _ImportCredentialError as exc:
        # User-visible, on stderr: the line and memory id of every offender
        # and which fields carried it. Never the matched text.
        for line in _credential_finding_lines(exc.findings):
            _stderr_line(line)
        _emit_error("import_credential_material")
        return 1
    except _ImportError as exc:
        logger.debug(
            "SQLite import validation failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_error("import_validation_failed")
        return 1
    except OSError as exc:
        logger.debug(
            "SQLite import read failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_error("import_snapshot_failed")
        return 1

    # The footer check above hashed the file as given. Quarantine replacement
    # starts here, and only for ids that are memory records in that file.
    try:
        missing_quarantine_ids = _missing_quarantine_memory_ids(validated_import, quarantine_ids)
    except _ImportError as exc:
        logger.debug(
            "SQLite import quarantine check failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _remove_sqlite_files(validated_import.spool_path)
        _emit_error("import_validation_failed")
        return 1
    if missing_quarantine_ids:
        logger.debug("quarantine ids not in the import file: %s", ", ".join(missing_quarantine_ids))
        _remove_sqlite_files(validated_import.spool_path)
        _emit_error("import_quarantine_unknown")
        return 1

    try:
        quarantine_plan = (
            _build_quarantine_plan(validated_import, quarantine_ids)
            if quarantine_ids
            else _EMPTY_QUARANTINE_PLAN
        )
        leftover_memory_findings = (
            _credential_findings_after_quarantine(validated_import, quarantine_plan) if quarantine_ids else ()
        )
    except _ImportError as exc:
        logger.debug(
            "SQLite import quarantine plan failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _remove_sqlite_files(validated_import.spool_path)
        _emit_error("import_validation_failed")
        return 1
    if leftover_memory_findings:
            # Same refusal as a normal import: the rewritten file would still
            # store a memory that carries credential material. Nothing is written.
            _remove_sqlite_files(validated_import.spool_path)
            for line in _credential_finding_lines(leftover_memory_findings):
                _stderr_line(line)
            _emit_error("import_credential_material")
            return 1

    target_existed = db_path.exists()
    working_path: Path | None = None
    credential_reports: tuple[tuple[str, str, str], ...] = ()
    try:
        _ensure_private_directory(db_path.parent)
        fd, raw_working_path = tempfile.mkstemp(
            prefix=f".{db_path.name}.restore.",
            suffix=".tmp",
            dir=db_path.parent,
        )
        os.close(fd)
        working_path = Path(raw_working_path)
        if target_existed:
            _copy_sqlite_database(db_path, working_path)
            staged_source = sqlite3.connect(working_path)
            try:
                _validate_alice_snapshot(staged_source)
            finally:
                staged_source.close()
        bootstrap_database(
            working_path,
            user_id=args.user_id,
            user_email=args.user_email,
            secure_parent=args.db is None,
        )
        quarantine_counts = {key: 0 for key, _singular, _plural in _QUARANTINE_COUNT_LABELS}
        with sqlite_user_connection(working_path, args.user_id) as conn:
            store = SQLiteVNextStore(conn, args.user_id)
            counts = _import_records(
                conn,
                store,
                validated_import,
                mode=args.mode,
                plan=quarantine_plan,
                quarantine_tally=quarantine_counts,
            )
        if quarantine_ids:
            # The spool still holds the file. Scan the rewritten rows before
            # publication deletes that spool. Never include the matched text.
            credential_reports = _quarantine_credential_reports(validated_import, quarantine_plan)
        # Move all committed WAL pages into the staged main file before
        # atomic publication, then durably persist it.
        checkpoint = sqlite3.connect(str(working_path))
        try:
            checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _validate_alice_snapshot(
                checkpoint,
                user_id=args.user_id,
                require_current_schema=True,
            )
        finally:
            checkpoint.close()
        with working_path.open("rb") as stream:
            os.fsync(stream.fileno())
        os.chmod(working_path, 0o600)
        if target_existed:
            # SQLite's backup API publishes the staged pages inside one
            # destination write transaction. Schema and data therefore
            # become visible together, or the original target rolls back.
            _publish_staged_database(working_path, db_path)
        else:
            # link() is an atomic no-clobber publish: if another process
            # created the requested target while validation/import ran, the
            # restore fails instead of replacing that new database.
            staged_path = working_path
            os.link(staged_path, db_path)
            # link() is the publication point. Cleanup is deliberately
            # best-effort after switching state so a failed temporary unlink
            # can never be misreported as a rolled-back restore.
            working_path = db_path
            _remove_sqlite_files(staged_path)
    except (_BackupError, _ImportError, OSError, sqlite3.Error) as exc:
        logger.debug(
            "SQLite restore failed before publication",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_error("restore_failed")
        return 1
    finally:
        if working_path is not None and working_path != db_path:
            _remove_sqlite_files(working_path)
        _remove_sqlite_files(validated_import.spool_path)
    # A new target is the already-fchmod(0600) staged inode published by
    # hard link, so no fallible post-publication chmod is needed. Existing
    # targets keep their inode and need an explicit owner-only hardening pass.
    # If that pass fails, return a distinct nonzero result without ever
    # claiming rollback: the imported records have committed.
    hardening_error: OSError | None = None
    if target_existed:
        try:
            _secure_sqlite_files(db_path)
        except OSError as exc:
            hardening_error = exc
            logger.debug(
                "SQLite restore committed but permission hardening failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
    _fsync_directory(db_path.parent)
    summary_error: OSError | ValueError | None = None
    try:
        _print_import_summary(
            counts,
            in_path=display_path,
            db_path=db_path,
            quarantine_ids=quarantine_ids,
            quarantine_counts=quarantine_counts,
            quarantine_reports=tuple(sorted({*quarantine_plan.reports, *credential_reports})),
        )
        sys.stdout.flush()
    except (OSError, ValueError) as exc:
        summary_error = exc
        logger.debug(
            "SQLite restore committed but summary output failed",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    if hardening_error is not None and summary_error is not None:
        _best_effort_stderr("restore_committed_hardening_and_summary_failed")
        return 2
    if hardening_error is not None:
        _best_effort_stderr("restore_committed_hardening_failed")
        return 2
    if summary_error is not None:
        _best_effort_stderr("restore_committed_summary_failed")
        return 2
    return 0


def _run_reindex_embeddings(args: argparse.Namespace) -> int:
    provider = get_embedding_provider()
    if provider is None:
        _emit_error("embedding_provider_not_configured")
        return 1
    batch_size = args.batch_size
    if batch_size < 1 or batch_size > MAX_EMBEDDINGS_BATCH_SIZE:
        _emit_error("embedding_batch_size_invalid")
        return 1

    db_path = resolve_db_path(data_dir=args.data_dir, db=args.db)
    bootstrap_database(
        db_path,
        user_id=args.user_id,
        user_email=args.user_email,
        secure_parent=args.db is None,
    )
    embedded = 0
    reindexed_incompatible = 0
    skipped = 0
    failed = 0
    batches = 0
    after_id: str | None = None
    while True:
        with sqlite_user_connection(db_path, args.user_id) as conn:
            store = SQLiteVNextStore(conn, args.user_id)
            rows = store.list_memories_missing_embeddings(
                limit=batch_size,
                after_id=after_id,
                embedding_provider=provider.provider,
                embedding_model=provider.model,
                embedding_endpoint=endpoint_fingerprint(
                    getattr(provider, "base_url", "")
                ),
                embedding_signature_version=EMBEDDING_SIGNATURE_VERSION,
            )
        if not rows:
            break
        batches += 1
        after_id = str(rows[-1]["id"])
        pending = [(row, memory_embedding_text(row)) for row in rows]
        embeddable = [(row, text) for row, text in pending if text]
        skipped += len(pending) - len(embeddable)
        if not embeddable:
            continue
        try:
            vectors = provider.embed_batch([text for _row, text in embeddable])
        except (VNextEmbeddingConfigurationError, VNextEmbeddingProviderError) as exc:
            failed += len(embeddable)
            logger.debug(
                "SQLite embedding batch failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            _emit_error("embedding_batch_failed")
            continue
        with sqlite_user_connection(db_path, args.user_id) as conn:
            store = SQLiteVNextStore(conn, args.user_id)
            for (row, _text), vector in zip(embeddable, vectors, strict=True):
                store.update_memory_embedding(
                    **signed_memory_embedding_update(row, vector, provider=provider)
                )
                if row.get("embedding_present") in (True, 1):
                    reindexed_incompatible += 1
                embedded += 1
    _secure_sqlite_files(db_path)
    print(
        json.dumps(
            {
                "provider": provider.provider,
                "model": provider.model,
                "batches": batches,
                "embedded": embedded,
                "reindexed_incompatible": reindexed_incompatible,
                "skipped": skipped,
                "failed": failed,
            },
            sort_keys=True,
        )
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    parser_stderr = StringIO()
    try:
        with redirect_stderr(parser_stderr):
            args = parser.parse_args(_normalized_argv(raw_argv))
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        logger.debug("alice-memory argument parsing failed: %s", parser_stderr.getvalue().strip())
        _emit_error("invalid_request")
        return int(exc.code) if isinstance(exc.code, int) else 2
    if _refuse_postgres_db_argument(args):
        return 2
    try:
        if args.command == "export":
            return _run_export(args)
        if args.command == "import":
            return _run_import(args)
        if args.command == "reindex-embeddings":
            return _run_reindex_embeddings(args)
        if args.command == "brief":
            return _run_brief(args)
        if args.command == "doctor":
            return _run_doctor(args)
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "sleep":
            return _run_sleep(args)
        if args.command == "sleep-proposals":
            return _run_sleep_proposals(args)
        if args.command == "install":
            return _run_install(args)
        return _run_mcp(args)
    except Exception as exc:  # pragma: no cover - boundary fail-closed backstop
        logger.debug(
            "Unhandled alice-memory command failure",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_error("alice_memory_failed")
        return 1


__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_DEMO_DATA_DIR",
    "bootstrap_database",
    "build_parser",
    "main",
    "resolve_db_path",
    "sqlite_url_for_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
