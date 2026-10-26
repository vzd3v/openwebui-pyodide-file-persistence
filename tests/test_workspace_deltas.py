"""V2 receipts reuse immutable blobs and keep transient state out of persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import JSON_VALUE, FileBytes, Limits, json_text
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.workspace_manifest import parse_manifest

pytestmark = pytest.mark.integration
HostFactory = Callable[[AsyncEngine, str | None], HostHarness]


class DeltaBrowser(BrowserReply):
    def __init__(self, files: list[FileBytes], outcome: str = "ok", *, warm: bool = False):
        super().__init__(files, outcome)
        self.warm = warm
        self.omit_changes = False
        self.transfers: list[int] = []

    async def __call__(self, payload: dict[str, JsonValue]) -> object:
        self.transfers.append(len(json_text(payload).encode()))
        raw = JSON_VALUE.validate_python(await super().__call__(payload))
        assert isinstance(raw, dict) and isinstance(raw["result"], str)
        reply = JSON_VALUE.validate_json(raw["result"])
        assert isinstance(reply, dict)
        if reply.get("phase") == "prepare":
            reply["warm"] = self.warm
        elif reply.get("phase") in {"execute", "snapshot", "recover"}:
            reply.update(
                version=2,
                manifest=[]
                if reply["phase"] == "execute"
                else [file.manifest().model_dump() for file in self.files],
                exit_code=0 if self.outcome == "ok" else 7,
            )
            reply["outcome"] = "ok" if self.outcome == "ok" else "execution_error"
            # Returning inherited bytes would hide a transport regression. Read the
            # actual server baseline from the generated public request boundary.
            import ast

            data = payload["data"]
            assert isinstance(data, dict) and isinstance(data["code"], str)
            config: dict[str, JsonValue] = {}
            for node in ast.walk(ast.parse(data["code"])):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "cfg" for t in node.targets
                ):
                    assert isinstance(node.value, ast.Call) and isinstance(
                        node.value.args[0], ast.Constant
                    )
                    assert isinstance(node.value.args[0].value, str)
                    value = JSON_VALUE.validate_json(node.value.args[0].value)
                    assert isinstance(value, dict)
                    config = value
            baseline = config["baseline"]
            assert isinstance(baseline, list)
            unchanged = {
                item["path"]: item["sha256"] for item in baseline if isinstance(item, dict)
            }
            files = reply["files"]
            assert isinstance(files, list)
            reply["files"] = (
                []
                if self.omit_changes
                else [
                    item
                    for item in files
                    if isinstance(item, dict)
                    and unchanged.get(item["path"])
                    != next(
                        file.manifest().sha256 for file in self.files if file.path == item["path"]
                    )
                ]
            )
        return {"result": json_text(reply)}


def op(result: dict[str, JsonValue]) -> str:
    value = result["operation_id"]
    assert isinstance(value, str)
    return value


def test_changed_only_blobs_reused_for_publication_and_retry(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            private = FileBytes("scripts/private.bin", b"x" * 1024)
            first = await host.call(
                DeltaBrowser([private, FileBytes("outputs/result.txt", b"one")])
            )
            assert first.get("state") == "published", first
            assert first["browser_workspace"] == {
                "mode": "new",
                "base_operation_id": None,
                "base_revision": 0,
            }
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, op(first))
            assert receipt.checkpoint_version == 2
            assert receipt.outputs[0].file_id == next(
                slot.file_id for slot in receipt.blobs if slot.path.startswith("outputs/")
            )
            assert len(host.provider.uploads) == 3  # Two blobs and their small manifest.
            second = await host.call(
                DeltaBrowser([private, FileBytes("outputs/result.txt", b"two")], warm=True)
            )
            assert second.get("state") == "published", second
            assert second["browser_workspace"] == {
                "mode": "warm",
                "base_operation_id": op(first),
                "base_revision": 1,
            }
            saved = await adapter.state.get(host.context, op(second))
            assert len(host.provider.uploads) == 5  # Changed output and small manifest only.
            assert saved.blobs[
                1 if saved.blobs[0].path.startswith("outputs/") else 0
            ].file_id == next(
                slot.file_id for slot in receipt.blobs if slot.path.startswith("scripts/")
            )
            data = await adapter.verify_slot(host.context, saved.checkpoint, 100000)
            parse_manifest(
                data,
                saved,
                Limits(),
            )
            assert b'"data":' not in data and b"private.bin" in data
            retried = await host.call(action="retry", operation_id=op(second))
            assert retried.get("state") == "published" and len(host.provider.uploads) == 5
            assert retried["browser_workspace"] == second["browser_workspace"]
            cold = await host.call(DeltaBrowser([private, FileBytes("outputs/result.txt", b"two")]))
            assert cold["browser_workspace"] == {
                "mode": "restored",
                "base_operation_id": op(second),
                "base_revision": 2,
            }

    asyncio.run(scenario())


def test_native_inputs_persist_without_copy_and_reauthorize_when_not_reattached(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            input_id = await host.add_input("actor-a", "input.txt", b"native bytes")
            first = await host.call(DeltaBrowser([]), files=[{"id": input_id}])
            assert first.get("state") == "published", first
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, op(first))
            assert len(receipt.inputs) == 1 and receipt.inputs[0].file_id == input_id
            assert len(host.provider.uploads) == 2  # Native upload plus manifest, no input clone.
            async with host.engine.begin() as connection:
                await connection.execute(
                    update(host.files)
                    .where(host.files.c.id == input_id)
                    .values(filename="renamed.txt")
                )
            second = await host.call(DeltaBrowser([], warm=True))
            assert second.get("state") == "published", second
            saved = await adapter.state.get(host.context, op(second))
            assert saved.inputs == receipt.inputs  # Stable acknowledged alias despite rename.
            async with host.engine.begin() as connection:
                await connection.execute(delete(host.files).where(host.files.c.id == input_id))
            browser = DeltaBrowser([], warm=True)
            failed = await host.call(browser)
            assert failed["incomplete"] is True and browser.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing_delta", "provider", "nonzero"])
def test_failed_delta_never_issues_or_advances_incomplete_head(
    database_case: DatabaseCase, host_factory: HostFactory, fault: str
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = DeltaBrowser(
                [FileBytes("outputs/result.txt", b"private on failure")],
                "error" if fault == "nonzero" else "ok",
            )
            browser.omit_changes = fault == "missing_delta"
            if fault == "provider":
                host.provider.fail_attempts = {1}
            failed = await host.call(browser)
            assert failed["incomplete"] is True and "issued_files" not in failed
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, op(failed))
            if fault == "nonzero":
                assert (
                    receipt.state == "accepted"
                    and receipt.exit_code == 7
                    and receipt.blobs[0].committed
                )
            else:
                assert receipt.state == "allocated" and not receipt.checkpoint.committed
            if fault == "provider":
                assert failed["stdout"] == "synthetic output"
                assert failed["stderr"] == ""
                assert failed["execution_outcome"] == "ok" and failed["exit_code"] == 0
                assert "recover" in str(failed["workspace_context"])
            async with host.engine.connect() as connection:
                assert list(await connection.scalars(select(host.chat_files.c.file_id))) == []

    asyncio.run(scenario())


def test_cold_inputs_use_separate_bounded_requests(
    database_case: DatabaseCase, host_factory: HostFactory
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            ids = [
                await host.add_input(
                    "actor-a", f"large-{n}.bin", bytes([n + 1]) * (6 * 1024 * 1024)
                )
                for n in range(2)
            ]
            browser = DeltaBrowser([])
            result = await host.call(browser, files=[{"id": file_id} for file_id in ids])
            assert result.get("state") == "published", result
            assert max(browser.transfers) < 15 * 1024 * 1024
            assert len(browser.restored[0]) == 2
            assert len(host.provider.uploads) == 3  # Two native inputs and manifest.

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["work", "scripts", "inputs"])
def test_legacy_reserved_file_preserves_head_and_supports_retry(
    database_case: DatabaseCase, host_factory: HostFactory, path: str
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            legacy = await host.seed_legacy([FileBytes(path, b"legacy bytes")])
            from pyodide_file_saver.tool import resume_publication

            await resume_publication(
                OpenWebUIAdapter(), host.context, legacy.operation_id, Limits()
            )
            browser = DeltaBrowser([])
            result = await host.call(browser)
            assert result["incomplete"] is True and "newly reserved directory" in str(
                result["error"]
            )
            assert browser.calls == 0
            adapter = OpenWebUIAdapter()
            _, head = await adapter.state.allocate(host.context)
            assert head and head.operation_id == legacy.operation_id
            assert (await host.call(action="retry", operation_id=legacy.operation_id))[
                "state"
            ] == "published"

    asyncio.run(scenario())
