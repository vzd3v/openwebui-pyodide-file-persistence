# Testing

Set up the checkout through [Contributing](../../CONTRIBUTING.md). Commands use a
POSIX shell. CI runs on Ubuntu with Python 3.11–3.15.

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

## What CI establishes

CI runs formatting, lint, strict types, deterministic builds, unit and integration
tests. Linux jobs cap unit execution at 30 seconds and integration at 5 minutes.
The generated single-file Tool is exercised through initialization, publication
and fresh restoration with a synthetic host and real SQLite/PostgreSQL.

Race/restart tests use committed connections and separate processes. Replace only
external host/provider boundaries; do not mock internal modules or databases.
Test observable behavior and persistence/security invariants. There are no test
line-count quotas or coverage-percentage gate; focused test-only PRs are welcome.

## Native Open WebUI acceptance

For a release or deployment, test the exact generated artifact, host version,
browser and storage provider. Use the real served Pyodide worker and ordinary
assistant calls. Include:

- DOCX/XLSX/PDF generation, automatic saving and exact downloaded bytes.
- Clickable links in assistant answers in original and shared stock UI.
- Python/shell shared files, warm reuse and fresh-browser restoration/rerun.
- Owner/recipient access, foreign/private denial and revocation.
- Interruption, failure and recovery paths relevant to the deployment.

Callback simulation, source inspection, loading a schema and passing CI do not
replace these checks. A new build does not inherit an older artifact's acceptance.
See [compatibility](compatibility.md) for the recorded scope.

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
