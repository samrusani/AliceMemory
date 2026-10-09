"""The edge and belief review services judge the id and the action before they look at a row, for a caller with a ceiling.

The routes look the row up first and hand the service a canonical id, so over HTTP the checks below never decide an
answer. The services are public, though, and a caller that reaches them with a ceiling and an id that is not an id would
send the database a value it cannot cast. These tests call the services over a store that records what it is asked, and
fails the way the database does when it is handed text that is not an id.

Mutations that must fail this file, each alone: read the id of an edge or of a belief without checking that it is an id
(the services in ``vnext_connections.py`` and ``vnext_contradictions.py``); check the action only for a caller without a
ceiling.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from alicebot_api.vnext_connections import (
    VNextConnectionNotFoundError,
    VNextConnectionService,
    VNextConnectionValidationError,
)
from alicebot_api.vnext_contradictions import (
    VNextContradictionNotFoundError,
    VNextContradictionService,
    VNextContradictionValidationError,
)

CEILING = ("public", "internal", "private", "unknown")


class RecordingStore:
    """Answers no row, records every read, and refuses text that is not an id the way a uuid column does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _read(self, name: str, row_id: str) -> None:
        self.calls.append((name, row_id))
        UUID(str(row_id))  # the database cast: text that is not an id is an error, not a missing row
        return None

    def get_edge(self, edge_id: str):
        return self._read("get_edge", edge_id)

    def get_belief(self, belief_id: str):
        return self._read("get_belief", belief_id)

    def update_edge_status(self, **_kwargs):
        raise AssertionError("a refused review wrote an edge")

    def update_belief_status(self, **_kwargs):
        raise AssertionError("a refused review wrote a belief")


def _edge(store: RecordingStore, edge_id: str, action: str):
    return VNextConnectionService(store).review_edge(edge_id=edge_id, action=action, sensitivity_allowed=CEILING)  # type: ignore[arg-type]


def _belief(store: RecordingStore, belief_id: str, action: str):
    return VNextContradictionService(store).review_belief(belief_id=belief_id, action=action, sensitivity_allowed=CEILING)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("review", "missing"),
    [(_edge, VNextConnectionNotFoundError), (_belief, VNextContradictionNotFoundError)],
    ids=["edge", "belief"],
)
def test_an_id_that_is_not_an_id_is_a_missing_row_and_never_reaches_the_store(review, missing) -> None:
    store = RecordingStore()
    action = "review" if review is _edge else "reinforce"
    for text in ("not-an-id", "", " ", "00000000-0000-0000-0000", "{}", "1"):
        with pytest.raises(missing):
            review(store, text, action)
    assert store.calls == []


@pytest.mark.parametrize(
    ("review", "invalid"),
    [(_edge, VNextConnectionValidationError), (_belief, VNextContradictionValidationError)],
    ids=["edge", "belief"],
)
def test_an_action_that_does_not_exist_is_a_bad_request_before_the_row_is_looked_up(review, invalid) -> None:
    store = RecordingStore()
    for row_id in (str(uuid4()), "not-an-id"):
        with pytest.raises(invalid):
            review(store, row_id, "no-such-action")
    assert store.calls == []


@pytest.mark.parametrize(
    ("review", "missing", "read", "action"),
    [
        (_edge, VNextConnectionNotFoundError, "get_edge", "review"),
        (_belief, VNextContradictionNotFoundError, "get_belief", "reinforce"),
    ],
    ids=["edge", "belief"],
)
def test_a_well_formed_id_that_names_no_row_is_a_missing_row_after_one_read(review, missing, read, action) -> None:
    store = RecordingStore()
    row_id = str(uuid4())
    with pytest.raises(missing):
        review(store, row_id, action)
    assert store.calls == [(read, row_id)]
