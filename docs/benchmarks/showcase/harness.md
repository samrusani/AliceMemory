# Showcase bench

The harness extends `scripts/alice_bench.py`. It builds one company, serves
each arm as a search tool, writes the questions, and writes a grading packet
that does not name the arm. Answering and grading happen outside this process.
Nothing in the harness calls a paid model API.

## Models

Both settings are local Ollama addresses. A host that is not loopback is refused.

- Chat, for mem0 extraction: `qwen2.5:7b` by default. `llama3.2` and `gemma4` are the other accepted models. `--chat-model` selects one, and the run manifest records it.
- Embeddings, for Alice and for mem0: `nomic-embed-text:v1.5`. Alice reads it as `ALICE_EMBEDDINGS_BASE_URL` set to the Ollama `/v1` prefix and `ALICE_EMBEDDINGS_MODEL` set to that model. Ollama serves an OpenAI-compatible `/v1/embeddings`. `--embed-model` and `--endpoint` are the settings. The default of `--embeddings` is `off`, so a test run does not call Ollama.
- What `--embeddings on` does depends on `--search-quality`. For any value other than `vectors` it applies the two settings while Alice recalls and embeds nothing at build time. A Markdown import creates no memory, and without the source opt-in it stores no vector for a source or a passage, so such a run answers from full-text search over the imported notes alone. That holds for the Phase 0 baseline and the Phase 1 comparison for v0.21.0 as well. Their builds posted the note text to the endpoint and threw the vectors away, which changed nothing about an answer. The harness no longer sends that text.
- For `--search-quality vectors`, `--embeddings on` is required and the build embeds the chunks through the product. [What a vectors run embeds](#what-a-vectors-run-embeds) says what, and how.

A test run uses `--extractor fake` and does not call Ollama.

## Questions

Every questions file states its kind: `demonstration`, `development` or `hidden`.

The 210 questions written for the two company sizes are the development set. They check that the harness works. The report of that set says so. The demonstration set is the small set in `scripts/showcase_demo.py`, and the demo uses only that set. A hidden set is run through `showcase-heldout`, which imports the notes folder it is given into `--run-dir` with each note's labels, the four team keys, the capture report and a snapshot. It generates nothing, and it does not look for any other question set. The same embedding and endpoint flags as `showcase-build` apply. The report refuses to combine kinds in one table, refuses to combine a held-out corpus with the generated company, and refuses to compare arms across models. `showcase-report --heldout` leaves the development-set sentence out. A held-out grade row leaves that sentence out on its own.

Phase 0 builds and reports the 1,000-note company only. A held-out folder is reported under its own corpus, for example `corpus heldout (N notes)`, with N counted from that folder, and never under the generator's size. `showcase-report --run-dir RUN` refuses a held-out grade row whose `notes` differ from the count the import recorded in that run's `showcase.json`. Without `--run-dir` the report uses the count on the grade row. The 10,000-note questions stay in the development set. The 10,000-note build waits until the 1,000-note results separate the arms.

## Team keys

Each team gets one key. The profile, the project binding, what the key read, and whether `memory.commit` was allowed are below. The table was made by importing six labeled notes and calling recall and `memory.commit` with each key.

| team | profile | project binding | public | internal | private | confidential | legal | financial | memory.commit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sales | trusted_local_agent | none | yes | yes | yes | no | yes | yes | allowed |
| support | memory_proposal_agent | none | yes | yes | yes | no | no | no | allowed |
| product | project_scoped_agent | none | yes | yes | yes | no | no | no | blocked |
| leadership | admin_agent | none | yes | yes | yes | yes | yes | yes | allowed |

Support and product read the same notes in this run. Both keys were created with no project binding. The vault has 0 project rows, so there is no project to bind the product key to. The permission model cannot give product a different read set without that binding. The harness does not add a filter of its own.
The product key memory.commit decision was blocked. The reasons were project_scope_required. The harness does not create a project or change the profile to hide that.

Support can commit a memory, so a support agent can record a decision, and it can read a private note. It does not read a legal note or a financial note. HR notes are stored as domain legal and sensitivity confidential, and pricing drafts are stored as domain financial and sensitivity private, so those notes stay out of the support key. Sales reads legal and financial notes through private, and not a confidential note. Leadership reads every one of these notes. Product reads the same notes as support, and its key cannot commit a memory until it has a project scope. This vault has no project row, and the imported notes are not on a project.

## Models on an answer

Each answer records the answering model as well as the arm, the team and the question. A resumed batch skips a row that already has that same four. The local chat model is the `models.chat` setting in the run manifest, and the endpoint has to be loopback. The harness does not download a model. The report compares arms inside one model. mem0 arms stay in the code and are not run or reported in Phase 0.

## Capture

`showcase-build` writes how many notes were discovered, captured, failed and indexed. Each failed path is listed. A failed import is not silent: the command stops and the list is in `capture.json`.

## What an answer records

`showcase-record` stores the evidence the arm returned (ids and passages), whether the answer cites that evidence, the tool call count, and the tokens and time the agent reports. An answer that cites nothing returned is marked unsupported.

## Report

For each arm, task, team and model the report gives the supported complete answer rate with a 95 percent interval, the leak rate, and tokens and time, plus the total across teams. It also gives the variance between questions, the sample size for a 10-point paired difference (a normal approximation, 95 percent two-sided, 80 percent power, variance 0.5), and a failure count for each wrong answer: the needed note was not captured, was not returned, or was returned and misused.

## Harness privileges

The report lists three calls a user of the same store cannot make, and an Alice number says that it depends on them.

- Team keys are created on the SQLite vault inside the harness. The shipped SQLite product cannot create a key (`alicebot agent keys create` requires Postgres) and a configured key is refused.
- Identities are resolved in process and handed to the MCP session.
- `alice_recall` is called with `sensitivity_allowed` taken from the team profile.

## Keyed permission path

`showcase-keyed-permission` runs the permission task for every team once against a PostgreSQL vault. Each note is imported with `alicebot vnext sources capture-file` and that note's own domain and sensitivity. Keys are created with `alicebot agent keys create`. Questions are asked with `POST /v0/vnext/context-packs` and the key in the `Authorization` header. That request does not send `sensitivity_allowed`. The report prints that leak rate beside the SQLite harness leak rate.

The database is the disposable local PostgreSQL the operator names, in `--database-url`, `ALICE_SHOWCASE_POSTGRES_URL`, `DATABASE_URL`, or `DATABASE_ADMIN_URL` when the others are unset. The command does not pull a Docker image. When that database is not named, the report says the path was not run and it does not print a leak rate for it.

## Arms

- `alice`: one key per team (sales, support, product, leadership). The search uses that key.
- `grep`: the capped grep the bench already has, over the whole snapshot.
- `notes`: `list`, `read`, and `search` on the plain note folder. A path that leaves the folder is refused. The search covers the whole folder.
- `fake-separated`: a keyword stand-in with one scope per team. The scopes are filled from the policy in the questions file. A result from this arm is not mem0.
- `mem0-separated`: the open-source library, one scope per team, optional. The harness fills each scope from that same policy. mem0 enforced nothing. Install with `pip install -e '.[bench]'`. It is not a product dependency. Each search reopens the saved configuration. Phase 0 does not run or report this arm.
- `mem0-shared`: the same library with one scope that holds every note. Nothing separated the teams. This is what an operator gets without building their own separation. Phase 0 does not run or report this arm.
- `none`: no search tool.

Grep and the note-folder arm search the whole shared folder. That is what a shared note vault gives.

The questions file records who may know each planted fact. HR pay is for leadership only. A pricing draft is for sales and leadership. Any other note is for every team. Leak scores use that table. They do not ask Alice who may read a note. A permission question asked by a team in that fact's list expects the value. A question asked by any other team expects not known.

## Demo setup

The search arms do not create a project. A product key with no project cannot commit a memory. The demo that records decisions calls `prepare_demo_product` in `scripts/showcase_demo.py`. That creates one product project and binds the product key to it. The search benchmark does not call it.

On main, not in v0.21.1, `python scripts/showcase_walkthrough.py` writes the 1,000-note company and prints a scripted meeting, from the stored tools and with no model call. Setup records the Keel price and the Larkspur account through `alice_memory_commit` and `alice_memory_correct` with each team's key, and does not extract decisions from notes. The meeting recalls the Larkspur contract for sales and for support, asks what changed for the account and for the price, and shows that support is not given the HR pay or the draft price. Before it prints that refusal it checks that leadership can read the HR note and that sales can read the draft price, so the refusal is not a search that found nothing. The last part is a benchmark summary. A number in it is read from the file named by `--grades`, the file `showcase-report --grades` reads, and with no such file the summary has no number. The walkthrough measures nothing. The results are on [the Phase 0 baseline](phase0-baseline.md) and [the Phase 1 results for v0.21.0](phase1-v0210-results.md).

For a recall question, `recall_note_retrieved` reports whether the search text contains each needed note. `showcase-record --retrieved yes` stores that on the answer.

## Commands

```
python scripts/generate_showcase_company.py --size 1000 --out DIR
python scripts/alice_bench.py showcase-build --run-dir RUN --size 1000
python scripts/alice_bench.py showcase-questions --size 1000 --out questions.json
python scripts/alice_bench.py showcase-search --run-dir RUN --arm alice --team support --query "..."
python scripts/alice_bench.py showcase-record --run-dir RUN --id ID --arm alice --team support --answer "..."
python scripts/alice_bench.py showcase-packet --questions questions.json --answers answers.json --out packet.json --key-out key.json
python scripts/alice_bench.py showcase-report --grades grades.json
python scripts/alice_bench.py showcase-heldout --notes DIR --questions FILE --run-dir RUN
python scripts/alice_bench.py showcase-keyed-permission --questions questions.json
```

`showcase-record` keeps the first answer for a question, an arm, a team and a model, so a batch can stop and resume. `showcase-heldout` imports the notes folder it is given and does not look for any other question set. Search, record and packet then use that run directory unchanged. Each size has 15 questions for each of the seven tasks, 210 in all. Those 210 questions are the development set. Phase 0 builds and reports the 1,000-note company only. A held-out report names `corpus heldout (N notes)` and does not use the generator's size.

A question with `abstain: true` is supported complete when the answer is correct and is not a leak. Any other question is supported complete only when it is correct and cites returned evidence. A citation is a returned id, or a word-aligned span of 8 or more characters that appears verbatim in a returned passage.

`showcase-build`, `showcase-heldout` and `showcase-search` take `--search-quality` of `off`, `passage`, `vectors` or `on`. The build records that value in `showcase.json` as `search_quality`, `unset` when the flag is absent. From v0.21.0, an unset run is passage mode, because the vault is SQLite, and it was document mode in v0.20.2. `showcase.json` therefore also records `ranking_mode`, `document`, `passage` or `vectors`, which the setting resolves to on that vault. A run of `vectors` is recorded as `vectors` and never as `passage`, and a checkout that would rank it as another mode is refused. A search whose quality differs from the recorded `search_quality` is refused. The Alice arm's tool text leaves out the echoed query. `showcase-search --budget N` allows 1 to 10 searches in that state folder, the default is 3, and the folder keeps the number it recorded. A failed search exits with the bench's search error code. A spent budget prints one line and exits with the bench's budget code. Any other refusal prints one line to stderr and exits nonzero.

## What a vectors run embeds

`--search-quality vectors` ranks the passages of a source with the vectors of its chunks as well as its words. Those vectors exist only if the build made them, and a recall that finds none answers as passage mode does. The harness therefore builds, checks and records them, and it refuses to go on without them.

**What is embedded.** Every chunk of every imported note, through the product. The text sent for a chunk is the note title, the headings above the chunk and the chunk, behind the model's document prefix (`search_document: ` for `nomic-embed-text`). A note's front matter is part of its chunk. The product skips a chunk that the credential check flags, a chunk of links and labels, and a chunk too short to read, and counts each by reason in `chunk_vectors.skipped`. Nothing else is skipped, and a build that finds a skip for any other reason, or any chunk pending or holding a vector of another model, endpoint or rule, fails and writes no `showcase.json`. The note text goes only to the endpoint named by `--endpoint`, which has to be on this machine, and the product checks the connected peer as well.

**How.** `showcase-build` and `showcase-heldout` take `--search-quality vectors --embeddings on`. Each note is imported by the product's `import-markdown` with `ALICE_SOURCE_EMBEDDINGS=on`, `ALICE_EMBEDDINGS_BASE_URL` and `ALICE_EMBEDDINGS_MODEL` set for that call, so the product embeds the chunks of the note after the import commits. When the last note is in, the harness runs the product's `alice-memory reindex-embeddings --sources` until nothing is pending, at most five times and stopping when a pass embeds nothing. It then counts the vault with the product's own census, as `alice-memory doctor` does. The command carries those variables and no other `ALICE` variable, so the vault cap, the cache size and the input cap are the product's defaults, and the values of the parent come back when it ends.

**What is recorded.** `showcase.json` gains a `chunk_vectors` block: the `model`, its `model_digest` as the loopback Ollama reports it (`/api/tags`), the `dimensions` of the stored vectors, `chunks_embedded`, `pending` and `stale` (both 0), `skipped` by reason with `skipped_total`, the `signature` fields the product stamps on a vector (provider, model, endpoint fingerprint, and the signature, header and prefix versions), and `backfill_passes`, the number of backfill passes the build took. `embedding_env` holds the endpoint and the model. `ranking_mode` is `vectors`.

**What every search checks.** Every search of the Alice arm in a vectors run carries the same variables, and they are allowed through the environment scrub for that arm of this quality only. The other arms (grep, the note folder, the keyword stand-in) read no vector. They carry the search quality and nothing more, and they are never refused for the state of the vectors or because the endpoint is down. Before the Alice arm spends one of its searches it checks the vault and refuses, with the bench's refusal exit code and one line on stderr, unless the process carries the variables, nothing is pending or stale, the vault stores exactly the `chunks_embedded` vectors the build recorded at the recorded width and signature, and the digest the endpoint reports for the model is the recorded one. The count of the vault reads every live chunk, so it is made once and kept in `vector_census.json` in the run folder. It is kept for a key made of the product's vector stamp, the number of vector, chunk and source rows with their highest row numbers, the signature in force and the recorded digest, and it is made again when the key changes: a vector deleted, overwritten or added, a note imported or retired, or another model. What the build recorded is compared on every search, and the digest is asked of the endpoint on every search. A search that asks for another quality than the run recorded is refused as before.

**What every search records.** The recall carries `debug` so that the harness can read the status of the vector stage. The harness takes the trace out again, so the text an answering agent reads is the text a recall without the trace returns. The status is appended to `vector_status.jsonl` in the state folder of the arm and team, one line for each search: its number, `status` (`enabled`, or the reason the stage did not run, such as `skipped: query embedding failed`), `used`, the counts the stage reports (`readable_chunks`, `stale_chunks`, `discovered_in_head`, `reordered_slots`, `filler_picks`) and the seconds the recall took. A search whose stage did not run is still answered, as passage mode answers, and it is counted as one that did not use vectors. `showcase-report --run-dir RUN` ends with `vector searches N, used vectors M` and the count of each status for a vectors run, and prints nothing more for a run of any other quality.

**The other commands.** `alice_bench.py build` takes `--embeddings`, `--endpoint` and `--embed-model` and builds a vectors vault the same way, recording `embedding_env` and `chunk_vectors` in its manifest. `recall`, `batch`, `search --arm alice` and `fingerprint` then take `--search-quality vectors`, check the vault as above, and record the status of each call (`batch` in `vector_stage` of its outputs, `search` in the state folder). They refuse a vault built for another quality, and a command of another quality refuses a vault built for vectors. `--embeddings on` with any other quality is refused there.

```
python scripts/alice_bench.py showcase-build --run-dir RUN --size 1000 --search-quality vectors --embeddings on
python scripts/alice_bench.py showcase-search --run-dir RUN --arm alice --team support --query "..." --search-quality vectors
```

## A 20 note run on a machine with Ollama

`showcase-live` refuses an endpoint that is not loopback. Phase 0 does not run the mem0 arms, so the command stops after that check. Do not download a model for a test run. The mem0 search code stays in `scripts/showcase_bench.py`.

```
python scripts/alice_bench.py showcase-live --run-dir RUN --notes 20 --endpoint http://127.0.0.1:11434
```
