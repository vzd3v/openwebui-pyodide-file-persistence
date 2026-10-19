# Using saved files

Select the Tool for a chat/model configured for native function calling and
Pyodide, as described in [installation](../ops/install.md). The assistant handles
managed execution and recovery calls; you normally just ask for the document.
The project is experimental: check the
[artifact-specific qualification matrix](../dev/compatibility.md).

Ask for a document normally. The agent works with relative paths in a managed
directory. `outputs/` means **issued to the user**, not “finished”: a requested
draft belongs there too. Other files stay in the private saved workspace.
The Tool cannot determine sensitivity or whether the agent chose correctly.

Successful managed execution saves a private checkpoint and automatically
publishes new/byte-changed issued files. The assistant must include returned
Markdown links in its answer. Cards are optional convenience, not a substitute.
Links use Open WebUI's authenticated file routes, not browser-local URLs. Sharing
is intended to use those same inline links without Controls. Before relying on it,
verify a fresh authorized recipient on your instance; qualification is scoped to
the linked matrix, not every build or deployment. Recipients must sign in and
have normal host access. Anonymous
download is not supported.

After **workspace saved**, open the chat in another authenticated browser and
ask to modify the result. The next managed execution restores automatically.
Inputs use `inputs/<file-id>/<name>`; identical names stay distinct and saved
edits are not replaced by original uploads.
Private checkpoints remain normal owner/admin-visible, deletable Files.

## When something goes wrong

Ask the assistant to report the actual Tool result rather than inventing a link
or rerunning a program automatically. For recovery, the assistant can use the
following `execute_code` actions; they are Tool parameters, not browser commands.

- **Ordinary Python error:** written files are privately checkpointed if transfer
  and saving succeed. Error-produced outputs are not later published by unrelated success.
- **Publication interrupted:** `execute_code` with `action="retry"`, the returned
  `operation_id` and no code finishes from saved bytes, never rerunning Python.
- **Response lost:** `action="status"` with no code lists recent historical
  receipts, with availability explicitly not rechecked. Saved/published states
  record past acknowledgements; bytes, destinations or grants may since be gone.
  Retry the saved operation without code to revalidate before receiving links;
  Python is not rerun.
- **Conflict:** checkpoint retained, but not selected as head or auto-published.
  No silent branch selection or overwrite occurs.
- **Deletion/revocation:** explicit incomplete outcome; an owner-deleted File
  is not silently recreated. Unchanged issued files retain original identities.
- **Empty issued file:** zero-byte files can be checkpointed, but the host/provider
  may reject publishing them as ordinary uploads. Publication stays incomplete;
  the Tool does not bypass that policy or replace the bytes.

Reload/closure before transfer can lose new work. Recovery requires an acknowledged
checkpoint that still exists and is accessible. Timeout does not confirm worker
termination. Do not use a new execution as an automatic retry. No automatic pruning
is performed.

## What is not saved

Only files in the managed workspace are checkpointed. Python variables, installed
packages, manual code-block work and unrelated browser files are not imported.
Existing browser persistence remains enabled and separate from this Tool.
