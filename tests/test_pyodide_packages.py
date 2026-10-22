"""Public CLI contracts with real files, streamed archives and loopback HTTP."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pyodide_packages.py"
PACKAGE = "sample-1.0-py3-none-any.whl"
PACKAGE_BYTES = b"synthetic package bytes, not executable code"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assets(root: Path, *, package: str = PACKAGE, content: bytes = PACKAGE_BYTES) -> Path:
    root.mkdir(exist_ok=True)
    files = {
        "package.json": json.dumps({"version": "1.2.3"}).encode(),
        "pyodide.mjs": b"synthetic loader",
        "pyodide.asm.mjs": b"synthetic bootstrap",
        "pyodide.asm.wasm": b"synthetic wasm",
        "python_stdlib.zip": b"synthetic stdlib",
    }
    catalog = {
        "info": {"version": "1.2.3"},
        "packages": {"sample": {"file_name": package, "sha256": sha256(content)}},
    }
    files["pyodide-lock.json"] = json.dumps(catalog).encode()
    for name, data in files.items():
        _ = (root / name).write_bytes(data)
    return root


def archive(
    path: Path,
    data: bytes = PACKAGE_BYTES,
    *,
    name: str = "pyodide/" + PACKAGE,
    kind: bytes = tarfile.REGTYPE,
    duplicate: bool = False,
) -> Path:
    with tarfile.open(path, "w:bz2") as stream:
        member = tarfile.TarInfo(name)
        member.type, member.size, member.linkname = kind, len(data), "/outside"
        stream.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        if duplicate:
            stream.addfile(member, io.BytesIO(data))
    return path


def run(
    *arguments: str | Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in arguments)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
        timeout=20,
    )


def report(result: subprocess.CompletedProcess[str], code: int) -> dict[str, object]:
    assert result.returncode == code, result.stdout + result.stderr
    value: object = json.loads(result.stdout)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@contextmanager
def served(
    original: Path,
    mirror: Path,
    *,
    failures: dict[str, int | bytes] | None = None,
    redirect: bool = False,
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = unquote(urlsplit(self.path).path.rsplit("/", 1)[-1])
            if redirect and not urlsplit(self.path).query:
                self.send_response(302)
                self.send_header("Location", "/pyodide/" + name + "?signature=synthetic")
                self.end_headers()
                return
            failure = (failures or {}).get(name)
            if isinstance(failure, int):
                self.send_error(failure)
                return
            target = original / name
            if not target.is_file():
                target = mirror / "current" / "pyodide" / name
            if failure is None and not target.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            _ = self.wfile.write(failure if isinstance(failure, bytes) else target.read_bytes())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/pyodide/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_audit_prepare_full_http_closure_and_idempotence(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    package_archive = archive(tmp_path / "upstream.tar.bz2")
    with served(source, root, redirect=True) as url:
        before = report(run("audit", "--assets", source, "--url", url), 2)
        assert before["status"] == "incomplete" and before["missing"] == [PACKAGE]
        created = report(
            run("prepare", "--assets", source, "--root", root, "--archive", package_archive), 0
        )
        assert created["status"] == "published"
        current = root / "current"
        assert (current / "pyodide" / PACKAGE).read_bytes() == PACKAGE_BYTES
        generation, modified = current.readlink(), current.lstat().st_mtime_ns
        package_archive.unlink()
        repeated = report(
            run("prepare", "--assets", source, "--root", root, "--archive", package_archive), 0
        )
        assert repeated["status"] == "unchanged"
        assert current.readlink() == generation and current.lstat().st_mtime_ns == modified
        assert report(run("audit", "--assets", source, "--url", url), 0)["status"] == "complete"
        assert not (source / PACKAGE).exists()  # Original runtime was never changed.
        assert {path.name for path in (current / "pyodide").iterdir()} == {PACKAGE}
        assert not (root / "private").stat().st_mode & 0o077
        assert not (current.parent / "private" / "prepare.log").stat().st_mode & 0o077


@pytest.mark.parametrize("failure", [503, b"login page with HTTP 200"])
def test_audit_distinguishes_failed_delivery_from_missing(
    tmp_path: Path,
    failure: int | bytes,
) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    with served(source, root, failures={PACKAGE: failure}) as url:
        actual = report(run("audit", "--assets", source, "--url", url), 1)
    assert not actual["missing"]
    failures = cast(dict[str, str], actual["failures"])
    assert failures[PACKAGE] == ("http_503" if failure == 503 else "sha256_mismatch")


@pytest.mark.parametrize(
    "name,kind,duplicate",
    [
        ("../../" + PACKAGE, tarfile.REGTYPE, False),
        ("/outside/" + PACKAGE, tarfile.REGTYPE, False),
        ("pyodide/" + PACKAGE, tarfile.SYMTYPE, False),
        ("pyodide/" + PACKAGE, tarfile.LNKTYPE, False),
        ("pyodide/" + PACKAGE, tarfile.REGTYPE, True),
    ],
)
def test_malicious_archives_cannot_publish(
    tmp_path: Path,
    name: str,
    kind: bytes,
    duplicate: bool,
) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    packed = archive(tmp_path / "bad.tar.bz2", name=name, kind=kind, duplicate=duplicate)
    _ = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 1)
    assert not (root / "current").exists() and not (tmp_path / PACKAGE).exists()


def test_sha_failure_resume_and_immutable_repair(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    packed = archive(tmp_path / "archive.tar.bz2", b"incorrect")
    failed = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 1)
    assert "differs from live lock" in str(failed["error"])
    _ = archive(packed)
    _ = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 0)
    original = (root / "current").readlink()
    published = root / original / "pyodide" / PACKAGE
    _ = published.write_bytes(b"damaged publication")
    packed.unlink()
    repaired = report(run("prepare", "--assets", source, "--root", root), 0)
    assert repaired["status"] == "published"
    assert (root / "current").readlink() != original
    assert published.read_bytes() == b"damaged publication"  # Never mutate a served generation.
    assert (root / "current" / "pyodide" / PACKAGE).read_bytes() == PACKAGE_BYTES


def test_failed_new_lock_preserves_previous_release(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    packed = archive(tmp_path / "archive.tar.bz2")
    _ = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 0)
    original = (root / "current").readlink()
    _ = assets(source, package="custom-package.whl", content=b"not in official archive")
    failed = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 1)
    assert "possibly custom packages" in str(failed["error"])
    assert (root / "current").readlink() == original
    assert (root / original / "pyodide" / PACKAGE).read_bytes() == PACKAGE_BYTES


def test_resume_reuses_only_verified_package_cache(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    second, second_bytes = "other-1.0-py3-none-any.whl", b"second synthetic package"
    lock_path = source / "pyodide-lock.json"
    lock = {
        "packages": {
            "sample": {"file_name": PACKAGE, "sha256": sha256(PACKAGE_BYTES)},
            "other": {"file_name": second, "sha256": sha256(second_bytes)},
        }
    }
    _ = lock_path.write_text(json.dumps(lock))
    packed = tmp_path / "archive.tar.bz2"
    with tarfile.open(packed, "w:bz2") as stream:
        for name, data in [(PACKAGE, PACKAGE_BYTES), (second, b"wrong")]:
            member = tarfile.TarInfo("pyodide/" + name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
    _ = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 1)
    assert (root / "private" / "cache" / sha256(PACKAGE_BYTES)).read_bytes() == PACKAGE_BYTES
    _ = archive(packed, second_bytes, name="pyodide/" + second)
    _ = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 0)
    assert (root / "current" / "pyodide" / PACKAGE).read_bytes() == PACKAGE_BYTES
    assert (root / "current" / "pyodide" / second).read_bytes() == second_bytes


def test_missing_bootstrap_is_failed_runtime_not_package_gap(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    with served(source, root, failures={"pyodide.asm.mjs": 404}) as url:
        actual = report(run("audit", "--assets", source, "--url", url), 1)
        assert "missing_core_assets" in str(actual["failures"])
    (source / "pyodide.asm.mjs").unlink()
    failed = report(run("prepare", "--assets", source, "--root", root), 1)
    assert "runtime bootstrap is missing" in str(failed["error"])


@pytest.mark.parametrize(
    "component", ["root", "lock", "private", "cache", "cached_file", "current"]
)
def test_output_symlinks_are_never_followed(tmp_path: Path, component: str) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    _ = sentinel.write_bytes(b"unchanged")
    root.mkdir(mode=0o750)
    private = root / "private"
    private.mkdir(mode=0o700)
    cache = private / "cache"
    cache.mkdir(mode=0o700)
    if component == "root":
        root = tmp_path / "linked-root"
        root.symlink_to(outside, target_is_directory=True)
    else:
        target = {
            "lock": root / "prepare.lock",
            "private": private,
            "cache": cache,
            "cached_file": cache / sha256(PACKAGE_BYTES),
            "current": root / "current",
        }[component]
        if target.is_dir():
            if target == private:
                cache.rmdir()
            target.rmdir()
        target.symlink_to(sentinel if component in {"lock", "cached_file"} else outside)
    packed = archive(tmp_path / "archive.tar.bz2")
    _ = report(run("prepare", "--assets", source, "--root", root, "--archive", packed), 1)
    assert sentinel.read_bytes() == b"unchanged"


def test_source_changed_before_publication_via_external_docker_boundary(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets"), tmp_path / "mirror"
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    _ = docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os,pathlib,subprocess,sys\n"
        "result=subprocess.run([sys.executable,*sys.argv[4:-1],os.environ['ASSETS']],capture_output=True)\n"
        "sys.stdout.buffer.write(result.stdout)\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "(pathlib.Path(os.environ['ASSETS'])/'pyodide.mjs').write_bytes(os.urandom(8))\n"
        "sys.exit(result.returncode)\n"
    )
    docker.chmod(0o700)
    packed = archive(tmp_path / "archive.tar.bz2")
    failed = report(
        run(
            "prepare",
            "--container",
            "synthetic",
            "--root",
            root,
            "--archive",
            packed,
            env={"PATH": str(binary) + os.pathsep + os.environ["PATH"], "ASSETS": str(source)},
        ),
        1,
    )
    assert "changed during preparation" in str(failed["error"])
    assert not (root / "current").exists()


def test_output_traversal_cannot_alias_source(tmp_path: Path) -> None:
    source = assets(tmp_path / "assets")
    other = tmp_path / "other"
    other.mkdir()
    failed = report(
        run("prepare", "--assets", source, "--root", other / ".." / "assets" / "mirror"), 1
    )
    assert "parent-directory traversal" in str(failed["error"])
    assert not (source / "mirror").exists()


def test_audit_rejects_cross_origin_redirect_before_request(tmp_path: Path) -> None:
    source = assets(tmp_path / "assets")
    received: list[str] = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.path)
            self.send_error(500)

        def log_message(self, format: str, *args: object) -> None:
            pass

    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    thread = threading.Thread(target=destination.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}{self.path}")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    redirect_thread = threading.Thread(
        target=redirect.serve_forever, kwargs={"poll_interval": 0.01}
    )
    redirect_thread.start()
    try:
        value = report(
            run(
                "audit",
                "--assets",
                source,
                "--url",
                f"http://127.0.0.1:{redirect.server_port}/pyodide/",
            ),
            1,
        )
        assert "outside the configured origin" in str(value["failures"])
        assert not received
    finally:
        redirect.shutdown()
        redirect.server_close()
        redirect_thread.join()
        destination.shutdown()
        destination.server_close()
        thread.join()


def test_unsafe_lock_and_runtime_mismatch_fail_without_publication(tmp_path: Path) -> None:
    source, root = assets(tmp_path / "assets", package="../escape.whl"), tmp_path / "mirror"
    failed = report(run("prepare", "--assets", source, "--root", root), 1)
    assert "Unsafe asset name" in str(failed["error"])
    _ = assets(source)
    _ = (source / PACKAGE).write_bytes(b"wrong bundled bytes")
    failed = report(run("prepare", "--assets", source, "--root", root), 1)
    assert "Bundled package differs" in str(failed["error"])
    assert not (root / "current").exists()
