#!/usr/bin/env python3
"""Validate the documented single-tenant deployment configuration contract.

This smoke intentionally does not claim that CI provisioned public DNS, a public
certificate, or a cloud host. It validates the checked-in fail-closed examples
and emits a path- and secret-free receipt that names the remaining owner-run
deployment proof.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
ENV_RELATIVE_PATH = Path("packaging/cloud/single-tenant.env.example")
CADDY_RELATIVE_PATH = Path("packaging/cloud/Caddyfile.example")
GUIDE_RELATIVE_PATH = Path("docs/deployment/single-tenant-self-hosted.md")
WORKFLOW_RELATIVE_PATH = Path(".github/workflows/deployment-guide-smoke.yml")
WEB_API_SOURCE_RELATIVE_PATH = Path("apps/web/lib/api.ts")
SEED_HELPER_RELATIVE_PATH = Path("scripts/seed_local_user.py")
POSTGRES_CA_PATH = "/etc/alicebot/postgres-ca.pem"
CLIENT_CA_PATH = "/etc/alicebot/client-ca.pem"
RECOVERY_ENV_PATH = "/etc/alicebot/backup-restore.env"
VALIDATED_ASSETS = {
    "caddyfile_example": CADDY_RELATIVE_PATH,
    "deployment_guide": GUIDE_RELATIVE_PATH,
    "environment_example": ENV_RELATIVE_PATH,
    "local_user_seed_helper": SEED_HELPER_RELATIVE_PATH,
    "web_api_source": WEB_API_SOURCE_RELATIVE_PATH,
    "workflow": WORKFLOW_RELATIVE_PATH,
}
CONTRACT_INPUTS = dict(VALIDATED_ASSETS)
REPORT_VERSION = "single_tenant_deployment_contract.v1"
OWNER_RECEIPT_BLOCKER = "owner_real_host_deployment_receipt"
_SAFE_FAILURE_CODE = re.compile(r"^[a-z0-9_.:-]+$")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}(?:\s|$)")
_SECRET_MARKERS = (
    "postgresql://",
    "alice_sk_",
    "BEGIN PRIVATE KEY",
    "ALICEBOT_DB_APP_PASSWORD",
    "ALICEBOT_DB_ADMIN_PASSWORD",
    "ALICEBOT_DB_BACKUP_PASSWORD",
    "ALICEBOT_DB_DRILL_PASSWORD",
)


class DeploymentContractError(RuntimeError):
    """A validation failure represented only by a stable, public-safe code."""

    def __init__(self, code: str):
        if _SAFE_FAILURE_CODE.fullmatch(code) is None:
            raise ValueError("deployment failure codes must be stable identifiers")
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise DeploymentContractError(code)


def _git_bytes(repo_root: Path, arguments: list[str], *, code: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentContractError(code) from exc
    if completed.returncode != 0:
        raise DeploymentContractError(code)
    return completed.stdout


def _snapshot_paths(repo_root: Path) -> tuple[list[bytes], bytes]:
    index = _git_bytes(
        repo_root,
        ["ls-files", "--stage", "-z"],
        code="carrier_index_unavailable",
    )
    if any(entry.startswith(b"160000 ") for entry in index.split(b"\0") if entry):
        raise DeploymentContractError("carrier_snapshot_gitlink_unsupported")
    current = _git_bytes(
        repo_root,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"],
        code="carrier_paths_unavailable",
    )
    head = _git_bytes(
        repo_root,
        ["ls-tree", "-r", "--name-only", "-z", "HEAD"],
        code="carrier_head_paths_unavailable",
    )
    return sorted({path for path in (*current.split(b"\0"), *head.split(b"\0")) if path}), index


def _snapshot_entry(root_fd: int, relative_path: bytes) -> tuple[bytes, int, bytes]:
    parts = relative_path.split(b"/")
    if not parts or any(part in {b"", b".", b".."} for part in parts):
        raise DeploymentContractError("carrier_path_invalid")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise DeploymentContractError("carrier_snapshot_nofollow_unavailable")
    parent_fd = os.dup(root_fd)
    try:
        try:
            for component in parts[:-1]:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = child_fd
            file_name = parts[-1]
            info = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                return b"missing", 0, b""
            raise DeploymentContractError("carrier_entry_stat_failed") from exc

        mode = info.st_mode & 0o177777
        if stat.S_ISLNK(info.st_mode):
            try:
                target = os.readlink(file_name, dir_fd=parent_fd)
            except OSError as exc:
                raise DeploymentContractError("carrier_symlink_read_failed") from exc
            return b"symlink", mode, target if isinstance(target, bytes) else os.fsencode(target)
        if not stat.S_ISREG(info.st_mode):
            raise DeploymentContractError("carrier_entry_type_unsupported")
        try:
            file_fd = os.open(file_name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
        except OSError as exc:
            raise DeploymentContractError("carrier_file_open_failed") from exc
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise DeploymentContractError("carrier_entry_changed_during_snapshot")
            content = hashlib.sha256()
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                content.update(chunk)
            after = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise DeploymentContractError("carrier_entry_changed_during_snapshot")
        return b"regular", mode, content.digest()
    finally:
        os.close(parent_fd)


def _digest_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _carrier_snapshot_once(repo_root: Path) -> str:
    paths, index = _snapshot_paths(repo_root)
    digest = hashlib.sha256(b"alice-carrier-snapshot-v1\0")
    _digest_field(digest, index)
    root_fd = os.open(repo_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for relative_path in paths:
            entry_type, mode, payload = _snapshot_entry(root_fd, relative_path)
            _digest_field(digest, relative_path)
            _digest_field(digest, entry_type)
            _digest_field(digest, f"{mode:o}".encode("ascii"))
            _digest_field(digest, payload)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def _read_regular_asset(root_fd: int, relative_path: Path) -> bytes:
    path_bytes = relative_path.as_posix().encode("utf-8")
    parts = path_bytes.split(b"/")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise DeploymentContractError("carrier_snapshot_nofollow_unavailable")
    parent_fd = os.dup(root_fd)
    try:
        try:
            for component in parts[:-1]:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
                os.close(parent_fd)
                parent_fd = child_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=parent_fd)
        except OSError as exc:
            raise DeploymentContractError("validated_asset_unreadable") from exc
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise DeploymentContractError("validated_asset_not_regular")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            raise DeploymentContractError("carrier_entry_changed_during_snapshot")
        return b"".join(chunks)
    finally:
        os.close(parent_fd)


def _read_contract_inputs(repo_root: Path) -> dict[str, bytes]:
    root_fd = os.open(repo_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        return {
            logical_name: _read_regular_asset(root_fd, relative_path)
            for logical_name, relative_path in CONTRACT_INPUTS.items()
        }
    finally:
        os.close(root_fd)


def repository_carrier_identity(repo_root: Path) -> tuple[dict[str, object], dict[str, bytes]]:
    repo_root = repo_root.resolve(strict=True)
    before_head = _git_bytes(repo_root, ["rev-parse", "HEAD"], code="source_head_commit_unavailable").strip()
    before_tree = _git_bytes(
        repo_root,
        ["rev-parse", "HEAD^{tree}"],
        code="source_head_tree_unavailable",
    ).strip()
    before_status = _git_bytes(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"],
        code="carrier_status_unavailable",
    )
    before_assets = _read_contract_inputs(repo_root)
    first_snapshot = _carrier_snapshot_once(repo_root)
    second_snapshot = _carrier_snapshot_once(repo_root)
    after_assets = _read_contract_inputs(repo_root)
    after_status = _git_bytes(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no"],
        code="carrier_status_unavailable",
    )
    after_head = _git_bytes(repo_root, ["rev-parse", "HEAD"], code="source_head_commit_unavailable").strip()
    after_tree = _git_bytes(
        repo_root,
        ["rev-parse", "HEAD^{tree}"],
        code="source_head_tree_unavailable",
    ).strip()
    if (
        first_snapshot != second_snapshot
        or before_assets != after_assets
        or before_status != after_status
        or before_head != after_head
        or before_tree != after_tree
    ):
        raise DeploymentContractError("carrier_changed_during_snapshot")
    try:
        source_head_commit = before_head.decode("ascii")
        source_head_tree = before_tree.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DeploymentContractError("source_identity_invalid") from exc
    _require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_head_commit) is not None, "source_identity_invalid")
    _require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_head_tree) is not None, "source_identity_invalid")
    provenance: dict[str, object] = {
        "source_head_commit": source_head_commit,
        "source_head_tree": source_head_tree,
        "carrier_state": "dirty" if before_status else "clean",
        "carrier_snapshot_sha256": first_snapshot,
        "validated_asset_sha256": {
            logical_name: hashlib.sha256(before_assets[logical_name]).hexdigest()
            for logical_name in sorted(VALIDATED_ASSETS)
        },
    }
    return provenance, before_assets


def parse_env_example(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "" or line.startswith("#"):
            continue
        _require("=" in line, "env_line_invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        _require(re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is not None, "env_key_invalid")
        _require(key not in values, "env_key_duplicate")
        values[key] = value.strip().strip('"').strip("'")
    return values


def _validate_exact_https_origin(value: str, *, code: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise DeploymentContractError(code) from exc
    _require(
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == "",
        code,
    )
    _require("*" not in value and "," not in value, code)


def _validate_database_url(
    value: str,
    *,
    expected_user: str,
    expected_placeholder: str,
    expected_database: str = "alicebot",
) -> tuple[str, tuple[str, int, str]]:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise DeploymentContractError("database_url_invalid") from exc
    _require(parsed.scheme in {"postgres", "postgresql"}, "database_scheme_invalid")
    _require(parsed.username == expected_user, "database_role_invalid")
    _require(parsed.password == expected_placeholder, "database_secret_placeholder_invalid")
    _require(parsed.hostname not in {None, "", "localhost", "127.0.0.1", "::1"}, "database_host_invalid")
    _require(parsed.path == f"/{expected_database}", "database_name_invalid")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise DeploymentContractError("database_port_invalid") from exc
    query = parse_qs(parsed.query, keep_blank_values=True)
    _require(query.get("sslmode") == ["verify-full"], "database_tls_invalid")
    _require(
        query.get("sslrootcert") == [POSTGRES_CA_PATH],
        "database_ca_path_invalid",
    )
    return expected_user, ((parsed.hostname or "").lower(), port, parsed.path)


def validate_environment(values: Mapping[str, str]) -> None:
    required = {
        "APP_ENV",
        "APP_HOST",
        "APP_PORT",
        "DATABASE_URL",
        "ALICEBOT_AUTH_USER_ID",
        "ALICE_LEGACY_SURFACES",
        "LEGACY_V0_ENABLED_OUTSIDE_DEV",
        "CORS_ALLOWED_ORIGINS",
        "CORS_ALLOW_CREDENTIALS",
        "NEXT_PUBLIC_ALICEBOT_API_BASE_URL",
        "NEXT_PUBLIC_ALICEBOT_USER_ID",
        "TRUST_PROXY_HEADERS",
        "TRUSTED_PROXY_IPS",
        "ALICE_WEB_HOST",
        "ALICE_WEB_PORT",
    }
    _require(required <= set(values), "env_required_key_missing")
    _require(values["APP_ENV"] == "production", "app_env_not_production")
    _require(values["APP_HOST"] == "127.0.0.1", "api_bind_not_loopback")
    _require(values["ALICE_WEB_HOST"] == "127.0.0.1", "web_bind_not_loopback")
    _require(values["APP_PORT"] == "8000", "api_port_invalid")
    _require(values["ALICE_WEB_PORT"] == "3000", "web_port_invalid")
    _require(values["ALICE_LEGACY_SURFACES"] == "0", "legacy_surface_not_disabled")
    _require(values["LEGACY_V0_ENABLED_OUTSIDE_DEV"].lower() == "false", "legacy_v0_production_gate_enabled")
    _require(values["CORS_ALLOW_CREDENTIALS"].lower() == "false", "cors_credentials_invalid")

    origin = values["CORS_ALLOWED_ORIGINS"]
    _validate_exact_https_origin(origin, code="cors_origin_invalid")
    _require(values["NEXT_PUBLIC_ALICEBOT_API_BASE_URL"] == origin, "web_api_origin_mismatch")
    _require(values.get("PUBLIC_ORIGIN") == origin, "public_origin_mismatch")

    try:
        auth_user = UUID(values["ALICEBOT_AUTH_USER_ID"])
        web_user = UUID(values["NEXT_PUBLIC_ALICEBOT_USER_ID"])
    except ValueError as exc:
        raise DeploymentContractError("auth_user_invalid") from exc
    _require(auth_user.int != 0 and auth_user == web_user, "auth_user_mismatch")

    _require(values["TRUST_PROXY_HEADERS"].lower() == "true", "proxy_headers_not_enabled")
    _require(values["TRUSTED_PROXY_IPS"] == "127.0.0.1", "trusted_proxy_invalid")

    _validate_database_url(
        values["DATABASE_URL"],
        expected_user="alicebot_app",
        expected_placeholder="${ALICEBOT_DB_APP_PASSWORD}",
    )

    for forbidden in (
        "DATABASE_ADMIN_URL",
        "DATABASE_BACKUP_URL",
        "DATABASE_LIFECYCLE_URL",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "WORKSPACE_PROVIDER_CONFIGS_JSON",
    ):
        _require(forbidden not in values, "deprecated_or_inline_secret_setting_present")


def validate_role_separated_database_contract(
    values: Mapping[str, str],
    guide_text: str,
) -> None:
    admin_match = re.search(r'(?m)^DATABASE_ADMIN_URL="([^"\n]+)"$', guide_text)
    backup_match = re.search(r'(?m)^DATABASE_BACKUP_URL="([^"\n]+)"$', guide_text)
    lifecycle_match = re.search(r'(?m)^DATABASE_LIFECYCLE_URL="([^"\n]+)"$', guide_text)
    if admin_match is None:
        raise DeploymentContractError("migration_admin_database_example_missing")
    if backup_match is None:
        raise DeploymentContractError("backup_database_example_missing")
    if lifecycle_match is None:
        raise DeploymentContractError("lifecycle_database_example_missing")
    runtime_role, runtime_endpoint = _validate_database_url(
        values["DATABASE_URL"],
        expected_user="alicebot_app",
        expected_placeholder="${ALICEBOT_DB_APP_PASSWORD}",
    )
    admin_role, admin_endpoint = _validate_database_url(
        admin_match.group(1),
        expected_user="alicebot_admin",
        expected_placeholder="${ALICEBOT_DB_ADMIN_PASSWORD}",
    )
    backup_role, backup_endpoint = _validate_database_url(
        backup_match.group(1),
        expected_user="alicebot_backup",
        expected_placeholder="${ALICEBOT_DB_BACKUP_PASSWORD}",
    )
    lifecycle_role, lifecycle_endpoint = _validate_database_url(
        lifecycle_match.group(1),
        expected_user="alicebot_drill",
        expected_placeholder="${ALICEBOT_DB_DRILL_PASSWORD}",
        expected_database="postgres",
    )
    _require(
        len({runtime_role, admin_role, backup_role, lifecycle_role}) == 4,
        "database_roles_not_separated",
    )
    _require(
        runtime_endpoint == admin_endpoint == backup_endpoint,
        "database_endpoints_mismatch",
    )
    _require(
        lifecycle_endpoint[:2] == runtime_endpoint[:2],
        "database_endpoints_mismatch",
    )


ALICE_UPSTREAMS = ("127.0.0.1:8000", "127.0.0.1:3000")


def _site_blocks(normalized: str) -> list[tuple[str, str]]:
    """Split a normalized Caddyfile into (address line, body) site blocks.

    Brace-depth scan rather than a real parser, which is enough for this file's shape and
    keeps the check dependency-free. The global options block opens with a bare `{` and is
    returned with an empty address so callers can skip it.
    """

    blocks: list[tuple[str, str]] = []
    address: str | None = None
    body: list[str] = []
    depth = 0
    for line in normalized.splitlines():
        stripped = line.strip()
        if depth == 0:
            if not stripped:
                continue
            if stripped.endswith("{"):
                address = stripped[:-1].strip()
                body = []
                depth = 1
            continue
        depth += stripped.count("{") - stripped.count("}")
        if depth <= 0:
            blocks.append((address or "", "\n".join(body)))
            address, body, depth = None, [], 0
            continue
        body.append(line)
    if address is not None:
        blocks.append((address, "\n".join(body)))
    return blocks


def _validate_caddy_block_scoping(normalized: str) -> None:
    """Every block that can reach Alice must itself demand a client certificate.

    The rest of this validator is file-wide substring matching, which was sound only while
    the example had exactly one site block. Once a second block exists (a public landing
    page on the apex, say), a file-wide check proves "some block has mTLS", not "the block
    serving Alice has mTLS". Reproduced 2026-08-15: stripping the tls/client_auth stanza
    from the Alice block and leaving those strings in any other block passed every check,
    so CI would go green on a configuration serving Alice to anyone.
    """

    reaches_alice = [
        (address, body)
        for address, body in _site_blocks(normalized)
        if address and any(f"reverse_proxy {upstream}" in body for upstream in ALICE_UPSTREAMS)
    ]
    _require(bool(reaches_alice), "caddy_api_upstream_invalid")
    for address, body in reaches_alice:
        _require("client_auth" in body, "caddy_authentication_missing")
        _require("mode require_and_verify" in body, "caddy_mtls_not_fail_closed")
        _require(
            f"trust_pool file {CLIENT_CA_PATH}" in body,
            "caddy_mtls_trust_pool_missing",
        )


def validate_caddyfile(text: str) -> None:
    normalized = "\n".join(line.split("#", 1)[0].rstrip() for line in text.splitlines())
    _validate_caddy_block_scoping(normalized)
    directives = {
        tuple(line.split())
        for line in normalized.splitlines()
        if line.strip() and line.strip() not in {"{", "}"}
    }
    _require(("alice.example.com", "{") in directives, "caddy_public_host_missing")
    _require("admin 127.0.0.1:2019" in normalized, "caddy_admin_not_loopback")
    _require("strict_sni_host on" in normalized, "caddy_strict_sni_missing")
    _require("client_auth" in normalized, "caddy_authentication_missing")
    _require("mode require_and_verify" in normalized, "caddy_mtls_not_fail_closed")
    _require(
        f"trust_pool file {CLIENT_CA_PATH}" in normalized,
        "caddy_mtls_trust_pool_missing",
    )
    _require(("reverse_proxy", "127.0.0.1:8000") in directives, "caddy_api_upstream_invalid")
    _require(("reverse_proxy", "127.0.0.1:3000") in directives, "caddy_web_upstream_invalid")
    api_matcher = re.search(r"(?m)^\s*@alice_api\s+path\s+([^\n]+)$", normalized)
    if api_matcher is None:
        raise DeploymentContractError("caddy_public_api_routes_invalid")
    public_api_paths = set(api_matcher.group(1).split())
    _require(
        public_api_paths
        == {
            "/healthz",
            "/openapi.json",
            "/docs",
            "/docs/*",
            "/redoc",
            "/redoc/*",
            "/v0/vnext",
            "/v0/vnext/*",
        },
        "caddy_public_api_routes_invalid",
    )
    _require("/v1" not in normalized and "/v0/*" not in normalized, "caddy_public_api_routes_invalid")
    _require("tls internal" not in normalized, "caddy_public_ca_disabled")
    _require(
        'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in normalized,
        "caddy_hsts_missing",
    )
    _require(
        'Content-Security-Policy "frame-ancestors \'none\'"' in normalized
        and 'X-Frame-Options "DENY"' in normalized,
        "caddy_clickjacking_defense_missing",
    )
    _require(
        re.search(r"(?im)^\s*header_up\s+X-Forwarded-For\b", normalized) is None,
        "caddy_forwarded_client_overridden",
    )
    _require(
        re.search(r"request_body\s*\{[^}]*\bmax_size\s+[0-9]+\s*[A-Za-z]+\b", normalized) is not None,
        "caddy_request_body_limit_missing",
    )
    for forbidden in ("reverse_proxy 0.0.0.0", "reverse_proxy localhost", "http://alice.example.com"):
        _require(forbidden not in normalized, "caddy_non_loopback_or_plaintext_upstream")


def validate_guide(text: str) -> None:
    normalized = re.sub(r"\s+", " ", text)
    required_phrases = (
        "Keyless equals local-machine-owner trust",
        "APP_ENV=production",
        "PostgreSQL 16",
        "pgvector",
        "sslmode=verify-full",
        "percent-encode each database password as URL userinfo",
        "alicebot_admin",
        "alicebot_app",
        "env:TELEGRAM_BOT_TOKEN",
        "before opening the firewall",
        "must return 401",
        "`/healthz` checks PostgreSQL only",
        "external scheduler",
        "off-host encrypted",
        "disposable restore",
        "no in-place schema downgrade",
        OWNER_RECEIPT_BLOCKER,
        "multi-tenant",
        "SLA",
        "high availability",
        "managed backup",
        "managed alert",
        "carrier_snapshot_sha256",
        "validated_asset_sha256",
        "/vnext is the only live authenticated browser console",
        "future BFF or client-side refactor",
        "Remote /v1 is unsupported",
        "no-client-certificate rejection",
        "untrusted-client-certificate rejection",
        "revoked-client-certificate rejection",
        "HSTS and clickjacking response headers",
        "DATABASE_ADMIN_URL is absent from the API runtime environment",
        "DATABASE_ADMIN_URL is required for migrations",
        "DATABASE_BACKUP_URL",
        "DATABASE_LIFECYCLE_URL",
        "`alicebot_admin` is `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`",
        "`alicebot_app` is `NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`",
        "`alicebot_backup` is `NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS`",
        "`alicebot_drill` is `NOSUPERUSER CREATEDB NOCREATEROLE NOBYPASSRLS`",
        "`CREATEDB` is cluster-wide",
        "CREATE EXTENSION IF NOT EXISTS pgcrypto",
        "CREATE EXTENSION IF NOT EXISTS vector",
        "template1",
        "GRANT CONNECT, CREATE ON DATABASE alice_restore_test TO alicebot_admin",
        "GRANT CONNECT, TEMPORARY ON DATABASE alice_restore_test TO alicebot_app",
        "GRANT CONNECT ON DATABASE alice_restore_test TO alicebot_backup",
        "GRANT USAGE, CREATE ON SCHEMA public",
        "TO alicebot_admin WITH GRANT OPTION",
        "GRANT USAGE ON SCHEMA public TO alicebot_app, alicebot_backup",
        "--no-comments",
        "ACL - SCHEMA public",
        '$4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public"',
        'test "$public_schema_acl_count" = 1',
        "--use-list=alice.restore.list",
        "Do not use `--no-acl`",
        "entire public-schema ACL is deliberately reconstructed target-side",
        "All table, sequence, and non-public-schema object ACL entries must remain",
        "./.venv/bin/python scripts/seed_local_user.py",
        "transaction-local",
        "id alicebot",
        "getent group alicebot",
        "env -u DATABASE_ADMIN_URL -u DATABASE_BACKUP_URL -u DATABASE_LIFECYCLE_URL",
        "Git-ignored deployment-local input",
        "excluded from the Git carrier/source identity",
        "root-owned, group-readable by `alicebot`, and mode `0640`",
        f"EnvironmentFile={RECOVERY_ENV_PATH}",
        f"BindReadOnlyPaths={POSTGRES_CA_PATH}",
        "run_phase5_ops_evidence.py --backend postgres",
        "runtime DB role=alicebot_app",
        "SELECT session_user, current_user",
        "admin DSN absent from API service environment",
        "backup DSN absent from API service environment",
        "lifecycle DSN absent from API service environment",
        "remote-v1-not-api",
        "remote-non-vnext-v0-not-api",
        "remote-vnext-lookalike-not-api",
    )
    for phrase in required_phrases:
        _require(phrase in normalized, "deployment_guide_contract_incomplete")
    transient_secret_prefix = "/run" + "/secrets/alicebot"
    _require(
        transient_secret_prefix not in normalized,
        "deployment_guide_transient_secret_path",
    )


def validate_seed_helper_contract(source: str) -> None:
    for fragment in (
        "from alicebot_api.db import set_current_user",
        "with conn.transaction():",
        "set_current_user(conn, user_id)",
        'current_env.get("DATABASE_ADMIN_URL", "").strip()',
        'current_env.get("ALICEBOT_AUTH_USER_ID", "").strip()',
        "ON CONFLICT (id) DO UPDATE",
    ):
        _require(fragment in source, "local_user_seed_contract_invalid")
    _require(
        'current_env.get("DATABASE_URL"' not in source,
        "local_user_seed_runtime_dsn_fallback",
    )


def _typescript_function_source(source: str, name: str) -> str:
    match = re.search(rf"(?:export\s+)?function\s+{re.escape(name)}\s*\(", source)
    if match is None:
        raise DeploymentContractError("web_trust_function_missing")
    opening = source.find("{", match.end())
    _require(opening >= 0, "web_trust_function_invalid")
    depth = 0
    for index in range(opening, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
    raise DeploymentContractError("web_trust_function_invalid")


def validate_web_trust_contract(source: str) -> None:
    current_origin = _typescript_function_source(source, "currentAliceWebOrigin")
    for fragment in (
        "process.env.PUBLIC_ORIGIN",
        "window.location.origin",
        'parsed.protocol !== "https:"',
        "parsed.username",
        "parsed.password",
        'parsed.pathname !== "/"',
        "parsed.search",
        "parsed.hash",
        "return parsed.origin",
    ):
        _require(fragment in current_origin, "web_current_origin_contract_invalid")

    trusted = _typescript_function_source(source, "isTrustedApiBaseUrl")
    for fragment in (
        "export function isTrustedApiBaseUrl",
        "isLocalApiBaseUrl(normalized)",
        'parsed.protocol === "https:"',
        'parsed.pathname === "/"',
        "parsed.origin === currentAliceWebOrigin()",
    ):
        _require(fragment in trusted, "web_exact_origin_trust_invalid")

    live_config = _typescript_function_source(source, "hasLiveApiConfig")
    _require(
        "isTrustedApiBaseUrl(config.apiBaseUrl)" in live_config,
        "web_live_config_bypasses_trust",
    )
    operator_key = _typescript_function_source(source, "shouldAttachVNextOperatorAgentApiKey")
    for fragment in (
        "isTrustedApiBaseUrl(apiBaseUrl)",
        'logicalPath === "/v0/vnext"',
        'logicalPath.startsWith("/v0/vnext/")',
    ):
        _require(fragment in operator_key, "web_operator_key_bypasses_trust")


def validate_supply_chain_pins(workflow_text: str) -> dict[str, int]:
    action_count = 0
    image_count = 0
    for raw_line in workflow_text.splitlines():
        line = raw_line.strip()
        action_match = re.match(r"uses:\s*([^\s#]+)", line)
        if action_match is not None:
            reference = action_match.group(1)
            if not reference.startswith("./"):
                action_count += 1
                _require("@" in reference, "workflow_action_unpinned")
                revision = reference.rsplit("@", 1)[1]
                _require(_FULL_SHA.fullmatch(revision) is not None, "workflow_action_unpinned")
        image_match = re.match(r"image:\s*([^\s#]+)", line)
        if image_match is not None:
            image_count += 1
            _require(_IMAGE_DIGEST.search(image_match.group(1)) is not None, "workflow_image_unpinned")
    _require(action_count > 0, "workflow_actions_missing")
    return {"actions": action_count, "images": image_count}


def _base_report(environment: str) -> dict[str, object]:
    return {
        "report_version": REPORT_VERSION,
        "environment": environment,
        "cloud_provider": "none",
        "public_dns": False,
        "public_ca": False,
        "evidence_kind": "configuration_contract_only",
        "real_cloud_host_exercised": False,
    }


def _assert_report_safe(report: Mapping[str, object]) -> None:
    serialized = json.dumps(report, sort_keys=True)
    for marker in _SECRET_MARKERS:
        _require(marker not in serialized, "report_contains_secret_marker")
    _require(
        re.search(
            r"(?:^|[\s\"'])/(?:Users|etc|home|private|run|tmp|var)/",
            serialized,
        )
        is None,
        "report_contains_path",
    )


def _decode_asset(assets: Mapping[str, bytes], logical_name: str) -> str:
    try:
        return assets[logical_name].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeploymentContractError("validated_asset_not_utf8") from exc


def _validated_report(
    *,
    provenance: Mapping[str, object],
    assets: Mapping[str, bytes],
    environment: str,
) -> dict[str, object]:
    env_text = _decode_asset(assets, "environment_example")
    caddy_text = _decode_asset(assets, "caddyfile_example")
    guide_text = _decode_asset(assets, "deployment_guide")
    workflow_text = _decode_asset(assets, "workflow")
    seed_helper_source = _decode_asset(assets, "local_user_seed_helper")
    web_api_source = _decode_asset(assets, "web_api_source")

    values = parse_env_example(env_text)
    validate_environment(values)
    validate_role_separated_database_contract(values, guide_text)
    validate_caddyfile(caddy_text)
    validate_guide(guide_text)
    validate_seed_helper_contract(seed_helper_source)
    validate_web_trust_contract(web_api_source)
    pins = validate_supply_chain_pins(workflow_text)

    report = {
        **_base_report(environment),
        **provenance,
        "status": "passed",
        "checks": {
            "api_and_web_loopback": "passed",
            "database_role_and_tls_contract": "passed",
            "exact_https_origin": "passed",
            "proxy_trust_boundary": "passed",
            "local_user_seed_contract": "passed",
            "web_exact_origin_trust": "passed",
            "guide_claim_boundaries": "passed",
            "supply_chain_pins": {"status": "passed", **pins},
        },
        "blockers": [OWNER_RECEIPT_BLOCKER],
    }
    _assert_report_safe(report)
    return report


def run_smoke(*, root: Path = ROOT, environment: str = "local_validation") -> dict[str, object]:
    provenance, assets = repository_carrier_identity(root)
    return _validated_report(provenance=provenance, assets=assets, environment=environment)


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    _assert_report_safe(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--environment",
        choices=("local_validation", "ephemeral_ci"),
        default="local_validation",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    provenance: dict[str, object] = {}
    try:
        provenance, assets = repository_carrier_identity(args.root)
        report = _validated_report(
            provenance=provenance,
            assets=assets,
            environment=args.environment,
        )
        exit_code = 0
    except (DeploymentContractError, OSError) as exc:
        failure_code = exc.code if isinstance(exc, DeploymentContractError) else "deployment_asset_unreadable"
        report = {
            **_base_report(args.environment),
            **provenance,
            "status": "failed",
            "failure_codes": [failure_code],
            "blockers": [OWNER_RECEIPT_BLOCKER],
        }
        exit_code = 1
    _assert_report_safe(report)
    if args.output is not None:
        try:
            _write_report(args.output, report)
        except OSError:
            report = {
                **_base_report(args.environment),
                **provenance,
                "status": "failed",
                "failure_codes": ["receipt_write_failed"],
                "blockers": [OWNER_RECEIPT_BLOCKER],
            }
            exit_code = 1
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
