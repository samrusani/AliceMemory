"""Conservative SQL partition: anything uncertain still reaches the kernel."""
from alicebot_api.vnext_derived_labels import MARKER_KEYS, DERIVED_ARTIFACT_TYPES, SENSITIVITY_RANK


def hidden_memory_input_sql(sensitivity_allowed, *, sqlite: bool, alias: str = "m") -> str:
    """Reject only a row proved hidden from the highest requested rank.

    A direct canonical source, or a direct source of one recorded memory
    input, suffices for rejection. So does a recorded memory input that is
    redacted: such a row is unverified, which the kernel reads as regulated.
    Anything else reaches effective admission. This predicate never grants a
    row and never filters by a derived domain.
    """

    if alias not in {"m", "memories"}:
        raise ValueError("unsupported memory alias")
    ceiling = max((SENSITIVITY_RANK.get(value, SENSITIVITY_RANK["unknown"]) for value in sensitivity_allowed or ()), default=0)
    blocked = [value for value, rank in SENSITIVITY_RANK.items() if rank > ceiling] if sensitivity_allowed else []
    if not blocked:
        return "TRUE"
    names = ",".join("'" + value + "'" for value in blocked)  # Closed kernel constants and whitelisted aliases are the only SQL inputs.
    if sqlite:
        source = f"alice_direct_source_hint({alias}.metadata_json)"
        memory = f"alice_direct_memory_hint({alias}.metadata_json)"
        parent_source = "alice_direct_source_hint(label_input.metadata_json)"
    else:
        def source_hint(meta):
            return f"COALESCE({meta}->>'source_id', CASE WHEN jsonb_typeof({meta}->'derived_from'->'sources')='array' AND jsonb_typeof({meta}->'derived_from'->'sources'->0)='string' THEN {meta}->'derived_from'->'sources'->>0 END)"
        meta = f"{alias}.metadata_json"
        source = "lower(" + source_hint(meta) + ")"
        memory = f"lower(COALESCE(CASE WHEN jsonb_typeof({meta}->'consolidation'->'cluster_member_ids')='array' AND jsonb_typeof({meta}->'consolidation'->'cluster_member_ids'->0)='string' THEN {meta}->'consolidation'->'cluster_member_ids'->>0 END, CASE WHEN jsonb_typeof({meta}->'derived_from'->'memories')='array' AND jsonb_typeof({meta}->'derived_from'->'memories'->0)='string' THEN {meta}->'derived_from'->'memories'->>0 END))"
        parent_source = "lower(" + source_hint("label_input.metadata_json") + ")"
    not_redacted = "TRUE" if sqlite else f"{alias}.metadata_json->'redacted' IS DISTINCT FROM 'true'::jsonb"
    # A redacted memory is the hidden input itself. A row that is not redacted and lists one first is unverified.
    if sqlite:
        safe_input = "CASE WHEN json_valid(label_input.metadata_json) THEN label_input.metadata_json ELSE 'null' END"
        input_redacted = f"COALESCE(json_extract({safe_input}, '$.redacted') = 1, 0)"
        input_not_redacted = f"NOT {input_redacted}"
    else:
        input_redacted = "label_input.metadata_json->'redacted' IS NOT DISTINCT FROM 'true'::jsonb"
        input_not_redacted = "label_input.metadata_json->'redacted' IS DISTINCT FROM 'true'::jsonb"
    if not sqlite:
        # Hash canonical and compact UUID spellings without casting untrusted
        # JSON. Exact text membership accepts the same case-insensitive forms
        # as the former guarded UUID cast. Tenant identity stays in every set;
        # the parent partition also uses a hash instead of a per-row join.
        source_set = f"SELECT user_id, unnest(ARRAY[id::text, replace(id::text, '-', '')]) FROM sources WHERE sensitivity IN ({names})"  # nosec B608 - closed kernel sensitivity constants only
        return f"""({alias}.sensitivity NOT IN ({names}) AND NOT (
            {not_redacted} AND (
                COALESCE(({alias}.user_id, {source}) IN (
                    {source_set}
                ), FALSE)
                OR COALESCE(({alias}.user_id, {memory}) IN (
                    SELECT label_input.user_id, unnest(ARRAY[label_input.id::text, replace(label_input.id::text, '-', '')])
                    FROM memories label_input
                    WHERE {input_redacted} OR ({input_not_redacted} AND COALESCE((label_input.user_id, {parent_source}) IN (
                        {source_set}
                    ), FALSE))
                ), FALSE)
            )))"""  # nosec B608
    return f"""({alias}.sensitivity NOT IN ({names}) AND NOT (
        {not_redacted} AND (EXISTS (
            SELECT 1 FROM sources label_source WHERE label_source.user_id={alias}.user_id
            AND label_source.id={source} AND label_source.sensitivity IN ({names})
        ) OR EXISTS (
            SELECT 1 FROM memories label_input
            WHERE label_input.user_id={alias}.user_id AND label_input.id={memory}
            AND ({input_redacted} OR ({input_not_redacted} AND EXISTS (
                SELECT 1 FROM sources label_source
                WHERE label_source.user_id=label_input.user_id AND label_source.id={parent_source}
                AND label_source.sensitivity IN ({names})
            )))
        ))))"""  # nosec B608


# What an event names. The guard admits an event for a caller with limits only when it admits every row the event names,
# and this is where the event writers' vocabulary is kept, so the guard and the count query read one list.
#
# * A target of one of these types is the labelled row the event is about.
EVENT_TARGET_KINDS = frozenset({"source", "memory", "open_loop", "artifact", "project", "belief"})
# * A graph edge has no label of its own. It is readable when each labelled end is (an entity end has no label).
EVENT_EDGE_TARGET = "graph_edge"
# * A target of one of these types is a row whose label the guard cannot read: a continuity object keeps its domain,
#   sensitivity and projects in its provenance and body, in the legacy store. An event about such a row cannot be shown to
#   be readable, so it is not shown to a caller with limits (the policy event of an explain of a continuity object is the
#   one the code writes).
EVENT_UNJUDGED_TARGETS = frozenset({"continuity_object"})
# * A chunk of a source has no label of its own either: the event that records it takes the label of the source its
#   payload names, and an event that names no source cannot be shown to be readable.
EVENT_CHILD_TARGETS = {"source_chunk": ("source", "source_id")}
# * A payload field that holds the id of a labelled row (one id, or a list of ids), with the kind of row it holds. The ids
#   of rows that carry no label (a scheduler run, a task, a revision, a provenance link, a confirmation) are not here.
EVENT_PAYLOAD_REFERENCES = {
    "source_id": "source",
    "source_ids": "source",
    "memory_id": "memory",
    "candidate_memory_id": "memory",
    "candidate_memory_ids": "memory",
    "rollup_candidate_ids": "memory",
    "expired_memory_ids": "memory",
    "superseded_member_ids": "memory",
    "member_id": "memory",
    "replacement_memory_id": "memory",
    "artifact_id": "artifact",
    "artifact_ids": "artifact",
    "belief_id": "belief",
    "project_id": "project",
    "project_ids": "project",
}
# * A field that names a different kind of row in different events, chosen by the start of the event type: the row that
#   replaced another (a source, a belief, a memory).
EVENT_TYPE_REFERENCES = {
    "source.": {"superseded_by": "source"},
    "belief.": {"superseded_by": "belief"},
    "agent.memory_": {"superseded_by": "memory"},
}
EVENT_REFERENCE_KEYS = tuple(
    sorted({*EVENT_PAYLOAD_REFERENCES, *(key for fields in EVENT_TYPE_REFERENCES.values() for key in fields)})
)


def event_references_sql(*, sqlite: bool) -> str:
    """The payload of an event, or NULL when it holds none of the fields that name a row.

    SQLite cuts the payload down to those fields. PostgreSQL returns the payload whole for the few events that hold one,
    which is cheaper than building an object for each.
    """
    names = ",".join("'" + key + "'" for key in EVENT_REFERENCE_KEYS)  # nosec B608 - closed module constants
    if sqlite:
        pairs = ", ".join(f"'{key}', json_extract(payload_json, '$.{key}')" for key in EVENT_REFERENCE_KEYS)  # nosec B608
        cut = f"CASE WHEN EXISTS (SELECT 1 FROM json_each(payload_json) WHERE key IN ({names})) THEN json_object({pairs}) END"  # nosec B608
        # Nested, because SQLite does not promise to stop at the first false term, and json_type fails on text that is not JSON.
        return "CASE WHEN json_valid(payload_json) THEN CASE WHEN json_type(payload_json) = 'object' THEN " + cut + " END END"
    # Most events name no row, so the payload is returned only for those that hold one of the fields at the top level.
    return f"CASE WHEN payload_json ?| ARRAY[{names}]::text[] THEN payload_json END"  # nosec B608


def event_names_a_row_sql(alias: str = "") -> str:
    """True for an event whose payload holds a field that names a row (PostgreSQL). ``alias`` is a closed literal."""
    if alias not in {"", "e"}:
        raise ValueError("unsupported event alias")
    names = ",".join("'" + key + "'" for key in EVENT_REFERENCE_KEYS)  # nosec B608 - closed module constants
    prefix = alias + "." if alias else ""
    return f"({prefix}payload_json ?| ARRAY[{names}]::text[])"  # nosec B608


def hidden_memory_event_sql(sensitivity_allowed, *, sqlite: bool) -> str:
    """Reject an event only when its same-tenant memory floor proves it hidden."""
    if not sensitivity_allowed:
        return "TRUE"
    label_sql = hidden_memory_input_sql(sensitivity_allowed, sqlite=sqlite)
    memory_id = "m.id" if sqlite else "m.id::text"
    # Both identifiers are closed literals; label_sql uses only kernel ranks.
    return ("(target_type IS NULL OR target_type <> 'memory' OR "  # nosec B608
            "(user_id, target_id) NOT IN (SELECT m.user_id, " + memory_id +
            " FROM memories m WHERE NOT (" + label_sql + ")))")  # nosec B608


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
