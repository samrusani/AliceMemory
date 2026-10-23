"""Shared fixtures for the tests of ``ALICE_SEARCH_QUALITY=vectors``.

The vectors are a stand-in. A test names the cosine it wants between the query and a chunk, by a tag word the chunk
carries, and the provider answers with a three dimensional vector that has exactly that cosine with the query vector.
Nothing here judges how well a real model ranks anything: these tests are about what the stage does with a ranking.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.source_chunk_embeddings import embed_new_sources, sqlite_store_context
from alicebot_api.source_embedding_policy import (
    SOURCE_EMBEDDINGS_ENV,
    SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
    SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
    SOURCE_EMBEDDINGS_REMOTE_ENV,
)
from alicebot_api.source_chunk_vector_search import reset_breaker
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_stores.sqlite.chunk_vector_cache import CACHE_MAX_MB_ENV, clear_registry

USER_ID = "00000000-0000-0000-0000-000000000001"
MODEL = "scripted-embed"
LOCAL_URL = "http://127.0.0.1:11434/v1"
SEARCH_QUALITY_ENV = "ALICE_SEARCH_QUALITY"
OPEN = ["public", "internal", "private", "unknown"]
QUERY = "zephyr"


def scripted_vector(text: str, cosines: Mapping[str, float], query_text: str) -> list[float]:
    """The vector with the cosine a tag in ``text`` names against the query vector ``[1, 0, 0]``."""

    if text.strip() == query_text:
        return [1.0, 0.0, 0.0]
    for tag, cosine in cosines.items():
        if tag in text:
            return [cosine, math.sqrt(max(1.0 - cosine * cosine, 0.0)), 0.0]
    return [0.0, 0.0, 1.0]


class ScriptedProvider:
    """An in-process provider with scripted cosines. It records the queries and the chunk batches it is sent."""

    provider = "openai_compatible"

    def __init__(
        self,
        cosines: Mapping[str, float] | None = None,
        *,
        query_text: str = QUERY,
        model: str = MODEL,
        base_url: str = LOCAL_URL,
        fail_queries: bool = False,
    ) -> None:
        self.cosines = dict(cosines or {})
        self.query_text = query_text
        self.model = model
        self.base_url = base_url
        self.fail_queries = fail_queries
        self.queries: list[str] = []
        self.batches: list[list[str]] = []

    def embed_text(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if len(texts) == 1 and texts[0].strip() == self.query_text:
            self.queries.append(texts[0])
            if self.fail_queries:
                from alicebot_api.vnext_embeddings import VNextEmbeddingProviderError

                raise VNextEmbeddingProviderError("embeddings request failed: refused")
        else:
            self.batches.append(list(texts))
        return [scripted_vector(text, self.cosines, self.query_text) for text in texts]

    embed_batch_native = embed_batch


class ScriptedServer:
    """The same scripted vectors behind a ``/v1/embeddings`` endpoint on 127.0.0.1."""

    def __init__(self, cosines: Mapping[str, float] | None = None, *, query_text: str = QUERY) -> None:
        self.cosines = dict(cosines or {})
        self.query_text = query_text
        self.texts: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                inputs = json.loads(raw)["input"]
                server.texts.extend(inputs)
                body = json.dumps(
                    {
                        "data": [
                            {"index": index, "embedding": scripted_vector(text, server.cosines, server.query_text)}
                            for index, text in enumerate(inputs)
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> "ScriptedServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def quiet_environment(monkeypatch: pytest.MonkeyPatch, *, opt_in: bool = True) -> None:
    """No embedding configuration but the opt-in; no proxy; empty caches and breaker."""

    for name in (
        SOURCE_EMBEDDINGS_REMOTE_ENV,
        SOURCE_EMBEDDINGS_MAX_SENSITIVITY_ENV,
        SOURCE_EMBEDDINGS_MAX_VECTORS_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_MAX_INPUT_CHARS_ENV,
        CACHE_MAX_MB_ENV,
        SEARCH_QUALITY_ENV,
        "ALICE_RERANKER_BASE_URL",
        "ALICE_RERANKER_MODEL",
        "ALICE_AGENT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    if opt_in:
        monkeypatch.setenv(SOURCE_EMBEDDINGS_ENV, "on")
    else:
        monkeypatch.delenv(SOURCE_EMBEDDINGS_ENV, raising=False)
    monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    clear_registry()
    reset_breaker()


def make_vault(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return database


def mcp_context(database: Path):
    from alicebot_api.mcp.types import MCPRuntimeContext

    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def fixed_id(*parts: object) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(str(part) for part in parts)))


def add_note(
    database: Path,
    name: str,
    chunks: Sequence[str],
    *,
    sensitivity: str = "public",
    domain: str = "project",
    metadata: Mapping[str, object] | None = None,
    captured_at: str = "2026-06-01T00:00:00Z",
) -> str:
    """A source with these chunks and ids that depend only on the names, so two vaults can be compared."""

    with sqlite_user_connection(database, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        source = store.create_source(
            {
                "id": fixed_id("source", name),
                "source_type": "note",
                "title": name,
                "content_hash": hashlib.sha256(f"hash-{name}".encode()).hexdigest(),
                "captured_at": captured_at,
                "domain": domain,
                "sensitivity": sensitivity,
                "metadata_json": dict(metadata or {}),
            }
        )
        for index, text in enumerate(chunks):
            store.create_source_chunk(
                {"id": fixed_id("chunk", name, index), "source_id": source["id"], "chunk_index": index, "text": text}
            )
        return str(source["id"])


def hit_text(tag: str, hits: int, *, words: int = 12) -> str:
    """A chunk of ``words`` words with ``hits`` occurrences of the query word and the tag. Equal length, more hits ranks higher."""

    body = ["zephyr"] * hits + ["filler"] * (words - hits - 1)
    return " ".join([*body, tag])


def plain_text(tag: str, *, words: int = 12) -> str:
    """A chunk with no occurrence of the query word."""

    return " ".join(["harbour"] * (words - 1) + [tag])


def embed_vault(database: Path, provider: ScriptedProvider, *, environ: Mapping[str, str] | None = None) -> None:
    """Embed every live source of the vault with the provider (the opt-in is on in ``environ``)."""

    with sqlite_user_connection(database, USER_ID) as conn:
        ids = [str(row["id"]) for row in conn.execute("SELECT id FROM sources WHERE deleted_at IS NULL ORDER BY id")]
    receipt = embed_new_sources(
        ids,
        store_context=lambda: sqlite_store_context(database, USER_ID),
        environ=dict(environ or {SOURCE_EMBEDDINGS_ENV: "on"}),
        provider=provider,
    )
    assert receipt is not None and receipt.pending == 0, receipt
