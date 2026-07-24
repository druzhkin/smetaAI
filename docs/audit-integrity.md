# Audit integrity, key rotation, and external anchoring

This control detects tampering; it does not grant business approval. Production
readiness requires both a live WORM policy and a fresh independently signed
checkpoint receipt.

## Event signatures

Every new audit event uses `HMAC-SHA256-V2`. The canonical hashed record
contains both `signing_key_id` and `signature_version`, so changing either
invalidates the event. Verification selects the exact historical key by ID and
then verifies every link from genesis.

Legacy events remain `HMAC-SHA256-V1`. Migration `b92d5f8c0e31` first verifies
every legacy chain with the configured historical key, then records that key
ID and version without changing the old event hash or signature. A wrong key,
malformed JSON/timestamp, or damaged chain aborts before schema changes. Once
audit evidence exists, downgrade is deliberately refused.

## Checkpoint and receipt protocol

An `ADMIN` whose tenant matches `AUDIT_OPERATOR_ORGANIZATION_ID` creates a
global checkpoint with:

`POST /v1/audit/checkpoints`

The service independently verifies all current chains and writes a canonical,
content-addressed `tenderguard.audit-checkpoint/v1` manifest to the evidence
store. It contains the event count and terminal sequence/hash of every
aggregate chain.

The external provider signs canonical JSON with this exact schema:

```json
{
  "anchored_at": "UTC timestamp",
  "checkpoint_hash": "lowercase SHA-256",
  "external_reference": "provider receipt/log reference",
  "provider_id": "configured provider",
  "provider_key_id": "configured provider key",
  "schema_version": "tenderguard.audit-anchor-receipt/v1"
}
```

A different `ADMIN` registers the Ed25519 receipt with:

`POST /v1/audit/checkpoints/{checkpoint_id}/receipts`

The application holds only the configured provider public key. It rejects a
same-admin receipt, future/pre-checkpoint timestamp, wrong signature, wrong
provider binding, or a conflicting retry. Receipt and checkpoint rows are
immutable in PostgreSQL.

`GET /v1/audit/anchor-status` is administrator-only. Readiness performs the
same validation: reopens and hashes the WORM object, verifies the receipt,
enforces maximum age, re-verifies every current audit chain using its key ID,
and proves every anchored terminal remains in current history.

In staging and production the release engine independently applies the same
WORM/anchor condition. A stale or invalid operational-integrity state produces
the blocking finding `OPERATIONAL_INTEGRITY_UNAVAILABLE` and the project
release result is `BLOCKED`; a green deployment probe is not trusted as a
substitute for this server-side release check.

## WORM requirements

Production configuration declares:

- `S3_REQUIRED_OBJECT_LOCK_MODE`;
- `S3_MINIMUM_RETENTION_DAYS`.

The evidence bucket must have versioning, object lock, and a default retention
policy meeting or exceeding both values. `COMPLIANCE` satisfies a configured
`GOVERNANCE` requirement, not the reverse. Every production evidence write
checks the live bucket policy and fails closed. The quarantine bucket is
separate and is not treated as the evidence trust anchor.

## Safe HMAC rotation

Never re-sign historical events.

1. Before migration `b92d5f8c0e31`, configure the exact legacy HMAC as
   `AUDIT_SIGNING_KEY` and give it a non-legacy `AUDIT_SIGNING_KEY_ID`.
2. Apply the migration and verify the complete chain.
3. Create and externally anchor a final checkpoint under the old active key.
4. Put the old key into `AUDIT_VERIFICATION_KEYS` under its unchanged ID.
5. Atomically configure a new `AUDIT_SIGNING_KEY_ID` and
   `AUDIT_SIGNING_KEY`. Keep all old verification keys for the full audit
   retention period.
6. Create one governed event, create a new checkpoint, register its independent
   receipt, and require `/health/ready` to return 200.

If any historical key is unavailable, stale anchoring cannot be repaired by
inventing a key or re-signing data. Keep release blocked and follow the
incident process.

## Remaining operational evidence

Repository tests use a deterministic test provider and simulated WORM status.
Production still requires a contracted/approved external transparency or
timestamp provider, independently protected provider keys, retention-policy
screenshots/API evidence, monitoring, alerting, and restore/tamper drills.
