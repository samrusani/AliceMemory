# Showcase bench

The harness extends `scripts/alice_bench.py`. It builds one company, serves
each arm as a search tool, writes the questions, and writes a grading packet
that does not name the arm. Answering and grading happen outside this process.
Nothing in the harness calls a paid model API.

## Models

Both settings are local Ollama addresses. A host that is not loopback is refused.

- Chat, for mem0 extraction: `qwen2.5:7b` by default. `llama3.2` and `gemma4` are the other accepted models. `--chat-model` selects one, and the run manifest records it.
- Embeddings, for Alice and for mem0: `nomic-embed-text:v1.5`. Alice reads it as `ALICE_EMBEDDINGS_BASE_URL` set to the Ollama `/v1` prefix and `ALICE_EMBEDDINGS_MODEL` set to that model. Ollama serves an OpenAI-compatible `/v1/embeddings`. `--embed-model` and `--endpoint` are the settings. `--embeddings on` applies those settings during import and during Alice recall, and the import posts the note text to that endpoint. The default is `off`, so a test run does not call Ollama.

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

The database is the disposable local PostgreSQL the tower names, in `--database-url`, `ALICE_SHOWCASE_POSTGRES_URL`, `DATABASE_URL`, or `DATABASE_ADMIN_URL` when the others are unset. The command does not pull a Docker image. When that database is not named, the report says the path was not run and it does not print a leak rate for it.

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

## A 20 note run on a machine with Ollama

`showcase-live` refuses an endpoint that is not loopback. Phase 0 does not run the mem0 arms, so the command stops after that check. Do not download a model for a test run. The mem0 search code stays in `scripts/showcase_bench.py`.

```
python scripts/alice_bench.py showcase-live --run-dir RUN --notes 20 --endpoint http://127.0.0.1:11434
```
