"""Correction and capture-commit inputs are bounded before anything is scanned.

Why this file exists. The review of S4.4 round 1 (finding 8, P2, 2026-09-23)
measured apply_continuity_correction with a 3 MB body at 44 s of CPU and
1.58 GB of RSS: the new credential check read the whole payload before the
object was even looked up, while v0.16.0 answered the same request in 0.01 s.
How it escaped: the round 1 tests only sent small bodies. These tests send
the large ones and assert the check is never reached, by replacing it with a
recorder rather than by timing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from alicebot_api import continuity_capture, continuity_review
from alicebot_api.continuity_capture import ContinuityCaptureValidationError, commit_continuity_captures
from alicebot_api.continuity_review import (
    ContinuityReviewNotFoundError,
    ContinuityReviewValidationError,
    apply_continuity_correction,
)
from alicebot_api.contracts import ContinuityCaptureCommitInput, ContinuityCorrectionInput
from alicebot_api.mcp.registry import _validate_mcp_arguments_against_advertised_schema
from alicebot_api.mcp.shared import MCPToolError
from alicebot_api.write_bounds import (
    MAX_CAPTURE_CANDIDATE_CHARS,
    MAX_CAPTURE_COMMIT_CANDIDATES,
    MAX_CORRECTION_FIELD_CHARS,
    MAX_CORRECTION_TITLE_CHARS,
    serialized_chars,
)

from tests.unit.test_credential_floor_every_door import _StoredContinuityObject

THREE_MB = "x" * (3 * 1024 * 1024)


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Every call the correction path makes to the credential check."""

    calls: list[tuple[object, ...]] = []

    def recorder(*fields: object, error: object) -> None:
        calls.append(fields)

    monkeypatch.setattr(continuity_review, "refuse_credential_material", recorder)
    return calls


def test_a_missing_object_is_reported_without_scanning_a_three_megabyte_body(scans) -> None:
    store = _StoredContinuityObject()
    with pytest.raises(ContinuityReviewNotFoundError):
        apply_continuity_correction(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            continuity_object_id=uuid4(),
            request=ContinuityCorrectionInput(action="edit", body={"decision_text": THREE_MB}),
        )
    assert scans == []
    assert store.writes == []


@pytest.mark.parametrize("field", ["body", "provenance", "replacement_body", "replacement_provenance"])
def test_an_oversized_mapping_is_refused_before_it_is_scanned(scans, field: str) -> None:
    store = _StoredContinuityObject()
    oversized = {"decision_text": "x" * MAX_CORRECTION_FIELD_CHARS}
    assert serialized_chars(oversized) > MAX_CORRECTION_FIELD_CHARS
    action = "supersede" if field.startswith("replacement") else "edit"
    with pytest.raises(ContinuityReviewValidationError, match=f"{field} must serialize"):
        apply_continuity_correction(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            continuity_object_id=store.object_id,
            request=ContinuityCorrectionInput(action=action, **{field: oversized}),  # type: ignore[arg-type]
        )
    assert scans == []
    assert store.writes == []


def test_a_mapping_at_the_bound_is_scanned_and_written(scans) -> None:
    # Guards the guard: the bound is not refusing everything.
    store = _StoredContinuityObject()
    body = {"decision_text": "x" * (MAX_CORRECTION_FIELD_CHARS - 40)}
    assert serialized_chars(body) <= MAX_CORRECTION_FIELD_CHARS
    with pytest.raises(LookupError):
        apply_continuity_correction(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            continuity_object_id=store.object_id,
            request=ContinuityCorrectionInput(action="edit", body=body),
        )
    assert scans, "the credential check ran"
    assert store.writes == ["create_continuity_correction_event"]


@pytest.mark.parametrize("field", ["title", "replacement_title"])
def test_an_oversized_title_is_refused_before_it_is_scanned(scans, field: str) -> None:
    store = _StoredContinuityObject()
    with pytest.raises(ContinuityReviewValidationError, match=f"{field} must be"):
        apply_continuity_correction(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            continuity_object_id=store.object_id,
            request=ContinuityCorrectionInput(action="delete", **{field: "t" * (MAX_CORRECTION_TITLE_CHARS + 1)}),  # type: ignore[arg-type]
        )
    assert scans == []


class _CaptureStore:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def __getattr__(self, name: str) -> object:
        def record(*_args: object, **_kwargs: object) -> object:
            self.writes.append(name)
            raise LookupError(name)

        return record


def _candidate(text: str) -> dict[str, object]:
    return {"candidate_type": "decision", "object_type": "Decision", "normalized_text": text, "confidence": 0.98}


def test_capture_commit_refuses_too_many_candidates_before_reading_any(monkeypatch) -> None:
    normalized: list[object] = []
    monkeypatch.setattr(continuity_capture, "_normalize_candidate", lambda payload: normalized.append(payload))
    store = _CaptureStore()
    with pytest.raises(ContinuityCaptureValidationError, match=f"at most {MAX_CAPTURE_COMMIT_CANDIDATES}"):
        commit_continuity_captures(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            request=ContinuityCaptureCommitInput(
                mode="assist", candidates=[_candidate("ship") for _ in range(MAX_CAPTURE_COMMIT_CANDIDATES + 1)]
            ),
        )
    assert normalized == [] and store.writes == []


def test_capture_commit_refuses_an_oversized_candidate_before_reading_any(monkeypatch) -> None:
    normalized: list[object] = []
    monkeypatch.setattr(continuity_capture, "_normalize_candidate", lambda payload: normalized.append(payload))
    store = _CaptureStore()
    with pytest.raises(ContinuityCaptureValidationError, match="must serialize"):
        commit_continuity_captures(
            store,  # type: ignore[arg-type]
            user_id=uuid4(),
            request=ContinuityCaptureCommitInput(
                mode="assist", candidates=[_candidate("ship"), _candidate("x" * MAX_CAPTURE_CANDIDATE_CHARS)]
            ),
        )
    assert normalized == [] and store.writes == []


def test_alice_memory_correct_bounds_the_whole_body_before_scanning(tmp_path, monkeypatch) -> None:
    """Each string can pass the schema while the mapping as a whole does not."""

    from alicebot_api.mcp import review as mcp_review
    from alicebot_api.mcp_tools import call_mcp_tool

    from tests.unit.test_credential_floor_every_door import _sqlite_context

    scanned: list[object] = []
    monkeypatch.setattr(mcp_review, "refuse_credential_material", lambda *fields, error: scanned.append(fields))
    context = _sqlite_context(tmp_path)
    committed = call_mcp_tool(
        context, name="alice_memory_commit", arguments={"title": "deploy", "canonical_text": "Deploys go out on Tuesdays."}
    )
    half = MAX_CORRECTION_FIELD_CHARS // 2 + 10
    with pytest.raises(MCPToolError, match="body must serialize"):
        call_mcp_tool(
            context,
            name="alice_memory_correct",
            arguments={
                "review_item_id": committed["memory"]["id"],
                "action": "edit-and-approve",
                "body": {"text": "x" * half, "body": "y" * half},
            },
        )
    assert scanned == []


@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        ("alice_review_apply", {"action": "edit-and-approve", "title": "t" * (MAX_CORRECTION_TITLE_CHARS + 1)}, "title"),
        ("alice_review_apply", {"action": "edit-and-approve", "body": {"body": "x" * (MAX_CORRECTION_FIELD_CHARS + 1)}}, "body"),
        ("alice_memory_correct", {"action": "edit-and-approve", "replacement_title": "t" * 281}, "replacement_title"),
        ("alice_memory_correct", {"action": "edit-and-approve", "body": {"text": THREE_MB}}, "body"),
    ],
)
def test_the_mcp_schemas_bound_titles_and_body_strings(tool: str, arguments: dict[str, object], path: str) -> None:
    with pytest.raises(MCPToolError, match=f"arguments.{path}.*at most"):
        _validate_mcp_arguments_against_advertised_schema(tool, {"review_item_id": str(uuid4()), **arguments})
    # Guards the guard: the same call at the bound passes the schema.
    _validate_mcp_arguments_against_advertised_schema(tool, {"review_item_id": str(uuid4()), "action": "approve"})
