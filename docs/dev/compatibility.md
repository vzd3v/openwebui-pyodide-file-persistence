# Compatibility

Requires **Open WebUI 0.12.0 or newer** with the host contracts below.

The adapter checks concrete capabilities before use. Those checks can reject an
incompatible host; they cannot prove every frontend or permission behavior. Test
[installation acceptance](../ops/install.md#check-your-installation) on your own
host/provider before enabling the Tool for users.

## Required host contracts

| Area | Required behavior |
| --- | --- |
| Database | Async SQLAlchemy engine and shared ORM metadata; PostgreSQL or local-disk SQLite; permission to create two Tool tables |
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

These checks apply to the Tool versions listed below. A new build needs its own
native acceptance. The observed configuration used Open WebUI 0.12.2, PostgreSQL
and local storage. It was not established as a pristine upstream installation;
no frontend patch was added for this Tool.

| Tool version and file checksum | Observed native behavior |
| --- | --- |
| `0.2.0a2`, SHA-256 `207e92d57f9ab69bf97cc9c79527b989523ae3b86c3fbfb5d8d79d53605c05bd` | Genuine Python → same-page Bash → fresh-browser saved-program rerun; empty imported initializer; 128 MiB warm scratch absent after restoration; original input reference and unchanged File ID reuse; new/warm/restored context |
| Same a2 file | Empty outputs, including `html` and Unicode names; duplicate basenames; nonempty→empty→nonempty immutable IDs; owner/recipient inline downloads with exact bytes/names; foreign/private denial and revocation/regrant; older saved links and a1 receipts readable |
| `0.2.0a1`, SHA-256 `7c3a04c4d152f2fdd24e5fc24dbb039ee5756b01e3baea5b27e958dc7a0bfad7` | Python/Bash shared files; warm scratch and fresh restoration; saved-program rerun; unchanged File ID reuse; original native CSV without duplicate upload; owner/recipient downloads after reload; foreign/private denial, revocation and deleted-input rejection |

Empty-file deletion was checked separately with a manually created native File
using the exact representation, not end to end on a Tool-issued empty File.
Storage-failure diagnostics used synthetic callbacks and real databases. Native
provider fault injection and a complete interruption matrix were not performed.
These observations do not qualify S3, GCS or Azure, or establish runtime acceptance
of the installer minimum 0.12.0.

## Version-sensitive integration

`execute:python` is checked against upstream source, but is not documented as a
stable extension API. Recheck it when upgrading Open WebUI. Filesystem/bootstrap
code uses public Pyodide APIs and the public npm `just-bash/browser` export.

Empty download and local deletion contracts were checked against official
[0.12.0 file routes](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/routers/files.py)
and [0.12.2 storage providers](https://github.com/open-webui/open-webui/blob/v0.12.2/backend/open_webui/storage/provider.py).
Source inspection is not runtime acceptance for every host.

## Saved-data compatibility

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
