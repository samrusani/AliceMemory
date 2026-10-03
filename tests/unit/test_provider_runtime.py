from __future__ import annotations

import json
import socket
from uuid import uuid4

import pytest

from alicebot_api.config import Settings
from alicebot_api.provider_runtime import (
    AZURE_ADAPTER_KEY,
    LLAMACPP_ADAPTER_KEY,
    OLLAMA_ADAPTER_KEY,
    OPENAI_COMPATIBLE_ADAPTER_KEY,
    OPENAI_RESPONSES_PROVIDER,
    VLLM_ADAPTER_KEY,
    ProviderAdapterNotFoundError,
    RuntimeProviderConfig,
    build_provider_test_model_request,
    make_provider_adapter_registry,
    resolve_runtime_provider_config_secrets,
)
from alicebot_api.provider_secrets import (
    ProviderSecretManagerError,
    build_provider_secret_ref,
    encode_provider_secret_ref,
    write_provider_api_key,
)
from alicebot_api.response_generation import (
    ModelInvocationError,
    ModelProviderUnavailableError,
)


class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_local_and_azure_provider_timeouts_are_typed(monkeypatch) -> None:
    from alicebot_api.azure_provider_helpers import request_azure_json
    from alicebot_api.local_provider_helpers import request_json

    def timeout_urlopen(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("alicebot_api.local_provider_helpers.open_provider_url", timeout_urlopen)
    monkeypatch.setattr("alicebot_api.azure_provider_helpers.open_provider_url", timeout_urlopen)

    with pytest.raises(ModelProviderUnavailableError, match="timed out"):
        request_json(
            method="GET",
            base_url="https://provider.example",
            path="/models",
            timeout_seconds=1,
        )
    with pytest.raises(ModelProviderUnavailableError, match="timed out"):
        request_azure_json(
            method="GET",
            base_url="https://azure.example",
            path="/openai/models",
            api_version="2024-10-21",
            timeout_seconds=1,
        )


def test_local_and_azure_non_utf8_responses_are_typed(monkeypatch) -> None:
    from alicebot_api.azure_provider_helpers import request_azure_json
    from alicebot_api.local_provider_helpers import request_json

    monkeypatch.setattr(
        "alicebot_api.local_provider_helpers.open_provider_url",
        lambda *_args, **_kwargs: FakeHTTPResponse(b"\xff\xfe\xfa"),
    )
    monkeypatch.setattr(
        "alicebot_api.azure_provider_helpers.open_provider_url",
        lambda *_args, **_kwargs: FakeHTTPResponse(b"\xff\xfe\xfa"),
    )

    with pytest.raises(ModelInvocationError, match="invalid JSON"):
        request_json(
            method="GET",
            base_url="https://provider.example",
            path="/models",
            timeout_seconds=1,
        )
    with pytest.raises(ModelInvocationError, match="invalid JSON"):
        request_azure_json(
            method="GET",
            base_url="https://azure.example",
            path="/openai/models",
            api_version="2024-10-21",
            timeout_seconds=1,
        )


@pytest.fixture(autouse=True)
def allow_documentation_provider_hosts(monkeypatch) -> None:
    original_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(hostname: str, port, type=0, proto=0):
        if hostname.endswith(".example"):
            sockaddr = ("93.184.216.34", 0)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
        return original_getaddrinfo(hostname, port, type=type, proto=proto)

    monkeypatch.setattr("alicebot_api.provider_security.socket.getaddrinfo", fake_getaddrinfo)


def make_runtime_provider_config(
    *,
    provider_key: str = OPENAI_COMPATIBLE_ADAPTER_KEY,
    base_url: str = "https://provider.example/v1",
    api_key: str = "provider-secret-key",
    auth_mode: str = "bearer",
    model_list_path: str = "/models",
    healthcheck_path: str = "/models",
    invoke_path: str = "/responses",
    azure_api_version: str = "",
    azure_auth_secret_ref: str = "",
) -> RuntimeProviderConfig:
    return RuntimeProviderConfig(
        provider_id=uuid4(),
        workspace_id=uuid4(),
        created_by_user_account_id=uuid4(),
        provider_key=provider_key,
        display_name="Primary Provider",
        model_provider=OPENAI_RESPONSES_PROVIDER,
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        default_model="gpt-5-mini",
        status="active",
        model_list_path=model_list_path,
        healthcheck_path=healthcheck_path,
        invoke_path=invoke_path,
        azure_api_version=azure_api_version,
        azure_auth_secret_ref=azure_auth_secret_ref,
        metadata={},
    )


def test_provider_registry_resolves_registered_adapter() -> None:
    registry = make_provider_adapter_registry()

    adapter = registry.resolve(OPENAI_COMPATIBLE_ADAPTER_KEY)
    azure_adapter = registry.resolve(AZURE_ADAPTER_KEY)
    ollama_adapter = registry.resolve(OLLAMA_ADAPTER_KEY)
    llamacpp_adapter = registry.resolve(LLAMACPP_ADAPTER_KEY)
    vllm_adapter = registry.resolve(VLLM_ADAPTER_KEY)

    assert adapter.adapter_key == OPENAI_COMPATIBLE_ADAPTER_KEY
    assert adapter.runtime_provider == OPENAI_RESPONSES_PROVIDER
    assert azure_adapter.adapter_key == AZURE_ADAPTER_KEY
    assert ollama_adapter.adapter_key == OLLAMA_ADAPTER_KEY
    assert llamacpp_adapter.adapter_key == LLAMACPP_ADAPTER_KEY
    assert vllm_adapter.adapter_key == VLLM_ADAPTER_KEY
    assert registry.keys() == [
        AZURE_ADAPTER_KEY,
        LLAMACPP_ADAPTER_KEY,
        OLLAMA_ADAPTER_KEY,
        OPENAI_COMPATIBLE_ADAPTER_KEY,
        VLLM_ADAPTER_KEY,
    ]


def test_provider_registry_rejects_unknown_adapter() -> None:
    registry = make_provider_adapter_registry()

    try:
        registry.resolve("unknown_provider")
    except ProviderAdapterNotFoundError as exc:
        assert "not registered" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("expected ProviderAdapterNotFoundError")


def test_build_provider_test_model_request_builds_expected_prompt_shape() -> None:
    request = build_provider_test_model_request(
        runtime_provider=OPENAI_RESPONSES_PROVIDER,
        model="gpt-5-mini",
        prompt_text="Confirm runtime is reachable.",
    )

    assert request.provider == OPENAI_RESPONSES_PROVIDER
    assert request.model == "gpt-5-mini"
    assert [section.name for section in request.prompt.sections] == [
        "system",
        "developer",
        "context",
        "conversation",
    ]
    assert "provider_test" in request.prompt.sections[2].content
    assert "Confirm runtime is reachable." in request.prompt.sections[3].content


def test_openai_compatible_adapter_invokes_registered_transport(monkeypatch) -> None:
    captured: dict[str, object] = {}
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(OPENAI_COMPATIBLE_ADAPTER_KEY)
    runtime_provider = make_runtime_provider_config(invoke_path="/responses-alt")

    def fake_urlopen(request, timeout, enforce_public_peer):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = None if request.data is None else json.loads(request.data.decode("utf-8"))
        if request.full_url.endswith("/models"):
            return FakeHTTPResponse(
                json.dumps({"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-5-mini"}]}).encode("utf-8")
            )
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "resp_provider_test",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Provider online"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 14,
                        "output_tokens": 3,
                        "total_tokens": 17,
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.local_provider_helpers.open_provider_url", fake_urlopen)
    monkeypatch.setattr("alicebot_api.response_generation.open_provider_url", fake_urlopen)

    capabilities = adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=9),
    )
    response = adapter.invoke(
        config=runtime_provider,
        settings=Settings(model_timeout_seconds=27),
        request=build_provider_test_model_request(
            runtime_provider=OPENAI_RESPONSES_PROVIDER,
            model="gpt-5-mini",
            prompt_text="Are you available?",
        ),
    )

    assert capabilities["health_status"] == "ok"
    assert capabilities["models"] == ["gpt-4.1-mini", "gpt-5-mini"]
    assert capabilities["models_endpoint"] == "/models"
    assert capabilities["invoke_endpoint"] == "/responses-alt"
    assert capabilities["supports_reasoning"] is False
    assert captured["url"] == "https://provider.example/v1/responses-alt"
    assert captured["timeout"] == 27
    assert captured["headers"]["Authorization"] == "Bearer provider-secret-key"
    assert response.provider == OPENAI_RESPONSES_PROVIDER
    assert response.model == "gpt-5-mini"
    assert response.output_text == "Provider online"


def test_openai_compatible_adapter_omits_auth_for_bootstrapped_no_auth_config(
    monkeypatch,
) -> None:
    captured_requests: list[dict[str, object]] = []
    captured_headers: list[dict[str, str]] = []
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(OPENAI_COMPATIBLE_ADAPTER_KEY)
    runtime_provider = resolve_runtime_provider_config_secrets(
        config=make_runtime_provider_config(
            api_key="auth_mode_none",
            auth_mode="none",
        ),
        settings=Settings(),
    )

    def fake_urlopen(request, timeout, enforce_public_peer):
        headers = dict(request.header_items())
        captured_headers.append(headers)
        captured_requests.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": headers,
            }
        )
        if request.full_url.endswith("/models"):
            return FakeHTTPResponse(json.dumps({"data": [{"id": "local-model"}]}).encode("utf-8"))
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "resp_no_auth",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "No-auth provider online"}],
                        }
                    ],
                    "usage": {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11},
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.local_provider_helpers.open_provider_url", fake_urlopen)
    monkeypatch.setattr("alicebot_api.response_generation.open_provider_url", fake_urlopen)

    capabilities = adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=5),
    )
    response = adapter.invoke(
        config=runtime_provider,
        settings=Settings(model_timeout_seconds=11),
        request=build_provider_test_model_request(
            runtime_provider=OPENAI_RESPONSES_PROVIDER,
            model="local-model",
            prompt_text="Are you available without authentication?",
        ),
    )

    assert runtime_provider.auth_mode == "none"
    assert runtime_provider.api_key == "auth_mode_none"
    assert capabilities["models"] == ["local-model"]
    assert response.output_text == "No-auth provider online"
    assert len(captured_requests) == 3
    assert all(
        "authorization" not in {key.lower() for key in headers}
        for headers in captured_headers
    )


def test_ollama_adapter_discovers_capabilities_and_invokes(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(OLLAMA_ADAPTER_KEY)
    runtime_provider = make_runtime_provider_config(
        provider_key=OLLAMA_ADAPTER_KEY,
        base_url="http://ollama.example:11434",
        api_key="",
        auth_mode="none",
        model_list_path="/api/tags",
        healthcheck_path="/api/version",
        invoke_path="/api/chat",
    )

    def fake_urlopen(request, timeout, enforce_public_peer):
        body = None if request.data is None else json.loads(request.data.decode("utf-8"))
        captured.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": body,
            }
        )
        if request.full_url.endswith("/api/version"):
            return FakeHTTPResponse(json.dumps({"version": "0.4.0"}).encode("utf-8"))
        if request.full_url.endswith("/api/tags"):
            return FakeHTTPResponse(
                json.dumps(
                    {
                        "models": [
                            {"name": "llama3.2:latest"},
                            {"name": "qwen2.5:latest"},
                        ]
                    }
                ).encode("utf-8")
            )
        return FakeHTTPResponse(
            json.dumps(
                {
                    "model": "llama3.2:latest",
                    "done": True,
                    "message": {"role": "assistant", "content": "Local Ollama reply"},
                    "prompt_eval_count": 20,
                    "eval_count": 6,
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.local_provider_helpers.open_provider_url", fake_urlopen)

    capabilities = adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=5),
    )
    response = adapter.invoke(
        config=runtime_provider,
        settings=Settings(model_timeout_seconds=11),
        request=build_provider_test_model_request(
            runtime_provider=OPENAI_RESPONSES_PROVIDER,
            model="llama3.2:latest",
            prompt_text="Reply from local ollama",
        ),
    )

    assert capabilities["adapter_key"] == OLLAMA_ADAPTER_KEY
    assert capabilities["health_status"] == "ok"
    assert capabilities["model_count"] == 2
    assert capabilities["models"] == ["llama3.2:latest", "qwen2.5:latest"]
    assert response.output_text == "Local Ollama reply"
    assert response.usage["input_tokens"] == 20
    assert response.usage["output_tokens"] == 6
    assert captured[0]["url"] == "http://ollama.example:11434/api/version"
    assert captured[1]["url"] == "http://ollama.example:11434/api/tags"
    assert captured[2]["url"] == "http://ollama.example:11434/api/chat"


def test_llamacpp_adapter_discovers_capabilities_and_invokes(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(LLAMACPP_ADAPTER_KEY)
    runtime_provider = make_runtime_provider_config(
        provider_key=LLAMACPP_ADAPTER_KEY,
        base_url="http://llamacpp.example:8080",
        api_key="",
        auth_mode="none",
        model_list_path="/v1/models",
        healthcheck_path="/health",
        invoke_path="/v1/chat/completions",
    )

    def fake_urlopen(request, timeout, enforce_public_peer):
        body = None if request.data is None else json.loads(request.data.decode("utf-8"))
        captured.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": body,
            }
        )
        if request.full_url.endswith("/health"):
            return FakeHTTPResponse(json.dumps({"status": "ok"}).encode("utf-8"))
        if request.full_url.endswith("/v1/models"):
            return FakeHTTPResponse(
                json.dumps({"data": [{"id": "Llama-3.2-3B-Instruct-Q4_K_M"}]}).encode("utf-8")
            )
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "chatcmpl-local-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "llama.cpp says hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 18,
                        "completion_tokens": 4,
                        "total_tokens": 22,
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.local_provider_helpers.open_provider_url", fake_urlopen)

    capabilities = adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=5),
    )
    response = adapter.invoke(
        config=runtime_provider,
        settings=Settings(model_timeout_seconds=11),
        request=build_provider_test_model_request(
            runtime_provider=OPENAI_RESPONSES_PROVIDER,
            model="Llama-3.2-3B-Instruct-Q4_K_M",
            prompt_text="Reply from local llamacpp",
        ),
    )

    assert capabilities["adapter_key"] == LLAMACPP_ADAPTER_KEY
    assert capabilities["health_status"] == "ok"
    assert capabilities["model_count"] == 1
    assert capabilities["models"] == ["Llama-3.2-3B-Instruct-Q4_K_M"]
    assert response.output_text == "llama.cpp says hi"
    assert response.response_id == "chatcmpl-local-1"
    assert response.usage["total_tokens"] == 22
    assert captured[0]["url"] == "http://llamacpp.example:8080/health"
    assert captured[1]["url"] == "http://llamacpp.example:8080/v1/models"
    assert captured[2]["url"] == "http://llamacpp.example:8080/v1/chat/completions"


def test_vllm_adapter_discovers_capabilities_and_invokes(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(VLLM_ADAPTER_KEY)
    runtime_provider = make_runtime_provider_config(
        provider_key=VLLM_ADAPTER_KEY,
        base_url="http://vllm.example:8001",
        api_key="",
        auth_mode="none",
        model_list_path="/v1/models",
        healthcheck_path="/health",
        invoke_path="/v1/chat/completions",
    )

    def fake_urlopen(request, timeout, enforce_public_peer):
        body = None if request.data is None else json.loads(request.data.decode("utf-8"))
        captured.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": body,
            }
        )
        if request.full_url.endswith("/health"):
            return FakeHTTPResponse(b"")
        if request.full_url.endswith("/v1/models"):
            return FakeHTTPResponse(
                json.dumps({"data": [{"id": "mistral-small-instruct"}, {"id": "deepseek-r1-distill"}]}).encode("utf-8")
            )
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "chatcmpl-vllm-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "vLLM says hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 16,
                        "completion_tokens": 5,
                        "total_tokens": 21,
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.local_provider_helpers.open_provider_url", fake_urlopen)

    capabilities = adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=5),
    )
    response = adapter.invoke(
        config=runtime_provider,
        settings=Settings(model_timeout_seconds=11),
        request=build_provider_test_model_request(
            runtime_provider=OPENAI_RESPONSES_PROVIDER,
            model="mistral-small-instruct",
            prompt_text="Reply from self-hosted vllm",
        ),
    )

    assert capabilities["adapter_key"] == VLLM_ADAPTER_KEY
    assert capabilities["health_status"] == "ok"
    assert capabilities["model_count"] == 2
    assert capabilities["models"] == ["deepseek-r1-distill", "mistral-small-instruct"]
    assert response.output_text == "vLLM says hi"
    assert response.response_id == "chatcmpl-vllm-1"
    assert response.usage["total_tokens"] == 21
    assert captured[0]["url"] == "http://vllm.example:8001/health"
    assert captured[1]["url"] == "http://vllm.example:8001/v1/models"
    assert captured[2]["url"] == "http://vllm.example:8001/v1/chat/completions"


def test_azure_adapter_discovers_capabilities_and_invokes_with_api_key(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(AZURE_ADAPTER_KEY)
    runtime_provider = make_runtime_provider_config(
        provider_key=AZURE_ADAPTER_KEY,
        base_url="https://azure.example",
        api_key="azure-api-key",
        auth_mode="azure_api_key",
        model_list_path="/openai/models",
        healthcheck_path="/openai/models",
        invoke_path="/openai/responses",
        azure_api_version="2024-10-21",
    )

    def fake_urlopen(request, timeout, enforce_public_peer):
        body = None if request.data is None else json.loads(request.data.decode("utf-8"))
        captured.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "body": body,
            }
        )
        if request.full_url.startswith("https://azure.example/openai/models"):
            return FakeHTTPResponse(json.dumps({"data": [{"id": "gpt-4.1-mini"}]}).encode("utf-8"))
        return FakeHTTPResponse(
            json.dumps(
                {
                    "id": "resp_azure_test_1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "Azure provider online"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 4,
                        "total_tokens": 15,
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("alicebot_api.azure_provider_helpers.open_provider_url", fake_urlopen)

    capabilities = adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=5),
    )
    response = adapter.invoke(
        config=runtime_provider,
        settings=Settings(model_timeout_seconds=12),
        request=build_provider_test_model_request(
            runtime_provider=OPENAI_RESPONSES_PROVIDER,
            model="gpt-4.1-mini",
            prompt_text="Azure runtime check",
        ),
    )

    assert capabilities["adapter_key"] == AZURE_ADAPTER_KEY
    assert capabilities["health_status"] == "ok"
    assert capabilities["model_count"] == 1
    assert capabilities["models"] == ["gpt-4.1-mini"]
    assert capabilities["azure_api_version"] == "2024-10-21"
    assert capabilities["azure_auth_mode"] == "azure_api_key"
    assert response.output_text == "Azure provider online"
    assert response.usage["total_tokens"] == 15
    assert captured[0]["url"] == "https://azure.example/openai/models?api-version=2024-10-21"
    assert captured[1]["url"] == "https://azure.example/openai/models?api-version=2024-10-21"
    assert captured[2]["url"] == "https://azure.example/openai/responses?api-version=2024-10-21"
    azure_headers = {key.lower(): value for key, value in captured[2]["headers"].items()}
    assert azure_headers["api-key"] == "azure-api-key"
    assert "authorization" not in azure_headers


def test_azure_adapter_uses_bearer_token_auth_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}
    registry = make_provider_adapter_registry()
    adapter = registry.resolve(AZURE_ADAPTER_KEY)
    runtime_provider = make_runtime_provider_config(
        provider_key=AZURE_ADAPTER_KEY,
        base_url="https://foundry.example",
        api_key="entra-token-value",
        auth_mode="azure_ad_token",
        model_list_path="/openai/models",
        healthcheck_path="/openai/models",
        invoke_path="/openai/responses",
        azure_api_version="2024-10-21",
    )

    def fake_urlopen(request, timeout, enforce_public_peer):
        del timeout
        captured["headers"] = dict(request.header_items())
        return FakeHTTPResponse(json.dumps({"data": [{"id": "gpt-4.1"}]}).encode("utf-8"))

    monkeypatch.setattr("alicebot_api.azure_provider_helpers.open_provider_url", fake_urlopen)

    adapter.discover_capabilities(
        config=runtime_provider,
        settings=Settings(healthcheck_timeout_seconds=5),
    )

    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["authorization"] == "Bearer entra-token-value"
    assert "api-key" not in headers


def test_resolve_runtime_provider_config_secrets_for_azure_secret_ref(tmp_path) -> None:
    settings = Settings(provider_secret_manager_url=f"file://{tmp_path}")
    secret_ref = build_provider_secret_ref(workspace_id=uuid4())
    write_provider_api_key(
        settings=settings,
        secret_ref=secret_ref,
        api_key="resolved-azure-secret",
    )
    config = make_runtime_provider_config(
        provider_key=AZURE_ADAPTER_KEY,
        auth_mode="azure_api_key",
        api_key="auth_mode_azure_secret_ref",
        azure_auth_secret_ref=encode_provider_secret_ref(secret_ref=secret_ref),
        azure_api_version="2024-10-21",
    )

    resolved = resolve_runtime_provider_config_secrets(
        config=config,
        settings=settings,
    )

    assert resolved.api_key == "resolved-azure-secret"


def test_resolve_runtime_provider_config_secrets_rejects_missing_azure_secret_ref() -> None:
    config = make_runtime_provider_config(
        provider_key=AZURE_ADAPTER_KEY,
        auth_mode="azure_api_key",
        api_key="auth_mode_azure_secret_ref",
        azure_auth_secret_ref="",
        azure_api_version="2024-10-21",
    )

    try:
        resolve_runtime_provider_config_secrets(
            config=config,
            settings=Settings(),
        )
    except ProviderSecretManagerError as exc:
        assert "azure_auth_secret_ref is required" in str(exc)
    else:  # pragma: no cover - defensive guard
        raise AssertionError("expected ProviderSecretManagerError")
