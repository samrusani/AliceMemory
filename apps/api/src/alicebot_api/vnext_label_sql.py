"""Conservative SQL partition: anything uncertain still reaches the kernel."""
from alicebot_api.vnext_derived_labels import MARKER_KEYS, DERIVED_ARTIFACT_TYPES, SENSITIVITY_RANK


def hidden_memory_input_sql(sensitivity_allowed, *, sqlite: bool, alias: str = "m") -> str:
    """Reject only a proved source floor above the highest requested rank.

    A direct canonical source, or a direct source of one recorded memory
    input, suffices for rejection. Anything else reaches effective admission.
    This predicate never grants a row and never filters by a derived domain.
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
        def uuid_hint(value):
            return f"CASE WHEN ({value}) ~* '^(?:[0-9a-f]{{32}}|[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}})$' THEN ({value})::uuid END"
        meta = f"{alias}.metadata_json"
        source = uuid_hint(source_hint(meta))
        memory = uuid_hint(f"COALESCE(CASE WHEN jsonb_typeof({meta}->'consolidation'->'cluster_member_ids')='array' AND jsonb_typeof({meta}->'consolidation'->'cluster_member_ids'->0)='string' THEN {meta}->'consolidation'->'cluster_member_ids'->>0 END, CASE WHEN jsonb_typeof({meta}->'derived_from'->'memories')='array' AND jsonb_typeof({meta}->'derived_from'->'memories'->0)='string' THEN {meta}->'derived_from'->'memories'->>0 END)")
        parent_source = uuid_hint(source_hint("label_input.metadata_json"))
    not_redacted = "TRUE" if sqlite else f"{alias}.metadata_json->'redacted' IS DISTINCT FROM 'true'::jsonb"
    input_not_redacted = "TRUE" if sqlite else "label_input.metadata_json->'redacted' IS DISTINCT FROM 'true'::jsonb"
    if not sqlite:
        # Uncorrelated row-valued sets are hashed once by PostgreSQL. Include
        # tenant identity in each set rather than scanning parents per row.
        return f"""({alias}.sensitivity NOT IN ({names}) AND NOT (
            {not_redacted} AND (
                COALESCE(({alias}.user_id, {source}) IN (
                    SELECT user_id, id FROM sources WHERE sensitivity IN ({names})
                ), FALSE)
                OR COALESCE(({alias}.user_id, {memory}) IN (
                    SELECT label_input.user_id, label_input.id FROM memories label_input
                    JOIN sources label_source ON label_source.user_id=label_input.user_id
                        AND label_source.id={parent_source}
                    WHERE {input_not_redacted} AND label_source.sensitivity IN ({names})
                ), FALSE)
            )))"""  # nosec B608
    return f"""({alias}.sensitivity NOT IN ({names}) AND NOT (
        {not_redacted} AND (EXISTS (
            SELECT 1 FROM sources label_source WHERE label_source.user_id={alias}.user_id
            AND label_source.id={source} AND label_source.sensitivity IN ({names})
        ) OR EXISTS (
            SELECT 1 FROM memories label_input JOIN sources label_source
            ON label_source.user_id=label_input.user_id AND label_source.id={parent_source}
            WHERE label_input.user_id={alias}.user_id AND label_input.id={memory}
            AND {input_not_redacted} AND label_source.sensitivity IN ({names})
        ))))"""  # nosec B608


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
