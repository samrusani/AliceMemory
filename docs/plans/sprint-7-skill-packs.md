# Sprint 7: skill packs

Design only. No pack text ships with this note, and this note makes no
behavior claim.

This note was checked against `mcp/definitions.py`,
`tests/unit/test_agent_facing_write_verb_guidance.py`,
`tests/unit/test_skill_packs_are_loadable.py`, and the packs named below.

## Corrections

The commit rule stays the v0.15.4 rule. `alice_memory_commit` says to
record a durable fact "including when the user has not asked you to
remember it". Both shipped packs say the same. `RETIRED_COMMIT_GATES` in
`test_agent_facing_write_verb_guidance.py` lists the wording v0.15.4
retired. A pack does not tell the agent to wait until the user has asked
to save a fact. If that rule should change, argue it separately, with the
measurement below as evidence.

Not every host gets text at session start. The hook is installed only for
Claude Code and Cursor. On Claude Code with tool search, only tool names
and server instructions load at session start, and Alice sends no server
instructions.

Counting an agent's tool calls needs a model. A run with no credentials
makes no calls. The measurement cannot run on the no-secrets real-host job.

The packs that exist name `alice_capture` and `alice_context_pack` as
full-surface tools, and other tests require them to describe what capture
returns. A test that fails on any fourth tool name would fail on both
packs. That is not the test.

## What Sprint 7 changes

Sprint 7 revises the packs that exist. It does not add new packs.

- `agent-skills/hermes/alice-memory`
- `agent-skills/openclaw/alice-project-memory`
- the older `docs/integrations/hermes-skill-pack/skills/alice-workflows`.
  It is legacy. It uses the manual `alice_core` server name plus
  full-surface tools, and no test scans it. The `alice_core` prefix does
  not match the server name the supported packs use. Sprint 7 will revise
  that mismatch: update the pack to the default tools, or keep it marked
  legacy and bring it under `test_skill_packs_are_loadable`.

`README.md` called these "a ready-made instruction pack for each host".
Packs exist for 2 of the 5 install hosts, and nothing measures them. This
pull request corrects that sentence.

The default-path steps a pack teaches use only the three default tools:
`alice_memory_commit`, `alice_recall`, and `alice_resume`. Full-surface
tools may be named only in a clearly marked full-surface section. Test
that, not a ban on the names. Do not add a case-sensitive `Stop` check. It
collides with ordinary prose.

A pack does not add a fourth tool, and it does not commit on the agent's
behalf. It does not register `SessionEnd` or `Stop`. Sprint 7 leaves the
Hermes memory provider's system prompt block out of scope.

## Commit trigger

Keep the v0.15.4 rule. Agents commit durable facts without being asked, and
the packs keep saying so. Change the fourth measured count. Do not count
commits on a prompt that asks for none. Label each commit as durable or
not, and count non-durable ones.

## Measurement, before any pack change

A pack change ships only with a measurement. No sentence may claim a
behavior change without one.

The measurement:

- Where: a separate `workflow_dispatch`-only workflow, not the no-secrets
  real-host job, using a model API key the owner adds as a secret. If the
  owner does not add one, the measurement waits.
- Pinned setup: the host, model, and version are pinned. Claude Code is
  first. Tool calls come from the host's stream output (`claude -p
  --output-format stream-json`), with prefixed names normalised.
- Recorded per run: whether the skill loaded.
- The arms: the same prompts with the pack absent, and the same prompts
  with the pack present.
- Held fixed across both arms: the MCP descriptions, the SessionStart
  brief, and tool search.
- The counts, per prompt and per arm:
  1. `alice_memory_commit` calls on prompts that carry a durable fact,
     asked or not.
  2. `alice_recall` calls on prompts that ask what was saved.
  3. `alice_resume` calls on prompts that ask to continue.
  4. commits labelled non-durable, under a labelling rule written with
     the prompt set before the run.
- Repeats: several per prompt. Thresholds attach to these counts and are
  written down before the run.
- Two prompt sets. A development set in the repo. A held-out set that we
  write and keep private, and run ourselves after the pack pull request is
  up for review. A pack tuned on the prompts it is scored on proves nothing.

Report the counts for both arms. A pack that does not move those counts
did not change behavior. Say that. Do not publish a sentence that says the
pack makes agents remember more until those counts are in the pull request
for the pack change.

## Acceptance for the pack change, not for this note

- The default-path steps name only `alice_memory_commit`, `alice_recall`,
  and `alice_resume`. Full-surface names appear only in a marked section.
- A test fails when a default-path step names another tool. It does not
  fail merely because `alice_capture` or `alice_context_pack` appears in
  the full-surface section.
- The measurement workflow and the development prompt list are committed
  with the pack. The held-out set is not committed.
- The pack pull request states the before and after counts, or it states
  that the measurement has not been run and makes no behavior claim.

## What this design does not do

- It does not register `SessionEnd` or `Stop`.
- It does not read a transcript.
- It does not call a model on a read path.
- It does not change the credential floor or the commit door.
- It does not edit a pack in this pull request. A pack edit waits for the
  measurement above.
