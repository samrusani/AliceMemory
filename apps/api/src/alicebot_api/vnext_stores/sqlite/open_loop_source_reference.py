"""The one rule that says an open loop names a source, on SQLite.

Three places ask the question: the reverse lookup of a source (``list_open_loops_referencing_source``), the owner's
delete preview and receipt count, and the scrub that blanks a source's loops (``retire_dependents``, which both
``sources delete`` and ``import-markdown --supersede`` reach). They read this one statement, so a loop that the lookup
lists for a source is counted and blanked for it, and one the lookup does not list is not.

A loop names a source when its ``source_id`` column holds the id, or when the text under one of the keys of
``SOURCE_REFERENCE_KEYS`` (``vnext_source_fence``) anywhere in its metadata is the id as stored or ``source:<id>``. The
metadata is walked with ``json_tree``, so a key at any depth counts. The comparison is exact text, so it does not read
an id in capitals, without hyphens, or inside a list or a JSON text, which the shared reference reader of memories and
saved quotes (``cited_source_ids``) does read; the scrub follows this rule and no further.
"""

from __future__ import annotations

# The keys are the literals of ``SOURCE_REFERENCE_KEYS``. A test reads this text and checks them against the set, so a
# key added to one and not to the other fails there.
OPEN_LOOP_SOURCE_REFERENCE_SQL = """(
                    open_loops.source_id = ?
                    OR EXISTS (
                      SELECT 1 FROM json_tree(open_loops.metadata_json) AS ref
                      WHERE ref.key IN (
                        'source_id', 'source_ids', 'source_ref', 'source_refs',
                        'source_references', 'selected_source_ids'
                      )
                        AND CAST(ref.value AS TEXT) IN (?, ?)
                    )
                  )"""


def open_loop_source_reference_sql(source_id: object) -> tuple[str, tuple[str, str, str]]:
    """``(condition, parameters)`` for the rows of ``open_loops`` that name ``source_id``.

    The condition reads the table by its own name, so it fits ``SELECT ... FROM open_loops`` and
    ``UPDATE open_loops``. The caller adds its own ``user_id`` test.
    """

    text = str(source_id)
    return OPEN_LOOP_SOURCE_REFERENCE_SQL, (text, text, f"source:{text}")
