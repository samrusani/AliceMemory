from __future__ import annotations

import sys as _sys
import typing as _typing
import types as _types

from . import agents as _agents
from . import arguments as _arguments
from . import automation as _automation
from . import capture as _capture
from . import constants as _constants
from . import context as _context
from . import continuity as _continuity
from . import errors as _errors
from . import evals as _evals
from . import memories as _memories
from . import models as _models
from . import parser as _parser
from . import runner as _runner
from . import scheduler as _scheduler
from . import shared as _shared
from . import smokes as _smokes
from .parser import build_parser as build_parser
from .runner import main as main

_CARRIER_MODULES = (
    _constants,
    _errors,
    _models,
    _arguments,
    _shared,
    _capture,
    _context,
    _memories,
    _agents,
    _scheduler,
    _smokes,
    _automation,
    _continuity,
    _evals,
    _parser,
    _runner,
)

for _carrier in _CARRIER_MODULES:
    for _name, _value in vars(_carrier).items():
        if not _name.startswith("__"):
            globals().setdefault(_name, _value)

for _carrier in _CARRIER_MODULES:
    globals().pop(_carrier.__name__.rsplit(".", maxsplit=1)[-1], None)

# The owner label commands do not extend the compatibility facade.
globals().pop("labels", None)

_PUBLIC_NAME_ORDER = "annotations argparse Iterator Sequence contextmanager redirect_stderr dataclass UTC datetime StringIO json logging os Path sys tempfile time TypedDict cast URLError Request urlopen UUID uuid4 psycopg format_artifact_detail_output format_capture_output format_continuity_brief_output format_contradiction_case_detail_output format_contradiction_case_list_output format_contradiction_sync_output format_explain_output format_lifecycle_detail_output format_lifecycle_list_output format_memory_operation_candidates_output format_memory_operation_commit_output format_memory_operations_output format_open_loops_output format_recall_output format_resume_output format_review_apply_output format_review_detail_output format_review_queue_output format_status_output format_task_brief_comparison_output format_task_brief_output format_temporal_explain_output format_temporal_state_output format_temporal_timeline_output format_trust_signals_output format_trusted_fact_pattern_explain_output format_trusted_fact_pattern_list_output format_trusted_fact_playbook_explain_output format_trusted_fact_playbook_list_output Settings get_runtime_settings get_settings ContinuityCaptureValidationError capture_continuity_input ContinuityBriefValidationError compile_continuity_brief ContinuityEvidenceNotFoundError build_continuity_explain get_continuity_artifact_detail ContinuityContradictionNotFoundError ContinuityContradictionValidationError get_contradiction_case list_contradiction_cases resolve_contradiction_case sync_contradictions MemoryMutationValidationError commit_memory_operations generate_memory_operation_candidates list_memory_operation_candidates list_memory_operations default_continuity_promotable default_continuity_searchable ContinuityLifecycleNotFoundError ContinuityLifecycleValidationError get_continuity_lifecycle_state list_continuity_lifecycle_state ContinuityOpenLoopValidationError compile_continuity_open_loop_dashboard get_thread_health_dashboard ContinuityRecallValidationError query_continuity_recall ContinuityResumptionValidationError compile_continuity_resumption_brief ContinuityReviewNotFoundError ContinuityReviewValidationError apply_continuity_correction get_continuity_review_detail list_continuity_review_queue list_trust_signals get_memory_hygiene_dashboard_summary CONTINUITY_CAPTURE_EXPLICIT_SIGNALS CONTINUITY_CORRECTION_ACTIONS CONTRADICTION_RESOLUTION_ACTIONS CONTINUITY_BRIEF_TYPE_ORDER DEFAULT_CONTINUITY_CAPTURE_LIMIT DEFAULT_CONTINUITY_BRIEF_CONFLICT_LIMIT DEFAULT_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT DEFAULT_CONTINUITY_BRIEF_TIMELINE_LIMIT DEFAULT_CONTINUITY_LIFECYCLE_LIMIT DEFAULT_CONTINUITY_OPEN_LOOP_LIMIT DEFAULT_CONTINUITY_RECALL_LIMIT DEFAULT_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT DEFAULT_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT DEFAULT_CONTINUITY_REVIEW_LIMIT DEFAULT_TEMPORAL_TIMELINE_LIMIT DEFAULT_TASK_BRIEF_TOKEN_BUDGET DEFAULT_TRUSTED_FACT_PROMOTION_LIMIT MAX_CONTINUITY_REVIEW_LIMIT MAX_CONTINUITY_OPEN_LOOP_LIMIT MAX_CONTINUITY_RECALL_LIMIT MAX_CONTINUITY_BRIEF_CONFLICT_LIMIT MAX_CONTINUITY_BRIEF_RELEVANT_FACT_LIMIT MAX_CONTINUITY_BRIEF_TIMELINE_LIMIT MAX_CONTINUITY_LIFECYCLE_LIMIT MAX_CONTINUITY_RESUMPTION_OPEN_LOOP_LIMIT MAX_CONTINUITY_RESUMPTION_RECENT_CHANGES_LIMIT MAX_TASK_BRIEF_TOKEN_BUDGET MAX_TEMPORAL_TIMELINE_LIMIT MAX_TRUSTED_FACT_PROMOTION_LIMIT ContradictionCaseListQueryInput ContradictionResolveInput ContradictionSyncInput ContinuityCaptureCreateInput ContinuityBriefRequestInput ContinuityCorrectionInput ContinuityLifecycleQueryInput ContinuityOpenLoopDashboardQueryInput ContinuityRecallQueryInput ContinuityResumptionBriefRequestInput ContinuityReviewQueueQueryInput MemoryOperationCommitInput MemoryOperationGenerateInput MemoryOperationListInput TaskBriefCompileRequestInput TemporalExplainQueryInput TemporalStateAtQueryInput TemporalTimelineQueryInput TrustSignalListQueryInput TrustedFactPatternListQueryInput TrustedFactPlaybookListQueryInput TaskBriefNotFoundError TaskBriefValidationError compare_task_briefs compile_and_persist_task_brief get_persisted_task_brief ping_database user_connection get_public_eval_run list_public_eval_runs list_public_eval_suites run_public_evals write_public_eval_report get_retrieval_evaluation_summary ContinuityStore ContinuityJsonObject legacy_surfaces_enabled TemporalStateValidationError get_temporal_explain get_temporal_state_at get_temporal_timeline TrustedFactPromotionNotFoundError get_trusted_fact_pattern get_trusted_fact_playbook list_trusted_fact_patterns list_trusted_fact_playbooks PERMISSION_PROFILES AgentIdentity PolicyDecision agent_metadata append_policy_events ensure_policy_allowed evaluate_agent_policy summarize_agent_policy_telemetry AgentKeyValidationError create_agent_key dispatch_vnext_artifact_review VNextCaptureService VNextCaptureValidationError BrainArtifactRequest VNextBrainService VNextBrainValidationError ConnectionFinderRequest VNextConnectionService VNextConnectionValidationError VNextConnectorService VNextConnectorValidationError list_connector_definitions load_connector_items_from_file scan_local_folder ContextTreeRequest VNextContextTreeService VNextContextTreeStore ContradictionFinderRequest VNextContradictionService VNextContradictionValidationError VNextDogfoodingService LOCAL_VNEXT_FRONTEND_ORIGINS VNextDoctorService local_live_cors_status VNEXT_EVAL_SUITE_ORDER run_vnext_evals write_vnext_benchmark_corpus write_vnext_eval_report ProjectAutomationRequest VNextProjectService VNextProjectValidationError QueueTaskRequest VNextQueueService VNextQueueValidationError JsonObject BUDGET_STRATEGIES CONTEXT_DEPTHS VNextRetrievalRequest VNextRetrievalService VNextRetrievalStore VNextRetrievalValidationError SchedulerRunRequest VNextSchedulerService VNextSchedulerStore VNextSchedulerValidationError WORKFLOW_TYPES default_schedule DEFAULT_LOG_FILE DEFAULT_PID_FILE DEFAULT_STATUS_FILE SchedulerRuntimeConfig daemon_status run_due_workflows_durable run_foreground_daemon run_now_durable start_background_daemon stop_daemon DeferredMemoryEmbedding EMBEDDINGS_API_KEY_ENV EMBEDDINGS_BASE_URL_ENV EMBEDDINGS_MODEL_ENV EMBEDDING_SIGNATURE_VERSION MAX_EMBEDDINGS_BATCH_SIZE endpoint_fingerprint get_embedding_provider memory_embedding_text persist_deferred_memory_embeddings_best_effort append_event json_safe redact_memory_flow VNextMemoryCommitService VNextMemoryCommitValidationError memory_commit_request_from_payload InMemorySecretProvider PostgresVNextStore DEFAULT_CLI_USER_ID DEFAULT_VNEXT_SENSITIVITY_ALLOWED MAINTENANCE_REPORT_PATH_ENV DEFAULT_MAINTENANCE_REPORT_PATH DEFAULT_VNEXT_DEMO_DATASET_PATH REVIEW_STATUS_CHOICES DEMO_SECRET_MARKERS logger EvalGateFailure EmbeddingBackfillFailure PartialCommandFailure CLIContext ModelGenerationKwargs build_parser main".split()
_public_values = {name: globals().pop(name) for name in _PUBLIC_NAME_ORDER}
globals().update(_public_values)

for _annotation in _models.ModelGenerationKwargs.__annotations__.values():
    if isinstance(_annotation, _typing.ForwardRef):
        _annotation.__forward_module__ = __name__

for _value in tuple(globals().values()):
    if getattr(_value, "__module__", "").startswith(f"{__name__}."):
        if isinstance(_value, type):
            for _member in vars(_value).values():
                if getattr(_member, "__module__", "").startswith(f"{__name__}."):
                    _member.__module__ = __name__
        _value.__module__ = __name__


class _CLICompatModule(_types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for carrier in _CARRIER_MODULES:
            if name in carrier.__dict__:
                setattr(carrier, name, value)


_sys.modules[__name__].__class__ = _CLICompatModule

__all__ = ["build_parser", "main"]
