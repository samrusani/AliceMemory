"""Size bounds on correction and capture-commit inputs, stated once.

Why they exist. The review of S4.4 round 1 (2026-09-23, finding 8) measured
a continuity correction with a 3 MB body at 44 s of CPU and 1.58 GB of RSS,
because the credential check read the whole payload before the object was
even looked up; v0.16.0 answered the same request in 0.01 s. The service now
looks the object up first, and these bounds refuse an oversized field before
anything is scanned. Every surface imports the numbers from here: the
services that enforce them, the HTTP models, and the MCP schemas that
advertise them.

Sizes are counted as JSON-serialized characters, so a nested mapping is
measured the same way whatever its shape.
"""

from __future__ import annotations

import json

from alicebot_api.vnext_json import json_safe

# One body, provenance, replacement_body or replacement_provenance mapping on
# a correction. The same size as the largest canonical_text a memory commit
# accepts over HTTP.
# A memory commit's source_refs (owner ruling R4, 2026-09-23): at most 64
# refs; a string ref at most 4,000 characters of raw text as sent, any other
# ref at most 4,000 characters serialized. Enforced by the service every
# surface calls (vnext_memory_commit._object_tuple); advertised, and enforced
# on the raw argument, by the MCP schema. Scoped to a memory commit: the
# propose and artifact source_refs are not bounded here.
MAX_COMMIT_SOURCE_REFS = 64
MAX_COMMIT_SOURCE_REF_CHARS = 4_000

MAX_CORRECTION_FIELD_CHARS = 20_000
# A title on a correction. Continuity objects already refuse longer titles.
MAX_CORRECTION_TITLE_CHARS = 280
# Candidates one capture-commit call may carry, and the size of each.
MAX_CAPTURE_COMMIT_CANDIDATES = 100
MAX_CAPTURE_CANDIDATE_CHARS = 20_000


def serialized_chars(value: object) -> int:
    """Characters in the JSON form of a value, the unit every bound here uses."""

    return len(json.dumps(json_safe(value), ensure_ascii=False))


def first_oversized(fields: dict[str, object], limit: int) -> str | None:
    """The name of the first field whose serialized size exceeds ``limit``."""

    for name, value in fields.items():
        if value is not None and serialized_chars(value) > limit:
            return name
    return None


__all__ = [
    "MAX_CAPTURE_CANDIDATE_CHARS",
    "MAX_CAPTURE_COMMIT_CANDIDATES",
    "MAX_CORRECTION_FIELD_CHARS",
    "MAX_CORRECTION_TITLE_CHARS",
    "first_oversized",
    "serialized_chars",
]
