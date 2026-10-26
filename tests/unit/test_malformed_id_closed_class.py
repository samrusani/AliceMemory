"""The malformed-id answer class is closed: every plain-string id argument is listed here.

The PostgreSQL store reads ids with ``WHERE id = %s::uuid``, so a value that is not an id in any
accepted spelling used to fail the cast at any door that read one. That is now fixed by reading
every id argument through ``_vnext_canonical_id`` first (a value that parses is used in canonical
form, one that does not is answered as a well-formed unknown id is). This test walks the mounted
HTTP application and the MCP tool definitions, collects every parameter, body field and tool
argument whose name ends ``_id`` or ``_ids`` and whose schema is a plain string, and compares the
collection with the list below. A new id argument must be added here with its kind:

- ``row_id``: the id names a stored row the door reads or writes. Its test reads malformed
  spellings of it at that door and asserts the answer matches a well-formed unknown id's.
- ``claim_string``: the value is recorded or compared as text (an agent claim, a trace id, an
  external chat id, a session reference); it never reaches an id cast.
- ``schema_uuid``: the field is parsed or refused at the argument layer (``format: uuid`` or a
  ``UUID``-typed model), so a malformed value answers invalid_request before any store call.
"""

from __future__ import annotations

import alicebot_api.main as main_module
from alicebot_api.mcp.definitions import _CORE_TOOL_DEFINITIONS, _LEGACY_TOOL_DEFINITIONS

HTTP_ROW_IDS = {
    ("GET", "/v0/vnext/beliefs/{belief_id}/state", "belief_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_belief_state_http_malformed_id_matches_unknown",
    ("POST", "/v0/vnext/beliefs/{belief_id}/review", "belief_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_belief_review_http_malformed_id_matches_unknown",
    ("GET", "/v0/vnext/graph/neighborhood/{target_id}", "target_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_graph_neighborhood_http_malformed_id_matches_unknown",
    ("POST", "/v0/vnext/graph/edges/{edge_id}/review", "edge_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_graph_edge_review_http_malformed_id_matches_unknown",
    ("GET", "/v0/vnext/projects/{project_id}/dashboard", "project_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_project_dashboard_http_malformed_id_matches_unknown",
    ("POST", "/v0/vnext/open-loops/{loop_id}/review", "loop_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_open_loop_review_http_malformed_id_matches_unknown",
    ("POST", "/v0/vnext/projects/update-candidates/{artifact_id}/review", "artifact_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_project_update_review_http_malformed_id_matches_unknown",
}

HTTP_CLAIM_STRINGS = {
    ("POST", "/v0/vnext/agents/ingest-output", "agent_id"),
    ("POST", "/v0/vnext/connectors/telegram/sync", "allowed_chat_ids"),
    # A confirmation is named by its token and not by the id of a row: the
    # lookup compares text and a malformed token answers like an unknown one.
    ("POST", "/v0/vnext/memories/confirm", "confirmation_id"),
}

HTTP_SCHEMA_UUIDS = {
    ("POST", "/v1/memory/operations/commit", "candidate_ids"),
}

HTTP_SCHEMA_UUIDS = {
    ("POST", "/v0/entities", "source_memory_ids"),
    ("POST", "/v0/entity-edges", "source_memory_ids"),
    ("POST", "/v0/memories/admit", "source_event_ids"),
    ("POST", "/v1/memory/operations/commit", "candidate_ids"),
}

MCP_ROW_IDS = {
    ("alice_explain", "memory_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_alice_explain_malformed_memory_id_matches_unknown",
    ("alice_memory_manage", "memory_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_alice_memory_manage_malformed_memory_id_matches_unknown",
    ("alice_memory_manage", "confirmation_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_graph_neighborhood", "target_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_vnext_open_loops", "source_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_vnext_open_loops", "project_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_open_loops", "loop_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_belief_review", "belief_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_belief_review_mcp_answers_not_found_for_malformed_and_unknown",
    ("alice_belief_state", "belief_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_mcp_tool_malformed_id_matches_unknown",
    ("alice_graph_edge_review", "edge_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_graph_edge_review_mcp_answers_not_found_for_malformed_and_unknown",
    ("alice_open_loop_review", "loop_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_mcp_tool_malformed_id_matches_unknown",
    ("alice_project_dashboard", "project_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_mcp_tool_malformed_id_matches_unknown",
    ("alice_vnext_project_dashboard", "project_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_vnext_artifact_get", "artifact_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_mcp_tool_malformed_id_matches_unknown",
    ("alice_vnext_artifact_review", "artifact_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_mcp_tool_malformed_id_matches_unknown",
    ("alice_project_update_review", "artifact_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_mcp_tool_malformed_id_matches_unknown",
    ("alice_open_loop_extract", "person_id"):
        "tests/integration/test_open_loop_person_not_found_postgres.py::test_a_real_person_in_any_accepted_spelling_makes_no_duplicate_loop",
    ("alice_vnext_undo_memory", "memory_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_memory_tool_malformed_memory_id_matches_unknown",
    ("alice_vnext_correct_memory", "memory_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_memory_tool_malformed_memory_id_matches_unknown",
    ("alice_vnext_forget_memory", "memory_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_memory_tool_malformed_memory_id_matches_unknown",
    ("alice_vnext_memory_audit", "memory_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_legacy_memory_tool_malformed_memory_id_matches_unknown",
    ("alice_open_loop_extract", "project_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_project_update_candidate", "project_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_row_id_argument_malformed_matches_unknown",
    ("alice_capture_candidates", "session_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_id_arrays_and_sessions_malformed_matches_unknown",
    ("alice_memory_mutations_generate", "session_id"):
        "tests/integration/test_malformed_id_doors_postgres.py::test_mcp_id_arrays_and_sessions_malformed_matches_unknown",
}

MCP_CLAIM_STRINGS = {
    "agent_id", "agent_run_id", "task_id", "trace_id", "confirmation_id",
}

MCP_SCHEMA_UUIDS = {
    # Refused at the argument layer: "candidate_ids must contain UUID strings".
    ("alice_memory_mutations_commit", "candidate_ids"),
}


def _http_plain_string_id_fields() -> set[tuple[str, str, str]]:
    schema = main_module.app.openapi()
    found: set[tuple[str, str, str]] = set()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                name = parameter.get("name", "")
                parameter_schema = parameter.get("schema", {})
                if name.endswith(("_id", "_ids")) and parameter_schema.get("type") == "string" and "format" not in parameter_schema:
                    found.add((method.upper(), path, name))
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
            reference = body.get("$ref", "")
            model = schema.get("components", {}).get("schemas", {}).get(reference.split("/")[-1], {}) if reference else body
            for name, field_schema in model.get("properties", {}).items():
                if not name.endswith(("_id", "_ids")):
                    continue
                if field_schema.get("type") == "string" and "format" not in field_schema:
                    found.add((method.upper(), path, name))
                elif field_schema.get("type") == "array" and field_schema.get("items", {}).get("type") == "string":
                    found.add((method.upper(), path, name))
    return found


def _mcp_plain_string_id_arguments() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS):
        for name, argument_schema in tool.get("inputSchema", {}).get("properties", {}).items():
            if not name.endswith(("_id", "_ids")):
                continue
            if argument_schema.get("type") == "string" and "format" not in argument_schema:
                found.add((tool["name"], name))
            elif argument_schema.get("type") == "array" and argument_schema.get("items", {}).get("type") == "string":
                found.add((tool["name"], name))
    return found


def test_every_http_plain_string_id_field_is_listed_with_its_kind() -> None:
    found = _http_plain_string_id_fields()
    listed = set(HTTP_ROW_IDS) | HTTP_CLAIM_STRINGS | HTTP_SCHEMA_UUIDS
    assert found == listed, {
        "unlisted": sorted(found - listed),
        "stale": sorted(listed - found),
    }


def test_every_mcp_plain_string_id_argument_is_listed_with_its_kind() -> None:
    found = _mcp_plain_string_id_arguments()
    listed = set(MCP_ROW_IDS) | MCP_SCHEMA_UUIDS | set(_tools_for_claims() and ())
    claim_arguments = {
        (tool["name"], argument)
        for tool in (*_CORE_TOOL_DEFINITIONS, *_LEGACY_TOOL_DEFINITIONS)
        for argument in MCP_CLAIM_STRINGS
        if (tool["name"], argument) in found
    }
    listed |= claim_arguments
    assert found == listed, {
        "unlisted": sorted(found - listed),
        "stale": sorted(listed - found),
    }


def _tools_for_claims() -> set[str]:
    return set()


def test_every_row_id_entry_names_a_test_that_reads_malformed_spellings() -> None:
    for key, test_name in {**HTTP_ROW_IDS, **MCP_ROW_IDS}.items():
        assert test_name.startswith("tests/integration/"), key
        assert "malformed" in test_name or "person" in test_name or "spelling" in test_name, key
