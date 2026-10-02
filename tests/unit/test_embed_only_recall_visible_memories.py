"""The embeddings endpoint is sent the text only of a memory that recall can return.

Reindex and the backfill already send only active and accepted memories. This
file covers the other doors a memory's text takes to the endpoint, and expiry:

* Embed-on-write. A write that waits for confirmation (``needs_review``), a
  proposal that waits for review (``candidate``) and a capture's candidate
  memories are not embedded when they are created. Each is embedded once, when
  it becomes active: confirm, approve, edit and approve, supersede with a
  replacement, correct, accept a consolidation. A rejected one is never
  embedded.
* Expiry. A memory whose ``valid_to`` has passed is never returned by vector
  recall (``_expiry_clause`` on SQLite, ``valid_to >= clock_timestamp()`` on
  Postgres), so reindex, the backfill and the doctor count leave it out with
  the same test.

Every test that talks to an embeddings endpoint talks to a fake one on
127.0.0.1, started by the helper of ``test_reindex_live_memories_only``, that
records every text it receives. Nothing here reaches the network or a paid API,
and no Postgres is needed: the Postgres statement is checked as text.

Each test names, in its docstring, the change to the code that must fail it.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import alicebot_api.cli as cli_module
from alicebot_api.config import Settings
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp_tools import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_schema import MEMORY_STATUSES, bootstrap_sqlite_schema
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_embeddings import (
    DeferredMemoryEmbedding,
    attach_memory_embedding,
    embeddable_deferred_inputs,
    get_embedding_provider,
    persist_deferred_memory_embeddings_outcome,
    prepare_memory_embeddings,
)
from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService
from alicebot_api.vnext_memory_version import memory_version_snapshot
from alicebot_api.vnext_recall_visibility import (
    MEMORY_SEARCHABLE_STATUSES,
    POSTGRES_UNEXPIRED_SQL,
    memory_is_recall_visible,
    parse_valid_to,
    valid_to_has_passed,
)
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_stores.postgres import memory_access as postgres_memory_access
from alicebot_api.vnext_stores.sqlite import embedding_cas as sqlite_embedding_cas
from alicebot_api.vnext_stores.sqlite import memory_access as sqlite_memory_access
from tests.unit.test_reindex_live_memories_only import (
    USER_ID,
    _configure,
    _doctor_line,
    _marker,
    _RecordingEmbeddingsServer,
    _reindex,
    _StatusAwareStore,
    _vectors_present,
)

PAST = "2020-01-01T00:00:00Z"
FAR_FUTURE = "2999-01-01T00:00:00Z"


def _sent(server: _RecordingEmbeddingsServer, label: str) -> int:
    """How many texts that carry the marker of ``label`` the endpoint received."""
    return sum(1 for text in server.texts if _marker(label) in text)


def _row(label: str, *, status: str | None, valid_to: object = None, memory_id: str | None = None) -> dict[str, object]:
    return {
        "id": memory_id or f"mem-{label}",
        "title": f"Title {label}",
        "canonical_text": _marker(label),
        "summary": None,
        "status": status,
        "valid_to": valid_to,
    }


# ---------------------------------------------------------------------------
# The predicate and the door
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "valid_to", "visible"),
    [
        ("active", None, True),
        ("accepted", None, True),
        ("active", FAR_FUTURE, True),
        ("active", "9999-12-31T23:59:59Z", True),
        ("active", datetime(2999, 1, 1, tzinfo=UTC), True),
        ("active", PAST, False),
        ("accepted", PAST, False),
        ("active", datetime(2020, 1, 1, tzinfo=UTC), False),
        ("active", "2020-01-01T00:00:00+00:00", False),
        ("active", "2020-01-01T00:00:00", False),
        ("active", "", True),
        ("active", "not a date", True),
        ("candidate", None, False),
        ("needs_review", None, False),
        ("private_only", None, False),
        ("superseded", None, False),
        ("rejected", None, False),
        ("archived", None, False),
        ("stale", None, False),
        ("", None, False),
        (None, None, False),
        ("ACTIVE", None, False),
    ],
)
def test_the_visibility_predicate_is_status_and_an_open_validity_window(
    status: object, valid_to: object, visible: bool
) -> None:
    """A row is recall-visible only in a searchable status with a validity window that has not closed.

    A missing status is not visible, and a ``valid_to`` that is not a date is read
    as no validity end (recall's SQL cannot order it either).

    Mutations: let a missing status pass (change the status test in
    ``memory_is_recall_visible`` so ``None`` falls through: the ``None`` row turns
    visible); flip the comparison in ``valid_to_has_passed`` (the past and far
    future rows swap); add ``candidate`` to ``MEMORY_SEARCHABLE_STATUSES`` (the
    ``candidate`` row turns visible).
    """

    row = {"status": status, "valid_to": valid_to}
    assert memory_is_recall_visible(row) is visible
    # a row with no valid_to key at all is the same as a null one
    if valid_to is None:
        assert memory_is_recall_visible({"status": status}) is visible


def test_valid_to_helpers_agree_on_the_forms_the_stores_write() -> None:
    """``parse_valid_to`` reads the three forms a store returns and nothing else.

    Mutations: stop reading a naive value as UTC in ``parse_valid_to`` (the first
    assertion fails: a naive time is not equal to an aware one); compare with
    ``<=`` in ``valid_to_has_passed`` (a window that ends exactly now reads as
    closed, and the ``now`` assertion fails).
    """

    assert parse_valid_to("2020-01-01T00:00:00") == datetime(2020, 1, 1, tzinfo=UTC)
    assert parse_valid_to("2020-01-01T00:00:00Z") == datetime(2020, 1, 1, tzinfo=UTC)
    assert parse_valid_to(datetime(2020, 1, 1, 2, tzinfo=UTC) - timedelta(hours=2)) == datetime(2020, 1, 1, tzinfo=UTC)
    assert parse_valid_to(None) is None and parse_valid_to("") is None and parse_valid_to(5) is None
    now = datetime(2026, 10, 1, tzinfo=UTC)
    assert valid_to_has_passed("2026-09-30T23:59:59Z", now=now) is True
    assert valid_to_has_passed("2026-10-01T00:00:00Z", now=now) is False  # a window that ends now is not closed yet
    assert valid_to_has_passed(None, now=now) is False


def test_a_deferred_snapshot_cannot_be_built_without_saying_its_status() -> None:
    """``status`` and ``valid_to`` have no default, so a caller cannot forget them.

    The door sends text only for a recall-visible snapshot. A constructor that
    could leave the status out would be a way to build a snapshot the door has no
    status for. ``from_memory`` reads both from the row, and turns a datetime
    ``valid_to`` (Postgres returns one) into text.

    Mutations: give ``status`` or ``valid_to`` a default of ``None`` or ``"active"``
    in the dataclass (the ``TypeError`` check fails); stop ``from_memory`` copying
    ``status`` (the snapshot of an active row is then withheld by the door, which
    the next test shows end to end).
    """

    with pytest.raises(TypeError):
        DeferredMemoryEmbedding(memory_id="x", title="t", canonical_text="c", summary=None)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        DeferredMemoryEmbedding(  # type: ignore[call-arg]
            memory_id="x", title="t", canonical_text="c", summary=None, status="active"
        )
    snapshot = DeferredMemoryEmbedding.from_memory(
        {"id": "x", "title": "t", "canonical_text": "c", "status": "accepted", "valid_to": datetime(2999, 1, 1, tzinfo=UTC)}
    )
    assert (snapshot.status, snapshot.valid_to) == ("accepted", "2999-01-01T00:00:00+00:00")
    assert snapshot.is_embeddable() is True
    assert DeferredMemoryEmbedding.from_memory({"id": "x"}).is_embeddable() is False


def test_the_door_sends_only_the_text_of_recall_visible_memories(monkeypatch) -> None:
    """``prepare_memory_embeddings`` is the one door: a memory recall cannot return is withheld.

    One row per status, an active row whose ``valid_to`` has passed, an active row
    whose window is still open, and a row with no status. Only the active and the
    accepted rows and the open-window row reach the endpoint, the rest are named
    in ``withheld`` (they are not failures), and a caller that offers every row
    gets the same result as one that offers only the right ones.

    Mutations: make ``DeferredMemoryEmbedding.is_embeddable`` return True (every
    text is sent); drop the ``item.is_embeddable(now=now)`` condition from the
    ``embeddable`` list in ``prepare_memory_embeddings`` (the same); compute
    ``withheld`` from a different test than the one that filters (the ``withheld``
    assertion fails); read ``valid_to`` as always open (the expired row is sent).
    """

    rows = [_row(status, status=status) for status in MEMORY_STATUSES]
    rows += [
        _row("expired", status="active", valid_to=PAST),
        _row("open-window", status="active", valid_to=FAR_FUTURE),
        _row("no-status", status=None),
    ]
    labels = (*MEMORY_STATUSES, "expired", "open-window", "no-status")
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        preparation = prepare_memory_embeddings(tuple(DeferredMemoryEmbedding.from_memory(row) for row in rows))

    assert server.labels_received(labels) == ["active", "accepted", "open-window"]
    assert sorted(item.memory_id for item in preparation.prepared) == sorted(
        ["mem-active", "mem-accepted", "mem-open-window"]
    )
    assert sorted(preparation.withheld) == sorted(
        f"mem-{label}" for label in labels if label not in ("active", "accepted", "open-window")
    )
    assert preparation.failures == ()
    # the helper a service uses to queue work keeps the same rows
    assert sorted(item.memory_id for item in embeddable_deferred_inputs(rows)) == sorted(
        ["mem-active", "mem-accepted", "mem-open-window"]
    )


def test_attach_and_persist_withhold_a_pending_row_without_calling_the_endpoint(monkeypatch) -> None:
    """The write-time helpers send nothing for a candidate and report it as withheld, not failed.

    ``attach_memory_embedding`` returns False and the endpoint is never called;
    the deferred persist names the row in ``withheld_ids`` and opens no store
    connection for it. The same call for an active row embeds it.

    Mutations: remove the gate from ``prepare_memory_embeddings`` (the endpoint is
    called for the candidate); drop ``withheld_ids=preparation.withheld`` from the
    early return in ``persist_deferred_memory_embeddings_outcome`` that runs when
    nothing was prepared (the first ``withheld_ids`` assertion fails), or from the
    final return of ``persist_prepared_memory_embeddings_outcome`` (the mixed
    assertion fails).
    """

    class Store:
        def __init__(self) -> None:
            self.updates: list[str] = []

        def update_memory_embedding(self, *, memory_id: str, **_signature: object) -> dict[str, object]:
            self.updates.append(memory_id)
            return {"id": memory_id}

    contexts_opened: list[int] = []

    @contextmanager
    def store_context():
        contexts_opened.append(1)
        yield Store()

    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        provider = get_embedding_provider()
        store = Store()
        assert attach_memory_embedding(store, _row("candidate", status="candidate"), provider=provider) is False
        assert server.texts == [] and store.updates == []

        outcome = persist_deferred_memory_embeddings_outcome(
            (DeferredMemoryEmbedding.from_memory(_row("needs", status="needs_review")),),
            store_context=store_context,
            provider=provider,
        )
        assert (outcome.attached_ids, outcome.failed, outcome.withheld_ids) == ((), (), ("mem-needs",))
        assert server.texts == [] and contexts_opened == []

        mixed = persist_deferred_memory_embeddings_outcome(
            (
                DeferredMemoryEmbedding.from_memory(_row("live", status="active")),
                DeferredMemoryEmbedding.from_memory(_row("pending", status="candidate")),
            ),
            store_context=store_context,
            provider=provider,
        )
        assert (mixed.attached_ids, mixed.failed, mixed.withheld_ids) == (("mem-live",), (), ("mem-pending",))
        assert server.labels_received(("live", "pending")) == ["live"]

        assert attach_memory_embedding(store, _row("direct", status="active"), provider=provider) is True
        assert store.updates == ["mem-direct"]


# ---------------------------------------------------------------------------
# Service level: a pending write queues no embedding work
# ---------------------------------------------------------------------------


def _memory_store() -> SQLiteVNextStore:
    connection = sqlite3.connect(":memory:")
    bootstrap_sqlite_schema(connection)
    user_id = str(uuid4())
    ensure_sqlite_user(connection, user_id, "embed@example.com")
    return SQLiteVNextStore(connection, user_id)


def _agent() -> AgentIdentity:
    return AgentIdentity(agent_id="hermes", agent_type="personal_assistant", permission_profile="trusted_local_agent")


def _request(label: str, **overrides: object) -> MemoryCommitRequest:
    payload: dict[str, object] = {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "title": f"Title {label}",
        "canonical_text": _marker(label),
        "domain": "professional",
        "sensitivity": "internal",
        "confidence": 0.95,
    }
    payload.update(overrides)
    return MemoryCommitRequest(**payload)  # type: ignore[arg-type]


def test_a_pending_commit_queues_no_embedding_work_and_its_resolution_queues_one(monkeypatch) -> None:
    """A review candidate and a confirmation request queue nothing; confirm queues the active row; reject queues nothing.

    This is the service seam every surface shares (the MCP tools, the CLI and the
    HTTP routes defer the embedding and run it after the commit). It is checked
    without the door's help: the queue itself must hold no pending row, so a
    caller that sends every queued row would still send nothing it should not.

    Mutations: call ``_attach_or_defer_memory_embedding`` again after the row is
    created in ``_create_review_candidate`` or in ``_create_confirmation`` (the
    queue then holds the pending row); stop ``_refresh_memory_derived_state``
    queueing on confirm (the confirmed row is never queued, the last checks fail).
    """

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    store = _memory_store()
    identity = _agent()

    review = VNextMemoryCommitService(store, defer_embeddings=True)
    proposal = review.commit(identity=identity, request=_request("review", source_type="browser_clip"))
    assert (proposal["status"], proposal["memory"]["status"]) == ("review_required", "candidate")
    assert review.deferred_embedding_inputs == ()

    pending = VNextMemoryCommitService(store, defer_embeddings=True)
    asked = pending.commit(identity=identity, request=_request("ask", confidence=0.7))
    assert (asked["status"], asked["memory"]["status"]) == ("confirmation_required", "needs_review")
    assert pending.deferred_embedding_inputs == ()

    direct = VNextMemoryCommitService(store, defer_embeddings=True)
    committed = direct.commit(identity=identity, request=_request("direct"))
    assert committed["status"] == "committed"
    assert [item.canonical_text for item in direct.deferred_embedding_inputs] == [_marker("direct")]
    assert [item.status for item in direct.deferred_embedding_inputs] == ["active"]

    confirm = VNextMemoryCommitService(store, defer_embeddings=True)
    confirm.confirm(identity=None, confirmation_id=asked["confirmation_id"])
    assert [item.canonical_text for item in confirm.deferred_embedding_inputs] == [_marker("ask")]

    second = pending.commit(identity=identity, request=_request("ask-again", confidence=0.7))
    reject = VNextMemoryCommitService(store, defer_embeddings=True)
    reject.confirm(identity=None, confirmation_id=second["confirmation_id"], action="reject")
    assert reject.deferred_embedding_inputs == ()


# ---------------------------------------------------------------------------
# End to end over the MCP tools, a SQLite vault and a recording endpoint
# ---------------------------------------------------------------------------


def _mcp_context(tmp_path: Path) -> tuple[MCPRuntimeContext, Path]:
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
    return MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=str(USER_ID)), database


def _call(context: MCPRuntimeContext, name: str, **arguments: object) -> dict[str, object]:
    return call_mcp_tool(context, name=name, arguments=arguments)


def test_confirmation_required_commit_sends_nothing_confirm_sends_it_once_reject_never(
    tmp_path: Path, monkeypatch
) -> None:
    """A write that waits for confirmation is not embedded until it is confirmed, and a rejected one never is.

    ``alice_memory_commit`` at confidence 0.7 returns ``confirmation_required``
    with the row in ``needs_review``. The endpoint has received nothing and the
    row has no vector. Confirming sends its text once and stores the vector.
    A second pending write that is rejected is never sent. In v0.19.2 the text was
    sent when the write was created, and a second time when it was confirmed.

    Mutation: make ``is_embeddable`` return True and call
    ``_attach_or_defer_memory_embedding`` again in ``_create_confirmation``. The
    first assertion then fails, because the pending text reaches the endpoint
    at creation. Dropping the call that ``confirm`` makes through
    ``_refresh_memory_derived_state`` leaves the confirmed row without a vector,
    and the second check fails.
    """

    context, database = _mcp_context(tmp_path)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)

        asked = _call(
            context, "alice_memory_commit", title="Pending one", canonical_text=_marker("confirm"), confidence=0.7
        )
        assert (asked["status"], asked["memory"]["status"]) == ("confirmation_required", "needs_review")  # type: ignore[index]
        confirm_id = str(asked["memory"]["id"])  # type: ignore[index]
        assert server.texts == []
        assert _vectors_present(database)[confirm_id] is False

        _call(
            context,
            "alice_memory_commit",
            confirmation_id=asked["confirmation_id"],
            confirmation_action="confirm",
        )
        assert _sent(server, "confirm") == 1 and len(server.texts) == 1
        assert _vectors_present(database)[confirm_id] is True

        rejected = _call(
            context, "alice_memory_commit", title="Pending two", canonical_text=_marker("reject"), confidence=0.7
        )
        reject_id = str(rejected["memory"]["id"])  # type: ignore[index]
        _call(
            context,
            "alice_memory_commit",
            confirmation_id=rejected["confirmation_id"],
            confirmation_action="reject",
        )
        assert _sent(server, "reject") == 0 and len(server.texts) == 1
        assert _vectors_present(database)[reject_id] is False

        # a direct commit is recall-visible at once and is embedded at once, once
        direct = _call(
            context, "alice_memory_commit", title="Direct", canonical_text=_marker("direct"), confidence=0.95
        )
        assert direct["status"] == "committed"
        assert _sent(server, "direct") == 1 and len(server.texts) == 2


def test_review_required_commit_sends_nothing_approve_sends_it_once_reject_never(
    tmp_path: Path, monkeypatch
) -> None:
    """A proposal that waits for review is not embedded until it is approved, and a rejected one never is.

    A commit from an external source (``browser_clip``) returns
    ``review_required`` with the row a ``candidate``. Nothing is sent. Approving
    it through ``alice_memory_correct`` sends its text once. Rejecting another
    sends nothing, and edit and approve sends only the edited text, never the
    original. A supersede with a replacement sends the replacement and not the
    candidate it replaces.

    Mutation: make ``is_embeddable`` return True and call
    ``_attach_or_defer_memory_embedding`` again in ``_create_review_candidate``
    (the first assertion fails), or drop ``refresh_memory_derived_state`` after
    the approve in ``mcp/review.py`` (the approved row has no vector).
    """

    context, database = _mcp_context(tmp_path)

    def propose(label: str) -> str:
        proposal = _call(
            context,
            "alice_memory_commit",
            title=f"Clip {label}",
            canonical_text=_marker(label),
            source_type="browser_clip",
        )
        assert (proposal["status"], proposal["memory"]["status"]) == ("review_required", "candidate")  # type: ignore[index]
        return str(proposal["memory"]["id"])  # type: ignore[index]

    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        approve_id = propose("approve")
        reject_id = propose("reject")
        edit_id = propose("original")
        replace_id = propose("replaced")
        assert server.texts == []
        assert set(_vectors_present(database).values()) == {False}

        _call(context, "alice_memory_correct", action="approve", review_item_id=approve_id)
        assert _sent(server, "approve") == 1 and len(server.texts) == 1

        _call(context, "alice_memory_correct", action="reject", review_item_id=reject_id)
        assert _sent(server, "reject") == 0 and len(server.texts) == 1

        _call(
            context,
            "alice_memory_correct",
            action="edit-and-approve",
            review_item_id=edit_id,
            title="Edited title",
            body={"text": _marker("edited")},
        )
        assert (_sent(server, "edited"), _sent(server, "original")) == (1, 0)

        _call(
            context,
            "alice_memory_correct",
            action="supersede-existing",
            review_item_id=replace_id,
            replacement_title="Replacement title",
            replacement_body={"text": _marker("replacement")},
        )
        assert (_sent(server, "replacement"), _sent(server, "replaced")) == (1, 0)
        assert len(server.texts) == 3

        present = _vectors_present(database)
        assert (present[approve_id], present[reject_id], present[edit_id], present[replace_id]) == (
            True,
            False,
            True,
            False,
        )


def test_a_capture_sends_nothing_and_accepting_a_candidate_sends_its_text_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Capture writes candidates and embeds none; approving a captured candidate embeds it once.

    ``alice_capture`` stores the note and proposes candidate memories. The
    endpoint receives nothing. Approving one candidate sends that candidate's
    text once and no other candidate's. In v0.19.2 every candidate was sent when
    the note was captured, whether or not anyone ever accepted it.

    Mutation: make ``DeferredMemoryEmbedding.is_embeddable`` return True. Capture
    hands its candidate rows to the door, which then sends them, and the first
    assertion fails.
    """

    context, database = _mcp_context(tmp_path)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        captured = _call(
            context,
            "alice_capture",
            raw_text=f"I prefer {_marker('capture')} on Tuesdays. Remember that I like the window seat.",
            title="A note",
        )
        assert captured
        assert server.texts == []

        with sqlite_user_connection(database, USER_ID) as conn:
            candidates = SQLiteVNextStore(conn, USER_ID).list_memories(status="candidate")
        assert candidates, "the note should have produced at least one candidate memory"
        assert set(_vectors_present(database).values()) == {False}

        chosen = next(row for row in candidates if _marker("capture") in str(row["canonical_text"]))
        _call(context, "alice_memory_correct", action="approve", review_item_id=str(chosen["id"]))
        assert _sent(server, "capture") == 1 and len(server.texts) == 1
        present = _vectors_present(database)
        assert present[str(chosen["id"])] is True
        assert sum(present.values()) == 1


def test_capture_queues_no_embedding_work_for_its_candidates(monkeypatch) -> None:
    """A deferring capture service hands back an empty queue, because its rows are candidates.

    Mutation: build ``deferred_embedding_inputs`` in ``vnext_capture.py`` from
    ``DeferredMemoryEmbedding.from_memory`` over every row, as v0.19.2 did (the
    queue then holds one item per candidate).
    """

    from alicebot_api.vnext_capture import VNextCaptureService

    monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
    store = _memory_store()
    result = VNextCaptureService(store, defer_embeddings=True).capture_text(  # type: ignore[arg-type]
        f"I prefer {_marker('queued')} on Tuesdays. Remember that I like the window seat.",
        title="Queued note",
    )
    assert result.candidate_memory_count >= 1
    assert result.deferred_embedding_inputs == ()
    assert {row["status"] for row in store.list_memories()} == {"candidate"}


def test_the_commit_cli_sends_nothing_for_a_pending_write_and_confirm_sends_it_once(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """``alicebot vnext memories commit`` and ``confirm`` follow the same rule as the MCP tool.

    The CLI runs against a SQLite store here, through the same patch point the
    backfill test uses. A pending commit sends nothing, confirming it sends its
    text once, and a rejected one is never sent.

    Mutation: call ``_attach_or_defer_memory_embedding`` again in
    ``_create_confirmation`` with the door open. The commit then sends the pending
    text (the first assertion fails).
    """

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")

    @contextmanager
    def sqlite_store_context(_ctx):
        with sqlite_user_connection(db_path, USER_ID) as conn:
            yield SQLiteVNextStore(conn, USER_ID)

    def run(*argv: str) -> dict[str, object]:
        code = cli_module.main(list(argv))
        out = capsys.readouterr().out
        assert code == 0, out
        return json.loads(out)

    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        monkeypatch.setattr(cli_module, "_vnext_store_context", sqlite_store_context)
        monkeypatch.setattr(
            cli_module, "get_settings", lambda: Settings(database_url="postgresql://db", auth_user_id=str(USER_ID))
        )

        confirmed = run(
            "vnext", "memories", "commit", "--title", "Pending", "--text", _marker("cli-confirm"), "--confidence", "0.7"
        )
        assert confirmed["status"] == "confirmation_required"
        assert server.texts == []
        run("vnext", "memories", "confirm", str(confirmed["confirmation_id"]))
        assert _sent(server, "cli-confirm") == 1 and len(server.texts) == 1

        rejected = run(
            "vnext", "memories", "commit", "--title", "Pending", "--text", _marker("cli-reject"), "--confidence", "0.7"
        )
        run("vnext", "memories", "confirm", str(rejected["confirmation_id"]), "--action", "reject")
        assert _sent(server, "cli-reject") == 0 and len(server.texts) == 1

        proposed = run(
            "vnext",
            "memories",
            "commit",
            "--title",
            "Clip",
            "--text",
            _marker("cli-review"),
            "--source-type",
            "browser_clip",
        )
        assert proposed["status"] == "review_required"
        assert _sent(server, "cli-review") == 0 and len(server.texts) == 1


# ---------------------------------------------------------------------------
# Every path that makes a pending row active embeds it, with the text it then has
# ---------------------------------------------------------------------------


def test_edit_on_confirm_and_correct_embed_the_new_text_once_and_never_the_pending_text(monkeypatch) -> None:
    """Confirm with an edit and ``correct`` embed the text the row has after the change, and not the old text.

    Both paths promote a pending row. The pending text was never sent, and the
    text the row carries when it turns active is sent once.

    Mutations: hand ``confirm`` the row as it was before the update (``memory``
    instead of ``updated``) in its call to ``_refresh_memory_derived_state``: that
    row is still ``needs_review``, so the door withholds it and the edited text is
    never sent. Or drop the refresh after the status change in ``confirm`` or in
    ``correct`` (nothing is sent for that path).
    """

    store = _memory_store()
    identity = _agent()
    service = VNextMemoryCommitService(store)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)

        asked = service.commit(identity=identity, request=_request("edit-old", confidence=0.7))
        review = service.commit(identity=identity, request=_request("correct-old", source_type="browser_clip"))
        assert server.texts == []

        service.confirm(
            identity=None,
            confirmation_id=asked["confirmation_id"],
            action="edit",
            canonical_text=_marker("edit-new"),
        )
        assert (_sent(server, "edit-new"), _sent(server, "edit-old")) == (1, 0)

        service.correct(identity=None, memory_id=str(review["memory"]["id"]), canonical_text=_marker("correct-new"))
        assert (_sent(server, "correct-new"), _sent(server, "correct-old")) == (1, 0)
        assert len(server.texts) == 2


def test_accepting_a_consolidation_candidate_embeds_it_once(monkeypatch) -> None:
    """A consolidation candidate (``candidate``) is embedded when it is accepted, and its members are not sent again.

    Mutation: drop ``_refresh_memory_derived_state`` after the update in
    ``accept_consolidation_candidate`` (nothing is sent and the accepted row has
    no vector).
    """

    store = _memory_store()
    members = [
        str(
            store.create_memory(
                {
                    "memory_key": f"member.{index}",
                    "value": {"text": _marker(f"member-{index}")},
                    "status": "active",
                    "memory_type": "semantic",
                    "title": f"Member {index}",
                    "canonical_text": _marker(f"member-{index}"),
                    "domain": "professional",
                    "sensitivity": "internal",
                }
            )["id"]
        )
        for index in range(2)
    ]
    candidate = store.create_memory(
        {
            "memory_key": "consolidation.candidate",
            "value": {"text": _marker("consolidated")},
            "status": "candidate",
            "memory_type": "semantic",
            "title": "Consolidated",
            "canonical_text": _marker("consolidated"),
            "domain": "professional",
            "sensitivity": "internal",
            "metadata_json": {
                "candidate_kind": "memory_consolidation",
                "consolidation_digest": "digest-1",
                "review_required": True,
                "consolidation": {
                    "cluster_member_ids": members,
                    "member_snapshots": [memory_version_snapshot(store.get_memory(member)) for member in members],
                    "proposal_kind": "merge",
                    "survivor_memory_id": None,
                    "proposed_supersede": list(members),
                    "reviewer_instructions": ["Review candidate memory."],
                },
            },
        }
    )
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        result = VNextMemoryCommitService(store).accept_consolidation_candidate(
            str(candidate["id"]), reason="Reviewed and accepted the merge."
        )
        assert result["status"] == "accepted"
        assert _sent(server, "consolidated") == 1 and len(server.texts) == 1


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def _add_active(db_path: Path, label: str, *, valid_to: str | None = None) -> str:
    with sqlite_user_connection(db_path, USER_ID) as conn:
        row = SQLiteVNextStore(conn, USER_ID).create_memory(
            {
                "memory_key": f"expiry-{label}",
                "value": {"text": _marker(label)},
                "memory_type": "semantic",
                "title": f"Title {label}",
                "canonical_text": _marker(label),
                "status": "active",
                "domain": "project",
                "sensitivity": "private",
                "valid_to": valid_to,
            }
        )
        return str(row["id"])


def _commit_service_call(db_path: Path, operation: str, memory_id: str) -> None:
    with sqlite_user_connection(db_path, USER_ID) as conn:
        service = VNextMemoryCommitService(SQLiteVNextStore(conn, USER_ID))
        getattr(service, operation)(memory_id, reason="test of the validity window")


def test_reindex_and_the_doctor_leave_out_an_expired_memory_and_follow_valid_to(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """An expired memory is not sent by reindex and is not counted by the doctor, and both follow ``valid_to``.

    Three active memories: one whose window closed (made so with ``expire``), one
    whose window is open far into the future, and one with no window. The doctor
    counts two and reindex sends two texts, never the expired one. ``unexpire``
    makes the expired one count and reindex sends it. Expiring a memory that
    already has a vector keeps the vector, and a model change does not send its
    text again until it is unexpired. In v0.19.2 reindex sent the expired text
    and the doctor counted it.

    Mutations: drop ``{expiry_sql}`` from the SQLite list (the first reindex sends
    the expired text); drop it from the count (the doctor says 3 while reindex
    embeds 2); reverse the comparison in ``_expiry_clause`` (the open rows are
    left out and the expired one is sent); put the expiry test inside the
    ``embedding IS NULL`` branch (the expired memory is sent under the new model).
    """

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    gone = _add_active(db_path, "gone")
    _commit_service_call(db_path, "expire", gone)
    open_window = _add_active(db_path, "open-window", valid_to=FAR_FUTURE)
    plain = _add_active(db_path, "plain")
    labels = ("gone", "open-window", "plain")
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server, model="model-a")

        assert _doctor_line(db_path, capsys) == "2"
        code, payload = _reindex(db_path, capsys, "--batch-size", "1")
        assert (code, payload["embedded"], payload["failed"], payload["skipped"]) == (0, 2, 0, 0)
        assert server.labels_received(labels) == ["open-window", "plain"]
        present = _vectors_present(db_path)
        assert (present[gone], present[open_window], present[plain]) == (False, True, True)
        assert _doctor_line(db_path, capsys) == "0"

        # the window is reopened: the memory counts again and the next reindex embeds it
        _commit_service_call(db_path, "unexpire", gone)
        assert _doctor_line(db_path, capsys) == "1"
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["embedded"]) == (0, 1)
        assert server.labels_received(labels) == ["gone", "open-window", "plain"]
        assert len(server.texts) == 3
        assert _doctor_line(db_path, capsys) == "0"

        # a memory that is expired after it was embedded keeps its vector and is not sent again
        _commit_service_call(db_path, "expire", plain)
        assert _vectors_present(db_path)[plain] is True
        _configure(monkeypatch, server, model="model-b")
        sent_before = len(server.texts)
        assert _doctor_line(db_path, capsys) == "2"
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["embedded"], payload["reindexed_incompatible"]) == (0, 2, 2)
        sent_after = server.texts[sent_before:]
        assert not any(_marker("plain") in text for text in sent_after)
        assert len(sent_after) == 2


def test_the_sqlite_list_and_count_read_recalls_own_expiry_clause(tmp_path: Path, monkeypatch) -> None:
    """The reindex list and the doctor count call ``_expiry_clause``, the function recall's SQL calls.

    The clause is replaced by one that matches no row. If the list and the count
    read it, both return nothing even for live rows. A copy of the test inside
    either query would ignore the replacement and still return the live row.

    Mutation: write ``(valid_to IS NULL OR valid_to >= ?)`` inline in
    ``list_memories_missing_embeddings`` or in ``count_memories_missing_embeddings``
    instead of calling ``_expiry_clause`` (the replacement has no effect there and
    the live row is still returned or counted).
    """

    count_memories_missing_embeddings = sqlite_embedding_cas.count_memories_missing_embeddings

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    _add_active(db_path, "live")
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        assert len(store.list_memories_missing_embeddings(statuses=MEMORY_SEARCHABLE_STATUSES)) == 1
        assert count_memories_missing_embeddings(store, statuses=MEMORY_SEARCHABLE_STATUSES) == 1

        monkeypatch.setattr(sqlite_embedding_cas, "_expiry_clause", lambda *_a, **_k: (" AND 0", []))
        assert store.list_memories_missing_embeddings(statuses=MEMORY_SEARCHABLE_STATUSES) == []
        assert count_memories_missing_embeddings(store, statuses=MEMORY_SEARCHABLE_STATUSES) == 0


def test_a_row_that_expires_between_the_list_and_the_send_is_withheld_not_failed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """If the list hands reindex an expired row, the door withholds it and reindex counts it as skipped.

    The shared expiry clause is replaced by an empty one, which is the state a row
    reaches when its window closes after the list was read. Reindex still sends
    only the live text, exits 0, and reports the expired row as ``skipped``.

    Mutations: remove the gate from ``prepare_memory_embeddings`` (the expired text
    is sent), or drop ``skipped += len(preparation.withheld)`` in
    ``_run_reindex_embeddings`` (``skipped`` is 0).
    """

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    gone = _add_active(db_path, "gone", valid_to=PAST)
    live = _add_active(db_path, "live")
    monkeypatch.setattr(sqlite_embedding_cas, "_expiry_clause", lambda *_a, **_k: ("", []))
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["embedded"], payload["failed"], payload["skipped"]) == (0, 1, 0, 1)
        assert server.labels_received(("gone", "live")) == ["live"]
        assert _vectors_present(db_path) == {gone: False, live: True}


def test_the_backfill_command_withholds_an_expired_row_the_list_returned(monkeypatch, capsys) -> None:
    """``backfill-embeddings`` counts a row the door withholds as skipped, sends nothing for it and exits 0.

    The stand-in store lists an expired row as a racing store would. The command
    sends only the live text.

    Mutation: remove the gate from ``prepare_memory_embeddings`` (the expired text
    is sent), or drop ``skipped += len(outcome.withheld_ids)`` or the ``withheld_ids``
    union from ``listed_ids`` in ``_run_vnext_memories_backfill_embeddings`` (the
    row is counted as a failure and the command raises).
    """

    rows = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "status": "active",
            "title": "Title gone",
            "canonical_text": _marker("gone"),
            "valid_to": PAST,
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "status": "active",
            "title": "Title live",
            "canonical_text": _marker("live"),
            "valid_to": None,
        },
    ]
    store = _StatusAwareStore(rows)

    @contextmanager
    def fake_vnext_store_context(_ctx):
        yield store

    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        monkeypatch.setattr(cli_module, "_vnext_store_context", fake_vnext_store_context)
        monkeypatch.setattr(
            cli_module, "get_settings", lambda: Settings(database_url="postgresql://db", auth_user_id=str(USER_ID))
        )
        exit_code = cli_module.main(["vnext", "memories", "backfill-embeddings", "--batch-size", "5"])
        payload = json.loads(capsys.readouterr().out)
        assert (exit_code, payload["embedded"], payload["failed"], payload["skipped"]) == (0, 1, 0, 1)
        assert server.labels_received(("gone", "live")) == ["live"]
    assert sorted(store.vectors) == ["00000000-0000-4000-8000-000000000002"]


def test_the_postgres_list_excludes_an_expired_memory_in_the_statement_it_sends() -> None:
    """The Postgres statement holds the shared unexpired test between the status test and the signature terms.

    No parameter is added for it: it reads ``clock_timestamp()``, as recall does.

    Mutation: drop ``AND {POSTGRES_UNEXPIRED_SQL}`` from
    ``list_memories_missing_embeddings`` in ``vnext_stores/postgres/embedding_cas.py``
    (the substring is missing), or place it after the signature terms (the order
    check fails).
    """

    class Cursor:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[object, ...]]] = []

        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            self.queries.append((query, params))

        def fetchall(self) -> list[dict[str, object]]:
            return []

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self) -> Cursor:
            return self.cursor_instance

    connection = Connection()
    store = PostgresVNextStore(connection)  # type: ignore[arg-type]
    store.list_memories_missing_embeddings(statuses=("active", "accepted"), limit=7)
    store.list_memories_missing_embeddings(
        statuses=("active", "accepted"), limit=7, embedding_provider="prov", embedding_model="mod"
    )
    (plain_query, plain_params), (signed_query, signed_params) = connection.cursor_instance.queries
    for query in (plain_query, signed_query):
        assert f"AND {POSTGRES_UNEXPIRED_SQL}\n" in query
        assert query.index("status IN") < query.index(POSTGRES_UNEXPIRED_SQL) < query.index("embedding_vector IS NULL")
    assert plain_params == ("active", "accepted", None, None, 7)
    assert signed_params == ("active", "accepted", "prov", "mod", None, None, 7)
    assert signed_query.index(POSTGRES_UNEXPIRED_SQL) < signed_query.index("IS DISTINCT FROM")
    assert plain_query.count("%s") == len(plain_params) and signed_query.count("%s") == len(signed_params)


def _callers_passing_include_expired_true() -> list[str]:
    """Every call in the package that passes ``include_expired=True``, as ``file:line``."""
    import ast

    source_root = Path(inspect.getsourcefile(postgres_memory_access)).parents[2]  # type: ignore[arg-type]
    found: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if (
                        keyword.arg == "include_expired"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is True
                    ):
                        found.append(f"{path.relative_to(source_root)}:{node.lineno}")
    return found


def test_reindex_recall_and_the_stores_share_one_expiry_test() -> None:
    """The listing's expiry test is recall's: SQLite calls the same function, Postgres holds the same text.

    SQLite recall builds its clause with ``_expiry_clause`` four times and the
    embedding list and count call the same function. Postgres recall writes
    ``valid_to IS NULL OR valid_to >= clock_timestamp()`` four times and the
    embedding list interpolates ``POSTGRES_UNEXPIRED_SQL``, which holds the same
    text. A change to the test in one place and not the others fails here.

    Mutations: change the comparison in ``POSTGRES_UNEXPIRED_SQL`` (``>`` for
    ``>=``) or in one of Postgres recall's four statements; change the clause in
    ``_expiry_clause``; stop either SQLite embedding function calling it; add a
    call that passes ``include_expired=True`` anywhere in the package (the last
    check then lists it).
    """

    from alicebot_api.vnext_stores.sqlite.query_predicates import _expiry_clause

    shared = POSTGRES_UNEXPIRED_SQL[1:-1]
    assert shared == "valid_to IS NULL OR valid_to >= clock_timestamp()"
    postgres_recall = inspect.getsource(postgres_memory_access)
    assert postgres_recall.count(f"(%s::boolean OR {shared})") == 4

    clause, params = _expiry_clause(False)  # type: ignore[operator]
    assert clause == " AND (valid_to IS NULL OR valid_to >= ?)" and len(params) == 1
    assert _expiry_clause(True) == ("", [])  # type: ignore[operator]
    sqlite_recall = inspect.getsource(sqlite_memory_access)
    assert sqlite_recall.count("self._expiry_clause(include_expired") == 4
    # the embedding list and count call the very function the store grafts for recall
    assert sqlite_embedding_cas._expiry_clause.__func__ is SQLiteVNextStore._expiry_clause  # type: ignore[attr-defined]
    assert sqlite_embedding_cas._expiry_clause is _expiry_clause
    embedding_source = inspect.getsource(sqlite_embedding_cas)
    assert embedding_source.count("_expiry_clause(False)") == 2
    # nothing asks recall for expired rows, so no search path returns one
    assert _callers_passing_include_expired_true() == []


def test_the_constant_is_defined_once_and_the_modules_that_name_it_hold_the_same_object() -> None:
    """``MEMORY_SEARCHABLE_STATUSES`` lives in ``vnext_recall_visibility`` and is re-exported by retrieval.

    Mutation: define a second tuple in ``vnext_retrieval`` (the identity check fails).
    """

    from alicebot_api import vnext_recall_visibility, vnext_retrieval
    from alicebot_api.session_briefing import COMMITTED_MEMORY_STATUSES

    assert vnext_retrieval.MEMORY_SEARCHABLE_STATUSES is vnext_recall_visibility.MEMORY_SEARCHABLE_STATUSES
    assert COMMITTED_MEMORY_STATUSES is vnext_recall_visibility.MEMORY_SEARCHABLE_STATUSES
    assert MEMORY_SEARCHABLE_STATUSES == ("active", "accepted")
