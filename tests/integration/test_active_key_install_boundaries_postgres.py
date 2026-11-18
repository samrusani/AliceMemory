"""Check key admission before dispatch with real, isolated vaults."""

from pathlib import Path
import sqlite3
from typing import get_args
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from alicebot_api.mcp import registry
from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.routers._vnext_shared import VNextSensitivity
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_keys import create_agent_key, hash_agent_key
from alicebot_api.vnext_connectors import VNextConnectorService
from tests.integration.derived_labels_postgres_support import label_harness  # noqa: F401
from tests.unit.test_mcp_typed_error_codes import _wire


def _table_counts(url):
    with psycopg.connect(url) as conn:
        tables = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
        return {name: conn.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(name))).fetchone()[0]
                for (name,) in tables}


def _arguments_for(schema):
    arguments = {}
    for name in schema.get("required", []):
        field = schema["properties"][name]
        if "enum" in field:
            arguments[name] = field["enum"][0]
        elif field.get("format") == "uuid":
            arguments[name] = str(uuid4())
        else:
            assert field["type"] == "string", (name, field)
            arguments[name] = "fixture"
    return arguments


def test_active_postgres_key_requires_mcp_server_key_before_every_tool(label_harness, monkeypatch):
    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    key = h.key("admin_agent")
    dispatched = []

    def probe(_, arguments):
        dispatched.append(arguments)
        return {"reached": True}

    tools = registry.list_mcp_tools()
    assert tools
    for tool in tools:
        monkeypatch.setitem(registry._TOOL_HANDLERS, tool["name"], probe)
    with h.store() as store:
        before_key = store.get_agent_api_key_by_hash(hash_agent_key(key))
    before_counts = _table_counts(h.urls["admin"])
    outcomes = {}
    for tool in tools:
        name = tool["name"]
        arguments = _arguments_for(tool["inputSchema"])
        registry._validate_mcp_arguments_against_advertised_schema(name, arguments)
        outcomes[name] = _wire(context, name, arguments).get("error")

    assert outcomes == dict.fromkeys(outcomes, {
        "code": "not_permitted",
        "message": "Set ALICE_AGENT_API_KEY for this server. Mint a PostgreSQL key with 'alicebot agent keys create'.",
    })
    assert not dispatched
    assert _table_counts(h.urls["admin"]) == before_counts
    with h.store() as store:
        assert store.get_agent_api_key_by_hash(hash_agent_key(key)) == before_key
    monkeypatch.setenv("ALICE_AGENT_API_KEY", key)
    assert _wire(context, "alice_recall", {"query": "fixture"})["reached"]
    assert _wire(context, "alice_state_at", {"entity_id": str(uuid4())})["error"]["code"] == "tool_not_found"
    with h.store() as store:
        store.revoke_agent_api_key(key_id=str(before_key["id"]))
    monkeypatch.delenv("ALICE_AGENT_API_KEY")
    assert _wire(context, "alice_recall", {"query": "fixture"})["reached"]
    assert _wire(context, "alice_state_at", {"entity_id": str(uuid4())})["reached"]


def test_keyless_mcp_codes_match_between_vaults(label_harness, monkeypatch, tmp_path: Path):
    postgres = label_harness
    vault = tmp_path / "vault.db"
    user_id = uuid4()
    bootstrap_database(vault, user_id=str(user_id), user_email="fixture@example.invalid")
    contexts = (
        MCPRuntimeContext(database_url=postgres.urls["app"], user_id=postgres.user_id),
        MCPRuntimeContext(database_url=sqlite_url_for_path(vault), user_id=user_id),
    )
    for name in ("alice_recall", "alice_state_at"):
        monkeypatch.setitem(registry._TOOL_HANDLERS, name, lambda *_: {"reached": True})
    arguments = {"query": "fixture"}
    assert [_wire(ctx, "alice_recall", arguments)["reached"] for ctx in contexts] == [True, True]
    postgres.key("read_only_agent")
    with sqlite_user_connection(vault, user_id) as conn:
        create_agent_key(SQLiteVNextStore(conn, user_id), user_id=user_id, agent_id="fixture", permission_profile="read_only_agent")

    before_counts = _table_counts(postgres.urls["admin"])
    with sqlite3.connect(vault) as conn:
        before_sqlite = list(conn.iterdump())
    for name, params, expected in (
        ("alice_recall", arguments, "not_permitted"),
        ("alice_recall", {**arguments, "agent_id": "fixture", "agent_type": "unknown-fixture-type"}, "not_permitted"),
        ("alice_recall", {**arguments, "agent_id": 7}, "invalid_request"),
        ("alice_recall", {**arguments, "agent_identity": 7}, "invalid_request"),
        ("alice_state_at", {"entity_id": str(uuid4())}, "not_permitted"),
    ):
        results = [_wire(ctx, name, params) for ctx in contexts]
        assert [result.get("error", {}).get("code") for result in results] == [expected, expected], results
    assert _table_counts(postgres.urls["admin"]) == before_counts
    with sqlite3.connect(vault) as conn:
        assert list(conn.iterdump()) == before_sqlite


def test_postgres_mcp_refuses_when_active_key_count_is_unavailable(label_harness, monkeypatch):
    from alicebot_api.vnext_store import PostgresVNextStore

    def unavailable(_):
        raise RuntimeError("Fixture count unavailable")

    def unexpected_dispatch(*_):
        pytest.fail("A call with an unavailable key count reached the handler")

    h = label_harness
    context = MCPRuntimeContext(database_url=h.urls["app"], user_id=h.user_id)
    monkeypatch.setattr(PostgresVNextStore, "count_active_agent_api_keys", unavailable)
    monkeypatch.setitem(registry._TOOL_HANDLERS, "alice_recall", unexpected_dispatch)
    result = _wire(context, "alice_recall", {"query": "fixture"})
    assert result["error"]["code"] == "not_permitted"
    assert "alicebot agent keys create" in result["error"]["message"]


@pytest.mark.parametrize("issuer", ("trusted_local_agent", "admin_agent", "keyless", "keyless_before_first_key"))
def test_browser_capability_uses_the_strictest_issuer_ceiling(label_harness, monkeypatch, issuer):
    h = label_harness
    levels = get_args(VNextSensitivity)
    assert levels
    base = "/v0/vnext/connectors/browser-clipper"
    origin = "https://example.invalid"
    if issuer in {"trusted_local_agent", "admin_agent"}:
        issuing_key = h.key(issuer)
    else:
        issuing_key = None
    issued = {}
    for level in levels:
        status, response, _ = h.request("POST", f"{base}/capabilities", payload={"origin": origin}, key=issuing_key)
        assert status == 201, response
        issued[level] = response["capability"]

    denied = set()
    if issuer != "keyless":
        trusted = issuing_key if issuer == "trusted_local_agent" else h.key("trusted_local_agent")
        for level in levels:
            payload = {"url": f"{origin}/keyed-{level}", "page_text": f"Keyed fixture for {level}.", "sensitivity": level}
            status, response, _ = h.request("POST", f"{base}/capture", payload=payload, key=trusted)
            assert status in {201, 403}, response
            if status == 403:
                assert "sensitivity_above_agent_ceiling" in response["policy_decision"]["reasons"]
                denied.add(level)
        assert denied
        with h.store() as store:
            VNextConnectorService(store).update_config(
                "browser_clipper", default_sensitivity=next(level for level in levels if level in denied),
            )

    original_capture = VNextConnectorService.capture_browser_clip

    def guard_capture(service, payload, **kwargs):
        if kwargs["capability_authorized"] and kwargs["default_sensitivity"] in denied:
            pytest.fail("The capability ceiling allowed dispatch to the capture service")
        return original_capture(service, payload, **kwargs)

    monkeypatch.setattr(VNextConnectorService, "capture_browser_clip", guard_capture)
    for level in levels:
        payload = {"url": f"{origin}/cap-{level}", "page_text": f"Capability fixture for {level}.",
                   "sensitivity": level, "capture_capability": issued[level]}
        before = _table_counts(h.urls["admin"])
        status, response, _ = h.request("POST", f"{base}/capture", payload=payload, origin=origin)
        if level in denied:
            assert (status, response) == (403, {"detail": {"code": "sensitivity_above_agent_ceiling",
                                          "message": "The requested sensitivity exceeds the browser capture ceiling"}})
            assert _table_counts(h.urls["admin"]) == before
            payload.pop("sensitivity")  # The model's default is private.
            status, response, _ = h.request("POST", f"{base}/capture", payload=payload, origin=origin)
        assert status == 201, response
        assert _table_counts(h.urls["admin"])["sources"] == before["sources"] + 1
        with h.store() as store:
            row = store.conn.execute("SELECT sensitivity FROM sources WHERE uri = %s", (payload["url"],)).fetchone()
        assert row["sensitivity"] == ("private" if level in denied else level)
