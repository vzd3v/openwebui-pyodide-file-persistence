# Install and remove

Pre-release procedure; stock-browser deployment qualification is pending.

## Requirements

- Open WebUI 0.12.2, native function calling, Pyodide engine, connected browser.
- Host Python 3.11/3.12 with Open WebUI's existing Pydantic 2/SQLAlchemy 2.
  Do not install this development environment into the host environment.
- Shared PostgreSQL, or disk SQLite after locking qualification; writable
  namespaced Tool tables in the existing DB. No separate DB/Redis/server.
- Configured storage provider. No provider has completed acceptance testing yet.

## Steps

1. Back up the host DB and file storage normally. Keep browser persistence enabled
   and existing browser files intact.
2. Check out the reviewed revision; run `uv sync --locked`, then
   `uv run python scripts/build_tool.py --check`.
3. Under **Workspace → Tools**, create a Tool, paste the complete generated
   `dist/openwebui_pyodide_file_saver.py`, assign a unique name and save.
4. Grant access only to intended testers through normal host controls; enable
   the Tool for the test model/chat. Select native function calling and Pyodide.
   The custom `execute_code` replaces the builtin only in this native path.
5. Leave persistence settings unchanged. Review Valves: defaults are 64 files,
   2 MiB/file, 4 MiB workspace, 8 MiB serialized transfer, 16 KiB per text stream
   and a 55-second callback deadline. Host upload limits still apply. These are
   candidate limits until boundary qualification, not measured support promises.
6. Request a synthetic file. Verify actual assistant inline-link download,
   exact bytes, fresh-browser workspace recovery, shared-recipient inline-link
   download, private/foreign denial and revocation. Controls must not be needed.

## Remove / rollback

Disable or remove only the Tool through normal host controls. Do not clear
browser data, change persistence settings, delete saved Files or drop the two
tables. Existing download links use ordinary host routes independent of Tool
installation. Reinstall the same compatible version to recover acknowledged
checkpoints. Incompatible schemas fail explicitly, without automatic migration.
