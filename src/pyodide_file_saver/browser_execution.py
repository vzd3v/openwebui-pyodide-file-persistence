"""Legacy checkpoint codec and non-executing frontend package hints."""

from __future__ import annotations

import ast

from pydantic import ValidationError

from .contracts import (
    JSON_VALUE,
    FileBytes,
    Limits,
    SaverError,
    TransferFile,
    decode_files,
)

PRELOAD_IMPORTS = frozenset(
    {
        "requests",
        "bs4",
        "numpy",
        "pandas",
        "matplotlib",
        "seaborn",
        "sklearn",
        "scipy",
        "re",
        "sympy",
        "tiktoken",
        "pytz",
        "openpyxl",
        "pptx",
        "docx",
    }
)


def import_hints(code: str) -> str:
    """Non-executing hints for the stock frontend's supported-package detection."""
    hints: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None  # Let browser Python report the error and preserve its workspace.
    if tree:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                hints.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                hints.add(node.module.split(".")[0])
            elif isinstance(node, ast.Attribute) and node.attr in {
                "read_excel",
                "to_excel",
                "ExcelWriter",
                "ExcelFile",
            }:
                hints.add("openpyxl")
    return "\n".join(f"# import {name}" for name in sorted(hints & PRELOAD_IMPORTS)) + "\n"


def read_checkpoint(data: bytes, limits: Limits) -> list[FileBytes]:
    if len(data) > limits.max_envelope_bytes:
        raise SaverError("Saved checkpoint exceeds the transfer limit")
    try:
        value = JSON_VALUE.validate_json(data)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "files"}
            or value["version"] != 1
        ):
            raise SaverError("Invalid saved checkpoint format")
        files = value["files"]
        if not isinstance(files, list):
            raise SaverError("Invalid saved checkpoint files")
        return decode_files([TransferFile.model_validate(item) for item in files], limits)
    except (ValidationError, ValueError, TypeError) as error:
        raise SaverError("Saved checkpoint is malformed") from error
