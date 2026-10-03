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
    SOURCE_REF_NOT_FOUND_MESSAGE,
    SourceReadFence,
    SourceRefNotFoundError,
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

    Mutations, each alone, in ``vnext_source_fence.py`` (``SourceReadFence.admits`` and the resolver) or in
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

    Mutation: remove the ``deleted_at`` check in ``SourceReadFence.admits``.
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

    Mutations: make ``admits`` also refuse a source outside ``identity.project_scope`` for an identity that is not
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


# -- 4. what the attached link did to the key's own reads -------------------------------------------------------


def test_what_a_key_may_attach_it_may_also_explain(vault: _Vault) -> None:
    """The fence is the one ``alice_explain`` applies to each source it discloses, so a memory a key committed with a
    link that passed is a memory that key can explain. On v0.20.0 a link to a source outside the fence made the key's
    own explain fail closed, and the same for every other key in the project.

    Every key tries every source kind. Each commit that is stored is explained by the same key, and each of those must
    work, so a fence that admits one source too many shows as an explain that fails. The control plants such a link
    straight into the store, past the door, and shows the explain does fail closed then, so the assertion is not
    vacuous. At least one commit of each key is stored, so the loop is not empty.

    Mutations: make ``admits`` accept ``allowed_with_filtering`` (the confidential source is then stored for the
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
    ("vnext_capture.py", "capture_source"),
    ("vnext_connectors.py", "ingest_agent_output"),
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
