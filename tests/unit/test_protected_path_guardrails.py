from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_protected_paths.py"
_SPEC = importlib.util.spec_from_file_location("check_protected_paths", _MODULE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
guardrails = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = guardrails
_SPEC.loader.exec_module(guardrails)


def test_categorize_files_tracks_overlapping_protected_areas() -> None:
    touched = guardrails.categorize_files(
        [
            "apps/api/src/alicebot_api/contracts.py",
            "apps/api/src/alicebot_api/trusted_fact_promotions.py",
            "README.md",
        ]
    )

    assert sorted(touched) == [
        "continuity APIs",
        "memory schema",
        "promotion logic",
        "trust rules",
    ]
    assert touched["promotion logic"] == ["apps/api/src/alicebot_api/trusted_fact_promotions.py"]


def test_validate_upgrade_overview_skips_non_protected_changes() -> None:
    assert guardrails.validate_upgrade_overview("", {}) == []


def test_categorize_files_protects_split_router_modules_as_continuity_apis() -> None:
    router_path = "apps/api/src/alicebot_api/routers/vnext_retrieval.py"

    touched = guardrails.categorize_files([router_path])

    assert touched == {"continuity APIs": [router_path]}


def test_categorize_files_protects_split_mcp_modules_as_continuity_apis() -> None:
    carrier_path = "apps/api/src/alicebot_api/mcp/registry.py"

    touched = guardrails.categorize_files([carrier_path])

    assert touched == {"continuity APIs": [carrier_path]}


def test_categorize_files_protects_split_cli_modules_as_continuity_apis() -> None:
    cli_paths = [
        "apps/api/src/alicebot_api/cli.py",
        "apps/api/src/alicebot_api/cli/runner.py",
    ]

    touched = guardrails.categorize_files(cli_paths)

    assert touched == {"continuity APIs": cli_paths}


def test_categorize_files_protects_split_legacy_store_modules_as_memory_schema() -> None:
    carrier_path = "apps/api/src/alicebot_api/legacy_store/providers_knowledge.py"

    touched = guardrails.categorize_files([carrier_path])

    assert touched == {"memory schema": [carrier_path]}


def test_categorize_files_protects_paired_vnext_store_seams_as_memory_schema() -> None:
    carrier_paths = [
        "apps/api/src/alicebot_api/vnext_store.py",
        "apps/api/src/alicebot_api/sqlite_store.py",
        "apps/api/src/alicebot_api/vnext_stores/memory_lifecycle_common.py",
        "apps/api/src/alicebot_api/vnext_stores/postgres/embedding_cas.py",
        "apps/api/src/alicebot_api/vnext_stores/sqlite/embedding_cas.py",
    ]

    touched = guardrails.categorize_files(carrier_paths)

    assert touched == {"memory schema": carrier_paths}


def test_categorize_files_protects_split_contract_modules_in_all_facade_areas() -> None:
    carrier_path = "apps/api/src/alicebot_api/_contracts/governance.py"

    touched = guardrails.categorize_files([carrier_path])

    assert touched == {
        "continuity APIs": [carrier_path],
        "memory schema": [carrier_path],
        "trust rules": [carrier_path],
    }


def test_validate_upgrade_overview_requires_checked_areas_and_notes() -> None:
    touched = {
        "memory schema": ["apps/api/alembic/versions/20260410_9999_example.py"],
        "continuity APIs": ["apps/api/src/alicebot_api/main.py"],
    }

    errors = guardrails.validate_upgrade_overview(
        """
## Upgrade Overview

### Protected Areas

- [x] memory schema

### Compatibility Impact

TBD

### Migration / Rollout

Pending

### Operator Action

None

### Validation

N/A
""",
        touched,
    )

    assert any("continuity APIs" in error for error in errors)
    assert any("Compatibility Impact" in error for error in errors)
    assert any("Rollback" in error for error in errors)


def test_validate_upgrade_overview_accepts_complete_metadata() -> None:
    touched = {
        "evidence pipeline": ["apps/api/src/alicebot_api/continuity_evidence.py"],
        "trust rules": ["apps/api/src/alicebot_api/memory.py"],
    }

    errors = guardrails.validate_upgrade_overview(
        """
## Summary

Short summary.

## Upgrade Overview

### Protected Areas

- [x] evidence pipeline
- [x] trust rules

### Compatibility Impact

Additive internal change only. Existing archived evidence rows stay readable and no API enum changes occur.

### Migration / Rollout

No deploy sequencing beyond the normal application rollout. Existing data remains valid without backfill.

### Operator Action

No manual operator action is required for this change.

### Validation

Ran targeted unit coverage for the guardrail parser and reviewed the protected-path mapping against the touched files.

### Rollback

Revert the change set and redeploy. No irreversible data rewrite occurs in this path.
""",
        touched,
    )

    assert errors == []


def test_validate_upgrade_overview_accepts_checked_continuity_apis_label() -> None:
    touched = {
        "continuity APIs": ["apps/api/src/alicebot_api/main.py"],
    }

    errors = guardrails.validate_upgrade_overview(
        """
## Upgrade Overview

### Protected Areas

- [x] continuity APIs

### Compatibility Impact

Additive-only API changes with backward-compatible request and response fields.

### Migration / Rollout

No extra migration sequencing beyond standard deployment.

### Operator Action

No manual operator steps are required.

### Validation

Executed guardrail parser tests and validated checked-area coverage for continuity APIs.

### Rollback

Revert the change and redeploy to restore the previous behavior.
""",
        touched,
    )

    assert not any("continuity APIs" in error for error in errors)


def _repair_batch_9_upgrade_overview() -> str:
    repo_root = Path(__file__).resolve().parents[2]
    handoff = (repo_root / "docs/handoff/2026-07-14-v0.10.4-remediation/ENGINEER_HANDOFF.md").read_text(
        encoding="utf-8"
    )
    fence = "```md\n## Upgrade Overview\n"
    start = handoff.index(fence) + len("```md\n")
    end = handoff.index("\n```", start)
    return handoff[start:end] + "\n"


def test_repair_batch_9_handoff_upgrade_overview_passes_representative_guard(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    overview = _repair_batch_9_upgrade_overview()
    sections = guardrails.extract_upgrade_sections(overview)

    assert guardrails.parse_checked_areas(sections["protected areas"]) == {
        "memory schema",
        "continuity apis",
    }
    representative_files = [
        "apps/api/alembic/versions/20260714_0090_project_scope_identity.py",
        "apps/api/src/alicebot_api/main.py",
    ]
    touched = guardrails.categorize_files(representative_files)
    assert set(touched) == {"memory schema", "continuity APIs"}
    assert guardrails.validate_upgrade_overview(overview, touched) == []

    event_path = tmp_path / "pull-request-event.json"
    event_path.write_text(
        json.dumps(
            {
                "pull_request": {
                    "base": {"sha": "63397ab^"},
                    "head": {"sha": "63397ab"},
                    "body": overview,
                }
            }
        ),
        encoding="utf-8",
    )
    observed_range: list[tuple[str, str]] = []

    def _representative_changed_files(base_sha: str, head_sha: str) -> list[str]:
        observed_range.append((base_sha, head_sha))
        return representative_files

    monkeypatch.setattr(guardrails, "changed_files_between", _representative_changed_files)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_protected_paths.py", "--event-path", str(event_path)],
    )

    assert guardrails.main() == 0
    assert observed_range == [("63397ab^", "63397ab")]
    assert "Protected-path upgrade metadata is present." in capsys.readouterr().out
