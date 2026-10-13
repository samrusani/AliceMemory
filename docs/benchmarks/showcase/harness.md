# Showcase bench

The harness extends `scripts/alice_bench.py`. It builds one company, serves
each arm as a search tool, writes the questions, and writes a grading packet
that does not name the arm. Answering and grading happen outside this process.
Nothing in the harness calls a paid model API.

## Models

Both settings are local Ollama addresses. A host that is not loopback is refused.

- Chat, for mem0 extraction: `qwen2.5:7b` by default. `llama3.2` and `gemma4` are the other accepted models. `--chat-model` selects one, and the run manifest records it.
- Embeddings, for Alice and for mem0: `nomic-embed-text:v1.5`. Alice reads it as `ALICE_EMBEDDINGS_BASE_URL` set to the Ollama `/v1` prefix and `ALICE_EMBEDDINGS_MODEL` set to that model. Ollama serves an OpenAI-compatible `/v1/embeddings`. `--embed-model` and `--endpoint` are the settings.

A test run uses `--extractor fake` and does not call Ollama.

## Arms

- `alice`: one key per team (sales, support, product, leadership). The search uses that key's profile ceiling.
- `grep`: the capped grep the bench already has.
- `notes`: `list`, `read`, and `search` on the plain note folder. A path that leaves the folder is refused.
- `mem0`: the open-source library, one scope per team, optional. Install with `pip install -e '.[bench]'`. It is not a product dependency.
- `none`: no search tool.

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

`showcase-record` keeps the first answer for an id, so a batch can stop and resume. `showcase-heldout` takes the notes folder and the questions file it is given and does not look for any other question set. Each size has 15 questions for each of the seven tasks, 210 in all.
