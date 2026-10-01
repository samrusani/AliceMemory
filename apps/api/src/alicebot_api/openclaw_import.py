from __future__ import annotations

from pathlib import Path
from uuid import UUID

from alicebot_api.continuity_evidence import SourceArtifactArchiveInput, archive_import_source_files
from alicebot_api.importer_models import (
    ImporterNormalizedBatch,
    ImporterNormalizedItem,
    ImporterWorkspaceContext,
)
from alicebot_api.importer_paths import DEFAULT_MAX_TEXT_FILE_BYTES, ImportSourceFile
from alicebot_api.importers.common import ImportPersistenceConfig, import_normalized_batch
from alicebot_api.openclaw_adapter import (
    load_openclaw_batch_from_snapshot,
    select_openclaw_source_files,
    snapshot_openclaw_source,
)
from alicebot_api.store import ContinuityStore, JsonObject


_OPENCLAW_DEDUPE_POSTURE = "workspace_and_payload_fingerprint"


def _to_generic_batch(source_path: Path, snapshot: list[ImportSourceFile]) -> ImporterNormalizedBatch:
    batch = load_openclaw_batch_from_snapshot(source_path, snapshot)
    return ImporterNormalizedBatch(
        context=ImporterWorkspaceContext(
            fixture_id=batch.context.fixture_id,
            workspace_id=batch.context.workspace_id,
            workspace_name=batch.context.workspace_name,
            source_path=batch.context.source_path,
        ),
        items=[
            ImporterNormalizedItem(
                source_item_id=item.source_item_id,
                source_file=item.source_file,
                source_locator=item.source_locator,
                source_segment_text=item.source_segment_text,
                source_segment_kind=item.source_segment_kind,
                object_type=item.object_type,
                status=item.status,
                raw_content=item.raw_content,
                title=item.title,
                body=item.body,
                confidence=item.confidence,
                source_provenance=item.source_provenance,
                dedupe_key=item.dedupe_key,
            )
            for item in batch.items
        ],
    )


def import_openclaw_source(
    store: ContinuityStore,
    *,
    user_id: UUID,
    source: str | Path,
    max_file_bytes: int = DEFAULT_MAX_TEXT_FILE_BYTES,
) -> JsonObject:
    # One snapshot feeds both the evidence archive and the parse, over one
    # selected file set, so the archived text is the text that was imported.
    # It is decoded text and not the disk bytes: the read is text mode, so CRLF
    # arrives as LF and the archive will not checksum against the original file.
    source_path, snapshot = snapshot_openclaw_source(source, max_file_bytes=max_file_bytes)
    archived_artifacts = archive_import_source_files(
        store,
        user_id=user_id,
        source_kind="openclaw_import",
        import_source_path=str(source_path),
        files=[
            SourceArtifactArchiveInput(
                relative_path=source_file.relative_path,
                display_name=source_file.path.name,
                media_type="application/json",
                content_text=source_file.text,
            )
            for source_file in select_openclaw_source_files(source_path, snapshot)
        ],
    )
    generic_batch = _to_generic_batch(source_path, snapshot)
    return import_normalized_batch(
        store,
        user_id=user_id,
        batch=generic_batch,
        config=ImportPersistenceConfig(
            source_kind="openclaw_import",
            source_prefix="openclaw",
            admission_reason="openclaw_import",
            dedupe_key_field="openclaw_dedupe_key",
            dedupe_posture=_OPENCLAW_DEDUPE_POSTURE,
            source_label="OpenClaw",
        ),
        archived_artifacts=archived_artifacts,
    )


__all__ = ["import_openclaw_source"]
