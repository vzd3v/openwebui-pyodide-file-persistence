# Contributing

Documentation fixes, bug reports and focused code changes are welcome. See
[compatibility](docs/dev/compatibility.md) for host requirements and test coverage.

## Report a problem

Include the project revision, Open WebUI/Python versions, database type, storage
provider, a small synthetic reproduction, and expected versus actual behavior.
Remove credentials, personal data and document contents from logs/screenshots.
For vulnerabilities, follow [Security](SECURITY.md).

Discuss substantial API, schema or architecture changes in an issue first. Small
fixes can go directly to a pull request.

## Set up a checkout

Use Node.js 22+, Python 3.11+ and
[uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
git clone https://github.com/vzd3v/openwebui-pyodide-file-persistence.git
cd openwebui-pyodide-file-persistence
uv sync --locked --python 3.12
npm ci --ignore-scripts
npm run build
uv run python scripts/build_tool.py --check
```

The generated Tool is `dist/openwebui_pyodide_file_saver.py`. The build prints its
SHA-256 and requires no running Open WebUI instance. Keep development dependencies
local to the checkout; do not install this environment into Open WebUI. Commit
source and lockfiles, not generated `build/` or `dist/` files. Change dependencies
only intentionally.

Read [architecture](docs/dev/architecture.md) for the code map and persistence
model, [Tool reference](docs/dev/tool-reference.md) for API behavior, and
[testing](docs/dev/testing.md) for check commands and a disposable PostgreSQL setup.

## Submit a pull request

1. Create a short-lived branch from current `main`, using a fork if needed.
2. Make a focused change with relevant tests and documentation. Keep unrelated
   cleanup separate.
3. Run the applicable checks and open a pull request targeting `main`.
4. Describe the problem, resulting behavior, checks run and any skipped checks or
   compatibility limits. Link the related issue when there is one.
5. Address review feedback. A maintainer merges after CI passes; delete the branch
   after merge. Do not push directly to `main` or bypass failing checks.

CI checks formatting, lint, strict types, reproducible builds, Python 3.11–3.15
with SQLite/PostgreSQL, and the Chromium worker. These tests do not qualify every
Open WebUI host or storage provider. Report observed evidence at its actual scope.

## Preserve these boundaries

- Use English documentation and synthetic examples. Never commit credentials,
  private conversations, real documents or deployment-specific configuration.
- Keep execution in the browser and host integration in the adapter. Do not add
  an execution server, parallel permissions system or Open WebUI patch.
- Preserve browser persistence and saved files. Schema/recovery changes need a
  non-destructive compatibility strategy.
- Test public behavior with real databases. Replace only external host/provider
  boundaries; callback fixtures do not prove stock UI behavior.

Integration code is [MIT](LICENSE). Bundled dependency licenses appear in
[third-party notices](THIRD_PARTY_NOTICES.txt) and the generated Tool.
