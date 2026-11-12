"""Bounded facts and honest capacity, independent of live browser availability."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyodide_file_saver.contracts import (
    JSON_VALUE,
    BrowserFailure,
    FailureCause,
    FileBytes,
    Limits,
    Receipt,
    json_text,
)
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.tool import Tools
from pyodide_file_saver.tool_results import acknowledged_capacity
from pyodide_file_saver.workspace_execution import browser_call
from pyodide_file_saver.workspace_recovery import native_failure_message, ui_locale


@pytest.mark.parametrize("version", [1, 2])
def test_capacity_uses_only_the_acknowledged_format_and_current_policy(version: int) -> None:
    retained = [
        FileBytes("outputs/result", b"12345"),
        FileBytes("scripts/.runs/latest/source.py", b"12"),
        FileBytes("scripts/.runs/latest/record.json", b"123"),
    ]
    receipt = Receipt.model_validate(
        {
            "operation_id": "saved",
            "actor_id": "actor",
            "chat_id": "chat",
            "message_id": "message",
            "base_revision": 0,
            "checkpoint_version": version,
            "checkpoint": {"path": "checkpoint.json", "file_id": "cp", "sha256": "", "size": 1},
            "files": [file.manifest().model_dump() for file in retained],
            "blobs": [
                {**file.manifest().model_dump(), "file_id": f"blob-{index}"}
                for index, file in enumerate(retained)
            ]
            if version == 2
            else [],
            "inputs": [{"path": "inputs/input/data", "file_id": "input", "sha256": "", "size": 8}],
        }
    )
    limits = Limits(max_files=4, max_total_bytes=20, max_input_bytes=30, max_file_bytes=10)
    capacity = acknowledged_capacity(receipt, limits)
    assert capacity["retained"] == {
        "count": 3,
        "bytes": 10,
        "remaining_count": 1,
        "remaining_bytes": 10,
        "within_current_policy": True,
        "run_record_count_included": 2,
    }
    assert capacity["inputs"] == {
        "count": 1,
        "bytes": 8,
        "remaining_count": 3,
        "remaining_bytes": 22,
        "within_current_policy": True,
    }
    assert capacity["live_pending_state"] == "not_measured"
    lowered = acknowledged_capacity(receipt, Limits(max_files=2, max_total_bytes=9))
    assert lowered["retained"] == {
        "count": 3,
        "bytes": 10,
        "remaining_count": 0,
        "remaining_bytes": 0,
        "within_current_policy": False,
        "run_record_count_included": 2,
    }
    assert acknowledged_capacity(None, limits)["retained"] is None


@pytest.mark.parametrize(
    ("variables", "locale"),
    [
        ({}, "en"),
        ({"{{USER_LANGUAGE}}": "ru-RU"}, "ru"),
        ({"{{USER_LANGUAGE}}": "en-US"}, "en"),
        ({"{{USER_LANGUAGE}}": "private-untrusted-locale"}, "en"),
    ],
)
def test_native_copy_uses_allowlisted_ui_preference_not_request_inference(
    variables: dict[str, JsonValue],
    locale: str,
) -> None:
    assert ui_locale({"variables": variables}) == locale
    cause = FailureCause(
        code="retained_count_limit", observed=65, limit=64, measurement="lower_bound"
    )
    message = native_failure_message(
        {"execution_completed": True, "workspace_saved": False, "cause": cause.model_dump()},
        locale,
    )
    assert "65" in message and "64" in message and "max_files" in message
    assert ("не менее" if locale == "ru" else "at least") in message
    assert "recover" in message and "private-untrusted-locale" not in message


@pytest.mark.parametrize("fault", ["timeout", "cancel"])
def test_phase_timeout_facts_preserve_external_cancellation(
    fault: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="pyodide_file_saver")

    async def callback(_payload: dict[str, JsonValue]) -> object:
        if fault == "cancel":
            raise asyncio.CancelledError("external cancellation")
        raise TimeoutError("private callback payload")

    async def scenario() -> None:
        if fault == "cancel":
            with pytest.raises(asyncio.CancelledError, match="external cancellation"):
                await browser_call(
                    callback, {"operation_id": "synthetic", "phase": "execute"}, Limits()
                )
            return
        with pytest.raises(BrowserFailure) as caught:
            await browser_call(
                callback, {"operation_id": "synthetic", "phase": "execute"}, Limits()
            )
        cause = caught.value.cause
        assert cause and cause.code == "callback_timeout" and cause.configured_wait_ms == 55000
        assert cause.elapsed_ms is not None and cause.elapsed_ms >= 0
        assert caught.value.details["configured_wait_ms"] == 55000

    asyncio.run(scenario())
    event = JSON_VALUE.validate_json(caplog.records[-1].message)
    assert isinstance(event, dict) and event["event"] == "browser_phase_finished"
    assert event["outcome"] == ("timeout" if fault == "timeout" else "cancelled")
    assert "private callback payload" not in caplog.text


@pytest.mark.integration
def test_numeric_limit_survives_results_database_logs_and_native_fallback(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="pyodide_file_saver")

    class LimitBrowser(BrowserReply):
        async def __call__(self, payload: dict[str, JsonValue]) -> object:
            result = JSON_VALUE.validate_python(await super().__call__(payload))
            current = self.configs[-1]
            policy = current.get("limits")
            if (
                current.get("phase") in {"snapshot", "recover"}
                and isinstance(policy, dict)
                and policy["max_files"] == 64
            ):
                assert isinstance(result, dict) and isinstance(result["result"], str)
                reply = JSON_VALUE.validate_json(result["result"])
                assert isinstance(reply, dict)
                reply.update(
                    outcome="snapshot_error",
                    cause={
                        "code": "retained_count_limit",
                        "observed": 65,
                        "limit": 64,
                        "measurement": "lower_bound",
                    },
                )
                result["result"] = json_text(reply)
            return result

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = LimitBrowser([])
            metadata: dict[str, JsonValue] = {"variables": {"{{USER_LANGUAGE}}": "ru"}}
            failed = await host.call(browser, metadata=metadata)
            assert (
                failed["execution_outcome"] == "ok" and failed["persistence_outcome"] == "pending"
            )
            assert "Программа завершилась" in str(failed["error"])
            assert "не менее 65" in str(failed["error"]) and "max_files" in str(failed["error"])
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            adapter = OpenWebUIAdapter()
            diagnostic = await adapter.state.diagnostics(host.context, operation_id)
            assert diagnostic.cause and diagnostic.cause.observed == 65
            assert diagnostic.first_failure_cause == diagnostic.cause
            assert "unchanged limit will fail again" in str(failed["recovery_plan"])

            async def upsert(
                message_id: str,
                chat_id: str,
                actor_id: str,
                patch: dict[str, JsonValue],
                db: AsyncSession | None = None,
            ) -> object:
                assert db is not None and actor_id == host.context.actor_id
                await db.execute(
                    update(host.messages)
                    .where(host.messages.c.id == f"{chat_id}-{message_id}")
                    .values(error=patch["error"])
                )
                return object()

            monkeypatch.setattr(
                sys.modules["open_webui.models.chat_messages"],
                "ChatMessages",
                SimpleNamespace(upsert_message=upsert),
                raising=False,
            )
            task_id = str(uuid4())
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
            notice = await adapter.persist_stop(
                host.context, {**metadata, "task_id": task_id}, {"receipt": failed["receipt"]}
            )
            assert notice["content"] == failed["error"]
            async with host.engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(host.messages.c.error).where(
                            host.messages.c.id
                            == f"{host.context.chat_id}-{host.context.message_id}"
                        )
                    )
                    == notice
                )
            current_metadata = {**metadata, "task_id": task_id}
            capacity = (await host.call(action="status", metadata=current_metadata))["capacity"]
            assert isinstance(capacity, dict) and capacity["state"] == "no_acknowledged_head"
            assert browser.calls == 1
            tool = Tools()
            tool.valves.max_files = 256
            recovered = await host.call(
                browser,
                action="recover",
                operation_id=operation_id,
                tool=tool,
                metadata=current_metadata,
            )
            assert recovered["status"] == "success" and recovered["operation_id"] == operation_id
            assert len([cfg for cfg in browser.configs if cfg["phase"] == "execute"]) == 1
            assert (
                await adapter.state.diagnostics(host.context, operation_id)
            ).first_failure_cause == diagnostic.cause

    asyncio.run(scenario())
    assert '"observed":65' in caplog.text and '"measurement":"lower_bound"' in caplog.text
