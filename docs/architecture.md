# Target architecture

## Architectural decision

The transactional core is a modular monolith. A microservice split at this
stage would weaken consistency across document revision, calculation snapshot,
approval, and audit records without providing a proven scaling benefit.
Compute-heavy and externally coupled work runs in isolated workers behind a
transactional outbox. Modules may later be extracted only after measured load
or organisational ownership justifies it.

## Trust boundaries

1. **Untrusted input** - archives, documents, spreadsheets, images, e-mail
   attachments, supplier files, and model output.
2. **Evidence zone** - immutable originals, content hashes, parser results,
   visual extraction results, evidence locations, and conflicts.
3. **Controlled domain zone** - approved document revisions, BoQ, canonical
   nomenclature, normalized quotes, methodology, rules, calculations, and
   approvals.
4. **Release zone** - immutable calculation snapshot and signed release
   decision.
5. **External systems** - OIDC, normative estimating engine, ERP, procurement,
   market data, supplier/RFQ, document management, and BI.

Nothing crosses a boundary merely because a model reports high confidence.

## Modules

- `identity`: OIDC validation, roles, project access, segregation of duties.
- `intake`: archive expansion, manifest, file health, Excel visibility/formula
  checks, document revision, referenced-document discovery.
- `evidence`: source locations, extraction runs, independent observations,
  conflicts, mandatory conflict-review tasks, manual corrections, and
  verification status.
- `document_graph`: revisions, supersession, dependencies, references, and
  current-set resolution.
- `project_passport`: object, address, dates, region, scope, constraints, and
  contractual facts as immutable current/superseded revisions with provenance.
- `boq`: revisioned/current WBS lines, revisioned quantities, formula inputs,
  alternatives, and an explicit cost-component plan that the calculation must
  cover exactly.
- `scope`: typology/rule packs, dependency graph, companion-work checks, and
  immutable evaluation attestations over a signed BoQ/rule input set.
- `nomenclature`: canonical items, critical attributes, match class, approved
  analogue proposals, four-eyes decisions, and catalog/equivalence versions.
- `normative`: adapter to an approved estimating engine; no synthetic fallback.
- `pricing`: quote normalization, commercial basis, source reliability,
  triangulation, governed price decisions, RFQ, spread review, and expiry.
- `contract`: revisioned evidence-backed terms, completeness rules, conflict
  checks, and approved cost impacts bound to contract/finance components.
- `risk`: revisioned evidence-backed risks and deterministic, model-versioned
  reserve calculations bound to explicit risk components.
- `calculation`: deterministic cost build-up for labour, plant, material,
  subcontract, logistics, mobilisation, finance, contract, risk, and margin.
- `validation`: independent recalculation, completeness, double-counting,
  rounding, currency, VAT, and scenario checks.
- `scenario`: policy-owned scenario definitions applied only to a verified
  fixed snapshot, with an independent recalculation and persisted result.
- `workflow`: formal states, hard stops, expert tasks, four-eyes approval, and
  release.
- `audit`: append-only hash-chained events and immutable snapshots.
- `actuals`: forecast-to-actual comparisons, reason taxonomy, and approved
  calibration examples; predictions are never recycled as facts.
- `integration`: deterministic signed snapshot/audit packages, transactional
  outbox, external delivery adapters, imports, and connector health.

## Deployment

Production deployment requires:

- PostgreSQL with point-in-time recovery, encryption, row-level access policy
  where tenancy demands it, and a dedicated append-only audit writer role;
- versioned S3-compatible storage with object lock/WORM for originals and
  released snapshots;
- at least two API replicas and isolated worker pools;
- a transactional outbox and idempotent consumers;
- OIDC with MFA enforced by the identity provider;
- central logs, metrics, traces, security audit export, alerting, and time
  synchronization;
- audit and Ed25519 export-signing keys from a secrets manager, never
  environment files committed to source control, plus an independently
  published historical public-key registry;
- tested backup restoration and documented RPO/RTO.

## Data invariants

- Money uses `Decimal`; floating point is forbidden.
- Currency, VAT basis, unit, rounding, and effective date are explicit.
- Original observations are immutable. Corrections create new observations.
- Every current value points to evidence or an approved assumption.
- Current passport, quantity, contract, risk, actual, nomenclature, and price
  records supersede prior revisions; upstream changes never overwrite history.
- A newer current document revision clears the confirmed set, moves the
  project to `BLOCKED`, invalidates current derived attestations, and requires
  new BoQ/evidence revisions before calculation can proceed.
- Every verified BoQ line declares its expected cost components. Calculation
  inputs must match that set exactly by line and semantic key: no missing,
  duplicate, or unplanned components are accepted.
- Quantity, unit, WBS, category, basis type, rate, currency, and source identity
  are reproduced from current governed records before calculation.
- A stage-gate attestation is valid only for the exact signed input set it
  evaluated; changed BoQ/rules/models make it stale.
- A released calculation points to immutable approved versions of all rule,
  catalog, methodology, rate, FX, and model inputs.
- Release rereads and hashes the snapshot object, recomputes its input/output
  and snapshot hashes, and compares its document-set and controlled-version
  set with the project's current bindings.
- A release export is generated only for an allowed release decision and the
  exact fixed snapshot/template version. Its Ed25519-signed manifest covers
  every mandatory content section by SHA-256; the immutable artifact row and
  content-addressed object retain key ID and public-key fingerprint.
- Price decisions and risk calculations preserve their contributing evidence
  graph. The lineage resolver walks recursively to immutable document
  revisions and locators and rejects cycles or missing nodes.
- Actuals enter calibration only after verified source reproduction, variance
  classification, and a separate methodology-owner approval.
- State transitions and critical reads/writes carry actor, request, reason, and
  prior/new values in audit events.
- `BLOCKED` is a first-class state and cannot be bypassed through API or direct
  workflow transition.
