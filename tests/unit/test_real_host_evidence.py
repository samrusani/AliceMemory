"""The host-evidence script records what a host sends a hook and an MCP server, and keeps no raw value.

Slice S0 of per-project memory asks one thing of the pinned real hosts: what do they hand a
SessionStart hook and an MCP server? ``scripts/real_host_evidence.py`` records it from a
dispatch-only job. These tests run the script's pieces and its whole ``run`` against stand-ins
for the two hosts (``tests/unit/host_evidence_fakes.py``), so no real host starts. A planted
client folder, key-shaped token, private sentence and environment value must reach no file.

Mutation notes live on each test. A miss raises AssertionError.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.unit import host_evidence_fakes as fakes

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "real_host_evidence.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("real_host_evidence", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["real_host_evidence"] = module
    spec.loader.exec_module(module)
    return module


evidence = _load()

PLANTED_ENV_VALUE = "hunter2-planted-env-value"
FORBIDDEN = (
    fakes.PLANTED_FOLDER,
    fakes.PLANTED_SENTENCE,
    fakes.PLANTED_TOKEN,
    fakes.PLANTED_KEY,
    PLANTED_ENV_VALUE,
    "alice-test-",
    "dummy-",
    "real-looking-key",
)


def _context(root: Path, names: list[str] | None = None) -> Any:
    (root / "repo" / "packages" / "app").mkdir(parents=True, exist_ok=True)
    (root / "home").mkdir(exist_ok=True)
    (root / "art").mkdir(exist_ok=True)
    return evidence.Context(
        {
            "launch": str(root / "repo" / "packages" / "app"),
            "repo": str(root / "repo"),
            "home": str(root / "home"),
            "artifacts": str(root / "art"),
            "scratch": str(root),
        },
        names or [],
    )


def _write_context(root: Path, names: list[str] | None = None) -> Path:
    path = root / "context.json"
    path.write_text(json.dumps(_context(root, names).to_json()), encoding="utf-8")
    return path


def _launch(root: Path) -> Path:
    return root / "repo" / "packages" / "app"


# --- paths and values ------------------------------------------------------------------------


def test_path_shape_keeps_depth_root_and_extension_and_hides_every_name(tmp_path: Path) -> None:
    """A path keeps its known root, its depth and its extension, and no real name.

    The deepest known folder wins, so a path under the launch folder reads ``<launch>``.
    Mutation: return the path unchanged, or sort the known folders shortest first
    (so ``<scratch>`` wins). This test fails.
    """

    root = tmp_path / "secret-client-checkout"
    ctx = _context(root)
    launch, repo = _launch(root), root / "repo"
    transcript = root / "home" / ".claude" / "projects" / "-secret-client" / "0b6c1d52-3a8f-4f43-9a54-5c7a9c7e8d11.jsonl"
    assert evidence.path_shape(str(launch), ctx) == "<launch>"
    assert evidence.path_shape(str(repo), ctx) == "<repo>"
    assert evidence.path_shape(str(launch / "src" / "main.py"), ctx) == "<launch>/<name>/<name>.py"
    assert evidence.path_shape(str(repo / "docs"), ctx) == "<repo>/<name>"
    assert evidence.path_shape(str(transcript), ctx) == "<home>/.claude/projects/<name>/<uuid>.jsonl"
    assert evidence.path_shape("/etc/secret-client/passwd.txt", ctx) == "<abs>/<name>/<name>/<name>.txt"
    assert evidence.path_shape("~/notes/todo", ctx) == "<tilde>/<name>/<name>"
    assert evidence.path_shape("./a/../b", ctx) == "<rel>/<name>"
    assert evidence.path_shape("file://" + str(launch / "x"), ctx) == "file://<launch>/<name>"
    for text in (str(transcript), "/etc/secret-client/passwd.txt", str(launch / "src")):
        assert "secret-client" not in evidence.path_shape(text, ctx)


def test_relation_places_a_path_against_the_launch_folder(tmp_path: Path) -> None:
    """The relation says where a reported folder sits: the launch folder, the repo root, between, or elsewhere.

    Mutation: report the repository root as the launch folder, or treat a folder between them as the
    root. This test fails.
    """

    root = tmp_path / "work"
    ctx = _context(root)
    (root / "repo" / "packages").mkdir(exist_ok=True)
    launch, repo = _launch(root), root / "repo"
    cases = {
        str(launch): "launch_dir",
        str(repo): "repo_root",
        str(repo / "packages"): "between_launch_dir_and_repo_root",
        str(launch / "src"): "below_launch_dir",
        str(repo / "docs"): "elsewhere_in_repo",
        str(root): "above_repo_root",
        str(root / "home"): "home",
        str(root / "home" / ".claude"): "below_home",
        "/usr/bin": "elsewhere",
        "relative/dir": "not_absolute",
    }
    for path, expected in cases.items():
        assert evidence.relation(path, ctx) == expected, path
    assert evidence.relation("file://" + str(launch), ctx) == "launch_dir"
    assert evidence.git_levels_up(str(launch)) is None
    (repo / ".git").mkdir()
    assert evidence.git_levels_up(str(launch)) == 2
    assert evidence.git_levels_up(str(repo)) == 0


def test_redact_value_keeps_words_and_numbers_and_hides_text_tokens_and_paths(tmp_path: Path) -> None:
    """A short word, a number and a flag stay. Free text, a token and a path do not.

    Mutation: keep free text, keep a long token, or skip the path shape. This test fails.
    """

    root = tmp_path / "secret-client-checkout"
    ctx = _context(root)
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "count": 3,
        "flag": True,
        "nothing": None,
        "session_id": "0b6c1d52-3a8f-4f43-9a54-5c7a9c7e8d11",
        "cwd": str(_launch(root)),
        "note": fakes.PLANTED_SENTENCE,
        "token": fakes.PLANTED_TOKEN,
        "nested": {"path": str(root / "repo" / "secret-file.txt"), "words": ["ok", fakes.PLANTED_SENTENCE]},
        fakes.PLANTED_SENTENCE: "key that is free text",
    }
    redacted = evidence.redact_value(payload, ctx)
    text = json.dumps(redacted)
    assert redacted["hook_event_name"] == "SessionStart" and redacted["source"] == "startup"
    assert redacted["count"] == 3 and redacted["flag"] is True and redacted["nothing"] is None
    assert redacted["session_id"] == "<uuid>"
    assert redacted["cwd"] == "<launch>"
    assert redacted["note"] == f"<text:{len(fakes.PLANTED_SENTENCE)} chars>"
    assert redacted["token"] == f"<token:{len(fakes.PLANTED_TOKEN)} chars>"
    assert redacted["nested"]["path"] == "<repo>/<name>.txt"
    for word in ("secret-client", "ghp_", "migration", "secret-file"):
        assert word not in text


def test_env_record_names_every_variable_and_records_no_value(tmp_path: Path) -> None:
    """Names only, with path shapes for two kinds of variable: a known path variable, and one the host added.

    Mutation: add the values to the record, report a path value for a variable that is neither known
    nor host-added, or report a non-path value of a host-added variable. This test fails.
    """

    root = tmp_path / "secret-client-checkout"
    ctx = _context(root, ["HOME", "PATH", "PLANTED", "PWD"])
    environ = {
        "HOME": str(root / "home"),
        "PATH": "/opt/secret-client/bin:/usr/bin",
        "PWD": str(_launch(root)),
        "PLANTED": PLANTED_ENV_VALUE,
        "CLAUDE_PROJECT_DIR": str(_launch(root)),
        "HOST_ADDED_DIR": str(root / "repo"),
        "HOST_ADDED_TOKEN": fakes.PLANTED_TOKEN,
        "NOT_ADDED_PATH": "/opt/secret-client/elsewhere",
    }
    ctx_names = ["HOME", "PATH", "PLANTED", "PWD", "NOT_ADDED_PATH"]
    ctx = evidence.Context(ctx.folders, ctx_names)
    record = evidence.env_record(environ, ctx)
    text = json.dumps(record)
    assert record["names"] == sorted(environ)
    assert record["count"] == len(environ)
    assert record["added_by_host"] == ["CLAUDE_PROJECT_DIR", "HOST_ADDED_DIR", "HOST_ADDED_TOKEN"]
    assert record["dropped_from_launch"] == []
    variables = record["path_variables"]
    assert set(variables) == {"HOME", "PWD", "CLAUDE_PROJECT_DIR", "HOST_ADDED_DIR"}
    assert variables["HOME"] == {"basis": "known_path_variable", "shape": "<home>", "relation": "home"}
    assert variables["CLAUDE_PROJECT_DIR"]["relation"] == "launch_dir"
    assert variables["HOST_ADDED_DIR"]["basis"] == "added_by_host"
    for raw in (PLANTED_ENV_VALUE, fakes.PLANTED_TOKEN, "secret-client", "/usr/bin", str(root)):
        assert raw not in text
    dropped = evidence.env_record({"HOME": str(root / "home")}, ctx)
    assert dropped["dropped_from_launch"] == ["NOT_ADDED_PATH", "PATH", "PLANTED", "PWD"]


def test_scrub_text_shapes_paths_and_hides_urls_tokens_and_known_secrets(tmp_path: Path) -> None:
    """A line of host output never prints a path, a URL, a token or a literal the run knows is secret.

    Mutation: skip the URL step, skip the token step or skip the literals. This test fails.
    """

    root = tmp_path / "work"
    ctx = _context(root)
    line = (
        f"ENOENT open '/home/runner/secret-client/x.txt' from https://github.com/acme/secret-client.git "
        f"and file://{_launch(root)}/z with key made-up-key-123 and {fakes.PLANTED_TOKEN}"
    )
    scrubbed = evidence.scrub_text(line, ctx, ["made-up-key-123"])
    assert "secret-client" not in scrubbed and "github.com" not in scrubbed
    assert "made-up-key-123" not in scrubbed and "ghp_" not in scrubbed
    assert "<url:https>" in scrubbed and "file://<launch>/<name>" in scrubbed and "<token>" in scrubbed
    assert len(evidence.scrub_text("x" * 500, ctx)) == 203


# --- the hook command ------------------------------------------------------------------------


def _hook(
    root: Path, stdin: bytes, *, cwd: Path | None = None, env_extra: dict[str, str] | None = None, host: str = "claude-code"
) -> subprocess.CompletedProcess[bytes]:
    context = _write_context(root, ["HOME", "PATH"])
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home"), "PLANTED": PLANTED_ENV_VALUE}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "hook", host, str(root / "art"), str(context)],
        input=stdin,
        cwd=cwd or _launch(root),
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )


def _payload(root: Path) -> bytes:
    return json.dumps(
        {
            "session_id": "0b6c1d52-3a8f-4f43-9a54-5c7a9c7e8d11",
            "transcript_path": str(root / "home" / ".claude" / "projects" / "-x" / "a.jsonl"),
            "cwd": str(_launch(root)),
            "hook_event_name": "SessionStart",
            "source": "startup",
            "note": fakes.PLANTED_SENTENCE,
            "token": fakes.PLANTED_TOKEN,
        }
    ).encode()


def test_hook_command_records_keys_values_cwd_and_env_names_without_raw_values(tmp_path: Path) -> None:
    """The hook saves every stdin key, a redacted value for each, its working folder and its variable names.

    It prints nothing, exits 0, and the record holds no raw path, token, sentence or variable value.
    Mutation: save the raw stdin or the raw environment, or print to stdout. This test fails.
    """

    root = tmp_path / "secret-client-checkout"
    done = _hook(root, _payload(root), env_extra={"CLAUDE_PROJECT_DIR": str(_launch(root))})
    assert done.returncode == 0 and done.stdout == b""
    files = sorted((root / "art").iterdir())
    assert [file.name for file in files] == ["claude-code-hook-1.json"]
    text = files[0].read_text(encoding="utf-8")
    record = json.loads(text)
    stdin = record["stdin"]
    assert stdin["valid_json"] is True and stdin["top_level"] == "dict"
    assert stdin["keys"] == ["session_id", "transcript_path", "cwd", "hook_event_name", "source", "note", "token"]
    assert stdin["path_valued_keys"] == ["transcript_path", "cwd"]
    assert stdin["values"]["cwd"] == "<launch>"
    assert stdin["values"]["transcript_path"] == "<home>/.claude/projects/<name>/<name>.jsonl"
    assert stdin["values"]["hook_event_name"] == "SessionStart" and stdin["values"]["source"] == "startup"
    assert stdin["cwd_key"] == {"present": True, "type": "str", "absolute": True, "shape": "<launch>", "relation": "launch_dir"}
    assert record["cwd"] == {"available": True, "shape": "<launch>", "relation": "launch_dir", "git_levels_up": None}
    assert "CLAUDE_PROJECT_DIR" in record["env"]["added_by_host"]
    assert record["env"]["path_variables"]["CLAUDE_PROJECT_DIR"]["relation"] == "launch_dir"
    assert record["probes"] == {"launch_env_forwarded": False, "entry_env_forwarded": False}
    for raw in FORBIDDEN + (str(tmp_path),):
        assert raw not in text


def test_hook_command_survives_bad_input_and_a_missing_context_and_a_huge_payload(tmp_path: Path) -> None:
    """A hook never breaks its host: not JSON, no context file, and a 1.5 MiB payload all exit 0.

    A payload past the cap is drained, so the host's write never breaks, and the record says it was
    cut at the cap. Mutation: exit 1 on bad JSON, stop reading at the cap without draining the rest,
    keep more than the cap, or fail when the context file is missing. This test fails.
    """

    root = tmp_path / "work"
    bad = _hook(root, b"this is not json")
    assert bad.returncode == 0
    record = json.loads((root / "art" / "claude-code-hook-1.json").read_text(encoding="utf-8"))
    assert record["stdin"] == {"bytes": 16, "truncated": False, "valid_json": False}
    (root / "context.json").unlink()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "hook", "codex", str(root / "art"), str(root / "missing.json")],
        input=json.dumps({"cwd": str(_launch(root))}).encode(),
        cwd=_launch(root),
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0
    codex = json.loads((root / "art" / "codex-hook-1.json").read_text(encoding="utf-8"))
    assert codex["stdin"]["values"]["cwd"].startswith("<abs>/")
    # The writer sends 1.5 MiB in chunks. If the hook stopped reading at the cap, a later write would
    # fail with a broken pipe, which is what a host sees when its hook does not drain stdin.
    context = _write_context(root, ["HOME", "PATH"])
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "hook", "claude-code", str(root / "art"), str(context)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=_launch(root),
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home")},
    )
    assert proc.stdin is not None
    proc.stdin.write(b'{"pad":"')
    for _ in range(48):
        proc.stdin.write(b"x" * (32 * 1024))
    proc.stdin.write(b'"}')
    proc.stdin.close()
    assert proc.wait(timeout=60) == 0
    assert proc.stdout is not None
    proc.stdout.close()
    cut = json.loads((root / "art" / "claude-code-hook-2.json").read_text(encoding="utf-8"))
    assert cut["stdin"]["truncated"] is True and cut["stdin"]["valid_json"] is False
    assert cut["stdin"]["bytes"] == 1024 * 1024


def test_each_hook_firing_is_its_own_numbered_record(tmp_path: Path) -> None:
    """Two firings leave two records, so a double fire reads as two.

    Mutation: write every firing to one file name. This test fails.
    """

    root = tmp_path / "work"
    _hook(root, _payload(root))
    _hook(root, _payload(root))
    assert sorted(path.name for path in (root / "art").iterdir()) == [
        "claude-code-hook-1.json",
        "claude-code-hook-2.json",
    ]


# --- the stub MCP server ---------------------------------------------------------------------


class _Mcp:
    """Drives ``real_host_evidence.py mcp`` over stdio the way a client does."""

    def __init__(self, root: Path, *, wait: float = 5.0, env_extra: dict[str, str] | None = None) -> None:
        self.root = root
        context = _write_context(root, ["HOME", "PATH"])
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home")}
        env.update(env_extra or {})
        self.proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "mcp", "claude-code", str(root / "art"), str(context), "--roots-wait", str(wait)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            cwd=_launch(root),
            env=env,
        )
        self.seen: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def read(self, timeout: float = 20.0) -> dict[str, Any]:
        """The next JSON-RPC line, or a failed assertion. A server that goes quiet must not hang the suite."""

        assert self.proc.stdout is not None
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            self.proc.kill()
            raise AssertionError(f"the stub sent nothing for {timeout} seconds; it sent {self.seen}")
        line = self.proc.stdout.readline()
        assert line, f"the stub closed its output; it sent {self.seen}"
        message = json.loads(line)
        self.seen.append(message)
        return message

    def initialize(self, capabilities: dict[str, Any], *, info: dict[str, Any] | None = None) -> dict[str, Any]:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": capabilities,
                    "clientInfo": info or {"name": "test-client", "version": "9.8.7"},
                },
            }
        )
        reply = self.read()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert self.read()["id"] == 2
        return reply

    def record(self) -> dict[str, Any]:
        return json.loads((self.root / "art" / "claude-code-mcp.json").read_text(encoding="utf-8"))

    def finish(self) -> dict[str, Any]:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            assert self.proc.wait(timeout=30) == 0
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise
        return self.record()


def test_mcp_stub_records_initialize_and_the_roots_answer(tmp_path: Path) -> None:
    """The stub records the client, every capability name, whether roots and elicitation are declared,
    and what ``roots/list`` returns, as a shape and a relation to the launch folder.

    Its stdout holds only JSON-RPC lines. Mutation: record ``declares.roots`` as false, read the
    capability from the wrong key, drop the roots answer, or print a line that is not JSON. This
    test fails.
    """

    root = tmp_path / "secret-client-checkout"
    server = _Mcp(root, env_extra={"ALICE_EVIDENCE_ENTRY_PROBE": "entry-probe-value"})
    reply = server.initialize(
        {
            "roots": {"listChanged": True},
            "elicitation": {"form": {}},
            "experimental": {"note": fakes.PLANTED_SENTENCE, "items": [fakes.PLANTED_TOKEN], "level": 2},
        },
        info={"name": "test-client", "version": "9.8.7", "title": fakes.PLANTED_SENTENCE},
    )
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    probe = server.read()
    assert probe["method"] == "roots/list" and probe["id"] == "alice-evidence-roots-1"
    server.send(
        {
            "jsonrpc": "2.0",
            "id": probe["id"],
            "result": {"roots": [{"uri": "file://" + str(_launch(root)), "name": "app"}]},
        }
    )
    record = server.finish()
    text = json.dumps(record)
    init = record["initialize"]
    assert init["client_info"] == {
        "name": "test-client",
        "version": "9.8.7",
        "title": f"<text:{len(fakes.PLANTED_SENTENCE)} chars>",
    }
    assert init["capabilities"]["experimental"] == {
        "note": f"<text:{len(fakes.PLANTED_SENTENCE)} chars>",
        "items": "<list of 1>",
        "level": 2,
    }
    assert init["capabilities"]["roots"] == {"listChanged": True}
    assert init["protocol_version"] == "2025-06-18"
    assert init["capability_names"] == ["roots", "elicitation", "experimental"]
    assert init["declares"] == {
        "roots": True,
        "roots_list_changed": True,
        "elicitation": True,
        "elicitation_modes": ["form"],
        "sampling": False,
    }
    roots = record["roots_list"]
    assert roots["sent"] is True and roots["outcome"] == "answered" and roots["root_count"] == 1
    assert roots["roots"] == [
        {"uri_type": "str", "uri_scheme": "file", "uri_shape": "file://<launch>", "relation": "launch_dir", "name_kind": "basename"}
    ]
    assert record["complete"] is True and record["stdin_closed"] is True
    assert record["client_requests"] == ["initialize", "notifications/initialized", "tools/list"]
    assert record["cwd"] == {"available": True, "shape": "<launch>", "relation": "launch_dir", "git_levels_up": None}
    assert record["probes"]["entry_env_forwarded"] is True and record["probes"]["launch_env_forwarded"] is False
    assert all(isinstance(message, dict) and message["jsonrpc"] == "2.0" for message in server.seen)
    assert "secret-client" not in text and str(tmp_path) not in text
    assert fakes.PLANTED_SENTENCE not in text and fakes.PLANTED_TOKEN not in text


def test_mcp_stub_asks_for_roots_even_when_none_are_declared_and_records_silence(tmp_path: Path) -> None:
    """A client that declares nothing is still asked, and a client that never answers is recorded as such.

    Mutation: send ``roots/list`` only when the client declared roots, or wait forever for an answer.
    This test fails.
    """

    root = tmp_path / "work"
    server = _Mcp(root, wait=0.5)
    server.initialize({})
    assert server.read()["method"] == "roots/list"
    time.sleep(1.2)
    mid = server.record()
    assert mid["complete"] is True
    record = server.finish()
    assert record["initialize"]["declares"]["roots"] is False
    assert record["roots_list"]["sent"] is True and record["roots_list"]["outcome"] == "no_reply"
    assert record["roots_list"]["waited_seconds"] >= 0.5


def test_mcp_stub_records_an_error_answer_without_the_paths_in_its_message(tmp_path: Path) -> None:
    """A method-not-found answer is recorded by code, with its message scrubbed.

    Mutation: count an error as an answer, or keep the message text. This test fails.
    """

    root = tmp_path / "secret-client-checkout"
    server = _Mcp(root)
    server.initialize({})
    probe = server.read()
    server.send(
        {
            "jsonrpc": "2.0",
            "id": probe["id"],
            "error": {"code": -32601, "message": f"Method not found at /srv/secret-client/{fakes.PLANTED_TOKEN}"},
        }
    )
    record = server.finish()
    roots = record["roots_list"]
    assert roots["outcome"] == "error" and roots["error"]["code"] == -32601
    assert "roots" not in roots
    text = json.dumps(record)
    assert "secret-client" not in text and "ghp_" not in text


def test_mcp_stub_notes_a_client_that_closes_before_the_probe_resolves(tmp_path: Path) -> None:
    """If the client leaves first, the record is complete and says why, so a waiting API stub is released.

    Mutation: leave ``complete`` false when stdin closes. This test fails.
    """

    root = tmp_path / "work"
    server = _Mcp(root, wait=30)
    server.initialize({})
    server.read()
    record = server.finish()
    assert record["complete"] is True
    assert record["roots_list"]["reason"] == "the client closed the connection first"


# --- a whole run against stand-ins for the hosts ---------------------------------------------


@pytest.fixture
def stand_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Put the stand-ins first on PATH, and prove ``claude`` and ``codex`` resolve to them."""

    bin_dir = tmp_path / "fake-bin"
    written = fakes.install_fakes(bin_dir)
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_MARKERS", str(markers))
    monkeypatch.setenv("FAKE_REPO_ROOT", str(ROOT))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-looking-key-must-not-reach-a-host")
    monkeypatch.setenv("CODEX_API_KEY", "real-looking-key-must-not-reach-a-host")
    monkeypatch.setenv("PLANTED", PLANTED_ENV_VALUE)
    monkeypatch.setenv("PLANTED_SERVICE_TOKEN", "service-token-must-not-reach-a-host")
    for name in ("claude", "codex"):
        assert shutil.which(name) == str(written[name]), f"{name} does not resolve to the stand-in"
    return written


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "answer", **options: Any
) -> tuple[int, Path, dict[str, Any]]:
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("FAKE_MODE", mode)
    artifacts = tmp_path / "art"
    options.setdefault("require_ci", False)
    options.setdefault("roots_wait", 1.0)
    options.setdefault("api_wait", 30.0)
    code = evidence.run(artifacts, tmp_path / "secret-client-workdir", **options)
    report_path = artifacts / "host-evidence.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    return code, artifacts, report


def _assert_nothing_raw(tmp_path: Path, artifacts: Path, *extra: Path) -> None:
    forbidden = FORBIDDEN + (str(tmp_path), str(tmp_path.resolve()), "secret-client-workdir", fakes.PLANTED_STDERR_PATH)
    files = [path for path in artifacts.rglob("*") if path.is_file()] + [path for path in extra if path.is_file()]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for raw in forbidden:
            assert raw not in text, (path.name, raw)


def test_run_records_both_hosts_and_keeps_no_raw_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """One run against both stand-in hosts records the hook and the server for each, as shapes only.

    It checks the claims the slice is for: the keys and redacted values of the hook payload, the
    working folder of each process, the variable names added by the host, whether a variable set at
    launch and one set in the server's entry arrive, the initialize capabilities, and what
    ``roots/list`` returns. It then scans every artifact, the step summary and the exit code for a
    planted client folder, token, sentence, key, variable value and path. A made-up key replaces
    any real one, and no credential-like variable of the runner reaches a host. The stub API lets
    the host go as soon as the roots probe is done. Mutation: let any record, the summary or the
    report carry a raw value, pass the runner's credential-like variables on, leave a real key in
    the host's environment, or hold the host for the stub's full wait. This test fails.
    """

    code, artifacts, report = _run(tmp_path, monkeypatch)
    assert code == 0, report.get("headline")
    claude, codex = report["hosts"]["claude-code"], report["hosts"]["codex"]
    assert claude["version"] == "2.1.281 (Claude Code)" and claude["pinned"] is True
    assert codex["version"] == "codex-cli 0.158.0" and codex["pinned"] is True
    assert claude["launched_in"] == {"placeholder": "<launch>", "git_levels_up": 2}
    for info in (claude, codex):
        assert info["problems"] == []
        assert info["hook"]["fired"] == 1
        hook = info["hook"]["records"][0]
        assert hook["cwd"]["relation"] == "launch_dir" and hook["cwd"]["git_levels_up"] == 2
        assert hook["stdin"]["cwd_key"]["relation"] == "launch_dir"
        assert "cwd" in hook["stdin"]["path_valued_keys"]
        assert hook["probes"]["launch_env_forwarded"] is True
        server = info["mcp"]["record"]
        assert server["cwd"]["relation"] == "launch_dir"
        assert server["initialize"]["declares"]["roots"] is True
        assert server["initialize"]["declares"]["elicitation"] is True
        assert server["roots_list"]["outcome"] == "answered"
        assert server["roots_list"]["roots"][0]["relation"] == "launch_dir"
        assert server["probes"]["entry_env_forwarded"] is True
        assert server["complete"] is True
    assert "CLAUDE_PROJECT_DIR" in claude["hook"]["records"][0]["env"]["added_by_host"]
    assert claude["hook"]["records"][0]["env"]["path_variables"]["CLAUDE_PROJECT_DIR"]["relation"] == "launch_dir"
    assert claude["hook"]["records"][0]["stdin"]["keys"] == [
        "session_id", "transcript_path", "cwd", "hook_event_name", "source", "note", "token", "nested",
    ]
    assert claude["hook"]["records"][0]["stdin"]["values"]["nested"] == {"key": f"<token:{len(fakes.PLANTED_KEY)} chars>", "count": 3, "flag": True}
    assert codex["hook"]["records"][0]["stdin"]["values"]["transcript_path"] is None
    assert claude["mcp"]["record"]["probes"]["launch_env_forwarded"] is True
    assert codex["mcp"]["record"]["probes"]["launch_env_forwarded"] is False
    assert codex["mcp"]["record"]["env"]["dropped_from_launch"], "the stand-in codex drops launch names"
    assert claude["run"]["init_event"]["cwd"]["relation"] == "launch_dir"
    assert claude["run"]["init_event"]["mcp_servers"] == [{"name": "alice-evidence", "status": "connected"}]
    assert claude["run"]["hook_events"] == [{"type": "system", "subtype": "hook_response", "outcome": "success", "exit_code": 0}]
    assert ("POST", "/v1/messages") in [tuple(item) for item in claude["run"]["stub_requests"]]
    assert ("POST", "/v1/responses") in [tuple(item) for item in codex["run"]["stub_requests"]]
    assert not (tmp_path / "markers" / "saw-real-key").exists()
    assert not (tmp_path / "markers" / "saw-token").exists()
    # The stub API releases the host as soon as the server's roots probe is done. A hold that ran
    # to its 30 second limit would show here, long before the host's own timeout.
    for info in (claude, codex):
        assert info["run"]["wall_ms"] < 20_000, info["run"]["wall_ms"]
    summary = tmp_path / "step-summary.md"
    assert summary.read_text(encoding="utf-8") == (artifacts / "host-evidence.md").read_text(encoding="utf-8")
    assert sorted(path.name for path in artifacts.iterdir()) == [
        "claude-code-hook-1.json",
        "claude-code-mcp.json",
        "codex-hook-1.json",
        "codex-mcp.json",
        "host-evidence.json",
        "host-evidence.md",
    ]
    _assert_nothing_raw(tmp_path, artifacts, summary)


def test_summary_shows_the_answers_in_words_and_hides_no_placeholder_from_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """The step summary names each answer, and wraps every placeholder in code so GitHub does not hide it.

    A bare ``<launch>`` is read as an HTML tag and vanishes. Mutation: print a placeholder outside a
    code span, or drop the headline. This test fails.
    """

    _code, artifacts, _report = _run(tmp_path, monkeypatch)
    summary = (artifacts / "host-evidence.md").read_text(encoding="utf-8")
    assert "### Headline" in summary and "#### SessionStart hook, fired 1 time(s)" in summary
    assert "roots capability declared: yes. elicitation declared: yes." in summary
    assert "roots/list sent after initialize, declared or not: outcome `answered`" in summary
    assert "`cwd` key: present, absolute: yes, `<launch>`, launch_dir" in summary
    assert "A variable set in the launch environment reached the hook: yes" in summary
    assert "| `session_id` | `<uuid>` |" in summary
    without_code = re.sub(r"`[^`]*`", "", summary)
    assert "<" not in without_code, re.findall(r".*<.*", without_code)[:3]
    assert "\u2014" not in summary and "\u2013" not in summary


def test_run_records_a_client_that_declares_nothing_and_answers_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """A host with no roots support is a finding, not a failure: the run exits 0 and says so.

    Mutation: treat an error answer as roots, or fail the run for it. This test fails.
    """

    code, artifacts, report = _run(tmp_path, monkeypatch, mode="error")
    assert code == 0
    for host in ("claude-code", "codex"):
        server = report["hosts"][host]["mcp"]["record"]
        assert server["initialize"]["declares"]["roots"] is False
        assert server["roots_list"]["outcome"] == "error" and server["roots_list"]["error"]["code"] == -32601
        assert server["roots_list"]["sent"] is True
    summary = (artifacts / "host-evidence.md").read_text(encoding="utf-8")
    assert "roots capability declared: no." in summary
    _assert_nothing_raw(tmp_path, artifacts)


def test_run_records_a_client_that_never_answers_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """A silent client is recorded as ``no_reply`` after the wait, and the host is not held forever.

    Mutation: report ``answered`` for no answer, or skip the wait limit. This test fails.
    """

    code, artifacts, report = _run(tmp_path, monkeypatch, mode="silent")
    assert code == 0
    for host in ("claude-code", "codex"):
        server = report["hosts"][host]["mcp"]["record"]
        assert server["roots_list"]["outcome"] == "no_reply"
        assert server["complete"] is True
    _assert_nothing_raw(tmp_path, artifacts)


def test_run_fails_loudly_when_the_hook_never_fires_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """A hook that does not fire is a problem the job reports, with the server evidence still written.

    Mutation: drop the no-fire problem, or stop recording the server when the hook is missing.
    This test fails.
    """

    code, artifacts, report = _run(tmp_path, monkeypatch, mode="no_hook")
    assert code == 1
    for host in ("claude-code", "codex"):
        info = report["hosts"][host]
        assert "the SessionStart hook did not fire" in info["problems"]
        assert info["hook"]["fired"] == 0
        assert info["mcp"]["record"]["roots_list"]["outcome"] == "answered"
    summary = (artifacts / "host-evidence.md").read_text(encoding="utf-8")
    assert "### Problems" in summary and "hook did not fire" in summary


def test_run_does_not_start_a_host_that_is_not_the_pinned_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """An unpinned host is reported and never launched, so the evidence is always for the pinned version.

    Mutation: launch whatever version is installed. This test fails.
    """

    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "9.9.9 (Claude Code)")
    code, artifacts, report = _run(tmp_path, monkeypatch)
    assert code == 1
    claude = report["hosts"]["claude-code"]
    assert claude["pinned"] is False and claude["run"] is None and claude["hook"]["fired"] == 0
    assert claude["problems"] == ["claude is not the pinned version 2.1.281 (Claude Code)"]
    assert report["hosts"]["codex"]["problems"] == []
    assert not (artifacts / "claude-code-mcp.json").exists()


def test_run_refuses_to_start_a_host_outside_github_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """Outside Actions the script starts no host and writes nothing, so it cannot run a real host on a laptop.

    Mutation: remove the check. This test fails.
    """

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    artifacts = tmp_path / "art"
    assert evidence.run(artifacts, tmp_path / "work") == 2
    assert not artifacts.exists() and not (tmp_path / "work").exists()
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    assert evidence.main(["run", str(artifacts), str(tmp_path / "work")]) == 2
    assert not artifacts.exists()


def test_run_refuses_a_non_empty_artifacts_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_ins: dict[str, Path]
) -> None:
    """A stale file cannot pass for evidence. Mutation: run into a non-empty directory. This test fails."""

    artifacts = tmp_path / "art"
    artifacts.mkdir()
    (artifacts / "stale.json").write_text("{}", encoding="utf-8")
    assert evidence.run(artifacts, tmp_path / "work", require_ci=False) == 1
    assert [path.name for path in artifacts.iterdir()] == ["stale.json"]


# --- the pieces the real hosts depend on -----------------------------------------------------


def test_codex_trust_hash_and_reply_match_the_real_host_test() -> None:
    """The script's copies equal the ones the real-Codex test checks against ``hooks/list``.

    Mutation: leave ``async`` out of the hash, hash the default limit, or change a byte of the
    mock reply. This test fails.
    """

    from tests.unit import test_codex_hook_real_host as real

    for kwargs in (
        {},
        {"timeout": 120},
        {"timeout": 120, "limit": 0},
        {"timeout": 120, "limit": 2500},
        {"timeout": 0, "limit": 9},
    ):
        assert evidence.codex_trust_hash("some command", **kwargs) == real.trust_hash("some command", **kwargs)
    for index in (1, 2, 7):
        assert evidence._codex_reply(index) == real._reply(index)


def test_main_dispatches_only_the_three_commands_and_rejects_the_rest(tmp_path: Path) -> None:
    """Anything but ``run``, ``hook`` and ``mcp`` with their arguments is a usage error.

    Mutation: accept an unknown host name or a malformed ``--roots-wait``. This test fails.
    """

    assert evidence.main([]) == 2
    assert evidence.main(["hook", "cursor", str(tmp_path), str(tmp_path / "c.json")]) == 2
    assert evidence.main(["mcp", "codex", str(tmp_path), str(tmp_path / "c.json"), "--roots-wait", "soon"]) == 2
    assert evidence.main(["mcp", "codex", str(tmp_path), str(tmp_path / "c.json"), "--wait", "1"]) == 2
    assert evidence.main(["run", str(tmp_path)]) == 2
