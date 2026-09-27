# OpenCode

OpenCode is opt-in. `alice-memory install` does not write it unless you pass
`--host opencode`. The default hosts stay Claude Desktop, Claude Code, Cursor,
and OpenClaw. There is no SessionStart hook. OpenCode has no hook block in
v1.18.32 (`experimental.hook` was removed in v1.1.46).

Install reads `$XDG_CONFIG_HOME` only when `--home` is omitted. An empty value
counts as unset. A value that is not absolute is refused. The global directory
is `<config home>/opencode`. On Windows that is `%USERPROFILE%\.config\opencode`.

## What gets written

The target is the file that already has `mcp.alice`. Otherwise it is
`opencode.jsonc` when that file exists, and otherwise `opencode.json`. A new
file starts with `$schema` set to `https://opencode.ai/config.json`. The
entry is:

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

## JSONC

`opencode.jsonc` is always edited as text. `opencode.json` is edited as text
when it is not strict JSON (a comment, a trailing comma, a duplicate key,
`NaN`, `1e999`, or a BOM). The receipt says `format: jsonc, edited as text`.
Install replaces only the `mcp.alice` value, or inserts `alice` or `mcp`
first, and adds a comma only before a sibling. Newlines and indent follow
the file. Every rewrite is backed up first.

On that path an old `mcp.alice` may hold `type`, a `command` array, and
`environment` values whose names are the documented host env keys:

- `ALICE_MCP_FULL_TOOLS`
- `ALICE_MCP_LEGACY_TOOLS`
- `ALICE_AGENT_API_KEY`
- `ALICE_LEGACY_SURFACES`
- `ALICE_EMBEDDINGS_BASE_URL`
- `ALICE_EMBEDDINGS_MODEL`
- `ALICE_EMBEDDINGS_API_KEY`

Each carried value has to be a string literal. Install copies that literal
byte for byte. `timeout`, `enabled`, `cwd`, any other `environment` name, a
comment inside `alice`, or a command that is not an array is left unchanged.
The receipt names the line and does not print the file.

## What is refused

- `alice` more than once, under `mcp` or `mcp.servers`, including
  `config.json`, `opencode.jsonc`, and `~/.opencode`.
- A legacy `<config home>/opencode/config` file.
- JSONC the text path cannot scan (a BOM, a token error, whitespace-only or
  comment-only text, a non-object top level or `mcp`, a duplicate `mcp` or
  `alice`, or nesting deeper than 64). The snippet uses a placeholder data
  dir when the scan stops before `alice`.
- An OpenCode directory install cannot stat. That host fails and the other
  hosts in the same run still get a receipt.

Check a file install did write with `opencode debug config` and
`opencode mcp list`.
