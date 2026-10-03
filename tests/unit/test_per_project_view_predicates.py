"""Per-project memory S2: the global marker in SQL and in Python (spec 6.1, tests 17, 19 and 20).

A request tuple may hold the reserved marker ``~global``. A row is in the view when its identity
set holds a requested id, or, when the marker is asked for, holds no Alice project id. The SQL
builders in ``query_predicates`` and ``project_scopes_overlap`` in ``vnext_project_scope`` are the
only places that learn it. The three identity functions describe rows and do not change.

Each test names the edit that makes it fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alicebot_api.mcp.registry import call_mcp_tool
from alicebot_api.sqlite_store import SQLiteVNextStore, sqlite_user_connection
from alicebot_api.vnext_agent_control import resource_project_scope
from alicebot_api.vnext_project_scope import (
    GLOBAL_PROJECT_MARKER,
    is_alice_project_id,
    is_global_scope,
    memory_project_scope,
    project_scopes_overlap,
    refuse_global_marker,
    source_project_scope,
)
from alicebot_api.vnext_stores.sqlite import query_predicates
from tests.unit.per_project_s2_support import PROJECT_A, PROJECT_B, SECONDARY_A, add_loop, add_memory, capture, context_for, db_path_for
from tests.unit.per_project_view_support import USER_ID

GLOBAL = GLOBAL_PROJECT_MARKER
UPPER_A = PROJECT_A.upper()
#: ``prj_a1a1a1a1a1a1a1a1`` spelled with JSON unicode escapes, in the last two characters of the id and in the first
#: character of the prefix. Built from a backslash so no tool that decodes escapes can turn them back into letters.
BACKSLASH = chr(92)
ESCAPED_ID = '"prj_a1a1a1a1a1a1a1' + BACKSLASH + 'u0061' + BACKSLASH + 'u0031"'
ESCAPED_PREFIX = '"' + BACKSLASH + 'u0070rj_a1a1a1a1a1a1a1a1"'

#: Metadata text and the ``project_id`` column of one row, by name. Each shape is raw text so a
#: test can plant spellings the store's writers never produce.
SHAPES: dict[str, tuple[str, str | None]] = {
    "root": (json.dumps({"project_scope": [PROJECT_A]}), None),
    "root_other": (json.dumps({"project_scope": [PROJECT_B]}), None),
    "root_secondary": (json.dumps({"project_scope": [SECONDARY_A]}), None),
    "nested_agentic": (json.dumps({"agentic_memory": {"project_scope": [PROJECT_A]}}), None),
    "nested_identity": (json.dumps({"agent_identity": {"project_scope": [PROJECT_A]}}), None),
    "legacy_singular": (json.dumps({"project": PROJECT_A}), None),
    "legacy_plural": (json.dumps({"projects": [PROJECT_A]}), None),
    "column_only": ("{}", PROJECT_A),
    "column_other": ("{}", PROJECT_B),
    "empty_beats_legacy": (json.dumps({"project_scope": [], "project": PROJECT_A}), None),
    "empty_beats_column": (json.dumps({"project_scope": []}), PROJECT_A),
    "uppercase": (json.dumps({"project_scope": [UPPER_A]}), None),
    "padded": (json.dumps({"project_scope": ["  " + PROJECT_A + "\t"]}), None),
    "multi": (json.dumps({"project_scope": [PROJECT_A, PROJECT_B]}), None),
    "mixed_name_and_id": (json.dumps({"project_scope": ["acme", PROJECT_A]}), None),
    "free_form": (json.dumps({"project_scope": ["acme"]}), None),
    "free_form_column": ("{}", "acme"),
    "numeric": (json.dumps({"project_scope": [12345]}), None),
    "nested_list": (json.dumps({"project_scope": [[PROJECT_A]]}), None),
    "wrong_length": (json.dumps({"project_scope": ["prj_a1a1a1a1a1a1a1a"]}), None),
    "stored_marker_name": (json.dumps({"project_scope": [GLOBAL]}), None),
    "malformed_canonical": (json.dumps({"project_scope": PROJECT_A}), None),
    "null_canonical": (json.dumps({"project_scope": None}), None),
    "no_scope": ("{}", None),
    "escaped_id": ('{"project_scope": [' + ESCAPED_ID + "]}", None),
    "escaped_prefix": ('{"project_scope": [' + ESCAPED_PREFIX + "]}", None),
    "mentions_id_elsewhere": (json.dumps({"note": PROJECT_A, "project_scope": ["acme"]}), None),
}

VIEWS: dict[str, tuple[str, ...]] = {
    "project": (PROJECT_A, SECONDARY_A, GLOBAL),
    "project_primary_only": (PROJECT_A, GLOBAL),
    "project_only": (PROJECT_A, SECONDARY_A),
    "global": (GLOBAL,),
    "other_project": (PROJECT_B, GLOBAL),
    "free_form_name": ("acme",),
    "name_and_marker": ("acme", GLOBAL),
    "uppercase_request": (UPPER_A, GLOBAL.upper()),
}


@pytest.fixture
def fuzz_store(tmp_path: Path):
    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context_for(data_dir)
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        yield store


def _plant(store: SQLiteVNextStore, table: str, row_id: str, metadata_text: str, project_id: str | None) -> None:
    store.conn.execute(
        f"UPDATE {table} SET metadata_json = ?, project_id = ? WHERE id = ?",
        (metadata_text, project_id, row_id),
    )


def _held_back(row: dict, scope: tuple[str, ...], excluded: tuple[str, ...]) -> bool:
    return row.get("domain") in excluded and is_global_scope(scope)


def _python_admits(scope: tuple[str, ...], request: tuple[str, ...]) -> bool:
    return project_scopes_overlap(scope, request)


def test_the_sql_predicate_for_memories_equals_the_python_predicate(fuzz_store: SQLiteVNextStore) -> None:
    """Mutation: remove the ``\\u`` guard from the native prefilter, or define global as "empty scope only".

    One memory per scope shape, planted as raw metadata text, read through ``list_memories`` under each
    view. The Python predicate is the judge. The ``\\u`` guard matters twice: an escaped id has no ``prj_``
    in its text for a project-only prefilter to find, and an escaped prefix has none for the fast global path.
    Defining global as "empty scope only" drops ``free_form`` and its relatives from the global views.
    """

    ids: dict[str, str] = {}
    for name, (metadata, project_id) in SHAPES.items():
        row = add_memory(fuzz_store, key=f"fuzz.{name}", text=f"Fuzz row {name} fuzzword")
        _plant(fuzz_store, "memories", str(row["id"]), metadata, project_id)
        ids[str(row["id"])] = name
    rows = fuzz_store.list_memories(limit=None)
    assert len(rows) == len(SHAPES)
    results: dict[str, set[str]] = {}
    for view_name, request in VIEWS.items():
        expected = {
            str(row["id"]) for row in rows if _python_admits(memory_project_scope(row), request)
        }
        got = {
            str(row["id"])
            for row in fuzz_store.list_memories(projects=request, limit=None, exclude_global_domains=())
        }
        assert got == expected, (view_name, sorted(ids[i] for i in got ^ expected))
        results[view_name] = got
    assert all(results.values()), "every view must admit some shape, or the comparison is vacuous"
    assert results["project"] != results["global"] != results["project_only"]
    assert results["global"] & results["project_only"] == set()


def test_the_sql_predicate_for_open_loops_equals_the_python_predicate(fuzz_store: SQLiteVNextStore) -> None:
    """Mutation: remove the ``\\u`` guard, or define global as "empty scope only" in the loop predicate.

    The loop builder reads the scope from ``metadata_json`` and the ``project_id`` column, as the memory
    builder does, but it is a different code path (``_metadata_scope_clause``).
    """

    ids: dict[str, str] = {}
    for name, (metadata, project_id) in SHAPES.items():
        row = add_loop(fuzz_store, title=f"Fuzz loop {name} fuzzword")
        _plant(fuzz_store, "open_loops", str(row["id"]), metadata, project_id)
        ids[str(row["id"])] = name
    rows = fuzz_store.list_open_loops(status=None, limit=1000)
    assert len(rows) == len(SHAPES)
    for view_name, request in VIEWS.items():
        expected = {str(row["id"]) for row in rows if _python_admits(resource_project_scope(row), request)}
        got = {
            str(row["id"])
            for row in fuzz_store.list_open_loops(
                status=None, limit=1000, scope_projects=request, exclude_global_domains=()
            )
        }
        assert got == expected, (view_name, sorted(ids[i] for i in got ^ expected))


#: Source envelopes hold the whole scope envelope in ``metadata_json``: root fields and optional
#: ``metadata_json`` and ``scope_json`` containers, which a different identity function reads.
SOURCE_SHAPES: dict[str, str] = {
    "root": json.dumps({"project_scope": [PROJECT_A]}),
    "container": json.dumps({"metadata_json": {"project_scope": [PROJECT_A]}}),
    "scope_json": json.dumps({"scope_json": {"project_scope": [PROJECT_A]}}),
    "other": json.dumps({"project_scope": [PROJECT_B]}),
    "legacy": json.dumps({"project": PROJECT_A}),
    "nested_agentic": json.dumps({"agentic_memory": {"project_scope": [PROJECT_A]}}),
    "empty_beats_legacy": json.dumps({"project_scope": [], "project": PROJECT_A}),
    "uppercase": json.dumps({"project_scope": [UPPER_A]}),
    "multi": json.dumps({"project_scope": [PROJECT_A, PROJECT_B]}),
    "free_form": json.dumps({"project_scope": ["acme"]}),
    "numeric": json.dumps({"project_scope": [7]}),
    "none": "{}",
    "escaped_id": '{"project_scope": [' + ESCAPED_ID + "]}",
    "escaped_prefix": '{"project_scope": [' + ESCAPED_PREFIX + "]}",
    "mentions_id_elsewhere": json.dumps({"raw_text": PROJECT_A, "project_scope": ["acme"]}),
}


def test_the_sql_predicate_for_sources_equals_the_python_predicate(tmp_path: Path) -> None:
    """Mutation: remove the ``\\u`` guard, or define global as "empty scope only" in the source predicate.

    Sources keep the whole envelope in their metadata and are read through ``search_sources``. The Python
    judge is ``source_project_scope``, which has its own presence rules.
    """

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = context_for(data_dir)
    names: dict[str, str] = {}
    for name in SOURCE_SHAPES:
        payload = capture(context, f"# {name}\n\nSource body {name}.\n", title=f"Fuzzsource {name}")
        names[str(payload["source_id"])] = name
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for source_id, name in names.items():
            store.conn.execute("UPDATE sources SET metadata_json = ? WHERE id = ?", (SOURCE_SHAPES[name], source_id))
        everything = store.search_sources(query="Fuzzsource", limit=1000)
        assert len(everything) == len(SOURCE_SHAPES)
        for view_name, request in VIEWS.items():
            expected = {
                str(row["id"])
                for row in everything
                if _python_admits(source_project_scope({"metadata_json": json.loads(SOURCE_SHAPES[names[str(row["id"])]])}), request)
            }
            got = {str(row["id"]) for row in store.search_sources(query="Fuzzsource", limit=1000, scope_projects=request)}
            assert got == expected, (view_name, sorted(names[i] for i in got ^ expected))


def test_the_exact_sql_test_without_the_prefilter_equals_the_python_predicate(fuzz_store: SQLiteVNextStore) -> None:
    """Mutation: change the exact test (``_view_membership_sql``) in one of the two places.

    The exact test is what decides every row the native prefilter cannot rule out. It is run here alone, as a
    bare predicate with no prefilter in front, against the same memories and views.
    """

    for name, (metadata, project_id) in SHAPES.items():
        row = add_memory(fuzz_store, key=f"exact.{name}", text=f"Exact row {name} exactword")
        _plant(fuzz_store, "memories", str(row["id"]), metadata, project_id)
    rows = fuzz_store.list_memories(limit=None)
    for view_name, request in VIEWS.items():
        identity = query_predicates.project_scope_identity(request)
        wants_global = GLOBAL in identity
        ids = tuple(value for value in identity if value != GLOBAL)
        sql, params = query_predicates._view_membership_sql(
            placeholders=fuzz_store._placeholders,
            scope_expression="alice_project_scope_identity(metadata_json, project_id)",
            ids=ids,
            wants_global=wants_global,
            domain_expression=None,
            global_excluded_domains=(),
            text_expressions=("metadata_json", "project_id"),
            partition=False,
        )
        got = {
            str(row["id"])
            for row in fuzz_store._fetch_all(f"SELECT id FROM memories WHERE {sql} = 1", tuple(params))
        }
        expected = {str(row["id"]) for row in rows if _python_admits(memory_project_scope(row), request)}
        assert got == expected, view_name


def test_the_view_holds_back_global_notes_in_the_excluded_domains_in_sql_and_in_python(
    fuzz_store: SQLiteVNextStore,
) -> None:
    """Mutation: test the domain without testing that the note is global, in either builder.

    A project's own note in an excluded domain stays; a global note in it goes; a note in another domain stays.
    The memory builder, the loop builder and the source row test agree with ``is_global_scope``.
    """

    excluded = ("health",)
    for name in ("root", "free_form", "no_scope", "multi", "escaped_id"):
        metadata, project_id = SHAPES[name]
        for domain in ("health", "project"):
            memory = add_memory(fuzz_store, key=f"held.{name}.{domain}", text=f"Held {name} {domain}", domain=domain)
            _plant(fuzz_store, "memories", str(memory["id"]), metadata, project_id)
            loop = add_loop(fuzz_store, title=f"Held loop {name} {domain}", domain=domain)
            _plant(fuzz_store, "open_loops", str(loop["id"]), metadata, project_id)
    request = (PROJECT_A, GLOBAL)
    rows = fuzz_store.list_memories(limit=None)
    expected_memories = {
        str(row["id"])
        for row in rows
        if _python_admits(memory_project_scope(row), request)
        and not _held_back(row, memory_project_scope(row), excluded)
    }
    got_memories = {
        str(row["id"])
        for row in fuzz_store.list_memories(projects=request, limit=None, exclude_global_domains=excluded)
    }
    assert got_memories == expected_memories
    loops = fuzz_store.list_open_loops(status=None, limit=1000)
    expected_loops = {
        str(row["id"])
        for row in loops
        if _python_admits(resource_project_scope(row), request)
        and not _held_back(row, resource_project_scope(row), excluded)
    }
    got_loops = {
        str(row["id"])
        for row in fuzz_store.list_open_loops(
            status=None, limit=1000, scope_projects=request, exclude_global_domains=excluded
        )
    }
    assert got_loops == expected_loops
    assert got_memories != {
        str(row["id"]) for row in rows if _python_admits(memory_project_scope(row), request)
    }, "the exclusion must change the result, or this test is vacuous"


def test_the_single_scan_partitions_label_every_stored_shape_as_the_python_judge_does(
    fuzz_store: SQLiteVNextStore,
) -> None:
    """Mutation: drop ``project_id`` from ``text_expressions`` in ``list_memories_view_partitions``, or in
    ``list_open_loops_view_partitions``, or drop the ``\\u`` guard from ``_no_alice_id_in_text``.

    The single-scan reads label a row global through a native fast path that looks only at the raw text of the
    columns it is given. A pre-upgrade note whose only project marker is the ``project_id`` column (the metadata
    is empty), an uppercase or padded id, an escaped id and a note that mentions an id without being scoped to it
    all have to land where ``project_scopes_overlap`` puts them. Every shape of the predicate fuzz is planted in
    a plain and a held-back domain, as a memory and as a loop, and each partition is compared with the Python
    judge, with nothing held back and with a domain held back. Without ``project_id`` in the list a
    column-only note of this project is read as global and one of another project too.
    """

    ceiling = ["public", "internal", "private", "unknown"]
    labels: dict[str, tuple[str, str]] = {}
    for name, (metadata, project_id) in SHAPES.items():
        for domain in ("project", "health"):
            memory = add_memory(fuzz_store, key=f"part.{name}.{domain}", text=f"Part row {name} {domain}", domain=domain)
            _plant(fuzz_store, "memories", str(memory["id"]), metadata, project_id)
            labels[str(memory["id"])] = (name, domain)
            loop = add_loop(fuzz_store, title=f"Part loop {name} {domain}", domain=domain)
            _plant(fuzz_store, "open_loops", str(loop["id"]), metadata, project_id)
            labels[str(loop["id"])] = (name, domain)
    project_ids = (PROJECT_A, SECONDARY_A)
    memories = fuzz_store.list_memories(limit=None)
    loops = fuzz_store.list_open_loops(status=None, limit=1000)
    assert len(memories) == len(loops) == 2 * len(SHAPES)
    for excluded in ((), ("health",)):
        for kind, rows, scope_of in (
            ("memory", memories, memory_project_scope),
            ("loop", loops, resource_project_scope),
        ):
            expected_project = {
                str(row["id"]) for row in rows if project_scopes_overlap(scope_of(row), project_ids)
            }
            expected_global = {
                str(row["id"])
                for row in rows
                if project_scopes_overlap(scope_of(row), (GLOBAL,))
                and not _held_back(row, scope_of(row), excluded)
            }
            if kind == "memory":
                project_rows, global_rows = fuzz_store.list_memories_view_partitions(
                    project_ids=project_ids,
                    exclude_global_domains=excluded,
                    per_partition_limit=1000,
                    domains=None,
                    sensitivity_allowed=ceiling,
                    status=None,
                    statuses=("active",),
                )
            else:
                project_rows, global_rows = fuzz_store.list_open_loops_view_partitions(
                    project_ids=project_ids,
                    exclude_global_domains=excluded,
                    per_partition_limit=1000,
                    domains=None,
                    sensitivity_allowed=ceiling,
                    status=None,
                    statuses=("open",),
                )
            got_project = {str(row["id"]) for row in project_rows}
            got_global = {str(row["id"]) for row in global_rows}
            assert got_project == expected_project, (
                kind, excluded, sorted(labels[i] for i in got_project ^ expected_project)
            )
            assert got_global == expected_global, (
                kind, excluded, sorted(labels[i] for i in got_global ^ expected_global)
            )
            names_in_project = {labels[i][0] for i in got_project}
            names_in_global = {labels[i][0] for i in got_global}
            # The comparison above is not vacuous: each hard shape lands on the side the judge puts it.
            assert {"column_only", "uppercase", "padded", "escaped_id", "escaped_prefix", "root"} <= names_in_project
            assert {"no_scope", "free_form", "free_form_column"} <= names_in_global
            assert "column_other" not in names_in_project | names_in_global


def test_a_stored_string_that_reads_like_the_marker_is_a_free_form_name() -> None:
    """Mutation: compare the marker against stored values.

    A note filed under the name ``~global`` is global like any free-form name, and it does not match a request
    for the project, and the marker is never compared with what a row stores.
    """

    assert project_scopes_overlap((GLOBAL,), (GLOBAL,)) is True
    assert project_scopes_overlap((GLOBAL,), (PROJECT_A,)) is False
    assert project_scopes_overlap((GLOBAL, PROJECT_A), (GLOBAL,)) is False
    assert project_scopes_overlap((PROJECT_A,), (GLOBAL,)) is False
    assert project_scopes_overlap((), (GLOBAL,)) is True
    assert project_scopes_overlap(("acme",), (GLOBAL,)) is True
    assert project_scopes_overlap((), ()) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (PROJECT_A, True),
        (UPPER_A, True),
        ("  " + PROJECT_A + " ", True),
        ("prj_a1a1a1a1a1a1a1a", False),
        ("prj_a1a1a1a1a1a1a1a1a", False),
        ("prj_g1g1g1g1g1g1g1g1", False),
        ("acme", False),
        (GLOBAL, False),
        (12, False),
        (None, False),
    ],
)
def test_an_alice_project_id_is_prj_and_sixteen_hex_characters(value: object, expected: bool) -> None:
    """Mutation: loosen the pattern to ``prj_`` and any length, or stop folding ASCII case."""

    assert is_alice_project_id(value) is expected


def test_the_identity_functions_describe_rows_and_do_not_know_the_marker(fuzz_store: SQLiteVNextStore) -> None:
    """Mutation: make an identity function return the marker for an empty scope.

    The three SQL identity functions and ``project_scope_identity`` describe rows. Capture dedupe compares
    them by equality, so a function that answered the marker for an empty scope would stop an unscoped
    request from matching an unscoped note and capture would mint duplicates (spec 6.1, 6.7).
    """

    cursor = fuzz_store.conn.execute(
        "SELECT alice_project_scope_identity(NULL, NULL) AS a, alice_project_scope_identity('{}', NULL) AS b,"
        " alice_project_scope_identity('{\"project_scope\": [\"acme\"]}', NULL) AS c,"
        " alice_source_project_scope_identity('{}') AS d, alice_source_project_scope_identity(NULL) AS e,"
        " alice_source_project_scope_identity('{\"project_scope\": [\"acme\"]}') AS f,"
        " alice_project_scope_value(NULL) AS g"
    )
    row = cursor.fetchone()
    cursor.close()
    assert [row[key] for key in "abcdef"] == ["[]", "[]", '["acme"]', "[]", "[]", '["acme"]']
    assert GLOBAL not in str(row["g"] or "")
    assert query_predicates.project_scope_identity(()) == ()


def test_an_unscoped_capture_still_matches_an_unscoped_note_by_identity(fuzz_store: SQLiteVNextStore) -> None:
    """Mutation: make an identity function return the marker, or give dedupe the policy scope.

    ``find_live_memory_by_canonical_text`` for an unscoped request still finds an unscoped note and a free-form
    named one by its exact scope, and an unscoped request never matches a note in a project.
    """

    plain = add_memory(fuzz_store, key="dedupe.plain", text="The same user asserted value")
    named = add_memory(fuzz_store, key="dedupe.named", text="The same user asserted value too", scope=("acme",))
    owned = add_memory(fuzz_store, key="dedupe.owned", text="The same user asserted value again", scope=(PROJECT_A,))
    found = fuzz_store.find_live_memory_by_canonical_text(
        "the same user asserted value", domain="project", sensitivity="public", project_scope=()
    )
    assert found is not None and found["id"] == plain["id"]
    found = fuzz_store.find_live_memory_by_canonical_text(
        "The same user asserted value too", domain="project", sensitivity="public", project_scope=("acme",)
    )
    assert found is not None and found["id"] == named["id"]
    assert (
        fuzz_store.find_live_memory_by_canonical_text(
            "The same user asserted value again", domain="project", sensitivity="public", project_scope=()
        )
        is None
    )
    found = fuzz_store.find_live_memory_by_canonical_text(
        "The same user asserted value again", domain="project", sensitivity="public", project_scope=(PROJECT_A,)
    )
    assert found is not None and found["id"] == owned["id"]


MARKER_SPELLINGS = [GLOBAL, GLOBAL.upper(), f"  {GLOBAL}  ", "~Global"]

COMMIT_ARGUMENTS = {
    "title": "t",
    "canonical_text": "text",
    "memory_type": "decision",
    "domain": "project",
    "sensitivity": "public",
    "confidence": 0.96,
    "rationale": "User said: remember this",
}
MARKER_TOOLS: dict[str, dict] = {
    "alice_recall": {"query": "anything"},
    "alice_context_pack": {"query": "anything"},
    "alice_resume": {},
    "alice_open_loops": {},
    "alice_recent_decisions": {},
    "alice_memory_review": {},
    "alice_memory_commit": COMMIT_ARGUMENTS,
    "alice_capture": {"raw_text": "body", "title": "t", "domain": "project", "sensitivity": "public"},
}
PROJECT_ARGUMENTS = ("projects", "project", "project_scope", "identity")
#: The spellings each tool's schema does not take at all. Those calls are refused by the schema before any
#: handler runs, with a different message, so they are not the marker check's to test.
NOT_TAKEN = {
    ("alice_capture", "project"),
    ("alice_capture", "projects"),
    ("alice_context_pack", "project"),
    ("alice_memory_commit", "project"),
    ("alice_memory_commit", "projects"),
    ("alice_memory_review", "project"),
    ("alice_open_loops", "project"),
    ("alice_open_loops", "projects"),
    ("alice_recent_decisions", "projects"),
    ("alice_resume", "projects"),
}


@pytest.mark.parametrize("spelling", MARKER_SPELLINGS)
@pytest.mark.parametrize(
    ("tool", "argument"),
    [(tool, argument) for tool in MARKER_TOOLS for argument in PROJECT_ARGUMENTS if (tool, argument) not in NOT_TAKEN],
)
def test_the_marker_is_refused_as_caller_input_on_every_tool(tmp_path: Path, tool: str, argument: str, spelling: str) -> None:
    """Mutation: remove the check at the argument boundary (``_refuse_reserved_project_marker`` in the registry).

    ``~global`` exists only inside a request tuple that Alice builds from a project view. A caller that sends
    it in ``projects``, ``project``, ``project_scope`` or an identity's ``project_scope``, in any case or with
    padding, is refused before any handler runs, with one fixed message, and nothing is stored.
    """

    from alicebot_api.mcp.policy import RESERVED_PROJECT_NAME_MESSAGE
    from alicebot_api.mcp.types import MCPInvalidRequestError

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = context_for(data_dir)

    def arguments_with(value: object) -> dict:
        arguments = dict(MARKER_TOOLS[tool])
        if argument == "projects":
            arguments["projects"] = [value]
        elif argument == "project":
            arguments["project"] = value
        elif argument == "project_scope":
            arguments["project_scope"] = [value]
        else:
            arguments["agent_identity"] = {
                "agent_id": "tester",
                "permission_profile": "trusted_local_agent",
                "project_scope": [value],
            }
        return arguments

    # A harmless value gets past the schema, so the refusal below is the marker check and nothing else.
    call_mcp_tool(context, name=tool, arguments=arguments_with("acme-fixture"))
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        before = SQLiteVNextStore(connection, USER_ID).list_memories(limit=None)
    with pytest.raises(MCPInvalidRequestError) as caught:
        call_mcp_tool(context, name=tool, arguments=arguments_with(spelling))
    assert str(caught.value) == RESERVED_PROJECT_NAME_MESSAGE
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        assert store.list_memories(limit=None) == before
        for rows in (
            store.list_memories(limit=None),
            store.list_open_loops(status=None, limit=100),
            store.search_sources(query="body", limit=10),
        ):
            assert not [row for row in rows if GLOBAL in json.dumps(row, default=str)]


def test_the_marker_is_never_stored_on_a_note_a_source_or_an_open_loop(tmp_path: Path) -> None:
    """Mutation: let a writer store the request tuple, or let ``write_project`` carry the marker.

    A free-form name that reads ``~global`` can be stored only through a path that skips the argument boundary,
    and the reads and the view the tests above build never write it. After a project brief, a global brief and
    a refused write, no table holds the marker where Alice put it.
    """

    from alicebot_api.project_view import ProjectView
    from tests.unit.per_project_view_support import compile_view_brief, project_view

    data_dir = tmp_path / "vault"
    data_dir.mkdir()
    context = context_for(data_dir)
    capture(context, "# t\n\nbody text\n", title="plain")
    capture(context, "# t\n\nbody text two\n", title="scoped", project_scope=(PROJECT_A,))
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        add_memory(store, key="never.stored.memory", text="A note in the project", scope=(PROJECT_A,))
        add_memory(store, key="never.stored.global", text="A note in no project")
        add_loop(store, title="A loop in the project", scope=(PROJECT_A,))
    for choice in ("project", "project_only", "global", "all"):
        compile_view_brief(data_dir, project_view(choice=choice))
    assert project_view().write_project == PROJECT_A
    assert ProjectView.unscoped().write_project is None
    with sqlite_user_connection(db_path_for(data_dir), USER_ID) as connection:
        store = SQLiteVNextStore(connection, USER_ID)
        for table in ("memories", "sources", "open_loops", "event_log"):
            column = "payload_json" if table == "event_log" else "metadata_json"
            count = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {column} LIKE '%~global%'").fetchone()
            assert count["n"] == 0, table


@pytest.mark.parametrize("module", ["vnext_rollups", "vnext_consolidation"])
def test_the_rollup_and_consolidation_helpers_refuse_a_tuple_that_holds_the_marker(module: str) -> None:
    """Mutation: let a helper ignore the marker.

    These helpers take explicit project names only. A helper that did not know the marker would read it as a
    name no row holds and hide every global note without saying so, so it raises.
    """

    import importlib

    helpers = importlib.import_module(f"alicebot_api.{module}")
    with pytest.raises(ValueError):
        helpers._scoped_rows([], domains=[], sensitivity_allowed=["public"], projects=(PROJECT_A, GLOBAL))
    assert helpers._scoped_rows([], domains=[], sensitivity_allowed=["public"], projects=(PROJECT_A,)) == []


def test_the_scheduler_helper_refuses_a_tuple_that_holds_the_marker() -> None:
    """Mutation: drop ``refuse_global_marker`` from the scheduler's project normalizer."""

    from alicebot_api import vnext_scheduler

    with pytest.raises(ValueError):
        vnext_scheduler._normalized_project_scope((GLOBAL,))
    assert vnext_scheduler._normalized_project_scope((PROJECT_A,)) == (PROJECT_A,)


def test_refuse_global_marker_names_the_caller_and_never_the_request() -> None:
    """Mutation: echo the request into the message."""

    with pytest.raises(ValueError) as caught:
        refuse_global_marker((PROJECT_A, GLOBAL), where="rollups")
    assert "rollups" in str(caught.value)
    assert PROJECT_A not in str(caught.value)
    refuse_global_marker((PROJECT_A, "acme"), where="rollups")


def test_the_cli_refuses_the_marker_as_a_scope_value(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Mutation: add the marker to the ``--scope`` choices.

    The command line takes ``project``, ``project_only``, ``global`` and ``all``, and the marker is none of them.
    """

    from alicebot_api.onramp import main as onramp_main

    for bad in (GLOBAL, "~GLOBAL"):
        assert onramp_main(["brief", "--data-dir", str(tmp_path / "v"), "--scope", bad]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "invalid_request" in captured.err
    assert not (tmp_path / "v").exists(), "a refused command creates no vault"
