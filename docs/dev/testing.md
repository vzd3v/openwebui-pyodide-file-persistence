# Verification

Use Python 3.11/3.12 and the committed lockfile:

```sh
uv sync --locked
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run basedpyright
uv run python scripts/build_tool.py --check
uv run python scripts/check_budgets.py
timeout 30s uv run pytest -q -m 'not integration'
timeout 300s uv run pytest -q -m integration
```

Use temporary **disk** SQLite and an isolated PostgreSQL DB selected by
`SAVER_TEST_DATABASE_URL`; never a deployed host DB. CI provides PostgreSQL.
Race/restart tests need multiple committed connections. Do not mock internal
modules or databases; replace only the external host/provider boundary.

CI budgets: unit ≤30s, integration ≤5min, test lines/files ≤source lines/files,
test diff ≤2×source diff. `scripts/check_budgets.py --base <commit>` additionally
checks the diff. Test public behavior and critical persistence/security
invariants, not implementation details. No coverage percentage gate.

Browser acceptance additionally requires the exact host/provider, real served
Pyodide worker, DOCX/XLSX/PDF automatic saving, ordinary assistant inline-link
clicks in original/shared stock UI, fresh-browser recovery, private/foreign
denial, revocation and interruption cases. Callback simulation/build/source
inspection is not that gate. Keep deployment evidence/identities/tokens/raw
conversations outside this repository.

Automated transfer/boundary tests and real disk SQLite/PostgreSQL tests are
present. Synthetic external-host and callback fixtures are not live Open WebUI
or browser proof. Real-browser/provider acceptance remains pending; do not
advertise a supported deployment/provider or release yet.
