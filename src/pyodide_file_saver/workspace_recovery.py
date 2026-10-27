"""Actionable recovery from durable facts, without replaying user programs."""

from __future__ import annotations

from pydantic import JsonValue

from .contracts import OperationDiagnostics, Receipt


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
        committed = receipt.checkpoint_version == 2 and all(
            slot.committed for slot in receipt.blobs
        )
        return {
            "execution": "completed",
            "next_step": "finish_publication" if committed else "save_completed_files",
            "tool_actions": ["status", "retry" if committed else "recover"],
            "needs_browser": not committed,
            "instructions": (
                "Use retry with this operation_id and no code; all reserved bytes were saved."
                if committed
                else "Use recover with this operation_id and no code in the original live "
                "browser. The program finished; do not execute it again to save its files."
            ),
        }
    if diagnostic.dispatch == "not_dispatched" and diagnostic.failure_code:
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
