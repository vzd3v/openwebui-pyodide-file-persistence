"""Generate a real browser payload for portable maintainer smoke tests."""

import json
import sys
from pathlib import Path

from pydantic import JsonValue

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pyodide_file_saver.browser_asset import shell_asset  # noqa: E402
from pyodide_file_saver.browser_workspace import workspace_wrapper  # noqa: E402
from pyodide_file_saver.contracts import JSON_VALUE, Limits, digest  # noqa: E402

value = JSON_VALUE.validate_python(json.load(sys.stdin))
assert isinstance(value, dict)
value.setdefault("limits", Limits().model_dump())
value.setdefault("shell_digest", digest(shell_asset().encode()))
if value.pop("include_shell", False):
    value["shell_asset"] = shell_asset()
code: JsonValue = value.get("code", "")
wrapper, nonce = workspace_wrapper(value, code if isinstance(code, str) else "")
print(json.dumps({"code": wrapper, "nonce": nonce}))
