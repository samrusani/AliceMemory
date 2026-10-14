# What changed and why

Unreleased (on main, not in v0.20.0): this page is a design. The tool, the route and the view are not built. v0.20.0 has no what-changed answer, and it has no measurement of one.

An agent or a person asks about one topic: an account, a price, a feature, a policy. The answer is the history of recorded decisions and changes on that topic. The read builds every step from records. It does not read note text to decide that a note is a decision, and it does not take `replaced` from a sentence in a later note.

## Response shape

The current step is first. The history follows, oldest to newest, and it includes that same current step so the chronology that was found is whole. The answer is the history found among the most relevant records. It is not a promise of a complete history.

Every answer, including an empty one, carries this sentence in these words: "This covers the recorded decisions the caller may read. It is not a company-wide statement."

```json
{
  "notice": "This covers the recorded decisions the caller may read. It is not a company-wide statement.",
  "topic": "Keel sensor kit price",
  "current": {
    "id": "memory id of the November decision",
    "kind": "memory",
    "chain": "memory id of the recall match that started this chain",
    "date": "2025-11-18",
    "change": "the list price was cut to 520 ledger units",
    "reason": {
      "text": "the June price was too high for the renewal",
      "source_id": "source id of the note the decision cites"
    },
    "replaced": "memory id of the June decision",
    "disagrees_with": null,
    "sources": [
      {
        "source_id": "source id",
        "title": "Keel sensor kit list price cut",
        "date": "2025-11-18"
      }
    ],
    "author": "showcase-sales",
    "review_state": "active"
  },
  "upcoming": [],
  "disputed": [],
  "history": []
}
```

`current` is the newest step the caller may read that is settled as of today. It is never filled from a step that is not settled. A step whose `valid_from` is after today goes in `upcoming`, not in `current`. Two readable steps joined by an unresolved contradiction record give `current: null` and `disputed` set to their ids. `upcoming` and `disputed` are ids of steps that also appear in `history`.

`history` is every step the caller may read, oldest first. Each step carries `id` and `kind` (`memory` or `source`), and `chain`, the id of the recall match that started its chain. `replaced` is the `id` of a step that appears in this caller's `history`, or null. It is never a description of a step the caller may not read, so a visible step that replaced a hidden one shows null. `disagrees_with` is the same kind of reference. `reason` is the recorded reason, or null. `sources` are ids, titles and dates. `author` is the agent or person the record already names. `review_state` is the status the review and promotion rules already store.

A topic with no visible step returns `current: null`, `upcoming: []`, `disputed: []`, `history: []`, the notice above, and the sentence "No recorded decision history was found", and it names `alice_recall` as the ordinary search. Those words are the same when no record matched and when every match is hidden.

## How a step belongs to the topic

The topic is free text. The read takes records in this order.

1. It runs recall over decision memories with the topic as the query, under the caller's limits. Those matches are the starts. At most 128 candidates are judged (`READABLE_SCAN_CAP`).
2. For each start the caller may read, it walks `superseded_by` and the pointer back to the decision this one replaced, for at most 64 hops, the bound `supersession_would_cycle` already uses. A repeated id stops the walk. The steps kept are one chain, marked with the start's id.
3. A source is a step only when the importer recorded a replacement (`superseded_by` on the source from `import-markdown --supersede`) and a decision in the chain cites that source, or the source is the recorded replacement of one it cites. The note text is not read to decide that the source is a change.
4. A contradiction record from `vnext_contradictions` (a `contradicts` link, or a contradiction-report candidate, naming a memory or a source and a belief's memory) marks `disagrees_with` when both ends are steps in this `history`.

Two matches that are joined by `superseded_by` are one chain. Two matches that are not joined stay two chains, each marked. Two unrelated decisions that share words with the topic both come back from recall. They stay separate chains. The answer does not join them into one history and does not drop either one.

## How a change is identified

A change is a record that already exists:

- a memory whose type is `decision`, and the `superseded_by` chain of that memory;
- a contradiction record, as above;
- a source replacement the importer recorded.

The date of a decision is `valid_from` when that field is set, otherwise the date of a source it cites that the caller may read, otherwise the day it was captured. `superseded_by` is the decision that replaced it. The read follows that pointer for at most 64 hops and stops. A chain longer than 64 contributes the steps found in those hops. A cycle contributes each step once.

No new table and no new column.

## What changed and why

`reason` is the reason stated in the decision record (`rationale`, or `confirmation_rationale` when that is the stored reason), or the reason stated in a source the decision cites when the caller may read that source. The quote is stored with that source's id. `reason` is null when no reason is stated. A model does not write it.

## Citations, evidence, author and review

Each step lists the source id, the title and the date of each source it cites. A decision memory cites the sources already named in its provenance (`source_refs`). A source step cites that source row. The citation is the id, the title and the date. It does not copy a passage from a source the caller may not read.

Every step also carries its source references, its author (the recording agent or person already on the record), and its review state under the existing review and promotion rules (the memory status and confirmation state the product already stores, including `active`, `needs_review` and `confirmed`). A decision with no source reference is shown with an empty `sources` list. Its `reason` still comes only from a recorded reason, and is null when the decision recorded none.

## What current means

`current` is the settled answer as of today among the steps the caller may read. It is the newest of those settled steps. It is never filled from a step that is not settled.

A step is settled when its review state is an accepted one (`active`, or confirmation `confirmed`), its `valid_from` is today or earlier or is unset, and it is not one of two readable steps joined by an unresolved contradiction. A contradiction record is unresolved when its review has not accepted or dismissed it (`candidate` or `needs_review`, rather than `reinforce`, `challenge`, `supersede` or `retire`).

A step that takes effect later (its `valid_from` is after today) goes in `upcoming`, not `current`. Two readable steps joined by an unresolved contradiction record give `current: null` and `disputed: [ids]`. The newer of those two is later in `history`.

None of these rules may depend on a step the caller may not read. A hidden newer step, or a hidden contradiction partner, changes nothing in the caller's answer: `current` is not cleared, `disputed` does not gain an id, and `replaced` and `disagrees_with` stay null rather than naming the hidden step.

## Permissions

The read uses the label guard recall uses: effective labels raised by inputs, domains, project binding, and unverified rows. That check applies to each step, to each cited source, and to each `replaced` and `disagrees_with` reference. A step, a source or a reference the caller may not read is left out. The history has no gap marker and no count of what was left out. `history` length is the number of steps returned.

`current` is the newest step the caller may read that is settled as of today.

A support key does not see an HR note or a pricing draft, and does not see a decision whose inputs raise it above that key. A sales key sees a pricing draft and a decision it may read, and does not see an HR note. A leadership key sees both. The owner and an unbound admin key see every step their own request admits. A key bound to one project, including a bound `admin_agent` key, sees only the steps in that project.

## When nothing is found

When nothing is found, or nothing visible is found, the answer says "No recorded decision history was found" and points to ordinary search (`alice_recall`). The same words cover both cases. A vault of plain notes with no recorded decisions gets that answer. The docs state this limit.

## Doors

The same answer on three doors, when this is built:

- an MCP tool, `alice_what_changed`, taking the topic;
- an HTTP route, `POST /v0/vnext/changes`, taking the topic;
- a view on the continuity page of the web console, which calls that route and does not keep a second copy of the history.

Each new door is registered in the operator route inventory and its probe table, in the label door registry, and in the MCP tool documentation.

## Cost on a 10,000-note vault

The read does not walk every note. It uses the existing recall index, judges at most 128 candidates, then follows `superseded_by` for at most 64 hops. When 128 candidates have been judged, the answer is the history found among them. It does not report how many further records matched, and it does not report a count or a gap caused by permissions.

When the read is built, its time is measured on the 10,000-note company next to recall's time on the same vault, and that pair of times is written here. This page does not yet have those times, because the read is not built. v0.20.0 has no such measurement. A budget test with 10,000 hidden candidates shows the 128 cap holds.

## Tests, when this is built

- The planted histories come back in order with their sources, after the setup script has recorded them through the tools below. Each team key sees only its own share.
- A key with limits gets the same answer, byte for byte, with and without hidden steps present.
- A `superseded_by` cycle, and a chain longer than 64, stop as this note says.
- An answer at the 128 cap.
- A small set of notes written by hand in plain wording that the generator does not produce. Their recorded decisions come back as well as the planted ones.
- The budget test of 10,000 hidden candidates.

## Decision

Recorded decisions, not extraction, are what this feature and the demo use.

The company's agents record decisions through the existing tools: commit a decision memory, and supersede it through correction (`alice_memory_correct`). The setup script for the synthetic company makes those calls with each team's key, through the normal APIs and permission checks. The demo, the docs and the case study describe this setup as scripted. It shows that Alice stores, retrieves, explains and protects recorded decisions. It does not show that agents recognise decisions on their own. This note does not add that script.

Extraction from imported notes with a local model is a later unit of its own. It is not started in this sprint. Before it may be claimed, it is measured on notes prepared independently of the build team, which the team does not see. The measurement covers missed decisions, invented decisions, wrong replacement links, disagreements and source attribution. Extracted decisions stay proposals until accepted under the review policy.

A decision-log import format stays on the backlog, as an option for customers who already keep decision logs.

A vault of plain notes with no recorded decisions shows "No recorded decision history was found". This limit is stated here.
