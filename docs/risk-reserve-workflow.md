# Governed risk-reserve workflow

This workflow covers execution risks and the reserve that enters the bid
calculation. It is fail-closed: extracted parameters, a plausible probability,
or a matching arithmetic total are not enough to create a usable reserve.

## Controlled model

Every project binds exactly one approved `risk_model` version. Its content is
validated as `RiskModelDefinition` and must declare:

- a non-empty, unique set of risk keys and the minimum required register size;
- the exact evidence field name for every declared risk;
- the subset of required risks and the subset that requires independent
  extraction;
- the controlled review role (`REVIEWER` or `TECHNICAL_EXPERT`);
- the exact BoQ line and semantic component that receives the reserve.

The content hash, organisation, approval audit chain, four-eyes approval and
project binding audit are reproduced when context is read, a risk is submitted,
a decision is recorded, a reserve is calculated, and the calculation stage gate
is evaluated. Correlated-risk aggregation is deliberately unavailable until an
approved executable correlation model is connected; declaring correlation
therefore blocks calculation instead of silently assuming independence.

## Evidence and risk review

The browser cannot enter probability, impact, currency, correlation or
mitigation amounts. It selects a structured observation returned by
`GET /v1/projects/{project_id}/risks/context`; the backend compares the
submitted structure byte-for-byte at the semantic JSON level with that current
server candidate.

Submission independently verifies:

1. the current confirmed document-set manifest and its confirmation audit;
2. the observation row, payload, field, model version and recursively resolved
   leaves;
3. project/document membership of every source leaf;
4. rejection of unreviewed manual evidence;
5. qualified and distinct automatic extraction domains for model-designated
   independent risks;
6. exact decimal constraints, impact ordering, ISO currency and the absence of
   unresolved conflicts.

Submission creates a dedicated `RISK_ITEM_REVIEW` task. The generic approval
API cannot close it. A dedicated decision binds the exact risk/task timestamps,
model hash, document set, direct evidence and independent leaves. The author
and task creator cannot approve the item; only the model-assigned role can.
Replacing a risk creates a new row and explicitly supersedes the old task while
retaining the complete earlier decision history.

## Deterministic reserve and independent validation

Only current, independently approved risk items participate. The primary
engine calculates the expected reserve for each independent triangular impact
as:

`probability × (minimum + most_likely + maximum) / 3`

A separately implemented validation path rebuilds the ordered inputs from
storage and repeats the arithmetic without using primary totals. It verifies
currency, model/version bindings, evidence and approval lineage, duplicate
keys, mitigation references, rounding and the target BoQ component.

The immutable calculation stores:

- the exact ordered risk IDs and input payload;
- model, document-set, approval and source bindings;
- the primary and independent values;
- an input hash and an output hash;
- explicit independent-validation status and failure reason;
- a supersession link when a later current calculation is fixed.

Any mismatch beyond the model's declared rounding, stale risk, missing required
key, model drift, evidence conflict or unsupported correlation blocks the
calculation stage. A successful result is bound to exactly one declared reserve
component so it cannot be omitted or counted twice.

## Database and stage enforcement

PostgreSQL guards make risk items, evidence links, reviews and calculations
append-only except for the narrow legal supersession transitions. Deferred
constraints reject a terminal review task without exactly one matching
decision. Illegal direct verification, history deletion, calculation mutation,
ambiguous current rows and unlinked supersession fail at commit.

`risk_stage_blockers` independently replays the model, document set, evidence
graph, task/approval/audit records, risk input hashes, both calculation paths
and reserve-component binding. It does not trust a persisted `VERIFIED` flag or
the primary service's total.

Uploading a newer current document revision invalidates current risk use:
items return to review, calculations become non-current, outstanding tasks are
superseded and the project remains blocked until the complete workflow is
repeated against the new manifest.

## Operator surface

`/projects/{project_id}/risk/manage` provides:

- the complete methodology-owned risk matrix and current blockers;
- only server-produced structured candidates from the current document set;
- dedicated optimistic four-eyes review with visible role and actor blockers;
- a calculation command that submits no financial values;
- the persisted reserve, independent-validation result and input/output hashes;
- explicit reason, exact project-code confirmation, acknowledgement and stable
  idempotency for every mutation.

The UI and backend were exercised together in desktop and mobile Chromium:
self-review was rejected, a separate reviewer approved the exact item, and the
reserve was independently reproduced without console or API errors. This is
application evidence, not production qualification.

## Remaining production evidence

Launch still requires an organisation-approved risk methodology, named
methodology owners, qualified extraction providers, approved correlation and
mitigation treatment where applicable, historical and parallel-operation
results, role-based UAT, independent security/load/recovery evidence and
approved operating procedures.
