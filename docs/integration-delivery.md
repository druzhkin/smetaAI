# Signed integration delivery and durable inbox

## Trust boundary

Integration transport proves who sent an exact payload and whether a qualified
remote endpoint durably acknowledged it. It does **not** prove that an imported
price, quantity, normative result, contract term, or actual is technically or
commercially correct.

An inbound message is stored in `integration_inbox_messages` and receives a
separate `integration_inbox_processings` record. Consumption may create an
`UNVERIFIED` domain record through the appropriate application service. Only
that domain's evidence, normalization, reconciliation, approval, and
stage-gate workflow may later produce `VERIFIED`. Transport status must never
be used as a verification shortcut.

## Wire contracts

Outbound events use `tenderguard.integration-event/v1` and include:

- immutable source message ID and aggregate ID;
- an internal topic and organization ID;
- an external delivery deduplication key;
- event occurrence and signing timestamps;
- canonical payload plus SHA-256;
- an Ed25519 key ID and signature over the complete event body.

The receiver returns `tenderguard.integration-receipt/v1` with the source
message ID, its own durable inbox ID, the same delivery deduplication key and
payload hash, receiver identity, `ACCEPTED` or `DUPLICATE`, timestamp, and an
Ed25519 signature. TenderGuard acknowledges the outbox row only after the
receipt reproduces the event and verifies against the public key in the active
adapter qualification.

`HttpsJsonIntegrationConnector` sends bounded JSON only to an explicitly
allowlisted HTTPS host. It does not follow redirects, embed URL credentials,
or accept an unbounded response. A bearer token is optional and is never
persisted in delivery evidence. Signed receipts remain mandatory even when TLS
and bearer authentication succeed.

## Outbound delivery lifecycle

1. A business transaction writes an immutable outbox event.
2. A dispatcher validates its active organization-scoped connector
   qualification and claims the event with `FOR UPDATE SKIP LOCKED`.
3. The transaction commits before network I/O.
4. The dispatcher signs and sends the exact canonical envelope.
5. In a new transaction it appends an immutable
   `connector_delivery_attempts` row and either:
   - records the signed receipt and acknowledges the outbox;
   - schedules bounded exponential retry; or
   - enters immutable dead letter for a permanent protocol/security failure or
     exhausted attempts.

A crash after remote acceptance but before local settlement is safe only when
the remote side persists the delivery deduplication key. The retry keeps that
same external key; a signed `DUPLICATE` receipt completes settlement.

An administrator may replay only a dead-lettered event with connector attempt
evidence. Replay creates a new internal outbox row and immutable
`outbox_replays` link while preserving the original external deduplication key
and exact payload. One terminal event can be replayed once; a failed replay
forms the next explicit link in the chain.

## Inbound lifecycle

The source must use a dedicated `SYSTEM` identity whose exact actor ID,
organization, topics, signing key, and method are present in an active adapter
qualification. Reception:

1. validates schema, organization, topic, key ID, Ed25519 signature, payload
   hash, timestamp age, and future clock skew;
2. rejects reuse of a message ID or deduplication key for different content;
3. atomically inserts the immutable envelope, qualification snapshot, signed
   TenderGuard receipt, generation-1 processing row, audit event, and outbox
   notification;
4. returns the stored signed receipt for an exact retry.

An inbox handler also uses a dedicated `SYSTEM` identity and a separate active
`INTEGRATION_INBOX_HANDLER` qualification. Claims have expiring ownership
tokens, bounded retry, and immutable consumed/dead-letter terminal states.
Acknowledgement requires a result reference and SHA-256 to the durable
downstream import. That reference is lineage for transport handling, not
business verification.

An administrator replays dead-lettered inbound work by creating the next
processing generation. The original envelope and every terminal generation
remain immutable.

## Qualification payload

All connector capabilities originate in a separately approved
`adapter_qualification` controlled version. Activation retains only public
trust metadata; endpoint secrets are runtime configuration.

Outbound delivery requires:

```json
{
  "supported_methods": ["INTEGRATION_OUTBOUND_DELIVERY"],
  "outbound_topics": ["audit.event.recorded"],
  "receipt_signing_key_id": "remote-receipt-key-2026-01",
  "receipt_public_key_b64": "<32-byte Ed25519 public key>",
  "receiver_id": "enterprise-event-ledger"
}
```

An inbound source requires:

```json
{
  "supported_methods": ["INTEGRATION_INBOUND_SOURCE"],
  "service_actor_id": "erp-source-worker",
  "inbound_topics": ["actual.acceptance.received"],
  "inbound_signing_key_id": "erp-event-key-2026-01",
  "inbound_signing_public_key_b64": "<32-byte Ed25519 public key>"
}
```

An inbox handler requires `INTEGRATION_INBOX_HANDLER`, its exact service actor,
and a non-empty `inbound_topics` allowlist. Key rotation creates and qualifies
a new version; it does not overwrite historical message evidence.

## Runtime configuration

Staging and production refuse startup without:

- `INTEGRATION_SIGNING_KEY_ID`;
- `INTEGRATION_SIGNING_PRIVATE_KEY_B64`;
- `INTEGRATION_RECEIVER_ID`;
- `INTEGRATION_OPERATOR_ORGANIZATION_ID`.

Lease, timeout, maximum attempts, retry bounds, event/response byte limits,
HTTP timeout, inbound age, and future-skew limits are explicit settings.
Connector endpoints, host allowlists, TLS/mTLS material, and authorization
secrets belong in the worker's secret/configuration system, not qualification
payloads or audit records.

Readiness validates the local signing key and at least one active outbound,
inbound-source, and inbox-handler qualification for the configured operator
organization. It cannot prove that an external endpoint is reachable or that a
scheduler is running; deployment health checks and alerts must prove those
facts separately.

## API

- `POST /v1/integrations/inbox`
- `POST /v1/integrations/inbox/claims`
- `POST /v1/integrations/inbox/processings/{id}/acknowledge`
- `POST /v1/integrations/inbox/processings/{id}/reject`
- `POST /v1/integrations/inbox/processings/{id}/replay`
- `GET /v1/integrations/inbox/{message_id}`
- `POST /v1/integrations/outbox/{event_id}/replay`

All mutations use the persisted HTTP idempotency contract in production.

## Still required before production

The repository does not supply organization-specific ERP, DMS, BI,
procurement/RFQ, market, route/rate, treasury, OCR, malware, or normative
endpoint bindings and credentials. It also does not deploy their schedulers,
dead-letter dashboards, paging, network policy, or vendor qualification
evidence. Each business handler needs contract tests against the real system,
failure drills, replay drills, load/soak results, data-owner approval, and a
demonstrated mapping into the correct unverified/domain workflow.
