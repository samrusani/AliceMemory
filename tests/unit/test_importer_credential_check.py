"""Per-item credential check for the markdown, ChatGPT, and OpenClaw importers.

Rule: an item that holds credential material is skipped, not stored active.
The import continues. The receipt names the item by id or line and counts
the skips. It never includes the matched text.

Mutation ``import_credential_item_as_active``: in ``import_normalized_batch``,
do not continue after the credential verdict, so the credential item is
written with its status (active). ``test_credential_item_is_skipped_and_absent_from_the_store``
must fail by assertion. An exception during import is not that failure.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable
from uuid import uuid4

import pytest

from alicebot_api.chatgpt_import import import_chatgpt_source
from alicebot_api.continuity_evidence import ArchivedArtifactRef
from alicebot_api.importer_models import (
    ImporterNormalizedBatch,
    ImporterNormalizedItem,
    ImporterWorkspaceContext,
)
from alicebot_api.importers.common import ImportPersistenceConfig, import_normalized_batch
from alicebot_api.markdown_import import import_markdown_source
from alicebot_api.openclaw_import import import_openclaw_source
from alicebot_api.store import JsonObject


REPO_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_FIXTURE = REPO_ROOT / "fixtures" / "importers" / "markdown" / "workspace_v1.md"
CHATGPT_FIXTURE = REPO_ROOT / "fixtures" / "importers" / "chatgpt" / "workspace_v1.json"
OPENCLAW_FIXTURE = REPO_ROOT / "fixtures" / "openclaw" / "workspace_v1.json"
OPENCLAW_DIR_FIXTURE = REPO_ROOT / "fixtures" / "openclaw" / "workspace_dir_v1"

SAFE = "Keep the weekly notes import deterministic."


def _deploy_token() -> str:
    """A GitHub token shape, built from parts so the source has no whole token."""

    return "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


class _RecordingImporterStore:
    """Records the rows ``import_normalized_batch`` writes. No database."""

    def __init__(self) -> None:
        self.objects: list[dict[str, object]] = []
        self.captures: list[dict[str, object]] = []
        self.segments: list[dict[str, object]] = []
        self.links: list[dict[str, object]] = []

    def list_continuity_recall_candidates(self) -> list[dict[str, object]]:
        return [{"provenance": obj["provenance"]} for obj in self.objects]

    def upsert_continuity_artifact_segment(self, **kwargs: object) -> dict[str, object]:
        row = {"id": uuid4(), **kwargs}
        self.segments.append(row)
        return row

    def create_continuity_capture_event(self, **kwargs: object) -> dict[str, object]:
        row = {"id": uuid4(), **kwargs}
        self.captures.append(row)
        return row

    def create_continuity_object(self, **kwargs: object) -> dict[str, object]:
        row = {"id": uuid4(), **kwargs}
        self.objects.append(row)
        return row

    def create_continuity_object_evidence_link(self, **kwargs: object) -> dict[str, object]:
        self.links.append(dict(kwargs))
        return {"id": uuid4()}

    def stored_text(self) -> str:
        return json.dumps(
            {
                "objects": self.objects,
                "captures": self.captures,
                "segments": self.segments,
                "links": self.links,
            },
            default=str,
        )


def _archive_without_storing_text(
    store: object,
    *,
    user_id: object,
    source_kind: str,
    import_source_path: str,
    files: list[object],
) -> dict[str, ArchivedArtifactRef]:
    """Stand in for the source-file archive so the test can run without Postgres.

    The real archive stores the file the user supplied, before items are
    filtered. This double does not keep that text. The assertions below are
    about the continuity object, the capture, and the segment.
    """

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


def _patch_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name in (
        "alicebot_api.markdown_import",
        "alicebot_api.chatgpt_import",
        "alicebot_api.openclaw_import",
    ):
        monkeypatch.setattr(f"{module_name}.archive_import_source_files", _archive_without_storing_text)


def _write_markdown(path: Path, token: str) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                "fixture_id: importer-credential-check",
                "workspace_id: importer-credential-workspace",
                "workspace_name: Credential Check",
                "---",
                f"- Decision: The deploy token is {token}. | id=secret-note",
                f"- Decision: {SAFE} | id=safe-note",
                f"- Decision: {SAFE} | id=safe-note",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_chatgpt(path: Path, token: str) -> None:
    path.write_text(
        json.dumps(
            {
                "fixture_id": "importer-credential-check",
                "workspace": {
                    "id": "importer-credential-workspace",
                    "name": "Credential Check",
                },
                "conversations": [
                    {
                        "id": "conv-1",
                        "messages": [
                            {
                                "id": "secret-msg",
                                "role": "user",
                                "text": f"Decision: The deploy token is {token}.",
                            },
                            {"id": "safe-msg", "role": "user", "text": f"Decision: {SAFE}"},
                            {"id": "safe-msg", "role": "user", "text": f"Decision: {SAFE}"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_openclaw(path: Path, token: str) -> None:
    path.write_text(
        json.dumps(
            {
                "fixture_id": "importer-credential-check",
                "workspace": {
                    "id": "importer-credential-workspace",
                    "name": "Credential Check",
                },
                "durable_memory": [
                    {
                        "id": "secret-memory",
                        "type": "decision",
                        "status": "active",
                        "content": f"The deploy token is {token}.",
                    },
                    {
                        "id": "safe-memory",
                        "type": "decision",
                        "status": "active",
                        "content": SAFE,
                    },
                    {
                        "id": "safe-memory",
                        "type": "decision",
                        "status": "active",
                        "content": SAFE,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


Importer = Callable[..., JsonObject]


@pytest.mark.parametrize(
    ("import_source", "write_source", "filename", "skipped_name"),
    [
        (
            import_markdown_source,
            _write_markdown,
            "notes.md",
            {"sequence_no": 1, "line_number": 1, "source_item_id": "secret-note"},
        ),
        (
            import_chatgpt_source,
            _write_chatgpt,
            "export.json",
            {"sequence_no": 1, "source_item_id": "conv-1:secret-msg"},
        ),
        (
            import_openclaw_source,
            _write_openclaw,
            "memories.json",
            {"sequence_no": 1, "entry_index": 1, "source_item_id": "secret-memory"},
        ),
    ],
)
def test_credential_item_is_skipped_and_absent_from_the_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    import_source: Importer,
    write_source: Callable[[Path, str], None],
    filename: str,
    skipped_name: dict[str, object],
) -> None:
    _patch_archive(monkeypatch)
    token = _deploy_token()
    source = tmp_path / filename
    write_source(source, token)
    store = _RecordingImporterStore()

    receipt = import_source(store, user_id=uuid4(), source=source)

    stored = store.stored_text()
    secret_in_store = token in stored
    assert secret_in_store is False
    assert SAFE in stored
    assert len(store.objects) == 1
    assert store.objects[0]["status"] == "active"
    assert receipt["status"] == "ok"
    assert receipt["imported_count"] == 1
    assert receipt["skipped_duplicates"] == 1
    assert receipt["skipped_credentials"] == 1
    assert receipt["skipped_credential_items"] == [skipped_name]
    assert token not in json.dumps(receipt)

    replay = import_source(store, user_id=uuid4(), source=source)

    assert token not in store.stored_text()
    assert len(store.objects) == 1
    assert replay["status"] == "noop"
    assert replay["imported_count"] == 0
    assert replay["skipped_credentials"] == 1
    assert replay["skipped_duplicates"] == 2
    assert replay["skipped_credential_items"] == [skipped_name]


def test_receipt_names_the_line_when_the_item_id_is_the_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_archive(monkeypatch)
    token = _deploy_token()
    source = tmp_path / "notes.md"
    source.write_text(
        "\n".join(
            [
                "---",
                "fixture_id: importer-credential-check",
                "workspace_id: importer-credential-workspace",
                "---",
                f"- Note: {SAFE} | id=safe-note",
                f"- Note: Rotate the saved credential. | id={token}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    store = _RecordingImporterStore()

    receipt = import_markdown_source(store, user_id=uuid4(), source=source)

    assert token not in store.stored_text()
    assert SAFE in store.stored_text()
    assert receipt["skipped_credentials"] == 1
    assert receipt["skipped_credential_items"] == [{"sequence_no": 2, "line_number": 2}]
    assert token not in json.dumps(receipt)


def test_completed_credential_item_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_archive(monkeypatch)
    token = _deploy_token()
    source = tmp_path / "notes.md"
    source.write_text(
        "\n".join(
            [
                "---",
                "fixture_id: importer-credential-check",
                "workspace_id: importer-credential-workspace",
                "---",
                f"- Decision: The deploy token is {token}. | id=secret-note | status=completed",
                f"- Decision: {SAFE} | id=safe-note",
                "",
            ]
        ),
        encoding="utf-8",
    )
    store = _RecordingImporterStore()

    receipt = import_markdown_source(store, user_id=uuid4(), source=source)

    assert token not in store.stored_text()
    assert SAFE in store.stored_text()
    assert store.objects[0]["status"] == "active"
    assert receipt["skipped_credentials"] == 1
    assert receipt["imported_count"] == 1


@pytest.mark.parametrize(
    ("import_source", "source", "total_candidates", "imported_count"),
    [
        (import_markdown_source, MARKDOWN_FIXTURE, 5, 4),
        (import_chatgpt_source, CHATGPT_FIXTURE, 5, 4),
        (import_openclaw_source, OPENCLAW_FIXTURE, 5, 4),
        (import_openclaw_source, OPENCLAW_DIR_FIXTURE, 4, 3),
    ],
)
def test_shipped_importer_fixtures_skip_no_credential_items(
    monkeypatch: pytest.MonkeyPatch,
    import_source: Importer,
    source: Path,
    total_candidates: int,
    imported_count: int,
) -> None:
    _patch_archive(monkeypatch)
    store = _RecordingImporterStore()

    first = import_source(store, user_id=uuid4(), source=source)

    assert first["status"] == "ok"
    assert first["total_candidates"] == total_candidates
    assert first["imported_count"] == imported_count
    assert first["skipped_duplicates"] == 1
    assert first["skipped_credentials"] == 0
    assert first["skipped_credential_items"] == []
    assert len(store.objects) == imported_count

    second = import_source(store, user_id=uuid4(), source=source)

    assert second["status"] == "noop"
    assert second["imported_count"] == 0
    assert second["skipped_duplicates"] == total_candidates
    assert second["skipped_credentials"] == 0
    assert second["skipped_credential_items"] == []
    assert len(store.objects) == imported_count


def _openssl_3_present() -> bool:
    """True when the ``openssl`` binary reports OpenSSL 3."""

    try:
        completed = subprocess.run(
            ["openssl", "version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if completed.returncode != 0:
        return False
    report = f"{completed.stdout}\n{completed.stderr}"
    return re.search(r"(?m)^OpenSSL 3\.", report) is not None


def _rsa_private_key_lines() -> list[str]:
    """A 2048-bit RSA private key from openssl, traditional PEM, never stored.

    OpenSSL 3 prints a PKCS#8 key unless ``-traditional`` is set. The
    traditional form is the dashed RSA armor line plus 25 base64 body lines.
    """

    completed = subprocess.run(
        ["openssl", "genrsa", "-traditional", "2048"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    assert len(lines) == 27
    assert len(lines[1:-1]) == 25
    return lines


@pytest.mark.skipif(not _openssl_3_present(), reason="OpenSSL 3 is absent")
def test_markdown_armored_private_key_block_is_skipped_whole(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A key inside a code fence is one skip, and no body line is stored.

    Reverting the block collapse stores the base64 lines as their own items.
    Reverting the skip after the verdict stores the collapsed block, body
    lines included. Either way a body line is in the store and this fails.
    """

    _patch_archive(monkeypatch)
    key_lines = _rsa_private_key_lines()
    body_lines = key_lines[1:-1]
    before = "Keep the weekly notes import deterministic."
    after = "File the weekly notes under the project folder."
    fence = "```"
    body = [
        f"- Note: {before} | id=safe-before",
        fence,
        *key_lines,
        fence,
        f"- Note: {after} | id=safe-after",
    ]
    source = tmp_path / "notes.md"
    source.write_text(
        "\n".join(
            [
                "---",
                "fixture_id: importer-credential-check",
                "workspace_id: importer-credential-workspace",
                "---",
                *body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    store = _RecordingImporterStore()

    receipt = import_markdown_source(store, user_id=uuid4(), source=source)

    stored = store.stored_text()
    for line in body_lines:
        assert line not in stored
    assert key_lines[0] not in stored
    assert key_lines[-1] not in stored
    assert before in stored
    assert after in stored
    start = body.index(key_lines[0]) + 1
    end = body.index(key_lines[-1]) + 1
    assert receipt["skipped_credentials"] == 1
    assert receipt["skipped_credential_items"] == [
        {
            "sequence_no": 3,
            "line_number": start,
            "line_end": end,
            "source_item_id": f"notes.md:{start}-{end}",
        }
    ]
    assert receipt["imported_count"] == 3
    assert receipt["skipped_duplicates"] == 1
    receipt_text = json.dumps(receipt)
    for line in body_lines:
        assert line not in receipt_text


def test_openclaw_routing_session_key_is_imported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ordinary OpenClaw entry with routing metadata is stored.

    Passing ``openclaw_raw_entry`` as a mapping makes ``session_key`` a
    secret name and this entry is skipped. v0.16.0 imported it.
    """

    _patch_archive(monkeypatch)
    content = "Prefers short replies in Telegram."
    source = tmp_path / "memories.json"
    source.write_text(
        json.dumps(
            {
                "fixture_id": "importer-credential-check",
                "workspace": {
                    "id": "importer-credential-workspace",
                    "name": "Credential Check",
                },
                "durable_memory": [
                    {
                        "id": "oc-1",
                        "type": "preference",
                        "content": content,
                        "session_key": "agent:main:telegram:dm:4471",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = _RecordingImporterStore()

    receipt = import_openclaw_source(store, user_id=uuid4(), source=source)

    assert receipt["skipped_credentials"] == 0
    assert receipt["skipped_credential_items"] == []
    assert receipt["imported_count"] == 1
    assert receipt["status"] == "ok"
    assert len(store.objects) == 1
    assert store.objects[0]["status"] == "active"
    assert content in store.stored_text()


def test_openclaw_secret_named_only_in_the_raw_entry_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A secret name in the raw entry is still skipped via the segment JSON.

    The value is not self-identifying. The canonical JSON of the entry is the
    segment text, and that text is what pair detection reads.
    """

    _patch_archive(monkeypatch)
    secret = "b7" + "Qx" * 12
    content = "Prefers short replies in Telegram."
    source = tmp_path / "memories.json"
    source.write_text(
        json.dumps(
            {
                "fixture_id": "importer-credential-check",
                "workspace": {
                    "id": "importer-credential-workspace",
                    "name": "Credential Check",
                },
                "durable_memory": [
                    {
                        "id": "oc-2",
                        "type": "note",
                        "content": content,
                        "api_key": secret,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = _RecordingImporterStore()

    receipt = import_openclaw_source(store, user_id=uuid4(), source=source)

    stored = store.stored_text()
    assert secret not in stored
    assert content not in stored
    assert receipt["skipped_credentials"] == 1
    assert receipt["imported_count"] == 0
    assert secret not in json.dumps(receipt)


def _armor_line(kind: str, label: str) -> str:
    return ("-" * 5) + f"{kind} {label}" + ("-" * 5)


def _radix64_line() -> str:
    return base64.b64encode(os.urandom(48)).decode("ascii")


def _checksum_line() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    return "=" + "".join(alphabet[byte % 64] for byte in os.urandom(4))


def _write_body(tmp_path: Path, body_lines: list[str]) -> Path:
    source = tmp_path / "notes.md"
    source.write_text(
        "\n".join(
            [
                "---",
                "fixture_id: importer-credential-check",
                "workspace_id: importer-credential-workspace",
                "---",
                *body_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return source


def _import_written(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
) -> tuple[_RecordingImporterStore, JsonObject]:
    _patch_archive(monkeypatch)
    store = _RecordingImporterStore()
    receipt = import_markdown_source(store, user_id=uuid4(), source=source)
    return store, receipt


def _skip_spans(receipt: JsonObject) -> list[tuple[int, int]]:
    items = receipt["skipped_credential_items"]
    assert isinstance(items, list)
    spans: list[tuple[int, int]] = []
    for item in items:
        assert isinstance(item, dict)
        start = item.get("line_number")
        end = item.get("line_end", start)
        if isinstance(start, int) and isinstance(end, int):
            spans.append((start, end))
    return spans


def test_lone_begin_and_end_snippets_keep_the_notes_between_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A BEGIN snippet, ordinary notes, then an END snippet stores the notes.

    The scan used to run from any full-line BEGIN to the next END with the
    same label. Restoring that scan skips the notes as one credential.
    """

    begin = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end = _armor_line("END", "RSA PRIVATE KEY")
    first_note = "The operator copies the first line only."
    second_note = "The closing line is a separate snippet."
    fence = "```"
    body = [
        f"- Note: {SAFE} | id=safe-before",
        fence,
        begin,
        fence,
        f"- Note: {first_note} | id=between-1",
        f"- Note: {second_note} | id=between-2",
        fence,
        end,
        fence,
        "- Note: File the weekly notes under the project folder. | id=safe-after",
    ]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))

    stored = store.stored_text()
    assert first_note in stored
    assert second_note in stored
    assert SAFE in stored
    assert "File the weekly notes under the project folder." in stored
    assert begin not in stored
    begin_at = body.index(begin) + 1
    end_at = body.index(end) + 1
    assert not any(start <= begin_at and end_at <= stop for start, stop in _skip_spans(receipt))


@pytest.mark.parametrize("kind", ["blank", "radix-64", "checksum", "armor header"])
def test_armored_block_includes_each_key_body_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    """Each key-body line stays inside the block, so the radix-64 line is not stored.

    Refusing that kind of line stops the scan early and stores the radix-64 line.
    """

    begin = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end = _armor_line("END", "RSA PRIVATE KEY")
    sentinel = _radix64_line()
    if kind == "blank":
        middle = [""]
    elif kind == "radix-64":
        middle = []
    elif kind == "checksum":
        middle = [_checksum_line()]
    elif kind == "armor header":
        middle = ["Version: GnuPG v2"]
    else:
        raise AssertionError(kind)
    body = [f"- Note: {SAFE} | id=safe-before", begin, *middle, sentinel, end]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))

    assert sentinel not in store.stored_text()
    assert SAFE in store.stored_text()
    begin_at = body.index(begin) + 1
    end_at = body.index(end) + 1
    assert (begin_at, end_at) in _skip_spans(receipt)
    assert receipt["skipped_credentials"] == 1


def test_code_fence_stops_the_armored_block_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A code fence between BEGIN and END leaves the following radix-64 line stored.

    Treating the fence as key body, or scanning past it, skips that line.
    """

    begin = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end = _armor_line("END", "RSA PRIVATE KEY")
    sentinel = _radix64_line()
    body = [begin, "```", sentinel, end]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))

    assert sentinel in store.stored_text()
    assert not any(start == 1 and stop == 4 for start, stop in _skip_spans(receipt))


def test_another_begin_stops_the_armored_block_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second BEGIN line starts its own block. The first BEGIN stays one line.

    Scanning through the second BEGIN joins both headers to the END line.
    """

    first = _armor_line("BEGIN", "RSA PRIVATE KEY")
    second = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end = _armor_line("END", "RSA PRIVATE KEY")
    sentinel = _radix64_line()
    body = [first, second, sentinel, end]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))

    assert sentinel not in store.stored_text()
    spans = _skip_spans(receipt)
    assert (2, 4) in spans
    assert (1, 4) not in spans
    assert (1, 1) in spans


def test_label_mismatch_does_not_form_an_armored_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BEGIN and END with different labels are not one block.

    A later END with the BEGIN label must not reach back across the mismatched
    END. Pairing any END line with the open BEGIN skips the radix-64 line too.
    """

    begin = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end_other = _armor_line("END", "EC PRIVATE KEY")
    end_same = _armor_line("END", "RSA PRIVATE KEY")
    sentinel = _radix64_line()
    body = [begin, sentinel, end_other, end_same]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))

    assert sentinel in store.stored_text()
    assert (1, 4) not in _skip_spans(receipt)
    assert (1, 3) not in _skip_spans(receipt)


def test_pgp_private_key_block_is_skipped_whole(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An OpenPGP private-key block, headers and checksum included, is one skip.

    The block is built at runtime. Reverting header, blank, radix-64, or
    checksum handling stores a body line.
    """

    begin = _armor_line("BEGIN", "PGP PRIVATE KEY BLOCK")
    end = _armor_line("END", "PGP PRIVATE KEY BLOCK")
    raw = base64.b64encode(os.urandom(96)).decode("ascii")
    body_lines = [raw[index : index + 64] for index in range(0, len(raw), 64)]
    header = "Version: GnuPG v2"
    comment = "Comment: throwaway"
    checksum = _checksum_line()
    block = [begin, header, comment, "", *body_lines, checksum, end]
    after = "File the weekly notes under the project folder."
    body = [f"- Note: {SAFE} | id=safe-before", *block, f"- Note: {after} | id=safe-after"]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))

    stored = store.stored_text()
    for line in (*body_lines, header, comment, checksum, begin, end):
        assert line not in stored
    assert SAFE in stored
    assert after in stored
    start = body.index(begin) + 1
    stop = body.index(end) + 1
    assert (start, stop) in _skip_spans(receipt)
    assert receipt["skipped_credentials"] == 1
    assert receipt["imported_count"] == 2


def test_unbulleted_typed_notes_between_begin_and_end_are_stored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BEGIN, unbulleted typed notes, and END store the notes.

    Notes directly after BEGIN are stored because a block needs a radix-64
    line of 40 or more characters. Dropping that requirement skips them.
    Notes after a long radix-64 line are stored because a ``Name: value``
    line counts only in the run directly after BEGIN. Accepting a
    ``Name: value`` line anywhere in the block skips those notes.
    """

    begin = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end = _armor_line("END", "RSA PRIVATE KEY")
    notes = [
        "Decision: Use Alice as the continuity layer for this agent.",
        "Task: Confirm the launch checklist owner.",
        "Owner: Dana",
        "Runbook: https://example.com/runbook",
    ]
    kept = [
        "Use Alice as the continuity layer for this agent.",
        "Confirm the launch checklist owner.",
        "Owner: Dana",
        "https://example.com/runbook",
    ]
    after = "File the weekly notes under the project folder."
    direct = [
        f"- Note: {SAFE} | id=safe-before",
        begin,
        *notes,
        end,
        f"- Note: {after} | id=safe-after",
    ]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, direct))
    stored = store.stored_text()
    for note in kept:
        assert note in stored
    assert SAFE in stored
    assert after in stored
    begin_at = direct.index(begin) + 1
    end_at = direct.index(end) + 1
    assert (begin_at, end_at) not in _skip_spans(receipt)

    sentinel = _radix64_line()
    after_body = [begin, sentinel, *notes, end]
    later_dir = tmp_path / "later"
    later_dir.mkdir()
    later_store, later_receipt = _import_written(
        monkeypatch,
        _write_body(later_dir, after_body),
    )
    later_stored = later_store.stored_text()
    for note in kept:
        assert note in later_stored
    assert sentinel in later_stored
    assert (1, len(after_body)) not in _skip_spans(later_receipt)


def test_one_word_lines_between_begin_and_end_are_stored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BEGIN, one-word lines, and END store the lines.

    Each word is radix-64 text shorter than 40 characters. Treating that
    short line as enough to form a block skips the words.
    """

    begin = _armor_line("BEGIN", "RSA PRIVATE KEY")
    end = _armor_line("END", "RSA PRIVATE KEY")
    words = ["Quill", "Marble", "Fern", "Nimbus"]
    after = "File the weekly notes under the project folder."
    body = [
        f"- Note: {SAFE} | id=safe-before",
        begin,
        *words,
        end,
        f"- Note: {after} | id=safe-after",
    ]
    store, receipt = _import_written(monkeypatch, _write_body(tmp_path, body))
    stored = store.stored_text()
    for word in words:
        assert word in stored
    assert SAFE in stored
    assert after in stored
    begin_at = body.index(begin) + 1
    end_at = body.index(end) + 1
    assert (begin_at, end_at) not in _skip_spans(receipt)


def _normalized_item(
    *,
    item_id: str,
    title: str,
    body: JsonObject,
    raw_content: str,
    segment: str,
    provenance: JsonObject | None = None,
    line_number: int = 1,
) -> ImporterNormalizedItem:
    return ImporterNormalizedItem(
        source_item_id=item_id,
        source_file="notes.md",
        source_locator={"line_number": line_number, "source_item_id": item_id},
        source_segment_text=segment,
        source_segment_kind="markdown_line",
        object_type="Note",
        status="active",
        raw_content=raw_content,
        title=title,
        body=body,
        confidence=0.84,
        source_provenance={} if provenance is None else provenance,
        dedupe_key=item_id,
    )


def _import_items(items: list[ImporterNormalizedItem]) -> tuple[_RecordingImporterStore, JsonObject]:
    store = _RecordingImporterStore()
    archived = {
        "notes.md": ArchivedArtifactRef(
            artifact_id=uuid4(),
            artifact_copy_id=uuid4(),
            relative_path="notes.md",
            checksum_sha256="cd" * 32,
        )
    }
    receipt = import_normalized_batch(
        store,
        user_id=uuid4(),
        batch=ImporterNormalizedBatch(
            context=ImporterWorkspaceContext(
                fixture_id="importer-credential-check",
                workspace_id="importer-credential-workspace",
                workspace_name="Credential Check",
                source_path="notes.md",
            ),
            items=items,
        ),
        config=ImportPersistenceConfig(
            source_kind="markdown_import",
            source_prefix="markdown",
            admission_reason="markdown_import",
            dedupe_key_field="markdown_dedupe_key",
            dedupe_posture="workspace_and_line_fingerprint",
        ),
        archived_artifacts=archived,
    )
    return store, receipt


def _late_secret(secret: str) -> str:
    return ("Keep the weekly notes import deterministic. " * 24) + secret


@pytest.mark.parametrize("field_name", ["title", "body", "provenance", "raw_content", "segment"])
def test_each_checked_field_skips_when_only_that_field_holds_a_secret(field_name: str) -> None:
    """A secret in only this field is skipped. Dropping the field stores it.

    The body holds the secret under ``api_key``. The value alone is not a
    credential shape, so passing the body by value, the way provenance is
    passed, stores it. The other fields hold a token at the end of a long note.
    """

    token = _deploy_token()
    late = _late_secret(token)
    opaque = "b7" + "Qx" * 12
    title = SAFE
    body: JsonObject = {"body": SAFE}
    raw_content = SAFE
    segment = SAFE
    provenance: JsonObject = {}
    secret_marker = token
    if field_name == "title":
        title = late
    elif field_name == "body":
        body = {"body": SAFE, "api_key": opaque}
        secret_marker = opaque
    elif field_name == "provenance":
        provenance = {"operator_note": late}
    elif field_name == "raw_content":
        raw_content = late
    elif field_name == "segment":
        segment = late
    else:
        raise AssertionError(field_name)

    secret_item = _normalized_item(
        item_id="secret-item",
        title=title,
        body=body,
        raw_content=raw_content,
        segment=segment,
        provenance=provenance,
        line_number=1,
    )
    safe_item = _normalized_item(
        item_id="safe-item",
        title=SAFE,
        body={"body": SAFE},
        raw_content=SAFE,
        segment=SAFE,
        line_number=2,
    )
    store, receipt = _import_items([secret_item, safe_item])

    assert secret_marker not in store.stored_text()
    assert SAFE in store.stored_text()
    assert receipt["skipped_credentials"] == 1
    assert receipt["imported_count"] == 1
    assert len(store.objects) == 1
    assert secret_marker not in json.dumps(receipt)


def test_corpus_classifier_reads_the_line_and_not_the_file_path() -> None:
    """A password assignment and a refused-example sentence keep their classes.

    Classifying from the file path swaps these two.
    """

    from scripts.measure_importer_credential_corpus import classify_skip_line

    assignment = "export " + "PG" + "PASSWORD='" + "stand-in" + "-from-the-operator'"
    prose = "`" + "DB_" + "PASSWORD=" + "example" + "-value` in a note and"
    assert classify_skip_line(assignment) == "placeholder password example"
    assert classify_skip_line(prose) == "floor refused-example list"
    assert classify_skip_line("The operator copies the first line only.") == "other"


def test_corpus_partition_counts_a_repeated_line_as_already_duplicate() -> None:
    """A later copy of a skipped line is not newly skipped.

    Counting every skip as newly skipped reports (3, 0) here.
    """

    from scripts.measure_importer_credential_corpus import partition_skips

    newly_skipped, already_duplicate, duplicate_sequences = partition_skips(
        ["a", "b", "a", "c"],
        {1, 3, 4},
    )
    assert newly_skipped == 2
    assert already_duplicate == 1
    assert duplicate_sequences == {3}
