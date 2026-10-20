"""Failures preserve accepted execution evidence without publishing unsafe links."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import BinaryIO

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import (
    JSON_VALUE,
    BoundContext,
    BrowserFailure,
    FileBytes,
    Limits,
    Receipt,
    json_text,
)
from pyodide_file_saver.tool import Tools, receipt_summary
from pyodide_file_saver.workspace_execution import browser_call


class Disconnected(dict[str, JsonValue]):
    reason = "session_disconnected"


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
    "failure", ["disconnect", "runtime", "python", "missing", "timeout", "list_code", "dict_code"]
)
def test_callback_failure_codes_are_content_free_and_phase_bound(failure: str) -> None:
    async def scenario() -> None:
        async def callback(payload: dict[str, JsonValue]) -> object:
            if failure == "timeout":
                raise TimeoutError("private host detail")
            if failure == "disconnect":
                return Disconnected(error="private host detail")
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
            assert receipt["execution_outcome"] == "unknown"
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
        if result["phase"] == "execute":
            result.update(stderr="synthetic traceback", exit_code=7, stdout_truncated=True)
        return {"result": json_text(result)}


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
