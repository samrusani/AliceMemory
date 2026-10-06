"""MCP backend selection and store-context helpers."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

from alicebot_api.db import user_connection
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.store import ContinuityStore
from alicebot_api.vnext_embeddings import (
    DeferredMemoryEmbedding,
    persist_deferred_memory_embeddings_best_effort,
)
from alicebot_api.vnext_store import PostgresVNextStore

from .types import MCPPreconditionFailedError, MCPRuntimeContext, MCPToolError


_SQLITE_POSTGRES_ONLY_MESSAGE = "this tool requires the Postgres backend; the SQLite on-ramp serves the core tools only"

# Mirrors the alice-memory on-ramp defaults (``onramp.DEFAULT_USER_EMAIL``);
# the on-ramp imports this module, so importing it back would be a cycle.
_SQLITE_DEFAULT_USER_EMAIL = "local@alice"
_SQLITE_DEFAULT_USER_DISPLAY_NAME = _SQLITE_DEFAULT_USER_EMAIL.split("@", 1)[0].replace(".", " ").title() or None


def _is_sqlite_backend(context: MCPRuntimeContext) -> bool:
    return context.database_url.startswith("sqlite:")


def _sqlite_path_from_url(database_url: str) -> str:
    """Extract the database file path from a ``sqlite:///`` URL.

    Accepts both the three-slash (``sqlite:///Users/x/memory.db``) and the
    SQLAlchemy-style four-slash (``sqlite:////Users/x/memory.db``) absolute
    forms; both resolve to ``/Users/x/memory.db``.
    """
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise MCPToolError(f"expected a sqlite:/// database URL, got '{database_url}'")
    if parsed.netloc not in {"", "localhost"}:
        raise MCPToolError("sqlite database URLs must reference a local file path")
    path = unquote(parsed.path)
    while path.startswith("//"):
        path = path[1:]
    if path in {"", "/"}:
        raise MCPToolError("sqlite database URL must include a database file path")
    return path


@contextmanager
def _store_context(context: MCPRuntimeContext):
    if _is_sqlite_backend(context):
        raise MCPPreconditionFailedError(_SQLITE_POSTGRES_ONLY_MESSAGE)
    with user_connection(context.database_url, context.user_id) as conn:
        yield ContinuityStore(conn)


@contextmanager
def _vnext_store_context(context: MCPRuntimeContext):
    from alicebot_api.vnext_label_guard import label_read_scope

    if _is_sqlite_backend(context):
        sqlite_path = _sqlite_path_from_url(context.database_url)
        with sqlite_user_connection(sqlite_path, context.user_id) as conn:
            # Bootstrap the acting user row (idempotent) so a bare
            # ``python -m alicebot_api.mcp_server`` launch against a fresh
            # sqlite:/// database works without the alice-memory on-ramp.
            ensure_sqlite_user(
                conn,
                context.user_id,
                _SQLITE_DEFAULT_USER_EMAIL,
                _SQLITE_DEFAULT_USER_DISPLAY_NAME,
            )
            store = SQLiteVNextStore(conn, context.user_id)
            with label_read_scope(store):
                yield store
        return
    try:
        with user_connection(context.database_url, context.user_id) as conn:
            store = PostgresVNextStore(conn)
            with label_read_scope(store):
                yield store
    except Exception as exc:
        from alicebot_api.vnext_label_writes import label_error_response
        from .types import MCPInvalidRequestError

        answer = label_error_response(exc)
        if answer is not None and answer[0] == 503:
            raise MCPInvalidRequestError(answer[1] + "; HTTP 503; Retry-After: 2") from None
        raise


def _persist_vnext_deferred_embedding_inputs(
    context: MCPRuntimeContext,
    deferred_inputs: Sequence[DeferredMemoryEmbedding],
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Persist optional embeddings after the authoritative tool transaction."""

    persist_deferred_memory_embeddings_best_effort(
        deferred_inputs,
        store_context=lambda: _vnext_store_context(context),
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
    )
