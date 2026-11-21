"""Public schemas and parsers must classify every explicit sensitivity input."""

import argparse

from alicebot_api.cli.parser import build_parser
from alicebot_api.main import app
from alicebot_api.mcp.registry import list_mcp_tools
from alicebot_api.onramp import build_parser as onramp_parser
from tests.sensitivity_intakes import CHANGE, CLI, CREATE, FAN_OUT, HTTP, MCP, OWNER, READ


FIELDS = {"sensitivity", "default_sensitivity"}


def _schema_fields(schema, definitions, prefix="", refs=()):
    if isinstance(schema, list):
        for child in schema:
            yield from _schema_fields(child, definitions, prefix, refs)
    if not isinstance(schema, dict):
        return
    if (ref := schema.get("$ref")) is not None and ref not in refs:
        yield from _schema_fields(definitions[ref.rsplit("/", 1)[-1]], definitions, prefix, (*refs, ref))
    for name, child in schema.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        if name in FIELDS:
            yield path
        yield from _schema_fields(child, definitions, path, refs)
    for key in ("items", "oneOf", "anyOf", "allOf", "additionalProperties"):
        yield from _schema_fields(schema.get(key), definitions, prefix, refs)


def _cli_fields(parser, path):
    for action in parser._actions:
        if "--sensitivity" in action.option_strings:
            yield path
        if isinstance(action, argparse._SubParsersAction):
            for command, child in action.choices.items():
                yield from _cli_fields(child, f"{path} {command}")


def test_every_public_sensitivity_input_is_classified(monkeypatch):
    """Guard edit: remove the queue create entry from the HTTP registry."""
    for flag in ("ALICE_LEGACY_SURFACES", "ALICE_MCP_LEGACY_TOOLS", "ALICE_MCP_FULL_TOOLS"):
        monkeypatch.setenv(flag, "1")
    monkeypatch.delenv("ALICE_AGENT_API_KEY", raising=False)
    monkeypatch.setattr(app, "openapi_schema", None)
    spec = app.openapi()
    definitions = spec["components"]["schemas"]
    http = set()
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in {"post", "put", "patch", "delete"}:
                continue
            for media in operation.get("requestBody", {}).get("content", {}).values():
                http.update((method.upper(), path, name) for name in _schema_fields(media["schema"], definitions))
            http.update(
                (method.upper(), path, param["name"]) for param in operation.get("parameters", [])
                if param.get("name") in FIELDS
            )
    mcp = {
        (tool["name"], name)
        for tool in list_mcp_tools()
        for name in _schema_fields(tool["inputSchema"], tool["inputSchema"].get("$defs", {}))
    }
    cli = set(_cli_fields(build_parser(), "alicebot")) | set(_cli_fields(onramp_parser(), "alice-memory"))
    for observed, registry in ((http, HTTP), (mcp, MCP), (cli, CLI)):
        assert observed == set(registry), {"unclassified": observed - set(registry), "gone": set(registry) - observed}
    classes = {CREATE, FAN_OUT, CHANGE, OWNER, READ}
    assert ({entry.kind for entry in HTTP.values()} | set(MCP.values()) | set(CLI.values())) == classes


def test_project_create_advertises_the_optional_authorization_header():
    """Guard edit: rename the project creation header in its generated schema."""
    operation = app.openapi()["paths"]["/v0/vnext/projects"]["post"]
    headers = {param["name"]: param for param in operation["parameters"] if param["in"] == "header"}
    assert headers["authorization"]["required"] is False
