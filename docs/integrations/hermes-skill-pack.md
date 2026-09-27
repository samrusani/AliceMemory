# Hermes Skill Pack for Alice Workflows

This pack provides Hermes-native skills that guide when and how to call Alice MCP tools.

This pack is legacy. The supported Hermes pack is
`agent-skills/hermes/alice-memory`. The skills here need the full tool
surface (`ALICE_MCP_FULL_TOOLS=1`).

## What This Pack Includes

Pack location in this repository:

- `docs/integrations/hermes-skill-pack/skills/alice-workflows/`

Skills:

- `alice-continuity-recall`
- `alice-resumption`
- `alice-open-loop-review`
- `alice-explain-provenance`
- `alice-correction-loop`

## Skill to Tool Map

| Skill | Primary Alice MCP tools |
|---|---|
| `alice-continuity-recall` | `alice_recall`, `alice_recent_decisions` |
| `alice-resumption` | `alice_resume`, `alice_context_pack` |
| `alice-open-loop-review` | `alice_open_loops`; optional `alice_recent_changes` is keyless-local legacy compatibility (`ALICE_MCP_LEGACY_TOOLS=1`) |
| `alice-explain-provenance` | `alice_context_pack`, `alice_recall` |
| `alice-correction-loop` | `alice_memory_review`, `alice_memory_correct`, `alice_recall`/`alice_resume` |

## Install

From the Alice repository root:

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills"
cp -R docs/integrations/hermes-skill-pack/skills/alice-workflows "$HERMES_HOME/skills/"
```

These skills need the full tool surface. Set `ALICE_MCP_FULL_TOOLS=1` in the
Alice server env. On the default three tools, only the `alice_recall` and
`alice_resume` steps work.

## Verify Installation

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  ./.venv/bin/hermes skills list --source local
```

You should see the five `alice-*` skills listed.

To confirm three core skills quickly:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}" \
  ./.venv/bin/hermes skills list --source local | \
  rg "alice-continuity-recall|alice-resumption|alice-open-loop-review"
```

## Skills vs Provider vs MCP: Responsibility Split

- Skills: decision policy and workflow instructions (when to call tools, how to format output, what evidence to include).
- External memory provider: always-on prefetch and memory-provider-native recall tools.
- MCP tools: runtime execution and deterministic continuity data retrieval/update.
- Practical rule: use skills to decide behavior; use provider plus MCP as the recommended execution shape.

See:

- `docs/integrations/hermes-bridge-operator-guide.md`
- `docs/integrations/hermes-memory-provider.md`
- `docs/integrations/hermes.md` (MCP-only fallback)

## When Hermes Should Prefer Alice Tools

Use Alice tools instead of inference-only answers when:

- the user asks for prior decisions, commitments, or timeline details
- the user asks to continue interrupted work with concrete next actions
- the user asks which blockers/open loops remain
- the user asks for provenance or evidence behind a claim
- the user asks to correct stale or incorrect continuity records

Concrete examples:

- "What did we decide about rollout gating last week?" -> prefer `alice_recall`
- "Resume thread `<uuid>` and give next action plus blockers." -> prefer `alice_resume`
- "What is still blocked for this project?" -> prefer `alice_open_loops`
- "Why do you think this is true?" -> prefer `alice_context_pack` + `alice_recall`
- "This memory is outdated, replace it." -> prefer `alice_memory_review` + `alice_memory_correct`

## Suggested Skill Loading

```bash
./.venv/bin/hermes -s alice-resumption -s alice-open-loop-review
```

Add `alice-explain-provenance` for evidence-first responses and `alice-correction-loop` for correction-heavy sessions.
