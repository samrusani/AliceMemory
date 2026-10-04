# Alice Memory for Claude Code

This plugin directory ships in v0.19.0. v0.18.0 has no Claude Code plugin.

It installs from the `alicememory` marketplace, which is `.claude-plugin/marketplace.json` at the root of this repository on `main`. The file pins this plugin to the v0.20.0 tag commit. Add the marketplace from `main`, not from a checkout of a tag: a tag's file is missing or pins an earlier release, and the [Claude Code plugin guide](https://github.com/samrusani/AliceMemory/blob/main/docs/integrations/claude-code-plugin.md) lists what each tag pins. Adding the repository as `samrusani/AliceMemory` reads its default branch, which is `main`, so run:

```bash
claude plugin marketplace add samrusani/AliceMemory
claude plugin install alice-memory@alicememory
```

If git on your machine is set to use SSH for GitHub and you have no key there, add `https://github.com/samrusani/AliceMemory.git` instead. If you already have a clone, `claude plugin marketplace add <path to the clone>` works too.

The plugin needs [uv](https://docs.astral.sh/uv/) on `PATH`. Its commands pin `alice-memory` to the version in `plugin.json`.

Use the plugin or `alice-memory install --host claude-code`, not both. To remove install's entries, run `claude mcp remove alice --scope user` and remove the `alice-memory-session-start` hook from `~/.claude/settings.json`.

The data directory is the `data_dir` option. The default is `~/.alice`. Use an absolute path or `~/...`, and use the same folder as your other hosts.

The SessionStart hook reads the folder from that option, through the `CLAUDE_PLUGIN_OPTION_DATA_DIR` variable Claude Code sets for it, and uses `~/.alice` when the option is unset. That is the folder the server uses, so the two open one vault. The hook's command has no `--data-dir`, because Claude Code does not run a hook whose arguments reference an option that is not set. In the plugin the hook ignores `ALICE_MEMORY_DATA_DIR`. A relative option value makes the hook print one line asking for an absolute path, and it opens nothing.

Tool names look like `mcp__plugin_alice-memory_alice__alice_recall`.

Auto-update is off for a third-party marketplace. After a release is published, refresh the `alicememory` marketplace so it carries the new pin, then run `claude plugin update alice-memory@alicememory`. If you added a clone, pull it first. `claude plugin marketplace --help` lists the marketplace commands.

Claude Code caps a hook's `additionalContext` or plain stdout at 10,000 characters. Over that it injects a file path and a 2,000-character preview.
