# Phase 1 passage vectors: the comparison

Registered on 2026-10-28. This is before any run on [the passage-vectors hidden set](phase1-passage-vectors-set.md), and before any arm answered one of its questions. That page said the comparison, and any retrieval measure used beside it, would be registered on their own page before the run. This is that page. The rules of [the Phase 1 pre-registration](phase1-preregistration.md) apply, and nothing here changes them.

## What is compared

- **Arms.**
  - **Alice with passage vectors:** `ALICE_SEARCH_QUALITY=vectors`. Every note's passages are embedded through the product by `nomic-embed-text` v1.5 at a local Ollama endpoint. The model digest is recorded in the run manifest.
  - **Alice without them:** `ALICE_SEARCH_QUALITY=passage`.
  - **Other arms:** capped grep, the note folder and no tool, as registered.
- **Code.**
  - Both Alice arms run one commit of `main` that contains this page. The run page names that commit before any answer is produced.
  - Both arms use the recall default of ten note passages, so the pair differs only in the ranking. That default was chosen on the hard set, which is not used here.
- **Models, prompts and scoring.**
  - **Models:** Claude Sonnet 5.5, and gemma4 26b served by Ollama with thinking off.
  - **Prompts:** the answering prompt (sha256 `e99a25d5d114dbe5e8d2609bf7212915dd600c4316400c33559c89921132bf4f`) and the grading prompt (sha256 `f3a23ebf6d5cf32c50d1eddc849506020a733f157b1008b2d9a70b68901f3a60`) of the earlier runs.
  - **Repeats:** three, with a majority of two.
  - **Scoring:** the blind grader with the deterministic rubric beside it, and the leak check.

## The main claim

On the 326 questions outside the permission task, Alice with passage vectors is compared with Alice without them, paired by question, within each model family.

- **Decision rule:** the pre-registration's. It is an exact two-sided sign test on the questions whose supported complete outcome differs, with p below 0.05 and more questions up than down.
- **Also as registered:** the paired 95% interval, the target of +10 points, the agreement of the deterministic rubric score, and a claim that names its model family.

## Floors

These are checked within each model family, with the same paired interval:
- **Exact lookup:** the 46 exact-term recall questions. The 95% lower bound of the paired change is above minus 5 points.
  - The set's page counted this task among those large enough for the bound. But 46 is below the size of about 60 that the same page gives, so the bound can fail even when no question gets worse.
  - If it fails with no question moving down, the report says so beside the result, and the G2 rule on exact terms below is the check that nothing got worse.
- **Change:** the 59 change questions, with the same bound.
- **Permission:** the 80 permission questions, with the same bound, and zero leaks across every team key and every repeat.
- **Speed:** the 95th percentile of warm recall in vectors mode is at or under 2 seconds at about 100,000 passages on SQLite, with every passage embedded. It is measured on the reference machine with the run's commit before the run, and the run page gives the number.

## The retrieval measure beside it (G2)

G2 asks whether vectors return the passage that holds the fact more often. It needs no answering model.

- **The method.** Each question's text is sent once as a search through the asking team's key, in each Alice arm. A question counts as found when every one of its required answer strings appears in the returned passages within the first 8,192 bytes of the output. This is how the earlier retrieval checks counted.
- **Primary rule (registered now):** on the 145 deep paraphrased recall questions, vectors find more. This is an exact two-sided sign test on the questions found by one arm and not the other, with p below 0.05 and more found by vectors.
- **Exact terms must not get worse:** on the 46 exact-term recall questions, the questions vectors lose minus those they gain is at most 2.
- **Reported beside it, whatever it shows:** every task's counts in both arms, the bytes returned per search (median and 90th percentile) and the search time.

**How the two parts are read together:**
- G2 is the primary evidence that the mechanism works. With about 326 questions, the answer comparison has little power for a gain of 3 to 5 points.
- If G2 passes and the main claim is inconclusive, the only claim made is that Alice with vectors finds the fact's passage more often on this set. No claim is made that it answers better.
- If G2 fails, no claim is made for vectors.

## Expected direction by task, written now

| Task | Expected |
| --- | --- |
| recall, deep paraphrased (145) | vectors better |
| recall, exact term (46) | no change |
| change (59) | no change |
| connection (29) | vectors better or no change |
| contradiction (12), corroboration (12), abstention (23) | no change |
| permission (80) | no change, zero leaks |

A task that moves against its expected direction is named in the report, whatever the main claim shows.

## Checks on the run itself

- **Determinism.** The vectors vault is built twice from the same notes. Both builds must record the same number of passage vectors and the same vector signature. The search results of G2 must be identical across the two builds. If either differs, the run stops and the report says why.
- **Readiness.** Every vectors search runs only after the harness has confirmed that no passage is waiting for a vector. Each search records whether the vector stage ran. A search in which it did not run is counted and reported, and not dropped.
- **Grep baseline.** The capped grep arm is answered in the same run, so a gain is also seen against plain file search.

## One look

This is the one comparison of passage vectors on this set. The set is not used again to judge passage vectors. The report is published whatever it shows. It names the question file hash, the prompts, the models, the code, the embedding model digest and every departure from this page.
