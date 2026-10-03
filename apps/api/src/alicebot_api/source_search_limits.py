"""The query the SQLite store can take, and the refusal when it cannot.

One rule for every caller. The session brief bounds its own excerpt query to fit
it, the SQLite store refuses a query past it before SQLite does, and the MCP
tools turn that refusal into a typed error the client can act on. The pattern
count is ``_search_patterns`` from ``vnext_stores.retrieval_common``, the
function the search itself uses, so the count cannot drift from what the search
builds.

There are two readers of a caller's query, and they bind it differently. The
source search splits it into one LIKE pattern per distinct term
(``source_search_query_breach``). The memory, open loop and event reads behind
``alice_resume`` and ``alice_recent_decisions`` match it as one literal
substring (``literal_match_query_breach``). Both are held to the same byte
limit, which is one measured number, SQLite's 50,000 byte LIKE operand with
room to spare.
"""

from __future__ import annotations

from dataclasses import dataclass

from alicebot_api.vnext_stores.retrieval_common import _search_patterns
from alicebot_api.vnext_stores.sqlite.query_predicates import _escape_like_literal

# What the SQLite source search can take.
#
# ``SQLiteVNextStore.search_sources`` builds one LIKE pattern for the whole
# phrase and one per distinct non-stopword term, and ORs them together.
# SQLite refuses that expression once it is too deep ("Expression tree is too
# large (maximum depth 1000)"). Measured on SQLite 3.49.1 through
# ``search_sources``: 991 patterns (the phrase and 990 distinct terms) pass and
# 992 fail, and with a project, people and time scope active 984 patterns are
# the most that pass. That is 30 to 35 KB of ordinary prose, or about 7 KB of
# short distinct tokens, so it sits under the byte limit below. Repeated words
# cost nothing here: 13,000 terms that make 3 distinct patterns pass.
# ``SOURCE_SEARCH_QUERY_MAX_PATTERNS`` is about half the measured limit so
# another SQLite build, or a clause added to the search, still has room. The
# pattern count includes the phrase, so the most distinct terms a query may
# have is one fewer.
#
# The same search binds each pattern as one LIKE operand, and SQLite refuses an
# operand over 50,000 bytes ("LIKE or GLOB pattern too complex"). The search
# casefolds every pattern before it binds it, and some characters grow when
# they casefold (U+0390 goes from 2 bytes to 6), so 18,000 bytes of them bind a
# 54,000 byte pattern. A query is taken whole only when both its raw and its
# casefolded UTF-8 bytes are within ``SOURCE_SEARCH_QUERY_MAX_BYTES``.
#
# The literal substring match (``list_memories``, ``list_open_loops``,
# ``list_open_loop_events`` and ``list_resume_memory_events``) binds one operand,
# ``'%' || lower(?) || '%'`` with the query backslash-escaped, so it has no term
# count to bound and nothing else to OR together. Two things differ from the
# source search. SQLite's ``lower()`` only folds ASCII, so a character that
# grows when it casefolds does not grow here and the casefolded count does not
# apply. What does grow the operand is the escape: every ``\``, ``%`` and ``_``
# gets a backslash in front. Measured on SQLite 3.49.1 through ``list_memories``
# and ``list_resume_memory_events``: 49,998 bytes of plain text pass and 49,999
# fail, and 24,999 underscores pass and 25,000 fail, because 25,000 underscores
# escape to 50,000 bytes and the two ``%`` make 50,002. So a query is taken whole
# only when its raw bytes and its escaped bytes are within the same
# ``SOURCE_SEARCH_QUERY_MAX_BYTES``. The number of distinct terms costs nothing
# here: 4,000 of them, 30,889 bytes, pass.
SOURCE_SEARCH_QUERY_MAX_BYTES = 40_000
SOURCE_SEARCH_QUERY_MAX_PATTERNS = 500


@dataclass(frozen=True, slots=True)
class SourceSearchQueryBreach:
    """Which limit a query broke, as integers only.

    ``reason`` is ``bytes`` (raw UTF-8 bytes), ``folded_bytes`` (UTF-8 bytes
    after casefolding), ``escaped_bytes`` (UTF-8 bytes after the LIKE escape,
    for the literal match) or ``terms`` (distinct search terms). The message is
    built from these integers and fixed words alone, never from the query, so
    it is safe to return to a client.
    """

    reason: str
    measured: int
    limit: int

    @property
    def message(self) -> str:
        if self.reason == "terms":
            return (
                f"query has {self.measured} distinct search terms; "
                f"the limit is {self.limit}. Use a shorter query."
            )
        if self.reason == "folded_bytes":
            return (
                f"query is {self.measured} UTF-8 bytes after case folding; "
                f"the limit is {self.limit}. Use a shorter query."
            )
        if self.reason == "escaped_bytes":
            return (
                f"query is {self.measured} UTF-8 bytes after escaping %, _ and backslash; "
                f"the limit is {self.limit}. Use a shorter query."
            )
        return f"query is {self.measured} UTF-8 bytes; the limit is {self.limit}. Use a shorter query."


class SourceSearchQueryTooLarge(ValueError):
    """A query the SQLite store cannot take whole.

    Raised for the source search and for the literal substring match. The name
    stays because the MCP server and its tests already catch it by this name.

    ``public_message`` holds only counts and limits, so a caller that returns
    it to an agent never echoes the query text.
    """

    def __init__(self, breach: SourceSearchQueryBreach) -> None:
        super().__init__(breach.message)
        self.breach = breach

    @property
    def public_message(self) -> str:
        return self.breach.message


def source_search_query_breach(query: str) -> SourceSearchQueryBreach | None:
    """The limit ``query`` breaks, or ``None`` when the source search can take it whole.

    The raw byte check comes first, so a huge query is never casefolded or
    tokenized. The casefolded bytes are what the search binds into its LIKE
    pattern. The pattern count is the search's own ``_search_patterns``, not a
    second tokenizer that could drift from it.
    """

    raw_bytes = len(query.encode("utf-8", "surrogatepass"))
    if raw_bytes > SOURCE_SEARCH_QUERY_MAX_BYTES:
        return SourceSearchQueryBreach("bytes", raw_bytes, SOURCE_SEARCH_QUERY_MAX_BYTES)
    folded_bytes = len(query.casefold().encode("utf-8", "surrogatepass"))
    if folded_bytes > SOURCE_SEARCH_QUERY_MAX_BYTES:
        return SourceSearchQueryBreach("folded_bytes", folded_bytes, SOURCE_SEARCH_QUERY_MAX_BYTES)
    patterns = len(_search_patterns(query))
    if patterns > SOURCE_SEARCH_QUERY_MAX_PATTERNS:
        # The first pattern is the whole phrase; the rest are distinct terms.
        return SourceSearchQueryBreach("terms", patterns - 1, SOURCE_SEARCH_QUERY_MAX_PATTERNS - 1)
    return None


def require_source_search_query(query: str) -> None:
    """Raise ``SourceSearchQueryTooLarge`` when the source search cannot take ``query``."""

    breach = source_search_query_breach(query)
    if breach is not None:
        raise SourceSearchQueryTooLarge(breach)


def literal_match_query_breach(query: str) -> SourceSearchQueryBreach | None:
    """The limit ``query`` breaks as a literal substring match, or ``None``.

    The same byte limit as ``source_search_query_breach``, with no term count and
    no casefolded count: the match is one operand, and SQLite's ``lower()`` does
    not grow it. The escaped bytes are counted with the store's own
    ``_escape_like_literal``, the function that builds the operand, so the count
    cannot drift from it. The raw check comes first, so a huge query is never
    escaped. ``query`` is the text the store binds, which is already stripped.
    """

    raw_bytes = len(query.encode("utf-8", "surrogatepass"))
    if raw_bytes > SOURCE_SEARCH_QUERY_MAX_BYTES:
        return SourceSearchQueryBreach("bytes", raw_bytes, SOURCE_SEARCH_QUERY_MAX_BYTES)
    escaped_bytes = len(_escape_like_literal(query).encode("utf-8", "surrogatepass"))
    if escaped_bytes > SOURCE_SEARCH_QUERY_MAX_BYTES:
        return SourceSearchQueryBreach("escaped_bytes", escaped_bytes, SOURCE_SEARCH_QUERY_MAX_BYTES)
    return None


def require_literal_match_query(query: str) -> None:
    """Raise ``SourceSearchQueryTooLarge`` when the literal match cannot take ``query``."""

    breach = literal_match_query_breach(query)
    if breach is not None:
        raise SourceSearchQueryTooLarge(breach)


def literal_match_operand(query: str) -> str:
    """``query`` escaped for the store's literal LIKE match, or ``SourceSearchQueryTooLarge``.

    The one way a caller's text becomes a LIKE operand in the SQLite store's
    memory, open loop and event reads. The check and the escape are one call, so
    a read that binds a query cannot skip the check.
    """

    require_literal_match_query(query)
    return _escape_like_literal(query)
