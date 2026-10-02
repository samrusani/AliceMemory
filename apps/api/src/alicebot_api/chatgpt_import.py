from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import UUID

from alicebot_api.continuity_evidence import SourceArtifactArchiveInput, archive_import_source_files
from alicebot_api.importer_models import (
    ImporterNormalizedBatch,
    ImporterNormalizedItem,
    ImporterValidationError,
    ImporterWorkspaceContext,
    OBJECT_TYPE_TO_BODY_KEY,
    OBJECT_TYPE_TO_PREFIX,
    as_json_object,
    dedupe_key_for_payload,
    merge_json_objects,
    normalize_object_type,
    normalize_optional_text,
    parse_optional_confidence,
    parse_optional_status,
)
from alicebot_api.importer_paths import (
    DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    ImportSourceFile,
    contained_source_files,
    read_contained_source_text,
    snapshot_source_files,
)
from alicebot_api.importers.common import ImportPersistenceConfig, import_normalized_batch
from alicebot_api.store import ContinuityStore, JsonObject, JsonValue


_DEFAULT_CONFIDENCE = 0.83
_DEFAULT_DEDUPE_POSTURE = "workspace_conversation_message_fingerprint"
_PREFIX_TO_OBJECT_TYPE: tuple[tuple[str, str], ...] = (
    ("decision:", "Decision"),
    ("next action:", "NextAction"),
    ("next:", "NextAction"),
    ("task:", "NextAction"),
    ("commitment:", "Commitment"),
    ("waiting for:", "WaitingFor"),
    ("blocker:", "Blocker"),
    ("fact:", "MemoryFact"),
    ("remember:", "MemoryFact"),
    ("note:", "Note"),
)


class ChatGPTImportValidationError(ImporterValidationError):
    """Raised when a ChatGPT import payload is invalid."""


def _truncate(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."


def _build_title(*, object_type: str, text: str, explicit_title: str | None) -> str:
    if explicit_title is not None:
        return _truncate(explicit_title, max_length=280)
    prefix = OBJECT_TYPE_TO_PREFIX[object_type]
    return _truncate(f"{prefix}: {text}", max_length=280)


def _build_raw_content(*, object_type: str, text: str) -> str:
    prefix = OBJECT_TYPE_TO_PREFIX[object_type]
    return f"{prefix}: {text}"


def _parse_json(path: Path, raw_text: str) -> object:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ChatGPTImportValidationError(
            f"invalid JSON at {path}: {exc.msg}"
        ) from exc


def _read_chatgpt_source_files(source: str | Path) -> tuple[Path, list[Path]]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise ChatGPTImportValidationError(f"ChatGPT source path does not exist: {source_path}")

    source_files = (
        [source_path]
        if source_path.is_file()
        else contained_source_files(
            source_path,
            suffixes=(".json",),
            recursive=True,
            error_factory=ChatGPTImportValidationError,
        )
    )
    if not source_files:
        raise ChatGPTImportValidationError("no ChatGPT JSON files were found at the source path")
    return source_path, source_files


def _snapshot_chatgpt_source(
    source: str | Path,
    *,
    max_file_bytes: int,
) -> tuple[Path, list[ImportSourceFile]]:
    """Select the ChatGPT export files and read each of them exactly once.

    ``max_file_bytes`` is required: it is the size a file may be before it is
    refused unread, and every caller has to say what it is.
    """

    source_path, source_files = _read_chatgpt_source_files(source)
    if source_path.is_file():
        return source_path, [
            ImportSourceFile(
                path=source_path,
                relative_path=source_path.name,
                text=read_contained_source_text(
                    source_path,
                    max_bytes=max_file_bytes,
                    error_factory=ChatGPTImportValidationError,
                ),
            )
        ]
    return source_path, snapshot_source_files(
        source_path,
        source_files,
        max_bytes=max_file_bytes,
        error_factory=ChatGPTImportValidationError,
    )


def _normalize_message_text(value: object) -> str | None:
    if isinstance(value, str):
        return normalize_optional_text(value)

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            normalized = _normalize_message_text(item)
            if normalized is None:
                continue
            parts.append(normalized)
        if not parts:
            return None
        return normalize_optional_text(" ".join(parts))

    if isinstance(value, dict):
        content = as_json_object(value)
        for key in ("text", "content", "message"):
            normalized = _normalize_message_text(content.get(key))
            if normalized is not None:
                return normalized
        normalized = _normalize_message_text(content.get("parts"))
        if normalized is not None:
            return normalized
        return None

    return None


def _resolve_object_type_and_text(*, text: str, type_hint: object) -> tuple[str, str]:
    hinted_type = normalize_object_type(type_hint) if type_hint is not None else None
    if hinted_type is not None and hinted_type != "Note":
        return hinted_type, text

    lowered = text.casefold()
    for prefix, object_type in _PREFIX_TO_OBJECT_TYPE:
        if not lowered.startswith(prefix):
            continue
        stripped = normalize_optional_text(text[len(prefix) :])
        if stripped is None:
            raise ChatGPTImportValidationError("ChatGPT message content must not be empty")
        return object_type, stripped

    if hinted_type is not None:
        return hinted_type, text

    return "Note", text


def _extract_messages_from_simple_list(messages: object) -> list[JsonObject]:
    if not isinstance(messages, list):
        return []

    output: list[JsonObject] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        output.append(as_json_object(message))
    return output


def _extract_messages_from_mapping(mapping: object) -> list[JsonObject]:
    if not isinstance(mapping, dict):
        return []

    nodes: list[tuple[float, str, JsonObject]] = []
    for key, raw_node in mapping.items():
        if not isinstance(raw_node, dict):
            continue
        node = as_json_object(raw_node)
        message = as_json_object(node.get("message"))
        if not message:
            continue

        raw_created_at = message.get("create_time", node.get("create_time"))
        created_at = 0.0
        if isinstance(raw_created_at, (int, float)):
            created_at = float(raw_created_at)
        elif isinstance(raw_created_at, str):
            try:
                created_at = float(raw_created_at.strip())
            except ValueError:
                created_at = 0.0

        message_id = normalize_optional_text(message.get("id")) or normalize_optional_text(key) or ""
        nodes.append((created_at, message_id, message))

    nodes.sort(key=lambda item: (item[0], item[1]))
    return [node[2] for node in nodes]


def _extract_conversations(payload: object) -> list[JsonObject]:
    if isinstance(payload, list):
        return [as_json_object(item) for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise ChatGPTImportValidationError("ChatGPT source root must be a JSON object or array")

    payload_object = as_json_object(payload)
    for key in ("conversations", "items", "records"):
        raw_conversations = payload_object.get(key)
        if raw_conversations is None:
            continue
        if not isinstance(raw_conversations, list):
            raise ChatGPTImportValidationError(f"{key} must be a JSON array")
        return [as_json_object(item) for item in raw_conversations if isinstance(item, dict)]

    if payload_object.get("mapping") is not None or payload_object.get("messages") is not None:
        return [payload_object]

    raise ChatGPTImportValidationError(
        "ChatGPT payload must include one of: conversations, items, records, mapping, or messages"
    )


def _extract_workspace_metadata(payload: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None, None

    payload_object = as_json_object(payload)
    fixture_id = normalize_optional_text(payload_object.get("fixture_id"))
    workspace_payload = as_json_object(payload_object.get("workspace"))

    workspace_id = normalize_optional_text(
        workspace_payload.get("id")
    )
    workspace_name = normalize_optional_text(
        workspace_payload.get("name")
    )

    return fixture_id, workspace_id, workspace_name


def _conversation_messages(conversation: JsonObject) -> list[JsonObject]:
    messages = _extract_messages_from_simple_list(conversation.get("messages"))
    if messages:
        return messages
    return _extract_messages_from_mapping(conversation.get("mapping"))


def _message_role(message: JsonObject) -> str | None:
    author = as_json_object(message.get("author"))
    role = normalize_optional_text(author.get("role"))
    if role is not None:
        return role.casefold()
    direct_role = normalize_optional_text(message.get("role"))
    if direct_role is None:
        return None
    return direct_role.casefold()


def _message_text(message: JsonObject) -> str | None:
    content_payload = message.get("content")
    if isinstance(content_payload, dict):
        content = as_json_object(content_payload)
        parts = content.get("parts")
        normalized = _normalize_message_text(parts)
        if normalized is not None:
            return normalized
        normalized = _normalize_message_text(content.get("text"))
        if normalized is not None:
            return normalized

    for key in ("text", "message", "content"):
        normalized = _normalize_message_text(message.get(key))
        if normalized is not None:
            return normalized

    return None


def load_chatgpt_payload(
    source: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
) -> ImporterNormalizedBatch:
    source_path, snapshot = _snapshot_chatgpt_source(source, max_file_bytes=max_file_bytes)
    return _load_chatgpt_batch(source_path, snapshot)


def _load_chatgpt_batch(
    source_path: Path,
    snapshot: list[ImportSourceFile],
) -> ImporterNormalizedBatch:
    fixture_id: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None

    items: list[ImporterNormalizedItem] = []

    for source_file in snapshot:
        payload = _parse_json(source_file.path, source_file.text)

        maybe_fixture_id, maybe_workspace_id, maybe_workspace_name = _extract_workspace_metadata(payload)
        if fixture_id is None:
            fixture_id = maybe_fixture_id
        if workspace_id is None:
            workspace_id = maybe_workspace_id
        if workspace_name is None:
            workspace_name = maybe_workspace_name

        conversations = _extract_conversations(payload)
        for conversation_index, conversation in enumerate(conversations, start=1):
            conversation_id = normalize_optional_text(
                conversation.get("id")
            ) or f"conversation-{conversation_index}"
            conversation_title = normalize_optional_text(conversation.get("title"))
            conversation_project = normalize_optional_text(conversation.get("project"))
            conversation_person = normalize_optional_text(conversation.get("person"))

            messages = _conversation_messages(conversation)
            for message_index, message in enumerate(messages, start=1):
                role = _message_role(message)
                if role in {"system", "assistant", "user"}:
                    pass
                elif role is not None:
                    continue

                text = _message_text(message)
                if text is None:
                    continue

                object_type, object_text = _resolve_object_type_and_text(
                    text=text,
                    type_hint=message.get("object_type"),
                )

                status = parse_optional_status(message.get("status")) or "active"
                confidence = parse_optional_confidence(message.get("confidence"))
                if confidence is None:
                    confidence = _DEFAULT_CONFIDENCE

                message_id = normalize_optional_text(message.get("id")) or f"{conversation_id}:{message_index}"
                source_item_id = f"{conversation_id}:{message_id}"

                explicit_title = normalize_optional_text(message.get("title"))
                if explicit_title is None:
                    explicit_title = conversation_title
                title = _build_title(
                    object_type=object_type,
                    text=object_text,
                    explicit_title=explicit_title,
                )

                body_key = OBJECT_TYPE_TO_BODY_KEY[object_type]
                body: JsonObject = {
                    body_key: object_text,
                    "raw_import_text": object_text,
                    "chatgpt_role": role,
                    "chatgpt_conversation_id": conversation_id,
                    "chatgpt_message_id": message_id,
                }

                source_provenance: JsonObject = {
                    "thread_id": conversation_id,
                    "chatgpt_conversation_id": conversation_id,
                    "chatgpt_message_id": message_id,
                }
                if role is not None:
                    source_provenance["chatgpt_role"] = role
                if conversation_project is not None:
                    source_provenance["project"] = conversation_project
                if conversation_person is not None:
                    source_provenance["person"] = conversation_person

                source_event_ids = [f"chatgpt-event:{conversation_id}:{message_id}"]
                source_provenance["source_event_ids"] = cast(JsonValue, source_event_ids)

                dedupe_payload = merge_json_objects(
                    {
                        "workspace_id": workspace_id or source_path.stem,
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "object_type": object_type,
                        "status": status,
                        "title": title,
                        "body": body,
                    },
                    source_provenance,
                )

                items.append(
                    ImporterNormalizedItem(
                        source_item_id=source_item_id,
                        source_file=source_file.relative_path,
                        source_locator={
                            "conversation_id": conversation_id,
                            "message_id": message_id,
                            "role": role,
                        },
                        source_segment_text=text,
                        source_segment_kind="chatgpt_message",
                        object_type=object_type,
                        status=status,
                        raw_content=_build_raw_content(object_type=object_type, text=object_text),
                        title=title,
                        body=body,
                        confidence=confidence,
                        source_provenance=source_provenance,
                        dedupe_key=dedupe_key_for_payload(dedupe_payload),
                    )
                )

    if not items:
        raise ChatGPTImportValidationError("ChatGPT source did not contain any importable messages")

    resolved_workspace_id = workspace_id or f"chatgpt-{source_path.stem}"
    return ImporterNormalizedBatch(
        context=ImporterWorkspaceContext(
            fixture_id=fixture_id,
            workspace_id=resolved_workspace_id,
            workspace_name=workspace_name,
            source_path=str(source_path),
        ),
        items=items,
    )


def import_chatgpt_source(
    store: ContinuityStore,
    *,
    user_id: UUID,
    source: str | Path,
    max_file_bytes: int = DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
) -> JsonObject:
    # One snapshot feeds both the evidence archive and the parse, so the
    # archived text is the text that was imported. It is decoded text and not
    # the disk bytes: the read is text mode, so CRLF arrives as LF and the
    # archive will not checksum against the original file.
    source_path, snapshot = _snapshot_chatgpt_source(source, max_file_bytes=max_file_bytes)
    archived_artifacts = archive_import_source_files(
        store,
        user_id=user_id,
        source_kind="chatgpt_import",
        import_source_path=str(source_path),
        files=[
            SourceArtifactArchiveInput(
                relative_path=source_file.relative_path,
                display_name=source_file.path.name,
                media_type="application/json",
                content_text=source_file.text,
            )
            for source_file in snapshot
        ],
    )
    batch = _load_chatgpt_batch(source_path, snapshot)
    return import_normalized_batch(
        store,
        user_id=user_id,
        batch=batch,
        config=ImportPersistenceConfig(
            source_kind="chatgpt_import",
            source_prefix="chatgpt",
            admission_reason="chatgpt_import",
            dedupe_key_field="chatgpt_dedupe_key",
            dedupe_posture=_DEFAULT_DEDUPE_POSTURE,
        ),
        archived_artifacts=archived_artifacts,
    )


__all__ = ["ChatGPTImportValidationError", "import_chatgpt_source", "load_chatgpt_payload"]
