"""A saved quote of a memory is withheld at every door of the mounted application, and the operator route sweep holds it.

Unreleased (on main, not in v0.20.0). A commit can cite a memory as ``{"memory_id": "<id>", "quote": "..."}`` or as the two
entries ``"memory:<id>"`` and ``{"quote": "..."}``, or in a wording no marker covers (``Memory <id>``, ``mem:<id>``, a URL, an id
under ``origin``). The quote holds words of the memory, the write check reads none of these, and on main until this change every
reader of the commit returned the words after the memory was redacted. The reader of saved quotes now withholds them from a
caller who may not read the memory. These tests run that on PostgreSQL through the mounted application, with real keys, over the
vault of the operator route sweep with ``redacted_family=True``: a public memory that is redacted, a derived commit, loop, report
and project state built from it, and five commits that quote it (two confirmed inline, one of them by an agent key).

* ``test_no_restricted_profile_is_shown_the_words_of_a_redacted_memory_on_any_route`` is the sweep. For each restricted profile it
  calls every ``/v0/vnext`` route the way the probe table does (plus the same call aimed at the redacted and derived rows by id,
  and every sensitivity where the route takes a filter) and requires that no answer carries a sentinel of the vault's hidden set,
  the words of the redacted memory included, and that no hidden row changed. It collects every leak before it fails.
* ``test_the_doors_withhold_the_quote_and_keep_the_id`` names the doors of the finding (recent commits with and without a short
  limit, the memory audit, the workspace with its recent events and its agent activity, ``alice_explain``,
  ``alice_memory_review`` detail and the legacy recent commits tool) and reads the ref a restricted key is shown, so an answer that
  is silent for another reason cannot pass.
* ``test_the_owner_and_an_unbound_admin_key_keep_the_quote_and_read_the_contained_rows`` is the control the other way, and says
  what the owner reads after a redaction (by id, in the lists and the dashboard; not through recall, the pack or the tree, which a
  request naming every sensitivity widens).
* ``test_the_dashboard_and_the_source_trace_withhold_the_quote`` runs the two routes whose rows carry a commit's metadata, and the
  events of the trace.
* ``test_every_spelling_of_a_memory_ref_is_withheld_...`` sends every wording of a memory ref through the commit route.
* ``test_the_update_candidate_of_a_contained_project_is_contained`` records a gap found while building the sweep.

Why the sweep is a file of its own and not a case of ``test_operator_routes_limits_postgres.py``: that file builds the plain
vault once per route and counts its rows, and the family is a second vault with a redacted memory, five more commits and
a project made after the scheduler runs. The two files read the same probe table (``PROBES``), so a route added to the
application gets a probe once and is swept by both, and ``tests/unit/test_operator_route_inventory.py`` still requires the table
to hold exactly the routes of the application.

Each test names the mutation that must fail it, and ``scripts/derived_label_mutations.json`` replays it.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError, MCPToolNotFoundError
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_probes import ALL_SENSITIVITY, PROBES, Call
from tests.integration.operator_route_runner import carried, run_call
from tests.integration.operator_route_vault import Vault

# profile name -> (permission profile, bound to the shown project)
RESTRICTED = {
    "trusted": ("trusted_local_agent", False),
    "read_only": ("read_only_agent", False),
    "memory_proposal": ("memory_proposal_agent", False),
    "trusted_bound": ("trusted_local_agent", True),
    "alpha_only": ("project_scoped_agent", True),
    "admin_bound": ("admin_agent", True),
}
# The routes that change the scheduler or a setting run last, so the sweep reads the vault before it changes.
LAST = {
    ("POST", "/v0/vnext/scheduler/pause"),
    ("POST", "/v0/vnext/scheduler/resume"),
    ("PATCH", "/v0/vnext/scheduler/workflows/{workflow_type}"),
    ("PATCH", "/v0/vnext/connectors/{connector_name}/config"),
    ("PUT", "/v0/vnext/settings/brain-charter"),
}
QUOTING = ("commit_quotes_redacted", "commit_quotes_redacted_typed", "commit_pending_quotes_redacted", "commit_confirmed_quotes_redacted")
# The commits that write the words of the memory as the name of a field, in the entry that names it and in the entry beside it.
KEYED = ("commit_keys_redacted", "commit_keys_redacted_typed")
# The commit the admin key asked to confirm inline and confirmed itself: its confirmation is an agent event, which the workspace
# lists in its agent activity as well as among the recent events.
BY_AN_AGENT = "commit_agent_confirmed_quotes_redacted"
REDACTED_MEMORIES = ("memory_redacted", "commit_of_redacted", *QUOTING, *KEYED, BY_AN_AGENT)
# The profiles that read the commits that quote the redacted memory but are shut out of the operator routes (a 403 on each).
SHUT_OUT_OF_ROUTES = ("read_only", "memory_proposal")
WORKSPACE = ("GET", "/v0/vnext/workspace")


def _keys(vault: Vault, harness) -> dict[str, str]:
    project = vault.ids["project_shown"]
    keys = {
        name: harness.key(permission, project=project if bound else None)
        for name, (permission, bound) in RESTRICTED.items()
        if name != "trusted"
    }
    keys["trusted"] = vault.keys["trusted"]
    return keys


def _aimed_at_the_redacted_rows(vault: Vault, route: tuple[str, str], base: list[Call]) -> list[Call]:
    """The probe calls, and the same calls aimed at the redacted memory and the rows built from it or quoting it."""

    memories = [vault.ids[name] for name in REDACTED_MEMORIES]
    targets = {
        "memory_id": memories,
        "loop_id": [vault.ids["loop_of_redacted"]],
        "artifact_id": [
            vault.ids["artifact_of_redacted"],
            vault.ids["ingest_quotes_redacted_artifact"],
            vault.ids["task_quotes_redacted_artifact"],
            vault.ids["artifact_shown"],
        ],
        "source_id": [vault.ids["ingest_quotes_redacted"]],
        "project_id": [vault.ids["project_of_redacted"]],
        "target_id": [
            *memories,
            vault.ids["loop_of_redacted"],
            vault.ids["artifact_of_redacted"],
            vault.ids["ingest_quotes_redacted"],
            vault.ids["ingest_quotes_redacted_artifact"],
            vault.ids["task_quotes_redacted_artifact"],
        ],
    }
    out = list(base)
    for call in base:
        for param, values in targets.items():
            if param in call.path:
                out += [
                    Call(f"{call.name}@{value[:8]}", path={**call.path, param: value}, query=call.query, body=call.body, mutates=call.mutates)
                    for value in values
                ]
            if call.body and param in call.body:
                out += [
                    Call(f"{call.name}@{value[:8]}", path=call.path, query=call.query, body={**call.body, param: value}, mutates=True)
                    for value in values
                ]
    method, template = route
    if method == "GET" and "{" not in template:
        out.append(Call("every sensitivity", query={"sensitivity_allowed": list(ALL_SENSITIVITY), "limit": 200}))
    if template == "/v0/vnext/context-packs":
        for query in ("SENTINEL HIDDEN", "redacted", "quotes redacted"):
            out.append(Call(f"pack {query}", body={"query": query, "options": {"sensitivity_allowed": list(ALL_SENSITIVITY), "limit": 50}}))
    return out


@pytest.mark.parametrize("profile", sorted(RESTRICTED))
def test_no_restricted_profile_is_shown_the_words_of_a_redacted_memory_on_any_route(label_harness, profile):
    """The vault holds a redacted public memory, rows built from it, and commits that quote it (two of them confirmed inline, so
    that their events hold the quote, one by the owner and one by an agent key). Every route is called by one restricted profile,
    and no answer carries the words of the memory, a title or text of a copy, or any other sentinel of the hidden set. No hidden
    row changes. Every leak is collected before the test fails, so one run names them all.

    The finding was that a commit's saved quote of a memory came back to an unbound ``trusted_local_agent`` key from the recent
    commits route, the memory audit and the workspace. This sweep reaches them (the quoting commits are public, so each key may
    read the commit) and every other route a commit's metadata could ride out of, the event feeds included (the workspace lists the
    confirmation of the agent key in its agent activity, which the owner's confirmation never reaches). The unbound trusted
    key must be shown the quoting commit somewhere; a read-only and a proposal key are shut out of every route (a 403) and are
    held to the same rule at the tools, in ``test_the_doors_withhold_the_quote_and_keep_the_id``.

    Mutations: delete the ``audit`` call on the reader in ``get_vnext_memory_audit``; delete the first ``withhold_saved_quotes``
    call of the workspace; delete the ``events`` call on the recent events of the workspace; delete the ``events`` call on its
    agent events; replace ``refused_memories`` with ``frozenset()`` in ``_verdict``. Each is in the manifest.
    """

    vault = Vault(label_harness, "q", redacted_family=True).build()
    key = _keys(vault, label_harness)[profile]
    before = vault.hidden_rows()
    # The workspace is read first: every other read of a restricted key appends policy events, and the agent activity lists only the
    # newest fifty agent events, so a later read could push the vault's own confirmation out of the window and the sweep would be silent.
    routes = sorted(PROBES, key=lambda route: (route in LAST, route != WORKSPACE, route[0] != "GET", route))
    shown: list[str] = []
    leaks: list[tuple[object, ...]] = []
    lists_the_agents_confirmation = False
    for route in routes:
        method, template = route
        for call in _aimed_at_the_redacted_rows(vault, route, PROBES[route](vault)):
            status, text = run_call(vault, method, template, call, key)
            leaked = [name for name in carried(vault, text) if vault.is_hidden(name)]
            if leaked:
                leaks.append((method, template, call.name, status, leaked))
            if status == 200 and vault.text("commit_quotes_redacted-title") in text:
                shown.append(f"{method} {template}")
            if route == WORKSPACE and status == 200:
                lists_the_agents_confirmation = lists_the_agents_confirmation or any(
                    event.get("target_id") == vault.ids[BY_AN_AGENT] and event.get("event_type") == "memory.updated"
                    for event in json.loads(text)["agent_activity"]["recent_events"]
                )
    assert leaks == [], (profile, len(leaks), leaks[:25])
    after = vault.hidden_rows()
    assert after == before, [key for key in after if after[key] != before.get(key)][:5]
    if profile == "trusted":
        assert shown, "a key that may read the quoting commit is shown the commit somewhere, so the sweep is not vacuous"
        assert lists_the_agents_confirmation, (
            "the workspace lists the confirmation an agent key made in its agent activity, so the sweep reaches that feed and is not "
            "silent because the feed holds nothing of the vault"
        )


def _all_text(*parts: object) -> str:
    return " ".join(json.dumps(part, default=str) for part in parts)


def _declared(vault: Vault, monkeypatch, harness, profile: str, tool: str, arguments: dict[str, object]):
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    return call_mcp_tool(context, name=tool, arguments={**arguments, "permission_profile": profile, "agent_id": f"declared-{profile}"})


def _tool(monkeypatch, harness, key: str, tool: str, arguments: dict[str, object]):
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    try:
        return call_mcp_tool(context, name=tool, arguments=arguments)
    except (MCPToolError, MCPToolNotFoundError):
        return None
    finally:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)


def _get(vault: Vault, template: str, key, **path_and_query):
    path = {name: value for name, value in path_and_query.items() if "{" + name + "}" in template}
    query = {name: value for name, value in path_and_query.items() if name not in path}
    return run_call(vault, "GET", template, Call("ad hoc", path=path, query=query), key)


def test_the_doors_withhold_the_quote_and_keep_the_id(label_harness, monkeypatch):
    """The doors of the finding, one by one, for every restricted profile that reaches them. A key that may read the quoting
    commit is shown the ref with the id of the memory and a ``null`` quote, in both spellings; none is shown the words. The
    workspace lists the events of the commit that was confirmed, among its recent events and, for the commit an agent key
    confirmed, in its agent activity too, and each event holds the ref with its id and no quote.

    Mutations: delete the reader call at the recent commits service (``if guard.active`` made false); delete the ``audit`` call
    of the audit route; delete the ``audit`` call of ``alice_explain``; delete the workspace's ``withhold_saved_quotes`` calls;
    delete the workspace's ``events`` call on the recent events; delete its ``events`` call on the agent events.
    """

    vault = Vault(label_harness, "d", redacted_family=True).build()
    keys = _keys(vault, label_harness)
    words = vault.text("memory_redacted-text")
    cited = vault.ids["memory_redacted"]
    plain, typed, pending, confirmed = (vault.ids[name] for name in QUOTING)
    by_an_agent = vault.ids[BY_AN_AGENT]
    keyed, keyed_typed = (vault.ids[name] for name in KEYED)
    # The commits that wrote the words as the name of a field (and the two confirmed commits, which wrote the words as a name beside
    # the quote) are shown the id and the quote marker, and no name of the field.
    expected = {
        plain: [{"memory_id": cited, "quote": None}],
        typed: [f"memory:{cited}", {"quote": None}],
        pending: [{"memory_id": cited, "quote": None}],
        confirmed: [{"memory_id": cited, "quote": None}],
        by_an_agent: [{"memory_id": cited, "quote": None}],
        keyed: [{"memory_id": cited}],
        keyed_typed: [f"memory:{cited}", {}],
    }
    problems: list[object] = []

    def refs_of(memory: dict) -> object:
        return memory["metadata_json"]["agentic_memory"]["source_refs"]

    def check(label: object, condition: bool) -> None:
        if not condition:
            problems.append(label)

    key = keys["trusted"]
    # The workspace is read first: every other read appends events, and the feed lists the newest twenty.
    status, text = _get(vault, "/v0/vnext/workspace", key)
    check(("workspace", status), status == 200 and words not in text)
    if status == 200:
        workspace = json.loads(text)
        review = {row["id"]: row for row in workspace["review_memories"]}
        check(("workspace review memory", pending), pending in review and refs_of(review[pending]) == expected[pending])
        recent = {row["id"]: row for row in workspace["agent_activity"]["recent_commits"]}
        check(("workspace recent commit", plain), plain in recent and refs_of(recent[plain]) == expected[plain])
        for feed, label, commit in (
            (workspace["recent_events"], "recent events", confirmed),
            (workspace["agent_activity"]["recent_events"], "agent activity", by_an_agent),
        ):
            events = [event for event in feed if event.get("target_id") == commit]
            check((f"workspace {label} list the events of the confirmed commit", len(events)), any(event.get("event_type") == "memory.updated" for event in events))
            for event in events:
                changes = (event.get("payload_json") or {}).get("changes") or {}
                agentic = (changes.get("metadata_json") or {}).get("agentic_memory") or {}
                if "source_refs" in agentic:
                    check((f"{label}: the event keeps the ref and loses the quote", event.get("event_type")), agentic["source_refs"] == expected[commit])
                    check((f"{label}: the event loses the excerpt", event.get("event_type")), "conversation_excerpt" not in agentic)
                    check((f"{label}: the event loses the quote of the value", event.get("event_type")), (changes.get("value") or {}).get("source_refs") == expected[commit])
    for limit in (100, 1):
        status, text = _get(vault, "/v0/vnext/memories/recent-commits", key, limit=limit)
        check(("recent commits", limit, status), status == 200 and words not in text)
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", key, limit=100)
    listed = {row["id"]: row for row in json.loads(text)["recent_commits"]} if status == 200 else {}
    for memory_id in (plain, typed, confirmed, by_an_agent, keyed, keyed_typed):
        check(("recent commits lists the ref", memory_id), memory_id in listed and refs_of(listed[memory_id]) == expected[memory_id])
    for memory_id, refs in expected.items():
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", key, memory_id=memory_id)
        check(("audit", memory_id, status), status == 200 and words not in text)
        if status == 200:
            body = json.loads(text)
            check(("audit ref", memory_id), refs_of(body["memory"]) == refs)
            check(
                ("audit parts", memory_id),
                all(words not in json.dumps(part, default=str) for part in (body["revisions"], body["events"], body["provenance_links"])),
            )
    for profile, profile_key in keys.items():
        for memory_id in (plain, typed, confirmed, by_an_agent, keyed, keyed_typed):
            explained = _tool(monkeypatch, label_harness, profile_key, "alice_explain", {"memory_id": memory_id})
            check(("explain", profile, memory_id), words not in _all_text(explained))
            detail = _tool(monkeypatch, label_harness, profile_key, "alice_memory_review", {"review_item_id": memory_id})
            check(("review detail", profile, memory_id), words not in _all_text(detail))
            if profile in {"trusted", "read_only", "memory_proposal"}:
                check(("explain reads the commit and keeps the id of the cited memory", profile, memory_id), explained is not None and cited in _all_text(explained))
    for profile in ("read_only", "trusted", "memory_proposal"):
        permission = RESTRICTED[profile][0]
        for limit in (100, 1):
            # A key is configured for this user, so the legacy tool refuses a call with a key; a call that declares a profile
            # is made with no key in the environment, as a client on an install with no keys makes it.
            try:
                result = _declared(vault, monkeypatch, label_harness, permission, "alice_vnext_recent_memory_commits", {"limit": limit})
            except (MCPToolError, MCPToolNotFoundError):
                continue
            check(("legacy recent commits", profile, limit), words not in _all_text(result))
    assert problems == [], problems


def _contained_texts(vault: Vault) -> dict[str, str]:
    return {name: vault.text(name) for name in ("commit_of_redacted-text", "artifact_of_redacted-title", "artifact_of_redacted-body")}


def test_the_owner_and_an_unbound_admin_key_keep_the_quote_and_read_the_contained_rows(label_harness, monkeypatch):
    """The unbound admin key is shown the quote at the audit route, the recent commits route, ``alice_explain`` and
    ``alice_memory_review`` detail, and reads the rows built from the redacted memory by id and in the lists (the artifact list,
    the project list, recent commits and the dashboard). It does not read them through recall, the context pack or the default
    context tree, which read an unverified row as regulated; a request that names every sensitivity (the ``sensitivity_allowed``
    argument of ``alice_recall`` and of the pack, the query of the tree) lists them. Every claim is checked on its own and every
    miss is reported together.

    Mutations: make ``SourceReadFence.entity_read_fenced`` true for an admin key (the quote goes); read an unverified row at its
    stored sensitivity in the default filter (the pack, the tree and recall list the rows).
    """

    vault = Vault(label_harness, "k", redacted_family=True).build()
    admin = vault.keys["admin"]
    words = vault.text("memory_redacted-text")
    problems: list[object] = []

    def check(label: object, condition: bool) -> None:
        if not condition:
            problems.append(label)

    # The workspace first (every other read appends events). The unbound admin key is shown the events as stored: the quote is in
    # the recent events and in the agent activity, so the restricted keys' silence at both is the reader's doing and not an empty feed.
    status, text = _get(vault, "/v0/vnext/workspace", admin)
    check(("workspace", status), status == 200)
    if status == 200:
        workspace = json.loads(text)
        for label, feed in (("recent events", workspace["recent_events"]), ("agent activity", workspace["agent_activity"]["recent_events"])):
            check((f"workspace {label} keep the quote", label), any(words in json.dumps(event, default=str) for event in feed))
    for name in (*QUOTING, BY_AN_AGENT):
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", admin, memory_id=vault.ids[name])
        check(("audit keeps the quote", name, status), status == 200 and words in text)
        check(("explain keeps the quote", name), words in _all_text(_tool(monkeypatch, label_harness, admin, "alice_explain", {"memory_id": vault.ids[name]})))
        check(("review detail keeps the quote", name), words in _all_text(_tool(monkeypatch, label_harness, admin, "alice_memory_review", {"review_item_id": vault.ids[name]})))
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", admin, limit=100)
    check(("recent commits list", status), status == 200 and words in text and vault.text("commit_of_redacted-text") in text)
    # The rows built from the memory are read by id, and in the lists.
    status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", admin, memory_id=vault.ids["commit_of_redacted"])
    check(("contained commit by id", status), status == 200 and vault.text("commit_of_redacted-text") in text)
    status, text = _get(vault, "/v0/vnext/artifacts/{artifact_id}", admin, artifact_id=vault.ids["artifact_of_redacted"])
    check(("contained artifact by id", status), status == 200 and vault.text("artifact_of_redacted-body") in text)
    status, text = _get(vault, "/v0/vnext/artifacts", admin, limit=100)
    check(("artifact list", status), status == 200 and vault.text("artifact_of_redacted-title") in text)
    status, text = _get(vault, "/v0/vnext/projects", admin, limit=100)
    check(("project list", status), status == 200 and vault.text("project_of_redacted-name") in text)
    status, text = _get(vault, "/v0/vnext/projects/{project_id}/dashboard", admin, project_id=vault.ids["project_of_redacted"])
    check(("dashboard", status), status == 200 and vault.text("project_of_redacted-name") in text)
    # Not through the pack, the tree or recall by default; a request that names every sensitivity lists them.
    contained = _contained_texts(vault)
    queries = ("SENTINEL", "SENTINEL HIDDEN", "commit_of_redacted", "artifact_of_redacted", "redacted")
    default_listed: set[str] = set()
    named_listed: set[str] = set()
    for query in queries:
        pack = {"query": query, "options": {"limit": 50, "max_items": 50}}
        status, text = run_call(vault, "POST", "/v0/vnext/context-packs", Call("default", body=pack), admin)
        default_listed |= {f"pack:{name}" for name, value in contained.items() if value in text}
        status, text = run_call(
            vault, "POST", "/v0/vnext/context-packs",
            Call("every sensitivity", body={**pack, "options": {**pack["options"], "sensitivity_allowed": list(ALL_SENSITIVITY)}}), admin,
        )
        named_listed |= {f"pack:{name}" for name, value in contained.items() if value in text}
        default = _all_text(_tool(monkeypatch, label_harness, admin, "alice_recall", {"query": query, "limit": 50}))
        default_listed |= {f"recall:{name}" for name, value in contained.items() if value in default}
        named = _all_text(_tool(monkeypatch, label_harness, admin, "alice_recall", {"query": query, "limit": 50, "sensitivity_allowed": list(ALL_SENSITIVITY)}))
        named_listed |= {f"recall:{name}" for name, value in contained.items() if value in named}
    check(("the pack and recall list no contained row by default", sorted(default_listed)), not default_listed)
    check(("the pack lists a contained row when every sensitivity is named", sorted(named_listed)), any(item.startswith("pack:") for item in named_listed))
    check(("recall lists a contained row when every sensitivity is named", sorted(named_listed)), any(item.startswith("recall:") for item in named_listed))
    # The tree takes a limit of 1 to 50 per root, and both calls ask for 50 so that the default one is silent for a reason.
    status, text = _get(vault, "/v0/vnext/context-tree", admin, limit=50)
    check(("the default tree lists no contained row", status), status == 200 and vault.text("artifact_of_redacted-title") not in text and vault.text("project_of_redacted-name") not in text)
    status, text = _get(vault, "/v0/vnext/context-tree", admin, sensitivity_allowed=list(ALL_SENSITIVITY), limit=50)
    check(("the tree lists a contained row when every sensitivity is named", status), status == 200 and (vault.text("artifact_of_redacted-title") in text or vault.text("project_of_redacted-name") in text))
    assert problems == [], problems


def test_the_owner_reads_the_quote_on_an_install_with_no_keys(label_harness):
    """With no agent key configured every call is the owner's: the audit and the recent commits route return the quote.

    Mutation: let ``SourceReadFence.fenced`` return true for a call with no identity.
    """

    vault = Vault(label_harness, "ow", owner=True, redacted_family=True).build()
    words = vault.text("memory_redacted-text")
    for name in (*QUOTING, BY_AN_AGENT):
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", None, memory_id=vault.ids[name])
        assert status == 200 and words in text, name
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", None, limit=100)
    assert status == 200 and words in text


def test_the_dashboard_and_the_source_trace_withhold_the_quote(label_harness):
    """``GET /v0/vnext/projects/{id}/dashboard`` lists the memories of a project with their metadata, and
    ``GET /v0/vnext/traces/sources/{id}`` lists the memories whose metadata names a source and the events about them. Two commits
    carry a quote of a memory that is then redacted: one stored as the commit route stores it, one confirmed inline (its event
    holds the quote). The quotes are not the words of the cited memory itself, which the dashboard lists too, so a control proves
    that each door carries the commit's own quote before the redaction. After it an unbound trusted key is shown the commit and
    none of the words; the unbound admin key is shown all.

    Mutations: delete the reader call of ``project_dashboard``; delete the reader call of ``_vnext_load_source_trace``; delete the
    ``events`` call of ``_vnext_load_source_trace``.
    """

    from alicebot_api.vnext_memory_commit import MemoryCommitRequest, VNextMemoryCommitService

    harness = label_harness
    vault = Vault(harness, "tr").build()
    admin, trusted = vault.keys["admin"], vault.keys["trusted"]
    cited_words = f"Atlas played {uuid4().hex} for 115 hours"
    quote = f"Atlas quote {uuid4().hex} of the notes"
    excerpt = f"Atlas excerpt {uuid4().hex} of the notes"
    # One transaction for the rows, one for the commit, one for its confirmation and one for the metadata write, as the routes
    # make them: a strict test refuses the graph lock after the label lock, and each of those takes one.
    with harness.store() as store:
        project = store.create_project({"name": "Atlas", "slug": f"atlas-{uuid4().hex[:8]}", "domain": "project", "sensitivity": "public"})
        project_id = str(project["id"])
        source = store.create_source(
            {"source_type": "note", "title": "Atlas notes", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public",
             "metadata_json": {"raw_text": "Atlas notes"}}
        )
        source_id = str(source["id"])
        cited = store.create_memory(
            {"memory_key": f"tr-{uuid4().hex[:8]}", "canonical_text": cited_words, "title": cited_words, "status": "active", "domain": "project",
             "sensitivity": "public", "metadata_json": {"project_scope": [project_id]}}
        )
        cited_id = str(cited["id"])
        # A commit as the commit route stores it, listed by the project and the trace: active, in the project, naming the source.
        stored = store.create_memory(
            {
                "memory_key": f"tr-commit-{uuid4().hex[:8]}", "canonical_text": "Atlas follow up on the games note", "title": "Atlas follow up",
                "status": "active", "domain": "project", "sensitivity": "public",
                "metadata_json": {
                    "project_scope": [project_id], "source_refs": [source_id],
                    "agentic_memory": {
                        "kind": "agentic_memory_commit", "status": "committed", "conversation_excerpt": excerpt,
                        "source_refs": [source_id, {"memory_id": cited_id, "quote": quote}],
                    },
                },
                "value": {"text": "Atlas follow up", "source_refs": [source_id, {"memory_id": cited_id, "quote": quote}]},
            }
        )
        stored_id = str(stored["id"])
    with harness.store() as store:
        asked = VNextMemoryCommitService(store, defer_embeddings=True).commit(
            identity=None,
            request=MemoryCommitRequest(
                user_id=str(harness.user_id), title="Atlas confirmed follow up", canonical_text="Atlas confirmed follow up on the games note",
                memory_type="semantic", domain="project", sensitivity="public", confidence=0.6,
                source_refs=(source_id, {"memory_id": cited_id, "quote": quote}), conversation_excerpt=excerpt, project_scope=(project_id,),
            ),
        )
    assert asked["status"] == "confirmation_required", str(asked)[:300]
    with harness.store() as store:
        VNextMemoryCommitService(store, defer_embeddings=True).confirm(
            identity=None, confirmation_id=asked["memory"]["confirmation_id"], action="confirm"
        )
    confirmed_id = str(asked["memory"]["id"])
    with harness.store() as store:
        row = store.get_memory(confirmed_id)
        store.update_memory(
            memory_id=confirmed_id, patch={"metadata_json": {**row["metadata_json"], "source_refs": [source_id]}}, actor_type="system"
        )

    def doors(key):
        return {
            "dashboard": _get(vault, "/v0/vnext/projects/{project_id}/dashboard", key, project_id=project_id),
            "trace": _get(vault, "/v0/vnext/traces/sources/{source_id}", key, source_id=source_id),
        }

    problems: list[object] = []

    def check(label: object, condition: bool) -> None:
        if not condition:
            problems.append(label)

    for door, (status, text) in doors(admin).items():
        check(("control carries the quote", door, status), status == 200 and quote in text and stored_id in text)
    status, text = doors(admin)["trace"]
    traced = json.loads(text)["events"] if status == 200 else []
    check(
        ("control: an event of the confirmed commit carries the quote", status),
        any(event.get("target_id") == confirmed_id and quote in json.dumps(event, default=str) for event in traced),
    )
    status, body = vault.admin_request("POST", "/v0/vnext/memories/redact", {"memory_id": cited_id, "reason": "sweep"})
    assert status == 200, (status, str(body)[:300])
    for door, (status, text) in doors(trusted).items():
        check(("trusted", door, status), status == 200 and quote not in text and excerpt not in text and cited_words not in text)
        check(("the commit is still listed", door), stored_id in text)
    status, text = doors(trusted)["trace"]
    traced = json.loads(text)["events"] if status == 200 else []
    check(
        ("the trace still lists the events of the confirmed commit", status),
        any(event.get("target_id") == confirmed_id for event in traced),
    )
    check(("no event of the trace carries the quote", status), all(quote not in json.dumps(event, default=str) for event in traced))
    for door, (status, text) in doors(admin).items():
        check(("admin keeps the quote", door, status), status == 200 and quote in text)
    assert problems == [], problems


def _spellings(cited: str, quote_of) -> dict[str, tuple[list[object], bool]]:
    """Every spelling of a memory ref, as the HTTP commit route is sent them, each with a quote of its own. The second value says
    whether the commit also saves the quote as its ``conversation_excerpt`` (the ones that name the memory in a string or a URL
    hold no quote in the ref)."""

    def q(name: str) -> str:
        return quote_of(name)

    return {
        "memory_id": ([{"memory_id": cited, "quote": q("memory_id")}], False),
        "memory prefix + quote entry": ([f"memory:{cited}", {"quote": q("memory prefix + quote entry")}], False),
        "MEMORY prefix": ([f"MEMORY:{cited}", {"quote": q("MEMORY prefix")}], False),
        "ref key": ([{"ref": f"memory:{cited}", "quote": q("ref key")}], False),
        "id key": ([{"id": f"memory:{cited}", "quote": q("id key")}], False),
        "memory_ids list": ([{"memory_ids": [cited], "quote": q("memory_ids list")}], False),
        "memory_refs": ([{"memory_refs": [f"memory:{cited}"], "quote": q("memory_refs")}], False),
        "no hyphens": ([{"memory_id": cited.replace("-", ""), "quote": q("no hyphens")}], False),
        "upper case": ([{"memory_id": cited.upper(), "quote": q("upper case")}], False),
        "braces": ([{"memory_id": "{" + cited + "}", "quote": q("braces")}], False),
        "urn": ([{"memory_id": "urn:uuid:" + cited, "quote": q("urn")}], False),
        "json text": ([json.dumps({"memory_id": cited, "quote": q("json text")})], False),
        "alice url": ([f"alice://memories/{cited}", {"quote": q("alice url")}], False),
        "nested": ([{"evidence": [{"memory_id": cited, "quote": q("nested")}]}], False),
        "conversation_excerpt": ([f"memory:{cited}"], True),
        # Wordings no marker covers: the commit route saves the excerpt it was sent whatever the ref says.
        "Memory <id>": ([f"Memory {cited}"], True),
        "mem:<id>": ([f"mem:{cited}"], True),
        "https url": ([f"https://host.example.test/memories/{cited}"], True),
        "alice://memory/<id>": ([f"alice://memory/{cited}", {"quote": q("alice://memory/<id>")}], False),
        "see memory: <id>": ([f"see memory: {cited}"], True),
        "markdown link": ([f"[memory]({cited})"], True),
        "key memory": ([{"memory": cited, "quote": q("key memory")}], False),
        "key origin": ([{"origin": cited, "quote": q("key origin")}], False),
        "key ref_id": ([{"ref_id": cited, "quote": q("key ref_id")}], False),
        "key parent_memory_id": ([{"parent_memory_id": cited, "quote": q("key parent_memory_id")}], False),
        "key supersedes": ([{"supersedes": cited, "quote": q("key supersedes")}], False),
        "text beside the id": ([{"memory_id": cited, "text": q("text beside the id")}], False),
        # Words written as the name of a field, whatever its value is: the name goes with the value.
        "key with a string value": ([{"memory_id": cited, q("key with a string value"): "x"}], False),
        "key with a null value": ([{"memory_id": cited, q("key with a null value"): None}], False),
        "key with a number value": ([{"memory_id": cited, q("key with a number value"): 1}], False),
        "key nested": ([{"memory_id": cited, "evidence": {q("key nested"): None}}], False),
        "key in json text": ([json.dumps({"memory_id": cited, q("key in json text"): None})], False),
        "key in the entry beside": ([f"memory:{cited}", {q("key in the entry beside"): None}], False),
        "key and a quote null": ([{"memory_id": cited, "quote": None, q("key and a quote null"): True}], False),
        "key beside an unmarked id": ([{"origin": cited, q("key beside an unmarked id"): None}], False),
        # The entry beside the ref: every string and every name of it goes, not only a field called ``quote``, and an id it
        # carries in a field that is not a reference names nothing.
        "companion bare string": ([f"memory:{cited}", q("companion bare string")], False),
        "companion text field": ([f"memory:{cited}", {"text": q("companion text field")}], False),
        "companion nested list": ([f"memory:{cited}", [[q("companion nested list")]]], False),
        "companion json text": ([f"memory:{cited}", json.dumps({"note": q("companion json text")})], False),
        "companion quote and a chunk id": (
            [f"memory:{cited}", {"quote": q("companion quote and a chunk id"), "chunk_id": str(uuid4())}], False
        ),
        # A quote that is an object or a list holds the id, and a quote with the id typed in its text names the memory.
        "quote is an object that holds the id": ([{"quote": {"memory_id": cited, "text": q("quote is an object that holds the id")}}], False),
        "quote is a list that holds the id": ([{"quote": [{"memory_id": cited, "text": q("quote is a list that holds the id")}]}], False),
        "quote with the marker in its text": ([{"quote": f"memory:{cited} {q('quote with the marker in its text')}"}], False),
        "excerpt that names the memory": ([], True),
        # Words typed behind the id: in a fragment of a reference, and after the id in a field that holds an id.
        "marker and a fragment": ([f"memory:{cited}#{q('marker and a fragment').replace(' ', '-')}"], False),
        "alice url and a fragment": ([f"alice://memories/{cited}#{q('alice url and a fragment').replace(' ', '-')}"], False),
        "text directive": ([f"memory:{cited}#:~:text={q('text directive').replace(' ', '%20')}"], False),
        "ref key and a fragment": ([{"ref": f"memory:{cited}#{q('ref key and a fragment').replace(' ', '-')}"}], False),
        "memory_id and a fragment": ([{"memory_id": f"{cited}#{q('memory_id and a fragment').replace(' ', '-')}"}], False),
        "memory_id and words": ([{"memory_id": f"{cited}: {q('memory_id and words')}"}], False),
    }


@pytest.mark.parametrize("change", ["redacted", "confidential"])
def test_every_spelling_of_a_memory_ref_is_withheld_at_the_audit_the_recent_commits_and_explain(label_harness, monkeypatch, change):
    """One commit per spelling of a memory ref, through the commit route, over a public memory that is then redacted or relabelled
    confidential. An unbound trusted key and a read-only key are shown none of the quote at the audit route, the recent commits
    route (with a short limit as well) and ``alice_explain``; the unbound admin key is shown all of them at the audit route.
    A control reads the commits before the change, so an absent quote was withheld. Every miss is reported together.

    Mutations: delete a spelling from ``_memory_ids_in_text`` (the ``alice://memories/`` test) or a key from
    ``MEMORY_REFERENCE_KEYS``: a marked commit keeps the quote; read only the named ids of a row in ``_refused_memories``: the
    unmarked commits keep it.
    """

    harness = label_harness
    vault = Vault(harness, "sp").build()
    admin, trusted = vault.keys["admin"], vault.keys["trusted"]
    read_only = harness.key("read_only_agent")
    sentinel = f"ZQXSPELL{uuid4().hex[:10]}"
    with harness.store() as store:
        cited = store.create_memory(
            {"memory_key": f"sp-{uuid4().hex[:8]}", "canonical_text": f"Atlas played {sentinel} for 115 hours", "title": "Atlas games",
             "status": "active", "domain": "project", "sensitivity": "public", "metadata_json": {}}
        )
    cited_id = str(cited["id"])
    commits: dict[str, str] = {}

    def quote_of(name: str) -> str:
        return f"Atlas played {sentinel} for 115 hours ({name})"

    for name, (refs, saves_excerpt) in _spellings(cited_id, quote_of).items():
        payload = {
            "title": f"Follow up {name}", "canonical_text": f"Follow up on the games note {name}", "memory_type": "fact",
            "domain": "project", "sensitivity": "public", "confidence": 0.99, "source_type": "agent", "source_refs": refs,
        }
        if saves_excerpt:
            payload["conversation_excerpt"] = (
                f"[memory:{cited_id}] {quote_of(name)}" if name == "excerpt that names the memory" else quote_of(name)
            )
        status, body = vault.admin_request("POST", "/v0/vnext/memories/commit", payload)
        assert status in {200, 201}, (name, status, str(body)[:300])
        commits[name] = str(body["memory"]["id"])

    def read(key, memory_id):
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", key, memory_id=memory_id)
        explained = _tool(monkeypatch, harness, key, "alice_explain", {"memory_id": memory_id})
        return status, text, _all_text(explained)

    problems: list[object] = []
    for name, memory_id in commits.items():
        status, text, explained = read(admin, memory_id)
        if not (status == 200 and sentinel in text):
            problems.append(("control", name, status))
    if change == "redacted":
        status, body = vault.admin_request("POST", "/v0/vnext/memories/redact", {"memory_id": cited_id, "reason": "sweep"})
    else:
        status, body = vault.admin_request(
            "POST", f"/v0/vnext/memories/{cited_id}/review", {"action": "edit", "sensitivity": "confidential"}
        )
    assert status == 200, (change, status, str(body)[:300])
    for name, memory_id in commits.items():
        for label, key in (("trusted", trusted), ("read_only", read_only)):
            status, text, explained = read(key, memory_id)
            if sentinel in text or sentinel in explained:
                problems.append((change, label, name))
        status, text, _explained = read(admin, memory_id)
        if not (status == 200 and sentinel in text):
            problems.append((change, "admin", name, status))
    for limit in (100, 1):
        status, text = _get(vault, "/v0/vnext/memories/recent-commits", trusted, limit=limit)
        if not (status == 200 and sentinel not in text):
            problems.append((change, "recent commits", limit, status))
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", admin, limit=100)
    if not (status == 200 and sentinel in text):
        problems.append((change, "recent commits admin", status))
    assert problems == [], problems


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the project update scan takes the newest active project and writes an update candidate whose title and body name it; "
        "the candidate records the project's sources and memories as its inputs and not the project, so a project that is "
        "itself contained (derived from a redacted memory) gives a candidate that is not contained, and an unbound trusted key "
        "reads the project's name in the artifact list; the security note lists it, and the day a candidate inherits the "
        "containment of its project this test fails and the entry is deleted"
    ),
)
def test_the_update_candidate_of_a_contained_project_is_contained(label_harness):
    """The scheduler runs a project update scan as the admin after the vault is built. With ``contained_project_scanned=True`` the
    project derived from the redacted memory exists when the scan runs, so the scan writes its update candidate, and the candidate
    is public and readable. The unbound trusted key is shown the name of the contained project in the artifact list.
    """

    vault = Vault(label_harness, "p", redacted_family=True, contained_project_scanned=True).build()
    status, text = _get(vault, "/v0/vnext/artifacts", vault.keys["trusted"])
    assert status == 200 and vault.text("project_of_redacted-name") not in text
