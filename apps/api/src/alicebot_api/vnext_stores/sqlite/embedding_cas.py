"""SQLite embedding compare-and-swap store seam."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

import numpy as np

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    EMBEDDING_TRUNCATED_SIGNATURE_KEY,
    embedding_text_is_cut,
    memory_embedding_content_sha256,
    memory_embedding_text,
    pad_embedding_vector,
)
from alicebot_api.vnext_stores.sqlite.columns import MEMORY_COLUMNS
from alicebot_api.vnext_stores.sqlite.query_predicates import _expiry_clause
from alicebot_api.vnext_stores.sqlite.vector_scan import bump_embedding_stamp
from alicebot_api.vnext_label_writes import takes_label_lock

VNextRow = dict[str, object]

# Point-read backing the resident-vector-cache invalidation contract: only a
# write that OVERWRITES or CLEARS an existing non-NULL embedding bumps the
# embedding_stamp token (in the same transaction as the write). Embed-on-write
# for a row without a vector must NOT bump -- the cache upserts new ids
# without a rebuild.
#
# The read is only sound while it shares a transaction with the UPDATE and
# the bump: executed in autocommit, a concurrent embed-on-write can commit
# NULL -> vector between the read and the UPDATE, turning this write into an
# overwrite whose bump the stale read skips (the cache then serves the dead
# vector forever). Both callers therefore take the writer lock (BEGIN
# IMMEDIATE, unless the caller already opened a transaction) BEFORE reading.
_EMBEDDING_PRESENT_SQL = """
                SELECT (embedding IS NOT NULL) AS embedding_present
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """


def _embedding_content_sha256_sqlite(
    title: object,
    canonical_text: object,
    summary: object,
) -> str:
    """SQLite UDF for the exact normalized text embedded by production."""
    return memory_embedding_content_sha256(
        {
            "title": title,
            "canonical_text": canonical_text,
            "summary": summary,
        }
    )


def _ensure_embedding_content_sha256_sqlite(conn: sqlite3.Connection) -> None:
    """Register the deterministic digest UDF once per SQLite connection."""
    cursor = conn.execute(
        "SELECT 1 FROM pragma_function_list WHERE name = 'alice_embedding_content_sha256' AND narg = 3 LIMIT 1"
    )
    try:
        registered = cursor.fetchone() is not None
    finally:
        cursor.close()
    if registered:
        return
    conn.create_function(
        "alice_embedding_content_sha256",
        3,
        _embedding_content_sha256_sqlite,
        deterministic=True,
    )


def _embedding_input_cut_sqlite(
    title: object,
    canonical_text: object,
    summary: object,
    max_chars: object,
) -> int | None:
    """SQLite UDF: the input cap when it would cut this row's embedded text, else NULL.

    This is the signature value a vector made today must carry in
    ``truncated_to_chars``: the cap for a text longer than it, and no key at
    all (NULL) for a text that fits.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        return None
    text = memory_embedding_text(
        {
            "title": title,
            "canonical_text": canonical_text,
            "summary": summary,
        }
    )
    return max_chars if embedding_text_is_cut(text, max_chars) else None


def _ensure_embedding_input_cut_sqlite(conn: sqlite3.Connection) -> None:
    """Register the input-cap UDF once per SQLite connection.

    Registered by the methods that read it, not when the store is opened, so
    every other use of a store connection is unchanged.
    """
    cursor = conn.execute(
        "SELECT 1 FROM pragma_function_list WHERE name = 'alice_embedding_input_cut' AND narg = 4 LIMIT 1"
    )
    try:
        registered = cursor.fetchone() is not None
    finally:
        cursor.close()
    if registered:
        return
    conn.create_function(
        "alice_embedding_input_cut",
        4,
        _embedding_input_cut_sqlite,
        deterministic=True,
    )


@takes_label_lock
def update_memory_embedding(
    self,
    *,
    memory_id: str,
    vector: list[float],
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    content_sha256: str | None = None,
    signature_version: int = 1,
    truncated_to_chars: int | None = None,
) -> VNextRow | None:
    if not vector:
        raise ContinuityStoreInvariantError("embedding vectors must not be empty")
    padded = pad_embedding_vector(vector)
    blob = np.asarray(padded, dtype=np.float32).tobytes()
    if not self.conn.in_transaction:
        # Writer lock BEFORE the presence read: the read decides bump vs
        # no-bump, so it must be atomic with the UPDATE and the bump.
        self.conn.execute("BEGIN IMMEDIATE")
    existing = self._fetch_optional_one(_EMBEDDING_PRESENT_SQL, (str(memory_id), self.user_id))
    overwrites_existing_vector = bool(existing and existing.get("embedding_present"))
    signature_values = (provider, model, content_sha256)
    if any(value is not None for value in signature_values):
        if not all(isinstance(value, str) and value for value in signature_values):
            raise ContinuityStoreInvariantError(
                "embedding provider, model, and content_sha256 must be supplied together"
            )
        signature_metadata = {
            "version": signature_version,
            "provider": provider,
            "model": model,
            "endpoint": endpoint if isinstance(endpoint, str) else "",
            "content_sha256": content_sha256,
        }
        if truncated_to_chars is not None:
            signature_metadata[EMBEDDING_TRUNCATED_SIGNATURE_KEY] = truncated_to_chars
        cursor = self._execute(
            """
                    UPDATE memories
                    SET embedding = ?,
                        metadata_json = json_set(metadata_json, ?, json(?))
                    WHERE id = ?
                      AND user_id = ?
                      AND deleted_at IS NULL
                      AND alice_embedding_content_sha256(title, canonical_text, summary) = ?
                    """,
            (
                blob,
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}",
                json.dumps(signature_metadata, sort_keys=True, separators=(",", ":")),
                str(memory_id),
                self.user_id,
                content_sha256,
            ),
        )
    else:
        cursor = self._execute(
            """
                    UPDATE memories
                    SET embedding = ?
                    WHERE id = ?
                      AND user_id = ?
                      AND deleted_at IS NULL
                    """,
            (blob, str(memory_id), self.user_id),
        )
    if cursor.rowcount == 0:
        return None
    if overwrites_existing_vector:
        # Reindex/backfill overwrite of a live vector: evict every resident
        # cache built over the old bytes, atomically with this write.
        bump_embedding_stamp(self._execute)
    return self._fetch_optional_one(
        """
                SELECT id
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                """,
        (str(memory_id), self.user_id),
    )


@takes_label_lock
def clear_memory_embedding(self, *, memory_id: str) -> VNextRow | None:
    """Invalidate an embedding derived from text that is about to change."""
    if not self.conn.in_transaction:
        # Writer lock BEFORE the presence read: the read decides bump vs
        # no-bump, so it must be atomic with the UPDATE and the bump.
        self.conn.execute("BEGIN IMMEDIATE")
    existing = self._fetch_optional_one(_EMBEDDING_PRESENT_SQL, (str(memory_id), self.user_id))
    had_vector = bool(existing and existing.get("embedding_present"))
    cursor = self._execute(
        f"""
                UPDATE memories
                SET embedding = NULL,
                    metadata_json = json_remove(
                      metadata_json,
                      '$.{EMBEDDING_SIGNATURE_METADATA_KEY}'
                    )
                WHERE id = ?
                  AND user_id = ?
                  AND deleted_at IS NULL
                """,
        (str(memory_id), self.user_id),
    )
    if cursor.rowcount == 0:
        return None
    if had_vector:
        # THE CLEAR-THEN-RE-EMBED HOLE: the commit service clears and then
        # re-embeds on text updates. The re-embed sees a NULL column and does
        # not bump, the id is already resident, and the top-k content-sha
        # recheck cannot catch a same-id vector swap -- so the CLEAR must
        # evict, in the same transaction as the NULLing write.
        bump_embedding_stamp(self._execute)
    return self._fetch_optional_one(
        """
                SELECT id
                FROM memories
                WHERE id = ?
                  AND user_id = ?
                """,
        (str(memory_id), self.user_id),
    )


def _missing_embeddings_clause(
    *,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_endpoint: str | None,
    embedding_signature_version: int | None,
    embedding_input_cap: int | None,
) -> tuple[str, list[object]]:
    """The OR terms that mark a row whose vector is not current.

    Shared by the reindex listing and the doctor count, so the number the
    doctor prints is the number reindex would work on.
    """
    signature_sql = ""
    signature_params: list[object] = []
    if embedding_provider is None and embedding_model is None:
        if embedding_input_cap is not None:
            raise ContinuityStoreInvariantError(
                "embedding_input_cap requires embedding_provider and embedding_model"
            )
        return signature_sql, signature_params
    if not embedding_provider or not embedding_model:
        raise ContinuityStoreInvariantError("embedding_provider and embedding_model must be supplied together")
    signature_sql = (
        " OR json_extract(metadata_json, ?) IS NOT ?"
        " OR json_extract(metadata_json, ?) IS NOT ?"
        " OR json_extract(metadata_json, ?) IS NOT "
        "alice_embedding_content_sha256(title, canonical_text, summary)"
    )
    signature_params.extend(
        (
            f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.provider",
            embedding_provider,
            f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.model",
            embedding_model,
            f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.content_sha256",
        )
    )
    if embedding_endpoint is not None:
        # Re-embed rows whose stored endpoint differs from the current one.
        signature_sql += " OR json_extract(metadata_json, ?) IS NOT ?"
        signature_params.extend(
            (
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.endpoint",
                embedding_endpoint,
            )
        )
    if embedding_signature_version is not None:
        signature_sql += " OR json_extract(metadata_json, ?) IS NOT ?"
        signature_params.extend(
            (
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.version",
                embedding_signature_version,
            )
        )
    if embedding_input_cap is not None:
        # The label a vector made now would carry: the cap for a text longer
        # than it, none for a text that fits. A row whose stored label differs
        # was made under another cap (or before caps existed, from a text the
        # endpoint may have cut without saying so) and is made again. A text
        # that fits under both caps carries no label and is left alone.
        signature_sql += (
            " OR json_extract(metadata_json, ?) IS NOT "
            "alice_embedding_input_cut(title, canonical_text, summary, ?)"
        )
        signature_params.extend(
            (
                f"$.{EMBEDDING_SIGNATURE_METADATA_KEY}.{EMBEDDING_TRUNCATED_SIGNATURE_KEY}",
                embedding_input_cap,
            )
        )
    return signature_sql, signature_params


def _embedding_status_values(statuses: Sequence[str], *, caller: str) -> list[str]:
    """The statuses a vector is made for, checked before a query is built.

    Required by the listing and the count alike, with no default, so a caller
    that forgets it fails here and cannot send every row's text to the
    embeddings endpoint. A bare string is refused (it would be read as one
    status per character), and so is an empty list.
    """
    if isinstance(statuses, str) or not statuses:
        raise ContinuityStoreInvariantError(f"{caller} requires a non-empty list of statuses")
    values = [str(status) for status in statuses]
    if not all(values):
        raise ContinuityStoreInvariantError(f"{caller} statuses must not be empty strings")
    return values


def list_memories_missing_embeddings(
    self,
    *,
    statuses: Sequence[str],
    limit: int = 100,
    after_id: str | None = None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_endpoint: str | None = None,
    embedding_signature_version: int | None = None,
    embedding_input_cap: int | None = None,
) -> list[VNextRow]:
    """Rows in ``statuses`` missing a vector or carrying an incompatible signature.

    ``statuses`` is required. The rows listed here are the ones whose text
    goes to the embeddings endpoint, so a caller names the statuses recall can
    return (``MEMORY_SEARCHABLE_STATUSES``) and a forgotten, rejected or
    candidate memory is never listed. A memory that later becomes active is
    listed from then on.

    A memory whose ``valid_to`` has passed is not listed either: recall's
    vector search skips it (``_expiry_clause``, the function called here too), so a
    vector for it could never be returned. It is listed again once ``valid_to``
    is cleared or moved past now.
    """
    if limit < 1:
        raise ContinuityStoreInvariantError("embedding backfill limit must be positive")
    status_values = _embedding_status_values(statuses, caller="list_memories_missing_embeddings")
    expiry_sql, expiry_params = _expiry_clause(False)
    signature_sql, signature_params = _missing_embeddings_clause(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_endpoint=embedding_endpoint,
        embedding_signature_version=embedding_signature_version,
        embedding_input_cap=embedding_input_cap,
    )
    if embedding_input_cap is not None:
        _ensure_embedding_input_cut_sqlite(self.conn)
    status_placeholders = ", ".join("?" for _status in status_values)
    params: list[object] = [
        self.user_id,
        *status_values,
        *expiry_params,
        *signature_params,
        after_id,
        after_id,
        limit,
    ]
    return self._fetch_all(
        f"""
                SELECT {", ".join(MEMORY_COLUMNS)},
                  (embedding IS NOT NULL) AS embedding_present
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN ({status_placeholders}){expiry_sql}
                  AND (
                    embedding IS NULL
                    {signature_sql}
                  )
                  AND (? IS NULL OR id > ?)
                ORDER BY id ASC
                LIMIT ?
                """,
        tuple(params),
    )


def count_memories_missing_embeddings(
    store: Any,
    *,
    statuses: tuple[str, ...],
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    embedding_endpoint: str | None = None,
    embedding_signature_version: int | None = None,
    embedding_input_cap: int | None = None,
) -> int:
    """How many memories in ``statuses`` have no current vector.

    The same test as ``list_memories_missing_embeddings`` (no vector, or a
    signature that is not today's), counted over the statuses given, and over
    the memories whose ``valid_to`` has not passed, so the count is the number
    of memories reindex embeds. With no provider named, a row counts when it has
    no vector at all.
    """
    status_values = _embedding_status_values(statuses, caller="count_memories_missing_embeddings")
    expiry_sql, expiry_params = _expiry_clause(False)
    signature_sql, signature_params = _missing_embeddings_clause(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_endpoint=embedding_endpoint,
        embedding_signature_version=embedding_signature_version,
        embedding_input_cap=embedding_input_cap,
    )
    if embedding_input_cap is not None:
        _ensure_embedding_input_cut_sqlite(store.conn)
    status_placeholders = ", ".join("?" for _status in status_values)
    params: list[object] = [store.user_id, *status_values, *expiry_params, *signature_params]
    row = store._fetch_optional_one(
        f"""
                SELECT COUNT(*) AS n
                FROM memories
                WHERE user_id = ?
                  AND deleted_at IS NULL
                  AND status IN ({status_placeholders}){expiry_sql}
                  AND (
                    embedding IS NULL
                    {signature_sql}
                  )
                """,
        tuple(params),
    )
    return int(row["n"]) if row else 0


for _embedding_method in (
    update_memory_embedding,
    clear_memory_embedding,
    list_memories_missing_embeddings,
):
    _embedding_method.__module__ = "alicebot_api.sqlite_store"
    _embedding_method.__qualname__ = f"SQLiteVNextStore.{_embedding_method.__name__}"
del _embedding_method
