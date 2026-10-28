"""Authenticated socket-boundary fixtures exercise durable runtime continuity."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from types import ModuleType
from uuid import uuid4

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import FileBytes, digest
from pyodide_file_saver.openwebui_adapter import EventCall, OpenWebUIAdapter
from pyodide_file_saver.tool import Tools


async def generation(host: HostHarness, task: str) -> None:
    adapter = OpenWebUIAdapter()
    async with adapter.state.transaction() as session:
        chat, message = await adapter.validate_context(session, host.context, lock=True)
        message["meta"] = {"task_id": task}
        history = chat["history"]
        assert isinstance(history, dict) and isinstance(history["messages"], dict)
        history["messages"][host.context.message_id] = message
        await session.execute(
            update(host.chats).where(host.chats.c.id == host.context.chat_id).values(chat=chat)
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "interruption",
    [
        "prepare",
        "restore",
        "restore_lost",
        "execute",
        "unknown",
        "wrong_runtime",
        "foreign_actor",
        "other_chat",
        "expired",
        "stale_generation",
        "cancel",
        "cancel_inflight",
        "stale_live",
        "stale_live_slow_cancel",
        "stale_live_wrong_runtime",
        "stale_live_cancel",
        "cold_prepare",
    ],
)
def test_reconnect_requires_original_runtime_and_never_replays_user_code(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            task = str(uuid4())
            await generation(host, task)
            runtime = str(uuid4())
            original = BrowserReply(
                [FileBytes("outputs/result.txt", b"result")], runtime_id=runtime
            )
            wrong = BrowserReply([], runtime_id=str(uuid4()))
            pool: dict[str, dict[str, object]] = {}
            requests: list[tuple[str, str]] = []
            probed = asyncio.Event()
            cancelled = asyncio.Event()
            stale_started = asyncio.Event()
            attempts: list[tuple[str, str]] = []
            armed = False

            def session(actor: str = "actor-a", chat: str = "chat-a") -> dict[str, object]:
                return {"id": actor, "chat_ids": [chat], "last_seen_at": time.time()}

            def reconnect() -> None:
                if not interruption.startswith("stale_live"):
                    pool.pop("old", None)
                pool["wrong"] = session()
                if interruption == "foreign_actor":
                    pool["new"] = session(actor="actor-b")
                elif interruption == "other_chat":
                    pool["new"] = session(chat="chat-b")
                elif interruption == "expired":
                    pool["new"] = {**session(), "last_seen_at": 0}
                elif interruption not in {"wrong_runtime", "cancel", "stale_live_wrong_runtime"}:
                    pool["new"] = session()

            async def factory(info: dict[str, JsonValue]) -> EventCall:
                sid = str(info["session_id"])

                async def callback(payload: dict[str, JsonValue]) -> object:
                    data = payload["data"]
                    assert isinstance(data, dict) and data["session_id"] == sid
                    if armed and interruption.startswith("stale_live") and sid == "old":
                        attempts.append((sid, "stalled"))
                        stale_started.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            if interruption == "stale_live_slow_cancel":
                                await asyncio.sleep(0.6)
                            cancelled.set()
                            raise
                    browser = wrong if sid == "wrong" else original
                    raw = await browser(payload)
                    config = browser.configs[-1]
                    phase = str(config["phase"])
                    attempts.append((sid, phase))
                    if interruption == "cold_prepare" and phase == "prepare":
                        await asyncio.sleep(0.5)
                    if armed and interruption == "cancel_inflight" and phase == "prepare":
                        probed.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled.set()
                            raise
                    assert data["id"] != config["operation_id"]
                    requests.append((sid, str(data["id"])))
                    if phase == "probe":
                        assert "code" not in config and data["files"] == []
                        probed.set()
                        if sid == "new" and interruption == "stale_generation":
                            await generation(host, str(uuid4()))
                    if (
                        armed
                        and sid == "old"
                        and (
                            interruption == phase
                            or interruption == "restore_lost"
                            and phase == "restore"
                        )
                    ):
                        reconnect()
                        if phase == "execute" or interruption == "restore_lost":
                            return {"error": "Client session disconnected."}
                    return raw

                return callback

            pool["old"] = session()
            socket = ModuleType("open_webui.socket.main")
            for name, value in {
                "SESSION_POOL": pool,
                "SESSION_POOL_TIMEOUT": 120,
                "get_session_pool_batches": lambda: [list(pool.items())],
                "get_event_call": factory,
            }.items():
                setattr(socket, name, value)
            monkeypatch.setitem(sys.modules, "open_webui.socket.main", socket)
            monkeypatch.setattr(sys.modules["open_webui.env"], "VERSION", "0.12.3")
            metadata: dict[str, JsonValue] = {"session_id": "old", "task_id": task}
            injected = await factory({"session_id": "old"})
            first_operation = ""
            if interruption not in {"unknown", "cold_prepare"}:
                first = await host.call(injected, metadata=metadata)
                assert first["state"] == "published"
                first_operation = str(first["operation_id"])
                assert original.calls == 1
            armed = True
            if interruption not in {
                "restore",
                "restore_lost",
                "execute",
                "cancel_inflight",
                "cold_prepare",
            }:
                reconnect()
            tool = Tools()
            tool.valves.callback_timeout_seconds = 1
            pending = asyncio.create_task(host.call(injected, metadata=metadata, tool=tool))
            if interruption in {"cancel", "cancel_inflight", "stale_live_cancel"}:
                await asyncio.wait_for(
                    stale_started.wait() if interruption == "stale_live_cancel" else probed.wait(),
                    timeout=2,
                )
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                if interruption == "cancel_inflight":
                    assert cancelled.is_set()
                adapter = OpenWebUIAdapter()
                receipts = await adapter.state.pending(host.context)
                interrupted = next(r for r in receipts if r.operation_id != first_operation)
                latest = await adapter.state.diagnostics(host.context, interrupted.operation_id)
                assert latest.dispatch == "not_dispatched"
                assert latest.failure_code == "invocation_cancelled"
                return
            result = await pending
            if interruption == "cold_prepare":
                assert result["state"] == "published" and original.calls == 1
                assert not any(sid != "old" for sid, _ in requests)
                return
            assert wrong.calls == 0
            assert all(c["phase"] == "probe" for c in wrong.configs)
            assert len({identity for _, identity in requests}) == len(requests)
            if interruption in {"prepare", "restore", "stale_live", "stale_live_slow_cancel"}:
                assert result["state"] == "published"
                assert original.calls == 2
                assert any(sid == "new" for sid, _ in requests)
                adapter = OpenWebUIAdapter()
                assert (
                    await adapter.state.browser_runtime(
                        host.context,
                        digest(b"old"),
                    )
                    == runtime
                )
                if interruption.startswith("stale_live"):
                    # A new call still receives the original injected SID. A known
                    # replacement is probed first instead of waiting on it again.
                    old_attempts = attempts.count(("old", "stalled"))
                    continued = await host.call(injected, metadata=metadata, tool=tool)
                    assert continued["state"] == "published" and original.calls == 3
                    assert attempts.count(("old", "stalled")) == old_attempts
                    await asyncio.wait_for(cancelled.wait(), timeout=1)
            else:
                assert result["incomplete"]
                assert original.calls == (
                    0 if interruption == "unknown" else 2 if interruption == "execute" else 1
                )
                receipt = result["receipt"]
                assert isinstance(receipt, dict)
                assert receipt["dispatch"] == (
                    "outcome_unknown" if interruption == "execute" else "not_dispatched"
                )

    asyncio.run(scenario())
