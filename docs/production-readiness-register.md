# Production readiness register

This register is deliberately fail-closed. `OPEN` items prevent autonomous bid
release even when repository tests pass.

| Gate | Current evidence | Status | Required owner/evidence |
|---|---|---|---|
| Full end-to-end controlled calculation | intake through passport, BoQ/quantity, scope, nomenclature/pricing/RFQ, typed logistics/mobilisation/contract finance, contract, risk, calculation, controlled scenario execution, independent snapshot/release, signed audit export, actual comparison and calibration approval are application-connected; a signed generic delivery/inbox boundary exists; qualified external extraction/normative/market/route/treasury/business adapters and scenario business qualification remain open | **PARTIAL** |
| Reproducibility | canonical Decimal inputs, version binding, content hashes, immutable snapshot | CORE IMPLEMENTED |
| Independent machine validation | separate recalculation path and mismatch/double-count tests | CORE IMPLEMENTED |
| Expert/four-eyes controls | version-bound tasks cover critical changes, analogues, price spread, contract impacts, reserves and calibration; organisation policy qualification absent | **PARTIAL** |
| No unexplained sums | every calculation input must exactly cover a planned BoQ component and reproduce current quantity/category/basis/source/rate/currency/unit; qualified normative and external sources remain absent | **PARTIAL** |
| No unregistered manual changes | immutable/revisioned observations, passport, quantities, contract, risk and actuals plus audited approval decisions; governed UI supports exact-manifest four-eyes document-set confirmation, source-level conflict resolution, and version-bound, evidence-checked, idempotent expert decisions; generic decisions cannot bypass dedicated conflict resolution, but other controlled mutation surfaces and complete UI/process qualification are absent | **PARTIAL** |
| Dangerous case blocking | negative tests cover new-revision invalidation, stale/tampered snapshots, controlled-version drift, conflict resolution, evidence independence, stale attestations, scope/quantity, price/RFQ, contract, risk, component coverage, invented sources, arithmetic and release policy; golden historical set absent | **PARTIAL** |
| Historical accuracy | no approved historical dataset or measured metrics supplied | **OPEN** |
| Blind estimator comparison | no completed blind comparison | **OPEN** |
| Parallel operation | not started | **OPEN** |
| Normative engine | interface exists; no licensed qualified adapter configured | **OPEN** |
| Market/RFQ connectors | interfaces exist; credentials/source agreements not supplied | **OPEN** |
| OCR + independent visual extraction | interfaces/reconciliation exist; qualified providers absent | **OPEN** |
| ERP/DMS/BI integrations | signed Ed25519 envelope/receipt contracts, HTTPS allowlisted adapter, qualification-bound organization/topic/key identity, immutable inbox and delivery evidence, dedupe, lease/retry/dead-letter and controlled replay are implemented and tested; real ERP/DMS/BI mappings, endpoints, credentials, schedulers, monitoring and qualification evidence are absent | **PARTIAL - PRODUCTION BLOCKER** |
| Detailed logistics/mobilisation | typed capacity-constrained transport, handling, storage, ancillary and mobilisation models; exact observation-value binding; controlled completeness/rounding; independent recalculation; mandatory four-eyes; immutable PostgreSQL revisions; BoQ/calculation/lineage integration are implemented and tested; real route/rate feeds and business qualification are absent | **PARTIAL** |
| Detailed contract finance | dated signed cash flows, piecewise funding rates, guarantee fees, day-count policy, independent day-by-day recalculation, required contract-term lineage, derived contract-impact and snapshot integration are implemented and tested; treasury feeds, indexation/FX policy data and business qualification are absent | **PARTIAL** |
| Operational scenario modelling | approved-policy scenarios execute from fixed snapshots, persist results, and independently recalculate; portfolio comparison UI and business qualification remain absent | **PARTIAL** |
| Operator and reviewer UI | production-built OIDC PKCE interface exposes role-filtered portfolio, work queue, project hard stops, indicators, record lineage and audit history; controlled project registration, quarantined upload/receipt polling, exact-manifest document-set confirmation, source-level conflict resolution, and expert decisions use explicit attestations and stable idempotency; document-set confirmation rejects the creator and stale candidates without treating the selected version as proof of completeness; conflict resolution exposes exact values/methods/locations/hashes, qualification identities, independence domains and commercial basis, revalidates qualifications at decision time, rejects source and task creators, dual-locks conflict/task versions, derives verified lineage, and cannot be bypassed by generic approval; decisions are optimistic-lock/version-bound, evidence-checked and four-eyes controlled; upload UI preserves quarantine/evidence separation and server limits; same-origin navigation, exact decimal rendering, security headers, desktop/mobile browser QA, frontend tests and container build are implemented; extraction correction, BoQ/pricing/calculation/release actions, accessibility evidence, role-based UAT and business qualification remain absent | **PARTIAL** |
| Export packages | released fixed snapshots produce deterministic content-addressed Ed25519-signed JSON packages with lineage, versions, approvals, workflow, release decision and verified audit chain; generic signed delivery exists, but an approved export receiver mapping, public-key trust registry and business qualification are absent | **PARTIAL** |
| Mutation reliability/outbox | all mutating APIs atomically persist actor-scoped idempotency, business writes, audit, universal outbox and replay response; signed outbound delivery preserves an external dedupe key and signed receipt; inbound messages are immutable and processed through leased generation records; retries, collision rejection, dead letters and replay are tested; production scheduler, real downstream inbox conformance, alerts and drills remain absent | **PARTIAL - PRODUCTION BLOCKER** |
| Load test | no representative document corpus/workload/SLO approved | **OPEN** |
| Malware scanning/sandbox | streamed separate quarantine, immutable qualification-bound exact-hash scan results, bounded archive streams, leased outbox dispatch, bounded retry/dead-letter/audited replay, and short-transaction worker parsing are implemented and tested; real scanner provider, production scheduler, disposable runtime limits, network policy and qualification evidence are absent | **PARTIAL - PRODUCTION BLOCKER** |
| Project access/information barriers | versioned owner-managed membership, action-specific project roles, same-tenant non-disclosure, qualified service identities, audited revocation, immutable PostgreSQL history, role-set/mask consistency guards, and authorization-before-pagination portfolio/work-queue queries are implemented and migration-tested on SQLite and PostgreSQL; IdP lifecycle, periodic recertification, break-glass recovery and optional RLS evidence are absent | **PARTIAL** |
| Audit integrity/WORM | versioned HMAC keys, verified legacy migration, immutable global checkpoints, four-eyes Ed25519 external receipts, full current-chain/anchored-terminal verification, and live WORM readiness enforcement are implemented and negatively tested; production provider, real bucket-policy evidence, schedules, alerts and tamper drill are absent | **PARTIAL - PRODUCTION BLOCKER** |
| Security review | code review completed; residual findings remain below | **OPEN** |
| Backup and point-in-time recovery | runbook drafted; restore evidence absent | **OPEN** |
| Disaster recovery | target RPO/RTO and alternate environment not approved | **OPEN** |
| Methodology approval | software workflow exists; organisation has not approved content | **OPEN** |
| Catalog/rule/model owners | roles exist; named people not supplied | **OPEN** |
| User training | materials and completion records absent | **OPEN** |
| Operating regulations | draft runbook exists; formal approval absent | **OPEN** |

## Consequence

The repository must not be labelled production-ready and the
`production_qualification` controlled version must not be approved until every
required gate has named evidence, date, environment, reviewer, and owner. The
release policy also requires a qualified normative adapter and an approved
production qualification version.
