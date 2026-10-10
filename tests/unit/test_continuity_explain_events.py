"""The policy event of an explain of a continuity object, who leaves one, and who is shown it.

``_authorize_explain_resource`` records a ``policy.decision`` event with the target type ``continuity_object`` and the id of
the object. Three things are pinned here, from the code on this tree:

* no identity and no caller that only declares a permission profile reaches the function, so they leave no event; a
  key-bound caller is stopped before anything is recorded when the policy would not allow the object, so a refused call
  leaves what a call on a missing id leaves; and a key-bound caller that the policy allows leaves one event, so the id an
  event names is always one its writer may read;
* the labels of a continuity object (domain, sensitivity and projects) sit in its provenance and body, in the legacy store,
  which the label guard does not read, so a reader whose limits are narrower than its writer's cannot be shown to be allowed
  to read the object. An event about one is therefore not shown to a caller with limits, in the list and in the count;
* the owner and an unbound admin key are not limited and see every event, in the context tree as well: the tree filters by
  a selection of sensitivities that the owner makes (its default one leaves out the confidential levels), and the selection
  is not a limit, so it never hides this event from a caller that has none. The tree is told by each of its three callers
  (the route, the tool and the command line) whether the caller has limits.

The explain of a continuity object is available on PostgreSQL only, and its object store is a stub here (one lookup
method). The vNext store, the policy engine, the event writer and the guard are the real code, on a SQLite vault; the real
store and the telemetry routes are covered in ``tests/integration/test_event_references_postgres.py``.

A refused explain leaves nothing for two reasons: the key-bound probe stops it before the policy event is written, and the
refusal is raised inside the transaction that would have written it, so the transaction rolls back. Either alone keeps the
event out, which is why the test below holds on when one is removed.

Mutations (``scripts/derived_label_mutations.json``): stop the guard from withholding an event about a continuity object (the
limited readers are shown the id and the labels of an object they cannot read); let a caller that only declares a profile
reach the recording (it leaves an event naming the object); read a selection as the limits of a caller in the guard of the
tree (the owner loses the event), or the limits of a caller as a selection (a key is shown the event); let the request of the
tree default to no limits; and, for each of the three callers of the tree, tell it every caller is limited or none is.
"""
from __future__ import annotations

import argparse
import ast
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from alicebot_api.cli import context as cli_context
from alicebot_api.mcp import context as mcp_context
from alicebot_api.mcp import evidence_artifacts
from alicebot_api.mcp.types import MCPRuntimeContext, MCPToolError
from alicebot_api.onramp import bootstrap_database, sqlite_url_for_path
from alicebot_api.routers import vnext_retrieval as tree_route
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY, AgentIdentity
from alicebot_api.vnext_context_tree import ContextTreeRequest, VNextContextTreeService
from alicebot_api.vnext_label_guard import LabelGuard, label_read_scope

USER = UUID("11111111-1111-4111-8111-111111111111")
CEILING = ("public", "internal", "private", "unknown")
# What each object's provenance says about it, in the keys the explain door reads.
OBJECTS = {
    "default": {"provenance": {}, "body": {}},
    "confidential": {"provenance": {"sensitivity": "confidential", "domain": "project"}, "body": {}},
    "alpha": {"provenance": {"sensitivity": "public", "domain": "project", "project_scope": ["alpha"]}, "body": {}},
}
KEYS = {
    "admin": dict(agent_id="k-admin", permission_profile="admin_agent"),
    "trusted": dict(agent_id="k-trusted", permission_profile="trusted_local_agent"),
    "read_only": dict(agent_id="k-read-only", permission_profile="read_only_agent"),
    "trusted_bound": dict(agent_id="k-bound", permission_profile="trusted_local_agent", project_scope=("alpha",), project_scope_locked=True),
}
# The objects each key's policy allows the explain of: the confidential one only to the admin key, and the one that names a
# project not to a key bound to another, and an object with no project not to a key bound to one.
ALLOWED = {
    "admin": {"default", "confidential", "alpha"},
    "trusted": {"default", "alpha"},
    "read_only": {"default", "alpha"},
    "trusted_bound": {"alpha"},
}
DECLARED = {
    "declared admin": dict(agent_id="d-admin", permission_profile="admin_agent"),
    "declared trusted": dict(agent_id="d-trusted", permission_profile="trusted_local_agent"),
    "declared read_only": dict(agent_id="d-read-only", permission_profile="read_only_agent"),
}


class TreeStore(SQLiteVNextStore):
    """The SQLite store with the two readers of the context tree that only PostgreSQL has."""

    def list_projects(self, **_kwargs):
        return []

    def list_artifacts(self, **_kwargs):
        return []


class World:
    def __init__(self, path, ids):
        self.path = path
        self.ids = ids
        self.context = MCPRuntimeContext(database_url=sqlite_url_for_path(path), user_id=USER)

    def store(self):
        return sqlite_user_connection(self.path, str(USER))

    def counts(self):
        with self.store() as conn:
            return {table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] for table in ("event_log", "agent_identities")}  # closed names

    def events(self):
        with self.store() as conn:
            return [row for row in SQLiteVNextStore(conn, str(USER)).list_events() if row.get("target_type") == "continuity_object"]

    def tree_store(self, *_ignored):
        """Stands in for the store of the tool and of the command line, which open PostgreSQL."""
        return self._open_tree_store()

    @contextmanager
    def _open_tree_store(self):
        with self.store() as conn:
            yield TreeStore(conn, str(USER))

    def tree(self, **request):
        with self._open_tree_store() as store, label_read_scope(store):
            return VNextContextTreeService(store).build_tree(ContextTreeRequest(limit=50, **request))

    def call_as(self, who):
        """The context and the extra arguments of a call by the owner (None), a key-bound caller or a declared profile."""
        if who is None:
            return self.context, {}
        if who in KEYS:
            return replace(self.context, agent_identity=AgentIdentity(auth="agent_api_key", **KEYS[who]), agent_identity_resolved=True), {}
        return self.context, {"agent_identity": dict(DECLARED[who])}

    def explain(self, who, object_id):
        context, arguments = self.call_as(who)
        try:
            evidence_artifacts._handle_alice_explain(context, {"continuity_object_id": str(object_id), **arguments})
        except MCPToolError:
            return False
        return True


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = tmp_path / "explain.db"
    bootstrap_database(path, user_id=str(USER), user_email="synthetic@example.invalid")
    ids = {name: uuid4() for name in OBJECTS}
    rows = {ids[name]: {"id": ids[name], **value} for name, value in OBJECTS.items()}

    class Objects:
        def get_continuity_object_optional(self, object_id):
            return rows.get(object_id)

    @contextmanager
    def objects(_context):
        yield Objects()

    monkeypatch.setattr(evidence_artifacts, "_store_context", objects)
    monkeypatch.setattr(evidence_artifacts, "build_continuity_explain", lambda *_a, **_k: {"explain": "stub"})
    # The handler refuses a SQLite context before it reads an identity. Only that check is lifted, so the stubs are reached.
    monkeypatch.setattr(evidence_artifacts, "_is_sqlite_backend", lambda _context: False)
    return World(path, ids)


def test_no_identity_and_no_declared_profile_leaves_an_event_naming_a_continuity_object(world):
    for who in (None, *DECLARED):
        for name, object_id in world.ids.items():
            before = world.counts()
            assert world.explain(who, object_id) is True, (who, name)
            assert world.counts() == before, (who, name)
    assert world.events() == []


@pytest.mark.parametrize("key", list(KEYS))
def test_a_key_bound_caller_leaves_one_event_for_an_object_its_policy_allows_and_none_for_the_rest(world, key):
    missing = uuid4()
    before = world.counts()
    assert world.explain(key, missing) is False
    missing_change = {table: world.counts()[table] - before[table] for table in before}
    assert missing_change == {"event_log": 0, "agent_identities": 0}
    for name, object_id in world.ids.items():
        before = world.counts()
        answered = world.explain(key, object_id)
        assert answered is (name in ALLOWED[key]), (key, name)
        after = world.counts()
        if answered:
            # The agent record and the policy event of the call are the two rows a first allowed call adds.
            assert after["event_log"] > before["event_log"], (key, name)
            assert [row["target_id"] for row in world.events()].count(str(object_id)) == 1, (key, name)
            assert [row["event_type"] for row in world.events() if row["target_id"] == str(object_id)] == ["policy.decision"]
        else:
            # A refused call leaves exactly what a call on an id that is not stored leaves: nothing.
            assert {table: after[table] - before[table] for table in before} == missing_change, (key, name)
    named = {row["target_id"] for row in world.events()}
    assert named == {str(world.ids[name]) for name in ALLOWED[key]}


def test_every_event_a_key_leaves_names_an_object_it_may_read(world):
    for key in KEYS:
        for object_id in world.ids.values():
            world.explain(key, object_id)
    with world.store() as conn:
        events = [row for row in SQLiteVNextStore(conn, str(USER)).list_events() if row.get("target_type") == "continuity_object"]
    by_id = {str(object_id): name for name, object_id in world.ids.items()}
    assert events
    for event in events:
        writer = next(key for key, spec in KEYS.items() if spec["agent_id"] == event["actor_id"])
        assert by_id[event["target_id"]] in ALLOWED[writer], (writer, by_id[event["target_id"]])


def test_a_caller_with_limits_is_not_shown_an_event_about_a_continuity_object_and_the_count_agrees(world):
    for key in KEYS:
        for object_id in world.ids.values():
            world.explain(key, object_id)
    written = world.events()
    assert len(written) == sum(len(allowed) for allowed in ALLOWED.values())
    with world.store() as conn:
        store = SQLiteVNextStore(conn, str(USER))
        everything = store.list_events()
        stored_continuity = [row for row in everything if row.get("target_type") == "continuity_object"]
        assert len(stored_continuity) == len(written)
        with label_read_scope(store):
            unlimited = LabelGuard(store, active=False)
            assert [row["id"] for row in unlimited.admit_events(everything)] == [row["id"] for row in everything]
            for options in (
                {"sensitivity_allowed": CEILING},
                {"sensitivity_allowed": CEILING, "domains": ("project",)},
                {"sensitivity_allowed": CEILING, "projects": ("alpha",), "all_of": ("alpha",)},
                {"sensitivity_allowed": CEILING, "projects": ("beta",), "all_of": ("beta",)},
                {"sensitivity_allowed": ("public",)},
            ):
                guard = LabelGuard(store, active=True, **options)
                shown = guard.admit_events(everything)
                assert not [row for row in shown if row.get("target_type") == "continuity_object"], options
                # The other events of the vault stay, and the count query reaches the same number as the list.
                assert len(shown) == len(everything) - len(written), options
                assert guard.readable_event_count() == len(shown), options


def _tree_events(tree):
    root = next(root for root in tree["roots"] if root["id"] == "root:events")
    return {child["ref"] for child in root["children"]}


def _continuity_refs(world):
    return {f"event:{row['id']}" for row in world.events()}


def _explain_every_object_as_admin(world):
    for object_id in world.ids.values():
        assert world.explain("admin", object_id) is True
    refs = _continuity_refs(world)
    assert len(refs) == len(OBJECTS)
    return refs


def test_the_context_tree_keeps_the_event_of_a_continuity_object_for_a_caller_without_limits_whatever_it_selects(world):
    """The sensitivities of the tree are a selection. The owner's default one leaves out the confidential levels and
    the event of an object is not labelled by them, so the selection never decides this event. Only the limits of the
    caller do."""
    refs = _explain_every_object_as_admin(world)
    with world.store() as conn:
        others = {f"event:{row['id']}" for row in SQLiteVNextStore(conn, str(USER)).list_events(limit=50) if row.get("target_type") != "continuity_object"}
    assert others
    for selection in (CEILING, ("public",), tuple(ALL_SENSITIVITY)):
        unlimited = _tree_events(world.tree(sensitivity_allowed=selection, caller_limited=False))
        assert refs <= unlimited and others <= unlimited, selection
        limited = _tree_events(world.tree(sensitivity_allowed=selection, caller_limited=True))
        assert not refs & limited, selection
        # The events that name no object it cannot read stay for the caller with limits.
        assert others <= limited, selection
    # A request that does not say is read as the request of a caller with limits.
    assert not refs & _tree_events(world.tree())
    assert not refs & _tree_events(world.tree(sensitivity_allowed=CEILING))


@pytest.mark.parametrize(
    ("who", "limited"),
    [
        (None, False),
        ("admin", False),
        ("declared admin", False),
        ("trusted", True),
        ("read_only", True),
        ("trusted_bound", True),
        ("declared trusted", True),
        ("declared read_only", True),
    ],
)
def test_the_context_tree_tool_tells_the_tree_whether_the_caller_has_limits(world, monkeypatch, who, limited):
    refs = _explain_every_object_as_admin(world)
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setattr(mcp_context, "_vnext_store_context", world.tree_store)
    context, arguments = world.call_as(who)
    tree = mcp_context._handle_alice_vnext_context_tree(context, {"limit": 50, **arguments})
    shown = _tree_events(tree)
    if limited:
        assert not refs & shown, who
    else:
        assert refs <= shown, who
    assert tree["summary"]["events"] == len(shown)


@pytest.mark.parametrize("who", [None, "admin", "trusted", "read_only", "trusted_bound"])
def test_the_context_tree_route_tells_the_tree_whether_the_caller_has_limits(world, monkeypatch, who):
    """GET /v0/vnext/context-tree with the default selection. The owner and an unbound admin key have no limits and an
    unbound trusted key has. The operator gate refuses a read-only key and a key bound to a project."""
    refs = _explain_every_object_as_admin(world)
    identity = None if who is None else AgentIdentity(auth="agent_api_key", **KEYS[who])

    @contextmanager
    def connection(_url, _user_id):
        with world.store() as conn:
            yield conn

    monkeypatch.setattr(tree_route, "get_settings", lambda: SimpleNamespace(database_url="sqlite-test"))
    monkeypatch.setattr(tree_route, "user_connection", connection)
    monkeypatch.setattr(tree_route, "PostgresVNextStore", lambda conn: TreeStore(conn, str(USER)))
    monkeypatch.setattr(tree_route, "resolve_protected_agent_identity", lambda *_args, **_kwargs: identity)
    response = tree_route.get_vnext_context_tree(user_id=USER, query="", domains=None, sensitivity_allowed=None, limit=50, include_events=True, authorization=None)
    if who in {"read_only", "trusted_bound"}:
        assert response.status_code == 403
        return
    assert response.status_code == 200
    shown = _tree_events(json.loads(response.body))
    if who == "trusted":
        assert not refs & shown
    else:
        assert refs <= shown


def test_the_context_tree_command_reads_the_owner_as_a_caller_without_limits(world, monkeypatch):
    refs = _explain_every_object_as_admin(world)
    monkeypatch.setattr(cli_context, "_vnext_store_context", world.tree_store)
    args = argparse.Namespace(query=[], domain=[], sensitivity_allowed=None, limit=50, no_events=False)
    shown = _tree_events(json.loads(cli_context._run_vnext_context_tree(SimpleNamespace(), args)))
    assert refs <= shown


def test_every_request_for_a_context_tree_says_whether_the_caller_has_limits() -> None:
    """The request defaults to a caller with limits, so a caller that forgets to say is not shown more. The three callers in
    the code say it, and a new one has to."""
    root = Path(__file__).resolve().parents[2] / "apps" / "api" / "src"
    found = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "vnext_context_tree.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ContextTreeRequest":
                found.append((path.relative_to(root).as_posix(), "caller_limited" in {keyword.arg for keyword in node.keywords}))
    assert sorted(found) == [
        ("alicebot_api/cli/context.py", True),
        ("alicebot_api/mcp/context.py", True),
        ("alicebot_api/routers/vnext_retrieval.py", True),
    ]
