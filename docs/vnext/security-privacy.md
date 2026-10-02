# vNext Security and Privacy

Alice vNext is built around private, correctable, inspectable continuity. This document describes the public-preview security posture.

## Defaults

- Local-first operation is the default.
- Source evidence is archived with content hashes, connector metadata, domain, sensitivity, and timestamps.
- Sensitive connector defaults are conservative: most new sources default to `private` or stricter.
- Generated artifacts inherit sensitivity from selected inputs.
- Prompt-injection content from sources is data, not policy.
- Model-backed workflows default private, confidential, highly sensitive, sacred, and regulated content to local-only or disabled routing unless an explicit policy configuration allows otherwise.
- Model-backed artifacts remain review-only and do not auto-promote trusted memory.

## Connector Safety

The live capture connector slice is local-first and intentionally narrow. Alice
can scan or poll configured local folders, accept browser clipper captures
through the local API, and ingest generic agent-output text. It may ingest
screenshot text or voice transcripts produced by external tools; it does not
poll channels, perform managed OAuth, package browser extension actions, execute
OCR or transcription models, perform hosted connector polling, or sync to a
cloud service.

Connector invariants:

- raw payload or extracted evidence is preserved in source metadata
- default domain and sensitivity are stored with the source
- connector defaults are stored in dedicated connector settings rows, not only in the event log
- sync cursors and counters are stored in dedicated connector state rows
- all connector text is marked as untrusted source material
- sync cursors prevent duplicate ingestion
- local folder scanning is constrained to allowed local roots; by default those are the user home, repo working directory, and system temp directory, with `ALICE_VNEXT_LOCAL_FOLDER_ROOTS` available for operator override
- From v0.20.0, each local folder file is read through a descriptor opened beneath the watched root without following a link, so the constraint to allowed local roots also holds for the read itself and not only for the check made before it; a file or directory swapped for a link during a sync is skipped, and a hard link planted inside the watched folder to a file elsewhere is still read; the scan also reads at most 2 MiB of a file, stops at 10,000 files or 64 MiB in all, and reports the files it skipped and whether a limit stopped it
- cursor advancement stops when a failed item could otherwise be skipped
- failed items are logged and not imported as broken memories
- connector payload text cannot trigger tool writes
- live connector captures produce candidate memory/review artifacts only; they do not auto-promote trusted memory

## Secrets

Do not commit secrets, tokens, real personal exports, private chats, production credentials, or unredacted customer data.

Connector secrets are referenced, not returned. The browser clipper and other
surviving local connectors may use `secret_ref` values. Local secret values are
stored through the secret-provider abstraction, with an encrypted local file
fallback for alpha use and an environment-reference provider for operators who
prefer environment variables.

Secret rules:

- API, CLI, UI, event logs, source metadata, artifact metadata, and health responses must expose only the reference or configured/not-configured status.
- Redaction applies before raw connector payloads are persisted.
- Trusted API clients may use a configured browser-clipper capture token, which is redacted from source/event evidence. The bookmarklet never receives it: the trusted Alice console issues a short-lived, origin-bound, one-time capability whose digest is stored until atomic redemption.
- The future OS keychain or hosted secret-provider implementation should satisfy the same interface without changing connector behavior.

Allowed public demo material:

- synthetic people and projects
- fake URLs under `example.test` or `example.com`
- synthetic source text
- redacted or generated screenshots
- fixture payloads with no real account identifiers

Disallowed public demo material:

- real OAuth tokens
- real health, legal, financial, family, or customer records
- real browser history
- real voice transcripts
- real email/calendar payloads unless fully synthetic

## Security Review Checklist

- Run unit tests, web tests, build, control-doc truth, and `git diff --check`.
- Run `alicebot eval run --suite all`; the `retrieval_quality` suite executes the production retrieval pipeline against a live database (`ALICEBOT_EVAL_DATABASE_URL`) and is reported as skipped without one.
- Inspect new fixtures for secrets and personal data.
- Inspect new connector write paths for raw-evidence preservation and failure isolation.
- Confirm no generated artifacts are auto-promoted to trusted memory.
- Confirm model-backed artifacts include source references, prompt/context hashes, provider metadata, and source-grounded fact/inference/recommendation/uncertainty sections.
- Confirm `alicebot vnext smoke model-backed` passes for at least one scheduled Postgres-backed model-backed workflow.
- Confirm `alicebot vnext smoke connector-hardening`, `alicebot vnext smoke secret-redaction`, and `alicebot vnext smoke dogfood-doctor` pass against the local Postgres database.
- Confirm docs do not claim live connector behavior that is not shipped.

## Reporting

For a vulnerability in the current public release, use the process in the repository `SECURITY.md`. For vNext preview issues, include the connector name, payload type, reproduction command, and whether the issue affects source archive, memory promotion, retrieval, artifact generation, or external tool writes.
