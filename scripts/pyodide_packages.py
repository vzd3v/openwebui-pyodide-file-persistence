#!/usr/bin/env python3
"""Audit Pyodide delivery, or explicitly prepare a verified additive package mirror."""

from __future__ import annotations

import argparse
import bz2
import concurrent.futures
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException, HTTPMessage, HTTPResponse
from pathlib import Path, PurePosixPath
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger("pyodide_packages")
ARCHIVE_URL = "https://github.com/pyodide/pyodide/releases/download/{0}/pyodide-{0}.tar.bz2"
MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_PACKAGE_BYTES = 1024**3
MAX_METADATA_BYTES = 16 * 1024**2
MAX_EXTRACTED_BYTES = 8 * 1024**3
MAX_ARCHIVE_CONTENT_BYTES = 16 * 1024**3
NETWORK_TIMEOUT = 60
CORE_FILES = {"pyodide.mjs", "pyodide.asm.wasm", "python_stdlib.zip"}
BOOTSTRAP_FILES = {"pyodide.asm.mjs", "pyodide.asm.js"}

# Run exactly the same read-only inventory inside Docker and for a local source tree.
# No Tool/Open WebUI import, database access or runtime execution is involved.
INVENTORY_PROGRAM = r"""
import hashlib,json,pathlib,re,sys
root=pathlib.Path(sys.argv[1])
if not root.is_dir():
    raise ValueError('Asset directory does not exist')
files={}
for path in sorted(root.iterdir()):
    if path.is_symlink():
        raise ValueError('Asset symlinks are unsupported: '+path.name)
    if path.is_file():
        with path.open('rb') as stream:
            files[path.name]=hashlib.file_digest(stream,'sha256').hexdigest()
raw=(root/'pyodide-lock.json').read_bytes()
if len(raw)>16*1024**2:
    raise ValueError('Pyodide lock exceeds 16 MiB')
lock=json.loads(raw)
version=json.loads((root/'package.json').read_text())['version']
print(json.dumps(dict(version=version,lock=lock,files=files)))
"""


@dataclass(frozen=True)
class Source:
    assets: Path | None
    container: str | None
    container_assets: str

    def inventory(self) -> Inventory:
        command = (
            [
                "docker",
                "exec",
                self.container,
                "python",
                "-c",
                INVENTORY_PROGRAM,
                self.container_assets,
            ]
            if self.container is not None
            else [sys.executable, "-c", INVENTORY_PROGRAM, str(self.assets)]
        )
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
        if result.returncode:
            raise ValueError("Cannot inspect source assets: " + result.stderr.strip())
        return Inventory.parse(json.loads(result.stdout))


@dataclass(frozen=True)
class Inventory:
    version: str
    files: dict[str, str]
    packages: dict[str, str]

    @classmethod
    def parse(cls, raw: object) -> Inventory:
        if not isinstance(raw, dict):
            raise ValueError("Invalid source inventory")
        data = cast(dict[str, object], raw)
        version, files, lock = data.get("version"), data.get("files"), data.get("lock")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError("Expected a stable numeric Pyodide version")
        if not isinstance(files, dict) or not isinstance(lock, dict):
            raise ValueError("Missing source files or Pyodide lock")
        checked_files = entries(cast(dict[str, object], files))
        for name in CORE_FILES | {"package.json", "pyodide-lock.json"}:
            if name not in checked_files:
                raise ValueError("Source runtime asset is missing: " + name)
        if not BOOTSTRAP_FILES.intersection(checked_files):
            raise ValueError("Source runtime bootstrap is missing (pyodide.asm.mjs or .js)")
        catalog = cast(dict[str, object], lock).get("packages")
        if not isinstance(catalog, dict) or not catalog:
            raise ValueError("Missing Pyodide package catalog")
        info = cast(dict[str, object], lock).get("info")
        if isinstance(info, dict):
            lock_version = cast(dict[str, object], info).get("version")
            if lock_version is not None and lock_version != version:
                raise ValueError("Source package.json and lock versions differ")
        expected: dict[str, str] = {}
        for item in cast(dict[str, object], catalog).values():
            if not isinstance(item, dict):
                raise ValueError("Invalid Pyodide package entry")
            package = cast(dict[str, object], item)
            pair = entries({str(package.get("file_name")): package.get("sha256")})
            name, digest = next(iter(pair.items()))
            if name in expected and expected[name] != digest:
                raise ValueError("Conflicting package hashes: " + name)
            expected[name] = digest
            if name in checked_files and checked_files[name] != digest:
                raise ValueError("Bundled package differs from source lock: " + name)
        return cls(version, checked_files, expected)

    @property
    def missing(self) -> dict[str, str]:
        return {name: digest for name, digest in self.packages.items() if name not in self.files}

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.record(), sort_keys=True).encode()).hexdigest()

    def record(self) -> dict[str, object]:
        return {"version": self.version, "files": self.files, "packages": self.packages}


def entries(raw: dict[str, object]) -> dict[str, str]:
    checked: dict[str, str] = {}
    for name, digest in raw.items():
        if (
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", name)
            or name in {".", ".."}
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("Unsafe asset name or SHA256: " + name)
        checked[name] = digest
    return checked


def digest_file(path: Path) -> str:
    regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Expected a regular file, never a link: " + str(path))


def directory(path: Path, *, private: bool = False, create: bool = True) -> None:
    # Reject redirects through any existing ancestor; refuse writable shared roots.
    for ancestor in reversed((path, *path.parents)):
        if ancestor.is_symlink():
            raise ValueError("Directory symlink is unsupported: " + str(ancestor))
    if create and not path.exists():
        path.mkdir(mode=0o700 if private else 0o750)
    mode = path.lstat()
    if (
        not stat.S_ISDIR(mode.st_mode)
        or mode.st_uid != os.geteuid()
        or mode.st_mode & 0o022
        or (private and mode.st_mode & 0o077)
    ):
        raise ValueError(
            "Directory must be owned by this operator and not shared writable: " + str(path)
        )


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ValueError("Expected a URL without credentials or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("HTTPS is required (HTTP is allowed only on loopback)")


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, origin: tuple[str, str | None, int | None] | None = None) -> None:
        super().__init__()
        self.origin = origin

    def redirect_request(
        self, req: Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> Request | None:
        # Check before following a redirect; checking the final URL would be too late.
        validate_url(newurl)
        if req.full_url.startswith("https:") and not newurl.startswith("https:"):
            raise ValueError("Refusing HTTPS downgrade")
        if self.origin is not None and url_origin(newurl) != self.origin:
            raise ValueError("Audit asset redirected outside the configured origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def url_origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def response(url: str, *, same_origin: bool = False) -> HTTPResponse:
    validate_url(url)
    return cast(
        HTTPResponse,
        build_opener(SafeRedirect(url_origin(url) if same_origin else None)).open(
            Request(url, headers={"Accept-Encoding": "identity"}), timeout=NETWORK_TIMEOUT
        ),
    )


def transfer(source: IO[bytes], destination: IO[bytes] | None, limit: int) -> str:
    digest, total = hashlib.sha256(), 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise ValueError("Asset exceeds the documented transfer limit")
        digest.update(chunk)
        if destination is not None:
            _ = destination.write(chunk)
    return digest.hexdigest()


def audit(source: Source, url: str) -> tuple[dict[str, object], int]:
    validate_url(url)
    if not url.endswith("/"):
        raise ValueError("The served Pyodide URL must end in /")
    if urlsplit(url).query:
        raise ValueError("The served Pyodide URL must not include a query")
    inventory = source.inventory()
    expected = {**inventory.files, **inventory.packages}

    def check(item: tuple[str, str]) -> tuple[str, str]:
        name, digest = item
        try:
            with response(urljoin(url, quote(name)), same_origin=True) as stream:
                actual = transfer(cast(IO[bytes], stream), None, MAX_PACKAGE_BYTES)
            return name, "ok" if actual == digest else "sha256_mismatch"
        except HTTPError as error:
            return name, "missing" if error.code == 404 else "http_" + str(error.code)
        except (OSError, ValueError, HTTPException) as error:
            return name, "request_failed: " + str(error)

    # A mismatched lock/runtime invalidates package diagnosis; still report each result.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(pool.map(check, sorted(expected.items())))
    missing = sorted(name for name, result in results.items() if result == "missing")
    failed = {name: result for name, result in results.items() if result not in {"ok", "missing"}}
    if source.inventory() != inventory:
        failed["source"] = "assets_changed_during_audit"
    core_missing = sorted(
        set(missing) & (CORE_FILES | BOOTSTRAP_FILES | {"pyodide-lock.json", "package.json"})
    )
    if core_missing:
        failed["runtime"] = "missing_core_assets: " + ", ".join(core_missing)
    status = "failed" if failed else "incomplete" if missing else "complete"
    return {
        "status": status,
        "version": inventory.version,
        "lock_sha256": inventory.files["pyodide-lock.json"],
        "checked": len(expected),
        "missing": missing,
        "failures": failed,
    }, 1 if failed else 2 if missing else 0


@contextmanager
def temporary_file(parent: Path) -> Iterator[tuple[Path, IO[bytes]]]:
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            yield temporary, stream
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    with temporary_file(path.parent) as (temporary, output):
        _ = output.write((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        output.flush()
        os.fsync(output.fileno())
        _ = temporary.replace(path)


def get_archive(root: Path, version: str, supplied: Path | None) -> Path:
    if supplied is not None:
        regular(supplied)
        if supplied.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds 2 GiB")
        return supplied
    archive = root / ("pyodide-" + version + ".tar.bz2")
    if archive.exists() or archive.is_symlink():
        regular(archive)
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds 2 GiB")
        return archive
    LOG.info("Downloading matching official archive for %s", version)
    with temporary_file(root) as (temporary, output):
        with response(ARCHIVE_URL.format(version)) as stream:
            _ = transfer(cast(IO[bytes], stream), output, MAX_ARCHIVE_BYTES)
        output.flush()
        os.fsync(output.fileno())
        _ = temporary.replace(archive)
    return archive


class ArchiveReader(io.RawIOBase):
    """Bound skipped members too; a small compressed archive may expand enormously."""

    def __init__(self, source: bz2.BZ2File) -> None:
        super().__init__()
        self.source, self.total = source, 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        self.total += len(chunk)
        if self.total > MAX_ARCHIVE_CONTENT_BYTES:
            raise ValueError("Decompressed archive exceeds 16 GiB")
        return chunk


def fill_cache(archive: Path, cache: Path, pending: dict[str, str]) -> None:
    wanted, seen, extracted = dict(pending), set[str](), 0
    with (
        bz2.BZ2File(archive, "rb") as compressed,
        tarfile.open(fileobj=ArchiveReader(compressed), mode="r|") as stream,
    ):
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError("Unsafe archive member: " + member.name)
            name = path.name
            if name not in wanted:
                continue
            if name in seen or not member.isfile() or member.size > MAX_PACKAGE_BYTES:
                raise ValueError("Duplicate, linked or oversized package: " + name)
            seen.add(name)
            extracted += member.size
            if extracted > MAX_EXTRACTED_BYTES:
                raise ValueError("Archive packages exceed 8 GiB")
            package = stream.extractfile(member)
            if package is None:
                raise ValueError("Unreadable archive package: " + name)
            with package, temporary_file(cache) as (temporary, output):
                digest = transfer(package, output, MAX_PACKAGE_BYTES)
                if digest != wanted[name]:
                    raise ValueError("Archive package differs from live lock: " + name)
                output.flush()
                os.fsync(output.fileno())
                _ = temporary.replace(cache / digest)
            del pending[name]
            LOG.info("Verified package %s", name)
    if pending:
        raise ValueError(
            "Matching archive lacks lock entries (possibly custom packages): "
            + ", ".join(sorted(pending))
        )


def current_generation(root: Path, inventory: Inventory) -> Path | None:
    current = root / "current"
    if not current.is_symlink():
        if current.exists():
            raise ValueError("Publication current must be an operator-created symlink")
        return None
    target = Path(os.readlink(current))
    if (
        target.is_absolute()
        or len(target.parts) != 3
        or target.parts[0] != "releases"
        or target.parts[2] != "public"
        or not re.fullmatch(r"[a-f0-9]{64}-[a-f0-9]{32}", target.parts[1])
    ):
        raise ValueError("Unsafe current publication symlink")
    generation = root / target.parent
    directory(generation, create=False)
    directory(generation / "public", create=False)
    directory(generation / "public" / "pyodide", create=False)
    manifest = generation / "manifest.json"
    regular(manifest)
    if json.loads(manifest.read_text()) != inventory.record():
        return None
    packages = generation / "public" / "pyodide"
    if set(path.name for path in packages.iterdir()) != set(inventory.missing):
        return None
    if any(digest_file(packages / name) != digest for name, digest in inventory.missing.items()):
        return None
    return generation


def prepare(source: Source, root: Path, supplied_archive: Path | None) -> dict[str, object]:
    if ".." in root.parts:
        raise ValueError("Output root must not contain parent-directory traversal")
    root = root.absolute()
    if source.assets is not None:
        assets = source.assets.resolve()
        if root == assets or root.is_relative_to(assets) or assets.is_relative_to(root):
            raise ValueError("Output root and source assets must be separate trees")
    directory(root)
    lock_path = root / "prepare.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        regular(lock_path)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        private, releases = root / "private", root / "releases"
        directory(private, private=True)
        directory(releases)
        log_path = private / "prepare.log"
        if log_path.exists() or log_path.is_symlink():
            regular(log_path)
        log_descriptor = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(log_descriptor)
        log_path.chmod(0o600)
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        try:
            return prepare_locked(source, root, private, releases, supplied_archive)
        except (
            OSError,
            ValueError,
            tarfile.TarError,
            subprocess.SubprocessError,
            HTTPException,
            URLError,
        ) as error:
            LOG.error("Preparation failed: %s: %s", type(error).__name__, error)
            raise
        finally:
            LOG.removeHandler(handler)
            handler.close()


def prepare_locked(
    source: Source, root: Path, private: Path, releases: Path, supplied_archive: Path | None
) -> dict[str, object]:
    inventory = source.inventory()
    previous = current_generation(root, inventory)
    result: dict[str, object] = {
        "version": inventory.version,
        "lock_sha256": inventory.files["pyodide-lock.json"],
        "supplemental": len(inventory.missing),
    }
    if previous is not None:
        if source.inventory() != inventory:
            raise ValueError("Source assets changed during verification")
        return {**result, "status": "unchanged", "generation": previous.name}
    cache = private / "cache"
    directory(cache, private=True)
    pending: dict[str, str] = {}
    for name, digest in inventory.missing.items():
        cached = cache / digest
        if not cached.exists() and not cached.is_symlink():
            pending[name] = digest
        elif digest_file(cached) != digest:
            # Cache is private and never hard-linked into a published generation.
            pending[name] = digest
    LOG.info(
        "%s: %d bundled assets, %d supplemental packages, %d pending",
        inventory.version,
        len(inventory.files),
        len(inventory.missing),
        len(pending),
    )
    if pending:
        fill_cache(get_archive(private, inventory.version, supplied_archive), cache, pending)
    generation = releases / (inventory.fingerprint + "-" + uuid.uuid4().hex)
    directory(generation)
    public = generation / "public"
    directory(public)
    packages = public / "pyodide"
    directory(packages)
    # Never repair or reuse a published directory: every publication is a fresh copy.
    for name, digest in inventory.missing.items():
        cached = cache / digest
        if digest_file(cached) != digest:
            raise ValueError("Verified cache changed before publication: " + name)
        target = packages / name
        _ = shutil.copyfile(cached, target, follow_symlinks=False)
        target.chmod(0o640)
        if digest_file(target) != digest:
            raise ValueError("Package changed during staging: " + name)
    write_json(generation / "manifest.json", inventory.record())
    if source.inventory() != inventory:
        raise ValueError("Source assets changed during preparation; previous release preserved")
    temporary_link = root / (".current-" + uuid.uuid4().hex)
    try:
        temporary_link.symlink_to(public.relative_to(root), target_is_directory=True)
        _ = temporary_link.replace(root / "current")
    finally:
        temporary_link.unlink(missing_ok=True)
    LOG.info("Published generation %s", generation.name)
    return {**result, "status": "published", "generation": generation.name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "prepare"):
        command = commands.add_parser(name)
        inputs = command.add_mutually_exclusive_group(required=True)
        _ = inputs.add_argument("--assets", type=Path, help="Local served Pyodide asset directory")
        _ = inputs.add_argument("--container", help="Running Docker container")
        _ = command.add_argument(
            "--container-assets", default="/app/build/pyodide", help="Asset directory inside Docker"
        )
        if name == "audit":
            _ = command.add_argument("--url", required=True, help="Served Pyodide URL ending in /")
        else:
            _ = command.add_argument(
                "--root", required=True, type=Path, help="Separate operator-owned output directory"
            )
            _ = command.add_argument(
                "--archive", type=Path, help="Offline matching official tar.bz2 instead of download"
            )
    args = parser.parse_args()
    source = Source(
        cast(Path | None, args.assets),
        cast(str | None, args.container),
        cast(str, args.container_assets),
    )
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    try:
        if args.command == "audit":
            report, code = audit(source, cast(str, args.url))
        else:
            _ = os.umask(0o027)
            report, code = (
                prepare(source, cast(Path, args.root), cast(Path | None, args.archive)),
                0,
            )
    except (
        OSError,
        ValueError,
        tarfile.TarError,
        subprocess.SubprocessError,
        HTTPException,
        URLError,
    ) as error:
        LOG.error("%s: %s", type(error).__name__, error)
        report, code = {"status": "failed", "error": str(error)}, 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
