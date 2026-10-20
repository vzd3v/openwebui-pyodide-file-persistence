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

Version `0.2.0a1` introduces the browser shell and version-two manifests. Earlier
release observations do not qualify this new artifact. Portable automated checks
use real disk SQLite/PostgreSQL and synthetic external host boundaries. A real
Chromium module-worker smoke exercises the production wrapper and public Pyodide
APIs, including 256 MiB transient scratch and fresh-worker retained restoration.
Neither check substitutes for native Tool/model execution or shared UI downloads.

Native stock-host acceptance of this version must separately verify installed
artifact identity, ordinary owner/model execution, configured storage, fresh-browser
recovery, shared inline downloads, private-file denial and grant revocation.
No remote S3/GCS/Azure provider qualification or general browser-memory capacity
promise is made. The advertised minimum is a requirement, not a multiversion
frontend compatibility matrix.

The version-sensitive `execute:python` event is checked against upstream source;
it is not documented as a stable public extension API. The integration requires
no upstream frontend changes but must be requalified when that contract changes.
The filesystem/bootstrap uses documented public Pyodide Python/JS APIs and the
public npm `just-bash/browser` export.

Portable Tool/build Python support is separate from the host's Python range.
This project does not override Open WebUI's own Python requirements. Checks fail
closed on missing contracts; do not bypass them by changing a version label.
