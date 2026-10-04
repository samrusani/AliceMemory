from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest

import scripts.check_control_doc_truth as control_doc_truth


def _checksum_manifest(version: str, digit: str = "0") -> str:
    return (
        digit * 64 + f"  alice_memory-{version}-py3-none-any.whl\n" + digit * 64 + f"  alice_memory-{version}.tar.gz\n"
    )


def _seed_truth_docs(
    tmp_path: Path,
    *,
    published: bool = False,
    version: str = "9.8.7",
    latest_published_version: str | None = None,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "alice-memory"\nversion = "{version}"\nreadme = "docs/pypi-description.md"\n',
        encoding="utf-8",
    )
    web_package = tmp_path / "apps" / "web" / "package.json"
    web_package.parent.mkdir(parents=True, exist_ok=True)
    web_package.write_text(f'{{"name":"alice-web","version":"{version}"}}\n', encoding="utf-8")
    description = tmp_path / control_doc_truth.PACKAGE_DESCRIPTION_RELATIVE_PATH
    description.parent.mkdir(parents=True, exist_ok=True)
    description.write_text(
        "# Alice Memory\n\nEvergreen package description.\n",
        encoding="utf-8",
    )
    for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES:
        doc_path = tmp_path / rule.relative_path
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("\n".join(rule.required_markers) + "\n", encoding="utf-8")
    release_dir = tmp_path / "docs" / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    publication_status = "published" if published else "pending"
    checksums_status = "recorded" if published else "pending"
    (release_dir / f"v{version}-release-notes.md").write_text(
        f"# Alice v{version} Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        f'"version":"{version}",'
        f'"publication_status":"{publication_status}",'
        f'"checksums_status":"{checksums_status}"}} -->\n\nReady.\n',
        encoding="utf-8",
    )
    if published:
        (release_dir / f"v{version}-checksums.txt").write_text(
            _checksum_manifest(version),
            encoding="utf-8",
        )
    elif latest_published_version is not None:
        (release_dir / f"v{latest_published_version}-release-notes.md").write_text(
            f"# Alice v{latest_published_version} Release Notes\n"
            '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
            f'"version":"{latest_published_version}",'
            '"publication_status":"published","checksums_status":"recorded"} -->\n\nPublished.\n',
            encoding="utf-8",
        )
        (release_dir / f"v{latest_published_version}-checksums.txt").write_text(
            _checksum_manifest(latest_published_version),
            encoding="utf-8",
        )

    for rule in control_doc_truth.VERSION_ALIGNED_DOC_RULES:
        doc_path = tmp_path / rule.relative_path
        with doc_path.open("a", encoding="utf-8") as handle:
            if published:
                if rule.relative_path == "docs/integrations/reference-paths.md":
                    handle.write(f"latest published `v{version}` baseline\n")
                elif rule.relative_path == "docs/alpha/headless-ubuntu-install.md":
                    handle.write(f"latest published release tag\n(`v{version}`)\n")
                else:
                    handle.write(f"`v{version}` is the latest published release\n")
                if rule.relative_path.endswith("CURRENT_STATE.md"):
                    handle.write(f"## What `v{version}` Shipped\n")
            else:
                handle.write(f"`v{version}` is the current release-hardening candidate\n")
                if rule.relative_path.endswith("CURRENT_STATE.md"):
                    handle.write(f"## What `v{version}` Targets\n")

    documented_published_version = version if published else latest_published_version
    if documented_published_version is not None:
        if not published:
            for rule in control_doc_truth.VERSION_ALIGNED_DOC_RULES:
                doc_path = tmp_path / rule.relative_path
                with doc_path.open("a", encoding="utf-8") as handle:
                    if rule.relative_path == "docs/integrations/reference-paths.md":
                        handle.write(
                            f"\nlatest published `v{documented_published_version}` baseline\n"
                        )
                    elif rule.relative_path == "docs/alpha/headless-ubuntu-install.md":
                        handle.write(
                            f"\nlatest published release tag\n(`v{documented_published_version}`)\n"
                        )
                    else:
                        handle.write(
                            f"\n`v{documented_published_version}` is the latest published release\n"
                        )
        for relative_path in ("README.md", "docs/vnext/README.md"):
            target = tmp_path / relative_path
            target.write_text(
                target.read_text(encoding="utf-8")
                + f"\n[Release notes](docs/release/v{documented_published_version}-release-notes.md)\n",
                encoding="utf-8",
            )
        for relative_path in ("ARCHITECTURE.md", "PRODUCT_BRIEF.md", "ROADMAP.md"):
            target = tmp_path / relative_path
            target.write_text(
                target.read_text(encoding="utf-8")
                + f"\ndocs/release/v{documented_published_version}-checksums.txt\n",
                encoding="utf-8",
            )
        target = tmp_path / "CURRENT_STATE.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                "## Release Boundary\n",
                f"## Release Boundary\ndocs/release/v{documented_published_version}-checksums.txt\n",
            ),
            encoding="utf-8",
        )
        install = tmp_path / "docs" / "alpha" / "headless-ubuntu-install.md"
        install.write_text(
            install.read_text(encoding="utf-8") + f"\nUse --tag v{documented_published_version}.\n",
            encoding="utf-8",
        )

    _seed_historical_remediation_docs(tmp_path)


def test_control_doc_truth_passes_with_required_markers() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=repo_root)

    assert issues == []


def test_phase2_handoff_reports_are_unignored_without_weakening_generic_report_policy() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    handoff_prefix = "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep"

    def is_ignored(relative_path: str) -> bool:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(repo_root),
                "check-ignore",
                "--no-index",
                "--quiet",
                relative_path,
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode in {0, 1}, result.stderr
        return result.returncode == 0

    for filename in ("BUILD_REPORT.md", "REVIEW_REPORT.md"):
        assert not is_ignored(f"{handoff_prefix}/{filename}")
        assert is_ignored(filename)


def test_phase2_dependency_handoff_acknowledges_audit_tool_semver_delta() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    handoff_root = repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep"
    readme = (handoff_root / "README.md").read_text(encoding="utf-8")
    fix_matrix = (handoff_root / "FIX_MATRIX.md").read_text(encoding="utf-8")
    engineer_handoff = (handoff_root / "ENGINEER_HANDOFF.md").read_text(encoding="utf-8")

    assert "`semver@7.8.0`" in readme
    assert "`semver@7.8.0`" in fix_matrix
    assert "`semver@7.8.0`" in engineer_handoff
    assert "c4cd48f582508459ca4927539bd5ae7c6976aa99a12201f588bf6a81669d86a3" in readme
    assert "final lockfile hash\n  is recorded" not in readme
    assert "Web dependency versions remain unchanged" not in fix_matrix
    assert "unchanged web graph" not in engineer_handoff


def test_active_vnext_status_records_phase3_structural_boundary() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    vnext_readme = (repo_root / "docs/vnext/README.md").read_text(encoding="utf-8")
    normalized = " ".join(vnext_readme.replace("-\n", "-").split())

    assert "`v0.12.0` shipped the Phase 3 structural refactor" in normalized
    assert "Structure only. Zero behavior change." in normalized
    assert "latest published release and remains the install" in normalized


def test_phase2_handoff_describes_event_union_and_memory_query_legs_exactly() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    handoff_root = repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep"
    fix_matrix = (handoff_root / "FIX_MATRIX.md").read_text(encoding="utf-8")
    surface_inventory = (handoff_root / "SURFACE_INVENTORY.md").read_text(encoding="utf-8")
    engineer_handoff = (handoff_root / "ENGINEER_HANDOFF.md").read_text(encoding="utf-8")
    normalized_surface = " ".join(surface_inventory.split())

    assert "`UNION ALL`" not in fix_matrix
    assert "`UNION ALL`" not in surface_inventory
    assert "deduplicate identical full event rows before stable ordering" in fix_matrix
    assert "deduplicate identical full event rows before stable" in normalized_surface
    assert "`list_memories(query=...)`" in surface_inventory
    assert "`list_resume_memory_events(query=...)`" in surface_inventory
    assert "`list_memories(query=...)`" in engineer_handoff
    assert "`list_resume_memory_events(query=...)`" in engineer_handoff
    assert "non-FTS `search_memories` path" not in surface_inventory


def test_phase2_readme_anchors_codeql_baseline_to_the_inventory() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    readme = (
        repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/README.md"
    ).read_text(encoding="utf-8")

    assert "242-alert published-base CodeQL" in readme
    assert "described in `SURFACE_INVENTORY.md`" in readme
    assert "CodeQL count above" not in readme


def test_phase2_included_handoff_docs_keep_final_report_status_temporally_true() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    handoff_root = repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep"
    documents = [
        (handoff_root / filename).read_text(encoding="utf-8")
        for filename in ("README.md", "SURFACE_INVENTORY.md", "ENGINEER_HANDOFF.md")
    ]

    assert all("At package-input freeze" in document for document in documents)
    assert all("`BUILD_REPORT.md`" in document for document in documents)
    assert all("`REVIEW_REPORT.md`" in document for document in documents)
    assert all("were still pending" in document for document in documents)
    assert "future reviewer report" not in documents[1]
    assert "final package/receipt/review pending" not in documents[1]


def test_phase2_engineer_handoff_checks_untracked_candidate_whitespace_safely() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    engineer_handoff = (
        repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/ENGINEER_HANDOFF.md"
    ).read_text(encoding="utf-8")

    assert "git ls-files --others --exclude-standard -z" in engineer_handoff
    assert 'read -r -d \'\' candidate_path' in engineer_handoff
    assert 'git diff --no-index --check /dev/null "$candidate_path"' in engineer_handoff
    assert 'test "$untracked_whitespace_failed" -eq 0' in engineer_handoff
    assert "read -r -d '' path" not in engineer_handoff


def test_phase2_docs_scope_ascii_query_parity_and_public_error_vocabularies_exactly() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "CHANGELOG.md",
        "CURRENT_STATE.md",
        "docs/alpha/mcp-tools.md",
        "docs/release/v0.11.1-release-notes.md",
        "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/README.md",
        "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/FIX_MATRIX.md",
        "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/SURFACE_INVENTORY.md",
        "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/ENGINEER_HANDOFF.md",
    )
    documents = {
        relative_path: (repo_root / relative_path).read_text(encoding="utf-8")
        for relative_path in relative_paths
    }

    for document in documents.values():
        assert "`list_memories(query=...)`" in document
        assert "`list_resume_memory_events(query=...)`" in document
    for relative_path in relative_paths:
        assert "alice_recall" in documents[relative_path]
    release_notes = documents["docs/release/v0.11.1-release-notes.md"]
    normalized_release_notes = " ".join(release_notes.split())
    assert "stable adapter-specific public vocabularies" in normalized_release_notes
    assert "same static\n  failure vocabulary" not in release_notes
    false_parity_claims = (
        "Memory search now uses the same ASCII",
        "Memory and open-loop queries now share ASCII",
        "Aligns memory and open-loop text search",
        "literal-substring memory-search contract",
    )
    assert all(claim not in document for document in documents.values() for claim in false_parity_claims)


def test_phase3_active_docs_record_published_v0120_boundary() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "README.md",
        "ARCHITECTURE.md",
        "ROADMAP.md",
        "CURRENT_STATE.md",
        "PRODUCT_BRIEF.md",
        ".ai/active/SPRINT_PACKET.md",
        "docs/vnext/README.md",
        "docs/release/v0.12.0-release-notes.md",
    )
    for relative_path in relative_paths:
        document = (repo_root / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(document.replace("-\n", "-").split())
        assert "v0.12.0" in normalized
        assert "Structure only. Zero behavior change." in normalized

    for relative_path in (
        "CURRENT_STATE.md",
        "docs/release/v0.12.0-release-notes.md",
    ):
        document = (repo_root / relative_path).read_text(encoding="utf-8")
        assert "3,804" in document
        assert "3,793" not in document
        assert "3,547" not in document


def test_memory_operations_and_product_docs_do_not_overclaim_audit_provenance() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    protocol = (repo_root / "docs/memory-operations-protocol.md").read_text(encoding="utf-8")
    product = (repo_root / "PRODUCT_BRIEF.md").read_text(encoding="utf-8")
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "Mutating verbs append audit evidence" in protocol
    assert "Read-only verbs\ndo not append" in protocol
    assert "exact authorized true redaction is the narrow exception" in protocol
    assert "explicit commit may legitimately have no source reference" in protocol
    assert "Every verb below appends" not in protocol
    assert "source-backed memories trace to source evidence where" in product
    assert "Explicit commits may legitimately have no source reference" in product
    assert "every memory traces back to source evidence" not in product
    assert "source-backed answers trace to the evidence that was supplied" in readme
    assert "when\n  evidence was supplied, provenance links" in readme
    assert "Explicit commits may legitimately have no source reference" in readme
    assert "every answer carries explainable provenance" not in readme
    assert "every memory can explain which sources" not in readme


def test_phase2_option_a_docs_pin_exact_scrub_and_retention_contract() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    handoff_root = repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep"
    documents = {
        "handoff README": (handoff_root / "README.md").read_text(encoding="utf-8"),
        "surface inventory": (handoff_root / "SURFACE_INVENTORY.md").read_text(encoding="utf-8"),
        "fix matrix": (handoff_root / "FIX_MATRIX.md").read_text(encoding="utf-8"),
        "engineer handoff": (handoff_root / "ENGINEER_HANDOFF.md").read_text(encoding="utf-8"),
        "operations protocol": (repo_root / "docs/memory-operations-protocol.md").read_text(encoding="utf-8"),
    }

    for label, document in documents.items():
        normalized = " ".join(document.replace("-\n", "-").split())
        assert "`[REDACTED]`" in normalized, label
        assert '`{"redacted":true}`' in normalized, label
        assert "`commit_digest`" in normalized, label
        assert "`confirmation_id`" in normalized, label
        assert "`confirmation_status`" in normalized, label
        assert "`last_confirmed_at`" in normalized, label
        assert "verbosity" in normalized, label
        assert "null" in normalized.casefold(), label

    detailed = documents["surface inventory"] + documents["operations protocol"]
    for retained_field in (
        "memory_type",
        "domain",
        "sensitivity",
        "confidence",
        "salience",
        "trust",
        "promotion",
        "evidence",
        "extracted-by-model",
        "validity",
        "seen",
        "review",
        "usefulness",
        "accuracy",
        "source_grounding",
        "novel_connections",
        "actionability",
        "hallucination_risk",
    ):
        assert retained_field in detailed
    surface = documents["surface inventory"]
    assert "commit/confirmation data" not in surface
    assert "numeric accuracy/usefulness scores" not in surface


def test_phase2_error_docs_scope_dynamic_diagnostic_claims_to_migrated_carriers() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "CURRENT_STATE.md",
        "CHANGELOG.md",
        "docs/release/v0.11.1-release-notes.md",
        "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/README.md",
        "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/FIX_MATRIX.md",
    )

    for relative_path in relative_paths:
        document = (repo_root / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(document.replace("-\n", "-").split())
        for migrated_surface in (
            "provider",
            "response",
            "scheduler",
            "evaluation",
            "doctor",
            "connector",
        ):
            assert migrated_surface in normalized, relative_path
        assert "`proxy_execution.py`" in normalized, relative_path
        assert "reasons remain dynamic" in normalized, relative_path
        for stale_claim in (
            "persisted diagnostic paths",
            "persisted failure records",
            "stored diagnostic failure paths",
        ):
            assert stale_claim not in normalized, relative_path


def test_cli_docs_separate_stderr_boundary_errors_from_structured_failure_reports() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cli_docs = (repo_root / "docs/integrations/cli.md").read_text(encoding="utf-8")

    assert "argument, boundary-validation,\n  and unhandled execution failures to stderr" in cli_docs
    for failure_name in ("EvalGateFailure", "EmbeddingBackfillFailure", "PartialCommandFailure"):
        assert failure_name in cli_docs
    assert "structured failure report on stdout" in cli_docs
    assert "return nonzero" in cli_docs
    assert "write failures to stderr" not in cli_docs


def test_phase2_post_freeze_docs_point_to_final_reports_and_external_release_work() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    release_notes = (repo_root / "docs/release/v0.11.1-release-notes.md").read_text(encoding="utf-8")
    roadmap = (repo_root / "ROADMAP.md").read_text(encoding="utf-8")
    fix_matrix = (
        repo_root / "docs/handoff/2026-07-16-v0.11.1-phase2-debt-sweep/FIX_MATRIX.md"
    ).read_text(encoding="utf-8")

    assert "At package-input freeze, the candidate was uncommitted" in release_notes
    assert "`BUILD_REPORT.md`" in release_notes
    assert "`REVIEW_REPORT.md`" in release_notes
    assert "still follow this\n  documentation freeze" not in release_notes
    # Durable form: the roadmap follows the structured publication record,
    # independently of any pending governed-version bump.
    latest_published = control_doc_truth._latest_structured_published_version(root_dir=repo_root)
    assert latest_published is not None
    assert f"`v{latest_published}` is the latest published release." in roadmap
    assert "**Verify and release the completed v0.12.0 structural handoff.**" not in roadmap
    assert "**Verify and publish the completed v0.11.1 handoff.**" not in roadmap
    assert "final `BUILD_REPORT.md`" in fix_matrix
    assert "reviewer-authored `REVIEW_REPORT.md`" in fix_matrix
    assert "After reviewer approval, the only remaining gates" in fix_matrix


def test_pending_v0140_roadmap_uses_latest_structured_published_record(tmp_path: Path) -> None:
    _seed_truth_docs(
        tmp_path,
        version="0.14.0",
        latest_published_version="0.13.1",
    )

    with (tmp_path / "pyproject.toml").open("rb") as handle:
        package_version = tomllib.load(handle)["project"]["version"]
    web_version = json.loads((tmp_path / "apps/web/package.json").read_text(encoding="utf-8"))["version"]
    roadmap = (tmp_path / "ROADMAP.md").read_text(encoding="utf-8")

    assert package_version == web_version == "0.14.0"
    assert control_doc_truth._latest_structured_published_version(root_dir=tmp_path) == "0.13.1"
    assert "`v0.13.1` is the latest published release" in roadmap
    assert "`v0.14.0` is the latest published release" not in roadmap
    assert control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path) == []


def test_published_v0140_roadmap_uses_new_structured_published_record(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, published=True, version="0.14.0")

    roadmap = (tmp_path / "ROADMAP.md").read_text(encoding="utf-8")

    assert control_doc_truth._latest_structured_published_version(root_dir=tmp_path) == "0.14.0"
    assert "`v0.14.0` is the latest published release" in roadmap
    assert control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path) == []


def test_published_v0140_rejects_wrong_roadmap_latest_version(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, published=True, version="0.14.0")
    roadmap = tmp_path / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace(
            "`v0.14.0` is the latest published release",
            "`v0.13.1` is the latest published release",
        ),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert "ROADMAP.md: names v0.13.1 as latest published instead of v0.14.0" in issues


def test_release_notes_pin_manual_only_publish_trigger_and_scheduler_once() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    release_notes = (repo_root / "docs/release/v0.11.1-release-notes.md").read_text(encoding="utf-8")

    assert "has no `release:` trigger" in release_notes
    assert "manual `workflow_dispatch` only" in release_notes
    assert "`--once` to its child before spawn" in release_notes
    assert "tests cover the release trigger" not in release_notes


def test_release_gate_requires_fresh_isolated_artifact_directories() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    releasing = (repo_root / "RELEASING.md").read_text(encoding="utf-8")
    engineer_handoff = (repo_root / "docs/handoff/2026-07-14-v0.10.4-remediation/ENGINEER_HANDOFF.md").read_text(
        encoding="utf-8"
    )
    build_report = (repo_root / "docs/handoff/2026-07-14-v0.10.4-remediation/BUILD_REPORT.md").read_text(
        encoding="utf-8"
    )

    assert "make release-check DIST_DIR=dist" not in releasing
    assert "`dist/SHA256SUMS`" not in releasing
    # The handoff is a frozen record and keeps the folder the 2026-07 gate used. RELEASING.md names no fixed
    # temporary folder (the hygiene test would refuse one) and makes the root with ``mktemp -d``.
    assert 'release_run_root="$(mktemp -d "${TMPDIR:-/tmp}/alice-release-check.XXXXXX")"' in releasing
    assert 'release_run_root="$(mktemp -d /tmp/alice-release-check.XXXXXX)"' in engineer_handoff
    for document in (releasing, engineer_handoff):
        for marker in (
            'DIST_DIR="$dist_dir"',
            'REPRO_DIST_DIR="$repro_dist_dir"',
            'test -z "$(find "$directory" -mindepth 1 -maxdepth 1 -print -quit)"',
        ):
            assert marker in document
    for marker in (
        "$DIST_DIR/SHA256SUMS",
        "never point either variable at user-owned artifact",
        "Run the gate without `-j`",
        "do not run `release-artifacts` separately first",
    ):
        assert marker in releasing
    for marker in (
        "Run without `-j`",
        "do not invoke that target separately",
        "never delete, overwrite, or rely on ignored historical",
    ):
        assert marker in engineer_handoff
    for marker in (
        "may have been refreshed by verification commands",
        "excluded from the tracked/untracked manifest and remediation",
        "not used as evidence",
    ):
        assert marker in build_report


def test_control_doc_truth_accepts_published_state_and_markers(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, published=True)

    assert control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path) == []


def test_control_doc_truth_fails_when_required_marker_is_missing(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    first_rule = control_doc_truth.CONTROL_DOC_TRUTH_RULES[0]
    first_doc_path = tmp_path / first_rule.relative_path
    first_doc_path.write_text("missing required baseline marker\n", encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(
        issue == f"{first_rule.relative_path}: missing required marker '{first_rule.required_markers[0]}'"
        for issue in issues
    )


def test_control_doc_truth_fails_when_disallowed_marker_is_present(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    target_rule = next(rule for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES if rule.relative_path == "ROADMAP.md")
    target_path = tmp_path / target_rule.relative_path
    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "\nGate ownership is canonicalized to Phase 4 runner scripts.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(
        issue
        == f"{target_rule.relative_path}: contains disallowed marker 'Gate ownership is canonicalized to Phase 4 runner scripts'"
        for issue in issues
    )


def test_control_doc_truth_fails_when_archive_index_is_missing(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    archive_rule = next(
        rule
        for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES
        if rule.relative_path == "docs/archive/planning/2026-04-08-context-compaction/README.md"
    )
    (tmp_path / archive_rule.relative_path).unlink()

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(issue == f"{archive_rule.relative_path}: missing file" for issue in issues)


def test_control_doc_truth_requires_repair_batch_history_path(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    history_rule = next(
        rule
        for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES
        if rule.relative_path == "docs/handoff/history/v0.10.4-repair-batches.md"
    )
    (tmp_path / history_rule.relative_path).unlink()

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert f"{history_rule.relative_path}: missing file" in issues


def test_control_doc_truth_requires_repair_batch_history_marker(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    history_rule = next(
        rule
        for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES
        if rule.relative_path == "docs/handoff/history/v0.10.4-repair-batches.md"
    )
    history_path = tmp_path / history_rule.relative_path
    missing_marker = history_rule.required_markers[2]
    history_path.write_text(
        history_path.read_text(encoding="utf-8").replace(missing_marker, "missing batch-16 history"),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert f"{history_rule.relative_path}: missing required marker '{missing_marker}'" in issues


@pytest.mark.parametrize(
    ("stale_claim", "issue_label"),
    (
        ("Repair Batch 16 is the current bounded correction.", "stale live-repair ledger claim"),
        ("The live repair ledger governs this sprint.", "stale live-repair ledger claim"),
        ("Mandatory repair pass is active.", "stale live-repair ledger claim"),
        ("Phase 4 is\nactive and authorized.", "Phase 4 active-work claim"),
        ("Alice executes OCR and transcription.", "Alice OCR/transcription execution claim"),
        ("Transcription is executed by Alice.", "Alice OCR/transcription execution claim"),
    ),
)
def test_active_sprint_packet_rejects_stale_or_future_scope_claims(
    tmp_path: Path,
    stale_claim: str,
    issue_label: str,
) -> None:
    _seed_truth_docs(tmp_path)
    packet = tmp_path / control_doc_truth._ACTIVE_SPRINT_PACKET
    packet.write_text(
        packet.read_text(encoding="utf-8") + f"\n{stale_claim}\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert f"{control_doc_truth._ACTIVE_SPRINT_PACKET}: contains {issue_label}" in issues


def test_active_sprint_packet_rejects_excess_lines(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    packet = tmp_path / control_doc_truth._ACTIVE_SPRINT_PACKET
    padding = "\n".join("padding" for _ in range(control_doc_truth._ACTIVE_SPRINT_PACKET_MAX_LINES + 1))
    packet.write_text(
        packet.read_text(encoding="utf-8") + f"\n{padding}\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert (
        f"{control_doc_truth._ACTIVE_SPRINT_PACKET}: exceeds "
        f"{control_doc_truth._ACTIVE_SPRINT_PACKET_MAX_LINES}-line control-document limit"
    ) in issues


def test_active_sprint_packet_rejects_excess_bytes(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    packet = tmp_path / control_doc_truth._ACTIVE_SPRINT_PACKET
    packet.write_text(
        packet.read_text(encoding="utf-8") + "x" * control_doc_truth._ACTIVE_SPRINT_PACKET_MAX_BYTES,
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert (
        f"{control_doc_truth._ACTIVE_SPRINT_PACKET}: exceeds "
        f"{control_doc_truth._ACTIVE_SPRINT_PACKET_MAX_BYTES}-byte control-document limit"
    ) in issues


def test_control_doc_truth_fails_when_stale_legacy_marker_is_present(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    target_rule = next(rule for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES if rule.relative_path == "README.md")
    target_path = tmp_path / target_rule.relative_path
    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "\nLegacy Compatibility Markers still apply here.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(
        issue == f"{target_rule.relative_path}: contains disallowed marker 'Legacy Compatibility Markers'"
        for issue in issues
    )


def test_control_doc_truth_fails_when_release_version_is_stale(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    target = tmp_path / "CURRENT_STATE.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "`v9.8.7` is the current release-hardening candidate",
            "`v9.8.6` is the current release-hardening candidate",
        ),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(issue.startswith("CURRENT_STATE.md: missing current-version candidate marker") for issue in issues)


def test_candidate_mode_rejects_stale_latest_published_claim(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    release_dir = tmp_path / "docs" / "release"
    (release_dir / "v9.8.6-release-notes.md").write_text(
        "# Alice v9.8.6 Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"9.8.6","publication_status":"published",'
        '"checksums_status":"recorded"} -->\n',
        encoding="utf-8",
    )
    (release_dir / "v9.8.6-checksums.txt").write_text(
        _checksum_manifest("9.8.6", "1"),
        encoding="utf-8",
    )
    target = tmp_path / "PRODUCT_BRIEF.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n`v9.8.5` is the latest published release.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(issue == "PRODUCT_BRIEF.md: names v9.8.5 as latest published instead of v9.8.6" for issue in issues)


@pytest.mark.parametrize(
    ("schema_version", "with_checksums"),
    (
        ("alice_release_document_state_v0", True),
        ("alice_release_document_state_v1", False),
    ),
)
def test_candidate_mode_rejects_latest_published_history_without_valid_evidence(
    tmp_path: Path, schema_version: str, with_checksums: bool
) -> None:
    _seed_truth_docs(tmp_path)
    release_dir = tmp_path / "docs" / "release"
    (release_dir / "v9.8.6-release-notes.md").write_text(
        "# Alice v9.8.6 Release Notes\n"
        f'<!-- alice-release-state: {{"schema_version":"{schema_version}",'
        '"version":"9.8.6","publication_status":"published",'
        '"checksums_status":"recorded"} -->\n',
        encoding="utf-8",
    )
    if with_checksums:
        (release_dir / "v9.8.6-checksums.txt").write_text(
            "2" * 64 + "  alice_memory-9.8.6.tar.gz\n",
            encoding="utf-8",
        )
    target = tmp_path / "PRODUCT_BRIEF.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n`v9.8.6` is the latest published release.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert (
        "PRODUCT_BRIEF.md: names v9.8.6 as latest published without a structured published release record"
    ) in issues


@pytest.mark.parametrize(
    "stale_claim",
    (
        "The latest published\nrelease is `v9.8.5`.",
        "`v9.8.5` remains the latest\npublished release.",
    ),
)
def test_candidate_mode_rejects_multiline_stale_latest_published_claim(tmp_path: Path, stale_claim: str) -> None:
    _seed_truth_docs(tmp_path)
    release_dir = tmp_path / "docs" / "release"
    (release_dir / "v9.8.6-release-notes.md").write_text(
        "# Alice v9.8.6 Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"9.8.6","publication_status":"published",'
        '"checksums_status":"recorded"} -->\n',
        encoding="utf-8",
    )
    (release_dir / "v9.8.6-checksums.txt").write_text(
        _checksum_manifest("9.8.6", "3"),
        encoding="utf-8",
    )
    target = tmp_path / "PRODUCT_BRIEF.md"
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{stale_claim}\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert ("PRODUCT_BRIEF.md: names v9.8.5 as latest published instead of v9.8.6") in issues


def test_control_doc_truth_rejects_empty_checksum_receipt_after_publication(
    tmp_path: Path,
) -> None:
    _seed_truth_docs(tmp_path, published=True)
    (tmp_path / "docs" / "release" / "v9.8.7-checksums.txt").write_text("# no artifact records\n", encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert (
        "docs/release/v9.8.7-checksums.txt: must contain exactly the canonical wheel and sdist SHA-256 records"
    ) in issues


@pytest.mark.parametrize(
    "manifest",
    (
        "4" * 64 + "  unrelated-package.zip\n",
        "5" * 64 + "  alice_memory-9.8.7.tar.gz\n",
        _checksum_manifest("9.8.7") + "6" * 64 + "  unrelated-package.zip\n",
        _checksum_manifest("9.8.7") + "7" * 64 + "  alice_memory-9.8.7.tar.gz\n",
    ),
)
def test_control_doc_truth_rejects_noncanonical_checksum_receipts(tmp_path: Path, manifest: str) -> None:
    _seed_truth_docs(tmp_path, published=True)
    (tmp_path / "docs" / "release" / "v9.8.7-checksums.txt").write_text(manifest, encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("must contain exactly the canonical wheel and sdist" in issue for issue in issues)


def test_candidate_mode_ignores_historical_state_with_extra_keys(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    release_dir = tmp_path / "docs" / "release"
    (release_dir / "v9.8.6-release-notes.md").write_text(
        "# Alice v9.8.6 Release Notes\n"
        '<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1",'
        '"version":"9.8.6","publication_status":"published",'
        '"checksums_status":"recorded","unexpected":true} -->\n',
        encoding="utf-8",
    )
    (release_dir / "v9.8.6-checksums.txt").write_text(_checksum_manifest("9.8.6", "8"), encoding="utf-8")
    target = tmp_path / "PRODUCT_BRIEF.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n`v9.8.6` is the latest published release.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert (
        "PRODUCT_BRIEF.md: names v9.8.6 as latest published without a structured published release record"
    ) in issues


@pytest.mark.parametrize("published", (False, True))
def test_control_doc_truth_rejects_extra_release_state_keys(tmp_path: Path, published: bool) -> None:
    _seed_truth_docs(tmp_path, published=published)
    notes = tmp_path / "docs" / "release" / "v9.8.7-release-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace('"version":"9.8.7",', '"version":"9.8.7","unexpected":true,'),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("must contain exactly the supported keys" in issue for issue in issues)


@pytest.mark.parametrize(
    ("relative_path", "stale_text"),
    (
        ("CHANGELOG.md", "the v0.10.0 matrix is the current closure record"),
        (
            "docs/release/v0.9.4-release-notes.md",
            "The v0.10.0 remediation cycle tracks unfinished work.",
        ),
    ),
)
def test_control_doc_truth_rejects_stale_historical_closure_wording(
    tmp_path: Path, relative_path: str, stale_text: str
) -> None:
    _seed_truth_docs(tmp_path)
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(stale_text + "\n", encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(issue == f"{relative_path}: contains stale present-tense v0.10.0 closure wording" for issue in issues)


@pytest.mark.parametrize(
    ("relative_path", "contradiction"),
    (
        (
            "CURRENT_STATE.md",
            "The cycle resumes feature work and clears the second audit's P2 backlog.",
        ),
        (
            "ARCHITECTURE.md",
            "The candidate resumes feature work and clears the second audit's P2 backlog.",
        ),
        (
            "RELEASING.md",
            "The semantic attestation replaces the repository-control attestation.",
        ),
    ),
)
def test_control_doc_truth_rejects_known_release_contradictions(
    tmp_path: Path,
    relative_path: str,
    contradiction: str,
) -> None:
    _seed_truth_docs(tmp_path)
    target = tmp_path / relative_path
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{contradiction}\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(issue.startswith(f"{relative_path}: contains disallowed marker") for issue in issues)


def test_control_doc_truth_requires_repository_control_attestation_contract(
    tmp_path: Path,
) -> None:
    _seed_truth_docs(tmp_path)
    target_rule = next(
        rule for rule in control_doc_truth.CONTROL_DOC_TRUTH_RULES if rule.relative_path == "RELEASING.md"
    )
    target = tmp_path / target_rule.relative_path
    target.write_text(
        target.read_text(encoding="utf-8").replace("ALICE_RELEASE_CONTROLS_ATTESTATION", "removed-control-variable"),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any(
        issue == ("RELEASING.md: missing required marker 'ALICE_RELEASE_CONTROLS_ATTESTATION'") for issue in issues
    )


def test_control_doc_truth_rejects_candidate_claim_after_publication(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, published=True)
    target = tmp_path / "PRODUCT_BRIEF.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n`v9.8.7` is still an unpublished candidate.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("describes published v9.8.7" in issue for issue in issues)


@pytest.mark.parametrize(
    "contradiction",
    (
        "`v9.8.7` remains\nan unpublished candidate.",
        "This unpublished candidate\nis `v9.8.7`.",
        "`v9.8.7` is not\tpublished.",
    ),
)
def test_control_doc_truth_rejects_multiline_candidate_claim_after_publication(
    tmp_path: Path, contradiction: str
) -> None:
    _seed_truth_docs(tmp_path, published=True)
    target = tmp_path / "PRODUCT_BRIEF.md"
    target.write_text(
        target.read_text(encoding="utf-8") + f"\n{contradiction}\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("describes published v9.8.7" in issue for issue in issues)


def test_control_doc_truth_requires_checksum_receipt_after_publication(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, published=True)
    (tmp_path / "docs" / "release" / "v9.8.7-checksums.txt").unlink()

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("missing for recorded publication" in issue for issue in issues)


def test_the_current_state_copy_stays_gone() -> None:
    """No second copy of ``CURRENT_STATE.md`` lives under ``.ai/handoff``.

    Nothing read that copy, and keeping it equal to the real file took a script comparison and two rule
    entries. It was deleted. A copy that came back would drift unseen, because the check no longer looks at it.

    Mutation: copy ``CURRENT_STATE.md`` to ``.ai/handoff/CURRENT_STATE.md``.
    """

    repo_root = Path(__file__).resolve().parents[2]

    assert (repo_root / "CURRENT_STATE.md").is_file()
    assert not (repo_root / ".ai" / "handoff" / "CURRENT_STATE.md").exists()


def test_git_ignores_a_current_state_copy_under_ai_handoff() -> None:
    """``.gitignore`` has no exception that tracks ``.ai/handoff/CURRENT_STATE.md``.

    The exception existed only to keep the copy in the repository. With it gone, the rest of ``.ai/handoff``
    stays local, so a copy recreated by hand is not committed by an ordinary ``git add``.

    Mutation: put ``!.ai/handoff/CURRENT_STATE.md`` back into ``.gitignore``.
    """

    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ("git", "-C", str(repo_root), "check-ignore", "--no-index", "--quiet", ".ai/handoff/CURRENT_STATE.md"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_control_doc_truth_does_not_name_a_current_state_copy() -> None:
    """The check script keeps no rule and no comparison for a ``CURRENT_STATE.md`` under ``.ai/handoff``.

    Mutations, each one alone: put the ``ControlDocTruthRule`` entry for ``.ai/handoff/CURRENT_STATE.md``
    back into the script; put the ``VersionAlignedDocRule`` entry back; put the byte comparison back.
    """

    source = Path(control_doc_truth.__file__).read_text(encoding="utf-8")
    assert re.search(r"\.ai\W+handoff", source) is None

    for rules in (control_doc_truth.CONTROL_DOC_TRUTH_RULES, control_doc_truth.VERSION_ALIGNED_DOC_RULES):
        state_paths = [rule.relative_path for rule in rules if rule.relative_path.endswith("CURRENT_STATE.md")]
        assert state_paths == ["CURRENT_STATE.md"]


def test_control_doc_truth_does_not_read_a_current_state_copy(tmp_path: Path) -> None:
    """A file at ``.ai/handoff/CURRENT_STATE.md`` changes nothing in the check, whatever it holds.

    The repository test above is what keeps the file out of the tree. This one shows the script does not
    read it: no required marker, no version line and no equality with ``CURRENT_STATE.md`` is asked of it.

    Mutation: put the byte comparison, or a rule for the path, back into the script.
    """

    _seed_truth_docs(tmp_path)
    copy = tmp_path / ".ai" / "handoff" / "CURRENT_STATE.md"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text("a copy that holds none of the markers and is not equal\n", encoding="utf-8")

    assert control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path) == []


@pytest.mark.parametrize("marker", ("## Snapshot", "## Release Boundary", "## Product Boundaries"))
def test_control_doc_truth_still_requires_each_current_state_marker(tmp_path: Path, marker: str) -> None:
    """``CURRENT_STATE.md`` keeps all of its required headings after the copy's rules were removed.

    Mutation: delete one marker from the ``CURRENT_STATE.md`` rule in the script.
    """

    _seed_truth_docs(tmp_path)
    target = tmp_path / "CURRENT_STATE.md"
    target.write_text(target.read_text(encoding="utf-8").replace(marker, "## Renamed"), encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert f"CURRENT_STATE.md: missing required marker '{marker}'" in issues


def test_control_doc_truth_still_rejects_a_disallowed_marker_in_current_state(tmp_path: Path) -> None:
    """A phrase on the disallowed list still fails when it is in ``CURRENT_STATE.md``.

    Mutation: skip ``CURRENT_STATE.md`` in the disallowed-marker loop.
    """

    _seed_truth_docs(tmp_path)
    target = tmp_path / "CURRENT_STATE.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nThe control tower decides.\n", encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert "CURRENT_STATE.md: contains disallowed marker 'control tower'" in issues


def test_control_doc_truth_still_rejects_a_stale_latest_claim_in_current_state(tmp_path: Path) -> None:
    """``CURRENT_STATE.md`` naming an older release as the latest published one still fails.

    Mutation: remove the ``CURRENT_STATE.md`` entry from ``VERSION_ALIGNED_DOC_RULES``.
    """

    _seed_truth_docs(tmp_path, latest_published_version="9.8.6")
    target = tmp_path / "CURRENT_STATE.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n`v9.8.5` is the latest published release.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert "CURRENT_STATE.md: names v9.8.5 as latest published instead of v9.8.6" in issues


def test_control_doc_truth_still_rejects_a_candidate_claim_in_published_current_state(tmp_path: Path) -> None:
    """``CURRENT_STATE.md`` calling a published version a candidate still fails.

    Mutation: remove the ``CURRENT_STATE.md`` entry from ``VERSION_ALIGNED_DOC_RULES``.
    """

    _seed_truth_docs(tmp_path, published=True)
    target = tmp_path / "CURRENT_STATE.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n`v9.8.7` is still an unpublished candidate.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert "CURRENT_STATE.md: describes published v9.8.7 as unpublished or a candidate" in issues


@pytest.mark.parametrize(
    "description",
    (
        "Alice v9.8.7 package description.\n",
        "Alice is the latest release.\n",
        "Alice is a release-gating candidate.\n",
    ),
)
def test_control_doc_truth_rejects_mutable_package_description_language(
    tmp_path: Path,
    description: str,
) -> None:
    _seed_truth_docs(tmp_path)
    (tmp_path / control_doc_truth.PACKAGE_DESCRIPTION_RELATIVE_PATH).write_text(
        description,
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("pypi-description.md" in issue for issue in issues)


def test_control_doc_truth_requires_evergreen_project_readme(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('readme = "docs/pypi-description.md"', 'readme = "README.md"'),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("project.readme must point" in issue for issue in issues)


def test_control_doc_truth_aligns_latest_notes_and_checksum_pointers(
    tmp_path: Path,
) -> None:
    _seed_truth_docs(tmp_path, published=True)
    readme = tmp_path / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("v9.8.7-release-notes.md", "v9.8.6-release-notes.md"),
        encoding="utf-8",
    )
    architecture = tmp_path / "ARCHITECTURE.md"
    architecture.write_text(
        architecture.read_text(encoding="utf-8").replace("v9.8.7-checksums.txt", "v9.8.6-checksums.txt"),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("latest release-notes link" in issue for issue in issues)
    assert any("ARCHITECTURE.md: published checksum pointer" in issue for issue in issues)


def test_control_doc_truth_aligns_release_boundary_and_install_tag(
    tmp_path: Path,
) -> None:
    _seed_truth_docs(tmp_path, published=True)
    target = tmp_path / "CURRENT_STATE.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("v9.8.7-checksums.txt", "v9.8.6-checksums.txt"),
        encoding="utf-8",
    )
    install = tmp_path / "docs" / "alpha" / "headless-ubuntu-install.md"
    install.write_text(
        install.read_text(encoding="utf-8").replace("--tag v9.8.7", "--tag v9.8.6"),
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("Release Boundary checksum pointer" in issue for issue in issues)
    assert any("literal install tag v9.8.6" in issue for issue in issues)


def _install_line_issues(tmp_path: Path) -> list[str]:
    return [
        issue
        for issue in control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)
        if "literal install tag" in issue
    ]


def test_every_living_doc_install_line_must_name_the_latest_published_release(tmp_path: Path) -> None:
    """A `git checkout`, `git clone --branch` or `--tag` line in any living doc is held to the published release.

    The check used to read one page. A page the check did not name could keep an old tag for a release or more.
    """

    _seed_truth_docs(tmp_path, published=True)
    assert _install_line_issues(tmp_path) == []

    lines = {
        "README.md": "To run it, run `git checkout v9.8.6` before `make setup`.\n",
        "docs/alpha/quickstart.md": "Run `git checkout --detach v9.8.6` first.\n",
        "docs/integrations/example.md": "git clone --branch v9.8.6 https://example.invalid/repo.git\n",
        "docs/deployment/example.md": "Install with `--tag v9.8.6`.\n",
        "plugins/example/README.md": "git checkout 'v9.8.6'\n",
        "docs/integrations/switch.md": "git switch --detach v9.8.6\n",
        "docs/integrations/tags-prefix.md": "git checkout tags/v9.8.6\n",
        "docs/integrations/short-flag.md": "git clone --depth 1 -b v9.8.6 https://example.invalid/repo.git\n",
        "docs/integrations/pip-url.md": "pip install git+https://example.invalid/repo.git@v9.8.6\n",
        ".github/example.md": "git checkout v9.8.6\n",
    }
    for relative_path, line in lines.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    issues = _install_line_issues(tmp_path)

    assert sorted(issues) == sorted(
        f"{relative_path}: literal install tag v9.8.6 must match latest published v9.8.7" for relative_path in lines
    )


def test_an_install_line_naming_the_published_release_or_no_tag_passes(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, published=True)
    page = tmp_path / "docs" / "alpha" / "quickstart.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "git checkout v9.8.7\ngit checkout main\ngit clone --branch main https://example.invalid/repo.git\n"
        "git switch --detach v9.8.7\ngit checkout tags/v9.8.7\ngit clone -b main https://example.invalid/repo.git\n"
        "pip install git+https://example.invalid/repo.git@v9.8.7\ngit checkout -b v9.8.6 origin/main\n"
        "uses: actions/checkout@v4\n"
        "The v9.8.6 release changed this. Run git checkout of the tag you want.\n",
        encoding="utf-8",
    )

    assert _install_line_issues(tmp_path) == []


def test_install_lines_in_dated_records_are_not_checked(tmp_path: Path) -> None:
    """A release note, a handoff, an archived page or a CHANGELOG section quotes its own release's install line."""

    _seed_truth_docs(tmp_path, published=True)
    for record in control_doc_truth.DATED_RECORD_PATHS:
        path = tmp_path / record
        if path.suffix == ".md":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("git checkout v9.8.6\n", encoding="utf-8")
        else:
            (path / "nested").mkdir(parents=True, exist_ok=True)
            (path / "nested" / "page.md").write_text("git clone --branch v9.8.6 x\n", encoding="utf-8")
    assert _install_line_issues(tmp_path) == []

    # A page that only looks like a record is still a living doc.
    lookalike = tmp_path / "docs" / "alpha" / "release-notes.md"
    lookalike.parent.mkdir(parents=True, exist_ok=True)
    lookalike.write_text("git checkout v9.8.6\n", encoding="utf-8")
    assert _install_line_issues(tmp_path) == [
        "docs/alpha/release-notes.md: literal install tag v9.8.6 must match latest published v9.8.7"
    ]


def test_the_dated_record_list_names_only_paths_that_exist() -> None:
    """A path that no longer exists would exempt whatever is later written at that name."""

    repo_root = Path(__file__).resolve().parents[2]

    assert control_doc_truth.DATED_RECORD_PATHS
    for record in control_doc_truth.DATED_RECORD_PATHS:
        assert (repo_root / record).exists(), record


def test_the_scan_reads_the_install_lines_this_repository_has() -> None:
    """The scan is not vacuous: it reaches the three pages that carry a literal tag, and every page it reaches names
    the release. A correct line on a fourth living page is welcome and does not fail this test."""

    repo_root = Path(__file__).resolve().parents[2]
    latest = control_doc_truth._latest_structured_published_version(root_dir=repo_root)
    assert latest is not None

    found: dict[str, set[str]] = {}
    for path in control_doc_truth.living_doc_files(repo_root):
        for match in control_doc_truth._LITERAL_INSTALL_TAG_PATTERN.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(path.relative_to(repo_root).as_posix(), set()).add(match.group("version"))

    for relative_path in ("README.md", "docs/alpha/headless-ubuntu-install.md", "docs/alpha/quickstart.md"):
        assert relative_path in found, f"the scan no longer reads the install line of {relative_path}"
    assert {relative_path: versions for relative_path, versions in found.items() if versions != {latest}} == {}


def test_living_doc_files_skip_dated_records_and_folders_that_hold_no_docs(tmp_path: Path) -> None:
    for relative_path in (
        "README.md",
        "docs/alpha/quickstart.md",
        "docs/integrations/hermes-skill-pack/skills/a/SKILL.md",
        "docs/examples/demo.py",
        "plugins/p/README.md",
        "agent-skills/s/SKILL.md",
        ".ai/active/SPRINT_PACKET.md",
        ".github/pull_request_template.md",
        "CHANGELOG.md",
        "docs/release/v1-release-notes.md",
        "docs/handoff/x/README.md",
        "docs/archive/y.md",
        "docs/plans/z.md",
        "docs/roadmap-friction-first.md",
        "tests/unit/fixture.md",
        "apps/web/node_modules/pkg/README.md",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    markdown = [path.relative_to(tmp_path).as_posix() for path in control_doc_truth.living_doc_files(tmp_path)]
    with_python = [
        path.relative_to(tmp_path).as_posix()
        for path in control_doc_truth.living_doc_files(tmp_path, suffixes=(".md", ".py"))
    ]

    assert sorted(markdown) == [
        ".ai/active/SPRINT_PACKET.md",
        ".github/pull_request_template.md",
        "README.md",
        "agent-skills/s/SKILL.md",
        "docs/alpha/quickstart.md",
        "docs/integrations/hermes-skill-pack/skills/a/SKILL.md",
        "plugins/p/README.md",
    ]
    assert "docs/examples/demo.py" in with_python
    assert "docs/examples/demo.py" not in markdown


def test_control_doc_truth_rejects_future_state_in_new_published_notes(
    tmp_path: Path,
) -> None:
    _seed_truth_docs(tmp_path, published=True)
    notes = tmp_path / "docs" / "release" / "v9.8.7-release-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8") + "\nArtifact digests will be recorded after publication.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert any("published notes contain future-state language" in issue for issue in issues)


def _seed_historical_remediation_docs(tmp_path: Path, *, finalization: bool = True) -> Path:
    handoff_dir = tmp_path / control_doc_truth._HISTORICAL_REMEDIATION_HANDOFF
    handoff_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, markers in control_doc_truth._HISTORICAL_REMEDIATION_MARKERS.items():
        finalization_markers = (
            control_doc_truth._HISTORICAL_FINALIZATION_MARKERS.get(relative_path, ()) if finalization else ()
        )
        (handoff_dir / relative_path).write_text(
            "\n".join((*markers, *finalization_markers)),
            encoding="utf-8",
        )
    return handoff_dir


def test_historical_remediation_handoff_cannot_silently_disappear(tmp_path: Path) -> None:
    assert control_doc_truth._validate_historical_remediation_handoff(tmp_path) == [
        f"{control_doc_truth._HISTORICAL_REMEDIATION_HANDOFF}: missing directory"
    ]


def test_control_doc_truth_requires_repair_batch_16_historical_handoff_boundary(
    tmp_path: Path,
) -> None:
    handoff_dir = _seed_historical_remediation_docs(tmp_path)

    engineer_handoff = handoff_dir / "ENGINEER_HANDOFF.md"
    engineer_handoff.write_text(
        engineer_handoff.read_text(encoding="utf-8").replace("- [x] continuity APIs", "- [ ] continuity APIs"),
        encoding="utf-8",
    )

    issues = control_doc_truth._validate_historical_remediation_handoff(tmp_path)

    assert any("ENGINEER_HANDOFF.md" in issue and "continuity APIs" in issue for issue in issues)


def test_control_doc_truth_rejects_stale_batch_14_pending_freeze_claim(
    tmp_path: Path,
) -> None:
    handoff_dir = _seed_historical_remediation_docs(tmp_path)

    readme = handoff_dir / "README.md"
    stale_marker = control_doc_truth._HISTORICAL_REMEDIATION_FORBIDDEN_MARKERS["README.md"][0]
    readme.write_text(
        readme.read_text(encoding="utf-8") + f"\n{stale_marker}\n",
        encoding="utf-8",
    )

    issues = control_doc_truth._validate_historical_remediation_handoff(tmp_path)

    assert any(
        issue
        == (
            f"{control_doc_truth._HISTORICAL_REMEDIATION_HANDOFF}/README.md: "
            f"contains stale remediation marker {stale_marker!r}"
        )
        for issue in issues
    )


def test_control_doc_truth_rejects_stale_batch_15_current_pending_claim(
    tmp_path: Path,
) -> None:
    handoff_dir = _seed_historical_remediation_docs(tmp_path)

    fix_matrix = handoff_dir / "FIX_MATRIX.md"
    stale_marker = control_doc_truth._HISTORICAL_REMEDIATION_FORBIDDEN_MARKERS["FIX_MATRIX.md"][-1]
    fix_matrix.write_text(
        fix_matrix.read_text(encoding="utf-8")
        + "\nRepair Batch 15 is the current\n"
        + "bounded correction. The tree is builder-frozen; independent review remains **PENDING**.\n",
        encoding="utf-8",
    )

    issues = control_doc_truth._validate_historical_remediation_handoff(tmp_path)

    assert any(
        issue
        == (
            f"{control_doc_truth._HISTORICAL_REMEDIATION_HANDOFF}/FIX_MATRIX.md: "
            f"contains stale remediation marker {stale_marker!r}"
        )
        for issue in issues
    )


def test_historical_handoff_requires_finalization_markers_unconditionally(tmp_path: Path) -> None:
    _seed_historical_remediation_docs(tmp_path, finalization=False)

    issues = control_doc_truth._validate_historical_remediation_handoff(tmp_path)

    assert any("missing finalization truth marker" in issue for issue in issues)


@pytest.mark.parametrize(
    ("relative_path", "marker"),
    [
        ("README.md", "The tree is intentionally uncommitted. No version source was bumped"),
        ("BUILD_REPORT.md", "Package version intentionally remains `0.10.3` until release-engineer finalization"),
        (
            "ENGINEER_HANDOFF.md",
            "Before the finalization commit, bump every governed version source to `0.10.4`",
        ),
        ("FIX_MATRIX.md", "v0.10.4 as uncommitted remediation"),
        (
            "SURFACE_INVENTORY.md",
            "Version sources remain `0.10.3` during this uncommitted remediation",
        ),
    ],
)
def test_control_doc_truth_rejects_stale_historical_finalization_claims(
    tmp_path: Path,
    relative_path: str,
    marker: str,
) -> None:
    handoff_dir = _seed_historical_remediation_docs(tmp_path)
    target = handoff_dir / relative_path
    line_wrapped_marker = marker.replace(" ", "\n", 1)
    target.write_text(target.read_text(encoding="utf-8") + f"\n{line_wrapped_marker}\n", encoding="utf-8")

    issues = control_doc_truth._validate_historical_remediation_handoff(tmp_path)

    assert any(
        issue
        == (
            f"{control_doc_truth._HISTORICAL_REMEDIATION_HANDOFF}/{relative_path}: "
            f"contains stale finalization marker {marker!r}"
        )
        for issue in issues
    )


def test_v011_candidate_allows_ordinary_production_changes_without_old_receipt_pins(tmp_path: Path) -> None:
    _seed_truth_docs(tmp_path, version="0.11.0")
    subprocess.run(("git", "-C", str(tmp_path), "init", "-q"), check=True)
    subprocess.run(("git", "-C", str(tmp_path), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Control Truth Test",
            "-c",
            "user.email=control-truth@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "v0.11 baseline",
        ),
        check=True,
    )
    production_path = tmp_path / "apps" / "api" / "src" / "alicebot_api" / "main.py"
    production_path.parent.mkdir(parents=True, exist_ok=True)
    production_path.write_text("# ordinary v0.11 production change\n", encoding="utf-8")

    issues = control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path)

    assert issues == []


def test_context_tree_docs_match_five_resource_groups_plus_events_and_legacy_boundary() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    integration = (repo_root / "docs/alpha/agent-integration.md").read_text(encoding="utf-8")
    handoff_docs = [
        (repo_root / "docs/handoff/2026-07-14-v0.10.4-remediation" / filename).read_text(encoding="utf-8")
        for filename in (
            "BUILD_REPORT.md",
            "ENGINEER_HANDOFF.md",
            "FIX_MATRIX.md",
            "SURFACE_INVENTORY.md",
        )
    ]

    assert "five resource groups" in changelog
    assert "outside the core MCP surface" in changelog
    assert "five resource groups" in integration
    assert "not part of that core surface" in integration
    assert "disabled on key-bound servers" in integration
    assert all("five resource groups" in text for text in handoff_docs)
    false_markers = (
        "all six groups",
        "its six groups",
        "across the six groups",
        "six memory/source/open-loop/artifact/entity/project groups",
    )
    assert all(marker not in text for text in (changelog, integration, *handoff_docs) for marker in false_markers)


def test_mcp_resume_docs_state_shared_ascii_literal_memory_and_open_loop_contract() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    mcp_docs = (repo_root / "docs/alpha/mcp-tools.md").read_text(encoding="utf-8")
    normalized = " ".join(mcp_docs.split())

    assert "root or nested `next_action` metadata participates only when its" in normalized
    assert "JSON value is a string" in normalized
    assert "Memory title/canonical-text/summary fields selected by" in normalized
    assert "`list_memories(query=...)` and `list_resume_memory_events(query=...)`" in normalized
    assert "all use the same ASCII case-insensitive literal" in normalized
    assert "memory search retains its memory-store matching contract" not in mcp_docs


def test_redaction_docs_do_not_overclaim_source_evidence_erasure() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    documents = [
        (repo_root / relative_path).read_text(encoding="utf-8")
        for relative_path in (
            "docs/memory-operations-protocol.md",
            "docs/alpha/mcp-tools.md",
            "docs/release/v0.11.1-release-notes.md",
            "CURRENT_STATE.md",
        )
    ]

    assert all("source/source-chunk evidence" in document for document in documents)
    assert all("separate source hygiene" in document for document in documents)
    false_erasure_claims = (
        "content everywhere",
        "all persisted content",
        "erases the persisted evidence content",
        "scrubs Alice's persisted copies",
    )
    assert all(claim not in document for document in documents for claim in false_erasure_claims)



@pytest.mark.parametrize(
    "relative", [".ai/active/private-build-notes.md", ".ai/handoff/old-release-review.md", "LOCAL_NOTES.md"]
)
def test_files_git_ignores_are_not_read_by_the_living_doc_scan(tmp_path: Path, relative: str) -> None:
    """A private note that git ignores (the repository's ``.ai`` rules, or a root file in ``.git/info/exclude``) is not
    a living doc, so an old install line in it cannot fail the check; the tracked sprint packet is still read.

    Mutation: make ``_git_ignored`` return an empty set. The ignored note is scanned and its stale tag fails the check.
    """

    _seed_truth_docs(tmp_path, published=True)
    assert control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path) == []
    repo_root = Path(control_doc_truth.__file__).resolve().parents[1]
    (tmp_path / ".gitignore").write_text((repo_root / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("/LOCAL_NOTES.md\n", encoding="utf-8")
    assert (tmp_path / ".ai" / "active" / "SPRINT_PACKET.md").is_file(), "the seed writes the sprint packet"
    note = tmp_path / relative
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Historical internal review\n\nReproduced using git checkout v9.8.6\n", encoding="utf-8")
    scanned = {path.relative_to(tmp_path).as_posix() for path in control_doc_truth.living_doc_files(tmp_path)}
    assert relative not in scanned
    assert ".ai/active/SPRINT_PACKET.md" in scanned
    assert control_doc_truth.run_control_doc_truth_check(root_dir=tmp_path) == []
