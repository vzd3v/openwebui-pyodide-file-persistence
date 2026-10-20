"""Server-side orchestration of browser-only execution and immutable delta commits."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from pydantic import JsonValue

from .browser_asset import shell_asset
from .browser_execution import read_checkpoint
from .browser_workspace import workspace_reply, workspace_wrapper
from .contracts import (
    JSON_VALUE,
    Action,
    BoundContext,
    FileBytes,
    FileSlot,
    Limits,
    Receipt,
    SaverError,
    decode_files,
    digest,
    encode_files,
    issued_paths,
    json_text,
    safe_relative_path,
)
from .openwebui_adapter import OpenWebUIAdapter
from .workspace_manifest import (
    WorkspaceManifest,
    manifest_bytes,
    parse_manifest,
    slot_entry,
    validate_manifest,
)


async def browser_call(
    event_call: Callable[[dict[str, JsonValue]], Awaitable[object]],
    config: dict[str, JsonValue],
    limits: Limits,
    session_id: JsonValue = None,
) -> tuple[object, str]:
    code = config.get("code", "")
    wrapper, nonce = workspace_wrapper(config, code if isinstance(code, str) else "")
    payload: dict[str, JsonValue] = {
        "type": "execute:python",
        "data": {
            "id": config["operation_id"],
            "code": wrapper,
            "files": [],
            "session_id": session_id,
        },
    }
    if len(json_text(payload).encode()) > limits.max_envelope_bytes:
        raise SaverError("Serialized native request exceeds transport limit")
    async with asyncio.timeout(limits.callback_timeout_seconds):
        return await event_call(payload), nonce


async def verify_manifest(
    adapter: OpenWebUIAdapter,
    context: BoundContext,
    receipt: Receipt,
    limits: Limits,
    *,
    read_blobs: bool,
) -> None:
    data = await adapter.verify_slot(context, receipt.checkpoint, limits.max_envelope_bytes)
    manifest = parse_manifest(data, receipt, limits)
    for slot in manifest.files:
        if read_blobs:
            await adapter.verify_slot(context, slot, limits.max_file_bytes)
        else:
            await adapter.authorize_file(context, slot.file_id, owned=True)
    for slot in manifest.inputs:
        await adapter.authorize_file(context, slot.file_id)


async def execute_workspace(
    adapter: OpenWebUIAdapter,
    context: BoundContext,
    call: Action,
    receipt: Receipt,
    head: Receipt | None,
    attachments: list[dict[str, JsonValue]],
    limits: Limits,
    event_call: Callable[[dict[str, JsonValue]], Awaitable[object]],
    blocked_modules: list[str],
    session_id: JsonValue = None,
) -> Receipt:
    config: dict[str, JsonValue] = {
        "operation_id": receipt.operation_id,
        "key": digest((context.actor_id + ":" + context.chat_id).encode()),
        "head": head.operation_id if head else "",
        "phase": "prepare",
        "shell_digest": digest(shell_asset().encode()),
        "restore": [],
        "limits": limits.model_dump(),
    }
    raw, nonce = await browser_call(event_call, config, limits, session_id)
    prepared = workspace_reply(raw, receipt.operation_id, nonce, limits)
    if prepared.phase != "prepare":
        raise SaverError("Invalid browser prepare phase")
    restore: list[FileBytes] = []
    legacy: list[str] = []
    inherited: list[FileSlot] = []
    previous_inputs: list[FileSlot] = []
    if head:
        if head.checkpoint_version == 2:
            await verify_manifest(adapter, context, head, limits, read_blobs=False)
            inherited = [slot.model_copy(deep=True) for slot in head.blobs]
            legacy, previous_inputs = list(head.legacy_paths), head.inputs
        else:
            data = await adapter.verify_slot(context, head.checkpoint, limits.max_envelope_bytes)
            old = read_checkpoint(data, limits)
            if [item.manifest() for item in old] != head.files:
                raise SaverError("Legacy checkpoint manifest differs from saved receipt")
            legacy = [item.path for item in old]
            if {"work", "scripts", "inputs", "outputs"}.intersection(legacy):
                raise SaverError(
                    "Legacy checkpoint has a file at a newly reserved directory. "
                    "Its bytes and head are preserved; recover/rename it using the previous "
                    "compatible Tool before upgrading this workspace."
                )
            # Legacy checkpoints have no immutable per-file blobs; first v2 commit
            # reserves those bytes without modifying or deleting the old checkpoint.
            restore = old
    legacy_input_ids = {
        path.split("/")[1]
        for path in legacy
        if path.startswith("inputs/") and len(path.split("/")) > 2
    }
    active_attachments = [item for item in attachments if item.get("id") not in legacy_input_ids]
    inputs, fresh_inputs = await adapter.referenced_inputs(
        context, active_attachments, previous_inputs, limits
    )
    additions = JSON_VALUE.validate_python(
        {
            "phase": "execute",
            "warm": prepared.warm,
            "code": call.code,
            "language": call.language,
            "cwd": call.cwd,
            "restore": [],
            "continuation": False,
            "baseline": [item.model_dump() for item in receipt.baseline],
            "legacy_paths": legacy,
            "inputs": [item.model_dump() for item in inputs],
            "blocked_modules": blocked_modules,
            "shell_asset": shell_asset()
            if call.language == "bash" and not prepared.shell_ready
            else "",
        }
    )
    assert isinstance(additions, dict)
    config.update(additions)

    async def send_restore(file: FileBytes) -> None:
        config["phase"], config["restore"] = (
            "restore",
            JSON_VALUE.validate_python(encode_files([file])),
        )
        response, restore_nonce = await browser_call(event_call, config, limits, session_id)
        restored = workspace_reply(response, receipt.operation_id, restore_nonce, limits)
        if restored.phase != "restore":
            raise SaverError("Invalid restore acknowledgement")
        config["continuation"] = True
        config["shell_asset"] = ""  # Shell code is needed only in the final execution.

    asset = config["shell_asset"]
    config["shell_asset"] = ""
    if not prepared.warm:
        for slot in inherited:
            data = await adapter.verify_slot(context, slot, limits.max_file_bytes)
            await send_restore(FileBytes(slot.path, data))
        for file in restore:
            await send_restore(file)
    for slot in inputs:
        if not prepared.warm or slot.path in fresh_inputs:
            data = await adapter.read_file(context, slot.file_id, limits.max_file_bytes)
            if len(data) != slot.size or digest(data) != slot.sha256:
                raise SaverError("Native input no longer matches its saved reference")
            await send_restore(FileBytes(slot.path, data))
    config["phase"], config["restore"], config["shell_asset"] = "execute", [], asset
    raw, nonce = await browser_call(event_call, config, limits, session_id)
    reply = workspace_reply(raw, receipt.operation_id, nonce, limits)
    if reply.phase != "execute":
        raise SaverError("Invalid browser execution phase")
    changed = decode_files(reply.files, limits)
    changes = {item.path: item for item in changed}
    inherited_by_path = {slot.path: slot for slot in inherited}
    old_bytes = {item.path: item for item in restore if item.path in legacy}
    manifests = sorted(reply.manifest, key=lambda item: item.path)
    if len({item.path for item in manifests}) != len(manifests):
        raise SaverError("Duplicate retained manifest path")
    blobs: list[FileSlot] = []
    uploads: dict[str, bytes] = {}
    for item in manifests:
        safe_relative_path(item.path)
        if item.path in changes:
            file = changes[item.path]
            if file.manifest() != item:
                raise SaverError("Changed bytes differ from retained manifest")
            uploads[item.path] = file.data
            blobs.append(
                FileSlot(path=item.path, file_id=str(uuid4()), sha256=item.sha256, size=item.size)
            )
        elif item.path in inherited_by_path and slot_entry(inherited_by_path[item.path]) == item:
            blobs.append(inherited_by_path[item.path])
        elif item.path in old_bytes and old_bytes[item.path].manifest() == item:
            uploads[item.path] = old_bytes[item.path].data
            blobs.append(
                FileSlot(path=item.path, file_id=str(uuid4()), sha256=item.sha256, size=item.size)
            )
        else:
            raise SaverError("Retained manifest contains bytes not supplied or acknowledged")
    if set(changes) - {item.path for item in manifests}:
        raise SaverError("Callback supplied unreferenced retained bytes")
    # Validate paths/counts/aggregate against the final retained state, not just deltas.
    validate_manifest(
        WorkspaceManifest(
            files=[slot.model_copy(update={"committed": True}) for slot in blobs],
            inputs=inputs,
            legacy_paths=legacy,
        ),
        limits,
    )
    if (
        len(reply.stdout.encode()) > limits.max_text_bytes
        or len(reply.stderr.encode()) > limits.max_text_bytes
    ):
        raise SaverError("Browser stdout/stderr exceeds configured text limit")
    selected: set[str] = (
        issued_paths(manifests, receipt.baseline) if reply.outcome == "ok" else set()
    )
    async with adapter.state.transaction() as session:
        saved = await adapter.state.load(session, receipt.operation_id, context, lock=True)
        if saved.state != "allocated" or saved.execution_completed:
            raise SaverError("Operation already received execution data")
        saved.checkpoint_version, saved.blobs, saved.inputs = 2, blobs, inputs
        saved.legacy_paths, saved.files, saved.language = legacy, manifests, call.language
        saved.execution_completed, saved.python_ok = True, reply.outcome == "ok"
        saved.exit_code = reply.exit_code
        saved.stdout, saved.stderr = reply.stdout, reply.stderr
        saved.stdout_truncated, saved.stderr_truncated = (
            reply.stdout_truncated,
            reply.stderr_truncated,
        )
        saved.outputs = [slot.model_copy(deep=True) for slot in blobs if slot.path in selected]
        await adapter.state.put(session, saved)
    for path, data in uploads.items():
        await adapter.store_slot(context, receipt.operation_id, path, data, limits.max_file_bytes)
    saved = await adapter.state.get(context, receipt.operation_id)
    checkpoint = manifest_bytes(saved)
    if len(checkpoint) > limits.max_envelope_bytes:
        raise SaverError("Retained manifest exceeds transport limit")
    async with adapter.state.transaction() as session:
        saved = await adapter.state.load(session, receipt.operation_id, context, lock=True)
        saved.checkpoint.sha256, saved.checkpoint.size = digest(checkpoint), len(checkpoint)
        await adapter.state.put(session, saved)
    return await adapter.store_slot(
        context, receipt.operation_id, None, checkpoint, limits.max_envelope_bytes
    )
