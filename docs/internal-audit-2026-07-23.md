# Internal audit — 2026-07-23

This is a repository audit, not production acceptance evidence.
Durable document-job findings were re-audited on 2026-07-24.

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
- approved releases can produce deterministic Ed25519-signed estimate/audit
  packages whose manifest covers every mandatory content section.

Open product blockers:

- no qualified OCR/visual, normative, market/RFQ, ERP/DMS/BI, or export
  connectors are supplied;
- detailed logistics/mobilisation planning and contract cash-flow calculation
  remain generic cost components rather than operational planning services;
- no external export delivery adapter or approved public-key trust registry;
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
- native export generation is idempotent and verification detects package,
  manifest, signature, source-snapshot, release-decision, and audit-chain
  divergence;
- SQLite and PostgreSQL migration cycles pass upgrade, downgrade, re-upgrade,
  and metadata-drift checks;
- the runtime image now contains Alembic configuration and migrations.

Open reliability risks:

- mutating POST operations do not yet implement a persisted idempotency-key
  ledger;
- document intake now has leased outbox dispatch, bounded retry/dead-letter,
  timeout checks, and audited administrator replay; other connector topics
  still have no production dispatcher, and the external scheduler/sandbox
  remains unqualified;
- no load, soak, failover, backup-restore, or disaster-recovery result exists;
- distributed locking and deadline behaviour under tender-day concurrency
  have not been qualified.

## Security audit

Resolved during the audit:

- the infrastructure `ADMIN` role no longer substitutes for methodology,
  assigned expert, calibration, document-set, or bid-release approval;
- snapshot/object tampering is detected on read and blocks release.
- whole-file API reads and `ZipFile.read` archive expansion were replaced by
  size-limited quarantine streaming and bounded worker-local spooling;
- untrusted parser calls were removed from the API path; exact-hash malware
  results and the worker parser both require active configured qualifications.
- document parsing no longer holds a database transaction; stale leases are
  reclaimable, terminal outbox events are immutable in PostgreSQL, and an
  exhausted upload retains its blocker until a new audited replay succeeds.

Open security blockers remain tracked in
`security_best_practices_report.md`: deployment/qualification of the real
scanner and disposable parser runtime, operational IdP/access recertification
and owner recovery, verified WORM/external audit anchoring, and distributed
rate limiting. Versioned project ACL enforcement is now implemented in the
application and protected by PostgreSQL membership-history triggers.

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
