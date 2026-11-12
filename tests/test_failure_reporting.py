"""Failures preserve accepted execution evidence without publishing unsafe links."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import BinaryIO, Literal
from uuid import uuid4

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyodide_file_saver.contracts import (
    JSON_VALUE,
    BoundContext,
    BrowserFailure,
    FailureCause,
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
@pytest.mark.parametrize(
    ("version", "delivery_fault"),
    [("0.12.3", "none")]
    + [
        ("0.13.3", fault)
        for fault in (
            "none",
            "writer_none",
            "writer_raise",
            "writer_hang",
            "writer_cancel",
            "emitter_raise",
            "emitter_hang",
            "emitter_cancel",
            "generation_stale",
            "generation_unavailable",
        )
    ],
)
def test_native_stop_is_opt_in_and_missing_capability_prevents_dispatch(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    delivery_fault: str,
    caplog: pytest.LogCaptureFixture,
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
            recheck_pending = False

            async def upsert(
                message: str,
                chat: str,
                actor: str,
                patch: dict[str, JsonValue],
                db: AsyncSession | None = None,
            ) -> object:
                nonlocal recheck_pending
                assert chat == host.context.chat_id and message == host.context.message_id
                assert actor == host.context.actor_id and db is not None
                persisted.append(patch)
                if delivery_fault == "generation_stale":
                    recheck_pending = True
                    return None
                if delivery_fault == "writer_none":
                    return None
                if delivery_fault == "writer_raise":
                    raise RuntimeError("private native writer detail")
                if delivery_fault == "writer_hang":
                    await asyncio.Future()
                if delivery_fault == "writer_cancel":
                    raise asyncio.CancelledError
                await db.execute(
                    update(host.messages)
                    .where(host.messages.c.id == f"{chat}-{message}")
                    .values(error=patch["error"])
                )
                recheck_pending = delivery_fault == "generation_unavailable"
                return object()

            async def emitter(event: dict[str, JsonValue]) -> object:
                emitted.append(event)
                if delivery_fault == "emitter_raise":
                    raise RuntimeError("private native emitter detail")
                if delivery_fault == "emitter_hang":
                    await asyncio.Future()
                if delivery_fault == "emitter_cancel":
                    raise asyncio.CancelledError

            async def disconnected(payload: dict[str, JsonValue]) -> object:
                return {"error": "Client session disconnected."}

            monkeypatch.setattr(
                sys.modules["open_webui.models.chat_messages"],
                "ChatMessages",
                SimpleNamespace(upsert_message=upsert),
                raising=False,
            )
            monkeypatch.setattr(sys.modules["open_webui.env"], "VERSION", version)
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

            async def current_user(id: str, db: AsyncSession | None = None) -> object:
                nonlocal recheck_pending
                if recheck_pending:
                    recheck_pending = False
                    if delivery_fault == "generation_unavailable":
                        raise RuntimeError("private generation lookup detail")
                    # The external host lookup replaces the actual stored
                    # generation after rollback, before fresh context reads.
                    message["meta"] = {"task_id": str(uuid4())}
                    async with host.engine.begin() as connection:
                        await connection.execute(
                            update(host.chats)
                            .where(host.chats.c.id == host.context.chat_id)
                            .values(chat=chat)
                        )
                return await host.user(id, db=db)

            monkeypatch.setattr(
                sys.modules["open_webui.models.users"],
                "Users",
                SimpleNamespace(get_user_by_id=current_user),
            )

            # The first failure is actionable output, not whole-response cancellation.
            first_failure = JSON_VALUE.validate_json(await fail_browser())
            assert isinstance(first_failure, dict) and first_failure["incomplete"] is True
            plan = first_failure["recovery_plan"]
            assert isinstance(plan, dict) and plan["execution"] == "not_started"
            assert not persisted and not emitted
            # A repeated action in the failed request remains bounded.
            if delivery_fault.startswith("generation_"):
                with pytest.raises(WorkspaceInvocationStopped):
                    await fail_browser()
                assert not emitted and browser.calls == 0
                assert '"phase":"generation"' in caplog.text
                assert "private generation lookup detail" not in caplog.text
                return
            if delivery_fault in {"writer_none", "writer_raise", "writer_hang"}:
                failure = JSON_VALUE.validate_json(await fail_browser())
                assert isinstance(failure, dict) and failure["status"] == "error"
                assert failure["incomplete"] is True and failure["agent_guidance"]
                assert not emitted and browser.calls == 0
                async with host.engine.connect() as connection:
                    saved_error = await connection.scalar(
                        select(host.messages.c.error).where(
                            host.messages.c.id
                            == f"{host.context.chat_id}-{host.context.message_id}"
                        )
                    )
                    assert saved_error is None
                before = len(await adapter.state.pending(host.context))
                again = JSON_VALUE.validate_json(await fail_browser())
                assert isinstance(again, dict) and again["status"] == "error"
                assert len(await adapter.state.pending(host.context)) == before == 1
                assert not emitted and browser.calls == 0
                assert '"phase":"persist"' in caplog.text
                assert '"error_type":' in caplog.text
                assert "private native writer detail" not in caplog.text
                return
            if delivery_fault in {"writer_cancel", "emitter_cancel"}:
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await fail_browser()
                assert type(cancelled.value) is asyncio.CancelledError
                assert len(emitted) == (1 if delivery_fault == "emitter_cancel" else 0)
                return
            with pytest.raises(WorkspaceInvocationStopped):
                await fail_browser()
            assert len(persisted) == 1 and persisted[0]["error"]
            saved_error = persisted[0]["error"]
            assert isinstance(saved_error, dict)
            assert str(saved_error["content"]).startswith("The program did not start.")
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
            assert [item["type"] for item in emitted] == ["chat:completion"]
            if delivery_fault.startswith("emitter_"):
                assert '"phase":"emit"' in caplog.text
                assert "private native emitter detail" not in caplog.text
                return
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
                cause=FailureCause(code="retained_file_limit"),
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
            assert len(persisted) == 1 and len(emitted) == 1
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
            final_error = persisted[-1]["error"]
            assert isinstance(final_error, dict)
            assert final_error["type"] == "workspace_recovery_required"
            assert "max_file_bytes" in str(final_error["content"])
            assert "cleanup/resizing" in str(final_error["content"])
            assert "unavailable" not in str(final_error["content"])
            assert len(await adapter.state.pending(host.context)) == 1
            saved_notice = await adapter.persist_stop(
                host.context,
                {"task_id": next_task},
                {"receipt": {"execution_completed": True, "workspace_saved": True}},
            )
            assert "Existing results are saved" in str(saved_notice["content"])
            assert "may still be running" not in str(saved_notice["content"])

    asyncio.run(scenario())


@pytest.mark.integration
def test_native_error_rechecks_generation_under_chat_lock(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
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

            async def missing_message(
                message: str,
                chat: str,
                actor: str,
                patch: dict[str, JsonValue],
                db: AsyncSession | None = None,
            ) -> object:
                return None

            monkeypatch.setattr(
                sys.modules["open_webui.models.chat_messages"],
                "ChatMessages",
                SimpleNamespace(upsert_message=missing_message),
                raising=False,
            )
            with pytest.raises(SaverError, match="could not be persisted"):
                await adapter.persist_stop(
                    host.context, {"task_id": replacement}, {"operation_id": "synthetic"}
                )
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
            assert len(recovery_calls) == 2 and all(
                item["code"] == "" and not item.get("code_b64") for item in recovery_calls
            )
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


@pytest.mark.integration
@pytest.mark.parametrize("locale", ["en-US", "ru-RU"])
@pytest.mark.parametrize("prior_dispatch", ["not_dispatched", "outcome_unknown"])
def test_unbound_gate_preserves_current_refusal_and_cross_chat_privacy(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    prior_dispatch: Literal["not_dispatched", "outcome_unknown"],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            monkeypatch.setattr(sys.modules["open_webui.env"], "VERSION", "0.13.3")
            adapter = OpenWebUIAdapter()
            task_id = str(uuid4())
            metadata: dict[str, JsonValue] = {
                "task_id": task_id,
                "session_id": "shared-browser",
                "variables": {"{{USER_LANGUAGE}}": locale},
            }
            original = host.context
            await adapter.state.initialize()
            prior, _ = await adapter.state.allocate(
                original,
                diagnostics=adapter.invocation_diagnostics(original, metadata),
            )
            await adapter.state.mark(
                original,
                prior.operation_id,
                phase="prepare",
                dispatch=prior_dispatch,
                failure_code="session_disconnected",
                session_failed=True,
            )
            prior_diagnostic = await adapter.state.diagnostics(original, prior.operation_id)
            current = BoundContext(original.actor_id, "chat-b", "assistant-b")
            async with adapter.state.transaction() as session:
                row = JSON_VALUE.validate_python(
                    await session.scalar(
                        select(host.chats.c.chat).where(host.chats.c.id == current.chat_id)
                    )
                )
                assert isinstance(row, dict)
                history = row["history"]
                assert isinstance(history, dict) and isinstance(history["messages"], dict)
                message = history["messages"][current.message_id]
                assert isinstance(message, dict)
                message.update(user_id=current.actor_id, meta={"task_id": task_id})
                await session.execute(
                    update(host.chats)
                    .where(host.chats.c.id == current.chat_id)
                    .values(user_id=current.actor_id, chat=row)
                )
                await session.execute(
                    update(host.messages)
                    .where(host.messages.c.chat_id == current.chat_id)
                    .values(user_id=current.actor_id)
                )

            persisted: list[dict[str, JsonValue]] = []
            emitted: list[dict[str, JsonValue]] = []

            async def upsert(
                message: str,
                chat: str,
                actor: str,
                patch: dict[str, JsonValue],
                db: AsyncSession | None = None,
            ) -> object:
                assert db is not None and (message, chat, actor) == (
                    current.message_id,
                    current.chat_id,
                    current.actor_id,
                )
                persisted.append(patch)
                await db.execute(
                    update(host.messages)
                    .where(host.messages.c.id == f"{chat}-{message}")
                    .values(error=patch["error"])
                )
                return object()

            async def emitter(event: dict[str, JsonValue]) -> object:
                emitted.append(event)
                return None

            monkeypatch.setattr(
                sys.modules["open_webui.models.chat_messages"],
                "ChatMessages",
                SimpleNamespace(upsert_message=upsert),
                raising=False,
            )
            tool = Tools()
            tool.valves.stop_on_browser_failure = True
            browser = BrowserReply([])
            first = await host.call(
                browser,
                tool=tool,
                context=current,
                metadata=metadata,
                emitter=emitter,
            )
            assert first["error_code"] == "execution_session_blocked"
            assert first["first_refusal"] is True
            assert first["execution_outcome"] == "not_started"
            assert first["persistence_outcome"] == "unchanged"
            assert first["permitted_actions"] == ["status"]
            assert "operation_id" not in first and "receipt" not in first
            plan = first["recovery_plan"]
            assert isinstance(plan, dict) and plan["tool_actions"] == ["status"]
            assert first["message"] == first["recovery"] == plan["instructions"]
            assert not persisted and not emitted
            with pytest.raises(WorkspaceInvocationStopped):
                await host.call(
                    browser,
                    tool=tool,
                    context=current,
                    metadata=metadata,
                    emitter=emitter,
                )
            assert browser.configs == [] and browser.calls == 0
            assert len(persisted) == len(emitted) == 1
            native = persisted[0]["error"]
            assert isinstance(native, dict) and native["content"] == first["message"]
            diagnostic = native["diagnostic"]
            assert isinstance(diagnostic, dict)
            assert diagnostic["error_code"] == "execution_request_blocked"
            assert diagnostic["operation_id"] == "" and diagnostic["receipt"] is None
            assert diagnostic["execution_outcome"] == "not_started"
            assert diagnostic["persistence_outcome"] == "unchanged"
            for value in (first, native, emitted):
                assert prior.operation_id not in json_text(value)
                assert original.chat_id not in json_text(value)
            if locale == "ru-RU":
                assert "Текущий код не запускался, сохранённые файлы не изменились." in str(
                    native["content"]
                )
                assert "обратитесь к администратору" in str(native["content"])
            else:
                assert "The current code did not run and saved files are unchanged." in str(
                    native["content"]
                )
                assert "contact an administrator" in str(native["content"])
            async with adapter.state.transaction() as session:
                saved = await session.scalar(
                    select(host.messages.c.error).where(
                        host.messages.c.id == f"{current.chat_id}-{current.message_id}"
                    )
                )
                assert saved == native
                assert (
                    await session.scalar(select(func.count()).select_from(adapter.state.operation))
                    == 1
                )
                gates = (await session.execute(select(adapter.state.gate))).mappings().all()
                assert len(gates) == 3  # Original request/session plus current refusal.
                assert sum(gate["operation_id"] == prior.operation_id for gate in gates) == 2
            assert await adapter.state.diagnostics(original, prior.operation_id) == prior_diagnostic
            # Status cannot expose an operation from another chat or clear its gate.
            status = await host.call(action="status", tool=tool, context=current, metadata=metadata)
            assert status["operations"] == []
            assert prior.operation_id not in json_text(status)

    asyncio.run(scenario())
