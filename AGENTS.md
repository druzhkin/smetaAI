# Working agreement

- This is a safety-critical tender-cost system. Never present an incomplete
  implementation, an AI confidence score, or a passing demo as evidence that a
  bid price is safe to release.
- Default to fail-closed behaviour. When evidence, approved methodology,
  independent validation, or required approvals are absent, return `BLOCKED`.
- Do not invent financial thresholds, normative rates, market prices, exchange
  rates, tax rates, coefficients, or equivalence rules.
- Normative cost calculations must be delegated to an approved estimating
  engine or backed by a complete, versioned, formally approved rule base.
- Every financial value must retain source-to-output provenance and every
  material manual change must be audited and approved under the four-eyes rule.
- Prefer direct criticism over reflexive agreement. State weaknesses, risks,
  inconsistencies, and missing evidence precisely.
- Keep calculation code deterministic, decimal-based, versioned, reproducible,
  and independently recalculable.
- Tests must cover dangerous negative cases and hard stops, not only happy
  paths.
