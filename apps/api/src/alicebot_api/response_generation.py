from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any, TypedDict
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from alicebot_api.compiler import compile_and_persist_trace
from alicebot_api.contracts import (
    AssistantResponseEventPayload,
    CompiledContextPack,
    ContextCompilerLimits,
    GenerateResponseSuccess,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ModelUsagePayload,
    PROMPT_ASSEMBLY_VERSION_V0,
    PromptAssemblyInput,
    PromptAssemblyResult,
    PromptAssemblyTracePayload,
    PromptSection,
    RESPONSE_GENERATION_VERSION_V0,
    ResponseTraceSummary,
    TRACE_KIND_RESPONSE_GENERATE,
    TraceEventRecord,
)
from alicebot_api.session_briefing import SESSION_BRIEF_FRAME, quote_session_brief_text
from alicebot_api.store import ContinuityStore, JsonObject, JsonValue

PROMPT_TRACE_EVENT_KIND = "response.prompt.assembled"
MODEL_COMPLETED_TRACE_EVENT_KIND = "response.model.completed"
MODEL_FAILED_TRACE_EVENT_KIND = "response.model.failed"
RESPONSE_FAILURE_MESSAGES = {
    "upstream_failure": "An upstream service failed",
    "conflict": "The request conflicts with the current resource state",
}
ResponseFailureCode = Literal["upstream_failure", "conflict"]
logger = logging.getLogger(__name__)
SYSTEM_INSTRUCTION = (
    "You are AliceBot. Reply to the latest user message using the provided durable context. "
    "If the context is insufficient, say so briefly instead of inventing facts."
)
DEVELOPER_INSTRUCTION = (
    "Treat the CONTEXT and CONVERSATION sections as authoritative durable state. "
    "Do not call tools, do not describe hidden chain-of-thought, and keep the reply concise."
)


class ModelInvocationError(RuntimeError):
    """Raised when the configured model provider cannot produce a response."""


class ModelProviderUnavailableError(ModelInvocationError):
    """Raised when an upstream model provider cannot be reached in time."""


class ResponseGenerationConflictError(RuntimeError):
    """Raised when newer thread activity supersedes a prepared response."""


@dataclass(frozen=True, slots=True)
class ResponseFailure:
    error_code: ResponseFailureCode
    detail: str
    trace: ResponseTraceSummary


@dataclass(frozen=True, slots=True)
class PreparedResponseGeneration:
    """Frozen handoff between database preparation and provider invocation."""

    user_id: UUID
    thread_id: UUID
    limits: ContextCompilerLimits
    compiled_trace_id: str
    compiled_trace_event_count: int
    prompt: PromptAssemblyResult
    model_request: ModelInvocationRequest
    user_event_id: UUID
    user_event_sequence_no: int


@dataclass(frozen=True, slots=True)
class OpenAICompatibleTransportConfig:
    base_url: str
    api_key: str
    timeout_seconds: int
    invoke_path: str = "/responses"
    auth_mode: str = "bearer"


class _OpenAIResponseContentItem(TypedDict, total=False):
    type: str
    text: str


class _OpenAIResponseOutputItem(TypedDict, total=False):
    type: str
    content: list[_OpenAIResponseContentItem]


class _OpenAIInputTokenDetails(TypedDict, total=False):
    cached_tokens: int | None


class _OpenAIResponseUsage(TypedDict, total=False):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    input_tokens_details: _OpenAIInputTokenDetails | None
    prompt_tokens_details: _OpenAIInputTokenDetails | None


class _OpenAIResponsePayload(TypedDict, total=False):
    id: str
    status: str
    output: list[_OpenAIResponseOutputItem]
    usage: _OpenAIResponseUsage


def _deterministic_json(value: JsonObject | list[object]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _context_section_payload(context_pack: CompiledContextPack) -> JsonObject:
    return cast(
        JsonObject,
        {
            "compiler_version": context_pack["compiler_version"],
            "scope": context_pack["scope"],
            "limits": context_pack["limits"],
            "user": context_pack["user"],
            "thread": context_pack["thread"],
            "sessions": context_pack["sessions"],
            "memories": context_pack["memories"],
            "memory_summary": context_pack["memory_summary"],
            "artifact_chunks": context_pack["artifact_chunks"],
            "artifact_chunk_summary": context_pack["artifact_chunk_summary"],
            "entities": context_pack["entities"],
            "entity_summary": context_pack["entity_summary"],
            "entity_edges": context_pack["entity_edges"],
            "entity_edge_summary": context_pack["entity_edge_summary"],
        },
    )


def _quote_runtime_note(value: object) -> object:
    """Quote stored strings. Mapping keys and ids stay as stored."""

    if isinstance(value, str):
        return quote_session_brief_text(value) if value.strip() else value
    if isinstance(value, list):
        return [_quote_runtime_note(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _quote_runtime_note(child) for key, child in value.items()}
    return value


def _frame_runtime_context_section(payload: JsonObject) -> str:
    """Frame the context section sent to a model by ``/v1/runtime/invoke``.

    The compiler pack is not rewritten. This copy quotes memory ``value``
    text and artifact-chunk prose, then puts the framing line above the JSON.
    """

    framed = dict(payload)
    memories = payload.get("memories")
    if isinstance(memories, list):
        quoted_memories: list[JsonValue] = []
        for memory in memories:
            if not isinstance(memory, dict):
                quoted_memories.append(cast(JsonValue, memory))
                continue
            copied = dict(memory)
            if "value" in copied:
                copied["value"] = cast(JsonValue, _quote_runtime_note(copied["value"]))
            quoted_memories.append(cast(JsonValue, copied))
        framed["memories"] = quoted_memories
    chunks = payload.get("artifact_chunks")
    if isinstance(chunks, list):
        framed["artifact_chunks"] = cast(JsonValue, _quote_runtime_note(chunks))
    body = _deterministic_json(cast(JsonObject, framed))
    return f"{SESSION_BRIEF_FRAME}\n{body}"


def assemble_prompt(
    *,
    request: PromptAssemblyInput,
    compile_trace_id: str,
) -> PromptAssemblyResult:
    sections = (
        PromptSection(name="system", content=request.system_instruction),
        PromptSection(name="developer", content=request.developer_instruction),
        PromptSection(
            name="context",
            content=_frame_runtime_context_section(_context_section_payload(request.context_pack)),
        ),
        PromptSection(
            name="conversation",
            content=_deterministic_json(cast(JsonObject, {"events": request.context_pack["events"]})),
        ),
    )
    prompt_text = "\n\n".join(f"[{section.name.upper()}]\n{section.content}" for section in sections)
    prompt_sha256 = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    trace_payload: PromptAssemblyTracePayload = {
        "version": PROMPT_ASSEMBLY_VERSION_V0,
        "compile_trace_id": compile_trace_id,
        "compiler_version": request.context_pack["compiler_version"],
        "prompt_sha256": prompt_sha256,
        "prompt_char_count": len(prompt_text),
        "section_order": [section.name for section in sections],
        "section_characters": {section.name: len(section.content) for section in sections},
        "included_session_count": len(request.context_pack["sessions"]),
        "included_event_count": len(request.context_pack["events"]),
        "included_memory_count": len(request.context_pack["memories"]),
        "included_entity_count": len(request.context_pack["entities"]),
        "included_entity_edge_count": len(request.context_pack["entity_edges"]),
    }
    return PromptAssemblyResult(
        sections=sections,
        prompt_text=prompt_text,
        prompt_sha256=prompt_sha256,
        trace_payload=trace_payload,
    )


def _openai_input_message(role: str, content: str) -> JsonObject:
    return {
        "role": role,
        "content": [{"type": "input_text", "text": content}],
    }


def _build_openai_responses_payload(request: ModelInvocationRequest) -> JsonObject:
    sections = {section.name: section.content for section in request.prompt.sections}
    return {
        "model": request.model,
        "store": request.store,
        "tool_choice": request.tool_choice,
        "tools": [],
        "input": [
            _openai_input_message("system", sections["system"]),
            _openai_input_message("developer", sections["developer"]),
            _openai_input_message("user", f"[CONTEXT]\n{sections['context']}"),
            _openai_input_message("user", f"[CONVERSATION]\n{sections['conversation']}"),
        ],
        "text": {"format": {"type": "text"}},
    }


def _extract_output_text(response_payload: _OpenAIResponsePayload) -> str:
    output_items = response_payload.get("output", [])
    for output_item in output_items:
        if output_item.get("type") != "message":
            continue
        for content_item in output_item.get("content", []):
            if content_item.get("type") == "output_text":
                text = content_item.get("text")
                if isinstance(text, str) and text:
                    return text
    raise ModelInvocationError("model response did not include assistant output text")


def _parse_usage(response_payload: _OpenAIResponsePayload) -> ModelUsagePayload:
    usage = response_payload.get("usage", {})
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    usage_payload: ModelUsagePayload = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(details_key)
        if not isinstance(details, dict):
            continue
        cached_tokens = details.get("cached_tokens")
        if isinstance(cached_tokens, int):
            usage_payload["cached_input_tokens"] = cached_tokens
            break
    return usage_payload


def _parse_openai_response_payload(raw_payload: bytes) -> _OpenAIResponsePayload:
    try:
        parsed_payload = json.loads(raw_payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ModelInvocationError("model provider returned invalid JSON") from exc

    if not isinstance(parsed_payload, dict):
        raise ModelInvocationError("model provider returned invalid JSON")

    return cast(_OpenAIResponsePayload, parsed_payload)


def _extract_http_error_detail(exc: HTTPError) -> str | None:
    raw_body = exc.read().decode("utf-8", errors="replace")
    try:
        parsed_error = json.loads(raw_body)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed_error, dict):
        return None

    error = parsed_error.get("error", {})
    if not isinstance(error, dict):
        return None

    detail = error.get("message")
    if isinstance(detail, str) and detail:
        return detail
    return None


def _build_model_http_request(
    *,
    base_url: str,
    api_key: str,
    auth_mode: str,
    invoke_path: str,
    payload: JsonObject,
) -> Request:
    normalized_invoke_path = invoke_path if invoke_path.startswith("/") else f"/{invoke_path}"
    endpoint = base_url.rstrip("/") + normalized_invoke_path
    normalized_auth_mode = auth_mode.strip().lower()
    headers = {"Content-Type": "application/json"}
    if normalized_auth_mode == "bearer":
        token = api_key.strip()
        if token == "":  # nosec B105
            raise ModelInvocationError("MODEL_API_KEY is not configured")
        headers["Authorization"] = f"Bearer {token}"
    elif normalized_auth_mode != "none":
        raise ModelInvocationError(f"unsupported provider auth_mode: {auth_mode}")
    return Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _model_failure_trace_payload(
    *,
    request: ModelInvocationRequest,
    error_code: ResponseFailureCode,
    error_message: str,
) -> JsonObject:
    return {
        "provider": request.provider,
        "model": request.model,
        "tool_choice": "none",
        "tools_enabled": False,
        "response_id": None,
        "finish_reason": "incomplete",
        "output_text_char_count": 0,
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        },
        "error_code": error_code,
        "error_message": error_message,
    }


def _create_linked_response_trace(
    *,
    store: ContinuityStore,
    user_id: UUID,
    thread_id: UUID,
    limits: ContextCompilerLimits,
    compiled_trace_id: str,
    compiled_trace_event_count: int,
    status: str,
    trace_events: list[TraceEventRecord],
) -> ResponseTraceSummary:
    trace = _create_response_trace(
        store=store,
        user_id=user_id,
        thread_id=thread_id,
        limits=limits,
        status=status,
        trace_events=trace_events,
    )
    trace["compile_trace_id"] = compiled_trace_id
    trace["compile_trace_event_count"] = compiled_trace_event_count
    return trace


def invoke_openai_compatible_model(
    *,
    transport: OpenAICompatibleTransportConfig,
    request: ModelInvocationRequest,
) -> ModelInvocationResponse:
    if request.provider != "openai_responses":
        raise ModelInvocationError(f"unsupported model provider: {request.provider}")

    payload = _build_openai_responses_payload(request)
    http_request = _build_model_http_request(
        base_url=transport.base_url,
        api_key=transport.api_key,
        auth_mode=transport.auth_mode,
        invoke_path=transport.invoke_path,
        payload=payload,
    )

    try:
        with urlopen(http_request, timeout=transport.timeout_seconds) as response:
            raw_payload = response.read()
    except HTTPError as exc:
        detail = _extract_http_error_detail(exc)
        if detail is not None:
            raise ModelInvocationError(detail) from exc
        raise ModelInvocationError(f"model provider returned HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise ModelProviderUnavailableError("model provider request timed out") from exc
    except URLError as exc:
        raise ModelProviderUnavailableError(f"model provider request failed: {exc.reason}") from exc

    response_payload = _parse_openai_response_payload(raw_payload)
    output_text = _extract_output_text(response_payload)
    finish_reason: Literal["completed", "incomplete"] = (
        "completed" if response_payload.get("status") == "completed" else "incomplete"
    )
    return ModelInvocationResponse(
        provider=request.provider,
        model=request.model,
        response_id=response_payload.get("id"),
        finish_reason=finish_reason,
        output_text=output_text,
        usage=_parse_usage(response_payload),
    )


def build_assistant_response_payload(
    *,
    prompt: PromptAssemblyResult,
    model_response: ModelInvocationResponse,
) -> AssistantResponseEventPayload:
    return {
        "text": model_response.output_text,
        "model": {
            "provider": model_response.provider,
            "model": model_response.model,
            "response_id": model_response.response_id,
            "finish_reason": model_response.finish_reason,
            "usage": model_response.usage,
        },
        "prompt": {
            "assembly_version": PROMPT_ASSEMBLY_VERSION_V0,
            "prompt_sha256": prompt.prompt_sha256,
            "section_order": [section.name for section in prompt.sections],
        },
    }


def _create_response_trace(
    *,
    store: ContinuityStore,
    user_id: UUID,
    thread_id: UUID,
    limits: ContextCompilerLimits,
    status: str,
    trace_events: list[TraceEventRecord],
) -> ResponseTraceSummary:
    trace = store.create_trace(
        user_id=user_id,
        thread_id=thread_id,
        kind=TRACE_KIND_RESPONSE_GENERATE,
        compiler_version=RESPONSE_GENERATION_VERSION_V0,
        status=status,
        limits=limits.as_payload(),
    )
    for sequence_no, trace_event in enumerate(trace_events, start=1):
        store.append_trace_event(
            trace_id=trace["id"],
            sequence_no=sequence_no,
            kind=trace_event.kind,
            payload=trace_event.payload,
        )
    return {
        "compile_trace_id": "",
        "compile_trace_event_count": 0,
        "response_trace_id": str(trace["id"]),
        "response_trace_event_count": len(trace_events),
    }


def prepare_response_generation(
    *,
    store: ContinuityStore,
    user_id: UUID,
    thread_id: UUID,
    message_text: str,
    limits: ContextCompilerLimits,
    runtime_override: tuple[str, str],
    system_instruction: str = SYSTEM_INSTRUCTION,
    developer_instruction: str = DEVELOPER_INSTRUCTION,
) -> PreparedResponseGeneration:
    """Persist the user turn and compile a provider-ready immutable request."""

    store.get_user(user_id)
    store.get_thread(thread_id)

    user_event = store.append_event(
        thread_id,
        None,
        "message.user",
        {"text": message_text},
    )
    compiled_trace = compile_and_persist_trace(
        store,
        user_id=user_id,
        thread_id=thread_id,
        limits=limits,
    )
    prompt = assemble_prompt(
        request=PromptAssemblyInput(
            context_pack=compiled_trace.context_pack,
            system_instruction=system_instruction,
            developer_instruction=developer_instruction,
        ),
        compile_trace_id=compiled_trace.trace_id,
    )
    model_provider, model_name = runtime_override
    model_request = ModelInvocationRequest(
        provider=model_provider,  # type: ignore[arg-type]
        model=model_name,
        prompt=prompt,
    )
    return PreparedResponseGeneration(
        user_id=user_id,
        thread_id=thread_id,
        limits=limits,
        compiled_trace_id=compiled_trace.trace_id,
        compiled_trace_event_count=compiled_trace.trace_event_count,
        prompt=prompt,
        model_request=model_request,
        user_event_id=user_event["id"],
        user_event_sequence_no=user_event["sequence_no"],
    )


def fail_response_generation(
    *,
    store: ContinuityStore,
    prepared: PreparedResponseGeneration,
    error: ModelInvocationError,
    error_code: ResponseFailureCode = "upstream_failure",
) -> ResponseFailure:
    """Persist the failure trace after an out-of-transaction provider error."""

    public_message = RESPONSE_FAILURE_MESSAGES[error_code]
    logger.error(
        "response generation failed with public error code=%s",
        error_code,
        exc_info=(type(error), error, error.__traceback__),
        extra={"public_error_code": error_code},
    )

    prompt_trace_event = TraceEventRecord(
        kind=PROMPT_TRACE_EVENT_KIND,
        payload=cast(JsonObject, prepared.prompt.trace_payload),
    )
    trace = _create_linked_response_trace(
        store=store,
        user_id=prepared.user_id,
        thread_id=prepared.thread_id,
        limits=prepared.limits,
        compiled_trace_id=prepared.compiled_trace_id,
        compiled_trace_event_count=prepared.compiled_trace_event_count,
        status="failed",
        trace_events=[
            prompt_trace_event,
            TraceEventRecord(
                kind=MODEL_FAILED_TRACE_EVENT_KIND,
                payload=cast(
                    JsonObject,
                    _model_failure_trace_payload(
                        request=prepared.model_request,
                        error_code=error_code,
                        error_message=public_message,
                    ),
                ),
            ),
        ],
    )
    return ResponseFailure(error_code=error_code, detail=public_message, trace=trace)


def complete_response_generation(
    *,
    store: ContinuityStore,
    prepared: PreparedResponseGeneration,
    model_response: ModelInvocationResponse,
) -> GenerateResponseSuccess:
    """Persist the assistant event and success trace in a short transaction."""

    prompt_trace_event = TraceEventRecord(
        kind=PROMPT_TRACE_EVENT_KIND,
        payload=cast(JsonObject, prepared.prompt.trace_payload),
    )
    assistant_payload = build_assistant_response_payload(
        prompt=prepared.prompt,
        model_response=model_response,
    )
    assistant_event = store.append_event_if_tail(
        prepared.thread_id,
        None,
        "message.assistant",
        cast(JsonObject, assistant_payload),
        expected_event_id=prepared.user_event_id,
        expected_sequence_no=prepared.user_event_sequence_no,
    )
    if assistant_event is None:
        raise ResponseGenerationConflictError(
            "response was superseded by newer thread activity; retry against the latest thread state"
        )
    trace = _create_linked_response_trace(
        store=store,
        user_id=prepared.user_id,
        thread_id=prepared.thread_id,
        limits=prepared.limits,
        compiled_trace_id=prepared.compiled_trace_id,
        compiled_trace_event_count=prepared.compiled_trace_event_count,
        status="completed",
        trace_events=[
            prompt_trace_event,
            TraceEventRecord(
                kind=MODEL_COMPLETED_TRACE_EVENT_KIND,
                payload=cast(JsonObject, model_response.to_trace_payload()),
            ),
        ],
    )
    return {
        "assistant": {
            "event_id": str(assistant_event["id"]),
            "sequence_no": assistant_event["sequence_no"],
            "text": model_response.output_text,
            "model_provider": model_response.provider,
            "model": model_response.model,
        },
        "trace": trace,
    }
