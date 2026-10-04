"""Four documentation follow-ups from the docs audit, each pinned to the code it describes.

1. The Hermes capture folder carries a dated notice naming what the four capture files no
   longer show.
2. `alice-memory install --write-mcpb` is documented in the CLI reference, with the
   launcher, the refusals and the support status read off the code.
3. Seven sprint-era pages open with the backend, the settings and the support status they
   need, and the CLI reference links them under "Legacy surfaces".

The sentences name behaviour of the code, so each test reads the behaviour from the code and
compares it with the page. A page that goes back to the old wording fails, and so does code
that changes under a page. The install claims are read by running `alice-memory install`
through `alicebot_api.onramp.main`, as a user does, in a temporary home. This file does not
import the host modules, so it is not one of the tests that the real-host workflow lists in
its pull request paths (`test_real_host_ci_workflow.py` keeps that list).

Mutations, each one alone: change a character of any one capture file; drop one of the nine
tool names from the capture notice; change `uvx` to `npx` in `build_mcpb_manifest`; change
`.mcpb` to `.zip` in `MCPB_SUFFIX`; delete the directory refusal in `write_mcpb_bundle`; change
the manifest's data directory default; put `--write-mcpb` into the quickstart; remove a link
from the "Legacy surfaces" section of the CLI reference; move a page's "Requirements and
status" section below another heading; put "This sprint" back into one of the seven pages;
move one named tool out of `_LEGACY_TOOL_NAMES`; make one named tool's handler open
`_vnext_store_context` instead of `_store_context`; add `alice_task_brief` to the default
handshake. Each fails one of the tests below.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest
import yaml

from alicebot_api import main as api_main
from alicebot_api.cli import build_parser
from alicebot_api.mcp import registry
from alicebot_api.mcp.policy import AGENT_API_KEY_ENV
from alicebot_api.onramp import main as onramp_main
from alicebot_api.surface_flags import LEGACY_SURFACES_ENV, MCP_FULL_TOOLS_ENV, MCP_LEGACY_TOOLS_ENV
from tests.unit.launcher_helpers import pin_launcher_search

ROOT = Path(__file__).resolve().parents[2]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _flat(relative: str) -> str:
    return " ".join(_text(relative).split())


def _section(relative: str, heading: str) -> str:
    """The body of the `## heading` section: from its heading to the next `## ` heading."""

    text = _text(relative)
    marker = f"\n## {heading}\n"
    assert text.count(marker) == 1, (relative, heading)
    body = text.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


# --- 1. The Hermes capture notice -------------------------------------------------------

HERMES_ASSETS = "docs/integrations/assets/hermes"
HERMES_CAPTURE_HASHES = {
    "hermes-mcp-test.png": "d43685bd0c7c977adaec6c07c846fa46edee0c406761ac1245fd085ad0ac35f9",
    "hermes-mcp-test.txt": "41345c0d21e6656d65e63fe6c34a0aa16059e3ca69836ae9a0e88eecebb034ad",
    "hermes-runtime-smoke.png": "7cacd0fc5361fef0f43f6138b7a557f5577eb2091563edbef721c4eb5ebdd94d",
    "hermes-runtime-smoke.txt": "5c5a38a4b78efd223e0860461357e41fabfc264b1a67699c0d50700ad38a9de0",
}


def test_the_hermes_captures_stay_unchanged_and_carry_a_dated_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The four capture files are a record, so they are pinned byte for byte, and a notice sits beside them.

    The notice is dated, says when the files were added (the date of the commit that added
    them), names the nine tools the capture lists and the server name it used, says what the
    server advertises now, and links the current Hermes guide. Each claim about today's
    surface is read from the code.

    Mutations: edit one byte of a capture file; drop a tool name from the notice; change the
    date; point the link at another page; change the default tool list in the registry.
    """

    for name, digest in HERMES_CAPTURE_HASHES.items():
        data = (ROOT / HERMES_ASSETS / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name

    capture = _text(f"{HERMES_ASSETS}/hermes-mcp-test.txt")
    smoke = _text(f"{HERMES_ASSETS}/hermes-runtime-smoke.txt")
    captured_tools = re.findall(r"^\s+(alice_[a-z_]+)\s", capture, flags=re.MULTILINE)
    assert len(captured_tools) == 9
    assert "Tools discovered: 9" in capture
    assert "Testing 'alice_core'" in capture
    assert "mcp_alice_core_alice_" in smoke

    notice = _text(f"{HERMES_ASSETS}/README.md")
    flat = " ".join(notice.split())
    assert notice.startswith("# Hermes captures\n\nNotice, 2026-10-04. ")
    assert (
        "They were added on 2026-04-09 and record the first run against a real Hermes install."
    ) in flat
    for name in HERMES_CAPTURE_HASHES:
        assert f"`{name}`" in flat, name
    for tool in captured_tools:
        assert f"`{tool}`" in flat, tool
    assert "Hermes testing a server named `alice_core`, which reports nine tools" in flat
    registered = json.loads(smoke)["registered_tools"]
    assert len(registered) == 3 and all(name.startswith("mcp_alice_core_alice_") for name in registered)
    assert "the smoke output, with three registered tools named `mcp_alice_core_alice_...`" in flat

    # What the server advertises now, from the registry.
    default = registry._DEFAULT_CORE_TOOL_ORDER
    assert len(default) == 3 and len(registry._CORE_TOOL_NAMES) == 11
    listed = ", ".join(f"`{name}`" for name in default)
    assert f"advertises three tools by default ({listed})" in flat
    assert f"`{MCP_FULL_TOOLS_ENV}=1` advertises eleven core tools" in flat
    # One of the nine is no longer a core tool.
    retired = [name for name in captured_tools if name not in registry._CORE_TOOL_NAMES]
    assert retired == ["alice_recent_changes"]
    assert "alice_recent_changes" in registry._LEGACY_TOOL_NAMES
    assert "`alice_recent_changes` is no longer one of them; it is a legacy tool." in flat
    # The installer writes the entry `alice`, and the manual examples still say `alice_core`.
    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    code, output = _install(capsys, tmp_path / "home", "--host", "hermes")
    assert code == 0, output
    written = yaml.safe_load(_host_path(output, "hermes").read_text(encoding="utf-8"))
    assert list(written["mcp_servers"]) == ["alice"]
    assert "`alice-memory install --host hermes` writes the server entry `alice`, not `alice_core`." in flat
    assert "`alice_core` is still the name in the manual full-stack examples." in flat
    assert "alice_core:" in _text("docs/integrations/examples/hermes-config.mcp-only.yaml")

    assert "see [Hermes Reference Integration](../../hermes.md)." in flat
    assert (ROOT / "docs/integrations/hermes.md").is_file()


# --- 2. `install --write-mcpb` ----------------------------------------------------------

CLI_REFERENCE = "docs/integrations/cli.md"
MCPB_SECTION = "Install and the Claude Desktop bundle"


def _install(capsys, home: Path, *extra: str) -> tuple[int, str]:
    """Run `alice-memory install --home HOME ...` and return the exit code and the receipt."""

    code = onramp_main(["install", "--home", str(home), *extra])
    return code, capsys.readouterr().out


def _blocks(output: str) -> list[str]:
    return output.split("\n\n")


def _host_path(output: str, host: str) -> Path:
    """The file a host's receipt block names on its `path:` line."""

    for block in _blocks(output):
        if block.startswith(f"host: {host}\n"):
            match = re.search(r"^path: (.+)$", block, flags=re.MULTILINE)
            assert match, block
            return Path(match.group(1))
    raise AssertionError(f"no receipt block for {host}:\n{output}")


def test_write_mcpb_is_documented_in_the_cli_reference_and_not_the_quickstart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reference says what the flag writes, what it needs and when it refuses, as the code does.

    Read from running install: the launcher line, the one-file zip, the data-dir default that
    `--data-dir` does not change, the two refusal reasons, the warning when `uvx` is missing,
    the non-zero exit after the host entries are written, the dry run, the parent folder mode,
    the replaced file and the default hosts. Read from the repository: nothing builds a `.mcpb`
    and the real-host workflow runs four other hosts. The quickstart does not mention the flag.

    Mutations: change `uvx` in the manifest; change `.mcpb` in `MCPB_SUFFIX`; delete the
    directory refusal; drop the uvx warning; change the manifest's data directory default; remove a
    default host; put `--write-mcpb` into the quickstart; delete the support-status
    paragraph; make a failed bundle exit zero.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx="/usr/local/bin/uvx")
    section = " ".join(_section(CLI_REFERENCE, MCPB_SECTION).split())

    def manifest_of(bundle: Path) -> dict:
        with zipfile.ZipFile(bundle) as archive:
            assert archive.namelist() == ["manifest.json"]
            return json.loads(archive.read("manifest.json").decode("utf-8"))

    # One install: a nested path under a parent that does not exist yet, and a data dir that is not the bundle's.
    vault = tmp_path / "vault"
    bundle = tmp_path / "out" / "nested" / "alice.mcpb"
    code, output = _install(
        capsys, tmp_path / "home", "--data-dir", str(vault), "--host", "cursor", "--write-mcpb", str(bundle)
    )
    assert code == 0, output
    assert f"mcpb: {bundle}\naction: written" in output
    manifest = manifest_of(bundle)
    command = manifest["server"]["mcp_config"]["command"]
    args = manifest["server"]["mcp_config"]["args"]
    assert manifest["server"]["entry_point"] == command == "uvx"
    launcher = " ".join([command, *args])
    assert launcher == "uvx alice-memory mcp --data-dir ${user_config.data_dir}"
    assert (
        f"The manifest's launcher runs `{launcher}`, so `uvx` (from [uv](https://docs.astral.sh/uv/)) is required "
        "on the machine that opens the bundle."
    ) in section
    default_dir = manifest["user_config"]["data_dir"]["default"]
    assert default_dir == "${HOME}/.alice"
    assert str(vault) not in json.dumps(manifest)
    assert f"The bundle's `data_dir` setting is a directory choice that defaults to `{default_dir}`." in section
    assert "`--data-dir` does not change it." in section
    assert "`alice-memory install --write-mcpb PATH` also writes a `.mcpb` bundle for Claude Desktop at PATH." in section
    assert "The bundle is a zip that holds one file, `manifest.json`. It holds no copy of Alice." in section
    assert (bundle.parent.stat().st_mode & 0o777) == 0o700 == (bundle.parent.parent.stat().st_mode & 0o777)
    assert "A missing parent folder is created with mode 0700." in section

    # A file already at the path is replaced.
    bundle.write_bytes(b"old")
    code, output = _install(capsys, tmp_path / "home", "--host", "cursor", "--write-mcpb", str(bundle))
    assert code == 0, output
    manifest_of(bundle)
    assert "A file already at PATH is replaced." in section

    # The two refusals, and what a refusal does to the run: the host entry is written and the exit is non-zero.
    folder = tmp_path / "folder.mcpb"
    folder.mkdir()
    reasons = []
    for target in (tmp_path / "alice.zip", folder):
        code, output = _install(capsys, tmp_path / "refused", "--host", "cursor", "--write-mcpb", str(target))
        assert code != 0
        block = _blocks(output)[-1]
        assert block.splitlines()[0] == f"mcpb: {target}"
        assert block.splitlines()[1] == "action: failed"
        reasons.append(block.splitlines()[2].removeprefix("reason: "))
        assert _host_path(output, "cursor").is_file()
    assert reasons == ["mcpb path must end in .mcpb", "mcpb path is a directory"]
    assert (
        "PATH must end in `.mcpb` and must not be a directory. Otherwise the bundle's block of the receipt reads "
        f"`action: failed` with the reason `{reasons[0]}` or `{reasons[1]}`, and install exits non-zero after it has "
        "written the host entries."
    ) in section

    # The flag adds to the host entries: without --host the default hosts are still written, and no other.
    code, output = _install(capsys, tmp_path / "shared", "--write-mcpb", str(tmp_path / "all.mcpb"))
    assert code == 0, output
    hosts = re.findall(r"^host: (\S+)$", output, flags=re.MULTILINE)
    assert hosts == ["claude-desktop", "claude-code", "cursor", "openclaw"]
    for host in hosts:
        assert _host_path(output, host).is_file(), host
    manifest_of(tmp_path / "all.mcpb")
    assert (
        "Without `--host`, install still writes the default hosts (Claude Desktop, Claude Code, Cursor and OpenClaw)."
    ) in section
    assert "The flag adds to the host entries and does not replace them." in section

    # A dry run prints the manifest and writes no bundle.
    dry = tmp_path / "dry.mcpb"
    code, output = _install(capsys, tmp_path / "dryhome", "--dry-run", "--host", "cursor", "--write-mcpb", str(dry))
    assert code == 0, output
    assert "action: dry-run" in _blocks(output)[-1] and "snippet:" in _blocks(output)[-1] and not dry.exists()
    assert "With `--dry-run`, install prints the manifest and writes no bundle." in section

    # The warning when uvx is not on PATH, and that the bundle is written anyway.
    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    warn = tmp_path / "warn.mcpb"
    code, output = _install(capsys, tmp_path / "warnhome", "--host", "cursor", "--write-mcpb", str(warn))
    assert code == 0, output
    assert "warning: the bundle runs uvx, which is not on PATH here" in _blocks(output)[-1]
    manifest_of(warn)
    assert (
        "When `uvx` is not on `PATH` where install runs, the receipt adds a warning and the bundle is still written."
    ) in section

    # Support status. Nothing builds a bundle for a release, and Claude Desktop is not a real-host job.
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    for workflow in workflows:
        body = workflow.read_text(encoding="utf-8").casefold()
        assert "mcpb" not in body, workflow.name
        assert "desktop" not in body, workflow.name
    real_host = _text(".github/workflows/real-host-ci.yml")
    assert "Pinned claude, hermes, opencode, and codex" in real_host
    assert (
        "No release builds or attaches a `.mcpb`. Installing one in Claude Desktop has no verified real-host support: "
        "real-host CI runs Claude Code, Hermes, OpenCode and Codex, and the bundle is covered by unit tests only."
    ) in section

    assert "--write-mcpb" not in _text("docs/alpha/quickstart.md")
    assert "[quickstart](../alpha/quickstart.md#install-with-alice-memory)" in section
    assert "## Install with alice-memory" in _text("docs/alpha/quickstart.md")


# --- 3. The seven legacy pages ----------------------------------------------------------

FULL_STACK = "[Full stack](../../README.md#full-stack-postgres--review-console)"
WORDS = {3: "three", 4: "four"}

MUTATIONS_PAGE = "docs/memory/p12-s2-automated-memory-operations.md"
CONTRADICTIONS_PAGE = "docs/memory/p12-s3-contradictions-trust-calibration.md"
HYGIENE_PAGE = "docs/memory/p13-s3-memory-hygiene-conversation-health.md"
TRACING_PAGE = "docs/retrieval/hybrid_tracing.md"
EVALS_PAGE = "docs/evals/public_eval_harness.md"
BRIEFING_PAGE = "docs/briefing/task-adaptive-briefing.md"
WALKTHROUGH_PAGE = "docs/examples/phase9-command-walkthrough.md"

MCP_TOOLS = {
    MUTATIONS_PAGE: (
        "alice_memory_mutations_generate",
        "alice_memory_mutations_list_candidates",
        "alice_memory_mutations_commit",
        "alice_memory_mutations_list_operations",
    ),
    CONTRADICTIONS_PAGE: (
        "alice_contradictions_detect",
        "alice_contradictions_list",
        "alice_contradictions_resolve",
        "alice_trust_signals",
    ),
    TRACING_PAGE: ("alice_recall_debug", "alice_resume_debug", "alice_retrieval_trace"),
    BRIEFING_PAGE: ("alice_task_brief", "alice_task_brief_show", "alice_task_brief_compare"),
    HYGIENE_PAGE: (),
    EVALS_PAGE: (),
    WALKTHROUGH_PAGE: (),
}

HTTP_ROUTES = {
    MUTATIONS_PAGE: (
        ("POST", "/v1/memory/operations/candidates/generate"),
        ("GET", "/v1/memory/operations/candidates"),
        ("POST", "/v1/memory/operations/commit"),
        ("GET", "/v1/memory/operations"),
    ),
    CONTRADICTIONS_PAGE: (
        ("POST", "/v1/contradictions/detect"),
        ("GET", "/v1/contradictions/cases"),
        ("GET", "/v1/contradictions/cases/{contradiction_case_id}"),
        ("POST", "/v1/contradictions/cases/{contradiction_case_id}/resolve"),
        ("GET", "/v1/trust/signals"),
    ),
    HYGIENE_PAGE: (
        ("GET", "/v0/memories/hygiene-dashboard"),
        ("GET", "/v0/threads/health-dashboard"),
    ),
    TRACING_PAGE: (
        ("GET", "/v0/continuity/recall"),
        ("GET", "/v0/continuity/resumption-brief"),
        ("GET", "/v0/continuity/retrieval-runs"),
        ("GET", "/v0/continuity/retrieval-runs/{retrieval_run_id}"),
    ),
    EVALS_PAGE: (
        ("GET", "/v1/evals/suites"),
        ("POST", "/v1/evals/runs"),
        ("GET", "/v1/evals/runs"),
        ("GET", "/v1/evals/runs/{eval_run_id}"),
    ),
    BRIEFING_PAGE: (
        ("POST", "/v0/task-briefs/compile"),
        ("GET", "/v0/task-briefs/{task_brief_id}"),
        ("POST", "/v0/task-briefs/compare"),
    ),
    WALKTHROUGH_PAGE: (),
}

CLI_COMMANDS = {
    MUTATIONS_PAGE: (("mutations", "generate"), ("mutations", "candidates"), ("mutations", "commit"), ("mutations", "operations")),
    CONTRADICTIONS_PAGE: (
        ("contradictions", "detect"),
        ("contradictions", "list"),
        ("contradictions", "show"),
        ("contradictions", "resolve"),
        ("trust", "signals"),
    ),
    HYGIENE_PAGE: (("status",),),
    TRACING_PAGE: (("recall",), ("resume",)),
    EVALS_PAGE: (("evals", "suites"), ("evals", "run"), ("evals", "runs"), ("evals", "show")),
}

# Test files that keep each page's surface working in CI.
KEPT_WORKING_BY = {
    MUTATIONS_PAGE: ("tests/integration/test_memory_mutations_api.py", "tests/integration/test_cli_integration.py"),
    CONTRADICTIONS_PAGE: ("tests/integration/test_contradictions_api.py", "tests/integration/test_cli_integration.py"),
    HYGIENE_PAGE: ("tests/unit/test_conversation_health.py", "apps/web/app/continuity/page.test.tsx"),
    TRACING_PAGE: (
        "tests/integration/test_continuity_recall_api.py",
        "tests/integration/test_cli_integration.py",
        "tests/integration/test_mcp_server.py",
    ),
    EVALS_PAGE: ("tests/integration/test_public_evals_api.py", "tests/integration/test_cli_integration.py"),
    BRIEFING_PAGE: ("tests/integration/test_task_briefing_api.py", "tests/integration/test_mcp_server.py"),
    WALKTHROUGH_PAGE: ("tests/integration/test_phase9_eval.py",),
}

TITLES = {
    MUTATIONS_PAGE: "# Automated Memory Operations",
    CONTRADICTIONS_PAGE: "# Contradictions and Trust Calibration",
    HYGIENE_PAGE: "# Memory Hygiene and Conversation Health",
    TRACING_PAGE: "# Hybrid Retrieval Tracing",
    EVALS_PAGE: "# Public Eval Harness",
    BRIEFING_PAGE: "# Task-Adaptive Briefing (Legacy Compatibility)",
    WALKTHROUGH_PAGE: "# Local Command Walkthrough",
}


def _mcp_status() -> str:
    return (
        "- Support status: legacy surface. Its MCP tools are off the default handshake, and the legacy MCP surface is "
        "frozen: it gets no new capabilities. The commands, routes and tools on this page are kept working: CI runs "
        "their tests against Postgres. New integrations use the core tools."
    )


def _mcp_backend(count: int) -> str:
    return (
        "- Backend: Postgres, for the CLI, the HTTP API and the MCP tools alike. The `alicebot` CLI refuses a SQLite "
        f"URL and the HTTP API runs only on Postgres. The {WORDS[count]} MCP tools below read the continuity store, "
        "which exists only on Postgres: on the SQLite `alice-memory` vault they are listed but their calls fail. "
        f"See {FULL_STACK}."
    )


_MCP_SETTINGS_HEAD = (
    f"- Settings: the MCP tools need `{MCP_LEGACY_TOOLS_ENV}=1` in the server environment, on a keyless server. "
    f"A server bound with `{AGENT_API_KEY_ENV}` never lists or accepts them."
)


def _expected_header(page: str) -> str:
    """The exact opening of each page: title, then the three bullets of "Requirements and status"."""

    count = len(MCP_TOOLS[page])
    if page in (MUTATIONS_PAGE, CONTRADICTIONS_PAGE):
        bullets = (
            _mcp_status(),
            _mcp_backend(count),
            f"{_MCP_SETTINGS_HEAD} The CLI commands and the HTTP routes need no flag.",
        )
    elif page == TRACING_PAGE:
        bullets = (
            _mcp_status(),
            _mcp_backend(count),
            f"{_MCP_SETTINGS_HEAD} `RETRIEVAL_TRACE_RETENTION_DAYS` sets how long stored traces are kept. "
            "The CLI flags and the HTTP routes need no other flag.",
        )
    elif page == BRIEFING_PAGE:
        bullets = (
            "- Support status: legacy surface. Its MCP tools are off the default handshake, and the HTTP routes and "
            "the CLI command do not exist unless the flag below is set. The surface is deprecated for removal before "
            "`1.0`, and until then it is kept working: CI runs its tests against Postgres with the flag set. New "
            "integrations use the core tools.",
            _mcp_backend(count),
            f"- Settings: `{LEGACY_SURFACES_ENV}=1` at process start for the HTTP routes and the CLI command. The "
            f"three MCP tools need `{LEGACY_SURFACES_ENV}=1` and `{MCP_LEGACY_TOOLS_ENV}=1` in the server "
            f"environment, on a keyless server. A server bound with `{AGENT_API_KEY_ENV}` never lists or accepts them.",
        )
    elif page == HYGIENE_PAGE:
        bullets = (
            "- Support status: legacy surface of the Postgres stack. It is not part of the default SQLite install. "
            "It is kept working: CI runs its unit and web tests. New integrations use the core MCP tools.",
            "- Backend: Postgres. The two HTTP routes belong to the HTTP API, which runs only on Postgres, and the two "
            "web panels read those routes, so they need the API on port 8000 and the web console (see "
            f"{FULL_STACK}). `alicebot status` refuses a SQLite URL.",
            "- Settings: none beyond the Postgres setup. This page names no MCP tool, so `ALICE_MCP_LEGACY_TOOLS` "
            "plays no part.",
        )
    elif page == EVALS_PAGE:
        bullets = (
            "- Support status: legacy surface of the Postgres stack. It is not part of the default SQLite install. "
            "It is kept working: CI runs `evals run` against Postgres and compares the report with the checked-in "
            "baseline. New integrations use the core MCP tools.",
            "- Backend: Postgres. The `alicebot` CLI refuses a SQLite URL and the HTTP API runs only on Postgres. The "
            f"harness needs a migrated database and a valid Alice user id (see {FULL_STACK}). `evals run` also writes "
            "its run and result rows to that database.",
            "- Settings: none. This page names no MCP tool, so `ALICE_MCP_LEGACY_TOOLS` plays no part, and no "
            "`ALICE_LEGACY_SURFACES` flag is needed.",
        )
    else:
        assert page == WALKTHROUGH_PAGE
        bullets = (
            "- Support status: legacy walkthrough of the Postgres stack. It is not part of the default SQLite "
            "install. The scripts it runs are kept working: CI runs the integration tests of the Phase 9 evaluation "
            "against Postgres. New integrations use the core MCP tools.",
            "- Backend: Postgres from `docker compose`, the repo's `.venv` from `make setup`, and the API on port "
            f"8000 for the health check (see {FULL_STACK}). The OpenClaw demo and the evaluation script write to "
            "that database. The SQLite `alice-memory` vault does not take part.",
            "- Settings: none beyond `DATABASE_URL`, which the scripts default to the local Docker Postgres. No MCP "
            "tool is involved, so `ALICE_MCP_LEGACY_TOOLS` plays no part.",
        )
    return f"{TITLES[page]}\n\n## Requirements and status\n\n" + "\n".join(bullets) + "\n"


@pytest.mark.parametrize("page", sorted(TITLES))
def test_each_legacy_page_opens_with_its_backend_settings_and_status(page: str) -> None:
    """The first section of the page, before any other heading, says what it needs and how it is supported.

    Mutations: move the section below another heading; change one bullet; put the sprint id
    back into the title; delete the Postgres sentence.
    """

    text = _text(page)
    assert text.startswith(_expected_header(page)), page
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings[0] == TITLES[page]
    assert headings[1] == "## Requirements and status"
    assert not re.search(r"^# P1[0-9]-S[0-9]", text, flags=re.MULTILINE)


SPRINT_VOICE = (
    "This sprint",
    "this sprint",
    "Sprint verification",
    "sprint verification",
    "Current branch",
    "current branch",
    "shipped branch",
    "P12-S",
    "P13-S",
    "New commands",
    "New tools",
    "Phase 12 /v0",
    "now runs as",
    "now syncs",
    "Explain output now",
    "remains green",
    "default v0.11 surface",
    "Existing non-debug recall",
)


@pytest.mark.parametrize("page", sorted(TITLES))
def test_the_legacy_pages_no_longer_describe_current_behaviour_in_sprint_voice(page: str) -> None:
    """Sprint-closeout phrasing is gone from the seven pages, and the sentences that replaced it are in.

    Mutations: put "This sprint" or "Current branch behavior" back into one page; delete a
    replacement sentence.
    """

    flat = _flat(page)
    for phrase in SPRINT_VOICE:
        assert phrase not in flat, (page, phrase)

    replacements = {
        MUTATIONS_PAGE: (
            "The mutation layer is an explicit step for post-turn continuity handling. The flow separates:",
            "`DELETE` goes through the existing continuity correction path as a logical tombstone.",
            "Two audit tables back the flow:",
            "These are legacy MCP tools. Their requirements are at the top of this page.",
        ),
        CONTRADICTIONS_PAGE: (
            "Contradiction state and trust adjustments are explicit across continuity review, explain, recall, CLI, "
            "API, and MCP surfaces.",
            "`active` and `stale` objects are live candidates, while",
            "Trust signals are ledger rows in `trust_signals`.",
            "Two tables back this:",
            "Recall syncs contradiction state for in-scope candidates before ranking.",
            "Tests cover:",
        ),
        HYGIENE_PAGE: (
            "Two bounded visibility surfaces cover memory hygiene and conversation health. They aggregate existing "
            "data and have no storage of their own.",
            "The dashboards add no connector, runtime, persistence, or retrieval substrate.",
            "Thread health is visible through both the API and the web panel.",
        ),
        TRACING_PAGE: (
            "The `/v0/continuity` retrieval runs as an explicit hybrid pipeline, not a single opaque ranking pass.",
            "Recall and resumption payloads carry the trace only when `debug` is requested.",
            "The CLI takes `recall --debug` and `resume --debug`.",
        ),
        EVALS_PAGE: (
            "The public harness is a reproducible local eval surface for Alice's quality.",
            "The public harness measures these continuity behaviors:",
            "It does not change retrieval, mutation, or contradiction behavior.",
            "- Baseline report artifact: `eval/baselines/public_eval_harness_v1.json`",
        ),
        BRIEFING_PAGE: ("The feature is not part of the default surface.",),
        WALKTHROUGH_PAGE: (),
    }
    for sentence in replacements[page]:
        assert sentence in flat, (page, sentence)


def _handlers_reaching(name: str, module_paths: list[Path]) -> set[str]:
    """Every function name reachable from a handler through names the MCP package defines."""

    references: dict[str, set[str]] = {}
    for path in module_paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = references.setdefault(node.name, set())
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Name):
                        names.add(inner.id)
                    elif isinstance(inner, ast.Attribute):
                        names.add(inner.attr)
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(references.get(current, ()))
    return seen


@pytest.mark.parametrize("page", [name for name, tools in sorted(MCP_TOOLS.items()) if tools])
def test_the_tools_a_legacy_page_names_are_legacy_and_need_postgres(
    page: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each named tool is a legacy tool whose handler opens the Postgres-only continuity store.

    That store raises on a SQLite vault (`_store_context`), which is why the page says the
    tool is listed there and fails. The flag rules are read from `list_mcp_tools`: the tools
    list only with `ALICE_MCP_LEGACY_TOOLS=1`, never with an agent key, and the task-brief
    tools also need `ALICE_LEGACY_SURFACES=1`. The names on the page are the names the test
    pins.

    Mutations: move a named tool into the core definitions; make a named handler open
    `_vnext_store_context` instead; add a task-brief tool to the default handshake; list the
    tools with an agent key set.
    """

    tools = MCP_TOOLS[page]
    flat = _flat(page)
    # The "frozen" in the status line is the sentence the MCP tools page says about the legacy surface.
    assert "the legacy surface is frozen and will not gain new capabilities" in _flat("docs/alpha/mcp-tools.md")
    if page != BRIEFING_PAGE:
        assert "the legacy MCP surface is frozen: it gets no new capabilities." in flat
    mentioned = sorted(set(re.findall(r"`(alice_[a-z_]+)`", flat)) - {"alice_core"})
    assert set(mentioned) >= set(tools), page
    # A tool the page names beyond its own surface must still be a real tool.
    known = registry._CORE_TOOL_NAMES | registry._LEGACY_TOOL_NAMES
    assert [name for name in mentioned if name not in known] == [], page

    modules = sorted((ROOT / "apps/api/src/alicebot_api/mcp").glob("*.py"))
    for name in tools:
        assert name in registry._LEGACY_TOOL_NAMES, name
        assert name not in registry._CORE_TOOL_NAMES, name
        handler = registry._TOOL_HANDLERS[name]
        reached = _handlers_reaching(handler.__name__, modules)
        assert "_store_context" in reached, name

    def listed(**env: str) -> set[str]:
        for variable in (AGENT_API_KEY_ENV, MCP_LEGACY_TOOLS_ENV, MCP_FULL_TOOLS_ENV, LEGACY_SURFACES_ENV):
            monkeypatch.delenv(variable, raising=False)
        for variable, value in env.items():
            monkeypatch.setenv(variable, value)
        return {str(tool["name"]) for tool in registry.list_mcp_tools()}

    task_brief = set(tools) <= registry._TASK_BRIEF_TOOL_NAMES
    assert not set(tools) & listed()
    assert not set(tools) & listed(**{AGENT_API_KEY_ENV: "alice_sk_x", MCP_LEGACY_TOOLS_ENV: "1", LEGACY_SURFACES_ENV: "1"})
    if task_brief:
        assert set(tools) == registry._TASK_BRIEF_TOOL_NAMES
        assert not set(tools) & listed(**{MCP_LEGACY_TOOLS_ENV: "1"})
        assert not set(tools) & listed(**{LEGACY_SURFACES_ENV: "1"})
        assert set(tools) <= listed(**{MCP_LEGACY_TOOLS_ENV: "1", LEGACY_SURFACES_ENV: "1"})
    else:
        assert not set(tools) & registry._TASK_BRIEF_TOOL_NAMES
        assert set(tools) <= listed(**{MCP_LEGACY_TOOLS_ENV: "1"})


def test_the_cli_commands_and_http_routes_a_legacy_page_names_exist_and_are_gated_as_it_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI commands and routes on the seven pages exist; only the task-brief ones need the legacy flag.

    The pages say the HTTP API and the `alicebot` CLI run only on Postgres. The CLI refuses a
    SQLite URL by a ValueError in `_build_context`; the HTTP stack has no SQLite branch in
    `main`, its routers or its settings.

    Mutations: rename a command in the parser; remove a route from the router; put a task-brief
    route outside `LEGACY_HTTP_OPERATION_KEYS`; put an ungated route into it; register
    `task-briefs` without the flag.
    """

    monkeypatch.delenv(LEGACY_SURFACES_ENV, raising=False)
    parser = build_parser()

    def subcommands(command: tuple[str, ...]) -> set[str]:
        current = parser  # the enclosing name, read at call time
        for name in command:
            actions = [a for a in current._actions if a.__class__.__name__ == "_SubParsersAction"]
            assert len(actions) == 1, command
            assert name in actions[0].choices, (command, name)
            current = actions[0].choices[name]
        return {
            choice
            for action in current._actions
            if action.__class__.__name__ == "_SubParsersAction"
            for choice in action.choices
        }

    for commands in CLI_COMMANDS.values():
        for command in commands:
            subcommands(command)
    assert "task-briefs" not in subcommands(())
    # The parser reads the flag when it is built, so build it again with the flag set.
    monkeypatch.setenv(LEGACY_SURFACES_ENV, "1")
    parser = build_parser()
    assert "task-briefs" in subcommands(())
    monkeypatch.delenv(LEGACY_SURFACES_ENV, raising=False)

    schema_paths = api_main.app.openapi()["paths"]
    gated = api_main.LEGACY_HTTP_OPERATION_KEYS
    for page, routes in HTTP_ROUTES.items():
        flat = _flat(page)
        for method, path in routes:
            if page == BRIEFING_PAGE:
                assert (method, path) in gated, (method, path)
                if api_main.LEGACY_SURFACES_ENABLED:
                    assert method.lower() in schema_paths[path], (method, path)
                else:
                    assert path not in schema_paths, (method, path)
            else:
                assert (method, path) not in gated, (method, path)
                assert method.lower() in schema_paths[path], (method, path)
            # The page writes each route as `METHOD /path`, with or without a query string.
            assert re.search(rf"`{method} {re.escape(path)}[`?]", flat), (page, method, path)

    assert "the 'alicebot' CLI requires a Postgres DATABASE_URL" in _text("apps/api/src/alicebot_api/cli/shared.py")
    assert "sqlite" not in _text("apps/api/src/alicebot_api/main.py").casefold()
    for router in sorted((ROOT / "apps/api/src/alicebot_api/routers").glob("*.py")):
        assert "sqlite" not in router.read_text(encoding="utf-8").casefold(), router.name


def test_ci_keeps_each_legacy_surface_working() -> None:
    """The workflow runs the integration tests on Postgres with the legacy flag, and each page's tests exist.

    Mutations: drop the legacy flag from the integration step; remove the Postgres service;
    delete one of the named test files.
    """

    workflow = _text(".github/workflows/tests.yml")
    assert "pgvector/pgvector" in workflow
    assert f"run: {LEGACY_SURFACES_ENV}=1 ./.venv/bin/python -m pytest tests/integration" in workflow
    assert "run: pnpm test" in [line.strip() for line in workflow.splitlines()]
    for page, tests in KEPT_WORKING_BY.items():
        for name in tests:
            assert (ROOT / name).is_file(), (page, name)
    assert "alice_task_brief" in _text("tests/integration/test_mcp_server.py")
    assert "alice_recall_debug" in _text("tests/integration/test_mcp_server.py")
    # The evals page says CI compares the report that `evals run` writes with the checked-in baseline.
    cli_integration = _text("tests/integration/test_cli_integration.py")
    assert 'run_cli(\n        ["evals", "run", "--report-path", str(report_path)]' in cli_integration
    assert 'baseline_payload = json.loads((REPO_ROOT / "eval" / "baselines" / "public_eval_harness_v1.json")' in cli_integration
    assert "assert written_payload == baseline_payload" in cli_integration
    assert (ROOT / "eval" / "baselines" / "public_eval_harness_v1.json").is_file()


LEGACY_SECTION = "Legacy surfaces"
LEGACY_LINKS = (
    ("Automated memory operations", "../memory/p12-s2-automated-memory-operations.md", MUTATIONS_PAGE),
    ("Contradictions and trust calibration", "../memory/p12-s3-contradictions-trust-calibration.md", CONTRADICTIONS_PAGE),
    ("Memory hygiene and conversation health", "../memory/p13-s3-memory-hygiene-conversation-health.md", HYGIENE_PAGE),
    ("Hybrid retrieval tracing", "../retrieval/hybrid_tracing.md", TRACING_PAGE),
    ("Public eval harness", "../evals/public_eval_harness.md", EVALS_PAGE),
    ("Task-adaptive briefing", "../briefing/task-adaptive-briefing.md", BRIEFING_PAGE),
    ("Local command walkthrough", "../examples/phase9-command-walkthrough.md", WALKTHROUGH_PAGE),
)


def test_the_cli_reference_links_all_seven_legacy_pages_under_legacy_surfaces() -> None:
    """One bullet per page, each link resolves, and the section says what the pages have in common.

    Mutations: remove one bullet; point a link at a missing file; rename the heading; delete
    the sentence about the opening section.
    """

    section = _section(CLI_REFERENCE, LEGACY_SECTION)
    bullets = [line for line in section.splitlines() if line.startswith("- [")]
    assert len(bullets) == len(LEGACY_LINKS) == 7
    for (label, link, page), bullet in zip(LEGACY_LINKS, bullets, strict=True):
        assert bullet.startswith(f"- [{label}]({link}): "), bullet
        assert ((ROOT / CLI_REFERENCE).parent / link).resolve() == (ROOT / page).resolve()
        assert (ROOT / page).is_file()
    flat = " ".join(section.split())
    assert (
        "These seven pages describe commands, routes and tools of the Postgres stack. Each opens with the backend it "
        "needs, the settings it needs and its support status."
    ) in flat
    assert "They are legacy surfaces: kept working and not part of the default install." in flat
