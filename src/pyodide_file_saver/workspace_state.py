"""Two namespaced tables in the host DB; short transactions, no provider I/O."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    select,
    text,
    update,
)
from sqlalchemy import inspect as inspect_sqlalchemy
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .contracts import SCHEMA_VERSION, BoundContext, FileSlot, Receipt, SaverError


class WorkspaceState:
    def __init__(self, engine: AsyncEngine, schema: str | None = None):
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise SaverError("Only PostgreSQL and disk SQLite are eligible host databases")
        if engine.dialect.name == "sqlite" and engine.url.database in {None, "", ":memory:"}:
            raise SaverError("In-memory SQLite is not a durable workspace database")
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.metadata = MetaData(schema=schema)
        self.workspace = Table(
            "pyodide_file_saver_workspace",
            self.metadata,
            Column("actor_id", String, primary_key=True),
            Column("chat_id", String, primary_key=True),
            Column("schema_version", Integer, nullable=False),
            Column("revision", Integer, nullable=False),
            Column("head_operation_id", String, nullable=True),
        )
        self.operation = Table(
            "pyodide_file_saver_operation",
            self.metadata,
            Column("operation_id", String, primary_key=True),
            Column("actor_id", String, nullable=False),
            Column("chat_id", String, nullable=False),
            Column("message_id", String, nullable=False),
            Column("base_revision", Integer, nullable=False),
            Column("version", Integer, nullable=False),
            Column("checkpoint_file_id", String, nullable=False),
            Column("receipt_json", Text, nullable=False),
            Column("created_at", BigInteger, nullable=False),
            Column("updated_at", BigInteger, nullable=False),
            Index("pyodide_file_saver_actor_chat_idx", "actor_id", "chat_id"),
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            if self.engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def initialize(self) -> None:
        # Serializes first-use DDL across workers without a process-local singleton.
        async with self.engine.connect() as connection:
            async with connection.begin():
                if self.engine.dialect.name == "postgresql":
                    await connection.execute(text("SELECT pg_advisory_xact_lock(735249401816231)"))
                else:
                    await connection.execute(text("BEGIN IMMEDIATE"))
                await connection.run_sync(self.metadata.create_all)
                await connection.run_sync(self._validate_shape)
        async with self.sessions() as session:
            bad = await session.scalar(
                select(self.workspace.c.actor_id)
                .where(self.workspace.c.schema_version != SCHEMA_VERSION)
                .limit(1)
            )
            if bad is not None:
                raise SaverError("Unsupported workspace schema version; data was not changed")

    def _validate_shape(self, connection: Connection) -> None:
        inspector = inspect_sqlalchemy(connection)
        for table in (self.workspace, self.operation):
            columns = inspector.get_columns(table.name, schema=self.metadata.schema)
            by_name = {column["name"]: column for column in columns}
            if set(by_name) != set(table.columns.keys()):
                raise SaverError("Incompatible existing Tool table shape")
            for column in table.columns:
                actual = by_name[column.name]
                if not isinstance(actual["type"], type(column.type)):
                    raise SaverError("Incompatible existing Tool column type")
                if bool(actual["nullable"]) != column.nullable:
                    raise SaverError("Incompatible existing Tool column nullability")
            primary = inspector.get_pk_constraint(table.name, schema=self.metadata.schema)
            if primary["constrained_columns"] != [column.name for column in table.primary_key]:
                raise SaverError("Incompatible existing Tool primary key")
            if inspector.get_foreign_keys(table.name, schema=self.metadata.schema):
                raise SaverError("Unexpected Tool table foreign keys")
            if inspector.get_unique_constraints(table.name, schema=self.metadata.schema):
                raise SaverError("Unexpected Tool table unique constraints")
            if connection.dialect.name == "sqlite":
                # SQLAlchemy omits SQLite expression indexes from reflection.
                # Native metadata still exposes uniqueness; never overlook a
                # constraint just because the added index has an expression.
                quote = connection.dialect.identifier_preparer.quote
                schema = f"{quote(self.metadata.schema)}." if self.metadata.schema else ""
                indexes = connection.exec_driver_sql(
                    f"PRAGMA {schema}index_list({quote(table.name)})"
                ).mappings()
                if any(row["unique"] and row["origin"] != "pk" for row in indexes):
                    raise SaverError("Unexpected Tool table unique index")
            expected_indexes = {
                index.name: ([column.name for column in index.columns], bool(index.unique))
                for index in table.indexes
            }
            indexes = inspector.get_indexes(table.name, schema=self.metadata.schema)
            if any(
                index["name"] in expected_indexes
                and (
                    index.get("column_sorting")
                    or any(key.endswith("_where") for key in index.get("dialect_options", {}))
                )
                for index in indexes
            ):
                raise SaverError("Incompatible existing Tool index definition")
            actual_indexes = {
                index["name"]: (index["column_names"], bool(index["unique"]))
                for index in indexes
                if not index.get("duplicates_constraint")
            }
            if any(
                actual_indexes.get(name) != shape for name, shape in expected_indexes.items()
            ) or any(
                unique
                for name, (_, unique) in actual_indexes.items()
                if name not in expected_indexes
            ):
                raise SaverError("Incompatible existing Tool indexes")

    async def allocate(
        self, context: BoundContext, language: Literal["python", "bash"] = "python"
    ) -> tuple[Receipt, Receipt | None]:
        operation_id = str(uuid4())
        async with self.transaction() as session:
            constructor = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
            inserted = await session.execute(
                constructor(self.workspace)
                .values(
                    actor_id=context.actor_id,
                    chat_id=context.chat_id,
                    schema_version=SCHEMA_VERSION,
                    revision=0,
                    head_operation_id=None,
                )
                .on_conflict_do_nothing(index_elements=["actor_id", "chat_id"])
                .returning(self.workspace.c.actor_id)
            )
            if inserted.scalar_one_or_none() is not None:
                orphaned = await session.scalar(
                    select(self.operation.c.operation_id)
                    .where(
                        self.operation.c.actor_id == context.actor_id,
                        self.operation.c.chat_id == context.chat_id,
                    )
                    .limit(1)
                )
                if orphaned is not None:
                    raise SaverError(
                        "Workspace coordination row is missing; refusing reinitialization"
                    )
            row = (
                (
                    await session.execute(
                        select(self.workspace)
                        .where(
                            self.workspace.c.actor_id == context.actor_id,
                            self.workspace.c.chat_id == context.chat_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if row["schema_version"] != SCHEMA_VERSION:
                raise SaverError("Unsupported workspace schema version")
            head_id = row["head_operation_id"]
            if int(row["revision"]) < 0 or (head_id is None) != (row["revision"] == 0):
                raise SaverError("Workspace head/revision state is inconsistent")
            head = await self.load(session, str(head_id), context) if head_id else None
            if head and head.state not in {"accepted", "published"}:
                raise SaverError("Acknowledged workspace head has an invalid state")
            receipt = Receipt(
                operation_id=operation_id,
                actor_id=context.actor_id,
                chat_id=context.chat_id,
                message_id=context.message_id,
                base_revision=int(row["revision"]),
                base_operation_id=head.operation_id if head else None,
                language=language,
                checkpoint=FileSlot(
                    path="checkpoint.json", file_id=str(uuid4()), sha256="", size=0
                ),
                baseline=head.files if head else [],
            )
            now = int(time.time())
            await session.execute(
                self.operation.insert().values(
                    operation_id=operation_id,
                    actor_id=context.actor_id,
                    chat_id=context.chat_id,
                    message_id=context.message_id,
                    base_revision=receipt.base_revision,
                    version=0,
                    checkpoint_file_id=receipt.checkpoint.file_id,
                    receipt_json=receipt.model_dump_json(),
                    created_at=now,
                    updated_at=now,
                )
            )
            return receipt, head

    async def load(
        self,
        session: AsyncSession,
        operation_id: str,
        context: BoundContext,
        *,
        lock: bool = False,
    ) -> Receipt:
        query = select(self.operation.c.receipt_json).where(
            self.operation.c.operation_id == operation_id,
            self.operation.c.actor_id == context.actor_id,
            self.operation.c.chat_id == context.chat_id,
        )
        if lock:
            query = query.with_for_update()
        raw = await session.scalar(query)
        if not isinstance(raw, str):
            raise SaverError("Saved operation is missing or is not in this actor/chat workspace")
        receipt = Receipt.model_validate_json(raw)
        if (
            receipt.actor_id != context.actor_id
            or receipt.chat_id != context.chat_id
            or receipt.operation_id != operation_id
        ):
            raise SaverError("Saved operation binding is inconsistent")
        return receipt

    async def get(self, context: BoundContext, operation_id: str) -> Receipt:
        async with self.sessions() as session:
            return await self.load(session, operation_id, context)

    async def pending(self, context: BoundContext, limit: int = 10) -> list[Receipt]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(self.operation.c.receipt_json)
                .where(
                    self.operation.c.actor_id == context.actor_id,
                    self.operation.c.chat_id == context.chat_id,
                )
                .order_by(self.operation.c.created_at.desc(), self.operation.c.operation_id.desc())
                .limit(limit)
            )
            return [Receipt.model_validate_json(str(raw)) for raw in rows]

    async def put(self, session: AsyncSession, receipt: Receipt) -> None:
        await session.execute(
            update(self.operation)
            .where(self.operation.c.operation_id == receipt.operation_id)
            .values(
                receipt_json=receipt.model_dump_json(),
                version=self.operation.c.version + 1,
                updated_at=int(time.time()),
            )
        )

    async def accept(self, context: BoundContext, operation_id: str) -> Receipt:
        async with self.transaction() as session:
            receipt = await self.load(session, operation_id, context, lock=True)
            if receipt.state != "saved":
                return receipt
            if not receipt.checkpoint.committed or (
                receipt.checkpoint_version == 2
                and not all(blob.committed for blob in receipt.blobs)
            ):
                raise SaverError("Checkpoint is not committed")
            changed = await session.execute(
                update(self.workspace)
                .where(
                    self.workspace.c.actor_id == context.actor_id,
                    self.workspace.c.chat_id == context.chat_id,
                    self.workspace.c.revision == receipt.base_revision,
                )
                .values(revision=receipt.base_revision + 1, head_operation_id=operation_id)
                .returning(self.workspace.c.revision)
            )
            receipt.state = "accepted" if changed.scalar_one_or_none() is not None else "conflict"
            await self.put(session, receipt)
            return receipt
