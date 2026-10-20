# Agent Instructions

This is a public, standalone open-source repository. Treat every tracked
file, commit and GitHub contribution as public OSS material, even while repository
visibility is temporarily private. Include only portable, redistributable content
appropriate for public distribution.

Read [README.md](README.md) for project scope and compatibility status, and
[CONTRIBUTING.md](CONTRIBUTING.md) for the branch, pull-request and check workflow.

If [agents.local.md](agents.local.md) exists, read it before making changes.
It contains local maintainer instructions, is gitignored and must never be
force-added. Public contributors do not need this optional local file.

## Working on changes

- Use a short-lived branch and a pull request targeting `main`. Do not push
  directly to `main` or bypass failing checks. Keep commits focused and preserve
  unrelated work in the checkout.
- Keep documentation in English. Use synthetic fixtures; never commit real
  credentials, personal data, conversations or deployment-specific configuration.
- Update documentation when behavior changes. Distinguish automated tests,
  real-browser observations and unverified compatibility claims.
- Follow [verification instructions](docs/dev/testing.md). Report skipped checks
  explicitly; simulated host callbacks are not browser acceptance evidence.
- Edit source modules, not generated `dist/` files. Build deterministically with
  `npm ci --ignore-scripts && npm run build`, then
  `python3 scripts/build_tool.py --check`; do not commit generated artifacts.

## Design constraints

- Keep Python execution in the browser. Do not add a server-side executor,
  separate service, authentication system or Open WebUI patch.
- Open WebUI owns identity, access control, configured storage and downloads.
  Keep version-sensitive integration in `openwebui_adapter.py`.
- Treat browser bytes as untrusted uploads, not attested model output.
- Preserve browser persistence and existing saved data. Do not silently delete,
  recreate or migrate acknowledged files or coordination state.
- Keep private workspace checkpoints separate from issued chat attachments.
  Do not publish unchanged inherited files or error-produced files implicitly.
- Durable database receipts and compare-and-set control shared mutations;
  process-local locks are not cross-worker authority.

See [architecture](docs/dev/architecture.md) and [security boundaries](SECURITY.md)
before changing persistence, concurrency or host integration.
