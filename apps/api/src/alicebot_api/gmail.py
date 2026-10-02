from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request
from uuid import UUID

import psycopg

from alicebot_api.artifacts import (
    SUPPORTED_RFC822_ARTIFACT_MEDIA_TYPE,
    TaskArtifactAlreadyExistsError,
    TaskArtifactValidationError,
    ensure_artifact_path_is_rooted,
    extract_artifact_text_from_bytes,
    ingest_task_artifact_record,
    register_task_artifact_record,
)
from alicebot_api.contracts import (
    GMAIL_ACCOUNT_LIST_ORDER,
    GMAIL_AUTH_KIND_OAUTH_ACCESS_TOKEN,
    GMAIL_PROTECTED_CREDENTIAL_KIND,
    GMAIL_PROVIDER,
    GMAIL_REFRESHABLE_PROTECTED_CREDENTIAL_KIND,
    GMAIL_READONLY_SCOPE,
    GmailAccountConnectInput,
    GmailAccountConnectResponse,
    GmailAccountDetailResponse,
    GmailAccountListResponse,
    GmailAccountRecord,
    GmailMessageIngestInput,
    GmailMessageIngestionResponse,
    TaskArtifactIngestInput,
    TaskArtifactRegisterInput,
)
from alicebot_api.gmail_secret_manager import (
    GMAIL_SECRET_MANAGER_KIND_FILE_V1,
    GmailSecretManager,
    GmailSecretManagerError,
)
from alicebot_api.provider_http import open_provider_url
from alicebot_api.store import ContinuityStore, ContinuityStoreInvariantError, GmailAccountRow, JsonObject
from alicebot_api.workspaces import TaskWorkspaceNotFoundError

GMAIL_MESSAGE_FETCH_TIMEOUT_SECONDS = 30
GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS = 30
GMAIL_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"
GMAIL_MESSAGE_ARTIFACT_ROOT = "gmail"
GMAIL_SECRET_MANAGER_KIND_LEGACY_DB_V0 = "legacy_db_v0"
_PATH_SEGMENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class GmailAccountNotFoundError(LookupError):
    """Raised when a Gmail account is not visible inside the current user scope."""


class GmailAccountAlreadyExistsError(RuntimeError):
    """Raised when the same provider account is connected twice for one user."""


class GmailMessageNotFoundError(LookupError):
    """Raised when a Gmail message cannot be found in the current account."""


class GmailMessageUnsupportedError(ValueError):
    """Raised when Gmail content cannot be converted into the RFC822 artifact seam."""


class GmailMessageFetchError(RuntimeError):
    """Raised when the Gmail API call fails for non-deterministic upstream reasons."""


class GmailCredentialNotFoundError(RuntimeError):
    """Raised when Gmail protected credentials are missing for a visible account."""


class GmailCredentialInvalidError(RuntimeError):
    """Raised when Gmail protected credentials are malformed for a visible account."""


class GmailCredentialRefreshError(RuntimeError):
    """Raised when Gmail access-token renewal fails for non-deterministic reasons."""


class GmailCredentialPersistenceError(RuntimeError):
    """Raised when renewed Gmail protected credentials cannot be persisted."""


class GmailCredentialValidationError(ValueError):
    """Raised when Gmail connect input contains an invalid credential combination."""


@dataclass(frozen=True, slots=True)
class ParsedGmailCredential:
    access_token: str
    credential_kind: str
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    access_token_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RefreshedGmailCredential:
    access_token: str
    access_token_expires_at: datetime
    refresh_token: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedGmailCredential:
    parsed_credential: ParsedGmailCredential
    credential_kind: str
    secret_manager_kind: str
    secret_ref: str | None
    credential_blob: JsonObject | None


def serialize_gmail_account_row(row: GmailAccountRow) -> GmailAccountRecord:
    return {
        "id": str(row["id"]),
        "provider": GMAIL_PROVIDER,
        "auth_kind": GMAIL_AUTH_KIND_OAUTH_ACCESS_TOKEN,
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


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_gmail_protected_credential_blob(
    *,
    access_token: str,
    refresh_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    access_token_expires_at: datetime | None = None,
) -> dict[str, str]:
    normalized_access_token = _coerce_nonempty_string(access_token)
    if normalized_access_token is None:
        raise GmailCredentialValidationError("gmail access token must be non-empty")

    refresh_bundle = (
        refresh_token,
        client_id,
        client_secret,
        access_token_expires_at,
    )
    if all(value is None for value in refresh_bundle):
        return {
            "credential_kind": GMAIL_PROTECTED_CREDENTIAL_KIND,
            "access_token": normalized_access_token,
        }

    normalized_refresh_token = _coerce_nonempty_string(refresh_token)
    normalized_client_id = _coerce_nonempty_string(client_id)
    normalized_client_secret = _coerce_nonempty_string(client_secret)
    if (
        normalized_refresh_token is None
        or normalized_client_id is None
        or normalized_client_secret is None
        or access_token_expires_at is None
    ):
        raise GmailCredentialValidationError(
            "gmail refresh credentials must include refresh_token, client_id, client_secret, "
            "and access_token_expires_at"
        )

    normalized_expires_at = _normalize_datetime(access_token_expires_at)
    return {
        "credential_kind": GMAIL_REFRESHABLE_PROTECTED_CREDENTIAL_KIND,
        "access_token": normalized_access_token,
        "refresh_token": normalized_refresh_token,
        "client_id": normalized_client_id,
        "client_secret": normalized_client_secret,
        "access_token_expires_at": normalized_expires_at.isoformat(),
    }


def build_gmail_secret_ref(*, user_id: UUID, gmail_account_id: UUID) -> str:
    return f"users/{user_id}/gmail-account-credentials/{gmail_account_id}.json"


def _write_external_gmail_secret(
    secret_manager: GmailSecretManager,
    *,
    gmail_account_id: UUID,
    secret_ref: str,
    credential_blob: JsonObject,
) -> None:
    try:
        secret_manager.write_secret(secret_ref=secret_ref, payload=credential_blob)
    except GmailSecretManagerError as exc:
        raise GmailCredentialPersistenceError(
            f"gmail account {gmail_account_id} protected credentials could not be persisted"
        ) from exc


def _load_external_gmail_secret(
    secret_manager: GmailSecretManager,
    *,
    gmail_account_id: UUID,
    secret_ref: str,
) -> JsonObject:
    try:
        return secret_manager.load_secret(secret_ref=secret_ref)
    except GmailSecretManagerError as exc:
        message = str(exc)
        if message.endswith("was not found"):
            raise GmailCredentialNotFoundError(
                f"gmail account {gmail_account_id} is missing protected credentials"
            ) from exc
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        ) from exc


def _persist_external_gmail_credential_metadata(
    store: ContinuityStore,
    *,
    gmail_account_id: UUID,
    auth_kind: str,
    credential_kind: str,
    secret_manager_kind: str,
    secret_ref: str,
) -> None:
    store.update_gmail_account_credential(
        gmail_account_id=gmail_account_id,
        auth_kind=auth_kind,
        credential_kind=credential_kind,
        secret_manager_kind=secret_manager_kind,
        secret_ref=secret_ref,
        credential_blob=None,
    )


def _resolve_gmail_credential(
    store: ContinuityStore,
    secret_manager: GmailSecretManager,
    *,
    gmail_account_id: UUID,
) -> ResolvedGmailCredential:
    credential = store.get_gmail_account_credential_optional(gmail_account_id)
    if credential is None:
        raise GmailCredentialNotFoundError(
            f"gmail account {gmail_account_id} is missing protected credentials"
        )

    if credential["auth_kind"] != GMAIL_AUTH_KIND_OAUTH_ACCESS_TOKEN:
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    if credential["secret_manager_kind"] == GMAIL_SECRET_MANAGER_KIND_FILE_V1:
        secret_ref = _coerce_nonempty_string(credential["secret_ref"])
        if secret_ref is None:
            raise GmailCredentialInvalidError(
                f"gmail account {gmail_account_id} has invalid protected credentials"
            )
        return ResolvedGmailCredential(
            parsed_credential=_parse_gmail_credential(
                gmail_account_id=gmail_account_id,
                credential_blob=_load_external_gmail_secret(
                    secret_manager,
                    gmail_account_id=gmail_account_id,
                    secret_ref=secret_ref,
                ),
            ),
            credential_kind=credential["credential_kind"],
            secret_manager_kind=credential["secret_manager_kind"],
            secret_ref=secret_ref,
            credential_blob=None,
        )

    if credential["secret_manager_kind"] != GMAIL_SECRET_MANAGER_KIND_LEGACY_DB_V0:
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    if credential["credential_blob"] is None:
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    parsed_credential = _parse_gmail_credential(
        gmail_account_id=gmail_account_id,
        credential_blob=credential["credential_blob"],
    )
    secret_ref = build_gmail_secret_ref(
        user_id=credential["user_id"],
        gmail_account_id=gmail_account_id,
    )
    _write_external_gmail_secret(
        secret_manager,
        gmail_account_id=gmail_account_id,
        secret_ref=secret_ref,
        credential_blob=credential["credential_blob"],
    )
    try:
        _persist_external_gmail_credential_metadata(
            store,
            gmail_account_id=gmail_account_id,
            auth_kind=credential["auth_kind"],
            credential_kind=parsed_credential.credential_kind,
            secret_manager_kind=secret_manager.kind,
            secret_ref=secret_ref,
        )
    except (ContinuityStoreInvariantError, psycopg.Error) as exc:
        try:
            secret_manager.delete_secret(secret_ref=secret_ref)
        except GmailSecretManagerError:
            pass
        raise GmailCredentialPersistenceError(
            f"gmail account {gmail_account_id} protected credentials could not be persisted"
        ) from exc

    return ResolvedGmailCredential(
        parsed_credential=parsed_credential,
        credential_kind=parsed_credential.credential_kind,
        secret_manager_kind=secret_manager.kind,
        secret_ref=secret_ref,
        credential_blob=None,
    )


def _parse_gmail_credential(
    *,
    gmail_account_id: UUID,
    credential_blob: object,
) -> ParsedGmailCredential:
    if not isinstance(credential_blob, dict):
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    credential_kind = credential_blob.get("credential_kind")
    access_token = _coerce_nonempty_string(credential_blob.get("access_token"))
    if access_token is None:
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    if credential_kind == GMAIL_PROTECTED_CREDENTIAL_KIND:
        return ParsedGmailCredential(
            access_token=access_token,
            credential_kind=credential_kind,
        )

    if credential_kind != GMAIL_REFRESHABLE_PROTECTED_CREDENTIAL_KIND:
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    refresh_token = _coerce_nonempty_string(credential_blob.get("refresh_token"))
    client_id = _coerce_nonempty_string(credential_blob.get("client_id"))
    client_secret = _coerce_nonempty_string(credential_blob.get("client_secret"))
    access_token_expires_at_raw = _coerce_nonempty_string(
        credential_blob.get("access_token_expires_at")
    )
    if (
        refresh_token is None
        or client_id is None
        or client_secret is None
        or access_token_expires_at_raw is None
    ):
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        )

    try:
        access_token_expires_at = _normalize_datetime(
            datetime.fromisoformat(access_token_expires_at_raw)
        )
    except ValueError as exc:
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has invalid protected credentials"
        ) from exc

    return ParsedGmailCredential(
        access_token=access_token,
        credential_kind=credential_kind,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        access_token_expires_at=access_token_expires_at,
    )


def refresh_gmail_access_token(
    *,
    gmail_account_id: UUID,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> RefreshedGmailCredential:
    request = Request(
        GMAIL_TOKEN_REFRESH_URL,
        data=urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with open_provider_url(
            request,
            timeout=GMAIL_TOKEN_REFRESH_TIMEOUT_SECONDS,
            enforce_public_peer=True,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {400, 401}:
            raise GmailCredentialInvalidError(
                f"gmail account {gmail_account_id} refresh credentials were rejected"
            ) from exc
        raise GmailCredentialRefreshError(
            f"gmail account {gmail_account_id} access token could not be renewed"
        ) from exc
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailCredentialRefreshError(
            f"gmail account {gmail_account_id} access token could not be renewed"
        ) from exc

    refreshed_access_token = _coerce_nonempty_string(payload.get("access_token"))
    replacement_refresh_token = _coerce_nonempty_string(payload.get("refresh_token"))
    expires_in = payload.get("expires_in")
    if refreshed_access_token is None or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise GmailCredentialRefreshError(
            f"gmail account {gmail_account_id} access token could not be renewed"
        )

    refreshed_expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
    return RefreshedGmailCredential(
        access_token=refreshed_access_token,
        access_token_expires_at=refreshed_expires_at,
        refresh_token=replacement_refresh_token,
    )


def _persist_refreshed_gmail_credential(
    store: ContinuityStore,
    secret_manager: GmailSecretManager,
    *,
    gmail_account_id: UUID,
    auth_kind: str,
    secret_ref: str,
    existing_credential: ParsedGmailCredential,
    refreshed_credential: RefreshedGmailCredential,
) -> None:
    original_credential_blob = build_gmail_protected_credential_blob(
        access_token=existing_credential.access_token,
        refresh_token=existing_credential.refresh_token,
        client_id=existing_credential.client_id,
        client_secret=existing_credential.client_secret,
        access_token_expires_at=existing_credential.access_token_expires_at,
    )
    replacement_refresh_token = (
        refreshed_credential.refresh_token
        if refreshed_credential.refresh_token is not None
        else existing_credential.refresh_token
    )
    replacement_credential_blob = build_gmail_protected_credential_blob(
        access_token=refreshed_credential.access_token,
        refresh_token=replacement_refresh_token,
        client_id=existing_credential.client_id,
        client_secret=existing_credential.client_secret,
        access_token_expires_at=refreshed_credential.access_token_expires_at,
    )
    try:
        secret_manager.write_secret(
            secret_ref=secret_ref,
            payload=cast(JsonObject, replacement_credential_blob),
        )
        store.update_gmail_account_credential(
            gmail_account_id=gmail_account_id,
            auth_kind=auth_kind,
            credential_kind=replacement_credential_blob["credential_kind"],
            secret_manager_kind=secret_manager.kind,
            secret_ref=secret_ref,
            credential_blob=None,
        )
    except (GmailSecretManagerError, ContinuityStoreInvariantError, psycopg.Error) as exc:
        try:
            secret_manager.write_secret(
                secret_ref=secret_ref,
                payload=cast(JsonObject, original_credential_blob),
            )
        except GmailSecretManagerError:
            pass
        raise GmailCredentialPersistenceError(
            f"gmail account {gmail_account_id} renewed protected credentials could not be persisted"
        ) from exc


def resolve_gmail_access_token(
    store: ContinuityStore,
    secret_manager: GmailSecretManager,
    *,
    gmail_account_id: UUID,
) -> str:
    credential = _resolve_gmail_credential(
        store,
        secret_manager,
        gmail_account_id=gmail_account_id,
    )
    parsed_credential = credential.parsed_credential
    if (
        parsed_credential.credential_kind != GMAIL_REFRESHABLE_PROTECTED_CREDENTIAL_KIND
        or parsed_credential.access_token_expires_at is None
        or parsed_credential.access_token_expires_at > datetime.now(UTC)
    ):
        return parsed_credential.access_token

    if (
        parsed_credential.refresh_token is None
        or parsed_credential.client_id is None
        or parsed_credential.client_secret is None
        or credential.secret_ref is None
    ):
        raise GmailCredentialInvalidError(
            f"gmail account {gmail_account_id} has incomplete refresh credentials"
        )

    refreshed_credential = refresh_gmail_access_token(
        gmail_account_id=gmail_account_id,
        refresh_token=parsed_credential.refresh_token,
        client_id=parsed_credential.client_id,
        client_secret=parsed_credential.client_secret,
    )
    _persist_refreshed_gmail_credential(
        store,
        secret_manager,
        gmail_account_id=gmail_account_id,
        auth_kind=GMAIL_AUTH_KIND_OAUTH_ACCESS_TOKEN,
        secret_ref=credential.secret_ref,
        existing_credential=parsed_credential,
        refreshed_credential=refreshed_credential,
    )
    return refreshed_credential.access_token


def create_gmail_account_record(
    store: ContinuityStore,
    secret_manager: GmailSecretManager,
    *,
    user_id: UUID,
    request: GmailAccountConnectInput,
) -> GmailAccountConnectResponse:
    del user_id

    existing = store.get_gmail_account_by_provider_account_id_optional(request.provider_account_id)
    if existing is not None:
        raise GmailAccountAlreadyExistsError(
            f"gmail account {request.provider_account_id} is already connected"
        )

    row: GmailAccountRow | None = None
    secret_ref: str | None = None
    try:
        row = store.create_gmail_account(
            provider_account_id=request.provider_account_id,
            email_address=request.email_address,
            display_name=request.display_name,
            scope=request.scope,
        )
        credential_blob = build_gmail_protected_credential_blob(
            access_token=request.access_token,
            refresh_token=request.refresh_token,
            client_id=request.client_id,
            client_secret=request.client_secret,
            access_token_expires_at=request.access_token_expires_at,
        )
        secret_ref = build_gmail_secret_ref(
            user_id=row["user_id"],
            gmail_account_id=row["id"],
        )
        _write_external_gmail_secret(
            secret_manager,
            gmail_account_id=row["id"],
            secret_ref=secret_ref,
            credential_blob=cast(JsonObject, credential_blob),
        )
        store.create_gmail_account_credential(
            gmail_account_id=row["id"],
            auth_kind=GMAIL_AUTH_KIND_OAUTH_ACCESS_TOKEN,
            credential_kind=credential_blob["credential_kind"],
            secret_manager_kind=secret_manager.kind,
            secret_ref=secret_ref,
            credential_blob=None,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise GmailAccountAlreadyExistsError(
            f"gmail account {request.provider_account_id} is already connected"
        ) from exc
    except GmailCredentialPersistenceError:
        raise
    except (ContinuityStoreInvariantError, psycopg.Error) as exc:
        if secret_ref is not None:
            try:
                secret_manager.delete_secret(secret_ref=secret_ref)
            except GmailSecretManagerError:
                pass
        raise GmailCredentialPersistenceError(
            "gmail protected credentials could not be persisted"
        ) from exc

    return {"account": serialize_gmail_account_row(row)}


def list_gmail_account_records(
    store: ContinuityStore,
    *,
    user_id: UUID,
) -> GmailAccountListResponse:
    del user_id

    items = [serialize_gmail_account_row(row) for row in store.list_gmail_accounts()]
    return {
        "items": items,
        "summary": {
            "total_count": len(items),
            "order": list(GMAIL_ACCOUNT_LIST_ORDER),
        },
    }


def get_gmail_account_record(
    store: ContinuityStore,
    *,
    user_id: UUID,
    gmail_account_id: UUID,
) -> GmailAccountDetailResponse:
    del user_id

    row = store.get_gmail_account_optional(gmail_account_id)
    if row is None:
        raise GmailAccountNotFoundError(f"gmail account {gmail_account_id} was not found")
    return {"account": serialize_gmail_account_row(row)}


def _sanitize_path_segment(value: str) -> str:
    sanitized = _PATH_SEGMENT_PATTERN.sub("_", value.strip())
    return sanitized.strip("._") or "message"


def build_gmail_message_artifact_relative_path(
    *,
    provider_account_id: str,
    provider_message_id: str,
) -> str:
    return (
        f"{GMAIL_MESSAGE_ARTIFACT_ROOT}/"
        f"{_sanitize_path_segment(provider_account_id)}/"
        f"{_sanitize_path_segment(provider_message_id)}.eml"
    )


def fetch_gmail_message_raw_bytes(*, access_token: str, provider_message_id: str) -> bytes:
    request = Request(
        (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
            f"{quote(provider_message_id, safe='')}?format=raw"
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
            timeout=GMAIL_MESSAGE_FETCH_TIMEOUT_SECONDS,
            enforce_public_peer=True,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise GmailMessageNotFoundError(
                f"gmail message {provider_message_id} was not found"
            ) from exc
        raise GmailMessageFetchError(
            f"gmail message {provider_message_id} could not be fetched"
        ) from exc
    except (OSError, URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailMessageFetchError(
            f"gmail message {provider_message_id} could not be fetched"
        ) from exc

    raw_payload = payload.get("raw")
    if not isinstance(raw_payload, str) or raw_payload == "":
        raise GmailMessageUnsupportedError(
            f"gmail message {provider_message_id} did not include RFC822 raw content"
        )

    padding = "=" * (-len(raw_payload) % 4)
    try:
        return base64.urlsafe_b64decode(raw_payload + padding)
    except (ValueError, TypeError) as exc:
        raise GmailMessageUnsupportedError(
            f"gmail message {provider_message_id} did not include valid RFC822 raw content"
        ) from exc


def ingest_gmail_message_record(
    store: ContinuityStore,
    secret_manager: GmailSecretManager,
    *,
    user_id: UUID,
    request: GmailMessageIngestInput,
) -> GmailMessageIngestionResponse:
    account = store.get_gmail_account_optional(request.gmail_account_id)
    if account is None:
        raise GmailAccountNotFoundError(f"gmail account {request.gmail_account_id} was not found")

    workspace = store.get_task_workspace_optional(request.task_workspace_id)
    if workspace is None:
        raise TaskWorkspaceNotFoundError(
            f"task workspace {request.task_workspace_id} was not found"
        )

    access_token = resolve_gmail_access_token(
        store,
        secret_manager,
        gmail_account_id=request.gmail_account_id,
    )

    store.lock_task_artifacts(workspace["id"])
    relative_path = build_gmail_message_artifact_relative_path(
        provider_account_id=account["provider_account_id"],
        provider_message_id=request.provider_message_id,
    )
    existing_artifact = store.get_task_artifact_by_workspace_relative_path_optional(
        task_workspace_id=request.task_workspace_id,
        relative_path=relative_path,
    )
    if existing_artifact is not None:
        raise TaskArtifactAlreadyExistsError(
            f"artifact {relative_path} is already registered for task workspace {request.task_workspace_id}"
        )

    raw_bytes = fetch_gmail_message_raw_bytes(
        access_token=access_token,
        provider_message_id=request.provider_message_id,
    )

    try:
        extract_artifact_text_from_bytes(
            relative_path=relative_path,
            payload=raw_bytes,
            media_type=SUPPORTED_RFC822_ARTIFACT_MEDIA_TYPE,
        )
    except TaskArtifactValidationError as exc:
        raise GmailMessageUnsupportedError(
            f"gmail message {request.provider_message_id} is not a supported RFC822 email"
        ) from exc

    workspace_path = Path(workspace["local_path"]).expanduser().resolve()
    artifact_path = (workspace_path / relative_path).resolve()
    ensure_artifact_path_is_rooted(
        workspace_path=workspace_path,
        artifact_path=artifact_path,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if artifact_path.exists():
        raise TaskArtifactValidationError(
            f"artifact path {artifact_path} already exists before Gmail ingestion registration"
        )
    artifact_path.write_bytes(raw_bytes)

    artifact_payload = register_task_artifact_record(
        store,
        user_id=user_id,
        request=TaskArtifactRegisterInput(
            task_workspace_id=request.task_workspace_id,
            local_path=str(artifact_path),
            media_type_hint=SUPPORTED_RFC822_ARTIFACT_MEDIA_TYPE,
        ),
    )
    ingestion_payload = ingest_task_artifact_record(
        store,
        user_id=user_id,
        request=TaskArtifactIngestInput(task_artifact_id=UUID(artifact_payload["artifact"]["id"])),
    )
    return {
        "account": serialize_gmail_account_row(account),
        "message": {
            "provider_message_id": request.provider_message_id,
            "artifact_relative_path": ingestion_payload["artifact"]["relative_path"],
            "media_type": SUPPORTED_RFC822_ARTIFACT_MEDIA_TYPE,
        },
        "artifact": ingestion_payload["artifact"],
        "summary": ingestion_payload["summary"],
    }
