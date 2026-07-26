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
- Location: `src/tenderguard/application/quarantine.py:211`,
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
- Location: `src/tenderguard/api/main.py:608`,
  `src/tenderguard/infrastructure/object_store.py:48`,
  `src/tenderguard/infrastructure/intake.py:642`
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

### SEC-003 - Project object authorisation and information barriers

- Rule ID: FASTAPI-AUTHZ-001
- Location: `src/tenderguard/application/projects.py:263`,
  `src/tenderguard/infrastructure/orm.py:57`,
  `migrations/versions/a81c4e7d9b20_add_project_access_control.py:30`
- Evidence: the central project lookup now requires an active, latest
  membership revision and an action-specific intersection between OIDC roles
  and project roles. Non-members receive `404`, infrastructure `ADMIN` is no
  longer accepted as a business-calculation role, and `SYSTEM` identities can
  use only an explicit capability backed by an active qualification bound to
  the exact service actor.
- Impact: the prior same-organisation horizontal-access path is closed.
  Membership grant, role replacement, and revocation are project-owner
  actions with project audit events and append-only revision history.
- Database enforcement: PostgreSQL rejects membership update/delete, forked
  revision chains, empty roles, and human membership for `SYSTEM`; application
  changes serialize on the project row. Legacy backfill refuses to infer an
  owner unless exactly one `project_created` audit event exists.
- Residual operational work: qualify IdP role/group lifecycle, owner recovery
  and break-glass procedures, periodic access review, and (where required)
  PostgreSQL RLS as an additional independent control.
- Status: **REMEDIATED IN APPLICATION/DATABASE CODE; operational access
  governance evidence OPEN**.

### SEC-004 - WORM and external audit anchoring require production evidence

- Rule ID: integrity/repudiation defence in depth
- Location: `src/tenderguard/infrastructure/object_store.py`,
  `src/tenderguard/application/audit_integrity.py`,
  `migrations/versions/b92d5f8c0e31_add_anchored_audit_integrity.py`
- Evidence: production writes and readiness verify live bucket
  versioning/object-lock/default retention. Audit events bind versioned HMAC
  key IDs; legacy history is verified before migration without re-signing.
  Global content-addressed checkpoints require a second administrator to
  register a configured-provider Ed25519 receipt. Readiness re-verifies the
  receipt, WORM object, every current chain, and all anchored terminals.
- Impact: a privileged infrastructure actor with DB/object-store/key access
  is detected only if the external provider/key and operational monitoring are
  genuinely independent. A simulated test provider is not that evidence.
- Fix: deploy and qualify the independent provider, separate audit
  writer/reader and provider custody, schedule anchors, alert on age/failure,
  capture real retention evidence, and run restore/tamper drills.
- Status: **REMEDIATED IN APPLICATION/DATABASE CODE; external trust and
  operational evidence OPEN - production blocker**.

### SEC-005 - Independent edge and upload-concurrency controls are absent

- Rule ID: FASTAPI-RES-001
- Location: `src/tenderguard/api/security.py:8`,
  `src/tenderguard/application/rate_limits.py:24`,
  `migrations/versions/fa2c5d7e9014_add_distributed_rate_limit_buckets.py:1`
- Evidence: the application checks declared and actually streamed body size,
  including chunked JSON and multipart requests. It now atomically consumes
  independent actor and organisation PostgreSQL buckets for read, mutation and
  upload traffic before business work. Policy disagreement or store failure
  returns `503`; over-limit traffic returns `429`; production refuses an
  incomplete or disabled policy.
- Impact: authentication and connection work occurs before the application
  quota. Unauthenticated floods, slow clients, concurrent multipart spooling,
  and database-connection exhaustion still require a separate ingress/runtime
  control plane.
- Fix: configure independent ingress body/header limits, timeouts, connection
  and unauthenticated-source limits, plus upload concurrency quotas. Approve
  application thresholds and verify all layers together with load, abuse and
  soak tests.
- Repository control added: a four-eyes governed, read-only, bounded load
  qualification runner enforces owner-defined overall and per-endpoint SLOs
  and emits a tamper-evident result. It can measure deployed controls once they
  exist, but it does not provide independent edge controls or replace upload,
  abuse, or soak testing.
- Status: **APPLICATION BODY AND DISTRIBUTED AUTHENTICATED QUOTAS REMEDIATED;
  EDGE/UNAUTHENTICATED/UPLOAD-CONCURRENCY CONTROLS OPEN**.

### SEC-006 - Enterprise connector trust is not operationally qualified

- Rule ID: integration trust-boundary / replay and identity defence
- Location: `src/tenderguard/application/integrations.py`,
  `src/tenderguard/integrations/http_json.py`,
  `docs/integration-delivery.md`
- Evidence: the generic transport binds organization, topic, payload hash,
  external delivery identity, source/receiver identity, and active
  qualification to Ed25519 event/receipt signatures. It persists immutable
  attempts and inbox messages, rejects collisions, commits network-free claim
  transactions, and uses bounded retry/dead-letter/replay.
- Impact: code-level signatures cannot prove that a production endpoint,
  service identity, business mapping, TLS route, or key custodian is the
  intended independent party. A misqualified connector can import authentic
  but semantically wrong data or disclose signed events to the wrong system.
- Fix: deploy organization-specific workers with endpoint allowlists, mTLS and
  secret isolation; qualify key custody, schemas and mappings; contract-test
  exact deduplication/receipt semantics; monitor heartbeats and dead letters;
  perform failure/replay/key-rotation drills. Keep imported business data
  unverified until its domain controls pass.
- Status: **APPLICATION TRANSPORT CORE IMPLEMENTED; DEPLOYMENT AND
  QUALIFICATION OPEN - production blocker**.

## Remediated during review

- Production OpenAPI/docs are disabled.
- Staging/production startup requires an immutable application build reference;
  readiness and runtime configuration expose it so recovery/load profiles can
  bind and reject a different deployed image before qualification.
- Trusted hosts are mandatory and wildcard hosts are rejected.
- The development audit signing key is rejected in staging/production.
- JWT signature, algorithm allowlist, issuer, audience, expiry, issued-at, and
  subject are validated.
- Security response headers and declared/streamed request-size limits are
  installed.
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
  third-party vulnerabilities in the built Linux API environment on
  2026-07-24 (the local proprietary package is naturally not present on PyPI).
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
- Project access is no longer inherited from organisation membership. Owners
  issue explicit versioned project roles; every project-bound service checks
  the role needed for that action, revoked users receive no object visibility,
  and PostgreSQL protects the membership history from mutation.
- Audit HMAC key IDs are covered by the event hash, old keys remain
  independently selectable for verification, and migration refuses damaged or
  wrongly keyed legacy history. WORM policy and a fresh externally signed
  checkpoint are now mandatory readiness conditions.
- Recovery qualification rechecks the exact schema, content-addressed
  originals/snapshots/exports/checkpoints, complete audit chains, controlled
  profile hashes, atomic-input evidence bases, deterministic primary and
  independent calculation replay, and owner-approved RPO/RTO. Corrupt objects
  and modified profiles fail closed. Real backups, restore/failover execution,
  external identity/secrets evidence, and independent reviewer sign-off remain
  operational blockers.
- CI runs frontend format/tests/build/dependency audit, both production
  container targets, Python dependency audit, migration/immutability tests,
  and scheduled CodeQL analysis for Python and JavaScript/TypeScript.
  Repository automation does not replace an organisation security review,
  runtime scanner, penetration test, infrastructure configuration review, or
  signed remediation acceptance.
