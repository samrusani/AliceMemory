"""Tests for the disclosed rerank stage (vnext_reranker).

Contract under test, in order of importance:

1. DORMANCY — unconfigured means byte-identical packs and ZERO provider
   calls: the stage must not exist for anyone who has not opted in.
2. Reorder semantics — provider scores reorder the fused head; slot counts
   never shrink; policy-excluded candidates are never re-admitted; equal
   scores fall through the content-stable cascade.
3. Fail-open — any provider failure keeps fused order and records itself.
4. Cap semantics — only the head (RERANK_*_CANDIDATE_CAP) is scored; the
   tail keeps its fused order.
5. HONESTY — the scoring prompt is frozen (sha-pinned HERE, as a literal,
   so module drift fails this file) and contains only generic relevance
   vocabulary; the trace stage record discloses model, counts, reorder
   flag, latency, and token usage.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
from uuid import UUID

import pytest

import alicebot_api.vnext_reranker as vnext_reranker
from alicebot_api import vnext_retrieval as vnext_retrieval_module
from alicebot_api.vnext_reranker import (
    RERANK_EMPTY_TEXT,
    RERANK_FAIL_OPEN_PREFIX,
    RERANK_MEMORY_CANDIDATE_CAP,
    RERANK_PROMPT_SHA256,
    RERANK_PROMPT_TEMPLATE,
    RERANK_SOURCE_CANDIDATE_CAP,
    RERANK_STATUS_NOOP,
    RERANK_STATUS_FAIL_OPEN_PROVIDER_ERROR,
    RERANK_STATUS_RERANKED,
    RERANK_TEXT_MAX_CHARS,
    OpenAICompatibleRerankProvider,
    RerankCompletion,
    VNextRerankerConfigurationError,
    VNextRerankerProviderError,
    build_rerank_prompt,
    get_reranker_provider,
    parse_rerank_scores,
    rerank,
    rerank_candidate_text,
    rerank_fused_candidates,
)
from alicebot_api.vnext_retrieval import (
    STAGE_DISABLED_MINIMAL,
    RetrievalCandidate,
    VNextRetrievalRequest,
    VNextRetrievalService,
)


# The frozen prompt, pinned as a LITERAL so any drift in the module
# constant fails this file (comparing module-to-module would auto-pass).
_PINNED_PROMPT_SHA256 = "fbe9a1ede4849d9c4019e3919568334ec7b2a0d75cf96cc4fdd2b79cdcb7094f"


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch) -> None:
    for name in (
        "ALICE_RERANKER_BASE_URL",
        "ALICE_RERANKER_MODEL",
        "ALICE_RERANKER_API_KEY",
        "ALICE_EMBEDDINGS_BASE_URL",
        "ALICE_EMBEDDINGS_MODEL",
        "ALICE_EMBEDDINGS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# -- honesty: frozen generic prompt --------------------------------------------


def test_rerank_prompt_template_is_sha_pinned() -> None:
    computed = hashlib.sha256(RERANK_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
    assert computed == _PINNED_PROMPT_SHA256
    # The module's exported sha (disclosed in every stage record) must be
    # the hash of the prompt that actually runs.
    assert RERANK_PROMPT_SHA256 == _PINNED_PROMPT_SHA256


def test_rerank_prompt_contains_only_generic_relevance_vocabulary() -> None:
    lowered = RERANK_PROMPT_TEMPLATE.casefold()
    for banned in (
        "question_type",
        "question type",
        "benchmark",
        "longmemeval",
        "abstain",
        "knowledge update",
        "temporal reasoning",
        "multi-session",
        "single-session",
        "preference",
        "session",
        "memory",
    ):
        assert banned not in lowered, f"rerank prompt must stay generic; found {banned!r}"


def test_build_rerank_prompt_numbers_documents_in_order() -> None:
    prompt = build_rerank_prompt("example query", ["first doc", "second doc"])

    assert "Query: example query" in prompt
    assert "[1] first doc\n[2] second doc" in prompt
    assert "JSON array of 2 integers" in prompt
    with pytest.raises(VNextRerankerConfigurationError, match="at least one document"):
        build_rerank_prompt("example query", [])


def test_rerank_candidate_text_collapses_whitespace_caps_and_backfills() -> None:
    item = {
        "title": "Roadmap  decision",
        "canonical_text": "Ship the beta\nin two waves.",
    }
    assert rerank_candidate_text(item) == "Roadmap decision Ship the beta in two waves."
    assert rerank_candidate_text({"id": "row-1"}) == RERANK_EMPTY_TEXT
    long_text = "word " * 400
    assert len(rerank_candidate_text({"canonical_text": long_text})) == RERANK_TEXT_MAX_CHARS


# -- env config seam ------------------------------------------------------------


def test_get_reranker_provider_returns_none_when_unconfigured(monkeypatch) -> None:
    assert get_reranker_provider() is None

    monkeypatch.setenv("ALICE_RERANKER_BASE_URL", "http://localhost:11434/v1")
    assert get_reranker_provider() is None  # model still missing

    monkeypatch.setenv("ALICE_RERANKER_MODEL", "qwen3:8b")
    provider = get_reranker_provider()
    assert provider is not None
    assert provider.base_url == "http://localhost:11434/v1"
    assert provider.model == "qwen3:8b"
    assert provider.api_key is None

    monkeypatch.setenv("ALICE_RERANKER_API_KEY", "sk-local")
    provider_with_key = get_reranker_provider()
    assert provider_with_key is not None
    assert provider_with_key.api_key == "sk-local"


# -- OpenAI-compatible provider seam --------------------------------------------


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_complete_posts_openai_chat_shape_and_reads_usage(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout, enforce_public_peer):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "[3, 1, 2]"}}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 7},
            }
        ).encode("utf-8")
        return _FakeResponse(body)

    monkeypatch.setattr(vnext_reranker, "open_provider_url", fake_urlopen)
    provider = OpenAICompatibleRerankProvider(
        base_url="http://localhost:11434/v1/", model="qwen3:8b", api_key="sk-local"
    )

    completion = provider.complete("score these")

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["payload"] == {
        "model": "qwen3:8b",
        "messages": [{"role": "user", "content": "score these"}],
        "temperature": 0,
    }
    assert dict(captured["headers"]).get("Authorization") == "Bearer sk-local"
    assert completion == RerankCompletion(content="[3, 1, 2]", prompt_tokens=42, completion_tokens=7)


def test_complete_omits_authorization_header_without_api_key(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout, enforce_public_peer):
        del timeout
        captured["headers"] = {key.casefold(): value for key, value in request.header_items()}
        return _FakeResponse(
            json.dumps({"choices": [{"message": {"content": "[1]"}}]}).encode("utf-8")
        )

    monkeypatch.setattr(vnext_reranker, "open_provider_url", fake_urlopen)
    provider = OpenAICompatibleRerankProvider(base_url="http://localhost:1234/v1", model="local")

    completion = provider.complete("score this")

    assert "authorization" not in captured["headers"]
    assert completion.prompt_tokens is None and completion.completion_tokens is None


def test_complete_raises_provider_error_on_malformed_payloads(monkeypatch) -> None:
    provider = OpenAICompatibleRerankProvider(base_url="http://localhost:1234/v1", model="local")

    monkeypatch.setattr(
        vnext_reranker,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(json.dumps({"unexpected": True}).encode("utf-8")),
    )
    with pytest.raises(VNextRerankerProviderError, match="choices"):
        provider.complete("score this")

    monkeypatch.setattr(
        vnext_reranker,
        "open_provider_url",
        lambda request, timeout, enforce_public_peer: _FakeResponse(
            json.dumps({"choices": [{"message": {"content": 17}}]}).encode("utf-8")
        ),
    )
    with pytest.raises(VNextRerankerProviderError, match="message content"):
        provider.complete("score this")


# -- score parsing ---------------------------------------------------------------


def test_parse_rerank_scores_tolerates_code_fences_and_prose() -> None:
    content = "Sure, here are the scores:\n```json\n[10, 90, 55]\n```"
    assert parse_rerank_scores(content, expected_count=3) == [10.0, 90.0, 55.0]


def test_parse_rerank_scores_rejects_bad_shapes() -> None:
    with pytest.raises(VNextRerankerProviderError, match="did not contain a JSON array"):
        parse_rerank_scores("no scores here", expected_count=2)
    with pytest.raises(VNextRerankerProviderError, match="2 scores for 3 documents"):
        parse_rerank_scores("[1, 2]", expected_count=3)
    with pytest.raises(VNextRerankerProviderError, match="must be integers between 0 and 100"):
        parse_rerank_scores('[1, "high"]', expected_count=2)
    with pytest.raises(VNextRerankerProviderError, match="must be integers between 0 and 100"):
        parse_rerank_scores("[true, false]", expected_count=2)
    for malformed in ("[50.0, 10]", "[NaN, 10]", "[Infinity, 10]", "[-1, 10]", "[101, 10]"):
        with pytest.raises(
            VNextRerankerProviderError,
            match="must be integers between 0 and 100",
        ):
            parse_rerank_scores(malformed, expected_count=2)


def test_rerank_fails_open_when_provider_scores_break_integer_range_contract() -> None:
    provider = ScriptedRerankProvider(contents=["[50, NaN, 100]"])
    candidates = [_item("a", "alpha"), _item("b", "bravo"), _item("c", "charlie")]

    outcome = rerank("query", candidates, provider=provider, max_candidates=48)

    assert outcome.status.startswith(RERANK_FAIL_OPEN_PREFIX)
    assert outcome.order == (0, 1, 2)
    assert outcome.scores is None


# -- rerank() core semantics -----------------------------------------------------


class ScriptedRerankProvider:
    """Mock provider returning queued completion contents, recording prompts."""

    provider = "mock"
    model = "mock-rerank"

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> RerankCompletion:
        self.prompts.append(prompt)
        if not self.contents:
            raise AssertionError("ScriptedRerankProvider ran out of scripted completions")
        return RerankCompletion(content=self.contents.pop(0), prompt_tokens=10, completion_tokens=5)


class ReversingRerankProvider:
    """Scores documents 1..n in prompt order, so the fused order reverses."""

    provider = "mock"
    model = "mock-reverse"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> RerankCompletion:
        self.calls += 1
        count = len(re.findall(r"^\[\d+\] ", prompt, re.MULTILINE))
        return RerankCompletion(
            content=json.dumps(list(range(1, count + 1))), prompt_tokens=10, completion_tokens=5
        )


class FailingRerankProvider:
    provider = "mock"
    model = "mock-fail"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> RerankCompletion:
        self.calls += 1
        raise VNextRerankerProviderError("reranker endpoint returned HTTP 500")


def _item(item_id: str, text: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {"id": item_id, "canonical_text": text}
    row.update(overrides)
    return row


def test_rerank_reorders_by_provider_scores_descending() -> None:
    provider = ScriptedRerankProvider(contents=["[10, 90, 50]"])
    candidates = [_item("a", "alpha"), _item("b", "bravo"), _item("c", "charlie")]

    outcome = rerank("which callsign", candidates, provider=provider, max_candidates=48)

    assert outcome.status == RERANK_STATUS_RERANKED
    assert outcome.order == (1, 2, 0)
    assert outcome.scores == (10.0, 90.0, 50.0)
    assert outcome.reordered is True
    assert outcome.candidates_scored == 3
    assert outcome.model == "mock-rerank"
    assert outcome.latency_ms >= 0
    assert outcome.prompt_tokens == 10 and outcome.completion_tokens == 5
    # The prompt sent to the provider is the frozen template over the
    # candidates' text, in fused order.
    assert provider.prompts == [build_rerank_prompt("which callsign", ["alpha", "bravo", "charlie"])]


def test_rerank_caps_scored_head_and_preserves_tail_order() -> None:
    provider = ScriptedRerankProvider(contents=["[1, 99]"])
    candidates = [_item("a", "alpha"), _item("b", "bravo"), _item("c", "charlie"), _item("d", "delta")]

    outcome = rerank("query", candidates, provider=provider, max_candidates=2)

    # Only the head was scored (prompt carries exactly two documents) …
    assert "JSON array of 2 integers" in provider.prompts[0]
    assert outcome.candidates_scored == 2
    # … and the tail keeps its fused positions after the reordered head.
    assert outcome.order == (1, 0, 2, 3)


def test_rerank_breaks_equal_scores_content_stably() -> None:
    provider = ScriptedRerankProvider(contents=["[50, 50]"])
    # Same score; the content-stable cascade puts the OLDER content-stamped
    # row first regardless of fused input position or id.
    newer = _item("id-0", "same length text a", valid_from="2026-05-01T00:00:00Z")
    older = _item("id-9", "same length text b", valid_from="2024-05-01T00:00:00Z")

    outcome = rerank("query", [newer, older], provider=provider, max_candidates=48)

    assert outcome.order == (1, 0)
    assert outcome.status == RERANK_STATUS_RERANKED


def test_rerank_noop_below_two_candidates_makes_zero_provider_calls() -> None:
    provider = FailingRerankProvider()  # would blow up if called

    outcome = rerank("query", [_item("a", "alpha")], provider=provider, max_candidates=48)

    assert provider.calls == 0
    assert outcome.status == RERANK_STATUS_NOOP
    assert outcome.order == (0,)
    assert outcome.scores is None
    assert outcome.reordered is False
    assert outcome.candidates_scored == 0


def test_rerank_fails_open_on_provider_error() -> None:
    provider = FailingRerankProvider()
    candidates = [_item("a", "alpha"), _item("b", "bravo")]

    outcome = rerank("query", candidates, provider=provider, max_candidates=48)

    assert provider.calls == 1
    assert outcome.order == (0, 1)
    assert outcome.scores is None
    assert outcome.reordered is False
    assert outcome.candidates_scored == 0
    assert outcome.status == RERANK_STATUS_FAIL_OPEN_PROVIDER_ERROR
    assert "VNextRerankerProviderError" not in outcome.status


def test_rerank_fails_open_on_malformed_scores() -> None:
    provider = ScriptedRerankProvider(contents=["no json array here"])

    outcome = rerank("query", [_item("a", "alpha"), _item("b", "bravo")], provider=provider, max_candidates=48)

    assert outcome.status.startswith(RERANK_FAIL_OPEN_PREFIX)
    assert outcome.order == (0, 1)


# -- rerank_fused_candidates over RetrievalCandidate lists -----------------------


def _candidate(
    item_id: str,
    text: str,
    *,
    rank: int,
    selected: bool,
    exclusion_reason: str | None = None,
) -> RetrievalCandidate:
    return RetrievalCandidate(
        item=_item(item_id, text),
        target_type="memory",
        rank=rank,
        rrf_score=1.0 / rank,
        stage_ranks={"fts": rank},
        selected=selected,
        exclusion_reason=exclusion_reason,
    )


def test_rerank_fused_candidates_promotes_and_demotes_preserving_slot_count() -> None:
    provider = ScriptedRerankProvider(contents=["[5, 10, 90, 80]"])
    candidates = [
        _candidate("m1", "one", rank=1, selected=True),
        _candidate("m2", "two", rank=2, selected=True),
        _candidate("m3", "three", rank=3, selected=False, exclusion_reason="trimmed_by_limit"),
        _candidate("m4", "four", rank=4, selected=False, exclusion_reason="trimmed_by_limit"),
    ]

    rebuilt, outcome = rerank_fused_candidates(
        candidates, query="query", provider=provider, limit=2, max_candidates=48
    )

    assert outcome.reordered is True
    assert [str(candidate.item["id"]) for candidate in rebuilt] == ["m3", "m4", "m2", "m1"]
    assert [candidate.rank for candidate in rebuilt] == [1, 2, 3, 4]
    # Exactly as many slots as fusion filled — promotion and demotion, no shrink.
    assert [candidate.selected for candidate in rebuilt] == [True, True, False, False]
    assert [candidate.exclusion_reason for candidate in rebuilt] == [
        None,
        None,
        "trimmed_by_limit",
        "trimmed_by_limit",
    ]


def test_rerank_fused_candidates_never_readmits_policy_excluded() -> None:
    provider = ScriptedRerankProvider(contents=["[1, 2]"])
    candidates = [
        _candidate("m1", "one", rank=1, selected=True),
        _candidate("blocked", "restricted row", rank=2, selected=False, exclusion_reason="sensitivity_filtered"),
        _candidate("m2", "two", rank=3, selected=False, exclusion_reason="trimmed_by_limit"),
    ]

    rebuilt, outcome = rerank_fused_candidates(
        candidates, query="query", provider=provider, limit=1, max_candidates=48
    )

    assert outcome.candidates_scored == 2  # only the reorderable pool was scored
    assert [str(candidate.item["id"]) for candidate in rebuilt] == ["m2", "m1", "blocked"]
    assert [candidate.selected for candidate in rebuilt] == [True, False, False]
    blocked = rebuilt[2]
    assert blocked.exclusion_reason == "sensitivity_filtered"


def test_rerank_fused_candidates_returns_untouched_list_on_fail_open() -> None:
    provider = FailingRerankProvider()
    candidates = [
        _candidate("m1", "one", rank=1, selected=True),
        _candidate("m2", "two", rank=2, selected=False, exclusion_reason="trimmed_by_limit"),
    ]

    rebuilt, outcome = rerank_fused_candidates(
        candidates, query="query", provider=provider, limit=1, max_candidates=48
    )

    assert outcome.status.startswith(RERANK_FAIL_OPEN_PREFIX)
    assert rebuilt == candidates  # same objects, same ranks — fused order stands
    assert rebuilt[0] is candidates[0]


def test_rerank_fused_candidates_returns_untouched_list_when_order_confirmed() -> None:
    provider = ScriptedRerankProvider(contents=["[90, 10]"])
    candidates = [
        _candidate("m1", "one", rank=1, selected=True),
        _candidate("m2", "two", rank=2, selected=False, exclusion_reason="trimmed_by_limit"),
    ]

    rebuilt, outcome = rerank_fused_candidates(
        candidates, query="query", provider=provider, limit=1, max_candidates=48
    )

    assert outcome.status == RERANK_STATUS_RERANKED
    assert outcome.reordered is False
    assert rebuilt == candidates
    assert rebuilt[0] is candidates[0]


# -- retrieval service integration ----------------------------------------------


class MinimalRetrievalStore:
    """Duck-typed store exercising the service's default fallback paths."""

    def __init__(
        self,
        *,
        memories: list[dict[str, object]],
        sources: list[dict[str, object]] | None = None,
    ) -> None:
        self.memories = memories
        self.sources = sources or []
        self.events: list[dict[str, object]] = []

    def append_event(self, event: dict[str, object]) -> dict[str, object]:
        self.events.append(event)
        return event

    def search_memories(self, *, query, domains=None, sensitivity_allowed=None, limit=8, **_filters):
        del query, domains, sensitivity_allowed
        return self.memories[:limit]

    def search_sources(self, *, query, domains=None, sensitivity_allowed=None, limit=8):
        del query, domains, sensitivity_allowed
        return self.sources[:limit]

    def list_open_loops(self, *, status="open", domains=None, sensitivity_allowed=None, limit=8):
        del status, domains, sensitivity_allowed, limit
        return []

    def list_provenance_links(self, *, target_type, target_id):
        del target_type, target_id
        return []

    def list_edges(self, *, from_id=None, to_id=None):
        del from_id, to_id
        return []


def _memory_row(memory_id: str, text: str) -> dict[str, object]:
    return {
        "id": memory_id,
        "memory_type": "semantic",
        "canonical_text": text,
        "status": "active",
        "domain": "project",
        "sensitivity": "private",
    }


def _source_row(source_id: str, title: str) -> dict[str, object]:
    return {
        "id": source_id,
        "source_type": "manual_text",
        "title": title,
        "domain": "project",
        "sensitivity": "private",
        "metadata_json": {},
    }


def _store() -> MinimalRetrievalStore:
    # Lowercase query surface everywhere: no temporal anchor, no coverage
    # intent, no salient grounding entities — the rerank delta is isolated.
    return MinimalRetrievalStore(
        memories=[
            _memory_row("memory-1", "meridian roadmap release decision alpha"),
            _memory_row("memory-2", "meridian roadmap release note bravo"),
            _memory_row("memory-3", "meridian roadmap release note charlie"),
            _memory_row("memory-4", "meridian roadmap release note delta"),
        ],
        sources=[
            _source_row("source-1", "meridian roadmap session one"),
            _source_row("source-2", "meridian roadmap session two"),
        ],
    )


_REQUEST = VNextRetrievalRequest(
    query="meridian roadmap release status",
    domains=("project",),
    max_items=2,
    trace_id="trace-rr-dormant-pin",
)


def test_unconfigured_service_takes_byte_identical_rerank_free_path(monkeypatch) -> None:
    """Dormancy guard: without ALICE_RERANKER_* config the stage must not exist.

    Compiles the same request twice — once with the real (dormant) code and
    once with the module entry point hard-disabled so any call would blow
    up — with deterministic pack ids. The packs must serialize
    byte-identically and carry no reranker vocabulary anywhere.
    """

    def compile_pack() -> dict[str, object]:
        counter = itertools.count(1)
        monkeypatch.setattr(vnext_retrieval_module, "uuid4", lambda: UUID(int=next(counter)))
        service = VNextRetrievalService(_store())
        assert service.reranker_provider is None
        return service.compile_context_pack(_REQUEST)

    dormant_pack = compile_pack()
    monkeypatch.setattr(
        vnext_retrieval_module.vnext_reranker,
        "rerank_fused_candidates",
        lambda *args, **kwargs: pytest.fail("dormant reranker must make zero calls"),
    )
    hard_disabled_pack = compile_pack()

    assert json.dumps(dormant_pack, sort_keys=True, default=str) == json.dumps(
        hard_disabled_pack, sort_keys=True, default=str
    )
    assert "reranker" not in json.dumps(dormant_pack, default=str)
    assert [memory["id"] for memory in dormant_pack["relevant_memories"]] == ["memory-1", "memory-2"]


def test_configured_reranker_reorders_pack_and_discloses_stage() -> None:
    provider = ReversingRerankProvider()
    dormant_pack = VNextRetrievalService(_store()).compile_context_pack(_REQUEST)
    pack = VNextRetrievalService(_store(), reranker_provider=provider).compile_context_pack(_REQUEST)

    # One listwise call per fused pool (memories, sources).
    assert provider.calls == 2
    # Reversing scores promote the fused tail into the slots; slot count is
    # preserved exactly (never drops below max_items).
    assert [memory["id"] for memory in pack["relevant_memories"]] == ["memory-4", "memory-3"]
    assert len(pack["relevant_memories"]) == len(dormant_pack["relevant_memories"])
    assert [source["id"] for source in pack["sources"]] == ["source-2", "source-1"]
    assert len(pack["sources"]) == len(dormant_pack["sources"])

    record = pack["trace"]["stages"]["reranker"]
    assert record["source"] == "reranker"
    assert record["provider"] == "mock"
    assert record["model"] == "mock-reverse"
    assert record["prompt_sha256"] == _PINNED_PROMPT_SHA256
    assert record["candidates_scored"] == 6  # 4 memories + 2 sources
    assert record["reordered"] is True
    assert isinstance(record["latency_ms"], int)
    assert record["usage"] == {"prompt_tokens": 20, "completion_tokens": 10}
    assert record["memories"]["status"] == RERANK_STATUS_RERANKED
    assert record["memories"]["candidates_scored"] == 4
    assert record["sources"]["status"] == RERANK_STATUS_RERANKED
    # The reranked ranks flow into the honest selection trace.
    selected_memory_ids = [
        entry["target_id"]
        for entry in pack["trace"]["selected"]
        if entry["target_type"] == "memory"
    ]
    assert selected_memory_ids == ["memory-4", "memory-3"]


def test_reranker_failure_fails_open_to_fused_order() -> None:
    provider = FailingRerankProvider()
    dormant_pack = VNextRetrievalService(_store()).compile_context_pack(_REQUEST)
    pack = VNextRetrievalService(_store(), reranker_provider=provider).compile_context_pack(_REQUEST)

    assert provider.calls == 2
    assert [memory["id"] for memory in pack["relevant_memories"]] == [
        memory["id"] for memory in dormant_pack["relevant_memories"]
    ]
    assert [source["id"] for source in pack["sources"]] == [
        source["id"] for source in dormant_pack["sources"]
    ]
    record = pack["trace"]["stages"]["reranker"]
    assert record["reordered"] is False
    assert record["candidates_scored"] == 0
    assert record["memories"]["status"].startswith(RERANK_FAIL_OPEN_PREFIX)
    assert record["sources"]["status"].startswith(RERANK_FAIL_OPEN_PREFIX)


def test_minimal_depth_skips_reranker_with_honest_status() -> None:
    provider = ReversingRerankProvider()
    pack = VNextRetrievalService(_store(), reranker_provider=provider).compile_context_pack(
        VNextRetrievalRequest(
            query="meridian roadmap release status",
            domains=("project",),
            max_items=2,
            context_depth="minimal",
            trace_id="trace-rr-minimal",
        )
    )

    assert provider.calls == 0
    record = pack["trace"]["stages"]["reranker"]
    assert record["status"] == STAGE_DISABLED_MINIMAL
    assert record["candidates_scored"] == 0
    assert record["reordered"] is False
    assert [memory["id"] for memory in pack["relevant_memories"]] == ["memory-1", "memory-2"]


def test_candidate_caps_match_design_scope() -> None:
    # The scope contract from the integration design: ~48 fused memory
    # candidates, ~24 fused sources, per pack compile.
    assert RERANK_MEMORY_CANDIDATE_CAP == 48
    assert RERANK_SOURCE_CANDIDATE_CAP == 24
