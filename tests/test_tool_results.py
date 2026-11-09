"""Native result errors, safe correction and independent durable outcomes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import BinaryIO, cast, get_type_hints

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import TypeAdapter
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import (
    JSON_VALUE,
    ActionName,
    FailureCause,
    FileBytes,
    LanguageName,
    json_text,
)
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.tool import Tools
from pyodide_file_saver.tool_results import exception_diagnostic


def test_public_arguments_expose_enum_schema() -> None:
    hints = get_type_hints(Tools.execute_code)
    assert TypeAdapter[object](hints["action"]).json_schema()["enum"] == [
        "execute",
        "retry",
        "status",
        "recover",
    ]
    assert TypeAdapter[object](hints["language"]).json_schema()["enum"] == ["python", "bash"]


def test_unknown_exception_class_name_is_not_diagnostic_content() -> None:
    class SecretPayloadError(Exception):
        pass

    assert exception_diagnostic(SecretPayloadError("private-payload")) == {
        "error_type": "Exception"
    }


@pytest.mark.integration
@pytest.mark.parametrize("category", ["TypeError", "OperationalError"])
def test_unexpected_failures_preserve_safe_category_and_owned_frame(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    category: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:

            def unavailable(
                file: BinaryIO, filename: str, tags: dict[str, str]
            ) -> tuple[bytes, str]:
                if category == "TypeError":
                    raise TypeError("private-provider-marker")
                raise OperationalError(
                    "private-provider-marker",
                    {"secret": "private-provider-marker"},
                    Exception("private-provider-marker"),
                )

            monkeypatch.setattr(host.provider, "upload_file", unavailable)
            failed = await host.call(BrowserReply([FileBytes("outputs/result.txt", b"result")]))
            assert failed["error_type"] == category
            assert failed["failure_frame"] == "store_slot" and isinstance(
                failed["failure_line"], int
            )
            assert failed["error_code"] == "host_operation_failed"
            assert "private-provider-marker" not in str(failed) + caplog.text
            assert category in caplog.text and "store_slot" in caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("action", "code", "language", "operation_id", "error_code", "field"),
    [
        ("execute", "", "python", "", "empty_program", "code"),
        ("private-rejected-value", "private-source", "python", "", "invalid_action", "action"),
        ("execute", "private-source", "private-language", "", "unsupported_language", "language"),
        ("retry", "private-source", "python", "", "unexpected_code", "code"),
        ("recover", "", "python", "", "missing_operation_id", "operation_id"),
        ("status", "", "python", "private-invalid-id", "invalid_operation_id", "operation_id"),
    ],
)
def test_validation_returns_actionable_failure_without_allocating_or_logging_values(
    action: str,
    code: str,
    language: str,
    operation_id: str,
    error_code: str,
    field: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="pyodide_file_saver")
    raw = asyncio.run(
        Tools().execute_code(
            action=cast(ActionName, action),
            code=code,
            language=cast(LanguageName, language),
            operation_id=operation_id,
            __chat_id__="synthetic-chat",
            __message_id__="synthetic-message",
        )
    )
    result = JSON_VALUE.validate_json(raw)
    assert isinstance(result, dict)
    assert result["status"] == "error" and result["error_code"] == error_code
    assert result["error"] and result["execution_outcome"] == "not_started"
    assert "operation_id" not in result
    details = result["error_details"]
    assert isinstance(details, dict) and details["field"] == field and details["example"]
    assert "private-" not in raw and "private-" not in caplog.text
    events = [JSON_VALUE.validate_json(record.message) for record in caplog.records]
    assert len(events) == 2 and all(isinstance(event, dict) for event in events)
    assert all(
        isinstance(event, dict) and event["invocation_id"] == result["invocation_id"]
        for event in events
    )


@pytest.mark.integration
def test_oversized_input_allows_corrected_execution_in_the_same_request(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            file_id = await host.add_input(host.context.actor_id, "synthetic.bin", b"123456")
            tool = Tools()
            tool.valves.max_file_bytes = 4
            browser = BrowserReply([])
            failed = await host.call(browser, tool=tool, files=[{"id": file_id}])
            assert failed["status"] == "error" and failed["error_code"] == "input_too_large"
            assert failed["error_details"] == {
                "file_id": file_id,
                "limit_bytes": 4,
                "size_bytes": 6,
                "input_origin": "new",
            }
            assert failed["execution_outcome"] == "not_started" and browser.calls == 0
            plan = failed["recovery_plan"]
            assert isinstance(plan, dict) and plan["next_step"] == "correct_inputs"
            assert "reconnect" not in str(plan).lower()
            corrected = await host.call(
                browser, tool=tool, files=[{"id": file_id}], input_file_ids=[]
            )
            assert corrected["status"] == "success" and browser.calls == 1
            assert corrected["error"] is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_program_error_is_native_failure_with_private_committed_state(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            failed = await host.call(
                BrowserReply([FileBytes("outputs/private.txt", b"private")], "error")
            )
            assert failed["status"] == "error" and failed["error_code"] == "program_error"
            assert failed["error"] and failed["python_ok"] is False
            assert failed["execution_outcome"] == "error"
            assert failed["persistence_outcome"] == "committed"
            assert failed["publication_outcome"] == "not_published" and "issued_files" not in failed
            status = await host.call(action="status")
            assert status["status"] == "success"
            assert status["capabilities"] == failed["capabilities"]

    asyncio.run(scenario())


@pytest.mark.integration
def test_diagnostics_keep_primary_failure_and_latest_attempt_without_new_tables(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            adapter = OpenWebUIAdapter()
            await adapter.initialize()
            receipt, _ = await adapter.state.allocate(host.context)
            cause = FailureCause(
                code="managed_alias",
                exception_type="ValueError",
                frame="managed_workspace",
                line=100,
            )
            await adapter.state.mark(
                host.context,
                receipt.operation_id,
                phase="snapshot",
                failure_code="browser_snapshot_failed",
                cause=cause,
            )
            latest = await adapter.state.mark(
                host.context,
                receipt.operation_id,
                phase="recover",
                failure_code="session_disconnected",
            )
            assert latest.failure_code == "session_disconnected" and latest.cause is None
            assert latest.first_failure_code == "browser_snapshot_failed"
            assert latest.first_failure_phase == "snapshot" and latest.first_failure_cause == cause
            assert latest.first_failure_at and latest.last_failure_at
            assert latest.last_failure_at >= latest.first_failure_at
            stored = await adapter.state.diagnostics(host.context, receipt.operation_id)
            assert stored == latest
            # Older JSON has no primary-failure evidence. A new attempt must not
            # invent the earlier cause or timestamp from its own failure.
            async with adapter.state.transaction() as session:
                await session.execute(
                    update(adapter.state.diagnostic)
                    .where(adapter.state.diagnostic.c.operation_id == receipt.operation_id)
                    .values(
                        diagnostic_json=json_text(
                            {"phase": "prepare", "failure_code": "browser_callback_timeout"}
                        )
                    )
                )
            upgraded = await adapter.state.mark(
                host.context,
                receipt.operation_id,
                phase="recover",
                failure_code="browser_snapshot_failed",
                cause=cause,
            )
            assert upgraded.first_failure_code == "browser_callback_timeout"
            assert upgraded.first_failure_phase == "unknown"
            assert upgraded.first_failure_at is None and upgraded.first_failure_cause is None
            assert upgraded.cause == cause

    asyncio.run(scenario())
