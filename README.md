# Browser execution for multi-user Open WebUI

Run Python in each user's browser, with **working download links in answers and
shared chats, saved scripts and results, and shell file commands for the agent.**
No additional execution server, per-user execution containers or Kubernetes
cluster is required. Open WebUI stores selected files and controls access.

The agent can search with `rg`, edit with `sed`, filter JSON with `jq`, and copy
or move files with `cp` and `mv`, instead of generating Python for each step.
This also reduces time spent generating and debugging code. Savings depend on
the task and model.

The built-in Pyodide interpreter already creates files. But a user who clicks a
broken link in the answer has to know to look in the sidebar's Files panel instead.
Sharing the chat does not automatically turn browser-local files into downloadable
attachments for its recipients.

This Tool saves selected results as native Open WebUI files and gives the assistant
their download links. Authorized recipients can download them from shared chats.
Python and shell commands use the same browser files; saved source programs and
results can be restored later to rerun a calculation.

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

## How it compares

| Option | What it provides | What to consider |
| --- | --- | --- |
| Built-in Pyodide Code Interpreter | Python, a browser file panel and optional browser-local persistence | Users must find files in the sidebar when answer links fail; browser-local files do not automatically become shared-chat attachments; file operations require Python |
| This Tool | Downloads from answers and authorized shared chats; saved programs and results; browser Python and shell file commands | Requires a connected browser and compatible host; shell supports file operations, not native Linux processes |
| Open Terminal | Server-side Python and a full terminal | Requires server resources and administration; free multi-user mode uses Linux accounts in a shared container, while managed per-user containers use Enterprise Terminals |

The built-in interpreter already supports browser files and image output. See the
[official Open Terminal deployment options](https://docs.openwebui.com/features/open-terminal/advanced/multi-user/).

## Install and try it

Requires **Open WebUI 0.12.0 or newer** with the
[compatible Tool APIs and storage](docs/dev/compatibility.md).

1. Open [Releases](https://github.com/vzd3v/openwebui-pyodide-file-persistence/releases).
   Under **Assets** for the release you want, download `openwebui_pyodide_file_saver.py`.
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
- [Execution limits and recovery](docs/user/limitations.md): browser, dependencies and uncertain work
- [Installation](docs/ops/install.md): settings, limits and removal
- [Missing Python packages](docs/ops/pyodide-packages.md): optional administrator utility
- [Security](SECURITY.md): access and trust boundaries
- [Contributing](CONTRIBUTING.md): development, tests and implementation docs

## License

Integration code: [MIT](LICENSE). The Tool file includes just-bash under
Apache-2.0 and its dependency licenses; see [third-party notices](THIRD_PARTY_NOTICES.txt).
