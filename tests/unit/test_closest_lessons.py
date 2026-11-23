"""Closest lessons on the SQLite MCP server."""

from contextlib import contextmanager

import pytest

from alicebot_api.mcp.types import MCPRuntimeContext
from alicebot_api.onramp import sqlite_url_for_path
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from tests.support.closest_lessons_contract import ClosestLessonsContract, make_vault
from tests.unit.test_lessons_read import Vault


@pytest.fixture(params=[False, True], ids=["words-and-paths", "vectors"])
def vectors_on(request):
    return request.param


def sqlite_closest_vault(tmp_path, monkeypatch, *, vector):
    vault = Vault(tmp_path / "vault.db")

    @contextmanager
    def store():
        with sqlite_user_connection(vault.path, vault.user) as conn:
            yield SQLiteVNextStore(conn, vault.user)

    context = MCPRuntimeContext(database_url=sqlite_url_for_path(vault.path), user_id=vault.user)
    return make_vault(context, store, monkeypatch, tmp_path, vector=vector)


@pytest.fixture
def closest_vault(tmp_path, monkeypatch, vectors_on):
    return sqlite_closest_vault(tmp_path, monkeypatch, vector=vectors_on)


class TestSQLiteClosestLessons(ClosestLessonsContract):
    pass
