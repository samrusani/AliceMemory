# Phase 1 comparison for v0.21.0

Written on 2026-10-22, before any answer of this run exists. It adds to [the Phase 1 pre-registration](phase1-preregistration.md) and [the hard hidden set](phase1-hard-set.md) and changes none of their rules.

## What is compared

v0.21.0 ships one part of Phase 1: passage ranking, which is the default of `alice_recall` on SQLite. This run is the registered Phase 1 comparison for that part.

- **Alice with Phase 1:** v0.21.0 as published, with `ALICE_SEARCH_QUALITY` unset, so `alice_recall` uses passage ranking.
- **Baseline:** the same v0.21.0 code with `ALICE_SEARCH_QUALITY=off`, which is the ranking v0.20.2 has.
- **Departure from the pre-registration's wording.** The pre-registration names the baseline as "Alice at the release before Phase 1". v0.21.0 also changed what a key with limits reads: ids of rows it may not read are withheld, and pages are refilled after hidden rows. Holding the code fixed makes the two Alice arms differ only in the ranking, so a difference can be attributed to Phase 1 and not to those changes. Both Alice arms ask through each team's key, as registered.
- **Other arms:** capped grep, the note folder and no tool, as registered.
- **Models:** Claude Sonnet 5.5, and gemma4 26b served by Ollama on a local machine with thinking off, as in the Phase 0 run. The answering prompt (sha256 `e99a25d5d114dbe5e8d2609bf7212915dd600c4316400c33559c89921132bf4f`) and the grading prompt (sha256 `f3a23ebf6d5cf32c50d1eddc849506020a733f157b1008b2d9a70b68901f3a60`) are the ones the Phase 0 run used.
- **Repeats and scoring:** 3 repeats, with a majority of 2, a blind grader with the deterministic rubric beside it, and the leak check, all as registered.

## Which answers this run produces

- **Hard set** (245 questions, 177 for the main claim): every arm is answered in this run.
- **Independent company** (204 questions, 138 for the main claim): both Alice arms are answered in this run. The grep, note folder and no-tool answers of the Phase 0 run are reused and scored again under the current rules. A comparison of Alice with file search on this set therefore spans two runs. It is reported as such and is not used for a claim.
- **Generated company** (50 permission questions): both Alice arms are answered, for the leak floor.

## Labels on the hard set

The hard set's notes carry the domain words of the company they describe, such as operations, technical, commercial, corporate, strategy and hr. Most of them are not Alice domains, and an import refuses a domain it does not have, so the notes cannot be imported with those words. Who may read a note in this set was defined, when the set was written and checked, by two things only: the sensitivity (a confidential note is readable by leadership only) and whether the domain is restricted (support and product may not read legal or financial notes). The import therefore keeps the legal and financial domains, labels every other note professional, and keeps every sensitivity. The note text and every other label are unchanged, and which team may read which note is the same for all 1,200 notes and all four teams. The locked files are not changed; the import reads a copy with these labels, and the file-search arms read the same copy.

## The credential gate on the hard set

v0.21.0's import withholds a line that its credential check flags, and a whole note when the note's file name is flagged. On the hard set the check flags short product codes (two letters, a hyphen and digits) as credential material, which is a false positive: it withholds 55 lines in 12 notes and one whole note. The run measures v0.21.0 as shipped, so both Alice arms import the notes with those lines withheld, exactly as a user's import would, and the fix goes into a later release.

- 7 of the 177 main-claim questions need a note with withheld lines, and for 1 of them a string the answer must contain appears only on a withheld line.
- Both Alice arms lose the same lines, so the comparison of Phase 1 with the baseline is not affected by it.
- The file-search arms read the notes in full, so a comparison of Alice with file search on this set carries this disadvantage for Alice. The report says so beside that comparison.
- The harness refuses a build in which the product withheld a whole note. For this run it lists such a note instead (`withheld_by_credential_gate` in the capture report) and counts the build as complete when every other note is captured and indexed. That is the only change to the harness, and the product code is the v0.21.0 code.

## Rules

The decision rule, the target, the agreement requirement and the floors are those of the pre-registration and the hard set page, unchanged. The speed floor was measured for the release on SQLite at 102,192 passages, with a 95th percentile of 1.56 to 1.58 s in passage mode (see the [v0.21.0 release notes](../../release/v0.21.0-release-notes.md)).

One table is added, and it is descriptive only: for each model family and set, the number of questions that passage ranking helped, harmed and left unchanged (supported complete under the baseline but not under Phase 1, and the reverse). It does not change the decision rule, which already counts both directions.

## One look

This is the one comparison of the hard set for Phase 1 as v0.21.0 ships it. A later part of Phase 1, such as passage vectors, exact lookup or agent tools, is judged on a new hidden set that is written and locked before its run. The hard set is not used again to judge Phase 1.

## Reporting

The report is published whatever it shows. It names the question file hashes, the prompts, the models, the code and every departure from this page.
