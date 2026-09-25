# Sprint 7: skill packs

Design only. No behavior change ships with this note.

This note was written against `main` at
`e8abf466f3b2727d1ccfe60d01aff622eb301758`. The three default tools are
`alice_memory_commit`, `alice_recall`, and `alice_resume`. A skill pack
teaches an agent to use those three. It does not add a fourth tool, and it
does not commit on the agent's behalf.

## Why this is the weakest lever

A pack works only when the agent chooses to follow it. Install and the
session-start hook put text in front of the model every time. A skill is
optional. A claim that a pack changes behaviour has to be a measurement,
not a reading of the pack text.

## What the pack teaches

Three actions, and nothing else:

- Commit when the user has said to save a fact. The pack points at
  `alice_memory_commit`. It does not tell the agent to auto-save assistant
  narration.
- Recall with `alice_recall` when the user asks what was saved.
- Resume with `alice_resume` when the user asks to continue.

The pack repeats the framing sentence and the quoting rule the session
brief already uses. It does not invent a second wording. It says a commit
is a fact, and a proposal is not a memory.

## What this design does not do

- It does not register `SessionEnd` or `Stop`.
- It does not read a transcript.
- It does not call a model on a read path.
- It does not change the credential floor or the commit door.
- It does not ship a pack that claims a behaviour change before the
  measurement below has been run.

## Measurement, before any claim

Fix the prompt list before the run. Use the same prompts with the pack
absent and with the pack present. Count tool calls, not the model's
prose:

- how often `alice_memory_commit` is called on a prompt that asks to save;
- how often `alice_recall` is called on a prompt that asks what was saved;
- how often `alice_resume` is called on a prompt that asks to continue;
- how often any of the three is called on a prompt that asks for none of
  them.

Report the counts for both conditions. A pack that does not move those
counts is a pack that did not change behaviour. Say that. Do not publish
a sentence that says the pack makes agents remember more until those
counts are in the PR.

The run uses a temp home and no credentials, on the Linux runner, the
same way the real-host trial does. Do not run it on a developer Mac.

## Acceptance

- The pack text names only the three default tools.
- A test fails if the pack text contains `SessionEnd`, `Stop`, or a fourth
  tool name.
- The measurement script and the prompt list are committed with the pack.
- The PR states the before and after counts, or it states that the
  measurement has not been run and makes no behaviour claim.
