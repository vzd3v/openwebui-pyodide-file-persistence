# Install the Tool

This guide is for an Open WebUI administrator. Installation adds one native Tool
and two tables to the existing database. It does not require an execution server,
frontend patch or separate database.

Check [compatibility](../dev/compatibility.md), then use the installation checks
below to verify downloads, sharing and recovery.

## Requirements

- Open WebUI with the required native Tool contracts, native function calling,
  the Pyodide engine and a connected browser for new executions.
- PostgreSQL or local-disk SQLite, with permission to create two tables. Replicas
  must share the database. Separate per-replica databases and network-filesystem
  SQLite are unsupported.
- Configured file storage. Empty retained files require native local storage.
  Other provider compatibility needs testing in your environment.

The Open WebUI host supplies Pydantic 2 and SQLAlchemy 2. Do not install this
repository's development environment into it. Building from source is optional;
see [Contributing](../../CONTRIBUTING.md).

## Install and configure

1. Back up the database and file storage normally. Keep browser persistence
   enabled and existing browser files intact.
2. Download [`openwebui_pyodide_file_saver.py` from 0.2.0a3](https://github.com/vzd3v/openwebui-pyodide-file-persistence/releases/download/v0.2.0a3/openwebui_pyodide_file_saver.py).
   Its SHA-256 is
   `8b4e3063ff5ce19372b34535a664abb7e6b03fc69ea44195b2a2288138700958`.
3. Under **Workspace → Tools**, create a Tool, paste the complete file and save.
   When upgrading an existing registration, update it in place instead of leaving
   duplicate `execute_code` Tools enabled.
4. Grant access to intended testers through normal Open WebUI controls and select
   the Tool for the test model/chat. Use **native function calling** and the
   **Pyodide** execution engine. Disable the model's built-in Code Interpreter
   capability so this Tool supplies `execute_code`.
5. If Tool Search is enabled, open **Settings → Admin → Interface → Always
   Loaded Tools** and add `execute_code`, preserving existing entries. This sets
   `chat.tool_search.always_loaded` so the model receives the schema directly.
   Tool Search can remain enabled for other functions; retain Tool selection and
   access grants. See [the host setting](https://docs.openwebui.com/reference/env-configuration/#tool_search_always_loaded).
6. Review the limits below, then run the acceptance checks.

On the first managed call, the Tool creates and validates
`pyodide_file_saver_workspace` and `pyodide_file_saver_operation`. It does not
change Open WebUI's own table definitions or migrations. Missing permissions or
incompatible state produce an error; the Tool never drops data to repair them.

## Check your installation

Use ordinary assistant calls and click links in the answer, not just attachment
cards or Controls:

1. Create a synthetic text file in `outputs/`. Download it and check the bytes.
2. Run Python, then shell against the same file. Save a program in `scripts/`.
3. Open a fresh authenticated browser session. Restore and rerun the saved program.
   Check that retained files return and transient scratch does not.
4. Share the chat with an authorized test recipient. Check their inline download.
   Confirm a foreign user cannot download it; revoke access and check denial again.
5. Try your actual result formats and storage provider. Check errors and recovery
   before treating the installation as ready for users.

See [testing](../dev/testing.md#native-open-webui-acceptance) for the full acceptance
scope. Passing CI or loading the Tool's schema is not a substitute for these checks.

## Limits

Valves are global settings for everyone using this Tool. Change them through the
native Tool editor/settings as an owner, write-granted editor or administrator,
subject to host policy. They are not per-user `UserValves`.

| Valve | Default |
| --- | --- |
| `max_files` | 64 retained regular files; a separate limit of 64 referenced inputs |
| `max_file_bytes` | 8 MiB decoded bytes per file |
| `max_total_bytes` | 8 MiB decoded retained files, excluding scratch and native inputs |
| `max_envelope_bytes` | 15 MiB serialized UTF-8 transfer/checkpoint |
| `max_text_bytes` | 16 KiB per stdout/stderr stream |
| `callback_timeout_seconds` | 55 seconds |

JSON/base64 overhead counts toward the serialized limit. Host upload/provider
policies can lower effective limits. Decoded byte limits do not bypass them.
These defaults stay below the host's 16 MiB callback ceiling and 60-second timeout;
they are not a browser-memory or provider-capacity promise. Cold restoration sends
files sequentially without chunking. Scratch uses browser memory separately.

Source updates do not replace explicitly saved Valve values. Review them when
upgrading. The settings boundary accepts decimal integer strings from the native
UI; fractions, exponent notation, booleans, unknown fields and out-of-range values
are rejected. Each field's description states its units.

## Missing Python packages

The Tool uses your instance's Pyodide package delivery. If a supported package
fails to load with an asset 404, use the optional
[package audit and mirror utility](pyodide-packages.md). It is an administrator
command, independent of Tool installation. It does not automatically install
packages in browsers or restore them after a fresh start.

## Stop or remove the Tool

Disable or deselect the Tool to stop new executions. Removing its registration
through normal host controls does not require deleting saved Files or its tables.
Existing links use normal host routes and still depend on Files, provider bytes
and permissions.

Before replacing the source with an older version, read
[saved-data compatibility](../dev/compatibility.md#saved-data-compatibility).
Preserve the database, saved Files and a version that can read them. Do not clear
browser data, change persistence settings, edit receipts or drop tables to force
a downgrade. Reinstall a compatible version to resume acknowledged workspaces.
