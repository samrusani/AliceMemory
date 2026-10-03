"""A quote saved from a source is shown only to a caller who may read that source now.

Unreleased (on main, not in v0.20.0). A link from a memory to a source stores the quote it was made with, and the
memory keeps copies of that quote in its own metadata (``metadata_json.provenance``, ``replacement_provenance`` and
``agentic_memory.conversation_excerpt``). The write fence checks the caller's permission on the source once, when the
link is made. A source can be reclassified after that (sensitivity raised, domain changed, project moved) or archived,
and on v0.20.0 and main before this change every reader kept returning the saved quote to a caller below the new
label: ``alice_memory_review`` by id (the link and the metadata copy), the context pack's ``supporting_evidence`` and
the full memory rows of the HTTP pack, and the memory row that ``alice_memory_manage`` (expire, unexpire, undo, forget)
and ``alice_memory_correct`` hand back. ``alice_explain`` and a source search already refused. Each reader now asks the
fence of ``vnext_source_fence.py`` again, with the caller's current permission on each linked source.

The tests here run the lifecycle on a real SQLite vault with real minted agent keys: a unique quote is saved on a
memory through each door that stores one, the source is reclassified with the shipped ``review_vnext_source`` handler
(``POST /v0/vnext/sources/{id}/review``), and every reader is called by every kind of key. The unique quote is searched
for in the serialized answer, not looked up at a field, so a copy in a field nobody listed is found as well. A control
reads each surface before the change of label and finds the quote there, so an absent quote means the reader withheld
it and not that the surface never carried it.

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the file was
restored by copying the saved copy back.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import mcp_server
from alicebot_api.config import Settings
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_keys import create_agent_key

_USER_ID = "00000000-0000-0000-0000-000000000001"
_KEY_ENV = "ALICE_AGENT_API_KEY"

# Two unique words inside one quote. A copy cut in the middle still holds one of them.
_WORD_A = "zinnwald-quote-8841"
_WORD_B = "marlin-oxide-5520"
# No word of the quote matches a capture rule (the extractor turns a line with "is", "needs" and the like into a
# candidate memory, which would copy the quote into a memory's own text), so the quote reaches a memory only as saved
# provenance.
_QUOTE = f"{_WORD_A} cone ten firing kiln log {_WORD_B}"
_SOURCE_TEXT = f"Alpha pottery log. Operator note: {_QUOTE}. End of log."

_KEY_SPECS = {
    # name: (permission profile, project the key is bound to)
    "trusted": ("trusted_local_agent", "alpha"),
    "project": ("project_scoped_agent", "alpha"),
    "admin": ("admin_agent", "alpha"),
    "read_only": ("read_only_agent", "alpha"),
    "unbound": ("trusted_local_agent", None),
}
_AGENT_IDS = {name: f"{profile}-{project or 'none'}" for name, (profile, project) in _KEY_SPECS.items()}

# Who may still read the source after each change, from the policy of each profile and nothing else: the default
# ceiling is public, internal, private and unknown (read_only: public, internal, unknown; admin: everything); a
# project scoped and a read only key may not read the health, family, spiritual, legal or financial domains; a key
# bound to alpha reads sources of alpha only; a key bound to no project reads any project; nobody reads an archived
# source. ``test_the_table_is_what_explain_says`` checks the table against ``alice_explain``, which fails closed when a
# key may not read a source the memory cites.
_AUTHORIZED_AFTER = {
    "confidential": {"admin"},
    "private": {"trusted", "project", "admin", "unbound"},
    "health": {"trusted", "admin", "unbound"},
    "beta": {"unbound"},
    "archived": set(),
}
_VARIANTS = tuple(_AUTHORIZED_AFTER)


class _Vault:
    """A throwaway SQLite vault, one real key of each kind, and the router modules pointed at it."""

    def __init__(self, context: MCPRuntimeContext, monkeypatch: pytest.MonkeyPatch, *, with_keys: bool = True) -> None:
        self.context = context
        self.monkeypatch = monkeypatch
        self.keys = (
            {name: self._mint(profile, project) for name, (profile, project) in _KEY_SPECS.items()} if with_keys else {}
        )
        # The writers of the doors below: a key of the vault, or the owner when the vault has no key.
        self.writer: str | None = "trusted" if with_keys else None
        self.reviewer: str | None = "admin" if with_keys else None
        self._point_routers_at_the_vault()

    # -- plumbing -----------------------------------------------------------------------------------------------

    def _mint(self, profile: str, project: str | None) -> str:
        path = _sqlite_path_from_url(self.context.database_url)
        with sqlite_user_connection(path, _USER_ID) as conn:
            _record, raw = create_agent_key(
                SQLiteVNextStore(conn, _USER_ID),
                user_id=_USER_ID,
                agent_id=f"{profile}-{project or 'none'}",
                permission_profile=profile,
                project_scope=project,
            )
        return raw

    def _point_routers_at_the_vault(self) -> None:
        """The HTTP routes run on Postgres only, so each router module is given the vault's SQLite store."""

        from alicebot_api.routers import vnext_memories, vnext_retrieval

        path = _sqlite_path_from_url(self.context.database_url)

        @contextmanager
        def connection(_database_url: object, current_user_id: object):  # type: ignore[no-untyped-def]
            with sqlite_user_connection(path, str(current_user_id)) as conn:
                yield conn

        class _Store(SQLiteVNextStore):
            """The SQLite store lacks three methods the source review route calls on Postgres."""

            def create_edge(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                return {}

            def list_artifacts_referencing_source(self, **_kwargs: object) -> list[object]:
                return []

            def delete_source(self, *, source_id: str, actor_type: str = "system") -> dict[str, object]:
                """The archive: the same soft delete the Postgres store does (``deleted_at`` set, the row kept)."""

                row = self.get_source(source_id)
                assert row is not None
                self.conn.execute(
                    "UPDATE sources SET deleted_at = '2026-10-01T00:00:00Z' WHERE id = ? AND user_id = ?",
                    (source_id, _USER_ID),
                )
                return dict(row)

        for module in (vnext_memories, vnext_retrieval):
            self.monkeypatch.setattr(module, "get_settings", lambda: Settings(database_url="postgresql://db"))
            self.monkeypatch.setattr(module, "user_connection", connection)
            self.monkeypatch.setattr(module, "PostgresVNextStore", lambda conn: _Store(conn, _USER_ID))
        self.monkeypatch.setattr(vnext_memories, "_vnext_load_source_trace", lambda **_kwargs: {})

    def wire(self, name: str, arguments: dict[str, object], *, who: str | None) -> dict[str, object]:
        """One ``tools/call`` through the real server object, as a client reads it. ``who`` None is the owner."""

        if who is None:
            os.environ.pop(_KEY_ENV, None)
        else:
            os.environ[_KEY_ENV] = self.keys[who]
        os.environ[MCP_FULL_TOOLS_ENV] = "1"
        try:
            server = mcp_server.MCPServer(context=self.context, input_stream=BytesIO(), output_stream=BytesIO())
            response = server._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            )
        finally:
            os.environ.pop(_KEY_ENV, None)
            os.environ.pop(MCP_FULL_TOOLS_ENV, None)
        assert response is not None
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict)
        return {"is_error": bool(result["isError"]), "payload": payload, "text": result["content"][0]["text"]}

    def sql(self, query: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with sqlite3.connect(_sqlite_path_from_url(self.context.database_url)) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(query, args).fetchall())

    # -- the doors that save a quote ----------------------------------------------------------------------------

    def capture_source(
        self, raw_text: str = _SOURCE_TEXT, title: str = "Alpha pottery log", *, as_owner: bool = False
    ) -> str:
        """Capture a source. By the vault's writer key, so it belongs to project alpha, or as the owner (no key), so it
        belongs to no project."""

        done = self.wire(
            "alice_capture",
            {"raw_text": raw_text, "title": title, "domain": "project", "sensitivity": "internal"},
            who=None if as_owner else self.writer,
        )
        assert done["is_error"] is False, done
        source_id = str(done["payload"]["source_id"])  # type: ignore[index]
        # The quote must reach a memory only through the provenance saved on it, never as the memory's own text.
        assert not self.sql("SELECT id FROM memories WHERE canonical_text LIKE ?", (f"%{_WORD_A}%",))
        return source_id

    def _candidate(self, text: str) -> str:
        """A pending memory of project alpha made by capture, for the review door."""

        done = self.wire(
            "alice_capture",
            {"raw_text": text, "title": text[:24], "domain": "project", "sensitivity": "internal"},
            who=self.writer,
        )
        assert done["is_error"] is False, done
        rows = self.sql("SELECT id, canonical_text FROM memories WHERE status = 'candidate' ORDER BY created_at DESC")
        return str(next(row["id"] for row in rows if text.split(":")[0] in str(row["canonical_text"])))

    def edit_and_approve(self, source_id: str, tag: str = "") -> tuple[str, str]:
        """``metadata_json.provenance`` and the link quote. Returns the memory id and a query that finds it."""

        candidate = self._candidate(f"Parentnote{tag}: the alpha kiln is fired on Mondays")
        done = self.wire(
            "alice_memory_correct",
            {
                "review_item_id": candidate,
                "action": "edit-and-approve",
                "body": {"text": "Kiln schedule: the alpha kiln is fired on Mondays."},
                "provenance": {"source_id": source_id, "quote": _QUOTE},
            },
            who=self.reviewer,
        )
        assert done["is_error"] is False, done
        return candidate, "kiln schedule Mondays"

    def supersede(self, source_id: str, tag: str = "") -> tuple[str, str]:
        """``metadata_json.replacement_provenance`` and the link quote, both on the replacement memory."""

        candidate = self._candidate(f"Supersedenote{tag}: the alpha glaze shelf was reorganised on Friday")
        done = self.wire(
            "alice_memory_correct",
            {
                "review_item_id": candidate,
                "action": "supersede-existing",
                "replacement_title": "Alpha glaze shelf v2",
                "replacement_body": {"text": "The alpha glaze shelf was reorganised on Saturday morning."},
                "replacement_provenance": {"source_id": source_id, "quote": _QUOTE},
                "reason": "moved",
            },
            who=self.reviewer,
        )
        assert done["is_error"] is False, done
        replacement = done["payload"]["replacement_object"]  # type: ignore[index]
        return str(replacement["id"]), "glaze shelf reorganised Saturday"

    def http_commit(self, source_id: str, tag: str = "") -> tuple[str, str]:
        """``agentic_memory.conversation_excerpt``, ``agentic_memory.source_refs``, ``value.source_refs`` and the link
        quote, through ``POST /v0/vnext/memories/commit`` (the MCP tool has no ``conversation_excerpt`` field)."""

        return self.http_commit_citing([source_id], tag=tag)

    def http_commit_citing(self, source_ids: list[str], tag: str = "") -> tuple[str, str]:
        """The same commit with several ``source_refs``: the route saves the one excerpt as the quote of every link."""

        from alicebot_api.routers import vnext_memories as router

        response = router.commit_vnext_memory(
            router.VNextMemoryCommitRequest(
                user_id=UUID(_USER_ID),
                title=f"Cone ten firing note {tag}",
                canonical_text=f"The cone ten firing schedule {tag} is posted on the wall calendar.",
                memory_type="project_fact",
                domain="project",
                sensitivity="internal",
                confidence=0.95,
                source_refs=list(source_ids),
                conversation_excerpt=_QUOTE,
                agent_id=_AGENT_IDS["trusted"],
            ),
            authorization=f"Bearer {self.keys['trusted']}",
        )
        body = json.loads(response.body)
        assert response.status_code == 201, body
        return str(body["memory"]["id"]), f"cone ten firing schedule {tag} wall calendar"

    def _http_commit_without_link(
        self, source_id: str, *, confidence: float, tag: str, excerpt: str = _QUOTE
    ) -> tuple[dict[str, object], str, str]:
        """``POST /v0/vnext/memories/commit`` at a confidence the policy does not accept at once. The row stores
        ``agentic_memory.conversation_excerpt``, ``agentic_memory.source_refs`` and ``value.source_refs``, and no
        provenance link: a link is created only by a commit that is committed at once."""

        from alicebot_api.routers import vnext_memories as router

        response = router.commit_vnext_memory(
            router.VNextMemoryCommitRequest(
                user_id=UUID(_USER_ID),
                title=f"Held cone ten note {tag}",
                canonical_text=f"The held cone ten firing schedule {tag} is posted by the kiln door.",
                memory_type="project_fact",
                domain="project",
                sensitivity="internal",
                confidence=confidence,
                source_refs=[source_id],
                conversation_excerpt=excerpt,
                agent_id=_AGENT_IDS["trusted"],
            ),
            authorization=f"Bearer {self.keys['trusted']}",
        )
        body = json.loads(response.body)
        memory_id = str(body["memory"]["id"])
        assert not self.sql("SELECT id FROM provenance_links WHERE target_id = ?", (memory_id,)), "no link"
        return body, memory_id, f"held cone ten firing schedule {tag} kiln door"

    def held_commit(self, source_id: str, tag: str = "") -> tuple[str, str]:
        """A commit the policy holds for review (confidence under 0.5), then approved by the admin key. No link."""

        body, memory_id, query = self._http_commit_without_link(source_id, confidence=0.4, tag=tag)
        assert body["status"] == "review_required", body
        approved = self.wire(
            "alice_memory_correct", {"review_item_id": memory_id, "action": "approve", "reason": "check"}, who=self.reviewer
        )
        assert approved["is_error"] is False, approved
        assert not self.sql("SELECT id FROM provenance_links WHERE target_id = ?", (memory_id,)), "approval adds no link"
        return memory_id, query

    def confirmed_commit(self, source_id: str, tag: str = "") -> tuple[str, str]:
        """A commit that needs the author's confirmation (confidence under 0.85), confirmed by the same key. No link."""

        body, memory_id, query = self._http_commit_without_link(source_id, confidence=0.7, tag=tag)
        assert body["status"] == "confirmation_required", body
        confirmation = body["confirmation"]
        confirmed = self.wire(
            "alice_memory_commit",
            {"confirmation_id": confirmation["confirmation_id"], "confirmation_action": "confirm"},  # type: ignore[index]
            who="trusted",
        )
        assert confirmed["is_error"] is False, confirmed
        assert not self.sql("SELECT id FROM provenance_links WHERE target_id = ?", (memory_id,)), "confirming adds no link"
        assert self.sql("SELECT status FROM memories WHERE id = ?", (memory_id,))[0]["status"] == "active"
        return memory_id, query

    # -- the change of label ------------------------------------------------------------------------------------

    def reclassify(self, source_id: str, variant: str) -> None:
        """Change the source with the shipped ``review_vnext_source`` handler, as an owner does from the workspace."""

        from alicebot_api.routers import vnext_memories as router

        fields: dict[str, object] = {
            "confidential": {"action": "update", "sensitivity": "confidential"},
            "private": {"action": "update", "sensitivity": "private"},
            "health": {"action": "update", "domain": "health"},
            "beta": {"action": "assign_project", "project_id": "beta"},
            "archived": {"action": "archive"},
        }[variant]  # type: ignore[assignment]
        response = router.review_vnext_source(
            UUID(source_id), router.VNextSourceReviewRequest(user_id=UUID(_USER_ID), **fields)  # type: ignore[arg-type]
        )
        assert response.status_code == 200, response.body
        row = self.sql("SELECT sensitivity, domain, deleted_at, metadata_json FROM sources WHERE id = ?", (source_id,))[0]
        expected = {
            "confidential": row["sensitivity"] == "confidential",
            "private": row["sensitivity"] == "private",
            "health": row["domain"] == "health",
            "beta": '"beta"' in str(row["metadata_json"]),
            "archived": row["deleted_at"] is not None,
        }[variant]
        assert expected, dict(row)

    # -- the readers --------------------------------------------------------------------------------------------

    def review(self, who: str | None, memory_id: str) -> dict[str, object]:
        return self.wire("alice_memory_review", {"review_item_id": memory_id}, who=who)

    def pack(self, who: str | None, query: str) -> dict[str, object]:
        return self.wire("alice_context_pack", {"query": query, "max_tokens": 4000}, who=who)

    def pack_deep(self, who: str | None, query: str) -> dict[str, object]:
        """The same pack at the deepest tier with the trace on and the source section off, so every section the pack
        has (the supersession context, the contradictions, the recent changes, the debug trace) is searched."""

        return self.wire(
            "alice_context_pack",
            {"query": query, "max_tokens": 4000, "context_depth": "high", "debug": True, "include_sources": False},
            who=who,
        )

    def http_pack(self, who: str | None, query: str, **options: object) -> dict[str, object]:
        """``POST /v0/vnext/context-packs``: the full memory rows, with ``metadata_json``."""

        from alicebot_api.routers import vnext_retrieval as router

        response = router.create_vnext_context_pack(
            router.VNextContextPackRequest(
                user_id=UUID(_USER_ID),
                query=query,
                options={"max_tokens": 4000, **options},
                agent_id=_AGENT_IDS[who] if who else None,
            ),
            authorization=f"Bearer {self.keys[who]}" if who else None,
        )
        text = response.body.decode()
        return {"is_error": response.status_code >= 400, "payload": json.loads(text), "text": text}

    def explain(self, who: str, memory_id: str) -> dict[str, object]:
        return self.wire("alice_explain", {"memory_id": memory_id}, who=who)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Vault:
    monkeypatch.delenv(_KEY_ENV, raising=False)
    monkeypatch.delenv(MCP_FULL_TOOLS_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    return _Vault(MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID)), monkeypatch)


_DOORS = ("edit_and_approve", "supersede", "http_commit")


def _make(vault: _Vault, door: str, source_id: str, *, tag: str = "") -> tuple[str, str]:
    return getattr(vault, door)(source_id, tag=tag)


def _holds_quote(answer: dict[str, object]) -> bool:
    """Whether any byte of the quote is in the serialized answer, whatever field it is in.

    The one section left out is the context pack's ``sources``: those are the source's own excerpts, read by the source
    search and fenced there, not the quote the memory saved. That reader skips the domain test for a key that names no
    domains, which is a separate, named gap (``test_the_pack_sources_section_is_a_separate_reader``).
    """

    payload = answer["payload"]
    if isinstance(payload, dict) and "sources" in payload:
        payload = {key: value for key, value in payload.items() if key != "sources"}
    text = json.dumps(payload)
    return _WORD_A in text or _WORD_B in text


def _readers(vault: _Vault, who: str | None, memory_id: str, query: str) -> dict[str, dict[str, object]]:
    readers = {
        "review": vault.review(who, memory_id),
        "pack": vault.pack(who, query),
        "pack_deep": vault.pack_deep(who, query),
    }
    if who is not None:
        # The HTTP route refuses a call with no key once the vault has any key, so the owner's call is made in a vault
        # with none (``test_the_owner_is_shown_what_was_stored_over_http``).
        readers["http_pack"] = vault.http_pack(who, query)
        readers["http_pack_deep"] = vault.http_pack(
            who, query, context_depth="high", include_sources=False, include_contradictions=True
        )
    return readers


def _pack_holds_the_memory(answer: dict[str, object], memory_id: str, *, http: bool) -> bool:
    payload = answer["payload"]
    rows = payload["relevant_memories" if http else "memories"]  # type: ignore[index]
    return memory_id in [str(row["id"]) for row in rows]


def _is_http(surface: str) -> bool:
    return surface.startswith("http")


# -- 1. the lifecycle -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("door", _DOORS)
def test_every_reader_carries_the_saved_quote_before_the_label_changes(vault: _Vault, door: str) -> None:
    """The control for the tests below. Before any change of label, every reader returns the quote to every key and to
    the owner, through every door that saves one, so a reader that is silent afterwards withheld it. The link and the
    metadata copy are both there: the review response holds the token twice for the doors that save a metadata copy.

    Mutation: none to make; this is the baseline that gives the others their meaning. The ``read_only`` key is the one
    that cannot call a write verb, and it reads all three surfaces here.
    """

    source_id = vault.capture_source()
    memory_id, query = _make(vault, door, source_id)
    for who in (*_KEY_SPECS, None):
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert answer["is_error"] is False, (who, surface, answer["text"][:300])  # type: ignore[index]
            if surface != "review":
                assert _pack_holds_the_memory(answer, memory_id, http=_is_http(surface)), (who, surface)
            assert _holds_quote(answer), (door, who, surface)
    review = vault.review("trusted", memory_id)
    assert str(review["text"]).count(_WORD_A) >= 2, "the link and the memory's own copy"


@pytest.mark.parametrize("variant", _VARIANTS)
@pytest.mark.parametrize("door", _DOORS)
def test_a_reader_withholds_the_quote_of_a_source_the_caller_may_no_longer_read(
    vault: _Vault, door: str, variant: str
) -> None:
    """The lifecycle of the audit. A memory saves a quote of a source, the source is reclassified (sensitivity
    raised to confidential or private, domain changed to health, project moved to ``beta``, or archived), and the
    memory is read by each kind of key. A key that may no longer read the source gets no byte of the quote from
    ``alice_memory_review`` by id, from ``alice_context_pack`` or from the full rows of ``POST /v0/vnext/context-packs``,
    and the memory itself is still returned, so the quote is gone and not the memory. A key that may read the source
    keeps every copy. The owner, a call with no agent key, is shown what was stored, including after the archive.

    On v0.20.0 and main before this change every non-admin key read the quote in the review response (two copies, the
    link and ``metadata_json``) and the pack (``supporting_evidence``, and the metadata of the HTTP rows), and for an
    archived source every key read it, the admin key included.

    Mutations, each alone: in ``_vnext_memory_review`` (``mcp/review.py``) return ``store.list_provenance_links(...)``
    unfiltered (the link copy); pass ``memory`` and not ``saved.memory(memory)`` to ``frame_disclosed_tree`` (the
    metadata copy); in ``_supporting_evidence`` (``vnext_retrieval.py``) remove the ``admits_link`` test (the pack);
    remove the ``saved_provenance.memories(...)`` line of ``compile_context_pack`` (the HTTP rows); make
    ``SavedProvenanceReader._judge`` mark every source admitted (everything); make ``SourceReadFence.admits`` skip
    the ``deleted_at`` test is not a mutation of the reader, the store leaves an archived row out before it asks.
    """

    source_id = vault.capture_source()
    memory_id, query = _make(vault, door, source_id)
    vault.reclassify(source_id, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert answer["is_error"] is False, (who, surface, str(answer["text"])[:300])
            if surface != "review":
                assert _pack_holds_the_memory(answer, memory_id, http=_is_http(surface)), (
                    "the memory stays visible, only its quote is withheld",
                    door,
                    variant,
                    who,
                    surface,
                )
            assert _holds_quote(answer) is (who in authorized), (door, variant, who, surface)
    for surface, answer in _readers(vault, None, memory_id, query).items():
        assert _holds_quote(answer), ("the owner is not fenced", door, variant, surface)


@pytest.mark.parametrize("door", _DOORS)
def test_a_reader_who_may_read_every_source_is_shown_exactly_what_the_owner_is_shown(vault: _Vault, door: str) -> None:
    """Authorized readers are unchanged. While the source is readable, the review of every kind of key is byte for byte
    the owner's. The pack depends on the scope of the caller (a pack with a project scope leaves out the ``event_time``
    that an unscoped pack adds from the source dates, which it always did), so the memories and the supporting
    evidence of a key bound to ``alpha`` are compared with those of the other keys bound to ``alpha``, and those of the
    key bound to no project with the owner's. The comparison is also run for the admin key after the source is made
    confidential, the one change of label that key may still read.

    The same comparison was run once against the code before this change on the same vault file (see the CHANGELOG
    entry), so "the owner's answer" here is also what v0.20.0 returned.

    Mutation: make ``_memory_without_refused_provenance`` drop a key it should keep (for example ``out.pop("value",
    None)``) and return the copy even when nothing is refused, or make ``SavedProvenanceReader.links`` return the links
    in reverse order: the review of the key differs from the owner's.
    """

    source_id = vault.capture_source()
    memory_id, query = _make(vault, door, source_id)

    def review(who: str | None) -> object:
        return vault.review(who, memory_id)["payload"]

    def pack(who: str | None) -> tuple[object, object]:
        payload = vault.pack(who, query)["payload"]
        return payload["memories"], payload["supporting_evidence"]  # type: ignore[index]

    owner_review, owner_pack = review(None), pack(None)
    assert _WORD_A in json.dumps(owner_review), "the control: the owner's review carries the quote"
    assert _WORD_A in json.dumps(owner_pack), "the control: the owner's pack carries the quote"
    scoped_pack = pack("trusted")
    assert _WORD_A in json.dumps(scoped_pack)
    for who in _KEY_SPECS:
        assert review(who) == owner_review, (door, who)
        assert pack(who) == (owner_pack if who == "unbound" else scoped_pack), (door, who)
    vault.reclassify(source_id, "confidential")
    assert review(None) == owner_review and pack(None) == owner_pack
    assert review("admin") == owner_review and pack("admin") == scoped_pack, door


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_table_is_what_explain_says(vault: _Vault, variant: str) -> None:
    """The expectations above come from the policy of each profile. ``alice_explain`` applies the same test to every
    source a memory cites and fails closed for a key that may not read one, and it was never changed, so it is an
    independent reading of the same rule. For every key and every change of label, explain succeeds exactly when the
    table says the key may still read the source.

    Mutation: any edit of ``_AUTHORIZED_AFTER`` fails this test and the lifecycle test together, which is the point.
    """

    source_id = vault.capture_source()
    memory_id, _query = vault.edit_and_approve(source_id)
    vault.reclassify(source_id, variant)
    for who in _KEY_SPECS:
        assert (vault.explain(who, memory_id)["is_error"] is False) is (who in _AUTHORIZED_AFTER[variant]), (variant, who)


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_the_owner_is_shown_what_was_stored_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """The owner, a call with no agent key, is not fenced: after the source is made confidential or archived the owner
    still reads every saved quote from the review, the MCP pack and ``POST /v0/vnext/context-packs`` (a vault with no key
    at all, since the HTTP route refuses a keyless call once any key exists). Authorized callers are covered by the
    lifecycle test above, which gives each key what its own permission allows.

    Mutation: make ``SourceReadFence.fenced`` return ``True`` for every fence (``return True``), so the owner is
    withheld from like a key bound to a project: the review, the pack and the HTTP pack lose the quote.
    """

    monkeypatch.delenv(_KEY_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    vault = _Vault(
        MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID)),
        monkeypatch,
        with_keys=False,
    )
    source_id = vault.capture_source()
    memory_id, query = vault.edit_and_approve(source_id)
    vault.reclassify(source_id, variant)
    for surface, answer in {
        "review": vault.review(None, memory_id),
        "pack": vault.pack(None, query),
        "http_pack": vault.http_pack(None, query),
    }.items():
        assert answer["is_error"] is False, (surface, str(answer["text"])[:300])
        assert _holds_quote(answer), (variant, surface)


# -- 2. the id goes with the quote ------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_the_id_of_a_source_the_caller_may_not_read_is_withheld_with_its_quote(vault: _Vault, variant: str) -> None:
    """A commit through ``alice_memory_commit`` keeps the ids it cited in more places than the link: in
    ``metadata_json.agentic_memory.source_refs``, in ``value.source_refs`` and in the ``new_value`` of its revision. On
    v0.20.0 a key that could not read the source still read the id from all of them and from the link row, in
    ``alice_memory_review`` by id and in the pack's ``supporting_evidence``. Now the link row, the entry of every ref list
    and the pack row are withheld, the same way a link that was never stored would be, and the memory is still returned.
    The admin key (confidential) keeps the id wherever it was.

    Mutations, each alone, in ``vnext_source_fence.py``: drop the ``_SOURCE_REFS_KEY`` filter of ``_memory_without_refused_provenance``
    (the id stays in ``agentic_memory.source_refs`` and ``value.source_refs``); drop the ``revision`` method's filter or
    stop calling ``saved.revision`` in ``_vnext_memory_review`` (the id stays in the revision's ``new_value``).
    """

    source_id = vault.capture_source()
    made = vault.wire(
        "alice_memory_commit",
        {
            "title": "Cone ten firing schedule",
            "canonical_text": "The cone ten firing schedule is posted on the wall calendar.",
            "memory_type": "project_fact",
            "domain": "project",
            "sensitivity": "internal",
            "source_refs": [source_id],
        },
        who="trusted",
    )
    assert made["is_error"] is False, made
    memory_id = str(made["payload"]["memory"]["id"])  # type: ignore[index]
    query = "cone ten firing schedule wall calendar"

    def ids_seen(answer: dict[str, object]) -> bool:
        payload = answer["payload"]
        if isinstance(payload, dict) and "sources" in payload:
            payload = {key: value for key, value in payload.items() if key != "sources"}
        return source_id in json.dumps(payload)

    for who in _KEY_SPECS:
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert ids_seen(answer), ("the control: before the change every key reads the id", who, surface)
    vault.reclassify(source_id, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert answer["is_error"] is False, (who, surface)
            assert ids_seen(answer) is (who in authorized), (variant, who, surface)


# -- 3. the pack's own source section is a different reader -----------------------------------------------------


@pytest.mark.parametrize(("variant", "excerpt_is_returned"), [("confidential", False), ("health", True)])
def test_the_pack_sources_section_is_a_separate_reader(vault: _Vault, variant: str, excerpt_is_returned: bool) -> None:
    """Pins the limit the release notes state, so the words and the behaviour cannot drift apart. The pack's ``sources``
    section is the source's own excerpt, read by the source search, which holds a source to the project of the key, to
    the sensitivity ceiling and to deletion, but not to the domains of a key that names none. So after a source is made
    ``health`` the project scoped key's pack still carries the source's excerpt, while the quote the memory saved is
    withheld (``supporting_evidence`` and the memory rows hold none of it). After the source is made confidential the
    source search leaves the source out, which is the control that this section does hold the sensitivity fence.

    Delete the ``health`` row of this test when the source search applies the domain test, and change the
    known-limitations line with it.

    Mutation: none in this change. Make the source search honour the domains of the caller and the ``health`` row
    fails, which is the signal to delete it.
    """

    source_id = vault.capture_source()
    memory_id, query = vault.edit_and_approve(source_id)
    vault.reclassify(source_id, variant)
    pack = vault.pack("project", query)
    assert not _holds_quote(pack), "the quote the memory saved is withheld"
    assert [str(row["id"]) for row in pack["payload"]["memories"]] == [memory_id]  # type: ignore[index]
    excerpts = [str(row.get("excerpt")) for row in pack["payload"]["sources"]]  # type: ignore[index]
    assert any(_WORD_A in excerpt for excerpt in excerpts) is excerpt_is_returned, variant


# -- 4. the verbs that hand a memory row back -------------------------------------------------------------------


_ACTORS = ("trusted", "project", "unbound", "admin")


def _manage(vault: _Vault, who: str, verb: str, memory_id: str) -> dict[str, object]:
    arguments: dict[str, object] = {"action": verb, "memory_id": memory_id, "reason": "check"}
    return vault.wire("alice_memory_manage", arguments, who=who)


@pytest.mark.parametrize("verb", ("expire", "unexpire", "undo", "forget"))
@pytest.mark.parametrize("door", ("edit_and_approve", "http_commit"))
def test_a_verb_returns_the_row_with_the_saved_quote_before_the_label_changes(
    vault: _Vault, verb: str, door: str
) -> None:
    """The control for the verb test below: while the caller may read the source, the row that ``alice_memory_manage``
    hands back carries the metadata copy of the quote, for each verb and for each door that saves one. So an absent
    quote afterwards was withheld by the verb's reader and not missing from its row.

    Mutation: none to make.
    """

    source_id = vault.capture_source()
    memory_id, _query = _make(vault, door, source_id)
    if verb == "unexpire":
        assert _manage(vault, "trusted", "expire", memory_id)["is_error"] is False
    answer = _manage(vault, "trusted", verb, memory_id)
    assert answer["is_error"] is False, answer
    assert _holds_quote(answer), (door, verb)


@pytest.mark.parametrize("variant", ("confidential", "archived"))
@pytest.mark.parametrize("verb", ("expire", "unexpire", "undo", "forget"))
@pytest.mark.parametrize("door", ("edit_and_approve", "http_commit"))
def test_a_verb_hands_back_the_row_without_the_quote_of_a_source_the_caller_may_not_read(
    vault: _Vault, door: str, verb: str, variant: str
) -> None:
    """``alice_memory_manage`` expire, unexpire, undo and forget (and the legacy tools and the HTTP routes that call the
    same ``VNextMemoryCommitService`` verbs) return the row they changed, and the row carries the metadata copy of the
    quote. After the source is made confidential or archived, a key that may not read it gets the row without the quote
    and the verb still does its work: the memory is expired, reopened, undone or forgotten as before. The admin key keeps the quote
    for a confidential source and loses it for an archived one. One memory per key, so each key's call acts on its own
    row and a retired row does not stop the next key.

    On v0.20.0 the trusted, project scoped and unbound keys read the quote from the row of every verb.

    Mutation: remove the ``_held_to_the_callers_read_fence`` decorator from the verb under test in
    ``vnext_memory_commit.py`` (``expire``, ``unexpire``, ``undo`` or ``forget``): that verb's rows leak. Make the
    decorator read ``identity`` as ``None``: every verb leaks.
    """

    source_id = vault.capture_source()
    memories = {who: _make(vault, door, source_id, tag=who)[0] for who in _ACTORS}
    if verb == "unexpire":
        for who in _ACTORS:
            assert _manage(vault, who, "expire", memories[who])["is_error"] is False
    vault.reclassify(source_id, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _ACTORS:
        answer = _manage(vault, who, verb, memories[who])
        assert answer["is_error"] is False, (who, answer)
        assert _holds_quote(answer) is (who in authorized), (door, verb, variant, who)
        assert answer["payload"]["memory"]["id"] == memories[who]  # type: ignore[index]
    states = {
        who: str(vault.sql("SELECT status, valid_to FROM memories WHERE id = ?", (memories[who],))[0]["status"])
        for who in _ACTORS
    }
    if verb in {"undo", "forget"}:
        assert set(states.values()) <= {"archived", "rejected", "superseded", "undone", "forgotten"}, states
    else:
        assert set(states.values()) == {"active"}, states


@pytest.mark.parametrize("who", ("trusted", "project"))
def test_the_http_verb_routes_hand_back_the_row_without_the_quote(vault: _Vault, who: str) -> None:
    """``POST /v0/vnext/memories/expire``, ``unexpire`` and ``undo`` return the same rows over HTTP, with the same fence:
    a key that may not read the confidential source gets the row without the quote.

    Mutation: remove the ``_held_to_the_callers_read_fence`` decorator from ``expire`` (the expire route leaks), or from
    ``undo`` (the undo route leaks).
    """

    from alicebot_api.routers import vnext_memories as router

    source_id = vault.capture_source()
    memories = {name: vault.http_commit(source_id, tag=name)[0] for name in ("expire", "undo")}
    vault.reclassify(source_id, "confidential")
    auth = f"Bearer {vault.keys[who]}"
    expired = router.expire_vnext_memory(
        router.VNextMemoryExpireRequest(
            user_id=UUID(_USER_ID), memory_id=UUID(memories["expire"]), reason="check", agent_id=_AGENT_IDS[who]
        ),
        authorization=auth,
    )
    assert expired.status_code == 200, expired.body
    assert not _holds_quote({"payload": json.loads(expired.body)})
    undone = router.undo_vnext_memory(
        router.VNextMemoryUndoRequest(
            user_id=UUID(_USER_ID), memory_id=UUID(memories["undo"]), reason="check", agent_id=_AGENT_IDS[who]
        ),
        authorization=auth,
    )
    assert undone.status_code == 200, undone.body
    assert not _holds_quote({"payload": json.loads(undone.body)})


# -- 5. a review that returns the row it changed ----------------------------------------------------------------


def _plant_saved_quote(vault: _Vault, memory_id: str, source_id: str) -> None:
    """Give a pending memory the two copies of a quote that an earlier write saved on it: the metadata copy and a
    link, as a row written before the source was reclassified holds them."""

    row = vault.sql("SELECT metadata_json FROM memories WHERE id = ?", (memory_id,))[0]
    metadata = json.loads(row["metadata_json"])
    metadata["provenance"] = {"source_id": source_id, "quote": _QUOTE, "evidence_role": "supports", "confidence": 0.8}
    vault.sql("UPDATE memories SET metadata_json = ? WHERE id = ?", (json.dumps(metadata), memory_id))
    path = _sqlite_path_from_url(vault.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        SQLiteVNextStore(conn, _USER_ID).create_provenance_link(
            {
                "target_type": "memory",
                "target_id": memory_id,
                "source_id": source_id,
                "quote": _QUOTE,
                "evidence_role": "supports",
                "confidence": 0.8,
            },
            actor_type="agent",
        )
    # sqlite3 opened above autocommits the UPDATE; the planted row is the one the readers below see.
    assert _WORD_A in str(vault.sql("SELECT metadata_json FROM memories WHERE id = ?", (memory_id,))[0][0])


@pytest.mark.parametrize("variant", ("confidential", "archived"))
@pytest.mark.parametrize("action", ("approve", "reject", "supersede-existing"))
def test_alice_memory_correct_hands_back_the_row_without_the_quote_of_a_source_the_admin_may_not_read(
    vault: _Vault, action: str, variant: str
) -> None:
    """``alice_memory_correct`` (admin keys only) returns the memory it reviewed, with its metadata. A pending memory that
    already holds a saved quote, reviewed after the source was reclassified, came back with the quote on v0.20.0, even
    for an archived source that no key may read. Now the admin key keeps the quote of a confidential source it may read
    and loses the quote of an archived one, and the review itself still happens.

    The row was written before the reclassification, so the quote is planted by the same two writes a review makes
    (the metadata copy and a link), with SQL, as the audit did for rows saved before a fix.

    Mutation: replace ``saved.tree(...)`` in ``_vnext_memory_correct`` (``mcp/review.py``) with the unfiltered rows
    (``shown = {"memory": updated, "replacement_object": replacement_object}``).
    """

    source_id = vault.capture_source()
    candidate = vault._candidate(f"Plantednote{action[:3]}: the alpha kiln shelf is cleaned on Fridays")
    _plant_saved_quote(vault, candidate, source_id)
    vault.reclassify(source_id, variant)
    arguments: dict[str, object] = {"review_item_id": candidate, "action": action, "reason": "check"}
    if action == "supersede-existing":
        arguments.update(
            replacement_title="Alpha kiln shelf v2",
            replacement_body={"text": "The alpha kiln shelf is cleaned on Saturdays."},
        )
    answer = vault.wire("alice_memory_correct", arguments, who="admin")
    assert answer["is_error"] is False, answer
    assert answer["payload"]["memory"]["id"] == candidate  # type: ignore[index]
    assert _holds_quote(answer) is (variant == "confidential"), (action, variant)
    status = vault.sql("SELECT status FROM memories WHERE id = ?", (candidate,))[0]["status"]
    assert status == {"approve": "active", "reject": "rejected", "supersede-existing": "superseded"}[action]


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_the_http_review_route_hands_back_the_row_without_the_quote_of_a_source_the_admin_may_not_read(
    vault: _Vault, variant: str
) -> None:
    """``POST /v0/vnext/memories/{id}/review`` returns ``{"memory": row}`` to a key. The same planted row is accepted by
    the admin key after the source is made confidential (the quote stays: the key may read the source) or archived (the
    quote is withheld).

    Mutation: remove the ``SavedProvenanceReader(...).memory(updated)`` line of ``review_vnext_memory``
    (``routers/vnext_memories.py``).
    """

    from alicebot_api.routers import vnext_memories as router

    source_id = vault.capture_source()
    candidate = vault._candidate("Httpreviewnote: the alpha kiln shelf is cleaned on Fridays")
    _plant_saved_quote(vault, candidate, source_id)
    vault.reclassify(source_id, variant)
    response = router.review_vnext_memory(
        UUID(candidate),
        router.VNextMemoryReviewRequest(user_id=UUID(_USER_ID), action="accept", agent_id=_AGENT_IDS["admin"]),
        authorization=f"Bearer {vault.keys['admin']}",
    )
    assert response.status_code == 200, response.body
    answer = {"payload": json.loads(response.body)}
    assert answer["payload"]["memory"]["id"] == candidate
    assert _holds_quote(answer) is (variant == "confidential")


# -- 6. a memory with no provenance link ------------------------------------------------------------------------


def _holds_source_id(answer: dict[str, object], source_id: str) -> bool:
    """Whether the id of the source is anywhere in the serialized answer, leaving out the pack's ``sources`` section
    (the source's own row, read by the source search and fenced there)."""

    payload = answer["payload"]
    if isinstance(payload, dict) and "sources" in payload:
        payload = {key: value for key, value in payload.items() if key != "sources"}
    return source_id in json.dumps(payload)


_LINKLESS_DOORS = ("held_commit", "confirmed_commit")
# The surfaces that carry the copy of the quote that a memory keeps in its own metadata. The MCP pack carries only the
# evidence of a link, so for a memory with no link it has nothing to withhold and nothing to leak.
_COPY_SURFACES = ("review", "http_pack", "http_pack_deep")
# The review returns the source id in the memory's ref lists. The HTTP pack's own scope pass has already dropped the
# refs it cannot prove from each row (for the authorized key too, which is why the rows must be judged before it), and
# the pack's trace names the source it selected, so the id is not a sign of the memory's copies there and only the
# quote is checked.
_ID_SURFACES = ("review",)


@pytest.mark.parametrize("door", _LINKLESS_DOORS)
def test_a_memory_with_no_link_carries_its_quote_and_its_source_id_in_its_own_copies_before_the_label_changes(
    vault: _Vault, door: str
) -> None:
    """The control for the lifecycle below. A commit that is not accepted at once (held for review at a confidence under
    0.5 and then approved by an admin key, or confirmed by its author at a confidence under 0.85) saves no provenance
    link: the quote and the id of its source are only in the copies on the memory (``agentic_memory.conversation_excerpt``,
    ``agentic_memory.source_refs``, ``value.source_refs`` and the revision). Every key reads them there, in the review by
    id and in the full rows of the HTTP pack, and ``alice_explain`` returns them too. The MCP pack carries none of them.

    Mutation: none to make; this is the baseline that gives the lifecycle test its meaning.
    """

    source_id = vault.capture_source()
    memory_id, query = getattr(vault, door)(source_id)
    for who in _KEY_SPECS:
        answers = _readers(vault, who, memory_id, query)
        for surface, answer in answers.items():
            assert answer["is_error"] is False, (who, surface, str(answer["text"])[:300])
            assert _holds_quote(answer) is (surface in _COPY_SURFACES), (door, who, surface)
            if surface in _ID_SURFACES:
                assert _holds_source_id(answer, source_id), (door, who, surface)
            if surface != "review":
                assert _pack_holds_the_memory(answer, memory_id, http=_is_http(surface)), (who, surface)
        explained = vault.explain(who, memory_id)
        assert explained["is_error"] is False and _holds_quote(explained), (door, who)
    assert _holds_quote(vault.review(None, memory_id)), "the owner"


@pytest.mark.parametrize("variant", _VARIANTS)
@pytest.mark.parametrize("door", _LINKLESS_DOORS)
def test_the_copies_of_a_memory_with_no_link_follow_its_source_when_the_source_is_reclassified(
    vault: _Vault, door: str, variant: str
) -> None:
    """The finding of the second review of this change, as a lifecycle. The memory has no link, so its copies of the
    quote and of the source id are the only thing that ties it to the source. The source is made confidential or private,
    its domain is changed to health, it is moved to project ``beta`` or it is archived, and each key reads the memory.

    On the first version of this change every restricted key still got the quote from the full rows of
    ``POST /v0/vnext/context-packs`` (``relevant_memories[].metadata_json``): the pack's scope pass drops the refs it
    cannot prove from each row before the rows were judged, so the reader found no refused source on a row that had no
    link, and kept the quote. ``alice_explain`` returned the memory row, the revision and the event payload to every
    restricted key, because it authorized only the sources a link names. Now a key that may not read the source gets no
    byte of the quote and no source id from the review, the HTTP pack or its deep tier, and explain is refused as it is
    for a memory with a link; a key that may read the source gets what it did before, and the memory is returned in
    every pack.

    Mutations, each alone: in ``compile_context_pack`` (``vnext_retrieval.py``) move the ``saved_provenance.memories(...)``
    line below the ``_sanitize_memory_scope_references`` call (the HTTP rows keep the quote for every key bound to a
    project); in ``SavedProvenanceReader._memory`` drop ``| _source_ids_named_by_memory_copies(row)`` from ``named``
    (the review and the HTTP rows keep the quote for every key); make ``source_ids_named_by_memory_audit`` return an
    empty set (explain succeeds for every key).
    """

    source_id = vault.capture_source()
    memory_id, query = getattr(vault, door)(source_id)
    vault.reclassify(source_id, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in _KEY_SPECS:
        for surface, answer in _readers(vault, who, memory_id, query).items():
            assert answer["is_error"] is False, (who, surface, str(answer["text"])[:300])
            if surface != "review":
                assert _pack_holds_the_memory(answer, memory_id, http=_is_http(surface)), (
                    "the memory stays visible, only its copies are withheld",
                    door,
                    variant,
                    who,
                    surface,
                )
            assert _holds_quote(answer) is (who in authorized and surface in _COPY_SURFACES), (
                door,
                variant,
                who,
                surface,
            )
            if surface in _ID_SURFACES:
                assert _holds_source_id(answer, source_id) is (who in authorized), (door, variant, who, surface)
        explained = vault.explain(who, memory_id)
        assert (explained["is_error"] is False) is (who in authorized), (door, variant, who)
        if who in authorized:
            assert _holds_quote(explained)
        else:
            assert source_id not in str(explained["text"]) and _WORD_A not in str(explained["text"])
    for surface, answer in _readers(vault, None, memory_id, query).items():
        assert (_holds_quote(answer)) is (surface in _COPY_SURFACES), ("the owner is not fenced", door, variant, surface)


@pytest.mark.parametrize("door", _LINKLESS_DOORS)
def test_a_reader_who_may_read_the_source_is_shown_the_same_rows_for_a_memory_with_no_link(
    vault: _Vault, door: str
) -> None:
    """Authorized callers are unchanged by judging the rows first. While the source is readable, the full rows of the
    HTTP pack of a key bound to ``alpha`` are those of the other keys bound to ``alpha``, and carry the copies; after the
    source is made confidential the admin key (which may read it) still gets the same rows.

    Mutation: in ``SavedProvenanceReader._memory`` return ``_memory_without_refused_provenance(row, refused=refused,
    withhold_quotes=True)`` even when nothing is refused (it must hand back the row itself): every key's rows lose the
    copies, and the rows no longer carry the quote before the change.
    """

    source_id = vault.capture_source()
    memory_id, query = getattr(vault, door)(source_id)

    def rows(who: str) -> object:
        payload = vault.http_pack(who, query)["payload"]
        return [row for row in payload["relevant_memories"] if row["id"] == memory_id]  # type: ignore[index]

    trusted_rows = rows("trusted")
    assert trusted_rows and _WORD_A in json.dumps(trusted_rows)
    for who in ("project", "admin", "read_only"):
        assert rows(who) == trusted_rows, who
    vault.reclassify(source_id, "confidential")
    assert rows("admin") == trusted_rows
    assert _WORD_A not in json.dumps(rows("trusted"))


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_the_quote_on_the_link_to_a_readable_source_is_withheld_when_a_sibling_link_is_refused(
    vault: _Vault, variant: str
) -> None:
    """The commit route saves one ``conversation_excerpt`` as the quote of every link it makes. A memory committed with
    two sources, where the excerpt came from the first, has two links with the same bytes. When the first source is
    made confidential or archived, its link is left out and the memory's copies are withheld, and the link to the second
    source (which the caller may still read) used to be returned with the same quote bytes in ``alice_memory_review`` and
    in ``supporting_evidence``. Now the link to the readable source is still shown (its id and role), without the quote.
    The admin key keeps both links and both quotes for a confidential source.

    Mutation: in ``SavedProvenanceReader._shown`` (``vnext_source_fence.py``) return ``link`` whenever it is admitted,
    without looking at the other links of the memory (the quote stays on the second link, in the review and the pack).
    """

    refused = vault.capture_source()
    kept = vault.capture_source("Alpha second log. Operator note: glaze shelf inventory nine jars.", "Alpha second log")
    memory_id, query = vault.http_commit_citing([refused, kept])
    vault.reclassify(refused, variant)
    authorized = _AUTHORIZED_AFTER[variant]

    def link_rows(answer: dict[str, object]) -> list[dict[str, object]]:
        payload = answer["payload"]
        if "review" in payload:  # type: ignore[operator]
            return payload["review"]["provenance_links"]  # type: ignore[index,no-any-return]
        return [row for row in payload["supporting_evidence"] if row["target_id"] == memory_id]  # type: ignore[index]

    for who in _KEY_SPECS:
        for surface in ("review", "pack", "http_pack"):
            answer = _readers(vault, who, memory_id, query)[surface]
            links = link_rows(answer)
            ids = {str(row["source_id"]) for row in links}
            if who in authorized:
                assert ids == {refused, kept} and all(_WORD_A in str(row["quote"]) for row in links), (who, surface)
            else:
                assert ids == {kept}, ("the link to the readable source stays, the other is left out", who, surface)
                assert all(row["quote"] is None for row in links), (variant, who, surface)
                assert not _holds_quote(answer), (variant, who, surface)
                assert refused not in json.dumps(answer["payload"]), (variant, who, surface)


@pytest.mark.parametrize("variant", ("confidential", "archived"))
def test_a_quote_that_says_something_else_stays_on_a_link_when_another_source_is_refused(
    vault: _Vault, variant: str
) -> None:
    """The control for the test above, from the review flow people use every day. A memory approved from a captured
    candidate with a ``provenance`` has two links: ``quoted_from``, from the candidate to the source it was captured
    from, with the candidate's own sentence as the quote, and ``supports``, to the source the review cited, with the
    reviewer's quote. When the cited source is made confidential or archived, the ``supports`` link is left out and the
    ``quoted_from`` link keeps its quote, because it does not say the refused source's text. Withholding every quote of a
    memory whenever one of its sources is refused would also take this one.

    Mutation: in ``SavedProvenanceReader._shown`` withhold the quote when any sibling link is left out (replace the
    comparison ``_quote_text(other.get(_QUOTE_KEY)) == quote`` with ``True``).
    """

    source_id = vault.capture_source()
    memory_id, query = vault.edit_and_approve(source_id)
    links = vault.sql("SELECT evidence_role, source_id, quote FROM provenance_links WHERE target_id = ?", (memory_id,))
    assert sorted(row["evidence_role"] for row in links) == ["quoted_from", "supports"]
    own_quote = next(str(row["quote"]) for row in links if row["evidence_role"] == "quoted_from")
    assert own_quote and _WORD_A not in own_quote
    vault.reclassify(source_id, variant)
    authorized = _AUTHORIZED_AFTER[variant]
    for who in ("trusted", "project", "read_only", "unbound", "admin"):
        for surface in ("review", "pack"):
            answer = _readers(vault, who, memory_id, query)[surface]
            payload = answer["payload"]
            rows = (
                payload["review"]["provenance_links"]  # type: ignore[index]
                if surface == "review"
                else [row for row in payload["supporting_evidence"] if row["target_id"] == memory_id]  # type: ignore[index]
            )
            roles = {row["evidence_role"]: row["quote"] for row in rows}
            assert own_quote in str(roles.get("quoted_from")), ("the candidate's own quote stays", variant, who, surface)
            assert ("supports" in roles) is (who in authorized), (variant, who, surface)


def test_a_source_with_no_project_is_outside_the_fence_of_a_key_bound_to_a_project(vault: _Vault) -> None:
    """A source the owner captured belongs to no project. A key bound to a project is blocked on a source with no project
    (``require_explicit_project_scope``), so after this change ``alice_memory_review`` by id leaves out the link, the
    quote and the source id of a memory that cites it for every key bound to a project, the admin key included, which is
    what ``alice_explain`` has always done for such a key. A key bound to no project reads it. On v0.20.0 the review
    returned all three to a key bound to a project. The release notes say this in one sentence.

    The memory is planted with the two writes a review makes (the metadata copy and a link), as the lifecycle tests of the
    review door do.

    Mutation: pass ``require_explicit_project_scope=False`` in ``SourceReadFence._admits``: the keys bound to ``alpha``
    read the link, the quote and the id again, and disagree with explain.
    """

    source_id = vault.capture_source(as_owner=True)
    row = vault.sql("SELECT metadata_json FROM sources WHERE id = ?", (source_id,))[0]
    assert json.loads(row["metadata_json"])["project_scope"] == [], "the owner's capture belongs to no project"
    candidate = vault._candidate("Projectlessnote: the alpha kiln shelf is cleaned on Fridays")
    _plant_saved_quote(vault, candidate, source_id)
    for who in _KEY_SPECS:
        answer = vault.review(who, candidate)
        assert answer["is_error"] is False, (who, answer)
        readable = who == "unbound"
        assert _holds_quote(answer) is readable, who
        assert _holds_source_id(answer, source_id) is readable, who
        assert (vault.explain(who, candidate)["is_error"] is False) is readable, ("explain agrees", who)


