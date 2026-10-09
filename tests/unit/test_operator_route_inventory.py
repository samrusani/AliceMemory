"""The routes of the application are read at run time, and a route that nobody wrote a probe for fails here.

The operator route sweep (``tests/integration/test_operator_routes_limits_postgres.py``) runs one probe per
``/v0/vnext`` route against a vault with hidden rows. These tests need no database. They require the probe table to
list exactly the routes the mounted application has, so a route that is added, renamed or moved without a probe fails
here before it can reach a caller; they require every other route of the application to belong to a family whose
posture is written down; and they show that the legacy ``/v0`` surface, which no key reaches by design, is closed
outside development.

Mutations, each one alone: rename a path in a router under ``routers/`` (the route is then in the application and not in
the table); delete a decorated route; add ``@memory_router.get("/v0/vnext/memories/new-feed")`` over any handler; move
a route from ``_VNEXT_CENTRAL_OPERATOR_ROUTES`` to nowhere.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest

import alicebot_api.main as main_module
from alicebot_api.config import Settings
from tests.integration.operator_route_probes import PENDING, PROBES
from tests.integration.operator_route_runner import invoke
from tests.integration.operator_route_vault import StubVault, Vault


def _app_routes() -> Iterator[tuple[str, str]]:
    for route in main_module.app.router.routes:
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        for context in effective_route_contexts() if callable(effective_route_contexts) else (route,):
            for method in sorted(getattr(context, "methods", None) or set()):
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    yield method, str(getattr(context, "path", ""))


def _vnext_routes() -> set[tuple[str, str]]:
    return {route for route in _app_routes() if route[1].startswith("/v0/vnext")}


def test_the_probe_table_lists_exactly_the_vnext_routes_of_the_application() -> None:
    routes = _vnext_routes()
    assert routes, "the application mounts no /v0/vnext route"
    assert sorted(set(PROBES) - routes) == [], "a probe for a route the application does not have"
    assert sorted(routes - set(PROBES)) == [], "a route with no probe: add one to operator_route_probes.py"


def test_every_vnext_route_is_classified_by_the_central_gate_and_in_one_class_only() -> None:
    local = main_module._VNEXT_ROUTE_LOCAL_POLICY
    operator = main_module._VNEXT_CENTRAL_OPERATOR_ROUTES
    assert local.isdisjoint(operator)
    assert _vnext_routes() == local | operator


def test_every_pending_entry_is_a_probed_route() -> None:
    assert set(PENDING) <= set(PROBES)
    assert all(reason.strip() for reason in PENDING.values())


def test_every_probe_builds_its_calls_and_each_names_a_sentinel_it_can_find() -> None:
    stub = StubVault()
    for route, build in PROBES.items():
        calls = build(stub)  # type: ignore[arg-type]
        assert calls, route
        assert len({call.name for call in calls}) == len(calls), route
        for call in calls:
            for name in (*call.admin_sees, *call.trusted_sees):
                assert name, (route, call.name)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def test_a_probe_sends_the_text_of_a_hidden_row_only_through_the_rows_made_for_it() -> None:
    """A key that sends a hidden text may be shown it back, and its own copy may be shown later, so the sweep cannot
    tell that from a leak. Only the rows listed in ``Vault.SENDABLE`` may be sent, and the sweep never looks for them.
    """

    stub = StubVault()
    sendable = {stub.text(name) for name in Vault.SENDABLE}
    sent = set()
    for route, build in PROBES.items():
        for call in build(stub):  # type: ignore[arg-type]
            for text in (*_strings(call.body), *_strings(call.query)):
                if text.startswith(("text:", "hidden:", "shown:")):
                    sent.add(text)
                    assert text in sendable, (route, call.name, text)
    assert sent, "no probe sends a deduplication text"


#: Every route that is not under /v0/vnext, by family. A route outside these families fails the test below.
LEGACY_V0_PREFIX = "/v0/"
V1_PREFIX = "/v1/"
PUBLIC_PATHS = frozenset({"/healthz", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"})


def _other_routes() -> set[tuple[str, str]]:
    return {route for route in _app_routes() if not route[1].startswith("/v0/vnext")}


def test_every_route_outside_vnext_belongs_to_a_family_with_a_written_posture() -> None:
    """The families are the three whose posture ``docs/security/auth-authorization.md`` and the security note state.

    The public paths carry no vault data. The retained legacy ``/v0`` routes take no agent key at all and are closed
    outside development and test (the test below). The ``/v1`` routes authenticate a key and authorize nothing.
    A route in a fourth family fails here until its posture is decided and written down.
    """

    unclassified = sorted(
        route
        for route in _other_routes()
        if route[1] not in PUBLIC_PATHS and not route[1].startswith((LEGACY_V0_PREFIX, V1_PREFIX))
    )
    assert unclassified == []


def _legacy_v0_routes() -> list[tuple[str, str]]:
    return sorted(
        route for route in _other_routes() if route[1].startswith(LEGACY_V0_PREFIX) and not route[1].startswith("/v0/vnext")
    )


@pytest.mark.parametrize("key", [None, "alice_sk_" + "a" * 32])
def test_the_legacy_v0_surface_answers_nothing_outside_development(monkeypatch, key) -> None:
    """No caller reaches a retained legacy route in a production environment, with a key or without one.

    A key is not consulted on these routes, so what a key could read there is decided by the environment alone. The
    middleware answers before the route and before any database read, so this runs without a database.
    """

    settings = dataclasses.replace(
        Settings(database_url="postgresql://unused:unused@127.0.0.1:1/unused"), app_env="production"
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    routes = _legacy_v0_routes()
    assert routes
    user_id = "00000000-0000-4000-8000-000000000001"
    for method, path in routes:
        template = path
        for part in [segment for segment in path.split("/") if segment.startswith("{")]:
            template = template.replace(part, "00000000-0000-4000-8000-000000000002")
        status, text = invoke(method, template, user_id=user_id, key=key, payload={"user_id": user_id} if method != "GET" else None)
        assert status == 404, (method, path, status, text[:120])
        assert "legacy v0 API is disabled outside development and test" in text
