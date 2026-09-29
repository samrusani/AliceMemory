# Codex

Unreleased (on main, not in v0.18.0): Codex support is on main and is not in the v0.18.0 release. v0.18.0 does not accept `--host codex`.

Codex is opt-in. `alice-memory install` does not write it unless you pass `--host codex`. The default hosts stay Claude Desktop, Claude Code, Cursor, and OpenClaw. This install writes the MCP entry only. It does not write a SessionStart hook, and it never writes `trusted_hash`.

Install reads `CODEX_HOME` only when `--home` is omitted. An empty value counts as unset. A value that is not absolute is refused. A missing path, or a path that is not a directory, is refused with Codex's own message. The directory is canonicalized. With `--home`, the file is `<home>/.codex/config.toml`. On Windows the default directory is `%USERPROFILE%\.codex`.

## What gets written

The file is `config.toml`, edited as text. A new file holds only Alice's table. Install touches only Alice's `command`, `args`, and the carried lines below. Every other byte stays, including `[mcp_servers.alice.tools.<tool>]` tables Codex writes when you remember an approval. The receipt says `format: toml, edited as text` and `session_start: none`.

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

Codex merges config layers. A profile (`<CODEX_HOME>/<name>.config.toml`), the system config (`/etc/codex/config.toml`, or `%ProgramData%\OpenAI\Codex\config.toml` on Windows), or a managed layer (`/etc/codex/managed_config.toml`) can also define `mcp_servers.alice`. Requirements can disable a server. Install edits only the user `config.toml` and prints a note when another layer defines alice. An unreadable layer file is a note too.

## What is carried

A re-run keeps these keys when they already sit on Alice's table and Codex would load them:

- `startup_timeout_sec` and `tool_timeout_sec`: a finite number, zero or greater, below 2^63
- `startup_timeout_ms`: an integer from 0 to 2^63-1
- `enabled`: a bool. `enabled = false` is kept, and the receipt says Codex will not start alice
- `default_tools_approval_mode`: `auto`, `prompt`, `writes`, or `approve`
- `env_vars`: an array of strings, on one line or several
- documented `ALICE_*` env values, and `ALICE_MEMORY_DATA_DIR`, copied byte for byte

A `tools...` key you wrote inside the alice table is kept too. Install does not edit, move, or remove a `tools` table.

Anything else in the alice table, including `cwd`, `url`, and `enabled_tools`, is refused. The file is not changed. The receipt says to keep editing that key by hand, or remove it and run install again.

## Check

Check the entry in text mode, which masks env values:

```bash
codex mcp get alice
```

`--json` prints env values in clear. Use it only when you mean to.
