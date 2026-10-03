PYTHON ?= ./.venv/bin/python
ALICEBOT ?= ./.venv/bin/alicebot
PNPM ?= pnpm
WEB_DIR ?= apps/web
DIST_DIR ?= dist
REPRO_DIST_DIR ?= $(DIST_DIR)-reproducibility-check
PROJECT_VERSION = $(shell $(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
# Reload is off unless a developer opts in: APP_RELOAD=true make dev
APP_RELOAD ?= false
ALICE_WEB_HOST ?= 127.0.0.1
ALICE_WEB_PORT ?= 3000

.PHONY: setup setup-browser setup-browser-linux migrate api dev runtime web-build doctor vnext scheduler alpha-check test-web test-python check-python-coverage test-longmemeval release-static release-identity release-finalization-check release-artifacts release-semantic-attestation release-check

setup:
	@python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || \
		{ echo "ERROR: Alice requires Python 3.12+ but python3 is $$(python3 -V 2>&1 | cut -d' ' -f2)." ; \
		  echo "Point python3 at 3.12+ (e.g. 'uv python install 3.12' or your package manager) and re-run." ; exit 1; }
	@test -f .env || cp .env.example .env
	@test -f .env.lite || cp .env.lite.example .env.lite
	@test -f $(WEB_DIR)/.env.local || cp $(WEB_DIR)/.env.local.example $(WEB_DIR)/.env.local
	./scripts/validate_env.sh .env .env.lite $(WEB_DIR)/.env.local
	python3 -m venv .venv
	$(PYTHON) -m pip install -e '.[dev]'
	PNPM="$(PNPM)" WEB_DIR="$(WEB_DIR)" ./scripts/pnpm_web_install.sh
	@echo "Setup complete. Next: make setup-browser (or setup-browser-linux on Debian/Ubuntu), then make migrate && make doctor"

# Install only the Playwright-managed Chromium binary on developer machines.
# Linux CI installs its additional OS packages separately with --with-deps.
setup-browser:
	$(PNPM) --dir $(WEB_DIR) run setup:browser

# Opt-in clean Debian/Ubuntu setup. The guard prevents macOS from ever running
# Playwright's Linux system-package installer.
setup-browser-linux:
	@test "$$(uname -s)" = "Linux" || \
		{ echo "ERROR: setup-browser-linux is supported only on Linux; use make setup-browser." ; exit 1; }
	$(PNPM) --dir $(WEB_DIR) run setup:browser:linux

migrate:
	./scripts/dev_up.sh

api:
	APP_RELOAD=$(APP_RELOAD) ./scripts/api_dev.sh

doctor:
	$(ALICEBOT) vnext doctor --fix-safe --ci

dev:
	./scripts/dev_up.sh
	APP_RELOAD=$(APP_RELOAD) ./scripts/api_dev.sh & \
	api_pid=$$!; \
	$(PNPM) --dir $(WEB_DIR) dev & \
	web_pid=$$!; \
	trap 'kill $$api_pid $$web_pid 2>/dev/null || true' INT TERM EXIT; \
	wait $$api_pid $$web_pid

runtime:
	./scripts/dev_up.sh
	$(PNPM) --dir $(WEB_DIR) build
	APP_RELOAD=$(APP_RELOAD) ./scripts/api_dev.sh & \
	api_pid=$$!; \
	$(PNPM) --dir $(WEB_DIR) start --hostname $(ALICE_WEB_HOST) --port $(ALICE_WEB_PORT) & \
	web_pid=$$!; \
	trap 'kill $$api_pid $$web_pid 2>/dev/null || true' INT TERM EXIT; \
	wait $$api_pid $$web_pid

web-build:
	$(PNPM) --dir $(WEB_DIR) build

vnext:
	@echo "Start the low-CPU local runtime with: make runtime"
	@echo "Use make dev only when editing the web UI."
	@echo "Then open: http://localhost:$(ALICE_WEB_PORT)/vnext"

scheduler:
	$(ALICEBOT) vnext scheduler daemon start --foreground

alpha-check:
	$(ALICEBOT) vnext alpha check

PYTHON_COVERAGE_JSON ?= /tmp/alicebot-python-coverage.json
PYTHON_MAIN_COVERAGE_MIN ?= 45
PYTHON_API_COVERAGE_MIN ?= $(PYTHON_MAIN_COVERAGE_MIN)

check-python-coverage:
	$(PYTHON) scripts/check_python_coverage.py --coverage-json $(PYTHON_COVERAGE_JSON) --path apps/api/src/alicebot_api/main.py --path apps/api/src/alicebot_api/routers/_api_shared.py --path apps/api/src/alicebot_api/routers/_vnext_automation.py --path apps/api/src/alicebot_api/routers/_vnext_embeddings.py --path apps/api/src/alicebot_api/routers/_vnext_shared.py --path apps/api/src/alicebot_api/routers/continuity.py --path apps/api/src/alicebot_api/routers/legacy_gated.py --path apps/api/src/alicebot_api/routers/memories_legacy.py --path apps/api/src/alicebot_api/routers/providers.py --path apps/api/src/alicebot_api/routers/vnext_memories.py --path apps/api/src/alicebot_api/routers/vnext_projects.py --path apps/api/src/alicebot_api/routers/vnext_retrieval.py --path apps/api/src/alicebot_api/routers/vnext_review.py --path apps/api/src/alicebot_api/routers/workspaces.py --min-percent $(PYTHON_API_COVERAGE_MIN)

test-python:
	$(PYTHON) -m pytest tests/unit -q --cov=alicebot_api --cov-report=term --cov-report=json:$(PYTHON_COVERAGE_JSON) --cov-fail-under=50
	$(MAKE) check-python-coverage PYTHON_COVERAGE_JSON=$(PYTHON_COVERAGE_JSON) PYTHON_API_COVERAGE_MIN=$(PYTHON_API_COVERAGE_MIN)
	ALICE_LEGACY_SURFACES=1 $(PYTHON) -m pytest tests/integration -q

test-web: setup-browser
	$(PNPM) --dir $(WEB_DIR) test
	$(PNPM) --dir $(WEB_DIR) test:coverage:core
	$(PNPM) --dir $(WEB_DIR) test:coverage:vnext
	$(PNPM) --dir $(WEB_DIR) typecheck
	$(PNPM) --dir $(WEB_DIR) lint
	$(PNPM) --dir $(WEB_DIR) build
	$(PNPM) --dir $(WEB_DIR) test:budget
	$(PNPM) --dir $(WEB_DIR) test:browser

test-longmemeval:
	$(PYTHON) -m pytest eval/longmemeval -q
	$(PYTHON) scripts/check_longmemeval_evidence.py

release-static:
	$(PYTHON) scripts/check_control_doc_truth.py
	$(PYTHON) scripts/release_check.py
	$(PYTHON) -m ruff check apps/api/src/alicebot_api scripts tests eval/longmemeval
	$(PYTHON) -m mypy --ignore-missing-imports apps/api/src/alicebot_api scripts/alice_bench.py scripts/alice_bench_gates.py scripts/alice_bench_audit.py scripts/release_check.py scripts/test_distribution_artifact.py scripts/normalize_sdist.py scripts/render_release_body.py scripts/decode_github_release_body.py scripts/prepare_mainprotect_update.py scripts/check_python_coverage.py scripts/combine_python_coverage.py scripts/check_control_doc_truth.py scripts/check_github_release_checks.py scripts/check_release_controls_attestation.py scripts/run_phase5_ops_evidence.py scripts/_phase5_ops_seed.py scripts/run_single_tenant_deployment_smoke.py

release-identity:
	git fetch --no-tags origin main
	$(PYTHON) scripts/release_check.py --require-clean --require-main-head

release-finalization-check: release-identity
	$(PYTHON) scripts/release_check.py --require-clean --require-main-head --require-finalized-release-docs

release-artifacts:
	SOURCE_DATE_EPOCH="$$(git show -s --format=%ct HEAD)" $(PYTHON) -m build --outdir $(DIST_DIR)
	$(PYTHON) scripts/normalize_sdist.py --source-date-epoch "$$(git show -s --format=%ct HEAD)" $(DIST_DIR)/*.tar.gz
	SOURCE_DATE_EPOCH="$$(git show -s --format=%ct HEAD)" $(PYTHON) -m build --outdir $(REPRO_DIST_DIR)
	$(PYTHON) scripts/normalize_sdist.py --source-date-epoch "$$(git show -s --format=%ct HEAD)" $(REPRO_DIST_DIR)/*.tar.gz
	@for artifact in $(DIST_DIR)/*.whl $(DIST_DIR)/*.tar.gz; do \
		cmp "$$artifact" "$(REPRO_DIST_DIR)/$$(basename "$$artifact")"; \
	done
	$(PYTHON) -m twine check $(DIST_DIR)/*
	$(PYTHON) scripts/release_check.py --dist-dir $(DIST_DIR) --write-checksums
	$(PYTHON) scripts/test_distribution_artifact.py $(DIST_DIR)/*.whl $(DIST_DIR)/*.tar.gz --expected-version $(PROJECT_VERSION)

# Canonical pre-publication gate. PostgreSQL must be available with the same
# role-separated environment used by tests/integration.
#
# The release eval runs with --release-gate: a run that never exercises the
# vector suite reports pass_fts_only, the aggregate fails because semantic
# targets were not measured, and the CLI exits non-zero (fail closed), so the
# gate cannot go green without measuring semantic/paraphrase retrieval quality.
# Point ALICEBOT_EVAL_DATABASE_URL at a pgvector database and set the
# ALICE_EMBEDDINGS_* provider variables so the vector stage actually runs; the
# default in-memory SQLite URL is a fail-closed smoke only.
ALICEBOT_EVAL_DATABASE_URL ?= sqlite:///:memory:
SEMANTIC_EVAL_ARTIFACT_DIR ?= artifacts/release
SEMANTIC_EVAL_REPORT ?= $(SEMANTIC_EVAL_ARTIFACT_DIR)/semantic-eval-report.json
SEMANTIC_EVAL_ATTESTATION ?= $(SEMANTIC_EVAL_ARTIFACT_DIR)/semantic-eval-attestation.json

release-semantic-attestation:
	mkdir -p $(SEMANTIC_EVAL_ARTIFACT_DIR)
	ALICEBOT_EVAL_DATABASE_URL=$(ALICEBOT_EVAL_DATABASE_URL) $(PYTHON) -m alicebot_api eval run --suite all --release-gate --report-path $(SEMANTIC_EVAL_REPORT)
	$(PYTHON) scripts/release_check.py --expected-sha "$$(git rev-parse HEAD)" --semantic-eval-report $(SEMANTIC_EVAL_REPORT) --write-semantic-eval-attestation $(SEMANTIC_EVAL_ATTESTATION)

release-check: release-identity release-static test-python test-longmemeval test-web release-artifacts release-semantic-attestation
