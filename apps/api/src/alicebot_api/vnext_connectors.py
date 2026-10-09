from __future__ import annotations

from copy import deepcopy

from collections.abc import Iterator, Mapping, Sequence
import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime
import fnmatch
from hashlib import sha256
import hmac
import io
import itertools
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID, uuid4

from alicebot_api.connector_payloads import ConnectorPayloadValidationError, normalize_telegram_source_item
from alicebot_api.importer_paths import ContainedReadRefused, read_text_beneath
from alicebot_api.vnext_capture import (
    CaptureCredentialRefused,
    SourceCaptureInput,
    VNextCaptureService,
    VNextCaptureStore,
)
from alicebot_api.vnext_embeddings import DeferredMemoryEmbedding
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_project_scope import resolve_project_scope
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_secrets import (
    SecretProvider,
    default_secret_provider,
    redact_secret_fields,
    redact_secret_value,
)


if TYPE_CHECKING:
    from alicebot_api.vnext_label_guard import LabelGuard

# The payload fields of a connector event that hold a cursor. A cursor is the position of the last item imported, and for a
# file or a page it is the path or the address of that item.
CONNECTOR_EVENT_CURSOR_FIELDS: dict[str, tuple[str, ...]] = {
    "connector.state_updated": ("cursor_value",),
    "connector.sync_started": ("previous_cursor",),
    "connector.sync_completed": ("previous_cursor", "sync_cursor"),
    "connector.sync_failed": ("previous_cursor", "sync_cursor"),
}
CONNECTOR_ITEM_IMPORT_ERROR_CODE = "connector_item_import_failed"
CONNECTOR_ITEM_IMPORT_ERROR_MESSAGE = "Connector item could not be imported"
CONNECTOR_SYNC_ERROR_CODE = "connector_sync_failed"
CONNECTOR_SYNC_ERROR_MESSAGE = "Connector synchronization failed"
logger = logging.getLogger(__name__)


def _connector_public_error_message(error_code: str) -> str:
    if error_code == CONNECTOR_ITEM_IMPORT_ERROR_CODE:
        return CONNECTOR_ITEM_IMPORT_ERROR_MESSAGE
    return CONNECTOR_SYNC_ERROR_MESSAGE


class VNextConnectorValidationError(ValueError):
    """Raised when a connector payload cannot be normalized safely."""


class VNextConnectorStore(VNextCaptureStore, Protocol):
    def list_events(self, *, target_type: str | None = None, target_id: str | None = None) -> list[JsonObject]: ...

    def get_source(self, source_id: str) -> JsonObject | None: ...

    def create_artifact(self, artifact: JsonObject, *, actor_type: str = "system") -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    name: str
    display_name: str
    phase: str
    source_type: str
    default_domain: str
    default_sensitivity: str
    raw_evidence_kind: str
    cursor_field: str
    description: str

    def to_record(self) -> JsonObject:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "phase": self.phase,
            "source_type": self.source_type,
            "default_domain": self.default_domain,
            "default_sensitivity": self.default_sensitivity,
            "raw_evidence_kind": self.raw_evidence_kind,
            "cursor_field": self.cursor_field,
            "description": self.description,
            "supports_default_domain": True,
            "supports_default_sensitivity": True,
            "supports_sync_cursor": True,
            "preserves_raw_evidence": True,
        }


@dataclass(frozen=True, slots=True)
class NormalizedConnectorItem:
    connector_name: str
    source_type: str
    external_id: str
    cursor: str
    raw_text: str
    title: str
    author: str | None = None
    uri: str | None = None
    raw_path: str | None = None
    captured_at: str | None = None
    source_created_at: str | None = None
    source_modified_at: str | None = None
    metadata_json: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorSyncResult:
    status: str
    connector_name: str
    item_count: int
    imported_count: int
    duplicate_count: int
    skipped_count: int
    failed_count: int
    previous_cursor: str | None
    sync_cursor: str | None
    error_code: str | None = None
    source_ids: tuple[str, ...] = ()
    failed_external_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    # Internal two-phase handoff. Omitted from ``to_record`` so deferring the
    # provider call does not change the connector API contract.
    deferred_embedding_inputs: tuple[DeferredMemoryEmbedding, ...] = ()

    def to_record(self) -> JsonObject:
        return {
            "status": self.status,
            "connector_name": self.connector_name,
            "item_count": self.item_count,
            "imported_count": self.imported_count,
            "duplicate_count": self.duplicate_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "previous_cursor": self.previous_cursor,
            "sync_cursor": self.sync_cursor,
            "error_code": self.error_code,
            "source_ids": list(self.source_ids),
            "failed_external_ids": list(self.failed_external_ids),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class LocalFolderScan:
    items: tuple[JsonObject, ...]
    path_count: int
    ignored_count: int
    recursive: bool
    extensions: tuple[str, ...]
    # Files the contained reader refused: a link or a moved directory, a file
    # that is not a regular file, one over the size cap, one that is not UTF-8
    # text, or one that could not be read. Each fails alone.
    refused_count: int = 0
    # True when a limit (files, total bytes, or directory entries listed) stopped
    # the scan before it had read everything that matched.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class AgentOutputIngestResult:
    status: str
    source_id: str | None
    artifact_id: str | None
    memory_id: str | None
    policy_decision: JsonObject | None
    # Internal two-phase handoff; deliberately excluded from ``to_record``.
    deferred_embedding_inputs: tuple[DeferredMemoryEmbedding, ...] = ()

    def to_record(self) -> JsonObject:
        return {
            "status": self.status,
            "source_id": self.source_id,
            "artifact_id": self.artifact_id,
            "memory_id": self.memory_id,
            "policy_decision": self.policy_decision,
        }


SUPPORTED_CONNECTORS: tuple[ConnectorDefinition, ...] = (
    ConnectorDefinition(
        name="telegram",
        display_name="Telegram source import",
        phase="on_demand",
        source_type="telegram_message",
        default_domain="personal",
        default_sensitivity="private",
        raw_evidence_kind="telegram_source_json",
        cursor_field="provider_update_id",
        description="Imports operator-supplied Telegram payloads into allowlisted raw source evidence.",
    ),
    ConnectorDefinition(
        name="browser_clipper",
        display_name="Browser clipper",
        phase="live_capture",
        source_type="browser_clip",
        default_domain="professional",
        default_sensitivity="private",
        raw_evidence_kind="browser_clip_json",
        cursor_field="captured_at_or_external_id",
        description="Captures clipped page text, URL, selection, and optional HTML snapshots.",
    ),
    ConnectorDefinition(
        name="local_folder",
        display_name="Local folder watcher",
        phase="live_capture",
        source_type="local_file",
        default_domain="project",
        default_sensitivity="private",
        raw_evidence_kind="local_text_file",
        cursor_field="mtime_ns_path",
        description="Backfills and incrementally scans configured Markdown/text folders such as Obsidian vaults.",
    ),
    ConnectorDefinition(
        name="agent_output",
        display_name="Agent output ingestion",
        phase="live_capture",
        source_type="agent_output",
        default_domain="project",
        default_sensitivity="private",
        raw_evidence_kind="agent_output_json",
        cursor_field="agent_run_id_or_external_id",
        description="Captures Hermes/OpenClaw outputs as reviewable source evidence and optional proposals.",
    ),
    ConnectorDefinition(
        name="pdf_document",
        display_name="PDF processing",
        phase="phase_2",
        source_type="pdf_document",
        default_domain="unknown",
        default_sensitivity="private",
        raw_evidence_kind="extracted_pdf_text",
        cursor_field="source_modified_at_or_external_id",
        description="Archives extracted PDF text and file metadata as reviewable source evidence.",
    ),
    ConnectorDefinition(
        name="docx_document",
        display_name="DOCX processing",
        phase="phase_2",
        source_type="docx_document",
        default_domain="unknown",
        default_sensitivity="private",
        raw_evidence_kind="extracted_docx_text",
        cursor_field="source_modified_at_or_external_id",
        description="Archives extracted DOCX text and file metadata as reviewable source evidence.",
    ),
    ConnectorDefinition(
        name="csv_table",
        display_name="CSV processing",
        phase="phase_2",
        source_type="csv_table",
        default_domain="professional",
        default_sensitivity="private",
        raw_evidence_kind="csv_rows_or_text",
        cursor_field="source_modified_at_or_external_id",
        description="Normalizes CSV text or rows into deterministic source text with raw row evidence.",
    ),
    ConnectorDefinition(
        name="screenshot_ocr",
        display_name="Screenshot processing",
        phase="phase_2",
        source_type="screenshot_ocr",
        default_domain="unknown",
        default_sensitivity="private",
        raw_evidence_kind="ocr_text",
        cursor_field="captured_at_or_external_id",
        description="Archives OCR output from screenshots with conservative privacy defaults.",
    ),
    ConnectorDefinition(
        name="voice_transcription",
        display_name="Voice transcription",
        phase="phase_2",
        source_type="voice_transcript",
        default_domain="personal",
        default_sensitivity="private",
        raw_evidence_kind="transcript_segments",
        cursor_field="recorded_at_or_external_id",
        description="Captures transcript text and segment metadata from a configured transcription pipeline.",
    ),
)

DEFAULT_LOCAL_FOLDER_IGNORES = (
    ".alice",
    ".cache",
    ".git",
    ".venv",
    "__pycache__",
    "generated",
    "alice export",
    "07 generated",
    "08 queue",
    "node_modules",
)
DEFAULT_LOCAL_FOLDER_EXTENSIONS = (".md", ".txt")
# Bounds on one scan. A file over the per-file cap is refused. The scan stops at
# the file count or the total byte cap, and it lists at most MAX_LOCAL_FOLDER_LISTED
# directory entries before it sorts them. Measured on v0.19.0: a 1 MiB file cost
# 1.2 seconds and about 90 MiB, and a 64 MiB file 77 seconds and 1.5 GiB.
MAX_LOCAL_FOLDER_FILE_BYTES = 2 * 1024 * 1024
MAX_LOCAL_FOLDER_FILES = 10_000
MAX_LOCAL_FOLDER_TOTAL_BYTES = 64 * 1024 * 1024
MAX_LOCAL_FOLDER_LISTED = 100_000
LOCAL_FOLDER_ROOTS_ENV = "ALICE_VNEXT_LOCAL_FOLDER_ROOTS"
CORE_SETTINGS_CONNECTORS = ("telegram", "local_folder", "browser_clipper", "agent_output")

_CONNECTOR_BY_NAME = {definition.name: definition for definition in SUPPORTED_CONNECTORS}
_VALID_DOMAINS = frozenset(
    {
        "professional",
        "personal",
        "family",
        "health",
        "spiritual",
        "financial",
        "legal",
        "learning",
        "relationship",
        "project",
        "agent_run",
        "system",
        "unknown",
    }
)
_VALID_SENSITIVITIES = frozenset(
    {
        "public",
        "internal",
        "private",
        "confidential",
        "highly_sensitive",
        "sacred",
        "regulated",
        "unknown",
    }
)


def list_connector_definitions() -> tuple[ConnectorDefinition, ...]:
    return SUPPORTED_CONNECTORS


def get_connector_definition(connector_name: str) -> ConnectorDefinition:
    normalized = connector_name.strip().casefold()
    definition = _CONNECTOR_BY_NAME.get(normalized)
    if definition is None:
        raise VNextConnectorValidationError(f"unknown vNext connector: {connector_name}")
    return definition


def _as_json_object(value: object, *, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise VNextConnectorValidationError(f"{label} must be a JSON object")
    return cast(JsonObject, value)


def _as_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _telegram_source_config(value: object) -> JsonObject:
    """Keep only the allowlist used by operator-supplied Telegram source imports."""

    if not isinstance(value, dict):
        return {}
    allowed_chat_ids = value.get("allowed_chat_ids")
    if not isinstance(allowed_chat_ids, list):
        return {}
    return {"allowed_chat_ids": list(allowed_chat_ids)}


def _required_text(payload: Mapping[str, object], keys: Sequence[str], *, connector_name: str) -> str:
    for key in keys:
        value = _as_optional_text(payload.get(key))
        if value is not None:
            return value
    joined = ", ".join(keys)
    raise VNextConnectorValidationError(f"{connector_name} item requires one of: {joined}")


def _first_text(payload: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = _as_optional_text(payload.get(key))
        if value is not None:
            return value
    return None


def _optional_iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _external_id(payload: Mapping[str, object], *, fallback: str) -> str:
    value = _first_text(payload, ("external_id", "id", "message_id", "filename", "path", "url"))
    return value or fallback


def _cursor_value(payload: Mapping[str, object], *, external_id: str, keys: Sequence[str]) -> str:
    value = _first_text(payload, keys)
    return value or external_id


def _cursor_lte(left: str, right: str | None) -> bool:
    if right is None:
        return False
    if left.isdecimal() and right.isdecimal():
        return int(left) <= int(right)
    return left <= right


def _cursor_sort_key(cursor: str) -> tuple[int, int | str]:
    if cursor.isdecimal():
        return (0, int(cursor))
    return (1, cursor)


def _int_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _stable_external_id(*parts: str) -> str:
    joined = "|".join(parts)
    return "sha256:" + sha256(joined.encode("utf-8")).hexdigest()


def _csv_rows_to_text(rows: object) -> str | None:
    if not isinstance(rows, list) or len(rows) == 0:
        return None

    output = io.StringIO()
    first_row = rows[0]
    if isinstance(first_row, dict):
        fieldnames: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                raise VNextConnectorValidationError("csv_table rows must all be objects or all be arrays")
            for key in row:
                if isinstance(key, str) and key not in fieldnames:
                    fieldnames.append(key)
        dict_writer = csv.DictWriter(output, fieldnames=fieldnames)
        dict_writer.writeheader()
        for row in rows:
            dict_writer.writerow({key: cast(Mapping[str, object], row).get(key, "") for key in fieldnames})
        return output.getvalue().strip()

    list_writer = csv.writer(output)
    for row in rows:
        if not isinstance(row, list):
            raise VNextConnectorValidationError("csv_table rows must all be objects or all be arrays")
        list_writer.writerow(row)
    return output.getvalue().strip()


def _telegram_attachment_metadata(payload: Mapping[str, object]) -> list[JsonObject]:
    message = payload.get("message")
    if not isinstance(message, Mapping):
        return []
    attachments: list[JsonObject] = []
    for key in ("photo", "document", "voice", "audio", "video"):
        value = message.get(key)
        if value is None:
            continue
        if key == "photo" and isinstance(value, list):
            attachments.append({"type": key, "count": len(value)})
        elif isinstance(value, Mapping):
            metadata: JsonObject = {"type": key}
            for field_name in ("file_id", "file_unique_id", "file_name", "mime_type", "duration", "file_size"):
                field_value = value.get(field_name)
                if isinstance(field_value, (str, int, float, bool)) or field_value is None:
                    metadata[field_name] = field_value
            attachments.append(metadata)
    return attachments


def _telegram_chat_id(payload: Mapping[str, object]) -> str | None:
    for key in ("message", "edited_message", "channel_post"):
        message = payload.get(key)
        if not isinstance(message, Mapping):
            continue
        chat = message.get("chat")
        if isinstance(chat, Mapping):
            chat_id = chat.get("id")
            if isinstance(chat_id, (str, int)):
                return str(chat_id)
    return None


def _telegram_update_id(payload: Mapping[str, object]) -> str | None:
    value = payload.get("update_id") or payload.get("id")
    if isinstance(value, (str, int)):
        normalized = str(value)
        return normalized if normalized else None
    return None


def _is_ignored_local_file(relative_path: Path, *, ignore_patterns: Sequence[str]) -> bool:
    normalized_parts = tuple(part.casefold() for part in relative_path.parts)
    if any(part in DEFAULT_LOCAL_FOLDER_IGNORES for part in normalized_parts):
        return True
    normalized_path = str(relative_path).replace("\\", "/")
    for pattern in ignore_patterns:
        cleaned = pattern.strip()
        if cleaned and fnmatch.fnmatch(normalized_path, cleaned):
            return True
    return False


def _path_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _allowed_local_folder_roots() -> tuple[Path, ...]:
    configured = os.environ.get(LOCAL_FOLDER_ROOTS_ENV)
    if configured:
        candidates = [Path(value).expanduser() for value in configured.split(os.pathsep) if value.strip()]
    else:
        candidates = [Path.home(), Path.cwd(), Path(tempfile.gettempdir())]

    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _relative_parts_under_allowed_root(raw_root: str | Path, allowed_root: Path) -> tuple[str, ...] | None:
    raw_text = os.fspath(raw_root).strip()
    if not raw_text or "\x00" in raw_text:
        return None
    expanded = os.path.realpath(os.path.abspath(os.path.expanduser(raw_text)))
    allowed_text = str(allowed_root)
    if expanded == allowed_text:
        return ()
    prefix = allowed_text + os.sep
    if not expanded.startswith(prefix):
        return None

    parts: list[str] = []
    for raw_part in expanded[len(prefix) :].split(os.sep):
        if raw_part in {"", ".", ".."}:
            return None
        safe_part = os.path.basename(raw_part)
        if safe_part != raw_part:
            return None
        parts.append(safe_part)
    return tuple(parts)


def _resolve_local_folder_root(raw_root: str | Path) -> Path:
    allowed_roots = _allowed_local_folder_roots()
    for allowed_root in allowed_roots:
        relative_parts = _relative_parts_under_allowed_root(raw_root, allowed_root)
        if relative_parts is None:
            continue
        root = allowed_root.joinpath(*relative_parts).resolve(strict=True)
        if not _path_within(root, allowed_root) or not root.is_dir():
            break
        return root

    allowed = ", ".join(str(allowed_root) for allowed_root in allowed_roots)
    raise VNextConnectorValidationError(
        f"local_folder watched path must be an existing directory under an allowed root; set {LOCAL_FOLDER_ROOTS_ENV} "
        f"to override. Allowed roots: {allowed}"
    )


def _agent_artifact_type(output_type: str | None) -> str:
    if output_type == "research_summary":
        return "research_brief"
    if output_type == "project_update":
        return "project_update"
    if output_type in {"sprint_summary", "code_review", "decision", "generated_plan", "meeting_summary"}:
        return "system_report"
    return "system_report"


def _normalize_telegram_item(payload: JsonObject) -> NormalizedConnectorItem:
    try:
        normalized = normalize_telegram_source_item(cast(dict[str, Any], payload))
    except ConnectorPayloadValidationError as exc:
        raise VNextConnectorValidationError(str(exc)) from exc
    text = normalized["message_text"].strip()
    if text == "":
        raise VNextConnectorValidationError("telegram item requires non-empty message text")
    external_id = f"{normalized['external_chat_id']}:{normalized['provider_message_id']}"
    cursor = normalized["provider_update_id"]
    username = normalized.get("external_username")
    author = f"@{username}" if username else normalized["external_user_id"]
    return NormalizedConnectorItem(
        connector_name="telegram",
        source_type="telegram_message",
        external_id=external_id,
        cursor=cursor,
        raw_text=text,
        title=f"Telegram capture - {str(normalized['sent_at']).replace('T', ' ')[:16]}",
        author=author,
        source_created_at=_optional_iso(normalized["sent_at"]),
        metadata_json={
            "raw_payload": payload,
            "normalized_payload": cast(JsonObject, normalized["normalized_payload"]),
            "provider_update_id": normalized["provider_update_id"],
            "provider_message_id": normalized["provider_message_id"],
            "external_chat_id": normalized["external_chat_id"],
            "idempotency_key": normalized["idempotency_key"],
            "chat_id": normalized["external_chat_id"],
            "message_id": normalized["provider_message_id"],
            "sender_id": normalized["external_user_id"],
            "sender_username": username,
            "message_date": _optional_iso(normalized["sent_at"]),
            "contains_links": "http://" in text.casefold() or "https://" in text.casefold(),
            "attachment_metadata": _telegram_attachment_metadata(payload),
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _normalize_browser_clip_item(payload: JsonObject) -> NormalizedConnectorItem:
    selected_text = _first_text(payload, ("selected_text", "selection", "excerpt"))
    user_note = _first_text(payload, ("user_note", "note"))
    page_text = _first_text(payload, ("page_text", "text", "markdown"))
    parts = [
        f"Selected text:\n{selected_text}" if selected_text else "",
        f"User note:\n{user_note}" if user_note else "",
        f"Page text:\n{page_text}" if page_text else "",
    ]
    text = "\n\n".join(part for part in parts if part)
    if text.strip() == "":
        raise VNextConnectorValidationError(
            "browser_clipper item requires selected_text, user_note, page_text, or text"
        )
    url = _as_optional_text(payload.get("url"))
    title = _first_text(payload, ("title", "page_title")) or "Browser clip"
    external_id = _external_id(payload, fallback=url or title)
    captured_at = _optional_iso(payload.get("captured_at"))
    cursor = _cursor_value(payload, external_id=external_id, keys=("cursor", "captured_at", "source_modified_at"))
    return NormalizedConnectorItem(
        connector_name="browser_clipper",
        source_type="browser_clip",
        external_id=external_id,
        cursor=cursor,
        raw_text=text,
        title=title,
        uri=url,
        captured_at=captured_at,
        source_created_at=captured_at,
        metadata_json={
            "raw_payload": payload,
            "url": url,
            "title": title,
            "selected_text_present": selected_text is not None,
            "user_note_present": user_note is not None,
            "captured_from_browser": True,
            "selection": selected_text,
            "html": _as_optional_text(payload.get("html")),
            # Promote clipper-provided project scope to the top level so
            # capture_source (which reads metadata_json["project_scope"])
            # actually applies it, instead of it being lost inside raw_payload
            # (audit 2 P1 #2).
            "project_scope": payload.get("project_scope") if isinstance(payload.get("project_scope"), list) else [],
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _normalize_local_folder_item(payload: JsonObject) -> NormalizedConnectorItem:
    text = _required_text(payload, ("text", "content"), connector_name="local_folder")
    path = _required_text(payload, ("path",), connector_name="local_folder")
    title = _first_text(payload, ("title", "filename")) or Path(path).name
    external_id = _external_id(payload, fallback=path)
    mtime_ns = payload.get("mtime_ns")
    cursor = _cursor_value(
        payload,
        external_id=external_id,
        keys=("cursor", "mtime_ns", "source_modified_at", "mtime"),
    )
    if isinstance(mtime_ns, int):
        cursor = f"{mtime_ns}:{external_id}"
    return NormalizedConnectorItem(
        connector_name="local_folder",
        source_type="local_file",
        external_id=external_id,
        cursor=cursor,
        raw_text=text,
        title=title,
        raw_path=path,
        source_modified_at=_optional_iso(payload.get("source_modified_at") or payload.get("mtime")),
        metadata_json={
            "raw_payload": payload,
            "path": path,
            "relative_path": _as_optional_text(payload.get("relative_path")),
            "file_size": payload.get("file_size") if isinstance(payload.get("file_size"), int) else None,
            "mtime": payload.get("mtime"),
            "extension": _as_optional_text(payload.get("extension")),
            "watched_root": _as_optional_text(payload.get("watched_root")),
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _normalize_agent_output_item(payload: JsonObject) -> NormalizedConnectorItem:
    content = _required_text(payload, ("content", "text", "summary"), connector_name="agent_output")
    title = _first_text(payload, ("title",)) or "Agent output"
    agent_id = _required_text(payload, ("agent_id",), connector_name="agent_output")
    external_id = _external_id(
        payload,
        fallback=_stable_external_id(agent_id, _first_text(payload, ("agent_run_id", "task_id")) or title, content),
    )
    captured_at = _optional_iso(payload.get("captured_at")) or _utc_now_iso()
    cursor = _cursor_value(payload, external_id=external_id, keys=("cursor", "agent_run_id", "captured_at"))
    return NormalizedConnectorItem(
        connector_name="agent_output",
        source_type="agent_output",
        external_id=external_id,
        cursor=cursor,
        raw_text=content,
        title=title,
        author=agent_id,
        captured_at=captured_at,
        source_created_at=captured_at,
        metadata_json={
            "raw_payload": payload,
            "agent_id": agent_id,
            "agent_type": _as_optional_text(payload.get("agent_type")),
            "agent_run_id": _as_optional_text(payload.get("agent_run_id")),
            "task_id": _as_optional_text(payload.get("task_id")),
            "project_scope": payload.get("project_scope") if isinstance(payload.get("project_scope"), list) else [],
            "output_type": _as_optional_text(payload.get("output_type")) or "general",
            "rationale": _as_optional_text(payload.get("rationale")),
            "source_refs": payload.get("source_refs") if isinstance(payload.get("source_refs"), list) else [],
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _normalize_document_item(connector_name: str, source_type: str, payload: JsonObject) -> NormalizedConnectorItem:
    text = _required_text(payload, ("text", "extracted_text", "content"), connector_name=connector_name)
    filename = _first_text(payload, ("filename", "name", "path")) or f"{connector_name} item"
    external_id = _external_id(payload, fallback=filename)
    cursor = _cursor_value(
        payload,
        external_id=external_id,
        keys=("cursor", "source_modified_at", "modified_at", "source_created_at", "created_at"),
    )
    return NormalizedConnectorItem(
        connector_name=connector_name,
        source_type=source_type,
        external_id=external_id,
        cursor=cursor,
        raw_text=text,
        title=filename,
        raw_path=_as_optional_text(payload.get("path")),
        source_created_at=_optional_iso(payload.get("source_created_at") or payload.get("created_at")),
        source_modified_at=_optional_iso(payload.get("source_modified_at") or payload.get("modified_at")),
        metadata_json={
            "raw_payload": payload,
            "filename": filename,
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _normalize_csv_item(payload: JsonObject) -> NormalizedConnectorItem:
    csv_text = _first_text(payload, ("csv_text", "text", "content"))
    if csv_text is None:
        csv_text = _csv_rows_to_text(payload.get("rows"))
    if csv_text is None or csv_text.strip() == "":
        raise VNextConnectorValidationError("csv_table item requires csv_text or non-empty rows")

    filename = _first_text(payload, ("filename", "name", "path")) or "CSV table"
    external_id = _external_id(payload, fallback=filename)
    cursor = _cursor_value(
        payload,
        external_id=external_id,
        keys=("cursor", "source_modified_at", "modified_at", "source_created_at", "created_at"),
    )
    return NormalizedConnectorItem(
        connector_name="csv_table",
        source_type="csv_table",
        external_id=external_id,
        cursor=cursor,
        raw_text=csv_text,
        title=filename,
        raw_path=_as_optional_text(payload.get("path")),
        source_created_at=_optional_iso(payload.get("source_created_at") or payload.get("created_at")),
        source_modified_at=_optional_iso(payload.get("source_modified_at") or payload.get("modified_at")),
        metadata_json={
            "raw_payload": payload,
            "row_count": len(cast(list[object], payload.get("rows")))
            if isinstance(payload.get("rows"), list)
            else None,
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _normalize_screenshot_item(payload: JsonObject) -> NormalizedConnectorItem:
    text = _required_text(payload, ("ocr_text", "text", "extracted_text"), connector_name="screenshot_ocr")
    filename = _first_text(payload, ("filename", "name", "path")) or "Screenshot OCR"
    external_id = _external_id(payload, fallback=filename)
    captured_at = _optional_iso(payload.get("captured_at") or payload.get("source_created_at"))
    cursor = _cursor_value(payload, external_id=external_id, keys=("cursor", "captured_at", "source_created_at"))
    return NormalizedConnectorItem(
        connector_name="screenshot_ocr",
        source_type="screenshot_ocr",
        external_id=external_id,
        cursor=cursor,
        raw_text=text,
        title=filename,
        raw_path=_as_optional_text(payload.get("path")),
        source_created_at=captured_at,
        metadata_json={
            "raw_payload": payload,
            "image_hash": _as_optional_text(payload.get("image_hash")),
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def _transcript_segments_to_text(segments: object) -> str | None:
    if not isinstance(segments, list):
        return None
    lines: list[str] = []
    for segment in segments:
        if isinstance(segment, str):
            stripped = segment.strip()
            if stripped:
                lines.append(stripped)
            continue
        if isinstance(segment, dict):
            text = _as_optional_text(segment.get("text"))
            if text is None:
                continue
            speaker = _as_optional_text(segment.get("speaker"))
            prefix = f"{speaker}: " if speaker else ""
            lines.append(f"{prefix}{text}")
    return "\n".join(lines) if lines else None


def _normalize_voice_item(payload: JsonObject) -> NormalizedConnectorItem:
    transcript = _first_text(payload, ("transcript", "text", "content"))
    if transcript is None:
        transcript = _transcript_segments_to_text(payload.get("segments"))
    if transcript is None or transcript.strip() == "":
        raise VNextConnectorValidationError("voice_transcription item requires transcript text or segments")

    title = _first_text(payload, ("title", "filename", "name", "path")) or "Voice transcript"
    external_id = _external_id(payload, fallback=title)
    recorded_at = _optional_iso(payload.get("recorded_at") or payload.get("source_created_at"))
    cursor = _cursor_value(payload, external_id=external_id, keys=("cursor", "recorded_at", "source_created_at"))
    return NormalizedConnectorItem(
        connector_name="voice_transcription",
        source_type="voice_transcript",
        external_id=external_id,
        cursor=cursor,
        raw_text=transcript,
        title=title,
        raw_path=_as_optional_text(payload.get("path")),
        source_created_at=recorded_at,
        metadata_json={
            "raw_payload": payload,
            "segments": payload.get("segments") if isinstance(payload.get("segments"), list) else None,
            "transcription_provider": _as_optional_text(payload.get("transcription_provider")),
            "untrusted_source_material": True,
            "raw_evidence_preserved": True,
        },
    )


def normalize_connector_item(connector_name: str, payload: Mapping[str, object]) -> NormalizedConnectorItem:
    definition = get_connector_definition(connector_name)
    item = _as_json_object(cast(JsonObject, redact_secret_fields(dict(payload))), label=f"{definition.name} item")
    if definition.name == "telegram":
        return _normalize_telegram_item(item)
    if definition.name == "browser_clipper":
        return _normalize_browser_clip_item(item)
    if definition.name == "local_folder":
        return _normalize_local_folder_item(item)
    if definition.name == "agent_output":
        return _normalize_agent_output_item(item)
    if definition.name == "pdf_document":
        return _normalize_document_item("pdf_document", "pdf_document", item)
    if definition.name == "docx_document":
        return _normalize_document_item("docx_document", "docx_document", item)
    if definition.name == "csv_table":
        return _normalize_csv_item(item)
    if definition.name == "screenshot_ocr":
        return _normalize_screenshot_item(item)
    if definition.name == "voice_transcription":
        return _normalize_voice_item(item)
    raise VNextConnectorValidationError(f"unknown vNext connector: {connector_name}")


def load_connector_items_from_file(path: str | Path) -> list[JsonObject]:
    payload_path = Path(path).expanduser().resolve()
    if payload_path.suffix.casefold() == ".csv":
        return [
            {
                "filename": payload_path.name,
                "path": str(payload_path),
                "external_id": str(payload_path),
                "source_modified_at": datetime.fromtimestamp(payload_path.stat().st_mtime).isoformat(),
                "csv_text": payload_path.read_text(encoding="utf-8"),
            }
        ]

    loaded = json.loads(payload_path.read_text(encoding="utf-8"))
    if isinstance(loaded, list):
        values = loaded
    elif isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
        values = cast(list[object], loaded["items"])
    elif isinstance(loaded, dict):
        values = [loaded]
    else:
        raise VNextConnectorValidationError("connector payload file must contain an object, item array, or items array")

    items: list[JsonObject] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise VNextConnectorValidationError(f"connector payload item {index} must be an object")
        items.append(cast(JsonObject, value))
    return items


def _walk_local_folder(root: Path, *, recursive: bool) -> Iterator[Path]:
    """Every entry under ``root`` the scan could consider, without entering an ignored folder.

    A folder whose name is one of ``DEFAULT_LOCAL_FOLDER_IGNORES`` (``node_modules``,
    ``.git`` and the rest, compared without regard to case) is not entered. Nothing in it
    could be imported, so it costs no time and its entries do not count toward
    ``MAX_LOCAL_FOLDER_LISTED``: a watched folder with a large ``node_modules`` is not cut
    short before its notes are reached. A link to a folder is listed and not followed.
    Entries come in the order the file system gives them, and the caller sorts them.
    """

    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = [
            name for name in directory_names if name.casefold() not in DEFAULT_LOCAL_FOLDER_IGNORES
        ]
        base = Path(directory)
        for name in (*directory_names, *file_names):
            yield base / name
        if not recursive:
            return


def _universal_newlines(text: str) -> str:
    """Translate line endings the way ``Path.read_text`` does, so a note's text and hash do not change."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def scan_local_folder(
    paths: Sequence[str | Path],
    *,
    recursive: bool = True,
    extensions: Sequence[str] = DEFAULT_LOCAL_FOLDER_EXTENSIONS,
    ignore_patterns: Sequence[str] = (),
) -> LocalFolderScan:
    """Read watched files without requiring or retaining a database connection."""

    normalized_extensions = tuple(extension.casefold() for extension in extensions)
    if not normalized_extensions:
        raise VNextConnectorValidationError("local_folder requires at least one file extension")
    items: list[JsonObject] = []
    ignored_count = 0
    refused_count = 0
    total_bytes = 0
    truncated = False
    for raw_root in paths:
        root = _resolve_local_folder_root(raw_root)
        walk = _walk_local_folder(root, recursive=recursive)
        # Take one entry past the cap to learn whether the walk had more, and
        # stop the walk there: sorting an unbounded walk materializes all of it.
        listed = list(itertools.islice(walk, MAX_LOCAL_FOLDER_LISTED + 1))
        if len(listed) > MAX_LOCAL_FOLDER_LISTED:
            truncated = True
            listed = listed[:MAX_LOCAL_FOLDER_LISTED]
        for file_path in sorted(listed):
            try:
                resolved_file = file_path.resolve(strict=True)
            except OSError:
                continue
            if not resolved_file.is_file() or not _path_within(resolved_file, root):
                continue
            if resolved_file.suffix.casefold() not in normalized_extensions:
                continue
            relative_path = resolved_file.relative_to(root)
            if _is_ignored_local_file(
                relative_path,
                ignore_patterns=ignore_patterns,
            ):
                ignored_count += 1
                continue
            if len(items) >= MAX_LOCAL_FOLDER_FILES or total_bytes >= MAX_LOCAL_FOLDER_TOTAL_BYTES:
                truncated = True
                break
            # The checks above name a file; they do not hold it. The read opens
            # it again beneath the root, link by link, and takes the text, the
            # size and the time from that one descriptor. It may take no more
            # than the per-file cap or what is left of the total.
            allowance = min(MAX_LOCAL_FOLDER_FILE_BYTES, MAX_LOCAL_FOLDER_TOTAL_BYTES - total_bytes)
            try:
                text, opened = read_text_beneath(root, relative_path, max_bytes=allowance)
            except ContainedReadRefused as refusal:
                if refusal.reason == "too_large" and allowance < MAX_LOCAL_FOLDER_FILE_BYTES:
                    # What is left of the total is smaller than the file. That is the
                    # total cap, not a file the scan refuses on its own account.
                    truncated = True
                    break
                refused_count += 1
                continue
            # Count the bytes that were read, so a size the descriptor misreports cannot
            # keep the total under its cap.
            total_bytes += max(opened.st_size, len(text.encode("utf-8")))
            items.append(
                {
                    "path": str(resolved_file),
                    "relative_path": str(relative_path),
                    "filename": resolved_file.name,
                    "text": _universal_newlines(text),
                    "file_size": opened.st_size,
                    "mtime": datetime.fromtimestamp(opened.st_mtime, UTC).isoformat().replace("+00:00", "Z"),
                    "mtime_ns": opened.st_mtime_ns,
                    "extension": resolved_file.suffix.casefold(),
                    "watched_root": str(root),
                    "external_id": str(resolved_file),
                }
            )
    return LocalFolderScan(
        items=tuple(items),
        path_count=len(paths),
        ignored_count=ignored_count,
        recursive=recursive,
        extensions=normalized_extensions,
        refused_count=refused_count,
        truncated=truncated,
    )


class VNextConnectorService:
    def __init__(
        self,
        store: VNextConnectorStore,
        *,
        secret_provider: SecretProvider | None = None,
        defer_embeddings: bool = False,
    ) -> None:
        self.store = store
        self.defer_embeddings = defer_embeddings
        self.capture_service = VNextCaptureService(store, defer_embeddings=defer_embeddings)
        self.secret_provider = secret_provider or default_secret_provider()

    def list_settings(self) -> JsonObject:
        if hasattr(self.store, "list_connector_settings"):
            rows = cast(Any, self.store).list_connector_settings()
            return {
                "items": [self._setting_row_to_config(row) for row in rows],
                "count": len(rows),
                "order": [str(row.get("connector_name")) for row in rows],
            }
        return {
            "items": [definition.to_record() for definition in list_connector_definitions()],
            "count": len(SUPPORTED_CONNECTORS),
            "order": [definition.name for definition in SUPPORTED_CONNECTORS],
        }

    def ensure_default_settings(self) -> JsonObject:
        if not hasattr(self.store, "get_connector_setting") or not hasattr(self.store, "upsert_connector_setting"):
            return {"status": "skipped", "reason": "connector settings storage is unavailable", "created": []}
        created: list[str] = []
        for definition in SUPPORTED_CONNECTORS:
            if definition.name not in CORE_SETTINGS_CONNECTORS:
                continue
            existing = cast(Any, self.store).get_connector_setting(definition.name)
            if existing is None:
                cast(Any, self.store).upsert_connector_setting(
                    self._default_setting_payload(definition.name),
                    actor_type="system",
                )
                created.append(definition.name)
            if hasattr(self.store, "get_connector_state") and hasattr(self.store, "upsert_connector_state"):
                state = cast(Any, self.store).get_connector_state(definition.name)
                if state is None:
                    cast(Any, self.store).upsert_connector_state(
                        {
                            "connector_name": definition.name,
                            "cursor_type": "sync_cursor",
                            "state_json": {"initialized_by": "ensure_default_settings"},
                        },
                        actor_type="system",
                    )
        return {"status": "ok", "created": created}

    def _default_setting_payload(self, connector_name: str) -> JsonObject:
        definition = get_connector_definition(connector_name)
        sync_mode = (
            "watch"
            if definition.name == "local_folder"
            else "on_demand"
        )
        interval = 30 if definition.name == "local_folder" else None
        return {
            "connector_name": definition.name,
            "enabled": False,
            "configured": False,
            "default_domain": definition.default_domain,
            "default_sensitivity": definition.default_sensitivity,
            "sync_mode": sync_mode,
            "poll_interval_seconds": interval,
            "secret_ref": None,
            "validation_errors_json": [],
            "metadata_json": {"config_json": {}, "initialized_by": "connector_service"},
        }

    def _setting_row_to_config(self, row: Mapping[str, object]) -> JsonObject:
        definition = get_connector_definition(str(row.get("connector_name")))
        metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
        config_json = cast(dict[str, object], metadata).get("config_json") if isinstance(metadata, dict) else {}
        if not isinstance(config_json, dict):
            config_json = {}
        if definition.name == "telegram":
            config_json = _telegram_source_config(config_json)
        validation_errors = row.get("validation_errors_json")
        if not isinstance(validation_errors, list):
            validation_errors = []
        secret_ref = None if definition.name == "telegram" else _as_optional_text(row.get("secret_ref"))
        return {
            "connector_id": str(row.get("id")) if row.get("id") is not None else None,
            "connector_name": definition.name,
            "enabled": bool(row.get("enabled")),
            "configured": bool(row.get("configured")),
            "secret_ref": secret_ref,
            "secret_configured": bool(secret_ref),
            "default_domain": row.get("default_domain") or definition.default_domain,
            "default_sensitivity": row.get("default_sensitivity") or definition.default_sensitivity,
            "sync_mode": "on_demand" if definition.name == "telegram" else row.get("sync_mode") or "manual",
            "poll_interval_seconds": None if definition.name == "telegram" else row.get("poll_interval_seconds"),
            "config_json": cast(JsonObject, config_json),
            "validation_errors": validation_errors,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "last_configured_at": row.get("last_configured_at"),
        }

    def get_cursor(self, connector_name: str) -> str | None:
        definition = get_connector_definition(connector_name)
        state = self._connector_row("states", "get_connector_state", definition.name)
        if isinstance(state, dict):
            cursor = _as_optional_text(state.get("cursor_value"))
            if cursor is not None:
                return cursor
        events = [
            event
            for event in self._connector_events(definition.name)
            if event.get("event_type") == "connector.sync_completed"
        ]
        events.sort(key=lambda event: str(event.get("occurred_at") or ""), reverse=True)
        for event in events:
            payload = event.get("payload_json")
            if not isinstance(payload, dict):
                continue
            cursor = _as_optional_text(payload.get("sync_cursor"))
            if cursor is not None:
                return cursor
        return None

    def _log_event(
        self,
        *,
        event_type: str,
        connector_name: str,
        payload: JsonObject,
    ) -> JsonObject:
        return append_event(
            self.store,
            event_type=event_type,
            actor_type="system",
            target_type="connector",
            target_id=connector_name,
            payload=payload,
        )

    def update_config(
        self,
        connector_name: str,
        *,
        enabled: bool | None = None,
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
        secret_ref: str | None = None,
        sync_mode: str | None = None,
        poll_interval_seconds: int | None = None,
        config_json: JsonObject | None = None,
    ) -> JsonObject:
        definition = get_connector_definition(connector_name)
        if definition.name == "telegram" and (
            secret_ref is not None
            or poll_interval_seconds is not None
            or sync_mode not in {None, "on_demand"}
        ):
            raise VNextConnectorValidationError(
                "telegram source ingestion is on-demand and does not accept polling or secret configuration"
            )
        existing_config = self.get_config(definition.name)
        existing_config_json = (
            existing_config.get("config_json") if isinstance(existing_config.get("config_json"), dict) else {}
        )
        domain = default_domain or _as_optional_text(existing_config.get("default_domain")) or definition.default_domain
        sensitivity = (
            default_sensitivity
            or _as_optional_text(existing_config.get("default_sensitivity"))
            or definition.default_sensitivity
        )
        if domain not in _VALID_DOMAINS:
            raise VNextConnectorValidationError(f"invalid connector default domain: {domain}")
        if sensitivity not in _VALID_SENSITIVITIES:
            raise VNextConnectorValidationError(f"invalid connector default sensitivity: {sensitivity}")
        incoming_config = cast(JsonObject, redact_secret_fields(config_json or {}))
        sanitized_config = cast(JsonObject, {**cast(dict[str, object], existing_config_json), **incoming_config})
        if definition.name == "telegram":
            sanitized_config = _telegram_source_config(sanitized_config)
        existing_sync_mode = _as_optional_text(existing_config.get("sync_mode"))
        normalized_sync_mode = (
            sync_mode
            or existing_sync_mode
            or (
                "watch"
                if definition.name == "local_folder"
                else "on_demand"
            )
        )
        resolved_secret_ref = (
            secret_ref if secret_ref is not None else _as_optional_text(existing_config.get("secret_ref"))
        )
        resolved_poll_interval = (
            poll_interval_seconds if poll_interval_seconds is not None else existing_config.get("poll_interval_seconds")
        )
        if definition.name == "telegram":
            normalized_sync_mode = "on_demand"
            resolved_secret_ref = None
            resolved_poll_interval = None
        validation_errors: list[str] = []
        if definition.name == "telegram":
            allowed = sanitized_config.get("allowed_chat_ids")
            if not isinstance(allowed, list) or not [
                item for item in allowed if isinstance(item, (str, int)) and str(item).strip()
            ]:
                validation_errors.append("allowed_chat_ids_required")
        if definition.name == "local_folder":
            paths = sanitized_config.get("paths")
            if enabled and (not isinstance(paths, list) or len(paths) == 0):
                validation_errors.append("watched_path_required")
        config = {
            "connector_name": definition.name,
            "enabled": bool(enabled) if enabled is not None else bool(existing_config.get("enabled")),
            "configured": True,
            "secret_ref": resolved_secret_ref,
            "secret_configured": bool(resolved_secret_ref),
            "default_domain": domain,
            "default_sensitivity": sensitivity,
            "sync_mode": normalized_sync_mode,
            "poll_interval_seconds": resolved_poll_interval,
            "config_json": sanitized_config,
            "validation_errors": validation_errors,
            "updated_at": _utc_now_iso(),
        }
        if hasattr(self.store, "upsert_connector_setting"):
            row = cast(Any, self.store).upsert_connector_setting(
                {
                    "connector_name": definition.name,
                    "enabled": config["enabled"],
                    "configured": config["configured"],
                    "default_domain": domain,
                    "default_sensitivity": sensitivity,
                    "sync_mode": normalized_sync_mode,
                    "poll_interval_seconds": resolved_poll_interval,
                    "secret_ref": resolved_secret_ref,
                    "validation_errors_json": validation_errors,
                    "metadata_json": {"config_json": sanitized_config},
                    "last_configured_at": config["updated_at"],
                },
                actor_type="user",
            )
            return self._setting_row_to_config(row)
        self._log_event(
            event_type="connector.config_updated",
            connector_name=definition.name,
            payload=config,
        )
        return config

    def get_config(self, connector_name: str) -> JsonObject:
        definition = get_connector_definition(connector_name)
        row = self._connector_row("settings", "get_connector_setting", definition.name)
        if isinstance(row, dict):
            return self._setting_row_to_config(row)
        events = [
            event
            for event in self._connector_events(definition.name)
            if event.get("event_type") == "connector.config_updated"
        ]
        events.sort(key=lambda event: str(event.get("occurred_at") or ""), reverse=True)
        if events:
            payload = events[0].get("payload_json")
            if isinstance(payload, dict):
                config = cast(JsonObject, {**payload})
                if definition.name == "telegram":
                    validation_errors = payload.get("validation_errors")
                    if not isinstance(validation_errors, list):
                        validation_errors = []
                    config = {
                        "connector_name": "telegram",
                        "enabled": bool(payload.get("enabled")),
                        "configured": bool(payload.get("configured")),
                        "secret_ref": None,
                        "secret_configured": False,
                        "default_domain": payload.get("default_domain") or definition.default_domain,
                        "default_sensitivity": (
                            payload.get("default_sensitivity") or definition.default_sensitivity
                        ),
                        "sync_mode": "on_demand",
                        "poll_interval_seconds": None,
                        "config_json": _telegram_source_config(payload.get("config_json")),
                        "validation_errors": validation_errors,
                        "updated_at": payload.get("updated_at"),
                    }
                return config
        return {
            "connector_name": definition.name,
            "enabled": False,
            "configured": False,
            "secret_ref": None,
            "secret_configured": False,
            "default_domain": definition.default_domain,
            "default_sensitivity": definition.default_sensitivity,
            "sync_mode": "on_demand" if definition.name == "telegram" else "manual",
            "poll_interval_seconds": None,
            "config_json": {},
            "validation_errors": [],
            "updated_at": None,
        }

    def connector_health(self, connector_name: str, *, guard: LabelGuard) -> JsonObject:
        """The health of one connector, as ``guard``'s caller may be shown it.

        ``guard`` is required. The owner and an unbound admin key pass an inactive guard and get the block as it was
        stored. For any other caller, ``last_captured_item`` names a captured source by id and external id, and
        ``cursor_state`` is the cursor of an imported item (for a file, its path), so each is shown only when every
        source it names is stored, not deleted, and admitted by ``guard`` on its effective labels. Counters, times and
        public error codes carry no row text and stay.
        """

        definition = get_connector_definition(connector_name)
        config = self.get_config(definition.name)
        candidate_state = self._connector_row("states", "get_connector_state", definition.name)
        state = candidate_state if isinstance(candidate_state, dict) else None
        events = self._connector_events(definition.name)
        events.sort(key=lambda event: str(event.get("occurred_at") or ""), reverse=True)
        sync_events = [
            event
            for event in events
            if event.get("event_type") in {"connector.sync_completed", "connector.sync_failed"}
            and isinstance(event.get("payload_json"), dict)
        ]
        latest_success = next(
            (event for event in sync_events if event.get("event_type") == "connector.sync_completed"), None
        )
        latest_failure = next(
            (event for event in sync_events if event.get("event_type") == "connector.sync_failed"), None
        )
        latest_item_failure = next(
            (event for event in events if event.get("event_type") == "connector.item_failed"), None
        )
        latest_import = next((event for event in events if event.get("event_type") == "connector.item_imported"), None)
        latest_scan = next((event for event in events if event.get("event_type") == "connector.local_folder_scan"), None)

        items_seen = 0
        items_captured = 0
        items_deduped = 0
        items_failed = 0
        processing_times: list[float] = []
        cursor_state: str | None = None
        for event in sync_events:
            payload = cast(JsonObject, event["payload_json"])
            items_seen += _int_count(payload.get("item_count"))
            items_captured += _int_count(payload.get("imported_count"))
            items_deduped += _int_count(payload.get("duplicate_count"))
            items_failed += _int_count(payload.get("failed_count"))
            cursor = _as_optional_text(payload.get("sync_cursor"))
            if cursor_state is None and cursor is not None:
                cursor_state = cursor
            processing_time = payload.get("processing_time_ms")
            if isinstance(processing_time, (int, float)) and not isinstance(processing_time, bool):
                processing_times.append(float(processing_time))
        last_error = None
        last_error_code = None
        latest_failure_payload = latest_failure.get("payload_json") if latest_failure is not None else None
        if isinstance(latest_failure_payload, dict):
            errors = latest_failure_payload.get("errors")
            last_error_code = _as_optional_text(latest_failure_payload.get("error_code"))
            if isinstance(errors, list) and errors:
                last_error_code = last_error_code or CONNECTOR_SYNC_ERROR_CODE
                last_error = _connector_public_error_message(last_error_code)
        latest_item_failure_payload = (
            latest_item_failure.get("payload_json") if latest_item_failure is not None else None
        )
        if last_error is None and isinstance(latest_item_failure_payload, dict):
            last_error_code = (
                _as_optional_text(latest_item_failure_payload.get("error_code"))
                or CONNECTOR_ITEM_IMPORT_ERROR_CODE
            )
            last_error = _connector_public_error_message(last_error_code)

        state_json = state.get("state_json") if state is not None else None
        state_error_code = (
            _as_optional_text(state_json.get("last_error_code")) if isinstance(state_json, dict) else None
        )
        state_last_error = state.get("last_error") if state is not None else None
        if state_last_error is not None:
            state_error_code = state_error_code or CONNECTOR_SYNC_ERROR_CODE
            state_last_error = _connector_public_error_message(state_error_code)

        latest_import_payload = latest_import.get("payload_json") if latest_import is not None else None
        # What the last local-folder scan skipped, so a refused file or a stop at a limit is not silent.
        latest_scan_payload = latest_scan.get("payload_json") if latest_scan is not None else None
        last_scan: JsonObject | None = None
        if latest_scan is not None and isinstance(latest_scan_payload, dict):
            last_scan = {
                "occurred_at": latest_scan.get("occurred_at"),
                "refused_count": _int_count(latest_scan_payload.get("refused_count")),
                "truncated": latest_scan_payload.get("truncated") is True,
            }
        last_captured_item = latest_import_payload if isinstance(latest_import_payload, dict) else None
        if guard.active and last_captured_item is not None:
            if not self._caller_reads_sources(guard, [last_captured_item.get("source_id")]):
                last_captured_item = None
        cursor_value = self.shown_cursor(
            definition.name,
            _as_optional_text(state.get("cursor_value"))
            if state is not None
            else cursor_state or self.get_cursor(definition.name),
            guard=guard,
            events=events,
        )
        return {
            "connector_name": definition.name,
            "display_name": definition.display_name,
            "enabled": bool(config.get("enabled")),
            "configured": bool(config.get("configured")),
            "default_domain": config.get("default_domain") or definition.default_domain,
            "default_sensitivity": config.get("default_sensitivity") or definition.default_sensitivity,
            "sync_mode": config.get("sync_mode") or "manual",
            "poll_interval_seconds": config.get("poll_interval_seconds"),
            "validation_errors": config.get("validation_errors") or [],
            "secret_configured": bool(config.get("secret_configured")),
            "last_sync_at": state.get("last_sync_at")
            if state is not None
            else sync_events[0].get("occurred_at")
            if sync_events
            else None,
            "last_success_at": state.get("last_success_at")
            if state is not None
            else latest_success.get("occurred_at")
            if latest_success is not None
            else None,
            "last_failure_at": state.get("last_failure_at")
            if state is not None
            else latest_failure.get("occurred_at")
            if latest_failure is not None
            else None,
            "last_error": state_last_error if state_last_error is not None else last_error,
            "last_error_code": state_error_code or last_error_code,
            "last_captured_item": last_captured_item,
            "last_scan": last_scan,
            "items_seen": int(state.get("items_seen", 0)) if state is not None else items_seen,
            "items_captured": int(state.get("items_captured", 0)) if state is not None else items_captured,
            "items_deduped": int(state.get("items_deduped", 0)) if state is not None else items_deduped,
            "items_failed": int(state.get("items_failed", 0)) if state is not None else items_failed,
            "cursor_state": cursor_value,
            "average_processing_time": state.get("average_processing_time_ms")
            if state is not None
            else round(sum(processing_times) / len(processing_times), 3)
            if processing_times
            else None,
        }

    def shown_cursor(
        self,
        connector_name: str,
        cursor: str | None,
        *,
        guard: LabelGuard,
        events: Sequence[Mapping[str, object]] | None = None,
    ) -> str | None:
        """``cursor`` as ``guard``'s caller may be shown it.

        A cursor is the position of the last item imported, and for a file or a page it is the path or the address of
        that item. It is shown to a caller with limits only when at least one import carries it and every import that
        carries it names a source the caller may read. A cursor that no import carries cannot be tied to a source, so it
        is not shown. The owner and an unbound admin key get the cursor as stored.
        """

        if cursor is None or not guard.active:
            return cursor
        known = events if events is not None else self._connector_events(connector_name)
        carriers: list[Mapping[str, object]] = []
        for event in known:
            payload = event.get("payload_json")
            if (
                event.get("event_type") == "connector.item_imported"
                and isinstance(payload, dict)
                and payload.get("sync_cursor") == cursor
            ):
                carriers.append(payload)
        if not carriers or not self._caller_reads_sources(guard, [item.get("source_id") for item in carriers]):
            return None
        return cursor

    def shown_sync_record(self, result: ConnectorSyncResult, *, guard: LabelGuard) -> JsonObject:
        """The record of a sync as ``guard``'s caller may be shown it: its two cursors pass through ``shown_cursor``.

        The previous cursor is where an earlier call, perhaps another caller's, left off, so it can name an item the
        caller may not read. The ids of the sources and of the failed items are the ones the call itself handled.
        """

        record = result.to_record()
        if guard.active:
            for name in ("previous_cursor", "sync_cursor"):
                cursor = record.get(name)
                record[name] = self.shown_cursor(
                    result.connector_name, cursor if isinstance(cursor, str) else None, guard=guard
                )
        return record

    def shown_event_cursors(
        self, events: Sequence[Mapping[str, Any]], *, guard: LabelGuard
    ) -> list[Mapping[str, Any]]:
        """Connector events as ``guard``'s caller may be shown them: the cursors an event recorded pass through ``shown_cursor``.

        A sync event records where the connector stood (``cursor_value`` of a state update, ``previous_cursor`` and
        ``sync_cursor`` of a sync), and for a file or a page that is its path or its address. Each one is shown when the
        caller may read the source it came from and is ``null`` otherwise, the same rule as the health block. An event
        that is not one of those, and a field that is empty, are left as they are (a failed item names only what the call
        that failed it sent, and it never became a row). The events handed in are not edited: an event with a cursor that
        is not shown is copied.
        """

        if not guard.active:
            return list(events)
        recorded: dict[str, list[Mapping[str, Any]]] = {}
        decided: dict[tuple[str, str], str | None] = {}
        shown: list[Mapping[str, Any]] = []
        for event in events:
            fields = CONNECTOR_EVENT_CURSOR_FIELDS.get(str(event.get("event_type")))
            payload = event.get("payload_json")
            if fields is None or not isinstance(payload, dict):
                shown.append(event)
                continue
            name = event.get("target_id") if event.get("target_type") == "connector" else payload.get("connector_name")
            cleaned = dict(payload)
            for field in fields:
                cursor = payload.get(field)
                if cursor is None or cursor == "":
                    continue
                # A cursor that is not text, and one of a connector the event does not name, cannot be tied to an import.
                if not isinstance(cursor, str) or not isinstance(name, str):
                    cleaned[field] = None
                    continue
                if (name, cursor) not in decided:
                    if name not in recorded:
                        recorded[name] = list(self._connector_events(name))
                    decided[(name, cursor)] = self.shown_cursor(name, cursor, guard=guard, events=recorded[name])
                cleaned[field] = decided[(name, cursor)]
            shown.append(event if cleaned == payload else {**event, "payload_json": cleaned})
        return shown

    def _caller_reads_sources(self, guard: LabelGuard, source_ids: Sequence[object]) -> bool:
        """True when every id names a stored, undeleted source that ``guard`` admits on its effective labels.

        An empty list, an id that is not a source id, a source that is missing and a source that is deleted all answer
        False, so a block that cannot be tied to a readable source is not shown.
        """

        getter = getattr(self.store, "get_source", None)
        if not callable(getter) or not source_ids:
            return False
        rows: list[Mapping[str, object]] = []
        for raw in dict.fromkeys(source_ids):
            try:
                source_id = str(UUID(str(raw)))
            except (ValueError, AttributeError, TypeError):
                return False
            row = getter(source_id)
            if not isinstance(row, Mapping) or row.get("deleted_at") is not None:
                return False
            rows.append(row)
        return len(guard.admit_rows("source", rows)) == len(rows)

    def _connector_row(self, namespace: str, method: str, name: str):
        from alicebot_api.vnext_label_guard import request_row_cache

        cache = request_row_cache(self.store, "connector_health_inputs")
        if cache is not None and namespace in cache and name in cache[namespace]:
            return deepcopy(cache[namespace][name])
        getter = getattr(self.store, method, None)
        row = getter(name) if callable(getter) else None
        if cache is not None:
            cache.setdefault(namespace, {})[name] = deepcopy(row)
        return row

    def _connector_events(self, name: str):
        from alicebot_api.vnext_label_guard import request_row_cache

        cache = request_row_cache(self.store, "connector_health_inputs")
        if cache is not None and name in cache.get("events", {}):
            return deepcopy(cache["events"][name])
        events = self.store.list_events(target_type="connector", target_id=name)
        if cache is not None:
            cache.setdefault("events", {})[name] = deepcopy(events)
        return events

    def connector_health_all(self, *, guard: LabelGuard) -> JsonObject:
        from copy import deepcopy
        from alicebot_api.vnext_label_guard import request_row_cache

        # Workspace, dogfooding and doctor render the same connector snapshot
        # in one guarded request. Keep this census request-local, with the
        # same store/write invalidation as label input rows. A snapshot is
        # reused only for the grant it was judged under.
        cache = request_row_cache(self.store, "connector_health_all")
        grant = ("result", guard.active, guard.domains, guard.sensitivity_allowed, guard.projects, guard.all_of)
        if cache is not None and grant in cache:
            return deepcopy(cache[grant])
        definitions = list_connector_definitions()
        inputs = request_row_cache(self.store, "connector_health_inputs")
        if inputs is not None:
            # Native stores expose the same tenant-scoped settings and states
            # in one read. Missing rows still use the historical event fallback.
            for namespace, method in (("settings", "list_connector_settings"), ("states", "list_connector_states")):
                if callable(getattr(type(self.store), method, None)):
                    rows = getattr(self.store, method)()
                    by_name = {str(row["connector_name"]): deepcopy(row) for row in rows
                               if isinstance(row, dict) and row.get("connector_name")
                               and (namespace != "states" or row.get("cursor_type") == "sync_cursor")}
                    inputs[namespace] = {definition.name: by_name.get(definition.name) for definition in definitions}
        items = [self.connector_health(definition.name, guard=guard) for definition in definitions]
        result: JsonObject = {"items": items, "count": len(items), "order": [str(item["connector_name"]) for item in items]}
        if cache is not None:
            cache[grant] = deepcopy(result)
        return result

    def set_connector_secret(self, connector_name: str, *, secret_ref: str, secret_value: str) -> JsonObject:
        definition = get_connector_definition(connector_name)
        if definition.name == "telegram":
            raise VNextConnectorValidationError(
                "telegram source ingestion does not accept or store connector secrets"
            )
        if not secret_value.strip():
            raise VNextConnectorValidationError("connector secret value is required")
        self.secret_provider.set_secret(secret_ref, secret_value)
        self._log_event(
            event_type="connector.secret_ref_updated",
            connector_name=definition.name,
            payload={
                "connector_name": definition.name,
                "secret_ref": secret_ref,
                "secret_present": self.secret_provider.has_secret(secret_ref),
            },
        )
        return {"connector_name": definition.name, "secret_ref": secret_ref, "secret_present": True}

    def _resolve_secret(self, secret_ref: str | None) -> str | None:
        if secret_ref is None:
            return None
        try:
            return self.secret_provider.get_secret(secret_ref)
        except Exception as exc:
            raise VNextConnectorValidationError("connector secret could not be resolved") from exc

    def _record_connector_state(
        self,
        result: ConnectorSyncResult,
        *,
        processing_time_ms: float,
    ) -> None:
        if not hasattr(self.store, "upsert_connector_state"):
            return
        now = _utc_now_iso()
        last_error = result.errors[0] if result.errors else None
        try:
            cast(Any, self.store).upsert_connector_state(
                {
                    "connector_name": result.connector_name,
                    "cursor_type": "sync_cursor",
                    "cursor_value": result.sync_cursor,
                    "last_sync_at": now,
                    "last_success_at": now if result.status in {"ok", "partial", "skipped"} else None,
                    "last_failure_at": now if result.status == "failed" else None,
                    "last_error": last_error,
                    "items_seen_delta": result.item_count,
                    "items_captured_delta": result.imported_count,
                    "items_deduped_delta": result.duplicate_count,
                    "items_failed_delta": result.failed_count,
                    "average_processing_time_ms": round(processing_time_ms, 3),
                    "state_json": {
                        "last_status": result.status,
                        "last_error_code": result.error_code,
                        "last_source_ids": list(result.source_ids),
                        "last_failed_external_ids": list(result.failed_external_ids),
                    },
                },
                actor_type="system",
            )
        except Exception:
            # State is telemetry/checkpointing; do not convert a successful
            # source capture into a failed sync because the state row is stale.
            self._log_event(
                event_type="connector.state_update_failed",
                connector_name=result.connector_name,
                payload={"connector_name": result.connector_name, "status": result.status},
            )

    def sync_telegram_updates(
        self,
        updates: Sequence[Mapping[str, object]],
        *,
        allowed_chat_ids: Sequence[str],
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
    ) -> ConnectorSyncResult:
        allowed = {str(chat_id) for chat_id in allowed_chat_ids if str(chat_id).strip()}
        if not allowed:
            raise VNextConnectorValidationError("telegram connector requires at least one allowed chat id")
        accepted: list[Mapping[str, object]] = []
        rejected_count = 0
        safe_cursor: str | None = None
        for update in updates:
            update_id = _telegram_update_id(update)
            if update_id is not None and (
                safe_cursor is None or _cursor_sort_key(update_id) > _cursor_sort_key(safe_cursor)
            ):
                safe_cursor = update_id
            chat_id = _telegram_chat_id(update)
            if chat_id is None or chat_id not in allowed:
                rejected_count += 1
                self._log_event(
                    event_type="connector.item_rejected",
                    connector_name="telegram",
                    payload={
                        "connector_name": "telegram",
                        "external_id": update_id or "unknown",
                        "reason": "chat_not_allowlisted",
                        "chat_id": chat_id,
                    },
                )
                continue
            accepted.append(update)
        result = self._sync_items(
            "telegram",
            accepted,
            default_domain=default_domain,
            default_sensitivity=default_sensitivity,
            original_item_count=len(updates),
            extra_skipped_count=rejected_count,
            safe_cursor_override=safe_cursor,
        )
        return result

    def sync_local_folder(
        self,
        paths: Sequence[str | Path],
        *,
        recursive: bool = True,
        extensions: Sequence[str] = DEFAULT_LOCAL_FOLDER_EXTENSIONS,
        ignore_patterns: Sequence[str] = (),
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
    ) -> ConnectorSyncResult:
        scan = scan_local_folder(
            paths,
            recursive=recursive,
            extensions=extensions,
            ignore_patterns=ignore_patterns,
        )
        return self.sync_local_folder_scan(
            scan,
            default_domain=default_domain,
            default_sensitivity=default_sensitivity,
        )

    def sync_local_folder_scan(
        self,
        scan: LocalFolderScan,
        *,
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
    ) -> ConnectorSyncResult:
        self._log_event(
            event_type="connector.local_folder_scan",
            connector_name="local_folder",
            payload={
                "connector_name": "local_folder",
                "path_count": scan.path_count,
                "file_count": len(scan.items),
                "ignored_count": scan.ignored_count,
                "refused_count": scan.refused_count,
                "truncated": scan.truncated,
                "recursive": scan.recursive,
                "extensions": list(scan.extensions),
            },
        )
        return self.sync_items(
            "local_folder",
            scan.items,
            default_domain=default_domain,
            default_sensitivity=default_sensitivity,
            use_cursor=False,
        )

    def capture_browser_clip(
        self,
        payload: Mapping[str, object],
        *,
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
        capability_authorized: bool = False,
    ) -> ConnectorSyncResult:
        config = self.get_config("browser_clipper")
        secret_ref = _as_optional_text(config.get("secret_ref"))
        if secret_ref is not None and not capability_authorized:
            expected_token = self._resolve_secret(secret_ref)
            provided_token = _as_optional_text(payload.get("capture_token"))
            if not expected_token or not provided_token or not hmac.compare_digest(provided_token, expected_token):
                self._log_event(
                    event_type="connector.item_rejected",
                    connector_name="browser_clipper",
                    payload={
                        "connector_name": "browser_clipper",
                        "reason": "invalid_capture_token",
                        "secret_ref": secret_ref,
                    },
                )
                raise VNextConnectorValidationError("browser clipper capture token is invalid")
        sanitized_payload_value: object = dict(payload)
        capability = _as_optional_text(payload.get("capture_capability"))
        if capability_authorized:
            if capability is None:
                raise VNextConnectorValidationError("browser clipper capability is required")
            sanitized_payload_value = redact_secret_value(sanitized_payload_value, capability)
        if not isinstance(sanitized_payload_value, dict):  # pragma: no cover - recursive redactor contract
            raise RuntimeError("browser clipper payload redaction returned a non-object")
        # Capabilities authorize only this transaction. Never persist even a
        # redacted marker that would imply they are connector source content.
        sanitized_payload_value.pop("capture_capability", None)
        sanitized_payload = cast(JsonObject, redact_secret_fields(sanitized_payload_value))
        return self.sync_items(
            "browser_clipper",
            [sanitized_payload],
            default_domain=default_domain,
            default_sensitivity=default_sensitivity,
            use_cursor=False,
        )

    def ingest_agent_output(
        self,
        payload: Mapping[str, object],
        *,
        policy_decision: JsonObject | None = None,
    ) -> AgentOutputIngestResult:
        item = normalize_connector_item("agent_output", payload)
        agent_id = str(item.metadata_json.get("agent_id") or item.author or "unknown")
        agent_identity = {
            "agent_id": agent_id,
            "agent_type": item.metadata_json.get("agent_type") or "unknown",
            "agent_run_id": item.metadata_json.get("agent_run_id"),
            "task_id": item.metadata_json.get("task_id"),
            "project_scope": item.metadata_json.get("project_scope") or [],
        }
        capture = VNextCaptureService(
            self.store,
            actor_type="agent",
            actor_id=agent_id,
            run_id=_as_optional_text(payload.get("agent_run_id")),
            agent_identity=agent_identity,
            policy_decision=policy_decision,
            defer_embeddings=self.defer_embeddings,
        ).capture_source(
            SourceCaptureInput(
                source_type=item.source_type,
                title=item.title,
                raw_text=item.raw_text,
                author=item.author,
                connector_name=item.connector_name,
                external_id=item.external_id,
                domain=_as_optional_text(payload.get("domain")) or "project",
                sensitivity=_as_optional_text(payload.get("sensitivity")) or "private",
                captured_at=item.captured_at,
                source_created_at=item.source_created_at,
                metadata_json={
                    **item.metadata_json,
                    "connector_name": "agent_output",
                    "external_id": item.external_id,
                    "policy_decision": policy_decision,
                },
            )
        )
        source_id = capture.source_id
        artifact_id: str | None = None
        memory_id: str | None = None
        artifact = self.store.create_artifact(
            {
                "artifact_type": _agent_artifact_type(_as_optional_text(payload.get("output_type"))),
                "title": item.title,
                "content_markdown": item.raw_text,
                "status": "needs_review",
                "domain": _as_optional_text(payload.get("domain")) or "project",
                "sensitivity": _as_optional_text(payload.get("sensitivity")) or "private",
                "generated_by": agent_id,
                "metadata_json": with_derived_from(
                    {
                        "connector_name": "agent_output",
                        "agent_identity": agent_identity,
                        "agent_id": agent_id,
                        "agent_run_id": item.metadata_json.get("agent_run_id"),
                        "project_scope": item.metadata_json.get("project_scope") or [],
                        "source_id": source_id,
                        "source_refs": [f"source:{source_id}"] if source_id else [],
                        "output_type": _as_optional_text(payload.get("output_type")) or "general",
                        "review_status": "needs_review",
                    },
                    {"sources": [{"id": source_id}] if source_id else []},
                ),
            },
            actor_type="agent",
        )
        artifact_id = str(artifact["id"])
        if source_id is not None:
            self.store.create_provenance_link(
                {
                    "target_type": "artifact",
                    "target_id": artifact_id,
                    "source_id": source_id,
                    "source_chunk_id": None,
                    "quote": item.title,
                    "evidence_role": "summarizes",
                    "confidence": 0.72,
                },
                actor_type="agent",
            )

        if bool(payload.get("propose_memory")):
            # Carry the agent's project scope onto the proposed memory so the
            # owning project can retrieve it and other projects are excluded.
            # Without this the candidate was unscoped for canonical retrieval
            # even though the source was scoped (audit 2 P1 #2).
            proposal_scope = resolve_project_scope(
                {
                    "metadata_json": item.metadata_json,
                    "scope_json": agent_identity,
                }
            ).values
            memory = self.store.create_memory(
                {
                    "memory_key": f"vnext.agent_output.{sha256((item.external_id + item.raw_text).encode('utf-8')).hexdigest()[:16]}",
                    "value": {
                        "text": item.title,
                        "source_id": source_id,
                        "artifact_id": artifact_id,
                        "rationale": item.metadata_json.get("rationale"),
                    },
                    "status": "candidate",
                    "source_event_ids": [value for value in (source_id, artifact_id) if value],
                    "memory_type": "agent_run",
                    "confidence": 0.62,
                    "title": item.title,
                    "canonical_text": item.title,
                    "summary": _as_optional_text(payload.get("rationale")) or item.raw_text[:280],
                    "domain": _as_optional_text(payload.get("domain")) or "project",
                    "sensitivity": _as_optional_text(payload.get("sensitivity")) or "private",
                    "project_scope": list(proposal_scope),
                    "project_id": proposal_scope[0] if len(proposal_scope) == 1 else None,
                    "created_by_agent_id": agent_id,
                    "run_id": item.metadata_json.get("agent_run_id"),
                    "metadata_json": with_derived_from(
                        {
                            "connector_name": "agent_output",
                            "agent_identity": agent_identity,
                            "agent_id": agent_id,
                            "agent_run_id": item.metadata_json.get("agent_run_id"),
                            "source_id": source_id,
                            "artifact_id": artifact_id,
                            "review_required": True,
                            "policy_decision": policy_decision,
                            **({"project_scope": list(proposal_scope)} if proposal_scope else {}),
                        },
                        {
                            "sources": [{"id": source_id}] if source_id else [],
                            "artifacts": [{"id": artifact_id}] if artifact_id else [],
                        },
                    ),
                },
                actor_type="agent",
            )
            memory_id = str(memory["id"])
            if source_id is not None:
                self.store.create_provenance_link(
                    {
                        "target_type": "memory",
                        "target_id": memory_id,
                        "source_id": source_id,
                        "source_chunk_id": None,
                        "quote": item.title,
                        "evidence_role": "inferred_from",
                        "confidence": 0.62,
                    },
                    actor_type="agent",
                )
            self._log_event(
                event_type="memory.candidate_created",
                connector_name="agent_output",
                payload={
                    "memory_id": memory_id,
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "review_required": True,
                },
            )

        append_event(
            self.store,
            event_type="agent.output_ingested",
            actor_type="agent",
            actor_id=agent_id,
            target_type="connector",
            target_id="agent_output",
            payload={
                "connector_name": "agent_output",
                "agent_identity": agent_identity,
                "source_id": source_id,
                "artifact_id": artifact_id,
                "memory_id": memory_id,
                "propose_memory": bool(payload.get("propose_memory")),
                "policy_decision": policy_decision,
            },
        )
        return AgentOutputIngestResult(
            status="imported",
            source_id=source_id,
            artifact_id=artifact_id,
            memory_id=memory_id,
            policy_decision=policy_decision,
            deferred_embedding_inputs=capture.deferred_embedding_inputs,
        )

    def sync_items(
        self,
        connector_name: str,
        items: Sequence[Mapping[str, object]],
        *,
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
        use_cursor: bool = True,
        original_item_count: int | None = None,
        extra_skipped_count: int = 0,
        safe_cursor_override: str | None = None,
    ) -> ConnectorSyncResult:
        definition = get_connector_definition(connector_name)
        if definition.name == "telegram":
            raise VNextConnectorValidationError(
                "telegram items require sync_telegram_updates with an explicit chat allowlist"
            )
        return self._sync_items(
            definition.name,
            items,
            default_domain=default_domain,
            default_sensitivity=default_sensitivity,
            use_cursor=use_cursor,
            original_item_count=original_item_count,
            extra_skipped_count=extra_skipped_count,
            safe_cursor_override=safe_cursor_override,
        )

    def _sync_items(
        self,
        connector_name: str,
        items: Sequence[Mapping[str, object]],
        *,
        default_domain: str | None = None,
        default_sensitivity: str | None = None,
        use_cursor: bool = True,
        original_item_count: int | None = None,
        extra_skipped_count: int = 0,
        safe_cursor_override: str | None = None,
    ) -> ConnectorSyncResult:
        started_at = time.perf_counter()
        definition = get_connector_definition(connector_name)
        config = self.get_config(definition.name)
        domain = default_domain or _as_optional_text(config.get("default_domain")) or definition.default_domain
        sensitivity = (
            default_sensitivity
            or _as_optional_text(config.get("default_sensitivity"))
            or definition.default_sensitivity
        )
        if domain not in _VALID_DOMAINS:
            raise VNextConnectorValidationError(f"invalid connector default domain: {domain}")
        if sensitivity not in _VALID_SENSITIVITIES:
            raise VNextConnectorValidationError(f"invalid connector default sensitivity: {sensitivity}")

        previous_cursor = self.get_cursor(definition.name) if use_cursor else None
        self._log_event(
            event_type="connector.sync_started",
            connector_name=definition.name,
            payload={
                "connector_name": definition.name,
                "item_count": original_item_count if original_item_count is not None else len(items),
                "previous_cursor": previous_cursor,
                "default_domain": domain,
                "default_sensitivity": sensitivity,
            },
        )

        normalized_items: list[NormalizedConnectorItem] = []
        source_ids: list[str] = []
        errors: list[str] = []
        failed_external_ids: list[str] = []
        imported_count = 0
        duplicate_count = 0
        skipped_count = extra_skipped_count
        failed_count = 0
        sync_cursor = previous_cursor
        failure_blocked_cursor_advance = False
        deferred_embedding_inputs: list[DeferredMemoryEmbedding] = []

        for index, item in enumerate(items):
            try:
                normalized_items.append(normalize_connector_item(definition.name, item))
            except Exception as exc:
                failed_count += 1
                external_id = _first_text(item, ("external_id", "id", "filename", "path", "url")) or f"item-{index}"
                failed_external_ids.append(external_id)
                errors.append(CONNECTOR_ITEM_IMPORT_ERROR_MESSAGE)
                failure_blocked_cursor_advance = True
                logger.exception(
                    "connector item normalization failed connector=%s external_id=%s error_code=%s",
                    definition.name,
                    external_id,
                    CONNECTOR_ITEM_IMPORT_ERROR_CODE,
                )
                self._log_event(
                    event_type="connector.item_failed",
                    connector_name=definition.name,
                    payload={
                        "connector_name": definition.name,
                        "external_id": external_id,
                        "sync_cursor": None,
                        "error_code": CONNECTOR_ITEM_IMPORT_ERROR_CODE,
                        "error_message": CONNECTOR_ITEM_IMPORT_ERROR_MESSAGE,
                    },
                )

        normalized_items.sort(key=lambda item: _cursor_sort_key(item.cursor))

        for normalized_item in normalized_items:
            if _cursor_lte(normalized_item.cursor, previous_cursor):
                skipped_count += 1
                continue

            try:
                capture_result = self.capture_service.capture_source(
                    SourceCaptureInput(
                        source_type=normalized_item.source_type,
                        title=normalized_item.title,
                        author=normalized_item.author,
                        uri=normalized_item.uri,
                        raw_path=normalized_item.raw_path,
                        raw_text=normalized_item.raw_text,
                        connector_name=normalized_item.connector_name,
                        external_id=normalized_item.external_id,
                        domain=domain,
                        sensitivity=sensitivity,
                        captured_at=normalized_item.captured_at,
                        source_created_at=normalized_item.source_created_at,
                        source_modified_at=normalized_item.source_modified_at,
                        metadata_json={
                            **normalized_item.metadata_json,
                            "connector_name": normalized_item.connector_name,
                            "external_id": normalized_item.external_id,
                            "sync_cursor": normalized_item.cursor,
                            "default_domain": domain,
                            "default_sensitivity": sensitivity,
                        },
                    )
                )
            except CaptureCredentialRefused:
                # A credential skip is not a failed item. Do not store the
                # title, path, or text that the floor refused.
                skipped_count += 1
                if not failure_blocked_cursor_advance:
                    sync_cursor = normalized_item.cursor
                continue
            except Exception as exc:
                failed_count += 1
                failed_external_ids.append(normalized_item.external_id)
                errors.append(CONNECTOR_ITEM_IMPORT_ERROR_MESSAGE)
                failure_blocked_cursor_advance = True
                logger.exception(
                    "connector item capture failed connector=%s external_id=%s error_code=%s",
                    definition.name,
                    normalized_item.external_id,
                    CONNECTOR_ITEM_IMPORT_ERROR_CODE,
                )
                self._log_event(
                    event_type="connector.item_failed",
                    connector_name=definition.name,
                    payload={
                        "connector_name": definition.name,
                        "external_id": normalized_item.external_id,
                        "sync_cursor": normalized_item.cursor,
                        "error_code": CONNECTOR_ITEM_IMPORT_ERROR_CODE,
                        "error_message": CONNECTOR_ITEM_IMPORT_ERROR_MESSAGE,
                    },
                )
                continue

            if capture_result.duplicate:
                duplicate_count += 1
            else:
                imported_count += 1
                if capture_result.source_id is not None:
                    source_ids.append(capture_result.source_id)
            deferred_embedding_inputs.extend(capture_result.deferred_embedding_inputs)

            self._log_event(
                event_type="connector.item_imported",
                connector_name=definition.name,
                payload={
                    "connector_name": definition.name,
                    "external_id": normalized_item.external_id,
                    "sync_cursor": normalized_item.cursor,
                    "source_id": capture_result.source_id,
                    "duplicate": capture_result.duplicate,
                    "raw_evidence_preserved": True,
                },
            )
            if not failure_blocked_cursor_advance:
                sync_cursor = normalized_item.cursor

        if not failure_blocked_cursor_advance and safe_cursor_override is not None:
            if sync_cursor is None or _cursor_sort_key(safe_cursor_override) > _cursor_sort_key(sync_cursor):
                sync_cursor = safe_cursor_override

        status = "ok"
        if failed_count > 0 and (imported_count > 0 or duplicate_count > 0):
            status = "partial"
        elif failed_count > 0:
            status = "failed"
        elif imported_count == 0 and duplicate_count == 0 and skipped_count > 0:
            status = "skipped"

        sync_result = ConnectorSyncResult(
            status=status,
            connector_name=definition.name,
            item_count=original_item_count if original_item_count is not None else len(items),
            imported_count=imported_count,
            duplicate_count=duplicate_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            previous_cursor=previous_cursor,
            sync_cursor=sync_cursor,
            error_code=CONNECTOR_ITEM_IMPORT_ERROR_CODE if failed_count else None,
            source_ids=tuple(source_ids),
            failed_external_ids=tuple(failed_external_ids),
            errors=tuple(errors),
            deferred_embedding_inputs=tuple(deferred_embedding_inputs),
        )
        final_event_type = "connector.sync_failed" if status == "failed" else "connector.sync_completed"
        processing_time_ms = round((time.perf_counter() - started_at) * 1000, 3)
        self._log_event(
            event_type=final_event_type,
            connector_name=definition.name,
            payload={**sync_result.to_record(), "processing_time_ms": processing_time_ms},
        )
        self._record_connector_state(sync_result, processing_time_ms=processing_time_ms)
        return sync_result


__all__ = [
    "AgentOutputIngestResult",
    "ConnectorDefinition",
    "ConnectorSyncResult",
    "DEFAULT_LOCAL_FOLDER_EXTENSIONS",
    "DEFAULT_LOCAL_FOLDER_IGNORES",
    "LOCAL_FOLDER_ROOTS_ENV",
    "LocalFolderScan",
    "MAX_LOCAL_FOLDER_FILES",
    "MAX_LOCAL_FOLDER_FILE_BYTES",
    "MAX_LOCAL_FOLDER_LISTED",
    "MAX_LOCAL_FOLDER_TOTAL_BYTES",
    "NormalizedConnectorItem",
    "SUPPORTED_CONNECTORS",
    "VNextConnectorService",
    "VNextConnectorStore",
    "VNextConnectorValidationError",
    "get_connector_definition",
    "list_connector_definitions",
    "load_connector_items_from_file",
    "normalize_connector_item",
    "scan_local_folder",
]
