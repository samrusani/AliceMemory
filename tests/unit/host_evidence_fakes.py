"""Stand-ins for ``claude`` and ``codex`` that the host-evidence tests put first on PATH.

They are not hosts. Each one plays the part the evidence script needs from a host: it
answers ``--version``, reads the files the script wrote (``settings.json`` or ``hooks.json``,
``.claude.json`` or ``config.toml``), runs the SessionStart hook with a JSON payload on stdin,
starts the stub MCP server over stdio, speaks the MCP handshake, answers or ignores the
``roots/list`` request, and makes one call to the loopback API the script started. Planted
values (a client folder name, a key-shaped token, a private sentence) let a test check that
none of them reaches an artifact.

``FAKE_MODE`` picks the client: ``answer`` declares roots and elicitation and answers
``roots/list``; ``error`` declares nothing and answers with method not found; ``silent``
declares nothing and never answers; ``no_hook`` is ``answer`` with no hook run.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

PLANTED_FOLDER = "secret-client-checkout"
PLANTED_SENTENCE = "ship the secret-client migration on friday"
# Built at run time so no key-shaped literal sits in the source for a secret scanner to find.
PLANTED_TOKEN = "ghp_" + "0123456789abcdefghijklmnopqrstuvwxyzAB"
PLANTED_KEY = "sk-live-" + "0123456789abcdef" * 2
PLANTED_STDERR_PATH = "/private/secret-client-checkout/notes.txt"

_COMMON = r'''
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


def run_hooks(commands, payload, env, cwd):
    for command in commands:
        subprocess.run(
            command, shell=True, input=json.dumps(payload).encode(), env=env, cwd=cwd, check=False, timeout=60
        )


class Server:
    def __init__(self, command, args, env, cwd):
        self.proc = subprocess.Popen(
            [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=cwd
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))
        self.lines.put(None)

    def send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def next(self, timeout):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return "timeout"

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def handshake(server, capabilities, roots_mode, cwd, name, version):
    server.send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": capabilities,
                "clientInfo": {"name": name, "version": version},
            },
        }
    )
    reply = server.next(10)
    assert isinstance(reply, dict) and reply.get("id") == 1, reply
    server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    server.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    deadline = time.time() + 8
    while time.time() < deadline:
        message = server.next(0.5)
        if message is None:
            return
        if message == "timeout" or message.get("method") != "roots/list":
            continue
        if roots_mode == "answer":
            root = {"uri": "file://" + cwd, "name": os.path.basename(cwd)}
            server.send({"jsonrpc": "2.0", "id": message["id"], "result": {"roots": [root]}})
        elif roots_mode == "error":
            error = {"code": -32601, "message": "Method not found at " + cwd}
            server.send({"jsonrpc": "2.0", "id": message["id"], "error": error})
        return


def call_api(url):
    request = urllib.request.Request(url, data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as reply:
            reply.read()
    except urllib.error.HTTPError:
        pass


MODES = {
    "answer": ({"roots": {"listChanged": True}, "elicitation": {}}, "answer"),
    "no_hook": ({"roots": {"listChanged": True}, "elicitation": {}}, "answer"),
    "error": ({}, "error"),
    "silent": ({}, "silent"),
}
'''

_CLAUDE = r'''

def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.281 (Claude Code)"))
        return 0
    cwd = os.getcwd()
    home = os.environ["HOME"]
    mode = os.environ.get("FAKE_MODE", "answer")
    capabilities, roots_mode = MODES[mode]
    if not os.environ["ANTHROPIC_API_KEY"].startswith("alice-test-"):
        open(os.path.join(os.environ["FAKE_MARKERS"], "saw-real-key"), "w").write("x")
    if "PLANTED_SERVICE_TOKEN" in os.environ:
        open(os.path.join(os.environ["FAKE_MARKERS"], "saw-token"), "w").write("x")
    settings = json.loads(open(home + "/.claude/settings.json").read())
    commands = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = cwd
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": home + "/.claude/projects/-" + "PLANTED_FOLDER" + "/" + str(uuid.uuid4()) + ".jsonl",
        "cwd": cwd,
        "hook_event_name": "SessionStart",
        "source": "startup",
        "note": "PLANTED_SENTENCE",
        "token": "PLANTED_TOKEN",
        "nested": {"key": "PLANTED_KEY", "count": 3, "flag": True},
    }
    if mode != "no_hook":
        run_hooks(commands, payload, env, cwd)
    config = json.loads(open(home + "/.claude.json").read())
    servers = []
    for name, entry in config["mcpServers"].items():
        server_env = dict(env)
        server_env.update(entry.get("env", {}))
        server = Server(entry["command"], entry["args"], server_env, cwd)
        handshake(server, capabilities, roots_mode, cwd, "claude-code", "2.1.281")
        servers.append((name, server))
    call_api(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true")
    init = {
        "type": "system",
        "subtype": "init",
        "cwd": cwd,
        "session_id": str(uuid.uuid4()),
        "tools": ["Bash", "mcp__alice-evidence__probe"],
        "mcp_servers": [{"name": name, "status": "connected"} for name, _ in servers],
    }
    print(json.dumps(init))
    print(json.dumps({"type": "system", "subtype": "hook_response", "outcome": "success", "exit_code": 0}))
    for _name, server in servers:
        server.close()
    sys.stderr.write("Not logged in, could not read PLANTED_STDERR_PATH\n")
    return 1


sys.exit(main())
'''

_CODEX = r'''

def main():
    argv = sys.argv[1:]
    if "--version" in argv:
        print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.158.0"))
        return 0
    import tomllib

    cwd = os.getcwd()
    mode = os.environ.get("FAKE_MODE", "answer")
    capabilities, roots_mode = MODES[mode]
    base = next(a.split('="', 1)[1].rstrip('"') for a in argv if a.startswith("openai_base_url="))
    codex_home = os.environ["CODEX_HOME"]
    if not os.environ["CODEX_API_KEY"].startswith("dummy-"):
        open(os.path.join(os.environ["FAKE_MARKERS"], "saw-real-key"), "w").write("x")
    if "PLANTED_SERVICE_TOKEN" in os.environ:
        open(os.path.join(os.environ["FAKE_MARKERS"], "saw-token"), "w").write("x")
    with open(codex_home + "/config.toml", "rb") as handle:
        config = tomllib.load(handle)
    hooks = json.loads(open(codex_home + "/hooks.json").read())
    root = os.environ["FAKE_REPO_ROOT"]
    sys.path[:0] = [root, root + "/apps/api/src"]
    from tests.unit.test_codex_hook_real_host import trust_hash

    state = config.get("hooks", {}).get("state", {})
    hooks_file = os.path.realpath(codex_home + "/hooks.json")
    commands = []
    for gi, group in enumerate(hooks["hooks"]["SessionStart"]):
        for hi, handler in enumerate(group["hooks"]):
            expected = trust_hash(
                handler["command"], timeout=handler.get("timeout"), limit=handler.get("additionalContextLimit")
            )
            trusted = state.get(f"{hooks_file}:session_start:{gi}:{hi}", {}).get("trusted_hash")
            if trusted == expected:
                commands.append(handler["command"])
    payload = {
        "session_id": str(uuid.uuid4()),
        "transcript_path": None,
        "cwd": cwd,
        "hook_event_name": "SessionStart",
        "model": "gpt-5.5",
        "source": "startup",
        "note": "PLANTED_SENTENCE",
    }
    if mode != "no_hook":
        run_hooks(commands, payload, dict(os.environ), cwd)
    servers = []
    for name, entry in config["mcp_servers"].items():
        # Codex passes a stdio server a short list of names and the entry's own env, nothing else.
        server_env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG") if k in os.environ}
        server_env.update(entry.get("env", {}))
        server = Server(entry["command"], entry["args"], server_env, cwd)
        handshake(server, capabilities, roots_mode, cwd, "codex-mcp-client", "0.158.0")
        servers.append(server)
    call_api(base + "/responses")
    for server in servers:
        server.close()
    return 0


sys.exit(main())
'''


def _fill(text: str) -> str:
    return (
        text.replace("PLANTED_FOLDER", PLANTED_FOLDER)
        .replace("PLANTED_SENTENCE", PLANTED_SENTENCE)
        .replace("PLANTED_TOKEN", PLANTED_TOKEN)
        .replace("PLANTED_KEY", PLANTED_KEY)
        .replace("PLANTED_STDERR_PATH", PLANTED_STDERR_PATH)
    )


def install_fakes(bin_dir: Path) -> dict[str, Path]:
    """Write executable ``claude`` and ``codex`` stand-ins into ``bin_dir``."""

    bin_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, body in (("claude", _CLAUDE), ("codex", _CODEX)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + _fill(_COMMON + body), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        written[name] = path
    return written
