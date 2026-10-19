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
- Actual decoded/serialized bounds apply below stock 16 MiB callbacks and
  60-second frontend timeout. Backend timeout does not confirm worker death.
- Remote provider `get_file` can materialize a whole object before bounded
  local reads; no unverified remote resource-bound promise is made.
- Host/provider empty-upload policy still applies: a zero-byte issued file may
  remain privately checkpointed with incomplete publication, never a false link.
- Checkpoints are owner/admin-visible ordinary private Files, not a hidden
  vault. Deletion applies. No pruning/GC; failed provider attempts can leave
  unacknowledged objects.
- `outputs/` is agent intent, not secret classification. Review before sharing.
- Captured stdout/stderr are returned as bounded Tool text to the assistant;
  printing sensitive content can disclose it even when files remain private.
