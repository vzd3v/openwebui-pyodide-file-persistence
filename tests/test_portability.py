"""Contract drift fails closed; labels and unused execution settings do not."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from sqlalchemy import Column, Index, String, func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import FileBytes, SaverError
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.workspace_state import WorkspaceState

pytestmark = pytest.mark.integration
HostFactory = Callable[[AsyncEngine, str | None], HostHarness]


@pytest.mark.parametrize("version", ["0.12.0", "0.12.3", "0.13.0-fork", "unknown"])
def test_capabilities_not_version_labels(
    database_case: DatabaseCase,
    host_factory: HostFactory,
    version: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            monkeypatch.setattr(sys.modules["open_webui.env"], "VERSION", version)
            result = await host.call(BrowserReply([FileBytes("outputs/a.txt", b"issued")]))
            assert result["state"] == "published"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage",
    [
        "sync_access",
        "access_arguments",
        "async_provider",
        "user_arguments",
        "config_arguments",
        "missing_method",
        "column_type",
        "required_insert",
        "physical_column",
    ],
)
def test_host_contract_failure_precedes_tool_ddl_or_provider(
    database_case: DatabaseCase,
    host_factory: HostFactory,
    damage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            chats = sys.modules["open_webui.models.chats"]
            if damage == "sync_access":

                def sync_access(*args: object, **kwargs: object) -> bool:
                    return True

                monkeypatch.setattr(
                    chats,
                    "Chats",
                    SimpleNamespace(get_accessible_chat_by_id=sync_access),
                )
            elif damage == "access_arguments":

                async def access(id: str) -> bool:
                    return True

                monkeypatch.setattr(
                    chats, "Chats", SimpleNamespace(get_accessible_chat_by_id=access)
                )
            elif damage == "async_provider":

                async def get_file(path: str) -> str:
                    return path

                monkeypatch.setattr(host.provider, "get_file", get_file)
            elif damage in {"user_arguments", "config_arguments", "missing_method"}:

                async def bad() -> None:
                    return None

                module, key, name = (
                    ("open_webui.models.config", "Config", "get")
                    if damage == "config_arguments"
                    else ("open_webui.models.users", "Users", "get_user_by_id")
                )
                monkeypatch.setattr(
                    sys.modules[module],
                    key,
                    SimpleNamespace(**({} if damage == "missing_method" else {name: bad})),
                )
            elif damage == "column_type":
                host.files.c.meta.type = String()
            elif damage == "required_insert":
                host.files.append_column(Column("new_required", String, nullable=False))
            else:
                prefix = f'"{database_case.schema}".' if database_case.schema else ""
                async with host.engine.begin() as connection:
                    await connection.execute(
                        text(f"ALTER TABLE {prefix}file DROP COLUMN updated_at")
                    )
            with pytest.raises(SaverError):
                await OpenWebUIAdapter().initialize()
            async with host.engine.connect() as connection:
                tables = await connection.run_sync(
                    lambda conn: inspect(conn).get_table_names(schema=database_case.schema)
                )
            assert "pyodide_file_saver_operation" not in tables
            assert not host.provider.uploads

    asyncio.run(scenario())


def test_recovery_ignores_engine_and_blocked_module_policy(
    database_case: DatabaseCase,
    host_factory: HostFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            first = await host.call(BrowserReply([FileBytes("outputs/a.txt", b"issued")]))
            operation = first["operation_id"]
            assert isinstance(operation, str)
            adapter = OpenWebUIAdapter()
            async with host.engine.connect() as connection:
                before = list(
                    await connection.scalars(select(adapter.state.operation.c.operation_id))
                )
            host.config["code_interpreter.engine"] = "jupyter"
            monkeypatch.delitem(sys.modules, "open_webui.config")
            assert "operations" in await host.call(action="status")
            assert (await host.call(action="retry", operation_id=operation))["state"] == "published"
            browser = BrowserReply([])
            failed = await host.call(browser)
            assert "Pyodide" in str(failed) and browser.calls == 0
            host.config["code_interpreter.engine"] = "pyodide"
            failed = await host.call(browser)
            assert "Missing host capability" in str(failed) and browser.calls == 0
            async with host.engine.connect() as connection:
                after = list(
                    await connection.scalars(select(adapter.state.operation.c.operation_id))
                )
            assert before == after

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage", ["benign", "unique", "unique_expression", "changed_expected", "partial_expected"]
)
def test_only_benign_extra_indexes_preserve_receipts(
    database_case: DatabaseCase,
    host_factory: HostFactory,
    damage: str,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            adapter = OpenWebUIAdapter()
            await adapter.initialize()
            receipt, _ = await adapter.state.allocate(host.context)
            async with host.engine.begin() as connection:
                if damage in {"changed_expected", "partial_expected"}:
                    prefix = f'"{database_case.schema}".' if database_case.schema else ""
                    await connection.execute(
                        text(f"DROP INDEX {prefix}pyodide_file_saver_actor_chat_idx")
                    )
                index = Index(
                    "pyodide_file_saver_actor_chat_idx"
                    if damage in {"changed_expected", "partial_expected"}
                    else "extra_idx",
                    *(
                        (adapter.state.operation.c.actor_id, adapter.state.operation.c.chat_id)
                        if damage == "partial_expected"
                        else (func.lower(adapter.state.operation.c.actor_id),)
                        if damage == "unique_expression"
                        else (adapter.state.operation.c.created_at,)
                    ),
                    unique=damage in {"unique", "unique_expression"},
                    sqlite_where=text("base_revision > 0")
                    if damage == "partial_expected"
                    else None,
                    postgresql_where=text("base_revision > 0")
                    if damage == "partial_expected"
                    else None,
                )
                await connection.run_sync(index.create)
            fresh = WorkspaceState(host.engine, database_case.schema)
            if damage == "benign":
                await fresh.initialize()
            else:
                with pytest.raises(SaverError):
                    await fresh.initialize()
            assert (
                await fresh.get(host.context, receipt.operation_id)
            ).operation_id == receipt.operation_id

    asyncio.run(scenario())
