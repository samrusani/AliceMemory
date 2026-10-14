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

The 210 questions written for the two company sizes are a development set written with the company. They check that the harness works. The report of that set says so. Published results come only from question sets run through `showcase-heldout`. `showcase-report --heldout` leaves the development-set sentence out.

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

## Arms

- `alice`: one key per team (sales, support, product, leadership). The search uses that key.
- `grep`: the capped grep the bench already has, over the whole snapshot.
- `notes`: `list`, `read`, and `search` on the plain note folder. A path that leaves the folder is refused. The search covers the whole folder.
- `fake-separated`: a keyword stand-in with one scope per team. The scopes are filled from the policy in the questions file. A result from this arm is not mem0.
- `mem0-separated`: the open-source library, one scope per team, optional. The harness fills each scope from that same policy. mem0 enforced nothing. Install with `pip install -e '.[bench]'`. It is not a product dependency. Each search reopens the saved configuration.
- `mem0-shared`: the same library with one scope that holds every note. Nothing separated the teams. This is what an operator gets without building their own separation.
- `none`: no search tool.

Grep and the note-folder arm search the whole shared folder. That is what a shared note vault gives.

The questions file records who may know each planted fact. HR pay is for leadership only. A pricing draft is for sales and leadership. Any other note is for every team. Leak scores use that table. They do not ask Alice who may read a note.

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
python scripts/alice_bench.py showcase-heldout --notes DIR --questions FILE
```

`showcase-record` keeps the first answer for a question, an arm and a team, so a batch can stop and resume. `showcase-heldout` takes the notes folder and the questions file it is given and does not look for any other question set. Each size has 15 questions for each of the seven tasks, 210 in all. Those 210 questions are the development set.

## A 20 note run on a machine with Ollama

This builds 20 notes from the company, turns embeddings on, builds the real mem0 arm and the Alice arm, and runs one search on each. It refuses an endpoint that is not loopback. Do not download a model for a test run.

```
python scripts/alice_bench.py showcase-live --run-dir RUN --notes 20 --endpoint http://127.0.0.1:11434
```
