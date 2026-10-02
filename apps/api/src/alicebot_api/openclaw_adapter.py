from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import cast

from alicebot_api.importer_paths import (
    DEFAULT_MAX_TEXT_FILE_BYTES,
    ImportSourceFile,
    contained_source_files,
    read_contained_source_text,
    snapshot_source_files,
)
from alicebot_api.openclaw_models import (
    OpenClawAdapterValidationError,
    OpenClawNormalizedBatch,
    OpenClawNormalizedItem,
    OpenClawWorkspaceContext,
    as_json_object,
    canonical_json_string,
    ensure_json_object,
    merge_json_objects,
    normalize_optional_text,
    parse_optional_confidence,
    parse_optional_status,
    pick_first_text,
    to_string_list,
)
from alicebot_api.store import JsonObject, JsonValue


_OPENCLAW_TYPE_TO_OBJECT_TYPE: dict[str, str] = {
    "decision": "Decision",
    "decisions": "Decision",
    "task": "NextAction",
    "next": "NextAction",
    "next_action": "NextAction",
    "nextaction": "NextAction",
    "action": "NextAction",
    "commitment": "Commitment",
    "waiting": "WaitingFor",
    "waiting_for": "WaitingFor",
    "waitingfor": "WaitingFor",
    "blocker": "Blocker",
    "fact": "MemoryFact",
    "memory_fact": "MemoryFact",
    "memory": "MemoryFact",
    "note": "Note",
}

_OBJECT_TYPE_TO_BODY_KEY: dict[str, str] = {
    "Note": "body",
    "MemoryFact": "fact_text",
    "Decision": "decision_text",
    "Commitment": "commitment_text",
    "WaitingFor": "waiting_for_text",
    "Blocker": "blocking_reason",
    "NextAction": "action_text",
}

_OBJECT_TYPE_TO_PREFIX: dict[str, str] = {
    "Decision": "Decision",
    "Commitment": "Commitment",
    "WaitingFor": "Waiting For",
    "Blocker": "Blocker",
    "NextAction": "Next Action",
    "MemoryFact": "Memory Fact",
    "Note": "Note",
}

_DEFAULT_CONFIDENCE = 0.82
_SUPPORTED_WORKSPACE_FILENAMES = (
    "workspace.json",
    "openclaw_workspace.json",
)
_SUPPORTED_MEMORY_FILENAMES = (
    "durable_memory.json",
    "memories.json",
    "openclaw_memories.json",
)


def _truncate(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."


def _normalize_object_type(value: object) -> str:
    normalized = normalize_optional_text(value)
    if normalized is None:
        return "Note"

    if normalized in _OBJECT_TYPE_TO_BODY_KEY:
        return normalized

    lowered = normalized.casefold().replace("-", "_").replace(" ", "_")
    return _OPENCLAW_TYPE_TO_OBJECT_TYPE.get(lowered, "Note")


def _build_body(*, object_type: str, text: str, raw_entry: JsonObject) -> JsonObject:
    body_key = _OBJECT_TYPE_TO_BODY_KEY[object_type]
    return {
        body_key: text,
        "raw_import_text": text,
        "openclaw_raw_entry": raw_entry,
    }


def _build_title(*, object_type: str, text: str, explicit_title: str | None) -> str:
    if explicit_title is not None:
        return _truncate(explicit_title, max_length=280)
    prefix = _OBJECT_TYPE_TO_PREFIX[object_type]
    return _truncate(f"{prefix}: {text}", max_length=280)


def _build_raw_content(*, object_type: str, text: str) -> str:
    prefix = _OBJECT_TYPE_TO_PREFIX[object_type]
    return f"{prefix}: {text}"


def _parse_json(path: Path, raw_text: str) -> object:
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise OpenClawAdapterValidationError(
            f"invalid JSON at {path}: {exc.msg}"
        ) from exc


def _extract_workspace_payloads(payload: object) -> tuple[JsonObject | None, list[JsonObject]]:
    if isinstance(payload, list):
        array_entries = [ensure_json_object(item, field_name="entry") for item in payload]
        return None, array_entries

    if not isinstance(payload, dict):
        raise OpenClawAdapterValidationError("OpenClaw source root must be a JSON object or array")

    workspace_payload = payload.get("workspace")
    if workspace_payload is not None and not isinstance(workspace_payload, dict):
        raise OpenClawAdapterValidationError("workspace must be a JSON object when provided")
    workspace_json = as_json_object(workspace_payload) if workspace_payload is not None else None

    entries: list[JsonObject] = []
    found_memory_key = False
    for key in ("durable_memory", "memories", "items", "records"):
        raw_entries = payload.get(key)
        if raw_entries is None:
            continue
        found_memory_key = True
        if not isinstance(raw_entries, list):
            raise OpenClawAdapterValidationError(f"{key} must be a JSON array")
        entries.extend(ensure_json_object(item, field_name=f"{key}[]") for item in raw_entries)

    if found_memory_key:
        return workspace_json, entries

    if workspace_payload is not None:
        return workspace_json, []

    # Single-record convenience format.
    if payload.get("content") is not None or payload.get("text") is not None:
        return None, [ensure_json_object(payload, field_name="payload")]

    raise OpenClawAdapterValidationError(
        "OpenClaw payload must include one of: durable_memory, memories, items, or records"
    )


def _extract_scope_provenance(entry: JsonObject, *, raw_provenance: JsonObject) -> JsonObject:
    entry_context = as_json_object(entry.get("context"))

    thread_id = pick_first_text(
        entry.get("thread_id"),
        raw_provenance.get("thread_id"),
        entry_context.get("thread_id"),
    )
    task_id = pick_first_text(
        entry.get("task_id"),
        raw_provenance.get("task_id"),
        entry_context.get("task_id"),
    )
    project = pick_first_text(
        entry.get("project"),
        entry.get("project_name"),
        raw_provenance.get("project"),
        entry_context.get("project"),
        entry_context.get("project_name"),
    )
    person = pick_first_text(
        entry.get("person"),
        entry.get("owner"),
        raw_provenance.get("person"),
        raw_provenance.get("owner"),
        entry_context.get("person"),
        entry_context.get("owner"),
    )
    confirmation_status = pick_first_text(
        entry.get("confirmation_status"),
        raw_provenance.get("confirmation_status"),
        entry_context.get("confirmation_status"),
    )

    source_event_ids = to_string_list(entry.get("source_event_ids"))
    if not source_event_ids:
        source_event_ids = to_string_list(raw_provenance.get("source_event_ids"))
    if not source_event_ids:
        source_event_ids = to_string_list(entry_context.get("source_event_ids"))

    payload: JsonObject = {}
    if thread_id is not None:
        payload["thread_id"] = thread_id
    if task_id is not None:
        payload["task_id"] = task_id
    if project is not None:
        payload["project"] = project
    if person is not None:
        payload["person"] = person
    if confirmation_status is not None:
        payload["confirmation_status"] = confirmation_status.casefold()
    if source_event_ids:
        payload["source_event_ids"] = cast(JsonValue, source_event_ids)

    tags = to_string_list(entry.get("tags"))
    if tags:
        payload["openclaw_tags"] = cast(JsonValue, tags)

    return payload


def _item_text(entry: JsonObject) -> str:
    text = pick_first_text(
        entry.get("text"),
        entry.get("content"),
        entry.get("summary"),
        entry.get("message"),
    )
    if text is None:
        raise OpenClawAdapterValidationError("OpenClaw entry must include text/content/summary/message")
    return text


def _normalize_entry(
    *,
    entry: JsonObject,
    source_file: str,
    entry_index: int,
    workspace_id: str,
) -> OpenClawNormalizedItem:
    source_identifier = pick_first_text(
        entry.get("id"),
        entry.get("memory_id"),
        entry.get("entry_id"),
    )
    source_item_id = source_identifier if source_identifier is not None else f"{source_file}:{entry_index + 1}"

    object_type = _normalize_object_type(
        pick_first_text(
            entry.get("object_type"),
            entry.get("type"),
            entry.get("kind"),
            entry.get("category"),
        )
    )
    status = parse_optional_status(entry.get("status")) or "active"

    text = _item_text(entry)
    title = _build_title(
        object_type=object_type,
        text=text,
        explicit_title=pick_first_text(entry.get("title")),
    )
    raw_entry = as_json_object(entry)

    raw_provenance = as_json_object(entry.get("provenance"))
    source_provenance = merge_json_objects(
        _extract_scope_provenance(entry, raw_provenance=raw_provenance),
        {
            "openclaw_record_type": pick_first_text(
                entry.get("type"),
                entry.get("kind"),
                entry.get("category"),
            )
            or "unknown",
        },
    )
    if source_identifier is not None:
        source_provenance["openclaw_source_identifier"] = source_identifier

    confidence = parse_optional_confidence(entry.get("confidence"))
    if confidence is None:
        confidence = parse_optional_confidence(raw_provenance.get("confidence"))
    if confidence is None:
        confidence = _DEFAULT_CONFIDENCE

    dedupe_payload: JsonObject = {
        "workspace_id": workspace_id,
        "source_identifier": source_identifier,
        "object_type": object_type,
        "status": status,
        "title": title,
        "body": _build_body(object_type=object_type, text=text, raw_entry=raw_entry),
        "source_provenance": source_provenance,
    }
    dedupe_key = sha256(canonical_json_string(dedupe_payload).encode("utf-8")).hexdigest()

    return OpenClawNormalizedItem(
        source_item_id=source_item_id,
        source_file=source_file,
        source_locator={
            "source_identifier": source_identifier,
            "entry_index": entry_index + 1,
        },
        source_segment_text=canonical_json_string(raw_entry),
        source_segment_kind="openclaw_entry",
        object_type=object_type,
        status=status,
        raw_content=_build_raw_content(object_type=object_type, text=text),
        title=title,
        body=_build_body(object_type=object_type, text=text, raw_entry=raw_entry),
        confidence=confidence,
        source_provenance=source_provenance,
        dedupe_key=dedupe_key,
    )


def _extract_context(
    *,
    source_path: Path,
    workspace_payload: JsonObject | None,
    fallback_fixture_id: str | None,
) -> OpenClawWorkspaceContext:
    payload = workspace_payload or {}
    workspace_id = pick_first_text(
        payload.get("id"),
        payload.get("workspace_id"),
        fallback_fixture_id,
        source_path.stem,
    )
    if workspace_id is None:
        raise OpenClawAdapterValidationError("workspace id could not be resolved")

    return OpenClawWorkspaceContext(
        fixture_id=fallback_fixture_id,
        workspace_id=workspace_id,
        workspace_name=pick_first_text(payload.get("name"), payload.get("title")),
        source_path=str(source_path),
    )


def _select_openclaw_source_paths(candidates: list[Path]) -> list[Path]:
    """Narrow a directory listing to the files the adapter will actually parse.

    Selection runs on the listing rather than on already-read text, for two
    reasons. An unrelated neighbour is never opened or decoded, so a stray
    ``.json`` file with invalid UTF-8 no longer fails an import that had no
    reason to look at it. And the set that gets archived becomes the same set
    the parse is handed, because both are driven by the one snapshot taken
    after this narrowing, so the parse can no longer reach content that was
    never archived.

    The reverse can still happen and is harmless: when a directory holds more
    than one workspace-named file, every one of them is archived while the
    parse consumes the first, so the archive is a superset. Nothing is imported
    from outside it, which is the direction that matters.
    """

    by_name = {path.name: path for path in candidates}
    named = [
        by_name[filename]
        for filename in (*_SUPPORTED_WORKSPACE_FILENAMES, *_SUPPORTED_MEMORY_FILENAMES)
        if filename in by_name
    ]
    if named:
        return named
    if not candidates:
        raise OpenClawAdapterValidationError("no OpenClaw memory entries were found at the source path")
    return list(candidates)


def snapshot_openclaw_source(
    source: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES,
) -> tuple[Path, list[ImportSourceFile]]:
    """Read every JSON file the adapter will consult, exactly once.

    A directory source is read one level deep, which is the only level the
    selection rules below look at, and never through a symlink. Selection is
    applied to the listing first, so a file the adapter will not parse is not
    read either.
    """

    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise OpenClawAdapterValidationError(f"OpenClaw source path does not exist: {source_path}")

    if source_path.is_file():
        return source_path, [
            ImportSourceFile(
                path=source_path,
                relative_path=source_path.name,
                text=read_contained_source_text(
                    source_path,
                    max_bytes=max_file_bytes,
                    error_factory=OpenClawAdapterValidationError,
                ),
            )
        ]

    json_files = contained_source_files(
        source_path,
        suffixes=(".json",),
        recursive=False,
        error_factory=OpenClawAdapterValidationError,
    )
    return source_path, snapshot_source_files(
        source_path,
        _select_openclaw_source_paths(json_files),
        max_bytes=max_file_bytes,
        error_factory=OpenClawAdapterValidationError,
    )


def select_openclaw_source_files(
    source_path: Path,
    snapshot: list[ImportSourceFile],
) -> list[ImportSourceFile]:
    """Apply the adapter's file-selection rule to an already-read snapshot."""

    if source_path.is_file():
        return list(snapshot)

    files_by_name = {source_file.path.name: source_file for source_file in snapshot}
    named = [
        files_by_name[filename]
        for filename in (*_SUPPORTED_WORKSPACE_FILENAMES, *_SUPPORTED_MEMORY_FILENAMES)
        if filename in files_by_name
    ]
    if named:
        return named
    if not snapshot:
        raise OpenClawAdapterValidationError("no OpenClaw memory entries were found at the source path")
    return list(snapshot)


def list_openclaw_source_files(
    source: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES,
) -> tuple[Path, list[Path]]:
    source_path, snapshot = snapshot_openclaw_source(source, max_file_bytes=max_file_bytes)
    return source_path, [
        source_file.path for source_file in select_openclaw_source_files(source_path, snapshot)
    ]


def load_openclaw_payload(
    source: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES,
) -> OpenClawNormalizedBatch:
    source_path, snapshot = snapshot_openclaw_source(source, max_file_bytes=max_file_bytes)
    return load_openclaw_batch_from_snapshot(source_path, snapshot)


def load_openclaw_batch_from_snapshot(
    source_path: Path,
    snapshot: list[ImportSourceFile],
) -> OpenClawNormalizedBatch:
    entries_by_file: list[tuple[str, list[JsonObject]]] = []
    workspace_payload: JsonObject | None = None
    fixture_id: str | None = None

    if source_path.is_file():
        only_file = snapshot[0]
        payload = _parse_json(only_file.path, only_file.text)
        parsed_workspace, entries = _extract_workspace_payloads(payload)
        if isinstance(payload, dict):
            fixture_id = normalize_optional_text(payload.get("fixture_id"))
        workspace_payload = parsed_workspace
        entries_by_file.append((source_path.name, entries))
    else:
        files_by_name = {candidate.path.name: candidate for candidate in snapshot}

        for filename in _SUPPORTED_WORKSPACE_FILENAMES:
            candidate = files_by_name.get(filename)
            if candidate is None:
                continue
            payload = _parse_json(candidate.path, candidate.text)
            parsed_workspace, _ = _extract_workspace_payloads(payload)
            workspace_payload = parsed_workspace or workspace_payload
            if isinstance(payload, dict):
                fixture_id = fixture_id or normalize_optional_text(payload.get("fixture_id"))
            break

        for filename in _SUPPORTED_MEMORY_FILENAMES:
            candidate = files_by_name.get(filename)
            if candidate is None:
                continue
            payload = _parse_json(candidate.path, candidate.text)
            parsed_workspace, entries = _extract_workspace_payloads(payload)
            if parsed_workspace is not None:
                workspace_payload = parsed_workspace
            if isinstance(payload, dict):
                fixture_id = fixture_id or normalize_optional_text(payload.get("fixture_id"))
            entries_by_file.append((filename, entries))

        if not entries_by_file:
            for candidate in snapshot:
                payload = _parse_json(candidate.path, candidate.text)
                parsed_workspace, entries = _extract_workspace_payloads(payload)
                if parsed_workspace is not None:
                    workspace_payload = parsed_workspace
                if isinstance(payload, dict):
                    fixture_id = fixture_id or normalize_optional_text(payload.get("fixture_id"))
                if entries:
                    entries_by_file.append((candidate.path.name, entries))

    if not entries_by_file:
        raise OpenClawAdapterValidationError("no OpenClaw memory entries were found at the source path")

    context = _extract_context(
        source_path=source_path,
        workspace_payload=workspace_payload,
        fallback_fixture_id=fixture_id,
    )

    normalized_items: list[OpenClawNormalizedItem] = []
    for source_file, entries in entries_by_file:
        for index, entry in enumerate(entries):
            normalized_items.append(
                _normalize_entry(
                    entry=entry,
                    source_file=source_file,
                    entry_index=index,
                    workspace_id=context.workspace_id,
                )
            )

    if not normalized_items:
        raise OpenClawAdapterValidationError("OpenClaw source did not contain any importable entries")

    return OpenClawNormalizedBatch(
        context=context,
        items=normalized_items,
    )


__all__ = [
    "OpenClawAdapterValidationError",
    "list_openclaw_source_files",
    "load_openclaw_batch_from_snapshot",
    "load_openclaw_payload",
    "select_openclaw_source_files",
    "snapshot_openclaw_source",
]
