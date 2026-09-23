---
name: alice-memory
description: Use Alice as the user's durable local memory. Load before answering from context, and whenever you learn something worth keeping across sessions.
version: 1.0.0
author: Alice Memory
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Memory, Continuity, MCP, Recall]
    related_skills: []
---

# Hermes Alice Memory Skill

Use Alice as the user's durable local memory and continuity layer.

Default loop: remember, recall, continue.

1. Call `alice_memory_commit` whenever you learn a durable fact worth keeping, including when the user has not asked you to remember it. It is the write verb for ordinary memory and what it records is immediately recallable.
2. Call `alice_recall` to search memory and imported sources.
3. Call `alice_resume` to pick work back up: last decision, next action, open loops, recent changes.

`alice_capture` and `alice_context_pack` are full-surface tools. Use them only when the server lists them. Capture stores a source; its passages come back from `alice_recall` under `sources`, as material to read and quote rather than as facts Alice asserts. Candidates stay unsearchable until a reviewer promotes them. Import is a source. Commit is a fact. Print the `receipt` field after a capture or commit so the user sees what was stored. Do not tell the user they must clear a review queue before a note is usable.

Your host may prefix these tool names with the server name. Read the names from the host's own tool list rather than assuming the bare form.

Rules:

- never directly mutate trusted memory or the database
- never bypass Alice policy
- never request sensitive domains unless needed and allowed

Default identity:

```json
{"agent_id":"hermes","agent_type":"personal_assistant","permission_profile":"trusted_local_agent","project_scope":[]}
```

Default scope is broad but policy-filtered. Avoid `health`, `family`, `spiritual`, `legal`, `financial`, and `regulated` unless the user explicitly enables that scope.

Good ambient commit, nobody asked for this one. At 0.84 it comes back `confirmation_required`, so it is not stored until the user answers:

```json
{"title":"Preferred daily planning format","canonical_text":"The user prefers daily planning summaries with decisions, blockers, and next actions.","domain":"personal","sensitivity":"private","confidence":0.84}
```

Ask the user, showing them the proposed text. If they agree, call `alice_memory_commit` again with the `confirmation_id` Alice returned, the same identity fields as the write (none in this example), and no memory fields:

```json
{"confirmation_id":"confirm-...","confirmation_action":"confirm"}
```

If they do not agree, send `"confirmation_action":"reject"`. Alice cannot tell whether you asked, so never answer for the user. To change the text, reject it and commit the corrected text as a new write. After 24 hours it can no longer be confirmed on `alice_memory_commit`: the next confirm or reject there that passes the policy check resolves it to `rejected`. This example sends no identity fields, so on a keyless server the answer is recorded as the local user, not as Hermes.

Good explicit commit, the user said to remember it:

```json
{"agent_id":"hermes","agent_type":"personal_assistant","permission_profile":"trusted_local_agent","title":"Preferred daily planning format","canonical_text":"The user prefers daily planning summaries with decisions, blockers, and next actions.","domain":"personal","sensitivity":"private","confidence":0.93,"source_type":"direct_user_instruction"}
```

A new write needs `title` and `canonical_text`. Everything else is optional, and any field not in the server's `tools/list` schema is rejected outright rather than ignored.

If Alice returns `confirmation_required`, finish it on `alice_memory_commit` as shown above, only with the user's answer. Alice refuses both confirm and reject for a read-only identity and for a key bound to another project (a keyless server trusts whatever `project_scope` the call declares). It refuses confirm, but not reject, when the pending write is above your sensitivity ceiling (anything above `private` for `trusted_local_agent`), so you can clear such a write but not store it. Alice refuses credential material, such as an API token or a private key, on a commit and on a confirm: leave the secret out, and never retry with it reworded, split or encoded. If Alice returns `review_required`, do not tell the user to clear a review queue.

Bad commit, too low confidence to be worth storing:

```json
{"title":"Possible reporting preference","canonical_text":"The user might dislike long reports.","confidence":0.31}
```

See `docs/alpha/hermes-skill.md` for full recipes.
