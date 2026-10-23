# Browser files and shell commands for Open WebUI

Open WebUI's built-in Code Interpreter runs Python in your browser through
Pyodide. Generated files can appear in its browser file panel while links in the
assistant's answer or shared chat still fail. For file searches and edits, the
agent also has to write Python instead of using shell commands.

This Tool adds **working download links, saved files and shell file commands** to
browser execution. Ask for a report, download it from the assistant's answer,
and share the chat with someone who has permission to read it. Keep the source
program to rerun the calculation later.

Python and shell commands use the same browser filesystem. Computation stays on
the user's device; Open WebUI stores the selected files and handles access and
downloads. **No additional execution server is needed.**

## What you can do

- **Compare spreadsheets and download the result.** “Compare these two CSVs by
  customer ID. Give me an XLSX with the differences and save the program so we can
  run it on next month's files.”
- **Edit files without generating Python for every change.** “Find every mention
  of the old product name in these Markdown files, update it, and give me the
  revised files.” The agent can use `rg`, `sed`, `jq` and pipelines alongside Python.
- **Continue a saved calculation.** “Rerun the saved analysis on this new CSV and
  update the report.” Selected programs and results can be restored in a fresh
  browser; temporary data can stay out of storage.

For file-heavy tasks, short shell commands and reusable programs avoid repeated
code generation. Keeping intermediate data in files and returning a small summary
can also reduce model context.

## How it compares

| Option | What it provides | What to consider |
| --- | --- | --- |
| Built-in Pyodide Code Interpreter | Python, a browser file panel and optional browser-local persistence | Arbitrary generated-file links in answers/shared chats are unreliable; no agent shell file commands |
| This Tool | Browser Python and limited shell commands over shared files; native download links; selected files saved for later work | Experimental; requires a connected browser and compatible host; not a full Linux environment |
| Open Terminal | Server-side Python and a full terminal | Requires server resources and administration; free multi-user mode uses Linux accounts in a shared container, while managed per-user containers use Enterprise Terminals |

The built-in interpreter already supports browser files and image output. See the
[official Open Terminal deployment options](https://docs.openwebui.com/features/open-terminal/advanced/multi-user/).

## Install and try it

**Experimental pre-release.** The installer requires Open WebUI 0.12.0 or newer;
that minimum is not a guarantee for every later version or storage provider.
Read [compatibility](docs/dev/compatibility.md) before enabling it.

1. Download [`openwebui_pyodide_file_saver.py` from release 0.2.0a3](https://github.com/vzd3v/openwebui-pyodide-file-persistence/releases/download/v0.2.0a3/openwebui_pyodide_file_saver.py).
2. In **Workspace → Tools**, create a Tool and paste the complete file. Enable it
   for a test chat with native function calling and the Pyodide engine. Disable
   the model's built-in Code Interpreter. Complete the
   [installation settings](docs/ops/install.md#install-and-configure) before testing.
3. Ask: “Create `outputs/hello.txt` containing `Hello from the browser` and give
   me the download link.” Click the link in the answer and check the contents.

An administrator installs it; chat users ask for files normally. The agent
chooses Python or shell and returns the supplied download links. Shared
recipients must sign in and have the normal Open WebUI permissions.

## Keep and reuse your work

Selected programs and results are saved in Open WebUI; scratch stays in the browser.
A fresh browser restores successfully saved files, but not Python variables or
installed packages. The [user guide](docs/user/guide.md#keep-the-files-you-need)
explains where to put files and how to continue an analysis.

## Before you use it

The shell supports file and text commands, not native processes, `curl`, `tar`
or a full Linux environment. Browser memory and available Pyodide packages limit
what can run. Choose a server executor for GPU work, native binaries or background
jobs. Open WebUI still needs storage for saved files.

Default retained-file limits are 8 MiB per file and 8 MiB total. Scratch uses
browser memory independently. Empty retained files require local storage.

- [User guide](docs/user/guide.md): examples, saving and recovery
- [Installation](docs/ops/install.md): settings, limits and removal
- [Missing Python packages](docs/ops/pyodide-packages.md): optional administrator utility
- [Security](SECURITY.md): access and trust boundaries
- [Contributing](CONTRIBUTING.md): development, tests and implementation docs

## License

Integration code: [MIT](LICENSE). The generated Tool includes just-bash under
Apache-2.0 and its dependency licenses; see [third-party notices](THIRD_PARTY_NOTICES.txt).
