# Architecture

One in-process native Tool; no separate server/executor, frontend patch or
custom route. Open WebUI owns identities, grants, ordinary File records,
configured provider bytes and downloads. Browser data is untrusted input.

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Strict actions, limits, envelopes, receipts and path validation |
| `workspace_state.py` | Namespaced schema, durable receipts, atomic head CAS |
| `browser_execution.py` | Guarded browser wrapper and transfer validation |
| `openwebui_adapter.py` | All version-sensitive host/storage/chat integration |
| `tool.py` | Restore → execute → checkpoint → publish/retry |

## State decision

Two tables use separate SQLAlchemy Core metadata in the **existing host DB**;
no upstream schema changes or foreign keys:

- `pyodide_file_saver_workspace`: actor/chat key, schema version, scalar revision,
  acknowledged operation ID.
- `pyodide_file_saver_operation`: server operation ID, actor/chat/message,
  base revision, state version, reserved checkpoint ID, receipt JSON/timestamps;
  actor/chat index. Receipts store hashes/lengths/IDs, not file bytes.

Files have no workspace uniqueness, CAS or retry ledger. Fake metadata-only
Files would clutter normal file interfaces and 404 on their own content route.
Two small coordination tables avoid that confusion; actual Files always have
provider bytes. First use serializes creation and validates exact initial
shape/version. No migration framework or automatic table deletion is included.

A fresh execution commits an operation before browser work. Checkpoints are
private ordinary Files, not chat-linked. File insert and committed receipt
marker share one short transaction; head CAS/accepted state another. A stale
operation retains bytes and cannot auto-publish. Code/message/session hashes
are never used as exactly-once execution identity.

Provider I/O stays outside transactions. Reserve File IDs first, use a fresh
object name for every write attempt, then elect one immutable File/receipt
mapping under DB lock. Losing attempts may leave unacknowledged objects;
no automatic GC is claimed. Committed-but-deleted Files are never recreated.

Publication locks Chat, merges attachments, flushes the host's normalized
message, inserts absent ChatFile links and updates the receipt in one transaction.
Reread actual bytes and persisted attachments/linkage before returning success.
UI notifications are presentation, not persistence. Download content URLs and
attachment-card URLs are distinct.

## Browser boundary

Native execution sends `files=[]`, avoiding stock global-basename loading.
Use fresh operation directories, scoped input aliases, fresh user globals with
`__name__='__main__'`, local bounded output capture and a runtime-wide managed
busy flag before awaits or FS mutation. Preserve host direct-import restrictions
and frontend supported-package detection with non-executing import hints.
This is not a sandbox against other code sharing the runtime.

Only regular relative files enter JSON/base64 snapshots. The server validates
paths, decoded lengths, hashes, count and serialized sizes; no backend user-code
execution, archive extraction, indexing or rendering.

## Build

`uv run python scripts/build_tool.py --check` flattens modules in fixed order,
preserves native Tool metadata and compares two builds. Generated `dist/` is
untracked. Building never imports Open WebUI.
