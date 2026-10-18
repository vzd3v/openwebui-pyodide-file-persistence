"""Public transfer boundaries: hostile browser data must not become an acknowledged head."""

import ast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from pyodide_file_saver.browser_execution import (
    checkpoint_bytes,
    consume_callback,
    make_wrapper,
    read_checkpoint,
)
from pyodide_file_saver.contracts import (
    Action,
    FileBytes,
    Limits,
    SaverError,
    encode_files,
    json_text,
    markdown_link,
    validate_files,
)


@pytest.mark.parametrize(
    "paths",
    [
        ["../secret"],
        ["/absolute"],
        ["x\\y"],
        ["a//b"],
        ["a/./b"],
        ["x\nname"],
        ["a", "a"],
        ["a", "a/b"],
        ["outputs"],
    ],
)
def test_reject_unrestorable_paths(paths: list[str]) -> None:
    with pytest.raises(SaverError):
        validate_files([FileBytes(path, b"x") for path in paths], Limits())


@pytest.mark.parametrize("dimension", ["file", "total", "count"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_actual_decoded_boundaries(dimension: str, delta: int) -> None:
    limits = Limits(max_files=3, max_file_bytes=6 if dimension == "total" else 3, max_total_bytes=6)
    files = (
        [FileBytes("a", b"x" * (3 + delta))]
        if dimension == "file"
        else [FileBytes(str(i), b"x") for i in range(3 + delta)]
        if dimension == "count"
        else [FileBytes("a", b"xxx"), FileBytes("b", b"x" * (3 + delta))]
    )
    if delta > 0:
        with pytest.raises(SaverError):
            read_checkpoint(checkpoint_bytes(files), limits)
    else:
        assert read_checkpoint(checkpoint_bytes(files), limits) == files


@pytest.mark.parametrize(
    "damage", ["unknown", "nonce", "operation", "base64", "truncated", "text", "oversized", "busy"]
)
def test_callback_rejects_untrusted_or_incomplete_data(damage: str) -> None:
    limits = Limits(max_text_bytes=8, max_envelope_bytes=1024)
    envelope: dict[str, object] = {
        "version": 1,
        "operation_id": "op",
        "nonce": "nonce",
        "outcome": "ok",
        "stdout": "",
        "stderr": "",
        "files": encode_files([FileBytes("a", b"x")]),
    }
    if damage == "unknown":
        envelope["owner"] = "foreign"
    elif damage in {"nonce", "operation"}:
        envelope["nonce" if damage == "nonce" else "operation_id"] = "foreign"
    elif damage == "base64":
        envelope["files"] = [{"path": "a", "data": "!!!"}]
    elif damage in {"text", "oversized"}:
        envelope["stdout"] = "x" * (9 if damage == "text" else 1100)
    elif damage == "busy":
        envelope["outcome"] = "busy"
    raw = json_text(envelope)
    with pytest.raises(SaverError):
        consume_callback(
            {"result": raw[:-1] if damage == "truncated" else raw}, "op", "nonce", limits
        )


def test_unicode_bytes_preload_hints_and_canonical_links() -> None:
    files = [FileBytes("outputs/Отчёт [draft].xlsx", b"\x00\xffbinary")]
    assert read_checkpoint(checkpoint_bytes(files), Limits()) == files
    wrapper, _ = make_wrapper(
        str(uuid4()),
        "from docx import Document\nimport pandas as pd\npd.DataFrame().to_excel('outputs/a.xlsx')",
        files,
        Limits(),
        ["socket"],
    )
    ast.parse(wrapper)  # Never execute browser Python on the backend.
    for package in ("docx", "pandas", "openpyxl"):
        assert f"# import {package}\n" in wrapper
    link = markdown_link("Отчёт [draft].xlsx", "synthetic-id")
    assert "\\[draft\\]" in link and link.endswith("/content?attachment=true)")
    assert "/content/content" not in link


@pytest.mark.parametrize(
    "value",
    [
        {"action": "execute"},
        {"action": "retry"},
        {"action": "retry", "code": "pass", "operation_id": str(uuid4())},
        {"action": "status", "code": "pass"},
        {"action": "execute", "code": "pass", "owner": "foreign"},
    ],
)
def test_action_contract_has_no_caller_selected_destination(value: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Action.model_validate(value)
