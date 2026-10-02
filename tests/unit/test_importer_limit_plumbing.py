"""The limit a caller states reaches every read an importer makes, and the OpenClaw default holds.

``read_contained_source_text`` and ``snapshot_source_files`` take a required
``max_bytes``, so no read can be made without a number. The number still has to be
the caller's: a call site that passes the default constant in its place compiles,
runs, and refuses nothing a caller lowered and nothing a caller raised. These tests
put a file between the caller's limit and the default, in each direction, at each
call site that reads: the OpenClaw single file, the OpenClaw folder and the ChatGPT
directory (the Markdown paths are covered in ``test_importer_size_limits.py``).

The OpenClaw default is the 16 MiB text default. Nothing before this module showed
it enforced, only that the signature held the constant.

Every test names the mutation that must fail it. Each mutation was made by hand and
the file was restored from a saved copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alicebot_api.chatgpt_import import (
    _snapshot_chatgpt_source,
    import_chatgpt_source,
    load_chatgpt_payload,
)
from alicebot_api.importer_paths import (
    DEFAULT_MAX_CHATGPT_EXPORT_BYTES,
    DEFAULT_MAX_TEXT_FILE_BYTES,
    MIB,
    ImportFileTooLargeError,
)
from alicebot_api.openclaw_adapter import (
    list_openclaw_source_files,
    load_openclaw_payload,
    snapshot_openclaw_source,
)
from alicebot_api.openclaw_import import import_openclaw_source
from alicebot_api.vnext_capture import ImportFileTooLargeRefused, VNextCaptureService
from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore

USER_ID = "00000000-0000-0000-0000-000000000001"


def _sparse_file(path: Path, size: int) -> Path:
    """A file of ``size`` NUL bytes that takes no disk, so no test pays to read it."""

    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


class _NoStore:
    """A store that fails the test on first use.

    Every import reads its snapshot before it touches the store, so a refusal
    leaves this object alone and a read that is wrongly allowed reaches it.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the store was used ({name}) after a file over the limit")


def _openclaw_folder(tmp_path: Path, *, size: int) -> Path:
    folder = tmp_path / "openclaw"
    folder.mkdir()
    _sparse_file(folder / "memory.json", size)
    return folder


def _chatgpt_folder(tmp_path: Path, *, size: int) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    _sparse_file(folder / "conversations.json", size)
    return folder


# ---------------------------------------------------------------------------
# OpenClaw: single file and folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["single file", "folder"])
@pytest.mark.parametrize("reader", ["snapshot", "list", "load"])
def test_a_lower_limit_from_the_caller_refuses_an_openclaw_file(tmp_path: Path, shape: str, reader: str) -> None:
    """A file of 200 bytes is far under the 16 MiB default and over the caller's 100.

    Mutation: pass ``DEFAULT_MAX_TEXT_FILE_BYTES`` in place of ``max_file_bytes`` as
    ``max_bytes`` in ``snapshot_openclaw_source``, in the single-file branch (the
    "single file" cases) or in the ``snapshot_source_files`` call (the "folder"
    cases). The file is then read and the call does not raise.
    """

    target = tmp_path / "memory.json" if shape == "single file" else _openclaw_folder(tmp_path, size=200)
    if shape == "single file":
        _sparse_file(target, 200)
    call = {"snapshot": snapshot_openclaw_source, "list": list_openclaw_source_files, "load": load_openclaw_payload}[
        reader
    ]

    with pytest.raises(ImportFileTooLargeError) as caught:
        call(target, max_file_bytes=100)

    assert caught.value.size_bytes == 200
    assert caught.value.limit_bytes == 100
    assert caught.value.file_path.name == "memory.json"


@pytest.mark.parametrize("shape", ["single file", "folder"])
def test_a_higher_limit_from_the_caller_lets_an_openclaw_file_over_the_default_through(
    tmp_path: Path, shape: str
) -> None:
    """A file of 17 MiB is over the 16 MiB default and under the caller's 20 MiB.

    Mutation: the same as above. With the default passed in place of the caller's
    limit the file is refused at 16 MiB and the call raises.
    """

    size = 17 * MIB
    if shape == "single file":
        target = _sparse_file(tmp_path / "memory.json", size)
    else:
        target = _openclaw_folder(tmp_path, size=size)

    _path, snapshot = snapshot_openclaw_source(target, max_file_bytes=20 * MIB)

    assert len(snapshot) == 1
    assert len(snapshot[0].text) == size


@pytest.mark.parametrize("shape", ["single file", "folder"])
def test_the_openclaw_default_is_sixteen_mib_and_is_enforced(tmp_path: Path, shape: str) -> None:
    """With no limit given a 16 MiB file is read and one byte more is refused.

    The signature held the constant. This shows the read uses it, for the
    single file and for a folder, in the snapshot and in the loader that parses it.

    Mutation: give ``snapshot_openclaw_source`` the ChatGPT default
    (``DEFAULT_MAX_CHATGPT_EXPORT_BYTES``), or a default no file can exceed. The
    file one byte over the limit is then read and the call does not raise.
    """

    def target_of(size: int, name: str) -> Path:
        if shape == "single file":
            return _sparse_file(tmp_path / f"{name}.json", size)
        folder = tmp_path / name
        folder.mkdir()
        _sparse_file(folder / "memory.json", size)
        return folder

    at_limit = target_of(DEFAULT_MAX_TEXT_FILE_BYTES, "at-limit")
    over = target_of(DEFAULT_MAX_TEXT_FILE_BYTES + 1, "over")

    _path, snapshot = snapshot_openclaw_source(at_limit)
    assert len(snapshot[0].text) == 16 * MIB
    with pytest.raises(ImportFileTooLargeError) as caught:
        snapshot_openclaw_source(over)
    assert caught.value.limit_bytes == 16 * MIB
    assert caught.value.size_bytes == 16 * MIB + 1
    with pytest.raises(ImportFileTooLargeError):
        list_openclaw_source_files(over)
    with pytest.raises(ImportFileTooLargeError):
        load_openclaw_payload(over)


@pytest.mark.parametrize("shape", ["single file", "folder"])
def test_the_openclaw_import_passes_the_callers_limit_before_it_touches_the_store(
    tmp_path: Path, shape: str
) -> None:
    """``import_openclaw_source`` hands its ``max_file_bytes`` to the snapshot.

    The store would fail the test if it were used, and the snapshot is taken
    before the store is.

    Mutation: delete ``max_file_bytes=max_file_bytes`` from the
    ``snapshot_openclaw_source`` call in ``import_openclaw_source`` (the default
    then applies and a 200 byte file is read), or pass the ChatGPT default.
    """

    if shape == "single file":
        target = _sparse_file(tmp_path / "memory.json", 200)
    else:
        target = _openclaw_folder(tmp_path, size=200)

    with pytest.raises(ImportFileTooLargeError):
        import_openclaw_source(_NoStore(), user_id=USER_ID, source=target, max_file_bytes=100)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ChatGPT: a directory
# ---------------------------------------------------------------------------


def test_a_lower_limit_from_the_caller_refuses_a_file_in_a_chatgpt_directory(tmp_path: Path) -> None:
    """The directory read goes through ``snapshot_source_files``, and the caller's limit reaches it.

    Mutation: pass ``DEFAULT_MAX_CHATGPT_EXPORT_BYTES`` in place of
    ``max_file_bytes`` as ``max_bytes`` in the ``snapshot_source_files`` call of
    ``_snapshot_chatgpt_source``. The 200 byte file is then read.
    """

    folder = _chatgpt_folder(tmp_path, size=200)

    with pytest.raises(ImportFileTooLargeError) as caught:
        _snapshot_chatgpt_source(folder, max_file_bytes=100)

    assert caught.value.file_path.name == "conversations.json"
    assert caught.value.size_bytes == 200
    assert caught.value.limit_bytes == 100
    with pytest.raises(ImportFileTooLargeError):
        load_chatgpt_payload(folder, max_file_bytes=100)
    with pytest.raises(ImportFileTooLargeError):
        import_chatgpt_source(_NoStore(), user_id=USER_ID, source=folder, max_file_bytes=100)  # type: ignore[arg-type]


def test_a_higher_limit_from_the_caller_lets_a_chatgpt_directory_file_over_the_text_default_through(
    tmp_path: Path,
) -> None:
    """17 MiB is over the 16 MiB text default and under the 512 MiB ChatGPT default.

    A directory read that used the text default in place of the ChatGPT one would
    refuse an export a single-file read accepts.

    Mutation: pass ``DEFAULT_MAX_TEXT_FILE_BYTES`` in the ``snapshot_source_files``
    call of ``_snapshot_chatgpt_source``. The directory is then refused at 16 MiB.
    """

    folder = _chatgpt_folder(tmp_path, size=17 * MIB)

    _path, snapshot = _snapshot_chatgpt_source(folder, max_file_bytes=DEFAULT_MAX_CHATGPT_EXPORT_BYTES)

    assert len(snapshot[0].text) == 17 * MIB


def test_the_service_refuses_a_chatgpt_directory_file_over_the_callers_limit(tmp_path: Path) -> None:
    """The service turns the directory refusal into the typed one and stores nothing.

    Mutation: pass the default constant in place of ``max_file_bytes`` in the
    ``_snapshot_chatgpt_source`` call of ``import_chatgpt_export_file``.
    """

    folder = _chatgpt_folder(tmp_path, size=200)
    store = InMemoryVNextCaptureStore()

    with pytest.raises(ImportFileTooLargeRefused, match="the limit is 100 bytes"):
        VNextCaptureService(store).import_chatgpt_export_file(folder, max_file_bytes=100)

    assert store.sources == []
    assert store.events == []
