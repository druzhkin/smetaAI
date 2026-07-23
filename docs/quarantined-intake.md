# Quarantined document-intake contract

## Purpose

Tender files are attacker-controlled. The API therefore does not parse an
uploaded PDF, spreadsheet, image, Office package, or archive and does not place
it in the evidence object store. It streams the `UploadFile` spool into a
separate content-addressed quarantine store, enforces the configured byte
limit, records the SHA-256 identity, creates an input-integrity blocker, and
returns HTTP `202`.

For a current-candidate revision, the project loses its confirmed document-set
binding immediately. A draft moves to `DOCUMENTS_INCOMPLETE`; an advanced
mutable project moves to `BLOCKED`. A scan or parsing failure is never reported
as a successful document revision.

## State machine

| From | To | Authority and condition |
|---|---|---|
| `QUARANTINED` | `CLEAN` | `SYSTEM`; exact object hash; active configured `MALWARE_SCAN` qualification; reproducible immutable report hash |
| `QUARANTINED` | `REJECTED` | same authority; `INFECTED` verdict with named threat |
| `QUARANTINED` | `SCAN_FAILED` | same authority; scanner `ERROR` verdict |
| `SCAN_FAILED` | `CLEAN`, `REJECTED`, `SCAN_FAILED` | a new qualified scanner run; run IDs are unique and immutable |
| `CLEAN` | `PROCESSING` | separately launched `SYSTEM` worker; exact active `DOCUMENT_INTAKE` qualification |
| `PROCESSING` | `PROCESSED` | quarantine hash reproduced after promotion; bounded inspection completed; document revision and manifest committed atomically |
| `PROCESSING` | `PROCESSING_FAILED` | unexpected worker failure; only the error type is persisted |
| `PROCESSING_FAILED` | `PROCESSING` | explicit isolated-worker retry while the exact clean scan remains valid |

There is no user, reviewer, administrator, or API action that changes an
upload to `CLEAN` or invokes a parser in the API process.

Only one unresolved current-candidate upload may exist for a project/logical
document key. This is checked by the application and a partial unique database
index. A rejected infected upload may be replaced; its blocking findings are
resolved only after a clean replacement has produced a document revision.

## Scanner result evidence

`POST /v1/projects/{project_id}/document-uploads/{upload_id}/scan-results`
accepts a result only from a `SYSTEM` identity. The result must bind:

- configured and active adapter qualification;
- exact quarantine SHA-256;
- unique external scanner run ID;
- definitions version and completion time;
- `CLEAN`, `INFECTED`, or `ERROR` verdict;
- canonical scan report whose SHA-256 is reproduced by the application.

PostgreSQL prevents update/delete of scan-result rows. Project and
quarantined-upload audit chains record the transition.

## Worker boundary

Run one upload through:

```powershell
tenderguard process-quarantined-upload --upload-id <opaque-upload-id>
```

Build the parser runtime with Docker target `document-worker`. The default
`api` target deliberately omits `openpyxl`, `Pillow`, and `pypdf`.

This command is the parser entry point and must run in a disposable,
network-denied container or sandbox with:

- read-only application image and no API/OIDC signing credentials;
- access only to quarantine read, evidence-store write, and scoped database
  operations;
- CPU, memory, process, temporary-disk, and wall-clock limits;
- patched PDF, Office, image, and archive libraries;
- central logs and job-level metrics;
- an active approved processor qualification matching configuration.

Archive members are decompressed through bounded streams into
`SpooledTemporaryFile`; the memory threshold is
`MAX_PARSER_SPOOL_MEMORY_BYTES`, after which data spills to worker-local disk.
`ZipFile.read` and API-level whole-file reads are not used.

The repository supplies the state machine, evidence contract, bounded parser,
worker entry point, and tests. It does **not** supply or qualify an
organisation's malware product, container runtime policy, network policy,
secrets manager, or worker orchestrator. Production readiness remains blocked
until those controls are deployed and evidenced.
