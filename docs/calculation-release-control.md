# Controlled calculation and release

## Boundary

The browser is not a calculation engine and is not a source of financial
inputs. It may request the current server candidate, attest that it has been
reviewed, and submit only:

- the project row version;
- the SHA-256 candidate hash;
- an auditable reason and idempotency key.

The server reconstructs the complete candidate again under the project lock.
It rejects a changed hash or row version before creating a calculation run or
snapshot.

## Server-generated candidate

`GET /v1/projects/{project_id}/calculation-context` returns a candidate only
while the project is in `CALCULATION_IN_PROGRESS`. The candidate is built from
the current confirmed document set and must exactly cover every cost component
of every current verified BoQ line.

For each component the server requires one current verified quantity and
exactly one applicable basis:

- replay-verified market price decision and its derived observation;
- validated normative artifact from a qualified normative path;
- exact approved assumption record;
- current validated risk reserve;
- current, document-aligned, four-eyes-approved commercial cost model.

The bound approved `calculation_model` supplies the currency, line and total
rounding scales, rounding mode, and independent tolerance. A component sign
and every factor identifier come from the verified BoQ component plan. Each
factor must resolve once in the bound approved versions. The candidate hash
commits to the project/version, document set, calculation model, complete
policy, and canonical atomic inputs.

`POST /v1/projects/{project_id}/calculations/current` accepts no quantities,
rates, factors, policy fields, or totals. It rebuilds and compares the
candidate, runs the primary Decimal calculation, independently recalculates
from the atomic inputs without consuming the primary total, fixes the
content-addressed snapshot, and advances only to
`INDEPENDENT_VALIDATION`. Any arithmetic difference beyond the controlled
tolerance blocks the result.

The calculation page displays quantities, rates, basis identifiers, factors,
policy and candidate hash as returned by the API. It never multiplies these
values in JavaScript. After execution it reads the fixed snapshot again and
shows the amount only when the object hash, nested snapshot data, calculation
run metadata, primary result and independent result agree.

## Release gate binding

`GET /v1/projects/{project_id}/release-gates` returns:

- the exact project state and row version used for evaluation;
- separate internal-use and bid decisions;
- every server-derived finding for each target;
- a distinct SHA-256 gate hash for each decision.

Each gate hash commits to the project identifier, workflow state, row version,
current document set, complete `ReleaseContext`, and exact target decision.
The release page renders the complete finding list. It does not expose a
signing form when the decision is blocked, the workflow state does not permit
the target, or the authenticated user lacks the approver role.

An approver submitting
`POST /v1/projects/{project_id}/release/{internal|bid}` must provide the exact
project row version and gate hash, a reason, project-code attestation,
target-state attestation, acknowledgement, and a stable idempotency key. The
server locks the project, validates the legal workflow transition, rebuilds
the complete release context and decision, and compares the gate hash. A
change in documents, evidence, controlled versions, calculation snapshot,
approvals, operational integrity, qualification evidence, state, or row
version makes the earlier decision unusable.

The accepted release record stores the gate hash, release-context hash,
project row version, snapshot identifier, findings and deciding actor. The
workflow transition and audit event are written in the same transaction. A
blocked server decision can never become `APPROVED_FOR_BID`.

## Database integrity

PostgreSQL rejects `UPDATE` and `DELETE` for:

- calculation runs;
- atomic calculation inputs;
- release decisions;
- scenario runs.

The same migration retains the earlier immutable pricing-evidence guards.
Corrections require a new governed revision or calculation; operators must not
edit database rows. Upgrade from a legacy database also remains fail-closed
when its audit chain cannot be verified with the configured historical key.

## Verification evidence

Automated checks cover:

- missing, duplicate, stale or mismatched basis records;
- client-supplied input/policy/quantity substitution;
- stale candidate and release-gate hashes;
- explicit Decimal precision, rounding and overflow bounds;
- primary/independent result mismatch;
- snapshot-object and calculation-run tampering;
- stable scenario selectors resolved against the fixed snapshot;
- SQLite migration round-trip and real PostgreSQL immutability triggers;
- API request shape and browser workflows.

Real Chromium QA on 25 July 2026 exercised a governed derived-cost candidate,
fixed a 9,300 RUB snapshot with a matching independent result, navigated to
release, and confirmed that seven deliberately missing qualification and
methodology prerequisites were shown as hard stops with no signing command.
Desktop and 390-by-844 layouts produced no browser console errors. This is
engineering evidence only; it is not historical accuracy, role-based UAT, or
authority to release a real bid.
