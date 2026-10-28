# Testing

Set up the checkout through [Contributing](../../CONTRIBUTING.md). Commands use a
POSIX shell. The [CI workflow](../../.github/workflows/checks.yml) defines the
Python matrix and service versions.

## Local checks

```sh
uv sync --locked
npm ci --ignore-scripts
npm run check
npm run build
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run basedpyright
uv run python scripts/build_tool.py --check
uv run pytest -q -m 'not integration'
uv run pytest -q -m integration
```

Unit tests need no Open WebUI installation. Integration tests use temporary disk
SQLite and, when `SAVER_TEST_DATABASE_URL` is set, a dedicated PostgreSQL database.
Without that variable, PostgreSQL cases are skipped. A SQLite-only run is not a
full integration run.

CI uses PostgreSQL 17. Databases need UTF-8 encoding; when creating one manually,
use `createdb --template=template0 --encoding=UTF8 saver_test`. SQL_ASCII can fail
before Tool code runs. Python 3.15 uses Psycopg's pure-Python implementation and
needs system `libpq`, because the pinned binary distribution has no 3.15 wheel.

## Disposable PostgreSQL

Never point tests at a deployed Open WebUI database. Fixtures create schemas and
test DDL, corruption and concurrent writes. Use a disposable database with schema
creation permission.

With Docker:

```sh
docker run --detach --rm --name pyodide-file-saver-tests \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_USER=saver \
  --env POSTGRES_PASSWORD=synthetic-test-password \
  --env POSTGRES_DB=saver_test postgres:17
```

Wait until `docker exec pyodide-file-saver-tests pg_isready -U saver -d saver_test`
accepts connections, then run:

```sh
export SAVER_TEST_DATABASE_URL=postgresql+psycopg://saver:synthetic-test-password@127.0.0.1:55432/saver_test
uv run pytest -q -m integration
```

These are synthetic local-test credentials. Afterwards,
`docker stop pyodide-file-saver-tests` removes the container and its test data.
Do not reuse a container holding data you need.

## Chromium worker

```sh
npm exec playwright-core -- install chromium
npm test
```

Use `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to select an existing Chromium. CI installs
Chromium and its system dependencies in a separate job.

This smoke test runs public Pyodide APIs and the production wrapper in a module
worker without Open WebUI. It checks shared Python/shell files, 256 MiB scratch,
32 MiB binary shell operations, text/JSON commands, retained deltas, fresh-worker
restoration, shell errors and rejected links. It proves browser-library behavior,
not native Open WebUI acceptance or available RAM on every device.

It also checks completion before snapshot failure, source-free snapshot/recovery,
single execution and bounded scratch logs. Database tests distinguish failures
before dispatch, during execution and after acknowledged completion, and check
that recovery releases only the applicable request gate.
Socket-boundary fixtures also check pre-dispatch reconnection against runtime
identity, actor/chat/session freshness, generation replacement and cancellation.
They reject replay after restore/execute dispatch. These are synthetic host
boundaries; native reconnect qualification still needs a real served frontend.
The matrix includes a stale session that remains present while its callback
stalls, delayed cancellation, replacement reuse on the next call, mismatched
runtimes and cancellation during discovery.

## What CI establishes

CI runs formatting, lint, strict types, deterministic builds, unit and integration
tests. Linux jobs cap unit execution at 30 seconds and integration at 5 minutes.
The built Tool file is exercised through initialization, publication
and fresh restoration with a synthetic host and real SQLite/PostgreSQL.

Race/restart tests use committed connections and separate processes. Replace only
external host/provider boundaries; do not mock internal modules or databases.
Test observable behavior and persistence/security invariants. There are no test
line-count quotas or coverage-percentage gate; focused test-only PRs are welcome.

## Native Open WebUI acceptance

Choose native checks from the changed behavior and the compatibility claims being
made. Use the exact Tool file, record the host version, browser and storage
provider, and use the real served Pyodide worker and ordinary assistant calls.
The scenario inventory is:

- DOCX/XLSX/PDF generation, automatic saving and exact downloaded bytes.
- Clickable links in assistant answers in original and shared stock UI.
- Python/shell shared files, warm reuse and fresh-browser restoration/rerun.
- Owner/recipient access, foreign/private denial and revocation.
- Interruption, failure and recovery paths relevant to the deployment.

Changes to execution/transport require relevant interruption and recovery checks.
Changes to restore, publication or access control require the corresponding
restoration, exact-download or permission scenarios. A new host/provider or a
claim of broad installation readiness requires the applicable inventory above.
Documentation-only changes need link/content checks; rerun runtime checks only
when the generated Tool bytes or claimed behavior change.

Callback simulation, source inspection and CI do not establish native behavior.
Earlier observations remain valid historical evidence for their recorded bytes;
do not relabel them as checks of a new build. An unchanged scenario need not be
repeated merely because the version changed. See [compatibility](compatibility.md)
for the recorded scope.

## Publishing a release

A release asset is the assembled Python Tool file, not a compiled executable.
It lets an administrator install without setting up the build toolchain.

Before publishing a prerelease:

1. Pass required CI on the reviewed source revision and complete checks relevant
   to the change. Do not bypass a failing check.
2. Build deterministically with `scripts/build_tool.py --check`; attach that exact
   Tool file and record its SHA-256 and source tag.
3. For behavior changes, record native evidence for the affected path when it
   depends on host/browser integration. State tested versions/providers and
   untested compatibility in the documentation and release notes. A missing
   check blocks a claim that depends on it, rather than every scoped prerelease.

Do not require the complete native inventory for every prerelease. A stable
release or new broad compatibility claim needs representative coverage of that
claim, including restoration, formats, downloads and access control. Production
adoption separately requires acceptance appropriate to that host and rollout;
publishing an asset does not authorize a deployment.

Keep deployment identities, tokens, raw chats and operational evidence out of the
repository and public reports. Publish synthetic reproduction steps and clearly
scoped observations.

## Package utility tests

```sh
uv run pytest -q tests/test_pyodide_packages.py
```

These run the optional administrator CLI against synthetic directories, real
compressed archives, loopback HTTP and an external Docker-command fixture. They
cover failure classification, actual fallback delivery, redirects, publication,
repair, cache reuse, source changes and unsafe paths/links. No Docker daemon,
upstream download or Open WebUI instance is needed. They test delivery behavior,
not arbitrary Python package compatibility.
