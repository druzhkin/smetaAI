# Controlled-version integrity

Controlled methodology, catalog, rule, model, threshold and qualification
content is a safety boundary. A database row with `status=APPROVED` is not
sufficient proof that the content was governed.

## Approval lifecycle

A version is created as `DRAFT` with:

- immutable kind, label and payload;
- organisation, creator and creation time in `_governance`;
- a SHA-256 content hash over kind, label and the complete governed payload;
- a signed `controlled_version_created` audit event.

Approval is the only permitted lifecycle mutation. It changes `DRAFT` to
`APPROVED`, records another authorised actor and timestamp, and appends a
signed `controlled_version_approved` event. Creator and approver must differ.
The required actor role is `CATALOG_OWNER` for catalog/equivalence content and
`METHODOLOGY_OWNER` for all other controlled content.

PostgreSQL rejects:

- insertion in an already-approved state;
- any change to identity, kind, label, hash or payload;
- any transition other than the exact `DRAFT -> APPROVED` transition;
- replacement of approval identity or time;
- deletion.

## Read-time verification

Binding, adapter activation, operational profile loading, calculation and
release reproduce the row instead of trusting its status. Verification checks:

1. the full content hash;
2. organisation ownership;
3. the expected kind and, where applicable, expected hash;
4. creation and approval actor roles;
5. four-eyes separation;
6. lifecycle timestamps;
7. the complete signed two-event audit chain and exact event payloads.

Calculation rejects any invalid or ambiguous bound version kind. Release
reports every invalid bound version through
`CONTROLLED_VERSION_INTEGRITY_FAILED`; malformed rows are not allowed to turn
the release-context build into an availability-based bypass. An invalid
approval policy cannot supply a financial threshold.

The fixed calculation snapshot also retains the exact set of bound version
IDs and hashes. A later binding change makes the snapshot stale.

## Trust boundary

Database triggers prevent accidental or application-level in-place mutation.
Read-time cryptographic verification also detects privileged direct insertion
without the governed audit chain. Database administrator access remains a
privileged security boundary and must be independently controlled, monitored
and covered by the external audit anchor and backup/tamper exercises.
