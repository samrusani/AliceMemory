"""One ChatGPT conversation that cannot be read does not fail the others.

``alice-memory import-chatgpt`` builds a transcript for every conversation in
the export before it writes any of them. A transcript that raised used to end
the whole command: a conversation of about 3,000 messages overran the
recursion limit in the mapping walk, and nothing was imported, from that
conversation or from any other. The receipt now names the one conversation by
its position and the rest import, which is how a conversation whose capture
fails already behaved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import alicebot_api.vnext_capture as vnext_capture
from alicebot_api.vnext_capture import (
    CHATGPT_CONVERSATION_UNREADABLE_CODE,
    SOURCE_IMPORT_ERROR_CODE,
    VNextCaptureService,
    VNextCaptureValidationError,
)
from tests.unit.test_vnext_capture import InMemoryVNextCaptureStore


def _token() -> str:
    return "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyz"


def _chain_conversation(conversation_id: str, nodes: int) -> dict[str, object]:
    mapping: dict[str, object] = {}
    for position in range(nodes):
        node_id = f"{conversation_id}-n{position}"
        mapping[node_id] = {
            "id": node_id,
            "parent": f"{conversation_id}-n{position - 1}" if position else None,
            "children": [f"{conversation_id}-n{position + 1}"] if position + 1 < nodes else [],
            "message": {
                "author": {"role": "user" if position % 2 == 0 else "assistant"},
                "create_time": 1_700_000_000 + position,
                "content": {"parts": [f"message {position} of {conversation_id}"]},
            },
        }
    return {
        "id": conversation_id,
        "title": f"Title {conversation_id}",
        "mapping": mapping,
        "current_node": f"{conversation_id}-n{nodes - 1}",
    }


def _unreadable_conversation(conversation_id: str, *, with_credential: bool = False) -> dict[str, object]:
    """A conversation whose ``create_time`` is an integer no float can hold.

    ``float()`` of it raises ``OverflowError`` after every message has been
    read, which is the kind of failure a malformed export produces at a point
    the transcript builder does not guard.
    """

    conversation = _chain_conversation(conversation_id, 2)
    conversation["create_time"] = 10**400
    if with_credential:
        mapping = conversation["mapping"]
        assert isinstance(mapping, dict)
        first = mapping[f"{conversation_id}-n0"]
        assert isinstance(first, dict)
        message = first["message"]
        assert isinstance(message, dict)
        message["content"] = {"parts": [f"deploy with {_token()}"]}
    return conversation


def _write(tmp_path: Path, conversations: list[dict[str, object]]) -> Path:
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(conversations), encoding="utf-8")
    return path


def test_a_conversation_of_three_thousand_messages_imports(tmp_path: Path) -> None:
    """The case from the report: a long chain used to fail the whole import.

    Mutation that must fail it: restore the recursive walk in
    ``_ordered_chatgpt_nodes``. The walk then raises ``RecursionError``, the
    conversation is refused, and the status is ``failed`` and not ``ok``.
    """

    store = InMemoryVNextCaptureStore()
    export = _write(tmp_path, [_chain_conversation("c1", 3000)])

    result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.status == "ok"
    assert result.imported_count == 1
    assert result.failed_count == 0
    metadata = store.sources[0]["metadata_json"]
    assert metadata["message_count"] == 3000
    transcript = metadata["raw_text"]
    assert transcript.index("message 0 of c1") < transcript.index("message 1500 of c1")
    assert transcript.index("message 1500 of c1") < transcript.index("message 2999 of c1")


def test_one_unreadable_conversation_is_named_by_position_and_the_others_import(tmp_path: Path) -> None:
    """The middle conversation is refused. The first and third are stored.

    Mutation that must fail it: remove the ``try``/``except Exception`` around
    ``_chatgpt_conversation_transcript`` in ``import_chatgpt_export_file``. The
    ``OverflowError`` then leaves the command and nothing is stored.
    """

    store = InMemoryVNextCaptureStore()
    export = _write(
        tmp_path,
        [
            _chain_conversation("first", 3),
            _unreadable_conversation("second"),
            _chain_conversation("third", 3),
        ],
    )

    result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.status == "partial"
    assert result.imported_count == 2
    assert result.failed_count == 1
    assert result.skipped_count == 0
    assert result.error_code == SOURCE_IMPORT_ERROR_CODE
    assert result.errors == (f"conversation 2 refused: {CHATGPT_CONVERSATION_UNREADABLE_CODE}",)
    assert CHATGPT_CONVERSATION_UNREADABLE_CODE == "conversation_unreadable"
    assert [source["external_id"] for source in store.sources] == ["first", "third"]
    # A refused conversation keeps its place: the third is still the third.
    assert [source["metadata_json"]["conversation_index"] for source in store.sources] == [1, 3]
    assert [source["metadata_json"]["export_conversation_count"] for source in store.sources] == [3, 3]


def test_the_refusal_is_recorded_as_an_event_without_the_conversation(tmp_path: Path) -> None:
    """The event log gets the typed code and the position, and nothing from the export.

    Mutation that must fail it: drop the ``self._log_event`` call for a
    refused conversation. The event is then missing.
    """

    store = InMemoryVNextCaptureStore()
    export = _write(tmp_path, [_chain_conversation("first", 2), _unreadable_conversation("second-secret-title")])

    VNextCaptureService(store).import_chatgpt_export_file(export)

    failures = [event for event in store.events if event["event_type"] == "source.import_failed"]
    assert len(failures) == 1
    payload = failures[0]["payload_json"]
    assert payload["error_code"] == CHATGPT_CONVERSATION_UNREADABLE_CODE
    assert payload["error_message"] == "Conversation could not be read"
    assert payload["title"] is None
    assert payload["metadata_json"] == {"filename": "conversations.json", "conversation_index": 2}
    assert "second-secret-title" not in json.dumps(failures[0], default=str)


def test_every_conversation_unreadable_is_a_failed_import_and_stores_nothing(tmp_path: Path) -> None:
    """With nothing readable the batch status is ``failed``, which exits 1.

    Mutation that must fail it: count a refused conversation as skipped
    instead of failed. The status is then ``skipped`` and the exit code 0.
    """

    store = InMemoryVNextCaptureStore()
    export = _write(tmp_path, [_unreadable_conversation("a"), _unreadable_conversation("b")])

    result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.status == "failed"
    assert result.failed_count == 2
    assert result.imported_count == 0
    assert result.errors == (
        f"conversation 1 refused: {CHATGPT_CONVERSATION_UNREADABLE_CODE}",
        f"conversation 2 refused: {CHATGPT_CONVERSATION_UNREADABLE_CODE}",
    )
    assert store.sources == []


def test_a_conversation_refused_half_way_leaves_no_credential_skips_behind(tmp_path: Path) -> None:
    """A token in a message of a conversation that is then refused is not reported.

    The transcript builder records a credential skip as it reads each message,
    and the refusal comes after them. The receipt must not name a message of a
    conversation it did not read.

    Mutation that must fail it: pass ``credential_items`` to the transcript
    builder directly instead of a list that is merged only on success.
    """

    store = InMemoryVNextCaptureStore()
    export = _write(
        tmp_path,
        [_unreadable_conversation("held", with_credential=True), _chain_conversation("kept", 2)],
    )

    result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.failed_count == 1
    assert result.imported_count == 1
    assert result.skipped_credentials == 0
    assert result.skipped_credential_items == ()
    assert _token() not in json.dumps([source["metadata_json"] for source in store.sources], default=str)


def test_a_credential_skip_in_a_readable_conversation_is_still_reported(tmp_path: Path) -> None:
    """The merge keeps the skips of conversations that are read.

    Mutation that must fail it: drop the merge loop that copies
    ``conversation_skips`` into ``credential_items``.
    """

    conversation = _unreadable_conversation("held", with_credential=True)
    del conversation["create_time"]
    store = InMemoryVNextCaptureStore()
    export = _write(tmp_path, [conversation])

    result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.failed_count == 0
    assert result.skipped_credentials >= 1
    assert result.skipped_credential_items[0].startswith("conversation held message 1")


def test_a_recursion_error_in_one_conversation_is_contained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any failure while a transcript is built is contained, a deep one included.

    The walk no longer recurses, so this stands in for the next thing that
    does: the walk raises ``RecursionError`` for the one conversation.

    Mutation that must fail it: narrow the ``except Exception`` to
    ``except OverflowError``.
    """

    real_walk = vnext_capture._ordered_chatgpt_nodes

    def walk(conversation: dict[str, object]) -> list[dict[str, object]]:
        if conversation.get("id") == "deep":
            raise RecursionError("maximum recursion depth exceeded")
        return real_walk(conversation)

    monkeypatch.setattr(vnext_capture, "_ordered_chatgpt_nodes", walk)
    store = InMemoryVNextCaptureStore()
    export = _write(tmp_path, [_chain_conversation("deep", 2), _chain_conversation("fine", 2)])

    result = VNextCaptureService(store).import_chatgpt_export_file(export)

    assert result.status == "partial"
    assert result.errors == (f"conversation 1 refused: {CHATGPT_CONVERSATION_UNREADABLE_CODE}",)
    assert [source["external_id"] for source in store.sources] == ["fine"]


def test_a_file_nested_past_the_decoder_depth_is_refused_with_a_reason(tmp_path: Path) -> None:
    """Arrays nested a hundred thousand deep are not an export.

    The decoder raises ``RecursionError``. It left the command as an untyped
    failure before, and it is a refusal that says why now. Nothing is written.

    Mutation that must fail it: remove the ``except RecursionError`` clause
    around ``json.loads``.
    """

    export = tmp_path / "conversations.json"
    export.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    store = InMemoryVNextCaptureStore()

    with pytest.raises(VNextCaptureValidationError, match="nested too deeply"):
        VNextCaptureService(store).import_chatgpt_export_file(export)

    assert store.sources == []
    assert store.events == []
