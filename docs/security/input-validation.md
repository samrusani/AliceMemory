# Input Validation And Injection Evidence

## HTTP And Structured Data

vNext request bodies inherit the shared Pydantic model configured with
`extra="forbid"`; typed fields, literals, UUIDs, length bounds, and route-specific
validators reject malformed values before persistence. New raw transports must
size-bound bytes before decoding, reject non-object JSON and unknown fields, and
then validate through the same model contract.

Metadata is treated as JSON data. Store calls serialize values through database
drivers rather than interpolating them into SQL expressions. Any operation that
selects a JSON field or ordering mode must choose from a code-owned allowlist.

## SQL Construction

The Postgres and SQLite stores bind caller values as parameters. The limited SQL
fragments assembled dynamically are code-owned column lists, fixed predicates,
or allowlisted sort/query modes; callers do not supply identifiers or SQL
syntax. SQL-shape tests pin security-sensitive predicates and generated text so
a refactor cannot silently drop user, project, lifecycle, or signature filters.

The Stage A source sweep found no confirmed SQL or JSON-path injection in the
reviewed current store paths. That is a bounded review result, not a claim that
future dynamic SQL is safe by construction.

## Full-Text Search

- PostgreSQL passes the strict query to `websearch_to_tsquery` as a bound value.
  Its match-any fallback constructs an OR expression from normalized literal
  lexemes and still binds the resulting query value.
- SQLite FTS5 query builders quote normalized tokens rather than accepting raw
  FTS syntax. Unit tests exercise quotes, operators, punctuation, and FTS5
  metacharacters for memory and source-chunk search.

SQLite adversarial FTS coverage is currently broader. Stage B should include
hostile PostgreSQL strings across strict, match-any, source, and scoped paths,
including empty/stop-word-only inputs and Unicode boundary cases.

## File And Import Paths

The SQLite portable export/import path has extensive alias, inode, symlink,
sidecar-name, immutable-snapshot, integrity-digest, unknown-schema, and atomic
publication tests. The content-directory importers have their own controls:

- Markdown recursively discovers `*.md` files;
- ChatGPT recursively discovers `*.json` files;
- OpenClaw reads named JSON files or direct-directory JSON fallbacks.

Since v0.15.2 those importers refuse a symlinked file or folder under the
selected root and a path that leaves it, open each file once without following
a link at the last component, require a regular file, and give the same text to
the archive step and to the parser. Two residuals remain, and both need local
write access inside a folder you chose. A hard link planted in the folder to a
file elsewhere is read as ordinary content, because a hard link is the file
itself. A folder on the path swapped for a symlink between the listing and the
read can redirect the read, because only the last component is opened without
following a link. Import only from a folder you control.

Corrected 2026-10-02: this section said the importers could include a symlinked
member outside the selected root and that the archive step and the parser could
read the source at different times, as a finding deferred from the Phase 5.1
carrier. v0.15.2 fixed both, and the release notes of v0.15.2 and v0.15.3 say
so. The Phase 5.1 evidence file keeps the finding as it was recorded then.

## Focused Evidence

```bash
./.venv/bin/pytest -q \
  tests/unit/test_importers.py \
  tests/unit/test_sqlite_onramp.py \
  tests/unit/test_vnext_store.py \
  tests/unit/test_sqlite_store.py \
  tests/integration/test_source_content_retrieval_postgres.py \
  tests/integration/test_vnext_fts_fallback_postgres.py
```

Run role-separated PostgreSQL tests against the supported database/pgvector
version. Do not treat a skipped integration suite as proof.
