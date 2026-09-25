#!/usr/bin/env python3
"""Measure markdown import credential skips on this repo's docs.

Imports every ``docs/**/*.md`` file through ``import_markdown_source`` and
prints how many items the receipt skips. Classes come from the line text,
not from the file path:

- ``placeholder password example``: the line is a shell assignment of a
  password variable. The floor cannot tell the stand-in value from a real
  password.
- ``floor refused-example list``: the line is a documentation sentence that
  quotes an example the floor refuses.
- ``other``: a skipped line that is neither of those.

``newly_skipped`` counts skipped items whose dedupe key is the first of its
kind in that file. The previous importer would have stored them.
``already_duplicate`` counts skipped items that repeat an earlier item's
dedupe key in the same file. The previous importer would have skipped those
as duplicates, so they are not a new loss.

The report names the file and the importer line number. It does not print
the line. Line numbers count from 1 on the first line after frontmatter.
The archive stand-in does not keep the source text.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))

from alicebot_api.continuity_evidence import ArchivedArtifactRef  # noqa: E402
from alicebot_api.markdown_import import (  # noqa: E402
    MarkdownImportValidationError,
    import_markdown_source,
    load_markdown_payload,
)
import alicebot_api.markdown_import as markdown_import  # noqa: E402


_PASSWORD_ASSIGNMENT = re.compile(
    r"(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*PASSWORD[A-Za-z0-9_]*=\S+(?:[ \t]*\\)?"
)
_REFUSED_EXAMPLE_PROSE = re.compile(
    r"PASSWORD|password|Secret:|BUILD_KEY|AKIA|hash_password|token",
    re.I,
)
_KNOWN_CLASSES = ("placeholder password example", "floor refused-example list", "other")


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


def classify_skip_line(line: str) -> str:
    """Classify one skipped line by its text."""

    text = line.strip()
    if text.startswith("- ") or text.startswith("* "):
        text = text[2:].strip()
    numbered = re.match(r"^\d+\.\s+(.*)$", text)
    if numbered:
        text = numbered.group(1).strip()
    if "\n" in text:
        text = text.splitlines()[0].strip()
    if _PASSWORD_ASSIGNMENT.fullmatch(text):
        return "placeholder password example"
    if _REFUSED_EXAMPLE_PROSE.search(text):
        return "floor refused-example list"
    return "other"


def partition_skips(
    dedupe_keys: list[str],
    skipped_sequences: set[int],
) -> tuple[int, int, set[int]]:
    """Return newly skipped, already duplicate, and the duplicate positions.

    ``skipped_sequences`` holds 1-based item positions. A skipped item is
    newly skipped when its dedupe key has not already appeared in the file.
    The third value is the 1-based positions of the already-duplicate skips.
    """

    seen: set[str] = set()
    newly_skipped = 0
    already_duplicate = 0
    duplicate_sequences: set[int] = set()
    for sequence_no, key in enumerate(dedupe_keys, start=1):
        first = key not in seen
        seen.add(key)
        if sequence_no not in skipped_sequences:
            continue
        if first:
            newly_skipped += 1
        else:
            already_duplicate += 1
            duplicate_sequences.add(sequence_no)
    return newly_skipped, already_duplicate, duplicate_sequences


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
    duplicate_rows: list[tuple[str, str]] = []
    newly_skipped = 0
    already_duplicate = 0
    unparsed: list[tuple[str, str]] = []
    store = _RecordingStore()

    for path in files:
        text = path.read_text(encoding="utf-8")
        physical_lines += len(text.splitlines())
        relative = path.relative_to(docs).as_posix()
        try:
            batch = load_markdown_payload(path)
            receipt = import_markdown_source(store, user_id=uuid4(), source=path)
        except MarkdownImportValidationError as exc:
            unparsed.append((relative, str(exc)))
            continue
        importable += int(receipt["total_candidates"])
        items = receipt["skipped_credential_items"]
        if not isinstance(items, list):
            raise SystemExit(f"skipped_credential_items was not a list for {relative}")
        sequences: set[int] = set()
        labeled: list[tuple[int, str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                raise SystemExit(f"skipped item was not an object for {relative}")
            sequence_no = item.get("sequence_no")
            if not isinstance(sequence_no, int) or not 1 <= sequence_no <= len(batch.items):
                raise SystemExit(f"skipped item had no batch position for {relative}")
            sequences.add(sequence_no)
            kind = classify_skip_line(batch.items[sequence_no - 1].source_segment_text)
            label = _line_label(item)
            skipped_rows.append((relative, label, kind))
            labeled.append((sequence_no, label, kind))
        new_count, duplicate_count, duplicate_sequences = partition_skips(
            [item.dedupe_key for item in batch.items],
            sequences,
        )
        newly_skipped += new_count
        already_duplicate += duplicate_count
        for sequence_no, label, _kind in labeled:
            if sequence_no in duplicate_sequences:
                duplicate_rows.append((relative, label))

    counts = Counter(row[2] for row in skipped_rows)
    print(f"files: {len(files)}")
    print(f"physical_lines: {physical_lines}")
    print(f"importable_lines: {importable}")
    print(f"skipped: {len(skipped_rows)}")
    print(f"newly_skipped: {newly_skipped}")
    print(f"already_duplicate: {already_duplicate}")
    print("classes:")
    for name in _KNOWN_CLASSES:
        print(f"  {name}: {counts[name]}")
        for relative, line, kind in skipped_rows:
            if kind == name:
                print(f"    docs/{relative}:{line}")
    extra = sorted(set(counts) - set(_KNOWN_CLASSES))
    for name in extra:
        print(f"  {name}: {counts[name]}")
        for relative, line, kind in skipped_rows:
            if kind == name:
                print(f"    docs/{relative}:{line}")
    print("already_duplicate_lines:")
    for relative, line in duplicate_rows:
        print(f"  docs/{relative}:{line}")
    print(f"unparsed: {len(unparsed)}")
    for relative, reason in unparsed:
        print(f"  docs/{relative}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
