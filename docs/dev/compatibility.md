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

Checks fail closed on concrete missing/incompatible contracts. They cannot prove
that an unknown implementation preserves every permission or frontend semantic.
Do not bypass a failed check by changing a version label or modifying host code.

## Qualification scope

The following observations apply to version `0.2.0a1`, generated artifact SHA256
`7c3a04c4d152f2fdd24e5fc24dbb039ee5756b01e3baea5b27e958dc7a0bfad7`.
Earlier artifacts and different host/provider configurations require their own checks.

| Environment | Verified scope |
| --- | --- |
| Portable integration with synthetic host/callback boundaries; real disk SQLite and PostgreSQL | Durable receipts, immutable blob reuse, CAS/retry, publication rollback, input authorization and version-one checkpoint recovery |
| Chromium module worker with production wrapper and public Pyodide APIs | Shared Python/shell files, 256 MiB transient scratch, 32 MiB binary shell operations, retained deltas, fresh-worker restoration and rejected links |
| Open WebUI 0.12.2 native UI/Tool path; PostgreSQL and local storage | Python followed by Bash in the same browser; 128 MiB warm scratch absent after fresh-browser restoration; saved-script rerun; unchanged script/output File ID reuse; original native CSV reference without duplicate upload |
| Same native configuration, authenticated owner/recipient/foreign users | Owner and authorized-recipient inline downloads with exact bytes after reload; foreign/private-file denial; grant revocation blocks recipient downloads; native input deletion blocks warm/cold execution without advancing the head |

The native observation does not establish a pristine upstream installation.
No frontend patch was added for this integration. The installer minimum of
0.12.0 remains a requirement checked against source contracts; this qualification
does not establish runtime acceptance of 0.12.0.
No remote S3/GCS/Azure provider qualification or general browser-memory capacity
promise is made.

The version-sensitive `execute:python` event is checked against upstream source;
it is not documented as a stable public extension API. Requalify when that
contract changes. The filesystem/bootstrap uses documented public Pyodide APIs
and the public npm `just-bash/browser` export.

Portable Tool/build Python support is separate from the host's Python range.
This project does not override Open WebUI's own Python requirements.
