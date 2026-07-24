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

The current interface includes three controlled mutation families:

- an estimator may register a project in `DRAFT`; the organisation comes from
  the authenticated identity, the creator receives a versioned owner
  membership, and creation does not imply document completeness;
- an estimator or technical expert may stream a supported file into the
  separate quarantine store. The browser binds document metadata, criticality,
  current-candidate intent, reason, acknowledgement, and a stable idempotency
  key. The receipt exposes the byte count, SHA-256, scan/processing state and
  resulting revision only if one is actually created;
- an assigned expert may record an approval, rejection, or
  changes-requested decision for an existing approval task. The operation
  requires the exact task timestamp, a reason, project-scoped evidence
  identifiers, an idempotency key, and backend four-eyes eligibility. Approval
  additionally requires explicit project-code confirmation in the browser.

The backend remains authoritative and records every accepted mutation in the
audit chain. An unsupported file, invalid logical key, stale task, missing
role, or unacknowledged command fails closed.

Extraction correction, conflict resolution, BoQ and price maintenance,
calculation execution, workflow transition, and bid release are not yet
represented as delivered UI operations. Production acceptance also requires
role-based user testing, accessibility verification, and business-process
qualification.

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
- quarantine allowlist, size-limit, retry/idempotency, infected-file, scanner
  outage and dead-letter operator drills;
- user training and named process-owner approval.

Successful local screenshots or a technical demo do not satisfy these gates.
