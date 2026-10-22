# Compatibility and qualification

This is an experimental pre-release, not a blanket support promise. The adapter
checks capabilities, not an exact Open WebUI version string. Installer metadata
specifies a **minimum**, not an exact-version constraint or proof for later forks.
The current installer minimum is Open WebUI 0.12.0.

## Required host contracts

- Async SQLAlchemy engine, shared host ORM metadata and the required File, Chat,
  ChatFile and normalized ChatMessage fields/types/identity. Publication updates
  only an existing assistant's `files`/`updated_at` in the same transaction as
  Chat JSON, ChatFile links and the durable receipt.
- Async user lookup, accessible-chat write authorization, configuration lookup
  and native file-access checks with compatible call signatures; synchronous
  configured-provider upload/read methods. Open WebUI remains the authority.
- PostgreSQL or durable local disk SQLite, with permission to create two
  namespaced Tool tables. Additional nonunique indexes are allowed; conflicting
  expected indexes, uniqueness, foreign keys or incompatible state are rejected,
  never automatically repaired or deleted.
- Native function calling with genuine injected actor/chat/assistant/request
  context. New Python execution additionally requires the Pyodide engine,
  blocked-module policy and a connected stock browser. Saved status/retry do not
  require a currently enabled execution engine or browser.
- The stock transfer/deadline contract: serialized payloads below 16 MiB and
  callback wait below 60 seconds. Provider upload limits can be lower.
- Empty retained files additionally require configured native `STORAGE_PROVIDER=local`
  and the native filename-qualified content route. The explicit representation is
  `path=""`, `data.content=""`, an integration marker and a committed origin receipt
  slot with exact zero size/SHA-256. Provider bytes are never replaced by fallback
  text for nonempty files. Native S3/GCS/Azure deletion cannot safely handle this
  representation, so new empty Files are rejected there and for unknown providers.
  This check does not prevent reads of previously saved empty Files.

Checks fail closed on concrete missing/incompatible contracts. They cannot prove
that an unknown implementation preserves every permission or frontend semantic.
Do not bypass a failed check by changing a version label or modifying host code.

## Saved-data compatibility

Version `0.2.0a3` starts every Tool call at the workspace root and removes the
model-facing `cwd` argument. Saved files and receipts keep their existing paths
and bytes; no data migration is needed. To rerun an earlier program that expects
another directory, enter its recorded `cwd` inside the program before executing
the saved source. Historical run records remain evidence of their original
execution directory. Changing directory inside one call does not carry over to
the next call.

Version `0.2.0a2` reads earlier receipts and version-one checkpoints. This is a
forward upgrade path, not a reversible binary swap: strict older `0.2.0a1` readers
reject the new receipt fields and FileSlot representation, including for nonempty
files. After a2 writes receipts, preserve the database and native Files and use a
version that understands those receipts. Deselecting the Tool can stop new calls;
replacing it with a1 does not restore workspace compatibility. Do not delete or
rewrite coordination data or saved Files to force a downgrade.

## Qualification scope

### Current artifact: 0.2.0a3

The fixed-root entrypoint requires fresh qualification. The older artifact's
native acceptance below does not qualify this build.

### Historical artifact: 0.2.0a2

The following observations apply to generated artifact SHA256
`207e92d57f9ab69bf97cc9c79527b989523ae3b86c3fbfb5d8d79d53605c05bd`.
Other artifacts and different host/provider configurations require their own checks.

| Environment | Verified scope |
| --- | --- |
| Portable integration with synthetic host/callback boundaries; real disk SQLite and PostgreSQL | Empty-file receipt provenance and rejection on remote/unknown providers; cold restoration and legacy recovery; accepted stdout/stderr/outcome after storage failure and specific-operation status; callback classification; unknown early outcomes and additive receipt context |
| Chromium module worker with production wrapper and public Pyodide APIs | Shared Python/shell files, 256 MiB transient scratch, 32 MiB binary shell operations, retained deltas, fresh-worker restoration and rejected links |
| Open WebUI 0.12.2 native UI/Tool path; PostgreSQL and local storage | Three genuine UI/model calls: Python, same-page Bash, then fresh-browser saved-script rerun with an empty imported package initializer; 128 MiB scratch present while warm and absent after fresh restoration; original native input reference and unchanged File ID reuse; new/warm/restored context matches the acknowledged base operations |
| Same native configuration, empty and nonempty retained files | Empty outputs including an output named `html` and Unicode names; duplicate basenames with distinct bytes; nonempty→empty→nonempty changes use new immutable File IDs; unchanged IDs reused; independent database/provider/callback checks and earlier a1 receipt reads |
| Same native configuration, authenticated owner/recipient/foreign users | Owner/recipient inline downloads with exact bytes and suggested filenames; private/foreign denial; revocation denies recipient downloads and restored grants restore access; earlier saved links remain readable with their existing access controls |

Native empty-file deletion was separately checked using a manually created native
File with the exact metadata representation. Deletion of a Tool-issued empty File
was not exercised end to end. Storage-failure and callback-error diagnostics use
synthetic host/callback boundaries with real databases; native provider fault
injection and a complete interruption matrix were not performed for this artifact.

### Historical artifact: 0.2.0a1

The following historical observations apply only to generated artifact SHA256
`7c3a04c4d152f2fdd24e5fc24dbb039ee5756b01e3baea5b27e958dc7a0bfad7`.
They do not extend the current artifact's qualification scope.

| Environment | Verified scope |
| --- | --- |
| Portable integration with synthetic host/callback boundaries; real disk SQLite and PostgreSQL | Durable receipts, immutable blob reuse, CAS/retry, publication rollback, input authorization and version-one checkpoint recovery |
| Chromium module worker with production wrapper and public Pyodide APIs | Shared Python/shell files, 256 MiB transient scratch, 32 MiB binary shell operations, retained deltas, fresh-worker restoration and rejected links |
| Open WebUI 0.12.2 native UI/Tool path; PostgreSQL and local storage | Python followed by Bash in the same browser; 128 MiB warm scratch absent after fresh-browser restoration; saved-script rerun; unchanged script/output File ID reuse; original native CSV reference without duplicate upload |
| Same native configuration, authenticated owner/recipient/foreign users | Owner and authorized-recipient inline downloads with exact bytes after reload; foreign/private-file denial; grant revocation blocks recipient downloads; native input deletion blocks warm/cold execution without advancing the head |

These native observations do not establish a pristine upstream installation.
No frontend patch was added for this integration. The installer minimum of
0.12.0 remains a requirement checked against source contracts; this qualification
does not establish runtime acceptance of 0.12.0.
No remote S3/GCS/Azure provider qualification or general browser-memory capacity
promise is made.

The version-sensitive `execute:python` event is checked against upstream source;
it is not documented as a stable public extension API. Requalify when that
contract changes. The filesystem/bootstrap uses documented public Pyodide APIs
and the public npm `just-bash/browser` export.
The metadata-backed empty download and local deletion behavior are checked against
official [0.12.0 file routes](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/routers/files.py)
and [0.12.2 storage providers](https://github.com/open-webui/open-webui/blob/v0.12.2/backend/open_webui/storage/provider.py).
These source checks do not establish runtime acceptance for every supported host.

Portable Tool/build Python support is separate from the host's Python range.
This project does not override Open WebUI's own Python requirements.
