# Phase 1 passage-vectors hidden set

Registered on 2026-10-23, before any run on this set, before passage vectors exist in the code, and before any arm answered a question. It adds to [the Phase 1 pre-registration](phase1-preregistration.md) and changes none of its rules. The [Phase 1 results for v0.21.0](phase1-v0210-results.md) said that a later part of Phase 1 is judged on a new hidden set written and locked before its run. This is that set, for passage vectors: finding a passage that states a fact in other words, deep inside a long document.

## The set

One question file of kind `hidden`, written and checked outside the team that builds Alice. That team has not seen it.

- **Content sha256:** `d2377a86bc92c8fad0d6b33a34fdd4b32c077cccc86e330541e6185c80a56ab7`, computed as the pre-registration states (canonical JSON of the file's `policy` and `questions` lists).
- **Corpus:** a third independently written company. It has 600 notes in four team folders, about 518,000 words, including 120 long documents of 2,500 to 5,000 words.
- **Questions:** 406, by task:

  | Task | Questions |
  | --- | --- |
  | recall | 191 |
  | change | 59 |
  | connection | 29 |
  | contradiction | 12 |
  | corroboration | 12 |
  | abstention | 23 |
  | permission | 80 |

  - Of the recall questions, 145 ask for a fact deep inside a long document in other words, and 46 name an exact identifier or term.
  - Of the change questions, 46 ask for the current value and 13 for the value on a past date.
  - Of the permission questions, 56 are asked by a team that may not read the answer and 24 by a team that may.
- **Main-claim questions:** the 326 questions outside the permission task.

## What it measures

The set was designed after a look at where the Alice arm lost answers on the hard set in the v0.21.0 run: many lost answers came from a long note that was found while the passage holding the fact was not.

- **Deep facts.** Every deep fact sits past word 1,200 of its long document, most of them past word 1,500. It sits in a section about something nearby, and its sentence does not repeat the document's subject. The same document holds near-miss sentences of the same kind about other entities.
- **Paraphrased questions.** A deep question uses none of the distinctive words of the sentence that holds the answer, and quotes no three words of it in a row.
- **Exact-term questions.** They name an identifier or term that appears in the answer's sentence, so a gain on paraphrased questions cannot hide a loss on exact lookup.
- **Floor tasks.** Change, exact lookup and permission are large enough that their floor can pass when nothing gets worse. With the registered interval, a task where almost every question is supported needs about 60 questions or more before its lower bound can clear minus 5 points.

No question was kept or dropped because of how any arm answered it.

## How it was made and checked

- **Notes.** Note writers worked from one plan of facts and labels, and every note passed a check of its labels, its length and its planned sentences. A scan of the whole corpus found every planned sentence only in its planned notes, and no restricted text in a note a denied team may read.
- **Questions.** Question writers worked from the plan. Every question passed a check: the asking team can read the notes the answer needs, a denied team cannot, and every required answer string is in the notes.
- **Verification.**
  - Blind verifiers, reading only the notes the asking team may read and without the answer keys, answered all 406 questions correctly, with no restricted text in any answer.
  - One question they found ambiguous was dropped.
  - Before any arm answered, required answer strings that carried more words than a correct answer needs were cut to the value itself. Change questions no longer count a mention of an earlier value as wrong.
- **Import and access.** The import was checked through the harness: every note was captured and indexed with its labels. Team access was checked through the same search the answering agents use, and a planted leak was caught on every denied permission question.

## How it is used

The rules of the pre-registration apply unchanged: the arms, both model families, three repeats with a majority of two, the blind grader with the rubric beside it, the leak check, the exact sign test and the floors.

- **One comparison.** Passage vectors are judged on this set once, against Alice without them, in one run with one harness, so the pair differs only in the ranking.
- **Its own page first.** That comparison and any retrieval measure used beside it are registered on their own page before the run.
- **Reporting.** Results are reported by task and for the deep-fact and exact-term questions separately, whatever they show.
