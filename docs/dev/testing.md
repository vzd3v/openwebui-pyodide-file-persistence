# Development and verification

Set up the checkout as described in [CONTRIBUTING](../../CONTRIBUTING.md).
Commands below use a POSIX shell. CI runs on Ubuntu with Python 3.11 and 3.12.

Use Python 3.11/3.12 and the committed lockfile:

```sh
uv sync --locked
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run basedpyright
uv run python scripts/build_tool.py --check
uv run python scripts/check_budgets.py
uv run pytest -q -m 'not integration'
uv run pytest -q -m integration
```

Unit checks do not require an Open WebUI installation. Integration fixtures use
temporary **disk** SQLite and an isolated PostgreSQL database selected by
`SAVER_TEST_DATABASE_URL`. Without that variable, PostgreSQL cases are **skipped**;
passing SQLite alone is not a full integration run. CI supplies PostgreSQL 17.

## Disposable PostgreSQL

Never point tests at a deployed Open WebUI database. Fixtures create independent
schemas and intentionally test DDL, corruption and concurrent committed writes.
Use a database dedicated to these tests, with permission to create schemas.

For example, with Docker installed, create a disposable local instance:

```sh
docker run --detach --rm --name pyodide-file-saver-tests \
  --publish 127.0.0.1:55432:5432 \
  --env POSTGRES_USER=saver \
  --env POSTGRES_PASSWORD=synthetic-test-password \
  --env POSTGRES_DB=saver_test postgres:17
```

Wait until `docker exec pyodide-file-saver-tests pg_isready -U saver -d saver_test`
reports that it accepts connections, then run:

```sh
export SAVER_TEST_DATABASE_URL=postgresql+psycopg://saver:synthetic-test-password@127.0.0.1:55432/saver_test
uv run pytest -q -m integration
```

The credentials above are synthetic local-test values, not deployment credentials.
After testing, `docker stop pyodide-file-saver-tests` removes that disposable
container and its test data. Do not reuse a container containing data you need.

## Test boundaries and CI

Race/restart tests use multiple committed connections and separate processes.
Do not mock internal modules or databases; replace only the external host/provider
boundary. Add focused tests for observable behavior and critical persistence or
security invariants, not private implementation details.

CI budgets: unit ≤30s, integration ≤5min, test lines/files ≤source lines/files,
test diff ≤2×source diff. `scripts/check_budgets.py --base <commit>` additionally
checks the diff. Test public behavior and critical persistence/security
invariants, not implementation details. No coverage percentage gate.

CI applies formatting, lint, strict types, deterministic build, maintenance
budgets, unit tests and integration tests in that order. Linux CI limits unit
execution to 30 seconds and integration execution to 5 minutes. A passing CI run
does not establish browser or provider compatibility.

## Browser qualification

Browser acceptance additionally requires the exact host/provider, real served
Pyodide worker, DOCX/XLSX/PDF automatic saving, ordinary assistant inline-link
clicks in original/shared stock UI, fresh-browser recovery, private/foreign
denial, revocation and interruption cases. Callback simulation/build/source
inspection is not that gate. Keep deployment evidence/identities/tokens/raw
conversations out of commits and issue/PR reports. Publish only sanitized,
synthetic reproduction steps and clearly scoped observations.

Automated transfer/boundary tests and real disk SQLite/PostgreSQL tests are
present. Synthetic external-host and callback fixtures are not live Open WebUI
or browser proof. Real-browser/provider acceptance remains pending; do not
advertise a supported deployment/provider or release yet.
