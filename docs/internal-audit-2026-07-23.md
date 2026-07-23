# Internal audit — 2026-07-23

This is a repository audit, not production acceptance evidence.

## Product audit

Resolved during the audit:

- a newer tender revision now blocks the project and invalidates derived
  current records instead of leaving the old BoQ reusable;
- BoQ lines now have stable business keys, supersession links, and one current
  revision;
- evidence conflicts now create mandatory review tasks and have an auditable
  resolution path;
- controlled scenario definitions now execute against fixed snapshots and
  receive an independent recalculation.

Open product blockers:

- no qualified OCR/visual, normative, market/RFQ, ERP/DMS/BI, or export
  connectors are supplied;
- detailed logistics/mobilisation planning and contract cash-flow calculation
  remain generic cost components rather than operational planning services;
- no signed estimate/audit export package;
- no representative historical qualification corpus, accuracy metrics, blind
  comparison, or parallel-operation evidence.

## Reliability audit

Resolved during the audit:

- snapshot hashes are stable under input/version ordering;
- all content-addressed reads verify SHA-256;
- release verifies the snapshot object, nested hashes, document-set binding,
  controlled-version binding, and the calculation run linked by the snapshot;
- readiness returns HTTP 503 when a mandatory runtime dependency or
  authentication configuration is unavailable;
- SQLite and PostgreSQL migration cycles pass upgrade, downgrade, re-upgrade,
  and metadata-drift checks;
- the runtime image now contains Alembic configuration and migrations.

Open reliability risks:

- mutating POST operations do not yet implement a persisted idempotency-key
  ledger;
- outbox tables and connector contracts exist, but no production dispatcher,
  retry/dead-letter operations, or replay qualification exists;
- no load, soak, failover, backup-restore, or disaster-recovery result exists;
- distributed locking and deadline behaviour under tender-day concurrency
  have not been qualified.

## Security audit

Resolved during the audit:

- the infrastructure `ADMIN` role no longer substitutes for methodology,
  assigned expert, calibration, document-set, or bid-release approval;
- snapshot/object tampering is detected on read and blocks release.

Open security blockers remain tracked in
`security_best_practices_report.md`: untrusted parser sandboxing and malware
quarantine, streamed upload/archive processing, project-level ACLs, verified
WORM/external audit anchoring, and distributed rate limiting.

## Usability and developer-experience audit

The action API covers the safety-critical write workflows and returns explicit
gate failures. Missing operator capabilities are material:

- most entities lack paginated list/search/history endpoints;
- there is no consolidated project workbench showing current revisions,
  blockers, tasks, lineage, and change impact;
- domain failures are mostly human-readable strings rather than a stable
  machine-readable error catalogue;
- there is no production user interface, accessibility review, operator
  training flow, or task notification experience;
- local `uv` editable environments can fail under a Cyrillic checkout path;
  verification therefore uses a clean `--no-editable` environment.
