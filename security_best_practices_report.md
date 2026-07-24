# Security best-practices report

## Executive summary

The application has strong baseline controls for a new codebase: strict OIDC
JWT validation, organisation-scoped queries, explicit response schemas,
content-addressed storage, archive traversal/zip-bomb checks, non-root
container, locked dependencies, production host validation, disabled
production API docs, request-size checks, and immutable audit/snapshot database
triggers.

It is **not ready for production**. The application now enforces streamed
separate quarantine, qualification-bound exact-hash malware results, and a
worker-only bounded parser entry point with leased outbox delivery. A real
malware provider, production scheduler, disposable runtime sandbox,
network/resource policy, and their
qualification evidence are external controls and remain production blockers.

## High severity

### SEC-001 - Operational malware scanner and parser sandbox are not deployed

- Rule ID: FASTAPI-UPLOAD-001 / defence-in-depth input boundary
- Location: `src/tenderguard/application/quarantine.py:193`,
  `src/tenderguard/application/document_processing.py:88`,
  `src/tenderguard/application/document_jobs.py:49`,
  `src/tenderguard/cli.py:123`
- Evidence: the API can only quarantine; a `SYSTEM` result must reproduce the
  report/object hashes and an active configured `MALWARE_SCAN` qualification;
  parsing is reachable through leased, bounded-retry outbox delivery after
  `CLEAN`. The repository does not deploy the scanner product, production
  scheduler, container network policy, or resource limits.
- Impact: if operators run the worker without the mandated disposable sandbox,
  a malicious clean-missed file could exploit a parser vulnerability under
  worker credentials.
- Fix: connect and qualify the organisation's scanner, deploy the CLI only as a
  network-denied disposable job with read-only image and CPU/memory/time/disk
  limits, restrict credentials, and capture qualification/load/abuse evidence.
- Mitigation: separate stores, exact-hash promotion, immutable scan evidence,
  a parser-free API image, parser qualification, expiring ownership tokens,
  persisted deadlines, dead-letter/replay audit, strict allowlist, patched
  worker libraries, and bounded spooling materially reduce exposure but cannot
  prove the runtime boundary or preempt every native parser.
- Status: **PARTIALLY REMEDIATED - production blocker**.

### SEC-002 - Application-level whole-file materialisation

- Rule ID: FASTAPI-RES-001 / FASTAPI-UPLOAD-001
- Location: `src/tenderguard/api/main.py:539`,
  `src/tenderguard/infrastructure/object_store.py:48`,
  `src/tenderguard/infrastructure/intake.py:567`
- Evidence: the API copies the `UploadFile` spool into quarantine with a hard
  byte limit. Archive members use `ZipFile.open`, bounded copy, and
  `SpooledTemporaryFile`; `ZipFile.read` is not used.
- Impact: the prior API/archive memory-exhaustion path is removed. Aggregate
  concurrency can still exhaust workers or temporary disk without edge and
  per-tenant quotas (tracked in SEC-005).
- Fix: retain the bounded streaming code and add representative concurrent
  upload/archive load tests plus ingress and tenant quotas.
- Status: **REMEDIATED IN APPLICATION CODE; operational load evidence OPEN**.

## Medium severity

### SEC-003 - Authorisation is organisation-wide, not project-ACL based

- Rule ID: FASTAPI-AUTHZ-001
- Location: `src/tenderguard/application/projects.py:196`
- Evidence: object access checks organisation ID and role, but no project
  membership or information-barrier table is evaluated.
- Impact: an authorised estimator/reviewer in the same organisation can access
  every tender project, which may violate need-to-know controls.
- Fix: add project membership/ACL and enforce it in the central project lookup;
  separate business administration from infrastructure administration.
- False-positive note: a single-team deployment may formally accept
  organisation-wide access, but that decision must be documented.
- Status: **OPEN**.

### SEC-004 - WORM and external audit anchoring are specified but not verified

- Rule ID: integrity/repudiation defence in depth
- Location: `docs/architecture.md:64`,
  `src/tenderguard/infrastructure/object_store.py`
- Evidence: S3-compatible storage is required, but bucket object-lock/retention
  configuration is not checked; the audit chain is HMAC-signed but not
  externally anchored and has no key-ID rotation protocol in code.
- Impact: a privileged infrastructure actor with DB/object-store/key access
  could rewrite both records and verification material.
- Fix: verify bucket versioning/object lock at readiness; use separate audit
  writer/reader roles; periodically anchor the terminal chain hash outside the
  application trust domain; add signing key IDs and rotation.
- Status: **OPEN**.

### SEC-005 - Distributed rate limiting and edge multipart controls are absent

- Rule ID: FASTAPI-RES-001
- Location: `src/tenderguard/api/security.py:8`
- Evidence: the application checks declared body size but has no distributed
  request/actor quota, and chunked-body enforcement is delegated to ingress.
- Impact: authenticated or unauthenticated request floods can consume workers,
  OIDC/JWKS calls, multipart parsing, and database connections.
- Fix: configure ingress body limits, timeouts, connection limits, per-actor and
  per-organisation rate limits, and upload concurrency quotas; verify with load
  and abuse tests.
- Status: **OPEN**.

## Remediated during review

- Production OpenAPI/docs are disabled.
- Trusted hosts are mandatory and wildcard hosts are rejected.
- The development audit signing key is rejected in staging/production.
- JWT signature, algorithm allowlist, issuer, audience, expiry, issued-at, and
  subject are validated.
- Security response headers and request Content-Length limits are installed.
- Unknown, corrupt, unsafe image, protected, and embedded-object inputs produce
  blocking findings.
- Atomic calculation inputs now resolve their claimed evidence in the database
  and must reproduce the controlled rate, currency, and unit; invented source
  identifiers are rejected before a snapshot is created.
- Automated observations require an active, organisation-scoped adapter
  qualification backed by a separately approved controlled version and can be
  submitted only by a `SYSTEM` identity; verified observations are derived only
  from exact agreement across distinct qualified independence domains.
- Snapshot lineage is resolved from the immutable content-addressed artifact
  through cost inputs, source observations, document revisions and locators.
- Local and S3 object reads recompute SHA-256 before returning content.
  Snapshot reads additionally recompute input, output, and snapshot hashes and
  release compares the snapshot's document set and controlled versions with
  current project bindings.
- A newer current document revision forces `BLOCKED` and invalidates derived
  records; an old snapshot cannot regain release authority merely because a
  new document set was confirmed.
- Infrastructure administrators can no longer approve controlled methodology,
  decide assigned expert tasks, approve calibration data, confirm a document
  set, or release a bid solely through the `ADMIN` role.
- Normative qualification now requires a current approved database record, and
  bid release separately requires a validated project-specific normative
  artifact.
- Production qualification cannot be asserted with one boolean: all mandatory
  quality gates require structured, hashed, owned, environment-specific
  evidence.
- Docker installation uses the lock file; `pip-audit` found no known
  third-party vulnerabilities in the resolved environment on 2026-07-23
  (the local proprietary package is naturally not present on PyPI).
- Approved releases can be exported only as deterministic content-addressed
  packages whose mandatory section hashes are covered by an Ed25519 signature.
  Production readiness now requires a valid signing key configuration;
  artifact rows retain key ID/public-key fingerprint and are immutable in
  PostgreSQL. External authenticity still depends on an independently trusted
  public-key registry.
- Uploads now land in a distinct quarantine store through a size-limited
  stream. Current-candidate uploads immediately clear current document-set
  authority and create a blocker. Only immutable exact-hash results from the
  configured active scanner qualification can set `CLEAN`; infected files
  cannot be parsed or manually overridden.
- PDF/Office/image/archive parsing is no longer an API route action. The
  worker-only entry point promotes the exact clean hash, streams archive
  members through bounded disk-spilling spools, and requires an active
  document-processor qualification. Docker builds a parser-free `api` target
  and a distinct parser-equipped `document-worker` target.
- Clean uploads are dispatched through expiring outbox and upload leases.
  Parsing runs without an open database transaction; success requires the same
  ownership token before the persisted deadline. Retry is bounded, exhausted
  work retains its input blocker in a dead-letter state, and administrator
  replay creates a new event instead of editing terminal PostgreSQL evidence.
