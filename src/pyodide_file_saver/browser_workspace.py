"""Public-Pyodide browser protocol: warm scratch plus bounded retained deltas."""

from __future__ import annotations

import base64
from uuid import uuid4

from pydantic import Field, JsonValue

from .browser_execution import import_hints
from .contracts import (
    JSON_VALUE,
    BrowserFailure,
    FailureCause,
    Limits,
    ManifestEntry,
    SaverError,
    StrictModel,
    TransferFile,
    json_text,
)


class WorkspaceReply(StrictModel):
    version: int = 2
    operation_id: str
    nonce: str
    phase: str
    runtime_id: str = ""
    warm: bool = False
    shell_ready: bool = False
    outcome: str = "ok"
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    execution_completed: bool = False
    cause: FailureCause | None = None
    manifest: list[ManifestEntry] = Field(default_factory=list)
    files: list[TransferFile] = Field(default_factory=list)
    binary: bool = False
    uploaded_file_id: str | None = None


WORKSPACE_BROWSER = r"""
async def __FUNCTION__():
    import builtins as bi
    import json as jsn
    cfg = jsn.loads(__CONFIG__)
    import os, stat, base64, hashlib, io, contextlib, traceback, ast, inspect, sys
    from uuid import uuid4
    import asyncio
    if cfg.get("code_b64"):
        cfg["code"] = base64.b64decode(cfg.pop("code_b64"), validate=True).decode("utf-8")
    class WorkspaceInvariantError(ValueError):
        def __init__(self, code, http_status=0):
            self.code = code
            self.http_status = http_status
            super().__init__(code)
    runtime_id = getattr(bi, "_pyodide_file_saver_runtime_id", None)
    if runtime_id is None:
        runtime_id = str(uuid4())
        bi._pyodide_file_saver_runtime_id = runtime_id
    cache = getattr(bi, "_pyodide_file_saver_workspaces", {})
    bi._pyodide_file_saver_workspaces = cache
    state = cache.get(cfg["key"])
    warm = bool(state and state["head"] == cfg["head"] and os.path.isdir(state["root"]))
    def response(**values):
        body = dict(version=2, operation_id=cfg["operation_id"],
            nonce=cfg["nonce"], phase=cfg["phase"], runtime_id=runtime_id)
        body.update(values)
        text = jsn.dumps(body, ensure_ascii=True, separators=(",", ":"))
        if len(text.encode()) + 1024 > cfg["limits"]["max_envelope_bytes"]:
            raise WorkspaceInvariantError("callback_limit")
        return text
    if cfg["phase"] == "probe":
        return response()
    if cfg.get("runtime_id") and cfg["runtime_id"] != runtime_id:
        return response(outcome="runtime_changed")
    from pyodide.code import run_js
    shell_ready = bool(run_js("Boolean(globalThis.__pyodideFileSaverShell && "
        "globalThis.__pyodideFileSaverShell.version === 'just-bash@3.6.0/workspace@2')"))
    shell_ready = shell_ready and (getattr(bi, "_pyodide_file_saver_shell_digest", None)
        == cfg.get("shell_digest"))
    if cfg["phase"] == "prepare":
        return response(warm=warm, shell_ready=shell_ready,
            binary=cfg.get("binary_transport", False))
    if getattr(bi, "_pyodide_file_saver_busy", None) is not None:
        return response(outcome="busy", stderr="Another managed execution is still running")
    bi._pyodide_file_saver_busy = cfg["operation_id"]
    old_cwd, old_import = os.getcwd(), bi.__import__
    evidence = {}
    try:
        if cfg["phase"] == "packages":
            # Source metadata is encoded so native frontend regex preloading
            # cannot bypass this phase's blocked-import filter. File bytes use HTTP.
            try:
                source = base64.b64decode(cfg["package_source"], validate=True).decode("utf-8")
                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    # The normal program path retains its SyntaxError/streams.
                    return response()
                names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        names.add(node.module.split(".")[0])
                names.difference_update(cfg["blocked_modules"])
                if not names:
                    return response()
                import pyodide_js
                from pyodide.ffi import create_proxy
                from js import Object
                failed = False
                def load_error(_message):
                    nonlocal failed
                    failed = True
                def load_progress(_message):
                    pass
                callbacks = []
                try:
                    callbacks.append(create_proxy(load_progress))
                    callbacks.append(create_proxy(load_error))
                    options = Object.new()
                    options.messageCallback, options.errorCallback = callbacks
                    options.checkIntegrity = True
                    # The vendor lock resolves imports (including PIL) to distributions.
                    # Some failures resolve normally and only invoke errorCallback.
                    # Do not time out this await and clear busy: the JS installer
                    # has no public abort API and may still be changing the runtime.
                    await pyodide_js.loadPackagesFromImports(
                        "\n".join("import " + name for name in sorted(names)), options)
                finally:
                    for callback in callbacks:
                        callback.destroy()
                return response(outcome="dependency_error" if failed else "ok")
            except Exception:
                return response(outcome="dependency_error")
        continuation = cfg.get("continuation", False)
        snapshot_only = cfg["phase"] in {"snapshot", "recover", "upload"}
        if snapshot_only:
            if not state or state["head"] != cfg["operation_id"]:
                return response(outcome="recovery_unavailable")
            continuation = True
        if continuation and not (state and state["head"] == cfg["operation_id"]):
            raise WorkspaceInvariantError("restore_changed")
        if not continuation and cfg["warm"] and not warm:
            raise WorkspaceInvariantError("workspace_changed")
        if not continuation and not cfg["warm"]:
            # A conflicting/unacknowledged old root remains intact for diagnosis.
            root = "/tmp/pyodide-workspace-" + cfg["operation_id"]
            os.mkdir(root)
            state = dict(root=root, head=cfg["head"], inputs=[])
            cache[cfg["key"]] = state
        root = state["root"]
        if not snapshot_only:
            state.pop("execution", None)
        # Any mutation makes this root pending until this operation is acknowledged.
        state["head"] = cfg["operation_id"]
        def target(relative):
            if (not relative or relative.startswith("/") or "\\" in relative
                    or any(p in ("", ".", "..") for p in relative.split("/"))):
                raise WorkspaceInvariantError("invalid_path")
            full = root + "/" + relative
            if os.path.commonpath([root, os.path.realpath(full)]) != root:
                raise WorkspaceInvariantError("path_escape")
            return full
        def regular(relative):
            full = target(relative)
            # No user symlinks in any retained/input ancestor, including internal links.
            parent = root
            for name in relative.split("/"):
                parent += "/" + name
                if os.path.islink(parent):
                    raise WorkspaceInvariantError("retained_link")
            return full
        for name in ("work", "scripts", "outputs", "inputs"):
            full = regular(name)
            os.makedirs(full, exist_ok=True)
            if not stat.S_ISDIR(os.lstat(full).st_mode):
                raise WorkspaceInvariantError("managed_directory")
        for name in ("inputs", "scripts", "outputs"):
            alias = root + "/work/" + name
            expected = "../" + name
            if os.path.lexists(alias):
                if not os.path.islink(alias) or os.readlink(alias) != expected:
                    raise WorkspaceInvariantError("managed_alias")
            else:
                os.symlink(expected, alias)
        os.makedirs(root + "/work/tmp", exist_ok=True)
        current_inputs = {f["path"] for f in cfg["inputs"]}
        for relative in state["inputs"]:
            if relative not in current_inputs:
                full = regular(relative)
                if os.path.isfile(full):
                    os.unlink(full)
        async def http(url, options, json_reply=False, expected_size=None):
            from js import fetch, AbortController, Object
            from pyodide.ffi import to_js
            controller = AbortController.new()
            options = to_js(options, dict_converter=Object.fromEntries)
            options.signal = controller.signal
            try:
                async with asyncio.timeout(min(45, cfg["limits"]["callback_timeout_seconds"],
                        cfg.get("http_timeout_seconds", 45))):
                    reply = await fetch(url, options)
                    if not reply.ok:
                        raise WorkspaceInvariantError("transfer_http", int(reply.status))
                    # Read the body under the same deadline and reject oversized
                    # responses before allocating an unbounded ArrayBuffer.
                    if json_reply:
                        return await reply.json()
                    from js import Uint8Array
                    reader = reply.body.getReader()
                    buffer = Uint8Array.new(expected_size)
                    offset = 0
                    try:
                        while True:
                            chunk = await reader.read()
                            if chunk.done:
                                break
                            if offset + int(chunk.value.byteLength) > expected_size:
                                raise WorkspaceInvariantError("transfer_changed")
                            buffer.set(chunk.value, offset)
                            offset += int(chunk.value.byteLength)
                        if offset != expected_size:
                            raise WorkspaceInvariantError("transfer_changed")
                        return buffer
                    finally:
                        # Cancellation is cleanup; it must not replace the read/budget cause.
                        try:
                            await reader.cancel()
                        except Exception:
                            pass
            except TimeoutError:
                raise WorkspaceInvariantError("transfer_timeout") from None
            except WorkspaceInvariantError:
                raise
            except Exception:
                raise WorkspaceInvariantError("transfer_request_failed") from None
            finally:
                controller.abort()
        for item in cfg.get("native_restore", []):
            full = regular(item["path"])
            raw = (await http(item["url"], dict(credentials="same-origin"),
                expected_size=item["size"])).to_py()
            if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise WorkspaceInvariantError("transfer_changed")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as stream:
                stream.write(raw)
            del raw
        for item in cfg["restore"]:
            full = regular(item["path"])
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as stream:
                stream.write(base64.b64decode(item["data"], validate=True))
        state["inputs"] = sorted(current_inputs)
        if cfg["phase"] == "restore":
            return response()
        for entry in cfg["inputs"]:
            full = regular(entry["path"])
            with open(full, "rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != entry["sha256"] or os.path.getsize(full) != entry["size"]:
                raise WorkspaceInvariantError("input_changed")
        cwd = root if cfg["cwd"] == "." else target(cfg["cwd"])
        if not os.path.isdir(cwd):
            raise WorkspaceInvariantError("missing_cwd")
        async def execute():
            record_base = "scripts/.runs"
            while record_base in cfg["legacy_paths"]:
                record_base += "-current"
            record_relative = record_base + "/latest"
            while any(p == record_relative or p.startswith(record_relative + "/")
                      for p in cfg["legacy_paths"]):
                record_relative += "-current"
            record_dir = regular(record_relative)
            os.makedirs(record_dir, exist_ok=True)
            source_name = "source.py" if cfg["language"] == "python" else "source.sh"
            with open(record_dir + "/" + source_name, "w", encoding="utf-8") as stream:
                stream.write(cfg["code"])
            record = dict(version=2, operation_id=cfg["operation_id"], language=cfg["language"],
                cwd=cfg["cwd"], source=source_name, python=sys.version.split()[0],
                inputs=cfg["inputs"],
                shell="just-bash@3.6.0" if cfg["language"] == "bash" else None)
            # Only package names/versions; no environment, credentials or runtime globals.
            import importlib.metadata
            class Capture(io.TextIOBase):
                def __init__(self, path):
                    self.file = open(path, "w", encoding="utf-8")
                    self.parts, self.truncated = [], False
                    self.remaining = cfg["limits"]["max_text_bytes"]
                def write(self, value):
                    raw = value.encode("utf-8", "replace")
                    kept = raw[:self.remaining]
                    text = kept.decode("utf-8", "ignore")
                    if text:
                        self.file.write(text)
                        self.parts.append(text)
                    self.remaining -= len(kept)
                    self.truncated |= len(raw) > len(kept)
                    return len(value)
                def value(self):
                    self.file.flush()
                    return "".join(self.parts)
            stdout = Capture(root + "/work/" + cfg["operation_id"] + ".stdout")
            stderr = Capture(root + "/work/" + cfg["operation_id"] + ".stderr")
            exit_code = 0
            os.chdir(cwd)
            def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
                if (name.split(".")[0] in cfg["blocked_modules"]
                        and (globals or {}).get("__name__") == "__main__"):
                    raise ImportError("Direct import of this module is restricted by the host")
                return old_import(name, globals, locals, fromlist, level)
            try:
                if cfg["language"] == "bash":
                    if not shell_ready:
                        run_js(cfg["shell_asset"])
                        bi._pyodide_file_saver_shell_digest = cfg.get("shell_digest")
                    import pyodide_js
                    from pyodide.ffi import to_js
                    from js import Object
                    shell = run_js("globalThis.__pyodideFileSaverShell").create(pyodide_js.FS, root)
                    options = to_js(dict(cwd="/" if cfg["cwd"] == "." else "/"+cfg["cwd"]),
                        dict_converter=Object.fromEntries)
                    result = await shell.exec(cfg["code"], options)
                    stdout.write(str(result.stdout))
                    stderr.write(str(result.stderr))
                    exit_code = int(result.exitCode)
                else:
                    bi.__import__ = restricted_import
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        namespace = dict(__name__="__main__", __builtins__=bi.__dict__)
                        compiled = compile(cfg["code"], "<managed-python>", "exec",
                            flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                        value = eval(compiled, namespace)
                        if inspect.isawaitable(value):
                            await value
            except BaseException:
                exit_code = 1
                traceback.print_exc(file=stderr)
            finally:
                bi.__import__ = old_import
            evidence = dict(execution_completed=True, exit_code=exit_code,
                stdout=stdout.value(), stderr=stderr.value(), stdout_truncated=stdout.truncated,
                stderr_truncated=stderr.truncated)
            state["execution"] = evidence
            stdout.file.close()
            stderr.file.close()
            record["packages"] = sorted({d.metadata["Name"]: d.version
                for d in importlib.metadata.distributions()}.items())
            try:
                import micropip
                record["dependency_lock"] = jsn.loads(micropip.freeze())
                record["dependency_lock_format"] = "pyodide-lock"
            except ImportError:
                record["dependency_lock"] = None
                record["dependency_lock_format"] = "unavailable: micropip is not installed"
            except Exception:
                record["dependency_lock"] = None
                record["dependency_lock_format"] = (
                    "unavailable: installed packages could not be frozen")
            record.update(exit_code=exit_code, stdout_truncated=stdout.truncated,
                stderr_truncated=stderr.truncated)
            # Code may edit its own source; acknowledged provenance is always the
            # submitted source. Recheck all ancestors after code before any write.
            source_path = regular(record_relative + "/" + source_name)
            record_path = regular(record_relative + "/record.json")
            with open(source_path, "w", encoding="utf-8") as stream:
                stream.write(cfg["code"])
            with open(record_path, "w", encoding="utf-8") as stream:
                jsn.dump(record, stream, ensure_ascii=True, sort_keys=True)
            return evidence
        if not snapshot_only:
            state["execution"] = await execute()
        evidence = state.get("execution")
        if not evidence:
            return response(outcome="recovery_unavailable")
        if cfg["phase"] == "execute" and cfg.get("defer_snapshot"):
            # A small acknowledgement reaches durable server state before any
            # retained-file scan, encoding or transfer can fail. Snapshot never
            # runs the submitted source again.
            return response(outcome="ok" if evidence["exit_code"] == 0 else "execution_error",
                **evidence)
        # Aliases are checked again after arbitrary user code, but never traversed.
        if os.path.realpath(root) != root or not stat.S_ISDIR(os.lstat(root).st_mode):
            raise WorkspaceInvariantError("managed_root")
        for name in ("inputs", "scripts", "outputs"):
            alias = root + "/work/" + name
            if not os.path.islink(alias) or os.readlink(alias) != "../"+name:
                raise WorkspaceInvariantError("managed_alias")
        for entry in cfg["inputs"]:
            with open(regular(entry["path"]), "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise WorkspaceInvariantError("input_changed")
        if cfg["phase"] == "upload":
            from js import Blob, FormData
            from pyodide.ffi import to_js
            item = cfg["native_transfer"]
            with open(regular(item["path"]), "rb") as stream:
                raw = stream.read(cfg["limits"]["max_file_bytes"] + 1)
            if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise WorkspaceInvariantError("transfer_changed")
            form = FormData.new()
            blob = Blob.new(to_js([memoryview(raw)]))
            form.append("file", blob, item["path"].split("/")[-1])
            form.append("metadata", jsn.dumps(dict(pyodide_file_saver=dict(
                operation_id=cfg["operation_id"], transfer_key=item["transfer_key"]))))
            result = await http(cfg["upload_url"], dict(method="POST", body=form,
                credentials="same-origin"), json_reply=True)
            file_id = str(result.id)
            del raw, blob, form
            return response(binary=True, uploaded_file_id=file_id,
                outcome="ok" if evidence["exit_code"] == 0 else "execution_error", **evidence)
        paths = set(cfg["legacy_paths"])
        for base in ("scripts", "outputs"):
            for directory, dirs, names in os.walk(regular(base), followlinks=False):
                for name in dirs + names:
                    relative = os.path.relpath(os.path.join(directory, name), root)
                    metadata = os.lstat(regular(relative))
                    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                        raise WorkspaceInvariantError("retained_type")
                paths.update(os.path.relpath(os.path.join(directory, name), root) for name in names)
        manifest, changed, total = [], [], 0
        baseline = {f["path"]: f["sha256"] for f in cfg["baseline"]}
        for relative in sorted(paths):
            full = regular(relative)
            if not os.path.exists(full):
                continue
            if not stat.S_ISREG(os.lstat(full).st_mode):
                raise WorkspaceInvariantError("retained_type")
            size = os.path.getsize(full)
            total += size
            if size > cfg["limits"]["max_file_bytes"]:
                raise WorkspaceInvariantError("retained_file_limit")
            if total > cfg["limits"]["max_total_bytes"]:
                raise WorkspaceInvariantError("retained_total_limit")
            if len(manifest) >= cfg["limits"]["max_files"]:
                raise WorkspaceInvariantError("retained_count_limit")
            with open(full, "rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest.append(dict(path=relative, sha256=sha, size=size))
            if baseline.get(relative) != sha and not cfg.get("binary_transport"):
                with open(full, "rb") as stream:
                    changed.append(dict(path=relative,
                        data=base64.b64encode(stream.read()).decode("ascii")))
        state["head"] = cfg["operation_id"]
        return response(outcome="ok" if evidence["exit_code"] == 0 else "execution_error",
            **evidence, manifest=manifest, files=changed, binary=cfg.get("binary_transport", False))
    except Exception as error:
        pending_evidence = (state.get("execution", {}) if state
            and state.get("head") == cfg["operation_id"] else {})
        # Never serialize exception messages, filenames, source or locals. Even
        # class names are selected from built-in types, not arbitrary user classes.
        kind = next((t.__name__ for t in (FileNotFoundError, PermissionError, ValueError,
            OSError, RuntimeError, TypeError, MemoryError) if isinstance(error, t)), "Exception")
        cause = dict(code=error.code if isinstance(error, WorkspaceInvariantError)
            else "snapshot_exception", exception_type=kind, frame="", line=0,
            http_status=error.http_status if isinstance(error, WorkspaceInvariantError) else 0)
        tb = error.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_name == "__FUNCTION__":
                cause.update(frame="managed_workspace", line=min(tb.tb_lineno, 10000))
            tb = tb.tb_next
        return response(outcome="snapshot_error",
            cause=cause, **(evidence or pending_evidence))
    finally:
        bi.__import__ = old_import
        try:
            os.chdir(old_cwd if os.path.isdir(old_cwd) else "/")
        finally:
            bi._pyodide_file_saver_busy = None
await globals().pop("__FUNCTION__")()
"""


def workspace_wrapper(config: dict[str, JsonValue], code: str = "") -> tuple[str, str]:
    nonce = str(uuid4())
    config = {**config, "nonce": nonce}
    source = config.get("code")
    if isinstance(source, str) and source:
        # Native frontends inspect callback source with regexes before dispatch.
        # Encode command metadata so only policy-filtered hints can request loading.
        config = {**config, "code": "", "code_b64": base64.b64encode(source.encode()).decode()}
    wrapper = WORKSPACE_BROWSER.replace("__FUNCTION__", "_pfs_" + nonce.replace("-", "")).replace(
        "__CONFIG__", repr(json_text(config))
    )
    # Reuse the existing supported-package hint extraction without executing it.
    if code and config.get("language") == "python":
        blocked = config.get("blocked_modules", [])
        blocked_names: set[str] = (
            {name for name in blocked if isinstance(name, str)}
            if isinstance(blocked, list)
            else set()
        )
        wrapper = import_hints(code, blocked_names) + wrapper
    return wrapper, nonce


def workspace_reply(raw: object, operation_id: str, nonce: str, limits: Limits) -> WorkspaceReply:
    parsed = JSON_VALUE.validate_python(raw)
    text = parsed.get("result") if isinstance(parsed, dict) else None
    if not isinstance(text, str):
        raise SaverError("Browser returned no workspace response")
    if len(text.encode()) > limits.max_envelope_bytes:
        raise SaverError("Browser workspace response exceeds transport limit")
    try:
        value = WorkspaceReply.model_validate_json(text)
    except ValueError as error:
        raise SaverError("Malformed browser workspace callback") from error
    if any(len(stream.encode()) > limits.max_text_bytes for stream in (value.stdout, value.stderr)):
        raise SaverError("Captured output exceeds configured text limit")
    if value.version != 2 or value.operation_id != operation_id or value.nonce != nonce:
        raise SaverError("Workspace callback binding is invalid")
    if value.outcome == "runtime_changed":
        raise BrowserFailure(
            "Browser runtime changed before execution", "browser_runtime_changed", value.phase
        )
    if value.outcome == "dependency_error":
        if value.phase != "packages" or value.execution_completed:
            raise SaverError(
                "Invalid package preparation acknowledgement", "invalid_browser_response"
            )
        raise SaverError(
            "The runtime could not load an imported package from its configured Pyodide catalog. "
            "No user program started; check the dependency or ask an administrator "
            "to verify package delivery.",
            "dependency_unavailable",
        )
    if value.outcome not in {"ok", "execution_error", "snapshot_error"}:
        raise BrowserFailure(
            "Browser workspace is busy"
            if value.outcome == "busy"
            else "Browser retained snapshot failed",
            "browser_busy" if value.outcome == "busy" else "browser_recovery_unavailable",
            value.phase
            if value.phase
            in {"prepare", "packages", "restore", "execute", "snapshot", "recover", "upload"}
            else "unknown",
        )
    return value
