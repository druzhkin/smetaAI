# Distributed request-rate control

## Safety boundary

TenderGuard uses PostgreSQL as the authoritative quota store for authenticated
API traffic. This is an application defence, not an ingress firewall. It
cannot protect capacity already spent on TLS termination, sockets,
unauthenticated identity validation, or bytes accepted by an upstream proxy.

Staging and production readiness therefore requires both:

1. configured application quotas described here; and
2. independently deployed ingress connection, timeout, header/body,
   unauthenticated-source, and upload-concurrency controls.

Neither layer is evidence that the other exists.

## Governed policy

The deployment must explicitly configure:

- a fixed-window duration;
- actor and organisation limits for `READ`;
- actor and organisation limits for `MUTATION`;
- actor and organisation limits for `UPLOAD`;
- a versioned HMAC identity-key ID and secret of at least 32 bytes.

There are no numeric software defaults. Capacity and security owners must
approve the values from representative traffic, load, abuse, multipart and
soak evidence. The complete policy is content-hashed into every bucket update.
Different policies on live instances inside one window fail closed.

`UPLOAD` includes multipart requests and document registration uploads.
Other `POST`, `PUT`, `PATCH`, and `DELETE` requests are `MUTATION`; remaining
methods are `READ`.

## Atomic consumption

After authentication and before business processing, the API derives keyed
HMAC identities for:

- the organisation and actor pair; and
- the organisation.

Raw identity values are not stored in quota rows. Both buckets are incremented
inside one short database transaction. The transaction is independent of the
idempotency/business transaction, so invalid requests, conflicts, hard stops,
and rolled-back mutations still consume quota.

The fixed-window update is a single database upsert. Concurrent requests
cannot undercount. A request is allowed only when both post-increment counts
are within policy. Denied requests continue to increment the buckets, avoiding
a boundary where rejected traffic is free.

PostgreSQL guards reject identity changes, deletion, decrements, skipped
increments, same-window policy mutation, backwards windows, and a later-window
reset to any value other than one.

## Responses and failure modes

An exhausted actor or organisation bucket returns `429` with:

- `RateLimit-Limit`;
- `RateLimit-Remaining`;
- `RateLimit-Reset`;
- `X-RateLimit-Category`;
- `Retry-After`.

An unavailable quota database, unsupported runtime database, policy mismatch,
or non-reproducible result returns `503`. The API never silently processes a
request without the configured quota.

## Operations and rotation

Deploy a policy change atomically to every application instance at a fixed
window boundary. Rotate the HMAC key ID and secret at the same boundary; a key
rotation creates new pseudonymous identities and therefore new buckets.
Preserve the approved change record without storing the secret in application
evidence.

Monitor `429` by organisation/category, any quota `503`, policy mismatch,
bucket-store latency, connection-pool saturation, organisation saturation and
actor-identity churn. Bucket rows are operational state, not calculation
evidence; do not edit or delete them to recover capacity.

Production qualification must demonstrate:

- exact concurrent-limit enforcement across multiple instances;
- no undercount during database contention;
- fail-closed behavior during quota-store loss and mixed policy deployment;
- ingress enforcement before application authentication/body parsing;
- bounded concurrent upload spooling and parser dispatch;
- recovery without direct bucket mutation;
- owner-approved limits under representative peak and abuse workloads.
