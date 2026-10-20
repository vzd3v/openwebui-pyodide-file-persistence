"""The only version-sensitive Open WebUI boundary. No imported host side effects at build time."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import io
import mimetypes
import time
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Literal, Protocol, cast
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import JSON, Connection, Integer, MetaData, String, Table, select, update
from sqlalchemy import inspect as inspect_sqlalchemy
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .contracts import (
    JSON_VALUE,
    BoundContext,
    FileSlot,
    Limits,
    Receipt,
    SaverError,
    digest,
    safe_filename,
)
from .workspace_state import WorkspaceState


class HostUser(Protocol):
    id: str
    role: str


class HostUsers(Protocol):
    async def get_user_by_id(self, id: str, db: AsyncSession | None = None) -> HostUser | None: ...


class HostChats(Protocol):
    async def get_accessible_chat_by_id(
        self,
        id: str,
        user: HostUser,
        db: AsyncSession | None = None,
        *,
        permission: Literal["read", "write"] = "read",
        chat: object = None,
    ) -> object: ...


class HostConfig(Protocol):
    async def get(self, key: str, default: object = None) -> object: ...


class HostStorage(Protocol):
    def get_file(self, file_path: str) -> str: ...
    def upload_file(
        self, file: BinaryIO, filename: str, tags: dict[str, str]
    ) -> tuple[bytes, str]: ...


class HostFileAccess(Protocol):
    async def __call__(
        self,
        file_id: str,
        access_type: str,
        user: HostUser,
        db: AsyncSession | None = None,
    ) -> bool: ...


def host_attribute(module: str, attribute: str) -> object:
    # All untyped host objects are narrowed here, not throughout orchestration.
    try:
        return cast(object, getattr(importlib.import_module(module), attribute))
    except (ImportError, AttributeError) as error:
        raise SaverError(f"Missing host capability: {module}.{attribute}") from error


def json_object(value: object) -> dict[str, JsonValue]:
    parsed = JSON_VALUE.validate_python(value)
    if not isinstance(parsed, dict):
        raise SaverError("Host returned an incompatible JSON object")
    return parsed


def require_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise SaverError("Required host identifier is missing")
    return value


class OpenWebUIAdapter:
    def __init__(self) -> None:
        engine = host_attribute("open_webui.internal.db", "async_engine")
        if not isinstance(engine, AsyncEngine):
            raise SaverError("Incompatible host async database engine")
        self.engine = engine
        base = host_attribute("open_webui.internal.db", "Base")
        metadata = cast(object, getattr(base, "metadata", None))
        if not isinstance(metadata, MetaData):
            raise SaverError("Incompatible host database metadata")
        schema = metadata.schema
        self.state = WorkspaceState(self.engine, schema)
        self.files = self._table(
            "open_webui.models.files",
            "File",
            {
                "id": "text",
                "user_id": "text",
                "path": "text",
                "hash": "text",
                "filename": "text",
                "data": "json",
                "meta": "json",
                "created_at": "integer",
                "updated_at": "integer",
            },
            insert=True,
        )
        self.chats = self._table(
            "open_webui.models.chats",
            "Chat",
            {"id": "text", "user_id": "text", "chat": "json", "updated_at": "integer"},
        )
        self.chat_files = self._table(
            "open_webui.models.chats",
            "ChatFile",
            {
                "id": "text",
                "user_id": "text",
                "chat_id": "text",
                "file_id": "text",
                "message_id": "text",
                "created_at": "integer",
                "updated_at": "integer",
            },
            insert=True,
        )
        self.messages = self._table(
            "open_webui.models.chat_messages",
            "ChatMessage",
            {
                "id": "text",
                "chat_id": "text",
                "user_id": "text",
                "role": "text",
                "files": "json",
                "updated_at": "integer",
            },
        )
        if any(
            table.metadata is not metadata
            for table in (self.files, self.chats, self.chat_files, self.messages)
        ):
            raise SaverError("Host models do not share database metadata")
        self.users_api = cast(HostUsers, host_attribute("open_webui.models.users", "Users"))
        self.chats_api = cast(HostChats, host_attribute("open_webui.models.chats", "Chats"))
        self.config = cast(HostConfig, host_attribute("open_webui.models.config", "Config"))
        self.storage = cast(HostStorage, host_attribute("open_webui.storage.provider", "Storage"))
        self.file_access = cast(
            HostFileAccess,
            host_attribute("open_webui.utils.access_control.files", "has_access_to_file"),
        )
        # Bind the calls we actually make, rather than assuming a matching name
        # or version proves argument/await compatibility. No host write canaries.
        self._call(self.users_api, "get_user_by_id", True, "id")
        self._call(
            self.chats_api,
            "get_accessible_chat_by_id",
            True,
            "id",
            object(),
            db=object(),
            permission="write",
            chat=object(),
        )
        self._call(self.config, "get", True, "key", None)
        self._call(self.config, "get", True, "key")
        self._call(self.file_access, "__call__", True, "id", "read", object(), db=object())
        self._call(self.storage, "get_file", False, "path")
        self._call(self.storage, "upload_file", False, io.BytesIO(), "name", {})

    @staticmethod
    def _call(
        owner: object, name: str, asynchronous: bool, *args: object, **kwargs: object
    ) -> None:
        function = owner if name == "__call__" else cast(object, getattr(owner, name, None))
        is_async = inspect.iscoroutinefunction(function) or inspect.iscoroutinefunction(
            inspect.getattr_static(type(function), "__call__", None)
        )
        if not callable(function) or is_async != asynchronous:
            raise SaverError(f"Incompatible host callable: {name}")
        try:
            inspect.signature(function).bind(*args, **kwargs)
        except (TypeError, ValueError) as error:
            raise SaverError(f"Incompatible host signature: {name}") from error

    @staticmethod
    def _table(module: str, name: str, required: dict[str, str], *, insert: bool = False) -> Table:
        table = cast(object, getattr(host_attribute(module, name), "__table__", None))
        if not isinstance(table, Table) or not set(required) <= set(table.columns.keys()):
            raise SaverError(f"Incompatible host table shape: {name}")
        categories = {"text": String, "integer": Integer, "json": JSON}
        for key, kind in required.items():
            if not isinstance(table.c[key].type, categories[kind]):
                raise SaverError(f"Incompatible host column: {name}.{key}")
        if [column.name for column in table.primary_key] != ["id"]:
            raise SaverError(f"Incompatible host primary key: {name}")
        if insert and any(
            column.name not in required
            and not column.nullable
            and column.default is None
            and column.server_default is None
            and column.computed is None
            and column.identity is None
            for column in table.columns
        ):
            raise SaverError(f"Unsupported required host insert field: {name}")
        return table

    async def initialize(self) -> None:
        # Check real deployed columns as well as the ORM declaration before DDL.
        async with self.engine.connect() as connection:

            def check(connection: Connection) -> None:
                inspector = inspect_sqlalchemy(connection)
                for table in (self.files, self.chats, self.chat_files, self.messages):
                    columns = {
                        column["name"]: column
                        for column in inspector.get_columns(table.name, schema=table.schema)
                    }
                    if not set(table.columns.keys()) <= columns.keys():
                        raise SaverError(f"Host database schema differs from ORM: {table.name}")
                    primary = inspector.get_pk_constraint(table.name, schema=table.schema)
                    if primary["constrained_columns"] != ["id"]:
                        raise SaverError(f"Host database primary key differs: {table.name}")
                    if table in (self.files, self.chat_files) and any(
                        name not in table.c
                        and not column["nullable"]
                        and column.get("default") is None
                        and not column.get("computed")
                        and not column.get("identity")
                        for name, column in columns.items()
                    ):
                        raise SaverError(
                            f"Unsupported required database insert field: {table.name}"
                        )
                    for column in table.columns:
                        # Compare dialect-independent affinities: PostgreSQL BIGINT
                        # and SQLite INTEGER both satisfy the integer contract.
                        actual_type = columns[column.name]["type"]
                        expected = column.type
                        for category in (String, Integer, JSON):
                            if isinstance(expected, category) and not isinstance(
                                actual_type, category
                            ):
                                raise SaverError(
                                    f"Host database column differs from ORM: "
                                    f"{table.name}.{column.name}"
                                )

            await connection.run_sync(check)
        await self.state.initialize()

    async def execution_policy(self) -> list[str]:
        if await self.config.get("code_interpreter.engine", "pyodide") != "pyodide":
            raise SaverError("Managed execution requires the host Pyodide engine")
        blocked = JSON_VALUE.validate_python(
            host_attribute("open_webui.config", "CODE_INTERPRETER_BLOCKED_MODULES")
        )
        if not isinstance(blocked, list) or not all(isinstance(item, str) for item in blocked):
            raise SaverError("Unsupported host blocked-module policy")
        return [item for item in blocked if isinstance(item, str)]

    async def bind(self, actor_id: str, chat_id: str, message_id: str) -> BoundContext:
        context = BoundContext(actor_id, chat_id, message_id)
        async with self.state.sessions() as session:
            await self.validate_context(session, context)
        return context

    async def validate_context(
        self, session: AsyncSession, context: BoundContext, *, lock: bool = False
    ) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        user = await self.users_api.get_user_by_id(context.actor_id)
        if user is None or user.id != context.actor_id or user.role not in {"user", "admin"}:
            raise SaverError("Authenticated actor is unavailable")
        query = select(self.chats).where(self.chats.c.id == context.chat_id)
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None:
            raise SaverError("Destination chat was deleted or is unavailable")
        host_chat = SimpleNamespace(**dict(row))
        accessible = await self.chats_api.get_accessible_chat_by_id(
            context.chat_id, user, db=session, permission="write", chat=host_chat
        )
        if not accessible:
            raise SaverError("Chat write permission is unavailable or revoked")
        if getattr(accessible, "id", None) != context.chat_id:
            raise SaverError("Incompatible host accessible-chat result")
        chat = json_object(row["chat"])
        history = json_object(chat.get("history"))
        messages = json_object(history.get("messages"))
        message = json_object(messages.get(context.message_id))
        if (
            message.get("role") != "assistant"
            or (message.get("user_id") or row["user_id"]) != context.actor_id
        ):
            raise SaverError("Destination is not this actor's assistant message")
        normalized = (
            (
                await session.execute(
                    select(self.messages).where(
                        self.messages.c.id == f"{context.chat_id}-{context.message_id}"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if normalized is None or (
            normalized["chat_id"] != context.chat_id
            or normalized["role"] != "assistant"
            or (normalized["user_id"] or row["user_id"]) != context.actor_id
        ):
            raise SaverError("Normalized assistant context is missing or inconsistent")
        return chat, message

    async def host_max_bytes(self, ceiling: int) -> int:
        value = await self.config.get("rag.file.max_size")
        if not value:
            return ceiling
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            raise SaverError("Unsupported host file-size configuration")
        try:
            maximum = int(value) * 1024 * 1024
        except (ValueError, OverflowError) as error:
            raise SaverError("Invalid host file-size configuration") from error
        if maximum < 0:
            raise SaverError("Invalid host file-size configuration")
        return min(ceiling, maximum)

    async def read_file(
        self, context: BoundContext, file_id: str, maximum: int, *, owned: bool = False
    ) -> bytes:
        async with self.state.sessions() as session:
            row = (
                (await session.execute(select(self.files).where(self.files.c.id == file_id)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise SaverError("Saved or attached File was deleted; it will not be recreated")
            if row["user_id"] != context.actor_id:
                user = await self.users_api.get_user_by_id(context.actor_id)
                if (
                    owned
                    or user is None
                    or not (
                        user.role == "admin"
                        or await self.file_access(file_id, "read", user, db=session) is True
                    )
                ):
                    raise SaverError("File is not readable by this actor")
            if await self._empty_file(session, row):
                return b""
            path = require_string(row["path"])
        try:
            provider_path = require_string(await asyncio.to_thread(self.storage.get_file, path))
            data = await asyncio.to_thread(self._read_bounded, provider_path, maximum)
        except (OSError, ValueError) as error:
            raise SaverError("Provider bytes are missing or unavailable") from error
        return data

    async def authorize_file(
        self, context: BoundContext, file_id: str, *, owned: bool = False
    ) -> tuple[str, str]:
        """Recheck host identity/grants without materializing provider bytes."""
        async with self.state.sessions() as session:
            row = (
                (await session.execute(select(self.files).where(self.files.c.id == file_id)))
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise SaverError("Saved or attached File was deleted; it will not be recreated")
            if row["user_id"] != context.actor_id:
                user = await self.users_api.get_user_by_id(context.actor_id)
                if (
                    owned
                    or user is None
                    or not (
                        user.role == "admin"
                        or await self.file_access(file_id, "read", user, db=session) is True
                    )
                ):
                    raise SaverError("File is not readable by this actor")
            path = "" if await self._empty_file(session, row) else require_string(row["path"])
            return require_string(row["filename"]), path

    async def _empty_file(self, session: AsyncSession, row: RowMapping) -> bool:
        """Recognize only an integration-created, receipt-backed exact empty File.

        Missing provider objects must never fall through to native text fallback.
        Native file ACLs are checked by the caller before reaching this method.
        """
        meta = json_object(row["meta"] or {})
        if meta.get("pyodide_file_saver_representation") != "empty":
            return False
        operation_id = require_string(meta.get("pyodide_file_saver_operation"))
        source = (
            (
                await session.execute(
                    select(self.state.operation).where(
                        self.state.operation.c.operation_id == operation_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise SaverError("Empty File origin receipt is unavailable")
        origin = await self.state.load(
            session,
            operation_id,
            BoundContext(
                require_string(source["actor_id"]),
                require_string(source["chat_id"]),
                require_string(source["message_id"]),
            ),
        )
        expected_hash = digest(b"")
        if (
            row["path"] != ""
            or row["user_id"] != origin.actor_id
            or row["hash"] != expected_hash
            or meta.get("size") != 0
            or meta.get("file_hash") != expected_hash
            or json_object(row["data"]).get("content") != ""
            or not any(
                slot.file_id == row["id"]
                and slot.committed
                and slot.representation == "empty"
                and slot.size == 0
                and slot.sha256 == expected_hash
                for slot in [*origin.blobs, *origin.outputs]
            )
        ):
            raise SaverError("Empty File differs from its acknowledged representation")
        return True

    async def referenced_inputs(
        self,
        context: BoundContext,
        attachments: list[dict[str, JsonValue]],
        previous: list[FileSlot],
        limits: Limits,
    ) -> tuple[list[FileSlot], list[str]]:
        result = [slot.model_copy(deep=True) for slot in previous]
        fresh: list[str] = []
        seen: set[str] = set()
        for slot in previous:
            await self.authorize_file(context, slot.file_id)
            seen.add(slot.file_id)
        if len(attachments) > limits.max_files:
            raise SaverError("Attached input count exceeds the configured limit")
        for item in attachments:
            if item.get("type") not in {None, "file"}:
                continue
            file_id = require_string(item.get("id"))
            if file_id in seen:
                continue
            seen.add(file_id)
            if len(file_id) > 128 or not all(char.isalnum() or char in "-_" for char in file_id):
                raise SaverError("Unsupported attached File identifier")
            name, provider_path = await self.authorize_file(context, file_id)
            # Skip this Tool's own issued attachments only after independent receipt proof.
            async with self.state.sessions() as session:
                raw = await session.scalar(
                    select(self.files.c.meta).where(self.files.c.id == file_id)
                )
            hint = json_object(raw or {}).get("pyodide_file_saver_operation")
            if isinstance(hint, str):
                try:
                    own = await self.state.get(context, hint)
                except SaverError:
                    own = None
                if own and any(slot.file_id == file_id for slot in own.outputs):
                    continue
            path = f"inputs/{file_id}/{safe_filename(name.replace(chr(92), '/').split('/')[-1])}"
            old = next(
                (slot for slot in previous if slot.file_id == file_id and slot.path == path), None
            )
            if old:
                result.append(old.model_copy(deep=True))
            else:
                data = await self.read_file(context, file_id, limits.max_file_bytes)
                fresh.append(path)
                result.append(
                    FileSlot(
                        path=path,
                        file_id=file_id,
                        sha256=digest(data),
                        size=len(data),
                        committed=True,
                        representation="empty" if provider_path == "" else "provider",
                    )
                )
        if len(result) > limits.max_files:
            raise SaverError("Referenced input count exceeds the configured limit")
        return result, fresh

    @staticmethod
    def _read_bounded(path: str, maximum: int) -> bytes:
        with Path(path).open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise SaverError("Provider file exceeds the configured byte limit")
        return data

    async def verify_slot(self, context: BoundContext, slot: FileSlot, maximum: int) -> bytes:
        if not slot.committed:
            raise SaverError("File has not been durably committed")
        data = await self.read_file(context, slot.file_id, maximum, owned=True)
        if len(data) != slot.size or digest(data) != slot.sha256:
            raise SaverError("Saved File bytes do not match their acknowledged receipt")
        return data

    @staticmethod
    def slot(receipt: Receipt, path: str | None) -> FileSlot:
        if path is None:
            return receipt.checkpoint
        for output in [*receipt.blobs, *receipt.outputs]:
            if output.path == path:
                return output
        raise SaverError("Output was not reserved by this operation")

    async def store_slot(
        self,
        context: BoundContext,
        operation_id: str,
        path: str | None,
        data: bytes,
        maximum: int,
    ) -> Receipt:
        receipt = await self.state.get(context, operation_id)
        slot = self.slot(receipt, path)
        if slot.committed:
            await self.verify_slot(context, slot, maximum)
            return receipt
        if digest(data) != slot.sha256 or len(data) != slot.size:
            raise SaverError("Bytes do not match the reserved File receipt")
        if len(data) > await self.host_max_bytes(maximum):
            raise SaverError("File exceeds the host upload-size policy")
        name = safe_filename(slot.path)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # A fresh object name for EVERY attempt is essential: concurrent retries
        # must never overwrite an object elected by another retry's File commit.
        object_name = f"{uuid4()}_{name}"
        empty = not data and path is not None
        provider_path = ""
        if (
            empty
            and getattr(
                importlib.import_module("open_webui.storage.provider"), "STORAGE_PROVIDER", None
            )
            != "local"
        ):
            raise SaverError(
                "Empty retained files require native local storage; remote or unknown "
                "providers cannot safely delete metadata-backed empty Files",
                "empty_files_require_local_storage",
            )
        if not empty:
            stored, provider_path = await asyncio.to_thread(
                self.storage.upload_file,
                io.BytesIO(data),
                object_name,
                {"OpenWebUI-User-Id": context.actor_id, "OpenWebUI-File-Id": slot.file_id},
            )
            if stored != data or not provider_path:
                raise SaverError("Storage provider did not confirm the intended bytes")
            provider_path = require_string(provider_path)
        async with self.state.transaction() as session:
            receipt = await self.state.load(session, operation_id, context, lock=True)
            elected = self.slot(receipt, path)
            if not elected.committed:
                existing = await session.scalar(
                    select(self.files.c.id).where(self.files.c.id == elected.file_id)
                )
                if existing is not None:
                    raise SaverError("Reserved File identifier collision; no file was overwritten")
                now = int(time.time())
                await session.execute(
                    self.files.insert().values(
                        id=elected.file_id,
                        user_id=context.actor_id,
                        hash=elected.sha256,
                        filename=name,
                        path=provider_path,
                        data={"content": ""} if empty else {},
                        meta={
                            "name": name,
                            "content_type": content_type,
                            "size": elected.size,
                            "file_hash": elected.sha256,
                            "pyodide_file_saver_operation": operation_id,
                            **({"pyodide_file_saver_representation": "empty"} if empty else {}),
                        },
                        created_at=now,
                        updated_at=now,
                    )
                )
                elected.committed = True
                elected.representation = "empty" if empty else "provider"
                for output in receipt.outputs:
                    if output.file_id == elected.file_id:
                        output.committed = True
                        output.representation = elected.representation
                if path is None:
                    if receipt.checkpoint_version == 2 and not all(
                        blob.committed for blob in receipt.blobs
                    ):
                        raise SaverError("Retained blobs must be committed before the manifest")
                    receipt.state = "saved"
                await self.state.put(session, receipt)
        # Lost attempts leave unacknowledged provider objects, never delete data
        # as compensation or substitute a different elected File identity.
        await self.verify_slot(context, self.slot(receipt, path), maximum)
        return receipt

    async def publish(self, context: BoundContext, operation_id: str) -> Receipt:
        initial = await self.state.get(context, operation_id)
        if initial.state not in {"accepted", "published"} or not initial.python_ok:
            return initial
        destination = BoundContext(context.actor_id, context.chat_id, initial.message_id)
        async with self.state.transaction() as session:
            # Match the host's chat-writer lock order, then serialize receipt writers.
            chat, message = await self.validate_context(session, destination, lock=True)
            receipt = await self.state.load(session, operation_id, context, lock=True)
            if not all(slot.committed for slot in receipt.outputs):
                raise SaverError("Not all issued Files are committed")
            existing_files = message.get("files") or []
            if not isinstance(existing_files, list):
                raise SaverError("Destination attachment state is incompatible")
            normalized_value = await session.scalar(
                select(self.messages.c.files).where(
                    self.messages.c.id == f"{context.chat_id}-{receipt.message_id}"
                )
            )
            normalized_files = JSON_VALUE.validate_python(normalized_value) or []
            if not isinstance(normalized_files, list):
                raise SaverError("Normalized attachment state is incompatible")
            files = list(existing_files)
            ids = {
                file_id
                for item in files
                if isinstance(item, dict) and isinstance(file_id := item.get("id"), str)
            }
            for item in normalized_files:
                file_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(file_id, str) and file_id not in ids:
                    files.append(item)
                    ids.add(file_id)
            now = int(time.time())
            for slot in receipt.outputs:
                file_row = (
                    (
                        await session.execute(
                            select(self.files)
                            .where(self.files.c.id == slot.file_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if file_row is None or file_row["user_id"] != context.actor_id:
                    raise SaverError(
                        "Issued File is deleted or unavailable; it will not be recreated"
                    )
                if slot.file_id not in ids:
                    name = safe_filename(slot.path)
                    files.append(
                        {
                            "type": "file",
                            "id": slot.file_id,
                            "name": name,
                            "url": f"/api/v1/files/{slot.file_id}",
                            "size": slot.size,
                            "status": "uploaded",
                        }
                    )
                    ids.add(slot.file_id)
                linked = await session.scalar(
                    select(self.chat_files.c.id).where(
                        self.chat_files.c.chat_id == context.chat_id,
                        self.chat_files.c.file_id == slot.file_id,
                    )
                )
                if linked is None:
                    await session.execute(
                        self.chat_files.insert().values(
                            id=str(uuid4()),
                            user_id=context.actor_id,
                            chat_id=context.chat_id,
                            message_id=receipt.message_id,
                            file_id=slot.file_id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            message["files"] = files
            history = json_object(chat["history"])
            messages = json_object(history["messages"])
            messages[receipt.message_id] = message
            history["messages"] = messages
            chat["history"] = history
            await session.execute(
                update(self.chats)
                .where(self.chats.c.id == context.chat_id)
                .values(chat=chat, updated_at=now)
            )
            changed = await session.execute(
                update(self.messages)
                .where(
                    self.messages.c.id == f"{context.chat_id}-{receipt.message_id}",
                    self.messages.c.chat_id == context.chat_id,
                    self.messages.c.role == "assistant",
                )
                .values(files=files, updated_at=now)
            )
            if not isinstance(changed, CursorResult) or changed.rowcount != 1:
                raise SaverError("Normalized assistant disappeared during publication")
            receipt.state = "published"
            await self.state.put(session, receipt)
        return receipt

    async def verify_publication(
        self, context: BoundContext, receipt: Receipt, limits: Limits
    ) -> None:
        destination = BoundContext(context.actor_id, context.chat_id, receipt.message_id)
        for slot in receipt.outputs:
            await self.verify_slot(context, slot, limits.max_file_bytes)
        async with self.state.sessions() as session:
            _, message = await self.validate_context(session, destination)
            normalized_files = await session.scalar(
                select(self.messages.c.files).where(
                    self.messages.c.id == f"{context.chat_id}-{receipt.message_id}"
                )
            )
            normalized = JSON_VALUE.validate_python(normalized_files)
            for collection in (message.get("files"), normalized):
                if not isinstance(collection, list):
                    raise SaverError("Persisted attachment state is missing")
                ids = [item.get("id") for item in collection if isinstance(item, dict)]
                if any(ids.count(slot.file_id) != 1 for slot in receipt.outputs):
                    raise SaverError("Persisted attachments do not match the publication receipt")
            for slot in receipt.outputs:
                linked = await session.scalar(
                    select(self.chat_files.c.id).where(
                        self.chat_files.c.chat_id == context.chat_id,
                        self.chat_files.c.file_id == slot.file_id,
                    )
                )
                if linked is None:
                    raise SaverError("Persisted shared-chat File association is missing")
