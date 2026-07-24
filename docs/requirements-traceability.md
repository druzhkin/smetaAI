# Requirements traceability

Status values:

- `APPLICATION_SLICE` - persisted and exercised through the current API flow.
- `APPLICATION_CORE` - persisted, stage-gated, and integration-tested as a
  production-oriented workflow, while external adapters or organisational
  qualification may still be open.
- `DOMAIN_CORE` - deterministic rules and data contracts are implemented and
  tested, but the complete operational workflow is not yet connected.
- `SCHEMA` - production-oriented persistence model exists without a complete
  application service.
- `PORT` - explicit integration contract; production connector/credentials are
  organisation-specific.
- `QUALIFICATION` - cannot be completed by code alone and requires operational
  evidence or formal approval.

| Scope | Target module | Delivery evidence |
|---|---|---|
| 1-3 intake, versions, graph | intake, document graph | streamed quarantine, qualified scan state machine, leased outbox retry/dead-letter/replay, short-transaction worker-only bounded manifest/revision processing and revision confirmation (`APPLICATION_SLICE`); external scanner/scheduler/sandbox (`PORT`/`OPERATIONS`); graph rules (`DOMAIN_CORE`) |
| 4 extraction | evidence + worker adapters | qualified immutable observations and independent reconciliation (`APPLICATION_SLICE`), OCR/visual providers (`PORT`) |
| 5 passport | project passport | revisioned provenance-backed facts, verification and stage blockers (`APPLICATION_CORE`) |
| 6-8 BoQ, completeness, nomenclature | BoQ, scope, nomenclature | BoQ/quantity revisions, exact cost-component plan, scope attestations, deterministic attribute matching and governed analogues (`APPLICATION_CORE`) |
| 9-10 norms and normative cost | normative adapter | result validation (`DOMAIN_CORE`); licensed engine (`PORT`, `QUALIFICATION`) |
| 11-13 procurement, market, resources | pricing, calculation | governed normalization, evidence classes, triangulation, price decisions, RFQ and exact atomic-input binding (`APPLICATION_CORE`); live sources/supplier exchange (`PORT`) |
| 14 logistics and mobilisation | calculation | explicit cost categories and exact component/evidence coverage (`APPLICATION_CORE`); detailed routing, mobilisation schedule and operational model remain open |
| 15 contract and finance | contract, calculation | revisioned terms, independent critical evidence, completeness findings and four-eyes cost impacts (`APPLICATION_CORE`); detailed cash-flow/guarantee model remains open |
| 16-18 risk, bid price, scenarios | risk, calculation, scenario | verified risk register, model-bound reserve and reserve-to-BoQ binding (`APPLICATION_CORE`); base bid calculation (`APPLICATION_SLICE`); approved-policy scenarios over fixed snapshots with independent recalculation (`APPLICATION_CORE`) |
| 19 independent verification | validation | separate recalculation, mismatch and snapshot controls (`APPLICATION_SLICE`) |
| 20 expert approval | workflow | version-bound task planning, evidence-required decisions and four-eyes checks (`APPLICATION_SLICE`); organisation policy qualification remains open |
| 21 audit | audit | versioned-key hash chains, verified legacy migration, immutable global checkpoints, four-eyes Ed25519 external receipts, full current-history verification, snapshot-to-document lineage, and live object-lock readiness enforcement (`APPLICATION_CORE`); production anchor provider, WORM evidence and drills (`QUALIFICATION`) |
| 22 export/integration | integration | deterministic Ed25519-signed estimate/audit package, persisted mutation idempotency, immutable metadata, verified download, unique consumer deduplication keys and universal transactional audit-event outbox (`APPLICATION_CORE`); enterprise delivery dispatchers/inboxes and external trust registry (`PORT`, `QUALIFICATION`) |
| 23-24 actuals and calibration | actuals | revisioned verified facts, fixed-snapshot comparison, mandatory variance reasons and methodology-owner calibration approval (`APPLICATION_CORE`); factual feed (`PORT`) |

## Verification contours

All eleven contours map to a named module or explicit integration boundary.
The current API connects intake, qualified evidence/reconciliation and
review-task-backed conflict resolution, passport,
BoQ/quantity, scope attestations, nomenclature/analogue decisions, governed
pricing/RFQ, contract terms and cost impacts, risk/register reserve,
calculation, independent validation, controlled scenario execution, expert
approval, lineage, audit, release, signed release export,
actuals, and calibration approval. Integration tests exercise positive and
negative paths through these workflows.

This does not make every contour operationally qualified. OCR/visual
extraction, the recognised normative engine, live price/RFQ sources,
enterprise integrations, detailed logistics/finance workflows,
deployed/qualified sandboxing, operational IdP/access-governance procedures,
and production operations remain open. Versioned project ACLs and
action-specific object authorisation are application/database controls;
release policy consumes unresolved blocking findings rather than
informal text; no contour may silently downgrade a failure because another
contour passed.

## Acceptance and readiness

The acceptance criteria are represented in the production-readiness register.
They remain open until backed by named evidence, owner, date, environment, and
approval. Repository tests are necessary but not sufficient evidence.
