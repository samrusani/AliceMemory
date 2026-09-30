# Alice Memory for Claude Code

Unreleased (on main, not in v0.18.0): this plugin directory is on main and is not in the v0.18.0 release. v0.18.0 has no Claude Code plugin.

The plugin needs [uv](https://docs.astral.sh/uv/) on `PATH`. Its commands pin `alice-memory` to the version in `plugin.json`.

Use the plugin or `alice-memory install --host claude-code`, not both. To remove install's entries, run `claude mcp remove alice --scope user` and remove the `alice-memory-session-start` hook from `~/.claude/settings.json`.

The data directory is the `data_dir` option. The default is `~/.alice`. Use an absolute path or `~/...`, and use the same folder as your other hosts.

Unreleased (on main, not in v0.18.0): the SessionStart hook reads the folder from that option, through the `CLAUDE_PLUGIN_OPTION_DATA_DIR` variable Claude Code sets for it, and uses `~/.alice` when the option is unset. That is the folder the server uses, so the two open one vault. The hook's command has no `--data-dir`, because Claude Code does not run a hook whose arguments reference an option that is not set. In the plugin the hook ignores `ALICE_MEMORY_DATA_DIR`. A relative option value makes the hook print one line asking for an absolute path, and it opens nothing.

Tool names look like `mcp__plugin_alice-memory_alice__alice_recall`.

Auto-update is off for a third-party marketplace. After a release is published, update with `claude plugin update alice-memory@alicememory`.

Claude Code caps a hook's `additionalContext` or plain stdout at 10,000 characters. Over that it injects a file path and a 2,000-character preview.
