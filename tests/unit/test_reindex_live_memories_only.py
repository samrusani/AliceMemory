"""Reindex and backfill send only the text of memories that recall can return.

Every test that talks to an embeddings endpoint talks to a fake one on
127.0.0.1, started here, that records every text it receives. Nothing in this
file reaches the network or a paid API, and no Postgres is needed: the Postgres
statement is checked as text, and the Postgres backfill command is run against a
store that lists rows the way the real one does.

Recall, the context pack and the doctor read the statuses ``active`` and
``accepted``. A forgotten (``superseded``), rejected, candidate or otherwise
non-live memory is never returned by them, so its text is not sent to the
endpoint, and a memory that later becomes active is embedded then.

Each test names, in its docstring, the change to the code that must fail it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

import pytest

import alicebot_api.cli as cli_module
from alicebot_api.config import Settings
from alicebot_api.onramp import bootstrap_database, main as onramp_main
from alicebot_api.session_briefing import COMMITTED_MEMORY_STATUSES
from alicebot_api.sqlite_schema import MEMORY_STATUSES
from alicebot_api.sqlite_store import SQLiteVNextStore, ensure_sqlite_user, sqlite_user_connection
from alicebot_api.store import ContinuityStoreInvariantError
from alicebot_api.vnext_embeddings import (
    EMBEDDINGS_API_KEY_ENV,
    EMBEDDINGS_BASE_URL_ENV,
    EMBEDDINGS_MAX_INPUT_CHARS_ENV,
    EMBEDDINGS_MODEL_ENV,
)
from alicebot_api.vnext_retrieval import MEMORY_SEARCHABLE_STATUSES
from alicebot_api.vnext_store import PostgresVNextStore
from alicebot_api.vnext_stores.postgres import memory_access as postgres_memory_access
from alicebot_api.vnext_stores.sqlite import memory_access as sqlite_memory_access

USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")
LIVE_STATUSES = ("active", "accepted")


def _marker(label: str) -> str:
    """The text of the memory made for ``label``: one string no other memory holds."""
    return f"marker-{label}-text"


class _RecordingEmbeddingsServer:
    """An OpenAI-shaped ``/v1/embeddings`` endpoint on 127.0.0.1 that records every text it gets."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                inputs = json.loads(raw)["input"]
                server.texts.extend(inputs)
                body = json.dumps(
                    {
                        "data": [
                            {"index": index, "embedding": _vector(text)} for index, text in enumerate(inputs)
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self) -> "_RecordingEmbeddingsServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def received(self) -> str:
        """Everything the endpoint has been sent so far, as one string."""
        return "\n".join(self.texts)

    def labels_received(self, labels: tuple[str, ...]) -> list[str]:
        """The labels, in the order given, whose marker text reached the endpoint."""
        blob = self.received()
        return [label for label in labels if _marker(label) in blob]


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(byte - 128) / 128.0 for byte in digest[:16]]


def _configure(monkeypatch, server: _RecordingEmbeddingsServer, *, model: str = "fake-embed") -> None:
    monkeypatch.setenv(EMBEDDINGS_BASE_URL_ENV, server.base_url)
    monkeypatch.setenv(EMBEDDINGS_MODEL_ENV, model)
    monkeypatch.delenv(EMBEDDINGS_API_KEY_ENV, raising=False)
    monkeypatch.delenv(EMBEDDINGS_MAX_INPUT_CHARS_ENV, raising=False)


def _add_memory(
    db_path: Path, label: str, *, status: str, user_id: UUID = USER_ID, deleted: bool = False
) -> str:
    """Add one memory whose text is ``_marker(label)``; returns its id."""
    with sqlite_user_connection(db_path, user_id) as conn:
        ensure_sqlite_user(conn, user_id, f"{user_id}@alice")
        row = SQLiteVNextStore(conn, user_id).create_memory(
            {
                "memory_key": f"reindex-{label}",
                "value": {"text": _marker(label)},
                "memory_type": "semantic",
                "title": f"Title {label}",
                "canonical_text": _marker(label),
                "status": status,
                "domain": "project",
                "sensitivity": "private",
            }
        )
        if deleted:
            conn.execute("UPDATE memories SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (row["id"],))
        return str(row["id"])


def _set_status(db_path: Path, memory_id: str, status: str) -> None:
    with sqlite_user_connection(db_path, USER_ID) as conn:
        conn.execute("UPDATE memories SET status = ? WHERE id = ?", (status, memory_id))


def _vectors_present(db_path: Path) -> dict[str, bool]:
    """{memory id: has a vector} for every row of the vault, whoever owns it."""
    connection = sqlite3.connect(db_path)
    try:
        return {
            str(memory_id): embedding is not None
            for memory_id, embedding in connection.execute("SELECT id, embedding FROM memories")
        }
    finally:
        connection.close()


def _reindex(db_path: Path, capsys, *extra: str) -> tuple[int, dict[str, object]]:
    code = onramp_main(["reindex-embeddings", "--db", str(db_path), "--user-id", str(USER_ID), *extra])
    return code, json.loads(capsys.readouterr().out)


def _doctor_line(db_path: Path, capsys, label: str = "memories without a current vector") -> str:
    assert onramp_main(["doctor", "--db", str(db_path), "--user-id", str(USER_ID)]) == 0
    for line in capsys.readouterr().out.splitlines():
        if line.startswith(f"{label}: "):
            return line[len(label) + 2 :]
    raise AssertionError(f"no {label!r} line")


def _seed_every_status(db_path: Path) -> dict[str, str]:
    """One memory per status, a deleted active one, and another user's active one."""
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    ids = {status: _add_memory(db_path, status, status=status) for status in MEMORY_STATUSES}
    ids["deleted"] = _add_memory(db_path, "deleted", status="active", deleted=True)
    ids["other-user"] = _add_memory(db_path, "other-user", status="active", user_id=OTHER_USER_ID)
    return ids


ALL_LABELS = (*MEMORY_STATUSES, "deleted", "other-user")


def test_reindex_sends_only_active_and_accepted_text_and_only_this_users_live_rows(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The endpoint receives the text of the active and accepted memories and of nothing else.

    The vault holds one memory in each of the nine statuses, an active memory
    with ``deleted_at`` set, and an active memory of another user. Reindex runs
    in pages of two, so the live rows sit between rows it must skip and a paging
    mistake would drop one. The doctor counts the same two rows reindex then
    embeds, and the other rows are left with no vector. In v0.19.2 the endpoint
    received the text of all nine statuses of this user.

    Mutations, each made and each failing an assertion here: pass every status
    to ``list_memories_missing_embeddings`` in ``_run_reindex_embeddings`` (the
    seven non-live texts reach the endpoint); drop ``AND status IN (...)`` from
    the SQLite query (the same); replace ``user_id = ?`` there with a condition
    that is always true (the other user's text reaches the endpoint); drop
    ``AND deleted_at IS NULL`` (the deleted row's text reaches it).
    """

    db_path = tmp_path / "memory.db"
    ids = _seed_every_status(db_path)
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)

        assert _doctor_line(db_path, capsys) == "2"
        code, payload = _reindex(db_path, capsys, "--batch-size", "2")

        assert (code, payload["embedded"], payload["failed"], payload["skipped"]) == (0, 2, 0, 0)
        assert server.labels_received(ALL_LABELS) == ["active", "accepted"]
        present = _vectors_present(db_path)
        assert {label: present[memory_id] for label, memory_id in ids.items()} == {
            label: label in LIVE_STATUSES for label in ALL_LABELS
        }
        assert _doctor_line(db_path, capsys) == "0"

        # nothing is left to do, and a second run sends nothing
        sent_before = len(server.texts)
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["batches"], payload["embedded"]) == (0, 0, 0)
        assert len(server.texts) == sent_before


def test_a_memory_that_becomes_active_is_embedded_by_the_next_reindex(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A candidate is not sent while it is a candidate, and is sent once it is active.

    Mutation: pass only ``accepted`` to the list query in
    ``_run_reindex_embeddings``. The flipped row is then never listed, the doctor
    still counts it, and the second reindex sends nothing, so the last
    assertions fail. Pass every status instead and the first reindex sends the
    candidate's text, so the first check fails.
    """

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    live_id = _add_memory(db_path, "live", status="active")
    candidate_id = _add_memory(db_path, "candidate", status="candidate")
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server)
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["embedded"]) == (0, 1)
        assert server.labels_received(("live", "candidate")) == ["live"]
        assert _vectors_present(db_path) == {live_id: True, candidate_id: False}

        _set_status(db_path, candidate_id, "active")
        assert _doctor_line(db_path, capsys) == "1"
        code, payload = _reindex(db_path, capsys)

        assert (code, payload["embedded"]) == (0, 1)
        assert server.labels_received(("live", "candidate")) == ["live", "candidate"]
        assert _vectors_present(db_path) == {live_id: True, candidate_id: True}
        assert _doctor_line(db_path, capsys) == "0"


def test_a_forgotten_memory_with_an_out_of_date_vector_is_not_embedded_again(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The status limit holds for a row that already has a vector the signature calls stale.

    A memory is embedded while active. It is forgotten (superseded) and the
    embedding model changes, which makes its vector stale. Reindex does not
    send its text again. When it is restored to active, it is sent. The doctor
    agrees at each step.

    Mutation: build the SQLite condition as ``embedding IS NULL AND status IN
    (...)`` followed by the signature terms joined with ``OR``, so the status
    limit covers only the rows with no vector. The forgotten row is then sent
    under the new model and the first check fails.
    """

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    memory_id = _add_memory(db_path, "once-live", status="active")
    with _RecordingEmbeddingsServer() as server:
        _configure(monkeypatch, server, model="model-a")
        assert _reindex(db_path, capsys)[1]["embedded"] == 1
        assert _doctor_line(db_path, capsys) == "0"

        _set_status(db_path, memory_id, "superseded")
        _configure(monkeypatch, server, model="model-b")
        sent_before = len(server.texts)
        assert _doctor_line(db_path, capsys) == "0"
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["embedded"], payload["reindexed_incompatible"]) == (0, 0, 0)
        assert len(server.texts) == sent_before

        _set_status(db_path, memory_id, "active")
        assert _doctor_line(db_path, capsys) == "1"
        code, payload = _reindex(db_path, capsys)
        assert (code, payload["embedded"], payload["reindexed_incompatible"]) == (0, 1, 1)
        assert len(server.texts) == sent_before + 1
        assert _doctor_line(db_path, capsys) == "0"


def test_the_stores_require_a_status_list_and_list_only_the_statuses_they_are_given(tmp_path: Path) -> None:
    """``statuses`` has no default, an empty or bare-string value is refused, and the filter is exact.

    A caller that forgets the argument gets a ``TypeError`` and cannot send every
    row to the endpoint. SQLite is run against a real vault. Postgres is checked
    as the statement it would send, with one placeholder per status, the statuses
    bound first, and nothing sent to the database when the value is refused.

    Mutations: give ``statuses`` a default of ``("active", "accepted")`` in
    either store (the ``TypeError`` check fails); drop the ``not statuses`` test
    from ``_embedding_status_values`` in either store (the empty list is then
    accepted); drop the ``isinstance(statuses, str)`` test in either (the string
    ``"active"`` is then read as the statuses ``a``, ``c``, ``t`` and so on and no
    error is raised); ignore the list in SQLite and always filter on the two live
    statuses (the ``candidate`` and ``superseded`` listings are empty); drop the
    ``status IN`` line from the Postgres statement; bind the statuses after the
    signature terms there (the signed statement's parameters are out of order).
    """

    db_path = tmp_path / "memory.db"
    bootstrap_database(db_path, user_id=USER_ID, user_email="local@alice")
    ids = {status: _add_memory(db_path, status, status=status) for status in ("active", "candidate", "superseded")}
    with sqlite_user_connection(db_path, USER_ID) as conn:
        store = SQLiteVNextStore(conn, USER_ID)
        with pytest.raises(TypeError, match="statuses"):
            store.list_memories_missing_embeddings()  # type: ignore[call-arg]
        for bad in ((), [], "active", ("active", "")):
            with pytest.raises(ContinuityStoreInvariantError, match="statuses"):
                store.list_memories_missing_embeddings(statuses=bad)  # type: ignore[arg-type]
        listed = {
            status: [row["id"] for row in store.list_memories_missing_embeddings(statuses=(status,))]
            for status in ("active", "candidate", "superseded", "rejected")
        }
        assert listed == {
            "active": [ids["active"]],
            "candidate": [ids["candidate"]],
            "superseded": [ids["superseded"]],
            "rejected": [],
        }
        both = store.list_memories_missing_embeddings(statuses=("active", "candidate"))
        assert sorted(str(row["id"]) for row in both) == sorted([ids["active"], ids["candidate"]])

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
    postgres = PostgresVNextStore(connection)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="statuses"):
        postgres.list_memories_missing_embeddings()  # type: ignore[call-arg]
    for bad in ((), [], "active", ("active", "")):
        with pytest.raises(ContinuityStoreInvariantError, match="statuses"):
            postgres.list_memories_missing_embeddings(statuses=bad)  # type: ignore[arg-type]
    assert connection.cursor_instance.queries == []

    postgres.list_memories_missing_embeddings(statuses=("active",), limit=7)
    postgres.list_memories_missing_embeddings(statuses=("active", "accepted"), limit=7)
    postgres.list_memories_missing_embeddings(
        statuses=("active", "accepted"), limit=7, embedding_provider="prov", embedding_model="mod"
    )
    (one_query, one_params), (two_query, two_params), (signed_query, signed_params) = (
        connection.cursor_instance.queries
    )
    assert "AND status IN (%s)\n" in one_query
    assert "AND status IN (%s, %s)\n" in two_query
    assert one_params == ("active", None, None, 7)
    assert two_params == ("active", "accepted", None, None, 7)
    assert one_query.count("%s") == len(one_params)
    assert two_query.count("%s") == len(two_params)
    # the statuses come before the signature terms in the statement, so they are bound before them
    assert signed_params == ("active", "accepted", "prov", "mod", None, None, 7)
    assert signed_query.count("%s") == len(signed_params)
    assert signed_query.index("status IN") < signed_query.index("IS DISTINCT FROM")
    assert one_query.index("deleted_at IS NULL") < one_query.index("status IN") < one_query.index("embedding_vector IS NULL")


def test_reindex_doctor_recall_and_the_stores_name_the_same_statuses() -> None:
    """One tuple, ``MEMORY_SEARCHABLE_STATUSES``, is what the callers pass and what recall's SQL reads.

    The doctor's name for it is the same object, and each store's SQL constant
    for the recall queries renders the same two statuses. A status added to one
    place and not the others fails here, not by sending a stranger's text.

    Mutation: add a status to ``MEMORY_SEARCHABLE_STATUSES`` (the two SQL
    constants then differ from it), or to a store's ``_MEMORY_SEARCHABLE_STATUSES_SQL``
    (that store then differs), or rebind ``COMMITTED_MEMORY_STATUSES`` to its own
    tuple (the identity check fails).
    """

    assert MEMORY_SEARCHABLE_STATUSES == LIVE_STATUSES
    assert COMMITTED_MEMORY_STATUSES is MEMORY_SEARCHABLE_STATUSES
    rendered = "(" + ", ".join(f"'{status}'" for status in MEMORY_SEARCHABLE_STATUSES) + ")"
    assert sqlite_memory_access._MEMORY_SEARCHABLE_STATUSES_SQL == rendered
    assert postgres_memory_access._MEMORY_SEARCHABLE_STATUSES_SQL == rendered
    assert set(MEMORY_SEARCHABLE_STATUSES) < set(MEMORY_STATUSES)


class _StatusAwareStore:
    """A store that lists rows the way the real ones do: only the statuses the caller names."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.vectors: dict[str, list[float]] = {}
        self.statuses_asked: list[tuple[str, ...]] = []

    def list_memories_missing_embeddings(
        self, *, statuses, limit: int = 100, after_id: str | None = None, **_signature: object
    ) -> list[dict[str, object]]:
        self.statuses_asked.append(tuple(statuses))
        missing = [
            row
            for row in sorted(self.rows, key=lambda item: str(item["id"]))
            if row["status"] in statuses
            and str(row["id"]) not in self.vectors
            and (after_id is None or str(row["id"]) > after_id)
        ]
        return missing[:limit]

    def update_memory_embedding(self, *, memory_id: str, vector: list[float], **_signature: object):
        self.vectors[memory_id] = vector
        return {"id": memory_id}


def test_postgres_backfill_command_sends_only_active_and_accepted_text(monkeypatch, capsys) -> None:
    """``alicebot vnext memories backfill-embeddings`` names the live statuses and sends no other text.

    The store here is a stand-in for the Postgres one that lists the statuses it
    is asked for, and the endpoint is the recording fake on 127.0.0.1. The
    command is run in pages of two.

    Mutation: pass every status, or leave the argument out, in
    ``_run_vnext_memories_backfill_embeddings``. The first sends the seven
    non-live texts, and the second raises ``TypeError`` from the store.
    """

    rows = [
        {
            "id": f"00000000-0000-4000-8000-{index:012d}",
            "status": status,
            "title": f"Title {status}",
            "canonical_text": _marker(status),
        }
        for index, status in enumerate(MEMORY_STATUSES, start=1)
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

        exit_code = cli_module.main(["vnext", "memories", "backfill-embeddings", "--batch-size", "2"])

        payload = json.loads(capsys.readouterr().out)
        assert (exit_code, payload["embedded"], payload["failed"]) == (0, 2, 0)
        assert server.labels_received(tuple(MEMORY_STATUSES)) == ["active", "accepted"]
    assert store.statuses_asked
    assert all(asked == MEMORY_SEARCHABLE_STATUSES for asked in store.statuses_asked)
    assert sorted(store.vectors) == sorted(
        str(row["id"]) for row in rows if row["status"] in LIVE_STATUSES
    )
