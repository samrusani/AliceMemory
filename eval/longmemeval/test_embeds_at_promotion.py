"""The harness embeds each memory when it promotes it, not when capture writes it.

Capture writes candidate memories, and since the embedding door sends text only
for a memory recall can return, capture no longer embeds a candidate. The harness
promotes every candidate to ``active`` with ``update_memory`` and then runs the
memory stages' vector search over them, so it embeds the promoted rows itself, in
batches, as a reviewer's acceptance does in the product. The text embedded, and
so the vector made for each memory, is the one capture's embed-on-write made
before: only the moment moves. Under ``sources_only`` nothing is promoted and
nothing is embedded.

The endpoint here is a fake one on 127.0.0.1 that records every text it receives.
No model, no paid API and no other network. Run from the repo root:

    .venv/bin/python -m pytest eval/longmemeval/test_embeds_at_promotion.py -q

Every test names, in its docstring, the change that must make it fail; each of
those changes was made by hand and the test seen to fail.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_EVAL_DIR = Path(__file__).resolve().parent.parent
_API_SRC = _EVAL_DIR.parent / "apps" / "api" / "src"
for _path in (_EVAL_DIR, _API_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from alicebot_api.vnext_embeddings import (  # noqa: E402
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
)

from longmemeval import adapter  # noqa: E402
from longmemeval.dataset import SYNTHETIC_FIXTURE_PATH, load_dataset  # noqa: E402


class _RecordingEndpoint:
    """An OpenAI-shaped ``/v1/embeddings`` endpoint on 127.0.0.1 that records every text it gets."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                inputs = json.loads(raw)["input"]
                outer.texts.extend(inputs)
                body = json.dumps(
                    {
                        "data": [
                            {
                                "index": index,
                                "embedding": [
                                    (byte - 128) / 128.0
                                    for byte in hashlib.sha256(text.encode("utf-8")).digest()[:16]
                                ],
                            }
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
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> "_RecordingEndpoint":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _configure_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (EMBEDDINGS_API_KEY_ENV, EMBEDDINGS_MAX_INPUT_CHARS_ENV):
        monkeypatch.delenv(name, raising=False)
    for name in ("ALICE_RERANKER_BASE_URL", "ALICE_RERANKER_MODEL", "ALICE_RERANKER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _ingest(tmp_path: Path, endpoint: _RecordingEndpoint, monkeypatch: pytest.MonkeyPatch, **choices: str):
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, endpoint.base_url)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "fake-embed")
    sent_when_promotion_starts: list[int] = []
    original = adapter.QuestionRun._promote_candidate_memories

    def spy(self, **kwargs: object) -> int:
        sent_when_promotion_starts.append(len(endpoint.texts))
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(adapter.QuestionRun, "_promote_candidate_memories", spy)
    question = load_dataset(SYNTHETIC_FIXTURE_PATH)[0]
    with adapter.question_run(question, tmp_path / "q.sqlite3", **choices) as run:
        stats = run.ingest()
        promoted_ids = [str(row["id"]) for row in run.store.list_memories(status="active")]
        with_vectors = run.store.list_memory_ids_with_embeddings(promoted_ids)
        candidate_ids = [str(row["id"]) for row in run.store.list_memories(status="candidate")]
        candidates_with_vectors = run.store.list_memory_ids_with_embeddings(candidate_ids)
    return stats, sent_when_promotion_starts, promoted_ids, set(with_vectors), candidate_ids, set(candidates_with_vectors)


def test_capture_sends_nothing_and_promotion_embeds_every_promoted_memory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing reaches the endpoint during capture; each promoted memory is embedded once at promotion.

    The default ``all_candidates`` mode promotes every extracted candidate. When
    promotion starts the endpoint has received nothing. When ingest ends every
    promoted memory has a vector and the endpoint received exactly one text per
    promoted memory, so the number of vectors is what an embed-on-write run made.

    Mutations: delete the ``attach_memory_embeddings`` call at the end of
    ``_promote_candidate_memories`` (the promoted memories have no vector and the
    endpoint received nothing). Or make ``DeferredMemoryEmbedding.is_embeddable``
    return True (capture then sends every candidate, so the endpoint has
    received texts when promotion starts, and then receives each text a second
    time).
    """

    with _RecordingEndpoint() as endpoint:
        stats, sent_at_promotion, promoted_ids, with_vectors, candidate_ids, _ = _ingest(
            tmp_path, endpoint, monkeypatch
        )

    assert stats.candidate_memory_count > 0, "capture extracted no candidates, the test would prove nothing"
    assert sent_at_promotion == [0]
    assert stats.promoted_memory_count == stats.candidate_memory_count == len(promoted_ids)
    assert candidate_ids == []
    assert with_vectors == set(promoted_ids)
    assert len(endpoint.texts) == len(promoted_ids)


def test_sources_only_embeds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing promoted nothing is embedded: candidates are not recall-visible, so none is sent.

    Capture used to embed every candidate whether or not it was ever promoted; a
    ``sources_only`` run, which models a real import, sent them all for vectors
    that no memory stage could read.

    Mutation: make ``DeferredMemoryEmbedding.is_embeddable`` return True (capture
    then sends every candidate, whatever the promotion mode, and the candidates
    hold vectors).
    """

    with _RecordingEndpoint() as endpoint:
        stats, sent_at_promotion, promoted_ids, _, candidate_ids, candidates_with_vectors = _ingest(
            tmp_path, endpoint, monkeypatch, promotion_mode=adapter.PROMOTION_MODE_SOURCES_ONLY
        )

    assert stats.candidate_memory_count > 0, "capture extracted no candidates, the test would prove nothing"
    assert sent_at_promotion == []  # the promotion step is never called
    assert promoted_ids == []
    assert len(candidate_ids) == stats.candidate_memory_count
    assert candidates_with_vectors == set()
    assert endpoint.texts == []
