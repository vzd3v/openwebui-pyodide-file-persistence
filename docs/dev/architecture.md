# Architecture

The Tool connects browser execution to Open WebUI's existing file storage and
permissions. Python runs in Pyodide; shell commands use the same filesystem
through just-bash. The server coordinates saving and downloads, not execution.

Open WebUI owns users, chat access, Files and downloads. Browser results are
untrusted uploads. See [Security](../../SECURITY.md) before changing a trust or
persistence boundary and the [Tool reference](tool-reference.md) for observable
behavior.

## Execution and saving

1. Bind live actor/chat/generation context; check durable gates before allocating.
2. Check the browser's cached workspace against the acknowledged operation. Reuse
   it if warm; otherwise restore verified saved files sequentially.
3. Execute the submitted source once. Python and shell read/write the same Pyodide
   filesystem; there is no mirrored shell filesystem.
4. Hash retained files. Transfer changed bytes and a complete manifest; do not
   scan or encode scratch.
5. Commit immutable file blobs, then the manifest. Select the workspace head with
   a revision compare-and-set.
6. After successful execution, publish new/changed outputs using their existing
   blob File IDs. Scripts, run records and manifests stay private.

A conflicting operation retains private bytes but cannot select the head or
publish automatically. Retry verifies committed bytes and current grants without
browser execution. Status reports historical acknowledgements, not current
availability.

## Durable state

Four SQLAlchemy Core tables use the existing host database:

- `pyodide_file_saver_workspace`: actor/chat revision and acknowledged operation.
- `pyodide_file_saver_operation`: reserved File IDs, manifest metadata, operation
  states and bounded execution text.
- `pyodide_file_saver_diagnostic`: dispatch phase, normalized failure and request scope.
- `pyodide_file_saver_gate`: failed request and authenticated actor/session guards.

Payloads live in the configured file storage. Host-owned tables and migrations
are unchanged. Database schema version remains one; defaulted receipt fields
support version-two manifests while preserving older receipts.

Diagnostics and gates are additive tables; existing receipt JSON is unchanged.
Workspace-row locking serializes guarded allocation and failure recording across
workers. Unknown execution remains fenced across messages/reconnects without TTL.
Completed uncheckpointed work has a separate persistence fence protecting its root.
First refusal commits a request latch after allocation rollback; concurrent workers
elect one warning, and later refusals can invoke optional native stopping.
Candidate queries select unresolved work independently of recent history, with
bounded returned metadata. Older uncertainty below the acknowledged revision
cannot replace the head through CAS and remains historical private state.

Only changed retained files receive new immutable blob IDs. Unchanged bytes reuse
acknowledged IDs. All required blobs must commit before the manifest and head.
Browser-supplied File IDs never establish authority.

Provider I/O happens outside database transactions. Each upload attempt gets a
fresh object name; a database lock elects one File/receipt mapping. File insertion
and its committed marker are atomic. Losing attempts can leave orphan objects;
there is no automatic garbage collection. Partial transfers are unacknowledged,
and deleted committed Files are never recreated automatically.

Publication locks Chat then operation receipt. It merges Chat/ChatMessage
attachment lists without changing message content/output, creates missing ChatFile
links, and commits publication atomically. Only issued outputs get chat grants.

## Browser protocol

Native events use `files=[]`. The wrapper uses public `pyodide_js.FS`,
`pyodide.code.run_js` and Python/JavaScript interop. It does not discover a global
Pyodide instance or worker internals. The shell registers its own namespace only.

Warm execution sends no inherited retained bytes and reuses the shell bundle
when its digest matches. Cold restoration reads and verifies one bounded File at
a time, sends sequential restore callbacks, then executes the program once.

A pending root is bound to its operation before mutation. Interleaved operations
cannot reuse it as the previous acknowledged head; they fail or use separate roots.
A runtime busy flag limits ordinary browser overlap. Database compare-and-set is
the authority across processes; neither mechanism isolates malicious shared code.

Snapshots contain scripts, outputs and exact grandfathered version-one paths.
The wrapper rejects root escapes, special files, links in retained/input ancestors
and replaced managed aliases. The server validates the final manifest against
acknowledged slots and verified fresh bytes.

Input references survive between calls and are reauthorized every time. Warm
calls check File existence, grants and local input hashes; they do not reread
inherited provider objects. Cold restoration and retry verify provider bytes.
Remote reads may materialize a whole object before a bounded local read.

Each call has fresh globals. Run records capture source, cwd, runtime, available
package lock, exit status and truncation flags. The head keeps the latest recipe;
historical receipts retain immutable earlier blobs. Full logs remain transient.
Errors and nonzero exits do not issue outputs.

Completion streams are captured before snapshot/recipe work. `recover` snapshots
only the current exact pending root after completion; it never runs source.
Partially uploaded snapshots must match every reserved path/hash/size and reuse
its immutable IDs. `retry` reconstructs a missing manifest only after all blobs
are committed and verified. Both paths retain head CAS and current grants.

## Code map

Python modules are under `src/pyodide_file_saver/`.

| Module | Responsibility |
| --- | --- |
| `tool.py` | Native entry point, status/retry, result presentation |
| `contracts.py` | Actions, limits, receipts, path validation |
| `workspace_state.py` | Tables, durable receipts, revision compare-and-set |
| `workspace_execution.py` | Authorization, bounded restoration, delta commits |
| `workspace_manifest.py` | Version-two immutable-blob manifests |
| `openwebui_adapter.py` | Version-sensitive host, storage and chat integration |
| `browser_workspace.py` | Browser prepare/restore/execute protocol |
| `browser_execution.py` | Version-one recovery and package hints |
| `browser_asset.py` | Embedded browser dependency |
| `browser/workspace-fs.ts` | Shell access to the Pyodide filesystem |

## Build and host compatibility

`npm ci --ignore-scripts && npm run build` bundles the public `just-bash/browser`
export from the lockfile and embeds dependency licenses. The compression adapter
rejects unsupported operations; upstream source is not patched.

`python3 scripts/build_tool.py --check` embeds the asset and flattens Python
modules in fixed order. It rejects conflicting imports and compares two builds.
The result is one Python file. `build/` and `dist/` are untracked; building neither
imports Open WebUI nor executes user programs.

The adapter checks APIs, signatures, ORM metadata and actual schema before
creating state. PostgreSQL replicas share one database; SQLite needs local disk
locking. See [compatibility](compatibility.md) for requirements and evidence.

The optional [package delivery utility](../ops/pyodide-packages.md) is separate
from this protocol and build. It prepares missing lock-verified assets for an
existing ingress; it adds no Tool dependency or execution service.
