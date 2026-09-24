from __future__ import annotations

from pathlib import Path
import re
from typing import cast
from uuid import UUID

from alicebot_api.continuity_evidence import SourceArtifactArchiveInput, archive_import_source_files
from alicebot_api.credential_floor import private_key_armor_role
from alicebot_api.importer_models import (
    ImporterNormalizedBatch,
    ImporterNormalizedItem,
    ImporterValidationError,
    ImporterWorkspaceContext,
    OBJECT_TYPE_TO_BODY_KEY,
    OBJECT_TYPE_TO_PREFIX,
    dedupe_key_for_payload,
    merge_json_objects,
    normalize_object_type,
    normalize_optional_text,
    parse_optional_confidence,
    parse_optional_status,
)
from alicebot_api.importer_paths import (
    ImportSourceFile,
    contained_source_files,
    read_contained_source_text,
    snapshot_source_files,
)
from alicebot_api.importers.common import ImportPersistenceConfig, import_normalized_batch
from alicebot_api.store import ContinuityStore, JsonObject, JsonValue


_DEFAULT_CONFIDENCE = 0.84
_DEFAULT_DEDUPE_POSTURE = "workspace_and_line_fingerprint"
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


class MarkdownImportValidationError(ImporterValidationError):
    """Raised when a markdown import payload is invalid."""


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


def _strip_list_prefix(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("- ") or stripped.startswith("* "):
        return stripped[2:].strip()
    numbered = re.match(r"^\d+\.\s+(.*)$", stripped)
    if numbered:
        return numbered.group(1).strip()
    return stripped


def _parse_frontmatter(raw_text: str) -> tuple[dict[str, str], list[str]]:
    lines = raw_text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, lines

    metadata: dict[str, str] = {}
    closing_index = -1
    for index in range(1, len(lines)):
        line = lines[index].strip()
        if line == "---":
            closing_index = index
            break
        if line == "" or line.startswith("#"):
            continue
        if ":" not in line:
            raise MarkdownImportValidationError("frontmatter lines must use key: value format")
        key, value = line.split(":", 1)
        normalized_key = normalize_optional_text(key)
        normalized_value = normalize_optional_text(value)
        if normalized_key is None or normalized_value is None:
            continue
        metadata[normalized_key.casefold().replace("-", "_")] = normalized_value

    if closing_index == -1:
        raise MarkdownImportValidationError("markdown frontmatter must be closed with ---")

    return metadata, lines[closing_index + 1 :]


def _armored_private_key_ranges(lines: list[str]) -> dict[int, tuple[int, int]]:
    """Map each 1-based line of a dashed private-key block to ``(start, end)``.

    The importer makes one item per line, and the credential check then skips
    only the BEGIN line. The base64 body and the END line would be stored.
    A block is the BEGIN line through the END line with the same label. A
    BEGIN line with no matching END is not a block: that line is still one
    item, and the check skips it on its own.
    """

    ranges: dict[int, tuple[int, int]] = {}
    index = 0
    while index < len(lines):
        role = private_key_armor_role(lines[index])
        if role is None or role[0] != "begin":
            index += 1
            continue
        label = role[1]
        end_index: int | None = None
        cursor = index + 1
        while cursor < len(lines):
            later = private_key_armor_role(lines[cursor])
            if later is not None and later == ("end", label):
                end_index = cursor
                break
            cursor += 1
        if end_index is None:
            index += 1
            continue
        start_no = index + 1
        end_no = end_index + 1
        for line_no in range(start_no, end_no + 1):
            ranges[line_no] = (start_no, end_no)
        index = end_index + 1
    return ranges


def _read_markdown_source(source: str | Path) -> tuple[Path, list[Path]]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.exists():
        raise MarkdownImportValidationError(f"markdown source path does not exist: {source_path}")

    if source_path.is_file():
        if source_path.suffix.casefold() != ".md":
            raise MarkdownImportValidationError("markdown source file must end with .md")
        return source_path, [source_path]

    files = contained_source_files(
        source_path,
        suffixes=(".md",),
        recursive=True,
        error_factory=MarkdownImportValidationError,
    )
    if not files:
        raise MarkdownImportValidationError(f"no markdown files were found at {source_path}")
    return source_path, files


def _snapshot_markdown_source(source: str | Path) -> tuple[Path, list[ImportSourceFile]]:
    """Select the markdown files and read each of them exactly once."""

    source_path, markdown_files = _read_markdown_source(source)
    if source_path.is_file():
        return source_path, [
            ImportSourceFile(
                path=source_path,
                relative_path=source_path.name,
                text=read_contained_source_text(
                    source_path,
                    error_factory=MarkdownImportValidationError,
                ),
            )
        ]
    return source_path, snapshot_source_files(
        source_path,
        markdown_files,
        error_factory=MarkdownImportValidationError,
    )


def _resolve_object_type_and_text(*, text: str, type_hint: str | None) -> tuple[str, str]:
    if type_hint is not None:
        return normalize_object_type(type_hint), text

    lowered = text.casefold()
    for prefix, object_type in _PREFIX_TO_OBJECT_TYPE:
        if not lowered.startswith(prefix):
            continue
        stripped = normalize_optional_text(text[len(prefix) :])
        if stripped is None:
            raise MarkdownImportValidationError("markdown entry content must not be empty")
        return object_type, stripped

    return "Note", text


def _parse_line_tags(line: str) -> tuple[str, dict[str, str]]:
    segments = [segment.strip() for segment in line.split("|")]
    text_segment = segments[0]
    tags: dict[str, str] = {}
    for segment in segments[1:]:
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        normalized_key = normalize_optional_text(key)
        normalized_value = normalize_optional_text(value)
        if normalized_key is None or normalized_value is None:
            continue
        tags[normalized_key.casefold().replace("-", "_")] = normalized_value
    return text_segment, tags


def _merge_source_event_ids(*, existing: list[str], maybe_csv: str | None, single: str | None) -> list[str]:
    output = list(existing)
    seen = set(output)

    if maybe_csv is not None:
        for part in maybe_csv.split(","):
            normalized = normalize_optional_text(part)
            if normalized is None or normalized in seen:
                continue
            output.append(normalized)
            seen.add(normalized)

    normalized_single = normalize_optional_text(single)
    if normalized_single is not None and normalized_single not in seen:
        output.append(normalized_single)

    return output


def load_markdown_payload(source: str | Path) -> ImporterNormalizedBatch:
    source_path, snapshot = _snapshot_markdown_source(source)
    return _load_markdown_batch(source_path, snapshot)


def _load_markdown_batch(
    source_path: Path,
    snapshot: list[ImportSourceFile],
) -> ImporterNormalizedBatch:
    fixture_id: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    default_status: str = "active"
    default_confidence = _DEFAULT_CONFIDENCE
    default_scope: JsonObject = {}

    items: list[ImporterNormalizedItem] = []

    for source_file in snapshot:
        file_path = source_file.path
        metadata, lines = _parse_frontmatter(source_file.text)

        if fixture_id is None:
            fixture_id = normalize_optional_text(metadata.get("fixture_id"))
        if workspace_id is None:
            workspace_id = normalize_optional_text(metadata.get("workspace_id"))
        if workspace_name is None:
            workspace_name = normalize_optional_text(metadata.get("workspace_name"))

        maybe_default_status = parse_optional_status(metadata.get("default_status"))
        if maybe_default_status is not None:
            default_status = maybe_default_status

        maybe_default_confidence = parse_optional_confidence(metadata.get("default_confidence"))
        if maybe_default_confidence is not None:
            default_confidence = maybe_default_confidence

        file_scope: JsonObject = {}
        for key in ("thread_id", "task_id", "project", "person", "confirmation_status"):
            value = normalize_optional_text(metadata.get(key))
            if value is not None:
                file_scope[key] = value if key != "confirmation_status" else value.casefold()

        armored_ranges = _armored_private_key_ranges(lines)
        consumed_through = 0

        for line_number, raw_line in enumerate(lines, start=1):
            if line_number <= consumed_through:
                continue
            armored_span = armored_ranges.get(line_number)
            if armored_span is not None:
                start_no, end_no = armored_span
                block_text = "\n".join(lines[start_no - 1 : end_no])
                source_item_id = f"{file_path.name}:{start_no}-{end_no}"
                title = _build_title(object_type="Note", text=block_text, explicit_title=None)
                body: JsonObject = {
                    "body": block_text,
                    "raw_import_text": block_text,
                    "markdown_raw_line": block_text,
                    "markdown_line_number": start_no,
                    "markdown_line_end": end_no,
                    "markdown_source_file": file_path.name,
                }
                source_provenance = merge_json_objects(
                    default_scope,
                    file_scope,
                    {"markdown_source_relpath": source_file.relative_path},
                )
                dedupe_payload: JsonObject = {
                    "workspace_id": workspace_id or source_path.stem,
                    "object_type": "Note",
                    "status": default_status,
                    "title": title,
                    "body": {
                        "body": block_text,
                        "raw_import_text": block_text,
                    },
                    "source_provenance": source_provenance,
                }
                items.append(
                    ImporterNormalizedItem(
                        source_item_id=source_item_id,
                        source_file=source_file.relative_path,
                        source_locator={
                            "line_number": start_no,
                            "line_end": end_no,
                            "source_item_id": source_item_id,
                        },
                        source_segment_text=block_text,
                        source_segment_kind="markdown_armored_private_key",
                        object_type="Note",
                        status=default_status,
                        raw_content=_build_raw_content(object_type="Note", text=block_text),
                        title=title,
                        body=body,
                        confidence=default_confidence,
                        source_provenance=source_provenance,
                        dedupe_key=dedupe_key_for_payload(dedupe_payload),
                    )
                )
                consumed_through = end_no
                continue

            stripped = _strip_list_prefix(raw_line)
            normalized_line = normalize_optional_text(stripped)
            if normalized_line is None:
                continue
            if normalized_line.startswith("#"):
                continue

            content_segment, tags = _parse_line_tags(normalized_line)
            normalized_content = normalize_optional_text(content_segment)
            if normalized_content is None:
                continue

            object_type, object_text = _resolve_object_type_and_text(
                text=normalized_content,
                type_hint=tags.get("type"),
            )
            status = parse_optional_status(tags.get("status")) or default_status
            confidence = parse_optional_confidence(tags.get("confidence"))
            if confidence is None:
                confidence = default_confidence

            source_item_id = normalize_optional_text(tags.get("id")) or f"{file_path.name}:{line_number}"
            title = _build_title(
                object_type=object_type,
                text=object_text,
                explicit_title=normalize_optional_text(tags.get("title")),
            )

            body_key = OBJECT_TYPE_TO_BODY_KEY[object_type]
            body: JsonObject = {
                body_key: object_text,
                "raw_import_text": object_text,
                "markdown_raw_line": raw_line,
                "markdown_line_number": line_number,
                "markdown_source_file": file_path.name,
            }

            source_provenance = merge_json_objects(
                default_scope,
                file_scope,
                {"markdown_source_relpath": source_file.relative_path},
            )

            for key in ("thread_id", "task_id", "project", "person", "confirmation_status"):
                value = normalize_optional_text(tags.get(key))
                if value is None:
                    continue
                source_provenance[key] = value if key != "confirmation_status" else value.casefold()

            source_event_ids = _merge_source_event_ids(
                existing=[],
                maybe_csv=tags.get("source_event_ids"),
                single=tags.get("source_event_id"),
            )
            if source_event_ids:
                source_provenance["source_event_ids"] = cast(JsonValue, source_event_ids)

            dedupe_payload: JsonObject = {
                "workspace_id": workspace_id or source_path.stem,
                "object_type": object_type,
                "status": status,
                "title": title,
                "body": {
                    body_key: object_text,
                    "raw_import_text": object_text,
                },
                "source_provenance": source_provenance,
            }

            items.append(
                ImporterNormalizedItem(
                    source_item_id=source_item_id,
                    source_file=source_file.relative_path,
                    source_locator={"line_number": line_number, "source_item_id": source_item_id},
                    source_segment_text=raw_line,
                    source_segment_kind="markdown_line",
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

    resolved_workspace_id = workspace_id or source_path.stem
    if not items:
        raise MarkdownImportValidationError("markdown source did not contain any importable entries")

    return ImporterNormalizedBatch(
        context=ImporterWorkspaceContext(
            fixture_id=fixture_id,
            workspace_id=resolved_workspace_id,
            workspace_name=workspace_name,
            source_path=str(source_path),
        ),
        items=items,
    )


def import_markdown_source(
    store: ContinuityStore,
    *,
    user_id: UUID,
    source: str | Path,
) -> JsonObject:
    # One snapshot feeds both the evidence archive and the parse, so the
    # archived text is the text that was imported. It is decoded text and not
    # the disk bytes: the read is text mode, so CRLF arrives as LF and the
    # archive will not checksum against the original file.
    source_path, snapshot = _snapshot_markdown_source(source)
    archived_artifacts = archive_import_source_files(
        store,
        user_id=user_id,
        source_kind="markdown_import",
        import_source_path=str(source_path),
        files=[
            SourceArtifactArchiveInput(
                relative_path=source_file.relative_path,
                display_name=source_file.path.name,
                media_type="text/markdown",
                content_text=source_file.text,
            )
            for source_file in snapshot
        ],
    )
    batch = _load_markdown_batch(source_path, snapshot)
    return import_normalized_batch(
        store,
        user_id=user_id,
        batch=batch,
        config=ImportPersistenceConfig(
            source_kind="markdown_import",
            source_prefix="markdown",
            admission_reason="markdown_import",
            dedupe_key_field="markdown_dedupe_key",
            dedupe_posture=_DEFAULT_DEDUPE_POSTURE,
        ),
        archived_artifacts=archived_artifacts,
    )


__all__ = ["MarkdownImportValidationError", "import_markdown_source", "load_markdown_payload"]
