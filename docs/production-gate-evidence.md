# Production gate evidence registry

This registry prevents the non-business production-readiness gates from being
satisfied by a caller-supplied SHA-256 string. It supplies the repository-side
evidence mechanism; it does not manufacture a security review, a load run, a
restore drill, resilience results, calibration evidence, or methodology
approval.

## Governed evidence profile

Every package binds an independently approved controlled version of kind
`production_gate_evidence_profile`, schema
`tenderguard.production-gate-evidence-profile/v1`. The profile fixes:

- one production gate and the immutable application build;
- allowed environments and maximum evidence age;
- internal-runner or externally attested evidence mode;
- required and allowed artifact categories, artifact count and byte limits;
- required machine-readable claim keys;
- independent approval roles;
- either the exact approved operational source profile or an Ed25519 attester
  identity, key ID, and public key.

The application defines no fallback age, size, or acceptance thresholds. A
methodology owner creates the profile and a different methodology owner
approves it. Superseding the profile invalidates packages that depended on it.

Only `AUDITOR`, `METHODOLOGY_OWNER`, and `ADMIN` can be selected as evidence
approval roles. `SYSTEM` and operational production roles cannot approve a
production-readiness claim.

## Evidence modes

`INTERNAL_QUALIFICATION_RESULT` is mandatory for:

- `load_test`, bound to one approved `load_test_profile` and a `LOAD` result;
- `backup_restore`, bound to one approved `recovery_profile` and a `RECOVERY`
  result.

The registry parses the self-verifying result, requires
`TECHNICAL_VERIFICATION_PASSED`, requires every finding to pass, checks the
exact source-profile ID/hash/audit approval, build, environment and timestamps,
and reopens the retained `QUALIFICATION_RESULT` object. The artifact JSON must
reproduce the registered result exactly. An externally signed report cannot
replace these runner results.

`EXTERNAL_ATTESTED_PACKAGE` is used for:

- `rules_and_catalog_calibration`;
- `damaged_conflicting_document_resilience`;
- `security_review`;
- `methodology_approval`.

The external provider signs the canonical
`tenderguard.production-gate-evidence/v1` statement with Ed25519. The statement
covers organisation, gate, profile ID/hash, immutable build, environment,
executor, time interval, content-addressed artifacts and declared claims.
TenderGuard verifies the signature against the public key in the approved
profile. Changing any covered value invalidates the package.

## Artifact requirements

Artifacts must already exist in the configured content-addressed evidence
store. For every submission and every later release check, TenderGuard:

1. reopens each object by SHA-256;
2. lets the object-store adapter verify its content hash;
3. counts the bytes and compares the exact declared size;
4. enforces the profile's per-object, total-byte and count limits;
5. enforces required/allowed categories and required claims.

In staging and production the normal evidence store must be the configured
versioned S3-compatible WORM store. Staging an artifact is an operational
integration responsibility; the API does not create a second unscanned file
upload path.

## Four eyes, audit, expiry, and revocation

Registration creates an immutable package row and signed audit event. A
separate actor in one of the profile-approved control roles records exactly one
immutable `APPROVED` or `REJECTED` review. The submitter and declared executor
cannot approve the package. The approval hash covers the package, decision,
reason, reviewer and timestamp.

An approved package can be revoked by an auditor, methodology owner, or
administrator. Revocation is append-only and immediately invalidates every
production qualification or bid-release attempt that references it. Expired
evidence, missing objects, a superseded profile, a changed build, broken audit
chain, invalid signature or mismatched approval also fail closed.

PostgreSQL rejects update and delete on package, approval, and revocation
tables. Downgrade refuses to remove the registry when any governed package
exists.

## Production qualification binding

Each of the six non-business gates must contain:

```json
{
  "status": "PASSED",
  "evidence_hash": "<approval_hash>",
  "evidence_package_id": "<package_id>",
  "source_reference": "production_gate_evidence_package:<package_id>",
  "owner_id": "<profile_creator>",
  "approved_by": "<independent_reviewer>",
  "approved_at": "<review timestamp>",
  "environment": "<package environment>"
}
```

Controlled-version approval and every bid release re-run the live package,
profile, object, result/signature, approval, expiry, revocation, and audit-chain
checks. Copying a valid-looking hash or a stale package into the controlled
version cannot satisfy the gate.

The four historical/blind/parallel/variance gates continue to bind the
separately governed business qualification campaign and its package.

## API

- `POST /v1/qualification/production-evidence/packages`
- `GET /v1/qualification/production-evidence/packages/{package_id}`
- `POST .../{package_id}/review`
- `POST .../{package_id}/revoke`

All mutations require persisted idempotency in production and produce audit and
transactional outbox records.

## What remains external

Repository tests demonstrate integrity and rejection behavior. Production
still requires real reports and runner output, an organisation-approved
evidence profile for every gate, independently controlled attester keys,
representative environments, retained telemetry, named reviewers, and formal
process-owner acceptance. Until those packages exist and remain valid,
`APPROVED_FOR_BID` stays blocked.
