# TenderGuard

TenderGuard is a fail-closed control plane for industrial tender costing. It is
designed for calculations that influence bid/no-bid decisions, bid price,
profit, procurement budgets, and contractual obligations.

It is deliberately **not** an autonomous price generator. The system assembles
evidence, detects conflicts and omissions, runs deterministic calculations,
performs an independent recalculation, enforces four-eyes approval, and blocks
release when the evidence is insufficient.

## Safety contract

`APPROVED_FOR_BID` is impossible unless all mandatory release gates pass:

- the current tender-document revision is fixed and complete;
- critical quantities are verified and conflicts are resolved;
- every cost has a source or an explicit approved assumption;
- technical analogues and price commercial bases are verified;
- the independent calculation matches within the approved rounding tolerance;
- methodology, catalog, rules, models, and thresholds have approved versions;
- required expert approvals satisfy segregation of duties;
- a content-addressed calculation snapshot is fixed.

The normative estimating adapter fails closed when no approved external
estimating engine or formally approved complete normative basis is configured.

## Architecture

The target architecture is a modular monolith for transactional consistency,
plus isolated workers and external adapters:

- FastAPI application/API;
- PostgreSQL as the system of record;
- S3-compatible immutable object storage for original documents and snapshots;
- OIDC authentication and role/attribute-based authorization;
- deterministic domain engines for intake, reconciliation, BoQ, scope,
  quantity, nomenclature, pricing, contract risk, calculation, and release;
- append-only tamper-evident audit events;
- independent calculation validator that does not consume main-engine totals;
- outbox-driven workers for OCR, visual extraction, connectors, and exports.

The Dockerfile has separate `api` and `document-worker` targets. The API image
does not install PDF, Excel, or image parser libraries; only the worker target
does.

See [architecture](docs/architecture.md), [safety case](docs/safety-case.md),
[requirements traceability](docs/requirements-traceability.md), and the
[signed export package specification](docs/signed-export-package.md).

## Implemented application core

The repository currently connects and integration-tests:

- document intake, revision-set confirmation, qualified extraction evidence,
  reconciliation, review-task-backed conflict resolution, and source lineage;
- owner-managed versioned project ACLs with action-specific roles,
  same-organisation information barriers, audited revocation, and qualified
  machine-service capabilities;
- streamed, separately stored quarantine uploads, qualification-bound malware
  results, durable outbox leases/retries/dead letters, controlled replay, and a
  worker-only bounded document/archive parser entry point with short database
  transactions;
- fail-closed invalidation of derived data when a newer current document
  revision appears;
- revisioned project-passport facts, BoQ lines, quantity verification, planned
  cost components, and attested scope-completeness evaluations;
- deterministic critical-attribute nomenclature matching, governed analogues,
  quote normalization, source triangulation, price decisions, and RFQ state;
- revisioned contract terms and approved contract-cost impacts;
- revisioned risks, a version-bound deterministic reserve calculation, and
  binding of the reserve to an explicit BoQ cost component;
- deterministic primary calculation from atomic inputs, a separate
  recalculation path, immutable snapshots, hard stops, four-eyes approvals,
  recursive source-to-document lineage, and integrity checks against the
  current document set and controlled-version bindings;
- approved-policy scenario calculation from a fixed snapshot, including a
  separate independent recalculation for each scenario;
- deterministic Ed25519-signed estimate/audit export packages containing the
  fixed snapshot, recursive lineage, controlled versions, approvals, workflow,
  release decision, and verified project audit chain;
- versioned audit HMAC keys, fail-closed legacy migration, content-addressed
  global checkpoints, four-eyes external Ed25519 receipts, and readiness that
  re-verifies current history plus the live evidence-bucket WORM policy;
- revisioned verified actuals, forecast-to-actual variance classification, and
  methodology-owner approval before facts become calibration examples.

These are application workflows, not a claim that the complete target system
or its external operational environment has been delivered.

## Local development

```powershell
uv sync --extra dev --no-editable --reinstall-package tenderguard
docker compose up -d postgres minio
uv run alembic upgrade head
uv run uvicorn tenderguard.api.main:app --reload
uv run pytest
```

On Windows, `--no-editable` also avoids a Python 3.11 `.pth` locale problem
when the checkout path contains Cyrillic characters. Keep
`--reinstall-package tenderguard` after source changes so tests do not import a
stale non-editable wheel.

Development authentication is disabled by default. Set
`ALLOW_INSECURE_DEV_AUTH=true` only on an isolated workstation; production
startup rejects it.

## Production status

This repository contains a substantial fail-closed application core, but it is
not the complete target system and is not proof of production acceptance. Live
bid release remains blocked until the organisation supplies and qualifies the
malware scanner and production scheduler/isolated worker runtime, normative
engine, OCR/visual
extraction and market/RFQ adapters, approved
methodology and financial thresholds, production identity and infrastructure,
an approved external audit-anchor provider and verified WORM policy, enterprise
integrations, historical validation set, operating procedures,
backup/disaster-recovery evidence, trained users, and named process owners.

Detailed logistics/mobilisation planning, contract cash-flow modelling,
external export delivery/connectors, and a complete operator read/search UI
remain to be completed. External verification also requires an approved
out-of-band signing-key registry. A successful local test run cannot
substitute for the required historical, blind-comparison, parallel-operation,
security, load, and recovery evidence.

The current gate-by-gate status is maintained in the
[production readiness register](docs/production-readiness-register.md).
Open security findings are in
[the security review](security_best_practices_report.md).
The untrusted-file boundary is specified in the
[quarantined intake contract](docs/quarantined-intake.md).
Audit checkpoint, anchoring, WORM, and key-rotation controls are specified in
[the audit-integrity runbook](docs/audit-integrity.md).
