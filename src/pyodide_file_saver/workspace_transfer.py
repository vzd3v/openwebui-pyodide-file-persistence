"""Durable native-HTTP reservations and source-free, sequential file adoption."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from .browser_workspace import WorkspaceReply
from .contracts import (
    BoundContext,
    FileSlot,
    Limits,
    ManifestEntry,
    NativeTransfer,
    Receipt,
    SaverError,
    digest,
    issued_paths,
    transfer_deadline,
)
from .openwebui_adapter import OpenWebUIAdapter
from .workspace_manifest import (
    WorkspaceManifest,
    manifest_bytes,
    retained_ready,
    slot_entry,
    validate_manifest,
)


async def stage_native_snapshot(
    adapter: OpenWebUIAdapter,
    context: BoundContext,
    operation_id: str,
    manifests: list[ManifestEntry],
    inherited: list[FileSlot],
    inputs: list[FileSlot],
    legacy: list[str],
    limits: Limits,
) -> Receipt:
    manifests = sorted(manifests, key=lambda item: item.path)
    old = {slot.path: slot for slot in inherited}
    host_maximum = await adapter.host_max_bytes(limits.max_file_bytes)
    # Validate the *complete* snapshot independently of which blobs already exist.
    validate_manifest(
        WorkspaceManifest(
            files=[
                FileSlot(
                    path=item.path,
                    file_id="pending",
                    sha256=item.sha256,
                    size=item.size,
                    committed=True,
                )
                for item in manifests
            ],
            inputs=inputs,
            legacy_paths=legacy,
        ),
        limits,
    )
    async with adapter.state.transaction() as session:
        saved = await adapter.state.load(session, operation_id, context, lock=True)
        if saved.native_transfers is not None:
            if saved.files != manifests or saved.inputs != inputs or saved.legacy_paths != legacy:
                raise SaverError(
                    "Pending snapshot differs from its frozen reservation",
                    "native_transfer_changed",
                )
            return saved
        if saved.state != "allocated" or saved.blobs or not saved.execution_completed:
            raise SaverError(
                "Operation cannot receive a new native snapshot", "native_transfer_changed"
            )
        blobs: list[FileSlot] = []
        transfers: list[NativeTransfer] = []
        for item in manifests:
            if (
                item.path not in old or slot_entry(old[item.path]) != item
            ) and item.size > host_maximum:
                raise SaverError(
                    "Retained file exceeds the native upload-size policy",
                    "retained_file_too_large",
                    details={"limit_bytes": host_maximum, "size_bytes": item.size},
                )
            if item.path in old and slot_entry(old[item.path]) == item:
                blobs.append(old[item.path].model_copy(deep=True))
            elif item.size == 0:
                blobs.append(
                    FileSlot(
                        path=item.path,
                        file_id=str(uuid4()),
                        sha256=item.sha256,
                        size=0,
                        representation="empty",
                    )
                )
            else:
                transfers.append(
                    NativeTransfer(
                        path=item.path,
                        transfer_key=str(uuid4()),
                        sha256=item.sha256,
                        size=item.size,
                    )
                )
        saved.checkpoint_version = 2
        saved.files, saved.blobs, saved.native_transfers = manifests, blobs, transfers
        saved.inputs, saved.legacy_paths = inputs, legacy
        selected: set[str] = issued_paths(manifests, saved.baseline) if saved.python_ok else set()
        saved.outputs = [slot.model_copy(deep=True) for slot in blobs if slot.path in selected]
        await adapter.state.put(session, saved)
        return saved


async def reconcile_native_uploads(
    adapter: OpenWebUIAdapter,
    context: BoundContext,
    receipt: Receipt,
    limits: Limits,
) -> Receipt:
    """Retry can adopt uploads even when neither HTTP nor callback returned an ID."""
    async with transfer_deadline(limits.transfer_timeout_seconds, "storage"):
        for slot in receipt.blobs:
            if not slot.committed:
                if slot.size != 0:
                    raise SaverError("Invalid pending native File slot")
                receipt = await adapter.store_slot(
                    context, receipt.operation_id, slot.path, b"", limits.max_file_bytes
                )
        for transfer in receipt.native_transfers or []:
            if any(slot.path == transfer.path for slot in receipt.blobs):
                continue
            candidate = await adapter.find_native_upload(context, receipt.operation_id, transfer)
            if candidate is not None:
                receipt = await adapter.adopt_native_upload(
                    context, receipt.operation_id, transfer.path, candidate, limits.max_file_bytes
                )
    return receipt


async def save_native_snapshot(
    adapter: OpenWebUIAdapter,
    context: BoundContext,
    receipt: Receipt,
    limits: Limits,
    upload: Callable[[NativeTransfer, float], Awaitable[WorkspaceReply]],
) -> Receipt:
    async with transfer_deadline(limits.transfer_timeout_seconds, "storage") as deadline:
        receipt = await reconcile_native_uploads(
            adapter, context, await adapter.state.get(context, receipt.operation_id), limits
        )
        for transfer in receipt.native_transfers or []:
            if any(slot.path == transfer.path for slot in receipt.blobs):
                continue
            # A failed HTTP response does not imply failed upload. Lookup first on
            # subsequent retry/recover; a dispatched upload callback is never replayed.
            until = deadline.when()
            assert until is not None
            remaining = max(0.001, until - asyncio.get_running_loop().time())
            reply = await upload(transfer, remaining)
            receipt = await adapter.adopt_native_upload(
                context,
                receipt.operation_id,
                transfer.path,
                reply.uploaded_file_id,
                limits.max_file_bytes,
            )
        if not retained_ready(receipt):
            raise SaverError("Native snapshot still has uncommitted files", "native_upload_missing")
        checkpoint = manifest_bytes(receipt)
        if len(checkpoint) > limits.max_envelope_bytes:
            raise SaverError("Retained manifest exceeds transport limit")
        async with adapter.state.transaction() as session:
            saved = await adapter.state.load(session, receipt.operation_id, context, lock=True)
            if not retained_ready(saved):
                raise SaverError("Native snapshot changed before checkpoint commit")
            saved.checkpoint.sha256, saved.checkpoint.size = digest(checkpoint), len(checkpoint)
            await adapter.state.put(session, saved)
        return await adapter.store_slot(
            context, receipt.operation_id, None, checkpoint, limits.max_envelope_bytes
        )
