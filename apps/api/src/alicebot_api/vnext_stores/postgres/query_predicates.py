"""PostgreSQL query predicates shared by memory and scoped reads."""

from __future__ import annotations

from alicebot_api.vnext_stores.retrieval_common import fts_fallback_tokens

_PROJECT_ASCII_WHITESPACE_PATTERN_SQL = "'[' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || ' ]+'"


_ASCII_PROJECT_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


_ASCII_PROJECT_LOWER = "abcdefghijklmnopqrstuvwxyz"


def _escape_like_literal(value: str) -> str:
    """Escape one literal substring for SQL LIKE with backslash ESCAPE."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _postgres_ascii_literal_contains_sql(value_expression: str) -> str:
    """Build a binary-collated ASCII-folded literal substring predicate."""

    folded_value = f"translate({value_expression}, '{_ASCII_PROJECT_UPPER}', '{_ASCII_PROJECT_LOWER}') COLLATE \"C\""
    folded_pattern = (
        f"('%%' || translate(%s, '{_ASCII_PROJECT_UPPER}', '{_ASCII_PROJECT_LOWER}') || '%%') COLLATE \"C\""
    )
    return f"({folded_value}) LIKE ({folded_pattern}) ESCAPE E'\\\\'"


def _normalized_project_identifier_sql(value_expression: str) -> str:
    """Mirror Python's explicit ASCII project-whitespace normalization."""

    return f"btrim(regexp_replace({value_expression}, {_PROJECT_ASCII_WHITESPACE_PATTERN_SQL}, ' ', 'g'), ' ')"


def _project_identifier_identity_sql(
    value_expression: str,
    *,
    already_normalized: bool = False,
) -> str:
    """Mirror the conservative ASCII-only project identity in PostgreSQL."""

    normalized = value_expression if already_normalized else _normalized_project_identifier_sql(value_expression)
    return f"""
(
  CASE
    WHEN octet_length({normalized}) = char_length({normalized})
      THEN translate({normalized}, '{_ASCII_PROJECT_UPPER}', '{_ASCII_PROJECT_LOWER}')
    ELSE {normalized}
  END
) COLLATE "C"
"""


def _jsonb_project_scope_values_sql(
    metadata_expression: str,
    *,
    legacy_keys: tuple[str, ...],
    project_id_expression: str | None = None,
) -> str:
    """Return one non-widening JSON array expression for project scope.

    Presence of the canonical top-level ``project_scope`` key is
    authoritative, including an explicit empty array.  Legacy nested and
    singular representations are consulted only when that key is absent.
    Malformed canonical values fail closed instead of resurrecting stale
    legacy scope.
    """

    canonical_scope = f"{metadata_expression} -> 'project_scope'"
    nested_scope = f"""
jsonb_build_array(
  {metadata_expression} #> '{{agentic_memory,project_scope}}',
  {metadata_expression} #> '{{agent_identity,project_scope}}'
)
"""
    nested_scope_present = f"""
(
  (
    jsonb_typeof({metadata_expression} -> 'agentic_memory') = 'object'
    AND ({metadata_expression} -> 'agentic_memory') ? 'project_scope'
  )
  OR (
    jsonb_typeof({metadata_expression} -> 'agent_identity') = 'object'
    AND ({metadata_expression} -> 'agent_identity') ? 'project_scope'
  )
)
"""
    project_id_branch = ""
    if project_id_expression is not None:
        project_id_branch = f"""
  WHEN {project_id_expression} IS NOT NULL
    THEN jsonb_build_array({project_id_expression})"""
    legacy_values = ",\n      ".join(
        f"{metadata_expression} #> '{{{','.join(key.split('.'))}}}'" for key in legacy_keys
    )
    resolved_scope = f"""
CASE
  WHEN {metadata_expression} ? 'project_scope'
    THEN CASE
      WHEN jsonb_typeof({canonical_scope}) = 'array' THEN {canonical_scope}
      ELSE '[]'::jsonb
    END
  WHEN {nested_scope_present}
    THEN {nested_scope}{project_id_branch}
  ELSE jsonb_build_array(
    {legacy_values}
  )
END
"""
    resolved_leaf_rows = _jsonb_project_scope_leaf_values_sql(
        f"({resolved_scope})",
        alias="generic_resolved_scope",
    )
    normalized_scope_value = _normalized_project_identifier_sql("scope_leaf.value")
    identity_scope_value = _project_identifier_identity_sql(
        "normalized_scope.value",
        already_normalized=True,
    )
    return f"""
(
  SELECT COALESCE(
    jsonb_agg(scope_identity.value ORDER BY scope_identity.value COLLATE "C"),
    '[]'::jsonb
  )
  FROM (
    SELECT DISTINCT {identity_scope_value} AS value
    FROM ({resolved_leaf_rows}) AS scope_leaf(value)
    CROSS JOIN LATERAL (
      SELECT {normalized_scope_value} AS value
    ) AS normalized_scope
    WHERE normalized_scope.value <> ''
  ) AS scope_identity
)
"""


def _jsonb_project_scope_leaf_values_sql(
    candidate_expression: str,
    *,
    alias: str,
) -> str:
    """Return the JSON scalar leaves accepted by Python and migration 0090."""

    return f"""
WITH RECURSIVE {alias}_nodes(value) AS (
  SELECT {candidate_expression}
  UNION ALL
  SELECT array_item.value
  FROM {alias}_nodes AS parent
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE
      WHEN jsonb_typeof(parent.value) = 'array' THEN parent.value
      ELSE '[]'::jsonb
    END
  ) AS array_item(value)
)
SELECT CASE jsonb_typeof(value)
  WHEN 'string' THEN value #>> '{{}}'
  WHEN 'number' THEN CASE
    WHEN (value #>> '{{}}')::numeric = trunc((value #>> '{{}}')::numeric)
      THEN CASE
        WHEN (value #>> '{{}}')::numeric = 0 THEN '0'
        ELSE trunc((value #>> '{{}}')::numeric)::text
      END
  END
  WHEN 'boolean' THEN CASE
    WHEN value = 'true'::jsonb THEN 'True'
    ELSE 'False'
  END
END AS value
FROM {alias}_nodes
WHERE jsonb_typeof(value) IN ('string', 'boolean')
   OR (
     jsonb_typeof(value) = 'number'
     AND (value #>> '{{}}')::numeric = trunc((value #>> '{{}}')::numeric)
   )
"""


def _jsonb_source_project_scope_values_sql(metadata_expression: str) -> str:
    """Resolve a persisted source's complete scope envelope in PostgreSQL.

    Source writers historically stored either plain metadata or an entire
    resource-shaped envelope inside ``sources.metadata_json``. This mirrors
    the Python resolver and migration 0090, including scalar-leaf parity.
    """

    root_alias_leaf_rows = _jsonb_project_scope_leaf_values_sql(
        "source_candidates.root_aliases",
        alias="root_alias_candidate",
    )
    resolved_leaf_rows = _jsonb_project_scope_leaf_values_sql(
        "resolved_source_scope.value",
        alias="resolved_scope",
    )
    normalized_root_alias_value = _normalized_project_identifier_sql("root_alias_candidate_value.value")
    normalized_scope_value = _normalized_project_identifier_sql("resolved_scope_value.value")
    identity_scope_value = _project_identifier_identity_sql(
        "normalized_scope.value",
        already_normalized=True,
    )
    return f"""
(
  SELECT COALESCE(
    jsonb_agg(scope_identity.value ORDER BY scope_identity.value COLLATE "C"),
    '[]'::jsonb
  )
  FROM (
    SELECT DISTINCT {identity_scope_value} AS value
    FROM LATERAL (
    SELECT CASE
      WHEN jsonb_typeof({metadata_expression}) = 'object'
        THEN {metadata_expression}
      ELSE '{{}}'::jsonb
    END AS value
  ) AS source_resource
  CROSS JOIN LATERAL (
    SELECT
      CASE
        WHEN jsonb_typeof(source_resource.value -> 'metadata_json') = 'object'
          THEN source_resource.value -> 'metadata_json'
        ELSE '{{}}'::jsonb
      END AS metadata_json,
      CASE
        WHEN jsonb_typeof(source_resource.value -> 'scope_json') = 'object'
          THEN source_resource.value -> 'scope_json'
        ELSE '{{}}'::jsonb
      END AS scope_json
  ) AS source_containers
  CROSS JOIN LATERAL (
    SELECT
      CASE
        WHEN jsonb_typeof(source_resource.value -> 'agentic_memory') = 'object'
          THEN (source_resource.value -> 'agentic_memory') ||
            CASE
              WHEN jsonb_typeof(source_containers.metadata_json -> 'agentic_memory') = 'object'
                THEN source_containers.metadata_json -> 'agentic_memory'
              ELSE '{{}}'::jsonb
            END
        ELSE source_containers.metadata_json -> 'agentic_memory'
      END AS agentic_memory,
      CASE
        WHEN jsonb_typeof(source_resource.value -> 'agent_identity') = 'object'
          THEN (source_resource.value -> 'agent_identity') ||
            CASE
              WHEN jsonb_typeof(source_containers.metadata_json -> 'agent_identity') = 'object'
                THEN source_containers.metadata_json -> 'agent_identity'
              ELSE '{{}}'::jsonb
            END
        ELSE source_containers.metadata_json -> 'agent_identity'
      END AS agent_identity
  ) AS source_nested
  CROSS JOIN LATERAL (
    SELECT
      jsonb_build_array(
        source_nested.agentic_memory -> 'project_scope',
        source_nested.agent_identity -> 'project_scope',
        source_containers.scope_json #> '{{agentic_memory,project_scope}}',
        source_containers.scope_json #> '{{agent_identity,project_scope}}'
      ) AS nested_scope,
      (
        (
          jsonb_typeof(source_nested.agentic_memory) = 'object'
          AND source_nested.agentic_memory ? 'project_scope'
        )
        OR (
          jsonb_typeof(source_nested.agent_identity) = 'object'
          AND source_nested.agent_identity ? 'project_scope'
        )
        OR (
          jsonb_typeof(source_containers.scope_json -> 'agentic_memory') = 'object'
          AND (source_containers.scope_json -> 'agentic_memory') ? 'project_scope'
        )
        OR (
          jsonb_typeof(source_containers.scope_json -> 'agent_identity') = 'object'
          AND (source_containers.scope_json -> 'agent_identity') ? 'project_scope'
        )
      ) AS nested_scope_present,
      jsonb_build_array(
        source_resource.value -> 'project_id',
        source_resource.value -> 'project',
        source_resource.value -> 'projects'
      ) AS root_aliases,
      jsonb_build_array(
        source_containers.metadata_json -> 'project_id',
        source_containers.metadata_json -> 'project',
        source_containers.metadata_json -> 'projects',
        source_nested.agentic_memory -> 'project_id',
        source_nested.agentic_memory -> 'project',
        source_nested.agentic_memory -> 'projects',
        source_containers.scope_json -> 'project_id',
        source_containers.scope_json -> 'project',
        source_containers.scope_json -> 'projects',
        source_containers.scope_json #> '{{agentic_memory,project_id}}',
        source_containers.scope_json #> '{{agentic_memory,project}}',
        source_containers.scope_json #> '{{agentic_memory,projects}}'
      ) AS final_aliases
  ) AS source_candidates
  CROSS JOIN LATERAL (
    SELECT CASE
      WHEN source_resource.value ? 'project_scope'
        THEN CASE
          WHEN jsonb_typeof(source_resource.value -> 'project_scope') = 'array'
            THEN source_resource.value -> 'project_scope'
          ELSE '[]'::jsonb
        END
      WHEN source_containers.metadata_json ? 'project_scope'
        THEN CASE
          WHEN jsonb_typeof(source_containers.metadata_json -> 'project_scope') = 'array'
            THEN source_containers.metadata_json -> 'project_scope'
          ELSE '[]'::jsonb
        END
      WHEN source_containers.scope_json ? 'project_scope'
        THEN CASE
          WHEN jsonb_typeof(source_containers.scope_json -> 'project_scope') = 'array'
            THEN source_containers.scope_json -> 'project_scope'
          ELSE '[]'::jsonb
        END
      WHEN source_candidates.nested_scope_present
        THEN source_candidates.nested_scope
      WHEN EXISTS (
        SELECT 1
        FROM (
          {root_alias_leaf_rows}
        ) AS root_alias_candidate_value
        WHERE {normalized_root_alias_value} <> ''
      ) THEN source_candidates.root_aliases
      ELSE source_candidates.final_aliases
    END AS value
  ) AS resolved_source_scope
  CROSS JOIN LATERAL (
    {resolved_leaf_rows}
  ) AS resolved_scope_value
  CROSS JOIN LATERAL (
    SELECT {normalized_scope_value} AS value
  ) AS normalized_scope
  WHERE normalized_scope.value <> ''
  ) AS scope_identity
)
"""


# The canonical top-level metadata array wins over nested agentic scope and
# singular legacy/index fallbacks. CASE precedence is intentionally
# non-widening: stale lower-priority representations cannot add projects once
# a higher-priority array is present.
_MEMORY_PROJECT_SCOPE_SQL = _jsonb_project_scope_values_sql(
    "metadata_json",
    legacy_keys=("project_id",),
    project_id_expression="project_id",
)


def _jsonb_string_array_identity_sql(array_expression: str) -> str:
    """Identity of a JSON array of strings. Any other shape contributes nothing."""

    normalized = _normalized_project_identifier_sql("floor_text.value")
    identity = _project_identifier_identity_sql("normalized_floor.value", already_normalized=True)
    return f"""
(
  SELECT COALESCE(
    jsonb_agg(floor_identity.value ORDER BY floor_identity.value COLLATE "C"),
    '[]'::jsonb
  )
  FROM (
    SELECT DISTINCT {identity} AS value
    FROM jsonb_array_elements(
      CASE
        WHEN jsonb_typeof({array_expression}) = 'array' THEN {array_expression}
        ELSE '[]'::jsonb
      END
    ) AS floor_element(value)
    CROSS JOIN LATERAL (
      SELECT CASE
        WHEN jsonb_typeof(floor_element.value) = 'string' THEN floor_element.value #>> '{{}}'
        ELSE ''
      END AS value
    ) AS floor_text
    CROSS JOIN LATERAL (
      SELECT {normalized} AS value
    ) AS normalized_floor
    WHERE normalized_floor.value <> ''
  ) AS floor_identity
)
"""


_PROJECT_FLOOR_SQL = _jsonb_string_array_identity_sql("metadata_json -> 'project_floor'")


# Overlap of scope united with floor. Original rows have no floor, so this
# matches the same rows as _MEMORY_PROJECT_SCOPE_SQL for them.
_MEMORY_GROUP_SCOPE_SQL = f"""
(
  SELECT COALESCE(
    jsonb_agg(grouped.value ORDER BY grouped.value COLLATE "C"),
    '[]'::jsonb
  )
  FROM (
    SELECT DISTINCT part.value
    FROM (
      SELECT jsonb_array_elements_text(({_MEMORY_PROJECT_SCOPE_SQL})) AS value
      UNION ALL
      SELECT jsonb_array_elements_text(({_PROJECT_FLOOR_SQL})) AS value
    ) AS part
  ) AS grouped
)
"""


_MEMORY_DIRECT_PEOPLE_SQL = """
EXISTS (
  SELECT 1
  FROM jsonb_path_query(
    jsonb_build_array(
      metadata_json -> 'person_id',
      metadata_json -> 'person_ids',
      metadata_json -> 'person',
      metadata_json -> 'people',
      metadata_json -> 'people_ids'
    ),
    'strict $.** ? (@.type() == "string")'
  ) AS scoped_person(value)
  WHERE lower(trim(both '"' FROM scoped_person.value::text)) = ANY(%s::text[])
)
"""


_MEMORY_SCOPE_EVENT_TIME_SQL = "COALESCE(valid_from, last_seen_at, updated_at, first_seen_at, created_at)"


_SCOPED_MEMORY_PROJECT_SQL = _jsonb_project_scope_values_sql(
    "m.metadata_json",
    legacy_keys=("project_id",),
    project_id_expression="m.project_id",
)


_SCOPED_MEMORY_DIRECT_PEOPLE_SQL = """
EXISTS (
  SELECT 1
  FROM jsonb_path_query(
    jsonb_build_array(
      m.metadata_json -> 'person_id',
      m.metadata_json -> 'person_ids',
      m.metadata_json -> 'person',
      m.metadata_json -> 'people',
      m.metadata_json -> 'people_ids'
    ),
    'strict $.** ? (@.type() == "string")'
  ) AS scoped_person(value)
  WHERE lower(trim(both '"' FROM scoped_person.value::text)) = ANY(%s::text[])
)
"""


_SCOPED_MEMORY_EVENT_TIME_SQL = "COALESCE(m.valid_from, m.last_seen_at, m.updated_at, m.first_seen_at, m.created_at)"


def _jsonb_scope_values_sql(metadata_expression: str, keys: tuple[str, ...]) -> str:
    """SQL predicate matching normalized string leaves under selected keys."""
    values = ",\n      ".join(f"{metadata_expression} #> '{{{','.join(key.split('.'))}}}'" for key in keys)
    return f"""
EXISTS (
  SELECT 1
  FROM jsonb_path_query(
    jsonb_build_array(
      {values}
    ),
    'strict $.** ? (@.type() == "string")'
  ) AS scoped_value(value)
  WHERE lower(trim(both '"' FROM scoped_value.value::text)) = ANY(%s::text[])
)
"""


_SOURCE_SCOPE_PROJECT_SQL = _jsonb_source_project_scope_values_sql("metadata_json")


_SOURCE_SCOPE_PEOPLE_SQL = _jsonb_scope_values_sql(
    "metadata_json",
    ("person_id", "person_ids", "person", "people", "people_ids"),
)


_SOURCE_SCOPE_EVENT_TIME_SQL = """
COALESCE(
  source_created_at,
  CASE
    WHEN pg_input_is_valid(metadata_json ->> 'session_date', 'timestamptz')
    THEN (metadata_json ->> 'session_date')::timestamptz
  END,
  CASE
    WHEN pg_input_is_valid(metadata_json ->> 'event_date', 'timestamptz')
    THEN (metadata_json ->> 'event_date')::timestamptz
  END,
  CASE
    WHEN pg_input_is_valid(metadata_json ->> 'date', 'timestamptz')
    THEN (metadata_json ->> 'date')::timestamptz
  END,
  captured_at
)
"""


_ARTIFACT_SCOPE_PROJECT_SQL = _jsonb_project_scope_values_sql(
    "metadata_json",
    legacy_keys=("project_id", "project", "projects"),
)


_OPEN_LOOP_SCOPE_PROJECT_SQL = _jsonb_project_scope_values_sql(
    "metadata_json",
    legacy_keys=("project_id", "project", "projects"),
    project_id_expression="project_id",
)


_OPEN_LOOP_SCOPE_PEOPLE_SQL = _jsonb_scope_values_sql(
    "metadata_json",
    ("person_id", "person_ids", "person", "people", "people_ids"),
)


_OPEN_LOOP_SCOPE_EVENT_TIME_SQL = "COALESCE(opened_at, updated_at, created_at)"


def _tsquery_any_expression(query: str) -> str | None:
    """OR-of-lexemes tsquery text for the ``match_any`` fallback pass.

    Each ``\\w+`` token is individually single-quoted so Postgres parses it
    as one literal lexeme; ``to_tsquery('english', ...)`` then stems it the
    same way the strict ``websearch_to_tsquery()`` pass does.
    """
    tokens = fts_fallback_tokens(query)
    if not tokens:
        return None
    return " | ".join(f"'{token}'" for token in tokens)


for _query_helper in (
    _escape_like_literal,
    _postgres_ascii_literal_contains_sql,
    _normalized_project_identifier_sql,
    _project_identifier_identity_sql,
    _jsonb_project_scope_values_sql,
    _jsonb_string_array_identity_sql,
    _jsonb_project_scope_leaf_values_sql,
    _jsonb_source_project_scope_values_sql,
    _jsonb_scope_values_sql,
    _tsquery_any_expression,
):
    _query_helper.__module__ = "alicebot_api.vnext_store"
    _query_helper.__qualname__ = _query_helper.__name__
del _query_helper
