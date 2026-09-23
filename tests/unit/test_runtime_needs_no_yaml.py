"""Runtime code must not import PyYAML; the dev extra has it only as a test oracle.

Why this exists. On 2026-09-22 PyYAML was declared in the dev extra so the
Hermes install tests could judge config.yaml with the loader Hermes uses.
The wheel does not depend on PyYAML, so a runtime ``import yaml`` would
break ``uvx alice-memory install --host hermes`` for every user while the
dev suite, which has PyYAML, stayed green. A dev-only dependency cannot be
caught by the tests that need it.

Two guards. An AST scan fails on any yaml import under apps/api/src or
workers, including one wrapped in try/except with a fallback. A subprocess
runs the Hermes install path with ``sys.modules["yaml"] = None``, so an
unguarded import fails at import time. The helpers take the source roots
as arguments, so the same guards can be pointed at a planted copy.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (REPO_ROOT / "apps" / "api" / "src", REPO_ROOT / "workers")
_DYNAMIC_IMPORTERS = frozenset({"import_module", "__import__"})


def _imported_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""] if node.level == 0 else []
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        first = node.args[0]
        if name in _DYNAMIC_IMPORTERS and isinstance(first, ast.Constant) and isinstance(first.value, str):
            return [first.value]
    return []


def yaml_imports_under(roots: Sequence[Path]) -> list[str]:
    """Every ``yaml`` import under ``roots``, as ``path:line``."""

    offenders: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for name in _imported_names(node):
                    if name == "yaml" or name.startswith("yaml."):
                        offenders.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', 0)}")
    return offenders


def run_hermes_install_without_yaml(
    roots: Sequence[Path], home: Path, data_dir: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``alice-memory install --host hermes`` with PyYAML blocked.

    The child sets ``sys.modules["yaml"] = None`` before importing Alice, so
    any ``import yaml`` raises. It then checks that the block held, exiting
    97 if yaml could still be imported.
    """

    code = "\n".join(
        (
            "import sys",
            "sys.modules['yaml'] = None",
            "from alicebot_api.onramp import main",
            "argv = ['install', '--host', 'hermes', '--home', sys.argv[1], '--data-dir', sys.argv[2]]",
            "status = main(argv)",
            "try:",
            "    import yaml",
            "except ImportError:",
            "    pass",
            "else:",
            "    raise SystemExit(97)",
            "raise SystemExit(status)",
        )
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(root) for root in roots)
    env.pop("ALICE_TEST_REAL_HOSTS", None)
    home.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-c", code, str(home), str(data_dir)],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_no_runtime_module_imports_yaml() -> None:
    """No ``yaml`` import under apps/api/src or workers.

    Mutation: add ``import yaml`` to host_install.py, bare or inside
    try/except with a fallback. This test fails.
    """

    assert yaml_imports_under(SOURCE_ROOTS) == []


def test_ast_guard_sees_every_import_form() -> None:
    """Guards the guard: each import form the scan claims to catch is caught.

    Mutation: make _imported_names ignore one of the forms. This test fails.
    """

    forms = [
        "import yaml",
        "import yaml as y",
        "from yaml import safe_load",
        "from yaml.loader import SafeLoader",
        "try:\n    import yaml\nexcept ImportError:\n    yaml = None",
        "import importlib\nimportlib.import_module('yaml')",
        "__import__('yaml')",
    ]
    for form in forms:
        tree = ast.parse(form)
        names = [name for node in ast.walk(tree) for name in _imported_names(node)]
        assert any(name == "yaml" or name.startswith("yaml.") for name in names), form


def test_hermes_install_runs_with_yaml_blocked(tmp_path: Path) -> None:
    """The Hermes edit path works in a process where PyYAML cannot be imported.

    The seeded config makes install scan and edit a real file, not only
    create one. Mutation: import yaml at the top of host_install.py. This
    test fails.
    """

    home = tmp_path / "home"
    config = home / ".hermes" / "config.yaml"
    config.parent.mkdir(parents=True)
    original = "# kept\nmodel: gpt-4o  # default\nmcp_servers:\n  other:\n    command: node\n"
    config.write_text(original, encoding="utf-8")

    proc = run_hermes_install_without_yaml(SOURCE_ROOTS, home, tmp_path / "data")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    written = config.read_text(encoding="utf-8")
    assert written.startswith(original)
    assert written[len(original) :].startswith("  alice:\n")
