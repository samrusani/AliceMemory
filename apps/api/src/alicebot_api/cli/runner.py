from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
import psycopg
from alicebot_api.continuity_capture import ContinuityCaptureValidationError
from alicebot_api.continuity_brief import ContinuityBriefValidationError
from alicebot_api.continuity_evidence import ContinuityEvidenceNotFoundError
from alicebot_api.continuity_contradictions import (
    ContinuityContradictionNotFoundError,
    ContinuityContradictionValidationError,
)
from alicebot_api.memory_mutations import MemoryMutationValidationError
from alicebot_api.continuity_lifecycle import ContinuityLifecycleNotFoundError, ContinuityLifecycleValidationError
from alicebot_api.continuity_open_loops import ContinuityOpenLoopValidationError
from alicebot_api.continuity_recall import ContinuityRecallValidationError
from alicebot_api.continuity_resumption import ContinuityResumptionValidationError
from alicebot_api.continuity_review import ContinuityReviewNotFoundError, ContinuityReviewValidationError
from alicebot_api.task_briefing import TaskBriefNotFoundError, TaskBriefValidationError
from alicebot_api.temporal_state import TemporalStateValidationError
from alicebot_api.trusted_fact_promotions import TrustedFactPromotionNotFoundError
from alicebot_api.vnext_capture import VNextCaptureValidationError
from alicebot_api.vnext_brain import VNextBrainValidationError
from alicebot_api.vnext_connections import VNextConnectionValidationError
from alicebot_api.vnext_connectors import VNextConnectorValidationError
from alicebot_api.vnext_contradictions import VNextContradictionValidationError
from alicebot_api.vnext_projects import VNextProjectValidationError
from alicebot_api.vnext_queue import VNextQueueValidationError
from alicebot_api.vnext_retrieval import VNextRetrievalValidationError
from alicebot_api.vnext_scheduler import VNextSchedulerValidationError
from .constants import (
    _CLI_COMMAND_FAILED,
    _CLI_DATABASE_FAILED,
    _CLI_FILESYSTEM_FAILED,
    _CLI_INVALID_REQUEST,
    _CLI_NOT_FOUND,
    _CLI_SQLITE_IMPORT,
    logger,
)
from .errors import EmbeddingBackfillFailure, EvalGateFailure, PartialCommandFailure, _emit_cli_error
from .arguments import _validate_arguments
from .parser import build_parser
from .shared import _SqliteImportCommandError, _build_context


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser_stderr = StringIO()
    try:
        with redirect_stderr(parser_stderr):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        logger.debug("CLI argument parsing failed: %s", parser_stderr.getvalue().strip())
        _emit_cli_error(code=_CLI_INVALID_REQUEST[0], message=_CLI_INVALID_REQUEST[1])
        return int(exc.code) if isinstance(exc.code, int) else 2

    try:
        _validate_arguments(args)
        ctx = _build_context(args)
        handler = args.handler
        output = handler(ctx, args)
    except (
        ValueError,
        OSError,
        psycopg.Error,
        ContinuityCaptureValidationError,
        VNextCaptureValidationError,
        VNextBrainValidationError,
        VNextConnectionValidationError,
        VNextConnectorValidationError,
        VNextContradictionValidationError,
        VNextProjectValidationError,
        VNextQueueValidationError,
        VNextRetrievalValidationError,
        VNextSchedulerValidationError,
        RuntimeError,
        ContinuityLifecycleValidationError,
        ContinuityLifecycleNotFoundError,
        ContinuityRecallValidationError,
        ContinuityBriefValidationError,
        ContinuityResumptionValidationError,
        ContinuityOpenLoopValidationError,
        ContinuityReviewValidationError,
        ContinuityReviewNotFoundError,
        ContinuityContradictionValidationError,
        ContinuityContradictionNotFoundError,
        ContinuityEvidenceNotFoundError,
        MemoryMutationValidationError,
        TaskBriefNotFoundError,
        TaskBriefValidationError,
        TemporalStateValidationError,
        TrustedFactPromotionNotFoundError,
    ) as exc:
        not_found_errors = (
            ContinuityLifecycleNotFoundError,
            ContinuityReviewNotFoundError,
            ContinuityContradictionNotFoundError,
            ContinuityEvidenceNotFoundError,
            TaskBriefNotFoundError,
            TrustedFactPromotionNotFoundError,
        )
        invalid_request_errors = (
            ValueError,
            ContinuityCaptureValidationError,
            VNextCaptureValidationError,
            VNextBrainValidationError,
            VNextConnectionValidationError,
            VNextConnectorValidationError,
            VNextContradictionValidationError,
            VNextProjectValidationError,
            VNextQueueValidationError,
            VNextRetrievalValidationError,
            VNextSchedulerValidationError,
            ContinuityLifecycleValidationError,
            ContinuityRecallValidationError,
            ContinuityBriefValidationError,
            ContinuityResumptionValidationError,
            ContinuityOpenLoopValidationError,
            ContinuityReviewValidationError,
            ContinuityContradictionValidationError,
            MemoryMutationValidationError,
            TaskBriefValidationError,
            TemporalStateValidationError,
        )
        exit_code = 1
        if isinstance(exc, _SqliteImportCommandError):
            code, message = _CLI_SQLITE_IMPORT
            # invalid_request from argument parsing exits 2. This refusal is
            # the same class of bad request.
            exit_code = 2
        elif isinstance(exc, not_found_errors):
            code, message = _CLI_NOT_FOUND
        elif isinstance(exc, invalid_request_errors):
            code, message = _CLI_INVALID_REQUEST
        elif isinstance(exc, psycopg.Error):
            code, message = _CLI_DATABASE_FAILED
        elif isinstance(exc, OSError):
            code, message = _CLI_FILESYSTEM_FAILED
        else:
            code, message = _CLI_COMMAND_FAILED
        logger.debug(
            "CLI command failed public_code=%s",
            code,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_cli_error(code=code, message=message)
        return exit_code
    except EvalGateFailure as exc:
        # Honor the JSON output contract (report to stdout) while signaling a
        # nonzero exit for a failing / not-fully-passing eval report.
        print(exc.output)
        return 1
    except EmbeddingBackfillFailure as exc:
        print(exc.output)
        return 1
    except PartialCommandFailure as exc:
        print(exc.output)
        return 1
    except Exception as exc:  # pragma: no cover - boundary fail-closed backstop
        logger.debug(
            "Unhandled CLI command failure",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        _emit_cli_error(code=_CLI_COMMAND_FAILED[0], message=_CLI_COMMAND_FAILED[1])
        return 1

    print(output)
    return 0
