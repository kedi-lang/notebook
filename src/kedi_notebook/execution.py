from __future__ import annotations

from collections.abc import Collection
from typing import Any

from kedi.errors import KediExecutionError, KediPythonTrace, KediTraceFrame

NOTEBOOK_SOURCE_PATHS = frozenset({"<notebook>"})


def format_execution_output(stdout: str, result: Any) -> str:
    if result is None:
        return stdout
    separator = "\n" if stdout and not stdout.endswith("\n") else ""
    return f"{stdout}{separator}{result}"


def execution_error_payload(
    exc: BaseException,
    *,
    source_paths: Collection[str] = NOTEBOOK_SOURCE_PATHS,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
    diagnostic = execution_error_diagnostic(exc, source_paths=source_paths)
    if diagnostic is not None:
        payload["diagnostic"] = diagnostic
    return payload


def execution_error_diagnostic(
    exc: BaseException,
    *,
    source_paths: Collection[str] = NOTEBOOK_SOURCE_PATHS,
) -> dict[str, Any] | None:
    if not isinstance(exc, KediExecutionError):
        return None

    for trace in reversed(exc.python_traces):
        diagnostic = _python_trace_diagnostic(exc, trace, source_paths)
        if diagnostic is not None:
            return diagnostic

    for frame in reversed(exc.frames):
        diagnostic = _frame_diagnostic(exc, frame, source_paths)
        if diagnostic is not None:
            return diagnostic
    return None


def _python_trace_diagnostic(
    exc: KediExecutionError,
    trace: KediPythonTrace,
    source_paths: Collection[str],
) -> dict[str, Any] | None:
    span = trace.span
    if span is None or span.source_path not in source_paths or trace.kedi_lineno is None:
        return None
    return {
        "source": span.source_path,
        "line": trace.kedi_lineno,
        "column": 1,
        "message": exc.message,
        "kind": trace.kind,
    }


def _frame_diagnostic(
    exc: KediExecutionError,
    frame: KediTraceFrame,
    source_paths: Collection[str],
) -> dict[str, Any] | None:
    span = frame.span
    if span is None or span.source_path not in source_paths:
        return None
    return {
        "source": span.source_path,
        "line": span.start_line,
        "column": span.start_col,
        "message": exc.message,
        "kind": frame.kind,
    }


__all__ = [
    "execution_error_diagnostic",
    "execution_error_payload",
    "format_execution_output",
]
