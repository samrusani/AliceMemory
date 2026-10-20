# Phase 1 hard hidden set

Registered on 2026-10-20, before any run on this set and before any Phase 1 result. The [Phase 0 baseline](phase0-baseline.md) showed that Claude with Alice already answers 133 of the 138 retrieval questions of the first hidden set, so a Phase 1 gain for a strong model cannot be measured there. This set is harder by design. It adds to [the Phase 1 pre-registration](phase1-preregistration.md) and changes nothing in it.

## The set

One question file of kind `hidden`, written and checked outside the team that builds Alice. That team has not seen it.

- **Content sha256:** `ee7de5e9cc698ff4e12bacffa8f27e9bbfcc99ab371966f071c594d972bfdef0`, computed as the pre-registration states (canonical JSON of the file's `policy` and `questions` lists).
- **Corpus:** an independently written company of 1,200 notes in four team folders, about 729,000 words, with 120 long documents of 2,000 to 4,000 words.
- **Questions:** 245. Recall 40, change 40, connection 25, contradiction 18, corroboration 20, abstention 34, and permission 68.
- **Main-claim questions:** the 177 questions outside the permission task.

## What makes it harder

The difficulty comes from how the notes and questions were written, not from choosing questions any arm got wrong:

- Many facts sit deep inside long documents, past their first 1,500 words.
- 186 questions touch a fact that is asked in other words than the notes use, with none of the notes' distinctive terms.
- Ten connection questions need three notes in a chain.
- The change chains run 4 to 6 stages, some changed silently, partly, or reversed. Ten questions ask for the value on a past date.
- Near-duplicate ids, superseded drafts and cancelled orders sit beside the live ones.

No question was kept or dropped because of how any arm answered it. A blind checker, reading only the notes the asking team can read and without the answer keys, answered every question. Five questions it found ambiguous were dropped before the lock.

## How it is used

The rules of the pre-registration apply unchanged: the same arms, models, three repeats with a majority of two, blind grader with the rubric beside it, leak check, and the exact sign test and floors.

- Phase 1 is judged on this set as well as on the first one. The main claim on this set uses its 177 non-permission questions.
- A claim names the set and the model family it holds for. Results on both sets are reported, whatever they show.
- On this set, the baseline and Phase 1 are answered in the same run with the same harness, so the pair differs only in the Alice code.
