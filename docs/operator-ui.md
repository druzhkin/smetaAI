# Operator UI contract

## Purpose and current boundary

The operator UI is an evidence-navigation and review surface for the
fail-closed TenderGuard control plane. It exposes:

- a role-filtered project portfolio;
- the user's governed work-item queue;
- project release gates, hard stops, current cost indicators, evidence counts,
  attention items, and audit history;
- paginated, searchable current and historical project records with source
  attributes;
- controlled project registration and quarantined document submission.

The interface does not make an estimate reliable by rendering it. Backend
authorization, workflow guards, version checks, idempotency, four-eyes
segregation, independent validation, and release policy remain authoritative.

The current interface includes controlled mutation families plus read-only
BoQ pricing and automatic-rework status surfaces:

- authorised financial users may open a post-row BoQ price matrix that fixes
  the operator's comparison surface: original BoQ/TZ name and quantity,
  catalog name and critical-attribute matrix, exact source-side names for won
  tenders, FGIS CS and independent market websites/portals, direct source URI
  and locator, normalized bases, and the deterministic system rationale. A
  missing group, stale match, failed lineage replay, RFQ or pending approval
  renders `BLOCKED` and withholds the proposed amount. The matrix is not a bid
  release decision;

- an estimator may register a project in `DRAFT`; the organisation comes from
  the authenticated identity, the creator receives a versioned owner
  membership, and creation does not imply document completeness;
- an estimator or technical expert may stream a supported file into the
  separate quarantine store. The browser binds document metadata, criticality,
  current-candidate intent, reason, acknowledgement, and a stable idempotency
  key. The receipt exposes the byte count, SHA-256, scan/processing state and
  resulting revision only if one is actually created;
- a reviewer or approver other than the candidate creator may confirm the
  exact document-set manifest and revision list. The server locks and
  revalidates the candidate, rejects stale or non-draft candidates, records
  the independent actor, and leaves document completeness and downstream
  verification gates unresolved;
- a reviewer or technical expert may resolve an extraction conflict only
  through its dedicated screen. The screen exposes every source value,
  method, actor, document revision, locator and object hash; disables a source
  authored by the current actor; binds the exact conflict and task timestamps;
  shows the adapter qualification, independence domain and commercial basis;
  and requires an independent reason, project code and acknowledgement. The
  backend revalidates all qualifications at decision time, rejects the
  conflict-task creator, and prevents the generic approval command from
  closing this task;
- a technical expert or reviewer may record a manual correction only against
  a revision selected from the current independently confirmed document set.
  The browser takes the policy version, document IDs and SHA-256 from the
  server, requires an exact locator/reason/project acknowledgement, transports
  exact numbers as strings and rejects nested JSON numbers. The result remains
  `MANUAL`/`UNVERIFIED`. A separate policy-assigned reviewer screen exposes the
  immutable value, reason, author, unit, document, hash, locator and task
  version; self-review and the generic decision API are blocked. Approval
  creates a separate lineage-linked `RULE_ENGINE`/`VERIFIED` observation;
- a reviewer or technical expert may reconcile two or more raw automatic
  observations only from a server-built context for the exact confirmed
  document set and approved reconciliation rule version. The screen exposes
  method, version, qualification, independence domain, locator and blockers.
  It rejects duplicate sources, manual/derived inputs, same-domain adapters
  and client-selected rule substitution. Disagreement creates a `Conflict`;
  the browser never merges it;
- an estimator or technical expert may create a BoQ line only from a
  server-listed verified observation in the reproduced current document-set
  manifest. Work code and unit must match that evidence, cost-component
  semantic keys are unique, and the line remains `IN_REVIEW`;
- a different technical expert or reviewer may verify the exact current BoQ
  revision. The review screen exposes WBS, complete component plan, source
  observations and server blockers; submission binds the line's
  timezone-aware `updated_at`. Author self-review, stale timestamps,
  superseded document sets and old-set evidence fail closed;
- a technical expert may separately send the exact imported XLSX quantity to
  four-eyes review without typing or changing the number. After both that
  review and BoQ line verification, an estimator or technical expert may
  attach the single server-held quantity from the BoQ record. The attachment
  command contains no value, unit, formula, rounding or waste coefficient;
  policy drift, multiple candidates, an existing quantity and any critical
  single-source line display `BLOCKED`;
- in `BOQ_REVIEW`, a reviewer or technical expert may run the approved
  Scope Completeness Engine for a WBS node. The server binds the current BoQ
  signature and controlled rule pack and persists findings; an empty UI list
  is not treated as evidence unless the evaluation is recorded;
- procurement or a technical expert may assess a BoQ component against the
  current approved catalog. The browser selects the source component from the
  server's current verified BoQ list instead of accepting a typed semantic
  key. Changing that component clears the selected evidence and catalog item.
  Only technical-attribute observations whose field and structured value are
  bound to the selected source component are shown. Catalog candidates may be
  ordered by disclosed literal term matches to reduce mechanical search, but
  the screen labels that order as retrieval only and never displays an AI
  confidence. The browser submits only the semantic component, canonical item
  and verified technical-attribute observation; it does not submit a match
  class. The server reproduces the unique current BoQ component, exact
  evidence binding, catalog audit chain, critical attributes and document set
  before calculating `EXACT`, `INSUFFICIENT_DATA`, or
  `TECHNICALLY_UNACCEPTABLE`;
- a technical expert may propose only an explicitly permitted functional or
  conditionally acceptable analogue. The review screen shows the immutable
  attribute matrix and approval tasks. Finalization is disabled until the
  server revalidates the current equivalence-rule version and every mandatory
  independent approval record;
- an estimator or technical expert may propose a revision of a current
  verified BoQ quantity only against the exact current quantity, confirmed
  document set, approved quantity/formula rules, approved manual-change policy,
  and project-scoped source observations. Critical changes create a
  policy-assigned four-eyes task. The author cannot approve it, the reviewer
  sees immutable before/after states and exact evidence, and only the original
  author may apply the approved server-held after-state. The browser never
  resubmits the approved numeric value during application; stale context,
  altered evidence, reused proposals, broken approval linkage, and unregistered
  revisions fail closed;
- procurement users may register a price only from a project-scoped verified
  observation whose two qualified extraction leaves reproduce one exact quote
  and one controlled source origin. Procurement or estimators then select only
  applicable unit, currency, region, party, payment, delivery and unloading
  references exposed by the bound approved price policy. The browser never
  accepts or computes a replacement amount. The server revalidates technical
  attributes, source independence, validity, availability, reference
  applicability and adjustment evidence, applies the policy's explicit
  decimal rounding, persists reproducible inputs, and deterministically
  replays them before triangulation. Missing evidence creates
  `RFQ_REQUIRED`/expert review rather than an approximate verified price;
- an estimator or technical expert may create a risk revision only by choosing
  an unchanged structured candidate from the current confirmed document set
  and the currently bound approved risk model. The browser accepts no
  probability, impact, currency, correlation or mitigation amount. A dedicated
  model-role reviewer sees the exact immutable item, evidence leaves, task
  version and all four-eyes blockers; the generic approval command cannot close
  the task. Once all required risks are independently approved, an eligible
  user may request reserve calculation without submitting a financial value.
  The server performs two separate calculations, persists input/output hashes
  and binds the verified reserve to the model-declared BoQ component;
- a policy-authorised post-bid operator may create an actual revision only by
  selecting an eligible verified observation from the server-held actuals
  context. The browser submits no value, unit, source class or occurrence date;
  it binds the exact observation creation timestamp and approved actuals-policy
  version. A different policy-assigned reviewer sees source leaves, project
  outcome evidence, task version and integrity/four-eyes blockers. Only a
  current approved fact may be compared with a forecast from its released
  snapshot. Actual, variance and calibration history is loaded through a
  bounded opaque cursor tied to the current project, metric and approved policy;
  released forecast candidates are loaded separately for the selected verified
  actual. The comparison command names the exact release decision, which the
  backend retrieves and replays once with its fixed snapshot. A malformed,
  stale or cross-scope cursor is rejected rather than widened. The classifier
  chooses one closed-list reason and an explanation, while the backend computes
  exact Decimal absolute/relative variance. A separate reviewer must reproduce
  and decide that variance; approval creates a still-pending calibration
  candidate. Only an independent methodology owner may accept or reject the
  reproducible candidate, and the UI states that approval neither proves model
  accuracy nor authorises price release;
- an assigned expert may record an approval, rejection, or
  changes-requested decision for an existing approval task. The operation
  requires the exact task timestamp, a reason, project-scoped evidence
  identifiers, an idempotency key, and backend four-eyes eligibility. Approval
  additionally requires explicit project-code confirmation in the browser;
- an estimator may fix a calculation only from the complete server-generated
  candidate. The browser shows the exact evidence bases, controlled policy,
  quantities, rates, factors and candidate hash but submits only that hash,
  project row version, reason, project-code confirmation and acknowledgement.
  The server rebuilds the candidate under lock, performs the primary and
  independent calculations, stores a content-addressed snapshot and hides any
  later amount whose snapshot/run integrity check fails;
- at final review, a reviewer or approver may select exact current price rows
  or hard-stop findings and return them without entering a replacement price.
  A separate status panel shows whether the immutable request is awaiting
  dispatch, has one stage command queued, or is blocked. Queue delivery and
  acknowledgement are explicitly not rendered as completed recalculation;
- an approver may select internal or bid release only when the target decision
  has no hard-stop findings and the workflow state permits the transition.
  The screen renders every finding and a target-specific gate hash. Submission
  requires the exact project code, exact target state and an explicit
  four-eyes acknowledgement; the server rebuilds the complete gate and rejects
  stale hashes before persisting the immutable release decision and audited
  transition.

The backend remains authoritative and records every accepted mutation in the
audit chain. An unsupported file, invalid logical key, stale task or conflict,
stale document-set candidate, self-confirmation, self-resolution, missing
role, or unacknowledged command fails closed.

General-purpose workflow transitions and several specialist maintenance
surfaces are not represented as operator mutations.
Production acceptance still requires role-based user testing, accessibility
verification, organisation-owned methodology/catalog qualification, and
business-process qualification.

## Browser authentication

Staging and production use OIDC Authorization Code Flow with PKCE through a
public browser client:

- `OIDC_ISSUER` identifies the HTTPS authority;
- `OIDC_WEB_CLIENT_ID` identifies the public client and is mandatory when the
  UI is enabled;
- `OIDC_WEB_SCOPE` contains unique scopes and must include `openid`;
- access-token state is held in `sessionStorage`, not persistent browser
  storage or cookies;
- callback routes are `/auth/callback` and `/auth/signout-callback`.

The public `/v1/runtime-config` response contains only the issuer, browser
client ID, scopes, environment, authentication mode, application version,
API base path, and the server upload-byte limit needed for preflight feedback.
It must never contain API audiences, JWKS configuration, signing keys,
credentials, or adapter configuration.

Development header authentication is visible only when
`ALLOW_INSECURE_DEV_AUTH=true` in a development or test environment. Staging
and production reject that setting during startup.

`PUBLIC_DEMO_ENABLED=true` replaces the login screen with a public diagnostic
workbench built from a controlled, checked-in snapshot. It exposes the complete
23-row Alabuga BoQ extraction, quantities, research candidates from FGIS CS and
the open market, raw observed amounts, literal name differences, source links,
evidence hashes and blocker explanations. The FGIS coverage panel separates 60
catalog candidates from 783 exact code/period responses, 16 published
observations, and the four observations whose name and unit match literally.
Users can search, filter, select a row, expand its evidence and download the
matrix as CSV.

The public workbench itself sends no project API requests and ships no original
source archives. The snapshot generator validates the fail-closed state,
refuses an unreleased proposed price, restricts source links to HTTPS and
excludes local paths and organisation metadata. The setting does not relax API
authentication; anonymous reads and mutations still return `401`.

For a development/test showcase, all three `SHOWCASE_OPERATOR_*` settings may
enable the separate `/import` route. Its shared access code maps to a fixed
estimator/technical-expert identity, never appears in runtime config, remains
only in component memory, and cannot approve or release a price. The route
creates a real draft and uploads original bytes into quarantine with stable
idempotency keys. Every receipt exposes the real status and SHA-256. The
configuration is invalid in staging/production; a qualified scanner and
isolated worker are still required before an upload can leave quarantine.

Public demo cannot be combined with `ALLOW_INSECURE_DEV_AUTH` and requires the
operator UI to be enabled. Raw research values and accepted files are not a
released estimate.

The identity provider must register exact HTTPS redirect URIs. Wildcard
redirects, implicit flow, browser-held client secrets, and password grant are
not supported.

## Authorization and information barriers

The UI obtains data only from project-scoped read APIs. The API independently
checks the current organisation, project membership revision, and action role
on every request. Hiding a navigation item is usability behavior, not an
authorization control.

Financial totals, pricing, calculation, and relevant audit material are
redacted for technical-only memberships. A user without membership receives no
project existence disclosure. Membership revocation takes effect at the API
boundary even if a browser still holds previously rendered data; responses use
`Cache-Control: no-store`.

Portfolio and work-queue pagination is authorization-aware in SQL. The latest
membership revision, active status, actor identity role and exact task role are
filtered before `LIMIT`; the service does not fetch an arbitrary oversampled
page and discard unauthorized rows afterward. A stored membership role mask is
checked against the immutable role evidence before returned rows are rendered.

## Navigation and rendering safety

The client router accepts only same-origin absolute paths. Scheme-relative
paths, external URLs, backslashes, and NUL characters are rejected. Evidence
values are rendered as text by React; the UI does not inject source HTML.
Money uses decimal strings returned by the API and locale formatting without
using JavaScript floating-point arithmetic for calculation.

Document-upload polling treats only explicit server states as facts. A
`QUARANTINED`, `SCAN_FAILED`, `PROCESSING_FAILED`, or dead-lettered upload is
visible in the document registry as an intake record but is not a document
revision or extraction evidence. A browser timeout is not translated into
success.

Every project view preserves visible workflow state and blocker count,
including at mobile breakpoints. A blocked calculation must never be presented
as ready with minor remarks.

The wide BoQ pricing matrix deliberately uses a horizontally scrollable ledger
with a sticky original-position column. Source cells show the original and
source-side names together so an operator can verify the mapping without
opening a second screen. The direct source link is rendered only from the
server-validated HTTPS source passport; text remains escaped by React.

## Browser security policy

The API applies:

- a restrictive `default-src 'self'` Content Security Policy with OIDC
  connection origins enumerated exactly;
- `frame-ancestors 'none'`, `object-src 'none'`, and `base-uri 'none'`;
- `X-Content-Type-Options: nosniff`;
- `Referrer-Policy: no-referrer`;
- `Cross-Origin-Opener-Policy: same-origin`;
- `Cross-Origin-Resource-Policy: same-origin`;
- a restrictive Permissions Policy;
- HSTS in staging and production;
- `Cache-Control: no-store`.

Changing the identity-provider origins requires a corresponding CSP review.

## Build and deployment

Local development:

```powershell
cd web
npm ci
npm run dev
```

Production bundle and checks:

```powershell
npm run format:check
npm run build
npm test -- --run
npm audit
```

The Docker `operator-ui` stage installs the locked npm graph and builds
`web/dist`. The API image copies the bundle to
`src/tenderguard/web_dist`. When `OPERATOR_UI_ENABLED=true`, staging and
production startup fail if the bundle is unavailable. An explicit
`OPERATOR_UI_DIST_PATH` may be used by controlled non-container deployments.
Public source maps are disabled; a future error-monitoring integration must
upload source maps out of band and keep them outside the served asset tree.

Readiness reports the operator UI asset check. This is a deployability signal,
not evidence of user training or process acceptance.

## Verification and operational evidence still required

Before production use, retain:

- identity-provider client configuration and redirect-URI review;
- role-by-role acceptance results, including revoked membership and
  cross-organisation non-disclosure;
- desktop/mobile browser compatibility and accessibility results;
- dependency and image vulnerability scans tied to the released digest;
- CSP violation monitoring and authentication-failure alerts;
- controlled-action workflow tests for every additional mutation surface;
- actuals role separation, blocked-source, stale observation/task, released
  forecast, rejected variance, methodology-owner and mobile operator
  acceptance results;
- manual-evidence policy binding, self-review, stale-document-set, exact-value
  and rejection/return operator acceptance results;
- quarantine allowlist, size-limit, retry/idempotency, infected-file, scanner
  outage and dead-letter operator drills;
- user training and named process-owner approval.

Successful local screenshots or a technical demo do not satisfy these gates.
