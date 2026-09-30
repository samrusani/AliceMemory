from __future__ import annotations

import builtins
import importlib
import json
import os
from pathlib import Path
import signal
from typing import Any
from uuid import uuid4

import pytest

from alicebot_api.chatgpt_import import ChatGPTImportValidationError, load_chatgpt_payload
from alicebot_api.vnext_capture import VNextCaptureService, VNextCaptureValidationError
from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore
from alicebot_api.importer_paths import read_contained_source_text
from alicebot_api.markdown_import import MarkdownImportValidationError, load_markdown_payload
from alicebot_api.openclaw_adapter import (
    OpenClawAdapterValidationError,
    list_openclaw_source_files,
    load_openclaw_payload,
)


_MARKDOWN_BODY = """---
fixture_id: containment-fixture
workspace_id: containment-workspace
---
- Decision: Keep the importer inside the selected root.
"""

_OUTSIDE_MARKER = "outside-the-import-root"

_CHATGPT_BODY: dict[str, object] = {
    "fixture_id": "containment-fixture",
    "workspace_id": "containment-workspace",
    "conversations": [
        {
            "id": "conversation-1",
            "title": "Containment",
            "messages": [{"role": "user", "text": "Decision: stay inside the root."}],
        }
    ],
}

_OPENCLAW_BODY: dict[str, object] = {
    "fixture_id": "containment-fixture",
    "id": "containment-workspace",
    "memories": [{"type": "decision", "text": "Stay inside the selected root."}],
}


def _raise_timeout(_signum: int, _frame: object) -> None:
    raise AssertionError("the importer blocked instead of refusing the source")


def _outside_tree(tmp_path: Path, filename: str, body: str) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / filename
    secret.write_text(body, encoding="utf-8")
    return secret


def _markdown_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.md").write_text(_MARKDOWN_BODY, encoding="utf-8")
    return root


def _chatgpt_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "export.json").write_text(json.dumps(_CHATGPT_BODY), encoding="utf-8")
    return root


def _openclaw_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    (root / "memories.json").write_text(json.dumps(_OPENCLAW_BODY), encoding="utf-8")
    return root


def test_markdown_import_rejects_a_file_symlink_escaping_the_root(tmp_path: Path) -> None:
    secret = _outside_tree(
        tmp_path,
        "secret.md",
        f"- Note: {_OUTSIDE_MARKER}\n",
    )
    root = _markdown_root(tmp_path)
    (root / "linked.md").symlink_to(secret)

    with pytest.raises(MarkdownImportValidationError, match="symlinked files"):
        load_markdown_payload(root)


def test_markdown_import_rejects_a_directory_symlink_escaping_the_root(tmp_path: Path) -> None:
    _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
    root = _markdown_root(tmp_path)
    (root / "linked_dir").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(MarkdownImportValidationError, match="symlinked directories"):
        load_markdown_payload(root)


def test_markdown_import_still_loads_a_root_without_symlinks(tmp_path: Path) -> None:
    _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
    root = _markdown_root(tmp_path)

    batch = load_markdown_payload(root)

    assert [item.source_file for item in batch.items] == ["notes.md"]
    assert _OUTSIDE_MARKER not in json.dumps([item.raw_content for item in batch.items])


def test_chatgpt_import_rejects_a_file_symlink_escaping_the_root(tmp_path: Path) -> None:
    secret = _outside_tree(tmp_path, "secret.json", json.dumps({"marker": _OUTSIDE_MARKER}))
    root = _chatgpt_root(tmp_path)
    (root / "linked.json").symlink_to(secret)

    with pytest.raises(ChatGPTImportValidationError, match="symlinked files"):
        load_chatgpt_payload(root)


def test_chatgpt_import_rejects_a_directory_symlink_escaping_the_root(tmp_path: Path) -> None:
    _outside_tree(tmp_path, "secret.json", json.dumps({"marker": _OUTSIDE_MARKER}))
    root = _chatgpt_root(tmp_path)
    (root / "linked_dir").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(ChatGPTImportValidationError, match="symlinked directories"):
        load_chatgpt_payload(root)


def test_chatgpt_import_still_loads_a_root_without_symlinks(tmp_path: Path) -> None:
    _outside_tree(tmp_path, "secret.json", json.dumps({"marker": _OUTSIDE_MARKER}))
    root = _chatgpt_root(tmp_path)

    batch = load_chatgpt_payload(root)

    assert [item.source_file for item in batch.items] == ["export.json"]


def test_openclaw_import_rejects_a_file_symlink_escaping_the_root(tmp_path: Path) -> None:
    secret = _outside_tree(tmp_path, "secret.json", json.dumps(_OPENCLAW_BODY))
    root = _openclaw_root(tmp_path)
    (root / "openclaw_memories.json").symlink_to(secret)

    with pytest.raises(OpenClawAdapterValidationError, match="symlinked files"):
        load_openclaw_payload(root)

    with pytest.raises(OpenClawAdapterValidationError, match="symlinked files"):
        list_openclaw_source_files(root)


def test_openclaw_import_ignores_a_symlinked_subdirectory(tmp_path: Path) -> None:
    _outside_tree(tmp_path, "secret.json", json.dumps({"marker": _OUTSIDE_MARKER}))
    root = _openclaw_root(tmp_path)
    (root / "linked_dir").symlink_to(tmp_path / "outside", target_is_directory=True)

    _source_path, selected = list_openclaw_source_files(root)

    assert [path.name for path in selected] == ["memories.json"]


def test_openclaw_import_still_loads_a_root_without_symlinks(tmp_path: Path) -> None:
    root = _openclaw_root(tmp_path)

    batch = load_openclaw_payload(root)

    assert batch.context.fixture_id == "containment-fixture"
    assert [item.source_file for item in batch.items] == ["memories.json"]


def test_a_symlinked_file_is_refused_even_when_it_targets_the_same_root(tmp_path: Path) -> None:
    root = _markdown_root(tmp_path)
    (root / "alias.md").symlink_to(root / "notes.md")

    with pytest.raises(MarkdownImportValidationError, match="symlinked files"):
        load_markdown_payload(root)


def test_read_contained_source_text_refuses_a_symlink_swapped_in_after_listing(tmp_path: Path) -> None:
    secret = _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
    root = _markdown_root(tmp_path)
    listed = root / "notes.md"
    listed.unlink()
    listed.symlink_to(secret)

    with pytest.raises(MarkdownImportValidationError, match="symlinked files"):
        read_contained_source_text(listed, error_factory=MarkdownImportValidationError)


def test_read_contained_source_text_returns_the_text_it_opened(tmp_path: Path) -> None:
    root = _markdown_root(tmp_path)

    assert read_contained_source_text(
        root / "notes.md",
        error_factory=MarkdownImportValidationError,
    ) == _MARKDOWN_BODY


def test_read_contained_source_text_refuses_upward_traversal(tmp_path: Path) -> None:
    secret = _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
    root = _markdown_root(tmp_path)

    with pytest.raises(MarkdownImportValidationError, match="traverse upward"):
        read_contained_source_text(
            root / ".." / "outside" / "secret.md",
            error_factory=MarkdownImportValidationError,
        )

    assert secret.read_text(encoding="utf-8").strip().endswith(_OUTSIDE_MARKER)


def test_read_contained_source_text_refuses_a_path_outside_the_declared_root(tmp_path: Path) -> None:
    secret = _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
    root = _markdown_root(tmp_path)

    with pytest.raises(MarkdownImportValidationError, match="escapes the selected root"):
        read_contained_source_text(
            secret,
            source_root=root,
            error_factory=MarkdownImportValidationError,
        )


def test_a_fifo_is_refused_instead_of_blocking_the_import(tmp_path: Path) -> None:
    """A FIFO with no writer parks a blocking open forever.

    The regular-file check runs after the open, so without O_NONBLOCK the
    importer never reaches it. Guard the whole call with an alarm so a
    regression shows up as a failure rather than a hung suite.
    """

    root = _markdown_root(tmp_path)
    os.mkfifo(root / "pipe.md")

    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(10)
    try:
        with pytest.raises(MarkdownImportValidationError, match="not a regular file"):
            load_markdown_payload(root)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_character_device_is_refused_as_a_source_file() -> None:
    with pytest.raises(MarkdownImportValidationError, match="not a regular file"):
        read_contained_source_text(
            Path("/dev/zero"),
            error_factory=MarkdownImportValidationError,
        )


def test_a_hardlink_into_the_root_is_a_documented_limitation(tmp_path: Path) -> None:
    """Pin the one attack containment cannot stop, so it cannot drift silently.

    A hard link is not a reference to a file, it is the file: same inode, same
    content, no owning directory to compare against. Refusing every file with
    a link count above one would reject ordinary content, so the importer reads
    it. The boundary that matters is the operator choosing the root.
    """

    secret = _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
    root = _markdown_root(tmp_path)
    os.link(secret, root / "hardlinked.md")

    batch = load_markdown_payload(root)

    assert _OUTSIDE_MARKER in json.dumps([item.raw_content for item in batch.items])


@pytest.mark.parametrize(
    ("module_name", "filename", "body", "importer"),
    (
        ("alicebot_api.markdown_import", "notes.md", _MARKDOWN_BODY, "import_markdown_source"),
        (
            "alicebot_api.chatgpt_import",
            "export.json",
            json.dumps(_CHATGPT_BODY),
            "import_chatgpt_source",
        ),
        (
            "alicebot_api.openclaw_import",
            "memories.json",
            json.dumps(_OPENCLAW_BODY),
            "import_openclaw_source",
        ),
    ),
)
def test_each_importer_opens_a_source_file_once_and_archives_what_it_parsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    filename: str,
    body: str,
    importer: str,
) -> None:
    """Prove the open-once claim by instrumentation, not by reading the code."""

    module = importlib.import_module(module_name)
    root = tmp_path / "root"
    root.mkdir()
    source = root / filename
    source.write_text(body, encoding="utf-8")

    opened: list[str] = []
    real_os_open = os.open
    real_builtin_open = builtins.open

    def counting_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path) == str(source):
            opened.append("os.open")
        return real_os_open(path, flags, *args, **kwargs)

    def counting_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, os.PathLike)) and str(file) == str(source):
            opened.append("builtins.open")
        return real_builtin_open(file, *args, **kwargs)

    archived: dict[str, Any] = {}
    parsed: dict[str, Any] = {}

    def fake_archive(_store: object, **kwargs: Any) -> list[object]:
        archived["files"] = kwargs["files"]
        return []

    def fake_import(_store: object, **kwargs: Any) -> dict[str, object]:
        parsed["batch"] = kwargs["batch"]
        return {}

    monkeypatch.setattr(module, "archive_import_source_files", fake_archive)
    monkeypatch.setattr(module, "import_normalized_batch", fake_import)
    monkeypatch.setattr(os, "open", counting_os_open)
    monkeypatch.setattr(builtins, "open", counting_builtin_open)
    try:
        getattr(module, importer)(object(), user_id=uuid4(), source=root)
    finally:
        monkeypatch.setattr(os, "open", real_os_open)
        monkeypatch.setattr(builtins, "open", real_builtin_open)

    assert opened == ["os.open"], f"source opened {len(opened)} times: {opened}"

    archived_files = archived["files"]
    assert len(archived_files) == 1
    # The archived evidence is the decoded text the parse consumed, taken from
    # the same in-memory snapshot rather than a second read. It is text and not
    # the disk bytes: the read applies universal newlines, so a CRLF source
    # archives as LF and will not checksum against the original file.
    assert archived_files[0].content_text == body
    assert parsed["batch"].items


def test_openclaw_snapshot_skips_neighbours_the_selection_rule_excludes(tmp_path: Path) -> None:
    """A neighbour that will not be parsed must never be opened either.

    Selection used to run after every top-level ``.json`` had been read, so one
    unrelated file with bytes that are not UTF-8 failed an import that had no
    reason to look at it.
    """

    root = _openclaw_root(tmp_path)
    (root / "workspace.json").write_text(json.dumps(_OPENCLAW_BODY), encoding="utf-8")
    (root / "unrelated.json").write_bytes(b'{"note": "\xff\xfe not utf-8"}')

    _source_path, selected = list_openclaw_source_files(root)
    assert [path.name for path in selected] == ["workspace.json", "memories.json"]

    batch = load_openclaw_payload(root)
    assert batch.items
    assert "unrelated.json" not in {item.source_file for item in batch.items}


def test_read_contained_source_text_names_the_file_that_is_not_utf8(tmp_path: Path) -> None:
    """A selected file with undecodable bytes leaves as the caller's own error.

    Raised bare, a ``UnicodeDecodeError`` reports a byte offset and no path,
    which does not tell an operator which file to go and look at.
    """

    root = _markdown_root(tmp_path)
    broken = root / "broken.md"
    broken.write_bytes(b"- Note: \xff\xfe not utf-8\n")

    with pytest.raises(MarkdownImportValidationError, match="not valid UTF-8 text") as caught:
        read_contained_source_text(broken, error_factory=MarkdownImportValidationError)

    assert broken.name in str(caught.value)
    # The offset is still recoverable for anyone debugging the source file.
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def _vnext_import(kind: str, path: Path) -> None:
    service = VNextCaptureService(InMemoryVNextCaptureStore())
    if kind == "markdown":
        service.import_markdown_folder(path)
        return
    service.import_chatgpt_export_file(path)


@pytest.mark.parametrize("kind", ["markdown", "chatgpt"])
def test_vnext_import_rejects_a_file_symlink_escaping_the_root(tmp_path: Path, kind: str) -> None:
    """T4. A linked file outside the root is refused.

    Fails if the folder walk goes back to rglob, which follows the link
    and imports the outside file.
    """

    if kind == "markdown":
        secret = _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
        root = _markdown_root(tmp_path)
        (root / "linked.md").symlink_to(secret)
    else:
        secret = _outside_tree(tmp_path, "secret.json", json.dumps({"marker": _OUTSIDE_MARKER}))
        root = _chatgpt_root(tmp_path)
        (root / "linked.json").symlink_to(secret)

    with pytest.raises(VNextCaptureValidationError, match="symlinked files"):
        _vnext_import(kind, root)


@pytest.mark.parametrize("kind", ["markdown", "chatgpt"])
def test_vnext_import_rejects_a_directory_symlink_escaping_the_root(tmp_path: Path, kind: str) -> None:
    """T4. A linked directory outside the root is refused.

    Fails if rglob is restored: it descends the link.
    """

    if kind == "markdown":
        _outside_tree(tmp_path, "secret.md", f"- Note: {_OUTSIDE_MARKER}\n")
        root = _markdown_root(tmp_path)
    else:
        _outside_tree(tmp_path, "secret.json", json.dumps({"marker": _OUTSIDE_MARKER}))
        root = _chatgpt_root(tmp_path)
    (root / "linked_dir").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(VNextCaptureValidationError, match="symlinked directories"):
        _vnext_import(kind, root)


@pytest.mark.parametrize("kind", ["markdown", "chatgpt"])
def test_vnext_import_rejects_a_same_root_alias(tmp_path: Path, kind: str) -> None:
    """T4. A symlink to another file in the same root is still a symlink."""

    if kind == "markdown":
        root = _markdown_root(tmp_path)
        (root / "alias.md").symlink_to(root / "notes.md")
    else:
        root = _chatgpt_root(tmp_path)
        (root / "alias.json").symlink_to(root / "export.json")

    with pytest.raises(VNextCaptureValidationError, match="symlinked files"):
        _vnext_import(kind, root)


@pytest.mark.parametrize("kind", ["markdown", "chatgpt"])
def test_vnext_import_refuses_a_fifo_instead_of_blocking(tmp_path: Path, kind: str) -> None:
    """T4. A FIFO is refused. A blocking open fails this test by alarm."""

    if kind == "markdown":
        root = _markdown_root(tmp_path)
        os.mkfifo(root / "pipe.md")
    else:
        root = _chatgpt_root(tmp_path)
        os.mkfifo(root / "pipe.json")

    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(10)
    try:
        with pytest.raises(VNextCaptureValidationError, match="not a regular file"):
            _vnext_import(kind, root)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
