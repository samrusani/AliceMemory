"""What a refused ChatGPT conversation leaves on the log, and what it must not swallow.

A conversation whose transcript cannot be built is refused alone and named in the
receipt by its position. The first version also called ``logger.exception``, which
prints the whole traceback on stderr (twelve lines for one conversation, with the
text of the error, which can quote the export). It now logs one line with the
position, the stable code and the name of the error type, and the traceback is at
debug level.

A ``MemoryError`` is not a fact about one conversation. The process is out of memory
and the next conversation fails the same way, so it leaves the command and is not
counted as an unreadable conversation.

Every test names the mutation that must fail it. Each mutation was made by hand and
the file was restored from a saved copy.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import alicebot_api
import alicebot_api.vnext_capture as vnext_capture
from alicebot_api.vnext_capture import CHATGPT_CONVERSATION_UNREADABLE_CODE, VNextCaptureService
from tests.unit.test_chatgpt_conversation_refusal import (
    _chain_conversation,
    _unreadable_conversation,
    _write,
)
from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore

SECRET_TITLE = "a-title-nobody-should-see-in-the-log"


def _export_with_one_unreadable_conversation(tmp_path: Path) -> Path:
    unreadable = _unreadable_conversation("second")
    unreadable["title"] = SECRET_TITLE
    return _write(tmp_path, [_chain_conversation("first", 3), unreadable, _chain_conversation("third", 3)])


def test_a_refused_conversation_logs_one_line_and_no_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One warning with the position, the code and the error type. No ``exc_info`` above debug.

    The line holds nothing from the export: not the title, not the text of the error.

    Mutation: log with ``logger.exception`` (or ``exc_info=True``) in the ``except
    Exception`` clause of ``import_chatgpt_export_file``. The record is then an
    error with a traceback attached.
    """

    export = _export_with_one_unreadable_conversation(tmp_path)
    store = InMemoryVNextCaptureStore()

    with caplog.at_level(logging.INFO, logger="alicebot_api.vnext_capture"):
        result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.status == "partial"
    refusals = [record for record in caplog.records if CHATGPT_CONVERSATION_UNREADABLE_CODE in record.getMessage()]
    assert len(refusals) == 1
    record = refusals[0]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    message = record.getMessage()
    assert "conversation_index=2" in message
    assert f"error_code={CHATGPT_CONVERSATION_UNREADABLE_CODE}" in message
    assert "error_type=OverflowError" in message
    assert SECRET_TITLE not in message
    assert "int too large" not in message, "the text of the error is not logged"
    assert [item for item in caplog.records if item.exc_info] == []


def test_the_traceback_is_there_at_debug_level(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The detail is not lost: whoever turns debug logging on gets the traceback.

    Mutation: delete the ``logger.debug`` call that carries ``exc_info``.
    """

    export = _export_with_one_unreadable_conversation(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="alicebot_api.vnext_capture"):
        VNextCaptureService(InMemoryVNextCaptureStore()).import_chatgpt_export_file(export)

    traced = [record for record in caplog.records if record.exc_info]
    assert len(traced) == 1
    assert traced[0].levelno == logging.DEBUG
    assert traced[0].exc_info is not None and traced[0].exc_info[0] is OverflowError


def test_the_command_prints_one_line_on_stderr_for_a_refused_conversation(tmp_path: Path) -> None:
    """``alice-memory import-chatgpt`` run for real: one line on stderr, the receipt on stdout.

    The run is a subprocess with its own ``HOME`` and a data directory in the test's
    directory, so the logging that is configured in a process of its own is the
    logging that is measured, and nothing outside the test directory is read.

    Mutation: log with ``logger.exception`` in the ``except Exception`` clause of
    ``import_chatgpt_export_file``. Stderr is then twelve lines and holds a traceback.
    """

    export = _export_with_one_unreadable_conversation(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    source_root = Path(alicebot_api.__file__).resolve().parent.parent
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("ALICE", "PYTHON"))
    }
    environment.update({"HOME": str(home), "PYTHONPATH": str(source_root)})

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alicebot_api.onramp",
            "import-chatgpt",
            "--from",
            str(export),
            "--data-dir",
            str(tmp_path / "vault"),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["status"] == "partial"
    assert receipt["errors"] == [f"conversation 2 refused: {CHATGPT_CONVERSATION_UNREADABLE_CODE}"]
    lines = completed.stderr.strip().splitlines()
    assert len(lines) == 1, completed.stderr
    assert "Traceback" not in completed.stderr
    assert "conversation_index=2" in lines[0]
    assert SECRET_TITLE not in completed.stderr


def test_a_memory_error_is_not_counted_as_an_unreadable_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running out of memory ends the import. It does not skip a conversation and go on.

    The second conversation raises ``MemoryError``. The command raises it, nothing is
    stored, and no refusal is recorded for a conversation that was never judged.

    Mutation: delete the ``except MemoryError: raise`` clause before the ``except
    Exception`` clause in ``import_chatgpt_export_file``. The conversation is then
    refused as unreadable and the other two are imported.
    """

    real_walk = vnext_capture._ordered_chatgpt_nodes

    def walk(conversation: dict[str, object]) -> list[dict[str, object]]:
        if conversation.get("id") == "second":
            raise MemoryError
        return real_walk(conversation)

    monkeypatch.setattr(vnext_capture, "_ordered_chatgpt_nodes", walk)
    export = _write(
        tmp_path,
        [_chain_conversation("first", 3), _chain_conversation("second", 3), _chain_conversation("third", 3)],
    )
    store = InMemoryVNextCaptureStore()

    with pytest.raises(MemoryError):
        VNextCaptureService(store).import_chatgpt_export_file(export)

    assert store.sources == []
    assert [event for event in store.events if event["event_type"] == "source.import_failed"] == []
