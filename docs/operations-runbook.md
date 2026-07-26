# Operations runbook (draft - not approved)

## Roles and segregation

- Estimator: builds BoQ, quantities, and cost inputs.
- Procurement: normalizes quotes and runs RFQ.
- Technical expert: verifies scope, quantities, equipment, and analogues.
- Reviewer: resolves evidence conflicts and reviews extraction.
- Approver: attempts release after all tasks pass.
- Methodology owner: owns calculation, quantity, risk, approval, and document
  requirement rules.
- Catalog owner: owns canonical nomenclature and equivalence rules.
- Auditor: read-only access to evidence, events, snapshots, and exports.
- Administrator: operates infrastructure and cannot substitute for business
  approval.

No critical change may be approved by its author.

## Audit and project-access migrations

Before applying migration `b92d5f8c0e31`, restore the exact
`AUDIT_SIGNING_KEY` that signed all unversioned audit history and configure its
non-legacy `AUDIT_SIGNING_KEY_ID`. The migration verifies every chain before
adding key/version metadata and never re-signs history. A wrong key or damaged
event aborts before DDL. Rotate only after this migration; preserve the old key
in `AUDIT_VERIFICATION_KEYS`. The complete protocol is in
`docs/audit-integrity.md`.

Before applying migration `a81c4e7d9b20`, restore the exact
`AUDIT_SIGNING_KEY` that signed the existing project audit history. The
migration verifies every complete project chain and derives the initial owner
only from its single verified `project_created` event. A missing event,
tampered chain, unknown creator role, or wrong historical key aborts the
migration transaction; do not bypass this stop by inserting memberships
manually.

Every active scanner, document processor, or other project-bound adapter
qualification must contain the governed `service_actor_id` of the runtime
identity that presents the `SYSTEM` role. After deployment, readiness and
service actions fail closed when this binding is absent or differs. Reapprove
legacy qualifications with evidence and four-eyes governance before enabling
traffic. Do not grant `SYSTEM` through project membership.

Project owners grant and revoke project membership through the API. Organisation
membership, an `ADMIN` role, or possession of a project identifier does not
confer project access. Retain at least two governed project owners where the
operational policy requires owner recovery; the application blocks removal of
the last recorded owner but cannot repair an IdP-disabled sole owner.
Membership migrations validate and backfill the relational role mask from the
immutable JSON role set. Invalid, duplicate, unknown, or `SYSTEM` role evidence
blocks migration; do not bypass the stop with a hand-written mask.

## Runtime health probes

- `GET /health/live` returns HTTP 200 while the process can serve requests.
- `GET /health/ready` returns HTTP 200 only when the database is at the exact
  application Alembic head, evidence and quarantine stores are reachable,
  the live evidence-bucket WORM policy satisfies configured retention,
  authentication is configured, the normative engine, malware scanner, and
  document processor are qualified, the worker actor is configured, the
  Ed25519 export and integration signing identities are valid, an active
  outbound/source/handler qualification set exists for the operator
  organization, a fresh external audit receipt validates against the complete
  current audit history, persisted idempotency is mandatory for mutations, and
  all distributed actor/organisation quota settings are explicitly configured.
  It returns HTTP 503 otherwise. Connector reachability and scheduler activity
  require separate deployment probes and alerts.
- Operational readiness remains distinct from bid authority. Project-specific
  document, evidence, calculation, approval, snapshot, and release hard stops
  are re-evaluated separately. Staging/production release also rechecks
  WORM/anchor integrity and returns a blocked decision with
  `OPERATIONAL_INTEGRITY_UNAVAILABLE` when it fails.

## Distributed request quotas

Staging and production must set `RATE_LIMIT_ENABLED=true`, a versioned
`RATE_LIMIT_IDENTITY_KEY_ID`, a secret of at least 32 bytes, one fixed-window
duration, and separate positive actor/organisation limits for `READ`,
`MUTATION`, and `UPLOAD`. TenderGuard intentionally provides no financial- or
capacity-owner thresholds. Approve the values from representative load,
multipart-abuse, concurrency and soak evidence before deployment.

Every authenticated request consumes both buckets in a separate short
transaction before business processing. Denied and subsequently invalid
requests therefore consume capacity. `429` returns the effective actor and
organisation limits, remaining capacity, window reset, category, and
`Retry-After`. If PostgreSQL is unavailable, the policy differs between live
instances, or bucket integrity cannot be reproduced, the API returns `503`
instead of processing without a quota.

Rotate the identity HMAC key only at an approved window boundary and deploy the
new key ID, secret and full policy atomically to every instance. Mixed policies
inside one live window fail closed. Never update, decrement, or delete bucket
rows directly; PostgreSQL permits only an exact increment or a later-window
reset to one. Alert on quota `429` rates by category, any quota `503`, policy
mismatch, database latency, organisation saturation, and repeated actor
churn.

These controls start after authentication. They do not protect TLS
termination, unauthenticated OIDC/JWKS work, socket/connection capacity, or
multipart bytes before the application. Deploy independent ingress request
size, header, connection, timeout and unauthenticated-IP controls plus upload
concurrency limits. Application quotas and ingress controls must be exercised
together in the governed load/security evidence package. See
`docs/distributed-rate-limiting.md`.

## Quarantined document intake

The upload endpoint returns `202` and an opaque upload ID. Do not expect a
document revision until the upload status becomes `PROCESSED`.

1. The scanner integration reads the quarantine object by its server-side
   identity and submits an exact-hash result as a `SYSTEM` identity.
2. A `CLEAN` scan transaction creates `document.upload.scan-clean` in the
   outbox. Run
   `tenderguard dispatch-document-intake --max-events <bounded-count>` only in
   the approved disposable profile built from Docker target
   `document-worker`. The target defaults to one delivery per container.
   `process-quarantined-upload --upload-id <id>` uses the same outbox claim
   protocol for incident-specific execution; it is not a bypass.
3. Each delivery obtains an expiring outbox lease and then a separate upload
   lease. Object promotion and parsing run after the claim transaction commits.
   Finalization succeeds only for the same lease token and before the persisted
   deadline.
4. Poll the upload status endpoint. `REJECTED`, `SCAN_FAILED`,
   `PROCESSING_FAILED`, and `PROCESSING_DEAD_LETTERED` are not successful
   intake. Failed deliveries use bounded exponential backoff; an expired lease
   is reclaimable by another worker.
5. On `PROCESSING_DEAD_LETTERED`, fix and evidence the scanner/processor/runtime
   incident. An `ADMIN` may then call
   `POST .../document-uploads/{id}/requeue-processing` with a specific reason.
   Replay creates a new outbox event and preserves the old terminal event; it
   does not change the malware verdict or register a document.
6. For an infected file, obtain a clean replacement from an authorised source.
   Do not override the verdict. A replacement resolves the old blocker only
   when it is itself scanned and processed.

Alert on stale leases, retry growth, any dead-letter, worker non-zero exit, and
age of the oldest pending `document.upload.scan-clean` event. Do not edit
`attempts`, lease columns, terminal timestamps, or upload status directly.

Before applying the durable-job migration, stop document workers and verify
that no upload is `PROCESSING`. The migration deliberately refuses an active
job instead of fabricating or discarding its ownership state. A downgrade is
also refused after lease/dead-letter history exists.

Use separate object-store buckets and credentials for quarantine and evidence.
The worker must have no unrestricted network egress and must be constrained by
CPU, memory, process, temporary-disk, and wall-clock limits. The detailed
contract is in `docs/quarantined-intake.md`.

## Controlled workflow

Operators must advance projects only through the API workflow transitions.
Each transition is recorded in the audit trail. Before moving to calculation,
resolve the server-reported passport, BoQ/quantity, scope, price/RFQ, contract,
and risk blockers. Before bid release, run the independent validator, fix a
content-addressed snapshot, complete every planned expert task, and use the
release evaluation; a UI badge or manually edited state is not release
authority.

On the calculation screen, review every server-provided component, source
identifier, quantity, rate, factor and controlled policy. The execution
command must contain only the current candidate hash, project row version,
reason and idempotency key; never add a client-calculated total. After
execution, confirm that the fixed snapshot passes object/run integrity and the
independent result is `passed`.

On the release screen, select internal or bid explicitly and review the full
hard-stop register. The approver must enter the project code and exact target
state. The request must carry the target-specific gate hash and row version
shown by the same server response. A stale-gate error requires reloading and
reviewing the entire result; it must not be retried with a substituted hash or
worked around by a direct workflow transition. See
`docs/calculation-release-control.md`.

When upstream evidence changes, create a new revision and repeat the affected
verification. Do not reuse an earlier scope attestation, price decision, risk
calculation, validation result, or snapshot when its signed input set is stale.
Uploading a new current document revision automatically moves an advanced
project to `BLOCKED` and invalidates derived current records. Resume through
`EXTRACTION_IN_PROGRESS`; do not re-verify a BoQ revision bound to the prior
document set.

Resolve an extraction conflict only through the conflict-resolution action.
The reviewer must state the reason and select an observation already present
in the conflict. The resulting derived verified observation, review task, and
approval record must remain in the audit chain.

Record a manual extraction correction only from the Evidence registry's
dedicated action. Select a revision from the current confirmed set and preserve
the exact value, unit, locator, observation time and reason. Exact numeric
values are decimal strings. The source remains `UNVERIFIED`; a different actor
with the role assigned by the bound `manual_evidence_policy` must use the
dedicated review action. If the policy or document set changed, create a new
observation against the new basis instead of attempting to approve the stale
task. See `docs/manual-evidence-review.md`.

Before pricing can advance, create every `commercial_cost_model` kind required
by the bound methodology. Do not enter a lump-sum percentage. Confirm that
each transport/cargo/rate, mobilisation, cash-flow, funding-rate, and guarantee
value is reproduced by the `commercial_cost_bases` map of its verified current
observation. A different reviewer must approve the generated task before
finalization. If a document set, policy, BoQ target, contract term, or evidence
value changes, create and approve a new model revision; never edit a validated
row. The full operating contract is in `docs/commercial-cost-models.md`.

Actuals may be entered only after an internal/bid approval or project closure.
The factual observation must be verified, the variance must be classified
against a fixed snapshot input, and a different methodology owner must approve
the resulting calibration example before it is admitted to a training or
benchmark dataset.

## Historical, blind, and parallel qualification

Before creating a campaign, the methodology owner must approve the exact
profile and a closed dataset whose selected and explicitly excluded cases cover
the full declared population. Keep the selection query/evidence hashes outside
the application database for independent comparison. Do not reveal blind or
parallel reference values in the dataset.

An auditor with access to every selected project locks the campaign. From that
point, never replace a snapshot, remove a case, or reuse the campaign after a
failed result. Professional references are prepared only after the lock and
verified by a different project reviewer. Blind professionals must not receive
the system prediction or possess bid authority.

Have a separate auditor evaluate exact metrics. Investigate every generated
material discrepancy using verified project evidence and the approved reason
taxonomy. A methodology owner who was neither creator nor evaluator may approve
only after all material reviews are independently accepted. Preserve the
campaign package hash, profile/dataset approvals, supporting observations,
telemetry and meeting decision.

The historical, blind, parallel, and variance gates in
`production_qualification` must reference the same `PASSED` campaign and
package. TenderGuard revalidates that evidence both when the controlled version
is approved and during bid release. Full schemas, roles and endpoints are in
`docs/business-qualification.md`.

## Non-business production gate evidence

Before running or commissioning a qualification activity, approve a dedicated
`production_gate_evidence_profile` for that gate and immutable build. Stage
supporting artifacts in the WORM evidence store and retain the external job,
change, scanner, or assurance references outside TenderGuard for independent
comparison.

For load and restore, run the approved operational profile and register the
complete passing result plus its retained JSON artifact. Do not replace a
blocked or failed result with a narrative report. For security, resilience,
calibration, and methodology evidence, have the provider sign the exact
canonical statement with the Ed25519 key already approved in the gate profile.

A different control-role actor reviews the package. Bind the returned approval
hash and package ID into the matching production gate. Revoke a package
immediately if a material defect, key compromise, incomplete scope, invalid
environment, or unreliable evidence is discovered. Expiry and revocation block
release without a grace status. See
`docs/production-gate-evidence.md` for the complete schema and API.

## Mutation retries and outbox delivery

All production mutation clients must send a stable `Idempotency-Key` for the
logical action and reuse it only when retrying that exact method/path/body. A
`409` means the key was reused for different content or an operation is still
in progress; do not generate a new key blindly when the first outcome is
unknown. Verify the original request and ledger.

Outbox delivery is at least once. Internal consumers use the event
`deduplication_key`; external signed receivers persist and deduplicate on
`delivery_deduplication_key`. Neither may infer identity from timing or payload
similarity. Do not edit event payloads, keys, attempts, leases, or terminal
timestamps directly. The complete mutation contract is in
`docs/reliable-mutations.md`.

## Signed enterprise integration

Run outbound dispatchers and inbound handlers only as dedicated `SYSTEM`
identities exactly bound to active organization/topic qualifications. Runtime
endpoints must be HTTPS, explicitly allowlisted, redirect-free, and supplied
through the approved secrets/configuration system. A successful TLS request is
not delivery: acknowledge an event only after the exact remote signed receipt
is persisted.

Alert on oldest pending age, retry rate, expired leases, every permanent
protocol failure/dead letter, signature/key/identity mismatch, deduplication
collision, replay, and absence of worker heartbeats. A replay is allowed only
after an administrator records the corrected incident and reason. It creates a
new immutable processing generation or internal outbox row; never rewrite the
terminal source record.

Inbound `ACCEPTED`/`CONSUMED` means only that transport and a durable handler
result succeeded. The result must enter the relevant domain as unverified and
pass its evidence, normalization, reconciliation, and approval controls.
Before production, execute exact-duplicate, crash-after-remote-acceptance,
timeout, stale/wrong-key, payload-collision, unavailable endpoint, dead-letter,
and replay drills against each real receiver. The full contract is in
`docs/integration-delivery.md`.

## Signed release export

Before calculation, bind an approved `export_template` declaring
`tenderguard.signed-estimate-audit/v1` and `TENDERGUARD_SIGNED_JSON`. Release
will block if this governed version is absent.

After an allowed internal/bid release, an approver or infrastructure
administrator may generate the package for the exact released snapshot.
Generation is idempotent for the snapshot, release decision, template, and
format. Deliver only the artifact returned by the verified content endpoint;
it rechecks the content-addressed object, manifest hashes, Ed25519 signature,
source snapshot, release decision, and packaged audit chain.

Publish the signing key ID and SHA-256 public-key fingerprint through an
organisation-approved channel independent of the package and application
database. Do not treat the public key embedded in a package as its own trust
anchor. Preserve prior public keys for the full artifact-retention period.

## Incident priorities

- P0: suspected wrong released bid, audit tampering, cross-organisation access,
  lost original/snapshot, or compromised signing/identity key. Disable release,
  preserve logs, revoke access, freeze affected snapshots, notify process owner.
- P1: failed independent calculation, incomplete current document set, corrupt
  upload, connector returning stale/wrong data, backup failure.
- P2: delayed extraction/RFQ/export with no released-price impact.

Do not "work around" a hard stop by editing database state.

For any suspected incorrect release, preserve the snapshot and evidence graph;
never replace or delete them. Mark the affected result superseded through the
governed workflow and open a new revision/correction chain.

## Backup and restore

Target controls:

- PostgreSQL continuous WAL archiving plus encrypted daily base backup;
- versioning and object lock for original documents and snapshots;
- independent backup account/credentials;
- quarterly restore test into an isolated environment;
- reconciliation of restored DB object hashes against restored object storage.

Restore acceptance:

1. Restore PostgreSQL to the declared point in time.
2. Restore/attach the exact object-store versions.
3. Select a four-eyes-approved `recovery_profile`; obtain its content hash from
   the independent change record and confirm it names the exact immutable
   application build being restored.
4. Prepare a timezone-explicit recovery exercise manifest with exact database,
   object-store, identity, connector, secrets-manager, executor, and change
   references.
5. Run `tenderguard verify-restored-system` against the isolated environment.
   It verifies the exact schema, all governed payloads and object references,
   audit/checkpoint/anchor integrity, signed exports, and deterministically
   replays every calculation plus the profile's golden set.
6. Preserve the exclusive, content-hashed result and infrastructure telemetry.
7. Have a different reviewer validate the declared external evidence and
   achieved RPO/RTO before registering the `backup_restore` gate evidence.

The exact command, schemas, blocking semantics, and evidence contract are in
`docs/operational-qualification.md`.

The verifier and negative tests exist. No real organisation backup, isolated
restore, failover, or independently signed exercise has yet been performed, so
the production backup/DR gate remains open.

## Release incident

If a new tender-document revision arrives after confirmation, the application
clears the confirmed document-set reference. Reconfirm the new candidate,
repeat extraction/reconciliation, invalidate affected price/quantity evidence,
recalculate, independently validate, and create a new snapshot. Released
snapshots are never edited.

## Key rotation

- Rotate OIDC/JWKS according to IdP procedure.
- Audit HMAC rotation uses versioned key IDs, retained historical verification
  keys, and independently anchored checkpoints. Never re-sign history; follow
  `docs/audit-integrity.md`.
- Ed25519 export-key rotation uses a new key ID. Existing artifact rows retain
  their exact public key and fingerprint, but the organisation must maintain
  the independently trusted historical public-key registry.
- Rotate database/S3 credentials through a secrets manager and test access
  before revocation.
