"""Attachment selection narrows new inputs without granting or dropping access."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast, get_type_hints

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import SaverError
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.tool import Tools
from pyodide_file_saver.tool_results import select_input_attachments, validate_invocation


def test_native_schema_exposes_optional_string_array() -> None:
    schema = TypeAdapter[object](get_type_hints(Tools.execute_code)["input_file_ids"]).json_schema()
    assert schema["anyOf"] == [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}]
    assert validate_invocation("print(1)", "execute", "", "python").input_file_ids is None
    assert validate_invocation("print(1)", "execute", "", "python", []).input_file_ids == []


@pytest.mark.parametrize("selection", ["private-value", ["bad/private"], ["a", "a"], [1]])
def test_invalid_selection_has_safe_actionable_details(selection: object) -> None:
    with pytest.raises(SaverError) as caught:
        validate_invocation("print(1)", "execute", "", "python", selection)
    assert caught.value.error_code == "invalid_input_selection"
    assert caught.value.details["field"] == "input_file_ids"
    assert "private-value" not in str(caught.value.details)


def test_selection_admits_only_current_injected_attachments() -> None:
    attachments: list[dict[str, JsonValue]] = [{"id": "a"}, {"id": "b", "type": "file"}]
    assert select_input_attachments(attachments, None) == attachments
    assert select_input_attachments(attachments, []) == []
    assert select_input_attachments(attachments, ["b"]) == attachments[1:]
    with pytest.raises(SaverError, match="current attachments"):
        select_input_attachments(attachments, ["foreign-id"])
    for action in ("status", "retry", "recover"):
        with pytest.raises(SaverError) as caught:
            validate_invocation("", action, "", "python", [])
        assert caught.value.error_code == "unexpected_input_selection"


@pytest.mark.integration
@pytest.mark.parametrize("fault", ["access", "missing", "unselected"])
def test_subset_corrects_new_input_refusal_without_reconnecting(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    fault: str,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            allowed = await host.add_input(host.context.actor_id, "allowed.txt", b"allowed")
            denied = (
                await host.add_input("actor-b", "private.txt", b"private")
                if fault == "access"
                else "missing-id"
            )
            files: list[dict[str, JsonValue]] = [{"id": allowed}, {"id": denied}]
            browser = BrowserReply([])
            failed = await host.call(
                browser,
                files=files if fault != "unselected" else [{"id": allowed}],
                input_file_ids=[denied] if fault == "unselected" else None,
            )
            assert browser.calls == 0 and failed["status"] == "error"
            if fault == "unselected":
                assert failed["error_code"] == "invalid_input_selection"
                assert "operation_id" not in failed
            else:
                details = failed["error_details"]
                assert isinstance(details, dict) and details["input_origin"] == "new"
                plan = failed["recovery_plan"]
                assert isinstance(plan, dict) and plan["next_step"] == "correct_inputs"
            corrected = await host.call(browser, files=files, input_file_ids=[allowed])
            assert corrected["status"] == "success" and browser.calls == 1
            adapter = OpenWebUIAdapter()
            receipt = await adapter.state.get(host.context, cast(str, corrected["operation_id"]))
            assert [item.file_id for item in receipt.inputs] == [allowed]

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("policy", [1, "unrecognized"])
def test_status_reports_configured_and_effective_upload_limits_without_failing(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    policy: object,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            host.config["rag.file.max_size"] = policy
            status = await host.call(action="status")
            assert status["status"] == "success"
            limits = status["limits"]
            assert isinstance(limits, dict) and isinstance(limits["configured"], dict)
            assert limits["configured"]["max_file_bytes"]
            assert limits["effective_upload_max_file_bytes"] == (
                1024 * 1024 if policy == 1 else None
            )
            assert limits["upload_policy_status"] == ("checked" if policy == 1 else "unavailable")

    asyncio.run(scenario())


@pytest.mark.integration
def test_lowered_input_limit_keeps_inherited_reference_and_explains_policy(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            file_id = await host.add_input(host.context.actor_id, "retained.txt", b"12")
            first = await host.call(BrowserReply([]), files=[{"id": file_id}])
            assert first["status"] == "success"
            tool = Tools()
            tool.valves.max_file_bytes = 1
            browser = BrowserReply([])
            failed = await host.call(browser, tool=tool, input_file_ids=[])
            assert failed["error_code"] == "input_too_large" and browser.calls == 0
            assert failed["error_details"] == {
                "file_id": file_id,
                "input_origin": "inherited",
                "limit_bytes": 1,
                "size_bytes": 2,
            }
            plan = failed["recovery_plan"]
            assert isinstance(plan, dict)
            assert plan["next_step"] == "restore_required_input_access"
            assert "reconnect" in str(plan["instructions"])
            assert "input_file_ids cannot remove" in str(plan["instructions"])
            # An empty selection cannot discard acknowledged data or clear its fence.
            repeated = await host.call(browser, input_file_ids=[])
            assert repeated["status"] == "error" and browser.calls == 0

    asyncio.run(scenario())
