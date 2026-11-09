"""Native Tool entrypoint: restore, browser execute, checkpoint, publish."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from pydantic import Field, JsonValue, ValidationError, field_validator

from .browser_execution import read_checkpoint
from .contracts import (
    TOOL_RELEASE,
    ActionName,
    BoundContext,
    BrowserFailure,
    ExecutionBlocked,
    LanguageName,
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
from .tool_results import (
    correctable_refusal,
    exception_diagnostic,
    refusal_origin,
    select_input_attachments,
    tool_result,
    validate_invocation,
)
from .workspace_execution import execute_workspace, verify_manifest
from .workspace_manifest import manifest_bytes, parse_manifest, retained_ready
from .workspace_recovery import recovery_plan
from .workspace_transfer import reconcile_native_uploads

EventCall = Callable[[dict[str, JsonValue]], Awaitable[object]]
EventEmitter = Callable[[dict[str, JsonValue]], Awaitable[object]]
SAVER_LOG = logging.getLogger("pyodide_file_saver")
PRESENTATION_TIMEOUT_SECONDS = 2.0


def log_stop_failure(
    correlation: dict[str, JsonValue], operation_id: str, phase: str, error: Exception
) -> None:
    SAVER_LOG.warning(
        json_text(
            {
                **correlation,
                "component": "presentation",
                "event": "native_cancellation_presentation_failed",
                "operation": operation_id,
                "phase": phase,
                **exception_diagnostic(error),
            }
        )
    )


def receipt_summary(receipt: Receipt) -> dict[str, JsonValue]:
    return {
        "operation_id": receipt.operation_id,
        "state": receipt.state,
        "workspace_saved": receipt.checkpoint.committed,
        "persistence_outcome": "committed"
        if receipt.checkpoint.committed
        else "pending"
        if receipt.execution_completed
        else "unchanged",
        "publication_outcome": "published" if receipt.state == "published" else "not_published",
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
            "termination is confirmed. Status and recover do not rerun source. "
            "Follow recovery_plan for this operation's recorded execution state."
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
        "first_failure_code": diagnostic.first_failure_code or None,
        "first_failure_phase": diagnostic.first_failure_phase or None,
        "first_failure_at": diagnostic.first_failure_at,
        "last_failure_at": diagnostic.last_failure_at,
        "cause": diagnostic.cause.model_dump() if diagnostic.cause else None,
        "first_failure_cause": diagnostic.first_failure_cause.model_dump()
        if diagnostic.first_failure_cause
        else None,
        "input_origin": diagnostic.input_origin or None,
        **(
            {"execution_outcome": "not_started"}
            if not receipt.execution_completed
            and diagnostic.dispatch == "not_dispatched"
            and diagnostic.failure_code
            else {}
        ),
        "recovery_plan": recovery_plan(receipt, diagnostic),
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
        if receipt.native_transfers is not None:
            receipt = await reconcile_native_uploads(adapter, context, receipt, limits)
        if not (
            receipt.execution_completed
            and receipt.checkpoint_version == 2
            and retained_ready(receipt)
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
    if receipt.state == "conflict":
        result["permitted_actions"] = ["status"]
        return tool_result(result, error_code="workspace_conflict", message=str(result["recovery"]))
    if not receipt.python_ok:
        result["permitted_actions"] = ["execute", "status"]
        return tool_result(result, error_code="program_error", message=str(result["recovery"]))
    if receipt.state != "published":
        result["permitted_actions"] = ["status", "retry"]
        return tool_result(
            result, error_code="publication_pending", message=str(result["recovery"])
        )
    result["permitted_actions"] = ["execute", "status", "retry"]
    return tool_result(result, message="Execution completed and files were saved")


class Tools:
    class Valves(Limits):
        """Conservative tunable transfer limits, always below stock transport ceilings."""

        stop_on_browser_failure: bool = Field(
            default=False,
            description=(
                "Stop repeated blocked browser actions after returning recovery guidance once. "
                "Enable only after native host acceptance; the first failure is a Tool result."
            ),
        )

        @field_validator(
            "max_files",
            "max_file_bytes",
            "max_total_bytes",
            "max_input_bytes",
            "transfer_timeout_seconds",
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
        action: ActionName = "execute",
        operation_id: str = "",
        language: LanguageName = "python",
        input_file_ids: list[str] | None = None,
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

        Each Python call has fresh globals: import names again or run a saved script.
        Before creating large or many retained files, use status with no code to
        inspect current limits and estimate output sizes. Do not create files above them.
        If execution completed but saving failed, ANY new execute is forbidden,
        including deleting, resizing or repacking its outputs. Follow recovery_plan
        or explain the limit; do not try a cleanup program in the fenced workspace.
        Native OS processes are unavailable: do not use os.system, subprocess or
        python/ffmpeg executables. Run a saved script inside Python with exec after
        importing its dependencies and Path when using pathlib. A failed network
        request alone does not prove that networking is prohibited. Keep each path
        component within 255 UTF-8 bytes.
        Explain failures and permitted next steps in the user's request language.
        Correct invalid calls/programs; never repeat an unchanged rejected call.
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
        use Python for Pyodide packages. Each shell call has a fresh environment. Warm workers
        can retain installed packages, but a fresh browser does not reinstall them.
        Static Python imports provide host package-loading hints; dynamic import
        probes do not. Native subprocesses, threads and network shell commands are
        unavailable. Bounded logs remain transient under work/;
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
        Static imports load packages from this runtime's Pyodide catalog before execution.
        Saved scripts require explicit outer imports or approved explicit package loading.
        input_file_ids selects only current attachments: omit it to admit all, or [] to
        admit none. Previously acknowledged inputs remain required and access-checked.
        Do not silently exclude an input the user's task needs just to bypass a refusal.

        :param code: Python or shell source for execute; write issued documents in outputs/.
        :param action: execute, retry, recover or status (includes current limits; no code).
        :param operation_id: Server-issued saved operation ID for retry or specific status.
        :param language: python or bash (browser just-bash; no native OS processes).
        :param input_file_ids: Current attachment IDs for execute; [] selects no new inputs.
        """
        current_operation = ""
        adapter: OpenWebUIAdapter | None = None
        context: BoundContext | None = None
        stop_ready = False
        limits: Limits | None = None
        effective_upload_limit: int | None = None
        upload_policy_status = "not_checked"
        invocation_id = str(uuid4())
        started_at = time.monotonic()
        stage = "validation"
        correlation: dict[str, JsonValue] = {
            "invocation_id": invocation_id,
            "release": TOOL_RELEASE,
        }
        for key, value in (
            ("chat_id", __chat_id__),
            ("message_id", __message_id__),
            ("task_id", OpenWebUIAdapter.task_id(__metadata__ or {})),
        ):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                correlation[key] = value
        if action in ("execute", "status", "retry", "recover"):
            correlation["action"] = action

        def finish(value: dict[str, JsonValue]) -> str:
            value["invocation_id"] = invocation_id
            if limits is not None:
                value["limits"] = {
                    "configured": limits.model_dump(),
                    "effective_upload_max_file_bytes": effective_upload_limit,
                    "upload_policy_status": upload_policy_status,
                    "other_native_constraints": "not_preflighted",
                    "file_transfer": (
                        "New files use binary Files HTTP; max_envelope_bytes bounds "
                        "metadata and legacy checkpoints, not new file size"
                    ),
                }
            event: dict[str, JsonValue] = {
                **correlation,
                "component": "execution",
                "event": "invocation_finished",
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                **({"operation": current_operation} if current_operation else {}),
            }
            for field in (
                "status",
                "error_code",
                "phase",
                "execution_outcome",
                "persistence_outcome",
                "publication_outcome",
                "cause",
                "error_details",
                "error_type",
                "failure_frame",
                "failure_line",
            ):
                if field in value:
                    event[field] = value[field]
            # Validation details contain a correction example; only fixed structural
            # fields and numeric limits enter operational logs.
            details = value.get("error_details")
            if isinstance(details, dict):
                event["error_details"] = {
                    key: details[key]
                    for key in (
                        "field",
                        "type",
                        "file_id",
                        "limit_bytes",
                        "size_bytes",
                        "size_lower_bound_bytes",
                        "limit_count",
                        "count",
                        "input_origin",
                    )
                    if key in details
                }
            (SAVER_LOG.warning if value.get("status") == "error" else SAVER_LOG.info)(
                json_text(event)
            )
            return json_text(value)

        SAVER_LOG.info(
            json_text({**correlation, "component": "execution", "event": "invocation_started"})
        )
        try:
            call = validate_invocation(code, action, operation_id, language, input_file_ids)
            stage = "configuration"
            # Cached Tool instances carry configuration, never actor/workspace state.
            limits = Limits.model_validate(
                self.valves.model_dump(exclude={"stop_on_browser_failure"})
            )
            if __request__ is None or not __user__:
                raise SaverError(
                    "Authenticated Open WebUI invocation context is required",
                    "invocation_context_unavailable",
                )
            stage = "admission"
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
            try:
                effective_upload_limit = await adapter.host_max_bytes(limits.max_file_bytes)
                upload_policy_status = "checked"
            except SaverError:
                # Capability reporting must not make read-only status unavailable.
                # Actual storage rechecks this policy and remains fail-closed.
                upload_policy_status = "unavailable"
            selected_attachments = select_input_attachments(__files__ or [], call.input_file_ids)
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
                return finish(
                    tool_result(
                        {
                            "operations": [
                                await operation_summary(
                                    adapter, context, item, streams=bool(call.operation_id)
                                )
                                for item in receipts
                            ],
                            "head": await operation_summary(adapter, context, head)
                            if head
                            else None,
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
                                "Bytes, destinations or grants may have been deleted or revoked. "
                                "Use retry with the saved operation_id and no code to revalidate "
                                "without rerunning code before returning download links."
                            ),
                        },
                        message="Operation status read; file availability was not rechecked",
                    )
                )
            if call.action == "retry":
                existing = await adapter.state.get(context, call.operation_id)
                current_operation = existing.operation_id
                receipt = await resume_publication(adapter, context, call.operation_id, limits)
            else:
                if __event_call__ is None:
                    raise SaverError(
                        "A connected browser session is required for execution",
                        "session_unavailable",
                    )
                blocked_modules = await adapter.execution_policy()
                if call.action == "recover":
                    receipt = await adapter.state.get(context, call.operation_id)
                    current_operation = receipt.operation_id
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
                    selected_attachments,
                    limits,
                    __event_call__,
                    blocked_modules,
                    (__metadata__ or {}).get("session_id"),
                    recovering=call.action == "recover",
                    metadata=__metadata__ or {},
                )
                receipt = await resume_publication(
                    adapter, context, receipt.operation_id, limits, read_blobs=False
                )
            if call.action in {"retry", "recover"} and receipt.state in {"accepted", "published"}:
                await adapter.state.release_request_gate(
                    context,
                    receipt.operation_id,
                    adapter.invocation_diagnostics(context, __metadata__ or {}).request_key,
                )
            result = final_result(receipt)
            result.update(await operation_summary(adapter, context, receipt))
            if receipt.state == "published" and __event_emitter__:
                try:
                    async with asyncio.timeout(PRESENTATION_TIMEOUT_SECONDS):
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
                                **correlation,
                                "operation": current_operation,
                                "error_type": type(error).__name__,
                            }
                        )
                    )
            return finish(result)
        except WorkspaceInvocationStopped:
            SAVER_LOG.warning(
                json_text(
                    {
                        **correlation,
                        "component": "execution",
                        "event": "invocation_cancelled",
                        **({"operation": current_operation} if current_operation else {}),
                        "duration_ms": round((time.monotonic() - started_at) * 1000),
                    }
                )
            )
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
                    SAVER_LOG.warning(
                        json_text(
                            {
                                **correlation,
                                "component": "execution",
                                "event": "cancellation_diagnostic_failed",
                            }
                        )
                    )
            SAVER_LOG.warning(
                json_text(
                    {
                        **correlation,
                        "component": "execution",
                        "event": "invocation_cancelled",
                        **({"operation": current_operation} if current_operation else {}),
                        "duration_ms": round((time.monotonic() - started_at) * 1000),
                    }
                )
            )
            raise
        except Exception as error:
            first_refusal = False
            repeated_browser_failure = False
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
                        SAVER_LOG.warning(
                            json_text(
                                {
                                    **correlation,
                                    "component": "execution",
                                    "event": "request_refusal_persistence_failed",
                                    "operation": current_operation,
                                }
                            )
                        )
            if isinstance(error, SaverError):
                detail = str(error)
                error_code = error.error_code or "host_operation_failed"
            elif isinstance(error, ValidationError):
                detail = "Invalid invocation, configuration or saved data"
                error_code = (
                    "invalid_configuration" if stage == "configuration" else "invalid_saved_data"
                )
            else:
                detail = "Host/storage operation failed; inspect the saved receipt before retrying"
                error_code = "host_operation_failed"
            result: dict[str, JsonValue] = {
                "incomplete": True,
                **({"operation_id": current_operation} if current_operation else {}),
                "execution_outcome": "not_started" if not current_operation else "unknown",
                "persistence_outcome": "unchanged" if not current_operation else "pending",
                "publication_outcome": "not_published",
                "phase": stage,
                **exception_diagnostic(error),
                "recovery": (
                    "Use status to inspect this operation; retry only a committed saved "
                    "operation, without code."
                ),
            }
            if isinstance(error, SaverError) and error.details:
                result["error_details"] = error.details
            origin = refusal_origin(error.details) if isinstance(error, SaverError) else ""
            correctable = correctable_refusal(error_code, origin)
            if stage == "validation" or error_code == "invalid_input_selection":
                result["recovery"] = "Correct the indicated arguments; status accepts no code."
                result["permitted_actions"] = ["execute", "status", "retry", "recover"]
            if correctable:
                result["recovery"] = (
                    "Correct the selected inputs or reduce their size; "
                    "do not repeat the unchanged call."
                )
            if isinstance(error, ExecutionBlocked):
                result["first_refusal"] = first_refusal
                result["recovery"] = (
                    "Execution was refused without allocating or dispatching a new operation. "
                    "Use status, retry or recover with the existing operation_id and no code. "
                    "Do not submit another execute in this request."
                )
            if isinstance(error, BrowserFailure):
                result["phase"] = error.phase
                if error.cause:
                    result["cause"] = error.cause.model_dump()
            if adapter and context and current_operation:
                try:
                    if not isinstance(error, ExecutionBlocked):
                        if isinstance(error, BrowserFailure):
                            repeated_browser_failure = not await adapter.state.record_refusal(
                                context,
                                adapter.invocation_diagnostics(context, __metadata__ or {}),
                                current_operation,
                            )
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
                            request_failed=not correctable,
                            cause=error.cause if isinstance(error, BrowserFailure) else None,
                            input_origin=origin,
                        )
                    receipt = await adapter.state.get(context, current_operation)
                    summary = await operation_summary(adapter, context, receipt)
                    result["receipt"] = summary
                    for field in (
                        "execution_outcome",
                        "persistence_outcome",
                        "publication_outcome",
                        "phase",
                    ):
                        result[field] = summary[field]
                    result.update(receipt_diagnostics(receipt))
                    diagnostic = await adapter.state.diagnostics(context, current_operation)
                    plan = recovery_plan(receipt, diagnostic)
                    result["recovery_plan"] = plan
                    result["recovery"] = plan["instructions"]
                    result["permitted_actions"] = plan["tool_actions"]
                except Exception:
                    result["receipt_unavailable"] = True
            result = tool_result(result, error_code=error_code, message=detail)
            if (
                stop_ready
                and adapter
                and context
                and isinstance(error, (BrowserFailure, ExecutionBlocked))
                and (
                    repeated_browser_failure
                    or (isinstance(error, ExecutionBlocked) and not first_refusal)
                )
            ):
                diagnostic = {
                    "operation_id": current_operation,
                    "error_code": error.error_code,
                    "receipt": result.get("receipt"),
                }
                native_error: dict[str, JsonValue] | None = None
                try:
                    async with asyncio.timeout(PRESENTATION_TIMEOUT_SECONDS):
                        native_error = await adapter.persist_stop(
                            context, __metadata__ or {}, diagnostic
                        )
                except Exception as stop_error:
                    log_stop_failure(correlation, current_operation, "persist", stop_error)
                try:
                    async with asyncio.timeout(PRESENTATION_TIMEOUT_SECONDS):
                        await adapter.validate_generation(
                            context, __metadata__ or {}, require_task=True
                        )
                except Exception as generation_error:
                    log_stop_failure(correlation, current_operation, "generation", generation_error)
                    finish(result)
                    raise WorkspaceInvocationStopped() from None
                if native_error is None:
                    # Acknowledgement is required before replacing the ordinary
                    # failed Tool result with cancellation. Its durable gate
                    # still forbids replay; the agent can explain this result.
                    return finish(result)
                if __event_emitter__:
                    try:
                        async with asyncio.timeout(PRESENTATION_TIMEOUT_SECONDS):
                            await __event_emitter__(
                                {
                                    "type": "chat:completion",
                                    "data": {
                                        "error": native_error,
                                    },
                                }
                            )
                    except Exception as delivery_error:
                        log_stop_failure(correlation, current_operation, "emit", delivery_error)
                finish(result)  # Log the last known facts; do not return or schedule inference.
                raise WorkspaceInvocationStopped() from None
            return finish(result)
