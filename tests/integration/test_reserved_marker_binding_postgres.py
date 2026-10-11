"""A key stored with the reserved project name as its binding is refused at every door, on PostgreSQL.

``~global`` is the name Alice puts in a request tuple to ask for the rows that belong to no project. A key whose
stored binding is that name (an older version let one be created) used to read the rows of every free-form project
through ``alice_memory_review``, because the project test read the binding as that request. The key is refused whole
now: at every ``/v0/vnext`` route, through the mounted application, and at every tool, with real keys. An ordinary
bound key, an unbound admin key and the owner read what they read before.

The same cases on SQLite, with per-project scoping on and off, are in ``tests/unit/test_reserved_marker_key_binding.py``.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.mcp.types import MCPNotPermittedError, MCPRuntimeContext
from alicebot_api.vnext_agent_keys import agent_key_prefix, hash_agent_key, mint_agent_key
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401  (fixture)
from tests.integration.operator_route_probes import PROBES
from tests.integration.operator_route_runner import carried, run_call
from tests.integration.operator_route_vault import Vault

ORCHARD, LIGHTHOUSE = "Orchard", "Lighthouse"
ALICE_PROJECT = "prj_" + "a" * 16
TEXTS = {
    "orchard": "Synthetic fact about the orchard project",
    "lighthouse": "Synthetic fact about the lighthouse project",
    "global": "Synthetic fact that belongs to no project",
    "alice": "Synthetic fact about the registered project",
}
SCOPES = {"orchard": [ORCHARD], "lighthouse": [LIGHTHOUSE], "global": [], "alice": [ALICE_PROJECT]}
READ_TOOLS: tuple[tuple[str, dict], ...] = (
    ("alice_memory_review", {"status": "all"}),
    ("alice_memory_review", {"status": "active"}),
    ("alice_recall", {"query": "Synthetic fact"}),
    ("alice_context_pack", {"query": "Synthetic fact"}),
    ("alice_resume", {}),
    ("alice_recent_decisions", {}),
    ("alice_open_loops", {"action": "list"}),
)


def build_world(harness) -> None:
    with harness.store() as store:
        for name, scope in SCOPES.items():
            row = store.create_memory(
                {
                    "memory_key": f"synthetic.{name}",
                    "canonical_text": TEXTS[name],
                    "title": f"Fact {name}",
                    "memory_type": "decision",
                    "status": "active",
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"project_scope": scope},
                }
            )
            store.create_open_loop(
                {"title": f"Loop {name}", "memory_id": str(row["id"]), "domain": "project", "sensitivity": "public", "metadata_json": {"project_scope": scope}}
            )
            store.create_source(
                {
                    "source_type": "note",
                    "title": f"Source {name}",
                    "content_hash": str(uuid4()),
                    "domain": "project",
                    "sensitivity": "public",
                    "metadata_json": {"project_scope": scope, "raw_text": TEXTS[name]},
                }
            )


def stored_key(harness, profile: str, binding: str | None) -> str:
    """A key written to the row with no check, as any version before this change could write it."""

    raw = mint_agent_key()
    with harness.store() as store:
        store.create_agent_api_key(
            {
                "user_id": str(harness.user_id),
                "agent_id": f"{profile}-{binding}-{uuid4().hex[:6]}",
                "permission_profile": profile,
                "project_scope": binding,
                "key_hash": hash_agent_key(raw),
                "key_prefix": agent_key_prefix(raw),
                "label": None,
            }
        )
    return raw


def read_mcp(harness, monkeypatch, key: str | None, tool: str, arguments: dict) -> str:
    if key is None:
        monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    context = MCPRuntimeContext(database_url=harness.urls["app"], user_id=harness.user_id)
    return json.dumps(call_mcp_tool(context, name=tool, arguments=arguments), default=str)


def counts(harness) -> dict[str, int]:
    with harness.store() as store, store.conn.cursor() as cur:
        found = {}
        for table in ("memories", "sources", "open_loops"):
            cur.execute(f"SELECT count(*) AS n FROM {table}")  # closed table names
            found[table] = cur.fetchone()["n"]
        return found


@pytest.mark.parametrize("scoping", ["off", "on"])
@pytest.mark.parametrize("profile", ["project_scoped_agent", "trusted_local_agent", "admin_agent", "read_only_agent"])
@pytest.mark.parametrize(("tool", "arguments"), READ_TOOLS, ids=[f"{tool}-{index}" for index, (tool, _a) in enumerate(READ_TOOLS)])
def test_a_key_stored_with_the_reserved_name_reads_nothing_through_any_tool(
    label_harness, monkeypatch, scoping, profile, tool, arguments  # noqa: F811
):
    """Mutation: remove the stored-binding check in ``resolve_agent_identity``.

    On the base ``alice_memory_review`` with ``status: all`` returned the text of the rows of two unrelated free-form
    projects to this key, and none of the other tools read them only because the store raised an error.
    """

    monkeypatch.setenv("ALICE_PROJECT_SCOPING", scoping)
    build_world(label_harness)
    key = stored_key(label_harness, profile, "~global")
    before = counts(label_harness)
    with pytest.raises(MCPNotPermittedError) as caught:
        read_mcp(label_harness, monkeypatch, key, tool, arguments)
    assert "bound to the project name ~global" in str(caught.value)
    assert not [text for text in TEXTS.values() if text in str(caught.value)]
    assert counts(label_harness) == before


def test_a_key_stored_with_the_reserved_name_is_refused_at_every_route(label_harness):
    """Mutation: remove the stored-binding check in ``resolve_agent_identity``.

    Every ``/v0/vnext`` route, from the table the sweep of the operator routes keeps (one probe for each route of the
    mounted application), asked with this key: each is refused with HTTP 403 before its handler runs, none answers
    a sentinel of the vault, and no hidden row changes. The one route that takes a one-time capability in place of
    a key is the only exception, and it takes no binding.
    """

    vault = Vault(label_harness, "m").build()
    key = stored_key(label_harness, "project_scoped_agent", "~global")
    admin_key = vault.keys["admin"]
    before = vault.hidden_rows()
    exempt = {("POST", "/v0/vnext/connectors/browser-clipper/capture")}
    refused = 0
    for route in sorted(PROBES):
        method, template = route
        for call in PROBES[route](vault):
            status, text = run_call(vault, method, template, call, key)
            if route in exempt:
                continue
            assert status == 403, (route, call.name, status, text[:200])
            assert carried(vault, text) == [], (route, call.name)
            refused += 1
    assert refused >= len(PROBES) - len(exempt)
    assert vault.hidden_rows() == before
    # The control: the same table answers the unbound admin key, so a refusal above is not a route that answers nobody.
    status, text = run_call(vault, "GET", "/v0/vnext/workspace", next(iter(PROBES[("GET", "/v0/vnext/workspace")](vault))), admin_key)
    assert status == 200


def test_a_key_bound_to_an_ordinary_project_reads_only_that_project(label_harness, monkeypatch):
    build_world(label_harness)
    key = stored_key(label_harness, "project_scoped_agent", ORCHARD)
    for tool, arguments in READ_TOOLS:
        text = read_mcp(label_harness, monkeypatch, key, tool, arguments)
        for name in ("lighthouse", "global", "alice"):
            assert TEXTS[name] not in text and f"Fact {name}" not in text, (tool, name)
    review = read_mcp(label_harness, monkeypatch, key, "alice_memory_review", {"status": "all"})
    assert "Fact orchard" in review
    status, body, _headers = label_harness.request("POST", "/v0/vnext/context-packs", payload={"query": "Synthetic fact"}, key=key)
    assert status == 201 and TEXTS["orchard"] in json.dumps(body)
    assert not [TEXTS[name] for name in ("lighthouse", "global", "alice") if TEXTS[name] in json.dumps(body)]


def test_a_key_made_through_the_product_for_a_project_reads_only_that_project(label_harness, monkeypatch):
    build_world(label_harness)
    key = label_harness.key("project_scoped_agent", project=ALICE_PROJECT)
    review = read_mcp(label_harness, monkeypatch, key, "alice_memory_review", {"status": "all"})
    assert "Fact alice" in review
    for name in ("orchard", "lighthouse", "global"):
        assert f"Fact {name}" not in review


def test_an_unbound_admin_key_still_reads_every_project(label_harness, monkeypatch):
    build_world(label_harness)
    key = label_harness.key("admin_agent")
    review = read_mcp(label_harness, monkeypatch, key, "alice_memory_review", {"status": "all"})
    for name in SCOPES:
        assert f"Fact {name}" in review, name
    status, body, _headers = label_harness.request("POST", "/v0/vnext/context-packs", payload={"query": "Synthetic fact"}, key=key)
    assert status == 201 and TEXTS["lighthouse"] in json.dumps(body)


def test_the_owner_still_reads_every_project(label_harness, monkeypatch):
    build_world(label_harness)
    review = read_mcp(label_harness, monkeypatch, None, "alice_memory_review", {"status": "all"})
    for name in SCOPES:
        assert f"Fact {name}" in review, name
    status, body, _headers = label_harness.request("POST", "/v0/vnext/context-packs", payload={"query": "Synthetic fact"})
    assert status == 201 and TEXTS["lighthouse"] in json.dumps(body)


@pytest.mark.parametrize("spelling", ["~global", "~GLOBAL", " ~Global "])
def test_a_declared_scope_with_the_reserved_name_is_refused_over_http(label_harness, spelling):
    """Mutation: remove the check from the declared-scope reader, or from the claim reader of a keyed call."""

    build_world(label_harness)
    identity = {"agent_id": "declared", "permission_profile": "project_scoped_agent", "project_scope": [spelling]}
    # No key exists: the identity is the payload's own, and its scope may not hold the name.
    status, body, _headers = label_harness.request(
        "POST", "/v0/vnext/context-packs", payload={"query": "Synthetic fact", "agent_identity": identity}
    )
    assert status == 400, body
    assert not [text for text in TEXTS.values() if text in json.dumps(body)]
    ordinary = {**identity, "project_scope": [ORCHARD]}
    status, body, _headers = label_harness.request(
        "POST", "/v0/vnext/context-packs", payload={"query": "Synthetic fact", "agent_identity": ordinary}
    )
    assert status == 201, body
    # Keys exist now: an unbound key and a bound key may not claim the name either.
    unbound = label_harness.key("trusted_local_agent")
    bound = label_harness.key("project_scoped_agent", project=ORCHARD)
    for key in (unbound, bound):
        for expected, payload in (
            (403, {"query": "Synthetic fact", "agent_identity": {"project_scope": [spelling]}}),
            (403, {"query": "Synthetic fact", "project_scope": [spelling]}),
            (400, {"query": "Synthetic fact", "agent_identity": {"agent_id": "declared", "project_scope": [spelling]}}),
        ):
            status, body, _headers = label_harness.request("POST", "/v0/vnext/context-packs", payload=payload, key=key)
            assert status == expected, (status, body)
            assert not [text for text in TEXTS.values() if text in json.dumps(body)]
