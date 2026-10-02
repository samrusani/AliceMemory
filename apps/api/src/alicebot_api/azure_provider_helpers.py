from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from alicebot_api.contracts import (
    ModelFinishReason,
    ModelInvocationRequest,
    ModelInvocationResponse,
    ModelUsagePayload,
)
from alicebot_api.provider_http import open_provider_url
from alicebot_api.provider_security import redirect_note, validate_provider_base_url
from alicebot_api.response_generation import (
    ModelInvocationError,
    ModelProviderUnavailableError,
)

AZURE_AUTH_MODE_API_KEY = "azure_api_key"
# Static auth-mode label; not a credential value.
AZURE_AUTH_MODE_AD_TOKEN = "azure_ad_token"  # nosec B105
DEFAULT_AZURE_API_VERSION = "2024-10-21"


def build_azure_auth_headers(*, auth_mode: str, credential: str) -> dict[str, str]:
    mode = auth_mode.strip().lower()
    token = credential.strip()
    if token == "":  # nosec B105
        raise ModelInvocationError("azure credential is required")
    if mode == AZURE_AUTH_MODE_API_KEY:
        return {"api-key": token}
    if mode == AZURE_AUTH_MODE_AD_TOKEN:
        return {"Authorization": f"Bearer {token}"}
    raise ModelInvocationError(f"unsupported provider auth_mode: {auth_mode}")


def request_azure_json(
    *,
    method: str,
    base_url: str,
    path: str,
    api_version: str,
    timeout_seconds: int,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validated_base_url = validate_provider_base_url(base_url)
    normalized_path = path if path.startswith("/") else f"/{path}"
    endpoint = _append_api_version(
        url=validated_base_url.rstrip("/") + normalized_path,
        api_version=api_version,
    )
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(endpoint, data=body, headers=request_headers, method=method)
    try:
        with open_provider_url(request, timeout=timeout_seconds, enforce_public_peer=True) as response:
            raw_payload = response.read()
    except HTTPError as exc:
        raise ModelInvocationError(f"model provider returned HTTP {exc.code}{redirect_note(exc.code)}") from exc
    except TimeoutError as exc:
        raise ModelProviderUnavailableError("model provider request timed out") from exc
    except URLError as exc:
        raise ModelProviderUnavailableError("model provider request failed") from exc

    try:
        parsed_payload = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelInvocationError("model provider returned invalid JSON") from exc

    if not isinstance(parsed_payload, dict):
        raise ModelInvocationError("model provider returned invalid JSON")
    return parsed_payload


def parse_azure_models(payload: dict[str, Any]) -> list[str]:
    data_payload = payload.get("data")
    if not isinstance(data_payload, list):
        raise ModelInvocationError("azure model enumeration payload is invalid")
    model_names: set[str] = set()
    for model_payload in data_payload:
        if not isinstance(model_payload, dict):
            continue
        raw_name = model_payload.get("id")
        if isinstance(raw_name, str) and raw_name.strip():
            model_names.add(raw_name.strip())
    return sorted(model_names)


def invoke_azure_openai_responses(
    *,
    request: ModelInvocationRequest,
    base_url: str,
    auth_mode: str,
    credential: str,
    api_version: str,
    invoke_path: str,
    timeout_seconds: int,
) -> ModelInvocationResponse:
    if request.provider != "openai_responses":
        raise ModelInvocationError(f"unsupported model provider: {request.provider}")
    headers = build_azure_auth_headers(auth_mode=auth_mode, credential=credential)
    payload = request_azure_json(
        method="POST",
        base_url=base_url,
        path=invoke_path,
        api_version=api_version,
        timeout_seconds=timeout_seconds,
        headers=headers,
        payload=_build_openai_responses_payload(request),
    )
    output_text = _extract_output_text(payload)
    finish_reason: ModelFinishReason = (
        "completed" if payload.get("status") == "completed" else "incomplete"
    )
    response_id = payload.get("id")
    return ModelInvocationResponse(
        provider=request.provider,
        model=request.model,
        response_id=response_id if isinstance(response_id, str) else None,
        finish_reason=finish_reason,
        output_text=output_text,
        usage=_parse_usage(payload),
    )


def _append_api_version(*, url: str, api_version: str) -> str:
    version = api_version.strip()
    if version == "":
        raise ModelInvocationError("azure api_version is required")
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_without_api_version = [(key, value) for key, value in query_items if key != "api-version"]
    query_without_api_version.append(("api-version", version))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query_without_api_version),
            parts.fragment,
        )
    )


def _openai_input_message(role: str, content: str) -> dict[str, object]:
    return {
        "role": role,
        "content": [{"type": "input_text", "text": content}],
    }


def _build_openai_responses_payload(request: ModelInvocationRequest) -> dict[str, object]:
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


def _extract_output_text(payload: dict[str, Any]) -> str:
    output_items = payload.get("output")
    if not isinstance(output_items, list):
        raise ModelInvocationError("model response did not include assistant output text")
    for output_item in output_items:
        if not isinstance(output_item, dict) or output_item.get("type") != "message":
            continue
        content_items = output_item.get("content")
        if not isinstance(content_items, list):
            continue
        for content_item in content_items:
            if not isinstance(content_item, dict) or content_item.get("type") != "output_text":
                continue
            text = content_item.get("text")
            if isinstance(text, str) and text:
                return text
    raise ModelInvocationError("model response did not include assistant output text")


def _parse_usage(payload: dict[str, Any]) -> ModelUsagePayload:
    usage_payload = payload.get("usage")
    if not isinstance(usage_payload, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    usage: ModelUsagePayload = {
        "input_tokens": (
            usage_payload.get("input_tokens")
            if isinstance(usage_payload.get("input_tokens"), int)
            else None
        ),
        "output_tokens": (
            usage_payload.get("output_tokens")
            if isinstance(usage_payload.get("output_tokens"), int)
            else None
        ),
        "total_tokens": (
            usage_payload.get("total_tokens")
            if isinstance(usage_payload.get("total_tokens"), int)
            else None
        ),
    }

    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage_payload.get(details_key)
        if not isinstance(details, dict):
            continue
        cached_tokens = details.get("cached_tokens")
        if isinstance(cached_tokens, int):
            usage["cached_input_tokens"] = cached_tokens
            break

    return usage
