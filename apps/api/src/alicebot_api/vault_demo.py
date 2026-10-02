"""One-command local vault demo for ``alice-memory demo --vault``.

Import a markdown folder as sources, then print doctor, the session brief,
and the one snippet a new session will quote. Import is a source. Commit
is a fact. Do not auto-promote.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import compile_local_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vault_doctor import compile_local_vault_doctor
from alicebot_api.vnext_capture import (
    CaptureResult,
    VNextCaptureService,
    VNextCaptureValidationError,
    normalize_text,
)

SOURCE_LINE_PREFIX = "**source**:"
QUOTE_LABEL = "will quote"
CANDIDATE_STATUS = "candidate"


class DemoVaultError(ValueError):
    """A user-facing demo failure with a clear message."""


def markdown_files_in_vault(vault: Path) -> list[Path]:
    """The ``*.md`` files ``import_markdown_folder`` would see, files only."""

    return sorted(path for path in vault.rglob("*.md") if path.is_file())


def markdown_file_has_importable_text(path: Path) -> bool:
    """True when capture would accept this file's text (not empty after normalize)."""

    try:
        normalize_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, VNextCaptureValidationError):
        return False
    return True


def validate_demo_vault(vault: str | Path) -> Path:
    """Resolve a vault folder that exists, is a directory, and has quotable ``*.md``."""

    resolved = Path(vault).expanduser().resolve()
    if not resolved.exists():
        raise DemoVaultError(f"vault does not exist: {resolved}")
    if not resolved.is_dir():
        raise DemoVaultError(f"vault is not a directory: {resolved}")
    files = markdown_files_in_vault(resolved)
    if not files:
        raise DemoVaultError(f"vault has no *.md files: {resolved}")
    if not any(markdown_file_has_importable_text(path) for path in files):
        raise DemoVaultError(f"vault has no quotable markdown: {resolved}")
    return resolved


def first_source_excerpt(brief: str) -> str | None:
    """The excerpt from the first ``**source**`` line, or None."""

    for line in brief.splitlines():
        if line.startswith(SOURCE_LINE_PREFIX):
            return line[len(SOURCE_LINE_PREFIX) :].strip()
    return None


def receipt_for_imported_source(store: SQLiteVNextStore, source_id: str) -> str | None:
    """Reuse ``CaptureResult.receipt`` for one newly imported source."""

    source = store.get_source(source_id)
    if source is None:
        return None
    chunks = store.list_source_chunks(source_id)
    memories = store.list_memories_referencing_source(source_id=source_id)
    candidates = [row for row in memories if row.get("status") == CANDIDATE_STATUS]
    content_hash = source.get("content_hash")
    receipt = CaptureResult(
        status="imported",
        source_id=source_id,
        content_hash=str(content_hash or ""),
        chunk_count=len(chunks),
        candidate_memory_count=len(candidates),
    ).to_record()["receipt"]
    return str(receipt) if isinstance(receipt, str) and receipt.strip() else None


def format_import_summary(*, imported_count: int, duplicate_count: int, failed_count: int) -> str:
    return "\n".join(
        (
            f"imported: {imported_count}",
            f"duplicate: {duplicate_count}",
            f"failed: {failed_count}",
        )
    )


def run_local_vault_demo(
    db_path: Path,
    *,
    user_id: UUID | str,
    vault: Path,
) -> str:
    """Import the vault, then render summary, receipt, doctor, brief, and quote."""

    with sqlite_user_connection(db_path, user_id) as connection:
        store = SQLiteVNextStore(connection, user_id)
        batch = VNextCaptureService(store).import_markdown_folder(vault)
        receipt: str | None = None
        if batch.source_ids:
            receipt = receipt_for_imported_source(store, batch.source_ids[0])

    if batch.imported_count == 0 and batch.duplicate_count == 0:
        raise DemoVaultError("import produced no sources")

    doctor = compile_local_vault_doctor(db_path, user_id=user_id)
    # The demo vault is its own vault: no project view, no extra line.
    brief = compile_local_session_brief(
        db_path,
        user_id=user_id,
        query=None,
        project_view=ProjectView.unscoped(),
        exclude_global_domains=frozenset(),
    )
    excerpt = first_source_excerpt(brief)
    if excerpt is None:
        raise DemoVaultError("session brief has no source line after import")

    blocks = [
        format_import_summary(
            imported_count=batch.imported_count,
            duplicate_count=batch.duplicate_count,
            failed_count=batch.failed_count,
        )
    ]
    if receipt:
        blocks[0] = f"{blocks[0]}\n{receipt}"
    blocks.extend((doctor, brief, f"{QUOTE_LABEL}: {excerpt}"))
    return "\n\n".join(blocks)


__all__ = [
    "CANDIDATE_STATUS",
    "DemoVaultError",
    "QUOTE_LABEL",
    "SOURCE_LINE_PREFIX",
    "first_source_excerpt",
    "format_import_summary",
    "markdown_file_has_importable_text",
    "markdown_files_in_vault",
    "receipt_for_imported_source",
    "run_local_vault_demo",
    "validate_demo_vault",
]
