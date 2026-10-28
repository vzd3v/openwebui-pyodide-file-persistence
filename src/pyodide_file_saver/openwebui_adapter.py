"""The only version-sensitive Open WebUI boundary. No imported host side effects at build time."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import io
import mimetypes
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import JSON, Connection, Integer, MetaData, String, Table, select, update
from sqlalchemy import inspect as inspect_sqlalchemy
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .browser_workspace import workspace_reply, workspace_wrapper
from .contracts import (
    JSON_VALUE,
    BoundContext,
    BrowserFailure,
    FileSlot,
    Limits,
    OperationDiagnostics,
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


class HostMessageWriter(Protocol):
    async def upsert_message(
        self,
        message_id: str,
        chat_id: str,
        user_id: str,
        data: dict[str, JsonValue],
        db: AsyncSession | None = None,
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


EventCall = Callable[[dict[str, JsonValue]], Awaitable[object]]


class HostEventFactory(Protocol):
    async def __call__(self, request_info: dict[str, JsonValue]) -> EventCall | None: ...


class HostSessionPool(Protocol):
    def get(self, key: str) -> dict[str, object] | None: ...


BROWSER_POLL_SECONDS = 1
BROWSER_PROBE_SECONDS = 5
BROWSER_PROBE_CANDIDATES = 8
BROWSER_DRAIN_SECONDS = 0.1
BROWSER_MAX_PENDING_CALLBACKS = 64
_pending_callbacks: set[asyncio.Future[object]] = set()


def consume_callback(task: asyncio.Future[object]) -> None:
    _pending_callbacks.discard(task)
    if not task.cancelled():
        task.exception()


class NativeBrowserConnection:
    """Rebind control calls only after recognizing the original live runtime.

    A runtime identifier is continuity evidence inside one authenticated user's
    interpreter, not an attestation or an authorization credential.
    """

    def __init__(
        self,
        adapter: OpenWebUIAdapter,
        context: BoundContext,
        metadata: dict[str, JsonValue],
        event_call: EventCall,
        limits: Limits,
    ) -> None:
        self.adapter, self.context, self.metadata = adapter, context, metadata
        self.event_call, self.limits = event_call, limits
        session = metadata.get("session_id")
        self.session_id = session if isinstance(session, str) and 0 < len(session) <= 256 else ""
        self.runtime_id = ""
        self.reconnected = False
        self.session_timeout = 0.0
        self.pool: HostSessionPool | None = None
        self.batches: Callable[[], Iterable[Iterable[tuple[str, dict[str, object]]]]] | None = None
        self.factory: HostEventFactory | None = None
        # Optional, version-sensitive recovery. Other hosts keep the injected RPC.
        if self.session_id and adapter.task_id(metadata):
            try:
                if host_attribute("open_webui.env", "VERSION") != "0.12.3":
                    return
                pool = host_attribute("open_webui.socket.main", "SESSION_POOL")
                batches = host_attribute("open_webui.socket.main", "get_session_pool_batches")
                factory = host_attribute("open_webui.socket.main", "get_event_call")
                timeout = host_attribute("open_webui.socket.main", "SESSION_POOL_TIMEOUT")
                if not isinstance(timeout, (int, float)) or timeout <= 0:
                    return
                adapter.require_callable(pool, "get", False, "session")
                adapter.require_callable(batches, "__call__", False)
                adapter.require_callable(factory, "__call__", True, {})
            except SaverError:
                return
            self.pool = cast(HostSessionPool, pool)
            self.batches = cast(
                Callable[[], Iterable[Iterable[tuple[str, dict[str, object]]]]], batches
            )
            self.factory = cast(HostEventFactory, factory)
            self.session_timeout = float(timeout)

    def live(self, session_id: str, *, chat: bool = False) -> bool:
        session = self.pool.get(session_id) if self.pool else None
        seen = session.get("last_seen_at") if session else None
        chats = session.get("chat_ids") if session else None
        return bool(
            session
            and session.get("id") == self.context.actor_id
            and isinstance(seen, (int, float))
            and time.time() - seen <= self.session_timeout
            and (not chat or isinstance(chats, list) and self.context.chat_id in chats)
        )

    @staticmethod
    def session_key(session_id: str) -> str:
        return digest(session_id.encode())

    async def observe(self, runtime_id: str) -> None:
        if not runtime_id or not self.session_id:
            return
        try:
            if str(UUID(runtime_id)) != runtime_id:
                raise ValueError
        except ValueError as error:
            raise SaverError("Browser runtime identifier is invalid") from error
        if self.reconnected and self.runtime_id != runtime_id:
            raise BrowserFailure(
                "Browser runtime changed during reconnect", "browser_runtime_changed", "prepare"
            )
        self.runtime_id = runtime_id
        await self.adapter.state.bind_browser(
            self.context, self.session_key(self.session_id), runtime_id
        )

    async def invoke(
        self, callback: EventCall, payload: dict[str, JsonValue], sid: str, phase: str
    ) -> object:
        if not self.live(sid):
            raise BrowserFailure("Browser session is unavailable", "session_disconnected", phase)
        if len(_pending_callbacks) >= BROWSER_MAX_PENDING_CALLBACKS:
            raise BrowserFailure(
                "Browser callback cleanup is saturated", "browser_callback_timeout", phase
            )
        pending = asyncio.ensure_future(callback(payload))
        _pending_callbacks.add(pending)
        pending.add_done_callback(consume_callback)
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=BROWSER_POLL_SECONDS)
                if done:
                    return pending.result()
                if not self.live(sid):
                    raise BrowserFailure(
                        "Browser session is unavailable", "session_disconnected", phase
                    )
        finally:
            # A host callback may delay cancellation while draining a socket write.
            # Its cleanup cannot consume the entire recovery budget or block Stop.
            if not pending.done():
                pending.cancel()
                await asyncio.wait({pending}, timeout=BROWSER_DRAIN_SECONDS)

    def probe_request(self, sid: str) -> tuple[dict[str, JsonValue], str, str]:
        operation_id = str(uuid4())
        config: dict[str, JsonValue] = {
            "operation_id": operation_id,
            "key": digest((self.context.actor_id + ":" + self.context.chat_id).encode()),
            "head": "",
            "phase": "probe",
            "restore": [],
            "limits": self.limits.model_dump(),
        }
        wrapper, nonce = workspace_wrapper(config, "")
        return (
            {
                "type": "execute:python",
                "data": {"id": nonce, "code": wrapper, "files": [], "session_id": sid},
            },
            operation_id,
            nonce,
        )

    async def candidates(self, tested: set[str]) -> tuple[list[str], set[str]]:
        if self.batches is None:
            return [], set()
        sessions = {
            sid: session
            for batch in self.batches()
            for sid, session in batch
            if sid not in tested and self.live(sid, chat=True)
        }
        known = await self.adapter.state.browser_sessions(
            self.context, self.runtime_id, [self.session_key(sid) for sid in sessions]
        )
        ranked = sorted(
            sessions,
            key=lambda sid: (
                self.session_key(sid) in known,
                cast(float, sessions[sid]["last_seen_at"]),
            ),
            reverse=True,
        )
        return ranked[: max(0, BROWSER_PROBE_CANDIDATES - len(tested))], known

    async def bind_route(self, sid: str) -> bool:
        if self.factory is None:
            return False
        await self.adapter.validate_generation(self.context, self.metadata, require_task=True)
        callback = await self.factory(
            {
                "session_id": sid,
                "user_id": self.context.actor_id,
                "chat_id": self.context.chat_id,
                "message_id": self.context.message_id,
            }
        )
        if callback is None:
            return False
        probe, operation_id, nonce = self.probe_request(sid)
        try:
            async with asyncio.timeout(
                min(BROWSER_PROBE_SECONDS, self.limits.callback_timeout_seconds / 3)
            ):
                raw = await self.invoke(callback, probe, sid, "probe")
            reply = workspace_reply(raw, operation_id, nonce, self.limits)
        except (TimeoutError, SaverError):
            return False
        if reply.phase != "probe" or reply.runtime_id != self.runtime_id:
            return False
        await self.adapter.validate_generation(self.context, self.metadata, require_task=True)
        self.session_id, self.event_call = sid, callback
        self.reconnected = True
        await self.observe(reply.runtime_id)
        return True

    async def call(self, payload: dict[str, JsonValue], phase: str) -> object:
        if self.pool is None or self.batches is None or self.factory is None:
            return await self.event_call(payload)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SaverError("Invalid native browser request")
        if phase != "prepare":
            data["session_id"] = self.session_id
            raw = await self.invoke(self.event_call, payload, self.session_id, phase)
            OpenWebUIAdapter.classify_callback(raw, phase)
            return raw
        if not self.runtime_id:
            self.runtime_id = await self.adapter.state.browser_runtime(
                self.context, self.session_key(self.session_id)
            )
        tested: set[str] = set()
        if self.runtime_id:
            # New Tool calls inherit the request's original SID. Prefer a replacement
            # recognized by an earlier call, but never trust the saved mapping alone.
            candidates, known = await self.candidates({self.session_id})
            for sid in candidates:
                if self.session_key(sid) in known:
                    tested.add(sid)
                    if await self.bind_route(sid):
                        break
        data["session_id"] = self.session_id
        try:
            # Cold startup needs the full deadline. A known interpreter's preparation
            # must leave time for discovery even while its obsolete SID appears live.
            timeout = (
                min(BROWSER_PROBE_SECONDS, self.limits.callback_timeout_seconds / 3)
                if self.runtime_id
                else None
            )
            async with asyncio.timeout(timeout):
                raw = await self.invoke(self.event_call, payload, self.session_id, phase)
            OpenWebUIAdapter.classify_callback(raw, phase)
            return raw
        except (TimeoutError, BrowserFailure):
            if not self.runtime_id:
                raise
        tested.add(self.session_id)
        while True:
            await self.adapter.validate_generation(self.context, self.metadata, require_task=True)
            candidates, _ = await self.candidates(tested)
            for sid in candidates:
                tested.add(sid)
                if await self.bind_route(sid):
                    data["session_id"] = sid
                    raw = await self.invoke(self.event_call, payload, sid, phase)
                    OpenWebUIAdapter.classify_callback(raw, phase)
                    return raw
            await asyncio.sleep(BROWSER_POLL_SECONDS)

    async def ensure_route(self) -> None:
        """Reconnect before mutation; never replay an already-sent mutable callback."""
        if self.pool is None:
            return
        await self.adapter.validate_generation(self.context, self.metadata, require_task=True)
        if self.live(self.session_id):
            return
        payload, operation_id, nonce = self.probe_request(self.session_id)
        try:
            async with asyncio.timeout(self.limits.callback_timeout_seconds):
                raw = await self.call(payload, "prepare")
        except TimeoutError as error:
            raise BrowserFailure(
                "Browser reconnect timed out before program dispatch",
                "browser_callback_timeout",
                "prepare",
            ) from error
        reply = workspace_reply(raw, operation_id, nonce, self.limits)
        if reply.phase != "probe":
            raise SaverError("Invalid browser continuity reply")
        await self.observe(reply.runtime_id)


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
    @staticmethod
    def task_id(metadata: dict[str, JsonValue]) -> str:
        value = metadata.get("task_id")
        if not isinstance(value, str):
            return ""
        try:
            return value if str(UUID(value)) == value else ""
        except ValueError:
            return ""

    @staticmethod
    def invocation_diagnostics(
        context: BoundContext, metadata: dict[str, JsonValue]
    ) -> OperationDiagnostics:
        # This input is injected by the host, never a model-facing Tool argument.
        task = OpenWebUIAdapter.task_id(metadata)
        session = metadata.get("session_id")
        session = session if isinstance(session, str) and 0 < len(session) <= 256 else ""
        request = ":".join((context.chat_id, context.message_id, task))
        return OperationDiagnostics(
            request_key="request:" + digest(request.encode()),
            session_key="session:" + digest(session.encode()) if session else "",
            request_scope="task" if task else "message",
        )

    @staticmethod
    def classify_callback(raw: object, phase: str) -> None:
        """Normalize known failure envelopes without attesting browser truth."""
        reason = getattr(raw, "reason", None)
        if isinstance(reason, str) and reason in {
            "session_unavailable",
            "session_mismatch",
            "session_disconnected",
            "event_timeout",
        }:
            raise BrowserFailure("Browser session is unavailable", reason, phase)
        parsed = JSON_VALUE.validate_python(raw)
        # Stock socket lifecycle failures have no workspace result. Match only the
        # known host text, never relay arbitrary error text to the model/logs.
        if isinstance(parsed, dict) and not isinstance(parsed.get("result"), str):
            if parsed.get("error") == "Client session disconnected.":
                raise BrowserFailure(
                    "Browser session is unavailable", "session_disconnected", phase
                )
            if parsed.get("error") == (
                "Event call timed out. The browser tab may be inactive or closed."
            ):
                raise BrowserFailure(
                    "Browser callback timed out; worker termination is not confirmed",
                    "browser_callback_timeout",
                    phase,
                )

    def require_stop_capability(self) -> None:
        if host_attribute("open_webui.env", "VERSION") != "0.12.3":
            raise SaverError(
                "Native cancellation is qualified only for Open WebUI 0.12.3; "
                "disable stop_on_browser_failure until this host is qualified"
            )
        self.require_callable(
            host_attribute("open_webui.models.chat_messages", "ChatMessages"),
            "upsert_message",
            True,
            "message",
            "chat",
            "actor",
            {},
            db=object(),
        )

    @staticmethod
    def _validate_task(
        message: dict[str, JsonValue], metadata: dict[str, JsonValue], *, require_task: bool = False
    ) -> None:
        meta = message.get("meta")
        stored_task = meta.get("task_id") if isinstance(meta, dict) else None
        injected_task = OpenWebUIAdapter.task_id(metadata)
        if require_task and (not injected_task or not stored_task):
            raise SaverError("Native cancellation requires a bound server task generation")
        if stored_task and stored_task != injected_task:
            raise SaverError("Injected generation differs from the live assistant message")

    async def validate_generation(
        self, context: BoundContext, metadata: dict[str, JsonValue], *, require_task: bool = False
    ) -> None:
        async with self.state.sessions() as session:
            _, message = await self.validate_context(session, context)
        self._validate_task(message, metadata, require_task=require_task)

    async def persist_stop(
        self,
        context: BoundContext,
        metadata: dict[str, JsonValue],
        diagnostic: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        receipt = diagnostic.get("receipt")
        not_started = (
            isinstance(receipt, dict) and receipt.get("execution_outcome") == "not_started"
        )
        error: dict[str, JsonValue] = {
            "content": (
                "The program did not start. Reconnect the browser and send a new message "
                "to continue from the last saved workspace."
                if not_started
                else "Browser execution is unavailable. Inspect operation status before continuing."
            ),
            "type": "workspace_executor_unavailable",
            "diagnostic": diagnostic,
        }
        async with self.state.transaction() as session:
            # Same Chat row lock as the inspected native generation/merge path.
            # Recheck the generation inside it; an earlier check is only admission.
            chat, message = await self.validate_context(session, context, lock=True)
            self._validate_task(message, metadata, require_task=True)
            writer = cast(
                HostMessageWriter, host_attribute("open_webui.models.chat_messages", "ChatMessages")
            )
            saved = await writer.upsert_message(
                context.message_id, context.chat_id, context.actor_id, {"error": error}, db=session
            )
            if saved is None:
                raise SaverError("Native assistant error could not be persisted")
            message.update(error=error, workspace_diagnostic=diagnostic)
            history = json_object(chat.get("history"))
            messages = json_object(history.get("messages"))
            messages[context.message_id] = message
            history["messages"], chat["history"] = messages, history
            await session.execute(
                update(self.chats)
                .where(self.chats.c.id == context.chat_id)
                .values(chat=chat, updated_at=int(time.time()))
            )
        return error

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
        self.require_callable(self.users_api, "get_user_by_id", True, "id")
        self.require_callable(
            self.chats_api,
            "get_accessible_chat_by_id",
            True,
            "id",
            object(),
            db=object(),
            permission="write",
            chat=object(),
        )
        self.require_callable(self.config, "get", True, "key", None)
        self.require_callable(self.config, "get", True, "key")
        self.require_callable(
            self.file_access, "__call__", True, "id", "read", object(), db=object()
        )
        self.require_callable(self.storage, "get_file", False, "path")
        self.require_callable(self.storage, "upload_file", False, io.BytesIO(), "name", {})

    @staticmethod
    def require_callable(
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
