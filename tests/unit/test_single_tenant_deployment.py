from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "run_single_tenant_deployment_smoke.py"
_SPEC = importlib.util.spec_from_file_location("run_single_tenant_deployment_smoke", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
deployment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deployment)


def _asset(path: Path) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _valid_env() -> dict[str, str]:
    return deployment.parse_env_example(_asset(deployment.ENV_RELATIVE_PATH))


def _copy_contract_tree(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / ".gitignore", target / ".gitignore")
    for relative_path in deployment.CONTRACT_INPUTS.values():
        destination = target / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative_path, destination)
    _git(target, "init", "-q")
    _git(target, "config", "user.email", "phase5-deployment@example.invalid")
    _git(target, "config", "user.name", "Phase 5 Deployment Test")
    _git(target, "add", ".")
    _git(target, "commit", "-q", "-m", "deployment contract baseline")


def test_checked_in_configuration_contract_passes_without_cloud_claims() -> None:
    report = deployment.run_smoke(root=ROOT, environment="ephemeral_ci")

    assert report["status"] == "passed"
    assert report["environment"] == "ephemeral_ci"
    assert report["cloud_provider"] == "none"
    assert report["public_dns"] is False
    assert report["public_ca"] is False
    assert report["evidence_kind"] == "configuration_contract_only"
    assert report["real_cloud_host_exercised"] is False
    assert report["blockers"] == ["owner_real_host_deployment_receipt"]
    assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", report["source_head_commit"])
    assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", report["source_head_tree"])
    assert report["carrier_state"] in {"clean", "dirty"}
    assert re.fullmatch(r"[0-9a-f]{64}", report["carrier_snapshot_sha256"])
    asset_hashes = report["validated_asset_sha256"]
    assert set(asset_hashes) == set(deployment.VALIDATED_ASSETS)
    for logical_name, relative_path in deployment.VALIDATED_ASSETS.items():
        assert asset_hashes[logical_name] == hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
    assert set(report["checks"]) == {
        "api_and_web_loopback",
        "database_role_and_tls_contract",
        "exact_https_origin",
        "local_user_seed_contract",
        "proxy_trust_boundary",
        "web_exact_origin_trust",
        "guide_claim_boundaries",
        "supply_chain_pins",
    }


def test_deployment_smoke_pins_web_live_and_bearer_trust_to_loopback_or_exact_https_origin() -> None:
    source = _asset(deployment.WEB_API_SOURCE_RELATIVE_PATH)

    deployment.validate_web_trust_contract(source)

    assert "export function isTrustedApiBaseUrl" in source
    assert "parsed.origin === currentAliceWebOrigin()" in source
    assert "isTrustedApiBaseUrl(config.apiBaseUrl)" in source
    assert "isTrustedApiBaseUrl(apiBaseUrl)" in source


@pytest.mark.parametrize(
    ("old", "new", "failure_code"),
    (
        (
            "process.env.PUBLIC_ORIGIN",
            '"https://alice.example.com"',
            "web_current_origin_contract_invalid",
        ),
        (
            "parsed.origin === currentAliceWebOrigin()",
            "Boolean(parsed.origin)",
            "web_exact_origin_trust_invalid",
        ),
        (
            "config.userId.trim() && isTrustedApiBaseUrl(config.apiBaseUrl)",
            "config.userId.trim()",
            "web_live_config_bypasses_trust",
        ),
        (
            "isTrustedApiBaseUrl(apiBaseUrl) &&\n    (logicalPath",
            "true &&\n    (logicalPath",
            "web_operator_key_bypasses_trust",
        ),
    ),
)
def test_web_trust_source_contract_fails_closed_if_exact_origin_checks_are_removed(
    old: str,
    new: str,
    failure_code: str,
) -> None:
    source = _asset(deployment.WEB_API_SOURCE_RELATIVE_PATH)
    assert old in source

    with pytest.raises(deployment.DeploymentContractError) as exc_info:
        deployment.validate_web_trust_contract(source.replace(old, new, 1))

    assert exc_info.value.code == failure_code


@pytest.mark.parametrize(
    ("logical_name", "relative_path", "suffix"),
    (
        ("caddyfile_example", deployment.CADDY_RELATIVE_PATH, b"\n# carrier mutation\n"),
        ("deployment_guide", deployment.GUIDE_RELATIVE_PATH, b"\n<!-- carrier mutation -->\n"),
        ("environment_example", deployment.ENV_RELATIVE_PATH, b"\n# carrier mutation\n"),
        ("local_user_seed_helper", deployment.SEED_HELPER_RELATIVE_PATH, b"\n# carrier mutation\n"),
        ("web_api_source", deployment.WEB_API_SOURCE_RELATIVE_PATH, b"\n// carrier mutation\n"),
        ("workflow", deployment.WORKFLOW_RELATIVE_PATH, b"\n# carrier mutation\n"),
    ),
)
def test_each_validated_asset_hash_and_carrier_snapshot_bind_uncommitted_bytes(
    tmp_path,
    logical_name: str,
    relative_path: Path,
    suffix: bytes,
) -> None:
    repo = tmp_path / "repo"
    _copy_contract_tree(repo)
    clean = deployment.run_smoke(root=repo, environment="ephemeral_ci")
    assert clean["carrier_state"] == "clean"

    changed_path = repo / relative_path
    changed_path.write_bytes(changed_path.read_bytes() + suffix)
    changed = deployment.run_smoke(root=repo, environment="ephemeral_ci")

    assert changed["source_head_commit"] == clean["source_head_commit"]
    assert changed["source_head_tree"] == clean["source_head_tree"]
    assert changed["carrier_state"] == "dirty"
    assert changed["validated_asset_sha256"][logical_name] != clean["validated_asset_sha256"][logical_name]
    assert changed["carrier_snapshot_sha256"] != clean["carrier_snapshot_sha256"]


def test_carrier_snapshot_hashes_symlink_target_without_following_external_bytes(tmp_path) -> None:
    repo = tmp_path / "repo"
    _copy_contract_tree(repo)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("external secret version one\n", encoding="utf-8")
    os.symlink(outside, repo / "external-link")

    linked = deployment.run_smoke(root=repo, environment="ephemeral_ci")
    outside.write_text("external secret version two\n", encoding="utf-8")
    after_external_change = deployment.run_smoke(root=repo, environment="ephemeral_ci")

    assert linked["carrier_state"] == "dirty"
    assert after_external_change == linked
    serialized = json.dumps(linked, sort_keys=True)
    assert "external secret" not in serialized
    assert str(tmp_path) not in serialized


def test_rendered_web_environment_is_ignored_without_hiding_tracked_sources(
    tmp_path: Path,
) -> None:
    ignored = _git(ROOT, "check-ignore", "-v", "apps/web/.env.production.local")
    assert "apps/web/.env*.local" in ignored
    assert (
        _git(ROOT, "ls-files", "--error-unmatch", "apps/web/.env.local.example")
        == "apps/web/.env.local.example"
    )
    assert _git(ROOT, "ls-files", "-i", "-c", "--exclude-standard") == ""

    repo = tmp_path / "repo"
    _copy_contract_tree(repo)
    clean = deployment.run_smoke(root=repo, environment="ephemeral_ci")
    web_env = repo / "apps" / "web" / ".env.production.local"
    web_env.write_text(
        "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=https://alice.example.com\n",
        encoding="utf-8",
    )
    first_render = deployment.run_smoke(root=repo, environment="ephemeral_ci")
    web_env.write_text(
        "NEXT_PUBLIC_ALICEBOT_API_BASE_URL=https://alice.changed.example\n",
        encoding="utf-8",
    )
    second_render = deployment.run_smoke(root=repo, environment="ephemeral_ci")

    assert _git(repo, "check-ignore", "-v", "apps/web/.env.production.local")
    assert first_render == clean
    assert second_render == clean


def test_environment_example_is_production_loopback_exact_origin_and_role_separated() -> None:
    values = _valid_env()
    guide = _asset(deployment.GUIDE_RELATIVE_PATH)

    deployment.validate_environment(values)
    deployment.validate_role_separated_database_contract(values, guide)

    assert values["APP_ENV"] == "production"
    assert values["APP_HOST"] == values["ALICE_WEB_HOST"] == "127.0.0.1"
    assert values["CORS_ALLOWED_ORIGINS"] == "https://alice.example.com"
    assert values["NEXT_PUBLIC_ALICEBOT_API_BASE_URL"] == values["CORS_ALLOWED_ORIGINS"]
    assert values["TRUST_PROXY_HEADERS"] == "true"
    assert values["TRUSTED_PROXY_IPS"] == "127.0.0.1"
    assert values["ALICE_LEGACY_SURFACES"] == "0"
    assert values["LEGACY_V0_ENABLED_OUTSIDE_DEV"] == "false"
    assert values["ALICEBOT_AUTH_USER_ID"] == "00000000-0000-0000-0000-000000000001"
    assert values["NEXT_PUBLIC_ALICEBOT_USER_ID"] == values["ALICEBOT_AUTH_USER_ID"]
    assert "alicebot_app:${ALICEBOT_DB_APP_PASSWORD}" in values["DATABASE_URL"]
    assert "DATABASE_ADMIN_URL" not in values
    assert "alicebot_admin:${ALICEBOT_DB_ADMIN_PASSWORD}" in guide
    assert "alicebot_backup:${ALICEBOT_DB_BACKUP_PASSWORD}" in guide
    assert "alicebot_drill:${ALICEBOT_DB_DRILL_PASSWORD}" in guide
    assert "sslmode=verify-full" in values["DATABASE_URL"]
    assert f"sslrootcert={deployment.POSTGRES_CA_PATH}" in values["DATABASE_URL"]


def test_local_user_seed_helper_uses_admin_dsn_and_transaction_local_rls_context() -> None:
    source = _asset(deployment.SEED_HELPER_RELATIVE_PATH)

    deployment.validate_seed_helper_contract(source)

    with pytest.raises(deployment.DeploymentContractError) as exc_info:
        deployment.validate_seed_helper_contract(
            source.replace("set_current_user(conn, user_id)", "pass", 1)
        )
    assert exc_info.value.code == "local_user_seed_contract_invalid"

    with pytest.raises(deployment.DeploymentContractError) as fallback_error:
        deployment.validate_seed_helper_contract(
            source.replace(
                'current_env.get("DATABASE_ADMIN_URL", "").strip()',
                'current_env.get("DATABASE_URL", "").strip()',
                1,
            )
        )
    assert fallback_error.value.code == "local_user_seed_contract_invalid"


@pytest.mark.parametrize(
    ("key", "value", "failure_code"),
    (
        ("APP_ENV", "development", "app_env_not_production"),
        ("APP_HOST", "0.0.0.0", "api_bind_not_loopback"),
        ("ALICE_WEB_HOST", "0.0.0.0", "web_bind_not_loopback"),
        ("CORS_ALLOWED_ORIGINS", "*", "cors_origin_invalid"),
        ("CORS_ALLOWED_ORIGINS", "http://alice.example.com", "cors_origin_invalid"),
        ("CORS_ALLOWED_ORIGINS", "https://alice.example.com,https://evil.example", "cors_origin_invalid"),
        ("TRUSTED_PROXY_IPS", "0.0.0.0/0", "trusted_proxy_invalid"),
        ("TRUST_PROXY_HEADERS", "false", "proxy_headers_not_enabled"),
        ("LEGACY_V0_ENABLED_OUTSIDE_DEV", "true", "legacy_v0_production_gate_enabled"),
    ),
)
def test_environment_validation_fails_closed_on_network_boundary_drift(
    key: str,
    value: str,
    failure_code: str,
) -> None:
    values = _valid_env()
    values[key] = value
    if key == "CORS_ALLOWED_ORIGINS":
        values["PUBLIC_ORIGIN"] = value
        values["NEXT_PUBLIC_ALICEBOT_API_BASE_URL"] = value

    with pytest.raises(deployment.DeploymentContractError) as exc_info:
        deployment.validate_environment(values)

    assert exc_info.value.code == failure_code


@pytest.mark.parametrize(
    ("target", "old", "new", "failure_code"),
    (
        (
            "runtime",
            "sslmode=verify-full",
            "sslmode=require",
            "database_tls_invalid",
        ),
        (
            "migration",
            f"sslrootcert={deployment.POSTGRES_CA_PATH}",
            "sslrootcert=/tmp/untrusted-ca.pem",
            "database_ca_path_invalid",
        ),
        (
            "runtime",
            "${ALICEBOT_DB_APP_PASSWORD}",
            "literal-password",
            "database_secret_placeholder_invalid",
        ),
        (
            "migration",
            "alicebot_admin:${ALICEBOT_DB_ADMIN_PASSWORD}",
            "alicebot_app:${ALICEBOT_DB_ADMIN_PASSWORD}",
            "database_role_invalid",
        ),
        (
            "runtime",
            "postgresql://alicebot_app:${ALICEBOT_DB_APP_PASSWORD}@db.alice.internal",
            "postgresql://[invalid",
            "database_url_invalid",
        ),
        (
            "migration",
            "db.alice.internal:5432",
            "db.alice.internal:5433",
            "database_endpoints_mismatch",
        ),
        (
            "backup",
            "alicebot_backup:${ALICEBOT_DB_BACKUP_PASSWORD}",
            "alicebot_app:${ALICEBOT_DB_BACKUP_PASSWORD}",
            "database_role_invalid",
        ),
        (
            "lifecycle",
            "db.alice.internal:5432/postgres",
            "db.alice.internal:5432/alicebot",
            "database_name_invalid",
        ),
    ),
)
def test_database_contract_rejects_weak_tls_paths_literal_secrets_and_shared_roles(
    target: str,
    old: str,
    new: str,
    failure_code: str,
) -> None:
    values = _valid_env()
    guide = _asset(deployment.GUIDE_RELATIVE_PATH)
    if target == "runtime":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace(old, new)
    else:
        guide = guide.replace(old, new, 1)

    with pytest.raises(deployment.DeploymentContractError) as exc_info:
        deployment.validate_environment(values)
        deployment.validate_role_separated_database_contract(values, guide)

    assert exc_info.value.code == failure_code


@pytest.mark.parametrize(
    ("key", "role", "placeholder", "database"),
    (
        (
            "DATABASE_ADMIN_URL",
            "alicebot_admin",
            "${ALICEBOT_DB_ADMIN_PASSWORD}",
            "alicebot",
        ),
        (
            "DATABASE_BACKUP_URL",
            "alicebot_backup",
            "${ALICEBOT_DB_BACKUP_PASSWORD}",
            "alicebot",
        ),
        (
            "DATABASE_LIFECYCLE_URL",
            "alicebot_drill",
            "${ALICEBOT_DB_DRILL_PASSWORD}",
            "postgres",
        ),
    ),
)
def test_runtime_environment_rejects_privileged_dsns(
    key: str,
    role: str,
    placeholder: str,
    database: str,
) -> None:
    values = _valid_env()
    values[key] = (
        f"postgresql://{role}:{placeholder}@db.alice.internal:5432/"
        f"{database}?sslmode=verify-full&sslrootcert={deployment.POSTGRES_CA_PATH}"
    )

    with pytest.raises(deployment.DeploymentContractError) as exc_info:
        deployment.validate_environment(values)

    assert exc_info.value.code == "deprecated_or_inline_secret_setting_present"


def test_migration_entrypoint_fails_clearly_without_admin_dsn_or_repo_venv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts" / "migrate.sh", scripts / "migrate.sh")
    assert not (repo / ".venv").exists()

    environment = os.environ.copy()
    environment["DATABASE_ADMIN_URL"] = ""

    completed = subprocess.run(
        ["bash", str(scripts / "migrate.sh")],
        cwd=repo,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "DATABASE_ADMIN_URL is required for migrations" in completed.stderr
    assert "Run 'make setup'" not in completed.stderr


def test_caddy_example_requires_mtls_and_preserves_real_client_ip() -> None:
    caddyfile = _asset(deployment.CADDY_RELATIVE_PATH)

    deployment.validate_caddyfile(caddyfile)

    assert "client_auth" in caddyfile
    assert "mode require_and_verify" in caddyfile
    assert f"trust_pool file {deployment.CLIENT_CA_PATH}" in caddyfile
    assert "strict_sni_host on" in caddyfile
    assert "header_up X-Forwarded-For" not in caddyfile
    assert "/v0/vnext /v0/vnext/*" in caddyfile
    assert "/v0/*" not in caddyfile
    assert "/v1/*" not in caddyfile
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in caddyfile
    assert 'Content-Security-Policy "frame-ancestors \'none\'"' in caddyfile
    assert 'X-Frame-Options "DENY"' in caddyfile


_MTLS_STANZA = """\ttls {
\t\t# Install the public certificate of the operator CA that issues one
\t\t# client certificate per authorized browser or agent at this path.
\t\tclient_auth {
\t\t\tmode require_and_verify
\t\t\ttrust_pool file /etc/alicebot/client-ca.pem
\t\t}
\t}
"""

_PUBLIC_LANDING_BLOCK = """

alicememory.com, www.alicememory.com {
\troot * /srv/landing
\tfile_server
}
"""

_DECOY_MTLS_BLOCK = """

decoy.example.com {
\ttls {
\t\tclient_auth {
\t\t\tmode require_and_verify
\t\t\ttrust_pool file /etc/alicebot/client-ca.pem
\t\t}
\t}
}
"""

_PUBLIC_BLOCK_PROXYING_TO_ALICE = """

alicememory.com {
\treverse_proxy 127.0.0.1:8000
}
"""


def test_a_public_site_block_is_allowed_when_it_cannot_reach_alice() -> None:
    """Adding a landing page on another hostname must not require weakening mTLS.

    This is the supported way to put a public page on the same host: a separate site
    block with no `tls` directive and, critically, no `reverse_proxy` to Alice.
    """

    caddyfile = _asset(deployment.CADDY_RELATIVE_PATH)

    deployment.validate_caddyfile(caddyfile + _PUBLIC_LANDING_BLOCK)


@pytest.mark.parametrize(
    ("mutation", "failure_code"),
    (
        pytest.param(
            lambda text: text.replace(_MTLS_STANZA, "") + _DECOY_MTLS_BLOCK,
            "caddy_authentication_missing",
            id="alice-block-loses-mtls-while-another-block-still-carries-the-strings",
        ),
        pytest.param(
            lambda text: text + _PUBLIC_BLOCK_PROXYING_TO_ALICE,
            "caddy_authentication_missing",
            id="a-second-block-proxies-to-alice-without-demanding-a-certificate",
        ),
    ),
)
def test_caddy_mtls_assertions_are_scoped_to_the_block_that_serves_alice(
    mutation, failure_code: str
) -> None:
    """The file-wide checks proved "some block has mTLS", not "Alice's block has mTLS".

    Reproduced 2026-08-15 against the shipped validator: stripping the whole tls stanza
    from the Alice block and leaving those strings anywhere else in the file passed every
    check. The hole was harmless while the example had one site block and opens the moment
    a second one is added, which is exactly what putting a landing page on the apex does.
    """

    caddyfile = _asset(deployment.CADDY_RELATIVE_PATH)

    with pytest.raises(deployment.DeploymentContractError) as excinfo:
        deployment.validate_caddyfile(mutation(caddyfile))

    assert str(excinfo.value) == failure_code


def test_the_mtls_stanza_constant_still_matches_the_shipped_example() -> None:
    """Keeps the mutation above from silently becoming a no-op after a reformat."""

    caddyfile = _asset(deployment.CADDY_RELATIVE_PATH)

    assert _MTLS_STANZA in caddyfile, (
        "the mTLS stanza was reformatted; update _MTLS_STANZA or the block-scoping "
        "mutation tests stop removing anything and pass vacuously"
    )


@pytest.mark.parametrize(
    ("mutation", "failure_code"),
    (
        (
            lambda text: text.replace("client_auth {", "authentication_disabled {"),
            "caddy_authentication_missing",
        ),
        (
            lambda text: text.replace("alice.example.com {", "alice.example.com.attacker.invalid {"),
            "caddy_public_host_missing",
        ),
        (
            lambda text: text.replace("mode require_and_verify", "mode request"),
            "caddy_mtls_not_fail_closed",
        ),
        (
            lambda text: text.replace(
                "reverse_proxy 127.0.0.1:8000",
                "header_up X-Forwarded-For 127.0.0.1\n\t\treverse_proxy 127.0.0.1:8000",
            ),
            "caddy_forwarded_client_overridden",
        ),
        (
            lambda text: text.replace("reverse_proxy 127.0.0.1:8000", "reverse_proxy 0.0.0.0:8000"),
            "caddy_api_upstream_invalid",
        ),
        (
            lambda text: text.replace(
                "reverse_proxy 127.0.0.1:8000",
                "reverse_proxy 127.0.0.1:8000.attacker.example",
            ),
            "caddy_api_upstream_invalid",
        ),
        (
            lambda text: text.replace(
                "reverse_proxy 127.0.0.1:3000",
                "reverse_proxy 127.0.0.1:3000 attacker.example:3000",
            ),
            "caddy_web_upstream_invalid",
        ),
        (
            lambda text: text.replace("tls {", "tls internal {", 1),
            "caddy_public_ca_disabled",
        ),
        (
            lambda text: text.replace("/v0/vnext /v0/vnext/*", "/v0/*"),
            "caddy_public_api_routes_invalid",
        ),
        (
            lambda text: text.replace("/v0/vnext /v0/vnext/*", "/v0/vnext /v0/vnext/* /v1/*"),
            "caddy_public_api_routes_invalid",
        ),
        (
            lambda text: text.replace("/v0/vnext /v0/vnext/*", "/v0/vnextish*"),
            "caddy_public_api_routes_invalid",
        ),
        (
            lambda text: text.replace(
                'Strict-Transport-Security "max-age=31536000; includeSubDomains"',
                'Strict-Transport-Security "max-age=0"',
            ),
            "caddy_hsts_missing",
        ),
        (
            lambda text: text.replace("frame-ancestors 'none'", "frame-ancestors *"),
            "caddy_clickjacking_defense_missing",
        ),
        (
            lambda text: text.replace("request_body {", "request_body_disabled {"),
            "caddy_request_body_limit_missing",
        ),
        (
            lambda text: text.replace("\t\tmax_size 4MB\n", ""),
            "caddy_request_body_limit_missing",
        ),
    ),
)
def test_caddy_validation_rejects_missing_auth_xff_spoof_and_public_upstreams(
    mutation,
    failure_code: str,
) -> None:
    caddyfile = mutation(_asset(deployment.CADDY_RELATIVE_PATH))

    with pytest.raises(deployment.DeploymentContractError) as exc_info:
        deployment.validate_caddyfile(caddyfile)

    assert exc_info.value.code == failure_code


def test_workflow_uses_full_action_shas_and_no_unpinned_images() -> None:
    workflow = _asset(deployment.WORKFLOW_RELATIVE_PATH)

    pins = deployment.validate_supply_chain_pins(workflow)

    assert pins == {"actions": 5, "images": 0}
    # Derive the pin actually in the workflow rather than hardcoding a digest,
    # so this mutation keeps biting after a routine action bump instead of
    # silently becoming a no-op replace that raises nothing.
    checkout_pin = re.search(r"actions/checkout@([0-9a-f]{40})", workflow)
    assert checkout_pin is not None, "workflow no longer pins actions/checkout by digest"
    with pytest.raises(deployment.DeploymentContractError) as action_error:
        deployment.validate_supply_chain_pins(
            workflow.replace(
                f"actions/checkout@{checkout_pin.group(1)}",
                f"actions/checkout@v7 # {checkout_pin.group(1)}",
            )
        )
    assert action_error.value.code == "workflow_action_unpinned"

    workflow_with_floating_image = workflow.replace(
        "    runs-on: ubuntu-latest",
        "    runs-on: ubuntu-latest\n    container:\n      image: caddy:2.10",
    )
    with pytest.raises(deployment.DeploymentContractError) as image_error:
        deployment.validate_supply_chain_pins(workflow_with_floating_image)
    assert image_error.value.code == "workflow_image_unpinned"


@pytest.mark.parametrize(
    "unsafe_report",
    (
        {"status": "failed", "detail": "postgresql://alice:secret@db/alice"},
        {"status": "failed", "detail": "/Users/operator/private/alice.env"},
        {"status": "failed", "detail": "/etc/alicebot/runtime.env"},
        {"status": "failed", "detail": "/tmp/alice-secret"},
        {"status": "failed", "detail": "alice_sk_example"},
        {"status": "failed", "detail": "ALICEBOT_DB_BACKUP_PASSWORD"},
        {"status": "failed", "detail": "ALICEBOT_DB_DRILL_PASSWORD"},
    ),
)
def test_receipt_sanitizer_rejects_secret_and_path_markers(unsafe_report) -> None:
    with pytest.raises(deployment.DeploymentContractError):
        deployment._assert_report_safe(unsafe_report)


def test_cli_failure_receipt_uses_stable_code_without_secret_or_path(tmp_path, capsys) -> None:
    contract_root = tmp_path / "deployment-with-secret"
    _copy_contract_tree(contract_root)
    env_path = contract_root / deployment.ENV_RELATIVE_PATH
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace("${ALICEBOT_DB_APP_PASSWORD}", "literal-password"),
        encoding="utf-8",
    )
    output = tmp_path / "receipt.json"

    exit_code = deployment.main(
        [
            "--root",
            str(contract_root),
            "--environment",
            "ephemeral_ci",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_codes"] == ["database_secret_placeholder_invalid"]
    assert payload["environment"] == "ephemeral_ci"
    assert payload["cloud_provider"] == "none"
    assert payload["source_head_commit"] == _git(contract_root, "rev-parse", "HEAD")
    assert payload["source_head_tree"] == _git(contract_root, "rev-parse", "HEAD^{tree}")
    assert payload["carrier_state"] == "dirty"
    assert set(payload["validated_asset_sha256"]) == set(deployment.VALIDATED_ASSETS)
    serialized = json.dumps(payload)
    assert "literal-password" not in serialized
    assert str(tmp_path) not in serialized
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert output.stat().st_mode & 0o777 == 0o600


def test_guide_names_operational_probes_backup_restore_upgrade_and_claim_limits() -> None:
    guide = _asset(deployment.GUIDE_RELATIVE_PATH)

    deployment.validate_guide(guide)

    assert "A keyless request to the operator workspace must return 401" in guide
    assert 'ALICEBOT_AUTH_USER_ID}")" = 401' in guide
    assert "must return 403" not in guide
    normalized_guide = re.sub(r"\s+", " ", guide)
    for required in (
        "Keyless equals local-machine-owner trust",
        "`/healthz` checks PostgreSQL only",
        "alicebot --version",
        "percent-encode each database password as URL userinfo",
        "keyless request",
        "external scheduler",
        "off-host encrypted copy",
        "disposable restore",
        "no in-place schema downgrade",
        "multi-tenant isolation",
        "SLA",
        "high availability",
        "managed backup",
        "managed alert",
        "owner_real_host_deployment_receipt",
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
        "alicebot_backup",
        "alicebot_drill",
        "BYPASSRLS",
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
        f"EnvironmentFile={deployment.RECOVERY_ENV_PATH}",
        f"BindReadOnlyPaths={deployment.POSTGRES_CA_PATH}",
        "run_phase5_ops_evidence.py --backend postgres",
        "runtime DB role=alicebot_app",
        "admin DSN absent from API service environment",
        "backup DSN absent from API service environment",
        "lifecycle DSN absent from API service environment",
        "remote-v1-not-api",
        "remote-non-vnext-v0-not-api",
        "remote-vnext-lookalike-not-api",
    ):
        assert required in normalized_guide
    assert "--database-admin-url" not in guide
    assert "--database-url" not in guide
    assert "systemctl show --property Environment" not in guide
    assert '"SELECT session_user, current_user"' in guide
    assert 'session_role != "alicebot_app" or effective_role != "alicebot_app"' in guide


def test_guide_restore_filter_keeps_non_public_schema_acls() -> None:
    guide = _asset(deployment.GUIDE_RELATIVE_PATH)
    predicate = '$4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public"'

    assert predicate in guide
    assert 'test "$public_schema_acl_count" = 1' in guide
    assert "--use-list=alice.restore.list" in guide
    assert "Do not use `--no-acl`" in guide

    with pytest.raises(deployment.DeploymentContractError) as broad_acl_error:
        deployment.validate_guide(
            guide.replace("--use-list=alice.restore.list", "--no-acl", 1)
        )
    assert broad_acl_error.value.code == "deployment_guide_contract_incomplete"

    with pytest.raises(deployment.DeploymentContractError) as broad_filter_error:
        deployment.validate_guide(guide.replace(predicate, '$4 == "ACL"'))
    assert broad_filter_error.value.code == "deployment_guide_contract_incomplete"


def test_owned_deployment_contract_has_no_transient_secret_path() -> None:
    old_prefix = "/run" + "/secrets/alicebot"
    owned_paths = (
        Path(".gitignore"),
        deployment.ENV_RELATIVE_PATH,
        deployment.CADDY_RELATIVE_PATH,
        deployment.GUIDE_RELATIVE_PATH,
        Path("scripts/run_single_tenant_deployment_smoke.py"),
        Path("tests/unit/test_single_tenant_deployment.py"),
    )

    for relative_path in owned_paths:
        assert old_prefix not in _asset(relative_path)


def test_workflow_records_ephemeral_non_cloud_truth_and_uploads_only_sanitized_receipt() -> None:
    workflow = _asset(deployment.WORKFLOW_RELATIVE_PATH)

    assert "--environment ephemeral_ci" in workflow
    assert "scripts/run_single_tenant_deployment_smoke.py" in workflow
    assert "tests/unit/test_single_tenant_deployment.py" in workflow
    assert "pnpm --dir apps/web test -- lib/api.test.ts" in workflow
    assert "single-tenant-deployment-smoke.json" in workflow
    assert "if-no-files-found: error" in workflow
    assert "cloud_provider" not in workflow  # The signed JSON report owns this field.
