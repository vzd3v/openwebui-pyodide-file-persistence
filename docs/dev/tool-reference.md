# Tool reference

This page describes `execute_code` for integrators and for diagnosing agent calls.
For ordinary chat use, start with the [user guide](../user/guide.md).

## Calls

| Field | Values | Purpose |
| --- | --- | --- |
| `action` | `execute` (default), `retry`, `recover`, `status` | Run, finish saving without rerunning, or inspect operations |
| `code` | Source text | Required for execution; empty for other actions |
| `language` | `python` (default), `bash` | Browser execution language |
| `operation_id` | Server-issued ID | Required for retry/recover; optional for status; absent for execution |

Every new `execute` runs a new program. Never substitute it for a retry after an
uncertain outcome. `status` without an ID lists recent operations without streams;
with an ID it includes retained File metadata, accepted bounded stdout/stderr and actual execution
outcome when available. Status never issues links and does not recheck old bytes
or grants. `retry` revalidates saved bytes and access before finishing publication,
without executing the program or recovering missing transfer bytes.

Status returns the acknowledged `head`, up to 32 `unresolved` execution blockers,
and up to 32 `recoverable` completed/saved operations independently of the recent
ten; each bounded list has a truncation flag. `dispatch`, `phase`, `failure_code`
and `request_scope` are durable diagnostics. Task scope uses a canonical injected
server task UUID and validates any stored message task; absent task IDs use a
conservative actor/chat/assistant-message gate. Session guards are actor-scoped;
model arguments cannot reset them or reconnect an old request.

Completed, uncheckpointed current-revision operations also fence new execution
with `execution_recovery_required`; completion evidence stays truthful. A first
fenced execute commits a request latch and returns `first_refusal=true` recovery
JSON without a new operation. Further executes reuse that latch; optional native
stopping may cancel them. The first browser failure also returns guidance rather
than stopping. Status and failed recovery leave the latch intact. Successful
retry/recover releases only the current request latch for that acknowledged
operation; it never clears a failed-session guard.

`recover` takes the current pending operation ID and no code. It requires that
exact completed root in the original live browser. Partial uploads must match
reserved bytes and retain immutable File IDs. `retry` can reconstruct a missing
manifest from a complete verified committed blob set. Both use existing grants
and revision CAS and never rerun user source. For manual new-chat recovery,
owner/admin File downloads use the IDs in specific status with the native
authenticated `/api/v1/files/{id}/content?attachment=true` route (empty Files use
`/content/download?attachment=true`). Status metadata itself does not verify
current bytes/access or grant access to another chat.

The result includes an exit code and truncation flags when execution was accepted.
`issued_files` includes each retained relative path, size, SHA-256 and Markdown
link. Include those Markdown links verbatim in the assistant answer; do not invent
filesystem URLs or substitute attachment cards or Controls instructions.

`browser_workspace` identifies new, warm or restored execution and its acknowledged
base operation. Early failures and older receipts can report `unknown`. It does
not promise that the worker is still alive or that scratch remains available.
Callback failures include a content-free `error_code` and `phase` when available;
raw host/provider errors are not returned.

`recovery_plan` derives instructions from retained evidence. Its `execution` is
`not_started` for a recorded pre-dispatch failure, `unknown` when execution might
have started, or `completed` after a received completion acknowledgement. It lists
applicable `tool_actions`, `next_step` and `needs_browser`. A pre-dispatch failure
also sets `needs_new_request` when automatic pre-dispatch reconnection cannot
establish the original runtime. Already-sent mutable callbacks are never replayed.
Completion is persisted before a separate source-free snapshot callback. This
does not make browser memory durable or establish completion when its reply is lost.

## Paths and input access

Each call starts at the workspace root. There is no model-facing `cwd` argument.
A program can change its own directory; that does not affect the next call.

- `work/`: transient scratch and bounded stdout/stderr logs; not walked for retention.
- `scripts/`: private retained source and run records.
- `outputs/`: retained user results; only new/changed files from successful calls
  are published.
- `inputs/<file-id>/<name>`: read-only native input references. Aliases remain
  stable after a native rename. Deletion or revoked access fails explicitly on
  every call, including calls that do not reattach the input.

Programs entering `work/` can use managed `inputs`, `scripts` and `outputs` links
to the root directories. Commands such as `find` that do not follow directory links
should use `../scripts` or an explicit link-following option. Do not replace managed
links. User-created links are excluded from retained snapshots even when their
target is inside the workspace.

For example, one Python call can write shared data:

```python
from pathlib import Path
Path("scripts/state").mkdir(exist_ok=True)
Path("scripts/state/data.json").write_text('{"values":[2,3,5]}')
```

A following Bash call can read it and produce a downloadable result:

```sh
jq -r '.values[]' scripts/state/data.json | awk '{sum += $1} END {print sum}' > outputs/sum.txt
```

## Python and shell

Each Python call has fresh globals. Import names again or run a retained program.
Installed packages may remain available in a warm worker. Static imports provide
Open WebUI package-loading hints; `importlib.import_module()` and `find_spec()`
probes do not request that preload. Use explicit imports or supported
Pyodide/micropip installation. Native subprocesses and OS threads are unavailable.

The bundled [just-bash](https://github.com/vercel-labs/just-bash) browser library
provides pipelines, redirection, loops and selected commands:

- Files: `cat`, `cp`, `mv`, `rm`, `mkdir`, `find`, `ls`, `stat`, `tree`, `touch`,
  `readlink`, `chmod`, `du`, `mktemp`, `split`.
- Text/JSON: `jq`, `rg`, `grep`, `sed`, `awk`, `sort`, `uniq`, `cut`, `head`, `tail`,
  `wc`, `xargs`, `diff`, `tee`, checksums and formatting utilities.

These are JavaScript implementations. `jq` supports a subset; `rg` uses JavaScript
regular expressions. Native processes, hard links, `python`, `sqlite3`, `js-exec`,
`tar`, `gzip`/`gunzip`, compressed search, `curl` and `wget` are unavailable.
Compression requests fail explicitly. The full selected list is in
[`browser/workspace-shell.ts`](../../browser/workspace-shell.ts).

Shell limits are 45 seconds and 16 MiB aggregate output. Returned text has a separate
[Valve cap](../ops/install.md#limits). Strings and heredocs are limited to 1 GiB,
arrays to 16 million entries. These guardrails do not guarantee usable browser RAM.

## Saving and run records

Only changed retained bytes are uploaded; unchanged immutable native Files are
reused. Cold restoration verifies and sends bounded files sequentially. Files are
not chunked and must fit the host's upload and transfer limits.

Private `scripts/.runs/latest/` records contain exact submitted source, cwd,
runtime, post-run installed package versions, exit status and a `micropip.freeze()`
lock when micropip is installed. Otherwise the lock is marked unavailable. The
current workspace keeps the latest recipe; historical operation records keep
immutable earlier blobs. Transient stdout/stderr logs under `work/` share the
per-stream `max_text_bytes` cap; excess text is discarded with truncation flags.

Private checkpoints and run records are ordinary owner/admin-visible Files, not
chat attachments. Only issued output Files receive chat attachment grants.

## Failure and recovery

Use the [recovery matrix and limits](../user/limitations.md#if-execution-or-saving-fails).
An ordinary program error saves retained files privately when storage succeeds;
error-produced outputs are not published by a later unrelated success. A losing
CAS checkpoint stays private. No operation automatically selects another branch,
replays source or overwrites the acknowledged head.

## Empty files and older saved data

With native local storage, empty retained files are receipt-backed native text
Files containing exactly zero bytes. Links use the filename-qualified native
route. No provider upload or placeholder bytes are used; normal authentication,
sharing and deletion still apply.

S3, GCS, Azure and unknown providers reject new empty retained files because their
native deletion cannot safely handle this representation. Previously saved,
receipt-backed empty Files remain readable after a storage change. Unmarked Files
with missing provider bytes are never treated as empty. There is no automatic
pruning, deletion or resurrection of saved Files.

Version-one checkpoints retain exact paths and bytes, including old saved input
copies. Those copies are independent saved data; new native references use live
input permissions. A legacy regular file named `work`, `scripts` or `inputs` at
the root blocks upgrade execution with a diagnostic. Use the previous compatible
Tool to rename/recover it; the old checkpoint and head are preserved. Recipe
paths avoid existing legacy files. Manual code blocks and unrelated browser files
are not imported. Keep existing browser persistence enabled.

Before changing versions, read [saved-data compatibility](compatibility.md#saved-data-compatibility).
Older binaries cannot read every newer receipt; deleting or rewriting saved data
is not a downgrade procedure.
