"""The one rule that says an open loop names a source, on SQLite.

The reverse lookup of a source (``list_open_loops_referencing_source``), the owner's delete preview and receipt
count, the scrub that blanks a source's loops, and the reader that withholds ids all use ``cited_source_ids``.
A loop names a source when that reader names the id: the ``source_id`` column, or a reference under
``SOURCE_REFERENCE_KEYS`` at any depth, in any spelling ``cited_source_ids`` names (capitals, no hyphens, braces,
``urn:uuid:``, a list, JSON text). An id in prose under some other key is incidental and is not a name.
"""

from __future__ import annotations

from collections.abc import Mapping

from alicebot_api.vnext_source_fence import SOURCE_REFERENCE_KEYS, cited_source_ids

# The lookup, the preview and the scrub read this set. It is the fence's set, not a second list of key names.
NAMED_REFERENCE_KEYS = SOURCE_REFERENCE_KEYS


def named_source_ids(row: Mapping[str, object]) -> frozenset[str]:
    """The canonical source ids ``row`` names, and no incidental id from prose under another key."""

    cited = cited_source_ids(row.get("metadata_json"))
    column = row.get("source_id")
    if column is not None and column != "":
        cited = cited | cited_source_ids(column)
    return cited.named
