from __future__ import annotations

import inspect
import json
from uuid import UUID

import pytest

import alicebot_api.response_generation as response_generation
from alicebot_api.contracts import (
    ContextCompilerLimits,
    ModelInvocationRequest,
    ModelInvocationResponse,
    PROMPT_ASSEMBLY_VERSION_V0,
    PromptAssemblyInput,
)
from alicebot_api.response_generation import (
    ModelInvocationError,
    OpenAICompatibleTransportConfig,
    PreparedResponseGeneration,
    ResponseGenerationConflictError,
    assemble_prompt,
    build_assistant_response_payload,
    complete_response_generation,
    fail_response_generation,
    invoke_openai_compatible_model,
    prepare_response_generation,
)


def make_context_pack() -> dict[str, object]:
    return {
        "compiler_version": "continuity_v0",
        "scope": {
            "user_id": "11111111-1111-1111-8111-111111111111",
            "thread_id": "22222222-2222-2222-8222-222222222222",
        },
        "limits": {
            "max_sessions": 3,
            "max_events": 8,
            "max_memories": 5,
            "max_entities": 5,
            "max_entity_edges": 10,
        },
        "user": {
            "id": "11111111-1111-1111-8111-111111111111",
            "email": "owner@example.com",
            "display_name": "Owner",
            "created_at": "2026-03-12T09:00:00+00:00",
        },
        "thread": {
            "id": "22222222-2222-2222-8222-222222222222",
            "title": "Thread",
            "created_at": "2026-03-12T09:00:00+00:00",
            "updated_at": "2026-03-12T09:05:00+00:00",
        },
        "sessions": [],
        "events": [
            {
                "id": "33333333-3333-3333-8333-333333333333",
                "session_id": None,
                "sequence_no": 1,
                "kind": "message.user",
                "payload": {"text": "Hello"},
                "created_at": "2026-03-12T09:06:00+00:00",
            }
        ],
        "memories": [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "memory_key": "user.preference.coffee",
                "value": {"likes": "oat milk"},
                "status": "active",
                "source_event_ids": ["33333333-3333-3333-8333-333333333333"],
                "created_at": "2026-03-12T09:04:00+00:00",
                "updated_at": "2026-03-12T09:05:00+00:00",
                "source_provenance": {"sources": ["symbolic"], "semantic_score": None},
            }
        ],
        "memory_summary": {
            "candidate_count": 1,
            "included_count": 1,
            "excluded_deleted_count": 0,
            "excluded_limit_count": 0,
            "hybrid_retrieval": {
                "requested": False,
                "embedding_config_id": None,
                "query_vector_dimensions": 0,
                "semantic_limit": 0,
                "symbolic_selected_count": 1,
                "semantic_selected_count": 0,
                "merged_candidate_count": 1,
                "deduplicated_count": 0,
                "included_symbolic_only_count": 1,
                "included_semantic_only_count": 0,
                "included_dual_source_count": 0,
                "similarity_metric": None,
                "source_precedence": ["symbolic", "semantic"],
                "symbolic_order": ["updated_at_asc", "created_at_asc", "id_asc"],
                "semantic_order": ["score_desc", "created_at_asc", "id_asc"],
            },
        },
        "artifact_chunks": [],
        "artifact_chunk_summary": {
            "requested": False,
            "lexical_requested": False,
            "semantic_requested": False,
            "scope": None,
            "query": None,
            "query_terms": [],
            "embedding_config_id": None,
            "query_vector_dimensions": 0,
            "limit": 0,
            "lexical_limit": 0,
            "semantic_limit": 0,
            "searched_artifact_count": 0,
            "lexical_candidate_count": 0,
            "semantic_candidate_count": 0,
            "merged_candidate_count": 0,
            "deduplicated_count": 0,
            "included_count": 0,
            "included_lexical_only_count": 0,
            "included_semantic_only_count": 0,
            "included_dual_source_count": 0,
            "excluded_uningested_artifact_count": 0,
            "excluded_limit_count": 0,
            "matching_rule": None,
            "similarity_metric": None,
            "source_precedence": ["lexical", "semantic"],
            "lexical_order": [
                "matched_query_term_count_desc",
                "first_match_char_start_asc",
                "relative_path_asc",
                "sequence_no_asc",
                "id_asc",
            ],
            "semantic_order": ["score_desc", "relative_path_asc", "sequence_no_asc", "id_asc"],
            "merged_order": [
                "source_precedence_asc",
                "lexical_rank_asc",
                "semantic_rank_asc",
                "relative_path_asc",
                "sequence_no_asc",
                "id_asc",
            ],
        },
        "entities": [],
        "entity_summary": {
            "candidate_count": 0,
            "included_count": 0,
            "excluded_limit_count": 0,
        },
        "entity_edges": [],
        "entity_edge_summary": {
            "anchor_entity_count": 0,
            "candidate_count": 0,
            "included_count": 0,
            "excluded_limit_count": 0,
        },
    }


def test_assemble_prompt_is_deterministic_and_explicit() -> None:
    first = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )
    second = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )

    assert first.prompt_text == second.prompt_text
    assert first.prompt_sha256 == second.prompt_sha256
    assert first.trace_payload == second.trace_payload
    assert [section.name for section in first.sections] == [
        "system",
        "developer",
        "context",
        "conversation",
    ]
    assert "[SYSTEM]\nSystem instruction" in first.prompt_text
    assert "[DEVELOPER]\nDeveloper instruction" in first.prompt_text
    assert '"memory_key":"user.preference.coffee"' in first.prompt_text
    assert first.trace_payload["version"] == PROMPT_ASSEMBLY_VERSION_V0
    assert first.trace_payload["compile_trace_id"] == "compile-trace-123"
    assert first.trace_payload["included_event_count"] == 1
    assert first.trace_payload["included_memory_count"] == 1


class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_invoke_openai_compatible_model_sends_tools_disabled_request_and_parses_response(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout, enforce_public_peer):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "resp_123",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Assistant reply"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 4,
                        "total_tokens": 16,
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.response_generation.open_provider_url", fake_urlopen)

    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )
    response = invoke_openai_compatible_model(
        transport=OpenAICompatibleTransportConfig(
            base_url="https://example.test/v1",
            api_key="secret-key",
            timeout_seconds=17,
        ),
        request=ModelInvocationRequest(
            provider="openai_responses",
            model="gpt-5-mini",
            prompt=prompt,
        ),
    )

    assert captured["url"] == "https://example.test/v1/responses"
    assert captured["timeout"] == 17
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert captured["body"]["tool_choice"] == "none"
    assert captured["body"]["tools"] == []
    assert captured["body"]["store"] is False
    assert [item["role"] for item in captured["body"]["input"]] == [
        "system",
        "developer",
        "user",
        "user",
    ]
    assert response == ModelInvocationResponse(
        provider="openai_responses",
        model="gpt-5-mini",
        response_id="resp_123",
        finish_reason="completed",
        output_text="Assistant reply",
        usage={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
    )


def test_invoke_openai_compatible_model_parses_optional_cached_input_token_telemetry(
    monkeypatch,
) -> None:
    def fake_urlopen(_request, timeout, enforce_public_peer):
        del timeout
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "resp_telemetry",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Assistant reply"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 8,
                        "total_tokens": 108,
                        "input_tokens_details": {"cached_tokens": 76},
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.response_generation.open_provider_url", fake_urlopen)

    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )

    response = invoke_openai_compatible_model(
        transport=OpenAICompatibleTransportConfig(
            base_url="https://example.test/v1",
            api_key="secret-key",
            timeout_seconds=17,
        ),
        request=ModelInvocationRequest(
            provider="openai_responses",
            model="gpt-5-mini",
            prompt=prompt,
        ),
    )

    assert response.usage == {
        "input_tokens": 100,
        "output_tokens": 8,
        "total_tokens": 108,
        "cached_input_tokens": 76,
    }


def test_invoke_openai_compatible_model_normalizes_non_utf8_provider_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        "alicebot_api.response_generation.open_provider_url",
        lambda *_args, **_kwargs: FakeHTTPResponse(b"\xff\xfe"),
    )
    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )

    with pytest.raises(ModelInvocationError, match="invalid JSON"):
        invoke_openai_compatible_model(
            transport=OpenAICompatibleTransportConfig(
                base_url="https://example.test/v1",
                api_key="secret-key",
                timeout_seconds=60,
            ),
            request=ModelInvocationRequest(
                provider="openai_responses",
                model="gpt-5-mini",
                prompt=prompt,
            ),
        )


def test_invoke_openai_compatible_model_rejects_blank_bearer_key_before_transport(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "alicebot_api.response_generation.open_provider_url",
        lambda *_args, **_kwargs: pytest.fail("blank bearer credentials must fail before transport"),
    )
    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )

    with pytest.raises(ModelInvocationError, match="MODEL_API_KEY is not configured"):
        invoke_openai_compatible_model(
            transport=OpenAICompatibleTransportConfig(
                base_url="https://example.test/v1",
                api_key="   ",
                timeout_seconds=60,
            ),
            request=ModelInvocationRequest(
                provider="openai_responses",
                model="gpt-5-mini",
                prompt=prompt,
            ),
        )


def test_build_assistant_response_payload_captures_model_and_prompt_metadata() -> None:
    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )
    payload = build_assistant_response_payload(
        prompt=prompt,
        model_response=ModelInvocationResponse(
            provider="openai_responses",
            model="gpt-5-mini",
            response_id="resp_123",
            finish_reason="completed",
            output_text="Assistant reply",
            usage={
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "cached_input_tokens": 9,
            },
        ),
    )

    assert payload == {
        "text": "Assistant reply",
        "model": {
            "provider": "openai_responses",
            "model": "gpt-5-mini",
            "response_id": "resp_123",
            "finish_reason": "completed",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
                "cached_input_tokens": 9,
            },
        },
        "prompt": {
            "assembly_version": "prompt_assembly_v0",
            "prompt_sha256": prompt.prompt_sha256,
            "section_order": ["system", "developer", "context", "conversation"],
        },
    }


def test_response_generation_compatibility_exports_and_fallback_contract_are_absent() -> None:
    for obsolete_export in (
        "invoke_model",
        "resolve_thread_model_runtime",
        "invoke_prepared_response",
        "generate_response",
    ):
        assert not hasattr(response_generation, obsolete_export)

    parameters = inspect.signature(prepare_response_generation).parameters
    assert "settings" not in parameters
    assert parameters["runtime_override"].default is inspect.Parameter.empty
    assert "agent_profile_id" not in PreparedResponseGeneration.__dataclass_fields__


@pytest.mark.parametrize(
    ("error_code", "message"),
    (
        ("upstream_failure", "An upstream service failed"),
        ("conflict", "The request conflicts with the current resource state"),
    ),
)
def test_response_failure_and_persisted_trace_are_static(
    error_code: str,
    message: str,
) -> None:
    sentinel = "UNIQUE_RESPONSE_PROVIDER_EXCEPTION_SENTINEL"
    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )
    prepared = PreparedResponseGeneration(
        user_id=UUID("11111111-1111-4111-8111-111111111111"),
        thread_id=UUID("22222222-2222-4222-8222-222222222222"),
        limits=ContextCompilerLimits(),
        compiled_trace_id="compile-trace-123",
        compiled_trace_event_count=1,
        prompt=prompt,
        model_request=ModelInvocationRequest(
            provider="openai_responses",
            model="gpt-5-mini",
            prompt=prompt,
        ),
        user_event_id=UUID("33333333-3333-4333-8333-333333333333"),
        user_event_sequence_no=1,
    )

    class FailureStore:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []

        def create_trace(self, **_kwargs: object) -> dict[str, object]:
            return {"id": UUID("44444444-4444-4444-8444-444444444444")}

        def append_trace_event(self, **kwargs: object) -> None:
            self.events.append(dict(kwargs))

    store = FailureStore()
    failure = fail_response_generation(
        store=store,  # type: ignore[arg-type]
        prepared=prepared,
        error=ModelInvocationError(sentinel),
        error_code=error_code,  # type: ignore[arg-type]
    )

    assert failure.error_code == error_code
    assert failure.detail == message
    assert sentinel not in repr(failure)
    assert store.events[-1]["payload"]["error_code"] == error_code
    assert store.events[-1]["payload"]["error_message"] == message
    assert sentinel not in json.dumps(store.events, default=str)


def test_interleaved_response_completion_rejects_superseded_user_turn() -> None:
    user_id = UUID("11111111-1111-4111-8111-111111111111")
    thread_id = UUID("22222222-2222-4222-8222-222222222222")
    first_event_id = UUID("33333333-3333-4333-8333-333333333331")
    second_event_id = UUID("33333333-3333-4333-8333-333333333332")
    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=make_context_pack(),
            system_instruction="System instruction",
            developer_instruction="Developer instruction",
        ),
        compile_trace_id="compile-trace-123",
    )
    model_request = ModelInvocationRequest(
        provider="openai_responses",
        model="gpt-5-mini",
        prompt=prompt,
    )
    limits = ContextCompilerLimits()

    def prepared(event_id: UUID, sequence_no: int) -> PreparedResponseGeneration:
        return PreparedResponseGeneration(
            user_id=user_id,
            thread_id=thread_id,
            limits=limits,
            compiled_trace_id=f"compile-{sequence_no}",
            compiled_trace_event_count=1,
            prompt=prompt,
            model_request=model_request,
            user_event_id=event_id,
            user_event_sequence_no=sequence_no,
        )

    class InterleavingStore:
        def __init__(self) -> None:
            self.tail = (second_event_id, 2)
            self.assistant_events: list[dict[str, object]] = []

        def append_event_if_tail(
            self,
            _thread_id,
            _session_id,
            kind,
            payload,
            *,
            expected_event_id,
            expected_sequence_no,
        ):
            if self.tail != (expected_event_id, expected_sequence_no):
                return None
            row = {
                "id": UUID("44444444-4444-4444-8444-444444444444"),
                "sequence_no": expected_sequence_no + 1,
                "kind": kind,
                "payload": payload,
            }
            self.tail = (row["id"], row["sequence_no"])
            self.assistant_events.append(row)
            return row

        def create_trace(self, **_kwargs):
            return {"id": UUID("55555555-5555-4555-8555-555555555555")}

        def append_trace_event(self, **_kwargs):
            return None

    store = InterleavingStore()
    model_response = ModelInvocationResponse(
        provider="openai_responses",
        model="gpt-5-mini",
        response_id="resp-1",
        finish_reason="completed",
        output_text="Latest-turn answer",
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )

    complete_response_generation(
        store=store,  # type: ignore[arg-type]
        prepared=prepared(second_event_id, 2),
        model_response=model_response,
    )
    with pytest.raises(ResponseGenerationConflictError, match="superseded"):
        complete_response_generation(
            store=store,  # type: ignore[arg-type]
            prepared=prepared(first_event_id, 1),
            model_response=model_response,
        )

    assert [event["payload"]["text"] for event in store.assistant_events] == ["Latest-turn answer"]
