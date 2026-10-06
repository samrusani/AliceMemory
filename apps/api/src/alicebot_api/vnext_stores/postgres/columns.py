"""Column manifests shared by PostgreSQL vNext store seams."""

EVENT_LOG_COLUMNS = """
                  id,
                  user_id,
                  event_type,
                  actor_type,
                  actor_id,
                  target_type,
                  target_id,
                  occurred_at,
                  payload_json,
                  trace_id,
                  run_id,
                  integrity_hash
                """

MEMORY_COLUMNS = """
                  id,
                  user_id,
                  agent_profile_id,
                  memory_key,
                  value,
                  status,
                  source_event_ids,
                  memory_type,
                  confidence,
                  salience,
                  confirmation_status,
                  trust_class,
                  promotion_eligibility,
                  evidence_count,
                  independent_source_count,
                  extracted_by_model,
                  trust_reason,
                  valid_from,
                  valid_to,
                  last_confirmed_at,
                  title,
                  canonical_text,
                  summary,
                  domain,
                  sensitivity,
                  first_seen_at,
                  last_seen_at,
                  last_reviewed_at,
                  metadata_json,
                  commit_digest,
                  confirmation_id,
                  project_id,
                  created_by_agent_id,
                  run_id,
                  superseded_by,
                  supersedes,
                  created_at,
                  updated_at,
                  deleted_at
                """

REVISION_COLUMNS = """
                  id,
                  user_id,
                  memory_id,
                  sequence_no,
                  action,
                  memory_key,
                  previous_value,
                  new_value,
                  source_event_ids,
                  candidate,
                  revision_number,
                  revision_type,
                  text_before,
                  text_after,
                  reason,
                  actor_type,
                  actor_id,
                  metadata_json,
                  created_at
                """

PROVENANCE_COLUMNS = """
                  id,
                  user_id,
                  target_type,
                  target_id,
                  source_id,
                  source_chunk_id,
                  quote,
                  evidence_role,
                  confidence,
                  created_at
                """

ARTIFACT_COLUMNS = """
                  id,
                  user_id,
                  artifact_type,
                  title,
                  content_markdown,
                  status,
                  domain,
                  sensitivity,
                  generated_by,
                  prompt_hash,
                  model_info_json,
                  created_at,
                  reviewed_at,
                  promoted_at,
                  metadata_json
                """

GRAPH_EDGE_COLUMNS = """
                  id,
                  user_id,
                  from_type,
                  from_id,
                  to_type,
                  to_id,
                  edge_type,
                  confidence,
                  explanation,
                  created_by,
                  created_at,
                  observed_at,
                  valid_from,
                  valid_to,
                  metadata_json
                """

ENTITY_COLUMNS = """
                  id,
                  user_id,
                  entity_type,
                  name,
                  normalized_name,
                  aliases,
                  metadata_json,
                  created_at,
                  updated_at,
                  deleted_at,
                  first_observed_at,
                  last_observed_at,
                  mention_count
                """

ENTITY_RELATIONSHIP_EVENT_COLUMNS = """
                  id,
                  user_id,
                  entity_id,
                  relationship_type_before,
                  relationship_type_after,
                  changed_at,
                  source_id,
                  metadata_json
                """

BELIEF_COLUMNS = """
                  id,
                  user_id,
                  memory_id,
                  claim,
                  status,
                  confidence,
                  first_seen_at,
                  last_reinforced_at,
                  last_challenged_at,
                  superseded_by,
                  metadata_json
                """

OPEN_LOOP_COLUMNS = """
                  id,
                  user_id,
                  memory_id::text AS memory_id,
                  title,
                  status,
                  opened_at,
                  due_at,
                  resolved_at,
                  resolution_note,
                  created_at,
                  updated_at,
                  description,
                  priority,
                  project_id,
                  person_id,
                  source_id::text AS source_id,
                  closed_at,
                  domain,
                  sensitivity,
                  metadata_json
                """
