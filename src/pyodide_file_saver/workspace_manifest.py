"""Version-two immutable blob references; database receipts remain authoritative."""

from pydantic import Field

from .contracts import (
    FileSlot,
    Limits,
    ManifestEntry,
    Receipt,
    SaverError,
    StrictModel,
    json_text,
    safe_relative_path,
)


class WorkspaceManifest(StrictModel):
    version: int = 2
    files: list[FileSlot] = Field(default_factory=list)
    inputs: list[FileSlot] = Field(default_factory=list)
    legacy_paths: list[str] = Field(default_factory=list)


def validate_manifest(manifest: WorkspaceManifest, limits: Limits) -> None:
    if manifest.version != 2 or len(manifest.files) > limits.max_files:
        raise SaverError("Invalid retained manifest version or file count")
    paths: set[str] = set()
    total = 0
    for slot in manifest.files:
        safe_relative_path(slot.path)
        if slot.path in paths or slot.size > limits.max_file_bytes or not slot.committed:
            raise SaverError("Invalid or uncommitted retained blob")
        if (
            not slot.path.startswith(("scripts/", "outputs/"))
            and slot.path not in manifest.legacy_paths
        ):
            raise SaverError("Transient paths cannot enter retained manifests")
        paths.add(slot.path)
        total += slot.size
    if total > limits.max_total_bytes:
        raise SaverError("Retained aggregate limit exceeded")
    if any(any(path.startswith(parent + "/") for parent in paths) for path in paths):
        raise SaverError("Retained manifest has conflicting paths")
    for slot in manifest.inputs:
        safe_relative_path(slot.path)
        if (
            not slot.path.startswith("inputs/")
            or not slot.committed
            or slot.size > limits.max_file_bytes
        ):
            raise SaverError("Invalid native input reference")


def manifest_bytes(receipt: Receipt) -> bytes:
    return json_text(
        WorkspaceManifest(
            files=receipt.blobs, inputs=receipt.inputs, legacy_paths=receipt.legacy_paths
        ).model_dump()
    ).encode()


def parse_manifest(data: bytes, receipt: Receipt, limits: Limits) -> WorkspaceManifest:
    value = WorkspaceManifest.model_validate_json(data)
    validate_manifest(value, limits)
    if (
        value.files != receipt.blobs
        or value.inputs != receipt.inputs
        or value.legacy_paths != receipt.legacy_paths
        or [slot_entry(slot) for slot in value.files] != receipt.files
    ):
        raise SaverError("Checkpoint references differ from their saved receipt")
    return value


def slot_entry(slot: FileSlot) -> ManifestEntry:
    return ManifestEntry(path=slot.path, sha256=slot.sha256, size=slot.size)


def retained_ready(receipt: Receipt) -> bool:
    """A partial native upload set must never look like an empty complete snapshot."""
    return (
        all(slot.committed for slot in receipt.blobs)
        and [slot_entry(slot) for slot in receipt.blobs] == receipt.files
        and (
            receipt.native_transfers is None
            or all(
                any(
                    slot_entry(slot)
                    == ManifestEntry(path=item.path, sha256=item.sha256, size=item.size)
                    for slot in receipt.blobs
                )
                for item in receipt.native_transfers
            )
        )
    )
