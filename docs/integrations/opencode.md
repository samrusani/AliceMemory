# OpenCode

OpenCode support starts in v0.18.0. alice-memory 0.17.0 does not accept
`--host opencode`; with 0.17.0, add the entry under "What gets written" by hand.

OpenCode is opt-in. `alice-memory install` does not write it unless you pass
`--host opencode`. The default hosts stay Claude Desktop, Claude Code, Cursor,
and OpenClaw. There is no SessionStart hook. OpenCode has no hook block in
v1.18.32 (`experimental.hook` was removed in v1.1.46).

Install reads `$XDG_CONFIG_HOME` only when `--home` is omitted. An empty value
counts as unset. A value that is not absolute is refused. The global directory
is `<config home>/opencode`. On Windows that is `%USERPROFILE%\.config\opencode`.

## What gets written

The target is the file that already has `mcp.alice`. When alice sits in `opencode.json` and an `opencode.jsonc` also exists, install targets `opencode.json`. Otherwise it is `opencode.jsonc` when that file exists, and otherwise `opencode.json`. A 0-byte `.jsonc` is skipped. A new
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
From v0.19.0, booleans and numbers are shown on top-level keys, including
`"enabled": false`. This is the rule for every JSON host, not only OpenCode.
A value under `environment`, `env`, `headers`, or any other map stays hidden,
whatever its type. In v0.18.0 a dry run showed only `command`, `type`,
`timeout`, and `cwd` at the top level, and every other value,
`"enabled": false` included, printed as `<hidden>`.

## JSONC

`opencode.jsonc` is always edited as text. `opencode.json` is edited as text
when it is not strict JSON (a comment, a trailing comma, a duplicate key,
or `1e999`). `NaN` and a BOM are refused. The receipt says `format: jsonc, edited as text`.
Install replaces only the `mcp.alice` value, or inserts `alice` or `mcp`
first, and adds a comma only before a sibling. Newlines and indent follow
the file. A file whose only line break is CR keeps CR. Before it writes,
install parses the edited text. `mcp.alice` has to equal the planned entry,
and every other value has to be unchanged. Otherwise install refuses and
writes nothing. Every rewrite is backed up first.

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

From v0.20.0, `ALICE_EMBEDDINGS_MAX_INPUT_CHARS`, the
most characters of one memory's text sent to the embeddings endpoint, is also
a documented host env key and is carried on the same terms as the seven above.
In v0.19.2 that name is not carried, so an entry that holds it is refused on
this path. A strict `opencode.json` kept every key that install did not write in
both versions.

## What is refused

- `alice` more than once, under `mcp` or `mcp.servers`, including
  `config.json`, `opencode.jsonc`, and `~/.opencode`. A refusal raised while
  scanning a text file names that file, says `format: jsonc, edited as text`,
  and uses the placeholder. A strict JSON refusal for those two duplicates
  also uses the placeholder when an `opencode.jsonc` is present. In v0.17.0
  there is no OpenCode install, so this refusal does not exist in the release.
- A legacy `<config home>/opencode/config` file.
- JSONC the text path cannot scan (a BOM, a token error, whitespace-only or
  comment-only text, a non-object top level or `mcp`, a duplicate `mcp` or
  `alice`, or nesting deeper than 64). The snippet uses a placeholder data
  dir when the scan stops before `alice`. When the entry is found but cannot
  be edited, the snippet uses that entry's `--data-dir`, or the same
  placeholder when the entry does not show one. A `--data-dir` value that
  holds `{env:` or `{file:` uses that placeholder. A scan refusal is a fixed
  label, with a line number when it comes from one file. A reason from the
  shared launcher planner still carries that planner's text.
- An OpenCode directory install cannot stat. The receipt names that directory.
  That host fails and the other hosts in the same run still get a receipt.

Check a file install did write with `opencode debug config` and
`opencode mcp list`.
