from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request
from uuid import UUID

import psycopg

from alicebot_api.artifacts import (
    SUPPORTED_TEXT_ARTIFACT_MEDIA_TYPES,
    TaskArtifactAlreadyExistsError,
    TaskArtifactValidationError,
    ensure_artifact_path_is_rooted,
    extract_artifact_text_from_bytes,
    ingest_task_artifact_record,
    register_task_artifact_record,
)
from alicebot_api.calendar_secret_manager import (
    CALENDAR_SECRET_MANAGER_KIND_FILE_V1,
    CalendarSecretManager,
    CalendarSecretManagerError,
)
from alicebot_api.contracts import (
    CALENDAR_ACCOUNT_LIST_ORDER,
    CALENDAR_EVENT_LIST_ORDER,
    CALENDAR_AUTH_KIND_OAUTH_ACCESS_TOKEN,
    MAX_CALENDAR_EVENT_LIST_LIMIT,
    CALENDAR_PROTECTED_CREDENTIAL_KIND,
    CALENDAR_PROVIDER,
    CALENDAR_READONLY_SCOPE,
    CalendarAccountConnectInput,
    CalendarAccountConnectResponse,
    CalendarAccountDetailResponse,
    CalendarAccountListResponse,
    CalendarAccountRecord,
    CalendarEventListInput,
    CalendarEventListResponse,
    CalendarEventSummaryRecord,
    CalendarEventIngestInput,
    CalendarEventIngestionResponse,
    TaskArtifactIngestInput,
    TaskArtifactRegisterInput,
)
from alicebot_api.provider_http import open_provider_url
from alicebot_api.store import (
    CalendarAccountRow,
    ContinuityStore,
    ContinuityStoreInvariantError,
    JsonObject,
)
from alicebot_api.workspaces import TaskWorkspaceNotFoundError

CALENDAR_EVENT_FETCH_TIMEOUT_SECONDS = 30
CALENDAR_EVENT_ARTIFACT_ROOT = "calendar"
CALENDAR_EVENT_ARTIFACT_MEDIA_TYPE = SUPPORTED_TEXT_ARTIFACT_MEDIA_TYPES[0]
_PATH_SEGMENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class CalendarAccountNotFoundError(LookupError):
    """Raised when a Calendar account is not visible inside the current user scope."""


class CalendarAccountAlreadyExistsError(RuntimeError):
    """Raised when the same provider account is connected twice for one user."""


class CalendarEventNotFoundError(LookupError):
    """Raised when a Calendar event cannot be found in the current account."""


class CalendarEventUnsupportedError(ValueError):
    """Raised when Calendar content cannot be converted into the text artifact seam."""


class CalendarEventFetchError(RuntimeError):
    """Raised when the Calendar API call fails for non-deterministic upstream reasons."""


class CalendarEventListValidationError(ValueError):
    """Raised when Calendar event list query filters are invalid."""


class CalendarCredentialNotFoundError(RuntimeError):
    """Raised when Calendar protected credentials are missing for a visible account."""


class CalendarCredentialInvalidError(RuntimeError):
    """Raised when Calendar protected credentials are malformed for a visible account."""


class CalendarCredentialPersistenceError(RuntimeError):
    """Raised when Calendar protected credentials cannot be persisted."""


class CalendarCredentialValidationError(ValueError):
    """Raised when Calendar connect input contains an invalid credential payload."""


def serialize_calendar_account_row(row: CalendarAccountRow) -> CalendarAccountRecord:
    return {
        "id": str(row["id"]),
        "provider": CALENDAR_PROVIDER,
        "auth_kind": CALENDAR_AUTH_KIND_OAUTH_ACCESS_TOKEN,
        "provider_account_id": row["provider_account_id"],
        "email_address": row["email_address"],
        "display_name": row["display_name"],
        "scope": row["scope"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _coerce_nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    return normalized


class CalendarProtectedCredential(TypedDict):
    credential_kind: str
    access_token: str


def build_calendar_protected_credential_blob(*, access_token: str) -> CalendarProtectedCredential:
    normalized_access_token = _coerce_nonempty_string(access_token)
    if normalized_access_token is None:
        raise CalendarCredentialValidationError("calendar access token must be non-empty")
    return {
        "credential_kind": CALENDAR_PROTECTED_CREDENTIAL_KIND,
        "access_token": normalized_access_token,
    }


def build_calendar_secret_ref(*, user_id: UUID, calendar_account_id: UUID) -> str:
    return f"users/{user_id}/calendar-account-credentials/{calendar_account_id}.json"


def _parse_calendar_credential(*, calendar_account_id: UUID, credential_blob: object) -> str:
    if not isinstance(credential_blob, dict):
        raise CalendarCredentialInvalidError(
            f"calendar account {calendar_account_id} has invalid protected credentials"
        )
    credential_kind = credential_blob.get("credential_kind")
    access_token = _coerce_nonempty_string(credential_blob.get("access_token"))
    if credential_kind != CALENDAR_PROTECTED_CREDENTIAL_KIND or access_token is None:
        raise CalendarCredentialInvalidError(
            f"calendar account {calendar_account_id} has invalid protected credentials"
        )
    return access_token


def _write_external_calendar_secret(
    secret_manager: CalendarSecretManager,
    *,
    calendar_account_id: UUID,
    secret_ref: str,
    credential_blob: CalendarProtectedCredential,
) -> None:
    try:
        payload: JsonObject = {
            "credential_kind": credential_blob["credential_kind"],
            "access_token": credential_blob["access_token"],
        }
        secret_manager.write_secret(secret_ref=secret_ref, payload=payload)
    except CalendarSecretManagerError as exc:
        raise CalendarCredentialPersistenceError(
            f"calendar account {calendar_account_id} protected credentials could not be persisted"
        ) from exc


def resolve_calendar_access_token(
    store: ContinuityStore,
    secret_manager: CalendarSecretManager,
    *,
    calendar_account_id: UUID,
) -> str:
    credential = store.get_calendar_account_credential_optional(calendar_account_id)
    if credential is None:
        raise CalendarCredentialNotFoundError(
            f"calendar account {calendar_account_id} is missing protected credentials"
        )
    if credential["auth_kind"] != CALENDAR_AUTH_KIND_OAUTH_ACCESS_TOKEN:
        raise CalendarCredentialInvalidError(
            f"calendar account {calendar_account_id} has invalid protected credentials"
        )
    if credential["secret_manager_kind"] != CALENDAR_SECRET_MANAGER_KIND_FILE_V1:
        raise CalendarCredentialInvalidError(
            f"calendar account {calendar_account_id} has invalid protected credentials"
        )
    secret_ref = _coerce_nonempty_string(credential["secret_ref"])
    if secret_ref is None:
        raise CalendarCredentialInvalidError(
            f"calendar account {calendar_account_id} has invalid protected credentials"
        )

    try:
        payload = secret_manager.load_secret(secret_ref=secret_ref)
    except CalendarSecretManagerError as exc:
        message = str(exc)
        if message.endswith("was not found"):
            raise CalendarCredentialNotFoundError(
                f"calendar account {calendar_account_id} is missing protected credentials"
            ) from exc
        raise CalendarCredentialInvalidError(
            f"calendar account {calendar_account_id} has invalid protected credentials"
        ) from exc
    return _parse_calendar_credential(
        calendar_account_id=calendar_account_id,
        credential_blob=payload,
    )


def create_calendar_account_record(
    store: ContinuityStore,
    secret_manager: CalendarSecretManager,
    *,
    user_id: UUID,
    request: CalendarAccountConnectInput,
) -> CalendarAccountConnectResponse:
    del user_id

    existing = store.get_calendar_account_by_provider_account_id_optional(request.provider_account_id)
    if existing is not None:
        raise CalendarAccountAlreadyExistsError(
            f"calendar account {request.provider_account_id} is already connected"
        )

    row: CalendarAccountRow | None = None
    secret_ref: str | None = None
    try:
        row = store.create_calendar_account(
            provider_account_id=request.provider_account_id,
            email_address=request.email_address,
            display_name=request.display_name,
            scope=request.scope,
        )
        credential_blob = build_calendar_protected_credential_blob(
            access_token=request.access_token,
        )
        secret_ref = build_calendar_secret_ref(
            user_id=row["user_id"],
            calendar_account_id=row["id"],
        )
        _write_external_calendar_secret(
            secret_manager,
            calendar_account_id=row["id"],
            secret_ref=secret_ref,
            credential_blob=credential_blob,
        )
        store.create_calendar_account_credential(
            calendar_account_id=row["id"],
            auth_kind=CALENDAR_AUTH_KIND_OAUTH_ACCESS_TOKEN,
            credential_kind=credential_blob["credential_kind"],
            secret_manager_kind=secret_manager.kind,
            secret_ref=secret_ref,
            credential_blob=None,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise CalendarAccountAlreadyExistsError(
            f"calendar account {request.provider_account_id} is already connected"
        ) from exc
    except CalendarCredentialPersistenceError:
        raise
    except (ContinuityStoreInvariantError, psycopg.Error) as exc:
        if secret_ref is not None:
            try:
                secret_manager.delete_secret(secret_ref=secret_ref)
            except CalendarSecretManagerError:
                pass
        raise CalendarCredentialPersistenceError(
            "calendar protected credentials could not be persisted"
        ) from exc

    return {"account": serialize_calendar_account_row(row)}


def list_calendar_account_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> CalendarAccountListResponse:
    del user_id

    items = [serialize_calendar_account_row(row) for row in store.list_calendar_accounts()]
    return {
        "items": items,
        "summary": {
            "total_count": len(items),
            "order": list(CALENDAR_ACCOUNT_LIST_ORDER),
        },
    }


def get_calendar_account_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    calendar_account_id: UUID,
) -> CalendarAccountDetailResponse:
    del user_id

    row = store.get_calendar_account_optional(calendar_account_id)
    if row is None:
        raise CalendarAccountNotFoundError(f"calendar account {calendar_account_id} was not found")
    return {"account": serialize_calendar_account_row(row)}


def _extract_optional_calendar_event_time(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    date_time = _coerce_nonempty_string(value.get("dateTime"))
    if date_time is not None:
        return date_time
    return _coerce_nonempty_string(value.get("date"))


def _serialize_calendar_event_summary(payload: object) -> CalendarEventSummaryRecord | None:
    if not isinstance(payload, dict):
        return None
    provider_event_id = _coerce_nonempty_string(payload.get("id"))
    if provider_event_id is None:
        return None
    return {
        "provider_event_id": provider_event_id,
        "status": _coerce_nonempty_string(payload.get("status")),
        "summary": _coerce_nonempty_string(payload.get("summary")),
        "start_time": _extract_optional_calendar_event_time(payload.get("start")),
        "end_time": _extract_optional_calendar_event_time(payload.get("end")),
        "html_link": _coerce_nonempty_string(payload.get("htmlLink")),
        "updated_at": _coerce_nonempty_string(payload.get("updated")),
    }


def _normalize_start_time_for_sort(start_time: str | None) -> str | None:
    if start_time is None:
        return None

    # All-day events use date-only values; normalize to midnight UTC.
    if len(start_time) == 10:
        try:
            parsed_date = date.fromisoformat(start_time)
        except ValueError:
            return None
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=UTC,
        ).isoformat()

    normalized = start_time.replace("Z", "+00:00")
    try:
        parsed_datetime = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed_datetime.tzinfo is None:
        parsed_datetime = parsed_datetime.replace(tzinfo=UTC)
    return parsed_datetime.astimezone(UTC).isoformat()


def _calendar_event_sort_key(record: CalendarEventSummaryRecord) -> tuple[str, str]:
    start_time = record["start_time"]
    return (
        _normalize_start_time_for_sort(start_time) or "~",
        record["provider_event_id"],
    )


def fetch_calendar_event_list_payload(
    *,
    access_token: str,
    limit: int = MAX_CALENDAR_EVENT_LIST_LIMIT,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
) -> list[JsonObject]:
    query_params: dict[str, str] = {
        "maxResults": str(limit),
        "showDeleted": "false",
        "singleEvents": "false",
    }
    if time_min is not None:
        query_params["timeMin"] = time_min.isoformat()
    if time_max is not None:
        query_params["timeMax"] = time_max.isoformat()
    query_string = urlencode(query_params)
    request = Request(
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events?{query_string}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with open_provider_url(
            request,
            timeout=CALENDAR_EVENT_FETCH_TIMEOUT_SECONDS,
            enforce_public_peer=True,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise CalendarEventFetchError("calendar events could not be fetched") from exc
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalendarEventFetchError("calendar events could not be fetched") from exc

    if not isinstance(payload, dict):
        raise CalendarEventFetchError("calendar events could not be fetched")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise CalendarEventFetchError("calendar events could not be fetched")

    items: list[JsonObject] = []
    for item in raw_items:
        if isinstance(item, dict):
            items.append(item)
    return items


def list_calendar_event_records(
    store: ContinuityStore,
    secret_manager: CalendarSecretManager,
    *,
    user_id: UUID,
    request: CalendarEventListInput,
) -> CalendarEventListResponse:
    del user_id

    if request.time_min is not None and request.time_max is not None and request.time_min > request.time_max:
        raise CalendarEventListValidationError("calendar event time_min must be less than or equal to time_max")

    account = store.get_calendar_account_optional(request.calendar_account_id)
    if account is None:
        raise CalendarAccountNotFoundError(f"calendar account {request.calendar_account_id} was not found")

    bounded_limit = max(1, min(request.limit, MAX_CALENDAR_EVENT_LIST_LIMIT))
    access_token = resolve_calendar_access_token(
        store,
        secret_manager,
        calendar_account_id=request.calendar_account_id,
    )
    raw_items = fetch_calendar_event_list_payload(
        access_token=access_token,
        limit=MAX_CALENDAR_EVENT_LIST_LIMIT,
        time_min=request.time_min,
        time_max=request.time_max,
    )
    records = [
        record
        for record in (_serialize_calendar_event_summary(item) for item in raw_items)
        if record is not None
    ]
    items = sorted(records, key=_calendar_event_sort_key)[:bounded_limit]
    return {
        "account": serialize_calendar_account_row(account),
        "items": items,
        "summary": {
            "total_count": len(items),
            "limit": bounded_limit,
            "order": list(CALENDAR_EVENT_LIST_ORDER),
            "time_min": None if request.time_min is None else request.time_min.isoformat(),
            "time_max": None if request.time_max is None else request.time_max.isoformat(),
        },
    }


def _sanitize_path_segment(value: str) -> str:
    sanitized = _PATH_SEGMENT_PATTERN.sub("_", value.strip())
    return sanitized.strip("._") or "event"


def build_calendar_event_artifact_relative_path(
    *,
    provider_account_id: str,
    provider_event_id: str,
) -> str:
    return (
        f"{CALENDAR_EVENT_ARTIFACT_ROOT}/"
        f"{_sanitize_path_segment(provider_account_id)}/"
        f"{_sanitize_path_segment(provider_event_id)}.txt"
    )


def fetch_calendar_event_payload(*, access_token: str, provider_event_id: str) -> JsonObject:
    request = Request(
        (
            "https://www.googleapis.com/calendar/v3/calendars/primary/events/"
            f"{quote(provider_event_id, safe='')}"
        ),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with open_provider_url(
            request,
            timeout=CALENDAR_EVENT_FETCH_TIMEOUT_SECONDS,
            enforce_public_peer=True,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise CalendarEventNotFoundError(
                f"calendar event {provider_event_id} was not found"
            ) from exc
        raise CalendarEventFetchError(
            f"calendar event {provider_event_id} could not be fetched"
        ) from exc
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalendarEventFetchError(
            f"calendar event {provider_event_id} could not be fetched"
        ) from exc

    if not isinstance(payload, dict):
        raise CalendarEventUnsupportedError(
            f"calendar event {provider_event_id} is not supported for ingestion"
        )
    return payload


def _extract_calendar_event_time(
    *,
    provider_event_id: str,
    payload: JsonObject,
    key: str,
) -> str:
    field = payload.get(key)
    if not isinstance(field, dict):
        raise CalendarEventUnsupportedError(
            f"calendar event {provider_event_id} is not supported for ingestion"
        )
    date_time = _coerce_nonempty_string(field.get("dateTime"))
    if date_time is not None:
        return date_time
    date = _coerce_nonempty_string(field.get("date"))
    if date is not None:
        return date
    raise CalendarEventUnsupportedError(
        f"calendar event {provider_event_id} is not supported for ingestion"
    )


def _optional_event_text(value: object) -> str:
    normalized = _coerce_nonempty_string(value)
    if normalized is None:
        return "(none)"
    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def build_calendar_event_artifact_text(*, provider_event_id: str, payload: JsonObject) -> str:
    source_event_id = _coerce_nonempty_string(payload.get("id"))
    if source_event_id is None:
        raise CalendarEventUnsupportedError(
            f"calendar event {provider_event_id} is not supported for ingestion"
        )
    start_value = _extract_calendar_event_time(
        provider_event_id=provider_event_id,
        payload=payload,
        key="start",
    )
    end_value = _extract_calendar_event_time(
        provider_event_id=provider_event_id,
        payload=payload,
        key="end",
    )
    organizer_email = None
    organizer = payload.get("organizer")
    if isinstance(organizer, dict):
        organizer_email = _coerce_nonempty_string(organizer.get("email"))

    lines = [
        f"Provider: {CALENDAR_PROVIDER}",
        f"Requested Event ID: {provider_event_id}",
        f"Source Event ID: {source_event_id}",
        f"Status: {_optional_event_text(payload.get('status'))}",
        f"Summary: {_optional_event_text(payload.get('summary'))}",
        f"Location: {_optional_event_text(payload.get('location'))}",
        f"Start: {start_value}",
        f"End: {end_value}",
        f"Organizer Email: {_optional_event_text(organizer_email)}",
        f"HTML Link: {_optional_event_text(payload.get('htmlLink'))}",
        "Description:",
        _optional_event_text(payload.get("description")),
    ]
    return "\n".join(lines).strip()


def ingest_calendar_event_record(
    store: ContinuityStore,
    secret_manager: CalendarSecretManager,
    *,
    user_id: UUID,
    request: CalendarEventIngestInput,
) -> CalendarEventIngestionResponse:
    account = store.get_calendar_account_optional(request.calendar_account_id)
    if account is None:
        raise CalendarAccountNotFoundError(f"calendar account {request.calendar_account_id} was not found")

    workspace = store.get_task_workspace_optional(request.task_workspace_id)
    if workspace is None:
        raise TaskWorkspaceNotFoundError(
            f"task workspace {request.task_workspace_id} was not found"
        )

    access_token = resolve_calendar_access_token(
        store,
        secret_manager,
        calendar_account_id=request.calendar_account_id,
    )
    store.lock_task_artifacts(workspace["id"])

    relative_path = build_calendar_event_artifact_relative_path(
        provider_account_id=account["provider_account_id"],
        provider_event_id=request.provider_event_id,
    )
    existing_artifact = store.get_task_artifact_by_workspace_relative_path_optional(
        task_workspace_id=request.task_workspace_id,
        relative_path=relative_path,
    )
    if existing_artifact is not None:
        raise TaskArtifactAlreadyExistsError(
            f"artifact {relative_path} is already registered for task workspace {request.task_workspace_id}"
        )

    event_payload = fetch_calendar_event_payload(
        access_token=access_token,
        provider_event_id=request.provider_event_id,
    )
    artifact_text = build_calendar_event_artifact_text(
        provider_event_id=request.provider_event_id,
        payload=event_payload,
    )
    artifact_bytes = artifact_text.encode("utf-8")
    extract_artifact_text_from_bytes(
        relative_path=relative_path,
        payload=artifact_bytes,
        media_type=CALENDAR_EVENT_ARTIFACT_MEDIA_TYPE,
    )

    workspace_path = Path(workspace["local_path"]).expanduser().resolve()
    artifact_path = (workspace_path / relative_path).resolve()
    ensure_artifact_path_is_rooted(
        workspace_path=workspace_path,
        artifact_path=artifact_path,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if artifact_path.exists():
        raise TaskArtifactValidationError(
            f"artifact path {artifact_path} already exists before Calendar ingestion registration"
        )
    artifact_path.write_bytes(artifact_bytes)

    artifact_payload = register_task_artifact_record(
        store,
        user_id=user_id,
        request=TaskArtifactRegisterInput(
            task_workspace_id=request.task_workspace_id,
            local_path=str(artifact_path),
            media_type_hint=CALENDAR_EVENT_ARTIFACT_MEDIA_TYPE,
        ),
    )
    ingestion_payload = ingest_task_artifact_record(
        store,
        user_id=user_id,
        request=TaskArtifactIngestInput(task_artifact_id=UUID(artifact_payload["artifact"]["id"])),
    )
    return {
        "account": serialize_calendar_account_row(account),
        "event": {
            "provider_event_id": request.provider_event_id,
            "artifact_relative_path": ingestion_payload["artifact"]["relative_path"],
            "media_type": CALENDAR_EVENT_ARTIFACT_MEDIA_TYPE,
        },
        "artifact": ingestion_payload["artifact"],
        "summary": ingestion_payload["summary"],
    }
