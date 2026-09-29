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

A non-empty data directory that is still relative after ``~`` expansion
is not fail-open. The process exits 0 and prints
``Alice: the data directory "<value>" is not an absolute path; set an absolute path.``
on stdout (inside ``additionalContext`` for JSON) and on stderr. An
empty ``--data-dir`` still falls through to ``$ALICE_MEMORY_DATA_DIR``
and then ``~/.alice``.
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
from alicebot_api.onramp import DEFAULT_USER_EMAIL, bootstrap_database, resolve_db_path
from alicebot_api.session_briefing import compile_local_session_brief

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
            "data directory that is not absolute after ~ expansion exits 0 "
            "and names that value instead of creating a vault."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            f"Vault directory. Must be absolute after ~ expansion. "
            f"Defaults to ${ALICE_MEMORY_DATA_DIR_ENV} or {DEFAULT_DATA_DIR}."
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


def _run(args: argparse.Namespace) -> int:
    try:
        sys.stdin.read()
    except OSError as exc:
        logger.debug("session-start stdin was not readable: %s", exc)

    data_dir = args.data_dir or os.environ.get(ALICE_MEMORY_DATA_DIR_ENV) or DEFAULT_DATA_DIR
    if data_dir and not Path(data_dir).expanduser().is_absolute():
        line = (
            f'Alice: the data directory "{data_dir}" is not an absolute path; '
            "set an absolute path."
        )
        print(line, file=sys.stderr, flush=True)
        _emit_context(line, output_format=args.format)
        return 0
    db_path = resolve_db_path(data_dir=data_dir, db=None)
    bootstrap_database(
        db_path,
        user_id=UUID(str(args.user_id)),
        user_email=DEFAULT_USER_EMAIL,
        secure_parent=True,
    )
    markdown = compile_local_session_brief(
        db_path,
        user_id=args.user_id,
        query=None,
    )
    if "jsonrpc" in markdown or "Content-Length:" in markdown:
        _fail_open(args.format)
        return 0
    _emit_context(markdown.rstrip("\n"), output_format=args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
