"""The distributed single file must load independently of the source package."""

import sys
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from pyodide_file_saver.tool import Tools


def test_bundled_tool_loads_and_exposes_concrete_valve_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    tool = cast(type[Tools], module.Tools)()
    assert tool.valves.max_files == 64
    assert "max_total_bytes" in tool.Valves.model_json_schema()["properties"]
