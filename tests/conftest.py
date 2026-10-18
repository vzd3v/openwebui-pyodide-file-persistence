"""Synthetic external host boundary; production modules and databases stay real."""

from __future__ import annotations

import ast
import asyncio
import io
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Event, Lock
from types import ModuleType, SimpleNamespace
from typing import BinaryIO
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from pyodide_file_saver.contracts import (
    JSON_VALUE,
    BoundContext,
    FileBytes,
    Limits,
    TransferFile,
    decode_files,
    encode_files,
    json_text,
)
from pyodide_file_saver.tool import Tools


@dataclass(frozen=True)
class DatabaseCase:
    url: str
    schema: str | None


@pytest.fixture(params=["sqlite", "postgres"])
def database_case(request: pytest.FixtureRequest, tmp_path: Path) -> DatabaseCase:
    if request.param == "sqlite":
        return DatabaseCase(f"sqlite+aiosqlite:///{tmp_path / 'host.db'}", None)
    url = os.getenv("SAVER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set SAVER_TEST_DATABASE_URL to an isolated PostgreSQL test database")
    if not url.startswith("postgresql+"):
        pytest.fail("SAVER_TEST_DATABASE_URL must select PostgreSQL")
    return DatabaseCase(url, "pfs_test_" + uuid4().hex)


@asynccontextmanager
async def open_database(case: DatabaseCase) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(case.url, poolclass=NullPool)
    try:
        if case.schema:
            # Generated identifier, isolated test DB only. Never drop existing data.
            async with engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{case.schema}"'))
        yield engine
    finally:
        await engine.dispose()


class Provider:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploads: list[str] = []
        self.fail_attempts: set[int] = set()
        self.barrier: Barrier | None = None
        self.pause_attempt: int | None = None
        self.paused, self.release = Event(), Event()
        self.lock = Lock()

    def upload_file(self, file: BinaryIO, filename: str, tags: dict[str, str]) -> tuple[bytes, str]:
        with self.lock:
            self.uploads.append(filename)
            attempt = len(self.uploads)
        data = file.read()
        if not data or attempt in self.fail_attempts:
            raise ValueError("Synthetic provider rejection")
        path = self.root / filename
        # Match ordinary overwrite semantics to expose accidental object-name reuse.
        with path.open("wb") as stream:
            stream.write(data[:1])
            if self.barrier and not filename.endswith("_checkpoint.json"):
                self.barrier.wait(timeout=10)
            if attempt == self.pause_attempt:
                self.paused.set()
                if not self.release.wait(timeout=10):
                    raise TimeoutError("Synthetic slow provider was not released")
            stream.write(data[1:])
        return data, str(path)

    def get_file(self, file_path: str) -> str:
        return file_path


class BrowserReply:
    """A callback fixture, not a Python executor or a real-browser acceptance test."""

    def __init__(self, files: list[FileBytes], outcome: str = "ok") -> None:
        self.files, self.outcome = files, outcome
        self.calls = 0
        self.restored: list[list[FileBytes]] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def __call__(self, payload: dict[str, JsonValue]) -> object:
        self.calls += 1
        data = payload["data"]
        assert isinstance(data, dict) and data["files"] == []
        code = data["code"]
        assert isinstance(code, str)
        config: dict[str, JsonValue] | None = None
        for node in ast.walk(ast.parse(code)):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "cfg" for target in node.targets
                )
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.args[0], ast.Constant)
                and isinstance(node.value.args[0].value, str)
            ):
                parsed = JSON_VALUE.validate_json(node.value.args[0].value)
                assert isinstance(parsed, dict)
                config = parsed
        assert config is not None and isinstance(config["restore"], list)
        self.restored.append(
            decode_files(
                [TransferFile.model_validate(item) for item in config["restore"]], Limits()
            )
        )
        self.entered.set()
        if self.release:
            await self.release.wait()
        return {
            "result": json_text(
                {
                    "version": 1,
                    "operation_id": data["id"],
                    "nonce": config["nonce"],
                    "outcome": self.outcome,
                    "stdout": "synthetic output",
                    "stderr": "",
                    "files": encode_files(self.files),
                }
            ),
            "stdout": None,
            "stderr": None,
        }


class HostHarness:
    def __init__(self, engine: AsyncEngine, schema: str | None, root: Path) -> None:
        self.engine = engine
        self.metadata = MetaData(schema=schema)
        self.context = BoundContext("actor-a", "chat-a", "assistant-a")
        self.users = {
            "actor-a": SimpleNamespace(id="actor-a", role="user"),
            "actor-b": SimpleNamespace(id="actor-b", role="user"),
        }
        self.write_allowed = True
        self.fail_message_write = False
        self.provider = Provider(root)
        self.config: dict[str, object] = {"code_interpreter.engine": "pyodide"}
        self.files = Table(
            "file",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("user_id", String),
            Column("hash", String),
            Column("filename", String),
            Column("path", String),
            Column("data", JSON),
            Column("meta", JSON),
            Column("created_at", Integer),
            Column("updated_at", Integer),
        )
        self.chats = Table(
            "chat",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("user_id", String),
            Column("chat", JSON),
            Column("updated_at", Integer),
        )
        self.messages = Table(
            "chat_message",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("chat_id", String),
            Column("user_id", String),
            Column("role", String),
            Column("content", String),
            Column("files", JSON),
        )
        self.chat_files = Table(
            "chat_file",
            self.metadata,
            Column("id", String, primary_key=True),
            Column("user_id", String),
            Column("chat_id", String),
            Column("file_id", String),
            Column("message_id", String),
            Column("created_at", Integer),
            Column("updated_at", Integer),
            UniqueConstraint("chat_id", "file_id"),
        )

    async def start(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(self.metadata.create_all)
            for actor, chat, message in (
                ("actor-a", "chat-a", "assistant-a"),
                ("actor-b", "chat-b", "assistant-b"),
            ):
                await connection.execute(
                    self.chats.insert().values(
                        id=chat,
                        user_id=actor,
                        updated_at=0,
                        chat={
                            "history": {
                                "messages": {
                                    message: {
                                        "role": "assistant",
                                        "user_id": actor,
                                        "content": "JSON text",
                                    }
                                }
                            }
                        },
                    )
                )
                await connection.execute(
                    self.messages.insert().values(
                        id=f"{chat}-{message}",
                        chat_id=chat,
                        user_id=actor,
                        role="assistant",
                        content="newer normalized text",
                        files=[],
                    )
                )

    def install_modules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        surfaces: dict[str, dict[str, object]] = {
            "open_webui.env": {"VERSION": "0.12.2"},
            "open_webui.internal.db": {
                "async_engine": self.engine,
                "Base": SimpleNamespace(metadata=self.metadata),
            },
            "open_webui.models.files": {"File": SimpleNamespace(__table__=self.files)},
            "open_webui.models.chats": {
                "Chat": SimpleNamespace(__table__=self.chats),
                "ChatFile": SimpleNamespace(__table__=self.chat_files),
                "Chats": SimpleNamespace(get_accessible_chat_by_id=self.accessible_chat),
            },
            "open_webui.models.chat_messages": {
                "ChatMessage": SimpleNamespace(__table__=self.messages),
                "ChatMessages": SimpleNamespace(upsert_message=self.upsert_message),
            },
            "open_webui.models.users": {"Users": SimpleNamespace(get_user_by_id=self.user)},
            "open_webui.models.config": {"Config": SimpleNamespace(get=self.get_config)},
            "open_webui.storage.provider": {"Storage": self.provider},
            "open_webui.config": {"CODE_INTERPRETER_BLOCKED_MODULES": []},
            "open_webui.utils.access_control.files": {"has_access_to_file": self.file_access},
        }
        for name, attributes in surfaces.items():
            module = ModuleType(name)
            for key, value in attributes.items():
                setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)

    async def user(self, id: str, db: AsyncSession | None = None) -> object:
        return self.users.get(id)

    async def accessible_chat(
        self,
        id: str,
        user: object,
        db: AsyncSession | None = None,
        *,
        permission: str = "read",
        chat: object = None,
    ) -> object:
        assert isinstance(user, SimpleNamespace) and isinstance(chat, SimpleNamespace)
        return chat if self.write_allowed and chat.user_id == user.id else None

    async def file_access(
        self, file_id: str, access_type: str, user: object, db: AsyncSession | None = None
    ) -> bool:
        return False

    async def get_config(self, key: str, default: object = None) -> object:
        return self.config.get(key, default)

    async def upsert_message(
        self,
        message_id: str,
        chat_id: str,
        user_id: str,
        data: dict[str, JsonValue],
        db: AsyncSession | None = None,
    ) -> object:
        assert db is not None
        if self.fail_message_write:
            raise OSError("Synthetic host message write failure")
        await db.execute(
            update(self.messages)
            .where(self.messages.c.id == f"{chat_id}-{message_id}")
            .values(**data)
        )
        return None

    async def call(
        self,
        browser: BrowserReply | None = None,
        *,
        action: str = "execute",
        operation_id: str = "",
        context: BoundContext | None = None,
        files: list[dict[str, JsonValue]] | None = None,
        tool: Tools | None = None,
    ) -> dict[str, JsonValue]:
        context = context or self.context
        result = JSON_VALUE.validate_json(
            await (tool or Tools()).execute_code(
                code="print('synthetic')" if action == "execute" else "",
                action=action,
                operation_id=operation_id,
                __user__={"id": context.actor_id},
                __chat_id__=context.chat_id,
                __message_id__=context.message_id,
                __request__=object(),
                __event_call__=browser,
                __files__=files,
            )
        )
        assert isinstance(result, dict)
        return result

    async def file_ids(self) -> list[str]:
        async with self.engine.connect() as connection:
            return [str(value) for value in await connection.scalars(select(self.files.c.id))]

    async def add_input(self, owner: str, name: str, data: bytes) -> str:
        file_id = str(uuid4())
        _, path = self.provider.upload_file(io.BytesIO(data), file_id, {})
        async with self.engine.begin() as connection:
            await connection.execute(
                self.files.insert().values(
                    id=file_id, user_id=owner, filename=name, path=path, data={}, meta={}
                )
            )
        return file_id


@pytest.fixture
def host_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[AsyncEngine, str | None], HostHarness]:
    def create(engine: AsyncEngine, schema: str | None) -> HostHarness:
        host = HostHarness(engine, schema, tmp_path)
        host.install_modules(monkeypatch)
        return host

    return create


@asynccontextmanager
async def open_host(
    case: DatabaseCase, factory: Callable[[AsyncEngine, str | None], HostHarness]
) -> AsyncIterator[HostHarness]:
    async with open_database(case) as engine:
        host = factory(engine, case.schema)
        await host.start()
        yield host
