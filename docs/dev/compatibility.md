# Compatibility

Requires **Open WebUI 0.12.0 or newer** with the host contracts below.

The adapter checks concrete capabilities before use. Those checks can reject an
incompatible host; they cannot prove every frontend or permission behavior. Test
[installation acceptance](../ops/install.md#check-your-installation) on your own
host/provider before enabling the Tool for users.

## Required host contracts

| Area | Required behavior |
| --- | --- |
| Database | Async SQLAlchemy engine and shared ORM metadata; PostgreSQL or local-disk SQLite; permission to create five Tool tables |
| Host models | Compatible File, Chat, ChatFile and normalized ChatMessage fields, types and identities |
| Authorization | Async user lookup, live chat-write checks, native file-access checks and genuine injected actor/chat/assistant/request context |
| Storage | Compatible synchronous provider upload/read methods and normal authenticated download routes |
| Execution | Native function calling, Pyodide engine, host blocked-module policy and a connected browser |
| Transport | Serialized metadata below 16 MiB and callback waits below 60 seconds; provider limits may be lower |
| Binary Files | Same-origin worker fetch with native cookie auth, canonical content GET and multipart upload with `process=false`; native metadata retained for upload reconciliation |

Binary routes use the canonical same-origin `/api/v1/files/` API, matching the
inspected Open WebUI 0.12.3 and 0.13.3 root-mounted frontends. Arbitrary subpath hosting is not
claimed. The existing worker must be able to send native authentication cookies.

Publication updates an existing assistant message's `files`/`updated_at`, Chat JSON,
ChatFile links and the operation receipt in one transaction. It preserves message
content/output. Status and retry do not require an active execution engine or browser.

Extra nonunique indexes are allowed. Conflicting expected indexes, uniqueness,
foreign keys or incompatible state are rejected rather than repaired or deleted.
Do not bypass a check by changing a version label or patching host code.

### Empty files

New empty retained files require native `STORAGE_PROVIDER=local` and the
filename-qualified content route. Their exact representation is `path=""`,
`data.content=""`, an integration marker, and a committed receipt slot with zero
size and the empty SHA-256. Missing nonempty provider bytes never become fallback
text or an empty file.

Native S3/GCS/Azure deletion cannot safely handle this representation, so new empty
Files are rejected for those and unknown providers. Previously saved empty Files
remain readable. Remote-provider behavior otherwise needs its own qualification.

## Automated coverage

- Python checks with real disk SQLite and PostgreSQL, including Tool
  initialization, publication and fresh restoration through synthetic host
  and callback boundaries.
- A Chromium module worker using the production wrapper and public Pyodide APIs:
  shared Python/shell files, 256 MiB scratch, 32 MiB binary shell operations,
  retained deltas, fresh-worker restoration and rejected links.
- Fixed-root calls and saved-data compatibility tests.

The [CI workflow](../../.github/workflows/checks.yml) defines the Python matrix.
The [test guide](testing.md) explains these boundaries and the native acceptance
checklist. Automated fixtures do not establish native Open WebUI acceptance or a
browser-memory capacity guarantee. Portable Tool/build Python support does not
extend Open WebUI's own host Python requirements.

## Recorded browser checks

These checks apply to the Tool versions listed below. New builds need verification
appropriate to their changes and claims, as described in [testing](testing.md).
The observed configuration used Open WebUI 0.12.2, PostgreSQL
and local storage. It was not established as a pristine upstream installation;
no frontend patch was added for this Tool.

| Tool version and file checksum | Observed native behavior |
| --- | --- |
| `0.2.0a2`, SHA-256 `207e92d57f9ab69bf97cc9c79527b989523ae3b86c3fbfb5d8d79d53605c05bd` | Genuine Python → same-page Bash → fresh-browser saved-program rerun; empty imported initializer; 128 MiB warm scratch absent after restoration; original input reference and unchanged File ID reuse; new/warm/restored context |
| Same a2 file | Empty outputs, including `html` and Unicode names; duplicate basenames; nonempty→empty→nonempty immutable IDs; owner/recipient inline downloads with exact bytes/names; foreign/private denial and revocation/regrant; older saved links and a1 receipts readable |
| `0.2.0a1`, SHA-256 `7c3a04c4d152f2fdd24e5fc24dbb039ee5756b01e3baea5b27e958dc7a0bfad7` | Python/Bash shared files; warm scratch and fresh restoration; saved-program rerun; unchanged File ID reuse; original native CSV without duplicate upload; owner/recipient downloads after reload; foreign/private denial, revocation and deleted-input rejection |

Empty-file deletion was checked separately with a manually created native File
using the exact representation, not end to end on a Tool-issued empty File.
For these a1/a2 records, storage-failure diagnostics used synthetic callbacks
and real databases; native provider fault injection and a complete interruption
matrix were not performed.
These observations do not qualify S3, GCS or Azure, or establish runtime acceptance
of the installer minimum 0.12.0.

## 0.2.0a4 native qualification

Artifact SHA-256
`fb556bb227ff1a3d7e2cd25e35b27c5460fddac99af56a061c678bbee1a701f4`
was checked through native browser calls on PostgreSQL-backed Open WebUI 0.12.3
with local customizations and local storage. This was not a pristine upstream runtime.

- Python/Bash, same-page continuation and fresh-browser restoration of saved state.
- Explicit dependency recipes with packages genuinely absent in the fresh worker,
  including `xlrd`; valid DOCX/XLSX byte hashes and unchanged office File ID reuse.
- Snapshot-limit failure preserving received completion/stdout; the first refused
  new execution allowed status, and another refusal stopped the response.
- Recovery of the original completed bytes after an operator limit adjustment:
  one operation, execution counter one, and no source replay.

Earlier restricted probes and a prior candidate separately checked native user
cancellation, late callbacks, sequential stopping and partial-provider-upload
recovery. They are not exact-final-artifact acceptance. A supplemental two-tab
probe encountered an API-read timeout and did not qualify that scenario.
Native stop remains opt-in. These checks do not qualify pristine upstream runtime
behavior, delegated tasks, approval/resume or preservation of other calls' results
in the same host batch. See [limitations](../user/limitations.md).

## 0.2.0a5 native qualification

Artifact SHA-256
`14f7c178b91a3d3f3aada87ea65c697926e935eb88c627d03001272fa6547468`
was checked through ordinary assistant calls in Chromium on PostgreSQL-backed
Open WebUI 0.12.3 with local customizations and local storage. This was not a
pristine upstream runtime. Optional native stopping was enabled.

- Bash published a baseline output, then Python completed once before its real
  9 MiB output exceeded the default retained-file limit.
- Separate prepare/execute/snapshot callbacks were observed. The first snapshot
  failure returned normal Tool guidance with received completion/stdout, allowing
  the assistant to finish its answer without a native executor error.
- After an operator limit adjustment, source-free recover saved the same operation
  and exact 9 MiB output. A subsequent Python call verified execution counter one
  and ordinary same-page continuation.
- A separate ordinary workflow generated DOCX/XLSX, shared saved state with Bash,
  and restored exact document hashes in a fresh worker; original downloads stayed readable.

This focused check does not qualify network disconnect/reconnect, repeated native
cancellation, sharing or other storage providers for
these exact bytes. The earlier a4 evidence remains scoped to that artifact.

## 0.2.0a6 native qualification

Artifact SHA-256
`779595e6204edaa7e465643c012b0cef64565249cb4d05a89172333720da86c4`
was checked through ordinary assistant calls and real served workers in Chromium
on PostgreSQL-backed Open WebUI 0.12.3 with local customizations and local storage.
This was not a pristine upstream runtime. Native source was not changed for the Tool.

- After an accepted request, closing the originating WebSocket preserved its page
  and worker. The Tool recognized that interpreter under its replacement session,
  rejected another same-user/same-chat worker, and dispatched the program once.
  The downloaded bytes matched; a later call verified execution counter one.
- Destroying the original page returned not-started guidance and sent no program
  to the remaining same-user/same-chat tab.
- Stop while reconnection was unavailable caused zero program dispatches.
- A real program-start marker preceded a mid-execution socket close. The Tool
  preserved unknown outcome with one dispatch and did not replay source.

These focused checks do not qualify pristine upstream installations, delegated
or approval/resume paths, remote storage, lost-restore acknowledgement replay,
provider-stream continuation or recovery of a destroyed interpreter.

## 0.3.0a1 native qualification

The assembled Tool SHA-256 is
`3992ca5a843909ef647f160c01f327126b3d14d53ac7370d66181f4eb02deb9a`.
Scoped native checks used Open WebUI 0.12.3 with local customizations,
PostgreSQL and local storage; this was not a pristine upstream installation.

- Exact 20 MiB native input and output bytes, three-file 60 MiB output,
  fresh-worker restoration and authenticated shared downloads matched hashes.
  Foreign users could not read outputs; private checkpoints/scripts stayed private.
- Lost HTTP upload responses and lost upload callbacks were tested separately.
  Recovery adopted the original verified File without replaying user source.
  Explicit Stop retained an unknown-outcome fence until a separate recovery
  request obtained completion evidence in the original worker.
- Ordinary errors remained available to the configured agent. An empty call and
  an oversized selected input were corrected within their respective requests.
  With a configured lower file limit, explicit selection excluded a new oversized
  input; `[]` preserved the acknowledged inherited input.
- Fresh-worker PIL/xlrd loading, permitted legacy docx loading, explicit imports
  before a saved script and fresh globals were checked through native calls.

These checks do not qualify every host/provider, guarantee arbitrary program
memory use or recover bytes after the original worker is lost. Native upload
policy can reject a file before a File ID or Tool invocation exists.

## 0.3.0a2 native qualification

The assembled Tool SHA-256 is
`491db343032c139b5bd5df2c32caf41240b390a798ca4c9d676ac15429108b49`.
Focused native checks used Chromium, Open WebUI 0.12.3 with local customizations,
PostgreSQL and local storage; this was not a pristine upstream installation.

- Three 20 MiB outputs matched downloaded hashes. Long ASCII and Unicode workspace
  names survived fresh-worker restoration while native upload names stayed bounded.
- A lost HTTP upload response was recovered by electing the original verified
  File ID. Recovery did not replay user source; execution counters remained one.
- Ordinary agent checks used both requested inputs, answered an English request
  in English, inspected current limits before splitting a 21 MiB output, and
  corrected a Python program error within the same Russian-language response.
- Callbacks contained metadata, with a largest observed reply of 1756 bytes.
  The disposable browser/harness container peaked at 795230208 bytes under a
  2 GiB cap. This is a synthetic workload measurement, not a parser-memory guarantee.

Automated real-database checks also cover revoked access or replaced generations
before dispatch, bounded optional status delivery, and preserving saved results
when presentation fails. These fixtures do not establish native fault delivery on
every host. Earlier qualification remains scoped to its recorded artifact.

## 0.3.0a3 native backward compatibility

Candidate Tool SHA-256:
`6916a3921d031010a8e15e80020fb342388f30c37e0b52e0570e1f8d3c54feea`.
Focused native checks used Chromium, PostgreSQL, local storage and Open WebUI
0.12.3 with local customizations. See the separate
[native Open WebUI 0.13.3 check](#native-open-webui-0133) for the same artifact.

- Russian and English program failures produced readable failed cards after
  reload; the agent corrected the source and returned the requested language.
- Exact 20 MiB downloads and fresh-worker restoration preserved bytes and an
  execution counter of one. DOCX, XLSX, PNG, CSV and JSON results passed semantic checks.
- Native Stop, socket reconnection and lost upload acknowledgement retained
  execution fences. Manual source-free recovery preserved completed results;
  upload reconciliation reused the original File ID.
- Repeated failed recovery produced a saved native error without executing the
  source again. Separate host-limit checks preserved available partial results;
  those response budgets belong to that host, not this Tool.

Writer/socket failure delivery was checked with real-database fixtures and
synthetic host boundaries; this does not qualify visibility during a native host outage.

## Native Open WebUI 0.13.3

Candidate Tool SHA-256
`6916a3921d031010a8e15e80020fb342388f30c37e0b52e0570e1f8d3c54feea`
was checked in Chromium against the unmodified official Open WebUI 0.13.3 image,
with PostgreSQL, local storage and native Pyodide file persistence enabled.
Optional native response stopping was enabled.

- Russian and English program errors remained readable in failed cards after
  reload. The agent received each error, corrected the source and answered in
  the requested language.
- A 20 MiB output matched its downloaded hash and survived fresh-browser
  restoration with an execution counter of one. Native CSV inputs, XLSX formulas,
  DOCX structure and PNG dimensions passed content checks; DOCX generation used
  the standard library, so this does not qualify the `python-docx` package.
- A 21 MiB output completed execution but exceeded the 20 MiB per-file limit.
  The agent explained that saving failed without replaying the program.
- Native Stop, a 65-second browser disconnection and a lost HTTP upload response
  preserved execution fences. Source-free recovery saved the original result;
  upload reconciliation retained the original File ID.
- Repeated failed recovery saved a readable native error. Owner and authorized
  recipient downloads from a shared chat matched the 20 MiB output after reload;
  foreign access was denied and private checkpoints/scripts remained private.

These checks qualify the listed Tool workflows, not whole-response limits or
every host/provider. A separate native two-round-limit check ended with raw
model control text and no saved error on the tested model routes; finite native
iterations alone did not establish reliable error presentation. The Tool does
not supply a shared budget for other Tools or subagents. Remote storage,
approval/resume, subagents and same-batch output preservation remain unqualified.

## Version-sensitive integration

Pre-dispatch reconnection is restricted to the inspected Open WebUI 0.12.3 and 0.13.3
authenticated session-pool and event-factory contracts and a bound native task
generation. Other hosts retain their injected callback and normal failure path.
The candidate must match the original runtime UUID, not merely the user/chat.
It does not resume unknown execution or recover a terminated/reloaded worker.
Native qualification for this feature must be recorded separately from callback
fixtures and the standalone Chromium wrapper test.

Scoped 0.13.3 runtime acceptance is recorded [above](#native-open-webui-0133).
Its [socket implementation](https://github.com/open-webui/open-webui/blob/v0.13.3/backend/open_webui/socket/main.py)
retains authenticated session batches and does not automatically resend
`execute:python` on disconnect. The
[normalized message writer](https://github.com/open-webui/open-webui/blob/v0.13.3/backend/open_webui/models/chat_messages.py)
retains its caller-owned transaction contract. Source inspection alone does not
qualify frontend execution, recovery, permissions or cancellation on another installation.

Reconnection stays within the configured callback timeout, probes at most eight
sessions and waits at most five seconds per candidate. A timed-out candidate is
not retried within that attempt; an inactive tab can therefore remain unavailable.

`execute:python` is checked against upstream source, but is not documented as a
stable extension API. Recheck it when upgrading Open WebUI. Filesystem/bootstrap
code uses public Pyodide APIs and the public npm `just-bash/browser` export.

Empty download and local deletion contracts were checked against official
[0.12.0 file routes](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/routers/files.py)
and [0.12.2 storage providers](https://github.com/open-webui/open-webui/blob/v0.12.2/backend/open_webui/storage/provider.py).
Source inspection is not runtime acceptance for every host.

## Saved-data compatibility

**0.3.0a1:** reads earlier receipts/checkpoints and preserves existing native Files.
New receipts include optional native-transfer reservations; diagnostics add
structured failure facts. Earlier strict readers reject these new fields.
After writing new state or larger files, keep a compatible reader and limits;
do not downgrade to an older binary or delete state to force compatibility.

**0.2.0a6:** adds one Tool-owned browser-continuity table without changing the
four earlier tables or receipt JSON. Earlier a5 readers ignore that additional
table and retain saved-data access; they do not implement automatic reconnect.
Preserve the extra table during rollback. Runtime UUIDs describe continuity,
not authenticated execution evidence or a new access-control authority.

**0.2.0a5:** preserves a4 receipt/table formats. New executions acknowledge
completion before a separate source-free snapshot callback. Recovery instructions
distinguish pre-dispatch failure, unknown execution and received completion. The
first browser failure returns guidance; optional cancellation applies to repeated
failures/refusals. Successful retry/recover releases only the current request's
gate for the acknowledged operation, preserving failed-session guards. Scratch
stdout/stderr logs now share the configured per-stream text cap. Automated tests
cover the new protocol; native coverage is limited to the focused a5 check above.

**0.2.0a4:** creates additive diagnostics/gate tables without changing existing
receipt JSON, Files or head semantics. Earlier readers can still read those
receipts but ignore the new execution safeguards; disable new execution before
rolling back. Unknown legacy operations at the current revision remain fenced;
older operations below an acknowledged head remain historical and cannot win CAS.

Optional native response stopping is restricted to the inspected 0.12.3 and 0.13.3 contracts
and disabled by default. Ordinary cancellation/UI behavior and repeated failure
stopping were observed on 0.12.3 and stock 0.13.3 installations. Same-batch output
preservation, approval/resume and subagent paths are not qualified. Requalify host upgrades.

**0.2.0a3:** every call starts at the workspace root. The model-facing `cwd`
argument is removed; saved files, paths and receipts require no migration.
For an older program that expects another directory, change to its recorded
`cwd` inside the program before running the saved source. That change does not
carry over to the next call. Historical records retain the original cwd.

**0.2.0a2:** reads earlier receipts and version-one checkpoints. Older `0.2.0a1`
binaries reject new receipt fields and FileSlot representations, even for nonempty
files. Once a2 or newer has written receipts, keep a compatible reader.

Disabling the Tool stops new calls. Replacing it with an older binary does not
restore compatibility. Preserve the database and native Files; never delete or
rewrite them to force a downgrade. See [removal](../ops/install.md#stop-or-remove-the-tool)
and the [legacy-data reference](tool-reference.md#empty-files-and-older-saved-data).
