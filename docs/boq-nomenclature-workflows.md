# Controlled reconciliation, BoQ, scope, and nomenclature workflows

## Trust boundary

These workflows treat the browser, record status, and stored controlled-version
ID as claims rather than proof. Every mutation reconstructs its governing
context from current database state.

The shared document-set integrity check requires:

- the exact project/current-set identity and `CONFIRMED` state;
- a non-empty, unique, bounded revision list;
- a manifest hash reproduced from that exact ordered list;
- a creator and confirmer with different identities;
- an ordered confirmation timestamp;
- the latest exact `document_set_confirmed` event in the valid complete project
  audit chain, including manifest, revision list, confirmer and authorised role.

BoQ and nomenclature evidence must point to a revision in that reproduced set.
A `VERIFIED` observation from a prior set is not reusable.

The shared controlled-version check requires exactly one project binding for
the purpose, matching kind and optional client-presented version ID, approved
content hash, organisation metadata, owner-role lifecycle, four-eyes approval,
a valid signed creation/approval audit chain, and the latest exact signed
project-binding event for that purpose. A row whose status merely says
`APPROVED`, or a binding row without its matching audit event, is insufficient.

## Independent reconciliation

`GET /v1/projects/{project_id}/evidence/reconciliation-context` returns raw,
current-set `UNVERIFIED` candidates with qualification, validity,
independence-domain, method/version, source locator, and explicit blockers.
Results are bounded and signal truncation.

`POST /v1/projects/{project_id}/evidence/reconcile` rechecks:

- the exact current `reconciliation_rules` binding;
- at least two unique original automatic observations;
- row/payload identity and current-set membership;
- one active qualification per source;
- exact service-identity/method/version binding;
- distinct qualification IDs and independence domains.

Agreement creates a separate derived `RULE_ENGINE`/`VERIFIED` observation.
Disagreement creates a `Conflict` and mandatory dedicated review task. Neither
path edits the source observations.

## BoQ authoring and review

`GET /v1/projects/{project_id}/boq/authoring-context` is available only in
`BOQ_IN_PROGRESS`. It lists bounded structured evidence from the reproduced
current set.

`POST /v1/projects/{project_id}/boq/lines` requires:

- an immutable stable line key and WBS node;
- work code and unit reproduced by at least one selected verified observation;
- non-empty unique evidence IDs;
- a non-empty cost-component plan with unique semantic keys and factor IDs;
- a reason and request idempotency key.

The result is `IN_REVIEW`. `GET .../boq/lines/{line_id}/review` returns the
immutable source/component ledger and blockers. `POST .../verify` binds the
exact timezone-aware line timestamp and requires a different reviewer. Any
concurrent edit, supersession, document-set drift, source drift, or identity
mismatch rejects the decision.

Quantity correction remains a separate controlled lifecycle. Optional formula
rules, when bound, now receive the same content/audit/four-eyes reproduction
as mandatory rule packs. Historical manual-change views reproduce their exact
approved policy audit chain, while application requires the currently bound
policy.

## Scope completeness

`POST /v1/projects/{project_id}/boq/scope-evaluations` executes only in
`BOQ_REVIEW`. The exact WBS node must contain at least one current verified
BoQ line; an unknown or empty node is rejected rather than recorded as a false
pass. The engine evaluates that WBS against the approved `scope_rules` version
and stores an input signature and findings. Changed BoQ or rules invalidate
the prior attestation. Absence of a line is never taken as proof that companion
work is unnecessary.

## Nomenclature

`GET /v1/projects/{project_id}/nomenclature/context` lists validated canonical
items from the reproduced current catalog and current-set technical evidence.
Catalog records must have normalized string attributes, a non-empty unique
critical-attribute list contained in those attributes, and an explicit
critical-price flag.

`POST .../nomenclature/assessments` accepts no result class. It requires the
source semantic key to occur exactly once among current verified BoQ
components, then deterministically compares critical attributes. The stored
match binds catalog version, source observation, current document set, actor,
and assessment method.

Every later quote or analogue operation replays that match integrity.
Mismatches may become a functional or conditionally acceptable analogue only
when the current approved equivalence rule explicitly covers every mismatched
attribute. Missing critical attributes cannot be waived. Analogue finalization
requires all exact mandatory approval tasks and records and remains subject to
four-eyes enforcement.

## Verification evidence

Integration tests cover:

- controlled-version substitution and approval-chain tampering;
- corrupted current-set manifest;
- exclusion and rejection of old-set evidence;
- independent reconciliation context and qualification domains;
- BoQ authoring context, author separation, and stale timestamp rejection;
- catalog context, deterministic exact matching, analogue controls, RFQ and
  price-chain reuse;
- full API flow through independent calculation and release gates.

These tests establish application behavior, not production qualification.
Qualified extraction providers, the licensed normative engine, real catalog
ownership, role-based UAT, accessibility evidence, and historical/blind/
parallel operating evidence remain external release blockers.
