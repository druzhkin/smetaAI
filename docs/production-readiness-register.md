# Production readiness register

This register is deliberately fail-closed. `OPEN` items prevent autonomous bid
release even when repository tests pass.

| Gate | Current evidence | Status | Required owner/evidence |
|---|---|---|---|
| Full end-to-end controlled calculation | intake through passport, BoQ/quantity, scope, nomenclature/pricing/RFQ, contract, risk, calculation, controlled scenario execution, independent snapshot/release, signed audit export, actual comparison and calibration approval are application-connected; qualified external extraction/normative/market adapters, detailed logistics/finance, scenario business qualification, and external delivery remain open | **PARTIAL** |
| Reproducibility | canonical Decimal inputs, version binding, content hashes, immutable snapshot | CORE IMPLEMENTED |
| Independent machine validation | separate recalculation path and mismatch/double-count tests | CORE IMPLEMENTED |
| Expert/four-eyes controls | version-bound tasks cover critical changes, analogues, price spread, contract impacts, reserves and calibration; organisation policy qualification absent | **PARTIAL** |
| No unexplained sums | every calculation input must exactly cover a planned BoQ component and reproduce current quantity/category/basis/source/rate/currency/unit; qualified normative and external sources remain absent | **PARTIAL** |
| No unregistered manual changes | immutable/revisioned observations, passport, quantities, contract, risk and actuals plus audited approval decisions; complete UI/process qualification absent | **PARTIAL** |
| Dangerous case blocking | negative tests cover new-revision invalidation, stale/tampered snapshots, controlled-version drift, conflict resolution, evidence independence, stale attestations, scope/quantity, price/RFQ, contract, risk, component coverage, invented sources, arithmetic and release policy; golden historical set absent | **PARTIAL** |
| Historical accuracy | no approved historical dataset or measured metrics supplied | **OPEN** |
| Blind estimator comparison | no completed blind comparison | **OPEN** |
| Parallel operation | not started | **OPEN** |
| Normative engine | interface exists; no licensed qualified adapter configured | **OPEN** |
| Market/RFQ connectors | interfaces exist; credentials/source agreements not supplied | **OPEN** |
| OCR + independent visual extraction | interfaces/reconciliation exist; qualified providers absent | **OPEN** |
| ERP/DMS/BI integrations | target ports/schema exist; endpoints and credentials absent | **OPEN** |
| Detailed logistics/mobilisation | generic governed cost components exist; routing, mobilisation schedule, site constraints and operational estimation service are not implemented | **OPEN** |
| Detailed contract finance | governed terms and cost impacts exist; cash-flow, retention, guarantee, credit and indexation calculation service is not implemented | **OPEN** |
| Operational scenario modelling | approved-policy scenarios execute from fixed snapshots, persist results, and independently recalculate; portfolio comparison UI and business qualification remain absent | **PARTIAL** |
| Export packages | released fixed snapshots produce deterministic content-addressed Ed25519-signed JSON packages with lineage, versions, approvals, workflow, release decision and verified audit chain; external delivery adapters, approved public-key distribution/trust registry and business qualification are absent | **PARTIAL** |
| Load test | no representative document corpus/workload/SLO approved | **OPEN** |
| Malware scanning/sandbox | streamed separate quarantine, immutable qualification-bound exact-hash scan results, bounded archive streams, leased outbox dispatch, bounded retry/dead-letter/audited replay, and short-transaction worker parsing are implemented and tested; real scanner provider, production scheduler, disposable runtime limits, network policy and qualification evidence are absent | **PARTIAL - PRODUCTION BLOCKER** |
| Project access/information barriers | versioned owner-managed membership, action-specific project roles, same-tenant non-disclosure, qualified service identities, audited revocation, and immutable PostgreSQL history are implemented; IdP lifecycle, periodic recertification, break-glass recovery and optional RLS evidence are absent | **PARTIAL** |
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
