# Safety case

## Claim

TenderGuard may emit `APPROVED_FOR_BID` only when a reproducible calculation is
supported by a complete, traceable evidence set and independent controls.

This claim is not yet established for production. The repository implements
and integration-tests a broad transactional enforcement core, including the
governed BoQ, quantity, scope, nomenclature, pricing, contract, risk,
calculation, independent validation, approval, release, lineage, actuals, and
calibration workflows. External adapters, several detailed costing workflows,
enterprise integrations, and all operational qualification evidence remain
open.

## Defence in depth

| Layer | Independent evidence | Failure response |
|---|---|---|
| Input integrity | archive manifest, hashes, file health, revision graph | block or `DOCUMENTS_INCOMPLETE` |
| Extraction | independent observations and reconciliation | conflict; no auto-merge |
| Scope | WBS/rule/dependency companion-work checks | scope issue; expert review |
| Quantity | source/formula/unit/geometry/alternatives and checks | unverified quantity hard stop |
| Nomenclature | explicit critical attributes and match class | reject or expert review |
| Normative | approved engine/basis/version/applicability | normative calculation unavailable |
| Price | qualified source classes, normalized commercial basis, triangulation and spread review | `RFQ_REQUIRED` or expert review |
| Calculation | exact planned-component coverage and deterministic primary build-up | no release on missing, duplicate, or unplanned components |
| Independent validation | separate recalculation from atomic inputs | arithmetic mismatch hard stop |
| Scenario | approved definitions over a fixed base snapshot plus independent recalculation | reject unknown or unsupported overrides |
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
- A hash chain detects alteration but is not a substitute for database
  permissions, external anchoring, backups, or WORM retention.
- Market evidence may be technically comparable yet commercially unavailable;
  availability and validity are mandatory price attributes.
- Model behaviour can change; exact model/prompt/rule versions and regression
  results must be approved and snapshotted.
- The implemented generic calculation categories do not yet constitute a
  complete logistics/mobilisation planner, contract cash-flow engine, or
  operational scenario workflow.
- Untrusted parsing still occurs without a qualified malware quarantine and
  isolated worker boundary; this is a production blocker.

## Required production evidence

Production bid authority requires all quality gates stated by the owner:
historical testing, blind estimator comparison, parallel operation, discrepancy
review, robustness and load tests, security review, restore testing, formally
approved methodology, trained users, and named owners. A technical demo cannot
waive these gates.
