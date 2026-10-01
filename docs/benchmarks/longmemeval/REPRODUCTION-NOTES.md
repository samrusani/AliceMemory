# LongMemEval harness 1.1: flags and reproduction notes

This page describes the run-level choices of harness 1.1, what each flag does,
and how to rerun the older configurations. It publishes no new score and runs
nothing. The numbers in [README.md](README.md) and the evidence files in this
folder were produced by harness 1.0 and are not edited here.

## Why 1.1 exists

LongMemEval names the id of every evidence session `answer_...`. No filler
session id starts that way: filler ids start with `sharegpt_` or `ultrachat_`,
or are plain hex. Harness 1.0 copied the dataset's session id into the stored
source title, into the first paragraph of each session's text (so into chunk 0),
into the source metadata, and into the header printed above every excerpt the
reader model sees. A reader could therefore tell which sessions were the
evidence. How much that changed any answer has not been measured.

Harness 1.1 hides the ids by default and records, on every run and every row,
the four choices that decide what a score measures. Before 1.1 none of the four
was recorded: three were fixed in the code and the excerpt source was read from
an environment variable that never reached the fingerprint.

## The four run-level choices

| Choice | Flag | Values (default first) | What it decides |
|---|---|---|---|
| Session labels | `--raw-session-labels` | keyed-hash labels, or raw ids with the flag | Whether the dataset's session ids reach the store and the reader. |
| Excerpt source | `--excerpt-source` or `ALICE_LME_EXCERPT_SOURCE` | `store_chunks`, `pack_excerpts` | Whether the harness reads every chunk of a retrieved source from the store, or only the excerpt the retrieval call returns. |
| Promotion | `--promotion-mode` | `all_candidates`, `sources_only` | Whether capture's candidate memories are all force-accepted, or left alone as after a real import. |
| Surface | `--surface` | `context_pack`, `recall` | Which retrieval call feeds the reader. |

The defaults reproduce the 1.0 pipeline except for the session labels, which are
hidden. Every value is in the fingerprint, in the per-row fields
`session_label_mode`, `session_label_key_id`, `excerpt_source`, `promotion_mode`,
`surface` and `harness_version`, and in the store-reuse marker.

## Session labels

The label of a session is `S` followed by the first 10 hex characters of
`HMAC-SHA256(key, question_id + NUL + session_id)`. The key is a constant
experiment key with the key id `lme-anon-v1`, recorded in the fingerprint. It is
not a secret. It keeps the label from being computable by the reader and from
carrying the dataset's prefixes. It is constant on purpose: a session gets the
same label in every arm and every run, so retrieved session labels line up
between arms, and the mapping can be recomputed offline. `compare_runs.py` joins
on `question_id` and needs nothing from the labels.

The constant key does not make every rendered context repeatable. On the prose
context pack the same store content gives the same `context_sha256` on every
run. The JSON pack and the recall tool result carry per-store source and memory
ids, and the recall result also carries `captured_at`, which is the ingest wall
clock, so on those two `context_sha256` is different on every run even with the
same config and the same labels. Compare those rows by answers and by
`retrieval.provenance.source_session_ids`, not by the hash.

- A label is used everywhere a session id used to go: the first paragraph of the
  session text, the source title, the external id, the source metadata, the
  header above each excerpt (prose pack), the `session_id` field of each excerpt
  record (JSON pack), the tool result of the recall surface, and
  `retrieval.provenance.source_session_ids` in the checkpoint rows.
- Dates and excerpt order are untouched. Dates are official benchmark input.
- Two different session ids that map to the same label inside one question stop
  the run before the first question starts. Nothing is merged silently.
- Raw ids exist in memory and in one file, written next to the checkpoint:
  `<checkpoint stem>.session-labels.jsonl`, one line per question, with the
  label-to-id mapping. It is never loaded into a store. Raw-label runs write no
  sidecar, because the labels are the ids.
- A store ingested under one label mode is not reused under the other. A store
  that holds raw ids is refused when read as an anonymised run, instead of
  showing raw ids to the reader.
- `coverage_probe.py` maps the dataset's `answer_session_ids` through the same
  function before comparing them with the retrieved sessions. It takes
  `--raw-session-labels` too, and its rows record the mode. Its
  `missed_session_ids` are labels in the run's mode.

## Reproducing the older configurations

Use raw labels and keep the other defaults:

```bash
python scripts/run_longmemeval.py --variant s --workers 3 --resume \
       --cot --max-items 16 --context-char-budget 24000 \
       --raw-session-labels --checkpoint <new checkpoint path>
```

with the same environment variables as the reproduction block in
[README.md](README.md). Notes:

- Use a new checkpoint. A 1.0 checkpoint is refused on resume, because its rows
  carry a different harness version and none of the new fields.
- The fingerprint digest of a 1.1 run never equals the digest of a 1.0 run, in
  raw mode too: the fingerprint gained fields and `harness_version` is part of
  it. The digests listed for the published runs stay valid for those files only.
  Compare per-question rows, not digests.
- The excerpt source of every published number was `store_chunks`, and every
  candidate memory was force-accepted (`all_candidates`) on the `context_pack`
  surface. Those are the defaults, so the command above does not name them.
- Whether a raw-label rerun on the new code reproduces the old per-question
  results is not verified here.

## Promotion mode

Capture extracts candidate memories from every ingested session. In the product
a candidate is not searchable until someone accepts it, and a real import gives a
user sources, not accepted memories.

- `all_candidates` (default) force-accepts every candidate straight after
  capture, as every earlier run did. It models a store whose owner reviewed and
  accepted everything extracted.
- `sources_only` promotes nothing. The store holds sources, chunks and
  unreviewed candidates, and retrieval can only return source excerpts. Memory
  lines are absent from the context. It cannot be combined with
  `--accept-rollups`, which groups promoted memories.

The two modes measure different things and their numbers must not be quoted as
the same metric.

## Surface

- `context_pack` (default) compiles a context pack with `compile_context_pack`
  and renders it into the reader's history slot under the character budget. A
  default MCP install does not expose that tool (`alice_context_pack` needs
  `ALICE_MCP_FULL_TOOLS=1`).
- `recall` calls the shipped `alice_recall` MCP tool through `call_mcp_tool`, the
  entry point the MCP server uses, with the tool's own call sequence and fences,
  and hands the reader `serialize_mcp_tool_result(result)`, the text the server
  puts in the model's tool message. The harness adds nothing and renders
  nothing.

What differs on the recall surface, all of it as the tool behaves:

- The limit is the run's `--max-items`. Its default, 8, equals the tool's default
  (a test pins that they stay equal). The replication used 16, so
  `--max-items 16` is a non-default recall call; the fingerprint records it. The
  tool accepts 1 to 50, and the run refuses any other value before the first
  question is ingested.
- There is no reference time, no coverage or aggregation gate and no instance
  diversity pass: the tool has none.
- No context character budget applies. The fingerprint records
  `context_char_budget` as `null` and `pack_format` as `recall_result`.
- It needs `--excerpt-source pack_excerpts`, which is chosen when nothing else is
  requested; `store_chunks` is refused. `--pack-format json` is refused.
- Source entries carry `captured_at`, which is the benchmark's ingest time and not
  the session date. The session date is in the source title.
- `ALICE_AGENT_API_KEY` must be unset. With it set the tool authenticates against
  issued keys the benchmark store does not have.
- `debug` is switched on for the call only to read the vector stage for the run
  statistics. The one block it adds is removed before the reader sees the text,
  and a test compares the result with a call made without `debug`.

## Resume and mixing

`--resume` compares the fingerprint digest and, separately, each of the per-row
fields above. A completed row that differs in any of them, or lacks one, makes
the run refuse and name the field. Use a new checkpoint for a different
configuration.

## Example arms

These are configurations, not results. Nothing here has been run.

| Arm | Flags |
|---|---|
| Old pipeline, ids hidden | defaults (`store_chunks`, `all_candidates`, `context_pack`) |
| Old pipeline, ids shown | `--raw-session-labels` |
| Pack excerpts only | `--excerpt-source pack_excerpts` |
| Sources only, pack excerpts | `--excerpt-source pack_excerpts --promotion-mode sources_only` |
| Recall tool | `--surface recall` (add `--promotion-mode sources_only` for a store with no accepted memories) |

Spend is the operator's call: every scored arm calls a chat model, a judge and,
when embeddings are configured, an embedding model. `--dry-run` calls no chat
model and no judge. It ingests and retrieves with the same `ALICE_EMBEDDINGS_*`
variables as a scored arm, so with embeddings configured it still calls the
embedding model, for ingest and for the query embedding of each retrieval.
Unset those variables for a dry run that calls nothing.

## Checks

`make test-longmemeval` runs the harness tests without a model or a network. The
leak guard, `eval/longmemeval/test_session_label_leak_guard.py`, ingests a fixture
whose evidence ids start with `answer_` and whose filler ids start with
`sharegpt_`, `ultrachat_` and plain hex, retrieves in both excerpt sources, both
pack formats and the recall surface, and fails if a raw id or one of those
prefixes appears in any stored table, the reader's context or a checkpoint row.
