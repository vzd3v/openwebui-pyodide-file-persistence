"""Exact empty files require durable provenance and retain native access controls."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from pyodide_file_saver.contracts import BoundContext, FileBytes, Limits, SaverError, markdown_link
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter, json_object
from pyodide_file_saver.tool import resume_publication


@pytest.mark.parametrize("name", ["html", "empty.txt", "Отчёт [draft].txt"])
def test_empty_link_uses_nonreserved_native_route_component(name: str) -> None:
    assert markdown_link(name, "synthetic-id", "empty").endswith(
        "/api/v1/files/synthetic-id/content/download?attachment=true)"
    )


@pytest.mark.integration
def test_legacy_empty_output_can_publish_and_upgrade_without_replacing_checkpoint(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [FileBytes("outputs/empty.txt", b""), FileBytes("scripts/empty.py", b"")]
            legacy = await host.seed_legacy(files)
            adapter = OpenWebUIAdapter()
            original = await adapter.verify_slot(host.context, legacy.checkpoint, 10000)
            published = await resume_publication(
                adapter, host.context, legacy.operation_id, Limits()
            )
            assert published.state == "published" and published.outputs[0].representation == "empty"
            assert await adapter.verify_slot(host.context, published.outputs[0], 8) == b""
            upgraded = await host.call(BrowserReply(files))
            assert upgraded["state"] == "published", upgraded
            assert await adapter.verify_slot(host.context, legacy.checkpoint, 10000) == original

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    "damage", ["marker", "hash", "content", "path", "null_path", "receipt", "deleted"]
)
def test_empty_file_provenance_cannot_mask_missing_or_corrupt_native_bytes(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    damage: str,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            result = await host.call(BrowserReply([FileBytes("outputs/html", b"")]))
            assert result["state"] == "published", result
            operation_id = result["operation_id"]
            assert isinstance(operation_id, str)
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation_id)
            slot = receipt.outputs[0]
            assert await adapter.read_file(host.context, slot.file_id, 8) == b""
            foreign = BoundContext("actor-b", "chat-b", "assistant-b")
            with pytest.raises(SaverError, match="not readable"):
                await adapter.read_file(foreign, slot.file_id, 8)
            async with host.engine.begin() as connection:
                if damage == "deleted":
                    await connection.execute(
                        delete(host.files).where(host.files.c.id == slot.file_id)
                    )
                elif damage == "receipt":
                    await connection.execute(
                        delete(adapter.state.operation).where(
                            adapter.state.operation.c.operation_id == operation_id
                        )
                    )
                else:
                    row = (
                        (
                            await connection.execute(
                                select(host.files).where(host.files.c.id == slot.file_id)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    values: dict[str, object]
                    if damage == "marker":
                        meta = json_object(row["meta"])
                        del meta["pyodide_file_saver_representation"]
                        values = {"meta": meta}
                    else:
                        if damage == "hash":
                            values = {"hash": "wrong"}
                        elif damage == "content":
                            values = {"data": {"content": "nonempty"}}
                        elif damage == "null_path":
                            values = {"path": None}
                        else:
                            values = {"path": "/missing/provider-object"}
                    await connection.execute(
                        update(host.files).where(host.files.c.id == slot.file_id).values(**values)
                    )
            with pytest.raises(SaverError):
                await adapter.read_file(host.context, slot.file_id, 8)
            with pytest.raises(SaverError):
                await adapter.authorize_file(host.context, slot.file_id)
            assert len(host.provider.uploads) == 1  # Empty bytes never reached the provider.

    asyncio.run(scenario())


@pytest.mark.integration
def test_authorized_empty_input_uses_origin_receipt_across_actor_workspaces(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            saved = await host.call(BrowserReply([FileBytes("outputs/empty.txt", b"")]))
            operation_id = saved["operation_id"]
            assert isinstance(operation_id, str)
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation_id)
            file_id = receipt.outputs[0].file_id
            granted = True

            async def access(
                file_id: str, access_type: str, user: object, db: AsyncSession | None = None
            ) -> bool:
                return granted and file_id == receipt.outputs[0].file_id

            monkeypatch.setattr(
                sys.modules["open_webui.utils.access_control.files"], "has_access_to_file", access
            )
            recipient = BoundContext("actor-b", "chat-b", "assistant-b")
            browser = BrowserReply([])
            imported = await host.call(browser, context=recipient, files=[{"id": file_id}])
            assert imported["state"] == "published", imported
            assert browser.restored == [[FileBytes(f"inputs/{file_id}/empty.txt", b"")]]
            assert len(host.provider.uploads) == 2  # Two manifests; no copied input blob.
            imported_id = imported["operation_id"]
            assert isinstance(imported_id, str)
            imported_receipt = await OpenWebUIAdapter().state.get(recipient, imported_id)
            assert imported_receipt.inputs[0].file_id == file_id
            granted = False
            denied = BrowserReply([])
            result: dict[str, JsonValue] = await host.call(denied, context=recipient)
            assert result["incomplete"] is True and denied.calls == 0

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["s3", "gcs", "azure", "unknown", None])
def test_unsupported_empty_lifecycle_fails_before_creating_a_native_empty_file(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    monkeypatch: pytest.MonkeyPatch,
    provider: str | None,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            module = sys.modules["open_webui.storage.provider"]
            if provider is None:
                monkeypatch.delattr(module, "STORAGE_PROVIDER")
            else:
                monkeypatch.setattr(module, "STORAGE_PROVIDER", provider)
            result = await host.call(BrowserReply([FileBytes("outputs/empty.txt", b"")]))
            assert result["error_code"] == "empty_files_require_local_storage"
            assert result["execution_outcome"] == "ok" and result["stdout"] == "synthetic output"
            assert "issued_files" not in result and not await host.file_ids()
            # The limitation concerns creation, not previously saved empty File reads.
            monkeypatch.setattr(module, "STORAGE_PROVIDER", "local", raising=False)
            saved = await host.call(BrowserReply([FileBytes("outputs/empty.txt", b"")]))
            assert saved["state"] == "published", saved
            operation_id = saved["operation_id"]
            assert isinstance(operation_id, str)
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, operation_id)
            monkeypatch.setattr(module, "STORAGE_PROVIDER", provider)
            assert await adapter.verify_slot(host.context, receipt.outputs[0], 8) == b""

    asyncio.run(scenario())
