from __future__ import annotations

import io
import json

import pytest

import alicebot_api.vnext_embeddings as vnext_embeddings
from alicebot_api.vnext_embeddings import (
    DeferredMemoryEmbedding,
    EMBEDDING_VECTOR_DIMENSIONS,
    OpenAICompatibleEmbeddingProvider,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    attach_memory_embedding,
    attach_memory_embeddings,
    endpoint_fingerprint,
    get_embedding_provider,
    memory_embedding_signature,
    memory_embedding_text,
    pad_embedding_vector,
    persist_deferred_memory_embeddings_best_effort,
    persist_prepared_memory_embeddings,
    prepare_memory_embeddings,
    signed_memory_embedding_update,
)


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_pad_embedding_vector_zero_pads_to_storage_width() -> None:
    padded = pad_embedding_vector([0.5, -0.25])

    assert len(padded) == EMBEDDING_VECTOR_DIMENSIONS
    assert padded[0] == 0.5
    assert padded[1] == -0.25
    assert set(padded[2:]) == {0.0}


def test_pad_embedding_vector_keeps_full_width_vectors_unchanged() -> None:
    vector = [0.001] * EMBEDDING_VECTOR_DIMENSIONS

    assert pad_embedding_vector(vector) == vector


def test_pad_embedding_vector_rejects_oversized_vectors() -> None:
    with pytest.raises(VNextEmbeddingConfigurationError, match="1537 dimensions"):
        pad_embedding_vector([0.1] * (EMBEDDING_VECTOR_DIMENSIONS + 1))


def test_pad_embedding_vector_rejects_empty_and_non_numeric_vectors() -> None:
    with pytest.raises(VNextEmbeddingConfigurationError, match="must not be empty"):
        pad_embedding_vector([])
    with pytest.raises(VNextEmbeddingConfigurationError, match="only numbers"):
        pad_embedding_vector([0.1, "bad"])  # type: ignore[list-item]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_pad_embedding_vector_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(VNextEmbeddingConfigurationError, match="finite numbers"):
        pad_embedding_vector([0.1, value])


def test_get_embedding_provider_returns_none_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_API_KEY", raising=False)

    assert get_embedding_provider() is None

    monkeypatch.setenv("ALICE_EMBEDDINGS_BASE_URL", "http://localhost:11434/v1")
    assert get_embedding_provider() is None

    monkeypatch.setenv("ALICE_EMBEDDINGS_MODEL", "nomic-embed-text")
    provider = get_embedding_provider()
    assert provider is not None
    assert provider.base_url == "http://localhost:11434/v1"
    assert provider.model == "nomic-embed-text"
    assert provider.api_key is None

    monkeypatch.setenv("ALICE_EMBEDDINGS_API_KEY", "sk-local")
    provider_with_key = get_embedding_provider()
    assert provider_with_key is not None
    assert provider_with_key.api_key == "sk-local"


def test_embed_batch_posts_openai_shape_and_pads_vectors(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout, enforce_public_peer):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        body = json.dumps(
            {
                "data": [
                    {"index": 1, "embedding": [0.2, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.3]},
                ]
            }
        ).encode("utf-8")
        return _FakeResponse(body)

    monkeypatch.setattr(vnext_embeddings, "open_provider_url", fake_urlopen)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:11434/v1/",
        model="nomic-embed-text",
        api_key="sk-local",
    )

    vectors = provider.embed_batch(["first text", "second text"])

    assert captured["url"] == "http://localhost:11434/v1/embeddings"
    assert captured["payload"] == {"model": "nomic-embed-text", "input": ["first text", "second text"]}
    assert dict(captured["headers"]).get("Authorization") == "Bearer sk-local"
    assert len(vectors) == 2
    assert vectors[0][:2] == [0.1, 0.3]
    assert vectors[1][:2] == [0.2, 0.4]
    assert all(len(vector) == EMBEDDING_VECTOR_DIMENSIONS for vector in vectors)


def test_embed_batch_normalizes_non_utf8_response_to_typed_provider_error(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vnext_embeddings,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(b"\xff\xfe\xfa"),
    )
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:1234/v1",
        model="local-embed",
    )

    with pytest.raises(VNextEmbeddingProviderError, match="embeddings request failed"):
        provider.embed_batch(["fact"])


@pytest.mark.parametrize(
    "data",
    [
        [
            {"index": 0, "embedding": [0.1]},
            {"index": 0, "embedding": [0.2]},
        ],
        [
            {"index": 5, "embedding": [0.1]},
            {"index": 9, "embedding": [0.2]},
        ],
        [
            {"index": True, "embedding": [0.1]},
            {"index": False, "embedding": [0.2]},
        ],
        [
            {"index": 0, "embedding": [0.1]},
            {"embedding": [0.2]},
        ],
    ],
    ids=["duplicate", "out-of-range", "boolean", "mixed-indexed"],
)
def test_embed_batch_rejects_non_permutation_indices(monkeypatch, data) -> None:
    monkeypatch.setattr(
        vnext_embeddings,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(json.dumps({"data": data}).encode("utf-8")),
    )
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:1234/v1", model="local-embed"
    )

    with pytest.raises(VNextEmbeddingProviderError, match="indices|index every"):
        provider.embed_batch(["first", "second"])


def test_embed_batch_without_indices_preserves_response_order(monkeypatch) -> None:
    monkeypatch.setattr(
        vnext_embeddings,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(
            json.dumps(
                {"data": [{"embedding": [0.1]}, {"embedding": [0.2]}]}
            ).encode("utf-8")
        ),
    )
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://localhost:1234/v1", model="local-embed"
    )

    vectors = provider.embed_batch(["first", "second"])

    assert vectors[0][0] == 0.1
    assert vectors[1][0] == 0.2


def test_embed_batch_omits_authorization_header_without_api_key(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout, enforce_public_peer):
        del timeout
        captured["headers"] = {key.casefold(): value for key, value in request.header_items()}
        return _FakeResponse(json.dumps({"data": [{"index": 0, "embedding": [0.1]}]}).encode("utf-8"))

    monkeypatch.setattr(vnext_embeddings, "open_provider_url", fake_urlopen)
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost:1234/v1", model="local-embed")

    provider.embed_text("local server text")

    assert "authorization" not in captured["headers"]


def test_embed_batch_raises_provider_error_on_bad_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        vnext_embeddings,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(json.dumps({"unexpected": True}).encode("utf-8")),
    )
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost:1234/v1", model="local-embed")

    with pytest.raises(VNextEmbeddingProviderError, match="data array"):
        provider.embed_text("text")


def test_embed_batch_rejects_empty_inputs_and_oversized_batches() -> None:
    provider = OpenAICompatibleEmbeddingProvider(base_url="http://localhost:1234/v1", model="local-embed")

    assert provider.embed_batch([]) == []
    with pytest.raises(VNextEmbeddingConfigurationError, match="non-empty strings"):
        provider.embed_batch(["ok", "   "])
    with pytest.raises(VNextEmbeddingConfigurationError, match="limited to"):
        provider.embed_batch(["text"] * (vnext_embeddings.MAX_EMBEDDINGS_BATCH_SIZE + 1))


def test_memory_embedding_text_joins_title_canonical_text_and_summary() -> None:
    text = memory_embedding_text(
        {"title": "Coffee", "canonical_text": "Sam prefers pour over.", "summary": "Coffee"}
    )

    assert text == "Coffee\nSam prefers pour over."
    assert memory_embedding_text({"title": None, "canonical_text": "  "}) == ""


def test_embedding_signature_distinguishes_endpoints_with_same_labels() -> None:
    # Two endpoints with identical provider+model labels but different base_urls
    # are different coordinate spaces. Their signatures must differ so retrieval
    # never pools vectors from one endpoint against a query embedded by another.
    memory = {"title": "Fact", "canonical_text": "Fact text."}
    endpoint_a = OpenAICompatibleEmbeddingProvider(
        base_url="https://a.example/v1", model="text-embed"
    )
    endpoint_b = OpenAICompatibleEmbeddingProvider(
        base_url="https://b.example/v1", model="text-embed"
    )

    sig_a = memory_embedding_signature(memory, provider=endpoint_a)
    sig_b = memory_embedding_signature(memory, provider=endpoint_b)

    assert sig_a.get("endpoint"), "signature must carry an endpoint fingerprint"
    assert sig_a["endpoint"] != sig_b["endpoint"]
    assert sig_a != sig_b
    # Same endpoint + same labels remains stable (so re-embeds are idempotent).
    endpoint_a_again = OpenAICompatibleEmbeddingProvider(
        base_url="https://a.example/v1/", model="text-embed"
    )
    assert memory_embedding_signature(memory, provider=endpoint_a_again)["endpoint"] == sig_a["endpoint"]


def test_endpoint_fingerprint_normalizes_only_scheme_host_and_default_port() -> None:
    canonical = endpoint_fingerprint("HTTPS://Embed.Example:443/CaseSensitive/V1?Model=AbC")

    assert canonical == endpoint_fingerprint(
        "https://embed.example/CaseSensitive/V1?Model=AbC"
    )
    assert canonical != endpoint_fingerprint(
        "https://embed.example/casesensitive/v1?model=abc"
    )
    assert endpoint_fingerprint("http://EMBED.example:80/v1/") == endpoint_fingerprint(
        "http://embed.example/v1"
    )


def test_signed_memory_embedding_update_is_complete_v2_contract() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://Embed.Example:443/Case/V1",
        model="text-embed",
    )
    memory = {"id": "memory-1", "canonical_text": "Signed fact."}

    update = signed_memory_embedding_update(memory, [0.5, 0.25], provider=provider)

    assert set(update) == {
        "memory_id",
        "vector",
        "provider",
        "model",
        "endpoint",
        "content_sha256",
        "signature_version",
    }
    assert update["memory_id"] == "memory-1"
    assert len(update["vector"]) == EMBEDDING_VECTOR_DIMENSIONS
    assert update["provider"] == "openai_compatible"
    assert update["model"] == "text-embed"
    assert update["endpoint"] == endpoint_fingerprint(provider.base_url)
    assert update["signature_version"] == 2


class _AttachStore:
    def __init__(self) -> None:
        self.embeddings: list[tuple[str, list[float]]] = []
        self.embedding_signatures: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []

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
    ) -> dict[str, object]:
        self.embeddings.append((memory_id, vector))
        self.embedding_signatures.append(
            {
                "provider": provider,
                "model": model,
                "endpoint": endpoint,
                "content_sha256": content_sha256,
                "version": signature_version,
            }
        )
        return {"id": memory_id}

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event


class _StubProvider:
    provider = "stub"
    model = "stub-embedding"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batch_calls: list[list[str]] = []

    def embed_text(self, text: str) -> list[float]:
        if self.fail:
            raise VNextEmbeddingProviderError("embeddings request failed: connection refused")
        return pad_embedding_vector([0.5, 0.5])

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [self.embed_text(text) for text in texts]


def test_attach_memory_embedding_noops_without_provider(monkeypatch) -> None:
    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    monkeypatch.delenv("ALICE_EMBEDDINGS_MODEL", raising=False)
    store = _AttachStore()

    attached = attach_memory_embedding(store, {"id": "memory-1", "canonical_text": "Fact."})

    assert attached is False
    assert store.embeddings == []
    assert store.events == []


def test_attach_memory_embedding_writes_vector_with_provider() -> None:
    store = _AttachStore()

    attached = attach_memory_embedding(
        store,
        {"status": "active", "id": "memory-1", "title": "Fact", "canonical_text": "Fact text."},
        provider=_StubProvider(),
    )

    assert attached is True
    assert store.embeddings[0][0] == "memory-1"
    assert len(store.embeddings[0][1]) == EMBEDDING_VECTOR_DIMENSIONS
    assert store.embedding_signatures == [
        {
            "provider": "stub",
            "model": "stub-embedding",
            "endpoint": "",
            "content_sha256": "059865dccf302f203a30b051d86bc76007a8c7c006702182189d42ea9fbf48b7",
            "version": 2,
        }
    ]


def test_attach_memory_embedding_logs_event_but_never_blocks_on_failure() -> None:
    store = _AttachStore()

    attached = attach_memory_embedding(
        store,
        {"status": "active", "id": "memory-1", "canonical_text": "Fact text."},
        provider=_StubProvider(fail=True),
        actor_type="agent",
        actor_id="hermes",
    )

    assert attached is False
    assert store.embeddings == []
    assert store.events[-1]["event_type"] == "memory.embedding_failed"
    assert store.events[-1]["actor_id"] == "hermes"
    assert store.events[-1]["payload_json"]["error_code"] == "embedding_preparation_failed"
    assert store.events[-1]["payload_json"]["error_message"] == "Memory embedding preparation failed"
    assert "connection refused" not in str(store.events[-1]["payload_json"])


def test_attach_memory_embedding_never_blocks_on_store_write_failure() -> None:
    class FailingStore(_AttachStore):
        def update_memory_embedding(self, **kwargs):
            raise RuntimeError("database temporarily unavailable")

    store = FailingStore()

    attached = attach_memory_embedding(
        store,
        {"status": "active", "id": "memory-1", "canonical_text": "Fact text."},
        provider=_StubProvider(),
    )

    assert attached is False
    assert store.events[-1]["event_type"] == "memory.embedding_failed"
    assert store.events[-1]["payload_json"]["error_code"] == "embedding_persistence_failed"
    assert store.events[-1]["payload_json"]["error_message"] == "Memory embedding persistence failed"
    assert "database temporarily unavailable" not in str(store.events[-1]["payload_json"])


def test_attach_memory_embeddings_batches_provider_call_and_isolates_store_failures() -> None:
    class PartiallyFailingStore(_AttachStore):
        def update_memory_embedding(self, **kwargs):
            if kwargs["memory_id"] == "memory-2":
                raise RuntimeError("one row failed")
            return super().update_memory_embedding(**kwargs)

    store = PartiallyFailingStore()
    provider = _StubProvider()

    attached = attach_memory_embeddings(
        store,
        [
            {"status": "active", "id": "memory-1", "canonical_text": "First fact."},
            {"status": "active", "id": "memory-2", "canonical_text": "Second fact."},
            {"status": "active", "id": "memory-3", "canonical_text": "Third fact."},
        ],
        provider=provider,
    )

    assert attached == 2
    assert len(provider.batch_calls) == 1
    assert len(provider.batch_calls[0]) == 3
    assert [memory_id for memory_id, _vector in store.embeddings] == ["memory-1", "memory-3"]
    assert len([event for event in store.events if event["event_type"] == "memory.embedding_failed"]) == 1


def test_two_phase_embedding_prepares_without_store_then_persists_best_effort() -> None:
    class PartiallyFailingStore(_AttachStore):
        def update_memory_embedding(self, **kwargs):
            if kwargs["memory_id"] == "memory-2":
                raise RuntimeError("one row failed")
            return super().update_memory_embedding(**kwargs)

    inputs = tuple(
        DeferredMemoryEmbedding.from_memory(
            {"status": "active", "id": f"memory-{index}", "canonical_text": f"Fact {index}."}
        )
        for index in range(1, 4)
    )
    provider = _StubProvider()

    preparation = prepare_memory_embeddings(inputs, provider=provider)

    assert len(provider.batch_calls) == 1
    assert len(preparation.prepared) == 3
    assert preparation.failures == ()
    assert preparation.provider == "stub"
    assert preparation.model == "stub-embedding"

    store = PartiallyFailingStore()
    persisted = persist_prepared_memory_embeddings(store, preparation)

    assert persisted == 2
    assert [memory_id for memory_id, _vector in store.embeddings] == ["memory-1", "memory-3"]
    assert len([event for event in store.events if event["event_type"] == "memory.embedding_failed"]) == 1


def test_two_phase_embedding_does_not_count_stale_compare_and_set_miss() -> None:
    class StaleStore(_AttachStore):
        def update_memory_embedding(self, **_kwargs):
            return None

    inputs = (
        DeferredMemoryEmbedding.from_memory(
            {"status": "active", "id": "memory-1", "canonical_text": "Text before an edit."}
        ),
    )
    preparation = prepare_memory_embeddings(inputs, provider=_StubProvider())

    assert persist_prepared_memory_embeddings(StaleStore(), preparation) == 0


def test_two_phase_embedding_carries_provider_failures_to_persistence_log() -> None:
    inputs = (
        DeferredMemoryEmbedding.from_memory(
            {"status": "active", "id": "memory-1", "canonical_text": "Fact text."}
        ),
    )

    preparation = prepare_memory_embeddings(inputs, provider=_StubProvider(fail=True))

    assert preparation.prepared == ()
    assert preparation.failures[0].memory_id == "memory-1"
    assert preparation.failures[0].error_code == "embedding_preparation_failed"
    assert preparation.failures[0].error_message == "Memory embedding preparation failed"
    assert "connection refused" not in repr(preparation.failures[0])
    store = _AttachStore()
    assert persist_prepared_memory_embeddings(store, preparation) == 0
    assert store.events[-1]["event_type"] == "memory.embedding_failed"
    assert store.events[-1]["payload_json"]["error_code"] == "embedding_preparation_failed"
    assert store.events[-1]["payload_json"]["error_message"] == "Memory embedding preparation failed"
    assert "connection refused" not in str(store.events[-1]["payload_json"])


def test_best_effort_deferred_embedding_swallows_connection_acquire_failure(
    caplog,
) -> None:
    class AcquireFailure:
        def __enter__(self):
            raise RuntimeError("pool exhausted")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    deferred = (
        DeferredMemoryEmbedding.from_memory(
            {"status": "active", "id": "memory-1", "canonical_text": "Fact text."}
        ),
    )

    with caplog.at_level("WARNING"):
        persisted = persist_deferred_memory_embeddings_best_effort(
            deferred,
            store_context=AcquireFailure,
            provider=_StubProvider(),
        )

    assert persisted == 0
    assert "persistence failed after the primary commit" in caplog.text
    assert "pool exhausted" in caplog.text


def test_best_effort_deferred_embedding_isolates_store_write_failure() -> None:
    class WriteFailureStore(_AttachStore):
        def update_memory_embedding(self, **_kwargs):
            raise RuntimeError("vector write failed")

    class StoreContext:
        def __init__(self, store: WriteFailureStore) -> None:
            self.store = store

        def __enter__(self) -> WriteFailureStore:
            return self.store

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    store = WriteFailureStore()
    deferred = (
        DeferredMemoryEmbedding.from_memory(
            {"status": "active", "id": "memory-1", "canonical_text": "Fact text."}
        ),
    )

    persisted = persist_deferred_memory_embeddings_best_effort(
        deferred,
        store_context=lambda: StoreContext(store),
        provider=_StubProvider(),
    )

    assert persisted == 0
    assert store.events[-1]["event_type"] == "memory.embedding_failed"
    assert store.events[-1]["payload_json"]["error_code"] == "embedding_persistence_failed"
    assert store.events[-1]["payload_json"]["error_message"] == "Memory embedding persistence failed"
    assert "vector write failed" not in str(store.events[-1]["payload_json"])


def test_best_effort_deferred_embedding_swallows_followup_commit_failure(
    caplog,
) -> None:
    class CommitFailure:
        def __init__(self, store: _AttachStore) -> None:
            self.store = store

        def __enter__(self) -> _AttachStore:
            return self.store

        def __exit__(self, exc_type, exc, tb) -> None:
            raise RuntimeError("commit failed")

    store = _AttachStore()
    deferred = (
        DeferredMemoryEmbedding.from_memory(
            {"status": "active", "id": "memory-1", "canonical_text": "Fact text."}
        ),
    )

    with caplog.at_level("WARNING"):
        persisted = persist_deferred_memory_embeddings_best_effort(
            deferred,
            store_context=lambda: CommitFailure(store),
            provider=_StubProvider(),
        )

    assert persisted == 0
    assert "persistence failed after the primary commit" in caplog.text
    assert "commit failed" in caplog.text
