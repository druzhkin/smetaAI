# Safety case

## Claim

TenderGuard may emit `APPROVED_FOR_BID` only when a reproducible calculation is
supported by a complete, traceable evidence set and independent controls.

This claim is not yet established for production. The repository implements
and integration-tests a broad transactional enforcement core, including the
governed BoQ, quantity, scope, nomenclature, pricing, contract, risk,
calculation, independent validation, approval, release, lineage, actuals, and
calibration workflows, plus signed estimate/audit export packages. External
adapters, production route/rate/treasury feeds, enterprise integrations, and
all operational qualification evidence remain open.

## Defence in depth

| Layer | Independent evidence | Failure response |
|---|---|---|
| Input integrity | archive manifest, hashes, file health, revision graph | block or `DOCUMENTS_INCOMPLETE` |
| Untrusted file boundary | separate quarantine, qualified exact-hash malware result, leased bounded-retry outbox, worker-only bounded parsing | no evidence promotion; retry or dead-letter with unresolved blocker; `BLOCKED`/`DOCUMENTS_INCOMPLETE` |
| Extraction | independent observations and reconciliation | conflict; no auto-merge |
| Scope | WBS/rule/dependency companion-work checks | scope issue; expert review |
| Quantity | source/formula/unit/geometry/alternatives and checks | unverified quantity hard stop |
| Nomenclature | explicit critical attributes and match class | reject or expert review |
| Normative | approved engine/basis/version/applicability | normative calculation unavailable |
| Price | qualified source classes, normalized commercial basis, triangulation and spread review | `RFQ_REQUIRED` or expert review |
| Logistics/finance | typed capacity/cash-flow models, exact observation-value binding, controlled completeness and dual recalculation | blocked model; no derived cost basis |
| Calculation | exact planned-component coverage and deterministic primary build-up | no release on missing, duplicate, or unplanned components |
| Independent validation | separate recalculation from atomic inputs | arithmetic mismatch hard stop |
| Scenario | approved definitions over a fixed base snapshot plus independent recalculation | reject unknown or unsupported overrides |
| Export | fixed released snapshot, mandatory content hashes, Ed25519 signature, immutable artifact metadata | refuse generation or verification |
| Audit integrity | per-key hash chains, WORM checkpoint, independent Ed25519 receipt, full current-history verification | readiness 503; investigate tampering or stale anchor |
| Mutation delivery | persisted request fingerprint/response plus unique transactional outbox deduplication key | replay exact result or reject conflicting key; at-least-once consumer deduplication |
| Contract | revisioned evidence-backed obligations tied to approved cost impacts | unresolved contract risk hard stop |
| Risk | verified register and version-bound deterministic reserve | missing or stale reserve hard stop |
| Approval | configurable tasks and four-eyes rule | approval hard stop |
| Actuals | revisioned verified facts, fixed-snapshot comparison and reason taxonomy | calibration data quarantine pending owner approval |

## Non-negotiable hard stops

The release policy evaluates the specified hard-stop list as server-derived
facts. Missing methodology-owned thresholds are themselves blockers; the
software never invents them.

Stage progression also fails closed before release. Pricing cannot advance
without current verified quantities, scope attestations, and passport facts.
Calculation cannot advance without a complete governed price basis, resolved
contract assessment, verified risk register, and current risk calculation.
Release independently re-evaluates these gates rather than trusting the
workflow state alone.

In staging and production, release also re-verifies the configured WORM policy
and fresh external audit checkpoint. Failure adds
`OPERATIONAL_INTEGRITY_UNAVAILABLE` and results in `BLOCKED`, even if an
orchestrator mistakenly sends traffic to a non-ready replica.

When a newer current document revision is uploaded, the confirmed document-set
reference is cleared, the project moves to `BLOCKED`, current derived facts and
decisions are marked stale/in-review, and outstanding approval tasks are
superseded. The old fixed snapshot remains immutable for audit, but release
rejects it because its document-set or controlled-version set is no longer
current.

Conflicting extraction values are not merged. A persisted conflict creates a
mandatory reviewer task; resolution must select one of the conflicting source
observations, be performed by someone other than that observation's author,
and creates a new verified derived observation with recursive lineage.

## Residual risks

- An approved but incorrect organisational methodology can still produce a
  wrong decision.
- Independent software paths can share bad atomic evidence; extraction and
  expert controls remain necessary.
- Two extraction paths are independent only when their active, approved adapter
  qualifications declare different independence domains. Merely using two
  prompts or two runs of one adapter is not independent evidence.
- The repository now combines hash chains, immutable PostgreSQL rows,
  content-addressed checkpoints, live WORM-policy enforcement, and external
  Ed25519 receipt validation. Production assurance still depends on an
  independently operated provider, protected keys, monitoring, and tested
  backup/restore and tamper response.
- Market evidence may be technically comparable yet commercially unavailable;
  availability and validity are mandatory price attributes.
- Model behaviour can change; exact model/prompt/rule versions and regression
  results must be approved and snapshotted.
- The implemented generic calculation categories do not yet constitute a
  complete logistics/mobilisation planner or contract cash-flow engine.
- A package-carried public key proves internal consistency but not external
  organisational identity by itself; independent consumers must trust the key
  ID/fingerprint through an approved out-of-band registry.
- The code enforces quarantine, durable leased delivery, bounded retry/dead
  letter, and a worker-only parser entry point, but no organisation-qualified
  malware provider, production scheduler, or runtime sandbox/network policy is
  deployed by this repository; production remains blocked until operational
  evidence proves that boundary.

## Required production evidence

Production bid authority requires all quality gates stated by the owner:
historical testing, blind estimator comparison, parallel operation, discrepancy
review, robustness and load tests, security review, restore testing, formally
approved methodology, trained users, and named owners. A technical demo cannot
waive these gates.
