"""Generate browser-only Python; the backend only parses code and validates bytes."""

from __future__ import annotations

import ast
import json
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from .contracts import (
    JSON_VALUE,
    Envelope,
    FileBytes,
    Limits,
    SaverError,
    decode_files,
    encode_files,
    json_text,
    validate_files,
)

PRELOAD_IMPORTS = frozenset(
    {
        "requests",
        "bs4",
        "numpy",
        "pandas",
        "matplotlib",
        "seaborn",
        "sklearn",
        "scipy",
        "re",
        "sympy",
        "tiktoken",
        "pytz",
        "openpyxl",
        "pptx",
        "docx",
    }
)

# This text is executed by Pyodide, never by the backend or build process.
BROWSER_WRAPPER = r"""
async def __FUNCTION__():
    import builtins as bi
    import json as jsn
    cfg = jsn.loads(__CONFIG__)
    def response(outcome, stdout="", stderr="", files=None):
        return jsn.dumps({"version": 1, "operation_id": cfg["operation_id"],
            "nonce": cfg["nonce"], "outcome": outcome, "stdout": stdout,
            "stderr": stderr, "files": files or []}, ensure_ascii=True, separators=(",", ":"))
    if getattr(bi, "_pyodide_file_saver_busy", None) is not None:
        return response("busy", stderr="Another managed execution is still running")
    bi._pyodide_file_saver_busy = cfg["operation_id"]
    old_cwd = None
    old_import = bi.__import__
    try:
        import ast
        import base64
        import contextlib
        import inspect
        import io
        import os
        import stat
        import traceback
        root = "/tmp/pyodide-file-saver-" + cfg["operation_id"]
        old_cwd = os.getcwd()
        os.mkdir(root)
        os.mkdir(root + "/outputs")
        for item in cfg["restore"]:
            target = root + "/" + item["path"]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "xb") as stream:
                stream.write(base64.b64decode(item["data"], validate=True))
        os.chdir(root)
        class BoundedOutput(io.StringIO):
            def __init__(self):
                super().__init__()
                self.remaining = cfg["limits"]["max_text_bytes"]
            def write(self, value):
                kept = value.encode("utf-8", "replace")[:self.remaining]
                self.remaining -= len(kept)
                super().write(kept.decode("utf-8", "ignore"))
                return len(value)
        stdout, stderr = BoundedOutput(), BoundedOutput()
        def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
            if (name.split(".")[0] in cfg["blocked_modules"]
                    and (globals or {}).get("__name__") == "__main__"):
                raise ImportError("Direct import of this module is restricted by the host")
            return old_import(name, globals, locals, fromlist, level)
        success = True
        bi.__import__ = restricted_import
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    namespace = {"__name__": "__main__", "__builtins__": bi.__dict__}
                    compiled = compile(cfg["code"], "<managed-python>", "exec",
                        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    value = eval(compiled, namespace)
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    success = False
                    traceback.print_exc(file=stderr)
        finally:
            bi.__import__ = old_import
        snapshot = []
        total = 0
        if os.path.realpath(root) != root or not stat.S_ISDIR(os.lstat(root).st_mode):
            raise ValueError("Managed root was replaced")
        for directory, dirs, names in os.walk(root, followlinks=False):
            for name in sorted(dirs + names):
                full = os.path.join(directory, name)
                metadata = os.lstat(full)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("Links are not permitted in managed snapshots")
                if name in dirs:
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise ValueError("Invalid managed directory")
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Only regular managed files are supported")
                relative = os.path.relpath(full, root)
                if os.path.commonpath([root, os.path.realpath(full)]) != root:
                    raise ValueError("Managed path escaped its root")
                with open(full, "rb") as stream:
                    data = stream.read(cfg["limits"]["max_file_bytes"] + 1)
                total += len(data)
                if (len(data) > cfg["limits"]["max_file_bytes"]
                        or total > cfg["limits"]["max_total_bytes"]
                        or len(snapshot) >= cfg["limits"]["max_files"]):
                    raise ValueError("Managed snapshot exceeds configured limits")
                snapshot.append({"path": relative,
                    "data": base64.b64encode(data).decode("ascii")})
        result = response("ok" if success else "python_error",
            stdout.getvalue(), stderr.getvalue(), sorted(snapshot, key=lambda f: f["path"]))
        if len(result.encode("utf-8")) + 1024 > cfg["limits"]["max_envelope_bytes"]:
            return response("snapshot_error", stderr="Serialized snapshot exceeds envelope limit")
        return result
    except Exception:
        return response("snapshot_error", stderr="Managed restoration or snapshot failed")
    finally:
        bi.__import__ = old_import
        try:
            if old_cwd is not None:
                os.chdir(old_cwd)
        finally:
            bi._pyodide_file_saver_busy = None
await __FUNCTION__()
"""


def make_wrapper(
    operation_id: str,
    code: str,
    restore: list[FileBytes],
    limits: Limits,
    blocked_modules: list[str],
) -> tuple[str, str]:
    validate_files(restore, limits)
    if len(code.encode("utf-8")) > limits.max_envelope_bytes:
        raise SaverError("Code exceeds the transfer limit")
    nonce = str(uuid4())
    # Import hints are comments: the stock frontend's supported-package regexes
    # see them, but they neither run imports nor bypass host import restrictions.
    hints: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None  # Let browser Python report the error and preserve its workspace.
    if tree:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hints.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                hints.add(node.module.split(".")[0])
            elif isinstance(node, ast.Attribute) and node.attr in {
                "read_excel",
                "to_excel",
                "ExcelWriter",
                "ExcelFile",
            }:
                hints.add("openpyxl")
    config = json_text(
        {
            "operation_id": operation_id,
            "nonce": nonce,
            "code": code,
            "restore": encode_files(restore),
            "limits": limits.model_dump(),
            "blocked_modules": blocked_modules,
        }
    )
    function_name = "_pfs_" + operation_id.replace("-", "")
    wrapper = BROWSER_WRAPPER.replace("__FUNCTION__", function_name).replace(
        "__CONFIG__", repr(config)
    )
    wrapper = "\n".join(f"# import {name}" for name in sorted(hints & PRELOAD_IMPORTS)) + wrapper
    payload: dict[str, JsonValue] = {
        "type": "execute:python",
        "data": {"id": operation_id, "code": wrapper, "files": []},
    }
    if len(json_text(payload).encode("utf-8")) > limits.max_envelope_bytes:
        raise SaverError("Serialized restore and code exceed the transfer limit")
    return wrapper, nonce


def consume_callback(
    raw: object, operation_id: str, nonce: str, limits: Limits
) -> tuple[Envelope, list[FileBytes]]:
    try:
        value = JSON_VALUE.validate_python(raw)
        if len(json_text(value).encode("utf-8")) > limits.max_envelope_bytes:
            raise SaverError("Callback envelope size limit exceeded")
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, str):
            raise SaverError(
                "Browser did not return a managed snapshot; received bytes are not saved"
            )
        envelope = Envelope.model_validate_json(result)
        if envelope.operation_id != operation_id or envelope.nonce != nonce:
            raise SaverError("Browser snapshot belongs to a different operation")
        if any(
            len(text.encode("utf-8")) > limits.max_text_bytes
            for text in (envelope.stdout, envelope.stderr)
        ):
            raise SaverError("Captured output exceeds its configured limit")
        if envelope.outcome in {"busy", "snapshot_error"}:
            raise SaverError(
                "Browser is busy; no new checkpoint saved"
                if envelope.outcome == "busy"
                else "Browser snapshot failed; previous acknowledged head is unchanged"
            )
        return envelope, decode_files(envelope.files, limits)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise SaverError("Malformed browser snapshot; no checkpoint acknowledged") from error


def checkpoint_bytes(files: list[FileBytes]) -> bytes:
    return json_text({"version": 1, "files": encode_files(files)}).encode("utf-8")


def read_checkpoint(data: bytes, limits: Limits) -> list[FileBytes]:
    if len(data) > limits.max_envelope_bytes:
        raise SaverError("Saved checkpoint exceeds the transfer limit")
    try:
        value = JSON_VALUE.validate_json(data)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "files"}
            or value["version"] != 1
        ):
            raise SaverError("Invalid saved checkpoint format")
        envelope = Envelope.model_validate(
            {
                "version": 1,
                "operation_id": "",
                "nonce": "",
                "outcome": "ok",
                "stdout": "",
                "stderr": "",
                "files": value["files"],
            }
        )
        return decode_files(envelope.files, limits)
    except (ValidationError, ValueError, TypeError) as error:
        raise SaverError("Saved checkpoint is malformed") from error
