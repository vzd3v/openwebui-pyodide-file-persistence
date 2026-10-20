# Installation and removal

Pre-release procedure; check the [artifact-specific matrix](../dev/compatibility.md).

## Requirements

- Open WebUI with the [required contracts](../dev/compatibility.md), native function
  calling, Pyodide engine and a connected browser for new execution.
- Build with Python 3.11+ and Node.js 22+. The Open WebUI host supplies Pydantic 2/SQLAlchemy 2;
  do not install this repository's development environment into it.
- Existing PostgreSQL or local disk SQLite database, with permission to create
  the Tool's namespaced tables. Replicas must share that database; separate
  per-replica databases and network-filesystem SQLite are not supported topologies.
- Configured storage provider; qualification is scoped to the linked matrix.

## Steps

1. Back up the host DB and file storage normally. Keep browser persistence enabled
   and existing browser files intact.
2. Clone this repository and check out the revision you intend to test. Run
   `npm ci --ignore-scripts`, `npm run build`, then
   `python3 scripts/build_tool.py --check` outside Open WebUI.
   The deterministic build embeds the shell library into `dist/openwebui_pyodide_file_saver.py`
   and prints its SHA256. It does not install or import Open WebUI.
3. Under **Workspace → Tools**, create a Tool, paste the complete generated
   `dist/openwebui_pyodide_file_saver.py`, assign a unique name and save.
4. Grant access only to intended testers through normal host controls; enable
   the Tool for the test model/chat. Select native function calling and Pyodide.
   The custom `execute_code` replaces the builtin only in this native path.
5. Leave browser persistence enabled and review the Tool's Valves below. No host
   application patch, additional executor or separate database is required.
6. Request a synthetic file. Verify actual assistant inline-link download,
   exact bytes, fresh-browser workspace recovery, shared-recipient inline-link
   download, private/foreign denial and revocation. Controls must not be needed.

The first managed call creates and validates two tables in the existing host DB:
`pyodide_file_saver_workspace` and `pyodide_file_saver_operation`. It does not
modify Open WebUI-owned table definitions or migrations. A missing host capability,
incompatible schema or insufficient permission fails explicitly; it is not repaired
by dropping data.

## Default limits

| Valve | Default |
| --- | --- |
| `max_files` | 64 retained regular files; separately 64 referenced inputs |
| `max_file_bytes` | 8 MiB decoded bytes per file |
| `max_total_bytes` | 8 MiB decoded retained files, excluding scratch and native inputs |
| `max_envelope_bytes` | 15 MiB serialized UTF-8 transfer/checkpoint |
| `max_text_bytes` | 16 KiB per stdout/stderr stream |
| `callback_timeout_seconds` | 55 seconds |

Base64/JSON overhead counts toward the serialized limit. Host upload/provider
restrictions may lower the effective limits. These defaults are not a blanket
provider qualification or capacity promise. Valves enforce serialized bounds below
the host's 16 MiB callback ceiling and a deadline below its 60-second worker timeout.
File count and captured-text caps are conservative policy, not a host capacity
claim. Limits apply to retained files, not only newly issued files. Scratch under work/
is independent of these limits and uses browser memory.

Configure **global Tool Valves** through the native Tool editor/settings UI as an
owner, write-granted editor or administrator (subject to host policy). These are
not ordinary chat-user `UserValves`; changes affect everyone using that Tool.
Every field describes its units. Updating source does not overwrite previously
saved explicit Valve values: review and intentionally change those values if
larger limits are wanted. Decoded limits do not bypass serialized JSON/base64
limits or the host's upload policy. Cold restoration sends files sequentially in
bounded callbacks; individual files are not chunked.
Native settings may submit integer values as decimal strings; only the global
Valves configuration boundary normalizes these to integers. Fractions, exponent
notation, booleans, unknown fields and out-of-range values remain invalid.

## Remove / rollback

For a non-destructive rollback, disable or deselect this Tool and restore the
previous Tool source if necessary. You can remove the Tool through normal host
controls without deleting saved Files or its tables.

Do not clear browser data, change persistence settings or drop the tables as part
of rollback. Existing links use ordinary host routes independent of Tool loading;
access still depends on the Files, provider bytes and Open WebUI permissions.
Reinstall the same compatible version to resume from acknowledged checkpoints.
Other database schema versions are rejected. Version-one checkpoints remain
readable by the new Tool; old Tool versions cannot read newly acknowledged
version-two manifests. Rollback preserves all existing files but may require
keeping the new Tool available for recovery of its own operations.
