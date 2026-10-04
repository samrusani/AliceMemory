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
BASE = "b383f6e69896717dfb60b887747e304c33f70d5b"
BASE_TREE = "faec22103b6bdee8650513f0c4c6aa28b7e5b912"
FORMAT = "alice-v0.14.0-deployment-guide-fixes-explicit-carrier-v1"
HANDOFF_REL = "docs/handoff/2026-07-24-v0.14.0-deployment-guide-fixes"
HANDOFF = ROOT / HANDOFF_REL
CARRIER_PATHS = (
    ".github/workflows/ops-evidence.yml",
    ".gitignore",
    "apps/api/alembic/versions/20260721_0093_artifact_quality_rating_reviewer_unique.py",
    "docs/alpha/backup-and-restore.md",
    "docs/deployment/single-tenant-self-hosted.md",
    f"{HANDOFF_REL}/ENGINEER_HANDOFF.md",
    f"{HANDOFF_REL}/FIX_MATRIX.md",
    f"{HANDOFF_REL}/README.md",
    "docs/runbooks/disaster-recovery.md",
    "packaging/cloud/Caddyfile.example",
    "packaging/cloud/single-tenant.env.example",
    "scripts/_phase5_ops_seed.py",
    "scripts/check_control_doc_truth.py",
    "scripts/install-ubuntu.sh",
    "scripts/run_phase5_ops_evidence.py",
    "scripts/run_single_tenant_deployment_smoke.py",
    "scripts/seed_local_user.py",
    "tests/integration/conftest.py",
    "tests/integration/test_local_workspace_bootstrap_api.py",
    "tests/unit/test_20260721_0093_artifact_quality_rating_reviewer_unique.py",
    "tests/unit/test_control_doc_truth.py",
    "tests/unit/test_least_privilege_deployment_workflow.py",
    "tests/unit/test_phase5_enterprise_handoff_truth.py",
    "tests/unit/test_phase5_ops_evidence.py",
    "tests/unit/test_seed_local_user.py",
    "tests/unit/test_single_tenant_deployment.py",
    "tests/unit/test_v0140_deployment_guide_fixes_handoff_truth.py",
    "tests/unit/test_vnext_release_polish.py",
)
RECEIPT_EXCLUSIONS = (
    f"{HANDOFF_REL}/BUILD_REPORT.md",
    f"{HANDOFF_REL}/REVIEW_REPORT.md",
)
SECURITY_CLAIM = "automated security scanning and internal adversarial review, findings triaged and fixed"
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
        "deployment-fixes base is missing from a checkout that is not verified shallow"
    )
    return False


def _field(name: bytes, value: bytes) -> bytes:
    return name + b"\0" + len(value).to_bytes(8, "big") + value


def _base_mode(relative_path: str) -> bytes:
    entry = _git("ls-tree", "-z", BASE, "--", relative_path).stdout
    return entry.split(b" ", 1)[0] if entry else b"000000"


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
            assert stat.S_ISDIR(component_info.st_mode), f"unsafe carrier parent component: {relative_path}"
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise AssertionError(f"carrier parent changed during receipt: {relative_path}") from exc
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
            return b"120000", b"symlink", (target if isinstance(target, bytes) else os.fsencode(target))
        assert stat.S_ISREG(info.st_mode), f"unsupported carrier entry type: {relative_path}"
        try:
            descriptor = os.open(file_name, os.O_RDONLY | nofollow, dir_fd=parent_fd)
        except OSError as exc:
            raise AssertionError(f"carrier entry changed during receipt: {relative_path}") from exc
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
        stable = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        assert all(getattr(before, key) == getattr(after, key) for key in stable), relative_path
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
    output = bytearray(
        b"".join(
            (
                _field(b"format", FORMAT.encode()),
                _field(b"base", BASE.encode()),
                _field(b"base_tree", BASE_TREE.encode()),
            )
        )
    )
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
        rf"Receipt-listed paths \({len(CARRIER_PATHS)}, bytewise sorted\):"
        r"\n\n```text\n(.*?)\n```",
        text,
        flags=re.DOTALL,
    )
    assert match is not None, "BUILD_REPORT.md lacks the explicit carrier path list"
    return tuple(match.group(1).splitlines())


def _live_dirty_paths() -> set[str]:
    changed = _git("diff", "--name-only", "-z", BASE, "--").stdout.split(b"\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
    return {path.decode("utf-8", errors="surrogateescape") for path in (*changed, *untracked) if path}


def _worktree_delta_paths() -> set[str]:
    changed = _git("diff", "--name-only", "-z", "HEAD", "--").stdout.split(b"\0")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0")
    return {path.decode("utf-8", errors="surrogateescape") for path in (*changed, *untracked) if path}


def _handoff_delta_paths(paths: set[str]) -> set[str]:
    prefix = f"{HANDOFF_REL}/"
    return {path for path in paths if path == HANDOFF_REL or path.startswith(prefix)}


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
    carrier_line = _git("rev-list", "--parents", "-n", "1", carrier).stdout.decode().split()
    assert carrier_line == [carrier, BASE], "deployment-fixes carrier must be one fresh commit directly on its base"


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


def _assert_integrated_handoff_immutable(
    carrier: str,
    *,
    head: str = "HEAD",
) -> None:
    # The README may gain the dated correction of 2026-10-04 and nothing else.
    drift = handoff_drift_problems(_git, carrier, head, HANDOFF_REL)
    assert not drift, (
        f"the integrated deployment-fixes handoff changed after its receipt-trailed carrier commit: {drift}"
    )
    for filename in ("BUILD_REPORT.md", "REVIEW_REPORT.md"):
        relative_path = f"{HANDOFF_REL}/{filename}"
        carrier_payload = _git("show", f"{carrier}:{relative_path}").stdout
        head_payload = _git("show", f"{head}:{relative_path}").stdout
        assert head_payload == carrier_payload, f"post-carrier report drift: {filename}"


def _is_ignored(relative_path: str) -> bool:
    result = _git("check-ignore", "--no-index", "--quiet", relative_path, check=False)
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


def test_deployment_fixes_handoff_package_and_claim_boundary() -> None:
    expected = {
        "README.md",
        "FIX_MATRIX.md",
        "BUILD_REPORT.md",
        "ENGINEER_HANDOFF.md",
    }
    review_path = HANDOFF / "REVIEW_REPORT.md"
    if review_path.exists():
        expected.add("REVIEW_REPORT.md")
    assert {path.name for path in HANDOFF.iterdir()} == expected

    documents = {path.name: path.read_text(encoding="utf-8") for path in HANDOFF.iterdir()}
    for name, document in documents.items():
        normalized = " ".join(document.split())
        lowered = normalized.casefold()
        assert SECURITY_CLAIM in normalized, name
        assert "\N{EM DASH}" not in document, name
        assert "independently audited" not in lowered, name
        assert "third-party audited" not in lowered, name
        assert "penetration tested" not in lowered, name
    if review_path.exists():
        review = documents["REVIEW_REPORT.md"]
        assert "Code carrier" in review
        assert "independent" in review.casefold()

    engineer = documents["ENGINEER_HANDOFF.md"]
    for heading in (
        "## Compatibility Impact",
        "## Validation",
        "## Rollback",
        "## Operator Action",
    ):
        assert heading in engineer
    assert "- [x] Memory schema\n" in engineer
    assert "- [x] Continuity APIs\n" in engineer


def test_deployment_fixes_base_version_and_protected_scope() -> None:
    if _base_history_available():
        assert _git("rev-parse", f"{BASE}^{{tree}}").stdout.decode().strip() == BASE_TREE
    assert tuple(sorted(CARRIER_PATHS, key=lambda value: value.encode())) == CARRIER_PATHS
    assert "pyproject.toml" not in CARRIER_PATHS
    assert "apps/web/package.json" not in CARRIER_PATHS
    assert all(not path.startswith("docs/release/") for path in CARRIER_PATHS)
    assert all(not path.startswith("docs/security/") for path in CARRIER_PATHS)
    assert all(
        not (path.startswith("docs/handoff/") and not path.startswith(f"{HANDOFF_REL}/")) for path in CARRIER_PATHS
    )

    python_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    web_version = json.loads((ROOT / "apps/web/package.json").read_text(encoding="utf-8"))["version"]
    assert python_version == web_version
    if _git("rev-parse", "HEAD").stdout.decode().strip() == BASE:
        assert python_version == "0.13.1"


def test_deployment_fixes_receipt_exclusions_and_report_manifest_are_exact() -> None:
    assert RECEIPT_EXCLUSIONS == (
        f"{HANDOFF_REL}/BUILD_REPORT.md",
        f"{HANDOFF_REL}/REVIEW_REPORT.md",
    )
    assert all(not _is_ignored(path) for path in RECEIPT_EXCLUSIONS)
    assert _is_ignored("BUILD_REPORT.md")
    assert _is_ignored("REVIEW_REPORT.md")
    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    assert _carrier_paths_from_report(report) == CARRIER_PATHS


def test_deployment_fixes_full_history_workflow_runs_truth_guard() -> None:
    workflow = (ROOT / ".github/workflows/ops-evidence.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in workflow
    evidence_step = workflow.split(
        "      - name: Run evidence contract tests\n",
        1,
    )[1].split("\n      - name:", 1)[0]
    assert "tests/unit/test_v0140_deployment_guide_fixes_handoff_truth.py" in (evidence_step)


def test_deployment_fixes_live_receipt_or_integrated_carrier() -> None:
    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    receipt = _receipt_from_report(report)
    history_available = _base_history_available()
    carrier = _carrier_commit(receipt) if history_available else None
    if carrier is None:
        if not history_available:
            message = _git("show", "-s", "--format=%B", "HEAD").stdout.decode()
            if f"Alice-Carrier-Receipt-SHA256: {receipt}" not in message:
                pytest.skip("verified shallow checkout cannot locate the historical carrier")
            assert hashlib.sha256(_build_receipt(lambda path: _read_commit_record("HEAD", path))).hexdigest() == receipt
            _assert_commit_report_receipts("HEAD", receipt)
            return

        assert _git("rev-parse", "HEAD").stdout.decode().strip() == BASE
        assert _git("diff", "--cached", "--quiet").returncode == 0
        present_exclusions = {path for path in RECEIPT_EXCLUSIONS if (ROOT / path).exists()}
        expected_paths = set(CARRIER_PATHS) | present_exclusions
        assert _live_dirty_paths() == expected_paths
        assert hashlib.sha256(build_live_receipt()).hexdigest() == receipt
        return

    handoff_delta = _handoff_delta_paths(_worktree_delta_paths())
    assert not handoff_delta, f"integrated checkout has live handoff drift: {sorted(handoff_delta)}"


def test_deployment_fixes_integrated_carrier_is_ancestry_and_content_bound() -> None:
    if not _base_history_available():
        pytest.skip("full-history ops workflow owns deployment-fixes ancestry assertions")
    report = (HANDOFF / "BUILD_REPORT.md").read_text(encoding="utf-8")
    receipt = _receipt_from_report(report)
    carrier = _carrier_commit(receipt)
    if carrier is None:
        pytest.skip("uncommitted carrier: live receipt test is authoritative")

    _assert_carrier_directly_descends_from_base(carrier)
    assert _git("merge-base", "--is-ancestor", BASE, carrier, check=False).returncode == 0
    assert _git("merge-base", "--is-ancestor", carrier, "HEAD", check=False).returncode == 0
    changed = set(_git("diff", "--name-only", BASE, carrier, "--").stdout.decode().splitlines())
    assert changed == set(CARRIER_PATHS) | set(RECEIPT_EXCLUSIONS)
    integrated = _build_receipt(lambda path: _read_commit_record(carrier, path))
    assert hashlib.sha256(integrated).hexdigest() == receipt
    _assert_commit_report_receipts(carrier, receipt)
    _assert_integrated_handoff_immutable(carrier)

    python_at_carrier = tomllib.loads(_git("show", f"{carrier}:pyproject.toml").stdout.decode())["project"]["version"]
    web_at_carrier = json.loads(_git("show", f"{carrier}:apps/web/package.json").stdout.decode())["version"]
    assert python_at_carrier == web_at_carrier == "0.13.1"


def test_deployment_fixes_receipt_detects_bytes_modes_and_links(
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
    assert linked == (
        b"120000",
        b"symlink",
        b"target-that-is-never-followed",
    )


def test_deployment_fixes_rejects_altered_lineage(monkeypatch) -> None:
    def fake_git(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del check
        assert arguments == ("rev-list", "--parents", "-n", "1", "carrier")
        return subprocess.CompletedProcess(
            arguments,
            0,
            b"carrier unexpected-parent\n",
            b"",
        )

    monkeypatch.setattr(f"{__name__}._git", fake_git)
    with pytest.raises(AssertionError, match="directly on its base"):
        _assert_carrier_directly_descends_from_base("carrier")


def test_deployment_fixes_allows_later_source_edits_but_rejects_handoff_drift(
    monkeypatch,
) -> None:
    later_source = CARRIER_PATHS[0]
    assert _handoff_delta_paths({later_source, "future/source.py"}) == set()
    report_path = RECEIPT_EXCLUSIONS[0]
    assert _handoff_delta_paths({later_source, report_path}) == {report_path}

    observed: list[tuple[str, ...]] = []

    def fake_git(
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del check
        observed.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, b"", b"")

    monkeypatch.setattr(f"{__name__}._git", fake_git)
    with pytest.raises(AssertionError, match="handoff changed"):
        _assert_integrated_handoff_immutable("carrier")
    assert observed[0] == ("diff", "--quiet", "carrier", "HEAD", "--", HANDOFF_REL)
