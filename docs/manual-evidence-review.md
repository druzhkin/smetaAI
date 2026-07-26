# Governed manual evidence review

Manual correction never changes an extracted observation and never grants
`VERIFIED` to an operator-entered row. It creates a new immutable
`MANUAL`/`UNVERIFIED` observation and a mandatory dedicated review task.
Only an independent reviewer may produce a separate derived
`RULE_ENGINE`/`VERIFIED` observation.

## Required controlled policy

Each project that permits manual evidence must bind exactly one independently
approved controlled version with both kind and purpose
`manual_evidence_policy`. A minimal payload is:

```json
{
  "review_role": "REVIEWER",
  "allowed_project_states": [
    "EXTRACTION_IN_PROGRESS",
    "EXTRACTION_REVIEW",
    "BOQ_IN_PROGRESS",
    "BOQ_REVIEW"
  ]
}
```

`review_role` may be only `REVIEWER` or `TECHNICAL_EXPERT`. States are
methodology-owned: the software supplies no permissive defaults. The complete
controlled-version content hash, organisation, four-eyes lifecycle and signed
audit chain are revalidated on entry and review.

## Entry invariants

The server accepts a manual observation only when:

- the actor has a current project membership and an accepted
  technical/reviewer role;
- the project is in a state explicitly listed by the bound policy;
- `method_version` equals the exact bound policy version ID;
- the referenced document revision belongs to the project's current
  independently confirmed document set;
- document ID, revision ID and original-object SHA-256 agree;
- the timestamp has a timezone and the reason is non-empty;
- the value contains no binary floating-point number, including at nested JSON
  levels.

Exact decimal values are transported as strings. The operator UI rejects JSON
numeric literals and requires exact numbers inside JSON to be quoted.

Entry persists the source reason, source observation hash, policy hash,
document-set revision, document revision, reviewer role and task creator. The
dedicated task ID is deterministic for the source observation and policy.
Retrying the same idempotent HTTP request cannot create a second task.

## Review invariants

`MANUAL_EVIDENCE_REVIEW` cannot be decided through the generic approval API.
The dedicated review locks the project, source and task, then rechecks:

- source row/payload identity and immutable source hash;
- current policy and current confirmed document set;
- exact task scope, required flag, assigned role and optimistic timestamp;
- source author and task creator separation;
- current project state.

Changing the document set or policy blocks the older task; it is not silently
re-based. Approval creates a new immutable observation with source, policy,
document-set, task and approval-record lineage. Rejection or
changes-requested creates no verified value.

PostgreSQL additionally makes approval records append-only, permits only
explicit task decision/supersession transitions, and uses deferred constraints
to require one immutable decision record that exactly matches every terminal
approval task. A task cannot be marked approved without its decision record,
and a decision record cannot be inserted against a pending task.

## Operator routes

- `GET /v1/projects/{project_id}/evidence/manual/context`
- `POST /v1/projects/{project_id}/evidence/observations`
- `GET /v1/projects/{project_id}/evidence/observations/{id}/manual-review`
- `POST /v1/projects/{project_id}/evidence/observations/{id}/manual-review/decision`

Every mutation requires an idempotency key and is recorded in the audit/outbox
transaction. This workflow is an application control, not proof that the
organisation's policy, reviewers or source documents are correct.
