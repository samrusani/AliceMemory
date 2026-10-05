from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.util
import logging
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import ForwardRef, get_type_hints

import pytest

import alicebot_api.cli as cli_package


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "apps" / "api" / "src"
CLI_FACADE = Path(cli_package.__file__).resolve()
CLI_PACKAGE = CLI_FACADE.parent
EXPECTED_MODULES = {
    "__init__.py",
    "__main__.py",
    "agents.py",
    "arguments.py",
    "automation.py",
    "capture.py",
    "constants.py",
    "context.py",
    "continuity.py",
    "errors.py",
    "evals.py",
    "memories.py",
    "labels.py",
    "models.py",
    "parser.py",
    "runner.py",
    "scheduler.py",
    "shared.py",
    "smokes.py",
}
# Re-measured under each flag state after adding `alicebot vnext memories
# quarantine`, the operator surface for the memory promotion incident
# control, and again after adding `--max-file-mib` to `alicebot vnext sources
# import-markdown` and `import-chatgpt` (two more actions, no new command),
# and again after adding it to `vnext sources capture-file`, `vnext connectors
# browser-clipper capture` and `vnext agents ingest-output` (three more
# actions, no new command).
# The owner label check and repair commands add three parsers, four actions,
# two leaves and two handlers under both flag states.
# Both keys are updated together: leaving one at its old value is how the
# other half silently rots.
EXPECTED_PARSER_RECEIPTS = {
    False: (162, 739, 124, 120),
    True: (166, 772, 127, 123),
}
EXPECTED_PUBLIC_NAME_COUNT = 270
EXPECTED_PUBLIC_NAMES_SHA256 = "8d97ffb088d5d8dea239c81589e9c109b81f7dc50b916d7bfb593ce13acae5fa"
CARRIER_ATTRIBUTE_NAMES = {
    "agents",
    "arguments",
    "automation",
    "capture",
    "constants",
    "context",
    "continuity",
    "errors",
    "evals",
    "memories",
    "labels",
    "models",
    "parser",
    "runner",
    "scheduler",
    "shared",
    "smokes",
}
FORBIDDEN_ONRAMPS = {
    "alicebot_api.cli",
    "alicebot_api.main",
    "alicebot_api.mcp_tools",
    "alicebot_api.onramp",
}
ALLOWED_COMPATIBILITY_FACADE_IMPORTS = {
    "memories.py": {"alicebot_api.mcp_tools"},
}


def _walk_parsers(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    rows = [(path, parser)]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for command, child in action.choices.items():
                rows.extend(_walk_parsers(child, (*path, command)))
    return rows


def _module_name(path: Path) -> str:
    if path.name == "__init__.py":
        return "alicebot_api.cli"
    return f"alicebot_api.cli.{path.stem}"


def _resolved_imports(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    current_module = _module_name(path)
    package = "alicebot_api.cli" if path.name == "__init__.py" else current_module.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            relative_name = "." * node.level + (node.module or "")
            base = importlib.util.resolve_name(relative_name, package)
        else:
            base = node.module or ""
        imported.add(base)
        if not node.module:
            imported.update(f"{base}.{alias.name}" for alias in node.names)
        elif base in {"alicebot_api", "alicebot_api.cli"}:
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def _assert_acyclic(graph: dict[str, set[str]]) -> None:
    active: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, chain: tuple[str, ...]) -> None:
        if module in active:
            raise AssertionError("CLI carrier import cycle: " + " -> ".join((*chain, module)))
        if module in visited:
            return
        active.add(module)
        for dependency in sorted(graph[module]):
            visit(dependency, (*chain, module))
        active.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module, ())


def test_cli_is_a_bounded_package_with_a_thin_compatibility_facade() -> None:
    assert CLI_FACADE.name == "__init__.py"
    assert CLI_PACKAGE.name == "cli"
    assert not (CLI_PACKAGE.parent / "cli.py").exists()
    module_paths = sorted(CLI_PACKAGE.glob("*.py"))
    assert {path.name for path in module_paths} == EXPECTED_MODULES
    for path in module_paths:
        assert len(path.read_text(encoding="utf-8").splitlines()) < 4_000, path.name
    assert len(CLI_FACADE.read_text(encoding="utf-8").splitlines()) < 200

    parser_module = importlib.import_module("alicebot_api.cli.parser")
    runner_module = importlib.import_module("alicebot_api.cli.runner")
    assert cli_package.__all__ == ["build_parser", "main"]
    assert cli_package.build_parser is parser_module.build_parser
    assert cli_package.main is runner_module.main
    assert cli_package.build_parser.__module__ == "alicebot_api.cli"
    assert cli_package.main.__module__ == "alicebot_api.cli"
    assert Path(cli_package.build_parser.__code__.co_filename).resolve() == CLI_PACKAGE / "parser.py"
    assert Path(cli_package.main.__code__.co_filename).resolve() == CLI_PACKAGE / "runner.py"

    public_names = [name for name in vars(cli_package) if not name.startswith("_")]
    assert len(public_names) == EXPECTED_PUBLIC_NAME_COUNT
    assert hashlib.sha256("\n".join(public_names).encode()).hexdigest() == EXPECTED_PUBLIC_NAMES_SHA256
    assert CARRIER_ATTRIBUTE_NAMES.isdisjoint(vars(cli_package))


def test_cli_carriers_are_acyclic_and_do_not_import_application_onramps() -> None:
    module_paths = sorted(CLI_PACKAGE.glob("*.py"))
    module_names = {_module_name(path) for path in module_paths}
    graph: dict[str, set[str]] = {}
    compatibility_facade_imports: dict[str, set[str]] = {}
    for path in module_paths:
        module_name = _module_name(path)
        imported = _resolved_imports(path)
        allowed_facades = ALLOWED_COMPATIBILITY_FACADE_IMPORTS.get(path.name, set())
        observed_facades = imported.intersection({"alicebot_api.mcp_tools"})
        if observed_facades:
            compatibility_facade_imports[path.name] = observed_facades
        prohibited = {
            name
            for name in imported
            if name in FORBIDDEN_ONRAMPS
            and name not in allowed_facades
            and not (path.name == "__init__.py" and name == "alicebot_api.cli")
        }
        assert prohibited == set(), path.name
        graph[module_name] = imported.intersection(module_names) - {module_name}
    # This one facade import is intentional: the old CLI import order applies
    # mcp_tools' compatibility metadata to redact_memory_flow before use.
    assert compatibility_facade_imports == ALLOWED_COMPATIBILITY_FACADE_IMPORTS
    _assert_acyclic(graph)


def test_cli_logger_and_repository_relative_defaults_keep_their_old_identity() -> None:
    constants = importlib.import_module("alicebot_api.cli.constants")
    logger_names: set[str] = set()
    for filename in sorted(EXPECTED_MODULES):
        module_name = "alicebot_api.cli" if filename == "__init__.py" else f"alicebot_api.cli.{Path(filename).stem}"
        module = importlib.import_module(module_name)
        logger_names.update(value.name for value in vars(module).values() if isinstance(value, logging.Logger))
    assert logger_names == {"alicebot_api.cli"}

    # The package adds one directory level. This is the root selected by the
    # old sibling cli.py via Path(__file__).resolve().parents[4].
    compatibility_root = CLI_FACADE.parents[5]
    assert constants.DEFAULT_MAINTENANCE_REPORT_PATH == (
        compatibility_root / "artifacts" / "ops" / "maintenance_status_latest.json"
    )
    assert constants.DEFAULT_VNEXT_DEMO_DATASET_PATH == (
        compatibility_root / "fixtures" / "vnext" / "demo_dataset.json"
    )


def test_cli_typed_dict_annotation_metadata_keeps_the_facade_identity() -> None:
    annotations = cli_package.ModelGenerationKwargs.__annotations__
    assert tuple(annotations) == (
        "generation_mode",
        "model_route_mode",
        "model_provider",
        "model",
        "model_temperature",
        "allow_cloud_private",
    )
    assert all(isinstance(annotation, ForwardRef) for annotation in annotations.values())
    assert {
        annotation.__forward_module__ for annotation in annotations.values() if isinstance(annotation, ForwardRef)
    } == {"alicebot_api.cli"}
    assert get_type_hints(cli_package.ModelGenerationKwargs) == {
        "generation_mode": str,
        "model_route_mode": str | None,
        "model_provider": str | None,
        "model": str | None,
        "model_temperature": float,
        "allow_cloud_private": bool,
    }


@pytest.mark.parametrize("legacy_surfaces", [False, True])
def test_cli_parser_contract_is_byte_stable_after_the_move(
    monkeypatch: pytest.MonkeyPatch,
    legacy_surfaces: bool,
) -> None:
    if legacy_surfaces:
        monkeypatch.setenv("ALICE_LEGACY_SURFACES", "1")
    else:
        monkeypatch.delenv("ALICE_LEGACY_SURFACES", raising=False)
    parser = cli_package.build_parser()
    rows = _walk_parsers(parser)
    leaves = [
        command_parser
        for _path, command_parser in rows
        if not any(isinstance(action, argparse._SubParsersAction) for action in command_parser._actions)
    ]
    # A byte digest of the rendered parser manifest is not portable: distinct
    # values were observed on CPython 3.12.13, 3.13.14, and 3.14.6 while every
    # structural count matched, so the structural counts below are the parser
    # contract asserted on all supported interpreters.
    counts = (
        len(rows),
        sum(len(command_parser._actions) for _path, command_parser in rows),
        len(leaves),
        len({getattr(command_parser.get_default("handler"), "__name__", None) for command_parser in leaves}),
    )
    assert counts == EXPECTED_PARSER_RECEIPTS[legacy_surfaces]


def test_cli_module_execution_and_project_entrypoints_are_unchanged() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {
        "alice": "alicebot_api.cli:main",
        "alice-memory": "alicebot_api.onramp:main",
        "alice-memory-session-start": "alicebot_api.session_start_hook:main",
        "alicebot": "alicebot_api.cli:main",
        "alicebot-mcp": "alicebot_api.mcp_server:main",
    }

    env = dict(os.environ)
    if env.get("ALICE_TEST_INSTALLED_WHEEL") == "1":
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = str(SOURCE_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "alicebot_api.cli", "ignored-by-design"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
