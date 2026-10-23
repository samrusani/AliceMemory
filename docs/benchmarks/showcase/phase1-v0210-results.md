# Phase 1 comparison for v0.21.0: results with the local model

Measured on 2026-10-22 and 2026-10-23, as set out in [the run page](phase1-v0210-run.md), under the rules of [the Phase 1 pre-registration](phase1-preregistration.md) and [the hard hidden set](phase1-hard-set.md). The questions stay hidden. This page gives the method, the numbers, and every place the run departed from the plan. It covers the gemma4 model family. The Claude family has not been run for this comparison (see the departures).

## What was run

- **Question files** (content sha256, computed as the pre-registration states):
  - hard set `ee7de5e9cc698ff4e12bacffa8f27e9bbfcc99ab371966f071c594d972bfdef0`;
  - independent company `c8efcf1480c4d4ee2ffbc9e6316d8c38e72bba88e2d6bd068c65dcef59dd5ef3`;
  - generated company `a6e85fbd6ee87efdd14589b733a6ec0adf12a878c0144db022761203a7c65129`.
- **Code:** v0.21.0 as published, commit `c4757e626405a4bfe77a0039244fb62b483650f7`.
- **Arms:**
  - Alice with Phase 1: `ALICE_SEARCH_QUALITY` unset, so `alice_recall` ranks by passage.
  - The baseline: the same code with `ALICE_SEARCH_QUALITY=off`, which ranks by document as v0.20.2 does.
  - Capped grep, the note folder, and no tool.

  Both Alice arms ask through the asking team's key. Each answering agent had one arm's tools and at most 5 searches per question.
- **Model:** gemma4 26b served by Ollama on a local machine, with thinking off. An empty reply is asked again up to twice with a recorded seed, then recorded as an empty, wrong answer.
- **Repeats:** 3 for every question, arm and set. A question counts as supported complete when at least 2 of the 3 answers are.
- **Prompts:** the answering prompt (sha256 `e99a25d5d114dbe5e8d2609bf7212915dd600c4316400c33559c89921132bf4f`) and the grading prompt (sha256 `f3a23ebf6d5cf32c50d1eddc849506020a733f157b1008b2d9a70b68901f3a60`) are the ones the Phase 0 run used.
- **Grading:** a grader that does not know the arm sees, for each question, the rubric and every arm's answer under a shuffled letter. The deterministic rubric score is computed beside it.
- **Supported:** an answer is supported when it is correct and quotes a returned id, or a word-aligned span of 8 or more characters from what its own arm returned. For a question whose right answer is "not known", correct and no leak is enough.
- **Which answers are new:**
  - on the hard set, every arm was answered in this run;
  - on the independent company, both Alice arms were answered in this run. The grep, note folder and no-tool answers are the Phase 0 run's, graded again here beside the Alice answers;
  - on the generated company, both Alice arms were answered, for the leak floor.

## Results

### Hard set (1,200 notes, 245 questions)

Questions supported complete:

| | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| main claim (177) | 104 | 96 | 84 | 96 | 34 |
| permission (68) | 63 | 58 | 28 | 27 | 50 |
| answers that leaked (of 735) | 0 | 0 | 70 | 82 | 0 |

By task, questions supported complete:

| task | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| recall (40) | 20 | 15 | 9 | 22 | 0 |
| change (40) | 21 | 20 | 14 | 14 | 0 |
| connection (25) | 6 | 4 | 14 | 9 | 0 |
| contradiction (18) | 8 | 8 | 3 | 7 | 0 |
| corroboration (20) | 15 | 15 | 10 | 10 | 0 |
| abstention (34) | 34 | 34 | 34 | 34 | 34 |

### Independent company (277 notes, 204 questions)

| | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| main claim (138) | 104 | 100 | 80 | 92 | 20 |
| permission (66) | 64 | 63 | 24 | 26 | 42 |
| answers that leaked (of 612) | 0 | 0 | 78 | 87 | 0 |

### Generated company (1,000 notes, 50 questions)

Both Alice arms answer all 50 questions supported complete, and neither leaked in any of its 150 answers.

## The main claim

Alice with Phase 1 is paired with the baseline by question. The test is an exact two-sided sign test on the questions whose outcome differs, and the interval is Newcombe's 95% interval of the paired difference.

| set | up | down | p | difference | 95% interval | rubric |
| --- | --- | --- | --- | --- | --- | --- |
| hard (177) | 11 | 3 | 0.057 | +4.5 points | +0.4 to +8.6 | 108 against 99, same direction |
| independent (138) | 6 | 2 | 0.29 | +2.9 points | minus 1.2 to +7.1 | 107 against 101, same direction |

The decision rule needs p below 0.05. Neither set passes it, so v0.21.0 makes no Phase 1 claim for the gemma4 family. The +10 point target is not reached on either set.

Helped, harmed and unchanged (descriptive only): on the hard set passage ranking helped 11 questions, harmed 3 and left 163 unchanged. On the independent company it helped 6, harmed 2 and left 130 unchanged. On the generated company no question changed.

## Against file search

Only the hard set can carry this claim, because there every arm was answered in the same run.

- **Alice with Phase 1 against grep:** 38 questions against 18, p 0.011, +11.3 points (+3.1 to +19.2), and the rubric moves the same way. The rule passes. On the hard set, with gemma4, Alice v0.21.0 answers 11 points more of the main-claim questions than capped grep.
  - The comparison carries the declared disadvantage for Alice: the credential check withheld lines that file search reads in full (see the departures).
  - 22 grep answers were empty after the two retries and count as wrong, as registered. No Alice answer was empty.
- **Alice with Phase 1 against the note folder:** 30 against 22, p 0.33, +4.5 points (minus 3.4 to +12.3). No claim.
- **The baseline:**
  - against grep: 30 against 18, p 0.11;
  - against the note folder: 28 each way.

On the independent company the file-search answers come from the Phase 0 run, so these pairs span two runs and are descriptive only. Alice with Phase 1 against grep is 30 against 6 (+17.4 points). Against the note folder it is 22 against 10 (+8.7 points, p 0.050).

## Floors

Passage ranking against the baseline, by task, with the same paired interval:

| set | recall | change | permission |
| --- | --- | --- | --- |
| hard | +12.5 (+2.3 to +22.0) | +2.5 (minus 2.2 to +7.1) | +7.4 (+0.9 to +15.4) |
| independent | +2.9 (minus 6.4 to +14.2) | +8.6 (minus 0.8 to +18.1) | +1.5 (minus 3.7 to +7.9) |

- **Paired floors (lower bound above minus 5 points):**
  - every one holds on the hard set;
  - on the independent company, change and permission hold;
  - recall does not hold as written. One question moved up and none down, but with 35 questions that interval's lower bound is minus 6.4. The floor is reported as not met, and the rule is not changed.
- **The generated company** carries no retrieval claim. Both Alice arms answer all 50 questions there, so nothing moved.
- **Zero leaks on the permission task:** holds for both Alice arms on all three sets, for every team key and every repeat.
- **Speed:** recall p95 of 1.56 to 1.58 s at 102,192 passages on SQLite, under the 2 second floor (measured for the release).

## Agreement

The grader and the rubric agree on:

- 720 to 733 of the 735 answers per arm on the hard set;
- 597 to 611 of the 612 per arm on the independent company;
- all 150 per arm on the generated company.

## What the numbers say

- **Passage ranking:** it moved more questions up than down on both sets (11 to 3 and 6 to 2), and the rubric agrees. The gain is smaller than the registered rule can tell apart from chance, so v0.21.0 ships passage ranking with no measured-gain claim for the local model. The largest movement is on recall in the hard set: 5 up, none down.
- **Permissions:** none of the 2,994 Alice answers leaked. Searching a shared folder leaked in 9.5% to 14.2% of answers.
- **File search:** on the hard set, Alice answers more than capped grep under the registered rule, and about as many as the note folder.
- **A weak spot:** questions that join several notes. On the hard set, grep answers 14 of the 25 connection questions, against 6 for Alice.

## Departures from the plan

- **Only one model family:** the run page names Claude Sonnet 5.5 and gemma4. Only gemma4 has been run. No Claude answer exists for this comparison. If the Claude family is run, its results will be added to this page under the same rules.
- **Baseline answers produced twice:**
  - In the first pass, every search of the baseline arm was refused, on all three sets. The search server that the answering agents call did not pass the run's search setting to the harness, and the harness refuses a search whose setting differs from the one the run was built with.
  - Those answers had no search results. They are kept and were never graded.
  - The server was fixed and tested. All 1,497 baseline answers were then produced again, with the same questions, before any grading. The passage-ranking arm was not affected.
- **Phase 0 answers graded again:** on the independent company, the grep, note folder and no-tool answers of the Phase 0 run were graded again in this run, so that every arm of a question shares one grader. grep supports 80 main-claim questions here, against 81 in the Phase 0 report. The note folder (92) and no tool (20) are unchanged.
- **The credential check on the hard set,** as declared on the run page: v0.21.0's import withheld 55 lines in 12 notes and one whole note, so both Alice arms read the notes without them, and the file-search arms read them in full. A later release fixes the false positive.
- **Labels on the hard set:** mapped as declared on the run page.
- **Empty answers:** after the two retries, these gemma4 answers were still empty and count as wrong:
  - on the hard set, 22 grep and 4 note folder answers;
  - on the independent company, from the Phase 0 run, 8 grep and 4 no-tool answers.

  No Alice answer was empty.
- **One look:** this was the one comparison of the hard set for Phase 1 as v0.21.0 ships it. A later part of Phase 1 is judged on a new hidden set.
