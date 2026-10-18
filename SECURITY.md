# Security boundaries

Pre-release: no deployment/provider matrix has completed acceptance testing.
Use synthetic documents for qualification.

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
  does not patch that behavior or promise browser-console secrecy. Backend
  logs contain only structured metadata, no code/bytes/envelopes/exception
  reprs or tracebacks containing DB/provider payloads.
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

Report vulnerabilities privately using GitHub security reporting if enabled,
otherwise contact the maintainer. Never post credentials, real document bytes
or a working exploit against a live instance in a public issue.
