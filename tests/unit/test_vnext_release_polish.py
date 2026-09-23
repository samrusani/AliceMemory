from __future__ import annotations

import json
import subprocess
from pathlib import Path
import re
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _read_cli_sources() -> str:
    package_root = ROOT / "apps/api/src/alicebot_api/cli"
    source_paths = sorted(package_root.rglob("*.py"))
    assert source_paths
    return "\n".join(path.read_text(encoding="utf-8") for path in source_paths)


def test_vnext_public_preview_docs_cover_release_polish_acceptance() -> None:
    readme = _read("README.md")
    overview = _read("docs/vnext/README.md")
    quickstart = _read("docs/vnext/quickstart.md")
    architecture = _read("docs/vnext/architecture.md")
    security = _read("docs/vnext/security-privacy.md")
    contributor = _read("docs/vnext/contributor-guide.md")
    checklist = _read("docs/release/vnext-public-release-checklist.md")

    for marker in (
        "The continuity layer for AI agents.",
        "docs/alpha/quickstart.md",
        "ALICE_MCP_LEGACY_TOOLS",
        "alice-memory",
    ):
        assert marker in readme

    assert "docs/alpha/quickstart.md" in quickstart
    assert "Connector Boundary" in architecture
    assert "Prompt-injection content from sources is data, not policy." in security
    assert "Use synthetic fixtures only." in contributor
    assert "No secrets, private exports, real personal data" in checklist


def test_vnext_demo_dataset_is_synthetic_and_connector_ready() -> None:
    payload = json.loads(_read("fixtures/vnext/demo_dataset.json"))
    serialized = json.dumps(payload, sort_keys=True).casefold()

    assert payload["dataset_id"] == "alice-vnext-demo-2026-05"
    assert "browser_clipper" in payload["connector_payloads"]
    assert "telegram" in payload["connector_payloads"]
    assert payload["agent_outputs"][0]["agent_id"] == "openclaw"
    assert payload["policy_boundary_checks"][0]["expected_decision"] == "blocked"
    assert "example.test" in serialized

    forbidden_markers = (
        "sk-",
        "xoxb-",
        "ghp_",
        "password",
        "access_token",
        "refresh_token",
        "@gmail.com",
    )
    for marker in forbidden_markers:
        assert marker not in serialized


def test_public_alpha_packaging_docs_and_commands_are_discoverable() -> None:
    readme = _read("README.md")
    alpha_readme = _read("docs/alpha/README.md")
    quickstart = _read("docs/alpha/quickstart.md")
    first_run = _read("docs/alpha/first-run.md")
    agent_integration = _read("docs/alpha/agent-integration.md")
    mcp_tools = _read("docs/alpha/mcp-tools.md")
    hermes_skill = _read("docs/alpha/hermes-skill.md")
    openclaw_skill = _read("docs/alpha/openclaw-skill.md")
    custom_agent = _read("docs/alpha/custom-agent-guide.md")
    context_recipes = _read("docs/alpha/context-pack-recipes.md")
    memory_recipes = _read("docs/alpha/memory-proposal-recipes.md")
    output_examples = _read("docs/alpha/agent-output-ingestion.md")
    limitations = _read("docs/alpha/known-limitations.md")
    security = _read("docs/alpha/security-and-privacy.md")
    onboarding = _read("docs/alpha/onboarding.md")
    troubleshooting = _read("docs/alpha/troubleshooting.md")
    release_notes = _read("docs/alpha/release-notes.md")
    cto_summary = _read("docs/archive/process/vnext-public-alpha-packaging-cto-summary.md")
    hermes_copy = _read("agent-skills/hermes/alice-memory/SKILL.md")
    openclaw_copy = _read("agent-skills/openclaw/alice-project-memory/SKILL.md")
    makefile = _read("Makefile")
    gitignore = _read(".gitignore")

    for marker in (
        "The continuity layer for AI agents.",
        "make setup",
        "docs/alpha/quickstart.md",
        "docs/alpha/agent-integration.md",
    ):
        assert marker in readme

    assert "alicebot vnext alpha check" in quickstart

    for path_marker in (
        "quickstart.md",
        "first-run.md",
        "agent-integration.md",
        "mcp-tools.md",
        "known-limitations.md",
        "security-and-privacy.md",
    ):
        assert path_marker in alpha_readme

    assert "make setup" in quickstart
    assert "Run doctor" in first_run
    assert "permission_profile" in agent_integration
    assert "alice_vnext_ingest_agent_output" in mcp_tools
    assert "Never directly mutate trusted memory." in hermes_skill
    assert "project_scoped_agent" in openclaw_skill
    assert "Review queues" in custom_agent
    assert context_recipes.count("## ") >= 11
    assert "Do not propose memory for" in memory_recipes
    assert "OpenClaw Sprint Summary" in output_examples
    assert "no hosted cloud" in limitations
    assert "trusted memory is not auto-promoted" in security
    assert "failing command and sanitized output" in onboarding
    assert "Unable to load live workspace: Load failed" in troubleshooting
    assert "alicebot vnext smoke local-cors" in quickstart
    assert "not hosted SaaS" in release_notes
    assert "Agent Skills v1 Hardening" in cto_summary
    assert "trusted_local_agent" in hermes_copy
    assert "project_scoped_agent" in openclaw_copy
    assert "alpha-check" in makefile


def test_headless_ubuntu_packaging_is_discoverable_and_safe_by_default() -> None:
    readme = _read("README.md")
    alpha_readme = _read("docs/alpha/README.md")
    install_doc = _read("docs/alpha/headless-ubuntu-install.md")
    hermes_doc = _read("docs/alpha/hermes-dogfood-ubuntu.md")
    release_notes = _read("docs/release/v0.6.0-alpha-rc.2-release-notes.md")
    cto_summary = _read("docs/archive/process/vnext-headless-ubuntu-cto-summary.md")
    installer = _read("scripts/install-ubuntu.sh")
    uninstaller = _read("scripts/uninstall-ubuntu.sh")
    env_template = _read("packaging/ubuntu/alicebot.env.example")
    web_env_template = _read("apps/web/.env.local.example")
    api_service = _read("packaging/systemd/alice-api.service")
    web_service = _read("packaging/systemd/alice-web.service")
    scheduler_service = _read("packaging/systemd/alice-scheduler.service")
    cli = _read_cli_sources()

    assert "docs/alpha/quickstart.md" in readme
    assert "headless-ubuntu-install.md" in alpha_readme
    assert "ssh -L 3000:127.0.0.1:3000" in install_doc
    assert "Do not expose `/vnext`" in install_doc
    assert "alicebot vnext alpha check --headless" in install_doc
    assert "agent_id: hermes" in hermes_doc
    assert "trusted_local_agent" in hermes_doc
    assert "policy-boundary test" in hermes_doc
    assert "v0.6.0-alpha-rc.2" in release_notes
    assert "not latest" in release_notes
    assert "Headless Ubuntu" in cto_summary

    for marker in (
        "--tag",
        "--branch",
        "--install-dir",
        "--skip-postgres-install",
        "--non-interactive",
        "--install-systemd",
    ):
        assert marker in installer

    assert "--remove-repo" in uninstaller
    assert "--drop-database" in uninstaller
    assert "Type DELETE to continue" in uninstaller

    for marker in (
        "DATABASE_URL=",
        "APP_ENV=development",
        "ALICE_API_HOST=127.0.0.1",
        "ALICE_WEB_HOST=127.0.0.1",
        "ALICE_SECRET_PROVIDER=",
        "CORS_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000",
        "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=http://127.0.0.1:8000",
        'ALICE_MCP_COMMAND="',
    ):
        assert marker in env_template
    assert "NEXT_PUBLIC_ALICEBOT_USER_ID=" in web_env_template

    for service in (api_service, web_service, scheduler_service):
        assert "User=__ALICE_USER__" in service
        assert "Restart=on-failure" in service
        assert "EnvironmentFile=__ALICE_ENV_FILE__" in service
        assert "0.0.0.0" not in service
        assert "%h/.alicebot" not in service

    assert "127.0.0.1" in api_service
    assert "127.0.0.1" in web_service
    assert "127.0.0.1" in scheduler_service
    assert "__ALICE_RUNTIME_DIR__" in api_service
    assert "__ALICE_RUNTIME_DIR__/vnext-scheduler" in scheduler_service
    assert "headless-ubuntu" in cli
    assert "--headless" in cli


def test_ubuntu_installer_uses_template_without_retired_telegram_secrets() -> None:
    template_path = "packaging/ubuntu/alicebot.env.example"
    env_template = _read(template_path)
    installer = _read("scripts/install-ubuntu.sh")

    for retired_marker in (
        "TELEGRAM_BOT_TOKEN",
        "telegram.bot_token.default",
        "X-Telegram-Bot-Api-Secret-Token",
    ):
        assert retired_marker not in env_template

    rendered_template = f'"${{INSTALL_DIR}}/{template_path}" > "${{ENV_FILE}}"'
    assert rendered_template in installer


def test_installation_issue_regressions_are_guarded() -> None:
    makefile = _read("Makefile")
    gitignore = _read(".gitignore")
    web_package = json.loads(_read("apps/web/package.json"))
    installer = _read("scripts/install-ubuntu.sh")
    dev_up = _read("scripts/dev_up.sh")
    api_dev = _read("scripts/api_dev.sh")
    lite_up = _read("scripts/alice_lite_up.sh")
    migrate = _read("scripts/migrate.sh")
    compose = _read("docker-compose.yml")
    compose_lite = _read("docker-compose.lite.yml")
    postgres_init = _read("infra/postgres/init/001_roles.sh")
    install_doc = _read("docs/alpha/headless-ubuntu-install.md")
    troubleshooting = _read("docs/alpha/troubleshooting.md")
    web_env = _read("apps/web/.env.local.example")

    assert "test -f .env || cp .env.example .env" in makefile
    assert "test -f .env.lite || cp .env.lite.example .env.lite" in makefile
    assert "test -f $(WEB_DIR)/.env.local || cp $(WEB_DIR)/.env.local.example $(WEB_DIR)/.env.local" in makefile
    assert "./scripts/validate_env.sh .env .env.lite" in makefile
    assert "./scripts/pnpm_web_install.sh" in makefile
    assert ".env.lite" in gitignore
    assert "apps/web/.env.local" in gitignore

    assert web_package["packageManager"].startswith("pnpm@10.")
    assert web_package["scripts"]["dev:clean"] == "rm -rf .next && next dev"
    assert set(web_package["pnpm"]["onlyBuiltDependencies"]) >= {"esbuild", "sharp", "unrs-resolver"}
    assert "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=http://127.0.0.1:8000" in web_env

    assert "PNPM_VERSION" in installer
    assert "pnpm@latest" not in installer
    assert "install_pnpm_from_npm" in installer
    assert "command -v npm" in installer
    assert '"${npm_bin}" install -g "pnpm@${PNPM_VERSION}"' in installer
    assert 'sudo "${npm_bin}" install -g "pnpm@${PNPM_VERSION}"' in installer
    assert "postgresql-${pg_major}-pgvector" in installer
    assert "CREATE EXTENSION IF NOT EXISTS vector" in installer
    assert 'PGVECTOR_MINIMUM_VERSION="0.8.0"' in installer
    assert 'dpkg --compare-versions "${installed_version}" ge "${PGVECTOR_MINIMUM_VERSION}"' in installer
    assert "ALTER EXTENSION vector UPDATE" in installer
    assert '"${ALICE_RUNTIME_DIR}/vnext-scheduler"' in installer
    assert "run_in_install_dir" in installer
    assert "-c apps/api/alembic.ini" in installer
    assert "seed_default_user_from_env" in installer
    assert '"${INSTALL_DIR}/scripts/seed_local_user.py"' in installer
    assert "INSERT INTO users (id, email, display_name)" not in installer
    assert "write_lite_env_if_missing" in installer
    assert "write_web_env_if_missing" in installer
    assert "validate_env_files" in installer
    migrations_section = installer.split("run_migrations_and_checks()", 1)[1].split("install_systemd_units()", 1)[0]
    assert migrations_section.index("alembic") < migrations_section.index("seed_default_user_from_env")
    assert migrations_section.index("seed_default_user_from_env") < migrations_section.index("vnext doctor")

    for script in (dev_up, api_dev, lite_up, migrate):
        assert "scripts/validate_env.sh" in script
        assert "Missing ${PYTHON_BIN}. Run 'make setup'" in script

    for compose_file in (compose, compose_lite):
        assert "ALICEBOT_COMPOSE_POSTGRES_PASSWORD" in compose_file
        assert "ALICEBOT_COMPOSE_APP_PASSWORD" in compose_file

    assert "ALICEBOT_APP_PASSWORD" in postgres_init
    assert "ALTER ROLE" in postgres_init
    assert 'ALICE_MCP_COMMAND="' in install_doc
    assert "postgresql-16-pgvector" in install_doc
    assert "CREATE EXTENSION IF NOT EXISTS vector" in install_doc
    assert "`~/.alicebot`" in install_doc
    assert "CORS_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000" in install_doc
    assert "docker compose down -v" in install_doc
    assert "Cannot find module './316.js'" in troubleshooting
    assert "pnpm --dir apps/web dev:clean" in troubleshooting


def test_publish_requires_independent_release_control_and_semantic_attestations() -> None:
    semantic_gate = _read(".github/workflows/semantic-release-gate.yml")
    publish = _read(".github/workflows/publish-pypi.yml")
    required_checks = _read("scripts/check_github_release_checks.py")

    early_control_gate = publish.split("- name: Require repository release-control attestation variable", 1)[1].split(
        "- name: Checkout the requested exact release tag", 1
    )[0]
    structured_control_gate = publish.split("- name: Validate release-specific repository controls", 1)[1].split(
        "- name: Fetch the protected main head", 1
    )[0]
    semantic_attestation_gate = publish.split("- name: Verify credential-free semantic eval attestation", 1)[1].split(
        "- name: Recheck release-critical source tests and coverage", 1
    )[0]

    assert "${{ vars.ALICE_RELEASE_CONTROLS_ATTESTATION }}" in early_control_gate
    assert publish.count("${{ vars.ALICE_RELEASE_CONTROLS_ATTESTATION }}") == 2
    assert 'test -n "$RELEASE_CONTROLS_ATTESTATION"' in early_control_gate
    for marker in (
        "python scripts/check_release_controls_attestation.py",
        '--repository "$GITHUB_REPOSITORY"',
        '--release-sha "$GITHUB_SHA"',
        '--release-tag "$RELEASE_TAG"',
        "--attestation-env RELEASE_CONTROLS_ATTESTATION",
    ):
        assert marker in structured_control_gate
    assert "--semantic-eval-attestation" not in structured_control_gate

    assert "Semantic eval attestation (exact SHA)" in semantic_gate
    assert "--release-gate" in semantic_gate
    assert "--write-semantic-eval-attestation" in semantic_gate
    assert "semantic-eval-attestation-${{ github.sha }}" in semantic_gate
    assert "head_sha=${GITHUB_SHA}" in publish
    assert "--semantic-eval-attestation" in semantic_attestation_gate
    assert "check_release_controls_attestation.py" not in semantic_attestation_gate
    assert "Semantic eval attestation (exact SHA)" in required_checks

    assert publish.index("Require repository release-control attestation variable") < publish.index(
        "Require an exact annotated-tag dispatch"
    )
    assert publish.index("Validate release-specific repository controls") < publish.index(
        "Fetch the protected main head"
    )
    assert publish.index("Fetch the protected main head") < publish.index(
        "Verify tag, version, SHA, main head, docs, and PyPI uniqueness"
    )
    assert publish.index("Verify tag, version, SHA, main head, docs, and PyPI uniqueness") < publish.index(
        "Resolve successful exact-SHA semantic gate run"
    )
    assert publish.index("Verify credential-free semantic eval attestation") < publish.index(
        "Build canonical wheel and sdist"
    )


def test_publish_stages_recoverable_exact_draft_before_pypi() -> None:
    publish = _read(".github/workflows/publish-pypi.yml")

    assert "finalize-existing-draft" in publish
    assert "resume-pypi-and-finalize" in publish
    assert "Stage verified recoverable GitHub draft" in publish
    assert "needs: stage-github-draft" in publish
    assert publish.index("stage-github-draft:") < publish.index("publish:")
    assert publish.index("publish:") < publish.index("finalize-github-release:")
    assert "--verify-pypi-artifacts" in publish
    assert "--verify-pypi-artifact-subset" in publish
    assert "--verify-release-assets" in publish
    assert "scripts/render_release_body.py" in publish
    assert "tail -n +3" not in publish
    assert "gh release download" in publish
    assert "--draft=false" in publish
    assert "skip-existing: true" in publish
    assert "release_state" in publish
    resume_verification = publish.split("verify-resume-artifacts:", 1)[1].split(
        "resume-pypi:", 1
    )[0]
    assert "deterministic-rebuild" in resume_verification
    assert "--compare-dist-dir deterministic-rebuild" in resume_verification
    assert resume_verification.index("--compare-dist-dir") < publish.index(
        "Resume only missing exact files through trusted publishing"
    )
    assert "cmp /tmp/alice-release-body.md" in resume_verification


def test_python_compatibility_inputs_are_pinned_and_subprocess_stays_installed() -> None:
    tests_workflow = _read(".github/workflows/tests.yml")
    compatibility = tests_workflow.split("python-compatibility:", 1)[1].split(
        "python-integration:", 1
    )[0]
    sqlite_onramp = _read("tests/unit/test_sqlite_onramp.py")

    assert "python -m pip install build==1.5.0" in compatibility
    assert "python -m pip install pytest==8.4.2" in compatibility
    assert 'ALICE_TEST_INSTALLED_WHEEL: "1"' in compatibility
    assert 'env.get("ALICE_TEST_INSTALLED_WHEEL") == "1"' in sqlite_onramp
    installed_branch = sqlite_onramp.split(
        'if env.get("ALICE_TEST_INSTALLED_WHEEL") == "1":', 1
    )[1].split("else:", 1)[0]
    assert 'env.pop("PYTHONPATH", None)' in installed_branch
    assert 'REPO_ROOT / "apps" / "api" / "src"' not in installed_branch


def test_release_gates_run_normal_cross_module_mypy() -> None:
    makefile = " ".join(_read("Makefile").split())
    tests_workflow = " ".join(_read(".github/workflows/tests.yml").split())
    expected = (
        "python -m mypy --ignore-missing-imports apps/api/src/alicebot_api "
        "scripts/release_check.py scripts/test_distribution_artifact.py "
        "scripts/normalize_sdist.py scripts/render_release_body.py "
        "scripts/decode_github_release_body.py "
        "scripts/prepare_mainprotect_update.py "
        "scripts/check_python_coverage.py "
        "scripts/check_control_doc_truth.py scripts/check_github_release_checks.py "
        "scripts/check_release_controls_attestation.py"
    )

    assert "--follow-imports=skip" not in makefile
    assert "--follow-imports=skip" not in tests_workflow
    assert expected in tests_workflow
    assert expected.replace("python", "$(PYTHON)", 1) in makefile
    assert "Normal cross-module first-party type check" in tests_workflow


def test_dev_dependencies_pin_coverage_floor_and_supported_pytest_major() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert "coverage>=7.7,<8.0" in dev_dependencies
    assert "pytest>=8.3,<10.0" in dev_dependencies


def test_release_workflow_is_manual_only_and_scheduler_child_preserves_once() -> None:
    publish = _read(".github/workflows/publish-pypi.yml")
    trigger_block = publish.split("permissions:", 1)[0]
    scheduler_runtime = _read("apps/api/src/alicebot_api/vnext_scheduler_runtime.py")
    background_start = scheduler_runtime.split("def start_background_daemon", 1)[1].split(
        "def run_foreground_daemon", 1
    )[0]

    assert "  workflow_dispatch:" in trigger_block
    assert "\n  release:" not in trigger_block
    assert 'if config.once:\n        command.append("--once")' in background_start
    assert background_start.index('command.append("--once")') < background_start.index(
        "subprocess.Popen("
    )


def test_pnpm10_dependency_audit_decision_is_fail_closed_and_documented() -> None:
    package = json.loads(_read("apps/web/package.json"))
    workflow = _read(".github/workflows/tests.yml")
    audit_script = _read("apps/web/scripts/npm-advisory-audit.mjs")
    releasing = _read("RELEASING.md")

    assert package["packageManager"] == "pnpm@10.23.0"
    assert package["devDependencies"]["semver"] == "7.8.0"
    assert "node-version: \"20\"" in workflow
    assert "node scripts/npm-advisory-audit.mjs --prod --audit-level=high" in workflow
    assert "node scripts/npm-advisory-audit.mjs --audit-level=high" in workflow
    assert "pnpm test:advisory-audit" in workflow
    assert package["scripts"]["test:advisory-audit"] == (
        "node --test scripts/npm-advisory-audit.test.mjs"
    )
    assert "https://github.com/orgs/pnpm/discussions/11377" in audit_script
    assert "https://github.com/orgs/pnpm/discussions/11377" in releasing
    assert "process.exit(2)" in audit_script


def test_ci_action_dependency_carrier_uses_exact_atomic_pins() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )
    checkout_refs = re.findall(r"actions/checkout@([0-9a-f]+)", workflows)
    codeql_refs = re.findall(
        r"github/codeql-action/(?:init|autobuild|analyze)@([0-9a-f]+)",
        workflows,
    )

    # 20 since the commit author check workflow joined the pull request
    # workflows. The count is the point: it forces a new action usage to be
    # reviewed rather than absorbed.
    assert checkout_refs == [
        "3d3c42e5aac5ba805825da76410c181273ba90b1"
    ] * 20
    assert codeql_refs == [
        "f205ea1c3313d32999d8d6a48b4f6530d4437b38"
    ] * 3
    security_workflow = _read(".github/workflows/security-scans.yml")
    for step in ("init", "autobuild", "analyze"):
        assert (
            f"github/codeql-action/{step}@"
            "f205ea1c3313d32999d8d6a48b4f6530d4437b38 # v4.37.4"
        ) in security_workflow


def test_python_compatibility_functional_tests_do_not_shadow_installed_wheel() -> None:
    tests_workflow = _read(".github/workflows/tests.yml")
    artifact_smoke = _read("scripts/test_distribution_artifact.py")
    compatibility = tests_workflow.split("python-compatibility:", 1)[1].split(
        "python-integration:", 1
    )[0]

    assert 'PYTHONPATH: ""' in compatibility
    assert "python -m pytest -o pythonpath=" in " ".join(compatibility.split())
    assert "resolved to checkout source instead of the installed wheel" in compatibility
    assert "Representative installed-wheel functional tests" in compatibility
    assert "tests/unit/test_cli_error_contracts.py" in compatibility
    assert "tests/unit/test_cli_package_split.py" in compatibility
    for marker in (
        "import alicebot_api.cli.parser as cli_parser",
        "import alicebot_api.cli.runner as cli_runner",
        "cli_module.build_parser is cli_parser.build_parser",
        "cli_module.main is cli_runner.main",
    ):
        assert marker in compatibility
        assert marker in artifact_smoke


def test_local_playwright_setup_is_explicit_idempotent_and_platform_safe() -> None:
    makefile = _read("Makefile")
    releasing = _read("RELEASING.md")
    web_package = json.loads(_read("apps/web/package.json"))
    tests_workflow = _read(".github/workflows/tests.yml")

    local_install = web_package["scripts"]["setup:browser"]
    linux_install = web_package["scripts"]["setup:browser:linux"]
    assert local_install == "playwright install chromium"
    assert "--with-deps" not in local_install
    assert linux_install == "playwright install --with-deps chromium"

    setup_target = makefile.split("setup-browser:", 1)[1].split("\n\n", 1)[0]
    assert "$(PNPM) --dir $(WEB_DIR) run setup:browser" in setup_target
    assert "--with-deps" not in setup_target
    linux_target = makefile.split("setup-browser-linux:", 1)[1].split("\n\n", 1)[0]
    assert 'test "$$(uname -s)" = "Linux"' in linux_target
    assert "$(PNPM) --dir $(WEB_DIR) run setup:browser:linux" in linux_target
    assert "test-web: setup-browser" in makefile

    candidate_commands = releasing.split("## Candidate Gate", 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    assert candidate_commands.index("make setup\n") < candidate_commands.index("make setup-browser\n")
    assert candidate_commands.index("make setup-browser\n") < candidate_commands.index("make release-check")
    normalized_releasing = " ".join(releasing.split())
    assert "idempotent local prerequisite" in normalized_releasing
    assert "not appropriate on macOS" in normalized_releasing
    assert "make setup-browser-linux" in normalized_releasing
    assert "refuses to run on macOS" in normalized_releasing

    assert "run: pnpm exec playwright install chromium" in tests_workflow
    assert "run: pnpm run setup:browser:linux" not in tests_workflow


def test_env_validator_rejects_unquoted_values_with_spaces(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "APP_ENV=development",
                "DATABASE_URL=postgresql://alicebot_app:alicebot_app@localhost:5432/alicebot",
                "DATABASE_ADMIN_URL=postgresql://alicebot_admin:alicebot_admin@localhost:5432/alicebot",
                "ALICE_MCP_COMMAND=/tmp/alicebot/.venv/bin/python -m alicebot_api.mcp_server",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "scripts" / "validate_env.sh"), str(env_file)],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "quote ALICE_MCP_COMMAND" in result.stderr

    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(
            "ALICE_MCP_COMMAND=/tmp/alicebot/.venv/bin/python -m alicebot_api.mcp_server",
            'ALICE_MCP_COMMAND="/tmp/alicebot/.venv/bin/python -m alicebot_api.mcp_server"',
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "scripts" / "validate_env.sh"), str(env_file)],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0



@pytest.mark.parametrize(
    "s3_lines",
    [
        (),
        ("S3_ACCESS_KEY=alicebot", "S3_SECRET_KEY=alicebot-secret"),
    ],
)
def test_env_validator_accepts_core_only_production_without_s3_credentials_or_overrides(
    tmp_path: Path,
    s3_lines: tuple[str, ...],
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "APP_ENV=production",
                "DATABASE_URL=postgresql://alicebot_app:custom@localhost:5432/alicebot",
                "DATABASE_ADMIN_URL=postgresql://alicebot_admin:custom@localhost:5432/alicebot",
                "ALICEBOT_AUTH_USER_ID=00000000-0000-4000-8000-000000000001",
                *s3_lines,
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(ROOT / "scripts" / "validate_env.sh"), str(env_file)],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
