"""Shared memory lifecycle and true-redaction invariants."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from alicebot_api.credential_floor import SEARCHABLE_STATUSES, refuse_credential_activation
from alicebot_api.vnext_repositories import JsonObject

# Canonical true-redaction marker. Content columns are replaced with this
# literal (text columns) or with {"redacted": True} (JSON columns) so the
# audit skeleton proves something existed and was redacted without
# retaining what it said. Keep in lockstep with Postgres migration
# 20260706_0079 (the append-only triggers only admit marker-shaped
# updates) and with sqlite_store, which re-exports this constant.
REDACTION_MARKER = "[REDACTED]"

# JSON replacement written into redacted JSON content columns.
REDACTED_JSON_VALUE: JsonObject = {"redacted": True}


def refuse_created_credential_activation(memory: Mapping[str, object]) -> None:
    """The store-level activation check on create (owner ruling C2).

    Both stores' create_memory call this before the INSERT. A row created
    directly in a searchable status is read as it will be stored.
    """

    if str(memory.get("status") or "candidate") in SEARCHABLE_STATUSES:
        refuse_credential_activation(memory.get("title"), memory.get("canonical_text", ""), memory.get("summary"))


def refuse_updated_credential_activation(
    patch: Mapping[str, object], load_prior: Callable[[], Mapping[str, object] | None]
) -> None:
    """The store-level activation check on update (owner ruling C2).

    Runs only when the patch moves a row into a searchable status from one
    that is not: the row is read as it will be stored, the patch's text over
    the stored text (the update COALESCEs, so a None in the patch keeps the
    stored value). A patch that keeps an active row active is a text edit,
    which the door that sent it checks.
    """

    if patch.get("status") not in SEARCHABLE_STATUSES:
        return
    prior = load_prior()
    if prior is None or str(prior.get("status") or "") in SEARCHABLE_STATUSES:
        return

    def stored(name: str) -> object:
        value = patch.get(name)
        return prior.get(name) if value is None else value

    refuse_credential_activation(stored("title"), stored("canonical_text"), stored("summary"))


def redacted_memory_metadata(metadata: object, *, redacted_at: str) -> JsonObject:
    """Return the exact content-free metadata marker used by both stores.

    Digests, source references, workflow inputs, and arbitrary extension keys
    can confirm or reproduce erased content.  Only non-content ownership and
    graph pointers survive alongside the marker and first redaction timestamp.
    """
    structural_keys = (
        "project_id",
        "project_scope",
        "superseded_by",
        "supersedes",
        "run_id",
        "agent_id",
        "created_by_agent_id",
    )
    scrubbed: JsonObject = {}
    if isinstance(metadata, Mapping):
        for key in structural_keys:
            if key in metadata:
                scrubbed[key] = metadata[key]
    scrubbed["redacted"] = True
    scrubbed["redacted_at"] = redacted_at
    return scrubbed


REDACTED_MEMORY_METADATA_KEYS = frozenset(
    {
        "project_id",
        "project_scope",
        "superseded_by",
        "supersedes",
        "run_id",
        "agent_id",
        "created_by_agent_id",
        "redacted",
        "redacted_at",
    }
)
PRIOR_REDACTED_MEMORY_METADATA_KEYS = REDACTED_MEMORY_METADATA_KEYS | {
    "consolidation_digest",
    "source_refs",
}


def _is_redacted_memory_shape(
    memory: Mapping[str, object],
    *,
    allowed_metadata_keys: frozenset[str] | set[str],
) -> bool:
    memory_id = str(memory.get("id") or "").strip()
    metadata_value = memory.get("metadata_json")
    value = memory.get("value")
    if not memory_id or not isinstance(metadata_value, Mapping) or not isinstance(value, Mapping):
        return False
    metadata = dict(metadata_value)
    return (
        memory.get("memory_key") == f"redacted.{memory_id}"
        and memory.get("canonical_text") == REDACTION_MARKER
        and memory.get("title") in {None, REDACTION_MARKER}
        and memory.get("summary") in {None, REDACTION_MARKER}
        and memory.get("trust_reason") in {None, REDACTION_MARKER}
        and dict(value) == REDACTED_JSON_VALUE
        and memory.get("source_event_ids") == []
        and memory.get("commit_digest") is None
        and memory.get("confirmation_id") is None
        and memory.get("status") == "archived"
        and memory.get("deleted_at") is not None
        and bool(memory.get("_redaction_embedding_cleared", True))
        and bool(memory.get("_redaction_fact_keys_cleared", True))
        and set(metadata).issubset(allowed_metadata_keys)
        and metadata.get("redacted") is True
        and bool(str(metadata.get("redacted_at") or "").strip())
    )


def is_redacted_memory(memory: Mapping[str, object]) -> bool:
    """Recognize only the canonical content-free memory skeleton.

    The predicate deliberately rejects partial marker rows.  It is shared by
    replay detection and post-redaction write guards so a fabricated
    ``metadata_json.redacted`` flag cannot freeze an ordinary memory.
    """

    return _is_redacted_memory_shape(
        memory,
        allowed_metadata_keys=REDACTED_MEMORY_METADATA_KEYS,
    )


def is_prior_redacted_memory_marker(memory: Mapping[str, object]) -> bool:
    """Recognize the bounded pre-0092 content marker eligible for repair.

    Pre-0092 redaction did not yet replace the memory key or clear every
    digest/source pointer.  Timestamp reuse therefore mirrors the 0092
    backfill's narrower proof: all content is already marker-shaped, derived
    search state is gone, the row is retired, metadata is bounded, and the
    caller separately proves an existing ``memory.redacted`` receipt.
    """

    metadata_value = memory.get("metadata_json")
    value = memory.get("value")
    if not isinstance(metadata_value, Mapping) or not isinstance(value, Mapping):
        return False
    metadata = dict(metadata_value)
    return (
        memory.get("canonical_text") == REDACTION_MARKER
        and memory.get("title") in {None, REDACTION_MARKER}
        and memory.get("summary") in {None, REDACTION_MARKER}
        and memory.get("trust_reason") in {None, REDACTION_MARKER}
        and dict(value) == REDACTED_JSON_VALUE
        and memory.get("status") == "archived"
        and memory.get("deleted_at") is not None
        and bool(memory.get("_redaction_embedding_cleared", True))
        and bool(memory.get("_redaction_fact_keys_cleared", True))
        and set(metadata).issubset(PRIOR_REDACTED_MEMORY_METADATA_KEYS)
        and metadata.get("redacted") is True
        and bool(str(metadata.get("redacted_at") or "").strip())
    )


PROJECT_UPDATE_REDACTED_METADATA_KEYS = frozenset(
    {
        "redacted",
        "redacted_at",
        "workflow",
        "project_id",
        "project_scope",
        "candidate_memory_id",
        "review_action",
    }
)


def is_redacted_project_update_artifact(artifact: Mapping[str, object]) -> bool:
    """Recognize only the canonical terminal project-update artifact skeleton."""

    metadata_value = artifact.get("metadata_json")
    model_info_value = artifact.get("model_info_json")
    if not isinstance(metadata_value, Mapping) or not isinstance(model_info_value, Mapping):
        return False
    metadata = dict(metadata_value)
    project_id = str(metadata.get("project_id") or "").strip()
    candidate_memory_id = str(metadata.get("candidate_memory_id") or "").strip()
    review_action = str(metadata.get("review_action") or "").strip()
    redacted_at = str(metadata.get("redacted_at") or "").strip()
    return (
        artifact.get("artifact_type") == "project_update"
        and artifact.get("status") in {"accepted", "rejected"}
        and artifact.get("title") == REDACTION_MARKER
        and artifact.get("content_markdown") == REDACTION_MARKER
        and artifact.get("prompt_hash") is None
        and dict(model_info_value) == REDACTED_JSON_VALUE
        and set(metadata) == PROJECT_UPDATE_REDACTED_METADATA_KEYS
        and metadata.get("redacted") is True
        and bool(redacted_at and project_id and candidate_memory_id)
        and metadata.get("workflow") == "project_auto_update"
        and metadata.get("project_scope") == [project_id]
        and (
            (artifact.get("status") == "accepted" and review_action in {"accept", "edit"})
            or (artifact.get("status") == "rejected" and review_action == "reject")
        )
    )

for _helper in (
    redacted_memory_metadata,
    _is_redacted_memory_shape,
    is_redacted_memory,
    is_prior_redacted_memory_marker,
    is_redacted_project_update_artifact,
):
    _helper.__module__ = "alicebot_api.vnext_store"
    _helper.__qualname__ = _helper.__name__
del _helper
