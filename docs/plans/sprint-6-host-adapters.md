# Sprint 6: host adapters

Design only. No installer change ships with this note.

The inventory of hosts we already install was read from `host_file_map` and
`INSTALL_HOSTS` in `apps/api/src/alicebot_api/host_install.py`. That set is
Claude Desktop, Claude Code, Cursor, OpenClaw, and Hermes. Hermes is opt-in
through `--host`. The other four are `DEFAULT_INSTALL_HOSTS`.

Sprint 6 adds OpenCode and Codex, and a Claude Code plugin manifest that is
an alternative to `alice-memory install`. Pi is deferred, like Kilo, until
Pi supports MCP or Sprint 6 has landed. Pi 0.87.1 says "No MCP", so install
has nothing to write. An Alice-owned TypeScript extension, the third-party
`pi-mcp-adapter`, or a skill that calls the CLI would each be a new
integration surface with no validation command. The third-party adapter
would add a dependency outside our control to the install path. This note
records that and does not add Pi.

## How a writer treats keys it did not write

The writer will match the file format. It will not follow one rule for every
host.

Plain JSON, which install can read and rewrite exactly, keeps every key the
user added. The JSON hosts already do this. A re-run over an install-shaped
entry with an added `env` key, `timeout`, and another key reports
`action: unchanged` and keeps all three. Only the Hermes writer refuses a
key Alice did not write.

Files install edits as text will use the Hermes model. That is TOML for Codex,
and JSONC for OpenCode when the file has comments. The writer will edit only
Alice's entry in place, carry the documented `ALICE_*` env keys byte for
byte, and refuse anything else with a snippet and a correct `next:` line.
It must not strip comments, and it must not rewrite a whole file that
install cannot fully parse. There is no TOML writer in the repo and
`tomllib` only reads. Codex will need a text-edit writer like Hermes's, or
a refusal.

OpenCode's entry is `mcp.<name> = {type: "local", command: [array],
environment: {}}`. The key is `environment`, not `env`, and there is no
separate `args`. `parse_launcher` today requires `command` to be a string
and `args` to be a list of strings. It will have to learn the array form.
`_masked` copies `command` as shown, and `masked_args` runs only on `args`.
With OpenCode the arguments live in the command array, so the writer will
apply that masking to the array.

## Rules the shipped installer already follows

`find_launcher` tries `uvx` on PATH, then installed `alice-memory` scripts
outside a uv cache, then an absolute `uvx`. A launcher is replaced only when
`launcher_problem` says it is dead, unless it is pinned or customised,
except a launcher inside a uv cache, which is always replaced.

A hook command blocks any `scheme://` and any index option, even one with a
local path. Only the allowlisted uvx options are carried into a hook
(`CARRY_OPTIONS_WITH_VALUE` and `CARRY_FLAGS` in `host_launcher.py`).

`--dry-run` prints only Alice's entry, with copied values masked.

A backup is written before every rewrite of an existing file, not only the
first, into `<data dir>/backups/host-configs/`.

The liveness check stays: a dead launcher is replaced, a pinned or
customised one is kept.

## OpenCode

Sprint 6 will ship OpenCode as MCP only, with no session start. OpenCode
1.18.32 has no hook in its config file (the `experimental.hook` block was
removed in v1.1.46). Its session route is a TypeScript plugin. This sprint
will not add that plugin. It would be code in a second language, with its
own release and trust surface, for one host. MCP-only matches the other
hosts without a start event, where the user runs `alice-memory brief`.

MCP server `instructions`, which OpenCode puts in the system prompt, are a
separate follow-up. Alice sends none today, and adding them affects every
host.

## Codex

Codex 0.157.0 has `SessionStart`, `Stop`, and `SessionEnd` hooks, and hooks
are on by default. Installer-written hooks run only after the user trusts
them. Sprint 6 will not register `SessionEnd` or `Stop`.

Codex will get its MCP entry in Sprint 6. Its SessionStart hook will come
in the same sprint only if a real-host check proves Codex accepts both the
hook config and our hook's output. Our current hook JSON carries a top-level
`additional_context` key, and a strict parse of Codex's output schema
rejects it. Codex will need `--format markdown` (plain stdout) or a Codex
output shape. Confirm that against real Codex, not a copied struct. Install
must print that the user has to trust the hook in Codex before it runs. If
no headless check can show Codex running the hook, the sprint will ship the
MCP entry alone and the docs will say why.

## Claude Code plugin

The plugin must be an alternative to `alice-memory install` for Claude Code,
never used alongside it. Running both would give two servers and two hooks.
Identical handlers are not merged.

It will carry the MCP server and the SessionStart hook. It will not
register `SessionEnd` or `Stop`. A plugin manifest cannot list tools. The
tools come from the server at runtime, named
`mcp__plugin_<plugin>_<server>__*`. Hook matchers and permission rules
written for `mcp__alice__*` do not match them. The docs will name the
`mcp__plugin_...` tool names.

The plugin's SessionStart hook must use exec form (a command plus args) so
`${user_config.data_dir}` is substituted, or the hook must read
`CLAUDE_PLUGIN_OPTION_DATA_DIR`. Claude Code rejects `${user_config.*}` in a
shell-form hook command. The adapter's validate step will reject a
shell-form SessionStart hook.

Install will detect an installed Alice plugin and skip Claude Code with a
message. The docs will say to use one or the other. The plugin will launch
`uvx` by name, because a static manifest cannot resolve an absolute path.
Its docs will say it needs uv. The data dir will come from `userConfig`,
following the existing MCPB `user_config.data_dir`.

## Opt-in

Each new host is opt-in through `--host`, as Hermes is, until it has a
passing real-host check. Moving one into `DEFAULT_INSTALL_HOSTS` is a
separate decision.

## Real-host check, required before an adapter merges

An adapter pull request merges only with a real-host CI check that meets
all of these:

1. The host reads the written config with its own tool: `opencode debug
   config` and, if it runs headless, `opencode mcp list`; `codex mcp list
   --json` or `codex mcp get` (parse the JSON; `codex doctor` exits 1
   without credentials); `claude plugin validate <dir> --strict --json` for
   the plugin (it checks `.mcp.json` from 2.1.281, the current pin).
2. A control case in a broken shape is rejected by the host. OpenCode and
   Codex ignore unknown keys, so the control has to be a type or transport
   error, not a stray key.
3. The run tests the install rules: seed an existing config, so a backup is
   written; add a user key between two runs, so keeping it is tested; check
   the dry-run masking.
4. It is wired into `real-host-ci.yml`, with the pinned versions and the
   `ran` count updated, and `test_real_host_ci_workflow.py` extended. Pi
   would need Node 22.19 or later; the job pins 22.14.0. If a host cannot
   run headless without credentials, that adapter does not merge until the
   owner decides on a CI secret.

The same constraints as the SessionEnd trial apply. Do not run this on a
developer Mac, and do not use a real home directory.

## Mutations each adapter pull request must include

- A second launcher.
- A shell-form SessionStart hook.
- A URL in a hook.
- A dropped user key.
- A skipped backup.
- An unmasked dry run.
- An uncarried uvx option.
- The host-side control accepted.

## What this design does not do

- It does not add Pi or Kilo.
- It does not register Claude Code `SessionEnd` or `Stop`, and it does not
  register Codex `SessionEnd` or `Stop`.
- It does not add a fourth default MCP tool.
- It does not point any host's session end at the sleep writer.
- It does not change the credential floor or the commit door.
- It does not ship installer code.
