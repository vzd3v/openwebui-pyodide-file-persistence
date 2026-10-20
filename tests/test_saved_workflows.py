"""Native Tool contracts with real state/storage and a synthetic external host/callback."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Barrier

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import BoundContext, FileBytes, Limits
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter, json_object
from pyodide_file_saver.tool import Tools, resume_publication

pytestmark = pytest.mark.integration
HostFactory = Callable[[AsyncEngine, str | None], HostHarness]


async def publication_trigger(host: HostHarness, enabled: bool) -> None:
    """Abort the final receipt write in the real DB, after attachment writes."""
    prefix = f'"{host.metadata.schema}".' if host.metadata.schema else ""
    table = f"{prefix}pyodide_file_saver_operation"
    async with host.engine.begin() as connection:
        if host.engine.dialect.name == "sqlite":
            statement = (
                f"CREATE TRIGGER reject_publication BEFORE UPDATE ON {table} "
                "WHEN json_extract(NEW.receipt_json, '$.state') = 'published' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic publication rejection'); END"
                if enabled
                else "DROP TRIGGER reject_publication"
            )
            await connection.execute(text(statement))
        elif enabled:
            await connection.execute(
                text(
                    f"CREATE FUNCTION {prefix}reject_publication() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    "IF NEW.receipt_json::json->>'state' = 'published' THEN "
                    "RAISE EXCEPTION 'synthetic publication rejection'; END IF; "
                    "RETURN NEW; END $$"
                )
            )
            await connection.execute(
                text(
                    f"CREATE TRIGGER reject_publication BEFORE UPDATE ON {table} "
                    f"FOR EACH ROW EXECUTE FUNCTION {prefix}reject_publication()"
                )
            )
        else:
            await connection.execute(text(f"DROP TRIGGER reject_publication ON {table}"))
            await connection.execute(text(f"DROP FUNCTION {prefix}reject_publication()"))


def operation(result: dict[str, JsonValue]) -> str:
    value = result["operation_id"]
    assert isinstance(value, str) and value
    return value


@pytest.mark.parametrize("fault", ["provider", "message"])
def test_legacy_saved_retry_converges_without_rerunning_python(
    database_case: DatabaseCase, host_factory: HostFactory, fault: str
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = BrowserReply(
                [
                    FileBytes("scripts/working.txt", b"private"),
                    FileBytes("outputs/Отчёт.txt", b"issued"),
                ]
            )
            host.provider.fail_attempts = {2} if fault == "provider" else set()
            if fault == "message":
                await OpenWebUIAdapter().initialize()
                await publication_trigger(host, True)
            legacy = await host.seed_legacy(browser.files)
            try:
                await resume_publication(
                    OpenWebUIAdapter(), host.context, legacy.operation_id, Limits()
                )
                raise AssertionError("Expected synthetic legacy publication failure")
            except (ValueError, DBAPIError):
                pass
            failed: dict[str, JsonValue] = {"operation_id": legacy.operation_id}

            adapter = OpenWebUIAdapter()
            saved = await adapter.state.get(host.context, operation(failed))
            assert saved.checkpoint.committed and saved.state == "accepted"
            async with host.engine.connect() as connection:
                assert (
                    await connection.scalar(select(func.count()).select_from(host.chat_files)) == 0
                )
                row = (
                    (
                        await connection.execute(
                            select(host.messages).where(host.messages.c.id == "chat-a-assistant-a")
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row and row["files"] == []
                assert row["content"] == "newer normalized text"
                assert row["output"] == [{"type": "message", "content": "preserved output"}]
                chat = json_object(
                    await connection.scalar(
                        select(host.chats.c.chat).where(host.chats.c.id == host.context.chat_id)
                    )
                )
                messages = json_object(json_object(chat["history"])["messages"])
                assert json_object(messages["assistant-a"]).get("files", []) == []
            host.provider.fail_attempts.clear()
            if fault == "message":
                await publication_trigger(host, False)
            results = await asyncio.gather(
                *(host.call(action="retry", operation_id=saved.operation_id) for _ in range(2))
            )
            assert all(result["state"] == "published" for result in results)
            assert len(await host.file_ids()) == 2
            receipt = await OpenWebUIAdapter().state.get(host.context, saved.operation_id)
            assert len(receipt.outputs) == 1
            assert (
                await adapter.verify_slot(host.context, receipt.outputs[0], Limits().max_file_bytes)
                == b"issued"
            )
            await adapter.verify_publication(host.context, receipt, Limits())
            async with host.engine.connect() as connection:
                assert (
                    await connection.scalar(select(func.count()).select_from(host.chat_files)) == 1
                )
                assert (
                    await connection.scalar(
                        select(host.messages.c.content).where(
                            host.messages.c.id == "chat-a-assistant-a"
                        )
                    )
                    == "newer normalized text"
                )
                assert await connection.scalar(
                    select(host.messages.c.output).where(host.messages.c.id == "chat-a-assistant-a")
                ) == [{"type": "message", "content": "preserved output"}]
                assert (
                    await connection.scalar(
                        select(host.chat_files.c.id).where(
                            host.chat_files.c.file_id == receipt.checkpoint.file_id
                        )
                    )
                    is None
                )
            links = results[0]["issued_files"]
            assert isinstance(links, list) and isinstance(links[0], dict)
            assert links[0]["attachment_url"] == f"/api/v1/files/{receipt.outputs[0].file_id}"
            assert (
                links[0]["download_url"]
                == f"/api/v1/files/{receipt.outputs[0].file_id}/content?attachment=true"
            )

    asyncio.run(scenario())


def test_private_error_output_stays_private_until_changed_and_deleted_issuance_is_not_recreated(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            original = [
                FileBytes("scripts/working.txt", b"private"),
                FileBytes("outputs/draft.txt", b"private error output"),
            ]
            failed = await host.call(BrowserReply(original, "python_error"))
            assert failed["workspace_saved"] is True and failed["python_ok"] is False
            browser = BrowserReply(original + [FileBytes("scripts/calculation.txt", b"42")])
            later = await host.call(browser)
            assert [{f.path: f.data for f in batch} for batch in browser.restored] == [
                {f.path: f.data for f in original}
            ]
            assert later["state"] == "published" and later["issued_files"] == []
            changed = [
                FileBytes("scripts/working.txt", b"private"),
                FileBytes("outputs/draft.txt", b"issued draft"),
            ]
            issued = await host.call(BrowserReply(changed))
            receipt = await OpenWebUIAdapter().state.get(host.context, operation(issued))
            output_id = receipt.outputs[0].file_id
            async with host.engine.begin() as connection:
                await connection.execute(delete(host.files).where(host.files.c.id == output_id))
            unchanged = await host.call(BrowserReply(changed))
            assert unchanged["incomplete"] is True and "issued_files" not in unchanged
            assert output_id not in await host.file_ids()

    asyncio.run(scenario())


def test_input_basename_collisions_and_checkpointed_edits_survive_original_deletion(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            first = await host.add_input("actor-a", "same.txt", b"first")
            second = await host.add_input("actor-a", "same.txt", b"second")
            aliases = [
                FileBytes(f"inputs/{first}/same.txt", b"edited"),
                FileBytes(f"inputs/{second}/same.txt", b"second"),
            ]
            attachments: list[dict[str, JsonValue]] = [
                {"type": "file", "id": file_id} for file_id in (first, second)
            ]
            legacy = await host.seed_legacy(aliases)
            await resume_publication(
                OpenWebUIAdapter(), host.context, legacy.operation_id, Limits()
            )
            async with host.engine.begin() as connection:
                await connection.execute(delete(host.files).where(host.files.c.id == first))
            fresh = BrowserReply(aliases)
            assert (await host.call(fresh, files=attachments))["state"] == "published"
            assert fresh.restored == [aliases]

    asyncio.run(scenario())


def test_context_file_and_operation_forgeries_fail_before_browser_or_publication(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            good = await host.call(BrowserReply([FileBytes("outputs/a.txt", b"a")]))
            foreign_context = BoundContext("actor-b", "chat-b", "assistant-b")
            foreign = await host.call(
                action="retry", operation_id=operation(good), context=foreign_context
            )
            assert foreign["incomplete"] is True and "issued_files" not in foreign
            foreign_file = await host.add_input("actor-b", "foreign.txt", b"not readable")
            cases: list[tuple[BoundContext, list[dict[str, JsonValue]]]] = [
                (host.context, [{"type": "file", "id": foreign_file}]),
                (BoundContext("actor-a", "chat-b", "assistant-b"), []),
                (BoundContext("actor-a", "chat-a", "assistant-b"), []),
            ]
            for context, files in cases:
                browser = BrowserReply([])
                assert (await host.call(browser, context=context, files=files))[
                    "incomplete"
                ] is True
                assert browser.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage",
    ["checkpoint_row", "output_row", "checkpoint_bytes", "output_bytes", "message", "rights"],
)
def test_lifecycle_retry_never_recreates_deleted_or_unavailable_results(
    database_case: DatabaseCase, host_factory: HostFactory, damage: str
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            result = await host.call(BrowserReply([FileBytes("outputs/a.txt", b"a")]))
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation(result))
            slot = receipt.checkpoint if damage.startswith("checkpoint") else receipt.outputs[0]
            if damage.endswith("row"):
                async with host.engine.begin() as connection:
                    await connection.execute(
                        delete(host.files).where(host.files.c.id == slot.file_id)
                    )
            elif damage.endswith("bytes"):
                async with host.engine.connect() as connection:
                    path = await connection.scalar(
                        select(host.files.c.path).where(host.files.c.id == slot.file_id)
                    )
                assert isinstance(path, str)
                await asyncio.to_thread(Path(path).unlink)
            elif damage == "message":
                async with host.engine.begin() as connection:
                    await connection.execute(
                        delete(host.messages).where(host.messages.c.id == "chat-a-assistant-a")
                    )
            else:
                host.write_allowed = False
            attempts = len(host.provider.uploads)
            if damage not in {"message", "rights"}:
                history = await host.call(action="status", operation_id=receipt.operation_id)
                assert history["availability"] == "not_rechecked"
                assert "issued_files" not in history
            retry = await host.call(action="retry", operation_id=receipt.operation_id)
            assert retry["incomplete"] is True and "issued_files" not in retry
            assert len(host.provider.uploads) == attempts

    asyncio.run(scenario())


def test_concurrent_provider_attempts_elect_one_immutable_file(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            host.provider.fail_attempts = {2}
            legacy = await host.seed_legacy([FileBytes("outputs/a.txt", b"complete issued bytes")])
            with pytest.raises(ValueError):
                await resume_publication(
                    OpenWebUIAdapter(), host.context, legacy.operation_id, Limits()
                )
            result: dict[str, JsonValue] = {"operation_id": legacy.operation_id}
            host.provider.fail_attempts.clear()
            host.provider.barrier = Barrier(2)
            host.provider.pause_attempt = 4
            tasks = [
                asyncio.create_task(host.call(action="retry", operation_id=operation(result)))
                for _ in range(2)
            ]
            stream = None
            try:
                assert await asyncio.to_thread(host.provider.paused.wait, 10)
                completed, _ = await asyncio.wait(
                    tasks, timeout=10, return_when=asyncio.FIRST_COMPLETED
                )
                assert completed and next(iter(completed)).result()["state"] == "published"
                adapter = OpenWebUIAdapter()
                receipt = await adapter.state.get(host.context, operation(result))
                async with host.engine.connect() as connection:
                    path = await connection.scalar(
                        select(host.files.c.path).where(
                            host.files.c.id == receipt.outputs[0].file_id
                        )
                    )
                assert isinstance(path, str)
                stream = await asyncio.to_thread(Path(path).open, "rb")
                prefix = await asyncio.to_thread(stream.read, 1)
                host.provider.release.set()
                replies = await asyncio.gather(*tasks)
                assert prefix + await asyncio.to_thread(stream.read) == b"complete issued bytes"
            finally:
                host.provider.release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                if stream:
                    await asyncio.to_thread(stream.close)
            assert all(reply["state"] == "published" for reply in replies)
            assert len(host.provider.uploads) == len(set(host.provider.uploads))
            assert len(await host.file_ids()) == 2
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation(result))
            assert (
                await adapter.verify_slot(host.context, receipt.outputs[0], 1024)
                == b"complete issued bytes"
            )

    asyncio.run(scenario())


def test_file_row_and_receipt_commit_marker_roll_back_together(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            adapter = OpenWebUIAdapter()
            await adapter.initialize()
            async with host.engine.begin() as connection:
                if database_case.schema:
                    qualified = f'"{database_case.schema}".'
                    await connection.execute(
                        text(
                            f"CREATE FUNCTION {qualified}synthetic_receipt_failure() "
                            "RETURNS trigger "
                            "LANGUAGE plpgsql AS $$ BEGIN "
                            "IF (NEW.receipt_json::jsonb->'checkpoint'->>'committed')::boolean "
                            "THEN RAISE EXCEPTION 'Synthetic receipt rejection'; "
                            "END IF; RETURN NEW; END $$"
                        )
                    )
                    await connection.execute(
                        text(
                            f"CREATE TRIGGER synthetic_receipt_failure BEFORE UPDATE ON "
                            f"{qualified}pyodide_file_saver_operation FOR EACH ROW "
                            f"EXECUTE FUNCTION {qualified}synthetic_receipt_failure()"
                        )
                    )
                else:
                    await connection.execute(
                        text(
                            "CREATE TRIGGER synthetic_receipt_failure BEFORE UPDATE ON "
                            "pyodide_file_saver_operation WHEN "
                            "json_extract(NEW.receipt_json, '$.checkpoint.committed') = 1 "
                            "BEGIN SELECT RAISE(ABORT, 'Synthetic receipt rejection'); END"
                        )
                    )
            browser = BrowserReply(
                [FileBytes("scripts/working.txt", b"received but not committed")]
            )
            result = await host.call(browser)
            assert result["incomplete"] is True
            receipt = await adapter.state.get(host.context, operation(result))
            assert receipt.state == "allocated" and not receipt.checkpoint.committed
            assert len(await host.file_ids()) == 1 and len(host.provider.uploads) == 2
            retry = await host.call(action="retry", operation_id=receipt.operation_id)
            assert retry["incomplete"] is True and browser.calls == 1
            assert len(host.provider.uploads) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
def test_unreceived_interrupted_work_never_replaces_acknowledged_head(
    database_case: DatabaseCase, host_factory: HostFactory, interrupt: str
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            prior = await host.call(BrowserReply([FileBytes("scripts/working.txt", b"saved")]))
            browser = BrowserReply([FileBytes("scripts/working.txt", b"not received")])
            browser.release = asyncio.Event()
            tool = Tools()
            tool.valves.callback_timeout_seconds = 1
            task = asyncio.create_task(host.call(browser, tool=tool))
            await asyncio.wait_for(browser.entered.wait(), timeout=10)
            if interrupt == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                assert result["incomplete"] is True
                receipt = result["receipt"]
                assert isinstance(receipt, dict) and receipt["workspace_saved"] is False
                assert receipt["execution_completed"] is False
            adapter = OpenWebUIAdapter()
            _, head = await adapter.state.allocate(host.context)
            assert head and head.operation_id == operation(prior)
            assert len(await host.file_ids()) == 2

    asyncio.run(scenario())


def test_empty_retained_files_use_receipt_backed_native_text_files(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [FileBytes("outputs/empty report.txt", b""), FileBytes("scripts/empty.py", b"")]
            result = await host.call(BrowserReply(files))
            assert result["state"] == "published", result
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation(result))
            assert receipt.checkpoint.committed and all(slot.committed for slot in receipt.blobs)
            assert all(slot.representation == "empty" for slot in receipt.blobs)
            assert len(host.provider.uploads) == 1  # Only the nonempty manifest.
            issued = result["issued_files"]
            assert isinstance(issued, list) and len(issued) == 1
            assert isinstance(issued[0], dict)
            assert str(issued[0]["download_url"]).endswith("/content/download?attachment=true")
            for slot in receipt.blobs:
                assert await adapter.verify_slot(host.context, slot, 8) == b""
            fresh = BrowserReply(files)
            assert (await host.call(fresh))["state"] == "published"
            assert fresh.restored == [files] or sorted(
                fresh.restored[0], key=lambda f: f.path
            ) == sorted(files, key=lambda f: f.path)
            assert len(host.provider.uploads) == 2  # Unchanged empties reused.
            assert (await host.call(action="retry", operation_id=operation(result)))[
                "state"
            ] == "published"

    asyncio.run(scenario())
