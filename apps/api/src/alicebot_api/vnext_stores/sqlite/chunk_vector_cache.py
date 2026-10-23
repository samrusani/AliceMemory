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
* **No doubling.** The matrix, the norms and the source codes grow in place (``ndarray.resize``), so an append never
  holds the old and the new array at once.
* **Cap.** ``ALICE_SOURCE_VECTOR_CACHE_MAX_MB`` (default 512, about 170,000 vectors of 768 floats) bounds the
  resident bytes. Over it the cache refuses with ``over_cap`` and the vector stage reports that status and is
  skipped: it never reads the table row by row instead.
* **The mask.** What a caller may read is decided outside, by ``readable_source_ids``, and handed in per call as a
  set of source ids. The cache ranks only the rows of those sources, and counts only those rows, so a row the caller
  may not read cannot take a rank or change a count.

A reader holds the entry's lock for as long as it uses the view, so an append cannot change the arrays under it.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

import numpy as np

from alicebot_api.sqlite_schema import (
    SOURCE_CHUNK_VECTORS_TABLE,
    source_chunk_vectors_stamp,
    source_chunk_vectors_table_exists,
)
from alicebot_api.source_embedding_policy import ChunkSignature
from alicebot_api.vnext_stores.sqlite.vector_scan import _resolve_database_file

CACHE_MAX_MB_ENV = "ALICE_SOURCE_VECTOR_CACHE_MAX_MB"
DEFAULT_CACHE_MAX_MB = 512

#: Why the cache did not serve.
REFUSED_OVER_CAP = "over_cap"
REFUSED_UNAVAILABLE = "unavailable"

#: Rows read and decoded in one step of a build or an append, and rows scored in one step of a scan.
_ROWS_PER_STEP = 2048

_SIGNATURE_SQL = (
    "provider = ? AND model = ? AND endpoint = ? AND signature_version = ? "
    "AND header_version = ? AND prefix_version = ?"
)


class ChunkVectorCacheRefused(Exception):
    """The cache did not serve. ``reason`` is ``REFUSED_OVER_CAP`` or ``REFUSED_UNAVAILABLE``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def cache_max_bytes() -> int:
    raw = os.environ.get(CACHE_MAX_MB_ENV, "").strip()
    try:
        megabytes = int(raw) if raw else DEFAULT_CACHE_MAX_MB
    except ValueError:
        megabytes = DEFAULT_CACHE_MAX_MB
    return max(megabytes, 0) * 1024 * 1024


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
    """Forget every entry (tests, and a process that wants the memory back)."""

    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def entry_counters(database_file: str, user_id: str) -> dict[str, int]:
    """Builds and appends of one entry, for the tests that prove a write did or did not rebuild it."""

    with _REGISTRY_LOCK:
        entry = _REGISTRY.get((database_file, str(user_id)))
    if entry is None:
        return {"rebuilds": 0, "appends": 0, "rows": 0}
    return {"rebuilds": entry.rebuilds, "appends": entry.appends, "rows": entry.rows}


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


def _rebuild(entry: _Entry, conn: sqlite3.Connection, user_id: str, signature: ChunkSignature, stamp: str | None, max_bytes: int) -> None:
    """Read every vector of the user that carries ``signature`` into one matrix, and the source codes of the rest."""

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
        WHERE user_id = ? AND rowid <= ? AND {_SIGNATURE_SQL}
        GROUP BY dimensions ORDER BY COUNT(*) DESC, dimensions DESC
        """,  # nosec B608 - constants and placeholders only
        (user_id, watermark, *params),
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
        matrix = np.empty((expected, dims), dtype=np.float32)
        codes = np.empty(expected, dtype=np.int32)
        filled = 0
        cursor = _query_rows(
            conn,
            f"""
            SELECT chunk_id, source_id, embedding FROM {SOURCE_CHUNK_VECTORS_TABLE}
            WHERE user_id = ? AND rowid <= ? AND dimensions = ? AND {_SIGNATURE_SQL}
            ORDER BY rowid
            """,  # nosec B608 - constants and placeholders only
            (user_id, watermark, dims, *params),
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
        WHERE user_id = ? AND rowid <= ? AND NOT ({_SIGNATURE_SQL} AND dimensions = ?)
        """,  # nosec B608 - constants and placeholders only
        (user_id, watermark, *params, entry.dims),
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
    """Add the rows written since the entry was built, which a stamp that did not change guarantees are only additions."""

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
        # In place: the arrays are the same objects before and after, so no second copy of the matrix exists.
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


@contextmanager
def chunk_vector_view(conn: sqlite3.Connection, user_id: str, signature: ChunkSignature) -> Iterator[ChunkVectorView]:
    """Validate the resident copy against the database, build or extend it as needed, and hold it for the caller.

    Raises ``ChunkVectorCacheRefused`` when the copy would be over its cap, or when the database has no stable file
    to key a copy by (an in-memory database). The caller reports that and does not read the table instead.
    """

    try:
        database_file = _resolve_database_file(conn)
    except sqlite3.Error as exc:
        raise ChunkVectorCacheRefused(REFUSED_UNAVAILABLE) from exc
    if database_file is None:
        raise ChunkVectorCacheRefused(REFUSED_UNAVAILABLE)
    key = (database_file, str(user_id))
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            entry = _Entry()
            _REGISTRY[key] = entry
    max_bytes = cache_max_bytes()
    with entry.lock:
        try:
            stamp = source_chunk_vectors_stamp(conn)
            if entry.signature != signature or entry.stamp != stamp:
                _rebuild(entry, conn, str(user_id), signature, stamp, max_bytes)
            else:
                _append(entry, conn, str(user_id), signature, max_bytes)
            if entry.resident_bytes > max_bytes:
                entry.reset()
                raise ChunkVectorCacheRefused(REFUSED_OVER_CAP)
        except ChunkVectorCacheRefused:
            with _REGISTRY_LOCK:
                _REGISTRY.pop(key, None)
            raise
        except sqlite3.Error as exc:
            with _REGISTRY_LOCK:
                _REGISTRY.pop(key, None)
            raise ChunkVectorCacheRefused(REFUSED_UNAVAILABLE) from exc
        except Exception:
            # Whatever else broke may have left the entry half built: it is dropped and built again next time.
            with _REGISTRY_LOCK:
                _REGISTRY.pop(key, None)
            raise
        yield ChunkVectorView(entry)


__all__ = [
    "CACHE_MAX_MB_ENV",
    "DEFAULT_CACHE_MAX_MB",
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
]
