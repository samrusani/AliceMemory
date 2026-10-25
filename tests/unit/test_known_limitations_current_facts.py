"""The facts that only the known limitations page states are pinned, and the numbers on it are the ones the code enforces.

The page is a short list of what is limited now in v0.21.0. Most of its statements are pinned next to the pages that
explain them (the local-folder scan, the HTTP edge, the expiry and commit text bullets and the rest). A bound that
another page owns is one short sentence on the page and a link, and the page that owns it holds the rest and is pinned
here, so the fact is not lost when it leaves the page:

* the file size limits of the importers, and the memory a ChatGPT export takes (about 6 to 9 times its size), which the
  importers page states;
* the query bounds (distinct terms and bytes), which "Size bounds" in ``mcp-tools.md`` states with the rules for
  counting them, and that they apply to the SQLite vault only;
* the three codes that tell a refusal from a failure, and the code each of them is;
* the residuals of the cited-source fence (the owner dependency trace reading the first id only, `provenance_count`, a
  link or id saved before v0.20.2, the write-time fence of an `admin_agent` writer);
* the Postgres doctor reading no chunk text;
* what an export leaves out and what an import never does, which the backup guide states;
* what SQLite source deletion keeps, which the importers page states;
* the HTTP body cap and the local-folder scan limits, whose numbers are read from the code;
* the doors the cited-source fence does not reach, on the ``Not covered`` list of "Saved quotes" in ``mcp-tools.md``
  (memory proposals, the agent-output ingest and the provenance links of artifacts).

A number is read from the code that enforces it (the importer limits, the search limits, the coded error set, the body
cap and the folder scan limits), so a change of the limit that leaves the page behind fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from alicebot_api.config import DEFAULT_MAX_CONNECTOR_SYNC_BODY_BYTES, DEFAULT_MAX_REQUEST_BODY_BYTES
from alicebot_api.importer_paths import DEFAULT_MAX_CHATGPT_EXPORT_BYTES, DEFAULT_MAX_TEXT_FILE_BYTES, MIB
from alicebot_api.mcp.types import MCP_CODED_ERROR_CODES
from alicebot_api.source_search_limits import SOURCE_SEARCH_QUERY_MAX_BYTES, SOURCE_SEARCH_QUERY_MAX_PATTERNS
from alicebot_api.vnext_connectors import (
    MAX_LOCAL_FOLDER_FILE_BYTES,
    MAX_LOCAL_FOLDER_FILES,
    MAX_LOCAL_FOLDER_TOTAL_BYTES,
)

ROOT = Path(__file__).resolve().parents[2]
FROM_V0210 = "from v0.21.0,"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(path: str) -> str:
    return _flat((ROOT / path).read_text(encoding="utf-8"))


def _page() -> str:
    return _read("docs/alpha/known-limitations.md")


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


def _under(heading_start: str, bullet: str) -> bool:
    """Whether ``bullet`` stands after the paragraph that starts with ``heading_start`` and before the next lead-in."""

    raw = (ROOT / "docs/alpha/known-limitations.md").read_text(encoding="utf-8")
    start = raw.index(heading_start)
    following = [raw.find(lead, start + 1) for lead in ("\nPostgres stack and outbound calls", "\nOpen in v0.21.0")]
    end = min(position for position in following if position != -1)
    return _flat(bullet.removeprefix("- ")) in _flat(raw[start:end])


def test_the_import_bullet_gives_the_limits_the_importers_enforce_and_the_importers_page_holds_the_rest() -> None:
    """A file over the limit is refused, and the importers page states the memory cost and what the limit leaves open.

    The sizes are read from ``importer_paths``. The page keeps one sentence and a link. The memory cost (about 6 to 9
    times the size of a ChatGPT export, from the measurement in the comment above ``DEFAULT_MAX_CHATGPT_EXPORT_BYTES``), the
    rule that a folder is not limited as a whole, and the three file commands that take the same limit are on the
    importers page.

    Mutations, each one alone: on the page, change ``512 MiB`` to ``256 MiB``, ``16 MiB (Markdown)`` to ``8 MiB
    (Markdown)`` or delete ``import_file_too_large``; on the importers page, delete ``That is a peak of about 6 to 9 times
    the size of the export.``, ``a folder is not limited as a whole`` or the sentence about the three file commands; in
    ``importer_paths.py``, set ``DEFAULT_MAX_CHATGPT_EXPORT_BYTES`` to ``256 * MIB``.
    """

    bullet = _bullet("the Markdown and ChatGPT imports read each file whole")
    text_mib = DEFAULT_MAX_TEXT_FILE_BYTES // MIB
    export_mib = DEFAULT_MAX_CHATGPT_EXPORT_BYTES // MIB
    assert (
        f"so a file over {text_mib} MiB (Markdown) or {export_mib} MiB (ChatGPT export) is refused with "
        "`import_file_too_large` unless `--max-file-mib N` raises the limit"
    ) in bullet
    assert bullet.endswith("([importers](../integrations/importers.md))")
    importers = _read("docs/integrations/importers.md")
    assert (
        f"the limit is per file: {text_mib} MiB for a Markdown file, {export_mib} MiB for a ChatGPT export"
    ) in importers
    assert "That is a peak of about 6 to 9 times the size of the export." in importers
    assert "a folder is not limited as a whole" in importers
    assert (
        "`alicebot vnext sources capture-file`, `alicebot vnext connectors browser-clipper capture --file` and "
        f"`alicebot vnext agents ingest-output --file` read their file with the same read: once, up to {text_mib} MiB"
    ) in importers


def test_the_query_bullet_gives_the_bounds_the_search_enforces_and_size_bounds_holds_the_rules() -> None:
    """The distinct terms and the bytes, on the SQLite vault only, with the rules for counting them on "Size bounds".

    The number of terms is one fewer than the number of patterns, because the pattern count includes the phrase (the
    comment in ``source_search_limits.py``). The bytes are ``SOURCE_SEARCH_QUERY_MAX_BYTES``. The page sits under the
    SQLite lead-in, which is how it says the limit applies to the SQLite vault only. "Size bounds" holds that the bytes are
    counted raw and again after case folding, that the query is never cut to fit, that the refusal is
    ``invalid_request`` with a message that names the limit, and that the Postgres backend has no such limit.

    Mutations, each one alone: on the page, change ``499`` to ``500``, either ``40,000`` to ``50,000``, delete ``several
    seconds`` or move the bullet under the Postgres lead-in; on "Size bounds", delete ``The bytes are counted as sent and
    again after case folding``, ``It is never cut to fit`` or ``The Postgres backend has no such limit and is unchanged``;
    in ``source_search_limits.py``, set ``SOURCE_SEARCH_QUERY_MAX_BYTES`` to ``50_000``.
    """

    bullet = _bullet("`alice_recall` and `alice_context_pack` refuse a query")
    terms = SOURCE_SEARCH_QUERY_MAX_PATTERNS - 1
    size = f"{SOURCE_SEARCH_QUERY_MAX_BYTES:,}"
    assert (
        f"`alice_recall` and `alice_context_pack` refuse a query of more than {terms} distinct search terms or {size} "
        f"UTF-8 bytes, and `alice_resume` and `alice_recent_decisions` one over {size} bytes"
    ) in bullet
    assert "a query inside the limit can still take several seconds on a vault with thousands of sources" in bullet
    assert bullet.endswith("([Size bounds](mcp-tools.md#size-bounds))")
    assert _under("SQLite mode (", bullet)
    tools = _read("docs/alpha/mcp-tools.md")
    assert "The bytes are counted as sent and again after case folding" in tools
    assert "It is never cut to fit, and no search runs first." in tools
    assert "A longer query is refused with `invalid_request` and a message that names the limit." in tools
    assert "The Postgres backend has no such limit and is unchanged." in tools
    assert "3.7 seconds at 499 distinct terms, against 0.30 seconds for two words" in tools


def test_the_error_bullet_names_the_generic_codes_and_the_three_that_tell_a_refusal_apart() -> None:
    """Three codes tell a refusal from a failure, they are the coded errors the server can send besides ``invalid_request``,
    and the three generic codes come before them.

    The three are read from ``MCP_CODED_ERROR_CODES``: the four coded errors less ``invalid_request``, which a query over a
    size limit already got in v0.20.0. The page is written for v0.21.0 and carries no marker.

    Mutations, each one alone: on the page, delete ``precondition_failed`` from the list, delete ``except
    `invalid_request` for a rejected argument or an oversized query``, move a generic code after the three or put
    ``From v0.20.2,`` back; in ``mcp/types.py``, change the code of ``MCPPreconditionFailedError``.
    """

    bullet = _bullet("tool failures over stdio return a generic code")
    generic, _, refusals = bullet.partition("except `invalid_request`")
    assert refusals
    for code in ("tool_not_found", "tool_request_failed", "tool_execution_failed"):
        assert f"`{code}`" in generic, code
    assert "return a generic code and no detail" in generic
    assert "for a rejected argument or an oversized query" in refusals
    new_codes = sorted(MCP_CODED_ERROR_CODES - {"invalid_request"})
    assert new_codes == ["not_found", "not_permitted", "precondition_failed"], new_codes
    assert "`not_permitted`, `not_found` and `precondition_failed`, which tell a refusal from a failure" in refusals
    for code in new_codes:
        assert f"`{code}`" not in generic, code
    assert "From v0.20.2" not in bullet
    assert bullet.endswith("([error codes](mcp-tools.md#error-codes))")


def test_the_cited_source_bullet_states_the_residuals_the_fence_leaves() -> None:
    """The residuals no other page lists in one place, and the two limits of v0.20.2 and v0.21.0 for the ref shapes.

    The owner dependency trace reading the first id of a multi-id ref only, ``provenance_count``, a link or id saved
    before v0.20.2 and the write-time fence of an ``admin_agent`` writer are on the page and in the changelog entry on
    saved quotes. The write-time fence of an ``admin_agent`` writer is also in ``mcp-tools.md``, which this test reads, so
    the two agree. The doors that answer ``not_found`` are the protection, and ``mcp-tools.md`` states them, so the page
    does not.

    Mutations, each one alone: on the page, delete the marker, ``the first id of a multi-id ref only``,
    ``provenance_count``, ``so `alice_explain` fails for it`` or ``a lower ceiling``, or change ``a lower ceiling`` to ``a
    higher ceiling``; in ``mcp-tools.md``, change ``fails for the keys of that project with a lower ceiling``.
    """

    residual = _bullet("the cited-source fence has gaps")
    assert residual.count(FROM_V0210) == 1
    assert residual.index("memory proposals") < residual.index(FROM_V0210)
    assert "memory proposals and the agent-output ingest store `source_refs` without checking the ids" in residual
    # What the sentence says of v0.20.2 and of v0.21.0, so it is not read as the behaviour of v0.21.0 alone.
    assert (
        f"(v0.20.2 kept an object entry and JSON text whole; {FROM_V0210} only a string that is not blank and not JSON text)"
    ) in residual
    assert "the owner dependency trace lists the first id of a multi-id ref only" in residual
    assert "`provenance_count` counts a withheld link" in residual
    assert (
        "A memory or open loop saved before v0.20.2 keeps its link, so `alice_explain` fails for it, and an `admin_agent` "
        "writer's link makes it fail for keys of a project with a lower ceiling"
    ) in residual
    assert residual.endswith("([Cited sources](mcp-tools.md#cited-sources))")

    tools = _read("docs/alpha/mcp-tools.md")
    assert "`alice_explain` of that memory then fails for the keys of that project with a lower ceiling" in tools
    assert "answers the same refusal with 404 and the public `not_found` error" in tools
    assert "answers 404 with the public `not_found` error" in tools


def test_the_doctor_bullet_says_the_postgres_doctor_reads_no_chunk_text() -> None:
    """The Postgres doctor reads source rows and ``raw_text`` only, and the SQLite doctor reads chunk text.

    The backup guide states the SQLite half; only the page states the Postgres half, so it is pinned here.

    Mutations, each one alone: change ``not chunk text`` to ``and chunk text``; delete ``where `alice-memory doctor` on
    SQLite reads chunk text``; in the backup guide, delete ``reads source chunk text as well as the source row``.
    """

    bullet = _bullet("the Postgres doctor")
    assert "(`alicebot vnext doctor`) reads source rows and `raw_text` only, not chunk text" in bullet
    assert "a credential only in a source chunk is not found there" in bullet
    assert "where `alice-memory doctor` on SQLite reads chunk text" in bullet
    assert "`alice-memory doctor` reads source chunk text as well as the source row" in _read(
        "docs/alpha/backup-and-restore.md"
    )


def test_the_export_bullet_names_the_rules_and_the_backup_guide_holds_the_rest() -> None:
    """Plaintext, no vectors, no overwrite and the rows that are not portable are on the page; the guide states each.

    The facts moved to the guide are pinned there: the portable set omits users, agent identities and API keys,
    embedding vectors and soft-deleted content; a portable file holds no vectors, so ``alice-memory reindex-embeddings``
    restores vector search; ``--mode skip`` skips only field-for-field identical ids and ``--mode fail`` aborts on every
    collision; a column the file row does not carry is not compared; and the guide says the export is sensitive
    plaintext and that a deep JSON column ends the export with ``export_failed``.

    Mutations, each one alone: on the page, delete ``import never overwrites an existing row``, ``embedding vectors are not
    exported`` or ``agent identities``; in the backup guide, delete ``agent identities and`` from the omitted rows, ``Portable
    JSONL does not contain embedding vectors``, ``aborts on every collision`` or ``The JSONL contains sensitive plaintext``.
    """

    bullet = _bullet("exports contain plaintext memory and source content")
    assert "so protect and encrypt copies" in bullet
    assert "embedding vectors are not exported (run `alice-memory reindex-embeddings` after an import)" in bullet
    assert "import never overwrites an existing row" in bullet
    assert "and users, agent identities, API keys and soft-deleted rows are not portable" in bullet
    assert (
        "A vault with JSON text nested about 1,000 levels or more cannot be exported (`export_failed`), and import refuses "
        "such text (`restore_failed`)"
    ) in bullet
    assert bullet.endswith("([Backup and restore](backup-and-restore.md))")
    guide = _read("docs/alpha/backup-and-restore.md")
    assert "The JSONL contains sensitive plaintext." in guide
    assert "They intentionally omit users, agent identities and API keys, embedding vectors, and soft-deleted content." in guide
    assert "Portable JSONL does not contain embedding vectors." in guide
    assert "alice-memory reindex-embeddings --db /path/to/restored-memory.db" in guide
    assert "`--mode skip` skips only field-for-field identical IDs" in guide
    assert "`--mode fail` aborts on every collision." in guide
    assert "Skip does not compare a column the file row does not carry" in guide
    assert "cannot be exported while the text is nested about 1,000 levels or more" in guide
    assert "ends with `export_failed`" in guide
    assert "This command restores a SQLite database. It is not a PostgreSQL import." in guide


def test_the_sqlite_boundaries_bullets_and_the_deletion_bullet_point_at_the_pages_that_hold_the_detail() -> None:
    """No review console, no scheduler, one user, no migration to Postgres and no agent keys are on the page; the guides state
    the migration and the key rule. SQLite source deletion keeps events, hashes, memory text and backups until the VACUUM
    step, and the importers page states each of them.

    Mutations, each one alone: on the page, delete ``no automatic migration to Postgres``, ``a set key fails closed`` or
    ``freed space keeps text until the VACUUM step``; in the agent guide, delete ``enforcement fails closed and every core tool
    call is refused, reads included``; in the importers page, delete ``Append-only source events retain old titles and
    hashes`` or ``Earlier backups and copies still hold the text``.
    """

    bullet = _bullet("no web console review")
    assert "no web console review (use `alice_memory_review` and `alice_memory_correct`)" in bullet
    for clause in ("no scheduler", "one user per database file", "no automatic migration to Postgres"):
        assert clause in bullet, clause
    assert (
        "no agent API keys (`alicebot agent keys create` requires Postgres, so leave `ALICE_AGENT_API_KEY` unset: a set key "
        "fails closed and refuses every core tool call, reads included)"
    ) in bullet
    assert _under("SQLite mode (", bullet)
    agent = _read("docs/alpha/agent-integration.md")
    assert "`alicebot agent keys create` requires Postgres. Leave `ALICE_AGENT_API_KEY` unset there" in agent
    assert "enforcement fails closed and every core tool call is refused, reads included" in agent

    deletion = _bullet("`sources delete` and `prune` keep source events")
    assert (
        "`sources delete` and `prune` keep source events, hashes, memory text and backups, and freed space keeps text until "
        "the VACUUM step"
    ) in deletion
    assert deletion.endswith("([importers](../integrations/importers.md#list-delete-and-prune-sqlite-sources))")
    importers = _read("docs/integrations/importers.md")
    assert "Append-only source events retain old titles and hashes" in importers
    assert "Every citing memory that was not redacted keeps its text" in importers
    assert "Removed text can remain in the vault file's unused space and write-ahead log until the file is rebuilt." in importers
    assert "Earlier backups and copies still hold the text." in importers
    assert 'sqlite3 <data-dir>/memory.db "VACUUM; PRAGMA wal_checkpoint(TRUNCATE);"' in importers


def test_the_postgres_bullets_name_the_numbers_the_code_enforces_and_the_threat_model_holds_the_rest() -> None:
    """The body cap and the local-folder scan limits on the page are the code's numbers; the threat model states the rules.

    The page names the 4 MiB cap (32 MiB for connector sync), the 2 MiB, 10,000 files and 64 MiB of the folder scan, the
    redirect rule, the weaknesses that stay (the cap is no rate limit, an agent key is not checked for ``Host`` and
    ``Origin``, the legacy ``/v0`` routes, a name in ``ALICEBOT_ALLOWED_HOSTS``, the hard link, the loopback and private
    address, the proxy, the response read whole) and a link to the threat model. The threat model states the rule for
    ``Host`` and ``Origin``, the 413, the folder scan, the redirect rule and that a provider that redirects must be
    configured with its final URL.

    Mutations, each one alone: on the page, change ``4 MiB``, ``32 MiB``, ``2 MiB``, ``10,000`` or ``64 MiB``; delete the
    hard link bullet; in the threat model, delete ``a provider that redirects must be configured with its final URL`` or
    ``A hard link planted in the folder to a file elsewhere is read as ordinary content``; in ``config.py``, set
    ``DEFAULT_MAX_REQUEST_BODY_BYTES`` to ``8 * 1024 * 1024``.
    """

    bounds = _bullet("the HTTP API refuses a request body")
    body_mib = DEFAULT_MAX_REQUEST_BODY_BYTES // MIB
    sync_mib = DEFAULT_MAX_CONNECTOR_SYNC_BODY_BYTES // MIB
    file_mib = MAX_LOCAL_FOLDER_FILE_BYTES // MIB
    total_mib = MAX_LOCAL_FOLDER_TOTAL_BYTES // MIB
    assert (
        f"the HTTP API refuses a request body over {body_mib} MiB ({sync_mib} MiB for connector sync) and a keyless request "
        "whose `Host` or `Origin` is not this machine's"
    ) in bounds
    assert (
        f"the local-folder scan reads at most {file_mib} MiB of a file and stops at {MAX_LOCAL_FOLDER_FILES:,} files or "
        f"{total_mib} MiB"
    ) in bounds
    assert "no provider, embeddings, reranker or fact-key call follows a redirect" in bounds
    weak = _bullet("the body cap is no rate limit")
    assert (
        "the body cap is no rate limit, a request with an agent key is not checked for `Host` and `Origin`, the legacy `/v0` "
        "routes apply the rule to every request, and a name in `ALICEBOT_ALLOWED_HOSTS` is trusted as this machine"
    ) in weak
    assert "a hard link planted inside the watched folder to a file elsewhere, as the importers do" in _bullet(
        "the local-folder scan still reads"
    )
    outbound = _bullet("the embeddings, reranker, fact-key and brain clients")
    assert (
        "can reach a loopback or private address, a request carried by a proxy is not held to the address rule, and a "
        "provider response is read whole"
    ) in outbound
    model = _read("docs/security/threat-model.md")
    assert "(a provider that redirects must be configured with its final URL)" in model
    assert "A hard link planted in the folder to a file elsewhere is read as ordinary content" in model
    assert "The cap is a bound on one request, not a rate limit." in model
    assert "A request with an agent key is not checked" in model
    assert "A provider call's response body is read whole." in model
    assert "A name the operator lists in `ALICEBOT_ALLOWED_HOSTS` is trusted as this machine." in model


def _saved_quotes_not_covered() -> str:
    """The ``Not covered:`` paragraph that ends "Saved quotes" in ``mcp-tools.md``, whitespace collapsed.

    "Saved quotes" is a ``###`` section of "Cited sources"; it ends at the next ``##`` heading.
    """

    raw = (ROOT / "docs/alpha/mcp-tools.md").read_text(encoding="utf-8")
    start = raw.index("### Saved quotes\n")
    end = raw.index("\n## ", start)
    section = raw[start:end]
    assert section.count("Not covered:") == 1
    return _flat(section[section.index("Not covered:") :])


def test_the_saved_quotes_not_covered_list_names_the_doors_the_fence_does_not_reach() -> None:
    """Proposals, the agent-output ingest and artifact links are on the page that says what is covered.

    These were only in the changelog entry (a dated record), while the limitations page sends a reader to "Saved quotes"
    for the rest of the residuals. Each is named in the ``Not covered`` list, with the tool or route it is reached by,
    and each tool and route named is one the server has, so a rename that leaves the page behind fails here. That the
    proposal route stores ``source_refs`` as sent is run by ``test_saved_quote_ref_reading_vault.py``.

    Mutations, each one alone, in ``mcp-tools.md``: delete ``POST /v0/vnext/memory-proposals`` or ``POST
    /v0/vnext/agents/ingest-output`` from the list; put back a sentence about ``alice_vnext_recent_memory_commits``;
    delete ``the provenance links of artifacts are not held to this fence``; move any of the three out of the
    ``Not covered`` list (above it). In the registry, rename ``alice_vnext_propose_memory``.
    """

    from alicebot_api.mcp.registry import _TOOL_DEFINITIONS_BY_NAME
    from alicebot_api.openapi_operation_contracts import OPENAPI_OPERATION_RESPONSE_SCHEMAS

    covered = _saved_quotes_not_covered()
    assert (
        "memory proposals (`alice_vnext_propose_memory`, `POST /v0/vnext/memory-proposals`) and the agent-output ingest "
        "(`alice_vnext_ingest_agent_output`, `POST /v0/vnext/agents/ingest-output`) store the `source_refs` they are "
        "given and check none of them"
    ) in covered
    # The legacy recent-commits tool left this list when it was held to the caller's read fence.
    assert "lists commit rows with no row-level fence" not in covered
    assert "the provenance links of artifacts are not held to this fence" in covered

    for tool in ("alice_vnext_propose_memory", "alice_vnext_ingest_agent_output", "alice_vnext_recent_memory_commits"):
        assert tool in _TOOL_DEFINITIONS_BY_NAME, tool
    for route in (("POST", "/v0/vnext/memory-proposals"), ("POST", "/v0/vnext/agents/ingest-output")):
        assert route in OPENAPI_OPERATION_RESPONSE_SCHEMAS, route
