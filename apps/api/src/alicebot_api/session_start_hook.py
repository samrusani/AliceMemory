"""Host session-start wrapper around the local session brief.

Cursor ``sessionStart`` and Claude Code ``SessionStart`` both consume
this process. Stdout is JSON with ``additional_context`` (Cursor) and
``hookSpecificOutput.additionalContext`` (Claude Code). OpenClaw can
run the same command with ``--format markdown``, or call
``alice-memory brief`` and read the markdown on stdout.

The brief is compiled in-process. A PATH ``alice-memory`` child would
have been a new Bandit finding for no extra safety: the argv was a
fixed list, and Cursor's GUI PATH often does not contain the command.

Fail open: JSON writes ``{}`` and exits 0. After ``--format markdown``
is known, fail-open is a single blank line and exit 0. If argparse
fails before format is known, ``{}`` is still correct for the default
JSON host. Never failClosed. Never print MCP protocol on stdout.

A non-empty ``--data-dir`` that is not absolute after ``~`` expansion is
not fail-open. The command exits 0 and prints one line, in the chosen
format and on stderr: ``Alice: the data directory "<value>" is not an
absolute path; set an absolute path.`` In JSON that line is
``additionalContext``. It does not start with ``{`` or ``[``. An empty
``--data-dir`` still falls back to ``$ALICE_MEMORY_DATA_DIR``, then
``~/.alice``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

from alicebot_api.mcp_server import _DEFAULT_MCP_USER_ID
from alicebot_api.onramp import (
    DEFAULT_USER_EMAIL,
    bootstrap_database,
    data_dir_absolute_after_tilde,
    resolve_db_path,
)
from alicebot_api.session_briefing import (
    brief_char_len,
    compile_local_session_brief,
    fit_emitted_session_brief,
)

ALICE_MEMORY_DATA_DIR_ENV = "ALICE_MEMORY_DATA_DIR"
DEFAULT_DATA_DIR = "~/.alice"
logger = logging.getLogger(__name__)


def _fail_open(output_format: str | None = None) -> None:
    if output_format == "markdown":
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    sys.stdout.write("{}\n")
    sys.stdout.flush()


def _emit_context(markdown: str, *, output_format: str) -> None:
    if output_format == "markdown":
        sys.stdout.write(markdown)
        if markdown and not markdown.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    payload = {
        "additional_context": markdown,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": markdown,
        },
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    output_format: str | None = None
    try:
        args = _parse_args(raw)
        output_format = args.format
        return _run(args)
    except SystemExit as exc:
        if exc.code in (0, None):
            raise
        _fail_open(output_format)
        return 0
    except Exception:
        _fail_open(output_format)
        return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alice-memory-session-start",
        description=(
            "Read a host session-start payload on stdin and print a session "
            "brief for injection. JSON failures write {} and exit 0. "
            "Markdown failures write a blank line and exit 0. A non-empty "
            "--data-dir that is not absolute after ~ expansion prints one "
            "line instead of that fail-open output, in the chosen format "
            "and on stderr, and still exits 0."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            f"Vault directory. Defaults to ${ALICE_MEMORY_DATA_DIR_ENV} or "
            f"{DEFAULT_DATA_DIR} when omitted or empty. A non-empty value "
            "must be absolute after ~ expansion."
        ),
    )
    parser.add_argument(
        "--user-id",
        default=_DEFAULT_MCP_USER_ID,
        help=f"Acting local user UUID. Defaults to {_DEFAULT_MCP_USER_ID}.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="json for Cursor/Claude Code; markdown for hosts that want the brief text.",
    )
    return parser.parse_args(argv)


def _data_dir_refusal_line(value: str) -> str:
    return (
        f'Alice: the data directory "{value}" is not an absolute path; '
        "set an absolute path."
    )


def _emit_data_dir_refusal(value: str, output_format: str) -> None:
    """One refusal line on stdout, in the chosen format, and on stderr."""

    line = _data_dir_refusal_line(value)
    print(line, file=sys.stderr)
    if output_format == "markdown":
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return
    _emit_context(line, output_format="json")


def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdin.read()
    except OSError as exc:
        logger.debug("session-start stdin was not readable: %s", exc)

    if args.data_dir and not data_dir_absolute_after_tilde(args.data_dir):
        _emit_data_dir_refusal(args.data_dir, args.format)
        return 0

    data_dir = args.data_dir or os.environ.get(ALICE_MEMORY_DATA_DIR_ENV) or DEFAULT_DATA_DIR
    db_path = resolve_db_path(data_dir=data_dir, db=None)
    bootstrap_database(
        db_path,
        user_id=UUID(str(args.user_id)),
        user_email=DEFAULT_USER_EMAIL,
        secure_parent=True,
    )
    duplicate = _claude_duplicate_setup_line()
    prefix = f"{duplicate}\n" if duplicate else ""
    markdown = compile_local_session_brief(
        db_path,
        user_id=args.user_id,
        query=None,
        reserve=brief_char_len(prefix),
    )
    markdown = fit_emitted_session_brief(prefix + markdown)
    if "jsonrpc" in markdown or "Content-Length:" in markdown:
        _fail_open(args.format)
        return 0
    _emit_context(markdown.rstrip("\n"), output_format=args.format)
    return 0


_CLAUDE_DUPLICATE_LINE = (
    "Alice is set up twice in Claude Code. Run `claude mcp remove alice --scope user` "
    "and remove the alice-memory-session-start hook from ~/.claude/settings.json."
)


def _claude_duplicate_setup_line() -> str | None:
    """One brief line when this process is the plugin hook and install's entries exist.

    ``CLAUDE_PLUGIN_ROOT`` is set only for the plugin's hook. Older cached
    plugin versions do not look for it, so they are unchanged.
    """

    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return None
    home = Path.home()
    claude_json = _read_json_object(home / ".claude.json")
    settings = _read_json_object(home / ".claude" / "settings.json")
    servers = claude_json.get("mcpServers") if isinstance(claude_json, dict) else None
    has_server = isinstance(servers, dict) and "alice" in servers
    has_hook = False
    if isinstance(settings, dict):
        from alicebot_api.host_install import _existing_alice_hook_command

        has_hook = _existing_alice_hook_command(settings, "claude-code") is not None
    if not has_server and not has_hook:
        return None
    return _CLAUDE_DUPLICATE_LINE


def _read_json_object(path: Path) -> object:
    """One JSON file, or None when it is missing or cannot be read.

    A bad encoding, a broken document, or a document nested too deeply
    leaves the brief in place.
    """

    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
