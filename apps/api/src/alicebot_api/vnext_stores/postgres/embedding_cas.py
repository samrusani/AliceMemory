"""PostgreSQL embedding compare-and-swap store seam."""

from __future__ import annotations

import math
from collections.abc import Sequence

from psycopg.types.json import Jsonb

from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import (
    EMBEDDING_SIGNATURE_METADATA_KEY,
    EMBEDDING_TRUNCATED_SIGNATURE_KEY,
)
from alicebot_api.vnext_recall_visibility import POSTGRES_UNEXPIRED_SQL
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_stores.postgres.columns import MEMORY_COLUMNS

VNextRow = dict[str, object]


# CPython 3.12's fixed Unicode whitespace table used by ``str.strip()``.
# PostgreSQL's POSIX ``[[:space:]]`` class is locale-dependent and omits the
# U+001C-U+001F controls, so it cannot safely guard embedding compare-and-swap
# freshness. Keep this table in lockstep with migration 0090 without importing
# migration code into the runtime package.
_PYTHON_312_STRIP_CODEPOINTS = (
    0x0009,
    0x000A,
    0x000B,
    0x000C,
    0x000D,
    0x001C,
    0x001D,
    0x001E,
    0x001F,
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    0x2000,
    0x2001,
    0x2002,
    0x2003,
    0x2004,
    0x2005,
    0x2006,
    0x2007,
    0x2008,
    0x2009,
    0x200A,
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
)
_PYTHON_312_STRIP_CHARS_SQL = " || ".join(f"chr({codepoint})" for codepoint in _PYTHON_312_STRIP_CODEPOINTS)


def _python_312_strip_sql(expression: str) -> str:
    return f"btrim({expression}, {_PYTHON_312_STRIP_CHARS_SQL})"


# Exact SQL mirror of vnext_embeddings.memory_embedding_text/content_sha256:
# trim title/canonical/summary, omit blanks, and preserve the first occurrence
# when fields contain identical text.
_MEMORY_EMBEDDING_CONTENT_SHA256_SQL = f"""
(
  SELECT encode(
    digest(
      concat_ws(
        E'\\n',
        normalized.title,
        CASE
          WHEN normalized.canonical_text IS DISTINCT FROM normalized.title
            THEN normalized.canonical_text
        END,
        CASE
          WHEN normalized.summary IS DISTINCT FROM normalized.title
           AND normalized.summary IS DISTINCT FROM normalized.canonical_text
            THEN normalized.summary
        END
      ),
      'sha256'
    ),
    'hex'
  )
  FROM (
    SELECT
      NULLIF({_python_312_strip_sql("title")}, '') AS title,
      NULLIF({_python_312_strip_sql("canonical_text")}, '') AS canonical_text,
      NULLIF({_python_312_strip_sql("summary")}, '') AS summary
  ) AS normalized
)
"""


# The same normalization as above, ending in the length in characters of the
# text embedded for the row (``len(memory_embedding_text(row))``) instead of its
# digest. ``list_memories_missing_embeddings`` compares it with the input cap.
_MEMORY_EMBEDDING_TEXT_CHARS_SQL = f"""
(
  SELECT char_length(
    concat_ws(
      E'\\n',
      normalized.title,
      CASE
        WHEN normalized.canonical_text IS DISTINCT FROM normalized.title
          THEN normalized.canonical_text
      END,
      CASE
        WHEN normalized.summary IS DISTINCT FROM normalized.title
         AND normalized.summary IS DISTINCT FROM normalized.canonical_text
          THEN normalized.summary
      END
    )
  )
  FROM (
    SELECT
      NULLIF({_python_312_strip_sql("title")}, '') AS title,
      NULLIF({_python_312_strip_sql("canonical_text")}, '') AS canonical_text,
      NULLIF({_python_312_strip_sql("summary")}, '') AS summary
  ) AS normalized
)
"""


def _vector_literal(vector: list[float]) -> str:
    if not vector:
        raise ContinuityStoreInvariantError("embedding vectors must not be empty")
    values: list[float] = []
    for value in vector:
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ContinuityStoreInvariantError("embedding vectors must contain only numbers") from exc
        if not math.isfinite(normalized):
            raise ContinuityStoreInvariantError("embedding vectors must contain only finite numbers")
        values.append(normalized)
    return "[" + ",".join(repr(value) for value in values) + "]"


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
    signature_values = (provider, model, content_sha256)
    if any(value is not None for value in signature_values):
        if not all(isinstance(value, str) and value for value in signature_values):
            raise ContinuityStoreInvariantError(
                "embedding provider, model, and content_sha256 must be supplied together"
            )
        signature_metadata: JsonObject = {
            "version": signature_version,
            "provider": provider,
            "model": model,
            "endpoint": endpoint if isinstance(endpoint, str) else "",
            "content_sha256": content_sha256,
        }
        if truncated_to_chars is not None:
            signature_metadata[EMBEDDING_TRUNCATED_SIGNATURE_KEY] = truncated_to_chars
        return self._fetch_optional_one(
            f"""
                    UPDATE memories
                    SET embedding_vector = %s::vector,
                        metadata_json = jsonb_set(
                          metadata_json,
                          '{{{EMBEDDING_SIGNATURE_METADATA_KEY}}}',
                          %s::jsonb,
                          true
                        )
                    WHERE id = %s::uuid
                      AND deleted_at IS NULL
                      AND ({_MEMORY_EMBEDDING_CONTENT_SHA256_SQL}) = %s
                    RETURNING id
                    """,
            (
                _vector_literal(vector),
                Jsonb(signature_metadata),
                memory_id,
                content_sha256,
            ),
        )
    return self._fetch_optional_one(
        f"""
                UPDATE memories
                SET embedding_vector = %s::vector
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING id
                """,
        (_vector_literal(vector), memory_id),
    )


def clear_memory_embedding(self, *, memory_id: str) -> VNextRow | None:
    """Invalidate content-derived vector state before a text mutation.

        The caller may immediately repopulate it through the configured
        provider. A missing/failed provider must leave NULL, never an
        embedding for the memory's previous text.
        """
    return self._fetch_optional_one(
        f"""
                UPDATE memories
                SET embedding_vector = NULL,
                    metadata_json = metadata_json - '{EMBEDDING_SIGNATURE_METADATA_KEY}'
                WHERE id = %s::uuid
                  AND deleted_at IS NULL
                RETURNING id
                """,
        (memory_id,),
    )


def _embedding_status_values(statuses: Sequence[str], *, caller: str) -> list[str]:
    """The statuses a vector is made for, checked before a query is built.

    Required by the listing, with no default, so a caller that forgets it
    fails here and cannot send every row's text to the embeddings endpoint. A
    bare string is refused (it would be read as one status per character), and
    so is an empty list.
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
    vector search skips it (the same ``POSTGRES_UNEXPIRED_SQL`` test), so a
    vector for it could never be returned. It is listed again once ``valid_to``
    is cleared or moved past now.
    """
    status_values = _embedding_status_values(statuses, caller="list_memories_missing_embeddings")
    status_placeholders = ", ".join("%s" for _status in status_values)
    signature_sql = ""
    signature_params: list[object] = []
    if embedding_input_cap is not None and embedding_provider is None and embedding_model is None:
        raise ContinuityStoreInvariantError(
            "embedding_input_cap requires embedding_provider and embedding_model"
        )
    if embedding_provider is not None or embedding_model is not None:
        if not embedding_provider or not embedding_model:
            raise ContinuityStoreInvariantError("embedding_provider and embedding_model must be supplied together")
        signature_sql = f"""
                  OR metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'provider'
                       IS DISTINCT FROM %s
                  OR metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'model'
                       IS DISTINCT FROM %s
                  OR metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'content_sha256'
                       IS DISTINCT FROM ({_MEMORY_EMBEDDING_CONTENT_SHA256_SQL})
            """
        signature_params.extend((embedding_provider, embedding_model))
        if embedding_endpoint is not None:
            # A vector embedded via a different endpoint is stale and must be
            # re-embedded for the current endpoint's coordinate space.
            signature_sql += f"""
                  OR metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'endpoint'
                       IS DISTINCT FROM %s
                """
            signature_params.append(embedding_endpoint)
        if embedding_signature_version is not None:
            signature_sql += f"""
                  OR metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> 'version'
                       IS DISTINCT FROM %s
                """
            signature_params.append(str(embedding_signature_version))
        if embedding_input_cap is not None:
            # The label a vector made now would carry: the cap for a text
            # longer than it, none for a text that fits. A row whose stored
            # label differs was made under another cap and is made again.
            signature_sql += f"""
                  OR metadata_json -> '{EMBEDDING_SIGNATURE_METADATA_KEY}' ->> '{EMBEDDING_TRUNCATED_SIGNATURE_KEY}'
                       IS DISTINCT FROM (
                         CASE WHEN ({_MEMORY_EMBEDDING_TEXT_CHARS_SQL}) > %s THEN %s::text END
                       )
                """
            signature_params.extend((embedding_input_cap, str(embedding_input_cap)))
    params: list[object] = [*status_values, *signature_params, after_id, after_id, limit]
    return self._fetch_all(
        f"""
                SELECT {MEMORY_COLUMNS},
                  (embedding_vector IS NOT NULL) AS embedding_present
                FROM memories
                WHERE deleted_at IS NULL
                  AND status IN ({status_placeholders})
                  AND {POSTGRES_UNEXPIRED_SQL}
                  AND (
                    embedding_vector IS NULL
                    {signature_sql}
                  )
                  AND (%s::uuid IS NULL OR id > %s::uuid)
                ORDER BY id ASC
                LIMIT %s
                """,
        tuple(params),
    )


for _embedding_method in (
    update_memory_embedding,
    clear_memory_embedding,
    list_memories_missing_embeddings,
):
    _embedding_method.__module__ = "alicebot_api.vnext_store"
    _embedding_method.__qualname__ = f"PostgresVNextStore.{_embedding_method.__name__}"
del _embedding_method
