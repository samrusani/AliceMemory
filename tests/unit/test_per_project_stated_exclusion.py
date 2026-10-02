"""Per-project memory S2: a read that asks for global rows must say what it holds back.

A request tuple may hold the reserved marker, which asks for the notes that belong to no project, and the
store can leave out global notes in some domains before the limit (``exclude_global_domains``). A reader
that defaulted that argument to "leave nothing out" would hold nothing back for any caller that forgot it, and
nothing would fail. So the argument defaults to ``None``, meaning "not stated", and a reader that is handed the
marker without a statement raises. The two single-scan partition readers go further and have no default for the
domain filter and the sensitivity ceiling either, because a caller that forgot one would read every domain or
every sensitivity (the team rule for a new read path: each control is a required argument).

Each test names the edit that makes it fail.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_project_scope import GLOBAL_PROJECT_MARKER
from tests.unit.per_project_s2_support import PROJECT_A, add_loop, add_memory, context_for, db_path_for
from tests.unit.per_project_view_support import USER_ID

GLOBAL = GLOBAL_PROJECT_MARKER
CEILING = ["public", "internal", "private", "unknown"]


@pytest.fixture
def store(tmp_path: Path):
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        found = SQLiteVNextStore(connection, USER_ID)
        add_memory(found, key="note.global", text="A global note")
        add_memory(found, key="note.health", text="A global health note", domain="health", sensitivity="private")
        add_memory(found, key="note.project", text="A project note", scope=(PROJECT_A,))
        add_loop(found, title="A global loop")
        add_loop(found, title="A global health loop", domain="health", sensitivity="private")
        add_loop(found, title="A project loop", scope=(PROJECT_A,))
        yield found


Reader = Callable[..., object]

#: Each reader that can be handed a request tuple holding the marker, called with that tuple.
READERS: dict[str, Callable[[SQLiteVNextStore, tuple[str, ...]], Reader]] = {
    "list_memories": lambda store, projects: lambda **kw: store.list_memories(projects=projects, limit=5, **kw),
    "list_open_loops": lambda store, projects: lambda **kw: store.list_open_loops(
        status=None, scope_projects=projects, limit=5, **kw
    ),
    "list_open_loop_events": lambda store, projects: lambda **kw: store.list_open_loop_events(
        statuses=("open",), scope_projects=projects, limit=5, **kw
    ),
    "list_resume_memory_events": lambda store, projects: lambda **kw: store.list_resume_memory_events(
        statuses=("active",), projects=projects, limit=5, **kw
    ),
}


@pytest.mark.parametrize("name", sorted(READERS))
@pytest.mark.parametrize("request_tuple", [(PROJECT_A, GLOBAL), (GLOBAL,), (GLOBAL.upper(), PROJECT_A)])
def test_a_reader_handed_the_marker_with_no_stated_exclusion_raises(
    store: SQLiteVNextStore, name: str, request_tuple: tuple[str, ...]
) -> None:
    """Mutation: give ``exclude_global_domains`` the default ``()`` again in that reader, or delete the raise for an
    unstated exclusion in ``_project_view_sql``.

    The marker asks for global rows, and global rows in the held-back domains are a decision the caller makes. With
    no statement the reader refuses. Saying nothing is held back (an empty tuple) and saying which domains are
    both fine.
    """

    read = READERS[name](store, request_tuple)
    with pytest.raises(ValueError, match="must state which global domains"):
        read()
    read(exclude_global_domains=())
    read(exclude_global_domains=("health", "family"))


@pytest.mark.parametrize("name", sorted(READERS))
def test_a_request_without_the_marker_needs_no_statement(store: SQLiteVNextStore, name: str) -> None:
    """Mutation: raise for every call that does not state the exclusion, or for a request with no project at all.

    The statement is about global rows. A request for named projects and a request with no project fence read
    what they always read, so every caller that never meets a view is unchanged.
    """

    for request_tuple in ((PROJECT_A,), ("acme",), ()):
        READERS[name](store, request_tuple)()


def test_a_stated_exclusion_changes_what_the_marker_returns(store: SQLiteVNextStore) -> None:
    """Mutation: ignore the stated exclusion in ``list_memories`` or ``list_open_loops``.

    The fixture holds a global health note and a global health loop. With the marker, nothing held back returns both,
    and holding ``health`` back returns neither, so the statement is not decoration.
    """

    for read, key in (
        (READERS["list_memories"](store, (GLOBAL,)), "title"),
        (READERS["list_open_loops"](store, (GLOBAL,)), "title"),
    ):
        kept = {str(row[key]) for row in read(exclude_global_domains=())}  # type: ignore[attr-defined]
        held = {str(row[key]) for row in read(exclude_global_domains=("health",))}  # type: ignore[attr-defined]
        assert any("health" in value for value in kept), kept
        assert not any("health" in value for value in held), held
        assert held < kept


@pytest.mark.parametrize("reader", ["list_memories_view_partitions", "list_open_loops_view_partitions"])
@pytest.mark.parametrize("missing", ["domains", "sensitivity_allowed", "exclude_global_domains", "project_ids"])
def test_the_partition_readers_require_every_control_as_an_argument(
    store: SQLiteVNextStore, reader: str, missing: str
) -> None:
    """Mutation: give ``domains`` or ``sensitivity_allowed`` a default of ``None`` again on either partition reader.

    The single-scan reads have no default for the project ids, the held-back domains, the domain filter or the
    sensitivity ceiling. A caller states each, and writes ``None`` where it means no filter.
    """

    arguments: dict[str, object] = {
        "project_ids": (PROJECT_A,),
        "exclude_global_domains": (),
        "per_partition_limit": 3,
        "domains": None,
        "sensitivity_allowed": CEILING,
    }
    getattr(store, reader)(**arguments)
    del arguments[missing]
    with pytest.raises(TypeError, match=missing):
        getattr(store, reader)(**arguments)


def test_source_reads_state_that_the_store_holds_nothing_back_for_them(store: SQLiteVNextStore) -> None:
    """Mutation: remove the stated empty exclusion from ``search_sources`` or ``search_source_chunks``.

    Source rows are held back in Python (``_source_honours_fence``), so the SQL reads of sources take the marker
    and state, at their call site, that they leave nothing out themselves. They keep working with a request tuple
    that holds the marker, and the brief's excerpt step is where the rule applies.
    """

    assert store.search_sources(query="anything", limit=3, scope_projects=(PROJECT_A, GLOBAL)) == []
    assert store.search_source_chunks(query="anything", limit=3, scope_projects=(PROJECT_A, GLOBAL)) == []
