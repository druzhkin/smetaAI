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
| 1-3 intake, versions, graph | intake, document graph | controlled project registration and quarantine-upload UI with explicit manifest `BLOCKED` outcome, streamed quarantine, qualified scan state machine, leased outbox retry/dead-letter/replay, short-transaction worker-only bounded manifest/revision processing, strict OOXML identity/XML-declaration checks, external-relationship classification, Excel formula/cell-error locators, exact-manifest read contract, and stale-safe four-eyes revision confirmation UI (`APPLICATION_SLICE`); external scanner/scheduler/sandbox (`PORT`/`OPERATIONS`); graph rules (`DOMAIN_CORE`) |
| 4 extraction | evidence + worker adapters | qualified immutable observations and independent reconciliation; bounded reconciliation UI exposes qualification/domain/source blockers and accepts only server-selected current-set observations plus the exact controlled rule version; governed manual correction binds an approved policy and current confirmed document revision, remains unverified until a dedicated four-eyes review creates a separate derived observation, with UI/API/audit/PostgreSQL controls (`APPLICATION_CORE`); OCR/visual providers (`PORT`) |
| 5 passport | project passport | approved requirements policy; exact current-document observations; qualified independent extraction leaves; revisioned immutable provenance; dedicated optimistic four-eyes decision; explicit rejected-revision supersession; unresolved-conflict, task/audit/value drift and missing-field stage blockers; no-manual-value operator UI (`APPLICATION_CORE`) |
| 6-8 BoQ, completeness, nomenclature | BoQ, scope, nomenclature | qualified/profile-bound XLSX row import; separate four-eyes identity and quantity proposals with complete derivation replay; no-client-value initial quantity attachment and critical single-source hard stop; current-document-bound BoQ authoring and stale-safe review UI; BoQ/quantity revisions; exact cost-component plan; scope-evaluation UI/attestations; validated catalog context; server-determined attribute matching and governed analogue review/finalization UI (`APPLICATION_CORE`, while production parser/profile qualification remains external) |
| 9-10 norms and normative cost | normative adapter | result validation (`DOMAIN_CORE`); licensed engine (`PORT`, `QUALIFICATION`) |
| 11-13 procurement, market, resources | pricing, calculation | governed normalization with explicit policy rounding, typed source passports, mandatory won-tender/FGIS CS/independent-market groups, source-side name and URI visibility, deterministic replay, a fail-closed post-row BoQ price matrix, price decisions, RFQ, exact atomic-input binding, and a no-manual-value operator workflow (`APPLICATION_CORE`); live sources/supplier exchange and provider qualification (`PORT`) |
| 14 logistics and mobilisation | calculation | governed capacity-constrained transport, handling, storage, ancillary and mobilisation models; canonical observation-value binding; controlled completeness/rounding; independent calculation; four-eyes; immutable derived BoQ input and lineage (`APPLICATION_CORE`); route/rate feeds and qualification (`PORT`, `QUALIFICATION`) |
| 15 contract and finance | contract, calculation | non-empty approved term policy with exact evidence-field mapping; confirmed-set direct/recursive evidence reproduction; dedicated optimistic four-eyes term decision; conflict, task, approval, audit and supersession checks; server-candidate zero/derived cost-impact UI; dated cash-flow, piecewise funding-rate, guarantee-fee and day-count model; independent daily/stage-gate recalculation and snapshot lineage (`APPLICATION_CORE`); treasury/indexation/FX feeds and qualification (`PORT`, `QUALIFICATION`) |
| 16-18 risk, bid price, scenarios | risk, calculation, scenario | current-document structured risk candidates with no browser-entered financial values, strict approved-model schema, dedicated optimistic four-eyes review, immutable supersession, fail-closed correlation handling, deterministic reserve plus separately coded replay and input/output hashes, independent stage-gate reconstruction and one exact reserve-to-BoQ binding; server-generated exact atomic-input candidate; fixed snapshot; hash-bound internal/bid release; approved-policy scenarios over replay-verified fixed snapshots with independent recalculation, persisted-result replay and a no-override-value comparison UI (`APPLICATION_CORE`); risk/scenario business qualification remains open |
| 19 independent verification | validation | separate recalculation, mismatch, explicit Decimal rounding/overflow and snapshot/run integrity controls (`APPLICATION_CORE`) |
| 20 expert approval | workflow | version-bound task planning, evidence-required decisions, dedicated conflict/manual-evidence workflows, four-eyes checks and exact gate-hash/project/target attestation at release; one final expert surface can return exact current price rows/findings through an immutable snapshot/hash-bound rework request without accepting a replacement price from the browser; a qualified leased dispatcher revalidates the request and creates one immutable stage command with retry/dead-letter/status evidence; approval, rework and dispatch records are append-only in PostgreSQL and task identity/scope is protected (`APPLICATION_CORE`); the four stage-command consumers and organisation policy qualification remain open |
| 21 audit | audit | versioned-key hash chains, verified legacy migration, immutable global checkpoints, four-eyes Ed25519 external receipts, full current-history verification, snapshot-to-document lineage, and live object-lock readiness enforcement (`APPLICATION_CORE`); production anchor provider, WORM evidence and drills (`QUALIFICATION`) |
| 22 export/integration | integration | deterministic Ed25519-signed estimate/audit package; persisted mutation idempotency; universal outbox; qualification-bound Ed25519 event/receipt contracts; immutable outbound attempts and inbound messages; leased retry/dead-letter processing; collision-safe deduplication and controlled replay (`APPLICATION_CORE`); organization-specific ERP/DMS/BI mappings, deployed workers/schedulers, endpoints, credentials, conformance/load evidence and external trust registry (`PORT`, `OPERATIONS`, `QUALIFICATION`) |
| 23-24 actuals and calibration | actuals + business qualification | revisioned evidence-derived facts, approved-policy metric/source rules, separate actual/variance/calibration four-eyes decisions, bounded scope-bound actual/variance/calibration and per-actual release-candidate cursors, exact selected-release fixed-snapshot replay, mandatory variance reasons, methodology-owner calibration approval, PostgreSQL task/entity/history guards, and a governed no-manual-value operator UI exposing exact roles, versions, lineage and decision blockers; governed closed historical/blind/parallel campaigns lock system forecasts before references, use exact unrounded metrics, require independent material-discrepancy review, and produce an immutable package revalidated by production release (`APPLICATION_CORE`/`APPLICATION_SLICE`); organisation dataset, professional comparisons, factual feeds and role-based UI qualification remain `QUALIFICATION`/`PORT` |

## Verification contours

All eleven contours map to a named module or explicit integration boundary.
The current API connects intake, qualified evidence/reconciliation and
review-task-backed conflict resolution with a dual-version,
source-provenance operator workflow, governed manual evidence correction and
independent review, passport,
BoQ/quantity, scope attestations, nomenclature/analogue decisions, governed
pricing/RFQ, contract terms and cost impacts, risk/register reserve,
calculation, independent validation, controlled scenario execution, expert
approval, lineage, audit, release, signed release export,
actuals, and calibration approval. Integration tests exercise positive and
negative paths through these workflows.

The historical/blind/parallel qualification mechanism is connected through its
API and PostgreSQL append-only guards. It prevents missing-reference
evaluation, self-review, result-hash tampering, rounded-threshold masking,
unreviewed material discrepancies, and an unbound production-qualification
claim. It is a qualification harness, not completed qualification evidence.

The remaining production-readiness gates are connected through immutable
evidence packages rather than declared hashes. The registry reopens retained
objects, validates exact internal load/recovery results or governed external
Ed25519 signatures, enforces four-eyes approval, and rejects expired, revoked,
tampered, missing or cross-build packages. Real organisation evidence remains
a `QUALIFICATION` responsibility.

This does not make every contour operationally qualified. OCR/visual
extraction, the recognised normative engine, live price/RFQ sources,
enterprise integrations, production logistics/finance feeds and qualification,
deployed/qualified sandboxing, operational IdP/access-governance procedures,
and production operations remain open. Versioned project ACLs and
action-specific object authorisation are application/database controls;
release policy consumes unresolved blocking findings rather than
informal text; no contour may silently downgrade a failure because another
contour passed.

## Acceptance and readiness

The acceptance criteria are represented in the production-readiness register.
Governed recovery/load profiles, immutable build binding, deterministic
snapshot replay, object/audit/export verification, exact unrounded SLO
decisions, and content-hashed result envelopes provide the repository-side
qualification mechanism (`APPLICATION_CORE`). Real workloads, backups,
alternate infrastructure, external telemetry, and independent decisions remain
`QUALIFICATION`. Gates remain open until backed by named evidence, owner, date,
environment, and approval. Repository tests are necessary but not sufficient
evidence.
