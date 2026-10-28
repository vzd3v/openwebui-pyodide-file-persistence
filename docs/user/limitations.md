# Browser execution and recovery limits

Python runs in your active browser. Keep the original chat tab connected until
execution and saving finish. Closing the browser does not move the computation
to a server or create a background job. On a qualified Open WebUI 0.12.3 host,
the Tool can reconnect before program dispatch if it recognizes the original
interpreter in an authenticated session. A reloaded worker, an unrelated tab or
missing continuity evidence cannot replace that interpreter automatically.
Preparation can also recover when the old session still appears connected but
does not answer. This does not extend the callback deadline or replay execution.

## What survives

Acknowledged files under `scripts/` and `outputs/` survive a fresh-browser restore.
Keep essential intermediate data under `scripts/state/`, alongside the saved
program and an explicit dependency recipe. Use `work/` for reproducible scratch.
Native inputs remain references to the original Files and require current access.

Python variables, scratch, transient logs and installed packages are not restored.
Each call has fresh Python globals even in a warm browser. Run records describe
observed packages; they do not reinstall them. A saved program that reads missing
scratch cannot resume merely because its source was saved.

## If execution or saving fails

Ask the assistant to use the existing operation ID and **no code**:

| Evidence | Implemented action | Limit |
| --- | --- | --- |
| Lost response or uncertain outcome | `status` | Shows the acknowledged head, unresolved operations and recent history; specific status includes bounded received streams |
| Failure before program dispatch | Explain the failure; reconnect before a new user request | The program did not start; automatic reconnect could not establish a usable original runtime |
| All file blobs committed, manifest or publication interrupted | `retry` | Verifies saved bytes and current access, reconstructs a missing manifest and finishes publication without running the program |
| Completed work or partially uploaded files remain in the original live browser | `recover` | Snapshots the current pending operation, preserves reserved File IDs and finishes saving without running source again |
| Worker is still busy, completion is unknown, or the pending browser root is gone | `status`; preserve the original tab if available | Recovery cannot retrieve bytes that never reached storage or a lost browser runtime |

`not_dispatched` means this program was not sent to execution. Preparation or
restoration may still have touched browser files. `possibly_running` and
`outcome_unknown` do not establish whether it ran or stopped. `completed_reported`
means bounded browser-reported completion was received; it does not prove a saved
checkpoint or attest the program's truthfulness.

Completion is acknowledged separately before retained files are scanned or
transferred. A later snapshot failure therefore preserves received completion
and bounded stdout/stderr. Loss before that acknowledgement can still leave the
outcome unknown. Results include a `recovery_plan` with the applicable next step.

Known-completed work without an acknowledged checkpoint blocks new programs with
`execution_recovery_required`, preserving its pending root and completion evidence.
Use recover before reloading/closing the tab. If retained bytes exceed the limits,
recovery still needs an administrator-approved limit change within transport bounds,
or an explicitly chosen new-chat computation. The Tool has no pending-root edit,
discard, historical inventory or export API.

A timeout never guarantees worker termination or exactly-once effects for
arbitrary Python. The Tool blocks further execution for a failed request and an
unresolved or unsaved workspace. A new message, reconnect or elapsed time does not erase
uncertainty. A newer acknowledged head fences older operations from selecting
or publishing over it; their history and private bytes remain intact.

If the old runtime is lost, there is no reset switch or automatic replay. Start a
new chat only after deciding to perform a **new computation**. Use existing output
download links, or ask the administrator to retrieve acknowledged programs/state
using the retained File IDs in specific operation status and the host's
authenticated File downloads. Attach those files to the new chat. Reattach
required native inputs and recreate the dependency recipe. Unsaved changes cannot
be recovered this way; the original chat's uncertain operation stays recorded.

## Packages and saved programs

Availability depends on the host's Pyodide catalog and administrator-permitted
package sources. Static imports in submitted Python provide limited host loading
hints. Imports hidden inside a saved script, dynamic imports and package probes do
not automatically load its dependencies: put explicit imports or an approved
installation step before running the saved script.

`ModuleNotFoundError` means an import is unavailable in the current worker. An
asset 404 is a package-delivery problem for the administrator; an incompatible
native wheel needs a browser-compatible alternative. These differ from a runtime
error after a successful import. For spreadsheets, `openpyxl` handles modern
`.xlsx`; legacy `.xls` may require `xlrd` if the host permits it. Do not replay an
arbitrary recorded `micropip.freeze()` inventory as an installation command.

## Bounds, storage and sharing

The [configured limits](../ops/install.md#limits) bound retained files, transfers
and returned text. Scratch stdout/stderr logs have the same per-stream text cap.
Other scratch still consumes browser memory; these limits do not
promise enough RAM for a particular calculation. Large results need smaller
retained outputs, not a blind retry or a larger callback timeout.

Only new or changed outputs from successful execution become chat attachments.
Programs, state and run records stay private owner/admin-visible Files. Printing
data returns it to the assistant. Review outputs before sharing. Provider limits,
deleted Files and revoked grants can make historical downloads unavailable;
status is not an availability check. New empty retained files require local
storage. Saved data is never silently pruned or recreated.

## Model response stopping

The durable gate bounds dispatches and receipt growth, not the model's token use.
Set a finite native tool-iteration limit on the host. The optional
[`stop_on_browser_failure` setting](../ops/install.md#native-response-stopping)
uses a separately qualified native cancellation path. It is disabled by default.
The first browser failure returns recovery guidance to the assistant, including
when stopping is enabled. Repeated failed browser actions or blocked executes in
that request can stop it. A first fenced execute in a new request also returns
recovery instructions without allocating or dispatching; status/retry/recover remain allowed.
An execute/execute/status batch can therefore stop before status is reached;
arbitrary batch ordering is not guaranteed. Successful retry/recover of the blocked
operation releases only that request's gate after checkpoint acknowledgement;
failed-session gates remain in force. Status and failed recovery never release it.
It cannot make the browser stop or guarantee that host output from other calls
in the same batch was appended. Committed Files and receipts remain discoverable
through status/retry even when that host output is missing.

The Tool checks the expected generation atomically before writing its native
error. The host's later cancellation finalization has no equivalent generation
fence. Avoid simultaneous regeneration and stopping of the same assistant message;
the Tool does not make that host race safe.
