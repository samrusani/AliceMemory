"""A saved quote of a memory is withheld from a caller who may not read that memory, on a real vault with real keys.

Unreleased (on main, not in v0.20.0). A commit can cite a memory in ``source_refs`` as ``{"memory_id": "<id>", "quote":
"..."}`` or as two entries, ``"memory:<id>"`` and ``{"quote": "..."}``, and the quote holds the words of that memory. The
write fence reads neither shape, so the commit keeps the quote as sent in ``metadata_json.agentic_memory.source_refs`` and in
``value.source_refs``. On main until this change every reader of the commit returned those words after the memory was
redacted, relabelled above a caller's ceiling, moved to another project or archived: the recent commits list, the memory
audit, ``alice_explain``, ``alice_memory_review`` detail and the legacy recent commits tool.

These tests run the lifecycle on a SQLite vault with a real key of each profile. The words of one memory carry a sentinel;
commits cite it in every spelling the reader takes; the memory is then made unreadable in each way a memory can be, and every
door is read by every key. A control reads the doors before the change and finds the sentinel at each, so an absent sentinel
means the reader withheld it and not that the door never carried it. A second vault has no keys: the owner reads it, and a
call that declares a permission profile reads the legacy recent commits tool. The Postgres doors (the workspace, the project
dashboard, the source trace, the operator route sweep) are in ``tests/integration/test_saved_quote_memory_refs_postgres.py``.

A memory can be cited in more ways than the reader has a marker for (``Memory <id>``, ``mem:<id>``, a URL, ``alice://memory/<id>``,
an id under ``memory``, ``origin``, ``ref_id`` or ``supersedes``), and the commit route saves the excerpt it was sent whatever the
ref says. The reader therefore looks up every id of a ref as a possible memory and refuses the caller the quote when the id names
a memory the caller may not read; a commit that was confirmed inline appends an event that holds the same refs, and the feeds of
events (the workspace, the source trace) are held to the rule too.

Each test names the mutation that must fail it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from alicebot_api.config import Settings
from alicebot_api.mcp.memories import redact_memory_flow
from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError, MCPToolNotFoundError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService

ALPHA = "prj_" + "a" * 16
BETA = "prj_" + "b" * 16
PROFILES = {
    "admin": ("admin_agent", None),
    "trusted": ("trusted_local_agent", None),
    "read_only": ("read_only_agent", None),
    "memory_proposal": ("memory_proposal_agent", None),
    "trusted_bound": ("trusted_local_agent", ALPHA),
    "alpha_only": ("project_scoped_agent", ALPHA),
    "admin_bound": ("admin_agent", ALPHA),
}
# The profiles a call with no key can declare without a project, which is how a client on an install with no keys asks.
DECLARED = ("admin", "trusted", "read_only", "memory_proposal")
# Who reads the memory the commits cite after each change, from the policy of each profile and nothing else: the default
# ceiling is public, internal, private and unknown (read_only and memory_proposal: public, internal, unknown); a project
# scoped, a read only and a proposal key may not read the health domain; a key bound to alpha reads alpha only. Nobody but
# the owner and an unbound admin key reads a redacted or archived memory, or a row made from a redacted one.
# ``test_the_table_is_what_explain_says`` checks the table against ``alice_explain`` of the cited memory itself.
READS_AFTER = {
    "redacted": {"admin"},
    "archived": {"admin"},
    "confidential": {"admin", "admin_bound"},
    "health": {"admin", "trusted", "trusted_bound", "admin_bound"},
    "beta": {"admin", "trusted", "read_only", "memory_proposal"},
    # The project column names another project than the metadata does; a memory is read by the column.
    "column": {"admin", "trusted", "read_only", "memory_proposal"},
    "contained": {"admin"},
    "missing": {"admin"},
}
VARIANTS = tuple(READS_AFTER)
SENTINEL_PREFIX = "ZQXMEMQUOTE"


def _quote(sentinel: str, tag: str) -> str:
    return f"Atlas played {sentinel} for 115 hours ({tag})"


def _key_words(sentinel: str, tag: str) -> str:
    return f"Atlas played {sentinel} for 115 hours ({tag} key words)"


class Vault:
    """A SQLite vault, one memory that commits cite and the commits that cite it, with or without a key of each profile."""

    def __init__(self, tmp_path, monkeypatch, *, with_keys: bool = True, unmarked: bool = True) -> None:
        self.unmarked = unmarked
        self.user = uuid4()
        self.path = tmp_path / "memory-quotes.sqlite3"
        self.sentinel = f"{SENTINEL_PREFIX}{uuid4().hex[:10]}"
        bootstrap_database(self.path, user_id=str(self.user), user_email="synthetic@example.invalid")
        monkeypatch.setenv("ALICE_MCP_FULL_TOOLS", "1")
        monkeypatch.setenv("ALICE_MCP_LEGACY_TOOLS", "1")
        monkeypatch.setenv("ALICE_PROJECT_SCOPING", "off")
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        monkeypatch.delenv("ALICE_EMBEDDINGS_BASE_URL", raising=False)
        self.monkeypatch = monkeypatch
        self.commits: dict[str, str] = {}
        self.key_shapes: set[str] = set()
        with sqlite_user_connection(self.path, self.user) as conn:
            store = SQLiteVNextStore(conn, self.user)
            self.cited = self._memory(store, f"Atlas played {self.sentinel} for 115 hours.", "alpha.cited")
            self.cited_id = str(self.cited["id"])
            self.keys = (
                {
                    name: create_agent_key(store, user_id=self.user, agent_id=name, permission_profile=profile, project_scope=bound)[1]
                    for name, (profile, bound) in PROFILES.items()
                }
                if with_keys
                else {}
            )
        # A captured source, and the candidate memory the capture made from it: a copy of the source that the source trace lists.
        captured = self.call("trusted" if with_keys else None, "alice_capture", {
            "raw_text": "Remember: the alpha kiln is fired on Mondays.", "title": "Kiln note", "domain": "project",
            "sensitivity": "internal",
        })
        self.source_id = str(captured["source_id"])  # type: ignore[index]
        with sqlite_user_connection(self.path, self.user) as conn:
            store = SQLiteVNextStore(conn, self.user)
            self._commit_every_spelling(store)
            self._cite_in_the_capture(store)

    # -- building --------------------------------------------------------------------------------------------

    def key_words(self, tag: str) -> str:
        """Words that carry the sentinel, written as the name of a field. The commit named ``tag`` is one of ``key_shapes``."""

        self.key_shapes.add(tag)
        return _key_words(self.sentinel, tag)

    def _memory(self, store, text: str, key: str):
        return store.create_memory(
            {
                "memory_key": key, "memory_type": "episode", "title": text, "canonical_text": text, "summary": text,
                "value": {"text": text}, "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {"project_scope": [ALPHA]},
            }
        )

    def _commit(self, store, name: str, refs: list[object], *, excerpt: str | None = None) -> str:
        """A commit through the shipped service, as the HTTP route and the MCP tool make it."""

        result = VNextMemoryCommitService(store).commit(
            identity=None,
            request=MemoryCommitRequest(
                user_id=str(self.user), title=f"Follow up {name}", canonical_text=f"Follow up on the games note {name}",
                memory_type="semantic", domain="project", sensitivity="public", confidence=0.95,
                source_refs=tuple(refs), conversation_excerpt=excerpt, project_scope=(ALPHA,),
            ),
        )
        assert result["status"] == "committed", result
        self.commits[name] = str(result["memory"]["id"])
        return self.commits[name]

    def _confirmed_commit(self, store) -> None:
        """A commit that asks for an inline confirmation (its confidence is middling) and is confirmed: the lifecycle that appends a
        ``memory.updated`` event whose payload holds the refs and the excerpt the commit was sent with. It cites the source, so
        the source trace lists it and its events, and the memory with a quote."""

        service = VNextMemoryCommitService(store)
        name = "confirmed"
        request = MemoryCommitRequest(
            user_id=str(self.user), title=f"Follow up {name}", canonical_text=f"Follow up on the games note {name}",
            memory_type="semantic", domain="project", sensitivity="public", confidence=0.6,
            source_refs=(
                {"source_id": self.source_id},
                {"memory_id": self.cited_id, "quote": _quote(self.sentinel, name), self.key_words(name): None},
            ),
            conversation_excerpt=_quote(self.sentinel, f"{name} excerpt"), project_scope=(ALPHA,),
        )
        asked = service.commit(identity=None, request=request)
        assert asked["status"] == "confirmation_required", asked
        service.confirm(identity=None, confirmation_id=asked["memory"]["confirmation_id"], action="confirm")
        self.commits[name] = str(asked["memory"]["id"])

    def _confirmed_by_an_agent(self, store) -> None:
        """The same lifecycle with an agent key doing it: the confirmation appends the ``memory.updated`` event with an agent for its
        actor, which the workspace lists in the agent activity as well as among the recent events."""

        service = VNextMemoryCommitService(store)
        identity = _identity_of("admin")
        name = "confirmed by an agent"
        request = MemoryCommitRequest(
            user_id=str(self.user), title=f"Follow up {name}", canonical_text=f"Follow up on the games note {name}",
            memory_type="semantic", domain="project", sensitivity="public", confidence=0.6,
            source_refs=({"memory_id": self.cited_id, "quote": _quote(self.sentinel, name), self.key_words(name): 2},),
            conversation_excerpt=_quote(self.sentinel, f"{name} excerpt"), project_scope=(ALPHA,),
        )
        asked = service.commit(identity=identity, request=request)
        assert asked["status"] == "confirmation_required", asked
        service.confirm(identity=identity, confirmation_id=asked["memory"]["confirmation_id"], action="confirm")
        self.commits[name] = str(asked["memory"]["id"])

    def _commit_every_spelling(self, store) -> None:
        """One commit per spelling of a memory ref. Each quote holds the sentinel and a tag of its own."""

        m = self.cited_id

        def q(tag: str) -> str:
            return _quote(self.sentinel, tag)

        spellings: dict[str, list[object]] = {
            "memory_id": [{"memory_id": m, "quote": q("memory_id")}],
            "memory prefix + quote entry": [f"memory:{m}", {"quote": q("memory prefix + quote entry")}],
            "MEMORY prefix": [f"MEMORY:{m}", {"quote": q("MEMORY prefix")}],
            "ref key": [{"ref": f"memory:{m}", "quote": q("ref key")}],
            "id key": [{"id": f"memory:{m}", "quote": q("id key")}],
            "memory_ids list": [{"memory_ids": [m], "quote": q("memory_ids list")}],
            "memory_refs": [{"memory_refs": [f"memory:{m}"], "quote": q("memory_refs")}],
            "no hyphens": [{"memory_id": m.replace("-", ""), "quote": q("no hyphens")}],
            "upper case": [{"memory_id": m.upper(), "quote": q("upper case")}],
            "braces": [{"memory_id": "{" + m + "}", "quote": q("braces")}],
            "urn": [{"memory_id": "urn:uuid:" + m, "quote": q("urn")}],
            "json text": [json.dumps({"memory_id": m, "quote": q("json text")})],
            "alice url": [f"alice://memories/{m}", {"quote": q("alice url")}],
            "conversation_excerpt": [f"memory:{m}"],
            "nested": [{"evidence": [{"memory_id": m, "quote": q("nested")}]}],
            # Cites a source the caller may read as well, so the commit is listed by the source trace and the dashboard.
            "source and memory": [{"source_id": self.source_id}, {"memory_id": m, "quote": q("source and memory")}],
            # Words written as the name of a field. A name holds its words whatever its value is (a string, ``null``, a number, a
            # boolean, an object), so a restricted reader is shown only the names the product writes.
            "key with a string value": [{"memory_id": m, self.key_words("key with a string value"): "x"}],
            "key with a null value": [{"memory_id": m, self.key_words("key with a null value"): None}],
            "key with a number value": [{"memory_id": m, self.key_words("key with a number value"): 1}],
            "key with a boolean value": [{"memory_id": m, "quote": None, self.key_words("key with a boolean value"): True}],
            "key nested": [{"memory_id": m, "evidence": {self.key_words("key nested"): None}}],
            "key under a reference key": [{"memory_id": m, "memories": [{"memory_id": m, self.key_words("key under a reference key"): None}]}],
            "key in json text": [json.dumps({"memory_id": m, self.key_words("key in json text"): None})],
            "key beside a quote": [{"memory_id": m, "quote": q("key beside a quote"), self.key_words("key beside a quote"): "x"}],
            "key in the entry beside": [f"memory:{m}", {self.key_words("key in the entry beside"): None}],
            "key and a source": [{"source_id": self.source_id}, {"memory_id": m, self.key_words("key and a source"): None}],
            # The entry beside the ref: every string and every field of it goes, not only a field called ``quote``.
            "companion bare string": [f"memory:{m}", q("companion bare string")],
            "companion text field": [f"memory:{m}", {"text": q("companion text field")}],
            "companion excerpt field": [f"memory:{m}", {"excerpt": q("companion excerpt field")}],
            "companion nested list": [f"memory:{m}", [[q("companion nested list")]]],
            "companion json text": [f"memory:{m}", json.dumps({"note": q("companion json text")})],
            "companion quote key with a space": [f"memory:{m}", {"quote ": q("companion quote key with a space")}],
            # An id the entry beside carries, in no reference field, names nothing: it keeps no quote, whether the id names a
            # source the caller may read or no row at all.
            "companion quote and a chunk id": [f"memory:{m}", {"quote": q("companion quote and a chunk id"), "chunk_id": str(uuid4())}],
            "companion quote and a source in a chunk id": [
                f"memory:{m}", {"quote": q("companion quote and a source in a chunk id"), "chunk_id": self.source_id}
            ],
            "companion quote, nested, and an id elsewhere": [
                f"memory:{m}", {"meta": {"first": str(uuid4())}, "memories": {"quote": q("companion quote, nested, and an id elsewhere")}}
            ],
            # A quote that is an object or a list is a structure that holds whatever its writer put in it, an id included.
            "quote is an object that holds the id": [{"quote": {"memory_id": m, "text": q("quote is an object that holds the id")}}],
            "excerpt is an object that holds the id": [
                {"conversation_excerpt": {"memory_id": m, "text": q("excerpt is an object that holds the id")}}
            ],
            "quote is a list that holds the id": [{"quote": [{"memory_id": m, "text": q("quote is a list that holds the id")}]}],
        }
        for name, refs in spellings.items():
            self._commit(store, name, refs, excerpt=q(name) if name == "conversation_excerpt" else None)
        # Wordings no marker covers. The first group names the memory in the ref and relies on the excerpt the commit route
        # saves; the second holds the quote in the ref, under a key the reader does not list. The writer accepts every one, since
        # none of them names a source.
        for name, ref in {} if not self.unmarked else {
            "Memory <id>": f"Memory {m}",
            "memory id <id>": f"memory id {m}",
            "mem:<id>": f"mem:{m}",
            "see memory: <id>": f"see memory: {m}",
            "markdown link": f"[memory]({m})",
            "https url": f"https://host.example.test/memories/{m}",
            "alice://memory/<id>": f"alice://memory/{m}",
        }.items():
            self._commit(store, name, [ref], excerpt=q(name))
        for name, ref in {} if not self.unmarked else {
            "key memory": {"memory": m},
            "key memoryId": {"memoryId": m},
            "key origin": {"origin": m},
            "key ref_id": {"ref_id": m},
            "key parent_memory_id": {"parent_memory_id": m},
            "key supersedes": {"supersedes": m},
            # Text beside the id that is not the quote: every string of the entry goes, not only the quote and the excerpt.
            "text beside the id": {"memory_id": m, "text": q("text beside the id")},
            "excerpt beside the id": {"memory_id": m, "excerpt": q("excerpt beside the id")},
            "note beside the id": {"memory_id": m, "note": q("note beside the id")},
        }.items():
            self._commit(store, name, [{**ref, "quote": q(name)} if "beside" not in name else ref])
        if self.unmarked:
            # An id typed in the text of a quote or of the excerpt is incidental: the quote is withheld when it names a memory the
            # caller may not read, so these are left out of the vault whose memory is removed from the table.
            self._commit(store, "quote with the marker in its text", [{"quote": f"memory:{m} {q('quote with the marker in its text')}"}])
            self._commit(store, "quote with the id in its text", [{"quote": f"{q('quote with the id in its text')} (memory {m})"}])
            self._commit(store, "excerpt that names the memory", [], excerpt=f"[memory:{m}] {q('excerpt that names the memory')}")
            # An id under a field the reader has no marker for, and words in the name of a field beside it.
            self._commit(store, "key beside an unmarked id", [{"origin": m, self.key_words("key beside an unmarked id"): None}])
            self._commit(store, "key beside an unmarked sentence", [f"Memory {m}", {self.key_words("key beside an unmarked sentence"): 1}])
            self._commit(store, "memory id and a quote entry", [f"alice://memory/{m}", {"quote": q("memory id and a quote entry")}])
            self._commit(store, "sentence with the words", [f"{q('sentence with the words')} (see memory {m})"])
        # Words typed behind the id: in a fragment of a reference, and after the id in a field that holds an id.
        for name, ref in {
            "memory prefix and a fragment": f"memory:{m}#{q('memory prefix and a fragment').replace(' ', '-')}",
            "alice url and a fragment": f"alice://memories/{m}#{q('alice url and a fragment').replace(' ', '-')}",
            "text directive": f"memory:{m}#:~:text={q('text directive').replace(' ', '%20')}",
            "ref key and a fragment": {"ref": f"memory:{m}#{q('ref key and a fragment').replace(' ', '-')}"},
            "memory_id and a fragment": {"memory_id": f"{m}#{q('memory_id and a fragment').replace(' ', '-')}"},
            "memory_id and words": {"memory_id": f"{m}: {q('memory_id and words')}"},
        }.items():
            self._commit(store, name, [ref])
        self._confirmed_commit(store)
        self._confirmed_by_an_agent(store)

    def _cite_in_the_capture(self, store) -> None:
        """The source trace lists a memory whose metadata names the source at the top (a memory proposal keeps the refs it was
        given there). The commit that cites the source and the memory is given that list, and it is an original row, so the
        caller's ceiling lets it through."""

        for name in ("source and memory", "confirmed"):
            row = store.get_memory(self.commits[name])
            metadata = {**row["metadata_json"], "source_refs": [self.source_id]}
            store.update_memory(memory_id=str(row["id"]), patch={"metadata_json": metadata}, actor_type="system")

    # -- changing the cited memory -----------------------------------------------------------------------------

    def make_unreadable(self, variant: str) -> None:
        with sqlite_user_connection(self.path, self.user) as conn:
            store = SQLiteVNextStore(conn, self.user)
            if variant == "redacted":
                assert redact_memory_flow(store, memory_id=self.cited_id, reason="synthetic")["status"] == "redacted"
            elif variant == "archived":
                store.update_memory(memory_id=self.cited_id, patch={"status": "archived"}, actor_type="system")
            elif variant == "confidential":
                store.update_memory(memory_id=self.cited_id, patch={"sensitivity": "confidential"}, actor_type="system")
            elif variant == "health":
                store.update_memory(memory_id=self.cited_id, patch={"domain": "health"}, actor_type="system")
            elif variant == "beta":
                row = store.get_memory(self.cited_id)
                self._set_metadata(conn, {**row["metadata_json"], "project_scope": [BETA]})
            elif variant == "column":
                row = store.get_memory(self.cited_id)
                conn.execute(
                    "UPDATE memories SET project_id = ?, metadata_json = ? WHERE id = ? AND user_id = ?",
                    (BETA, json.dumps({k: v for k, v in row["metadata_json"].items() if k != "project_scope"} | {"project_id": ALPHA}), self.cited_id, str(self.user)),
                )
            elif variant == "contained":
                # The commits cite a copy of the memory, and the memory the copy was made from is redacted afterwards.
                original = self._memory(store, f"Atlas played {self.sentinel} in the original note.", "alpha.original")
                derived = with_derived_from(
                    {"project_scope": [ALPHA], "discovered_by": "vnext_weekly_synthesis"}, {"memories": [original]}
                )
                self._set_metadata(conn, derived)
                assert redact_memory_flow(store, memory_id=str(original["id"]), reason="synthetic")["status"] == "redacted"
            elif variant == "missing":
                conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (self.cited_id, str(self.user)))
            else:  # pragma: no cover
                raise AssertionError(variant)

    def _set_metadata(self, conn, metadata: dict[str, object]) -> None:
        conn.execute(
            "UPDATE memories SET metadata_json = ? WHERE id = ? AND user_id = ?",
            (json.dumps(metadata), self.cited_id, str(self.user)),
        )

    # -- reading -----------------------------------------------------------------------------------------------

    def call(self, who: str | None, tool: str, arguments: dict[str, object]) -> object:
        self.monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
        if who is not None:
            self.monkeypatch.setenv("ALICE_AGENT_API_KEY", self.keys[who])
        context = MCPRuntimeContext(database_url=sqlite_url_for_path(self.path), user_id=UUID(str(self.user)))
        return call_mcp_tool(context, name=tool, arguments=arguments)

    def try_call(self, who: str | None, tool: str, arguments: dict[str, object]) -> dict[str, object] | None:
        """The parsed answer of a tool, or None when it answers an error."""

        try:
            return {"body": self.call(who, tool, arguments)}
        except (MCPToolError, MCPToolNotFoundError):
            return None

    def declared(self, who: str, tool: str, arguments: dict[str, object]) -> dict[str, object] | None:
        """A call with no key that declares a permission profile, as a client on an install with no keys does."""

        return self.try_call(None, tool, {**arguments, "agent_id": f"declared-{who}", "permission_profile": PROFILES[who][0]})

    @contextmanager
    def routes(self):
        """The HTTP handlers of the memory routes, pointed at this vault's SQLite store (they are PostgreSQL only otherwise)."""

        from alicebot_api.routers import vnext_memories

        path = self.path

        @contextmanager
        def connection(_database_url, current_user_id):  # type: ignore[no-untyped-def]
            with sqlite_user_connection(path, str(current_user_id)) as conn:
                yield conn

        with self.monkeypatch.context() as patch:
            patch.setattr(vnext_memories, "get_settings", lambda: Settings(database_url="postgresql://db"))
            patch.setattr(vnext_memories, "user_connection", connection)
            patch.setattr(vnext_memories, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, self.user))
            yield vnext_memories

    def route(self, who: str | None, name: str, **kwargs) -> dict[str, object] | None:
        """One handler call by one key (or by the owner): the parsed body, or None when it answers an error status."""

        with self.routes() as module:
            authorization = f"Bearer {self.keys[who]}" if who is not None else None
            if name == "recent":
                response = module.list_vnext_recent_memory_commits(
                    user_id=self.user, limit=kwargs.get("limit", 100), authorization=authorization
                )
            else:
                response = module.get_vnext_memory_audit(
                    memory_id=UUID(kwargs["memory_id"]), user_id=self.user, authorization=authorization
                )
        if response.status_code != 200:
            return None
        return {"body": json.loads(response.body)}


class _PostgresOnlyReads(SQLiteVNextStore):
    """The reads the project dashboard and the source trace make that SQLite does not have (it has no projects table and no
    artifacts), answered with the rows a project of this vault would have: the vault's memories, and no artifact or loop."""

    def get_project(self, project_id):  # type: ignore[no-untyped-def]
        return {
            "id": project_id, "name": "Atlas", "domain": "project", "sensitivity": "public", "current_state": None,
            "metadata_json": {},
        }

    def list_open_loops(self, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    def list_artifacts(self, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    def list_artifacts_referencing_source(self, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    def list_open_loops_referencing_source(self, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    def list_source_chunks(self, source_id, limit=None):  # type: ignore[no-untyped-def]
        return []


def _identity_of(who: str):
    from alicebot_api.vnext_agent_control import AgentIdentity

    profile, bound = PROFILES[who]
    return AgentIdentity(
        agent_id=who, permission_profile=profile, project_scope=(bound,) if bound else (), auth="agent_api_key",
        project_scope_locked=bound is not None,
    )


def _dashboard(vault: "Vault", who: str | None) -> dict[str, object]:
    from alicebot_api.vnext_projects import VNextProjectService

    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = _PostgresOnlyReads(conn, vault.user)
        identity = _identity_of(who) if who is not None else None
        return {"body": VNextProjectService(store).project_dashboard(project_id=ALPHA, identity=identity)}


def _source_trace(vault: "Vault", who: str | None) -> dict[str, object]:
    from alicebot_api.routers._vnext_shared import _vnext_load_source_trace

    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = _PostgresOnlyReads(conn, vault.user)
        identity = _identity_of(who) if who is not None else None
        source = store.get_source(vault.source_id)
        return {"body": _vnext_load_source_trace(store=store, source=source, identity=identity)}


def _without_unmarked(request) -> bool:
    """The vault of the ``missing`` variant leaves out the commits whose ref names the memory in a way no marker covers: with the
    row removed from the table their id names no memory, and ``test_an_unmarked_id_of_a_memory_that_is_not_in_the_table_...``
    pins what the reader does with it."""

    callspec = getattr(request.node, "callspec", None)
    return callspec is not None and callspec.params.get("variant") == "missing"


@pytest.fixture
def vault(tmp_path, monkeypatch, request):
    return Vault(tmp_path, monkeypatch, unmarked=not _without_unmarked(request))


@pytest.fixture
def keyless(tmp_path, monkeypatch, request):
    return Vault(tmp_path / "keyless", monkeypatch, with_keys=False, unmarked=not _without_unmarked(request))


def _event_feed(vault: "Vault", who: str | None) -> dict[str, object]:
    """The events about the commits of the vault as the workspace shows them to one caller: the events its guard admits, then the
    reader of saved quotes. This is the code ``GET /v0/vnext/workspace`` runs for ``recent_events``, on the SQLite store. The
    workspace asks for the newest 20 events of the store, and every read of a door appends events of its own, so the source of
    events here is the events about the commits, newest first, which the guard and the reader then treat as the workspace's."""

    from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope, sensitivity_ceiling
    from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence

    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        identity = _identity_of(who) if who is not None else None
        allowed = ["public", "internal", "private", "unknown"]
        ceiling = sensitivity_ceiling(identity)
        if ceiling is not None:
            allowed = [value for value in allowed if value in ceiling]
        projects = identity.project_scope if identity is not None else ()
        guard = LabelGuard.for_filters(
            store, (), allowed, projects, all_of=projects if identity is not None and identity.project_scope_locked else None
        )
        fence = SourceReadFence.for_identity(identity)
        if not fence.entity_read_fenced:
            guard = LabelGuard(store=store, active=False)

        def newest(size: int) -> list[dict[str, object]]:
            events = [
                event
                for memory_id in vault.commits.values()
                for event in store.list_events(target_type="memory", target_id=memory_id)
            ]
            events.sort(key=lambda event: (str(event["occurred_at"]), str(event["id"])), reverse=True)
            return events[:size]

        with label_read_scope(store):
            feed = guard.newest_admitted_events(newest, want=1000)
        if fence.entity_read_fenced:
            feed = SavedProvenanceReader(store, fence=fence).events(feed)
        return {"body": feed}


def _agent_event_feed(vault: "Vault", who: str | None) -> dict[str, object]:
    """The agent activity of the workspace for one caller: the events an agent key caused that its guard admits, then the reader of
    saved quotes. This is the code ``GET /v0/vnext/workspace`` runs for ``agent_activity.recent_events``, on the SQLite store
    (``tests/unit/test_saved_quote_memory_refs_doors.py`` runs the whole workspace builder on a stub store). The workspace asks for
    the newest 50, and every read of a door by a key appends events of its own, so the source of events here is the agent events
    about the commits, as ``_event_feed`` takes the events about them."""

    from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope, sensitivity_ceiling
    from alicebot_api.vnext_source_fence import SavedProvenanceReader, SourceReadFence

    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        identity = _identity_of(who) if who is not None else None
        allowed = ["public", "internal", "private", "unknown"]
        ceiling = sensitivity_ceiling(identity)
        if ceiling is not None:
            allowed = [value for value in allowed if value in ceiling]
        projects = identity.project_scope if identity is not None else ()
        guard = LabelGuard.for_filters(
            store, (), allowed, projects, all_of=projects if identity is not None and identity.project_scope_locked else None
        )
        fence = SourceReadFence.for_identity(identity)
        if not fence.entity_read_fenced:
            guard = LabelGuard(store=store, active=False)
        commits = set(vault.commits.values())

        def newest(size: int) -> list[dict[str, object]]:
            return [event for event in store.list_agent_events(limit=100_000) if str(event["target_id"]) in commits][:size]

        with label_read_scope(store):
            feed = guard.newest_admitted_events(newest, want=1000)
        if fence.entity_read_fenced:
            feed = SavedProvenanceReader(store, fence=fence).events(feed)
        return {"body": feed}


def _text(answer: dict[str, object] | None) -> str:
    return json.dumps(answer["body"], default=str) if answer is not None else ""


def _keyed_doors(vault: Vault, who: str) -> dict[str, dict[str, object] | None]:
    """What every door that returns a commit gives one key, for every commit. ``None`` is a refusal or an error. The two HTTP
    operator routes are asked of the unbound trusted and admin keys, the only ones the central gate lets in."""

    answers: dict[str, dict[str, object] | None] = {}
    operator = who in {"admin", "trusted"}
    if operator:
        answers["recent_commits route"] = vault.route(who, "recent", limit=100)
        answers["recent_commits route limit 1"] = vault.route(who, "recent", limit=1)
    for name, memory_id in vault.commits.items():
        if operator:
            answers[f"audit route {name}"] = vault.route(who, "audit", memory_id=memory_id)
        answers[f"explain {name}"] = vault.try_call(who, "alice_explain", {"memory_id": memory_id})
        answers[f"review detail {name}"] = vault.try_call(who, "alice_memory_review", {"review_item_id": memory_id})
    answers["event feed"] = _event_feed(vault, who)
    answers["agent event feed"] = _agent_event_feed(vault, who)
    answers["review list"] = vault.try_call(who, "alice_memory_review", {"status": "all", "limit": 100})
    answers["pack"] = vault.try_call(who, "alice_context_pack", {"query": "Follow up on the games note"})
    answers["recall"] = vault.try_call(who, "alice_recall", {"query": "Follow up on the games note", "limit": 50})
    answers["resume"] = vault.try_call(who, "alice_resume", {})
    answers["recent decisions"] = vault.try_call(who, "alice_recent_decisions", {})
    return answers


def _declared_doors(vault: Vault, who: str) -> dict[str, dict[str, object] | None]:
    """The doors a call that declares a profile reaches: the legacy recent commits tool, and review detail by id."""

    answers = {
        "legacy recent commits": vault.declared(who, "alice_vnext_recent_memory_commits", {"limit": 100}),
        "legacy recent commits limit 1": vault.declared(who, "alice_vnext_recent_memory_commits", {"limit": 1}),
    }
    for name, memory_id in vault.commits.items():
        answers[f"review detail {name}"] = vault.declared(who, "alice_memory_review", {"review_item_id": memory_id})
    return answers


def _carried(answers: dict[str, dict[str, object] | None], sentinel: str) -> set[str]:
    return {door for door, answer in answers.items() if sentinel in _text(answer)}


def test_the_control_finds_the_quote_at_every_door_before_the_memory_changes(vault: Vault, keyless: Vault) -> None:
    """Before the change every key that reaches a door is shown the words in it, so a door that is silent afterwards withheld
    them. The doors that must carry them are named, so a door that answers nothing cannot pass for a withheld one.
    """

    for who in ("admin", "trusted", "trusted_bound"):
        carried = _carried(_keyed_doors(vault, who), vault.sentinel)
        assert any(door.startswith("explain") for door in carried), who
        assert any(door.startswith("review detail") for door in carried), who
        assert "event feed" in carried, (who, "the confirmed commit's event carries the quote")
        assert "agent event feed" in carried, (who, "the event of the commit an agent confirmed carries the quote")
        if who in {"admin", "trusted"}:
            assert "recent_commits route" in carried and "recent_commits route limit 1" in carried, who
            assert any(door.startswith("audit route") for door in carried), who
    for who in DECLARED:
        carried = _carried(_declared_doors(keyless, who), keyless.sentinel)
        assert {"legacy recent commits", "legacy recent commits limit 1"} <= carried, who
    for name, memory_id in keyless.commits.items():
        assert keyless.sentinel in _text(keyless.route(None, "audit", memory_id=memory_id)), name
        assert keyless.sentinel in _text(keyless.try_call(None, "alice_explain", {"memory_id": memory_id})), name


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_key_that_may_not_read_the_cited_memory_is_shown_none_of_its_words(vault: Vault, variant: str) -> None:
    """For each way a memory stops being readable (redacted, archived, raised above the ceiling, moved to the health domain,
    moved to another project, copied from a memory that is redacted, removed from the table), every door that returns a
    commit shows a key that may not read the memory none of the quotes in any spelling, and shows a key that may read it
    all of them. The commit itself is still returned.

    Mutations: in ``SavedProvenanceReader._judge_memories``, admit every memory (``self._memory_admitted[memory_id] = True``):
    every variant fails; drop the ``deleted_at`` test: the redacted and archived variants fail; settle no effective row
    (``effective = row``): the contained variant fails; return from ``_memory_ids_named_by_memory_copies`` before the
    reference keys are read: the spellings under ``memory_ids`` and ``memory_refs`` fail.
    """

    vault.make_unreadable(variant)
    readers = READS_AFTER[variant]
    for who in PROFILES:
        answers = _keyed_doors(vault, who)
        assert any(answer is not None for answer in answers.values()), (variant, who, "the key reads something")
        carried = _carried(answers, vault.sentinel)
        if who in readers:
            continue
        assert not carried, (variant, who, sorted(carried)[:3])
    # The control the other way: a key that may read the memory still gets its words wherever the door carries them.
    for who in readers & {"admin", "trusted"}:
        carried = _carried(_keyed_doors(vault, who), vault.sentinel)
        assert "recent_commits route" in carried and any(door.startswith("audit route") for door in carried), (variant, who)
        assert any(door.startswith("review detail") for door in carried), (variant, who)


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_key_that_may_read_the_cited_memory_is_shown_the_names_of_the_fields_it_was_written_with(vault: Vault, variant: str) -> None:
    """Words written as the name of a field are withheld from a key that may not read the memory the entry cites, and from nobody
    else: the keys that read the memory after the change, and the owner and the unbound admin key, are shown the entry as it was
    stored, field names included. Every shape of the key words (a string, ``null``, a number or a boolean for the value, a name
    nested under another field or inside JSON text, a name in the entry beside the id, a name beside an id no marker covers) is
    read by each key that may read, at the doors that return the whole commit.

    Mutation: refuse every memory to a caller with limits (``self._memory_admitted[memory_id] = False`` in ``_judge_memories``): the
    keys that read the memory after the change lose the words.
    """

    vault.make_unreadable(variant)
    names = [name for name in vault.commits if name in vault.key_shapes]
    assert len(names) >= 12, "the shapes of the finding are committed"
    readers = READS_AFTER[variant] & {"admin", "trusted", "trusted_bound", "admin_bound"}
    assert "admin" in readers
    for who in sorted(readers):
        for name in names:
            memory_id = vault.commits[name]
            doors = [
                vault.try_call(who, "alice_explain", {"memory_id": memory_id}),
                vault.try_call(who, "alice_memory_review", {"review_item_id": memory_id}),
            ]
            if who in {"admin", "trusted"}:
                doors.append(vault.route(who, "audit", memory_id=memory_id))
            assert any(vault.key_words(name) in _text(answer) for answer in doors), (variant, who, name)


def test_a_ref_that_is_json_text_with_an_escaped_surrogate_does_not_fail_a_route(vault: Vault) -> None:
    """A ref string such as ``{"quote": "q", "note": "\\ud800"}`` is stored as six ASCII characters and decodes to a lone surrogate.
    The reader wrote the decoded text again with ``ensure_ascii=False``, so the recent commits route and the memory audit route
    raised ``UnicodeEncodeError`` when the response was encoded, for an unbound ``trusted_local_agent`` key, whether or not the
    cited id named a memory. The answers of the routes and of the tools are encoded here; the owner and an unbound admin key
    read the text as it was stored.

    An entry that is rebuilt keeps only the names the product writes and the references, so the surrogate is in a field that is
    dropped (``test_a_ref_that_is_json_text_is_written_again_in_ascii`` pins the encoding of what stays).
    """

    ghost = str(uuid4())
    poison = '{"quote": "q", "note": "\\ud800"}'
    shapes = {
        "poison beside a missing memory": [f"memory:{ghost}", poison],
        "poison in an entry that names the memory": [json.dumps({"memory_id": vault.cited_id, "k": 1}).replace('"k"', '"\\ud800k"')],
        "poison in a quote": [json.dumps({"memory_id": vault.cited_id, "quote": "x"}).replace('"x"', '"\\ud800"')],
    }
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        poisoned = {name: vault._commit(store, name, refs) for name, refs in shapes.items()}
    for variant in ("redacted",):
        vault.make_unreadable(variant)
    for who in ("trusted", "admin"):
        recent = vault.route(who, "recent", limit=100)
        assert recent is not None, who
        for name, memory_id in poisoned.items():
            audit = vault.route(who, "audit", memory_id=memory_id)
            assert audit is not None, (who, name)
    for who in ("trusted", "read_only", "trusted_bound"):
        for name, memory_id in poisoned.items():
            for tool, arguments in (("alice_explain", {"memory_id": memory_id}), ("alice_memory_review", {"review_item_id": memory_id})):
                answer = vault.try_call(who, tool, arguments)
                assert answer is None or json.dumps(answer, ensure_ascii=False).encode("utf-8"), (who, name, tool)


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_call_that_declares_a_profile_is_held_to_it_at_the_legacy_tool_and_review_detail(keyless: Vault, variant: str) -> None:
    """On an install with no agent keys a call may declare a permission profile. The legacy recent commits tool and review
    detail by id hold it to that profile, as they did for a source: a declared read_only, trusted or memory_proposal caller
    is shown none of the words of a memory it may not read. (``alice_explain`` does not hold a declared profile; the security
    note says so.)

    Mutation: in ``recent_commits`` of the commit service, skip the reader (``if guard.active and False``).
    """

    keyless.make_unreadable(variant)
    for who in DECLARED:
        carried = _carried(_declared_doors(keyless, who), keyless.sentinel)
        if who in READS_AFTER[variant]:
            assert {"legacy recent commits", "legacy recent commits limit 1"} <= carried, (variant, who)
        else:
            assert not carried, (variant, who, sorted(carried)[:3])


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_owner_and_an_unbound_admin_key_keep_every_quote(vault: Vault, keyless: Vault, variant: str) -> None:
    """The owner (a call with no key, on an install with none) is shown what was stored, and so is an unbound admin key.

    Mutations: make ``SourceReadFence.fenced`` return ``True`` for the owner, or let ``_judge_memories`` skip the
    ``entity_read_fenced`` test: the owner and the admin key lose the words of a redacted memory.
    """

    vault.make_unreadable(variant)
    keyless.make_unreadable(variant)
    for subject, who in ((keyless, None), (vault, "admin")):
        for name, memory_id in subject.commits.items():
            answer = subject.route(who, "audit", memory_id=memory_id)
            assert answer is not None and subject.sentinel in _text(answer), (variant, who, name)
        recent = subject.route(who, "recent", limit=100)
        assert recent is not None and subject.sentinel in _text(recent), (variant, who)
        detail = subject.try_call(who, "alice_memory_review", {"review_item_id": subject.commits["memory_id"]})
        assert detail is not None and subject.sentinel in _text(detail), (variant, who)
    legacy = keyless.try_call(None, "alice_vnext_recent_memory_commits", {"limit": 100})
    assert legacy is not None and keyless.sentinel in _text(legacy), variant


def test_the_id_of_the_cited_memory_stays_and_the_rest_of_the_commit_is_untouched(vault: Vault) -> None:
    """The ref keeps its id and loses its quote (the marker a withheld quote has on a link, ``null``); the commit keeps its
    title and text, and every field of the ref that the product writes. A field the product does not write goes with its
    value, whatever the value is, so the entry ``{"evidence": [{"memory_id": "<id>", "quote": null}]}`` loses ``evidence`` and
    the id with it (a restricted reader is shown less, never more), and the entry that names the memory with a name made of words
    keeps ``memory_id`` and loses the words.

    Mutations: replace ``_withhold_entry_text`` with a function that returns ``None`` (the entry goes, and the id with it); keep
    the fields of an entry that the product does not write (``_product_key`` returns the lower-cased key whatever it is: the
    key rows keep their words).
    """

    vault.make_unreadable("redacted")

    def refs(name: str) -> object:
        answer = vault.route("trusted", "audit", memory_id=vault.commits[name])
        assert answer is not None
        memory = answer["body"]["memory"]
        assert memory["title"] == f"Follow up {name}"
        assert memory["value"]["source_refs"] == memory["metadata_json"]["agentic_memory"]["source_refs"]
        return memory["metadata_json"]["agentic_memory"]["source_refs"]

    assert refs("memory_id") == [{"memory_id": vault.cited_id, "quote": None}]
    assert refs("memory prefix + quote entry") == [f"memory:{vault.cited_id}", {"quote": None}]
    assert refs("nested") == [{}]
    assert refs("upper case") == [{"memory_id": vault.cited_id.upper(), "quote": None}]
    assert json.loads(refs("json text")[0]) == {"memory_id": vault.cited_id, "quote": None}
    assert refs("alice url") == [f"alice://memories/{vault.cited_id}", {"quote": None}]
    # Words typed behind the id go with the quote; the reference stays without its fragment.
    assert refs("memory prefix and a fragment") == [f"memory:{vault.cited_id}"]
    assert refs("alice url and a fragment") == [f"alice://memories/{vault.cited_id}"]
    assert refs("text directive") == [f"memory:{vault.cited_id}"]
    assert refs("ref key and a fragment") == [{"ref": f"memory:{vault.cited_id}"}]
    assert refs("memory_id and a fragment") == [{"memory_id": vault.cited_id}]
    assert refs("memory_id and words") == [{"memory_id": None}]
    # Words written as the name of a field go with the value of the field, whatever the value is, at any depth and inside JSON
    # text; the names the product writes stay (``memory_id``, ``source_id``, ``memories`` and the quote marker).
    cited = vault.cited_id
    assert refs("key with a string value") == [{"memory_id": cited}]
    assert refs("key with a null value") == [{"memory_id": cited}]
    assert refs("key with a number value") == [{"memory_id": cited}]
    assert refs("key with a boolean value") == [{"memory_id": cited, "quote": None}]
    assert refs("key nested") == [{"memory_id": cited}]
    assert refs("key under a reference key") == [{"memory_id": cited, "memories": [{"memory_id": cited}]}]
    assert json.loads(refs("key in json text")[0]) == {"memory_id": cited}
    assert refs("key beside a quote") == [{"memory_id": cited, "quote": None}]
    assert refs("key in the entry beside") == [f"memory:{cited}", {}]
    assert refs("key and a source") == [{"source_id": vault.source_id}, {"memory_id": cited}]
    assert refs("key beside an unmarked id") == [{}]
    assert refs("key beside an unmarked sentence") == [None, {}]
    # The entry beside the ref loses every string and every name the product does not write, as the entry that names the memory
    # does; an id it carries in a field that is not a reference names nothing and keeps no quote.
    assert refs("companion bare string") == [f"memory:{cited}", None]
    assert refs("companion text field") == [f"memory:{cited}", {}]
    assert refs("companion excerpt field") == [f"memory:{cited}", {}]
    assert refs("companion nested list") == [f"memory:{cited}", [[None]]]
    assert refs("companion quote key with a space") == [f"memory:{cited}", {}]
    assert json.loads(refs("companion json text")[1]) == {}
    assert refs("companion quote and a chunk id") == [f"memory:{cited}", {"quote": None}]
    assert refs("companion quote and a source in a chunk id") == [f"memory:{cited}", {"quote": None}]
    assert refs("companion quote, nested, and an id elsewhere") == [f"memory:{cited}", {"memories": {"quote": None}}]
    # A quote that is a structure holds the id the entry names the memory by, and goes whole with it.
    assert refs("quote is an object that holds the id") == [{"quote": None}]
    assert refs("excerpt is an object that holds the id") == [{"conversation_excerpt": None}]
    assert refs("quote is a list that holds the id") == [{"quote": None}]
    assert refs("quote with the marker in its text") == [{"quote": None}]
    assert refs("quote with the id in its text") == [{"quote": None}]


def test_the_conversation_excerpt_of_a_commit_that_cites_an_unreadable_memory_is_withheld(vault: Vault) -> None:
    """The commit route saves its excerpt as ``agentic_memory.conversation_excerpt``. A commit that cites a memory the caller
    may not read loses that copy with the quote, as a commit that cites a source it may not read does.

    Mutation: pass ``withhold_quotes=bool(refused)`` in ``_verdict`` (the memories no longer count): the excerpt stays.
    """

    name = "conversation_excerpt"
    stored = vault.route("admin", "audit", memory_id=vault.commits[name])
    assert stored["body"]["memory"]["metadata_json"]["agentic_memory"]["conversation_excerpt"] == _quote(vault.sentinel, name)
    vault.make_unreadable("redacted")
    shown = vault.route("trusted", "audit", memory_id=vault.commits[name])["body"]
    assert "conversation_excerpt" not in shown["memory"]["metadata_json"]["agentic_memory"]
    assert vault.sentinel not in json.dumps(shown, default=str)
    kept = vault.route("admin", "audit", memory_id=vault.commits[name])["body"]
    assert kept["memory"]["metadata_json"]["agentic_memory"]["conversation_excerpt"] == _quote(vault.sentinel, name)


@pytest.mark.parametrize("variant", ["confidential", "health", "beta", "column", "contained"])
def test_the_table_is_what_explain_says(vault: Vault, variant: str) -> None:
    """``READS_AFTER`` is written from the policy of each profile. The cited memory itself is the judge here: a key reads the
    memory exactly when ``alice_explain`` of that memory answers, which is the test the reader applies. (A redacted, archived
    or removed memory is not asked: explain answers the error of a missing row to every key, the unbound admin key included,
    and the reader keeps the quote for that key.)

    Mutation: none; the test pins the table so that a change to a profile's policy changes the table with it.
    """

    vault.make_unreadable(variant)
    for who in PROFILES:
        answer = vault.try_call(who, "alice_explain", {"memory_id": vault.cited_id})
        assert (answer is not None) is (who in READS_AFTER[variant]), (variant, who)


# -- the doors that are PostgreSQL routes, run here on the services they call -----------------------------------------------


def test_the_project_dashboard_and_the_source_trace_withhold_the_quote_from_a_key_that_may_not_read_the_memory(vault: Vault) -> None:
    """``GET /v0/vnext/projects/{id}/dashboard`` (and the dashboards of the workspace) lists the memories of a project with
    their metadata, and ``GET /v0/vnext/traces/sources/{id}`` (and the trace in a source review) lists the memories that cite
    a source. Both are PostgreSQL routes; the services they call run here on the SQLite store, with the reads SQLite lacks
    answered. A key that may not read the cited memory is shown none of the words, the owner and the unbound admin key all.

    Mutations: drop the reader from ``project_dashboard`` (``quote_fence.entity_read_fenced`` made false); drop it from
    ``_vnext_load_source_trace``.
    """

    # Before the memory is redacted every caller that reaches a door reads the quote. The source trace answers a profile
    # its guard refuses (read_only) as a missing trace, as the operator gate refuses it over HTTP, so that door is read
    # by the owner, the admin key and the trusted key here.
    readers = {_dashboard: (None, "admin", "trusted", "read_only"), _source_trace: (None, "admin", "trusted")}
    for door in (_dashboard, _source_trace):
        before = {who: door(vault, who) for who in readers[door]}
        assert all(vault.sentinel in _text(answer) for answer in before.values()), door.__name__
    vault.make_unreadable("redacted")
    for door in (_dashboard, _source_trace):
        for who in ("trusted", "trusted_bound", "read_only", "memory_proposal", "alpha_only", "admin_bound"):
            answer = door(vault, who)
            assert vault.sentinel not in _text(answer), (door.__name__, who)
        for who in (None, "admin"):
            assert vault.sentinel in _text(door(vault, who)), (door.__name__, who)
    listed = _dashboard(vault, "trusted")["body"]["memories"]
    assert any(row["id"] == vault.commits["source and memory"] for row in listed), "the commit is still listed"
    traced = _source_trace(vault, "trusted")["body"]["candidate_memories"]
    assert any(row["id"] == vault.commits["source and memory"] for row in traced), "the commit is still traced"


def test_alice_explain_does_not_hold_a_call_that_declares_a_profile_with_no_key(keyless: Vault) -> None:
    """The pages say it plainly: on an install with no agent keys a call that declares a restricted profile is held to it by
    ``alice_memory_review`` by id and by the legacy recent commits tool, and ``alice_explain`` and the legacy audit tool have
    never held it (the declared profile is a claim and not a credential). The saved quote of a memory is withheld from a key at
    both and not from that call, so the limit the pages name is true.

    Mutation: drop ``_is_key_bound_explain(identity)`` from the condition of the reader call in
    ``_handle_alice_vnext_memory_audit``: the declared profile is held and this test fails.
    """

    keyless.make_unreadable("redacted")
    for who in ("read_only", "memory_proposal", "trusted"):
        for tool in ("alice_explain", "alice_vnext_memory_audit"):
            answer = keyless.declared(who, tool, {"memory_id": keyless.commits["memory_id"]})
            assert answer is not None and keyless.sentinel in _text(answer), (who, tool)


def test_an_unmarked_id_of_a_memory_that_is_not_in_the_table_names_nothing_and_keeps_the_quote(tmp_path, monkeypatch) -> None:
    """The limit the pages state. An id that no marker says is a memory is refused the caller only when it names a memory the
    store holds a row for, a redacted or archived one included. A memory removed from the table (no door of the product does
    that: a memory is archived or redacted) leaves an id that cannot be told from a chunk id or a session id, so a commit that
    names it in a way no marker covers keeps its quote. The same id behind ``memory:`` or under ``memory_id`` is refused.

    Mutation: refuse an incidental id that names no row (``self._memory_exists`` ignored in ``_refused_memories``): the unmarked
    commits lose the quote and the limit the pages state is no longer true.
    """

    vault = Vault(tmp_path, monkeypatch)
    vault.make_unreadable("missing")
    unmarked = ("Memory <id>", "mem:<id>", "https url", "key origin", "sentence with the words")
    marked = ("memory_id", "memory prefix + quote entry", "alice url", "memory_ids list", "text beside the id")
    for name in unmarked:
        answer = vault.route("trusted", "audit", memory_id=vault.commits[name])
        assert answer is not None and vault.sentinel in _text(answer), name
    for name in marked:
        answer = vault.route("trusted", "audit", memory_id=vault.commits[name])
        assert answer is not None and vault.sentinel not in _text(answer), name


def test_an_id_that_names_no_memory_leaves_the_quote_readable_after_the_memory_is_redacted(vault: Vault) -> None:
    """A commit whose ref holds the id of a chunk and of a session, and a quote of its own, cites no memory the caller may not
    read, so the quote stays for every key that reads the commit. The reader refuses on a memory row and not on the shape of an id.

    Mutation: refuse every id of a ref that is not a source (``cited.incidental`` read as named in ``_refused_memories``): the
    quote goes.
    """

    control = f"Atlas played ZQXCONTROL{vault.sentinel[-6:]} for 12 hours"
    refs = [{"chunk_id": str(uuid4()), "session": str(uuid4()), "origin": str(uuid4()), "quote": control}]
    with sqlite_user_connection(vault.path, vault.user) as conn:
        commit_id = vault._commit(SQLiteVNextStore(conn, vault.user), "unrelated ids", refs)
    del vault.commits["unrelated ids"]
    vault.make_unreadable("redacted")
    for who in ("admin", "trusted", "read_only", "trusted_bound"):
        explained = vault.try_call(who, "alice_explain", {"memory_id": commit_id})
        detail = vault.try_call(who, "alice_memory_review", {"review_item_id": commit_id})
        assert control in _text(explained) + _text(detail), who
    for who in ("admin", "trusted"):
        answer = vault.route(who, "audit", memory_id=commit_id)
        assert answer is not None and control in _text(answer), who
        assert control in _text(vault.route(who, "recent", limit=100)), who
