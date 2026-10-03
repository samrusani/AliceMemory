"""Deterministic local embeddings for the scale benchmark.

Real embed-on-write at 100k memories would mean 100k embedding API calls, so
the benchmark uses a deterministic token-mixture embedding instead: every
token hashes (BLAKE2b) to a fixed unit gaussian vector and a text embeds as
the normalized sum of its token vectors. Texts that share vocabulary land
near each other in cosine space, which gives the vector-search stage and the
consolidation clusterer realistic non-uniform neighborhoods while staying
bit-reproducible across runs and backends.

Two delivery mechanisms, both producing identical vectors:

- :class:`DeterministicEmbeddingProvider` satisfies
  ``alicebot_api.vnext_embeddings.EmbeddingProvider`` and is injected
  directly into ``VNextRetrievalService`` / ``VNextConsolidationService``
  (both take an ``embedding_provider`` argument).
- :class:`stub_embeddings_server` runs a local OpenAI-compatible
  ``/embeddings`` endpoint so ambient ``get_embedding_provider()`` callers
  (memory-commit embed-on-write, and acceptance of a candidate) exercise the real
  ``OpenAICompatibleEmbeddingProvider`` HTTP client path end to end.

Caveat carried into the published results: this makes embedding computation
essentially free, so all reported latencies EXCLUDE real embedding-API time.
"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import blake2b
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from typing import Iterator, Sequence

import numpy as np

ACTIVE_DIMENSIONS = 256
STORAGE_DIMENSIONS = 1536
MODEL_NAME = "det-mix-256"

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_token_cache: dict[str, np.ndarray] = {}
_text_cache: dict[str, list[float]] = {}
_TEXT_CACHE_MAX = 20_000


def _token_vector(token: str) -> np.ndarray:
    cached = _token_cache.get(token)
    if cached is not None:
        return cached
    seed = int.from_bytes(blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
    vector = np.random.default_rng(seed).standard_normal(ACTIVE_DIMENSIONS).astype(np.float32)
    vector /= float(np.linalg.norm(vector)) or 1.0
    _token_cache[token] = vector
    return vector


def deterministic_vector(text: str) -> list[float]:
    """1536-dim storage-width vector for ``text`` (first 256 dims active)."""
    cached = _text_cache.get(text)
    if cached is not None:
        return cached
    tokens = _TOKEN_PATTERN.findall(text.casefold()) or ["empty"]
    accumulated = np.zeros(ACTIVE_DIMENSIONS, dtype=np.float32)
    for token in tokens:
        accumulated += _token_vector(token)
    norm = float(np.linalg.norm(accumulated))
    if norm > 0.0:
        accumulated /= norm
    padded = np.zeros(STORAGE_DIMENSIONS, dtype=np.float32)
    padded[:ACTIVE_DIMENSIONS] = accumulated
    vector = [float(value) for value in padded]
    if len(_text_cache) < _TEXT_CACHE_MAX:
        _text_cache[text] = vector
    return vector


class DeterministicEmbeddingProvider:
    """In-process ``EmbeddingProvider`` built on :func:`deterministic_vector`."""

    provider = "deterministic_local"
    model = MODEL_NAME
    base_url = "deterministic://alice-scale/v1"

    def embed_text(self, text: str) -> list[float]:
        return deterministic_vector(text)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [deterministic_vector(text) for text in texts]


class _EmbeddingsHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if not self.path.endswith("/embeddings"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length))
            raw_input = payload.get("input")
            texts = [raw_input] if isinstance(raw_input, str) else list(raw_input)
            data = [
                {"object": "embedding", "index": index, "embedding": deterministic_vector(str(text))}
                for index, text in enumerate(texts)
            ]
        except (json.JSONDecodeError, TypeError, ValueError):
            self.send_error(400)
            return
        body = json.dumps({"object": "list", "model": MODEL_NAME, "data": data}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep benchmark output clean


@contextmanager
def stub_embeddings_server() -> Iterator[str]:
    """Local OpenAI-compatible ``/embeddings`` endpoint; yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingsHandler)
    thread = threading.Thread(target=server.serve_forever, name="scale-embeddings-stub", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


__all__ = [
    "ACTIVE_DIMENSIONS",
    "DeterministicEmbeddingProvider",
    "MODEL_NAME",
    "STORAGE_DIMENSIONS",
    "deterministic_vector",
    "stub_embeddings_server",
]
