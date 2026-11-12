"""A compatible reader precedes producers that write optional numeric evidence."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pyodide_file_saver.contracts import FailureCause, OperationDiagnostics


def test_legacy_cause_serialization_is_unchanged_even_when_nested() -> None:
    legacy = {
        "code": "retained_count_limit",
        "exception_type": "ValueError",
        "frame": "managed_workspace",
        "line": 123,
        "http_status": 0,
    }
    cause = FailureCause.model_validate(legacy)
    assert cause.model_dump() == legacy
    diagnostic = OperationDiagnostics(cause=cause, first_failure_cause=cause)
    wire = json.loads(diagnostic.model_dump_json())
    assert wire["cause"] == legacy and wire["first_failure_cause"] == legacy
    saved = OperationDiagnostics.model_validate_json(diagnostic.model_dump_json())
    assert saved.model_dump()["cause"] == legacy
    assert saved.model_dump()["first_failure_cause"] == legacy


@pytest.mark.parametrize("timeout", [False, True])
def test_compatible_reader_preserves_numeric_evidence_on_roundtrip(timeout: bool) -> None:
    cause = (
        FailureCause(
            code="callback_timeout",
            exception_type="TimeoutError",
            elapsed_ms=55001,
            configured_wait_ms=55000,
        )
        if timeout
        else FailureCause(
            code="retained_count_limit", observed=65, limit=64, measurement="lower_bound"
        )
    )
    old_reader_compatible = OperationDiagnostics(cause=cause, first_failure_cause=cause)
    restored = OperationDiagnostics.model_validate_json(old_reader_compatible.model_dump_json())
    restored.phase = "recover"
    assert restored.cause == cause and restored.first_failure_cause == cause
    assert OperationDiagnostics.model_validate_json(restored.model_dump_json()).cause == cause


@pytest.mark.parametrize(
    "fields",
    [
        {"observed": 65},
        {"observed": True, "limit": 64, "measurement": "exact"},
        {"observed": 2**53, "limit": 64, "measurement": "exact"},
        {"elapsed_ms": -1},
        {"configured_wait_ms": 3600001},
        {"payload": "private-marker"},
    ],
)
def test_diagnostics_accept_only_bounded_complete_numeric_facts(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FailureCause.model_validate({"code": "retained_count_limit", **fields})
