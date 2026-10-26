# Compatibility

Requires **Open WebUI 0.12.0 or newer** with the host contracts below.

The adapter checks concrete capabilities before use. Those checks can reject an
incompatible host; they cannot prove every frontend or permission behavior. Test
[installation acceptance](../ops/install.md#check-your-installation) on your own
host/provider before enabling the Tool for users.

## Required host contracts

| Area | Required behavior |
| --- | --- |
| Database | Async SQLAlchemy engine and shared ORM metadata; PostgreSQL or local-disk SQLite; permission to create four Tool tables |
| Host models | Compatible File, Chat, ChatFile and normalized ChatMessage fields, types and identities |
| Authorization | Async user lookup, live chat-write checks, native file-access checks and genuine injected actor/chat/assistant/request context |
| Storage | Compatible synchronous provider upload/read methods and normal authenticated download routes |
| Execution | Native function calling, Pyodide engine, host blocked-module policy and a connected browser |
| Transport | Serialized payloads below 16 MiB and callback waits below 60 seconds; provider upload limits may be lower |

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

This focused check does not qualify network disconnect/reconnect, repeated native
cancellation, fresh-browser restoration, sharing or other storage providers for
these exact bytes. The earlier a4 evidence remains scoped to that artifact.

## Version-sensitive integration

`execute:python` is checked against upstream source, but is not documented as a
stable extension API. Recheck it when upgrading Open WebUI. Filesystem/bootstrap
code uses public Pyodide APIs and the public npm `just-bash/browser` export.

Empty download and local deletion contracts were checked against official
[0.12.0 file routes](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/routers/files.py)
and [0.12.2 storage providers](https://github.com/open-webui/open-webui/blob/v0.12.2/backend/open_webui/storage/provider.py).
Source inspection is not runtime acceptance for every host.

## Saved-data compatibility

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

Optional native response stopping is restricted to the inspected 0.12.3 contract
and disabled by default. Ordinary native cancellation/UI behavior was observed on
a 0.12.3 installation; pristine stock, same-batch output preservation,
approval/resume and subagent paths are not qualified. Requalify host upgrades.

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
