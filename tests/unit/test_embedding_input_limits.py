"""Embedding input limits: one over-long memory must not sink its neighbours.

Every test that talks to an endpoint talks to a fake one on 127.0.0.1, started
here. The fake can refuse a text over a size, cut every text silently, answer
every request with an error, or answer with an error body that holds a
credential. Nothing in this file reaches the network or a paid API.

Each test names, in its docstring, the change to the code that must fail it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest

from alicebot_api import vnext_embeddings
from alicebot_api.onramp import bootstrap_database, main as onramp_main
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_embeddings import (
    DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS,
    EMBEDDING_FAILED_IDS_LISTED,
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
    LISTED_ID_WITHHELD,
    PROVIDER_MESSAGE_WITHHELD,
    PROVIDER_REASON_MAX_CHARS,
    DeferredMemoryEmbedding,
    EmbeddingTextFailure,
    MemoryEmbeddingFailure,
    OpenAICompatibleEmbeddingProvider,
    VNextEmbeddingConfigurationError,
    VNextEmbeddingProviderError,
    cut_embedding_text,
    embed_batch_isolating,
    get_embedding_provider,
    listable_memory_id,
    memory_embedding_content_sha256,
    memory_embedding_signature,
    memory_embedding_text,
    pad_embedding_vector,
    persist_prepared_memory_embeddings,
    prepare_memory_embeddings,
    resolve_embeddings_max_input_chars,
    signed_memory_embedding_update,
    summarize_embedding_failures,
)

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
HTTP_PREFIX = "embeddings endpoint returned HTTP 400: "


def _vec(text: str) -> list[float]:
    """The fake endpoint's deterministic vector for a text."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(byte - 128) / 128.0 for byte in digest[:16]]


def _text(length: int, seed: str = "a") -> str:
    """A text of exactly ``length`` characters with no whitespace to strip."""
    return (seed + "x" * length)[:length]


def _fake_credential() -> str:
    """A credential-shaped string built at runtime, so no source line holds one."""
    return "s" + "k-" + hashlib.sha256(b"embedding-input-limits-fixture").hexdigest()[:40]


class _FakeEmbeddingsServer:
    """An OpenAI-shaped ``/v1/embeddings`` endpoint on 127.0.0.1.

    Modes:
      accept       every text is embedded
      reject       a request holding any text longer than ``limit`` gets HTTP 400
                   with an OpenAI-style context-length error
      truncate     every text is cut to ``limit`` characters before its vector is
                   made, and the request succeeds (a silently cutting endpoint)
      always       every request gets ``status`` with ``message``, probe included
      message      the same as ``always``; kept apart so a test reads as what it means
    """

    def __init__(
        self,
        mode: str = "accept",
        *,
        limit: int = 2000,
        status: int = 400,
        message: str = "model not found",
    ) -> None:
        self.mode = mode
        self.limit = limit
        self.status = status
        self.message = message
        self.requests: list[dict[str, object]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence
                return

            def _send(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                inputs = json.loads(raw)["input"]
                lens = [len(text) for text in inputs]
                entry: dict[str, object] = {"lens": lens, "texts": inputs}
                server.requests.append(entry)
                if server.mode in ("always", "message"):
                    entry["status"] = server.status
                    self._send(server.status, {"error": {"message": server.message}})
                    return
                over = [index for index, length in enumerate(lens) if length > server.limit]
                if server.mode == "reject" and over:
                    entry["status"] = 400
                    self._send(
                        400,
                        {
                            "error": {
                                "message": (
                                    f"This model's maximum context length is {server.limit} "
                                    f"characters, however input {over[0]} has {lens[over[0]]}."
                                ),
                                "type": "invalid_request_error",
                                "code": "context_length_exceeded",
                            }
                        },
                    )
                    return
                entry["status"] = 200
                seen = [text[: server.limit] if server.mode == "truncate" else text for text in inputs]
                self._send(
                    200,
                    {"data": [{"index": i, "embedding": _vec(text)} for i, text in enumerate(seen)]},
                )

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> "_FakeEmbeddingsServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _provider(server: _FakeEmbeddingsServer, **kwargs: object) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(base_url=server.base_url, model="fake-embed", **kwargs)  # type: ignore[arg-type]


# --- the cap ------------------------------------------------------------------


def test_provider_cuts_each_text_to_the_cap_before_sending() -> None:
    """A 5,000-character text reaches the endpoint as its first 1,000 characters.

    Mutation: send the text whole in ``embed_batch`` (drop ``cut_embedding_text``).
    The endpoint refuses the 5,000-character text with HTTP 400 and this fails.
    """

    long_text = _text(5000)
    with _FakeEmbeddingsServer("reject", limit=1000) as server:
        vectors = _provider(server, max_input_chars=1000).embed_batch([long_text, "short"])

    assert server.requests[0]["lens"] == [1000, 5]
    assert server.requests[0]["texts"][0] == long_text[:1000]  # type: ignore[index]
    assert vectors[0][:16] == _vec(long_text[:1000])
    assert vectors[1][:16] == _vec("short")


def test_provider_cuts_a_query_too_so_recall_keeps_its_vector_stage() -> None:
    """``embed_text`` (the query path) is cut like a stored text.

    Mutation: apply the cut only in a caller of ``embed_batch`` and not inside it.
    A long recall query then reaches the endpoint whole, is refused, and this fails.
    """

    with _FakeEmbeddingsServer("reject", limit=1000) as server:
        vector = _provider(server, max_input_chars=1000).embed_text(_text(4000, "q"))

    assert vector[:16] == _vec(_text(4000, "q")[:1000])
    assert server.requests[0]["lens"] == [1000]


def test_cap_boundary_keeps_a_text_of_exactly_the_cap_and_cuts_one_more() -> None:
    """300 characters pass whole, 301 become 300.

    Mutation: cut one character short (``text[: max_chars - 1]``), or cut at
    ``>=`` and lose the last character of a text that fits. Either way the
    lengths here change and this fails.
    """

    assert cut_embedding_text("b" * 300, 300) == "b" * 300
    assert cut_embedding_text("c" * 301, 300) == "c" * 300
    assert cut_embedding_text("short", None) == "short"
    with _FakeEmbeddingsServer("accept") as server:
        _provider(server, max_input_chars=300).embed_batch(["b" * 300, "c" * 301])
    assert server.requests[0]["lens"] == [300, 300]


def test_cut_does_not_leave_trailing_whitespace_and_never_empties_a_text() -> None:
    """The cut text ends on a non-space, and a text of leading spaces is not emptied.

    Mutation: drop the ``rstrip`` (the first assertion fails) or drop the
    ``or head`` fallback (the second returns an empty string and fails).
    """

    assert cut_embedding_text("ab" + " " * 10 + "cd", 8) == "ab"
    assert cut_embedding_text(" " * 20 + "z", 8) == " " * 8


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("  ", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("1500", 1500),
        (" 256 ", 256),
        ("1000000", 1_000_000),
        ("255", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("1000001", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("0", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("-5", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("abc", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
        ("12.5", DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS),
    ],
)
def test_cap_environment_value_is_taken_in_range_and_ignored_out_of_range(
    raw: str | None, expected: int
) -> None:
    """A usable value is taken as given; an unusable one gives the default.

    Mutation: drop the range check in ``resolve_embeddings_max_input_chars``. A cap of
    0 or 1,000,001, or one below the floor, is then accepted and this fails.
    """

    assert resolve_embeddings_max_input_chars(raw) == expected


def test_an_unusable_cap_value_warns_once_not_on_every_provider_build(monkeypatch, caplog) -> None:
    """A typo in the cap is reported once per value, not on every recall.

    Mutation: drop the ``_WARNED_INPUT_CAP_VALUES`` check. The warning is then
    written on each call and this fails.
    """

    monkeypatch.setattr(vnext_embeddings, "_WARNED_INPUT_CAP_VALUES", set())
    with caplog.at_level("WARNING", logger="alicebot_api.vnext_embeddings"):
        for _ in range(3):
            assert resolve_embeddings_max_input_chars("lots") == DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS
        assert resolve_embeddings_max_input_chars("12") == DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert all(EMBEDDINGS_MAX_INPUT_CHARS_ENV in message for message in messages)


def test_provider_reads_the_cap_from_the_environment(monkeypatch) -> None:
    """``get_embedding_provider`` hands the environment cap to the provider.

    Mutation: build the provider without ``max_input_chars=``. The provider then
    keeps the default and the 1,500 here fails.
    """

    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, "http://127.0.0.1:9/v1")
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, "fake-embed")
    monkeypatch.delenv(EMBEDDINGS_API_KEY_ENV, raising=False)
    monkeypatch.delenv(EMBEDDINGS_MAX_INPUT_CHARS_ENV, raising=False)
    provider = get_embedding_provider()
    assert provider is not None
    assert provider.max_input_chars == DEFAULT_EMBEDDINGS_MAX_INPUT_CHARS == 8000

    monkeypatch.setenv(EMBEDDINGS_MAX_INPUT_CHARS_ENV, "1500")
    provider = get_embedding_provider()
    assert provider is not None
    assert provider.max_input_chars == 1500


@pytest.mark.parametrize("bad", [0, 255, 1_000_001, True, "1000"])
def test_provider_constructor_rejects_an_unusable_cap(bad: object) -> None:
    """A cap that is not a whole number in range is a configuration error.

    Mutation: drop the range check in ``OpenAICompatibleEmbeddingProvider.__init__``.
    """

    with pytest.raises(VNextEmbeddingConfigurationError, match="max_input_chars"):
        OpenAICompatibleEmbeddingProvider(
            base_url="http://127.0.0.1:9/v1", model="m", max_input_chars=bad  # type: ignore[arg-type]
        )


# --- the signature ------------------------------------------------------------


def test_signature_labels_a_cut_text_and_hashes_the_whole_text() -> None:
    """A cut text carries ``truncated_to_chars``; a text that fits carries nothing.

    The digest is of the whole text either way, so an edit past the cut is seen.
    Mutation: omit the label from ``memory_embedding_signature`` (the first
    assertion fails), always add it (the short-text assertions fail), or hash the
    cut text (the digest assertion fails).
    """

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:9/v1", model="m", max_input_chars=1000
    )
    long_memory = {"id": "m-long", "canonical_text": _text(1500)}
    short_memory = {"id": "m-short", "canonical_text": _text(1000)}

    long_signature = memory_embedding_signature(long_memory, provider=provider)
    short_signature = memory_embedding_signature(short_memory, provider=provider)
    assert long_signature["truncated_to_chars"] == 1000
    assert "truncated_to_chars" not in short_signature
    assert long_signature["content_sha256"] == memory_embedding_content_sha256(long_memory)

    long_update = signed_memory_embedding_update(long_memory, [0.5, 0.25], provider=provider)
    short_update = signed_memory_embedding_update(short_memory, [0.5, 0.25], provider=provider)
    assert long_update["truncated_to_chars"] == 1000
    assert "truncated_to_chars" not in short_update
    assert set(short_update) == {
        "memory_id",
        "vector",
        "provider",
        "model",
        "endpoint",
        "content_sha256",
        "signature_version",
    }


def test_provider_that_declares_no_cap_signs_whole_text_vectors() -> None:
    """A provider with no ``max_input_chars`` is not cut and not labelled.

    Mutation: read a default cap when the provider has none. A test double with
    a long memory then gets a label it never earned, and this fails.
    """

    class NoCapProvider:
        provider = "stub"
        model = "stub-embed"
        base_url = "http://127.0.0.1:9/v1"

    update = signed_memory_embedding_update(
        {"id": "m", "canonical_text": _text(50_000)}, [0.5], provider=NoCapProvider()
    )
    assert "truncated_to_chars" not in update


def test_silently_cutting_endpoint_gives_the_vector_of_the_head_alice_chose() -> None:
    """With a cap under the endpoint's own limit, the vector is of Alice's head, labelled.

    The endpoint cuts at 1,500 without saying so. Alice cuts first, at 1,000, so
    the vector is of a known text and the signature says it was cut.
    Mutation: drop the cut in ``embed_batch``. The endpoint then cuts at 1,500,
    the vector is of the first 1,500 characters, and this fails. Drop the label
    and the ``truncated_to_chars`` assertion fails.
    """

    provider_text = _text(5000)
    memory = {"id": "m-1", "canonical_text": provider_text}
    with _FakeEmbeddingsServer("truncate", limit=1500) as server:
        provider = _provider(server, max_input_chars=1000)
        preparation = prepare_memory_embeddings(
            (DeferredMemoryEmbedding.from_memory(memory),), provider=provider
        )

    assert preparation.failures == ()
    prepared = preparation.prepared[0]
    assert list(prepared.vector[:16]) == _vec(provider_text[:1000])
    assert prepared.truncated_to_chars == 1000
    assert prepared.to_update()["truncated_to_chars"] == 1000
    assert prepared.content_sha256 == memory_embedding_content_sha256(memory)


# --- isolating a refused text -------------------------------------------------


def _is_vector(result: object) -> bool:
    return isinstance(result, list)


def test_one_refused_text_does_not_sink_its_batch_and_is_named_with_the_reason() -> None:
    """One text over the endpoint's limit fails alone; the other seven get vectors.

    Mutation: return a failure for every text when the batch fails (no bisection).
    The seven neighbours then have no vector and this fails.
    """

    texts = [_text(40, f"n{index}") for index in range(8)]
    texts[5] = _text(3000, "big")
    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        results = embed_batch_isolating(_provider(server), texts)

    assert [_is_vector(result) for result in results] == [True] * 5 + [False] + [True] * 2
    for index in (0, 1, 2, 3, 4, 6, 7):
        assert results[index][:16] == _vec(texts[index])  # type: ignore[index]
    failure = results[5]
    assert isinstance(failure, EmbeddingTextFailure)
    assert failure.isolated is True
    assert failure.status == 400
    assert failure.reason is not None
    assert failure.reason.startswith(HTTP_PREFIX)
    assert "maximum context length is 2000" in failure.reason
    # whole batch, one probe, and a handful of halves: far fewer than one per text twice over
    assert len(server.requests) <= 1 + 1 + len(texts)


def test_two_refused_texts_are_both_named() -> None:
    """Two refused texts at different places each fail alone.

    Mutation: stop after the first refused text is found. The second then has no
    entry or a vector it should not have, and this fails.
    """

    texts = [_text(40, f"n{index}") for index in range(8)]
    texts[1] = _text(3000, "bigone")
    texts[6] = _text(3000, "bigtwo")
    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        results = embed_batch_isolating(_provider(server), texts)

    failed = [index for index, result in enumerate(results) if isinstance(result, EmbeddingTextFailure)]
    assert failed == [1, 6]
    assert all(_is_vector(results[index]) for index in (0, 2, 3, 4, 5, 7))


def test_a_failure_that_is_not_about_one_text_is_not_split() -> None:
    """A model-not-found 400 on every request costs two requests, not hundreds.

    The batch fails, the probe of one trivial text fails too, so no split is tried.
    Mutation: drop the probe in ``embed_batch_isolating``. The batch is then split
    to single texts, sixteen texts take more than two requests, and this fails.
    """

    texts = [_text(40, f"n{index}") for index in range(16)]
    with _FakeEmbeddingsServer("always", status=400, message="model not found") as server:
        results = embed_batch_isolating(_provider(server), texts)

    assert len(server.requests) == 2
    assert all(isinstance(result, EmbeddingTextFailure) for result in results)
    assert all(result.isolated is False for result in results)  # type: ignore[union-attr]
    assert all("model not found" in (result.reason or "") for result in results)  # type: ignore[union-attr]


@pytest.mark.parametrize("status", [401, 404, 429, 500, 503])
def test_only_content_statuses_are_split(status: int) -> None:
    """A 401, 404, 429 or 5xx is the endpoint's state, not a text's, and is not split.

    Mutation: add 5xx or 429 to ``INPUT_REJECTION_HTTP_STATUSES``. The batch is
    then probed (a second request) and this fails.
    """

    texts = [_text(40, f"n{index}") for index in range(4)]
    with _FakeEmbeddingsServer("always", status=status, message="try later") as server:
        results = embed_batch_isolating(_provider(server), texts)

    assert len(server.requests) == 1
    assert all(isinstance(result, EmbeddingTextFailure) and result.status == status for result in results)


def test_isolating_a_batch_of_all_refused_texts_takes_fewer_than_two_requests_per_text() -> None:
    """Eight texts that are all refused cost the batch, one probe and fourteen halves.

    The probe passes (it is short), so the batch is split to single texts, and
    every text is refused alone. A binary split of eight texts is fourteen
    requests, so sixteen in all, and every text is named as refused alone.
    Mutation: probe again at each level, or retry a refused single text. The
    request count then passes sixteen and this fails.
    """

    texts = [_text(3000, f"big{index}") for index in range(8)]
    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        results = embed_batch_isolating(_provider(server), texts)

    assert len(server.requests) == 1 + 1 + 14 == 2 * len(texts)
    assert all(isinstance(result, EmbeddingTextFailure) and result.isolated for result in results)


def test_a_half_that_fails_for_another_reason_is_not_called_isolated() -> None:
    """If a half fails with a status that is not about content, it is not split and not isolated.

    The endpoint refuses the whole batch for length, then answers 503 for one
    half. Mutation: mark every failure isolated. The 503 half is then reported
    as refused alone and this fails.
    """

    class FlakyProvider:
        provider = "stub"
        model = "stub-embed"
        base_url = "http://127.0.0.1:9/v1"

        def embed_batch(self, texts):
            if len(texts) == 4 or texts == ["alice embedding probe"]:
                if len(texts) == 4 and texts[0].startswith("late"):
                    raise VNextEmbeddingProviderError("embeddings endpoint returned HTTP 503", status=503)
                if len(texts) == 4:
                    return [pad_embedding_vector([0.5]) for _ in texts]
                return [pad_embedding_vector([0.5])]
            raise VNextEmbeddingProviderError("embeddings endpoint returned HTTP 400", status=400)

    texts = ["early1", "early2", "early3", "early4", "late1", "late2", "late3", "late4"]
    results = embed_batch_isolating(FlakyProvider(), texts)  # type: ignore[arg-type]

    assert [_is_vector(result) for result in results] == [True] * 4 + [False] * 4
    assert all(
        isinstance(result, EmbeddingTextFailure) and result.status == 503 and result.isolated is False
        for result in results[4:]
    )


def test_a_single_text_that_fails_is_reported_isolated_without_a_probe() -> None:
    """A batch of one has nothing to split: one request, the reason, isolated.

    Mutation: probe or retry a batch of one. The request count changes and this fails.
    """

    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        results = embed_batch_isolating(_provider(server), [_text(3000)])

    assert len(server.requests) == 1
    failure = results[0]
    assert isinstance(failure, EmbeddingTextFailure)
    assert failure.isolated is True
    assert "maximum context length" in (failure.reason or "")


def test_wrong_vector_count_is_a_failure_not_a_split() -> None:
    """A provider that returns the wrong number of vectors fails the batch whole.

    Mutation: treat a count mismatch as input-specific and split. The request
    count is then more than one and this fails.
    """

    class ShortProvider:
        provider = "stub"
        model = "stub-embed"
        base_url = "http://127.0.0.1:9/v1"
        calls = 0

        def embed_batch(self, texts):
            ShortProvider.calls += 1
            return [pad_embedding_vector([0.5])]

    results = embed_batch_isolating(ShortProvider(), ["one", "two", "three"])  # type: ignore[arg-type]
    assert ShortProvider.calls == 1
    assert all(isinstance(result, EmbeddingTextFailure) for result in results)


# --- the reason ---------------------------------------------------------------


def test_the_reason_keeps_status_and_at_most_300_characters_of_the_message() -> None:
    """A 5,000-character error message is cut to 300 characters.

    Mutation: drop the cut in ``sanitize_provider_message``. The reason then
    holds the whole message and this fails.
    """

    message = ("lengthy " * 700).strip()
    with _FakeEmbeddingsServer("always", status=400, message=message) as server:
        with pytest.raises(VNextEmbeddingProviderError) as excinfo:
            _provider(server).embed_batch(["one text"])

    assert excinfo.value.status == 400
    text = str(excinfo.value)
    assert text.startswith(HTTP_PREFIX)
    kept = text[len(HTTP_PREFIX) :]
    assert 0 < len(kept) <= PROVIDER_REASON_MAX_CHARS == 300
    assert kept.endswith("...")


def test_a_credential_in_the_error_body_is_withheld_everywhere() -> None:
    """An error body that holds a credential never reaches the message or the reason.

    The credential is built at runtime. Mutation: drop the ``credential_verdict``
    call in ``sanitize_provider_message``. The credential then appears in the
    exception text, the reason, and the id listing's reason tally, and this fails.
    """

    secret = _fake_credential()
    with _FakeEmbeddingsServer("always", status=400, message=f"Incorrect API key provided: {secret}.") as server:
        provider = _provider(server)
        with pytest.raises(VNextEmbeddingProviderError) as excinfo:
            provider.embed_batch(["one text"])
        results = embed_batch_isolating(provider, ["one text", "two text"])

    assert secret not in str(excinfo.value)
    assert PROVIDER_MESSAGE_WITHHELD in str(excinfo.value)
    assert excinfo.value.status == 400
    failures = [result for result in results if isinstance(result, EmbeddingTextFailure)]
    assert len(failures) == 2
    assert all(secret not in (failure.reason or "") for failure in failures)
    summary = summarize_embedding_failures(
        [MemoryEmbeddingFailure(f"m-{i}", "code", "message", reason=f.reason) for i, f in enumerate(failures)]
    )
    assert secret not in json.dumps(summary)


def test_the_configured_api_key_is_redacted_even_when_it_has_no_credential_shape() -> None:
    """An endpoint that echoes the key it was sent does not get it printed.

    The key here is an ordinary-looking string the credential check does not
    flag, so only the replacement of the configured key protects it. Mutation:
    drop the ``secrets`` replacement in ``sanitize_provider_message``.
    """

    api_key = "local-" + "hunter2key"
    assert vnext_embeddings.credential_verdict(api_key) is None
    with _FakeEmbeddingsServer(
        "always", status=401, message=f"Authorization failed for key {api_key}; check your key"
    ) as server:
        provider = _provider(server, api_key=api_key)
        with pytest.raises(VNextEmbeddingProviderError) as excinfo:
            provider.embed_batch(["one text"])

    assert api_key not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)
    assert "check your key" in str(excinfo.value)


def test_error_body_is_read_for_the_shapes_the_compatible_servers_use() -> None:
    """The message is found in ``error.message``, ``error`` as text, ``message`` and ``detail``.

    Mutation: read only ``error.message``. The other three shapes then fall back
    to the raw JSON and the exact-text assertions fail.
    """

    from alicebot_api.vnext_embeddings import _provider_error_text

    assert _provider_error_text('{"error": {"message": "openai shape"}}') == "openai shape"
    assert _provider_error_text('{"error": "ollama shape"}') == "ollama shape"
    assert _provider_error_text('{"message": "message shape"}') == "message shape"
    assert _provider_error_text('{"detail": "detail shape"}') == "detail shape"
    assert _provider_error_text("not json at all") == "not json at all"
    assert _provider_error_text('{"unexpected": 1}') == '{"unexpected": 1}'
    assert _provider_error_text("[1, 2]") == "[1, 2]"


def test_reason_for_other_exceptions_is_none_so_driver_text_is_not_printed() -> None:
    """Only the two embedding error types supply text for a reason.

    Mutation: return ``str(exc)`` for any exception. A driver error with a path
    or a value in it would then be printed, and this fails.
    """

    from alicebot_api.vnext_embeddings import embedding_failure_reason

    assert embedding_failure_reason(RuntimeError("secret path /home/someone/x")) is None
    assert embedding_failure_reason(VNextEmbeddingProviderError("connection refused")) == "connection refused"


def test_event_log_record_holds_the_status_and_never_the_message() -> None:
    """The ``memory.embedding_failed`` event gets fixed text and the status number only.

    Mutation: put the failure's ``reason`` into the event payload. The endpoint's
    text would then be stored in the event log, and this fails.
    """

    class Store:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def update_memory_embedding(self, **_kwargs: object) -> dict[str, object]:
            return {"id": "x"}

        def append_event(self, event: dict[str, object]) -> dict[str, object]:
            self.events.append(event)
            return event

    texts = {"m-1": _text(40, "ok"), "m-2": _text(3000, "big")}
    inputs = tuple(
        DeferredMemoryEmbedding.from_memory({"id": memory_id, "canonical_text": text})
        for memory_id, text in texts.items()
    )
    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        preparation = prepare_memory_embeddings(inputs, provider=_provider(server))
    store = Store()
    attached = persist_prepared_memory_embeddings(store, preparation)

    assert attached == 1
    (event,) = store.events
    payload = event["payload_json"]
    assert event["target_id"] == "m-2"
    assert payload["error_code"] == "embedding_preparation_failed"  # type: ignore[index]
    assert payload["provider_status"] == 400  # type: ignore[index]
    assert "context length" not in json.dumps(payload)
    assert "context length" not in repr(preparation.failures[0])
    assert "context length" in (preparation.failures[0].reason or "")


# --- failed-id listing --------------------------------------------------------


def test_failed_ids_are_withheld_when_long_credential_shaped_or_not_printable() -> None:
    """An id that is a finding, long, or holds a control character is not listed.

    Mutation: return the id unchanged from ``listable_memory_id``. The credential,
    the 200-character id and the newline id are then printed, and this fails.
    """

    credential_id = "AKIA" + "ABCDEFGHIJKLMNOP"
    assert vnext_embeddings.credential_verdict(credential_id) is not None
    assert listable_memory_id("c1b8ba19-fb72-4270-9ac1-fc12dac8aad3") == "c1b8ba19-fb72-4270-9ac1-fc12dac8aad3"
    assert listable_memory_id(credential_id) == LISTED_ID_WITHHELD
    assert listable_memory_id("x" * 200) == LISTED_ID_WITHHELD
    assert listable_memory_id("ok\nforged line") == LISTED_ID_WITHHELD
    assert listable_memory_id("tab\there") == LISTED_ID_WITHHELD


def test_failed_id_listing_is_bounded_and_counts_the_rest() -> None:
    """At most 100 ids are listed; the rest are counted. Reasons are tallied, most common first.

    Mutation: list every id. A run that fails 150 memories prints 150 ids and
    ``failed_ids_omitted`` is 0, and this fails.
    """

    failures = [
        MemoryEmbeddingFailure(f"id-{index:03d}", "code", "message", reason="too long" if index % 3 else "other")
        for index in range(150)
    ]
    summary = summarize_embedding_failures(failures)

    assert len(summary["failed_ids"]) == EMBEDDING_FAILED_IDS_LISTED == 100  # type: ignore[arg-type]
    assert summary["failed_ids"][0] == "id-000"  # type: ignore[index]
    assert summary["failed_ids_omitted"] == 50
    assert summary["failure_reasons"] == [  # type: ignore[comparison-overlap]
        {"reason": "too long", "count": 100},
        {"reason": "other", "count": 50},
    ]
    assert summarize_embedding_failures([]) == {
        "failed_ids": [],
        "failed_ids_omitted": 0,
        "failure_reasons": [],
    }


# --- the store ----------------------------------------------------------------


def _seed_vault(db_path: Path, texts: dict[str, str], *, status: str = "active") -> dict[str, str]:
    """Create one active memory per entry; returns {key: memory id}."""

    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    ids: dict[str, str] = {}
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        for key, text in texts.items():
            row = store.create_memory(
                {
                    "memory_key": f"limits-{key}",
                    "value": {"text": text},
                    "memory_type": "semantic",
                    "title": f"Memory {key}",
                    "canonical_text": text,
                    "status": status,
                    "domain": "project",
                    "sensitivity": "private",
                }
            )
            ids[key] = str(row["id"])
    return ids


def _rows(db_path: Path) -> dict[str, sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return {
            row["id"]: row
            for row in connection.execute(
                "SELECT id, embedding, metadata_json FROM memories WHERE deleted_at IS NULL"
            )
        }
    finally:
        connection.close()


def _signature(row: sqlite3.Row) -> dict[str, object]:
    return json.loads(row["metadata_json"]).get("_alice_embedding", {})


def test_store_writes_the_cut_label_into_the_signature_and_an_overwrite_removes_it(tmp_path: Path) -> None:
    """``update_memory_embedding(truncated_to_chars=N)`` stores N; a later write without it clears it.

    Mutation: leave ``truncated_to_chars`` out of the signature the SQLite store
    writes (the first assertion fails), or write the key even when the value is
    ``None`` (the last assertion fails, the key is still there).
    """

    db_path = tmp_path / "memory.db"
    ids = _seed_vault(db_path, {"a": "Fact text."})
    memory_id = ids["a"]
    digest = memory_embedding_content_sha256({"title": "Memory a", "canonical_text": "Fact text."})
    common = {
        "memory_id": memory_id,
        "vector": [1.0, 0.0],
        "provider": "openai_compatible",
        "model": "m",
        "endpoint": "e",
        "content_sha256": digest,
        "signature_version": 2,
    }
    with sqlite_user_connection(db_path, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).update_memory_embedding(**common, truncated_to_chars=1000) is not None
    assert _signature(_rows(db_path)[memory_id])["truncated_to_chars"] == 1000
    with sqlite_user_connection(db_path, USER_ID) as conn:
        assert SQLiteVNextStore(conn, USER_ID).update_memory_embedding(**common) is not None
    assert "truncated_to_chars" not in _signature(_rows(db_path)[memory_id])


def test_store_rejects_a_cap_without_a_provider(tmp_path: Path) -> None:
    """A cap with no provider to compare against is refused, not ignored.

    Mutation: drop the invariant check in ``_missing_embeddings_clause``. The cap
    is then silently ignored and this fails.
    """

    from alicebot_api.store import ContinuityStoreInvariantError

    db_path = tmp_path / "memory.db"
    _seed_vault(db_path, {"a": "Fact text."})
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with pytest.raises(ContinuityStoreInvariantError, match="embedding_input_cap"):
            store.list_memories_missing_embeddings(embedding_input_cap=1000)


class _PostgresCursor:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "_PostgresCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        self.queries.append((query, params))

    def fetchone(self) -> dict[str, object]:
        return {"id": "00000000-0000-0000-0000-000000000001"}

    def fetchall(self) -> list[dict[str, object]]:
        return []


class _PostgresConnection:
    def __init__(self) -> None:
        self.cursor_instance = _PostgresCursor()

    def cursor(self) -> _PostgresCursor:
        return self.cursor_instance


def test_postgres_store_signs_the_cut_label_and_lists_rows_whose_label_differs() -> None:
    """The Postgres carrier writes the label, and its list query compares it with the cap.

    No Postgres runs in this suite, so this checks the statement the store would
    send: the signature the update writes, the label comparison in the list
    query, and that every placeholder has a parameter. A live-database check of
    the same behaviour is in tests/integration/test_vnext_embedding_input_cap_postgres.py.
    Mutation: leave ``truncated_to_chars`` out of the Postgres signature (the first
    assertion fails), or drop the cap term from the list query (the SQL and the
    parameter assertions fail).
    """

    from alicebot_api.store import ContinuityStoreInvariantError
    from alicebot_api.vnext_store import PostgresVNextStore

    connection = _PostgresConnection()
    store = PostgresVNextStore(connection)  # type: ignore[arg-type]
    memory_id = "00000000-0000-0000-0000-000000000001"
    store.update_memory_embedding(
        memory_id=memory_id,
        vector=[1.0],
        provider="provider",
        model="model",
        endpoint="endpoint",
        content_sha256="digest",
        signature_version=2,
        truncated_to_chars=1000,
    )
    store.update_memory_embedding(
        memory_id=memory_id,
        vector=[1.0],
        provider="provider",
        model="model",
        endpoint="endpoint",
        content_sha256="digest",
        signature_version=2,
    )
    cut_query, cut_params = connection.cursor_instance.queries[0]
    plain_query, plain_params = connection.cursor_instance.queries[1]
    assert cut_params[1].obj["truncated_to_chars"] == 1000  # type: ignore[attr-defined]
    assert "truncated_to_chars" not in plain_params[1].obj  # type: ignore[attr-defined]
    assert cut_query == plain_query

    connection.cursor_instance.queries.clear()
    store.list_memories_missing_embeddings(
        embedding_provider="provider",
        embedding_model="model",
        embedding_endpoint="endpoint",
        embedding_signature_version=2,
        embedding_input_cap=1000,
    )
    store.list_memories_missing_embeddings(
        embedding_provider="provider",
        embedding_model="model",
        embedding_endpoint="endpoint",
        embedding_signature_version=2,
    )
    capped_query, capped_params = connection.cursor_instance.queries[0]
    uncapped_query, uncapped_params = connection.cursor_instance.queries[1]
    assert "->> 'truncated_to_chars'" in capped_query
    assert "char_length(" in capped_query
    assert "truncated_to_chars" not in uncapped_query
    assert capped_params[-5:-3] == (1000, "1000")
    assert capped_params[-3:] == (None, None, 100)
    assert capped_query.count("%s") == len(capped_params)
    assert uncapped_query.count("%s") == len(uncapped_params)
    with pytest.raises(ContinuityStoreInvariantError, match="embedding_input_cap"):
        store.list_memories_missing_embeddings(embedding_input_cap=1000)


# --- reindex ------------------------------------------------------------------


def _configure(monkeypatch, server: _FakeEmbeddingsServer, *, cap: int | None = None, model: str = "fake-embed") -> None:
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, model)
    monkeypatch.delenv(EMBEDDINGS_API_KEY_ENV, raising=False)
    if cap is None:
        monkeypatch.delenv(EMBEDDINGS_MAX_INPUT_CHARS_ENV, raising=False)
    else:
        monkeypatch.setenv(EMBEDDINGS_MAX_INPUT_CHARS_ENV, str(cap))


def _reindex(db_path: Path, capsys) -> tuple[int, dict[str, object], str]:
    code = onramp_main(["reindex-embeddings", "--db", str(db_path), "--user-id", str(USER_ID)])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def _doctor_line(db_path: Path, capsys, label: str = "memories without a current vector") -> str:
    assert onramp_main(["doctor", "--db", str(db_path), "--user-id", str(USER_ID)]) == 0
    for line in capsys.readouterr().out.splitlines():
        if line.startswith(f"{label}: "):
            return line[len(label) + 2 :]
    raise AssertionError(f"no {label!r} line")


def test_reindex_names_the_refused_memory_and_still_embeds_its_neighbours(
    tmp_path: Path, monkeypatch, capsys, caplog
) -> None:
    """One over-long memory is named, with the endpoint's reason; five neighbours get vectors.

    The cap is raised above the text so the endpoint, not the cap, refuses it.
    Mutation: embed the whole batch in one ``embed_batch`` call in
    ``prepare_memory_embeddings`` with no isolation. The five neighbours then get
    no vector and ``failed_ids`` names all six. Or drop
    ``**summarize_embedding_failures(failures)`` from the reindex output, and
    ``failed_ids`` is missing. Either way this fails. Let ``_run_reindex_embeddings``
    leave ``log_failures`` on and the failure is also written to the process
    log, which prints a free-text line on the command's stderr, and the
    log-record assertion fails.
    """

    db_path = tmp_path / "memory.db"
    texts = {f"s{index}": _text(60, f"short{index}") for index in range(5)}
    texts["big"] = _text(3000, "big")
    ids = _seed_vault(db_path, texts)
    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        _configure(monkeypatch, server, cap=20_000)
        with caplog.at_level("DEBUG", logger="alicebot_api.vnext_embeddings"):
            code, payload, stderr = _reindex(db_path, capsys)
    assert [record for record in caplog.records if record.levelname in ("WARNING", "ERROR")] == []

    assert code == 1
    assert payload["embedded"] == 5
    assert payload["failed"] == 1
    assert payload["failed_ids"] == [ids["big"]]
    assert payload["failed_ids_omitted"] == 0
    (reason,) = payload["failure_reasons"]  # type: ignore[misc]
    assert reason["count"] == 1
    assert reason["reason"].startswith(HTTP_PREFIX)
    assert "maximum context length is 2000" in reason["reason"]
    assert payload["input_cap_chars"] == 20_000
    assert payload["truncated_inputs"] == 0
    # stderr stays the documented error records and nothing else
    assert [json.loads(line)["error"]["code"] for line in stderr.splitlines()] == ["embedding_batch_failed"]
    rows = _rows(db_path)
    assert [rows[ids[key]]["embedding"] is not None for key in texts] == [True] * 5 + [False]
    # the doctor and reindex agree on what is still missing
    assert _doctor_line(db_path, capsys) == "1"


def test_reindex_with_the_default_cap_embeds_a_long_memory_from_its_head_and_labels_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A memory over the cap is embedded from its first ``cap`` characters, labelled, and not retried.

    Mutation: do not pass the label to the store (drop ``truncated_to_chars`` in
    ``PreparedMemoryEmbedding.to_update``). The signature then carries no label
    and the label assertions fail. Make ``_embedding_input_cut_sqlite`` always
    return ``None`` and the labelled long row is listed again on the second run,
    so the ``batches == 0`` assertion fails.
    """

    db_path = tmp_path / "memory.db"
    ids = _seed_vault(db_path, {"fits": _text(80, "fits"), "long": _text(5000, "long")})
    with _FakeEmbeddingsServer("reject", limit=1000) as server:
        _configure(monkeypatch, server, cap=1000)
        code, payload, _stderr = _reindex(db_path, capsys)
        sent = [length for request in server.requests for length in request["lens"]]  # type: ignore[union-attr]

        assert code == 0
        assert payload["embedded"] == 2
        assert payload["failed"] == 0
        assert payload["truncated_inputs"] == 1
        assert payload["input_cap_chars"] == 1000
        assert max(sent) == 1000
        rows = _rows(db_path)
        assert _signature(rows[ids["long"]])["truncated_to_chars"] == 1000
        assert "truncated_to_chars" not in _signature(rows[ids["fits"]])
        full_text = memory_embedding_text({"title": "Memory long", "canonical_text": _text(5000, "long")})
        assert _signature(rows[ids["long"]])["content_sha256"] == hashlib.sha256(full_text.encode()).hexdigest()

        # nothing is left to do, and the doctor agrees
        code, payload, _stderr = _reindex(db_path, capsys)
        assert (code, payload["batches"], payload["embedded"]) == (0, 0, 0)
        assert _doctor_line(db_path, capsys) == "0"


def test_changing_the_cap_reembeds_exactly_the_rows_whose_embedded_text_changes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Lowering and raising the cap re-embeds the medium and long rows and never the short one.

    Mutation: drop the cap term from ``_missing_embeddings_clause``. A cap change
    is then invisible and the second and third runs embed 0, so this fails.
    Compare each stored label with the cap itself instead of with the label a
    vector made now would carry, and the short row, which has no label, is
    listed again, so the request-count assertions fail.
    """

    db_path = tmp_path / "memory.db"
    ids = _seed_vault(
        db_path,
        {"short": _text(100, "short"), "medium": _text(700, "medium"), "long": _text(5000, "long")},
    )
    with _FakeEmbeddingsServer("accept") as server:
        _configure(monkeypatch, server, cap=1000)
        _reindex(db_path, capsys)
        short_blob = _rows(db_path)[ids["short"]]["embedding"]
        assert [_signature(_rows(db_path)[ids[key]]).get("truncated_to_chars") for key in ("short", "medium", "long")] == [
            None,
            None,
            1000,
        ]

        # cap lowered to 500: the 700-character row and the long row now embed from a cut text
        server.requests.clear()
        _configure(monkeypatch, server, cap=500)
        code, payload, _stderr = _reindex(db_path, capsys)
        assert (code, payload["embedded"], payload["truncated_inputs"]) == (0, 2, 2)
        assert sorted(length for request in server.requests for length in request["lens"]) == [500, 500]  # type: ignore[union-attr]
        rows = _rows(db_path)
        assert [_signature(rows[ids[key]]).get("truncated_to_chars") for key in ("short", "medium", "long")] == [
            None,
            500,
            500,
        ]
        assert rows[ids["short"]]["embedding"] == short_blob

        # cap raised back to 1000: the 700-character row fits again and loses its label
        server.requests.clear()
        _configure(monkeypatch, server, cap=1000)
        code, payload, _stderr = _reindex(db_path, capsys)
        assert (code, payload["embedded"], payload["truncated_inputs"]) == (0, 2, 1)
        rows = _rows(db_path)
        assert [_signature(rows[ids[key]]).get("truncated_to_chars") for key in ("short", "medium", "long")] == [
            None,
            None,
            1000,
        ]
        assert rows[ids["short"]]["embedding"] == short_blob

        _configure(monkeypatch, server, cap=1000)
        assert _reindex(db_path, capsys)[1]["batches"] == 0


def test_a_vector_made_before_caps_existed_for_a_long_text_is_made_again(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A long row with an unlabelled vector (what an older Alice stored) is re-embedded once.

    An older Alice stored such a vector from whatever the endpoint kept of the
    whole text, possibly a silent cut. Under a cap the row is expected to carry a
    label, so it is listed. Mutation: make ``_embedding_input_cut_sqlite`` always
    return ``None``, which is the same as treating a missing label as current.
    The long row is then skipped and this fails.
    """

    db_path = tmp_path / "memory.db"
    ids = _seed_vault(db_path, {"long": _text(5000, "long"), "fits": _text(80, "fits")})
    with _FakeEmbeddingsServer("accept") as server:
        _configure(monkeypatch, server, cap=1000)
        # write today's signature but with no label, as an older release did
        provider = get_embedding_provider()
        assert provider is not None
        with sqlite_user_connection(db_path, USER_ID) as conn:
            store = SQLiteVNextStore(conn, USER_ID)
            for key in ("long", "fits"):
                memory = {"id": ids[key], "title": f"Memory {key}", "canonical_text": _text(5000 if key == "long" else 80, key)}
                memory["canonical_text"] = _text(5000, "long") if key == "long" else _text(80, "fits")
                update = signed_memory_embedding_update(memory, [0.5, 0.5], provider=provider)
                update.pop("truncated_to_chars", None)  # type: ignore[misc]
                assert store.update_memory_embedding(**update) is not None
        code, payload, _stderr = _reindex(db_path, capsys)

    assert (code, payload["embedded"], payload["truncated_inputs"]) == (0, 1, 1)
    rows = _rows(db_path)
    assert _signature(rows[ids["long"]])["truncated_to_chars"] == 1000
    assert "truncated_to_chars" not in _signature(rows[ids["fits"]])


def test_reindex_withholds_a_credential_shaped_memory_id_and_lists_a_normal_one(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A restored backup can hold an odd id; reindex prints a normal id and withholds a finding.

    Mutation: print ``memory_id`` as is in ``summarize_embedding_failures``. The
    credential-shaped id appears in the output and this fails.
    """

    db_path = tmp_path / "memory.db"
    ids = _seed_vault(db_path, {"normal": _text(3000, "norm"), "odd": _text(3000, "odd")})
    credential_id = "AKIA" + "ABCDEFGHIJKLMNOP"
    with sqlite_user_connection(db_path, USER_ID) as conn:
        conn.execute("UPDATE memories SET id = ? WHERE id = ?", (credential_id, ids["odd"]))
    with _FakeEmbeddingsServer("reject", limit=2000) as server:
        _configure(monkeypatch, server, cap=20_000)
        code = onramp_main(["reindex-embeddings", "--db", str(db_path), "--user-id", str(USER_ID)])
    captured = capsys.readouterr()

    assert code == 1
    payload = json.loads(captured.out)
    assert sorted(payload["failed_ids"]) == sorted([ids["normal"], LISTED_ID_WITHHELD])
    assert credential_id not in captured.out + captured.err


def test_reindex_output_and_event_log_never_hold_a_credential_from_the_endpoint(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An endpoint that answers every request with a credential in the body prints none of it.

    The credential is built at runtime. Mutation: skip the credential check in
    ``sanitize_provider_message``. The secret then reaches stdout, stderr or the
    stored events, and this fails.
    """

    secret = _fake_credential()
    db_path = tmp_path / "memory.db"
    _seed_vault(db_path, {"a": "First fact.", "b": "Second fact."})
    with _FakeEmbeddingsServer("always", status=400, message=f"bad key {secret}") as server:
        _configure(monkeypatch, server)
        code = onramp_main(["reindex-embeddings", "--db", str(db_path), "--user-id", str(USER_ID)])
    captured = capsys.readouterr()

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["failed"] == 2
    assert secret not in captured.out + captured.err
    assert payload["failure_reasons"][0]["reason"] == f"{HTTP_PREFIX}{PROVIDER_MESSAGE_WITHHELD}"
    connection = sqlite3.connect(db_path)
    try:
        dump = "\n".join(str(value) for row in connection.execute("SELECT * FROM event_log") for value in row)
    finally:
        connection.close()
    assert secret not in dump


# --- the doctor ---------------------------------------------------------------


def test_doctor_counts_active_memories_without_a_current_vector(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The line counts active and accepted rows with no vector or a vector that is not today's.

    Mutation: count every status (a candidate is then counted: the first
    number is wrong), count only rows with no vector (the model change leaves
    the count at 0), or print a fixed number.
    """

    db_path = tmp_path / "memory.db"
    _seed_vault(db_path, {"a": "First fact.", "b": "Second fact.", "c": "Third fact."})
    with sqlite_user_connection(db_path, USER_ID) as conn:
        SQLiteVNextStore(conn, USER_ID).create_memory(
            {
                "memory_key": "limits-candidate",
                "value": {"text": "A candidate."},
                "memory_type": "semantic",
                "title": "Candidate",
                "canonical_text": "A candidate.",
                "status": "candidate",
                "domain": "project",
                "sensitivity": "private",
            }
        )
    for name in (EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV, EMBEDDINGS_API_KEY_ENV, EMBEDDINGS_MAX_INPUT_CHARS_ENV):
        monkeypatch.delenv(name, raising=False)

    # no provider: no vector can be current, and the line says why
    assert _doctor_line(db_path, capsys) == "3 (no embedding provider configured)"

    with _FakeEmbeddingsServer("accept") as server:
        _configure(monkeypatch, server)
        assert _doctor_line(db_path, capsys) == "3"
        assert _reindex(db_path, capsys)[1]["embedded"] == 4  # the candidate is reindexed too
        assert _doctor_line(db_path, capsys) == "0"
        # a different model makes every vector stale
        _configure(monkeypatch, server, model="another-model")
        assert _doctor_line(db_path, capsys) == "3"
        # so does a different cap, but only for a row whose embedded text would change
        _configure(monkeypatch, server)
        assert _doctor_line(db_path, capsys) == "0"
        _configure(monkeypatch, server, cap=256)
        assert _doctor_line(db_path, capsys) == "0"


def test_doctor_line_is_in_the_report_between_facts_and_the_brief(tmp_path: Path, monkeypatch, capsys) -> None:
    """The new line sits after ``committed facts`` and before ``last brief``.

    Mutation: move the line to the end of the report. The order here fails.
    """

    for name in (EMBEDDINGS_BASE_URL_ENV, EMBEDDINGS_MODEL_ENV, EMBEDDINGS_API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    assert onramp_main(["doctor", "--db", str(db_path), "--user-id", str(USER_ID)]) == 0
    labels = [line.split(":", 1)[0] for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert labels.index("memories without a current vector") == labels.index("committed facts") + 1
    assert labels.index("last brief") == labels.index("memories without a current vector") + 1

