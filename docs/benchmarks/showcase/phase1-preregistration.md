# Phase 1 pre-registration

Written on 2026-10-19, before any run on the hidden sets and before any Phase 1 result. Phase 1 adds passage retrieval and agent tools. This page fixes how that change will be judged, so the bar cannot move after the numbers are in.

## The hidden sets

Two question files of kind `hidden`. They were written and checked outside the team that builds Alice, and that team has not seen them.

| Set | Notes | Questions |
| --- | --- | --- |
| An independently written company | 277, in four team folders | 204: recall 35, change 35, connection 18, contradiction 15, corroboration 15, abstention 20, permission 66 |
| The generated company (1,000 notes, seed 20261013) | 1,000 | 50 |

Content sha256 of each file:

- independent company: `c8efcf1480c4d4ee2ffbc9e6316d8c38e72bba88e2d6bd068c65dcef59dd5ef3`
- generated company: `a6e85fbd6ee87efdd14589b733a6ec0adf12a878c0144db022761203a7c65129`

The content hash is the sha256 of `json.dumps({"policy": policy, "questions": questions}, sort_keys=True, separators=(",", ":"))` encoded as UTF-8, where `policy` and `questions` are the file's two lists. Every run names these hashes. A change to any question, rubric or policy entry makes a new file with a new hash, and results on it are reported apart from these.

The 50 questions on the generated company check the permission plumbing only. Most of its notes are one-line templates, and its planted facts are in a fixture anyone can read, so it carries no retrieval claim.

## How answers are produced

- **Arms:** Alice at the release before Phase 1 (the baseline), Alice with Phase 1, capped grep, the note folder, and no tool. Every Alice answer is asked through the asking team's key.
- **Models:** one Claude model and one local Ollama chat model, each named in the run manifest. Arms are compared within a model, never across models.
- **Repeats:** every question is answered 3 times for each arm and model. A question counts as supported complete when at least 2 of the 3 answers are.
- **Answering prompt:** frozen before the first run, and its sha256 is printed in the baseline report.

## How answers are scored

- **Correct:** a grader that does not know which arm wrote the answer judges it against the question's rubric. The deterministic rubric score (`score_answer` in `scripts/showcase_bench.py`) is reported beside the grader for every answer.
- **Supported complete:** correct, and the answer cites evidence the arm returned, as the harness defines it.
- **Leak:** the answer contains a policy text the asking team may not know. The check reads the questions file's policy table and never calls product code.

## The main claim

On the 138 retrieval questions of the independent company (every task except permission), Alice with Phase 1 is compared with the baseline, paired by question, separately within each model family.

- **Decision rule:** an exact two-sided sign test on the questions whose supported complete outcome differs between the two arms. Phase 1 improves when p is below 0.05 and more questions moved up than down.
- **Interval:** the paired difference is reported with a 95% interval (Newcombe's score interval for a paired difference of proportions).
- **Target:** +10 points. A smaller gain that passes the decision rule ships with a claim of exactly its measured size.
- **Agreement:** the deterministic rubric score must move in the same direction. If it does not, no claim is made.
- A claim names the model family it holds for. A family that does not pass gets no claim.

**Against file search:** a claim that Alice beats capped grep or the note folder is made only if the same decision rule passes against that arm.

## Floors

Each floor is checked within each model family, with the same paired interval:

- recall (exact lookup), change (time) and permission: the 95% lower bound of the paired change is above minus 5 points;
- zero leaks on the permission task in both sets, across every team key and every repeat;
- p95 of ordinary recall at or under 2 seconds at 100,000 passages on the reference machine, which Phase 0 records with the baseline.

## What follows from a result

If the gain is inconclusive, capture, chunking and reader behaviour are inspected before any graph or planner work is added. The baseline report, the Phase 1 comparison and an ablation (passage vectors, several passages per document, neighbouring text, each removed in turn) are published with the hashes they used.
