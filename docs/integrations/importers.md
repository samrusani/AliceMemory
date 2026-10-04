# Importer Integration

Alice ships three importer paths.

## Shipped Importers

- OpenClaw: `openclaw_import`
- Markdown: `markdown_import`
- ChatGPT export: `chatgpt_import`

## Canonical Loader Commands

```bash
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_v1.json
./scripts/load_openclaw_sample_data.sh --source fixtures/openclaw/workspace_dir_v1
./scripts/load_markdown_sample_data.sh --source fixtures/importers/markdown/workspace_v1.md
./scripts/load_chatgpt_sample_data.sh --source fixtures/importers/chatgpt/workspace_v1.json
```

## OpenClaw One-Command Demo

```bash
./scripts/use_alice_with_openclaw.sh
```

See [docs/integrations/openclaw.md](openclaw.md) for end-to-end before/after output and replay expectations.

## Importer Behavior Contract

- imported records are queryable through the `alicebot recall` and `alicebot resume` CLI, which read the continuity store. The MCP tools `alice_recall` and `alice_resume` do not read them
- provenance remains explicit with importer-specific `source_kind`
- dedupe posture is deterministic per source payload
- replaying the same fixture returns noop duplicate skips
- an item that holds credential material is skipped, and the import continues
- the receipt field `skipped_credentials` is how many items were skipped
- `skipped_credential_items` names each skipped item by id or line and does not include the matched text
- receipt line numbers count from 1 on the first line after frontmatter
- a dashed private-key block in markdown is one skipped item when the BEGIN line and the END line stand alone, share a label, every line between them is key body, and at least one of those lines is radix-64 text of 40 or more characters. Key body is base64 or radix-64 text, a `=` checksum line, a blank line, or a `Name: value` armor header. A `Name: value` line counts only as a run directly after the BEGIN line, before the first blank line or radix-64 line. A code fence, another BEGIN line, or any other line stops the scan, and that BEGIN line is one item on its own. The receipt names the block's line range
- an OpenClaw raw entry is checked by value, so a routing `session_key` is imported
- From v0.18.0, provenance is checked with its keys. A routing `session_key` of the form `agent:<profile>:<channel>:<kind>:<tail>`, with an optional `:topic:<digits>` suffix, is still imported. The profile may contain digits, `-`, or `_`. The tail may be digits with a leading `+` or `-`, or a lowercase UUID. An uppercase profile and a Slack `C04...` tail are skipped. A secret name over an opaque value is skipped. In v0.17.0, provenance is read by value only, so a secret under a secret name is imported unless the value itself has a known key shape, such as a Stripe key
- placeholder password examples are skipped at import

## Verification Example

```bash
./.venv/bin/python -m alicebot_api recall --query "MCP tool surface" --limit 5
./.venv/bin/python -m alicebot_api resume --max-recent-changes 5 --max-open-loops 5
```

These commands read the continuity store that the three importers above
write. An agent that calls `alice_recall` over MCP does not see those records.

## Import for MCP Recall

To make a Markdown folder or a ChatGPT export recallable by an agent over MCP,
import it as a source:

```bash
alicebot vnext sources import-markdown <folder>
alicebot vnext sources import-chatgpt <file>
```

Both take `--domain` and `--sensitivity`. Like every `alicebot` command, they
need a Postgres `DATABASE_URL`, so point the MCP server at the same database
and user. `alice_recall` returns the text under `sources`, not under
`results`.

In v0.17.0, that sentence was "`alice_resume` does not
show it." That was false. A captured line that reads like a decision became
a candidate, and `alice_resume` showed it, including a token in the line.
The same release has three other capture gaps. Capture has no credential
check, so a note that holds a key is stored and `alice_recall` and
`alice-memory brief` return it. The folder import follows a symlink out of
the folder. Any JSON file is stored, including one that is not a chat export.

From v0.18.0, `alice_capture`, capture-text, capture-file, connectors, and
both imports refuse a source that carries
credential material and write nothing. A batch counts that source as
skipped, not failed. `alice_resume`, `alice_recent_decisions`, the
SessionStart brief, and `alice-memory brief` read only active memories, so
a captured candidate stays in review until the owner promotes it. The
folder import refuses a symlink and a non-regular file, and a single
markdown file is allowed. A JSON file with no conversations is refused and
nothing is written.

From v0.18.0, `alice-memory` imports a Markdown path or a ChatGPT export
into the SQLite store that MCP recall reads:

```bash
alice-memory import-markdown --from PATH
alice-memory import-chatgpt --from PATH
```

PATH for markdown is a file or a folder. Both commands take `--data-dir` or
`--db`, `--domain`, and `--sensitivity`. They write sources and chunks only,
not candidate memories. The line filter runs first. `capture_source` then
refuses text the filter could not isolate, and that file is skipped. A
flagged line or a private-key block is stored as
`[withheld: credential material]`. An unmatched BEGIN line is withheld
through the end of the file. `skipped_count` is how many files were skipped
for any reason. A withheld line does not increase it. A ChatGPT conversation
the backstop refuses is counted in `skipped_count` because that conversation
was not stored. `skipped_credentials` and `skipped_credential_items` count
every credential skip, whole files and withheld units, and do not include
the matched text. Items say `line N`, `lines N to M`,
`conversation X message N`, or `file K (name withheld)`.
From v0.19.0, a folder item names the file, as
`file 1 (week.md) line 2` or `file K (name withheld) line 2`, and a
ChatGPT conversation title stored as `withheld` is counted as
`conversation X title`. In v0.18.0 the item is only `line N` or
`lines N to M`, and that title is not counted. A token in a file
name skips that file and the receipt uses `file K (name withheld)`. A token
in the folder name refuses the import, writes nothing, and does not print
the path. Encoding is all or nothing: one markdown file that is not valid
UTF-8 refuses the folder, and the error names that file, or says withheld
when the file name is flagged. In v0.20.0, exit code 1 means the batch status is
`failed`, and it also covers path errors. Replay of the same file is
`duplicate`. OpenClaw stays on the loader scripts above. `alice-memory
doctor` counts stored sources the commit door still flags and prints their ids.
From v0.19.0, each line, message, and title is
checked with that verdict. A flagged line, message, or title is withheld
and named, and the rest of the file or conversation is imported. The
doctor flags a stored source that still contains a low-entropy
AKIA-shaped key. In v0.18.0 the line filter misses that key, capture
stores it inside the file, and the doctor does not flag it.
In v0.20.0, SQLite has no way to delete a source. Unreleased (on main, not in v0.20.0):
use `alice-memory sources list` and `alice-memory sources delete <id>` for SQLite.
On Postgres, soft-delete each listed
source with `DELETE /v0/vnext/sources/{id}`. The doctor scans up to 10,000 sources
on a workspace dashboard load and says when that scan stopped early. A
SQLite URL on `alicebot vnext sources import-markdown` or `import-chatgpt`
exits 2 with `sqlite_import_use_alice_memory` and names these two commands.
In v0.17.0, those commands do not exist, and a SQLite
URL on the `alicebot` imports is `invalid_request`.

Unreleased (on main, not in v0.20.0): each file of a Markdown import, and each
conversation of a ChatGPT import, is written as one unit, on SQLite and on
Postgres. When a write fails part way (a chunk write, for example), the file's
source, chunks, entity links and events are all rolled back. The receipt counts
the file in `failed_count`, one `source.import_failed` event is written after
the rollback, and the next file imports in the same transaction, so the batch
still commits once. That event names a Markdown file by its path under the
folder (`relative_path`) and a ChatGPT conversation by its index and id. A
second import of the fixed file then imports it in full. In v0.20.0 on SQLite
the failed file stayed live with the chunks written before the failure, a
second import of it reported `duplicate` and never completed it, and two
`source.import_failed` events were written for it. On Postgres, a failure at
the SQL level went differently in v0.20.0 (read from the code, not run against
a live server): the failed statement aborted the open transaction, the failure
logging that followed raised on it, and the batch stopped with an error. A
source that v0.20.0 left half built stays as it is. Nothing repairs it, and a
re-import of the same file still reports it as a duplicate.

## File Size Limit and Long Conversations

From v0.20.0, `alice-memory import-markdown`,
`alice-memory import-chatgpt`, `alicebot vnext sources import-markdown` and
`alicebot vnext sources import-chatgpt` refuse a file over a size limit
before they read any of it. The error is `import_file_too_large` on both
commands, with exit code 1:

```json
{"error":{"code":"import_file_too_large","message":"import source file is too large: conversations.json is 603979776 bytes (576.00 MiB), the limit is 536870912 bytes (512.00 MiB). Raise the limit with --max-file-mib, or import a smaller file."}}
```

The message names the file by its name, never its path, and withholds a name
the credential check flags. No source is written. `alice-memory` still creates
an empty database file if there was none.

- the limit is per file: 16 MiB for a Markdown file, 512 MiB for a ChatGPT
  export
- `--max-file-mib N` sets it, as a whole number of MiB of at least 1. There is
  no value that means no limit: give a number larger than the file
- a folder is not limited as a whole. The import still holds every selected
  file in memory at once, so a folder of many files just under the limit uses
  the sum of them
- the size is read from the open file, before the first byte is read, and the
  read stops one byte past the limit. A file that is growing, or that reports a
  size of zero and holds more, is refused too
- the text that is read is the same as before: the same UTF-8 check and the
  same newline handling
- a ChatGPT export is one JSON file (`conversations.json`), an array of every
  conversation. The importer parses it whole, because it cannot tell where one
  conversation ends without parsing the array, so the limit has to cover the
  whole export and not a conversation
- the loaders `load_markdown_payload`, `load_chatgpt_payload`,
  `load_openclaw_payload`, `import_markdown_source`, `import_chatgpt_source`
  and `import_openclaw_source` take `max_file_bytes` with the same defaults. A
  file over it raises `ImportFileTooLargeError`, which is a `ValueError` and
  not the importer's own validation error
- `alicebot vnext sources capture-file`, `alicebot vnext connectors
  browser-clipper capture --file` and `alicebot vnext agents ingest-output
  --file` read their file with the same read: once, up to 16 MiB, with the
  same `import_file_too_large` refusal and the same `--max-file-mib N`. The
  path is resolved first, so a link the caller types is followed, as it is for
  the importers. What the open refuses is a file that is a link by then, and
  anything that is not a regular file, such as a FIFO. A file that is not UTF-8
  is refused by its name. `VNextCaptureService.capture_file` takes
  `max_file_bytes`. In v0.19.2 these three read the whole file with no limit,
  followed a link put in place of the file after the path was resolved, and
  waited on a FIFO
- a ChatGPT conversation that cannot be read is refused by its position, and
  the process log gets one line for it, with the position, the code and the
  name of the error type. It holds no traceback and none of the error's text,
  which can quote the export. The traceback is at debug level. A
  `MemoryError` while a conversation is read is not a fact about that
  conversation, so it ends the import and is not counted as an unreadable
  conversation

Why these numbers. Measured on the `alice-memory import-chatgpt` command with
synthetic exports of 219 to 1,092 conversations of 60 messages each, peak
memory was about 80 MB for the process plus 5.7 MB for every MB of export (377
MB at 52 MB), and 8.7 MB for every MB when one character outside the Basic
Multilingual Plane, such as an emoji, is in the file, because Python then
stores the whole text at four bytes a character (535 MB at 52 MB). The import
took 1.3 seconds for every MB (1.9 with the emoji). At 512 MiB that is about 3
to 4.6 GB of memory and 11 to 16 minutes. A Markdown file of 16 MiB peaked at 335 MB and took about 15 to 23 seconds. The defaults come from those measurements. They do not come from a
survey of real exports, which was not possible here. An export over 512 MiB
needs `--max-file-mib` and that much memory.

From v0.20.0, one ChatGPT conversation that cannot be
turned into a transcript no longer fails the whole import. The receipt counts
it in `failed_count`, names it by its position in `errors`, as
`conversation 2 refused: conversation_unreadable`, and imports the others. The
position is the number `conversation_index` carries, and an item of the file's
array that is not an object is not counted. The status is `partial`, as it is
for a conversation whose capture fails. If every conversation is refused the
status is `failed` and the exit code is 1. A
refused conversation keeps its position, so the third conversation in the file
is still `conversation_index` 3 when the second is refused. A credential skip
in a conversation that is then refused is not reported. The event log gets a
`source.import_failed` event with `error_code` `conversation_unreadable` and
the position, and none of the conversation. The process log gets the one line described above,
and the traceback only at debug level.
A ChatGPT file nested too deeply for the JSON decoder is refused as a whole
with `ChatGPT export is nested too deeply to read`.

From v0.20.0, a conversation of any length imports. The
walk over a conversation's `mapping` is a loop and not a recursive call for
each message, and returns the same order as before. In v0.19.2 a conversation
whose messages form one chain of about 1,000 replies (990 imported and 995 did
not) overran the interpreter's recursion limit, and the command failed with
`alice_memory_failed` (`command_failed` on `alicebot`) and imported nothing,
from that conversation or from any other. The loaders for the continuity store
(`load_chatgpt_payload`, `import_chatgpt_source`) are not changed: they still
recurse over the nesting of a message's content and fail at about 480 levels,
which no export has and which neither command above reaches.

## Evaluation Harness

```bash
EVAL_USER_ID="$(./.venv/bin/python -c 'import uuid; print(uuid.uuid4())')"
EVAL_USER_EMAIL="phase9-eval-${EVAL_USER_ID}@example.com"
./scripts/run_phase9_eval.sh --user-id "${EVAL_USER_ID}" --user-email "${EVAL_USER_EMAIL}" --display-name "Phase9 Eval" --report-path eval/reports/phase9_eval_latest.json
```

Evidence paths:

- `eval/baselines/phase9_s37_baseline.json`
- `eval/reports/phase9_eval_latest.json`

## Scope Guard

No additional importer families are currently shipped.

## Replacement on re-import

Unreleased (on main, not in v0.20.0): `alice-memory import-markdown` accepts
`--supersede`, `--no-supersede`, `--dry-run` and `--allow-looser-classification`.
Replacement is off by default. In v0.20.0, importing an edited file adds another
live source. The default still does that, and now counts changed paths in the
receipt with a command to preview replacement. Unchanged default imports keep
their existing receipt shape.

```bash
alice-memory import-markdown --from notes --supersede --dry-run
alice-memory import-markdown --from notes --supersede
```

Replacement matches the resolved absolute path and the Markdown importer
connector within one user. The receipt prints file labels, not absolute paths.
Moving a folder, or restoring on another machine, starts new path identities.
Import once at the new location before later edits can replace those versions.
Manual captures, ChatGPT exports, connector sources and agent captures are
never retired by this command. Postgres import commands accept no new flags.

An omitted domain or sensitivity keeps the known path's most recent live label;
a first import uses `unknown`. With replacement enabled, every live match is
checked before capture: sensitivity may stay equal or rise, and a domain may
stay equal or move from `unknown` to a named domain. Other domain changes and
lower sensitivity need `--allow-looser-classification`. A project-scope change
is always refused. A refusal creates and retires nothing. The dry run uses the
same decision and rolls back its database writes; it leaves sleep proposals
unchanged.

A new version and retirement share one savepoint. An unchanged same-path
duplicate is kept, retiring any other stale versions of that path. If the new
text instead matches another path's live source, nothing is retired or added:
`kept` reports `matches_other_live_source`. New versions count as imports and
the batch is `ok`; an unchanged batch is `duplicate`. A batch that only retires
stale same-path duplicates is `ok`, with zero imports. Nonzero replacement
receipts include `superseded_count` and up to ten titles and ids, refusal reasons,
and every retained memory id in `memories_citing_replaced`, without a silent cap.

Replacement keeps old source text inside the live vault file, but exports omit
it. It closes and blanks source-backed open loops, rejects pending candidates,
removes source mention edges from live entity counts, scrubs unsupported linker
entities and removes sleep proposals. Entity observation windows only widen.
Every memory whose text was not redacted keeps its own text and provenance;
review the listed memory ids separately, including rejected, stale, superseded
and archived memories. A key-bound `alice_explain` remains unavailable
when its audit cites a retired source. The keyless owner can still read the audit.

A memory counts as citing a source when it has a provenance link to the source
or to one of its chunks, when its `source_event_ids` hold the source id, or when
its references name the source. References are read the way saved quotes read
them, so every spelling counts: an id in upper or lower case, with or without
hyphens, in braces, as `urn:uuid:<id>`, as `source:<id>` with or without a
`#chunk-0` suffix, as `alice://sources/<id>`, in a list, under `source_refs`,
`sources`, `source_id`, `source_ids` or `selected_source_ids` at any depth,
inside a reference that is JSON text, and in free text elsewhere in the memory's
metadata or value. An id inside a `quote` or
`conversation_excerpt` field names nothing, and a `memory:` reference names a
memory. Replacement, `sources delete` and `sources prune` all use this rule, so
a pending proposal that cites the source in any of those spellings is rejected
or scrubbed and is counted in the receipt and the preview. One spelling is not
found: an id written with decimal digits of another script.

There is no restore command for sources. Import the old text with `--supersede`
to make it live again. A pre-deletion export can conflict with rows in the same
vault in both restore modes; restore into a fresh vault instead. An export made
after replacement does not contain the replaced versions.

Unreleased (on main, not in v0.20.0): a refused replacement reports `refused`
when no file imported or duplicated, or `partial` when another file did.
Either receipt exits 1 when any file was refused. A batch with status `failed`
or a path error also exits 1. Successful `ok` and `duplicate` receipts exit 0;
a partial batch caused only by ordinary per-file failures keeps its existing
exit code 0 and reports the failures in `failed_count`.

Unreleased (on main, not in v0.20.0): a preview does not change database content,
but SQLite can create empty `memory.db-wal` and `memory.db-shm` coordination files
beside a cleanly closed vault. Sleep publication and source retirement share a
file lock; publication also rechecks source liveness under the database writer
lock, so an overlapping sleep run cannot put a retired excerpt back.

Unreleased (on main, not in v0.20.0): the keyless owner can still see a saved quote
and the replaced source id through a retained memory in review by id, context
packs and explain; keyed callers remain fenced. A bare `--supersede` on a path
with several live copies inherits the newest copy's labels and refuses if an
older copy is stricter, unless the owner explicitly allows looser classification.

## List, delete and prune SQLite sources

Unreleased (on main, not in v0.20.0): the owner CLI lists source ids and scrubs
one source or old replaced versions. No MCP tool, installer action or SQLite
HTTP route exposes deletion.

```bash
alice-memory sources list --all --limit 50
alice-memory sources list --query lantern --superseded
alice-memory sources delete SOURCE_ID
alice-memory sources delete SOURCE_ID --yes
alice-memory sources prune --superseded --older-than 30
alice-memory sources prune --superseded --older-than 30 --yes
```

List is read-only. It shows id, printed title, type, a relative path or external
id, chunk count, domain, sensitivity, capture date and live or replaced state.
`--all` includes live and replaced versions; `--superseded` shows replaced
versions only. `--limit` accepts 1 to 1000, default 50. A low chunk count can
help locate a partial import left by v0.20.0. Scrubbed rows are omitted.

Delete accepts either a live or replaced id. Unknown and already scrubbed ids
are refused. Prune selects only replaced versions; `--older-than DAYS` accepts
an integer from 0 through 9223372036854775807 days since replacement. Neither destructive command writes without
`--yes`: it prints the targets and counts and exits 2. With `--yes`, the command
selects its targets again under the writer lock and applies one transaction.

Scrub first removes the source's sleep proposals, then clears source labels and
metadata, overwrites chunks and provenance quotes, redacts pending or rejected
candidates and their revisions, blanks mention edges, updates entity counts,
scrubs unsupported linker entities, and closes and blanks source-backed loops.
A candidate the existing redaction path cannot cover refuses the database
transaction and names its id. A later failure may remove sleep proposals; they
can regenerate with `alice-memory sleep`. Every citing memory that was not
redacted keeps its text and is listed in both preview and receipt without a cap,
including active, accepted, private_only, stale, superseded and archived rows. Use `alice_memory_manage` with action `forget` to remove a
listed memory from recall, or the owner's memory redaction command to overwrite
its own text. Source deletion alone does neither to committed memories.

Each scrub enables SQLite secure deletion to zero freed space, and each delete
or prune transaction merges both full-text indexes to remove obsolete postings.
Append-only source events retain old titles and hashes, import events retain
folder paths, and source rows retain hash columns. Postgres source deletion and
review archive remain soft deletes: their source text and chunks are not
scrubbed by those routes.

Removed text can remain in the vault file's unused space and write-ahead log
until the file is rebuilt. To remove that leftover text, stop every program
that uses the vault, including MCP servers and the session hook, replace
`<data-dir>` with the vault directory, and run:

```bash
sqlite3 <data-dir>/memory.db "VACUUM; PRAGMA wal_checkpoint(TRUNCATE);"
```

Earlier backups and copies still hold the text. This command does not remove
text deliberately retained in audit events or unredacted memories.

A corrupt `sleep_proposals.jsonl` refuses deletion. Stop every program using the
vault, move that file aside, retry deletion, then run `alice-memory sleep` to
regenerate proposals. Do not restore the moved file after deleting sources.
A candidate redaction failure rolls back database changes and names the affected
candidates; sleep proposals may already have been removed and can regenerate.
Invalid source UUIDs and out-of-range ages exit 1. CLI syntax errors exit 2;
a valid deletion preview also exits 2 and includes `requires_yes: true`.

Prune reduces retained source text in the full-text index. It offers no restore
verb. Import old text again to make it a new live source. Exports omit retired
sources; restoring an older export over changed ids can fail in either restore
mode, so restore into a fresh vault. The doctor reports a nonzero replaced-source
count and gives the SQLite source deletion command for flagged source rows.
