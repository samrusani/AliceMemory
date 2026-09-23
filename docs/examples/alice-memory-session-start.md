# Session-start brief

Copy these examples into a host config. Do not drop them into this
repository's `.cursor/hooks.json`; that would fire against the live vault.

The wrapper reads the host payload on stdin, compiles the same brief as
`alice-memory brief` against `ALICE_MEMORY_DATA_DIR` or `--data-dir`
(default `~/.alice`), and prints JSON:

```json
{
  "additional_context": "<markdown brief>",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "<markdown brief>"
  }
}
```

`additional_context` is the Cursor `sessionStart` field.
`hookSpecificOutput.additionalContext` is the Claude Code `SessionStart`
field. One command covers both.

On any error the JSON wrapper prints `{}` and exits 0. After
`--format markdown` is known, fail-open is a single blank line and
exit 0. If argparse fails before format is known, `{}` is still
correct for the default JSON host. It never failCloses and never
prints MCP protocol on stdout.

## Cursor

Copy `docs/examples/cursor-session-start-hooks.json` into the project's
`.cursor/hooks.json`, or merge the `sessionStart` block into an existing
file. Point `ALICE_MEMORY_DATA_DIR` at the vault the host should read.

```bash
export ALICE_MEMORY_DATA_DIR="$HOME/.alice"
```

## Claude Code

Copy `docs/examples/claude-code-session-start-hooks.json` into the Claude
Code hooks file (often `~/.claude/settings.json` under `hooks`, or a
project `.claude/settings.json`). Same environment variable.

Claude Code's shape is not Cursor's. Each `SessionStart` item is a group
whose `hooks` array holds the handler, and the handler needs
`"type": "command"`. Claude Code ignores a flat `{"command": ...}` item;
`claude doctor` lists it under "Invalid settings".

`alice-memory install` (or `--host claude-code`) writes this group into
`~/.claude/settings.json`. The hook runs the same launcher as the Alice
MCP entry in `~/.claude.json`: for a uvx entry,
`uvx <the entry's uvx options> --from <the entry's package spec> alice-memory-session-start`
(`uvx --from alice-memory ...` for a new entry), so it resolves the same
version from the same index, and for an entry that runs the
installed `alice-memory` script by path, `alice-memory-session-start`
from the same directory. Install writes the script path instead of
`uvx` when `uvx` is not on PATH. If that directory has no
`alice-memory-session-start`, install leaves the hook as it was and
prints a warning. `alice-memory-session-start` first shipped in 0.16.0,
so for a uvx entry pinned below it (`alice-memory==0.15.7`, `<0.16` and
so on) install adds no hook and leaves an existing one's command as it
was, and says to pin `alice-memory>=0.16` or remove the pin. Install
never writes a URL into a hook file and never prints one unmasked. It
carries only `--prerelease`, `--python`/`-p`, `--python-preference` and
the flags `--native-tls`, `--offline`, `--no-cache` and `--refresh` from
the entry's uvx options into the hook; with any other option (an index
such as `--index-url`, even a local `--find-links`, a URL in any form,
or something like `--with`), install adds no hook and leaves an existing
one's command as it was. For an index, move it into your user-level uv
config, `~/.config/uv/uv.toml` as `[[index]]` (`%APPDATA%\uv\uv.toml` on Windows),
not a project uv.toml, since the hook runs from the project's directory;
keep its credentials in a keyring, `.netrc`, or
`UV_INDEX_<NAME>_USERNAME` and `UV_INDEX_<NAME>_PASSWORD`; then remove
the option from the entry's args and run install again. The receipt
prints the hook argv install would add after that change
(`session_start_argv_after_change:`). A hook kept this way still follows
the entry's data dir: install changes only its `--data-dir` word, and
when that word is not literal (such as `$HOME/old`) while `--data-dir`
moves the entry, it refuses the hook, says to change its `--data-dir` by
hand, and exits 1; a `--db` entry's hook keeps its own store. The hook's `--data-dir` is the dir that entry's server
opens, read with the server's own argument parser: the one passed to
install, else the entry's `--data-dir` (abbreviated or not, the last one
if repeated), else `~/.alice`. With no entry yet, install uses the data
dir of an existing Alice hook, then `~/.alice`. An existing hook's
`--data-dir` is read the way its shell reads it: a word with every `$`,
backtick and backslash inside single quotes, or with none, is literal, so
`--data-dir "/Users/me/My Vault"` is relied on as written. Reading stops
at the first `;`, `&&`, `||`, `|`, line break or comment, so a
`--data-dir` after them is not the hook's. A
`--data-dir` the shell expands, such as `$HOME/.alice`, `"$HOME/.alice"`
or `~/.alice`, is never relied on: install leaves that hook's command as
it was unless you pass `--data-dir`, and says so when it also replaced
the entry's launcher.

On macOS and Linux the command is quoted with POSIX shell rules
(`shlex.join`), so a data dir such as `/Users/me/my vault` is written as
`--data-dir '/Users/me/my vault'` and reaches the script as one
argument. On Windows, where the hook may run under cmd, PowerShell or
Git Bash, install writes forward slashes and double-quotes a path that
holds a space. It does not write the hook when a path holds `"`, `$`, a
backtick, `%`, `!`, `'`, `{`, `}`, `,`, `[`, `]`, a line break or a quote
PowerShell reads as one, or when the script path itself would need quotes; the receipt
prints the argv (`session_start_argv:`) to add by hand and install exits
1.

Install in v0.16.0 and earlier wrote the flat Cursor item there, which
Claude Code ignored. Re-running install from a version with this fix
replaces that item. Duplicate Alice entries in well-formed groups are
removed; a group whose `hooks` is not a list is left as it is. Hooks and
settings that are not Alice's keep their values. Install rewrites the
whole file as two-space-indented JSON, after saving a timestamped backup
into `<data dir>/backups/host-configs/`
(`claude-code-settings.json.alice-backup-<UTC time>`), a 0700 directory,
and does not touch the file when its parsed JSON would not change. If
`settings.json` is a symbolic link, install edits the file the link
points to and keeps the link; the backup still goes to the data dir,
not next to the target. `alice-memory install --dry-run` prints the
Alice entry and the hook it would write, with every value from your
entry except `command`, `type`, `timeout` and `cwd` hidden, and changes
nothing.

If the `alice` entry in `~/.claude.json` is not one install wrote (the
Postgres entry in `docs/integrations/mcp.md`, for example), install
leaves that file alone and exits 1. It still repairs the shape of an
existing Alice hook, keeping that hook's own command and `--data-dir`,
and it does not add a hook where there is none. For an entry that opens
`--db`, the hook runs the entry's launcher but keeps its own
`--data-dir`, and no hook is added where there is none.

## OpenClaw

OpenClaw does not need a plugin. Either:

- run `alice-memory brief --data-dir "$ALICE_MEMORY_DATA_DIR"` and use
  the markdown on stdout, or
- run `alice-memory-session-start --format markdown` if the host wants
  the same fail-open wrapper without JSON.

## Check

```bash
alice-memory brief --data-dir /tmp/alice-brief-check
```

An empty directory prints one quiet line and exits 0.
