"""A saved quote of a memory is withheld at every door of the mounted application, and the operator route sweep holds it.

Unreleased (on main, not in v0.20.0). A commit can cite a memory as ``{"memory_id": "<id>", "quote": "..."}`` or as the two
entries ``"memory:<id>"`` and ``{"quote": "..."}``. The quote holds words of the memory, the write check reads neither shape, and
on main until this change every reader of the commit returned the words after the memory was redacted. The reader of saved quotes
now withholds them from a caller who may not read the memory. These tests run that on PostgreSQL through the mounted application,
with real keys, over the vault of the operator route sweep with ``redacted_family=True``: a public memory that is redacted, a
derived commit, loop, report and project state built from it, and three commits that quote it.

* ``test_no_restricted_profile_is_shown_the_words_of_a_redacted_memory_on_any_route`` is the sweep. For each restricted profile it
  calls every ``/v0/vnext`` route the way the probe table does (plus the same call aimed at the redacted and derived rows by id,
  and every sensitivity where the route takes a filter) and requires that no answer carries a sentinel of the vault's hidden set,
  the words of the redacted memory included, and that no hidden row changed.
* ``test_the_doors_withhold_the_quote_and_keep_the_id`` names the doors of the finding (recent commits with and without a short
  limit, the memory audit, the workspace, ``alice_explain``, ``alice_memory_review`` detail and the legacy recent commits tool) and
  reads the ref a restricted key is shown, so an answer that is silent for another reason cannot pass.
* ``test_the_owner_and_an_unbound_admin_key_keep_the_quote_and_read_the_contained_rows`` is the control the other way, and says
  what the owner reads after a redaction (by id, in the lists and the dashboard; not through recall, the pack or the tree).
* ``test_the_dashboard_and_the_source_trace_withhold_the_quote`` runs the two routes whose rows carry a commit's metadata.

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
QUOTING = ("commit_quotes_redacted", "commit_quotes_redacted_typed", "commit_pending_quotes_redacted")
REDACTED_MEMORIES = ("memory_redacted", "commit_of_redacted", *QUOTING)


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
        "artifact_id": [vault.ids["artifact_of_redacted"]],
        "project_id": [vault.ids["project_of_redacted"]],
        "target_id": [*memories, vault.ids["loop_of_redacted"], vault.ids["artifact_of_redacted"]],
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
    """The vault holds a redacted public memory, rows built from it, and commits that quote it. Every route is called by one
    restricted profile, and no answer carries the words of the memory, a title or text of a copy, or any other sentinel of the
    hidden set. No hidden row changes.

    The finding was that a commit's saved quote of a memory came back to an unbound ``trusted_local_agent`` key from the recent
    commits route, the memory audit and the workspace. This sweep reaches them (the quoting commits are public, so each key may
    read the commit) and every other route a commit's metadata could ride out of.

    Mutations: delete the ``audit`` call on the reader in ``get_vnext_memory_audit``; delete the first ``withhold_saved_quotes``
    call of the workspace; replace ``refused_memories`` with ``frozenset()`` in ``_verdict``. Each is in the manifest.
    """

    vault = Vault(label_harness, "q", redacted_family=True).build()
    key = _keys(vault, label_harness)[profile]
    before = vault.hidden_rows()
    routes = sorted(PROBES, key=lambda route: (route in LAST, route[0] != "GET", route))
    shown: list[str] = []
    for route in routes:
        method, template = route
        for call in _aimed_at_the_redacted_rows(vault, route, PROBES[route](vault)):
            status, text = run_call(vault, method, template, call, key)
            leaked = [name for name in carried(vault, text) if vault.is_hidden(name)]
            assert leaked == [], (profile, method, template, call.name, status, leaked)
            if status == 200 and any(vault.text(name) in text for name in ("commit_quotes_redacted-title",)):
                shown.append(f"{method} {template}")
    after = vault.hidden_rows()
    assert after == before, [key for key in after if after[key] != before.get(key)][:5]
    if profile in {"trusted", "read_only", "memory_proposal"}:
        assert shown, "a key that may read the quoting commit is shown the commit somewhere, so the sweep is not vacuous"


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
    commit is shown the ref with the id of the memory and a ``null`` quote, in both spellings; none is shown the words.

    Mutations: delete the reader call at the recent commits service (``if guard.active`` made false); delete the ``audit`` call
    of the audit route; delete the ``audit`` call of ``alice_explain``; delete the workspace's ``withhold_saved_quotes`` calls.
    """

    vault = Vault(label_harness, "d", redacted_family=True).build()
    keys = _keys(vault, label_harness)
    words = vault.text("memory_redacted-text")
    cited = vault.ids["memory_redacted"]
    plain, typed, pending = (vault.ids[name] for name in QUOTING)
    expected = {plain: [{"memory_id": cited, "quote": None}], typed: [f"memory:{cited}", {"quote": None}], pending: [{"memory_id": cited, "quote": None}]}

    def refs_of(memory: dict) -> object:
        return memory["metadata_json"]["agentic_memory"]["source_refs"]

    for profile in ("trusted",):
        key = keys[profile]
        for limit in (100, 1):
            status, text = _get(vault, "/v0/vnext/memories/recent-commits", key, limit=limit)
            assert status == 200 and words not in text, (profile, limit)
        status, text = _get(vault, "/v0/vnext/memories/recent-commits", key, limit=100)
        listed = {row["id"]: row for row in json.loads(text)["recent_commits"]}
        assert refs_of(listed[plain]) == expected[plain] and refs_of(listed[typed]) == expected[typed]
        for memory_id, refs in expected.items():
            status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", key, memory_id=memory_id)
            assert status == 200 and words not in text, memory_id
            body = json.loads(text)
            assert refs_of(body["memory"]) == refs
            assert all(words not in json.dumps(part, default=str) for part in (body["revisions"], body["events"], body["provenance_links"]))
        status, text = _get(vault, "/v0/vnext/workspace", key)
        workspace = json.loads(text)
        assert status == 200 and words not in text
        review = {row["id"]: row for row in workspace["review_memories"]}
        assert refs_of(review[pending]) == expected[pending]
        assert refs_of({row["id"]: row for row in workspace["agent_activity"]["recent_commits"]}[plain]) == expected[plain]
    for profile, key in keys.items():
        for memory_id in (plain, typed):
            explained = _tool(monkeypatch, label_harness, key, "alice_explain", {"memory_id": memory_id})
            assert words not in _all_text(explained), (profile, "explain")
            detail = _tool(monkeypatch, label_harness, key, "alice_memory_review", {"review_item_id": memory_id})
            assert words not in _all_text(detail), (profile, "review detail")
            if profile in {"trusted", "read_only", "memory_proposal"} and explained is not None:
                assert cited in _all_text(explained), (profile, "explain keeps the id of the cited memory")
    for profile in ("read_only", "trusted", "memory_proposal"):
        permission = RESTRICTED[profile][0]
        for limit in (100, 1):
            # A key is configured for this user, so the legacy tool refuses a call with a key; a call that declares a profile
            # is made with no key in the environment, as a client on an install with no keys makes it.
            try:
                result = _declared(vault, monkeypatch, label_harness, permission, "alice_vnext_recent_memory_commits", {"limit": limit})
            except (MCPToolError, MCPToolNotFoundError):
                continue
            assert words not in _all_text(result), (profile, limit)


def test_the_owner_and_an_unbound_admin_key_keep_the_quote_and_read_the_contained_rows(label_harness, monkeypatch):
    """The unbound admin key is shown the quote at the audit route, the recent commits route, ``alice_explain`` and
    ``alice_memory_review`` detail, and reads the rows built from the redacted memory by id and in the lists (the artifact list,
    the project list, recent commits and the dashboard). It does not read them through the context pack, or the default context
    tree, which read an unverified row as regulated; a request that names every sensitivity lists them.

    Mutations: make ``SourceReadFence.entity_read_fenced`` true for an admin key (the quote goes); read an unverified row at its
    stored sensitivity in the default filter (the pack and the tree list the rows).
    """

    vault = Vault(label_harness, "k", redacted_family=True).build()
    admin = vault.keys["admin"]
    words = vault.text("memory_redacted-text")
    for name in QUOTING:
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", admin, memory_id=vault.ids[name])
        assert status == 200 and words in text, name
        assert words in _all_text(_tool(monkeypatch, label_harness, admin, "alice_explain", {"memory_id": vault.ids[name]})), name
        assert words in _all_text(_tool(monkeypatch, label_harness, admin, "alice_memory_review", {"review_item_id": vault.ids[name]})), name
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", admin, limit=100)
    assert status == 200 and words in text and vault.text("commit_of_redacted-text") in text
    # The rows built from the memory are read by id, and in the lists.
    status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", admin, memory_id=vault.ids["commit_of_redacted"])
    assert status == 200 and vault.text("commit_of_redacted-text") in text
    status, text = _get(vault, "/v0/vnext/artifacts/{artifact_id}", admin, artifact_id=vault.ids["artifact_of_redacted"])
    assert status == 200 and vault.text("artifact_of_redacted-body") in text
    status, text = _get(vault, "/v0/vnext/artifacts", admin, limit=100)
    assert status == 200 and vault.text("artifact_of_redacted-title") in text
    status, text = _get(vault, "/v0/vnext/projects", admin, limit=100, status="")
    assert status == 200 and vault.text("project_of_redacted-name") in text
    status, text = _get(vault, "/v0/vnext/projects/{project_id}/dashboard", admin, project_id=vault.ids["project_of_redacted"])
    assert status == 200 and vault.text("project_of_redacted-name") in text
    # Not through the pack, the tree or recall by default; a request that names every sensitivity lists them.
    pack = {"query": "SENTINEL", "options": {"limit": 50, "max_items": 50}}
    status, text = run_call(vault, "POST", "/v0/vnext/context-packs", Call("default", body=pack), admin)
    assert status in (200, 201) and vault.text("artifact_of_redacted-title") not in text and vault.text("commit_of_redacted-text") not in text
    status, text = run_call(
        vault, "POST", "/v0/vnext/context-packs",
        Call("every sensitivity", body={**pack, "options": {**pack["options"], "sensitivity_allowed": list(ALL_SENSITIVITY)}}), admin,
    )
    assert status in (200, 201) and (vault.text("commit_of_redacted-text") in text or vault.text("artifact_of_redacted-title") in text)
    status, text = _get(vault, "/v0/vnext/context-tree", admin)
    assert status == 200 and vault.text("artifact_of_redacted-title") not in text and vault.text("project_of_redacted-name") not in text
    status, text = _get(vault, "/v0/vnext/context-tree", admin, sensitivity_allowed=list(ALL_SENSITIVITY), limit=100)
    assert status == 200 and (vault.text("artifact_of_redacted-title") in text or vault.text("project_of_redacted-name") in text)


def test_the_owner_reads_the_quote_on_an_install_with_no_keys(label_harness):
    """With no agent key configured every call is the owner's: the audit and the recent commits route return the quote.

    Mutation: let ``SourceReadFence.fenced`` return true for a call with no identity.
    """

    vault = Vault(label_harness, "ow", owner=True, redacted_family=True).build()
    words = vault.text("memory_redacted-text")
    for name in QUOTING:
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", None, memory_id=vault.ids[name])
        assert status == 200 and words in text, name
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", None, limit=100)
    assert status == 200 and words in text


def test_the_dashboard_and_the_source_trace_withhold_the_quote(label_harness):
    """``GET /v0/vnext/projects/{id}/dashboard`` lists the memories of a project with their metadata, and
    ``GET /v0/vnext/traces/sources/{id}`` lists the memories whose metadata names a source. A commit that cites a memory and
    sits in the project, or names the source at the top of its metadata, carries the quote in both. After the memory is redacted
    an unbound trusted key is shown the commit and none of the words; the unbound admin key is shown all.

    Mutations: delete the reader call of ``project_dashboard``; delete the reader call of ``_vnext_load_source_trace``.
    """

    harness = label_harness
    vault = Vault(harness, "tr").build()
    admin, trusted = vault.keys["admin"], vault.keys["trusted"]
    words = f"Atlas played {uuid4().hex} for 115 hours"
    with harness.store() as store:
        project = store.create_project({"name": "Atlas", "slug": f"atlas-{uuid4().hex[:8]}", "domain": "project", "sensitivity": "public"})
        source = store.create_source(
            {"source_type": "note", "title": "Atlas notes", "content_hash": str(uuid4()), "domain": "project", "sensitivity": "public",
             "metadata_json": {"raw_text": "Atlas notes"}}
        )
        cited = store.create_memory(
            {"memory_key": f"tr-{uuid4().hex[:8]}", "canonical_text": words, "title": words, "status": "active", "domain": "project",
             "sensitivity": "public", "metadata_json": {"project_scope": [str(project["id"])]}}
        )
    status, body = vault.admin_request(
        "POST", "/v0/vnext/memories/commit",
        {
            "title": "Atlas follow up", "canonical_text": "Atlas follow up on the games note", "memory_type": "fact", "domain": "project",
            "sensitivity": "public", "confidence": 0.99, "source_type": "agent", "project_scope": [str(project["id"])],
            "source_refs": [str(source["id"]), {"memory_id": str(cited["id"]), "quote": words}],
        },
    )
    assert status in {200, 201}, (status, str(body)[:300])
    commit_id = str(body["memory"]["id"])
    with harness.store() as store:
        row = store.get_memory(commit_id)
        store.update_memory(
            memory_id=commit_id, patch={"metadata_json": {**row["metadata_json"], "source_refs": [str(source["id"])]}}, actor_type="system"
        )

    def doors(key):
        return {
            "dashboard": _get(vault, "/v0/vnext/projects/{project_id}/dashboard", key, project_id=str(project["id"])),
            "trace": _get(vault, "/v0/vnext/traces/sources/{source_id}", key, source_id=str(source["id"])),
        }

    for door, (status, text) in doors(admin).items():
        assert status == 200 and words in text, ("control", door)
    status, body = vault.admin_request("POST", "/v0/vnext/memories/redact", {"memory_id": str(cited["id"]), "reason": "sweep"})
    assert status == 200, (status, str(body)[:300])
    for door, (status, text) in doors(trusted).items():
        assert status == 200 and words not in text, ("trusted", door)
        assert commit_id in text, (door, "the commit is still listed")
    for door, (status, text) in doors(admin).items():
        assert status == 200 and words in text, ("admin", door)


def _spellings(cited: str, quote_of) -> dict[str, tuple[list[object], bool]]:
    """Every spelling of a memory ref, as the HTTP commit route is sent them, each with a quote of its own. The second value says
    whether the commit also saves the quote as its ``conversation_excerpt`` (the last one does, and its ref holds no quote)."""

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
    }


@pytest.mark.parametrize("change", ["redacted", "confidential"])
def test_every_spelling_of_a_memory_ref_is_withheld_at_the_audit_the_recent_commits_and_explain(label_harness, monkeypatch, change):
    """One commit per spelling of a memory ref, through the commit route, over a public memory that is then redacted or relabelled
    confidential. An unbound trusted key and a read-only key are shown none of the quote at the audit route, the recent commits
    route (with a short limit as well) and ``alice_explain``; the unbound admin key is shown all of them at the audit route.
    A control reads the commits before the change, so an absent quote was withheld.

    Mutations: delete a spelling from ``_memory_ids_in_text`` (the ``alice://memories/`` test) or a key from
    ``MEMORY_REFERENCE_KEYS``: its commit keeps the quote.
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
            payload["conversation_excerpt"] = quote_of(name)
        status, body = vault.admin_request("POST", "/v0/vnext/memories/commit", payload)
        assert status in {200, 201}, (name, status, str(body)[:300])
        commits[name] = str(body["memory"]["id"])

    def read(key, name, memory_id):
        status, text = _get(vault, "/v0/vnext/memories/{memory_id}/audit", key, memory_id=memory_id)
        explained = _tool(monkeypatch, harness, key, "alice_explain", {"memory_id": memory_id})
        return status, text, _all_text(explained)

    for name, memory_id in commits.items():
        status, text, explained = read(admin, name, memory_id)
        assert status == 200 and sentinel in text, ("control", name)
    if change == "redacted":
        status, body = vault.admin_request("POST", "/v0/vnext/memories/redact", {"memory_id": cited_id, "reason": "sweep"})
        assert status == 200, (status, str(body)[:300])
    else:
        status, body, _headers = harness.relabel("memory", cited_id, sensitivity="confidential")
        assert status == 200, (status, str(body)[:300])
    for name, memory_id in commits.items():
        for label, key in (("trusted", trusted), ("read_only", read_only)):
            status, text, explained = read(key, name, memory_id)
            assert sentinel not in text and sentinel not in explained, (change, label, name)
        status, text, _explained = read(admin, name, memory_id)
        assert status == 200 and sentinel in text, (change, "admin", name)
    for limit in (100, 1):
        status, text = _get(vault, "/v0/vnext/memories/recent-commits", trusted, limit=limit)
        assert status == 200 and sentinel not in text, (change, limit)
    status, text = _get(vault, "/v0/vnext/memories/recent-commits", admin, limit=100)
    assert status == 200 and sentinel in text
