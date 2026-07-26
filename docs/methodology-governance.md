# Methodology governance

Financial and technical thresholds are controlled business content, not
software defaults.

The methodology owner must version and approve:

- mandatory document types and critical project-passport fields;
- quantity tolerances, rounding, sign rules, balance rules, and historical
  benchmark versions;
- WBS/typology/dependency scope-completeness rules;
- critical nomenclature attributes and equivalence rules;
- approved normative bases, methods, regions, periods, and coefficients;
- price commercial basis, explicit normalization rounding scale/mode,
  triangulation, expiry, reliability, and RFQ rules;
- required logistics/mobilisation/finance model kinds and components,
  commercial-model rounding, day-count convention, zero-cost policy, and
  required contract-term/cash-flow coverage;
- approval triggers and all monetary/share thresholds;
- risk method, correlations, reserve treatment, and scenario definitions;
- calculation rounding/tax/currency policy and independent tolerance;
- actual-vs-forecast reason taxonomy and calibration eligibility.
- historical/blind/parallel population rules, exclusion policy, minimum sample
  sizes and independence domains, exact accuracy/materiality thresholds,
  parallel duration, reporting rounding, and discrepancy reason taxonomy.
- per-gate evidence mode, immutable build and environments, evidence age,
  artifact categories/count/byte limits, required claims, approved reviewer
  roles, exact load/recovery source profiles, and external attester keys.

The application currently binds the following governed content directly into
stage gates or calculation lineage:

- `document_requirements`, `passport_requirements`, `quantity_policy`, and
  `scope_rules`;
- `nomenclature_catalog`, `equivalence_rules`, `price_policy`, and
  `approval_thresholds`;
- `contract_risk_rules`, `risk_model`, `calculation_policy`,
  `commercial_cost_model`, `scenario_policy`, `export_template`, and
  `production_qualification`;
- a qualified normative adapter version plus a validated project-specific
  normative result whenever a normative component is used.

Policy payloads are schemas, not suggestion text. For example, price source
classes, normalization parameters, `normalization_rounding_scale`,
`normalization_rounding_mode`, spread rules, and selection method are read
from the project-bound approved `price_policy`. Normalized monetary values are
rounded before persistence and the same policy is used by integrity replay;
the database is not allowed to choose an implicit precision. Risk rounding and
the reserve component mapping come from the approved `risk_model`. Missing or
unsupported required fields block the operation instead of invoking a software
default.

Scenario overrides are also controlled content. The API selects a named
definition from the bound approved `scenario_policy`; it does not accept
arbitrary override values from the caller. Each scenario starts from a fixed,
integrity-checked snapshot and is independently recalculated.

The bound `export_template` must declare the mandatory signed-package schema
and format. It cannot remove the calculation snapshot, recursive lineage,
controlled versions, approvals, workflow, release decision, or audit chain.
Changing the template binding makes an earlier calculation snapshot stale for
new release/export purposes.

Controlled versions are created in `DRAFT`, approved by another authorised
person, and explicitly bound to a project. PostgreSQL permits only the exact
`DRAFT -> APPROVED` lifecycle transition and makes identity, payload and
approval evidence immutable. Binding, profile loading, calculation and
release reproduce the content hash, organisation, actor roles, four-eyes
separation and complete signed audit chain; a status string alone is never
accepted. A project cannot calculate with ambiguous duplicate version kinds.
A later version does not silently change an existing calculation snapshot.
See [controlled-version integrity](controlled-version-integrity.md).

Approval thresholds are organisation-owned values. The application may
evaluate a configured threshold, but it must not create a monetary threshold,
unchecked-cost share, high-value definition, or material-profit-impact
definition on behalf of the methodology owner.

`production_qualification` is a special controlled version. Its evidence must
link to the readiness register and may state `all_gates_complete=true` only
after formal process-owner approval. The payload must contain every mandatory
quality gate under `gates`; each gate must be `PASSED` and include a SHA-256
`evidence_hash`, `owner_id`, independent `approved_by`, `approved_at`, and
`environment`. A boolean without this complete evidence map is ignored by the
release engine.

Historical, blind-comparison, parallel-operation, and variance-resolution
gates must additionally point to one `PASSED` business qualification campaign
and its immutable approval package. Controlled-version approval and bid
release both revalidate the campaign, evaluation, discrepancy reviews,
segregation of duties, package hash, and audit chain. See
[business qualification](business-qualification.md).

The other six gates must reference a live approved package from the production
gate evidence registry. A hash without a package ID, retained object,
independent approval, valid profile and audit chain is rejected. Load and
backup/restore additionally require the exact internal qualification result;
they cannot be closed by an external report. See
[production gate evidence](production-gate-evidence.md).
