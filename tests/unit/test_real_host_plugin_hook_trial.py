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
with open("__CALLS__", "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": original, "home": os.environ.get("HOME")}) + "\n")
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
if os.environ.get("FAKE_REJECT_DEBUG") == "1" and debug is not None and argv[:1] == ["plugin"]:
    print("error: unknown option '--debug-file'", file=sys.stderr)
    raise SystemExit(1)
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
                    blocked = mode == "unset-option" and "data_dir" not in info["config"] and any(
                        "${user_config.data_dir}" in arg for arg in handler.get("args", [])
                    )
                    args = [substitute(arg, options) for arg in handler.get("args", [])]
                    hooks.append((handler["command"], args, {"CLAUDE_PLUGIN_ROOT": str(directory)}, blocked))
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
        _FAKE_CLAUDE.replace("__PYTHON__", sys.executable).replace("__CALLS__", str(calls)),
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
    """One full trial with the fake claude, read by the tests that only inspect it."""

    patch = pytest.MonkeyPatch()
    try:
        finished = _Trial(tmp_path_factory.mktemp("trial"), patch)
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
    hook_argv = ["--from", _pin(), "alice-memory-session-start", "--data-dir", "~/.alice"]
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
    ] == [["--from", _pin(), "alice-memory-session-start", "--data-dir", vault]]
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
    Mutation: return 1 when no plugin hook ran, or count the settings hook as
    the plugin's. This test fails.
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

    The fake refuses the plugin hook when ``data_dir`` is not set, as the pinned
    claude did in the first CI trial, and runs it when ``--config`` set it.
    Mutation: drop ``hook_results`` from the run record, read the wrong event
    subtype, or drop the results column. This test fails.
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

    A ``|`` in a cell is escaped, and the summary appends. Mutation: skip the
    escape, overwrite the summary file, or drop a run's line. This test fails.
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
    assert "uvx --from " + _pin() + " alice-memory-session-start --data-dir ~/.alice" in table[3]
    assert "success (exit 0)" in table[3] and "success (exit 0)<br>success (exit 0)" in table[4]
    assert "control-a: yes" in table[4]
    assert "control-b: yes" in table[5]
    assert "hook_started x1" in table[3]
    assert "plugins: alice-memory; errors: 0" in table[3]
    assert "POST /v1/messages?beta=true" in table[3]
    assert text.count("<details>") == 7


def test_debug_lines_are_filtered_and_capped_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only keyword lines stay, and a run keeps at most 200 of them.

    Mutation: keep every line, or drop the cap. This test fails.
    """

    trial = _Trial(tmp_path, monkeypatch, FAKE_DEBUG_LINES="300")
    run = trial.runs()["2"]
    assert len(run["debug_lines"]) == 200
    assert run["debug_line_count"] == 302
    assert not any("unrelated" in line for line in run["debug_lines"])
    assert run["debug_lines"][:2] == ["[DEBUG] loaded plugin alice-memory", "[DEBUG] SessionStart hooks matched: 1"]
    full = (trial.artifacts / run["debug_file"]).read_text(encoding="utf-8")
    assert "unrelated network line" in full


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
