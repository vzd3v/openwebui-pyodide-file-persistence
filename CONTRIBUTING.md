# Contributing

Bug reports, documentation fixes and focused code changes are welcome.
The project is experimental; see [compatibility status](README.md) before
testing against an Open WebUI instance.

## Issues and proposals

For a bug, include the project revision, Open WebUI and Python versions, database
type, storage provider, a small synthetic reproduction and expected/actual behavior.
Redact credentials, document contents and personal data from logs or screenshots.
Use [SECURITY.md](SECURITY.md) for suspected vulnerabilities.

For a substantial API, schema or architecture change, open an issue to discuss
the approach before implementing it. Small fixes can be submitted directly as
pull requests.

## Development

Use Node.js 22+, Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From a clone of the repository:

```sh
uv sync --locked --python 3.12
npm ci --ignore-scripts
npm run build
```

The environment is local to this checkout. Do not install it into an Open WebUI
runtime. Keep `uv.lock` committed and update dependencies only as an intentional
part of a change. See [verification](docs/dev/testing.md) for check commands,
disposable PostgreSQL setup and the distinction between automated and browser tests.

## Pull requests

This project uses [GitHub Flow](https://docs.github.com/en/get-started/using-github/github-flow):

1. Create a short-lived branch from current `main`; use a fork if you do not have
   write access. There is no long-lived `develop` branch.
2. Make a focused change with descriptive commits. Include relevant tests and
   documentation; do not mix unrelated cleanup into the same pull request.
3. Run the applicable checks and open a pull request targeting `main`.
4. Explain the problem, the change, verification performed and any skipped checks
   or compatibility limitations. Link a related issue when one exists.
5. Address review feedback. A maintainer reviews and merges after CI passes;
   delete the working branch after merge. Do not push changes directly to `main`.

CI runs on pull requests and on `main`. It builds the pinned browser dependency and checks formatting, lint, strict types,
reproducible builds and tests on Python 3.11–3.15 with disk SQLite and PostgreSQL.
This portable Tool matrix does not extend Open WebUI's own host Python range.
Do not bypass failing checks or describe a skipped check as passing.

## Scope and safety

- Keep documentation in English and examples synthetic. Never include real
  credentials, conversations, document contents or deployment configuration.
- Preserve browser persistence and acknowledged server files. Schema or recovery
  changes need a non-destructive compatibility strategy.
- Keep host integration in the adapter. Do not add server-side Python execution,
  a parallel permission system or patches to Open WebUI.
- Test behavior at public boundaries; do not replace internal modules or real
  databases with mocks. Browser callback fixtures do not prove stock UI behavior.
- Commit source, not generated `dist/` artifacts. Building the native Tool does
  not require a running Open WebUI instance.

Integration code is [MIT](LICENSE). Bundled dependency licenses are described in
[third-party notices](THIRD_PARTY_NOTICES.txt) and embedded in the generated Tool.
