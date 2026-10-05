"""A write that cites a source by id is held to the caller's read fence.

Unreleased (on main, not in v0.20.0). v0.20.0 checked only that a cited source existed for the acting user. A key
bound to one project could attach a source of another project, a source above its sensitivity ceiling, a source in a
domain its profile may not read, a global source or a deleted one, through ``alice_memory_commit`` ``source_refs`` and
through the ``provenance`` of ``alice_memory_correct``, while an id that did not exist failed (``tool_request_failed``
on v0.20.0, ``precondition_failed`` on SQLite on main). So a source id was an existence oracle, and the attached link then
made the key's own ``alice_explain`` of the memory fail closed and showed the foreign id to anyone in the project who
reviewed the memory by id.

The fence is the one ``alice_explain`` applies to each source it discloses. Every refusal (missing, deleted, other
project, above the ceiling, restricted domain, global) is one answer: ``not_found`` with the fixed message.

``POST /v0/vnext/open-loops`` is the third door: an open loop keeps the ``source_id`` and ``memory_id`` it is given in
columns of its own. Section 3c holds that door to the same fence, for a source and for a memory, and answers 404 for
every refusal. That is the writer's fence, once. What a later reader of the loop is shown is a separate rule, held to
the reader's own fence (``tests/unit/test_open_loop_references_read_fence.py``).

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the file was
restored by copying the saved copy back.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import sqlite3
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from alicebot_api import mcp_server
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_control import AgentIdentity
from alicebot_api.vnext_agent_keys import create_agent_key
from alicebot_api.vnext_memory_commit import (
    VNextMemoryCommitService,
    memory_commit_request_from_payload,
)
from alicebot_api.vnext_source_fence import (
    EXPLAIN_DISCLOSURE_ACTION,
    MEMORY_REF_NOT_FOUND_MESSAGE,
    SOURCE_REF_NOT_FOUND_MESSAGE,
    MemoryRefNotFoundError,
    SourceReadFence,
    SourceRefNotFoundError,
    resolve_attachable_memory_id,
    resolve_attachable_source_id,
    resolve_attachable_sources,
)

_USER_ID = "00000000-0000-0000-0000-000000000001"
_KEY_ENV = "ALICE_AGENT_API_KEY"
_FIXED_MESSAGE = "The tool request could not be processed"
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "apps" / "api" / "src" / "alicebot_api"

_TEXT = {
    "own": "Alpha own note: the alpha kiln runs on Mondays with saltwhite-glaze-12.",
    "health": "Alpha health note: knee rehab uses brine-wrap-33 twice a week.",
    "confidential": "Alpha confidential note: the supplier quote was cedar-ledger-55 dollars.",
    "deleted": "Alpha deleted note: the old kiln log said ember-chart-91.",
    "beta": "Beta only note: the beta kiln is fired on Thursdays with kumquat-harbor-77 glaze.",
    "global": "Global note: the studio closes for the holiday, global-bell-48.",
}
_REFUSED_KINDS = ("beta", "global", "deleted", "unknown")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "_Vault":
    monkeypatch.delenv(_KEY_ENV, raising=False)
    monkeypatch.delenv(MCP_FULL_TOOLS_ENV, raising=False)
    database = resolve_db_path(data_dir=str(tmp_path), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    return _Vault(MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID)))


class _Vault:
    """A throwaway SQLite vault, real agent keys of four kinds, and one source of each state."""

    def __init__(self, context: MCPRuntimeContext) -> None:
        self.context = context
        self.keys = {
            "alpha_trusted": self._mint("trusted_local_agent", "alpha"),
            "alpha_project": self._mint("project_scoped_agent", "alpha"),
            "alpha_admin": self._mint("admin_agent", "alpha"),
            "beta_trusted": self._mint("trusted_local_agent", "beta"),
            "alpha_read_only": self._mint("read_only_agent", "alpha"),
        }
        self.sources: dict[str, str] = {
            "own": self._capture(_TEXT["own"], self.keys["alpha_trusted"]),
            "health": self._capture(_TEXT["health"], self.keys["alpha_trusted"], domain="health"),
            "confidential": self._capture(
                _TEXT["confidential"], self.keys["alpha_admin"], sensitivity="confidential"
            ),
            "deleted": self._capture(_TEXT["deleted"], self.keys["alpha_trusted"]),
            "beta": self._capture(_TEXT["beta"], self.keys["beta_trusted"]),
            "global": self._capture(_TEXT["global"], None),
            "unknown": str(uuid4()),
        }
        self.sql("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (self.sources["deleted"],))

    # -- plumbing -----------------------------------------------------------------------------------------------

    def _mint(self, profile: str, project: str) -> str:
        path = _sqlite_path_from_url(self.context.database_url)
        with sqlite_user_connection(path, _USER_ID) as conn:
            _record, raw = create_agent_key(
                SQLiteVNextStore(conn, _USER_ID),
                user_id=_USER_ID,
                agent_id=f"{profile}-{project}",
                permission_profile=profile,
                project_scope=project,
            )
        return raw

    def wire(self, name: str, arguments: dict[str, object], *, key: str | None) -> dict[str, object]:
        """One ``tools/call`` through the real server object, as a client reads it."""

        if key is None:
            os.environ.pop(_KEY_ENV, None)
        else:
            os.environ[_KEY_ENV] = key
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
        return {"is_error": bool(result["isError"]), "payload": payload}

    def sql(self, query: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with sqlite3.connect(_sqlite_path_from_url(self.context.database_url)) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(query, args).fetchall())

    def count(self, table: str) -> int:
        return int(self.sql(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])

    def _capture(
        self, text: str, key: str | None, *, domain: str = "project", sensitivity: str = "internal"
    ) -> str:
        done = self.wire(
            "alice_capture",
            {"raw_text": text, "title": text[:24], "domain": domain, "sensitivity": sensitivity},
            key=key,
        )
        assert done["is_error"] is False, done
        return str(done["payload"]["source_id"])  # type: ignore[index]

    # -- the doors ----------------------------------------------------------------------------------------------

    def commit(self, who: str | None, refs: list[str], **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "title": f"Pottery entry {uuid4().hex[:8]}",
            "canonical_text": f"Pottery schedule entry {uuid4().hex} is on the wall calendar.",
            "memory_type": "project_fact",
            "domain": "project",
            "sensitivity": "internal",
            "source_refs": refs,
        }
        arguments.update(overrides)
        return self.wire("alice_memory_commit", arguments, key=self.keys[who] if who else None)

    def link_sources(self, memory_id: str) -> list[str]:
        rows = self.sql("SELECT source_id FROM provenance_links WHERE target_id = ?", (memory_id,))
        return [str(row["source_id"]) for row in rows]

    def candidate(self, text: str) -> str:
        """A pending memory in project alpha, made by capture, for the review door."""

        self._capture(text, self.keys["alpha_trusted"])
        rows = self.sql("SELECT id, canonical_text FROM memories WHERE status = 'candidate' ORDER BY created_at DESC")
        return str(next(row["id"] for row in rows if text.split(":")[0] in str(row["canonical_text"])))


def _without_trace_ids(value: object) -> object:
    """The answer with the random ``trace_id`` of each policy record removed, so two answers can be compared."""

    if isinstance(value, dict):
        return {key: _without_trace_ids(item) for key, item in value.items() if key != "trace_id"}
    if isinstance(value, list):
        return [_without_trace_ids(item) for item in value]
    return value


def _is_refusal(answer: dict[str, object]) -> bool:
    return bool(answer["is_error"]) and answer["payload"]["error"]["code"] == "not_found"  # type: ignore[index]


# -- 1. the commit door, every source state and writer ----------------------------------------------------------


# Which source kinds each key may cite. A trusted key reads every domain, so it may cite the health note; a project
# scoped key may not read a restricted domain; only the admin key is cleared for a confidential note. Nobody may
# cite another project's note, a global one (a key bound to a project gets no global fill), a deleted one or one that
# does not exist.
_ADMITTED = {
    "alpha_trusted": {"own", "health"},
    "alpha_project": {"own"},
    "alpha_admin": {"own", "health", "confidential"},
}


@pytest.mark.parametrize("writer", sorted(_ADMITTED))
def test_a_key_bound_commit_cites_only_what_its_key_may_read(vault: _Vault, writer: str) -> None:
    """For each key and each source kind, the commit is stored or refused as the fence says, and every refusal is
    byte for byte the same answer.

    The own-project row proves the call works at all, so a refusal is not an unrelated error. The answers of all
    refused rows are compared as one set: one code, one message, nothing that tells a missing id from a held-back
    one. A refused call leaves no memory and no link behind.

    Mutations, each alone, in ``vnext_source_fence.py`` (``SourceReadFence._admits`` and the resolver) or in
    ``commit()``: pass ``project_scope=()`` and ``require_explicit_project_scope=False`` to the policy call (the
    project control: ``beta`` and ``global`` are stored); only ``require_explicit_project_scope=False`` (the global
    row); ``domains=()`` (the restricted-domain row of the project scoped key); ``sensitivity_allowed=()`` (the
    confidential row of the trusted and project scoped keys); ``return True`` before the policy call (every row);
    ``SourceReadFence.unfenced()`` in place of ``for_identity(identity)`` in ``commit()`` (every row); the
    ``resolve_attachable_sources`` call removed from ``commit()`` (``unknown`` answers ``precondition_failed`` again
    and the foreign ones are stored).
    """

    refused_answers = []
    for kind, source_id in vault.sources.items():
        memories_before, links_before = vault.count("memories"), vault.count("provenance_links")
        answer = vault.commit(writer, [source_id])
        if kind in _ADMITTED[writer]:
            assert answer["is_error"] is False, (writer, kind, answer)
            assert answer["payload"]["status"] == "committed"  # type: ignore[index]
            memory_id = str(answer["payload"]["memory"]["id"])  # type: ignore[index]
            assert vault.link_sources(memory_id) == [source_id], (writer, kind)
        else:
            assert _is_refusal(answer), (writer, kind, answer)
            refused_answers.append(json.dumps(answer, sort_keys=True))
            assert (vault.count("memories"), vault.count("provenance_links")) == (memories_before, links_before), (
                writer,
                kind,
            )
    assert len(refused_answers) == len(vault.sources) - len(_ADMITTED[writer])
    assert len(set(refused_answers)) == 1, "a refused source must not be told apart from a missing one"
    assert json.loads(refused_answers[0])["payload"]["error"]["message"] == _FIXED_MESSAGE


def test_a_refused_caller_is_told_nothing_about_which_ids_exist(vault: _Vault) -> None:
    """A key that the policy refuses outright (read-only writes nothing) gets the same answer for a source that exists
    in another project and one that does not exist.

    Mutation: move the ``resolve_attachable_sources`` call in ``commit()`` above the ``decision.write_mode ==
    "reject"`` branch. The existing id then answers differently from the missing one, or both answer ``not_found``
    where the policy refusal should come first.
    """

    beta = _without_trace_ids(vault.commit("alpha_read_only", [vault.sources["beta"]]))
    unknown = _without_trace_ids(vault.commit("alpha_read_only", [vault.sources["unknown"]]))
    own = _without_trace_ids(vault.commit("alpha_read_only", [vault.sources["own"]]))
    assert beta == unknown == own
    assert not _is_refusal(beta), "the policy refusal comes first, so the source check must not have run"
    assert beta["payload"]["status"] == "rejected"  # type: ignore[index]


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("confirmation_required", {"confidence": 0.7}),
        ("review_required", {"source_type": "web_page"}),
    ],
)
def test_a_pending_commit_is_held_to_the_same_fence(vault: _Vault, label: str, overrides: dict[str, object]) -> None:
    """A write that waits for confirmation or review stores the ref on its row, so it meets the fence too.

    The own-project call stores a pending row, proving the pending path is reached. The other project's source, a
    global one and a missing one are refused with the one answer and store nothing.

    Mutation: call ``resolve_attachable_sources`` only inside ``_create_committed_memory`` and not in ``commit()``
    (the check then runs for a plain commit only). The pending rows are stored with the foreign id.
    """

    rows_before = vault.count("memories")
    pending = vault.commit("alpha_trusted", [vault.sources["own"]], **overrides)
    assert pending["payload"]["status"] == label  # type: ignore[index]
    assert vault.count("memories") == rows_before + 1
    refused = []
    for kind in ("beta", "global", "unknown"):
        before = vault.count("memories")
        answer = vault.commit("alpha_trusted", [vault.sources[kind]], **overrides)
        assert _is_refusal(answer), (label, kind, answer)
        refused.append(json.dumps(answer, sort_keys=True))
        assert vault.count("memories") == before
    assert len(set(refused)) == 1


def test_a_source_ref_of_the_form_the_commit_accepts_is_normalised_before_the_check(vault: _Vault) -> None:
    """``source:<uuid>`` and an upper-case uuid name the same source as the bare lower-case id.

    Mutation: remove ``.removeprefix("source:")`` in ``source_uuids_in_ref`` (the ref is then not read as an id, so
    nothing is checked and the commit is stored), or drop the ``str(UUID(...))`` normalisation (the upper-case form
    is looked up verbatim, matches no row and is refused as missing, so the own-project upper-case row fails).
    """

    beta = vault.sources["beta"]
    own = vault.sources["own"]
    for ref in (f"source:{beta}", beta.upper(), f"source:{beta.upper()}"):
        assert _is_refusal(vault.commit("alpha_trusted", [ref])), ref
    stored = vault.commit("alpha_trusted", [f"source:{own}", own.upper()])
    assert stored["is_error"] is False
    assert vault.link_sources(str(stored["payload"]["memory"]["id"])) == [own]  # type: ignore[index]


def test_free_form_refs_are_still_accepted_and_one_bad_id_among_good_ones_refuses_the_call(vault: _Vault) -> None:
    """A ref that names no id (a URL, a label) was never a stored source and is stored as before. One id outside the
    fence among good refs refuses the whole call.

    Mutation: make ``source_uuids_in_ref`` raise or return the text for a ref that is not a uuid (the URL row fails),
    or ``break`` out of the loop in ``resolve_attachable_sources`` at the first admitted id (the mixed row stores).
    """

    free = vault.commit("alpha_trusted", ["https://example.org/notes/1", "meeting notes 2026-10-03"])
    assert free["is_error"] is False
    assert vault.link_sources(str(free["payload"]["memory"]["id"])) == []  # type: ignore[index]
    mixed = vault.commit("alpha_trusted", [vault.sources["own"], vault.sources["beta"]])
    assert _is_refusal(mixed)


def test_every_id_in_a_nested_ref_is_checked_not_only_the_one_that_gets_the_link() -> None:
    """A ref that is an object may name several ids, and the row stores the whole object. The link goes to the first
    id, so the second one rides along unchecked unless the fence reads all of them.

    The store here is a stub with one project's source and one of another project, and the identity is a key bound
    to the first project.

    Mutation: in ``resolve_attachable_sources`` check ``linked`` instead of ``wanted`` (only the first id of each ref).
    """

    own, other = str(uuid4()), str(uuid4())
    store = _StubStore({own: _source(own, scope=["alpha"]), other: _source(other, scope=["beta"])})
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    with pytest.raises(SourceRefNotFoundError):
        resolve_attachable_sources(store, [{"source_ids": [own, other]}], fence=fence)
    assert resolve_attachable_sources(store, [{"source_ids": [own]}], fence=fence).ids == (own,)
    # The link goes to the first id of each ref, as a commit always did.
    both = resolve_attachable_sources(store, [{"source_ids": [own]}, {"source_id": own}], fence=SourceReadFence.unfenced())
    assert both.ids == (own,)


def test_a_nested_ref_with_two_admitted_ids_is_accepted_and_links_only_the_first() -> None:
    """The other half of the nested-ref rule: every id is checked, and the link still goes to the first id of each
    ref. A nested ref naming two ids the key may read is accepted, links the first, and a second ref of its own links
    its own first id.

    Mutations: in ``resolve_attachable_sources`` return ``wanted`` instead of ``linked`` (the second id is linked too,
    so the first assertion fails), or look rows up for ``linked`` only (the second admitted id is then missing from
    the rows and the call is refused).
    """

    first, second = str(uuid4()), str(uuid4())
    store = _StubStore({first: _source(first, scope=["alpha"]), second: _source(second, scope=["alpha"])})
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    assert resolve_attachable_sources(store, [{"source_ids": [first, second]}], fence=fence).ids == (first,)
    assert resolve_attachable_sources(
        store, [{"source_ids": [first, second]}, {"source_id": second}], fence=fence
    ).ids == (first, second)


# -- 2. what the fence admits, by direct call, one control at a time ---------------------------------------------


class _StubStore:
    """Returns every row it holds, deleted or not, the way a store without the deleted filter would."""

    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self._rows = rows

    def get_sources_by_ids(self, source_ids: list[str]) -> list[dict[str, object]]:
        return [self._rows[source_id] for source_id in source_ids if source_id in self._rows]


class _SingleReadStore:
    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self._rows = rows

    def get_source(self, source_id: str) -> dict[str, object] | None:
        return self._rows.get(source_id)


def _source(
    source_id: str,
    *,
    scope: list[str],
    domain: str = "project",
    sensitivity: str = "internal",
    deleted_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": source_id,
        "domain": domain,
        "sensitivity": sensitivity,
        "deleted_at": deleted_at,
        "metadata_json": {"project_scope": scope},
    }


def _bound_identity(profile: str, project: str) -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"{profile}-{project}",
        permission_profile=profile,
        project_scope=(project,),
        auth="agent_api_key",
        project_scope_locked=True,
    )


def test_a_deleted_source_is_refused_by_the_fence_itself_and_not_only_by_the_store() -> None:
    """The store leaves a deleted source out of ``get_source`` and ``get_sources_by_ids``, and the fence refuses one
    anyway when a store hands it over. Either layer alone leaves the end-to-end answer right, so each has its own
    test (this one and ``test_the_store_leaves_a_deleted_source_out``).

    Mutation: remove the ``deleted_at`` check in ``SourceReadFence._admits``.
    """

    deleted = _source(str(uuid4()), scope=["alpha"], deleted_at="2026-01-01T00:00:00Z")
    live = _source(str(uuid4()), scope=["alpha"])
    for fence in (
        SourceReadFence.unfenced(),
        SourceReadFence.for_identity(_bound_identity("admin_agent", "alpha")),
    ):
        assert fence.admits(live) is True
        assert fence.admits(deleted) is False
        store = _StubStore({str(deleted["id"]): deleted})
        with pytest.raises(SourceRefNotFoundError):
            resolve_attachable_sources(store, [str(deleted["id"])], fence=fence)


def test_the_store_leaves_a_deleted_source_out(vault: _Vault) -> None:
    """``get_source`` and ``get_sources_by_ids`` do not return a soft-deleted source, so a vault with one refuses it
    as missing even before the fence runs.

    Mutation: remove ``AND deleted_at IS NULL`` from ``get_source`` and ``get_sources_by_ids`` in ``sqlite_store.py``.
    """

    deleted = vault.sources["deleted"]
    live = vault.sources["own"]
    path = _sqlite_path_from_url(vault.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        assert store.get_source(deleted) is None
        assert store.get_source(live) is not None
        assert [row["id"] for row in store.get_sources_by_ids([deleted, live])] == [live]


@pytest.mark.parametrize(
    ("label", "profile", "source", "admitted"),
    [
        ("own project", "trusted_local_agent", dict(scope=["alpha"]), True),
        ("other project", "trusted_local_agent", dict(scope=["beta"]), False),
        ("global", "trusted_local_agent", dict(scope=[]), False),
        ("shared with another project", "trusted_local_agent", dict(scope=["alpha", "beta"]), False),
        ("restricted domain, trusted", "trusted_local_agent", dict(scope=["alpha"], domain="health"), True),
        ("restricted domain, project scoped", "project_scoped_agent", dict(scope=["alpha"], domain="health"), False),
        ("unknown domain, project scoped", "project_scoped_agent", dict(scope=["alpha"], domain="unknown"), True),
        ("above the ceiling", "trusted_local_agent", dict(scope=["alpha"], sensitivity="confidential"), False),
        ("above the ceiling, project scoped", "project_scoped_agent", dict(scope=["alpha"], sensitivity="regulated"), False),
        ("at the ceiling", "trusted_local_agent", dict(scope=["alpha"], sensitivity="private"), True),
        ("admin above the default ceiling", "admin_agent", dict(scope=["alpha"], sensitivity="confidential"), True),
        ("read only profile, internal", "read_only_agent", dict(scope=["alpha"], sensitivity="internal"), True),
        ("read only profile, private", "read_only_agent", dict(scope=["alpha"], sensitivity="private"), False),
    ],
)
def test_the_fence_admits_what_the_key_may_read_and_nothing_else(
    label: str, profile: str, source: dict[str, object], admitted: bool
) -> None:
    """One control at a time on a key bound to ``alpha``: project scope (other project, global, shared), domain
    (restricted, by profile), sensitivity ceiling (by profile). The own-project and at-the-ceiling rows are the
    controls that keep a blanket refusal from passing.

    Mutations: as in ``test_a_key_bound_commit_cites_only_what_its_key_may_read``, now by direct call, so each shows
    in the row of its own control.
    """

    fence = SourceReadFence.for_identity(_bound_identity(profile, "alpha"))
    row = _source(str(uuid4()), **source)  # type: ignore[arg-type]
    assert fence.admits(row) is admitted, label


def test_the_owner_is_not_fenced_and_a_keyless_declaration_is_not_a_key(vault: _Vault) -> None:
    """The owner (a call with no agent identity) may cite any live source, another project's included; that is the
    owner's own vault. A keyless call that only declares a profile and a project is held to the profile's ceiling and
    domains, but its declared project is not enforced, exactly as for every other read and write on a keyless install.

    Mutations: make ``_admits`` also refuse a source outside ``identity.project_scope`` for an identity that is not
    locked (the declared project then refuses the other project's source, and the keyless row fails), and make
    ``SourceReadFence.unfenced()`` return a fence built from a locked identity bound to a project (the owner then
    cannot cite the other project's source, and the owner row fails).
    """

    beta = vault.sources["beta"]
    owner = vault.commit(None, [beta])
    assert owner["is_error"] is False
    assert vault.link_sources(str(owner["payload"]["memory"]["id"])) == [beta]  # type: ignore[index]
    assert _is_refusal(vault.commit(None, [vault.sources["deleted"]]))
    assert _is_refusal(vault.commit(None, [vault.sources["unknown"]]))

    declared = {
        "agent_id": "keyless-agent",
        "agent_type": "coding_agent",
        "permission_profile": "trusted_local_agent",
        "project_scope": ["alpha"],
    }
    other_project = vault.wire(
        "alice_memory_commit",
        {
            "title": "Declared agent entry",
            "canonical_text": "Declared agent entry lists the pottery wall calendar.",
            "memory_type": "project_fact",
            "domain": "project",
            "sensitivity": "internal",
            "source_refs": [beta],
            **declared,
        },
        key=None,
    )
    assert other_project["is_error"] is False, "a declared project is not a key and is not enforced"
    above = vault.wire(
        "alice_memory_commit",
        {
            "title": "Declared agent confidential",
            "canonical_text": "Declared agent entry cites the confidential supplier note.",
            "memory_type": "project_fact",
            "domain": "project",
            "sensitivity": "internal",
            "source_refs": [vault.sources["confidential"]],
            **declared,
        },
        key=None,
    )
    assert _is_refusal(above), "the ceiling of the declared profile applies to a keyless agent"


def test_a_store_that_cannot_look_a_source_up_refuses_every_id_and_one_that_can_look_up_one_at_a_time_works() -> None:
    """The Postgres store has ``get_sources_by_ids``, a minimal store only ``get_source``. A store with neither cannot
    prove a source exists, so a ref that names an id is refused (a ref that names no id still passes).

    Mutation: in ``_rows_by_id`` return the ids unchecked when the store has neither method, or drop the
    ``get_source`` fallback.
    """

    source_id = str(uuid4())
    row = _source(source_id, scope=["alpha"])
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    assert resolve_attachable_sources(_SingleReadStore({source_id: row}), [source_id], fence=fence).ids == (source_id,)
    assert resolve_attachable_sources(_StubStore({source_id: row}), [source_id], fence=fence).ids == (source_id,)
    for any_fence in (fence, SourceReadFence.unfenced()):
        with pytest.raises(SourceRefNotFoundError):
            resolve_attachable_sources(object(), [source_id], fence=any_fence)
        assert resolve_attachable_sources(object(), ["https://example.org/x"], fence=any_fence).ids == ()


def test_rows_whose_id_is_a_uuid_object_are_matched_as_the_postgres_store_returns_them() -> None:
    """The Postgres store hands back ``id`` as a ``UUID`` object and the SQLite store as text. The lookup keys the rows
    by the text of the id, so both answer a request that names the id as text. No Postgres runs here, so this stub
    stands in for the one thing the two stores do differently.

    Mutation: drop the ``str(...)`` around ``row.get("id")`` in ``_rows_by_id``. The row is then never found and the own
    source is refused as missing.
    """

    source_id = uuid4()
    row = _source(str(source_id), scope=["alpha"])
    row["id"] = source_id
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    assert resolve_attachable_sources(_StubStore({str(source_id): row}), [str(source_id)], fence=fence).ids == (
        str(source_id),
    )


def test_one_message_for_every_refusal() -> None:
    """Missing, deleted and outside the fence raise one class with one message, so the text cannot tell them apart
    even in a log line that reaches a client by mistake.

    Mutation: raise ``SourceRefNotFoundError`` with a different argument at one of the raise sites, or put the id in
    the message.
    """

    missing, deleted, other = str(uuid4()), str(uuid4()), str(uuid4())
    store = _StubStore(
        {
            deleted: _source(deleted, scope=["alpha"], deleted_at="2026-01-01T00:00:00Z"),
            other: _source(other, scope=["beta"]),
        }
    )
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    messages = set()
    for source_id in (missing, deleted, other):
        with pytest.raises(SourceRefNotFoundError) as caught:
            resolve_attachable_sources(store, [source_id], fence=fence)
        messages.add(str(caught.value))
    assert messages == {SOURCE_REF_NOT_FOUND_MESSAGE}


class _StubMemoryStore:
    """Returns the memory rows it holds, deleted or not, and records every id it was asked for."""

    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self._rows = rows
        self.asked: list[str] = []

    def get_memory(self, memory_id: str) -> dict[str, object] | None:
        self.asked.append(memory_id)
        return self._rows.get(memory_id)


def _memory_row(
    memory_id: str,
    *,
    scope: list[str],
    domain: str = "project",
    sensitivity: str = "internal",
    deleted_at: str | None = None,
) -> dict[str, object]:
    return {
        "id": memory_id,
        "domain": domain,
        "sensitivity": sensitivity,
        "deleted_at": deleted_at,
        "metadata_json": {"project_scope": scope},
    }


def test_a_deleted_memory_is_refused_by_the_fence_itself_and_not_only_by_the_store() -> None:
    """The stores leave a deleted memory out of ``get_memory``, and the fence refuses one anyway when a store hands it
    over. Either layer alone leaves the end-to-end answer right, so this test covers the fence and
    ``test_the_store_leaves_a_deleted_memory_out`` covers the store.

    Mutation: remove the ``deleted_at`` check in ``SourceReadFence._admits``.
    """

    deleted = _memory_row(str(uuid4()), scope=["alpha"], deleted_at="2026-01-01T00:00:00Z")
    live = _memory_row(str(uuid4()), scope=["alpha"])
    for fence in (
        SourceReadFence.unfenced(),
        SourceReadFence.for_identity(_bound_identity("admin_agent", "alpha")),
    ):
        assert fence.admits_memory(live) is True
        assert fence.admits_memory(deleted) is False
        store = _StubMemoryStore({str(deleted["id"]): deleted})
        with pytest.raises(MemoryRefNotFoundError):
            resolve_attachable_memory_id(store, str(deleted["id"]), fence=fence)


def test_the_store_leaves_a_deleted_memory_out(vault: _Vault) -> None:
    """``get_memory`` does not return a soft-deleted memory, so the open-loop door refuses one as missing before the
    fence runs. Pinned because the fence's own deleted check is then a second layer.

    Mutation: remove ``AND deleted_at IS NULL`` from ``get_memory`` in ``vnext_stores/sqlite/memory_access.py``.
    """

    ids = _memories(vault)
    path = _sqlite_path_from_url(vault.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        store = SQLiteVNextStore(conn, _USER_ID)
        assert store.get_memory(ids["deleted"]) is None
        assert store.get_memory(ids["own"]) is not None


@pytest.mark.parametrize(
    ("label", "profile", "memory", "admitted"),
    [
        ("own project", "trusted_local_agent", dict(scope=["alpha"]), True),
        ("other project", "trusted_local_agent", dict(scope=["beta"]), False),
        ("global", "trusted_local_agent", dict(scope=[]), False),
        ("shared with another project", "trusted_local_agent", dict(scope=["alpha", "beta"]), False),
        ("restricted domain, trusted", "trusted_local_agent", dict(scope=["alpha"], domain="health"), True),
        ("restricted domain, project scoped", "project_scoped_agent", dict(scope=["alpha"], domain="health"), False),
        ("above the ceiling", "trusted_local_agent", dict(scope=["alpha"], sensitivity="confidential"), False),
        ("at the ceiling", "trusted_local_agent", dict(scope=["alpha"], sensitivity="private"), True),
        ("admin above the default ceiling", "admin_agent", dict(scope=["alpha"], sensitivity="confidential"), True),
        ("read only profile, private", "read_only_agent", dict(scope=["alpha"], sensitivity="private"), False),
    ],
)
def test_the_fence_admits_a_memory_by_the_same_controls_it_applies_to_a_source(
    label: str, profile: str, memory: dict[str, object], admitted: bool
) -> None:
    """The memory side of the fence, one control at a time on a key bound to ``alpha``: project scope, domain and
    sensitivity ceiling. The own-project and at-the-ceiling rows are the controls that keep a blanket refusal from
    passing.

    Mutations: ``admits_memory`` returns True, returns False, or ignores the row's scope (passes ``("alpha",)``).
    """

    fence = SourceReadFence.for_identity(_bound_identity(profile, "alpha"))
    row = _memory_row(str(uuid4()), **memory)  # type: ignore[arg-type]
    assert fence.admits_memory(row) is admitted, label


def test_a_memory_scope_is_read_the_way_explain_reads_it() -> None:
    """``alice_explain`` reads the scope of a memory it is given with ``resource_project_scope``, where a root
    ``project_scope`` key beats the metadata envelope. The fence reads it the same way, so a row whose root scope is
    ``alpha`` and whose envelope says ``beta`` is admitted to a key bound to ``alpha`` and the reverse is refused. A
    source is read the other way round (the envelope first), so this is not the source rule.

    Mutation: use ``source_project_scope(memory)`` in ``admits_memory``.
    """

    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    root_alpha = {**_memory_row(str(uuid4()), scope=["beta"]), "project_scope": ["alpha"]}
    root_beta = {**_memory_row(str(uuid4()), scope=["alpha"]), "project_scope": ["beta"]}
    assert fence.admits_memory(root_alpha) is True
    assert fence.admits_memory(root_beta) is False


def test_the_single_id_resolvers_check_the_value_before_the_store_sees_it_and_return_the_canonical_id() -> None:
    """The open-loop route passes one id per column. A value that is not a UUID is refused without a store call (the
    Postgres store casts the id and would fail with a 500), and a UUID in another spelling is looked up and returned
    in canonical form, which is what the route then stores. Every refusal of a kind carries its one fixed message.

    Mutations: in ``resolve_attachable_memory_id`` pass the raw value to ``get_memory`` or skip the UUID parse; in
    ``resolve_attachable_source_id`` return the raw value, or return an empty id when the value names none.
    """

    memory_id, source_id = str(uuid4()), str(uuid4())
    memories = _StubMemoryStore({memory_id: _memory_row(memory_id, scope=["alpha"])})
    sources = _StubStore({source_id: _source(source_id, scope=["alpha"])})
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))

    for bad in ("not-a-uuid", "", "   ", "12345"):
        with pytest.raises(MemoryRefNotFoundError) as caught_memory:
            resolve_attachable_memory_id(memories, bad, fence=fence)
        assert str(caught_memory.value) == MEMORY_REF_NOT_FOUND_MESSAGE
        with pytest.raises(SourceRefNotFoundError) as caught_source:
            resolve_attachable_source_id(sources, bad, fence=fence)
        assert str(caught_source.value) == SOURCE_REF_NOT_FOUND_MESSAGE
    assert memories.asked == [], "a value that is not a UUID never reaches the store"

    assert resolve_attachable_memory_id(memories, memory_id.upper(), fence=fence) == memory_id
    assert memories.asked == [memory_id]
    assert resolve_attachable_memory_id(memories, f"  {memory_id}  ", fence=fence) == memory_id
    assert resolve_attachable_source_id(sources, source_id.replace("-", ""), fence=fence) == source_id

    other = str(uuid4())
    with pytest.raises(MemoryRefNotFoundError):
        resolve_attachable_memory_id(memories, other, fence=fence)
    with pytest.raises(MemoryRefNotFoundError):
        resolve_attachable_memory_id(object(), memory_id, fence=SourceReadFence.unfenced())
    with pytest.raises(SourceRefNotFoundError):
        resolve_attachable_source_id(object(), source_id, fence=SourceReadFence.unfenced())


def test_the_fence_and_explain_authorize_under_one_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fence is the test ``alice_explain`` applies, so both read the action from one constant. Pinned from three
    sides: the fence asks the policy engine with that constant, ``_authorize_explain_resource`` names the constant and
    no string literal, and the constant is the read action the engine knows.

    Mutations: put ``"memory.recall"`` (or any other literal) in ``_admits``, or put the literal ``"memory.audit"``
    back in ``_authorize_explain_resource``.
    """

    from alicebot_api import vnext_source_fence
    from alicebot_api.vnext_agent_control import evaluate_agent_policy as real_evaluate

    asked: list[str] = []

    def recording(**kwargs: object):  # type: ignore[no-untyped-def]
        asked.append(str(kwargs["action"]))
        return real_evaluate(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(vnext_source_fence, "evaluate_agent_policy", recording)
    fence = SourceReadFence.for_identity(_bound_identity("trusted_local_agent", "alpha"))
    fence.admits(_source(str(uuid4()), scope=["alpha"]))
    fence.admits_memory(_memory_row(str(uuid4()), scope=["alpha"]))
    assert asked == [EXPLAIN_DISCLOSURE_ACTION, EXPLAIN_DISCLOSURE_ACTION]
    assert EXPLAIN_DISCLOSURE_ACTION == "memory.audit"

    tree = ast.parse((_SRC / "mcp" / "evidence_artifacts.py").read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_authorize_explain_resource"
    )
    literals = [node.value for node in ast.walk(function) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    assert not [text for text in literals if text.startswith("memory.")], literals
    actions = [
        keyword.value
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "action"
    ]
    assert [ast.unparse(action) for action in actions] == ["EXPLAIN_DISCLOSURE_ACTION"]


# -- 3. the review door -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["edit-and-approve", "supersede-existing"])
def test_the_review_door_holds_provenance_to_the_same_fence(vault: _Vault, action: str) -> None:
    """``alice_memory_correct`` with a ``provenance`` (edit) or ``replacement_provenance`` (supersede) attaches a link
    by id, and only an admin key may call it. An admin key bound to ``alpha`` may cite its own project's source and
    nothing outside its fence, and a refused call leaves the candidate and its links as they were.

    The cases are those of the commit door: every refusal is ``not_found`` with the one message.

    Mutations: replace ``SourceReadFence.for_identity(identity)`` with ``SourceReadFence.unfenced()`` in the
    ``provenance`` call of ``_vnext_memory_correct`` (``mcp/review.py``), which fails the ``edit-and-approve`` rows,
    and in the ``replacement_provenance`` call, which fails the ``supersede-existing`` rows.
    """

    field = "provenance" if action == "edit-and-approve" else "replacement_provenance"
    base: dict[str, object] = (
        {"title": "Alpha lathe serviced in May"}
        if action == "edit-and-approve"
        else {"replacement_title": "Alpha lathe v2", "replacement_body": {"text": "Alpha lathe v2 is serviced in June."}}
    )
    refused = []
    for kind in ("beta", "global", "deleted", "unknown"):
        candidate = vault.candidate(f"Review door {kind}: the alpha lathe is serviced in May")
        links_before = vault.count("provenance_links")
        answer = vault.wire(
            "alice_memory_correct",
            {
                "action": action,
                "review_item_id": candidate,
                "reason": "check",
                **base,
                field: {"source_id": vault.sources[kind], "evidence_role": "supports", "confidence": 0.8},
            },
            key=vault.keys["alpha_admin"],
        )
        assert _is_refusal(answer), (action, kind, answer)
        refused.append(json.dumps(answer, sort_keys=True))
        assert vault.count("provenance_links") == links_before
        assert vault.sql("SELECT status FROM memories WHERE id = ?", (candidate,))[0]["status"] == "candidate"
    assert len(set(refused)) == 1

    candidate = vault.candidate("Review door own: the alpha lathe is serviced in May")
    done = vault.wire(
        "alice_memory_correct",
        {
            "action": action,
            "review_item_id": candidate,
            "reason": "check",
            **base,
            field: {"source_id": vault.sources["own"], "evidence_role": "supports", "confidence": 0.8},
        },
        key=vault.keys["alpha_admin"],
    )
    assert done["is_error"] is False, done
    cited = vault.sql(
        "SELECT target_id FROM provenance_links WHERE source_id = ? AND evidence_role = 'supports'",
        (vault.sources["own"],),
    )
    assert len(cited) == 1


def test_the_owner_may_cite_another_projects_source_through_the_review_door(vault: _Vault) -> None:
    """The keyless owner is not fenced there either, and a deleted or missing source is still refused.

    Mutation: make ``SourceReadFence.unfenced()`` return a fence built from a locked identity bound to a project
    (``for_identity(None)`` returns it, so the owner of the review door then cannot cite another project's source).
    """

    candidate = vault.candidate("Owner review: the alpha lathe is serviced in May")
    for kind in ("deleted", "unknown"):
        answer = vault.wire(
            "alice_memory_correct",
            {
                "action": "edit-and-approve",
                "review_item_id": candidate,
                "title": "Alpha lathe serviced in May",
                "provenance": {"source_id": vault.sources[kind]},
            },
            key=None,
        )
        assert _is_refusal(answer), kind
    done = vault.wire(
        "alice_memory_correct",
        {
            "action": "edit-and-approve",
            "review_item_id": candidate,
            "title": "Alpha lathe serviced in May",
            "provenance": {"source_id": vault.sources["beta"]},
        },
        key=None,
    )
    assert done["is_error"] is False, done


def test_a_key_cannot_mark_another_projects_source_as_corrected(vault: _Vault) -> None:
    """On v0.20.0 an admin key bound to ``alpha`` could attach a ``quoted_from`` link, with a quote that covers the
    excerpt of a source of project ``beta``, to an ``alpha`` memory and then supersede that memory. The ``beta`` key's
    recall then showed its own source with ``derived_memory_corrected: true``, so one project wrote into another's
    reads. The attach is refused now and the ``beta`` excerpt stays unmarked.

    The control does the same inside ``alpha`` with its own source and shows the label does appear when the link is
    allowed, so the assertion that it does not appear for ``beta`` is not vacuous.

    Mutation: replace ``SourceReadFence.for_identity(identity)`` with ``SourceReadFence.unfenced()`` in the
    ``provenance`` call of ``_vnext_memory_correct`` (``mcp/review.py``).
    """

    def corrected_labels(key: str, query: str) -> list[object]:
        answer = vault.wire("alice_recall", {"query": query}, key=key)
        return [source.get("derived_memory_corrected") for source in answer["payload"]["sources"]]  # type: ignore[index]

    def attach_and_supersede(source_kind: str) -> dict[str, object]:
        candidate = vault.candidate(f"Quoted door {source_kind}: the alpha glaze shelf was reorganised on Friday")
        attached = vault.wire(
            "alice_memory_correct",
            {
                "action": "edit-and-approve",
                "review_item_id": candidate,
                "title": "Alpha glaze shelf",
                "provenance": {
                    "source_id": vault.sources[source_kind],
                    "evidence_role": "quoted_from",
                    "quote": _TEXT[source_kind],
                    "confidence": 0.9,
                },
            },
            key=vault.keys["alpha_admin"],
        )
        if attached["is_error"]:
            return attached
        vault.wire(
            "alice_memory_correct",
            {
                "action": "supersede-existing",
                "review_item_id": candidate,
                "replacement_title": "Alpha glaze shelf v2",
                "replacement_body": {"text": "The alpha glaze shelf was reorganised on Saturday."},
                "reason": "moved",
            },
            key=vault.keys["alpha_admin"],
        )
        return attached

    assert corrected_labels(vault.keys["beta_trusted"], "kumquat-harbor-77") == [None]
    assert _is_refusal(attach_and_supersede("beta"))
    assert corrected_labels(vault.keys["beta_trusted"], "kumquat-harbor-77") == [None]

    assert corrected_labels(vault.keys["alpha_trusted"], "saltwhite-glaze-12") == [None]
    assert attach_and_supersede("own")["is_error"] is False
    assert corrected_labels(vault.keys["alpha_trusted"], "saltwhite-glaze-12") == [True]


# -- 3b. the HTTP commit route ----------------------------------------------------------------------------------


def test_the_http_commit_route_answers_one_404_for_every_refusal(vault: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /v0/vnext/memories/commit`` over the same vault, with the real SQLite store behind the route and a real
    agent key. A source that is missing, deleted, global or of another project answers 404 with the public
    ``not_found`` body, the same body for each, and stores nothing. The own-project call answers 201 and stores the
    link.

    On v0.20.0 the own-project and the foreign calls both answered 201 and an unknown id raised out of the route.

    The MCP wire rolls a failed call back, which would hide a memory written before the check ran, so this test is also
    the one that pins "before anything is written": the route commits when it returns, and a memory stored ahead of the
    refusal would stay.

    Mutations: remove the ``except SourceRefNotFoundError`` clause of ``commit_vnext_memory``
    (``routers/vnext_memories.py``), so the error leaves the route as an exception; and resolve the sources inside
    ``_create_committed_memory`` after the row is written, instead of in ``commit()``, so each refused call leaves a
    memory behind.
    """

    from contextlib import contextmanager

    from alicebot_api.config import Settings
    from alicebot_api.routers import vnext_memories as router

    path = _sqlite_path_from_url(vault.context.database_url)

    @contextmanager
    def connection(_database_url: object, current_user_id: object):  # type: ignore[no-untyped-def]
        with sqlite_user_connection(path, str(current_user_id)) as conn:
            yield conn

    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(router, "user_connection", connection)
    monkeypatch.setattr(router, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, _USER_ID))

    def post(source_id: str) -> tuple[int, dict[str, object]]:
        response = router.commit_vnext_memory(
            router.VNextMemoryCommitRequest(
                user_id=UUID(_USER_ID),
                title=f"Pottery entry {uuid4().hex[:8]}",
                canonical_text=f"Pottery schedule entry {uuid4().hex} is on the wall calendar.",
                memory_type="project_fact",
                domain="project",
                sensitivity="internal",
                confidence=0.95,
                source_refs=[source_id],
                agent_id="trusted_local_agent-alpha",
            ),
            authorization=f"Bearer {vault.keys['alpha_trusted']}",
        )
        return response.status_code, json.loads(response.body)

    memories_before = vault.count("memories")
    status, body = post(vault.sources["own"])
    assert status == 201, body
    assert body["status"] == "committed"
    assert vault.link_sources(str(body["memory"]["id"])) == [vault.sources["own"]]  # type: ignore[index]

    refused = []
    for kind in _REFUSED_KINDS:
        status, body = post(vault.sources[kind])
        assert status == 404, (kind, status, body)
        refused.append(json.dumps(body, sort_keys=True))
    assert len(set(refused)) == 1
    assert json.loads(refused[0])["detail"]["code"] == "not_found"
    assert vault.count("memories") == memories_before + 1, "only the own-project call stored a memory"


# -- 3c. the HTTP open-loop route -------------------------------------------------------------------------------


_LOOP_AGENT_IDS = {
    "alpha_trusted": "trusted_local_agent-alpha",
    "alpha_project": "project_scoped_agent-alpha",
    "alpha_admin": "admin_agent-alpha",
    "alpha_read_only": "read_only_agent-alpha",
    "beta_trusted": "trusted_local_agent-beta",
}


class _OwnerVault(_Vault):
    """A throwaway SQLite vault with no agent key at all, so a keyless call is the owner's."""

    def __init__(self, context: MCPRuntimeContext) -> None:
        self.context = context
        self.keys = {}


def _post_open_loop(vault: _Vault, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """``POST /v0/vnext/open-loops`` with the real SQLite store behind the route, as the commit-route test does.

    The route is Postgres only, so the router module's settings, connection and store are swapped for the vault's.
    The returned function posts one loop as a key of the vault (or as the owner with ``None``) and returns the status
    and the decoded body.
    """

    from contextlib import contextmanager

    from alicebot_api.config import Settings
    from alicebot_api.routers import vnext_projects as router

    path = _sqlite_path_from_url(vault.context.database_url)

    @contextmanager
    def connection(_database_url: object, current_user_id: object):  # type: ignore[no-untyped-def]
        with sqlite_user_connection(path, str(current_user_id)) as conn:
            yield conn

    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(router, "user_connection", connection)
    monkeypatch.setattr(router, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, _USER_ID))

    def post(who: str | None, **fields: object) -> tuple[int, dict[str, object]]:
        response = router.create_vnext_open_loop(
            router.VNextOpenLoopCreateRequest(
                user_id=UUID(_USER_ID),
                title=f"Loop {uuid4().hex[:8]}",
                agent_id=_LOOP_AGENT_IDS[who] if who else None,
                **fields,  # type: ignore[arg-type]
            ),
            authorization=f"Bearer {vault.keys[who]}" if who else None,
        )
        return response.status_code, json.loads(response.body)

    return post


def _memories(vault: _Vault) -> dict[str, str]:
    """One memory of each state the source fence is tested with, under the same kind names as ``vault.sources``."""

    def committed(who: str | None) -> str:
        made = vault.commit(who, [])
        assert made["is_error"] is False, made
        return str(made["payload"]["memory"]["id"])  # type: ignore[index]

    ids = {
        "own": committed("alpha_trusted"),
        "health": committed("alpha_trusted"),
        "confidential": committed("alpha_trusted"),
        "deleted": committed("alpha_trusted"),
        "beta": committed("beta_trusted"),
        "global": committed(None),
        "unknown": str(uuid4()),
    }
    vault.sql("UPDATE memories SET domain = 'health' WHERE id = ?", (ids["health"],))
    vault.sql("UPDATE memories SET sensitivity = 'confidential' WHERE id = ?", (ids["confidential"],))
    vault.sql("UPDATE memories SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ids["deleted"],))
    return ids


def _row_counts(vault: _Vault) -> dict[str, int]:
    """The row count of every table, so a refused call can be shown to have written nothing anywhere."""

    tables = [str(row["name"]) for row in vault.sql("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {name: vault.count(f'"{name}"') for name in sorted(tables) if not name.endswith(("_data", "_idx", "_config", "_docsize", "_content"))}


@pytest.mark.parametrize("field", ["source_id", "memory_id"])
@pytest.mark.parametrize("writer", sorted(_ADMITTED))
def test_a_key_bound_open_loop_names_only_a_source_or_memory_its_key_may_read(
    vault: _Vault, monkeypatch: pytest.MonkeyPatch, writer: str, field: str
) -> None:
    """The loop keeps the id it is given, so the id must be one the key could be shown (the readers also withhold
    from a lower reader what the writer was allowed to name, which the other test file pins). Each key tries every
    state of a source and of a memory: its own project (the control that must succeed),
    a restricted domain, above its ceiling, another project, global, deleted and unknown. What is admitted is stored
    and answers 201. Everything else answers one 404 body, and the call writes nothing in any table.

    On v0.20.0 and on main every one of these answered 201 and stored the id, and an unknown id raised the foreign key
    error out of the route, so the id was an existence oracle and the loop attached another project's row.

    Mutations: drop the ``resolve_attachable_source_id`` call (or the ``resolve_attachable_memory_id`` call) in
    ``create_vnext_open_loop`` and store the id as it came; make ``admits`` or ``admits_memory`` return True (a refused
    row is stored); make ``admits_memory`` return False for a bound key (the own row fails); move the
    ``except (SourceRefNotFoundError, MemoryRefNotFoundError)`` clause inside the ``with user_connection`` block (the
    refused call then commits the policy rows it wrote).
    """

    post = _post_open_loop(vault, monkeypatch)
    ids = vault.sources if field == "source_id" else _memories(vault)
    refused_bodies: set[str] = set()
    stored = 0
    for kind, value in ids.items():
        before = _row_counts(vault)
        status, body = post(writer, **{field: value})
        if kind in _ADMITTED[writer]:
            assert status == 201, (writer, kind, body)
            assert body["open_loop"][field] == value  # type: ignore[index]
            stored += 1
            continue
        assert status == 404, (writer, kind, status, body)
        refused_bodies.add(json.dumps(body, sort_keys=True))
        assert _row_counts(vault) == before, (writer, kind, "a refused call wrote something")
    assert len(refused_bodies) == 1, refused_bodies
    assert json.loads(next(iter(refused_bodies)))["detail"]["code"] == "not_found"
    assert stored == len(_ADMITTED[writer])
    kept = [row[field] for row in vault.sql(f"SELECT {field} FROM open_loops WHERE {field} IS NOT NULL")]
    assert sorted(kept) == sorted(ids[kind] for kind in _ADMITTED[writer])


def test_the_owner_may_name_any_live_source_and_memory_in_an_open_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no agent key in the vault a keyless call is the owner's, and the owner is not fenced: a source and a
    memory of another project and a global one are stored. A deleted row and an unknown id still answer 404, as for
    every caller, and a missing id no longer raises out of the route.

    Mutation: make ``SourceReadFence.for_identity(None)`` return a fence built from a bound identity (the owner then
    gets 404 for the other project's rows), or make ``admits`` skip the deleted check.
    """

    database = resolve_db_path(data_dir=str(tmp_path / "owner"), db=None)
    bootstrap_database(database, user_id=_USER_ID, user_email="local@alice")
    owner = _OwnerVault(MCPRuntimeContext(database_url=sqlite_url_for_path(database), user_id=UUID(_USER_ID)))
    post = _post_open_loop(owner, monkeypatch)
    other_project = owner.wire(
        "alice_capture",
        {"raw_text": "Beta kiln log: firing at cone-six-914.", "title": "Beta log", "domain": "project",
         "sensitivity": "internal", "project_scope": ["beta"]},
        key=None,
    )
    assert other_project["is_error"] is False, other_project
    sources = {
        "other_project": str(other_project["payload"]["source_id"]),  # type: ignore[index]
        "global": owner._capture("Global note: the studio closes for the holiday, bell-48.", None),
        "deleted": owner._capture("Deleted note: the old log said ember-chart-91.", None),
    }
    owner.sql("UPDATE sources SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (sources["deleted"],))
    memories = {kind: str(owner.commit(None, [])["payload"]["memory"]["id"]) for kind in ("one", "deleted")}  # type: ignore[index]
    owner.sql("UPDATE memories SET deleted_at = '2026-01-01T00:00:00Z' WHERE id = ?", (memories["deleted"],))

    for kind in ("other_project", "global"):
        status, body = post(None, source_id=sources[kind])
        assert status == 201, (kind, body)
        assert body["open_loop"]["source_id"] == sources[kind]  # type: ignore[index]
    status, body = post(None, memory_id=memories["one"])
    assert status == 201, body
    assert body["open_loop"]["memory_id"] == memories["one"]  # type: ignore[index]
    for refused in (
        {"source_id": sources["deleted"]},
        {"source_id": str(uuid4())},
        {"memory_id": memories["deleted"]},
        {"memory_id": str(uuid4())},
    ):
        status, body = post(None, **refused)
        assert status == 404, (refused, status, body)


def test_a_malformed_id_answers_like_a_missing_one_and_the_stored_id_is_the_one_that_was_checked(
    vault: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value that is not a UUID answers the same 404 as an unknown id, where the Postgres cast would have failed with
    a 500. A value that is one in a spelling the database also reads (upper case, no hyphens) is stored in canonical
    form, so what the fence looked up is what the loop keeps.

    Mutation: store ``request.source_id`` or ``request.memory_id`` instead of the id the resolver returned (the
    canonical-form rows fail), or let the resolver pass a value that is not a UUID.
    """

    post = _post_open_loop(vault, monkeypatch)
    memories = _memories(vault)
    reference = json.dumps(post("alpha_trusted", source_id=vault.sources["unknown"])[1], sort_keys=True)
    for field in ("source_id", "memory_id"):
        for bad in ("not-a-uuid", "12345", "   ", "00000000-0000-0000-0000-00000000000g"):
            status, body = post("alpha_trusted", **{field: bad})
            assert status == 404, (field, bad, body)
            assert json.dumps(body, sort_keys=True) == reference
    for field, own in (("source_id", vault.sources["own"]), ("memory_id", memories["own"])):
        for spelling in (own.upper(), own.replace("-", ""), f"  {own}  "):
            status, body = post("alpha_trusted", **{field: spelling})
            assert status == 201, (field, spelling, body)
            assert body["open_loop"][field] == own  # type: ignore[index]


def test_the_policy_refusal_comes_before_any_id_is_looked_at(vault: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read only key may not create a loop. It gets the same 403 for an id of its own project, one of another
    project and one that does not exist, so a refused caller learns nothing about which ids exist. A key that may
    create a loop and names a foreign id gets the 404, so the two refusals are told apart by the caller's own right and
    not by the id.

    Mutation: move the ``read_fence``/``resolve_attachable_*`` block of ``create_vnext_open_loop`` above the
    ``_vnext_policy_checked`` call (the read only key then gets 403 for its own source and 404 for the others).
    """

    post = _post_open_loop(vault, monkeypatch)
    memories = _memories(vault)
    answers = set()
    for field, ids in (("source_id", vault.sources), ("memory_id", memories)):
        for kind in ("own", "beta", "unknown"):
            status, body = post("alpha_read_only", **{field: ids[kind]})
            assert status == 403, (field, kind, body)
            answers.add(json.dumps(_without_trace_ids(body), sort_keys=True))
    assert len(answers) == 1, answers
    assert post("alpha_trusted", source_id=vault.sources["beta"])[0] == 404


def test_one_refused_id_refuses_the_whole_call_and_a_loop_with_no_ids_is_unchanged(
    vault: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loop may name a source and a memory. If either is refused the loop is not created, and the answer is the one
    404. A loop with neither (the common case) and a loop with both of its own are stored as before.

    Mutation: resolve only the source id (the foreign memory row is stored), or resolve the memory id only when no
    source id is given.
    """

    post = _post_open_loop(vault, monkeypatch)
    memories = _memories(vault)
    before = _row_counts(vault)
    for refused in (
        {"source_id": vault.sources["own"], "memory_id": memories["beta"]},
        {"source_id": vault.sources["beta"], "memory_id": memories["own"]},
        {"source_id": vault.sources["beta"], "memory_id": memories["beta"]},
    ):
        status, body = post("alpha_trusted", **refused)
        assert status == 404, (refused, body)
    assert _row_counts(vault) == before
    status, body = post("alpha_trusted")
    assert status == 201, body
    assert body["open_loop"]["source_id"] is None and body["open_loop"]["memory_id"] is None  # type: ignore[index]
    status, body = post("alpha_trusted", source_id=vault.sources["own"], memory_id=memories["own"])
    assert status == 201, body
    assert body["open_loop"]["source_id"] == vault.sources["own"]  # type: ignore[index]
    assert body["open_loop"]["memory_id"] == memories["own"]  # type: ignore[index]


def test_a_refused_open_loop_id_never_reaches_the_open_loop_list(vault: _Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason the door matters: a loop keeps ``source_id`` and ``memory_id`` and ``alice_open_loops`` returns the
    ones the reader may read. After the refused calls no loop holds the id of another project's source or memory, and
    the loop that names the key's own source is listed with it, so the list is shown to carry the field at all.

    Mutation: any mutation of the matrix test above that stores a foreign id.
    """

    post = _post_open_loop(vault, monkeypatch)
    memories = _memories(vault)
    foreign = [vault.sources["beta"], vault.sources["global"], memories["beta"], memories["global"]]
    for field, ids in (("source_id", [vault.sources["beta"], vault.sources["global"]]), ("memory_id", [memories["beta"], memories["global"]])):
        for value in ids:
            assert post("alpha_trusted", **{field: value})[0] == 404
    assert post("alpha_trusted", source_id=vault.sources["own"])[0] == 201
    listed = vault.wire("alice_open_loops", {"status": "all"}, key=vault.keys["alpha_project"])
    assert listed["is_error"] is False, listed
    text = json.dumps(listed["payload"])
    assert vault.sources["own"] in text
    for value in foreign:
        assert value not in text


# -- 4. what the attached link did to the key's own reads -------------------------------------------------------


def test_what_a_key_may_attach_it_may_also_explain(vault: _Vault) -> None:
    """The fence is the one ``alice_explain`` applies to each source it discloses, so a memory a key committed with a
    link that passed is a memory that key can explain. On v0.20.0 a link to a source outside the fence made the key's
    own explain fail closed, and the same for every other key in the project.

    Every key tries every source kind. Each commit that is stored is explained by the same key, and each of those must
    work, so a fence that admits one source too many shows as an explain that fails. The control plants such a link
    straight into the store, past the door, and shows the explain does fail closed then, so the assertion is not
    vacuous. At least one commit of each key is stored, so the loop is not empty.

    Mutations: make ``_admits`` accept ``allowed_with_filtering`` (the confidential source is then stored for the
    trusted and project scoped keys, and their explain fails), drop the domains control (the health source is stored
    for the project scoped key and its explain fails), or any mutation of the matrix test that lets a foreign source
    through.
    """

    for writer in sorted(_ADMITTED):
        stored = 0
        for kind, source_id in vault.sources.items():
            made = vault.commit(writer, [source_id])
            if made["is_error"]:
                continue
            stored += 1
            memory_id = str(made["payload"]["memory"]["id"])  # type: ignore[index]
            explained = vault.wire("alice_explain", {"memory_id": memory_id}, key=vault.keys[writer])
            assert explained["is_error"] is False, (writer, kind, explained)
        assert stored >= 1, writer

    made = vault.commit("alpha_trusted", [vault.sources["own"]])
    memory_id = str(made["payload"]["memory"]["id"])  # type: ignore[index]
    path = _sqlite_path_from_url(vault.context.database_url)
    with sqlite_user_connection(path, _USER_ID) as conn:
        SQLiteVNextStore(conn, _USER_ID).create_provenance_link(
            {"target_type": "memory", "target_id": memory_id, "source_id": vault.sources["beta"]},
            actor_type="agent",
        )
    control = vault.wire("alice_explain", {"memory_id": memory_id}, key=vault.keys["alpha_trusted"])
    assert control["is_error"] is True


def test_the_foreign_source_id_never_reaches_a_review_by_id(vault: _Vault) -> None:
    """The id of another project's source is not stored on the memory, so a reviewer in the project cannot read it
    from ``alice_memory_review`` by id. On v0.20.0 the link and its id came back there to every key in the project.

    The own-project commit beside it is read back the same way, so the review call is shown to list links at all.

    Mutation: any mutation of the matrix test that stores the foreign link (for example the ``unfenced()`` one in
    ``commit()``).
    """

    beta = vault.sources["beta"]
    assert _is_refusal(vault.commit("alpha_trusted", [beta]))
    made = vault.commit("alpha_trusted", [vault.sources["own"]])
    memory_id = str(made["payload"]["memory"]["id"])  # type: ignore[index]
    reviewed = vault.wire("alice_memory_review", {"review_item_id": memory_id}, key=vault.keys["alpha_project"])
    assert reviewed["is_error"] is False
    links = reviewed["payload"]["review"]["provenance_links"]  # type: ignore[index]
    assert [link["source_id"] for link in links] == [vault.sources["own"]]
    # The beta source has a candidate memory and a link of its own, made when it was captured. What must not exist
    # is any memory the alpha keys committed that names it, or any link from one.
    committed = [dict(row) for row in vault.sql("SELECT * FROM memories WHERE memory_key LIKE 'agentic_memory.%'")]
    assert committed, "the own-project commit above is one of them"
    assert beta not in json.dumps(committed, default=str)
    linked_from_commits = [
        str(row["source_id"])
        for row in vault.sql(
            "SELECT source_id FROM provenance_links WHERE target_id IN "
            "(SELECT id FROM memories WHERE memory_key LIKE 'agentic_memory.%')"
        )
    ]
    assert linked_from_commits == [vault.sources["own"]]


def test_a_link_that_passes_for_a_writer_still_shows_its_id_to_keys_with_a_lower_ceiling(
    vault: _Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fence at write time is the writer's own read fence, not the fence of whoever reads later. An ``admin_agent``
    key may cite a confidential source of its own project, and then a ``trusted_local_agent`` key and a
    ``project_scoped_agent`` key of the same project (both with a lower ceiling) read the memory and an open loop that
    names the source. Each reader of the link now asks the reader's own fence again (``SavedProvenanceReader``,
    ``tests/unit/test_saved_quotes_follow_the_source_fence.py``), so ``alice_memory_review`` by id lists no link and
    names the source's id nowhere, and the context pack's ``supporting_evidence`` is empty, for those keys.
    ``alice_explain`` of the memory still fails for them. The open-loop readers check each reference against the
    reader's own fence too, so the same keys get the loop with ``source_id`` ``null``. The admin key still reads the
    link, the id, the explanation and the loop's ``source_id``.

    The name of this test is the old limit, kept so that the pin changes in place: until these changes a lower ceiling
    saw the id in the review, in the pack and in the open loop.

    Mutations: make ``_admits`` refuse an ``admin_agent`` identity (the admin commit then answers ``not_found``);
    return the stored links in ``_vnext_memory_review`` (``mcp/review.py``), which fails the review assertion; remove
    the ``admits_link`` test in ``_supporting_evidence`` (``vnext_retrieval.py``), which fails the pack assertion; or
    drop the ``withhold_unreadable_references`` call in ``_handle_alice_vnext_open_loops`` (the open-loop assertion
    then fails).
    """

    confidential = vault.sources["confidential"]
    made = vault.commit(
        "alpha_admin",
        [confidential],
        canonical_text="Pottery kiln schedule entry for the saltwhite-glaze-12 cone firing.",
    )
    assert made["is_error"] is False, made
    memory_id = str(made["payload"]["memory"]["id"])  # type: ignore[index]
    status, body = _post_open_loop(vault, monkeypatch)("alpha_admin", source_id=confidential)
    assert status == 201
    loop_id = str(body["open_loop"]["id"])  # type: ignore[index]
    admin = vault.wire("alice_memory_review", {"review_item_id": memory_id}, key=vault.keys["alpha_admin"])
    assert [link["source_id"] for link in admin["payload"]["review"]["provenance_links"]] == [confidential]  # type: ignore[index]
    assert vault.wire("alice_explain", {"memory_id": memory_id}, key=vault.keys["alpha_admin"])["is_error"] is False
    admin_pack = vault.wire(
        "alice_context_pack", {"query": "saltwhite-glaze-12 kiln firing", "max_tokens": 2000}, key=vault.keys["alpha_admin"]
    )["payload"]
    assert [row["source_id"] for row in admin_pack["supporting_evidence"]] == [confidential]  # type: ignore[index]
    for reader in ("alpha_trusted", "alpha_project"):
        key = vault.keys[reader]
        reviewed = vault.wire("alice_memory_review", {"review_item_id": memory_id}, key=key)
        assert reviewed["payload"]["review"]["provenance_links"] == [], reader  # type: ignore[index]
        assert confidential not in json.dumps(reviewed["payload"]), reader
        assert vault.wire("alice_explain", {"memory_id": memory_id}, key=key)["is_error"] is True, reader
        pack = vault.wire(
            "alice_context_pack", {"query": "saltwhite-glaze-12 kiln firing", "max_tokens": 2000}, key=key
        )["payload"]
        assert pack["supporting_evidence"] == [], reader  # type: ignore[index]
        assert confidential not in json.dumps(pack), reader
        assert "cedar-ledger-55" not in json.dumps(pack), reader
        listed = vault.wire("alice_open_loops", {"status": "all"}, key=key)["payload"]["items"]  # type: ignore[index]
        assert [(row["id"], row["source_id"]) for row in listed] == [(loop_id, None)], reader
        assert confidential not in json.dumps(listed), reader
    admin_listed = vault.wire("alice_open_loops", {"status": "all"}, key=vault.keys["alpha_admin"])["payload"]["items"]  # type: ignore[index]
    assert [(row["id"], row["source_id"]) for row in admin_listed] == [(loop_id, confidential)]


# -- 5. an idempotent replay is not a new attach ----------------------------------------------------------------


def test_an_idempotent_replay_returns_the_stored_memory_without_reading_the_sources_again(vault: _Vault) -> None:
    """A retry with the same idempotency key and the same refs returns the memory the first call stored, even if the
    source was deleted in between. The check runs for a new write, after the replay branch.

    Mutation: move ``resolve_attachable_sources`` in ``commit()`` above the ``self._idempotent_memory`` replay branch.
    The retry then answers ``not_found``.
    """

    own = vault.sources["own"]
    first = vault.commit("alpha_trusted", [own], idempotency_key="replay-after-delete", title="Replay", canonical_text="Replay entry text.")
    assert first["is_error"] is False
    vault.sql("UPDATE sources SET deleted_at = '2026-02-01T00:00:00Z' WHERE id = ?", (own,))
    second = vault.commit("alpha_trusted", [own], idempotency_key="replay-after-delete", title="Replay", canonical_text="Replay entry text.")
    assert second["is_error"] is False, second
    assert second["payload"]["idempotent_replay"] is True  # type: ignore[index]


# -- 6. the doors, and the shape of the argument ----------------------------------------------------------------


def test_the_fence_is_a_required_keyword_only_argument_with_no_default() -> None:
    """Every function that resolves cited ids takes the fence by keyword, with no default, so a caller that forgets it
    fails to run and does not read "no fence".

    Mutation: give ``fence`` in ``resolve_attachable_sources`` or ``source_fence`` in ``_validated_review_provenance``
    a default, or make it positional.
    """

    from alicebot_api.mcp.review import _validated_review_provenance

    for function, name in ((resolve_attachable_sources, "fence"), (_validated_review_provenance, "source_fence")):
        parameter = inspect.signature(function).parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, function
        assert parameter.default is inspect.Parameter.empty, function


def _provenance_link_call_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_provenance_link"
            ):
                scope = node
                while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = parents[scope]
                name = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
                sites.add((str(path.relative_to(_SRC)), name))
    return sites


# Every place that writes a provenance link, and where its source id comes from. A new site fails this test until it
# is read, classified and added here.
_OWN_SOURCE_SITES = {
    # The source was captured or ingested by this very call, so the caller named no id.
    ("vnext_capture.py", "_capture_source"),
    ("vnext_connectors.py", "ingest_agent_output"),
    # Regeneration reads the authorized source and its chunks before making new rows.
    ("vnext_source_regeneration.py", "regenerate_source_inputs"),
}
_NAMED_SOURCE_SITES = {
    # The caller named the id. Each is behind ``resolve_attachable_sources`` and the caller's ``SourceReadFence``.
    ("vnext_memory_commit.py", "_create_provenance_links"),
    ("mcp/review.py", "_vnext_memory_correct"),
}


def test_every_provenance_link_site_is_classified() -> None:
    """The test that finds a door a fix missed: a new ``create_provenance_link`` call fails here until its source id
    is classified as one the call made itself or one the caller named (and then it must go through the fence).

    Mutation: add ``store.create_provenance_link({...})`` to any function other than the four listed (six calls), or
    rename one of the listed functions.
    """

    found = _provenance_link_call_sites()
    expected = _OWN_SOURCE_SITES | _NAMED_SOURCE_SITES
    assert found == expected, {"new": sorted(found - expected), "gone": sorted(expected - found)}


def test_the_named_source_sites_take_checked_ids_only() -> None:
    """The two sites where a caller names an id are fed by the resolver, not by raw ids.

    ``_create_provenance_links`` takes ``AttachableSources``, which only ``resolve_attachable_sources`` builds, and
    never reads ``source_refs`` itself. The review door calls ``_validated_review_provenance`` with a fence at both of
    its call sites. Read from the syntax tree, so a change that feeds either site from raw ids fails.

    Mutation: make ``_create_provenance_links`` loop over ``request.source_refs`` again, or remove the
    ``resolve_attachable_sources`` call from ``_validated_review_provenance``, or drop ``source_fence=`` from one of
    the two ``_validated_review_provenance`` calls.
    """

    commit_tree = ast.parse((_SRC / "vnext_memory_commit.py").read_text(encoding="utf-8"))
    review_tree = ast.parse((_SRC / "mcp" / "review.py").read_text(encoding="utf-8"))

    def function(tree: ast.AST, name: str) -> ast.FunctionDef:
        return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)

    link_function = function(commit_tree, "_create_provenance_links")
    arguments = {arg.arg: ast.unparse(arg.annotation) for arg in link_function.args.kwonlyargs if arg.annotation}
    assert arguments.get("attachable_sources") == "AttachableSources"
    assert not [
        node for node in ast.walk(link_function) if isinstance(node, ast.Attribute) and node.attr == "source_refs"
    ]

    validator = function(review_tree, "_validated_review_provenance")
    resolver_calls = [
        node
        for node in ast.walk(validator)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_attachable_sources"
    ]
    assert len(resolver_calls) == 1
    assert [keyword.arg for keyword in resolver_calls[0].keywords] == ["fence"]

    validator_calls = [
        node
        for node in ast.walk(review_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_validated_review_provenance"
    ]
    assert len(validator_calls) == 2
    for call in validator_calls:
        fence = next(keyword.value for keyword in call.keywords if keyword.arg == "source_fence")
        assert ast.unparse(fence) == "SourceReadFence.for_identity(identity)"


def _function_sites(call_name: str) -> set[tuple[str, str]]:
    """Every ``<anything>.<call_name>(...)`` call under ``alicebot_api``, as (file, enclosing function)."""

    sites: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == call_name:
                scope: ast.AST = node
                while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = parents[scope]
                name = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
                sites.add((path.relative_to(_SRC).as_posix(), name))
    return sites


# Where Python that ships or runs outside the tests lives. The unit tests are left out on purpose: no test builds the
# type, and the test of the type's rules may name it freely.
_NON_TEST_PYTHON_ROOTS = ("apps/api", "docs", "eval", "scripts", "workers")


def _non_test_python_sources() -> list[Path]:
    paths = set(_ROOT.glob("*.py"))
    for root_name in _NON_TEST_PYTHON_ROOTS:
        paths.update((_ROOT / root_name).rglob("*.py"))
    return sorted(paths)


def _attachable_source_constructions() -> set[tuple[str, str]]:
    """Every place in the non-test sources that builds an ``AttachableSources`` or makes a type that can, as
    (repository path, enclosing function or ``<module>``).

    A construction is a call that names the type: plainly, through a module (``fence.AttachableSources(...)``),
    through an ``import ... as`` alias or through a name assigned from it, at module level or inside a function. A
    subclass is a site too, and so is ``replace(x, ids=...)``, which copies an instance with ids that were never
    checked. What it cannot see is a name built at run time (``getattr``, ``type(x)(...)``).
    """

    sites: set[tuple[str, str]] = set()
    for path in _non_test_python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {"AttachableSources"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names if alias.name == "AttachableSources")
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _names_the_type(node.value, names):
                names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            site = None
            if isinstance(node, ast.Call):
                copies_with_ids = any(keyword.arg == "ids" for keyword in node.keywords) and (
                    (isinstance(node.func, ast.Name) and node.func.id == "replace")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "replace")
                )
                if _names_the_type(node.func, names) or copies_with_ids:
                    scope: ast.AST = node
                    while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        scope = parents[scope]
                    site = scope.name if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
            elif isinstance(node, ast.ClassDef) and any(_names_the_type(base, names) for base in node.bases):
                site = f"class {node.name}"
            if site is not None:
                sites.add((path.relative_to(_ROOT).as_posix(), site))
    return sites


def _names_the_type(node: ast.AST, names: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in names) or (
        isinstance(node, ast.Attribute) and node.attr in names
    )


def test_attachable_sources_are_built_only_by_the_resolver() -> None:
    """``AttachableSources`` is the type that says "these ids were checked", and it is a plain public dataclass, so
    nothing but convention stops a new caller from building one out of unchecked ids. This finds every construction
    in every Python file outside the tests (the package, the scripts, the workers, the eval harness, the examples):
    exactly one, inside ``resolve_attachable_sources``.

    Mutations (each made in a scratch edit and the file restored by copying the saved copy back): build
    ``AttachableSources(ids=...)`` in a function of another file, at module level of another file, through
    ``import alicebot_api.vnext_source_fence as m`` and ``m.AttachableSources(...)``, through
    ``from ... import AttachableSources as X``, through ``X = AttachableSources`` and ``X(...)``, in a file under
    ``scripts/``, as a second construction inside ``vnext_source_fence.py``, as ``class X(AttachableSources)``, or as
    ``replace(attachable, ids=...)``. The control is a use of the name that is no construction
    (``isinstance(x, AttachableSources)``), which must pass.
    """

    scanned = {path.relative_to(_ROOT).as_posix() for path in _non_test_python_sources()}
    assert "apps/api/src/alicebot_api/vnext_source_fence.py" in scanned
    assert "scripts/release_check.py" in scanned
    assert any(path.startswith("workers/") for path in scanned)
    assert any(path.startswith("eval/") for path in scanned)
    assert not any(path.startswith("tests/") for path in scanned)

    assert _attachable_source_constructions() == {
        ("apps/api/src/alicebot_api/vnext_source_fence.py", "resolve_attachable_sources")
    }


# Every place that creates an open loop, and where the loop's source and memory ids come from. A new site fails the
# test below until it is read and classified here.
_OWN_LOOP_SITES = {
    # No id is named by a caller: the source or memory is one the call itself read or made, or the route has no agent
    # identity at all (the legacy ``POST /v0/open-loops`` of the owner's own vault).
    ("cli/capture.py", "_run_vnext_demo_load"),
    ("cli/smokes.py", "_seed_local_runtime_smoke_inputs"),
    ("cli/smokes.py", "_run_vnext_smoke_operator_console"),
    ("cli/smokes.py", "_run_vnext_smoke_agent_integration_pack"),
    ("continuity_task_eval.py", "_seed_fixture"),
    ("explicit_commitments.py", "_resolve_open_loop_outcome"),
    ("memory.py", "create_open_loop_record"),
    ("memory.py", "_create_open_loop_for_memory"),
    ("vnext_brain.py", "_create_candidate_open_loops"),
    ("vnext_projects.py", "extract_open_loops"),
    ("vnext_source_regeneration.py", "regenerate_source_inputs"),
    ("vnext_scheduler.py", "_publish_mutation"),
    ("vnext_stores/postgres/graph_open_loops.py", "upsert_open_loop_by_automation_digest"),
    ("vnext_stores/sqlite/graph_open_loops.py", "upsert_open_loop_by_automation_digest"),
}
_NAMED_LOOP_SITES = {
    # The caller named the ids. Behind the resolvers and the caller's ``SourceReadFence``.
    ("routers/vnext_projects.py", "create_vnext_open_loop"),
}


def test_every_open_loop_create_site_is_classified() -> None:
    """The open-loop twin of the provenance-link site test: a loop keeps the ``source_id`` and ``memory_id`` it is
    given in columns of its own, so a new ``create_open_loop`` call fails here until it is classified as one whose ids
    the call made itself or one whose ids a caller named (and then it must go through the fence).

    Mutation: add ``store.create_open_loop({...})`` to any function not listed, or rename a listed function.
    """

    found = _function_sites("create_open_loop")
    expected = _OWN_LOOP_SITES | _NAMED_LOOP_SITES
    assert found == expected, {"new": sorted(found - expected), "gone": sorted(expected - found)}


def test_the_open_loop_route_stores_only_what_the_resolvers_returned() -> None:
    """Read from the syntax tree of ``create_vnext_open_loop``. ``request.source_id`` and ``request.memory_id`` appear
    only as the argument of their resolver or in the ``is not None`` guard before it, never as a value that is stored;
    each resolver call carries ``fence=``; both come after the policy check and before ``create_open_loop``; and the
    loop payload takes ``source_id`` and ``memory_id`` from the variables the resolvers filled.

    Mutations: put ``request.source_id`` (or ``request.memory_id``) back in the payload, drop ``fence=`` from a
    resolver call, or move the resolver block above ``_vnext_policy_checked``.
    """

    tree = ast.parse((_SRC / "routers" / "vnext_projects.py").read_text(encoding="utf-8"))
    route = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "create_vnext_open_loop"
    )
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(route):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    resolvers = {"source_id": "resolve_attachable_source_id", "memory_id": "resolve_attachable_memory_id"}
    for field, resolver in resolvers.items():
        uses = [
            node
            for node in ast.walk(route)
            if isinstance(node, ast.Attribute) and node.attr == field and ast.unparse(node.value) == "request"
        ]
        assert uses, field
        for use in uses:
            parent = parents[use]
            as_argument = (
                isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == resolver
            )
            as_guard = (
                isinstance(parent, ast.Compare)
                and [type(op) for op in parent.ops] == [ast.IsNot]
                and ast.unparse(parent.comparators[0]) == "None"
            )
            assert as_argument or as_guard, (field, ast.unparse(parent))

    calls = {
        name: [
            node
            for node in ast.walk(route)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        ]
        for name in (*resolvers.values(), "_vnext_policy_checked")
    }
    create = next(
        node
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "create_open_loop"
    )
    policy_line = calls["_vnext_policy_checked"][0].lineno
    for resolver in resolvers.values():
        assert len(calls[resolver]) == 1, resolver
        assert [keyword.arg for keyword in calls[resolver][0].keywords] == ["fence"], resolver
        assert policy_line < calls[resolver][0].lineno < create.lineno, resolver

    payload = create.args[0]
    assert isinstance(payload, ast.Dict)
    stored = {
        key.value: ast.unparse(value)
        for key, value in zip(payload.keys, payload.values)
        if isinstance(key, ast.Constant) and key.value in resolvers
    }
    assert stored == {"source_id": "source_id", "memory_id": "memory_id"}, stored


_INTAKE_FIELD_NAMES = {
    "source_id",
    "source_ids",
    "source_refs",
    "source_chunk_id",
    "source_event_ids",
    "source_memory_ids",
    "memory_id",
    "memory_ids",
    "provenance",
    "replacement_provenance",
}

# Every HTTP body a key-bound caller can send (a ``VNextAgentRequest``) that has a field naming a source, a memory or a
# provenance object, and what happens to the value. The legacy routes of ``routers/memories_legacy.py`` and
# ``routers/continuity.py`` carry no agent identity and are not here.
_HTTP_INTAKES = {
    "vnext_memories.py:VNextMemoryCommitRequest.source_refs": "fenced: VNextMemoryCommitService.commit resolves every id",
    "vnext_projects.py:VNextOpenLoopCreateRequest.source_id": "fenced: create_vnext_open_loop resolves it",
    "vnext_projects.py:VNextOpenLoopCreateRequest.memory_id": "fenced: create_vnext_open_loop resolves it",
    "vnext_memories.py:VNextMemoryProposalRequest.source_refs": "stored as given: a proposal keeps the refs and no reader resolves them",
    "vnext_memories.py:VNextAgentOutputIngestRequest.source_refs": "stored as given: the new source keeps the refs in its metadata and no reader resolves them",
    "vnext_memories.py:VNextMemoryUndoRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "vnext_memories.py:VNextMemoryCorrectRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "vnext_memories.py:VNextMemoryForgetRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "vnext_memories.py:VNextMemoryExpireRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "vnext_memories.py:VNextMemoryUnexpireRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "vnext_memories.py:VNextMemoryAcceptConsolidationRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "vnext_memories.py:VNextMemoryRedactRequest.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
}

# The same for the MCP tool schemas, core and legacy. A key-bound caller reaches only the core tools: the legacy tools
# are off whenever ``ALICE_AGENT_API_KEY`` is set.
_MCP_INTAKES = {
    "alice_memory_commit.source_refs": "fenced: VNextMemoryCommitService.commit resolves every id",
    "alice_vnext_commit_memory.source_refs": "fenced: the legacy alias of alice_memory_commit, one handler",
    "alice_memory_correct.provenance": "fenced: _validated_review_provenance resolves the source id",
    "alice_memory_correct.provenance.source_id": "fenced: _validated_review_provenance resolves the source id",
    "alice_memory_correct.provenance.source_chunk_id": "fenced: checked against the fenced source",
    "alice_memory_correct.replacement_provenance": "fenced: _validated_review_provenance resolves the source id",
    "alice_memory_correct.replacement_provenance.source_id": "fenced: _validated_review_provenance resolves the source id",
    "alice_memory_correct.replacement_provenance.source_chunk_id": "fenced: checked against the fenced source",
    "alice_vnext_propose_memory.source_refs": "stored as given: a proposal keeps the refs and no reader resolves them",
    "alice_vnext_ingest_agent_output.source_refs": "stored as given: the new source keeps the refs in its metadata and no reader resolves them",
    "alice_vnext_open_loops.source_id": "listed in the schema and read by no handler: the tool only lists loops",
    "alice_review_apply.provenance": "stored as given: a continuity object keeps it as JSON and no source row is read",
    "alice_review_apply.provenance.source_event_ids": "stored as given: a continuity object keeps it as JSON and no source row is read",
    "alice_review_apply.replacement_provenance": "stored as given: a continuity object keeps it as JSON and no source row is read",
    "alice_review_apply.replacement_provenance.source_event_ids": "stored as given: a continuity object keeps it as JSON and no source row is read",
    "alice_memory_manage.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "alice_explain.memory_id": "by-id verb: explain fences every row it discloses",
    "alice_vnext_undo_memory.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "alice_vnext_correct_memory.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "alice_vnext_forget_memory.memory_id": "by-id verb: the memory service refuses it by the policy rules of the typed codes",
    "alice_vnext_memory_audit.memory_id": "by-id verb: explain fences every row it discloses",
}
_INTAKE_KINDS = ("fenced: ", "stored as given: ", "by-id verb: ", "listed in the schema and read by no handler: ")


def _agent_request_classes() -> dict[str, tuple[str, ast.ClassDef]]:
    classes: dict[str, tuple[str, ast.ClassDef]] = {}
    for path in sorted((_SRC / "routers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = (path.name, node)

    def is_agent_request(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name == "VNextAgentRequest":
            return True
        if name in seen or name not in classes:
            return False
        bases = [base.id for base in classes[name][1].bases if isinstance(base, ast.Name)]
        return any(is_agent_request(base, seen | {name}) for base in bases)

    return {name: item for name, item in classes.items() if name != "VNextAgentRequest" and is_agent_request(name)}


def _mcp_schema_intakes() -> set[str]:
    from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS

    found: set[str] = set()

    def walk(schema: object, prefix: str) -> None:
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                for key, value in properties.items():
                    if key in _INTAKE_FIELD_NAMES:
                        found.add(f"{prefix}.{key}")
                    walk(value, f"{prefix}.{key}")
            for key in ("items", "oneOf", "anyOf", "allOf"):
                if key in schema:
                    walk(schema[key], prefix)
        elif isinstance(schema, list):
            for item in schema:
                walk(item, prefix)

    for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS):
        walk(tool["inputSchema"], str(tool["name"]))
    return found


def test_every_intake_that_names_a_source_or_memory_is_classified() -> None:
    """The test that finds a door by its input: every HTTP body a key-bound caller can send and every MCP tool schema
    is scanned for a field that names a source, a memory or a provenance object, and each one found must be
    classified here as fenced, stored as given (and resolved by no reader), a by-id verb, or not read at all. A new
    field fails the test until it is read and classified, so a door like the open-loop route cannot be added, or
    missed, without a line in this table. The scan finds the fields; each ``fenced`` line is pinned by the behavioural
    tests above.

    Mutation: add ``source_id: str | None = None`` to any ``VNextAgentRequest`` body, or a ``source_id`` property to
    any tool schema, that is not listed.
    """

    http = {
        f"{file}:{name}.{statement.target.id}"
        for name, (file, node) in _agent_request_classes().items()
        for statement in node.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id in _INTAKE_FIELD_NAMES
    }
    assert http == set(_HTTP_INTAKES), {"new": sorted(http - set(_HTTP_INTAKES)), "gone": sorted(set(_HTTP_INTAKES) - http)}
    mcp = _mcp_schema_intakes()
    assert mcp == set(_MCP_INTAKES), {"new": sorted(mcp - set(_MCP_INTAKES)), "gone": sorted(set(_MCP_INTAKES) - mcp)}
    for table in (_HTTP_INTAKES, _MCP_INTAKES):
        for key, disposition in table.items():
            assert disposition.startswith(_INTAKE_KINDS), (key, disposition)
