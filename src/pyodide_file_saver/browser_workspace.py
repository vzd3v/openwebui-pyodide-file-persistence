"""Public-Pyodide browser protocol: warm scratch plus bounded retained deltas."""

from __future__ import annotations

from uuid import uuid4

from pydantic import Field, JsonValue

from .browser_execution import import_hints
from .contracts import (
    JSON_VALUE,
    BrowserFailure,
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
    warm: bool = False
    shell_ready: bool = False
    outcome: str = "ok"
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    execution_completed: bool = False
    manifest: list[ManifestEntry] = Field(default_factory=list)
    files: list[TransferFile] = Field(default_factory=list)


WORKSPACE_BROWSER = r"""
async def __FUNCTION__():
    import builtins as bi
    import json as jsn
    cfg = jsn.loads(__CONFIG__)
    import os, stat, base64, hashlib, io, contextlib, traceback, ast, inspect, sys
    cache = getattr(bi, "_pyodide_file_saver_workspaces", {})
    bi._pyodide_file_saver_workspaces = cache
    state = cache.get(cfg["key"])
    warm = bool(state and state["head"] == cfg["head"] and os.path.isdir(state["root"]))
    def response(**values):
        body = dict(version=2, operation_id=cfg["operation_id"],
            nonce=cfg["nonce"], phase=cfg["phase"])
        body.update(values)
        text = jsn.dumps(body, ensure_ascii=True, separators=(",", ":"))
        if len(text.encode()) + 1024 > cfg["limits"]["max_envelope_bytes"]:
            raise ValueError("Retained delta exceeds callback limit")
        return text
    from pyodide.code import run_js
    shell_ready = bool(run_js("Boolean(globalThis.__pyodideFileSaverShell && "
        "globalThis.__pyodideFileSaverShell.version === 'just-bash@3.6.0/workspace@2')"))
    shell_ready = shell_ready and (getattr(bi, "_pyodide_file_saver_shell_digest", None)
        == cfg.get("shell_digest"))
    if cfg["phase"] == "prepare":
        return response(warm=warm, shell_ready=shell_ready)
    if getattr(bi, "_pyodide_file_saver_busy", None) is not None:
        return response(outcome="busy", stderr="Another managed execution is still running")
    bi._pyodide_file_saver_busy = cfg["operation_id"]
    old_cwd, old_import = os.getcwd(), bi.__import__
    evidence = {}
    try:
        continuation = cfg.get("continuation", False)
        snapshot_only = cfg["phase"] in {"snapshot", "recover"}
        if snapshot_only:
            if not state or state["head"] != cfg["operation_id"]:
                return response(outcome="recovery_unavailable")
            continuation = True
        if continuation and not (state and state["head"] == cfg["operation_id"]):
            raise ValueError("Browser restore sequence changed")
        if not continuation and cfg["warm"] and not warm:
            raise ValueError("Browser workspace changed after preparation")
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
                raise ValueError("Invalid relative path")
            full = root + "/" + relative
            if os.path.commonpath([root, os.path.realpath(full)]) != root:
                raise ValueError("Path escaped managed root")
            return full
        def regular(relative):
            full = target(relative)
            # No user symlinks in any retained/input ancestor, including internal links.
            parent = root
            for name in relative.split("/"):
                parent += "/" + name
                if os.path.islink(parent):
                    raise ValueError("Links cannot enter retained snapshots")
            return full
        for name in ("work", "scripts", "outputs", "inputs"):
            full = regular(name)
            os.makedirs(full, exist_ok=True)
            if not stat.S_ISDIR(os.lstat(full).st_mode):
                raise ValueError("Managed directory replaced")
        for name in ("inputs", "scripts", "outputs"):
            alias = root + "/work/" + name
            expected = "../" + name
            if os.path.lexists(alias):
                if not os.path.islink(alias) or os.readlink(alias) != expected:
                    raise ValueError("Managed convenience alias replaced")
            else:
                os.symlink(expected, alias)
        os.makedirs(root + "/work/tmp", exist_ok=True)
        current_inputs = {f["path"] for f in cfg["inputs"]}
        for relative in state["inputs"]:
            if relative not in current_inputs:
                full = regular(relative)
                if os.path.isfile(full):
                    os.unlink(full)
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
                raise ValueError("Native input bytes changed")
        cwd = root if cfg["cwd"] == "." else target(cfg["cwd"])
        if not os.path.isdir(cwd):
            raise ValueError("Requested cwd does not exist")
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
            raise ValueError("Managed root replaced")
        for name in ("inputs", "scripts", "outputs"):
            alias = root + "/work/" + name
            if not os.path.islink(alias) or os.readlink(alias) != "../"+name:
                raise ValueError("Managed convenience alias replaced")
        for entry in cfg["inputs"]:
            with open(regular(entry["path"]), "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise ValueError("Read-only native input was modified; copy it to work/ first")
        paths = set(cfg["legacy_paths"])
        for base in ("scripts", "outputs"):
            for directory, dirs, names in os.walk(regular(base), followlinks=False):
                for name in dirs + names:
                    relative = os.path.relpath(os.path.join(directory, name), root)
                    metadata = os.lstat(regular(relative))
                    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                        raise ValueError("Only regular retained files are supported")
                paths.update(os.path.relpath(os.path.join(directory, name), root) for name in names)
        manifest, changed, total = [], [], 0
        baseline = {f["path"]: f["sha256"] for f in cfg["baseline"]}
        for relative in sorted(paths):
            full = regular(relative)
            if not os.path.exists(full):
                continue
            if not stat.S_ISREG(os.lstat(full).st_mode):
                raise ValueError("Invalid retained regular file")
            size = os.path.getsize(full)
            total += size
            if (size > cfg["limits"]["max_file_bytes"]
                    or total > cfg["limits"]["max_total_bytes"]
                    or len(manifest) >= cfg["limits"]["max_files"]):
                raise ValueError("Retained file limits exceeded; scratch work/ is independent")
            with open(full, "rb") as stream:
                sha = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest.append(dict(path=relative, sha256=sha, size=size))
            if baseline.get(relative) != sha:
                with open(full, "rb") as stream:
                    changed.append(dict(path=relative,
                        data=base64.b64encode(stream.read()).decode("ascii")))
        state["head"] = cfg["operation_id"]
        return response(outcome="ok" if evidence["exit_code"] == 0 else "execution_error",
            **evidence, manifest=manifest, files=changed)
    except Exception:
        pending_evidence = (state.get("execution", {}) if state
            and state.get("head") == cfg["operation_id"] else {})
        return response(outcome="snapshot_error",
            **(evidence or pending_evidence))
    finally:
        bi.__import__ = old_import
        try:
            os.chdir(old_cwd if os.path.isdir(old_cwd) else "/")
        finally:
            bi._pyodide_file_saver_busy = None
await __FUNCTION__()
"""


def workspace_wrapper(config: dict[str, JsonValue], code: str = "") -> tuple[str, str]:
    nonce = str(uuid4())
    config = {**config, "nonce": nonce}
    wrapper = WORKSPACE_BROWSER.replace("__FUNCTION__", "_pfs_" + nonce.replace("-", "")).replace(
        "__CONFIG__", repr(json_text(config))
    )
    # Reuse the existing supported-package hint extraction without executing it.
    if code and config.get("language") == "python":
        wrapper = import_hints(code) + wrapper
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
    if value.outcome not in {"ok", "execution_error", "snapshot_error"}:
        raise BrowserFailure(
            "Browser workspace is busy"
            if value.outcome == "busy"
            else "Browser retained snapshot failed",
            "browser_busy" if value.outcome == "busy" else "browser_recovery_unavailable",
            value.phase
            if value.phase in {"prepare", "restore", "execute", "snapshot", "recover"}
            else "unknown",
        )
    return value
