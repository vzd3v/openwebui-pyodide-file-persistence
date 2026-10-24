"""Durable refusal and execution uncertainty survive independent processes."""

from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from conftest import DatabaseCase, open_database
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from pyodide_file_saver.contracts import BoundContext, ExecutionBlocked, OperationDiagnostics
from pyodide_file_saver.workspace_state import WorkspaceState

pytestmark = pytest.mark.integration


def allocate_in_process(
    case: DatabaseCase, request: str, session_key: str, record_refusal: bool = False
) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        engine = create_async_engine(case.url, poolclass=NullPool)
        try:
            state = WorkspaceState(engine, case.schema)
            await state.initialize()
            try:
                receipt, _ = await state.allocate(
                    BoundContext("actor", "chat", "assistant"),
                    diagnostics=OperationDiagnostics(request_key=request, session_key=session_key),
                )
                return receipt.operation_id, "allocated"
            except ExecutionBlocked as error:
                if record_refusal:
                    first = await state.record_refusal(
                        BoundContext("actor", "chat", "assistant"),
                        OperationDiagnostics(request_key=request, session_key=session_key),
                        error.operation_id,
                    )
                    return error.operation_id, "first_refusal" if first else "repeat_refusal"
                return error.operation_id, error.error_code or ""
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.mark.parametrize("dispatched", [False, True])
def test_failure_gates_survive_restart_without_receipt_growth(
    database_case: DatabaseCase, dispatched: bool
) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            state = WorkspaceState(engine, database_case.schema)
            await state.initialize()
            context = BoundContext("actor", "chat", "assistant")
            receipt, _ = await state.allocate(
                context,
                diagnostics=OperationDiagnostics(request_key="request-a", session_key="session-a"),
            )
            await state.mark(
                context,
                receipt.operation_id,
                phase="execute" if dispatched else "prepare",
                dispatch="outcome_unknown" if dispatched else "not_dispatched",
                failure_code="session_disconnected",
                session_failed=True,
            )
            with ProcessPoolExecutor(
                max_workers=1, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                loop = asyncio.get_running_loop()
                for request, session_key, expected in (
                    ("request-a", "session-a", "execution_request_blocked"),
                    ("request-b", "session-a", "execution_session_blocked"),
                    (
                        "request-b",
                        "session-b",
                        "execution_unresolved" if dispatched else "allocated",
                    ),
                ):
                    operation_id, code = await loop.run_in_executor(
                        pool, allocate_in_process, database_case, request, session_key
                    )
                    assert code == expected
                    if code != "allocated":
                        assert operation_id == receipt.operation_id
            async with state.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(state.operation)) == (
                    1 if dispatched else 2
                )
            restarted = WorkspaceState(engine, database_case.schema)
            diagnostic = await restarted.diagnostics(context, receipt.operation_id)
            assert diagnostic.failure_code == "session_disconnected"
            unresolved = await restarted.unresolved(context)
            assert (
                receipt.operation_id in [item.operation_id for item in unresolved]
            ) == dispatched
            # Actor isolation: another actor may use the same opaque session key.
            independent, _ = await restarted.allocate(
                BoundContext("other", "chat", "assistant"),
                diagnostics=OperationDiagnostics(request_key="request-a", session_key="session-a"),
            )
            assert independent.actor_id == "other"

    asyncio.run(scenario())


def test_unresolved_and_head_are_independent_of_recent_history(database_case: DatabaseCase) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            state = WorkspaceState(engine, database_case.schema)
            await state.initialize()
            context = BoundContext("actor", "chat", "assistant")
            head, _ = await state.allocate(context)
            async with state.transaction() as session:
                head.state, head.checkpoint.committed, head.execution_completed = (
                    "saved",
                    True,
                    True,
                )
                await state.put(session, head)
            await state.accept(context, head.operation_id)
            unknown, _ = await state.allocate(context, diagnostics=OperationDiagnostics())
            await state.mark(context, unknown.operation_id, dispatch="outcome_unknown")
            completed, _ = await state.allocate(context)
            async with state.transaction() as session:
                completed.execution_completed = True
                await state.put(session, completed)
                await session.execute(
                    update(state.operation)
                    .where(
                        state.operation.c.operation_id.in_(
                            [unknown.operation_id, completed.operation_id]
                        )
                    )
                    .values(created_at=0)
                )
            for index in range(12):
                refused, _ = await state.allocate(context)
                await state.mark(
                    context,
                    refused.operation_id,
                    dispatch="not_dispatched",
                    phase="prepare",
                    failure_code=f"synthetic-{index}",
                )
            assert unknown.operation_id not in [
                item.operation_id for item in await state.pending(context)
            ]
            assert [item.operation_id for item in await state.unresolved(context)] == [
                unknown.operation_id
            ]
            current = await state.head(context)
            assert current and current.operation_id == head.operation_id
            assert [item.operation_id for item in await state.recoverable(context)] == [
                completed.operation_id
            ]

    asyncio.run(scenario())


def test_guarded_allocation_race_across_processes_allocates_once(
    database_case: DatabaseCase,
) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            state = WorkspaceState(engine, database_case.schema)
            await state.initialize()
            with ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                loop = asyncio.get_running_loop()
                outcomes = await asyncio.gather(
                    *(
                        loop.run_in_executor(
                            pool, allocate_in_process, database_case, f"request-{i}", ""
                        )
                        for i in range(2)
                    )
                )
            assert sorted(code for _, code in outcomes) == ["allocated", "execution_unresolved"]
            assert len({operation for operation, _ in outcomes}) == 1
            async with state.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(state.operation)) == 1

    asyncio.run(scenario())


def test_completed_unsaved_fence_and_first_refusal_latch_race(database_case: DatabaseCase) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            state = WorkspaceState(engine, database_case.schema)
            await state.initialize()
            context = BoundContext("actor", "chat", "assistant")
            receipt, _ = await state.allocate(context)
            receipt.execution_completed, receipt.python_ok, receipt.stdout = True, True, "completed"
            async with state.transaction() as session:
                await state.put(session, receipt)
            await state.mark(context, receipt.operation_id, dispatch="completed_reported")
            with ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                loop = asyncio.get_running_loop()
                outcomes = await asyncio.gather(
                    *(
                        loop.run_in_executor(
                            pool, allocate_in_process, database_case, "new-generation", "", True
                        )
                        for _ in range(2)
                    )
                )
            assert sorted(code for _, code in outcomes) == ["first_refusal", "repeat_refusal"]
            assert {operation for operation, _ in outcomes} == {receipt.operation_id}
            with pytest.raises(ExecutionBlocked) as caught:
                await state.allocate(
                    context, diagnostics=OperationDiagnostics(request_key="later-task")
                )
            assert caught.value.error_code == "execution_recovery_required"
            saved = await state.get(context, receipt.operation_id)
            assert saved.execution_completed and saved.stdout == "completed"
            assert (
                await state.diagnostics(context, receipt.operation_id)
            ).dispatch == "completed_reported"
            assert not await state.unresolved(context)
            assert [item.operation_id for item in await state.recoverable(context)] == [
                receipt.operation_id
            ]
            async with state.sessions() as session:
                assert await session.scalar(select(func.count()).select_from(state.operation)) == 1
            saved.state, saved.checkpoint.committed, saved.python_ok = "saved", True, False
            async with state.transaction() as session:
                await state.put(session, saved)
            await state.accept(context, receipt.operation_id)
            next_receipt, _ = await state.allocate(
                context, diagnostics=OperationDiagnostics(request_key="after-recovery")
            )
            assert next_receipt.base_revision == 1

    asyncio.run(scenario())


def test_new_acknowledged_revision_fences_old_uncertainty_without_erasing_it(
    database_case: DatabaseCase,
) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            state = WorkspaceState(engine, database_case.schema)
            await state.initialize()
            context = BoundContext("actor", "chat", "assistant")
            old, _ = await state.allocate(context)
            newer, _ = await state.allocate(context)
            async with state.transaction() as session:
                newer.state, newer.checkpoint.committed, newer.execution_completed = (
                    "saved",
                    True,
                    True,
                )
                await state.put(session, newer)
            await state.accept(context, newer.operation_id)
            allocated, head = await state.allocate(context, diagnostics=OperationDiagnostics())
            assert head and head.operation_id == newer.operation_id
            assert allocated.base_revision == 1
            assert (
                await state.diagnostics(context, old.operation_id)
            ).dispatch == "outcome_unknown"
            assert (await state.get(context, old.operation_id)).state == "allocated"
            assert old.operation_id not in [
                item.operation_id for item in await state.unresolved(context)
            ]

    asyncio.run(scenario())
