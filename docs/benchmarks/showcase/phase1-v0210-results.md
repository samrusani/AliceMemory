# Phase 1 comparison for v0.21.0: results

Measured as set out in [the run page](phase1-v0210-run.md), under the rules of [the Phase 1 pre-registration](phase1-preregistration.md) and [the hard hidden set](phase1-hard-set.md): the gemma4 family on 2026-10-22 and 2026-10-23, and the Claude family on 2026-10-24. The questions stay hidden. This page gives the method, the numbers for each family, and every place the run departed from the plan.

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
- **Models:**
  - gemma4 26b served by Ollama on a local machine, with thinking off. An empty reply is asked again up to twice with a recorded seed, then recorded as an empty, wrong answer.
  - Claude Sonnet 5.5 (`claude-sonnet-5-5`), one answering agent per answer with only its arm's tools, as in the Phase 0 run.
- **Repeats:** 3 for every question, arm and set. A question counts as supported complete when at least 2 of the 3 answers are.
- **Prompts:** the answering prompt (sha256 `e99a25d5d114dbe5e8d2609bf7212915dd600c4316400c33559c89921132bf4f`) and the grading prompt (sha256 `f3a23ebf6d5cf32c50d1eddc849506020a733f157b1008b2d9a70b68901f3a60`) are the ones the Phase 0 run used.
- **Grading:** a grader that does not know the arm sees, for each question, the rubric and every arm's answer under a shuffled letter. The deterministic rubric score is computed beside it.
- **Supported:** an answer is supported when it is correct and quotes a returned id, or a word-aligned span of 8 or more characters from what its own arm returned. For a question whose right answer is "not known", correct and no leak is enough.
- **Which answers are new:**
  - on the hard set, every arm was answered in this run;
  - on the independent company, both Alice arms were answered in this run. The grep, note folder and no-tool answers are the Phase 0 run's, graded again here beside the Alice answers;
  - on the generated company, both Alice arms were answered, for the leak floor.

## gemma4 26b

### Results

#### Hard set (1,200 notes, 245 questions)

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

#### Independent company (277 notes, 204 questions)

| | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| main claim (138) | 104 | 100 | 80 | 92 | 20 |
| permission (66) | 64 | 63 | 24 | 26 | 42 |
| answers that leaked (of 612) | 0 | 0 | 78 | 87 | 0 |

#### Generated company (1,000 notes, 50 questions)

Both Alice arms answer all 50 questions supported complete, and neither leaked in any of its 150 answers.

### The main claim

Alice with Phase 1 is paired with the baseline by question. The test is an exact two-sided sign test on the questions whose outcome differs, and the interval is Newcombe's 95% interval of the paired difference.

| set | up | down | p | difference | 95% interval | rubric |
| --- | --- | --- | --- | --- | --- | --- |
| hard (177) | 11 | 3 | 0.057 | +4.5 points | +0.4 to +8.6 | 108 against 99, same direction |
| independent (138) | 6 | 2 | 0.29 | +2.9 points | minus 1.2 to +7.1 | 107 against 101, same direction |

The decision rule needs p below 0.05. Neither set passes it, so v0.21.0 makes no Phase 1 claim for the gemma4 family. The +10 point target is not reached on either set.

Helped, harmed and unchanged (descriptive only): on the hard set passage ranking helped 11 questions, harmed 3 and left 163 unchanged. On the independent company it helped 6, harmed 2 and left 130 unchanged. On the generated company no question changed.

### Against file search

Only the hard set can carry this claim, because there every arm was answered in the same run.

- **Alice with Phase 1 against grep:** 38 questions against 18, p 0.011, +11.3 points (+3.1 to +19.2), and the rubric moves the same way. The rule passes. On the hard set, with gemma4, Alice v0.21.0 answers 11 points more of the main-claim questions than capped grep.
  - The comparison carries the declared disadvantage for Alice: the credential check withheld lines that file search reads in full (see the departures).
  - 22 grep answers were empty after the two retries and count as wrong, as registered. No Alice answer was empty.
- **Alice with Phase 1 against the note folder:** 30 against 22, p 0.33, +4.5 points (minus 3.4 to +12.3). No claim.
- **The baseline:**
  - against grep: 30 against 18, p 0.11;
  - against the note folder: 28 each way.

On the independent company the file-search answers come from the Phase 0 run, so these pairs span two runs and are descriptive only. Alice with Phase 1 against grep is 30 against 6 (+17.4 points). Against the note folder it is 22 against 10 (+8.7 points, p 0.050).

### Floors

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

### Agreement

The grader and the rubric agree on:

- 720 to 733 of the 735 answers per arm on the hard set;
- 597 to 611 of the 612 per arm on the independent company;
- all 150 per arm on the generated company.

### What the numbers say

- **Passage ranking:** it moved more questions up than down on both sets (11 to 3 and 6 to 2), and the rubric agrees. The gain is smaller than the registered rule can tell apart from chance, so v0.21.0 ships passage ranking with no measured-gain claim for the local model. The largest movement is on recall in the hard set: 5 up, none down.
- **Permissions:** none of the 2,994 Alice answers leaked. Searching a shared folder leaked in 9.5% to 14.2% of answers.
- **File search:** on the hard set, Alice answers more than capped grep under the registered rule, and about as many as the note folder.
- **A weak spot:** questions that join several notes. On the hard set, grep answers 14 of the 25 connection questions, against 6 for Alice.

## Claude Sonnet 5.5

### Results

#### Hard set (1,200 notes, 245 questions)

Questions supported complete:

| | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| main claim (177) | 165 | 160 | 165 | 172 | 34 |
| permission (68) | 68 | 65 | 18 | 18 | 50 |
| answers that leaked (of 735) | 0 | 0 | 139 | 140 | 0 |

By task, questions supported complete:

| task | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| recall (40) | 38 | 34 | 36 | 40 | 0 |
| change (40) | 33 | 34 | 37 | 38 | 0 |
| connection (25) | 23 | 22 | 24 | 23 | 0 |
| contradiction (18) | 17 | 16 | 17 | 18 | 0 |
| corroboration (20) | 20 | 20 | 18 | 20 | 0 |
| abstention (34) | 34 | 34 | 33 | 33 | 34 |

#### Independent company (277 notes, 204 questions)

| | Alice with Phase 1 | baseline | grep | note folder | no tool |
| --- | --- | --- | --- | --- | --- |
| main claim (138) | 135 | 133 | 133 | 138 | 20 |
| permission (66) | 64 | 66 | 24 | 24 | 42 |
| answers that leaked (of 612) | 0 | 0 | 112 | 102 | 0 |

#### Generated company (1,000 notes, 50 questions)

Both Alice arms answer all 50 questions supported complete, and neither leaked in any of its 150 answers.

### The main claim

The same paired test as for gemma4:

| set | up | down | p | difference | 95% interval | rubric |
| --- | --- | --- | --- | --- | --- | --- |
| hard (177) | 8 | 3 | 0.23 | +2.8 points | minus 1.0 to +7.0 | 166 against 162, same direction |
| independent (138) | 3 | 1 | 0.63 | +1.4 points | minus 2.0 to +5.5 | 135 against 132, same direction |

Neither set passes the decision rule, so v0.21.0 makes no Phase 1 claim for the Claude family either. The +10 point target is not reached on either set.

Helped, harmed and unchanged (descriptive only): on the hard set passage ranking helped 8 questions, harmed 3 and left 166 unchanged. On the independent company it helped 3, harmed 1 and left 134 unchanged. On the generated company no question changed.

### Against file search

On the hard set:

- **Alice with Phase 1 against grep:** 9 questions against 9, p 1.0, no difference (minus 5.0 to +5.0). No claim.
- **Alice with Phase 1 against the note folder:** 4 against 11, p 0.12, minus 4.0 points (minus 8.8 to +0.4). No claim either way.
- **The baseline:**
  - against grep: 11 against 16, p 0.44;
  - against the note folder: 5 against 17, p 0.017, minus 6.8 points (minus 12.3 to minus 1.6). With Claude, the note folder answers more main-claim questions than the baseline.

On the independent company these pairs span two runs and are descriptive only. Alice with Phase 1 against grep is 3 against 1. Against the note folder it is 0 against 3 (minus 2.2 points).

### Floors

Passage ranking against the baseline, by task, with the same paired interval:

| set | recall | change | permission |
| --- | --- | --- | --- |
| hard | +10.0 (minus 2.4 to +23.7) | minus 2.5 (minus 12.7 to +7.3) | +4.4 (minus 1.7 to +12.2) |
| independent | 0.0 (minus 9.9 to +9.9) | 0.0 (minus 9.9 to +9.9) | minus 3.0 (minus 10.4 to +2.9) |

- **Paired floors (lower bound above minus 5 points):**
  - on the hard set, recall and permission hold. Change does not hold as written: 1 question moved up and 2 down.
  - on the independent company, none holds as written. Both arms answer all 35 recall and all 35 change questions, and with nothing moving, the interval's lower bound for 35 questions is minus 9.9. On permission, 2 questions moved down and none up. Neither was a leak: in one, the answer offered an earlier figure as the answer to a question it should have declined, and in the other, a team allowed to read the figure declined to give it in 2 of 3 repeats.
  - The floors are reported as not met where they are not met, and the rule is not changed.
- **The generated company** carries no retrieval claim. Both Alice arms answer all 50 questions there, so nothing moved.
- **Zero leaks on the permission task:** holds for both Alice arms on all three sets, for every team key and every repeat.

### Agreement

The grader and the rubric agree on:

- 722 to 735 of the 735 answers per arm on the hard set;
- 597 to 612 of the 612 per arm on the independent company;
- all 150 per arm on the generated company.

### What the numbers say

- **Passage ranking:** it moved more questions up than down on both sets (8 to 3 and 3 to 1), and the rubric agrees, but the gain is far from what the registered rule can tell apart from chance. With Claude, both Alice arms already answer most main-claim questions (160 or more of 177 on the hard set, 133 or more of 138 on the independent company), which leaves little room to move.
- **Permissions:** none of the 2,994 Alice answers leaked. Searching a shared folder leaked in 16.7% to 19.0% of answers.
- **File search:** with Claude, capped grep matches Alice on the hard set's main claim (165 each), and the note folder answers more (172, no claim). The difference is permission: Alice answers all 68 permission questions, and grep and the note folder 18 each, because they return notes the asking team may not read. Over all 245 questions on the hard set, Alice answers 233, the note folder 190 and grep 183.
- **A weak spot:** questions about what changed. On the hard set, Alice answers 33 of the 40 change questions, grep 37 and the note folder 38.

## Departures from the plan

- **The Claude family ran later:** its answers were produced on 2026-10-24, after the gemma4 results on this page were published and after the hard set's gemma4 answers had been studied to plan the next part of Phase 1. The code, the prompts, the question files and the scoring are the ones the gemma4 run used. The Claude plans for the hard set's passage-ranking run were written on 2026-10-22 with the gemma4 plans, and the other runs' Claude plans on 2026-10-24 by the same script.
- **One Claude batch set aside:** a chat message sent to the operator while the first batch of 450 answers was being started reached the answering agents. That batch was repeat 1 of the hard set's passage-ranking run, its Alice and grep answers. 173 of the answers mention the message and 114 spent searches on it, grep answers more often than Alice answers. The batch was set aside and never graded, and repeat 1 was answered again with new labels.
- **15 Claude baseline answers answered again:** on the hard set, two answering batches of the baseline arm ran at the same time, and 15 baseline searches failed with a harness error, one in each of 15 answers. Run alone afterwards, the same searches succeed. Because only the baseline arm was affected, those 15 answers were answered again once with new labels, chosen from the search log alone before any grading. The first answers are kept and were not graded. Answering batches ran one at a time after that.
- **Claude grading in parts:** Claude answers are longer, so each grading packet was split across several grading runs with the same prompt, batches and grader. Six batches failed on a usage limit and were run again, and a rerun grades again every batch after the first failure in its part. Seven questions whose grades came back incomplete were graded again whole. Every answer has one grade, and none was chosen by its result.
- **Baseline answers produced twice:**
  - In the first pass, every search of the baseline arm was refused, on all three sets. The search server that the answering agents call did not pass the run's search setting to the harness, and the harness refuses a search whose setting differs from the one the run was built with.
  - Those answers had no search results. They are kept and were never graded.
  - The server was fixed and tested. All 1,497 baseline answers were then produced again, with the same questions, before any grading. The passage-ranking arm was not affected.
- **Phase 0 answers graded again:** on the independent company, the grep, note folder and no-tool answers of the Phase 0 run were graded again in this run, so that every arm of a question shares one grader.
  - gemma4: grep supports 80 main-claim questions here, against 81 in the Phase 0 report. The note folder (92) and no tool (20) are unchanged.
  - Claude: grep supports 133 (Phase 0: 133), the note folder 138 (137) and no tool 20 (20).
- **The credential check on the hard set,** as declared on the run page: v0.21.0's import withheld 55 lines in 12 notes and one whole note, so both Alice arms read the notes without them, and the file-search arms read them in full. A later release fixes the false positive.
- **Labels on the hard set:** mapped as declared on the run page.
- **Empty answers:** after the two retries, these gemma4 answers were still empty and count as wrong:
  - on the hard set, 22 grep and 4 note folder answers;
  - on the independent company, from the Phase 0 run, 8 grep and 4 no-tool answers.

  No Alice answer was empty, and no Claude answer was empty.
- **One look:** this was the one comparison of the hard set for Phase 1 as v0.21.0 ships it, for both model families. A later part of Phase 1 is judged on a new hidden set.
