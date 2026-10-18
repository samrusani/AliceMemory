import { ContinuityCaptureForm } from "../../components/continuity-capture-form";
import { ContinuityCorrectionForm } from "../../components/continuity-correction-form";
import { ContinuityDailyBriefPanel } from "../../components/continuity-daily-brief";
import { ContinuityInboxList } from "../../components/continuity-inbox-list";
import { ContinuityOpenLoopsPanel } from "../../components/continuity-open-loops-panel";
import { ContinuityRecallPanel } from "../../components/continuity-recall-panel";
import { ContinuityReviewQueue } from "../../components/continuity-review-queue";
import { ContinuityWeeklyReviewPanel } from "../../components/continuity-weekly-review";
import { EmptyState } from "../../components/empty-state";
import { PageHeader } from "../../components/page-header";
import { ResumptionBrief } from "../../components/resumption-brief";
import { SectionCard } from "../../components/section-card";
import { StatusBadge } from "../../components/status-badge";
import { ThreadHealthDashboard } from "../../components/thread-health-dashboard";
import type {
  ApiSource,
  ContinuityCaptureInboxItem,
  ContinuityCaptureInboxSummary,
  ContinuityDailyBrief,
  ContinuityOpenLoopDashboard,
  ContinuityRecallResult,
  ContinuityRecallSummary,
  ContinuityReviewDetail,
  ContinuityReviewObject,
  ContinuityReviewQueueSummary,
  ContinuityReviewStatusFilter,
  ContinuityResumptionBrief,
  ContinuityWeeklyReview,
  ThreadHealthDashboardSummary,
  WhatChangedAnswer,
} from "../../lib/api";
import {
  getContinuityReviewDetail,
  combinePageModes,
  getApiConfig,
  getContinuityCaptureDetail,
  getContinuityDailyBrief,
  getContinuityOpenLoopDashboard,
  getContinuityResumptionBrief,
  getThreadHealthDashboard,
  getContinuityWeeklyReview,
  hasLiveApiConfig,
  listContinuityReviewQueue,
  listContinuityCaptures,
  pageModeLabel,
  postWhatChanged,
  queryContinuityRecall,
} from "../../lib/api";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

type SettledRead<T> = { ok: true; value: T } | { ok: false; error: unknown };

function settleRead<T>(promise: Promise<T>): Promise<SettledRead<T>> {
  return promise.then(
    (value) => ({ ok: true, value }),
    (error: unknown) => ({ ok: false, error }),
  );
}

function unwrapRead<T>(result: SettledRead<T>): T {
  if (!result.ok) {
    throw result.error;
  }
  return result.value;
}

function normalizeParam(value: string | string[] | undefined) {
  if (Array.isArray(value)) {
    return normalizeParam(value[0]);
  }
  return value?.trim() ?? "";
}

function parsePositiveInt(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return parsed;
}

function parseNonNegativeInt(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return fallback;
  }
  return parsed;
}

function resolveSelectedCaptureId(requestedCaptureId: string, items: ContinuityCaptureInboxItem[]) {
  if (!items.length) {
    return "";
  }

  const available = new Set(items.map((item) => item.capture_event.id));
  if (requestedCaptureId && available.has(requestedCaptureId)) {
    return requestedCaptureId;
  }

  return items[0]?.capture_event.id ?? "";
}

function resolveSelectedReviewObjectId(requestedObjectId: string, items: ContinuityReviewObject[]) {
  if (!items.length) {
    return "";
  }

  const available = new Set(items.map((item) => item.id));
  if (requestedObjectId && available.has(requestedObjectId)) {
    return requestedObjectId;
  }

  return items[0]?.id ?? "";
}

function parseReviewStatus(value: string): ContinuityReviewStatusFilter {
  if (
    value === "correction_ready" ||
    value === "active" ||
    value === "stale" ||
    value === "superseded" ||
    value === "deleted" ||
    value === "all"
  ) {
    return value;
  }
  return "correction_ready";
}

const continuityCaptureFixtures: ContinuityCaptureInboxItem[] = [
  {
    capture_event: {
      id: "capture-fixture-1",
      raw_content: "Finalize launch checklist",
      explicit_signal: "task",
      admission_posture: "DERIVED",
      admission_reason: "explicit_signal_task",
      created_at: "2026-03-29T09:00:00Z",
    },
    derived_object: {
      id: "object-fixture-1",
      capture_event_id: "capture-fixture-1",
      object_type: "NextAction",
      status: "active",
      title: "Next Action: Finalize launch checklist",
      body: {
        action_text: "Finalize launch checklist",
        raw_content: "Finalize launch checklist",
        explicit_signal: "task",
      },
      provenance: {
        capture_event_id: "capture-fixture-1",
        source_kind: "continuity_capture_event",
        admission_reason: "explicit_signal_task",
      },
      confidence: 1,
      created_at: "2026-03-29T09:00:00Z",
      updated_at: "2026-03-29T09:00:00Z",
    },
  },
  {
    capture_event: {
      id: "capture-fixture-2",
      raw_content: "Maybe revisit this next month",
      explicit_signal: null,
      admission_posture: "TRIAGE",
      admission_reason: "ambiguous_capture_requires_triage",
      created_at: "2026-03-29T09:10:00Z",
    },
    derived_object: null,
  },
];

const continuityCaptureSummaryFixture: ContinuityCaptureInboxSummary = {
  limit: 20,
  returned_count: continuityCaptureFixtures.length,
  total_count: continuityCaptureFixtures.length,
  derived_count: 1,
  triage_count: 1,
  order: ["created_at_desc", "id_desc"],
};

const continuityRecallFixtures: ContinuityRecallResult[] = [
  {
    id: "recall-fixture-1",
    capture_event_id: "capture-fixture-1",
    object_type: "NextAction",
    status: "active",
    title: "Next Action: Finalize launch checklist",
    body: {
      action_text: "Finalize launch checklist",
    },
    provenance: {
      thread_id: "thread-fixture-1",
      project: "Launch Project",
      person: "Alex",
      source_event_ids: ["event-fixture-1"],
    },
    confirmation_status: "unconfirmed",
    admission_posture: "DERIVED",
    confidence: 1,
    relevance: 121,
    last_confirmed_at: null,
    supersedes_object_id: null,
    superseded_by_object_id: null,
    scope_matches: [
      { kind: "project", value: "launch project" },
      { kind: "person", value: "alex" },
    ],
    provenance_references: [
      { source_kind: "continuity_capture_event", source_id: "capture-fixture-1" },
      { source_kind: "event", source_id: "event-fixture-1" },
      { source_kind: "thread", source_id: "thread-fixture-1" },
    ],
    ordering: {
      scope_match_count: 2,
      query_term_match_count: 1,
      confirmation_rank: 2,
      freshness_posture: "aging",
      freshness_rank: 3,
      provenance_posture: "strong",
      provenance_rank: 3,
      supersession_posture: "current",
      supersession_rank: 3,
      posture_rank: 2,
      lifecycle_rank: 4,
      confidence: 1,
    },
    created_at: "2026-03-29T09:00:00Z",
    updated_at: "2026-03-29T09:00:00Z",
  },
];

const continuityRecallSummaryFixture: ContinuityRecallSummary = {
  query: null,
  filters: {
    since: null,
    until: null,
  },
  limit: 20,
  returned_count: continuityRecallFixtures.length,
  total_count: continuityRecallFixtures.length,
  order: ["relevance_desc", "created_at_desc", "id_desc"],
};

const continuityResumptionFixture: ContinuityResumptionBrief = {
  assembly_version: "continuity_resumption_brief_v0",
  scope: {
    since: null,
    until: null,
  },
  last_decision: {
    item: null,
    empty_state: {
      is_empty: true,
      message: "No decision found in the requested scope.",
    },
  },
  open_loops: {
    items: [],
    summary: {
      limit: 5,
      returned_count: 0,
      total_count: 0,
      order: ["created_at_desc", "id_desc"],
    },
    empty_state: {
      is_empty: true,
      message: "No open loops found in the requested scope.",
    },
  },
  recent_changes: {
    items: continuityRecallFixtures,
    summary: {
      limit: 5,
      returned_count: continuityRecallFixtures.length,
      total_count: continuityRecallFixtures.length,
      order: ["created_at_desc", "id_desc"],
    },
    empty_state: {
      is_empty: false,
      message: "No recent changes found in the requested scope.",
    },
  },
  next_action: {
    item: continuityRecallFixtures[0],
    empty_state: {
      is_empty: false,
      message: "No next action found in the requested scope.",
    },
  },
  sources: ["continuity_capture_events", "continuity_objects"],
};

const continuityOpenLoopWaitingFixture: ContinuityRecallResult = {
  id: "open-loop-waiting-1",
  capture_event_id: "capture-open-loop-waiting-1",
  object_type: "WaitingFor",
  status: "active",
  title: "Waiting For: Vendor quote",
  body: {
    waiting_for_text: "Vendor quote",
  },
  provenance: {
    thread_id: "thread-fixture-1",
    project: "Launch Project",
  },
  confirmation_status: "unconfirmed",
  admission_posture: "DERIVED",
  confidence: 1,
  relevance: 96,
  last_confirmed_at: null,
  supersedes_object_id: null,
  superseded_by_object_id: null,
  scope_matches: [],
  provenance_references: [{ source_kind: "continuity_capture_event", source_id: "capture-open-loop-waiting-1" }],
  ordering: {
    scope_match_count: 0,
    query_term_match_count: 0,
    confirmation_rank: 2,
    freshness_posture: "aging",
    freshness_rank: 3,
    provenance_posture: "partial",
    provenance_rank: 2,
    supersession_posture: "current",
    supersession_rank: 3,
    posture_rank: 2,
    lifecycle_rank: 4,
    confidence: 1,
  },
  created_at: "2026-03-29T09:30:00Z",
  updated_at: "2026-03-29T09:30:00Z",
};

const continuityOpenLoopBlockerFixture: ContinuityRecallResult = {
  id: "open-loop-blocker-1",
  capture_event_id: "capture-open-loop-blocker-1",
  object_type: "Blocker",
  status: "active",
  title: "Blocker: Await security approval",
  body: {
    blocking_reason: "Await security approval",
  },
  provenance: {
    thread_id: "thread-fixture-1",
    project: "Launch Project",
  },
  confirmation_status: "unconfirmed",
  admission_posture: "DERIVED",
  confidence: 1,
  relevance: 95,
  last_confirmed_at: null,
  supersedes_object_id: null,
  superseded_by_object_id: null,
  scope_matches: [],
  provenance_references: [{ source_kind: "continuity_capture_event", source_id: "capture-open-loop-blocker-1" }],
  ordering: {
    scope_match_count: 0,
    query_term_match_count: 0,
    confirmation_rank: 2,
    freshness_posture: "aging",
    freshness_rank: 3,
    provenance_posture: "partial",
    provenance_rank: 2,
    supersession_posture: "current",
    supersession_rank: 3,
    posture_rank: 2,
    lifecycle_rank: 4,
    confidence: 1,
  },
  created_at: "2026-03-29T09:35:00Z",
  updated_at: "2026-03-29T09:35:00Z",
};

const continuityOpenLoopStaleFixture: ContinuityRecallResult = {
  id: "open-loop-stale-1",
  capture_event_id: "capture-open-loop-stale-1",
  object_type: "WaitingFor",
  status: "stale",
  title: "Waiting For: Stale finance response",
  body: {
    waiting_for_text: "Stale finance response",
  },
  provenance: {
    thread_id: "thread-fixture-1",
    project: "Launch Project",
  },
  confirmation_status: "unconfirmed",
  admission_posture: "DERIVED",
  confidence: 1,
  relevance: 90,
  last_confirmed_at: null,
  supersedes_object_id: null,
  superseded_by_object_id: null,
  scope_matches: [],
  provenance_references: [{ source_kind: "continuity_capture_event", source_id: "capture-open-loop-stale-1" }],
  ordering: {
    scope_match_count: 0,
    query_term_match_count: 0,
    confirmation_rank: 2,
    freshness_posture: "stale",
    freshness_rank: 2,
    provenance_posture: "partial",
    provenance_rank: 2,
    supersession_posture: "historical",
    supersession_rank: 2,
    posture_rank: 2,
    lifecycle_rank: 3,
    confidence: 1,
  },
  created_at: "2026-03-29T09:40:00Z",
  updated_at: "2026-03-29T09:40:00Z",
};

const continuityOpenLoopDashboardFixture: ContinuityOpenLoopDashboard = {
  scope: {
    since: null,
    until: null,
  },
  waiting_for: {
    items: [continuityOpenLoopWaitingFixture],
    summary: {
      limit: 20,
      returned_count: 1,
      total_count: 1,
      order: ["created_at_desc", "id_desc"],
    },
    empty_state: {
      is_empty: false,
      message: "No waiting-for items in the requested scope.",
    },
  },
  blocker: {
    items: [continuityOpenLoopBlockerFixture],
    summary: {
      limit: 20,
      returned_count: 1,
      total_count: 1,
      order: ["created_at_desc", "id_desc"],
    },
    empty_state: {
      is_empty: false,
      message: "No blocker items in the requested scope.",
    },
  },
  stale: {
    items: [continuityOpenLoopStaleFixture],
    summary: {
      limit: 20,
      returned_count: 1,
      total_count: 1,
      order: ["created_at_desc", "id_desc"],
    },
    empty_state: {
      is_empty: false,
      message: "No stale items in the requested scope.",
    },
  },
  next_action: {
    items: [continuityRecallFixtures[0]],
    summary: {
      limit: 20,
      returned_count: 1,
      total_count: 1,
      order: ["created_at_desc", "id_desc"],
    },
    empty_state: {
      is_empty: false,
      message: "No next-action items in the requested scope.",
    },
  },
  summary: {
    limit: 20,
    total_count: 4,
    posture_order: ["waiting_for", "blocker", "stale", "next_action"],
    item_order: ["created_at_desc", "id_desc"],
  },
  sources: ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
};

const continuityDailyBriefFixture: ContinuityDailyBrief = {
  assembly_version: "continuity_daily_brief_v0",
  scope: {
    since: null,
    until: null,
  },
  waiting_for_highlights: continuityOpenLoopDashboardFixture.waiting_for,
  blocker_highlights: continuityOpenLoopDashboardFixture.blocker,
  stale_items: continuityOpenLoopDashboardFixture.stale,
  next_suggested_action: {
    item: continuityRecallFixtures[0],
    empty_state: {
      is_empty: false,
      message: "No next suggested action in the requested scope.",
    },
  },
  sources: ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
};

const continuityWeeklyReviewFixture: ContinuityWeeklyReview = {
  assembly_version: "continuity_weekly_review_v0",
  scope: {
    since: null,
    until: null,
  },
  rollup: {
    total_count: 4,
    waiting_for_count: 1,
    blocker_count: 1,
    stale_count: 1,
    correction_recurrence_count: 0,
    freshness_drift_count: 1,
    next_action_count: 1,
    posture_order: ["waiting_for", "blocker", "stale", "next_action"],
  },
  waiting_for: continuityOpenLoopDashboardFixture.waiting_for,
  blocker: continuityOpenLoopDashboardFixture.blocker,
  stale: continuityOpenLoopDashboardFixture.stale,
  next_action: continuityOpenLoopDashboardFixture.next_action,
  sources: ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
};

const continuityReviewFixtures: ContinuityReviewObject[] = [
  {
    id: "review-fixture-1",
    capture_event_id: "capture-fixture-1",
    object_type: "NextAction",
    status: "active",
    title: "Next Action: Finalize launch checklist",
    body: {
      action_text: "Finalize launch checklist",
    },
    provenance: {
      thread_id: "thread-fixture-1",
    },
    confidence: 1,
    last_confirmed_at: null,
    supersedes_object_id: null,
    superseded_by_object_id: null,
    created_at: "2026-03-29T09:00:00Z",
    updated_at: "2026-03-29T09:00:00Z",
  },
];

const continuityReviewSummaryFixture: ContinuityReviewQueueSummary = {
  status: "correction_ready",
  limit: 20,
  returned_count: continuityReviewFixtures.length,
  total_count: continuityReviewFixtures.length,
  order: ["updated_at_desc", "created_at_desc", "id_desc"],
};

const threadHealthDashboardFixture: ThreadHealthDashboardSummary = {
  posture: "watch",
  total_thread_count: 3,
  recent_thread_count: 1,
  stale_thread_count: 1,
  risky_thread_count: 1,
  watch_thread_count: 1,
  thresholds: {
    recent_window_hours: 24,
    stale_window_hours: 72,
    risky_score_threshold: 2,
  },
  recent_threads: [
    {
      thread: {
        id: "thread-fixture-1",
        title: "Launch Project",
        agent_profile_id: "assistant_default",
        created_at: "2026-03-29T09:00:00Z",
        updated_at: "2026-03-29T09:20:00Z",
      },
      health_posture: "healthy",
      activity_posture: "recent",
      risk_posture: "normal",
      risk_score: 0,
      last_activity_at: "2026-03-29T09:20:00Z",
      last_conversation_at: "2026-03-29T09:20:00Z",
      hours_since_last_activity: 4,
      conversation_event_count: 3,
      operational_event_count: 1,
      active_session_count: 1,
      open_loop_count: 1,
      stale_open_loop_count: 0,
      unresolved_contradiction_count: 0,
      weak_trust_signal_count: 0,
      reasons: ["No active contradiction, stale open-loop, or weak-trust pressure is currently visible."],
      recommended_action: "No immediate intervention required.",
    },
  ],
  stale_threads: [
    {
      thread: {
        id: "thread-fixture-2",
        title: "Vendor Follow-up",
        agent_profile_id: "assistant_default",
        created_at: "2026-03-20T09:00:00Z",
        updated_at: "2026-03-21T09:00:00Z",
      },
      health_posture: "watch",
      activity_posture: "stale",
      risk_posture: "watch",
      risk_score: 1,
      last_activity_at: "2026-03-21T09:00:00Z",
      last_conversation_at: "2026-03-21T09:00:00Z",
      hours_since_last_activity: 96,
      conversation_event_count: 1,
      operational_event_count: 1,
      active_session_count: 0,
      open_loop_count: 1,
      stale_open_loop_count: 1,
      unresolved_contradiction_count: 0,
      weak_trust_signal_count: 0,
      reasons: ["1 stale open-loop item(s)."],
      recommended_action: "Review stale waiting-for or blocker items linked to this thread.",
    },
  ],
  risky_threads: [
    {
      thread: {
        id: "thread-fixture-3",
        title: "Preference conflict",
        agent_profile_id: "assistant_default",
        created_at: "2026-03-25T09:00:00Z",
        updated_at: "2026-03-28T09:00:00Z",
      },
      health_posture: "critical",
      activity_posture: "current",
      risk_posture: "risky",
      risk_score: 2,
      last_activity_at: "2026-03-28T09:00:00Z",
      last_conversation_at: "2026-03-28T09:00:00Z",
      hours_since_last_activity: 28,
      conversation_event_count: 2,
      operational_event_count: 0,
      active_session_count: 0,
      open_loop_count: 0,
      stale_open_loop_count: 0,
      unresolved_contradiction_count: 1,
      weak_trust_signal_count: 0,
      reasons: ["1 unresolved contradiction case(s)."],
      recommended_action: "Resolve contradiction cases before trusting this thread for recall or briefing.",
    },
  ],
  items: [],
  sources: [
    "threads",
    "thread_sessions",
    "thread_events",
    "continuity_recall",
    "contradiction_cases",
    "trust_signals",
  ],
};

function renderDetail(item: ContinuityCaptureInboxItem | null, source: ApiSource | "unavailable" | null, unavailableReason?: string) {
  if (!item) {
    return (
      <SectionCard
        eyebrow="Capture detail"
        title="No capture selected"
        description="Select one capture row to inspect its immutable event payload and derived-object provenance."
      >
        <EmptyState
          title="Detail panel is idle"
          description="Choose one capture from the inbox to inspect posture and provenance."
        />
      </SectionCard>
    );
  }

  const capture = item.capture_event;
  const derived = item.derived_object;

  return (
    <SectionCard
      eyebrow="Capture detail"
      title={capture.raw_content}
      description="Derived objects remain explicitly linked to immutable capture evidence through provenance fields."
    >
      <div className="detail-grid">
        <div className="detail-summary">
          <StatusBadge
            status={source ?? "unavailable"}
            label={
              source === "live"
                ? "Live detail"
                : source === "fixture"
                  ? "Fixture detail"
                  : "Detail unavailable"
            }
          />
          <StatusBadge
            status={capture.admission_posture}
            label={capture.admission_posture === "TRIAGE" ? "Triage" : "Derived"}
          />
        </div>

        {unavailableReason ? (
          <div className="execution-summary__note execution-summary__note--danger">
            <p className="execution-summary__label">Detail read</p>
            <p>{unavailableReason}</p>
          </div>
        ) : null}

        <dl className="key-value-grid key-value-grid--compact">
          <div>
            <dt>Capture ID</dt>
            <dd className="mono">{capture.id}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>{capture.created_at}</dd>
          </div>
          <div>
            <dt>Explicit signal</dt>
            <dd>{capture.explicit_signal ?? "None"}</dd>
          </div>
          <div>
            <dt>Admission reason</dt>
            <dd>{capture.admission_reason}</dd>
          </div>
        </dl>

        {derived ? (
          <>
            <div className="detail-group">
              <h3>Derived object</h3>
              <pre className="execution-summary__code">{JSON.stringify(derived, null, 2)}</pre>
            </div>
            <div className="detail-group detail-group--muted">
              <h3>Provenance</h3>
              <pre className="execution-summary__code">{JSON.stringify(derived.provenance, null, 2)}</pre>
            </div>
          </>
        ) : (
          <div className="detail-group detail-group--muted">
            <h3>Triage posture</h3>
            <p className="muted-copy">
              This capture is stored immutably and visible in inbox triage. No durable object was promoted.
            </p>
          </div>
        )}
      </div>
    </SectionCard>
  );
}

export default async function ContinuityPage({
  searchParams,
}: {
  searchParams?: SearchParams;
}) {
  const params = (searchParams ? await searchParams : {}) as Record<
    string,
    string | string[] | undefined
  >;

  const requestedCaptureId = normalizeParam(params.capture);
  const changedTopic = normalizeParam(params.what_changed);
  const requestedReviewObjectId = normalizeParam(params.review_object);
  const recallQuery = normalizeParam(params.recall_query);
  const recallThreadId = normalizeParam(params.recall_thread);
  const recallTaskId = normalizeParam(params.recall_task);
  const recallProject = normalizeParam(params.recall_project);
  const recallPerson = normalizeParam(params.recall_person);
  const recallSince = normalizeParam(params.recall_since);
  const recallUntil = normalizeParam(params.recall_until);
  const recallLimit = parsePositiveInt(normalizeParam(params.recall_limit), 20);
  const reviewStatus = parseReviewStatus(normalizeParam(params.review_status));
  const reviewLimit = parsePositiveInt(normalizeParam(params.review_limit), 20);
  const openLoopLimit = parseNonNegativeInt(normalizeParam(params.open_loop_limit), 20);
  const dailyBriefLimit = parseNonNegativeInt(normalizeParam(params.daily_limit), 3);
  const weeklyReviewLimit = parseNonNegativeInt(normalizeParam(params.weekly_limit), 5);
  const resumptionRecentChanges = parseNonNegativeInt(normalizeParam(params.resumption_recent), 5);
  const resumptionOpenLoops = parseNonNegativeInt(normalizeParam(params.resumption_open), 5);

  const apiConfig = getApiConfig();
  const liveModeReady = hasLiveApiConfig(apiConfig);
  let whatChanged: WhatChangedAnswer | null = null;
  let whatChangedError: string | undefined;
  if (liveModeReady && changedTopic) {
    try {
      whatChanged = await postWhatChanged(apiConfig.apiBaseUrl, apiConfig.userId, changedTopic);
    } catch (error) {
      whatChangedError =
        error instanceof Error ? error.message : "What changed could not be loaded.";
    }
  }

  // Launch independent continuity reads together. Each result still degrades
  // separately below, while the detail reads remain correctly dependent on
  // the selected list and review-queue rows.
  const liveReads = liveModeReady
    ? {
        captures: settleRead(listContinuityCaptures(apiConfig.apiBaseUrl, apiConfig.userId, { limit: 20 })),
        recall: settleRead(queryContinuityRecall(apiConfig.apiBaseUrl, apiConfig.userId, {
          query: recallQuery,
          threadId: recallThreadId,
          taskId: recallTaskId,
          project: recallProject,
          person: recallPerson,
          since: recallSince,
          until: recallUntil,
          limit: recallLimit,
        })),
        resumption: settleRead(getContinuityResumptionBrief(apiConfig.apiBaseUrl, apiConfig.userId, {
          query: recallQuery,
          threadId: recallThreadId,
          taskId: recallTaskId,
          project: recallProject,
          person: recallPerson,
          since: recallSince,
          until: recallUntil,
          maxRecentChanges: resumptionRecentChanges,
          maxOpenLoops: resumptionOpenLoops,
        })),
        openLoops: settleRead(getContinuityOpenLoopDashboard(apiConfig.apiBaseUrl, apiConfig.userId, {
          query: recallQuery,
          threadId: recallThreadId,
          taskId: recallTaskId,
          project: recallProject,
          person: recallPerson,
          since: recallSince,
          until: recallUntil,
          limit: openLoopLimit,
        })),
        dailyBrief: settleRead(getContinuityDailyBrief(apiConfig.apiBaseUrl, apiConfig.userId, {
          query: recallQuery,
          threadId: recallThreadId,
          taskId: recallTaskId,
          project: recallProject,
          person: recallPerson,
          since: recallSince,
          until: recallUntil,
          limit: dailyBriefLimit,
        })),
        weeklyReview: settleRead(getContinuityWeeklyReview(apiConfig.apiBaseUrl, apiConfig.userId, {
          query: recallQuery,
          threadId: recallThreadId,
          taskId: recallTaskId,
          project: recallProject,
          person: recallPerson,
          since: recallSince,
          until: recallUntil,
          limit: weeklyReviewLimit,
        })),
        threadHealth: settleRead(getThreadHealthDashboard(apiConfig.apiBaseUrl, apiConfig.userId)),
        reviewQueue: settleRead(listContinuityReviewQueue(apiConfig.apiBaseUrl, apiConfig.userId, {
          status: reviewStatus,
          limit: reviewLimit,
        })),
      }
    : null;

  // Chain the selected review-detail read directly from its parent queue read.
  // Attaching this continuation now prevents unrelated capture/brief panels
  // from delaying detail I/O after the queue has already resolved.
  const liveReviewDetailRead = liveModeReady
    ? liveReads!.reviewQueue
        .then((queueRead) => {
          const queuePayload = unwrapRead(queueRead);
          const selectedObjectId = resolveSelectedReviewObjectId(
            requestedReviewObjectId,
            queuePayload.items,
          );
          if (!selectedObjectId) {
            return null;
          }
          return settleRead(
            getContinuityReviewDetail(
              apiConfig.apiBaseUrl,
              selectedObjectId,
              apiConfig.userId,
            ),
          ).then((detailRead) => ({ selectedObjectId, detailRead }));
        })
        .catch(() => null)
    : null;

  let items = continuityCaptureFixtures;
  let summary = continuityCaptureSummaryFixture;
  let listSource: ApiSource = "fixture";
  let listUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.captures);
      items = payload.items;
      summary = payload.summary;
      listSource = "live";
    } catch (error) {
      listUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity capture inbox could not be loaded.";
    }
  }

  const selectedCaptureId = resolveSelectedCaptureId(requestedCaptureId, items);
  const selectedFromList = items.find((item) => item.capture_event.id === selectedCaptureId) ?? null;
  let selectedItem = selectedFromList;
  let selectedSource: ApiSource | "unavailable" | null = selectedFromList ? listSource : null;
  let selectedUnavailableReason: string | undefined;

  if (selectedFromList && liveModeReady && listSource === "live") {
    try {
      const payload = await getContinuityCaptureDetail(
        apiConfig.apiBaseUrl,
        selectedFromList.capture_event.id,
        apiConfig.userId,
      );
      selectedItem = payload.capture;
      selectedSource = "live";
    } catch (error) {
      selectedUnavailableReason =
        error instanceof Error
          ? error.message
          : "Selected continuity capture detail could not be loaded.";
      selectedSource = "unavailable";
    }
  }

  let recallResults = continuityRecallFixtures;
  let recallSummary = continuityRecallSummaryFixture;
  let recallSource: ApiSource = "fixture";
  let recallUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.recall);
      recallResults = payload.items;
      recallSummary = payload.summary;
      recallSource = "live";
    } catch (error) {
      recallUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity recall query could not be loaded.";
    }
  }

  let brief = continuityResumptionFixture;
  let resumptionSource: ApiSource = "fixture";
  let resumptionUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.resumption);
      brief = payload.brief;
      resumptionSource = "live";
    } catch (error) {
      resumptionUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity resumption brief could not be loaded.";
    }
  }

  let openLoopDashboard = continuityOpenLoopDashboardFixture;
  let openLoopSource: ApiSource | "unavailable" = "fixture";
  let openLoopUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.openLoops);
      openLoopDashboard = payload.dashboard;
      openLoopSource = "live";
    } catch (error) {
      openLoopSource = "unavailable";
      openLoopUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity open-loop dashboard could not be loaded.";
    }
  }

  let dailyBrief = continuityDailyBriefFixture;
  let dailyBriefSource: ApiSource = "fixture";
  let dailyBriefUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.dailyBrief);
      dailyBrief = payload.brief;
      dailyBriefSource = "live";
    } catch (error) {
      dailyBriefUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity daily brief could not be loaded.";
    }
  }

  let weeklyReview = continuityWeeklyReviewFixture;
  let weeklyReviewSource: ApiSource = "fixture";
  let weeklyReviewUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.weeklyReview);
      weeklyReview = payload.review;
      weeklyReviewSource = "live";
    } catch (error) {
      weeklyReviewUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity weekly review could not be loaded.";
    }
  }

  let threadHealthDashboard = threadHealthDashboardFixture;
  let threadHealthSource: ApiSource = "fixture";
  let threadHealthUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.threadHealth);
      threadHealthDashboard = payload.dashboard;
      threadHealthSource = "live";
    } catch (error) {
      threadHealthUnavailableReason =
        error instanceof Error
          ? error.message
          : "Thread health dashboard could not be loaded.";
    }
  }

  let reviewItems = continuityReviewFixtures;
  let reviewSummary = continuityReviewSummaryFixture;
  let reviewSource: ApiSource | "unavailable" = "fixture";
  let reviewUnavailableReason: string | undefined;

  if (liveModeReady) {
    try {
      const payload = unwrapRead(await liveReads!.reviewQueue);
      reviewItems = payload.items;
      reviewSummary = payload.summary;
      reviewSource = "live";
    } catch (error) {
      reviewSource = "unavailable";
      reviewUnavailableReason =
        error instanceof Error
          ? error.message
          : "Continuity review queue could not be loaded.";
    }
  }

  const selectedReviewObjectId = resolveSelectedReviewObjectId(requestedReviewObjectId, reviewItems);
  const selectedReviewFromQueue = reviewItems.find((item) => item.id === selectedReviewObjectId) ?? null;
  let selectedReviewDetail: ContinuityReviewDetail | null = selectedReviewFromQueue
    ? {
        continuity_object: selectedReviewFromQueue,
        correction_events: [],
        supersession_chain: { supersedes: null, superseded_by: null },
      }
    : null;
  let correctionSource: ApiSource | "unavailable" = selectedReviewFromQueue ? reviewSource : "unavailable";
  const correctionMutationTargetSource: ApiSource | "unavailable" = selectedReviewFromQueue
    ? reviewSource
    : "unavailable";
  let correctionUnavailableReason: string | undefined;

  if (selectedReviewFromQueue && liveModeReady && reviewSource === "live") {
    const chainedDetail = await liveReviewDetailRead;
    if (chainedDetail?.selectedObjectId === selectedReviewFromQueue.id) {
      try {
        const payload = unwrapRead(chainedDetail.detailRead);
        selectedReviewDetail = payload.review;
        correctionSource = "live";
      } catch (error) {
        correctionUnavailableReason =
          error instanceof Error
            ? error.message
            : "Selected continuity review detail could not be loaded.";
        correctionSource = "unavailable";
      }
    } else {
      correctionSource = "unavailable";
      correctionUnavailableReason = "Selected continuity review detail could not be loaded.";
    }
  }

  const pageMode = combinePageModes(
    listSource,
    selectedSource === "unavailable" ? null : selectedSource,
    recallSource,
    resumptionSource,
    openLoopSource === "unavailable" ? null : openLoopSource,
    dailyBriefSource,
    weeklyReviewSource,
    threadHealthSource,
    reviewSource === "unavailable" ? null : reviewSource,
    correctionSource === "unavailable" ? null : correctionSource,
  );

  return (
    <div className="stack">
      <PageHeader
        eyebrow="Continuity"
        title="Continuity workspace"
        description="Capture quickly, query continuity with provenance-backed recall, and compile deterministic resumption sections."
        meta={<StatusBadge status={pageMode} label={pageModeLabel(pageMode)} />}
      />

      {changedTopic ? (
        <SectionCard title="What changed" eyebrow={changedTopic}>
          {whatChanged ? (
            <div className="stack">
              <p>{whatChanged.notice}</p>
              {whatChanged.message ? (
                <p>
                  {whatChanged.message} Search with {whatChanged.search}.
                </p>
              ) : null}
              {whatChanged.chains.map((chain) => (
                <div key={chain.start}>
                  <p>{chain.current ? chain.current.change : "No settled step."}</p>
                  <ol>
                    {chain.history.map((step) => (
                      <li key={step.id}>
                        {step.date} {step.change}
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          ) : (
            <p>
              {whatChangedError ??
                "This view calls POST /v0/vnext/changes. The API is not configured."}
            </p>
          )}
        </SectionCard>
      ) : null}

      <ContinuityCaptureForm
        apiBaseUrl={apiConfig.apiBaseUrl}
        userId={apiConfig.userId}
        source={listSource}
      />

      <div className="grid grid--two">
        <ContinuityInboxList
          items={items}
          selectedCaptureId={selectedCaptureId}
          summary={summary}
          source={listSource}
          unavailableReason={listUnavailableReason}
        />
        {renderDetail(selectedItem, selectedSource, selectedUnavailableReason)}
      </div>

      <div className="grid grid--two">
        <ContinuityRecallPanel
          results={recallResults}
          summary={recallSummary}
          source={recallSource}
          unavailableReason={recallUnavailableReason}
          filters={{
            query: recallQuery,
            threadId: recallThreadId,
            taskId: recallTaskId,
            project: recallProject,
            person: recallPerson,
            since: recallSince,
            until: recallUntil,
            limit: recallLimit,
          }}
        />
        <ResumptionBrief
          brief={brief}
          source={resumptionSource}
          unavailableReason={resumptionUnavailableReason}
        />
      </div>

      <div className="grid grid--two">
        <ContinuityOpenLoopsPanel
          apiBaseUrl={apiConfig.apiBaseUrl}
          userId={apiConfig.userId}
          dashboard={openLoopDashboard}
          source={openLoopSource}
          unavailableReason={openLoopUnavailableReason}
        />
        <ContinuityDailyBriefPanel
          brief={dailyBrief}
          source={dailyBriefSource}
          unavailableReason={dailyBriefUnavailableReason}
        />
      </div>

      <ContinuityWeeklyReviewPanel
        review={weeklyReview}
        source={weeklyReviewSource}
        unavailableReason={weeklyReviewUnavailableReason}
      />

      <ThreadHealthDashboard
        dashboard={threadHealthDashboard}
        source={threadHealthSource}
        unavailableReason={threadHealthUnavailableReason}
      />

      <div className="grid grid--two">
        <ContinuityReviewQueue
          items={reviewItems}
          summary={reviewSummary}
          selectedObjectId={selectedReviewObjectId}
          source={reviewSource}
          unavailableReason={reviewUnavailableReason}
          filters={{
            status: reviewStatus,
            limit: reviewLimit,
          }}
        />
        <div className="detail-stack">
          {correctionUnavailableReason ? (
            <div className="execution-summary__note execution-summary__note--danger">
              <p className="execution-summary__label">Review detail</p>
              <p>{correctionUnavailableReason}</p>
            </div>
          ) : null}
          <ContinuityCorrectionForm
            apiBaseUrl={apiConfig.apiBaseUrl}
            userId={apiConfig.userId}
            source={correctionSource}
            mutationTargetSource={correctionMutationTargetSource}
            review={selectedReviewDetail}
          />
        </div>
      </div>
    </div>
  );
}
