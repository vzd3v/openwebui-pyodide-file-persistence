"""Native Tool entrypoint: restore, browser execute, checkpoint, publish."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from pydantic import Field, JsonValue, ValidationError, field_validator

from .browser_execution import read_checkpoint
from .contracts import (
    Action,
    BoundContext,
    BrowserFailure,
    ExecutionBlocked,
    Limits,
    Receipt,
    SaverError,
    WorkspaceInvocationStopped,
    digest,
    download_url,
    json_text,
    markdown_link,
    safe_filename,
)
from .openwebui_adapter import OpenWebUIAdapter, require_string
from .workspace_execution import execute_workspace, verify_manifest
from .workspace_manifest import manifest_bytes, parse_manifest

EventCall = Callable[[dict[str, JsonValue]], Awaitable[object]]
EventEmitter = Callable[[dict[str, JsonValue]], Awaitable[object]]
SAVER_LOG = logging.getLogger("pyodide_file_saver")


def receipt_summary(receipt: Receipt) -> dict[str, JsonValue]:
    return {
        "operation_id": receipt.operation_id,
        "state": receipt.state,
        "workspace_saved": receipt.checkpoint.committed,
        "execution_completed": receipt.execution_completed,
        "python_ok": receipt.python_ok,
        "language": receipt.language,
        "browser_workspace": {
            "mode": "unknown"
            if receipt.browser_warm is None
            else "warm"
            if receipt.browser_warm
            else "restored"
            if receipt.base_operation_id
            else "new",
            "base_operation_id": receipt.base_operation_id,
            "base_revision": receipt.base_revision,
        },
        "exit_code": receipt.exit_code if receipt.execution_completed else None,
        "execution_outcome": ("ok" if receipt.python_ok else "error")
        if receipt.execution_completed
        else "unknown",
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
    }


def receipt_diagnostics(receipt: Receipt) -> dict[str, JsonValue]:
    """Accepted output is evidence of execution, never of successful persistence."""
    result: dict[str, JsonValue] = {}
    if receipt.execution_completed:
        result.update(
            stdout=receipt.stdout,
            stderr=receipt.stderr,
            execution_outcome="ok" if receipt.python_ok else "error",
            exit_code=receipt.exit_code,
            stdout_truncated=receipt.stdout_truncated,
            stderr_truncated=receipt.stderr_truncated,
        )
    if not receipt.checkpoint.committed:
        result["workspace_context"] = (
            "The last acknowledged workspace head remains selected. "
            "The old browser root may still exist; no deletion or worker "
            "termination is confirmed. Use recover with this operation_id and no code "
            "in the original live browser to checkpoint completed pending work. "
            "Unknown execution blocks new programs in this workspace."
        )
    return result


async def operation_summary(
    adapter: OpenWebUIAdapter, context: BoundContext, receipt: Receipt, *, streams: bool = False
) -> dict[str, JsonValue]:
    diagnostic = await adapter.state.diagnostics(context, receipt.operation_id)
    return {
        **receipt_summary(receipt),
        **(receipt_diagnostics(receipt) if streams else {}),
        "phase": diagnostic.phase,
        "dispatch": diagnostic.dispatch,
        "failure_code": diagnostic.failure_code or None,
        "request_scope": diagnostic.request_scope,
        **({"retained_files": [slot.model_dump() for slot in receipt.blobs]} if streams else {}),
    }


async def resume_publication(
    adapter: OpenWebUIAdapter,
    context: BoundContext,
    operation_id: str,
    limits: Limits,
    *,
    read_blobs: bool = True,
) -> Receipt:
    receipt = await adapter.state.get(context, operation_id)
    if receipt.state == "allocated":
        if not (
            receipt.execution_completed
            and receipt.checkpoint_version == 2
            and all(blob.committed for blob in receipt.blobs)
        ):
            raise SaverError(
                "No complete committed blob set is available; recover requires the original "
                "live browser, and retry never reruns code"
            )
        for blob in receipt.blobs:
            await adapter.verify_slot(context, blob, limits.max_file_bytes)
        for slot in receipt.inputs:
            await adapter.authorize_file(context, slot.file_id)
        checkpoint = manifest_bytes(receipt)
        parse_manifest(checkpoint, receipt, limits)
        if len(checkpoint) > limits.max_envelope_bytes:
            raise SaverError("Retained manifest exceeds transport limit")
        async with adapter.state.transaction() as session:
            saved = await adapter.state.load(session, operation_id, context, lock=True)
            if saved.state == "allocated":
                saved.checkpoint.sha256, saved.checkpoint.size = digest(checkpoint), len(checkpoint)
                await adapter.state.put(session, saved)
        receipt = await adapter.store_slot(
            context, operation_id, None, checkpoint, limits.max_envelope_bytes
        )
    if receipt.checkpoint_version == 2:
        await verify_manifest(adapter, context, receipt, limits, read_blobs=read_blobs)
        files = []
    else:
        checkpoint = await adapter.verify_slot(
            context, receipt.checkpoint, limits.max_envelope_bytes
        )
        files = read_checkpoint(checkpoint, limits)
        if [file.manifest() for file in files] != receipt.files:
            raise SaverError("Checkpoint manifest differs from its saved receipt")
    if receipt.state == "saved":
        receipt = await adapter.state.accept(context, operation_id)
    if receipt.state == "conflict" or not receipt.python_ok:
        return receipt
    by_path = {file.path: file.data for file in files}
    for slot in receipt.outputs:
        if receipt.checkpoint_version == 2:
            if not slot.committed:
                raise SaverError("Issued retained blob is not committed")
            continue
        data = by_path.get(slot.path)
        if data is None:
            raise SaverError("Reserved issued bytes are missing from the saved checkpoint")
        receipt = await adapter.store_slot(
            context, operation_id, slot.path, data, limits.max_file_bytes
        )
    receipt = await adapter.publish(context, operation_id)
    await adapter.verify_publication(context, receipt, limits)
    return receipt


def final_result(receipt: Receipt) -> dict[str, JsonValue]:
    result = receipt_summary(receipt)
    result.update(receipt_diagnostics(receipt))
    if receipt.state == "conflict":
        result["incomplete"] = True
        result["recovery"] = (
            "This checkpoint is retained but conflicts with a newer workspace head. "
            "It was not selected or published. Do not rerun or overwrite it automatically."
        )
    elif not receipt.python_ok:
        result["incomplete"] = True
        result["recovery"] = (
            "Execution failed. Written files were checkpointed privately; error-produced outputs "
            "were not published. Correct the program in a new execution."
        )
    elif receipt.state == "published":
        links: list[JsonValue] = [
            {
                "name": safe_filename(slot.path),
                "relative_path": slot.path,
                "size": slot.size,
                "sha256": slot.sha256,
                "markdown": markdown_link(
                    safe_filename(slot.path), slot.file_id, slot.representation
                ),
                "download_url": download_url(slot.file_id, slot.representation),
                "attachment_url": f"/api/v1/files/{slot.file_id}",
            }
            for slot in receipt.outputs
        ]
        result["issued_files"] = links
        result["instructions"] = (
            "Include each supplied Markdown download link verbatim in your final answer. "
            "Attachment cards or Controls are not a substitute for links in the answer. "
            "Do not invent filesystem URLs. Unchanged inherited outputs are not newly issued."
        )
    else:
        result["incomplete"] = True
        result["recovery"] = (
            "Retry this saved operation_id without code; do not execute code again."
        )
    return result


class Tools:
    class Valves(Limits):
        """Conservative tunable transfer limits, always below stock transport ceilings."""

        stop_on_browser_failure: bool = Field(
            default=False,
            description="Opt-in native cancellation; enable only after host acceptance tests.",
        )

        @field_validator(
            "max_files",
            "max_file_bytes",
            "max_total_bytes",
            "max_envelope_bytes",
            "max_text_bytes",
            "callback_timeout_seconds",
            mode="before",
        )
        @classmethod
        def parse_integer_setting(cls, value: object) -> object:
            # Native settings inputs serialize numbers as strings. Normalize
            # only that configuration boundary, never runtime/browser contracts.
            if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value):
                return int(value)
            return value

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def execute_code(
        self,
        code: str = "",
        action: str = "execute",
        operation_id: str = "",
        language: str = "python",
        __user__: dict[str, JsonValue] | None = None,
        __chat_id__: str | None = None,
        __message_id__: str | None = None,
        __request__: object = None,
        __metadata__: dict[str, JsonValue] | None = None,
        __files__: list[dict[str, JsonValue]] | None = None,
        __event_call__: EventCall | None = None,
        __event_emitter__: EventEmitter | None = None,
    ) -> str:
        """Run Python or Bash in the user's browser with durable retained files.

        Every call starts at the workspace root, shared by Python and Bash. Use
        work/ for transient scratch. Write files
        issued to the user under outputs/; scripts/ holds private retained source and recipes.
        Scratch work/ is not saved. Inputs are restored as inputs/<file-id>/<name>.
        Print calculations you want returned. Successful execution automatically
        publishes new/byte-changed outputs. Include supplied Markdown download
        links verbatim in the answer, including for shared chats; never substitute
        filesystem paths, attachment cards or instructions to open Controls.
        Ordinary Python errors and nonzero Bash exits save retained files privately.
        Bash provides jq, rg, find, head, tail, sed, awk and ordinary file commands;
        use Python for Pyodide packages. Every call has a fresh Python namespace
        or shell environment; import names again or run a saved script. Warm workers
        can retain installed packages, but a fresh browser does not reinstall them.
        Static Python imports provide host package-loading hints; dynamic import
        probes do not. Native subprocesses, threads and network shell commands are
        unavailable. Full logs remain transient under work/;
        returned text is bounded with truncation flags and an exit code.
        Empty retained files require native local storage; remote providers do not
        support the native metadata-backed empty File lifecycle.
        Use action='retry' with a returned operation_id and no code to finish a
        saved publication without rerunning code. Use action='status' and no
        code to find recent operations after a lost response. Supply operation_id
        to recover its accepted bounded stdout/stderr, including after storage
        failure. Status reports past
        acknowledgements, not current byte/grant availability; retry revalidates
        without rerunning code before returning links. A new 'execute' always
        runs a new program: never use it as an automatic retry.
        Use action='recover' with the pending operation_id and no code to snapshot
        completed work in the original live browser without rerunning source.
        Unknown execution or completed work awaiting a checkpoint blocks new programs
        even after reconnect or a new message. Lost browser bytes,
        scratch, variables and packages cannot be restored. Keep essential intermediate
        data in scripts/state/ and load permitted dependencies before running saved source.

        :param code: Python or shell source for execute; write issued documents in outputs/.
        :param action: execute, retry, recover or status.
        :param operation_id: Server-issued saved operation ID for retry or specific status.
        :param language: python or bash (browser just-bash; no native OS processes).
        """
        current_operation = ""
        adapter: OpenWebUIAdapter | None = None
        context: BoundContext | None = None
        stop_ready = False
        try:
            call = Action.model_validate(
                {
                    "code": code,
                    "action": action,
                    "operation_id": operation_id,
                    "language": language,
                    "cwd": ".",
                }
            )
            # Cached Tool instances carry configuration, never actor/workspace state.
            limits = Limits.model_validate(
                self.valves.model_dump(exclude={"stop_on_browser_failure"})
            )
            if __request__ is None or not __user__:
                raise SaverError("Authenticated Open WebUI invocation context is required")
            adapter = OpenWebUIAdapter()
            context = await adapter.bind(
                require_string(__user__.get("id")),
                require_string(__chat_id__),
                require_string(__message_id__),
            )
            await adapter.initialize()
            await adapter.validate_generation(
                context,
                __metadata__ or {},
                require_task=self.valves.stop_on_browser_failure
                and call.action in {"execute", "recover"},
            )
            if self.valves.stop_on_browser_failure and call.action in {"execute", "recover"}:
                adapter.require_stop_capability()
                if __event_emitter__ is None:
                    raise SaverError("Native cancellation requires the host event emitter")
                stop_ready = True
            if call.action == "status":
                receipts = (
                    [await adapter.state.get(context, call.operation_id)]
                    if call.operation_id
                    else await adapter.state.pending(context)
                )
                head = await adapter.state.head(context)
                unresolved = await adapter.state.unresolved(context)
                recoverable = await adapter.state.recoverable(context)
                return json_text(
                    {
                        "operations": [
                            await operation_summary(
                                adapter, context, item, streams=bool(call.operation_id)
                            )
                            for item in receipts
                        ],
                        "head": await operation_summary(adapter, context, head) if head else None,
                        "unresolved": [
                            await operation_summary(adapter, context, item)
                            for item in unresolved[:32]
                        ],
                        "unresolved_truncated": len(unresolved) > 32,
                        "recoverable": [
                            await operation_summary(adapter, context, item)
                            for item in recoverable[:32]
                        ],
                        "recoverable_truncated": len(recoverable) > 32,
                        "availability": "not_rechecked",
                        "instructions": (
                            "Receipts record past acknowledgements, not current availability. "
                            "Bytes, destinations or grants may since have been deleted or revoked. "
                            "Use retry with the saved operation_id and no code to revalidate "
                            "without rerunning code before returning download links."
                        ),
                    }
                )
            if call.action == "retry":
                current_operation = call.operation_id
                receipt = await resume_publication(adapter, context, call.operation_id, limits)
            else:
                if __event_call__ is None:
                    raise SaverError("A connected browser session is required for execution")
                blocked_modules = await adapter.execution_policy()
                if call.action == "recover":
                    current_operation = call.operation_id
                    receipt = await adapter.state.get(context, call.operation_id)
                    if receipt.state != "allocated":
                        raise SaverError("Use retry to verify reserved or committed saved bytes")
                    call = call.model_copy(update={"language": receipt.language})
                    head = (
                        await adapter.state.get(context, receipt.base_operation_id)
                        if receipt.base_operation_id
                        else None
                    )
                else:
                    receipt, head = await adapter.state.allocate(
                        context,
                        call.language,
                        diagnostics=adapter.invocation_diagnostics(context, __metadata__ or {}),
                    )
                current_operation = receipt.operation_id
                receipt = await execute_workspace(
                    adapter,
                    context,
                    call,
                    receipt,
                    head,
                    __files__ or [],
                    limits,
                    __event_call__,
                    blocked_modules,
                    (__metadata__ or {}).get("session_id"),
                    recovering=call.action == "recover",
                )
                receipt = await resume_publication(
                    adapter, context, receipt.operation_id, limits, read_blobs=False
                )
            result = final_result(receipt)
            result.update(await operation_summary(adapter, context, receipt))
            if receipt.state == "published" and __event_emitter__:
                try:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": "Workspace saved; issued files are ready",
                                "done": True,
                            },
                        }
                    )
                except Exception as error:
                    # Presentation is not persistence and cannot revoke a verified save.
                    SAVER_LOG.warning(
                        json_text(
                            {
                                "component": "presentation",
                                "operation": current_operation,
                                "error_type": type(error).__name__,
                            }
                        )
                    )
            SAVER_LOG.info(
                json_text(
                    {
                        "component": "execution",
                        "operation": current_operation,
                        "state": receipt.state,
                    }
                )
            )
            return json_text(result)
        except WorkspaceInvocationStopped:
            raise
        except asyncio.CancelledError:
            # The caller's cancellation is distinct from a callback timeout. Do
            # not turn it into an ordinary result or assert worker termination.
            if adapter and context and current_operation:
                try:
                    async with asyncio.timeout(3):
                        diagnostic = await adapter.state.diagnostics(context, current_operation)
                        await adapter.state.mark(
                            context,
                            current_operation,
                            failure_code="invocation_cancelled",
                            dispatch="outcome_unknown"
                            if diagnostic.dispatch == "possibly_running"
                            else None,
                        )
                except Exception:
                    SAVER_LOG.warning("Cancellation diagnostic persistence failed")
            raise
        except Exception as error:
            first_refusal = False
            if isinstance(error, ExecutionBlocked):
                current_operation = error.operation_id
                if adapter and context:
                    try:
                        first_refusal = await adapter.state.record_refusal(
                            context,
                            adapter.invocation_diagnostics(context, __metadata__ or {}),
                            current_operation,
                        )
                    except Exception:
                        SAVER_LOG.warning("Request refusal persistence failed")
            if isinstance(error, SaverError):
                detail = str(error)
            elif isinstance(error, ValidationError):
                detail = "Invalid invocation, configuration or saved data"
            else:
                detail = "Host/storage operation failed; inspect the saved receipt before retrying"
            result: dict[str, JsonValue] = {
                "incomplete": True,
                "error": detail,
                "operation_id": current_operation,
                "recovery": (
                    "Use status to inspect this operation; retry only a committed saved "
                    "operation, without code."
                ),
            }
            if isinstance(error, SaverError) and error.error_code:
                result["error_code"] = error.error_code
            if isinstance(error, ExecutionBlocked):
                result["first_refusal"] = first_refusal
                result["recovery"] = (
                    "Execution was refused without allocating or dispatching a new operation. "
                    "Use status, retry or recover with the existing operation_id and no code. "
                    "Do not submit another execute in this request."
                )
            if isinstance(error, BrowserFailure):
                result["phase"] = error.phase
            if adapter and context and current_operation:
                try:
                    if not isinstance(error, ExecutionBlocked):
                        diagnostic = await adapter.state.diagnostics(context, current_operation)
                        await adapter.state.mark(
                            context,
                            current_operation,
                            dispatch="outcome_unknown"
                            if diagnostic.dispatch == "possibly_running"
                            else None,
                            failure_code=(error.error_code or "host_operation_failed")
                            if isinstance(error, SaverError)
                            else "host_operation_failed",
                            session_failed=isinstance(error, BrowserFailure)
                            and error.error_code in {"session_unavailable", "session_disconnected"},
                        )
                    receipt = await adapter.state.get(context, current_operation)
                    result["receipt"] = await operation_summary(adapter, context, receipt)
                    result.update(receipt_diagnostics(receipt))
                except Exception:
                    result["receipt_unavailable"] = True
            # No exception repr/traceback: DB/provider errors can contain payloads or secrets.
            SAVER_LOG.warning(
                json_text(
                    {
                        "component": "execution",
                        "operation": current_operation,
                        "error_type": type(error).__name__,
                        **(
                            {"error_code": error.error_code}
                            if isinstance(error, SaverError) and error.error_code
                            else {}
                        ),
                        **({"phase": error.phase} if isinstance(error, BrowserFailure) else {}),
                    }
                )
            )
            if (
                stop_ready
                and adapter
                and context
                and (
                    isinstance(error, BrowserFailure)
                    or (isinstance(error, ExecutionBlocked) and not first_refusal)
                )
            ):
                diagnostic = {
                    "operation_id": current_operation,
                    "error_code": error.error_code,
                    "receipt": result.get("receipt"),
                }
                try:
                    async with asyncio.timeout(3):
                        native_error = await adapter.persist_stop(
                            context, __metadata__ or {}, diagnostic
                        )
                        if __event_emitter__:
                            await __event_emitter__(
                                {
                                    "type": "status",
                                    "data": {
                                        "description": native_error["content"],
                                        "done": True,
                                    },
                                }
                            )
                            await __event_emitter__(
                                {
                                    "type": "chat:completion",
                                    "data": {
                                        "error": native_error,
                                    },
                                }
                            )
                except Exception:
                    SAVER_LOG.warning("Native cancellation presentation failed")
                raise WorkspaceInvocationStopped() from None
            return json_text(result)
