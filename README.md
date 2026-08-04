# TenderGuard

TenderGuard is a fail-closed control plane for industrial tender costing. It is
designed for calculations that influence bid/no-bid decisions, bid price,
profit, procurement budgets, and contractual obligations.

It is deliberately **not** an autonomous price generator. The system assembles
evidence, detects conflicts and omissions, runs deterministic calculations,
performs an independent recalculation, enforces four-eyes approval, and blocks
release when the evidence is insufficient.

## Safety contract

`APPROVED_FOR_BID` is impossible unless all mandatory release gates pass:

- the current tender-document revision is fixed and complete;
- critical quantities are verified and conflicts are resolved;
- every cost has a source or an explicit approved assumption;
- technical analogues and price commercial bases are verified;
- the independent calculation matches within the approved rounding tolerance;
- methodology, catalog, rules, models, and thresholds have approved versions;
- required expert approvals satisfy segregation of duties;
- a content-addressed calculation snapshot is fixed.

The normative estimating adapter fails closed when no approved external
estimating engine or formally approved complete normative basis is configured.

## Architecture

The target architecture is a modular monolith for transactional consistency,
plus isolated workers and external adapters:

- FastAPI application/API;
- PostgreSQL as the system of record;
- S3-compatible immutable object storage for original documents and snapshots;
- OIDC authentication and role/attribute-based authorization;
- deterministic domain engines for intake, reconciliation, BoQ, scope,
  quantity, nomenclature, pricing, contract risk, calculation, and release;
- append-only tamper-evident audit events;
- independent calculation validator that does not consume main-engine totals;
- outbox-driven workers for OCR, visual extraction, connectors, and exports.

The Dockerfile has separate `api` and `document-worker` targets. The API image
does not install PDF, Excel, or image parser libraries; only the worker target
does.

See [architecture](docs/architecture.md), [safety case](docs/safety-case.md),
[requirements traceability](docs/requirements-traceability.md),
[governed commercial cost models](docs/commercial-cost-models.md), the
[controlled calculation and release contract](docs/calculation-release-control.md),
and the [signed export package specification](docs/signed-export-package.md).
The [controlled spreadsheet contract](docs/boq-spreadsheet-contract.md)
defines the qualified/profile-bound XLSX row import, independent identity and
quantity review, fail-closed initial attachment, and the still-unimplemented
evidence-preserving export boundary. The
[signed integration contract](docs/integration-delivery.md) defines the
transport trust boundary. The [operator UI contract](docs/operator-ui.md)
defines browser authentication, information barriers, deployment, and the
current limits of the human workflow. The
[controlled BoQ and nomenclature workflow](docs/boq-nomenclature-workflows.md)
defines operation-time document-set, evidence, controlled-version, review, and
analogue invariants. The
[governed project-passport workflow](docs/project-passport-workflow.md)
defines the evidence, independence, four-eyes, supersession, and stage-gate
contract for project facts.

## Implemented application core

The repository currently connects and integration-tests:

- document intake, revision-set confirmation, qualified extraction evidence,
  reconciliation, review-task-backed conflict resolution, and source lineage;
- owner-managed versioned project ACLs with action-specific roles,
  same-organisation information barriers, audited revocation, role-filtered
  pagination at the SQL boundary, and qualified machine-service capabilities;
- streamed, separately stored quarantine uploads, qualification-bound malware
  results, durable outbox leases/retries/dead letters, controlled replay, and a
  worker-only bounded document/archive parser entry point with short database
  transactions;
- fail-closed invalidation of derived data when a newer current document
  revision appears;
- revisioned project-passport facts selected only from exact current-set
  observations, policy-declared required/independent fields, qualified
  independence leaves, dedicated stale-safe four-eyes decisions, auditable
  correction/supersession, late-conflict blocking, and a no-manual-value
  operator workflow; BoQ lines, quantity verification, planned cost
  components, and attested scope-completeness evaluations;
- deterministic literal catalog-candidate retrieval with disclosed matched
  terms and no equivalence/confidence claim; exact source-item-bound
  critical-attribute nomenclature matching; governed analogues,
  policy-versioned quote normalization with deterministic integrity replay,
  commercially complete/independent source triangulation, price decisions,
  and RFQ state;
- revisioned contract terms and approved contract-cost impacts;
- governed capacity-based logistics, component-based mobilisation, and dated
  contract-finance models with exact observation-value binding, independent
  recalculation, mandatory four-eyes approval, and derived BoQ inputs;
- a governed risk register built only from current server evidence, dedicated
  four-eyes review, immutable supersession, model-bound deterministic reserve,
  separately coded independent recalculation, fail-closed correlation handling,
  and binding to one explicit BoQ cost component;
- deterministic primary calculation from atomic inputs, a separate
  recalculation path, immutable snapshots, hard stops, four-eyes approvals,
  recursive source-to-document lineage, and integrity checks against the
  current document set and controlled-version bindings;
- approved-policy scenario calculation from a fixed snapshot, including a
  separate independent recalculation for each scenario, replay-verified
  persisted comparisons, and a controlled operator screen that submits no
  financial override values;
- deterministic Ed25519-signed estimate/audit export packages containing the
  fixed snapshot, recursive lineage, controlled versions, approvals, workflow,
  release decision, and verified project audit chain;
- versioned audit HMAC keys, fail-closed legacy migration, content-addressed
  global checkpoints, four-eyes external Ed25519 receipts, and readiness that
  re-verifies current history plus the live evidence-bucket WORM policy;
- persisted actor-scoped idempotency for every mutating API operation and a
  deduplicated transactional `audit.event.recorded` outbox stream;
- atomic PostgreSQL-backed fixed-window quotas for authenticated actors and
  organisations, with separately governed read/mutation/upload limits,
  pseudonymous keyed identities, fail-closed policy consistency, explicit
  `429`/`503` behavior, immutable bucket identities, and mandatory production
  configuration without invented thresholds;
- qualification-bound Ed25519 outbound envelopes and exact signed receipts,
  immutable delivery attempts, a durable signed inbox with collision-safe
  deduplication, leased processing, bounded retry/dead-letter, and controlled
  replay; transport acceptance deliberately does not verify imported business
  data;
- a production-built React operator read interface with OIDC Authorization
  Code + PKCE, role-filtered portfolio/task/workbench/record views, visible
  hard stops, exact monetary rendering, safe same-origin navigation, restrictive
  browser security headers, and fail-closed production asset configuration;
- a controlled expert-decision surface that binds the exact task revision,
  validates project-scoped evidence, enforces four-eyes eligibility, requires
  explicit high-impact confirmation, persists idempotently, and refreshes the
  immutable decision/audit history;
- controlled project registration and document-intake surfaces with stable
  idempotency keys, server-aligned byte limits, strict identifier validation,
  explicit operator attestations, separate quarantine receipts, and status
  polling that never represents an unscanned upload as evidence;
- independent document-set confirmation that exposes the exact manifest and
  revision identifiers, rejects self-confirmation and stale candidates, and
  does not confuse version selection with document completeness;
- dedicated conflict resolution that compares full source observations and
  locations, rejects the source author and conflict-task creator, binds both
  optimistic versions, preserves a validated normalized commercial basis, and
  creates a separately verified derived observation;
- governed manual extraction correction that accepts only a revision from the
  current independently confirmed document set, binds an approved
  `manual_evidence_policy`, preserves the exact reason/value/unit/locator and
  object SHA-256, rejects floating-point values recursively, and creates an
  immutable `UNVERIFIED` source plus a dedicated four-eyes task. Approval
  creates a separate lineage-linked `VERIFIED` observation; the generic
  approval API cannot bypass this workflow;
- controlled operator workflows for qualified independent reconciliation,
  current-document-bound BoQ authoring, stale-safe four-eyes line review,
  scope completeness execution, deterministic catalog matching, and governed
  analogue proposal/finalization. The browser submits source identities and
  attestations, never a merged evidence value or nomenclature conclusion;
  backend operations reproduce the confirmed manifest and its latest signed
  confirmation event, plus complete controlled-version approval and project
  binding audit chains, and reject otherwise verified evidence from an older
  set;
- governed BoQ quantity correction that binds immutable before/after states to
  the exact current quantity, confirmed document set, approved rules and
  policy, and project-scoped observations; critical changes require an
  independent policy-assigned approval, while only the original author may
  apply the server-held after-state and every accepted application remains
  linked to its approval and resulting quantity;
- governed spreadsheet quantity intake in which identity and quantity receive
  separate four-eyes decisions, every derived manual observation is fully
  replayed at use time, and the browser can attach only the exact server-held
  value to a verified non-critical line. Critical single-source quantities,
  policy gaps, drift and ambiguity remain `BLOCKED`;
- governed price-source registration, normalization, and triangulation in the
  operator UI: the browser submits only a verified observation identifier,
  selects references from the bound approved price policy, and never
  resubmits or calculates the monetary value. The server replays the exact
  quote and normalization, applies policy-explicit decimal rounding, and opens
  RFQ/expert review when evidence is insufficient; verified price decisions
  are replayed again before calculation, while PostgreSQL guards protect quote
  inputs, normalized prices, and decision history from in-place mutation;
- governed FGIS CS source acquisition that binds an exact KSR-code request to
  the current project version, confirmed document set, approved acquisition
  policy, qualified SYSTEM adapter and verified nomenclature evidence. Every
  accepted HTTP body and the canonical manifest are retained by SHA-256 and
  replayed before use; the operator UI shows the BoQ and official source names
  side by side, while the result remains `UNVERIFIED` and `BLOCKED` and never
  creates a price quote automatically;
- hash-bound diagnostic research for an entire extracted XLSX BoQ: every row
  is explicitly classified, material rows issue bounded FGIS CS KSR searches,
  and the atomic output package retains every raw JSON response by SHA-256 plus
  the source and official names side by side. Retrieval remains `UNVERIFIED`;
  it neither approves a mapping nor supplies a price;
- hash-bound batch FGIS CS history research for the exact KSR candidates
  retained by that BoQ package. Subject, price zone and all portal-listed
  periods are fetched once, the complete code-by-period grid is retained and
  replayed from raw HTTP bodies, and HTTP status/media type remain in the
  manifest. A portal-level `400`, `404` or `422` blocks that exact observation;
  authentication, throttling, transport and server failures abort publication.
  Published fields remain diagnostic evidence and never become a bid price;
- hash-bound public-market research for every material BoQ row. An explicit
  profile either selects exact HTTPS product/catalog pages or records that no
  usable public page was found. The collector pins DNS-resolved public
  addresses, refuses redirects and private networks, accepts bounded HTML only,
  and extracts decimal offers solely from scoped Schema.org JSON-LD or
  microdata. Original pages, HTTP metadata and SHA-256 identities are retained
  and replayed without network access. Search snippets, visible price-like
  prose, ambiguous multi-offer markup and missing commercial terms never become
  price evidence; every result remains diagnostic and blocked for bid use;
- deterministic post-research literal assessment that preserves the exact BoQ
  and seller names, extracts only explicit dimensions, measurements, classes
  and designations with source offsets, reports missing and variant literals,
  and never treats text overlap as technical equivalence. Every retained offer
  also receives an explicit list of missing commercial terms. The assessment
  replays the complete raw market package before publication, and a separate
  deterministic Word report exposes all raw amounts, exact page URLs and hard
  stops while stating that no normalized price or project estimate exists;
- a fail-closed post-row BoQ pricing matrix that places the exact BoQ/TZ,
  catalog, won-tender, FGIS CS and market source names side by side, preserves
  direct source/document locators and commercial bases, and withholds the
  proposed amount unless source passports, all three required source groups,
  nomenclature, policy, deterministic decision replay and approvals pass;
- a fail-closed analysis-package worker that writes a new XLSX matrix, a short
  DOCX business report and a hash manifest from the governed matrix or a
  diagnostic extraction. Blocked rows expose no proposed price or line total,
  the project total remains blank without a fixed released snapshot, and the
  XLSX contains no formulas, hidden sheets or external links. Complete packages
  are staged before publication and deterministic OOXML bytes are covered by
  the manifest. Transactional
  outbox consumption, governed artifact storage and production qualification
  remain outstanding;
- governed calculation and release surfaces: the browser displays a
  server-generated candidate and submits only its hash, project version and
  reason; the server rebuilds all atomic inputs and policy before fixing the
  independently validated snapshot. Separate internal/bid release decisions
  expose every hard stop and a context-bound gate hash; the approver must
  attest the exact project and target, while PostgreSQL prevents in-place
  mutation of calculation runs, atomic inputs, scenarios and release records;
- one final expert-review surface after the fixed calculation snapshot: an
  expert can accept through the existing hash-bound release gate or select
  exact current BoQ price rows/release findings and return them to the earliest
  deterministic processing stage. The immutable rework request retains the
  snapshot, gate hash, row/finding identifiers and reason, while an outbox
  event provides the automation-worker handoff. A qualification-bound,
  lease/retry/dead-letter dispatcher now validates that immutable request and
  creates exactly one immutable stage command; the UI shows dispatch state
  without representing queue acceptance as completed rework;
- revisioned verified actuals, forecast-to-actual variance classification, and
  methodology-owner approval before facts become calibration examples; the
  governed operator UI selects only server-held evidence and released
  forecasts, loads actual/variance/calibration history and per-actual forecast
  candidates through bounded policy/project-scoped cursors, submits no factual
  value or variance arithmetic, and exposes exact roles, provenance, optimistic
  versions and four-eyes/integrity blockers. Forecast comparison replays the
  exact selected release decision and its fixed snapshot;
- governed historical/blind/parallel business qualification with a
  closed-population dataset, pre-reference forecast locking, two-step
  professional evidence, exact unrounded metrics, independent material
  discrepancy review, PostgreSQL append-only guards, and a package whose
  campaign and audit chain are revalidated before production qualification can
  satisfy release.
- a governed registry for every remaining production-readiness gate, with
  build/environment-bound profiles, content-addressed retained artifacts,
  Ed25519 external attestations, mandatory internal load/recovery results,
  four-eyes approval, expiry/revocation, PostgreSQL append-only guards, and
  live revalidation during controlled-version approval and bid release.
- universal governed-version integrity: PostgreSQL accepts only draft
  creation and the exact four-eyes approval transition, while binding,
  profile loading, calculation and release reproduce content hashes,
  organisation ownership, actor roles and the complete signed audit chain.
- PostgreSQL approval-evidence integrity: task identity and scope are
  immutable, only explicit decision or auditable document supersession
  or entity-replacement transitions are accepted, approval records are
  append-only and unique per task, and deferred constraints require the
  terminal task status to agree with its immutable decision record.

These are application workflows, not a claim that the complete target system
or its external operational environment has been delivered.

## Local development

```powershell
uv sync --extra dev --no-editable --reinstall-package tenderguard
docker compose up -d postgres minio
uv run alembic upgrade head
uv run uvicorn tenderguard.api.main:app --reload
uv run pytest
```

Run one bounded batch of final-review dispatches from the scheduler/worker
runtime after configuring and qualifying its dedicated SYSTEM identity:

```powershell
uv run tenderguard dispatch-final-rework --max-events 100
```

This command only validates and routes work. It does not claim that extraction,
pricing or recalculation has completed.

Run the operator UI development server in a second terminal:

```powershell
cd web
npm ci
npm run dev
```

The Vite development server proxies `/v1` to the API. For the container-equivalent
production bundle, run `npm run build`; FastAPI serves the resulting `web/dist`
assets in development or test. Staging and production startup fail if the UI is
enabled but no built assets or browser OIDC client are configured.

On Windows, `--no-editable` also avoids a Python 3.11 `.pth` locale problem
when the checkout path contains Cyrillic characters. Keep
`--reinstall-package tenderguard` after source changes so tests do not import a
stale non-editable wheel.

Development authentication is disabled by default. Set
`ALLOW_INSECURE_DEV_AUTH=true` only on an isolated workstation; production
startup rejects it.

`PUBLIC_DEMO_ENABLED=true` opens the checked-in Alabuga diagnostic workbench
without a login. The workbench publishes all 23 extracted BoQ names,
quantities, FGIS CS and market research candidates, raw observed amounts,
source links and blocker explanations. The snapshot is generated from the
hash-pinned diagnostic packages with
`python scripts/build_public_diagnostic_snapshot.py`; the generator refuses to
publish an unreleased proposed price or a non-HTTPS research source.

The setting does not grant an API identity: protected reads, uploads, edits,
approvals, and releases remain authenticated and fail closed. Original source
archives and project mutation controls are not shipped in the public bundle.
Public demo and insecure development authentication cannot be enabled
together. Raw observed values are research data, not evidence that a bid price
is safe to release.

## Production status

This repository contains a substantial fail-closed application core, but it is
not the complete target system and is not proof of production acceptance. Live
bid release remains blocked until the organisation supplies and qualifies the
malware scanner and production scheduler/isolated worker runtime, normative
engine, OCR/visual
extraction and market/RFQ adapters, approved
methodology and financial thresholds, production identity and infrastructure,
an approved external audit-anchor provider and verified WORM policy, enterprise
integrations, a completed organisation-approved historical/blind/parallel
qualification campaign, operating procedures,
backup/disaster-recovery evidence, trained users, and named process owners.

Production route/rate/treasury feeds and qualification for the implemented
logistics/mobilisation/contract-finance models, organization-specific
ERP/DMS/BI/export endpoint bindings and handlers, deployed integration
schedulers/monitoring, specialist maintenance surfaces, role-based UAT,
accessibility evidence, and business qualification of the operator interface
remain to be completed. Controlled reconciliation, current-set BoQ
authoring/review, governed project-passport authoring/review, scope execution,
deterministic nomenclature assessment, analogue review, and governed contract
term/cost-impact authoring/review plus actual/variance/calibration review are
implemented;
that implementation is not a substitute for real provider, methodology,
catalog-owner, or user-acceptance evidence.
Application actor/organisation quotas are implemented, but independently
deployed ingress connection/body/time limits, unauthenticated-abuse controls,
upload concurrency limits, and representative abuse/soak evidence remain
production blockers.
External verification also requires approved out-of-band
signing-key registries and real endpoint conformance evidence. A successful
local test run or an empty qualification campaign cannot substitute for
representative historical, blind-comparison, parallel-operation, security,
load, and recovery evidence.

The current gate-by-gate status is maintained in the
[production readiness register](docs/production-readiness-register.md).
Approved-profile load and restore verification are specified in the
[operational qualification contract](docs/operational-qualification.md).
Historical, blind and parallel controls are specified in the
[business qualification contract](docs/business-qualification.md).
The non-business readiness evidence contract is specified in the
[production gate evidence registry](docs/production-gate-evidence.md).
The governed model/rule/catalog lifecycle is specified in
[controlled-version integrity](docs/controlled-version-integrity.md).
The policy-bound raw FGIS CS evidence workflow is specified in
[governed FGIS CS acquisition](docs/fgiscs-acquisition.md).
Open security findings are in
[the security review](security_best_practices_report.md).
The untrusted-file boundary is specified in the
[quarantined intake contract](docs/quarantined-intake.md).
Audit checkpoint, anchoring, WORM, and key-rotation controls are specified in
[the audit-integrity runbook](docs/audit-integrity.md).
Mutation retry and outbox delivery semantics are specified in
[the reliable-mutations contract](docs/reliable-mutations.md).
Signed transport, inbox, and replay semantics are specified in
[the integration-delivery contract](docs/integration-delivery.md).
Manual correction and four-eyes verification are specified in
[the governed manual-evidence contract](docs/manual-evidence-review.md).
The zero-touch processing boundary, final expert decision, and automatic
rework dispatch are specified in
[the final expert-review contract](docs/final-expert-review.md).
The acquisition limits and provenance requirements for sources that do not
require a commercial data subscription are documented in
[the free public data-source register](docs/free-public-data-sources.md).
Contract-term evidence, review, cost impact and independent stage gating are
specified in
[the governed contract-risk workflow](docs/contract-risk-workflow.md).
Risk evidence, four-eyes review, reserve calculation and independent stage
gating are specified in
[the governed risk-reserve workflow](docs/risk-reserve-workflow.md).
Runtime procedures are maintained in the
[operations runbook](docs/operations-runbook.md), with application quota
semantics in the
[distributed rate-limiting contract](docs/distributed-rate-limiting.md).
Methodology ownership and version governance are specified in
[the methodology-governance contract](docs/methodology-governance.md).
The dated baseline findings remain available in the
[internal audit report](docs/internal-audit-2026-07-23.md).
