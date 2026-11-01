from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ops-evidence.yml"
INTEGRATION_CONFTEXT_PATH = ROOT / "tests" / "integration" / "conftest.py"
JOB_NAME = "Backup, restore, upgrade, and monitoring evidence"
BOOTSTRAP_TEST = (
    "tests/integration/test_local_workspace_bootstrap_api.py::"
    "test_documented_empty_users_seed_then_workspace_bootstrap_under_least_privilege_roles"
)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block(source: str, header: str, indent: int) -> str:
    lines = source.splitlines()
    marker = f"{' ' * indent}{header}"
    matches = [index for index, line in enumerate(lines) if line == marker]
    assert len(matches) == 1, f"expected one workflow block: {header}"
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and _indent(lines[index]) <= indent:
            end = index
            break
    return "\n".join(lines[start:end]) + "\n"


def _clean_scalar(value: str) -> str:
    scalar = value.split(" #", 1)[0].strip()
    if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in {'"', "'"}:
        return scalar[1:-1]
    return scalar


def _field_value(source: str, key: str, indent: int) -> str:
    lines = source.splitlines()
    prefix = f"{' ' * indent}{key}:"
    matches = [index for index, line in enumerate(lines) if _indent(line) == indent and line.startswith(prefix)]
    assert len(matches) == 1, f"expected one workflow field: {key}"
    start = matches[0]
    value = lines[start][len(prefix) :].strip()
    if value not in {"|", "|-", ">", ">-"}:
        return _clean_scalar(value)

    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].strip() and _indent(lines[index]) <= indent:
            end = index
            break
    content_indent = indent + 2
    return "\n".join(line[content_indent:] if line.strip() else "" for line in lines[start + 1 : end])


def _simple_mapping(source: str, indent: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in source.splitlines():
        if not line.strip() or _indent(line) != indent:
            continue
        key, separator, value = line[indent:].partition(":")
        assert separator and value.strip(), f"expected scalar workflow mapping: {line}"
        assert key not in result, f"duplicate workflow mapping key: {key}"
        result[key] = _clean_scalar(value)
    return result


def _direct_keys(source: str, indent: int) -> set[str]:
    keys: set[str] = set()
    for line in source.splitlines():
        if not line.strip() or _indent(line) != indent:
            continue
        key, separator, _value = line[indent:].partition(":")
        assert separator, f"expected workflow mapping key: {line}"
        assert key not in keys, f"duplicate workflow mapping key: {key}"
        keys.add(key)
    return keys


def _job() -> str:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    return _block(workflow, "ops-evidence:", 2)


def _step(job: str, name: str) -> str:
    return _block(_block(job, "steps:", 4), f"- name: {name}", 6)


def test_ops_job_preserves_check_identity_full_history_and_action_pins() -> None:
    job = _job()

    assert _field_value(job, "name", 4) == JOB_NAME
    checkout = _step(job, "Checkout full history")
    assert _direct_keys(checkout, 8) == {"uses", "with"}
    assert _field_value(checkout, "uses", 8) == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert _simple_mapping(_block(checkout, "with:", 8), 10) == {
        "fetch-depth": "0",
    }
    assert (
        _field_value(_step(job, "Set up Python"), "uses", 8)
        == "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"
    )
    assert (
        _field_value(_step(job, "Upload sanitized evidence"), "uses", 8)
        == "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"
    )


def test_root_superuser_is_confined_to_service_and_setup_step() -> None:
    job = _job()
    services = _block(job, "services:", 4)
    postgres = _block(services, "postgres:", 6)
    assert _simple_mapping(_block(postgres, "env:", 8), 10) == {
        "POSTGRES_USER": "alicebot_root",
        "POSTGRES_PASSWORD": "ci-root",
        "POSTGRES_DB": "postgres",
    }
    assert _simple_mapping(_block(job, "env:", 4), 6) == {
        "DATABASE_ADMIN_URL": ("postgresql://alicebot_admin:ci-admin@localhost:5432/alicebot"),
        "DATABASE_URL": "postgresql://alicebot_app:ci-app@localhost:5432/alicebot",
        "DATABASE_BACKUP_URL": ("postgresql://alicebot_backup:ci-backup@localhost:5432/alicebot"),
    }

    setup = _step(job, "Bootstrap extensions and least-privilege roles")
    assert _simple_mapping(_block(setup, "env:", 8), 10) == {
        "PGHOST": "localhost",
        "PGUSER": "alicebot_root",
        "PGPASSWORD": "ci-root",
    }
    unprivileged_job = job.replace(postgres, "", 1).replace(setup, "", 1)
    assert "alicebot_root" not in unprivileged_job
    assert "ci-root" not in unprivileged_job


def test_setup_pins_role_capabilities_and_template_extensions() -> None:
    run = _field_value(
        _step(_job(), "Bootstrap extensions and least-privilege roles"),
        "run",
        8,
    )
    normalized = " ".join(run.split())

    assert normalized.index("psql --dbname template1") < normalized.index("CREATE DATABASE alicebot")
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in normalized
    assert "CREATE EXTENSION IF NOT EXISTS vector" in normalized
    assert (
        "CREATE ROLE alicebot_admin LOGIN PASSWORD 'ci-admin' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
    ) in normalized
    assert (
        "CREATE ROLE alicebot_app LOGIN PASSWORD 'ci-app' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
    ) in normalized
    assert (
        "CREATE ROLE alicebot_backup LOGIN PASSWORD 'ci-backup' NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS"
    ) in normalized
    assert (
        "CREATE ROLE alicebot_drill LOGIN PASSWORD 'ci-drill' NOSUPERUSER CREATEDB NOCREATEROLE NOBYPASSRLS"
    ) in normalized
    for row in (
        "alicebot_admin|f|f|f|f",
        "alicebot_app|f|f|f|f",
        "alicebot_backup|f|f|f|t",
        "alicebot_drill|f|t|f|f",
    ):
        assert row in run
    assert "rolinherit" not in run
    assert "WHERE rolname = 'alicebot_root'" in run
    assert run.count("WHERE extname IN ('pgcrypto', 'vector')") == 2
    assert "CREATE DATABASE alicebot OWNER alicebot_admin TEMPLATE template1" in run


def test_job_runs_the_live_bootstrap_and_both_sanitized_evidence_paths() -> None:
    job = _job()
    bootstrap = _step(job, "Prove empty-user seed and workspace bootstrap")
    assert _simple_mapping(_block(bootstrap, "env:", 8), 10) == {"ALICEBOT_LEAST_PRIVILEGE_DEPLOYMENT": "1"}
    bootstrap_run = _field_value(bootstrap, "run", 8)
    assert BOOTSTRAP_TEST in bootstrap_run
    assert "--require-executed-tests" in bootstrap_run

    deployment = _step(job, "Emit the deployment configuration contract")
    deployment_run = _field_value(deployment, "run", 8)
    assert "scripts/run_single_tenant_deployment_smoke.py" in deployment_run
    assert "--environment ephemeral_ci" in deployment_run
    assert "artifacts/phase5/single-tenant-deployment-smoke.json" in deployment_run

    ops = _step(
        job,
        "Execute both backend drills with the dedicated backup role",
    )
    assert _simple_mapping(_block(ops, "env:", 8), 10) == {
        "DATABASE_LIFECYCLE_URL": ("postgresql://alicebot_drill:ci-drill@localhost:5432/postgres")
    }
    ops_run = _field_value(ops, "run", 8)
    assert "scripts/run_phase5_ops_evidence.py" in ops_run
    assert "--backend all" in ops_run

    upload = _step(job, "Upload sanitized evidence")
    upload_with = _block(upload, "with:", 8)
    assert _field_value(upload_with, "path", 10).splitlines() == [
        "artifacts/phase5/ops-evidence.json",
        "artifacts/phase5/single-tenant-deployment-smoke.json",
    ]
    assert _field_value(upload_with, "if-no-files-found", 10) == "error"


def test_integration_fixture_separates_database_lifecycle_from_admin() -> None:
    source = INTEGRATION_CONFTEXT_PATH.read_text(encoding="utf-8")

    assert 'lifecycle_root_url = os.getenv("DATABASE_LIFECYCLE_URL", admin_root_url)' in source
    assert "psycopg.connect(lifecycle_root_url, autocommit=True)" in source
    assert "psycopg.connect(lifecycle_database_url, autocommit=True)" in source
    assert 'sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}")' in source
    assert 'sql.SQL("GRANT CREATE, USAGE ON SCHEMA public TO {}")' in source
    assert 'config = make_alembic_config(database_urls["admin"])' in source


def test_raw_workflow_parser_rejects_shadow_steps_keys_and_comments() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    job = _job()
    checkout = _step(job, "Checkout full history")

    duplicate_step_workflow = workflow.replace(checkout, checkout + checkout, 1)
    assert duplicate_step_workflow != workflow
    with pytest.raises(AssertionError, match="expected one workflow block"):
        _step(_block(duplicate_step_workflow, "ops-evidence:", 2), "Checkout full history")

    duplicate_env_workflow = workflow.replace(
        "          PGUSER: alicebot_root\n",
        "          PGUSER: alicebot_root\n          PGUSER: alicebot_app\n",
        1,
    )
    assert duplicate_env_workflow != workflow
    duplicate_env_job = _block(duplicate_env_workflow, "ops-evidence:", 2)
    duplicate_env_setup = _step(
        duplicate_env_job,
        "Bootstrap extensions and least-privilege roles",
    )
    with pytest.raises(AssertionError, match="duplicate workflow mapping key: PGUSER"):
        _simple_mapping(_block(duplicate_env_setup, "env:", 8), 10)

    # Derive both pins and their trailing comments from the workflow rather than
    # hardcoding them. A pinned literal here silently decays into a no-op replace
    # after any routine action bump, which retires the mutation while the test
    # keeps passing.
    checkout_use = re.search(
        r"^        uses: (actions/checkout@[0-9a-f]{40})(.*)$", workflow, re.MULTILINE
    )
    python_use = re.search(
        r"^        uses: (actions/setup-python@[0-9a-f]{40})(.*)$", workflow, re.MULTILINE
    )
    assert checkout_use is not None, "workflow no longer pins actions/checkout by digest"
    assert python_use is not None, "workflow no longer pins actions/setup-python by digest"
    checkout_pin = checkout_use.group(1)
    wrong_pin = "actions/checkout@0000000000000000000000000000000000000000"
    scoped_pin_workflow = workflow.replace(
        f"        uses: {checkout_pin}{checkout_use.group(2)}\n",
        f"        uses: {wrong_pin} # {checkout_pin}\n",
        1,
    ).replace(
        f"        uses: {python_use.group(1)}{python_use.group(2)}\n",
        f"        uses: {checkout_pin} # wrong step\n",
        1,
    )
    assert scoped_pin_workflow != workflow
    # Both halves of the mutation must have landed, or the scoping assertions
    # below prove nothing.
    assert wrong_pin in scoped_pin_workflow
    assert "# wrong step" in scoped_pin_workflow
    scoped_job = _block(scoped_pin_workflow, "ops-evidence:", 2)
    scoped_checkout = _step(scoped_job, "Checkout full history")
    assert _field_value(scoped_checkout, "uses", 8) == wrong_pin
    with pytest.raises(AssertionError):
        assert _field_value(scoped_checkout, "uses", 8) == checkout_pin
