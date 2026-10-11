from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
from collections.abc import Iterator
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Callable
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from alicebot_api.db import user_connection
from alicebot_api.vnext_agent_control import AgentIdentity, PolicyDecision
from alicebot_api.vnext_event_log import append_event
from alicebot_api.vnext_repositories import JsonObject
from alicebot_api.vnext_scheduler import (
    SchedulerRunRequest,
    SCHEDULER_WORKFLOW_ERROR_CODE,
    SCHEDULER_WORKFLOW_ERROR_MESSAGE,
    SchedulerWorkflowPlan,
    VNextSchedulerService,
    VNextSchedulerValidationError,
    withhold_unreadable_artifact,
)
from alicebot_api.vnext_store import PostgresVNextStore


DEFAULT_RUNTIME_DIR = Path("~/.alicebot/vnext-scheduler").expanduser()
DEFAULT_PID_FILE = DEFAULT_RUNTIME_DIR / "scheduler.pid"
DEFAULT_STATUS_FILE = DEFAULT_RUNTIME_DIR / "scheduler-status.json"
DEFAULT_LOG_FILE = DEFAULT_RUNTIME_DIR / "scheduler.log"
INSTANCE_TOKEN_ENV = "ALICEBOT_SCHEDULER_INSTANCE_TOKEN"
DEFAULT_CLAIM_LEASE_SECONDS = 900.0
SCHEDULER_CLAIM_LOST_ERROR_CODE = "scheduler_claim_lost"
SCHEDULER_CLAIM_LOST_ERROR_MESSAGE = "Scheduler claim was lost before completion"
SCHEDULER_CLAIM_SUPERSEDED_ERROR_CODE = "scheduler_claim_superseded"
SCHEDULER_CLAIM_SUPERSEDED_ERROR_MESSAGE = "Scheduler claim was superseded before completion"
SCHEDULER_SCAN_ERROR_CODE = "scheduler_scan_failed"
SCHEDULER_SCAN_ERROR_MESSAGE = "Scheduler scan failed"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerRuntimeConfig:
    database_url: str
    user_id: UUID
    interval_seconds: float = 60.0
    limit: int = 10
    pid_file: Path = DEFAULT_PID_FILE
    status_file: Path = DEFAULT_STATUS_FILE
    log_file: Path = DEFAULT_LOG_FILE
    once: bool = False
    claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_safe(child) for child in value]
    if isinstance(value, tuple):
        return [_json_safe(child) for child in value]
    if isinstance(value, (datetime, Path, UUID)):
        return str(value)
    return value


def _write_json(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> JsonObject | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_identity(pid: int) -> str | None:
    """Stable process-birth fingerprint used to reject a reused PID.

    Linux exposes the kernel start tick. macOS and other POSIX platforms use
    ``ps`` start time plus command, stored only as a hash so database URLs in
    the daemon command never land in status files.
    """
    if not _pid_running(pid):
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
        closing = raw.rfind(")")
        start_ticks = raw[closing + 2 :].split()[19]
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            boot_id = "unknown-boot"
        return f"linux:{boot_id}:{start_ticks}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    identity_material = result.stdout.strip()
    if result.returncode != 0 or not identity_material:
        return None
    return "ps-sha256:" + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()


def _read_pid_record(path: Path) -> JsonObject | None:
    record = _read_json(path)
    if record is not None:
        return record
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    # Legacy records intentionally carry no ownership identity. They can be
    # observed as stale, but stop_daemon will never signal them blindly.
    return {"pid": pid, "legacy": True}


def _write_pid_record(path: Path, *, pid: int, instance_token: str, process_identity: str) -> None:
    _write_json(
        path,
        {
            "pid": pid,
            "instance_token": instance_token,
            "process_identity": process_identity,
        },
    )


def _owner_file(pid_file: Path) -> Path:
    return pid_file.with_name(f"{pid_file.name}.owner")


@contextmanager
def _lease_guard(pid_file: Path) -> Iterator[None]:
    """Serialize cross-process owner creation and stale-owner reclamation."""
    guard = pid_file.with_name(f"{pid_file.name}.owner.lock")
    guard.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_exclusive_json(path: Path, payload: JsonObject) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(_json_safe(payload), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _lease_is_stale(record: JsonObject) -> bool:
    """Only structurally verifiable, no-longer-matching owners are stale."""
    pid = record.get("pid")
    expected = record.get("process_identity")
    token = record.get("instance_token")
    if not isinstance(pid, int) or not isinstance(expected, str) or not isinstance(token, str):
        return False
    if not _pid_running(pid):
        return True
    current = _process_identity(pid)
    # Identity inspection can be denied or fail transiently for a live PID.
    # Treat that as unknown, not stale: reclaiming an unverifiable live owner
    # would permit a second scheduler daemon to start concurrently.
    return current is not None and current != expected


def _acquire_owner_lease(
    pid_file: Path,
    *,
    pid: int,
    instance_token: str,
    process_identity: str,
) -> bool:
    path = _owner_file(pid_file)
    payload: JsonObject = {
        "pid": pid,
        "instance_token": instance_token,
        "process_identity": process_identity,
    }
    with _lease_guard(pid_file):
        for _attempt in range(2):
            if _write_exclusive_json(path, payload):
                return True
            existing = _read_json(path)
            if existing is None or not _lease_is_stale(existing):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return False


def _replace_owner_lease(
    pid_file: Path,
    *,
    pid: int,
    instance_token: str,
    process_identity: str,
) -> bool:
    path = _owner_file(pid_file)
    with _lease_guard(pid_file):
        existing = _read_json(path)
        if existing is None or existing.get("instance_token") != instance_token:
            return False
        _write_json(
            path,
            {
                "pid": pid,
                "instance_token": instance_token,
                "process_identity": process_identity,
            },
        )
    return True


def _release_owner_lease(pid_file: Path, *, instance_token: str) -> None:
    path = _owner_file(pid_file)
    with _lease_guard(pid_file):
        existing = _read_json(path)
        if existing is None or existing.get("instance_token") != instance_token:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _sleep_interruptibly(
    interval_seconds: float,
    *,
    should_stop: Callable[[], bool],
    sleep_fn: Callable[[float], None],
) -> None:
    remaining = max(interval_seconds, 0.1)
    while remaining > 0 and not should_stop():
        step = min(remaining, 0.5)
        sleep_fn(step)
        remaining -= step


@contextmanager
def _autocommit_user_store(
    database_url: str,
    user_id: UUID,
) -> Iterator[PostgresVNextStore]:
    """Dedicated tenant session whose statements never span provider work."""

    with psycopg.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
    ) as conn:
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, false)",
            (str(user_id),),
        )
        yield PostgresVNextStore(conn)


def _coerce_claim_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise RuntimeError("scheduler claim is missing its scheduled timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@contextmanager
def _claim_heartbeat(
    *,
    database_url: str,
    user_id: UUID,
    run_id: str,
    claim_token: str,
    claim_version: int,
    lease_seconds: float,
) -> Iterator[threading.Event]:
    stop = threading.Event()
    fence_lost = threading.Event()
    heartbeat_interval = max(1.0, lease_seconds / 3.0)

    def heartbeat() -> None:
        while not stop.wait(heartbeat_interval):
            lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            try:
                with user_connection(database_url, user_id) as conn:
                    renewed = PostgresVNextStore(conn).heartbeat_scheduler_claim(
                        run_id=run_id,
                        claim_token=claim_token,
                        claim_version=claim_version,
                        lease_expires_at=lease_expires_at,
                    )
            except Exception:
                renewed = False
            if not renewed:
                fence_lost.set()
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"alice-scheduler-heartbeat-{run_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield fence_lost
    finally:
        stop.set()
        thread.join(timeout=min(heartbeat_interval + 1.0, 5.0))


def run_now_durable(
    *,
    database_url: str,
    user_id: UUID,
    request: SchedulerRunRequest,
) -> JsonObject:
    """Run one manual workflow without holding a transaction over provider I/O."""

    if database_url.startswith("sqlite:"):
        raise VNextSchedulerValidationError("durable scheduler execution requires PostgreSQL")
    with user_connection(database_url, user_id) as conn:
        started = VNextSchedulerService(PostgresVNextStore(conn)).begin_run(request)
    workflow_value = started.get("workflow")
    run_value = started.get("run")
    if not isinstance(workflow_value, dict) or not isinstance(run_value, dict):
        raise RuntimeError("scheduler manual run returned a malformed workflow or run")
    workflow = workflow_value
    run = run_value
    try:
        with _autocommit_user_store(database_url, user_id) as execution_store:
            plan = VNextSchedulerService(execution_store).prepare_started_workflow(
                request,
                run=run,
            )
        with user_connection(database_url, user_id) as conn:
            return VNextSchedulerService(PostgresVNextStore(conn)).publish_started_workflow(
                request,
                workflow=workflow,
                run=run,
                plan=plan,
            )
    except Exception as exc:  # noqa: BLE001 - failure is persisted as the run outcome
        with user_connection(database_url, user_id) as conn:
            return VNextSchedulerService(PostgresVNextStore(conn)).fail_started_workflow(
                request,
                workflow=workflow,
                run=run,
                error=exc,
            )


def run_due_workflows_durable(
    *,
    database_url: str,
    user_id: UUID,
    limit: int = 10,
    triggered_by: str = "scheduler",
    agent_identity: AgentIdentity | None = None,
    policy_decision: PolicyDecision | None = None,
    now: datetime | None = None,
    claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
) -> JsonObject:
    """Claim, execute, and fenced-finalize due work across transaction boundaries."""

    if database_url.startswith("sqlite:"):
        raise VNextSchedulerValidationError("durable scheduler execution requires PostgreSQL")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if claim_lease_seconds < 3.0:
        raise ValueError("claim_lease_seconds must be at least 3 seconds")
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    with user_connection(database_url, user_id) as conn:
        initial_store = PostgresVNextStore(conn)
        VNextSchedulerService(initial_store).ensure_default_workflows()
        reaped = initial_store.reap_expired_scheduler_claims(
            reference_time=checked_at,
            limit=max(limit * 2, 20),
            actor_type="scheduler",
        )

    runs: list[JsonObject] = []
    for _index in range(limit):
        lease_expires_at = datetime.now(UTC) + timedelta(seconds=claim_lease_seconds)
        with user_connection(database_url, user_id) as conn:
            claim_store = PostgresVNextStore(conn)
            claim = claim_store.claim_due_scheduler_workflow(
                checked_at=checked_at,
                lease_expires_at=lease_expires_at,
                triggered_by=triggered_by,
                policy_decision_json=(policy_decision.to_record() if policy_decision is not None else None),
                agent_identity_json=(agent_identity.to_record() if agent_identity is not None else None),
            )
        if claim is None:
            break
        workflow_value = claim.get("workflow")
        run_value = claim.get("run")
        if not isinstance(workflow_value, dict) or not isinstance(run_value, dict):
            raise RuntimeError("scheduler claim returned a malformed workflow or run")
        workflow = workflow_value
        run = run_value
        run_id = str(run["id"])
        claim_token = str(claim["claim_token"])
        claim_version_value = claim.get("claim_version")
        if not isinstance(claim_version_value, int) or isinstance(
            claim_version_value,
            bool,
        ):
            raise RuntimeError("scheduler claim returned a malformed claim version")
        claim_version = claim_version_value
        scheduled_for = _coerce_claim_datetime(claim.get("scheduled_for"))
        request: SchedulerRunRequest = VNextSchedulerService(claim_store).claimed_request(
            workflow=workflow,
            checked_at=checked_at,
            scheduled_for=scheduled_for,
            agent_identity=agent_identity,
            policy_decision=policy_decision,
        )

        plan: SchedulerWorkflowPlan | None = None
        artifact: JsonObject | None = None
        error: Exception | None = None
        with _claim_heartbeat(
            database_url=database_url,
            user_id=user_id,
            run_id=run_id,
            claim_token=claim_token,
            claim_version=claim_version,
            lease_seconds=claim_lease_seconds,
        ) as fence_lost:
            try:
                with _autocommit_user_store(database_url, user_id) as execution_store:
                    plan = VNextSchedulerService(execution_store).execute_claimed_workflow(
                        request,
                        run=run,
                    )
            except Exception as exc:  # noqa: BLE001 - persisted as a failed scheduler run
                logger.exception(
                    "claimed scheduler workflow failed run_id=%s error_code=%s",
                    run_id,
                    SCHEDULER_WORKFLOW_ERROR_CODE,
                )
                error = exc

        run_metadata_value = run.get("metadata_json")
        run_metadata = dict(run_metadata_value) if isinstance(run_metadata_value, dict) else {}
        finalized: JsonObject | None = None
        answered_run: JsonObject | None = None
        answered_artifact: JsonObject | None = None
        artifact_id: str | None = None
        if error is None and not fence_lost.is_set() and plan is not None:
            try:
                with user_connection(database_url, user_id) as conn:
                    finalize_store = PostgresVNextStore(conn)
                    if finalize_store.lock_scheduler_claim_for_publish(
                        run_id=run_id,
                        claim_token=claim_token,
                        claim_version=claim_version,
                    ):
                        # The lock above is the publication boundary: no
                        # staged write is executed until the exact live fence
                        # is held, and every write below shares this short
                        # transaction with the final run/workflow transition.
                        artifact = plan.publish(finalize_store)
                        artifact_id = str(artifact["id"])
                        current_workflow = finalize_store.get_scheduler_workflow(
                            str(workflow["workflow_type"])
                        )
                        next_run_at = (
                            VNextSchedulerService(finalize_store).next_run_after_workflow(current_workflow)
                            if current_workflow is not None
                            else None
                        )
                        finalized = finalize_store.finalize_scheduler_claim(
                            run_id=run_id,
                            claim_token=claim_token,
                            claim_version=claim_version,
                            status="succeeded",
                            artifact_id=artifact_id,
                            error_message=None,
                            next_run_at=next_run_at,
                            metadata_json={**run_metadata, "artifact_id": artifact_id},
                            actor_type=triggered_by,
                        )
                        if finalized is None:
                            raise RuntimeError("scheduler claim expired during atomic publication")
                        append_event(
                            finalize_store,
                            event_type="scheduler.artifact_created",
                            actor_type=triggered_by,
                            target_type="artifact",
                            target_id=artifact_id,
                            trace_id=str(run.get("trace_id")),
                            run_id=run_id,
                            payload={
                                "workflow_type": workflow["workflow_type"],
                                "scheduler_run_id": run_id,
                                "claim_version": claim_version,
                            },
                        )
                        # The answer carries the report only when the caller of this scan may read it now. The stored
                        # run and report stay as they are for the owner.
                        answered_run, answered_artifact = withhold_unreadable_artifact(
                            finalize_store, agent_identity, run=finalized, artifact=artifact
                        )
            except Exception as exc:  # noqa: BLE001 - publication transaction rolled back
                logger.exception(
                    "scheduler publication failed run_id=%s error_code=%s",
                    run_id,
                    SCHEDULER_WORKFLOW_ERROR_CODE,
                )
                error = exc
                artifact = None
                artifact_id = None
                finalized = None
                answered_run = None
                answered_artifact = None

        status = "succeeded" if finalized is not None else "failed"
        if fence_lost.is_set():
            error_code = SCHEDULER_CLAIM_LOST_ERROR_CODE
            error_message = SCHEDULER_CLAIM_LOST_ERROR_MESSAGE
        elif error is not None:
            error_code = SCHEDULER_WORKFLOW_ERROR_CODE
            error_message = SCHEDULER_WORKFLOW_ERROR_MESSAGE
        else:
            error_code = SCHEDULER_CLAIM_SUPERSEDED_ERROR_CODE
            error_message = SCHEDULER_CLAIM_SUPERSEDED_ERROR_MESSAGE
        if finalized is None:
            with user_connection(database_url, user_id) as conn:
                failure_store = PostgresVNextStore(conn)
                current_workflow = failure_store.get_scheduler_workflow(str(workflow["workflow_type"]))
                next_run_at = (
                    VNextSchedulerService(failure_store).next_run_after_workflow(current_workflow)
                    if current_workflow is not None
                    else None
                )
                finalized = failure_store.finalize_scheduler_claim(
                    run_id=run_id,
                    claim_token=claim_token,
                    claim_version=claim_version,
                    status="failed",
                    artifact_id=None,
                    error_message=error_message,
                    next_run_at=next_run_at,
                    metadata_json={
                        **run_metadata,
                        "error_code": error_code,
                        "staged_side_effects_published": False,
                    },
                    actor_type=triggered_by,
                )
            if finalized is None:
                finalized = {
                    **run,
                    "status": "failed",
                    "error_message": error_message,
                    "metadata_json": {**run_metadata, "error_code": error_code},
                    "fence_lost": True,
                }
            artifact = None
        runs.append(
            {
                "workflow_type": workflow["workflow_type"],
                "scheduled_for": scheduled_for.isoformat(),
                "run": answered_run if status == "succeeded" and answered_run is not None else finalized,
                "artifact": answered_artifact if status == "succeeded" else None,
            }
        )

    if runs:
        with user_connection(database_url, user_id) as conn:
            append_event(
                PostgresVNextStore(conn),
                event_type="scheduler.due_scan",
                actor_type=triggered_by,
                payload={"checked_at": checked_at.isoformat(), "due_count": len(runs), "limit": limit},
            )
    failed_count = 0
    for item in runs:
        run_value = item.get("run")
        if isinstance(run_value, dict) and run_value.get("status") == "failed":
            failed_count += 1
    return {
        "checked_at": checked_at.isoformat(),
        "due_count": len(runs),
        "failed_count": failed_count,
        "reaped_count": len(reaped),
        "runs": runs,
    }


def daemon_status(*, pid_file: Path = DEFAULT_PID_FILE, status_file: Path = DEFAULT_STATUS_FILE) -> JsonObject:
    status = _read_json(status_file) or {}
    pid_record = _read_pid_record(pid_file) or {}
    owner = _read_json(_owner_file(pid_file)) or {}
    owner_pid = owner.get("pid")
    pid = (
        owner_pid
        if isinstance(owner_pid, int)
        else (status.get("pid") if isinstance(status.get("pid"), int) else pid_record.get("pid"))
    )
    expected_identity = owner.get("process_identity")
    owner_token = owner.get("instance_token")
    ownership_records_match = bool(
        isinstance(owner_pid, int)
        and isinstance(owner_token, str)
        and isinstance(expected_identity, str)
        and pid_record.get("pid") == owner_pid
        and pid_record.get("instance_token") == owner_token
        and pid_record.get("process_identity") == expected_identity
        and status.get("pid") == owner_pid
        and status.get("instance_token") == owner_token
        and status.get("process_identity") == expected_identity
    )
    current_identity = _process_identity(pid) if isinstance(pid, int) else None
    ownership_verified = bool(ownership_records_match and current_identity == expected_identity)
    running = bool(
        status.get("running") is not False and isinstance(pid, int) and _pid_running(pid) and ownership_verified
    )
    return {
        **status,
        "configured": status != {} or pid_record != {},
        "pid": pid if isinstance(pid, int) else None,
        "pid_file": str(pid_file),
        "status_file": str(status_file),
        "ownership_verified": ownership_verified,
        "ownership_records_match": ownership_records_match,
        "reported_running": status.get("running"),
        "running": running,
    }


def stop_daemon(
    *, pid_file: Path = DEFAULT_PID_FILE, status_file: Path = DEFAULT_STATUS_FILE, timeout_seconds: float = 10.0
) -> JsonObject:
    status = daemon_status(pid_file=pid_file, status_file=status_file)
    pid = status.get("pid")
    if not isinstance(pid, int):
        return {**status, "stopped": False, "message": "No scheduler daemon pid file found."}
    if not _pid_running(pid):
        return {**status, "stopped": True, "message": "Scheduler daemon was already stopped."}
    if status.get("ownership_verified") is not True:
        return {
            **status,
            "stopped": False,
            "message": "Refusing to signal an unverified scheduler PID; remove stale runtime files manually.",
        }
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current = daemon_status(pid_file=pid_file, status_file=status_file)
        if not _pid_running(pid) or current.get("running") is False:
            return {**current, "stopped": True}
        time.sleep(0.2)
    return {
        **daemon_status(pid_file=pid_file, status_file=status_file),
        "stopped": False,
        "message": "Scheduler daemon did not stop before timeout.",
    }


def start_background_daemon(config: SchedulerRuntimeConfig) -> JsonObject:
    current = daemon_status(pid_file=config.pid_file, status_file=config.status_file)
    if current.get("running") is True:
        return {**current, "started": False, "message": "Scheduler daemon is already running."}
    current_pid = current.get("pid")
    if (
        isinstance(current_pid, int)
        and _pid_running(current_pid)
        and current.get("ownership_verified") is not True
        and current.get("reported_running") is not False
    ):
        return {
            **current,
            "started": False,
            "message": "Refusing to replace an unverified live scheduler PID.",
        }
    config.pid_file.parent.mkdir(parents=True, exist_ok=True)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "alicebot_api",
        "--user-id",
        str(config.user_id),
        "vnext",
        "scheduler",
        "daemon",
        "start",
        "--foreground",
        "--interval-seconds",
        str(config.interval_seconds),
        "--limit",
        str(config.limit),
        "--pid-file",
        str(config.pid_file),
        "--status-file",
        str(config.status_file),
        "--log-file",
        str(config.log_file),
    ]
    if config.once:
        command.append("--once")
    instance_token = uuid4().hex
    parent_identity = _process_identity(os.getpid())
    if parent_identity is None or not _acquire_owner_lease(
        config.pid_file,
        pid=os.getpid(),
        instance_token=instance_token,
        process_identity=parent_identity,
    ):
        return {
            **current,
            "started": False,
            "message": "Scheduler ownership is held by another launcher or daemon.",
        }
    child_env = {
        **os.environ,
        INSTANCE_TOKEN_ENV: instance_token,
        "DATABASE_URL": config.database_url,
    }
    log_handle = config.log_file.open("ab")
    try:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=child_env,
        )
    except Exception:
        _release_owner_lease(config.pid_file, instance_token=instance_token)
        raise
    finally:
        log_handle.close()
    identity_deadline = time.monotonic() + 2.0
    process_identity = _process_identity(process.pid)
    while process_identity is None and process.poll() is None and time.monotonic() < identity_deadline:
        time.sleep(0.01)
        process_identity = _process_identity(process.pid)
    if process_identity is None:
        process.terminate()
        _release_owner_lease(config.pid_file, instance_token=instance_token)
        return {
            **current,
            "started": False,
            "message": "Scheduler process started but ownership could not be verified; it was terminated.",
        }
    if not _replace_owner_lease(
        config.pid_file,
        pid=process.pid,
        instance_token=instance_token,
        process_identity=process_identity,
    ):
        process.terminate()
        return {
            **current,
            "started": False,
            "message": "Scheduler ownership handoff failed; the child was terminated.",
        }
    _write_pid_record(
        config.pid_file,
        pid=process.pid,
        instance_token=instance_token,
        process_identity=process_identity,
    )
    _write_json(
        config.status_file,
        {
            "pid": process.pid,
            "instance_token": instance_token,
            "process_identity": process_identity,
            "running": True,
            "started_at": _now_iso(),
            "last_heartbeat_at": _now_iso(),
            "interval_seconds": config.interval_seconds,
            "limit": config.limit,
            "mode": "background",
            "log_file": str(config.log_file),
        },
    )
    return daemon_status(pid_file=config.pid_file, status_file=config.status_file) | {"started": True}


def run_foreground_daemon(
    config: SchedulerRuntimeConfig,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> JsonObject:
    stop_requested = False

    def _request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_sigterm = signal.signal(signal.SIGTERM, _request_stop)
    previous_sigint = signal.signal(signal.SIGINT, _request_stop)
    config.pid_file.parent.mkdir(parents=True, exist_ok=True)
    instance_token = os.environ.get(INSTANCE_TOKEN_ENV) or uuid4().hex
    process_identity = _process_identity(os.getpid())
    if process_identity is None:
        raise RuntimeError("scheduler process ownership could not be established")
    current = daemon_status(pid_file=config.pid_file, status_file=config.status_file)
    if current.get("running") is True and current.get("pid") != os.getpid():
        return {**current, "started": False, "message": "Scheduler daemon is already running."}
    current_pid = current.get("pid")
    if (
        isinstance(current_pid, int)
        and current_pid != os.getpid()
        and _pid_running(current_pid)
        and current.get("ownership_verified") is not True
    ):
        return {**current, "started": False, "message": "Refusing an unverified live scheduler PID."}
    owner = _read_json(_owner_file(config.pid_file)) or {}
    inherited_lease = owner.get("instance_token") == instance_token
    if inherited_lease:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            owner = _read_json(_owner_file(config.pid_file)) or {}
            pid_record = _read_pid_record(config.pid_file) or {}
            parent_status = _read_json(config.status_file) or {}
            if (
                owner.get("pid") == os.getpid()
                and owner.get("process_identity") == process_identity
                and pid_record.get("pid") == os.getpid()
                and pid_record.get("instance_token") == instance_token
                and pid_record.get("process_identity") == process_identity
                and parent_status.get("pid") == os.getpid()
                and parent_status.get("instance_token") == instance_token
                and parent_status.get("process_identity") == process_identity
            ):
                break
            time.sleep(0.01)
        inherited_lease = (
            owner.get("pid") == os.getpid()
            and owner.get("process_identity") == process_identity
            and (_read_pid_record(config.pid_file) or {}).get("instance_token") == instance_token
            and (_read_pid_record(config.pid_file) or {}).get("process_identity") == process_identity
            and (_read_json(config.status_file) or {}).get("instance_token") == instance_token
            and (_read_json(config.status_file) or {}).get("process_identity") == process_identity
        )
    if not inherited_lease and not _acquire_owner_lease(
        config.pid_file,
        pid=os.getpid(),
        instance_token=instance_token,
        process_identity=process_identity,
    ):
        return {
            **current,
            "started": False,
            "message": "Scheduler ownership is held by another launcher or daemon.",
        }
    _write_pid_record(
        config.pid_file,
        pid=os.getpid(),
        instance_token=instance_token,
        process_identity=process_identity,
    )
    started_at = _now_iso()
    last_payload: JsonObject = {
        "pid": os.getpid(),
        "instance_token": instance_token,
        "process_identity": process_identity,
        "running": True,
        "started_at": started_at,
        "last_heartbeat_at": started_at,
        "interval_seconds": config.interval_seconds,
        "limit": config.limit,
        "mode": "foreground",
        "last_due_scan": None,
        "last_error": None,
        "last_error_code": None,
        "last_error_type": None,
        "exit_code": 0,
    }
    try:
        while not stop_requested:
            try:
                result = run_due_workflows_durable(
                    database_url=config.database_url,
                    user_id=config.user_id,
                    limit=config.limit,
                    claim_lease_seconds=config.claim_lease_seconds,
                )
                failed_count_value = result.get("failed_count", 0)
                if not isinstance(failed_count_value, int) or isinstance(
                    failed_count_value,
                    bool,
                ):
                    raise RuntimeError("durable scheduler returned an invalid failed count")
                failed_count = failed_count_value
                last_payload = {
                    **last_payload,
                    "running": True,
                    "last_heartbeat_at": _now_iso(),
                    "last_due_scan": result,
                    "last_due_scan_at": result.get("checked_at"),
                    "last_due_count": result.get("due_count", 0),
                    "last_error": SCHEDULER_WORKFLOW_ERROR_MESSAGE if failed_count else None,
                    "last_error_code": SCHEDULER_WORKFLOW_ERROR_CODE if failed_count else None,
                    "last_error_type": None,
                    "exit_code": 1 if failed_count else 0,
                }
            except Exception:  # pragma: no cover - exercised through CLI smoke paths
                logger.exception(
                    "scheduler scan failed error_code=%s",
                    SCHEDULER_SCAN_ERROR_CODE,
                )
                last_payload = {
                    **last_payload,
                    "running": True,
                    "last_heartbeat_at": _now_iso(),
                    "last_error": SCHEDULER_SCAN_ERROR_MESSAGE,
                    "last_error_code": SCHEDULER_SCAN_ERROR_CODE,
                    "last_error_type": None,
                    "exit_code": 1,
                }
            _write_json(config.status_file, last_payload)
            if config.once:
                break
            _sleep_interruptibly(
                config.interval_seconds,
                should_stop=lambda: stop_requested,
                sleep_fn=sleep_fn,
            )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        stopped_payload = {**last_payload, "running": False, "stopped_at": _now_iso()}
        _write_json(config.status_file, stopped_payload)
        _release_owner_lease(config.pid_file, instance_token=instance_token)
    return daemon_status(pid_file=config.pid_file, status_file=config.status_file)


__all__ = [
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "DEFAULT_LOG_FILE",
    "DEFAULT_PID_FILE",
    "DEFAULT_RUNTIME_DIR",
    "DEFAULT_STATUS_FILE",
    "SchedulerRuntimeConfig",
    "daemon_status",
    "run_due_workflows_durable",
    "run_foreground_daemon",
    "run_now_durable",
    "start_background_daemon",
    "stop_daemon",
]
