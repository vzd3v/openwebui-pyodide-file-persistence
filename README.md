# Browser workspaces for Open WebUI

Run Python and familiar shell commands against the same browser files. Keep large
scratch data in browser memory, save selected files durably, and return native
Open WebUI download links in the assistant's answer.

**Experimental pre-release.** Requires Open WebUI 0.12.0 or newer with compatible
native Tool contracts. Check the [qualification scope](docs/dev/compatibility.md)
before enabling it on an instance.

## How it works

The agent uses one `execute_code` Tool with `language="python"` or `"bash"`.
Python runs in Pyodide; shell commands run through the open-source
[just-bash](https://github.com/vercel-labs/just-bash) browser library. Both use
one filesystem, with no execution server or per-user server process.

| Directory | Purpose | Saved to Open WebUI |
| --- | --- | --- |
| `work/` | Temporary data, intermediate files and full execution logs | No |
| `scripts/` | Source, reusable programs and private run recipes | Yes, changed bytes only |
| `outputs/` | Files intended for the user | Yes; new/changed files are published after success |
| `inputs/` | Authorized native File references | Original File reused; no duplicate upload |

A warm browser keeps scratch files between calls. A fresh browser restores the
acknowledged retained files and authorized inputs. Ordinary execution errors save
retained files privately when transfer/storage succeed; failed outputs are not
published. Existing version-one checkpoints remain readable and are not deleted.

Open WebUI owns authentication, sharing, storage and downloads. The integration
adds two coordination tables in its existing database and installs as one native
Tool. No frontend patch, runtime npm installation, shell CDN or custom HTTP route
is required.

## Quick start

Maintainers build with Python 3.11+ and Node.js 22+. End users need only the
resulting single Python file; do not install development dependencies into Open WebUI.

```sh
git clone https://github.com/vzd3v/openwebui-pyodide-file-persistence.git
cd openwebui-pyodide-file-persistence
npm ci --ignore-scripts
npm run build
python3 scripts/build_tool.py --check
```

Paste `dist/openwebui_pyodide_file_saver.py` into **Workspace → Tools**, then enable
it for a test chat/model using native function calling and the Pyodide engine.
Keep existing browser persistence enabled. Follow the
[installation guide](docs/ops/install.md) and test with synthetic files first.

Ask for a document normally. The assistant writes issued files under `outputs/`
and includes the Tool's supplied Markdown download links in its answer.

## Limits

Bash here is a browser implementation, not a native operating-system terminal.
It provides `jq`, `rg`, `sed`, `awk`, `find`, pipelines and common file commands;
[command limits and examples](docs/user/guide.md) describe the supported subset.
No native processes, network commands or compressed-file operations are exposed.

Scratch capacity depends on the browser and Pyodide memory. Retained-file and
transport limits are independent of scratch size. Python variables and installed
packages are not restored automatically; private recipes record the submitted
source, runtime and available dependency lock. There is no automatic file pruning.
Empty retained files require native local storage; creation is rejected on remote
or unknown providers because their native empty-File deletion is incompatible.

Read the [user guide](docs/user/guide.md), [security boundaries](SECURITY.md),
[architecture](docs/dev/architecture.md), [testing](docs/dev/testing.md) and
[contribution guide](CONTRIBUTING.md).

## License

Integration code: [MIT](LICENSE). The generated Tool includes just-bash under
Apache-2.0 and its dependency licenses; see [third-party notices](THIRD_PARTY_NOTICES.txt).
