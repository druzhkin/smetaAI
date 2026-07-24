# Reliable mutations and transactional outbox

## Persisted idempotency contract

Staging and production require `Idempotency-Key` on every `POST`, `PUT`,
`PATCH`, and `DELETE`. The key is 8-128 characters and is scoped to the exact
organisation and actor. Clients must generate a new unpredictable key for each
logical operation and retain it until the outcome is known.

The server fingerprints:

- HTTP method and concrete path;
- sorted query parameters;
- normalized media type;
- SHA-256 and size of a non-multipart body;
- sorted form fields plus filename, media type, size, and SHA-256 of every
  multipart file.

The idempotency reservation, business writes, audit events, universal outbox
events, and saved JSON response are committed in one database transaction.
Consequences:

- the first successful request returns `Idempotency-Replayed: false`;
- an exact retry returns the stored status/body and
  `Idempotency-Replayed: true` without executing domain logic;
- reuse for a different method/path/body returns `409`;
- an invalid or missing required key returns `422` or `428`;
- a failed transaction leaves no durable reservation, so a corrected retry
  may execute;
- concurrent PostgreSQL requests use `INSERT ... ON CONFLICT DO NOTHING`; one
  executes and the other waits for and replays the committed result.

Completed records are immutable in PostgreSQL. The database permits only the
single `PENDING → COMPLETED` transition and rejects update/delete afterward.
Mutation responses must be buffered JSON; adding a streaming mutation endpoint
requires a separately designed replay artifact.

External object writes are content-addressed. They are not transactionally
controlled by PostgreSQL, so a crash can leave an unreferenced object, but a
retry writes the same hash rather than duplicating or replacing evidence.
Orphan reconciliation and retention remain an operations responsibility.

## Universal transactional outbox

Every audit event creates `audit.event.recorded` in the same transaction. Its
payload contains organisation, audit event ID, aggregate, sequence, event
type/hash, and timestamp. The stable deduplication key is:

`audit-event:{audit_event_id}`

Purpose-specific topics, such as document intake and signed export generation,
remain available. Every outbox row now has a unique `deduplication_key`, and
claims expose it to consumers.

Delivery is at least once. Consumers must persist the deduplication key before
applying an external side effect, and acknowledge only after that side effect
is durable. PostgreSQL forbids changing an outbox event's identity, topic,
aggregate, payload, deduplication key, or creation time; terminal events remain
fully immutable. Leases, bounded retry, and dead-letter settlement remain
mutable only before terminal state.

## Body limits

Non-multipart API bodies use `MAX_API_REQUEST_BYTES`; multipart uploads use the
separate upload limit. The ASGI middleware counts actual streamed bytes, so
chunked transfer encoding cannot bypass the application limit. Production
ingress must enforce equal or stricter body, time, connection, and concurrency
limits independently.

## Remaining operational work

The generic signed connector dispatcher and durable inbox now preserve
qualified adapter identity, exact payload hashes, stable external
deduplication, signed receipts, immutable delivery attempts, lease/retry/dead
letter state, and generation-based controlled replay. The full contract is in
`docs/integration-delivery.md`.

Production still needs organization-specific connector bindings, credentials,
scheduler deployment, business handlers, monitoring, dead-letter ownership,
and replay drills. Repository success does not prove those external side
effects or make accepted inbound values verified.
