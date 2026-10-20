# Security

## Reporting a vulnerability

Use **Report a vulnerability** in the repository's Security tab when private
reporting is available. If it is unavailable, open an issue asking only for a
private security contact; do not include vulnerability details in that issue.
This follows [GitHub's private reporting guidance](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

In the private report, include the affected revision, Open WebUI version, required
permissions, impact and a synthetic reproduction. Do not send real credentials,
personal data or production document contents. Avoid testing exploits against
instances you do not own or have permission to test.

## Compatibility status

Pre-release: see the artifact-specific [qualification matrix](docs/dev/compatibility.md).
Contract checks are not browser/provider qualification. Use synthetic documents
for qualification of a new build or environment.

## Trust and data boundaries

- Open WebUI owns identity, chat-write/file-access grants and storage. Rebind
  injected context to live server records; internal calls do not run HTTP
  dependency checks automatically. No parallel authentication system.
- Browser data is an untrusted self-upload, not attested agent execution.
  Checksums verify transfer, never provenance or content sensitivity.
- Managed directories and busy flags prevent ordinary managed overlap, not
  malicious/manual code in the shared interpreter. This is not a sandbox.
- Existing same-origin Pyodide trust remains; browser persistence is unchanged.
  Disabling it is not a substitute for safe migration and recovery.
- Stock frontend DevTools logs can contain native payloads/results. This Tool
  does not patch that behavior or promise browser-console secrecy. Its own
  backend logger emits structured metadata, not code, file bytes, envelopes or
  exception tracebacks containing DB/provider payloads. This is not a guarantee
  about logging elsewhere in Open WebUI or its configured providers.
- Bytes are stored without extraction/indexing/inline HTML. Downloads use
  normal authenticated content routes, not public/signed URLs.
- Retained transfer bounds apply below stock 16 MiB callbacks and
  60-second frontend timeout. Backend timeout does not confirm worker death.
- Remote provider `get_file` can materialize a whole object before bounded
  local reads; no unverified remote resource-bound promise is made.
- Exact empty retained files use receipt-backed native text Files with local
  storage only. Remote/unknown providers reject new empty Files before insertion;
  see the [native lifecycle contract](docs/dev/compatibility.md). Missing provider
  bytes never become an empty-file fallback or a false download link.
- Checkpoints are owner/admin-visible ordinary private Files, not a hidden
  vault. Deletion applies. No pruning/GC; failed provider attempts can leave
  unacknowledged objects.
- `outputs/` is agent intent, not secret classification. Review before sharing.
- Shell paths are rooted in one workspace; they do not grant native OS/process
  access. Python still shares the browser interpreter's trust boundary. This
  integration is not a security boundary against arbitrary code in that worker.
- Scratch/full logs are browser-local. Retained scripts/recipes and dependency
  lock data are private ordinary Files, not chat attachments; they may contain
  sensitive source. No environment-variable dump is recorded.
- New input references are checked against live host grants on every call.
  Warm reuse checks metadata/grants and local input hashes, not inherited
  provider-byte integrity. Cold restore and retry verify actual provider bytes.
  Legacy version-one saved input copies remain independent saved owner data.
- Captured stdout/stderr are returned as bounded Tool text to the assistant;
  printing sensitive content can disclose it even when files remain private.

## Dependency advisory scope

The distributed browser bundle does not include Undici's Node networking code;
network commands are disabled. The installed maintainer dependency graph still
includes `guarded-fetch`'s pinned Undici version with
[Node networking advisories](https://github.com/advisories/GHSA-w293-vg96-wgc3).
The esbuild [Windows development-server advisory](https://github.com/advisories/GHSA-g7r4-m6w7-qqqr)
concerns a build dependency; this project uses its build API without a server.
The browser does include `sprintf-js`, which has an
[unpatched precision advisory](https://github.com/advisories/GHSA-hp3w-g68c-fv3c).
The pinned shell bounds precision and returns formatting failures as command
errors; subsequent commands remain usable in synthetic regression probes. These
scope observations do not establish absence of other vulnerable paths. Reassess
dependency advisories when changing commands, networking or build dependencies.
