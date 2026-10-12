"""The changelog and the pages say what source deletion does to graph edges and what a write door accepts, as main-only.

Both changes are on main and not in v0.20.0, so every sentence that states them carries the marker
``Unreleased (on main, not in v0.20.0):`` once, and the page keeps its v0.20.0 reading around it.

Mutations, each one alone: delete the marker from an entry or a paragraph; delete the clause that names the edges the
command left open (``left every other edge open``); delete the clause that says no key reads an edge on SQLite; delete the
limit (`64 levels`) or the non-finite numbers from the changelog entry or from ``mcp-tools.md``; delete the sentence of
``threat-model.md`` that names `finite_number`; put back the claim that v0.20.0 and main stored a ref nested about 950 levels over HTTP or MCP (only a direct call of the
commit service did); delete the sentence that says a write is never run twice, the one that says the second attempt does not
write the audit events again, or the one that says a write of a row the decoder cannot read writes the placeholder back. Each fails the
test named for it.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"


def _flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def _entries(starts: str) -> list[str]:
    lines = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    return [" ".join(line.split()) for line in lines if line.startswith(f"- {MARK} {starts}")]


def test_the_changelog_entry_for_source_edges_names_the_old_behaviour_and_the_keys() -> None:
    entries = _entries("on SQLite, `sources delete`, `sources prune --superseded` and `import-markdown --supersede` close and blank every graph edge")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.count(MARK) == 1
    for clause in (
        "at either end, whatever the edge type and the type written for the end",
        "`urn:uuid:`",
        "An edge that names the source or one of its chunks only in its explanation or metadata is blanked and keeps its link.",
        "or an open loop that the command dismisses and blanks, goes with it",
        "each edge once",
        "`graph_edges`",
        "A folder import makes the pass when its first file is replaced, so a folder that replaces nothing reads no edge.",
        "These commands are new on main, so v0.20.0 has no deletion to correct",
        "left every other edge open with its explanation and metadata",
        "`alice-memory import` restores an edge of any type and shape",
        "No key can read one on SQLite",
        "which a configured `ALICE_AGENT_API_KEY` removes",
        "`entity_relationship_events` keeps its `source_id`",
        "An alias that an entity learned from the source",
    ):
        assert clause in entry, clause


def test_the_changelog_entry_for_the_write_limits_names_the_limits_and_what_was_wrong() -> None:
    entries = _entries("JSON that a caller hands a write door to be stored nests at most 64 levels")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.count(MARK) == 1
    for clause in (
        "holds no `NaN`, `Infinity`, `-Infinity` or number too large for a float (`1e999`)",
        "A memory commit measured the serialized size",
        "A direct call of the commit service stored an entry nested about 950 levels deep (in v0.19.2, in v0.20.0 and on main until now)",
        "v0.19.2 stored an entry nested up to about 200 levels and answered a server error from about 255 on",
        "the MCP commit tool takes strings only",
        "in v0.20.0 and on main until now a stored value nested near the interpreter's recursion limit",
        "`GET /v0/vnext/memories/{id}/audit`",
        "stored it as `null` without a word",
        "type `json_too_deep`",
        "type `finite_number`",
        "A test walks every model that can arrive in a request body, the nested ones included",
        "Each entry of the `source_refs` of an agent output is held to 64 levels",
        "decoded by `loads_stored` on both stores",
        "which is `json.loads` and costs what it costs",
        "an MCP tool call that writes nothing but the audit events of its policy check (the tools marked read-only, and `alice_open_loops` with its `list` action)",
        "The retry is once for the whole call",
        "so a retried call leaves the events an ordinary call leaves",
        "`[nested too deeply]`",
        "A request or tool call that may have written is never run twice",
        "writes the placeholder back into the row and into the audit event it appends",
        "seven levels over the whole SQLite unit suite",
        "raised `RecursionError` and stopped",
        "No migration is required.",
    ):
        assert clause in entry, clause


def test_the_importers_page_states_the_edge_rule_as_main_only() -> None:
    page = _flat("docs/integrations/importers.md")
    start = page.index("An edge names the source when the source, or one of its chunks, is at either end")
    paragraph = page[start : page.index("Each scrub enables SQLite secure deletion")]
    assert paragraph.count(MARK) == 1
    before, after = paragraph.split(MARK)
    assert "closed, and its explanation and metadata are blanked" in before
    assert "keeps its link" in before
    assert "`graph_edges`" in before and "`mention_edges` counts the mention edges among them" in before
    assert "or an open loop that the command dismisses and blanks" in before
    assert "makes that pass when its first file is replaced, so a folder that replaces nothing reads no edge" in before
    assert "earlier builds of this command, which is new on main" in after
    assert "A key cannot read an edge on SQLite" in after
    assert "`entity_relationship_events` is append-only" in after
    assert "the alias `dr alice marlow` for `Alice Marlow`, stays on the entity" in page


def test_the_mcp_tools_page_states_the_write_limits_as_main_only() -> None:
    page = _flat("docs/alpha/mcp-tools.md")
    start = page.index(f"{MARK} JSON that a caller hands a tool, the HTTP API or the command line to be stored nests at most 64 levels")
    paragraph = page[start : page.index("A correction refuses a body, provenance, replacement body, or replacement provenance")]
    assert paragraph.count(MARK) == 1
    for clause in (
        "`NaN`, `Infinity`, `-Infinity` or number too large for a float (`1e999`)",
        "A tool answers `invalid_request` before it runs",
        "answers HTTP 422 on every route",
        "it nests past 256 levels is an invalid request, and the server goes on",
        "direct call of the commit service stored a ref nested about 950 levels deep (in v0.19.2, in v0.20.0 and on main until now)",
        "and the MCP commit tool takes strings only",
        "is run once more with every stored column cut to 256 levels",
        "The second attempt does not write those audit events again",
        "`alice_open_loops` with its `list` action",
        "the part below is one placeholder string",
        "A call that may have written is never run twice",
        "writes the placeholder back into the row and into the audit event",
    ):
        assert clause in paragraph, clause


def test_the_threat_model_names_the_finite_number_error_as_main_only() -> None:
    page = _flat("docs/security/threat-model.md")
    start = page.index("with a validation error of type `json_too_deep` and nothing from the body in it.")
    sentence = page[start : start + 600]
    assert sentence.count(MARK) == 1
    assert "refused the same way, with an error of type `finite_number`" in sentence
    assert "held to 64 levels where the product stores it" in sentence
