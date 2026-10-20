# Phase 0 baseline

Measured on 2026-10-20, on the two hidden question sets fixed in [the Phase 1 pre-registration](phase1-preregistration.md), before any Phase 1 code. The questions themselves stay hidden. This page gives the method, the numbers, and every place the run departed from the plan.

## What was run

- **Question files:** independent company `c8efcf1480c4d4ee2ffbc9e6316d8c38e72bba88e2d6bd068c65dcef59dd5ef3`, generated company `a6e85fbd6ee87efdd14589b733a6ec0adf12a878c0144db022761203a7c65129` (content sha256, computed as the pre-registration states).
- **Code:** an export of main at `675a5943`: v0.20.2 plus the later changes on main, including the showcase harness, and no Phase 1 code.
- **Corpora:** the independent company has 277 notes written outside the team that builds Alice, in four team folders with their own sensitivity and domain labels. The generated company is the harness's 1,000-note company at seed 20261013. Both imports captured every note.
- **Arms:** Alice through the asking team's key, capped grep over the notes, the note folder (list, read and grep), and no tool. Each answering agent had exactly one arm's tools and at most 5 searches per question.
- **Models:** Claude Sonnet 5.5, and gemma4 26b served by Ollama on a local machine. Arms are compared within a model only.
- **Repeats:** every question was answered 3 times for each arm and model. A question counts as supported complete when at least 2 of the 3 answers are.
- **Prompts:** the answering prompt (sha256 `e99a25d5d114dbe5e8d2609bf7212915dd600c4316400c33559c89921132bf4f`) is the same for every arm and both models, and does not tell the answerer its team, so only the Alice key decides what a team can see. The grading prompt has sha256 `f3a23ebf6d5cf32c50d1eddc849506020a733f157b1008b2d9a70b68901f3a60`.
- **Scoring:**
  - A grader that does not know the arm judges each answer against the question's rubric, and the deterministic rubric score is computed beside it.
  - An answer is supported when it is correct and quotes a returned id, or a word-aligned span of 8 or more characters from what its own arm returned. For a question whose right answer is "not known", correct and no leak is enough.
  - A leak is an answer that contains a restricted policy text the asking team may not know, checked without calling Alice.

## Results

### Independent company (277 notes, 204 questions)

Questions supported complete, out of the questions in each group:

| | Alice | grep | note folder | no tool |
| --- | --- | --- | --- | --- |
| Claude, retrieval (138) | 133 | 133 | 137 | 20 |
| Claude, permission (66) | 65 | 24 | 25 | 42 |
| gemma4, retrieval (138) | 99 | 81 | 92 | 20 |
| gemma4, permission (66) | 63 | 25 | 26 | 42 |

Answers that leaked restricted information, out of 612 answers per arm (204 questions, 3 repeats):

| | Alice | grep | note folder | no tool |
| --- | --- | --- | --- | --- |
| Claude | 0 | 112 | 102 | 0 |
| gemma4 | 0 | 78 | 87 | 0 |

Paired on the retrieval questions (exact sign test on the questions where the two arms differ, Newcombe 95% interval of the difference):

- Claude, Alice against grep: 3 questions each way, difference +0.0 points (minus 4.3 to +4.3).
- Claude, Alice against the note folder: 1 against 5, p 0.22, difference minus 2.9 points (minus 7.5 to +1.0).
- gemma4, Alice against grep: 28 against 10, p 0.005, difference +13.0 points (+4.5 to +21.3).
- gemma4, Alice against the note folder: 21 against 14, p 0.31, difference +5.1 points (minus 3.3 to +13.3).

### Generated company (1,000 notes, 50 questions)

| | Alice | grep | note folder | no tool |
| --- | --- | --- | --- | --- |
| Claude, retrieval (30) | 30 | 29 | 30 | 8 |
| Claude, permission (20) | 20 | 7 | 7 | 13 |
| Claude, leaking answers (of 150) | 0 | 41 | 39 | 0 |
| gemma4, retrieval (30) | 30 | 23 | 22 | 8 |
| gemma4, permission (20) | 20 | 6 | 7 | 13 |
| gemma4, leaking answers (of 150) | 0 | 29 | 34 | 0 |

For gemma4, Alice against grep is 7 against 0 (p 0.016) and against the note folder 8 against 0 (p 0.008). Most of the generated company's notes are one-line templates, so these numbers check the permission plumbing more than retrieval.

## What the numbers say

- **Permissions:** none of the 1,524 Alice answers leaked. Searching a shared folder leaked in 13% to 27% of answers. Giving each team its own folder would stop that, but 93 of the 118 questions outside the permission task whose answer is in the notes need a note from another team's folder.
- **Retrieval with a strong model:** with Claude, plain file search answers as many of these questions as Alice. Both are close to the ceiling.
- **Retrieval with a smaller model:** with gemma4, Alice answers 13 points more than capped grep on the independent company, a difference the sign test separates.
- **The grader and the rubric agree** on 593 to 611 of the 612 answers per arm, and 144 to 150 of 150 on the generated company.

## Consequence for Phase 1

Alice with Claude already answers 133 of the 138 retrieval questions. At most 5 can improve, and a 5 to 0 split gives p 0.0625, so under the pre-registration Phase 1 cannot claim a gain for the Claude family on this set. Only the gemma4 family can show one here. A harder hidden set is being written so that a strong-model gain can be measured. It will be registered here, by hash and with its rules, before any Phase 1 result exists. This page and the pre-registration are not changed by it.

## Departures from the plan

Each was found and fixed before any scoring, and the earlier answers are kept but not used.

- **grep syntax:** the harness ran grep with basic syntax. Answering agents write extended patterns (`a|b`, groups), which basic grep reads literally, and about nine in ten such searches matched nothing. The grep and note-folder arms were answered again, for every repeat and both models, with extended syntax. Afterwards about 2% of Claude's extended-syntax searches matched nothing.
- **gemma4:** its thinking mode put whole replies out of view when no tool was offered, so every gemma4 call ran with thinking off. An empty reply was asked again up to twice with a recorded seed, and then recorded as an empty, wrong answer, so no question lost its pair.
- **Grading:** two graders each skipped 4 items. Those 32 answers were graded again on their own with the same prompt.
- **Per-answer cost:** tokens and time per answer are recorded for gemma4. For Claude only run totals are available.
