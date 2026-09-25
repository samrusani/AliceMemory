# Sprint 6: host adapters

Design only. No behavior change ships with this note.

The inventory of hosts we already install was read from `host_file_map` in
`apps/api/src/alicebot_api/host_install.py` on `main` at
`e8abf466f3b2727d1ccfe60d01aff622eb301758`. That map has Claude Desktop,
Claude Code, Cursor, OpenClaw, and Hermes. Sprint 6 adds four more, in
this order: OpenCode, Codex, a Claude Code plugin manifest, then Pi.
Kilo stays deferred.

## What each adapter must do

Every adapter is an install and a re-run. It follows the Sprint 4 install
rules already used by the hosts we ship:

- Keep keys the user wrote. A re-run replaces only the block Alice wrote.
  Documented env keys stay, byte for byte, the way Hermes keeps its
  documented env block. A key Alice did not write still refuses the file.
- One launcher. The command is the same `uvx` launcher the other hosts
  use. When `uv` is installed and `uvx` is not on `PATH`, write an absolute
  `uvx` path. Do not write a versioned Homebrew Cellar path.
- No URL in a hook. A hook command is a local launcher line. It does not
  contain an HTTP URL.
- Per-host refusals. A file that is not the shape that host documents is
  refused with that host's message, and the other hosts are not touched.
- A backup before the first rewrite of a file the user already has. A
  second re-run that changes nothing does not write another backup.

The config path for each new host is taken from that host's own docs and
pinned in a test with the host version. This note does not guess the path.

## OpenCode

First, because that is the user order in the queue. Install writes the MCP
server entry and, if OpenCode has a session-start hook, the same
SessionStart command Claude Code and Cursor already get. It does not add
an end hook. A host without an end event keeps using `alice-memory sleep`.

## Codex

Codex has no end hook. Install writes the MCP server entry only. Session
end stays `alice-memory sleep`. Do not invent a Codex end hook.

## Claude Code plugin manifest

This is a manifest for the Claude Code plugin surface, not a second
installer. It advertises the same three default tools: `alice_memory_commit`,
`alice_recall`, and `alice_resume`. It does not register `SessionEnd` or
`Stop`. The existing installer keeps writing only `hooks.SessionStart`.

## Pi

Last of the four. Same install rules. No end hook unless Pi's docs name
one, and even then this sprint does not point it at the sleep writer.

## What this design does not do

- It does not add Kilo.
- It does not register Claude Code `SessionEnd` or `Stop`.
- It does not add a fourth default MCP tool.
- It does not point any host's session end at the sleep writer.
- It does not change the credential floor or the commit door.

## Real-host check

Each adapter gets one validation run we can repeat. On the Linux runner,
with a temp home and no credentials:

- install the host's pinned build;
- run `alice-memory install` for that host;
- run it again;
- record whether the second run kept the user's keys, wrote one launcher,
  left the backup in place, and did not put a URL in a hook.

The same constraints as the SessionEnd trial apply. Do not run this on a
developer Mac, and do not use a real home directory. If the host will not
start without credentials, say so and stop. Do not add a secret to CI for
this design.

## Acceptance

- One install test and one re-run test per host, including a user key that
  must survive the re-run.
- A mutation that writes a second launcher, or a URL into a hook, fails
  that host's test.
- The real-host run above is recorded in the PR for that host, with the
  host version. A missing run is named, not treated as a pass.
