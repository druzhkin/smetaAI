# Signed estimate/audit package

## Purpose

The package is a machine-verifiable record of an already allowed internal or
bid release. It does not approve a calculation and cannot bypass a release
hard stop.

Native format:

- schema: `tenderguard.signed-estimate-audit/v1`;
- format: `TENDERGUARD_SIGNED_JSON`;
- media type:
  `application/vnd.tenderguard.signed-estimate-audit+json`;
- signature: Ed25519 over the canonical manifest;
- storage: SHA-256 content-addressed object plus immutable artifact metadata.

## Mandatory signed contents

The manifest records a SHA-256 digest for every section:

- fixed calculation snapshot and independent validation;
- recursive cost-input-to-document lineage;
- exact approved controlled-version payloads;
- approval tasks and decisions as of release;
- workflow transitions as of release;
- allowed release decision;
- project audit chain from genesis through the release decision;
- project and document-set identity.

The content set is fixed in software. An `export_template` may identify and
version the format, but it cannot omit a mandatory section.

## Generation rules

Generation fails unless:

- the project is `APPROVED_FOR_INTERNAL_USE` or `APPROVED_FOR_BID`;
- an allowed release decision references the same snapshot and state;
- the snapshot is fixed, current, and passes nested integrity checks;
- the snapshot controlled-version set exactly matches project bindings;
- the bound `export_template` is approved and its content hash is valid;
- recursive lineage and the project audit chain verify;
- a valid Ed25519 private key and non-empty key ID are configured.

One artifact is allowed for each snapshot, release decision, template version,
and native format. Retrying generation returns and re-verifies the same
artifact rather than signing a different representation.

## Verification and trust

The verifier recomputes every section hash, the manifest hash, public-key
fingerprint, and Ed25519 signature. It also compares the package with immutable
database metadata, rereads the source snapshot, confirms the allowed release
decision, and verifies the packaged audit chain.

The package includes its public key so that cryptographic corruption can be
detected offline. This does not establish organisational authenticity by
itself. An external consumer must compare the key ID and SHA-256 fingerprint
with an independently approved registry or publication channel.

## Rotation and retention

Never overwrite an export artifact or reuse a key ID for different key
material. After rotation, retain the prior public key and fingerprint for at
least as long as any package signed by it. Distribution connectors and the
external trusted-key registry remain production integration responsibilities.
