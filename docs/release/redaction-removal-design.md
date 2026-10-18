# Redaction removal: design note

This note is for a ruling. It does not change redaction. v0.20.2 replaces a redacted memory's own text with `[REDACTED]` and restricts reports built from it to the owner and an unbound admin key. The words stay in those reports. Full removal waits on the ruling below.

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
- A killed import's `.vault.db.restore.*.tmp` working copy in the data directory. The next import or open now removes that copy. The sentinel search reads the data directory's files, so a copy that is still there is a hit.

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

## Order of the work, and a crash

Not built. Proposed order, one redacted memory at a time:

1. Mark the memory `removal_started` and record the id. The text of the memory, its revisions, and its events is replaced with `[REDACTED]` in this step, as it is today.
2. Find every derived row whose recorded inputs name that id. The SQL scan above is that find.
3. For each such row, apply the ruling: leave it, rebuild it, or drop only the claims that name the removed input. Write the row. Record the id in `removal_done` for that memory.
4. Clear `removal_started` when the list is finished.

A crash after step 1 and before step 4 leaves `removal_started` set. The next run reads that mark and continues from the rows not yet in `removal_done`. It does not restore the memory text, because step 1 already replaced it and the revisions were scrubbed with it.

## What undo can restore

Undo cannot put the memory text back. The revisions and the events are scrubbed in the same redaction, so there is no stored copy of the old text on the memory. Undo can only matter for a report that was left whole or rebuilt: there is nothing on the memory to copy back into the report. A ruling that wants undo to restore a report has to keep the old report text somewhere undo can read, which is another copy of the words.

## Ruling asked

For a report with two claims that do not depend on each other:

- Leave the report whole. The unsupported claim stays. The supported claim stays. This is v0.20.2.
- Rebuild the report from the inputs that remain. The unsupported claim goes. The supported claim stays if its input is still readable. A claim that lived only inside the report, with no surviving input, goes too.
- Keep finer provenance and drop only the claim whose input was removed. Same visible result as a rebuild when each claim has its own input. A report of reports can drop one claim and keep the other.

The measurements above are the cost of finding the rows, and the text each choice leaves in the two-claim report.
