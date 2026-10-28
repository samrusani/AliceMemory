# Phase 1 passage vectors: the run

Written on 2026-10-28, before any answer of this run exists. It adds to [the comparison](phase1-passage-vectors-comparison.md) and [the passage-vectors hidden set](phase1-passage-vectors-set.md), and changes none of their rules.

## The code

Both Alice arms run `main` at commit `2e0d82221d818ecf8f26cf172f3e673ce0216dd6`, the commit that added the comparison page. The harness of that commit built the vaults and answers every search.

## The vaults

Three vaults were built from the set's 600 notes: one for Alice without vectors (`ALICE_SEARCH_QUALITY=passage`) and two for Alice with vectors (`ALICE_SEARCH_QUALITY=vectors`), for the determinism check. The answers read the first vectors build.

- **Notes.** In every build, all 600 notes were captured and indexed with their labels, as 1,975 passages. The import withheld no line.
- **Vectors.** The product embedded the passages with `nomic-embed-text` v1.5, served by Ollama 0.40.11 on the machine that holds the vaults.
  - Model digest: `0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`, with 768 dimensions.
  - Each vectors build holds 1,974 passage vectors, and no passage is waiting for one.
  - The remaining passage is shorter than the product embeds, so it has no vector.

## The determinism check: passed

- Both vectors builds recorded 1,974 passage vectors and the same vector signature.
- G2's searches returned the same passages, in the same order, in both builds, for each of the 327 questions that have required answer strings.

The run goes on.

## G2: recorded before any answer

G2 was run on these vaults on 2026-10-28, as the comparison page sets out, before any answer. Its decision is recorded here so that it is fixed before the answers exist.

| Rule | Without vectors | With vectors | Result |
| --- | --- | --- | --- |
| Deep paraphrased recall (145): vectors find more | 26 | 51 | Passes. 26 questions found by vectors only and 1 by the other arm only. Exact two-sided sign test p = 0.0000004. |
| Exact-term recall (46): losses minus gains at most 2 | 44 | 44 | Passes. 1 gained and 1 lost. |

The report gives the counts by task, the bytes returned and the search times.

## The speed floor: met

Measured on 2026-10-28 on the reference machine with the run's commit, on the public synthetic corpus of about 100,000 passages on SQLite.

- **The vault.**
  - 13,400 notes in 102,192 passages. 88,792 passages carry a vector, and none is waiting for one.
  - The other 13,400, one in each note, hold only links and labels. The product does not embed such a passage.
- **The method.** Four runs on one vault, in the order passage, vectors, vectors, passage. Each run timed 180 warm recalls after 10 warm-up calls.
- **The result.**
  - In vectors mode, the 95th percentile was 1.72 s and 1.70 s. The floor is 2 seconds.
  - In passage mode, it was 1.56 s and 1.54 s.
  - The vector stage ran in all 180 recalls of each vectors run.

## The answers

- **Arms and vaults.**
  - On the first vectors build: Alice with vectors, capped grep, the note folder and no tool.
  - On the passage build: Alice without vectors.
  - The note folders of the two builds hold the same 600 notes.
- **Size.** 406 questions, five arms and three repeats: 6,090 answers in each model family.
- **Labels.** Each answer's random label was drawn once, for both families, before any answer.
- **gemma4 26b.**
  - Served by Ollama 0.40.11 on a local machine, with thinking off.
  - Model digest: `001e5dafc3c77684c2307ebc6ab8e336e10c9b18eca52acf547d72fc83c3ca8c`. This is the model of the v0.21.0 run, when the same machine ran Ollama 0.40.6.
  - Context of 16,384 tokens and the temperature of the model file. Each repeat's seed is its number.
- **Claude Sonnet 5.5.** As in the earlier runs.
- **Order.**
  - The gemma4 family answers first.
  - The Claude family answers later, on the same vaults, with the same code and labels.
  - The report gives both families. If one of them is not answered, the report says so and is published with the other.

## Departure from the comparison page

- **The speed floor's "every passage embedded".**
  - 13,400 passages of the speed corpus hold only links and labels, and the product never embeds such a passage.
  - The floor was measured with every other passage embedded and none waiting, which is the most a vault of that corpus can hold.

Any later departure is named in the report.
