"""Deterministically bundle source modules into one native Tool; no host import."""

from __future__ import annotations

import argparse
import ast
import hashlib
import tomllib
from pathlib import Path

MODULES = (
    "contracts",
    "workspace_state",
    "browser_execution",
    "browser_asset",
    "browser_workspace",
    "workspace_manifest",
    "openwebui_adapter",
    "workspace_execution",
    "tool",
)
ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
HEADER = f'''"""
title: Pyodide File Saver
author: vzd3v
version: {VERSION}
license: MIT
description: Browser Python and Bash workspaces with durable files and shared-chat links.
required_open_webui_version: 0.12.0
"""
from __future__ import annotations
'''


def build() -> str:
    imports: dict[str, None] = {}
    bindings: dict[str, str] = {}
    bodies: list[str] = []
    for module in MODULES:
        source = (ROOT / "src" / "pyodide_file_saver" / f"{module}.py").read_text()
        if module == "browser_asset":
            asset = ROOT / "build" / "browser-shell.js"
            if not asset.is_file():
                raise ValueError("Build browser dependency first: npm ci && npm run build")
            source = source.replace(
                '_EMBEDDED_SHELL = ""', "_EMBEDDED_SHELL = " + repr(asset.read_text())
            )
        lines = source.splitlines(keepends=True)
        tree = ast.parse(source)
        removed: set[int] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                removed.update(range(node.lineno - 1, node.end_lineno or node.lineno))
                if isinstance(node, ast.ImportFrom) and (node.level or node.module == "__future__"):
                    continue
                for alias in node.names:
                    if isinstance(node, ast.Import):
                        name = alias.asname or alias.name.split(".")[0]
                        target = alias.name if alias.asname else name
                    else:
                        if alias.name == "*":
                            raise ValueError("Wildcard imports cannot be flattened safely")
                        name = alias.asname or alias.name
                        target = f"{node.module}.{alias.name}"
                    previous = bindings.get(name)
                    if previous is not None and previous != target:
                        raise ValueError(
                            f"Conflicting bundled import binding {name!r}: "
                            f"{previous!r} versus {target!r} in {module}"
                        )
                    bindings[name] = target
                imports[ast.unparse(node)] = None
            elif (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                removed.update(range(node.lineno - 1, node.end_lineno or node.lineno))
        body = "".join(line for index, line in enumerate(lines) if index not in removed).strip()
        bodies.append(f"# --- {module} ---\n{body}\n")
    artifact = HEADER + "\n".join(imports) + "\n\n" + "\n\n".join(bodies)
    compile(artifact, "<bundled-tool>", "exec")  # Syntax check only, no execution.
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Build twice and compare bytes")
    args = parser.parse_args()
    artifact = build().encode("utf-8")
    if args.check and artifact != build().encode("utf-8"):
        raise SystemExit("Build is not deterministic")
    destination = ROOT / "dist" / "openwebui_pyodide_file_saver.py"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(artifact)
    print(f"Built {destination.relative_to(ROOT)} sha256={hashlib.sha256(artifact).hexdigest()}")


if __name__ == "__main__":
    main()
