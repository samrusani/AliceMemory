"""Two facts about a redacted memory that the pages state: a loop its caller wrote stays readable, and what the owner reads.

Unreleased (on main, not in v0.20.0). Redacting a memory contains the reports and derived rows built from it (the owner and an
unbound admin key read them; every other key is refused them). Two cases at the edge of that rule are pinned here, on a real
SQLite vault with real keys, because the security note, the known limitations page and the tool reference say them in words:

* An open loop made with ``POST /v0/vnext/open-loops`` over a memory, with the memory's words in the title and description its
  caller typed, stays readable after the memory is redacted. The title is text the caller wrote, not a copy the system made, so
  it is not contained; the ``memory_id`` column is an id and reads ``null`` once the memory is redacted. A loop that a producer
  made from the memory (it records the memory as an input) is a copy and is contained with the reports.
* After the memory is redacted the owner and an unbound admin key read a contained row by id, but not through recall or the
  context pack, because the default sensitivity filter of those two reads an unverified row as regulated. A request that names
  every sensitivity lists them: the ``sensitivity_allowed`` argument of ``alice_recall`` and of ``alice_context_pack`` both do it.
  (The artifact and project lists, the recent commits list, the dashboard and the context tree are PostgreSQL routes and are
  checked in ``tests/integration/test_saved_quote_memory_refs_postgres.py``.)

Each test names the mutation that must fail it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import UUID

import pytest

from alicebot_api.config import Settings
from alicebot_api.vnext_agent_control import ALL_SENSITIVITY
from alicebot_api.vnext_derived_labels import with_derived_from
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.unit.test_redacted_input_containment_sqlite import ALPHA, RESTRICTED, Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    return Vault(tmp_path, monkeypatch)


@contextmanager
def _loop_route(vault: Vault):
    """The handler of ``POST /v0/vnext/open-loops``, pointed at the vault's SQLite store (it is a PostgreSQL route)."""

    from alicebot_api.routers import vnext_projects

    path = vault.path

    @contextmanager
    def connection(_database_url, current_user_id):  # type: ignore[no-untyped-def]
        with sqlite_user_connection(path, str(current_user_id)) as conn:
            yield conn

    with vault.monkeypatch.context() as patch:
        patch.setattr(vnext_projects, "get_settings", lambda: Settings(database_url="postgresql://db"))
        patch.setattr(vnext_projects, "user_connection", connection)
        patch.setattr(vnext_projects, "PostgresVNextStore", lambda conn: SQLiteVNextStore(conn, vault.user))
        yield vnext_projects


def _create_loop(vault: Vault, *, title: str, description: str) -> str:
    with _loop_route(vault) as module:
        response = module.create_vnext_open_loop(
            module.VNextOpenLoopCreateRequest(
                user_id=UUID(str(vault.user)),
                title=title,
                description=description,
                memory_id=vault.redacted,
                domain="project",
                sensitivity="public",
                project_scope=[ALPHA],
                agent_id="admin",
            ),
            authorization=f"Bearer {vault.keys['admin']}",
        )
    body = json.loads(response.body)
    assert response.status_code == 201, body
    return str(body["open_loop"]["id"])


def _loops(vault: Vault, key: str | None) -> list[dict[str, object]]:
    answer = vault.call(key, "alice_open_loops", {"action": "list"})
    return list(answer.get("loops") or answer.get("items") or answer.get("open_loops") or [])  # type: ignore[union-attr]


def test_a_loop_its_caller_wrote_over_a_memory_stays_readable_after_the_memory_is_redacted(vault: Vault) -> None:
    """The title and description of a loop made over the memory hold the words the caller typed. After the memory is redacted
    every key that reads the loop still reads them (they are the caller's text, so the redaction does not reach them), and the
    ``memory_id`` column reads ``null`` for every reader, the owner included, as the column of a loop that points at a deleted
    memory always did.

    A loop that records the memory as an input (what a producer makes) is a copy of it: it is contained, and a key with limits
    is not shown it. (The loop list reads an unverified row as regulated, so the owner's default list leaves it out as well.)

    Mutation: mark the loop a derived row on creation (``POST /v0/vnext/open-loops`` stores ``derived_from`` for ``memory_id``):
    the first assertion fails for every restricted key.
    """

    words = f"Atlas played {vault.sentinel} for 115 hours"
    loop_id = _create_loop(vault, title=f"Follow up: {words}", description=f"Check {words}")
    with sqlite_user_connection(vault.path, vault.user) as conn:
        store = SQLiteVNextStore(conn, vault.user)
        copy = store.create_open_loop(
            {
                "title": f"Copied: {words}", "description": "made by a producer", "status": "open", "domain": "project",
                "sensitivity": "public", "memory_id": vault.redacted,
                "metadata_json": with_derived_from({"project_scope": [ALPHA]}, {"memories": [vault.members[3]]}),
            }
        )
    copy_id = str(copy["id"])
    before = {name: _loops(vault, key) for name, key in vault.keys.items()}
    assert any(vault.sentinel in json.dumps(rows, default=str) for rows in before.values()), "the control reads the words"

    assert vault.redact()["status"] == "redacted"

    for name in ("trusted", "trusted_bound", "admin", "admin_bound"):
        rows = {str(row["id"]): row for row in _loops(vault, vault.keys[name])}
        assert loop_id in rows, name
        assert words in json.dumps(rows[loop_id], default=str), name
    owner_rows = {str(row["id"]): row for row in _loops(vault, None)}
    assert loop_id in owner_rows
    for name in RESTRICTED:
        shown = {str(row["id"]): row for row in _loops(vault, vault.keys[name])}
        assert copy_id not in shown, name
        if loop_id in shown:
            assert shown[loop_id].get("memory_id") in (None, ""), name
    assert owner_rows[loop_id].get("memory_id") is None, "a loop that points at a redacted memory shows no id to anyone"


def test_the_owner_and_an_unbound_admin_key_read_contained_rows_by_id_but_not_through_recall_or_the_pack(vault: Vault) -> None:
    """After the memory is redacted, the rows built from it are read by id by the owner and an unbound admin key
    (``alice_explain`` and ``alice_memory_review`` detail), and are listed by neither recall nor the context pack, because the
    default sensitivity filter of those reads an unverified row as regulated. A request that names every sensitivity lists them,
    in recall (the ``sensitivity_allowed`` argument of ``alice_recall``) as in the pack.

    Mutation: let ``is_unverified`` rows through the default filter (read an unverified row as its stored sensitivity in
    ``LabelGuard.effective_row``): the pack and recall list them by default. Make the ``sensitivity_allowed`` argument of
    ``alice_recall`` count for nothing: the second assertion of each loop fails.
    """

    vault.redact()
    queries = ("Atlas played hours", "Project state", "Merge Atlas", "Again")
    for key in (None, vault.keys["admin"]):
        for row_id in vault.contained:
            assert vault.sentinel in json.dumps(vault.call(key, "alice_explain", {"memory_id": row_id}), default=str)
            detail = vault.call(key, "alice_memory_review", {"review_item_id": row_id})
            assert vault.sentinel in json.dumps(detail, default=str)
        for query in queries:
            recalled = json.dumps(vault.call(key, "alice_recall", {"query": query, "limit": 50}), default=str)
            packed = json.dumps(vault.call(key, "alice_context_pack", {"query": query}), default=str)
            assert not [row_id for row_id in vault.contained if row_id in recalled], (key is None, "recall", query)
            assert not [row_id for row_id in vault.contained if row_id in packed], (key is None, "pack", query)
        listed = set()
        recalled_when_named = set()
        for query in queries:
            named = json.dumps(
                vault.call(key, "alice_context_pack", {"query": query, "sensitivity_allowed": list(ALL_SENSITIVITY)}),
                default=str,
            )
            listed |= {row_id for row_id in vault.contained if row_id in named}
            recalled = json.dumps(
                vault.call(key, "alice_recall", {"query": query, "limit": 50, "sensitivity_allowed": list(ALL_SENSITIVITY)}),
                default=str,
            )
            recalled_when_named |= {row_id for row_id in vault.contained if row_id in recalled}
        assert listed, (key is None, "a pack that names every sensitivity lists contained rows")
        assert recalled_when_named, (key is None, "recall that names every sensitivity lists contained rows")
