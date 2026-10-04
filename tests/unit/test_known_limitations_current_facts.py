"""The facts that only the known limitations page states are pinned, and the numbers on it are the ones the code enforces.

The page is a short list of what is limited now. Most of its statements are pinned next to the pages that explain
them (the importers, the local-folder scan, the HTTP edge, the expiry and commit text bullets and the rest). These are
the ones that had no pin at all, because no other page repeats them or the page that does is not read by a test:

* the memory a ChatGPT export takes (about 6 to 9 times its size) and the two file size limits of the importers;
* the query bounds (distinct terms and bytes) and the note that they apply to the SQLite vault only;
* the three error codes that are main-only, and the code each of them is;
* the cited-source doors that answer `not_found`, and the residuals of the fence (the owner dependency trace reading
  the first id only, `provenance_count`, a link or id saved before the fix, the write-time fence of an `admin_agent`
  writer);
* the Postgres doctor reading no chunk text.

A number is read from the code that enforces it (the importer limits, the search limits, the coded error set), so a
change of the limit that leaves the page behind fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from alicebot_api.importer_paths import DEFAULT_MAX_CHATGPT_EXPORT_BYTES, DEFAULT_MAX_TEXT_FILE_BYTES, MIB
from alicebot_api.mcp.types import MCP_CODED_ERROR_CODES
from alicebot_api.source_search_limits import SOURCE_SEARCH_QUERY_MAX_BYTES, SOURCE_SEARCH_QUERY_MAX_PATTERNS

ROOT = Path(__file__).resolve().parents[2]
MARK = "Unreleased (on main, not in v0.20.0):"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _page() -> str:
    return _flat((ROOT / "docs/alpha/known-limitations.md").read_text(encoding="utf-8"))


def _bullet(start: str) -> str:
    """The one bullet of the page that starts with ``start``, whitespace collapsed."""

    raw = (ROOT / "docs/alpha/known-limitations.md").read_text(encoding="utf-8")
    found = [
        _flat(item)
        for block in re.split(r"\n\s*\n", raw)
        for item in re.split(r"\n(?=- )", block)
        if item.startswith("- " + start)
    ]
    assert len(found) == 1, (start, len(found))
    return found[0]


def test_the_import_bullet_gives_the_limits_the_importers_enforce_and_the_memory_cost() -> None:
    """A file over the limit is refused before it is read, and a ChatGPT export costs 6 to 9 times its size.

    The sizes are read from ``importer_paths``. The memory cost is stated by the page and by the comment above
    ``DEFAULT_MAX_CHATGPT_EXPORT_BYTES``, which gives the measurement.

    Mutations, each one alone: on the page, change ``512 MiB`` to ``256 MiB``, ``16 MiB (Markdown)`` to ``8 MiB
    (Markdown)``, ``6 to 9 times`` to ``2 to 3 times``, delete ``before it is read`` or ``a folder is not limited as a
    whole``; in ``importer_paths.py``, set ``DEFAULT_MAX_CHATGPT_EXPORT_BYTES`` to ``256 * MIB``.
    """

    bullet = _bullet("the Markdown and ChatGPT imports read each file whole into memory")
    text_mib = DEFAULT_MAX_TEXT_FILE_BYTES // MIB
    export_mib = DEFAULT_MAX_CHATGPT_EXPORT_BYTES // MIB
    assert (
        f"so a file over {text_mib} MiB (Markdown) or {export_mib} MiB (ChatGPT export) is refused before it is read, "
        "with `import_file_too_large`"
    ) in bullet
    assert "a ChatGPT export takes about 6 to 9 times its size" in bullet
    assert "and `--max-file-mib N` raises the limit; a folder is not limited as a whole" in bullet
    assert f"take the same {text_mib} MiB limit and `--max-file-mib N`" in bullet
    assert "[docs/integrations/importers.md](../integrations/importers.md)" in bullet


def test_the_query_bullet_gives_the_bounds_the_search_enforces_and_says_they_are_sqlite_only() -> None:
    """The distinct terms and the bytes, counted raw and after case folding, never cut, on the SQLite vault only.

    The number of terms is one fewer than the number of patterns, because the pattern count includes the phrase (the
    comment in ``source_search_limits.py``). The bytes are ``SOURCE_SEARCH_QUERY_MAX_BYTES``.

    Mutations, each one alone: on the page, change ``499`` to ``500``, either ``40,000`` to ``50,000``, delete ``counted
    raw and again after case folding``, ``the query is never cut to fit`` or ``the limit applies to the SQLite vault
    only``; in ``source_search_limits.py``, set ``SOURCE_SEARCH_QUERY_MAX_BYTES`` to ``50_000``.
    """

    bullet = _bullet("`alice_recall` and `alice_context_pack` refuse a query")
    terms = SOURCE_SEARCH_QUERY_MAX_PATTERNS - 1
    size = f"{SOURCE_SEARCH_QUERY_MAX_BYTES:,}"
    assert (
        f"`alice_recall` and `alice_context_pack` refuse a query of more than {terms} distinct search terms or {size} "
        "UTF-8 bytes, counted raw and again after case folding"
    ) in bullet
    assert f"`alice_resume` and `alice_recent_decisions` refuse one over {size} UTF-8 bytes" in bullet
    assert "each with `invalid_request` and a message that names the limit" in bullet
    assert "the query is never cut to fit, and the limit applies to the SQLite vault only" in bullet
    assert "See [Size bounds](mcp-tools.md#size-bounds)" in bullet


def test_the_error_bullet_names_the_main_only_codes_after_the_marker_and_the_server_has_them() -> None:
    """Three codes are main-only, they are the coded errors the server can send besides ``invalid_request``, and the
    v0.20.0 codes come before the marker.

    The three are read from ``MCP_CODED_ERROR_CODES``: the four coded errors less ``invalid_request``, which v0.20.0
    already sent for a size limit.

    Mutations, each one alone: on the page, change ``three more codes`` to ``two more codes``, delete ``precondition_failed``
    from the list, move the marker to the front of the bullet, or delete ``except a query over a size limit``; in
    ``mcp/types.py``, change the code of ``MCPPreconditionFailedError``.
    """

    bullet = _bullet("tool failures over stdio return a generic code")
    before, marker, after = bullet.partition(MARK)
    assert marker
    for code in ("tool_not_found", "tool_request_failed", "tool_execution_failed"):
        assert f"`{code}`" in before, code
    assert "except a query over a size limit, which returns `invalid_request` and names the limit" in before
    new_codes = sorted(MCP_CODED_ERROR_CODES - {"invalid_request"})
    assert new_codes == ["not_found", "not_permitted", "precondition_failed"], new_codes
    assert "three more codes, `not_permitted`, `not_found` and `precondition_failed`, tell a refusal from a failure" in after
    for code in new_codes:
        assert f"`{code}`" not in before, code
    assert "(see the error codes table in [docs/alpha/mcp-tools.md](mcp-tools.md#error-codes))" in after


def test_the_cited_source_bullets_state_the_doors_and_the_residuals_the_fence_leaves() -> None:
    """The doors that answer ``not_found`` on main, what v0.20.0 did, and the residuals no other page lists in one place.

    The owner dependency trace reading the first id of a multi-id ref only, ``provenance_count``, and a link or id saved
    before the fix are on the page and in the changelog entry on saved quotes. The write-time fence of an
    ``admin_agent`` writer is also in ``mcp-tools.md``, which this test reads, so the two agree.

    Mutations, each one alone: on the page, change ``not_found`` to ``not_permitted`` in the doors sentence, delete ``(404
    over HTTP)``, delete the marker of either bullet, delete ``the first id of a multi-id ref only``, ``provenance_count``
    or ``so `alice_explain` still fails for such a memory``, or change ``a lower ceiling`` to ``a higher ceiling``; in
    ``mcp-tools.md``, change ``fails for the keys of that project with a lower ceiling``.
    """

    doors = _bullet("in v0.20.0 a key bound to one project can attach a source it cannot read")
    assert doors.count(MARK) == 1
    before, after = doors.split(MARK)
    assert "can attach a source it cannot read through `alice_memory_commit` `source_refs`" in before
    assert "the `provenance` of `alice_memory_correct`" in before
    assert "over HTTP on Postgres, `POST /v0/vnext/open-loops`" in before
    assert "and a saved quote stays readable after its source is reclassified" in before
    assert "those doors answer `not_found` (404 over HTTP) for a source or memory the caller may not read" in after
    assert "the readers of a saved quote and of an open loop ask the reader's own fence again" in after
    assert (
        "a link that passes the fence of an `admin_agent` writer still makes `alice_explain` of that memory fail for "
        "the keys of a project with a lower ceiling"
    ) in after
    assert "not_found" not in before

    residual = _bullet("the cited-source fence does not cover everything yet")
    assert residual.count(MARK) == 1
    assert residual.index(MARK) < residual.index("memory proposals")
    assert "memory proposals and the agent-output ingest store their `source_refs` as given" in residual
    assert "the owner dependency trace lists a memory by the first id of a multi-id ref only" in residual
    assert "`provenance_count` still counts a withheld link" in residual
    assert (
        "a memory or open loop saved before the fix keeps its link or id, so `alice_explain` still fails for such a "
        "memory"
    ) in residual

    tools = _flat((ROOT / "docs/alpha/mcp-tools.md").read_text(encoding="utf-8"))
    assert "`alice_explain` of that memory then fails for the keys of that project with a lower ceiling" in tools
    assert "answers the same refusal with 404 and the public `not_found` error" in tools
    assert "answers 404 with the public `not_found` error" in tools


def test_the_doctor_bullet_says_the_postgres_doctor_reads_no_chunk_text() -> None:
    """The Postgres doctor reads source rows and ``raw_text`` only, and the SQLite doctor reads chunk text.

    Only the page says this, so it is pinned here.

    Mutations, each one alone: change ``not chunk text`` to ``and chunk text``; delete ``where `alice-memory doctor` on
    SQLite reads chunk text``.
    """

    bullet = _bullet("the Postgres doctor")
    assert "(`alicebot vnext doctor`) reads source rows and `raw_text` only, not chunk text" in bullet
    assert "a credential that sits only in a source chunk is not found there" in bullet
    assert "where `alice-memory doctor` on SQLite reads chunk text" in bullet
