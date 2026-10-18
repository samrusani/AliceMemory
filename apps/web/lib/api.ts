export type ApiSource = "live" | "fixture";
export type PageDataMode = "live" | "fixture" | "mixed";

export type ApiConfig = {
  apiBaseUrl: string;
  userId: string;
  defaultThreadId: string;
  defaultToolId: string;
};

const DEFAULT_READ_TIMEOUT_MS = 15_000;
const DEFAULT_MUTATION_TIMEOUT_MS = 30_000;
const LONG_RUNNING_MUTATION_TIMEOUT_MS = 120_000;
const MAX_API_TIMEOUT_MS = 120_000;

// Client-memory only. The vNext operator console owns this value for the
// lifetime of its mounted browser session; it is never read from persistent
// configuration or included in request URLs.
let vNextOperatorAgentApiKey = "";

export type ApiRequestInit = RequestInit & {
  timeoutMs?: number;
};

export function setVNextOperatorAgentApiKey(agentApiKey: string) {
  vNextOperatorAgentApiKey = agentApiKey.trim();
}

export function clearVNextOperatorAgentApiKey() {
  vNextOperatorAgentApiKey = "";
}

function redactVNextOperatorAgentApiKey(value: string, requestSecrets: string[] = []) {
  return Array.from(new Set([vNextOperatorAgentApiKey, ...requestSecrets].filter(Boolean))).reduce(
    (sanitized, secret) => sanitized.split(secret).join("[redacted agent key]"),
    value,
  );
}

export type ThreadItem = {
  id: string;
  title: string;
  agent_profile_id: string;
  created_at: string;
  updated_at: string;
};

export type ThreadActivityPosture = "recent" | "current" | "stale";
export type ThreadRiskPosture = "normal" | "watch" | "risky";
export type ThreadHealthPosture = "healthy" | "watch" | "critical";

export type ThreadHealthThresholds = {
  recent_window_hours: number;
  stale_window_hours: number;
  risky_score_threshold: number;
};

export type ThreadHealthItem = {
  thread: ThreadItem;
  health_posture: ThreadHealthPosture;
  activity_posture: ThreadActivityPosture;
  risk_posture: ThreadRiskPosture;
  risk_score: number;
  last_activity_at: string | null;
  last_conversation_at: string | null;
  hours_since_last_activity: number | null;
  conversation_event_count: number;
  operational_event_count: number;
  active_session_count: number;
  open_loop_count: number;
  stale_open_loop_count: number;
  unresolved_contradiction_count: number;
  weak_trust_signal_count: number;
  reasons: string[];
  recommended_action: string;
};

export type ThreadHealthDashboardSummary = {
  posture: ThreadHealthPosture;
  total_thread_count: number;
  recent_thread_count: number;
  stale_thread_count: number;
  risky_thread_count: number;
  watch_thread_count: number;
  thresholds: ThreadHealthThresholds;
  recent_threads: ThreadHealthItem[];
  stale_threads: ThreadHealthItem[];
  risky_threads: ThreadHealthItem[];
  items: ThreadHealthItem[];
  sources: string[];
};

export type ResumptionBriefSectionSummary = {
  limit: number;
  returned_count: number;
  total_count: number;
  order: string[];
};

export type ToolRoutingReason = {
  code: string;
  source: string;
  message: string;
  tool_id: string | null;
  policy_id: string | null;
  consent_key: string | null;
};

export type ToolRecord = {
  id: string;
  tool_key: string;
  name: string;
  description: string;
  version: string;
  metadata_version: string;
  active: boolean;
  tags: string[];
  action_hints: string[];
  scope_hints: string[];
  domain_hints: string[];
  risk_hints: string[];
  metadata: Record<string, unknown>;
  created_at: string;
};

export type JsonObject = Record<string, unknown>;

export type VNextRow = JsonObject & {
  id: string;
  status?: string;
  title?: string | null;
  domain?: string | null;
  sensitivity?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type VNextSourceRecord = VNextRow & {
  source_type: string;
  captured_at: string;
  connector_name?: string | null;
  external_id?: string | null;
  metadata_json?: JsonObject;
};

export type VNextMemoryRecord = VNextRow & {
  memory_key: string;
  memory_type: string;
  canonical_text: string;
  summary?: string | null;
  value?: JsonObject;
  metadata_json?: JsonObject;
};

export type VNextArtifactRecord = VNextRow & {
  artifact_type: string;
  content_markdown: string;
  generated_by: string;
  prompt_hash?: string | null;
  model_info_json?: JsonObject | null;
  metadata_json?: JsonObject;
};

export type VNextArtifactQualityEvalRecord = VNextRow & {
  artifact_id: string;
  reviewer_id?: string | null;
  usefulness?: number | null;
  accuracy?: number | null;
  source_grounding?: number | null;
  novel_connections?: number | null;
  actionability?: number | null;
  hallucination_risk?: number | null;
  verbosity: string;
  missed_context?: string | null;
  comments?: string | null;
  metadata_json?: JsonObject;
};

export type VNextProjectRecord = VNextRow & {
  name: string;
  slug: string;
  current_state?: string | null;
  description?: string | null;
  metadata_json?: JsonObject;
};

export type VNextOpenLoopRecord = VNextRow & {
  title: string;
  description?: string | null;
  due_at?: string | null;
  priority?: string | null;
  memory_id?: string | null;
  project_id?: string | null;
  source_id?: string | null;
  resolution_note?: string | null;
  metadata_json?: JsonObject;
};

export type VNextPersonRecord = VNextRow & {
  name: string;
  relationship_type?: string | null;
  organization?: string | null;
  notes?: string | null;
};

export type VNextBeliefRecord = VNextRow & {
  claim: string;
  confidence?: number;
  memory_id?: string;
};

export type VNextTaskRecord = VNextRow & {
  title: string;
  task_type: string;
  instructions?: string;
  write_policy?: string;
};

export type VNextEventRecord = VNextRow & {
  event_type: string;
  actor_type: string;
  actor_id?: string | null;
  target_type?: string | null;
  target_id?: string | null;
  occurred_at: string;
  payload_json?: JsonObject;
  trace_id?: string | null;
  run_id?: string | null;
};

export type VNextBrainCharterRecord = VNextRow & {
  content_markdown: string;
  owner_json?: JsonObject;
  memory_philosophy_json?: JsonObject;
  life_domains_json?: JsonObject;
  active_projects_json?: unknown[];
  communication_style_json?: JsonObject;
  priorities_json?: JsonObject;
  autonomous_rules_json?: unknown[];
  quality_standard_json?: unknown[];
};

export type VNextProjectDashboard = {
  project: VNextProjectRecord;
  state: string | null;
  memories: VNextMemoryRecord[];
  open_loops: VNextOpenLoopRecord[];
  artifacts: VNextArtifactRecord[];
  counts: {
    memories: number;
    open_loops: number;
    artifacts: number;
  };
};

export type VNextAgentIdentityRecord = VNextRow & {
  agent_id: string;
  agent_type: string;
  permission_profile: string;
  display_name?: string | null;
  project_scope_json?: unknown[];
  metadata_json?: JsonObject;
};

export type VNextPolicyDecisionRecord = {
  decision: string;
  action: string;
  permission_profile: string;
  reasons: string[];
  effective_domains: string[];
  effective_sensitivity_allowed: string[];
  review_required: boolean;
  trace_id: string;
};

export type VNextSchedulerWorkflowRecord = VNextRow & {
  workflow_type: string;
  enabled: boolean;
  paused: boolean;
  schedule_json?: JsonObject;
  timezone: string;
  next_run_at?: string | null;
  last_run_id?: string | null;
  last_run_at?: string | null;
  last_result?: string | null;
  last_error?: string | null;
  metadata_json?: JsonObject;
};

export type VNextSchedulerRunRecord = VNextRow & {
  workflow_type: string;
  status: string;
  triggered_by: string;
  trace_id: string;
  started_at: string;
  finished_at?: string | null;
  artifact_id?: string | null;
  error_message?: string | null;
  policy_decision_json?: JsonObject;
  agent_identity_json?: JsonObject;
  metadata_json?: JsonObject;
};

export type VNextSchedulerDaemonStatus = {
  configured?: boolean;
  running?: boolean;
  pid?: number | null;
  pid_file?: string;
  status_file?: string;
  started_at?: string;
  stopped_at?: string;
  last_heartbeat_at?: string;
  last_due_scan_at?: string;
  last_due_count?: number;
  last_error?: string | null;
  mode?: string;
};

export type VNextSchedulerStatus = {
  mode: string;
  disabled_by_default: boolean;
  workflows: VNextSchedulerWorkflowRecord[];
  recent_runs: VNextSchedulerRunRecord[];
  enabled_count: number;
  paused_count: number;
  last_failure: VNextSchedulerRunRecord | null;
  recent_failures?: VNextSchedulerRunRecord[];
  last_due_scan?: VNextEventRecord | null;
  next_due_workflow?: VNextSchedulerWorkflowRecord | null;
  currently_running_workflow?: VNextSchedulerRunRecord | null;
  last_success_by_workflow?: Record<string, VNextSchedulerRunRecord | null>;
  daemon?: VNextSchedulerDaemonStatus;
};

export type VNextAgentActivity = {
  agents: VNextAgentIdentityRecord[];
  recent_events: VNextEventRecord[];
  policy_blocks: VNextEventRecord[];
  generated_artifacts: VNextArtifactRecord[];
  pending_review_items: VNextMemoryRecord[];
  recent_commits: VNextMemoryRecord[];
  inline_confirmations: VNextMemoryRecord[];
};

export type VNextConnectorHealthRecord = {
  connector_name: string;
  display_name?: string;
  enabled: boolean;
  configured: boolean;
  default_domain?: string;
  default_sensitivity?: string;
  last_sync_at?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_error?: string | null;
  last_captured_item?: JsonObject | null;
  items_seen: number;
  items_captured: number;
  items_deduped: number;
  items_failed: number;
  cursor_state?: string | null;
  average_processing_time?: number | null;
  sync_mode?: string;
  poll_interval_seconds?: number | null;
  validation_errors?: string[];
  secret_configured?: boolean;
};

export type VNextConnectorConfigRecord = {
  connector_id?: string | null;
  connector_name: string;
  enabled: boolean;
  configured: boolean;
  secret_ref?: string | null;
  secret_configured?: boolean;
  default_domain: string;
  default_sensitivity: string;
  sync_mode?: string;
  poll_interval_seconds?: number | null;
  config_json?: JsonObject;
  validation_errors?: string[];
  updated_at?: string | null;
  last_configured_at?: string | null;
};

export type VNextBrowserClipCapability = {
  status: "issued";
  capability: string;
  origin: string;
  expires_at: string;
  one_time: true;
};

export type VNextConnectorStatusPayload = {
  config: VNextConnectorConfigRecord;
  health: VNextConnectorHealthRecord;
  recent_captures: VNextSourceRecord[];
  recent_failures: VNextEventRecord[];
};

export type VNextDogfoodingDashboard = {
  captures_by_connector: Array<{ connector_name: string; count: number }>;
  captures_today: number;
  captures_this_week: number;
  capture_trend_by_day?: Array<{ date: string; count: number }>;
  capture_trend_by_week?: Array<{ period: string; count: number }>;
  candidate_memories_created: number;
  memory_status_counts: Record<string, number>;
  candidate_memory_review_rate?: number;
  generated_artifacts_created: number;
  artifact_status_counts: Record<string, number>;
  artifact_quality_average?: number | null;
  artifact_quality_rating_count: number;
  artifact_rating_trend?: Array<{ date: string; count: number }>;
  daily_brief_review_status?: string | null;
  weekly_synthesis_review_status?: string | null;
  connections_surfaced: number;
  contradictions_surfaced: number;
  open_loop_status_counts: Record<string, number>;
  open_loops_created: number;
  open_loops_closed: number;
  agent_context_packs_requested: number;
  agent_memory_proposals: number;
  policy_blocks_filters: number;
  connector_failures: number;
  top_failure_causes?: Array<{ cause: string; count: number }>;
  scheduler_freshness?: JsonObject;
  agent_activity_summary?: JsonObject;
  policy_block_filter_summary?: JsonObject;
  dogfood_readiness?: {
    status: "green" | "yellow" | "red";
    reason: string;
    captures_today: number;
    scheduler_fresh: boolean;
    artifact_rating_count: number;
    policy_blocks_filters: number;
  };
  last_successful_scheduler_run?: JsonObject | null;
  connector_health: { items: VNextConnectorHealthRecord[]; count: number; order: string[] };
  insight_feedback: {
    count: number;
    useful_yes: number;
    useful_no: number;
    useful_not_sure: number;
    missed_something_yes: number;
  };
};

export type VNextDoctorCheckRecord = {
  name: string;
  status: string;
  severity: string;
  message: string;
  recommended_fix?: string | null;
  details?: JsonObject;
};

export type VNextDoctorPayload = {
  status: "pass" | "warn" | "fail" | string;
  fix_safe_applied: boolean;
  ci_mode: boolean;
  blocking_failure_count: number;
  warning_count: number;
  checks: VNextDoctorCheckRecord[];
  recommended_fixes: Array<string | null>;
  migration_status: JsonObject;
  connector_health: { items: VNextConnectorHealthRecord[]; count: number; order: string[] };
};

export type VNextSourceTracePayload = {
  trace_id: string;
  trace_kind: string;
  source: VNextSourceRecord;
  chunks: JsonObject[];
  candidate_memories: VNextMemoryRecord[];
  artifacts: VNextArtifactRecord[];
  open_loops: VNextOpenLoopRecord[];
  events: VNextEventRecord[];
  summary: {
    source_id: string;
    chunk_count: number;
    candidate_memory_count: number;
    artifact_count: number;
    open_loop_count: number;
    event_count: number;
  };
};

export type VNextArtifactTracePayload = {
  trace_id: string;
  trace_kind: string;
  artifact: VNextArtifactRecord;
  sources: VNextSourceRecord[];
  quality_evals: VNextArtifactQualityEvalRecord[];
  events: VNextEventRecord[];
  summary: JsonObject;
};

export type VNextTelemetryCounter = {
  count: number;
  [key: string]: unknown;
};

export type VNextPolicyTelemetrySummary = {
  total_agent_events: number;
  total_policy_decisions: number;
  policy_blocks_by_agent: VNextTelemetryCounter[];
  policy_filters_by_agent: VNextTelemetryCounter[];
  requires_review_by_agent: VNextTelemetryCounter[];
  restricted_domains_requested: VNextTelemetryCounter[];
  workflows_triggered_by_agents: VNextTelemetryCounter[];
  memory_proposals_by_agent: VNextTelemetryCounter[];
  artifact_generation_by_agent: VNextTelemetryCounter[];
};

export type VNextWorkspacePayload = {
  mode: "live";
  summary: {
    source_count: number;
    candidate_memory_count: number;
    review_memory_count: number;
    artifact_count: number;
    open_loop_count: number;
    project_count: number;
    event_count: number;
    agent_count?: number;
    scheduler_enabled_count?: number;
    quality_eval_count?: number;
    memory_status_counts: Record<string, number>;
    artifact_status_counts: Record<string, number>;
    open_loop_status_counts: Record<string, number>;
  };
  sources: VNextSourceRecord[];
  review_memories: VNextMemoryRecord[];
  artifacts: VNextArtifactRecord[];
  projects: VNextProjectRecord[];
  project_dashboards: VNextProjectDashboard[];
  open_loops: VNextOpenLoopRecord[];
  people: VNextPersonRecord[];
  beliefs: VNextBeliefRecord[];
  tasks: VNextTaskRecord[];
  recent_events: VNextEventRecord[];
  quality_evals?: VNextArtifactQualityEvalRecord[];
  agent_activity?: VNextAgentActivity;
  connector_health?: { items: VNextConnectorHealthRecord[]; count: number; order: string[] };
  dogfooding?: VNextDogfoodingDashboard;
  doctor?: VNextDoctorPayload;
  traceability?: { items: VNextSourceTracePayload[]; count: number; order: string[] };
  policy_telemetry?: VNextPolicyTelemetrySummary;
  scheduler?: VNextSchedulerStatus;
  brain_charter: VNextBrainCharterRecord | null;
};

export type VNextSourceCreatePayload = {
  user_id: string;
  raw_text: string;
  title?: string | null;
  domain: string;
  sensitivity: string;
};

export type VNextSourceReviewPayload = {
  user_id: string;
  action: "review" | "update" | "assign_project" | "archive";
  title?: string;
  domain?: string;
  sensitivity?: string;
  project_id?: string;
  review_note?: string;
};

export type VNextMemoryReviewPayload = {
  user_id: string;
  action: "accept" | "edit" | "reject" | "private" | "assign_project" | "promote";
  title?: string;
  canonical_text?: string;
  summary?: string;
  domain?: string;
  sensitivity?: string;
  project_id?: string;
  reason?: string;
};

export type VNextMemoryConfirmationPayload = {
  user_id: string;
  confirmation_id: string;
  action: "confirm" | "edit" | "reject";
  canonical_text?: string;
  rationale?: string;
};

export type VNextMemoryConfirmationResponse = {
  status: "committed" | "rejected";
  write_mode: "confirm_inline" | "reject";
  confirmation_id?: string;
  reason?: string;
  memory: VNextMemoryRecord;
};

export type VNextProjectCreatePayload = {
  user_id: string;
  name: string;
  slug?: string;
  description?: string;
  current_state?: string;
  domain: string;
  sensitivity: string;
};

export type VNextOpenLoopCreatePayload = {
  user_id: string;
  title: string;
  description?: string;
  due_at?: string;
  priority?: string;
  memory_id?: string;
  project_id?: string;
  source_id?: string;
  domain: string;
  sensitivity: string;
};

export type VNextOpenLoopReviewPayload = {
  user_id: string;
  action: "close" | "snooze" | "edit" | "reopen";
  title?: string;
  description?: string;
  due_at?: string;
  priority?: string;
  resolution_note?: string;
};

export type VNextBrainCharterUpsertPayload = {
  user_id: string;
  content_markdown: string;
  owner_json?: JsonObject;
  memory_philosophy_json?: JsonObject;
  life_domains_json?: JsonObject;
  active_projects_json?: unknown[];
  communication_style_json?: JsonObject;
  priorities_json?: JsonObject;
  autonomous_rules_json?: unknown[];
  quality_standard_json?: unknown[];
  sensitivity: string;
};

export type VNextContextPack = JsonObject & {
  context_pack_id: string;
  query_interpretation: JsonObject;
  current_known_state: VNextMemoryRecord[];
  relevant_memories: VNextMemoryRecord[];
  relevant_beliefs: VNextMemoryRecord[];
  decisions: VNextMemoryRecord[];
  open_loops: VNextOpenLoopRecord[];
  supporting_evidence: JsonObject[];
  contradicting_evidence: JsonObject[];
  sources: VNextSourceRecord[];
  warnings: string[];
  trace_id: string;
  trace: JsonObject;
};

export type GovernedRequestRecord = {
  thread_id: string;
  tool_id: string;
  action: string;
  scope: string;
  domain_hint: string | null;
  risk_hint: string | null;
  attributes: Record<string, unknown>;
};

export type ApprovalItem = {
  id: string;
  thread_id: string;
  task_run_id: string | null;
  task_step_id: string | null;
  status: string;
  request: GovernedRequestRecord;
  tool: ToolRecord;
  routing: {
    decision: string;
    reasons: ToolRoutingReason[];
    trace: {
      trace_id: string;
      trace_event_count: number;
    };
  };
  created_at: string;
  resolution: {
    resolved_at: string;
    resolved_by_user_id: string;
  } | null;
};

export type TaskItem = {
  id: string;
  thread_id: string;
  tool_id: string;
  status: string;
  request: GovernedRequestRecord;
  tool: ToolRecord;
  latest_approval_id: string | null;
  latest_execution_id: string | null;
  created_at: string;
  updated_at: string;
};

export type TaskStepItem = {
  id: string;
  task_id: string;
  sequence_no: number;
  kind: string;
  status: string;
  request: GovernedRequestRecord;
  outcome: {
    routing_decision: string;
    approval_id: string | null;
    approval_status: string | null;
    execution_id: string | null;
    execution_status: string | null;
    blocked_reason: string | null;
  };
  lineage: {
    parent_step_id: string | null;
    source_approval_id: string | null;
    source_execution_id: string | null;
  };
  trace: {
    trace_id: string;
    trace_kind: string;
  };
  created_at: string;
  updated_at: string;
};

export type TaskStepListSummary = {
  task_id: string;
  total_count: number;
  latest_sequence_no: number | null;
  latest_status: string | null;
  next_sequence_no: number;
  append_allowed: boolean;
  order: string[];
};

export type TaskRunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "waiting_user"
  | "paused"
  | "failed"
  | "done"
  | "cancelled";
export type TaskRunStopReason =
  | "waiting_approval"
  | "waiting_user"
  | "budget_exhausted"
  | "paused"
  | "approval_rejected"
  | "policy_blocked"
  | "retry_exhausted"
  | "fatal_error"
  | "done"
  | "cancelled";
export type TaskRunFailureClass = "transient" | "policy" | "approval" | "budget" | "fatal";
export type TaskRunRetryPosture =
  | "none"
  | "retryable"
  | "exhausted"
  | "terminal"
  | "paused"
  | "awaiting_approval"
  | "awaiting_user";

export type TaskRunItem = {
  id: string;
  task_id: string;
  status: TaskRunStatus;
  checkpoint: JsonObject;
  tick_count: number;
  step_count: number;
  max_ticks: number;
  retry_count: number;
  retry_cap: number;
  retry_posture: TaskRunRetryPosture;
  failure_class: TaskRunFailureClass | null;
  stop_reason: TaskRunStopReason | null;
  last_transitioned_at: string;
  created_at: string;
  updated_at: string;
};

export type TaskRunListSummary = {
  task_id: string;
  total_count: number;
  order: string[];
};

export type ToolExecutionResult = {
  handler_key: string | null;
  status: string;
  output: JsonObject | null;
  reason: string | null;
  budget_decision?: JsonObject;
};

export type ToolExecutionItem = {
  id: string;
  approval_id: string;
  task_run_id: string | null;
  task_step_id: string;
  thread_id: string;
  tool_id: string;
  trace_id: string;
  request_event_id: string | null;
  result_event_id: string | null;
  status: string;
  handler_key: string | null;
  idempotency_key: string | null;
  request: GovernedRequestRecord;
  tool: ToolRecord;
  result: ToolExecutionResult;
  executed_at: string;
};

export type ApprovalExecutionResponse = {
  request: {
    approval_id: string;
    task_run_id: string | null;
    task_step_id: string;
  };
  approval: ApprovalItem;
  tool: ToolRecord;
  result: ToolExecutionResult;
  events: {
    request_event_id: string;
    request_sequence_no: number;
    result_event_id: string;
    result_sequence_no: number;
  } | null;
  trace: {
    trace_id: string;
    trace_event_count: number;
  };
};

export type TraceReviewSummaryItem = {
  id: string;
  thread_id: string;
  kind: string;
  compiler_version: string;
  status: string;
  created_at: string;
  trace_event_count: number;
};

export type TraceReviewItem = TraceReviewSummaryItem & {
  limits: Record<string, unknown>;
};

export type TraceReviewEventItem = {
  id: string;
  trace_id: string;
  sequence_no: number;
  kind: string;
  payload: unknown;
  created_at: string;
};

export type TraceReviewListSummary = {
  total_count: number;
  order: string[];
};

export type TraceReviewEventListSummary = {
  trace_id: string;
  total_count: number;
  order: string[];
};

export type MemoryReviewStatus = "active" | "deleted";
export type MemoryReviewStatusFilter = MemoryReviewStatus | "all";
export type OpenLoopStatus = "open" | "resolved" | "dismissed";
export type OpenLoopStatusFilter = OpenLoopStatus | "all";
export type MemoryReviewLabelValue =
  | "correct"
  | "incorrect"
  | "outdated"
  | "insufficient_evidence";

export type MemoryType =
  | "preference"
  | "identity_fact"
  | "relationship_fact"
  | "project_fact"
  | "decision"
  | "commitment"
  | "routine"
  | "constraint"
  | "working_style";

export type MemoryConfirmationStatus = "unconfirmed" | "confirmed" | "contested";

export type MemoryReviewRecord = {
  id: string;
  memory_key: string;
  value: unknown;
  status: MemoryReviewStatus;
  source_event_ids: string[];
  memory_type?: MemoryType;
  confidence?: number | null;
  salience?: number | null;
  confirmation_status?: MemoryConfirmationStatus;
  valid_from?: string | null;
  valid_to?: string | null;
  last_confirmed_at?: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
};

export type MemoryReviewListSummary = {
  status: MemoryReviewStatusFilter;
  limit: number;
  returned_count: number;
  total_count: number;
  has_more: boolean;
  order: string[];
};

export type MemoryRevisionReviewRecord = {
  id: string;
  memory_id: string;
  sequence_no: number;
  action: string;
  memory_key: string;
  previous_value: unknown | null;
  new_value: unknown | null;
  source_event_ids: string[];
  created_at: string;
};

export type MemoryRevisionReviewListSummary = {
  memory_id: string;
  limit: number;
  returned_count: number;
  total_count: number;
  has_more: boolean;
  order: string[];
};

export type MemoryReviewLabelCounts = {
  correct: number;
  incorrect: number;
  outdated: number;
  insufficient_evidence: number;
};

export type MemoryReviewLabelRecord = {
  id: string;
  memory_id: string;
  reviewer_user_id: string;
  label: MemoryReviewLabelValue;
  note: string | null;
  created_at: string;
};

export type MemoryReviewLabelSummary = {
  memory_id: string;
  total_count: number;
  counts_by_label: MemoryReviewLabelCounts;
  order: MemoryReviewLabelValue[];
};

export type MemoryReviewQueueItem = {
  id: string;
  memory_key: string;
  value: unknown;
  status: "active";
  source_event_ids: string[];
  memory_type?: MemoryType;
  confidence?: number | null;
  salience?: number | null;
  confirmation_status?: MemoryConfirmationStatus;
  valid_from?: string | null;
  valid_to?: string | null;
  last_confirmed_at?: string | null;
  is_high_risk: boolean;
  is_stale_truth: boolean;
  queue_priority_mode: MemoryReviewQueuePriorityMode;
  priority_reason: string;
  created_at: string;
  updated_at: string;
};

export type MemoryReviewQueuePriorityMode =
  | "oldest_first"
  | "recent_first"
  | "high_risk_first"
  | "stale_truth_first";

export type MemoryReviewQueueSummary = {
  memory_status: "active";
  review_state: "unlabeled";
  priority_mode: MemoryReviewQueuePriorityMode;
  available_priority_modes: MemoryReviewQueuePriorityMode[];
  limit: number;
  returned_count: number;
  total_count: number;
  has_more: boolean;
  order: string[];
};

export type MemoryQualityGateStatus =
  | "healthy"
  | "needs_review"
  | "insufficient_sample"
  | "degraded";

export type MemoryQualityReviewAction =
  | "adjudicate_minimum_sample"
  | "review_high_risk_queue"
  | "review_stale_truth_queue"
  | "drain_unlabeled_queue"
  | "investigate_correction_recurrence"
  | "remediate_freshness_drift"
  | "monitor_quality_posture";

export type MemoryQualityGateSummary = {
  status: MemoryQualityGateStatus;
  precision: number | null;
  precision_target: number;
  adjudicated_sample_count: number;
  minimum_adjudicated_sample: number;
  remaining_to_minimum_sample: number;
  unlabeled_memory_count: number;
  high_risk_memory_count: number;
  stale_truth_count: number;
  superseded_active_conflict_count: number;
  counts: {
    active_memory_count: number;
    labeled_active_memory_count: number;
    adjudicated_correct_count: number;
    adjudicated_incorrect_count: number;
    outdated_label_count: number;
    insufficient_evidence_label_count: number;
  };
};

export type MemoryEvaluationSummary = {
  total_memory_count: number;
  active_memory_count: number;
  deleted_memory_count: number;
  labeled_memory_count: number;
  unlabeled_memory_count: number;
  total_label_row_count: number;
  label_row_counts_by_value: MemoryReviewLabelCounts;
  label_value_order: MemoryReviewLabelValue[];
  quality_gate?: MemoryQualityGateSummary;
};

export type MemoryTrustQueueAgingSummary = {
  anchor_updated_at: string | null;
  newest_updated_at: string | null;
  oldest_updated_at: string | null;
  backlog_span_hours: number;
  fresh_within_24h_count: number;
  aging_24h_to_72h_count: number;
  stale_over_72h_count: number;
};

export type MemoryTrustQueuePostureSummary = {
  priority_mode: MemoryReviewQueuePriorityMode;
  total_count: number;
  high_risk_count: number;
  stale_truth_count: number;
  priority_reason_counts: Record<string, number>;
  order: string[];
  aging: MemoryTrustQueueAgingSummary;
};

export type MemoryTrustCorrectionFreshnessSummary = {
  total_open_loop_count: number;
  stale_open_loop_count: number;
  correction_recurrence_count: number;
  freshness_drift_count: number;
};

export type MemoryTrustRecommendedReview = {
  priority_mode: MemoryReviewQueuePriorityMode;
  action: MemoryQualityReviewAction;
  reason: string;
};

export type MemoryHygienePosture = "healthy" | "watch" | "critical";
export type MemoryHygieneFocusKind =
  | "duplicates"
  | "stale_facts"
  | "unresolved_contradictions"
  | "weak_trust"
  | "review_queue_pressure";

export type MemoryDuplicateGroup = {
  group_key: string;
  memory_type: string;
  normalized_value: string;
  count: number;
  memory_ids: string[];
  memory_keys: string[];
  latest_updated_at: string;
};

export type MemoryReviewQueuePressureSummary = {
  posture: MemoryHygienePosture;
  total_count: number;
  stale_over_72h_count: number;
  aging_24h_to_72h_count: number;
  reason: string;
};

export type MemoryHygieneFocusItem = {
  kind: MemoryHygieneFocusKind;
  posture: MemoryHygienePosture;
  count: number;
  reason: string;
  action: string;
  sample_ids: string[];
};

export type MemoryHygieneDashboardSummary = {
  posture: MemoryHygienePosture;
  reason: string;
  duplicate_group_count: number;
  duplicate_memory_count: number;
  stale_fact_count: number;
  unresolved_contradiction_count: number;
  weak_trust_count: number;
  review_queue_pressure: MemoryReviewQueuePressureSummary;
  duplicate_groups: MemoryDuplicateGroup[];
  focus: MemoryHygieneFocusItem[];
  sources: string[];
};

export type MemoryTrustDashboardSummary = {
  quality_gate: MemoryQualityGateSummary;
  queue_posture: MemoryTrustQueuePostureSummary;
  retrieval_quality: RetrievalEvaluationSummary;
  correction_freshness: MemoryTrustCorrectionFreshnessSummary;
  recommended_review: MemoryTrustRecommendedReview;
  sources: string[];
};

export type OpenLoopRecord = {
  id: string;
  memory_id: string | null;
  title: string;
  status: OpenLoopStatus;
  opened_at: string;
  due_at: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  created_at: string;
  updated_at: string;
};

export type OpenLoopListSummary = {
  status: OpenLoopStatusFilter;
  limit: number;
  returned_count: number;
  total_count: number;
  has_more: boolean;
  order: string[];
};

export type EntityType = "person" | "merchant" | "product" | "project" | "routine";

export type EntityRecord = {
  id: string;
  entity_type: EntityType;
  name: string;
  source_memory_ids: string[];
  created_at: string;
};

export type EntityListSummary = {
  total_count: number;
  order: string[];
};

export type EntityEdgeRecord = {
  id: string;
  from_entity_id: string;
  to_entity_id: string;
  relationship_type: string;
  valid_from: string | null;
  valid_to: string | null;
  source_memory_ids: string[];
  created_at: string;
};

export type EntityEdgeListSummary = {
  entity_id: string;
  total_count: number;
  order: string[];
};

export type GmailReadonlyScope = "https://www.googleapis.com/auth/gmail.readonly";

export type GmailAccountRecord = {
  id: string;
  provider: string;
  auth_kind: string;
  provider_account_id: string;
  email_address: string;
  display_name: string | null;
  scope: GmailReadonlyScope;
  created_at: string;
  updated_at: string;
};

export type GmailAccountListSummary = {
  total_count: number;
  order: string[];
};

export type GmailAccountConnectPayload = {
  user_id: string;
  provider_account_id: string;
  email_address: string;
  display_name?: string | null;
  scope: GmailReadonlyScope;
  access_token: string;
  refresh_token?: string | null;
  client_id?: string | null;
  client_secret?: string | null;
  access_token_expires_at?: string | null;
};

export type GmailMessageIngestPayload = {
  user_id: string;
  task_workspace_id: string;
};

export type GmailMessageIngestionRecord = {
  provider_message_id: string;
  artifact_relative_path: string;
  media_type: string;
};

export type GmailMessageIngestionResponse = {
  account: GmailAccountRecord;
  message: GmailMessageIngestionRecord;
  artifact: TaskArtifactRecord;
  summary: TaskArtifactChunkListSummary;
};

export type CalendarReadonlyScope = "https://www.googleapis.com/auth/calendar.readonly";

export type CalendarAccountRecord = {
  id: string;
  provider: string;
  auth_kind: string;
  provider_account_id: string;
  email_address: string;
  display_name: string | null;
  scope: CalendarReadonlyScope;
  created_at: string;
  updated_at: string;
};

export type CalendarAccountListSummary = {
  total_count: number;
  order: string[];
};

export type CalendarAccountConnectPayload = {
  user_id: string;
  provider_account_id: string;
  email_address: string;
  display_name?: string | null;
  scope: CalendarReadonlyScope;
  access_token: string;
};

export type CalendarEventIngestPayload = {
  user_id: string;
  task_workspace_id: string;
};

export type CalendarEventIngestionRecord = {
  provider_event_id: string;
  artifact_relative_path: string;
  media_type: string;
};

export type CalendarEventIngestionResponse = {
  account: CalendarAccountRecord;
  event: CalendarEventIngestionRecord;
  artifact: TaskArtifactRecord;
  summary: TaskArtifactChunkListSummary;
};

export type CalendarEventSummaryRecord = {
  provider_event_id: string;
  status: string | null;
  summary: string | null;
  start_time: string | null;
  end_time: string | null;
  html_link: string | null;
  updated_at: string | null;
};

export type CalendarEventListSummary = {
  total_count: number;
  limit: number;
  order: string[];
  time_min: string | null;
  time_max: string | null;
};

export type CalendarEventListResponse = {
  account: CalendarAccountRecord;
  items: CalendarEventSummaryRecord[];
  summary: CalendarEventListSummary;
};

export type CalendarEventListQuery = {
  limit?: number;
  timeMin?: string;
  timeMax?: string;
};

export type TaskWorkspaceStatus = "active";

export type TaskWorkspaceRecord = {
  id: string;
  task_id: string;
  status: TaskWorkspaceStatus;
  local_path: string;
  created_at: string;
  updated_at: string;
};

export type TaskWorkspaceListSummary = {
  total_count: number;
  order: string[];
};

export type TaskArtifactStatus = "registered";
export type TaskArtifactIngestionStatus = "pending" | "ingested";

export type TaskArtifactRecord = {
  id: string;
  task_id: string;
  task_workspace_id: string;
  status: TaskArtifactStatus;
  ingestion_status: TaskArtifactIngestionStatus;
  relative_path: string;
  media_type_hint: string | null;
  created_at: string;
  updated_at: string;
};

export type TaskArtifactListSummary = {
  total_count: number;
  order: string[];
};

export type TaskArtifactChunkRecord = {
  id: string;
  task_artifact_id: string;
  sequence_no: number;
  char_start: number;
  char_end_exclusive: number;
  text: string;
  created_at: string;
  updated_at: string;
};

export type TaskArtifactChunkListSummary = {
  total_count: number;
  total_characters: number;
  media_type: string;
  chunking_rule: string;
  order: string[];
};

export type MemoryReviewLabelPayload = {
  user_id: string;
  label: MemoryReviewLabelValue;
  note?: string | null;
};

export type MemoryAdmitPayload = {
  user_id: string;
  memory_key: string;
  value: unknown | null;
  source_event_ids: string[];
  delete_requested?: boolean;
  open_loop?: {
    title: string;
    due_at?: string | null;
  };
};

export type ExplicitCommitmentPattern =
  | "remind_me_to"
  | "i_need_to"
  | "dont_let_me_forget_to"
  | "remember_to";

export type ExplicitCommitmentOpenLoopDecision =
  | "CREATED"
  | "NOOP_ACTIVE_EXISTS"
  | "NOOP_MEMORY_NOT_PERSISTED";

export type ExtractExplicitCommitmentsPayload = {
  user_id: string;
  source_event_id: string;
};

export type ExtractedCommitmentCandidateRecord = {
  memory_key: string;
  value: unknown;
  source_event_ids: string[];
  delete_requested: boolean;
  pattern: ExplicitCommitmentPattern;
  commitment_text: string;
  open_loop_title: string;
};

export type ExplicitCommitmentAdmissionRecord = {
  decision: "NOOP" | "ADD" | "UPDATE" | "DELETE";
  reason: string;
  memory: PersistedMemoryRecord | null;
  revision: PersistedMemoryRevisionRecord | null;
  open_loop: {
    decision: ExplicitCommitmentOpenLoopDecision;
    reason: string;
    open_loop: OpenLoopRecord | null;
  };
};

export type ExplicitCommitmentExtractionResponse = {
  candidates: ExtractedCommitmentCandidateRecord[];
  admissions: ExplicitCommitmentAdmissionRecord[];
  summary: {
    source_event_id: string;
    source_event_kind: string;
    candidate_count: number;
    admission_count: number;
    persisted_change_count: number;
    noop_count: number;
    open_loop_created_count: number;
    open_loop_noop_count: number;
  };
};

export type ContinuityCaptureExplicitSignal =
  | "remember_this"
  | "task"
  | "decision"
  | "commitment"
  | "waiting_for"
  | "blocker"
  | "next_action"
  | "note";

export type ContinuityCaptureAdmissionPosture = "DERIVED" | "TRIAGE";

export type ContinuityObjectType =
  | "Note"
  | "MemoryFact"
  | "Decision"
  | "Commitment"
  | "WaitingFor"
  | "Blocker"
  | "NextAction";

export type ContinuityCaptureEventRecord = {
  id: string;
  raw_content: string;
  explicit_signal: ContinuityCaptureExplicitSignal | null;
  admission_posture: ContinuityCaptureAdmissionPosture;
  admission_reason: string;
  created_at: string;
};

export type ContinuityObjectRecord = {
  id: string;
  capture_event_id: string;
  object_type: ContinuityObjectType;
  status: "active" | "completed" | "cancelled" | "superseded" | "stale";
  title: string;
  body: JsonObject;
  provenance: JsonObject;
  confidence: number;
  created_at: string;
  updated_at: string;
};

export type ContinuityReviewStatusFilter =
  | "correction_ready"
  | "active"
  | "stale"
  | "superseded"
  | "deleted"
  | "all";

export type ContinuityCorrectionAction = "confirm" | "edit" | "delete" | "supersede" | "mark_stale";

export type ContinuityReviewObject = {
  id: string;
  capture_event_id: string;
  object_type: ContinuityObjectType;
  status: string;
  title: string;
  body: JsonObject;
  provenance: JsonObject;
  confidence: number;
  last_confirmed_at: string | null;
  supersedes_object_id: string | null;
  superseded_by_object_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ContinuityCorrectionEvent = {
  id: string;
  continuity_object_id: string;
  action: ContinuityCorrectionAction;
  reason: string | null;
  before_snapshot: JsonObject;
  after_snapshot: JsonObject;
  payload: JsonObject;
  created_at: string;
};

export type ContinuityReviewQueueSummary = {
  status: ContinuityReviewStatusFilter;
  limit: number;
  returned_count: number;
  total_count: number;
  order: string[];
};

export type ContinuityReviewDetail = {
  continuity_object: ContinuityReviewObject;
  correction_events: ContinuityCorrectionEvent[];
  supersession_chain: {
    supersedes: ContinuityReviewObject | null;
    superseded_by: ContinuityReviewObject | null;
  };
};

export type ContinuityCorrectionPayload = {
  user_id: string;
  action: ContinuityCorrectionAction;
  reason?: string | null;
  title?: string | null;
  body?: JsonObject | null;
  provenance?: JsonObject | null;
  confidence?: number | null;
  replacement_title?: string | null;
  replacement_body?: JsonObject | null;
  replacement_provenance?: JsonObject | null;
  replacement_confidence?: number | null;
};

export type ContinuityCaptureInboxItem = {
  capture_event: ContinuityCaptureEventRecord;
  derived_object: ContinuityObjectRecord | null;
};

export type ContinuityCaptureInboxSummary = {
  limit: number;
  returned_count: number;
  total_count: number;
  derived_count: number;
  triage_count: number;
  order: string[];
};

export type ContinuityCaptureCreatePayload = {
  user_id: string;
  raw_content: string;
  explicit_signal?: ContinuityCaptureExplicitSignal | null;
};

export type ContinuityRecallScopeKind = "thread" | "task" | "project" | "person";
export type ContinuityRecallFreshnessPosture = "fresh" | "aging" | "stale" | "superseded" | "unknown";
export type ContinuityRecallProvenancePosture = "strong" | "partial" | "weak" | "missing";
export type ContinuityRecallSupersessionPosture = "current" | "historical" | "superseded" | "deleted";

export type ContinuityRecallScopeMatch = {
  kind: ContinuityRecallScopeKind;
  value: string;
};

export type ContinuityRecallProvenanceReference = {
  source_kind: string;
  source_id: string;
};

export type ContinuityRecallOrdering = {
  scope_match_count: number;
  query_term_match_count: number;
  confirmation_rank: number;
  freshness_posture: ContinuityRecallFreshnessPosture;
  freshness_rank: number;
  provenance_posture: ContinuityRecallProvenancePosture;
  provenance_rank: number;
  supersession_posture: ContinuityRecallSupersessionPosture;
  supersession_rank: number;
  posture_rank: number;
  lifecycle_rank: number;
  confidence: number;
};

export type ContinuityRecallResult = {
  id: string;
  capture_event_id: string;
  object_type: ContinuityObjectType;
  status: string;
  title: string;
  body: JsonObject;
  provenance: JsonObject;
  confirmation_status: MemoryConfirmationStatus;
  admission_posture: ContinuityCaptureAdmissionPosture;
  confidence: number;
  relevance: number;
  last_confirmed_at: string | null;
  supersedes_object_id: string | null;
  superseded_by_object_id: string | null;
  scope_matches: ContinuityRecallScopeMatch[];
  provenance_references: ContinuityRecallProvenanceReference[];
  ordering: ContinuityRecallOrdering;
  created_at: string;
  updated_at: string;
};

export type ContinuityRecallSummary = {
  query: string | null;
  filters: {
    thread_id?: string;
    task_id?: string;
    project?: string;
    person?: string;
    since: string | null;
    until: string | null;
  };
  limit: number;
  returned_count: number;
  total_count: number;
  order: string[];
};

export type RetrievalEvaluationFixtureResult = {
  fixture_id: string;
  title: string;
  query: string;
  top_k: number;
  expected_relevant_ids: string[];
  returned_ids: string[];
  hit_count: number;
  precision_at_k: number;
  top_result_id: string | null;
  top_result_ordering: ContinuityRecallOrdering | null;
};

export type RetrievalEvaluationSummary = {
  fixture_count: number;
  evaluated_fixture_count: number;
  passing_fixture_count: number;
  precision_at_k_mean: number;
  precision_at_1_mean: number;
  precision_target: number;
  status: "pass" | "fail";
  fixture_order: string[];
  result_order: string[];
};

export type ContinuityResumptionEmptyState = {
  is_empty: boolean;
  message: string;
};

export type ContinuityResumptionSingleSection = {
  item: ContinuityRecallResult | null;
  empty_state: ContinuityResumptionEmptyState;
};

export type ContinuityResumptionListSection = {
  items: ContinuityRecallResult[];
  summary: ResumptionBriefSectionSummary;
  empty_state: ContinuityResumptionEmptyState;
};

export type ContinuityResumptionBrief = {
  assembly_version: string;
  scope: ContinuityRecallSummary["filters"];
  last_decision: ContinuityResumptionSingleSection;
  open_loops: ContinuityResumptionListSection;
  recent_changes: ContinuityResumptionListSection;
  next_action: ContinuityResumptionSingleSection;
  sources: string[];
};

export type ContinuityOpenLoopPosture = "waiting_for" | "blocker" | "stale" | "next_action";

export type ContinuityOpenLoopReviewAction = "done" | "deferred" | "still_blocked";

export type ContinuityOpenLoopSectionSummary = {
  limit: number;
  returned_count: number;
  total_count: number;
  order: string[];
};

export type ContinuityOpenLoopSection = {
  items: ContinuityRecallResult[];
  summary: ContinuityOpenLoopSectionSummary;
  empty_state: ContinuityResumptionEmptyState;
};

export type ContinuityOpenLoopDashboard = {
  scope: ContinuityRecallSummary["filters"];
  waiting_for: ContinuityOpenLoopSection;
  blocker: ContinuityOpenLoopSection;
  stale: ContinuityOpenLoopSection;
  next_action: ContinuityOpenLoopSection;
  summary: {
    limit: number;
    total_count: number;
    posture_order: ContinuityOpenLoopPosture[];
    item_order: string[];
  };
  sources: string[];
};

export type ContinuityDailyBrief = {
  assembly_version: string;
  scope: ContinuityRecallSummary["filters"];
  waiting_for_highlights: ContinuityOpenLoopSection;
  blocker_highlights: ContinuityOpenLoopSection;
  stale_items: ContinuityOpenLoopSection;
  next_suggested_action: ContinuityResumptionSingleSection;
  sources: string[];
};

export type ContinuityWeeklyReview = {
  assembly_version: string;
  scope: ContinuityRecallSummary["filters"];
  rollup: {
    total_count: number;
    waiting_for_count: number;
    blocker_count: number;
    stale_count: number;
    correction_recurrence_count: number;
    freshness_drift_count: number;
    next_action_count: number;
    posture_order: ContinuityOpenLoopPosture[];
  };
  waiting_for: ContinuityOpenLoopSection;
  blocker: ContinuityOpenLoopSection;
  stale: ContinuityOpenLoopSection;
  next_action: ContinuityOpenLoopSection;
  sources: string[];
};

export type ContinuityOpenLoopReviewActionPayload = {
  user_id: string;
  action: ContinuityOpenLoopReviewAction;
  note?: string | null;
};

export type ContinuityOpenLoopReviewActionResult = {
  continuity_object: ContinuityReviewObject;
  correction_event: ContinuityCorrectionEvent;
  review_action: ContinuityOpenLoopReviewAction;
  lifecycle_outcome: string;
};

export type OpenLoopCreatePayload = {
  user_id: string;
  memory_id?: string | null;
  title: string;
  due_at?: string | null;
};

export type OpenLoopStatusUpdatePayload = {
  user_id: string;
  status: OpenLoopStatus;
  resolution_note?: string | null;
};

export type ApprovalResolutionResponse = {
  approval: ApprovalItem;
  trace: {
    trace_id: string;
    trace_event_count: number;
  };
};

export type PersistedMemoryRecord = {
  id: string;
  user_id: string;
  memory_key: string;
  value: unknown;
  status: "active" | "deleted";
  source_event_ids: string[];
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
};

export type PersistedMemoryRevisionRecord = {
  id: string;
  user_id: string;
  memory_id: string;
  sequence_no: number;
  action: "NOOP" | "ADD" | "UPDATE" | "DELETE";
  memory_key: string;
  previous_value: unknown | null;
  new_value: unknown | null;
  source_event_ids: string[];
  candidate: JsonObject;
  created_at: string;
};

export type MemoryAdmissionResponse = {
  decision: "NOOP" | "ADD" | "UPDATE" | "DELETE";
  reason: string;
  memory: PersistedMemoryRecord | null;
  revision: PersistedMemoryRevisionRecord | null;
  open_loop?: OpenLoopRecord | null;
};

export class ApiError extends Error {
  status: number;
  code: string;
  detail: unknown;
  retryAfterSeconds: number | null;

  constructor(
    message: string,
    status: number,
    code = "request_failed",
    detail: unknown = null,
    retryAfterSeconds: number | null = null,
    requestSecrets: string[] = [],
  ) {
    const sanitizedDetail = sanitizeApiErrorDetail(detail, requestSecrets);
    super(sanitizePublicErrorText(message, requestSecrets));
    this.name = "ApiError";
    this.status = status;
    const sanitizedCode = redactVNextOperatorAgentApiKey(code, requestSecrets);
    this.code = /^[a-z0-9_.-]{1,80}$/i.test(sanitizedCode) ? sanitizedCode : "request_failed";
    this.detail = sanitizedDetail;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

const PUBLIC_URL_PATTERN = /https?:\/\/\S+/gi;

export function sanitizePublicErrorText(message: string, requestSecrets: string[] = []) {
  return redactVNextOperatorAgentApiKey(message, requestSecrets).replace(PUBLIC_URL_PATTERN, (candidate) => {
    try {
      const parsed = new URL(candidate);
      parsed.username = "";
      parsed.password = "";
      parsed.search = "";
      parsed.hash = "";
      return parsed.toString().replace(/\/$/, parsed.pathname === "/" ? "" : "/");
    } catch {
      return "[redacted URL]";
    }
  });
}

function sanitizeApiErrorDetail(value: unknown, requestSecrets: string[] = []): unknown {
  if (typeof value === "string") {
    return sanitizePublicErrorText(value, requestSecrets);
  }
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeApiErrorDetail(item, requestSecrets));
  }
  if (typeof value === "object" && value !== null) {
    const sanitizedEntries: Array<[string, unknown]> = [];
    const usedKeys = new Set<string>();
    for (const [key, item] of Object.entries(value)) {
      const sanitizedKey = sanitizePublicErrorText(key, requestSecrets);
      let outputKey = sanitizedKey;
      let collisionIndex = 2;
      while (usedKeys.has(outputKey)) {
        outputKey = `${sanitizedKey}#${collisionIndex}`;
        collisionIndex += 1;
      }
      usedKeys.add(outputKey);
      sanitizedEntries.push([outputKey, sanitizeApiErrorDetail(item, requestSecrets)]);
    }
    return Object.fromEntries(sanitizedEntries);
  }
  return value;
}

export function sanitizeApiBaseUrl(apiBaseUrl: string) {
  const normalized = apiBaseUrl.trim();
  if (!normalized) {
    return "";
  }
  try {
    const parsed = new URL(normalized);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
      return "";
    }
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString().replace(/\/$/, parsed.pathname === "/" ? "" : "/");
  } catch {
    return "";
  }
}

function readEnv(publicValue: string | undefined, serverValue: string | undefined) {
  if (typeof window !== "undefined") {
    return publicValue ?? "";
  }

  return publicValue ?? serverValue ?? "";
}

export function getApiConfig(): ApiConfig {
  return {
    apiBaseUrl: sanitizeApiBaseUrl(
      readEnv(
        process.env.NEXT_PUBLIC_ALICEBOT_API_BASE_URL,
        process.env.ALICEBOT_API_BASE_URL,
      ),
    ),
    userId: readEnv(process.env.NEXT_PUBLIC_ALICEBOT_USER_ID, process.env.ALICEBOT_USER_ID),
    defaultThreadId: readEnv(
      process.env.NEXT_PUBLIC_ALICEBOT_THREAD_ID,
      process.env.ALICEBOT_THREAD_ID,
    ),
    defaultToolId: readEnv(process.env.NEXT_PUBLIC_ALICEBOT_TOOL_ID, process.env.ALICEBOT_TOOL_ID),
  };
}

export function isLocalApiBaseUrl(apiBaseUrl: string) {
  const normalized = sanitizeApiBaseUrl(apiBaseUrl);
  if (!normalized) {
    return false;
  }

  try {
    const parsed = new URL(normalized);
    return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(parsed.hostname);
  } catch {
    return false;
  }
}

function currentAliceWebOrigin() {
  const configuredOrigin =
    typeof window === "undefined" ? process.env.PUBLIC_ORIGIN ?? "" : window.location.origin;
  const normalized = configuredOrigin.trim();
  if (!normalized) {
    return "";
  }

  try {
    const parsed = new URL(normalized);
    if (
      parsed.protocol !== "https:" ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      return "";
    }
    return parsed.origin;
  } catch {
    return "";
  }
}

export function isTrustedApiBaseUrl(apiBaseUrl: string) {
  const normalized = sanitizeApiBaseUrl(apiBaseUrl);
  if (!normalized) {
    return false;
  }
  if (isLocalApiBaseUrl(normalized)) {
    return true;
  }

  try {
    const parsed = new URL(normalized);
    return (
      parsed.protocol === "https:" &&
      parsed.pathname === "/" &&
      parsed.origin === currentAliceWebOrigin()
    );
  } catch {
    return false;
  }
}

export function hasLiveApiConfig(config: Pick<ApiConfig, "apiBaseUrl" | "userId">) {
  return Boolean(config.userId.trim() && isTrustedApiBaseUrl(config.apiBaseUrl));
}

export function combinePageModes(...modes: Array<ApiSource | null | undefined>): PageDataMode {
  const presentModes = modes.filter(Boolean) as ApiSource[];
  if (presentModes.length === 0) {
    return "fixture";
  }

  const uniqueModes = Array.from(new Set(presentModes));
  if (uniqueModes.length === 1) {
    return uniqueModes[0];
  }

  return "mixed";
}

export function pageModeLabel(mode: PageDataMode) {
  if (mode === "live") {
    return "Live API";
  }

  if (mode === "mixed") {
    return "Mixed fallback";
  }

  return "Fixture-backed";
}

function compareIsoDatesDesc(left: string, right: string) {
  return new Date(right).getTime() - new Date(left).getTime();
}

function pickLatestApproval(items: ApprovalItem[]) {
  return [...items].sort((left, right) => compareIsoDatesDesc(left.created_at, right.created_at))[0] ?? null;
}

function pickLatestTask(items: TaskItem[], approval: ApprovalItem | null) {
  if (approval) {
    const linkedTask =
      [...items]
        .filter((item) => item.latest_approval_id === approval.id)
        .sort((left, right) => {
          const updatedDelta = compareIsoDatesDesc(left.updated_at, right.updated_at);
          if (updatedDelta !== 0) {
            return updatedDelta;
          }

          return compareIsoDatesDesc(left.created_at, right.created_at);
        })[0] ?? null;

    if (linkedTask) {
      return linkedTask;
    }
  }

  return [...items].sort((left, right) => {
    const updatedDelta = compareIsoDatesDesc(left.updated_at, right.updated_at);
    if (updatedDelta !== 0) {
      return updatedDelta;
    }

    return compareIsoDatesDesc(left.created_at, right.created_at);
  })[0] ?? null;
}

function pickExplicitlyLinkedExecution(
  items: ToolExecutionItem[],
  task: TaskItem | null,
  approval: ApprovalItem | null,
) {
  if (task?.latest_execution_id) {
    return items.find((item) => item.id === task.latest_execution_id) ?? null;
  }

  if (approval) {
    return (
      [...items]
        .filter((item) => item.approval_id === approval.id)
        .sort((left, right) => compareIsoDatesDesc(left.executed_at, right.executed_at))[0] ?? null
    );
  }

  return null;
}

export function buildApiUrl(
  apiBaseUrl: string,
  path: string,
  query?: Record<string, string | undefined>,
) {
  const sanitizedBaseUrl = sanitizeApiBaseUrl(apiBaseUrl);
  if (!sanitizedBaseUrl) {
    throw new ApiError("The configured API base URL is invalid", 0, "invalid_api_base_url");
  }
  const logicalPath = path.replace(/^\/+/, "");
  const url = new URL(logicalPath, `${sanitizedBaseUrl.replace(/\/$/, "")}/`);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value) {
      url.searchParams.set(key, value);
    }
  }
  return url.toString();
}

function shouldAttachVNextOperatorAgentApiKey(apiBaseUrl: string, path: string) {
  if (!vNextOperatorAgentApiKey) {
    return false;
  }

  const logicalPath = `/${path.replace(/^\/+/, "")}`;
  return (
    isTrustedApiBaseUrl(apiBaseUrl) &&
    (logicalPath === "/v0/vnext" || logicalPath.startsWith("/v0/vnext/"))
  );
}

function serializeRequestHeaders(headers: Headers) {
  const serialized: Record<string, string> = {};
  headers.forEach((value, key) => {
    const normalizedKey = key.toLowerCase();
    const outputKey =
      normalizedKey === "content-type"
        ? "Content-Type"
        : normalizedKey === "authorization"
          ? "Authorization"
          : key;
    serialized[outputKey] = value;
  });
  return serialized;
}

function parseRetryAfterSeconds(value: string | null) {
  if (!value) {
    return null;
  }
  const numericSeconds = Number(value);
  if (Number.isFinite(numericSeconds) && numericSeconds >= 0) {
    return Math.ceil(numericSeconds);
  }
  const retryAt = Date.parse(value);
  if (!Number.isFinite(retryAt)) {
    return null;
  }
  return Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
}

function detailMessage(detail: unknown) {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) =>
        typeof item === "object" && item !== null && "msg" in item
          ? String(item.msg)
          : typeof item === "string"
            ? item
            : "",
      )
      .filter(Boolean);
    return messages.length ? messages.join("; ") : null;
  }
  if (typeof detail === "object" && detail !== null) {
    for (const key of ["message", "detail", "error", "reason"]) {
      const value = (detail as Record<string, unknown>)[key];
      if (typeof value === "string" && value.trim()) {
        return value;
      }
    }
  }
  return null;
}

export async function requestJson<T>(
  apiBaseUrl: string,
  path: string,
  init?: ApiRequestInit,
  query?: Record<string, string | undefined>,
): Promise<T> {
  const requestMethod = (init?.method ?? "GET").toUpperCase();
  const defaultTimeoutMs = requestMethod === "GET" ? DEFAULT_READ_TIMEOUT_MS : DEFAULT_MUTATION_TIMEOUT_MS;
  const requestedTimeoutMs = init?.timeoutMs;
  const timeoutMs =
    typeof requestedTimeoutMs === "number" && Number.isFinite(requestedTimeoutMs)
      ? Math.min(MAX_API_TIMEOUT_MS, Math.max(1, Math.trunc(requestedTimeoutMs)))
      : defaultTimeoutMs;
  const fetchInit: ApiRequestInit = { ...(init ?? {}) };
  delete fetchInit.timeoutMs;
  const controller = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const callerSignal = fetchInit.signal;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  try {
    const requestUrl = buildApiUrl(apiBaseUrl, path, query);
    const requestHeaders = new Headers(fetchInit.headers);
    if (!requestHeaders.has("Content-Type")) {
      requestHeaders.set("Content-Type", "application/json");
    }
    let autoAttachedAgentApiKey = "";
    if (
      shouldAttachVNextOperatorAgentApiKey(apiBaseUrl, path) &&
      !requestHeaders.has("Authorization")
    ) {
      autoAttachedAgentApiKey = vNextOperatorAgentApiKey;
      requestHeaders.set("Authorization", `Bearer ${autoAttachedAgentApiKey}`);
    }
    const authorizationHeader = requestHeaders.get("Authorization")?.trim() ?? "";
    const bearerMatch = /^Bearer\s+(.+)$/i.exec(authorizationHeader);
    const requestSecrets = bearerMatch?.[1] ? [bearerMatch[1]] : [];

    const response = await fetch(requestUrl, {
      cache: "no-store",
      ...fetchInit,
      signal: controller.signal,
      headers: serializeRequestHeaders(requestHeaders),
    });

    const payload = (await response.json().catch(() => null)) as
      | { detail?: unknown; message?: unknown; code?: unknown }
      | null;
    if (!response.ok) {
      const detail = sanitizeApiErrorDetail(
        payload?.detail ?? payload?.message ?? null,
        requestSecrets,
      );
      const message = detailMessage(detail) ?? `Request failed with status ${response.status}`;
      const nestedCode =
        typeof detail === "object" && detail !== null && !Array.isArray(detail)
          ? (detail as Record<string, unknown>).code
          : null;
      const codeCandidate = payload?.code ?? nestedCode;
      const code = typeof codeCandidate === "string" ? codeCandidate : "request_failed";
      throw new ApiError(
        message,
        response.status,
        code,
        detail,
        parseRetryAfterSeconds(response.headers.get("Retry-After")),
        requestSecrets,
      );
    }

    return payload as T;
  } catch (error) {
    if (timedOut) {
      throw new ApiError(
        `Request timed out after ${timeoutMs / 1000} seconds`,
        0,
        "request_timeout",
      );
    }
    if (controller.signal.aborted && !(error instanceof ApiError)) {
      throw new ApiError("Request was cancelled", 0, "request_cancelled");
    }
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError("Unable to reach the configured API", 0, "transport_error");
  } finally {
    clearTimeout(timeoutId);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

export function getThreadHealthDashboard(apiBaseUrl: string, userId: string) {
  return requestJson<{ dashboard: ThreadHealthDashboardSummary }>(
    apiBaseUrl,
    "/v0/threads/health-dashboard",
    undefined,
    { user_id: userId },
  );
}

export type WhatChangedSource = {
  source_id: string;
  title: string;
  date: string;
};

export type WhatChangedStep = {
  id: string;
  kind: string;
  chain: string;
  date: string;
  change: string;
  reason: { text: string; source_id: string | null } | null;
  replaced: string | null;
  disagrees_with: string | null;
  sources: WhatChangedSource[];
  author: string;
  review_state: string;
};

export type WhatChangedAnswer = {
  notice: string;
  topic: string;
  chains: {
    start: string;
    current: WhatChangedStep | null;
    upcoming: string[];
    disputed: string[];
    history: WhatChangedStep[];
  }[];
  message?: string;
  search?: string;
};

export function postWhatChanged(apiBaseUrl: string, userId: string, topic: string) {
  return requestJson<WhatChangedAnswer>(
    apiBaseUrl,
    "/v0/vnext/changes",
    {
      method: "POST",
      body: JSON.stringify({ topic }),
    },
    { user_id: userId },
  );
}

export function queryContinuityRecall(
  apiBaseUrl: string,
  userId: string,
  options?: {
    query?: string;
    threadId?: string;
    taskId?: string;
    project?: string;
    person?: string;
    since?: string;
    until?: string;
    limit?: number;
  },
) {
  const query = options?.query?.trim();
  const threadId = options?.threadId?.trim();
  const taskId = options?.taskId?.trim();
  const project = options?.project?.trim();
  const person = options?.person?.trim();
  const since = options?.since?.trim();
  const until = options?.until?.trim();
  const limit =
    typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit > 0
      ? String(Math.trunc(options.limit))
      : undefined;

  return requestJson<{ items: ContinuityRecallResult[]; summary: ContinuityRecallSummary }>(
    apiBaseUrl,
    "/v0/continuity/recall",
    undefined,
    {
      user_id: userId,
      query: query || undefined,
      thread_id: threadId || undefined,
      task_id: taskId || undefined,
      project: project || undefined,
      person: person || undefined,
      since: since || undefined,
      until: until || undefined,
      limit,
    },
  );
}

export function getContinuityRetrievalEvaluation(apiBaseUrl: string, userId: string) {
  return requestJson<{ fixtures: RetrievalEvaluationFixtureResult[]; summary: RetrievalEvaluationSummary }>(
    apiBaseUrl,
    "/v0/continuity/retrieval-evaluation",
    undefined,
    {
      user_id: userId,
    },
  );
}

export function getContinuityResumptionBrief(
  apiBaseUrl: string,
  userId: string,
  options?: {
    query?: string;
    threadId?: string;
    taskId?: string;
    project?: string;
    person?: string;
    since?: string;
    until?: string;
    maxRecentChanges?: number;
    maxOpenLoops?: number;
  },
) {
  const query = options?.query?.trim();
  const threadId = options?.threadId?.trim();
  const taskId = options?.taskId?.trim();
  const project = options?.project?.trim();
  const person = options?.person?.trim();
  const since = options?.since?.trim();
  const until = options?.until?.trim();
  const maxRecentChanges =
    typeof options?.maxRecentChanges === "number" &&
    Number.isFinite(options.maxRecentChanges) &&
    options.maxRecentChanges >= 0
      ? String(Math.trunc(options.maxRecentChanges))
      : undefined;
  const maxOpenLoops =
    typeof options?.maxOpenLoops === "number" &&
    Number.isFinite(options.maxOpenLoops) &&
    options.maxOpenLoops >= 0
      ? String(Math.trunc(options.maxOpenLoops))
      : undefined;

  return requestJson<{ brief: ContinuityResumptionBrief }>(
    apiBaseUrl,
    "/v0/continuity/resumption-brief",
    undefined,
    {
      user_id: userId,
      query: query || undefined,
      thread_id: threadId || undefined,
      task_id: taskId || undefined,
      project: project || undefined,
      person: person || undefined,
      since: since || undefined,
      until: until || undefined,
      max_recent_changes: maxRecentChanges,
      max_open_loops: maxOpenLoops,
    },
  );
}

export function getContinuityOpenLoopDashboard(
  apiBaseUrl: string,
  userId: string,
  options?: {
    query?: string;
    threadId?: string;
    taskId?: string;
    project?: string;
    person?: string;
    since?: string;
    until?: string;
    limit?: number;
  },
) {
  const query = options?.query?.trim();
  const threadId = options?.threadId?.trim();
  const taskId = options?.taskId?.trim();
  const project = options?.project?.trim();
  const person = options?.person?.trim();
  const since = options?.since?.trim();
  const until = options?.until?.trim();
  const limit =
    typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit >= 0
      ? String(Math.trunc(options.limit))
      : undefined;

  return requestJson<{ dashboard: ContinuityOpenLoopDashboard }>(
    apiBaseUrl,
    "/v0/continuity/open-loops",
    undefined,
    {
      user_id: userId,
      query: query || undefined,
      thread_id: threadId || undefined,
      task_id: taskId || undefined,
      project: project || undefined,
      person: person || undefined,
      since: since || undefined,
      until: until || undefined,
      limit,
    },
  );
}

export function getContinuityDailyBrief(
  apiBaseUrl: string,
  userId: string,
  options?: {
    query?: string;
    threadId?: string;
    taskId?: string;
    project?: string;
    person?: string;
    since?: string;
    until?: string;
    limit?: number;
  },
) {
  const query = options?.query?.trim();
  const threadId = options?.threadId?.trim();
  const taskId = options?.taskId?.trim();
  const project = options?.project?.trim();
  const person = options?.person?.trim();
  const since = options?.since?.trim();
  const until = options?.until?.trim();
  const limit =
    typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit >= 0
      ? String(Math.trunc(options.limit))
      : undefined;

  return requestJson<{ brief: ContinuityDailyBrief }>(
    apiBaseUrl,
    "/v0/continuity/daily-brief",
    undefined,
    {
      user_id: userId,
      query: query || undefined,
      thread_id: threadId || undefined,
      task_id: taskId || undefined,
      project: project || undefined,
      person: person || undefined,
      since: since || undefined,
      until: until || undefined,
      limit,
    },
  );
}

export function getContinuityWeeklyReview(
  apiBaseUrl: string,
  userId: string,
  options?: {
    query?: string;
    threadId?: string;
    taskId?: string;
    project?: string;
    person?: string;
    since?: string;
    until?: string;
    limit?: number;
  },
) {
  const query = options?.query?.trim();
  const threadId = options?.threadId?.trim();
  const taskId = options?.taskId?.trim();
  const project = options?.project?.trim();
  const person = options?.person?.trim();
  const since = options?.since?.trim();
  const until = options?.until?.trim();
  const limit =
    typeof options?.limit === "number" && Number.isFinite(options.limit) && options.limit >= 0
      ? String(Math.trunc(options.limit))
      : undefined;

  return requestJson<{ review: ContinuityWeeklyReview }>(
    apiBaseUrl,
    "/v0/continuity/weekly-review",
    undefined,
    {
      user_id: userId,
      query: query || undefined,
      thread_id: threadId || undefined,
      task_id: taskId || undefined,
      project: project || undefined,
      person: person || undefined,
      since: since || undefined,
      until: until || undefined,
      limit,
    },
  );
}

export function applyContinuityOpenLoopReviewAction(
  apiBaseUrl: string,
  continuityObjectId: string,
  payload: ContinuityOpenLoopReviewActionPayload,
) {
  return requestJson<ContinuityOpenLoopReviewActionResult>(
    apiBaseUrl,
    `/v0/continuity/open-loops/${continuityObjectId}/review-action`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function listApprovals(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: ApprovalItem[]; summary: { total_count: number; order: string[] } }>(
    apiBaseUrl,
    "/v0/approvals",
    undefined,
    { user_id: userId },
  );
}

export function getApprovalDetail(apiBaseUrl: string, approvalId: string, userId: string) {
  return requestJson<{ approval: ApprovalItem }>(
    apiBaseUrl,
    `/v0/approvals/${approvalId}`,
    undefined,
    { user_id: userId },
  );
}

export function resolveApproval(
  apiBaseUrl: string,
  approvalId: string,
  action: "approve" | "reject",
  userId: string,
) {
  return requestJson<ApprovalResolutionResponse>(apiBaseUrl, `/v0/approvals/${approvalId}/${action}`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId }),
  });
}

export function listTasks(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: TaskItem[]; summary: { total_count: number; order: string[] } }>(
    apiBaseUrl,
    "/v0/tasks",
    undefined,
    { user_id: userId },
  );
}

export function getTaskDetail(apiBaseUrl: string, taskId: string, userId: string) {
  return requestJson<{ task: TaskItem }>(
    apiBaseUrl,
    `/v0/tasks/${taskId}`,
    undefined,
    { user_id: userId },
  );
}

export function getTaskSteps(apiBaseUrl: string, taskId: string, userId: string) {
  return requestJson<{ items: TaskStepItem[]; summary: TaskStepListSummary }>(
    apiBaseUrl,
    `/v0/tasks/${taskId}/steps`,
    undefined,
    { user_id: userId },
  );
}

export function listTaskRuns(apiBaseUrl: string, taskId: string, userId: string) {
  return requestJson<{ items: TaskRunItem[]; summary: TaskRunListSummary }>(
    apiBaseUrl,
    `/v0/tasks/${taskId}/runs`,
    undefined,
    { user_id: userId },
  );
}

export function executeApproval(
  apiBaseUrl: string,
  approvalId: string,
  userId: string,
  taskRunId?: string | null,
) {
  const payload: { user_id: string; task_run_id?: string } = {
    user_id: userId,
  };
  if (taskRunId !== null && taskRunId !== undefined) {
    payload.task_run_id = taskRunId;
  }
  return requestJson<ApprovalExecutionResponse>(apiBaseUrl, `/v0/approvals/${approvalId}/execute`, {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function admitMemory(apiBaseUrl: string, payload: MemoryAdmitPayload) {
  return requestJson<MemoryAdmissionResponse>(apiBaseUrl, "/v0/memories/admit", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function extractExplicitCommitments(
  apiBaseUrl: string,
  payload: ExtractExplicitCommitmentsPayload,
) {
  return requestJson<ExplicitCommitmentExtractionResponse>(
    apiBaseUrl,
    "/v0/open-loops/extract-explicit-commitments",
    {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
    },
  );
}

export function createContinuityCapture(
  apiBaseUrl: string,
  payload: ContinuityCaptureCreatePayload,
) {
  return requestJson<{ capture: ContinuityCaptureInboxItem }>(
    apiBaseUrl,
    "/v0/continuity/captures",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function listContinuityCaptures(
  apiBaseUrl: string,
  userId: string,
  options?: {
    limit?: number;
  },
) {
  return requestJson<{ items: ContinuityCaptureInboxItem[]; summary: ContinuityCaptureInboxSummary }>(
    apiBaseUrl,
    "/v0/continuity/captures",
    undefined,
    {
      user_id: userId,
      limit: options?.limit ? String(options.limit) : undefined,
    },
  );
}

export function getContinuityCaptureDetail(
  apiBaseUrl: string,
  captureEventId: string,
  userId: string,
) {
  return requestJson<{ capture: ContinuityCaptureInboxItem }>(
    apiBaseUrl,
    `/v0/continuity/captures/${captureEventId}`,
    undefined,
    {
      user_id: userId,
    },
  );
}

export function listContinuityReviewQueue(
  apiBaseUrl: string,
  userId: string,
  options?: {
    status?: ContinuityReviewStatusFilter;
    limit?: number;
  },
) {
  return requestJson<{ items: ContinuityReviewObject[]; summary: ContinuityReviewQueueSummary }>(
    apiBaseUrl,
    "/v0/continuity/review-queue",
    undefined,
    {
      user_id: userId,
      status: options?.status ?? "correction_ready",
      limit: options?.limit ? String(options.limit) : undefined,
    },
  );
}

export function getContinuityReviewDetail(
  apiBaseUrl: string,
  continuityObjectId: string,
  userId: string,
) {
  return requestJson<{ review: ContinuityReviewDetail }>(
    apiBaseUrl,
    `/v0/continuity/review-queue/${continuityObjectId}`,
    undefined,
    {
      user_id: userId,
    },
  );
}

export function applyContinuityCorrection(
  apiBaseUrl: string,
  continuityObjectId: string,
  payload: ContinuityCorrectionPayload,
) {
  return requestJson<{
    continuity_object: ContinuityReviewObject;
    correction_event: ContinuityCorrectionEvent;
    replacement_object: ContinuityReviewObject | null;
  }>(
    apiBaseUrl,
    `/v0/continuity/review-queue/${continuityObjectId}/corrections`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function listOpenLoops(
  apiBaseUrl: string,
  userId: string,
  options?: {
    status?: OpenLoopStatusFilter;
    limit?: number;
  },
) {
  return requestJson<{ items: OpenLoopRecord[]; summary: OpenLoopListSummary }>(
    apiBaseUrl,
    "/v0/open-loops",
    undefined,
    {
      user_id: userId,
      status: options?.status,
      limit: options?.limit ? String(options.limit) : undefined,
    },
  );
}

export function getOpenLoopDetail(apiBaseUrl: string, openLoopId: string, userId: string) {
  return requestJson<{ open_loop: OpenLoopRecord }>(
    apiBaseUrl,
    `/v0/open-loops/${openLoopId}`,
    undefined,
    { user_id: userId },
  );
}

export function createOpenLoop(apiBaseUrl: string, payload: OpenLoopCreatePayload) {
  return requestJson<{ open_loop: OpenLoopRecord }>(apiBaseUrl, "/v0/open-loops", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateOpenLoopStatus(
  apiBaseUrl: string,
  openLoopId: string,
  payload: OpenLoopStatusUpdatePayload,
) {
  return requestJson<{ open_loop: OpenLoopRecord }>(
    apiBaseUrl,
    `/v0/open-loops/${openLoopId}/status`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function listToolExecutions(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: ToolExecutionItem[]; summary: { total_count: number; order: string[] } }>(
    apiBaseUrl,
    "/v0/tool-executions",
    undefined,
    { user_id: userId },
  );
}

export function getToolExecution(apiBaseUrl: string, executionId: string, userId: string) {
  return requestJson<{ execution: ToolExecutionItem }>(
    apiBaseUrl,
    `/v0/tool-executions/${executionId}`,
    undefined,
    { user_id: userId },
  );
}

export function listTraces(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: TraceReviewSummaryItem[]; summary: TraceReviewListSummary }>(
    apiBaseUrl,
    "/v0/traces",
    undefined,
    { user_id: userId },
  );
}

export function getTraceDetail(apiBaseUrl: string, traceId: string, userId: string) {
  return requestJson<{ trace: TraceReviewItem }>(
    apiBaseUrl,
    `/v0/traces/${traceId}`,
    undefined,
    { user_id: userId },
  );
}

export function getTraceEvents(apiBaseUrl: string, traceId: string, userId: string) {
  return requestJson<{ items: TraceReviewEventItem[]; summary: TraceReviewEventListSummary }>(
    apiBaseUrl,
    `/v0/traces/${traceId}/events`,
    undefined,
    { user_id: userId },
  );
}

export function connectGmailAccount(
  apiBaseUrl: string,
  payload: GmailAccountConnectPayload,
) {
  return requestJson<{ account: GmailAccountRecord }>(apiBaseUrl, "/v0/gmail-accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listGmailAccounts(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: GmailAccountRecord[]; summary: GmailAccountListSummary }>(
    apiBaseUrl,
    "/v0/gmail-accounts",
    undefined,
    { user_id: userId },
  );
}

export function getGmailAccountDetail(apiBaseUrl: string, gmailAccountId: string, userId: string) {
  return requestJson<{ account: GmailAccountRecord }>(
    apiBaseUrl,
    `/v0/gmail-accounts/${gmailAccountId}`,
    undefined,
    { user_id: userId },
  );
}

export function ingestGmailMessage(
  apiBaseUrl: string,
  gmailAccountId: string,
  providerMessageId: string,
  payload: GmailMessageIngestPayload,
) {
  return requestJson<GmailMessageIngestionResponse>(
    apiBaseUrl,
    `/v0/gmail-accounts/${gmailAccountId}/messages/${providerMessageId}/ingest`,
    {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
    },
  );
}

export function connectCalendarAccount(
  apiBaseUrl: string,
  payload: CalendarAccountConnectPayload,
) {
  return requestJson<{ account: CalendarAccountRecord }>(apiBaseUrl, "/v0/calendar-accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listCalendarAccounts(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: CalendarAccountRecord[]; summary: CalendarAccountListSummary }>(
    apiBaseUrl,
    "/v0/calendar-accounts",
    undefined,
    { user_id: userId },
  );
}

export function getCalendarAccountDetail(
  apiBaseUrl: string,
  calendarAccountId: string,
  userId: string,
) {
  return requestJson<{ account: CalendarAccountRecord }>(
    apiBaseUrl,
    `/v0/calendar-accounts/${calendarAccountId}`,
    undefined,
    { user_id: userId },
  );
}

export function listCalendarEvents(
  apiBaseUrl: string,
  calendarAccountId: string,
  userId: string,
  query?: CalendarEventListQuery,
) {
  const limitValue =
    typeof query?.limit === "number" && Number.isFinite(query.limit) && query.limit > 0
      ? String(Math.trunc(query.limit))
      : undefined;
  const timeMin = query?.timeMin?.trim() ? query.timeMin.trim() : undefined;
  const timeMax = query?.timeMax?.trim() ? query.timeMax.trim() : undefined;

  return requestJson<CalendarEventListResponse>(
    apiBaseUrl,
    `/v0/calendar-accounts/${calendarAccountId}/events`,
    undefined,
    {
      user_id: userId,
      limit: limitValue,
      time_min: timeMin,
      time_max: timeMax,
    },
  );
}

export function ingestCalendarEvent(
  apiBaseUrl: string,
  calendarAccountId: string,
  providerEventId: string,
  payload: CalendarEventIngestPayload,
) {
  return requestJson<CalendarEventIngestionResponse>(
    apiBaseUrl,
    `/v0/calendar-accounts/${calendarAccountId}/events/${providerEventId}/ingest`,
    {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
    },
  );
}

export function listTaskWorkspaces(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: TaskWorkspaceRecord[]; summary: TaskWorkspaceListSummary }>(
    apiBaseUrl,
    "/v0/task-workspaces",
    undefined,
    { user_id: userId },
  );
}

export function listTaskArtifacts(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: TaskArtifactRecord[]; summary: TaskArtifactListSummary }>(
    apiBaseUrl,
    "/v0/task-artifacts",
    undefined,
    { user_id: userId },
  );
}

export function getTaskArtifactDetail(apiBaseUrl: string, taskArtifactId: string, userId: string) {
  return requestJson<{ artifact: TaskArtifactRecord }>(
    apiBaseUrl,
    `/v0/task-artifacts/${taskArtifactId}`,
    undefined,
    { user_id: userId },
  );
}

export function listTaskArtifactChunks(apiBaseUrl: string, taskArtifactId: string, userId: string) {
  return requestJson<{ items: TaskArtifactChunkRecord[]; summary: TaskArtifactChunkListSummary }>(
    apiBaseUrl,
    `/v0/task-artifacts/${taskArtifactId}/chunks`,
    undefined,
    { user_id: userId },
  );
}

export function listEntities(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: EntityRecord[]; summary: EntityListSummary }>(
    apiBaseUrl,
    "/v0/entities",
    undefined,
    { user_id: userId },
  );
}

export function getEntityDetail(apiBaseUrl: string, entityId: string, userId: string) {
  return requestJson<{ entity: EntityRecord }>(
    apiBaseUrl,
    `/v0/entities/${entityId}`,
    undefined,
    { user_id: userId },
  );
}

export function listEntityEdges(apiBaseUrl: string, entityId: string, userId: string) {
  return requestJson<{ items: EntityEdgeRecord[]; summary: EntityEdgeListSummary }>(
    apiBaseUrl,
    `/v0/entities/${entityId}/edges`,
    undefined,
    { user_id: userId },
  );
}

export function listMemories(
  apiBaseUrl: string,
  userId: string,
  options?: {
    status?: MemoryReviewStatusFilter;
    limit?: number;
  },
) {
  return requestJson<{ items: MemoryReviewRecord[]; summary: MemoryReviewListSummary }>(
    apiBaseUrl,
    "/v0/memories",
    undefined,
    {
      user_id: userId,
      status: options?.status,
      limit: options?.limit ? String(options.limit) : undefined,
    },
  );
}

export function listMemoryReviewQueue(
  apiBaseUrl: string,
  userId: string,
  options?: number | { limit?: number; priorityMode?: MemoryReviewQueuePriorityMode },
) {
  const limit = typeof options === "number" ? options : options?.limit;
  const priorityMode = typeof options === "number" ? undefined : options?.priorityMode;
  return requestJson<{ items: MemoryReviewQueueItem[]; summary: MemoryReviewQueueSummary }>(
    apiBaseUrl,
    "/v0/memories/review-queue",
    undefined,
    {
      user_id: userId,
      limit: limit ? String(limit) : undefined,
      priority_mode: priorityMode,
    },
  );
}

export async function getMemoryEvaluationSummary(apiBaseUrl: string, userId: string) {
  const [evaluationPayload, qualityGatePayload] = await Promise.all([
    requestJson<{ summary: Omit<MemoryEvaluationSummary, "quality_gate"> }>(
      apiBaseUrl,
      "/v0/memories/evaluation-summary",
      undefined,
      { user_id: userId },
    ),
    requestJson<{ summary: MemoryQualityGateSummary }>(
      apiBaseUrl,
      "/v0/memories/quality-gate",
      undefined,
      { user_id: userId },
    ),
  ]);

  return {
    summary: {
      ...evaluationPayload.summary,
      quality_gate: qualityGatePayload.summary,
    },
  };
}

export function getMemoryTrustDashboard(apiBaseUrl: string, userId: string) {
  return requestJson<{ dashboard: MemoryTrustDashboardSummary }>(
    apiBaseUrl,
    "/v0/memories/trust-dashboard",
    undefined,
    { user_id: userId },
  );
}

export function getMemoryHygieneDashboard(apiBaseUrl: string, userId: string) {
  return requestJson<{ dashboard: MemoryHygieneDashboardSummary }>(
    apiBaseUrl,
    "/v0/memories/hygiene-dashboard",
    undefined,
    { user_id: userId },
  );
}

export function getMemoryDetail(apiBaseUrl: string, memoryId: string, userId: string) {
  return requestJson<{ memory: MemoryReviewRecord }>(
    apiBaseUrl,
    `/v0/memories/${memoryId}`,
    undefined,
    { user_id: userId },
  );
}

export function getMemoryRevisions(
  apiBaseUrl: string,
  memoryId: string,
  userId: string,
  limit?: number,
) {
  return requestJson<{ items: MemoryRevisionReviewRecord[]; summary: MemoryRevisionReviewListSummary }>(
    apiBaseUrl,
    `/v0/memories/${memoryId}/revisions`,
    undefined,
    {
      user_id: userId,
      limit: limit ? String(limit) : undefined,
    },
  );
}

export function listMemoryLabels(apiBaseUrl: string, memoryId: string, userId: string) {
  return requestJson<{ items: MemoryReviewLabelRecord[]; summary: MemoryReviewLabelSummary }>(
    apiBaseUrl,
    `/v0/memories/${memoryId}/labels`,
    undefined,
    { user_id: userId },
  );
}

export function submitMemoryLabel(
  apiBaseUrl: string,
  memoryId: string,
  payload: MemoryReviewLabelPayload,
) {
  return requestJson<{ label: MemoryReviewLabelRecord; summary: MemoryReviewLabelSummary }>(
    apiBaseUrl,
    `/v0/memories/${memoryId}/labels`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getVNextWorkspace(apiBaseUrl: string, userId: string) {
  return requestJson<VNextWorkspacePayload>(
    apiBaseUrl,
    "/v0/vnext/workspace",
    undefined,
    { user_id: userId },
  );
}

export function createVNextSource(apiBaseUrl: string, payload: VNextSourceCreatePayload) {
  return requestJson<{
    status: string;
    source_id: string | null;
    content_hash: string;
    chunk_count: number;
    candidate_memory_count: number;
    duplicate: boolean;
    errors: string[];
  }>(apiBaseUrl, "/v0/vnext/sources", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function reviewVNextSource(
  apiBaseUrl: string,
  sourceId: string,
  payload: VNextSourceReviewPayload,
) {
  return requestJson<{ source: VNextSourceRecord; archived: boolean; trace: VNextSourceTracePayload }>(
    apiBaseUrl,
    `/v0/vnext/sources/${sourceId}/review`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getVNextSourceTrace(apiBaseUrl: string, sourceId: string, userId: string) {
  return requestJson<VNextSourceTracePayload>(
    apiBaseUrl,
    `/v0/vnext/traces/sources/${sourceId}`,
    undefined,
    { user_id: userId },
  );
}

export function getVNextArtifactTrace(apiBaseUrl: string, artifactId: string, userId: string) {
  return requestJson<VNextArtifactTracePayload>(
    apiBaseUrl,
    `/v0/vnext/traces/artifacts/${artifactId}`,
    undefined,
    { user_id: userId },
  );
}

export function createVNextContextPack(
  apiBaseUrl: string,
  payload: {
    user_id: string;
    query: string;
    scope?: JsonObject;
    options?: JsonObject;
  },
) {
  return requestJson<VNextContextPack>(apiBaseUrl, "/v0/vnext/context-packs", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function generateVNextDailyBrief(
  apiBaseUrl: string,
  payload: { user_id: string; scope?: JsonObject; options?: JsonObject },
) {
  return requestJson<VNextArtifactRecord>(apiBaseUrl, "/v0/vnext/artifacts/generate/daily-brief", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function generateVNextWeeklySynthesis(
  apiBaseUrl: string,
  payload: { user_id: string; scope?: JsonObject; options?: JsonObject },
) {
  return requestJson<VNextArtifactRecord>(
    apiBaseUrl,
    "/v0/vnext/artifacts/generate/weekly-synthesis",
    {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
    },
  );
}

export function reviewVNextArtifact(
  apiBaseUrl: string,
  artifactId: string,
  payload: { user_id: string; action: "review" | "accept" | "reject" | "promote" | "archive" },
) {
  return requestJson<VNextArtifactRecord>(apiBaseUrl, `/v0/vnext/artifacts/${artifactId}/review`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function rateVNextArtifactQuality(
  apiBaseUrl: string,
  artifactId: string,
  payload: {
    user_id: string;
    reviewer_id?: string | null;
    usefulness?: number | null;
    accuracy?: number | null;
    source_grounding?: number | null;
    novel_connections?: number | null;
    actionability?: number | null;
    hallucination_risk?: number | null;
    verbosity?: string;
    missed_context?: string | null;
    comments?: string | null;
  },
) {
  return requestJson<VNextArtifactQualityEvalRecord>(
    apiBaseUrl,
    `/v0/vnext/artifacts/${artifactId}/quality-ratings`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getVNextQualityEvals(
  apiBaseUrl: string,
  userId: string,
  options: { artifactId?: string; limit?: number } = {},
) {
  const query: Record<string, string> = {
    user_id: userId,
    limit: String(options.limit ?? 100),
  };
  if (options.artifactId) {
    query.artifact_id = options.artifactId;
  }
  return requestJson<{ items: VNextArtifactQualityEvalRecord[]; count: number }>(
    apiBaseUrl,
    "/v0/vnext/quality-evals",
    undefined,
    query,
  );
}

export function captureVNextBrowserClip(
  apiBaseUrl: string,
  payload: {
    user_id: string;
    url: string;
    title?: string | null;
    selected_text?: string | null;
    page_text?: string | null;
    user_note?: string | null;
    capture_token?: string | null;
    capture_capability?: string | null;
    domain?: string;
    sensitivity?: string;
  },
) {
  return requestJson<JsonObject>(apiBaseUrl, "/v0/vnext/connectors/browser-clipper/capture", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function issueVNextBrowserClipCapability(
  apiBaseUrl: string,
  payload: { user_id: string; origin: string },
) {
  return requestJson<VNextBrowserClipCapability>(
    apiBaseUrl,
    "/v0/vnext/connectors/browser-clipper/capabilities",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function updateVNextConnectorConfig(
  apiBaseUrl: string,
  connectorName: string,
  payload: {
    user_id: string;
    enabled?: boolean;
    default_domain?: string;
    default_sensitivity?: string;
    secret_ref?: string | null;
    sync_mode?: string;
    poll_interval_seconds?: number | null;
    config_json?: JsonObject;
  },
) {
  if (connectorName === "telegram") {
    throw new TypeError(
      "Telegram configuration is on-demand and must use updateVNextTelegramConnectorConfig",
    );
  }
  return requestJson<VNextConnectorConfigRecord>(apiBaseUrl, `/v0/vnext/connectors/${connectorName}/config`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function updateVNextTelegramConnectorConfig(
  apiBaseUrl: string,
  payload: {
    user_id: string;
    enabled?: boolean;
    default_domain?: string;
    default_sensitivity?: string;
    config_json: {
      allowed_chat_ids: string[];
    };
  },
) {
  if (!payload.config_json.allowed_chat_ids.some((chatId) => chatId.trim())) {
    throw new TypeError("Telegram configuration requires an explicit chat allowlist");
  }
  return requestJson<VNextConnectorConfigRecord>(apiBaseUrl, "/v0/vnext/connectors/telegram/config", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function syncVNextTelegramConnector(
  apiBaseUrl: string,
  payload: {
    user_id: string;
    updates: [JsonObject, ...JsonObject[]];
    allowed_chat_ids: [string, ...string[]];
    default_domain?: string;
    default_sensitivity?: string;
  },
) {
  if (payload.updates.length === 0 || !payload.allowed_chat_ids.some((chatId) => chatId.trim())) {
    throw new TypeError("Telegram ingestion requires supplied updates and an explicit chat allowlist");
  }
  return requestJson<JsonObject>(apiBaseUrl, "/v0/vnext/connectors/telegram/sync", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function syncVNextLocalFolderConnector(
  apiBaseUrl: string,
  payload: {
    user_id: string;
    paths?: string[];
    recursive?: boolean;
    extensions?: string[];
    ignore_patterns?: string[];
    default_domain?: string;
    default_sensitivity?: string;
  },
) {
  return requestJson<JsonObject>(apiBaseUrl, "/v0/vnext/connectors/local-folder/sync", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function getVNextConnectorStatus(apiBaseUrl: string, userId: string, connectorName: string) {
  return requestJson<VNextConnectorStatusPayload>(
    apiBaseUrl,
    `/v0/vnext/connectors/${connectorName}/status`,
    undefined,
    { user_id: userId },
  );
}

export function getVNextConnectorsHealth(apiBaseUrl: string, userId: string) {
  return requestJson<{ items: VNextConnectorHealthRecord[]; count: number; order: string[] }>(
    apiBaseUrl,
    "/v0/vnext/connectors/health",
    undefined,
    { user_id: userId },
  );
}

export function getVNextDogfoodingDashboard(apiBaseUrl: string, userId: string) {
  return requestJson<VNextDogfoodingDashboard>(
    apiBaseUrl,
    "/v0/vnext/dogfooding",
    undefined,
    { user_id: userId },
  );
}

export function runVNextDoctor(
  apiBaseUrl: string,
  payload: { user_id: string; fix_safe?: boolean; ci?: boolean },
) {
  return requestJson<VNextDoctorPayload>(apiBaseUrl, "/v0/vnext/doctor/run", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function recordVNextArtifactInsightFeedback(
  apiBaseUrl: string,
  artifactId: string,
  payload: {
    user_id: string;
    useful_insight: "yes" | "no" | "not_sure";
    surfaced_missed?: "yes" | "no" | "not_sure" | null;
    comments?: string | null;
  },
) {
  return requestJson<VNextEventRecord>(apiBaseUrl, `/v0/vnext/artifacts/${artifactId}/insight-feedback`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function reviewVNextMemory(
  apiBaseUrl: string,
  memoryId: string,
  payload: VNextMemoryReviewPayload,
) {
  return requestJson<{ memory: VNextMemoryRecord }>(
    apiBaseUrl,
    `/v0/vnext/memories/${memoryId}/review`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function confirmVNextMemory(
  apiBaseUrl: string,
  payload: VNextMemoryConfirmationPayload,
) {
  return requestJson<VNextMemoryConfirmationResponse>(
    apiBaseUrl,
    "/v0/vnext/memories/confirm",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function createVNextProject(apiBaseUrl: string, payload: VNextProjectCreatePayload) {
  return requestJson<{ project: VNextProjectRecord }>(apiBaseUrl, "/v0/vnext/projects", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function generateVNextProjectUpdate(
  apiBaseUrl: string,
  payload: { user_id: string; scope?: JsonObject; options?: JsonObject },
) {
  return requestJson<VNextArtifactRecord>(apiBaseUrl, "/v0/vnext/projects/update-candidates", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function createVNextOpenLoop(apiBaseUrl: string, payload: VNextOpenLoopCreatePayload) {
  return requestJson<{ open_loop: VNextOpenLoopRecord }>(apiBaseUrl, "/v0/vnext/open-loops", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function reviewVNextOpenLoop(
  apiBaseUrl: string,
  loopId: string,
  payload: VNextOpenLoopReviewPayload,
) {
  return requestJson<VNextOpenLoopRecord>(apiBaseUrl, `/v0/vnext/open-loops/${loopId}/review`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getVNextBrainCharter(apiBaseUrl: string, userId: string) {
  return requestJson<{ brain_charter: VNextBrainCharterRecord | null }>(
    apiBaseUrl,
    "/v0/vnext/settings/brain-charter",
    undefined,
    { user_id: userId },
  );
}

export function upsertVNextBrainCharter(
  apiBaseUrl: string,
  payload: VNextBrainCharterUpsertPayload,
) {
  return requestJson<{ brain_charter: VNextBrainCharterRecord }>(
    apiBaseUrl,
    "/v0/vnext/settings/brain-charter",
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
  );
}

export function getVNextSchedulerStatus(apiBaseUrl: string, userId: string) {
  return requestJson<VNextSchedulerStatus>(
    apiBaseUrl,
    "/v0/vnext/scheduler/status",
    undefined,
    { user_id: userId },
  );
}

export function getVNextSchedulerFailures(apiBaseUrl: string, userId: string, limit = 20) {
  return requestJson<{ items: VNextSchedulerRunRecord[]; count: number }>(
    apiBaseUrl,
    "/v0/vnext/scheduler/failures",
    undefined,
    { user_id: userId, limit: String(limit) },
  );
}

export function getVNextPolicyTelemetry(apiBaseUrl: string, userId: string) {
  return requestJson<{ summary: VNextPolicyTelemetrySummary }>(
    apiBaseUrl,
    "/v0/vnext/agents/policy-telemetry",
    undefined,
    { user_id: userId },
  );
}

export function patchVNextSchedulerWorkflow(
  apiBaseUrl: string,
  workflowType: string,
  payload: {
    user_id: string;
    enabled?: boolean;
    paused?: boolean;
    schedule_json?: JsonObject;
    timezone?: string;
    model_options?: JsonObject;
  },
) {
  return requestJson<{ workflow: VNextSchedulerWorkflowRecord; policy_decision: VNextPolicyDecisionRecord }>(
    apiBaseUrl,
    `/v0/vnext/scheduler/workflows/${workflowType}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export function runVNextSchedulerWorkflowNow(
  apiBaseUrl: string,
  workflowType: string,
  payload: { user_id: string; scope?: JsonObject; options?: JsonObject },
) {
  return requestJson<{
    run: VNextSchedulerRunRecord;
    artifact: VNextArtifactRecord | null;
    policy_decision: VNextPolicyDecisionRecord;
  }>(apiBaseUrl, `/v0/vnext/scheduler/workflows/${workflowType}/run-now`, {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}

export function runVNextSchedulerDue(
  apiBaseUrl: string,
  payload: { user_id: string; limit?: number },
) {
  return requestJson<{
    checked_at: string;
    due_count: number;
    runs: { workflow_type: string; scheduled_for: string; run: VNextSchedulerRunRecord; artifact: VNextArtifactRecord | null }[];
    policy_decision: VNextPolicyDecisionRecord;
  }>(apiBaseUrl, "/v0/vnext/scheduler/run-due", {
    method: "POST",
    body: JSON.stringify(payload),
    timeoutMs: LONG_RUNNING_MUTATION_TIMEOUT_MS,
  });
}
