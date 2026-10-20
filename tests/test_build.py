"""The distributed single file must load independently of the source package."""

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.contracts import BoundContext, FileBytes, Limits
from pyodide_file_saver.openwebui_adapter import OpenWebUIAdapter
from pyodide_file_saver.tool import Tools


def load_bundle(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    root = Path(__file__).resolve().parents[1]
    specification = spec_from_file_location("synthetic_builder", root / "scripts" / "build_tool.py")
    assert specification and specification.loader
    builder = module_from_spec(specification)
    specification.loader.exec_module(builder)
    build = cast(Callable[[], str], builder.build)
    artifact = build()
    assert artifact == build() and "from ." not in artifact
    module = ModuleType("synthetic_bundled_tool")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # Load our own extension, never the browser wrapper or caller-provided Python.
    exec(compile(artifact, "<synthetic-bundled-tool>", "exec"), module.__dict__)
    return module


def test_bundled_tool_loads_and_exposes_concrete_valve_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_bundle(monkeypatch)
    tool = cast(type[Tools], module.Tools)()
    assert tool.valves.max_files == 64
    assert "max_total_bytes" in tool.Valves.model_json_schema()["properties"]
    # Match native settings POST strings, then its persisted integer JSON.
    settings = {
        "max_files": "64",
        "max_file_bytes": "4194304",
        "max_total_bytes": "7340032",
        "max_envelope_bytes": "14680064",
        "max_text_bytes": "16384",
        "callback_timeout_seconds": "55",
    }
    valves = tool.Valves.model_validate(settings)
    persisted = valves.model_dump(exclude_unset=True)
    assert persisted == {name: int(value) for name, value in settings.items()}
    assert all(type(value) is int for value in persisted.values())
    assert Limits.model_validate(persisted).max_file_bytes == 4194304


@pytest.mark.parametrize("field", list(Tools.Valves.model_fields))
@pytest.mark.parametrize(
    "value", [True, False, 1.0, 1.5, "1.0", "1e3", "1_000", "true", "", " 1", "1 ", "++1", "١"]
)
def test_valves_reject_non_integer_settings(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Tools.Valves.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_files", "0"),
        ("max_files", "257"),
        ("max_file_bytes", "0"),
        ("max_file_bytes", "16777216"),
        ("max_total_bytes", "-1"),
        ("max_total_bytes", "16777216"),
        ("max_envelope_bytes", "1023"),
        ("max_envelope_bytes", "16777216"),
        ("max_text_bytes", "0"),
        ("max_text_bytes", "65537"),
        ("callback_timeout_seconds", "0"),
        ("callback_timeout_seconds", "60"),
    ],
)
def test_valves_integer_strings_preserve_all_numeric_bounds(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Tools.Valves.model_validate({field: value})


def test_ui_conversion_does_not_relax_runtime_limits_or_unknown_settings() -> None:
    assert Tools.Valves.model_validate({"max_files": "+64"}).model_dump()["max_files"] == 64
    with pytest.raises(ValidationError):
        Tools.Valves.model_validate({"unknown": "1"})
    for field, value in Limits().model_dump().items():
        with pytest.raises(ValidationError):
            Limits.model_validate({field: str(value)})


@pytest.mark.parametrize(
    ("first", "second", "valid"),
    [
        ("from sqlalchemy import inspect", "import inspect", False),
        ("import inspect", "from sqlalchemy import inspect", False),
        ("import json as shared", "from pathlib import Path as shared", False),
        ("from pathlib import Path as shared", "from uuid import uuid4 as shared", False),
        ("import json", "import json as json", True),
        (
            "from sqlalchemy import inspect as inspect_sqlalchemy",
            "from sqlalchemy import inspect as inspect_sqlalchemy, select",
            True,
        ),
        ("import collections.abc", "import collections", True),
        ("from pathlib import *", "", False),
    ],
)
def test_build_cli_rejects_import_binding_collisions(
    tmp_path: Path, first: str, second: str, valid: bool
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "scripts" / "build_tool.py"
    script.parent.mkdir()
    shutil.copyfile(root / "scripts" / "build_tool.py", script)
    shutil.copyfile(root / "pyproject.toml", tmp_path / "pyproject.toml")
    source = tmp_path / "src" / "pyodide_file_saver"
    source.mkdir(parents=True)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "browser-shell.js").write_text("/* synthetic browser asset */")
    for name in ("browser_asset", "browser_workspace", "workspace_manifest", "workspace_execution"):
        (source / f"{name}.py").write_text("")
    for name, content in {
        "contracts": first,
        "workspace_state": second,
        "browser_execution": "",
        "openwebui_adapter": "",
        "tool": "",
    }.items():
        (source / f"{name}.py").write_text(content)
    result = subprocess.run(
        [sys.executable, str(script), "--check"], capture_output=True, text=True, check=False
    )
    destination = tmp_path / "dist" / "openwebui_pyodide_file_saver.py"
    if valid:
        assert result.returncode == 0 and destination.is_file(), result.stderr
    else:
        assert result.returncode != 0 and not destination.exists()
        assert (
            "Conflicting bundled import binding" in result.stderr
            or "Wildcard imports" in result.stderr
        )


@pytest.mark.integration
def test_generated_tool_initializes_publishes_and_restores(
    monkeypatch: pytest.MonkeyPatch,
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
) -> None:
    # Production classes come from the installed artifact, not source imports.
    module = load_bundle(monkeypatch)
    tool = cast(type[Tools], module.Tools)()
    adapter_type = cast(type[OpenWebUIAdapter], module.OpenWebUIAdapter)
    context_type = cast(type[BoundContext], module.BoundContext)
    limits_type = cast(type[Limits], module.Limits)

    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            files = [
                FileBytes("scripts/private.txt", b"private"),
                FileBytes("outputs/a.txt", b"issued"),
            ]
            browser = BrowserReply(files)
            result = await host.call(browser, tool=tool)
            assert result["state"] == "published" and browser.calls == 1
            operation_id = result["operation_id"]
            assert isinstance(operation_id, str)
            adapter = adapter_type()
            context = context_type("actor-a", "chat-a", "assistant-a")
            receipt = await adapter.state.get(context, operation_id)
            await adapter.verify_publication(context, receipt, limits_type())
            assert await adapter.verify_slot(context, receipt.outputs[0], 1024) == b"issued"
            async with host.engine.connect() as connection:
                assert (await connection.scalars(select(host.chat_files.c.file_id))).all() == [
                    receipt.outputs[0].file_id
                ]
                row = (
                    (
                        await connection.execute(
                            select(host.messages).where(host.messages.c.id == "chat-a-assistant-a")
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["content"] == "newer normalized text"
                assert row["output"] == [{"type": "message", "content": "preserved output"}]
            fresh = BrowserReply(files)
            again = await host.call(fresh, tool=cast(type[Tools], module.Tools)())
            assert again["state"] == "published" and again["issued_files"] == []
            assert fresh.calls == 1
            assert [{f.path: f.data for f in batch} for batch in fresh.restored] == [
                {f.path: f.data for f in files}
            ]

    asyncio.run(scenario())
