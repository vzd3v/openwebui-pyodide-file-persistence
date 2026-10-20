# Using the browser workspace

Enable the Tool for a chat/model using native function calling and Pyodide.
Normally the assistant chooses the execution language and handles saving; ask for
the document or analysis you need. See [installation](../ops/install.md).

## Paths and execution

`execute_code` accepts `language="python"` or `"bash"`. Each call starts with a
fresh Python namespace or shell environment; filesystem changes are shared.
Import names again in each call or execute a retained script. An installed package
can remain available in a warm worker even though its Python variables do not.
Static imports provide Open WebUI package-loading hints before execution;
`importlib.import_module()` and `find_spec()` probes do not request that preload.
Use explicit imports or supported Pyodide/micropip installation where appropriate.
Native subprocesses and OS threads are unavailable in this browser runtime.
The default `cwd="work"` is relative to the workspace root. `cwd="."` selects
the root; other values select an existing workspace-relative directory. Absolute
paths and `..` components are not accepted as the `cwd` parameter.

- `work/` is transient scratch. A warm browser reuses it; a fresh worker does not.
- `scripts/` is retained privately. Store reusable programs here.
- `outputs/` is intended for the user, including requested drafts. Successful
  calls publish only newly created or byte-changed output files.
- `inputs/<file-id>/<name>` contains referenced native attachments. Aliases remain
  stable after an upload is renamed. Authorization is rechecked on every call,
  including calls that do not reattach the input. Deleted/revoked inputs fail
  explicitly. Copy an input into scratch before editing it.

Inside `work/`, managed `inputs`, `scripts` and `outputs` convenience links point
to their root directories. Ordinary reads/writes such as `outputs/report.csv`
work from the default cwd. Tools that do not follow directory links, such as
`find`, should use `../scripts` or an explicit link-following option. Do not
replace these managed links. User-created links are excluded from retained
snapshots, even if their targets stay inside the workspace.

For example, Python can prepare data:

```python
from pathlib import Path
Path("scripts/data.json").write_text('{"values":[2,3,5]}')
```

A following Bash call can use it immediately:

```sh
jq -r '.values[]' scripts/data.json | awk '{sum += $1} END {print sum}' > outputs/sum.txt
```

The same file is visible to a later Python call. Large intermediates can stay
under `work/`; only retained files are hashed and considered for upload.

## Shell capabilities

The pinned just-bash 3.6.0 browser library supplies shell syntax, pipelines,
redirection, loops and a selected command set:

- File operations: `cat`, `cp`, `mv`, `rm`, `mkdir`, `find`, `ls`, `stat`, `tree`,
  `touch`, `readlink`, `chmod`, `du`, `mktemp`, `split`.
- Text/JSON: `jq`, `rg`, `grep`, `sed`, `awk`, `sort`, `uniq`, `cut`, `head`, `tail`,
  `wc`, `xargs`, `diff`, `tee`, checksums and common formatting utilities.

These are JavaScript implementations, not GNU binaries. `jq` implements a
compatible subset; arbitrary jq programs and every GNU option are not guaranteed.
`rg` uses a JavaScript regex implementation. Native processes, hard links,
`python`, `sqlite3`, `js-exec`, `tar`, `gzip`/`gunzip`, compressed search, `curl`
and `wget` are unavailable. Use the Python language for Pyodide packages.
Unsupported compression fails explicitly. Shell commands have a 45-second
execution limit and a 16 MiB aggregate output limit; returned text has its separate
Valve cap. Browser string/heredoc limits are 1 GiB, array limits 16 million
entries. These are command guardrails, not guaranteed usable browser RAM.

## Saving and recovery

After an acknowledged save, a fresh authenticated browser can restore retained
files. Only changed retained bytes are transferred/uploaded; immutable native File
blobs are reused. Cold restoration sends bounded files sequentially. Individual
files remain subject to transport/upload limits; no file chunking is provided.

Private `scripts/.runs/latest/` files record exact submitted source, cwd, runtime,
post-run installed package versions, exit status and `micropip.freeze()` dependency
lock when micropip is installed. Otherwise the recipe explicitly marks the lock
unavailable. The latest recipe remains in the current workspace; historical
receipts retain their own immutable recipe blobs. These records support manual
inspection/rerunning of the saved source. They do not restore Python variables,
install dependencies automatically, or guarantee reproducibility of external data.
Full stdout/stderr logs remain transient in `work/`; the Tool returns bounded text
with explicit truncation flags and the command exit code.
Once a valid execution result is accepted, a later storage failure still returns
its bounded stdout/stderr and actual exit status. This does not acknowledge a
saved workspace or issue download links. Before a valid execution result arrives,
the exit status and outcome are unknown, including after a callback timeout.
`browser_workspace` reports whether a valid execution used a warm root, restored
the named acknowledged base operation, or started a new workspace. Early failures
and older receipts report `unknown`. This records execution context, not worker
termination or continued scratch availability after the response.

The assistant must include returned Markdown download links verbatim. Links use
normal authenticated Open WebUI file routes. Shared recipients must sign in and
have normal host access; anonymous downloads are not supported. Private source,
recipes and checkpoint manifests are not attached to the chat.

When a call fails:

- An ordinary Python error or nonzero shell exit saves retained files privately
  if saving succeeds. Error-produced outputs are not published by unrelated success.
- `action="retry"`, a saved `operation_id` and no code revalidate committed bytes
  and finish publication without executing the source again.
- `action="status"` and no code list historical receipts. Availability is not
  rechecked until retry; deleted bytes or revoked grants can invalidate old links.
  Supplying a specific `operation_id` also returns its accepted bounded stdout/stderr
  and actual execution outcome, including after a lost storage-failure response.
  The recent-operation list omits streams. Status never issues download links.
- A conflict keeps the losing checkpoint private and leaves the acknowledged
  head unchanged. No automatic branch selection occurs.
- A failure before a complete manifest is committed is unacknowledged. Retry
  cannot rerun the code or recover missing transfer bytes. The last acknowledged
  head remains selected; browser-local pending files may still exist.
  A following execution can select a new browser root restored from that head,
  so neither scratch nor unacknowledged retained changes are guaranteed to be
  visible there. The previous browser root is not automatically deleted. Inspect
  pending work before choosing a new execution; retry never reruns its source.

Timeout does not prove worker termination. Do not automatically repeat a new
execution after a lost response. Browser closure/reload can lose scratch and
unacknowledged changes. Empty retained files are supported with native **local**
storage as receipt-backed native text Files containing exactly zero bytes. Their
links use the native filename-qualified download route; native authentication,
sharing and deletion still apply. No provider upload or placeholder bytes are used.
S3, GCS, Azure and unknown providers reject new empty retained files explicitly:
their native deletion path cannot safely clean up this representation. Existing
receipt-backed empty Files remain readable after a storage configuration change.
Unmarked native files with missing provider bytes never become empty files.
No automatic pruning, deletion or resurrection of saved Files is performed.
Callback failures include a content-free `error_code` and `phase` when available;
raw host/provider error text is not returned. Issued file records also include
the retained relative path, byte size and SHA-256 alongside the download link.

Version-one saved files retain their original exact paths and bytes when read
into the new format, including old saved input copies. If an old regular file is
named work, scripts or inputs at the root, execution stops with an explicit
upgrade diagnostic; use the previous compatible Tool to rename/recover it. The
old checkpoint and head are preserved. Private recipe directories avoid existing
legacy file paths. That compatibility copy
is independent of the original upload; new native input references use the live
authorization rules above. Manual code blocks and unrelated browser files are
not automatically imported. Keep existing browser persistence enabled.

Older saved data can be read by the new version; older binaries cannot read all
new receipts. Preserve saved Files and coordination data when stopping or changing
versions; see [saved-data compatibility](../dev/compatibility.md#saved-data-compatibility)
before attempting a downgrade.
