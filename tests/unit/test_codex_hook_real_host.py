"""The real Codex runs the SessionStart hook install wrote, and injects the brief.

Opt-in: ALICE_TEST_REAL_HOSTS=1 on Linux with ``codex`` on PATH (the pinned
real-host job installs ``@openai/codex@0.158.0``). Nothing here reaches the
network. ``codex exec`` talks to a loopback mock of ``/v1/responses`` that
answers with one assistant message and records each request body, so the
test can read what Codex put in front of the model.

How the trust hash is made: this file computes it, per
``hooks/src/engine/discovery.rs`` (``hook_hash``) and
``config/src/fingerprint.rs`` (``version_for_toml``): ``sha256:`` of the
compact JSON, keys sorted, of ``{"event_name": "session_start", "hooks":
[<normalized handler>]}``. The normalized handler has ``type``, ``command``,
``timeout`` (600 when unset), ``async`` and, when it is not the default
2,500, ``additionalContextLimit``. ``codex app-server`` ``hooks/list`` runs
without credentials, so the test also asks it for ``currentHash`` and
``trustStatus`` and compares. When the app-server cannot be read those
checks fail and print why (``Rig.alice_hook`` and ``Rig.listed_hooks`` assert
it). Only control 2, which expects Codex to list no hook at all, tolerates an
unreadable answer, and says why.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from alicebot_api.host_install import host_file_map
from alicebot_api.onramp import main as onramp_main
from tests.unit.codex_hook_helpers import NUMBER_CASES, commit_fact, number_case_document
from tests.unit.launcher_helpers import pin_launcher_search

REAL_HOSTS_ENV = "ALICE_TEST_REAL_HOSTS"
requires_real_codex = pytest.mark.skipif(
    os.environ.get(REAL_HOSTS_ENV) != "1"
    or sys.platform != "linux"
    or shutil.which("codex") is None,
    reason="set ALICE_TEST_REAL_HOSTS=1 on Linux with codex on PATH",
)

LINE_A = "quartz-heron-8842"
LINE_B = "basalt-otter-3170"
FRAME = "Stored notes from Alice memory"
TRUST_NEXT = (
    'next: open Codex. At "Hooks need review", choose Review hooks and trust the '
    "alice-memory-session-start hook, or use /hooks. Until then Codex skips it "
    "without a message."
)
MODIFIED = "The hook changed, so Codex will skip it until you trust it again."
WRITTEN = "session_start: written to hooks.json, not trusted yet"
UNCHANGED = "session_start: unchanged in hooks.json (Codex runs it only if you have trusted it)"
DEFAULT_LIMIT = 2500


# --- the trust hash -------------------------------------------------------------------------


def trust_hash(command: str, *, timeout: int | None = None, limit: int | None = None) -> str:
    """The hash Codex keeps for a command handler with no matcher and no statusMessage."""

    handler: dict[str, object] = {
        "type": "command",
        "command": command,
        "timeout": 600 if timeout is None else max(timeout, 1),
        "async": False,
    }
    if limit is not None and limit != DEFAULT_LIMIT:
        handler["additionalContextLimit"] = limit
    identity = {"event_name": "session_start", "hooks": [handler]}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_trust_hash_is_the_canonical_form_discovery_rs_hashes() -> None:
    """Pin the canonical JSON the hash is taken over, so an edit to it is noticed.

    Mutation: leave ``async`` out, or hash the default limit. This test fails.
    The real-host tests below are what show Codex agrees.
    """

    canonical = json.dumps(
        {
            "event_name": "session_start",
            "hooks": [
                {
                    "type": "command",
                    "command": "x",
                    "timeout": 120,
                    "async": False,
                    "additionalContextLimit": 0,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert canonical == (
        '{"event_name":"session_start","hooks":[{"additionalContextLimit":0,"async":false,'
        '"command":"x","timeout":120,"type":"command"}]}'
    )
    assert trust_hash("x", timeout=120, limit=0) == "sha256:" + hashlib.sha256(
        canonical.encode()
    ).hexdigest()
    assert trust_hash("x", timeout=120, limit=DEFAULT_LIMIT) == trust_hash("x", timeout=120)
    assert trust_hash("x") != trust_hash("x", timeout=120)


# --- a loopback stand-in for /v1/responses --------------------------------------------------


def _sse(events: list[dict]) -> str:
    """Server-sent events the way Codex's own test helper writes them."""

    out: list[str] = []
    for event in events:
        out.append(f"event: {event['type']}\n")
        if len(event) == 1:
            out.append("\n")
        else:
            out.append(f"data: {json.dumps(event, separators=(',', ':'))}\n\n")
    return "".join(out)


def _reply(index: int) -> bytes:
    response_id = f"resp-{index}"
    return _sse(
        [
            {"type": "response.created", "response": {"id": response_id}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg-{index}",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage": {
                        "input_tokens": 0,
                        "input_tokens_details": None,
                        "output_tokens": 0,
                        "output_tokens_details": None,
                        "total_tokens": 0,
                    },
                },
            },
        ]
    ).encode("utf-8")


class ResponsesServer:
    """Answers ``POST .../responses`` with one assistant message and records the request body."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.other: list[str] = []
        self.errors: list[str] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # Codex tries a WebSocket first. An HTTP/1.0 answer to that upgrade is a protocol
            # error that it retries; an HTTP/1.1 404 is a refusal it falls back from at once.
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def do_POST(self) -> None:  # noqa: N802
                raw = self._body()
                if not self.path.split("?", 1)[0].endswith("/responses"):
                    owner.other.append(f"POST {self.path}")
                    self.send_error(404)
                    return
                try:
                    encoding = (self.headers.get("Content-Encoding") or "identity").lower()
                    if encoding == "gzip":
                        raw = gzip.decompress(raw)
                    elif encoding != "identity":
                        raise ValueError(f"request encoding {encoding} is not handled")
                    body = json.loads(raw)
                    owner.requests.append({"path": self.path, "body": body})
                    payload = _reply(len(owner.requests))
                except Exception as problem:  # noqa: BLE001
                    owner.errors.append(f"{type(problem).__name__}: {problem}")
                    self.send_error(500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(payload)
                self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                owner.other.append(f"GET {self.path}")
                self.send_error(404)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


def developer_texts(request: dict) -> list[str]:
    """Every text part of every developer message in a request's input."""

    body = request["body"]
    items = body.get("input") if isinstance(body, dict) else None
    texts: list[str] = []
    for item in items if isinstance(items, list) else []:
        if not (isinstance(item, dict) and item.get("type") == "message"):
            continue
        if item.get("role") != "developer":
            continue
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                part["text"]
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return texts


def request_tools(request: dict) -> list[dict]:
    """The tools a request offers.

    The default model in codex 0.158.0 uses "responses lite", which sends the
    tools as an ``additional_tools`` item at the start of ``input`` and no top
    level ``tools``. Older models send ``tools``. Both are read.
    """

    body = request["body"]
    if not isinstance(body, dict):
        return []
    found: list[dict] = []
    top = body.get("tools")
    if isinstance(top, list):
        found.extend(tool for tool in top if isinstance(tool, dict))
    items = body.get("input")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            listed = item.get("tools")
            if isinstance(listed, list):
                found.extend(tool for tool in listed if isinstance(tool, dict))
    return found


def has_alice_recall(request: dict) -> bool:
    """True when the request carries a ``namespace`` tool ``mcp__alice...`` with child ``alice_recall``."""

    for tool in request_tools(request):
        if tool.get("type") != "namespace" or not str(tool.get("name", "")).startswith("mcp__alice"):
            continue
        children = tool.get("tools")
        if any(
            isinstance(child, dict) and child.get("name") == "alice_recall"
            for child in children or []
        ):
            return True
    return False


# --- codex's own view of the hooks, when its app-server answers ------------------------------


def codex_hooks(env: dict[str, str], cwd: Path) -> tuple[list[dict], list[str]] | str:
    """``(hooks, warnings)`` from ``codex app-server`` ``hooks/list``, or why it could not be read."""

    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as problem:
        return f"could not start codex app-server: {problem}"
    lines: queue.Queue[str | None] = queue.Queue()
    stderr_lines: list[str] = []

    def pump_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def pump_stderr() -> None:
        assert proc.stderr is not None
        stderr_lines.extend(proc.stderr)

    for target in (pump_stdout, pump_stderr):
        threading.Thread(target=target, daemon=True).start()

    def send(message: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def response(request_id: int) -> dict | None:
        while True:
            try:
                line = lines.get(timeout=90)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "alice-test", "title": None, "version": "0"}},
            }
        )
        started = response(1)
        if started is None or "error" in started:
            return f"initialize failed: {started} stderr={''.join(stderr_lines)[-600:]}"
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}})
        listed = response(2)
        if listed is None or "error" in listed:
            return f"hooks/list failed: {listed} stderr={''.join(stderr_lines)[-600:]}"
        entries = listed["result"]["data"]
        entry = entries[0] if entries else {"hooks": [], "warnings": [], "errors": []}
        if entry.get("errors"):
            return f"hooks/list reported errors: {entry['errors']}"
        return list(entry.get("hooks") or []), list(entry.get("warnings") or [])
    except (OSError, KeyError, TypeError, ValueError) as problem:
        return f"hooks/list could not be read: {type(problem).__name__}: {problem}"
    finally:
        proc.kill()
        proc.wait(timeout=30)


# --- the rig --------------------------------------------------------------------------------


@dataclass
class Run:
    """One ``codex exec`` run: the process and the requests it sent to the mock."""

    proc: subprocess.CompletedProcess[str]
    requests: list[dict]
    other: list[str] = field(default_factory=list)

    def developer(self) -> list[str]:
        return [text for request in self.requests for text in developer_texts(request)]

    def everything(self) -> str:
        return json.dumps([request["body"] for request in self.requests])

    def alice_context(self) -> str:
        """The developer message that carries the brief, or an empty string."""

        return next((text for text in self.developer() if FRAME in text), "")

    def summary(self) -> str:
        return (
            f"exit={self.proc.returncode} requests={len(self.requests)} "
            f"developer_messages={len(self.developer())}"
        )

    def describe(self) -> str:
        roles = [
            [
                f"{item.get('type')}:{item.get('role')}"
                for item in request["body"].get("input", [])
                if isinstance(item, dict)
            ]
            for request in self.requests
        ]
        tools = [
            [f"{tool.get('type')}:{tool.get('name')}" for tool in request_tools(request)]
            for request in self.requests
        ]
        return (
            f"exit={self.proc.returncode} requests={len(self.requests)} other={self.other}\n"
            f"input roles={roles}\ntools={tools}\n"
            f"stdout={self.proc.stdout[-1500:]!r}\nstderr={self.proc.stderr[-2500:]!r}"
        )


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        server: ResponsesServer,
    ) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.server = server
        self.evidence: list[str] = []
        self.home = tmp_path / "home"
        self.cwd = tmp_path / "cwd"
        self.cwd.mkdir()
        (self.home / ".codex").mkdir(parents=True)
        (tmp_path / "sqlite").mkdir()
        scripts = Path(sys.executable).parent
        assert (scripts / "alice-memory").is_file() and (scripts / "alice-memory-session-start").is_file(), (
            f"the real alice-memory scripts are not in {scripts}"
        )
        pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=scripts)
        files = host_file_map(self.home.resolve())["codex"]
        self.config = files["mcp"]
        self.hooks = files["hooks"]
        env = os.environ.copy()
        for name in list(env):
            if name.startswith(("CODEX_", "OPENAI_", "ALICE_")):
                env.pop(name)
        env.pop("PYTHONPATH", None)
        env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.home / ".codex"),
                "CODEX_API_KEY": "dummy-" + secrets.token_hex(8),
                "CODEX_SQLITE_HOME": str(tmp_path / "sqlite"),
            }
        )
        self.env = env

    def say(self, text: str) -> None:
        """Keep a line of evidence. ``install`` reads and clears the captured output, so a print
        made before it would be lost; ``flush`` prints them all at the end."""

        self.evidence.append(text)

    def flush(self) -> None:
        print("\n".join(self.evidence))

    def install(self, vault: Path, *extra: str) -> tuple[int, str, str]:
        code = onramp_main(
            ["install", "--home", str(self.home), "--data-dir", str(vault), "--host", "codex", *extra]
        )
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def run(self, *extra: str) -> Run:
        before = len(self.server.requests)
        seen_other = len(self.server.other)
        proc = subprocess.run(
            [
                "codex",
                "exec",
                "--skip-git-repo-check",
                # The default model in 0.158.0 is code mode only: it lists no MCP tool in its
                # request. gpt-5.5 lists them, as the spec's namespace check expects.
                "-m",
                "gpt-5.5",
                "-c",
                f'openai_base_url="http://127.0.0.1:{self.server.port}/v1"',
                "-c",
                'otel.metrics_exporter="none"',
                "-c",
                'mcp_servers.alice.omit_tools_from=["deferred"]',
                # The MCP server is optional, and Codex waits 1 second for an optional one
                # before it drops it from the first request. Python takes longer to start.
                "-c",
                "mcp_optional_startup_grace_ms=60000",
                *extra,
                "Say ok.",
            ],
            cwd=self.cwd,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        run = Run(proc, self.server.requests[before:], self.server.other[seen_other:])
        assert not self.server.errors, self.server.errors
        assert proc.returncode == 0, run.describe()
        assert run.requests, run.describe()
        return run

    def key(self, group: int, handler: int = 0) -> str:
        return f"{self.hooks.resolve()}:session_start:{group}:{handler}"

    def hash_of(self, group: int, handler: int = 0) -> str:
        document = json.loads(self.hooks.read_text(encoding="utf-8"))
        item = document["hooks"]["SessionStart"][group]["hooks"][handler]
        return trust_hash(
            item["command"], timeout=item.get("timeout"), limit=item.get("additionalContextLimit")
        )

    def trust(self, *groups: int) -> None:
        """Write ``trusted_hash`` for each group's first handler, replacing an older one for that key."""

        text = self.config.read_text(encoding="utf-8") if self.config.exists() else ""
        for group in groups:
            header = f'[hooks.state."{self.key(group)}"]\n'
            older = re.compile(re.escape(header) + r'trusted_hash = "[^"]*"\n')
            text = older.sub("", text).rstrip("\n")
            text += f'\n\n{header}trusted_hash = "{self.hash_of(group)}"\n'
        self.config.write_text(text, encoding="utf-8")

    def codex_hooks(self) -> tuple[list[dict], list[str]] | str:
        return codex_hooks(self.env, self.cwd)

    def listed_hooks(self) -> dict[str, dict]:
        """Every hook Codex's ``hooks/list`` reports, by key. It fails when the app-server is unreadable."""

        listed = self.codex_hooks()
        assert not isinstance(listed, str), listed
        hooks, warnings = listed
        self.say(f"  hooks/list warnings: {warnings}")
        by_key = {str(hook.get("key")): hook for hook in hooks}
        assert len(by_key) == len(hooks), hooks
        return by_key

    def alice_hook(self) -> dict:
        """Codex's ``hooks/list`` entry for Alice's hook.

        ``codex app-server`` answers ``hooks/list`` with no credentials, so this
        is the independent check on the hash and the trust status.
        """

        listed = self.codex_hooks()
        assert not isinstance(listed, str), listed
        hooks, warnings = listed
        self.say(f"  hooks/list warnings: {warnings}")
        found = [hook for hook in hooks if "alice-memory-session-start" in str(hook.get("command"))]
        assert len(found) == 1, (found, hooks)
        return found[0]


@pytest.fixture
def rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[Rig]:
    server = ResponsesServer()
    server.start()
    try:
        yield Rig(tmp_path, monkeypatch, capsys, server)
    finally:
        server.stop()


def _write_json(path: Path, document: object) -> bytes:
    text = json.dumps(document, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return text.encode("utf-8")


def _backups(vault: Path, name: str) -> list[Path]:
    directory = vault / "backups" / "host-configs"
    return sorted(directory.glob(f"codex-{name}.alice-backup-*")) if directory.is_dir() else []


def _group(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


# --- the run --------------------------------------------------------------------------------


@requires_real_codex
def test_real_codex_runs_the_session_start_hook(rig: Rig) -> None:
    """Codex trusts nothing install wrote, runs it once the user trusts it, and skips it when it changes.

    Steps: (1) seed a user hook with a trusted hash, install, the backups equal
    the seeds; (2) a first run, no bypass flag: the user's group runs and
    Alice's does not, as the receipt says; (3) trust Alice's item and run
    again: a developer message holds the known brief line and the alice tools
    are in the request; (4) a user group after Alice's leaves every index
    alone; (5) a new ``--data-dir`` changes the hook, so Codex skips it
    (Modified) and the receipt said so; (6) a dry run masks the hook.
    Mutations: insert Alice's group at index 0, write a ``trusted_hash``, or
    use ``--format json``. This test fails.
    """

    tmp_path, monkeypatch = rig.tmp_path, rig.monkeypatch
    vault_a, vault_b = tmp_path / "vault-a", tmp_path / "vault-b"
    commit_fact(vault_a, monkeypatch, "Hook proof A", f"The hook proof codeword is {LINE_A}.")
    commit_fact(vault_b, monkeypatch, "Hook proof B", f"The hook proof codeword is {LINE_B}.")
    marker = tmp_path / "user-hook-ran"
    second_marker = tmp_path / "second-user-hook-ran"
    user_command = f"touch {shlex.quote(str(marker))}"
    user_group = _group(user_command)

    # 1. Seed and install.
    hooks_seed = _write_json(rig.hooks, {"hooks": {"SessionStart": [user_group]}})
    config_seed = (
        "# kept comment\n"
        "check_for_update_on_startup = false\n"
        "\n"
        f'[hooks.state."{rig.key(0)}"]\n'
        f'trusted_hash = "{trust_hash(user_command)}"\n'
    )
    rig.config.write_text(config_seed, encoding="utf-8")
    code, out, err = rig.install(vault_a)
    assert code == 0, (out, err)
    lines = out.splitlines()
    assert WRITTEN in lines and TRUST_NEXT in lines
    assert [path.read_bytes() for path in _backups(vault_a, "hooks.json")] == [hooks_seed]
    assert [path.read_bytes() for path in _backups(vault_a, "config.toml")] == [config_seed.encode()]
    written = json.loads(rig.hooks.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert written[0] == user_group and len(written) == 2
    assert "--format markdown" in written[1]["hooks"][0]["command"]
    assert "trusted_hash" not in rig.hooks.read_text(encoding="utf-8")
    state = tomllib.loads(rig.config.read_text(encoding="utf-8"))["hooks"]["state"]
    assert list(state) == [rig.key(0)]
    rig.say(f"step 1: installed. hooks.json has {len(written)} SessionStart groups; group 0 is the "
            f"user's, group 1 is Alice's; backups equal the seeds")
    rig.say(f"  alice command: {written[1]['hooks'][0]['command'].replace(str(tmp_path), '<tmp>')}")

    # 2. First run, no bypass flag: Alice's hook is untrusted.
    first = rig.run()
    assert marker.exists(), first.describe()
    assert not any(LINE_A in text for text in first.developer()), first.describe()
    assert LINE_A not in first.everything(), first.describe()
    listed = rig.alice_hook()
    assert listed["key"] == rig.key(1), listed
    assert listed["trustStatus"] == "untrusted", listed
    assert listed["currentHash"] == rig.hash_of(1), (listed, rig.hash_of(1))
    assert listed["additionalContextLimit"] == 0 and listed["timeoutSec"] == 120, listed
    rig.say(
        f"step 2: {first.summary()}; the user's marker was written; no brief line "
        f"({LINE_A}) in any developer message"
    )
    rig.say(
        f"  hooks/list: trustStatus={listed['trustStatus']} "
        f"currentHash={listed['currentHash']} (equals the hash computed here)"
    )

    # 3. Trust Alice's item, run again without the bypass flag.
    rig.trust(1)
    marker.unlink()
    second = rig.run()
    assert marker.exists(), second.describe()
    assert any(LINE_A in text for text in second.developer()), second.describe()
    assert any(has_alice_recall(request) for request in second.requests), second.describe()
    listed = rig.alice_hook()
    assert listed["trustStatus"] == "trusted", listed
    rig.say(
        f"step 3: {second.summary()}; a developer message holds {LINE_A}: "
        f"{second.alice_context()[:170]!r}"
    )
    rig.say(
        "  tools: "
        + ", ".join(
            f"{tool.get('name')}[{','.join(str(child.get('name')) for child in tool.get('tools') or [])}]"
            for tool in request_tools(second.requests[0])
            if str(tool.get("name", "")).startswith("mcp__alice")
        )
    )
    rig.say(f"  hooks/list: trustStatus={listed['trustStatus']}")

    # 4. A second user group after Alice's, install again with nothing changed.
    document = json.loads(rig.hooks.read_text(encoding="utf-8"))
    document["hooks"]["SessionStart"].append(_group(f"touch {shlex.quote(str(second_marker))}"))
    after_user_edit = _write_json(rig.hooks, document)
    code, out, err = rig.install(vault_a)
    assert code == 0, (out, err)
    assert UNCHANGED in out.splitlines(), out
    assert rig.hooks.read_bytes() == after_user_edit
    groups = json.loads(rig.hooks.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert groups[0] == user_group
    assert "alice-memory-session-start" in groups[1]["hooks"][0]["command"]
    assert "touch" in groups[2]["hooks"][0]["command"] and len(groups) == 3
    # An unchanged run rewrites nothing, so it makes no backup: the one from step 1 stays alone.
    assert len(_backups(vault_a, "hooks.json")) == 1
    marker.unlink()
    third = rig.run()
    assert marker.exists() and not second_marker.exists(), third.describe()
    assert any(LINE_A in text for text in third.developer()), third.describe()
    rig.say(
        f"step 4: install said unchanged; groups are user, alice, second user; {third.summary()}; "
        f"the brief line is still present; the second group ran nothing (untrusted)"
    )

    # 5. A new data dir changes the hook, so Codex skips it until it is trusted again.
    before_change = rig.hooks.read_bytes()
    config_before_change = rig.config.read_bytes()
    code, out, err = rig.install(vault_b)
    assert code == 0, (out, err)
    lines = out.splitlines()
    assert WRITTEN in lines and MODIFIED in lines and TRUST_NEXT in lines, out
    groups = json.loads(rig.hooks.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert groups[0] == user_group and groups[2] == document["hooks"]["SessionStart"][2]
    assert str(vault_b.resolve()) in groups[1]["hooks"][0]["command"]
    assert [path.read_bytes() for path in _backups(vault_b, "hooks.json")] == [before_change]
    assert [path.read_bytes() for path in _backups(vault_b, "config.toml")] == [config_before_change]
    marker.unlink()
    fourth = rig.run()
    assert marker.exists(), fourth.describe()
    assert not any(LINE_A in text or LINE_B in text for text in fourth.developer()), fourth.describe()
    assert not any(FRAME in text for text in fourth.developer()), fourth.describe()
    assert LINE_A not in fourth.everything() and LINE_B not in fourth.everything()
    assert any(has_alice_recall(request) for request in fourth.requests), fourth.describe()
    listed = rig.alice_hook()
    assert listed["trustStatus"] == "modified", listed
    assert listed["currentHash"] == rig.hash_of(1), (listed, rig.hash_of(1))
    rig.say(
        f"step 5: install said written and changed, with the trust line; {fourth.summary()}; "
        f"the user's group still ran; neither {LINE_A} nor {LINE_B} appeared; "
        f"hooks/list: trustStatus={listed['trustStatus']}"
    )

    rig.trust(1)
    fifth = rig.run()
    assert any(LINE_B in text for text in fifth.developer()), fifth.describe()
    assert not any(LINE_A in text for text in fifth.developer()), fifth.describe()
    rig.say(f"step 5b: trusted again, {fifth.summary()}: {LINE_B} appears and {LINE_A} does not")

    # 6. The dry run masks the hook.
    secret = "startup|" + secrets.token_hex(6)
    document = json.loads(rig.hooks.read_text(encoding="utf-8"))
    document["hooks"]["SessionStart"][1]["matcher"] = secret
    seeded = _write_json(rig.hooks, document)
    code, out, err = rig.install(vault_b, "--dry-run")
    assert code == 0, (out, err)
    assert rig.hooks.read_bytes() == seeded
    assert secret not in out + err
    assert "<hidden>" in out and "hook matcher" in out
    assert "--format markdown" in out
    rig.say("step 6: the dry run printed <hidden> for the matcher and showed --format markdown")
    rig.flush()


@requires_real_codex
def test_real_codex_ignores_json_hook_output(rig: Rig) -> None:
    """Control: a hook that prints JSON with a top-level ``additional_context`` injects nothing.

    Alice's own hook in ``--format json`` and a hook that prints
    ``{"additional_context": "x"}`` are both trusted and run, and neither
    reaches the model, while a trusted hook that prints plain text does. That
    is why install's hook prints markdown. Codex's own ``hooks/list`` says
    all three are ``trusted`` with the hash computed here, so a hook that
    injected nothing was one Codex ran, not one it skipped. Mutation: make
    install write ``--format json``. The main test above fails, and this
    control shows why. Mutation: trust the wrong key for either control
    hook. The ``trustStatus`` assertion fails, where ``not injected`` alone
    would have passed.
    """

    tmp_path, monkeypatch = rig.tmp_path, rig.monkeypatch
    vault = tmp_path / "vault"
    commit_fact(vault, monkeypatch, "Control fact", f"The control codeword is {LINE_A}.")
    code, out, err = rig.install(vault)
    assert code == 0, (out, err)
    document = json.loads(rig.hooks.read_text(encoding="utf-8"))
    alice = document["hooks"]["SessionStart"][0]["hooks"][0]
    alice["command"] = alice["command"].replace("--format markdown", "--format json")
    assert "--format json" in alice["command"]
    plain = "plain-control-text-4471"
    document["hooks"]["SessionStart"].append(
        _group("printf '%s' " + shlex.quote(plain))
    )
    document["hooks"]["SessionStart"].append(
        _group("printf '%s' " + shlex.quote('{"additional_context": "x-control-json-9902"}'))
    )
    _write_json(rig.hooks, document)
    rig.trust(0, 1, 2)
    listed = rig.listed_hooks()
    for group in range(3):
        entry = listed.get(rig.key(group))
        assert entry is not None, (rig.key(group), sorted(listed))
        assert entry["trustStatus"] == "trusted", entry
        assert entry["currentHash"] == rig.hash_of(group), (entry, rig.hash_of(group))
    run = rig.run()
    assert any(plain in text for text in run.developer()), run.describe()
    assert "x-control-json-9902" not in run.everything(), run.describe()
    assert LINE_A not in run.everything(), run.describe()
    assert not any(FRAME in text for text in run.developer()), run.describe()
    rig.say(
        f"control 1: {run.summary()}; hooks/list: all three hooks are trusted with the computed hash; "
        "the plain-text hook was injected; Alice's hook in --format json "
        "and a hook printing an additional_context object injected nothing"
    )
    rig.flush()


@requires_real_codex
def test_real_codex_skips_a_hooks_file_with_an_http_handler(rig: Rig) -> None:
    """Control: a sibling handler of type ``http`` makes Codex skip the whole file.

    Even with ``--dangerously-bypass-hook-trust`` there is no brief, and the
    user's own group does not run. Without the ``http`` handler the same
    file, run the same way, injects the brief. That is why install refuses
    such a file, and it does, with nothing written. Mutation: accept a
    hooks.json with an ``http`` handler. The refusal assertions fail.
    """

    tmp_path, monkeypatch = rig.tmp_path, rig.monkeypatch
    vault = tmp_path / "vault"
    commit_fact(vault, monkeypatch, "Control fact", f"The control codeword is {LINE_A}.")
    marker = tmp_path / "user-hook-ran"
    code, out, err = rig.install(vault)
    assert code == 0, (out, err)
    good = json.loads(rig.hooks.read_text(encoding="utf-8"))
    good["hooks"]["SessionStart"].insert(0, _group(f"touch {shlex.quote(str(marker))}"))
    _write_json(rig.hooks, good)
    bypass = "--dangerously-bypass-hook-trust"

    with_file = rig.run(bypass)
    assert marker.exists(), with_file.describe()
    assert any(LINE_A in text for text in with_file.developer()), with_file.describe()
    marker.unlink()

    broken = json.loads(json.dumps(good))
    broken["hooks"]["SessionStart"].append(
        {"hooks": [{"type": "http", "url": "http://127.0.0.1:9/never"}]}
    )
    broken_bytes = _write_json(rig.hooks, broken)
    skipped = rig.run(bypass)
    assert not marker.exists(), skipped.describe()
    assert LINE_A not in skipped.everything(), skipped.describe()
    listed = rig.codex_hooks()
    rig.say(f"control 2: hooks/list said {listed}")
    if not isinstance(listed, str):
        hooks, warnings = listed
        assert hooks == [] and any("hooks.json" in warning for warning in warnings), listed

    code, out, err = rig.install(vault)
    assert code == 1, (out, err)
    assert rig.hooks.read_bytes() == broken_bytes
    assert "Codex would skip this hooks.json" in out
    rig.say(
        "control 2: with an http sibling and --dangerously-bypass-hook-trust Codex ran nothing "
        f"({skipped.summary()}); install refuses the same file"
    )
    rig.flush()


@requires_real_codex
def test_real_codex_and_install_agree_on_hooks_json_number_and_spelling_rules(rig: Rig) -> None:
    """Codex loads or skips hooks.json exactly where install accepts or refuses it, case by case.

    Each case of ``NUMBER_CASES`` is a hooks.json with a plain command handler
    under SessionStart (the probe) and the case's handler under Stop. The real
    ``codex app-server`` ``hooks/list`` lists the probe when Codex loaded the
    file, and lists nothing with a hooks.json warning when it skipped it. Install
    is run on the same file. The three must agree: Codex, install, and the
    table's verdict, which came from a Rust judge built from Codex's own
    types. That build turns on serde_json's ``arbitrary_precision`` as Codex
    does, so an ``mcp_tool`` input takes an integer of any size and a ``-0``
    timeout loads. Every disagreement is collected before the test fails, so one
    run in the pinned job shows them all. Mutation: restore the i64 range check
    in ``_codex_toml_representable``, or drop the both-spellings check. The
    cases it breaks disagree with Codex and this test fails.
    """

    tmp_path = rig.tmp_path
    problems: list[str] = []
    for index, (label, handler, expected) in enumerate(NUMBER_CASES):
        rig.hooks.write_text(number_case_document(handler), encoding="utf-8")
        before = rig.hooks.read_bytes()
        rig.config.unlink(missing_ok=True)
        listed = rig.codex_hooks()
        assert not isinstance(listed, str), (label, listed)
        hooks, warnings = listed
        loaded = any("probe-hook" in str(hook.get("command")) for hook in hooks)
        if not loaded and not (hooks == [] and any("hooks.json" in warning for warning in warnings)):
            problems.append(f"{label}: Codex listed no probe and gave no hooks.json warning: {listed}")
        code, out, err = rig.install(tmp_path / f"vault-{index}")
        accepted = code == 0
        if not accepted and (
            "Codex would skip this hooks.json" not in out or rig.hooks.read_bytes() != before
        ):
            problems.append(f"{label}: install refused for another reason, or wrote: {out} {err}")
        rig.say(f"{label}: codex {'loads' if loaded else 'skips'}, install {'accepts' if accepted else 'refuses'}")
        if not (loaded == accepted == expected):
            problems.append(
                f"{label}: codex loaded={loaded}, install accepted={accepted}, table says {expected}"
                + ("" if loaded else f"; codex warnings={warnings}")
            )
    rig.flush()
    assert not problems, "\n".join(problems)


@requires_real_codex
def test_real_codex_does_not_spill_a_large_brief(rig: Rig) -> None:
    """``additionalContextLimit: 0`` keeps Codex from spilling a large brief to a file.

    Codex counts one token per four bytes, so five facts of about 1,300
    Chinese characters each make a brief of about 4,900 tokens, over its
    default limit of 2,500. With install's ``0`` the developer message holds
    every fact and no spill footer. As a control, the same hook with the key
    removed is spilled to a file: the message ends with a "Full hook output
    saved to" footer and the middle of the brief is cut. Mutation: write a
    limit other than 0, or leave the key out. The first half fails.
    """

    tmp_path, monkeypatch = rig.tmp_path, rig.monkeypatch
    vault = tmp_path / "vault"
    marks = [f"END-MARK-{number}" for number in range(5)]
    for number, mark in enumerate(marks):
        body = "".join(chr(0x4E00 + number * 400 + offset) for offset in range(1300))
        commit_fact(vault, monkeypatch, f"Large fact {number}", f"{body} {mark}")
    code, out, err = rig.install(vault)
    assert code == 0, (out, err)
    rig.trust(0)

    whole = rig.run()
    texts = [text for text in whole.developer() if FRAME in text]
    assert len(texts) == 1, whole.describe()
    text = texts[0]
    assert len(text.encode("utf-8")) > 4 * DEFAULT_LIMIT, len(text.encode("utf-8"))
    assert all(mark in text for mark in marks), whole.describe()
    assert "Full hook output saved to" not in text, whole.describe()
    rig.say(
        f"large brief: {len(text.encode('utf-8'))} bytes "
        f"(about {len(text.encode('utf-8')) // 4} tokens) injected whole, every fact present, "
        "no spill footer"
    )

    document = json.loads(rig.hooks.read_text(encoding="utf-8"))
    handler = document["hooks"]["SessionStart"][0]["hooks"][0]
    assert handler.pop("additionalContextLimit") == 0
    _write_json(rig.hooks, document)
    rig.trust(0)
    spilled = rig.run()
    previews = [text for text in spilled.developer() if FRAME in text]
    assert len(previews) == 1, spilled.describe()
    assert "Full hook output saved to" in previews[0], spilled.describe()
    assert not all(mark in previews[0] for mark in marks), spilled.describe()
    rig.say(
        f"control 3: without additionalContextLimit Codex spilled it: {len(previews[0].encode('utf-8'))} "
        f"bytes kept, footer {previews[0].splitlines()[-1]!r}"
    )
    rig.flush()
