# Security best-practices report

## Executive summary

The application has strong baseline controls for a new codebase: strict OIDC
JWT validation, organisation-scoped queries, explicit response schemas,
content-addressed storage, archive traversal/zip-bomb checks, non-root
container, locked dependencies, production host validation, disabled
production API docs, request-size checks, and immutable audit/snapshot database
triggers.

It is **not ready for production**. Two high-severity controls depend on
infrastructure that is not implemented: malware/sandbox processing and
streaming isolation for large untrusted documents.

## High severity

### SEC-001 - Untrusted document parsing is not sandboxed or malware-scanned

- Rule ID: FASTAPI-UPLOAD-001 / defence-in-depth input boundary
- Location: `src/tenderguard/application/projects.py:219`,
  `src/tenderguard/infrastructure/intake.py:145`,
  `src/tenderguard/infrastructure/intake.py:196`
- Evidence: uploaded bytes are parsed by PDF, Excel, Pillow, and ZIP libraries
  in the API process. No qualified malware scanner or isolated worker is called.
- Impact: a malicious tender file could exploit a parser vulnerability, cause
  resource exhaustion, or reach internal services under API credentials.
- Fix: quarantine originals first; scan with a qualified engine; parse only in
  network-restricted, disposable workers with CPU/memory/time limits and
  read-only inputs; promote evidence only after scan success.
- Mitigation: strict file allowlist, patched parsers, upload limits, and archive
  checks are implemented, but do not close this finding.
- Status: **OPEN - production blocker**.

### SEC-002 - Large uploads and archive members are materialized in memory

- Rule ID: FASTAPI-RES-001 / FASTAPI-UPLOAD-001
- Location: `src/tenderguard/api/main.py:291`,
  `src/tenderguard/infrastructure/intake.py:492`
- Evidence: the endpoint reads the complete upload and `ZipFile.read` reads each
  archive member as bytes.
- Impact: concurrent valid-size uploads or highly expanded files can exhaust
  API memory and cause availability loss during tender deadlines.
- Fix: stream uploads into quarantine object storage; inspect archives via
  bounded streams in workers; apply per-tenant concurrency/rate quotas.
- Mitigation: Content-Length middleware, endpoint limit, file-count,
  uncompressed-size, depth, and compression-ratio checks exist.
- Status: **OPEN - production blocker**.

## Medium severity

### SEC-003 - Authorisation is organisation-wide, not project-ACL based

- Rule ID: FASTAPI-AUTHZ-001
- Location: `src/tenderguard/application/projects.py:172`
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
