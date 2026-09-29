"""Dispatch-only trial: why does the alice-memory plugin's SessionStart hook not run?

A real Claude Code 2.1.281 run against a loopback API stub got past the login
check, and the stub uvx still logged no session-start row. This script runs the
plugin six ways, with controls, and writes one row per case to ``report.json``.
It changes nothing in Alice. It uses the standard library only.

``run`` builds a fresh temp home, marketplace, stub API and stub ``uvx`` per
case, so no case sees another case's state. ``mark`` is the command the control
hooks run: it appends one line to a marker file.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PINNED_VERSION = "2.1.281 (Claude Code)"
# Kept equal to host_install.CLAUDE_MARKETPLACE_NAME and CLAUDE_PLUGIN_ID by a unit test.
_MARKETPLACE = "alicememory"
_PLUGIN = "alice-memory"
_PLUGIN_ID = f"{_PLUGIN}@{_MARKETPLACE}"
_PROBE = "probe-plugin"
_PROBE_ID = f"{_PROBE}@{_MARKETPLACE}"
_SESSION_START_ARG = "alice-memory-session-start"
_UNSET = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_PLUGIN_ROOT",
)
_DELAY_SECONDS = 5.0
_TIMEOUT_SECONDS = 120
_DEBUG_KEYWORDS = ("sessionstart", "hook", "plugin", "alice-memory", "user_config")
_DEBUG_LINE_LIMIT = 200
_SUMMARY_DEBUG_LINES = 40
_CLIP = 300
_EVENT_LIMIT = 200
_STRING_LIMIT = 2000
_STDOUT_HEAD = 5
_STDOUT_TAIL = 2
_FILE_LIMIT = 100
_INIT_ONLY = ("--init-only",)
_STREAM = ("-p", "--output-format", "stream-json", "--verbose", "ok")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_SOURCE = _REPO_ROOT / "plugins" / "alice-memory"


def mark(marker: Path) -> int:
    """Append one line to ``marker`` and exit 0, printing nothing."""

    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    fields = payload if isinstance(payload, dict) else {}
    line = {
        "hook_event_name": fields.get("hook_event_name"),
        "source": fields.get("source"),
        "payload_keys": sorted(str(key) for key in fields),
        "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),
        "time_ns": time.time_ns(),
    }
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    return 0


class _Api:
    """Loopback stand-in for the Anthropic API. Records method and path, no headers.

    ``delay`` is how long a reply waits. The request is recorded before the wait.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []
        self.delay = 0.0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(handler, method: str) -> None:
                owner.records.append((method, handler.path))
                length = int(handler.headers.get("Content-Length") or 0)
                if length:
                    handler.rfile.read(length)
                if owner.delay > 0:
                    time.sleep(owner.delay)
                body = (
                    b'{"type":"error","error":{"type":"invalid_request_error",'
                    b'"message":"alice test stub"}}'
                )
                try:
                    handler.send_response(400)
                    handler.send_header("Content-Type", "application/json")
                    handler.send_header("Content-Length", str(len(body)))
                    handler.end_headers()
                    handler.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_POST(handler) -> None:  # noqa: N802
                handler._reply("POST")

            def do_GET(handler) -> None:  # noqa: N802
                handler._reply("GET")

            def log_message(handler, _format: str, *_args: object) -> None:  # noqa: N802
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _clip(text: str, limit: int = _CLIP) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _bound(value: object) -> object:
    """Copy JSON data with every long string clipped."""

    if isinstance(value, str):
        return _clip(value, _STRING_LIMIT)
    if isinstance(value, list):
        return [_bound(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _bound(item) for key, item in value.items()}
    return value


def _lines(raw: bytes | str | None) -> list[str]:
    if raw is None:
        return []
    text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    return text.splitlines()


def hook_events(stdout: str) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    """Stream events whose type or subtype names a hook, and the ``system/init`` event."""

    events: list[dict[str, object]] = []
    init: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if not isinstance(loaded, dict):
            continue
        kind = str(loaded.get("type", ""))
        subtype = str(loaded.get("subtype", ""))
        if kind == "system" and subtype == "init" and init is None:
            bounded = _bound(loaded)
            init = bounded if isinstance(bounded, dict) else None
        elif "hook" in f"{kind} {subtype}".lower() and len(events) < _EVENT_LIMIT:
            bounded = _bound(loaded)
            if isinstance(bounded, dict):
                events.append(bounded)
    return events, init


def hook_results(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """What each hook said when it finished: outcome, exit code and the first line of its output."""

    results: list[dict[str, object]] = []
    for event in events:
        if event.get("subtype") != "hook_response":
            continue
        text = str(event.get("output") or event.get("stderr") or event.get("stdout") or "")
        results.append(
            {
                "hook_name": event.get("hook_name"),
                "outcome": event.get("outcome"),
                "exit_code": event.get("exit_code"),
                "first_line": _clip(text.splitlines()[0], 200) if text else "",
            }
        )
    return results


def key_debug_lines(text: str, limit: int = _DEBUG_LINE_LIMIT) -> tuple[list[str], int]:
    """Debug-log lines that mention a hook, plugin or user_config term, and how many matched."""

    matched = [
        line for line in text.splitlines() if any(word in line.lower() for word in _DEBUG_KEYWORDS)
    ]
    return [_clip(line, 500) for line in matched[:limit]], len(matched)


def _read_records(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except ValueError:
            loaded = {"unparsed": _clip(line)}
        records.append(loaded if isinstance(loaded, dict) else {"unparsed": _clip(line)})
    return records


def _argv(record: dict[str, object]) -> list[str]:
    argv = record.get("argv")
    return [str(item) for item in argv] if isinstance(argv, list) else []


def _write_uvx_stub(bin_dir: Path, log_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "uvx"
    stub.write_text(
        "\n".join(
            (
                f"#!{sys.executable}",
                "import json, os, sys",
                "row = {",
                '    "argv": sys.argv[1:],',
                '    "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),',
                '    "CLAUDE_PLUGIN_OPTION_DATA_DIR": os.environ.get("CLAUDE_PLUGIN_OPTION_DATA_DIR"),',
                "}",
                f"path = {str(log_path)!r}",
                "with open(path, 'a', encoding='utf-8') as handle:",
                "    handle.write(json.dumps(row) + '\\n')",
                "",
            )
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _mark_command(marker: Path) -> tuple[str, list[str]]:
    """Exec form: the interpreter, and this script's ``mark`` command with a marker path."""

    return sys.executable, [str(Path(__file__).resolve()), "mark", str(marker)]


def _write_probe_plugin(directory: Path, marker: Path) -> None:
    """A plugin with one exec-form SessionStart hook and no ``${user_config}``."""

    (directory / ".claude-plugin").mkdir(parents=True)
    (directory / "hooks").mkdir()
    (directory / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": _PROBE,
                "version": "0.0.1",
                "description": "Writes a marker on SessionStart. A control for the trial.",
            }
        ),
        encoding="utf-8",
    )
    command, args = _mark_command(marker)
    (directory / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": command, "args": args}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


class _Sandbox:
    """One case's home, project, marketplace, stub uvx and environment."""

    def __init__(self, root: Path, api: _Api, *, probe_marker: Path | None = None) -> None:
        self.root = root
        self.home = root / "home"
        self.project = root / "project"
        self.market = root / "market"
        self.uvx_log = root / "uvx.jsonl"
        (self.home / ".claude").mkdir(parents=True)
        self.project.mkdir()
        plugins: list[dict[str, str]] = []
        shutil.copytree(_PLUGIN_SOURCE, self.market / "plugins" / _PLUGIN)
        plugins.append({"name": _PLUGIN, "source": f"./plugins/{_PLUGIN}"})
        if probe_marker is not None:
            _write_probe_plugin(self.market / "plugins" / _PROBE, probe_marker)
            plugins.append({"name": _PROBE, "source": f"./plugins/{_PROBE}"})
        (self.market / ".claude-plugin").mkdir()
        (self.market / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": _MARKETPLACE,
                    "owner": {"name": "Alice Memory"},
                    "description": "Alice Memory plugins",
                    "plugins": plugins,
                }
            ),
            encoding="utf-8",
        )
        bin_dir = root / "bin"
        _write_uvx_stub(bin_dir, self.uvx_log)
        env = os.environ.copy()
        for name in _UNSET:
            env.pop(name, None)
        env["HOME"] = str(self.home)
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["DISABLE_AUTOUPDATER"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] = "1"
        env["ANTHROPIC_BASE_URL"] = api.base_url
        env["ANTHROPIC_API_KEY"] = "alice-test-" + secrets.token_hex(8)
        self.env = env

    @property
    def settings_path(self) -> Path:
        return self.home / ".claude" / "settings.json"


class _Call:
    def __init__(self, exit_code: int | str, stdout: str, stderr: str, wall_ms: float) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.wall_ms = wall_ms


def _claude(args: list[str], sandbox: _Sandbox) -> _Call:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            ["claude", *args],
            cwd=sandbox.project,
            env=sandbox.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = "\n".join(_lines(exc.stdout if isinstance(exc.stdout, bytes) else None))
        err = "\n".join(_lines(exc.stderr if isinstance(exc.stderr, bytes) else None))
        return _Call("timeout", out, err, (time.perf_counter() - started) * 1000)
    return _Call(
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
        (time.perf_counter() - started) * 1000,
    )


def _first(text: str) -> str:
    lines = text.splitlines()
    return _clip(lines[0]) if lines else ""


def _setup_step(
    sandbox: _Sandbox, artifacts: Path, slug: str, name: str, args: list[str]
) -> tuple[dict[str, object], _Call]:
    """One setup call with ``--debug-file`` before the subcommand, retried without it once.

    The retry runs only when the CLI names the flag as an unknown option.
    """

    debug = artifacts / f"{slug}.setup-{name}.debug.log"
    call = _claude(["--debug-file", str(debug), *args], sandbox)
    rejected = False
    if call.exit_code != 0 and "unknown option" in call.stderr and "--debug-file" in call.stderr:
        rejected = True
        call = _claude(args, sandbox)
    record: dict[str, object] = {
        "step": name,
        "args": args,
        "exit_code": call.exit_code,
        "first_stderr_line": _first(call.stderr),
        "first_stdout_line": _first(call.stdout),
        "debug_file": debug.name if debug.is_file() else None,
        "debug_file_rejected": rejected,
    }
    return record, call


def _plugin_list(sandbox: _Sandbox, artifacts: Path, slug: str) -> dict[str, object]:
    record, call = _setup_step(sandbox, artifacts, slug, "list", ["plugin", "list", "--json"])
    try:
        parsed: object = json.loads(call.stdout)
    except ValueError:
        parsed = None
    record["listed"] = _bound(parsed)
    return record


def _files_under(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    found = sorted(
        item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()
    )
    return found[:_FILE_LIMIT]


def _settings_snapshot(sandbox: _Sandbox) -> object:
    try:
        return _bound(json.loads(sandbox.settings_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _setup(
    sandbox: _Sandbox,
    artifacts: Path,
    slug: str,
    installs: list[list[str]],
) -> list[dict[str, object]]:
    steps = [
        _setup_step(
            sandbox,
            artifacts,
            slug,
            "marketplace-add",
            ["plugin", "marketplace", "add", str(sandbox.market)],
        )[0]
    ]
    for index, extra in enumerate(installs):
        if steps[-1]["exit_code"] != 0:
            break
        steps.append(
            _setup_step(
                sandbox, artifacts, slug, f"install-{index + 1}", ["plugin", "install", *extra]
            )[0]
        )
    if steps[-1]["exit_code"] == 0:
        steps.append(_plugin_list(sandbox, artifacts, slug))
    return steps


def _setup_ok(steps: list[dict[str, object]]) -> bool:
    """Every install step passed. The listing is a diagnostic and may fail."""

    return all(step["exit_code"] == 0 for step in steps if step["step"] != "list") and any(
        str(step["step"]).startswith("install") for step in steps
    )


def _execute(
    sandbox: _Sandbox,
    api: _Api,
    artifacts: Path,
    *,
    label: str,
    slug: str,
    args: tuple[str, ...],
    delay: float,
    markers: dict[str, Path],
) -> dict[str, object]:
    """Run claude once from a clean log and clean markers, and record what happened."""

    sandbox.uvx_log.write_text("", encoding="utf-8")
    for path in markers.values():
        path.unlink(missing_ok=True)
    api.records.clear()
    api.delay = delay
    debug = artifacts / f"{slug}.debug.log"
    call = _claude(["--debug-file", str(debug), *args], sandbox)
    api.delay = 0.0
    events, init = hook_events(call.stdout)
    debug_text = debug.read_text(encoding="utf-8", errors="replace") if debug.is_file() else ""
    debug_lines, debug_count = key_debug_lines(debug_text)
    records = _read_records(sandbox.uvx_log)
    stdout_lines = call.stdout.splitlines()
    return {
        "label": label,
        "args": list(args),
        "debug_file": debug.name if debug.is_file() else None,
        "exit_code": call.exit_code,
        "first_stderr_line": _first(call.stderr),
        "first_stdout_line": _first(call.stdout),
        "stdout_head": [_clip(line) for line in stdout_lines[:_STDOUT_HEAD]],
        "stdout_tail": [_clip(line) for line in stdout_lines[-_STDOUT_TAIL:]],
        "wall_ms": call.wall_ms,
        "stub_delay_seconds": delay,
        "stub_requests": [[method, path] for method, path in api.records],
        "uvx_records": records,
        "plugin_hook_ran": any(_SESSION_START_ARG in _argv(record) for record in records),
        "markers": {name: path.is_file() and path.stat().st_size > 0 for name, path in markers.items()},
        "hook_events": events,
        "hook_results": hook_results(events),
        "init_event": init,
        "init_plugins": init.get("plugins") if init else None,
        "init_plugin_errors": init.get("plugin_errors") if init else None,
        "debug_lines": debug_lines,
        "debug_line_count": debug_count,
    }


def _row(
    number: int,
    name: str,
    shows: str,
    sandbox: _Sandbox,
    setup: list[dict[str, object]],
    runs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "case": number,
        "name": name,
        "shows": shows,
        "setup": setup,
        "setup_ok": _setup_ok(setup),
        "settings_after_setup": _settings_snapshot(sandbox),
        "claude_files": _files_under(sandbox.home / ".claude"),
        "runs": runs,
    }


def _failed_row(number: int, name: str, shows: str, sandbox: _Sandbox, setup: list[dict[str, object]]) -> dict[str, object]:
    return _row(number, name, shows, sandbox, setup, [])


def _install_our_plugin() -> list[list[str]]:
    return [[_PLUGIN_ID]]


def _case_one(artifacts: Path, temp: Path, api: _Api, delay: float) -> dict[str, object]:
    sandbox = _Sandbox(temp / "case-1", api)
    setup = _setup(sandbox, artifacts, "case1", _install_our_plugin())
    name = "init-only"
    shows = "claude --init-only runs Setup and SessionStart hooks and makes no model call"
    if not _setup_ok(setup):
        return _failed_row(1, name, shows, sandbox, setup)
    run = _execute(
        sandbox, api, artifacts, label="1", slug="case1-init-only", args=_INIT_ONLY, delay=0.0, markers={}
    )
    return _row(1, name, shows, sandbox, setup, [run])


def _case_two(artifacts: Path, temp: Path, api: _Api, delay: float) -> dict[str, object]:
    sandbox = _Sandbox(temp / "case-2", api)
    setup = _setup(sandbox, artifacts, "case2", _install_our_plugin())
    name = "stream-json"
    shows = "hook events and system/init from a -p run against the stub"
    if not _setup_ok(setup):
        return _failed_row(2, name, shows, sandbox, setup)
    run = _execute(
        sandbox, api, artifacts, label="2", slug="case2-stream", args=_STREAM, delay=0.0, markers={}
    )
    return _row(2, name, shows, sandbox, setup, [run])


def _case_three(artifacts: Path, temp: Path, api: _Api, delay: float) -> dict[str, object]:
    """Control A: a user SessionStart hook in settings.json."""

    sandbox = _Sandbox(temp / "case-3", api)
    marker = artifacts / "case3-control-a.marker"
    setup = _setup(sandbox, artifacts, "case3", _install_our_plugin())
    name = "control-a-user-hook"
    shows = "whether SessionStart fires at all in this setup (a user settings hook)"
    if not _setup_ok(setup):
        return _failed_row(3, name, shows, sandbox, setup)
    try:
        settings = json.loads(sandbox.settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    command, args = _mark_command(marker)
    settings["hooks"] = {
        "SessionStart": [{"hooks": [{"type": "command", "command": command, "args": args}]}]
    }
    sandbox.settings_path.write_text(json.dumps(settings), encoding="utf-8")
    run = _execute(
        sandbox,
        api,
        artifacts,
        label="3",
        slug="case3-control-a",
        args=_STREAM,
        delay=0.0,
        markers={"control-a": marker},
    )
    return _row(3, name, shows, sandbox, setup, [run])


def _case_four(artifacts: Path, temp: Path, api: _Api, delay: float) -> dict[str, object]:
    """Control B: a second plugin with a plain SessionStart hook."""

    marker = artifacts / "case4-control-b.marker"
    sandbox = _Sandbox(temp / "case-4", api, probe_marker=marker)
    setup = _setup(sandbox, artifacts, "case4", [[_PROBE_ID], [_PLUGIN_ID]])
    name = "control-b-probe-plugin"
    shows = "whether plugin SessionStart hooks fire at all (a probe plugin with no user_config)"
    if not _setup_ok(setup):
        return _failed_row(4, name, shows, sandbox, setup)
    run = _execute(
        sandbox,
        api,
        artifacts,
        label="4",
        slug="case4-control-b",
        args=_STREAM,
        delay=0.0,
        markers={"control-b": marker},
    )
    return _row(4, name, shows, sandbox, setup, [run])


def _case_five(artifacts: Path, temp: Path, api: _Api, delay: float) -> dict[str, object]:
    """Control C: the option is set at install time."""

    sandbox = _Sandbox(temp / "case-5", api)
    vault = temp / "case-5" / "vault"
    vault.mkdir()
    setup = _setup(
        sandbox, artifacts, "case5", [[_PLUGIN_ID, "--config", f"data_dir={vault}"]]
    )
    name = "control-c-data-dir-set"
    shows = "whether an unset data_dir option blocks the hook (data_dir set with --config)"
    if not _setup_ok(setup):
        return _failed_row(5, name, shows, sandbox, setup)
    run = _execute(
        sandbox, api, artifacts, label="5", slug="case5-control-c", args=_STREAM, delay=0.0, markers={}
    )
    return _row(5, name, shows, sandbox, setup, [run])


def _case_six(artifacts: Path, temp: Path, api: _Api, delay: float) -> dict[str, object]:
    """Cases 1 and 2 with the stub holding its reply."""

    sandbox = _Sandbox(temp / "case-6", api)
    setup = _setup(sandbox, artifacts, "case6", _install_our_plugin())
    name = "delayed-stub"
    shows = f"cases 1 and 2 with the stub delaying every reply {delay:g} seconds"
    if not _setup_ok(setup):
        return _failed_row(6, name, shows, sandbox, setup)
    runs = [
        _execute(
            sandbox,
            api,
            artifacts,
            label="6a",
            slug="case6a-delay-init-only",
            args=_INIT_ONLY,
            delay=delay,
            markers={},
        ),
        _execute(
            sandbox,
            api,
            artifacts,
            label="6b",
            slug="case6b-delay-stream",
            args=_STREAM,
            delay=delay,
            markers={},
        ),
    ]
    return _row(6, name, shows, sandbox, setup, runs)


_CASES = (_case_one, _case_two, _case_three, _case_four, _case_five, _case_six)


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _event_counts(events: object) -> str:
    if not isinstance(events, list) or not events:
        return "none"
    counts: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        key = f"{event.get('type', '')}/{event.get('subtype', '')}"
        counts[key] = counts.get(key, 0) + 1
    return "<br>".join(f"{key} x{count}" for key, count in counts.items())


def _init_cell(run: dict[str, object]) -> str:
    if run.get("init_event") is None:
        return "no init event"
    plugins = run.get("init_plugins")
    names: list[str] = []
    if isinstance(plugins, list):
        for item in plugins:
            names.append(str(item.get("name")) if isinstance(item, dict) else str(item))
    errors = run.get("init_plugin_errors")
    error_text = str(len(errors)) if isinstance(errors, list) else "no plugin_errors field"
    return _cell(f"plugins: {', '.join(names) or 'none'}; errors: {error_text}")


def _results_cell(run: dict[str, object]) -> str:
    results = run.get("hook_results")
    if not isinstance(results, list) or not results:
        return "none"
    cells = []
    for item in results:
        if not isinstance(item, dict):
            continue
        detail = f": {item.get('first_line')}" if item.get("first_line") else ""
        cells.append(_cell(f"{item.get('outcome')} (exit {item.get('exit_code')}){detail}"))
    return "<br>".join(cells)


def _uvx_cell(run: dict[str, object]) -> str:
    records = run.get("uvx_records")
    if not isinstance(records, list) or not records:
        return "none"
    return "<br>".join(_cell(_clip("uvx " + " ".join(_argv(item)), 160)) for item in records if isinstance(item, dict))


def _requests_cell(run: dict[str, object]) -> str:
    requests = run.get("stub_requests")
    if not isinstance(requests, list) or not requests:
        return "none"
    return "<br>".join(_cell(f"{item[0]} {item[1]}") for item in requests)


def _markers_cell(run: dict[str, object]) -> str:
    markers = run.get("markers")
    if not isinstance(markers, dict) or not markers:
        return "-"
    return "<br>".join(f"{name}: {'yes' if present else 'no'}" for name, present in markers.items())


def _headline(rows: list[dict[str, object]]) -> str:
    ran: list[str] = []
    missed: list[str] = []
    controls: dict[str, bool] = {}
    for row in rows:
        runs = row.get("runs")
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, dict):
                continue
            markers = run.get("markers")
            if isinstance(markers, dict):
                controls.update({str(key): bool(value) for key, value in markers.items()})
            (ran if run.get("plugin_hook_ran") else missed).append(str(run.get("label")))
    parts = [
        "alice-memory session-start reached uvx in runs: " + (", ".join(ran) or "none"),
        "not in runs: " + (", ".join(missed) or "none"),
    ]
    for name in ("control-a", "control-b"):
        if name in controls:
            parts.append(f"{name} {'fired' if controls[name] else 'did not fire'}")
    return "; ".join(parts) + "."


def render_summary(report: dict[str, object]) -> str:
    rows = report.get("rows")
    rows = rows if isinstance(rows, list) else []
    lines = [
        str(report.get("headline", "")),
        "",
        f"claude {report.get('claude_version')}. One line per claude run; case 6 has two runs.",
        "",
        "| Case | Command | Exit | First stderr line | Stub requests | uvx records | Markers | Hook events | Hook results | Init |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        runs = row.get("runs")
        if not isinstance(runs, list) or not runs:
            lines.append(
                f"| {row.get('case')} | setup failed | - | - | - | - | - | - | - | - |"
            )
            continue
        for run in runs:
            if not isinstance(run, dict):
                continue
            command = " ".join(str(item) for item in run.get("args", []))
            lines.append(
                "| "
                + " | ".join(
                    (
                        _cell(run.get("label")),
                        _cell(f"claude {command}"),
                        _cell(run.get("exit_code")),
                        _cell(run.get("first_stderr_line") or "(empty)"),
                        _requests_cell(run),
                        _uvx_cell(run),
                        _markers_cell(run),
                        _event_counts(run.get("hook_events")),
                        _results_cell(run),
                        _init_cell(run),
                    )
                )
                + " |"
            )
    lines.append("")
    for row in rows:
        if not isinstance(row, dict):
            continue
        failed = [
            step
            for step in row.get("setup", [])
            if isinstance(step, dict) and step.get("exit_code") != 0 and step.get("step") != "list"
        ]
        for step in failed:
            lines.append(
                f"Case {row.get('case')} setup step {step.get('step')} exited {step.get('exit_code')}: "
                f"{_cell(step.get('first_stderr_line'))}"
            )
        for run in row.get("runs", []):
            if not isinstance(run, dict):
                continue
            debug_lines = run.get("debug_lines")
            shown = debug_lines[:_SUMMARY_DEBUG_LINES] if isinstance(debug_lines, list) else []
            lines.append(
                f"<details><summary>Run {run.get('label')}: {run.get('debug_line_count')} "
                f"key debug lines (first {len(shown)})</summary>"
            )
            lines.append("")
            lines.append("```")
            lines.extend(_clip(str(line), _CLIP) for line in shown)
            lines.append("```")
            lines.append("</details>")
            lines.append("")
    return "\n".join(lines) + "\n"


def run(artifacts: Path, temp: Path, delay_seconds: float = _DELAY_SECONDS) -> int:
    """Run the six cases against the pinned claude and write report.json."""

    if artifacts.exists() and (not artifacts.is_dir() or any(artifacts.iterdir())):
        print("refusing a non-empty artifacts directory", file=sys.stderr)
        return 1
    artifacts.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=False)
    (artifacts / "claude-version.txt").write_text(version.stdout, encoding="utf-8")
    version_text = version.stdout.strip()
    if version_text != _PINNED_VERSION:
        print(f"claude is not the pinned version: {version_text!r}", file=sys.stderr)
        return 1
    help_run = subprocess.run(["claude", "--help"], capture_output=True, text=True, check=False)
    (artifacts / "claude-help.txt").write_text(help_run.stdout, encoding="utf-8")
    api = _Api()
    try:
        rows = [case(artifacts, temp, api, delay_seconds) for case in _CASES]
    finally:
        api.close()
    report: dict[str, object] = {
        "headline": _headline(rows),
        "claude_version": version_text,
        "python": sys.version.split()[0],
        "delay_seconds": delay_seconds,
        "rows": rows,
    }
    (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(render_summary(report))
    broken = [row["case"] for row in rows if not row["setup_ok"]]
    if broken:
        print(f"setup failed for cases {broken}; see report.json", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "mark":
        return mark(Path(args[1]))
    if len(args) == 3 and args[0] == "run":
        return run(Path(args[1]), Path(args[2]))
    print("usage: real_host_plugin_hook_trial.py run <artifacts> <temp>", file=sys.stderr)
    print("       real_host_plugin_hook_trial.py mark <marker>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
