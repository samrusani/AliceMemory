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
`conversation X message N`, or `file K (name withheld)`. A token in a file
name skips that file and the receipt uses `file K (name withheld)`. A token
in the folder name refuses the import, writes nothing, and does not print
the path. Encoding is all or nothing: one markdown file that is not valid
UTF-8 refuses the folder, and the error names that file, or says withheld
when the file name is flagged. Exit code 1 means the batch status is
`failed`, and it also covers path errors. Replay of the same file is
`duplicate`. OpenClaw stays on the loader scripts above. `alice-memory
doctor` counts stored sources the floor still flags and prints their ids.
SQLite has no way to delete a source yet. On Postgres, delete each listed
source with `DELETE /v0/vnext/sources/{id}`. The doctor scans up to 10,000 sources
on a workspace dashboard load and says when that scan stopped early. A
SQLite URL on `alicebot vnext sources import-markdown` or `import-chatgpt`
exits 2 with `sqlite_import_use_alice_memory` and names these two commands.
In v0.17.0, those commands do not exist, and a SQLite
URL on the `alicebot` imports is `invalid_request`.

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
