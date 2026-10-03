"""A request that names no domain reads only what the caller's permission profile may read, in every reader.

Unreleased (on main, not in v0.20.0). In v0.20.0 a key that was refused when it asked for ``domains: ["health"]`` read
health, family, spiritual, legal and financial notes, open loops, decisions and source excerpts when it asked for no
domains. ``alice_recall``, ``alice_context_pack``, ``alice_resume``, ``alice_open_loops`` and ``alice_recent_decisions``
all returned them to a ``project_scoped_agent`` key and to a ``read_only_agent`` key. Only ``alice_memory_review`` was
right, because it fills the domain list with every label before it asks the policy.

The vault below holds, in project alpha, one note, one decision, one open loop and one source in each of nine domains
(``project``, ``professional``, ``personal`` and ``unknown``, which a restricted profile may read, and ``family``,
``health``, ``spiritual``, ``legal`` and ``financial``, which it may not), and a set of another project that no alpha key
may read. Every text names its domain and kind in a marker (``MK-HEALTH-N`` is the health note), and no query contains a
marker, so a marker in an answer is a row that was read and not the query echoed back. The held-back domains are seeded
last, so their rows are the newest and a reader that leaks them leaks them at the front.

The rule under test, for every restricted profile (everything but ``trusted_local_agent`` and ``admin_agent``): a request
that names no domain, or an empty list, is the profile's permitted domains (every label except family, health, spiritual,
legal and financial, ``unknown`` included). A request that names domains narrows that set. A request for only held-back
domains is refused, as in v0.20.0. An unrestricted profile and the owner read every domain, as before.

Each test names the mutation that must fail it. The mutations were made by hand in a scratch edit and the file was
restored by copying the saved copy back. The reader edits that each fail this file, one at a time: ``domains=[]`` in the
``search_source_excerpts`` call of ``alice_recall`` and in its ``_memory_fts_rows`` call; ``domains=None`` in the
open-loop list of ``mcp/projects.py``; ``domain_filter = None`` in ``_vnext_recent_decisions``; ``domains=()`` in the
request of ``_vnext_context_pack_payload``; dropping ``or tuple(VNEXT_DOMAINS)`` from ``alice_memory_review``;
``domain_filter = None`` in ``compile_session_brief``; ``domains=retrieval_request.domains`` in the HTTP context-pack
route; the caller's own ``scope`` domains in ``_vnext_connection_request``. One edit does not fail it alone:
``"domains": None`` in the chunk search of ``_source_stage_lists``, because the candidate check ``_allowed`` still drops
the row afterwards. Both edits together do fail it, so the two layers are each real and the answer is held by either.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator
from uuid import UUID

import pytest

from alicebot_api import mcp_server
from alicebot_api.mcp.runtime import _sqlite_path_from_url
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, resolve_db_path, sqlite_url_for_path
from alicebot_api.project_identity import detect_project
from alicebot_api.project_view import ProjectView
from alicebot_api.session_briefing import compile_session_brief
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.surface_flags import MCP_FULL_TOOLS_ENV
from alicebot_api.vnext_agent_control import AgentIdentity, evaluate_agent_policy
from alicebot_api.vnext_agent_keys import create_agent_key
from tests.unit.per_project_s2_support import PROJECT_B, add_loop, add_memory, normalized_json, repo_with_remote

USER_ID = "00000000-0000-0000-0000-000000000001"
KEY_ENV = "ALICE_AGENT_API_KEY"
SCOPING_ENV = "ALICE_PROJECT_SCOPING"

HELD_BACK = ("family", "health", "spiritual", "legal", "financial")
PERMITTED_SEEDED = ("project", "professional", "personal", "unknown")
SEEDED = (*PERMITTED_SEEDED, *HELD_BACK)

MARKER = re.compile(r"MK-([A-Z]+)-([NDLS])")

RESTRICTED_KEYS = ("project_scoped", "read_only", "memory_proposal")
UNRESTRICTED_KEYS = ("trusted", "admin")

#: One call per reader. Each is made as a key bound to alpha, so ``project_scope`` is the key's own.
TOOLS: dict[str, dict[str, object]] = {
    "alice_recall": {"query": "kiln", "limit": 50},
    # The token budget is raised so the packer drops nothing: a row missing from an answer is then a row that was fenced.
    "alice_context_pack": {"query": "kiln", "max_items": 50, "max_tokens": 50000, "project_scope": ["alpha"]},
    "alice_resume": {"project": "alpha", "max_recent_changes": 20, "max_open_loops": 20},
    "alice_open_loops": {"project_scope": ["alpha"], "limit": 100},
    "alice_recent_decisions": {"project_scope": ["alpha"], "limit": 50},
    "alice_memory_review": {"project_scope": ["alpha"], "status": "all", "limit": 50},
}

#: The kinds each reader shows for every domain it may read. ``alice_resume`` names one decision and the open loops, so
#: only its loops are checked for every domain.
KINDS = {
    "alice_recall": {"N", "D", "S"},
    "alice_context_pack": {"N", "D"},
    "alice_resume": {"L"},
    "alice_open_loops": {"L"},
    "alice_recent_decisions": {"D"},
    "alice_memory_review": {"N", "D"},
}

TEXT = {
    "N": "The kiln schedule entry MK-{} is posted.",
    "D": "We decided the kiln decision MK-{} stands.",
    "L": "kiln loop MK-{}",
    "S": "Source document about the kiln firing log MK-{} entry text.",
}


#: label, permission profile, project binding. The two unbound keys name no project, so the per-project view fills the
#: scope of a call made as either of them.
KEY_SPECS = (
    ("project_scoped", "project_scoped_agent", "alpha"),
    ("read_only", "read_only_agent", "alpha"),
    ("memory_proposal", "memory_proposal_agent", "alpha"),
    ("trusted", "trusted_local_agent", "alpha"),
    ("admin", "admin_agent", "alpha"),
    ("read_only_unbound", "read_only_agent", None),
    ("trusted_unbound", "trusted_local_agent", None),
)


class Vault:
    """A throwaway SQLite vault: real keys, rows in nine domains, and the ids of each domain's rows."""

    def __init__(self, root: Path, *, keyed: bool = True) -> None:
        os.environ["HOME"] = str(root / "home")
        (root / "home").mkdir()
        self.repo = repo_with_remote(root / "repo")
        detection = detect_project(argument=str(self.repo))
        assert detection.context is not None
        self.project_id = detection.context.ids[0]
        database = resolve_db_path(data_dir=str(root / "vault"), db=None)
        bootstrap_database(database, user_id=USER_ID, user_email="local@alice")
        self.url = sqlite_url_for_path(database)
        self.path = _sqlite_path_from_url(self.url)
        self.context = MCPRuntimeContext(database_url=self.url, user_id=UUID(USER_ID))
        self.scoped_context = MCPRuntimeContext(
            database_url=self.url, user_id=UUID(USER_ID), project_dir=str(self.repo)
        )
        self.keys: dict[str, str] = {}
        with sqlite_user_connection(self.path, USER_ID) as conn:
            store = SQLiteVNextStore(conn, USER_ID)
            for label, profile, project in KEY_SPECS if keyed else ():
                _record, raw = create_agent_key(
                    store,
                    user_id=USER_ID,
                    agent_id=f"{label}-agent",
                    permission_profile=profile,
                    project_scope=project,
                )
                self.keys[label] = raw
            # Alpha's rows carry the Alice project id of the repository too, so the per-project view reads them as the
            # project's own. Held-back domains go in last, so they are the newest rows.
            alpha = ("alpha", self.project_id)
            for domain in SEEDED:
                tag = domain.upper()
                add_memory(
                    store, key=f"n.{domain}", text=TEXT["N"].format(f"{tag}-N"), domain=domain,
                    sensitivity="internal", scope=alpha,
                )
                add_memory(
                    store, key=f"d.{domain}", text=TEXT["D"].format(f"{tag}-D"), domain=domain,
                    sensitivity="internal", scope=alpha, memory_type="decision",
                )
                add_loop(store, title=TEXT["L"].format(f"{tag}-L"), domain=domain, sensitivity="internal", scope=alpha)
            # Rows of another Alice project (domain project): outside every alpha key and outside the view of the
            # repository. (A free-form name such as ``beta`` is a global note to the view, so the other project
            # is named by a project id.)
            add_memory(
                store, key="n.other", text=TEXT["N"].format("OTHER-N"), domain="project", sensitivity="internal",
                scope=(PROJECT_B,),
            )
            add_memory(
                store, key="d.other", text=TEXT["D"].format("OTHER-D"), domain="project", sensitivity="internal",
                scope=(PROJECT_B,), memory_type="decision",
            )
            add_loop(
                store, title=TEXT["L"].format("OTHER-L"), domain="project", sensitivity="internal", scope=(PROJECT_B,)
            )
            # One note per domain for the by-id test: scoped to alpha alone, so a key bound to alpha may be shown it.
            for domain in SEEDED:
                add_memory(
                    store, key=f"x.{domain}", text=TEXT["N"].format(f"{domain.upper()}-N"), domain=domain,
                    sensitivity="internal", scope=("alpha",),
                )
        for domain in SEEDED:
            done = self.wire(
                "alice_capture",
                {
                    "raw_text": TEXT["S"].format(f"{domain.upper()}-S"),
                    "title": f"kiln source {domain}",
                    "domain": domain,
                    "sensitivity": "internal",
                    "project_scope": ["alpha"],
                },
                key="trusted" if keyed else None,
            )
            assert done["is_error"] is False, (domain, done)
        self.ids: dict[str, set[str]] = {domain: set() for domain in SEEDED}
        for table in ("memories", "open_loops", "sources"):
            for row in self.sql(f"SELECT id, domain FROM {table}"):
                if row["domain"] in self.ids:
                    self.ids[str(row["domain"])].add(str(row["id"]))
        assert all(len(ids) >= 3 for ids in self.ids.values()), self.ids

    def sql(self, query: str, args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return list(conn.execute(query, args).fetchall())

    def wire(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        key: str | None,
        scoping: bool = False,
    ) -> dict[str, object]:
        """One ``tools/call`` through the real server object, as a client reads it."""

        if key is None:
            os.environ.pop(KEY_ENV, None)
        else:
            os.environ[KEY_ENV] = self.keys[key]
        os.environ[MCP_FULL_TOOLS_ENV] = "1"
        if scoping:
            os.environ[SCOPING_ENV] = "on"
        try:
            server = mcp_server.MCPServer(
                context=self.scoped_context if scoping else self.context,
                input_stream=BytesIO(),
                output_stream=BytesIO(),
            )
            response = server._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
            )
        finally:
            for name_ in (KEY_ENV, MCP_FULL_TOOLS_ENV, SCOPING_ENV):
                os.environ.pop(name_, None)
        assert response is not None
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        assert isinstance(payload, dict)
        return {"is_error": bool(result["isError"]), "payload": payload}

    def read(
        self,
        tool: str,
        *,
        key: str | None,
        domains: list[str] | None = None,
        extra: dict[str, object] | None = None,
        scoping: bool = False,
    ) -> dict[str, object]:
        arguments: dict[str, object] = dict(TOOLS[tool])
        if key is None or key.endswith("_unbound"):
            # The owner and an unbound key name no project, so the alpha scope is the only one they would have named.
            arguments.pop("project_scope", None)
            arguments.pop("project", None)
        if domains is not None:
            arguments["domains"] = domains
        arguments.update(extra or {})
        return self.wire(tool, arguments, key=key, scoping=scoping)

    def found(self, answer: dict[str, object]) -> set[tuple[str, str]]:
        """``(domain, kind)`` of every marker in the answer, with the domain in lower case."""

        text = json.dumps(answer["payload"])
        return {(domain.lower(), kind) for domain, kind in MARKER.findall(text)}

    def held_back_in(self, answer: dict[str, object]) -> list[str]:
        """The held-back domains an answer reaches, by marker or by the id of any row of that domain."""

        text = json.dumps(answer["payload"])
        reached = {domain.lower() for domain, _kind in MARKER.findall(text) if domain.lower() in HELD_BACK}
        reached |= {domain for domain in HELD_BACK if any(row_id in text for row_id in self.ids[domain])}
        return sorted(reached)

    def error_code(self, answer: dict[str, object]) -> str | None:
        if not answer["is_error"]:
            return None
        error = answer["payload"].get("error")  # type: ignore[union-attr]
        return str(error.get("code")) if isinstance(error, dict) else "unknown_error"


@pytest.fixture(scope="module")
def vault(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Vault]:
    patcher = pytest.MonkeyPatch()
    for name in (KEY_ENV, SCOPING_ENV, "ALICE_PROJECT_DIR", MCP_FULL_TOOLS_ENV):
        patcher.delenv(name, raising=False)
    patcher.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    try:
        yield Vault(tmp_path_factory.mktemp("omitted_domains"))
    finally:
        patcher.undo()


@pytest.fixture(scope="module")
def keyless_vault(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Vault]:
    """The same rows with no agent key issued, so the HTTP routes still accept a declared identity (once any key
    exists, a route refuses a call that declares one without the key)."""

    patcher = pytest.MonkeyPatch()
    for name in (KEY_ENV, SCOPING_ENV, "ALICE_PROJECT_DIR", MCP_FULL_TOOLS_ENV):
        patcher.delenv(name, raising=False)
    patcher.setenv("HOME", str(tmp_path_factory.mktemp("home_keyless")))
    try:
        yield Vault(tmp_path_factory.mktemp("omitted_domains_keyless"), keyed=False)
    finally:
        patcher.undo()


def _assert_permitted_content_returns(vault: Vault, tool: str, found: set[tuple[str, str]], domains: tuple[str, ...]) -> None:
    for domain in domains:
        for kind in KINDS[tool]:
            assert (domain, kind) in found, (tool, domain, kind, sorted(found))


# ---------------------------------------------------------------------------------------------------------------------
# 1. A restricted key that names no domain


@pytest.mark.parametrize("request_shape", ["omitted", "empty_list"])
@pytest.mark.parametrize("key", RESTRICTED_KEYS)
@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_a_restricted_key_that_names_no_domain_reads_none_of_the_held_back_domains(
    vault: Vault, tool: str, key: str, request_shape: str
) -> None:
    """Mutation: remove the ``if not domains: return permitted, ()`` branch of ``_filtered_domains``
    (``vnext_agent_control.py``). Every case then reaches all five held-back domains, as v0.20.0 did.

    The answer holds no marker and no row id of family, health, spiritual, legal or financial, anywhere in the payload
    (the results, the source excerpts, the open loops, the recent changes and the pointers). It still holds the project's
    own rows and the rows of the other three permitted domains, so the filter did not empty the answer, and holds
    nothing of the other project.
    """

    domains = None if request_shape == "omitted" else []
    answer = vault.read(tool, key=key, domains=domains)
    assert answer["is_error"] is False, answer
    assert vault.held_back_in(answer) == [], (tool, key, request_shape)
    found = vault.found(answer)
    _assert_permitted_content_returns(vault, tool, found, PERMITTED_SEEDED)
    assert not any(domain == "other" for domain, _kind in found), "project scope still applies"


@pytest.mark.parametrize("key", RESTRICTED_KEYS)
@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_a_restricted_key_that_asks_for_a_held_back_domain_is_still_refused(vault: Vault, tool: str, key: str) -> None:
    """Mutation: let the explicit branch of ``_filtered_domains`` keep held-back labels (drop the test against
    ``RESTRICTED_DOMAINS``).

    ``domains: ["health"]`` answers ``not_permitted`` on every reader, as in v0.20.0, and so does a request for several
    held-back domains. The refusal carries nothing of the vault.
    """

    for held_back in (["health"], ["family", "legal"], list(HELD_BACK)):
        answer = vault.read(tool, key=key, domains=held_back)
        assert vault.error_code(answer) == "not_permitted", (tool, key, held_back, answer)
        assert vault.held_back_in(answer) == []


@pytest.mark.parametrize("key", RESTRICTED_KEYS)
@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_a_named_permitted_domain_narrows_the_answer_and_a_mixed_request_drops_the_held_back_one(
    vault: Vault, tool: str, key: str
) -> None:
    """Mutation: turn a named request into the whole permitted set (return ``permitted`` whenever a restricted profile
    asks, in ``_filtered_domains``), or let a mixed request pass its held-back label.

    ``["project"]`` returns the project's rows and none of ``professional`` or ``personal`` (``unknown`` rows may come
    with it: the stores return ``unknown`` under every domain filter, as they did in v0.20.0). ``["project", "health"]``
    is the same answer, and holds no health row.
    """

    for domains in (["project"], ["project", "health"]):
        answer = vault.read(tool, key=key, domains=domains)
        assert answer["is_error"] is False, (tool, key, domains, answer)
        assert vault.held_back_in(answer) == []
        found = vault.found(answer)
        _assert_permitted_content_returns(vault, tool, found, ("project",))
        assert not {domain for domain, _kind in found} & {"professional", "personal"}, (tool, key, domains)


@pytest.mark.parametrize("label", ["banana", "HEALTH", "Health"])
@pytest.mark.parametrize("key", RESTRICTED_KEYS)
@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_a_request_that_is_not_a_label_never_returns_a_held_back_row_and_never_returns_everything(
    vault: Vault, tool: str, key: str, label: str
) -> None:
    """Mutation: intersect the request with the permitted labels and pass the empty result on (return ``()`` from
    ``_filtered_domains`` when nothing survives), which every reader reads as "no filter".

    A word that is not a label matches no row (labels are lowercase by the database check). The answer holds nothing of
    the five, and nothing of the permitted domains either, apart from ``unknown`` rows that the stores return under
    every domain filter. If the empty tuple reached a reader the answer would hold all nine domains.
    """

    answer = vault.read(tool, key=key, domains=[label])
    assert answer["is_error"] is False, (tool, key, label, answer)
    assert vault.held_back_in(answer) == []
    found_domains = {domain for domain, _kind in vault.found(answer)}
    assert found_domains <= {"unknown"}, (tool, key, label, sorted(found_domains))


# ---------------------------------------------------------------------------------------------------------------------
# 2. Callers that are not held back


@pytest.mark.parametrize("request_shape", ["omitted", "empty_list"])
@pytest.mark.parametrize("key", UNRESTRICTED_KEYS)
@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_an_unrestricted_key_reads_every_domain_with_or_without_naming_them(
    vault: Vault, tool: str, key: str, request_shape: str
) -> None:
    """Mutation: apply the permitted set to the trusted and admin profiles (empty ``UNRESTRICTED_DOMAIN_PROFILES``).

    A trusted or admin key that names no domain still reads family, health, spiritual, legal and financial rows, as in
    v0.20.0, and ``domains: ["health"]`` still reads the health rows. An omitted list and an empty list give the same
    answer once ids, timestamps and trace ids are replaced. (The answer of a call that lists all nine domains is not
    compared: a pack counts its request in its token estimate, so the two differ by a few tokens in v0.20.0 as well.)
    """

    domains = None if request_shape == "omitted" else []
    answer = vault.read(tool, key=key, domains=domains)
    assert answer["is_error"] is False, answer
    found = vault.found(answer)
    _assert_permitted_content_returns(vault, tool, found, SEEDED)
    assert vault.held_back_in(answer) == sorted(HELD_BACK)
    other_shape = vault.read(tool, key=key, domains=[] if domains is None else None)
    assert normalized_json(answer["payload"]) == normalized_json(other_shape["payload"]), (tool, key)
    health = vault.read(tool, key=key, domains=["health"])
    assert health["is_error"] is False
    assert ("health", sorted(KINDS[tool])[0]) in vault.found(health)


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_the_owner_reads_every_domain_of_every_project(vault: Vault, tool: str) -> None:
    """Mutation: apply the permitted set to a call with no identity (derive it before the ``identity is None`` return of
    ``evaluate_agent_policy``).

    A call with no key and no declared identity is the owner. It reads all nine domains, as in v0.20.0, with the domains
    omitted or an empty list.
    """

    for domains in (None, []):
        answer = vault.read(tool, key=None, domains=domains)
        assert answer["is_error"] is False, answer
        found = vault.found(answer)
        _assert_permitted_content_returns(vault, tool, found, SEEDED)
        assert vault.held_back_in(answer) == sorted(HELD_BACK)


# ---------------------------------------------------------------------------------------------------------------------
# 3. A keyless call that declares a profile is held to that profile


KEYLESS_IDENTITIES: dict[str, tuple[dict[str, object], bool]] = {
    # name: (the identity fields sent with the call, whether the profile is held back)
    "agent_id_only": ({"agent_id": "someagent"}, True),
    "declared_read_only": ({"agent_id": "a", "permission_profile": "read_only_agent"}, True),
    "declared_project_scoped": (
        {"agent_id": "a", "permission_profile": "project_scoped_agent", "project_scope": ["alpha"]},
        True,
    ),
    "declared_memory_proposal": ({"agent_id": "a", "permission_profile": "memory_proposal_agent"}, True),
    "openclaw_default": ({"agent_id": "openclaw", "project_scope": ["alpha"]}, True),
    "declared_trusted": ({"agent_id": "a", "permission_profile": "trusted_local_agent"}, False),
    "declared_admin": ({"agent_id": "a", "permission_profile": "admin_agent"}, False),
    "hermes_default": ({"agent_id": "hermes"}, False),
}


@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize("name", sorted(KEYLESS_IDENTITIES))
def test_a_keyless_call_that_declares_a_profile_is_held_to_that_profile_when_it_names_no_domain(
    vault: Vault, name: str, tool: str
) -> None:
    """Mutation: remove the omitted-request branch of ``_filtered_domains`` (the same edit as the first test).

    No key is set. A call that declares ``read_only_agent``, ``project_scoped_agent`` or ``memory_proposal_agent``, or
    only an ``agent_id`` (which defaults to ``read_only_agent``, except ``hermes`` and ``openclaw``), and that names no
    domain stops reading the five held-back domains. A call that declares ``trusted_local_agent`` or ``admin_agent``, and
    ``hermes`` with no profile, reads every domain as before. This is v0.20.0's keyless rule (a declared profile is
    honoured) now applied to a request with no domains.
    """

    identity, held_back = KEYLESS_IDENTITIES[name]
    arguments = {**{k: v for k, v in TOOLS[tool].items() if k not in {"project_scope", "project"}}, **identity}
    answer = vault.wire(tool, arguments, key=None)
    assert answer["is_error"] is False, (name, tool, answer)
    found = vault.found(answer)
    if held_back:
        assert vault.held_back_in(answer) == [], (name, tool)
        _assert_permitted_content_returns(vault, tool, found, PERMITTED_SEEDED)
    else:
        assert vault.held_back_in(answer) == sorted(HELD_BACK), (name, tool)
        _assert_permitted_content_returns(vault, tool, found, SEEDED)


# ---------------------------------------------------------------------------------------------------------------------
# 4. The per-project view


def test_the_per_project_fill_applies_the_domain_filter_to_the_project_rows_of_a_restricted_caller(vault: Vault) -> None:
    """Mutation: pass ``None`` in place of ``domain_filter`` in one fill of ``_vnext_resume`` (``mcp/retrieval.py``).
    Each of these edits was made alone and fails this test: the decision fill (``read_memories``, project branch), the
    open-loop fill, and ``domains=None`` in both event reads of ``read_events``. The event edit fails the four-places
    check at the end: the held-back events are the newest, so without the domain in the query they take the places and
    the by-id fence then drops them, which leaves fewer than four. Removing that fence as well also leaks them.

    Scoping is on, a project is found for the repository, and the key names no project and is bound to none, so the call
    reads the project view: the project's rows first and global rows after. The project's own rows in a held-back domain
    are not held back by the view (it holds back global rows only), so the domain filter is what keeps them out. The
    newest decision is the project's financial one, the open loops are the project's, and none of them is held back.
    """

    unscoped = vault.read("alice_resume", key="trusted_unbound", scoping=True)
    assert unscoped["is_error"] is False
    assert vault.held_back_in(unscoped) == sorted(HELD_BACK), "an unrestricted key still reads them"

    for domains in (None, []):
        answer = vault.read("alice_resume", key="read_only_unbound", domains=domains, scoping=True)
        assert answer["is_error"] is False, answer
        brief = answer["payload"]["brief"]  # type: ignore[index]
        assert vault.held_back_in(answer) == [], domains
        loops = brief["open_loops"]
        assert {str(loop["domain"]) for loop in loops} == set(PERMITTED_SEEDED), loops
        assert brief["last_decision"] is not None
        assert brief["last_decision"]["domain"] in PERMITTED_SEEDED
        assert brief["next_action"] is not None and brief["next_action"]["domain"] in PERMITTED_SEEDED
        assert "MK-OTHER" not in json.dumps(answer["payload"]), "another project is still outside the view"
        # The newest events are the held-back ones. Four places are still filled, with permitted rows.
        narrow = vault.read(
            "alice_resume", key="read_only_unbound", domains=domains, extra={"max_recent_changes": 4}, scoping=True
        )
        changes = narrow["payload"]["brief"]["recent_changes"]  # type: ignore[index]
        permitted_ids = set().union(*(vault.ids[domain] for domain in PERMITTED_SEEDED))
        assert len(changes) == 4, changes
        assert {str(change["target_id"]) for change in changes} <= permitted_ids

    refused = vault.read("alice_resume", key="read_only_unbound", domains=["health"], scoping=True)
    assert vault.error_code(refused) == "not_permitted"


def test_the_per_project_fill_of_a_keyless_declared_profile_is_held_to_it(vault: Vault) -> None:
    """Mutation: the same three edits as the test above. A keyless call that declares ``read_only_agent`` reads the project
    view with scoping on, and now leaves out the project's held-back rows.
    """

    arguments = {
        "max_recent_changes": 20,
        "max_open_loops": 20,
        "agent_id": "someagent",
    }
    answer = vault.wire("alice_resume", arguments, key=None, scoping=True)
    assert answer["is_error"] is False, answer
    assert vault.held_back_in(answer) == []
    loops = answer["payload"]["brief"]["open_loops"]  # type: ignore[index]
    assert {str(loop["domain"]) for loop in loops} == set(PERMITTED_SEEDED)


# ---------------------------------------------------------------------------------------------------------------------
# 5. The reads that take a row's own domain


def test_explain_by_id_still_refuses_a_held_back_memory_and_discloses_a_permitted_one(vault: Vault) -> None:
    """Not changed by the fix, pinned here because a by-id read carries the row's own domain and was never open.

    ``alice_explain`` of the project memory answers; of the health memory it fails with the same fixed error a missing id
    gets, and the answer holds no text of the row. Mutation: make the explain door read the effective domains of a
    request with none (drop ``domains=(target_domain,)`` from its policy call), which would admit the health memory.
    """

    def memory_id(domain: str) -> str:
        rows = vault.sql("SELECT id FROM memories WHERE domain = ? AND memory_key = ?", (domain, f"x.{domain}"))
        return str(rows[0]["id"])

    allowed = vault.wire("alice_explain", {"memory_id": memory_id("project")}, key="project_scoped")
    assert allowed["is_error"] is False, allowed
    for domain in HELD_BACK:
        refused = vault.wire("alice_explain", {"memory_id": memory_id(domain)}, key="project_scoped")
        assert refused["is_error"] is True, (domain, refused)
        assert f"MK-{domain.upper()}" not in json.dumps(refused["payload"])
        assert vault.error_code(refused) == vault.error_code(
            vault.wire("alice_explain", {"memory_id": "no-such-memory"}, key="project_scoped")
        ), domain


def test_the_session_brief_holds_a_restricted_decision_to_its_domains(vault: Vault) -> None:
    """Mutation: pass ``None`` in place of ``domain_filter`` in the unscoped fact read of ``compile_session_brief``
    (``session_briefing.py``), and in its open-loop read.

    No agent-facing tool renders the brief today (the hook and ``alice-memory brief`` run as the owner), so this holds the
    library: given the decision a restricted identity gets for a request with no domains, the brief it renders holds no
    held-back row, and given an unrestricted decision it holds them.
    """

    def brief_for(profile: str) -> str:
        decision = evaluate_agent_policy(
            identity=AgentIdentity(agent_id="b", permission_profile=profile),
            action="context_pack.request",
            domains=(),
            sensitivity_allowed=("public", "internal", "unknown"),
            project_scope=("alpha",),
        )
        assert decision.decision == "allowed"
        with sqlite_user_connection(vault.path, USER_ID) as conn:
            return compile_session_brief(
                SQLiteVNextStore(conn, USER_ID),
                effective_domains=decision.effective_domains,
                effective_sensitivity_allowed=decision.effective_sensitivity_allowed,
                effective_project_scope=decision.effective_project_scope,
                project_view=ProjectView.unscoped(),
                exclude_global_domains=frozenset(),
                query=None,
            )

    restricted = brief_for("project_scoped_agent")
    for domain in HELD_BACK:
        assert f"MK-{domain.upper()}" not in restricted, domain
    assert "MK-PROJECT-N" in restricted and "MK-PROJECT-L" in restricted
    unrestricted = brief_for("trusted_local_agent")
    assert any(f"MK-{domain.upper()}" in unrestricted for domain in HELD_BACK)


# ---------------------------------------------------------------------------------------------------------------------
# 6. The HTTP routes


@contextmanager
def _routes_over_the_vault(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The HTTP routes run on Postgres only. Their settings, connection and store are swapped for the vault's, as the
    commit-route and open-loop-route tests of the cited-source fence do, so the real route function runs over the real
    SQLite store with a real key."""

    from alicebot_api.config import Settings
    from alicebot_api.routers import vnext_retrieval as router

    @contextmanager
    def connection(_database_url: object, current_user_id: object) -> Iterator[sqlite3.Connection]:
        with sqlite_user_connection(vault.path, str(current_user_id)) as conn:
            yield conn

    monkeypatch.setattr(router, "get_settings", lambda: Settings(database_url="postgresql://db"))
    monkeypatch.setattr(router, "user_connection", connection)
    monkeypatch.setattr(router, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, USER_ID))
    yield


def _post_pack(vault: Vault, *, key: str | None, domains: list[str] | None, **identity: object):  # type: ignore[no-untyped-def]
    from alicebot_api.routers import vnext_retrieval as router

    scope: dict[str, object] = {}
    if domains is not None:
        scope["domains"] = domains
    response = router.create_vnext_context_pack(
        router.VNextContextPackRequest(
            user_id=UUID(USER_ID),
            query="kiln",
            scope=scope,
            options={"max_items": 50, "sensitivity_allowed": ["public", "internal", "unknown"]},
            **identity,  # type: ignore[arg-type]
        ),
        authorization=f"Bearer {vault.keys[key]}" if key is not None else None,
    )
    return response.status_code, json.loads(response.body)


@pytest.mark.parametrize("key", RESTRICTED_KEYS)
def test_the_http_context_pack_route_holds_a_restricted_key_to_its_domains(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """``POST /v0/vnext/context-packs`` over the vault's SQLite store with a real key (the route is Postgres only; the CI
    Postgres job runs ``tests/integration/test_vnext_omitted_domains_api.py`` for the same call on Postgres).

    Mutation: remove the omitted-request branch of ``_filtered_domains``. The route hands ``decision.effective_domains`` to
    ``compile_context_pack``, so the pack then holds the five domains. With the fix the pack's own record says what was
    asked (nothing) and what was read (the eight labels), in ``query_interpretation.domains`` and in ``policy_decision``.
    """

    from alicebot_api.vnext_agent_control import permitted_domains

    with _routes_over_the_vault(vault, monkeypatch):
        status, body = _post_pack(vault, key=key, domains=None, project_scope=["alpha"])
        assert status == 201, body
        assert vault.held_back_in({"payload": body}) == []
        found = vault.found({"payload": body})
        assert ("project", "N") in found and ("project", "D") in found
        assert body["query_interpretation"]["domains"] == list(permitted_domains(key_profile(key)) or ())
        assert body["policy_decision"]["requested_domains"] == []
        assert body["policy_decision"]["effective_domains"] == body["query_interpretation"]["domains"]

        refused_status, refused = _post_pack(vault, key=key, domains=["health"], project_scope=["alpha"])
        assert refused_status == 403
        assert "all_requested_domains_restricted" in refused["policy_decision"]["reasons"]


def key_profile(key: str) -> str:
    return {
        "project_scoped": "project_scoped_agent",
        "read_only": "read_only_agent",
        "memory_proposal": "memory_proposal_agent",
        "trusted": "trusted_local_agent",
        "admin": "admin_agent",
    }[key]


@pytest.mark.parametrize("key", UNRESTRICTED_KEYS)
def test_the_http_context_pack_route_is_unchanged_for_an_unrestricted_key(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Mutation: apply the permitted set to the trusted and admin profiles. The pack of an unrestricted key that names no
    domain holds every domain and its record shows an empty domain list, both as in v0.20.0.
    """

    with _routes_over_the_vault(vault, monkeypatch):
        status, body = _post_pack(vault, key=key, domains=None, project_scope=["alpha"])
    assert status == 201, body
    assert vault.held_back_in({"payload": body}) == sorted(HELD_BACK)
    assert body["query_interpretation"]["domains"] == []
    assert body["policy_decision"]["effective_domains"] == []


def test_the_http_context_pack_route_holds_a_keyless_declared_profile_to_it(
    keyless_vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request that declares ``read_only_agent`` in its body, with no key and none issued, is held to that profile when
    it names no domain. The owner (no identity) and a declared ``trusted_local_agent`` are not.

    Mutation: remove the omitted-request branch of ``_filtered_domains``.
    """

    vault = keyless_vault
    with _routes_over_the_vault(vault, monkeypatch):
        _status, restricted = _post_pack(vault, key=None, domains=None, agent_id="a", permission_profile="read_only_agent")
        _status, trusted = _post_pack(vault, key=None, domains=None, agent_id="a", permission_profile="trusted_local_agent")
        _status, owner = _post_pack(vault, key=None, domains=None)
    assert vault.held_back_in({"payload": restricted}) == []
    assert restricted["query_interpretation"]["domains"] == [
        "professional", "personal", "learning", "relationship", "project", "agent_run", "system", "unknown",
    ]
    assert vault.held_back_in({"payload": trusted}) == sorted(HELD_BACK)
    assert vault.held_back_in({"payload": owner}) == sorted(HELD_BACK)
    assert owner["query_interpretation"]["domains"] == []


def test_every_http_request_builder_passes_the_effective_domains_of_the_decision() -> None:
    """The artifact, report and project-update routes build their request from ``decision.effective_domains``. Each builder
    is given the decision a restricted key gets for a request with no domains, and the one a trusted key gets.

    Mutation: build one request from ``request.scope`` domains (the caller's own words) in place of the decision's.
    The routes themselves run on Postgres only; the builders are the only place they read the domain list.
    """

    from alicebot_api.routers import _vnext_automation as automation
    from alicebot_api.routers import vnext_review as review
    from alicebot_api.vnext_agent_control import permitted_domains

    def decision_for(profile: str):  # type: ignore[no-untyped-def]
        identity = AgentIdentity(agent_id="b", permission_profile=profile, project_scope=("alpha",))
        return identity, evaluate_agent_policy(
            identity=identity,
            action="artifact.generate",
            domains=(),
            sensitivity_allowed=("public", "internal", "unknown"),
            project_scope=("alpha",),
        )

    builders = (
        (automation._vnext_project_automation_request, automation.VNextProjectAutomationRequest),
        (review._vnext_brain_artifact_request, review.VNextBrainArtifactGenerateRequest),
        (review._vnext_connection_request, review.VNextConnectionReportGenerateRequest),
        (review._vnext_contradiction_request, review.VNextContradictionReportGenerateRequest),
    )
    for builder, request_model in builders:
        for profile, expected in (
            ("project_scoped_agent", permitted_domains("project_scoped_agent")),
            ("read_only_agent", permitted_domains("read_only_agent")),
            ("trusted_local_agent", ()),
            ("admin_agent", ()),
        ):
            identity, decision = decision_for(profile)
            fields = {"user_id": UUID(USER_ID)}
            if "query" in request_model.model_fields:
                fields["query"] = "kiln"
            built = builder(request_model(**fields), identity=identity, decision=decision)  # type: ignore[arg-type]
            assert tuple(built.domains) == tuple(expected or ()), (builder.__name__, profile)
