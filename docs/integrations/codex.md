# Codex

Unreleased (on main, not in v0.18.0): Codex support is on main and is not in the v0.18.0 release. v0.18.0 does not accept `--host codex`.

Codex is opt-in. `alice-memory install` does not write it unless you pass `--host codex`. The default hosts stay Claude Desktop, Claude Code, Cursor, and OpenClaw. It writes the MCP entry in `config.toml` and a SessionStart hook in `hooks.json`. It never writes `trusted_hash`, so Codex skips the hook until you trust it (see below).

Install reads `CODEX_HOME` only when `--home` is omitted. An empty value counts as unset. A value that is not absolute is refused. The next line says to set `CODEX_HOME` to an absolute path, or pass `--home`, then run install again. A missing path, or a path that is not a directory, is refused with Codex's own message. The directory is canonicalized. With `--home`, the file is `<home>/.codex/config.toml`. If `--home` is passed and `CODEX_HOME` points elsewhere, the receipt says so. On Windows the default directory is `%USERPROFILE%\.codex`.

## What gets written

The file is `config.toml`, edited as text. A new file holds only Alice's table. Install touches only Alice's `command`, `args`, and the carried lines below. Every other byte stays, including `[mcp_servers.alice.tools]` and `[mcp_servers.alice.tools.<tool>]` tables Codex writes when you remember an approval. The receipt says `format: toml, edited as text`. A success receipt ends with `next: check it with: codex mcp get alice`.

Install checks Alice's entry and the number ranges below. It does not check every other Codex rule for the rest of the file. An integer token in value position outside `-2^63` to `2^63-1` (decimal, hex, octal, or binary, underscores allowed) is refused, and the reason names the line. A float token in value position that is not finite is refused unless it is literally `inf` or `nan`. A number-like bare key is left alone. `tools` must be a table whose values are tables. `approval_mode` must be `auto`, `prompt`, `writes`, or `approve`. `output_token_limit` must be a positive integer. A value that fails one of those checks is refused and the file is not written. A `config.toml` nested so deeply that install cannot parse it is refused too, with `config.toml nests too deeply`, and the file is not written. A `hooks.json` nested that deeply is refused too, and nothing is written.

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

## SessionStart hook

Unreleased (on main, not in v0.18.0): `--host codex` also writes a SessionStart hook, so a new Codex session starts with the session brief in front of the model.

The hook goes in `<CODEX_HOME>/hooks.json`, not in `config.toml`. Install appends one group at the end of `hooks.SessionStart` and never inserts it ahead of yours. Codex keys its trust records by group and handler index, so inserting ahead of your groups would shift them and Codex would silently stop running hooks you had trusted. The group has no `matcher`, so it runs on every session source (startup, resume, clear, compact and fork). Its one handler is:

```json
{
  "type": "command",
  "command": "uvx --from alice-memory alice-memory-session-start --data-dir <data dir> --format markdown",
  "timeout": 120,
  "additionalContextLimit": 0
}
```

- `--format markdown`: Codex reads plain text as context. It rejects the JSON Cursor and Claude Code read, because a top-level `additional_context` fails its output check, and nothing is injected.
- `timeout` is 120 seconds, enough for a cold `uvx` resolve at the first turn. Codex's default is 600 seconds.
- `additionalContextLimit` is `0`, which tells Codex never to spill the brief to a file. Alice's own budget already bounds the brief.
- The command is quoted for the OS install runs on. On Windows Codex runs it in PowerShell, so the first word is bare and a value with a space is double-quoted. A data dir with `'` or `$` in it is refused.

A re-run replaces Alice's handler in place, at the same group and handler index, and leaves your groups alone. That includes the JSON-mode `alice-memory-session-start` item Codex's Claude Code import copies into an empty `hooks.json`: install replaces it, so there is one hook, and it prints markdown. Install replaces the whole handler, so an `async` or `commandWindows` key on it does not survive, and a handler with the right command but a missing `additionalContextLimit` or a different `timeout` is replaced too. A hooks file with more than one Alice hook is refused.

Without `--data-dir`, a new MCP entry opens the data dir of an Alice hook that is already installed (the one in `hooks.json`, or the one in `config.toml` when it holds TOML hooks), so the entry and the hook stay on the same vault. `--data-dir` moves both. A hook whose `--data-dir` is relative, or read by the shell, is not used for this.

**Trust.** Install never writes `trusted_hash` or `hooks.state`. Codex skips a hook it has not been told to trust, with no message outside its TUI, and a hook whose command changed is skipped until you trust it again. That review is there so no program can quietly add a command that runs on every session. After install, open Codex. At "Hooks need review", choose Review hooks and trust the `alice-memory-session-start` hook, or use `/hooks`. The receipt says `session_start: written to hooks.json, not trusted yet` and prints that as a `next:` line. When a re-run changes the command (a new `--data-dir`, say) it adds `The hook changed, so Codex will skip it until you trust it again.`, but only once `hooks.json` has been written; a run that fails first says nothing of the kind. A re-run that changes nothing says `session_start: unchanged in hooks.json (Codex runs it only if you have trusted it)`.

**Refused.** Codex skips a whole `hooks.json` on any type error, your own groups included. Install checks every handler against Codex's types before it edits the file: the top level holds only `description` and `hooks`, each event is a list of objects, each group's `hooks` is a list, each handler's `type` is `command`, `mcp_tool`, `prompt` or `agent` with that type's required fields (a `command` handler has a `command` string), `timeout` and `additionalContextLimit` are whole numbers of at least 0, `async` is true or false, and `matcher` is a string. A file that fails a check is refused with `Codex would skip this hooks.json`, and nothing is written, `config.toml` included. The reason names the place, never a value from the file. The file must be strict JSON: UTF-8, no BOM, no duplicate key, no NaN, no lone surrogate escape such as `\ud800` (Codex's JSON reader rejects it). An empty file counts as new.

**TOML hooks.** If `config.toml` already holds hooks (a non-empty array under any of `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop` or `Interrupt` in `[hooks]`), install writes no hook, because Codex would load both files and run the brief twice. It still writes the MCP entry, prints the hook as `[[hooks.SessionStart]]` TOML, and exits 1 with a `next:` line. Add the hook to `config.toml` yourself, then trust it as above. Until it is there, every run prints it again and exits 1. Once the printed hook is in `config.toml` (the same command, `timeout` and `additionalContextLimit`), the next run says `session_start: unchanged in config.toml (Codex runs it only if you have trusted it)` and exits 0. If `config.toml` holds an Alice hook that is not the one install would print, for a different data dir, format, launcher, `timeout` or limit, install does not edit it: the refusal tells you to replace that hook with the printed one, so Codex does not run the brief twice. `[hooks.state]`, an empty `SessionStart = []`, and any other key under `[hooks]` do not count.

If `[features] hooks = false` is set, Codex runs no hook at all, and the receipt says so, in a dry run too.

**Kept commands.** The launcher, URL and index rules for hooks are the ones the other hosts use. When install must keep an existing hook command as it is, that command still has to print markdown. A kept command that does not is refused, and the receipt prints the argv to add.

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
