"""A file an operator names is read the way an importer reads its source: bounded, once, no link.

``VNextCaptureService.capture_file`` (``alicebot vnext sources capture-file``),
``alicebot vnext connectors browser-clipper capture --file`` and ``alicebot vnext agents ingest-output
--file`` each read a whole file with ``Path.read_text``. That read follows a link
that is put in place of the file after the path was resolved, opens a FIFO and waits
on it, and reads a file of any size into memory. They now use the read the importers
use (``read_contained_source_text``), with the importers' default of 16 MiB and
``--max-file-mib`` to change it.

The path the caller names is still resolved first, so a link the caller typed is
followed, as it is by the importers. What is refused is a file that is a link, or
anything that is not a regular file, at the moment it is opened.

Every test names the mutation that must fail it. Each mutation was made by hand and
the file was restored from a saved copy.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

import alicebot_api.cli as cli_module
from alicebot_api.importer_paths import DEFAULT_MAX_TEXT_FILE_BYTES, MIB
from alicebot_api.vnext_capture import (
    ImportFileTooLargeRefused,
    VNextCaptureService,
    VNextCaptureValidationError,
    read_named_text_file,
)
from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore


def _sparse_file(path: Path, size: int) -> Path:
    """A file of ``size`` NUL bytes that takes no disk, so no test pays to read it."""

    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


def _error_record(stderr: str) -> dict[str, Any]:
    records = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert len(records) == 1
    assert "Traceback" not in stderr
    error: dict[str, Any] = records[0]["error"]
    return error


def _forbid_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that wrongly passes the size check fails fast instead of being captured for minutes."""

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a file over the limit reached the capture")

    monkeypatch.setattr(VNextCaptureService, "capture_source", forbidden)


# ---------------------------------------------------------------------------
# the read
# ---------------------------------------------------------------------------


def test_the_read_is_bounded_and_names_the_file_it_refused(tmp_path: Path) -> None:
    """``read_named_text_file`` stops at the limit and the refusal is the public one.

    The refusal names the file by its name and never its path, as the import
    refusals do. A file exactly at the limit is read.

    Mutation: read with ``Path.read_text`` in ``read_named_text_file`` (no limit,
    so nothing is refused), or compare ``>=`` against the limit in
    ``read_contained_source_text``.
    """

    note = tmp_path / "note.md"
    note.write_text("- Note: " + "x" * 42 + "\n", encoding="utf-8")
    size = note.stat().st_size
    assert read_named_text_file(note, max_file_bytes=size).startswith("- Note: ")

    with pytest.raises(ImportFileTooLargeRefused) as caught:
        read_named_text_file(note, max_file_bytes=size - 1)

    assert caught.value.reason_code == "import_file_too_large"
    assert f"note.md is {size} bytes" in str(caught.value)
    assert f"the limit is {size - 1} bytes" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_the_default_limit_is_the_importers_sixteen_mib() -> None:
    """The default of the read and of ``capture_file`` is 16 MiB, the importers' text default.

    Mutation: change the default of ``read_named_text_file`` or of
    ``capture_file`` to another number, or to the ChatGPT export default.
    """

    assert DEFAULT_MAX_TEXT_FILE_BYTES == 16 * MIB
    assert inspect.signature(read_named_text_file).parameters["max_file_bytes"].default == 16 * MIB
    assert inspect.signature(VNextCaptureService.capture_file).parameters["max_file_bytes"].default == 16 * MIB


def test_a_file_that_is_not_utf8_is_refused_by_name_not_by_byte_offset(tmp_path: Path) -> None:
    """A decode failure leaves as the capture error that names the file.

    ``read_text`` raised ``UnicodeDecodeError``, a byte offset with no file in it.
    A file whose name carries a credential is named as withheld.

    Mutation: read with ``Path.read_text`` in ``read_named_text_file``.
    """

    broken = tmp_path / "broken.md"
    broken.write_bytes(b"- Note: \xff\xfe not text\n")
    with pytest.raises(VNextCaptureValidationError, match="not valid UTF-8 text: broken.md"):
        read_named_text_file(broken)

    token = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"
    flagged = tmp_path / f"{token}.md"
    flagged.write_bytes(b"\xff\xfe")
    with pytest.raises(VNextCaptureValidationError) as caught:
        read_named_text_file(flagged)
    assert token not in str(caught.value)
    assert "withheld" in str(caught.value)


def test_line_endings_are_translated_as_read_text_translated_them(tmp_path: Path) -> None:
    """The text, and so the content hash, of a note with Windows line endings does not change.

    Mutation: delete the universal newline translation in
    ``read_contained_source_text``. A note captured before this change would
    then capture a second time under a different hash.
    """

    note = tmp_path / "crlf.md"
    note.write_bytes(b"- Note: one\r\n- Note: two\r- Note: three\n")
    assert read_named_text_file(note) == note.read_text(encoding="utf-8") == "- Note: one\n- Note: two\n- Note: three\n"


# ---------------------------------------------------------------------------
# capture_file
# ---------------------------------------------------------------------------


def test_capture_file_refuses_a_file_over_the_limit_before_it_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is read and nothing is stored. The caller's limit decides, in both directions.

    ``os.fdopen`` is how the bytes are read, so it fails the test if it is reached
    for the file that is over the limit. A limit of the file's own size reads it.

    Mutation: ``capture_file`` reads with ``file_path.read_text`` (no limit), or
    ignores ``max_file_bytes`` and applies the default.
    """

    note = tmp_path / "note.md"
    note.write_text("- Note: " + "x" * 192 + "\n", encoding="utf-8")
    assert note.stat().st_size == 201
    store = InMemoryVNextCaptureStore()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the file was read before its size was checked")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", forbidden)
        with pytest.raises(ImportFileTooLargeRefused, match="the limit is 200 bytes"):
            VNextCaptureService(store).capture_file(note, max_file_bytes=200)
    assert store.sources == []
    assert store.events == []

    result = VNextCaptureService(store).capture_file(note, max_file_bytes=201)
    assert result.status == "imported"
    assert len(store.sources) == 1


def test_capture_file_applies_the_default_when_no_limit_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A note of 16 MiB plus one byte is refused with no limit given.

    Mutation: change the default of ``capture_file``.
    """

    _forbid_capture(monkeypatch)
    huge = _sparse_file(tmp_path / "huge.md", DEFAULT_MAX_TEXT_FILE_BYTES + 1)

    with pytest.raises(ImportFileTooLargeRefused, match=r"the limit is 16777216 bytes \(16.00 MiB\)"):
        VNextCaptureService(InMemoryVNextCaptureStore()).capture_file(huge)


def test_capture_file_refuses_a_file_swapped_for_a_link_after_the_path_was_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between resolving the path and opening the file is closed.

    The real ``Path.resolve`` runs, and then the note is replaced by a link to a file
    elsewhere, which is the swap a local process can make. The open refuses a link, and
    the text of the other file is not stored.

    Mutation: ``capture_file`` reads with ``file_path.read_text``, or the open leaves
    out ``O_NOFOLLOW``. The other file's text is then stored.
    """

    note = tmp_path / "note.md"
    note.write_text("- Note: a plain note\n", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text("- Note: text from somewhere else\n", encoding="utf-8")
    real_resolve = Path.resolve
    swapped: list[Path] = []

    def resolve_then_swap(self: Path, *args: Any, **kwargs: Any) -> Path:
        resolved = real_resolve(self, *args, **kwargs)
        if resolved.name == "note.md" and not swapped:
            swapped.append(resolved)
            resolved.unlink()
            resolved.symlink_to(other)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_swap)
    store = InMemoryVNextCaptureStore()

    with pytest.raises(VNextCaptureValidationError, match="symlinked files"):
        VNextCaptureService(store).capture_file(note)

    assert swapped, "the swap did not run, so the assertions above are vacuous"
    assert store.sources == []
    assert "somewhere else" not in json.dumps(store.events, default=str)


def test_capture_file_follows_a_link_the_caller_named_and_keeps_the_identity(tmp_path: Path) -> None:
    """A link typed on the command line is resolved first, as the importers resolve their source.

    The source's identity is the resolved path, as it was before this change, so a
    note captured earlier is found again and not captured twice.

    Mutation: stop resolving the path (use ``Path(path).expanduser()``), which makes
    the link itself the path that is opened and refuses it, or put the unresolved
    path in ``external_id``.
    """

    real = tmp_path / "real.md"
    real.write_text("- Note: the real note\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    store = InMemoryVNextCaptureStore()

    result = VNextCaptureService(store).capture_file(link)

    assert result.status == "imported"
    assert store.sources[0]["external_id"] == str(real.resolve())
    assert store.sources[0]["title"] == "real.md"


def test_capture_file_refuses_what_is_not_a_regular_file(tmp_path: Path) -> None:
    """A directory with a note's name is refused as what it is, not read as text.

    ``read_text`` raised ``IsADirectoryError``, an operating system error.

    Mutation: read with ``file_path.read_text`` in ``capture_file``.
    """

    folder = tmp_path / "folder.md"
    folder.mkdir()
    store = InMemoryVNextCaptureStore()

    with pytest.raises(VNextCaptureValidationError, match="not a regular file"):
        VNextCaptureService(store).capture_file(folder)

    assert store.sources == []


def test_capture_file_does_not_wait_on_a_pipe(tmp_path: Path) -> None:
    """A FIFO under a note's name, with no writer, is refused at once. Its open must not park.

    The capture runs in a thread so a read that does wait fails the test after a few
    seconds and not by hanging the run. The thread is released afterwards.

    Mutation: open without ``O_NONBLOCK`` in ``read_contained_source_text``, or read
    with ``file_path.read_text``. The open then parks on the pipe.
    """

    pipe = tmp_path / "pipe.md"
    os.mkfifo(pipe)
    store = InMemoryVNextCaptureStore()
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            VNextCaptureService(store).capture_file(pipe)
        except BaseException as exc:  # noqa: BLE001 - the outcome is what the test inspects
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)
    if worker.is_alive():
        # Give the parked open its writer so the thread ends, then fail.
        writer = os.open(pipe, os.O_WRONLY)
        os.close(writer)
        worker.join(timeout=5)
        pytest.fail("the capture waited on a pipe that has no writer")

    assert len(outcome) == 1
    assert isinstance(outcome[0], VNextCaptureValidationError)
    assert "not a regular file" in str(outcome[0])
    assert store.sources == []


# ---------------------------------------------------------------------------
# the three commands
# ---------------------------------------------------------------------------


def _patched_cli_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryVNextCaptureStore:
    store = InMemoryVNextCaptureStore()

    @contextmanager
    def fake_vnext_store_context(_ctx: object) -> Iterator[InMemoryVNextCaptureStore]:
        yield store

    monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
    return store


class _RecordingConnectorService:
    """Stands in for the connector service and keeps what the command handed it."""

    payloads: list[dict[str, Any]] = []

    def __init__(self, _store: object, **_kwargs: object) -> None:
        pass

    def capture_browser_clip(self, payload: dict[str, Any], **_kwargs: object) -> Any:
        self.payloads.append(payload)
        return SimpleNamespace(to_record=lambda: {"status": "ok"}, deferred_embedding_inputs=())

    def ingest_agent_output(self, payload: dict[str, Any], **_kwargs: object) -> Any:
        self.payloads.append(payload)
        return SimpleNamespace(to_record=lambda: {"status": "ok"}, deferred_embedding_inputs=())


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _patched_cli_store(monkeypatch)
    _RecordingConnectorService.payloads = []
    monkeypatch.setattr(cli_module, "VNextConnectorService", _RecordingConnectorService)
    return _RecordingConnectorService.payloads


def _browser_capture(path: Path, *extra: str) -> list[str]:
    return ["vnext", "connectors", "browser-clipper", "capture", "--url", "https://example.com/page", "--file", str(path), *extra]


def _ingest_output(path: Path, *extra: str) -> list[str]:
    return ["vnext", "agents", "ingest-output", "--agent-id", "agent-1", "--title", "Run", "--file", str(path), *extra]


def test_capture_file_command_takes_the_flag_and_answers_with_the_typed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A note of 1.1 MiB is over ``--max-file-mib 1`` and under ``--max-file-mib 2``.

    Mutation: stop passing ``max_file_bytes`` from ``_run_vnext_sources_capture_file``,
    so the flag is accepted and ignored, or delete the flag from the ``capture-file``
    parser (the command then exits 2).
    """

    store = _patched_cli_store(monkeypatch)
    big = tmp_path / "wide.md"
    big.write_text("- Note: " + "steady words " * 90_000, encoding="utf-8")
    assert MIB < big.stat().st_size < 2 * MIB

    assert cli_module.main(["vnext", "sources", "capture-file", str(big), "--max-file-mib", "1"]) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "wide.md is" in error["message"]
    assert "the limit is 1048576 bytes (1.00 MiB)" in error["message"]
    assert "--max-file-mib" in error["message"]
    assert str(tmp_path) not in error["message"]
    assert store.sources == []

    assert cli_module.main(["vnext", "sources", "capture-file", str(big), "--max-file-mib", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "imported"
    assert len(store.sources) == 1


def test_capture_file_command_applies_the_default_with_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """16 MiB plus one byte is refused with no flag, and the message says 16 MiB.

    Mutation: change the fallback that ``_file_limit_bytes`` gets in
    ``_run_vnext_sources_capture_file`` to the ChatGPT default.
    """

    _patched_cli_store(monkeypatch)
    _forbid_capture(monkeypatch)
    huge = _sparse_file(tmp_path / "huge.md", DEFAULT_MAX_TEXT_FILE_BYTES + 1)

    assert cli_module.main(["vnext", "sources", "capture-file", str(huge)]) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "(16.00 MiB)" in error["message"]


@pytest.mark.parametrize("build", [_browser_capture, _ingest_output], ids=["page capture", "agent output"])
def test_the_file_options_read_the_file_once_and_hand_its_text_on(
    tmp_path: Path, recorded: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], build: Any
) -> None:
    """The text of the file, with its line endings translated, reaches the service.

    A link the caller typed is followed to its target, as before.

    Mutation: leave ``.resolve()`` out of the path the handler (``_run_vnext_browser_clip``
    or ``_run_vnext_agents_ingest_output``) opens. The link itself is then opened, the
    open refuses it, and the command exits 1.
    """

    real = tmp_path / "output.md"
    real.write_bytes(b"- Note: first\r\n- Note: second\n")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    assert cli_module.main(build(link)) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    assert len(recorded) == 1
    key = "page_text" if build is _browser_capture else "content"
    assert recorded[0][key] == "- Note: first\n- Note: second\n"


@pytest.mark.parametrize("build", [_browser_capture, _ingest_output], ids=["page capture", "agent output"])
def test_the_file_options_take_the_flag_and_refuse_an_oversized_file_before_the_service(
    tmp_path: Path, recorded: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], build: Any
) -> None:
    """3 MiB is over ``--max-file-mib 1``. The service never sees the text.

    Mutation: stop passing the limit from the handler (``_run_vnext_browser_clip``
    or ``_run_vnext_agents_ingest_output``), or delete the flag from its parser.
    """

    big = _sparse_file(tmp_path / "wide.md", 3 * MIB)

    assert cli_module.main(build(big, "--max-file-mib", "1")) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "wide.md is 3145728 bytes (3.00 MiB)" in error["message"]
    assert "--max-file-mib" in error["message"]
    assert recorded == []


@pytest.mark.parametrize("build", [_browser_capture, _ingest_output], ids=["page capture", "agent output"])
def test_the_file_options_apply_the_default_with_no_flag(
    tmp_path: Path, recorded: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], build: Any
) -> None:
    """16 MiB plus one byte is refused with no flag.

    Mutation: change the fallback that ``_file_limit_bytes`` gets in either handler
    to the ChatGPT default, or to a number no file exceeds.
    """

    huge = _sparse_file(tmp_path / "huge.md", DEFAULT_MAX_TEXT_FILE_BYTES + 1)

    assert cli_module.main(build(huge)) == 1

    error = _error_record(capsys.readouterr().err)
    assert error["code"] == "import_file_too_large"
    assert "(16.00 MiB)" in error["message"]
    assert recorded == []


@pytest.mark.parametrize("build", [_browser_capture, _ingest_output], ids=["page capture", "agent output"])
def test_the_file_options_refuse_a_file_that_is_not_a_regular_file(
    tmp_path: Path, recorded: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], build: Any
) -> None:
    """A directory is refused as a bad request, not read.

    Mutation: read with ``Path.read_text`` in either handler. The error is then an
    operating system failure (``filesystem_failed``).
    """

    folder = tmp_path / "folder.md"
    folder.mkdir()

    assert cli_module.main(build(folder)) == 1

    assert _error_record(capsys.readouterr().err)["code"] == "invalid_request"
    assert recorded == []


@pytest.mark.parametrize("value", ["0", "1.5", "lots"])
@pytest.mark.parametrize(
    "build",
    [
        lambda path, *extra: ["vnext", "sources", "capture-file", str(path), *extra],
        _browser_capture,
        _ingest_output,
    ],
    ids=["capture-file", "page capture", "agent output"],
)
def test_the_new_flags_share_the_floor_of_the_import_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], build: Any, value: str
) -> None:
    """There is no value that means no limit, and no fractional MiB.

    Mutation: give one of the three parsers its own type that accepts ``0``.
    """

    assert cli_module.main(build(tmp_path / "x.md", "--max-file-mib", value)) == 2
    assert _error_record(capsys.readouterr().err)["code"] == "invalid_request"
