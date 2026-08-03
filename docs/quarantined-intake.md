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

The operator UI submits the same governed command and displays a quarantine
receipt with the upload ID, exact SHA-256, byte count, malware verdict,
processing attempts, failure code, and processed revision ID. It polls the
read endpoint without inferring success from elapsed time. Quarantined uploads
may appear in the document register as `QUARANTINED_UPLOAD` records, but they
remain distinct from document revisions and observations.

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

`PROCESSED` means that the worker completed and committed its result; it does
not mean that intake passed. The authoritative intake outcome is
`manifest.all_files_processed`. The upload receipt UI renders a false value as
`BLOCKED`, treats a missing manifest on `PROCESSED` as `BLOCKED`, lists the
bounded findings and explicitly keeps structural intake separate from
evidence, methodology, independent validation, and bid release. Domain
validation rejects a serialized manifest when `all_files_processed` contradicts
blocker findings or file-health flags.

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

OOXML inspection rejects packages that do not contain the exact required main
part, content-type declaration, package relationship, and expected main XML
root for their `.docx`, `.xlsx`, `.xlsm`, or `.pptx` suffix. Duplicate or
case-colliding parts and unsafe or malformed XML are blockers. Every XML part
is streamed through a parser that forbids DTD, entity, notation, and external
entity declarations before any domain use. Non-hyperlink external
relationships and hyperlink targets outside `http`, `https`, or `mailto` are
blockers; ordinary hyperlinks using those schemes are bounded warnings
recorded by relationship type, URI scheme, and target hash without persisting
the target URL.

Excel inspection fails closed on hidden content, missing formula caches,
formula error tokens, cached formula error results, non-formula error values,
and external workbook links. Findings include bounded sheet/cell locators.
Passing this structural check does not validate a workbook's rates, tax basis,
formula methodology, totals, or release price; those still require governed
evidence and independent recalculation.

The repository supplies the state machine, evidence contract, leased
dispatcher, bounded retry/dead-letter/replay, bounded parser, worker entry
points, and tests. It does **not** supply or qualify an organisation's malware
product, production scheduler, container runtime policy, network policy,
secrets manager, or worker infrastructure. Production readiness remains
blocked until those controls are deployed and evidenced.
