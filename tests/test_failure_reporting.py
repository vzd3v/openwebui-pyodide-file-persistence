"""Failures preserve accepted execution evidence without publishing unsafe links."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import BinaryIO
from uuid import uuid4

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyodide_file_saver.contracts import (
    JSON_VALUE,
    BoundContext,
    BrowserFailure,
    FileBytes,
    Limits,
    Receipt,
    SaverError,
    WorkspaceInvocationStopped,
    json_text,
)
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.tool import Tools, receipt_summary
from pyodide_file_saver.workspace_execution import browser_call


class Disconnected(dict[str, JsonValue]):
    reason = "session_disconnected"


@pytest.mark.integration
def test_repeated_failed_tool_calls_reuse_one_receipt_without_browser_dispatch(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    class RefusedBrowser(BrowserReply):
        async def __call__(self, payload: dict[str, JsonValue]) -> object:
            self.calls += 1
            return {"error": "Client session disconnected."}

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = RefusedBrowser([])
            results = [await host.call(browser) for _ in range(20)]
            assert browser.calls == 1
            assert len({str(item["operation_id"]) for item in results}) == 1
            assert results[1]["error_code"] == "execution_request_blocked"
            status = await host.call(action="status")
            operations = status["operations"]
            assert isinstance(operations, list) and len(operations) == 1

    asyncio.run(scenario())


def test_old_receipt_context_is_unknown_without_inventing_a_restored_head() -> None:
    receipt = Receipt.model_validate_json(
        json_text(
            {
                "operation_id": "synthetic-operation",
                "actor_id": "actor-a",
                "chat_id": "chat-a",
                "message_id": "assistant-a",
                "base_revision": 3,
                "checkpoint": {
                    "path": "checkpoint.json",
                    "file_id": "synthetic-file",
                    "sha256": "",
                    "size": 0,
                },
            }
        )
    )
    assert receipt_summary(receipt)["browser_workspace"] == {
        "mode": "unknown",
        "base_operation_id": None,
        "base_revision": 3,
    }


@pytest.mark.parametrize(
    "failure",
    [
        "disconnect",
        "stock_disconnect",
        "stock_timeout",
        "runtime",
        "python",
        "missing",
        "timeout",
        "list_code",
        "dict_code",
    ],
)
def test_callback_failure_codes_are_content_free_and_phase_bound(failure: str) -> None:
    async def scenario() -> None:
        async def callback(payload: dict[str, JsonValue]) -> object:
            if failure == "timeout":
                raise TimeoutError("private host detail")
            if failure == "disconnect":
                return Disconnected(error="private host detail")
            if failure == "stock_disconnect":
                return {"error": "Client session disconnected."}
            if failure == "stock_timeout":
                return {"error": "Event call timed out. The browser tab may be inactive or closed."}
            if failure in {"runtime", "python"}:
                return {
                    "error_code": "runtime_error" if failure == "runtime" else "python_exception",
                    "stderr": "private host detail",
                }
            if failure in {"list_code", "dict_code"}:
                return {"error_code": [] if failure == "list_code" else {}}
            return None

        with pytest.raises(BrowserFailure) as caught:
            await browser_call(
                callback, {"operation_id": "synthetic", "phase": "prepare"}, Limits()
            )
        assert caught.value.phase == "prepare"
        assert (
            caught.value.error_code
            == {
                "disconnect": "session_disconnected",
                "stock_disconnect": "session_disconnected",
                "stock_timeout": "browser_callback_timeout",
                "runtime": "browser_runtime_error",
                "python": "browser_python_exception",
                "missing": "browser_missing_response",
                "timeout": "browser_callback_timeout",
                "list_code": "browser_missing_response",
                "dict_code": "browser_missing_response",
            }[failure]
        )
        assert "private host detail" not in str(caught.value)

    asyncio.run(scenario())


@pytest.mark.integration
def test_native_stop_is_opt_in_and_missing_capability_prevents_dispatch(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            tool = Tools()
            tool.valves.stop_on_browser_failure = True
            browser = BrowserReply([])
            unavailable = await host.call(browser, tool=tool)
            assert unavailable["incomplete"] is True and browser.calls == 0
            persisted: list[dict[str, JsonValue]] = []
            emitted: list[dict[str, JsonValue]] = []

            async def upsert(
                message: str,
                chat: str,
                actor: str,
                patch: dict[str, JsonValue],
                db: AsyncSession | None = None,
            ) -> object:
                assert chat == host.context.chat_id and message == host.context.message_id
                assert actor == host.context.actor_id and db is not None
                persisted.append(patch)
                await db.execute(
                    update(host.messages)
                    .where(host.messages.c.id == f"{chat}-{message}")
                    .values(error=patch["error"])
                )
                return object()

            async def emitter(event: dict[str, JsonValue]) -> object:
                emitted.append(event)

            async def disconnected(payload: dict[str, JsonValue]) -> object:
                return {"error": "Client session disconnected."}

            monkeypatch.setattr(
                sys.modules["open_webui.models.chat_messages"],
                "ChatMessages",
                SimpleNamespace(upsert_message=upsert),
                raising=False,
            )
            monkeypatch.setattr(sys.modules["open_webui.env"], "VERSION", "0.12.3")
            task_id = str(uuid4())
            adapter = OpenWebUIAdapter()
            async with adapter.state.transaction() as session:
                chat, message = await adapter.validate_context(session, host.context, lock=True)
                message["meta"] = {"task_id": task_id}
                history = chat["history"]
                assert isinstance(history, dict) and isinstance(history["messages"], dict)
                history["messages"][host.context.message_id] = message
                await session.execute(
                    update(host.chats)
                    .where(host.chats.c.id == host.context.chat_id)
                    .values(chat=chat)
                )

            async def fail_browser() -> str:
                return await tool.execute_code(
                    code="print('never dispatched')",
                    __user__={"id": host.context.actor_id},
                    __chat_id__=host.context.chat_id,
                    __message_id__=host.context.message_id,
                    __request__=object(),
                    __metadata__={"task_id": task_id},
                    __event_call__=disconnected,
                    __event_emitter__=emitter,
                )

            # The first failure is actionable output, not whole-response cancellation.
            first_failure = JSON_VALUE.validate_json(await fail_browser())
            assert isinstance(first_failure, dict) and first_failure["incomplete"] is True
            plan = first_failure["recovery_plan"]
            assert isinstance(plan, dict) and plan["execution"] == "not_started"
            assert not persisted and not emitted
            # A repeated action in the failed request remains bounded.
            with pytest.raises(WorkspaceInvocationStopped):
                await fail_browser()
            assert len(persisted) == 1 and persisted[0]["error"]
            async with host.engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            select(host.messages).where(
                                host.messages.c.id
                                == f"{host.context.chat_id}-{host.context.message_id}"
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["error"] == persisted[0]["error"]
                assert row["content"] == "newer normalized text"
                assert row["output"] == [{"type": "message", "content": "preserved output"}]
                assert row["files"] == []
            assert [item["type"] for item in emitted] == ["status", "chat:completion"]
            status = await host.call(action="status", tool=tool, metadata={"task_id": task_id})
            operations = status["operations"]
            assert isinstance(operations, list) and isinstance(operations[0], dict)
            assert operations[0]["dispatch"] == "not_dispatched"
            operation_id = operations[0]["operation_id"]
            assert isinstance(operation_id, str)
            receipt = await adapter.state.get(host.context, operation_id)
            receipt.execution_completed, receipt.python_ok, receipt.stdout = True, True, "completed"
            async with adapter.state.transaction() as session:
                await adapter.state.put(session, receipt)
            await adapter.state.mark(
                host.context,
                operation_id,
                dispatch="completed_reported",
                phase="snapshot",
                failure_code="browser_snapshot_failed",
            )
            next_task = str(uuid4())
            async with adapter.state.transaction() as session:
                chat, message = await adapter.validate_context(session, host.context, lock=True)
                message["meta"] = {"task_id": next_task}
                history = chat["history"]
                assert isinstance(history, dict) and isinstance(history["messages"], dict)
                history["messages"][host.context.message_id] = message
                await session.execute(
                    update(host.chats)
                    .where(host.chats.c.id == host.context.chat_id)
                    .values(chat=chat)
                )

            async def replay() -> str:
                return await tool.execute_code(
                    code="print('never replay')",
                    __user__={"id": host.context.actor_id},
                    __chat_id__=host.context.chat_id,
                    __message_id__=host.context.message_id,
                    __request__=object(),
                    __metadata__={"task_id": next_task},
                    __event_call__=browser,
                    __event_emitter__=emitter,
                )

            first = JSON_VALUE.validate_json(await replay())
            assert isinstance(first, dict) and first["first_refusal"] is True
            assert first["error_code"] == "execution_recovery_required"
            assert first["stdout"] == "completed" and browser.calls == 0
            assert len(persisted) == 1 and len(emitted) == 2
            readable = await host.call(
                action="status",
                operation_id=operation_id,
                tool=tool,
                metadata={"task_id": next_task},
            )
            assert readable["operations"] and len(persisted) == 1
            with pytest.raises(WorkspaceInvocationStopped):
                await replay()
            assert len(persisted) == 2 and browser.calls == 0
            assert len(await adapter.state.pending(host.context)) == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_native_error_rechecks_generation_under_chat_lock(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            adapter = OpenWebUIAdapter()
            await adapter.initialize()
            original, replacement = str(uuid4()), str(uuid4())
            async with adapter.state.transaction() as session:
                chat, message = await adapter.validate_context(session, host.context, lock=True)
                message["meta"] = {"task_id": original}
                history = chat["history"]
                assert isinstance(history, dict) and isinstance(history["messages"], dict)
                history["messages"][host.context.message_id] = message
                await session.execute(
                    update(host.chats)
                    .where(host.chats.c.id == host.context.chat_id)
                    .values(chat=chat)
                )
            await adapter.validate_generation(host.context, {"task_id": original})
            async with adapter.state.transaction() as session:
                chat, message = await adapter.validate_context(session, host.context, lock=True)
                message["meta"] = {"task_id": replacement}
                history = chat["history"]
                assert isinstance(history, dict) and isinstance(history["messages"], dict)
                history["messages"][host.context.message_id] = message
                await session.execute(
                    update(host.chats)
                    .where(host.chats.c.id == host.context.chat_id)
                    .values(chat=chat)
                )
                # Both SQLite BEGIN IMMEDIATE and PostgreSQL row locks must
                # serialize this stale writer behind the replacement generation.
                stop = asyncio.create_task(
                    adapter.persist_stop(
                        host.context, {"task_id": original}, {"operation_id": "synthetic"}
                    )
                )
                done, _ = await asyncio.wait({stop}, timeout=0.05)
                assert not done
            with pytest.raises(SaverError, match="generation"):
                await stop
            async with adapter.state.sessions() as session:
                _, message = await adapter.validate_context(session, host.context)
                assert "error" not in message and message["meta"] == {"task_id": replacement}

    asyncio.run(scenario())


@pytest.mark.integration
def test_external_cancel_survives_diagnostic_database_failure(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:

            async def cancelled(payload: dict[str, JsonValue]) -> object:
                async with host.engine.begin() as connection:
                    if database_case.schema:
                        qualified = f'"{database_case.schema}".'
                        await connection.execute(
                            text(
                                f"CREATE FUNCTION {qualified}reject_diagnostic() RETURNS trigger "
                                "LANGUAGE plpgsql AS $$ BEGIN "
                                "RAISE EXCEPTION 'Synthetic failure'; END $$"
                            )
                        )
                        command = (
                            f"CREATE TRIGGER reject_diagnostic BEFORE UPDATE ON {qualified}"
                            "pyodide_file_saver_diagnostic FOR EACH ROW EXECUTE FUNCTION "
                            f"{qualified}reject_diagnostic()"
                        )
                    else:
                        command = (
                            "CREATE TRIGGER reject_diagnostic BEFORE UPDATE ON "
                            "pyodide_file_saver_diagnostic BEGIN "
                            "SELECT RAISE(ABORT, 'Synthetic failure'); END"
                        )
                    await connection.execute(text(command))
                raise asyncio.CancelledError("original cancellation")

            with pytest.raises(asyncio.CancelledError, match="original cancellation"):
                await Tools().execute_code(
                    code="print('synthetic')",
                    __user__={"id": host.context.actor_id},
                    __chat_id__=host.context.chat_id,
                    __message_id__=host.context.message_id,
                    __request__=object(),
                    __event_call__=cancelled,
                )

    asyncio.run(scenario())


@pytest.mark.integration
def test_early_bash_failure_records_requested_language_and_unknown_outcome(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:

            async def disconnected(payload: dict[str, JsonValue]) -> object:
                return Disconnected(error="private session detail")

            raw = await Tools().execute_code(
                code="echo synthetic",
                language="bash",
                __user__={"id": host.context.actor_id},
                __chat_id__=host.context.chat_id,
                __message_id__=host.context.message_id,
                __request__=object(),
                __event_call__=disconnected,
            )
            result = JSON_VALUE.validate_json(raw)
            assert isinstance(result, dict)
            assert result["error_code"] == "session_disconnected" and result["phase"] == "prepare"
            receipt = result["receipt"]
            assert isinstance(receipt, dict)
            assert receipt["language"] == "bash" and receipt["exit_code"] is None
            assert receipt["execution_outcome"] == "not_started"
            assert receipt["browser_workspace"] == {
                "mode": "unknown",
                "base_operation_id": None,
                "base_revision": 0,
            }
            assert not receipt["execution_completed"] and not receipt["workspace_saved"]
            assert "private session detail" not in raw
            assert "issued_files" not in result and not host.provider.uploads

    asyncio.run(scenario())


class ErrorBrowser(BrowserReply):
    async def __call__(self, payload: dict[str, JsonValue]) -> object:
        raw = JSON_VALUE.validate_python(await super().__call__(payload))
        assert isinstance(raw, dict) and isinstance(raw["result"], str)
        result = JSON_VALUE.validate_json(raw["result"])
        assert isinstance(result, dict)
        if result["phase"] in {"execute", "snapshot", "recover"}:
            result.update(stderr="synthetic traceback", exit_code=7, stdout_truncated=True)
        return {"result": json_text(result)}


@pytest.mark.integration
def test_partial_blob_recovery_reuses_reserved_ids_and_rejects_changed_snapshot(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [FileBytes("outputs/a.txt", b"first"), FileBytes("outputs/b.txt", b"second")]
            browser = BrowserReply(files)
            host.provider.fail_attempts = {2}
            failed = await host.call(browser)
            operation = failed["operation_id"]
            assert isinstance(operation, str)
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation)
            assert [slot.committed for slot in receipt.blobs] == [True, False]
            immutable_ids = [slot.file_id for slot in receipt.blobs]
            host.provider.fail_attempts.clear()
            browser.files = [files[0], FileBytes("outputs/b.txt", b"changed")]
            changed = await host.call(browser, action="recover", operation_id=operation)
            assert changed["incomplete"] is True and "issued_files" not in changed
            assert [
                slot.file_id for slot in (await adapter.state.get(host.context, operation)).blobs
            ] == immutable_ids
            browser.files = files
            recovered = await host.call(browser, action="recover", operation_id=operation)
            assert recovered["state"] == "published"
            final = await adapter.state.get(host.context, operation)
            assert [slot.file_id for slot in final.blobs] == immutable_ids
            recovery_calls = [item for item in browser.configs if item["phase"] == "recover"]
            assert len(recovery_calls) == 2 and all(item["code"] == "" for item in recovery_calls)
            assert not [item for item in browser.configs[2:] if item["phase"] == "prepare"]

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("provider_timeout", [False, True])
def test_accepted_error_streams_survive_provider_failure_without_checkpoint_or_links(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
    provider_timeout: bool,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            host.provider.fail_attempts = {1}
            if provider_timeout:

                def upload(
                    file: BinaryIO, filename: str, tags: dict[str, str]
                ) -> tuple[bytes, str]:
                    raise TimeoutError("private provider detail")

                monkeypatch.setattr(host.provider, "upload_file", upload)
            result = await host.call(
                ErrorBrowser([FileBytes("outputs/private.txt", b"private")], "error")
            )
            assert result["incomplete"] is True and "issued_files" not in result
            assert (
                result["stdout"] == "synthetic output" and result["stderr"] == "synthetic traceback"
            )
            assert result["exit_code"] == 7 and result["execution_outcome"] == "error"
            assert result["stdout_truncated"] is True
            receipt = result["receipt"]
            assert isinstance(receipt, dict)
            assert receipt["execution_completed"] is True and receipt["workspace_saved"] is False
            assert "old browser root" in str(result["workspace_context"])
            assert "callback" not in str(result["error"]).lower()
            assert "private provider detail" not in str(result)
            operation_id = result["operation_id"]
            assert isinstance(operation_id, str)
            status = await host.call(action="status", operation_id=operation_id)
            operations = status["operations"]
            assert isinstance(operations, list) and isinstance(operations[0], dict)
            assert operations[0]["stdout"] == result["stdout"]
            assert operations[0]["stderr"] == result["stderr"]
            assert operations[0]["exit_code"] == 7
            assert operations[0]["workspace_saved"] is False
            assert "issued_files" not in operations[0] and status["availability"] == "not_rechecked"
            recent = await host.call(action="status")
            recent_operations = recent["operations"]
            assert isinstance(recent_operations, list) and isinstance(recent_operations[0], dict)
            assert "stdout" not in recent_operations[0]
            denied = await host.call(
                action="status",
                operation_id=operation_id,
                context=BoundContext("actor-b", "chat-b", "assistant-b"),
            )
            assert denied["incomplete"] is True and "synthetic traceback" not in str(denied)

    asyncio.run(scenario())
