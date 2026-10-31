"""Process-local resident copy of the chunk vectors, for the vector stage of passage recall (SQLite only).

The memory cache (``vector_scan``) holds 1,536 wide float32 vectors, is stamped by ``embedding_stamp`` and, above its
cap, falls back to a scan that streams every blob out of SQLite. None of that fits chunk vectors: they are stored at
the model's own width, a re-embed of memories must not rebuild them, and a stateless scan of every blob on every
recall is the cost this cache exists to avoid. This module is its own cache:

* **Width.** The matrix is ``(rows, dimensions)`` float32 at the width the vectors were stored, one entry for each
  (database file, user), built for one signature (``ChunkSignature``). Rows of another signature, or of another
  width, are not loaded: only their source codes are kept, so a status can say that vectors exist but are stale.
* **Stamp.** The entry is valid while ``source_chunk_vector_stamp`` (``alice_schema_state``) is the token it was
  built at. A trigger of the vector table rewrites the token when a row is overwritten or deleted, by whatever path
  and from whatever process. Rows that were added are found by their row number and appended. Nothing here reads
  ``embedding_stamp``, so a memory re-embed leaves this cache alone.
* **The append peak.** The matrix, the norms and the source codes grow in place (``ndarray.resize``): on Linux with
  glibc and on macOS an append was measured not to hold a second copy of the matrix (an append holds at most one page
  of fetched rows beside the grown arrays). Other allocators were not measured. The matrix is made small and grown by
  ``ndarray.resize`` to the row count of the build before it is filled, because a block numpy allocates directly
  carries ``madvise(MADV_HUGEPAGE)`` advice on Linux, and a ``realloc`` of a block numpy advised copies it: the first
  append after such a build held the old and the new matrix at once (about one whole matrix at the peak).
* **Cap.** ``ALICE_SOURCE_VECTOR_CACHE_MAX_MB`` (default 512, about 170,000 vectors of 768 floats) limits the
  vector arrays of the copy (``resident_bytes``: the matrix, the norms and the source codes; the ids, the indexes and the
  temporary buffers of a build or a scan are not counted). Over it the cache refuses with ``over_cap`` and the vector stage reports that status and is
  skipped: it never reads the table row by row instead.
* **The mask.** What a caller may read is decided outside, by ``readable_source_ids``, and handed in per call as a
  set of source ids. The cache ranks only the rows of those sources, and counts only those rows, so a row the caller
  may not read cannot take a rank or change a count.
* **Scoped copies.** The user-wide copy above is for the owner and an unbound admin key. A caller with limits is served
  from a *scoped* copy that holds only the vectors of the sources that caller may read: its width is chosen, and its
  cap judged, over those rows and no others, so whether the stage runs, what it reports and what it ranks are
  functions of rows the caller may read. A scoped copy is keyed by the ids of those sources (a digest of the sorted
  list), so a relabel that changes what a caller may read is a different copy. It is valid while the *fingerprint* of
  its own sources is the one it was built at: a digest of the rewrite tokens (``source_chunk_vector_fingerprint``) that
  the triggers of the vector table keep for each source whose vector was overwritten or deleted, read for the sources
  of the copy and no others. A vector of a source the caller may not read, overwritten, deleted or added, neither
  invalidates the copy nor changes the work done for it; the vault-wide stamp is not read for it at all. A vector
  of a source the copy holds that is added, overwritten or deleted rebuilds it (it is not extended by row number, which
  can be reused after rows above it were deleted). At most
  ``max_scoped_copies()`` are kept for a user (default ``DEFAULT_SCOPED_COPIES``, ``ALICE_SOURCE_VECTOR_SCOPED_COPIES``),
  least recently used first out, each under its own cap (default ``DEFAULT_SCOPED_CACHE_MAX_MB``,
  ``ALICE_SOURCE_VECTOR_SCOPED_CACHE_MAX_MB``). There is no budget for the size of all of them together, and so no
  eviction by size: the rows of one scope, which another caller may not read, could otherwise decide whether that
  caller's copy is kept. With the defaults the sum of the limits for one user in one process is the limit of the
  user-wide copy plus the number of scoped copies times the limit of one (512 + 4 x 256 = 1,536 MB of vector arrays);
  the memory of the process is higher, because what ``resident_bytes`` does not count is not limited.

A reader holds the entry's lock for as long as it uses the view, so an append cannot change the arrays under it.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

import numpy as np

from alicebot_api.sqlite_schema import (
    SOURCE_CHUNK_VECTORS_TABLE,
    source_chunk_vector_fingerprint,
    source_chunk_vectors_stamp,
    source_chunk_vectors_table_exists,
)
from alicebot_api.source_embedding_policy import ChunkSignature
from alicebot_api.vnext_stores.sqlite.vector_scan import _resolve_database_file

CACHE_MAX_MB_ENV = "ALICE_SOURCE_VECTOR_CACHE_MAX_MB"
DEFAULT_CACHE_MAX_MB = 512
#: The cap of one scoped copy (a caller with limits), and how many of them are kept for one user.
SCOPED_CACHE_MAX_MB_ENV = "ALICE_SOURCE_VECTOR_SCOPED_CACHE_MAX_MB"
DEFAULT_SCOPED_CACHE_MAX_MB = 256
SCOPED_COPIES_ENV = "ALICE_SOURCE_VECTOR_SCOPED_COPIES"
DEFAULT_SCOPED_COPIES = 4
MAX_SCOPED_COPIES_SETTING = 64

#: Why the cache did not serve.
REFUSED_OVER_CAP = "over_cap"
REFUSED_UNAVAILABLE = "unavailable"

#: Rows read and decoded in one step of a build or an append, and rows scored in one step of a scan.
_ROWS_PER_STEP = 2048

#: Restricts a read of the vector table to the sources of a scope. One JSON array of ids is the only parameter.
_SCOPE_SQL = " AND source_id IN (SELECT value FROM json_each(?))"

_SIGNATURE_SQL = (
    "provider = ? AND model = ? AND endpoint = ? AND signature_version = ? "
    "AND header_version = ? AND prefix_version = ?"
)


class ChunkVectorCacheRefused(Exception):
    """The cache did not serve. ``reason`` is ``REFUSED_OVER_CAP`` or ``REFUSED_UNAVAILABLE``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _megabytes(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        megabytes = int(raw) if raw else default
    except ValueError:
        megabytes = default
    return max(megabytes, 0) * 1024 * 1024


def cache_max_bytes() -> int:
    """The cap of the user-wide copy (the owner's), ``ALICE_SOURCE_VECTOR_CACHE_MAX_MB``, 512 MB by default."""

    return _megabytes(CACHE_MAX_MB_ENV, DEFAULT_CACHE_MAX_MB)


def scoped_cache_max_bytes() -> int:
    """The cap of one scoped copy (a caller with limits), ``ALICE_SOURCE_VECTOR_SCOPED_CACHE_MAX_MB``, 256 MB by default."""

    return _megabytes(SCOPED_CACHE_MAX_MB_ENV, DEFAULT_SCOPED_CACHE_MAX_MB)


def max_scoped_copies() -> int:
    """How many scoped copies are kept for one user, ``ALICE_SOURCE_VECTOR_SCOPED_COPIES``, 4 by default (1 to 64)."""

    raw = os.environ.get(SCOPED_COPIES_ENV, "").strip()
    try:
        count = int(raw) if raw else DEFAULT_SCOPED_COPIES
    except ValueError:
        count = DEFAULT_SCOPED_COPIES
    return count if 1 <= count <= MAX_SCOPED_COPIES_SETTING else DEFAULT_SCOPED_COPIES


def _signature_params(signature: ChunkSignature) -> tuple[object, ...]:
    return (
        signature.provider,
        signature.model,
        signature.endpoint,
        signature.signature_version,
        signature.header_version,
        signature.prefix_version,
    )


class _Entry:
    __slots__ = (
        "lock",
        "signature",
        "stamp",
        "max_rowid",
        "dims",
        "matrix",
        "norms",
        "codes",
        "chunk_ids",
        "source_ids",
        "code_of",
        "rows_of_source",
        "stale_codes",
        "rebuilds",
        "appends",
    )

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        self.signature: ChunkSignature | None = None
        self.stamp: str | None = None
        self.max_rowid = 0
        self.dims = 0
        self.matrix: np.ndarray = np.empty((0, 0), dtype=np.float32)
        self.norms: np.ndarray = np.empty(0, dtype=np.float32)
        self.codes: np.ndarray = np.empty(0, dtype=np.int32)
        self.chunk_ids: list[str] = []
        self.source_ids: list[str] = []
        self.code_of: dict[str, int] = {}
        self.rows_of_source: dict[int, list[int]] = {}
        self.stale_codes: list[int] = []
        self.rebuilds = getattr(self, "rebuilds", 0)
        self.appends = getattr(self, "appends", 0)

    @property
    def rows(self) -> int:
        return len(self.chunk_ids)

    @property
    def resident_bytes(self) -> int:
        return int(self.matrix.nbytes + self.norms.nbytes + self.codes.nbytes)

    def code_for(self, source_id: str) -> int:
        code = self.code_of.get(source_id)
        if code is None:
            code = len(self.source_ids)
            self.code_of[source_id] = code
            self.source_ids.append(source_id)
        return code


_REGISTRY: dict[tuple[str, str], _Entry] = {}
_REGISTRY_LOCK = threading.Lock()


def clear_registry() -> None:
    """Forget every entry, scoped ones included (tests, and a process that wants the memory back)."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        _SCOPED.clear()


def entry_counters(database_file: str, user_id: str) -> dict[str, int]:
    """Builds, appends, rows, width and resident bytes of the user-wide copy, for the tests that prove a write did or did
    not rebuild it and for measuring it."""

    with _REGISTRY_LOCK:
        entry = _REGISTRY.get((database_file, str(user_id)))
    if entry is None:
        return {"rebuilds": 0, "appends": 0, "rows": 0, "dims": 0, "bytes": 0}
    return {
        "rebuilds": entry.rebuilds,
        "appends": entry.appends,
        "rows": entry.rows,
        "dims": entry.dims,
        "bytes": entry.resident_bytes,
    }


def scoped_entry_counters(database_file: str, user_id: str) -> dict[str, dict[str, int]]:
    """Builds, appends, rows, width and resident bytes of each scoped copy of one user, oldest first, by digest."""

    with _REGISTRY_LOCK:
        copies = list(_SCOPED.get((database_file, str(user_id)), OrderedDict()).items())
    return {
        digest: {
            "rebuilds": entry.rebuilds,
            "appends": entry.appends,
            "rows": entry.rows,
            "dims": entry.dims,
            "bytes": entry.resident_bytes,
        }
        for digest, entry in copies
    }


def _row_bytes(dims: int) -> int:
    return dims * 4 + 4 + 4


def _read_blob_into(row: np.ndarray, blob: object, dims: int) -> bool:
    vector = np.frombuffer(cast(bytes, blob), dtype=np.float32)
    if vector.size != dims:
        return False
    row[:] = vector
    return True


def _norms_of(block: np.ndarray) -> np.ndarray:
    norms = np.empty(block.shape[0], dtype=np.float32)
    for start in range(0, block.shape[0], _ROWS_PER_STEP):
        stop = min(start + _ROWS_PER_STEP, block.shape[0])
        norms[start:stop] = np.linalg.norm(block[start:stop], axis=1)
    return norms


def _query_rows(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
    cursor = conn.execute(sql, params)
    cursor.row_factory = None  # plain tuples: these reads can be of every row of the user
    return cursor


def _scope_parts(scope_json: str | None) -> tuple[str, tuple[object, ...]]:
    """The SQL that restricts a read of the vector table to a scope, and its parameter; nothing for the user-wide copy."""

    return ("", ()) if scope_json is None else (_SCOPE_SQL, (scope_json,))


def _rebuild(
    entry: _Entry,
    conn: sqlite3.Connection,
    user_id: str,
    signature: ChunkSignature,
    stamp: str | None,
    max_bytes: int,
    scope_json: str | None = None,
) -> None:
    """Read every vector that carries ``signature`` into one matrix, and the source codes of the rest.

    With ``scope_json`` (a JSON array of source ids) only the vectors of those sources are read: the width is the
    commonest width among them, and the cap is judged on their count, so the copy is a function of those rows alone.
    """

    scope_sql, scope_params = _scope_parts(scope_json)
    entry.reset()
    entry.rebuilds += 1
    # The signature and the stamp are set last: an entry that stopped half way matches nothing and is built again.
    if not source_chunk_vectors_table_exists(conn):
        entry.signature = signature
        entry.stamp = stamp
        return
    params = _signature_params(signature)
    cursor = _query_rows(conn, f"SELECT COALESCE(MAX(rowid), 0) FROM {SOURCE_CHUNK_VECTORS_TABLE}", ())  # nosec B608
    try:
        watermark = int(cursor.fetchone()[0])
    finally:
        cursor.close()
    cursor = _query_rows(
        conn,
        f"""
        SELECT dimensions, COUNT(*) FROM {SOURCE_CHUNK_VECTORS_TABLE}
        WHERE user_id = ? AND rowid <= ? AND {_SIGNATURE_SQL}{scope_sql}
        GROUP BY dimensions ORDER BY COUNT(*) DESC, dimensions DESC
        """,  # nosec B608 - constants and placeholders only
        (user_id, watermark, *params, *scope_params),
    )
    try:
        widths = cursor.fetchall()
    finally:
        cursor.close()
    if widths:
        dims, expected = int(widths[0][0]), int(widths[0][1])
        if expected * _row_bytes(dims) > max_bytes:
            entry.reset()
            raise ChunkVectorCacheRefused(REFUSED_OVER_CAP)
        entry.dims = dims
        # The matrix is made small and grown to the full row count by ndarray.resize before it is filled, so the
        # large block is made by realloc. numpy advises madvise(MADV_HUGEPAGE) only when it allocates a block, and a
        # block it advises realloc of a mapped chunk copies on Linux (an append then holds the old and the new matrix
        # at once, one whole matrix at the first append after a build). A block that realloc made carries no such
        # advice, and realloc extends or moves it without a copy.
        matrix = np.empty((1, dims), dtype=np.float32)
        matrix.resize((expected, dims), refcheck=False)
        codes = np.empty(expected, dtype=np.int32)
        filled = 0
        cursor = _query_rows(
            conn,
            f"""
            SELECT chunk_id, source_id, embedding FROM {SOURCE_CHUNK_VECTORS_TABLE}
            WHERE user_id = ? AND rowid <= ? AND dimensions = ? AND {_SIGNATURE_SQL}{scope_sql}
            ORDER BY rowid
            """,  # nosec B608 - constants and placeholders only
            (user_id, watermark, dims, *params, *scope_params),
        )
        try:
            while filled < expected:
                page = cursor.fetchmany(min(_ROWS_PER_STEP, expected - filled))
                if not page:
                    break
                for chunk_id, source_id, blob in page:
                    if not _read_blob_into(matrix[filled], blob, dims):
                        continue
                    code = entry.code_for(str(source_id))
                    codes[filled] = code
                    entry.chunk_ids.append(str(chunk_id))
                    entry.rows_of_source.setdefault(code, []).append(filled)
                    filled += 1
        finally:
            cursor.close()
        if filled != expected:
            matrix.resize((filled, dims), refcheck=False)
            codes.resize(filled, refcheck=False)
        entry.matrix = matrix
        entry.codes = codes
        entry.norms = _norms_of(matrix)
    cursor = _query_rows(
        conn,
        f"""
        SELECT source_id FROM {SOURCE_CHUNK_VECTORS_TABLE}
        WHERE user_id = ? AND rowid <= ? AND NOT ({_SIGNATURE_SQL} AND dimensions = ?){scope_sql}
        """,  # nosec B608 - constants and placeholders only
        (user_id, watermark, *params, entry.dims, *scope_params),
    )
    try:
        for (source_id,) in cursor:
            entry.stale_codes.append(entry.code_for(str(source_id)))
    finally:
        cursor.close()
    entry.max_rowid = watermark
    entry.signature = signature
    entry.stamp = stamp


def _append(entry: _Entry, conn: sqlite3.Connection, user_id: str, signature: ChunkSignature, max_bytes: int) -> None:
    """Add the rows written since the user-wide entry was built, which a stamp that did not change guarantees are only additions.

    Only the user-wide copy is extended. A row number can be reused after the rows above it were deleted, which the stamp
    of the whole table reports and a scoped copy, which does not read it, would not see; a scoped copy is rebuilt.
    """

    if not source_chunk_vectors_table_exists(conn):
        return
    cursor = _query_rows(conn, f"SELECT COALESCE(MAX(rowid), 0) FROM {SOURCE_CHUNK_VECTORS_TABLE}", ())  # nosec B608
    try:
        newest = int(cursor.fetchone()[0])
    finally:
        cursor.close()
    if newest <= entry.max_rowid:
        return
    params = _signature_params(signature)
    entry.appends += 1
    while entry.max_rowid < newest:
        cursor = _query_rows(
            conn,
            f"""
            SELECT rowid, chunk_id, source_id, dimensions, embedding, ({_SIGNATURE_SQL}) AS sig_ok
            FROM {SOURCE_CHUNK_VECTORS_TABLE}
            WHERE user_id = ? AND rowid > ? AND rowid <= ?
            ORDER BY rowid LIMIT ?
            """,  # nosec B608 - constants and placeholders only
            (*params, user_id, entry.max_rowid, newest, _ROWS_PER_STEP),
        )
        try:
            page = cursor.fetchall()
        finally:
            cursor.close()
        if not page:
            entry.max_rowid = newest
            break
        entry.max_rowid = int(page[-1][0])
        fresh = []
        for row in page:
            if row[5] and entry.dims in (0, int(row[3])):
                fresh.append(row)
            else:
                entry.stale_codes.append(entry.code_for(str(row[2])))
        if not fresh:
            continue
        if entry.dims == 0:
            entry.dims = int(fresh[0][3])
            entry.matrix = np.empty((0, entry.dims), dtype=np.float32)
        start = entry.rows
        if (start + len(fresh)) * _row_bytes(entry.dims) > max_bytes:
            entry.reset()
            raise ChunkVectorCacheRefused(REFUSED_OVER_CAP)
        # In place: the arrays are the same objects before and after. On Linux with glibc and on macOS the resize was
        # measured not to hold a second copy of the matrix; other allocators were not measured.
        entry.matrix.resize((start + len(fresh), entry.dims), refcheck=False)
        entry.codes.resize(start + len(fresh), refcheck=False)
        kept = 0
        for _rowid, chunk_id, source_id, _dims, blob, _ok in fresh:
            if not _read_blob_into(entry.matrix[start + kept], blob, entry.dims):
                continue
            code = entry.code_for(str(source_id))
            entry.codes[start + kept] = code
            entry.chunk_ids.append(str(chunk_id))
            entry.rows_of_source.setdefault(code, []).append(start + kept)
            kept += 1
        if kept != len(fresh):
            entry.matrix.resize((start + kept, entry.dims), refcheck=False)
            entry.codes.resize(start + kept, refcheck=False)
        tail = entry.matrix[start : start + kept]
        entry.norms.resize(start + kept, refcheck=False)
        entry.norms[start : start + kept] = _norms_of(tail)


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    chunk_id: str
    source_id: str
    score: float


class ChunkVectorMask:
    """The rows a caller may read: the vectors of the sources in ``allowed``, and nothing else."""

    __slots__ = ("_entry", "_flags", "rows", "indices", "stale")

    def __init__(self, entry: _Entry, allowed: Collection[str]) -> None:
        self._entry = entry
        flags = np.zeros(len(entry.source_ids), dtype=bool)
        for source_id in allowed:
            code = entry.code_of.get(str(source_id))
            if code is not None:
                flags[code] = True
        self._flags = flags
        self.indices = np.flatnonzero(flags[entry.codes]) if entry.rows else np.empty(0, dtype=np.int64)
        self.rows = int(self.indices.size)
        self.stale = int(flags[np.asarray(entry.stale_codes, dtype=np.int64)].sum()) if entry.stale_codes else 0

    def allows(self, source_id: str) -> bool:
        code = self._entry.code_of.get(str(source_id))
        return code is not None and bool(self._flags[code])


class ChunkVectorView:
    """A locked, validated entry. Use it only inside the ``with`` that produced it."""

    def __init__(self, entry: _Entry) -> None:
        self._entry = entry

    @property
    def dims(self) -> int:
        return self._entry.dims

    @property
    def total_rows(self) -> int:
        return self._entry.rows

    def mask(self, allowed: Collection[str]) -> ChunkVectorMask:
        return ChunkVectorMask(self._entry, allowed)

    def _scores(self, mask: ChunkVectorMask, positions: np.ndarray, query: np.ndarray, query_norm: float) -> np.ndarray:
        entry = self._entry
        scores = np.empty(positions.size, dtype=np.float32)
        contiguous = positions.size == entry.rows
        for start in range(0, positions.size, _ROWS_PER_STEP):
            stop = min(start + _ROWS_PER_STEP, positions.size)
            if contiguous:
                block = entry.matrix[start:stop]
                norms = entry.norms[start:stop]
            else:
                selected = positions[start:stop]
                block = entry.matrix[selected]
                norms = entry.norms[selected]
            dots = block @ query
            with np.errstate(divide="ignore", invalid="ignore"):
                cosine = dots / (norms * query_norm)
            cosine[~np.isfinite(cosine)] = -1.0
            scores[start:stop] = cosine
        return scores

    @staticmethod
    def _prepare_query(query_vector: Sequence[float], dims: int) -> tuple[np.ndarray, float] | None:
        query = np.asarray(query_vector, dtype=np.float32)
        if query.ndim != 1 or query.size != dims:
            return None
        norm = float(np.linalg.norm(query))
        if not np.isfinite(norm) or norm == 0.0:
            return None
        return query, norm

    def top_chunks(self, mask: ChunkVectorMask, query_vector: Sequence[float], count: int) -> list[ScoredChunk]:
        """The ``count`` readable chunks nearest the query, best first. Equal scores order by chunk id."""

        entry = self._entry
        prepared = self._prepare_query(query_vector, entry.dims)
        if prepared is None or mask.rows == 0 or count < 1:
            return []
        query, norm = prepared
        scores = self._scores(mask, mask.indices, query, norm)
        take = min(count, scores.size)
        if take < scores.size:
            best = np.argpartition(-scores, take - 1)[:take]
        else:
            best = np.arange(scores.size)
        chosen = [(float(scores[i]), entry.chunk_ids[int(mask.indices[i])], int(mask.indices[i])) for i in best]
        chosen.sort(key=lambda item: (-item[0], item[1]))
        return [
            ScoredChunk(chunk_id, entry.source_ids[int(entry.codes[position])], score)
            for score, chunk_id, position in chosen
        ]

    def rank_in_source(self, mask: ChunkVectorMask, source_id: str, query_vector: Sequence[float]) -> list[ScoredChunk]:
        """Every chunk of one readable source that holds a current vector, best first. Empty for a source outside the mask."""

        entry = self._entry
        prepared = self._prepare_query(query_vector, entry.dims)
        if prepared is None or not mask.allows(source_id):
            return []
        code = entry.code_of[str(source_id)]
        positions = np.asarray(entry.rows_of_source.get(code, ()), dtype=np.int64)
        if positions.size == 0:
            return []
        query, norm = prepared
        scores = self._scores(mask, positions, query, norm)
        ranked = sorted(
            ((float(scores[i]), entry.chunk_ids[int(positions[i])]) for i in range(positions.size)),
            key=lambda item: (-item[0], item[1]),
        )
        return [ScoredChunk(chunk_id, str(source_id), score) for score, chunk_id in ranked]


def scope_digest(scope: Collection[str]) -> tuple[str, str]:
    """The key of a scoped copy and the JSON array its builds read by: both are functions of the ids and nothing else."""

    ordered = sorted({str(source_id) for source_id in scope})
    payload = json.dumps(ordered, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest(), payload


_SCOPED: dict[tuple[str, str], OrderedDict[str, _Entry]] = {}


def _entry_for(database_file: str, user_id: str, digest: str | None) -> tuple[_Entry, str]:
    """The entry for the user-wide copy (``digest`` of ``None``) or for the scoped copy with this digest.

    A scoped copy that is asked for is the most recently used, and the least recently used one beyond
    ``max_scoped_copies()`` is forgotten. Which one that is depends on which scopes were asked for, never on a row
    and never on the size of any copy.
    """

    with _REGISTRY_LOCK:
        if digest is None:
            entry = _REGISTRY.get((database_file, user_id))
            if entry is None:
                entry = _Entry()
                _REGISTRY[(database_file, user_id)] = entry
            return entry, ""
        copies = _SCOPED.setdefault((database_file, user_id), OrderedDict())
        entry = copies.get(digest)
        if entry is None:
            entry = _Entry()
            copies[digest] = entry
        copies.move_to_end(digest)
        while len(copies) > max_scoped_copies():
            copies.popitem(last=False)
        return entry, digest


def _forget(database_file: str, user_id: str, digest: str | None, entry: _Entry) -> None:
    with _REGISTRY_LOCK:
        if digest is None:
            if _REGISTRY.get((database_file, user_id)) is entry:
                del _REGISTRY[(database_file, user_id)]
            return
        copies = _SCOPED.get((database_file, user_id))
        if copies is not None and copies.get(digest) is entry:
            del copies[digest]


@contextmanager
def chunk_vector_view(
    conn: sqlite3.Connection,
    user_id: str,
    signature: ChunkSignature,
    *,
    scope: Collection[str] | None = None,
) -> Iterator[ChunkVectorView]:
    """Validate the resident copy against the database, build or extend it as needed, and hold it for the caller.

    Without ``scope`` the copy is the user-wide one, built from every vector of the user, for the owner and an unbound
    admin key. With ``scope`` (the ids of the sources a caller with limits may read) the copy is a scoped one built
    from the vectors of those sources only, so its width and its cap are judged on rows the caller may read.

    Raises ``ChunkVectorCacheRefused`` when the copy would be over its cap, or when the database has no stable file
    to key a copy by (an in-memory database). The caller reports that and does not read the table instead.
    """

    try:
        database_file = _resolve_database_file(conn)
    except sqlite3.Error as exc:
        raise ChunkVectorCacheRefused(REFUSED_UNAVAILABLE) from exc
    if database_file is None:
        raise ChunkVectorCacheRefused(REFUSED_UNAVAILABLE)
    user = str(user_id)
    digest, scope_json = scope_digest(scope) if scope is not None else (None, None)
    entry, _key = _entry_for(database_file, user, digest)
    max_bytes = cache_max_bytes() if scope_json is None else scoped_cache_max_bytes()
    with entry.lock:
        try:
            # The user-wide copy is valid by the stamp of the whole table. A scoped copy is valid by the fingerprint of
            # its own sources, so a vector of a source it does not hold cannot invalidate it.
            stamp = (
                source_chunk_vectors_stamp(conn)
                if scope_json is None
                else source_chunk_vector_fingerprint(conn, user, scope_json)
            )
            if entry.signature != signature or entry.stamp != stamp:
                _rebuild(entry, conn, user, signature, stamp, max_bytes, scope_json)
            elif scope_json is None:
                _append(entry, conn, user, signature, max_bytes)
            if entry.resident_bytes > max_bytes:
                entry.reset()
                raise ChunkVectorCacheRefused(REFUSED_OVER_CAP)
        except ChunkVectorCacheRefused:
            _forget(database_file, user, digest, entry)
            raise
        except sqlite3.Error as exc:
            _forget(database_file, user, digest, entry)
            raise ChunkVectorCacheRefused(REFUSED_UNAVAILABLE) from exc
        except Exception:
            # Whatever else broke may have left the entry half built: it is dropped and built again next time.
            _forget(database_file, user, digest, entry)
            raise
        yield ChunkVectorView(entry)


__all__ = [
    "CACHE_MAX_MB_ENV",
    "DEFAULT_CACHE_MAX_MB",
    "DEFAULT_SCOPED_CACHE_MAX_MB",
    "DEFAULT_SCOPED_COPIES",
    "MAX_SCOPED_COPIES_SETTING",
    "SCOPED_CACHE_MAX_MB_ENV",
    "SCOPED_COPIES_ENV",
    "REFUSED_OVER_CAP",
    "REFUSED_UNAVAILABLE",
    "ChunkVectorCacheRefused",
    "ChunkVectorMask",
    "ChunkVectorView",
    "ScoredChunk",
    "cache_max_bytes",
    "chunk_vector_view",
    "clear_registry",
    "entry_counters",
    "max_scoped_copies",
    "scope_digest",
    "scoped_cache_max_bytes",
    "scoped_entry_counters",
]
