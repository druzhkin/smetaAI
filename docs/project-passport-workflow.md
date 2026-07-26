# Governed project-passport workflow

The project passport is a controlled set of project facts. It is not a text
summary and it cannot be completed by typing a plausible value into the
passport screen.

## Trust boundary

The approved `document_requirements` version declares:

- required and optional passport fields;
- fields that require independent extraction;
- the expert role allowed to review each submitted fact.

Every command revalidates the controlled-version content hash and its signed
creation, approval, and project-binding history. It also revalidates the
current independently confirmed document-set manifest. A stored `APPROVED`
label is never sufficient by itself.

The browser selects observation identifiers from the server context. The
submitted value and unit are copied from one selected observation and the
server requires every selected observation to reproduce them exactly. It
rejects evidence from another project, field, document set, value, unit, or
ineligible status. The browser cannot enter or merge a passport value.

## Independent extraction

For a policy-declared independent field, the server recursively resolves
derived observations to their leaf sources and requires at least two leaves.
Each leaf must:

- be an automated, eligible observation;
- reproduce its immutable row identity and payload;
- use an approved, unexpired adapter qualification;
- match the qualified method, adapter version, organisation, and service
  actor;
- have a qualification and independence domain distinct from every other
  leaf.

An LLM confidence score, two rows from one adapter, or a derived observation
that ultimately reuses one leaf does not establish independence. Derived
graphs are resolved recursively, reject cycles, malformed or missing source
identities, and must reproduce the fact's exact value, unit, field, and current
document set at every leaf; a derived result cannot conceal disagreeing
sources.

A raw `MANUAL` observation remains ineligible even for a non-independent
passport field. It must first pass the governed manual-evidence review, which
creates a separate lineage-linked `VERIFIED` observation; the passport review
does not bypass that control.

## Review and revision lifecycle

Submission creates an `IN_REVIEW` fact and a dedicated
`PASSPORT_FACT_REVIEW` task. The task hashes the exact value, unit, source
observations, independence leaves, author, requirements version, document set,
and review role.

The decision command binds the exact fact and task timestamps. The assigned
review role must be present, and the reviewer cannot be the fact or task
author. Before accepting a decision, the server repeats the controlled
version, manifest, conflict, observation, value, unit, qualification, and
independence checks. Approval creates one immutable approval record and one
signed audit event.

`CHANGES_REQUESTED` and `REJECTED` preserve the rejected fact and decision.
Submitting a correction makes the old fact non-current and explicitly
supersedes its review task with the replacement fact identifier. PostgreSQL
permits that lifecycle transition only when the prior payload remains
contained in the new payload and the auditable supersession markers are
present.

## Stage and release gates

The extraction-to-BoQ transition and release rebuild passport integrity from
stored primitives. For every required field they require:

- one current `VERIFIED` fact;
- exact value and unit reproduction from current-set observations;
- current controlled requirements and document-set bindings;
- qualified independence leaves where required;
- a matching approved dedicated task and immutable approval record;
- a matching project decision audit event;
- different author and reviewer;
- no unresolved field conflict, including a conflict created after approval.

Any mismatch yields a blocker such as `missing`, `unresolved-conflict`, or
`integrity-failed`. The workflow remains blocked instead of presenting a
partially trusted passport as ready.

## Operator API

- `GET /v1/projects/{project_id}/passport/context`
- `POST /v1/projects/{project_id}/passport/facts`
- `POST /v1/projects/{project_id}/passport/facts/{fact_id}/decision`
- `POST /v1/projects/{project_id}/passport/validate`

Mutations are idempotent, project-authorised, audit-recorded, and bounded by
explicit reasons and optimistic versions.

## Remaining production evidence

The workflow does not qualify real OCR, table, or visual providers and does
not decide the organisation's passport field policy. Production use still
requires approved requirements, qualified adapters and service identities,
representative historical and blind validation, role-based UAT, operating
procedures, training, security review, and the other open gates in the
production-readiness register.
