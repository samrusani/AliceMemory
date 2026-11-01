# The showcase walkthrough

One command shows what a meeting shows:

```bash
python scripts/showcase_walkthrough.py
```

Run it from a checkout of the repository. From this change the plain command works as written: the script puts its own checkout and package source on the path. On the v0.23.1 tag the plain command stops with ModuleNotFoundError, and there it needs `PYTHONPATH=<checkout>:<checkout>/apps/api/src` or the checkout's own editable install. The measured times below were taken with that PYTHONPATH set: about 27 seconds on the v0.23.1 tag and about 23 seconds on main, 37 output lines, no warning and no error. It calls no model and reaches nothing outside the checkout: the demo vault and data directory are written under `showcase-demo-run/` next to where you run it (`--run-dir` moves it).

## What each part shows

1. **Recall.** Sales asks which service contract Larkspur Metals signed for the Keel sensor kit; the answer includes the contract identifier `QF-C-2041`. Support asks for the same agreement and gets the same stored fact: teams share what one of them learned.
2. **What changed and why.** Sales asks `alice_what_changed` about the Larkspur Metals account and about the Keel sensor kit price. The answer is the recorded chain: the current step, the recorded reason, and the history. The price history shows the June list price of 480 ledger units cut to 520, with the recorded reason for the cut.
3. **The refusal.** Support asks for Mara Quen's compensation and for the draft list price. Both answers hold none of the restricted text: support's key does not reach the HR note or the pricing draft. Leadership asking the HR question and sales asking the draft question were given the facts, so the refusal is a permission and not an empty search. The script checks that control before it prints the refusal.
4. **Benchmark summary.** With no grades file this part carries no number. It names the published result pages and links them.

## Scripted and measured

- **Scripted:** the setup. The script writes the 1,000-note company, records the price change and the account through `alice_memory_commit` and `alice_memory_correct` with each team's key, and mints the team keys. It does not extract decisions from notes; a vault of plain notes with no recorded decisions shows "No recorded decision history was found".
- **Measured:** the answers. Everything printed after the setup comes from the tools: `alice_recall` and `alice_what_changed` answers for each team's key. No model is called and no answer is generated; what you see is what a stored answer returns.
- **Not measured here:** the accuracy and leak numbers of the benchmark. The summary reads every count from the file named by `--grades` (the same file `showcase-report --grades` reads), which a benchmark run produces. The repository does not carry the recorded grades of the published runs, so a meeting run shows the published pages by link and not by number; showing the numbers again needs that grades file, not a new model run.

## The published numbers

The measured claims and their comparison arms are on [the Phase 0 baseline](../benchmarks/showcase/phase0-baseline.md) (Alice against grep, a note folder and no tool), [the Phase 1 results for v0.21.0](../benchmarks/showcase/phase1-v0210-results.md) and [the passage vectors results](../benchmarks/showcase/phase1-passage-vectors-results.md), with [what changed and why](../benchmarks/showcase/what-changed-and-why.md) describing the what-changed read the demo uses.
