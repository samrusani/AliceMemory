# Agent-answer check: method note

Unreleased (on main, not in v0.20.0): this page describes a measurement harness that exists on main only. v0.20.0 has no harness for it and no agent-answer number. The first release that states one will carry the numbers and a receipt in this folder, and until then nothing here is a claim about how well Alice searches.

## What it measures

A person imports a folder of notes into Alice and an agent tries to answer questions from it, with a few searches. The check asks how often the answer was in what the agent read, and how that compares with an agent that has `grep` over the same files. Alice's own answer to a question is never trusted: every question carries the facts a correct answer needs, written as verbatim strings that were checked against the files.

There are two tiers, because they cost different amounts and prove different things.

| Tier | What it is | Cost | What it can say |
| --- | --- | --- | --- |
| 1 | Run `alice_recall` for every question and look for the anchors in what came back | Seconds, no model, no network | Whether a change moved the facts into the agent's view. Builders can run it as often as they like on the dev set. It decides whether a slice of work may ship. |
| 2 | Answering agents with three searches, a judge that has the gold answers, and a grep arm over the same files | Hours of subscription runs, no paid API | How many questions an agent answers right. It makes the claim a release states. |

Tier 1 is necessary and not sufficient. In the audit that started this work, a check that only asked whether the right file came back said 36 of 40 where a judge said 16 of 40. An anchor in the output is stricter than "the right file", and still weaker than a correct answer.

## What Tier 1 scores

* Only text that came out of the vault: `sources[].excerpt` and `results[].text`. The echoed question, titles, capture dates, ids and entity names never score, so a question that repeats an anchor, or a date anchor, cannot score without retrieval. The scorer stops on a string field it does not know, so a new field has to be decided on purpose.
* The byte budget counts the whole serialized output, because that is what the agent pays: the framing line, then every key in order. Budgets are 4 KB and 8 KB. A string counts when it ends inside the first B bytes.
* A question is a hit when every one of its facts has an anchor in a scored string inside the budget. The report also gives facts found, documents per call and bytes per call, so relevance is tied to context cost.
* Whitespace is collapsed before comparing, and the quoting layer that recall puts around an excerpt is read off first.
* The negative control scores each question's anchors against the next question's output and prints that rate beside every table. A set whose floor is not near zero has a leak or weak anchors.
* Each anchor is verified at build time to occur verbatim in the file it names. The build also records its length, how many files hold it and whether the question holds it, and flags an anchor shorter than 12 characters, in more than 3 files, or present in its own question. A person rewrites or approves a flagged anchor by hand.
* A vault is rebuilt for every checkout and corpus, never cached, because the chunker is one of the things a release can change. The import order moves what the recency list shows, so a dev result is shown under three orders (`sorted`, `reverse` and a seeded shuffle) and the lowest count is the one that is read.

## Running it

The harness is `scripts/alice_bench.py`. It uses the Markdown importer of the checkout it is pointed at (`--checkout REPO`), runs the MCP server in process the way a host calls it, and removes every `ALICE*` environment variable from a run, so an agent key or an embeddings endpoint in the parent cannot reach it. It refuses a `--data-dir` outside its own run directory, so it can never open a real vault. It needs a POSIX system. It reads git only when the checkout has a `.git` of its own, so an exported copy of a commit works and is recorded as `no git`.

```
python scripts/alice_bench.py build --run-dir RUN --corpus CORPUS --order sorted --questions QUESTIONS.json
python scripts/alice_bench.py batch --run-dir RUN --questions QUESTIONS.json --out OUTPUTS.json
python scripts/alice_bench.py score --questions QUESTIONS.json --outputs OUTPUTS.json
python scripts/alice_bench.py recall --run-dir RUN --query "a question"
python scripts/alice_bench.py anchors --run-dir RUN --questions QUESTIONS.json --strict-flags
python scripts/alice_bench.py fingerprint --run-dir RUN
```

`score` takes one outputs file per import order and prints the minimum over them. `--search-quality off|passage|on` sets `ALICE_SEARCH_QUALITY` for a run; a checkout that does not read it ignores it, and the fingerprint records the value either way.

A vault belongs to the checkout that built it. The manifest records that checkout as a hash of its source (`checkout_source_sha256`: the path and the bytes of every file under `apps/api/src`, in a fixed order, leaving out only `__pycache__` folders and `.DS_Store`), the real path `alicebot_api` was imported from, the Python and SQLite versions it ran on (`python_version`, `sqlite_version`, because the full-text ranking of a recall comes from SQLite) and, when the checkout has a `.git` at its own root, its commit and whether it differs from that commit. A commit and a dirty flag cannot tell a second uncommitted edit from the first, so the hash is the part that moves with every edit. A checkout with no `.git`, such as the export of a commit, is recorded as `no git` with no commit and no dirty flag, and its hash equals the hash of a worktree of the same commit. A `.git` that git cannot read, or one that is not the root of a repository of its own (an empty folder, a stub or a dangling link that git walks past), is a refusal, so an export that sits inside another repository is never given that repository's commit. The hash covers `apps/api/src` and nothing else: not the installed dependencies, not the other folders of the repository, and it refuses a symlink inside the tree. The build record holds the hash of the harness that built the vault as well (`harness_sha256`, which the manifest also keeps beside the record), and a different harness refuses it. The identity of the checkout and the harness is read before a build imports anything and again after the last file, and a build whose two readings differ is refused and records nothing, so a source edited while it ran is never recorded for a vault that the old code cut. A batch checks the checkout and the harness again when it ends, so an edit made while it ran is refused and not recorded under the old hash. The hash of the harness is taken when the script is loaded, which is the start of a command, and every command refuses if the script on disk differs from it when the command ends, and a build refuses at once if it differs before the build starts. These checks compare readings, so an edit that is made and undone inside one command is not seen.

The manifest also records the vault folder (`vault_dir`, relative to the run directory), what the vault holds and the hash of the snapshot that grep searches (`snapshot_hash`). What the vault holds is `sources` and `chunks` (the counts), `vault_text_sha256` (a hash of the stored text of each source in capture order), `vault_sources_sha256` (a hash of every column of every live source, so the labels `domain` and `sensitivity` that decide what recall may return are held, and so is every other column), `vault_chunks_sha256` (a hash of every chunk's source, index and text) and `vault_row_counts` (the row count of every table, event log included). Every command that reads the vault or the snapshot checks all of it again, reading the vault without writing to it, and refuses on a difference in any of them: another checkout or another source hash, another harness, another vault folder (a rebuild into a new `--data-dir` leaves the old folder on disk, and a nested folder counts by its whole path), a vault copied over another one under the same folder name (it is compared by the text of its chunks, so a vault cut by another chunker is refused even when it holds the same number of chunks), a vault built with other labels or with a source column edited in place, a vault with a chunk edited in place or a memory, a graph row or an event added, an import order or corpus that is not the one the manifest describes, or a snapshot that was changed. The snapshot hash covers every file in the folder whatever its name, because grep reads every file, so a stray file, a folder holding one, or a `.DS_Store` that a Finder visit adds is a refusal too; remove it or build again. Nothing is repaired; build again with `--rebuild`. `--data-dir` has to be a folder of its own inside the run directory, never the run directory or a name the harness keeps. `build` refuses a `--sensitivity` or `--domain` that recall would hide by default, because grep would still read those files and the two arms would see different text. `score` refuses outputs files that came from different source hashes, commits, switches, corpora, question sets or Python and SQLite versions, or from one import order twice, or whose fingerprint names a build of the vault by another checkout or another harness than the one that ran it. Each outputs file is held to its own build record this way even when it is the only one, and `score --json` leaves the per-question results out unless `--per-question` is given.

The outputs file carries a fingerprint of what the numbers measured: the hash of the checkout's source, its git commit and whether it is dirty (or `no git`), the real path `alicebot_api` was imported from, the Python and SQLite versions, the digest of `tools/list`, the switch, the arguments each `alice_recall` call carried and the limit, depth and sources setting they resolve to, the import order, the corpus, snapshot and question-set hashes, the model ids, the prompt hashes, the hash of `gates.json` and the hash of the harness. It does not hold the package version string, because that reads installed metadata and can name a different build than the tree on the path.

`tests/fixtures/search_quality` is a public example: eight invented documents, twelve invented questions and their anchors, and the recall outputs for them. It lets CI test the harness, the scorer and the gate arithmetic with no private material. Its outputs file carries a fingerprint cut down to the import order, so the `score` command refuses it, and the tests score it through the library.

## Tier 2 in outline

The answering agents get one way to search, the `search` command of the harness, with three searches per question counted in a file-locked counter and logged in an append-only file. One arm has `alice_recall` at its default settings (`limit`, `context_depth` and `include_sources` stay at the tool's defaults and the wrapper refuses other values). The other has `grep` over a snapshot of the same files, one `grep` per search, no pipes, output cut at 16 KB. A second grep run with no cut shows whether the cut made grep look worse than it is.

```
python scripts/alice_bench.py search --arm alice --run-dir RUN --state-dir STATE --query "a question"
python scripts/alice_bench.py search --arm grep --run-dir RUN --state-dir STATE --pattern "key" --grep-options='-i -n -C 2'
```

The snapshot grep searches is the stored text of the vault, so it is built through the importer's credential filter and neither arm can see a line the other cannot. The lines the filter withheld are recorded per corpus in the manifest, by file and line, never as text.

`scripts/alice_bench_audit.py` reads each answering run's tool-use record and flags any call that is not the wrapper. A flagged run is graded "not answered" for its arm and is not re-run, because agents break out when a search fails and re-running voided runs would condition each arm's surviving runs differently. The void rate of each arm is reported. A run lost to an infrastructure crash may be re-run and is recorded apart. Isolation is a precondition: before the baseline, a probe agent with the answerers' exact tool list has to fail to read a repository file, run a raw shell command, call another server's tool and call a commit tool.

One judge call per question sees the question, the gold answer, the required facts and every answer shuffled with no arm label. A fifth of the questions are judged again with the answers in the reverse order, a second judge call guesses which answer came from which arm, and the owner grades a sample by hand. All the answering agents, the question writers and the judge are one model family, and the claim says so.

## The gates

`gates.json` holds every number that decides a gate, and it is committed before any held-out number exists. The thresholds were chosen by running `scripts/alice_bench_gates.py`, which prints the exact chance that each gate passes for an assumed share of improved and regressed questions at 120 questions. Some gates ask for a gain and some only guard against harm, and the file says which is which. A threshold change after the lock is a new commit that says why, and numbers from before and after are never mixed in one claim.

```
python scripts/alice_bench_gates.py
python scripts/alice_bench_gates.py --improve 0.12 --regress 0.04
```

The same file records the budget for the CI time of this work. The unit tests run as 3 parallel shard jobs behind the required check, each with a 20 minute limit. The single job they came from ran a median of 13.9 and at most 17.8 minutes across the 70 runs in which it succeeded on 2026-10-02 and 2026-10-03, past the 17 minute line, and the longest shard ran a median of 7.6 and at most 8.0 minutes across the 2 successful runs of the pull request that split it. The new tests may add 90 seconds, and the unit tests are split again before another test lands if a shard passes 17 minutes.

## What the numbers can and cannot say

With 120 questions on two folders, the interval around a difference between two arms is about 11 to 12 points either side, so Tier 2 cannot tell a 5 point difference from nothing. It is a tripwire against a clear harm and a point estimate, and the wording of a claim follows the interval and nothing else. It says nothing about a large vault, Postgres, another model family, notes written in a person's own words, or the memory-note path. The dev corpus is the easy case for grep: 22 files and about 800 KB.

## Held-out questions

Builders may tune on the dev set. Acceptance is on questions that the maintainers keep outside this repository and never show builders, and a builder is told pass or fail for each gate and, on a fail, the pooled net, never a question or a per-question result. Each release needs fresh questions, because a set a builder has seen is fitted to. The release notes of a release that changes retrieval, ranking, import or the shape of a result carry the agent-answer claim, or say plainly that no score was taken for that release.
