"""Phase-specific failure recovery preserves completed programs and request progress."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import JSON_VALUE, FileBytes, json_text
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter


class InterruptedBrowser(BrowserReply):
    def __init__(self, phase: str, *, disconnect: bool = True) -> None:
        super().__init__([FileBytes("outputs/result.txt", b"executed once")])
        self.phase, self.disconnect, self.interrupted = phase, disconnect, True

    async def __call__(self, payload: dict[str, JsonValue]) -> object:
        raw = JSON_VALUE.validate_python(await super().__call__(payload))
        assert isinstance(raw, dict) and isinstance(raw["result"], str)
        reply = JSON_VALUE.validate_json(raw["result"])
        assert isinstance(reply, dict)
        if reply["phase"] == self.phase and self.interrupted:
            if self.disconnect:
                return {"error": "Client session disconnected."}
            reply.update(outcome="snapshot_error", files=[], manifest=[])
        return {"result": json_text(reply)}


@pytest.mark.integration
@pytest.mark.parametrize(
    ("phase", "execution", "next_step"),
    [
        ("prepare", "not_started", "reconnect_before_new_execution"),
        ("execute", "unknown", "inspect_original_runtime"),
        ("snapshot", "completed", "save_completed_files"),
    ],
)
def test_callback_loss_reports_the_actual_dispatch_state(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    phase: str,
    execution: str,
    next_step: str,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = InterruptedBrowser(phase)
            failed = await host.call(browser)
            plan = failed["recovery_plan"]
            assert isinstance(plan, dict)
            assert (plan["execution"], plan["next_step"]) == (execution, next_step)
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            adapter = OpenWebUIAdapter()
            saved = await adapter.state.get(host.context, operation_id)
            assert saved.execution_completed == (phase == "snapshot")
            assert not saved.checkpoint.committed and not host.provider.uploads
            if phase == "snapshot":
                assert saved.stdout == "synthetic output"
                browser.interrupted = False
                recovered = await host.call(browser, action="recover", operation_id=operation_id)
                assert recovered["state"] == "published"
                assert len([c for c in browser.configs if c["phase"] == "execute"]) == 1
                assert all(c["code"] == "" for c in browser.configs if c["phase"] == "recover")
            assert len(await adapter.state.pending(host.context)) == 1

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("disconnect", [False, True])
def test_successful_recovery_releases_only_the_current_request_gate(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    disconnect: bool,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = InterruptedBrowser("snapshot", disconnect=disconnect)
            metadata: dict[str, JsonValue] = {"task_id": "task-a", "session_id": "browser-a"}
            failed = await host.call(browser, metadata=metadata)
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            browser.interrupted = False
            recovered = await host.call(
                browser, action="recover", operation_id=operation_id, metadata=metadata
            )
            assert recovered["state"] == "published"
            continued = await host.call(browser, metadata=metadata)
            if disconnect:
                assert continued["error_code"] == "execution_session_blocked"
            else:
                assert continued["state"] == "published"
            assert len([c for c in browser.configs if c["phase"] == "execute"]) == (
                1 if disconnect else 2
            )

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("changed_phase", ["snapshot", "recover"])
def test_later_callback_cannot_replace_acknowledged_completion(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    changed_phase: str,
) -> None:
    class ChangedEvidenceBrowser(InterruptedBrowser):
        changed = True

        async def __call__(self, payload: dict[str, JsonValue]) -> object:
            raw = JSON_VALUE.validate_python(await super().__call__(payload))
            assert isinstance(raw, dict) and isinstance(raw["result"], str)
            reply = JSON_VALUE.validate_json(raw["result"])
            assert isinstance(reply, dict)
            if self.changed and reply["phase"] == changed_phase:
                reply["stdout"] = "different completion"
            return {"result": json_text(reply)}

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = ChangedEvidenceBrowser("snapshot", disconnect=False)
            browser.interrupted = changed_phase == "recover"
            failed = await host.call(browser)
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            browser.interrupted = False
            if changed_phase == "recover":
                failed = await host.call(browser, action="recover", operation_id=operation_id)
            assert failed["incomplete"] and "evidence changed" in str(failed["error"])
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation_id)
            assert receipt.execution_completed and receipt.stdout == "synthetic output"
            assert not receipt.checkpoint.committed and not host.provider.uploads
            browser.changed = False
            recovered = await host.call(browser, action="recover", operation_id=operation_id)
            assert recovered["state"] == "published"
            assert len([c for c in browser.configs if c["phase"] == "execute"]) == 1

    asyncio.run(scenario())
