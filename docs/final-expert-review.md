# Final expert review and automatic rework

## Business behaviour

The intended operating model is zero-touch project processing followed by one
final expert decision. Project staff do not enter prices, choose an analogue,
or change a calculated amount during the automatic pass.

At `EXPERT_REVIEW` the expert sees one package:

- every BoQ/TZ price row and its source/matching status;
- the current proposed row price or `BLOCKED` instead of an invented amount;
- all project release findings;
- the fixed calculation snapshot and exact release-gate hash;
- two actions: release the exact current result, or return selected items to
  rework.

Release remains impossible when a hard stop is present. A single expert action
does not waive missing normative data, a missing qualified engine, incomplete
source evidence, or four-eyes approval of a material manual change.

## Rework contract

The browser never sends a replacement price. It sends only:

- the exact current matrix-row or release-finding reference;
- the current blocker code, or `EXPERT_RECHECK_REQUESTED` for a row that the
  expert wants checked again despite passing automatic checks;
- an expert comment;
- project row version, release target, and current gate hash.

The server rebuilds the release evaluation and price matrix. It rejects a stale
hash, stale project version, absent row, outdated blocker, non-current finding,
non-expert actor, missing fixed snapshot, or snapshot/document-set mismatch.

Accepted rework is append-only in `expert_rework_requests`. PostgreSQL rejects
updates and deletes. The project moves from `EXPERT_REVIEW` to the earliest
applicable processing stage:

1. document issue -> `EXTRACTION_IN_PROGRESS`;
2. quantity/nomenclature issue -> `BOQ_IN_PROGRESS`;
3. price/contract-cost issue or selected price row -> `PRICING_IN_PROGRESS`;
4. calculation/snapshot issue -> `CALCULATION_IN_PROGRESS`;
5. infrastructure, governance, approval, or other non-automatic issue ->
   `BLOCKED`.

The request and its content hash are written to the audit chain and a
`project.final-review.rework-requested` outbox event is created atomically.
Changing project state makes the old snapshot ineligible for release; the gate
hash must be rebuilt only after rework and independent recalculation finish.

A qualified automatic dispatcher now claims that event with a lease. It
revalidates the immutable request, snapshot, project state, service
qualification, explicitly qualified stage list and issue references. In one
transaction it creates an immutable dispatch record and exactly one stage
command:

- `project.automation.extraction.requested`;
- `project.automation.boq.requested`;
- `project.automation.pricing.requested`;
- `project.automation.calculation.requested`.

Repeated delivery reuses the same dispatch and command. A tampered or stale
request is dead-lettered. A target with no safe automatic route is recorded as
`BLOCKED` and produces no stage command. A technically routable stage that is
absent from the worker's approved `supported_rework_stages` is also `BLOCKED`;
configuration cannot silently widen qualification. The source event is
acknowledged only after the downstream command exists durably.

The operator screen polls a read-only status endpoint and distinguishes
`PENDING_DISPATCH`, `STAGE_COMMAND_QUEUED`, downstream delivery state, and
`BLOCKED`. A queued or acknowledged command is explicitly not presented as a
completed recalculation.

## What is implemented and what is not

Implemented:

- immutable database record and migration;
- optimistic/version/hash binding;
- exact server-side row/finding validation;
- deterministic rework routing;
- state transition, signed audit event, and durable outbox handoff;
- qualification-bound dispatcher with leases, bounded retries, dead-lettering,
  idempotent stage-command creation, immutable dispatch evidence, and signed
  audit event;
- bounded CLI scheduler entry point `tenderguard dispatch-final-rework`;
- project-scoped status API and operator status panel with fail-closed
  integrity reporting;
- final-review UI for selecting rows/findings and submitting one reason;
- existing acceptance/release path remains authoritative;
- negative tests for stale evidence, unknown rows, wrong role, and PostgreSQL
  mutation attempts.

Not yet implemented end to end:

- qualified consumers for the four stage-command topics that actually run the
  connector, reconciliation, pricing and calculation modules through
  completion;
- fully automatic replacement of the existing intermediate approval tasks;
- qualified live market, won-tender, and supplier connectors for every
  category;
- production-qualified normative calculation without an approved external
  engine or complete formally approved rule base.

Therefore the repository now has the safe final-decision, rework request and
dispatch control plane. It is not yet truthful to claim that every real project
completes without intermediate staff: dispatch is not execution. That claim
becomes valid only after every stage consumer, required free/public connector,
and operational qualification is delivered and tested on representative
tenders.
