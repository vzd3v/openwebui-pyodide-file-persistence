# Installation and removal

Pre-release procedure; stock-browser deployment qualification is pending.

## Requirements

- Open WebUI 0.12.2, native function calling, Pyodide engine, connected browser.
- Build with Python 3.11/3.12. The Open WebUI host supplies Pydantic 2/SQLAlchemy 2;
  do not install this repository's development environment into it.
- Existing PostgreSQL or local disk SQLite database, with permission to create
  the Tool's namespaced tables. Replicas must share that database; separate
  per-replica databases and network-filesystem SQLite are not supported topologies.
- Configured storage provider. No provider has completed acceptance testing yet.

## Steps

1. Back up the host DB and file storage normally. Keep browser persistence enabled
   and existing browser files intact.
2. Clone this repository and check out the revision you intend to test. Run
   `python3 scripts/build_tool.py --check` in the checkout, outside Open WebUI.
   This standard-library-only build writes `dist/openwebui_pyodide_file_saver.py`
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
modify Open WebUI-owned table definitions or migrations. An unexpected version,
incompatible schema or insufficient permission fails explicitly; it is not repaired
by dropping data.

## Default limits

| Valve | Default |
| --- | --- |
| `max_files` | 64 regular files |
| `max_file_bytes` | 2 MiB per file |
| `max_total_bytes` | 4 MiB workspace, including restored inputs |
| `max_envelope_bytes` | 8 MiB serialized transfer |
| `max_text_bytes` | 16 KiB per stdout/stderr stream |
| `callback_timeout_seconds` | 55 seconds |

Base64/JSON overhead counts toward the serialized limit. Host upload/provider
restrictions may lower the effective limits. These defaults are not a blanket
provider qualification or capacity promise. Valves enforce serialized bounds below
the host's 16 MiB callback ceiling and a deadline below its 60-second worker timeout.

## Remove / rollback

For a non-destructive rollback, disable or deselect this Tool and restore the
previous Tool source if necessary. You can remove the Tool through normal host
controls without deleting saved Files or its tables.

Do not clear browser data, change persistence settings or drop the tables as part
of rollback. Existing links use ordinary host routes independent of Tool loading;
access still depends on the Files, provider bytes and Open WebUI permissions.
Reinstall the same compatible version to resume from acknowledged checkpoints.
Other schema versions are rejected, not automatically migrated.
