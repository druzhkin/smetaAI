# Controlled BoQ spreadsheet contract

## Current status

Six bounded stages are implemented:

- `probe-boq-xlsx` performs profile-pinned diagnostic extraction and writes
  provenance-rich row candidates without changing governed project data.
- `import-governed-boq-xlsx` may persist those candidates as `UNVERIFIED`
  evidence observations, but only from the confirmed current document set,
  through an approved controlled profile and a qualified service identity.
- the BoQ spreadsheet-candidate workflow lets a technical specialist propose
  only the work code, canonical description and unit, then routes that proposal
  through the existing independent manual-evidence review before it can appear
  in BoQ authoring.
- the quantity from the same imported row is submitted as a separate immutable
  proposal. The browser sends the source identity and hash, not a typed numeric
  value. A different employee must independently approve that exact cell and
  unit through the manual-evidence review workflow.
- after the BoQ line itself is independently verified, a non-critical line may
  receive that exact server-held quantity under the current approved quantity
  policy. The browser cannot alter the value, unit, rounding or waste factor.
  A critical line remains `BLOCKED` because one reviewed spreadsheet row is
  not independent quantity evidence.
- `export-boq-analysis` creates a new XLSX price matrix, a short DOCX business
  report and a hash manifest from a governed price-matrix view or a diagnostic
  extraction. It preserves source-side names and locators, exposes blockers and
  leaves blocked financial cells and the project total blank.

These stages do not verify nomenclature, calculate a price, prove scope
completeness or release a bid. A blocked workbook creates no observations. The
production environment remains `BLOCKED` until a real parser qualification,
approved profile binding, service-account configuration, independent
validation and operational approval exist.

This export is an analysis-package worker, not the production release worker.
It does not consume the transactional outbox, store artifacts in the governed
object store, prove a fixed calculation snapshot or authorize a bid. The CLI
therefore cannot create an `APPROVED_FOR_BID` package from user-supplied flags.

## Governed import boundary

The command requires:

- the project and source document revision;
- a confirmed, integrity-valid current document set containing that revision;
- an approved controlled version bound to purpose `boq_xlsx_profile`;
- a profile that pins the exact workbook SHA-256, sheet, headers, rows, units
  and parser identity/version;
- an approved `TABLE_PARSER` adapter qualification for the same organization
  and isolated service account;
- an object-store entry whose size, hash and XLSX media type match the manifest;
- a project state explicitly allowed by the controlled profile.

The worker binding is configured with all three values or none:

```text
BOQ_XLSX_ADAPTER=<qualified adapter name>
BOQ_XLSX_ADAPTER_QUALIFICATION_ID=<approved qualification ID>
BOQ_XLSX_WORKER_ACTOR_ID=<isolated service account ID>
```

After the source revision, confirmed document set and controlled profile are
present, an operator may invoke:

```text
tenderguard import-governed-boq-xlsx \
  --project-id <project> \
  --document-revision-id <revision> \
  --request-id <unique request> \
  --reason "<bounded audit reason>"
```

Released, superseded and archived projects are never importable. Any hidden
content, unsafe formula, external link, ambiguous header, duplicate identity,
missing required value, unsupported unit or corrupt/protected source stops the
entire import. There is no partial-row fallback.

Every imported row retains:

- project, document, revision and confirmed document-set identities;
- workbook object hash and archive path;
- profile version and profile content hash;
- adapter qualification and parser version;
- worksheet, row and exact cell coordinates;
- original position ID, description, specification, unit and quantity;
- source observation time and deterministic extraction-run identity.

The importer is idempotent. A replay must reproduce the same observations and
run payload; disagreement is an integrity failure.

## Governed row mapping

`GET /v1/projects/{project_id}/boq/spreadsheet-candidates` reproduces every
listed candidate from its observation, qualified adapter, approved bound XLSX
profile and exactly one completed extraction run. Missing or inconsistent
lineage is an integrity failure rather than an empty result.

`POST .../spreadsheet-candidates/{observation_id}/mapping` accepts an explicit
specialist proposal containing only:

- the canonical work code;
- the canonical description;
- the canonical unit;
- the exact source-observation hash and proposal timestamp;
- a bounded audit reason.

The proposal is a new immutable `MANUAL`/`UNVERIFIED` observation. Its payload
states `quantity_promoted=false`; neither quantity nor price is copied into the
proposal. The exact imported observation ID and content hash are retained as
upstream lineage. A second active proposal is rejected.

The existing `MANUAL_EVIDENCE_REVIEW` task supplies the four-eyes decision.
Approval produces a separate `RULE_ENGINE`/`VERIFIED` `boq_line` observation;
rejection produces no verified value. Source mutation, document-set drift,
policy drift, self-review, task drift or upstream hash drift blocks approval.
Only the derived verified observation becomes visible to BoQ authoring.

## Governed quantity review and initial attachment

`POST .../spreadsheet-candidates/{observation_id}/quantity-evidence` accepts
only the exact source-observation hash, proposal timestamp and audit reason.
The server reproduces the imported decimal quantity, unit, cell locator,
profile, qualified adapter and completed extraction run. It then creates a
separate `MANUAL`/`UNVERIFIED` observation named
`boq_quantity:<source_item_id>`. It does not reuse the identity-mapping
approval. A second active quantity proposal is rejected.

The existing four-eyes review may produce a lineage-linked `VERIFIED`
quantity observation. Before any later use, the application replays the full
manual-review derivation: current approved policy and binding, confirmed
document set, direct and upstream hashes, task identity and exact payload,
approval record, reviewer separation and all timestamps. A stored status of
`VERIFIED` is not sufficient.

`GET .../boq/lines/{line_id}/initial-quantity-context` exposes the exact
server-held candidate and blockers. `POST .../initial-quantity` accepts only
that candidate ID and hash, the exact line timestamp and an audit reason. It
does not accept a quantity, unit, formula, rounding scale or waste factor.
Attachment is rejected when the line is not current and verified, the source
mapping is absent, the quantity policy is missing or incomplete, a current
quantity already exists, lineage drifts, multiple candidates exist, or the
line is marked as having a critical quantity. Quantity correction after this
initial attachment remains the existing separately governed revision flow.

## What an imported row means

An imported row is evidence of what a specific source workbook contained. It
is not proof that:

- the row describes one valid purchasing item;
- quantity or unit is commercially correct;
- a KSR, FGIS CS, tender or market item is equivalent;
- a price is current, comparable or safe;
- the project total is complete.

The source-row status therefore remains `UNVERIFIED`, including after its
identity mapping and separate quantity proposal are approved. Only the derived
reviewed observations may be used, and released calculation remains blocked
until the quantity-policy, nomenclature, technical-attribute,
document-completeness and human-approval gates also pass.

## Technical attributes and nomenclature

Name parsing may create attribute candidates, never verified attributes.
Source substrings and cell locators must be preserved for every candidate.
Independent extraction/reconciliation and human review must produce a
controlled observation such as:

```json
{
  "field_name": "technical_attributes:<source_item_id>",
  "value": {
    "source_item_id": "<source_item_id>",
    "attributes": {
      "<approved_attribute_name>": "<exact_normalized_value>"
    }
  }
}
```

Literal-name similarity may rank catalog candidates but cannot establish
technical equivalence. `EXACT` is available only after deterministic equality
of every catalog-declared critical attribute. Missing evidence produces
`INSUFFICIENT_DATA`; a material difference produces
`TECHNICALLY_UNACCEPTABLE`.

## Enriched analysis workbook

The implemented worker creates new exclusive files and never overwrites the
upload or an existing output directory. It stages the complete package before
publication and canonicalizes OOXML metadata so repeated builds from the same
report are byte-for-byte reproducible. Visible output sheets are:

1. `ВОР_с_оценкой` — source row and controlled calculation columns.
2. `Источники` — exact sources and normalized prices.
3. `Сопоставления` — source, FGIS CS, tender, market and catalog names plus
   critical-attribute comparisons.
4. `Блокировки` — missing evidence, stale data, conflicts and approvals.
5. `Метаданные` — source and output hashes, controlled versions, document set
   and release state.

The main sheet must expose source position, source names in every compared
system, exact locators/URIs, commercial basis, dates, normalized prices,
matching rationale, blockers and reviewer/approver identities.

An analysis workbook may contain blocked rows, but blocked proposed prices and
totals remain blank. It contains no formulas, hidden sheets or external links.
A release workbook may be generated only by a future governed consumer from an
approved, fixed snapshot that has passed independent recalculation. Workbook
values are not authoritative by themselves; signed server values and
provenance are.

## Verification gate

Before acceptance, a generated workbook must be reopened by an approved
spreadsheet runtime. Formulas, external links and hidden content must be
rechecked; generated values must be compared with the signed source package;
each sheet must be rendered for visual review.

Negative tests must cover ambiguous headers, duplicate identities, merged-row
drift, missing units, stale evidence, cross-item attributes, formula errors,
tampered output, tampered manual-review decisions, critical single-source
quantity rejection and blocked-price withholding.

The supplied colleague calculation workbook for Alabuga fails the strict OOXML
intake gate and cannot be treated as evidence. The original VOR inside the
supplied archive can be extracted diagnostically with a hash-pinned profile,
but its 23 candidates remain `BLOCKED`: they have not entered the governed
calculation and have no verified nomenclature or prices.
