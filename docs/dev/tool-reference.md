# Tool reference

This page describes `execute_code` for integrators and for diagnosing agent calls.
For ordinary chat use, start with the [user guide](../user/guide.md).

## Calls

| Field | Values | Purpose |
| --- | --- | --- |
| `action` | `execute` (default), `retry`, `status` | Run a program, finish saved publication, or inspect operations |
| `code` | Source text | Required for execution; must be empty for retry/status |
| `language` | `python` (default), `bash` | Browser execution language |
| `operation_id` | Server-issued ID | Required for retry; optional for status; absent for execution |

Every new `execute` runs a new program. Never substitute it for a retry after an
uncertain outcome. `status` without an ID lists recent operations without streams;
with an ID it includes the accepted bounded stdout/stderr and actual execution
outcome when available. Status never issues links and does not recheck old bytes
or grants. `retry` revalidates saved bytes and access before finishing publication,
without executing the program or recovering missing transfer bytes.

The result includes an exit code and truncation flags when execution was accepted.
`issued_files` includes each retained relative path, size, SHA-256 and Markdown
link. Include those Markdown links verbatim in the assistant answer; do not invent
filesystem URLs or substitute attachment cards or Controls instructions.

`browser_workspace` identifies new, warm or restored execution and its acknowledged
base operation. Early failures and older receipts can report `unknown`. It does
not promise that the worker is still alive or that scratch remains available.
Callback failures include a content-free `error_code` and `phase` when available;
raw host/provider errors are not returned.

## Paths and input access

Each call starts at the workspace root. There is no model-facing `cwd` argument.
A program can change its own directory; that does not affect the next call.

- `work/`: transient scratch and full stdout/stderr logs; not walked for retention.
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
Path("scripts/data.json").write_text('{"values":[2,3,5]}')
```

A following Bash call can read it and produce a downloadable result:

```sh
jq -r '.values[]' scripts/data.json | awk '{sum += $1} END {print sum}' > outputs/sum.txt
```

## Python and shell

Each Python call has fresh globals. Import names again or run a retained program.
Installed packages may remain available in a warm worker. Static imports provide
Open WebUI package-loading hints; `importlib.import_module()` and `find_spec()`
probes do not request that preload. Use explicit imports or supported
Pyodide/micropip installation. Native subprocesses and OS threads are unavailable.

The pinned [just-bash 3.6.0](https://github.com/vercel-labs/just-bash) browser library
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
immutable earlier blobs. Full stdout/stderr stay transient under `work/`.

Private checkpoints and run records are ordinary owner/admin-visible Files, not
chat attachments. Only issued output Files receive chat attachment grants.

## Failure and recovery

| Situation | What is known | Safe next step |
| --- | --- | --- |
| Ordinary Python error or nonzero shell exit | Retained files are saved privately if storage succeeds; failed outputs are not published by a later unrelated success | Inspect the error, then submit corrected code as a new execution |
| Execution accepted, later storage failure | Bounded stdout/stderr and actual exit status are available; saved workspace and links are not confirmed | Inspect status and retry the existing ID if the saved operation permits it |
| No valid result, including callback timeout | Execution outcome and exit status are unknown | Inspect status; do not automatically execute again |
| Manifest not fully committed | Operation is unacknowledged; the last acknowledged head remains selected | Preserve and inspect browser-local pending work; retry cannot recreate missing bytes |
| Concurrent update conflict | Losing saved bytes stay private; acknowledged head is unchanged | Inspect both operations; no automatic branch selection occurs |

A later execution may select a new root restored from the acknowledged head.
Scratch and unacknowledged changes from the previous root are not guaranteed to
appear there. The previous root is not automatically deleted. Browser closure or
reload may lose pending work. A timeout does not prove worker termination.

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
