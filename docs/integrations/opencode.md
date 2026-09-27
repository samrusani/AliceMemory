# OpenCode

OpenCode is opt-in. `alice-memory install` does not write it unless you pass
`--host opencode`. The default hosts stay Claude Desktop, Claude Code, Cursor,
and OpenClaw. There is no SessionStart hook. OpenCode has no hook block in
v1.18.32 (`experimental.hook` was removed in v1.1.46).

Install reads `$XDG_CONFIG_HOME` only when `--home` is omitted. An empty value
counts as unset. A value that is not absolute is refused. The global directory
is `<config home>/opencode`. On Windows that is `%USERPROFILE%\.config\opencode`.

## What gets written

The target is the strict JSON file that already has `mcp.alice`. Otherwise it
is `opencode.json`. A new file starts with `$schema` set to
`https://opencode.ai/config.json`. The entry is:

```json
{
  "type": "local",
  "command": ["uvx", "alice-memory", "mcp", "--data-dir", "<data dir>"]
}
```

Install does not write an `environment` key. OpenCode passes its own
environment through, and the server reads `--data-dir`.

A re-run of strict JSON keeps every key it did not write. On `mcp.alice` that
includes `timeout`, `enabled`, `cwd`, and `environment` (for example
`environment.FOO`). Sibling servers under `mcp` stay. A dry run prints only
the Alice entry and masks the `command` array: a URL after its scheme, and the
value of a flag whose name holds key, token, secret, or password.

## What is refused

- Any non-empty `opencode.jsonc`, global or under `~/.opencode`. The receipt
  shows the masked entry to add by hand, then `opencode mcp list`.
- `alice` more than once, under `mcp` or `mcp.servers`, including `config.json`
  and `~/.opencode`.
- A legacy `<config home>/opencode/config` file.
- A `.json` file that is not strict JSON (a BOM, a comment, a trailing comma,
  a duplicate key, `NaN`, or `1e999`).

Nothing is written in those cases. Check a file install did write with
`opencode debug config` and `opencode mcp list`.
