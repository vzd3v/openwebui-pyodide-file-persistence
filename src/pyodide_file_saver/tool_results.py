"""Ordinary native Tool results and content-free invocation corrections."""

from __future__ import annotations

import re
from typing import NoReturn, cast

from pydantic import JsonValue, ValidationError
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
)

from .contracts import (
    MAX_MANAGED_FILES,
    NATIVE_FILE_ID_PATTERN,
    Action,
    BrowserFailure,
    ExecutionBlocked,
    InputOrigin,
    SaverError,
)

CORRECTABLE_REFUSALS = frozenset({"input_too_large", "input_count_limit", "input_total_too_large"})
INPUT_ACCESS_REFUSALS = frozenset(
    {"file_unavailable", "file_access_denied", "provider_bytes_unavailable"}
)


def refusal_origin(details: dict[str, JsonValue]) -> InputOrigin:
    value = details.get("input_origin")
    return "new" if value == "new" else "inherited" if value == "inherited" else ""


def correctable_refusal(code: str, origin: InputOrigin = "") -> bool:
    return (
        code == "dependency_unavailable"
        or code in CORRECTABLE_REFUSALS
        and origin != "inherited"
        or code in INPUT_ACCESS_REFUSALS
        and origin == "new"
    )


AGENT_GUIDANCE = (
    "Use the execution, persistence and permitted-action facts independently. "
    "Correct an invalid call or program rather than repeating it unchanged. "
    "If the task cannot be completed, explain the cause and next step in the user's "
    "request language. Never rerun an unknown execution or completed code just to save files."
)
WORKSPACE_CAPABILITIES: dict[str, JsonValue] = {
    "globals_preserved": False,
    "native_processes": False,
    "shell": "browser just-bash; selected file/text commands, not host executables",
    "retained_state": "Use scripts/state/ for persistent data; import names in every Python call.",
}
FAILURE_FRAMES = frozenset(
    {
        "execute_code",
        "execute_workspace",
        "browser_call",
        "workspace_reply",
        "resume_publication",
        "bind",
        "initialize",
        "validate_generation",
        "validate_context",
        "validate_invocation",
        "read_file",
        "referenced_inputs",
        "store_slot",
        "stage_native_snapshot",
        "save_native_snapshot",
        "reconcile_native_uploads",
        "adopt_native_upload",
        "find_native_upload",
        "verify_slot",
        "publish",
        "verify_publication",
        "get",
        "load",
        "mark",
    }
)


def exception_diagnostic(error: Exception) -> dict[str, JsonValue]:
    """Select known structural categories and owned frames, never arbitrary names/text."""
    kind = next(
        (
            kind.__name__
            for kind in (
                BrowserFailure,
                ExecutionBlocked,
                SaverError,
                ValidationError,
                OperationalError,
                IntegrityError,
                ProgrammingError,
                DBAPIError,
                SQLAlchemyError,
                TimeoutError,
                TypeError,
                ValueError,
                OSError,
                RuntimeError,
            )
            if isinstance(error, kind)
        ),
        "Exception",
    )
    result: dict[str, JsonValue] = {"error_type": kind}
    tb = error.__traceback__
    while tb:
        owner = tb.tb_frame.f_globals.get("__name__")
        name = tb.tb_frame.f_code.co_name
        if (
            isinstance(owner, str)
            and (owner == __name__ or owner.startswith("pyodide_file_saver."))
            and name in FAILURE_FRAMES
        ):
            result.update(failure_frame=name, failure_line=min(tb.tb_lineno, 100000))
        tb = tb.tb_next
    return result


def tool_result(
    fields: dict[str, JsonValue], *, error_code: str | None = None, message: str = "Tool completed"
) -> dict[str, JsonValue]:
    """Use native OUI's top-level error shape while preserving existing fields."""
    return {
        **fields,
        "status": "error" if error_code else "success",
        "error_code": error_code or "none",
        "error": message if error_code else None,
        "message": message,
        "agent_guidance": AGENT_GUIDANCE,
        "capabilities": WORKSPACE_CAPABILITIES.copy(),
        "permitted_actions": fields.get("permitted_actions", []),
        **({"incomplete": True} if error_code else {}),
    }


def validate_invocation(
    code: object,
    action: object,
    operation_id: object,
    language: object,
    input_file_ids: object = None,
) -> Action:
    """Do not expose rejected values or Pydantic input/context in error details."""

    def refuse(
        error_code: str, field: str, message: str, example: dict[str, JsonValue]
    ) -> NoReturn:
        raise SaverError(
            message,
            error_code,
            details={"field": field, "type": error_code, "example": example},
        )

    execute_example: dict[str, JsonValue] = {"action": "execute", "code": "print(1)"}
    status_example: dict[str, JsonValue] = {"action": "status"}
    if input_file_ids is not None:
        if action != "execute":
            refuse(
                "unexpected_input_selection",
                "input_file_ids",
                "Only execute accepts input_file_ids; omit it for status/retry/recover.",
                status_example,
            )
        if not isinstance(input_file_ids, list):
            refuse(
                "invalid_input_selection",
                "input_file_ids",
                "Use a list of distinct native File IDs from the current attachments.",
                execute_example,
            )
        selection = cast(list[object], input_file_ids)
        if len(selection) > MAX_MANAGED_FILES or not all(
            isinstance(item, str) and NATIVE_FILE_ID_PATTERN.fullmatch(item) for item in selection
        ):
            refuse(
                "invalid_input_selection",
                "input_file_ids",
                "Use a list of distinct native File IDs from the current attachments.",
                execute_example,
            )
        if len(set(cast(list[str], selection))) != len(selection):
            refuse(
                "invalid_input_selection",
                "input_file_ids",
                "Select each attached File ID at most once.",
                execute_example,
            )
    if not isinstance(action, str) or action not in {"execute", "status", "retry", "recover"}:
        refuse(
            "invalid_action",
            "action",
            "Use action execute, status, retry or recover.",
            status_example,
        )
    if not isinstance(language, str) or language not in {"python", "bash"}:
        refuse("unsupported_language", "language", "Use language python or bash.", execute_example)
    if not isinstance(code, str):
        refuse("invalid_code", "code", "Code must be source text for execute.", execute_example)
    if not isinstance(operation_id, str) or (
        operation_id and not re.fullmatch(r"[0-9a-f-]{36}", operation_id)
    ):
        refuse(
            "invalid_operation_id",
            "operation_id",
            "Use a returned operation_id; status without an ID lists recent operations.",
            status_example,
        )
    if action == "execute":
        if not code.strip():
            refuse("empty_program", "code", "Execute requires nonblank code.", execute_example)
        if operation_id:
            refuse(
                "unexpected_operation_id",
                "operation_id",
                "Execute creates a new operation; omit operation_id. "
                "To inspect saved work use status.",
                status_example,
            )
    else:
        if code:
            refuse(
                "unexpected_code",
                "code",
                "Status, retry and recover accept no code.",
                status_example,
            )
        if action in {"retry", "recover"} and not operation_id:
            refuse(
                "missing_operation_id",
                "operation_id",
                "Retry/recover require the returned operation_id and no code; "
                "use status to find it.",
                status_example,
            )
    try:
        return Action.model_validate(
            {
                "code": code,
                "action": action,
                "operation_id": operation_id,
                "language": language,
                "cwd": ".",
                "input_file_ids": input_file_ids,
            }
        )
    except ValidationError as error:
        raise SaverError("Use valid action-specific arguments.", "invalid_invocation") from error


def select_input_attachments(
    attachments: list[dict[str, JsonValue]],
    selected: list[str] | None,
) -> list[dict[str, JsonValue]]:
    """Selection narrows injected attachments; it never admits arbitrary File IDs."""
    if selected is None:
        return attachments
    candidates = [item for item in attachments if item.get("type") in (None, "file")]
    available: set[str] = set()
    for item in candidates:
        value = item.get("id")
        if isinstance(value, str):
            available.add(value)
    if not set(selected).issubset(available):
        raise SaverError(
            "Select only File IDs from the current attachments; selection cannot grant file access",
            "invalid_input_selection",
            details={"field": "input_file_ids", "type": "unavailable_selection"},
        )
    chosen = set(selected)
    return [item for item in candidates if isinstance(item.get("id"), str) and item["id"] in chosen]
