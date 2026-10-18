# Files and recovery

Ask for a document normally. The agent works with relative paths in a managed
directory. `outputs/` means **issued to the user**, not “finished”: a requested
draft belongs there too. Other files stay in the private saved workspace.
The Tool cannot determine sensitivity or whether the agent chose correctly.

Successful managed execution saves a private checkpoint and automatically
publishes new/byte-changed issued files. The assistant must include returned
Markdown links in its answer. Cards are optional convenience, not a substitute.
Authorized shared-chat recipients use the same inline links without Controls.
Anonymous download is not promised; normal host authentication/access applies.

After **workspace saved**, open the chat in another authenticated browser and
ask to modify the result. The next managed execution restores automatically.
Inputs use `inputs/<file-id>/<name>`; identical names stay distinct and saved
edits are not replaced by original uploads. Variables/packages are not saved.
Private checkpoints remain normal owner/admin-visible, deletable Files.

## Incomplete work

- **Python error:** written files are privately checkpointed if transfer/saving
  succeed. Error-produced outputs are not later published by unrelated success.
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

Reload/closure before transfer can lose new work; only acknowledged checkpoints
are promised recoverable. Timeout does not confirm worker termination. Do not
use a new execution as an automatic retry. No automatic pruning is performed.
