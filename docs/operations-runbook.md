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

## Runtime health probes

- `GET /health/live` returns HTTP 200 while the process can serve requests.
- `GET /health/ready` returns HTTP 200 only when the database is at the exact
  application Alembic head, evidence and quarantine stores are reachable,
  authentication is configured, malware-scanner and document-processor
  qualifications are active, the worker actor is configured, and the Ed25519
  export signing key is valid. It returns HTTP 503 otherwise.
- Normative-engine qualification is reported by readiness but remains a
  separate bid-release hard stop; an operational API process is not, by that
  fact alone, authorised to issue a bid.

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

Actuals may be entered only after an internal/bid approval or project closure.
The factual observation must be verified, the variance must be classified
against a fixed snapshot input, and a different methodology owner must approve
the resulting calibration example before it is admitted to a training or
benchmark dataset.

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
3. Run migrations in read-only rehearsal mode.
4. Verify the audit chain and snapshot hashes.
5. Recalculate a golden set from atomic inputs.
6. Confirm OIDC, connector, and secrets-manager bindings.
7. Record achieved RPO/RTO and independent reviewer sign-off.

No restore test has yet been performed for this repository.

## Release incident

If a new tender-document revision arrives after confirmation, the application
clears the confirmed document-set reference. Reconfirm the new candidate,
repeat extraction/reconciliation, invalidate affected price/quantity evidence,
recalculate, independently validate, and create a new snapshot. Released
snapshots are never edited.

## Key rotation

- Rotate OIDC/JWKS according to IdP procedure.
- Audit HMAC rotation requires a key ID and externally anchored final hash for
  the old chain; this is an open implementation item.
- Ed25519 export-key rotation uses a new key ID. Existing artifact rows retain
  their exact public key and fingerprint, but the organisation must maintain
  the independently trusted historical public-key registry.
- Rotate database/S3 credentials through a secrets manager and test access
  before revocation.
