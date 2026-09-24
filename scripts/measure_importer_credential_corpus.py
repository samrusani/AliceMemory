#!/usr/bin/env python3
"""Measure markdown import credential skips on this repo's docs.

Imports every ``docs/**/*.md`` file through ``import_markdown_source`` and
prints how many items the receipt skips. Two classes:

- ``placeholder password example``: a shell assignment the floor cannot tell
  from a real password. These are the skipped lines outside the promotion doc.
- ``floor refused-example list``: a skipped line of the refused-example list
  in ``docs/memory/promotion-personas.md``.

The report names the file and the importer line number. It does not print
the line. Line numbers count from 1 on the first line after frontmatter.
The archive stand-in does not keep the source text.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))

from alicebot_api.continuity_evidence import ArchivedArtifactRef  # noqa: E402
from alicebot_api.markdown_import import (  # noqa: E402
    MarkdownImportValidationError,
    import_markdown_source,
)
import alicebot_api.markdown_import as markdown_import  # noqa: E402


_PROMOTION_DOC = "memory/promotion-personas.md"


class _RecordingStore:
    def list_continuity_recall_candidates(self) -> list[dict[str, object]]:
        return []

    def upsert_continuity_artifact_segment(self, **kwargs: object) -> dict[str, object]:
        return {"id": uuid4(), **kwargs}

    def create_continuity_capture_event(self, **kwargs: object) -> dict[str, object]:
        return {"id": uuid4(), **kwargs}

    def create_continuity_object(self, **kwargs: object) -> dict[str, object]:
        return {"id": uuid4(), **kwargs}

    def create_continuity_object_evidence_link(self, **kwargs: object) -> dict[str, object]:
        return {"id": uuid4()}


def _archive_without_text(
    store: object,
    *,
    user_id: object,
    source_kind: str,
    import_source_path: str,
    files: list[object],
) -> dict[str, ArchivedArtifactRef]:
    del store, user_id, source_kind, import_source_path
    archived: dict[str, ArchivedArtifactRef] = {}
    for file in files:
        relative_path = file.relative_path
        archived[relative_path] = ArchivedArtifactRef(
            artifact_id=uuid4(),
            artifact_copy_id=uuid4(),
            relative_path=relative_path,
            checksum_sha256="ab" * 32,
        )
    return archived


def _line_class(relative_path: str) -> str:
    if relative_path.replace("\\", "/") == _PROMOTION_DOC:
        return "floor refused-example list"
    return "placeholder password example"


def _line_label(item: dict[str, object]) -> str:
    start = item.get("line_number")
    end = item.get("line_end")
    if isinstance(start, int) and isinstance(end, int) and end != start:
        return f"{start}-{end}"
    if isinstance(start, int):
        return str(start)
    return "?"


def main() -> int:
    markdown_import.archive_import_source_files = _archive_without_text
    docs = ROOT / "docs"
    files = sorted(path for path in docs.rglob("*.md") if path.is_file())
    physical_lines = 0
    importable = 0
    skipped_rows: list[tuple[str, str, str]] = []
    unparsed: list[tuple[str, str]] = []
    store = _RecordingStore()

    for path in files:
        text = path.read_text(encoding="utf-8")
        physical_lines += len(text.splitlines())
        relative = path.relative_to(docs).as_posix()
        try:
            receipt = import_markdown_source(store, user_id=uuid4(), source=path)
        except MarkdownImportValidationError as exc:
            unparsed.append((relative, str(exc)))
            continue
        importable += int(receipt["total_candidates"])
        items = receipt["skipped_credential_items"]
        if not isinstance(items, list):
            raise SystemExit(f"skipped_credential_items was not a list for {relative}")
        for item in items:
            if not isinstance(item, dict):
                raise SystemExit(f"skipped item was not an object for {relative}")
            skipped_rows.append((relative, _line_label(item), _line_class(relative)))

    counts = Counter(row[2] for row in skipped_rows)
    print(f"files: {len(files)}")
    print(f"physical_lines: {physical_lines}")
    print(f"importable_lines: {importable}")
    print(f"skipped: {len(skipped_rows)}")
    print("classes:")
    for name in ("placeholder password example", "floor refused-example list"):
        print(f"  {name}: {counts[name]}")
        for relative, line, kind in skipped_rows:
            if kind == name:
                print(f"    docs/{relative}:{line}")
    extra = sorted(set(counts) - {"placeholder password example", "floor refused-example list"})
    for name in extra:
        print(f"  {name}: {counts[name]}")
    print(f"unparsed: {len(unparsed)}")
    for relative, reason in unparsed:
        print(f"  docs/{relative}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
