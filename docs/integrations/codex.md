# Codex

Unreleased (on main, not in v0.18.0): Codex support is on main and is not in the v0.18.0 release. v0.18.0 does not accept `--host codex`.

Codex is opt-in. `alice-memory install` does not write it unless you pass `--host codex`. The default hosts stay Claude Desktop, Claude Code, Cursor, and OpenClaw. This install writes the MCP entry only. It does not write a SessionStart hook, and it never writes `trusted_hash`.

Install reads `CODEX_HOME` only when `--home` is omitted. An empty value counts as unset. A value that is not absolute is refused. The next line says to set `CODEX_HOME` to an absolute path, or pass `--home`, then run install again. A missing path, or a path that is not a directory, is refused with Codex's own message. The directory is canonicalized. With `--home`, the file is `<home>/.codex/config.toml`. If `--home` is passed and `CODEX_HOME` points elsewhere, the receipt says so. On Windows the default directory is `%USERPROFILE%\.codex`.

## What gets written

The file is `config.toml`, edited as text. A new file holds only Alice's table. Install touches only Alice's `command`, `args`, and the carried lines below. Every other byte stays, including `[mcp_servers.alice.tools]` and `[mcp_servers.alice.tools.<tool>]` tables Codex writes when you remember an approval. The receipt says `format: toml, edited as text` and `session_start: none`. A success receipt ends with `next: check it with: codex mcp get alice`.

Install checks Alice's entry and the number ranges below. It does not check every other Codex rule for the rest of the file. An integer token in value position outside `-2^63` to `2^63-1` (decimal, hex, octal, or binary, underscores allowed) is refused, and the reason names the line. A float token in value position that is not finite is refused unless it is literally `inf` or `nan`. A number-like bare key is left alone. `tools` must be a table whose values are tables. `approval_mode` must be `auto`, `prompt`, `writes`, or `approve`. `output_token_limit` must be a positive integer. A value that fails one of those checks is refused and the file is not written. A `config.toml` nested so deeply that install cannot parse it is refused too, with `config.toml nests too deeply`, and the file is not written.

A rewrite sets the file mode to `0600`. The same writer does this for every host, because these files can hold keys. A new file is `0600` as well.

```toml
[mcp_servers.alice]
command = "uvx"
args = ["alice-memory", "mcp", "--data-dir", "<data dir>"]
```

Install does not write an `env` table. `--data-dir` is in `args`. The server reads `--data-dir`.

Codex passes a stdio server only `HOME`, `PATH`, `LANG`, and a few other names. If you are behind a proxy, or you followed install's advice for `UV_INDEX_*` credentials, add `env_vars` yourself, as a list of names:

```toml
env_vars = ["HTTPS_PROXY", "UV_INDEX_PRIVATE_USERNAME"]
```

Codex merges config layers. A profile (`<CODEX_HOME>/<name>.config.toml`), the system config (`/etc/codex/config.toml`, or `%ProgramData%\OpenAI\Codex\config.toml` on Windows), or a managed layer (`/etc/codex/managed_config.toml`) can also define `mcp_servers.alice`. Requirements can disable a server. Install edits only the user `config.toml` and prints a note with the full path when another layer defines alice, and says Codex merges it. An unreadable layer file is a note with the full path too. A layer that is not UTF-8, or is nested so deeply that install cannot parse it, counts as unreadable.

## What is carried

A re-run keeps these keys when they already sit on Alice's table and Codex would load them:

- `startup_timeout_sec` and `tool_timeout_sec`: a finite number, zero or greater, below 2^63
- `startup_timeout_ms`: an integer from 0 to 2^63-1
- `enabled`: a bool. `enabled = false` is kept, and the receipt says Codex will not start alice
- `default_tools_approval_mode`: `auto`, `prompt`, `writes`, or `approve`
- `env_vars`: an array of strings, on one line or several
- documented `ALICE_*` env values, and `ALICE_MEMORY_DATA_DIR`, copied byte for byte

A `tools...` key you wrote inside the alice table is kept too, when there is no `[mcp_servers.alice.tools...]` table. If both are present, in either order, install refuses and asks you to move each `tools.<name>` key into its own `[mcp_servers.alice.tools.<name>]` table. Install does not edit, move, or remove a `tools` table, including a bare `[mcp_servers.alice.tools]` table.

A comment inside `command`, `args`, or an inline `env` is refused. The comment stays in the file. Comments inside a carried value such as `env_vars` stay.

Anything else in the alice table, including `cwd`, `url`, and `enabled_tools`, is refused. The file is not changed. The receipt says to keep editing that key by hand, or remove it and run install again. That remove line is only for those keys. A comment, an unquoted env value, or a tools key that belongs in its own table gets a line that names that fix instead.

When the refusal is on an alice entry install already found and the file parses, the snippet shows that entry's own `command` and `args`, with secret-looking values shown as `<hidden>`. That needs a string `command` and an `args` list of strings. If either is missing or has another shape, or the file does not parse, the snippet uses install's own launcher instead. It keeps the entry's data dir when install can read it. If install cannot read the dir, the snippet uses a placeholder, not `~/.alice`. The next line says to edit the alice entry by hand, then check it with `codex mcp get alice`.

## Check

Check the entry in text mode, which masks env values:

```bash
codex mcp get alice
```

`--json` prints env values in clear. Use it only when you mean to.
