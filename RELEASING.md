# Releasing Alice



`v0.16.0` is the latest published release and remains the checksum/install
baseline.
Preparing candidate documents does not authorize a tag, PyPI upload, or GitHub
Release.


## One Release Identity

`pyproject.toml` is the canonical package-version source. The API and Python
entrypoints read the installed distribution metadata. The release check also
requires the private web package and active control documents to agree with
the candidate version.

A stable publication uses all of the following on the same commit:

- package version `X.Y.Z`;
- annotated Git tag `vX.Y.Z`;
- non-draft, non-prerelease GitHub release `vX.Y.Z`;
- wheel and sdist metadata version `X.Y.Z`.

## Manual Repository Prerequisites

Before publishing, configure these controls in GitHub and verify them by
readback:

1. Protect the `pypi` environment with required reviewers and a deployment
   branch policy limited to `main` or protected branches.
2. Keep the required `main` status checks strict — tests, security scans, and
   the protected-path guardrail — so no release commit reaches `main` without
   passing them. This is a single-maintainer repository: the maintainer merges
   release pull requests by administrative merge after those checks pass, which
   is the audited release route. The controls below gate *what publishes*, not
   *who approves the merge*. Keep `main` branch protection enabled as well.
   Before every release, run the read-only ruleset drift check so a renamed CI
   job cannot silently remove a current release-critical context. Additional
   organization-level required checks are allowed:

   ```bash
   GITHUB_TOKEN="$(gh auth token)" python scripts/check_github_release_checks.py \
     --repo OWNER/REPOSITORY --sha RELEASE_SHA --check-rulesets
   ```

   If that readback reports drift in `MainProtect`, an authorized repository
   administrator can prepare and inspect an update that preserves every
   current condition, bypass actor, and non-status rule while replacing only
   the required status-check list with the repository constant:

   ```bash
   REPOSITORY="OWNER/REPOSITORY"
   RULESET_ID="$(gh api "repos/${REPOSITORY}/rulesets" \
     --jq '.[] | select(.name == "MainProtect" and .target == "branch") | .id')"
   test -n "$RULESET_ID"
   gh api "repos/${REPOSITORY}/rulesets/${RULESET_ID}" \
     > /tmp/alice-mainprotect-current.json
   python -m scripts.prepare_mainprotect_update \
     --input /tmp/alice-mainprotect-current.json \
     --output /tmp/alice-mainprotect-update.json
   python -m json.tool /tmp/alice-mainprotect-update.json
   gh api --method PUT "repos/${REPOSITORY}/rulesets/${RULESET_ID}" \
     --input /tmp/alice-mainprotect-update.json
   GITHUB_TOKEN="$(gh auth token)" python scripts/check_github_release_checks.py \
     --repo "$REPOSITORY" --sha RELEASE_SHA --check-rulesets
   ```

   The `PUT` is the state-changing step. Review the generated JSON and obtain
   repository-administrator authorization before running it.
3. Protect stable `v*` tags from mutation or deletion.
4. Enable immutable GitHub releases for the repository.
5. Configure PyPI trusted publishing for this repository, the pinned publish
   workflow, and the protected `pypi` environment. Do not store or use a
   long-lived PyPI upload credential.
6. If a provider credential has ever been exposed in a local file or terminal
   transcript, revoke and replace it at the provider. Do not paste or print the
   old or replacement value while checking that local `.env` files remain
   ignored by Git.
7. Immediately before publishing, after the exact stable tag exists but before
   any stable GitHub Release exists, verify
   items 1-6 by readback for that tag and commit. Then set the repository
   variable `ALICE_RELEASE_CONTROLS_ATTESTATION` to the credentialless JSON
   contract below. Publication fails closed when the variable is missing,
   malformed, expired, stale, names another repository, tag, or SHA, omits a
   control, adds an unknown claim, or contains any control value other than
   JSON `true`. Remove it immediately if the verified state changes.
8. Protect a separate `semantic-release` environment. Configure
   `ALICE_RELEASE_EMBEDDINGS_BASE_URL` as an environment secret,
   `ALICE_RELEASE_EMBEDDINGS_MODEL` as an environment variable, and (when the
   provider requires it) `ALICE_RELEASE_EMBEDDINGS_API_KEY` as an environment
   secret. Dispatch `semantic-release-gate.yml` on the exact SHA you will
   tag. That SHA is `origin/main` after the release pull request has merged,
   not the release-branch tip. The workflow uploads a credential-free report
   and attestation named with that SHA. Publication fails closed unless it
   can resolve a successful gate run, download that exact artifact, and
   validate its source SHA, report digest, passing suite set, Postgres
   backend, and positive signed-vector candidate count.

Use this exact closed shape for the repository variable, replacing every
placeholder with the release-specific value. `verified_at` must not be in the
future or more than 24 hours old. `expires_at` must be later than `verified_at`
and the current time, with a validity window no longer than 24 hours.

```json
{
  "schema_version": "alice_release_controls_attestation_v1",
  "repository": "OWNER/REPOSITORY",
  "release_sha": "40_lowercase_hex_characters",
  "release_tag": "vX.Y.Z",
  "verified_at": "YYYY-MM-DDTHH:MM:SSZ",
  "expires_at": "YYYY-MM-DDTHH:MM:SSZ",
  "controls": {
    "pypi_environment_protected": true,
    "pypi_required_reviewers": true,
    "pypi_deployment_policy_restricted": true,
    "main_branch_protected": true,
    "main_required_checks_strict": true,
    "stable_tags_protected": true,
    "immutable_releases_enabled": true,
    "trusted_publishing_enabled": true,
    "no_long_lived_pypi_credentials": true,
    "exposed_provider_credentials_revoked": true,
    "exact_sha_release_checks_required": true
  }
}
```

This variable contains claims only; credentials, endpoints, and tokens never
belong in it. The repository-control attestation and exact-SHA semantic
attestation are independent mandatory gates. Neither is evidence for, or a
substitute for, the other.

The workflow cannot create these repository settings itself. An unprotected
PyPI environment is a release blocker even when the YAML is correct.

## Candidate Gate

Run from a clean checkout whose exact SHA is the intended `main` head:

```bash
set -eu

make setup
make setup-browser
make migrate

release_run_root="$(mktemp -d /tmp/alice-release-check.XXXXXX)"
dist_dir="$release_run_root/dist"
repro_dist_dir="$release_run_root/reproducibility-check"
semantic_dir="$release_run_root/semantic"

mkdir "$dist_dir" "$repro_dist_dir" "$semantic_dir"

for directory in "$dist_dir" "$repro_dist_dir" "$semantic_dir"; do
  test -z "$(find "$directory" -mindepth 1 -maxdepth 1 -print -quit)"
done

make release-check \
  DIST_DIR="$dist_dir" \
  REPRO_DIST_DIR="$repro_dist_dir" \
  PYTHON_COVERAGE_JSON="$release_run_root/python-coverage.json" \
  SEMANTIC_EVAL_ARTIFACT_DIR="$semantic_dir" \
  SEMANTIC_EVAL_REPORT="$semantic_dir/semantic-eval-report.json" \
  SEMANTIC_EVAL_ATTESTATION="$semantic_dir/semantic-eval-attestation.json"

printf 'Release evidence retained under %s\n' "$release_run_root"
```

Run the gate without `-j` and with the required role-separated PostgreSQL and
embedding-provider environment already configured. Pass both distribution
directories explicitly: `release-artifacts`, which is already part of
`release-check`, performs no cleanup or emptiness check. It writes the primary
wheel, sdist, and checksum manifest to the brand-new empty `DIST_DIR` and the
comparison build to the brand-new empty `REPRO_DIST_DIR`. Retain the temporary
root as release evidence; never point either variable at user-owned artifact
directories and do not run `release-artifacts` separately first.

`make setup-browser` is an idempotent local prerequisite that installs the
Playwright-managed Chromium binary. It intentionally does not pass
`--with-deps`: that option invokes Linux system-package installation and is not
appropriate on macOS. `make test-web` also declares this prerequisite so a
direct local web-gate run cannot skip it. On a clean Debian or Ubuntu release
host, substitute the guarded `make setup-browser-linux` target; it installs
Chromium plus the required Linux runner packages and refuses to run on macOS.
The Linux CI job installs the Playwright Chromium binary without
`--with-deps`. That flag shells out to apt-get, which has hung until
the job timeout on ubuntu-24.04 runners after the dpkg lock was already
free.

`make migrate` must target a disposable release-candidate database or a live
database that has a current, restore-tested backup. PostgreSQL verification
uses separate admin and application URLs: the admin role installs `pgvector`,
creates/grants the application role, and runs Alembic; runtime and isolation
tests use the non-superuser application role. Never point this gate at an
unbacked production database.

For an upgrade rehearsal from a database older than migration `0089`, run the
[migration `0087`/`0089` duplicate preflight](docs/alpha/headless-ubuntu-install.md#migration-00870089-duplicate-preflight)
against a restored representative backup before treating a clean-database gate
as upgrade evidence. Those migrations can remove an invalid concurrent-index
catalog entry and retry, but they deliberately cannot choose which domain row
should survive a real duplicate idempotency key.

The gate performs correctness-only Python linting, normal cross-module mypy
over the complete first-party production/release-tool surface, unit coverage,
PostgreSQL integration tests, every model-free LongMemEval test, the checked-in
compact dataset-manifest/slice consistency contract, and the offline evidence
replay. Web gates include units, core plus vNext per-file coverage,
TypeScript, lint, the production build, navigation/axe/outage browser
tests, and bundle budgets. It also builds both distributions, runs Twine, and
tests the installed wheel/sdist across all four public entrypoints. It first
fetches `origin/main`, and writes `$DIST_DIR/SHA256SUMS` only after both
artifacts pass.

Web dependency auditing deliberately remains on
`apps/web/scripts/npm-advisory-audit.mjs` while the reproducible web toolchain
is pinned to Node 20 and pnpm 10.23.0. pnpm 11 now uses npm's bulk advisory
endpoint, as recorded in the [pnpm 11 audit migration](https://github.com/orgs/pnpm/discussions/11377),
but upgrading the package-manager major is a separate compatibility carrier.
The repository wrapper already calls that bulk endpoint directly and fails
closed when dependency enumeration, the endpoint, or response parsing fails;
do not replace it with `pnpm audit` until that toolchain upgrade passes the
complete web matrix and bundle budgets.

The release regression surface also treats every advertised MCP JSON Schema
keyword as an executable pre-handler contract. RFC 3339 full dates must be
both syntactically and calendrically valid, unsupported advertised formats
fail closed, and numeric confidence fields reject booleans and non-finite
values as well as values outside their declared 0..1 interval. Durable SQLite
and PostgreSQL rollback tests prove representative invalid corrections do not
partially mutate review state.

The gate also runs the canonical retrieval-quality eval with `--release-gate`.
That flag fails closed: a run that never exercises the vector stage reports
`pass_fts_only` and exits non-zero, so the gate cannot go green without
measuring semantic/paraphrase retrieval quality. Point
`ALICEBOT_EVAL_DATABASE_URL` at a `pgvector` database and set the
`ALICE_EMBEDDINGS_*` provider variables before running `make release-check`;
the default in-memory SQLite URL exists only as a fail-closed smoke. The report
must prove that production-compatible signed vectors produced a nonzero vector
candidate count; merely attempting a vector stage is not evidence. CI runs the
no-provider path on purpose and asserts it fails closed — ordinary CI green is
not the semantic release gate.

The configured semantic run writes an attested report containing the exact Git
SHA, suite configuration, and a structured `embedding_signature` copied from
the signed vector writes. That identity contains its schema/signature versions,
the non-secret provider and model labels plus their SHA-256 fingerprints, and
the existing 16-hex endpoint fingerprint; it never contains a raw endpoint URL
or API key. The report digest binds this identity, and the attestation must copy
it exactly. Validation also derives and reconciles the canonical suite order
and titles, nonempty case lists with all 78 canonical identities, exact
generator-owned query/target linkage, acceptance targets and target-check
keys, known corpus digests, and Postgres backend evidence for every suite.
Suite and case metrics, evidence, retrieval subsets/latencies/seeding, graph
ranks, and control objects are recursively closed and type checked; numeric
values must be finite, non-boolean, and in their declared ranges. Rank-derived
case metrics and suite/subset aggregates must reconcile with the reported
evidence. Executed/skipped counts, pass/fail counts, pass rate, and report/
summary status are derived and reconciled as well. Unknown fields and
recursively detected credential-bearing keys or values are rejected in both
report and attestation. A provider-free release-gate run fails for missing
configured semantic evidence, not because a validator/provider mismatch
fabricates a digest failure.
The canonical `report_digest` is a `sha256:<hex>` digest over every semantic
report field except its wall-clock timestamp and the digest itself. The
attestation's separate bare `report_sha256` is the byte-level transport hash of
the report file. The publish workflow accepts only a verified report artifact
whose SHA equals the release tag commit. Credentials remain operator-supplied
and are never stored in repository files or ordinary CI variables. A
deterministic test provider is valid for regression tests, not for a public
release attestation.

CI must pass on the exact candidate SHA. Do not treat a green parent commit,
branch name, or locally rebuilt artifact as equivalent evidence.

## Semantic eval attestation: one dispatch on the SHA you will tag

`publish-pypi.yml` requires the check `Semantic eval attestation (exact SHA)`
on the release commit. That check is not a pull-request check.
`scripts/check_github_release_checks.py` records that it is dispatched
against the accepted exact SHA after merge.

This repository merges with a merge commit. A gate run on the
release-branch tip does not attach to the tagged SHA, so it cannot
satisfy publish.

Dispatch `semantic-release-gate.yml` once from `main` after the release
pull request is merged, on that merge SHA, then create the annotated tag
on the same SHA. A second dispatch from the tag is only required if the
first run was not already on that SHA.

A rehearsal dispatch from the release branch, before merge, is useful
when the workflow file itself changed. That is the `v0.15.0` failure:
the workflow at the tag had never executed. It is not a publish input.

## Tag And Publish

Before the finalization commit, move the candidate entries out of `Unreleased`
under a dated `## vX.Y.Z — YYYY-MM-DD` heading. Change the release-note title
from `Release Candidate Notes` to `Release Notes`, replace the candidate-status
paragraph, and remove `not published yet`. This finalizes the release text
without claiming that publication has already happened.

The release notes must carry exactly one machine-readable state comment on
physical line 2, immediately under the exact title. It must not appear inside
a code fence or anywhere else in the release-notes file:

```html
<!-- alice-release-state: {"schema_version":"alice_release_document_state_v1","version":"X.Y.Z","publication_status":"pending","checksums_status":"pending"} -->
```

The finalization check reads this structured state, not prose keywords. Keep it
`pending` on the tag. The evidence-backed post-publication commit changes the
two statuses to `published` and `recorded` after the checksums file exists.

Only after that finalization commit is merged and the prerequisites above are
verified:

1. Confirm the checkout is the exact remote `main` head and the worktree is
   clean.
2. Run `make release-finalization-check`. It rejects stale changelog or
   release-note candidate state.
3. Create an annotated `vX.Y.Z` tag on that SHA. Lightweight tags are rejected.
4. Run `python scripts/release_check.py --tag vX.Y.Z --expected-sha SHA
   --require-main-head --require-clean`; the publish workflow repeats the same
   identity check from the exact tag dispatch.
5. Read back the external controls again and set the release-specific
   `ALICE_RELEASE_CONTROLS_ATTESTATION` for that repository, tag, and SHA with
   fresh `verified_at` and `expires_at` values.
6. Confirm the protected semantic gate succeeded on that exact SHA and inspect
   its credential-free report and attestation artifact. If the only green
   gate run is on a different SHA (the release-branch tip, or a parent),
   dispatch again on this SHA. Do not publish on a parent-SHA attestation.
7. Confirm no stable GitHub Release exists for the tag. Any existing release
   must be the recoverable draft left by an earlier attempt. Then dispatch the
   transactional workflow from the tag itself:

   ```bash
   gh workflow run publish-pypi.yml --ref vX.Y.Z \
     -f release_tag=vX.Y.Z -f publication_mode=publish
   ```

   Do not create the GitHub Release manually. The workflow stages the verified
   draft before PyPI and makes it stable only after PyPI accepts the same bytes.

The `Publish to PyPI` workflow then:

- rejects a mismatched dispatch/tag identity or an already-existing stable
  GitHub Release; an existing matching draft is refreshed and reverified;
- rejects a missing, invalid, stale, wrong-repository, wrong-tag, wrong-SHA, or
  incomplete repository-control attestation;
- reads the live active branch rulesets and rejects a ruleset that omits any
  current release-critical workflow context, while allowing additional policy
  checks;
- independently rejects missing exact-SHA semantic report/attestation evidence
  or release documents whose structured publication/checksum state is
  inconsistent;
- rejects a lightweight tag;
- requires the tag commit to equal the exact `origin/main` head;
- rejects a version already present on PyPI;
- builds the wheel and sdist under pinned build backends and a commit-derived
  `SOURCE_DATE_EPOCH`, then rejects a byte-different reproducibility rebuild;
- tests those exact bytes and records their SHA-256 digests;
- stages a draft GitHub Release containing those exact verified files, the
  checksum manifest, and a body rendered only from structured identity fields;
- reads the draft assets back and byte-compares them before any upload to
  PyPI;
- publishes the downloaded, checksum-verified artifacts through the protected
  `pypi` environment; and
- only after the PyPI job succeeds, verifies the staged files against PyPI's
  recorded SHA-256 digests and makes that same draft non-draft and immutable.

### Recovering finalization after PyPI succeeds

The workflow deliberately stages and verifies a draft before crossing the
PyPI boundary. If PyPI succeeds but GitHub finalization or readback fails,
first rerun only the failed `finalize-github-release` job in the original run.
That job downloads the already-staged draft assets, checks `SHA256SUMS`, and
requires every wheel/sdist filename and digest to exactly match PyPI before it
can remove draft status.

If the original run can no longer be retried, dispatch the explicit recovery
mode from the same annotated tag:

```bash
gh workflow run publish-pypi.yml --ref vX.Y.Z \
  -f release_tag=vX.Y.Z -f publication_mode=finalize-existing-draft
```

Recovery does not rebuild, upload, or select files. It requires the existing
draft for the exact tag, downloads its attached artifacts, verifies their
manifest, compares their SHA-256 digests with PyPI, regenerates the neutral body
from the tag/SHA/checksum fields, and finalizes only that draft. A missing
draft, changed file set, digest mismatch, non-annotated tag, or wrong SHA fails
closed. If finalization succeeded but the runner died before readback, the same
mode accepts the already-stable release only after re-verifying the exact tag,
body, three-asset set, checksum manifest, and PyPI digests. Do not run normal
publish mode after PyPI already contains the version.

PyPI uploads wheel and sdist as separate requests. If PyPI accepted only one
file before an upload failure, use the protected resume mode:

```bash
gh workflow run publish-pypi.yml --ref vX.Y.Z \
  -f release_tag=vX.Y.Z -f publication_mode=resume-pypi-and-finalize
```

That mode requires the exact staged draft, enforces an asset set containing
only one wheel, one sdist, and `SHA256SUMS`, and proves every file already on
PyPI is a byte-identical subset of the staged distributions. The protected
`pypi` job then skips the verified existing file and uploads only the missing
file. Finalization still requires the complete PyPI file set and digests to
match the staged draft. An unexpected filename or changed digest fails closed.

## After Publication

Verify the GitHub Release, a clean install from PyPI, and the published file
hashes against PyPI's integrity attestations (which bind each file to this
repository, the tag, the workflow, the exact commit, and the `pypi`
environment). The transactional workflow attaches the verified files before
the release becomes immutable. The durable checksum record also lives in the
repository: record the published wheel and sdist SHA-256 digests in
`docs/release/vX.Y.Z-checksums.txt` as part of the post-publication commit,
which also updates remaining active control-document status language to say
the version is published. That truth change is a separate, evidence-backed
commit; the tagged changelog and release notes were already finalized before
the tag and must not have claimed publication early.

Verify checksum files on Linux with `sha256sum -c SHA256SUMS` and on stock
macOS with `shasum -a 256 -c SHA256SUMS`.

`v0.16.0` is the latest published release and remains the install, checksum,
and baseline reference.

`v0.17.0` is the current release candidate. It is not published.
