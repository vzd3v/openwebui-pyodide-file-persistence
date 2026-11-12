"""Actionable recovery from durable facts, without replaying user programs."""

from __future__ import annotations

from pydantic import JsonValue

from .contracts import FailureCause, OperationDiagnostics, Receipt
from .tool_results import INPUT_ACCESS_REFUSALS, correctable_refusal
from .workspace_manifest import retained_ready

RETAINED_LIMIT_POLICIES = {
    "retained_count_limit": "max_files",
    "retained_file_limit": "max_file_bytes",
    "retained_total_limit": "max_total_bytes",
}


def ui_locale(metadata: dict[str, JsonValue]) -> str:
    """Use the native UI preference for static copy, never as an authority or query language."""
    variables = metadata.get("variables")
    value = variables.get("{{USER_LANGUAGE}}") if isinstance(variables, dict) else None
    return "ru" if isinstance(value, str) and value.lower() in {"ru", "ru-ru"} else "en"


def retained_limit_message(cause: FailureCause, locale: str = "en") -> str:
    """One bounded explanation for the model result, recovery plan and native error card."""
    policy = RETAINED_LIMIT_POLICIES[cause.code]
    russian = locale == "ru"
    subject = {
        "retained_count_limit": ("Retained file count", "Количество сохраняемых файлов"),
        "retained_file_limit": ("One retained file's size", "Размер одного сохраняемого файла"),
        "retained_total_limit": ("Retained total size", "Общий размер сохраняемых файлов"),
    }[cause.code][int(russian)]
    if cause.observed is not None and cause.limit is not None:
        bound = (
            ("не менее " if russian else "at least ") if cause.measurement == "lower_bound" else ""
        )
        unit = "" if cause.code == "retained_count_limit" else (" байт" if russian else " bytes")
        constraint = (
            f"{subject}: {bound}{cause.observed}{unit}; лимит {cause.limit}{unit} ({policy}). "
            if russian
            else f"{subject}: {bound}{cause.observed}{unit}; limit {cause.limit}{unit} ({policy}). "
        )
    else:
        constraint = (
            f"{subject} превышает лимит ({policy}); точное значение не записано. "
            if russian
            else f"{subject} exceeds the limit ({policy}); numeric evidence was not recorded. "
        )
    if cause.code == "retained_count_limit":
        constraint += (
            "Служебные записи выполнения тоже учитываются. "
            if russian
            else "Private run records count too. "
        )
    if russian:
        return (
            "Программа завершилась, но файлы ещё не сохранены. "
            + constraint
            + f"С прежним лимитом recover снова откажет. Сохраните исходную вкладку. "
            f"Попросите администратора проверить и при необходимости увеличить {policy}, "
            "затем вызовите recover для той же операции без кода в исходном живом Worker. "
            "Не запускайте программу повторно; не выполняйте очистку или изменение размеров файлов."
        )
    return (
        "The program completed, but its files are not saved. "
        + constraint
        + f"Recovery with the unchanged limit will fail again. Keep the original tab. "
        f"Ask an administrator to review and, if appropriate, raise {policy} sufficiently, "
        "then recover the same operation without code in the original live worker. "
        "Do not rerun the program or execute cleanup/resizing code."
    )


def unbound_gate_message(
    error_code: JsonValue, operation_id: JsonValue, locale: str = "en"
) -> str | None:
    """Explain only a known pre-allocation gate, without revealing another chat's IDs."""
    if operation_id != "" or error_code not in (
        "execution_request_blocked",
        "execution_session_blocked",
    ):
        return None
    if locale == "ru":
        return (
            "Не удалось запустить код: выполнение заблокировано после предыдущего сбоя "
            "в запросе или сессии. Текущий код не запускался, сохранённые файлы не изменились. "
            "Проверьте сообщение об ошибке и план восстановления в исходном чате; "
            "здесь данные предыдущей операции недоступны. "
            "Если не удаётся найти исходный чат, обратитесь к администратору. "
            "Не повторяйте выполнение в этом запросе: переподключение само по себе "
            "не подтверждает, что прежнюю программу безопасно запустить заново."
        )
    return (
        "Code could not start: execution is blocked after an earlier request or session failure. "
        "The current code did not run and saved files are unchanged. Check the error and "
        "recovery plan in the original chat; the earlier operation's details are unavailable "
        "here. If you cannot find the original chat, contact an administrator. "
        "Do not repeat execution in this request: reconnecting alone does not establish "
        "that the earlier program is safe to run again."
    )


def native_failure_message(
    receipt: dict[str, JsonValue],
    locale: str,
    diagnostic: dict[str, JsonValue] | None = None,
) -> str:
    """Deterministic terminal copy; it requires no additional model continuation."""
    if (
        not receipt
        and diagnostic
        and (
            diagnostic.get("execution_outcome") == "not_started"
            and diagnostic.get("persistence_outcome") == "unchanged"
        )
    ):
        message = unbound_gate_message(
            diagnostic.get("error_code"), diagnostic.get("operation_id"), locale
        )
        if message:
            return message
    completed_unsaved = (
        receipt.get("execution_completed") is True and receipt.get("workspace_saved") is not True
    )
    cause = receipt.get("cause")
    if (
        completed_unsaved
        and isinstance(cause, dict)
        and cause.get("code") in RETAINED_LIMIT_POLICIES
    ):
        return retained_limit_message(FailureCause.model_validate(cause), locale)
    russian = locale == "ru"
    if completed_unsaved:
        return (
            "Программа завершилась, но её файлы не сохранены. Новое выполнение, включая очистку "
            "и изменение размеров, заблокировано. Проверьте status и план восстановления; "
            "не запускайте программу повторно."
            if russian
            else "The program finished, but its files could not be saved. "
            "New execution, including cleanup or resizing, is blocked. "
            "Inspect status and the recovery plan; do not rerun the program."
        )
    if receipt.get("execution_outcome") == "not_started":
        return (
            "Программа не запускалась. Проверьте status операции и её план восстановления."
            if russian
            else "The program did not start. Inspect operation status and follow its recovery plan."
        )
    if receipt.get("workspace_saved") is True:
        return (
            "Результаты сохранены, но новое выполнение заблокировано в этом запросе или сессии "
            "браузера. Проверьте status и план восстановления."
            if russian
            else "Existing results are saved, but further execution is blocked in this request "
            "or browser session. Inspect status and follow its recovery plan."
        )
    return (
        "Исход выполнения неизвестен; программа может ещё работать. Сохраните исходную вкладку, "
        "проверьте status и не запускайте программу повторно."
        if russian
        else "The execution outcome is unresolved; the program may still be running. "
        "Keep the original tab, inspect status and do not rerun the program."
    )


def recovery_plan(receipt: Receipt, diagnostic: OperationDiagnostics) -> dict[str, JsonValue]:
    if receipt.checkpoint.committed:
        return {
            "execution": "completed" if receipt.execution_completed else "unknown",
            "next_step": "verify_saved_files",
            "tool_actions": ["status", "retry"],
            "needs_browser": False,
            "instructions": "Use retry with this operation_id and no code to verify saved bytes.",
        }
    if receipt.execution_completed:
        committed = receipt.checkpoint_version == 2 and retained_ready(receipt)
        limit_failure = (
            diagnostic.cause
            if diagnostic.cause and diagnostic.cause.code in RETAINED_LIMIT_POLICIES
            else None
        )
        return {
            "execution": "completed",
            "next_step": "finish_publication" if committed else "save_completed_files",
            "tool_actions": (
                ["status", "retry", "recover"]
                if receipt.native_transfers is not None and not committed
                else ["status", "retry" if committed else "recover"]
            ),
            "needs_browser": not committed,
            "instructions": (
                retained_limit_message(limit_failure)
                if limit_failure and not committed
                else "Use retry with this operation_id and no code; all reserved bytes were saved."
                if committed
                else "Use retry without code to reconcile native uploads first; if files remain "
                "unsaved, use recover in the original live browser. Never rerun the program."
                if receipt.native_transfers is not None
                else "Use recover with this operation_id and no code in the original live "
                "browser. The program finished; do not execute it again to save its files."
            ),
        }
    if diagnostic.dispatch == "not_dispatched" and diagnostic.failure_code:
        if diagnostic.failure_code == "dependency_unavailable":
            return {
                "execution": "not_started",
                "next_step": "correct_dependencies",
                "tool_actions": ["status", "execute"],
                "needs_browser": True,
                "instructions": "The program did not start. Use supported explicit imports or "
                "approved package loading; ask an administrator to check catalog asset delivery "
                "when needed. Then submit a corrected execute; do not repeat an unchanged failure.",
            }
        if (
            diagnostic.failure_code in INPUT_ACCESS_REFUSALS
            or diagnostic.input_origin == "inherited"
        ):
            correction = correctable_refusal(diagnostic.failure_code, diagnostic.input_origin)
            return {
                "execution": "not_started",
                "next_step": "correct_inputs" if correction else "restore_required_input_access",
                "tool_actions": ["status", "execute"] if correction else ["status"],
                "needs_browser": False,
                "instructions": (
                    "The program did not start. A newly selected input is unavailable. Restore "
                    "its access, or explicitly select a permitted attachment subset for a "
                    "corrected execute. Explain any limitation before omitting a required file."
                    if correction
                    else "The program did not start. Restore availability/access of the required "
                    "input or review the configured input limits. input_file_ids cannot remove "
                    "acknowledged inputs. Preserve the saved head; reconnecting cannot repair this."
                ),
            }
        if diagnostic.failure_code == "browser_restore_failed":
            return {
                "execution": "not_started",
                "next_step": "restore_required_files",
                "tool_actions": ["status"],
                "needs_browser": True,
                "needs_new_request": True,
                "instructions": "The program did not start. Check availability, access and exact "
                "bytes of the required saved/input Files before a new request. Preserve the "
                "acknowledged head; reconnecting alone cannot repair deleted or changed files.",
            }
        if correctable_refusal(diagnostic.failure_code, diagnostic.input_origin):
            return {
                "execution": "not_started",
                "next_step": "correct_inputs",
                "tool_actions": ["status", "execute"],
                "needs_browser": True,
                "instructions": (
                    "The program did not start. Correct the selected inputs or reduce their size, "
                    "then submit a corrected execute. Do not repeat the unchanged rejected call."
                ),
            }
        return {
            "execution": "not_started",
            "next_step": "reconnect_before_new_execution",
            "tool_actions": ["status"],
            "needs_browser": True,
            "needs_new_request": True,
            "instructions": (
                "This program was not dispatched. Explain the connection failure and keep "
                "earlier results. After reconnecting, a new user request may execute the "
                "program from the acknowledged head. Automatic reconnect could not establish "
                "the original runtime."
            ),
        }
    return {
        "execution": "unknown",
        "next_step": "inspect_original_runtime",
        "tool_actions": ["status", "recover"],
        "needs_browser": True,
        "instructions": (
            "Inspect status and preserve the original tab. The program may still be running. "
            "Use recover without code only after it completes in that runtime; a reconnect "
            "does not establish the outcome or authorize another execution."
        ),
    }
