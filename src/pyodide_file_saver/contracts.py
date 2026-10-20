"""Validated transfer and durable-state contracts; browser data is never authority."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

HOST_SOCKET_LIMIT = 16 * 1024 * 1024
FILENAME_BYTES = 180
JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
SCHEMA_VERSION = 1


class SaverError(Exception):
    """Safe, content-free failure suitable for a Tool result."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class Limits(StrictModel):
    max_files: int = Field(
        default=64,
        ge=1,
        le=256,
        description=(
            "Maximum retained regular files in the managed workspace "
            "(scratch work/ is excluded); count, not bytes."
        ),
    )
    max_file_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
        lt=HOST_SOCKET_LIMIT,
        description="Maximum decoded bytes per managed file; host upload policy may lower it.",
    )
    max_total_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
        lt=HOST_SOCKET_LIMIT,
        description=(
            "Maximum decoded bytes across retained files, "
            "excluding transient work/ and referenced native inputs."
        ),
    )
    max_envelope_bytes: int = Field(
        default=15 * 1024 * 1024,
        ge=1024,
        lt=HOST_SOCKET_LIMIT,
        description=(
            "Maximum serialized UTF-8 bytes per request, callback or checkpoint, "
            "including JSON/base64 overhead; strictly below the stock 16 MiB "
            "transport ceiling."
        ),
    )
    max_text_bytes: int = Field(
        default=16 * 1024,
        ge=1,
        le=64 * 1024,
        description=(
            "Maximum captured UTF-8 bytes per stdout/stderr stream; excess text is truncated."
        ),
    )
    callback_timeout_seconds: int = Field(
        default=55,
        ge=1,
        lt=60,
        description=(
            "Seconds waiting for a browser callback; strictly below the stock "
            "60-second frontend deadline. Timeout does not prove worker termination."
        ),
    )


class Action(StrictModel):
    action: Literal["execute", "retry", "status"] = "execute"
    code: str = ""
    language: Literal["python", "bash"] = "python"
    cwd: str = "work"
    operation_id: str = ""

    @model_validator(mode="after")
    def validate_action(self) -> Action:
        if self.action == "execute":
            if not self.code.strip() or self.operation_id:
                raise ValueError("execute requires code and no operation_id")
        elif self.code or (self.action == "retry" and not self.operation_id):
            raise ValueError("retry requires operation_id and no code; status accepts no code")
        if self.operation_id and not re.fullmatch(r"[0-9a-f-]{36}", self.operation_id):
            raise ValueError("invalid operation_id")
        if self.cwd != ".":
            safe_relative_path(self.cwd)
        return self


class TransferFile(StrictModel):
    path: str
    data: str


class ManifestEntry(StrictModel):
    path: str
    sha256: str
    size: int = Field(ge=0)


class FileSlot(StrictModel):
    path: str
    file_id: str
    sha256: str
    size: int = Field(ge=0)
    committed: bool = False


class Receipt(StrictModel):
    schema_version: Literal[1] = 1
    operation_id: str
    actor_id: str
    chat_id: str
    message_id: str
    base_revision: int
    state: Literal["allocated", "saved", "accepted", "conflict", "published"] = "allocated"
    checkpoint: FileSlot
    files: list[ManifestEntry] = Field(default_factory=list)
    baseline: list[ManifestEntry] = Field(default_factory=list)
    outputs: list[FileSlot] = Field(default_factory=list)
    execution_completed: bool = False
    python_ok: bool = False
    stdout: str = ""
    stderr: str = ""
    checkpoint_version: Literal[1, 2] = 1
    blobs: list[FileSlot] = Field(default_factory=list)
    inputs: list[FileSlot] = Field(default_factory=list)
    legacy_paths: list[str] = Field(default_factory=list)
    language: Literal["python", "bash"] = "python"
    exit_code: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True, slots=True)
class BoundContext:
    actor_id: str
    chat_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class FileBytes:
    path: str
    data: bytes

    def manifest(self) -> ManifestEntry:
        return ManifestEntry(path=self.path, sha256=digest(self.data), size=len(self.data))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value.encode("utf-8")) > 1024
        or path.is_absolute()
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or str(path) != value
    ):
        raise SaverError("Invalid managed relative path")
    return value


def safe_filename(value: str) -> str:
    name = PurePosixPath(safe_relative_path(value)).name
    if len(name.encode("utf-8")) <= FILENAME_BYTES:
        return name
    # Preserve ordinary extensions (including compound document/archive suffixes)
    # and distinguish long names sharing a prefix without changing short names.
    suffix = "".join(PurePosixPath(name).suffixes)
    if len(suffix.encode("utf-8")) > FILENAME_BYTES // 2:
        suffix = PurePosixPath(name).suffix
    if len(suffix.encode("utf-8")) > FILENAME_BYTES // 2:
        suffix = ""  # An arbitrarily long dotted tail is not a useful extension.
    marker = "-" + digest(name.encode("utf-8"))[:16]
    budget = FILENAME_BYTES - len((marker + suffix).encode("utf-8"))
    stem = name[: -len(suffix)] if suffix else name
    return stem.encode("utf-8")[:budget].decode("utf-8", "ignore") + marker + suffix


def validate_files(files: list[FileBytes], limits: Limits) -> None:
    if len(files) > limits.max_files:
        raise SaverError("Managed file count limit exceeded")
    seen: set[str] = set()
    total = 0
    for file in files:
        safe_relative_path(file.path)
        if file.path == "outputs":
            raise SaverError("outputs is a reserved directory, not a regular file")
        if file.path in seen or len(file.data) > limits.max_file_bytes:
            raise SaverError("Duplicate managed path or per-file size limit exceeded")
        seen.add(file.path)
        total += len(file.data)
    if total > limits.max_total_bytes:
        raise SaverError("Managed aggregate size limit exceeded")
    if any(
        str(parent) in seen
        for path in seen
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    ):
        raise SaverError("Managed files contain an impossible file/directory conflict")


def encode_files(files: list[FileBytes]) -> list[dict[str, str]]:
    return [{"path": f.path, "data": base64.b64encode(f.data).decode("ascii")} for f in files]


def decode_files(files: list[TransferFile], limits: Limits) -> list[FileBytes]:
    if len(files) > limits.max_files:
        raise SaverError("Managed file count limit exceeded")
    decoded: list[FileBytes] = []
    for file in files:
        if len(file.data) > 4 * ((limits.max_file_bytes + 2) // 3):
            raise SaverError("Encoded per-file size limit exceeded")
        try:
            data = base64.b64decode(file.data, validate=True)
        except (ValueError, binascii.Error) as error:
            raise SaverError("Invalid base64 transfer") from error
        decoded.append(FileBytes(safe_relative_path(file.path), data))
    validate_files(decoded, limits)
    return decoded


def issued_paths(current: list[ManifestEntry], baseline: list[ManifestEntry]) -> set[str]:
    previous = {entry.path: entry.sha256 for entry in baseline}
    return {
        entry.path
        for entry in current
        if entry.path.startswith("outputs/") and previous.get(entry.path) != entry.sha256
    }


def markdown_link(name: str, file_id: str) -> str:
    label = re.sub(r"([\\`*_{}\[\]()<>!|])", r"\\\1", name)
    return f"[{label}](/api/v1/files/{file_id}/content?attachment=true)"
