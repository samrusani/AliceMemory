"""Conservative SQL partition: anything uncertain still reaches the kernel."""
from alicebot_api.vnext_derived_labels import MARKER_KEYS, DERIVED_ARTIFACT_TYPES


def original_label_sql(kind: str, *, sqlite: bool = False) -> str:
    """Only rows definitely original by is_derived may be counted in SQL.

    Scalar metadata, redacted derived markers and legacy shapes remain in the
    Python partition. JSON text is not reinterpreted by the SQL partition.
    """
    if kind == "source":
        return "TRUE"
    markers = ",".join("'" + key + "'" for key in sorted(MARKER_KEYS))
    if sqlite:
        safe = "CASE WHEN json_valid(metadata_json) THEN metadata_json ELSE 'null' END"
        predicate = f"(json_type({safe}) = 'object' AND NOT EXISTS (SELECT 1 FROM json_each({safe}) WHERE key IN ({markers})))"  # nosec B608 - closed module constants, no caller input
    else:
        predicate = f"(jsonb_typeof(metadata_json) = 'object' AND NOT (metadata_json ?| ARRAY[{markers}]::text[]))"
    if kind == "artifact":
        types = ",".join("'" + value + "'" for value in sorted(DERIVED_ARTIFACT_TYPES))
        predicate = f"({predicate} AND artifact_type NOT IN ({types}))"
    return predicate
