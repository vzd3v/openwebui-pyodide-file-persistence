"""Namespaced tables in the host DB; short transactions, no provider I/O."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    Connection,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy import inspect as inspect_sqlalchemy
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from .contracts import (
    SCHEMA_VERSION,
    BoundContext,
    ExecutionBlocked,
    FileSlot,
    OperationDiagnostics,
    Receipt,
    SaverError,
)


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
        # Additive Tool-owned tables only; never alter host tables or old receipts.
        self.diagnostic = Table(
            "pyodide_file_saver_diagnostic",
            self.metadata,
            Column("operation_id", String, primary_key=True),
            Column("diagnostic_json", Text, nullable=False),
            Column("active", Integer, nullable=False),
        )
        self.gate = Table(
            "pyodide_file_saver_gate",
            self.metadata,
            Column("actor_id", String, primary_key=True),
            Column("scope_key", String, primary_key=True),
            Column("chat_id", String, nullable=False),
            Column("operation_id", String, nullable=False),
        )
        self.browser = Table(
            "pyodide_file_saver_browser",
            self.metadata,
            Column("actor_id", String, primary_key=True),
            Column("chat_id", String, primary_key=True),
            Column("session_key", String, primary_key=True),
            Column("runtime_id", String, nullable=False),
        )

    async def browser_runtime(self, context: BoundContext, session_key: str) -> str:
        async with self.sessions() as session:
            value = await session.scalar(
                select(self.browser.c.runtime_id).where(
                    self.browser.c.actor_id == context.actor_id,
                    self.browser.c.chat_id == context.chat_id,
                    self.browser.c.session_key == session_key,
                )
            )
            return str(value) if value else ""

    async def bind_browser(self, context: BoundContext, session_key: str, runtime_id: str) -> None:
        constructor = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        async with self.transaction() as session:
            await session.execute(
                constructor(self.browser)
                .values(
                    actor_id=context.actor_id,
                    chat_id=context.chat_id,
                    session_key=session_key,
                    runtime_id=runtime_id,
                )
                .on_conflict_do_update(
                    index_elements=["actor_id", "chat_id", "session_key"],
                    set_={"runtime_id": runtime_id},
                )
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
        for table in self.metadata.sorted_tables:
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
        self,
        context: BoundContext,
        language: Literal["python", "bash"] = "python",
        *,
        diagnostics: OperationDiagnostics | None = None,
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
            if diagnostics is not None:
                for key in (diagnostics.request_key, diagnostics.session_key):
                    if not key:
                        continue
                    gate = (
                        (
                            await session.execute(
                                select(self.gate).where(
                                    self.gate.c.actor_id == context.actor_id,
                                    self.gate.c.scope_key == key,
                                )
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if gate:
                        raise ExecutionBlocked(
                            str(gate["operation_id"]) if gate["chat_id"] == context.chat_id else "",
                            "execution_request_blocked"
                            if key == diagnostics.request_key
                            else "execution_session_blocked",
                        )
                for pending, diagnostic in await self._unfinished(session, context):
                    if diagnostic.dispatch in {"possibly_running", "outcome_unknown"} or (
                        diagnostic.phase == "allocated" or not diagnostic.failure_code
                    ):
                        raise ExecutionBlocked(pending.operation_id, "execution_unresolved")
                _, _, unsaved = self._receipt_candidates()
                pending_id = await session.scalar(
                    select(self.operation.c.operation_id)
                    .where(
                        self.operation.c.actor_id == context.actor_id,
                        self.operation.c.chat_id == context.chat_id,
                        self.operation.c.base_revision >= int(row["revision"]),
                        unsaved,
                    )
                    .order_by(self.operation.c.created_at, self.operation.c.operation_id)
                    .limit(1)
                )
                if pending_id:
                    raise ExecutionBlocked(str(pending_id), "execution_recovery_required")
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
            if diagnostics is not None:
                await session.execute(
                    self.diagnostic.insert().values(
                        operation_id=operation_id,
                        diagnostic_json=diagnostics.model_dump_json(),
                        active=1,
                    )
                )
            return receipt, head

    async def record_refusal(
        self, context: BoundContext, diagnostics: OperationDiagnostics, operation_id: str
    ) -> bool:
        """Commit one warning per request after the refused allocation rolled back."""
        if not diagnostics.request_key:
            raise SaverError("A bound request is required for refusal recording")
        constructor = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        async with self.transaction() as session:
            inserted = await session.execute(
                constructor(self.gate)
                .values(
                    actor_id=context.actor_id,
                    scope_key=diagnostics.request_key,
                    chat_id=context.chat_id,
                    operation_id=operation_id,
                )
                .on_conflict_do_nothing(index_elements=["actor_id", "scope_key"])
                .returning(self.gate.c.operation_id)
            )
            return inserted.scalar_one_or_none() is not None

    async def diagnostics(self, context: BoundContext, operation_id: str) -> OperationDiagnostics:
        async with self.sessions() as session:
            receipt = await self.load(session, operation_id, context)
            return await self._diagnostics(session, receipt)

    async def release_request_gate(
        self, context: BoundContext, operation_id: str, request_key: str
    ) -> None:
        """Successful recovery permits this request to continue; dead sessions stay fenced."""
        if not request_key:
            return
        async with self.transaction() as session:
            await session.execute(
                select(self.workspace.c.actor_id)
                .where(
                    self.workspace.c.actor_id == context.actor_id,
                    self.workspace.c.chat_id == context.chat_id,
                )
                .with_for_update()
            )
            receipt = await self.load(session, operation_id, context, lock=True)
            if receipt.state not in {"accepted", "published"} or not receipt.checkpoint.committed:
                raise SaverError("Only acknowledged recovery can release a request gate")
            await session.execute(
                delete(self.gate).where(
                    self.gate.c.actor_id == context.actor_id,
                    self.gate.c.chat_id == context.chat_id,
                    self.gate.c.operation_id == operation_id,
                    self.gate.c.scope_key == request_key,
                )
            )

    async def _diagnostics(self, session: AsyncSession, receipt: Receipt) -> OperationDiagnostics:
        raw = await session.scalar(
            select(self.diagnostic.c.diagnostic_json).where(
                self.diagnostic.c.operation_id == receipt.operation_id
            )
        )
        if isinstance(raw, str):
            return OperationDiagnostics.model_validate_json(raw)
        return OperationDiagnostics(
            phase="legacy",
            dispatch="completed_reported" if receipt.execution_completed else "outcome_unknown",
        )

    async def _unfinished(
        self, session: AsyncSession, context: BoundContext
    ) -> list[tuple[Receipt, OperationDiagnostics]]:
        revision = await session.scalar(
            select(self.workspace.c.revision).where(
                self.workspace.c.actor_id == context.actor_id,
                self.workspace.c.chat_id == context.chat_id,
            )
        )
        incomplete, _, _ = self._receipt_candidates()
        rows = (
            await session.execute(
                select(self.operation.c.receipt_json, self.diagnostic.c.diagnostic_json)
                .select_from(
                    self.operation.outerjoin(
                        self.diagnostic,
                        self.operation.c.operation_id == self.diagnostic.c.operation_id,
                    )
                )
                .where(
                    self.operation.c.actor_id == context.actor_id,
                    self.operation.c.chat_id == context.chat_id,
                    self.operation.c.base_revision >= (revision or 0),
                    incomplete,
                    or_(self.diagnostic.c.active == 1, self.diagnostic.c.operation_id.is_(None)),
                )
                .order_by(self.operation.c.created_at, self.operation.c.operation_id)
                .limit(33)
            )
        ).all()
        result: list[tuple[Receipt, OperationDiagnostics]] = []
        for raw, diagnostic_json in rows:
            receipt = Receipt.model_validate_json(str(raw))
            if receipt.state == "allocated" and not receipt.execution_completed:
                diagnostic = (
                    OperationDiagnostics.model_validate_json(str(diagnostic_json))
                    if diagnostic_json is not None
                    else OperationDiagnostics(phase="legacy", dispatch="outcome_unknown")
                )
                result.append((receipt, diagnostic))
        return result

    def _receipt_candidates(
        self,
    ) -> tuple[ColumnElement[bool], ColumnElement[bool], ColumnElement[bool]]:
        raw = self.operation.c.receipt_json
        if self.engine.dialect.name == "sqlite":
            state = func.json_extract(raw, "$.state")
            completed = func.json_extract(raw, "$.execution_completed")
            ok = func.json_extract(raw, "$.python_ok")
        else:
            data = sql_cast(raw, JSON)
            state = data["state"].as_string()
            completed = data["execution_completed"].as_boolean()
            ok = data["python_ok"].as_boolean()
        return (
            (state == "allocated") & func.coalesce(completed, False).is_(False),
            or_(
                (state == "allocated") & completed.is_(True),
                state == "saved",
                (state == "accepted") & ok.is_(True),
            ),
            (state == "allocated") & completed.is_(True),
        )

    async def recoverable(self, context: BoundContext) -> list[Receipt]:
        _, candidate, _ = self._receipt_candidates()
        async with self.sessions() as session:
            rows = await session.scalars(
                select(self.operation.c.receipt_json)
                .where(
                    self.operation.c.actor_id == context.actor_id,
                    self.operation.c.chat_id == context.chat_id,
                    candidate,
                )
                .order_by(self.operation.c.created_at.desc(), self.operation.c.operation_id.desc())
                .limit(33)
            )
            return [Receipt.model_validate_json(str(raw)) for raw in rows]

    async def unresolved(self, context: BoundContext) -> list[Receipt]:
        async with self.sessions() as session:
            return [
                receipt
                for receipt, diagnostic in await self._unfinished(session, context)
                if diagnostic.dispatch != "not_dispatched" or not diagnostic.failure_code
            ]

    async def head(self, context: BoundContext) -> Receipt | None:
        async with self.sessions() as session:
            operation_id = await session.scalar(
                select(self.workspace.c.head_operation_id).where(
                    self.workspace.c.actor_id == context.actor_id,
                    self.workspace.c.chat_id == context.chat_id,
                )
            )
            return await self.load(session, str(operation_id), context) if operation_id else None

    async def mark(
        self,
        context: BoundContext,
        operation_id: str,
        *,
        phase: str | None = None,
        dispatch: Literal[
            "not_dispatched", "possibly_running", "completed_reported", "outcome_unknown"
        ]
        | None = None,
        failure_code: str = "",
        session_failed: bool = False,
    ) -> OperationDiagnostics:
        async with self.transaction() as session:
            # Match allocation's lock order so it cannot observe the failure
            # diagnostic after checking a not-yet-committed failure gate.
            await session.execute(
                select(self.workspace.c.actor_id)
                .where(
                    self.workspace.c.actor_id == context.actor_id,
                    self.workspace.c.chat_id == context.chat_id,
                )
                .with_for_update()
            )
            receipt = await self.load(session, operation_id, context, lock=True)
            diagnostic = await self._diagnostics(session, receipt)
            if phase is not None:
                diagnostic.phase = phase
            if dispatch is not None:
                diagnostic.dispatch = dispatch
            if failure_code:
                diagnostic.failure_code = failure_code
            constructor = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
            active = int(
                not receipt.execution_completed
                and (diagnostic.dispatch != "not_dispatched" or not diagnostic.failure_code)
            )
            await session.execute(
                constructor(self.diagnostic)
                .values(
                    operation_id=operation_id,
                    diagnostic_json=diagnostic.model_dump_json(),
                    active=active,
                )
                .on_conflict_do_update(
                    index_elements=["operation_id"],
                    set_={"diagnostic_json": diagnostic.model_dump_json(), "active": active},
                )
            )
            if failure_code:
                for key in (
                    diagnostic.request_key,
                    diagnostic.session_key if session_failed else "",
                ):
                    if key:
                        await session.execute(
                            constructor(self.gate)
                            .values(
                                actor_id=context.actor_id,
                                scope_key=key,
                                chat_id=context.chat_id,
                                operation_id=operation_id,
                            )
                            .on_conflict_do_nothing(index_elements=["actor_id", "scope_key"])
                        )
            return diagnostic

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
            await session.execute(
                select(self.workspace.c.actor_id)
                .where(
                    self.workspace.c.actor_id == context.actor_id,
                    self.workspace.c.chat_id == context.chat_id,
                )
                .with_for_update()
            )
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
