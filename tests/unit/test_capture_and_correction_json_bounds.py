"""The capture commit, the correction and the connector configuration services hold caller JSON to the write limits.

The HTTP routes ``POST /v0/continuity/captures/commit`` and ``POST /v0/continuity/review/.../corrections``, the CLI options
``--body-json``, ``--provenance-json`` and their replacement twins, and ``alice_commit_captures`` all end in these services.
Each measured the serialized size of its JSON first, and serializing a value nested a thousand levels deep overflowed
the serializer; a number that is not finite reached the database. The services now ask ``refuse_unstorable_json`` before the
size, answer with their own validation error, and write nothing for a refused request.

Mutations, each one alone: stop asking in ``commit_continuity_captures``, in ``apply_continuity_correction`` or in
``VNextConnectorService.update_config``; each makes the matching test fail.
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from alicebot_api.continuity_capture import ContinuityCaptureValidationError, commit_continuity_captures
from alicebot_api.continuity_review import ContinuityReviewValidationError, apply_continuity_correction
from alicebot_api.contracts import ContinuityCaptureCommitInput, ContinuityCorrectionInput
from alicebot_api.json_bounds import MAX_STORED_JSON_DEPTH
from alicebot_api.vnext_connectors import VNextConnectorService, VNextConnectorValidationError
from tests.unit.test_continuity_capture import ContinuityCaptureStoreStub
from tests.unit.test_continuity_review import ContinuityReviewStoreStub

LIMIT = MAX_STORED_JSON_DEPTH
USER = UUID("11111111-1111-4111-8111-111111111111")


def _objects(depth: int) -> dict[str, object]:
    value: dict[str, object] = {"text": "x"}
    for _ in range(depth - 1):
        value = {"k": value}
    return value


BAD = (
    pytest.param(_objects(LIMIT + 1), "must nest at most 64 levels", id="one level over"),
    pytest.param(_objects(950), "must nest at most 64 levels", id="950 levels"),
    pytest.param(_objects(100_000), "must nest at most 64 levels", id="100000 levels"),
    pytest.param({"text": "x", "score": float("nan")}, "must not hold NaN", id="NaN"),
    pytest.param({"text": "x", "score": float("inf")}, "must not hold NaN", id="Infinity"),
    pytest.param({"text": "x", "score": json.loads("1e999")}, "must not hold NaN", id="1e999"),
)


@pytest.mark.parametrize(("bad", "message"), BAD)
@pytest.mark.parametrize("field", ("body", "provenance", "replacement_body", "replacement_provenance"))
def test_the_capture_and_correction_services_refuse_what_cannot_be_stored(field: str, bad: dict, message: str) -> None:
    store = ContinuityReviewStoreStub()
    row = store.add_object(title="Decision: Keep rollout phased", status="active")
    request = ContinuityCorrectionInput(action="edit", **{field: bad})
    with pytest.raises(ContinuityReviewValidationError, match=message):
        apply_continuity_correction(store, user_id=USER, continuity_object_id=row["id"], request=request)  # type: ignore[arg-type]
    assert store.call_log == [], "a refused correction writes nothing"

    capture_store = ContinuityCaptureStoreStub()
    with pytest.raises(ContinuityCaptureValidationError, match=message):
        commit_continuity_captures(
            capture_store,  # type: ignore[arg-type]
            user_id=capture_store.user_id,
            request=ContinuityCaptureCommitInput(mode="assist", sync_fingerprint="sync:bounds", candidates=[bad]),  # type: ignore[arg-type]
        )


def test_a_correction_inside_the_limits_is_applied() -> None:
    store = ContinuityReviewStoreStub()
    row = store.add_object(title="Decision: Keep rollout phased", status="active")
    payload = apply_continuity_correction(
        store,  # type: ignore[arg-type]
        user_id=USER,
        continuity_object_id=row["id"],
        request=ContinuityCorrectionInput(action="edit", title="Decision: Updated", body={"text": "Updated", "extra": _objects(LIMIT - 2)}),
    )
    assert payload["correction_event"]["action"] == "edit"


@pytest.mark.parametrize(("bad", "message"), BAD[:1] + BAD[3:4])
def test_the_connector_config_is_held_to_the_limits(bad: dict, message: str) -> None:
    class NoStore:
        def __getattr__(self, name: str):
            raise AssertionError(f"the store was read ({name}) before the configuration was refused")

    service = VNextConnectorService.__new__(VNextConnectorService)
    service.store = NoStore()  # type: ignore[assignment]
    with pytest.raises(VNextConnectorValidationError, match=message):
        service.update_config("agent_output", config_json=bad)
