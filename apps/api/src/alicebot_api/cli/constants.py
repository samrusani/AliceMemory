from __future__ import annotations

import logging
from pathlib import Path

DEFAULT_CLI_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_VNEXT_SENSITIVITY_ALLOWED = ("public", "internal", "private", "unknown")
MAINTENANCE_REPORT_PATH_ENV = "ALICEBOT_MAINTENANCE_REPORT_PATH"
DEFAULT_MAINTENANCE_REPORT_PATH = (
    Path(__file__).resolve().parents[5] / "artifacts" / "ops" / "maintenance_status_latest.json"
)
DEFAULT_VNEXT_DEMO_DATASET_PATH = Path(__file__).resolve().parents[5] / "fixtures" / "vnext" / "demo_dataset.json"
REVIEW_STATUS_CHOICES = ("correction_ready", "active", "stale", "superseded", "deleted", "all")
DEMO_SECRET_MARKERS = ("sk-", "xoxb-", "ghp_", "password", "access_token", "refresh_token", "@gmail.com")

logger = logging.getLogger("alicebot_api.cli")

_CLI_INVALID_REQUEST = ("invalid_request", "The command request is invalid")
_CLI_SQLITE_IMPORT = (
    "sqlite_import_use_alice_memory",
    "A SQLite URL cannot run alicebot vnext sources import. "
    "Use alice-memory import-markdown --from PATH or alice-memory import-chatgpt --from PATH.",
)
_CLI_NOT_FOUND = ("not_found", "The requested resource was not found")
_CLI_DATABASE_FAILED = ("database_operation_failed", "The database operation failed")
_CLI_FILESYSTEM_FAILED = ("filesystem_operation_failed", "The filesystem operation failed")
_CLI_COMMAND_FAILED = ("command_failed", "The command could not be completed")
