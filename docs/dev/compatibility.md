# Compatibility and qualification

This is an experimental pre-release, not a blanket support promise. The adapter
checks capabilities, not an exact Open WebUI version string. Installer metadata
specifies a **minimum**, not an exact-version constraint or proof for later forks.
The current installer minimum is Open WebUI 0.12.0.

## Required host contracts

- Async SQLAlchemy engine, shared host ORM metadata and the required File, Chat,
  ChatFile and normalized ChatMessage fields/types/identity. Publication updates
  only an existing assistant's `files`/`updated_at` in the same transaction as
  Chat JSON, ChatFile links and the durable receipt.
- Async user lookup, accessible-chat write authorization, configuration lookup
  and native file-access checks with compatible call signatures; synchronous
  configured-provider upload/read methods. Open WebUI remains the authority.
- PostgreSQL or durable local disk SQLite, with permission to create two
  namespaced Tool tables. Additional nonunique indexes are allowed; conflicting
  expected indexes, uniqueness, foreign keys or incompatible state are rejected,
  never automatically repaired or deleted.
- Native function calling with genuine injected actor/chat/assistant/request
  context. New Python execution additionally requires the Pyodide engine,
  blocked-module policy and a connected stock browser. Saved status/retry do not
  require a currently enabled execution engine or browser.
- The stock transfer/deadline contract: serialized payloads below 16 MiB and
  callback wait below 60 seconds. Provider upload limits can be lower.

Checks fail closed on concrete missing/incompatible contracts. They cannot prove
that an unknown implementation preserves every permission or frontend semantic.
Do not bypass a failed check by changing a version label or modifying host code.

## Evidence scope

Current `0.1.0a2` observations below apply only to generated artifact SHA256
`a522503432ebdd06ed013b873b692a41275c0cfa0d0674838d695b9ce520059d`
and its corresponding source. Earlier `0.1.0a2` drafts are not qualified by these
results.

| Artifact / environment | Evidence | Not established |
| --- | --- | --- |
| Current `0.1.0a2`, portable Python 3.11–3.15 | Automated checks with synthetic external host/callback boundaries and real disk SQLite/PostgreSQL | Actual host Python support or browser/provider acceptance |
| Current `0.1.0a2` single-file Tool loaded by actual upstream 0.12.0/0.12.2/0.12.3 native loader, Python 3.11/3.12, fresh disk SQLite/PostgreSQL, local provider | Generated Tool initialization/Valve schema, capability/permission contracts, 3 MiB binary storage/restoration, real-trigger atomic publication rollback and content/output preservation, zero-Python retry, owner/shared/foreign/private-checkpoint access; synthetic external execution callback | A running pristine host, stock UI/browser execution, remote providers or every later version |
| Prior `0.1.0a1`, maintainer-modified Open WebUI 0.12.2, PostgreSQL, local storage | Scoped live stock-browser DOCX/XLSX/PDF generation, owner/shared inline downloads, fresh-browser recovery, private/foreign denial, grant revocation, fault/retry/concurrency and lifecycle/interruption checks | Pristine upstream deployment, all providers/topologies, or the current build |
| Current `0.1.0a2`, maintainer-modified Open WebUI 0.12.2, PostgreSQL, local storage | Native settings save/reload and source-update preservation; ordinary-owner 3 MiB binary generation, owner/shared inline downloads with exact bytes/names, fresh-browser private workspace and original-binary recovery, new checksum output, private/foreign denial and existing Tool/data preservation | Pristine upstream deployment, other providers/topologies, full-capacity/transport-boundary testing or replay of the prior full stress matrix |
| Corresponding current modular source, PostgreSQL/local storage, genuine served-worker callbacks where execution occurs | Five direct checks: malformed callback path, invalid action, worker file-size limit, stale/winner CAS, isolated non-Pyodide engine recovery/execute guard; saved status/retry use zero callbacks | Another native model/Socket.IO or Tool-grant test, or a full fault/interruption matrix |

The current stock-browser transfer used explicit 4 MiB/file, 7 MiB/workspace and
14 MiB/envelope Valves. The native UI exposed the 8/8/15 MiB defaults and described
fields, accepted integer-form strings and retained saved values after reload and
same-source update. A 3 MiB binary and fresh-browser recovery do not establish
full 8 MiB capacity or the 16 MiB transport boundary. Prior-artifact stress results
were not replayed wholesale for this build.

Portable Tool/build Python support is separate from the host's Python range.
The examined upstream Open WebUI releases declare Python 3.11/3.12; this project
does not override their requirement. No S3/GCS/Azure browser qualification is
claimed. Contract tests, source comparison and build success are not stock UI
sharing or browser-worker proof.
