"""Compare whole lesson answers on the same vault before and after the change."""

import json
import os
from pathlib import Path
import subprocess
import sys


def _rpc(pythonpath, vault, calls):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALICE")}
    env.update(PYTHONPATH=pythonpath, ALICE_MCP_FULL_TOOLS="1", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(
        [sys.executable, "-m", "alicebot_api.onramp", "mcp", "--data-dir", str(vault)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
    )

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def read():
        line = process.stdout.readline()
        assert line, process.stderr.read()
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"},
    }})
    read()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    answers = []
    for index, (name, arguments) in enumerate(calls, 1):
        send({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        answer = read()["result"]
        assert not answer.get("isError"), answer
        answers.append(json.loads(answer["content"][0]["text"]))
    process.stdin.close()
    assert process.wait(timeout=30) == 0
    return answers


def test_whole_answer_matches_main_on_the_same_vault(tmp_path):
    """Guard: change the publication actor and the two whole answers differ."""
    root = Path(__file__).resolve().parents[2]
    configured = os.environ.get("ALICE_LESSON_MAIN_ARCHIVE")
    if configured:
        archive = Path(configured)
    else:
        archive = tmp_path / "main.zip"
        with archive.open("wb") as output:
            subprocess.run(["git", "archive", "--format=zip", "dbc3b910"], cwd=root, stdout=output, check=True)
    base_path = os.pathsep.join((str(archive) + "/apps/api/src", str(archive)))
    head_path = os.pathsep.join((str(root / "apps/api/src"), str(root)))
    vault = tmp_path / "vault"
    lesson = {"applies_when": "rebasing a branch", "what_happened": "A check failed.",
              "do_instead": "Review both sides.", "paths": ["src/*.py"]}
    arguments = {"task": "rebasing a branch", "paths": ["src/a.py"]}
    before = _rpc(base_path, vault, [("alice_memory_commit", {"lesson": lesson}), ("alice_lessons", arguments)])
    assert before[0]["status"] == "committed" and before[1]["count"] == 1
    after = _rpc(head_path, vault, [("alice_lessons", arguments)])
    assert after[0] == before[1]
