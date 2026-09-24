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

import json
from pathlib import Path
import subprocess
from typing import Callable
from uuid import uuid4

import pytest

from alicebot_api.chatgpt_import import import_chatgpt_source
from alicebot_api.continuity_evidence import ArchivedArtifactRef
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
