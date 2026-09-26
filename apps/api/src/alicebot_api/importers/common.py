from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from alicebot_api.continuity_evidence import ArchivedArtifactRef, checksum_sha256_for_text
from alicebot_api.credential_floor import credential_verdict, string_values
from alicebot_api.importer_models import (
    ImporterNormalizedBatch,
    ImporterNormalizedItem,
    OBJECT_TYPE_TO_EXPLICIT_SIGNAL,
    to_string_list,
)
from alicebot_api.store import ContinuityStore, JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class ImportPersistenceConfig:
    source_kind: str
    source_prefix: str
    admission_reason: str
    dedupe_key_field: str
    dedupe_posture: str
    source_label: str | None = None


def _existing_dedupe_keys(
    store: ContinuityStore,
    *,
    source_kind: str,
    dedupe_key_field: str,
) -> set[str]:
    dedupe_keys: set[str] = set()
    for row in store.list_continuity_recall_candidates():
        provenance = row["provenance"]
        if not isinstance(provenance, dict):
            continue
        if provenance.get("source_kind") != source_kind:
            continue
        dedupe_key = provenance.get(dedupe_key_field)
        if isinstance(dedupe_key, str) and dedupe_key.strip() != "":
            dedupe_keys.add(dedupe_key)
    return dedupe_keys


def _deterministic_source_event_id(*, source_kind: str, workspace_id: str, source_item_id: str) -> str:
    return f"{source_kind}:{workspace_id}:{source_item_id}"


def _build_provenance(
    *,
    batch: ImporterNormalizedBatch,
    source_file: str,
    source_item_id: str,
    source_provenance: JsonObject,
    source_dedupe_key: str,
    source_event_ids: list[str],
    config: ImportPersistenceConfig,
) -> JsonObject:
    source_prefix = config.source_prefix
    provenance: JsonObject = {
        **source_provenance,
        "source_event_ids": cast(JsonValue, source_event_ids),
        "source_kind": config.source_kind,
        f"{source_prefix}_workspace_id": batch.context.workspace_id,
        f"{source_prefix}_workspace_name": batch.context.workspace_name,
        f"{source_prefix}_fixture_id": batch.context.fixture_id,
        f"{source_prefix}_source_path": batch.context.source_path,
        f"{source_prefix}_source_file": source_file,
        f"{source_prefix}_source_item_id": source_item_id,
        config.dedupe_key_field: source_dedupe_key,
        f"{source_prefix}_dedupe_posture": config.dedupe_posture,
    }
    if config.source_label is not None:
        provenance["source_label"] = config.source_label
    return provenance


def _body_credential_fields(body: JsonObject) -> tuple[object, ...]:
    """The body as the check should read it.

    The body is passed with its keys, which is how the continuity object door
    reads a body. An OpenClaw raw entry is the exception: it is passed by
    value. Provenance is passed with its keys. Passed as a mapping, every
    key of the entry is a name. A routing ``session_key`` whose value is
    ``agent:<profile>:<channel>:<kind>:<tail>``, with an optional
    ``:topic:<digits>`` suffix, is an identifier. The profile is a short
    lowercase word and may contain digits, ``-``, or ``_``. Channel and
    kind are short lowercase words. The tail is digits with an optional
    leading ``+`` or ``-``, or a lowercase UUID. An uppercase profile, and
    an opaque alphanumeric tail such as a Slack ``C04`` id, are not.
    Pair detection for that entry uses the segment text, which is the
    entry's canonical JSON.
    """

    raw_entry = body.get("openclaw_raw_entry")
    if not isinstance(raw_entry, Mapping):
        return (body,)
    rest = {key: value for key, value in body.items() if key != "openclaw_raw_entry"}
    return (rest, string_values(raw_entry))


def _item_credential_fields(item: ImporterNormalizedItem, provenance: JsonObject) -> tuple[object, ...]:
    """Fields of one item that the import would persist, in reading order.

    ``credential_verdict`` is the S4.4 check. Provenance is passed with its
    keys. The importer dedupe fields end in ``dedupe_key``, and ``dedupe``
    is a structural name, so a digest under that name is not a secret.
    """

    return (
        item.title,
        *_body_credential_fields(item.body),
        provenance,
        item.raw_content,
        item.source_segment_text,
    )


def _locator_int(locator: JsonObject, key: str) -> int | None:
    value = locator.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _credential_skip_name(item: ImporterNormalizedItem, sequence_no: int) -> JsonObject:
    """Name a skipped item by id or line. Never include the matched text.

    The id is omitted when the id itself is credential material, so the
    receipt cannot echo the secret. ``sequence_no`` is the item's place in
    the batch and is always safe to report.
    """

    name: JsonObject = {"sequence_no": sequence_no}
    line_number = _locator_int(item.source_locator, "line_number")
    if line_number is not None:
        name["line_number"] = line_number
    line_end = _locator_int(item.source_locator, "line_end")
    if line_end is not None:
        name["line_end"] = line_end
    entry_index = _locator_int(item.source_locator, "entry_index")
    if entry_index is not None:
        name["entry_index"] = entry_index
    if credential_verdict(item.source_item_id) is None:
        name["source_item_id"] = item.source_item_id
    return name


def import_normalized_batch(
    store: ContinuityStore,
    *,
    user_id: UUID,
    batch: ImporterNormalizedBatch,
    config: ImportPersistenceConfig,
    archived_artifacts: dict[str, ArchivedArtifactRef],
) -> JsonObject:
    del user_id

    existing_dedupe_keys = _existing_dedupe_keys(
        store,
        source_kind=config.source_kind,
        dedupe_key_field=config.dedupe_key_field,
    )
    run_dedupe_keys: set[str] = set()

    imported_object_ids: list[str] = []
    imported_capture_ids: list[str] = []
    skipped_duplicates = 0
    skipped_credentials = 0
    skipped_credential_items: list[JsonObject] = []

    for sequence_no, item in enumerate(batch.items, start=1):
        source_event_ids = to_string_list(item.source_provenance.get("source_event_ids"))
        if not source_event_ids:
            source_event_ids = [
                _deterministic_source_event_id(
                    source_kind=config.source_kind,
                    workspace_id=batch.context.workspace_id,
                    source_item_id=item.source_item_id,
                )
            ]
        provenance = _build_provenance(
            batch=batch,
            source_file=item.source_file,
            source_item_id=item.source_item_id,
            source_provenance=item.source_provenance,
            source_dedupe_key=item.dedupe_key,
            source_event_ids=source_event_ids,
            config=config,
        )
        # Skip this item and keep going. Raising would refuse the whole import.
        if credential_verdict(*_item_credential_fields(item, provenance)) is not None:
            skipped_credentials += 1
            skipped_credential_items.append(_credential_skip_name(item, sequence_no))
            continue

        if item.dedupe_key in existing_dedupe_keys or item.dedupe_key in run_dedupe_keys:
            skipped_duplicates += 1
            continue

        run_dedupe_keys.add(item.dedupe_key)

        archived_artifact = archived_artifacts.get(item.source_file)
        if archived_artifact is None:
            raise ValueError(f"missing archived artifact for source file '{item.source_file}'")

        segment = store.upsert_continuity_artifact_segment(
            artifact_id=archived_artifact.artifact_id,
            artifact_copy_id=archived_artifact.artifact_copy_id,
            source_item_id=item.source_item_id,
            sequence_no=sequence_no,
            segment_kind=item.source_segment_kind,
            locator=item.source_locator,
            raw_content=item.source_segment_text,
            checksum_sha256=checksum_sha256_for_text(item.source_segment_text),
        )

        capture = store.create_continuity_capture_event(
            raw_content=item.raw_content,
            explicit_signal=OBJECT_TYPE_TO_EXPLICIT_SIGNAL[item.object_type],
            admission_posture="DERIVED",
            admission_reason=config.admission_reason,
        )

        provenance["artifact_id"] = str(archived_artifact.artifact_id)
        provenance["artifact_copy_id"] = str(archived_artifact.artifact_copy_id)
        provenance["artifact_copy_checksum_sha256"] = archived_artifact.checksum_sha256
        provenance["artifact_segment_id"] = str(segment["id"])
        provenance["artifact_segment_source_item_id"] = item.source_item_id

        continuity_object = store.create_continuity_object(
            capture_event_id=capture["id"],
            object_type=item.object_type,
            status=item.status,
            title=item.title,
            body=item.body,
            provenance=provenance,
            confidence=item.confidence,
        )
        store.create_continuity_object_evidence_link(
            continuity_object_id=continuity_object["id"],
            artifact_id=archived_artifact.artifact_id,
            artifact_copy_id=archived_artifact.artifact_copy_id,
            artifact_segment_id=segment["id"],
            relationship="primary_source_segment",
        )

        imported_capture_ids.append(str(capture["id"]))
        imported_object_ids.append(str(continuity_object["id"]))

    imported_count = len(imported_object_ids)
    status = "ok" if imported_count > 0 else "noop"

    return {
        "status": status,
        "source_path": batch.context.source_path,
        "fixture_id": batch.context.fixture_id,
        "workspace_id": batch.context.workspace_id,
        "workspace_name": batch.context.workspace_name,
        "total_candidates": len(batch.items),
        "imported_count": imported_count,
        "skipped_duplicates": skipped_duplicates,
        "skipped_credentials": skipped_credentials,
        "skipped_credential_items": cast(JsonValue, skipped_credential_items),
        "dedupe_posture": config.dedupe_posture,
        "provenance_source_kind": config.source_kind,
        "provenance_source_label": config.source_label,
        "imported_capture_event_ids": cast(JsonValue, imported_capture_ids),
        "imported_object_ids": cast(JsonValue, imported_object_ids),
    }


__all__ = ["ImportPersistenceConfig", "import_normalized_batch"]
