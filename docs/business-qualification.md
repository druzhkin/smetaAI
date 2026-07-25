# Business qualification campaign

This contract implements the repository-side mechanism for historical
validation, blind professional comparison, and parallel operation. It does not
provide the organisation's projects, professional estimates, thresholds, or
approval. Until a real campaign reaches `PASSED` and the other readiness gates
are independently evidenced, bid release remains blocked.

## Controlled inputs

Two independently approved controlled versions are mandatory:

- kind `business_qualification_profile`, schema
  `tenderguard.business-qualification-profile/v1`;
- kind `business_qualification_dataset`, schema
  `tenderguard.business-qualification-dataset/v1`.

The profile binds the exact immutable application build, currency, owner-set
comparison metric and commercial-basis evidence hash, minimum case counts,
maximum per-case absolute percentage error, MAPE, absolute
bias, material-discrepancy threshold, maximum exclusion ratio, minimum blind
independence domains, minimum parallel time span, display rounding, and the
closed discrepancy-reason taxonomy. The application has no fallback financial
or accuracy thresholds.

The dataset declares the closed population, evidence and query hashes,
selection method, cutoff, every selected case, and every exclusion with a
reason and evidence hash. Selected cases plus exclusions must equal the
population exactly. Case and snapshot identifiers are unique. Historical cases
pre-bind a verified actual; blind and parallel cases deliberately do not carry
the reference value.

Profile and dataset creation and approval use the normal controlled-version
four-eyes workflow. Campaign creation independently verifies both full audit
chains and content hashes.

## Forecast locking and anti-leakage

Campaign creation is restricted to an auditor or methodology owner with
explicit membership in every project. For each case the server:

1. reads the named fixed snapshot from content-addressed storage;
2. verifies object SHA-256, input/output/snapshot hashes and structure;
3. validates the stored primary and independent results;
4. reproduces the calculation-run currency, total, engine, status and payload;
5. requires successful independent validation and the exact profile currency;
6. verifies that the snapshot existed by the dataset cutoff;
7. persists an immutable case prediction and hash.

The resulting campaign is `INPUTS_LOCKED`. The exact profile, dataset,
application build, population, exclusions, snapshots, predictions, and input
hash cannot be edited. A duplicate campaign over the same organisation and
input basis is rejected.

Historical references are copied at lock time only from a verified
project-level actual with the profile-declared metric whose value/unit
reproduce a verified source observation. The actual-record and
actual-verification audit events must form a
valid four-eyes project audit chain. The immutable qualification reference
contains a content hash over the actual, source observation and both audit
event hashes. The verified source must carry the same comparison-basis hash as
the profile and must already exist at the dataset cutoff.

Blind and parallel references use a two-step process after the lock:

1. a project professional prepares an evidence observation tied to an exact
   document revision and locator;
2. a different project reviewer verifies that observation and creates a new
   immutable verified observation plus the campaign reference.

The preparer cannot be the campaign creator or snapshot creator. The reviewer
cannot be the preparer, campaign creator, or snapshot creator. Blind references
must attest that the professional was blinded to the system result and had no
bid authority. Parallel references must attest no bid authority. Performed
timestamps must be after campaign lock and not in the future. The server takes
the amount from the prepared evidence; the review command cannot replace it.
Both professional stages recheck the exact profile comparison-basis hash.

The preparer/reviewer endpoints do not return the locked system prediction.
Only auditor/methodology detail access may inspect prediction and evaluation
records.

## Exact evaluation

A different auditor or methodology owner evaluates the campaign. Before
calculating metrics, the service reopens and re-verifies every snapshot,
calculation run, case prediction, historical reference, professional source
observation, attestation and content hash.

All ratios use exact `Fraction(Decimal)` arithmetic. Display rounding never
affects pass/fail:

- case absolute percentage error;
- mode MAPE;
- mode absolute mean signed bias;
- maximum case error;
- exclusion ratio;
- blind independence-domain count;
- parallel calendar span.

An exact value equal to a maximum passes; a value above it fails even if both
round to the same displayed number. The evaluation contains exact
numerator/denominator pairs and a self-verifying result hash.

Failure of any controlled metric moves the campaign to `FAILED`. It cannot be
approved in place; calibration requires a new controlled profile/dataset and a
new campaign. A passing result moves to `EXPERT_REVIEW`.

Every case at or above its owner-defined material threshold creates an
immutable discrepancy. An independent auditor or methodology owner must assign
an approved reason code, root cause, corrective action, and verified
project-observation evidence. Reviewers cannot be the campaign creator,
evaluator, snapshot creator, or reference registrar. Rejected or missing
reviews prevent final approval.

## Final approval and release linkage

A methodology owner distinct from the creator and evaluator may approve only a
metrics-passing campaign whose every material discrepancy has an accepted
review by another actor. Approval creates an immutable package hash over:

- campaign input hash and immutable build;
- profile/dataset IDs and hashes;
- evaluation ID and self-verifying result hash;
- population size;
- every accepted discrepancy review and evidence hash.

The campaign then becomes `PASSED`. PostgreSQL permits only
`INPUTS_LOCKED -> EXPERT_REVIEW|FAILED -> PASSED`; its basis is immutable and
case/reference/evaluation/discrepancy/review/approval tables reject update and
delete.

The `production_qualification` controlled version must include a
`business_qualification` block with campaign ID, package hash, approver,
timestamp and environment. The historical, blind, parallel and variance gates
must reference that campaign and the same package hash. On controlled-version
approval and again on bid release, TenderGuard revalidates:

- campaign organisation and `PASSED` state;
- creator/evaluator/final-approver segregation;
- evaluation self-hash, all three passing modes, cases and references;
- exact material discrepancy/review coverage;
- approval package and timestamp;
- the complete campaign audit chain and lock/evaluate/approve events.

An arbitrary 64-character string cannot satisfy this gate.

## API workflow

- `POST /v1/qualification/business/campaigns`
- `GET /v1/qualification/business/campaigns/{campaign_id}`
- `POST .../cases/{case_id}/references/prepare`
- `POST .../cases/{case_id}/references/verify`
- `POST .../evaluate`
- `POST .../discrepancies/{discrepancy_id}/review`
- `POST .../approve`

All mutations use the persisted idempotency ledger, audit chain, and
transactional outbox.

## Required external evidence

Repository tests prove deterministic behavior and rejection paths, not
business accuracy. Production still requires an organisation-approved
population, professional estimators, sufficient independent domains and
parallel duration, real facts, controlled thresholds, completed discrepancy
analysis, process-owner sign-off, and immutable retention of supporting
documents. Failed or selectively omitted projects must remain visible in the
dataset/exclusion record.
