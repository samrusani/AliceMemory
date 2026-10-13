# What changed and why

An agent or a person asks about one topic: an account, a price, a feature, a policy. The answer is the history of decisions and changes on that topic. This note is the shape of that answer. It does not add the tool, the route, or the view.

## Response shape

The current step is first. The history follows, oldest to newest, and it includes that same current step so the chronology is whole.

```json
{
  "topic": "Keel sensor kit price",
  "current": {
    "date": "2025-11-18",
    "change": "the list price was cut to 520 ledger units",
    "replaced": "the list price decision of 2 June 2025",
    "sources": [
      {
        "source_id": "source id",
        "title": "Keel sensor kit list price cut",
        "date": "2025-11-18"
      }
    ]
  },
  "history": [
    {
      "date": "2025-01-15",
      "change": "the list price was set at 480 ledger units",
      "replaced": null,
      "sources": []
    }
  ]
}
```

`current` is the newest step the caller may read. `history` is every step the caller may read, oldest first. `replaced` is the earlier decision this step put aside, or null when the step names none. `sources` are the notes behind the step. A topic with no visible step returns `current: null` and `history: []`.

## How a change is identified

A change is one of two records that already exist.

1. A memory whose type is `decision`. Its date is `valid_from` when that field is set, otherwise the date of the source it cites, otherwise the day it was captured. `superseded_by` is the decision that replaced it. The read follows that pointer for at most 64 hops, the bound `supersession_would_cycle` already uses, and stops.

2. A source note, when the vault holds the note and no decision memory yet. The company file is notes. A note is a change when its text is a decision, a pricing change, or a status note. Its date is the date line in the note, which the importer stores in the source text. `replaced` is set when a later note on the same topic says it replaces that earlier decision, using the sentence in the note. Two notes that share a topic and do not say that one replaces the other stay two steps, and neither is marked as the replacement.

No new table and no new column. The source text, the decision memory, and `superseded_by` already hold the date, the change, and the replacement.

## How sources are cited

Each step lists the source id, the title, and the date. A decision memory cites the sources already named in its provenance (`source_refs`). A step that is itself a source cites that source row. The citation is the id and the title. It does not copy a passage from a source the caller may not read.

## Permissions

The read uses the same checks as recall. The caller's key supplies the ceiling. A step above that ceiling is left out. The history has no gap marker and no count of the steps that were left out. `history` length is the number of steps returned. A topic that is missing and a topic whose every step is hidden both return `current: null` and `history: []`.

A support key does not see an HR note or a pricing draft. A sales key sees a pricing draft and does not see an HR note. A leadership key sees both. The owner and an unbound admin key see every step.

## Doors

The same answer on three doors, when this is built:

- an MCP tool, `alice_what_changed`, taking the topic;
- an HTTP route, `POST /v0/vnext/changes`, taking the topic;
- a view on the continuity page of the web console, which calls that route and does not keep a second copy of the history.

## Cost on a 10,000-note vault

The read does not walk every note. It uses the existing recall index to gather candidate decision memories and sources, and it judges at most 128 of them, the cap other list reads already use (`READABLE_SCAN_CAP`). It then follows `superseded_by` for at most 64 hops. The cost is one index lookup plus those 128 judgments and that short walk. It does not grow by reading the rest of the 10,000 notes. This note does not report a timed figure for that vault.

When 128 candidates have been judged, the answer is the visible steps among them. It does not report how many further notes matched.

## Tests, when this is built

The planted histories in the synthetic company come back in order with their sources. Each team key sees only its own share: the support key is refused the HR pay record and the draft price, the sales key sees the draft price and is refused the HR pay record, and the leadership key sees both. The owner and an unbound admin key see the full history.
