"""BM25 over ordinary readable rows, using connection-local FTS vocabulary."""

from __future__ import annotations

import math
import re
from typing import Any


def _tokens(blob: bytes) -> int:
    """Decode the existing FTS5 column-size varints; never change the index."""
    total = value = 0
    for byte in blob:
        value = (value << 7) | (byte & 127)
        if byte < 128:
            total += value
            value = 0
    return total


def _prepare(store: Any, expression: str) -> list[list[str]]:
    conn = store.conn
    for name, function in (("alice_fts_tokens", _tokens), ("alice_fts_log", math.log)):
        cursor = conn.execute("SELECT 1 FROM pragma_function_list WHERE name=? AND narg=1 LIMIT 1", (name,))
        try:
            installed = cursor.fetchone() is not None
        finally:
            cursor.close()
        if not installed:
            conn.create_function(name, 1, function, deterministic=True)
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.alice_memory_terms USING fts5vocab(main, memories_fts, instance)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.alice_chunk_terms USING fts5vocab(main, source_chunks_fts, instance)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.alice_query_text USING fts5(text, tokenize='porter unicode61')")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.alice_query_terms USING fts5vocab(temp, alice_query_text, instance)")
    conn.execute("DELETE FROM temp.alice_query_text")
    phrases = re.findall(r'"([^"]+)"', expression)
    conn.executemany("INSERT INTO temp.alice_query_text(rowid,text) VALUES (?,?)", enumerate(phrases, 1))
    rows = conn.execute("SELECT doc,term FROM temp.alice_query_terms ORDER BY doc,offset").fetchall()
    result: list[list[str]] = [[] for _ in phrases]
    for row in rows:
        result[row["doc"] - 1].append(row["term"])
    return [phrase for phrase in result if phrase]


def _phrase(table: str, terms: list[str]) -> tuple[str, str, tuple[str, ...]]:
    joins = " ".join(
        f"JOIN temp.{table} v{i} ON v{i}.doc=v0.doc AND v{i}.col=v0.col AND v{i}.offset=v0.offset+{i} AND v{i}.term=?"
        for i in range(1, len(terms))
    )
    return f"temp.{table} v0 {joins}", "v0.term=?", (*terms[1:], terms[0])


def rank_sql(
    store: Any, *, expression: str, memory: bool, population_from: str,
    population_where: str, population_params: tuple[object, ...], ordinary: str,
) -> tuple[str, str, tuple[object, ...]] | None:
    """Keep corpus statistics and candidate term frequencies inside one fenced statement.

    Ordinary rows alone supply rarity and average length. Derived candidates
    use that population's statistics, but never contribute to them. All SQL
    fragments are closed store expressions; caller values are bound.
    """
    phrases = _prepare(store, expression)
    if not phrases:
        return None
    vocab = "alice_memory_terms" if memory else "alice_chunk_terms"
    docsize = "memories_fts_docsize" if memory else "source_chunks_fts_docsize"
    alias = "m" if memory else "c"
    population_sql = (
        f"SELECT {alias}.rowid AS rid,alice_fts_tokens(d.sz) AS length,({ordinary}) AS ordinary "
        f"FROM {population_from} JOIN {docsize} d ON d.id={alias}.rowid WHERE {population_where}"
    )
    parameters: list[object] = list(population_params)
    frequency_sql = [
        f"(SELECT count(*) FROM rank_hits WHERE ordinary AND phrase={i}) AS df{i}"
        for i in range(len(phrases))
    ]
    weights = ("CASE v0.col WHEN 'title' THEN 1.0 WHEN 'canonical_text' THEN 0.4 "
               "WHEN 'summary' THEN 0.2 WHEN 'memory_key' THEN 0.4 WHEN 'fact_keys' THEN 0.1 ELSE 0 END") if memory else "1.0"
    hits: list[str] = []
    for i, phrase in enumerate(phrases):
        table, where, values = _phrase(vocab, phrase)
        hits.append(
            f"SELECT v0.doc, {i} AS phrase, sum({weights}) AS frequency, p.length, p.ordinary "
            f"FROM {table} CROSS JOIN rank_population p WHERE p.rid=v0.doc AND {where} GROUP BY v0.doc"
        )
        parameters.extend(values)
    idf = "CASE phrase " + " ".join(
        f"WHEN {i} THEN max(alice_fts_log((n-df{i}+0.5)/(df{i}+0.5)),0.000001)" for i in range(len(phrases))
    ) + " END"
    cte = ("WITH rank_population AS MATERIALIZED (" + population_sql + "), "
           "rank_hits AS MATERIALIZED (" + " UNION ALL ".join(hits) + "), "
           "rank_statistics AS MATERIALIZED (SELECT count(*) AS n, avg(length) AS average,"
           + ",".join(frequency_sql) + " FROM rank_population WHERE ordinary), "
           "readable_scores AS MATERIALIZED (SELECT doc, sum((" + idf + ") * (frequency * 2.2) / "
           "(frequency + 1.2 * (0.25 + 0.75 * length / average))) AS score "
           "FROM rank_hits CROSS JOIN rank_statistics GROUP BY doc) ")
    join = f" CROSS JOIN rank_statistics LEFT JOIN readable_scores ON readable_scores.doc={alias}.rowid "
    return cte, join, tuple(parameters)


def read_ranked_memories(store: Any, query: str, params: tuple[object, ...], *,
                         limit: int, offset: int, domains: Any, sensitivity_allowed: Any,
                         projects: Any) -> list[dict[str, object]]:
    """Keep derived candidates and their scores inside the store until admitted.

    Page the same ranked SQL result as the retrieval stage. This does not load
    the derived population for statistics: only result pages reach the shared
    exact label guard, and only admitted rows leave this store module.
    """
    from alicebot_api.vnext_label_guard import admit_loaded, label_read_scope

    admitted: list[dict[str, object]] = []
    page_limit = limit
    with label_read_scope(store):
        while True:
            rows = store._fetch_all(query, (*params[:-2], page_limit, offset))
            admitted.extend(admit_loaded(store, kind="memory", rows=rows, domains=domains,
                                         sensitivity_allowed=sensitivity_allowed, projects=projects))
            if len(admitted) >= limit or len(rows) < page_limit:
                return admitted[:limit]
            offset += len(rows)
            page_limit = min(max(limit, 128), 1024)
