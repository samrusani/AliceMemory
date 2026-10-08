"""A route that runs the doctor must decide, for its caller, whether the doctor reads the vault's content.

``VNextDoctorService.run`` reads content by default, because the owner's command line and the smoke tests have no
caller to limit. A route has a caller, so every call to it from a router names ``include_content_diagnostics`` and
passes a value taken from the caller, never a literal. Without this check a new route that forgets the argument would
hand a key with a ceiling the ids of credential-bearing sources and the derived-label counts of rows it may not read.

Mutation: delete ``include_content_diagnostics=unfenced`` from the workspace route.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROUTERS = Path(__file__).resolve().parents[2] / "apps/api/src/alicebot_api/routers"


def _doctor_runs() -> list[tuple[str, int, ast.Call]]:
    runs = []
    for path in sorted(ROUTERS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Call)
                and getattr(node.func.value.func, "id", None) == "VNextDoctorService"
            ):
                runs.append((path.name, node.lineno, node))
    return runs


def test_every_router_call_to_the_doctor_chooses_content_diagnostics_for_its_caller() -> None:
    runs = _doctor_runs()
    # The workspace route and the two doctor routes. A new call site must be added here on purpose.
    assert sorted(name for name, _line, _node in runs) == ["vnext_memories.py", "vnext_memories.py", "workspaces.py"]
    for name, line, node in runs:
        value = {keyword.arg: keyword.value for keyword in node.keywords}.get("include_content_diagnostics")
        assert value is not None, f"{name}:{line} runs the doctor without choosing content diagnostics"
        assert not isinstance(value, ast.Constant), f"{name}:{line} chooses content diagnostics with a literal"
