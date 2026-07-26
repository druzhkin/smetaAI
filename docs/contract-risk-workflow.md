# Governed contract-risk workflow

This workflow covers contractual conditions that change execution cost or risk.
It is fail-closed: a term is not sufficient merely because text was extracted,
and a zero cost impact is not accepted merely because an estimator entered
zero.

## Controlled policy

Every project binds exactly one approved `contract_risk_rules` version. Its
`contract` section is validated as `ContractRequirementsPolicy` and must
contain:

- at least one `required_term_kinds` entry;
- an exact, unique `evidence_field_names` mapping for every required kind;
- `independently_verified_term_kinds` as a subset of required kinds;
- a controlled review role (`REVIEWER` or `TECHNICAL_EXPERT`).

The content hash, organisation, approval audit chain, four-eyes approval and
project binding audit are reproduced on every context, mutation and stage
gate. An empty required-term list is invalid and cannot bypass the contract
gate.

## Evidence and term review

The browser does not submit arbitrary contract text. It selects observations
returned by `GET /v1/projects/{project_id}/contract/context`; the submitted
value is copied from that server context.

The service then independently verifies:

1. the exact confirmed current document-set manifest and its confirmation
   audit;
2. observation row/payload identity, field, value, null unit, method, version
   and status;
3. membership of all direct and recursively resolved leaf observations in the
   confirmed set;
4. rejection of raw unreviewed manual evidence;
5. qualified, distinct automatic leaf domains for policy-designated
   independent terms;
6. absence of an unresolved conflict for the policy-mapped field.

Submission creates a dedicated `CONTRACT_TERM_REVIEW` task. Generic approval is
explicitly forbidden. The dedicated decision binds the exact term and task
timestamps, approved rules hash, document set, evidence IDs and recursively
resolved independence IDs. The term author cannot decide the task. Rejected or
changes-requested revisions remain immutable; a replacement explicitly
supersedes their tasks.

## Cost impact

Only a verified term with a reproducible review task and approval record may
receive a cost-impact proposal.

- A non-zero amount must exactly reproduce a current validated
  `CONTRACT_FINANCE` model, its currency, target BoQ line, semantic component,
  current document set and controlled commercial policy.
- A zero deterministic amount requires a non-empty explicit reason and cannot
  carry a hidden component or model reference.

Every proposal creates a policy-owned `CONTRACT_COST_IMPACT` approval task.
The proposer cannot approve it. Finalisation uses optimistic locking, requires
all exact tasks to be `APPROVED`, verifies their entity/type/records, and
replays the finance-model reference. Replacing a proposal explicitly
supersedes its old task instead of leaving a misleading pending item.

## Independent stage gate

`contract_stage_blockers` does not trust the service booleans. For every
required kind it independently rebuilds:

- governed policy and current document-set integrity;
- the observation graph and exact value;
- independent qualified leaves where required;
- the immutable term submission hash;
- the dedicated review task, approval record and decision audit event;
- the current supersession chain;
- the zero-cost assumption or validated finance model;
- the cost-impact approval record and finalisation audit event.

Any unresolved conflict, missing record, stale version, task drift, evidence
tampering, self-approval or model mismatch returns an integrity blocker and
prevents calculation/release progression.

## Operator surface

`/projects/{project_id}/contract/manage` provides:

- the complete methodology-owned required-term matrix;
- exact current-set evidence candidates and their blockers;
- dedicated term review with four-eyes and optimistic-version blockers;
- selection of a server-side finance model or an explicit zero-cost
  assumption;
- direct links to required cost-impact approval tasks;
- finalisation only after every task is approved;
- persisted formal contract validation.

All mutations require a reason, exact project-code confirmation, an explicit
attestation and an idempotency key.

## Remaining production evidence

This repository implementation is not production qualification. Launch still
requires organisation-approved contract rules, named methodology owners,
qualified extraction providers, real treasury/indexation/FX inputs, historical
and parallel-operation results, role-based UAT, security/load/recovery evidence
and approved operating procedures.
