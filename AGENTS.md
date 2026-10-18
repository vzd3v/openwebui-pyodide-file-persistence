# Repository Instructions

This is an independent open-source project. Every tracked file and commit must
be suitable for eventual public distribution. No private infrastructure facts,
deployment identifiers, organization-specific configuration, credentials,
conversations or raw operational artifacts belong in this repository.

Optional maintainer-only instructions: read [agents.local.md](agents.local.md)
when present. That file is intentionally untracked and must not be added to git.

## Product boundaries

- Open WebUI owns users, access control and final file storage. Do not introduce
  a parallel authentication system or execute user Python in its server process.
- Preserve existing browser persistence. Changing that setting requires separate
  approval after verified saving, restoration, migration and rollback.
- Treat browser results as untrusted user uploads, never attested model output.
- Keep private workspace recovery separate from intentionally published outputs.
- Do not patch or monkeypatch Open WebUI to conceal an unsupported integration.

## Workflow

- English documentation, neutral examples and synthetic fixtures only.
- Work on `main`; commit logical tasks without unrelated changes.
- Internal specifications, implementation plans and reviews are maintained
  outside this repository. Do not commit them, retain them in git history or
  link private planning documents from public documentation.
- Do not implement before specification and plan gates pass and implementation
  is approved. Do not change repository visibility without explicit approval.
- Verify claims against the supported upstream versions. Document limitations;
  never turn an untested assumption into a compatibility promise.
- Reuse public interfaces and configured storage providers. Isolate necessary
  version-sensitive Open WebUI calls behind a compatibility adapter.
- Source modules produce one self-contained deployable Tool; generated outputs
  must be reproducible. Do not retain example-only code or throwaway scaffolding.
- Keep tests focused on public behavior and critical persistence/security
  invariants. Run formatting, lint, strict type checks and relevant tests before
  claiming a code change is complete.
- Keep internal deployment, access and acceptance evidence outside this repo.

## Implementation foundations

- In-process native extension; initial target 0.12.2, host Python 3.11/3.12,
  browser Pyodide, existing configured storage and DB.
- Multi-user actor/chat state; shared PostgreSQL coordination, disk SQLite only
  after locking tests. No RPS/concurrency target or separate DB per replica.
- No background jobs/queues/second auth. Host native callback transport only.
  Durable receipts are critical state; local locks are never authority.
- All upstream imports/protocol casts stay in `openwebui_adapter.py`.
- [Architecture](docs/dev/architecture.md), [checks/budgets](docs/dev/testing.md).
- Build: `uv run python scripts/build_tool.py --check`; never track `dist/`.
