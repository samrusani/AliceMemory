"""SQLite store seam for the vectors of source chunks (``source_chunk_embeddings``).

The functions here take a ``SQLiteVNextStore`` as their first argument and use its connection and its
``user_id``. They read and write the side table and the rows it is judged against, and hold no rule about what
may be sent or when a vector is current: that is ``source_embedding_policy`` and ``source_chunk_embeddings``.
They are functions of this module, not methods of the store class, so the shape of the class is unchanged.
Every statement binds ``user_id``.

The table is never written inside a statement that touches ``source_chunks``: the full-text update trigger
of that table fires on any update of a chunk row, so a vector kept on the chunk row would re-index the
chunk's text every time it was written.

The table is made by the first write (``ensure_chunk_vector_table``), not by the bootstrap, so a vault that never
stored a vector has no such table. Every read here answers "none" for a vault without it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING

import numpy as np

from alicebot_api.sqlite_schema import (
    drop_source_chunk_vectors_table,
    ensure_source_chunk_vectors_table,
    source_chunk_vectors_table_exists,
)
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import MAX_NATIVE_EMBEDDING_DIMENSIONS
from alicebot_api.vnext_stores.sqlite.primitives import _utc_now_iso

if TYPE_CHECKING:
    from alicebot_api.sqlite_store import SQLiteVNextStore

VNextRow = dict[str, object]

#: The columns of a vector row without the vector itself.
CHUNK_VECTOR_META_COLUMNS = (
    "chunk_id",
    "source_id",
    "dimensions",
    "provider",
    "model",
    "endpoint",
    "signature_version",
    "header_version",
    "prefix_version",
    "content_sha256",
    "created_at",
)

#: A source that has not been deleted, replaced or scrubbed.
LIVE_SOURCE_SQL = "deleted_at IS NULL AND COALESCE(json_extract(metadata_json, '$.scrubbed'), 0) != 1"


@contextmanager
def secure_deletion(store: SQLiteVNextStore) -> Iterator[None]:
    """Zero the pages a statement frees or rewrites, then put the connection's setting back.

    A vector is 3 KB, about a page, and SQLite moves cells between pages when it inserts, overwrites and deletes.
    With ``secure_delete`` off each move leaves the old bytes in unused space, so deleting a row later does not
    remove the bytes an earlier move left behind. Scrub turns the setting on for the chunk text for the same
    reason. The vectors are written and deleted under it, inside the caller's savepoint. The write-ahead log and
    earlier backups can still hold the bytes until a checkpoint and VACUUM: this is logical deletion.
    """

    row = store._execute("PRAGMA secure_delete").fetchone()
    previous = int(row["secure_delete"]) if row is not None else 0
    store._execute("PRAGMA secure_delete=ON")
    try:
        yield
    finally:
        store._execute(f"PRAGMA secure_delete={'FAST' if previous == 2 else previous}")


@contextmanager
def read_snapshot(store: SQLiteVNextStore) -> Iterator[None]:
    """Read inside one transaction, so that every read in the block sees the vault as it was at the first one.

    The store's connection commits each read on its own until it writes, and a source row read, then a commit by
    another connection, then its chunk rows read, would be two states. A transaction begun here (deferred: it takes no
    write lock) pins one. A connection that is already inside a transaction is read as it is. The block ends the
    transaction it began, with a rollback, because it wrote nothing.
    """

    conn = store.conn
    began = False
    if not conn.in_transaction:
        conn.execute("BEGIN")
        began = True
    try:
        yield
    finally:
        if began and conn.in_transaction:
            # label state: none, this block only read, so it wrote no label that the request's label state could hold.
            conn.rollback()


def chunk_vector_table_exists(store: SQLiteVNextStore) -> bool:
    return source_chunk_vectors_table_exists(store.conn)


def ensure_chunk_vector_table(store: SQLiteVNextStore) -> None:
    """Make the vector table when the vault has none. Call it inside the transaction of the write that needs it."""

    ensure_source_chunk_vectors_table(store.conn)


def count_chunk_vectors(store: SQLiteVNextStore) -> int:
    if not chunk_vector_table_exists(store):
        return 0
    row = store._fetch_one(
        "count chunk vectors",
        "SELECT COUNT(*) AS n FROM source_chunk_embeddings WHERE user_id = ?",
        (store.user_id,),
    )
    return int(row["n"])  # type: ignore[call-overload]


def count_source_chunk_vectors(store: SQLiteVNextStore, source_id: str) -> int:
    if not chunk_vector_table_exists(store):
        return 0
    row = store._fetch_one(
        "count chunk vectors of a source",
        "SELECT COUNT(*) AS n FROM source_chunk_embeddings WHERE user_id = ? AND source_id = ?",
        (store.user_id, str(source_id)),
    )
    return int(row["n"])  # type: ignore[call-overload]


def embedding_source_row(store: SQLiteVNextStore, source_id: str) -> VNextRow | None:
    """The fields of one source the embedding rules read, with ``live`` for the lifecycle test."""

    row = store._fetch_optional_one(
        f"""
        SELECT id, title, domain, sensitivity,
               ({LIVE_SOURCE_SQL}) AS live
        FROM sources
        WHERE id = ? AND user_id = ?
        """,  # nosec B608 - LIVE_SOURCE_SQL is a module constant
        (str(source_id), store.user_id),
    )
    if row is None:
        return None
    row["live"] = bool(row["live"])
    return row


def embedding_source_chunks(store: SQLiteVNextStore, source_id: str) -> list[VNextRow]:
    """Every chunk of a source in reading order: id, index and text."""

    return store._fetch_all(
        """
        SELECT id, chunk_index, text
        FROM source_chunks
        WHERE source_id = ? AND user_id = ?
        ORDER BY chunk_index ASC, id ASC
        """,
        (str(source_id), store.user_id),
    )


def count_embedding_source_chunks(store: SQLiteVNextStore, source_id: str) -> int:
    row = store._fetch_one(
        "count chunks of a source",
        "SELECT COUNT(*) AS n FROM source_chunks WHERE source_id = ? AND user_id = ?",
        (str(source_id), store.user_id),
    )
    return int(row["n"])  # type: ignore[call-overload]


def chunk_vector_meta(store: SQLiteVNextStore, source_id: str) -> dict[str, VNextRow]:
    """The vector rows of one source without the vectors, by chunk id."""

    if not chunk_vector_table_exists(store):
        return {}
    rows = store._fetch_all(
        f"""
        SELECT {", ".join(CHUNK_VECTOR_META_COLUMNS)}
        FROM source_chunk_embeddings
        WHERE source_id = ? AND user_id = ?
        """,  # nosec B608 - the column list is a module constant
        (str(source_id), store.user_id),
    )
    return {str(row["chunk_id"]): row for row in rows}


def live_source_ids_after(store: SQLiteVNextStore, after_id: str | None, limit: int) -> list[str]:
    """Ids of live sources in id order, after ``after_id``: the pages a backfill walks."""

    rows = store._fetch_all(
        f"""
        SELECT id
        FROM sources
        WHERE user_id = ?
          AND {LIVE_SOURCE_SQL}
          AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,  # nosec B608 - LIVE_SOURCE_SQL is a module constant
        (store.user_id, after_id or "", int(limit)),
    )
    return [str(row["id"]) for row in rows]


def put_chunk_vector(
    store: SQLiteVNextStore,
    *,
    chunk_id: str,
    source_id: str,
    vector: Sequence[float],
    provider: str,
    model: str,
    endpoint: str,
    signature_version: int,
    header_version: int,
    prefix_version: int,
    content_sha256: str,
) -> None:
    """Write the vector of one chunk, replacing the one it has. The caller has already judged it current.

    The vector is stored at the width it has. A chunk that is not in ``source_chunks`` for this user is a
    foreign key failure, which the caller reads as a chunk that went away.
    """

    ensure_source_chunk_vectors_table(store.conn)
    values = np.asarray(vector, dtype=np.float32)
    if values.ndim != 1 or values.size == 0 or values.size > MAX_NATIVE_EMBEDDING_DIMENSIONS:
        raise ContinuityStoreInvariantError("a chunk vector must be a non-empty one-dimensional array")
    if not np.all(np.isfinite(values)):
        raise ContinuityStoreInvariantError("a chunk vector must hold only finite numbers")
    store._execute(
        """
        INSERT INTO source_chunk_embeddings (
          chunk_id, user_id, source_id, dimensions, embedding, provider, model, endpoint,
          signature_version, header_version, prefix_version, content_sha256, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chunk_id) DO UPDATE SET
          user_id = excluded.user_id,
          source_id = excluded.source_id,
          dimensions = excluded.dimensions,
          embedding = excluded.embedding,
          provider = excluded.provider,
          model = excluded.model,
          endpoint = excluded.endpoint,
          signature_version = excluded.signature_version,
          header_version = excluded.header_version,
          prefix_version = excluded.prefix_version,
          content_sha256 = excluded.content_sha256,
          created_at = excluded.created_at
        """,
        (
            str(chunk_id),
            store.user_id,
            str(source_id),
            int(values.size),
            values.tobytes(),
            provider,
            model,
            endpoint,
            int(signature_version),
            int(header_version),
            int(prefix_version),
            content_sha256,
            _utc_now_iso(),
        ),
    )


def delete_chunk_vectors(store: SQLiteVNextStore, *, source_id: str | None = None) -> int:
    """Delete the vectors of one source, or of every source of the user. Returns how many rows went."""

    if not chunk_vector_table_exists(store):
        return 0
    with secure_deletion(store):
        if source_id is None:
            cursor = store._execute("DELETE FROM source_chunk_embeddings WHERE user_id = ?", (store.user_id,))
        else:
            cursor = store._execute(
                "DELETE FROM source_chunk_embeddings WHERE user_id = ? AND source_id = ?",
                (store.user_id, str(source_id)),
            )
    return int(cursor.rowcount)


def drop_chunk_vector_table(store: SQLiteVNextStore) -> int:
    """Delete every vector of the user, and remove the table when no vector of anyone is left in it.

    The vault is then as a build without chunk vectors left it, which is what lets an older release export it.
    Returns how many rows the user had. The rows go under ``secure_deletion``, so the table is empty of vector bytes
    before it is dropped.
    """

    removed = delete_chunk_vectors(store)
    if chunk_vector_table_exists(store):
        row = store._fetch_one("count remaining chunk vectors", "SELECT COUNT(*) AS n FROM source_chunk_embeddings")
        if int(row["n"]) == 0:  # type: ignore[call-overload]
            drop_source_chunk_vectors_table(store.conn)
    return removed


__all__ = [
    "CHUNK_VECTOR_META_COLUMNS",
    "LIVE_SOURCE_SQL",
    "chunk_vector_meta",
    "chunk_vector_table_exists",
    "drop_chunk_vector_table",
    "ensure_chunk_vector_table",
    "count_chunk_vectors",
    "count_embedding_source_chunks",
    "count_source_chunk_vectors",
    "delete_chunk_vectors",
    "embedding_source_chunks",
    "embedding_source_row",
    "live_source_ids_after",
    "put_chunk_vector",
    "read_snapshot",
    "secure_deletion",
]
