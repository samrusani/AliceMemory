"""A roll-up pass does not raise when a card the pass did not make this time already holds its digest key.

A roll-up card has the memory key ``vnext.rollup.<digest>``, and the digest comes from the members, so a
proposal for members that have not changed since an earlier card was made has the key that card holds. The
unique index on ``(user, profile, memory_key)`` holds the key in every status. #518 held the group back for a
card whose validity window had closed. Two more ways of holding the key still made the pass raise
``IntegrityError`` in v0.20.0 and on main before this change:

* a reviewer rejected the card (status ``rejected``, window open), and
* the confirmation-age arm of the staleness sweep marked the card ``stale`` (it leaves ``valid_to`` open).

Both are reproduced here on SQLite. A superseded card, a card outside the fence of the pass and a row that is not a
roll-up card hold the key the same way, so the pass reports every one of them as held back.

A card archived through ``update_memory`` holds the key too, and it is the one case the store's own read hides:
``update_memory`` sets ``deleted_at`` and keeps ``memory_key``, and ``get_memory_by_key`` skips rows with
``deleted_at`` set, so a read that guards the key must ask for soft-deleted rows (``include_deleted=True``). The
archived rows in these tests are made that way, through ``update_memory``, never by creating a row with the status
``archived``, which leaves ``deleted_at`` empty and does not reach the case.

Every test names, in its docstring, the change to the code that must fail it. No test calls a model, an
embeddings endpoint or the network.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from alicebot_api.vnext_rollups import VNextRollupService, _may_name_card
from alicebot_api.vnext_scheduler import SchedulerRunRequest, VNextSchedulerService
from alicebot_api.vnext_stores.postgres.memory_access import get_memory_by_key as postgres_get_memory_by_key
from tests.unit.test_expired_memories_everywhere import (
    ALL_SENSITIVITY,
    FAR_FUTURE,
    _ArtifactShim,
    _digest_card,
    _memory_store,
)
from tests.unit.test_vnext_rollups import GAME_SPECS, _rollup_candidates, _seed_game_memories


def _rollup_card_count(store: object) -> int:
    """Every roll-up card in the store, whatever its status."""

    rows = store.conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) FROM memories WHERE memory_key LIKE 'vnext.rollup.%'"
    ).fetchone()
    return int(rows[0])


def _first_card(store: object) -> tuple[VNextRollupService, str]:
    """Seed four game memories, run the pass once, and return the service and the id of the card it made."""

    _seed_game_memories(store, order=(0, 1, 2, 3))
    service = VNextRollupService(store, embedding_provider=None)  # type: ignore[arg-type]
    assert len(service.propose_rollups().proposals) == 1
    return service, str(_rollup_candidates(store)[0]["id"])


def _held(service: VNextRollupService, digest: str, **fence: object) -> tuple[bool, dict[str, object] | None]:
    """What ``_row_at_digest_key`` returns for ``digest`` under a fence that lets everything through, with overrides."""

    arguments: dict[str, object] = {
        "rollup_key": "topic:games",
        "domains": None,
        "sensitivity_allowed": list(ALL_SENSITIVITY),
        "projects": (),
    }
    arguments.update(fence)
    return service._row_at_digest_key(digest, **arguments)  # type: ignore[arg-type]


def test_a_rejected_card_with_an_open_window_holds_its_group_back_and_the_pass_does_not_raise() -> None:
    """A reviewer rejected the card for these members; the next pass reports the group and proposes nothing.

    Four game memories make one group. The first pass proposes a card and a reviewer rejects it (status
    ``rejected``, ``valid_to`` open, so #518's expired-card read does not name it). Before this change the second
    pass raised ``IntegrityError`` on the card's key. It now reports ``existing_card_members_unchanged``, names the
    card and its status, makes no new row, and a fifth memory changes the members so the next pass proposes a new
    card, not a revision of the rejected one.

    Mutations, each one alone: delete the ``_row_at_digest_key`` block from ``propose_rollups`` (the second pass
    raises ``IntegrityError``); make ``_row_at_digest_key`` return ``(False, None)`` for a row whose status is
    ``rejected`` (same).
    """

    with _memory_store() as store:
        service, card_id = _first_card(store)
        store.conn.execute("UPDATE memories SET status = 'rejected' WHERE id = ?", (card_id,))
        assert store.get_memory(card_id)["valid_to"] is None  # type: ignore[index]
        cards_before = _rollup_card_count(store)

        second = service.propose_rollups()
        assert second.proposals == []
        assert [group["state"] for group in second.groups] == ["existing_card_members_unchanged"]
        assert second.groups[0]["existing_memory_id"] == card_id
        assert second.groups[0]["existing_status"] == "rejected"
        assert _rollup_card_count(store) == cards_before

        _seed_game_memories(store, order=(4,))
        changed = service.propose_rollups()
        assert [group["state"] for group in changed.groups] == ["created"]
        assert len(changed.proposals) == 1 and changed.proposals[0]["revises_memory_id"] is None


def test_a_card_the_confirmation_age_arm_marked_stale_holds_its_group_back_and_the_pass_does_not_raise() -> None:
    """The sweep marks a card stale for lack of confirmation, with ``valid_to`` still open; the pass does not raise.

    The members are working-state memories (``project_state``) that were confirmed recently, so the real staleness
    sweep leaves them alone. The card is made active as an accepted one would be and has not been confirmed within
    the window, so the sweep marks it ``stale`` by the ``confirmation_window_elapsed`` arm and keeps ``valid_to``
    empty. Before this change the pass after the sweep raised ``IntegrityError`` on the card's key. It now reports
    ``existing_card_members_unchanged`` with the status ``stale``.

    Mutations, each one alone: delete the ``_row_at_digest_key`` block from ``propose_rollups`` (the pass after the
    sweep raises ``IntegrityError``); make ``_row_at_digest_key`` return ``(False, None)`` for a row whose status is
    ``stale`` (same).
    """

    with _memory_store() as store:
        service, card_id = _first_card(store)
        reference = (datetime.now(UTC) + timedelta(days=400)).isoformat()
        store.conn.execute("UPDATE memories SET memory_type = 'project_state' WHERE id != ?", (card_id,))
        store.conn.execute("UPDATE memories SET last_confirmed_at = ? WHERE id != ?", (reference, card_id))
        store.conn.execute(
            "UPDATE memories SET status = 'active', memory_type = 'project_state' WHERE id = ?", (card_id,)
        )

        report = VNextSchedulerService(_ArtifactShim(store))._run_staleness_sweep(  # type: ignore[arg-type]
            SchedulerRunRequest(workflow_type="staleness_sweep", options={"reference_time": reference}), metadata={}
        )
        assert report["metadata_json"]["stale_marked_memory_ids"] == [card_id]
        swept = store.get_memory(card_id)
        assert swept is not None and swept["status"] == "stale" and swept["valid_to"] is None
        cards_before = _rollup_card_count(store)

        after_sweep = service.propose_rollups()
        assert after_sweep.proposals == []
        assert [group["state"] for group in after_sweep.groups] == ["existing_card_members_unchanged"]
        assert after_sweep.groups[0]["existing_memory_id"] == card_id
        assert after_sweep.groups[0]["existing_status"] == "stale"
        assert _rollup_card_count(store) == cards_before

        _seed_game_memories(store, order=(4,))
        store.conn.execute("UPDATE memories SET memory_type = 'project_state' WHERE id != ?", (card_id,))
        store.conn.execute("UPDATE memories SET last_confirmed_at = ? WHERE id != ?", (reference, card_id))
        changed = service.propose_rollups()
        assert [group["state"] for group in changed.groups] == ["created"]
        assert len(changed.proposals) == 1 and changed.proposals[0]["revises_memory_id"] is None


@pytest.mark.parametrize(
    "status", ["candidate", "active", "accepted", "stale", "rejected", "superseded", "archived"]
)
def test_a_card_with_an_open_window_holds_the_key_in_every_status(status: str) -> None:
    """``_row_at_digest_key`` reports the key as held, and names the card, whatever the card's status.

    The row holds the memory key in every status, so a new card for the same members collides with it. The
    window is open here, which is the case #518's read leaves out.

    The ``archived`` card is archived through ``update_memory`` (``deleted_at`` set, key kept).

    Mutations, each one alone: test the status in ``_row_at_digest_key`` (return ``(False, None)`` for any status
    outside ``active`` and ``accepted``), which fails the ``candidate``, ``stale``, ``rejected``, ``superseded``
    and ``archived`` cases; drop ``include_deleted=True`` from the store call in ``_row_at_digest_key``, which
    fails the ``archived`` case.
    """

    with _memory_store() as store:
        card = _digest_card(store, "digest-open", status=status, valid_to=None)
        held, named = _held(VNextRollupService(store, embedding_provider=None), "digest-open")
        assert held is True
        assert named is not None and named["id"] == card["id"]


def test_a_card_archived_through_the_store_holds_its_group_back_and_the_pass_does_not_raise() -> None:
    """A card archived with ``update_memory`` still holds its key; the next pass reports the group and proposes nothing.

    ``update_memory`` with status ``archived`` sets ``deleted_at`` and keeps ``memory_key``, and the unique index
    counts the row. ``get_memory_by_key`` skips a row with ``deleted_at`` set, so before this change the read that
    guards the key did not see the card and the pass raised ``IntegrityError`` on it (as it did in v0.20.0). The pass
    now reports ``existing_card_members_unchanged`` with ``existing_status`` ``archived``, makes no new row, and a
    fifth memory changes the members so the next pass proposes a new card, not a revision of the archived one.

    Mutations, each one alone: drop ``include_deleted=True`` from the store call in ``_row_at_digest_key`` (the
    second pass raises ``IntegrityError``); delete the ``_row_at_digest_key`` block from ``propose_rollups`` (same).
    """

    with _memory_store() as store:
        service, card_id = _first_card(store)
        store.update_memory(memory_id=card_id, patch={"status": "archived"})
        archived = store.conn.execute("SELECT status, deleted_at FROM memories WHERE id = ?", (card_id,)).fetchone()
        assert archived[0] == "archived" and archived[1] is not None
        assert store.get_memory(card_id) is None
        cards_before = _rollup_card_count(store)

        second = service.propose_rollups()
        assert second.proposals == []
        assert [group["state"] for group in second.groups] == ["existing_card_members_unchanged"]
        assert second.groups[0]["existing_memory_id"] == card_id
        assert second.groups[0]["existing_status"] == "archived"
        assert _rollup_card_count(store) == cards_before

        _seed_game_memories(store, order=(4,))
        changed = service.propose_rollups()
        assert [group["state"] for group in changed.groups] == ["created"]
        assert len(changed.proposals) == 1 and changed.proposals[0]["revises_memory_id"] is None


def test_an_archived_card_with_a_closed_window_is_reported_as_expired_and_the_pass_does_not_raise() -> None:
    """A card that was archived and whose window has closed keeps #518's state, ``expired_card_members_unchanged``.

    The expired-card read runs first. It asked the store for live rows only, so before this change it did not see
    an archived card and the group fell through to the digest-key read, which reported
    ``existing_card_members_unchanged``; with neither read asking for soft-deleted rows the pass raised.

    Mutation: drop ``include_deleted=True`` from the store call in ``_expired_card_for_digest`` (the group is
    reported as ``existing_card_members_unchanged`` and the ``expired_memory_id`` key is missing).
    """

    with _memory_store() as store:
        service, card_id = _first_card(store)
        store.conn.execute("UPDATE memories SET valid_to = '2020-01-01T00:00:00Z' WHERE id = ?", (card_id,))
        store.update_memory(memory_id=card_id, patch={"status": "archived"})
        assert store.get_memory(card_id) is None
        cards_before = _rollup_card_count(store)

        outcome = service.propose_rollups()
        assert outcome.proposals == []
        assert [group["state"] for group in outcome.groups] == ["expired_card_members_unchanged"]
        assert outcome.groups[0]["expired_memory_id"] == card_id
        assert _rollup_card_count(store) == cards_before


def test_get_memory_by_key_skips_a_soft_deleted_row_unless_it_is_asked_for_one() -> None:
    """The SQLite read hides an archived row by default and returns it for ``include_deleted=True``.

    The default stays as it was, so every other caller still sees live rows only. A live row is returned either
    way, and a key no row holds is ``None`` either way.

    Mutations, each one alone, in ``vnext_stores/sqlite/memory_access.py``: make ``get_memory_by_key`` ignore
    ``include_deleted`` and always filter ``deleted_at IS NULL`` (the ``include_deleted=True`` read returns
    nothing and its assertion fails); make it never filter (the default read returns the archived row and the
    first assertion fails).
    """

    with _memory_store() as store:
        live = _digest_card(store, "digest-live", valid_to=None)
        archived = _digest_card(store, "digest-archived", valid_to=None, status="archived")
        assert store.get_memory_by_key(memory_key="vnext.rollup.digest-archived") is None
        assert store.get_memory_by_key(memory_key="vnext.rollup.digest-archived", include_deleted=False) is None
        found = store.get_memory_by_key(memory_key="vnext.rollup.digest-archived", include_deleted=True)
        assert found is not None and found["id"] == archived["id"] and found["status"] == "archived"
        assert found["deleted_at"] is not None
        for flag in (False, True):
            row = store.get_memory_by_key(memory_key="vnext.rollup.digest-live", include_deleted=flag)
            assert row is not None and row["id"] == live["id"]
            assert store.get_memory_by_key(memory_key="vnext.rollup.digest-nothing", include_deleted=flag) is None


def test_the_postgres_get_memory_by_key_filters_soft_deleted_rows_only_by_default() -> None:
    """The Postgres read builds the same query: ``deleted_at IS NULL`` by default, no such clause with ``include_deleted``.

    The Postgres store is not opened here. The function is called on a stand-in that records the SQL and the
    parameters it is given, so the test reads what the function asks the database for.

    Mutations, each one alone, in ``vnext_stores/postgres/memory_access.py``: always add the ``deleted_at IS NULL``
    clause (the ``include_deleted=True`` assertion fails); never add it (the default assertion fails); drop the
    ``include_deleted`` parameter (the call raises ``TypeError``).
    """

    seen: list[tuple[str, tuple[object, ...]]] = []

    def fetch_optional_one(sql: str, params: tuple[object, ...]) -> None:
        seen.append((sql, params))

    store = SimpleNamespace(_fetch_optional_one=fetch_optional_one)
    postgres_get_memory_by_key(store, memory_key="vnext.rollup.digest-x")
    postgres_get_memory_by_key(store, memory_key="vnext.rollup.digest-x", include_deleted=False)
    postgres_get_memory_by_key(store, memory_key="vnext.rollup.digest-x", include_deleted=True)
    default_sql, explicit_false_sql, include_sql = (entry[0] for entry in seen)
    assert "deleted_at IS NULL" in default_sql and "deleted_at IS NULL" in explicit_false_sql
    assert "deleted_at IS NULL" not in include_sql
    assert {entry[1] for entry in seen} == {("assistant_default", "vnext.rollup.digest-x")}


def test_a_key_no_row_holds_is_not_held_and_a_store_with_no_key_lookup_holds_nothing() -> None:
    """``_row_at_digest_key`` is ``(False, None)`` for a key with no row and for a store that cannot look a key up.

    Mutations, each one alone: return ``(True, None)`` for a missing row (the first assertion fails); delete the
    ``callable(getter)`` test (the store with no key lookup raises); read the key from anything but
    ``vnext.rollup.{digest}`` (``vnext.rollup.{rollup_key}``: the card of the next test is not found).
    """

    class WithoutKeyLookup:
        pass

    with _memory_store() as store:
        service = VNextRollupService(store, embedding_provider=None)
        _digest_card(store, "digest-there", valid_to=None)
        assert _held(service, "digest-not-there") == (False, None)
        assert _held(service, "digest-there")[0] is True
        assert _held(VNextRollupService(WithoutKeyLookup(), embedding_provider=None), "digest-there") == (  # type: ignore[arg-type]
            False,
            None,
        )


@pytest.mark.parametrize(
    ("card", "outside", "inside"),
    [
        pytest.param({"domain": "personal"}, {"domains": ["project"]}, {"domains": ["personal"]}, id="domain"),
        pytest.param(
            {"sensitivity": "private"},
            {"sensitivity_allowed": ["public", "internal"]},
            {"sensitivity_allowed": ["public", "internal", "private"]},
            id="sensitivity",
        ),
        pytest.param({"project": "alpha"}, {"projects": ("beta",)}, {"projects": ("alpha",)}, id="project"),
    ],
)
def test_a_card_outside_the_fence_of_the_pass_holds_the_key_but_is_not_named(
    card: dict[str, object], outside: dict[str, object], inside: dict[str, object]
) -> None:
    """The domain, the sensitivity ceiling and the projects of the pass each keep a held card from being named.

    The accepted-card read takes all three, so this read does too. Outside the fence the key is still held, so
    the pass does not try to create the card and raise, but the row is not named. A pass whose fence takes the
    card in names it.

    Mutations, each one alone, in the ``_scoped_rows`` call of ``_may_name_card``: pass ``domains=None`` (the
    ``domain`` case fails), pass ``sensitivity_allowed=list(ALL_SENSITIVITY)`` (the ``sensitivity`` case fails),
    pass ``projects=()`` (the ``project`` case fails). Deleting the ``_scoped_rows`` call fails all three.
    """

    with _memory_store() as store:
        service = VNextRollupService(store, embedding_provider=None)
        row = _digest_card(store, "digest-fenced", valid_to=None, **card)  # type: ignore[arg-type]
        assert _held(service, "digest-fenced", **outside) == (True, None)
        held, named = _held(service, "digest-fenced", **inside)
        assert held is True and named is not None and named["id"] == row["id"]
        assert _held(service, "digest-fenced")[1] is not None


def test_a_row_at_the_digest_key_that_is_not_this_groups_rollup_card_holds_the_key_but_is_not_named() -> None:
    """A row of another kind, or the card of another roll-up key, holds the key and is not named.

    Mutations, each one alone, in ``_may_name_card``: delete the ``_is_rollup_card`` test (the row of another
    kind is named); delete the ``rollup_key`` comparison (the card of another key is named).
    """

    with _memory_store() as store:
        service = VNextRollupService(store, embedding_provider=None)
        _digest_card(store, "digest-other-kind", candidate_kind="consolidation_candidate", valid_to=None)
        _digest_card(store, "digest-other-key", rollup_key="topic:cooking", valid_to=None)
        _digest_card(store, "digest-this-key", valid_to=None)
        assert _held(service, "digest-other-kind") == (True, None)
        assert _held(service, "digest-other-key") == (True, None)
        held, named = _held(service, "digest-this-key")
        assert held is True and named is not None


def test_a_card_outside_the_fence_makes_the_pass_report_the_group_held_without_naming_the_card() -> None:
    """The pass reports ``digest_key_held_by_another_row`` for a card it may not name, and neither raises nor names it.

    The first pass makes the card. A reviewer then reclassifies it as ``private``. The next pass runs under a
    ceiling of ``internal``, which takes the four members in and shuts the card out, so neither read names it. The
    key is still held, so creating the card would raise ``IntegrityError``. The group is reported with no card id.

    Mutations, each one alone: delete the ``held`` branch of ``propose_rollups`` (the pass raises
    ``IntegrityError``); make that branch name the card it cannot name (``existing_memory_id`` appears in the
    record).
    """

    with _memory_store() as store:
        service, card_id = _first_card(store)
        store.conn.execute("UPDATE memories SET sensitivity = 'private' WHERE id = ?", (card_id,))
        cards_before = _rollup_card_count(store)

        outcome = service.propose_rollups(sensitivity_allowed=["public", "internal"])
        assert outcome.proposals == []
        assert [group["state"] for group in outcome.groups] == ["digest_key_held_by_another_row"]
        record = outcome.groups[0]
        assert "existing_memory_id" not in record and "existing_status" not in record
        assert card_id not in repr(outcome.groups)
        assert _rollup_card_count(store) == cards_before


def test_the_pass_hands_its_own_fence_to_the_digest_key_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``propose_rollups`` gives ``_row_at_digest_key`` the domains, sensitivity ceiling and projects it was called with.

    Four game memories sit in project ``alpha``, domain ``project`` and sensitivity ``internal``, and the pass runs
    under a fence that takes them in and is not the default. The first pass makes the card, a reviewer rejects it,
    and the next pass reaches the read. The read gets the pass's own fence and the group's roll-up key, and the
    real read names the card, so the fence does not shut out the very card the pass made.

    Mutations, each one alone, at the ``_row_at_digest_key`` call in ``propose_rollups``: pass ``domains=None``,
    pass ``sensitivity_allowed=list(ALL_SENSITIVITY)``, pass ``projects=()``, pass a fixed ``rollup_key``.
    """

    fence = {"domains": ["project"], "sensitivity_allowed": ["internal", "private"], "projects": ("alpha",)}
    calls: list[dict[str, object]] = []
    original = VNextRollupService._row_at_digest_key

    def spy(self: VNextRollupService, rollup_digest: str, **kwargs: object) -> tuple[bool, dict[str, object] | None]:
        calls.append(kwargs)
        return original(self, rollup_digest, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(VNextRollupService, "_row_at_digest_key", spy)
    with _memory_store() as store:
        for index, (_key, text, session_date, _speaker) in enumerate(GAME_SPECS[:4]):
            store.create_memory(
                {
                    "memory_key": f"memory.alpha.{index}",
                    "value": {"text": text},
                    "status": "active",
                    "memory_type": "episode",
                    "title": text,
                    "canonical_text": text,
                    "summary": text,
                    "domain": "project",
                    "sensitivity": "internal",
                    "project_id": "alpha",
                    "metadata_json": {"session_date": session_date, "project_scope": ["alpha"]},
                }
            )
        service = VNextRollupService(store, embedding_provider=None)
        assert len(service.propose_rollups(**fence).proposals) == 1  # type: ignore[arg-type]
        card = _rollup_candidates(store)[0]
        store.conn.execute("UPDATE memories SET status = 'rejected' WHERE id = ?", (card["id"],))
        calls.clear()

        held = service.propose_rollups(**fence)  # type: ignore[arg-type]
        assert [group["state"] for group in held.groups] == ["existing_card_members_unchanged"]
        assert held.groups[0]["existing_memory_id"] == str(card["id"])
        assert calls == [{"rollup_key": held.groups[0]["rollup_key"], **fence}]


def test_a_closed_card_is_still_reported_as_expired_and_not_as_an_existing_card() -> None:
    """#518's state for a card whose window has closed is unchanged: ``expired_card_members_unchanged``.

    The expired-card read runs first, so a card with a closed window keeps the state and the ``expired_memory_id``
    key #518 gave it, and an open-window card gets the new state.

    Mutation: run the ``_row_at_digest_key`` block of ``propose_rollups`` before the ``expired_card`` block (the
    closed card is reported as ``existing_card_members_unchanged``).
    """

    with _memory_store() as store:
        service, card_id = _first_card(store)
        store.conn.execute("UPDATE memories SET status = 'active', valid_to = '2020-01-01T00:00:00Z' WHERE id = ?", (card_id,))
        outcome = service.propose_rollups()
        assert [group["state"] for group in outcome.groups] == ["expired_card_members_unchanged"]
        assert outcome.groups[0]["expired_memory_id"] == card_id
        assert "existing_memory_id" not in outcome.groups[0]

        store.conn.execute("UPDATE memories SET valid_to = ? WHERE id = ?", (FAR_FUTURE, card_id))
        reopened = service.propose_rollups()
        assert [group["state"] for group in reopened.groups] == ["already_covered_by_accepted"]


def test_every_control_of_the_digest_key_reads_is_a_required_keyword_argument() -> None:
    """``_row_at_digest_key`` and ``_may_name_card`` take the roll-up key, the domains, the sensitivity ceiling and the projects as required keywords.

    A caller cannot leave one out (and mypy rejects it), the way ``_expired_card_for_digest`` and the brief's
    store protocol already do.

    Mutation: give any of the four a default (``projects: tuple[str, ...] = ()``) in either function, or make one
    positional-or-keyword.
    """

    for function in (VNextRollupService._row_at_digest_key, _may_name_card):
        parameters = inspect.signature(function).parameters
        for name in ("rollup_key", "domains", "sensitivity_allowed", "projects"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (function.__name__, name)
            assert parameters[name].default is inspect.Parameter.empty, (function.__name__, name)
