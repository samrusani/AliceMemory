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

The hook never inspects the content of a stored note. The brief always
opens with ``SESSION_BRIEF_FRAME``, and every stored string is flattened
onto one line and JSON-quoted behind a label, so a note cannot be protocol
framing whatever it says. A note about ``jsonrpc`` or ``Content-Length:``
is an ordinary note. Through v0.19.0 this module dropped the whole brief
when the text held either string. That check dates from v0.16.0, when the
hook parsed the output of a child ``alice-memory brief`` process, and it
had no purpose once brief compilation moved in-process.

Per-project memory (spec 4.2, 6.4). With scoping on, the project comes from the
folder the host started the session in: ``--project-dir``, ``$ALICE_PROJECT_DIR``,
the ``cwd`` string in the JSON the host sends on stdin (Claude Code and Codex send
the launch folder, which the host-evidence run recorded), then the working
folder. The hook parses at most 64 KiB of stdin and reads and discards the rest.
The resolver walks up to the git root, so a subfolder finds its repository. A
failed detection is not a failed hook: the brief carries a plain status line.
While scoping is off, which is the release default until the flip, the hook
detects nothing and prints exactly what v0.20.0 printed.

A data directory that is not absolute after ``~`` expansion is not
fail-open. That covers a non-empty ``--data-dir``, a non-empty
``$ALICE_MEMORY_DATA_DIR`` when it is the value in use, and the plugin
option below. The command exits 0 and prints one line, in the chosen
format and on stderr: ``Alice: the data directory "<value>" is not an
absolute path; set an absolute path.`` In JSON that line is
``additionalContext``. It does not start with ``{`` or ``[``. An empty
``--data-dir`` is the same as none: it falls back to
``$ALICE_MEMORY_DATA_DIR``, then ``~/.alice``. An empty
``$ALICE_MEMORY_DATA_DIR`` is the same as unset. Nothing is created for a
refused value. With no value at all outside plugin mode the built-in
``~/.alice`` default is used and is not checked. The value in the line has
control characters escaped and is cut at 200 characters.

Plugin mode. Claude Code sets ``CLAUDE_PLUGIN_ROOT`` for a plugin's hooks
and servers and not for a hook in ``settings.json``, so a non-empty value
marks this command as the Claude Code plugin's hook. With no ``--data-dir``
in that mode the data directory is ``$CLAUDE_PLUGIN_OPTION_DATA_DIR`` when
it is set and non-empty, else ``~/.alice``. ``$ALICE_MEMORY_DATA_DIR`` is
ignored, so the hook and the plugin's server, which reads the same plugin
option and defaults to the same folder, always open one vault. The
absolute-after-``~`` rule above applies to the option value. The plugin's
hook carries no ``--data-dir`` because Claude Code does not run a hook whose
arguments reference a plugin option that is unset. Outside plugin mode
nothing changes, and an explicit ``--data-dir`` wins everywhere.
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
from alicebot_api.project_identity import (
    MAX_HOOK_PAYLOAD_BYTES,
    hook_payload_cwd,
    host_platform,
    read_hook_payload,
)
from alicebot_api.project_view import ProjectView, resolve_view_at_edge, working_folder
from alicebot_api.session_briefing import (
    brief_char_len,
    compile_local_session_brief,
    fit_emitted_session_brief,
    sensitive_global_exclusion,
)

ALICE_MEMORY_DATA_DIR_ENV = "ALICE_MEMORY_DATA_DIR"
CLAUDE_PLUGIN_ROOT_ENV = "CLAUDE_PLUGIN_ROOT"
CLAUDE_PLUGIN_OPTION_DATA_DIR_ENV = "CLAUDE_PLUGIN_OPTION_DATA_DIR"
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
            "--data-dir, or a non-empty $ALICE_MEMORY_DATA_DIR in use, that is "
            "not absolute after ~ expansion prints one "
            "line instead of that fail-open output, in the chosen format "
            "and on stderr, and still exits 0. Inside the Claude Code plugin "
            f"(${CLAUDE_PLUGIN_ROOT_ENV} set) with no --data-dir, the vault is "
            f"${CLAUDE_PLUGIN_OPTION_DATA_DIR_ENV} when set, else "
            f"{DEFAULT_DATA_DIR}, and ${ALICE_MEMORY_DATA_DIR_ENV} is ignored."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            f"Vault directory. Defaults to ${ALICE_MEMORY_DATA_DIR_ENV} or "
            f"{DEFAULT_DATA_DIR} when omitted or empty (in the Claude Code "
            f"plugin, ${CLAUDE_PLUGIN_OPTION_DATA_DIR_ENV} or {DEFAULT_DATA_DIR}). "
            "A non-empty value, from this flag or from "
            f"${ALICE_MEMORY_DATA_DIR_ENV}, must be absolute after ~ expansion."
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
    parser.add_argument(
        "--project-dir",
        default=None,
        help=(
            "Folder that decides which project the brief is for, when per-project scoping "
            "is on. Without it: $ALICE_PROJECT_DIR, then the folder the host sends on stdin "
            "as cwd, then the working folder. It must be absolute and exist, or the next "
            "source is used. Ignored while scoping is off."
        ),
    )
    return parser.parse_args(argv)


_REFUSAL_VALUE_LIMIT = 200


def _refusal_value(value: str) -> str:
    """The refused value as one short printable line.

    The value comes from a flag or an environment variable, and the refusal
    line goes into the model's context without the brief's size guards. A
    control character or line break is written as an escape (``\\n``), and the
    text is cut at 200 characters with ``...``. A normal path is unchanged.
    """

    shown: list[str] = []
    used = 0
    for char in value:
        piece = char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
        if used + len(piece) > _REFUSAL_VALUE_LIMIT:
            shown.append("...")
            break
        shown.append(piece)
        used += len(piece)
    return "".join(shown)


def _data_dir_refusal_line(value: str) -> str:
    return (
        f'Alice: the data directory "{_refusal_value(value)}" is not an absolute path; '
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


def _read_stdin_payload() -> bytes | None:
    """The host's stdin payload, at most 64 KiB of it, or ``None``.

    The rest of a larger payload is read and thrown away, so the host's write never
    blocks. Hosts close stdin after the payload, as they did when this hook read it
    whole and threw it away.
    """

    stream = sys.stdin
    try:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            return read_hook_payload(buffer)
        data = stream.read(MAX_HOOK_PAYLOAD_BYTES + 1)
        if len(data) <= MAX_HOOK_PAYLOAD_BYTES:
            return data.encode("utf-8", "replace") if isinstance(data, str) else bytes(data)
        while stream.read(MAX_HOOK_PAYLOAD_BYTES):
            pass
        return None
    except (OSError, ValueError) as exc:
        logger.debug("session-start stdin was not readable: %s", exc)
        return None


def _project_view_for_hook(
    args: argparse.Namespace, db_path: Path, payload: bytes | None
) -> ProjectView:
    """The view for this session: the project of the folder the host started in.

    The folder is ``--project-dir``, ``$ALICE_PROJECT_DIR``, the ``cwd`` the host
    sent on stdin, then the working folder. While scoping is off, nothing is
    detected and nothing is printed that v0.20.0 did not print.
    """

    environ = os.environ
    platform = host_platform(environ)
    hook_cwd = hook_payload_cwd(payload, platform=platform)
    return resolve_view_at_edge(
        db_path=db_path,
        environ=environ,
        argument_dir=args.project_dir,
        hook_cwd=hook_cwd,
        process_cwd=working_folder(),
        platform=platform,
    ).view


def _run(args: argparse.Namespace) -> int:
    payload = _read_stdin_payload()

    requested = args.data_dir
    if not requested and _plugin_mode():
        requested = os.environ.get(CLAUDE_PLUGIN_OPTION_DATA_DIR_ENV) or DEFAULT_DATA_DIR
    if not requested:
        requested = os.environ.get(ALICE_MEMORY_DATA_DIR_ENV) or ""
    # A value somebody gave is checked: --data-dir, the plugin option (or its
    # ~/.alice default in plugin mode), and a non-empty variable. With none of
    # them outside plugin mode the built-in ~/.alice default is used unchecked,
    # as before. A refused value creates nothing.
    if requested and not data_dir_absolute_after_tilde(requested):
        _emit_data_dir_refusal(requested, args.format)
        return 0

    db_path = resolve_db_path(data_dir=requested or DEFAULT_DATA_DIR, db=None)
    bootstrap_database(
        db_path,
        user_id=UUID(str(args.user_id)),
        user_email=DEFAULT_USER_EMAIL,
        secure_parent=True,
    )
    duplicate = _claude_duplicate_setup_line()
    prefix = f"{duplicate}\n" if duplicate else ""
    view = _project_view_for_hook(args, db_path, payload)
    markdown = compile_local_session_brief(
        db_path,
        user_id=args.user_id,
        query=None,
        project_view=view,
        exclude_global_domains=sensitive_global_exclusion(view),
        reserve=brief_char_len(prefix),
    )
    markdown = fit_emitted_session_brief(prefix + markdown)
    _emit_context(markdown.rstrip("\n"), output_format=args.format)
    return 0


def _plugin_mode() -> bool:
    """True when this process is the Claude Code plugin's hook.

    Claude Code sets ``CLAUDE_PLUGIN_ROOT`` for a plugin's hooks and servers
    and not for a hook in ``settings.json``. This command runs only as a hook,
    so a non-empty value means the plugin's hook. An empty value is not
    plugin mode. Older cached plugin versions do not look for it, so they are
    unchanged.
    """

    return bool(os.environ.get(CLAUDE_PLUGIN_ROOT_ENV))


_CLAUDE_DUPLICATE_LINE = (
    "Alice is set up twice in Claude Code. Run `claude mcp remove alice --scope user` "
    "and remove the alice-memory-session-start hook from ~/.claude/settings.json."
)


def _claude_duplicate_setup_line() -> str | None:
    """One brief line when this process is the plugin hook and install's entries exist.

    Plugin mode is ``CLAUDE_PLUGIN_ROOT`` set and non-empty.
    """

    if not _plugin_mode():
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

    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
