"""The compact ``alice_memory_commit`` result (search-quality spec section 7, tests TC1 to TC8).

Unreleased (on main, not in v0.20.0). A saved one-sentence fact came back as about 3.7 KB:
the whole stored row and the policy decision three times. With ``ALICE_MCP_COMMIT_RESULT=compact``
the tool answers with the id, the outcome, the receipt, why the write was held, the
confirmation id and the proposed text, and the scope it was saved under. Until the release that
turns the compact result on, an unset variable means ``full``, the bytes v0.20.0 returned.

Every test below names the mutation that must fail it. Each test opts in to the mode it checks,
so none depends on the build default.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.unit.fixtures_commit_result_golden import COMMITTED_FACT, GOLDENS, HELD_FACT

USER_ID = "00000000-0000-0000-0000-000000000001"
REPO_ROOT = Path(__file__).resolve().parents[2]

# The memory fields the view keeps. Written out here, apart from the module's own tuple, so a
# change to the tuple shows up as a failing test and not as a silently different pin.
MEMORY_VIEW_FIELDS = {
    "canonical_text",
    "confidence",
    "created_at",
    "created_by_agent_id",
    "domain",
    "id",
    "memory_type",
    "project_id",
    "project_scope",
    "sensitivity",
    "status",
    "superseded_by",
    "supersedes",
    "title",
}

TRUSTED_AGENT = {
    "agent_id": "compact-test-agent",
    "agent_type": "coding_agent",
    "permission_profile": "trusted_local_agent",
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")


@pytest.fixture(autouse=True)
def _quiet_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No variable of the machine running the tests changes what a commit returns."""

    from alicebot_api.commit_result import COMMIT_RESULT_ENV
    from alicebot_api.mcp_tools import AGENT_API_KEY_ENV, LEGACY_SURFACES_ENV, MCP_LEGACY_TOOLS_ENV
    from alicebot_api.vnext_embeddings import (
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
    )

    for name in (
        COMMIT_RESULT_ENV,
        AGENT_API_KEY_ENV,
        LEGACY_SURFACES_ENV,
        MCP_LEGACY_TOOLS_ENV,
        EMBEDDINGS_API_KEY_ENV,
        EMBEDDINGS_BASE_URL_ENV,
        EMBEDDINGS_MODEL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _context(tmp_path: Path):
    from alicebot_api.mcp_tools import MCPRuntimeContext
    from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path

    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=USER_ID)


def _commit(context, *, name: str = "alice_memory_commit", **arguments) -> dict:
    from alicebot_api.mcp.registry import call_mcp_tool

    return call_mcp_tool(context, name=name, arguments=arguments)


def _wire(payload: Mapping[str, object]) -> str:
    """The text an MCP host hands the model, which is what the size limits count."""

    from alicebot_api.recall_framing import serialize_mcp_tool_result

    return serialize_mcp_tool_result(payload)


def _normalise(value: object) -> object:
    if isinstance(value, dict):
        return {key: _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, str):
        return _TIME.sub("<ts>", _UUID.sub("<id>", value))
    return value


def _leaves(value: object) -> Iterator[str]:
    """Every scalar of a result, as JSON text, wherever it sits."""

    if isinstance(value, Mapping):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item)
    else:
        yield json.dumps(value, sort_keys=True)


class _Spy:
    """Records each (full result, compact result) pair that ``call_mcp_tool`` produces."""

    def __init__(self) -> None:
        self.pairs: list[tuple[dict, dict]] = []


def _spy_on_compaction(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    import alicebot_api.mcp.registry as registry

    spy = _Spy()
    real = registry.compact_commit_result

    def recording(full):
        snapshot = json.loads(json.dumps(full))
        compact = real(full)
        spy.pairs.append((snapshot, json.loads(json.dumps(compact))))
        return compact

    monkeypatch.setattr(registry, "compact_commit_result", recording)
    return spy


def _outcomes(context) -> dict[str, dict]:
    """One result of each outcome the tool can return, through the tool name."""

    outcomes: dict[str, dict] = {}
    outcomes["committed"] = _commit(context, **COMMITTED_FACT)
    outcomes["committed_with_identity"] = _commit(
        context, title="Cache TTL", canonical_text="The cache TTL is sixty seconds.", **TRUSTED_AGENT
    )
    _commit(context, title="Replayed", canonical_text="A replayed fact.", idempotency_key="compact-replay")
    outcomes["replay"] = _commit(
        context, title="Replayed", canonical_text="A replayed fact.", idempotency_key="compact-replay"
    )
    outcomes["confirmation_required"] = _commit(context, **HELD_FACT)
    outcomes["review_required"] = _commit(
        context, title="From a page", canonical_text="A web page said the sky is green.", source_type="web_page"
    )
    outcomes["rejected"] = _commit(
        context,
        title="Too sensitive",
        canonical_text="A confidential note.",
        sensitivity="confidential",
        **TRUSTED_AGENT,
    )
    held = _commit(context, title="Held two", canonical_text="The team maybe deploys on Saturdays.", confidence=0.7)
    outcomes["finish_confirm"] = _commit(
        context, confirmation_id=held["confirmation_id"], confirmation_action="confirm"
    )
    held = _commit(context, title="Held three", canonical_text="The team maybe deploys on Sundays.", confidence=0.7)
    outcomes["finish_reject"] = _commit(
        context, confirmation_id=held["confirmation_id"], confirmation_action="reject"
    )
    return outcomes


# --- TC1: the switch -----------------------------------------------------------------------------


def test_the_switch_values_and_the_build_default() -> None:
    """``compact`` and ``full`` in any case with surrounding space are read; anything else is the default.

    Until the release that turns the compact result on, the default is ``full``, so main returns the
    bytes v0.20.0 returned. That release changes ``BUILD_DEFAULT_COMMIT_RESULT`` and this test with it.
    Mutation: treat an unrecognised, empty or non-string value as ``compact``; or make the build
    default ``compact``; or read the value without ``strip`` or ``lower``.
    """

    from alicebot_api.commit_result import (
        BUILD_DEFAULT_COMMIT_RESULT,
        COMMIT_RESULT_ENV,
        commit_result_mode,
        parse_commit_result_mode,
    )

    assert COMMIT_RESULT_ENV == "ALICE_MCP_COMMIT_RESULT"
    assert BUILD_DEFAULT_COMMIT_RESULT == "full"
    assert commit_result_mode({}) == "full"
    for value, expected in (
        ("compact", "compact"),
        (" Compact ", "compact"),
        ("COMPACT", "compact"),
        ("full", "full"),
        (" FULL\n", "full"),
    ):
        assert commit_result_mode({COMMIT_RESULT_ENV: value}) == expected, value
    for value in ("", " ", "bogus", "compacted", "1", "on", "true", "fulll"):
        assert commit_result_mode({COMMIT_RESULT_ENV: value}) == BUILD_DEFAULT_COMMIT_RESULT, value
    for value in (None, 1, True, b"compact", ["compact"]):
        assert parse_commit_result_mode(value) is None, value


@pytest.mark.parametrize("value", [None, "full", " FULL ", "", "bogus", "compacted", "on"])
def test_off_returns_the_v0200_result_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """Unset, ``full`` and an unrecognised value return the pinned v0.20.0 result for two outcomes.

    The pinned results are in ``fixtures_commit_result_golden``: normalised for ids and timestamps,
    produced by the unmodified code, compared as rendered text. A held write is the second outcome
    because its result has the extra ``confirmation`` and ``confirmation_id``.
    Mutation: ignore the switch and always compact; treat an unrecognised value as compact; or drop
    a key of the full result in the shared handler.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    if value is not None:
        monkeypatch.setenv(COMMIT_RESULT_ENV, value)
    context = _context(tmp_path)
    committed = _commit(context, **COMMITTED_FACT)
    held = _commit(context, **HELD_FACT)
    for label, result in (("committed", committed), ("confirmation_required", held)):
        rendered = json.dumps(_normalise(result), indent=1, sort_keys=True)
        pinned = json.dumps(GOLDENS[label], indent=1, sort_keys=True)
        assert rendered == pinned, (label, value)


@pytest.mark.parametrize("value", ["compact", " Compact "])
def test_compact_returns_the_compact_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """With ``compact`` set the tool answers with the view: no decision record, no row internals.

    Mutation: ignore the switch (the result stays full), or leave ``policy_decision`` or
    ``memory.metadata_json`` in the view.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    monkeypatch.setenv(COMMIT_RESULT_ENV, value)
    context = _context(tmp_path)
    result = _commit(context, **COMMITTED_FACT)
    assert result["status"] == "committed"
    assert "policy_decision" not in result
    assert set(result["memory"]) == MEMORY_VIEW_FIELDS
    assert "metadata_json" not in result["memory"]
    assert len(_wire(result)) < len(_wire(GOLDENS["committed"])) // 4


# --- TC2: nothing is added ------------------------------------------------------------------------


def test_the_compact_result_holds_no_value_the_full_result_did_not_carry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For every outcome, each scalar of the compact result is a scalar of the full result of the same call.

    The full result is read where ``call_mcp_tool`` hands it to the compaction, so the two are one
    call and not two calls that could differ. Counted, not only present: a value the full result
    held once cannot appear twice.
    Mutation: put into the view a value the full result did not carry, for example read the row
    again from the store, or compute a field (``memory["indexed"] = True``), or echo a value twice.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    monkeypatch.setenv(COMMIT_RESULT_ENV, "compact")
    spy = _spy_on_compaction(monkeypatch)
    outcomes = _outcomes(_context(tmp_path))
    assert len(spy.pairs) >= len(outcomes)
    for full, compact in spy.pairs:
        extra = Counter(_leaves(compact)) - Counter(_leaves(full))
        assert not extra, (compact.get("status"), dict(extra))
    assert {pair[1]["status"] for pair in spy.pairs} >= {
        "committed",
        "confirmation_required",
        "review_required",
        "rejected",
    }


def test_the_compaction_takes_the_full_result_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compaction has one parameter, and it does not open the database.

    It runs a result that names a memory no vault holds, with ``sqlite3.connect`` refusing, and
    again inside the tool call, so a compaction that reads the store fails here.
    Mutation: give ``compact_commit_result`` a context or store argument and read the row inside
    it, or open a connection in it.
    """

    import alicebot_api.mcp.registry as registry
    from alicebot_api.commit_result import COMMIT_RESULT_ENV, compact_commit_result

    assert list(inspect.signature(compact_commit_result).parameters) == ["full"]

    def refuse(*args: object, **kwargs: object):
        raise AssertionError("the compaction opened the database")

    planted = {
        "memory": {"id": "memory-that-no-vault-holds", "status": "active", "metadata_json": {"x": 1}},
        "policy_decision": {"reasons": ["r"], "requires_confirmation": False, "requires_dashboard_review": False},
        "receipt": "saved as a fact.",
        "status": "committed",
        "write_mode": "commit",
    }
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", refuse)
        view = compact_commit_result(planted)
    assert view["memory"] == {"id": "memory-that-no-vault-holds", "status": "active"}

    monkeypatch.setenv(COMMIT_RESULT_ENV, "compact")
    real = registry.compact_commit_result

    def guarded(full):
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", refuse)
            return real(full)

    monkeypatch.setattr(registry, "compact_commit_result", guarded)
    result = _commit(_context(tmp_path), **COMMITTED_FACT)
    assert result["status"] == "committed"


# --- TC3: what the flow needs ---------------------------------------------------------------------


def test_the_decision_keys_are_lifted_to_the_top_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reasons``, ``requires_confirmation`` and ``requires_dashboard_review`` sit at the top level.

    They are not top-level keys of the full result: they live inside ``policy_decision``. The value is
    the full result's own, so a held write says ``requires_confirmation: true`` and a write waiting in
    review says ``requires_dashboard_review: true``, and a rejected one keeps the three reasons the full
    result had.
    Mutation: drop one of the lifted keys (``requires_dashboard_review`` is the one the review write
    needs).
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    monkeypatch.setenv(COMMIT_RESULT_ENV, "compact")
    spy = _spy_on_compaction(monkeypatch)
    outcomes = _outcomes(_context(tmp_path))

    committed = outcomes["committed"]
    assert committed["reasons"] == []
    assert committed["requires_confirmation"] is False
    assert committed["requires_dashboard_review"] is False
    assert committed["reason"] == "explicit_trusted_memory_commit"

    held = outcomes["confirmation_required"]
    assert held["reasons"] == ["medium_confidence_requires_confirmation"]
    assert held["requires_confirmation"] is True
    assert held["requires_dashboard_review"] is False
    assert held["reason"] == "medium_confidence_requires_confirmation"

    review = outcomes["review_required"]
    assert review["status"] == "review_required"
    assert review["requires_dashboard_review"] is True
    assert review["requires_confirmation"] is False
    assert review["reasons"] == ["external_source_requires_review"]
    assert review["proposal_id"]

    rejected = outcomes["rejected"]
    assert rejected["status"] == "rejected"
    assert set(rejected["reasons"]) == {
        "restricted_sensitivity_filtered",
        "sensitive_memory_requires_confirmation",
        "sensitivity_above_agent_ceiling",
    }
    assert rejected["reason"] == "sensitivity_above_agent_ceiling"
    assert rejected["requires_confirmation"] is False
    assert rejected["requires_dashboard_review"] is False
    assert "memory" not in rejected
    full_rejected = next(full for full, compact in spy.pairs if compact.get("status") == "rejected")
    assert rejected["reasons"] == full_rejected["reasons"]

    for label, result in outcomes.items():
        assert "policy_decision" not in result, label
        assert result["receipt"], label


def test_a_lifted_value_never_replaces_a_key_the_full_result_already_had() -> None:
    """A key the full result holds at the top level is kept as it is; the decision record fills only gaps.

    A rejected result carries ``reason`` and ``reasons`` at the top level and in ``policy_decision``, and
    today the two agree. If a later change makes them differ, the top level is what the full result
    said first, so it stays. A key the decision record holds and the top level lacks is lifted.
    Mutation: let a lifted value overwrite the top-level key (drop the ``key not in view`` test).
    """

    from alicebot_api.commit_result import compact_commit_result

    view = compact_commit_result(
        {
            "status": "rejected",
            "reason": "top-reason",
            "reasons": ["top-one", "top-two"],
            "policy_decision": {
                "reason": "nested-reason",
                "reasons": ["nested-one"],
                "requires_confirmation": False,
                "requires_dashboard_review": True,
                "status": "nested-status",
                "trace_id": "nested-trace",
                "policy_decision": {"reasons": ["innermost"]},
            },
        }
    )
    assert view == {
        "status": "rejected",
        "reason": "top-reason",
        "reasons": ["top-one", "top-two"],
        "requires_confirmation": False,
        "requires_dashboard_review": True,
    }


def test_a_held_write_keeps_the_confirmation_id_and_the_proposed_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id to answer with, the text to show the user and the finished result all survive.

    The confirmation flow reads ``confirmation_id`` and shows ``confirmation.proposed_text``. Finishing
    it on the same tool returns the saved memory with the same id, and a reject says so.
    Mutation: drop ``confirmation_id`` or ``confirmation`` from the view (for example add them to the
    dropped top-level keys), or drop ``canonical_text`` from the memory view.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    monkeypatch.setenv(COMMIT_RESULT_ENV, "compact")
    outcomes = _outcomes(_context(tmp_path))

    held = outcomes["confirmation_required"]
    assert held["status"] == "confirmation_required"
    assert held["write_mode"] == "confirm_inline"
    assert held["confirmation_id"].startswith("confirm-")
    assert held["confirmation"]["confirmation_id"] == held["confirmation_id"]
    assert held["confirmation"]["proposed_text"] == HELD_FACT["canonical_text"]
    assert held["confirmation"]["status"] == "pending"
    assert held["confirmation"]["expires_at"]
    assert held["memory"]["status"] == "needs_review"
    assert held["memory"]["canonical_text"] == HELD_FACT["canonical_text"]
    assert held["receipt"] == "needs confirmation."

    confirmed = outcomes["finish_confirm"]
    assert confirmed["status"] == "committed"
    assert confirmed["confirmation_id"].startswith("confirm-")
    assert confirmed["memory"]["status"] == "active"
    assert set(confirmed["memory"]) == MEMORY_VIEW_FIELDS

    rejected = outcomes["finish_reject"]
    assert rejected["status"] == "rejected"
    assert rejected["memory"]["status"] == "rejected"
    assert rejected["receipt"] == "rejected."
    assert "text_withheld" in rejected

    assert outcomes["replay"]["idempotent_replay"] is True


def test_the_memory_view_keeps_the_scope_a_note_was_saved_under(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``project_scope`` and ``project_id`` come back from a scoped save; ``supersedes`` and ``superseded_by`` are kept.

    The scope is the agent's only receipt of which project a note landed in, and per-project
    slices S3 to S5 report on all four. The first two are read from a real scoped save. The last two
    are never set by a plain save, so they are read from a planted full result, with a value.
    Mutation: drop ``project_scope``, ``project_id``, ``supersedes`` or ``superseded_by`` from the view.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV, compact_commit_result

    monkeypatch.setenv(COMMIT_RESULT_ENV, "compact")
    scoped = _commit(
        _context(tmp_path),
        title="Scoped fact",
        canonical_text="The scoped fact belongs to the alpha project.",
        project_scope=["alpha"],
    )
    assert scoped["memory"]["project_scope"] == ["alpha"]
    assert scoped["memory"]["project_id"] == "alpha"

    view = compact_commit_result(
        {
            "memory": {
                "id": "m-new",
                "status": "active",
                "supersedes": "m-old",
                "superseded_by": "m-newer",
                "project_scope": ["alpha", "beta"],
                "project_id": None,
                "metadata_json": {"dropped": True},
            },
            "receipt": "saved as a fact.",
            "status": "committed",
        }
    )
    assert view["memory"] == {
        "id": "m-new",
        "status": "active",
        "supersedes": "m-old",
        "superseded_by": "m-newer",
        "project_scope": ["alpha", "beta"],
        "project_id": None,
    }


def test_a_memory_field_the_full_result_lacks_is_not_added() -> None:
    """The view selects; it never fills in. A missing field stays missing, a null stays null.

    Mutation: build the view with ``memory.get(key)`` for every listed field (which adds the missing
    ones as null), or default a missing field to a value.
    """

    from alicebot_api.commit_result import compact_commit_result

    view = compact_commit_result({"memory": {"id": "m1", "supersedes": None}, "status": "committed"})
    assert view["memory"] == {"id": "m1", "supersedes": None}
    assert compact_commit_result({"status": "rejected", "reason": "r"}) == {"status": "rejected", "reason": "r"}
    assert compact_commit_result({"memory": None, "status": "x"}) == {"memory": None, "status": "x"}
    only_nested = compact_commit_result({"policy_decision": {"reasons": ["a"], "trace_id": "t", "status": "s"}})
    assert only_nested == {"reasons": ["a"]}


# --- TC4: the top level is a denylist --------------------------------------------------------------


def test_a_top_level_key_the_view_does_not_know_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key a later slice adds to the full result reaches the agent with no edit to the view.

    Per-project S4 adds ``saved_to``, ``will_save_to`` and ``scope_requested``. They are planted
    here at the top level of the handler's result, and the tool answers with all three.
    Mutation: make the top level an allowlist of the known keys.
    """

    import alicebot_api.mcp.registry as registry
    from alicebot_api.commit_result import COMMIT_RESULT_ENV, compact_commit_result

    planted = {"saved_to": "alpha", "will_save_to": ["alpha"], "scope_requested": None}
    view = compact_commit_result({"status": "committed", "write_mode": "commit", **planted})
    assert {key: view[key] for key in planted} == planted

    monkeypatch.setenv(COMMIT_RESULT_ENV, "compact")
    real_handler = registry._TOOL_HANDLERS["alice_memory_commit"]

    def handler(context, arguments):
        return {**real_handler(context, arguments), **planted}

    monkeypatch.setitem(registry._TOOL_HANDLERS, "alice_memory_commit", handler)
    result = _commit(_context(tmp_path), **COMMITTED_FACT)
    assert {key: result[key] for key in planted} == planted
    assert "policy_decision" not in result


# --- TC5: sizes -------------------------------------------------------------------------------------

_TWO_THOUSAND = ("lorem ipsum dolor sit amet " * 80)[:2000]

#: The four rows of the sizes table in ``docs/alpha/mcp-tools.md``, keyed as ``_measured_sizes`` returns them.
SIZE_LABELS = {
    "fact": "one-sentence fact, no identity",
    "fact_identity": "one-sentence fact, declared identity",
    "held": "held for confirmation",
    "long": "2,000-character memory",
}


def _wire_size(payload: Mapping[str, object]) -> int:
    """Bytes of the text a host hands the model, with every timestamp counted at six fractional digits.

    ``datetime.isoformat`` prints no fraction when the microsecond is zero, which is 7 bytes less per
    timestamp once in a million times. Counting that case as the usual one keeps a documented size exact.
    """

    text = _TIME.sub("2026-01-01T00:00:00.000000Z", _wire(payload))
    return len(text.encode("utf-8"))


def _measured_sizes(monkeypatch: pytest.MonkeyPatch, mode: str, base: Path) -> dict[str, int]:
    """The four documented writes on a fresh SQLite vault, as wire bytes in one mode.

    The fixtures are the short fact and the held write of ``fixtures_commit_result_golden.py``, the same
    fact with a declared identity, and a 2,000-character memory. The order of the writes is part of the
    measurement, and the docs quote what this function returns.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    monkeypatch.setenv(COMMIT_RESULT_ENV, mode)
    context = _context(base)
    results = {
        "fact": _commit(context, **COMMITTED_FACT),
        "fact_identity": _commit(
            context, title="Cache TTL", canonical_text="The cache TTL is sixty seconds.", **TRUSTED_AGENT
        ),
        "held": _commit(context, **HELD_FACT),
        "long": _commit(context, title="Long note", canonical_text=_TWO_THOUSAND),
    }
    if mode == "compact":
        assert _wire(results["long"]).count(_TWO_THOUSAND) == 1
        assert len(_TWO_THOUSAND) == 2000
    return {label: _wire_size(result) for label, result in results.items()}


def test_the_compact_result_is_a_fraction_of_the_full_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire sizes, in bytes, of the text an MCP host hands the model.

    A one-sentence fact is at most 650 bytes without an identity and with one, a held write at most
    1.2 KB, and a 2,000-character memory at most 2.7 KB with the text once. The full result of the same
    calls is checked too, so a test that passes because the full result also shrank fails. Measured
    on a fresh SQLite vault: 3,696, 4,186, 4,633 and 7,817 bytes full; 595, 583, 1,066 and 2,536 compact
    (``test_every_documented_size_is_what_the_fixtures_produce`` pins those exact numbers).
    Mutation: echo the text three times (add ``summary`` and ``value.text`` to the view), or keep
    ``policy_decision`` or ``metadata_json``.
    """

    full = _measured_sizes(monkeypatch, "full", tmp_path / "full")
    compact = _measured_sizes(monkeypatch, "compact", tmp_path / "compact")
    assert full["fact"] > 3_500 and full["fact_identity"] > 3_900 and full["held"] > 4_400 and full["long"] > 7_500
    assert compact["fact"] <= 650
    assert compact["fact_identity"] <= 650
    assert compact["held"] <= 1_200
    assert compact["long"] <= 2_700
    assert compact["fact"] < full["fact"] // 5
    assert compact["long"] < full["long"] // 2


def _flat(text: str) -> str:
    return " ".join(text.split())


def _doc(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _changelog_bullet() -> str:
    """The one Unreleased bullet about the compact commit result, on one line."""

    changelog = _doc("CHANGELOG.md")
    section = changelog[changelog.index("## Unreleased") : changelog.index("\n## v0.20.0")]
    bullets = [item for item in section.split("\n- ")[1:] if item.startswith("`alice_memory_commit` can answer")]
    assert len(bullets) == 1
    return _flat(bullets[0])


def _number(text: str) -> int:
    return int(text.replace(",", ""))


def test_every_documented_size_is_what_the_fixtures_produce(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sizes table of ``docs/alpha/mcp-tools.md``, the changelog bullet and the rounded sizes in
    prose are the bytes the test fixtures produce today, and the ratio words are true for every row.

    Two of the four numbers of the first draft (4,134 and 570, then 7,810 and 2,530) did not reproduce.
    The ratio claims are ``under a sixth`` (both one-sentence facts), ``under a quarter`` (the held write)
    and ``under a third`` (the 2,000-character memory, which carries its text once).
    Mutation, each one alone: change one number of the docs table, or of the changelog bullet, by one
    byte; change the rounded ``about 3.7 KB`` or ``about 0.6 KB`` in ``first-memory.md``; write ``about a
    sixth of the bytes`` back into the docs; add ``summary`` to the memory view (every compact number then
    moves); change ``_TWO_THOUSAND``.
    """

    full = _measured_sizes(monkeypatch, "full", tmp_path / "full")
    compact = _measured_sizes(monkeypatch, "compact", tmp_path / "compact")
    assert set(full) == set(compact) == set(SIZE_LABELS)

    guide = _doc("docs/alpha/mcp-tools.md")
    rows = {
        label: (_number(full_bytes), _number(compact_bytes))
        for label, full_bytes, compact_bytes in re.findall(
            r"^\| ([^|]+?) \| ([\d,]+) bytes \| ([\d,]+) bytes \|$", guide, re.MULTILINE
        )
    }
    assert rows == {SIZE_LABELS[key]: (full[key], compact[key]) for key in SIZE_LABELS}

    bullet = _changelog_bullet()
    pairs = [(_number(compact_bytes), _number(full_bytes)) for compact_bytes, full_bytes in re.findall(r"([\d,]+) against ([\d,]+)", bullet)]
    assert pairs == [(compact[key], full[key]) for key in SIZE_LABELS]

    def kilobytes(size: int) -> str:
        return f"about {size / 1000:.1f} KB"

    first_memory = _flat(_doc("docs/alpha/first-memory.md"))
    for text in (first_memory, _flat(guide), bullet):
        assert kilobytes(full["fact"]) in text
    assert kilobytes(compact["fact"]) in first_memory

    assert compact["fact"] * 6 < full["fact"]
    assert compact["fact_identity"] * 6 < full["fact_identity"]
    assert compact["held"] * 4 < full["held"]
    assert compact["long"] * 3 < full["long"]
    for text in (_flat(guide), bullet):
        for words in ("under a sixth", "under a quarter", "under a third"):
            assert words in text, words
        assert "about a sixth" not in text


# --- TC6: the legacy alias -------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["compact", "full", None])
def test_the_legacy_alias_keeps_the_full_result_in_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None
) -> None:
    """``alice_vnext_commit_memory`` shares the handler and is not on the compacted name.

    It returns the pinned full result with the switch at ``compact``, at ``full`` and unset, and it is
    the same handler object as the core tool, so the handler map and the alias pin do not move.
    Mutation: compact inside the shared handler (the alias then compacts too), or add the alias to
    the compacted names.
    """

    import alicebot_api.mcp.registry as registry
    from alicebot_api.commit_result import COMMIT_RESULT_ENV, COMMIT_RESULT_TOOL
    from alicebot_api.mcp.memories import _handle_alice_vnext_commit_memory
    from alicebot_api.mcp_tools import MCP_LEGACY_TOOLS_ENV

    assert COMMIT_RESULT_TOOL == "alice_memory_commit"
    assert registry._TOOL_HANDLERS["alice_vnext_commit_memory"] is _handle_alice_vnext_commit_memory
    assert registry._TOOL_HANDLERS["alice_memory_commit"] is _handle_alice_vnext_commit_memory
    monkeypatch.setenv(MCP_LEGACY_TOOLS_ENV, "1")
    if mode is not None:
        monkeypatch.setenv(COMMIT_RESULT_ENV, mode)
    result = _commit(_context(tmp_path), name="alice_vnext_commit_memory", **COMMITTED_FACT)
    rendered = json.dumps(_normalise(result), indent=1, sort_keys=True)
    assert rendered == json.dumps(GOLDENS["committed"], indent=1, sort_keys=True)


# --- TC7: the receipt and the examples ---------------------------------------------------------------


def test_the_receipt_is_the_same_text_in_both_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every outcome's ``receipt`` is identical in ``full`` and ``compact``, and none is empty.

    The skill packs tell an agent to print it, and the held write's receipt is what tells the agent to ask.
    Mutation: drop ``receipt`` from the compact result, or shorten it.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    receipts: dict[str, dict[str, str]] = {}
    for mode in ("full", "compact"):
        monkeypatch.setenv(COMMIT_RESULT_ENV, mode)
        outcomes = _outcomes(_context(tmp_path / mode))
        receipts[mode] = {label: str(result.get("receipt")) for label, result in outcomes.items()}
    assert receipts["full"] == receipts["compact"]
    assert all(receipt and receipt != "None" for receipt in receipts["compact"].values())
    assert receipts["compact"]["confirmation_required"] == "needs confirmation."


def _stdio_env(extra: Mapping[str, str]) -> dict[str, str]:
    """The environment a host gives the server: nothing of Alice's, the repo's code, and the entry's variables."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    env.update(extra)
    return env


def _stdio_commit(tmp_path: Path, extra: Mapping[str, str]) -> dict:
    """Start the real server, commit one fact over its stdio, and return the parsed tool result."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "alice_memory_commit",
                "arguments": {"title": "Stdio fact", "canonical_text": "A short fact committed over stdio."},
            },
        },
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "alicebot_api.onramp", "mcp", "--data-dir", str(tmp_path / "vault")],
        input="\n".join(json.dumps(item) for item in requests) + "\n",
        capture_output=True,
        text=True,
        env=_stdio_env(extra),
        cwd=tmp_path,
        timeout=120,
        check=True,
    )
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    reply = next(item for item in replies if item.get("id") == 2)
    assert not reply["result"].get("isError"), reply
    parsed = json.loads(reply["result"]["content"][0]["text"])
    assert isinstance(parsed, dict)
    return parsed


def test_the_variable_in_a_server_environment_reaches_the_real_server(tmp_path: Path) -> None:
    """A real ``alice-memory mcp`` process reads the variable from the environment it was started with.

    That is the environment a host builds from the entry's ``env`` map, so this is the server's half
    of the host question: given the variable, the server answers compact. The other half, whether a
    host passes the entry's map on, is the host's and is in docs/alpha/mcp-tools.md. Unset gives the
    full result, and ``full`` gives the same.
    Mutation: ignore ``os.environ`` in ``call_mcp_tool`` (pass an empty mapping), or read the variable
    at import time from a different place.
    """

    compact = _stdio_commit(tmp_path / "a", {"ALICE_MCP_COMMIT_RESULT": "compact"})
    assert "policy_decision" not in compact
    assert set(compact["memory"]) == MEMORY_VIEW_FIELDS
    unset = _stdio_commit(tmp_path / "b", {})
    assert "policy_decision" in unset and "metadata_json" in unset["memory"]
    full = _stdio_commit(tmp_path / "c", {"ALICE_MCP_COMMIT_RESULT": "full"})
    assert "policy_decision" in full and "metadata_json" in full["memory"]


def test_the_mcp_quickstart_example_runs_against_the_compact_result(tmp_path: Path) -> None:
    """``docs/examples/mcp_quickstart.py`` commits and recalls over stdio with the compact result.

    It reads ``memory.canonical_text``, ``memory.id`` and ``memory.status`` from the commit result and
    prints them. It starts its own server and passes its environment on, so the variable reaches it.
    Mutation: drop ``canonical_text``, ``id`` or ``status`` from the memory view (the example then stops
    with "commit did not store the sentence" or prints ``None``).
    """

    script = REPO_ROOT / "docs" / "examples" / "mcp_quickstart.py"
    env = _stdio_env({"ALICE_MCP_COMMIT_RESULT": "compact"})
    completed = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180, check=True
    )
    assert "MCP QUICKSTART OK" in completed.stdout
    match = re.search(r"commit: memory_id=(\S+) status=(\S+)", completed.stdout)
    assert match is not None, completed.stdout
    assert match.group(1) != "None" and match.group(2) == "active"


def test_the_release_smoke_script_reads_only_keys_the_compact_result_keeps(
    tmp_path: Path,
) -> None:
    """``scripts/test_distribution_artifact.py`` reads ``status`` and ``memory.id`` from its commit result.

    The reads are taken from the script's own text, so a new read of a dropped field fails here, and
    each is followed in the compact result of a real server committing with the script's arguments.
    Mutation: drop ``id`` from the memory view, or add a read of ``policy_decision`` or
    ``metadata_json`` to the script.
    """

    source = (REPO_ROOT / "scripts" / "test_distribution_artifact.py").read_text(encoding="utf-8")
    reads = re.findall(r'commit_payload((?:\["[a-z_]+"\])+)', source)
    paths = sorted({tuple(re.findall(r'\["([a-z_]+)"\]', read)) for read in reads})
    assert ("status",) in paths and ("memory", "id") in paths, paths
    compact = _stdio_commit(tmp_path, {"ALICE_MCP_COMMIT_RESULT": "compact"})
    for path in paths:
        node: object = compact
        for key in path:
            assert isinstance(node, dict) and key in node, (path, sorted(compact))
            node = node[key]
        assert node not in (None, ""), path


# --- TC8: the default-surface confirmation flow --------------------------------------------------------


def test_the_default_surface_confirmation_cases_pass_unchanged_against_the_compact_result() -> None:
    """The 25 cases of ``test_default_surface_can_finish_confirmation_required.py`` pass with ``compact`` set.

    The file is not edited and not copied: it runs as it is, in its own process, with the variable
    set, so it reads the compact result the way an agent on the default three tools would. It walks the
    whole flow: a held write, the confirmation id, the proposed text, confirm, reject and the refusals.
    Mutation: drop ``confirmation_id`` (or ``confirmation``) from the compact result.
    """

    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE_")}
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    env["ALICE_MCP_COMMIT_RESULT"] = "compact"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/test_default_surface_can_finish_confirmation_required.py",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    tail = completed.stdout[-1500:]
    assert completed.returncode == 0, tail
    match = re.search(r"(\d+) passed", completed.stdout)
    assert match is not None and int(match.group(1)) >= 25, tail
    assert "failed" not in completed.stdout and "error" not in completed.stdout.lower(), tail


# --- install --------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("uvx_on_path")
def test_install_never_writes_the_variable_into_a_host_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A fresh install of every host writes no ``ALICE_MCP_COMMIT_RESULT`` anywhere, so the build default decides.

    Install writes an ``env`` map only for Hermes, and only with the data dir. If it wrote the
    variable, the result a user gets would change with an install and not with a release.
    Mutation: write ``ALICE_MCP_COMMIT_RESULT`` into the env map ``mcp_server_payload`` builds, or into
    any host's entry.
    """

    from alicebot_api.onramp import main as onramp_main

    hosts = ("claude-desktop", "claude-code", "cursor", "openclaw", "hermes", "opencode", "codex")
    home = tmp_path / "home"
    argv = ["install", "--home", str(home), "--data-dir", str(tmp_path / "vault")]
    for host in hosts:
        argv += ["--host", host]
    assert onramp_main(argv) == 0, capsys.readouterr()
    written = [path for path in home.rglob("*") if path.is_file()]
    assert len(written) >= 8, written
    for path in written:
        assert "ALICE_MCP_COMMIT_RESULT" not in path.read_text(encoding="utf-8"), path
    assert "ALICE_MCP_COMMIT_RESULT" not in capsys.readouterr().out


# --- TC9: what the docs say about the variable ---------------------------------------------------------


def test_the_mcp_guide_lists_the_variable_with_its_values_and_the_marker() -> None:
    """The Runtime Scope list of ``docs/integrations/mcp.md`` names the variable, both values and the marker.

    Mutation, each one alone: delete the bullet; delete ``Unreleased (on main, not in v0.20.0):`` from it;
    delete the word ``full`` or the word ``compact`` from it; move it out of the Runtime Scope section.
    """

    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    guide = _doc("docs/integrations/mcp.md")
    scope = guide[guide.index("## Runtime Scope") : guide.index("## Default Tool Surface")]
    items = [_flat(item) for item in scope.split("\n- ")[1:] if item.startswith(f"`{COMMIT_RESULT_ENV}`")]
    assert len(items) == 1
    item = items[0]
    assert "Unreleased (on main, not in v0.20.0):" in item
    assert "`compact`" in item and "`full`" in item
    assert "mcp-tools.md#the-commit-result" in item
    assert "cannot set it" in item


@pytest.mark.parametrize("build_default", ["full", "compact"])
def test_the_one_call_check_tells_arrival_from_the_default_under_either_build_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_default: str
) -> None:
    """The check in the docs (save one fact with ``compact``, one with ``full``, compare the shapes) works
    before and after the release that turns the compact result on.

    A server that received the variable gives a result without ``policy_decision`` for ``compact`` and one
    with it for ``full``. A server that did not gives the same shape twice, whichever default the build has.
    The first draft said a result that still carries ``policy_decision`` means the variable did not arrive,
    which is false once the default is ``compact``.
    Mutation, each one alone: make the server ignore the variable (read the build default whatever is set);
    put the first draft's sentence back into the docs.
    """

    import alicebot_api.commit_result as commit_result
    from alicebot_api.commit_result import COMMIT_RESULT_ENV

    monkeypatch.setattr(commit_result, "BUILD_DEFAULT_COMMIT_RESULT", build_default)
    serial = iter(range(100))

    def shape(value: str | None) -> bool:
        """Whether the result carries ``policy_decision``, with the variable set to ``value`` or unset."""

        if value is None:
            monkeypatch.delenv(COMMIT_RESULT_ENV, raising=False)
        else:
            monkeypatch.setenv(COMMIT_RESULT_ENV, value)
        return "policy_decision" in _commit(_context(tmp_path / str(next(serial))), **COMMITTED_FACT)

    arrived = (shape("compact"), shape("full"))
    missing = (shape(None), shape(None))
    assert arrived == (False, True)
    assert missing[0] == missing[1] == (build_default == "full")

    check = _flat(_doc("docs/alpha/mcp-tools.md"))
    assert "set it to `compact` and save one fact, then set it to `full` and save another" in check
    assert "A server that did not gives the same shape both times" in check
    assert "means the variable did not reach the server" not in check
    assert "a result that still carries `policy_decision`" not in check


def test_the_docs_name_the_build_default_the_code_has() -> None:
    """The one sentence that says an unset variable still returns the v0.20.0 result is there exactly while
    ``BUILD_DEFAULT_COMMIT_RESULT`` is ``full``.

    The release that turns the compact result on changes the constant, this test, and that sentence.
    Mutation: set ``BUILD_DEFAULT_COMMIT_RESULT`` to ``compact`` without editing the docs, or delete the
    sentence from ``docs/alpha/mcp-tools.md`` while the constant is ``full``.
    """

    from alicebot_api.commit_result import BUILD_DEFAULT_COMMIT_RESULT

    guide = _flat(_doc("docs/alpha/mcp-tools.md"))
    says_full = "On main that is still what an unset variable returns, until the release that turns the compact result on." in guide
    assert says_full == (BUILD_DEFAULT_COMMIT_RESULT == "full")
    bullet = _changelog_bullet()
    assert ("an unset variable and `ALICE_MCP_COMMIT_RESULT=full` return the v0.20.0 result byte for byte" in bullet) == (
        BUILD_DEFAULT_COMMIT_RESULT == "full"
    )


def test_the_host_table_claims_only_what_this_repository_checked() -> None:
    """Every host row says the host's forwarding of the entry's map is not verified, the plugin row says it has
    no setting, no row or paragraph leans on a CI artifact, and the Codex sentence names ``env_vars``.

    The Claude Code plugin's server entry is read from the repository, so the sentence that says a plugin
    user cannot set the variable stays true or fails.
    Mutation, each one alone: put ``host-evidence`` or ``entry_env_forwarded`` back into the section; write
    ``yes`` in the last cell of a row; delete the Codex ``env_vars`` sentence from ``mcp-tools.md`` or from
    ``codex.md``; add an ``env`` map to ``plugins/alice-memory/.mcp.json``; add a second setting to the
    plugin manifest.
    """

    guide = _doc("docs/alpha/mcp-tools.md")
    section = guide[guide.index("Where to set the variable.") : guide.index("Use canonical schema values")]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| ") and not line.startswith(("| Host", "| ---"))
    ]
    assert [row[0] for row in rows] == [
        "Claude Desktop, Claude Code, Cursor, OpenClaw",
        "Hermes",
        "Codex",
        "OpenCode",
        "Claude Code plugin",
    ]
    assert all(len(row) == 4 for row in rows)
    assert [row[3] for row in rows[:4]] == ["not verified"] * 4
    plugin = rows[4]
    assert plugin[3] == "no setting"
    assert "has no `env` map" in plugin[1] and "a plugin user cannot set the variable" in plugin[1]

    prose = _flat(section)
    for cited in ("host-evidence", "real-host-ci", "entry_env_forwarded", "launch_env_forwarded", "artifact"):
        assert cited not in prose, cited
    assert "Not verified for any host, Claude Code and Codex included" in prose
    assert 'env_vars = ["ALICE_MCP_COMMIT_RESULT"]' in prose
    assert "a variable exported in the shell that starts Codex does not reach the server by itself" in prose
    codex = _flat(_doc("docs/integrations/codex.md"))
    assert "or name it in the same table's `env_vars` to forward the shell's value" in codex

    root = REPO_ROOT / "plugins" / "alice-memory"
    entry = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["alice"]
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "env" not in entry
    assert set(manifest["userConfig"]) == {"data_dir"}
