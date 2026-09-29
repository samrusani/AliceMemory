# Claude Code plugin

Unreleased (on main, not in v0.18.0): the plugin directory is on main and is not in the v0.18.0 release. v0.18.0 has no Claude Code plugin and no marketplace file.

The plugin is `plugins/alice-memory`. It is not published from `main`. A later post-publication change adds `.claude-plugin/marketplace.json` with a `git-subdir` source pinned to a release tag.

Use the plugin or `alice-memory install --host claude-code`, not both. Install skips when `enabledPlugins["alice-memory@alicememory"]` in `~/.claude/settings.json` is true and install has not written its own Claude Code entries. If those entries exist, install refuses. If the key is false, install writes and notes that enabling the plugin later sets Alice up twice.

Project and local plugin scopes are invisible to install.
