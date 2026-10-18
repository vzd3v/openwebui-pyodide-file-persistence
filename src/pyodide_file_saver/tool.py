"""Native Tool entrypoint: restore, browser execute, checkpoint, publish."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from .browser_execution import (
    checkpoint_bytes,
    consume_callback,
    make_wrapper,
    read_checkpoint,
)
from .contracts import (
    Action,
    BoundContext,
    FileBytes,
    FileSlot,
    Limits,
    Receipt,
    SaverError,
    digest,
    issued_paths,
    json_text,
    markdown_link,
    safe_filename,
)
from .openwebui_adapter import OpenWebUIAdapter, require_string

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
    }


async def resume_publication(
    adapter: OpenWebUIAdapter, context: BoundContext, operation_id: str, limits: Limits
) -> Receipt:
    receipt = await adapter.state.get(context, operation_id)
    if receipt.state == "allocated":
        raise SaverError("No received checkpoint is committed; retry cannot rerun Python")
    checkpoint = await adapter.verify_slot(context, receipt.checkpoint, limits.max_envelope_bytes)
    files = read_checkpoint(checkpoint, limits)
    if [file.manifest() for file in files] != receipt.files:
        raise SaverError("Checkpoint manifest differs from its saved receipt")
    if receipt.state == "saved":
        receipt = await adapter.state.accept(context, operation_id)
    if receipt.state == "conflict" or not receipt.python_ok:
        return receipt
    by_path = {file.path: file.data for file in files}
    for slot in receipt.outputs:
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
    result["stdout"] = receipt.stdout
    result["stderr"] = receipt.stderr
    if receipt.state == "conflict":
        result["incomplete"] = True
        result["recovery"] = (
            "This checkpoint is retained but conflicts with a newer workspace head. "
            "It was not selected or published. Do not rerun or overwrite it automatically."
        )
    elif not receipt.python_ok:
        result["incomplete"] = True
        result["recovery"] = (
            "Python failed. Written files were checkpointed privately; error-produced outputs "
            "were not published. Correct the program in a new execution."
        )
    elif receipt.state == "published":
        links: list[JsonValue] = [
            {
                "name": safe_filename(slot.path),
                "markdown": markdown_link(safe_filename(slot.path), slot.file_id),
                "download_url": f"/api/v1/files/{slot.file_id}/content?attachment=true",
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
            "Retry this saved operation_id without code; do not execute Python again."
        )
    return result


class Tools:
    class Valves(Limits):
        """Conservative tunable transfer limits, always below stock transport ceilings."""

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
        """Run Python in the user's browser with automatic durable file saving.

        Work in the current managed directory using relative paths. Write files
        issued to the user under outputs/; other files are private workspace
        recovery material. Inputs are restored as inputs/<file-id>/<name>.
        Print calculations you want returned. Successful execution automatically
        publishes new/byte-changed outputs. Include supplied Markdown download
        links verbatim in the answer, including for shared chats; never substitute
        filesystem paths, attachment cards or instructions to open Controls.
        Ordinary Python errors still save written files privately, without issuance.
        Use action='retry' with a returned operation_id and no code to finish a
        saved publication without rerunning Python. Use action='status' and no
        code to find recent operations after a lost response. Status reports past
        acknowledgements, not current byte/grant availability; retry revalidates
        without rerunning Python before returning links. A new 'execute' always
        runs a new program: never use it as an automatic retry.

        :param code: Python source for execute only; write issued documents in outputs/.
        :param action: execute, retry or status.
        :param operation_id: Server-issued saved operation ID for retry or specific status.
        :param language: Only python is supported.
        """
        current_operation = ""
        adapter: OpenWebUIAdapter | None = None
        context: BoundContext | None = None
        try:
            call = Action.model_validate(
                {"code": code, "action": action, "operation_id": operation_id, "language": language}
            )
            # Cached Tool instances carry configuration, never actor/workspace state.
            limits = Limits.model_validate(self.valves.model_dump())
            if __request__ is None or not __user__:
                raise SaverError("Authenticated Open WebUI invocation context is required")
            adapter = OpenWebUIAdapter()
            context = await adapter.bind(
                require_string(__user__.get("id")),
                require_string(__chat_id__),
                require_string(__message_id__),
            )
            await adapter.initialize()
            if call.action == "status":
                receipts = (
                    [await adapter.state.get(context, call.operation_id)]
                    if call.operation_id
                    else await adapter.state.pending(context)
                )
                return json_text(
                    {
                        "operations": [receipt_summary(item) for item in receipts],
                        "availability": "not_rechecked",
                        "instructions": (
                            "Receipts record past acknowledgements, not current availability. "
                            "Bytes, destinations or grants may since have been deleted or revoked. "
                            "Use retry with the saved operation_id and no code to revalidate "
                            "without rerunning Python before returning download links."
                        ),
                    }
                )
            if call.action == "retry":
                current_operation = call.operation_id
                receipt = await resume_publication(adapter, context, call.operation_id, limits)
            else:
                if __event_call__ is None:
                    raise SaverError("A connected browser session is required for Python execution")
                receipt, head = await adapter.state.allocate(context)
                current_operation = receipt.operation_id
                restore: list[FileBytes] = []
                if head:
                    data = await adapter.verify_slot(
                        context, head.checkpoint, limits.max_envelope_bytes
                    )
                    restore = read_checkpoint(data, limits)
                    if [file.manifest() for file in restore] != head.files:
                        raise SaverError("Workspace head manifest is inconsistent")
                attachments = __files__ or []
                if len(attachments) > limits.max_files:
                    raise SaverError("Attached input count exceeds the configured limit")
                restore = await adapter.input_files(context, attachments, restore, limits)
                wrapper, nonce = make_wrapper(
                    receipt.operation_id, call.code, restore, limits, adapter.blocked_modules
                )
                payload: dict[str, JsonValue] = {
                    "type": "execute:python",
                    "data": {
                        "id": receipt.operation_id,
                        "code": wrapper,
                        "files": [],
                        "session_id": (__metadata__ or {}).get("session_id"),
                    },
                }
                if len(json_text(payload).encode("utf-8")) > limits.max_envelope_bytes:
                    raise SaverError("Serialized native request exceeds the transfer limit")
                async with asyncio.timeout(limits.callback_timeout_seconds):
                    raw = await __event_call__(payload)
                envelope, files = consume_callback(raw, receipt.operation_id, nonce, limits)
                checkpoint = checkpoint_bytes(files)
                if len(checkpoint) > limits.max_envelope_bytes:
                    raise SaverError("Serialized checkpoint exceeds the transfer limit")
                manifests = [file.manifest() for file in files]
                selected: set[str] = (
                    issued_paths(manifests, receipt.baseline) if envelope.outcome == "ok" else set()
                )
                async with adapter.state.transaction() as session:
                    saved = await adapter.state.load(
                        session, receipt.operation_id, context, lock=True
                    )
                    if saved.state != "allocated":
                        raise SaverError("Operation already received a checkpoint")
                    saved.files = manifests
                    saved.execution_completed = True
                    saved.python_ok = envelope.outcome == "ok"
                    saved.stdout, saved.stderr = envelope.stdout, envelope.stderr
                    saved.checkpoint.sha256 = digest(checkpoint)
                    saved.checkpoint.size = len(checkpoint)
                    saved.outputs = [
                        FileSlot(
                            path=item.path, file_id=str(uuid4()), sha256=item.sha256, size=item.size
                        )
                        for item in manifests
                        if item.path in selected
                    ]
                    await adapter.state.put(session, saved)
                await adapter.store_slot(
                    context, receipt.operation_id, None, checkpoint, limits.max_envelope_bytes
                )
                receipt = await resume_publication(adapter, context, receipt.operation_id, limits)
            result = final_result(receipt)
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
        except Exception as error:
            if isinstance(error, (SaverError, TimeoutError)):
                detail = (
                    str(error)
                    if isinstance(error, SaverError)
                    else (
                        "Browser callback timed out. Worker termination is not confirmed; "
                        "previous acknowledged workspace is preserved."
                    )
                )
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
            if adapter and context and current_operation:
                try:
                    result["receipt"] = receipt_summary(
                        await adapter.state.get(context, current_operation)
                    )
                except Exception:
                    result["receipt_unavailable"] = True
            # No exception repr/traceback: DB/provider errors can contain payloads or secrets.
            SAVER_LOG.warning(
                json_text(
                    {
                        "component": "execution",
                        "operation": current_operation,
                        "error_type": type(error).__name__,
                    }
                )
            )
            return json_text(result)
