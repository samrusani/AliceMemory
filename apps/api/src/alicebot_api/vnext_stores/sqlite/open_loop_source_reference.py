"""The one rule that says an open loop names a source, on SQLite.

Three places ask the question: the reverse lookup of a source (``list_open_loops_referencing_source``), the owner's
delete preview and receipt count, and the scrub that blanks a source's loops (``retire_dependents``, which both
``sources delete`` and ``import-markdown --supersede`` reach). They read this one statement, so a loop that the lookup
lists for a source is counted and blanked for it, and one the lookup does not list is not.

A loop names a source when its ``source_id`` column holds the id, or when the text under one of the keys of
``SOURCE_REFERENCE_KEYS`` (``vnext_source_fence``) anywhere in its metadata is the id as stored or ``source:<id>``. The
metadata is walked with ``json_tree``, so a key at any depth counts. The comparison is exact text, so any spelling other
than the id as stored or ``source:<id>`` is not read (capitals, no hyphens, braces, a ``urn:uuid:`` or ``SOURCE:``
prefix, a list, a JSON text, and every other form), although the shared reference reader of memories and saved quotes
(``cited_source_ids``) reads many of them. The scrub follows this rule and no further.
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

# The two statements of the delete preview and the scrub are written out here, once, so neither caller builds SQL.
# Each is the fixed text around the constant above. Nothing a caller passes enters the text: the user id, the id of the
# source and the timestamps are bound parameters, in the order ``open_loop_source_reference_params`` documents.
# Bandit reads an f-string over SQL words as injection (B608) whatever it holds, so the two lines say why they are safe.
OPEN_LOOP_SOURCE_COUNT_SQL = f"""SELECT count(*) AS count FROM open_loops
WHERE user_id = ? AND {OPEN_LOOP_SOURCE_REFERENCE_SQL}"""  # nosec B608 # fixed text and a module constant; every value is bound
OPEN_LOOP_SOURCE_BLANK_SQL = f"""UPDATE open_loops SET title = ?, description = ?, status = 'dismissed',
resolved_at = ?, closed_at = ?, resolution_note = ?, metadata_json = '{{}}', updated_at = ?
WHERE user_id = ? AND {OPEN_LOOP_SOURCE_REFERENCE_SQL}"""  # nosec B608 # fixed text and a module constant; every value is bound


def open_loop_source_reference_params(source_id: object) -> tuple[str, str, str]:
    """The three parameters of ``OPEN_LOOP_SOURCE_REFERENCE_SQL``, in order: the column test, the id, ``source:<id>``."""

    text = str(source_id)
    return text, text, f"source:{text}"


def open_loop_source_reference_sql(source_id: object) -> tuple[str, tuple[str, str, str]]:
    """``(condition, parameters)`` for the rows of ``open_loops`` that name ``source_id``.

    The condition reads the table by its own name, so it fits ``SELECT ... FROM open_loops`` and
    ``UPDATE open_loops``. The caller adds its own ``user_id`` test.
    """

    return OPEN_LOOP_SOURCE_REFERENCE_SQL, open_loop_source_reference_params(source_id)
