"""The dispatch-only plugin hook trial records one row per case and never judges the hook.

A fake ``claude`` on PATH stands in for the pinned CLI. It installs plugins from
the trial's marketplace, fires SessionStart hooks from settings.json and from
installed plugins, posts to the trial's API stub, and streams JSON events. These
tests read the rows the trial writes. They do not call GitHub or the real claude.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "real_host_plugin_hook_trial.py"
PLUGIN_JSON = ROOT / "plugins" / "alice-memory" / ".claude-plugin" / "plugin.json"
_PINNED = "2.1.281 (Claude Code)"
STREAM_ARGS = ["-p", "--output-format", "stream-json", "--verbose", "ok"]
# Names the fake claude records from its own environment on every call.
WATCHED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_PLUGIN_ROOT",
    "DISABLE_AUTOUPDATER",
    "HOME",
    "PATH",
)
# What the outer shell might hold. The trial has to keep every one of these away from claude.
OUTER_ENV = {
    "ANTHROPIC_API_KEY": "outer-key-sentinel",
    "ANTHROPIC_AUTH_TOKEN": "outer-auth-sentinel",
    "CLAUDE_CODE_OAUTH_TOKEN": "outer-oauth-sentinel",
    "CLAUDE_CONFIG_DIR": "/outer/claude-config",
    "CLAUDE_PLUGIN_ROOT": "/outer/plugin-root",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0",
    "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "0",
    "DISABLE_AUTOUPDATER": "0",
}

_FAKE_CLAUDE = r'''#!__PYTHON__
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

original = sys.argv[1:]
WATCHED = __WATCHED__
with open("__CALLS__", "a", encoding="utf-8") as handle:
    row = {"argv": original, "home": os.environ.get("HOME")}
    row["env"] = {name: os.environ.get(name) for name in WATCHED}
    handle.write(json.dumps(row) + "\n")
argv = list(original)
debug = None
if "--debug-file" in argv:
    at = argv.index("--debug-file")
    debug = argv[at + 1]
    del argv[at : at + 2]
mode = os.environ.get("FAKE_MODE", "")
if argv == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "2.1.281 (Claude Code)"))
    raise SystemExit(0)
if argv == ["--help"]:
    print("fake help: --debug-file <path> --init-only")
    raise SystemExit(0)
reject_on = [item for item in os.environ.get("FAKE_REJECT_DEBUG_ON", "").split(",") if item]
rejecting = os.environ.get("FAKE_REJECT_DEBUG") == "1" or bool(argv[1:2] and argv[1] in reject_on)
if rejecting and debug is not None and argv[:1] == ["plugin"]:
    print("error: unknown option '--debug-file'", file=sys.stderr)
    raise SystemExit(1)
if os.environ.get("FAKE_PLUGIN_STDERR") and argv[:1] == ["plugin"]:
    print(os.environ["FAKE_PLUGIN_STDERR"], file=sys.stderr)
    raise SystemExit(1)
for env_name, content in (("FAKE_TOUCH_EMPTY", ""), ("FAKE_TOUCH_FILLED", "written\n")):
    if os.environ.get(env_name) and argv[:1] != ["plugin"]:
        Path(os.environ[env_name]).write_text(content)
home = Path(os.environ["HOME"])
state_path = home / ".claude" / "fake-state.json"
state = json.loads(state_path.read_text()) if state_path.is_file() else {"market": None, "installed": {}}
settings_path = home / ".claude" / "settings.json"


def save() -> None:
    state_path.write_text(json.dumps(state))


if argv[:3] == ["plugin", "marketplace", "add"]:
    state["market"] = argv[3]
    save()
    raise SystemExit(0)
if argv[:2] == ["plugin", "install"]:
    plugin_id = argv[2]
    name = plugin_id.split("@")[0]
    if os.environ.get("FAKE_INSTALL_FAIL") == name:
        print("install exploded", file=sys.stderr)
        raise SystemExit(1)
    config = {}
    if "--config" in argv:
        key, _, value = argv[argv.index("--config") + 1].partition("=")
        config[key] = value
    market = Path(state["market"])
    listing = json.loads((market / ".claude-plugin" / "marketplace.json").read_text())
    entry = [item for item in listing["plugins"] if item["name"] == name][0]
    target = home / ".claude" / "plugins" / "cache" / name
    shutil.copytree(market / entry["source"], target, dirs_exist_ok=True)
    state["installed"][name] = {"dir": str(target), "config": config}
    save()
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    settings.setdefault("enabledPlugins", {})[plugin_id] = True
    settings_path.write_text(json.dumps(settings))
    raise SystemExit(0)
if argv[:2] == ["plugin", "list"]:
    if os.environ.get("FAKE_LIST_FAIL") == "1":
        print("list exploded", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps([{"id": name + "@alicememory", "enabled": True} for name in state["installed"]]))
    raise SystemExit(0)

printing = "-p" in argv
if not printing and "--init-only" not in argv:
    raise SystemExit(2)
stream = "stream-json" in argv
UNSET = 'Failed to run: Plugin option "data_dir" isn\'t set. Open /plugin manage to configure it, or check that the plugin\'s userConfig schema declares "data_dir".'


def substitute(arg, options):
    for key, value in options.items():
        arg = arg.replace("${user_config." + key + "}", str(value))
    return arg


hooks = []
servers = []
if mode != "no-plugin-hooks":
    for name, info in state["installed"].items():
        directory = Path(info["dir"])
        plugin = json.loads((directory / ".claude-plugin" / "plugin.json").read_text())
        defaults = {key: value.get("default") for key, value in plugin.get("userConfig", {}).items()}
        options = {**defaults, **info["config"]}
        hooks_file = directory / "hooks" / "hooks.json"
        if hooks_file.is_file():
            for group in json.loads(hooks_file.read_text()).get("hooks", {}).get("SessionStart", []):
                for handler in group["hooks"]:
                    blocked = mode == "unset-option" and name == "alice-memory" and "data_dir" not in info["config"]
                    args = [substitute(arg, options) for arg in handler.get("args", [])]
                    extra = {"CLAUDE_PLUGIN_ROOT": str(directory)}
                    extra.update({"CLAUDE_PLUGIN_OPTION_" + key.upper(): str(value) for key, value in info["config"].items()})
                    hooks.append((handler["command"], args, extra, blocked))
        mcp_file = directory / ".mcp.json"
        if mcp_file.is_file() and printing:
            for server in json.loads(mcp_file.read_text()).get("mcpServers", {}).values():
                servers.append((server["command"], [substitute(arg, options) for arg in server.get("args", [])]))
settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
for group in settings.get("hooks", {}).get("SessionStart", []):
    for handler in group["hooks"]:
        hooks.append((handler["command"], handler.get("args", []), {}, False))
events = []
for command, args, extra, blocked in hooks:
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"}).encode()
    events.append({"type": "system", "subtype": "hook_started", "hook_event": "SessionStart", "hook_name": "SessionStart:startup"})
    if blocked:
        output, code = UNSET, 1
    else:
        done = subprocess.run(
            [command, *args], input=payload, env={**os.environ, **extra}, capture_output=True, check=False
        )
        output, code = (done.stdout + done.stderr).decode(), done.returncode
    events.append(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "SessionStart",
            "hook_name": "SessionStart:startup",
            "output": output,
            "exit_code": code,
            "outcome": "success" if code == 0 else "error",
        }
    )
for command, args in servers:
    subprocess.run([command, *args], stdin=subprocess.DEVNULL, capture_output=True, check=False)
if debug:
    lines = ["[DEBUG] starting", "[DEBUG] unrelated network line"]
    lines += ["[DEBUG] loaded plugin " + name for name in state["installed"]]
    lines += ["[DEBUG] SessionStart hooks matched: " + str(len(hooks))]
    lines += ["[DEBUG] hook line " + str(index) for index in range(int(os.environ.get("FAKE_DEBUG_LINES", "0")))]
    Path(debug).write_text("\n".join(lines) + "\n")
if stream:
    for event in events:
        print(json.dumps(event))
    print(json.dumps({"type": "system", "subtype": "api_retry"}))
    print(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "plugins": [{"name": name, "path": info["dir"]} for name, info in state["installed"].items()],
                "plugin_errors": [],
            }
        )
    )
if not printing:
    raise SystemExit(0)
if mode == "hang":
    time.sleep(60)
request = urllib.request.Request(
    os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true", data=b"{}", method="POST"
)
try:
    urllib.request.urlopen(request, timeout=30)
except urllib.error.HTTPError:
    pass
if stream:
    print(json.dumps({"type": "result", "is_error": True}))
print("API Error: 400 alice|test stub", file=sys.stderr)
raise SystemExit(1)
'''


def _load_trial():
    spec = importlib.util.spec_from_file_location("real_host_plugin_hook_trial", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Path:
    """Put the fake claude first on PATH. Returns the file that logs every call."""

    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    calls = tmp_path / "calls.jsonl"
    stub = bindir / "claude"
    stub.write_text(
        _FAKE_CLAUDE.replace("__PYTHON__", sys.executable)
        .replace("__CALLS__", str(calls))
        .replace("__WATCHED__", repr(WATCHED_ENV)),
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + ":" + os.environ.get("PATH", ""))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return calls


def _calls(calls: Path) -> list[dict]:
    if not calls.is_file():
        return []
    return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines() if line]


class _Trial:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay: float = 0.3, **env: str) -> None:
        self.module = _load_trial()
        self.calls_path = _fake(tmp_path, monkeypatch, **env)
        self.artifacts = tmp_path / "artifacts"
        self.temp = tmp_path / "temp"
        self.summary = tmp_path / "summary.md"
        self.summary.write_text("earlier step\n", encoding="utf-8")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(self.summary))
        self.delay = delay
        self.code = self.module.run(self.artifacts, self.temp, delay_seconds=delay)
        report = self.artifacts / "report.json"
        self.report = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else None

    @property
    def calls(self) -> list[dict]:
        return _calls(self.calls_path)

    @property
    def session_calls(self) -> list[dict]:
        """Every call the cases made, without the version pin and the help capture."""

        return [call for call in self.calls if call["argv"] not in (["--version"], ["--help"])]

    def rows(self) -> dict[int, dict]:
        assert self.report is not None
        return {row["case"]: row for row in self.report["rows"]}

    def runs(self) -> dict[str, dict]:
        return {run["label"]: run for row in self.rows().values() for run in row["runs"]}


@pytest.fixture(scope="module")
def trial(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Trial]:
    """One full trial with the fake claude, read by the tests that only inspect it.

    The outer environment holds ``OUTER_ENV``, so every test that reads this
    trial also runs where the trial has something to keep out.
    """

    patch = pytest.MonkeyPatch()
    try:
        finished = _Trial(tmp_path_factory.mktemp("trial"), patch, **OUTER_ENV)
    finally:
        patch.undo()
    yield finished


def _pin() -> str:
    return "alice-memory==" + json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]


def test_six_cases_each_write_a_row(trial: _Trial) -> None:
    """Every case writes a row with its exit code, stub requests, uvx records, markers and events.

    Mutation: drop ``--verbose`` or ``--output-format`` from the stream command,
    run case 1 without ``--init-only``, or skip a case. This test fails.
    """

    assert trial.code == 0
    rows = trial.rows()
    assert sorted(rows) == [1, 2, 3, 4, 5, 6]
    assert [[run["label"] for run in rows[number]["runs"]] for number in sorted(rows)] == [
        ["1"],
        ["2"],
        ["3"],
        ["4"],
        ["5"],
        ["6a", "6b"],
    ]
    assert all(row["setup_ok"] for row in rows.values())
    runs = trial.runs()
    assert runs["1"]["args"] == ["--init-only"]
    assert runs["6a"]["args"] == ["--init-only"]
    for label in ("2", "3", "4", "5", "6b"):
        assert runs[label]["args"] == STREAM_ARGS
    assert runs["1"]["exit_code"] == 0
    assert runs["1"]["stub_requests"] == []
    for label in ("2", "3", "4", "5", "6b"):
        assert runs[label]["exit_code"] == 1
        assert runs[label]["first_stderr_line"] == "API Error: 400 alice|test stub"
        assert runs[label]["stub_requests"] == [["POST", "/v1/messages?beta=true"]]
        subtypes = [event["subtype"] for event in runs[label]["hook_events"]]
        assert "hook_started" in subtypes and "hook_response" in subtypes
        assert "api_retry" not in subtypes and "init" not in subtypes
        assert runs[label]["init_event"]["subtype"] == "init"
    assert runs["1"]["hook_events"] == [] and runs["1"]["init_event"] is None
    hook_argv = ["--from", _pin(), "alice-memory-session-start"]
    mcp_argv = ["--from", _pin(), "alice-memory", "mcp", "--data-dir", "~/.alice"]
    for label in ("1", "2", "3", "4", "6a", "6b"):
        argvs = [record["argv"] for record in runs[label]["uvx_records"]]
        assert [argv for argv in argvs if "alice-memory-session-start" in argv] == [hook_argv], label
        assert runs[label]["plugin_hook_ran"] is True
        # The server is spawned only when there is a conversation, so init-only has no mcp record.
        assert (mcp_argv in argvs) == (label not in ("1", "6a")), label
    vault = str(trial.temp / "case-5" / "vault")
    assert [
        record["argv"] for record in runs["5"]["uvx_records"] if "alice-memory-session-start" in record["argv"]
    ] == [["--from", _pin(), "alice-memory-session-start"]]
    ok = {"hook_name": "SessionStart:startup", "outcome": "success", "exit_code": 0, "first_line": ""}
    assert runs["1"]["hook_results"] == [] and runs["2"]["hook_results"] == [ok]
    assert runs["3"]["hook_results"] == [ok, ok] and runs["4"]["hook_results"] == [ok, ok]
    assert runs["2"]["stdout_tail"][-1] == '{"type": "result", "is_error": true}'
    assert len(runs["2"]["stdout_tail"]) == 2
    assert runs["1"]["markers"] == {} and runs["2"]["markers"] == {}
    assert runs["3"]["markers"] == {"control-a": True}
    assert runs["4"]["markers"] == {"control-b": True}
    assert {plugin["name"] for plugin in runs["4"]["init_plugins"]} == {"probe-plugin", "alice-memory"}
    assert {plugin["name"] for plugin in runs["2"]["init_plugins"]} == {"alice-memory"}
    assert runs["2"]["init_plugin_errors"] == []


def test_every_claude_call_gets_its_own_debug_file(trial: _Trial) -> None:
    """Every call except the version pin passes ``--debug-file <artifacts>/<name>.debug.log``.

    Setup calls put the flag before the subcommand. Each run has its own log.
    Mutation: drop the flag from a run or from a setup step, or reuse one path.
    This test fails.
    """

    calls = trial.calls
    assert [call["argv"] for call in calls[:2]] == [["--version"], ["--help"]]
    assert "fake help" in (trial.artifacts / "claude-help.txt").read_text(encoding="utf-8")
    seen: list[str] = []
    for call in trial.session_calls:
        argv = call["argv"]
        assert argv[0] == "--debug-file", argv
        assert Path(argv[1]).parent == trial.artifacts and argv[1].endswith(".debug.log"), argv
        seen.append(argv[1])
    assert len(set(seen)) == len(seen)
    runs = trial.runs()
    logs = {run["debug_file"] for run in runs.values()}
    assert len(logs) == 7 and None not in logs
    assert all((trial.artifacts / name).is_file() for name in logs)
    setup_calls = [call["argv"][2] for call in trial.session_calls if call["argv"][2] == "plugin"]
    assert len(setup_calls) == 6 * 3 + 1 * 1  # marketplace-add, install, list per case; case 4 has two installs


def test_a_hook_that_does_not_run_is_a_valid_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing plugin hook is a result: exit 0, six rows, the headline says so.

    The settings hook still fires, so control A shows SessionStart works.
    Mutation: return 1 when no plugin hook ran, count the settings hook as the
    plugin's, or print ``yes`` in the Markers cell for a marker that did not
    fire. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_MODE="no-plugin-hooks")
    assert trial.code == 0
    runs = trial.runs()
    assert all(run["plugin_hook_ran"] is False for run in runs.values())
    assert all(
        "alice-memory-session-start" not in record["argv"] for run in runs.values() for record in run["uvx_records"]
    )
    assert all(run["hook_results"] == [] or run["label"] in ("3", "4") for run in runs.values())
    assert runs["3"]["markers"] == {"control-a": True}
    assert runs["4"]["markers"] == {"control-b": False}
    headline = trial.report["headline"]
    assert headline.startswith("alice-memory session-start reached uvx in runs: none;")
    assert "not in runs: 1, 2, 3, 4, 5, 6a, 6b" in headline
    assert "control-a fired" in headline and "control-b did not fire" in headline
    text = trial.summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n" + headline + "\n")
    table = [line for line in text.splitlines() if line.startswith("| ")]
    markers = [re.split(r"(?<!\\)\|", line)[1:-1][6].strip() for line in table[2:]]
    assert markers == ["-", "-", "control-a: yes", "control-b: no", "-", "-", "-"]


def test_control_hooks_are_exec_form_and_the_probe_has_no_user_config(trial: _Trial) -> None:
    """Control A is a settings hook and control B a plugin hook, both exec form with the interpreter.

    Mutation: write a shell-form command string, put ``${user_config...}`` in the
    probe's args, or leave the probe out of the marketplace. This test fails.
    """

    settings = json.loads((trial.temp / "case-3" / "home" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    marker = trial.artifacts / "case3-control-a.marker"
    assert settings["hooks"] == {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": sys.executable, "args": [str(SCRIPT), "mark", str(marker)]}]}
        ]
    }
    assert settings["enabledPlugins"] == {"alice-memory@alicememory": True}
    probe = trial.temp / "case-4" / "market" / "plugins" / "probe-plugin"
    hooks_text = (probe / "hooks" / "hooks.json").read_text(encoding="utf-8")
    assert "${user_config" not in hooks_text
    assert json.loads(hooks_text) == {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": sys.executable,
                            "args": [str(SCRIPT), "mark", str(trial.artifacts / "case4-control-b.marker")],
                        }
                    ]
                }
            ]
        }
    }
    listing = json.loads((trial.temp / "case-4" / "market" / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    assert [entry["name"] for entry in listing["plugins"]] == ["alice-memory", "probe-plugin"]
    assert listing["name"] == "alicememory"
    two = json.loads((trial.temp / "case-2" / "market" / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    assert [entry["name"] for entry in two["plugins"]] == ["alice-memory"]
    installs = [call["argv"][2:] for call in trial.calls if call["argv"][2:4] == ["plugin", "install"]]
    assert ["plugin", "install", "probe-plugin@alicememory"] in installs
    assert ["plugin", "install", "alice-memory@alicememory"] in installs


def test_control_c_sets_the_option_at_install_and_the_others_do_not(trial: _Trial) -> None:
    """Only case 5 installs with ``--config data_dir=<tmp>``.

    Mutation: pass ``--config`` in every case, or drop it from case 5. This test fails.
    """

    vault = str(trial.temp / "case-5" / "vault")
    with_config = [
        call["argv"][2:] for call in trial.calls if "--config" in call["argv"]
    ]
    assert with_config == [["plugin", "install", "alice-memory@alicememory", "--config", f"data_dir={vault}"]]
    assert Path(vault).is_dir()
    settings = json.loads((trial.temp / "case-2" / "home" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "pluginConfigs" not in settings


def test_the_delay_applies_only_to_case_six(trial: _Trial) -> None:
    """The stub holds its reply for the delay in 6a and 6b and not in cases 1 to 5.

    The fake claude waits for the reply, so a run that skipped the wait is shorter
    than the delay. Mutation: pass the delay in case 2, or never sleep in the stub.
    This test fails.
    """

    runs = trial.runs()
    for label in ("1", "2", "3", "4", "5"):
        assert runs[label]["stub_delay_seconds"] == 0.0, label
    for label in ("6a", "6b"):
        assert runs[label]["stub_delay_seconds"] == 0.3, label
    assert runs["6b"]["wall_ms"] >= 300
    assert trial.report["delay_seconds"] == 0.3
    assert "0.3 seconds" in trial.rows()[6]["shows"]


def test_an_unset_option_that_blocks_the_hook_shows_in_the_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that starts and fails shows its outcome, exit code and message in the row and the table.

    The fake refuses the plugin's hook when ``data_dir`` is not set, as the pinned
    claude did in the first CI trial, and runs it when ``--config`` set it. The
    current hooks.json no longer names the option, so the fake refuses on the
    option alone. The point is how the trial reports a hook that starts and
    fails. Mutation: drop ``hook_results`` from the run record, read the wrong
    event subtype, or drop the results column. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_MODE="unset-option")
    assert trial.code == 0
    runs = trial.runs()
    message = (
        'Failed to run: Plugin option "data_dir" isn\'t set. Open /plugin manage to configure it, '
        'or check that the plugin\'s userConfig schema declares "data_dir".'
    )
    failed = {"hook_name": "SessionStart:startup", "outcome": "error", "exit_code": 1, "first_line": message}
    ok = {"hook_name": "SessionStart:startup", "outcome": "success", "exit_code": 0, "first_line": ""}
    # --init-only prints no stream, so its outcome is only in the debug log.
    assert runs["1"]["hook_results"] == [] and runs["6a"]["hook_results"] == []
    assert runs["2"]["hook_results"] == [failed]
    assert runs["3"]["hook_results"] == [failed, ok]
    assert runs["4"]["hook_results"] == [ok, failed]
    assert runs["5"]["hook_results"] == [ok]
    assert runs["6b"]["hook_results"] == [failed]
    assert [r["plugin_hook_ran"] for r in (runs[k] for k in ("1", "2", "3", "4", "5", "6a", "6b"))] == [
        False,
        False,
        False,
        False,
        True,
        False,
        False,
    ]
    assert runs["3"]["markers"] == {"control-a": True} and runs["4"]["markers"] == {"control-b": True}
    assert trial.report["headline"] == (
        "alice-memory session-start reached uvx in runs: 5; not in runs: 1, 2, 3, 4, 6a, 6b; "
        "control-a fired; control-b fired."
    )
    table = [line for line in trial.summary.read_text(encoding="utf-8").splitlines() if line.startswith("| ")]
    assert f"error (exit 1): {message}".replace("|", "\\|") in table[3]
    assert "success (exit 0)" in table[6]
    assert "| none |" in table[2]


def test_each_run_starts_from_clean_logs_and_its_own_home(trial: _Trial) -> None:
    """A run sees only its own uvx records, markers and requests.

    6a leaves a session-start row, and 6b must not repeat it. Mutation: do not
    clear the uvx log between runs, or share one home between cases. This test
    fails.
    """

    runs = trial.runs()
    assert len(runs["6a"]["uvx_records"]) == 1
    hooks_in_6b = [r for r in runs["6b"]["uvx_records"] if "alice-memory-session-start" in r["argv"]]
    assert len(hooks_in_6b) == 1
    assert len(runs["6b"]["uvx_records"]) == 2
    assert runs["6b"]["stub_requests"] == [["POST", "/v1/messages?beta=true"]]
    homes = {call["home"] for call in trial.session_calls}
    assert len(homes) == 6
    assert all(Path(home).parent.parent == trial.temp for home in homes)
    assert runs["3"]["markers"] == {"control-a": True}
    assert "control-b" not in runs["3"]["markers"]
    assert not (trial.temp / "case-2" / "home" / ".claude" / "plugins" / "cache" / "probe-plugin").exists()


def test_setup_failure_marks_the_case_and_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed install is recorded, the report is still written, and the trial exits 1.

    Mutation: ignore a failed setup step and run anyway, or return 0. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_INSTALL_FAIL="alice-memory")
    assert trial.code == 1
    assert trial.report is not None
    assert all(row["setup_ok"] is False for row in trial.rows().values())
    assert all(row["runs"] == [] for row in trial.rows().values())
    install = trial.rows()[2]["setup"][1]
    assert install["step"] == "install-1" and install["exit_code"] == 1
    assert install["first_stderr_line"] == "install exploded"
    text = trial.summary.read_text(encoding="utf-8")
    assert "setup failed" in text and "install exploded" in text


def test_a_rejected_debug_flag_is_retried_without_it_for_setup_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a subcommand names ``--debug-file`` as unknown, the step runs once more without it.

    Mutation: drop the retry. Setup fails and this test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_REJECT_DEBUG="1")
    assert trial.code == 0
    steps = [step for row in trial.rows().values() for step in row["setup"]]
    assert steps and all(step["debug_file_rejected"] is True for step in steps)
    plain = [call["argv"] for call in trial.calls if call["argv"][:1] == ["plugin"]]
    assert plain and all("--debug-file" not in argv for argv in plain)
    assert all(run["debug_file"] for run in trial.runs().values())


def test_a_wrong_claude_version_stops_before_any_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the pinned claude runs the trial.

    Mutation: skip the version check. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_VERSION="9.9.9 (Claude Code)")
    assert trial.code == 1
    assert trial.report is None
    assert [call["argv"] for call in trial.calls] == [["--version"]]
    assert (trial.artifacts / "claude-version.txt").read_text(encoding="utf-8").strip() == "9.9.9 (Claude Code)"


def test_a_timeout_is_recorded_and_the_trial_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TimeoutExpired becomes exit_code ``timeout`` with the debug lines and requests so far.

    Mutation: let TimeoutExpired escape. This test fails.
    """

    module = _load_trial()
    _fake(tmp_path, monkeypatch, FAKE_MODE="hang")
    monkeypatch.setattr(module, "_TIMEOUT_SECONDS", 2)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    api = module._Api()
    try:
        row = module._case_two(artifacts, tmp_path / "temp", api, 0.0)
    except subprocess.TimeoutExpired:
        raise AssertionError("timeout was not recorded") from None
    finally:
        api.close()
    run = row["runs"][0]
    assert run["exit_code"] == "timeout"
    assert run["plugin_hook_ran"] is True
    assert run["debug_line_count"] >= 1


def test_run_refuses_a_non_empty_artifacts_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale files cannot look like a fresh run.

    Mutation: drop the check. This test fails.
    """

    module = _load_trial()
    calls = _fake(tmp_path, monkeypatch)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "old.txt").write_text("old", encoding="utf-8")
    assert module.run(artifacts, tmp_path / "temp") == 1
    assert not (artifacts / "report.json").exists()
    assert _calls(calls) == []


def test_summary_has_one_escaped_line_per_run_and_keeps_earlier_output(trial: _Trial) -> None:
    """The table has a header, a separator, and one line for each of the seven runs.

    A ``|`` in a cell is escaped, and the summary appends. The Exit, First
    stderr line and Markers cells are read per row, so a cell cannot say
    something the run did not do. Mutation: skip the escape, overwrite the
    summary file, drop a run's line, print a fixed Exit value, print ``yes``
    for every marker, or print nothing for an empty stderr line. This test
    fails.
    """

    text = trial.summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n")
    table = [line for line in text.splitlines() if line.startswith("| ")]
    assert table[0].startswith("| Case | Command | Exit | First stderr line |")
    assert table[1].startswith("| --- |")
    assert [line.split(" | ")[0] for line in table[2:]] == ["| 1", "| 2", "| 3", "| 4", "| 5", "| 6a", "| 6b"]
    for line in table:
        assert len(re.split(r"(?<!\\)\|", line)) == 12, line
    assert "API Error: 400 alice\\|test stub" in table[3]
    assert "claude --init-only" in table[2]
    cells = [[cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]] for line in table[2:]]
    assert [row[2] for row in cells] == ["0", "1", "1", "1", "1", "0", "1"]
    assert [row[3] for row in cells] == ["(empty)"] + ["API Error: 400 alice\\|test stub"] * 4 + [
        "(empty)",
        "API Error: 400 alice\\|test stub",
    ]
    assert [row[6] for row in cells] == ["-", "-", "control-a: yes", "control-b: yes", "-", "-", "-"]
    hook_cell = "uvx --from " + _pin() + " alice-memory-session-start"
    server_cell = "uvx --from " + _pin() + " alice-memory mcp --data-dir ~/.alice"
    assert cells[1][5] == hook_cell + "<br>" + server_cell
    assert cells[0][5] == hook_cell
    assert "success (exit 0)" in table[3] and "success (exit 0)<br>success (exit 0)" in table[4]
    assert "control-a: yes" in table[4]
    assert "control-b: yes" in table[5]
    assert "hook_started x1" in table[3]
    assert "plugins: alice-memory; errors: 0" in table[3]
    assert "POST /v1/messages?beta=true" in table[3]
    assert text.count("<details>") == 7


def test_debug_lines_are_filtered_and_capped_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only keyword lines stay, and a run keeps at most 200 of them.

    Mutation: keep every line, drop the cap, or show 4 lines in the summary
    instead of 40. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_DEBUG_LINES="300")
    run = trial.runs()["2"]
    assert len(run["debug_lines"]) == 200
    assert run["debug_line_count"] == 302
    assert not any("unrelated" in line for line in run["debug_lines"])
    assert run["debug_lines"][:2] == ["[DEBUG] loaded plugin alice-memory", "[DEBUG] SessionStart hooks matched: 1"]
    full = (trial.artifacts / run["debug_file"]).read_text(encoding="utf-8")
    assert "unrelated network line" in full
    # The step summary shows only the first 40 of them, and says how many matched.
    text = trial.summary.read_text(encoding="utf-8")
    assert "Run 2: 302 key debug lines (first 40)" in text
    shown = text.split("Run 2: 302 key debug lines (first 40)", 1)[1].split("```")[1]
    assert len([line for line in shown.splitlines() if line]) == 40
    assert shown.splitlines()[1] == "[DEBUG] loaded plugin alice-memory"


def test_key_debug_lines_match_each_term_and_keep_order() -> None:
    """The five terms match without regard to case, and unrelated lines are dropped.

    Mutation: drop ``user_config``, make the match case sensitive, or cap at 100.
    This test fails.
    """

    module = _load_trial()
    text = "\n".join(
        [
            "a SessionStart line",
            "unrelated one",
            "a HOOK line",
            "loading Plugin x",
            "the alice-memory package",
            "value ${user_config.data_dir}",
            "unrelated two",
        ]
    )
    lines, count = module.key_debug_lines(text)
    assert lines == [
        "a SessionStart line",
        "a HOOK line",
        "loading Plugin x",
        "the alice-memory package",
        "value ${user_config.data_dir}",
    ]
    assert count == 5
    many = "\n".join(f"hook {index}" for index in range(250))
    lines, count = module.key_debug_lines(many)
    assert len(lines) == 200 and count == 250
    assert lines[0] == "hook 0" and lines[-1] == "hook 199"
    long_lines, _ = module.key_debug_lines("hook " + "x" * 2000)
    assert len(long_lines[0]) < 600


def test_hook_events_keep_hook_types_hook_subtypes_and_the_init_event() -> None:
    """An event stays when its type or its subtype names a hook. ``system/init`` is kept apart.

    Mutation: test only the type, only the subtype, or drop the init event.
    This test fails.
    """

    module = _load_trial()
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "hook_started", "n": 1}),
            json.dumps({"type": "hook_response", "n": 2}),
            json.dumps({"type": "system", "subtype": "init", "plugins": [{"name": "alice-memory"}]}),
            json.dumps({"type": "assistant", "message": "ok"}),
            json.dumps({"type": "system", "subtype": "api_retry"}),
            json.dumps({"type": "system", "subtype": "PostToolUseHook", "n": 3}),
            "not json at all",
            json.dumps(["hook"]),
            json.dumps({"type": "system", "subtype": "hook_response", "output": "y" * 5000}),
        ]
    )
    events, init = module.hook_events(stream)
    assert [event.get("n") for event in events] == [1, 2, 3, None]
    assert init is not None and init["plugins"] == [{"name": "alice-memory"}]
    assert len(events[-1]["output"]) < 2100
    assert module.hook_events("") == ([], None)


def test_hook_results_read_the_response_events_only() -> None:
    """Each ``hook_response`` gives its outcome, exit code and first output line, clipped.

    The output falls back to stderr and then stdout, and ``hook_started`` gives
    nothing. Mutation: read ``hook_started`` too, skip the clip, or ignore
    stderr. This test fails.
    """

    module = _load_trial()
    events = [
        {"type": "system", "subtype": "hook_started", "hook_name": "a"},
        {"type": "system", "subtype": "hook_response", "hook_name": "a", "output": "first\nsecond", "exit_code": 1, "outcome": "error"},
        {"type": "system", "subtype": "hook_response", "hook_name": "b", "output": "", "stderr": "from stderr", "exit_code": 2, "outcome": "error"},
        {"type": "system", "subtype": "hook_response", "hook_name": "c", "output": "", "stdout": "from stdout", "exit_code": 0, "outcome": "success"},
        {"type": "system", "subtype": "hook_response", "hook_name": "d", "output": "x" * 500, "exit_code": 0, "outcome": "success"},
        {"type": "system", "subtype": "hook_response", "hook_name": "e"},
    ]
    results = module.hook_results(events)
    assert [(r["hook_name"], r["outcome"], r["exit_code"]) for r in results] == [
        ("a", "error", 1),
        ("b", "error", 2),
        ("c", "success", 0),
        ("d", "success", 0),
        ("e", None, None),
    ]
    assert [r["first_line"] for r in results][:3] == ["first", "from stderr", "from stdout"]
    assert len(results[3]["first_line"]) < 210 and results[4]["first_line"] == ""
    assert module.hook_results([]) == []


def test_the_results_cell_escapes_pipes_and_joins_hooks() -> None:
    """A hook message with a ``|`` cannot break the table, and each hook gets its own line.

    Mutation: skip the escape, or drop the exit code. This test fails.
    """

    module = _load_trial()
    cell = module._results_cell(
        {
            "hook_results": [
                {"outcome": "error", "exit_code": 1, "first_line": "a|b"},
                {"outcome": "success", "exit_code": 0, "first_line": ""},
            ]
        }
    )
    assert cell == "error (exit 1): a\\|b<br>success (exit 0)"
    assert module._results_cell({"hook_results": []}) == "none"
    assert module._results_cell({}) == "none"


def test_the_init_cell_names_plugins_and_says_when_an_error_list_is_absent() -> None:
    """The Init column lists plugin names and the error count, and says so when there is no list.

    Mutation: print ``None`` for a missing list, or show the plugin paths
    instead of the names. This test fails.
    """

    module = _load_trial()
    plugins = [{"name": "alice-memory", "path": "/p"}, "bare"]
    assert module._init_cell({"init_event": {}, "init_plugins": plugins, "init_plugin_errors": []}) == (
        "plugins: alice-memory, bare; errors: 0"
    )
    assert module._init_cell({"init_event": {}, "init_plugins": plugins, "init_plugin_errors": [{}, {}]}) == (
        "plugins: alice-memory, bare; errors: 2"
    )
    assert module._init_cell({"init_event": {}, "init_plugins": None, "init_plugin_errors": None}) == (
        "plugins: none; errors: no plugin_errors field"
    )
    assert module._init_cell({"init_event": None}) == "no init event"


def test_mark_appends_one_line_per_call_and_exits_zero_on_bad_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The marker command appends a line for each run and never fails the host.

    Mutation: overwrite instead of append, or exit 1 on bad JSON. This test fails.
    """

    module = _load_trial()
    marker = tmp_path / "out" / "marker"

    class _Stdin:
        def __init__(self, raw: bytes) -> None:
            self.buffer = io.BytesIO(raw)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin/root")
    monkeypatch.setattr(sys, "stdin", _Stdin(b'{"hook_event_name":"SessionStart","source":"startup","session_id":"s"}'))
    assert module.main(["mark", str(marker)]) == 0
    monkeypatch.setattr(sys, "stdin", _Stdin(b"not json"))
    assert module.main(["mark", str(marker)]) == 0
    rows = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["hook_event_name"] == "SessionStart" and rows[0]["source"] == "startup"
    assert rows[0]["payload_keys"] == ["hook_event_name", "session_id", "source"]
    assert rows[0]["CLAUDE_PLUGIN_ROOT"] == "/plugin/root"
    assert rows[1]["hook_event_name"] is None


def test_the_trial_uses_the_standard_library_and_the_plugin_name(tmp_path: Path) -> None:
    """The script never imports alicebot_api, and its plugin name is the shipped one.

    The marketplace and plugin ids are tied to install's constants in
    ``test_claude_code_plugin.py``. Mutation: import host_install at the top of
    the script, or change the plugin name. This test fails.
    """

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util\n"
        "import sys\n"
        f"spec = importlib.util.spec_from_file_location('trial', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print('LOADED' if 'alicebot_api' in sys.modules else 'ABSENT')\n",
        encoding="utf-8",
    )
    completed = subprocess.run([sys.executable, str(probe)], capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout.decode().strip() == "ABSENT"

    module = _load_trial()
    assert module._PLUGIN == json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["name"]
    assert module._PINNED_VERSION == _PINNED

    marker = tmp_path / "marker"
    ran = subprocess.run(
        [sys.executable, str(SCRIPT), "mark", str(marker)], input=b"{}", capture_output=True, check=False
    )
    assert ran.returncode == 0 and marker.read_text(encoding="utf-8").count("\n") == 1
    usage = subprocess.run([sys.executable, str(SCRIPT), "nope"], capture_output=True, check=False)
    assert usage.returncode == 2


def test_the_sandbox_env_is_isolated_and_points_at_the_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child env is a copy of the outer env with the trial's own values laid over it.

    The outer shell here holds every name the trial must keep out, and wrong
    values for the three flags and the base URL. The real-host test builds the
    same env, and the trial's table is only comparable to it while they match.
    Mutation: delete ``env["DISABLE_AUTOUPDATER"] = "1"``, the
    ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` line, or the
    ``CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL`` line; remove
    ``CLAUDE_CONFIG_DIR`` or ``CLAUDE_PLUGIN_ROOT`` from ``_UNSET``; put the stub
    bin after the outer PATH; use ``os.environ`` without the copy; or hard-code
    the key. This test fails.
    """

    module = _load_trial()
    for name, value in {
        **OUTER_ENV,
        "ANTHROPIC_BASE_URL": "http://outer.invalid",
        "KEEP_ME": "kept",
        "PATH": "/outer/bin",
        "HOME": str(tmp_path / "outer-home"),
    }.items():
        monkeypatch.setenv(name, value)
    api = module._Api()
    try:
        first = module._Sandbox(tmp_path / "one", api)
        second = module._Sandbox(tmp_path / "two", api)
        base_url = api.base_url
    finally:
        api.close()
    env = first.env
    assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", base_url)
    assert env["ANTHROPIC_BASE_URL"] == base_url
    assert env["HOME"] == str(tmp_path / "one" / "home")
    assert env["PATH"] == str(tmp_path / "one" / "bin") + os.pathsep + "/outer/bin"
    for name in (
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
    ):
        assert env[name] == "1", name
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_PLUGIN_ROOT", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name not in env, name
    assert re.fullmatch(r"alice-test-[0-9a-f]{16}", env["ANTHROPIC_API_KEY"])
    assert second.env["ANTHROPIC_API_KEY"] != env["ANTHROPIC_API_KEY"]
    assert second.env["HOME"] == str(tmp_path / "two" / "home")
    assert env["KEEP_ME"] == "kept"
    assert os.environ.get("CLAUDE_CONFIG_DIR") == "/outer/claude-config"
    assert os.environ.get("DISABLE_AUTOUPDATER") == "0"
    assert os.access(tmp_path / "one" / "bin" / "uvx", os.X_OK)


def test_every_claude_call_gets_the_sandbox_env_whatever_the_outer_env_holds(trial: _Trial) -> None:
    """Each call the cases make sees its own home, the stub, the three flags and nothing from outside.

    The fake claude records its own environment on every call, and this
    fixture's outer shell holds ``OUTER_ENV``. Mutation: hand the outer env to
    ``subprocess.run``, drop a name from ``_UNSET``, or delete a flag. This test
    fails.
    """

    assert trial.session_calls
    urls = set()
    for call in trial.session_calls:
        env = call["env"]
        home = Path(call["home"])
        case = home.parent
        assert home.name == "home" and case.parent == trial.temp, call
        assert env["HOME"] == str(home)
        assert Path(env["PATH"].split(os.pathsep)[0]) == case / "bin", call
        for name in (
            "DISABLE_AUTOUPDATER",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
        ):
            assert env[name] == "1", (name, call)
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_PLUGIN_ROOT", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            assert env[name] is None, (name, call)
        assert env["ANTHROPIC_API_KEY"].startswith("alice-test-"), call
        assert re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", env["ANTHROPIC_BASE_URL"]), call
        urls.add(env["ANTHROPIC_BASE_URL"])
    assert len(urls) == 1
    # The version pin and the help capture run outside any case and see the outer shell.
    outside = [call for call in trial.calls if call["argv"] in (["--version"], ["--help"])]
    assert len(outside) == 2
    assert all(call["env"]["CLAUDE_CONFIG_DIR"] == "/outer/claude-config" for call in outside)


def test_the_stub_records_a_request_before_its_delayed_reply() -> None:
    """A request is on the record while the reply is still held, and the reply comes late.

    If claude exits before a delayed reply, the row must still show the request.
    Mutation: move ``owner.records.append`` after the ``time.sleep`` block, or
    never sleep. This test fails.
    """

    import threading
    import time
    import urllib.error
    import urllib.request

    module = _load_trial()
    api = module._Api()
    api.delay = 1.5
    outcome: dict[str, int] = {}

    def call() -> None:
        request = urllib.request.Request(api.base_url + "/v1/messages?beta=true", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(request, timeout=30)  # noqa: S310
        except urllib.error.HTTPError as error:
            outcome["status"] = error.code

    thread = threading.Thread(target=call)
    started = time.monotonic()
    try:
        thread.start()
        while not api.records and time.monotonic() < started + 1.0:
            time.sleep(0.01)
        recorded = list(api.records)
        waiting = thread.is_alive()
        thread.join(timeout=30)
    finally:
        api.close()
    assert recorded == [("POST", "/v1/messages?beta=true")]
    assert waiting is True
    assert outcome == {"status": 400}
    assert time.monotonic() - started >= 1.5


def test_uvx_records_carry_the_plugin_root_and_the_install_time_option(trial: _Trial) -> None:
    """The stub uvx records two env values, and only case 5's hook sees ``data_dir``.

    The hook gets ``CLAUDE_PLUGIN_ROOT`` from the plugin's install directory. In
    the fake a server spawn gets neither (the real claude also sets the root for
    the server, which this test does not model), and the outer
    ``CLAUDE_PLUGIN_ROOT`` does not reach it. Mutation: delete the ``CLAUDE_PLUGIN_OPTION_DATA_DIR`` row or the
    ``CLAUDE_PLUGIN_ROOT`` row from the stub uvx script. This test fails.
    """

    cases = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6a": 6, "6b": 6}
    vault = str(trial.temp / "case-5" / "vault")
    for label, run in trial.runs().items():
        root = trial.temp / f"case-{cases[label]}" / "home" / ".claude" / "plugins" / "cache" / "alice-memory"
        assert run["uvx_records"], label
        for record in run["uvx_records"]:
            assert set(record) == {"argv", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_OPTION_DATA_DIR"}, label
            if "alice-memory-session-start" in record["argv"]:
                assert record.get("CLAUDE_PLUGIN_ROOT") == str(root), label
                expected = vault if label == "5" else None
                assert record.get("CLAUDE_PLUGIN_OPTION_DATA_DIR") == expected, label
            else:
                assert record.get("CLAUDE_PLUGIN_ROOT") is None, label
                assert record.get("CLAUDE_PLUGIN_OPTION_DATA_DIR") is None, label


@pytest.mark.parametrize(
    "message",
    [
        "error: unknown option '--other-flag'",
        "error: --debug-file needs a path",
        "boom",
    ],
)
def test_a_setup_failure_that_is_not_an_unknown_debug_flag_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    """Only ``unknown option '--debug-file'`` earns a second call. Any other failure is final.

    The three messages are an unknown option that names another flag, a message
    that names the flag without saying it is unknown, and a plain failure. Each
    case makes exactly one plugin call. Mutation: retry on any nonzero exit,
    drop ``--debug-file`` from the text test, or drop ``unknown option`` from it.
    This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_PLUGIN_STDERR=message)
    assert trial.code == 1
    plugin_calls = [call["argv"] for call in trial.session_calls if "plugin" in call["argv"]]
    assert len(plugin_calls) == 6
    assert all(argv[0] == "--debug-file" for argv in plugin_calls)
    steps = [step for row in trial.rows().values() for step in row["setup"]]
    assert len(steps) == 6
    assert all(step["debug_file_rejected"] is False for step in steps)
    assert all(step["exit_code"] == 1 and step["first_stderr_line"] == message for step in steps)


def test_the_debug_flag_is_dropped_only_for_the_steps_that_reject_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When only ``plugin install`` rejects the flag, only the install steps are retried.

    Marketplace add and list keep the flag and are never repeated. Seven
    installs run twice, once with the flag and once without. Mutation: mark
    every step rejected, or retry every step. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_REJECT_DEBUG_ON="install")
    assert trial.code == 0
    steps = [(str(step["step"]), step["debug_file_rejected"]) for row in trial.rows().values() for step in row["setup"]]
    assert len(steps) == 19
    assert all(rejected is name.startswith("install") for name, rejected in steps), steps
    calls = [call["argv"] for call in trial.session_calls if "plugin" in call["argv"]]
    plain = [argv for argv in calls if argv[0] != "--debug-file"]
    flagged = [argv for argv in calls if argv[0] == "--debug-file"]
    assert len(plain) == 7 and all(argv[:2] == ["plugin", "install"] for argv in plain)
    assert len(flagged) == 19
    assert len([argv for argv in flagged if argv[2:4] == ["plugin", "install"]]) == 7


def test_a_failing_plugin_list_does_not_fail_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing is a diagnostic. A failed listing leaves setup ok, the runs in place and the summary quiet.

    Mutation: count the ``list`` step in ``_setup_ok``, or drop the ``list``
    exemption from the summary's setup-failure lines. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_LIST_FAIL="1")
    assert trial.code == 0
    rows = trial.rows()
    assert all(row["setup_ok"] is True for row in rows.values())
    assert all(row["runs"] for row in rows.values())
    lists = [step for row in rows.values() for step in row["setup"] if step["step"] == "list"]
    assert len(lists) == 6
    assert all(step["exit_code"] == 1 and step["listed"] is None for step in lists)
    assert all(step["first_stderr_line"] == "list exploded" for step in lists)
    text = trial.summary.read_text(encoding="utf-8")
    assert "setup step" not in text and "setup failed" not in text
    assert "list exploded" not in text


def test_the_timeout_and_the_default_delay_are_pinned() -> None:
    """A run may take two minutes, and the default reply delay is five seconds.

    The tests above run with a shorter delay, so nothing else reads the default.
    Mutation: set ``_TIMEOUT_SECONDS`` to 1, or change ``_DELAY_SECONDS`` or the
    default of ``run``. This test fails.
    """

    import inspect

    module = _load_trial()
    assert module._TIMEOUT_SECONDS == 120
    assert module._DELAY_SECONDS == 5.0
    assert inspect.signature(module.run).parameters["delay_seconds"].default == 5.0


def test_each_row_records_the_settings_and_the_claude_files(trial: _Trial) -> None:
    """A row keeps settings.json and the sorted files under ``.claude``, with the enabled plugins.

    Mutation: drop ``settings_after_setup`` or ``claude_files`` from the row, or
    stop sorting the files. This test fails.
    """

    rows = trial.rows()
    assert rows[2].get("settings_after_setup") == {"enabledPlugins": {"alice-memory@alicememory": True}}
    assert rows[4].get("settings_after_setup", {}).get("enabledPlugins") == {
        "probe-plugin@alicememory": True,
        "alice-memory@alicememory": True,
    }
    files = rows[2].get("claude_files")
    assert isinstance(files, list) and files == sorted(files)
    assert "settings.json" in files
    assert "plugins/cache/alice-memory/hooks/hooks.json" in files
    assert "plugins/cache/alice-memory/.claude-plugin/plugin.json" in files
    assert "plugins/cache/probe-plugin/hooks/hooks.json" in rows[4]["claude_files"]


def test_files_under_is_sorted_relative_and_capped(tmp_path: Path) -> None:
    """The file listing is relative, sorted, and stops at 100 names.

    Mutation: drop the cap or the sort. This test fails.
    """

    module = _load_trial()
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    for index in range(120):
        (root / "sub" / f"f{index:03d}").write_text("x", encoding="utf-8")
    found = module._files_under(root)
    assert len(found) == 100
    assert found == sorted(found)
    assert found[0] == "sub/f000" and found[-1] == "sub/f099"
    assert module._files_under(tmp_path / "missing") == []


def test_output_lines_decode_leniently_and_the_uvx_cell_is_clipped() -> None:
    """Bytes that are not UTF-8 become replacement characters, and a long uvx line is cut for the table.

    A timeout hands back raw bytes. Mutation: decode strictly in ``_lines``, or
    stop clipping the uvx cell. This test fails.
    """

    module = _load_trial()
    try:
        decoded = module._lines(b"ok\n\xffbad\nend")
    except UnicodeDecodeError:
        raise AssertionError("_lines decoded strictly and raised on a byte that is not UTF-8") from None
    assert decoded == ["ok", "\ufffdbad", "end"]
    assert module._lines("a\nb") == ["a", "b"]
    assert module._lines(None) == []
    long = {"argv": ["--from", "x" * 500]}
    cell = module._uvx_cell({"uvx_records": [long, {"argv": ["a|b"]}]})
    first, second = cell.split("<br>")
    assert first.startswith("uvx --from xxx") and first.endswith("...")
    assert len(first) == 163
    assert second == "uvx a\\|b"
    assert module._uvx_cell({"uvx_records": []}) == "none"


def test_the_trial_script_passes_the_repos_mypy(tmp_path: Path) -> None:
    """The script type-checks under the command CI runs for its sibling scripts.

    CI's list does not name this file, so this test is the check. Mutation:
    unpack ``host, port = self._httpd.server_address[:2]`` in ``base_url`` again.
    mypy reports ``str-bytes-safe`` and this test fails.
    """

    pytest.importorskip("mypy")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--ignore-missing-imports",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            str(SCRIPT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_command_line_hands_run_its_two_directories_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``run <artifacts> <temp>`` calls ``run(artifacts, temp)`` with the default delay.

    The workflow uploads the first path and works in the second, and case 6 needs
    the five second delay. Mutation: swap the two paths, pass a delay of 0.0 or
    any other value, drop the ``run`` branch, or let a wrong argument count
    through. This test fails.
    """

    module = _load_trial()
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record(*args: object, **kwargs: object) -> int:
        seen.append((args, kwargs))
        return 7

    monkeypatch.setattr(module, "run", record)
    assert module.main(["run", str(tmp_path / "artifacts"), str(tmp_path / "work")]) == 7
    assert seen == [((tmp_path / "artifacts", tmp_path / "work"), {})]
    assert isinstance(seen[0][0][0], Path) and isinstance(seen[0][0][1], Path)
    for bad in ([], ["run"], ["run", "one"], ["run", "one", "two", "three"], ["mark"], ["other", "a", "b"]):
        assert module.main(bad) == 2, bad
        assert "usage: real_host_plugin_hook_trial.py run <artifacts> <temp>" in capsys.readouterr().err
    assert len(seen) == 1
    marked: list[Path] = []
    monkeypatch.setattr(module, "mark", lambda marker: marked.append(marker) or 3)
    assert module.main(["mark", str(tmp_path / "marker")]) == 3
    assert marked == [tmp_path / "marker"]
    assert len(seen) == 1


def test_a_claude_call_reads_no_stdin_runs_in_the_project_and_times_out_at_the_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each claude call gets a closed stdin, the case's project as cwd and the module's timeout.

    A run that inherited the checkout as its cwd or a live stdin would not match
    the real-host test. A timeout typed into the call instead of read from the
    constant would cut case 6b, which holds two replies. Mutation: drop
    ``stdin=subprocess.DEVNULL``, drop ``cwd=sandbox.project``, or write a
    literal timeout in ``_claude``. This test fails.
    """

    import types

    module = _load_trial()
    project = tmp_path / "project"
    project.mkdir()
    sandbox = types.SimpleNamespace(project=project, env={"KEEP": "1"})
    seen: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, b"out", b"err")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_TIMEOUT_SECONDS", 7.5)
    call = module._claude(["--version"], sandbox)
    assert call.exit_code == 0 and call.stdout == "out" and call.stderr == "err"
    assert len(seen) == 1
    assert seen[0]["command"] == ["claude", "--version"]
    assert seen[0]["stdin"] is subprocess.DEVNULL
    assert seen[0]["cwd"] == project
    assert seen[0]["timeout"] == 7.5
    assert seen[0]["env"] == {"KEEP": "1"}
    assert seen[0]["capture_output"] is True and seen[0]["check"] is False


def test_the_trial_stub_records_get_and_post_requests_in_order() -> None:
    """The stub lists every request, GET and POST, with the query string, in arrival order.

    Mutation: delete ``do_GET`` from the handler, or record only POSTs. This
    test fails.
    """

    import urllib.error
    import urllib.request

    module = _load_trial()
    api = module._Api()
    try:
        for method, path in (("GET", "/v1/models"), ("POST", "/v1/messages?beta=true"), ("GET", "/v1/messages?x=1")):
            data = b"{}" if method == "POST" else None
            request = urllib.request.Request(api.base_url + path, data=data, method=method)
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=10)  # noqa: S310
            assert caught.value.code == 400
        records = list(api.records)
    finally:
        api.close()
    assert records == [("GET", "/v1/models"), ("POST", "/v1/messages?beta=true"), ("GET", "/v1/messages?x=1")]


def test_a_marker_counts_only_when_the_control_hook_wrote_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty marker file is not a fired control, and a written one is.

    The fake claude touches or fills the marker during the run, and ``_execute``
    clears the marker before it. Mutation: count a marker by ``is_file()``
    alone. This test fails.
    """

    module = _load_trial()
    _fake(tmp_path, monkeypatch)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    api = module._Api()
    try:
        empty = tmp_path / "empty.marker"
        filled = tmp_path / "filled.marker"
        absent = tmp_path / "absent.marker"
        stale = tmp_path / "stale.marker"
        stale.write_text("left over from an earlier run\n", encoding="utf-8")
        monkeypatch.setenv("FAKE_TOUCH_EMPTY", str(empty))
        monkeypatch.setenv("FAKE_TOUCH_FILLED", str(filled))
        sandbox = module._Sandbox(tmp_path / "case", api)
        run = module._execute(
            sandbox,
            api,
            artifacts,
            label="x",
            slug="marker-check",
            args=module._INIT_ONLY,
            delay=0.0,
            markers={"empty": empty, "filled": filled, "absent": absent, "stale": stale},
        )
    finally:
        api.close()
    assert run["markers"] == {"empty": False, "filled": True, "absent": False, "stale": False}
