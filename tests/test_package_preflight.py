"""Public Pyodide loading boundary and source-free failure acknowledgement."""

from __future__ import annotations

import ast
import asyncio
import base64
import builtins
import sys
from collections.abc import Callable, Mapping
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from conftest import BrowserReply, DatabaseCase, HostHarness, open_host
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine

from pyodide_file_saver.browser_workspace import workspace_wrapper
from pyodide_file_saver.contracts import JSON_VALUE, Limits, json_text


@pytest.mark.parametrize("mode", ["ok", "callback", "exception", "syntax", "blocked"])
def test_vendor_loader_filters_imports_and_keeps_callbacks_alive(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    proxies: list[Any] = []

    class Proxy:
        def __init__(self, callback: Callable[[str], None]) -> None:
            self.callback = callback
            self.destroyed = False

        def __call__(self, value: str) -> None:
            assert not self.destroyed
            self.callback(value)

        def destroy(self) -> None:
            self.destroyed = True

    def create_proxy(callback: Callable[[str], None]) -> Proxy:
        proxy = Proxy(callback)
        proxies.append(proxy)
        return proxy

    async def load(source: str, options: Any) -> None:
        calls.append(source)
        assert options.checkIntegrity is True
        options.messageCallback("private-package-path")
        await asyncio.sleep(0)
        if mode == "callback":
            options.errorCallback("private-package-url")
        if mode == "exception":
            raise RuntimeError("private-package-url")

    # Only the external browser/Pyodide boundary is replaced. The production
    # generated Python wrapper executes normally, including its busy/finally guard.
    def run_js(_source: str) -> bool:
        return False

    modules: dict[str, dict[str, object]] = {
        "pyodide.code": {"run_js": run_js},
        "pyodide.ffi": {"create_proxy": create_proxy},
        "pyodide_js": {"loadPackagesFromImports": load},
        "js": {"Object": SimpleNamespace(new=SimpleNamespace)},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    runtime_values: dict[str, object] = {
        "_pyodide_file_saver_runtime_id": "synthetic-runtime",
        "_pyodide_file_saver_workspaces": {},
        "_pyodide_file_saver_busy": None,
    }
    for name, value in runtime_values.items():
        monkeypatch.setattr(builtins, name, value, raising=False)
    source = "from PIL import Image\nimport xlrd, subprocess\nfrom numpy.linalg import norm"
    if mode == "syntax":
        source = "import PIL\nif ("
    if mode == "blocked":
        source = "import subprocess\nfrom subprocess import run"
    config: dict[str, JsonValue] = {
        "operation_id": "synthetic-operation",
        "phase": "packages",
        "key": "synthetic",
        "head": "",
        "blocked_modules": ["subprocess"],
        "package_source": base64.b64encode(source.encode()).decode(),
        "limits": Limits().model_dump(),
    }
    wrapper, _ = workspace_wrapper(config)
    assert source not in wrapper and "import PIL" not in wrapper
    tree = ast.parse(wrapper)
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(tree.body[:-1], []), "<managed-wrapper>", "exec"), namespace)
    assert isinstance(tree.body[-1], ast.Expr)
    expression = ast.Expression(tree.body[-1].value)
    raw = asyncio.run(
        eval(
            compile(expression, "<managed-wrapper>", "eval", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT),
            namespace,
        )
    )
    result = JSON_VALUE.validate_json(raw)
    assert isinstance(result, dict)
    assert result.get("outcome", "ok") == (
        "dependency_error" if mode in {"callback", "exception"} else "ok"
    )
    assert calls == (
        [] if mode in {"syntax", "blocked"} else ["import PIL\nimport numpy\nimport xlrd"]
    )
    assert all(proxy.destroyed for proxy in proxies)
    assert "private-package" not in raw
    runtime_state: Mapping[str, Any] = vars(builtins)
    assert runtime_state.get("_pyodide_file_saver_busy") is None
    assert not any(name.startswith("_pfs_") for name in namespace)


class PackageFailure(BrowserReply):
    def __init__(self, lost: bool = False) -> None:
        super().__init__([])
        self.lost = lost

    async def __call__(self, payload: dict[str, JsonValue]) -> object:
        result = await super().__call__(payload)
        config = self.configs[-1]
        if config["phase"] == "packages":
            assert config["code"] == "" and config["package_source"]
            if self.lost:
                raise TimeoutError("private-package-url")
            parsed = JSON_VALUE.validate_python(result)
            assert isinstance(parsed, dict) and isinstance(parsed["result"], str)
            value = JSON_VALUE.validate_json(parsed["result"])
            assert isinstance(value, dict)
            value["outcome"] = "dependency_error"
            return {"result": json_text(value)}
        return result


@pytest.mark.integration
@pytest.mark.parametrize("lost", [False, True])
def test_failed_package_preparation_never_dispatches_source(
    database_case: DatabaseCase,
    host_factory: Callable[[AsyncEngine, str | None], HostHarness],
    lost: bool,
) -> None:
    async def scenario() -> None:
        async with open_host(database_case, host_factory) as host:
            browser = PackageFailure(lost)
            failed = await host.call(browser)
            assert failed["error_code"] == (
                "browser_callback_timeout" if lost else "dependency_unavailable"
            )
            assert browser.calls == 0 and not any(
                config["phase"] in {"execute", "restore"} for config in browser.configs
            )
            assert failed["execution_outcome"] == "not_started"
            assert "private-package" not in str(failed)
            corrected_browser = BrowserReply([])
            corrected = await host.call(corrected_browser)
            assert corrected_browser.calls == (0 if lost else 1)
            assert corrected["status"] == ("error" if lost else "success")

    asyncio.run(scenario())
