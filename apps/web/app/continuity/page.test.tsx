import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ContinuityPage from "./page";

const {
  getApiConfigMock,
  getContinuityDailyBriefMock,
  getContinuityOpenLoopDashboardMock,
  getContinuityCaptureDetailMock,
  getContinuityReviewDetailMock,
  getContinuityResumptionBriefMock,
  getThreadHealthDashboardMock,
  getContinuityWeeklyReviewMock,
  hasLiveApiConfigMock,
  listContinuityReviewQueueMock,
  listContinuityCapturesMock,
  queryContinuityRecallMock,
  postWhatChangedMock,
  refreshMock,
} = vi.hoisted(() => ({
  getApiConfigMock: vi.fn(),
  getContinuityDailyBriefMock: vi.fn(),
  getContinuityOpenLoopDashboardMock: vi.fn(),
  getContinuityCaptureDetailMock: vi.fn(),
  getContinuityReviewDetailMock: vi.fn(),
  getContinuityResumptionBriefMock: vi.fn(),
  getThreadHealthDashboardMock: vi.fn(),
  getContinuityWeeklyReviewMock: vi.fn(),
  hasLiveApiConfigMock: vi.fn(),
  listContinuityReviewQueueMock: vi.fn(),
  listContinuityCapturesMock: vi.fn(),
  queryContinuityRecallMock: vi.fn(),
  postWhatChangedMock: vi.fn(),
  refreshMock: vi.fn(),
}));

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    className,
    "aria-current": ariaCurrent,
  }: {
    href: string;
    children: React.ReactNode;
    className?: string;
    "aria-current"?: React.AriaAttributes["aria-current"];
  }) => (
    <a href={href} className={className} aria-current={ariaCurrent}>
      {children}
    </a>
  ),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    refresh: refreshMock,
  }),
}));

vi.mock("../../lib/api", async () => {
  const actual = await vi.importActual("../../lib/api");
  return {
    ...actual,
    getApiConfig: getApiConfigMock,
    getContinuityDailyBrief: getContinuityDailyBriefMock,
    getContinuityOpenLoopDashboard: getContinuityOpenLoopDashboardMock,
    getContinuityCaptureDetail: getContinuityCaptureDetailMock,
    getContinuityReviewDetail: getContinuityReviewDetailMock,
    getContinuityResumptionBrief: getContinuityResumptionBriefMock,
    getThreadHealthDashboard: getThreadHealthDashboardMock,
    getContinuityWeeklyReview: getContinuityWeeklyReviewMock,
    hasLiveApiConfig: hasLiveApiConfigMock,
    listContinuityReviewQueue: listContinuityReviewQueueMock,
    listContinuityCaptures: listContinuityCapturesMock,
    queryContinuityRecall: queryContinuityRecallMock,
    postWhatChanged: postWhatChangedMock,
  };
});

describe("ContinuityPage", () => {
  beforeEach(() => {
    getApiConfigMock.mockReset();
    getContinuityDailyBriefMock.mockReset();
    getContinuityOpenLoopDashboardMock.mockReset();
    getContinuityCaptureDetailMock.mockReset();
    getContinuityReviewDetailMock.mockReset();
    getContinuityResumptionBriefMock.mockReset();
    getThreadHealthDashboardMock.mockReset();
    getContinuityWeeklyReviewMock.mockReset();
    hasLiveApiConfigMock.mockReset();
    listContinuityReviewQueueMock.mockReset();
    listContinuityCapturesMock.mockReset();
    queryContinuityRecallMock.mockReset();
    postWhatChangedMock.mockReset();
    refreshMock.mockReset();

    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "",
      userId: "",
      defaultThreadId: "",
      defaultToolId: "",
    });
    hasLiveApiConfigMock.mockReturnValue(false);
  });

  afterEach(() => {
    cleanup();
  });

  it("uses fixture continuity state when live API config is absent", async () => {
    render(await ContinuityPage({ searchParams: Promise.resolve({}) }));

    expect(screen.getByText("Fixture-backed")).toBeInTheDocument();
    expect(screen.getByText("Fixture inbox")).toBeInTheDocument();
    expect(screen.getByText("Fixture recall")).toBeInTheDocument();
    expect(screen.getByText("Fixture brief")).toBeInTheDocument();
    expect(screen.getByText("Fixture open loops")).toBeInTheDocument();
    expect(screen.getByText("Fixture daily brief")).toBeInTheDocument();
    expect(screen.getByText("Fixture weekly review")).toBeInTheDocument();
    expect(screen.getByText("Conversation health posture")).toBeInTheDocument();
    expect(screen.getByText("Fixture thread dashboard")).toBeInTheDocument();
    expect(screen.getByText("Fixture review queue")).toBeInTheDocument();
    expect(screen.getByText("Continuity recall")).toBeInTheDocument();
    expect(screen.getAllByText("Correction actions").length).toBeGreaterThan(0);
    expect(listContinuityCapturesMock).not.toHaveBeenCalled();
    expect(queryContinuityRecallMock).not.toHaveBeenCalled();
    expect(getContinuityResumptionBriefMock).not.toHaveBeenCalled();
    expect(getContinuityOpenLoopDashboardMock).not.toHaveBeenCalled();
    expect(getContinuityDailyBriefMock).not.toHaveBeenCalled();
    expect(getContinuityWeeklyReviewMock).not.toHaveBeenCalled();
    expect(getThreadHealthDashboardMock).not.toHaveBeenCalled();
    expect(listContinuityReviewQueueMock).not.toHaveBeenCalled();
  });

  it("renders live continuity inbox, recall, and resumption when reads succeed", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "https://api.example.com",
      userId: "user-1",
      defaultThreadId: "thread-1",
      defaultToolId: "tool-1",
    });
    hasLiveApiConfigMock.mockReturnValue(true);
    listContinuityCapturesMock.mockResolvedValue({
      items: [
        {
          capture_event: {
            id: "capture-live-1",
            raw_content: "Decision: Keep admission conservative",
            explicit_signal: null,
            admission_posture: "DERIVED",
            admission_reason: "high_confidence_prefix_decision",
            created_at: "2026-03-29T09:20:00Z",
          },
          derived_object: {
            id: "object-live-1",
            capture_event_id: "capture-live-1",
            object_type: "Decision",
            status: "active",
            title: "Decision: Keep admission conservative",
            body: {
              decision_text: "Keep admission conservative",
            },
            provenance: {
              capture_event_id: "capture-live-1",
              source_kind: "continuity_capture_event",
            },
            confidence: 0.95,
            created_at: "2026-03-29T09:20:00Z",
            updated_at: "2026-03-29T09:20:00Z",
          },
        },
      ],
      summary: {
        limit: 20,
        returned_count: 1,
        total_count: 1,
        derived_count: 1,
        triage_count: 0,
        order: ["created_at_desc", "id_desc"],
      },
    });
    getContinuityCaptureDetailMock.mockResolvedValue({
      capture: {
        capture_event: {
          id: "capture-live-1",
          raw_content: "Decision: Keep admission conservative",
          explicit_signal: null,
          admission_posture: "DERIVED",
          admission_reason: "high_confidence_prefix_decision",
          created_at: "2026-03-29T09:20:00Z",
        },
        derived_object: {
          id: "object-live-1",
          capture_event_id: "capture-live-1",
          object_type: "Decision",
          status: "active",
          title: "Decision: Keep admission conservative",
          body: {
            decision_text: "Keep admission conservative",
          },
          provenance: {
            capture_event_id: "capture-live-1",
            source_kind: "continuity_capture_event",
          },
          confidence: 0.95,
          created_at: "2026-03-29T09:20:00Z",
          updated_at: "2026-03-29T09:20:00Z",
        },
      },
    });
    queryContinuityRecallMock.mockResolvedValue({
      items: [
        {
          id: "recall-live-1",
          capture_event_id: "capture-live-1",
          object_type: "Decision",
          status: "active",
          title: "Decision: Keep admission conservative",
          body: { decision_text: "Keep admission conservative" },
          provenance: { thread_id: "thread-1" },
          confirmation_status: "confirmed",
          admission_posture: "DERIVED",
          confidence: 0.95,
          relevance: 130,
          last_confirmed_at: "2026-03-29T09:20:00Z",
          supersedes_object_id: null,
          superseded_by_object_id: null,
          scope_matches: [{ kind: "thread", value: "thread-1" }],
          provenance_references: [{ source_kind: "continuity_capture_event", source_id: "capture-live-1" }],
          ordering: {
            scope_match_count: 1,
            query_term_match_count: 1,
            confirmation_rank: 3,
            freshness_posture: "fresh",
            freshness_rank: 4,
            provenance_posture: "partial",
            provenance_rank: 2,
            supersession_posture: "current",
            supersession_rank: 3,
            posture_rank: 2,
            lifecycle_rank: 4,
            confidence: 0.95,
          },
          created_at: "2026-03-29T09:20:00Z",
          updated_at: "2026-03-29T09:20:00Z",
        },
      ],
      summary: {
        query: "decision",
        filters: {
          thread_id: "thread-1",
          since: null,
          until: null,
        },
        limit: 20,
        returned_count: 1,
        total_count: 1,
        order: ["relevance_desc", "created_at_desc", "id_desc"],
      },
    });
    getContinuityResumptionBriefMock.mockResolvedValue({
      brief: {
        assembly_version: "continuity_resumption_brief_v0",
        scope: { thread_id: "thread-1", since: null, until: null },
        last_decision: {
          item: {
            id: "recall-live-1",
            capture_event_id: "capture-live-1",
            object_type: "Decision",
            status: "active",
            title: "Decision: Keep admission conservative",
            body: { decision_text: "Keep admission conservative" },
            provenance: { thread_id: "thread-1" },
            confirmation_status: "confirmed",
            admission_posture: "DERIVED",
            confidence: 0.95,
            relevance: 130,
            last_confirmed_at: "2026-03-29T09:20:00Z",
            supersedes_object_id: null,
            superseded_by_object_id: null,
            scope_matches: [{ kind: "thread", value: "thread-1" }],
            provenance_references: [{ source_kind: "continuity_capture_event", source_id: "capture-live-1" }],
            ordering: {
              scope_match_count: 1,
              query_term_match_count: 1,
              confirmation_rank: 3,
              freshness_posture: "fresh",
              freshness_rank: 4,
              provenance_posture: "partial",
              provenance_rank: 2,
              supersession_posture: "current",
              supersession_rank: 3,
              posture_rank: 2,
              lifecycle_rank: 4,
              confidence: 0.95,
            },
            created_at: "2026-03-29T09:20:00Z",
            updated_at: "2026-03-29T09:20:00Z",
          },
          empty_state: { is_empty: false, message: "No decision found in the requested scope." },
        },
        open_loops: {
          items: [],
          summary: { limit: 5, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No open loops found in the requested scope." },
        },
        recent_changes: {
          items: [],
          summary: { limit: 5, returned_count: 0, total_count: 1, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No recent changes found in the requested scope." },
        },
        next_action: {
          item: null,
          empty_state: { is_empty: true, message: "No next action found in the requested scope." },
        },
        sources: ["continuity_capture_events", "continuity_objects"],
      },
    });
    getContinuityOpenLoopDashboardMock.mockResolvedValue({
      dashboard: {
        scope: { thread_id: "thread-1", since: null, until: null },
        waiting_for: {
          items: [],
          summary: { limit: 20, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No waiting-for items in the requested scope." },
        },
        blocker: {
          items: [],
          summary: { limit: 20, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No blocker items in the requested scope." },
        },
        stale: {
          items: [],
          summary: { limit: 20, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No stale items in the requested scope." },
        },
        next_action: {
          items: [],
          summary: { limit: 20, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No next-action items in the requested scope." },
        },
        summary: {
          limit: 20,
          total_count: 0,
          posture_order: ["waiting_for", "blocker", "stale", "next_action"],
          item_order: ["created_at_desc", "id_desc"],
        },
        sources: ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
      },
    });
    getContinuityDailyBriefMock.mockResolvedValue({
      brief: {
        assembly_version: "continuity_daily_brief_v0",
        scope: { thread_id: "thread-1", since: null, until: null },
        waiting_for_highlights: {
          items: [],
          summary: { limit: 3, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No waiting-for highlights for today in the requested scope." },
        },
        blocker_highlights: {
          items: [],
          summary: { limit: 3, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No blocker highlights for today in the requested scope." },
        },
        stale_items: {
          items: [],
          summary: { limit: 3, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No stale items for today in the requested scope." },
        },
        next_suggested_action: {
          item: null,
          empty_state: { is_empty: true, message: "No next suggested action in the requested scope." },
        },
        sources: ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
      },
    });
    getContinuityWeeklyReviewMock.mockResolvedValue({
      review: {
        assembly_version: "continuity_weekly_review_v0",
        scope: { thread_id: "thread-1", since: null, until: null },
        rollup: {
          total_count: 0,
          waiting_for_count: 0,
          blocker_count: 0,
          stale_count: 0,
          correction_recurrence_count: 0,
          freshness_drift_count: 0,
          next_action_count: 0,
          posture_order: ["waiting_for", "blocker", "stale", "next_action"],
        },
        waiting_for: {
          items: [],
          summary: { limit: 5, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No waiting-for items in the requested scope." },
        },
        blocker: {
          items: [],
          summary: { limit: 5, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No blocker items in the requested scope." },
        },
        stale: {
          items: [],
          summary: { limit: 5, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No stale items in the requested scope." },
        },
        next_action: {
          items: [],
          summary: { limit: 5, returned_count: 0, total_count: 0, order: ["created_at_desc", "id_desc"] },
          empty_state: { is_empty: true, message: "No next-action items in the requested scope." },
        },
        sources: ["continuity_capture_events", "continuity_objects", "continuity_correction_events"],
      },
    });
    getThreadHealthDashboardMock.mockResolvedValue({
      dashboard: {
        posture: "critical",
        total_thread_count: 2,
        recent_thread_count: 1,
        stale_thread_count: 0,
        risky_thread_count: 1,
        watch_thread_count: 0,
        thresholds: {
          recent_window_hours: 24,
          stale_window_hours: 72,
          risky_score_threshold: 2,
        },
        recent_threads: [
          {
            thread: {
              id: "thread-live-1",
              title: "Launch thread",
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
            hours_since_last_activity: 2,
            conversation_event_count: 3,
            operational_event_count: 1,
            active_session_count: 1,
            open_loop_count: 0,
            stale_open_loop_count: 0,
            unresolved_contradiction_count: 0,
            weak_trust_signal_count: 0,
            reasons: ["No active contradiction, stale open-loop, or weak-trust pressure is currently visible."],
            recommended_action: "No immediate intervention required.",
          },
        ],
        stale_threads: [],
        risky_threads: [
          {
            thread: {
              id: "thread-live-2",
              title: "Conflict thread",
              agent_profile_id: "assistant_default",
              created_at: "2026-03-28T09:00:00Z",
              updated_at: "2026-03-28T09:20:00Z",
            },
            health_posture: "critical",
            activity_posture: "current",
            risk_posture: "risky",
            risk_score: 2,
            last_activity_at: "2026-03-28T09:20:00Z",
            last_conversation_at: "2026-03-28T09:20:00Z",
            hours_since_last_activity: 26,
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
      },
    });
    listContinuityReviewQueueMock.mockResolvedValue({
      items: [
        {
          id: "object-live-1",
          capture_event_id: "capture-live-1",
          object_type: "Decision",
          status: "active",
          title: "Decision: Keep admission conservative",
          body: {
            decision_text: "Keep admission conservative",
          },
          provenance: {
            capture_event_id: "capture-live-1",
          },
          confidence: 0.95,
          last_confirmed_at: "2026-03-29T09:20:00Z",
          supersedes_object_id: null,
          superseded_by_object_id: null,
          created_at: "2026-03-29T09:20:00Z",
          updated_at: "2026-03-29T09:20:00Z",
        },
      ],
      summary: {
        status: "correction_ready",
        limit: 20,
        returned_count: 1,
        total_count: 1,
        order: ["updated_at_desc", "created_at_desc", "id_desc"],
      },
    });
    getContinuityReviewDetailMock.mockResolvedValue({
      review: {
        continuity_object: {
          id: "object-live-1",
          capture_event_id: "capture-live-1",
          object_type: "Decision",
          status: "active",
          title: "Decision: Keep admission conservative",
          body: { decision_text: "Keep admission conservative" },
          provenance: { capture_event_id: "capture-live-1" },
          confidence: 0.95,
          last_confirmed_at: "2026-03-29T09:20:00Z",
          supersedes_object_id: null,
          superseded_by_object_id: null,
          created_at: "2026-03-29T09:20:00Z",
          updated_at: "2026-03-29T09:20:00Z",
        },
        correction_events: [],
        supersession_chain: {
          supersedes: null,
          superseded_by: null,
        },
      },
    });

    render(await ContinuityPage({ searchParams: Promise.resolve({ capture: "capture-live-1", recall_query: "decision" }) }));

    expect(screen.getByText("Live API")).toBeInTheDocument();
    expect(screen.getByText("Live inbox")).toBeInTheDocument();
    expect(screen.getByText("Live recall")).toBeInTheDocument();
    expect(screen.getByText("Live brief")).toBeInTheDocument();
    expect(screen.getByText("Live open loops")).toBeInTheDocument();
    expect(screen.getByText("Live daily brief")).toBeInTheDocument();
    expect(screen.getByText("Live weekly review")).toBeInTheDocument();
    expect(screen.getByText("Live thread dashboard")).toBeInTheDocument();
    expect(screen.getByText("Live review queue")).toBeInTheDocument();
    expect(screen.getByText("Conflict thread")).toBeInTheDocument();
    expect(screen.getAllByText("Decision: Keep admission conservative").length).toBeGreaterThan(0);

    expect(listContinuityCapturesMock).toHaveBeenCalledWith("https://api.example.com", "user-1", {
      limit: 20,
    });
    expect(getContinuityCaptureDetailMock).toHaveBeenCalledWith(
      "https://api.example.com",
      "capture-live-1",
      "user-1",
    );
    expect(queryContinuityRecallMock).toHaveBeenCalledWith("https://api.example.com", "user-1", {
      query: "decision",
      threadId: "",
      taskId: "",
      project: "",
      person: "",
      since: "",
      until: "",
      limit: 20,
    });
    expect(listContinuityReviewQueueMock).toHaveBeenCalledWith("https://api.example.com", "user-1", {
      status: "correction_ready",
      limit: 20,
    });
    expect(getContinuityReviewDetailMock).toHaveBeenCalledWith(
      "https://api.example.com",
      "object-live-1",
      "user-1",
    );
    expect(getContinuityResumptionBriefMock).toHaveBeenCalledWith("https://api.example.com", "user-1", {
      query: "decision",
      threadId: "",
      taskId: "",
      project: "",
      person: "",
      since: "",
      until: "",
      maxRecentChanges: 5,
      maxOpenLoops: 5,
    });
    expect(getContinuityOpenLoopDashboardMock).toHaveBeenCalledWith(
      "https://api.example.com",
      "user-1",
      {
        query: "decision",
        threadId: "",
        taskId: "",
        project: "",
        person: "",
        since: "",
        until: "",
        limit: 20,
      },
    );
    expect(getContinuityDailyBriefMock).toHaveBeenCalledWith("https://api.example.com", "user-1", {
      query: "decision",
      threadId: "",
      taskId: "",
      project: "",
      person: "",
      since: "",
      until: "",
      limit: 3,
    });
    expect(getContinuityWeeklyReviewMock).toHaveBeenCalledWith("https://api.example.com", "user-1", {
      query: "decision",
      threadId: "",
      taskId: "",
      project: "",
      person: "",
      since: "",
      until: "",
      limit: 5,
    });
    expect(getThreadHealthDashboardMock).toHaveBeenCalledWith("https://api.example.com", "user-1");
  });

  it("renders configured-live fallback objects as non-actionable after read outages", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "http://127.0.0.1:9",
      userId: "user-1",
      defaultThreadId: "thread-1",
      defaultToolId: "tool-1",
    });
    hasLiveApiConfigMock.mockReturnValue(true);
    for (const read of [
      listContinuityCapturesMock,
      queryContinuityRecallMock,
      getContinuityResumptionBriefMock,
      getContinuityOpenLoopDashboardMock,
      getContinuityDailyBriefMock,
      getContinuityWeeklyReviewMock,
      getThreadHealthDashboardMock,
      listContinuityReviewQueueMock,
    ]) {
      read.mockRejectedValue(new Error("configured live read unavailable"));
    }

    render(await ContinuityPage({ searchParams: Promise.resolve({}) }));

    expect(screen.getByText("Open-loop dashboard unavailable")).toBeInTheDocument();
    expect(screen.getByText("Review queue unavailable")).toBeInTheDocument();
    expect(screen.getByText("Correction unavailable")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: "Done" })) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "Apply correction" })).toBeDisabled();
    expect(screen.getAllByText(/fallback open loops are read-only/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/fallback review objects are read-only/i).length).toBeGreaterThan(0);
  });

  it("keeps a live queue target actionable when only its detail read fails", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "https://api.example.com",
      userId: "user-1",
      defaultThreadId: "thread-1",
      defaultToolId: "tool-1",
    });
    hasLiveApiConfigMock.mockReturnValue(true);
    for (const read of [
      listContinuityCapturesMock,
      queryContinuityRecallMock,
      getContinuityResumptionBriefMock,
      getContinuityOpenLoopDashboardMock,
      getContinuityDailyBriefMock,
      getContinuityWeeklyReviewMock,
      getThreadHealthDashboardMock,
    ]) {
      read.mockRejectedValue(new Error("unrelated live read unavailable"));
    }
    listContinuityReviewQueueMock.mockResolvedValue({
      items: [
        {
          id: "object-live-detail-outage",
          capture_event_id: "capture-live-detail-outage",
          object_type: "Decision",
          status: "active",
          title: "Decision: Live queue target remains correction-ready",
          body: { decision_text: "Live queue target remains correction-ready" },
          provenance: { capture_event_id: "capture-live-detail-outage" },
          confidence: 0.9,
          last_confirmed_at: null,
          supersedes_object_id: null,
          superseded_by_object_id: null,
          created_at: "2026-07-13T10:00:00Z",
          updated_at: "2026-07-13T10:00:00Z",
        },
      ],
      summary: {
        status: "correction_ready",
        limit: 20,
        returned_count: 1,
        total_count: 1,
        order: ["updated_at_desc", "created_at_desc", "id_desc"],
      },
    });
    getContinuityReviewDetailMock.mockRejectedValue(new Error("detail history unavailable"));

    render(await ContinuityPage({ searchParams: Promise.resolve({}) }));

    expect(screen.getByText("Live review queue")).toBeInTheDocument();
    expect(screen.getByText("Live target · detail unavailable")).toBeInTheDocument();
    expect(screen.getByText(/detail history unavailable/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply correction" })).toBeEnabled();
  });

  it("starts review detail as soon as the review queue resolves", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "https://api.example.com",
      userId: "user-1",
      defaultThreadId: "thread-1",
      defaultToolId: "tool-1",
    });
    hasLiveApiConfigMock.mockReturnValue(true);

    let releaseCaptures = () => {};
    listContinuityCapturesMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          releaseCaptures = () =>
            resolve({
              items: [],
              summary: {
                limit: 20,
                returned_count: 0,
                total_count: 0,
                derived_count: 0,
                triage_count: 0,
                order: ["created_at_desc", "id_desc"],
              },
            });
        }),
    );
    for (const read of [
      queryContinuityRecallMock,
      getContinuityResumptionBriefMock,
      getContinuityOpenLoopDashboardMock,
      getContinuityDailyBriefMock,
      getContinuityWeeklyReviewMock,
      getThreadHealthDashboardMock,
    ]) {
      read.mockRejectedValue(new Error("unrelated read unavailable"));
    }

    const reviewObject = {
      id: "object-live-review",
      capture_event_id: "capture-live-review",
      object_type: "Decision" as const,
      status: "active",
      title: "Decision: Start detail without unrelated panels",
      body: { decision_text: "Start detail without unrelated panels" },
      provenance: { capture_event_id: "capture-live-review" },
      confidence: 0.9,
      last_confirmed_at: null,
      supersedes_object_id: null,
      superseded_by_object_id: null,
      created_at: "2026-07-13T10:00:00Z",
      updated_at: "2026-07-13T10:00:00Z",
    };
    listContinuityReviewQueueMock.mockResolvedValue({
      items: [reviewObject],
      summary: {
        status: "correction_ready",
        limit: 20,
        returned_count: 1,
        total_count: 1,
        order: ["updated_at_desc", "created_at_desc", "id_desc"],
      },
    });
    getContinuityReviewDetailMock.mockResolvedValue({
      review: {
        continuity_object: reviewObject,
        correction_events: [],
        supersession_chain: { supersedes: null, superseded_by: null },
      },
    });

    const pagePromise = ContinuityPage({ searchParams: Promise.resolve({}) });

    await vi.waitFor(() => {
      expect(getContinuityReviewDetailMock).toHaveBeenCalledWith(
        "https://api.example.com",
        "object-live-review",
        "user-1",
      );
    });
    // The page is still blocked on captures here; detail already started from
    // its parent queue promise rather than waiting behind that unrelated read.
    releaseCaptures();
    await pagePromise;
  });

  it("shows what changed from the route when the page is asked", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "",
      userId: "",
      defaultThreadId: "",
      defaultToolId: "",
    });
    hasLiveApiConfigMock.mockReturnValue(false);
    render(await ContinuityPage({ searchParams: Promise.resolve({ what_changed: "Keel price" }) }));
    expect(screen.getByText("What changed")).toBeInTheDocument();
    expect(screen.getByText("This view calls POST /v0/vnext/changes. The API is not configured.")).toBeInTheDocument();
    expect(postWhatChangedMock).not.toHaveBeenCalled();
  });

  it("renders the route answer and does not keep a second history", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "https://api.example.com",
      userId: "user-1",
      defaultThreadId: "",
      defaultToolId: "",
    });
    hasLiveApiConfigMock.mockReturnValue(true);
    for (const mock of [
      listContinuityCapturesMock,
      queryContinuityRecallMock,
      getContinuityResumptionBriefMock,
      getContinuityOpenLoopDashboardMock,
      getContinuityDailyBriefMock,
      getContinuityWeeklyReviewMock,
      getThreadHealthDashboardMock,
      listContinuityReviewQueueMock,
    ]) {
      mock.mockRejectedValue(new Error("offline"));
    }
    postWhatChangedMock.mockResolvedValue({
      notice: "This covers the recorded decisions the caller may read. It is not a company-wide statement.",
      topic: "Keel price",
      chains: [
        {
          start: "memory-1",
          current: {
            id: "memory-1",
            kind: "memory",
            chain: "memory-1",
            date: "2025-11-18",
            change: "the list price was cut to 520",
            reason: null,
            replaced: null,
            disagrees_with: null,
            sources: [],
            author: "showcase-sales",
            review_state: "active",
          },
          upcoming: [],
          disputed: [],
          history: [
            {
              id: "memory-1",
              kind: "memory",
              chain: "memory-1",
              date: "2025-11-18",
              change: "the list price was cut to 520",
              reason: null,
              replaced: null,
              disagrees_with: null,
              sources: [],
              author: "showcase-sales",
              review_state: "active",
            },
          ],
        },
      ],
    });
    render(await ContinuityPage({ searchParams: Promise.resolve({ what_changed: "Keel price" }) }));
    expect(postWhatChangedMock).toHaveBeenCalledWith("https://api.example.com", "user-1", "Keel price");
    expect(screen.getAllByText(/the list price was cut to 520/).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Topic")).toHaveValue("Keel price");
  });

  it("shows reasons, sources, upcoming, and disputed from the route", async () => {
    getApiConfigMock.mockReturnValue({
      apiBaseUrl: "https://api.example.com",
      userId: "user-1",
      defaultThreadId: "",
      defaultToolId: "",
    });
    hasLiveApiConfigMock.mockReturnValue(true);
    for (const mock of [
      listContinuityCapturesMock,
      queryContinuityRecallMock,
      getContinuityResumptionBriefMock,
      getContinuityOpenLoopDashboardMock,
      getContinuityDailyBriefMock,
      getContinuityWeeklyReviewMock,
      getThreadHealthDashboardMock,
      listContinuityReviewQueueMock,
    ]) {
      mock.mockRejectedValue(new Error("offline"));
    }
    postWhatChangedMock.mockResolvedValue({
      notice: "This covers the recorded decisions the caller may read. It is not a company-wide statement.",
      topic: "lantern policy",
      chains: [
        {
          start: "memory-now",
          current: {
            id: "memory-now",
            kind: "memory",
            chain: "memory-now",
            date: "2026-03-01",
            change: "the lantern stays lit",
            reason: { text: "the recorded reason", source_id: "source-1" },
            replaced: "memory-old",
            disagrees_with: null,
            sources: [{ source_id: "source-1", title: "November price note", date: "2026-03-01" }],
            author: "showcase-sales",
            review_state: "active",
          },
          upcoming: ["memory-later"],
          disputed: ["memory-dispute"],
          history: [
            {
              id: "memory-now",
              kind: "memory",
              chain: "memory-now",
              date: "2026-03-01",
              change: "the lantern stays lit",
              reason: { text: "the recorded reason", source_id: "source-1" },
              replaced: null,
              disagrees_with: null,
              sources: [{ source_id: "source-1", title: "November price note", date: "2026-03-01" }],
              author: "showcase-sales",
              review_state: "active",
            },
            {
              id: "memory-later",
              kind: "memory",
              chain: "memory-now",
              date: "2026-11-01",
              change: "the later lantern rule",
              reason: null,
              replaced: null,
              disagrees_with: null,
              sources: [],
              author: "showcase-sales",
              review_state: "active",
            },
            {
              id: "memory-dispute",
              kind: "memory",
              chain: "memory-now",
              date: "2026-02-01",
              change: "the disputed lantern rule",
              reason: null,
              replaced: null,
              disagrees_with: "memory-now",
              sources: [],
              author: "showcase-sales",
              review_state: "active",
            },
          ],
        },
      ],
    });
    render(await ContinuityPage({ searchParams: Promise.resolve({ what_changed: "lantern policy" }) }));
    expect(screen.getAllByText("Reason: the recorded reason").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Source: November price note 2026-03-01").length).toBeGreaterThan(0);
    const upcoming = screen.getByRole("heading", { name: "Upcoming" }).closest("section");
    const disputed = screen.getByRole("heading", { name: "Disputed" }).closest("section");
    expect(upcoming).not.toBeNull();
    expect(disputed).not.toBeNull();
    expect(upcoming).toHaveTextContent("the later lantern rule");
    expect(disputed).toHaveTextContent("the disputed lantern rule");
    expect(screen.queryByText(/gap/i)).not.toBeInTheDocument();
  });
});
