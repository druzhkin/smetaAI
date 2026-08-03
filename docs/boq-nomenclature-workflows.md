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

Imported XLSX rows enter through a preceding governed mapping step. The
spreadsheet-candidate context reproduces the exact current-set source row,
adapter qualification, controlled workbook profile and extraction run. A
technical specialist may propose only work identity, canonical description and
unit. The proposal retains the source observation and hash as upstream lineage
and explicitly does not promote quantity or price. The existing independent
manual-evidence review must approve it before a verified `boq_line` observation
exists. Any source-lineage drift is a hard stop.

Quantity from that imported row has a separate proposal and a separate
four-eyes decision. The server, not the browser, copies the exact decimal,
unit and locator into `boq_quantity:<source_item_id>`. Every downstream use
replays the complete current manual-review policy, document set, upstream
hashes, task, approval record, actor separation and timestamps. A status-only
`VERIFIED` row is rejected.

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

Only after that line verification may an estimator or technical expert attach
the reviewed imported quantity. The initial-quantity API accepts the evidence
ID/hash and exact line timestamp, but no numeric value, unit, formula, rounding
or waste coefficient. The current quantity policy is reproduced before use.
A missing or ambiguous quantity, policy drift, an existing current quantity,
or a critical line with only this single source returns `BLOCKED`.

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
items from the reproduced current catalog and the current verified BoQ
components that may be assessed. The client selects a server-listed source
component; free-form source-item identifiers are not required by the operator
UI. Technical evidence is returned only from the exact controlled field
`technical_attributes:<source_item_id>` and its structured value must repeat
the same `source_item_id` together with a non-empty `attributes` object. A
verified observation bound to another component, an unbound legacy flat
attribute object, or a source component that is not globally unique across the
current verified BoQ fails closed.

Catalog records must have normalized string attributes, a non-empty unique
critical-attribute list contained in those attributes, and an explicit
critical-price flag. Once a source component is selected, catalog candidates
are ordered by deterministic literal token retrieval over the source
description, canonical identifier and catalog attributes. Compound
letter/number identifiers such as `DN100` and `DN 100` are tokenized
consistently. The API returns the exact matched terms and critical attribute
values used for that order, plus an explicit warning: retrieval order is not
technical-equivalence evidence. It is never converted into a probability,
confidence score, match class or release decision.

`POST .../nomenclature/assessments` accepts no result class. It requires the
source semantic key to occur exactly once among current verified BoQ
components. It independently rechecks the exact evidence field and embedded
source-item binding, then deterministically compares critical attributes. The
stored match binds catalog version, source observation, current document set,
actor, and assessment method. Every later integrity replay reconstructs the
same bound attribute object rather than trusting the stored match payload.

Every later quote or analogue operation replays that match integrity.
Mismatches may become a functional or conditionally acceptable analogue only
when the current approved equivalence rule explicitly covers every mismatched
attribute. Missing critical attributes cannot be waived. Analogue finalization
requires all exact mandatory approval tasks and records and remains subject to
four-eyes enforcement.

## BoQ price matrix

`GET /v1/projects/{project_id}/boq/pricing-matrix` produces one read-only row
per current BoQ cost component. It does not select a price in the browser. Each
row exposes:

- the exact BoQ description, work code, WBS, quantity, unit, semantic item and
  cost basis;
- the current nomenclature match, catalog identifier, source/canonical
  attribute matrix, mismatch set, method and controlled catalog version;
- separate collections for won-tender, FGIS CS and independent market prices;
- the exact source display name, source-side item name, record identifier,
  HTTPS URI, observation, document revision, locator, dates, commercial status,
  raw price and every current-policy normalized price;
- the current deterministic price decision, approved selection method and
  reasons, but only when its complete integrity replay returns `VERIFIED`.

Every new quote carries a structured source passport. FGIS CS is not inferred
from a generic `OFFICIAL_OR_PRIMARY` label or a convenient source name:
`source_type=FGIS_CS` must be present in the immutable quote evidence and must
agree with its evidence class. Won-tender and website/marketplace
classifications are enforced in the same way. External sources require a
credential-free HTTPS URI; a supplier quote may instead remain bound to the
controlled project document set.

The qualification of every extraction leaf must explicitly include the
evidence class, the exact `supported_price_source_types` value, and the
controlled `supported_price_source_origins` identifier. A generally qualified
official-source parser cannot label an observation as FGIS CS unless its
approved qualification authorizes both `FGIS_CS` and that exact origin;
missing legacy declarations fail closed and require requalification rather
than silent migration.

The proposed amount is withheld and the row returns `BLOCKED` when the BoQ
line or quantity is unverified, the nomenclature or catalog chain is invalid,
one of the won-tender/FGIS CS/market groups is absent, source integrity fails,
the price policy is unavailable, or the current decision is absent,
review-required or RFQ-required. No similarity or AI-confidence score is
reported as evidence.

## Verification evidence

Integration tests cover:

- controlled-version substitution and approval-chain tampering;
- corrupted current-set manifest;
- exclusion and rejection of old-set evidence;
- independent reconciliation context and qualification domains;
- BoQ authoring context, author separation, and stale timestamp rejection;
- separate imported quantity review, no-client-value attachment, critical
  single-source blocking and post-approval tamper replay;
- catalog context, deterministic exact matching, analogue controls, RFQ and
  price-chain reuse;
- rejection of cross-item technical evidence, unsupported attribute fields,
  unknown/duplicate current BoQ component identities, and stale client source
  context;
- deterministic catalog retrieval explanations without confidence or
  equivalence claims;
- source-passport validation, mandatory won-tender/FGIS CS/market groups,
  post-row source-name visibility and withholding of incomplete proposed
  prices;
- full API flow through independent calculation and release gates.

These tests establish application behavior, not production qualification.
Qualified extraction providers, the licensed normative engine, real catalog
ownership, role-based UAT, accessibility evidence, and historical/blind/
parallel operating evidence remain external release blockers.
