# Fix missing Pyodide package files

Use this optional administrator utility when a package supported by your Pyodide
runtime fails to load because its asset returns HTTP 404. Some Open WebUI images
ship only a curated subset of `pyodide-lock.json`. See
[upstream issue #29895](https://github.com/open-webui/open-webui/issues/29895).

The utility audits delivery and prepares **only missing, hash-verified package
files** for your existing web server. Original runtime files and the lockfile stay
authoritative. It does not change the Tool, patch Open WebUI, install packages in
browsers or fix arbitrary incompatible PyPI wheels.

Use Python 3.11+ on POSIX, with no development dependencies. Docker is needed only
for inspecting a container. Run as an administrator who can read the assets and
owns the output directory, never through a chat Tool.

## 1. Audit the running deployment

From a checkout of the revision you want to use:

```sh
python3 scripts/pyodide_packages.py audit \
  --container open-webui \
  --url https://chat.example.org/pyodide/
```

The default container path is `/app/build/pyodide`; override it with
`--container-assets`. For a non-Docker installation, replace `--container open-webui`
with `--assets /path/to/served/pyodide`. Use the actual running deployment's assets.

Audit is read-only. It checks source files against the live lock, then downloads
and hashes every served runtime/catalog asset with at most two concurrent requests.
**This can transfer over a gigabyte.** HTTPS certificate verification is required;
loopback HTTP is allowed for tests. Redirects must stay on the configured origin,
including signed-query redirects. The utility accepts no login credentials; use an
administrator-controlled route serving the actual bytes if static routes are protected.

Results are JSON on stdout; diagnostics go to stderr.

| Exit | Status | Meaning |
| --- | --- | --- |
| `0` | `complete` | All checked assets match source/lock hashes |
| `2` | `incomplete` | Package assets returned 404; `missing` lists them |
| `1` | `failed` | Invalid/changing source, missing core runtime, hash mismatch, HTTP/network error or external redirect |

HTTP 200 with an HTML login/error page fails hashing. A complete audit proves
asset delivery for that snapshot, not Python program or PyPI-wheel compatibility.
See [Pyodide package loading](https://pyodide.org/en/stable/usage/loading-packages.html).

## 2. Prepare the missing files

Choose a root separate from the original assets, with an existing parent directory.
The utility requires operator ownership and rejects symlinked output ancestors or
directories writable by others. It follows only its validated `current` pointer.

```sh
python3 scripts/pyodide_packages.py prepare \
  --container open-webui \
  --root /var/lib/pyodide-packages
```

This downloads the official `pyodide-VERSION.tar.bz2` for the running runtime.
For offline use, add `--archive /path/to/pyodide-VERSION.tar.bz2`. Only missing
basenames matching the live lock's SHA-256 are selected. Missing custom packages,
unsafe locks, wrong hashes, duplicate entries or linked packages fail without
changing the previous publication. URL-based/non-basename lock entries are rejected;
no alternate browser CDN is selected automatically.

Preparation caches verified packages, rechecks the source inventory and atomically
switches `current` to a new immutable generation. A nonblocking process lock prevents
concurrent preparation. Unchanged repeats return `status: unchanged`. Repairs stage
a new generation rather than overwriting published files. Prepare again after
changing the Open WebUI image or lockfile.

| Path | Access |
| --- | --- |
| `current/pyodide/` | Missing packages; readable by the existing web server |
| `releases/*/manifest.json` | Private source inventory; never serve |
| `private/` | Private archive, verified cache and log; never serve |
| `prepare.lock` | Local coordination; never serve |

Directories use `0750`, published files `0640`, private directories `0700`, and
manifests/cache/logs `0600`. Grant the web server traversal/read access only to the
publication using your group or ACL policy. A setgid root with the intended group
can provide inheritance. The helper does not grant access, edit nginx or reload it.

### Transfer and disk limits

| Resource | Bound |
| --- | --- |
| Compressed archive | 2 GiB |
| One package | 1 GiB |
| Selected archive contents | 8 GiB |
| All decompressed contents, including skipped entries | 16 GiB |
| Download socket timeout | 60 seconds |

Only stable numeric Pyodide versions are accepted. Interrupted archive downloads
restart from the beginning; verified package cache entries remain reusable. An
operator can remove a damaged cached archive from `private/` before retrying.
Old/staged generations remain for inspection and rollback; cleanup is manual.

## 3. Add a 404-only fallback

Preserve the existing `/pyodide/` proxy and headers. Merge a fallback for **404
only** into the existing ingress. For nginx, using an illustrative upstream name:

```nginx
location /pyodide/ {
    proxy_pass http://openwebui;
    # Preserve existing proxy/cache/security settings.
    proxy_intercept_errors on;
    error_page 404 = @pyodide_packages;
}

location @pyodide_packages {
    internal;
    root /var/lib/pyodide-packages/current;
    try_files $uri =404;
    expires -1;
    # Apply the original assets' required security/cross-origin headers.
}
```

Successful upstream responses stay authoritative, including runtime and lock
bytes. Other upstream errors remain errors. Preserve CSP, cross-origin, cache
and content-type policies. Never expose the output root through a broad `alias`
or directory listing.

Validate and reload the existing ingress normally, then rerun `audit` against its
public URL. If it remains `incomplete`, check web-server read/traverse permissions
and the fallback's `/pyodide/` path.

The mirror supplies bytes, not installed packages. Use normal Pyodide loading or
`await micropip.install(...)`; a fresh browser still needs installation. Mirrored
packages retain their own licenses. This repository distributes no Pyodide archive
or package bytes; its MIT license covers the utility.

## Roll back

Remove the ingress fallback and reload to return to original delivery. Preserve
original assets and workspace data. To roll back only the mirror, atomically select
a retained generation for the **same source inventory**, then audit it. Never delete
a generation referenced by `current` or still in use. This utility does not manage
application upgrades.
