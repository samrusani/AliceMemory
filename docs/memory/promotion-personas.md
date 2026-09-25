# Memory promotion personas

Alice's write gate historically had one setting: every agent memory proposal
went to human review. That is right for an enterprise deployment and wrong for
a personal second brain, whose owner never opens the review queue.

This document says plainly who each persona is for, what the tradeoff is, and
what an enterprise buyer should be told.

## Positioning

**Tiered promotion is a personal and team feature. Enterprise deployments
leave it off, and off is the default.**

`enterprise` is the default persona, and a deployment that configures nothing
at all behaves exactly as it did before this feature existed. An enterprise
buyer asking "can an agent write to memory without review" should get:

> Not in your configuration. `ALICE_MEMORY_PERSONA` is unset, which is the
> review-gated behaviour you already have, and auto-promotion additionally
> requires you to issue an agent API key. Leave both alone and nothing
> changes.

That is the honest answer. It is not a discussion of writer trust levels.

## The personas

| persona | who it is for | behaviour |
|---|---|---|
| unset | everyone, until they choose otherwise | identical to the pre-promotion write gate |
| `enterprise` | explicit review-gated posture | identical to unset, plus an audit record of the decision |
| `personal` | one owner, one second brain, no review queue | auto-promote when nothing escalates |
| `team` | a small team that wants the writes and a digest afterwards | auto-promote, plus a non-blocking `review.item_created` digest entry |

## What has to be true before anything is promoted

1. A persona is configured, by the owner's Brain Charter or by
   `ALICE_MEMORY_PERSONA`.
2. At least one active agent API key exists. Without a key nobody is
   distinguishable from anybody, so no writer is trusted.
3. The writer is either an agent whose identity resolved through an issued key
   (`authenticated_agent`), or the owner on a surface that actually
   authenticated this call (`owner`). A caller-asserted identity and an
   unidentifiable caller are never promotion-eligible.
4. The agent's permission profile permits the write in the first place.
   Promotion removes the human gate in front of an authorization; it never
   widens one. `read_only_agent` and `memory_proposal_agent` never promote.
5. No hard floor rule fires: credential material, agent-directed instructions,
   or re-ingestion of the agent's own output as fact.
6. No enabled escalation filter fires.

## What the content filters do and do not model

The three defences against content Alice pulled in are deliberately split.

`indirect_provenance` reads the declared `source_type` as an allowlist of
labels meaning "the writer composed this". Anything unrecognised, including
an invented label or an empty one, counts as external and escalates. Reading
it the other way round rewarded a relabel: an agent wanting to dodge the
filter only had to make a string up.

`unverified_authority_claim` does **not** consult the source type at all. It
is the one content backstop that does not rest on self-declaration, because
the ASI06 threat model is an agent manipulated by content it fetched, and a
defence that asks the possibly-compromised component to label its own fetch
honestly is not a defence. It models a specific shape: a claim that approval
has *already* been given, which asserts that scrutiny is unnecessary. A dated
report of an approval ("Legal approved the contract last Thursday") carries
no such marker and promotes.

`agent_control_vocabulary` stays scoped to external provenance, because its
members ("do not tell the user", a pasted `Assistant:` transcript, a note
opening "New instructions:") are ordinary things for an owner or an
authenticated agent to record and an attack surface only when they arrive
inside fetched material.

The hard floor carries only shapes with no ordinary declarative reading. The
test is grammatical rather than lexical: an imperative addressed to the agent
("ignore all previous instructions"), a second-person authority claim coupled
with an instruction to persist it, or control markup. The same words in a
declarative sentence are a note, which is why "we agreed to ignore the
previous guidelines" and "the style guide says to ignore prior rules" both
promote.

"Addressed to the agent" is decided by the subject of the clause, not by
whether it has one. A second-person subject leaves a clause a command, so
"could you ignore your previous instructions" and "if you ignore your prior
instructions" are on the floor along with the bare imperative. Any other
subject makes it a report. The one exception is the bare-infinitive
construction, where "our policy lets you ignore the previous guidelines" is a
report about a permission and promotes.

Everything the floor reads is read on every text-carrying field: title,
canonical text, conversation excerpt and `source_refs`, including refs nested
inside mappings and lists. Refs are persisted on the row and replayed into
context packs like body text, so a rule that skipped them would be optional.

### One credential rule, one check on the write paths

The write paths below all call one check, `carries_credential_material` in
`alicebot_api.credential_floor`. Two of them, the memory commit and the
promotion floor, also run v0.16.0's own check (see "Two checks at the doors
v0.16.0 checked" below). Since 2026-09-23 it is the design round's
"precision-first plus keyed pairs" detector, chosen after three designs were
scored on a held-out corpus no designer saw. It recognises a credential by
its value, and it reads three things:

1. Each field on its own, raw and after unicode normalisation, plus base64
   and hex decodings, character-despaced runs, and text with literal `\n`,
   `\r\n` and `\t` escapes turned back into whitespace: self-identifying key
   formats that need real key material after the prefix (OpenAI, Anthropic,
   Stripe `sk_live_`, `sk_test_`, `rk_live_` and `rk_test_`, GitHub, GitLab,
   npm, Slack, AWS, Google, Hugging Face, SendGrid, Slack webhooks, PyPI, and
   Alice's own `alice_sk_` agent key), private key material (below), Bearer
   with an opaque token, a JWT, a password inside a URL, "the password for X is <value>",
   and a `NAME=value` line whose name is a secret name and whose value looks
   like a secret.
2. Each key and value pair of a mapping body, alone and never joined, under
   the same name grammar. So `{"api_key": "<key>"}` in a body is caught,
   while structural keys such as `fact_key`, `dedupe_key` or `next_page_token`
   are not secret names.
3. Across fields, only the case-exact formats above, so a key split across a
   title and a body is still caught. A field that is a copy of another (a
   summary that is the text's first 280 characters) breaks the join, so a
   row's end is never read against its own start.

**Private key material: recall wins over precision.** Four rules, any of
which refuses:

- the dashed, case-exact armor line of a private key on its own, anywhere in
  a field, whatever surrounds it: five dashes, `BEGIN`, an optional `RSA`,
  `EC`, `DSA`, `OPENSSH` or `ENCRYPTED`, `PRIVATE KEY`, five dashes; and the
  same line for a `PGP PRIVATE KEY BLOCK`. A header with redaction filler
  after it, or a header quoted in a sentence, is refused too (this document
  names the line rather than quoting it for that reason);
- a case-exact `BEGIN ... PRIVATE KEY` (or `PRIVATE KEY BLOCK`) header
  with a real base64 key body after it. The header's dashes may be missing
  or replaced by a dash-like character (hyphens, en and em dashes, the minus
  sign), and the header and body lines may carry a quote or comment prefix
  (`> `, `# `, `//`), be joined with `<br>` or flattened with a literal `\n`.
  The body starts a following line and is a run of at least 16 base64
  characters (a key's first body line is 64 or 70). Folded onto one line, as
  the commit door folds whitespace, it follows the header's armor dashes or a
  quote prefix, or is a whole 40-character line. So a note that names the
  header and then says "1Password", "HashiCorp" or a date is not refused;
  across fields, the body's first 16 characters must sit in one field;
- a PuTTY `.ppk` file: its `PuTTY-User-Key-File-2:` or `-3:` header and, in
  the same field, either its `Private-MAC:` (40 or 64 hex digits) or a real
  private body after `Private-Lines: N`. A note that describes the format,
  naming the header and the sections, is not refused;
- a base64-encoded key file: the first 128 characters of every base64 run
  of 40 or more are decoded, joined across line breaks or the single spaces
  the commit door leaves of them, and refused when they start a private key
  armor line (kubeconfig `client-key-data`, a Kubernetes `tls.key`,
  `SSH_PRIVATE_KEY_B64=`, a file wrapped at any width). A run that starts a
  PuTTY file is decoded in full (up to 16 KB) and judged by the PuTTY rule.

The header-only refusal is back since round 3 of S4.4 (2026-09-23), and this
is why. v0.16.0 refused the armor line on its own. Round 2 read a header only
with a key body after it, and the adversary review then passed real keys
pasted behind `> ` or `# `, as double-escaped JSON, as a JSON array of lines
and joined with `<br>`; a key file encoded as base64 was never decoded at
all. A private key is the most dangerous thing to store and the rarest to
appear innocently, so a documentation note that quotes the armor line is
refused along with the key. The case-insensitive form without dashes
("begin by rotating the private key /certificate pair") is not read at all:
it refused ordinary prose.

Verified on 2026-09-23 against real keys generated once for the tests and
stored base64-encoded as throwaway fixtures: OpenSSH ed25519, RSA and ECDSA,
each unencrypted and passphrase-encrypted; traditional and PKCS#8 RSA and
EC, including an encrypted RSA key; two OpenPGP secret keys from GnuPG; and
two PuTTY files, v2 RSA and v3 ed25519, built from the OpenSSH keys to the
documented format because puttygen is not installed (structurally faithful,
not puttygen output). Each armored key is refused raw, with JSON-escaped
newlines, with CRLF, as a one-line escaped `.env` value, inside a
service-account JSON, inside a mapping body, split between title and text,
behind `> ` and `# `, double-escaped, as a JSON array of lines, joined with
`<br>`, and base64-encoded as `client-key-data`, `tls.key`,
`SSH_PRIVATE_KEY_B64=` or wrapped at 40, 44, 64 or 76 characters, at the
detector, the commit door and import; the armored keys also with their
dashes replaced by any of seven dash-like characters, and with no dashes and
`> `, `# ` or `//` in front of every line. A key pasted next to its public
half is refused.

**Not refused:** SSH public keys since 2026-09-23 (`ssh-ed25519`, `ssh-rsa`,
`ecdsa-sha2-nistp256` and its siblings, and the FIDO types
`sk-ssh-ed25519@openssh.com` and `sk-ecdsa-sha2-nistp256@openssh.com`),
alone or after a label such as "Deploy key:"; a PEM public key, a
certificate and an OpenPGP public key block; a GPG key id or fingerprint
under the `signingkey` setting. A public key is
published on purpose, and until then every door refused one.

The scan is linear in the length of the text. Measured on 2026-09-23 on the
ten shapes of the committed linear-time test, at 200 KB each took at most
0.34 seconds, and four times the input cost 3.8 to 5.0 times the time
(three runs). One scan of the assignment pattern used
before 2026-09-22 took 43 seconds on 50 KB. A field that grows more than four
times under unicode normalisation (and past 1,024 characters) is refused with
its own message rather than scanned or truncated. The same cap also counts
the distinct strings of one write together. Ordinary scripts grow at most
1.44 times. The linear-time claim is about this check only, not the whole
promotion evaluation.

Since 2026-09-23 the promotion floor's credential clause calls the same check
(addendum F1), over every field the write persists: title, text, excerpt,
rationale, source refs and project scope. Before that it ran its own rule,
which disagreed with the write floor in both directions. On the 100-note
promotion corpus no persona's outcome moved.

### Two checks at the doors v0.16.0 checked

v0.16.0 checked credentials in two places: the memory commit gate and the
promotion floor. On 2026-09-23 a review found this check storing, at the
commit door, shapes v0.16.0 refused there: a secret word anywhere in a name
but the last segment (`PASSWORD_DB=`, `TOKEN_GITHUB=`, `API_KEY_RAW=`,
`GITHUB_TOKEN_WRITE=`, `PRIVATE_KEY_DATA=`), primary and routing keys, hex
AES keys, Telegram, Discord, Mapbox and Google OAuth tokens under secret
names, numeric prose passwords, Django secret keys with symbols, and hex
dumps of key files. The class was structural, so these two doors now refuse
when either of two checks refuses:

1. `carries_credential_material`, as everywhere else; or
2. v0.16.0's own check at that door, re-implemented in linear time in
   `alicebot_api.legacy_credential_check`, over the fields v0.16.0 read
   there, less four named carve-outs. The commit gate gets v0.16.0's commit
   check (the floor rule plus its prefix patterns), per field: title, text,
   excerpt, rationale and every source ref string, keys included. The floor
   gets v0.16.0's floor rule only, over title, text, excerpt and source
   refs, also joined with a space and with nothing, as v0.16.0 read them.

The carve-outs are the design round's named false positives, and there are
no others. Each excuses one finding at a time, so a note with an excused
`sk-learn` and a real token is refused for the token:

- **SSH public keys and key-type names**: `ssh-ed25519`, `ssh-rsa` and the
  other key types, a full public key, the FIDO types, and a key type as the
  value of a key name (`key: ssh-ed25519`), but not the header line of a
  PuTTY private key file. The detector still refuses a key type alone after
  a qualified label such as "Deploy key", unless the public key body
  follows it.
- **`sk-` and lower-case words**: `sk-learn`, `sk-learn-bench-2024`.
  Upper case (`SK-LEARN`, `SK-1482-fix`) is not excused.
- **Structural key names with identifier values**: `fact_key` and the
  continuity subject keys, `cache_key`, `partition_key`, `sort_key`,
  `next_page_token`, `memory_key`, and dedupe keys (`dedupe_key`,
  `*_dedupe_key`, `idempotency_key`), with a value made of lower-case words,
  short numbers, UUIDs or hex digests; `next_page_token` may also hold an
  opaque cursor of up to 128 characters that holds none of the token
  prefixes v0.16.0 knew. `{"key": "Q3-plan"}`, `token_budget` and
  `session_key` are not on the list.
- **Dotted references to where a secret lives**: a value rooted at
  `settings.`, `config.`, `self.`, `secrets.`, `ENV.`, `process.env` or
  `os.environ`, as in `api_key = settings.OPENAI_API_KEY`, whose attributes
  are names as people write them (snake, upper snake, camel or Pascal case,
  each word ending in at most three digits). A random run of letters and
  digits after the root is not excused.

Every carve-out ends at a boundary, a character that cannot extend a token
(not a letter, digit, `_` or `-`). A FIDO key type, a public key body, or a
key type followed by a word, with a token glued straight onto its end, is
not excused: the review of round 5 found each of those stored at commit,
where v0.16.0 refused them.

No carve-out applies to a name containing "passw". The detector itself also
no longer reads a dotted value under a password name as a code reference,
at any door: a `DB_PASSWORD` set to a dotted phrase with a year in it was
stored. The price is that a password name set to `process.env.DB_PASSWORD`
or `var.db_password` is refused with it (this document names those lines
rather than quoting them, since the check would refuse them here too).

Every other door (import, proposal, `correct()`, approve and accept, the
review edit, artifact promotion, `/v1`, the continuity writes and memory
admission) keeps `carries_credential_material` alone, as the table below
lists. The name-order class above passes those doors.

What this costs, measured on 2026-09-23 at the commit door by running both
this branch and v0.16.0 (880915a) itself: of 400 sentences sampled from this
repository's docs, neither refuses any; of the 474 doc sentences that use
credential vocabulary, both refuse the same 6 (five lines of this document
that quote refused examples, and one about a publish job's `id-token: write`
permission from an audit report); of the 100 promotion-corpus notes, neither
refuses any; of the design round's 34 named benign notes, v0.16.0 refused 27
and this branch refuses 16. The 16 are notes v0.16.0 refused at commit that
no carve-out covers, among them "The password policy is 12-character minimum
with one symbol.", "In Q3 we begin private key rotation for the signing
service.", "Our team password manager is 1Password.",
`ALICE_SECRET_PROVIDER=encrypted_local` and `{"csrf_token": "a8Fk2LmQ9x"}`.
The doors that use the new check alone accept them. At import, which v0.16.0 did not check, 4 of the
474 are refused. The counts are pinned in
`tests/unit/test_legacy_credential_check.py`.

The re-implementation is compared with v0.16.0's own code, kept verbatim as
a test oracle: the same verdict at both doors on each of 4,500 generated
inputs in CI, and on 70,000 more when this was written. v0.16.0's NAME=value
expression was quadratic (4.3 seconds for one 16,000-character field of
`a_a_a_...`, over 30 seconds for `key_key_...`); the re-implementation,
both doors together, took at most 0.42 seconds at 200 KB on the nine
adversarial shapes of its linear-time test, and four times the input cost
3.9 to 4.1 times the time (three runs).

### What is covered

Verified by execution on 2026-09-23; each row names the tests that pin it.

| Write | Surfaces | Result | Pinned by |
|---|---|---|---|
| Memory commit | `alice_memory_commit`, `alice_vnext_commit_memory`, `POST /v0/vnext/memories/commit`, the CLI | `rejected`, reason `unsafe_secret_storage` (`unsafe_text_expansion` for the expansion cap), no row written. Also refused when v0.16.0's commit check refuses and no carve-out applies | `test_credential_floor_every_door.py` door 2, `test_vnext_memory_commit.py`, `test_legacy_credential_check.py` |
| Memory proposal | `POST /v0/vnext/memory-proposals`, `alice_vnext_propose_memory`, `alicebot vnext agents propose-memory`, all through one function | refused before anything is written, the policy audit included; rationale and source refs are read too | `test_credential_activation_and_withhold.py`, integration `test_round2_c2_http_propose_*` |
| A vNext memory row created in, or moved into, `active` or `accepted` | both stores' `create_memory` and `update_memory` | refused (`CredentialActivationRefused`, a `ValueError`). Transitions only: an update that leaves an active row active is not read here, only by the door that sends it | `test_the_store_*`, integration `test_round2_c2_the_postgres_store_*` |
| Approve or accept of a row already stored | inline confirm (`alice_memory_manage` confirm, `alice_memory_commit` with `confirmation_id`, `POST /v0/vnext/memories/confirm`, `alice_vnext_confirm_memory`, the CLI), review accept and promote (`POST /v0/vnext/memories/{id}/review`), `alice_memory_correct` approve, consolidation accept, project-update accept (`POST /v0/vnext/projects/update-candidates/{id}/review`), legacy continuity confirm (`alice_review_apply`), open-loop `still_blocked` (`POST /v0/continuity/open-loops/{id}/review-action`) | refused in the door's own error contract, row unchanged; the reason stored with the approval is read too. A test lists these doors: it walks the source for functions that write a literal searchable status (a heuristic, not proof), and the doors that compute the status are listed by hand | `test_credential_activation_and_withhold.py`, integration `test_round2_c2_*`, `test_round3_*` |
| `correct()`, unexpire | `POST /v0/vnext/memories/correct`, `alice_vnext_correct_memory`, the unexpire route, tool and CLI | refused, row unchanged; unexpire counts as an activation whenever it clears `valid_to` | door 3 tests, `test_r2_unexpire_is_an_activation_and_refuses` |
| Review edit, text the request supplies | `POST /v0/vnext/memories/{id}/review`, `alice_memory_correct` edit-and-approve and supersede-existing | refused, row unchanged; a title-only edit is read against the stored body | door 3 tests, integration `test_round2_a_title_only_*` |
| Reject and retire | confirm reject, review reject, `alice_memory_correct` reject, `alice_review_apply` delete and mark_stale, the open-loop review-action note, expire, forget, undo, the quarantine sweep | always completes. A reason carrying credential material is stored as `rationale withheld: it carried credential material` in the row, the revision and the event, and the response carries `rationale_withheld: true`. Text supplied with a reject is withheld the same way (`text_withheld: true`). History entries a writer carries forward are withheld too | `test_confirm_reject_*`, `test_r2_*`, integration `test_round2_c6_*` |
| Artifact promotion | `POST /v0/vnext/artifacts/{id}/review` action `promote`, `alice_vnext_artifact_review`, `alicebot vnext artifacts review` | refused, no memory created | door 5 tests |
| `/v1` memory operations | `/v1/memory/operations/candidates/generate` and `/commit`, `alice_memory_mutations_generate` and `_commit` | refused, transaction rolled back | door 4 tests |
| Legacy continuity writes | `/v0/continuity/captures` when it derives an object, `/v0/continuity/captures/commit`, `/v0/continuity/review-queue/{id}/corrections`, `alice_commit_captures`, `alice_review_apply`. The Hermes plugin used to route a commit HTTP 400 into `POST /v0/continuity/captures`. It no longer does that | refused, transaction rolled back | door 4 tests |
| Legacy memory admission | `POST /v0/memories/admit`, `/v0/memories/extract-explicit-preferences`, `/v0/open-loops/extract-explicit-commitments`, `/v0/memories/capture-explicit-signals`, including the open-loop title these write | refused before any branch, request rolled back | integration `test_round2_r1_*` |
| Backup restore | `alice-memory import`, every memory record whatever its status: title, text, a summary that is not a copy of the text, the `value` column by value, `metadata_json` keyed (correction history included) except the keys the product itself writes (`rollup_key`), `memory_key` and `project_id` | refused with `import_credential_material`, no records written; stderr lists the line and memory id of every offender, never the text. A rollup card restores | door 6 tests, `test_round2_import_*`, `test_private_key_recall.py` |
| Markdown, ChatGPT, and OpenClaw import | `import_markdown_source`, `import_chatgpt_source`, `import_openclaw_source` | the item is skipped and the rest of the file is imported. The receipt counts `skipped_credentials` and names each skip in `skipped_credential_items` by id or line, never the matched text. Fields checked: title, the body with its keys, provenance by value, raw content, and the segment text. An OpenClaw raw entry is passed by value, and pair detection reads the segment's canonical JSON. A dashed private-key block in markdown is one skipped item when the BEGIN line and the END line stand alone, share a label, every line between them is key body, and at least one of those lines is radix-64 text of 40 or more characters. Key body is base64 or radix-64 text, a `=` checksum line, a blank line, or a `Name: value` armor header. A `Name: value` line counts only as a run directly after the BEGIN line, before the first blank line or radix-64 line. A code fence, another BEGIN line, or any other line stops the scan, and that BEGIN line is one item on its own. The receipt names the block's line range. Receipt line numbers count from 1 on the first line after frontmatter. Placeholder password examples are skipped at import | `test_importer_credential_check.py` |

### What is not covered

Stated so nobody reads the table above as "every surface".

- **Source text, and what import still leaves in place.** Source capture
  (`alice_capture`, connectors) archives source text as written, and nothing
  checks that archive. The markdown, ChatGPT, and OpenClaw importers check
  each item and skip one that holds credential material, naming it on the
  receipt. What remains uncovered: the archived source copy, which still
  holds a skipped secret, and a private-key body that is not inside a dashed
  block. The block skip needs the BEGIN line and the END line to stand
  alone, with the same label, only key body between them, and at least one
  radix-64 line of 40 or more characters. A `Name: value` line is key body
  only as a run directly after the BEGIN line, before the first blank line
  or radix-64 line. Notes between a lone BEGIN line and a lone END line are
  imported, including unbulleted typed notes and one-word lines when the
  block does not form. A body line that starts with a list marker or a `>`
  prefix is not key body, so a key written that way is not one block. A
  body line outside such a block is still one item per line, so a line that
  does not itself trip the floor is stored. Receipt line numbers count from
  1 on the first line after frontmatter.
  Placeholder password examples are skipped at import. The floor cannot
  tell a placeholder password from a real one, so the example line is named
  on the receipt and the rest of the file is imported.
- **Background writers' candidates.** Consolidation, rollups, brain insights,
  project updates, scheduler workflows and agent-output ingestion create
  candidate rows without checking them. None of them creates an active row;
  approving one meets the activation check.
- **The capture inbox and open loops.** A legacy `/v0/continuity/captures`
  call that derives no object stores its raw text in the capture inbox.
  `capture_continuity_input` runs `commit_door_secret_verdict` on that
  text after the empty check and returns HTTP 400 when the check refuses,
  so the memory-write mirror, the HTTP 404 fallback, and a client that
  sends `user_id` in the body do not store credential material. Ordinary
  prose that trips the legacy gate is refused on this route too. The
  Hermes plugin no longer falls back on HTTP 400. HTTP 404 still uses the
  route when the candidate endpoints are absent. The legacy standalone
  open-loop create route (`create_open_loop_record`) and open-loop titles
  in general are not checked, except the title the four admission routes
  write.
- **Import beyond memory rows.** The revisions, events and non-memory records
  in an import file are not checked.
- **Reasons stored before this change.** A credential in a reason, rationale
  or history entry written before 2026-09-23 stays in rows that are never
  rewritten, until those rows are redacted. A writer that rewrites a history
  withholds what it carries forward.
- **Provenance is read by value only** (owner ruling C3). On the legacy
  continuity writes, the two review surfaces and the import `value` column,
  keys are not read. A Stripe key is still caught there by its prefix, but
  under a key name an AWS secret access key and a plain password are not
  caught. This is not an edge case: provenance is where an agent would put a
  secret if it wanted to. The root cause, a name rule that counts any `*_key`
  as a secret name, is a follow-up ticket; once it lands, key and value
  reading returns on provenance and the value column.
- **Splits of a label and its value across two fields.** The credential
  check reads each field on its own, and it reads a case-exact key format
  across a field boundary. A label in one field and the value in another
  is not that, so `carries_credential_material` does not flag a title
  `Prod DB password:` with the value in the body, and the commit gate does
  not refuse it. The promotion floor still holds that title back.
  `hard_floor_hits` returns `credential_material`, because the floor also
  runs v0.16.0's promotion floor rule. An authenticated agent under an
  auto-promote persona does not promote it. The same title without the
  colon is not held back. Both results are pinned:
  `test_f1_a_password_label_with_a_colon_is_still_held_by_the_promotion_floor`
  and
  `test_f1_the_label_split_residual_is_not_held_back_and_that_is_documented`.
- **A `fact_key`/`value` structured body.** The promotion floor does not
  hold one. `hard_floor_hits` does not return `credential_material` for a
  mapping such as `{"fact_key": "deploy_owner", "value": "Platform"}`.
- **A lower-case AWS id split across fields** (`id ak` over
  `iaiosfodnn7example`). In one field it is caught in any case.
- **A split whose second half is a copy of another field.** Someone who builds
  a backup by hand can put `Deploy token: ghp_` in a summary and the rest of
  the key in the value's text; the copy breaks the join. It needs an attacker
  who writes the export file.
- **A bare `alice_sk_` key body, and a rare `alice_sk_` key.** The 43
  characters after `alice_sk_` are not recognised without the prefix. With
  the prefix, about one generated key in 20,000 to 200,000 (measured
  2026-09-23: 1 of 20,000 from the real generator, 1 of 200,000 seeded) has
  enough `-` and `_` separators that its segments read as short words, and it
  is missed. Left to the design round rather than special-cased here.
- **Config shapes no rule reads.** `machine host login user password X`
  (netrc), `aws configure set aws_secret_access_key X`, `curl -u user:X`,
  `docker login -p X`, `mysql -pX`, a `.pgpass` line and
  `Authorization: Basic` all pass (pinned in `test_private_key_recall.py`).
  An INI or YAML line with the secret name and `=` or `:` is caught.
- **An upper-case Slack prefix.** `XOXB-` and the other Slack prefixes are
  read case-exact, as Slack issues them; an upper-cased token passes.
- **Formats the check does not know.** `Authorization: Basic` with base64
  user and password, `Authorization: Token <hex>`, a lower-case `bearer`,
  `mysql -p<password>`, `docker login -p`, a JSON `"pass"` field, wallet seed
  phrases, `0x` private keys, Azure SAS tokens and connection strings, Twilio,
  Discord and Telegram bot tokens, and any high-entropy string with no
  prefix and no secret name next to it. At the commit door and the
  promotion floor, v0.16.0's check still catches some of these under a
  secret name.
- **Names with the secret word first, or an unlisted suffix, at every door
  but commit and the floor.** `PASSWORD_DB=`, `TOKEN_GITHUB=`,
  `API_KEY_RAW=`, `GITHUB_TOKEN_WRITE=`, `PRIVATE_KEY_DATA=`, primary and
  routing keys and hex AES keys pass import, proposals, `correct()`, the
  review edit and the other doors that use the new check alone. Those doors
  never had v0.16.0's check.
- **Hex dumps of some key files.** An `xxd -p` dump of an encrypted PKCS#8
  key or an OpenPGP secret key passes every door, as it passed v0.16.0: its
  first 60-digit line decodes to a header cut short, which neither check
  reads. Dumps of the other key files are refused at the commit door by
  v0.16.0's check; at the other doors only the PKCS#8 and EC dumps, whose
  whole armor line fits in the first 60 digits, are refused.

### What it refuses anyway

- **Toy passwords in documentation.** A `NAME=value` line with a real secret
  name and a password-shaped value is refused even in documentation, so
  `DB_PASSWORD=hunter2abc` in a note and
  `PGPASSWORD='drill-password-from-your-secret-manager'` in a shell example
  are refused (owner ruling: no allow-list of toy passwords). So are "Bank
  portal password: Changed after the phishing scare.", "Password:
  see-vault", "Secret: Launch2027PlanB", `BUILD_KEY=v2026.09.1`, the jwt.io <!-- gitleaks:allow -->
  sample token, `AKIAIOSFODNN7EXAMPLE`, upper-case coincidences such as
  `SEE ASIA` over `PACIFICTEAMNOTES...`, and `password = hash_password(pw)`
  in a code note. Remove or reword the value; a vault that already holds one
  is fixed by redacting the row.
- **A backup that holds one of these cannot be restored** until the row is
  redacted in the source vault and the vault exported again. If the source
  vault is gone, `alice-memory import --quarantine` removes the credential
  from the named memory and from the records derived from it, stores that
  memory as `rejected`, and reports any other copies it finds. See
  [Backup and restore](../alpha/backup-and-restore.md).

### Known residuals of the promotion floor

1. The floor is a grammatical model of English. It has residual false
   negatives on unusual constructions and residual false positives on notes
   genuinely phrased as instructions. The measured rate on adversarial
   ordinary material is roughly 95%, not 100%.

   > Correction, added 2026-09-23. The 95% figure is the share of ordinary
   > notes the floor lets through (94.2% auto-promotion on held-out ordinary
   > notes at v0.15.1; CI now asserts 100% on the committed 100-note corpus).
   > It is a false-positive measure. It was never a catch rate on
   > instruction-shaped attacks, and no such rate has been measured: the
   > S4.4 review found a paraphrased exfiltration rule the floor misses and
   > a genuine owner preference it flags.
2. The prose fields are matched as one joined surface, which is what holds an
   injection split across a title and a body. The same join means a field
   ending on a bare floor verb and a field beginning with that verb's object
   can spell a command neither field contains, and that pair gates. Narrowing
   it by matching fields separately would reopen the split-injection bypass,
   so it stands as a disclosed false positive rather than a traded-away
   defence. It is pinned in the unit tests in both directions.
3. `agent_control_vocabulary` is provenance-scoped by choice, so a pasted
   transcript shape relayed under an internal `source_type` promotes.
4. A claim about a person referred to by first name alone is not caught by
   `third_party_person`.
5. The hard floor only decides whether a write may skip review. It never
   lowers an outcome, so instruction-shaped content that the ordinary commit
   gate already commits is stored active whatever the persona, and
   `correct()`, `confirm()`, artifact promotion, the `/v1` memory operations
   and `alice-memory import` do not read it at all. Refusing it outright
   would also refuse ordinary notes phrased as instructions ("always use pnpm
   here"), so it is an open owner decision rather than a fix in waiting.
   What changed on 2026-09-23 is on the read side: the SessionStart brief
   opens with a line saying the notes below are quoted data, not
   instructions, and quotes every item.

## The tradeoff, stated plainly

On a deployment that opts in, an authenticated agent at `trusted_local_agent`
or above can write durable memory without a human gate. **A compromised agent
key can therefore poison memory.** That is the cost of trusting an out-of-band
credential instead of trying to read intent out of the sentence, and the
alternative was measured: inspecting content to decide whether an agent could
be believed refused every agent write, because an agent authors both its claim
and any evidence it offers for the claim.

What stands against it:

- it is opt-in per deployment and additionally requires a key to exist;
- every write records which agent made it, under what source type, at what
  writer trust, and the read path surfaces that on the row so a poisoned
  memory is visibly agent-authored rather than anonymous;

  > Correction, added 2026-09-24. Recall and resume items include `writer`
  > (`writer.id` and `writer.established`), on the MCP tools and on CLI
  > resume. Context packs include `writer` too. The SessionStart brief
  > still shows no writer.
- the row stays undoable and expirable through the ordinary lifecycle;
- credential material is refused on the write paths listed under "What is
  covered", whoever the writer is;

  > Note, added 2026-09-23. Until 2026-09-22 this bullet read "the hard floor
  > still catches credentials and agent-directed instructions whatever the
  > writer". That was false: the floor only kept such writes from skipping
  > review, and several write paths did not check credentials at all.
- the hard floor keeps agent-directed instructions from skipping review. It
  does not refuse them: an instruction-shaped note that the ordinary commit
  gate already commits, such as an explicit remember from a direct source at
  confidence 0.85 or more, is stored. See Known residual 5;
- `alicebot vnext memories quarantine` expires everything a named key
  auto-promoted in a window, so a compromised key's blast radius is bounded
  and recoverable rather than unbounded in count and time.

## Recovering from a compromised key

```
alicebot agent keys revoke <key>                       # stop the next write
alicebot vnext memories quarantine \
    --target-agent-id <id> --reason "incident" --dry-run
alicebot vnext memories quarantine \
    --target-agent-id <id> --reason "incident"
```

**Quarantine is deliberately CLI-only. This is a decision, not unfinished
work, and it should not be reopened without revisiting the reasoning.**

Two reasons. The first is security: quarantine exists precisely for the case
where an agent key has been compromised, and it is reachable by the human
operator and by an `admin_agent` key. An HTTP route would widen who can reach
the incident control to anything able to present an admin key over the
network. Requiring shell access on the host is a posture, not a scheduling
compromise.

The second is that adding the route moves receipts in seven test files beyond
the two authorised for this work, and those seven are not all drift
detectors: the public error shapes and the vNext auth surface are behavioural
contracts with external consumers. Updating nineteen tests across that set to
add parity for a control that already has a working operator surface is the
kind of change where a guard gets updated because it was in the way rather
than because somebody understood what it pinned. This repository has already
lost two guards that way.

The sweep expires, it never deletes or redacts. Rows, revisions, provenance
and events all survive, the sweep records exactly which ids it acted on, and
`memory.unexpire` reverses it row by row. Who can run it: the human operator, and an `admin_agent` key **for its own
writes only**. No agent below `admin_agent` can run it at all, and an admin
key naming another agent is blocked with
`quarantine_limited_to_own_agent_id`, so the sweep cannot be used to bury
somebody else's memories. Sweeping an arbitrary key is the human operator's
to do.

It can only reach rows carrying a `memory.auto_promoted` event for the named
agent. A reviewed write, a human write, and another agent's write are not
addressable by it at all.

## Configuration

See `.env.example`. `ALICE_MEMORY_ESCALATION_FILTERS` is an enable-list: an
empty value keeps all filters on, and turning them all off requires the
explicit `DISABLE_ALL_ESCALATION_FILTERS` token.
