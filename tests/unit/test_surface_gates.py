from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _isolated_http_inventory(flag_value: str | None) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    if flag_value is None:
        env.pop("ALICE_LEGACY_SURFACES", None)
    else:
        env["ALICE_LEGACY_SURFACES"] = flag_value
    script = """
import json
from alicebot_api.main import LEGACY_HTTP_OPERATION_KEYS, app
from alicebot_api.openapi_operation_contracts import PERMANENTLY_REMOVED_OPENAPI_OPERATIONS

schema = app.openapi()
operations = {
    (method.upper(), path)
    for path, path_item in schema["paths"].items()
    for method in path_item
    if method in {"get", "post", "put", "patch", "delete"}
}
print(json.dumps({
    "count": len(operations),
    "legacy_count": len(operations & LEGACY_HTTP_OPERATION_KEYS),
    "removed_count": len(operations & PERMANENTLY_REMOVED_OPENAPI_OPERATIONS),
    "runtime_invoke_count": list(operations).count(("POST", "/v1/runtime/invoke")),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _isolated_proxy_execution_posture(flag_value: str | None) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    if flag_value is None:
        env.pop("ALICE_LEGACY_SURFACES", None)
    else:
        env["ALICE_LEGACY_SURFACES"] = flag_value
    script = """
import json
import sys
from alicebot_api.main import app

route_paths = set()
for route in app.router.routes:
    effective_route_contexts = getattr(route, "effective_route_contexts", None)
    route_contexts = (
        effective_route_contexts()
        if callable(effective_route_contexts)
        else (route,)
    )
    route_paths.update(
        str(route_context.path)
        for route_context in route_contexts
        if getattr(route_context, "path", None) is not None
    )

print(json.dumps({
    "module_loaded": "alicebot_api.proxy_execution" in sys.modules,
    "execute_route_mounted": "/v0/approvals/{approval_id}/execute" in route_paths,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("flag_value", [None, "", "0", "true", "yes", "on", "01", " 1"])
def test_http_legacy_surface_gate_fails_closed_for_every_non_exact_value(flag_value: str | None) -> None:
    assert _isolated_http_inventory(flag_value) == {
        "count": 184,
        "legacy_count": 0,
        "removed_count": 0,
        "runtime_invoke_count": 1,
    }


def test_http_legacy_surface_gate_mounts_exact_inventory_only_for_one() -> None:
    assert _isolated_http_inventory("1") == {
        "count": 233,
        "legacy_count": 49,
        "removed_count": 0,
        "runtime_invoke_count": 1,
    }


def test_proxy_execution_module_and_route_are_absent_from_default_process() -> None:
    assert _isolated_proxy_execution_posture(None) == {
        "module_loaded": False,
        "execute_route_mounted": False,
    }


def test_proxy_execution_route_is_mounted_but_module_stays_lazy_when_enabled() -> None:
    assert _isolated_proxy_execution_posture("1") == {
        "module_loaded": False,
        "execute_route_mounted": True,
    }


def test_http_legacy_surface_gate_cannot_expand_after_main_import() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "apps" / "api" / "src")
    env.pop("ALICE_LEGACY_SURFACES", None)
    script = """
import json
import os
from alicebot_api.main import LEGACY_HTTP_OPERATION_KEYS, app

def inventory():
    app.openapi_schema = None
    schema = app.openapi()
    operations = {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    return {
        "count": len(operations),
        "legacy_count": len(operations & LEGACY_HTTP_OPERATION_KEYS),
    }

before = inventory()
os.environ["ALICE_LEGACY_SURFACES"] = "1"
after = inventory()
print(json.dumps({"before": before, "after": after}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    expected_inventory = {"count": 184, "legacy_count": 0}
    assert json.loads(completed.stdout) == {
        "before": expected_inventory,
        "after": expected_inventory,
    }
