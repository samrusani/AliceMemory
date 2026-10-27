"""The malformed-id answer class is closed: every id-named field is listed here.

The PostgreSQL store reads ids with ``WHERE id = %s::uuid``, so a value that is not an id in any
accepted spelling used to fail the cast at any door that read one. This test walks the mounted HTTP
application's OpenAPI schema and every MCP tool definition, follows ``$ref``, ``anyOf`` and
``oneOf`` (the shape an optional ``str | None`` takes), ``nullable`` and ``type`` lists (the shape
``{"type": ["string", "null"]}`` takes), ``allOf`` wrappers, arrays (``list[str] | None``) and
nested objects, collects every field or argument whose name ends ``_id`` or ``_ids``, and
classifies each one:

- ``schema_uuid``: the schema says ``format: uuid`` (a ``UUID``-typed model or field), so a
  malformed value answers invalid_request before any store call. Detected from the schema, not
  listed here.
- ``claim_string``: the value is recorded or compared as text (an agent claim, a trace id, a
  session reference, an external chat id, a confirmation token, a project scope name, an external
  provider reference); it never reaches an id cast. Checked in below by name, or by exact location
  for a name that is a row id at another door.
- ``row_id``: the id names a stored row the door reads or writes, in canonical form. Every such
  entry is checked in below with the exact pytest node that reads malformed spellings of it at that
  door, and that node must be among the nodes pytest collects, so renaming, moving or removing a
  test fails here until the entry follows.

Fields the walker cannot see (a free-form object such as the extract request's ``scope``, whose
``person_id`` the door reads by name) are listed as explicit entries and checked by the node test.
A new plain-string id field then fails the first test until someone decides which kind it is; a
row-id entry whose test disappears fails the node check.
"""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import alicebot_api.main as main_module
from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS

REPO_ROOT = Path(__file__).resolve().parents[2]

DOORS = "tests/integration/test_malformed_id_doors_postgres.py"
PERSON = "tests/integration/test_open_loop_person_not_found_postgres.py"
ROW_IDS = "tests/integration/test_row_ids_grant_no_access_postgres.py"
REF_FENCE = "tests/unit/test_source_refs_read_fence.py"

# The row ids of the HTTP doors: (method, path, field path) -> the exact pytest node that sends a
# malformed and a well-formed unknown value to that door and requires the two wire answers to be
# equal. A field path is the property name for a body field, dotted for a nested one; a path
# parameter is the parameter's name.
HTTP_ROW_IDS: dict[tuple[str, str, str], str] = {
    ("GET", "/v0/vnext/beliefs/{belief_id}/state", "belief_id"):
        f"{DOORS}::test_belief_state_http_malformed_id_matches_unknown[no-such-id]",
    ("POST", "/v0/vnext/beliefs/{belief_id}/review", "belief_id"):
        f"{DOORS}::test_belief_review_http_malformed_id_matches_unknown[no-such-id]",
    ("GET", "/v0/vnext/graph/neighborhood/{target_id}", "target_id"):
        f"{DOORS}::test_graph_neighborhood_http_malformed_id_matches_unknown[no-such-id]",
    ("POST", "/v0/vnext/graph/edges/{edge_id}/review", "edge_id"):
        f"{DOORS}::test_graph_edge_review_http_malformed_id_matches_unknown[no-such-id]",
    ("GET", "/v0/vnext/projects/{project_id}/dashboard", "project_id"):
        f"{DOORS}::test_project_dashboard_http_malformed_id_matches_unknown[no-such-id]",
    ("POST", "/v0/vnext/open-loops/{loop_id}/review", "loop_id"):
        f"{DOORS}::test_open_loop_review_http_malformed_id_matches_unknown[no-such-id]",
    ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review", "artifact_id"):
        f"{DOORS}::test_project_update_review_http_malformed_id_matches_unknown[no-such-id]",
    # The extract request's scope is a free-form object the walker cannot
    # see into; its person_id is a row id the person tests read in every
    # malformed spelling.
    ("POST", "/v0/vnext/open-loops/extract", "scope.person_id"):
        f"{PERSON}::test_a_malformed_person_id_answers_not_found_and_writes_nothing[no-such-person]",
    # The open-loop create keeps the memory, source and project it is
    # given: the memory and source go through the single-id resolvers,
    # which refuse a value that is not an id before the store sees it, and
    # the project is held to the key's binding in every spelling.
    ("POST", "/v0/vnext/open-loops", "memory_id"):
        f"{REF_FENCE}::test_the_single_id_resolvers_check_the_value_before_the_store_sees_it_and_return_the_canonical_id",
    ("POST", "/v0/vnext/open-loops", "source_id"):
        f"{REF_FENCE}::test_the_single_id_resolvers_check_the_value_before_the_store_sees_it_and_return_the_canonical_id",
    ("POST", "/v0/vnext/open-loops", "project_id"):
        f"{ROW_IDS}::test_an_open_loop_project_id_outside_the_binding_or_not_well_formed_is_a_missing_project",
}

# A value with one of these names is a claim, not the id of a stored row: an
# agent declares it (agent_id, agent_run_id, task_id, trace_id,
# agent_profile_id, session_id), the caller names an external chat
# (allowed_chat_ids), a confirmation is named by a token compared as text
# (confirmation_id), a reviewer is matched against the authenticated
# identity (reviewer_id), and the legacy continuity surfaces name external
# provider accounts, events and messages by the id the provider assigned.
HTTP_CLAIM_NAMES = {
    "agent_id", "agent_run_id", "task_id", "trace_id", "agent_profile_id",
    "session_id", "allowed_chat_ids", "confirmation_id", "reviewer_id",
    "provider_account_id", "provider_event_id", "provider_message_id",
    "client_id",
}

# Locations whose name would otherwise read as a row id but are claims, listed where they stand
# because the same name is a row id at another door: the project_id of the
# source and memory review bodies is a project scope name recorded on the
# row (a project that is not stored files no loop and answers without an
# error, which the project admission tests pin).
HTTP_CLAIM_PATHS = {
    ("POST", "/v0/vnext/sources/{source_id}/review", "project_id"),
    ("POST", "/v0/vnext/memories/{memory_id}/review", "project_id"),
}

# The row ids of the MCP tools: (tool, argument path) -> the exact pytest node that reads malformed
# spellings of it and requires the wire answer to equal a well-formed unknown id's.
MCP_ROW_IDS: dict[tuple[str, str], str] = {
    ("alice_explain", "memory_id"):
        f"{DOORS}::test_alice_explain_malformed_memory_id_matches_unknown[no-such-id]",
    ("alice_memory_manage", "memory_id"):
        f"{DOORS}::test_alice_memory_manage_malformed_memory_id_matches_unknown[no-such-id-forget]",
    ("alice_memory_manage", "confirmation_id"):
        f"{DOORS}::test_alice_memory_manage_malformed_memory_id_matches_unknown[no-such-id-forget]",
    ("alice_graph_neighborhood", "target_id"):
        f"{DOORS}::test_mcp_row_id_argument_malformed_matches_unknown[no-such-id-alice_graph_neighborhood-arguments0]",
    ("alice_vnext_open_loops", "source_id"):
        f"{DOORS}::test_mcp_row_id_argument_malformed_matches_unknown[no-such-id-alice_vnext_open_loops-arguments1]",
    ("alice_vnext_open_loops", "project_id"):
        f"{DOORS}::test_mcp_row_id_argument_malformed_matches_unknown[no-such-id-alice_vnext_open_loops-arguments2]",
    ("alice_open_loops", "loop_id"):
        f"{DOORS}::test_mcp_row_id_argument_malformed_matches_unknown[no-such-id-alice_open_loops-arguments3]",
    ("alice_project_update_candidate", "project_id"):
        f"{DOORS}::test_mcp_row_id_argument_malformed_matches_unknown[no-such-id-alice_project_update_candidate-arguments4]",
    ("alice_vnext_project_dashboard", "project_id"):
        f"{DOORS}::test_mcp_row_id_argument_malformed_matches_unknown[no-such-id-alice_vnext_project_dashboard-arguments5]",
    ("alice_belief_review", "belief_id"):
        f"{DOORS}::test_belief_review_mcp_answers_not_found_for_malformed_and_unknown",
    ("alice_belief_state", "belief_id"):
        f"{DOORS}::test_legacy_mcp_tool_malformed_id_matches_unknown[no-such-id-alice_belief_state]",
    ("alice_graph_edge_review", "edge_id"):
        f"{DOORS}::test_graph_edge_review_mcp_answers_not_found_for_malformed_and_unknown",
    ("alice_open_loop_review", "loop_id"):
        f"{DOORS}::test_legacy_mcp_tool_malformed_id_matches_unknown[no-such-id-alice_open_loop_review]",
    ("alice_project_dashboard", "project_id"):
        f"{DOORS}::test_legacy_mcp_tool_malformed_id_matches_unknown[no-such-id-alice_project_dashboard]",
    ("alice_vnext_artifact_get", "artifact_id"):
        f"{DOORS}::test_legacy_mcp_tool_malformed_id_matches_unknown[no-such-id-alice_vnext_artifact_get]",
    ("alice_vnext_artifact_review", "artifact_id"):
        f"{DOORS}::test_legacy_mcp_tool_malformed_id_matches_unknown[no-such-id-alice_vnext_artifact_review]",
    ("alice_project_update_review", "artifact_id"):
        f"{DOORS}::test_legacy_mcp_tool_malformed_id_matches_unknown[no-such-id-alice_project_update_review]",
    ("alice_open_loop_extract", "person_id"):
        f"{PERSON}::test_the_legacy_mcp_tool_answers_not_found_for_an_unknown_and_a_malformed_person",
    ("alice_open_loop_extract", "project_id"):
        f"{PERSON}::test_the_legacy_mcp_tool_answers_a_malformed_project_id_as_an_unknown_one",
    ("alice_vnext_undo_memory", "memory_id"):
        f"{DOORS}::test_legacy_memory_tool_malformed_memory_id_matches_unknown[no-such-id-alice_vnext_undo_memory]",
    ("alice_vnext_correct_memory", "memory_id"):
        f"{DOORS}::test_legacy_memory_tool_malformed_memory_id_matches_unknown[no-such-id-alice_vnext_correct_memory]",
    ("alice_vnext_forget_memory", "memory_id"):
        f"{DOORS}::test_legacy_memory_tool_malformed_memory_id_matches_unknown[no-such-id-alice_vnext_forget_memory]",
    ("alice_vnext_memory_audit", "memory_id"):
        f"{DOORS}::test_legacy_memory_tool_malformed_memory_id_matches_unknown[no-such-id-alice_vnext_memory_audit]",
    # A candidate id that is not a stored candidate is reported per
    # candidate (not committed) and never fails the call, malformed or
    # unknown.
    ("alice_commit_captures", "candidates[].candidate_id"):
        f"{DOORS}::test_commit_capture_candidate_id_is_reported_not_failed",
}

# An MCP argument with one of these names is a claim, not a row id: the
# agent declares it, or it is recorded as provenance data (a session
# reference), or it is a confirmation token compared as text. The ids
# inside ``provenance`` and ``replacement_provenance`` of
# ``alice_review_apply`` (``artifact_id``, ``artifact_segment_id``,
# ``capture_event_id``, ``source_event_ids``) are free-form ids recorded as
# data and read back as stored; no row lookup, no 500, which
# ``test_review_apply_provenance_ids_are_data_not_lookups`` pins.
MCP_CLAIM_NAMES = {
    "agent_id", "agent_run_id", "task_id", "trace_id",
    "confirmation_id", "session_id",
    "artifact_segment_id", "capture_event_id", "source_event_ids",
}

# Locations whose name would otherwise read as a row id but are claims: the
# ``artifact_id`` inside the ``provenance`` and ``replacement_provenance``
# of ``alice_review_apply`` is a free-form id recorded as data and read
# back as stored (no row lookup, no 500), while the same name at the
# artifact tools is a row id listed above.
MCP_CLAIM_PATHS = {
    ("alice_review_apply", "provenance.artifact_id"),
    ("alice_review_apply", "replacement_provenance.artifact_id"),
}


def _resolve(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 50:
        schema = components.get(schema["$ref"].split("/")[-1], {})
        seen += 1
    return schema if isinstance(schema, dict) else {}


def _leaf_kinds(schema: dict[str, Any], components: dict[str, Any]) -> set[tuple[str, bool]]:
    """The (shape, uuid) of every string leaf this schema position can take.

    The shape is ``str`` or ``list``; ``uuid`` is true when the leaf says
    ``format: uuid``. Optional fields (``str | None``), ``type`` lists
    (``{"type": ["string", "null"]}``), ``nullable``, ``anyOf``/``oneOf``
    and ``allOf`` wrappers all end in the string leaves they hold.
    """

    kinds: set[tuple[str, bool]] = set()
    stack = [schema]
    seen = 0
    while stack and seen < 100:
        node = _resolve(stack.pop(), components)
        seen += 1
        types = node.get("type")
        type_names = set(types) if isinstance(types, list) else {types}
        if "string" in type_names:
            kinds.add(("str", "format" in node))
        if node.get("type") == "array" or "array" in type_names:
            items = _resolve(node.get("items", {}), components)
            item_types = items.get("type")
            item_names = set(item_types) if isinstance(item_types, list) else {item_types}
            if "string" in item_names:
                kinds.add(("list", "format" in items))
        if node.get("nullable") and ("string" in type_names):
            kinds.add(("str", "format" in node))
        stack.extend(node.get("anyOf", []))
        stack.extend(node.get("oneOf", []))
        stack.extend(node.get("allOf", []))
    return kinds


def _walk_properties(
    schema: dict[str, Any], components: dict[str, Any], visit: Any, path: str = "", depth: int = 0
) -> None:
    """Visit every property of this schema position, nested objects included."""

    if depth > 12:
        return
    node = _resolve(schema, components)
    for name, field_schema in node.get("properties", {}).items():
        field_path = f"{path}.{name}" if path else name
        visit(name, field_path, field_schema, components)
        _walk_properties(field_schema, components, visit, field_path, depth + 1)
    items = _resolve(node.get("items", {}), components)
    if items:
        _walk_properties(items, components, visit, f"{path}[]" if path else "[]", depth + 1)
    for sub in (*node.get("anyOf", []), *node.get("oneOf", []), *node.get("allOf", [])):
        _walk_properties(sub, components, visit, path, depth + 1)
    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        _walk_properties(additional, components, visit, f"{path}{{}}" if path else "{}", depth + 1)


def _http_id_fields() -> tuple[set[tuple[str, str, str, str]], set[tuple[str, str, str, str]]]:
    """Every id-named HTTP field as (method, path, field path, shape), split by uuid-ness.

    The first set holds the plain-string fields the classes below must
    close; the second holds the ``format: uuid`` fields the argument layer
    already refuses.
    """

    schema = main_module.app.openapi()
    components = schema.get("components", {}).get("schemas", {})
    plain: set[tuple[str, str, str, str]] = set()
    uuids: set[tuple[str, str, str, str]] = set()

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                name = parameter.get("name", "")
                if not name.endswith(("_id", "_ids")):
                    continue
                for shape, is_uuid in _leaf_kinds(parameter.get("schema", {}), components):
                    entry = (method.upper(), path, f"param:{name}", shape)
                    (uuids if is_uuid else plain).add(entry)
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})

            def visit(name: str, field_path: str, field_schema: dict[str, Any], comps: dict[str, Any]) -> None:
                if not name.endswith(("_id", "_ids")):
                    return
                for shape, is_uuid in _leaf_kinds(field_schema, comps):
                    entry = (method.upper(), path, field_path, shape)
                    (uuids if is_uuid else plain).add(entry)

            _walk_properties(body, components, visit)
    return plain, uuids


def _mcp_id_arguments() -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str]]]:
    """Every id-named MCP argument as (tool, argument path, shape), split by uuid-ness."""

    plain: set[tuple[str, str, str]] = set()
    uuids: set[tuple[str, str, str]] = set()
    for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS):
        input_schema = tool.get("inputSchema", {})

        def visit(name: str, field_path: str, field_schema: dict[str, Any], comps: dict[str, Any]) -> None:
            if not name.endswith(("_id", "_ids")):
                return
            for shape, is_uuid in _leaf_kinds(field_schema, comps):
                entry = (tool["name"], field_path, shape)
                (uuids if is_uuid else plain).add(entry)

        _walk_properties(input_schema, {}, visit)
    return plain, uuids


@lru_cache(maxsize=1)
def _collected_nodes() -> frozenset[str]:
    """Every pytest node the row-id entries name, collected once.

    The named files are collected for real (``pytest --collect-only``), so a
    test that was renamed, moved or removed is not found here even when the
    string in the list still looks right. The match is exact: an entry names
    the whole node id, parameters included.
    """

    files = sorted({node.split("::")[0] for node in (*HTTP_ROW_IDS.values(), *MCP_ROW_IDS.values())})
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *files],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=False,
    )
    nodes = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and "::" in line and not line.startswith("=")
    }
    assert result.returncode == 0, (result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    return frozenset(nodes)


def test_every_http_plain_string_id_field_is_listed_with_its_kind() -> None:
    plain, uuids = _http_id_fields()
    assert len(plain) >= 20 and len(uuids) >= 100
    listed: set[tuple[str, str, str, str]] = set()
    for method, path, field_path, shape in plain:
        name = field_path.split(":")[-1].split(".")[-1].split("[]")[-1].split("{}")[-1]
        if (
            (method, path, name) in HTTP_ROW_IDS
            or (method, path, field_path) in HTTP_ROW_IDS
            or (method, path, field_path) in HTTP_CLAIM_PATHS
            or name in HTTP_CLAIM_NAMES
        ):
            listed.add((method, path, field_path, shape))
    assert plain == listed, {
        "unlisted": sorted(plain - listed),
        "stale": sorted(listed - plain),
    }


def test_every_http_row_id_entry_names_a_collected_test() -> None:
    nodes = _collected_nodes()
    for key, node in HTTP_ROW_IDS.items():
        assert node in nodes, (key, node)


def test_every_mcp_plain_string_id_argument_is_listed_with_its_kind() -> None:
    plain, uuids = _mcp_id_arguments()
    assert len(plain) >= 20 and len(uuids) >= 40
    listed: set[tuple[str, str, str]] = set()
    for tool, argument_path, shape in plain:
        name = argument_path.split(".")[-1].split("[]")[-1].split("{}")[-1]
        if (tool, argument_path) in MCP_ROW_IDS or (tool, argument_path) in MCP_CLAIM_PATHS or name in MCP_CLAIM_NAMES:
            listed.add((tool, argument_path, shape))
    assert plain == listed, {
        "unlisted": sorted(plain - listed),
        "stale": sorted(listed - plain),
    }


def test_every_mcp_row_id_entry_names_a_collected_test() -> None:
    nodes = _collected_nodes()
    for key, node in MCP_ROW_IDS.items():
        assert node in nodes, (key, node)
