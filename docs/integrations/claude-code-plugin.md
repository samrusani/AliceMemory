# Claude Code plugin

The plugin directory ships in v0.19.0. v0.18.0 has no Claude Code plugin and no marketplace file. The v0.19.0 tag has no marketplace file either. `main` has one, and the plugin installs from it as the `alicememory` marketplace.

The plugin is `plugins/alice-memory`. `.claude-plugin/marketplace.json` on `main` lists it with a `git-subdir` source pinned to the v0.19.0 tag and to that tag's commit. A later change to the plugin directory on `main` is not installed until a change moves the pin to a later tag.

Tool names look like `mcp__plugin_alice-memory_alice__<tool>`.

## Install

The v0.19.0 tag has no marketplace file, so a checkout of the tag has nothing to add. `main` has `.claude-plugin/marketplace.json`, with the name `alicememory` and one plugin entry pinned to the v0.19.0 tag commit. Clone this repository, which checks out `main`, and run:

```bash
claude plugin marketplace add <path to the clone>
claude plugin install alice-memory@alicememory
```

`claude plugin install alice-memory@alicememory --config data_dir=<absolute path>` sets the option at install. Real host CI runs these commands against Claude Code 2.1.281, with a marketplace file in a temporary directory. The dispatch-only marketplace check runs them against the committed file.

## Data directory

The data directory is the plugin option `data_dir`, with the default `~/.alice`. The server gets it as `--data-dir` in `.mcp.json`. The hook gets it another way.

The SessionStart hook reads the folder from the plugin option, through the `CLAUDE_PLUGIN_OPTION_DATA_DIR` variable Claude Code sets for the hook, and uses `~/.alice` when the option is unset. That is the folder the server uses, so the hook and the server open one vault. The hook's command is exactly `uvx --from alice-memory==<version> alice-memory-session-start`, with no `--data-dir`. Claude Code does not run a hook whose arguments reference a plugin option that is not set. A hook with `--data-dir ${user_config.data_dir}` did not run until the option was set, which is why the hook reads the variable itself.

Claude Code sets `CLAUDE_PLUGIN_ROOT` for a plugin's hooks and servers and not for a hook in `settings.json`. When it is set and non-empty and no `--data-dir` is given, `alice-memory-session-start` ignores `ALICE_MEMORY_DATA_DIR`. An explicit `--data-dir` still wins, and outside the plugin nothing changes. A relative option value makes the hook print one line asking for an absolute path, and it opens nothing.

Use the plugin or `alice-memory install --host claude-code`, not both. Install skips when `enabledPlugins["alice-memory@alicememory"]` in `~/.claude/settings.json` is true and install has not written its own Claude Code entries. If those entries exist, install refuses. If the key is false, install writes and notes that enabling the plugin later sets Alice up twice.

Project and local plugin scopes are invisible to install.
