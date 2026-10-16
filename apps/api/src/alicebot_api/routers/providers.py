from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, model_validator
import psycopg
from psycopg.rows import dict_row

from alicebot_api.http_json_types import BoundedJsonObject
from alicebot_api.azure_provider_helpers import (
    AZURE_AUTH_MODE_AD_TOKEN,
    AZURE_AUTH_MODE_API_KEY,
    DEFAULT_AZURE_API_VERSION,
)
from alicebot_api.config import Settings, get_settings
from alicebot_api.contracts import (
    ContextCompilerLimits,
    DEFAULT_MAX_ENTITIES,
    DEFAULT_MAX_ENTITY_EDGES,
    DEFAULT_MAX_EVENTS,
    DEFAULT_MAX_MEMORIES,
    DEFAULT_MAX_SESSIONS,
    GenerateResponseSuccess,
    ModelInvocationRequest,
    ModelInvocationResponse,
    PROVIDER_LIST_ORDER,
)
from alicebot_api.db import set_current_user_account, user_connection
from alicebot_api.local_workspace import get_local_workspace
from alicebot_api.provider_configuration import provider_config_fingerprint
from alicebot_api.provider_runtime import (
    AZURE_ADAPTER_KEY,
    LLAMACPP_ADAPTER_KEY,
    OLLAMA_ADAPTER_KEY,
    OPENAI_RESPONSES_PROVIDER,
    VLLM_ADAPTER_KEY,
    ProviderAdapter,
    ProviderAdapterNotFoundError,
    ProviderCapabilitySnapshot,
    RuntimeProviderConfig,
    build_provider_test_model_request,
    make_provider_adapter_registry,
    normalized_capability_snapshot,
    resolve_runtime_provider_config_secrets,
)
from alicebot_api.provider_secrets import (
    ProviderSecretManagerError,
    build_provider_secret_ref,
    decode_provider_secret_ref,
    delete_provider_api_key,
    encode_provider_secret_ref,
    is_provider_secret_ref,
    write_provider_api_key,
)
from alicebot_api.provider_security import (
    sanitize_provider_error_message,
    validate_provider_base_url,
)
from alicebot_api.public_errors import (
    CONFLICT,
    UPSTREAM_FAILURE,
    public_exception_response,
)
from alicebot_api.response_generation import (
    DEVELOPER_INSTRUCTION,
    ModelInvocationError,
    ModelProviderUnavailableError,
    ResponseFailure,
    ResponseGenerationConflictError,
    SYSTEM_INSTRUCTION,
    complete_response_generation,
    fail_response_generation,
    prepare_response_generation,
)
from alicebot_api.response_jobs import (
    RESPONSE_JOB_ENDPOINT_RUNTIME,
    RESPONSE_JOB_LEASE_SECONDS,
    ResponseGenerationJobRow,
    ResponseGenerationJobStore,
    ResponseJobFenceLostError,
    normalize_idempotency_key,
    request_fingerprint,
)
from alicebot_api.routers._api_shared import (
    LOGGER,
    _json_object,
    _resolve_authenticated_v1_user_id,
)
from alicebot_api.routers._vnext_shared import BaseModel
from alicebot_api.store import (
    ContinuityStore,
    ContinuityStoreInvariantError,
    JsonObject,
    ModelProviderRow,
    ProviderCapabilityRow,
)


router = APIRouter()


provider_adapter_registry = make_provider_adapter_registry()


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected a mapping row")
    output: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("row keys must be strings")
        output[key] = item
    return output


def _serialize_model_provider(provider: ModelProviderRow) -> dict[str, object]:
    return {
        "id": str(provider["id"]),
        "workspace_id": str(provider["workspace_id"]),
        "created_by_user_account_id": str(provider["created_by_user_account_id"]),
        "provider_key": provider["provider_key"],
        "model_provider": provider["model_provider"],
        "display_name": provider["display_name"],
        "base_url": redact_url_credentials(provider["base_url"]),
        "auth_mode": provider["auth_mode"],
        "default_model": provider["default_model"],
        "status": provider["status"],
        "model_list_path": provider["model_list_path"],
        "healthcheck_path": provider["healthcheck_path"],
        "invoke_path": provider["invoke_path"],
        "azure_api_version": provider["azure_api_version"],
        "metadata": provider["metadata"],
        "config_revision": provider["config_revision"],
        "created_at": provider["created_at"].isoformat(),
        "updated_at": provider["updated_at"].isoformat(),
    }


def _serialize_provider_capability(capability: ProviderCapabilityRow) -> dict[str, object]:
    snapshot = capability["capability_snapshot"]
    capability_version = snapshot.get("capability_version")
    if not isinstance(capability_version, str) or capability_version == "":
        capability_version = "provider_capability_v1"
    return {
        "provider_id": str(capability["provider_id"]),
        "adapter_key": capability["adapter_key"],
        "discovery_status": capability["discovery_status"],
        "capability_version": capability_version,
        "snapshot": snapshot,
        "discovery_error": capability["discovery_error"],
        "provider_config_revision": capability["provider_config_revision"],
        "discovered_at": capability["discovered_at"].isoformat(),
    }


def _runtime_provider_config_or_none(
    *,
    store: ContinuityStore,
    provider_id: UUID,
    workspace_id: UUID,
    settings: Settings,
) -> RuntimeProviderConfig | None:
    row = store.get_model_provider_for_workspace_optional(
        provider_id=provider_id,
        workspace_id=workspace_id,
    )
    if row is None:
        return None
    validate_provider_base_url(row["base_url"])
    return resolve_runtime_provider_config_secrets(
        config=RuntimeProviderConfig.from_row(_object_dict(row)),
        settings=settings,
    )


def _normalize_provider_path(*, field_name: str, value: str) -> str:
    path = value.strip()
    if path == "":
        raise ValueError(f"{field_name} is required")
    return path if path.startswith("/") else f"/{path}"


def _provider_config_fingerprint(
    *,
    provider_key: str,
    model_provider: str,
    display_name: str,
    base_url: str,
    api_key: str,
    auth_mode: str,
    default_model: str,
    status: str,
    model_list_path: str,
    healthcheck_path: str,
    invoke_path: str,
    azure_api_version: str,
    azure_auth_secret_ref: str,
    metadata: JsonObject,
) -> str:
    return provider_config_fingerprint(
        provider_key=provider_key,
        model_provider=model_provider,
        display_name=display_name,
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        default_model=default_model,
        status=status,
        model_list_path=model_list_path,
        healthcheck_path=healthcheck_path,
        invoke_path=invoke_path,
        azure_api_version=azure_api_version,
        azure_auth_secret_ref=azure_auth_secret_ref,
        metadata=metadata,
    )


def _fallback_provider_capability_snapshot(
    *,
    adapter_key: str,
    runtime_provider: str,
    model_list_path: str,
    healthcheck_path: str,
    invoke_path: str,
    extra_snapshot_fields: dict[str, str] | None = None,
) -> ProviderCapabilitySnapshot:
    snapshot = normalized_capability_snapshot(
        adapter_key=adapter_key,
        runtime_provider=runtime_provider,
        supports_tool_calls=False,
        supports_reasoning=False,
        supports_streaming=False,
        supports_store=False,
        supports_vision_input=False,
        supports_audio_input=False,
    )
    snapshot["health_status"] = "unreachable"
    snapshot["health_endpoint"] = healthcheck_path
    snapshot["models_endpoint"] = model_list_path
    snapshot["invoke_endpoint"] = invoke_path
    snapshot["model_count"] = 0
    snapshot["models"] = []
    if extra_snapshot_fields:
        azure_api_version = extra_snapshot_fields.get("azure_api_version")
        azure_auth_mode = extra_snapshot_fields.get("azure_auth_mode")
        if azure_api_version is not None:
            snapshot["azure_api_version"] = azure_api_version
        if azure_auth_mode is not None:
            snapshot["azure_auth_mode"] = azure_auth_mode
    return snapshot


@dataclass(frozen=True, slots=True)
class _ProviderDiscoveryOutcome:
    adapter_key: str
    discovery_status: str
    capability_snapshot: JsonObject
    discovery_error: str | None


def _discover_provider_capability(
    *,
    provider: ModelProviderRow,
    settings: Settings,
) -> _ProviderDiscoveryOutcome:
    """Perform provider discovery without holding a database transaction."""

    runtime_provider = resolve_runtime_provider_config_secrets(
        config=RuntimeProviderConfig.from_row(_object_dict(provider)),
        settings=settings,
    )
    adapter = provider_adapter_registry.resolve(runtime_provider.provider_key)
    try:
        snapshot = adapter.discover_capabilities(
            config=runtime_provider,
            settings=settings,
        )
    except ModelInvocationError as exc:
        discovery_error = sanitize_provider_error_message(str(exc))
        extra_snapshot_fields = None
        if runtime_provider.provider_key == AZURE_ADAPTER_KEY:
            extra_snapshot_fields = {
                "azure_api_version": runtime_provider.azure_api_version.strip() or DEFAULT_AZURE_API_VERSION,
                "azure_auth_mode": runtime_provider.auth_mode,
            }
        snapshot = _fallback_provider_capability_snapshot(
            adapter_key=adapter.adapter_key,
            runtime_provider=adapter.runtime_provider,
            model_list_path=runtime_provider.model_list_path,
            healthcheck_path=runtime_provider.healthcheck_path,
            invoke_path=runtime_provider.invoke_path,
            extra_snapshot_fields=extra_snapshot_fields,
        )
        return _ProviderDiscoveryOutcome(
            adapter_key=adapter.adapter_key,
            discovery_status="failed",
            capability_snapshot=_json_object(snapshot),
            discovery_error=discovery_error,
        )
    return _ProviderDiscoveryOutcome(
        adapter_key=adapter.adapter_key,
        discovery_status="ready",
        capability_snapshot=_json_object(snapshot),
        discovery_error=None,
    )


def _persist_discovered_provider_capability(
    *,
    settings: Settings,
    user_account_id: UUID,
    workspace_id: UUID,
    provider: ModelProviderRow,
    outcome: _ProviderDiscoveryOutcome,
) -> ProviderCapabilityRow | None:
    """Persist discovery only if the exact provider configuration is current."""

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        # label state: none, provider tables carry no label and this connection has no vNext store.
        with conn.transaction():
            context = get_local_workspace(conn, user_account_id=user_account_id)
            if context is None or context["workspace"]["id"] != workspace_id:
                return None
            return ContinuityStore(conn).upsert_provider_capability_if_current(
                workspace_id=workspace_id,
                provider_id=provider["id"],
                discovered_by_user_account_id=user_account_id,
                adapter_key=outcome.adapter_key,
                discovery_status=outcome.discovery_status,
                capability_snapshot=outcome.capability_snapshot,
                discovery_error=outcome.discovery_error,
                expected_config_revision=provider["config_revision"],
                expected_config_fingerprint_sha256=provider["config_fingerprint_sha256"],
            )


@dataclass(frozen=True, slots=True)
class _RuntimeProviderInvocationOutcome:
    response: ModelInvocationResponse | None
    error: ModelInvocationError | None
    latency_ms: int
    error_detail: str | None


def _attempt_runtime_provider_model(
    *,
    adapter: ProviderAdapter,
    runtime_provider: RuntimeProviderConfig,
    settings: Settings,
    model_request: ModelInvocationRequest,
) -> _RuntimeProviderInvocationOutcome:
    """Perform only the external provider call; no persistence handle is required."""

    started_at = time.monotonic()
    try:
        model_response = adapter.invoke(
            config=runtime_provider,
            settings=settings,
            request=model_request,
        )
    except ValueError as exc:
        LOGGER.exception(
            "Provider invocation failed with public error code=%s status=%d",
            UPSTREAM_FAILURE.code,
            UPSTREAM_FAILURE.status_code,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "public_error_code": UPSTREAM_FAILURE.code,
                "public_error_status": UPSTREAM_FAILURE.status_code,
            },
        )
        error_detail = UPSTREAM_FAILURE.message
        return _RuntimeProviderInvocationOutcome(
            response=None,
            error=ModelInvocationError(error_detail),
            latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            error_detail=error_detail,
        )
    except ModelInvocationError as exc:
        LOGGER.exception(
            "Provider invocation failed with public error code=%s status=%d",
            UPSTREAM_FAILURE.code,
            UPSTREAM_FAILURE.status_code,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "public_error_code": UPSTREAM_FAILURE.code,
                "public_error_status": UPSTREAM_FAILURE.status_code,
            },
        )
        error: ModelInvocationError
        if isinstance(exc, ModelProviderUnavailableError):
            error = ModelProviderUnavailableError(UPSTREAM_FAILURE.message)
        else:
            error = ModelInvocationError(UPSTREAM_FAILURE.message)
        return _RuntimeProviderInvocationOutcome(
            response=None,
            error=error,
            latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            error_detail=UPSTREAM_FAILURE.message,
        )
    return _RuntimeProviderInvocationOutcome(
        response=model_response,
        error=None,
        latency_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        error_detail=None,
    )


def _record_runtime_provider_invocation(
    *,
    store: ContinuityStore,
    workspace_id: UUID,
    invoked_by_user_account_id: UUID,
    thread_id: UUID | None,
    invocation_kind: str,
    adapter: ProviderAdapter,
    runtime_provider: RuntimeProviderConfig,
    model_request: ModelInvocationRequest,
    outcome: _RuntimeProviderInvocationOutcome,
) -> None:
    """Persist provider telemetry after the network call has finished."""

    model_response = outcome.response
    store.record_provider_invocation_telemetry(
        workspace_id=workspace_id,
        provider_id=runtime_provider.provider_id,
        thread_id=thread_id,
        invoked_by_user_account_id=invoked_by_user_account_id,
        invocation_kind=invocation_kind,
        adapter_key=adapter.adapter_key,
        runtime_provider=runtime_provider.model_provider,
        requested_model=model_request.model,
        response_model=model_response.model if model_response is not None else None,
        response_id=model_response.response_id if model_response is not None else None,
        status="succeeded" if model_response is not None else "failed",
        latency_ms=outcome.latency_ms,
        usage=_json_object(model_response.usage)
        if model_response is not None
        else {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        error_detail=outcome.error_detail,
    )


class ProviderConfigurationChangedError(RuntimeError):
    """Raised when mutable provider write context changes across an I/O gap."""


@dataclass(frozen=True, slots=True)
class _StagedProviderSecret:
    secret_ref: str
    encoded_reference: str


def _stage_provider_secret(
    *,
    settings: Settings,
    workspace_id: UUID,
    credential: str,
) -> _StagedProviderSecret:
    normalized_credential = credential.strip()
    if normalized_credential == "":
        raise ValueError("provider credential is required")
    secret_ref = build_provider_secret_ref(workspace_id=workspace_id)
    write_provider_api_key(
        settings=settings,
        secret_ref=secret_ref,
        api_key=normalized_credential,
    )
    return _StagedProviderSecret(
        secret_ref=secret_ref,
        encoded_reference=encode_provider_secret_ref(secret_ref=secret_ref),
    )


def _retire_provider_secret_if_unreferenced(
    *,
    settings: Settings,
    workspace_id: UUID,
    user_account_id: UUID,
    encoded_reference: str,
) -> None:
    if not is_provider_secret_ref(encoded_reference):
        return

    # A commit acknowledgement can be lost after the database has durably
    # stored the staged reference. Treat any inability to prove non-reference
    # as "in use" so compensation can only leak an orphan, never delete a live
    # credential.
    try:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            # label state: none, provider tables carry no label and this connection has no vNext store.
            with conn.transaction():
                _assert_provider_write_context(
                    conn=conn,
                    expected_workspace_id=workspace_id,
                    expected_user_account_id=user_account_id,
                )
                in_use = ContinuityStore(conn).is_provider_secret_reference_in_use(
                    workspace_id=workspace_id,
                    encoded_reference=encoded_reference,
                )
    except Exception:
        LOGGER.warning(
            "provider secret retirement skipped because reference state was unavailable",
            exc_info=True,
        )
        return
    if in_use:
        return

    try:
        delete_provider_api_key(
            settings=settings,
            secret_ref=decode_provider_secret_ref(encoded_reference),
        )
    except ProviderSecretManagerError:
        LOGGER.warning("unreferenced provider secret could not be retired", exc_info=True)


def _discard_staged_provider_secret(
    *,
    settings: Settings,
    workspace_id: UUID,
    user_account_id: UUID,
    staged_secret: _StagedProviderSecret | None,
) -> None:
    if staged_secret is None:
        return
    _retire_provider_secret_if_unreferenced(
        settings=settings,
        workspace_id=workspace_id,
        user_account_id=user_account_id,
        encoded_reference=staged_secret.encoded_reference,
    )


def _resolve_owned_provider_workspace(
    *,
    settings: Settings,
    user_account_id: UUID,
) -> tuple[UUID, UUID]:
    return _require_local_provider_workspace(settings=settings, user_account_id=user_account_id)


def _assert_provider_write_context(
    *,
    conn: Any,
    expected_workspace_id: UUID,
    expected_user_account_id: UUID,
) -> None:
    context = get_local_workspace(conn, user_account_id=expected_user_account_id)
    if context is None or context["workspace"]["id"] != expected_workspace_id:
        raise ProviderConfigurationChangedError(
            "provider write context changed while credential storage was being prepared"
        )


def _create_workspace_provider_durable(
    *,
    settings: Settings,
    authenticated_user_id: UUID,
    provider_key: str,
    display_name: str,
    base_url: str,
    api_key: str,
    auth_mode: str,
    default_model: str,
    model_list_path: str,
    healthcheck_path: str,
    invoke_path: str,
    metadata: dict[str, object],
) -> tuple[ModelProviderRow, ProviderCapabilityRow]:
    workspace_id, user_account_id = _resolve_owned_provider_workspace(
        settings=settings,
        user_account_id=authenticated_user_id,
    )
    normalized_base_url = validate_provider_base_url(base_url)
    normalized_auth_mode = auth_mode.strip().lower()
    normalized_api_key = api_key.strip()
    staged_secret: _StagedProviderSecret | None = None
    if normalized_auth_mode == "bearer":
        staged_secret = _stage_provider_secret(
            settings=settings,
            workspace_id=workspace_id,
            credential=normalized_api_key,
        )
        api_key_field = staged_secret.encoded_reference
    elif normalized_auth_mode == "none":
        if normalized_api_key != "":
            raise ValueError("api_key must be empty when auth_mode is none")
        api_key_field = "auth_mode_none"
    else:
        raise ValueError(f"unsupported auth_mode: {auth_mode}")

    provider_persisted = False
    try:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            # label state: none, provider tables carry no label and this connection has no vNext store.
            with conn.transaction():
                _assert_provider_write_context(
                    conn=conn,
                    expected_workspace_id=workspace_id,
                    expected_user_account_id=user_account_id,
                )
                provider, capability = _register_workspace_provider(
                    store=ContinuityStore(conn),
                    workspace_id=workspace_id,
                    created_by_user_account_id=user_account_id,
                    provider_key=provider_key,
                    display_name=display_name,
                    base_url=normalized_base_url,
                    api_key_field=api_key_field,
                    auth_mode=auth_mode,
                    default_model=default_model,
                    model_list_path=model_list_path,
                    healthcheck_path=healthcheck_path,
                    invoke_path=invoke_path,
                    metadata=metadata,
                )
        provider_persisted = True
        return provider, capability
    finally:
        if not provider_persisted:
            _discard_staged_provider_secret(
                settings=settings,
                workspace_id=workspace_id,
                user_account_id=user_account_id,
                staged_secret=staged_secret,
            )


def _register_workspace_provider(
    *,
    store: ContinuityStore,
    workspace_id: UUID,
    created_by_user_account_id: UUID,
    provider_key: str,
    display_name: str,
    base_url: str,
    api_key_field: str,
    auth_mode: str,
    default_model: str,
    model_list_path: str,
    healthcheck_path: str,
    invoke_path: str,
    metadata: dict[str, object],
) -> tuple[ModelProviderRow, ProviderCapabilityRow]:
    normalized_display_name = display_name.strip()
    normalized_base_url = base_url.strip()
    normalized_api_key_field = api_key_field.strip()
    normalized_default_model = default_model.strip()
    normalized_auth_mode = auth_mode.strip().lower()
    normalized_model_list_path = _normalize_provider_path(
        field_name="model_list_path",
        value=model_list_path,
    )
    normalized_healthcheck_path = _normalize_provider_path(
        field_name="healthcheck_path",
        value=healthcheck_path,
    )
    normalized_invoke_path = _normalize_provider_path(
        field_name="invoke_path",
        value=invoke_path,
    )

    if normalized_display_name == "":
        raise ValueError("display_name is required")
    normalized_base_url = validate_provider_base_url(
        normalized_base_url,
        require_dns_resolution=False,
    )
    if normalized_default_model == "":
        raise ValueError("default_model is required")
    if normalized_auth_mode not in {"bearer", "none"}:
        raise ValueError(f"unsupported auth_mode: {auth_mode}")
    if normalized_auth_mode == "bearer" and not is_provider_secret_ref(normalized_api_key_field):
        raise ValueError("api_key must be a staged secret reference when auth_mode is bearer")
    if normalized_auth_mode == "none" and normalized_api_key_field != "auth_mode_none":
        raise ValueError("api_key must be empty when auth_mode is none")

    encoded_api_key = normalized_api_key_field

    normalized_metadata = _json_object(metadata)
    config_fingerprint = _provider_config_fingerprint(
        provider_key=provider_key,
        model_provider=OPENAI_RESPONSES_PROVIDER,
        display_name=normalized_display_name,
        base_url=normalized_base_url,
        api_key=encoded_api_key,
        auth_mode=normalized_auth_mode,
        default_model=normalized_default_model,
        status="active",
        model_list_path=normalized_model_list_path,
        healthcheck_path=normalized_healthcheck_path,
        invoke_path=normalized_invoke_path,
        azure_api_version="",
        azure_auth_secret_ref="",
        metadata=normalized_metadata,
    )
    provider = store.create_model_provider(
        workspace_id=workspace_id,
        created_by_user_account_id=created_by_user_account_id,
        provider_key=provider_key,
        model_provider=OPENAI_RESPONSES_PROVIDER,
        display_name=normalized_display_name,
        base_url=normalized_base_url,
        api_key=encoded_api_key,
        default_model=normalized_default_model,
        status="active",
        metadata=normalized_metadata,
        auth_mode=normalized_auth_mode,
        model_list_path=normalized_model_list_path,
        healthcheck_path=normalized_healthcheck_path,
        invoke_path=normalized_invoke_path,
        azure_api_version="",
        # Non-Azure providers intentionally store an empty Azure secret ref.
        azure_auth_secret_ref="",  # nosec B106
        config_fingerprint_sha256=config_fingerprint,
    )

    adapter = provider_adapter_registry.resolve(provider_key)
    capability = store.upsert_provider_capability_if_current(
        workspace_id=workspace_id,
        provider_id=provider["id"],
        discovered_by_user_account_id=created_by_user_account_id,
        adapter_key=adapter.adapter_key,
        discovery_status="failed",
        capability_snapshot=_json_object(
            _fallback_provider_capability_snapshot(
                adapter_key=adapter.adapter_key,
                runtime_provider=adapter.runtime_provider,
                model_list_path=normalized_model_list_path,
                healthcheck_path=normalized_healthcheck_path,
                invoke_path=normalized_invoke_path,
            )
        ),
        discovery_error="capability discovery pending",
        expected_config_revision=provider["config_revision"],
        expected_config_fingerprint_sha256=provider["config_fingerprint_sha256"],
    )
    if capability is None:  # pragma: no cover - same-transaction invariant
        raise ContinuityStoreInvariantError("new provider configuration changed before capability initialization")
    return provider, capability


def _normalize_azure_api_version(value: str) -> str:
    api_version = value.strip()
    if api_version == "":
        raise ValueError("api_version is required")
    return api_version


def _register_workspace_azure_provider(
    *,
    store: ContinuityStore,
    workspace_id: UUID,
    created_by_user_account_id: UUID,
    display_name: str,
    base_url: str,
    credential_secret_ref: str,
    auth_mode: str,
    default_model: str,
    model_list_path: str,
    healthcheck_path: str,
    invoke_path: str,
    api_version: str,
    metadata: dict[str, object],
) -> tuple[ModelProviderRow, ProviderCapabilityRow]:
    normalized_display_name = display_name.strip()
    normalized_base_url = base_url.strip()
    normalized_credential_secret_ref = credential_secret_ref.strip()
    normalized_default_model = default_model.strip()
    normalized_auth_mode = auth_mode.strip().lower()
    normalized_api_version = _normalize_azure_api_version(api_version)
    normalized_model_list_path = _normalize_provider_path(
        field_name="model_list_path",
        value=model_list_path,
    )
    normalized_healthcheck_path = _normalize_provider_path(
        field_name="healthcheck_path",
        value=healthcheck_path,
    )
    normalized_invoke_path = _normalize_provider_path(
        field_name="invoke_path",
        value=invoke_path,
    )

    if normalized_display_name == "":
        raise ValueError("display_name is required")
    normalized_base_url = validate_provider_base_url(
        normalized_base_url,
        require_dns_resolution=False,
    )
    if normalized_default_model == "":
        raise ValueError("default_model is required")
    if normalized_auth_mode not in {AZURE_AUTH_MODE_API_KEY, AZURE_AUTH_MODE_AD_TOKEN}:
        raise ValueError(f"unsupported auth_mode: {auth_mode}")
    if not is_provider_secret_ref(normalized_credential_secret_ref):
        raise ValueError("azure credential must be a staged secret reference")

    encoded_secret_ref = normalized_credential_secret_ref

    normalized_metadata = _json_object(metadata)
    config_fingerprint = _provider_config_fingerprint(
        provider_key=AZURE_ADAPTER_KEY,
        model_provider=OPENAI_RESPONSES_PROVIDER,
        display_name=normalized_display_name,
        base_url=normalized_base_url,
        api_key="auth_mode_azure_secret_ref",
        auth_mode=normalized_auth_mode,
        default_model=normalized_default_model,
        status="active",
        model_list_path=normalized_model_list_path,
        healthcheck_path=normalized_healthcheck_path,
        invoke_path=normalized_invoke_path,
        azure_api_version=normalized_api_version,
        azure_auth_secret_ref=encoded_secret_ref,
        metadata=normalized_metadata,
    )
    provider = store.create_model_provider(
        workspace_id=workspace_id,
        created_by_user_account_id=created_by_user_account_id,
        provider_key=AZURE_ADAPTER_KEY,
        model_provider=OPENAI_RESPONSES_PROVIDER,
        display_name=normalized_display_name,
        base_url=normalized_base_url,
        api_key="auth_mode_azure_secret_ref",
        default_model=normalized_default_model,
        status="active",
        metadata=normalized_metadata,
        auth_mode=normalized_auth_mode,
        model_list_path=normalized_model_list_path,
        healthcheck_path=normalized_healthcheck_path,
        invoke_path=normalized_invoke_path,
        azure_api_version=normalized_api_version,
        azure_auth_secret_ref=encoded_secret_ref,
        config_fingerprint_sha256=config_fingerprint,
    )

    adapter = provider_adapter_registry.resolve(AZURE_ADAPTER_KEY)
    capability = store.upsert_provider_capability_if_current(
        workspace_id=workspace_id,
        provider_id=provider["id"],
        discovered_by_user_account_id=created_by_user_account_id,
        adapter_key=adapter.adapter_key,
        discovery_status="failed",
        capability_snapshot=_json_object(
            _fallback_provider_capability_snapshot(
                adapter_key=adapter.adapter_key,
                runtime_provider=adapter.runtime_provider,
                model_list_path=normalized_model_list_path,
                healthcheck_path=normalized_healthcheck_path,
                invoke_path=normalized_invoke_path,
                extra_snapshot_fields={
                    "azure_api_version": normalized_api_version,
                    "azure_auth_mode": normalized_auth_mode,
                },
            )
        ),
        discovery_error="capability discovery pending",
        expected_config_revision=provider["config_revision"],
        expected_config_fingerprint_sha256=provider["config_fingerprint_sha256"],
    )
    if capability is None:  # pragma: no cover - same-transaction invariant
        raise ContinuityStoreInvariantError("new Azure provider configuration changed before capability initialization")
    return provider, capability


def _create_workspace_azure_provider_durable(
    *,
    settings: Settings,
    authenticated_user_id: UUID,
    display_name: str,
    base_url: str,
    credential: str,
    auth_mode: str,
    default_model: str,
    model_list_path: str,
    healthcheck_path: str,
    invoke_path: str,
    api_version: str,
    metadata: dict[str, object],
) -> tuple[ModelProviderRow, ProviderCapabilityRow]:
    workspace_id, user_account_id = _resolve_owned_provider_workspace(
        settings=settings,
        user_account_id=authenticated_user_id,
    )
    normalized_base_url = validate_provider_base_url(base_url)
    staged_secret = _stage_provider_secret(
        settings=settings,
        workspace_id=workspace_id,
        credential=credential,
    )
    provider_persisted = False
    try:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            # label state: none, provider tables carry no label and this connection has no vNext store.
            with conn.transaction():
                _assert_provider_write_context(
                    conn=conn,
                    expected_workspace_id=workspace_id,
                    expected_user_account_id=user_account_id,
                )
                provider, capability = _register_workspace_azure_provider(
                    store=ContinuityStore(conn),
                    workspace_id=workspace_id,
                    created_by_user_account_id=user_account_id,
                    display_name=display_name,
                    base_url=normalized_base_url,
                    credential_secret_ref=staged_secret.encoded_reference,
                    auth_mode=auth_mode,
                    default_model=default_model,
                    model_list_path=model_list_path,
                    healthcheck_path=healthcheck_path,
                    invoke_path=invoke_path,
                    api_version=api_version,
                    metadata=metadata,
                )
        provider_persisted = True
        return provider, capability
    finally:
        if not provider_persisted:
            _discard_staged_provider_secret(
                settings=settings,
                workspace_id=workspace_id,
                user_account_id=user_account_id,
                staged_secret=staged_secret,
            )


def _update_workspace_provider(
    *,
    store: ContinuityStore,
    existing_provider: ModelProviderRow,
    updated_by_user_account_id: UUID,
    display_name: str | None,
    base_url: str | None,
    api_key: str | None,
    ad_token: str | None,
    credential_secret_ref: str | None,
    auth_mode: str | None,
    default_model: str | None,
    model_list_path: str | None,
    healthcheck_path: str | None,
    invoke_path: str | None,
    api_version: str | None,
    metadata: dict[str, object] | None,
) -> tuple[ModelProviderRow, ProviderCapabilityRow]:
    provider_key = existing_provider["provider_key"]
    normalized_display_name = existing_provider["display_name"] if display_name is None else display_name.strip()
    normalized_base_url = existing_provider["base_url"] if base_url is None else base_url.strip()
    normalized_default_model = existing_provider["default_model"] if default_model is None else default_model.strip()
    normalized_model_list_path = (
        existing_provider["model_list_path"]
        if model_list_path is None
        else _normalize_provider_path(field_name="model_list_path", value=model_list_path)
    )
    normalized_healthcheck_path = (
        existing_provider["healthcheck_path"]
        if healthcheck_path is None
        else _normalize_provider_path(field_name="healthcheck_path", value=healthcheck_path)
    )
    normalized_invoke_path = (
        existing_provider["invoke_path"]
        if invoke_path is None
        else _normalize_provider_path(field_name="invoke_path", value=invoke_path)
    )
    normalized_metadata: JsonObject = existing_provider["metadata"] if metadata is None else _json_object(metadata)

    if normalized_display_name == "":
        raise ValueError("display_name is required")
    normalized_base_url = validate_provider_base_url(
        normalized_base_url,
        require_dns_resolution=False,
    )
    if normalized_default_model == "":
        raise ValueError("default_model is required")

    encoded_api_key = existing_provider["api_key"]
    normalized_auth_mode = existing_provider["auth_mode"] if auth_mode is None else auth_mode.strip().lower()
    normalized_api_version = existing_provider["azure_api_version"]
    normalized_azure_secret_ref = existing_provider["azure_auth_secret_ref"]

    if provider_key == AZURE_ADAPTER_KEY:
        if normalized_auth_mode not in {AZURE_AUTH_MODE_API_KEY, AZURE_AUTH_MODE_AD_TOKEN}:
            raise ValueError(f"unsupported auth_mode: {normalized_auth_mode}")
        credential_update = api_key if normalized_auth_mode == AZURE_AUTH_MODE_API_KEY else ad_token
        if normalized_auth_mode != existing_provider["auth_mode"] and (
            credential_update is None or credential_update.strip() == "" or credential_secret_ref is None
        ):
            credential_field = "api_key" if normalized_auth_mode == AZURE_AUTH_MODE_API_KEY else "ad_token"
            raise ValueError(f"{credential_field} is required when changing Azure auth_mode")
        if api_version is not None:
            normalized_api_version = _normalize_azure_api_version(api_version)
        if credential_update is not None and credential_update.strip() != "":
            if credential_secret_ref is None or not is_provider_secret_ref(credential_secret_ref):
                raise ValueError("azure credential must be staged before provider update")
            encoded_api_key = "auth_mode_azure_secret_ref"
            normalized_azure_secret_ref = credential_secret_ref
    else:
        if normalized_auth_mode not in {"bearer", "none"}:
            raise ValueError(f"unsupported auth_mode: {normalized_auth_mode}")
        if normalized_auth_mode == "none":
            if api_key is not None and api_key.strip() != "":
                raise ValueError("api_key must be empty when auth_mode is none")
            encoded_api_key = "auth_mode_none"
        else:
            if api_key is not None:
                if api_key.strip() == "":
                    raise ValueError("api_key is required when auth_mode is bearer")
                if credential_secret_ref is None or not is_provider_secret_ref(credential_secret_ref):
                    raise ValueError("api_key must be staged before provider update")
                encoded_api_key = credential_secret_ref
            elif existing_provider["auth_mode"] != "bearer":
                raise ValueError("api_key is required when auth_mode is bearer")
        normalized_api_version = ""
        normalized_azure_secret_ref = ""

    config_fingerprint = _provider_config_fingerprint(
        provider_key=provider_key,
        model_provider=existing_provider["model_provider"],
        display_name=normalized_display_name,
        base_url=normalized_base_url,
        api_key=encoded_api_key,
        auth_mode=normalized_auth_mode,
        default_model=normalized_default_model,
        status=existing_provider["status"],
        model_list_path=normalized_model_list_path,
        healthcheck_path=normalized_healthcheck_path,
        invoke_path=normalized_invoke_path,
        azure_api_version=normalized_api_version,
        azure_auth_secret_ref=normalized_azure_secret_ref,
        metadata=normalized_metadata,
    )
    provider = store.update_model_provider(
        provider_id=existing_provider["id"],
        workspace_id=existing_provider["workspace_id"],
        provider_key=provider_key,
        model_provider=existing_provider["model_provider"],
        display_name=normalized_display_name,
        base_url=normalized_base_url,
        api_key=encoded_api_key,
        auth_mode=normalized_auth_mode,
        default_model=normalized_default_model,
        status=existing_provider["status"],
        model_list_path=normalized_model_list_path,
        healthcheck_path=normalized_healthcheck_path,
        invoke_path=normalized_invoke_path,
        azure_api_version=normalized_api_version,
        azure_auth_secret_ref=normalized_azure_secret_ref,
        metadata=normalized_metadata,
        config_fingerprint_sha256=config_fingerprint,
        expected_config_revision=existing_provider["config_revision"],
        expected_config_fingerprint_sha256=existing_provider["config_fingerprint_sha256"],
    )
    if provider is None:
        raise ProviderConfigurationChangedError("provider configuration changed while the update was being committed")

    adapter = provider_adapter_registry.resolve(provider_key)
    extra_snapshot_fields = None
    if provider_key == AZURE_ADAPTER_KEY:
        extra_snapshot_fields = {
            "azure_api_version": normalized_api_version,
            "azure_auth_mode": normalized_auth_mode,
        }
    capability = store.upsert_provider_capability_if_current(
        workspace_id=existing_provider["workspace_id"],
        provider_id=provider["id"],
        discovered_by_user_account_id=updated_by_user_account_id,
        adapter_key=adapter.adapter_key,
        discovery_status="failed",
        capability_snapshot=_json_object(
            _fallback_provider_capability_snapshot(
                adapter_key=adapter.adapter_key,
                runtime_provider=adapter.runtime_provider,
                model_list_path=normalized_model_list_path,
                healthcheck_path=normalized_healthcheck_path,
                invoke_path=normalized_invoke_path,
                extra_snapshot_fields=extra_snapshot_fields,
            )
        ),
        discovery_error="capability discovery pending",
        expected_config_revision=provider["config_revision"],
        expected_config_fingerprint_sha256=provider["config_fingerprint_sha256"],
    )
    if capability is None:  # pragma: no cover - same-transaction invariant
        raise ContinuityStoreInvariantError("updated provider configuration changed before capability initialization")
    return provider, capability


def _update_workspace_provider_durable(
    *,
    settings: Settings,
    authenticated_user_id: UUID,
    provider_id: UUID,
    display_name: str | None,
    base_url: str | None,
    api_key: str | None,
    ad_token: str | None,
    auth_mode: str | None,
    default_model: str | None,
    model_list_path: str | None,
    healthcheck_path: str | None,
    invoke_path: str | None,
    api_version: str | None,
    metadata: dict[str, object] | None,
) -> tuple[ModelProviderRow, ProviderCapabilityRow]:
    workspace_id, user_account_id = _resolve_owned_provider_workspace(
        settings=settings,
        user_account_id=authenticated_user_id,
    )
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        # label state: none, provider tables carry no label and this connection has no vNext store.
        with conn.transaction():
            _assert_provider_write_context(
                conn=conn,
                expected_workspace_id=workspace_id,
                expected_user_account_id=user_account_id,
            )
            existing_provider = ContinuityStore(conn).get_model_provider_for_workspace_optional(
                provider_id=provider_id,
                workspace_id=workspace_id,
            )
            if existing_provider is None:
                raise LookupError(f"provider {provider_id} was not found")

    validated_base_url = validate_provider_base_url(existing_provider["base_url"] if base_url is None else base_url)

    final_auth_mode = existing_provider["auth_mode"] if auth_mode is None else auth_mode.strip().lower()
    credential: str | None = None
    if existing_provider["provider_key"] == AZURE_ADAPTER_KEY:
        if final_auth_mode == AZURE_AUTH_MODE_API_KEY:
            if ad_token is not None and ad_token.strip() != "":
                raise ValueError("ad_token must be empty when auth_mode is azure_api_key")
            credential = api_key
        elif final_auth_mode == AZURE_AUTH_MODE_AD_TOKEN:
            if api_key is not None and api_key.strip() != "":
                raise ValueError("api_key must be empty when auth_mode is azure_ad_token")
            credential = ad_token
        else:
            raise ValueError(f"unsupported auth_mode: {final_auth_mode}")
        if final_auth_mode != existing_provider["auth_mode"] and (credential is None or credential.strip() == ""):
            credential_field = "api_key" if final_auth_mode == AZURE_AUTH_MODE_API_KEY else "ad_token"
            raise ValueError(f"{credential_field} is required when changing Azure auth_mode")
    else:
        if ad_token is not None and ad_token.strip() != "":
            raise ValueError("ad_token is only supported by Azure providers")
        if final_auth_mode not in {"bearer", "none"}:
            raise ValueError(f"unsupported auth_mode: {final_auth_mode}")
        if final_auth_mode == "none" and api_key is not None and api_key.strip() != "":
            raise ValueError("api_key must be empty when auth_mode is none")
        if final_auth_mode == "bearer":
            credential = api_key

    staged_secret: _StagedProviderSecret | None = None
    if credential is not None:
        if credential.strip() == "":
            if existing_provider["provider_key"] != AZURE_ADAPTER_KEY:
                raise ValueError("api_key is required when auth_mode is bearer")
        else:
            staged_secret = _stage_provider_secret(
                settings=settings,
                workspace_id=workspace_id,
                credential=credential,
            )

    old_secret_reference = (
        existing_provider["azure_auth_secret_ref"]
        if existing_provider["provider_key"] == AZURE_ADAPTER_KEY
        else existing_provider["api_key"]
    )
    provider_persisted = False
    try:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            with conn.transaction():
                _assert_provider_write_context(
                    conn=conn,
                    expected_workspace_id=workspace_id,
                    expected_user_account_id=user_account_id,
                )
                store = ContinuityStore(conn)
                current_provider = store.get_model_provider_for_workspace_optional(
                    provider_id=provider_id,
                    workspace_id=workspace_id,
                )
                if current_provider is None:
                    raise LookupError(f"provider {provider_id} was not found")
                if (
                    current_provider["config_revision"] != existing_provider["config_revision"]
                    or current_provider["config_fingerprint_sha256"] != existing_provider["config_fingerprint_sha256"]
                ):
                    raise ProviderConfigurationChangedError(
                        "provider configuration changed while credential storage was being prepared"
                    )
                provider, capability = _update_workspace_provider(
                    store=store,
                    existing_provider=current_provider,
                    updated_by_user_account_id=user_account_id,
                    display_name=display_name,
                    base_url=validated_base_url,
                    api_key=api_key,
                    ad_token=ad_token,
                    credential_secret_ref=(None if staged_secret is None else staged_secret.encoded_reference),
                    auth_mode=auth_mode,
                    default_model=default_model,
                    model_list_path=model_list_path,
                    healthcheck_path=healthcheck_path,
                    invoke_path=invoke_path,
                    api_version=api_version,
                    metadata=metadata,
                )
        provider_persisted = True
        new_secret_reference = (
            provider["azure_auth_secret_ref"] if provider["provider_key"] == AZURE_ADAPTER_KEY else provider["api_key"]
        )
        if old_secret_reference != new_secret_reference:
            _retire_provider_secret_if_unreferenced(
                settings=settings,
                workspace_id=workspace_id,
                user_account_id=user_account_id,
                encoded_reference=old_secret_reference,
            )
        return provider, capability
    finally:
        if not provider_persisted:
            _discard_staged_provider_secret(
                settings=settings,
                workspace_id=workspace_id,
                user_account_id=user_account_id,
                staged_secret=staged_secret,
            )


def _seed_workspace_provider_configs(
    *,
    settings: Settings,
    user_account_id: UUID,
    workspace_id: UUID,
) -> list[ModelProviderRow]:
    if len(settings.workspace_provider_configs) == 0:
        return []
    resolved_workspace_id, resolved_user_account_id = _resolve_owned_provider_workspace(
        settings=settings,
        user_account_id=user_account_id,
    )
    if resolved_workspace_id != workspace_id:
        raise ProviderConfigurationChangedError("workspace selection changed before provider bootstrap")
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        # label state: none, provider tables carry no label and this connection has no vNext store.
        with conn.transaction():
            _assert_provider_write_context(
                conn=conn,
                expected_workspace_id=workspace_id,
                expected_user_account_id=resolved_user_account_id,
            )
            existing_provider_keys = {
                (provider["provider_key"], provider["display_name"])
                for provider in ContinuityStore(conn).list_model_providers_for_workspace(workspace_id=workspace_id)
            }

    seeded_providers: list[ModelProviderRow] = []
    for provider_config in settings.workspace_provider_configs:
        provider_identity = (provider_config.provider_key, provider_config.display_name)
        if provider_identity in existing_provider_keys:
            continue
        provider, _capability = _create_workspace_provider_durable(
            settings=settings,
            authenticated_user_id=resolved_user_account_id,
            provider_key=provider_config.provider_key,
            display_name=provider_config.display_name,
            base_url=provider_config.base_url,
            api_key=provider_config.api_key,
            auth_mode=provider_config.auth_mode,
            default_model=provider_config.default_model,
            model_list_path=provider_config.model_list_path,
            healthcheck_path=provider_config.healthcheck_path,
            invoke_path=provider_config.invoke_path,
            metadata={} if provider_config.metadata is None else dict(provider_config.metadata),
        )
        seeded_providers.append(provider)
        existing_provider_keys.add(provider_identity)
    return seeded_providers


def redact_url_credentials(raw_url: str) -> str:
    parsed = urlsplit(raw_url)

    if parsed.hostname is None or (parsed.username is None and parsed.password is None):
        return raw_url

    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _response_job_headers(
    job: ResponseGenerationJobRow,
    *,
    replayed: bool,
) -> dict[str, str]:
    headers = {"Response-Job-Id": str(job["id"])}
    if replayed:
        headers["Idempotency-Replayed"] = "true"
    return headers


def _response_job_public_status(job: ResponseGenerationJobRow) -> JsonObject:
    return {
        "id": str(job["id"]),
        "state": job["state"],
        "endpoint": job["endpoint"],
        "created_at": job["created_at"].isoformat(),
        "updated_at": job["updated_at"].isoformat(),
        "completed_at": None if job["completed_at"] is None else job["completed_at"].isoformat(),
    }


def _terminal_response_job_replay(job: ResponseGenerationJobRow) -> JSONResponse:
    payload = job["response_payload"] if job["state"] == "succeeded" else job["error_payload"]
    status_code = job["response_status_code"]
    if payload is None or status_code is None:
        raise RuntimeError("terminal response job is missing its persisted outcome")
    return JSONResponse(
        status_code=status_code,
        headers=_response_job_headers(job, replayed=True),
        content=jsonable_encoder(payload),
    )


def _response_job_replay_or_in_progress(
    *,
    store: ResponseGenerationJobStore,
    job: ResponseGenerationJobRow,
    expected_request_fingerprint: str,
) -> JSONResponse | None:
    if job["request_fingerprint_sha256"] != expected_request_fingerprint:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "idempotency_key_reused",
                    "message": "Idempotency-Key was already used for a different request",
                }
            },
        )
    if job["state"] in {"succeeded", "failed"}:
        return _terminal_response_job_replay(job)
    if job["state"] == "pending":
        return None
    if job["state"] != "running":
        raise RuntimeError(f"unsupported response job state: {job['state']}")

    abandoned_payload: JsonObject = {
        "detail": {
            "code": "provider_outcome_unknown",
            "message": (
                "the original provider call did not finalize before its lease expired; "
                "AliceBot will not invoke it again under the same Idempotency-Key"
            ),
        },
        "response_job": {**_response_job_public_status(job), "state": "failed"},
    }
    abandoned = store.fail_if_abandoned(
        job_id=job["id"],
        error_payload=abandoned_payload,
    )
    if abandoned is not None:
        return _terminal_response_job_replay(abandoned)
    return JSONResponse(
        status_code=202,
        headers={
            **_response_job_headers(job, replayed=True),
            "Retry-After": "2",
        },
        content=jsonable_encoder(
            {
                "detail": {
                    "code": "response_generation_in_progress",
                    "message": "response generation is already in progress for this Idempotency-Key",
                },
                "response_job": _response_job_public_status(job),
            }
        ),
    )


class RegisterProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_key: Literal["openai_compatible"] = "openai_compatible"
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=8000)
    auth_mode: Literal["bearer"] = "bearer"
    default_model: str = Field(min_length=1, max_length=200)
    model_list_path: str = Field(default="/models", min_length=1, max_length=200)
    healthcheck_path: str = Field(default="/models", min_length=1, max_length=200)
    invoke_path: str = Field(default="/responses", min_length=1, max_length=200)
    metadata: BoundedJsonObject = Field(default_factory=dict)


class RegisterOllamaProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(default="http://127.0.0.1:11434", min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=8000)
    auth_mode: Literal["bearer", "none"] = "none"
    default_model: str = Field(min_length=1, max_length=200)
    model_list_path: str = Field(default="/api/tags", min_length=1, max_length=200)
    healthcheck_path: str = Field(default="/api/version", min_length=1, max_length=200)
    invoke_path: str = Field(default="/api/chat", min_length=1, max_length=200)
    metadata: BoundedJsonObject = Field(default_factory=dict)


class RegisterLlamaCppProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(default="http://127.0.0.1:8080", min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=8000)
    auth_mode: Literal["bearer", "none"] = "none"
    default_model: str = Field(min_length=1, max_length=200)
    model_list_path: str = Field(default="/v1/models", min_length=1, max_length=200)
    healthcheck_path: str = Field(default="/health", min_length=1, max_length=200)
    invoke_path: str = Field(default="/v1/chat/completions", min_length=1, max_length=200)
    metadata: BoundedJsonObject = Field(default_factory=dict)


class RegisterVllmProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(default="http://127.0.0.1:8001", min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=8000)
    auth_mode: Literal["bearer", "none"] = "none"
    default_model: str = Field(min_length=1, max_length=200)
    model_list_path: str = Field(default="/v1/models", min_length=1, max_length=200)
    healthcheck_path: str = Field(default="/health", min_length=1, max_length=200)
    invoke_path: str = Field(default="/v1/chat/completions", min_length=1, max_length=200)
    metadata: BoundedJsonObject = Field(default_factory=dict)


class RegisterAzureProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    auth_mode: Literal["azure_api_key", "azure_ad_token"] = "azure_api_key"
    api_key: str | None = Field(default=None, max_length=8000)
    ad_token: str | None = Field(default=None, max_length=16000)
    api_version: str = Field(default=DEFAULT_AZURE_API_VERSION, min_length=1, max_length=40)
    default_model: str = Field(min_length=1, max_length=200)
    model_list_path: str = Field(default="/openai/models", min_length=1, max_length=200)
    healthcheck_path: str = Field(default="/openai/models", min_length=1, max_length=200)
    invoke_path: str = Field(default="/openai/responses", min_length=1, max_length=200)
    metadata: BoundedJsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_auth_payload(self) -> "RegisterAzureProviderRequest":
        api_key = None if self.api_key is None else self.api_key.strip()
        ad_token = None if self.ad_token is None else self.ad_token.strip()

        if self.auth_mode == AZURE_AUTH_MODE_API_KEY:
            if api_key in (None, ""):
                raise ValueError("api_key is required when auth_mode is azure_api_key")
            if ad_token not in (None, ""):
                raise ValueError("ad_token must be empty when auth_mode is azure_api_key")
            return self

        if ad_token in (None, ""):
            raise ValueError("ad_token is required when auth_mode is azure_ad_token")
        if api_key not in (None, ""):
            raise ValueError("api_key must be empty when auth_mode is azure_ad_token")
        return self


class TestProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: UUID
    model: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str = Field(
        default="Reply with a concise provider connectivity confirmation.",
        min_length=1,
        max_length=1000,
    )


class UpdateProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    auth_mode: str | None = Field(default=None, min_length=1, max_length=40)
    api_key: str | None = Field(default=None, max_length=8000)
    ad_token: str | None = Field(default=None, max_length=16000)
    api_version: str | None = Field(default=None, min_length=1, max_length=40)
    default_model: str | None = Field(default=None, min_length=1, max_length=200)
    model_list_path: str | None = Field(default=None, min_length=1, max_length=200)
    healthcheck_path: str | None = Field(default=None, min_length=1, max_length=200)
    invoke_path: str | None = Field(default=None, min_length=1, max_length=200)
    metadata: BoundedJsonObject | None = None


class RuntimeInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: UUID
    thread_id: UUID
    message: str = Field(min_length=1, max_length=4000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_sessions: int = Field(default=DEFAULT_MAX_SESSIONS, ge=1, le=50)
    max_events: int = Field(default=DEFAULT_MAX_EVENTS, ge=1, le=200)
    max_memories: int = Field(default=DEFAULT_MAX_MEMORIES, ge=1, le=200)
    max_entities: int = Field(default=DEFAULT_MAX_ENTITIES, ge=1, le=200)
    max_entity_edges: int = Field(default=DEFAULT_MAX_ENTITY_EDGES, ge=1, le=400)


def _require_local_provider_workspace(
    *,
    settings: Settings,
    user_account_id: UUID,
) -> tuple[UUID, UUID]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        # label state: none, provider tables carry no label and this connection has no vNext store.
        with conn.transaction():
            context = get_local_workspace(conn, user_account_id=user_account_id)
    if context is None:
        raise LookupError("local workspace is not bootstrapped; POST /v1/workspaces/bootstrap first")
    return context["workspace"]["id"], user_account_id


@router.post("/v1/providers")
def register_v1_provider(request: Request, body: RegisterProviderRequest) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        provider, capability = _create_workspace_provider_durable(
            settings=settings,
            authenticated_user_id=user_account_id,
            provider_key=body.provider_key,
            display_name=body.display_name,
            base_url=body.base_url,
            api_key=body.api_key,
            auth_mode=body.auth_mode,
            default_model=body.default_model,
            model_list_path=body.model_list_path,
            healthcheck_path=body.healthcheck_path,
            invoke_path=body.invoke_path,
            metadata=body.metadata,
        )
        discovery = _discover_provider_capability(provider=provider, settings=settings)
        refreshed_capability = _persist_discovered_provider_capability(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=provider["workspace_id"],
            provider=provider,
            outcome=discovery,
        )
        if refreshed_capability is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "provider configuration changed during capability discovery"},
            )
        capability = refreshed_capability
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderConfigurationChangedError as exc:
        return public_exception_response(exc, status_code=409)
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            status_code=409,
            content={"detail": "provider display_name must be unique within the workspace"},
        )
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
            }
        ),
    )


@router.post("/v1/providers/ollama/register")
def register_v1_ollama_provider(
    request: Request,
    body: RegisterOllamaProviderRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        provider, capability = _create_workspace_provider_durable(
            settings=settings,
            authenticated_user_id=user_account_id,
            provider_key=OLLAMA_ADAPTER_KEY,
            display_name=body.display_name,
            base_url=body.base_url,
            api_key=body.api_key or "",
            auth_mode=body.auth_mode,
            default_model=body.default_model,
            model_list_path=body.model_list_path,
            healthcheck_path=body.healthcheck_path,
            invoke_path=body.invoke_path,
            metadata=body.metadata,
        )
        discovery = _discover_provider_capability(provider=provider, settings=settings)
        refreshed_capability = _persist_discovered_provider_capability(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=provider["workspace_id"],
            provider=provider,
            outcome=discovery,
        )
        if refreshed_capability is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "provider configuration changed during capability discovery"},
            )
        capability = refreshed_capability
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderConfigurationChangedError as exc:
        return public_exception_response(exc, status_code=409)
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            status_code=409,
            content={"detail": "provider display_name must be unique within the workspace"},
        )
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
            }
        ),
    )


@router.post("/v1/providers/llamacpp/register")
def register_v1_llamacpp_provider(
    request: Request,
    body: RegisterLlamaCppProviderRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        provider, capability = _create_workspace_provider_durable(
            settings=settings,
            authenticated_user_id=user_account_id,
            provider_key=LLAMACPP_ADAPTER_KEY,
            display_name=body.display_name,
            base_url=body.base_url,
            api_key=body.api_key or "",
            auth_mode=body.auth_mode,
            default_model=body.default_model,
            model_list_path=body.model_list_path,
            healthcheck_path=body.healthcheck_path,
            invoke_path=body.invoke_path,
            metadata=body.metadata,
        )
        discovery = _discover_provider_capability(provider=provider, settings=settings)
        refreshed_capability = _persist_discovered_provider_capability(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=provider["workspace_id"],
            provider=provider,
            outcome=discovery,
        )
        if refreshed_capability is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "provider configuration changed during capability discovery"},
            )
        capability = refreshed_capability
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderConfigurationChangedError as exc:
        return public_exception_response(exc, status_code=409)
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            status_code=409,
            content={"detail": "provider display_name must be unique within the workspace"},
        )
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
            }
        ),
    )


@router.post("/v1/providers/vllm/register")
def register_v1_vllm_provider(
    request: Request,
    body: RegisterVllmProviderRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        provider, capability = _create_workspace_provider_durable(
            settings=settings,
            authenticated_user_id=user_account_id,
            provider_key=VLLM_ADAPTER_KEY,
            display_name=body.display_name,
            base_url=body.base_url,
            api_key=body.api_key or "",
            auth_mode=body.auth_mode,
            default_model=body.default_model,
            model_list_path=body.model_list_path,
            healthcheck_path=body.healthcheck_path,
            invoke_path=body.invoke_path,
            metadata=body.metadata,
        )
        discovery = _discover_provider_capability(provider=provider, settings=settings)
        refreshed_capability = _persist_discovered_provider_capability(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=provider["workspace_id"],
            provider=provider,
            outcome=discovery,
        )
        if refreshed_capability is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "provider configuration changed during capability discovery"},
            )
        capability = refreshed_capability
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderConfigurationChangedError as exc:
        return public_exception_response(exc, status_code=409)
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            status_code=409,
            content={"detail": "provider display_name must be unique within the workspace"},
        )
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
            }
        ),
    )


@router.post("/v1/providers/azure/register")
def register_v1_azure_provider(
    request: Request,
    body: RegisterAzureProviderRequest,
) -> JSONResponse:
    settings = get_settings()

    if body.auth_mode == AZURE_AUTH_MODE_API_KEY:
        credential = body.api_key
    else:
        credential = body.ad_token
    if credential is None:
        return JSONResponse(status_code=400, content={"detail": "azure credential is required"})

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        provider, capability = _create_workspace_azure_provider_durable(
            settings=settings,
            authenticated_user_id=user_account_id,
            display_name=body.display_name,
            base_url=body.base_url,
            credential=credential,
            auth_mode=body.auth_mode,
            default_model=body.default_model,
            model_list_path=body.model_list_path,
            healthcheck_path=body.healthcheck_path,
            invoke_path=body.invoke_path,
            api_version=body.api_version,
            metadata=body.metadata,
        )
        discovery = _discover_provider_capability(provider=provider, settings=settings)
        refreshed_capability = _persist_discovered_provider_capability(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=provider["workspace_id"],
            provider=provider,
            outcome=discovery,
        )
        if refreshed_capability is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "provider configuration changed during capability discovery"},
            )
        capability = refreshed_capability
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderConfigurationChangedError as exc:
        return public_exception_response(exc, status_code=409)
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            status_code=409,
            content={"detail": "provider display_name must be unique within the workspace"},
        )
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
            }
        ),
    )


@router.get("/v1/providers")
def list_v1_providers(request: Request) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        workspace_id, _ = _require_local_provider_workspace(
            settings=settings,
            user_account_id=user_account_id,
        )
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            # label state: none, provider tables carry no label and this connection has no vNext store.
            with conn.transaction():
                context = get_local_workspace(conn, user_account_id=user_account_id)
                if context is None or context["workspace"]["id"] != workspace_id:
                    raise LookupError("local workspace is not bootstrapped")
                store = ContinuityStore(conn)
                providers = store.list_model_providers_for_workspace(workspace_id=workspace_id)
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    items = [_serialize_model_provider(provider) for provider in providers]
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "items": items,
                "summary": {
                    "total_count": len(items),
                    "order": list(PROVIDER_LIST_ORDER),
                },
            }
        ),
    )


@router.get("/v1/providers/{provider_id}")
def get_v1_provider(provider_id: UUID, request: Request) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        workspace_id, _ = _require_local_provider_workspace(
            settings=settings,
            user_account_id=user_account_id,
        )
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            # label state: none, provider tables carry no label and this connection has no vNext store.
            with conn.transaction():
                context = get_local_workspace(conn, user_account_id=user_account_id)
                if context is None or context["workspace"]["id"] != workspace_id:
                    raise LookupError("local workspace is not bootstrapped")
                store = ContinuityStore(conn)
                provider = store.get_model_provider_for_workspace_optional(
                    provider_id=provider_id,
                    workspace_id=workspace_id,
                )
                if provider is None:
                    return public_exception_response(
                        LookupError(f"provider {provider_id} was not found"), status_code=404
                    )
                capability = store.get_provider_capability_for_provider_optional(
                    provider_id=provider_id,
                    workspace_id=workspace_id,
                )
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": None if capability is None else _serialize_provider_capability(capability),
            }
        ),
    )


@router.patch("/v1/providers/{provider_id}")
def update_v1_provider(
    provider_id: UUID,
    request: Request,
    body: UpdateProviderRequest,
) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        provider, capability = _update_workspace_provider_durable(
            settings=settings,
            authenticated_user_id=user_account_id,
            provider_id=provider_id,
            display_name=body.display_name,
            base_url=body.base_url,
            api_key=body.api_key,
            ad_token=body.ad_token,
            auth_mode=body.auth_mode,
            default_model=body.default_model,
            model_list_path=body.model_list_path,
            healthcheck_path=body.healthcheck_path,
            invoke_path=body.invoke_path,
            api_version=body.api_version,
            metadata=body.metadata,
        )
        discovery = _discover_provider_capability(provider=provider, settings=settings)
        refreshed_capability = _persist_discovered_provider_capability(
            settings=settings,
            user_account_id=user_account_id,
            workspace_id=provider["workspace_id"],
            provider=provider,
            outcome=discovery,
        )
        if refreshed_capability is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "provider configuration changed during capability discovery"},
            )
        capability = refreshed_capability
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderConfigurationChangedError as exc:
        return public_exception_response(exc, status_code=409)
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            status_code=409,
            content={"detail": "provider display_name must be unique within the workspace"},
        )
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
            }
        ),
    )


@router.post("/v1/providers/test")
def test_v1_provider(request: Request, body: TestProviderRequest) -> JSONResponse:
    settings = get_settings()

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        workspace_id, _ = _require_local_provider_workspace(
            settings=settings,
            user_account_id=user_account_id,
        )
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            # label state: none, provider tables carry no label and this connection has no vNext store.
            with conn.transaction():
                context = get_local_workspace(conn, user_account_id=user_account_id)
                if context is None or context["workspace"]["id"] != workspace_id:
                    raise LookupError("local workspace is not bootstrapped")
                provider = ContinuityStore(conn).get_model_provider_for_workspace_optional(
                    provider_id=body.provider_id,
                    workspace_id=workspace_id,
                )
                if provider is None:
                    return public_exception_response(
                        LookupError(f"provider {body.provider_id} was not found"), status_code=404
                    )

        runtime_provider = resolve_runtime_provider_config_secrets(
            config=RuntimeProviderConfig.from_row(_object_dict(provider)),
            settings=settings,
        )
        adapter = provider_adapter_registry.resolve(runtime_provider.provider_key)
        model_name = (body.model or runtime_provider.default_model).strip()
        if model_name == "":
            raise ValueError("model is required")

        discovery = _discover_provider_capability(provider=provider, settings=settings)
        invocation_outcome: _RuntimeProviderInvocationOutcome | None = None
        model_response: ModelInvocationResponse | None = None
        if discovery.discovery_status == "ready":
            model_request = build_provider_test_model_request(
                runtime_provider=runtime_provider.model_provider,
                model=model_name,
                prompt_text=body.prompt.strip(),
            )
            invocation_outcome = _attempt_runtime_provider_model(
                adapter=adapter,
                runtime_provider=runtime_provider,
                settings=settings,
                model_request=model_request,
            )
            model_response = invocation_outcome.response
            if invocation_outcome.error is not None:
                discovery = _ProviderDiscoveryOutcome(
                    adapter_key=discovery.adapter_key,
                    discovery_status="failed",
                    capability_snapshot=discovery.capability_snapshot,
                    discovery_error=invocation_outcome.error_detail,
                )

        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            with conn.transaction():
                persisted_context = get_local_workspace(conn, user_account_id=user_account_id)
                if persisted_context is None or persisted_context["workspace"]["id"] != workspace_id:
                    raise LookupError("local workspace is not bootstrapped")
                store = ContinuityStore(conn)
                capability = store.upsert_provider_capability_if_current(
                    workspace_id=workspace_id,
                    provider_id=provider["id"],
                    discovered_by_user_account_id=user_account_id,
                    adapter_key=discovery.adapter_key,
                    discovery_status=discovery.discovery_status,
                    capability_snapshot=discovery.capability_snapshot,
                    discovery_error=discovery.discovery_error,
                    expected_config_revision=provider["config_revision"],
                    expected_config_fingerprint_sha256=provider["config_fingerprint_sha256"],
                )
                if capability is None:
                    return JSONResponse(
                        status_code=409,
                        content={"detail": "provider configuration changed during provider test"},
                    )
                if invocation_outcome is not None:
                    _record_runtime_provider_invocation(
                        store=store,
                        workspace_id=workspace_id,
                        invoked_by_user_account_id=user_account_id,
                        thread_id=None,
                        invocation_kind="provider_test",
                        adapter=adapter,
                        runtime_provider=runtime_provider,
                        model_request=model_request,
                        outcome=invocation_outcome,
                    )
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)
    except PermissionError as exc:
        return public_exception_response(exc, status_code=403)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    if discovery.discovery_status != "ready" or model_response is None:
        return JSONResponse(
            status_code=502,
            content=jsonable_encoder(
                {
                    "detail": {
                        "code": UPSTREAM_FAILURE.code,
                        "message": UPSTREAM_FAILURE.message,
                    },
                    "provider": _serialize_model_provider(provider),
                    "capabilities": _serialize_provider_capability(capability),
                }
            ),
        )

    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(
            {
                "provider": _serialize_model_provider(provider),
                "capabilities": _serialize_provider_capability(capability),
                "result": {
                    "provider": model_response.provider,
                    "model": model_response.model,
                    "response_id": model_response.response_id,
                    "finish_reason": model_response.finish_reason,
                    "text": model_response.output_text,
                    "usage": model_response.usage,
                },
            }
        ),
    )


@router.post("/v1/runtime/invoke")
def invoke_v1_runtime(request: Request, body: RuntimeInvokeRequest) -> JSONResponse:
    settings = get_settings()
    raw_idempotency_key = request.headers.get("idempotency-key")
    if raw_idempotency_key is None or raw_idempotency_key.strip() == "":
        return JSONResponse(
            status_code=428,
            content={"detail": "Idempotency-Key header is required"},
        )
    try:
        normalized_idempotency_key = normalize_idempotency_key(raw_idempotency_key)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    workspace_id: UUID | None = None
    user_account_id: UUID | None = None
    unresolved_runtime_provider: RuntimeProviderConfig | None = None
    runtime_provider: RuntimeProviderConfig | None = None

    try:
        user_account_id = _resolve_authenticated_v1_user_id(settings, request)
        workspace_id, _ = _require_local_provider_workspace(
            settings=settings,
            user_account_id=user_account_id,
        )
    except LookupError as exc:
        return public_exception_response(exc, status_code=404)
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)

    if workspace_id is None or user_account_id is None:
        return JSONResponse(status_code=500, content={"detail": "runtime context could not be resolved"})

    fingerprint = request_fingerprint(
        cast(
            JsonObject,
            {
                "workspace_id": str(workspace_id),
                "body": body.model_dump(mode="json"),
            },
        )
    )

    # Atomically reserve or lock the stable request identity before touching
    # provider configuration, secret files, DNS, or adapters. This
    # closes the absent-row lookup/create race while preserving terminal replay
    # even if mutable runtime configuration is later removed.
    try:
        with user_connection(settings.database_url, user_account_id) as conn:
            set_current_user_account(conn, user_account_id)
            job_store = ResponseGenerationJobStore(conn)
            initial_lookup = job_store.create_or_get_for_update(
                user_id=user_account_id,
                workspace_id=workspace_id,
                endpoint=RESPONSE_JOB_ENDPOINT_RUNTIME,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_sha256=fingerprint,
            )
            replay = _response_job_replay_or_in_progress(
                store=job_store,
                job=initial_lookup.job,
                expected_request_fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
    except ResponseJobFenceLostError as exc:
        return public_exception_response(exc, status_code=409)

    # Fetch only database-backed provider state while the transaction
    # is open. Credential resolution and network-address validation happen after
    # the connection is released.
    try:
        with user_connection(settings.database_url, user_account_id) as conn:
            set_current_user_account(conn, user_account_id)
            store = ContinuityStore(conn)
            provider_row = store.get_model_provider_for_workspace_optional(
                provider_id=body.provider_id,
                workspace_id=workspace_id,
            )
            if provider_row is None:
                return public_exception_response(
                    LookupError(f"provider {body.provider_id} was not found"), status_code=404
                )
            unresolved_runtime_provider = RuntimeProviderConfig.from_row(_object_dict(provider_row))

        validate_provider_base_url(unresolved_runtime_provider.base_url)
        runtime_provider = resolve_runtime_provider_config_secrets(
            config=unresolved_runtime_provider,
            settings=settings,
        )
    except ValueError as exc:
        return public_exception_response(exc, status_code=400)
    except ProviderSecretManagerError as exc:
        return public_exception_response(exc, status_code=500)

    if runtime_provider is None:
        return JSONResponse(status_code=500, content={"detail": "runtime provider could not be resolved"})

    selected_model = (body.model or runtime_provider.default_model).strip()
    if selected_model == "":
        return JSONResponse(status_code=400, content={"detail": "model is required"})

    runtime_limits = ContextCompilerLimits(
        max_sessions=body.max_sessions,
        max_events=body.max_events,
        max_memories=body.max_memories,
        max_entities=body.max_entities,
        max_entity_edges=body.max_entity_edges,
    )
    try:
        adapter = provider_adapter_registry.resolve(runtime_provider.provider_key)
    except ProviderAdapterNotFoundError as exc:
        return public_exception_response(exc, status_code=422)

    try:
        with user_connection(settings.database_url, user_account_id) as conn:
            set_current_user_account(conn, user_account_id)
            store = ContinuityStore(conn)
            job_store = ResponseGenerationJobStore(conn)
            lookup = job_store.create_or_get_for_update(
                user_id=user_account_id,
                workspace_id=workspace_id,
                endpoint=RESPONSE_JOB_ENDPOINT_RUNTIME,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_sha256=fingerprint,
            )
            replay = _response_job_replay_or_in_progress(
                store=job_store,
                job=lookup.job,
                expected_request_fingerprint=fingerprint,
            )
            if replay is not None:
                return replay
            prepared = prepare_response_generation(
                store=store,
                user_id=user_account_id,
                thread_id=body.thread_id,
                message_text=body.message,
                limits=runtime_limits,
                runtime_override=(runtime_provider.model_provider, selected_model),
                system_instruction=SYSTEM_INSTRUCTION,
                developer_instruction=DEVELOPER_INSTRUCTION,
            )
            lease_token = uuid4()
            claimed_job = job_store.claim_pending(
                job_id=lookup.job["id"],
                lease_token=lease_token,
                lease_seconds=RESPONSE_JOB_LEASE_SECONDS,
                user_event_id=prepared.user_event_id,
                user_event_sequence_no=prepared.user_event_sequence_no,
            )
    except ContinuityStoreInvariantError as exc:
        return public_exception_response(exc, status_code=404)
    except ResponseJobFenceLostError as exc:
        return public_exception_response(exc, status_code=409)

    outcome = _attempt_runtime_provider_model(
        adapter=adapter,
        runtime_provider=runtime_provider,
        settings=settings,
        model_request=prepared.model_request,
    )

    try:
        with user_connection(settings.database_url, user_account_id) as conn:
            set_current_user_account(conn, user_account_id)
            store = ContinuityStore(conn)
            _record_runtime_provider_invocation(
                store=store,
                workspace_id=workspace_id,
                invoked_by_user_account_id=user_account_id,
                thread_id=body.thread_id,
                invocation_kind="runtime_invoke",
                adapter=adapter,
                runtime_provider=runtime_provider,
                model_request=prepared.model_request,
                outcome=outcome,
            )
            response_conflict = False
            result: GenerateResponseSuccess | ResponseFailure
            if outcome.error is not None:
                result = fail_response_generation(
                    store=store,
                    prepared=prepared,
                    error=outcome.error,
                )
            else:
                model_response = outcome.response
                if model_response is None:  # pragma: no cover - outcome invariant
                    raise ModelInvocationError("model provider returned no outcome")
                try:
                    result = complete_response_generation(
                        store=store,
                        prepared=prepared,
                        model_response=model_response,
                    )
                except ResponseGenerationConflictError as exc:
                    response_conflict = True
                    LOGGER.exception(
                        "Runtime response generation failed with public error code=%s status=%d",
                        CONFLICT.code,
                        CONFLICT.status_code,
                        exc_info=(type(exc), exc, exc.__traceback__),
                        extra={
                            "public_error_code": CONFLICT.code,
                            "public_error_status": CONFLICT.status_code,
                        },
                    )
                    result = fail_response_generation(
                        store=store,
                        prepared=prepared,
                        error=ModelInvocationError(CONFLICT.message),
                        error_code="conflict",
                    )
            response_metadata: JsonObject = {
                "workspace_id": str(workspace_id),
            }
            if isinstance(result, ResponseFailure):
                status_code = 409 if response_conflict else 502
                response_payload = cast(
                    JsonObject,
                    jsonable_encoder(
                        {
                            "detail": {
                                "code": result.error_code,
                                "message": result.detail,
                            },
                            "trace": result.trace,
                            "metadata": {
                                **response_metadata,
                                "provider_id": str(runtime_provider.provider_id),
                                "provider_key": runtime_provider.provider_key,
                            },
                        }
                    ),
                )
                terminal_state = "failed"
            else:
                successful_model_response = outcome.response
                if successful_model_response is None:  # pragma: no cover - outcome invariant
                    raise ModelInvocationError("model provider returned no outcome")
                response_payload = cast(
                    JsonObject,
                    jsonable_encoder(
                        {
                            "assistant": {
                                "event_id": result["assistant"]["event_id"],
                                "sequence_no": result["assistant"]["sequence_no"],
                                "provider_id": str(runtime_provider.provider_id),
                                "provider_key": runtime_provider.provider_key,
                                "model_provider": result["assistant"]["model_provider"],
                                "model": result["assistant"]["model"],
                                "response_id": successful_model_response.response_id,
                                "finish_reason": successful_model_response.finish_reason,
                                "text": result["assistant"]["text"],
                                "usage": successful_model_response.usage,
                            },
                            "trace": result["trace"],
                            "metadata": response_metadata,
                        }
                    ),
                )
                status_code = 200
                terminal_state = "succeeded"

            terminal_job = ResponseGenerationJobStore(conn).finalize(
                job_id=claimed_job["id"],
                lease_token=lease_token,
                state=terminal_state,
                status_code=status_code,
                payload=response_payload,
            )
    except ContinuityStoreInvariantError as exc:
        return public_exception_response(exc, status_code=404)
    except ResponseJobFenceLostError as exc:
        return public_exception_response(exc, status_code=409)

    return JSONResponse(
        status_code=status_code,
        headers=_response_job_headers(terminal_job, replayed=False),
        content=response_payload,
    )
