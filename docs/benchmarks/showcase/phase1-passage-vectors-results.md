# Phase 1 passage vectors: results

Measured as set out in [the run page](phase1-passage-vectors-run.md), under the rules of [the comparison](phase1-passage-vectors-comparison.md), [the passage-vectors hidden set](phase1-passage-vectors-set.md) and [the Phase 1 pre-registration](phase1-preregistration.md). The gemma4 family answered on 2026-10-28. The Claude family had not answered when this page was written. The questions stay hidden. This page gives the method, the numbers, and every place the run departed from the plan.

## What was run

- **Question file:** content sha256 `d2377a86bc92c8fad0d6b33a34fdd4b32c077cccc86e330541e6185c80a56ab7`, computed as the pre-registration states. 406 questions, 326 of them for the main claim.
- **Code:** `main` at commit `2e0d82221d818ecf8f26cf172f3e673ce0216dd6`, for both Alice arms.
- **Arms:**
  - Alice with passage vectors: `ALICE_SEARCH_QUALITY=vectors`, with `nomic-embed-text` v1.5 (digest `0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`).
  - Alice without them: `ALICE_SEARCH_QUALITY=passage`.
  - Capped grep, the note folder, and no tool.

  Both Alice arms ask through the asking team's key. Each answering agent had one arm's tools and at most 5 searches per question. The vector stage ran in all 1,343 searches made in vectors mode.
- **Model:** gemma4 26b (digest `001e5dafc3c77684c2307ebc6ab8e336e10c9b18eca52acf547d72fc83c3ca8c`), served by Ollama on a local machine, with thinking off. An empty reply is asked again up to twice with a recorded seed, then recorded as an empty, wrong answer.
- **Repeats:** 3 for every question and arm. A question counts as supported complete when at least 2 of the 3 answers are.
- **Prompts:** the answering prompt (sha256 `e99a25d5d114dbe5e8d2609bf7212915dd600c4316400c33559c89921132bf4f`) and the grading prompt (sha256 `f3a23ebf6d5cf32c50d1eddc849506020a733f157b1008b2d9a70b68901f3a60`) of the earlier runs.
- **Grading:** a grader that does not know the arm sees, for each question, the rubric and every arm's answer under a shuffled letter. The deterministic rubric score is computed beside it.
- **Supported:** an answer is supported when it is correct and quotes a returned id, or a word-aligned span of 8 or more characters from what its own arm returned. For a question whose right answer is "not known", correct and no leak is enough.
- **Before any answer:** the determinism check, G2 and the speed floor, as the run page records them.

## G2

Each question's text sent once as a search, in each Alice arm. Found means every required answer string is in the returned passages within the first 8,192 bytes. The decision was recorded on the run page before any answer.

| | without vectors | with vectors |
| --- | --- | --- |
| deep paraphrased recall (145) | 26 | 51 |
| exact-term recall (46) | 44 | 44 |
| all recall (191) | 70 | 95 |
| change (59) | 46 | 49 |
| connection (29) | 19 | 19 |
| contradiction (12) | 8 | 10 |
| corroboration (12) | 10 | 11 |
| permission, allowed team (24) | 23 | 23 |

- **Primary rule:** passes. 26 deep questions were found with vectors only and 1 without them only, p = 0.0000004.
- **Exact terms:** passes. 1 gained and 1 lost.
- **Bytes per search:** median 14,630 with vectors and 14,530 without; 90th percentile 15,337 and 15,258.
- **Search time on this vault:** median 0.189 s with vectors and 0.078 s without; 90th percentile 0.210 s and 0.101 s.

## gemma4 26b

### Results

Questions supported complete:

| | Alice with vectors | Alice without | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| main claim (326) | 166 | 159 | 141 | 229 | 23 |
| permission (80) | 78 | 76 | 32 | 29 | 56 |
| answers that leaked (of 1,218) | 0 | 0 | 124 | 143 | 0 |

By task, questions supported complete:

| task | Alice with vectors | Alice without | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| recall, deep paraphrased (145) | 29 | 24 | 20 | 105 | 0 |
| recall, exact term (46) | 35 | 40 | 45 | 43 | 0 |
| change (59) | 45 | 40 | 24 | 26 | 0 |
| connection (29) | 15 | 15 | 19 | 19 | 0 |
| contradiction (12) | 9 | 7 | 4 | 5 | 0 |
| corroboration (12) | 11 | 11 | 8 | 9 | 0 |
| abstention (23) | 22 | 22 | 21 | 22 | 23 |
| permission (80) | 78 | 76 | 32 | 29 | 56 |

### The main claim

Alice with vectors is paired with Alice without them by question. The test is an exact two-sided sign test on the questions whose outcome differs, and the interval is Newcombe's 95% interval of the paired difference.

| questions | up | down | p | difference | 95% interval | rubric |
| --- | --- | --- | --- | --- | --- | --- |
| main claim (326) | 24 | 17 | 0.35 | +2.1 points | minus 1.7 to +6.0 | 153 against 146, same direction |

The decision rule needs p below 0.05. It does not pass, so there is no claim that Alice with vectors answers better for the gemma4 family. The +10 point target is not reached.

### Floors

Alice with vectors against Alice without them, with the same paired interval:

| floor | with vectors | without | up | down | difference (95% interval) | holds |
| --- | --- | --- | --- | --- | --- | --- |
| exact-term recall (46) | 35 | 40 | 0 | 5 | minus 10.9 (minus 21.4 to minus 1.7) | no |
| change (59) | 45 | 40 | 7 | 2 | +8.5 (minus 1.3 to +18.3) | yes |
| permission (80) | 78 | 76 | 2 | 0 | +2.5 (minus 2.1 to +8.5) | yes |

- **Exact lookup does not hold.** The comparison page's note on this task's size does not apply: 5 questions moved down and none up.
  - On each of the 5, Alice without vectors answered in all 3 repeats, and Alice with vectors in at most 1.
  - The grader and the rubric agree on all of them.
- **Zero leaks on the permission task:** holds for both Alice arms, for every team key and every repeat.
- **Speed:** met, as the run page records. The 95th percentile of warm recall in vectors mode was 1.70 to 1.72 s at 102,192 passages.

### Expected direction by task

| task | expected | up | down | p |
| --- | --- | --- | --- | --- |
| recall, deep paraphrased (145) | vectors better | 11 | 6 | 0.33 |
| recall, exact term (46) | no change | 0 | 5 | 0.063 |
| change (59) | no change | 7 | 2 | 0.18 |
| connection (29) | vectors better or no change | 2 | 2 | 1.0 |
| contradiction (12) | no change | 3 | 1 | 0.63 |
| corroboration (12) | no change | 0 | 0 | 1.0 |
| abstention (23) | no change | 1 | 1 | 1.0 |
| permission (80) | no change | 2 | 0 | 0.50 |

- **Exact-term recall moved against its expected direction:** 5 questions down and none up.
- **Change and contradiction moved up,** in the direction of vectors, where no change was expected. Neither reaches p below 0.05.
- No other task moved against its expected direction.

### The first search

For each Alice answer, its first search that the harness answered counts as found when it returned every required answer string within the first 8,192 bytes. For each question, the share of its three answers found on the first search is compared between the two Alice arms.

| questions | with vectors | without | higher with vectors | lower | p | rule |
| --- | --- | --- | --- | --- | --- | --- |
| main claim, with required strings (303) | 47.9% | 43.3% | 30 | 11 | 0.0043 | passes |
| deep paraphrased (145) | 21.1% | 13.8% | 18 | 3 | 0.0015 | passes |
| exact term (46) | 77.5% | 85.5% | 1 | 5 | 0.22 | |

The first-search rule passes for the gemma4 family: an agent's first search finds the fact more often with vectors.

### Searches per answer

- **The efficiency rule passes.**
  - On the 326 main-claim questions, an answer made 1.12 searches with vectors and 1.19 without.
  - 46 questions needed fewer searches with vectors and 19 more, p = 0.0011.
  - Over all 406 questions: 1.10 against 1.17, with 51 fewer and 20 more.
- **The efficiency claim is not made,** because the exact-lookup floor does not hold.
- **Reported beside it:**

  | | with vectors | without |
  | --- | --- | --- |
  | searches, all answers | 1,343 | 1,419 |
  | bytes of search results per answer, mean | 15,342 | 15,986 |
  | bytes of search results per answer, median | 14,531 | 14,434 |
  | supported complete answers | 735 | 710 |
  | bytes per supported complete answer | 25,423 | 27,423 |

### Against file search

- **Alice with vectors against grep:** 61 questions against 36, p 0.014, +7.7 points (+1.8 to +13.4). The rubric moves the same way (153 against 138), so the rule passes.
  - 156 grep answers were empty after the two retries and count as wrong, as registered. No Alice answer was empty.
- **Alice without vectors against grep:** 53 against 35, p 0.069, +5.5 points. No claim.
- **Both Alice arms against the note folder:** the note folder answers more, 229 against 166 and 159.
  - With vectors: 41 questions up and 104 down, minus 19.3 points (minus 26.1 to minus 12.3).
  - The gap is on the deep facts: the note folder answers 105 of the 145, and Alice 29 with vectors and 24 without.

### Agreement

The grader and the rubric agree on 1,152 to 1,218 of the 1,218 answers per arm.

### What the numbers say

- **Vectors make the first search better at finding a fact stated in other words.** G2 and the first-search rule both pass. For gemma4, that seldom turns into a right answer: on the deep questions, 29 against 24 of 145.
- **Vectors cost exact lookups.**
  - On the 5 lost questions, the agent's first search returned every required answer string within the first 8,192 bytes in 15 of the 15 answers without vectors, and in 3 of the 15 with vectors.
  - With vectors, gemma4 searched again in only 4 of those 15 answers.
  - G2, which sends the whole question as one search, did not show this: there both arms found 44 of the 46 exact-term questions.
- **Under the registered rules,** the gemma4 family supports two claims: a whole-question search finds the fact's passage more often with vectors, and an agent's first search finds it more often. It supports no claim that Alice answers better or with fewer searches.
- **The largest gap is not between the two Alice arms.** On the deep facts, the note folder answers 105 of 145, against 29 for Alice with vectors. The note folder lets the agent open a note and read it, and Alice returns passages from notes.
- **Permissions:** none of the 2,436 Alice answers leaked. Searching a shared folder leaked in 10.2% to 11.7% of answers.

## Claude Sonnet 5.5

The Claude family had not answered when this page was written. Its labels were drawn before any answer, and the code, the vaults and the plan are kept. If it answers, its results are added here. If it does not, this section says so.

## Departures from the plan

- **The Ollama version:** the run page names Ollama 0.40.11 on the machine that served gemma4. When the answers started, that machine ran 0.40.12, having updated itself. The model is the same: the same digest, chat template and parameters. Every answer of the run was served by 0.40.12.
- **The speed floor's "every passage embedded":** as the run page states, 13,400 passages of the speed corpus hold only links and labels and are never embedded.
- **Grading:** in one batch, the grader returned grades for only 1 of its 5 questions. The other 4 questions were graded again whole, as in the earlier runs. Every answer has one grade, and none was chosen by its result.
- **Readings the comparison page left open,** fixed when the scoring script was written, before any score was computed:
  - The first search is compared on the main-claim questions that have a required answer string: 303 of the 326. The 23 abstention questions have none. An answer with no search the harness answered counts as not found.
  - The efficiency rule is read on the 326 main-claim questions. All 406 are given beside it.
  - In the scoring script, a task expected to get better is named as moving against its direction when more of its questions moved down than up, and a task expected not to change when it moved with p below 0.05 in either direction. By that reading no task is named, because exact-term recall has p = 0.063. This page names it anyway, because 5 of its questions moved down and none up.
- **Empty answers:** after the two retries, 156 grep, 8 note folder and 9 no-tool answers were still empty and count as wrong. No Alice answer was empty.
- **One look:** this is the one comparison of passage vectors on this set. The set is not used again to judge passage vectors.
