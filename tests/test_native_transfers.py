"""Native HTTP is an external boundary; reservations/election use real databases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

import pytest
from conftest import DatabaseCase, HostHarness, open_host
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import FileBytes, FileSlot, Limits, Receipt, SaverError, digest
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.tool import resume_publication
from pyodide_file_saver.workspace_manifest import retained_ready
from pyodide_file_saver.workspace_transfer import stage_native_snapshot


async def pending(host: HostHarness, files: list[FileBytes]) -> Receipt:
    adapter = OpenWebUIAdapter()
    await adapter.initialize()
    receipt, _ = await adapter.state.allocate(host.context)
    async with adapter.state.transaction() as session:
        receipt.execution_completed = receipt.python_ok = True
        await adapter.state.put(session, receipt)
    return await stage_native_snapshot(
        adapter,
        host.context,
        receipt.operation_id,
        [item.manifest() for item in files],
        [],
        [],
        [],
        Limits(),
    )


async def native_upload(
    host: HostHarness,
    receipt: Receipt,
    path: str,
    data: bytes,
    *,
    owner: str = "actor-a",
    key: str | None = None,
) -> str:
    """Emulate only native multipart/provider insertion, not Tool persistence."""
    transfer = next(item for item in receipt.native_transfers or [] if item.path == path)
    file_id = await host.add_input(owner, path.split("/")[-1], data)
    async with host.engine.begin() as connection:
        await connection.execute(
            update(host.files)
            .where(host.files.c.id == file_id)
            .values(
                created_at=1,
                # Deliberately forge the upstream hash: adoption must hash provider bytes.
                meta={
                    "file_hash": transfer.sha256,
                    "size": transfer.size,
                    "data": {
                        "pyodide_file_saver": {
                            "operation_id": receipt.operation_id,
                            "transfer_key": key or transfer.transfer_key,
                        }
                    },
                },
            )
        )
    return file_id


@pytest.mark.integration
def test_native_reservations_survive_restart_and_reject_changed_snapshot(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [FileBytes("outputs/a.bin", b"native bytes")]
            receipt = await pending(host, files)
            adapter = OpenWebUIAdapter()
            repeated = await stage_native_snapshot(
                adapter, host.context, receipt.operation_id, receipt.files, [], [], [], Limits()
            )
            assert repeated.native_transfers == receipt.native_transfers
            assert not receipt.blobs and not retained_ready(receipt)
            assert (
                await adapter.state.accept(host.context, receipt.operation_id)
            ).state == "allocated"
            with pytest.raises(SaverError, match="No complete committed"):
                await resume_publication(adapter, host.context, receipt.operation_id, Limits())
            with pytest.raises(SaverError, match="frozen reservation"):
                await stage_native_snapshot(
                    adapter,
                    host.context,
                    receipt.operation_id,
                    [FileBytes("outputs/a.bin", b"different").manifest()],
                    [],
                    [],
                    [],
                    Limits(),
                )
            assert not await host.file_ids()
            assert await adapter.state.head(host.context) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_lost_http_response_reconciles_and_publishes_without_browser(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [
                FileBytes("outputs/a.bin", b"native bytes"),
                FileBytes("scripts/recipe.py", b"print(1)"),
                FileBytes("outputs/empty.txt", b""),
            ]
            receipt = await pending(host, files)
            ids: dict[str, str] = {}
            for item in files:
                if item.data:
                    ids[item.path] = await native_upload(host, receipt, item.path, item.data)
            # Neither upload IDs nor a live worker are passed to retry.
            checkpoint_id = receipt.checkpoint.file_id
            saved = await resume_publication(
                OpenWebUIAdapter(), host.context, receipt.operation_id, Limits()
            )
            assert saved.state == "published" and saved.checkpoint.file_id == checkpoint_id
            assert {slot.file_id for slot in saved.blobs if slot.size} == set(ids.values())
            assert {slot.path for slot in saved.outputs} == {"outputs/a.bin", "outputs/empty.txt"}
            before = await host.file_ids()
            again = await resume_publication(
                OpenWebUIAdapter(), host.context, receipt.operation_id, Limits()
            )
            assert again == saved and await host.file_ids() == before
            async with host.engine.connect() as connection:
                links = list(await connection.scalars(select(host.chat_files.c.file_id)))
            assert set(links) == {slot.file_id for slot in saved.outputs}

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["owner", "key", "hash", "size"])
def test_native_candidate_is_independently_verified(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    damage: str,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            file = FileBytes("outputs/a.bin", b"expected")
            receipt = await pending(host, [file])
            data = b"wrong!!!" if damage == "hash" else b"short" if damage == "size" else file.data
            file_id = await native_upload(
                host,
                receipt,
                file.path,
                data,
                owner="actor-b" if damage == "owner" else "actor-a",
                key=str(uuid4()) if damage == "key" else None,
            )
            with pytest.raises(SaverError) as caught:
                await OpenWebUIAdapter().adopt_native_upload(
                    host.context, receipt.operation_id, file.path, file_id, Limits().max_file_bytes
                )
            assert (
                caught.value.error_code
                == {
                    "owner": "native_upload_owner_mismatch",
                    "key": "native_upload_key_mismatch",
                    "hash": "native_upload_bytes_mismatch",
                    "size": "native_upload_bytes_mismatch",
                }[damage]
            )
            saved = await OpenWebUIAdapter().state.get(host.context, receipt.operation_id)
            assert saved.blobs == [] and not saved.checkpoint.committed
            assert await OpenWebUIAdapter().state.head(host.context) is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_duplicate_native_uploads_elect_one_immutable_file(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            file = FileBytes("outputs/a.bin", b"native bytes")
            receipt = await pending(host, [file])
            candidates = [
                await native_upload(host, receipt, file.path, file.data) for _ in range(2)
            ]

            async def adopt(candidate: str) -> Receipt:
                return await OpenWebUIAdapter().adopt_native_upload(
                    host.context,
                    receipt.operation_id,
                    file.path,
                    candidate,
                    Limits().max_file_bytes,
                )

            first, second = await asyncio.gather(*(adopt(candidate) for candidate in candidates))
            assert len(first.blobs) == len(second.blobs) == 1
            winner = first.blobs[0].file_id
            assert second.blobs[0].file_id == winner and winner in candidates
            # The unacknowledged duplicate is preserved; retry never replaces the winner.
            saved = await resume_publication(
                OpenWebUIAdapter(), host.context, receipt.operation_id, Limits()
            )
            again = await adopt(next(candidate for candidate in candidates if candidate != winner))
            assert again.blobs == saved.blobs
            assert set(candidates).issubset(await host.file_ids())

    asyncio.run(scenario())


@pytest.mark.integration
def test_partial_native_uploads_cannot_commit_checkpoint(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [FileBytes("outputs/a.bin", b"a"), FileBytes("outputs/b.bin", b"b")]
            receipt = await pending(host, files)
            file_id = await native_upload(host, receipt, files[0].path, files[0].data)
            receipt = await OpenWebUIAdapter().adopt_native_upload(
                host.context, receipt.operation_id, files[0].path, file_id, Limits().max_file_bytes
            )
            assert not retained_ready(receipt)
            with pytest.raises(SaverError, match="No complete committed"):
                await resume_publication(
                    OpenWebUIAdapter(), host.context, receipt.operation_id, Limits()
                )
            assert await OpenWebUIAdapter().state.head(host.context) is None
            assert not (
                await OpenWebUIAdapter().state.get(host.context, receipt.operation_id)
            ).checkpoint.committed

    asyncio.run(scenario())


def test_legacy_receipts_default_to_legacy_transfer_semantics() -> None:
    receipt = Receipt(
        operation_id="old",
        actor_id="actor",
        chat_id="chat",
        message_id="message",
        base_revision=0,
        checkpoint=FileSlot(
            path="checkpoint.json", file_id="preserved", sha256=digest(b"checkpoint"), size=10
        ),
    )
    raw = receipt.model_dump()
    raw.pop("native_transfers")
    assert Receipt.model_validate(raw).native_transfers is None


@pytest.mark.integration
@pytest.mark.parametrize("loss", ["http", "callback"])
def test_binary_orchestration_recovers_lost_upload_without_source_replay(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    loss: str,
) -> None:
    from conftest import BrowserReply
    from pydantic import JsonValue

    from pyodide_file_saver.contracts import JSON_VALUE, json_text

    class NativeBrowser(BrowserReply):
        def __init__(self, host: HostHarness) -> None:
            # Native local storage prefixes a UUID; a long extension must not
            # overflow its filesystem component limit on the fallback path.
            super().__init__([FileBytes("outputs/a." + "x" * 220, b"native result")])
            self.host = host

        async def __call__(self, payload: dict[str, JsonValue]) -> object:
            raw = JSON_VALUE.validate_python(await super().__call__(payload))
            assert isinstance(raw, dict) and isinstance(raw["result"], str)
            reply = JSON_VALUE.validate_json(raw["result"])
            assert isinstance(reply, dict)
            config = self.configs[-1]
            reply["binary"] = True
            reply["files"] = []
            if config["phase"] == "upload":
                assert (
                    config["code"] == "" and not config.get("code_b64") and config["restore"] == []
                )
                filename = config["upload_filename"]
                assert isinstance(filename, str) and len(filename.encode()) <= 180
                assert filename != self.files[0].path.split("/")[-1]
                remaining = config["http_timeout_seconds"]
                assert isinstance(remaining, (float, int)) and 0 < remaining <= 120
                operation_id = config["operation_id"]
                assert isinstance(operation_id, str)
                receipt = await OpenWebUIAdapter().state.get(self.host.context, operation_id)
                await native_upload(self.host, receipt, self.files[0].path, self.files[0].data)
                if loss == "callback":
                    return {"error": "Client session disconnected."}
                reply.update(
                    outcome="snapshot_error",
                    cause={
                        "code": "transfer_http",
                        "exception_type": "RuntimeError",
                        "http_status": 0,
                    },
                )
            return {"result": json_text(reply)}

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = NativeBrowser(host)
            failed = await host.call(browser)
            assert failed["status"] == "error" and failed["execution_outcome"] == "ok"
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            saved = await OpenWebUIAdapter().state.get(host.context, operation_id)
            assert saved.native_transfers and not saved.blobs and not saved.checkpoint.committed
            retried = await host.call(None, action="retry", operation_id=operation_id)
            assert retried["state"] == "published", retried
            assert len([config for config in browser.configs if config["phase"] == "execute"]) == 1
            assert len([config for config in browser.configs if config["phase"] == "upload"]) == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_pending_legacy_reservations_keep_ids_and_inline_recovery(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    from conftest import BrowserReply

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = BrowserReply(
                [FileBytes("outputs/a.txt", b"a"), FileBytes("outputs/b.txt", b"b")]
            )
            host.provider.fail_attempts = {2}
            failed = await host.call(browser)
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            before = await OpenWebUIAdapter().state.get(host.context, operation_id)
            assert before.native_transfers is None and len(before.blobs) == 2
            assert sum(slot.committed for slot in before.blobs) == 1
            host.provider.fail_attempts.clear()
            recovered = await host.call(browser, action="recover", operation_id=operation_id)
            assert recovered["state"] == "published", recovered
            after = await OpenWebUIAdapter().state.get(host.context, operation_id)
            assert [slot.file_id for slot in after.blobs] == [slot.file_id for slot in before.blobs]
            assert after.native_transfers is None
            config = next(config for config in browser.configs if config["phase"] == "recover")
            assert config["binary_transport"] is False and config["code"] == ""

    asyncio.run(scenario())


@pytest.mark.integration
def test_aggregate_input_budget_counts_inherited_and_new_references(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    from conftest import BrowserReply

    from pyodide_file_saver.tool import Tools

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            first = await host.add_input("actor-a", "a.bin", b"aa")
            second = await host.add_input("actor-a", "b.bin", b"bb")
            tool = Tools()
            tool.valves.max_input_bytes = 3
            browser = BrowserReply([])
            result = await host.call(browser, tool=tool, files=[{"id": first}])
            assert result["state"] == "published"
            before = len(browser.configs)
            failed = await host.call(browser, tool=tool, files=[{"id": second}])
            assert failed["error_code"] == "input_total_too_large", failed
            assert failed["execution_outcome"] == "not_started"
            assert not any(config["phase"] == "execute" for config in browser.configs[before:])
            details = failed["error_details"]
            assert isinstance(details, dict)
            assert details["size_bytes"] == 4 and details["limit_bytes"] == 3

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("cause", ["http", "hash"])
def test_failed_cold_binary_restore_never_dispatches_source(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    cause: str,
) -> None:
    from conftest import BrowserReply
    from pydantic import JsonValue

    from pyodide_file_saver.contracts import JSON_VALUE, json_text

    class FailedRestore(BrowserReply):
        async def __call__(self, payload: dict[str, JsonValue]) -> object:
            raw = JSON_VALUE.validate_python(await super().__call__(payload))
            assert isinstance(raw, dict) and isinstance(raw["result"], str)
            reply = JSON_VALUE.validate_json(raw["result"])
            assert isinstance(reply, dict)
            if reply["phase"] == "prepare":
                reply["binary"] = True
            elif reply["phase"] == "restore":
                assert self.configs[-1]["native_restore"]
                reply.update(
                    outcome="snapshot_error",
                    cause={
                        "code": "transfer_http" if cause == "http" else "transfer_changed",
                        "http_status": 404 if cause == "http" else 0,
                    },
                )
            return {"result": json_text(reply)}

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            original = await host.call(BrowserReply([FileBytes("scripts/required.py", b"value=1")]))
            browser = FailedRestore([])
            failed = await host.call(browser)
            assert failed["status"] == "error" and failed["execution_outcome"] == "not_started"
            assert failed["error_code"] == "browser_restore_failed"
            plan = failed["recovery_plan"]
            assert isinstance(plan, dict) and plan["next_step"] == "restore_required_files"
            operation_id = failed["operation_id"]
            assert isinstance(operation_id, str)
            assert not any(config["phase"] == "execute" for config in browser.configs)
            adapter = OpenWebUIAdapter()
            head = await adapter.state.head(host.context)
            assert head and head.operation_id == original["operation_id"]
            retry = await host.call(None, action="retry", operation_id=operation_id)
            assert retry["status"] == "error" and retry["execution_outcome"] == "not_started"
            diagnostic = await adapter.state.diagnostics(host.context, operation_id)
            assert diagnostic.first_failure_code == "browser_restore_failed"
            assert diagnostic.first_failure_phase == "restore"

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["restore", "upload"])
def test_aggregate_transfer_deadline_preserves_execution_facts(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    phase: str,
) -> None:
    from conftest import BrowserReply
    from pydantic import JsonValue

    from pyodide_file_saver.contracts import JSON_VALUE, json_text
    from pyodide_file_saver.tool import Tools

    class SlowTransfer(BrowserReply):
        async def __call__(self, payload: dict[str, JsonValue]) -> object:
            raw = JSON_VALUE.validate_python(await super().__call__(payload))
            assert isinstance(raw, dict) and isinstance(raw["result"], str)
            reply = JSON_VALUE.validate_json(raw["result"])
            assert isinstance(reply, dict)
            reply["binary"], reply["files"] = True, []
            if reply["phase"] == phase:
                remaining = self.configs[-1]["http_timeout_seconds"]
                assert isinstance(remaining, (float, int)) and 0 < remaining <= 1
                await asyncio.sleep(5)
            return {"result": json_text(reply)}

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [FileBytes("outputs/a.txt", b"a")]
            if phase == "restore":
                assert (await host.call(BrowserReply(files)))["state"] == "published"
            tool = Tools()
            tool.valves.transfer_timeout_seconds = 1
            browser = SlowTransfer(files)
            failed = await host.call(browser, tool=tool)
            assert failed["error_code"] == "transfer_timeout", failed
            assert failed["phase"] == ("restore" if phase == "restore" else "storage")
            cause = failed["cause"]
            assert isinstance(cause, dict) and cause["code"] == "transfer_timeout"
            assert cause["configured_wait_ms"] == 1000
            assert isinstance(cause["elapsed_ms"], int) and cause["elapsed_ms"] >= 900
            assert failed["execution_outcome"] == ("not_started" if phase == "restore" else "ok")
            assert bool([c for c in browser.configs if c["phase"] == "execute"]) == (
                phase == "upload"
            )

    asyncio.run(scenario())


def test_transfer_deadline_preserves_external_cancellation_and_nested_timeouts() -> None:
    from pyodide_file_saver.contracts import transfer_deadline

    async def scenario() -> None:
        entered = asyncio.Event()

        async def transfer() -> None:
            async with transfer_deadline(30, "storage"):
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(transfer())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(TimeoutError, match="external provider timeout"):
            async with transfer_deadline(30, "storage"):
                raise TimeoutError("external provider timeout")

    asyncio.run(scenario())
