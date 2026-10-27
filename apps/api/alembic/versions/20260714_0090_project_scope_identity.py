"""Repair source dedupe keys for canonical project-scope identity.

Revision ID: 20260714_0090
Revises: 20260713_0089

Runtime scope comparisons use a conservative cross-store set identity: ASCII
whitespace is normalized explicitly, ASCII-only identifiers compare without
case, and identifiers containing non-ASCII remain exact and case-sensitive.
Migration 0085 predated that contract: its source dedupe backfill retained
case and duplicates and omitted domain and sensitivity, so migrated rows could
disagree with newly captured rows.

This fix-forward migration recomputes the live-source identity from preserved
raw text, the presence-aware canonical/legacy scope, and classification.  If
historical duplicates collapse to one identity, only the oldest live row
retains the key; evidence rows are never deleted.  Explicitly empty canonical
scope remains authoritative throughout the repair.
"""

from __future__ import annotations

from alembic import op


revision = "20260714_0090"
down_revision = "20260713_0089"
branch_labels = None
depends_on = None


_DROP_INDEX = "DROP INDEX IF EXISTS sources_user_dedupe_key_unique_idx"
_CREATE_INDEX = """
CREATE UNIQUE INDEX sources_user_dedupe_key_unique_idx
  ON sources (user_id, dedupe_key)
  WHERE deleted_at IS NULL AND dedupe_key IS NOT NULL
"""

_CLEAR_LIVE_SOURCE_IDENTITIES = """
UPDATE sources
SET dedupe_key = NULL
WHERE deleted_at IS NULL
"""

# CPython 3.12's fixed Unicode whitespace table used by ``str.strip()``.
# Keep this explicit: PostgreSQL POSIX character classes are locale-sensitive
# and do not agree for controls such as NEL (U+0085) on every installation.
_PYTHON_312_STRIP_CODEPOINTS = (
    0x0009,
    0x000A,
    0x000B,
    0x000C,
    0x000D,
    0x001C,
    0x001D,
    0x001E,
    0x001F,
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    0x2000,
    0x2001,
    0x2002,
    0x2003,
    0x2004,
    0x2005,
    0x2006,
    0x2007,
    0x2008,
    0x2009,
    0x200A,
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
)
_PYTHON_312_STRIP_CHARS_SQL = " || ".join(f"chr({codepoint})" for codepoint in _PYTHON_312_STRIP_CODEPOINTS)

_INSTALL_CAPTURE_TEXT_NORMALIZER_HELPER = f"""
CREATE OR REPLACE FUNCTION pg_temp.alice_normalize_capture_text(raw_text text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT btrim(
    replace(replace(raw_text, E'\\r\\n', E'\\n'), E'\\r', E'\\n'),
    {_PYTHON_312_STRIP_CHARS_SQL}
  )
$$
"""

_INSTALL_SCOPE_LEAVES_HELPER = r"""
CREATE OR REPLACE FUNCTION pg_temp.alice_project_scope_leaves(input_value jsonb)
RETURNS SETOF text
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
  element jsonb;
BEGIN
  CASE jsonb_typeof(input_value)
    WHEN 'array' THEN
      FOR element IN SELECT value FROM jsonb_array_elements(input_value) AS items(value)
      LOOP
        RETURN QUERY
        SELECT nested.value
        FROM pg_temp.alice_project_scope_leaves(element) AS nested(value);
      END LOOP;
    WHEN 'string' THEN
      RETURN NEXT input_value #>> '{}';
    WHEN 'number' THEN
      -- Python accepts finite mathematical integers regardless of the JSON
      -- lexical spelling (1, 1.0, and 1e0 are one project identity).
      IF (input_value #>> '{}')::numeric = trunc((input_value #>> '{}')::numeric) THEN
        RETURN NEXT CASE
          WHEN (input_value #>> '{}')::numeric = 0 THEN '0'
          ELSE trunc((input_value #>> '{}')::numeric)::text
        END;
      END IF;
    WHEN 'boolean' THEN
      -- bool is an int subclass in Python, whose string spelling is title case.
      RETURN NEXT CASE WHEN input_value = 'true'::jsonb THEN 'True' ELSE 'False' END;
    ELSE
      RETURN;
  END CASE;
END;
$$
"""

_INSTALL_NORMALIZED_SCOPE_HELPER = r"""
CREATE OR REPLACE FUNCTION pg_temp.alice_normalized_project_scope(input_value jsonb)
RETURNS text[]
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT COALESCE(
    array_agg(deduplicated.normalized ORDER BY deduplicated.first_ordinal),
    ARRAY[]::text[]
  )
  FROM (
    SELECT normalized.value AS normalized, MIN(raw_value.ordinality) AS first_ordinal
    FROM pg_temp.alice_project_scope_leaves(input_value)
      WITH ORDINALITY AS raw_value(value, ordinality)
    CROSS JOIN LATERAL (
      SELECT btrim(
        regexp_replace(
          raw_value.value,
          '[' || chr(9) || chr(10) || chr(11) || chr(12) || chr(13) || ' ]+',
          ' ',
          'g'
        ),
        ' '
      ) AS value
    ) AS normalized
    WHERE normalized.value <> ''
    GROUP BY normalized.value
  ) AS deduplicated
$$
"""

_INSTALL_SOURCE_SCOPE_RESOURCE_HELPER = r"""
CREATE OR REPLACE FUNCTION pg_temp.alice_source_scope_resource(stored_metadata jsonb)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
  resource jsonb := '{}'::jsonb;
  metadata_container jsonb := '{}'::jsonb;
  direct_nested jsonb;
  stored_nested jsonb;
  nested_key text;
BEGIN
  IF jsonb_typeof(stored_metadata) = 'object' THEN
    resource := stored_metadata;
  END IF;
  IF jsonb_typeof(resource -> 'metadata_json') = 'object' THEN
    metadata_container := resource -> 'metadata_json';
  END IF;

  -- Old source rows stored these container members directly. Merge them into
  -- the universal resource envelope; an explicitly stored nested container
  -- wins only on the keys it actually carries.
  FOREACH nested_key IN ARRAY ARRAY['agentic_memory', 'agent_identity']
  LOOP
    direct_nested := resource -> nested_key;
    IF jsonb_typeof(direct_nested) = 'object' THEN
      stored_nested := metadata_container -> nested_key;
      IF jsonb_typeof(stored_nested) = 'object' THEN
        direct_nested := direct_nested || stored_nested;
      END IF;
      metadata_container := jsonb_set(
        metadata_container,
        ARRAY[nested_key],
        direct_nested,
        true
      );
    END IF;
  END LOOP;

  RETURN jsonb_set(resource, '{metadata_json}', metadata_container, true);
END;
$$
"""

_INSTALL_SCOPE_RESOLVER_HELPER = r"""
CREATE OR REPLACE FUNCTION pg_temp.alice_resolve_project_scope(resource jsonb)
RETURNS text[]
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
  metadata_container jsonb := '{}'::jsonb;
  scope_container jsonb := '{}'::jsonb;
  resolved text[] := ARRAY[]::text[];
BEGIN
  IF jsonb_typeof(resource) IS DISTINCT FROM 'object' THEN
    RETURN resolved;
  END IF;

  IF jsonb_typeof(resource -> 'metadata_json') = 'object' THEN
    metadata_container := resource -> 'metadata_json';
  END IF;
  IF jsonb_typeof(resource -> 'scope_json') = 'object' THEN
    scope_container := resource -> 'scope_json';
  END IF;

  -- Canonical presence is authoritative, even for empty, null, or malformed
  -- values.  Root wins, then metadata_json, then scope_json.
  IF resource ? 'project_scope' THEN
    IF jsonb_typeof(resource -> 'project_scope') = 'array' THEN
      RETURN pg_temp.alice_normalized_project_scope(resource -> 'project_scope');
    END IF;
    RETURN resolved;
  END IF;
  IF metadata_container ? 'project_scope' THEN
    IF jsonb_typeof(metadata_container -> 'project_scope') = 'array' THEN
      RETURN pg_temp.alice_normalized_project_scope(metadata_container -> 'project_scope');
    END IF;
    RETURN resolved;
  END IF;
  IF scope_container ? 'project_scope' THEN
    IF jsonb_typeof(scope_container -> 'project_scope') = 'array' THEN
      RETURN pg_temp.alice_normalized_project_scope(scope_container -> 'project_scope');
    END IF;
    RETURN resolved;
  END IF;

  -- Historical nested project_scope values aggregate in container order,
  -- agentic_memory before agent_identity. Presence is authoritative even
  -- when every present value normalizes to an empty scope.
  IF (
    jsonb_typeof(metadata_container -> 'agentic_memory') = 'object'
    AND (metadata_container -> 'agentic_memory') ? 'project_scope'
  ) OR (
    jsonb_typeof(metadata_container -> 'agent_identity') = 'object'
    AND (metadata_container -> 'agent_identity') ? 'project_scope'
  ) OR (
    jsonb_typeof(scope_container -> 'agentic_memory') = 'object'
    AND (scope_container -> 'agentic_memory') ? 'project_scope'
  ) OR (
    jsonb_typeof(scope_container -> 'agent_identity') = 'object'
    AND (scope_container -> 'agent_identity') ? 'project_scope'
  ) THEN
    RETURN pg_temp.alice_normalized_project_scope(
      jsonb_build_array(
        metadata_container #> '{agentic_memory,project_scope}',
        metadata_container #> '{agent_identity,project_scope}',
        scope_container #> '{agentic_memory,project_scope}',
        scope_container #> '{agent_identity,project_scope}'
      )
    );
  END IF;

  -- Root aliases precede every alias carried inside a container.
  resolved := pg_temp.alice_normalized_project_scope(
    jsonb_build_array(
      resource -> 'project_id',
      resource -> 'project',
      resource -> 'projects'
    )
  );
  IF cardinality(resolved) > 0 THEN
    RETURN resolved;
  END IF;

  -- Final-tier aliases aggregate metadata_json before scope_json.  The
  -- universal resolver intentionally supports nested aliases only under
  -- agentic_memory; agent_identity singular aliases are not a fallback.
  RETURN pg_temp.alice_normalized_project_scope(
    jsonb_build_array(
      metadata_container -> 'project_id',
      metadata_container -> 'project',
      metadata_container -> 'projects',
      metadata_container #> '{agentic_memory,project_id}',
      metadata_container #> '{agentic_memory,project}',
      metadata_container #> '{agentic_memory,projects}',
      scope_container -> 'project_id',
      scope_container -> 'project',
      scope_container -> 'projects',
      scope_container #> '{agentic_memory,project_id}',
      scope_container #> '{agentic_memory,project}',
      scope_container #> '{agentic_memory,projects}'
    )
  );
END;
$$
"""

_DROP_SCOPE_RESOLVER_HELPERS = (
    "DROP FUNCTION IF EXISTS pg_temp.alice_resolve_project_scope(jsonb)",
    "DROP FUNCTION IF EXISTS pg_temp.alice_source_scope_resource(jsonb)",
    "DROP FUNCTION IF EXISTS pg_temp.alice_normalized_project_scope(jsonb)",
    "DROP FUNCTION IF EXISTS pg_temp.alice_project_scope_leaves(jsonb)",
    "DROP FUNCTION IF EXISTS pg_temp.alice_normalize_capture_text(text)",
)

_RECOMPUTE_LIVE_SOURCE_IDENTITIES = r"""
WITH resolved AS (
  SELECT
    source.id,
    source.user_id,
    source.captured_at,
    source.content_hash,
    source.domain,
    source.sensitivity,
    source.metadata_json,
    pg_temp.alice_resolve_project_scope(
      pg_temp.alice_source_scope_resource(source.metadata_json)
    ) AS resolved_scope
  FROM sources AS source
  WHERE source.deleted_at IS NULL
),
computed AS (
  SELECT
    resolved.id,
    resolved.user_id,
    resolved.captured_at,
    CASE
      WHEN jsonb_typeof(resolved.metadata_json -> 'raw_text') = 'string' THEN
        'capture-md5:' || md5(
          pg_temp.alice_normalize_capture_text(
            resolved.metadata_json ->> 'raw_text'
          ) ||
          CASE
            WHEN scope_identity.value <> ''
              THEN chr(31) || 'project_scope:' || scope_identity.value
            ELSE ''
          END ||
          chr(31) || 'domain:' || lower(btrim(COALESCE(resolved.domain, 'unknown'))) ||
          chr(31) || 'sensitivity:' || lower(btrim(COALESCE(resolved.sensitivity, 'unknown')))
        )
      ELSE resolved.content_hash
    END AS computed_key
  FROM resolved
  CROSS JOIN LATERAL (
    SELECT COALESCE(
      string_agg(scope_value.value, chr(31) ORDER BY scope_value.value COLLATE "C"),
      ''
    ) AS value
    FROM (
      SELECT DISTINCT (
        CASE
          WHEN octet_length(normalized_scope.value) = char_length(normalized_scope.value)
            THEN translate(
              normalized_scope.value,
              'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
              'abcdefghijklmnopqrstuvwxyz'
            )
          ELSE normalized_scope.value
        END
      ) COLLATE "C" AS value
      FROM unnest(resolved.resolved_scope) AS normalized_scope(value)
    ) AS scope_value
  ) AS scope_identity
),
ranked AS (
  SELECT
    computed.*,
    row_number() OVER (
      PARTITION BY computed.user_id, computed.computed_key
      ORDER BY computed.captured_at ASC, computed.id ASC
    ) AS duplicate_rank
  FROM computed
)
UPDATE sources AS source
SET dedupe_key = ranked.computed_key
FROM ranked
WHERE source.id = ranked.id
  AND ranked.duplicate_rank = 1
"""


def upgrade() -> None:
    op.execute(_DROP_INDEX)
    op.execute(_INSTALL_CAPTURE_TEXT_NORMALIZER_HELPER)
    op.execute(_INSTALL_SCOPE_LEAVES_HELPER)
    op.execute(_INSTALL_NORMALIZED_SCOPE_HELPER)
    op.execute(_INSTALL_SOURCE_SCOPE_RESOURCE_HELPER)
    op.execute(_INSTALL_SCOPE_RESOLVER_HELPER)
    # The identity recompute below reads and writes rows on sources, whose
    # forced row-level security binds the documented table owner
    # (NOSUPERUSER NOBYPASSRLS) to owner policies that bind
    # app.current_user_id(), unset during an upgrade, so it silently
    # matched no rows for a vault that already carries sources while a
    # superuser upgrade rewrote them. Temporarily use the owner bypass, as
    # 0067, 0070, 0092, 0093 and 0095 to 0097 do, inside Alembic's single
    # transaction. Concurrent sessions never see the relaxed FORCE setting;
    # failure rolls back the bracket, the recompute and the schema change
    # together, and the forced setting is restored before the transaction
    # commits.
    op.execute("ALTER TABLE sources NO FORCE ROW LEVEL SECURITY")
    op.execute(_CLEAR_LIVE_SOURCE_IDENTITIES)
    op.execute(_RECOMPUTE_LIVE_SOURCE_IDENTITIES)
    op.execute("ALTER TABLE sources FORCE ROW LEVEL SECURITY")
    for statement in _DROP_SCOPE_RESOLVER_HELPERS:
        op.execute(statement)
    op.execute(_CREATE_INDEX)


def downgrade() -> None:
    # The stronger identity is safe for the preceding runtime and collapsing
    # historical duplicates is not reversibly attributable. Keep repaired
    # values and only rebuild the same partial uniqueness fence.
    op.execute(_DROP_INDEX)
    op.execute(_CREATE_INDEX)
