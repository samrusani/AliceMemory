# Hermes captures

Notice, 2026-10-04. These four files are a record. They do not show the current Hermes setup.

They were added on 2026-04-09 and record the first run against a real Hermes install. The `.png` files render the text of the `.txt` files. They are kept unchanged.

| File | What it shows |
|---|---|
| `hermes-mcp-test.txt`, `hermes-mcp-test.png` | Hermes testing a server named `alice_core`, which reports nine tools |
| `hermes-runtime-smoke.txt`, `hermes-runtime-smoke.png` | the smoke output, with three registered tools named `mcp_alice_core_alice_...` |

What has changed since:

- The nine tools were `alice_capture`, `alice_recall`, `alice_resume`, `alice_open_loops`, `alice_recent_decisions`, `alice_recent_changes`, `alice_memory_review`, `alice_memory_correct` and `alice_context_pack`. That list is retired. The server now advertises three tools by default (`alice_memory_commit`, `alice_recall`, `alice_resume`), and `ALICE_MCP_FULL_TOOLS=1` advertises eleven core tools. `alice_recent_changes` is no longer one of them; it is a legacy tool.
- `alice-memory install --host hermes` writes the server entry `alice`, not `alice_core`. `alice_core` is still the name in the manual full-stack examples.

For the current setup, see [Hermes Reference Integration](../../hermes.md).
