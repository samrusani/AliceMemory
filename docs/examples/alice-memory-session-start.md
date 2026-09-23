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
`~/.claude/settings.json`. The hook's `--data-dir` is the one passed to
install; without the flag, install keeps the data dir of the existing
Alice MCP entry in `~/.claude.json`, then the one in an existing Alice
hook, else `~/.alice`. Install in v0.16.0 and earlier wrote the flat
Cursor item there, which Claude Code ignored. Re-running install from a
version with this fix replaces that item. Duplicate Alice entries in
well-formed groups are removed; a group whose `hooks` is not a list is
left as it is. Hooks and settings that are not Alice's keep their
values. Install rewrites the whole file as two-space-indented JSON, after
saving a timestamped backup next to it
(`settings.json.alice-backup-<UTC time>`), and does not touch the file
when nothing would change.

If the `alice` entry in `~/.claude.json` is not one install wrote (the
Postgres entry in `docs/integrations/mcp.md`, for example), install
leaves that file alone and exits 1. It still repairs the shape of an
existing Alice hook, keeping that hook's own `--data-dir`, and it does
not add a hook where there is none.

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
