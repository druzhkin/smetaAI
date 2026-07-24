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
| `QUARANTINED` | `CLEAN` | exact `SYSTEM` service identity bound to the active configured `MALWARE_SCAN` qualification; exact object hash; reproducible immutable report hash |
| `QUARANTINED` | `REJECTED` | same authority; `INFECTED` verdict with named threat |
| `QUARANTINED` | `SCAN_FAILED` | same authority; scanner `ERROR` verdict |
| `SCAN_FAILED` | `CLEAN`, `REJECTED`, `SCAN_FAILED` | a new qualified scanner run; run IDs are unique and immutable |
| `CLEAN` | `PROCESSING` | leased `document.upload.scan-clean` delivery; separately launched `SYSTEM` worker; exact active `DOCUMENT_INTAKE` qualification |
| `PROCESSING` | `PROCESSED` | quarantine hash reproduced after promotion; bounded inspection completed; document revision and manifest committed atomically |
| `PROCESSING` | `PROCESSING_FAILED` | timeout or worker failure; only a machine error code and error type are persisted |
| `PROCESSING`, expired lease | `PROCESSING` | a new worker atomically reclaims ownership and receives a new token |
| `PROCESSING_FAILED` | `PROCESSING` | outbox retry after bounded exponential delay while exact scan and qualifications remain valid |
| `PROCESSING`, `PROCESSING_FAILED`, `CLEAN` | `PROCESSING_DEAD_LETTERED` | processing or delivery attempt budget exhausted; original input blocker remains unresolved |
| `PROCESSING_DEAD_LETTERED` | `CLEAN` | audited `ADMIN` replay after incident resolution; creates a new outbox event and preserves the terminal event |

No user, reviewer, administrator, or API action can assert an initial malware
`CLEAN` verdict or invoke a parser in the API process. Administrator replay
only restores a previously scanned upload to the delivery queue; the worker
revalidates the exact CLEAN evidence and both active qualifications.

Only one unresolved current-candidate upload may exist for a project/logical
document key. This is checked by the application and a partial unique database
index. A rejected infected upload may be replaced; its blocking findings are
resolved only after a clean replacement has produced a document revision.

## Scanner result evidence

`POST /v1/projects/{project_id}/document-uploads/{upload_id}/scan-results`
accepts a result only from the exact `SYSTEM` service identity recorded in the
configured qualification. The result must bind:

- configured and active adapter qualification;
- organisation and service actor identity;
- exact quarantine SHA-256;
- unique external scanner run ID;
- definitions version and completion time;
- `CLEAN`, `INFECTED`, or `ERROR` verdict;
- canonical scan report whose SHA-256 is reproduced by the application.

PostgreSQL prevents update/delete of scan-result rows. Project and
quarantined-upload audit chains record the transition.

## Durable delivery contract

The scan-result transaction writes `document.upload.scan-clean` to the outbox.
A dispatcher claims one available row with `FOR UPDATE SKIP LOCKED`, a random
ownership token, worker identity, attempt number, and expiry. A different
worker cannot acknowledge or reject that claim. After expiry, a new claim
replaces the token; stale completion cannot finalize the document.
The configured and qualification-bound service actor and the per-container
worker instance ID are stored
separately so parallel replicas remain attributable without minting new
application identities.

The document upload has an independent processing lease and a shorter
persisted deadline. Claim, finalize, and failure are short transactions.
Quarantine/evidence I/O and all parser calls occur between them without an
open database transaction. The service checks the deadline before and after
the expensive phases and again before commit. An external container timeout is
still mandatory because Python cannot safely interrupt every native parser.

Retry delay grows exponentially within configured bounds. When the configured
attempt budget is exhausted, both delivery and upload become terminal and the
input-integrity blocker remains open. PostgreSQL prevents update/delete of
terminal outbox rows. Replay never edits that row: it requires an
administrator, a reason, exact prior CLEAN evidence, and creates a new event.

## Worker boundary

Run one upload through:

```powershell
tenderguard process-quarantined-upload --upload-id <opaque-upload-id>
```

Drain a bounded batch through:

```powershell
tenderguard dispatch-document-intake --max-events 10
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

The repository supplies the state machine, evidence contract, leased
dispatcher, bounded retry/dead-letter/replay, bounded parser, worker entry
points, and tests. It does **not** supply or qualify an organisation's malware
product, production scheduler, container runtime policy, network policy,
secrets manager, or worker infrastructure. Production readiness remains
blocked until those controls are deployed and evidenced.
