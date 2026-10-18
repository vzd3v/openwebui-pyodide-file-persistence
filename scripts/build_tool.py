"""Deterministically bundle source modules into one native Tool; no host import."""

from __future__ import annotations

import argparse
import ast
import hashlib
from pathlib import Path

MODULES = ("contracts", "workspace_state", "browser_execution", "openwebui_adapter", "tool")
ROOT = Path(__file__).resolve().parents[1]
HEADER = '''"""
title: Pyodide File Saver
author: vzd3v
version: 0.1.0a1
license: MIT
description: Durable browser Python workspaces and shared-chat file download links.
required_open_webui_version: 0.12.2
"""
from __future__ import annotations
'''


def build() -> str:
    imports: dict[str, None] = {}
    bodies: list[str] = []
    for module in MODULES:
        source = (ROOT / "src" / "pyodide_file_saver" / f"{module}.py").read_text()
        lines = source.splitlines(keepends=True)
        tree = ast.parse(source)
        removed: set[int] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                removed.update(range(node.lineno - 1, node.end_lineno or node.lineno))
                if isinstance(node, ast.ImportFrom) and (node.level or node.module == "__future__"):
                    continue
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
