from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib

import pytest

from tests.unit.frozen_handoff_support import handoff_drift_problems


ROOT = Path(__file__).resolve().parents[2]
BASE = "c9d24243920a694eaf00ad595da392a1478710dd"
BASE_TREE = "ecc16a53f580308959e97e8b1f02edd04bbe3bfc"
FORMAT = "alice-v0.14.0-phase5-enterprise-track-explicit-carrier-v1"
HANDOFF_REL = "docs/handoff/2026-07-21-v0.14.0-phase5-enterprise-track"
HANDOFF = ROOT / HANDOFF_REL
CARRIER_PATHS = (
    ".github/workflows/deployment-guide-smoke.yml",
    ".github/workflows/ops-evidence.yml",
    ".github/workflows/tests.yml",
    ".gitignore",
    "Makefile",
    "README.md",
    "SECURITY.md",
    "apps/api/alembic/versions/20260721_0094_browser_clip_capabilities.py",
    "apps/api/src/alicebot_api/browser_clip_capabilities.py",
    "apps/api/src/alicebot_api/config.py",
    "apps/api/src/alicebot_api/main.py",
    "apps/api/src/alicebot_api/openapi_operation_contracts.py",
    "apps/api/src/alicebot_api/routers/vnext_memories.py",
    "apps/api/src/alicebot_api/sqlite_schema.py",
    "apps/api/src/alicebot_api/sqlite_store.py",
    "apps/api/src/alicebot_api/vnext_connectors.py",
    "apps/api/src/alicebot_api/vnext_secrets.py",
    "apps/api/src/alicebot_api/vnext_store.py",
    "apps/api/src/alicebot_api/vnext_stores/postgres/browser_clip_capabilities.py",
    "apps/api/src/alicebot_api/vnext_stores/sqlite/browser_clip_capabilities.py",
    "apps/web/app/approvals/loading.tsx",
    "apps/web/app/approvals/page.test.tsx",
    "apps/web/app/approvals/page.tsx",
    "apps/web/app/artifacts/loading.tsx",
    "apps/web/app/artifacts/page.test.tsx",
    "apps/web/app/artifacts/page.tsx",
    "apps/web/app/entities/loading.tsx",
    "apps/web/app/entities/page.test.tsx",
    "apps/web/app/entities/page.tsx",
    "apps/web/app/globals.css",
    "apps/web/app/memories/loading.tsx",
    "apps/web/app/memories/page.test.tsx",
    "apps/web/app/memories/page.tsx",
    "apps/web/app/traces/loading.tsx",
    "apps/web/app/traces/page.test.tsx",
    "apps/web/components/approval-detail.tsx",
    "apps/web/components/approval-list.tsx",
    "apps/web/components/artifact-chunk-list.tsx",
    "apps/web/components/artifact-detail.tsx",
    "apps/web/components/artifact-list.tsx",
    "apps/web/components/browser-clipper.test.ts",
    "apps/web/components/entity-detail.tsx",
    "apps/web/components/entity-edge-list.tsx",
    "apps/web/components/entity-list.tsx",
    "apps/web/components/memory-label-list.tsx",
    "apps/web/components/memory-review-lists.test.tsx",
    "apps/web/components/memory-revision-list.tsx",
    "apps/web/components/trace-list.tsx",
    "apps/web/components/vnext-brain-workspace.tsx",
    "apps/web/components/vnext-operator-auth.test.tsx",
    "apps/web/components/vnext-workspace-model.ts",
    "apps/web/lib/api.test.ts",
    "apps/web/lib/api.ts",
    "apps/web/test/browser/navigation.spec.ts",
    "apps/web/test/browser/review-dashboard-demo.spec.ts",
    "docs/alpha/README.md",
    "docs/alpha/agent-integration.md",
    "docs/alpha/backup-and-restore.md",
    "docs/alpha/demo-mode.md",
    "docs/alpha/first-run.md",
    "docs/alpha/headless-ubuntu-install.md",
    "docs/alpha/known-limitations.md",
    "docs/alpha/quickstart.md",
    "docs/alpha/review-dashboard-demo.md",
    "docs/alpha/security-and-privacy.md",
    "docs/deployment/single-tenant-self-hosted.md",
    f"{HANDOFF_REL}/ENGINEER_HANDOFF.md",
    f"{HANDOFF_REL}/FIX_MATRIX.md",
    f"{HANDOFF_REL}/README.md",
    "docs/runbooks/disaster-recovery.md",
    "docs/runbooks/health-and-monitoring.md",
    "docs/runbooks/upgrade-v0.12-to-current.md",
    "docs/runbooks/vnext-dogfood-daily-checklist.md",
    "docs/security/README.md",
    "docs/security/auth-authorization.md",
    "docs/security/dependency-posture.md",
    "docs/security/external-review-brief.md",
    "docs/security/input-validation.md",
    "docs/security/secrets-redaction.md",
    "docs/security/stage-a-evidence.md",
    "docs/security/threat-model.md",
    "docs/vnext/architecture.md",
    "docs/vnext/security-privacy.md",
    "packaging/cloud/Caddyfile.example",
    "packaging/cloud/single-tenant.env.example",
    "scripts/_phase5_ops_seed.py",
    "scripts/migrate.sh",
    "scripts/run_phase5_ops_evidence.py",
    "scripts/run_single_tenant_deployment_smoke.py",
    "tests/integration/test_browser_clip_capabilities.py",
    "tests/integration/test_default_surface_integration.py",
    "tests/integration/test_migrations.py",
    "tests/integration/test_provider_runtime_api.py",
    "tests/integration/test_review_dashboard_demo.py",
    "tests/integration/test_stage_a_agent_key_isolation.py",
    "tests/unit/test_20260721_0094_browser_clip_capabilities.py",
    "tests/unit/test_browser_clip_capabilities.py",
    "tests/unit/test_browser_clip_capability_storage.py",
    "tests/unit/test_config.py",
    "tests/unit/test_legacy_gated_router_split.py",
    "tests/unit/test_legacy_surface_test_posture.py",
    "tests/unit/test_main.py",
    "tests/unit/test_memories_legacy_router_split.py",
    "tests/unit/test_phase5_enterprise_handoff_truth.py",
    "tests/unit/test_phase5_ops_evidence.py",
    "tests/unit/test_providers_router_split.py",
    "tests/unit/test_runnable_docs_secret_argv.py",
    "tests/unit/test_single_tenant_deployment.py",
    "tests/unit/test_sqlite_store.py",
    "tests/unit/test_stage_a_vnext_auth_surface.py",
    "tests/unit/test_store_events_revisions_split.py",
    "tests/unit/test_store_graph_open_loops_split.py",
    "tests/unit/test_store_memory_access_split.py",
    "tests/unit/test_store_memory_lifecycle_split.py",
    "tests/unit/test_surface_gates.py",
    "tests/unit/test_vnext_agent_keys.py",
    "tests/unit/test_vnext_connectors.py",
    "tests/unit/test_vnext_main.py",
    "tests/unit/test_vnext_production_proxy_auth.py",
    "tests/unit/test_vnext_release_polish.py",
    "tests/unit/test_vnext_secrets.py",
    "tests/unit/test_workspaces_router_split.py",
)
RECEIPT_EXCLUSIONS = (
    f"{HANDOFF_REL}/BUILD_REPORT.md",
    f"{HANDOFF_REL}/REVIEW_REPORT.md",
)
PROTECTED_SQLITE_PATH = "apps/api/src/alicebot_api/vnext_stores/sqlite/memory_access.py"
Record = tuple[bytes, bytes, bytes]
RecordReader = Callable[[str], Record]


def _git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", "-C", str(ROOT), *arguments),
        check=check,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _base_history_available() -> bool:
    probe = _git("cat-file", "-e", f"{BASE}^{{commit}}", check=False)
    if probe.returncode == 0:
        return True
    shallow = _git("rev-parse", "--is-shallow-repository", check=False)
    assert shallow.returncode == 0 and shallow.stdout.strip() == b"true", (
        "phase 5 base commit is missing from a checkout that is not verified shallow"
    )
    return False


def _require_full_git_history() -> None:
    if not _base_history_available():
        pytest.skip(
            "phase 5 base commit unavailable in this checkout (shallow CI "
            "clone); the full-history ops workflow owns ancestry assertions"
        )


def _field(name: bytes, value: bytes) -> bytes:
    return name + b"\0" + len(value).to_bytes(8, "big") + value


def _base_mode(relative_path: str) -> bytes:
    entry = _git("ls-tree", "-z", BASE, "--", relative_path).stdout
    assert entry, relative_path
    return entry.split(b" ", 1)[0]


def _read_live_record(
    relative_path: str,
    *,
    root: Path = ROOT,
    base_mode_reader: Callable[[str], bytes] = _base_mode,
) -> Record:
    parts = relative_path.encode("utf-8").split(b"/")
    assert parts and all(part not in {b"", b".", b".."} for part in parts)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    assert nofollow is not None and directory is not None
    try:
        parent_fd = os.open(root, os.O_RDONLY | directory | nofollow)
    except OSError as exc:
        raise AssertionError(f"unsafe carrier root: {root}") from exc
    try:
        for component in parts[:-1]:
            try:
                component_info = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return base_mode_reader(relative_path), b"deleted", b""
            assert stat.S_ISDIR(component_info.st_mode), (
                f"unsafe carrier parent component: {relative_path}"
            )
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise AssertionError(
                    f"carrier parent changed during receipt: {relative_path}"
                ) from exc
            child_info = os.fstat(child_fd)
            try:
                assert stat.S_ISDIR(child_info.st_mode)
                assert (component_info.st_dev, component_info.st_ino) == (
                    child_info.st_dev,
                    child_info.st_ino,
                ), f"carrier parent changed during receipt: {relative_path}"
            except BaseException:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd

        file_name = parts[-1]
        try:
            info = os.stat(file_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return base_mode_reader(relative_path), b"deleted", b""
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(file_name, dir_fd=parent_fd)
            return b"120000", b"symlink", (
                target if isinstance(target, bytes) else os.fsencode(target)
            )
        assert stat.S_ISREG(info.st_mode), (
            f"unsupported carrier entry type: {relative_path}"
        )
        try:
            descriptor = os.open(file_name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
        except OSError as exc:
            raise AssertionError(
                f"carrier entry changed during receipt: {relative_path}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            assert stat.S_ISREG(before.st_mode)
            assert (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino), (
                f"carrier entry changed during receipt: {relative_path}"
            )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        stable = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        assert all(getattr(before, key) == getattr(after, key) for key in stable), (
            relative_path
        )
        mode = b"100755" if before.st_mode & stat.S_IXUSR else b"100644"
        return mode, b"regular", b"".join(chunks)
    finally:
        os.close(parent_fd)


def _read_commit_record(commit: str, relative_path: str) -> Record:
    entry = _git("ls-tree", "-z", commit, "--", relative_path).stdout
    if not entry:
        return _base_mode(relative_path), b"deleted", b""
    metadata, actual_path = entry.rstrip(b"\0").split(b"\t", 1)
    mode, object_type, _object_id = metadata.split(b" ", 2)
    assert object_type == b"blob" and actual_path.decode() == relative_path
    payload = _git("show", f"{commit}:{relative_path}").stdout
    return mode, b"symlink" if mode == b"120000" else b"regular", payload


def _build_receipt(reader: RecordReader) -> bytes:
    parts = (
        _field(b"format", FORMAT.encode()),
        _field(b"base", BASE.encode()),
        _field(b"base_tree", BASE_TREE.encode()),
    )
    output = bytearray(b"".join(parts))
    for relative_path in CARRIER_PATHS:
        mode, kind, payload = reader(relative_path)
        output.extend(_field(b"path", relative_path.encode()))
        output.extend(_field(b"mode", mode))
        output.extend(_field(b"kind", kind))
        output.extend(_field(b"sha256", hashlib.sha256(payload).hexdigest().encode()))
    return bytes(output)


def build_live_receipt() -> bytes:
    return _build_receipt(_read_live_record)


def _receipt_from_report(text: str) -> str:
    match = re.search(r"carrier receipt sha256:\s*`([0-9a-f]{64})`", text)
    assert match is not None, "BUILD_REPORT.md lacks the frozen carrier receipt"
    return match.group(1)


def _carrier_paths_from_report(text: str) -> tuple[str, ...]:
    match = re.search(
        r"Receipt-listed paths \(122, bytewise sorted\):\n\n```text\n(.*?)\n```",
        text,
        flags=re.DOTALL,
    )
    assert match is not None, "BUILD_REPORT.md lacks the explicit carrier path list"
    return tuple(match.group(1).splitlines())


def _live_dirty_paths() -> set[str]:
    changed = _git("diff", "--name-only", "-z", BASE, "--").stdout.split(b"\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
    return {
        path.decode("utf-8", errors="surrogateescape")
        for path in (*changed, *untracked)
        if path
    }


def _worktree_delta_paths() -> set[str]:
    changed = _git("diff", "--name-only", "-z", "HEAD", "--").stdout.split(b"\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(
        b"\0"
    )
    return {
        path.decode("utf-8", errors="surrogateescape")
        for path in (*changed, *untracked)
        if path
    }


def _historical_handoff_delta_paths(paths: set[str]) -> set[str]:
    prefix = f"{HANDOFF_REL}/"
    return {
        path
        for path in paths
        if path == HANDOFF_REL or path.startswith(prefix)
    }


def _carrier_commit(receipt: str) -> str | None:
    commits = _git(
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep=Alice-Carrier-Receipt-SHA256: {receipt}",
        "HEAD",
        check=False,
    )
    if commits.returncode != 0:
        return None
    matches = commits.stdout.decode().split()
    assert len(matches) <= 1, f"duplicate carrier receipt commits: {matches}"
    return matches[0] if matches else None


def _assert_carrier_directly_descends_from_base(carrier: str) -> None:
    carrier_line = (
        _git("rev-list", "--parents", "-n", "1", carrier).stdout.decode().split()
    )
    assert carrier_line == [carrier, BASE], (
        "the replacement carrier must be one fresh commit directly on the Phase 5 base"
    )


def _assert_commit_report_receipts(commit: str, receipt: str) -> None:
    message = _git("show", "-s", "--format=%B", commit).stdout.decode()
    assert f"Alice-Carrier-Receipt-SHA256: {receipt}" in message
    for filename, trailer in (
        ("BUILD_REPORT.md", "Alice-Build-Report-SHA256"),
        ("REVIEW_REPORT.md", "Alice-Review-Report-SHA256"),
    ):
        payload = _git("show", f"{commit}:{HANDOFF_REL}/{filename}").stdout
        expected = hashlib.sha256(payload).hexdigest()
        assert f"{trailer}: {expected}" in message


def _assert_integrated_handoff_immutable(carrier: str, *, head: str = "HEAD") -> None:
    # The README may gain the dated correction of 2026-10-04 and nothing else.
    drift = handoff_drift_problems(_git, carrier, head, HANDOFF_REL)
    assert not drift, (
        f"the integrated Phase 5 handoff changed after its receipt-trailed carrier commit: {drift}"
    )
    for filename in ("BUILD_REPORT.md", "REVIEW_REPORT.md"):
        relative_path = f"{HANDOFF_REL}/{filename}"
        carrier_payload = _git("show", f"{carrier}:{relative_path}").stdout
        head_payload = _git("show", f"{head}:{relative_path}").stdout
        assert head_payload == carrier_payload, f"post-carrier report drift: {filename}"


def _assert_historical_protected_scope_immutable(carrier: str) -> None:
    protected_diff = _git(
        "diff",
        "--quiet",
        BASE,
        carrier,
        "--",
        PROTECTED_SQLITE_PATH,
        check=False,
    )
    assert protected_diff.returncode == 0, (
        "the protected SQLite memory-access path changed during Phase 5"
    )


def _is_ignored(relative_path: str) -> bool:
    result = _git("check-ignore", "--no-index", "--quiet", relative_path, check=False)
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


def test_phase5_handoff_package_and_owner_boundaries() -> None:
    assert {path.name for path in HANDOFF.iterdir()} == {
        "README.md",
        "FIX_MATRIX.md",
        "BUILD_REPORT.md",
        "ENGINEER_HANDOFF.md",
        "REVIEW_REPORT.md",
    }
    documents = {
        name: (HANDOFF / name).read_text(encoding="utf-8")
        for name in (
            "README.md",
            "FIX_MATRIX.md",
            "BUILD_REPORT.md",
            "ENGINEER_HANDOFF.md",
            "REVIEW_REPORT.md",
        )
    }
    review = documents["REVIEW_REPORT.md"]
    assert "Code carrier" in review
    assert "Phase 5 completion" in review
    assert "5.4 OWNER GATE OPEN" in review
    assert "COMMITTED-SHA CI GATE OPEN" in review
    assert "no remaining P0-P3" in review
    for name in documents:
        normalized = " ".join(documents[name].split())
        lowered = normalized.lower()
        assert "NO-GO pending the 5.4 owner gate and green committed-SHA CI" in normalized, name
        assert "Stage A" in normalized, name
        assert "Stage B" in normalized, name
        assert "automated security scanning under OpenAI Trusted Access" in normalized, name
        assert "plus internal adversarial review" in normalized, name
        assert "5.1.c OWNER GATE OPEN" not in normalized, name
        assert "has been independently audited" not in lowered, name
        assert "external audit completed" not in lowered, name
        assert "5.4 OWNER GATE OPEN" in normalized, name
        assert "COMMITTED-SHA CI GATE OPEN" in normalized, name


def test_phase5_ops_workflow_runs_truth_guard_with_full_history() -> None:
    workflow = (ROOT / ".github/workflows/ops-evidence.yml").read_text(encoding="utf-8")
    assert (
        "      - name: Checkout full history\n"
        "        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7\n"
        "        with:\n"
        "          fetch-depth: 0\n"
    ) in workflow
    evidence_step = workflow.split(
        "      - name: Run evidence contract tests\n", 1
    )[1].split("\n      - name:", 1)[0]
    assert "./.venv/bin/python -m pytest" in evidence_step
    assert "tests/unit/test_phase5_ops_evidence.py" in evidence_step
    assert "tests/unit/test_phase5_enterprise_handoff_truth.py" in evidence_step
    assert "-q -p no:cacheprovider" in evidence_step
    assert ".github/workflows/ops-evidence.yml" in CARRIER_PATHS
    assert "tests/unit/test_phase5_enterprise_handoff_truth.py" in CARRIER_PATHS


def test_phase5_versions_and_protected_scope_are_bound_to_the_carrier() -> None:
    python_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    web_version = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))[
        "version"
    ]
    assert python_version == web_version
    assert "pyproject.toml" not in CARRIER_PATHS
    assert "apps/web/package.json" not in CARRIER_PATHS
    assert PROTECTED_SQLITE_PATH not in CARRIER_PATHS
    assert all(not path.startswith("docs/release/") for path in CARRIER_PATHS)
    assert _git("diff", "--quiet", "HEAD", "--", "docs/release").returncode == 0
    assert _git("diff", "--quiet", "HEAD", "--", PROTECTED_SQLITE_PATH).returncode == 0


def test_phase5_base_tree_and_historical_scope_are_bound_to_the_carrier() -> None:
    _require_full_git_history()
    assert _git("rev-parse", f"{BASE}^{{tree}}").stdout.decode().strip() == BASE_TREE
    head = _git("rev-parse", "HEAD").stdout.decode().strip()
    if head == BASE:
        python_version = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        assert python_version == "0.13.1"
        assert _git("diff", "--quiet", BASE, "--", "docs/release").returncode == 0
        assert _git("diff", "--quiet", BASE, "--", PROTECTED_SQLITE_PATH).returncode == 0
    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    carrier = _carrier_commit(_receipt_from_report(report))
    assert carrier is not None
    prior_handoff_changes = (
        _git("diff", "--name-only", BASE, carrier, "--", "docs/handoff")
        .stdout.decode()
        .splitlines()
    )
    assert all(path.startswith(f"{HANDOFF_REL}/") for path in prior_handoff_changes)


def test_phase5_history_probe_degrades_only_for_a_verified_shallow_clone(
    monkeypatch,
) -> None:
    def shallow_git(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del check
        if arguments[0] == "cat-file":
            return subprocess.CompletedProcess(arguments, 128, b"", b"missing")
        assert arguments == ("rev-parse", "--is-shallow-repository")
        return subprocess.CompletedProcess(arguments, 0, b"true\n", b"")

    monkeypatch.setattr(f"{__name__}._git", shallow_git)
    assert _base_history_available() is False

    def full_git(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del check
        if arguments[0] == "cat-file":
            return subprocess.CompletedProcess(arguments, 128, b"", b"missing")
        return subprocess.CompletedProcess(arguments, 0, b"false\n", b"")

    monkeypatch.setattr(f"{__name__}._git", full_git)
    with pytest.raises(AssertionError, match="not verified shallow"):
        _base_history_available()


def test_phase5_receipt_exclusions_and_ignore_rules_are_exact() -> None:
    assert RECEIPT_EXCLUSIONS == (
        f"{HANDOFF_REL}/BUILD_REPORT.md",
        f"{HANDOFF_REL}/REVIEW_REPORT.md",
    )
    assert all(not _is_ignored(path) for path in RECEIPT_EXCLUSIONS)
    assert _is_ignored("BUILD_REPORT.md")
    assert _is_ignored("REVIEW_REPORT.md")
    assert tuple(sorted(CARRIER_PATHS, key=lambda value: value.encode())) == CARRIER_PATHS
    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    assert _carrier_paths_from_report(report) == CARRIER_PATHS


def test_phase5_live_carrier_scope_and_receipt() -> None:
    head = _git("rev-parse", "HEAD").stdout.decode().strip()
    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    receipt = _receipt_from_report(report)
    if head == BASE:
        assert _git("diff", "--cached", "--quiet").returncode == 0
        expected_paths = set(CARRIER_PATHS) | set(RECEIPT_EXCLUSIONS)
        assert _live_dirty_paths() == expected_paths
        assert hashlib.sha256(build_live_receipt()).hexdigest() == receipt
        return

    handoff_delta = _historical_handoff_delta_paths(_worktree_delta_paths())
    assert not handoff_delta, (
        "integrated checkout has live historical handoff drift: "
        f"{sorted(handoff_delta)}"
    )

    # A shallow PR checkout can still prove the carrier bytes when HEAD itself
    # is the receipt-trailed carrier. Full ancestry is proved separately in the
    # fetch-depth:0 Phase 5 ops workflow.
    message = _git("show", "-s", "--format=%B", "HEAD").stdout.decode()
    if f"Alice-Carrier-Receipt-SHA256: {receipt}" not in message:
        pytest.skip("live carrier is integrated; full-history receipt test owns ancestry")
    assert hashlib.sha256(_build_receipt(lambda path: _read_commit_record("HEAD", path))).hexdigest() == receipt
    _assert_commit_report_receipts("HEAD", receipt)


def test_phase5_integrated_carrier_is_ancestry_and_content_bound() -> None:
    head = _git("rev-parse", "HEAD").stdout.decode().strip()
    if head == BASE:
        pytest.skip("uncommitted carrier: live receipt test is authoritative")
    _require_full_git_history()

    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    receipt = _receipt_from_report(report)
    carrier = _carrier_commit(receipt)
    assert carrier is not None
    _assert_carrier_directly_descends_from_base(carrier)
    assert _git("merge-base", "--is-ancestor", BASE, carrier, check=False).returncode == 0
    assert _git("merge-base", "--is-ancestor", carrier, "HEAD", check=False).returncode == 0
    changed = set(_git("diff", "--name-only", BASE, carrier, "--").stdout.decode().splitlines())
    assert changed == set(CARRIER_PATHS) | set(RECEIPT_EXCLUSIONS)
    integrated = _build_receipt(lambda path: _read_commit_record(carrier, path))
    assert hashlib.sha256(integrated).hexdigest() == receipt
    _assert_commit_report_receipts(carrier, receipt)
    _assert_historical_protected_scope_immutable(carrier)
    _assert_integrated_handoff_immutable(carrier)

    python_at_carrier = tomllib.loads(_git("show", f"{carrier}:pyproject.toml").stdout.decode())[
        "project"
    ]["version"]
    web_at_carrier = json.loads(
        _git("show", f"{carrier}:apps/web/package.json").stdout.decode()
    )["version"]
    assert python_at_carrier == web_at_carrier == "0.13.1"


def test_phase5_receipt_records_fail_on_old_bytes_modes_deletions_and_symlinks(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "carrier.txt"
    candidate.write_bytes(b"first\n")
    base_mode_reader = lambda _path: b"100644"
    first = _read_live_record(
        "carrier.txt",
        root=tmp_path,
        base_mode_reader=base_mode_reader,
    )
    candidate.write_bytes(b"second\n")
    second = _read_live_record(
        "carrier.txt",
        root=tmp_path,
        base_mode_reader=base_mode_reader,
    )
    assert first != second

    candidate.chmod(0o755)
    executable = _read_live_record(
        "carrier.txt",
        root=tmp_path,
        base_mode_reader=base_mode_reader,
    )
    assert executable[0] == b"100755" and executable != second

    candidate.unlink()
    os.symlink("target-that-is-never-followed", candidate)
    linked = _read_live_record(
        "carrier.txt",
        root=tmp_path,
        base_mode_reader=base_mode_reader,
    )
    assert linked == (b"120000", b"symlink", b"target-that-is-never-followed")

    candidate.unlink()
    deleted = _read_live_record(
        "carrier.txt",
        root=tmp_path,
        base_mode_reader=base_mode_reader,
    )
    assert deleted == (b"100644", b"deleted", b"")

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "nested.txt").write_text("payload\n", encoding="utf-8")
    os.symlink("real-parent", tmp_path / "linked-parent", target_is_directory=True)
    with pytest.raises(AssertionError, match="unsafe carrier parent component"):
        _read_live_record(
            "linked-parent/nested.txt",
            root=tmp_path,
            base_mode_reader=base_mode_reader,
        )


def test_phase5_integrated_handoff_rejects_post_carrier_drift(monkeypatch) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        observed.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, b"", b"")

    monkeypatch.setattr(f"{__name__}._git", fake_git)
    with pytest.raises(AssertionError, match="handoff changed"):
        _assert_integrated_handoff_immutable("carrier-commit")
    assert observed[0] == ("diff", "--quiet", "carrier-commit", "HEAD", "--", HANDOFF_REL)


def test_phase5_integrated_carrier_rejects_a_child_of_the_failed_carrier(
    monkeypatch,
) -> None:
    def fake_git(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del check
        assert arguments == ("rev-list", "--parents", "-n", "1", "replacement")
        return subprocess.CompletedProcess(
            arguments,
            0,
            b"replacement failed-carrier\n",
            b"",
        )

    monkeypatch.setattr(f"{__name__}._git", fake_git)
    with pytest.raises(AssertionError, match="fresh commit directly"):
        _assert_carrier_directly_descends_from_base("replacement")


def test_phase5_integrated_live_delta_allows_new_carriers_but_not_old_handoff_drift() -> None:
    unrelated = {
        ".coverage.ci-host.pid123.Xabc123.Habcdefghijh",
        "temporary-test-artifact.txt",
    }
    assert _historical_handoff_delta_paths(unrelated) == set()

    historical_source_path = CARRIER_PATHS[0]
    historical_doc_path = "docs/alpha/README.md"
    later_handoff_path = (
        "docs/handoff/2026-07-24-v0.14.0-deployment-guide-fixes/README.md"
    )
    assert historical_doc_path in CARRIER_PATHS
    report_path = RECEIPT_EXCLUSIONS[0]
    assert (
        _historical_handoff_delta_paths(
            {
                *unrelated,
                historical_source_path,
                historical_doc_path,
                later_handoff_path,
            }
        )
        == set()
    )
    assert _historical_handoff_delta_paths(
        {*unrelated, historical_source_path, report_path}
    ) == {report_path}


def test_phase5_integrated_handoff_rejects_report_byte_drift(monkeypatch) -> None:
    def fake_git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        if arguments[0] == "diff":
            return subprocess.CompletedProcess(arguments, 0, b"", b"")
        commit_spec = arguments[1]
        payload = b"carrier report" if commit_spec.startswith("carrier-commit:") else b"drift"
        return subprocess.CompletedProcess(arguments, 0, payload, b"")

    monkeypatch.setattr(f"{__name__}._git", fake_git)
    with pytest.raises(AssertionError, match="post-carrier report drift"):
        _assert_integrated_handoff_immutable("carrier-commit")


def test_phase5_integrated_carrier_rejects_protected_path_drift(monkeypatch) -> None:
    observed: list[tuple[str, ...]] = []

    def fake_git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        del check
        observed.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            1,
            b"",
            b"",
        )

    monkeypatch.setattr(f"{__name__}._git", fake_git)
    with pytest.raises(AssertionError, match="protected SQLite memory-access path"):
        _assert_historical_protected_scope_immutable("carrier-commit")
    assert observed == [
        ("diff", "--quiet", BASE, "carrier-commit", "--", PROTECTED_SQLITE_PATH),
    ]
