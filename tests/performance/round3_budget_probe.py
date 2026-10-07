"""Persistent revision-isolated probe, allowing interleaved same-data samples."""
import importlib.util
import cProfile
from importlib.metadata import PackageNotFoundError, version
import io
import json
import os
from pathlib import Path
import platform
import pstats
import subprocess
import sys
import time
from uuid import UUID

EXPECTED_CAPTURE_FUNCTIONS = {
    "workspace": ("get_vnext_workspace", "_vnext_workspace_payload"),
    "dogfooding": ("get_vnext_dogfooding_dashboard", "dashboard"),
}


def profile_action(action, name, trace, context):
    """Save diagnostics separately; neither timings nor host paths enter the protocol."""
    directory = Path(trace)
    directory.mkdir(parents=True, exist_ok=True)
    profiler = cProfile.Profile()
    error = None
    try:
        profiler.enable()
        action()
    except Exception as exc:
        # Exception text can contain request data or paths; retain only its type.
        error = type(exc).__name__
    finally:
        profiler.disable()
    stats = pstats.Stats(profiler).strip_dirs()
    stats.dump_stats(str(directory / (name + ".prof")))
    captured = {key[2] for key in stats.stats}
    expected = EXPECTED_CAPTURE_FUNCTIONS[name]
    missing = sorted(set(expected) - captured)
    report = io.StringIO()
    for sort in ("cumulative", "tottime"):
        pstats.Stats(profiler, stream=report).strip_dirs().sort_stats(sort).print_stats(40)
    (directory / (name + ".txt")).write_text(report.getvalue(), encoding="utf-8")
    dependencies = {}
    for package in ("fastapi", "starlette", "anyio", "psycopg"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = "unavailable"
    receipt = {
        **context, "diagnostic": True, "action": name,
        "status": "error" if error else "incomplete" if missing else "complete",
        "error_type": error, "expected_functions": list(expected), "missing_functions": missing,
        "runtime": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                    "system": platform.system(), "machine": platform.machine(), "dependencies": dependencies},
    }
    (directory / (name + ".json")).write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    return receipt


def serve(actions, commands, output, *, trace=None, profile_context=None):
    for line in commands:
        name = line.strip()
        if name == "stop":
            break
        if name.startswith("profile:"):
            action = name.removeprefix("profile:")
            assert trace and action in EXPECTED_CAPTURE_FUNCTIONS, "invalid diagnostic command"
            receipt = profile_action(actions[action], action, trace, profile_context() if profile_context else {})
            print(json.dumps(receipt), file=output, flush=True)
            continue
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        result = actions[name]()
        measurement = {"wall": time.perf_counter() - wall_start, "cpu": time.process_time() - cpu_start}
        if name == "recall":
            measurement["memories"] = len(result["results"])
        elif name == "pack":
            measurement["memories"] = len(result["memories"])
        print(json.dumps(measurement), file=output, flush=True)


def main():
    repo, backend, location, user, profile, key = sys.argv[1:7]
    sys.path[:0] = [str(Path(repo) / "apps/api/src"), repo]
    os.environ["DATABASE_URL"] = location
    if key:
        os.environ["ALICE_AGENT_API_KEY"] = key
    else:
        os.environ.pop("ALICE_AGENT_API_KEY", None)
    os.environ["ALICE_MCP_FULL_TOOLS"] = "1"
    os.environ["ALICE_LEGACY_SURFACES"] = "1"
    from alicebot_api.mcp.registry import call_mcp_tool
    from alicebot_api.mcp.types import MCPRuntimeContext

    context = MCPRuntimeContext(database_url=location, user_id=UUID(user))
    arguments = {"query": "synthetic budget observation"}
    if profile == "keyless_agent_id":
        arguments["agent_id"] = "budget-keyless"
    actions = {"pack": lambda: call_mcp_tool(context, name="alice_context_pack", arguments=arguments),
               "recall": lambda: call_mcp_tool(context, name="alice_recall", arguments=arguments)}
    if backend == "postgres":
        from alicebot_api.config import Settings
        from alicebot_api import main as main_module
        from alicebot_api.routers import workspaces, vnext_memories
        settings = Settings(database_url=location)
        for module in (main_module, workspaces, vnext_memories):
            module.get_settings = lambda: settings
        support = Path(__file__).resolve().parents[1] / "integration/derived_labels_postgres_support.py"
        spec = importlib.util.spec_from_file_location("round3_transport", support)
        transport = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = transport
        spec.loader.exec_module(transport)

        def read(path):
            status, body, _headers = transport.invoke("GET", path, user_id=user, key=key)
            assert status == 200, (status, body)
            return body

        actions.update(workspace=lambda: read("/v0/vnext/workspace"), dogfooding=lambda: read("/v0/vnext/dogfooding"))

    def profile_context():
        receipt = json.loads(os.environ.get("ALICE_READ_PROFILE_CONTEXT", "{}"))
        receipt["revision"] = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
        return receipt

    serve(actions, sys.stdin, sys.stdout, trace=os.environ.get("ALICE_READ_PROFILE"), profile_context=profile_context)


if __name__ == "__main__":
    main()
