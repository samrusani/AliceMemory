# Redaction removal: design note

This note does not change redaction. v0.20.2 replaces a redacted memory's own text with `[REDACTED]` and restricts reports built from it to the owner and an unbound admin key. The words stay in those reports. Full removal of redacted text from reports is called P1 here. It is not built, and it waits for a keyed deployment. The choice among the ways to treat a report stays open until P1 is built; this note records the options and what each costs.

Measured on SQLite, 17 October 2026, in this repository, with a synthetic vault. No PostgreSQL was running.

## Where the text can live

- The memory row: `canonical_text`, `title`, `summary`, `value`.
- Its revisions (`previous_value`, `new_value`) and its events.
- Quoted provenance and saved quotes that name it.
- A derived row that copied the words into its own text. On SQLite that is another memory. On PostgreSQL it is also a daily brief, a weekly synthesis, a project update, a connection report, a contradiction report, a consolidation report, a roll-up card, a weekly candidate, a promoted copy, or a project state.
- The fields those reports copy into: `content_markdown`, `quote_new`, `explanation`, `suggested_current_state`, `value.rollup.instances`, and the digest in `member_snapshots`.
- A belief's claim, and a graph edge's explanation.
- An open loop's title and description when a person typed them. That text is not a copy the system made.
- The source the memory was captured from. Redaction does not rewrite the source.
- The full-text index, the embedding, an export, a restored backup, and the SQLite `-wal` and `-shm` files.
- A killed import's `.vault.db.restore.*.tmp` working copy in the data directory. The next import into that vault, or the next open of an existing vault, now removes that copy, unless a restore is still running in the directory. On a vault that was never created, only the next import into that path removes it. The sentinel search reads the data directory's files, so a copy that is still there is a hit.

## What happens in each place today

A redaction of one memory was run through `redact_memory_bundle` on SQLite.

- The memory's `canonical_text` and `title` became `[REDACTED]`.
- A second memory whose text was two claims, one from the redacted memory and one from a memory that stayed, still held both claims after the redaction.
- The redaction did not ask for the 10,000 other derived rows in a second vault. Removing one memory took 0.0016 seconds. Inserting those 10,000 rows took 0.56 seconds. Finding every row whose metadata named one id, with `instr` over 10,000 rows, took 0.0023 seconds and found all 10,000. Reading the metadata in Python took 0.0073 seconds.

So the memory itself is erased. A report that copied it is not. The cost of finding the copies in a vault of 10,000 derived rows is a few milliseconds, not the cost of rewriting them.

## Two ways to treat a report

The report was one memory whose text was:

`CLAIM-A keel price is 520`
`CLAIM-B larkspur seats 40`

CLAIM-A's memory was redacted. CLAIM-B's memory was not.

- Leave the report whole, which is what the product does. CLAIM-A was still in the report. CLAIM-B was still in the report. Unsupported text left behind: the whole CLAIM-A line. Supported text lost: none.
- Rebuild the report from the inputs that remain. CLAIM-B is still stored on its own memory, so a rebuild can keep CLAIM-B and drop CLAIM-A. Supported text lost: none, if the surviving input is still readable. Unsupported text left behind: none.
- Finer provenance, one line per input. Dropping only the line whose input was removed keeps CLAIM-B and drops CLAIM-A, the same result as the rebuild for this report. Repeating that split 100,000 times took 0.031 seconds. Repeating "keep only the surviving claim" 100,000 times took 0.003 seconds. The difference is not the vault cost. The vault cost is finding the rows, which was 0.0023 seconds for 10,000 rows, and then writing each report that names the input.

A report of reports has the same choice one level up. Leaving it whole leaves CLAIM-A wherever a copy printed it. Rebuilding it whole drops every claim in that report, including CLAIM-B, unless the rebuild reads the surviving inputs again. Finer provenance drops only the claim whose input is gone.

## Inferential recovery: a third outcome

Containment and text removal both ask where the redacted words are. A third question is different: can the secret still be rebuilt from evidence that stays readable. A removal can pass the first two and fail the third, because the words are gone and the facts around them are not. Normal recall can also miss that evidence. It ranks what matches the question, and a clue does not repeat what the question says. P1 scores three outcomes, each on its own:

- Containment: a caller who may not read the redacted memory gets none of its words from any read door, including every report built from it. v0.20.2 does this by restricting such reports to the owner and an unbound admin key.
- Text removal: none of the places listed above that the operation covers still stores the words.
- Inferential recovery: using only evidence that survives and that the caller may read, the secret can still be worked out.

### A synthetic case

Everything here is invented. A fictional company holds a secret value: the reserve price of a tender called Keel is 520. People also call the tender "the harbor bid". The vault holds these rows, all linked to the one Keel entity, and a few hundred unrelated notes so that recall has something to rank.

| Row | Says | Holds the value 520 | Role |
| --- | --- | --- | --- |
| D | The Keel reserve price is 520. | yes | The direct disclosure. This memory is redacted. |
| C1 | For the harbor bid, our walk-away figure is last year's floor plus 8 percent. | no | Indirect clue: the rule. |
| C2 | Last year's floor on the harbor bid was 481.5. | no | Indirect clue: the base. |
| C3 | Finance rounds every walk-away figure to a whole number. | no | Indirect clue: the rounding. |
| C4 | A daily brief that copies C1 and C2: for the harbor bid, walk-away is 481.5 plus 8 percent. | no | A derived row. Its recorded inputs are C1 and C2, not D. |

A fixed solver computes 481.5 times 1.08, which is 520.02, and rounds it to 520. It reads the text of whatever rows it is handed. It returns 520 only when it has the rule, the base and the rounding, or C4 and the rounding.

The operation redacts D. It declares its scope: D, the rows whose recorded inputs name D, the stores it names, and the caller classes it covers. C1 to C4 are not rows whose inputs name D, so the operation leaves them alone. This is measurement only. It does not delete a related fact, and it does not restrict one.

Two things are measured afterwards.

1. Normal recall. As a caller who may read C1 to C4, ask the question that D answered ("What is the Keel reserve price?"), and then one question for each clue. Record which of D, C1, C2, C3 and C4 come back, whether any returned text holds 520, and whether the solver reaches 520 from the returned text.
2. An owner-side check of the surviving evidence. As the owner, list every surviving row inside the declared scope that shares an entity, a project, a source or a time window with D, with its recorded inputs. Run the same solver on that list.

The question shares no key word with C1 to C4, because they say "harbor bid" and "walk-away figure" where D says "Keel" and "reserve price". Keyword recall is therefore expected to miss them for the direct question. A vector stage may return some, and P1 records what each stage returns. If recall returns no clue, or too few for the solver, recall says the removal is safe while the check shows that 520 can be rebuilt. The gap between the two lists is the finding. No number for the gap is claimed here. P1 measures it.

### How P1 acceptance scores the three outcomes

- Each outcome has its own result and its own count. There is no single "removed" flag, because one flag would hide the third outcome.
- All three are scored inside the scope the operation declares. A row, store or caller class outside it is listed as outside the scope and is not counted as a failure.
- Containment passes when no read door returns D's words to any caller class in scope.
- Text removal passes when a search of every store in scope, including the SQLite `-wal` and `-shm` files and any working copy in the data directory, finds no copy of D's words.
- Inferential recovery is reported, not passed by deleting anything. It records the clue rows that normal recall reached, the clue rows the owner-side check listed, and whether the solver rebuilt the secret from each list. Acceptance asks that the check lists every surviving clue in scope (four of four in the synthetic case) and that both results are recorded, so a gap between them is visible. It does not ask that the secret be impossible to rebuild.
- A clue in a store the operation did not declare is listed as outside the scope.

A solver for one invented rule shows that the measurement can find a case built to be rebuilt. It does not show that clues in a real vault are found, and this note does not claim that.

## Order of the work, and a crash

Not built. Proposed order, one redacted memory at a time:

1. Mark the memory `removal_started` and record the id. The text of the memory, its revisions, and its events is replaced with `[REDACTED]` in this step, as it is today.
2. Find every derived row whose recorded inputs name that id. The SQL scan above is that find.
3. For each such row, apply the ruling: leave it, rebuild it, or drop only the claims that name the removed input. Write the row. Record the id in `removal_done` for that memory.
4. Clear `removal_started` when the list is finished.

A crash after step 1 and before step 4 leaves `removal_started` set. The next run reads that mark and continues from the rows not yet in `removal_done`. It does not restore the memory text, because step 1 already replaced it and the revisions were scrubbed with it.

## What undo can restore

Undo cannot put the memory text back. The revisions and the events are scrubbed in the same redaction, so there is no stored copy of the old text on the memory. Undo can only matter for a report that was left whole or rebuilt: there is nothing on the memory to copy back into the report. A ruling that wants undo to restore a report has to keep the old report text somewhere undo can read, which is another copy of the words.

## The choice that stays open

The choice between restricting a derived row whole, regenerating it, and claim-level provenance stays open until P1 is built. P1 waits for a keyed deployment. Nothing here decides it. For a report with two claims that do not depend on each other:

- Restrict the derived row whole. The unsupported claim stays. The supported claim stays. Both are visible only to the owner and an unbound admin key. This is v0.20.2. Finding the rows took 0.0023 seconds over 10,000 rows.
- Regenerate the row from the inputs that remain. The unsupported claim goes. The supported claim stays if its input is still readable. A claim that lived only inside the report, with no surviving input, goes too. The cost of finding the rows is the same. The cost of regenerating a real report was not measured. Inserting 10,000 rows took 0.56 seconds, which is the nearest figure.
- Keep claim-level provenance and drop only the claim whose input was removed. The visible result is the same as a regeneration when each claim has its own input. A report of reports can drop one claim and keep the other. It needs a provenance record per claim. Splitting a report 100,000 times took 0.031 seconds, and keeping only the surviving claim 100,000 times took 0.003 seconds.

None of the three changes the third outcome. Each can drop the redacted claim from a report and leave C1 to C4 where they are. The measurements above are the cost of finding the rows and the text each choice leaves in the two-claim report. They were taken on SQLite. No PostgreSQL costs were measured.
