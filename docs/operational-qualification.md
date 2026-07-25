# Operational qualification controls

This contract covers the repository-supported parts of load qualification and
backup/restore verification. It does not turn a local test into production
acceptance. Both commands require an independently approved controlled version,
bind the exact profile hash, emit a content hash over the complete result, and
still require independent review.

## Governed profiles

Create the profile through the normal controlled-version workflow. The creator
and approver must be different methodology owners. Do not insert a profile
directly into the database.

- Recovery uses kind `recovery_profile` and schema
  `tenderguard.recovery-profile/v1`.
- Load uses kind `load_test_profile` and schema
  `tenderguard.load-profile/v1`.

The runner verifies the current payload hash, `APPROVED` state, approval
metadata, the full controlled-version audit chain, and the independently
supplied `--expected-profile-hash`. A modified row, unknown historical HMAC key,
creator/approver collision, or mismatched hash blocks before verification or
traffic starts.

The exact immutable application build reference is also mandatory and must be
an OCI-style `sha256:<64 lowercase hex>` digest or a full
`git:<40-or-64 lowercase hex>` object ID; labels such as `latest` are rejected.
RPO, RTO, latency, throughput, success ratio, representative endpoints,
concurrency, duration, and request count are owner-supplied fields. The
application has no fallback values and must not invent them.

## Recovery exercise

The recovery command is read-only with respect to the restored application
database and object stores. Run it only against an isolated restore
environment:

```powershell
uv run tenderguard verify-restored-system `
  --profile-version-id <approved-version-id> `
  --expected-profile-hash <sha256-from-independent-change-record> `
  --exercise-manifest <exercise.json> `
  --output <new-result-path.json>
```

The exercise manifest uses schema `tenderguard.recovery-exercise/v1` and binds:

- exercise, controlled-profile ID and hash;
- source and isolated restore environments;
- incident, restored database point, and restoration start timestamps;
- exact database and object-store backup references;
- identity, connector, and secrets-manager evidence references;
- executor and approved change reference.

Timestamps must carry a timezone. The restored point cannot be after the
incident; restoration cannot start before it. An output path is created
exclusively and is never overwritten.

The verifier checks:

1. exact approved application build and Alembic head;
2. evidence and quarantine-store reachability;
3. the approved WORM requirement;
4. required OIDC, signing, and current adapter bindings;
5. every controlled-version content hash;
6. every document-set manifest and current project binding;
7. every referenced original and quarantine object, including SHA-256, key,
   and size;
8. every fixed calculation snapshot, primary replay, independent replay,
   calculation-run record, atomic-input record, controlled version, and
   evidence basis;
9. every stored scenario, approved scenario policy/evidence, primary result,
   and independent replay;
10. every profile-named golden snapshot;
11. every signed export package, embedded snapshot, signature, and packaged
    audit chain;
12. every current audit chain, every checkpoint object and terminal, and the
    external anchor when required;
13. achieved RPO and end-to-end RTO against the approved values.

`TECHNICAL_VERIFICATION_PASSED` means only that these machine checks passed.
The result carries `independent_reviewer_signoff_required=true`. The reviewer
must independently validate backup, identity, connector, secrets-manager, and
change references; confirm that the source point is authentic; record the
exercise in the readiness evidence registry; and approve it before the
`backup_restore` gate can be marked `PASSED`.

Missing profile approval, malformed evidence, an unavailable dependency, or a
preflight error yields `BLOCKED`. An integrity or SLO mismatch yields `FAILED`.
Neither status may be converted to a pass by editing the JSON result.

## Load qualification

The load runner accepts only `GET` and `HEAD`. Endpoint paths are
origin-relative, redirects are disabled, response bodies are bounded, and
credentials may only be read from the approved environment-variable name.
Secrets are never placed in the profile or result.

```powershell
uv run tenderguard run-load-qualification `
  --profile-version-id <approved-version-id> `
  --expected-profile-hash <sha256-from-independent-change-record> `
  --output <new-result-path.json>
```

The profile defines an HTTPS origin (plain HTTP is allowed only for loopback
testing), exact target environment/build, duration, concurrency, exact request
cap, timeout, response limit, weighted endpoints, expected statuses, and
mandatory overall/per-endpoint SLOs. Before workload traffic, the runner reads
`/v1/runtime-config` and blocks on an environment/build mismatch. A production
target additionally requires an explicit production flag and change reference
in the four-eyes-approved profile. This is a safety interlock, not general
permission to load-test production.

Results contain counts, bounded error categories, HTTP status distribution,
nearest-rank p50/p95/p99 latency, exact Decimal success ratio and throughput,
the approved thresholds, and a hash over the complete result. Every overall
and per-endpoint condition must pass. Averaging a slow or failing endpoint into
a healthy aggregate cannot hide it.

The built-in runner is appropriate for controlled single-node API
qualification. It does not claim distributed internet-scale generation,
browser rendering performance, database saturation analysis, upload/archive
abuse coverage, or soak testing. Where the approved workload requires those
capabilities, use a separately qualified generator and import its signed
evidence under the same readiness gate.

## Evidence handling

Keep the profile hash in a channel independent of the restored database and
result file. Preserve:

- approved profile and audit history;
- exercise/change ticket;
- runner version/commit;
- immutable result JSON and its `result_hash`;
- infrastructure telemetry for the exact interval;
- deviations, incident records, and remediation retest;
- independent reviewer identity, decision, and timestamp.

Do not approve `production_qualification` merely because the command returned
zero. The complete gate still requires the owner, environment, reviewer,
evidence hash, and all organisation-specific qualification work.
