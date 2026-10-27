# Security

## Report a vulnerability

Use **Report a vulnerability** in the repository's Security tab when available.
Otherwise, open an issue asking only for a private contact; do not post exploit
details. See [GitHub's private reporting guidance](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

Include the affected revision, Open WebUI version, required permissions, impact
and a synthetic reproduction. Do not send credentials, personal data or production
documents. Test only instances you own or have permission to test.

## Access and stored files

Open WebUI owns authentication, chat-write permissions, file access and storage.
The Tool binds injected context to live server records; internal calls do not
inherit HTTP dependency checks automatically. Downloads use normal authenticated
routes, not public or signed URLs. Saved bytes are not extracted, indexed or
rendered as inline HTML by this Tool.

Only issued outputs become chat attachments. Scripts, run records and checkpoint
manifests remain ordinary private Files visible to their owner and administrators;
they are not a hidden vault. They can contain sensitive source and dependency
information. No environment-variable dump is recorded. Review outputs before
sharing: their directory expresses the agent's intent, not a sensitivity check.

Native input references are reauthorized on every call. Warm reuse checks File
existence, grants and local hashes; cold restoration and retry also verify provider
bytes. Legacy saved input copies remain independent owner data. Deletion and
revocation apply to native Files. The Tool does not prune or resurrect them;
failed provider attempts may leave unacknowledged objects.

Empty retained files require local storage and committed receipt provenance.
Remote/unknown providers reject new empty Files; missing provider bytes never
become an empty-file fallback. See [compatibility](docs/dev/compatibility.md#empty-files).

## Execution and disclosure

Browser results are untrusted uploads, not proof that the model executed a
particular program. Hashes verify bytes, not provenance or sensitivity.

Shell paths stay within a workspace and grant no native OS/process access. Python
still shares the browser interpreter's trust boundary. Managed paths and busy
flags limit ordinary mistakes and overlap; they do not isolate malicious/manual
code in that interpreter. This Tool is not a sandbox against arbitrary worker code.
Existing same-origin Pyodide trust and browser persistence remain in place.

Pre-dispatch reconnect probes only fresh authenticated sessions of the same actor
and chat. Its browser runtime UUID identifies continuity, not authority or proof
of execution. A matching UUID cannot grant chat/file access, clear durable gates
or authorize replay of a program whose dispatch was already attempted.

Scratch and transient logs stay in the browser. Scratch logs and captured
stdout/stderr share a per-stream byte cap. Captured stdout/stderr return to the
assistant as bounded text: printing sensitive content discloses it even if its
source file is private. Other host features can also pass file content to a model.

Stock frontend DevTools logs may contain native payloads/results. The Tool's own
backend logger emits metadata rather than code, file bytes, envelopes or payload
tracebacks. It does not control other Open WebUI or provider logs and cannot
promise browser-console secrecy.

## Resource and compatibility limits

Retained transfers stay below stock 16 MiB callbacks and the 60-second frontend
timeout. A backend timeout does not prove that the browser worker stopped.
Durable gates limit repeat dispatch across workers and reconnects; they do not
guarantee exactly-once arbitrary code or bound a model's token use. Known failure
envelopes are normalized fail-closed protocol signals, not trusted browser claims.
Optional native cancellation affects the server response only and needs separate
host qualification; see [execution limits](docs/user/limitations.md).
Remote providers may materialize an entire object before bounded local reads.
Browser command limits are not a device-memory guarantee.

[Capability checks and automated tests](docs/dev/compatibility.md) do not qualify
every native host/provider. Keep persistence enabled and follow
saved-data compatibility rules rather than deleting state to resolve an error.

## Optional package utility

The [package delivery utility](docs/ops/pyodide-packages.md) is an explicit
administrator command. Its operator-owned output stays separate from original
assets. Expose only published packages; keep archives, cache, inventories and logs
private. Preparation failures preserve the previous immutable publication.
Lockfile hashes authorize package bytes, not the safety of package code. The
utility does not create an executor or change authentication.

## Dependency advisories

The browser bundle excludes Undici's Node networking code, and network shell
commands are disabled. The maintainer dependency graph still includes
`guarded-fetch`'s pinned Undici with a
[Node networking advisory](https://github.com/advisories/GHSA-w293-vg96-wgc3).

The esbuild [Windows development-server advisory](https://github.com/advisories/GHSA-g7r4-m6w7-qqqr)
concerns a build dependency; this project uses the build API without a server.
The browser includes `sprintf-js`, which has an
[unpatched precision advisory](https://github.com/advisories/GHSA-hp3w-g68c-fv3c).
The pinned shell bounds precision and returns formatting failures as command
errors; synthetic regression probes confirm later commands remain usable.

These observations do not establish the absence of other vulnerable paths.
Reassess advisories when changing commands, networking or build dependencies.
