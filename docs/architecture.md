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

- `identity`: OIDC validation, versioned project membership and scoped roles,
  owner-managed access, qualified service capabilities, and segregation of
  business duties from infrastructure administration.
- `intake`: separately stored streamed quarantine, qualification-bound malware
  results, leased outbox delivery, bounded retry/dead-letter/replay, worker-only
  bounded archive expansion, manifest, file health, Excel visibility/formula
  checks, document revision, and referenced-document discovery.
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
- `operator_ui`: OIDC Authorization Code + PKCE browser client, role-filtered
  portfolio/work-queue/workbench/record read models, visible release hard
  stops, exact-manifest four-eyes document-set confirmation, source-level
  conflict resolution with dual optimistic locks, safe same-origin navigation,
  and backend-enforced information barriers. The UI is never a policy or
  authorization boundary.
- `integration`: deterministic signed snapshot/audit packages; a transactional
  outbox with stable external delivery identity; qualification-bound Ed25519
  event/receipt envelopes; immutable delivery attempts and inbound messages;
  leased processing generations, collision-safe deduplication, dead letters,
  controlled replay, external adapters, and connector health.

## Deployment

Production deployment requires:

- PostgreSQL with point-in-time recovery, encryption, row-level access policy
  where tenancy demands it, and a dedicated append-only audit writer role;
- versioned S3-compatible storage with object lock/WORM for originals and
  released snapshots;
- at least two API replicas and isolated worker pools;
- separate API and document-worker images; parser libraries are absent from
  the API image;
- distinct quarantine and evidence buckets; parser workers run with network,
  CPU, memory, process, time, and temporary-disk limits;
- a transactional outbox and idempotent consumers; document-intake delivery
  uses `FOR UPDATE SKIP LOCKED`, expiring ownership tokens, bounded exponential
  retry, immutable terminal events, and explicit audited replay;
- isolated integration workers that validate active organization/topic/key
  qualifications, commit claims before network I/O, require an exact signed
  remote receipt, and settle in a new short transaction; inbound transport
  acceptance creates immutable evidence and an unverified processing task,
  never verified business data;
- mutating APIs use a persisted actor/organisation-scoped idempotency ledger;
  reservation, business state, audit, universal outbox event, and saved
  response commit atomically; all audit events publish a unique downstream
  deduplication key;
- typed logistics, mobilisation, and dated finance models bind formula values
  to current-document observations, use controlled Decimal rounding, execute
  independent capacity/day-by-day recalculation, require four-eyes approval,
  and expose only immutable `DERIVED_MODEL` BoQ bases to the main calculation;
- OIDC with MFA enforced by the identity provider;
- governed project-owner recovery, periodic membership recertification, and
  monitored break-glass access; PostgreSQL RLS where an independent database
  information barrier is required;
- central logs, metrics, traces, security audit export, alerting, and time
  synchronization;
- audit and Ed25519 export-signing keys from a secrets manager, never
  environment files committed to source control, plus an independently
  published historical public-key registry;
- integration signing keys, remote receipt/source trust keys, mTLS material,
  endpoint allowlists, and service identities under independent rotation and
  qualification procedures;
- tested backup restoration and documented RPO/RTO.
- immutable build identity exposed by runtime/readiness and bound by governed
  load/recovery profiles; qualification refuses a different environment or
  image before accepting evidence.

## Data invariants

- Money uses `Decimal`; floating point is forbidden.
- Organisation tenancy never implies project access. Human access requires the
  latest active membership revision and the exact project role accepted by the
  action. The immutable JSON role set must reproduce its relational bit mask;
  PostgreSQL rejects divergent revisions. Portfolio and work-queue queries join
  the latest membership and apply the exact project role before pagination.
  Revocation is a new revision; prior membership evidence is immutable.
- `SYSTEM` is never a project membership role. Machine access requires an
  explicit capability, active organisation-scoped adapter qualification, and
  an exact bound service identity; a service token cannot use generic project
  read routes.
- Currency, VAT basis, unit, rounding, and effective date are explicit.
- Original observations are immutable. Corrections create new observations.
- An original upload cannot enter the evidence zone or reach a parser before
  an exact qualified malware result is `CLEAN`; scan-result rows are immutable.
- A parser may finalize only with the exact current processing lease. Parsing
  and object promotion occur outside database transactions; claim, failure,
  and finalize use separate short transactions.
- Exhausted document processing enters `PROCESSING_DEAD_LETTERED`, leaves the
  input blocker unresolved, and requires an audited administrator replay that
  creates a new outbox event rather than editing terminal delivery evidence.
- External delivery acknowledges an outbox event only after an exact
  qualification-bound signed receipt. Retries preserve the external delivery
  key; replay creates new internal evidence without changing terminal history.
- Inbound signature/transport acceptance never implies technical, commercial,
  normative, or factual verification. Domain verification remains a separate
  evidence and approval workflow.
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
