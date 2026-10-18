"""Real committed databases: head CAS, actor binding and non-destructive bootstrap."""

import asyncio
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest
from conftest import DatabaseCase, open_database
from sqlalchemy import delete, select, text, update

from pyodide_file_saver.contracts import BoundContext, SaverError
from pyodide_file_saver.workspace_state import WorkspaceState

pytestmark = pytest.mark.integration


def accept_in_worker(case: DatabaseCase, operation_id: str) -> str:
    async def run() -> str:
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(case.url, poolclass=NullPool)
        try:
            state = WorkspaceState(engine, case.schema)
            return (
                await state.accept(BoundContext("actor", "chat", "assistant"), operation_id)
            ).state
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_racing_heads_keep_loser_and_survive_new_state_instance(
    database_case: DatabaseCase,
) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            left, right = (
                WorkspaceState(engine, database_case.schema),
                WorkspaceState(engine, database_case.schema),
            )
            await asyncio.gather(left.initialize(), right.initialize())
            context = BoundContext("actor", "chat", "assistant")
            allocated = await asyncio.gather(left.allocate(context), right.allocate(context))
            receipts = [item[0] for item in allocated]
            assert receipts[0].operation_id != receipts[1].operation_id
            assert all(item.base_revision == 0 for item in receipts)
            for receipt in receipts:
                async with left.transaction() as session:
                    saved = await left.load(session, receipt.operation_id, context, lock=True)
                    saved.state, saved.checkpoint.committed = "saved", True
                    await left.put(session, saved)
            with ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                loop = asyncio.get_running_loop()
                states = await asyncio.gather(
                    *(
                        loop.run_in_executor(
                            pool, accept_in_worker, database_case, item.operation_id
                        )
                        for item in receipts
                    )
                )
            assert sorted(states) == ["accepted", "conflict"]
            outcomes = [await left.get(context, item.operation_id) for item in receipts]
            winner = next(item for item in outcomes if item.state == "accepted")
            restarted = WorkspaceState(engine, database_case.schema)
            _, restored = await restarted.allocate(context)
            assert restored and restored.operation_id == winner.operation_id
            for foreign in (
                BoundContext("other", "chat", "assistant"),
                BoundContext("actor", "other", "assistant"),
            ):
                with pytest.raises(SaverError):
                    await restarted.get(foreign, winner.operation_id)
            assert (
                await restarted.get(
                    context,
                    next(item.operation_id for item in outcomes if item.state == "conflict"),
                )
            ).checkpoint.committed

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["workspace", "head", "version", "shape"])
def test_corrupt_coordination_fails_without_reinitializing(
    database_case: DatabaseCase, damage: str
) -> None:
    async def scenario() -> None:
        async with open_database(database_case) as engine:
            state = WorkspaceState(engine, database_case.schema)
            await state.initialize()
            context = BoundContext("actor", "chat", "assistant")
            receipt, _ = await state.allocate(context)
            async with state.transaction() as session:
                if damage == "workspace":
                    await session.execute(delete(state.workspace))
                elif damage == "head":
                    await session.execute(
                        update(state.workspace).values(revision=1, head_operation_id="missing")
                    )
                elif damage == "version":
                    await session.execute(update(state.workspace).values(schema_version=2))
                else:
                    qualified = f'"{database_case.schema}".' if database_case.schema else ""
                    await session.execute(
                        text(
                            f"ALTER TABLE {qualified}pyodide_file_saver_workspace "
                            "ADD COLUMN unexpected INTEGER"
                        )
                    )
            with pytest.raises(SaverError):
                if damage in {"version", "shape"}:
                    await state.initialize()
                else:
                    await state.allocate(context)
            assert (
                await state.get(context, receipt.operation_id)
            ).operation_id == receipt.operation_id
            if damage == "workspace":
                async with state.sessions() as session:
                    assert await session.scalar(select(state.workspace.c.actor_id)) is None

    asyncio.run(scenario())
