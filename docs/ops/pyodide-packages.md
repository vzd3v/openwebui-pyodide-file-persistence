# Diagnose and complete Pyodide package delivery

The Tool uses the Pyodide runtime and package files served by Open WebUI. Some
images ship a curated subset of the files named in `pyodide-lock.json`. A missing
same-origin wheel can therefore fail even though that package supports Pyodide.
This is a host asset-delivery problem, separate from workspace persistence.
See [Open WebUI issue #29895](https://github.com/open-webui/open-webui/issues/29895).

The optional `scripts/pyodide_packages.py` utility audits those assets and prepares
an additive mirror for administrators. It does not change the Tool, Open WebUI,
the original runtime, lockfile, database or browser data. Python 3.11+ on POSIX
is sufficient; no repository development dependencies are needed. Docker is only
required when inspecting a container. Run it with an account that owns the output
directory and can read the source assets, not through a chat Tool.

## Audit first

From a checkout of the revision you intend to use:

```sh
python3 scripts/pyodide_packages.py audit \
  --container open-webui \
  --url https://chat.example.org/pyodide/
```

The default asset path inside Docker is `/app/build/pyodide`; use
`--container-assets` if your image differs. For a non-Docker deployment, replace
`--container open-webui` with `--assets /path/to/served/pyodide`. Select the actual
running image's directory, not an unrelated download of the same version.

Audit is read-only. It hashes the source files, checks bundled packages against
the live lock, then downloads and hashes every served runtime and catalog asset
with at most two concurrent requests. This can transfer over a gigabyte; run it
deliberately. It requires HTTPS with normal certificate verification; loopback
HTTP is supported for local testing. Redirects must remain on the configured
origin. Signed-query redirects on that origin are supported. It does not accept
login credentials or bypass authentication; protected static routes must be tested
through an administrator-controlled route that serves their actual bytes.

JSON is written to stdout; diagnostics go to stderr:

| Exit | Status | Meaning |
| --- | --- | --- |
| `0` | `complete` | Every checked served asset matches the source/lock SHA256 |
| `2` | `incomplete` | Package assets returned HTTP 404; `missing` names them |
| `1` | `failed` | Invalid source, missing core runtime, hash mismatch, HTTP/network failure, external redirect or changing source |

HTTP 200 containing HTML is a hash failure, not a successful package. A complete
audit proves asset delivery for that snapshot; it does not prove arbitrary Python
code or PyPI packages work. A native compiled PyPI wheel is not automatically
compatible with WebAssembly. Consult the matching
[Pyodide package documentation](https://pyodide.org/en/stable/usage/loading-packages.html)
for package installation and supported builds.

## Prepare an additive mirror

Choose an output directory separate from the original assets. Its existing parent
must exist. The utility requires operator ownership and refuses symlinked output
ancestors or directories writable by other users. The only publication symlink it
follows is its validated `current` pointer.

```sh
python3 scripts/pyodide_packages.py prepare \
  --container open-webui \
  --root /var/lib/pyodide-packages
```

Preparation downloads the official `pyodide-VERSION.tar.bz2` release matching the
running runtime. Offline use accepts `--archive /path/to/pyodide-VERSION.tar.bz2`.
The archive is never trusted as a replacement runtime: only missing basenames
whose bytes match the running lock's SHA256 are selected. An absent custom package,
unsafe lock, mismatched hash, duplicate archive entry or linked package fails
explicitly and preserves the previous publication. URL-based/non-basename lock
entries are unsupported and rejected; no alternate CDN is selected automatically.

Verified packages are cached privately and reused after interruption. Published
generations are separate copies: repairs stage a new generation, never overwrite
files in the current one. A nonblocking process lock prevents concurrent prepares;
the source inventory is read again before an atomic `current` symlink swap.
An unchanged repeat returns `status: unchanged` without rewriting current files.
Run preparation again after changing the Open WebUI image or lockfile; do not serve
a previous generation as if it were qualified for a new runtime.

The output contains:

| Path | Intended access |
| --- | --- |
| `current/pyodide/` | Only missing package files, readable by the existing web server |
| `releases/*/manifest.json` | Operator-only source inventory; never web-expose |
| `private/` | Operator-only archive, verified cache and preparation log |
| `prepare.lock` | Local coordination, never web-expose |

Directories are created with mode `0750`, published files with `0640`, private
directories with `0700`, manifests/cache/log files with `0600`. Use your existing
web-server group or ACL policy to grant traversal of publication ancestors and
read access to published packages only. A setgid output directory with the intended
web-server group can provide group inheritance; do not grant access to `private/`
or manifests. The helper does not grant permissions, edit nginx or reload services.

Transfers are bounded to 2 GiB per archive, 1 GiB per package and 8 GiB of selected
archive contents; total decompressed archive data, including skipped entries,
is bounded to 16 GiB. Only stable numeric Pyodide versions are accepted.
Downloads use a 60-second socket timeout. A partial archive
download is retried from the beginning; already verified package cache entries
remain reusable. A damaged cached archive can be removed from `private/` by its
operator before retrying. Old/staged generations are retained for inspection and
rollback; there is no automatic garbage collection.

## Connect the existing web server

Preserve your existing `/pyodide/` proxy and headers. Add interception of **only
404** to an internal fallback. For example, merge the following into an existing
nginx server configuration; the upstream name is illustrative:

```nginx
location /pyodide/ {
    proxy_pass http://openwebui;
    # Keep the deployment's existing proxy/cache/security settings here.
    proxy_intercept_errors on;
    error_page 404 = @pyodide_packages;
}

location @pyodide_packages {
    internal;
    root /var/lib/pyodide-packages/current;
    try_files $uri =404;
    expires -1;
    # Apply the same required security/cross-origin headers as original assets.
}
```

Never expose the output root with a broad `alias` or directory listing. Original
successful responses remain authoritative, including runtime and lock bytes;
other upstream errors remain errors. Keep deployment-specific CSP, cross-origin,
cache and content-type policies consistent for these package responses. Validate
and reload your existing ingress through its normal procedure, then rerun `audit`
against the public URL. An audit that remains `incomplete` often indicates missing
read/traverse permission or a fallback path that does not match `/pyodide/`.

The mirror delivers package bytes; it does not install them into every browser.
Use normal Pyodide loading or `await micropip.install(...)` when needed. A fresh
browser still requires package installation. These upstream packages retain their
own licenses; this repository's MIT license covers the utility, not every mirrored
package. No Pyodide archive or package bytes are distributed in this repository.

## Roll back

Remove the fallback from the ingress and reload it to return to original delivery.
Keep original runtime assets and saved workspace data intact. For a mirror-only
rollback, atomically select a retained generation prepared for the **same** source
inventory, then audit it. Do not delete a generation still referenced by `current`
or in use by the web server. This utility does not manage application upgrades.
