# Architecture

One in-process native Tool coordinates browser execution with the existing host
database and storage provider. Open WebUI owns identities, grants, Files,
attachments and downloads. Browser bytes remain untrusted self-uploads.

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Strict actions, limits, receipts and path validation |
| `workspace_state.py` | Two namespaced tables, durable receipts and head CAS |
| `browser_execution.py` | Version-one checkpoint compatibility and package hints |
| `browser_workspace.py` | Public-Pyodide prepare/restore/execute protocol |
| `browser_asset.py` | Embedded maintainer-built browser dependency |
| `workspace_manifest.py` | Version-two immutable blob manifests |
| `workspace_execution.py` | Authorization, bounded restores and delta commits |
| `openwebui_adapter.py` | Version-sensitive host/storage/chat integration |
| `browser/workspace-fs.ts` | just-bash filesystem adapter to the same Pyodide FS |
| `tool.py` | Native entrypoint, status, retry and result presentation |

## Durable state

Two SQLAlchemy Core tables use the host's existing database without changing host
schemas or migrations: `pyodide_file_saver_workspace` records actor/chat revision
and acknowledged operation; `pyodide_file_saver_operation` stores reserved File IDs,
manifest metadata, states and bounded text. File payloads stay in configured host
storage. Database schema version remains one; defaulted receipt fields add the
version-two checkpoint format without deleting or rewriting old receipts.

Allocate an operation before browser work. Reserve new immutable blob IDs only
for changed retained files; reuse acknowledged IDs for unchanged bytes. A version-two
checkpoint is a small JSON manifest referencing these blobs and native input IDs.
Commit every required blob before committing the manifest and selecting the head
with revision CAS. No browser-supplied File ID becomes authority. A conflicting
operation keeps its private bytes and cannot automatically publish.

Provider I/O stays outside transactions. Every upload attempt gets a fresh object
name; one File/receipt mapping is elected under a database lock. File insertion and
its committed receipt marker are atomic. Losing attempts may leave orphan objects;
no automatic garbage collection is claimed. A partial blob failure does not become
an acknowledged checkpoint, and committed-but-deleted Files are never recreated.

Successful publication reuses the output blob's File ID. Chat attachment grants
cover only issued outputs, never scripts, recipes or the whole manifest. Publication
locks Chat then receipt, merges Chat/ChatMessage attachment lists, preserves message
content/output, creates absent ChatFile links and commits published state atomically.
Retry verifies saved manifest/blob bytes and host grants without browser execution.
Status reports historical acknowledgements without claiming current availability.

## Browser protocol

Every native event has `files=[]`. The wrapper uses public `pyodide_js.FS`,
`pyodide.code.run_js` and Python/JS interop; it does not discover a global Pyodide
instance or worker internals. The shell bundle registers only its own namespace.
The filesystem adapter performs real reads/writes on that same filesystem;
there is no second mirrored shell filesystem.

Prepare checks the browser's actor/chat cache against the authoritative operation
head. Warm execution reuses scratch, sends no inherited retained bytes, and does
not resend the shell bundle when its artifact digest matches. Cold restoration reads/verifies one bounded File at
a time and sends sequential restore callbacks before executing user source once.
A pending root is bound to its operation before any mutation. Interleaved operations
cannot treat it as the old acknowledged head; mismatches fail or create separate
roots. Revision CAS remains the cross-worker authority. A runtime-wide busy flag
prevents ordinary overlap within each browser invocation, not malicious shared code.

Only scripts, outputs and exact grandfathered version-one paths enter retained
snapshots. Reject links in retained/input ancestors, replaced managed aliases,
root escapes and special files. Scratch is not walked or base64 encoded. Compute
retained hashes and return changed bytes plus a complete manifest; the server
validates the final manifest against acknowledged slots and verified fresh bytes.
New input references persist across calls and are reauthorized even when attachments
are absent. Warm calls check File existence/grants and local input hashes; they do
not reread/hash inherited provider objects. Cold restores and retries verify those
actual provider bytes. A remote provider may materialize an object before the
bounded local read; this is not remote streaming.

Fresh Python globals, host direct-import restrictions and supported-package hints
remain. A recipe records exact submitted source after execution, runtime/package
information, available micropip lock, exit code and truncation flags. The current
head keeps only the latest recipe paths; historical receipts keep old immutable
blobs. Full output logs stay transient. Errors/nonzero exits never issue new outputs.

## Build and compatibility

`npm ci --ignore-scripts && npm run build` bundles the public `just-bash/browser`
export and dependencies from the committed lockfile. Compression has an explicit
consumer adapter that fails closed; no upstream sources are patched. Dependency
licenses are embedded in the generated asset.

`python3 scripts/build_tool.py --check` embeds that asset and flattens Python
modules in fixed order. It rejects conflicting import bindings and compares two
builds. Generated `build/` and `dist/` are untracked. The installed artifact is
one Python file; neither build step imports Open WebUI or runs user code.

The adapter checks concrete APIs, call signatures, ORM metadata and actual schema
before creating state. PostgreSQL coordinates replicas sharing one database;
SQLite requires local disk locking. Separate per-replica databases and network-file
SQLite are unsupported. See [compatibility](compatibility.md) for qualification.

The optional [package delivery utility](../ops/pyodide-packages.md) is separate
from the Tool build and browser protocol. It inventories existing runtime assets,
audits same-origin delivery and prepares only missing lock-verified package files
for an administrator's existing ingress. It introduces no Tool dependency,
application patch, execution service or package installation callback.
