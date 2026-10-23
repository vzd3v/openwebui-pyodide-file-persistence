# Working with files

Once an administrator [installs the Tool](../ops/install.md), enable it for your
chat and ask for the result you need. You do not need to write code or choose a
language. The assistant can use Python for calculations and shell commands for
searching and editing files.

## Try these tasks

**Compare data:**

> Compare the two attached CSVs by customer ID. Save the differences as
> `outputs/differences.xlsx`, return its download link, and keep the analysis
> program under `scripts/` for next time.

**Edit documents:**

> Search these Markdown files for the old product name, replace it with the new
> name, and give me links to the revised files. Summarize what changed.

**Reuse an analysis:**

> Use the saved program in `scripts/` on this new CSV. Install any dependencies
> it needs, rerun it, and return the updated report.

Actual Python package availability depends on your instance. The assistant should
report a missing package rather than pretend that a file was created.

## Download and share results

The assistant should include a download link in its answer. Click that link to
get the file; opening Controls should not be necessary. A filesystem path such
as `outputs/report.xlsx` is not a download link.

Links use normal authenticated Open WebUI downloads. A recipient of a shared chat
must sign in and have permission to access it. Sharing a result does not attach
private programs or run records. Deleting a file or revoking access can make an
old link stop working.

If a result is missing its link, ask the assistant to check the saved operation
and retry publication. It should use the existing operation ID, not automatically
run the program again.

## Keep the files you need

| Directory | What belongs here | Saved by the Tool? |
| --- | --- | --- |
| `inputs/` | Attachments you have permission to use | Referenced originals; access is checked on every call |
| `work/` | Temporary data and full logs | No |
| `scripts/` | Programs to keep and rerun | Yes, privately |
| `outputs/` | Documents and results to download | Yes; new or changed files are linked after success |

Python and shell see the same files. Each call starts at the workspace root, so
`outputs/report.csv` means the same thing in both languages. Copy an input into
`work/` before editing it.

A warm browser can reuse scratch files between calls. After a fresh-browser
restore, only acknowledged saved files and authorized inputs return. Variables,
temporary files and installed packages do not automatically return. Even in a warm
browser, each call has a fresh Python namespace or shell environment.

The Tool keeps the latest submitted program and available dependency information
in private run records. These help the assistant inspect and repeat a calculation;
they do not install dependencies or reproduce external data automatically.

## Spend context on the answer

Ask the assistant to keep large intermediate results in `work/`, save the useful
result and return a short summary. It can search files with `rg`, select JSON with
`jq`, and rerun a saved program instead of rewriting it.

File bytes saved by the Tool are separate from its text response. This can reduce
unnecessary conversation text, but it does not prevent the model from seeing file
content through other tools, attachment processing or printed output.

## When something fails

- **The program reports an error:** saved files may still have been preserved
  privately. Outputs from the failed call do not become chat downloads. Ask the
  assistant to inspect the error and correct the program.
- **The program finished but saving failed:** its accepted output and exit status
  remain available, but a saved workspace is not confirmed. Retry the existing
  operation if possible; do not assume the files were published.
- **The response was lost or timed out:** first ask for operation status. A timeout
  does not prove execution stopped. Automatically running it again can duplicate work.
- **Two calls conflict:** the losing saved operation stays private. The Tool does
  not choose a replacement automatically.
- **A file exceeds the limit:** ask for a smaller result or talk to the administrator.
  The defaults are 8 MiB per retained file and 8 MiB total; scratch is separate.

Before reloading the browser or starting another execution after a failed save,
inspect pending work. Unsaved changes and scratch may be lost or left in an older
browser workspace. See the [Tool reference](../dev/tool-reference.md#failure-and-recovery)
for exact recovery behavior.

## Limits to expect

Shell commands are browser implementations. Common file commands, `rg`, `jq`,
`sed`, `awk` and pipelines are available; not every GNU option or jq expression
is supported. Native programs, subprocesses, OS threads, network shell commands
and compression commands are unavailable. Use Python for Pyodide packages.

Browser memory limits scratch. Empty saved files require local storage; other
storage providers reject their creation. There is no automatic pruning of saved
files. Review results before sharing them, and use ordinary Open WebUI deletion
and access controls when needed.

For exact API fields, command limits, package loading and saved-data behavior,
see the [Tool reference](../dev/tool-reference.md).
